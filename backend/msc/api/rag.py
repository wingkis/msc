"""API routes: RAG status, hybrid search, reindex, voice, insights.

Web-UI-facing like the model card and digests — deliberately absent from
/api/v1/help and /llms.txt, except that GET /search?mode=hybrid (in
search.py) is part of the agent contract and degrades on its own.

Everything honours MSC_RAG_ENABLED (the kill switch), MSC_READ_ONLY and the
usual auth guards: an unconfigured provider never 500s, it degrades or
answers with a readable 4xx/5xx.
"""

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from msc.api.memories import _actor, _guard_write, _require_auth, _resolve_memory
from msc.config import get_settings
from msc.database import get_db
from msc.filesystem.md_writer import write_memory_file
from msc.models import Embedding, Memory, RagCache, contradicted_ids
from msc.schemas import EchoRequest, RagImportRequest, RagSettingsUpdate
from msc.services import llm, rag_providers
from msc.services.model_config import get_config, note_result, public_view
from msc.services.rag import (
    TUNABLES, dirty_depth, rrf_fusion, semantic_ids, setting, set_setting,
    vector_counts,
)

router = APIRouter()

settings = get_settings()

_AUDIO_EXT = {"wav", "mp3", "m4a", "ogg", "webm", "flac"}
_MAX_AUDIO_BYTES = 25 * 1024 * 1024


def _require_rag() -> None:
    if not settings.rag_enabled:
        raise HTTPException(status_code=503,
                            detail="RAG 已关闭（MSC_RAG_ENABLED=false），其余功能不受影响")


def _embed_card(db: Session):
    cfg = get_config(db, "embed")
    return cfg, bool(cfg.enabled and (cfg.base_url or "").strip() and (cfg.model or "").strip())


def _contradicted_ids(db: Session) -> set:
    """See msc.models.contradicted_ids — one definition, now including digests."""
    return contradicted_ids(db)


# ── Status ───────────────────────────────────────────────────────────

@router.get("/status")
def rag_status(db: Session = Depends(get_db)):
    """Provider readiness, vector counts, queue depth, effective tuning."""
    cards = {purpose: public_view(get_config(db, purpose))
             for purpose in ("embed", "rerank", "whisper")}
    embed_cfg, embed_ready = _embed_card(db)
    return {
        "enabled": settings.rag_enabled,
        "ready": settings.rag_enabled and embed_ready,
        "active_model": (embed_cfg.model or "").strip(),
        "vectors": vector_counts(db),
        "dirty": dirty_depth(db),
        "providers": cards,
        "settings": {key: setting(db, key) for key in TUNABLES},
    }


@router.put("/settings")
def update_settings(data: RagSettingsUpdate, request: Request, db: Session = Depends(get_db)):
    """Tune fusion parameters without a rebuild (DB wins over .env)."""
    _require_auth(request, db)
    _guard_write()
    patch = data.model_dump(exclude_unset=True)
    for key in TUNABLES:
        if patch.get(key) is None:
            continue
        value = int(patch[key])
        if value <= 0:
            raise HTTPException(status_code=422, detail=f"{key} 必须是正整数")
        set_setting(db, key, value)
    return {"settings": {key: setting(db, key) for key in TUNABLES}}


# ── Indexing ─────────────────────────────────────────────────────────

@router.post("/reindex")
def reindex(request: Request, db: Session = Depends(get_db)):
    """Queue every live memory for embedding. The worker drains in batches;
    GET /rag/status shows the remaining depth."""
    _require_auth(request, db)
    _guard_write()
    _require_rag()

    from msc.services.rag import mark_all_dirty
    marked = mark_all_dirty(db)
    return {"message": f"已把 {marked} 条记忆排入嵌入队列", "queued": marked,
            "note": "worker 每 15 秒消费一批；未配置 embed provider 时队列会保留"}


