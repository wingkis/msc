"""The one place a database row is mirrored back out to disk.

Push, edit, restore and startup maintenance all have to leave the same three
artefacts behind — the markdown file, the JSON index entry and the sync hash.
Skipping the hash is what made the next pull re-import our own write.
"""

from msc.filesystem.indexer import drop_index_entry, update_index_entry
from msc.filesystem.md_reader import file_sync_hash
from msc.filesystem.md_writer import delete_memory_file, write_memory_file
from msc.models import Memory


def write_through(db, memory: Memory) -> None:
    """Write a memory to its file + index, then record the resulting hash."""
    tag_names = [t.name for t in memory.tags]
    write_memory_file(slug=memory.slug, name=memory.name, description=memory.description or "",
                      body=memory.body or "", tags=tag_names, priority=memory.priority,
                      category=memory.category, lifespan=memory.lifespan,
                      branch=memory.branch or "", created=memory.created_at,
                      updated=memory.updated_at)
    update_index_entry(slug=memory.slug, name=memory.name, description=memory.description or "",
                       tags=tag_names, priority=memory.priority, category=memory.category,
                       lifespan=memory.lifespan, created=memory.created_at,
                       updated=memory.updated_at)
    memory.last_sync_hash = file_sync_hash(memory.slug)
    db.commit()


def delete_through(slug: str) -> None:
    """Remove a memory's file and its index entry (soft delete does the same)."""
    delete_memory_file(slug)
    drop_index_entry(slug)
