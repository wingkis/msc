"""API routes: memories CRUD, graph, relate."""

import json
import os
import time
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from msc.config import get_settings
from msc.database import get_db
from msc.models import Memory, Tag, MemoryVersion, MemoryRelation, ApiKey
from msc.schemas import (
    BatchMemories, MemoryCreate, MemoryUpdate, MemoryResponse,
    GraphResponse, GraphNode, GraphEdge,
    MemoryInvalidate, MemoryVerdict, ProbeItem, PreflightItem, PreflightResponse,
    MEMORY_STATUSES, dump_refs, parse_refs,
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
# A memory that a re-check contradicted is not deleted: a wrong belief is still
# evidence. It stops being handed to the next session as if it were current.
INVALIDATED_STATUSES = ("stale", "contradicted")


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
    # status joins the same "422 must name the allowed values" contract. The
    # CHECK constraint only exists in databases created after this column, so on
    # an upgraded db an unchecked value would otherwise surface as a 500.
    for field, allowed in (("category", CATEGORIES), ("priority", PRIORITIES),
                           ("lifespan", LIFESPANS), ("status", MEMORY_STATUSES)):
        value = getattr(data, field, None)
        if value is not None and value not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid {field} {value!r}; allowed: {', '.join(allowed)}",
            )


def _live_status_pred():
    """Predicate for "not contradicted", NULL-safe.

    A database upgraded with ALTER TABLE has NULL status on old rows, and NULL
    compares as unknown — a bare ``!= 'contradicted'`` would hide every memory
    written before this column existed.
    """
    return or_(Memory.status.is_(None), Memory.status != "contradicted")


def _split_contradicted(query):
    """Return (query_without_contradicted, how_many_were_hidden)."""
    hidden = query.filter(Memory.status == "contradicted").count()
    return query.filter(_live_status_pred()), hidden


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
    """Find a memory by numeric id, then by slug, then by exact name, then by a unique prefix.

    Agents rarely know the exact slug; they know roughly what it was called,
    and hook context lines show the numeric id, so accept that too.
    """
    query = db.query(Memory)
    if not include_archived:
        query = query.filter(Memory.archived == 0)

    if ref.isdigit():
        mem = query.filter(Memory.id == int(ref)).first()
        if mem:
            return mem

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
        refs=dump_refs(data.refs), status="active",
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
    include_invalidated: bool = Query(
        False, description="Keep memories a re-check contradicted in the list "
                           "(the default hides them; X-Contradicted-Hidden says how many)"),
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

    # Hide contradicted memories by default, but never silently: an agent that
    # pulls incrementally must be able to notice the shelf is smaller than the
    # library. The rows stay addressable by slug and via ?include_invalidated.
    hidden = 0
    if not include_invalidated:
        query, hidden = _split_contradicted(query)

    order_col = {
        "updated": Memory.updated_at, "created": Memory.created_at,
        "name": Memory.name, "priority": Memory.priority,
    }[order]
    rows = query.order_by(order_col.desc()).all()

    if response is not None:
        response.headers["X-Total-Count"] = str(len(rows))
        response.headers["X-Contradicted-Hidden"] = str(hidden)

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


@router.post("/graph/auto-link")
def auto_link_graph(
    min_freq: int = Query(3, ge=2, le=10, description="A keyword must appear in at least this many memories"),
    max_coverage: float = Query(0.3, gt=0.0, le=1.0, description="Words covering more than this share of memories are too generic to link on"),
    min_shared: int = Query(3, ge=1, le=5, description="A pair must share at least this many keywords"),
    rebuild: bool = Query(False, description="Drop all auto-created keyword edges first, then recompute"),
    request: Request = None, db: Session = Depends(get_db),
):
    """One pass: link memories that share several exact whole-word keywords.

    Scans name/description/body/tags of every live memory, tokenises it
    (ASCII words + segmented Chinese words), and connects pairs whose shared
    keyword count reaches ``min_shared``. Keywords whose coverage exceeds
    ``max_coverage`` of the corpus are treated as ambient vocabulary and
    skipped. Idempotent — already linked pairs are skipped.
    """
    from msc.services.keyword_links import build_keyword_links

    _require_auth(request, db)
    _guard_write()
    return build_keyword_links(db, min_freq=min_freq, max_coverage=max_coverage,
                               min_shared=min_shared, rebuild=rebuild)


# ── Provenance preflight (MUST be before /{slug} route) ───────────────

# HTTP methods that mean "not this verb", not "this endpoint is gone".
_METHOD_UNSUPPORTED = (405, 501)


