"""API routes: photo sources, gallery browsing, thumbnails, pic tuning.

Web-UI-facing like the RAG routes — deliberately absent from /help and
/llms.txt. Everything honours MSC_PIC_ENABLED (the kill switch),
MSC_READ_ONLY and the usual auth guards.

Stage one (msc-pic MVP): local sources, placeholder rows, thumbnail
generation, gallery browsing. Captioning, semantic search, remote sources
and the trash bin land on later passes of the same branch.
"""

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import and_, or_, text as sql_text
from sqlalchemy import true as sql_true
from sqlalchemy.orm import Session

from msc.api.memories import _actor, _guard_write, _require_auth
from msc.config import get_settings
from msc.database import get_db
from msc.models import PicItem, PicSource, PicVector
from msc.picsources import SourceError, build_source
from msc.schemas import (
    PicItemsBatch, PicItemsPurge, PicSettingsUpdate, PicSourceCreate, PicSourceUpdate,
)
from msc.services.model_config import get_config, public_view
from msc.services.pic_index import (
    PIC_TUNABLES, lifecycle_counts, pic_semantic_ids, pic_settings,
    set_pic_setting, source_counts, source_counts_all, state_counts,
)
from msc.services.rag import rrf_fusion, setting as rag_setting

_log = logging.getLogger("msc.pic")
router = APIRouter()
settings = get_settings()

PIC_STATES = ("discovered", "thumb_ready", "captioned", "embedded", "failed")


def _require_pic() -> None:
    if not settings.pic_enabled:
        raise HTTPException(status_code=503,
                            detail="照片功能已关闭（MSC_PIC_ENABLED=false），其余功能不受影响")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pic_roots() -> list:
    return [r.strip() for r in (settings.pic_roots or "").split(",") if r.strip()]


def _under_roots(real: str) -> bool:
    for root in pic_roots():
        root_real = os.path.realpath(root)
        if real == root_real or real.startswith(root_real + os.sep):
            return True
    return False


def _validate_source(kind: str, root_path: str, host: str, share: str, url: str) -> None:
    """Reject cards the worker could never serve, with a message that names
    the field to fix."""
    if kind == "local":
        if not root_path.strip():
            raise HTTPException(status_code=422, detail="本地源必须填目录路径")
        if not root_path.strip().startswith("/"):
            raise HTTPException(status_code=422, detail="目录路径必须是绝对路径（服务器视角）")
        real = os.path.realpath(root_path.strip())
        if not _under_roots(real):
            raise HTTPException(status_code=422, detail=(
                f"{root_path} 不在允许的目录根之内；可在 MSC_PIC_ROOTS 增加根目录"
                f"（当前：{', '.join(pic_roots()) or '无'}），并在 docker-compose.yml 挂载同名目录"))
    elif kind == "smb":
        if not host.strip() or not share.strip():
            raise HTTPException(status_code=422, detail="SMB 源必须填服务器地址和共享名")
    elif kind == "webdav":
        if "://" not in url.strip():
            raise HTTPException(status_code=422, detail="WebDAV 源必须填完整 URL（含 http:// 或 https://）")


def _source_view(row: PicSource, counts: dict | None = None) -> dict:
    """Card as the UI sees it — the password is never sent back in full."""
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "root_path": row.root_path or "",
        "host": row.host or "",
        "share": row.share or "",
        "subpath": row.subpath or "",
        "url": row.url or "",
        "username": row.username or "",
        "password_set": bool(row.password),
        "enabled": bool(row.enabled),
        "state": row.state or "idle",
        "resuming": bool(row.scan_cursor),
        "last_scan_at": row.last_scan_at,
        "last_error": row.last_error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "counts": counts or {},
    }


def _get_source(db: Session, source_id: int) -> PicSource:
    row = db.get(PicSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"照片源 {source_id} 不存在")
    return row