@router.delete("/vectors")
def purge_vectors(model: str = Query(..., description="Embedding model name to drop"),
                  request: Request = None, db: Session = Depends(get_db)):
    """Drop one model's vectors — the cleanup half of an embedding-model switch.

    The active model cannot be purged: that would empty semantic search until
    a reindex runs. Switch cards first, then purge the old model here.
    """
    _require_auth(request, db)
    _guard_write()
    _require_rag()

    cfg, ready = _embed_card(db)
    if ready and model == (cfg.model or "").strip():
        raise HTTPException(status_code=422,
                            detail=f"{model} 是当前 embed 卡片正在使用的模型，先换卡片再清理旧向量")
    removed = db.query(Embedding).filter(Embedding.model == model).delete()
    db.commit()
    return {"model": model, "removed": removed, "by": _actor(request) if request else ""}


# ── Search ───────────────────────────────────────────────────────────

@router.get("/search")
def rag_search(q: str = Query(..., min_length=1), top: int = Query(10, le=50),
               rerank: bool = Query(True, description="Use the rerank card when configured"),
               db: Session = Depends(get_db)):
    """Pure semantic search with optional rerank, plus the fused keyword view.

    Returns results tagged semantic/keyword/both — the same shape the search
    page renders, without paging filters.
    """
    if not settings.rag_enabled:
        raise HTTPException(status_code=503, detail="RAG 已关闭（MSC_RAG_ENABLED=false）")

    recall = max(setting(db, "recall_top"), top)
    sem_ids, sem_cos, sem_error = semantic_ids(db, q, recall)
    if not sem_ids:
        detail = sem_error or "embed provider 未配置或还没有向量；先用 /rag/reindex"
        raise HTTPException(status_code=503, detail=detail)

    # Optional rerank pass over the recalled candidates.
    rerank_note = None
    if rerank:
        cfg = get_config(db, "rerank")
        if cfg.enabled and (cfg.base_url or "").strip() and (cfg.model or "").strip():
            memories = db.query(Memory).filter(Memory.id.in_(sem_ids),
                                               Memory.archived == 0).all()
            docs = [f"{m.name}\n{m.description or ''}\n{(m.body or '')[:800]}"
                    for m in memories]
            try:
                ranked = rag_providers.rerank(cfg, q, docs, top_n=setting(db, "rerank_topn"))
                note_result(db, cfg)
                ordered, seen = [], set()
                for item in ranked:
                    idx = item["index"]
                    if 0 <= idx < len(docs) and memories[idx].id not in seen:
                        seen.add(memories[idx].id)
                        ordered.append(memories[idx].id)
                sem_ids = ordered or sem_ids
            except llm.LLMError as exc:
                note_result(db, cfg, error=str(exc))
                rerank_note = str(exc)  # degrade to plain cosine order

    keyword = [(row[0], row[1]) for row in _keyword_for_fusion(q, recall, False)]
    # Filtered before the fusion, not after the truncation: dropping a
    # contradicted memory must not also cost a result slot. The count travels
    # with the answer — a quiet disappearance is what made 「索引坏了」 and
    # 「已被推翻」 unreadable from outside in the first place.
    gone = _contradicted_ids(db)
    hidden = len(({i for i in sem_ids} | {i for i, _ in keyword}) & gone)
    if hidden:
        sem_ids = [i for i in sem_ids if i not in gone]
        keyword = [(mid, score) for mid, score in keyword if mid not in gone]
    fused = rrf_fusion([cid for cid, _ in keyword], sem_ids, rrf_k=setting(db, "rrf_k"))

    top_ids = [mid for mid, _, _ in fused][:top]
    by_id = {m.id: m for m in db.query(Memory).filter(Memory.id.in_(top_ids)).all()}
    results = []
    for mid, score, match in fused[:top]:
        m = by_id.get(mid)
        if not m or m.archived:
            continue
        results.append({
            "id": m.id, "slug": m.slug, "name": m.name, "description": m.description,
            "category": m.category, "priority": m.priority, "score": score,
            "match": match,
            "cos": round(sem_cos.get(mid, 0.0), 4) if match in ("semantic", "both") else None,
        })
    out = {"query": q, "results": results,
           "model": (get_config(db, "embed").model or "").strip(),
           "contradicted_hidden": hidden}
    if rerank_note:
        out["rerank_error"] = rerank_note
    return out


