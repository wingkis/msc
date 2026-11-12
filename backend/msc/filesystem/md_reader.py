"""Read markdown files from filesystem."""

from pathlib import Path
from typing import List, Optional

from msc.config import get_settings
from msc.schemas import REF_KINDS
from msc.text import body_hash, normalize_tags, strip_stray_frontmatter

settings = get_settings()


def parse_frontmatter(file_content: str) -> tuple[dict, str]:
    """Parse the leading YAML-like frontmatter block of a markdown file.

    Returns (frontmatter_dict, body_string). Only the first block is metadata;
    any further block that leaked into the body is stripped from it.
    """
    lines = file_content.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, strip_stray_frontmatter(file_content)

    fm_lines: list[str] = []
    body_start = len(lines)  # an unterminated block has no body
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body_start = i + 1
            break
        fm_lines.append(lines[i])

    frontmatter = {}
    for line in fm_lines:
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        # md_writer quotes values that contain a colon; undo that here.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        frontmatter[key.strip().lower()] = value

    body = strip_stray_frontmatter("\n".join(lines[body_start:]))
    return frontmatter, body


def parse_ref_list(value) -> List[str]:
    """Parse a frontmatter `refs:` value, dropping anything malformed.

    Strict parse would fail a whole file import over one mistyped prefix, and a
    human editing markdown is exactly who that punishes. The API keeps its 422
    for the same mistake: a machine writing a bad kind should be told, a person
    editing a file should keep their memory.
    """
    if isinstance(value, (list, tuple)):
        candidates = [str(v) for v in value]
    else:
        candidates = str(value or "").split(",")
    out: List[str] = []
    for raw in candidates:
        item = raw.strip().strip('"\\\'')
        if not item or ":" not in item:
            continue
        kind, _, target = item.partition(":")
        kind = kind.strip().lower()
        target = target.strip()
        if kind in REF_KINDS and target and f"{kind}:{target}" not in out:
            out.append(f"{kind}:{target}")
    return out


def read_memory_file(slug: str, memory_dir: Path = None) -> Optional[dict]:
    """Read a memory file and return parsed data.

    Returns dict with: slug, name, description, body, tags, priority, category, lifespan, branch, created, updated
    Returns None if file not found.
    """
    if memory_dir is None:
        memory_dir = settings.memory_dir

    file_path = memory_dir / f"{slug}.md"
    if not file_path.exists():
        return None

    content = file_path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(content)

    if not frontmatter:
        return None

    # Tags may be a plain list, a comma separated string, or str(['a, b'])
    tags = normalize_tags(frontmatter.get("tags", ""))
    # Absent and empty are different statements: a file written before refs
    # existed must not clear the provenance already in the database, while
    # `refs:` with nothing after it means "this memory has none".
    raw_refs = frontmatter.get("refs")

    return {
        "slug": slug,
        "name": frontmatter.get("name", slug),
        "description": frontmatter.get("description", ""),
        "body": body.strip(),
        "tags": tags,
        "priority": frontmatter.get("priority", "medium"),
        "category": frontmatter.get("category", "reference"),
        "lifespan": frontmatter.get("lifespan", "permanent"),
        "branch": frontmatter.get("branch", ""),
        "created": frontmatter.get("created", ""),
        "updated": frontmatter.get("updated", ""),
        "refs": None if raw_refs is None else parse_ref_list(raw_refs),
        # Read-only in this direction; carried so a human reading the file (or
        # a diff) can see the memory was contradicted.
        "status": frontmatter.get("status", ""),
        "invalidated_reason": frontmatter.get("invalidated_reason", ""),
        "file_path": str(file_path),
    }


def list_memory_files(memory_dir: Path = None) -> List[str]:
    """List all memory file slugs."""
    if memory_dir is None:
        memory_dir = settings.memory_dir

    # Compare against file *names*: MEMORY.md has stem "MEMORY", so matching the
    # stem against "MEMORY.md" never skipped it and the index file showed up as
    # drift in GET /api/v1/sync/status.
    skip = {"MEMORY.md"}
    return [
        f.stem for f in sorted(memory_dir.glob("*.md"))
        if f.name not in skip and not f.name.startswith(".")
    ]


def sync_digest(data: dict) -> str:
    """The fingerprint of everything the import would copy out of a file.

    Hashing the body alone was not enough: refs, tags, priority and category
    arrive from the frontmatter, so a hand-edited `refs:` line left the digest
    identical and the pull pass never looked at the file again. Provenance
    edited by hand is the whole point of the file channel.
    """
    refs = data.get("refs")
    parts = [data.get("body", "") or "", data.get("priority", "") or "",
             data.get("category", "") or "", data.get("lifespan", "") or "",
             data.get("branch", "") or "", ",".join(data.get("tags") or []),
             "<unread>" if refs is None else ",".join(refs)]
    return body_hash("\x1f".join(parts))


def file_sync_hash(slug: str, memory_dir: Path = None) -> str:
    """What the pull pass would record for this file ('' when unreadable)."""
    data = read_memory_file(slug, memory_dir)
    return sync_digest(data) if data else ""
