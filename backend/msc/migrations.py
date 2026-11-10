"""Startup schema + data maintenance.

``Base.metadata.create_all()`` only creates missing tables — it never alters an
existing one. That silently broke upgrades: ``sync_conflicts.status`` was added
to the model, old databases kept their old shape, and every
``GET /api/v1/sync/conflicts`` died with "no such column".

Everything here is idempotent and safe to run on every startup.
"""

import logging

from sqlalchemy import inspect, text

from msc.database import Base, engine

_log = logging.getLogger("msc.migrations")

# SQLite types for the handful of column types this project uses.
_SQLITE_TYPE = {
    "INTEGER": "INTEGER", "TEXT": "TEXT", "REAL": "REAL", "BOOLEAN": "INTEGER",
    "VARCHAR": "TEXT",
}


def _existing_columns(conn, table: str) -> dict:
    rows = conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
    return {row[1]: row for row in rows}


def add_missing_columns() -> list[str]:
    """ALTER TABLE ADD COLUMN for anything the models declare and the DB lacks."""
    added: list[str] = []
    tables = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.tables.values():
            if table.name not in tables:
                continue  # create_all handles whole missing tables
            existing = _existing_columns(conn, table.name)
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = str(column.type).upper()
                for key, sqlite_type in _SQLITE_TYPE.items():
                    if col_type.startswith(key):
                        col_type = sqlite_type
                        break
                else:
                    col_type = "TEXT"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
                added.append(f"{table.name}.{column.name}")
                _log.info("schema: added column %s.%s", table.name, column.name)
    return added


def normalize_existing_tags() -> int:
    """Repair tag names wrecked by an old importer.

    Frontmatter like ``tags: ['codex, sandbox']`` used to be stored as one
    literal string, so the database filled up with ``['codex``, ``sandbox']`` and
    several spellings of the same tag. When any name is dirty the whole tag set
    is rebuilt from ``normalize_tags`` output, which handles merging, splitting
    and the UNIQUE(name) constraint in one pass. A clean tag table is left
    completely untouched.
    """
    from msc.text import normalize_tags

    with engine.begin() as conn:
        tags = conn.execute(text("SELECT id, name FROM tags")).fetchall()
        dirty = [name for _, name in tags if normalize_tags([name]) != [name]]
        if not dirty:
            return 0

        name_by_id = {tag_id: name for tag_id, name in tags}
        per_memory: dict[int, set] = {}
        for memory_id, tag_id in conn.execute(text("SELECT memory_id, tag_id FROM memory_tags")):
            for clean in normalize_tags([name_by_id.get(tag_id, "")]):
                per_memory.setdefault(memory_id, set()).add(clean)

        conn.execute(text("DELETE FROM memory_tags"))
        conn.execute(text("DELETE FROM tags"))
        conn.execute(text("DELETE FROM sqlite_sequence WHERE name = 'tags'"))

        tag_ids: dict[str, int] = {}
        for name in sorted({clean for names in per_memory.values() for clean in names}):
            conn.execute(text("INSERT INTO tags (name) VALUES (:p0)"), {"p0": name})
            tag_ids[name] = conn.execute(text("SELECT last_insert_rowid()")).scalar()

        for memory_id, names in per_memory.items():
            for name in names:
                conn.execute(
                    text("INSERT OR IGNORE INTO memory_tags (memory_id, tag_id) VALUES (:p0, :p1)"),
                    {"p0": memory_id, "p1": tag_ids[name]},
                )

    return len(dirty)


def ensure_branch_records() -> list[str]:
    """Register every branch that memories actually use.

    ``GET /api/v1/branches`` read the registry only, so a memory tagged
    ``branch=main`` never made the branch discoverable.
    """
    created: list[str] = []
    with engine.begin() as conn:
        used = [row[0] for row in conn.execute(
            text("SELECT DISTINCT branch FROM memories "
                 "WHERE branch IS NOT NULL AND branch <> '' ORDER BY branch"))]
        known = {row[0] for row in conn.execute(text("SELECT name FROM memory_branches"))}
        for name in used:
            if name not in known:
                conn.execute(text("INSERT INTO memory_branches (name, description) "
                                  "VALUES (:p0, 'auto-registered from memories')"), {"p0": name})
                created.append(name)
    return created


