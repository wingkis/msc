"""RAG glue: dirty queue, cosine scan, RRF fusion, tunable settings.

Cosine is a full scan over the embeddings table with numpy — at 10k x 1024
float32 that is ~40 MB and ~0.05 s per query, far below the FTS and model
latencies around it. Beyond ~50k vectors a sqlite-vec style extension can be
swapped in behind scan_scores() without touching callers.
"""

import array
import logging
from datetime import datetime, timezone

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from msc.config import get_settings
from msc.models import Embedding, Memory, RagDirty, RagSetting

_log = logging.getLogger("msc.rag")

TUNABLES = ("recall_top", "rerank_topn", "rrf_k", "embed_batch")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def setting(db: Session, key: str) -> int:
    """DB override first, .env default second."""
    settings = get_settings()
    row = db.query(RagSetting).filter(RagSetting.key == key).first()
    if row:
        try:
            return int(row.value)
        except ValueError:
            _log.warning("rag_settings.%s is not an integer: %r", key, row.value)
    return {"recall_top": settings.rag_recall_top, "rerank_topn": settings.rag_rerank_topn,
            "rrf_k": settings.rag_rrf_k, "embed_batch": settings.rag_embed_batch}[key]


def set_setting(db: Session, key: str, value: int) -> None:
    row = db.query(RagSetting).filter(RagSetting.key == key).first()
    if row is None:
        row = RagSetting(key=key, value=str(int(value)), updated_at=_now())
        db.add(row)
    else:
        row.value = str(int(value))
        row.updated_at = _now()
    db.commit()


def mark_dirty(db: Session, memory_id: int) -> None:
    """Queue one memory for (re)embedding.

    Never raises: RAG is an enhancement, a broken queue row must not turn a
    successful memory write into a 500. The rag_worker also re-embeds anything
    without a current vector, so a lost mark self-heals.
    """
    try:
        db.execute(sql_text("INSERT OR IGNORE INTO rag_dirty (memory_id, marked_at) "
                            "VALUES (:p0, :p1)"), {"p0": memory_id, "p1": _now()})
        db.commit()
    except Exception as exc:
        db.rollback()
        _log.warning("could not mark memory %s dirty: %s", memory_id, exc)


def mark_all_dirty(db: Session) -> int:
    """Queue every live memory — the reindex button."""
    ids = [row[0] for row in db.query(Memory.id).filter(Memory.archived == 0).all()]
    for memory_id in ids:
        db.execute(sql_text("INSERT OR IGNORE INTO rag_dirty (memory_id, marked_at) "
                            "VALUES (:p0, :p1)"), {"p0": memory_id, "p1": _now()})
    db.commit()
    return len(ids)


def embed_text(memory: Memory, max_chars: int) -> str:
    """What gets embedded: title and description carry the topic, the body the
    detail. Trimming keeps one memory inside the provider's context window."""
    parts = [memory.name or ""]
    if memory.description:
        parts.append(memory.description)
    if memory.body:
        parts.append(memory.body)
    joined = "\n".join(p for p in parts if p).strip()
    return joined[:max_chars] if max_chars > 0 else joined


def pack_vec(vec) -> bytes:
    return array.array("f", [float(x) for x in vec]).tobytes()


def unpack_vec(blob: bytes) -> list:
    arr = array.array("f")
    arr.frombytes(blob)
    return arr.tolist()


def vector_counts(db: Session) -> dict:
    """How many vectors each model holds."""
    rows = db.query(Embedding.model, sql_text("count(*)")).group_by(Embedding.model).all()
    return {model: count for model, count in rows}


def dirty_depth(db: Session) -> int:
    return db.query(RagDirty).count()