def _keyword_for_fusion(q: str, fetch: int, include_archived: bool):
    from msc.api.search import _keyword_candidates
    return _keyword_candidates(q, fetch, include_archived)


# ── Voice ────────────────────────────────────────────────────────────

@router.post("/transcribe")
def transcribe(request: Request, file: UploadFile = File(...),
               db: Session = Depends(get_db)):
    # Sync on purpose: the whisper call blocks for seconds and FastAPI runs
    # sync endpoints in the thread pool, keeping the event loop free.
    """Whisper a recording into text, with duplicate-check candidates.

    The browser sends 16-bit PCM WAV (whisper.cpp does not decode webm), so
    the UI encodes before uploading. ``similar`` lists live memories whose
    vectors sit above cos 0.6 with the transcript — the UI offers to append
    to one instead of creating a near-duplicate.
    """
    _require_auth(request, db)
    _guard_write()
    if not settings.rag_enabled:
        raise HTTPException(status_code=503, detail="RAG 已关闭（MSC_RAG_ENABLED=false）")

    cfg = get_config(db, "whisper")
    if not cfg.enabled or not (cfg.base_url or "").strip() or not (cfg.model or "").strip():
        raise HTTPException(status_code=503, detail="whisper provider 未配置：去设置页填语音转写卡片")

    name = file.filename or "audio.wav"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _AUDIO_EXT:
        raise HTTPException(status_code=422,
                            detail=f"不支持的音频格式 .{ext or '?'}（支持 {'/'.join(sorted(_AUDIO_EXT))}）")
    content = file.file.read(_MAX_AUDIO_BYTES + 1)
    if len(content) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="音频超过 25MB，请缩短录音")
    if not content:
        raise HTTPException(status_code=422, detail="上传的音频是空的")

    try:
        result = rag_providers.transcribe(cfg, name, content)
    except llm.LLMError as exc:
        note_result(db, cfg, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))
    note_result(db, cfg)

    similar = []
    embed_cfg, embed_ready = _embed_card(db)
    if embed_ready:
        sem_ids, sem_cos, _ = semantic_ids(db, result["text"], 5)
        # Near-duplicate suggestions decide what an agent appends to, so a
        # disproved belief must not be offered as a home for new facts.
        gone = _contradicted_ids(db)
        sem_ids = [i for i in sem_ids if i not in gone]
        by_id = {m.id: m for m in db.query(Memory).filter(Memory.id.in_(sem_ids)).all()}
        for mid in sem_ids:
            cos = sem_cos.get(mid, 0.0)
            if cos > 0.6 and mid in by_id:
                m = by_id[mid]
                similar.append({"id": m.id, "slug": m.slug, "name": m.name,
                                "cos": round(cos, 4)})
    return {"text": result["text"], "latency_ms": result["latency_ms"], "similar": similar}


# ── Insights ─────────────────────────────────────────────────────────

