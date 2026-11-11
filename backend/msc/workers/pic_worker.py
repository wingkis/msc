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

The vision and embed stages are deliberately paced (caption_batch 1 per 15 s
tick) because they usually share a GPU with the chat model. 能力全开
(caption_burst) drops that pacing on request: both stages then run to a
burst_seconds time budget, embed follows the captions, and the loop only
idles BURST_GAP between passes. See _captions/_embeds.
"""

import time
from concurrent.futures import ThreadPoolExecutor
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
    content_hash, guess_kind, pic_setting, pic_settings, taken_at_from_image,
)

settings = get_settings()

# 全开模式 between passes: long enough to let a failing endpoint back off,
# short enough that the queue never waits on a timer.
BURST_GAP = 3


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


def _caption_call(cfg, thumb_path: str, name: str, rel_path: str) -> str:
    """One vision call, and nothing else: no session, no ORM instance. That is
    what makes it safe to run off the main thread when caption_workers > 1 —
    llm.chat builds its own short-lived httpx client per call."""
    thumb = Path(settings.pic_dir) / (thumb_path or "")
    if not thumb_path or not thumb.is_file():
        raise SourceError("缩略图文件缺失")
    folder = rel_path.rsplit("/", 1)[0] or "/"
    answer = llm.chat(cfg, [
        {"role": "system", "content": _CAPTION_SYSTEM},
        {"role": "user", "content": [
            llm.image_part(thumb.read_bytes(), "image/webp"),
            {"type": "text", "text": f"文件名：{name}（目录：{folder}）"},
        ]},
    ], max_tokens=int(cfg.max_tokens or 600))
    return answer["text"]


def _caption_apply(db, item, text: str, report: dict) -> None:
    """Main thread only: write one answer into the row and commit."""
    caption, tags = _parse_caption(text)
    if not caption:
        _backoff(item, llm.LLMError("模型没有给出描述"))
        db.commit()
        return
    item.caption, item.tags = caption, tags
    item.state, item.last_error = "captioned", None
    item.updated_at = _now().isoformat()
    report["captioned"] = report.get("captioned", 0) + 1
    db.commit()


def _caption_chunk(db, cfg, rows, workers: int, report: dict) -> bool:
    """Caption one chunk. False means the endpoint is unhappy: the stage stops
    for this pass, the failed item waits on its retry_at backoff, and the rest
    of the queue is untouched until the next pass.

    workers == 1 is the historic shape — one call at a time, abandoned at the
    first model error. workers > 1 has that many requests on the wire at once,
    so the answers that did arrive still get written while the failures back
    off. A missing thumbnail is the item's own problem either way."""
    if workers <= 1:
        for item in rows:
            try:
                text = _caption_call(cfg, item.thumb_path, item.name, item.rel_path)
            except SourceError as exc:
                _backoff(item, exc)
                db.commit()
                continue
            except llm.LLMError as exc:
                note_result(db, cfg, error=str(exc))
                _backoff(item, exc)
                db.commit()
                report["errors"].append(f"caption: {exc}")
                return False
            _caption_apply(db, item, text, report)
        return True

    targets = [(item, item.thumb_path, item.name, item.rel_path) for item in rows]
    answers, broken = {}, []
    with ThreadPoolExecutor(max_workers=min(workers, len(targets))) as pool:
        futures = {pool.submit(_caption_call, cfg, path, name, rel_path): item
                   for (item, path, name, rel_path) in targets}
        for future, item in futures.items():
            try:
                answers[item.id] = future.result()
            except (SourceError, llm.LLMError) as exc:
                broken.append((item, exc))
    model_error = False
    for item, exc in broken:
        if isinstance(exc, llm.LLMError):
            note_result(db, cfg, error=str(exc))
            report["errors"].append(f"caption: {exc}")
            model_error = True
        _backoff(item, exc)
        db.commit()
    for item in rows:
        if item.id in answers:
            _caption_apply(db, item, answers[item.id], report)
    return not model_error


