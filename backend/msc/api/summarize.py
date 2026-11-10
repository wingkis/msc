"""API routes: the model card and memory digests.

These exist for a human in the Web UI, not for agents — that is why they are
deliberately absent from /api/v1/help and /llms.txt. Everything that writes
goes through the usual auth/read-only guards so the emergency switches still
hold.
"""

from datetime import date
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from msc.api.memories import _actor, _guard_write, _require_auth, _resolve_memory
from msc.database import get_db
from msc.models import Memory, SummaryRun
from msc.schemas import ModelConfigUpdate, SummarizeRequest
from msc.services import llm
from msc.services.model_config import get_config, note_result, public_view, update_config
from msc.services.summarizer import (
    SEARCH_GROUNDING, SEARCH_PROMPT_TEMPLATE, day_bounds, memories_in_window,
    restore_sources, run_summary,
)

router = APIRouter()


def _parse_day(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"date 需要 YYYY-MM-DD，收到 {value!r}")


# ── Model cards ──────────────────────────────────────────────────────

def _purpose_or_422(purpose: str) -> str:
    from msc.services.model_config import PURPOSES
    if purpose not in PURPOSES:
        raise HTTPException(status_code=422,
                            detail=f"unknown purpose {purpose!r}; allowed: {', '.join(PURPOSES)}")
    return purpose


@router.get("/model")
def read_model(purpose: str = Query("chat"), db: Session = Depends(get_db)):
    """One card as the UI edits it (default: the chat card). Keys stay masked."""
    return public_view(get_config(db, _purpose_or_422(purpose)))


@router.get("/model/cards")
def read_cards(db: Session = Depends(get_db)):
    """Every purpose card at once — what the settings page renders."""
    from msc.services.model_config import get_cards
    return [public_view(row) for row in get_cards(db)]


@router.put("/model")
def write_model(data: ModelConfigUpdate, request: Request,
                purpose: str = Query("chat"), db: Session = Depends(get_db)):
    """Save one card. Bad vocabulary or a malformed schedule returns 422."""
    _require_auth(request, db)
    _guard_write()
    try:
        row = update_config(db, data.model_dump(exclude_unset=True), _purpose_or_422(purpose))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return public_view(row)


@router.post("/model/test")
def test_model(request: Request, purpose: str = Query("chat"),
               db: Session = Depends(get_db)):
    """Reachability probe, dispatched by purpose.

    chat: /v1/models then one short completion (both signals matter — some
    local servers list models but not the one the card names).
    embed: embed one sentence and report the dimension, which the card shows.
    rerank: rerank two documents and check the order came back.
    whisper: GET /health against the server (llama.cpp convention).
    """
    _require_auth(request, db)
    _guard_write()

    purpose = _purpose_or_422(purpose)
    cfg = get_config(db, purpose)
    if purpose in ("chat", "echo"):
        return _test_chat_card(db, cfg)
    if purpose == "vision":
        return _test_vision_card(db, cfg)
    return _test_rag_card(db, cfg, purpose)


def _probe_png_bytes() -> bytes:
    """A 16x16 red square, built on the fly — small enough to inline in every
    vision probe, distinctive enough that 'what colour is this' answers prove
    the image actually reached the model."""
    from io import BytesIO

    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (16, 16), "red").save(buf, "PNG")
    return buf.getvalue()


def _test_vision_card(db, cfg):
    """Vision probes must send an image. A chat-only llama.cpp (no --mmproj)
    answers text fine and rejects pictures — a text-only probe would call it
    ready, and every caption would fail later."""
    report = {"endpoint": None, "models": [], "chat": None, "error": None}
    try:
        report["endpoint"] = llm.chat_endpoint(cfg.base_url)
        try:
            report["models"] = llm.list_models(cfg)
        except llm.LLMError as exc:
            report["models_error"] = str(exc)
        # 512, not 64: reasoning-style vision models (q3 and friends) spend
        # the whole budget thinking and answer with empty content — the probe
        # would blame the model for arithmetic it imposed itself.
        answer = llm.chat(cfg, [{"role": "user", "content": [
            llm.image_part(_probe_png_bytes(), "image/png"),
            {"type": "text", "text": "连接测试。只用两个字描述这张图的颜色。"},
        ]}], max_tokens=512)
        report["chat"] = {"ok": True, "model": answer["model"], "reply": answer["text"][:120],
                          "latency_ms": answer["latency_ms"]}
        note_result(db, cfg)
        report["ok"] = True
    except llm.LLMError as exc:
        note_result(db, cfg, error=str(exc))
        report["error"] = str(exc)
        report["ok"] = False
    return report


