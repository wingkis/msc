"""The one place a database row is mirrored back out to disk.

Push, edit, restore and startup maintenance all have to leave the same three
artefacts behind — the markdown file, the JSON index entry and the sync hash.
Skipping the hash is what made the next pull re-import our own write.
"""

from msc.config import get_settings
from msc.filesystem.indexer import drop_index_entry, update_index_entry
from msc.filesystem.md_reader import file_sync_hash
from msc.filesystem.md_writer import delete_memory_file, write_memory_file
from msc.models import Memory
from msc.schemas import parse_refs


def write_through(db, memory: Memory) -> None:
    """Write a memory to its file + index, then record the resulting hash.

    This is also the single hook that queues the memory for re-embedding:
    every API write, version revert, digest creation and startup repair lands
    here, so one INSERT OR IGNORE covers the whole write path.
    """
    tag_names = [t.name for t in memory.tags]
    write_memory_file(slug=memory.slug, name=memory.name, description=memory.description or "",
                      body=memory.body or "", tags=tag_names, priority=memory.priority,
                      category=memory.category, lifespan=memory.lifespan,
                      branch=memory.branch or "", created=memory.created_at,
                      updated=memory.updated_at, refs=parse_refs(memory.refs),
                      status=memory.status or "", invalidated_reason=memory.invalidated_reason or "")
    update_index_entry(slug=memory.slug, name=memory.name, description=memory.description or "",
                       tags=tag_names, priority=memory.priority, category=memory.category,
                       lifespan=memory.lifespan, created=memory.created_at,
                       updated=memory.updated_at)
    memory.last_sync_hash = file_sync_hash(memory.slug)
    db.commit()

    if get_settings().rag_enabled and not memory.archived and memory.id:
        from msc.services.rag import mark_dirty
        mark_dirty(db, memory.id)


def delete_through(slug: str) -> None:
    """Remove a memory's file and its index entry (soft delete does the same)."""
    delete_memory_file(slug)
    drop_index_entry(slug)
