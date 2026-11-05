"""Shared text helpers — the single source of truth for slugs, tags and
frontmatter hygiene. Everything that touches tag data goes through
``normalize_tags`` so stringified Python lists can never come back."""

import re
from typing import List
import hashlib

# Tags may arrive as a list, a comma separated string, or the wreckage of
# str(['a, b']) -> "['a, b']". Split on the separators, then strip the junk.
_TAG_SPLIT = re.compile(r"[,;、]")
_TAG_JUNK = "\"'[]{}"

_META_KEYS = {
    "name", "description", "tags", "priority", "category",
    "lifespan", "branch", "created", "updated",
}
_KEY_LINE = re.compile(r"^[a-zA-Z][\w-]*:\s*\S")


def normalize_tags(raw) -> List[str]:
    """Turn anything tag-shaped into a clean, ordered, de-duplicated list.

    >>> normalize_tags("['codex, sandbox']")
    ['codex', 'sandbox']
    >>> normalize_tags(["a", "b,a"])
    ['a', 'b']
    """
    if raw is None:
        return []

    parts: List[str]
    if isinstance(raw, str):
        parts = _TAG_SPLIT.split(raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = []
        for item in raw:
            parts.extend(_TAG_SPLIT.split(str(item)))
    else:
        parts = [str(raw)]

    out: List[str] = []
    seen = set()
    for part in parts:
        tag = part.strip().strip(_TAG_JUNK).strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def slugify(name: str) -> str:
    """Slug that survives Chinese names, matching what the Web UI produces."""
    slug = re.sub(r"[^a-zA-Z0-9一-鿿\s-]", "", name or "").strip()
    slug = re.sub(r"\s+", "-", slug).lower()
    return slug or "unnamed"


def _metadata_block_end(lines: list, start: int) -> int:
    """Index just past a metadata block starting at ``start``, or -1 for none.

    Two shapes show up in the wild: a whole file imported as a body (block
    fenced by ``---`` on both sides) and the residue of a half-parsed file
    (``key: value`` lines terminated by a single closing ``---``). Both have to
    look like real metadata — at least two ``key: value`` lines, one of them a
    known key — so a body that legitimately opens with a markdown rule, or a
    note that starts "Note: ...", survives untouched.
    """
    fenced = lines[start].strip() == "---"
    j = start + 1 if fenced else start
    key_lines = 0
    meta_keys = 0

    while j < len(lines) and (fenced or _KEY_LINE.match(lines[j])):
        if fenced and lines[j].strip() == "---":
            break
        if _KEY_LINE.match(lines[j]):
            key_lines += 1
            if lines[j].split(":", 1)[0].strip().lower() in _META_KEYS:
                meta_keys += 1
        j += 1

    if key_lines < 2 or meta_keys < 1 or j >= len(lines):
        return -1
    if fenced or lines[j].strip() == "---":
        return j + 1
    return -1


def strip_stray_frontmatter(text: str) -> str:
    """Remove metadata blocks that leaked into the start of a memory body."""
    if not text:
        return ""

    lines = text.split("\n")
    while True:
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            break
        end = _metadata_block_end(lines, i)
        if end < 0:
            break
        lines = lines[:i] + lines[end:]

    return "\n".join(lines).strip()


def clean_body(text: str) -> str:
    return strip_stray_frontmatter(text or "")


def body_hash(text: str) -> str:
    """Fingerprint of a body, used as the filesystem<->DB change detector.

    Both sides hash the *cleaned* body, so a write-through that is immediately
    followed by a pull is recognised as "nothing changed".
    """
    return hashlib.sha256(clean_body(text or "").encode("utf-8")).hexdigest()[:16]
