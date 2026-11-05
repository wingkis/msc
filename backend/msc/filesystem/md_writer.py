"""Write markdown files from DB state."""

from pathlib import Path
from typing import List

from msc.config import get_settings
from msc.text import clean_body, normalize_tags

settings = get_settings()


def _escape_frontmatter_value(value: str) -> str:
    """Escape a value for YAML frontmatter."""
    if ":" in value or value.startswith(("'", '"', "{", "[")):
        return f'"{value.replace(chr(34), chr(92) + chr(34))}"'
    return value


def write_memory_file(slug: str, name: str, description: str, body: str,
                      tags: List[str] = None, priority: str = "medium",
                      category: str = "reference", lifespan: str = "permanent",
                      branch: str = "", created: str = None, updated: str = None,
                      memory_dir: Path = None) -> Path:
    """Write a memory as a markdown file with frontmatter.

    Returns the path to the written file.
    """
    if memory_dir is None:
        memory_dir = settings.memory_dir

    memory_dir.mkdir(parents=True, exist_ok=True)

    # Build frontmatter
    lines = ["---"]
    lines.append(f"name: {name}")
    if description:
        lines.append(f"description: {_escape_frontmatter_value(description)}")
    clean_tags = normalize_tags(tags)
    if clean_tags:
        lines.append(f"tags: {', '.join(clean_tags)}")
    lines.append(f"priority: {priority}")
    lines.append(f"category: {category}")
    lines.append(f"lifespan: {lifespan}")
    if branch:
        lines.append(f"branch: {branch}")
    if created:
        lines.append(f"created: {created}")
    if updated:
        lines.append(f"updated: {updated}")
    lines.append("---")

    # A body that already contains frontmatter would nest another block on
    # every write, so sanitize it before it goes back out to disk.
    clean = clean_body(body or "")
    content = "\n".join(lines) + (f"\n{clean}\n" if clean else "\n")

    file_path = memory_dir / f"{slug}.md"
    file_path.write_text(content, encoding="utf-8")
    return file_path


def update_memory_file(slug: str, **kwargs) -> Path:
    """Update an existing memory file (read, modify frontmatter fields, write back)."""
    from msc.filesystem.md_reader import read_memory_file

    file_data = read_memory_file(slug)
    if not file_data:
        # File doesn't exist, create new
        return write_memory_file(slug, **kwargs)

    # Update fields
    for key, value in kwargs.items():
        if value is not None:
            file_data[key] = value

    return write_memory_file(
        slug=file_data["slug"],
        name=file_data["name"],
        description=file_data.get("description", ""),
        body=file_data.get("body", ""),
        tags=file_data.get("tags", []),
        priority=file_data.get("priority", "medium"),
        category=file_data.get("category", "reference"),
        lifespan=file_data.get("lifespan", "permanent"),
        branch=file_data.get("branch", ""),
        created=file_data.get("created", ""),
        updated=file_data.get("updated", ""),
    )


def delete_memory_file(slug: str, memory_dir: Path = None) -> bool:
    """Delete a memory markdown file."""
    if memory_dir is None:
        memory_dir = settings.memory_dir

    file_path = memory_dir / f"{slug}.md"
    if file_path.exists():
        file_path.unlink()
        return True
    return False
