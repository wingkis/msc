"""Pydantic schemas for API request/response."""

from __future__ import annotations
import json
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


class TagBase(BaseModel):
    name: str

class TagResponse(TagBase):
    id: int
    model_config = {"from_attributes": True}


MEMORY_STATUSES = ("active", "stale", "contradicted")
# The kinds of pointer a later reader can actually re-check. A memory body is a
# historical observation; a ref is the handle that lets someone prove or kill it.
# `git:<sha>` `file:<path>` `url:<endpoint>` `mem:<slug>` `cmd:<line>`
# `host:<name>`.
REF_KINDS = ("git", "file", "url", "mem", "cmd", "host")


def normalize_refs(values) -> List[str]:
    """Validate and de-duplicate refs; anything without a known prefix raises.

    Raising here (rather than filtering silently) means a typo'd ref becomes a
    422 that names the allowed prefixes — an unverifiable ref is worse than no
    ref, because it reads like a guarantee.
    """
    out: List[str] = []
    for raw in values or []:
        ref = str(raw).strip()[:500]
        if not ref:
            continue
        kind, _, target = ref.partition(":")
        if kind.lower() not in REF_KINDS or not target.strip():
            raise ValueError(
                f"ref {raw!r} needs a '<kind>:<target>' form with kind one of: "
                f"{', '.join(REF_KINDS)}"
            )
        ref = f"{kind.lower()}:{target.strip()}"
        if ref not in out:
            out.append(ref)
    return out


def dump_refs(values) -> str:
    return json.dumps(list(values or []), ensure_ascii=False)


def parse_refs(stored) -> List[str]:
    """Read back the JSON column defensively — a broken blob must not 500 a read."""
    if not stored:
        return []
    if isinstance(stored, (list, tuple)):
        return [str(v) for v in stored]
    try:
        data = json.loads(stored)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in data] if isinstance(data, list) else []


class MemoryCreate(BaseModel):
    name: str = Field(..., min_length=1)
    # Agents usually want a stable key of their own; without this the server
    # always derives the slug from the name.
    slug: Optional[str] = Field(None, min_length=1, max_length=200)
    description: str = ""
    body: str = ""
    priority: str = "medium"
    category: str = "reference"
    lifespan: str = "permanent"
    branch: str = ""
    tags: List[str] = []
    related_slugs: List[str] = []
    refs: List[str] = []

    @field_validator("refs", mode="before")
    @classmethod
    def _check_refs(cls, v):
        return normalize_refs(v)


class BatchMemories(BaseModel):
    memories: List[MemoryCreate] = Field(..., max_length=200)
    upsert: bool = Field(True, description="Update in place when the slug already exists")

class MemoryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    body: Optional[str] = None
    priority: Optional[str] = None
    category: Optional[str] = None
    lifespan: Optional[str] = None
    branch: Optional[str] = None
    tags: Optional[List[str]] = None
    related_slugs: Optional[List[str]] = None
    refs: Optional[List[str]] = None

    @field_validator("refs", mode="before")
    @classmethod
    def _check_refs(cls, v):
        return None if v is None else normalize_refs(v)

class MemoryResponse(BaseModel):
    id: int
    slug: str
    name: str
    description: str
    body: str
    priority: str
    category: str
    lifespan: str
    branch: str
    tags: List[TagResponse] = []
    created_at: str
    updated_at: str
    archived: int
    source: str
    refs: List[str] = []
    status: str = "active"
    invalidated_at: Optional[str] = None
    invalidated_reason: Optional[str] = None
    verified_at: Optional[str] = None

    @field_validator("refs", mode="before")
    @classmethod
    def _refs_from_column(cls, v):
        return parse_refs(v)

    @field_validator("status", mode="before")
    @classmethod
    def _status_from_column(cls, v):
        # Columns added by ALTER TABLE are NULL on pre-existing rows.
        return v or "active"

    model_config = {"from_attributes": True}


class VersionResponse(BaseModel):
    id: int
    memory_id: int
    version_num: int
    name: str
    description: Optional[str]
    body: str
    priority: Optional[str]
    category: Optional[str]
    tags_snapshot: Optional[str]
    change_summary: Optional[str]
    created_at: str
    created_by: Optional[str] = None
    model_config = {"from_attributes": True}


class GraphNode(BaseModel):
    id: int
    slug: str
    name: str
    category: str
    priority: str
    color: str

class GraphEdge(BaseModel):
    source: int
    target: int
    relation_type: str

