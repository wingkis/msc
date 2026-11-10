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


def test_hard_delete_keeps_other_ids_and_never_reuses(client):
    """Ids are assigned once: purging a row neither renumbers the survivors
    nor hands the freed id to a new memory (AUTOINCREMENT)."""
    a = client.post("/api/v1/memories", json=_payload(name="Keep A")).json()
    b = client.post("/api/v1/memories", json=_payload(name="Purge B")).json()
    c = client.post("/api/v1/memories", json=_payload(name="Keep C")).json()

    assert client.delete("/api/v1/memories/purge-b?hard=true").status_code == 200

    assert client.get("/api/v1/memories/keep-a").json()["id"] == a["id"]
    assert client.get("/api/v1/memories/keep-c").json()["id"] == c["id"]

    fresh = client.post("/api/v1/memories", json=_payload(name="Fresh D")).json()
    assert fresh["id"] not in (a["id"], b["id"], c["id"])


def test_hard_delete_works_on_archived_memories(client):
    created = client.post("/api/v1/memories", json=_payload(name="Archive Then Purge"))
    slug = created.json()["slug"]
    assert client.delete(f"/api/v1/memories/{slug}").status_code == 200  # soft
    assert client.delete(f"/api/v1/memories/{slug}?hard=true").status_code == 200
    assert client.get(f"/api/v1/memories/{slug}", params={"include_archived": True}).status_code == 404


def test_batch_update_records_a_version_snapshot(client):
    client.post("/api/v1/memories", json={"name": "Batch Versioned",
                                          "slug": "batch-versioned", "body": "first"})
    resp = client.post("/api/v1/memories/batch", json={
        "memories": [{"name": "Batch Versioned", "slug": "batch-versioned", "body": "second"}],
        "upsert": True,
    })
    assert resp.status_code in (200, 201), resp.text
    versions = client.get("/api/v1/memories/versions/batch-versioned").json()
    assert len(versions) == 1 and versions[0]["body"] == "first"


def test_restore_accepts_fuzzy_references(client):
    """The restore route resolves slugs like every other detail route."""
    client.post("/api/v1/memories", json=_payload(name="Fuzzy Restore"))
    assert client.delete("/api/v1/memories/fuzzy-restore").status_code == 200
    revived = client.post("/api/v1/memories/fuzzy/restore")
    assert revived.status_code == 200
    assert revived.json()["archived"] == 0


def test_purge_archived_hard_deletes_everything_soft_deleted(client):
    slugs = []
    for i in range(3):
        resp = client.post("/api/v1/memories", json=_payload(name=f"Purge {i}"))
        slugs.append(resp.json()["slug"])
    for slug in slugs:
        assert client.delete(f"/api/v1/memories/{slug}").status_code == 200  # soft

    result = client.delete("/api/v1/memories/archived")
    assert result.status_code == 200
    assert result.json()["purged"] >= 3

    assert client.get("/api/v1/memories", params={"archived": True}).json() == []
    for slug in slugs:
        assert client.get(f"/api/v1/memories/{slug}",
                          params={"include_archived": True}).status_code == 404


def test_graph_auto_link_shares_exact_keywords(client):
    for i in range(3):
        client.post("/api/v1/memories", json={
            "name": f"Backup note {i}", "slug": f"backup-note-{i}",
            "body": "docker backup strategy", "category": "project"})
    client.post("/api/v1/memories", json={
        "name": "Unrelated", "slug": "unrelated-note", "body": "nothing relevant here"})

    # max_coverage=1.0 turns off the ambient-word cap so these tests isolate
    # the pair logic; the cap itself is exercised by the live corpus.
    resp = client.post("/api/v1/memories/graph/auto-link",
                       params={"min_freq": 3, "max_coverage": 1.0})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["relations_created"] > 0
    assert any("backup" in p["words"] for p in data["pairs"])

    relations = client.get("/api/v1/memories/backup-note-0/relate").json()
    linked = {r["slug"] for r in relations}
    assert {"backup-note-1", "backup-note-2"} <= linked
    assert "unrelated-note" not in linked

    # Idempotent: a second pass creates nothing new.
    second = client.post("/api/v1/memories/graph/auto-link",
                         params={"min_freq": 3, "max_coverage": 1.0}).json()
    assert second["relations_created"] == 0


