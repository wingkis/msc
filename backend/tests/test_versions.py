"""Version history regressions (this endpoint used to return a bare 500)."""


def test_list_versions_ok_after_edits(client):
    client.post("/api/v1/memories", json={"name": "Versioned Note", "slug": "versioned-note",
                                          "body": "v1", "category": "knowledge"})
    client.put("/api/v1/memories/versioned-note", json={"body": "v2"})
    client.put("/api/v1/memories/versioned-note", json={"body": "v3"})

    resp = client.get("/api/v1/memories/versions/versioned-note")
    assert resp.status_code == 200, resp.text
    versions = resp.json()
    # Only the latest snapshot survives: the state before the most recent edit.
    assert len(versions) == 1
    assert versions[0]["body"] == "v2"
    assert client.get("/api/v1/memories/versioned-note").json()["body"] == "v3"


def test_versions_alias_and_missing_memory(client):
    assert client.get("/api/v1/memories/versioned-note/versions").status_code == 200
    assert client.get("/api/v1/memories/versions/no-such-note").status_code == 404


def test_restore_walks_back_and_records_the_restore(client):
    client.put("/api/v1/memories/versioned-note", json={"body": "v4"})
    versions = client.get("/api/v1/memories/versions/versioned-note").json()
    assert len(versions) == 1
    target = versions[0]  # snapshot taken before the v4 edit -> body "v3"
    assert target["body"] == "v3"

    restored = client.post(f"/api/v1/memories/versions/versioned-note/restore/{target['version_num']}")
    assert restored.status_code == 200, restored.text
    assert client.get("/api/v1/memories/versioned-note").json()["body"] == "v3"

    # The restore itself is undoable: exactly one snapshot again, holding the
    # state the restore replaced.
    history = client.get("/api/v1/memories/versions/versioned-note").json()
    assert len(history) == 1
    assert history[0]["change_summary"].startswith("Restored to version")
    assert history[0]["body"] == "v4"


def test_restore_missing_version_is_404(client):
    resp = client.post("/api/v1/memories/versions/versioned-note/restore/9999")
    assert resp.status_code == 404


def test_agent_attribution_is_recorded(client):
    client.post("/api/v1/memories", json={"name": "Attributed Note", "slug": "attributed-note",
                                          "body": "first"})
    client.put("/api/v1/memories/attributed-note", json={"body": "second"},
               headers={"X-Agent": "claude-code"})
    history = client.get("/api/v1/memories/versions/attributed-note").json()
    assert history[0]["created_by"] == "claude-code"
