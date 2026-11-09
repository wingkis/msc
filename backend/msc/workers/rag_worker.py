"""Embedding worker — consumes the rag_dirty queue.

Same daemon-thread shape as the other workers, and it re-reads the provider
card every tick so configuring an embed endpoint from the Web UI takes effect
without a restart. When no embed provider is configured the marks simply sit
in the queue: enabling RAG later backfills everything automatically, and a
provider outage retries instead of losing work.

Batches of 16 are what the msc-rag sidecar proved out over a night of real
traffic on llama-server embedders.
"""

import time
from datetime import datetime, timezone

from sqlalchemy import text as sql_text

from msc.config import get_settings
from msc.database import SessionLocal
from msc.models import Embedding, Memory, RagDirty
from msc.services import rag_providers
from msc.services.llm import LLMError
from msc.services.model_config import get_config, note_result
from msc.services.rag import embed_text, pack_vec, setting

settings = get_settings()


def run_once() -> dict | None:
    """One pass over the queue. Returns a report, or None if there was nothing
    to do (no provider, RAG off, or the queue empty)."""
    if settings.read_only or not settings.rag_enabled:
        return None

    db = SessionLocal()
    try:
        cfg = get_config(db, "embed")
        if not cfg.enabled or not (cfg.base_url or "").strip() or not (cfg.model or "").strip():
            return None  # keep the queue; configuring the card backfills later

        batch_size = max(1, setting(db, "embed_batch"))
        max_chars = int(cfg.max_input_chars or 4000)
        model_name = cfg.model.strip()

        # Marks for archived or hard-deleted memories can never be embedded;
        # drop them so the queue can actually reach zero. FK cascades are off
        # on these connections, and hard deletes remove no vectors either, so
        # embeddings for purged memories are reaped here too.
        db.execute(sql_text(
            "DELETE FROM rag_dirty WHERE memory_id NOT IN "
            "(SELECT id FROM memories WHERE archived = 0)"))
        db.execute(sql_text(
            "DELETE FROM embeddings WHERE memory_id NOT IN (SELECT id FROM memories)"))

        # The self-heal the docstrings promise: anything live without a vector
        # for the active model gets queued — pre-existing memories on upgrade,
        # lost marks, or a provider that was configured after the fact.
        db.execute(sql_text(
            "INSERT OR IGNORE INTO rag_dirty (memory_id, marked_at) "
            "SELECT id, :p1 FROM memories WHERE archived = 0 AND id NOT IN "
            "(SELECT memory_id FROM embeddings WHERE model = :p0)"),
            {"p0": model_name, "p1": datetime.now(timezone.utc).isoformat()})
        db.commit()

        report = {"embedded": 0, "skipped": 0, "errors": []}
        while True:
            rows = (db.query(RagDirty, Memory)
                    .join(Memory, Memory.id == RagDirty.memory_id)
                    .filter(Memory.archived == 0)
                    .order_by(RagDirty.marked_at)
                    .limit(batch_size).all())
            if not rows:
                break

            # An empty memory text (body deleted, name blank) is a permanent
            # skip: drop the mark now so it cannot spin in the queue forever.
            pairs, texts = [], []
            for dirty, memory in rows:
                text = embed_text(memory, max_chars)
                if text:
                    pairs.append((dirty, memory))
                    texts.append(text)
                else:
                    db.delete(dirty)
                    report["skipped"] += 1
            db.commit()
            if not texts:
                continue

            try:
                vectors = rag_providers.embed(cfg, texts)
            except LLMError as exc:
                # Whole batch failed (endpoint down, model name wrong): stop
                # the cycle, retry on the next tick, keep every mark queued.
                db.rollback()
                note_result(db, cfg, error=str(exc))
                report["errors"].append(str(exc))
                return report

            now = datetime.now(timezone.utc).isoformat()
            for (dirty, memory), vec in zip(pairs, vectors):
                payload = pack_vec(vec)
                row = db.get(Embedding, (memory.id, model_name))
                if row is None:
                    db.add(Embedding(memory_id=memory.id, model=model_name,
                                     dim=len(vec), vec=payload, embedded_at=now))
                else:
                    row.dim, row.vec, row.embedded_at = len(vec), payload, now
                db.delete(dirty)
                report["embedded"] += 1
            db.commit()
            note_result(db, cfg)

        return report if (report["embedded"] or report["skipped"] or report["errors"]) else None
    except Exception as exc:  # the loop must survive anything
        db.rollback()
        print(f"RAG worker error: {exc}")
        return {"error": str(exc)}
    finally:
        db.close()


def start_rag_worker(interval: int = 15) -> None:
    print(f"RAG worker started (queue drain every {interval}s)")
    while True:
        try:
            result = run_once()
            if result and (result.get("embedded") or result.get("errors")):
                print(f"RAG: embedded {result.get('embedded', 0)}, "
                      f"skipped {result.get('skipped', 0)}"
                      + (f", errors: {result['errors'][:2]}" if result.get("errors") else ""))
        except Exception as exc:
            print(f"RAG worker error: {exc}")
        time.sleep(interval)
