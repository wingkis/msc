"""Full-text search regressions.

The original schema had no INSERT trigger for the external-content FTS table,
so memories created through the API were never searchable.
"""


def test_created_memory_is_searchable(client):
    slug = "fts-regression-check"
    client.post("/api/v1/memories", json={
        "name": "FTS Regression Check", "slug": slug,
        "body": "zebra-crossover marker phrase", "category": "knowledge",
    })

    results = client.get("/api/v1/search", params={"q": "zebra-crossover"}).json()
    assert [r["slug"] for r in results] == [slug]
    assert results[0]["score"] > 0


def test_weird_queries_never_500(client):
    for q in ['AND', 'C++', '"unbalanced', 'a:b', '近field 混合', '-(x OR y)', '*']:
        resp = client.get("/api/v1/search", params={"q": q})
        assert resp.status_code == 200, f"{q!r} -> {resp.text}"


def test_text_query_that_matches_nothing_returns_nothing(client):
    assert client.get("/api/v1/search", params={"q": "zzz-no-such-phrase-zzz"}).json() == []


def test_filters_combine_with_the_query(client):
    client.post("/api/v1/memories", json={
        "name": "Combine Filter Demo", "body": "pivotword body text",
        "category": "config", "tags": ["pivot"],
    })
    assert client.get("/api/v1/search", params={"q": "pivotword", "categories": "config"}).json()
    assert client.get("/api/v1/search", params={"q": "pivotword", "categories": "user"}).json() == []


def test_recommend_related(client):
    client.post("/api/v1/memories", json={"name": "Hub Note", "slug": "hub-note",
                                          "body": "hub", "tags": ["shared-tag"]})
    client.post("/api/v1/memories", json={"name": "Spoke Note", "slug": "spoke-note",
                                          "body": "spoke", "tags": ["shared-tag"],
                                          "related_slugs": ["hub-note"]})

    results = client.get("/api/v1/search/recommend/hub-note").json()
    assert "spoke-note" in [r["slug"] for r in results]
    # Relations are stored in both directions, so each peer must be deduplicated.
    assert [r["slug"] for r in results].count("spoke-note") == 1

    linked = client.get("/api/v1/memories/hub-note/relate").json()
    assert [r["slug"] for r in linked] == ["spoke-note"]


def test_reindex_repairs_the_search_index(client, db):
    from msc.fts import fts_desync_report, rebuild_fts

    assert rebuild_fts() > 0
    assert fts_desync_report()["missing"] == 0

    resp = client.post("/api/v1/sync/reindex")
    assert resp.status_code == 200