class GraphResponse(BaseModel):
    nodes: List[GraphNode]
    edges: List[GraphEdge]
    # A degree cap silently hides edges, so say how many: an 84-node memory set
    # carries ~2.3k auto-linked edges and hairballs the layout completely.
    edges_total: int = 0
    edges_dropped: int = 0
    max_degree: int = Field(0, description="the cap that was applied; 0 = unlimited")
    # Every relation type present with its edge count, so a caller can decide
    # what to hide instead of guessing which kinds the auto-linker wrote.
    edges_by_type: Dict[str, int] = Field(default_factory=dict)
    relations: List[str] = Field(default_factory=list, description="types the caller asked for; empty = all")


class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    key: str

class ApiKeyCreate(BaseModel):
    prefix: str
    route_type: str = "full"
    route_value: Optional[str] = None
    description: str = ""
    read_access: bool = True
    write_access: bool = True

class PrefixRouteCreate(BaseModel):
    prefix: str
    route_type: str
    route_value: Optional[str] = None
    read_access: bool = True
    write_access: bool = True
    description: str = ""

class BranchCreate(BaseModel):
    name: str
    parent_id: Optional[int] = None
    description: str = ""

class SyncConflictResolve(BaseModel):
    source: str

class SearchRequest(BaseModel):
    q: str = ""
    tags: Optional[List[str]] = None
    categories: Optional[List[str]] = None
    priorities: Optional[List[str]] = None
    branches: Optional[List[str]] = None
    limit: int = 20
    offset: int = 0
    include_invalidated: bool = False

class SearchResult(BaseModel):
    id: int
    slug: str
    name: str
    description: str
    category: str
    priority: str
    tags: List[TagResponse] = []
    score: float


class MemoryInvalidate(BaseModel):
    """Mark a memory as contradicted by a re-check, optionally naming its successor."""
    reason: str = Field(..., min_length=1, max_length=2000)
    status: str = Field("contradicted", description="stale | contradicted")
    superseded_by: Optional[str] = Field(None, description="Slug of the memory that replaces this one")

    @field_validator("status")
    @classmethod
    def _check_status(cls, v):
        if v not in ("stale", "contradicted"):
            raise ValueError(f"invalid status {v!r}; allowed: stale, contradicted")
        return v


class MemoryVerdict(BaseModel):
    """What a re-check concluded. `ok` clears an earlier invalidation."""
    verdict: str = Field("ok", description="ok | stale | contradicted")
    reason: str = Field("", max_length=2000)

    @field_validator("verdict")
    @classmethod
    def _check_verdict(cls, v):
        if v not in MEMORY_STATUSES and v != "ok":
            raise ValueError(f"invalid verdict {v!r}; allowed: ok, {', '.join(MEMORY_STATUSES)}")
        return "active" if v == "ok" else v


class ProbeItem(BaseModel):
    ref: str
    kind: str
    verdict: str          # ok | missing | unchecked
    detail: str = ""


class PreflightItem(BaseModel):
    slug: str
    status: str
    refs: List[str] = []
    probes: List[ProbeItem] = []
    verdict: str          # active | contradicted | stale | uncheckable
    detail: str = ""


class PreflightResponse(BaseModel):
    checked: int = 0
    hidden: int = 0
    budget_ms: int = 0
    items: List[PreflightItem] = []


class ModelConfigUpdate(BaseModel):
    """Partial update of the model card. Omitted fields keep their value.

    ``api_key`` is the exception: sending "" clears it, sending nothing keeps it,
    because the API never returns the stored key in full.
    """
    label: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    enabled: Optional[bool] = None
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(None, ge=64, le=16384)
    timeout_seconds: Optional[int] = Field(None, ge=5, le=1800)
    max_input_chars: Optional[int] = Field(None, ge=500, le=200000)
    extra_json: Optional[str] = None
    prompt_template: Optional[str] = None
    auto_enabled: Optional[bool] = None
    auto_time: Optional[str] = None
    auto_timezone: Optional[str] = None
    default_category: Optional[str] = None
    default_priority: Optional[str] = None
    archive_sources: Optional[bool] = None


class SummarizeRequest(BaseModel):
    """What the digest should cover. ``slugs`` for a manual pick, ``date`` for a day."""
    slugs: List[str] = Field(default_factory=list, max_length=300)
    date: Optional[str] = Field(None, description="YYYY-MM-DD in the card's timezone")
    query: Optional[str] = Field(None, max_length=200,
                                 description="the search that produced slugs; goes into the prompt and the digest body")
    name: Optional[str] = Field(None, max_length=200)
    category: Optional[str] = None
    priority: Optional[str] = None
    tags: Optional[List[str]] = None
    instruction: Optional[str] = Field(None, max_length=500)
    archive_sources: Optional[bool] = Field(None, description="Soft-delete the source memories; omit = use the card default")
    dry_run: bool = Field(False, description="Preview the digest without writing anything")


