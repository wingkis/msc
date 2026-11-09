"""RAG: provider cards, dirty queue + worker, hybrid search, voice, insights.

Providers are always faked — these tests care about the plumbing around them:
what gets queued, what gets stored, how search degrades when nothing is
configured, and how the kill switch behaves.
"""

import base64
import hashlib
import random
import struct

from msc.config import get_settings
from msc.services import llm, rag_providers
from msc.workers import rag_worker

# Directional fake embeddings: cat/kitten and dog/puppy sit on fixed axes, so
# cosine order is predictable. Everything else gets a deterministic PRNG
# vector in 64 dimensions — distinct texts stay near-orthogonal (no accidental
# cos>0.92 pairs), identical texts embed identically (the duplicate detector
# has something real to find).
_DIM = 64
_TOKEN_AXIS = {"cat": 0, "kitten": 0, "dog": 1, "puppy": 1, "qorax": 2}


def _fake_embed_for(text: str):
    lowered = text.lower()
    for token, axis in _TOKEN_AXIS.items():
        if token in lowered:
            vec = [0.0] * _DIM
            vec[axis] = 1.0
            return vec
    rng = random.Random(hashlib.md5(text.encode("utf-8")).hexdigest())
    return [rng.uniform(-1.0, 1.0) for _ in range(_DIM)]


def _fake_embed(cfg, texts):
    return [_fake_embed_for(t) for t in texts]


def _fake_chat_ok(text="评注与长文正文。", model="fake/chat"):
    def fake_chat(cfg, messages, max_tokens=None):
        return {"text": text, "model": model, "finish_reason": "stop",
                "usage": {}, "latency_ms": 5, "reasoning_only": False}
    return fake_chat


def _make(client, name, body="内容", **extra):
    created = client.post("/api/v1/memories", json={
        "name": name, "body": body, "category": "project", "priority": "medium", **extra})
    assert created.status_code == 201, created.text
    return created.json()["slug"]


def _embed_card(client, **patch):
    response = client.put("/api/v1/model?purpose=embed",
                          json={"base_url": "http://embed.test:8080",
                                "model": "fake-embed", **patch})
    assert response.status_code == 200, response.text
    return response.json()


# ── Provider cards ───────────────────────────────────────────────────

def test_purpose_cards_are_seeded_and_independent(client):
    cards = client.get("/api/v1/model/cards").json()
    purposes = {c["purpose"] for c in cards}
    assert purposes == {"chat", "embed", "rerank", "whisper"}
    # The chat card keeps 2.x behaviour: env-seeded, unconfigured in tests.
    chat = next(c for c in cards if c["purpose"] == "chat")
    assert chat["base_url"] == "" and chat["ready"] is False
    # The old unversioned call still reads the chat card.
    assert client.get("/api/v1/model").json()["purpose"] == "chat"

    saved = client.put("/api/v1/model?purpose=rerank",
                       json={"base_url": "rerank.test:8080", "model": "fake-rerank"}).json()
    assert saved["purpose"] == "rerank" and saved["ready"] is True
    assert saved["endpoint"] == "http://rerank.test:8080/v1/rerank"  # bare host gets a scheme
    # Writing the rerank card must not touch chat or embed.
    assert client.get("/api/v1/model?purpose=chat").json()["base_url"] == ""
    assert client.get("/api/v1/model?purpose=embed").json()["base_url"] == ""

    assert client.get("/api/v1/model?purpose=bogus").status_code == 422
    assert client.put("/api/v1/model?purpose=bogus", json={}).status_code == 422


def test_embed_probe_reports_and_persists_the_dimension(client, monkeypatch):
    _embed_card(client)
    monkeypatch.setattr(rag_providers, "embed", _fake_embed)
    report = client.post("/api/v1/model/test?purpose=embed").json()
    assert report["ok"] is True and report["probe"]["dim"] == _DIM

    card = client.get("/api/v1/model?purpose=embed").json()
    assert card["probed_dim"] == _DIM and card["last_error"] is None