def _url_verdict(status_code: int) -> tuple:
    """Map an HTTP answer onto a provenance verdict: ``(verdict, detail)``.

    Getting this wrong invents staleness, which is worse than missing it. A 405
    only says the verb is unsupported; a 401/403 says something is there and
    hiding; a 5xx says something is there and unwell. None of those contradicts
    "this endpoint is where the fact lives". Only silence or a 404 does.
    """
    if status_code < 400:
        return "ok", f"HTTP {status_code}"
    if status_code == 404:
        return "missing", "HTTP 404 — endpoint is gone"
    if status_code in _METHOD_UNSUPPORTED:
        return "retry", f"HTTP {status_code} — method unsupported"
    if status_code in (401, 403):
        return "unchecked", f"HTTP {status_code} — answers, but not to us"
    if 400 <= status_code < 500:
        return "unchecked", f"HTTP {status_code} — answers, path unconfirmed"
    return "unchecked", f"HTTP {status_code} — answers, but unwell"


def _probe_ref(db: Session, ref: str) -> ProbeItem:
    """Re-check one ref from the server. Never raises.

    `missing` means the handle is gone; `unchecked` means this server is not the
    place to check it. Conflating the two would turn a probe into a rumour.
    """
    kind, _, target = ref.partition(":")
    target = target.strip()
    if kind == "mem":
        hit = db.query(Memory).filter(Memory.slug == target, Memory.archived == 0).first()
        if hit is None:
            hit = db.query(Memory).filter(Memory.name == target, Memory.archived == 0).first()
        return ProbeItem(ref=ref, kind=kind, verdict="ok" if hit else "missing",
                         detail="live memory" if hit else "no live memory with that slug")
    if kind == "file":
        if not os.path.isabs(target):
            return ProbeItem(ref=ref, kind=kind, verdict="unchecked",
                             detail="relative path belongs to the writing host")
        if os.path.exists(target):
            return ProbeItem(ref=ref, kind=kind, verdict="ok", detail="path exists")
        return ProbeItem(ref=ref, kind=kind, verdict="missing", detail="path is gone")
    if kind == "url":
        if not target.startswith(("http://", "https://")):
            return ProbeItem(ref=ref, kind=kind, verdict="unchecked", detail="not an http(s) endpoint")
        try:
            import httpx
            with httpx.Client(timeout=1.5) as probe_client:
                reply = probe_client.head(target, follow_redirects=True)
                verdict, detail = _url_verdict(reply.status_code)
                if verdict == "retry":  # ask the same way a browser would
                    reply = probe_client.get(target, follow_redirects=True)
                    verdict, detail = _url_verdict(reply.status_code)
            if verdict == "retry":
                verdict, detail = "unchecked", detail
            return ProbeItem(ref=ref, kind=kind, verdict=verdict, detail=detail)
        except Exception as exc:
            # No answer at all: the service this ref pointed at is not there.
            return ProbeItem(ref=ref, kind=kind, verdict="missing",
                             detail=f"{type(exc).__name__}: {exc}"[:160])
    # git:/cmd:/host: — the only machine that can answer is the one that wrote
    # it, and the server must not guess on its behalf.
    return ProbeItem(ref=ref, kind=kind, verdict="unchecked",
                     detail=f"{kind}: refs are checked by the client that wrote them")


@router.get("/preflight", response_model=PreflightResponse)
def preflight(
    limit: int = Query(20, le=100),
    budget_ms: int = Query(1200, le=8000, description="Wall-clock ceiling for probing"),
    category: Optional[str] = Query(None), priority: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    include_invalidated: bool = Query(
        True, description="Re-probe contradicted ones too, so a re-check can restore them"),
    db: Session = Depends(get_db),
):
    """Re-check as much of a memory's provenance as this server can reach.

    A session hook calls this before injecting memory, so a belief that no
    longer matches reality cannot enter the prompt as fact. Deliberately
    partial and deliberately read-only: it reports verdicts, it never writes.
    Turning a verdict into a status change is an explicit POST /{slug}/verify,
    because a GET that silently rewrites the library would also be a GET that
    corrupts it whenever two agents disagree about a probe.
    """
    deadline = time.monotonic() + max(0, budget_ms) / 1000.0
    query = db.query(Memory).filter(Memory.archived == 0)
    if category: query = query.filter(Memory.category == category)
    if priority: query = query.filter(Memory.priority == priority)
    if tag: query = query.join(Memory.tags).filter(Tag.name.in_(normalize_tags(tag)))

    hidden = query.filter(Memory.status == "contradicted").count()
    if not include_invalidated:
        query = query.filter(_live_status_pred())
    rows = [m for m in query.order_by(Memory.updated_at.desc()).all() if parse_refs(m.refs)]

    items: List[PreflightItem] = []
    for memory in rows[:limit]:
        refs = parse_refs(memory.refs)
        probes: List[ProbeItem] = []
        for ref in refs:
            if time.monotonic() > deadline:
                probes.append(ProbeItem(ref=ref, kind=ref.partition(":")[0],
                                        verdict="unchecked", detail="probe budget exhausted"))
                continue
            probes.append(_probe_ref(db, ref))
        verdict, detail = "active", ""
        gone = [p.ref for p in probes if p.verdict == "missing"]
        if gone:
            verdict, detail = "contradicted", "; ".join(gone)[:300]
        elif probes and all(p.verdict == "unchecked" for p in probes):
            verdict = "uncheckable"
        items.append(PreflightItem(slug=memory.slug, status=memory.status or "active",
                                   refs=refs, probes=probes, verdict=verdict, detail=detail))
    return PreflightResponse(checked=len(items), hidden=hidden, budget_ms=budget_ms, items=items)


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
                    branch=data.branch or "", created_at=now, updated_at=now, source="msc",
                    refs=dump_refs(data.refs), status="active")
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
    if data.refs is not None:
        memory.refs = dump_refs(data.refs)
    # A PUT deliberately does NOT clear an invalidation. The session-turn channel
    # PUTs a whole body every round; if rewriting text revived a memory, every
    # contradicted belief would evaporate on the next turn. Only POST /{slug}/verify
    # with verdict ok restores it — recovery is an act of checking, not of editing.

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


