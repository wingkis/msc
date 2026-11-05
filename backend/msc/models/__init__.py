"""ORM models."""

from sqlalchemy import (
    Column, Integer, String, Text, ForeignKey,
    CheckConstraint, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from msc.database import Base


class Memory(Base):
    __tablename__ = "memories"

    # AUTOINCREMENT so a hard-deleted id is never handed to a new memory;
    # remaining memories keep their ids no matter what gets deleted.
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    slug = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(String, default="")
    body = Column(Text, default="")
    priority = Column(String, nullable=False)
    category = Column(String, nullable=False)
    lifespan = Column(String, default="permanent")
    branch = Column(String, default="")
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)
    locked_at = Column(String, nullable=True)
    locked_by = Column(String, nullable=True)
    archived = Column(Integer, default=0)
    source = Column(String, default="msc")
    last_sync_hash = Column(String, nullable=True)
    owner_id = Column(Integer, nullable=True)

    tags = relationship("Tag", secondary="memory_tags", back_populates="memories")
    versions = relationship("MemoryVersion", back_populates="memory", lazy="select")
    relations_out = relationship(
        "MemoryRelation",
        foreign_keys="MemoryRelation.memory_id_a",
        back_populates="memory_a",
    )
    relations_in = relationship(
        "MemoryRelation",
        foreign_keys="MemoryRelation.memory_id_b",
        back_populates="memory_b",
    )

    __table_args__ = (
        CheckConstraint("priority IN ('low','medium','high','critical')", name="ck_priority"),
        CheckConstraint("category IN ('config','project','user','knowledge','reference')", name="ck_category"),
        CheckConstraint("lifespan IN ('temporary','permanent')", name="ck_lifespan"),
        CheckConstraint("source IN ('msc','imported','claude')", name="ck_source"),
    )


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False, index=True)

    memories = relationship("Memory", secondary="memory_tags", back_populates="tags")


class MemoryTag(Base):
    __tablename__ = "memory_tags"

    memory_id = Column(Integer, ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True)
    tag_id = Column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


class MemoryVersion(Base):
    __tablename__ = "memory_versions"

    id = Column(Integer, primary_key=True, index=True)
    memory_id = Column(Integer, ForeignKey("memories.id", ondelete="CASCADE"), nullable=False, index=True)
    version_num = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    description = Column(String)
    body = Column(Text, nullable=False)
    priority = Column(String)
    category = Column(String)
    tags_snapshot = Column(Text)  # JSON
    change_summary = Column(String)
    created_at = Column(String, nullable=False)
    created_by = Column(String)

    memory = relationship("Memory", back_populates="versions")

    __table_args__ = (
        UniqueConstraint("memory_id", "version_num", name="uq_memory_version"),
    )


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String)
    role = Column(String, default="viewer", nullable=False)
    api_key_hash = Column(String, nullable=False)
    api_key_prefix = Column(String)
    created_at = Column(String, nullable=False)
    last_login = Column(String)
    is_active = Column(Integer, default=1)

    __table_args__ = (
        CheckConstraint("role IN ('admin','editor','viewer')", name="ck_role"),
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    key_hash = Column(String, unique=True, nullable=False)
    key_prefix = Column(String, nullable=False, index=True)
    description = Column(String)
    created_at = Column(String, nullable=False)
    last_used = Column(String)
    is_active = Column(Integer, default=1)


class PrefixRoute(Base):
    __tablename__ = "prefix_routes"

    id = Column(Integer, primary_key=True, index=True)
    prefix = Column(String, unique=True, nullable=False, index=True)
    route_type = Column(String, nullable=False)
    route_value = Column(String)  # JSON
    read_access = Column(Integer, default=1)
    write_access = Column(Integer, default=1)
    description = Column(String)

    __table_args__ = (
        CheckConstraint("route_type IN ('full','branch','category','tag','host')", name="ck_route_type"),
    )


class MemoryBranch(Base):
    __tablename__ = "memory_branches"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False, index=True)
    parent_id = Column(Integer, ForeignKey("memory_branches.id"))
    description = Column(String)


class MemoryRelation(Base):
    __tablename__ = "memory_relations"

    memory_id_a = Column(Integer, ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True)
    memory_id_b = Column(Integer, ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True)
    relation_type = Column(String, default="related", nullable=False)
    created_at = Column(String, nullable=False)

    memory_a = relationship("Memory", foreign_keys=[memory_id_a], back_populates="relations_out")
    memory_b = relationship("Memory", foreign_keys=[memory_id_b], back_populates="relations_in")

    __table_args__ = (
        CheckConstraint(
            "relation_type IN ('related','depends_on','supersedes','seealso','keyword')",
            name="ck_relation_type",
        ),
    )


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id = Column(Integer, primary_key=True, index=True)
    memory_id = Column(Integer, ForeignKey("memories.id"))
    api_key_prefix = Column(String)
    direction = Column(String)
    status = Column(String)
    detail = Column(String)
    remote_ip = Column(String)
    created_at = Column(String, nullable=False)


class SyncConflict(Base):
    __tablename__ = "sync_conflicts"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, nullable=False, index=True)
    source = Column(String, nullable=False)
    reason = Column(String)
    resolved = Column(Integer, default=0)
    resolved_at = Column(String)
    resolved_by = Column(String)
    status = Column(String, default="open")
    created_at = Column(String, nullable=False)

    __table_args__ = (
        CheckConstraint("source IN ('db','filesystem','claude')", name="ck_source"),
    )