@router.get("/health-report")
def health_report(db: Session = Depends(get_db)):
    """Near-duplicate pairs (cos > 0.92), stale temporary memories, archive
    stats. Suggestions only — merging stays with the digest flow."""
    if not settings.rag_enabled:
        raise HTTPException(status_code=503, detail="RAG 已关闭（MSC_RAG_ENABLED=false）")

    report: dict = {"duplicates": [], "stale_temporary": [], "archived": {}}

    embed_cfg, embed_ready = _embed_card(db)
    if embed_ready:
        model_name = (embed_cfg.model or "").strip()
        total_vecs = db.query(Embedding).filter(Embedding.model == model_name).count()
        rows = db.query(Embedding.memory_id, Embedding.vec, Embedding.dim).filter(
            Embedding.model == model_name).limit(3001).all()
        live = {m.id: m for m in db.query(Memory).filter(Memory.archived == 0).all()}
        rows = [r for r in rows if r[0] in live][:3000]
        truncated = total_vecs > len(rows)
        # Pairwise cos is O(n^2/2); 3000 vectors ≈ 4.5M cos, sub-second with
        # numpy — and this runs on a human pressing a button, not per request.
        try:
            import numpy
            dim = rows[0][2] if rows else 0
            matrix = numpy.empty((len(rows), dim), dtype=numpy.float32)
            ids = []
            for mid, blob, row_dim in rows:
                if row_dim != dim or len(blob) != dim * 4:
                    continue  # mixed-dim leftovers from a model switch: skip row
                matrix[len(ids)] = numpy.frombuffer(blob, dtype=numpy.float32)
                ids.append(mid)  # ids and matrix rows must advance together
            matrix = matrix[:len(ids)]
            if ids and dim > 0:
                norms = numpy.linalg.norm(matrix, axis=1)
                norms = numpy.where(norms == 0, 1.0, norms)
                unit = matrix / norms[:, None]
                sims = unit @ unit.T
                # Vectorised upper-triangle extraction: 3000 vectors -> one
                # mask, not 4.5M Python-level numpy scalar lookups.
                i_idx, j_idx = numpy.where(numpy.triu(sims, 1) > 0.92)
                pairs = [{
                    "a": {"id": ids[i], "slug": live[ids[i]].slug, "name": live[ids[i]].name},
                    "b": {"id": ids[j], "slug": live[ids[j]].slug, "name": live[ids[j]].name},
                    "cos": round(float(sims[i][j]), 4),
                } for i, j in zip(i_idx, j_idx)]
                report["duplicates"] = pairs[:50]
                if len(pairs) > 50:
                    report["duplicates_note"] = f"共 {len(pairs)} 对，只列出前 50"
        except ImportError:
            report["duplicates_error"] = "numpy 未安装，重复检测跳过"
        except Exception as exc:
            report["duplicates_error"] = f"重复检测失败: {exc}"
        if truncated:
            report["duplicates_note"] = (report.get("duplicates_note", "") +
                                         "（只扫描了前 3000 条向量）").strip()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    stale = db.query(Memory).filter(
        Memory.archived == 0, Memory.lifespan == "temporary",
        Memory.updated_at < cutoff).order_by(Memory.updated_at).limit(50).all()
    report["stale_temporary"] = [
        {"id": m.id, "slug": m.slug, "name": m.name, "updated_at": m.updated_at}
        for m in stale]

    oldest = (db.query(Memory).filter(Memory.archived == 1)
              .order_by(Memory.updated_at).first())
    report["archived"] = {"count": db.query(Memory).filter(Memory.archived == 1).count(),
                          "oldest": oldest.updated_at if oldest else None}
    report["suggestion"] = (
        "重复对可以用记忆页多选 → 小结所选合并；temporary 过期记忆适合归档。"
        if (report["duplicates"] or stale) else "没有发现需要处理的问题。")
    return report