class StatsResponse(BaseModel):
    total: int
    by_category: dict[str, int]
    by_priority: dict[str, int]
    by_source: dict[str, int]


# ── RAG ──────────────────────────────────────────────────────────────

class RagSettingsUpdate(BaseModel):
    """Fusion tuning; omitted keys keep their value. All positive integers."""
    recall_top: Optional[int] = Field(None, ge=1, le=500)
    rerank_topn: Optional[int] = Field(None, ge=1, le=100)
    rrf_k: Optional[int] = Field(None, ge=1, le=1000)
    embed_batch: Optional[int] = Field(None, ge=1, le=64)


class EchoRequest(BaseModel):
    slug: str = Field(..., min_length=1)
    instruction: Optional[str] = Field(None, max_length=300)
    # Persist the echo as a markdown memory file. Off by default (the endpoint
    # stays read-only unless you ask); the Web UI turns it on.
    save: bool = False
    # Base-relative filename, default "{slug}-回响". No absolute paths, no "..".
    save_path: Optional[str] = Field(None, max_length=200)


class RagImportEntry(BaseModel):
    # One of slug / memory_id identifies the target; vec is base64 little-
    # endian float32, exactly dim*4 bytes.
    slug: Optional[str] = None
    memory_id: Optional[int] = None
    dim: int = Field(..., ge=1, le=65536)
    vec: str = Field(..., min_length=8)


class RagImportRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=200)
    # Bounded so one admin POST cannot balloon into gigabytes of base64;
    # a 50k-memory library imports in a handful of calls.
    entries: List[RagImportEntry] = Field(..., max_length=5000)


# ── Photos (msc-pic) ─────────────────────────────────────────────────

class PicSourceCreate(BaseModel):
    """One photo source card. Which fields matter depends on kind:
    local→root_path, smb→host/share/subpath, webdav→url."""
    name: str = Field(..., min_length=1, max_length=100)
    kind: str = Field("local", pattern="^(local|smb|webdav)$")
    root_path: str = Field("", max_length=500)
    host: str = Field("", max_length=200)
    share: str = Field("", max_length=200)
    subpath: str = Field("", max_length=500)
    url: str = Field("", max_length=500)
    username: str = Field("", max_length=200)
    password: str = Field("", max_length=500)


class PicSourceUpdate(BaseModel):
    """Partial edit; like the model cards, an omitted password keeps the
    stored one and "" clears it."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    kind: Optional[str] = Field(None, pattern="^(local|smb|webdav)$")
    root_path: Optional[str] = Field(None, max_length=500)
    host: Optional[str] = Field(None, max_length=200)
    share: Optional[str] = Field(None, max_length=200)
    subpath: Optional[str] = Field(None, max_length=500)
    url: Optional[str] = Field(None, max_length=500)
    username: Optional[str] = Field(None, max_length=200)
    password: Optional[str] = Field(None, max_length=500)
    enabled: Optional[bool] = None


class PicSettingsUpdate(BaseModel):
    """Pipeline tuning; omitted keys keep their value. All positive integers."""
    scan_seconds: Optional[int] = Field(None, ge=1, le=300)
    thumb_batch: Optional[int] = Field(None, ge=1, le=200)
    rescan_hours: Optional[int] = Field(None, ge=1, le=8760)
    thumb_px: Optional[int] = Field(None, ge=64, le=2048)
    caption_batch: Optional[int] = Field(None, ge=1, le=32)
    embed_batch: Optional[int] = Field(None, ge=1, le=64)
    caption_paused: Optional[int] = Field(None, ge=0, le=1)  # 0/1 switch
    caption_burst: Optional[int] = Field(None, ge=0, le=1)   # 0/1 能力全开 switch
    burst_seconds: Optional[int] = Field(None, ge=15, le=900)  # budget per pass
    caption_workers: Optional[int] = Field(None, ge=1, le=64)  # vision calls at once


class PicItemsBatch(BaseModel):
    """Bulk gallery actions. archive/restore are soft (never touch the
    originals); retry re-queues failed photos; forget deletes only the
    derived rows (thumb/vectors) for vanished photos; unmissing brings a
    hidden-but-present photo back into the gallery; ignore parks a failed
    photo out of the queue (reason kept, retry undoes it)."""
    ids: List[int] = Field(..., min_length=1, max_length=2000)
    action: str = Field(..., pattern="^(archive|restore|retry|forget|unmissing|ignore)$")


class PicItemsPurge(BaseModel):
    """The one truly destructive call: deletes the ORIGINAL files. confirm
    is a hard gate — the UI additionally makes the human type DELETE."""
    ids: List[int] = Field(..., min_length=1, max_length=1000)
    confirm: bool = False
