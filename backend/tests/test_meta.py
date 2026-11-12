"""Self-describing endpoints and LAN-mode behaviour."""


def test_health_reports_mode_and_self_check(client):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["mode"] == "lan"
    assert body["auth_enabled"] is False
    assert body["database"] == "ok"
    assert body["search_index"]["unindexed"] == 0


def test_config_tells_clients_no_login_is_needed(client):
    body = client.get("/api/v1/config").json()
    assert body == {**body, "auth_enabled": False, "mode": "lan"}
    assert body["version"]


def test_meta_advertises_the_vocab_an_agent_must_use(client):
    body = client.get("/api/v1/meta").json()
    assert "reference" in body["vocab"]["category"]
    assert "critical" in body["vocab"]["priority"]
    assert body["tags"]


def test_tags_and_stats_and_help(client):
    client.post("/api/v1/memories", json={"name": "Meta Fixture Note", "slug": "meta-fixture-note",
                                          "body": "meta fixture", "tags": ["metafixture"]})

    tags = client.get("/api/v1/tags").json()
    assert tags and all({"name", "count"} <= set(t) for t in tags)
    assert "metafixture" in [t["name"] for t in tags]

    stats = client.get("/api/v1/stats").json()
    assert stats["total"] >= 1

    help_text = client.get("/api/v1/help")
    assert help_text.status_code == 200
    assert "/api/v1/memories" in help_text.text
    # an agent that only curls the API must still learn how to *write* provenance
    for needle in ('"refs":', "X-Provenance-Hint", "memories/preflight", "X-Search-Status"):
        assert needle in help_text.text, needle

    llms = client.get("/llms.txt")
    assert llms.status_code == 200
    assert "MSC" in llms.text
    assert '"refs":' in llms.text


def test_graph_returns_nodes_and_edges(client):
    client.post("/api/v1/memories", json={"name": "Graph Hub", "slug": "graph-hub", "body": "hub"})
    client.post("/api/v1/memories", json={"name": "Graph Leaf", "slug": "graph-leaf", "body": "leaf",
                                          "related_slugs": ["graph-hub"]})

    graph = client.get("/api/v1/memories/graph").json()
    slugs = {n["slug"] for n in graph["nodes"]}
    assert {"graph-hub", "graph-leaf"} <= slugs
    assert any({"source", "target"} and e["relation_type"] == "related" for e in graph["edges"])
    assert all(set(e) == {"source", "target", "relation_type"} for e in graph["edges"])