# ── Provenance: contradiction and recovery ────────────────────────────

@router.post("/{slug}/invalidate", response_model=MemoryResponse)
def invalidate_memory(slug: str, data: MemoryInvalidate, request: Request,
                      db: Session = Depends(get_db)):
    """Record that reality no longer matches this memory.

    This is the write-back half of the loop a memory system otherwise never
    closes: read → re-check → contradicted → stop believing it. Without it the
    library only ever grows, and a stale claim keeps re-entering every later
    session carrying the confidence of its own priority label.
    """
    _require_auth(request, db)
    _guard_write()
    memory = _resolve_memory(db, slug)
    actor = _actor(request)

    successor = None
    if data.superseded_by:
        if data.superseded_by == memory.slug:
            raise HTTPException(status_code=422, detail="A memory cannot supersede itself")
        successor = db.query(Memory).filter(
            Memory.slug == data.superseded_by, Memory.archived == 0).first()
        if successor is None:
            raise HTTPException(status_code=404,
                                detail=f"superseded_by not found: {data.superseded_by}")

    if memory.status == data.status and (memory.invalidated_reason or "") == data.reason:
        return memory  # the same verdict twice is a no-op, not a fresh stamp

    snapshot_previous(db, memory, f"{data.status}: {data.reason}"[:500], actor)
    now = datetime.now(timezone.utc).isoformat()
    memory.status = data.status
    memory.invalidated_at = now
    memory.invalidated_reason = data.reason
    memory.updated_at = now
    if successor is not None:
        edge = db.query(MemoryRelation).filter(
            MemoryRelation.memory_id_a == successor.id,
            MemoryRelation.memory_id_b == memory.id,
            MemoryRelation.relation_type == "supersedes").first()
        if edge is None:
            db.add(MemoryRelation(memory_id_a=successor.id, memory_id_b=memory.id,
                                  relation_type="supersedes", created_at=now))
    db.commit()
    _write_through(db, memory)
    return memory


@router.post("/{slug}/verify", response_model=MemoryResponse)
def verify_memory(slug: str, data: MemoryVerdict, request: Request,
                  db: Session = Depends(get_db)):
    """Record what a re-check concluded. ``verdict: ok`` is the only way back.

    Recovery is an act of checking, never an act of editing — so neither PUT nor
    a rewritten body clears an invalidation. A successful re-check also drops the
    supersedes edge the contradiction created: the graph is a working surface,
    and an edge nobody still believes in misleads more than it documents. The
    version snapshot keeps the evidence of the disagreement.
    """
    _require_auth(request, db)
    _guard_write()
    memory = _resolve_memory(db, slug)
    now = datetime.now(timezone.utc).isoformat()

    if data.verdict != "active" and not data.reason.strip():
        raise HTTPException(status_code=422,
                            detail=f"verdict {data.verdict!r} needs a reason — "
                                   "an unexplained downgrade is just noise")

    was_invalidated = memory.status in INVALIDATED_STATUSES
    if data.verdict == "active":
        memory.status = "active"
        memory.verified_at = now
        if was_invalidated:
            memory.invalidated_at = None
            memory.invalidated_reason = None
            memory.updated_at = now
            db.query(MemoryRelation).filter(
                MemoryRelation.memory_id_b == memory.id,
                MemoryRelation.relation_type == "supersedes").delete(synchronize_session=False)
    else:
        memory.status = data.verdict
        memory.invalidated_at = now
        memory.invalidated_reason = data.reason.strip()[:2000]
        memory.updated_at = now

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

