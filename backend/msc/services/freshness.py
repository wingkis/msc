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

import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import or_

from msc.models import Memory, MemoryReview, Tag
from msc.schemas import parse_refs

# Injection order of the hook: a critical memory nobody has re-checked in a year
# is a bigger problem than a low-priority one that is merely old.
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

ACTIVE_STATUSES = (None, "", "active")

# What the automatic half is allowed to touch. Two kinds of memory are out of
# scope, and both are out for a *stated* reason that gets counted, never for a
# silent one:
#
#   no refs    — "nobody re-checked this" is an empty complaint when there is
#                nothing to re-check. Those memories go to a separate one-time
#                bucket ("go attach a handle"), and they never age.
#   flow       — session turn/sess records, digests and echoes have their own
#                The shape list covers the legacy hook naming too
#                (`session-summary-<date>`, v2 / v3.4-era): a machine-written
#                digest is flow whether or not a human later attached refs to
#                it — the rule asks who maintains it, not who last touched it.
#                lifecycle (consolidate → archive) and are regenerated, not
#                re-verified. Note the rule is tag + the auto-generated slug
#                shapes, NOT the loose `summary` tag: msc-2-1-1-upgrade-summary
#                carries that tag and is durable knowledge people quote.
FLOW_TAGS = {"turn", "echo"}
_FLOW_SLUG = re.compile(r"(?:^|-)(?:turn|sess)-\d|^(?:daily-)?summary-\d"
                  r"|^session-summary-\d")
NO_REVIEW_TAG = "no-review"


def _tags_of(memory: Memory) -> set:
    try:
        return {tag.name for tag in memory.tags}
    except Exception:      # a detached instance in a long-running sweep
        return set()


def is_flow(memory: Memory) -> bool:
    """会话流水 / 自动小结 / 回响：有自己的生命周期，不是复核的对象。"""
    tags = _tags_of(memory)
    if tags & FLOW_TAGS:
        return True
    return bool(_FLOW_SLUG.search(memory.slug or ""))


def is_exempt(memory: Memory) -> bool:
    """A recorded human exemption: 「这条不需要复核」是一个决定，不是一个 0。"""
    return NO_REVIEW_TAG in _tags_of(memory)


def has_refs(memory: Memory) -> bool:
    return bool(parse_refs(memory.refs))


def scope_of(memory: Memory) -> str:
    """checkable | unanchored | skipped_flow | skipped_no_review."""
    if is_flow(memory):
        return "skipped_flow"
    if is_exempt(memory):
        return "skipped_no_review"
    return "checkable" if has_refs(memory) else "unanchored"


def _has_refs_pred():
    """SQL mirror of has_refs() — the same predicate idx_memories_refs_live serves."""
    return Memory.refs.isnot(None), Memory.refs != "", Memory.refs != "[]"


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
    """Ref-bearing, non-flow memories whose last confirmation predates ``days``.

    The SQL filter is a cheap necessary condition, not the answer: a memory
    verified after the cutoff cannot be due whatever its other stamps say, so
    the string comparison is a safe narrowing pass. The exact judgement
    (``verified_at``, else ``created_at``) happens below on parsed timestamps,
    where the mixed ISO formats in this database cannot lie to a string compare.

    Two exclusions live here rather than in the caller because decay is the one
    automatic write in this module and must be narrow at its source: rows with
    no refs (nothing to re-check) and flow/exempt rows (their lifecycle is
    consolidate→archive, not re-verify).
    """
    if days <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    rows = db.query(Memory).filter(
        Memory.archived == 0,
        _active_pred(),
        # Decay needs a handle to be an honest complaint. A memory with no refs
        # is not "unconfirmed", it is "unconfirmable": aging it would stamp a
        # verdict about the reviewer's inconvenience. Those rows belong in the
        # one-time "attach a handle" bucket, and they stay active until someone does.
        *_has_refs_pred(),
        # Necessary condition, index-free but cheap: a memory verified after the
        # cutoff cannot be due, whatever its other stamps say. The exact
        # judgement (verified → created) happens below on parsed timestamps,
        # where mixed ISO formats cannot lie to a string comparison.
        or_(Memory.verified_at.is_(None), Memory.verified_at == "",
            Memory.verified_at < cutoff.isoformat()),
    ).all()
    aged = []
    for memory in rows:
        if is_flow(memory) or is_exempt(memory):
            continue      # 流水与人工豁免：有记录地跳过，不是悄悄不见
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
    scope = scope_of(memory)
    if scope.startswith("skipped"):
        return []
    status = live_status(memory)
    kind, moment = last_confirmed(memory)
    if status == "contradicted":
        out.append("已被推翻：等复核复位或被新记忆取代")
    elif status == "stale":
        out.append("已降为 stale：复核后 confirm 或推翻，别让它悬着")
    # Age reasons only fire past the threshold, so a library does not read as
    # "everything is due" the day it is created; structural reasons (no refs,
    # contradicted and waiting) always show, because they are fixable today.
    aged = (days > 0 and has_refs(memory) and moment is not None
            and (now - moment) >= timedelta(days=days))
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
    if not has_refs(memory):
        out.append("没挂出处：复核时无从下手（一次性补把手，不是每次都要来看）")
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
    scope = scope_of(memory)
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
        "bucket": scope,
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
                 tag: Optional[str] = None, only: Optional[str] = None) -> dict:
    """Read-only queue: what deserves a re-check next, and why.

    Nothing here writes. A clock may make a memory suspicious; only a probe or a
    human may make it contradicted.

    Two buckets, both counted, neither silent: ``checkable`` (has refs, so a
    re-check is possible) and ``unanchored`` (no refs, so the work is to attach a
    handle once — not to be reminded forever). Session flow, digests and
    ``no-review`` rows are out of scope and reported as such, which is why the
    response carries a census: a queue of 2 next to a library of 116 has to say
    where the other 114 went.
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

    due = []
    counts = {"checkable": 0, "unanchored": 0, "skipped_flow": 0,
              "skipped_no_review": 0, "not_due": 0}
    for memory in rows:
        scope = scope_of(memory)
        if scope.startswith("skipped"):
            counts[scope] += 1
            continue
        # Judged without the `only` slice on purpose: a filter changes what is
        # listed, never what the library contains. A census that shrinks when
        # you ask for a narrower view is the same lie as an empty search index.
        row = queue_item(memory, days, now)
        if row:
            counts[scope] += 1
            due.append(row)
        else:
            counts["not_due"] += 1
    due.sort(key=sort_key)
    items = [row for row in due
             if not only or only == "all" or row["bucket"] == only][:limit]
    return {"stale_after_days": days, "only": only or "all", "total": len(due),
            "listed": len(items), "counts": counts, "items": items}


def apply_decay(db, days: int, limit: int = 200, actor: str = "decay-worker",
                now: datetime = None, apply: bool = True) -> dict:
    """Stamp aged ``active`` memories ``stale`` — or just report them.

    Dry-run is the default at the API boundary; the worker passes apply=True
    deliberately. The operation is idempotent by construction: a ``stale`` row
    no longer matches the active predicate, so running it every hour costs one
    indexed query and writes only the rows that just crossed the line.

    Scope comes from ``aged_memories``: ref-bearing, non-flow, non-exempt rows
    only. Old and hand-written memories without a handle are never stamped, and
    that is a stated exemption in the queue census, not a silent omission.
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
