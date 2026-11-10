"""Photo source adapters: one class per kind, dispatched by build_source().

A source row in the database carries the address and credentials; an adapter
turns that row into a uniform walk/read/stream interface the worker and API
never have to specialise per kind. Remote kinds (smb, webdav) arrive in a
later pass — the interface is the same from day one.
"""

from msc.models import PicSource
from msc.picsources.base import (  # noqa: F401 - re-exported for callers
    IMAGE_EXT, ImageSource, PicEntry, SourceError, image_ext, jail_join,
)


def build_source(row: PicSource):
    """Instantiate the adapter for one pic_sources row. Raises SourceError
    for a kind or address the row cannot actually serve."""
    kind = (row.kind or "").strip()
    if kind == "local":
        from msc.picsources.local import LocalSource
        return LocalSource(row.root_path)
    if kind == "smb":
        from msc.picsources.smb import SmbSource
        return SmbSource(row.host, row.share, row.subpath, row.username, row.password)
    if kind == "webdav":
        from msc.picsources.webdav import WebDavSource
        return WebDavSource(row.url, row.username, row.password)
    raise SourceError(f"未知照片源类型 {kind!r}（支持 local/smb/webdav）")
