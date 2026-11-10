"""The contract every photo source implements, plus the path jail.

Walks are streaming and resumable: iter_entries(since=cursor) yields entries
whose rel_path sorts after the cursor, so an interrupted thousand-photo scan
picks up where the worker last committed — no re-walk of completed work at
the insert level, and INSERT OR IGNORE makes a re-yield free anyway.

rel_path is always POSIX-style and source-root relative. Every adapter must
jail it: no absolute paths, no '..', and the resolved target must sit under
the source root (same doctrine as the echo save path in api/rag.py).
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional

# What counts as a photo. RAW formats are deliberately absent in v1.
IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "heic", "heif"}


class SourceError(RuntimeError):
    """Any source-side failure, phrased for a human reading the card."""


@dataclass
class PicEntry:
    rel_path: str      # POSIX, relative to the source root
    name: str
    size: int
    mtime_iso: str     # UTC ISO, from the filesystem/server


def image_ext(name: str) -> str:
    """Lowercase extension of a filename ('' when there is none)."""
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_image_name(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return not base.startswith(".") and image_ext(base) in IMAGE_EXT


def jail_join(root: Path, rel_path: str) -> Path:
    """root / rel_path, refusing anything that could escape the root.

    Raises SourceError (never returns a dangerous path). The symlink case is
    covered by resolving both sides before comparing.
    """
    p = PurePosixPath(rel_path or "")
    if p.is_absolute() or not p.parts or ".." in p.parts:
        raise SourceError(f"路径越界：{rel_path!r} 必须是源根内的相对路径")
    root_real = Path(os.path.realpath(root))
    target = root_real.joinpath(*p.parts)
    if not str(os.path.realpath(target)).startswith(str(root_real) + os.sep) \
            and os.path.realpath(target) != str(root_real):
        raise SourceError(f"路径越界：{rel_path!r} 解析后离开了源根")
    return target


def mtime_iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


class ImageSource:
    """Uniform read-only view of one photo source (plus delete for purge)."""

    kind = ""

    def probe(self) -> dict:
        """Connectivity + root existence check for the card's Probe button.
        Returns {reachable, root_ok, error, latency_ms}; never raises."""
        raise NotImplementedError

    def iter_entries(self, since: Optional[str] = None) -> Iterator[PicEntry]:
        """Streaming walk of image files; only entries after ``since``."""
        raise NotImplementedError

    def get_bytes(self, rel_path: str, max_bytes: Optional[int] = None) -> bytes:
        """Whole file, or its first max_bytes (for content hashing)."""
        raise NotImplementedError

    def stream(self, rel_path: str, max_bytes: int = 0):
        """Yield the file in chunks for original-image responses."""
        raise NotImplementedError

    def delete(self, rel_path: str) -> None:
        """Remove the ORIGINAL file — only ever called by an explicit purge."""
        raise NotImplementedError

    def close(self) -> None:
        pass