def _item_view(item: PicItem, source_name: str = "", dup: bool = False) -> dict:
    # The tile URL carries the hash its bytes were rendered from: new content
    # (a re-scanned replaced file, or a row that reappeared after the old one
    # was purged) gets a new URL, so no 24 h browser cache can ever pair an
    # old thumbnail picture with a new row's caption.
    thumb_url = None
    if item.thumb_path:
        thumb_url = f"/api/v1/pic/item/{item.id}/thumb"
        if item.content_hash:
            thumb_url += f"?v={item.content_hash}"
    return {
        "id": item.id,
        "source_id": item.source_id,
        "source_name": source_name,
        "rel_path": item.rel_path,
        "name": item.name,
        "ext": item.ext,
        "kind": item.kind,
        "state": item.state,
        "taken_at": item.taken_at,
        "taken_at_src": item.taken_at_src,
        "mtime": item.mtime,
        "size_bytes": item.size_bytes,
        "width": item.width,
        "height": item.height,
        "caption": (item.caption or "")[:200],
        "tags": item.tags or "",
        "thumb_available": bool(item.thumb_path),
        "thumb_url": thumb_url,
        "content_hash": item.content_hash,
        "dup": dup,
        "origin_uri": item.origin_uri,
        "archived": bool(item.archived),
        "error_count": item.error_count,
        "last_error": item.last_error,
        "discovered_at": item.discovered_at,
    }


# ── Status & tuning ──────────────────────────────────────────────────

@router.get("/status")
def pic_status(db: Session = Depends(get_db)):
    """Kill switch, pipeline depth, per-state counts, effective tuning."""
    _require_pic()
    counts = state_counts(db)
    lifecycle = lifecycle_counts(db)
    per_source = source_counts_all(db)
    tunables = pic_settings(db)
    return {
        "enabled": settings.pic_enabled,
        "roots": pic_roots(),
        "thumb_dir": str(Path(settings.pic_dir) / "thumbs"),
        "counts": counts,
        "pending_thumbs": counts.get("discovered", 0),
        "pending_captions": counts.get("thumb_ready", 0),
        "failed": counts.get("failed", 0),
        "missing": lifecycle["missing"],
        "trashed": lifecycle["trashed"],
        "providers": {purpose: public_view(get_config(db, purpose))
                      for purpose in ("vision", "embed")},
        "sources": [_source_view(row, per_source.get(row.id, {}))
                    for row in db.query(PicSource).order_by(PicSource.id).all()],
        "settings": {key: tunables[key] for key in PIC_TUNABLES},
    }


@router.put("/settings")
def update_settings(data: PicSettingsUpdate, request: Request, db: Session = Depends(get_db)):
    """Tune the pipeline without a rebuild (DB wins over .env)."""
    _require_auth(request, db)
    _guard_write()
    _require_pic()
    patch = data.model_dump(exclude_unset=True)
    for key in PIC_TUNABLES:
        if patch.get(key) is None:
            continue
        set_pic_setting(db, key, int(patch[key]))
    tunables = pic_settings(db)
    return {"settings": {key: tunables[key] for key in PIC_TUNABLES}}


# ── Source cards ─────────────────────────────────────────────────────

@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    _require_pic()
    per_source = source_counts_all(db)
    return [_source_view(row, per_source.get(row.id, {}))
            for row in db.query(PicSource).order_by(PicSource.id).all()]