def scan_vector_table(db: Session, table, id_col, model_col, query_vec,
                      model: str, top: int, live_ids: set):
    """Cosine over one vector table's rows for `model`, restricted to live ids.

    Shared by memories (Embedding) and photos (PicVector): the math is
    identical, only the corpus differs — and keeping the tables separate is
    what stops the two searches from seeing each other.
    """
    try:
        import numpy
    except ImportError:  # pragma: no cover - numpy is a declared dependency
        _log.warning("numpy missing; semantic search degraded to empty")
        return []

    q = numpy.asarray(query_vec, dtype=numpy.float32)
    qn = float(numpy.linalg.norm(q))
    if qn == 0.0:
        return []

    rows = db.query(id_col, table.vec, table.dim).filter(model_col == model).all()
    if not rows:
        return []

    matrix = numpy.empty((len(rows), len(q)), dtype=numpy.float32)
    valid = []
    for obj_id, blob, dim in rows:
        if obj_id not in live_ids or dim != len(q) or len(blob) != dim * 4:
            continue  # model switch mid-scan or truncated blob: skip, reindex fixes
        matrix[len(valid)] = numpy.frombuffer(blob, dtype=numpy.float32)
        valid.append(obj_id)
    if not valid:
        return []
    matrix = matrix[:len(valid)]
    norms = numpy.linalg.norm(matrix, axis=1)
    scores = matrix @ q / numpy.where(norms == 0, 1.0, norms) / qn
    order = numpy.argsort(-scores)[:top]
    return [(valid[i], float(scores[i])) for i in order if scores[i] > 0.0]


def scan_scores(db: Session, query_vec, model: str, top: int,
                include_archived: bool = False):
    """Cosine similarity of query_vec against every stored vector for `model`.

    Returns [(memory_id, cos)] sorted best-first. Numpy import is inside the
    function so a missing dependency degrades search, not boot.
    """
    live = {row[0] for row in (
        db.query(Memory.id).filter(Memory.archived == 0).all()
        if not include_archived else db.query(Memory.id).all())}
    return scan_vector_table(db, Embedding, Embedding.memory_id, Embedding.model,
                             query_vec, model, top, live)


def embed_query(db: Session, q: str):
    """Embed a query string with the active embed card.

    Returns (vector, model_name, error). A missing provider is not an error
    worth 500-ing over — callers degrade to keyword search — but the error
    text is kept so /rag/search can say why.
    """
    from msc.services import rag_providers
    from msc.services.llm import LLMError
    from msc.services.model_config import get_config

    cfg = get_config(db, "embed")
    if not cfg.enabled or not (cfg.base_url or "").strip() or not (cfg.model or "").strip():
        return None, "", None
    try:
        vec = rag_providers.embed(cfg, [q])[0]
    except LLMError as exc:
        return None, "", str(exc)
    return vec, cfg.model.strip(), None


def semantic_ids(db: Session, q: str, top: int, include_archived: bool = False):
    """Ranked memory ids for a semantic pass. Returns (ids, cos_by_id, error)."""
    vec, model, error = embed_query(db, q)
    if vec is None:
        return [], {}, error
    scored = scan_scores(db, vec, model, top, include_archived=include_archived)
    return [memory_id for memory_id, _ in scored], dict(scored), None


def rrf_fusion(keyword_ids, semantic_ids, rrf_k: int = 60):
    """Reciprocal Rank Fusion of two ranked id lists.

    score(id) = sum over lists of 1/(k + rank). Returns [(id, score, match)]
    where match is keyword | semantic | both — the badge the UI shows.
    """
    fused: dict[int, list] = {}
    for rank, memory_id in enumerate(keyword_ids):
        entry = fused.setdefault(memory_id, [0.0, "keyword"])
        entry[0] += 1.0 / (rrf_k + rank + 1)
    for rank, memory_id in enumerate(semantic_ids):
        entry = fused.setdefault(memory_id, [0.0, "semantic"])
        if entry[1] == "keyword":
            entry[1] = "both"
            entry[0] += 1.0 / (rrf_k + rank + 1)
        else:
            entry[0] += 1.0 / (rrf_k + rank + 1)
    ranked = sorted(fused.items(), key=lambda kv: -kv[1][0])
    return [(memory_id, round(score, 6), match) for memory_id, (score, match) in ranked]