def test_graph_auto_link_ignores_substring_only_matches(client):
    """A word embedded inside a longer token must not count as a match."""
    for i in range(3):
        client.post("/api/v1/memories", json={
            "name": f"Keyword test {i}", "slug": f"keyword-test-{i}",
            "body": "exactword appears here", "category": "knowledge"})
    client.post("/api/v1/memories", json={
        "name": "Substring trap", "slug": "substring-trap",
        "body": "notexactwordnot is a different token", "category": "knowledge"})

    client.post("/api/v1/memories/graph/auto-link", params={"min_freq": 3, "max_coverage": 1.0})
    relations = {r["slug"] for r in client.get("/api/v1/memories/keyword-test-0/relate").json()}
    assert "substring-trap" not in relations
    assert {"keyword-test-1", "keyword-test-2"} <= relations


def test_auto_link_requires_several_shared_keywords(client):
    """Pairs sharing only one keyword stay unlinked; the cap skips ubiquitous words."""
    client.post("/api/v1/memories", json={"name": "Maple", "slug": "link-a",
                                          "body": "alpha beta", "category": "knowledge"})
    client.post("/api/v1/memories", json={"name": "Cedar", "slug": "link-b",
                                          "body": "alpha beta", "category": "knowledge"})
    client.post("/api/v1/memories", json={"name": "Pine", "slug": "link-c",
                                          "body": "alpha gamma", "category": "knowledge"})

    client.post("/api/v1/memories/graph/auto-link",
                params={"min_freq": 2, "max_coverage": 1.0, "min_shared": 2})
    linked = {r["slug"] for r in client.get("/api/v1/memories/link-a/relate").json()}
    assert "link-b" in linked
    assert "link-c" not in linked


def test_auto_link_rebuild_replaces_keyword_edges(client):
    rebuilt = client.post("/api/v1/memories/graph/auto-link",
                          params={"min_freq": 2, "max_coverage": 1.0,
                                  "min_shared": 2, "rebuild": True}).json()
    assert rebuilt["relations_created"] > 0
    relation = client.get("/api/v1/memories/link-a/relate").json()
    assert any(r["relation_type"] == "keyword" for r in relation)


# ── Degree cap: the hairball fix ─────────────────────────────────────

def _graph_ids_by_slug(data):
    return {n["slug"]: n["id"] for n in data["nodes"]}


def test_graph_without_max_degree_still_returns_every_edge(client):
    hub = client.post("/api/v1/memories", json=_payload(name="Cap Free Hub",
                                                       slug="cap-free-hub")).json()["slug"]
    leaf = client.post("/api/v1/memories", json=_payload(name="Cap Free Leaf",
                                                        slug="cap-free-leaf")).json()["slug"]
    assert client.post(f"/api/v1/memories/{hub}/relate?target={leaf}").status_code == 200

    data = client.get("/api/v1/memories/graph").json()
    assert data["max_degree"] == 0 and data["edges_dropped"] == 0
    assert data["edges_total"] == len(data["edges"])


def test_graph_max_degree_caps_every_node(client):
    hub = client.post("/api/v1/memories", json=_payload(name="Degree Hub",
                                                       slug="degree-hub")).json()["slug"]
    for i in range(5):
        leaf = client.post("/api/v1/memories",
                           json=_payload(name=f"Degree Leaf {i}",
                                         slug=f"degree-leaf-{i}")).json()["slug"]
        assert client.post(f"/api/v1/memories/{hub}/relate?target={leaf}").status_code == 200

    full = client.get("/api/v1/memories/graph").json()
    hub_id = _graph_ids_by_slug(full)[hub]
    assert sum(1 for e in full["edges"] if hub_id in (e["source"], e["target"])) == 5

    capped = client.get("/api/v1/memories/graph", params={"max_degree": 2}).json()
    degrees = {}
    for edge in capped["edges"]:
        degrees[edge["source"]] = degrees.get(edge["source"], 0) + 1
        degrees[edge["target"]] = degrees.get(edge["target"], 0) + 1
    assert max(degrees.values()) <= 2
    assert capped["max_degree"] == 2
    assert capped["edges_dropped"] == capped["edges_total"] - len(capped["edges"]) >= 3
    # Nodes are never dropped by a cap: only edges are.
    assert len(capped["nodes"]) == len(full["nodes"])