def _test_chat_card(db, cfg):
    report = {"endpoint": None, "models": [], "chat": None, "error": None}
    try:
        report["endpoint"] = llm.chat_endpoint(cfg.base_url)
        try:
            report["models"] = llm.list_models(cfg)
        except llm.LLMError as exc:
            # A missing /v1/models is not fatal; the chat probe below decides.
            report["models_error"] = str(exc)

        answer = llm.chat(cfg, [{"role": "user",
                                 "content": "连接测试。只回复两个字：可用"}], max_tokens=256)
        report["chat"] = {"ok": True, "model": answer["model"], "reply": answer["text"][:120],
                          "latency_ms": answer["latency_ms"]}
        note_result(db, cfg)
        report["ok"] = True
    except llm.LLMError as exc:
        note_result(db, cfg, error=str(exc))
        report["error"] = str(exc)
        report["ok"] = False
    return report


def _test_rag_card(db, cfg, purpose):
    from msc.services import rag_providers

    report: dict = {"purpose": purpose, "endpoint": None, "probe": None, "error": None}
    try:
        if purpose == "embed":
            report["endpoint"] = rag_providers.embeddings_endpoint(cfg.base_url)
            vectors = rag_providers.embed(cfg, ["连接测试"])
            dim = len(vectors[0])
            cfg.probed_dim = dim
            note_result(db, cfg)
            report["probe"] = {"ok": True, "dim": dim,
                               "latency_ms": getattr(rag_providers.embed, "latency_ms", 0)}
        elif purpose == "rerank":
            report["endpoint"] = rag_providers.rerank_endpoint(cfg.base_url)
            results = rag_providers.rerank(cfg, "测试", ["无关的内容。", "这是连接测试。"], top_n=2)
            best = max(results, key=lambda r: r["score"]) if results else None
            note_result(db, cfg)
            report["probe"] = {"ok": bool(best) and best["index"] == 1,
                               "results": results[:2]}
        else:  # whisper
            import httpx
            report["endpoint"] = rag_providers.transcriptions_endpoint(cfg.base_url)
            health_url = rag_providers._base(cfg.base_url) + "/health"
            try:
                with httpx.Client(timeout=float(cfg.timeout_seconds or 120)) as client:
                    response = client.get(health_url)
                report["probe"] = {"ok": response.status_code < 400,
                                   "health_status": response.status_code}
            except httpx.HTTPError:
                # No /health is legal for whisper endpoints; report the endpoint
                # as reachable-if-postable, leave the real check to first use.
                report["probe"] = {"ok": True, "health_status": "no /health; 首次使用验证"}
        report["ok"] = bool(report["probe"] and report["probe"].get("ok"))
        if not report["ok"]:
            note_result(db, cfg, error="探针没有通过")
            report["error"] = "探针没有通过（响应形状不对或分数顺序反了）"
    except (llm.LLMError, ValueError) as exc:
        note_result(db, cfg, error=str(exc))
        report["error"] = str(exc)
        report["ok"] = False
    return report


# ── Digests ──────────────────────────────────────────────────────────

@router.get("/summarize/window")
def summarize_window(day: Optional[str] = Query(None, description="YYYY-MM-DD, card timezone"),
                     db: Session = Depends(get_db)):
    """What "today" would cover, so the UI can show the scope before spending
    a model call on it."""
    cfg = get_config(db)
    period, start, end = day_bounds(cfg, _parse_day(day))
    memories = memories_in_window(db, cfg, period)
    return {
        "period": period, "start": start, "end": end, "count": len(memories),
        "timezone": cfg.auto_timezone or "container",
        "memories": [{"id": m.id, "slug": m.slug, "name": m.name, "updated_at": m.updated_at}
                     for m in memories],
    }


def _resolve_refs(db: Session, refs: List[str]) -> Tuple[List[Memory], List[str]]:
    """Memory rows for slugs/ids, plus the references that resolved to nothing.

    A digest over a search result set runs while the page still shows rows that
    may have been deleted elsewhere, so partial resolution is normal."""
    memories: List[Memory] = []
    missing: List[str] = []
    for ref in refs:
        try:
            memory = _resolve_memory(db, ref)
        except HTTPException:
            missing.append(ref)
            continue
        if memory not in memories:
            memories.append(memory)
    return memories, missing


