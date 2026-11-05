#!/usr/bin/env python3
"""
Migration: Convert existing .msc markdown files + JSON index to SQLite.

Usage:
    python migrate_from_markdown.py --db /home/astro/msc/data/msc.db --memory-dir /home/astro/.msc/memory
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import sqlite3


MEMORY_DIR_DEFAULT = Path("/home/astro/.msc/memory")
INDEX_PATH_DEFAULT = MEMORY_DIR_DEFAULT / ".msc_index.json"

VALID_CATEGORIES = {"config", "project", "user", "knowledge", "reference"}
VALID_PRIORITIES = {"low", "medium", "high", "critical"}
VALID_LIFESPANS = {"temporary", "permanent"}
SKIP_FILES = {"MEMORY.md", ".msc_index.json"}


def parse_frontmatter(file_path: Path) -> tuple[dict, str]:
    content = file_path.read_text(encoding="utf-8")
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, content

    fm_lines = []
    body_start = 1
    in_fm = False
    for i, line in enumerate(lines, 0):
        if line.strip() == "---" and not in_fm:
            in_fm = True
            body_start = i + 1
            continue
        if line.strip() == "---" and in_fm:
            break
        if in_fm:
            fm_lines.append(line)

    frontmatter = {}
    for line in fm_lines:
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            frontmatter[key.strip().lower()] = value.strip()

    return frontmatter, "\n".join(lines[body_start:])


def parse_tags(raw_tags) -> list[str]:
    if not raw_tags:
        return []
    raw_tags = str(raw_tags).strip()
    if not raw_tags:
        return []
    if raw_tags.startswith("["):
        try:
            return json.loads(raw_tags)
        except (json.JSONDecodeError, TypeError):
            pass
    if "," in raw_tags:
        return [t.strip().strip('"\'') for t in raw_tags.split(",") if t.strip()]
    return [raw_tags]


def main():
    parser = argparse.ArgumentParser(description="Migrate .msc markdown files to SQLite")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--memory-dir", type=Path, default=MEMORY_DIR_DEFAULT)
    args = parser.parse_args()

    memory_dir = args.memory_dir
    db_path = args.db or memory_dir.parent / "data" / "msc.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Memory dir : {memory_dir}")
    print(f"Database   : {db_path}\n")

    # Load index
    index_path = memory_dir / ".msc_index.json"
    index_data = {}
    if index_path.exists():
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        index_data = data.get("memories", data)
    print(f"Loaded index: {len(index_data)} entries\n")

    # Remove stale DB
    if db_path.exists():
        db_path.unlink()

    # Connect and create schema
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    cursor = conn.cursor()

    schema_path = Path("/home/astro/msc/backend/schema.sql")
    if not schema_path.exists():
        schema_path = memory_dir.parent / "schema.sql"
    with open(schema_path) as f:
        cursor.executescript(f.read())
    conn.commit()

    imported = 0
    skipped = 0
    errors = 0

    for md_file in sorted(memory_dir.glob("*.md")):
        if md_file.name in SKIP_FILES:
            skipped += 1
            continue

        slug = md_file.stem

        try:
            frontmatter, body = parse_frontmatter(md_file)
            idx = index_data.get(slug, {})

            name = idx.get("name", frontmatter.get("name", slug))
            description = idx.get("description", frontmatter.get("description", ""))
            tags = parse_tags(idx.get("tags", frontmatter.get("tags", "")))
            priority = idx.get("priority", frontmatter.get("priority", "medium")).lower()
            category = idx.get("category", frontmatter.get("category", "reference")).lower()
            lifespan = idx.get("lifespan", frontmatter.get("lifespan", "permanent")).lower()
            created = idx.get("created", frontmatter.get("created", datetime.now(timezone.utc).isoformat()))
            updated = idx.get("updated", frontmatter.get("updated", datetime.now(timezone.utc).isoformat()))

            # Validate
            if priority not in VALID_PRIORITIES:
                priority = "medium"
            if category not in VALID_CATEGORIES:
                category = "reference"

            body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

            # Upsert memory
            existing = cursor.execute("SELECT id FROM memories WHERE slug = ?", (slug,)).fetchone()
            if existing:
                cursor.execute("""UPDATE memories SET name=?, description=?, body=?, priority=?,
                    category=?, lifespan=?, created_at=?, updated_at=?, last_sync_hash=?
                    WHERE slug=?""",
                    (name, description, body, priority, category, lifespan,
                     created, updated, body_hash, slug))
            else:
                cursor.execute("""INSERT INTO memories
                    (slug, name, description, body, priority, category, lifespan,
                     created_at, updated_at, source, last_sync_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported', ?)""",
                    (slug, name, description, body, priority, category, lifespan,
                     created, updated, body_hash))
            conn.commit()

            # Memory ID
            cursor.execute("SELECT id FROM memories WHERE slug = ?", (slug,))
            memory_id = cursor.fetchone()[0]

            # Insert tags
            tag_ids = []
            for tag in tags:
                tag = tag.strip()
                if not tag:
                    continue
                cursor.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
                cursor.execute("SELECT id FROM tags WHERE name = ?", (tag,))
                tag_ids.append(cursor.fetchone()[0])
                conn.commit()

            # Link memory_tags
            for tag_id in tag_ids:
                cursor.execute("INSERT OR IGNORE INTO memory_tags (memory_id, tag_id) VALUES (?, ?)",
                    (memory_id, tag_id))
            conn.commit()

            # Insert relations
            relations_field = idx.get("related", frontmatter.get("related", ""))
            if relations_field:
                for related_slug in [r.strip() for r in str(relations_field).split(",") if r.strip()]:
                    related_row = cursor.execute("SELECT id FROM memories WHERE slug = ?", (related_slug,)).fetchone()
                    if related_row and related_row[0] != memory_id:
                        cursor.execute("""INSERT OR IGNORE INTO memory_relations
                            (memory_id_a, memory_id_b, relation_type, created_at)
                            VALUES (?, ?, 'related', ?)""",
                            (memory_id, related_row[0], created))
                        cursor.execute("""INSERT OR IGNORE INTO memory_relations
                            (memory_id_a, memory_id_b, relation_type, created_at)
                            VALUES (?, ?, 'related', ?)""",
                            (related_row[0], memory_id, created))
                        conn.commit()

            imported += 1
            print(f"  OK     {slug} ({category}/{priority}, {len(tags)} tags)")

        except Exception as e:
            conn.rollback()
            print(f"  ERROR  {slug}: {e}")
            errors += 1

    # Import relations from index
    for rel in index_data.get("relations", []):
        from_slug = rel.get("from", rel.get("from_slug", ""))
        to_slug = rel.get("to", rel.get("to_slug", ""))
        rel_type = rel.get("type", "related")
        if not from_slug or not to_slug:
            continue
        from_row = cursor.execute("SELECT id FROM memories WHERE slug = ?", (from_slug,)).fetchone()
        to_row = cursor.execute("SELECT id FROM memories WHERE slug = ?", (to_slug,)).fetchone()
        if from_row and to_row and from_row[0] != to_row[0]:
            cursor.execute("""INSERT OR IGNORE INTO memory_relations
                (memory_id_a, memory_id_b, relation_type, created_at)
                VALUES (?, ?, ?, ?)""",
                (from_row[0], to_row[0], rel_type, datetime.now(timezone.utc).isoformat()))
            conn.commit()

    # Rebuild FTS
    try:
        cursor.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        conn.commit()
    except Exception as e:
        print(f"  FTS rebuild: {e}")

    # Update JSON index
    if index_path.exists():
        full_index = {"memories": index_data}
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(full_index, f, indent=2, ensure_ascii=False)

    conn.close()

    print("\nMigration complete!")
    print(f"  Imported : {imported} memories")
    print(f"  Skipped  : {skipped} files")
    print(f"  Errors   : {errors}")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
