"""ORM models."""

from sqlalchemy import (
    Column, Integer, String, Text, ForeignKey, LargeBinary,
    CheckConstraint, Float, UniqueConstraint,
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

    # Provenance. A memory is a historical observation, not a verified fact, so
    # the parts of it that a later reader can re-check are stored apart from the
    # prose: refs holds checkable pointers (git:/file:/url:/mem:/cmd:/host:),
    # status says whether a re-check has since contradicted it, and verified_at
    # records the last time something confirmed it.
    refs = Column(Text, default="")            # JSON array of "kind:value"
    # Nullable on purpose: the upgrade path is ALTER TABLE ADD COLUMN, which
    # cannot carry a NOT NULL default, so an existing database has NULL here and
    # a fresh one would pretend otherwise. Reads are NULL-safe (validators +
    # _live_status_pred), and ensure_provenance() backfills the values.
    status = Column(String, default="active")
    invalidated_at = Column(String, nullable=True)
    invalidated_reason = Column(Text, nullable=True)
    verified_at = Column(String, nullable=True)

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
        # Only fresh databases get this constraint: ALTER TABLE ADD COLUMN (the
        # upgrade path for an existing db) cannot add a table-level CHECK, so the
        # API validates the status vocabulary as well. See _validate_vocab.
        CheckConstraint("status IN ('active','stale','contradicted')", name="ck_status"),
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
            "relation_type IN ('related','depends_on','supersedes','seealso','keyword','summarizes')",
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


class ModelConfig(Base):
    """The model cards: one row per purpose, edited in place from the Web UI.

    .env only seeds the first row of each purpose; after that the database
    wins, so pointing MSC at a different local model is a page refresh rather
    than a rebuild. ``chat`` digests memories, ``embed``/``rerank`` power
    semantic search, ``whisper`` transcribes voice notes.
    """

    __tablename__ = "model_configs"

    id = Column(Integer, primary_key=True)
    # chat | embed | rerank | whisper — enforced in the service layer because
    # SQLite cannot ALTER a CHECK onto an existing table. The migration adds a
    # UNIQUE index so each purpose has at most one card.
    purpose = Column(String, default="chat")
    label = Column(String, default="本地模型")
    base_url = Column(String, default="")
    model = Column(String, default="")
    api_key = Column(String, default="")
    enabled = Column(Integer, default=1)

    temperature = Column(Float, default=0.2)
    max_tokens = Column(Integer, default=1500)
    timeout_seconds = Column(Integer, default=180)
    max_input_chars = Column(Integer, default=12000)
    # Verbatim JSON merged into the chat request, for provider-specific switches
    # (turning thinking off, reasoning effort, ...).
    extra_json = Column(String, default="")
    prompt_template = Column(String, default="")

    auto_enabled = Column(Integer, default=0)
    auto_time = Column(String, default="23:30")  # HH:MM, in auto_timezone
    auto_timezone = Column(String, default="")   # empty = container local time
    default_category = Column(String, default="project")
    default_priority = Column(String, default="medium")
    archive_sources = Column(Integer, default=0)

    # Embedding cards: dimension reported by the probe button, so the UI can
    # show "1024 维" before anything is indexed.
    probed_dim = Column(Integer, default=0)

    last_ok_at = Column(String)
    last_error = Column(String)
    updated_at = Column(String, nullable=False)


class Embedding(Base):
    """One vector per (memory, model). Keying by model name means switching
    embedding providers writes new rows instead of overwriting old ones —
    switching back costs nothing, and a reindex can prune the old model after.
    """

    __tablename__ = "embeddings"

    memory_id = Column(Integer, ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True)
    model = Column(String, primary_key=True)
    dim = Column(Integer, nullable=False)
    vec = Column(LargeBinary, nullable=False)  # float32 little-endian, dim*4 bytes
    embedded_at = Column(String, nullable=False)


class RagDirty(Base):
    """Memories whose text changed and need (re)embedding.

    Written by the same code paths that write the memory itself, consumed by
    rag_worker. INSERT OR IGNORE keeps marking free; the FK cascade drops a
    mark when the memory is hard-deleted.
    """

    __tablename__ = "rag_dirty"

    memory_id = Column(Integer, ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True)
    marked_at = Column(String, nullable=False)


class RagCache(Base):
    """Day-scoped results that cost a model call (the daily pick)."""

    __tablename__ = "rag_cache"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)  # JSON
    created_at = Column(String, nullable=False)


class RagSetting(Base):
    """Fusion tuning (recall_top, rrf_k, ...) — env seeds, DB wins, same as cards."""

    __tablename__ = "rag_settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class PicSource(Base):
    """Photo source card — the pic cousin of the model card.

    A row is one folder or remote share the user linked from the Web UI:
    a local path inside MSC_PIC_ROOTS, an SMB share, or a WebDAV URL.
    Credentials live here (masked in the API, same policy as api_key).
    ``scan_cursor`` is the resumable scan pointer: the last rel_path the
    walker already inserted, NULL when a walk completed. Interrupting a
    thousand-photo scan mid-walk costs nothing — the next tick resumes.
    """

    __tablename__ = "pic_sources"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False, default="local")   # local | smb | webdav
    # local: absolute path inside pic_roots. smb: host+share+subpath. webdav: url.
    root_path = Column(String, default="")
    host = Column(String, default="")
    share = Column(String, default="")
    subpath = Column(String, default="")
    url = Column(String, default="")
    username = Column(String, default="")
    password = Column(String, default="")
    enabled = Column(Integer, default=1)

    # idle | scanning | paused | error — paused means "user stopped it", the
    # scan cursor survives and resume picks the walk back up.
    state = Column(String, default="idle")
    scan_cursor = Column(String, nullable=True)
    scan_epoch = Column(Integer, default=0)   # bumped by /scan to re-request a walk
    # Walk-session stamp: the GC cutoff for missing detection. Set when a
    # walk starts from scratch; items whose originals were not seen since
    # this moment when the walk COMPLETES are marked missing.
    scan_started_at = Column(String, nullable=True)
    last_scan_at = Column(String, nullable=True)
    last_error = Column(String, nullable=True)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)

    __table_args__ = (
        CheckConstraint("kind IN ('local','smb','webdav')", name="ck_pic_source_kind"),
        CheckConstraint("state IN ('idle','scanning','paused','error')", name="ck_pic_source_state"),
    )


