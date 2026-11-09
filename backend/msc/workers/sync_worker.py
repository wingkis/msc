"""Background sync worker — picks up changes agents make directly on disk.

Dropping or editing a markdown file in the memory directory is the simplest
agent workflow there is, so the file side has to be a first-class writer:
name, body, tags and metadata all flow back into the DB, and a brand new file
becomes a memory instead of being ignored.
"""

import time
from datetime import datetime, timezone

from msc.config import get_settings
from msc.database import SessionLocal
from msc.filesystem.md_reader import list_memory_files, read_memory_file
from msc.models import Memory, Tag
from msc.text import body_hash, clean_body, normalize_tags

settings = get_settings()

CATEGORIES = {"config", "project", "user", "knowledge", "reference"}
PRIORITIES = {"low", "medium", "high", "critical"}
LIFESPANS = {"temporary", "permanent"}


def _safe(value: str, allowed: set, default: str) -> str:
    """A hand-written file with ``priority: urgent`` must not kill the cycle —
    the SQLite CHECK constraint would raise and abort the whole batch."""
    return value if value in allowed else default


def _apply_tags(db, memory: Memory, raw_tags) -> None:
    memory.tags = []
    db.flush()
    for name in normalize_tags(raw_tags):
        tag = db.query(Tag).filter(Tag.name == name).first()
        if not tag:
            tag = Tag(name=name)
            db.add(tag)
            db.flush()
        memory.tags.append(tag)


def _from_file(db, slug: str, data: dict, memory: Memory = None) -> Memory:
    """Copy parsed file contents onto a memory (creating one if needed)."""
    body = clean_body(data.get("body", ""))
    now = datetime.now(timezone.utc).isoformat()

    if memory is None:
        # Every NOT NULL column has to be set before the row is inserted: an
        # early flush used to write priority/category as NULL and abort the pull.
        memory = Memory(slug=slug, name=data.get("name") or slug, created_at=now, updated_at=now,
                        priority="medium", category="reference", lifespan="permanent",
                        source="claude")
        db.add(memory)

    memory.name = data.get("name") or slug
    memory.description = data.get("description", "") or ""
    memory.body = body
    memory.priority = _safe(data.get("priority", "medium"), PRIORITIES, memory.priority or "medium")
    memory.category = _safe(data.get("category", "reference"), CATEGORIES, memory.category or "reference")
    memory.lifespan = _safe(data.get("lifespan", "permanent"), LIFESPANS, memory.lifespan or "permanent")
    memory.branch = data.get("branch", "") or ""
    memory.last_sync_hash = body_hash(body)
    memory.updated_at = data.get("updated") or now
    _apply_tags(db, memory, data.get("tags", []))
    return memory


def run_sync_cycle() -> dict:
    """One filesystem -> DB pass. Returns what it found."""
    db = SessionLocal()
    created, updated, skipped = [], [], []
    touched_ids: list[int] = []
    try:
        for slug in list_memory_files():
            data = read_memory_file(slug)
            if not data:
                continue

            body = clean_body(data.get("body", ""))
            digest = body_hash(body)
            memory = db.query(Memory).filter(Memory.slug == slug).first()

            if memory is None:
                memory = _from_file(db, slug, data)
                created.append(slug)
                db.flush()
                touched_ids.append(memory.id)
                continue

            if digest == memory.last_sync_hash:
                continue

            file_updated = data.get("updated", "")
            if file_updated and memory.updated_at and file_updated < memory.updated_at:
                # DB is newer than the file; the next API write rewrites the file.
                skipped.append(slug)
                continue

            _from_file(db, slug, data, memory)
            updated.append(slug)
            db.flush()
            touched_ids.append(memory.id)

        db.commit()

        # File-side writes bypass the API's write_through hook, so the pull
        # queues its own re-embeddings.
        if settings.rag_enabled and touched_ids:
            from msc.services.rag import mark_dirty
            for memory_id in touched_ids:
                mark_dirty(db, memory_id)

        return {"created": created, "updated": updated, "skipped_newer_db": skipped}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def start_sync_worker(interval: int = None) -> None:
    if interval is None:
        interval = settings.sync_interval_seconds

    print(f"Sync worker started (interval={interval}s)")
    while True:
        try:
            result = run_sync_cycle()
            if result["created"] or result["updated"]:
                print(f"Sync: +{len(result['created'])} ~{len(result['updated'])} "
                      f"{result['created'] + result['updated']}")
        except Exception as e:
            print(f"Sync worker error: {e}")
        time.sleep(interval)
