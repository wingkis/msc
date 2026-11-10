"""SMB source via smbprotocol — direct client, no host mount.

NAS boxes on the LAN answer SMB with nothing configured on the DGX side, so
the card carries host/share/credentials and this adapter connects on demand.
The session is cached per adapter; an OSError mid-operation drops it so the
next attempt re-registers — that is the whole reconnect story, and item
level backoff (retry_at) covers the hiccup.

smbprotocol is imported lazily: a host without the wheel can still run every
other feature; only SMB cards fail, with a message that names the package.
Kerberos is not supported (NTLM only) — documented, not guessed at.
"""

import os
from pathlib import PurePosixPath
from typing import Iterator, Optional

from msc.picsources.base import (
    ImageSource, PicEntry, SourceError, is_image_name, mtime_iso,
)


def _smbclient():
    try:
        import smbclient
        return smbclient
    except ImportError as exc:
        raise SourceError("服务器缺少 smbprotocol 库（pip install smbprotocol）") from exc


class SmbSource(ImageSource):
    kind = "smb"

    def __init__(self, host: str, share: str, subpath: str = "",
                 username: str = "", password: str = ""):
        self.host = (host or "").strip()
        self.share = (share or "").strip()
        self.subpath = (subpath or "").strip().strip("/")
        self.username = (username or "").strip()
        self.password = password or ""
        self._session = False
        if not self.host or not self.share:
            raise SourceError("SMB 源需要服务器地址和共享名")

    # -- paths --------------------------------------------------------

    def _uri(self, rel: str = "") -> str:
        # Windows-style UNC: \\host\share\sub\rel. rel is already jailed.
        parts = [self.host, self.share]
        parts.extend(p for p in self.subpath.split("/") if p)
        parts.extend(p for p in PurePosixPath(rel or "").parts)
        return "\\\\" + "\\".join(parts)

    def _jail(self, rel_path: str) -> str:
        p = PurePosixPath(rel_path or "")
        if p.is_absolute() or not p.parts or ".." in p.parts:
            raise SourceError(f"路径越界：{rel_path!r} 必须是共享内的相对路径")
        return "/".join(p.parts)

    def _connect(self):
        smbclient = _smbclient()
        if self._session:
            return smbclient
        try:
            if self.username:
                smbclient.register_session(self.host, username=self.username,
                                           password=self.password)
            else:
                # No guest= kwarg across smbprotocol versions: an empty
                # session is what NAS/Mac guest shares answer to; servers
                # that demand credentials raise and the card shows it.
                smbclient.register_session(self.host)
        except Exception as exc:
            raise SourceError(f"连接 {self.host} 失败：{exc}") from exc
        self._session = True
        return smbclient

    def _drop(self):
        self._session = False

    # -- ImageSource --------------------------------------------------

    def probe(self) -> dict:
        import time
        started = time.perf_counter()
        try:
            smbclient = self._connect()
            st = smbclient.stat(self._uri())
            ok = True
            error = None if st.is_dir() else "共享里找不到该子目录"
        except SourceError as exc:
            self._drop()
            return {"reachable": False, "root_ok": False, "error": str(exc),
                    "latency_ms": int((time.perf_counter() - started) * 1000)}
        except OSError as exc:
            self._drop()
            return {"reachable": True, "root_ok": False, "error": str(exc)[:300],
                    "latency_ms": int((time.perf_counter() - started) * 1000)}
        return {"reachable": True, "root_ok": ok and error is None,
                "error": error, "latency_ms": int((time.perf_counter() - started) * 1000)}

    def iter_entries(self, since: Optional[str] = None) -> Iterator[PicEntry]:
        smbclient = self._connect()
        # Sorted DFS with an explicit stack, mirroring LocalSource so cursors
        # mean the same thing across kinds.
        stack = [""]
        while stack:
            cur = stack.pop()
            try:
                with smbclient.scandir(self._uri(cur)) as scan:
                    dirs, files = [], []
                    for entry in scan:
                        name = entry.name
                        if name.startswith("."):
                            continue
                        try:
                            if entry.is_dir():
                                dirs.append(name)
                            elif is_image_name(name):
                                files.append(entry)
                        except OSError:
                            continue
            except OSError:
                self._drop()  # stale handle: next operation re-registers
                raise
            for name in sorted(dirs, reverse=True):
                stack.append(f"{cur}/{name}" if cur else name)
            for entry in sorted(files, key=lambda e: e.name):
                rel = f"{cur}/{entry.name}" if cur else entry.name
                if since is not None and rel <= since:
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                yield PicEntry(rel_path=rel, name=entry.name, size=st.st_size or 0,
                               mtime_iso=mtime_iso(st.last_write_time or 0))

    def get_bytes(self, rel_path: str, max_bytes: Optional[int] = None) -> bytes:
        uri = self._uri(self._jail(rel_path))
        smbclient = self._connect()
        try:
            with smbclient.open(uri, "rb") as fh:
                return fh.read(max_bytes) if max_bytes else fh.read()
        except OSError:
            self._drop()
            raise

    def stream(self, rel_path: str, max_bytes: int = 0):
        uri = self._uri(self._jail(rel_path))
        smbclient = self._connect()
        try:
            with smbclient.open(uri, "rb") as fh:
                if max_bytes:
                    if fh.seek(0, os.SEEK_END) > max_bytes:
                        raise SourceError("原图超过大小上限")
                    fh.seek(0)
                while chunk := fh.read(65536):
                    yield chunk
        except OSError:
            self._drop()
            raise

    def delete(self, rel_path: str) -> None:
        uri = self._uri(self._jail(rel_path))
        smbclient = self._connect()
        try:
            smbclient.remove(uri)
        except FileNotFoundError:
            pass
        except OSError:
            self._drop()
            raise

    def close(self) -> None:
        self._session = False  # smbprotocol caches sessions process-wide; drop ours
