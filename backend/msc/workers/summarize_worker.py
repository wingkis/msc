"""Daily digest worker — the scheduled half of the model card.

The card is editable from the Web UI, so the loop re-reads it every tick
instead of caching anything: turning auto-summary on must take effect without
a restart. A day is attempted once, successes and failures alike; a failed
night is retried by hand from the settings page rather than hammered every
minute while the model is offline.
"""

import time
from datetime import datetime, timezone

from msc.config import get_settings
from msc.database import SessionLocal
from msc.services import llm
from msc.services.model_config import get_config
from msc.services.summarizer import (
    done_periods, due_period, run_summary, window_memories,
)

settings = get_settings()


def run_due(now: datetime = None) -> dict | None:
    """One scheduler tick: summarise the due day, or report that nothing is due."""
    if settings.read_only:
        return None

    db = SessionLocal()
    try:
        cfg = get_config(db)
        if not cfg.enabled or not cfg.auto_enabled:
            return None
        if not (cfg.base_url or "").strip() or not (cfg.model or "").strip():
            return None

        period = due_period(cfg, now or datetime.now(timezone.utc), set(done_periods(db)))
        if not period:
            return None

        memories, retired = window_memories(db, cfg, period)
        print(f"Auto digest: {period} ({len(memories)} memories, {len(retired)} contradicted)")
        # An automated digest has nobody watching its response, so the only
        # place it can say "I left these out" is inside itself.
        note = f"范围内另有 {len(retired)} 条已被推翻的记忆，未纳入本小结。" if retired else ""
        return run_summary(
            db, cfg, memories, actor="auto-summary", kind="auto", period=period,
            date_range=period, note=note,
        )
    except llm.LLMError as exc:
        print(f"Auto digest failed: {exc}")
        return {"error": str(exc)}
    except Exception as exc:  # a bad day must not take the loop down
        print(f"Auto digest error: {exc}")
        return {"error": str(exc)}
    finally:
        db.close()


def start_summarize_worker(interval: int = 60) -> None:
    print(f"Digest worker started (check every {interval}s, default time 23:30 local)")
    while True:
        try:
            result = run_due()
            if result and result.get("error"):
                pass  # already printed
            elif result and result.get("created"):
                print(f"Digest written: {result['created']['slug']}")
        except Exception as exc:
            print(f"Digest worker error: {exc}")
        time.sleep(interval)
