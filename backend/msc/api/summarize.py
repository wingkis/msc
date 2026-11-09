"""API routes: the model card and memory digests.

These exist for a human in the Web UI, not for agents — that is why they are
deliberately absent from /api/v1/help and /llms.txt. Everything that writes
goes through the usual auth/read-only guards so the emergency switches still
hold.
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from msc.api.memories import _actor, _guard_write, _require_auth, _resolve_memory
from msc.database import get_db
from msc.models import Memory, SummaryRun
from msc.schemas import ModelConfigUpdate, SummarizeRequest
from msc.services import llm
from msc.services.model_config import get_config, note_result, public_view, update_config
from msc.services.summarizer import (
    day_bounds, memories_in_window, restore_sources, run_summary,
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
    if purpose == "chat":
        return _test_chat_card(db, cfg)
    return _test_rag_card(db, cfg, purpose)


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


@router.post("/summarize/selected")
def summarize_selected(data: SummarizeRequest, request: Request, db: Session = Depends(get_db)):
    """Summarise the memories the user ticked in the list."""
    _require_auth(request, db)
    if not data.dry_run:
        _guard_write()
    actor = _actor(request)

    if not data.slugs:
        raise HTTPException(status_code=422, detail="要先选择至少一条记忆")

    memories: List[Memory] = []
    missing: List[str] = []
    for ref in data.slugs:
        try:
            memory = _resolve_memory(db, ref)
        except HTTPException:
            missing.append(ref)
            continue
        if memory not in memories:
            memories.append(memory)
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
