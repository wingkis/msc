"""API routes: memory version history."""

import json
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from msc.database import get_db
from msc.models import MemoryVersion
from msc.schemas import VersionResponse
from msc.api.memories import (
    _actor, _apply_tags, _guard_write, _resolve_memory, _write_through, next_version_num,
)

router = APIRouter()


def _memory_id(db: Session, slug: str) -> int:
    memory = _resolve_memory(db, slug)
    return memory.id


@router.get("/versions/{slug}", response_model=list[VersionResponse])
@router.get("/{slug}/versions", response_model=list[VersionResponse])
def list_versions(slug: str, limit: int = Query(50, le=200), db: Session = Depends(get_db)):
    """List version history for a memory."""
    memory_id = _memory_id(db, slug)

    return db.query(MemoryVersion).filter(
        MemoryVersion.memory_id == memory_id
    ).order_by(MemoryVersion.version_num.desc()).limit(limit).all()


@router.get("/versions/{slug}/{version_num}", response_model=VersionResponse)
def get_version(slug: str, version_num: int, db: Session = Depends(get_db)):
    """Get a specific version."""
    memory_id = _memory_id(db, slug)
    version = db.query(MemoryVersion).filter(
        MemoryVersion.memory_id == memory_id,
        MemoryVersion.version_num == version_num,
    ).first()

    if not version:
        raise HTTPException(status_code=404, detail=f"Version {version_num} not found")

    return version


@router.post("/versions/{slug}/restore/{version_num}")
def restore_version(slug: str, version_num: int, request: Request, db: Session = Depends(get_db)):
    """Restore a memory to a specific version."""
    memory = _resolve_memory(db, slug)

    version = db.query(MemoryVersion).filter(
        MemoryVersion.memory_id == memory.id,
        MemoryVersion.version_num == version_num,
    ).first()

    if not version:
        raise HTTPException(status_code=404, detail=f"Version {version_num} not found")

    _guard_write()
    actor = _actor(request)

    # Save current state as a version first, so the restore is itself undoable.
    tag_names = [t.name for t in memory.tags]
    db.add(MemoryVersion(
        memory_id=memory.id,
        version_num=next_version_num(db, memory.id),
        name=memory.name,
        description=memory.description,
        body=memory.body,
        priority=memory.priority,
        category=memory.category,
        tags_snapshot=json.dumps(tag_names),
        change_summary=f"Restored to version {version_num}",
        created_at=datetime.now(timezone.utc).isoformat(),
        created_by=actor,
    ))

    snapshot_tags = []
    if version.tags_snapshot:
        try:
            parsed = json.loads(version.tags_snapshot)
            snapshot_tags = parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            snapshot_tags = []

    memory.name = version.name
    memory.description = version.description
    memory.body = version.body
    if version.priority:
        memory.priority = version.priority
    if version.category:
        memory.category = version.category
    if version.tags_snapshot:
        _apply_tags(db, memory, snapshot_tags)
    memory.updated_at = datetime.now(timezone.utc).isoformat()
    db.commit()

    _write_through(db, memory)

    return {"message": f"Restored to version {version_num}", "slug": memory.slug, "by": actor}
