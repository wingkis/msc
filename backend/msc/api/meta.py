"""Self-describing endpoints — everything an agent needs to use this service
without a human reading a README to it first."""

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from msc.api.memories import CATEGORIES, LIFESPANS, PRIORITIES
from msc.auth.api_key import auth_status
from msc.config import VERSION, get_settings
from msc.database import get_db
from msc.models import Memory, MemoryBranch, MemoryTag, Tag
from msc.schemas import MEMORY_STATUSES, REF_KINDS

settings = get_settings()
router = APIRouter()


def _tag_counts(db: Session) -> list:
    """Tag name -> number of memories using it."""
    return (db.query(Tag.name, func.count(MemoryTag.memory_id))
            .outerjoin(MemoryTag, MemoryTag.tag_id == Tag.id)
            .group_by(Tag.name)
            .order_by(func.count(MemoryTag.memory_id).desc(), Tag.name)
            .all())


def _counts(db: Session) -> dict:
    return {
        "total": db.query(Memory).filter(Memory.archived == 0).count(),
        "archived": db.query(Memory).filter(Memory.archived == 1).count(),
        # How much of the library a re-check has already contradicted. Worth
        # stating: a corpus that never accumulates contradictions is a corpus
        # nobody is checking, not a corpus that happens to be correct.
        "contradicted": db.query(Memory).filter(
            Memory.archived == 0, Memory.status == "contradicted").count(),
        # Aged out by the decay worker: still believed, but nobody has vouched
        # for it recently. Reported separately from contradicted because the two
        # deserve different work — one needs a re-check, the other a successor.
        "stale": db.query(Memory).filter(
            Memory.archived == 0, Memory.status == "stale").count(),
        # Nothing written here has ever been re-checked. The number an agent
        # should read before trusting "the library agrees with reality".
        "never_verified": db.query(Memory).filter(
            Memory.archived == 0,
            or_(Memory.verified_at.is_(None), Memory.verified_at == "")).count(),
        "no_refs": db.query(Memory).filter(
            Memory.archived == 0,
            or_(Memory.refs.is_(None), Memory.refs == "", Memory.refs == "[]")).count(),
        "decay": {"stale_after_days": settings.stale_after_days,
                  "enabled": settings.stale_after_days > 0},
        "by_category": {r[0]: r[1] for r in db.query(Memory.category, func.count("*"))
                        .filter(Memory.archived == 0).group_by(Memory.category).all()},
        "by_priority": {r[0]: r[1] for r in db.query(Memory.priority, func.count("*"))
                        .filter(Memory.archived == 0).group_by(Memory.priority).all()},
    }


@router.get("/meta")
def meta(db: Session = Depends(get_db)):
    """Machine-readable description of the service: mode, vocab, tags, counts.

    An agent can call this once and then write data that the schema will
    actually accept.
    """
    tags = _tag_counts(db)

    return {
        "service": "msc",
        "version": VERSION,
        **auth_status(),
        "read_only": settings.read_only,
        "base_url": settings.public_url or f"http://{settings.bind_host}:{settings.bind_port}",
        "vocab": {
            "category": list(CATEGORIES),
            "priority": list(PRIORITIES),
            "lifespan": list(LIFESPANS),
            "status": list(MEMORY_STATUSES),
            "ref_kind": list(REF_KINDS),
        },
        "branches": [b.name for b in db.query(MemoryBranch).order_by(MemoryBranch.name).all()],
        "tags": [{"name": name, "count": count} for name, count in tags],
        **_counts(db),
    }


@router.get("/tags")
def list_tags(db: Session = Depends(get_db)):
    """Tag names with usage counts."""
    return [{"name": name, "count": count} for name, count in _tag_counts(db)]


