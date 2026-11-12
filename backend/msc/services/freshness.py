"""Time decay and the re-check queue — the engine a library needs to self-maintain.

``refs`` records what a memory was based on and ``status`` records what a
re-check concluded. Neither answers the most common question about an old
memory: *nobody has looked at this in a long time*. ``verified_at`` was added
for exactly that question and then written but never read, so decay had no
automatic path and a belief could sit at ``active`` forever purely by nobody
bothering.

Two rules keep this honest:

* Decay only ever downgrades ``active`` → ``stale``. It never contradicts
  (that needs a reason a reader can check) and it never restores anything —
  recovery stays an act of re-checking, not of a clock noticing.
* ``stale`` is deliberately still recallable and still listed. Time passing is
  suspicion, not evidence. ``contradicted`` is the only status that hides.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import or_

from msc.models import Memory, MemoryReview, Tag
from msc.schemas import parse_refs

# Injection order of the hook: a critical memory nobody has re-checked in a year
# is a bigger problem than a low-priority one that is merely old.
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

ACTIVE_STATUSES = (None, "", "active")


def parse_ts(value) -> Optional[datetime]:
    """ISO string → aware datetime, or None. Never raises on a weird row.

    Timestamps in this database came from several writers over several
    versions: ``Z`` suffixes, ``+00:00`` offsets, naive strings, and the odd
    space instead of a ``T``. A decay job that crashes on one bad row would
    stop maintaining every other row, so unreadable means "unknown age".
    """
    if not value:
        return None
    text = str(value).strip().replace(" ", "T", 1) if " " in str(value) else str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            moment = datetime.strptime(str(value).strip()[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def last_confirmed(memory: Memory) -> Tuple[str, Optional[datetime]]:
    """(how, when) — the last moment this memory was actually vouched for.

    ``updated_at`` is deliberately NOT one of the sources, and that is the whole
    point. Editing a body is not re-checking it (the rule already enforced for
    restoring a contradicted memory), and the decay stamp itself moves
    ``updated_at`` — so a clock that measured "how long since anyone touched
    this" would reset its own timer on every sweep and a typo would buy a belief
    another 90 days of unexamined confidence. The clock therefore measures from
    the last real act of confirmation: a ``verify``, or the original write when
    nobody has looked since.
    """
    for kind, value in (("verified", memory.verified_at), ("created", memory.created_at)):
        moment = parse_ts(value)
        if moment:
            return kind, moment
    return "unknown", None


def live_status(memory: Memory) -> str:
    return memory.status or "active"


def _active_pred():
    """NULL-safe "still believed", the mirror image of contradicted_ids."""
    return or_(Memory.status.is_(None), Memory.status == "", Memory.status == "active")


def aged_memories(db, days: int, now: datetime = None, limit: int = 500) -> List[Memory]:
    """Active memories whose last confirmation is older than ``days``.

    The SQL filter on ``updated_at`` is a cheap necessary condition, not the
    answer: the last confirmation is the *newest* of three stamps, so a row
    with an old ``updated_at`` can still be young, and a row whose
    ``updated_at`` is past the cutoff cannot be a candidate at all. That makes
    the index-backed string comparison a safe narrowing pass; the real
    judgement happens in Python on parsed timestamps, where mixed ISO formats
    cannot lie.
    """
    if days <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    rows = db.query(Memory).filter(
        Memory.archived == 0,
        _active_pred(),
        # Necessary condition, index-free but cheap: a memory verified after the
        # cutoff cannot be due, whatever its other stamps say. The exact
        # judgement (verified → created) happens below on parsed timestamps,
        # where mixed ISO formats cannot lie to a string comparison.
        or_(Memory.verified_at.is_(None), Memory.verified_at == "",
            Memory.verified_at < cutoff.isoformat()),
    ).all()
    aged = []
    for memory in rows:
        _kind, moment = last_confirmed(memory)
        if moment is None or moment < cutoff:
            aged.append(memory)
    aged.sort(key=lambda m: (PRIORITY_RANK.get(m.priority, 4),
                             (last_confirmed(m)[1] or datetime.min.replace(tzinfo=timezone.utc))))
    return aged[:limit]


def reasons_for(memory: Memory, days: int, now: datetime = None) -> List[str]:
    """Why this memory belongs in the queue — in plain words, because a queue
    of bare numbers gets ignored."""
    now = now or datetime.now(timezone.utc)
    out: List[str] = []
    status = live_status(memory)
    kind, moment = last_confirmed(memory)
    if status == "contradicted":
        out.append("已被推翻：等复核复位或被新记忆取代")
    elif status == "stale":
        out.append("已降为 stale：复核后 confirm 或推翻，别让它悬着")
    # Age reasons only fire past the threshold, so a library does not read as
    # "everything is due" the day it is created; structural reasons (no refs,
    # contradicted and waiting) always show, because they are fixable today.
    aged = days > 0 and moment is not None and (now - moment) >= timedelta(days=days)
    if moment is None:
        out.append("没有任何可信时间戳：写下多久了都不知道")
    elif aged and kind != "verified":
        out.append(f"自写下（{moment.date()}）以来没人复核过 {(now - moment).days} 天")
    elif aged:
        out.append(f"上次复核已过去 {(now - moment).days} 天")
    edited = parse_ts(memory.updated_at)
    if edited and moment and edited > moment + timedelta(days=max(1, days // 4)):
        # Rewritten long after the last check: the prose moved, the evidence did not.
        out.append(f"复核之后正文又改过（最后动笔 {edited.date()}）")
    if not parse_refs(memory.refs):
        out.append("没挂出处：复核时无从下手")
    return out


def queue_item(memory: Memory, days: int, now: datetime = None) -> Optional[dict]:
    """One queue row, or None when nothing about this memory needs attention.

    Shared by the read-only queue and the decay sweep so "what is due" and
    "what just got stamped stale" cannot drift into two different definitions.
    """
    now = now or datetime.now(timezone.utc)
    why = reasons_for(memory, days, now)
    if not why:
        return None
    kind, moment = last_confirmed(memory)
    return {
        "slug": memory.slug,
        "name": memory.name,
        "priority": memory.priority,
        "category": memory.category,
        "status": live_status(memory),
        "refs": parse_refs(memory.refs),
        "last_confirmed": kind,
        "last_confirmed_at": moment.isoformat() if moment else None,
        "last_edited_at": (parse_ts(memory.updated_at) or moment).isoformat()
                          if (parse_ts(memory.updated_at) or moment) else None,
        "age_days": (now - moment).days if moment else None,
        "reasons": why,
    }


def sort_key(item: dict):
    """Priority first, then the oldest, then slug so two runs agree on order.

    A critical memory nobody has looked at for a year outranks a low-priority
    note that is merely ancient. Unknown age sorts first on purpose: a memory
    with no readable timestamp is the kind of thing a re-check exists to find.
    """
    return (PRIORITY_RANK.get(item["priority"], 4),
            -(item["age_days"] if item["age_days"] is not None else 10**6),
            item["slug"])


def review_queue(db, days: int = 90, limit: int = 50, now: datetime = None,
                 priority: Optional[str] = None, category: Optional[str] = None,
                 tag: Optional[str] = None) -> dict:
    """Read-only queue: what deserves a re-check next, and why.

    Nothing here writes. A clock may make a memory suspicious; only a probe or a
    human may make it contradicted.
    """
    from msc.text import normalize_tags

    now = now or datetime.now(timezone.utc)
    query = db.query(Memory).filter(Memory.archived == 0)
    if priority:
        query = query.filter(Memory.priority == priority)
    if category:
        query = query.filter(Memory.category == category)
    if tag:
        query = query.join(Memory.tags).filter(Tag.name.in_(normalize_tags(tag)))
    rows = query.all()

    items = [row for row in (queue_item(memory, days, now) for memory in rows) if row]
    items.sort(key=sort_key)
    return {"stale_after_days": days, "total": len(items), "items": items[:limit]}


def apply_decay(db, days: int, limit: int = 200, actor: str = "decay-worker",
                now: datetime = None, apply: bool = True) -> dict:
    """Stamp aged ``active`` memories ``stale`` — or just report them.

    Dry-run is the default at the API boundary; the worker passes apply=True
    deliberately. The operation is idempotent by construction: a ``stale`` row
    no longer matches the active predicate, so running it every hour costs one
    indexed query and writes only the rows that just crossed the line.
    """
    now = now or datetime.now(timezone.utc)
    candidates = aged_memories(db, days, now=now, limit=limit)
    stamps = []
    for memory in candidates:
        # aged_memories already checked the age, so the item always exists.
        stamps.append(queue_item(memory, days, now) or {
            "slug": memory.slug, "name": memory.name, "priority": memory.priority,
            "category": memory.category, "status": live_status(memory),
            "refs": parse_refs(memory.refs), "last_confirmed": "unknown",
            "last_confirmed_at": None, "last_edited_at": None, "age_days": None,
            "reasons": [f"超过 {days} 天没有复核"],
        })
        if not apply:
            continue
        iso = now.isoformat()
        db.add(MemoryReview(
            memory_id=memory.id, action="decay", from_status=live_status(memory),
            to_status="stale", reason="; ".join(stamps[-1]["reasons"])[:2000],
            actor=actor, created_at=iso,
        ))
        memory.status = "stale"
        memory.updated_at = iso  # the file channel and incremental pulls must see it
    if apply and candidates:
        db.commit()
        # Mirror to the markdown copy, or the file side keeps reading as active
        # and the next import would "restore" a status the library has already
        # downgraded. Same reason the digest writes through.
        from msc.filesystem.write_through import write_through

        for memory in candidates:
            write_through(db, memory)
    stamps.sort(key=sort_key)
    return {"stale_after_days": days, "dry_run": not apply, "count": len(candidates),
            "slugs": [row["slug"] for row in stamps], "items": stamps}