def test_rerank_probe_checks_the_order(client, monkeypatch):
    def fake_rerank(cfg, query, docs, top_n=10):
        # Best match is the document mentioning the query word.
        return [{"index": i, "score": 1.0 if "测试" in d else 0.1}
                for i, d in enumerate(docs)]
    monkeypatch.setattr(rag_providers, "rerank", fake_rerank)
    client.put("/api/v1/model?purpose=rerank",
               json={"base_url": "http://rerank.test:8080", "model": "fake-rerank"})
    report = client.post("/api/v1/model/test?purpose=rerank").json()
    assert report["ok"] is True


# ── Dirty queue + worker ─────────────────────────────────────────────

def test_writes_queue_embeddings_without_an_embedder_present(client):
    client.put("/api/v1/model?purpose=embed", json={"base_url": "", "model": ""})
    before = client.get("/api/v1/rag/status").json()["dirty"]
    _make(client, "Queued Write Test")
    after = client.get("/api/v1/rag/status").json()["dirty"]
    assert after >= before + 1

    # With no provider configured, the worker leaves the queue alone.
    assert rag_worker.run_once() is None
    assert client.get("/api/v1/rag/status").json()["dirty"] == after


def test_worker_drains_the_queue_with_a_faked_embedder(client, monkeypatch):
    _embed_card(client)  # idempotent: may already be configured by earlier tests
    monkeypatch.setattr(rag_providers, "embed", _fake_embed)

    slug = _make(client, "Kitten Vector Test", body="a kitten sleeps")
    depth = client.get("/api/v1/rag/status").json()["dirty"]
    assert depth >= 1

    report = rag_worker.run_once()
    assert report and report["embedded"] >= 1
    # Everything queued got vectors; the queue is empty again.
    status = client.get("/api/v1/rag/status").json()
    assert status["dirty"] == 0
    assert status["vectors"].get("fake-embed", 0) >= 1
    assert status["ready"] is True

    memory_id = client.get(f"/api/v1/memories/{slug}").json()["id"]
    vecs = [row for row in _fetch_vectors(client, memory_id)]
    assert vecs, "the new memory should carry a vector"


def _fetch_vectors(client, memory_id):
    from msc.database import SessionLocal
    from msc.models import Embedding
    db = SessionLocal()
    try:
        return [(e.model, e.dim, e.vec) for e in
                db.query(Embedding).filter(Embedding.memory_id == memory_id).all()]
    finally:
        db.close()


def test_worker_survives_an_outage_and_retries(client, monkeypatch):
    _embed_card(client)
    slug = _make(client, "Outage Retry Test", body="puppy")

    def boom(cfg, texts):
        raise llm.LLMError("连接失败: embedder down")
    monkeypatch.setattr(rag_providers, "embed", boom)

    report = rag_worker.run_once()
    assert report and report["errors"], "the outage must be reported"
    assert client.get("/api/v1/rag/status").json()["dirty"] >= 1  # queue kept

    card = client.get("/api/v1/model?purpose=embed").json()
    assert "embedder down" in (card["last_error"] or "")

    monkeypatch.setattr(rag_providers, "embed", _fake_embed)
    report = rag_worker.run_once()
    assert report and report["embedded"] >= 1
    assert client.get(f"/api/v1/memories/{slug}").status_code == 200


# ── Search modes ─────────────────────────────────────────────────────

def test_search_modes_and_semantic_recall(client, monkeypatch):
    monkeypatch.setattr(rag_providers, "embed", _fake_embed)
    # A memory only reachable semantically: the query shares no keywords with
    # it — "哪个宠物在睡觉" FTS/LIKE finds nothing, the vector finds the puppy.
    _make(client, "Puppy Napping", body="the puppy naps on the rug")
    assert rag_worker.run_once() is not None  # drain any leftovers from earlier tests

    hits = client.get("/api/v1/search", params={"q": "puppy"}).json()
    keyword_ids = {h["slug"] for h in hits}
    assert "puppy-napping" in keyword_ids
    assert all("match" not in h for h in hits)  # keyword mode keeps the 2.x shape

    semantic = client.get("/api/v1/search", params={"q": "cat", "mode": "semantic"}).json()
    slugs = {h["slug"] for h in semantic}
    assert "kitten-vector-test" in slugs
    assert all(h["match"] == "semantic" for h in semantic)

    hybrid = client.get("/api/v1/search", params={"q": "puppy", "mode": "hybrid"}).json()
    matches = {h["slug"]: h["match"] for h in hybrid}
    assert "puppy-napping" in matches
    assert matches["puppy-napping"] == "both"  # found by FTS and by cosine

    direct = client.get("/api/v1/rag/search", params={"q": "puppy"}).json()
    assert direct["results"], "direct semantic search finds it"
    assert direct["model"] == "fake-embed"