@router.post("/sources")
def create_source(data: PicSourceCreate, request: Request, db: Session = Depends(get_db)):
    _require_auth(request, db)
    _guard_write()
    _require_pic()
    _validate_source(data.kind, data.root_path, data.host, data.share, data.url)
    now = _now_iso()
    row = PicSource(
        name=data.name.strip(), kind=data.kind, root_path=data.root_path.strip(),
        host=data.host.strip(), share=data.share.strip(), subpath=data.subpath.strip(),
        url=data.url.strip(), username=data.username.strip(), password=data.password,
        enabled=1, state="idle", created_at=now, updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _source_view(row)


@router.put("/sources/{source_id}")
def edit_source(source_id: int, data: PicSourceUpdate, request: Request,
                db: Session = Depends(get_db)):
    _require_auth(request, db)
    _guard_write()
    _require_pic()
    row = _get_source(db, source_id)
    patch = data.model_dump(exclude_unset=True)

    merged = {f: patch.get(f, getattr(row, f) or "")
              for f in ("kind", "root_path", "host", "share", "url")}
    _validate_source(merged["kind"], merged["root_path"], merged["host"],
                     merged["share"], merged["url"])

    address_changed = any(
        patch.get(f) is not None and (patch[f] or "") != (getattr(row, f) or "")
        for f in ("kind", "root_path", "host", "share", "subpath", "url"))

    for field in ("name", "kind", "root_path", "host", "share", "subpath", "url", "username"):
        if patch.get(field) is not None:
            setattr(row, field, str(patch[field]).strip())
    if "password" in patch and patch["password"] is not None:
        row.password = patch["password"]  # "" clears, omitted keeps — card policy
    if patch.get("enabled") is not None:
        row.enabled = 1 if patch["enabled"] else 0
        if not row.enabled and row.state == "scanning":
            row.state = "paused"  # mid-walk disable: the cursor waits for resume

    if address_changed:
        # A moved address invalidates the old cursor: re-scan from the top.
        row.scan_cursor, row.last_scan_at, row.state = None, None, "idle"

    row.updated_at = _now_iso()
    db.commit()
    return _source_view(row, source_counts(db, row.id))


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    """Drop a source and everything MSC derived from it — thumbnails, rows,
    vectors. The original photos are never touched: deleting the card that
    points at a folder must not delete the folder."""
    _require_auth(request, db)
    _guard_write()
    _require_pic()
    row = _get_source(db, source_id)

    item_ids = [i[0] for i in db.query(PicItem.id).filter(PicItem.source_id == row.id).all()]
    if item_ids:
        db.query(PicVector).filter(PicVector.pic_id.in_(item_ids)).delete(synchronize_session=False)
        db.query(PicItem).filter(PicItem.source_id == row.id).delete(synchronize_session=False)
    db.delete(row)
    db.commit()

    thumb_root = Path(settings.pic_dir) / "thumbs" / str(source_id)
    if thumb_root.is_dir():
        import shutil
        shutil.rmtree(thumb_root, ignore_errors=True)
    return {"deleted": source_id, "items_removed": len(item_ids),
            "by": _actor(request),
            "note": "只删除了 MSC 内的派生数据（缩略图/描述/向量），原图未改动"}


@router.post("/sources/{source_id}/probe")
def probe_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    """Connectivity check for the card's Probe button; persists the outcome."""
    _require_auth(request, db)
    _require_pic()
    row = _get_source(db, source_id)
    try:
        adapter = build_source(row)
    except SourceError as exc:
        row.last_error = str(exc)[:500]
        db.commit()
        return {"ok": False, "error": str(exc)}
    try:
        report = adapter.probe()
    except Exception as exc:  # adapters should not raise from probe, belt and braces
        report = {"reachable": False, "root_ok": False, "error": str(exc)[:300]}
    finally:
        adapter.close()
    ok = bool(report.get("reachable") and report.get("root_ok"))
    row.last_error = None if ok else (report.get("error") or "不可达")[:500]
    row.updated_at = _now_iso()
    db.commit()
    return {"ok": ok, **report}


@router.post("/sources/{source_id}/scan")
def request_scan(source_id: int, request: Request, db: Session = Depends(get_db)):
    """Ask for a fresh walk now: clears the schedule so the worker picks it up
    on the next tick (walking is the worker's job, never the request's)."""
    _require_auth(request, db)
    _guard_write()
    _require_pic()
    row = _get_source(db, source_id)
    row.last_scan_at, row.scan_cursor = None, None
    row.scan_epoch = (row.scan_epoch or 0) + 1
    if row.state != "paused":
        row.state = "idle"
    row.updated_at = _now_iso()
    db.commit()
    return {"source_id": row.id,
            "message": "已排入扫描队列；worker 每 15 秒推进一段，中断后可续传"}


@router.post("/sources/{source_id}/pause")
def pause_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    _require_auth(request, db)
    _guard_write()
    _require_pic()
    row = _get_source(db, source_id)
    row.state, row.updated_at = "paused", _now_iso()
    db.commit()
    return {"source_id": row.id, "state": "paused",
            "note": "扫描游标已保留，继续时从断点接着走"}


@router.post("/sources/{source_id}/resume")
def resume_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    _require_auth(request, db)
    _guard_write()
    _require_pic()
    row = _get_source(db, source_id)
    row.state, row.updated_at = "idle", _now_iso()
    db.commit()
    return {"source_id": row.id, "state": "idle",
            "resuming": bool(row.scan_cursor)}


# ── Local folder picker ──────────────────────────────────────────────

@router.get("/browse")
def browse(path: str = Query("", description="server-side directory"),
           db: Session = Depends(get_db)):
    """List directories under MSC_PIC_ROOTS so source cards can be picked —
    never typed — from the Web UI. Subdirectories only, jailed to the roots."""
    _require_pic()
    roots = pic_roots()
    target = path.strip() or (roots[0] if roots else "")
    if not target:
        return {"roots": roots, "path": "", "dirs": [],
                "message": "MSC_PIC_ROOTS 未配置，没有可浏览的目录根"}
    real = os.path.realpath(target)
    if not _under_roots(real):
        raise HTTPException(status_code=403, detail="该路径不在允许的目录根（MSC_PIC_ROOTS）内")
    if not os.path.isdir(real):
        raise HTTPException(status_code=404, detail=f"目录不存在：{target}")
    dirs = []
    try:
        with os.scandir(real) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        dirs.append({"name": entry.name, "path": entry.path})
                except OSError:
                    continue
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"没有权限读取 {target}")
    return {"roots": roots, "path": real, "parent": os.path.dirname(real), "dirs": dirs}


