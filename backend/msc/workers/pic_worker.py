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
from msc.models import PicItem, PicSource, PicVector
from msc.picsources import SourceError, build_source
from msc.services import llm, rag_providers
from msc.services.model_config import get_config, note_result
from msc.services.rag import pack_vec
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
    # Walk-session clock: a fresh walk (no cursor) starts a new one; a
    # resumed walk keeps the original stamp so the GC cutoff spans the whole
    # session even when it stretches across many ticks.
    now_iso = _now().isoformat()
    if src.scan_cursor is None:
        src.scan_started_at = now_iso
    walk_start = src.scan_started_at or now_iso
    db.commit()
    budget = pic_setting(db, "scan_seconds")
    started = time.monotonic()
    walked = inserted = 0
    completed = False
    try:
        for entry in adapter.iter_entries(since=src.scan_cursor):
            if time.monotonic() - started > budget:
                break
            result = db.execute(sql_text(
                "INSERT OR IGNORE INTO pic_items "
                "(source_id, rel_path, origin_uri, name, ext, size_bytes, mtime, kind, "
                " state, archived, missing, error_count, last_seen_at, "
                " discovered_at, updated_at) "
                "VALUES (:p0, :p1, :p2, :p3, :p4, :p5, :p6, :p7, "
                " 'discovered', 0, 0, 0, :p8, :p8, :p8)"),
                {"p0": src.id, "p1": entry.rel_path,
                 "p2": f"pic://{src.id}/{entry.rel_path}", "p3": entry.name,
                 "p4": entry.name.rsplit(".", 1)[-1].lower() if "." in entry.name else "",
                 "p5": entry.size, "p6": entry.mtime_iso,
                 "p7": guess_kind(entry.name), "p8": now_iso})
            # missing/error_count/archived are written explicitly: a raw INSERT
            # bypasses the ORM column defaults, and a NULL missing would drop
            # the photo straight out of the gallery (see ensure_pic_defaults).
            if result.rowcount:
                inserted += 1
            else:
                # Same path, different file now (size or mtime moved): the old
                # thumb, caption and vector describe bytes that no longer
                # exist. Demote the row to discovered so the whole pipeline
                # rebuilds it, and drop the stale vector NOW so semantic search
                # can never answer with the replaced-away content. Runs before
                # the touch below, which would overwrite the compared values.
                swapped = db.execute(sql_text(
                    "UPDATE pic_items SET state = 'discovered', caption = '', "
                    "tags = '', error_count = 0, retry_at = NULL, "
                    "last_error = NULL, updated_at = :p8 "
                    "WHERE source_id = :p0 AND rel_path = :p1 AND state <> 'discovered' "
                    "AND (size_bytes IS NOT :p5 OR mtime IS NOT :p6)"),
                    {"p0": src.id, "p1": entry.rel_path, "p5": entry.size,
                     "p6": entry.mtime_iso, "p8": now_iso})
                if swapped.rowcount:
                    db.execute(sql_text(
                        "DELETE FROM pic_vectors WHERE pic_id = (SELECT id FROM pic_items "
                        "WHERE source_id = :p0 AND rel_path = :p1)"),
                        {"p0": src.id, "p1": entry.rel_path})
                    report["replaced"] = report.get("replaced", 0) + 1
                # Touch (or revive): the original is there RIGHT NOW. Size/mtime
                # refresh too, so replaced files carry fresh metadata even when
                # the row predates them. Only existing rows need this — a fresh
                # insert already carries all of it, which halves the statements
                # on a first walk of a big share.
                db.execute(sql_text(
                    "UPDATE pic_items SET last_seen_at = :p8, missing = 0, "
                    "size_bytes = :p5, mtime = :p6, updated_at = :p8 "
                    "WHERE source_id = :p0 AND rel_path = :p1"),
                    {"p0": src.id, "p1": entry.rel_path, "p5": entry.size,
                     "p6": entry.mtime_iso, "p8": now_iso})
            walked += 1
            src.scan_cursor = entry.rel_path
            if walked % 200 == 0:
                db.commit()
        else:
            completed = True  # loop drained without hitting the budget break
    except Exception as exc:  # SourceError, OSError or an adapter bug
        db.commit()  # keep the cursor progress made so far
        src.state = "error"
        src.last_error = str(exc)[:500]
        db.commit()
        report["errors"].append(f"source {src.id}: {exc}")
        return

    if completed:
        # GC — only after a COMPLETED walk: originals that vanished from the
        # source get missing=1 (hidden, never auto-deleted: the user decides
        # keep / forget / resync in the 缺失 view). An interrupted walk has
        # seen only part of the tree and must not judge the rest.
        gone = db.execute(sql_text(
            "UPDATE pic_items SET missing = 1, updated_at = :p1 WHERE source_id = :p0 "
            "AND archived = 0 AND missing = 0 "
            "AND (last_seen_at IS NULL OR last_seen_at < :p2)"),
            {"p0": src.id, "p1": now_iso, "p2": walk_start}).rowcount
        if gone:
            report["missing"] = report.get("missing", 0) + gone
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
    thumb_px = pic_setting(db, "thumb_px")
    now_iso = _now().isoformat()
    rows = (db.query(PicItem)
            .filter(PicItem.source_id == src.id, PicItem.state == "discovered",
                    PicItem.archived == 0, PicItem.missing == 0,
                    or_(PicItem.retry_at.is_(None), PicItem.retry_at <= now_iso))
            .order_by(PicItem.id).limit(batch).all())
    if not rows:
        return

    from PIL import Image  # ImportError here is a broken image, not a runtime choice

    for item in rows:
        try:
            raw = adapter.get_bytes(item.rel_path)
        except Exception as exc:
            # Any adapter-side failure — hiccup, offline share, even a bug in
            # an adapter — is the ITEM's retry queue problem, not a reason to
            # kill the whole tick. Undecodable CONTENT fails below, per file.
            _backoff(item, exc)
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
                thumb.thumbnail((thumb_px,) * 2)
                rel_thumb = f"thumbs/{src.id}/{item.id}.webp"
                target = Path(settings.pic_dir) / rel_thumb
                target.parent.mkdir(parents=True, exist_ok=True)
                thumb.save(target, "WEBP", quality=80)
            item.thumb_path = rel_thumb
            item.last_error = None
            # Burst shots and re-saved copies: if a sibling already has a
            # caption under the same content hash, inherit it — one vision
            # call saved per duplicate.
            twin = (db.query(PicItem)
                    .filter(PicItem.content_hash == item.content_hash,
                            PicItem.id != item.id, PicItem.caption != "")
                    .first()) if item.content_hash else None
            if twin is not None:
                item.caption, item.tags = twin.caption, twin.tags
                item.state = "captioned"
                report["reused"] = report.get("reused", 0) + 1
            else:
                item.state = "thumb_ready"
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


