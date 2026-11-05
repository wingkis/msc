"""API routes: memories CRUD, graph, relate."""

import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from msc.config import get_settings
from msc.database import get_db
from msc.models import Memory, Tag, MemoryVersion, MemoryRelation, ApiKey
from msc.schemas import (
    BatchMemories, MemoryCreate, MemoryUpdate, MemoryResponse,
    GraphResponse, GraphNode, GraphEdge,
)
from msc.auth.api_key import verify_key, LAN_USER
from msc.text import clean_body, normalize_tags, slugify
from msc.filesystem.write_through import delete_through, write_through

settings = get_settings()
router = APIRouter()


def _require_auth(request: Request, db: Session) -> dict:
    """Unified auth: accepts JWT Bearer token OR X-API-Key header.

    With MSC_AUTH_ENABLED=false (default, LAN mode) this returns the anonymous
    admin identity immediately — no login, no token, no expiry.
    """
    from fastapi import HTTPException, status
    from jose import jwt as pyjwt

    if not settings.auth_enabled:
        return dict(LAN_USER)

    # Try JWT Bearer token
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            token = auth[7:]
            payload = pyjwt.decode(token, settings.secret_key, algorithms=["HS256"])
            return {
                "user_id": int(payload.get("sub", 0)),
                "username": payload.get("username", ""),
                "role": payload.get("role", "viewer"),
                "prefix": payload.get("prefix"),
            }
        except Exception:
            pass  # Invalid JWT, try API key

    # Try X-API-Key header
    api_key = request.headers.get("X-API-Key", "")
    if api_key.startswith("api-"):
        parts = api_key.split("-", 2)
        if len(parts) >= 3:
            prefix = parts[1]
            key_record = db.query(ApiKey).filter(
                ApiKey.key_prefix == prefix, ApiKey.is_active == 1
            ).first()
            if key_record and verify_key(api_key, key_record.key_hash):
                return {
                    "user_id": 0,
                    "username": f"api-{prefix}",
                    "role": "editor",
                    "prefix": prefix,
                }

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


CATEGORIES = ("config", "project", "user", "knowledge", "reference")
PRIORITIES = ("low", "medium", "high", "critical")
LIFESPANS = ("temporary", "permanent")


def _slugify(name: str) -> str:
    return slugify(name)


def _guard_write() -> None:
    """MSC_READ_ONLY=true turns the whole service into a read-only mirror."""
    if settings.read_only:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Server is read-only (MSC_READ_ONLY=true); writes are rejected",
        )


def _validate_vocab(data) -> None:
    """Reject bad vocabulary with a 422 that names the allowed values.

    The SQLite schema enforces these with CHECK constraints, so an unchecked
    value surfaces as a 500 IntegrityError — useless for an agent.
    """
    for field, allowed in (("category", CATEGORIES), ("priority", PRIORITIES), ("lifespan", LIFESPANS)):
        value = getattr(data, field, None)
        if value is not None and value not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid {field} {value!r}; allowed: {', '.join(allowed)}",
            )


def _actor(request: Request) -> str:
    """Who wrote this, for the version history.

    There is no login on the LAN, so attribution is a courtesy: send
    ``X-Agent: claude-code`` (or ``?by=name``) and it lands in created_by.
    """
    name = request.headers.get("X-Agent") or request.headers.get("X-Client") or ""
    if not name:
        name = request.query_params.get("by", "")
    name = name.strip()[:64]
    return name or LAN_USER["username"]


def _resolve_memory(db: Session, ref: str, include_archived: bool = False) -> Memory:
    """Find a memory by slug, then by exact name, then by a unique prefix.

    Agents rarely know the exact slug; they know roughly what it was called.
    """
    query = db.query(Memory)
    if not include_archived:
        query = query.filter(Memory.archived == 0)

    mem = query.filter(Memory.slug == ref).first()
    if mem:
        return mem

    mem = query.filter(func.lower(Memory.name) == ref.lower()).first()
    if mem:
        return mem

    matches = query.filter(Memory.slug.like(f"{ref}%")).limit(5).all()
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail=f"Ambiguous reference {ref!r}: {', '.join(m.slug for m in matches)}",
        )

    matches = query.filter(Memory.slug.like(f"%{ref}%")).limit(5).all()
    if len(matches) == 1:
        return matches[0]

    raise HTTPException(status_code=404, detail=f"Memory not found: {ref}")