# ── Gallery ──────────────────────────────────────────────────────────

@router.get("/items")
def list_items(source_id: int = Query(None), state: str = Query(None),
               kind: str = Query(None), q: str = Query("", max_length=200),
               taken_from: str = Query("", max_length=10), taken_to: str = Query("", max_length=10),
               folder: str = Query("", max_length=300, description="源内子目录过滤，如 2024/夏"),
               view: str = Query("", pattern="^(|gallery|trash|missing)$",
                                 description="相册 / 回收站 / 缺失(原图已不在源里)"),
               include_archived: bool = Query(False),
               include_missing: bool = Query(False),
               trash: bool = Query(False, description="（旧参数）等同 view=trash"),
               ids_only: bool = Query(False, description="只要 id 清单：全选/清空回收站用"),
               limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
               db: Session = Depends(get_db)):
    """Gallery page: placeholders and finished tiles together, newest-first
    by taken_at (photos still without EXIF sit at the end until thumb runs)."""
    _require_pic()
    query = (db.query(PicItem, PicSource.name)
             .join(PicSource, PicSource.id == PicItem.source_id))
    conditions = []
    if view == "trash" or trash:
        conditions.append(PicItem.archived == 1)
    elif view == "missing":
        conditions.append(and_(PicItem.archived == 0, PicItem.missing == 1))
    else:
        if not include_archived:
            conditions.append(PicItem.archived == 0)
        if not include_missing:
            conditions.append(PicItem.missing == 0)
    if source_id is not None:
        conditions.append(PicItem.source_id == source_id)
    if state:
        if state not in PIC_STATES:
            raise HTTPException(status_code=422,
                                detail=f"未知状态 {state!r}（支持 {'/'.join(PIC_STATES)}）")
        conditions.append(PicItem.state == state)
    if kind:
        if kind not in ("photo", "screenshot"):
            raise HTTPException(status_code=422, detail="kind 只支持 photo/screenshot")
        conditions.append(PicItem.kind == kind)
    if folder.strip():
        clean = folder.strip().strip("/")
        if ".." in clean.split("/"):
            raise HTTPException(status_code=422, detail="目录过滤不能包含 ..")
        conditions.append(PicItem.rel_path.like(f"{clean}/%"))
    if q.strip():
        needle = f"%{q.strip()}%"
        conditions.append(or_(PicItem.name.like(needle), PicItem.caption.like(needle),
                              PicItem.rel_path.like(needle)))
    if taken_from.strip():
        conditions.append(PicItem.taken_at >= taken_at_prefix(taken_from))
    if taken_to.strip():
        conditions.append(PicItem.taken_at <= taken_at_prefix(taken_to, end=True))
    if conditions:
        query = query.filter(and_(*conditions))
    # Count the base table, not the joined one: source_id is a NOT NULL FK to a
    # primary key, so the pic_sources join can only add rows to a COUNT.
    where = and_(*conditions) if conditions else sql_true()
    total = db.query(PicItem).filter(where).count()

    if ids_only:
        # Select-all / empty-trash need the FULL id set, not just the page.
        ids = [row[0] for row in db.query(PicItem.id).filter(where)
               .order_by(PicItem.id).limit(5000).all()]
        return {"total": total, "ids": ids, "truncated": total > len(ids)}

    rows = (query.order_by(PicItem.taken_at.isnot(None).desc(), PicItem.taken_at.desc(),
                           PicItem.id.desc())
            .limit(limit).offset(offset).all())
    # Duplicate badge: same content hash appearing more than once (burst
    # shots, re-saved copies). Only the hashes on THIS page are probed, so the
    # indexed lookup stays O(page) instead of grouping the whole library on
    # every gallery request (the UI polls this every 5 s).
    page_hashes = list({item.content_hash for item, _ in rows if item.content_hash})
    dup_hashes = set()
    for start in range(0, len(page_hashes), 400):
        chunk = page_hashes[start:start + 400]
        marks = ", ".join(f":h{i}" for i in range(len(chunk)))
        dup_hashes |= {row[0] for row in db.execute(sql_text(
            "SELECT content_hash FROM pic_items WHERE archived = 0 "
            f"AND content_hash IN ({marks}) "
            "GROUP BY content_hash HAVING count(*) > 1"),
            {f"h{i}": h for i, h in enumerate(chunk)})}
    return {"total": total, "limit": limit, "offset": offset,
            "items": [_item_view(item, name, item.content_hash in dup_hashes)
                      for item, name in rows]}


