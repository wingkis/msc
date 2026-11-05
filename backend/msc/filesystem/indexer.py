"""Update JSON index file."""

import json
from pathlib import Path

from msc.config import get_settings
from msc.filesystem.md_reader import read_memory_file

settings = get_settings()


def build_index(memory_dir: Path = None, index_path: Path = None) -> dict:
    """Rebuild the JSON index from all memory files."""
    if memory_dir is None:
        memory_dir = settings.memory_dir
    if index_path is None:
        index_path = memory_dir / ".msc_index.json"

    memories = {}
    for md_file in memory_dir.glob("*.md"):
        if md_file.name in ("MEMORY.md", ".msc_index.json"):
            continue
        slug = md_file.stem
        data = read_memory_file(slug, memory_dir)
        if data:
            memories[slug] = {
                "name": data["name"],
                "description": data["description"],
                "tags": ", ".join(data["tags"]) if data.get("tags") else "",
                "priority": data.get("priority", "medium"),
                "category": data.get("category", "reference"),
                "lifespan": data.get("lifespan", "permanent"),
                "created": data.get("created", ""),
                "updated": data.get("updated", ""),
            }

    result = {"memories": memories}
    index_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def drop_index_entry(slug: str, memory_dir: Path = None, index_path: Path = None) -> bool:
    """Remove one entry so the index cannot outlive its file."""
    if memory_dir is None:
        memory_dir = settings.memory_dir
    if index_path is None:
        index_path = memory_dir / ".msc_index.json"
    if not index_path.exists():
        return False

    data = json.loads(index_path.read_text(encoding="utf-8"))
    memories = data.get("memories", {})
    if slug not in memories:
        return False

    memories.pop(slug)
    data["memories"] = memories
    index_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def update_index_entry(slug: str, name: str, description: str, tags: list[str],
                       priority: str, category: str, lifespan: str,
                       created: str, updated: str,
                       memory_dir: Path = None, index_path: Path = None) -> None:
    """Update a single entry in the JSON index."""
    if memory_dir is None:
        memory_dir = settings.memory_dir
    if index_path is None:
        index_path = memory_dir / ".msc_index.json"

    if not index_path.exists():
        build_index(memory_dir, index_path)
        return

    data = json.loads(index_path.read_text(encoding="utf-8"))
    data["memories"][slug] = {
        "name": name,
        "description": description,
        "tags": ", ".join(tags) if tags else "",
        "priority": priority,
        "category": category,
        "lifespan": lifespan,
        "created": created,
        "updated": updated,
    }

    index_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
