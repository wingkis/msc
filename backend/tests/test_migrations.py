"""Startup maintenance against an *old* database.

create_all() creates missing tables but never alters existing ones, which is how
``/api/v1/sync/conflicts`` ended up dead in the field.
"""

import sqlite3

import pytest
from sqlalchemy import create_engine, text

from msc import migrations

LEGACY_SCHEMA = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    description TEXT, body TEXT, priority TEXT NOT NULL, category TEXT NOT NULL,
    lifespan TEXT, branch TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'msc'
);
CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
CREATE TABLE memory_tags (memory_id INTEGER NOT NULL, tag_id INTEGER NOT NULL,
                          PRIMARY KEY (memory_id, tag_id));
CREATE TABLE memory_branches (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
                              parent_id INTEGER, description TEXT);
CREATE TABLE sync_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL, source TEXT NOT NULL,
    reason TEXT, resolved INTEGER NOT NULL DEFAULT 0, resolved_at TEXT, resolved_by TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE model_configs (
    id INTEGER PRIMARY KEY, label TEXT, base_url TEXT, model TEXT, api_key TEXT,
    enabled INTEGER, temperature REAL, max_tokens INTEGER, timeout_seconds INTEGER,
    max_input_chars INTEGER, extra_json TEXT, prompt_template TEXT, auto_enabled INTEGER,
    auto_time TEXT, auto_timezone TEXT, default_category TEXT, default_priority TEXT,
    archive_sources INTEGER, last_ok_at TEXT, last_error TEXT, updated_at TEXT NOT NULL
);
INSERT INTO model_configs (id, label, base_url, model, enabled, updated_at)
VALUES (1, '2.2 chat card', '', '', 1, '2026-01-01');
INSERT INTO memories (slug, name, priority, category, branch, created_at, updated_at)
VALUES ('branchy', 'Branchy', 'medium', 'reference', 'work', '2026-01-01', '2026-01-01');
INSERT INTO tags (id, name) VALUES (1, '[''codex'), (2, 'sandbox'']'), (3, 'codex'''), (4, '''');
INSERT INTO memory_tags VALUES (1, 1), (1, 2), (1, 3);
"""


@pytest.fixture()
def legacy_engine(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    con.commit()
    con.close()

    engine = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr(migrations, "engine", engine)
    return engine


def _rows(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).fetchall()


def test_missing_columns_are_added(legacy_engine):
    columns_before = {r[1] for r in _rows(legacy_engine, "PRAGMA table_info(sync_conflicts)")}
    assert "status" not in columns_before

    added = migrations.add_missing_columns()
    assert "sync_conflicts.status" in added

    columns_after = {r[1] for r in _rows(legacy_engine, "PRAGMA table_info(sync_conflicts)")}
    assert "status" in columns_after
    # Idempotent: a second pass finds nothing to do.
    assert migrations.add_missing_columns() == []


def test_malformed_tags_are_repaired_and_merged(legacy_engine):
    changed = migrations.normalize_existing_tags()
    assert changed > 0

    names = sorted(r[0] for r in _rows(legacy_engine, "SELECT name FROM tags"))
    assert "codex" in names
    assert not [n for n in names if any(junk in n for junk in "[]'\"")]
    # All three variants of "codex" collapse into one row still linked to the memory.
    linked = sorted(r[0] for r in _rows(
        legacy_engine,
        "SELECT t.name FROM tags t JOIN memory_tags mt ON mt.tag_id = t.id WHERE mt.memory_id = 1"))
    assert linked == ["codex", "sandbox"]


def test_branches_in_use_become_registered(legacy_engine):
    assert migrations.ensure_branch_records() == ["work"]
    assert migrations.branch_counts() == {"work": 1}
    assert migrations.ensure_branch_records() == []


def test_rag_tables_exist_on_a_fresh_database(client):
    """create_all picks up embeddings/rag_dirty/rag_cache/rag_settings (no Alembic)."""
    from sqlalchemy import inspect

    from msc.database import engine

    tables = set(inspect(engine).get_table_names())
    assert {"embeddings", "rag_dirty", "rag_cache", "rag_settings"} <= tables
    columns = {c["name"] for c in inspect(engine).get_columns("model_configs")}
    assert {"purpose", "probed_dim"} <= columns


def test_a_2_2_card_gets_a_purpose_and_purposes_go_unique(legacy_engine):
    added = migrations.add_missing_columns()
    assert "model_configs.purpose" in added and "model_configs.probed_dim" in added

    backfilled = migrations.ensure_model_purpose()
    assert backfilled == 1  # the lone 2.2 card becomes the chat card
    purposes = [r[0] for r in _rows(legacy_engine, "SELECT purpose FROM model_configs WHERE id = 1")]
    assert purposes == ["chat"]

    index_names = {i["name"] for i in inspect_engine(legacy_engine).get_indexes("model_configs")}
    assert "uq_model_configs_purpose" in index_names
    # Idempotent: nothing NULL left, index already there.
    assert migrations.ensure_model_purpose() == 0


def inspect_engine(engine):
    from sqlalchemy import inspect
    return inspect(engine)


def test_malformed_embedding_rows_are_pruned(legacy_engine):
    from sqlalchemy import text

    migrations.add_missing_columns()
    with legacy_engine.begin() as conn:
        conn.execute(text("""CREATE TABLE IF NOT EXISTS embeddings (
            memory_id INTEGER NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL,
            vec BLOB NOT NULL, embedded_at TEXT NOT NULL,
            PRIMARY KEY (memory_id, model))"""))
        conn.execute(text("INSERT INTO embeddings VALUES (1, 'm', 2, x'0000000000000000', 'now')"))  # ok: 2*4 bytes
        conn.execute(text("INSERT INTO embeddings VALUES (2, 'm', 3, x'000000000000', 'now')"))      # broken: 6 != 12
        conn.execute(text("INSERT INTO embeddings VALUES (3, 'm', 0, x'', 'now')"))                  # broken: dim 0

    assert migrations.prune_broken_embeddings() == 2
    assert migrations.prune_broken_embeddings() == 0
    kept = [r[0] for r in _rows(legacy_engine, "SELECT memory_id FROM embeddings")]
    assert kept == [1]


def test_bodies_with_leaked_frontmatter_are_repaired(client, db, memory_dir):
    """Rows written before the reader was fixed stay dirty until maintenance runs."""
    from msc.models import Memory

    created = client.post("/api/v1/memories", json={
        "slug": "legacy-body", "name": "Legacy Body", "body": "real body"})
    assert created.status_code in (200, 201), created.text

    row = db.query(Memory).filter(Memory.slug == "legacy-body").first()
    row.body = "name: Legacy Body\ndescription: old\ntags: test\n---\nreal body"
    db.commit()

    assert migrations.clean_existing_bodies() == ["legacy-body"]

    db.expire_all()
    row = db.query(Memory).filter(Memory.slug == "legacy-body").first()
    assert row.body == "real body"

    written = (memory_dir / "legacy-body.md").read_text(encoding="utf-8")
    assert written.count("---") == 2
    assert "real body" in written
    # Nothing left to repair, so a second pass touches no files.
    assert migrations.clean_existing_bodies() == []


def test_body_repair_never_touches_archived_memories(client, db, memory_dir):
    """An archived row keeps no file on purpose — rewriting one would resurrect it."""
    from msc.models import Memory

    created = client.post("/api/v1/memories", json={
        "slug": "gone-away", "name": "Gone Away", "body": "real body"})
    assert created.status_code in (200, 201), created.text
    assert client.delete("/api/v1/memories/gone-away").status_code == 200

    row = db.query(Memory).filter(Memory.slug == "gone-away").first()
    row.body = "name: Gone Away\ndescription: old\ntags: test\n---\nreal body"
    db.commit()

    assert migrations.clean_existing_bodies() == []
    assert not (memory_dir / "gone-away.md").exists()


def test_pic_queue_queries_are_index_seeks(client):
    """pic_items.state is the ingest queue: three times a tick the worker asks
    "what is still at stage X, oldest id first". A full walk of the photo table
    per stage is what makes a 50k-photo library feel stuck, so the planner must
    take an index — and re-running the maintenance pass must stay a no-op.
    """
    from msc.migrations import PIC_INDEXES, ensure_pic_indexes

    assert ensure_pic_indexes() == len(PIC_INDEXES)
    assert ensure_pic_indexes() == len(PIC_INDEXES)  # IF NOT EXISTS: idempotent

    plans = []
    with migrations.engine.connect() as conn:
        conn.execute(text(
            "INSERT INTO pic_items (source_id, rel_path, origin_uri, name, state, "
            "archived, missing, error_count, discovered_at, updated_at) VALUES "
            "(1, 'plan-a.jpg', 'pic://1/plan-a.jpg', 'plan-a.jpg', 'discovered', "
            "0, 0, 0, '2026-01-01', '2026-01-01')"))
        for sql in (
            "SELECT id FROM pic_items WHERE state = 'discovered' AND archived = 0 "
            "AND missing = 0 ORDER BY id LIMIT 16",
            "SELECT id FROM pic_items WHERE state = 'thumb_ready' AND archived = 0 "
            "AND missing = 0 ORDER BY id LIMIT 16",
            "SELECT id FROM pic_items WHERE source_id = 1 AND state = 'discovered' "
            "AND archived = 0 AND missing = 0 ORDER BY id LIMIT 16",
        ):
            rows = conn.execute(text("EXPLAIN QUERY PLAN " + sql)).fetchall()
            # EXPLAIN QUERY PLAN columns are (selectid, order, from, detail).
            plans.append(" ".join(str(row[3]) for row in rows))
        conn.execute(text("DELETE FROM pic_items WHERE origin_uri = 'pic://1/plan-a.jpg'"))
        conn.commit()
    # SQLite writes SEARCH when it can take an index (COVERING INDEX = the
    # index alone answers the query); a bare SCAN of pic_items is the thing
    # this guards against, because that walk repeats on every worker tick.
    assert all(plan.startswith("SEARCH pic_items USING ") for plan in plans), plans