def taken_at_prefix(value: str, end: bool = False) -> str:
    """'2026-11' / '2026-11-10' → comparable ISO bound (start or end of span)."""
    v = value.strip()
    if len(v) == 4:                      # year
        return f"{v}-12-31T23:59:59" if end else f"{v}-01-01"
    if len(v) == 7:                      # month
        if end:
            year, month = int(v[:4]), int(v[5:7])
            nxt = f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"
            return f"{nxt}-01T00:00:00"
        return f"{v}-01"
    if len(v) == 10:                     # day
        return f"{v}T23:59:59" if end else f"{v}T00:00:00"
    return v


@router.get("/folders")
def pic_folders(source_id: int = Query(...), db: Session = Depends(get_db)):
    """Distinct subdirectories of one source (from already-indexed rel_paths)
    — feeds the folder dropdown in the gallery toolbar."""
    _require_pic()
    rels = [row[0] for row in db.query(PicItem.rel_path)
            .filter(PicItem.source_id == source_id, PicItem.archived == 0)
            .limit(50000).all()]
    folders: set = set()
    for rel in rels:
        cur = ""
        for part in rel.split("/")[:-1]:
            cur = f"{cur}/{part}" if cur else part
            folders.add(cur)
    return {"source_id": source_id, "folders": sorted(folders)[:500]}


@router.get("/search")
def pic_search(q: str = Query(..., min_length=1), top: int = Query(12, ge=1, le=100),
               db: Session = Depends(get_db)):
    """Hybrid photo search: captions in pic_vectors (cosine) fused with a
    keyword leg over name/caption/rel_path via the same RRF as memories.

    Unlike /rag/search there is no 503 without an embed card: photos still
    answer with keyword hits and a note — the gallery degrades, it does not
    refuse.
    """
    _require_pic()
    recall = max(rag_setting(db, "recall_top"), top)
    sem_ids, sem_cos, sem_error = pic_semantic_ids(db, q, recall)

    needle = f"%{q.strip()}%"
    kw_rows = (db.query(PicItem.id)
               .filter(PicItem.archived == 0, PicItem.missing == 0,
                       or_(PicItem.name.like(needle), PicItem.caption.like(needle),
                           PicItem.rel_path.like(needle)))
               .order_by(PicItem.updated_at.desc()).limit(recall).all())
    kw_ids = [row[0] for row in kw_rows]

    if not sem_ids and not kw_ids:
        note = sem_error or "没有命中；embed 卡未配置时只有关键词搜索"
        return {"query": q, "results": [], "note": note}

    fused = rrf_fusion(kw_ids, sem_ids, rrf_k=rag_setting(db, "rrf_k"))
    top_ids = [pid for pid, _, _ in fused][:top]
    by_id = {i.id: i for i in db.query(PicItem).filter(PicItem.id.in_(top_ids)).all()}
    names = {}
    if by_id:
        src_ids = {i.source_id for i in by_id.values()}
        names = dict(db.query(PicSource.id, PicSource.name)
                     .filter(PicSource.id.in_(src_ids)).all())
    results = []
    for pid, score, match in fused[:top]:
        item = by_id.get(pid)
        if item is None or item.archived:
            continue
        view = _item_view(item, names.get(item.source_id, ""))
        view["score"] = score
        view["match"] = match
        view["cos"] = round(sem_cos.get(pid, 0.0), 4) if match in ("semantic", "both") else None
        results.append(view)
    out = {"query": q, "results": results,
           "model": (get_config(db, "embed").model or "").strip()}
    if sem_error:
        out["note"] = f"语义腿不可用：{sem_error}"
    return out


