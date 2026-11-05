"""API routes: search, recommend."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from msc.database import get_db, engine
from msc.fts import sanitize_match
from msc.models import Memory, Tag, MemoryRelation
from msc.text import normalize_tags

router = APIRouter()


def _raw_exec(sql, params=None):
    """Execute raw SQL using SQLAlchemy engine connection."""
    with engine.connect() as conn:
        from sqlalchemy import text
        if params is None:
            result = conn.execute(text(sql))
        else:
            # Convert list params to dict for named bindparams
            result = conn.execute(text(sql), {f"p{i}": v for i, v in enumerate(params)})
        rows = result.fetchall()
        conn.commit()
        return rows


def _get_tags_for_memory(mem_id: int, db: Session) -> list:
    """Get tags for a memory by ID (avoids lazy loading issues with raw queries)."""
    rows = _raw_exec("SELECT t.id, t.name FROM tags t JOIN memory_tags mt ON t.id = mt.tag_id WHERE mt.memory_id = :p0", [mem_id])
    return [{"id": t[0], "name": t[1]} for t in rows]


@router.get("")
def search_memories(
    q: str = Query("", description="Full-text search query"),
    tags: Optional[str] = Query(None, description="Comma-separated tag filter"),
    categories: Optional[str] = Query(None, description="Comma-separated category filter"),
    priorities: Optional[str] = Query(None, description="Comma-separated priority filter"),
    branches: Optional[str] = Query(None, description="Comma-separated branch filter"),
    include_archived: bool = Query(False, description="Include soft-deleted memories"),
    limit: int = Query(20, le=100),
    offset: int = Query(0),
    response: Response = None,
    request: Request = None,
    db: Session = Depends(get_db),
):
    """Full-text + filtered search. No auth: open on the LAN (see MSC_AUTH_ENABLED).

    Ranking comes from FTS5; arbitrary input is quoted first so operators and
    punctuation can't break the query, and a LIKE pass covers what FTS misses.
    Filters combine with the text query rather than replacing it.
    """
    candidates: List[tuple] = []  # (memory_id, score)

    if q:
        match = sanitize_match(q)
        active = "" if include_archived else " AND m.archived = 0"
        active_like = "" if include_archived else " AND archived = 0"
        try:
            rows = _raw_exec(
                "SELECT m.id, -f.rank FROM memories m "
                "JOIN memories_fts f ON f.rowid = m.id "
                "WHERE memories_fts MATCH :p0" + active + " "
                "ORDER BY f.rank LIMIT :p1",
                [match, max(limit * 4, 40)],
            )
            candidates = [(r[0], round(min(1.0, max(0.0, float(r[1]))), 4)) for r in rows]
        except Exception:
            candidates = []  # never let a weird query turn into a 500

        if not candidates:
            like = f"%{q}%"
            rows = _raw_exec(
                "SELECT id FROM memories WHERE (name LIKE :p0 OR description LIKE :p1 "
                "OR body LIKE :p2 OR slug LIKE :p3)" + active_like +
                " ORDER BY updated_at DESC LIMIT :p4",
                [like, like, like, like, max(limit * 4, 40)],
            )
            candidates = [(r[0], 0.5) for r in rows]

    query = db.query(Memory)
    if not include_archived:
        query = query.filter(Memory.archived == 0)

    if candidates:
        query = query.filter(Memory.id.in_([cid for cid, _ in candidates]))

    if q and not candidates:
        # A text query that matched nothing should not fall through to "everything".
        return []

    if tags:
        tag_list = normalize_tags(tags)
        if tag_list:
            query = query.join(Memory.tags).filter(Tag.name.in_(tag_list))

    if categories:
        cat_list = [c.strip() for c in categories.split(",") if c.strip()]
        query = query.filter(Memory.category.in_(cat_list))

    if priorities:
        pri_list = [p.strip() for p in priorities.split(",") if p.strip()]
        query = query.filter(Memory.priority.in_(pri_list))

    if branches:
        br_list = [b.strip() for b in branches.split(",") if b.strip()]
        query = query.filter(Memory.branch.in_(br_list))

    if q:
        order = {cid: i for i, (cid, _) in enumerate(candidates)}
        memories = sorted(query.all(), key=lambda m: order.get(m.id, len(order)))
    else:
        memories = query.order_by(Memory.updated_at.desc()).all()

    total = len(memories)
    memories = memories[offset:offset + limit]

    results = []
    for m in memories:
        score = next((s for cid, s in candidates if cid == m.id), 1.0)
        results.append({
            "id": m.id,
            "slug": m.slug,
            "name": m.name,
            "description": m.description,
            "category": m.category,
            "priority": m.priority,
            "tags": [{"id": t.id, "name": t.name} for t in m.tags],
            "score": score,
        })

    if response is not None:
        response.headers["X-Total-Count"] = str(total)

    return results


@router.get("/recommend/{slug}")
def recommend_related(slug: str, limit: int = Query(5, le=20), db: Session = Depends(get_db)):
    """Recommend memories related to the given slug."""
    mem = db.query(Memory).filter(Memory.slug == slug, Memory.archived == 0).first()
    if not mem:
        raise HTTPException(status_code=404, detail="Memory not found")

    related = db.query(MemoryRelation).filter(
        (MemoryRelation.memory_id_a == mem.id) | (MemoryRelation.memory_id_b == mem.id)
    ).limit(limit).all()

    result = []
    seen = set()
    for rel in related:
        other_id = rel.memory_id_b if rel.memory_id_a == mem.id else rel.memory_id_a
        other = db.query(Memory).filter(Memory.id == other_id, Memory.archived == 0).first()
        # Relations exist in both directions, so the same memory used to be
        # recommended twice.
        if other and other.id not in seen:
            seen.add(other.id)
            result.append({
                "id": other.id,
                "slug": other.slug,
                "name": other.name,
                "description": other.description,
                "category": other.category,
                "priority": other.priority,
                "relation_type": rel.relation_type,
            })

    # Also suggest by shared tags
    mem_tags = {t.name for t in mem.tags}
    if mem_tags:
        tag_suggestions = db.query(Memory).join(Memory.tags).filter(
            Memory.slug != slug,
            Memory.archived == 0,
            Tag.name.in_(list(mem_tags)),
        ).limit(limit).all()

        for s in tag_suggestions:
            if not any(r["id"] == s.id for r in result):
                result.append({
                    "id": s.id,
                    "slug": s.slug,
                    "name": s.name,
                    "description": s.description,
                    "category": s.category,
                    "priority": s.priority,
                    "relation_type": "suggested",
                })

    return result[:limit]