class PicItem(Base):
    """One photo, one row — and the state column IS the pipeline queue.

    discovered → thumb_ready → captioned → embedded, plus failed. Unlike
    memories (whose text changes asynchronously behind a rag_dirty queue),
    a photo's work state lives on the row itself: the pic_worker picks
    anything lagging behind, so every stage commits independently and a
    container restart resumes exactly where the last commit was.
    Transient source errors back off via retry_at instead of failing the photo.
    """

    __tablename__ = "pic_items"

    # sqlite_autoincrement: without it SQLite hands a deleted max id to the
    # next insert, and thumbnails (file path, API URL, browser cache) all key
    # a tile by id — a reused id would paint the deleted photo's cached tile
    # over the new row. AUTOINCREMENT makes the counter monotonic for good.
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("pic_sources.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    # POSIX path relative to the source root; unique per source so a resumed
    # or repeated walk inserts nothing twice.
    rel_path = Column(String, nullable=False)
    origin_uri = Column(String, unique=True, nullable=False)  # pic://<sid>/<rel_path>
    name = Column(String, nullable=False)
    ext = Column(String, default="")
    size_bytes = Column(Integer, default=0)
    mtime = Column(String, default="")
    # blake2b(size + first 1MB): dedup, burst-shot caption reuse, broken-file triage.
    content_hash = Column(String, default="", index=True)
    width = Column(Integer, default=0)
    height = Column(Integer, default=0)
    taken_at = Column(String, index=True, nullable=True)
    taken_at_src = Column(String, default="")  # exif | exif_naive | mtime
    kind = Column(String, default="photo")      # photo | screenshot
    caption = Column(Text, default="")
    tags = Column(String, default="")           # comma separated
    thumb_path = Column(String, default="")     # pic_dir-relative: thumbs/<sid>/<id>.webp

    state = Column(String, nullable=False, default="discovered")
    error_count = Column(Integer, default=0)
    retry_at = Column(String, nullable=True)
    last_error = Column(String, nullable=True)
    # Source-side lifecycle: every walk touches last_seen_at; when a walk
    # COMPLETES and an item was not seen, missing=1 hides it (thumbnail and
    # caption survive — the user decides: keep, forget, or resync). A later
    # walk that sees the file again clears the flag.
    last_seen_at = Column(String, nullable=True)
    missing = Column(Integer, default=0)

    archived = Column(Integer, default=0)       # trash bin, same idea as Memory.archived
    deleted_at = Column(String, nullable=True)
    # "这条失败我不再管了": a failed row keeps state='failed' and its
    # last_error (the reason stays readable), but leaves the queue and the
    # gallery. Kept as a flag rather than a new state because state carries a
    # CHECK constraint, and because ignoring is reversible: retry clears it.
    ignored = Column(Integer, default=0)
    discovered_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)

    __table_args__ = (
        UniqueConstraint("source_id", "rel_path", name="uq_pic_source_path"),
        CheckConstraint(
            "state IN ('discovered','thumb_ready','captioned','embedded','failed')",
            name="ck_pic_state",
        ),
        CheckConstraint("kind IN ('photo','screenshot')", name="ck_pic_kind"),
        # Table-level SQLite dialect kwarg: a plain rowid lets SQLite hand a
        # deleted max id to the next insert, and thumbnails (file path, API
        # URL, 24 h browser cache) all key a tile by id — a reused id would
        # paint the deleted photo's cached tile over the new row.
        {"sqlite_autoincrement": True},
    )


class PicVector(Base):
    """Photo caption embeddings — keyed by model exactly like Embedding.

    Deliberately its own table: the embeddings self-heal SQL joins memories,
    so photo ids living in there would silently cross-pollute memory search.
    Photos are searched only by /pic/search; memories never see these rows.
    """

    __tablename__ = "pic_vectors"

    pic_id = Column(Integer, ForeignKey("pic_items.id", ondelete="CASCADE"), primary_key=True)
    model = Column(String, primary_key=True)
    dim = Column(Integer, nullable=False)
    vec = Column(LargeBinary, nullable=False)  # float32 little-endian, dim*4 bytes
    embedded_at = Column(String, nullable=False)


class PicSetting(Base):
    """Pic pipeline tuning (thumb_batch, scan_seconds, ...) — env seeds, DB wins."""

    __tablename__ = "pic_settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class SummaryRun(Base):
    """One row per digest attempt — the audit trail behind the model card.

    Failed attempts are recorded too: that is what stops the daily worker from
    hammering an unreachable model every minute.
    """

    __tablename__ = "summary_runs"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String, nullable=False, default="manual")  # manual | auto
    period = Column(String, default="")                      # YYYY-MM-DD for daily digests
    slug = Column(String)                                    # memory created (NULL if none)
    model = Column(String, default="")
    source_count = Column(Integer, default=0)
    archived_count = Column(Integer, default=0)
    ok = Column(Integer, default=1)
    error = Column(String)
    duration_ms = Column(Integer, default=0)
    created_by = Column(String, default="")
    created_at = Column(String, nullable=False)

    __table_args__ = (
        CheckConstraint("kind IN ('manual','auto')", name="ck_summary_kind"),
    )