@router.get("/item/{item_id}")
def item_detail(item_id: int, db: Session = Depends(get_db)):
    _require_pic()
    row = db.get(PicItem, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"照片 {item_id} 不存在")
    source = db.get(PicSource, row.source_id)
    view = _item_view(row, source.name if source else "")
    view["origin_uri"] = row.origin_uri
    return view


@router.get("/item/{item_id}/thumb")
def item_thumb(item_id: int, db: Session = Depends(get_db)):
    """The webp tile. Cacheable forever under id+content hash: callers ask
    with ?v=<content_hash>, so a re-render (new bytes, thumb_px change) lands
    under a fresh URL while unchanged photos keep hitting the cache."""
    _require_pic()
    row = db.get(PicItem, item_id)
    if row is None or not row.thumb_path:
        raise HTTPException(status_code=404, detail="缩略图还没有生成")
    target = Path(settings.pic_dir) / row.thumb_path
    if not target.is_file():
        raise HTTPException(status_code=404, detail="缩略图文件缺失（重新扫描可补齐）")
    return FileResponse(str(target), media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=86400"})


# ── Original images (streamed from wherever they live) ───────────────

_MEDIA = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
          "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
          "tif": "image/tiff", "tiff": "image/tiff",
          "heic": "image/heic", "heif": "image/heif"}


@router.get("/item/{item_id}/original")
def item_original(item_id: int, db: Session = Depends(get_db)):
    """The untouched original, streamed through the source adapter — local
    file, SMB share or WebDAV alike. MSC never copies it to disk."""
    _require_pic()
    row = db.get(PicItem, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"照片 {item_id} 不存在")
    source = db.get(PicSource, row.source_id)
    if source is None:
        raise HTTPException(status_code=410, detail="这张照片的源卡片已被删除，原图位置未知")
    cap = settings.pic_max_original_mb * (1 << 20) if settings.pic_max_original_mb > 0 else 0
    try:
        adapter = build_source(source)
        inner = adapter.stream(row.rel_path, max_bytes=cap)
    except SourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    def guarded():
        try:
            yield from inner
        except (SourceError, OSError) as exc:
            _log.warning("original stream aborted for %s: %s", row.origin_uri, exc)
        finally:
            adapter.close()

    safe = re.sub(r'[^\w.\-]', "_", row.name or "photo")[:100]
    return StreamingResponse(
        guarded(),
        media_type=_MEDIA.get(row.ext, "application/octet-stream"),
        headers={"Content-Disposition": f'inline; filename="{safe}"',
                 "Cache-Control": "private, max-age=3600"},
    )


# ── Trash, bulk, purge ───────────────────────────────────────────────

def _set_archived(items: list, archived: int) -> int:
    now = _now_iso()
    for item in items:
        item.archived = archived
        item.deleted_at = now if archived else None
        item.updated_at = now
    return len(items)


@router.delete("/item/{item_id}")
def soft_delete_item(item_id: int, request: Request, db: Session = Depends(get_db)):
    """Trash-bin an item: gallery/search hide it, the file is untouched."""
    _require_auth(request, db)
    _guard_write()
    _require_pic()
    row = db.get(PicItem, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"照片 {item_id} 不存在")
    _set_archived([row], 1)
    db.commit()
    return {"id": item_id, "archived": True, "by": _actor(request),
            "note": "已进回收站，原图未动，可随时恢复"}


