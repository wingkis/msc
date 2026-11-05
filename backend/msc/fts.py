"""SQLite FTS5 maintenance and query sanitising.

``memories_fts`` is an *external content* table (content='memories'), which
means the index is only as good as the triggers keeping it in sync. The
original schema had no INSERT trigger at all and used plain UPDATE statements
for delete/update, both of which are invalid for external-content tables — so
anything created through the API was silently unsearchable.

ensure_fts() is idempotent and safe to run on every startup.
"""

from sqlalchemy import text

from msc.database import engine

_LEGACY_TRIGGERS = ("memories_ai_fts", "memories_ab_fts")

_DDL = (
    # insert
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, name, description, body)
        VALUES (new.id, new.name, new.description, new.body);
    END;
    """,
    # update: delete the old row from the index, then re-add it
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, name, description, body)
        VALUES ('delete', old.id, old.name, old.description, old.body);
        INSERT INTO memories_fts(rowid, name, description, body)
        VALUES (new.id, new.name, new.description, new.body);
    END;
    """,
    # soft delete keeps the row in memories, so the index keeps it too
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, name, description, body)
        VALUES ('delete', old.id, old.name, old.description, old.body);
    END;
    """,
)


def ensure_fts() -> None:
    """Create the FTS triggers, dropping the broken originals if present."""
    with engine.begin() as conn:
        # Virtual tables need CREATE VIRTUAL TABLE; plain CREATE TABLE ... USING is
        # a syntax error, so a fresh database used to end up with no search index.
        conn.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
                          "name, description, body, content='memories', content_rowid='id')"))
        for name in _LEGACY_TRIGGERS:
            conn.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        for stmt in _DDL:
            conn.execute(text(stmt))


def rebuild_fts() -> int:
    """Rebuild the whole index from the memories table. Returns row count."""
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')"))
        return conn.execute(text("SELECT count(*) FROM memories")).scalar() or 0


def fts_desync_report() -> dict:
    """Rows present in ``memories`` but absent from the search index."""
    with engine.connect() as conn:
        missing = [row[0] for row in conn.execute(text(
            "SELECT m.slug FROM memories m "
            "WHERE m.id NOT IN (SELECT rowid FROM memories_fts) "
            "ORDER BY m.id"
        ))]
        total = conn.execute(text("SELECT count(*) FROM memories")).scalar() or 0
    return {"total": total, "missing": len(missing), "slugs": missing[:20]}


def sanitize_match(query: str) -> str:
    """Turn arbitrary user input into a safe FTS5 MATCH expression.

    Every word becomes a quoted phrase, so operators and punctuation
    (``AND``, ``:``, ``C++``, quotes, parens) can no longer make FTS5 raise a
    syntax error — and words match as prefixes, which is what people expect
    when searching notes.
    """
    if not query:
        return ""

    terms: list[str] = []
    for raw in query.split():
        word = raw.strip("\"'`()[]{}^~*|+-!<>=").strip()
        if not word:
            continue
        terms.append('"' + word.replace('"', '""') + '"*')
    return " AND ".join(terms)
