"""Backup create/list/download/delete and filename handling."""

import os


def test_backup_roundtrip(client, backup_dir):
    created = client.post("/api/v1/backup/create")
    assert created.status_code == 200, created.text
    filename = created.json()["filename"]
    assert (backup_dir / filename).exists()

    listed = client.get("/api/v1/backup/list").json()
    assert filename in [b["filename"] for b in listed]

    download = client.get(f"/api/v1/backup/backups/{filename}/download")
    assert download.status_code == 200
    assert download.content[:2] == b"\x1f\x8b"  # gzip magic

    assert client.delete(f"/api/v1/backup/backups/{filename}").status_code == 200
    assert filename not in [b["filename"] for b in client.get("/api/v1/backup/list").json()]


def test_path_traversal_is_rejected(client):
    for evil in ["../../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "notes.md"]:
        assert client.get(f"/api/v1/backup/backups/{evil}/download").status_code in (400, 404)
        assert client.delete(f"/api/v1/backup/backups/{evil}").status_code in (400, 404)


def test_backup_worker_skips_wal_sidecars(tmp_path, monkeypatch):
    from msc.workers import backup_worker

    monkeypatch.setattr(backup_worker.settings, "backup_dir", tmp_path / "backups")
    monkeypatch.setattr(backup_worker.settings, "db_path", tmp_path / "msc.db")
    monkeypatch.setattr(backup_worker.settings, "memory_dir", tmp_path / "memory")

    (tmp_path / "backups").mkdir()
    (tmp_path / "memory").mkdir()
    (tmp_path / "msc.db").write_bytes(b"not really a db")
    (tmp_path / "msc.db-wal").write_bytes(b"wal")

    filename = backup_worker.create_backup()
    assert filename and (tmp_path / "backups" / filename).exists()


def test_prune_backups_keeps_only_the_newest(tmp_path, monkeypatch):
    """The 24h backup loop must not grow /app/backups forever."""
    from msc.workers import backup_worker

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(backup_worker.settings, "backup_dir", backup_dir)
    monkeypatch.setattr(backup_worker.settings, "backup_keep", 3)

    for index in range(6):
        path = backup_dir / f"msc-backup-abcdef{index}-2026010{index}_000000.tar.gz"
        path.write_bytes(b"x")
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))

    removed = backup_worker.prune_backups()
    assert len(removed) == 3
    assert len(list(backup_dir.glob("msc-backup-*.tar.gz"))) == 3
    # keep=0 means "never prune"
    assert backup_worker.prune_backups(keep=0) == []


def test_backups_are_listed_newest_first(client, backup_dir):
    """Random ids sit in the middle of the filename, so name order != time order."""
    for index, name in enumerate(["msc-backup-ffff0000-20260101_000000.tar.gz",
                                  "msc-backup-0000aaaa-20260102_000000.tar.gz"]):
        (backup_dir / name).write_bytes(b"x")
        os.utime(backup_dir / name, (1_700_000_000 + index, 1_700_000_000 + index))

    listed = client.get("/api/v1/backup/list").json()
    names = [b["filename"] for b in listed]
    assert names[:2] == ["msc-backup-0000aaaa-20260102_000000.tar.gz",
                         "msc-backup-ffff0000-20260101_000000.tar.gz"]


def test_backup_restore_replays_memories(client, backup_dir):
    original = client.post("/api/v1/memories", json={"name": "Restore Test",
                                                     "slug": "restore-test", "body": "original"})
    original_id = original.json()["id"]

    filename = client.post("/api/v1/backup/create").json()["filename"]
    client.put("/api/v1/memories/restore-test", json={"body": "changed later"})
    assert client.get("/api/v1/memories/restore-test").json()["body"] == "changed later"

    result = client.post(f"/api/v1/backup/backups/{filename}/restore")
    assert result.status_code == 200, result.text

    restored = client.get("/api/v1/memories/restore-test").json()
    assert restored["body"] == "original"
    # Ids come back verbatim from the backup — restoring never renumbers.
    assert restored["id"] == original_id
