"""Self-describing endpoints — everything an agent needs to use this service
without a human reading a README to it first."""

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from msc.api.memories import CATEGORIES, LIFESPANS, PRIORITIES
from msc.auth.api_key import auth_status
from msc.config import VERSION, get_settings
from msc.database import get_db
from msc.models import Memory, MemoryBranch, MemoryTag, Tag

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
                                      limit<=500, offset; X-Total-Count header)
  GET  /memories/{{slug}}             one memory; slug, exact name, or unique fragment
  GET  /search?q=...                  full-text (safe with any characters) + filters
                                      mode=keyword|semantic|hybrid (default keyword;
                                      semantic modes need an embed provider, otherwise
                                      they degrade to keyword; hybrid results carry
                                      a "match" field)
  GET  /meta                          vocabularies, tags, counts
  GET  /tags                          tag names + usage counts
  GET  /stats                         counts by category/priority/source
  GET  /memories/graph                relation graph (?max_degree=N caps edges per node; 0 = all)
  GET  /versions/{{slug}}             edit history (only the latest snapshot is kept)

WRITE
  POST   /memories                    create            {{"name":..., "slug":..., "body":...,
                                                         "tags":["a","b"], "category":"...",
                                                         "priority":"...", "related_slugs":[...]}}
  PUT    /memories/{{slug}}           create-or-replace (upsert by default)
  POST   /memories/batch              {{"memories":[...], "upsert":true}} -> per-item result
  POST   /memories/{{slug}}/relate    link two memories
  POST   /memories/graph/auto-link    connect memory pairs sharing >=min_shared exact
                                      keywords (default min_freq=3, max_coverage=0.3,
                                      min_shared=3, rebuild=false)
  DELETE /memories/{{slug}}           soft delete (add ?hard=true to purge)
  DELETE /memories/archived           hard-purge every archived memory at once
  POST   /memories/{{slug}}/restore   undo a soft delete
  POST   /backup/backups/{{filename}}/restore  replay a backup into DB + files

FILES
  Memories are mirrored as markdown in {memory_dir}/<slug>.md. Dropping a file
  there is picked up automatically within {sync_interval}s.

CONVENTIONS
  - category: {cats}
  - priority: {pris}
  - lifespan: {life}
  - Send "X-Agent: <your name>" on writes; it is recorded in the version history.
  - Bad vocabulary returns 422 with the allowed values, never a 500.

EXAMPLES
  curl -s {base}/api/v1/memories?limit=3
  curl -s "{base}/api/v1/search?q=backup"
  curl -s -X PUT {base}/api/v1/memories/my-key \\
       -H "Content-Type: application/json" -H "X-Agent: my-agent" \\
       -d '{{"name":"My key","body":"what it is","category":"reference","tags":["a"]}}'
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
    )


@router.get("/help", response_class=PlainTextResponse)
def help_text():
    """Plain-text usage guide, tuned for curl rather than a browser."""
    return PlainTextResponse(help_body(), media_type="text/plain; charset=utf-8")
