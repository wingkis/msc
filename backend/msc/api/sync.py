"""API routes: sync (push, pull, conflicts, logs)."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from msc.config import get_settings
from msc.database import get_db
from msc.models import Memory, SyncLog, SyncConflict
from msc.filesystem.md_reader import (
    file_sync_hash, list_memory_files,
)
from msc.filesystem.md_writer import write_memory_file
from msc.schemas import parse_refs
from msc.schemas import SyncConflictResolve

settings = get_settings()
router = APIRouter()


def _log_sync(db: Session, memory_id: Optional[int], api_key_prefix: Optional[str],
              direction: str, status: str, detail: str, remote_ip: Optional[str]):
    """Log a sync operation."""
    log = SyncLog(
        memory_id=memory_id,
        api_key_prefix=api_key_prefix,
        direction=direction,
        status=status,
        detail=detail,
        remote_ip=remote_ip,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(log)
    db.commit()


@router.post("/push")
def sync_push(request: Request, db: Session = Depends(get_db)):
    """Push DB state to filesystem. Writes all memories as markdown files."""
    memories = db.query(Memory).filter(Memory.archived == 0).all()
    written = 0
    errors: list[str] = []

    for mem in memories:
        tag_names = [t.name for t in mem.tags]
        try:
            write_memory_file(
                slug=mem.slug, name=mem.name, description=mem.description or "",
                body=mem.body or "", tags=tag_names, priority=mem.priority,
                category=mem.category, lifespan=mem.lifespan,
                branch=mem.branch or "", created=mem.created_at, updated=mem.updated_at,
                refs=parse_refs(mem.refs), status=mem.status or "",
                invalidated_reason=mem.invalidated_reason or "",
            )
            mem.last_sync_hash = file_sync_hash(mem.slug)
            written += 1
        except Exception as e:
            errors.append(f"{mem.slug}: {e}")

    db.commit()

    _log_sync(db, None, None, "push", "ok" if errors == 0 else "error",
              f"Wrote {written}/{len(memories)}" + (f"; failed: {'; '.join(errors[:5])}" if errors else ""),
              None)

    return {"message": f"Pushed {written} memories ({len(errors)} errors)",
            "written": written, "errors": errors[:20]}


@router.post("/pull")
def sync_pull(request: Request, db: Session = Depends(get_db)):
    """Pull filesystem changes into the DB.

    Same code path as the background worker, so a manual pull can't disagree
    with what the automatic sync would have done.
    """
    from msc.workers.sync_worker import run_sync_cycle

    result = run_sync_cycle()
    created, updated, skipped = result["created"], result["updated"], result["skipped_newer_db"]

    _log_sync(db, None, None, "pull", "ok",
              f"Created {len(created)}, Updated {len(updated)}, Skipped {len(skipped)}", None)
    return {
        "message": f"Pulled: {len(created)} created, {len(updated)} updated, {len(skipped)} skipped",
        "created": created, "updated": updated, "skipped_newer_db": skipped,
    }


@router.get("/status")
def sync_status(db: Session = Depends(get_db)):
    """Where DB and filesystem stand right now — what a sync would change."""
    from msc.fts import fts_desync_report

    # Compare against every row, archived included: a soft-deleted memory keeps
    # its row, so it is not "a file the database is missing".
    all_slugs = {row[0] for row in db.query(Memory.slug).all()}
    live_slugs = {row[0] for row in db.query(Memory.slug).filter(Memory.archived == 0).all()}
    files = list_memory_files()
    file_slugs = set(files)
    unindexed = fts_desync_report()
    last = db.query(SyncLog).order_by(SyncLog.created_at.desc()).first()

    return {
        "memory_dir": str(settings.memory_dir),
        "memory_dir_exists": settings.memory_dir.exists(),
        "db_memories": len(live_slugs),
        "db_archived": len(all_slugs) - len(live_slugs),
        "files": len(file_slugs),
        "only_in_db": sorted(live_slugs - file_slugs)[:20],
        "only_in_files": sorted(file_slugs - all_slugs)[:20],
        "search_index_unindexed": unindexed["missing"],
        "sync_interval_seconds": settings.sync_interval_seconds,
        "last_sync": {"direction": last.direction, "status": last.status, "detail": last.detail,
                      "created_at": last.created_at} if last else None,
    }


@router.post("/reindex")
def reindex(request: Request, db: Session = Depends(get_db)):
    """Rebuild the FTS index and the JSON file index from scratch."""
    from msc.fts import rebuild_fts
    from msc.filesystem.indexer import build_index

    indexed = rebuild_fts()
    index = build_index()
    _log_sync(db, None, None, "sync", "ok", f"Reindexed {indexed} rows, {len(index['memories'])} files", None)
    return {"message": f"Reindexed {indexed} memories", "files": len(index["memories"])}


@router.get("/conflicts")
def list_conflicts(db: Session = Depends(get_db)):
    """List unresolved sync conflicts."""
    conflicts = db.query(SyncConflict).filter(SyncConflict.resolved == 0).order_by(
        SyncConflict.created_at.desc()
    ).all()
    return [{"id": c.id, "slug": c.slug, "source": c.source, "reason": c.reason,
             "created_at": c.created_at} for c in conflicts]


@router.post("/conflicts/{slug}/resolve")
def resolve_conflict(slug: str, data: SyncConflictResolve, db: Session = Depends(get_db)):
    """Resolve a sync conflict."""
    conflict = db.query(SyncConflict).filter(
        SyncConflict.slug == slug,
        SyncConflict.resolved == 0,
    ).first()

    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")

    conflict.resolved = 1
    conflict.resolved_at = datetime.now(timezone.utc).isoformat()
    conflict.resolved_by = "admin"
    db.commit()

    return {"message": f"Conflict resolved: source={data.source}"}


@router.get("/logs")
def list_sync_logs(
    limit: int = Query(50),
    direction: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """List sync logs."""
    query = db.query(SyncLog).order_by(SyncLog.created_at.desc())
    if direction:
        query = query.filter(SyncLog.direction == direction)
    logs = query.limit(limit).all()
    return [{"id": l.id, "memory_id": l.memory_id, "api_key_prefix": l.api_key_prefix,
             "direction": l.direction, "status": l.status, "detail": l.detail,
             "remote_ip": l.remote_ip, "created_at": l.created_at}
            for l in logs]