def _apply_tags(db: Session, memory: Memory, raw_tags) -> None:
    """Replace a memory's tags, accepting any tag shape an agent might send."""
    memory.tags = []
    db.flush()
    for name in normalize_tags(raw_tags):
        tag = db.query(Tag).filter(Tag.name == name).first()
        if not tag:
            tag = Tag(name=name)
            db.add(tag)
            db.flush()
        memory.tags.append(tag)


def _write_through(db: Session, memory: Memory) -> None:
    """File + JSON index + last_sync_hash — see msc.filesystem.write_through."""
    write_through(db, memory)


def _create_by_slug(db: Session, slug: str, data: MemoryUpdate, actor: str) -> Memory:
    """Backstop for PUT-with-upsert: nothing matched, so create it."""
    now = datetime.now(timezone.utc).isoformat()
    memory = Memory(
        slug=slug, name=data.name or slug, description=data.description or "",
        body=clean_body(data.body), priority=data.priority or "medium",
        category=data.category or "reference", lifespan=data.lifespan or "permanent",
        branch=data.branch or "", created_at=now, updated_at=now,
        source="msc", owner_id=None,
    )
    db.add(memory)
    db.flush()
    if data.tags is not None:
        _apply_tags(db, memory, data.tags)

    db.add(MemoryVersion(
        memory_id=memory.id, version_num=1, name=memory.name,
        description=memory.description, body=memory.body, priority=memory.priority,
        category=memory.category, tags_snapshot=json.dumps([]),
        change_summary="created by upsert", created_at=now, created_by=actor,
    ))
    db.commit()
    _write_through(db, memory)
    return memory


def _category_color(category: str) -> str:
    colors = {"config": "#3b82f6", "project": "#22c55e", "user": "#eab308", "knowledge": "#a855f7", "reference": "#9ca3af"}
    return colors.get(category, "#9ca3af")


def next_version_num(db: Session, memory_id: int) -> int:
    """max()+1 — counting rows goes wrong the moment a version is removed."""
    return (db.query(func.max(MemoryVersion.version_num)).filter(
        MemoryVersion.memory_id == memory_id).scalar() or 0) + 1


def snapshot_previous(db: Session, memory: Memory, change_summary: str, actor: str) -> None:
    """Record the pre-edit state, keeping only the newest snapshot.

    The full history used to grow forever; one snapshot is enough to undo the
    last change, and the versions table stays small. Version numbers keep
    increasing so a restore still has a stable reference.
    """
    version_num = next_version_num(db, memory.id)
    db.query(MemoryVersion).filter(MemoryVersion.memory_id == memory.id).delete()
    db.add(MemoryVersion(
        memory_id=memory.id, version_num=version_num,
        name=memory.name, description=memory.description, body=memory.body,
        priority=memory.priority, category=memory.category,
        tags_snapshot=json.dumps([t.name for t in memory.tags]),
        change_summary=change_summary,
        created_at=datetime.now(timezone.utc).isoformat(), created_by=actor,
    ))


# ── List memories ─────────────────────────────────────────────────────

@router.get("", response_model=List[MemoryResponse])
def list_memories(
    category: Optional[str] = Query(None), priority: Optional[str] = Query(None),
    tag: Optional[str] = Query(None), branch: Optional[str] = Query(None),
    archived: bool = Query(False, description="List only the soft-deleted ones"),
    include_archived: bool = Query(False, description="Keep deleted ones in the list"),
    since: Optional[str] = Query(None, description="Only memories updated at/after this ISO timestamp"),
    q: Optional[str] = Query(None, description="Substring match on name/description/body/slug"),
    order: str = Query("updated", pattern="^(updated|created|priority|name)$"),
    limit: int = Query(50, le=500), offset: int = Query(0),
    response: Response = None,
    db: Session = Depends(get_db), request: Request = None,
):
    """List memories. ``X-Total-Count`` reports the full match count, and
    ``since`` makes incremental pulls cheap for polling agents."""
    query = db.query(Memory)
    if archived:
        query = query.filter(Memory.archived == 1)
    elif not include_archived:
        query = query.filter(Memory.archived == 0)

    if category: query = query.filter(Memory.category == category)
    if priority: query = query.filter(Memory.priority == priority)
    if branch: query = query.filter(Memory.branch == branch)
    if tag: query = query.join(Memory.tags).filter(Tag.name.in_(normalize_tags(tag)))
    if since: query = query.filter(Memory.updated_at >= since)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Memory.name.like(like)) | (Memory.description.like(like))
            | (Memory.body.like(like)) | (Memory.slug.like(like))
        )

    order_col = {
        "updated": Memory.updated_at, "created": Memory.created_at,
        "name": Memory.name, "priority": Memory.priority,
    }[order]
    rows = query.order_by(order_col.desc()).all()

    if response is not None:
        response.headers["X-Total-Count"] = str(len(rows))

    return rows[offset:offset + limit]


