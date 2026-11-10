"""Photo ingest worker — the resumable four-stage pipeline.

scan → thumb → caption → embed. This pass drives scan and thumb; the
caption/embed stages are picked up by the same loop once the vision card
exists. The queue is pic_items.state itself: every stage commits per item
(or per 200-row insert batch while walking), so killing the container
mid-thousand-photo folder loses nothing — the next tick resumes from the
row/cursor state, never from scratch.

Scan uses a per-source time budget instead of a row budget: walking a remote
share is latency-bound, and the cursor makes stopping mid-walk free.
Transient source errors (network hiccup) back off through retry_at; a file
that cannot be decoded is marked failed so the queue can actually drain.
"""

import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from sqlalchemy import or_, text as sql_text

from msc.config import get_settings
from msc.database import SessionLocal
from msc.models import PicItem, PicSource
from msc.picsources import SourceError, build_source
from msc.services.pic_index import (
    content_hash, guess_kind, pic_setting, taken_at_from_image,
)

settings = get_settings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _backoff(item: PicItem, exc: Exception) -> None:
    """Transient source failure: keep the state, retry later with 1-32 min steps."""
    item.error_count = (item.error_count or 0) + 1
    delay = 60 * (2 ** min(item.error_count, 5))
    item.retry_at = (_now() + timedelta(seconds=delay)).isoformat()
    item.last_error = str(exc)[:300]
    item.updated_at = _now().isoformat()


def _scan(db, src: PicSource, adapter, report: dict) -> None:
    """Walk one source, inserting placeholders until the time budget runs out."""
    if src.state == "paused":
        return
    rescan_cutoff = (_now() - timedelta(hours=pic_setting(db, "rescan_hours"))).isoformat()
    due = src.scan_cursor is not None or not src.last_scan_at or src.last_scan_at < rescan_cutoff
    if not due:
        return

    src.state = "scanning"
    db.commit()
    budget = pic_setting(db, "scan_seconds")
    started = time.monotonic()
    now_iso = _now().isoformat()
    walked = inserted = 0
    completed = False
    try:
        for entry in adapter.iter_entries(since=src.scan_cursor):
            if time.monotonic() - started > budget:
                break
            result = db.execute(sql_text(
                "INSERT OR IGNORE INTO pic_items "
                "(source_id, rel_path, origin_uri, name, ext, size_bytes, mtime, kind, "
                " state, archived, discovered_at, updated_at) "
                "VALUES (:p0, :p1, :p2, :p3, :p4, :p5, :p6, :p7, 'discovered', 0, :p8, :p8)"),
                {"p0": src.id, "p1": entry.rel_path,
                 "p2": f"pic://{src.id}/{entry.rel_path}", "p3": entry.name,
                 "p4": entry.name.rsplit(".", 1)[-1].lower() if "." in entry.name else "",
                 "p5": entry.size, "p6": entry.mtime_iso,
                 "p7": guess_kind(entry.name), "p8": now_iso})
            inserted += result.rowcount
            walked += 1
            src.scan_cursor = entry.rel_path
            if walked % 200 == 0:
                db.commit()
        else:
            completed = True  # loop drained without hitting the budget break
    except (SourceError, OSError) as exc:
        db.commit()  # keep the cursor progress made so far
        src.state = "error"
        src.last_error = str(exc)[:500]
        db.commit()
        report["errors"].append(f"source {src.id}: {exc}")
        return

    if completed:
        src.scan_cursor = None
        src.last_scan_at = now_iso
        src.state = "idle"
        src.last_error = None
    db.commit()
    report["scanned"] += inserted


def _thumbs(db, src: PicSource, adapter, report: dict) -> None:
    """discovered → thumb_ready: one thumbnail, one commit, one visible tile."""
    if src.state in ("paused", "error") or not src.enabled:
        return
    batch = pic_setting(db, "thumb_batch")
    now_iso = _now().isoformat()
    rows = (db.query(PicItem)
            .filter(PicItem.source_id == src.id, PicItem.state == "discovered",
                    PicItem.archived == 0,
                    or_(PicItem.retry_at.is_(None), PicItem.retry_at <= now_iso))
            .order_by(PicItem.id).limit(batch).all())
    if not rows:
        return

    from PIL import Image  # ImportError here is a broken image, not a runtime choice

    for item in rows:
        try:
            raw = adapter.get_bytes(item.rel_path)
        except (SourceError, OSError) as exc:
            _backoff(item, exc)  # source hiccup: this photo is not broken, the wire is
            db.commit()
            continue
        try:
            with Image.open(BytesIO(raw)) as im:
                item.width, item.height = im.size
                item.taken_at, item.taken_at_src = taken_at_from_image(im, item.mtime)
                item.content_hash = content_hash(len(raw), raw[:1 << 20])
                thumb = im
                if im.mode not in ("RGB", "RGBA", "L", "P"):
                    thumb = im.convert("RGB")
                thumb.thumbnail((pic_setting(db, "thumb_px"),) * 2)
                rel_thumb = f"thumbs/{src.id}/{item.id}.webp"
                target = Path(settings.pic_dir) / rel_thumb
                target.parent.mkdir(parents=True, exist_ok=True)
                thumb.save(target, "WEBP", quality=80)
            item.thumb_path = rel_thumb
            item.state = "thumb_ready"
            item.last_error = None
            item.updated_at = _now().isoformat()
            report["thumbs"] += 1
        except Exception as exc:
            # Undecodable file (corrupt, or HEIC on a host without pillow-heif):
            # permanent for this item, the queue must be able to drain.
            item.state = "failed"
            item.error_count = (item.error_count or 0) + 1
            item.last_error = str(exc)[:300]
            item.updated_at = _now().isoformat()
            report["failed"] += 1
        db.commit()  # one tile lands while the user watches, before the next read


def run_once() -> dict | None:
    """One pass over every enabled source. Returns a report, or None if idle."""
    if settings.read_only or not settings.pic_enabled:
        return None

    db = SessionLocal()
    report = {"scanned": 0, "thumbs": 0, "failed": 0, "errors": []}
    try:
        for src in db.query(PicSource).filter(PicSource.enabled == 1).all():
            try:
                adapter = build_source(src)
            except SourceError as exc:
                src.state = "error"
                src.last_error = str(exc)[:500]
                db.commit()
                report["errors"].append(f"source {src.id}: {exc}")
                continue
            try:
                _scan(db, src, adapter, report)
                _thumbs(db, src, adapter, report)
            finally:
                adapter.close()
        worked = report["scanned"] or report["thumbs"] or report["failed"] or report["errors"]
        return report if worked else None
    except Exception as exc:  # the loop must survive anything
        db.rollback()
        print(f"PIC worker error: {exc}")
        return {"error": str(exc)}
    finally:
        db.close()


def start_pic_worker(interval: int = 15) -> None:
    print(f"Photo worker started (scan/thumb pass every {interval}s)")
    while True:
        try:
            result = run_once()
            if result and not result.get("error"):
                touched = result["scanned"] + result["thumbs"] + result["failed"]
                if touched or result["errors"]:
                    print(f"PIC: discovered {result['scanned']}, thumbs {result['thumbs']}, "
                          f"failed {result['failed']}"
                          + (f", errors: {result['errors'][:2]}" if result["errors"] else ""))
        except Exception as exc:
            print(f"Photo worker error: {exc}")
        time.sleep(interval)