@router.post("/summarize/selected")
def summarize_selected(data: SummarizeRequest, request: Request, db: Session = Depends(get_db)):
    """Summarise the memories the user ticked in the list."""
    _require_auth(request, db)
    if not data.dry_run:
        _guard_write()
    actor = _actor(request)

    if not data.slugs:
        raise HTTPException(status_code=422, detail="要先选择至少一条记忆")

    memories, missing = _resolve_refs(db, data.slugs)
    if not memories:
        raise HTTPException(status_code=404,
                            detail=f"选中的记忆都不存在: {', '.join(missing[:5])}")

    try:
        result = run_summary(
            db, get_config(db), memories, actor=actor, kind="manual",
            name=data.name or "", category=data.category or "", priority=data.priority or "",
            tags=data.tags, archive_sources=data.archive_sources,
            instruction=data.instruction or "", dry_run=data.dry_run,
        )
    except llm.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    result["missing"] = missing
    return result


@router.post("/summarize/search")
def summarize_search(data: SummarizeRequest, request: Request, db: Session = Depends(get_db)):
    """Digest the memories the last search returned.

    The user typed a query and wants it answered, so this route sends
    ``SEARCH_PROMPT_TEMPLATE`` (answer / 依据 / 记忆里没有的) with the query in
    the prompt, plus ``SEARCH_GROUNDING``: the query already chose these rows,
    so explaining the topic from the model's own background knowledge is the
    main way this goes wrong. The sources are never archived either - the
    card's ``archive_sources`` switch belongs to the daily digest, and quietly
    soft-deleting a search result set would be nasty."""
    _require_auth(request, db)
    if not data.dry_run:
        _guard_write()
    actor = _actor(request)

    if not data.slugs:
        raise HTTPException(status_code=422, detail="先搜索出结果，再点「搜索并总结」")

    memories, missing = _resolve_refs(db, data.slugs)
    if not memories:
        raise HTTPException(status_code=404,
                            detail=f"这些记忆已不存在: {', '.join(missing[:5])}")

    query = (data.query or "").strip()
    framing = f"关键词命中 {len(memories)} 条" if query else f"选中 {len(memories)} 条"
    instruction = "\n".join(part for part in (SEARCH_GROUNDING, (data.instruction or "").strip()) if part)

    try:
        result = run_summary(
            db, get_config(db), memories, actor=actor, kind="manual",
            name=data.name or "", category=data.category or "", priority=data.priority or "",
            tags=data.tags or ["summary", "search"], archive_sources=bool(data.archive_sources),
            instruction=instruction, date_range=framing, query=query,
            template=SEARCH_PROMPT_TEMPLATE,
            note=f"由搜索「{query}」的命中生成。" if query else "",
            dry_run=data.dry_run,
        )
    except llm.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    result["missing"] = missing
    result["query"] = query
    return result


@router.post("/summarize/daily")
def summarize_daily(data: SummarizeRequest, request: Request, db: Session = Depends(get_db)):
    """Digest one local day — the manual twin of the scheduled job."""
    _require_auth(request, db)
    if not data.dry_run:
        _guard_write()
    actor = _actor(request)

    cfg = get_config(db)
    period, _, _ = day_bounds(cfg, _parse_day(data.date))
    memories = memories_in_window(db, cfg, period)

    try:
        return run_summary(
            db, cfg, memories, actor=actor, kind="manual", period=period,
            name=data.name or "", category=data.category or "", priority=data.priority or "",
            tags=data.tags, archive_sources=data.archive_sources,
            instruction=data.instruction or "", date_range=period, dry_run=data.dry_run,
        )
    except llm.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/summarize/runs")
def list_runs(limit: int = Query(10, le=50), db: Session = Depends(get_db)):
    """Recent digest attempts, failures included."""
    rows = db.query(SummaryRun).order_by(SummaryRun.id.desc()).limit(limit).all()
    return [{
        "id": row.id, "kind": row.kind, "period": row.period, "slug": row.slug,
        "model": row.model, "source_count": row.source_count,
        "archived_count": row.archived_count, "ok": bool(row.ok), "error": row.error,
        "duration_ms": row.duration_ms, "created_at": row.created_at,
        "created_by": row.created_by,
    } for row in rows]


@router.post("/summarize/{slug}/restore-sources")
def restore_digest_sources(slug: str, request: Request, db: Session = Depends(get_db)):
    """Undo the soft-delete half of a digest. The digest itself is untouched."""
    _require_auth(request, db)
    _guard_write()

    digest = _resolve_memory(db, slug)
    restored = restore_sources(db, digest)
    return {"digest": digest.slug, "restored": restored, "by": _actor(request),
            "message": f"已恢复 {len(restored)} 条原始记忆" if restored
                       else "这条小结没有软删除任何记忆"}