@router.get("/daily-pick")
def daily_pick(refresh: bool = Query(False), db: Session = Depends(get_db)):
    """A 700-900 character long-form piece woven from recent memories, cached
    per day (the generation costs a chat-model call; reading it should be free)."""
    if not settings.rag_enabled:
        raise HTTPException(status_code=503, detail="RAG 已关闭（MSC_RAG_ENABLED=false）")

    chat_cfg = get_config(db, "chat")
    if not chat_cfg.enabled or not (chat_cfg.base_url or "").strip() or not (chat_cfg.model or "").strip():
        raise HTTPException(status_code=503, detail="奇遇长文需要先在模型卡片里配置 chat 模型")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache_key = f"daily-pick-{today}"
    cached = db.query(RagCache).filter(RagCache.key == cache_key).first()
    if cached and not refresh:
        import json
        return {**json.loads(cached.value), "cached": True, "date": today}

    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    gone = _contradicted_ids(db)
    live = [Memory.archived == 0]
    if gone:
        live.append(Memory.id.notin_(gone))
    pool = db.query(Memory).filter(*live,
                                   Memory.updated_at >= week_ago).order_by(
        Memory.updated_at.desc()).limit(40).all()
    if not pool:
        pool = db.query(Memory).filter(*live).order_by(
            Memory.updated_at.desc()).limit(20).all()
    if not pool:
        raise HTTPException(status_code=422, detail="还没有记忆可以取材")

    # Deterministic-but-varied sample: the day picks the offset, so a refresh
    # within one day still lands on the same cache entry, and tomorrow differs.
    day_seed = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
    sample = pool[(day_seed * 7) % max(1, len(pool)):(day_seed * 7) % max(1, len(pool)) + 8] or pool[:8]
    source_block = "\n\n".join(
        f"### {m.name} ({m.slug})\n{(m.body or m.description or '')[:400]}" for m in sample)

    messages = [
        {"role": "system", "content": "你是记忆馆的守夜人。把给定的记忆片段编织成一篇 700-900 字的中文长文："
                                      "有场景、有转折、有细节，让读的人重新遇见这些记录里的决定与时刻。"
                                      "不要列清单，不要提及「记忆片段」这个词。直接输出正文。"},
        {"role": "user", "content": f"日期：{today}\n\n记忆片段：\n{source_block}"},
    ]
    try:
        answer = llm.chat(chat_cfg, messages, max_tokens=1800)
    except llm.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    import json
    payload = {"body": answer["text"], "model": answer["model"],
               "latency_ms": answer["latency_ms"], "sources": [m.slug for m in sample]}
    row = db.get(RagCache, cache_key)
    if row is None:
        db.add(RagCache(key=cache_key, value=json.dumps(payload, ensure_ascii=False),
                        created_at=datetime.now(timezone.utc).isoformat()))
    else:
        row.value, row.created_at = json.dumps(payload, ensure_ascii=False), \
            datetime.now(timezone.utc).isoformat()
    db.commit()
    return {**payload, "cached": False, "date": today}


def _echo_config(db):
    """The commentary model: the dedicated echo card when it is configured,
    otherwise the chat card. Echo can point at a different API on purpose."""
    for purpose in ("echo", "chat"):
        cfg = get_config(db, purpose)
        if cfg.enabled and (cfg.base_url or "").strip() and (cfg.model or "").strip():
            return cfg, purpose
    return None, None


def _save_echo(db, memory: Memory, text: str, answer_model: str, rel: str | None):
    """Write the commentary as a markdown memory file under the echo base dir
    (default: the memory directory, so the sync worker adopts it as a real
    memory on its next cycle). The path stays inside the base: no absolute,
    no '..'. Returns the written path."""
    base = Path(settings.echo_dir or settings.memory_dir)
    name = (rel or "").strip().removesuffix(".md")
    p = PurePosixPath(name or f"{memory.slug}-回响")
    if p.is_absolute() or not p.parts or ".." in p.parts:
        raise HTTPException(status_code=422,
                            detail="保存路径必须是 base 目录内的相对路径（不能以 / 开头或含 ..）；"
                                   "base 默认是记忆目录，可用 MSC_ECHO_DIR 更改")
    final = "/".join(p.parts)
    if final != f"{memory.slug}-回响":
        clash = db.query(Memory).filter(Memory.slug == final).first()
        if clash and clash.id != memory.id:
            raise HTTPException(status_code=422, detail="保存路径与现有记忆的 slug 冲突，请换个文件名")

    target = (base / f"{final}.md").resolve()
    if not str(target).startswith(str(base.resolve())):
        raise HTTPException(status_code=422, detail="保存路径越界")
    (target.parent).mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    return write_memory_file(
        slug=final, name=f"回响·{memory.name}",
        description=f"对记忆「{memory.slug}」的回响评注（模型 {answer_model}）",
        body=text, tags=["echo"], priority="medium", category="reference",
        lifespan="permanent", created=now, updated=now, memory_dir=base)