def test_graph_degree_cap_prefers_curated_relations(client):
    """An auto-linked keyword edge must lose to a hand-made one when there is
    room for only one."""
    words = "quorum replication fencing"
    names = ["Keyword Node One", "Keyword Node Two", "Keyword Node Three"]
    slugs = []
    for i, name in enumerate(names):
        created = client.post("/api/v1/memories", json={
            "name": name, "slug": f"keyword-node-{i}", "body": words,
            "category": "project", "priority": "medium"})
        slugs.append(created.json()["slug"])
    assert client.post("/api/v1/memories/graph/auto-link",
                       params={"min_freq": 2, "max_coverage": 1.0, "min_shared": 2}).status_code == 200

    # hand-made edge on top of the keyword mesh
    assert client.post(f"/api/v1/memories/{slugs[0]}/relate?target={slugs[1]}").status_code == 200

    full = client.get("/api/v1/memories/graph").json()
    first = _graph_ids_by_slug(full)[slugs[0]]
    mine = [e for e in full["edges"] if first in (e["source"], e["target"])]
    assert any(e["relation_type"] == "related" for e in mine)
    assert any(e["relation_type"] == "keyword" for e in mine)

    capped = client.get("/api/v1/memories/graph", params={"max_degree": 1}).json()
    mine = [e for e in capped["edges"] if first in (e["source"], e["target"])]
    assert len(mine) == 1 and mine[0]["relation_type"] == "related"


# ── Relation types: hide the noise, or clear it outright ─────────────

def _keyword_trio(client, prefix):
    """Three memories sharing exact rare words, so auto-link connects exactly
    this trio no matter what else the shared test database holds."""
    body = "archival retrieval provenance"
    slugs = []
    for i in range(3):
        created = client.post("/api/v1/memories", json={
            "name": f"{prefix.title()} Node {i}", "slug": f"{prefix}-node-{i}",
            "body": body, "category": "project", "priority": "medium"})
        slugs.append(created.json()["slug"])
    assert client.post("/api/v1/memories/graph/auto-link",
                       params={"min_freq": 2, "max_coverage": 1.0,
                               "min_shared": 2}).status_code == 200
    return slugs


def test_graph_relations_param_hides_auto_links(client):
    """?relations= is a whitelist: hiding 2.3k keyword edges must leave every
    node on the canvas, only the noise goes away."""
    slugs = _keyword_trio(client, "rel-filter")
    full = client.get("/api/v1/memories/graph").json()
    ids = {full["nodes"][i]["id"] for i in range(len(full["nodes"]))}
    by_slug = _graph_ids_by_slug(full)
    mine = [(by_slug[slugs[0]], by_slug[slugs[1]])]
    assert any(e["relation_type"] == "keyword" and
               {e["source"], e["target"]} == set(mine[0]) for e in full["edges"])

    curated = client.get("/api/v1/memories/graph",
                         params={"relations": "related,summarizes,depends_on"}).json()
    assert all(e["relation_type"] != "keyword" for e in curated["edges"])
    assert len(curated["nodes"]) == len(full["nodes"])
    # The checkbox labels need the counts they are hiding.
    assert curated["edges_by_type"].get("keyword", 0) > 0
    assert curated["relations"] == ["depends_on", "related", "summarizes"]
    assert ids == {n["id"] for n in curated["nodes"]}


def test_graph_relations_param_unknown_type_is_empty_not_rejected(client):
    data = client.get("/api/v1/memories/graph", params={"relations": "no-such-type"}).json()
    assert data["edges"] == [] and data["edges_total"] == 0
    assert data["nodes"], "nodes are never dropped by an edge filter"


def test_clear_auto_links_keeps_curated_relations(client):
    slugs = _keyword_trio(client, "rel-clear")
    assert client.post(f"/api/v1/memories/{slugs[0]}/relate?target={slugs[1]}").status_code == 200

    before = client.get("/api/v1/memories/graph").json()["edges_by_type"]
    assert before.get("keyword", 0) > 0

    cleared = client.delete("/api/v1/memories/graph/auto-links")
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["relation_type"] == "keyword"
    # /relate rewrites only one direction of an existing pair, so a pair can
    # hold one keyword row and one curated row: count what actually went.
    assert 0 < body["rows_deleted"] <= before["keyword"] * 2
    assert body["edges_deleted"] == body["rows_deleted"] // 2

    after = client.get("/api/v1/memories/graph").json()
    assert "keyword" not in after["edges_by_type"]
    assert after["edges_by_type"].get("related", 0) > 0, "a hand-made relation went missing"
    assert any(r["relation_type"] == "related"
               for r in client.get(f"/api/v1/memories/{slugs[0]}/relate").json())

    again = client.delete("/api/v1/memories/graph/auto-links").json()
    assert again["edges_deleted"] == 0 and again["rows_deleted"] == 0