def test_semantic_search_degrades_without_a_provider(client, monkeypatch):
    # Configured card, provider that refuses: keyword results come back and
    # the failure is surfaced in a header instead of a 500.
    _embed_card(client)
    monkeypatch.setattr(rag_providers, "embed",
                        lambda cfg, texts: (_ for _ in ()).throw(
                            llm.LLMError("连接失败: refused")))
    assert client.post("/api/v1/memories", json={
        "name": "Embedding Refusal Check", "body": "embedding refusal"}).status_code == 201

    response = client.get("/api/v1/search", params={"q": "embedding", "mode": "hybrid"})
    assert response.status_code == 200
    results = response.json()
    assert "embedding-refusal-check" in {r["slug"] for r in results}
    assert "refused" in response.headers.get("X-Semantic-Error", "")


def test_kill_switch_reverts_to_2_x_behaviour(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_enabled", False)

    status = client.get("/api/v1/rag/status").json()
    assert status["enabled"] is False
    assert client.get("/api/v1/rag/health-report").status_code == 503
    assert client.post("/api/v1/rag/reindex").status_code == 503
    # /search still answers, and semantic modes behave as keyword.
    response = client.get("/api/v1/search", params={"q": "puppy", "mode": "semantic"})
    assert response.status_code == 200
    assert "puppy-napping" in {r["slug"] for r in response.json()}
    assert rag_worker.run_once() is None


# ── Reindex / purge / import ─────────────────────────────────────────

def test_reindex_queues_everything_and_import_backfills(client):
    result = client.post("/api/v1/rag/reindex").json()
    assert result["queued"] >= 1
    assert client.get("/api/v1/rag/status").json()["dirty"] >= 1

    # Import bypasses the worker: base64 float32 straight into the table.
    slug = _make(client, "Imported Vector Memo", body="puppy from the sidecar")
    memory_id = client.get(f"/api/v1/memories/{slug}").json()["id"]
    vec = struct.pack("<4f", 0.0, 1.0, 0.0, 0.0)
    imported = client.post("/api/v1/rag/import", json={
        "model": "sidecar-embed",
        "entries": [
            {"memory_id": memory_id, "dim": 4, "vec": base64.b64encode(vec).decode()},
            {"slug": "no-such-slug", "dim": 4, "vec": base64.b64encode(vec).decode()},
            {"memory_id": memory_id, "dim": 7, "vec": base64.b64encode(vec).decode()},
        ],
    }).json()
    assert imported["imported"] == 1 and imported["skipped"] == 2

    vectors = _fetch_vectors(client, memory_id)
    assert ("sidecar-embed", 4, vec) in vectors


def test_vector_purge_protects_the_active_model(client):
    blocked = client.delete("/api/v1/rag/vectors", params={"model": "fake-embed"})
    assert blocked.status_code == 422  # it is the embed card's model right now

    purged = client.delete("/api/v1/rag/vectors", params={"model": "sidecar-embed"})
    assert purged.status_code == 200 and purged.json()["removed"] >= 1
    assert "sidecar-embed" not in client.get("/api/v1/rag/status").json()["vectors"]


def test_settings_tuning_round_trip(client):
    saved = client.put("/api/v1/rag/settings", json={"recall_top": 42}).json()
    assert saved["settings"]["recall_top"] == 42
    assert client.get("/api/v1/rag/status").json()["settings"]["recall_top"] == 42
    client.put("/api/v1/rag/settings", json={"recall_top": 30})
    assert client.put("/api/v1/rag/settings", json={"recall_top": 0}).status_code == 422


# ── Voice ────────────────────────────────────────────────────────────

def test_transcribe_reports_text_and_duplicates(client, monkeypatch):
    async def fake_transcribe_bytes(cfg, filename, content):
        return {"text": "the puppy naps on the rug again", "latency_ms": 7}

    def fake_transcribe(cfg, filename, content):
        return {"text": "the puppy naps on the rug again", "latency_ms": 7}

    monkeypatch.setattr(rag_providers, "transcribe", fake_transcribe)
    monkeypatch.setattr(rag_providers, "embed", _fake_embed)
    client.put("/api/v1/model?purpose=whisper",
               json={"base_url": "http://whisper.test:8081", "model": "fake-whisper"})

    response = client.post("/api/v1/rag/transcribe",
                           files={"file": ("note.wav", b"RIFFfakewavdata", "audio/wav")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "puppy" in body["text"]
    assert body["similar"], "cos>0.6 duplicate candidates come back"
    assert any(item["slug"] == "puppy-napping" for item in body["similar"])

    bad = client.post("/api/v1/rag/transcribe",
                      files={"file": ("note.txt", b"not audio", "text/plain")})
    assert bad.status_code == 422
    empty = client.post("/api/v1/rag/transcribe",
                        files={"file": ("note.wav", b"", "audio/wav")})
    assert empty.status_code == 422


# ── Insights ─────────────────────────────────────────────────────────

def test_health_report_finds_near_duplicates(client, monkeypatch):
    monkeypatch.setattr(rag_providers, "embed", _fake_embed)
    # qorax sits on its own axis, so this pair is the only >0.92 collision.
    _make(client, "Duplicate One", body="qorax probe")
    _make(client, "Duplicate Two", body="qorax probe twin")
    rag_worker.run_once()

    report = client.get("/api/v1/rag/health-report").json()
    pairs = report["duplicates"]
    assert any({p["a"]["slug"], p["b"]["slug"]} >=
               {"duplicate-one", "duplicate-two"} for p in pairs)
    assert report["suggestion"]


def test_daily_pick_generates_once_and_caches(client, monkeypatch):
    client.put("/api/v1/model", json={"base_url": "http://chat.test:1234",
                                      "model": "fake/chat"})
    calls = []

    def counting_chat(cfg, messages, max_tokens=None):
        calls.append(messages)
        return {"text": "今夜的记忆长文。", "model": "fake/chat", "finish_reason": "stop",
                "usage": {}, "latency_ms": 9, "reasoning_only": False}
    monkeypatch.setattr(llm, "chat", counting_chat)

    first = client.get("/api/v1/rag/daily-pick").json()
    assert first["cached"] is False and first["body"] == "今夜的记忆长文。"
    assert len(calls) == 1

    second = client.get("/api/v1/rag/daily-pick").json()
    assert second["cached"] is True
    assert len(calls) == 1  # the cache means one model call per day

    third = client.get("/api/v1/rag/daily-pick", params={"refresh": "true"}).json()
    assert third["cached"] is False and len(calls) == 2

    # Leave the chat card as the digest tests expect it: unconfigured.
    client.put("/api/v1/model", json={"base_url": "", "model": ""})


def test_echo_comments_without_writing(client, monkeypatch):
    monkeypatch.setattr(llm, "chat", _fake_chat_ok())
    client.put("/api/v1/model", json={"base_url": "http://chat.test:1234",
                                      "model": "fake/chat"})
    slug = _make(client, "Echo Subject", body="something worth revisiting")
    before = client.get("/api/v1/memories", params={"q": "Echo"}).json()

    result = client.post("/api/v1/rag/echo", json={"slug": slug}).json()
    assert result["slug"] == slug and "评注" in result["body"]

    after = client.get("/api/v1/memories", params={"q": "Echo"}).json()
    assert [m["slug"] for m in after] == [m["slug"] for m in before]  # nothing saved

    assert client.post("/api/v1/rag/echo", json={"slug": "nope-missing"}).status_code == 404
    client.put("/api/v1/model", json={"base_url": "", "model": ""})  # leave chat unconfigured


def test_heatmap_counts_by_day(client):
    heatmap = client.get("/api/v1/rag/heatmap", params={"days": 30}).json()
    assert heatmap["days"] == 30 and heatmap["counts"]
    today = sum(c["count"] for c in heatmap["counts"])
    assert today >= 1