HELP = """MSC — memory service for LAN agents. No login, no token, nothing expires.

Base: {base}/api/v1

READ
  GET  /memories                      list (filters: category, priority, tag, branch,
                                      since=<ISO>, q=<text>, include_archived, order,
                                      limit<=500, offset, include_invalidated,
                                      ref=<kind>:<target> (impact lookup: which memories
                                      cite one endpoint/path/commit);
                                      X-Total-Count + X-Contradicted-Hidden headers)
  GET  /memories/{{slug}}             one memory; slug, exact name, or unique fragment
  GET  /memories/preflight            re-check stored refs (budget_ms<=8000, limit<=100,
                                      category/priority/tag filters). Reports verdicts
                                      only — it never writes. Server can reach url:/file:/
                                      mem:; git:/cmd:/host:/ip: come back "unchecked"
  GET  /memories/review-due           what deserves a re-check next, ranked by priority
                                      then age, each item saying why. Two buckets, both
                                      counted: bucket=checkable (has refs -> re-check it)
                                      and bucket=unanchored (no refs -> attach a handle
                                      once, it is not a recurring errand). ?only=checkable
                                      |unanchored, ?limit<=500, ?stale_after_days=N.
                                      The response also carries a census (counts):
                                      skipped_flow / skipped_no_review / not_due — so a
                                      short queue never reads as an empty library.
                                      Read-only: age makes a memory suspicious, never wrong
                                      GET  /search?q=...                  full-text (safe with any characters) + filters
                                      mode=keyword|semantic|hybrid (default keyword;
                                      semantic modes need an embed provider, otherwise
                                      they degrade to keyword; hybrid results carry
                                      a "match" field); include_invalidated to also
                                      recall contradicted ones.
                                      X-Search-Status: ok|no_match|degraded|error —
                                      a broken index is not the same answer as
                                      "nothing matched".
  GET  /meta                          vocabularies, tags, counts
  GET  /tags                          tag names + usage counts
  GET  /stats                         counts by category/priority/source
  GET  /memories/graph                relation graph (?max_degree=N caps edges per node, 0 = all;
                                      ?relations=a,b shows only those relation types; response
                                      carries edges_by_type so you know what is what)
  GET  /versions/{{slug}}             edit history (only the latest snapshot is kept)
  GET  /memories/{{slug}}/reviews     every verdict recorded against it, newest first
                                      (invalidate / verify / decay, with actor + reason)

WRITE
  POST   /memories                    create            {{"name":..., "slug":..., "body":...,
                                                         "tags":["a","b"], "category":"...",
                                                         "priority":"...", "related_slugs":[...],
                                                         "refs":["host:web01","git:1a2b3c4"]}}
  PUT    /memories/{{slug}}           create-or-replace (upsert by default)
  POST   /memories/batch              {{"memories":[...], "upsert":true}} -> per-item result
  POST   /memories/{{slug}}/relate    link two memories
  POST   /memories/{{slug}}/invalidate  a re-check contradicted this:
                                        {{"reason":"why it no longer holds",
                                          "status":"contradicted|stale",
                                          "superseded_by":"other-slug"}}  (optional)
  POST   /memories/{{slug}}/verify    outcome of a re-check. verdict "ok" is the ONLY
                                      way back to active — editing a body never restores
                                      {{"verdict":"ok|stale|contradicted","reason":"..."}}
                                      Both verdict endpoints accept "if_status" /
                                      "if_updated_at": the value you read first. If the
                                      memory moved in between you get 409, not a quiet
                                      overwrite of whoever re-checked it first.
  POST   /memories/decay              stamp long-unchecked memories "stale" (still listed,
                                      still recallable). ?apply=true to write, otherwise it
                                      only reports; ?stale_after_days=N to override.
                                      Scope: memories that carry refs, and not session
                                      flow / digests / no-review rows — with nothing to
                                      re-check, ageing would be a verdict about the reader.
                                      Never contradicts, never restores: those need a reason
  POST   /memories/graph/auto-link    connect memory pairs sharing >=min_shared exact
                                      keywords (default min_freq=3, max_coverage=0.3,
                                      min_shared=3, rebuild=false)
  DELETE /memories/{{slug}}           soft delete (add ?hard=true to purge one,
                                      archived rows included — this is how you
                                      clear a single archived memory)
  DELETE /memories/archived           hard-purge EVERY archived memory at once.
                                      No filter, no body: refuses with 400 unless
                                      ?confirm=all, because a request that meant
                                      one slug used to erase the whole archive
  DELETE /memories/graph/auto-links   drop every auto-linked "keyword" edge (hand-made
                                      relations are untouched; /graph/auto-link rebuilds them)
  POST   /memories/{{slug}}/restore   undo a soft delete
  POST   /backup/backups/{{filename}}/restore  replay a backup into DB + files

FILES
  Memories are mirrored as markdown in {memory_dir}/<slug>.md. Dropping a file
  there is picked up automatically within {sync_interval}s.

CONVENTIONS
  - category: {cats}
  - priority: {pris}
  - lifespan: {life}
  - status: {statuses}. A memory is a historical observation, not a verified fact.
  - refs: attach what a later reader can re-check — {refkinds}, each as
    "<kind>:<target>" on the memory. An unverifiable ref is worse than none, so a
    typo'd kind is a 422, not something quietly stored.
  - Write with at least one ref: a memory nobody can trace back is a hunch with a
    timestamp. Cite the commit, the file, the URL, another memory, the exact
    command, the machine. Send "refs":["<kind>:<target>",...] in the JSON body
    (PUT replaces them, omission leaves them alone). A write that stores no refs
    gets an X-Provenance-Hint response header — a nudge, not an error.
  - Re-checking: preflight only judges what this server can reach, so git:/cmd:/
    host:/ip: come back "unchecked" rather than a guess. From the client,
    cli/msc-verify.sh covers that other half (dry-run unless --yes) and
    cli/msc-refs-backfill.sh lifts provenance already written in bodies into refs
  - Contradicted memories drop out of /memories and /search by default and stay
    readable by slug; the drop is announced in X-Contradicted-Hidden, never quiet.
    They are never deleted — a wrong belief is evidence.
  - Ageing: after MSC_STALE_AFTER_DAYS (default 90, 0 = off) a memory nobody has
    verified becomes "stale" on its own. That is suspicion, not evidence — stale
    memories keep showing up in lists, search and digests. Clear it with
    POST /memories/{{slug}}/verify {{"verdict":"ok"}}, or work down GET /memories/review-due.
    Only memories that carry refs age: with no handle there is nothing to re-check,
    so those rows wait in the "unanchored" bucket instead of on a clock, and an old
    or hand-written memory is never nagged into a status it cannot answer for.
    Session flow (tag=turn; turn-/sess-/session-summary-/summary- slugs, which
    covers the legacy hook naming too) and anything tagged "no-review" is exempt
    by record, counted in the census. A machine-written digest stays flow even
    after someone attaches refs to it: the rule asks who maintains it, not who
    last touched it. Rewriting a body is NOT a re-check — only verify clears the
    clock.
    /meta reports stale / never_verified / no_refs so you can see how honest the
    library is being before you quote it.
  - Send "X-Agent: <your name>" on writes; it is recorded in the version history.
  - Bad vocabulary returns 422 with the allowed values, never a 500.

EXAMPLES
  curl -s {base}/api/v1/memories?limit=3
  curl -s "{base}/api/v1/search?q=backup"
  curl -s -X PUT {base}/api/v1/memories/my-key \\
       -H "Content-Type: application/json" -H "X-Agent: my-agent" \\
       -d '{{"name":"My key","body":"what it is","category":"reference",
            "tags":["a"],"refs":["ip:192.168.1.20","file:/etc/app/config.toml"]}}'
"""


def help_body() -> str:
    """The plain-text usage guide, shared by /api/v1/help and /llms.txt."""
    base = settings.public_url or f"http://{settings.bind_host}:{settings.bind_port}"
    return HELP.format(
        base=base,
        memory_dir=settings.memory_dir,
        sync_interval=settings.sync_interval_seconds,
        cats=", ".join(CATEGORIES),
        pris=", ".join(PRIORITIES),
        life=", ".join(LIFESPANS),
        statuses=" | ".join(MEMORY_STATUSES),
        refkinds=" ".join(f"{k}:" for k in REF_KINDS),
    )


@router.get("/help", response_class=PlainTextResponse)
def help_text():
    """Plain-text usage guide, tuned for curl rather than a browser."""
    return PlainTextResponse(help_body(), media_type="text/plain; charset=utf-8")