def branch_counts() -> dict[str, int]:
    """How many live memories use each branch name."""
    with engine.connect() as conn:
        return {row[0]: row[1] for row in conn.execute(
            text("SELECT branch, count(*) FROM memories "
                 "WHERE branch IS NOT NULL AND branch <> '' AND archived = 0 "
                 "GROUP BY branch"))}


def clean_existing_bodies() -> list[str]:
    """Drop frontmatter that an old importer left sitting inside a body.

    ``test-memory.md`` is the evidence: a file whose metadata block was written
    twice got imported whole, so the API returned ``name: ...`` lines as prose.
    Reading is already guarded, but rows written before that fix stay dirty, so
    the repair goes back out to disk too (file + index + sync hash).

    Archived memories are skipped: their row survives on purpose while the file
    is gone, and touching one would resurrect a deleted memory on disk.
    """
    from msc.database import SessionLocal
    from msc.filesystem.write_through import write_through
    from msc.models import Memory
    from msc.text import clean_body

    fixed: list[str] = []
    with SessionLocal() as db:
        for memory in db.query(Memory).filter(Memory.archived == 0).all():
            body = memory.body or ""
            cleaned = clean_body(body)
            if cleaned == body:
                continue
            memory.body = cleaned
            try:
                write_through(db, memory)
            except Exception as exc:  # the DB fix matters more than the file
                _log.warning("cleaned %s in the database but not on disk: %s", memory.slug, exc)
                db.commit()
            fixed.append(memory.slug)
    return fixed


def ensure_relation_types() -> bool:
    """Widen the relation_type CHECK constraint.

    'keyword' came from auto-linking, 'summarizes' from the model card's
    digests. SQLite cannot ALTER a CHECK, so the table is rebuilt in place.
    Idempotent: a no-op once the constraint already allows every type.
    """
    required = ("keyword", "summarizes")
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE name='memory_relations' AND type='table'"
        )).fetchone()
        existing = row[0] or ""
        if row is None or all(need in existing for need in required):
            return False
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(text("""
            CREATE TABLE memory_relations_new (
                memory_id_a INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                memory_id_b INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                relation_type TEXT NOT NULL DEFAULT 'related'
                    CHECK (relation_type IN ('related','depends_on','supersedes','seealso','keyword','summarizes')),
                created_at TEXT NOT NULL,
                PRIMARY KEY (memory_id_a, memory_id_b)
            )
        """))
        conn.execute(text("INSERT INTO memory_relations_new SELECT * FROM memory_relations"))
        conn.execute(text("DROP TABLE memory_relations"))
        conn.execute(text("ALTER TABLE memory_relations_new RENAME TO memory_relations"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
        _log.info("schema: memory_relations CHECK now allows %s", ", ".join(required))
        return True


def prune_version_history() -> int:
    """Keep only the newest version snapshot per memory.

    Writes now record a single snapshot (undo of the last change), so rows
    accumulated from older multi-version behaviour are dropped on startup.
    """
    with engine.begin() as conn:
        removed = conn.execute(text(
            "DELETE FROM memory_versions WHERE version_num < ("
            "SELECT MAX(version_num) FROM memory_versions mv "
            "WHERE mv.memory_id = memory_versions.memory_id)"
        )).rowcount
    if removed:
        _log.info("schema: pruned %d old version snapshots", removed)
    return removed


def ensure_model_purpose() -> int:
    """Give every card a purpose, then make purposes unique.

    Databases from 2.2.0 have a single row (the chat card) and no purpose
    column; add_missing_columns leaves it NULL, and the whole RAG layer keys
    its cards by purpose, so NULL has to become 'chat' before anything else
    looks at the table. The unique index is best-effort: it fails if someone
    hand-inserted duplicate purposes, and that must not stop startup.
    """
    with engine.begin() as conn:
        if "model_configs" not in set(inspect(engine).get_table_names()):
            return 0
        backfilled = conn.execute(text(
            "UPDATE model_configs SET purpose = 'chat' "
            "WHERE purpose IS NULL OR purpose = ''"
        )).rowcount
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_model_configs_purpose "
                              "ON model_configs(purpose)"))
        except Exception as exc:
            _log.warning("could not enforce unique model purposes: %s", exc)
        if backfilled:
            _log.info("schema: backfilled purpose on %d model card(s)", backfilled)
    return backfilled


