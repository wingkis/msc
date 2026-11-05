"""DB <-> filesystem sync."""


def test_push_writes_markdown_files(client, memory_dir):
    client.post("/api/v1/memories", json={"name": "Pushed Note", "slug": "pushed-note",
                                          "body": "written to disk", "tags": ["pushed"]})
    resp = client.post("/api/v1/sync/push")
    assert resp.status_code == 200
    assert resp.json()["errors"] == []

    path = memory_dir / "pushed-note.md"
    assert path.exists()
    text = path.read_text()
    assert text.startswith("---")
    assert "name: Pushed Note" in text
    assert "written to disk" in text


def test_external_file_edit_is_pulled(client, memory_dir):
    client.post("/api/v1/memories", json={"name": "Pulled Note", "slug": "pulled-note",
                                          "body": "original"})
    client.post("/api/v1/sync/push")

    path = memory_dir / "pulled-note.md"
    path.write_text(path.read_text().replace("original", "edited outside msc"), encoding="utf-8")

    resp = client.post("/api/v1/sync/pull")
    assert resp.status_code == 200
    assert "pulled-note" in resp.json()["updated"]
    assert "edited outside msc" in client.get("/api/v1/memories/pulled-note").json()["body"]


def test_new_file_on_disk_is_imported(client, memory_dir):
    (memory_dir / "handwritten-note.md").write_text(
        "---\nname: Handwritten Note\ncategory: knowledge\npriority: high\ntags: handwritten\n---\n\nBy hand.\n",
        encoding="utf-8",
    )
    resp = client.post("/api/v1/sync/pull")
    assert resp.status_code == 200
    assert "handwritten-note" in resp.json()["created"]

    memory = client.get("/api/v1/memories/handwritten-note").json()
    assert memory["source"] == "claude"
    assert memory["tags"][0]["name"] == "handwritten"


def test_sync_status_reports_divergence(client):
    resp = client.get("/api/v1/sync/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["memory_dir_exists"] is True
    assert body["db_memories"] >= 1
    assert body["files"] >= 1
    assert "only_in_db" in body and "only_in_files" in body


def test_conflicts_endpoint_survives_schema_upgrade(client):
    """/sync/conflicts used to 500 because create_all never added new columns."""
    resp = client.get("/api/v1/sync/conflicts")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_push_then_pull_is_a_no_op(client):
    """A write-through must not look like an external file edit next cycle.

    Without last_sync_hash on write, every pull re-imported every memory.
    """
    client.post("/api/v1/memories", json={"name": "Idempotent Note", "slug": "idempotent-note",
                                          "body": "stable body", "tags": ["idempotent"]})
    client.post("/api/v1/sync/push")

    pulled = client.post("/api/v1/sync/pull").json()
    assert "idempotent-note" not in pulled["created"]
    assert "idempotent-note" not in pulled["updated"]

    status = client.get("/api/v1/sync/status").json()
    assert "idempotent-note" not in status["only_in_files"]
    assert "idempotent-note" not in status["only_in_db"]


def test_index_file_is_not_a_memory(client, memory_dir):
    """MEMORY.md is the human index; it must never show up as drift."""
    (memory_dir / "MEMORY.md").write_text("# Index\n\n- something\n", encoding="utf-8")

    status = client.get("/api/v1/sync/status").json()
    assert "MEMORY" not in status["only_in_files"]
    assert "MEMORY" not in client.post("/api/v1/sync/pull").json()["created"]


def test_archived_memory_is_not_reported_as_a_new_file(client):
    client.post("/api/v1/memories", json={"name": "Archive Drift", "slug": "archive-drift",
                                          "body": "archive drift"})
    client.delete("/api/v1/memories/archive-drift")

    status = client.get("/api/v1/sync/status").json()
    assert "archive-drift" not in status["only_in_files"]
    assert status["db_archived"] >= 1


def test_sync_logs_grow(client):
    client.post("/api/v1/sync/push")
    logs = client.get("/api/v1/sync/logs").json()
    assert logs and logs[0]["direction"] in ("push", "pull", "sync")
