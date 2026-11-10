"""WebDAV source: PROPFIND walks over https, no extra dependency.

httpx speaks the protocol well enough in ~100 lines: Depth-1 PROPFIND per
directory for the walk, Range GETs for content hashing, plain GET streaming
for originals. Credentials use Basic auth (Digest/Ntlm servers are a known
limitation for v1 — the probe reports 401 honestly instead of looping).
"""

import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from typing import Iterator, Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from msc.picsources.base import ImageSource, PicEntry, SourceError, is_image_name


def _local(tag: str) -> str:
    """Strip the {namespace} from an XML tag name; DAV servers spell it many ways."""
    return tag.rsplit("}", 1)[-1]


def parse_propfind(xml_text: str, base_path: str):
    """(dirs, files) of href paths relative to base_path, from a Depth-1 body.

    Only 200/207 propstat entries count; resourcetype>collection decides
    dirs. hrefs arrive percent-encoded absolute paths — we diff them against
    the queried path so nesting stays correct on servers that answer with
    full URLs or bare names alike.
    """
    dirs, files = [], []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceError(f"WebDAV 响应不是合法 XML: {exc}") from exc
    if _local(root.tag) == "multistatus":
        responses = [c for c in root if _local(c.tag) == "response"]
    else:
        responses = [root]
    for resp in responses:
        status_ok = True
        href = None
        is_collection = False
        length = 0
        mtime = ""
        for prop in resp.iter():
            kind = _local(prop.tag)
            if kind == "status" and prop.text:
                status_ok = " 200 " in f" {prop.text.strip()} " or "207" in prop.text
            elif kind == "href":
                href = (prop.text or "").strip()
            elif kind == "collection":
                is_collection = True
            elif kind == "getcontentlength":
                try:
                    length = int((prop.text or "0").strip())
                except ValueError:
                    length = 0
            elif kind == "getlastmodified":
                mtime = (prop.text or "").strip()
        if not href or not status_ok:
            continue
        path = unquote(urlsplit(href).path or href)
        if path.endswith("/"):
            path = path.rstrip("/")
        if path == base_path.rstrip("/") or not path.startswith(base_path.rstrip("/") + "/"):
            continue  # the collection itself, or a foreign href
        rel = path[len(base_path.rstrip("/")) + 1:]
        if not rel or rel.startswith("."):
            continue
        (dirs if is_collection else files).append((rel, length, mtime))
    return dirs, files


class WebDavSource(ImageSource):
    kind = "webdav"

    def __init__(self, url: str, username: str = "", password: str = ""):
        raw = (url or "").strip()
        if "://" not in raw:
            raise SourceError("WebDAV 源需要完整 URL（http:// 或 https://）")
        parts = urlsplit(raw)
        self.base = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
        self.username = (username or "").strip()
        self.password = password or ""

    def _client(self) -> httpx.Client:
        auth = httpx.BasicAuth(self.username, self.password) if self.username else None
        return httpx.Client(auth=auth, timeout=30.0, follow_redirects=True)

    def _propfind(self, client: httpx.Client, path: str, depth: str = "1") -> httpx.Response:
        body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
                "<d:prop><d:resourcetype/><d:getcontentlength/>"
                "<d:getlastmodified/></d:prop></d:propfind>")
        return client.request("PROPFIND", self.base + quote(path), data=body,
                              headers={"Depth": depth, "Content-Type": "application/xml"})

    def probe(self) -> dict:
        import time
        started = time.perf_counter()
        try:
            with self._client() as client:
                response = self._propfind(client, "", depth="0")
        except httpx.HTTPError as exc:
            return {"reachable": False, "root_ok": False, "error": str(exc)[:300],
                    "latency_ms": int((time.perf_counter() - started) * 1000)}
        latency = int((time.perf_counter() - started) * 1000)
        if response.status_code in (401, 403):
            return {"reachable": True, "root_ok": False,
                    "error": f"鉴权被拒（HTTP {response.status_code}；仅支持 Basic 认证）",
                    "latency_ms": latency}
        ok = response.status_code in (200, 207, 405)  # 405: server without propfind on root
        return {"reachable": True, "root_ok": ok,
                "error": None if ok else f"HTTP {response.status_code}",
                "latency_ms": latency}

    def iter_entries(self, since: Optional[str] = None) -> Iterator[PicEntry]:
        try:
            yield from self._walk(since)
        except httpx.HTTPError as exc:
            # The worker catches SourceError, not httpx's family: an offline
            # cloud disk must park the source in "error", not crash the tick.
            raise SourceError(f"WebDAV 通信失败：{exc}") from exc

    def _walk(self, since: Optional[str]) -> Iterator[PicEntry]:
        with self._client() as client:
            stack = [""]
            while stack:
                cur = stack.pop()
                response = self._propfind(client, cur)
                if response.status_code not in (200, 207):
                    raise SourceError(f"PROPFIND {cur or '/'} → HTTP {response.status_code}")
                dirs, files = parse_propfind(response.text, cur)
                # DFS in sorted order: push reversed, pop the next-smallest.
                for rel, _, _ in sorted(dirs, reverse=True):
                    stack.append(f"{cur}/{rel}" if cur else rel)
                for rel, length, _ in sorted(files):
                    name = rel.rsplit("/", 1)[-1]
                    if not is_image_name(name):
                        continue
                    if since is not None and rel <= since:
                        continue
                    yield PicEntry(rel_path=rel, name=name, size=length, mtime_iso="")

    def get_bytes(self, rel_path: str, max_bytes: Optional[int] = None) -> bytes:
        rel = self._check(rel_path)
        headers = {"Range": f"bytes=0-{max_bytes - 1}"} if max_bytes else {}
        try:
            with self._client() as client:
                response = client.get(self.base + quote(rel), headers=headers)
        except httpx.HTTPError as exc:
            raise SourceError(f"WebDAV 通信失败：{exc}") from exc
        if response.status_code not in (200, 206):
            raise SourceError(f"GET {rel} → HTTP {response.status_code}")
        return response.content

    def stream(self, rel_path: str, max_bytes: int = 0):
        try:
            yield from self._stream(rel_path, max_bytes)
        except httpx.HTTPError as exc:
            raise SourceError(f"WebDAV 通信失败：{exc}") from exc

    def _stream(self, rel_path: str, max_bytes: int):
        rel = self._check(rel_path)
        with self._client() as client:
            with client.stream("GET", self.base + quote(rel)) as response:
                if response.status_code not in (200, 206):
                    raise SourceError(f"GET {rel} → HTTP {response.status_code}")
                delivered = 0
                for chunk in response.iter_bytes(65536):
                    delivered += len(chunk)
                    if max_bytes and delivered > max_bytes:
                        raise SourceError("原图超过大小上限")
                    yield chunk

    def delete(self, rel_path: str) -> None:
        rel = self._check(rel_path)
        try:
            with self._client() as client:
                response = client.delete(self.base + quote(rel))
        except httpx.HTTPError as exc:
            raise SourceError(f"WebDAV 通信失败：{exc}") from exc
        if response.status_code not in (200, 204, 404, 424):
            raise SourceError(f"DELETE {rel} → HTTP {response.status_code}")

    @staticmethod
    def _check(rel_path: str) -> str:
        p = PurePosixPath(rel_path or "")
        if p.is_absolute() or not p.parts or ".." in p.parts:
            raise SourceError(f"路径越界：{rel_path!r} 必须是源根内的相对路径")
        return "/".join(p.parts)