def prune_broken_embeddings() -> int:
    """Drop malformed vectors and rows orphaned by hard deletes.

    A half-written or hand-mangled blob would poison every cosine scan with
    garbage, and SQLite foreign keys are not enabled on these connections
    (see msc.database), so the ON DELETE CASCADE never fires — an embedding
    or dirty mark for a purged memory has to be reaped here. Reindex brings
    anything that should still be indexed back.
    """
    with engine.begin() as conn:
        tables = set(inspect(engine).get_table_names())
        if "embeddings" not in tables:
            return 0
        removed = conn.execute(text(
            "DELETE FROM embeddings WHERE length(vec) <> dim * 4 OR dim <= 0"
        )).rowcount
        removed += conn.execute(text(
            "DELETE FROM embeddings WHERE memory_id NOT IN (SELECT id FROM memories)"
        )).rowcount
        if "rag_dirty" in tables:
            removed += conn.execute(text(
                "DELETE FROM rag_dirty WHERE memory_id NOT IN (SELECT id FROM memories)"
            )).rowcount
    if removed:
        _log.info("schema: pruned %d broken/orphaned embedding row(s)", removed)
    return removed


def prune_broken_pic_vectors() -> int:
    """Malformed or orphaned photo vectors — same reap as embeddings.

    photo ids are per-table, so a PicVector whose pic_id vanished (purged or
    source-deleted) must not linger in the cosine scan.
    """
    with engine.begin() as conn:
        if "pic_vectors" not in set(inspect(engine).get_table_names()):
            return 0
        removed = conn.execute(text(
            "DELETE FROM pic_vectors WHERE length(vec) <> dim * 4 OR dim <= 0"
        )).rowcount
        removed += conn.execute(text(
            "DELETE FROM pic_vectors WHERE pic_id NOT IN (SELECT id FROM pic_items)"
        )).rowcount
    if removed:
        _log.info("schema: pruned %d broken/orphaned pic vector(s)", removed)
    return removed


def sweep_orphan_thumbs() -> int:
    """webp tiles on disk whose pic_items row is gone (purged items, hard
    deletes). The row is the truth; the tile dir is a cache."""
    from pathlib import Path

    from msc.config import get_settings

    root = Path(get_settings().pic_dir) / "thumbs"
    if not root.is_dir():
        return 0
    with engine.connect() as conn:
        if "pic_items" not in set(inspect(engine).get_table_names()):
            return 0
        known = {f"{row[0]}.webp"
                 for row in conn.execute(text("SELECT id FROM pic_items"))}
    removed = 0
    for sid_dir in root.iterdir():
        if not sid_dir.is_dir():
            continue
        for tile in sid_dir.iterdir():
            if tile.suffix == ".webp" and tile.name not in known:
                try:
                    tile.unlink()
                    removed += 1
                except OSError:
                    continue
    if removed:
        _log.info("schema: swept %d orphaned thumbnail file(s)", removed)
    return removed


def ensure_pic_defaults() -> int:
    """ALTER TABLE ADD COLUMN leaves NULLs on existing rows, and our filters
    speak == 0: a NULL missing/error_count silently drops live photos out of
    the gallery AND out of every queue. Backfill the defaults once per column.
    """
    fixed = 0
    with engine.begin() as conn:
        if "pic_items" not in set(inspect(engine).get_table_names()):
            return 0
        fixed += conn.execute(text(
            "UPDATE pic_items SET missing = 0 WHERE missing IS NULL")).rowcount
        fixed += conn.execute(text(
            "UPDATE pic_items SET error_count = 0 WHERE error_count IS NULL")).rowcount
        fixed += conn.execute(text(
            "UPDATE pic_items SET archived = 0 WHERE archived IS NULL")).rowcount
    if fixed:
        _log.info("schema: backfilled %d default value(s) on pic_items", fixed)
    return fixed


