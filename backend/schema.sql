-- MSC v2 Database Schema
-- SQLite + FTS5 full-text search

CREATE TABLE IF NOT EXISTS memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    priority        TEXT NOT NULL CHECK(priority IN ('low','medium','high','critical')),
    category        TEXT NOT NULL CHECK(category IN ('config','project','user','knowledge','reference')),
    lifespan        TEXT NOT NULL DEFAULT 'permanent' CHECK(lifespan IN ('temporary','permanent')),
    branch          TEXT DEFAULT '',                  -- branch name (e.g. "work", "personal")
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    locked_at       TEXT,
    locked_by       TEXT,
    archived        INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'msc' CHECK(source IN ('msc','imported','claude')),
    last_sync_hash  TEXT,
    owner_id        INTEGER
);

CREATE TABLE IF NOT EXISTS tags (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id  INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (memory_id, tag_id)
);

CREATE TABLE IF NOT EXISTS memory_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    version_num     INTEGER NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    body            TEXT NOT NULL,
    priority        TEXT,
    category        TEXT,
    tags_snapshot   TEXT,               -- JSON array of tag names
    change_summary  TEXT,
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    UNIQUE(memory_id, version_num)
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    display_name    TEXT,
    role            TEXT NOT NULL DEFAULT 'viewer'
                    CHECK(role IN ('admin','editor','viewer')),
    api_key_hash    TEXT NOT NULL,
    api_key_prefix  TEXT,
    created_at      TEXT NOT NULL,
    last_login      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS api_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash        TEXT UNIQUE NOT NULL,
    key_prefix      TEXT NOT NULL,
    description     TEXT,
    created_at      TEXT NOT NULL,
    last_used       TEXT,
    is_active       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS prefix_routes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prefix          TEXT UNIQUE NOT NULL,
    route_type      TEXT NOT NULL CHECK(route_type IN ('full','branch','category','tag','host')),
    route_value     TEXT,               -- JSON: branch name / tag list / category list / host list
    read_access     INTEGER DEFAULT 1,
    write_access    INTEGER DEFAULT 1,
    description     TEXT
);

CREATE TABLE IF NOT EXISTS memory_branches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,
    parent_id       INTEGER REFERENCES memory_branches(id),
    description     TEXT
);

CREATE TABLE IF NOT EXISTS memory_relations (
    memory_id_a     INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    memory_id_b     INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    relation_type   TEXT NOT NULL DEFAULT 'related'
                    CHECK(relation_type IN ('related','depends_on','supersedes','seealso','keyword','summarizes')),
    created_at      TEXT NOT NULL,
    PRIMARY KEY (memory_id_a, memory_id_b, relation_type)
);

CREATE TABLE IF NOT EXISTS sync_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       INTEGER REFERENCES memories(id),
    api_key_prefix  TEXT,
    direction       TEXT CHECK(direction IN ('push','pull','sync')),
    status          TEXT CHECK(status IN ('ok','conflict','error')),
    detail          TEXT,
    remote_ip       TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL,
    source          TEXT NOT NULL,
    reason          TEXT,
    resolved        INTEGER NOT NULL DEFAULT 0,
    resolved_at     TEXT,
    resolved_by     TEXT,
    status          TEXT DEFAULT 'open',
    created_at      TEXT NOT NULL,
    CHECK(source IN ('db','filesystem','claude'))
);

-- FTS5 virtual table for full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    name, description, body,
    content='memories',
    content_rowid='id'
);

-- Triggers to keep the external-content FTS index in sync.
-- memories_fts is content='memories', so it MUST be maintained with the
-- 'insert'/'delete' commands; plain UPDATE/INSERT statements corrupt it, and
-- without an INSERT trigger new memories are simply never indexed.
CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, name, description, body)
    VALUES (new.id, new.name, new.description, new.body);
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, name, description, body)
    VALUES ('delete', old.id, old.name, old.description, old.body);
    INSERT INTO memories_fts(rowid, name, description, body)
    VALUES (new.id, new.name, new.description, new.body);
END;

CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, name, description, body)
    VALUES ('delete', old.id, old.name, old.description, old.body);
END;