def _captions(db, report: dict) -> None:
    """thumb_ready → captioned on the vision card.

    Paced by default: one caption_batch (1) per pass, and the stage stops on
    the first model error — a busy or down multimodal endpoint must not burn
    the tick. That default is deliberate: the vision card usually shares its
    GPU with whatever answers chat.

    Two levers on the 照片源 settings card: caption_paused parks the stage
    outright (scan/thumbs keep flowing, zero vision calls), and caption_burst
    — 能力全开 — removes the pacing instead: keep pulling the queue until
    burst_seconds of model time is used, caption_workers calls at a time.
    """
    tunables = pic_settings(db)
    if tunables["caption_paused"]:
        return
    cfg = get_config(db, "vision")
    if not cfg.enabled or not (cfg.base_url or "").strip() or not (cfg.model or "").strip():
        return  # keep the queue; configuring the card backfills later

    burst = bool(tunables["caption_burst"])
    workers = max(1, tunables["caption_workers"])
    batch = max(1, tunables["caption_batch"])
    if burst:
        batch = max(batch, workers)  # a chunk smaller than the fan-out idles threads
    deadline = time.monotonic() + max(15, tunables["burst_seconds"]) if burst else 0.0
    while True:
        now_iso = _now().isoformat()
        rows = (db.query(PicItem)
                .filter(PicItem.state == "thumb_ready", PicItem.archived == 0,
                        PicItem.missing == 0,
                        or_(PicItem.retry_at.is_(None), PicItem.retry_at <= now_iso))
                .order_by(PicItem.id).limit(batch).all())
        if not rows:
            break
        if not _caption_chunk(db, cfg, rows, workers, report):
            return
        if not burst or time.monotonic() >= deadline:
            break
    note_result(db, cfg)


def _embed_chunk(db, cfg, model_name: str, batch: int, report: dict) -> bool:
    """One embed call (a batch of captions → vectors). False = the embed card
    is unhappy; those rows stay captioned and are retried next pass."""
    rows = (db.query(PicItem).filter(PicItem.state == "captioned", PicItem.archived == 0,
                                     PicItem.missing == 0)
            .order_by(PicItem.id).limit(batch).all())
    if not rows:
        return True

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
        return True

    try:
        vectors = rag_providers.embed(cfg, texts)
    except llm.LLMError as exc:
        note_result(db, cfg, error=str(exc))
        report["errors"].append(f"embed: {exc}")
        return False  # states stay captioned; retried next tick

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
    return True


def _embeds(db, report: dict) -> None:
    """captioned → embedded with the active embed card; PicVector is keyed by
    model exactly like the memory embeddings, and an embed-card switch
    demotes stragglers so the self-heal re-embeds them.

    One embed_batch per pass normally. Under 能力全开 the captions arrive far
    faster than one batch can drain, so this follows them to the same
    burst_seconds budget instead of leaving a captioned backlog behind."""
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

    tunables = pic_settings(db)
    batch = max(1, tunables["embed_batch"])
    burst = bool(tunables["caption_burst"])
    deadline = time.monotonic() + max(15, tunables["burst_seconds"]) if burst else 0.0
    embedded_before = report.get("embedded", 0)
    while True:
        wrote_before = (report.get("embedded", 0), report["failed"])
        keep = _embed_chunk(db, cfg, model_name, batch, report)
        if not keep:
            break  # embed card unhappy: the rest waits for the next pass
        if (report.get("embedded", 0), report["failed"]) == wrote_before:
            break  # queue drained (or everything left is unembeddable)
        if not burst or time.monotonic() >= deadline:
            break
    if report.get("embedded", 0) > embedded_before:
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
        # Tells the loop below it may skip the idle timer; the stages already
        # ran to their own budget, this is only about the gap between passes.
        if pic_setting(db, "caption_burst"):
            report["burst"] = 1
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
    print(f"Photo worker started (pass every {interval}s; {BURST_GAP}s while "
          f"能力全开 has the queue)")
    while True:
        gap = interval
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
                    if result.get("burst"):
                        print(f"PIC: 全开中（{result.get('captioned', 0)} 张描述 / "
                              f"{result.get('embedded', 0)} 张索引）")
                if touched and result.get("burst"):
                    gap = BURST_GAP  # 能力全开: the queue continues, no 15 s pause
        except Exception as exc:
            print(f"Photo worker error: {exc}")
        time.sleep(gap)