# ── Graph (MUST be before /{slug} route) ──────────────────────────────

@router.get("/graph", response_model=GraphResponse)
def get_graph(center: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """Get graph data for Vis.js visualization."""
    if center:
        center_mem = db.query(Memory).filter(Memory.slug == center, Memory.archived == 0).first()
        if not center_mem: raise HTTPException(status_code=404, detail=f"Memory not found: {center}")
        connected_ids = {center_mem.id}
        for rel in db.query(MemoryRelation).filter(
            (MemoryRelation.memory_id_a == center_mem.id) | (MemoryRelation.memory_id_b == center_mem.id)
        ).all():
            connected_ids.add(rel.memory_id_b if rel.memory_id_a == center_mem.id else rel.memory_id_a)
        memories = db.query(Memory).filter(Memory.id.in_(connected_ids), Memory.archived == 0).all()
    else:
        memories = db.query(Memory).filter(Memory.archived == 0).all()

    nodes = [GraphNode(id=mem.id, slug=mem.slug, name=mem.name,
                       category=mem.category, priority=mem.priority, color=_category_color(mem.category))
             for mem in memories]
    memory_ids = {m.id for m in memories}
    # Relations are stored in both directions, so fold each pair into one edge.
    edges, seen = [], set()
    for rel in db.query(MemoryRelation).filter(
        MemoryRelation.memory_id_a.in_(memory_ids), MemoryRelation.memory_id_b.in_(memory_ids)
    ).all():
        key = (frozenset((rel.memory_id_a, rel.memory_id_b)), rel.relation_type)
        if key in seen:
            continue
        seen.add(key)
        edges.append(GraphEdge(source=rel.memory_id_a, target=rel.memory_id_b,
                               relation_type=rel.relation_type))
    return GraphResponse(nodes=nodes, edges=edges)


# ── Get memory detail ─────────────────────────────────────────────────

@router.get("/{slug}", response_model=MemoryResponse)
def get_memory(slug: str, include_archived: bool = Query(False), db: Session = Depends(get_db)):
    """Fetch one memory. Accepts a slug, an exact name, or a unique slug fragment."""
    return _resolve_memory(db, slug, include_archived=include_archived)


# ── Create memory ─────────────────────────────────────────────────────

@router.post("", response_model=MemoryResponse, status_code=201)
def create_memory(data: MemoryCreate, request: Request, db: Session = Depends(get_db)):
    _require_auth(request, db)
    _guard_write()
    _validate_vocab(data)

    slug = (data.slug or "").strip() or _slugify(data.name)
    existing = db.query(Memory).filter(Memory.slug == slug).first()
    if existing and not existing.archived:
        raise HTTPException(status_code=409, detail=f"Slug already exists: {slug}")
    if existing and existing.archived:
        # A soft-deleted memory still owns its slug; revive it instead of
        # failing with a confusing conflict.
        existing.archived = 0

    now = datetime.now(timezone.utc).isoformat()
    tags = []
    for tag_name in normalize_tags(data.tags):
        tag = db.query(Tag).filter(Tag.name == tag_name).first()
        if not tag:
            tag = Tag(name=tag_name)
            db.add(tag)
            db.flush()
        tags.append(tag)

    memory = Memory(slug=slug, name=data.name, description=data.description or "",
                    body=clean_body(data.body), priority=data.priority or "medium",
                    category=data.category or "reference", lifespan=data.lifespan or "permanent",
                    branch=data.branch or "", created_at=now, updated_at=now, source="msc")
    memory.tags = tags
    db.add(memory)
    db.flush()

    for related_slug in data.related_slugs or []:
        related = db.query(Memory).filter(Memory.slug == related_slug, Memory.archived == 0).first()
        if related:
            db.add(MemoryRelation(memory_id_a=memory.id, memory_id_b=related.id,
                                  relation_type="related", created_at=now))
            db.add(MemoryRelation(memory_id_a=related.id, memory_id_b=memory.id,
                                  relation_type="related", created_at=now))

    db.commit()
    _write_through(db, memory)
    return memory


# ── Update memory ─────────────────────────────────────────────────────

@router.put("/{slug}", response_model=MemoryResponse)
def update_memory(slug: str, data: MemoryUpdate, request: Request,
                  upsert: bool = Query(True, description="Create this slug if it does not exist"),
                  db: Session = Depends(get_db)):
    """Create-or-replace by slug — the idempotent write agents expect.

    ``?upsert=false`` restores the strict 404-if-missing behaviour.
    """
    current_user = _require_auth(request, db)
    _guard_write()
    _validate_vocab(data)
    current_user["username"] = _actor(request)

    memory = db.query(Memory).filter(Memory.slug == slug).first()
    if memory is None:
        if not upsert:
            raise HTTPException(status_code=404, detail=f"Memory not found: {slug}")
        return _create_by_slug(db, slug, data, current_user["username"])
    if memory.archived:
        memory.archived = 0  # writing to a soft-deleted memory revives it

    snapshot_previous(db, memory,
                      data.body if data.body else (data.name or "metadata update"),
                      current_user["username"])

    for field in ("name", "description", "body", "priority", "category", "lifespan", "branch"):
        val = getattr(data, field)
        if val is not None: setattr(memory, field, val)
    if data.body is not None:
        memory.body = clean_body(data.body)
    if data.tags is not None:
        _apply_tags(db, memory, data.tags)

    memory.updated_at = datetime.now(timezone.utc).isoformat()
    db.commit()

    _write_through(db, memory)
    return memory


# ── Purge every archived memory (hard) ────────────────────────────────
# Registered before /{slug}: Starlette matches routes in registration order,
# and a bare DELETE /memories/archived would otherwise be read as a slug.

@router.delete("/archived")
def purge_archived(request: Request, db: Session = Depends(get_db)):
    """Hard-delete all soft-deleted memories in one pass (rows, history, relations)."""
    _require_auth(request, db)
    _guard_write()
    actor = _actor(request)

    memories = db.query(Memory).filter(Memory.archived == 1).all()
    purged = []
    for memory in memories:
        slug = memory.slug
        db.query(MemoryVersion).filter(MemoryVersion.memory_id == memory.id).delete()
        db.query(MemoryRelation).filter(
            (MemoryRelation.memory_id_a == memory.id) | (MemoryRelation.memory_id_b == memory.id)
        ).delete()
        memory.tags = []
        db.delete(memory)
        purged.append(slug)
    db.commit()
    for slug in purged:
        delete_through(slug)
    return {"purged": len(purged), "slugs": purged, "by": actor}


# ── Delete memory (soft delete) ───────────────────────────────────────

@router.delete("/{slug}")
def delete_memory(slug: str, request: Request,
                  hard: bool = Query(False, description="Really remove the row and its history"),
                  db: Session = Depends(get_db)):
    """Soft delete by default — a LAN service with no login needs an undo path.

    Resolution includes archived rows: a soft-deleted memory can still be
    purged with ?hard=true, which is exactly what the UI's "彻底删除" does.
    """
    _require_auth(request, db)
    _guard_write()
    memory = _resolve_memory(db, slug, include_archived=True)
    actor = _actor(request)

    if hard:
        db.query(MemoryVersion).filter(MemoryVersion.memory_id == memory.id).delete()
        db.query(MemoryRelation).filter(
            (MemoryRelation.memory_id_a == memory.id) | (MemoryRelation.memory_id_b == memory.id)
        ).delete()
        memory.tags = []
        db.delete(memory)
        db.commit()
        delete_through(slug)
        return {"message": f"Memory deleted: {slug}", "hard": True, "by": actor}

    memory.archived = 1
    memory.updated_at = datetime.now(timezone.utc).isoformat()
    db.commit()
    delete_through(slug)
    return {"message": f"Memory archived: {slug}", "hard": False, "by": actor,
            "restore": f"POST /api/v1/memories/{slug}/restore"}


@router.post("/{slug}/restore", response_model=MemoryResponse)
def restore_memory(slug: str, request: Request, db: Session = Depends(get_db)):
    """Bring a soft-deleted memory back. Accepts the same slug/name resolution
    as the other detail routes, not just an exact slug."""
    _require_auth(request, db)
    _guard_write()

    memory = _resolve_memory(db, slug, include_archived=True)
    if not memory.archived:
        raise HTTPException(status_code=409, detail=f"Memory is not archived: {slug}")

    memory.archived = 0
    memory.updated_at = datetime.now(timezone.utc).isoformat()
    db.commit()
    _write_through(db, memory)
    return memory


@router.post("/batch", status_code=201)
def batch_memories(data: BatchMemories, request: Request, db: Session = Depends(get_db)):
    """Create (or upsert) many memories in one call. Per-item results, no
    partial-failure ambiguity: each item reports its own status."""
    _require_auth(request, db)
    _guard_write()
    actor = _actor(request)

    created, updated, failed = [], [], []
    for item in data.memories:
        try:
            _validate_vocab(item)
            slug = (item.slug or "").strip() or _slugify(item.name)
            existing = db.query(Memory).filter(Memory.slug == slug).first()
            if existing:
                if not data.upsert:
                    failed.append({"slug": slug, "error": "exists"})
                    continue
                snapshot_previous(db, existing, "batch update", actor)
                existing.name = item.name or existing.name
                existing.description = item.description or ""
                existing.body = clean_body(item.body)
                existing.priority = item.priority or existing.priority
                existing.category = item.category or existing.category
                existing.lifespan = item.lifespan or existing.lifespan
                existing.branch = item.branch or ""
                existing.archived = 0
                existing.updated_at = datetime.now(timezone.utc).isoformat()
                _apply_tags(db, existing, item.tags)
                _write_through(db, existing)
                updated.append(slug)
            else:
                _create_by_slug(db, slug, MemoryUpdate(**item.model_dump(exclude={"slug", "related_slugs"})), actor)
                created.append(slug)
        except HTTPException as exc:
            failed.append({"slug": (item.slug or "").strip() or _slugify(item.name), "error": exc.detail})
        except Exception as exc:  # keep the batch going, report what broke
            db.rollback()
            failed.append({"slug": (item.slug or "").strip() or _slugify(item.name), "error": str(exc)})

    return {
        "created": created, "updated": updated, "failed": failed,
        "by": actor,
        "message": f"{len(created)} created, {len(updated)} updated, {len(failed)} failed",
    }


# ── Relate ────────────────────────────────────────────────────────────

@router.get("/{slug}/relate", response_model=list)
def get_memory_relations(slug: str, relation_type: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """Get relations for a memory."""
    memory = db.query(Memory).filter(Memory.slug == slug, Memory.archived == 0).first()
    if not memory: raise HTTPException(status_code=404, detail="Memory not found")

    query = db.query(MemoryRelation).filter(
        (MemoryRelation.memory_id_a == memory.id) | (MemoryRelation.memory_id_b == memory.id)
    )
    if relation_type:
        query = query.filter(MemoryRelation.relation_type == relation_type)

    relations = []
    seen = set()
    for rel in query.all():
        other_id = rel.memory_id_b if rel.memory_id_a == memory.id else rel.memory_id_a
        other = db.query(Memory).filter(Memory.id == other_id, Memory.archived == 0).first()
        # Both directions of a pair are stored, so without this every linked
        # memory showed up twice in GET /{slug}/relate.
        if other and (other.id, rel.relation_type) not in seen:
            seen.add((other.id, rel.relation_type))
            relations.append({
                "id": other.id,
                "slug": other.slug,
                "name": other.name,
                "relation_type": rel.relation_type,
            })
    return relations


@router.post("/{slug}/relate")
def relate_memory(
    slug: str,
    target_slug: str = Query(..., alias="target"),
    relation_type: str = Query("related"),
    db: Session = Depends(get_db),
):
    """Create a relation between two memories."""
    mem_a = db.query(Memory).filter(Memory.slug == slug, Memory.archived == 0).first()
    if not mem_a: raise HTTPException(status_code=404, detail="Memory not found")
    mem_b = db.query(Memory).filter(Memory.slug == target_slug, Memory.archived == 0).first()
    if not mem_b: raise HTTPException(status_code=404, detail="Target memory not found")
    if mem_a.id == mem_b.id:
        raise HTTPException(status_code=400, detail="Cannot relate memory to itself")

    existing = db.query(MemoryRelation).filter(
        ((MemoryRelation.memory_id_a == mem_a.id) & (MemoryRelation.memory_id_b == mem_b.id)) |
        ((MemoryRelation.memory_id_a == mem_b.id) & (MemoryRelation.memory_id_b == mem_a.id))
    ).first()

    if existing:
        existing.relation_type = relation_type
        db.commit()
        return {"message": f"Updated relation: {slug} <-> {target_slug} ({relation_type})"}
    else:
        db.add(MemoryRelation(memory_id_a=mem_a.id, memory_id_b=mem_b.id,
                              relation_type=relation_type, created_at=datetime.now(timezone.utc).isoformat()))
        db.add(MemoryRelation(memory_id_a=mem_b.id, memory_id_b=mem_a.id,
                              relation_type=relation_type, created_at=datetime.now(timezone.utc).isoformat()))
        db.commit()
        return {"message": f"Created relation: {slug} <-> {target_slug} ({relation_type})"}

