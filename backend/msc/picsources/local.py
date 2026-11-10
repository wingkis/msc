"""Local filesystem source: a directory bind-mounted into the container.

The root must sit inside MSC_PIC_ROOTS (validated when the card is created);
this adapter just walks it. Walk order is deterministic (sorted DFS) and
``since`` skips everything lexicographically at or before the cursor, which
makes the worker's resume cheap on local disks.
"""

import os
from pathlib import Path
from typing import Iterator, Optional

from msc.picsources.base import (
    ImageSource, PicEntry, SourceError, is_image_name, jail_join, mtime_iso,
)


class LocalSource(ImageSource):
    kind = "local"

    def __init__(self, root_path: str):
        self.root = Path((root_path or "").strip())
        if not str(self.root):
            raise SourceError("本地源还没有填目录路径")

    def _resolve(self, rel_path: str) -> Path:
        return jail_join(self.root, rel_path)

    def probe(self) -> dict:
        try:
            real = Path(os.path.realpath(self.root))
            ok = real.is_dir()
            return {"reachable": True, "root_ok": ok,
                    "error": None if ok else f"目录不存在：{self.root}",
                    "latency_ms": 0}
        except OSError as exc:
            return {"reachable": False, "root_ok": False, "error": str(exc), "latency_ms": 0}

    def iter_entries(self, since: Optional[str] = None) -> Iterator[PicEntry]:
        if not self.root.is_dir():
            raise SourceError(f"目录不存在：{self.root}")
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            rel_dir = Path(dirpath).relative_to(self.root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            for name in sorted(filenames):
                if not is_image_name(name):
                    continue
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if since is not None and rel <= since:
                    continue
                try:
                    st = os.stat(os.path.join(dirpath, name))
                except OSError:  # vanished mid-walk: skip, next rescan sees truth
                    continue
                yield PicEntry(rel_path=rel, name=name, size=st.st_size,
                               mtime_iso=mtime_iso(st.st_mtime))

    def get_bytes(self, rel_path: str, max_bytes: Optional[int] = None) -> bytes:
        target = self._resolve(rel_path)
        with open(target, "rb") as fh:
            return fh.read(max_bytes) if max_bytes else fh.read()

    def stream(self, rel_path: str, max_bytes: int = 0):
        target = self._resolve(rel_path)
        size = target.stat().st_size
        if max_bytes and size > max_bytes:
            raise SourceError(f"原图 {size // (1024 * 1024) + 1}MB 超过上限 {max_bytes // (1024 * 1024)}MB")
        with open(target, "rb") as fh:
            while chunk := fh.read(65536):
                yield chunk

    def delete(self, rel_path: str) -> None:
        target = self._resolve(rel_path)
        try:
            target.unlink()
        except FileNotFoundError:
            pass  # already gone on disk: the purge proceeds to drop the row
