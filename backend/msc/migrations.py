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
        return {
            "database": "ok",
            "memories": memories,
            "search_index": {"total": index["total"], "unindexed": index["missing"]},
        }
    except Exception as exc:  # never turn a health probe into a 500
        return {"database": f"error: {exc}"}