@router.post("/item/{item_id}/restore")
def restore_item(item_id: int, request: Request, db: Session = Depends(get_db)):
    _require_auth(request, db)
    _guard_write()
    _require_pic()
    row = db.get(PicItem, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"照片 {item_id} 不存在")
    _set_archived([row], 0)
    db.commit()
    return {"id": item_id, "archived": False}


@router.post("/items/batch")
def batch_items(data: PicItemsBatch, request: Request, db: Session = Depends(get_db)):
    """Multi-select gallery actions: archive / restore / retry."""
    _require_auth(request, db)
    _guard_write()
    _require_pic()
    rows = db.query(PicItem).filter(PicItem.id.in_(data.ids)).all()
    changed = 0
    if data.action in ("archive", "restore"):
        changed = _set_archived(rows, 1 if data.action == "archive" else 0)
    elif data.action == "retry":
        now = _now_iso()
        for row in rows:
            if row.state != "failed":
                continue
            row.state, row.error_count, row.retry_at, row.last_error = \
                "discovered", 0, None, None
            row.updated_at = now
            changed += 1
    elif data.action == "unmissing":
        # 保留: the original is gone but the user wants the tile kept.
        now = _now_iso()
        for row in rows:
            row.missing, row.updated_at = 0, now
            changed += 1
    elif data.action == "forget":
        # 清除: drop thumbnails/vectors/rows — derived data only, the
        # original (wherever it still is) is never touched by this action.
        # No ORM edits on rows we are about to bulk-delete: a pending UPDATE
        # flushed after the DELETE is a StaleDataError.
        ids = []
        for row in rows:
            if row.thumb_path:
                try:
                    (Path(settings.pic_dir) / row.thumb_path).unlink(missing_ok=True)
                except OSError:
                    pass
            ids.append(row.id)
        if ids:
            db.query(PicVector).filter(PicVector.pic_id.in_(ids)).delete(synchronize_session=False)
            db.query(PicItem).filter(PicItem.id.in_(ids)).delete(synchronize_session=False)
            changed = len(ids)
    db.commit()
    return {"action": data.action, "changed": changed}


@router.post("/items/purge")
def purge_items(data: PicItemsPurge, request: Request, db: Session = Depends(get_db)):
    """彻底删除: remove the ORIGINAL files, then the derived rows.

    The only photo endpoint that touches real photos. confirm=true is a hard
    gate (the UI makes the user type DELETE); a failed original delete keeps
    the row so nothing silently disappears — the error names the item.
    """
    _require_auth(request, db)
    _guard_write()
    _require_pic()
    if not data.confirm:
        raise HTTPException(status_code=422,
                            detail="彻底删除会移除磁盘上的原图，需要 confirm=true 才执行")

    # One fetch for the rows and one for their sources: emptying a trash bin of
    # 500 photos used to be 1000 point SELECTs before the first unlink.
    wanted = list(dict.fromkeys(data.ids))
    items = {row.id: row for row in
             db.query(PicItem).filter(PicItem.id.in_(wanted)).all()}
    src_ids = {row.source_id for row in items.values()}
    sources = ({row.id: row for row in
                db.query(PicSource).filter(PicSource.id.in_(src_ids)).all()}
               if src_ids else {})

    adapters: dict = {}
    purged, kept, errors = 0, 0, []
    for item_id in wanted:
        item = items.get(item_id)
        if item is None:
            kept += 1
            continue
        try:
            source = sources.get(item.source_id)
            if source is None:
                raise SourceError("源卡片已删除，无法定位原图")
            adapter = adapters.setdefault(item.source_id, build_source(source))
            adapter.delete(item.rel_path)
        except (SourceError, OSError) as exc:
            errors.append(f"{item_id}: {exc}")
            kept += 1
            continue
        if item.thumb_path:
            thumb = Path(settings.pic_dir) / item.thumb_path
            try:
                thumb.unlink()
            except FileNotFoundError:
                pass
        db.query(PicVector).filter(PicVector.pic_id == item.id).delete(synchronize_session=False)
        db.delete(item)
        purged += 1
    db.commit()
    for adapter in adapters.values():
        try:
            adapter.close()
        except Exception:
            pass
    return {"purged": purged, "kept": kept, "errors": errors[:10], "by": _actor(request)}
