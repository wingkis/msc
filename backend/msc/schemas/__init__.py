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


class StatsResponse(BaseModel):
    total: int
    by_category: dict[str, int]
    by_priority: dict[str, int]
    by_source: dict[str, int]
