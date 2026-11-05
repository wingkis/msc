"""Background backup worker — scheduled backups."""

import tarfile
import uuid
from datetime import datetime, timezone
import time

from msc.config import get_settings

settings = get_settings()


def create_backup():
    """Create a backup archive."""
    backup_dir = settings.backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)

    backup_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"msc-backup-{backup_id}-{timestamp}.tar.gz"
    backup_path = backup_dir / filename

    try:
        with tarfile.open(str(backup_path), "w:gz") as tar:
            if settings.db_path.exists():
                tar.add(str(settings.db_path), arcname="msc.db")
            if settings.memory_dir.exists():
                for md_file in settings.memory_dir.glob("*.md"):
                    tar.add(str(md_file), arcname=f"memories/{md_file.name}")
            index_path = settings.memory_dir / ".msc_index.json"
            if index_path.exists():
                tar.add(str(index_path), arcname="msc_index.json")

        return filename
    except Exception as e:
        print(f"Backup failed: {e}")
        return None


def prune_backups(keep: int = None) -> list[str]:
    """Delete the oldest archives so the scheduled backup cannot fill the disk."""
    keep = settings.backup_keep if keep is None else keep
    if keep <= 0 or not settings.backup_dir.exists():
        return []

    archives = sorted(settings.backup_dir.glob("msc-backup-*.tar.gz"),
                      key=lambda path: path.stat().st_mtime)
    removed = []
    for path in archives[:-keep]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            continue
    return removed


def start_backup_worker(interval_hours: int = 24):
    """Start the background backup loop."""
    interval = interval_hours * 3600
    print(f"Backup worker started (interval={interval_hours}h, keep={settings.backup_keep})")

    while True:
        try:
            filename = create_backup()
            if filename:
                print(f"Backup created: {filename}")
                removed = prune_backups()
                if removed:
                    print(f"Pruned {len(removed)} old backup(s): {', '.join(removed[:3])}")
            else:
                print("Backup failed")
        except Exception as e:
            print(f"Backup worker error: {e}")
        time.sleep(interval)
