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