@router.post("/echo")
def echo(data: EchoRequest, request: Request, db: Session = Depends(get_db)):
    """A 500-800 character commentary on one memory. It runs on the echo card
    when configured (its own model API), else the chat card. Saving as a
    markdown memory file is opt-in — the Web UI turns it on, the raw API does
    not — and the user keeps the last word either way."""
    _require_auth(request, db)
    if not settings.rag_enabled:
        raise HTTPException(status_code=503, detail="RAG 已关闭（MSC_RAG_ENABLED=false）")
    if data.save:
        _guard_write()

    chat_cfg, card = _echo_config(db)
    if chat_cfg is None:
        raise HTTPException(status_code=503, detail="评注需要先配置评注（echo）或 chat 模型卡")

    memory = _resolve_memory(db, data.slug)
    context = f"{memory.body or memory.description or ''}"[: int(chat_cfg.max_input_chars or 12000)]
    messages = [
        {"role": "system", "content": "读一条记忆，写一段 500-800 字的中文评注：回到当时的情境，"
                                      "指出决定背后的取舍、与今天状态的关系、值得再确认的点。"
                                      "像写给老友的信，不是报告。直接输出正文。"},
        {"role": "user", "content": f"记忆「{memory.name}」（{memory.updated_at[:10]} 更新）：\n{context}"
                                    + (f"\n\n用户特别想听：{data.instruction}" if data.instruction else "")},
    ]
    try:
        answer = llm.chat(chat_cfg, messages, max_tokens=1600)
    except llm.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    out = {"slug": memory.slug, "name": memory.name, "body": answer["text"],
           "model": answer["model"], "card": card, "latency_ms": answer["latency_ms"]}
    if data.save:
        out["saved_to"] = str(_save_echo(db, memory, answer["text"], answer["model"],
                                         data.save_path))
    return out


@router.get("/heatmap")
def heatmap(days: int = Query(120, le=365), db: Session = Depends(get_db)):
    """Creation counts per local day, for the insights page heat strip."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = db.execute(sql_text(
        "SELECT substr(created_at, 1, 10) AS day, count(*) FROM memories "
        "WHERE created_at >= :p0 GROUP BY day ORDER BY day"), {"p0": since}).fetchall()
    return {"days": days, "counts": [{"day": day, "count": count} for day, count in rows]}


# ── Sidecar import ───────────────────────────────────────────────────

@router.post("/import")
def import_vectors(data: RagImportRequest, request: Request, db: Session = Depends(get_db)):
    """Import vectors from the msc-rag sidecar's index.db without re-embedding.

    Entries carry either a slug or a memory_id; ``vec`` is base64-encoded
    little-endian float32 (dim*4 bytes). Unknown or mismatched entries are
    skipped and counted — a partial import still saves the bulk of the work.
    """
    _require_auth(request, db)
    _guard_write()
    _require_rag()

    now = datetime.now(timezone.utc).isoformat()
    imported, skipped = 0, 0
    for entry in data.entries:
        if entry.memory_id is not None:
            memory = db.get(Memory, entry.memory_id)
        else:
            memory = db.query(Memory).filter(Memory.slug == entry.slug).first()
        if memory is None:
            skipped += 1
            continue
        try:
            blob = base64.b64decode(entry.vec, validate=True)
        except Exception:
            skipped += 1
            continue
        if entry.dim <= 0 or len(blob) != entry.dim * 4:
            skipped += 1
            continue
        row = db.get(Embedding, (memory.id, data.model))
        if row is None:
            db.add(Embedding(memory_id=memory.id, model=data.model, dim=entry.dim,
                             vec=blob, embedded_at=now))
        else:
            row.dim, row.vec, row.embedded_at = entry.dim, blob, now
        imported += 1
    db.commit()
    return {"model": data.model, "imported": imported, "skipped": skipped,
            "by": _actor(request)}
