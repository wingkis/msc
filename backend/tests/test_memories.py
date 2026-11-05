"""CRUD, upsert, batch and the vocabulary guards."""


def _payload(name="Test Memory", **overrides):
    data = {"name": name, "body": "some body text", "category": "reference",
            "priority": "medium", "tags": ["alpha", "beta"]}
    data.update(overrides)
    return data


def test_create_get_update_delete_roundtrip(client):
    created = client.post("/api/v1/memories", json=_payload(name="Roundtrip One"))
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    assert slug == "roundtrip-one"

    fetched = client.get(f"/api/v1/memories/{slug}")
    assert fetched.status_code == 200
    assert fetched.json()["body"] == "some body text"
    assert {t["name"] for t in fetched.json()["tags"]} == {"alpha", "beta"}

    updated = client.put(f"/api/v1/memories/{slug}", json={"priority": "critical"})
    assert updated.status_code == 200
    assert updated.json()["priority"] == "critical"

    deleted = client.delete(f"/api/v1/memories/{slug}")
    assert deleted.status_code == 200

    assert client.get(f"/api/v1/memories/{slug}").status_code == 404
    assert client.get(f"/api/v1/memories/{slug}", params={"include_archived": True}).status_code == 200

    restored = client.post(f"/api/v1/memories/{slug}/restore")
    assert restored.status_code == 200
    assert restored.json()["archived"] == 0


def test_duplicate_slug_is_a_conflict_not_a_500(client):
    first = client.post("/api/v1/memories", json=_payload(name="Dup Slug"))
    assert first.status_code == 201
    second = client.post("/api/v1/memories", json=_payload(name="Dup Slug"))
    assert second.status_code == 409
    assert "exists" in second.json()["detail"]


def test_bad_vocabulary_returns_422_with_allowed_values(client):
    resp = client.post("/api/v1/memories", json=_payload(name="Bad Vocab", category="nonsense"))
    assert resp.status_code == 422
    assert "config" in resp.json()["detail"]


def test_upsert_creates_then_replaces(client):
    slug = "explicit-upsert-slug"
    created = client.put(f"/api/v1/memories/{slug}", json={"name": "Upserted", "body": "v1"})
    assert created.status_code == 200, created.text

    replaced = client.put(f"/api/v1/memories/{slug}", json={"body": "v2"})
    assert replaced.status_code == 200
    assert replaced.json()["body"] == "v2"

    strict = client.put("/api/v1/memories/does-not-exist-strict", json={"body": "x"},
                        params={"upsert": False})
    assert strict.status_code == 404


def test_list_filters_and_total_count_header(client):
    client.post("/api/v1/memories", json=_payload(name="Filterable", category="project",
                                                  priority="high", tags=["filterme"]))
    resp = client.get("/api/v1/memories", params={"tag": "filterme"})
    assert resp.status_code == 200
    assert resp.headers["X-Total-Count"] == str(len(resp.json()))
    assert all("filterme" in [t["name"] for t in m["tags"]] for m in resp.json())

    since = client.get("/api/v1/memories", params={"since": "2000-01-01T00:00:00+00:00"}).json()
    assert len(since) >= 1


def test_batch_reports_per_item_results(client):
    resp = client.post("/api/v1/memories/batch", json={
        "memories": [
            _payload(name="Batch One"),
            _payload(name="Batch Two", category="nonsense"),
        ],
        "upsert": True,
    })
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert "batch-one" in body["created"] or "batch-one" in body["updated"]
    assert any(item["slug"] == "batch-two" for item in body["failed"])


def test_frontmatter_in_a_body_does_not_nest(client):
    body = "---\nname: ghost\ncategory: project\n---\nReal content here."
    created = client.post("/api/v1/memories", json=_payload(name="Stray Frontmatter", body=body))
    assert created.status_code == 201
    assert created.json()["body"] == "Real content here."


def test_hard_delete_drops_the_file_index_entry(client, memory_dir):
    """The JSON index is exported to agents, so it must not list deleted memories."""
    import json

    created = client.post("/api/v1/memories", json={
        "slug": "index-me", "name": "Index Me", "body": "body"})
    assert created.status_code in (200, 201), created.text

    index_path = memory_dir / ".msc_index.json"
    assert "index-me" in json.loads(index_path.read_text(encoding="utf-8"))["memories"]

    assert client.delete("/api/v1/memories/index-me?hard=true").status_code == 200
    assert not (memory_dir / "index-me.md").exists()
    assert "index-me" not in json.loads(index_path.read_text(encoding="utf-8"))["memories"]