PIC_INDEXES = (
    # The state column IS the ingest queue: every 15 s the worker asks "what is
    # still at stage X, oldest id first" for thumbs, captions and embeds. Without
    # this index that is a full table walk per stage per tick.
    ("idx_pic_items_queue",
     "CREATE INDEX IF NOT EXISTS idx_pic_items_queue "
     "ON pic_items (state, archived, missing, id)"),
    # _thumbs() walks one source at a time; the plain source_id index alone makes
    # it read every photo of that source on each pass.
    ("idx_pic_items_source_state",
     "CREATE INDEX IF NOT EXISTS idx_pic_items_source_state "
     "ON pic_items (source_id, state, archived, missing)"),
)


def ensure_pic_indexes() -> int:
    """Queue indexes on pic_items — idempotent, and a no-op if the tables
    aren't there (kill switch / brand-new database)."""
    with engine.begin() as conn:
        if "pic_items" not in set(inspect(engine).get_table_names()):
            return 0
        for _name, ddl in PIC_INDEXES:
            conn.execute(text(ddl))
        have = {row[0] for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'pic_items'"))}
    made = [name for name, _ in PIC_INDEXES if name in have]
    if made:
        _log.info("schema: pic_items queue indexes ready (%s)", ", ".join(made))
    return len(made)


def run_maintenance() -> dict:
    """Full startup pass. Returns a summary so it can be logged and reported."""
    from msc.fts import ensure_fts, fts_desync_report, rebuild_fts

    Base.metadata.create_all(bind=engine)
    summary = {
        "columns_added": add_missing_columns(),
        "tags_normalized": normalize_existing_tags(),
        "branches_registered": ensure_branch_records(),
        "bodies_cleaned": clean_existing_bodies(),
        "versions_pruned": prune_version_history(),
        "relation_types_updated": ensure_relation_types(),
        "model_purpose_backfilled": ensure_model_purpose(),
        "embeddings_pruned": prune_broken_embeddings(),
        "pic_defaults_backfilled": ensure_pic_defaults(),
        "pic_indexes": ensure_pic_indexes(),
        "pic_vectors_pruned": prune_broken_pic_vectors(),
        "pic_thumbs_swept": sweep_orphan_thumbs(),
    }
    ensure_fts()
    desync = fts_desync_report()
    if desync["missing"]:
        rebuild_fts()
        desync = fts_desync_report()
    summary["search_index"] = desync
    return summary


def health_detail() -> dict:
    """Cheap self-check for GET /api/v1/health."""
    from msc.fts import fts_desync_report

    try:
        with engine.connect() as conn:
            memories = conn.execute(text("SELECT count(*) FROM memories WHERE archived = 0")).scalar() or 0
        index = fts_desync_report()
        detail = {
            "database": "ok",
            "memories": memories,
            "search_index": {"total": index["total"], "unindexed": index["missing"]},
        }
        try:
            with engine.connect() as conn:
                detail["embeddings"] = conn.execute(text("SELECT count(*) FROM embeddings")).scalar() or 0
                detail["embeddings_queued"] = conn.execute(text("SELECT count(*) FROM rag_dirty")).scalar() or 0
        except Exception:
            pass  # RAG tables absent (kill switch / brand-new db) — not a health failure
        try:
            with engine.connect() as conn:
                detail["photos"] = conn.execute(text(
                    "SELECT count(*) FROM pic_items WHERE archived = 0")).scalar() or 0
                detail["photos_pending"] = conn.execute(text(
                    "SELECT count(*) FROM pic_items WHERE archived = 0 "
                    "AND state IN ('discovered','thumb_ready','captioned')")).scalar() or 0
        except Exception:
            pass  # photo tables absent (kill switch / brand-new db) — same pass
        return detail
    except Exception as exc:  # never turn a health probe into a 500
        return {"database": f"error: {exc}"}
