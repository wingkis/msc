"""API routes: backup (create, list, download, delete, restore)."""

import re
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from msc.config import get_settings
from msc.database import get_db
# Backup model is not needed - backups are file-based

settings = get_settings()
router = APIRouter()

# Backups are addressed by filename in the URL, so the name has to be pinned
# down before it is joined onto the backup directory.
_BACKUP_NAME = re.compile(r"^msc-backup-[0-9a-f]{8}-[0-9_]{15}\.tar\.gz$")


def _backup_path(filename: str) -> Path:
    if not _BACKUP_NAME.match(filename):
        raise HTTPException(status_code=400, detail=f"Not a backup filename: {filename}")
    path = settings.backup_dir / filename
    resolved = path.resolve()
    if not str(resolved).startswith(str(settings.backup_dir.resolve())) or not resolved.exists():
        raise HTTPException(status_code=404, detail="Backup not found")
    return resolved


@router.post("/create")
def create_backup(db: Session = Depends(get_db)):
    """Create a backup archive of the database and memory files."""
    backup_dir = settings.backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)

    backup_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # Use a consistent timestamp so the filename passes _backup_name() on the way back in.
    filename = f"msc-backup-{backup_id}-{timestamp}.tar.gz"
    backup_path = backup_dir / filename

    with tarfile.open(str(backup_path), "w:gz") as tar:
        # Add database
        if settings.db_path.exists():
            tar.add(str(settings.db_path), arcname="msc.db")

        # Add memory files
        if settings.memory_dir.exists():
            for md_file in settings.memory_dir.glob("*.md"):
                tar.add(str(md_file), arcname=f"memories/{md_file.name}")

        # Add index
        index_path = settings.memory_dir / ".msc_index.json"
        if index_path.exists():
            tar.add(str(index_path), arcname="msc_index.json")

    return {
        "id": backup_id,
        "filename": filename,
        "size": backup_path.stat().st_size,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/list")
def list_backups(db: Session = Depends(get_db)):
    """List all backups."""
    backup_dir = settings.backup_dir
    backups = []
    files = [f for f in backup_dir.glob("msc-backup-*.tar.gz") if f.is_file()]
    for f in sorted(files, key=lambda f: f.stat().st_mtime, reverse=True):
        backups.append({
            "id": f.name.split("-")[2],
            "filename": f.name,
            "size": f.stat().st_size,
            "created_at": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
        })
    return backups


@router.get("/backups/{filename}/download")
def download_backup(filename: str):
    """Download a backup file."""
    backup_path = _backup_path(filename)
    return Response(
        content=backup_path.read_bytes(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.delete("/backups/{filename}")
def delete_backup(filename: str):
    """Delete a backup file."""
    backup_path = _backup_path(filename)
    backup_path.unlink()
    return {"message": f"Backup deleted: {filename}"}


# Tables a backup can replay. Ids are copied verbatim, so a restored memory
# keeps the id it had when the backup was taken.
_RESTORE_TABLES = ("memories", "tags", "memory_tags", "memory_versions", "memory_relations")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@router.post("/backups/{filename}/restore")
def restore_backup(filename: str):
    """Replay a backup archive into the live database and memory files."""
    backup_path = _backup_path(filename)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        with tarfile.open(backup_path, "r:gz") as tar:
            tar.extractall(tmpdir, filter="data")

        src_db = tmpdir / "msc.db"
        if not src_db.exists():
            raise HTTPException(status_code=400, detail="Backup contains no database")

        restored: dict[str, int] = {}
        with sqlite3.connect(src_db) as src, sqlite3.connect(settings.db_path) as dst:
            dst.execute("PRAGMA foreign_keys=OFF")
            src_tables = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in _RESTORE_TABLES if t not in src_tables]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Backup database is missing tables: {', '.join(missing)}",
                )
            live_tables = {row[0] for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}

            for table in _RESTORE_TABLES:
                if table not in live_tables:
                    continue  # created by startup maintenance before inserts land
                columns = [c[1] for c in src.execute(f'PRAGMA table_info("{table}")')
                           if _IDENTIFIER.match(c[1])]
                if not columns:
                    continue
                quoted = ", ".join(f'"{c}"' for c in columns)
                rows = src.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
                dst.execute(f'DELETE FROM "{table}"')
                if rows:
                    placeholders = ", ".join("?" for _ in columns)
                    dst.executemany(f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})', rows)
                restored[table] = len(rows)
            dst.commit()

        # Memory files and the JSON index, so disk and DB agree after a restore.
        mem_dir = settings.memory_dir
        mem_dir.mkdir(parents=True, exist_ok=True)
        files = 0
        if (tmpdir / "memories").is_dir():
            for md in (tmpdir / "memories").glob("*.md"):
                shutil.copy(md, mem_dir / md.name)
                files += 1
        if (tmpdir / "msc_index.json").exists():
            shutil.copy(tmpdir / "msc_index.json", mem_dir / ".msc_index.json")

    from msc.database import engine
    from msc.fts import ensure_fts, rebuild_fts
    engine.dispose()  # fresh pooled connections see the restored rows
    ensure_fts()
    rebuild_fts()

    return {
        "message": f"Backup restored: {filename}",
        "memories": restored.get("memories", 0),
        "versions": restored.get("memory_versions", 0),
        "files": files,
    }