_CAPTION_SYSTEM = (
    "你是照片库的说明员。仔细看这张照片，最多输出两行：\n"
    "第一行：一句具体的中文描述（画面里的人/物/场景/动作/光线/可见文字）。\n"
    "第二行（可选）：以「标签: 」开头，给 3-6 个检索标签。\n"
    "不要寒暄，不确定的细节不要编造。直接输出这两行。")


def _parse_caption(text: str):
    """First non-empty line is the caption; a 标签:/tags: line becomes tags."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "", ""
    caption = lines[0][:400]
    tags = ""
    for line in lines[1:]:
        low = line.lower()
        for mark in ("标签:", "标签：", "tags:", "tags："):
            if low.startswith(mark):
                tags = line[len(mark):].strip()[:200]
                break
    return caption, tags


def _captions(db, report: dict) -> None:
    """thumb_ready → captioned on the vision card, one photo per call.

    The batch is 1 by default and the whole stage stops on the first model
    error (card noted, retry_at backoff) — a busy or down multimodal endpoint
    must not burn the tick, and the states stay queued for the next one.

    caption_paused (the 暂停图像描述 switch on the 照片源 settings card)
    parks this stage outright: scan and thumbs keep flowing, but zero vision
    calls leave the container while the model is busy elsewhere.
    """
    if pic_setting(db, "caption_paused"):
        return
    cfg = get_config(db, "vision")
    if not cfg.enabled or not (cfg.base_url or "").strip() or not (cfg.model or "").strip():
        return  # keep the queue; configuring the card backfills later

    batch = max(1, pic_setting(db, "caption_batch"))
    now_iso = _now().isoformat()
    rows = (db.query(PicItem)
            .filter(PicItem.state == "thumb_ready", PicItem.archived == 0,
                    PicItem.missing == 0,
                    or_(PicItem.retry_at.is_(None), PicItem.retry_at <= now_iso))
            .order_by(PicItem.id).limit(batch).all())
    for item in rows:
        thumb = Path(settings.pic_dir) / (item.thumb_path or "")
        if not item.thumb_path or not thumb.is_file():
            _backoff(item, SourceError("缩略图文件缺失"))
            db.commit()
            continue
        try:
            answer = llm.chat(cfg, [
                {"role": "system", "content": _CAPTION_SYSTEM},
                {"role": "user", "content": [
                    llm.image_part(thumb.read_bytes(), "image/webp"),
                    {"type": "text", "text": f"文件名：{item.name}（目录：{item.rel_path.rsplit('/', 1)[0] or '/'}）"},
                ]},
            ], max_tokens=int(cfg.max_tokens or 600))
        except llm.LLMError as exc:
            note_result(db, cfg, error=str(exc))
            _backoff(item, exc)
            db.commit()
            report["errors"].append(f"caption: {exc}")
            return  # endpoint unhappy: stop, retry next tick
        caption, tags = _parse_caption(answer["text"])
        if not caption:
            _backoff(item, llm.LLMError("模型没有给出描述"))
            db.commit()
            continue
        item.caption, item.tags = caption, tags
        item.state, item.last_error = "captioned", None
        item.updated_at = _now().isoformat()
        report["captioned"] = report.get("captioned", 0) + 1
        db.commit()
    note_result(db, cfg)


def _embeds(db, report: dict) -> None:
    """captioned → embedded with the active embed card; PicVector is keyed by
    model exactly like the memory embeddings, and an embed-card switch
    demotes stragglers so the self-heal re-embeds them."""
    cfg = get_config(db, "embed")
    if not cfg.enabled or not (cfg.base_url or "").strip() or not (cfg.model or "").strip():
        return
    model_name = cfg.model.strip()

    # Self-heal: embedded rows with no vector for the current model fall back
    # to captioned and get re-embedded below (card switch, lost write).
    db.execute(sql_text(
        "UPDATE pic_items SET state = 'captioned' WHERE state = 'embedded' "
        "AND archived = 0 "
        "AND id NOT IN (SELECT pic_id FROM pic_vectors WHERE model = :p0)"),
        {"p0": model_name})
    db.commit()

    batch = max(1, pic_setting(db, "embed_batch"))
    rows = (db.query(PicItem).filter(PicItem.state == "captioned", PicItem.archived == 0,
                                     PicItem.missing == 0)
            .order_by(PicItem.id).limit(batch).all())
    if not rows:
        return

    texts, pairs = [], []
    for item in rows:
        if (item.caption or "").strip():
            texts.append(item.caption[:1000])
            pairs.append(item)
        else:
            item.state, item.last_error = "failed", "没有可嵌入的描述"
            report["failed"] += 1
    db.commit()
    if not texts:
        return

    try:
        vectors = rag_providers.embed(cfg, texts)
    except llm.LLMError as exc:
        note_result(db, cfg, error=str(exc))
        report["errors"].append(f"embed: {exc}")
        return  # states stay captioned; retried next tick

    now_iso = _now().isoformat()
    for item, vec in zip(pairs, vectors):
        payload = pack_vec(vec)
        row = db.get(PicVector, (item.id, model_name))
        if row is None:
            db.add(PicVector(pic_id=item.id, model=model_name, dim=len(vec),
                             vec=payload, embedded_at=now_iso))
        else:
            row.dim, row.vec, row.embedded_at = len(vec), payload, now_iso
        item.state = "embedded"
        item.updated_at = now_iso
        report["embedded"] = report.get("embedded", 0) + 1
    db.commit()
    note_result(db, cfg)


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
        _captions(db, report)
        _embeds(db, report)
        worked = (report["scanned"] or report["thumbs"] or report["failed"]
                  or report.get("captioned") or report.get("embedded")
                  or report.get("replaced") or report["errors"])
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
                touched = (result["scanned"] + result["thumbs"] + result["failed"]
                           + result.get("captioned", 0) + result.get("embedded", 0)
                           + result.get("reused", 0) + result.get("replaced", 0))
                if touched or result["errors"]:
                    print(f"PIC: discovered {result['scanned']}, thumbs {result['thumbs']}, "
                          f"captioned {result.get('captioned', 0)}, "
                          f"embedded {result.get('embedded', 0)}, "
                          f"replaced {result.get('replaced', 0)}, "
                          f"failed {result['failed']}"
                          + (f", errors: {result['errors'][:2]}" if result["errors"] else ""))
        except Exception as exc:
            print(f"Photo worker error: {exc}")
        time.sleep(interval)
