"""Pydantic schemas for API request/response."""

from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel, Field


class TagBase(BaseModel):
    name: str

class TagResponse(TagBase):
    id: int
    model_config = {"from_attributes": True}


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

class SearchResult(BaseModel):
    id: int
    slug: str
    name: str
    description: str
    category: str
    priority: str
    tags: List[TagResponse] = []
    score: float


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
