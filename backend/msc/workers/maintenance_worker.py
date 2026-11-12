"""Maintenance worker — the scheduled half of re-checking.

Re-checking had an entry point (``GET /preflight``, ``cli/msc-verify.sh``) but
no trigger: it happened exactly when an agent thought of it, which for a library
of memories is nearly never. This loop supplies the one judgement that needs no
intelligence at all — "nobody has vouched for this memory in N days" — and
leaves every judgement that does need one (contradicted, restored) to a probe or
a human.

Idempotent by construction, so it needs no "did I already run today" bookkeeping
like the digest does: only ``active`` rows past the cutoff are stamped, and a
row that just became ``stale`` no longer matches. Set ``MSC_STALE_AFTER_DAYS=0``
to switch the whole thing off.
"""

import time

from msc.config import get_settings
from msc.database import SessionLocal
from msc.services import freshness

settings = get_settings()

# Stamped per sweep. A first run on an old library could touch hundreds of rows
# at once — writing hundreds of .md files in one tick — so the backlog drains
# over a few ticks instead of stalling the loop.
SWEEP_LIMIT = 200


def run_due(now=None) -> dict | None:
    """One tick: stamp aged memories stale, or report that nothing is due."""
    if settings.read_only:
        return None
    days = settings.stale_after_days
    if days <= 0:
        return None

    db = SessionLocal()
    try:
        result = freshness.apply_decay(db, days=days, limit=SWEEP_LIMIT,
                                       actor="decay-worker", now=now, apply=True)
        return result if result.get("count") else None
    except Exception as exc:  # a bad sweep must not take the loop down
        print(f"Decay error: {exc}")
        return {"error": str(exc), "count": 1}
    finally:
        db.close()


def start_maintenance_worker(interval: int = None) -> None:
    interval = max(60, interval or settings.decay_interval_seconds)
    days = settings.stale_after_days
    state = f"{days} days" if days > 0 else "disabled (MSC_STALE_AFTER_DAYS=0)"
    print(f"Decay worker started (check every {interval}s, threshold {state})")
    while True:
        try:
            result = run_due()
            if result and result.get("error"):
                pass  # already printed
            elif result:
                print(f"Decay: {result['count']} memory/memories -> stale "
                      f"({', '.join(result['slugs'][:5])}{' …' if result['count'] > 5 else ''})")
        except Exception as exc:
            print(f"Decay worker error: {exc}")
        time.sleep(interval)
