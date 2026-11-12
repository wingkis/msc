"""Provenance: refs, contradiction, and the only way back.

A memory is a historical observation. These tests hold the two properties that
make that safe to inject into a session: a memory can be contradicted by a
re-check, and a contradicted memory cannot come back as a quiet fact.
"""

import pytest
from sqlalchemy import text

# Every memory this file creates is torn down at module end. Without that, a
# later module that counts the default list (limit 50) breaks purely because
# this suite exists — an order dependency, not a real failure.
_CREATED: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _clean_up(client):
    yield
    for slug in _CREATED:
        client.delete(f"/api/v1/memories/{slug}")
    _CREATED.clear()


def _create(client, name, **overrides):
    created = client.post("/api/v1/memories", json=_memo(name, **overrides))
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    _CREATED.append(slug)
    return slug


def _memo(name, **overrides):
    data = {"name": name, "body": f"{name} body about provtest", "category": "project",
            "priority": "medium", "tags": ["provtest"]}
    data.update(overrides)
    return data


def _status_of(client, slug):
    got = client.get(f"/api/v1/memories/{slug}", params={"include_invalidated": True})
    assert got.status_code == 200, got.text
    return got.json()


def test_refs_roundtrip_and_status(client):
    slug = _create(client, "Prov Refs", refs=[
        "git:a802674", "file:/home/astro/msc/backend/msc/main.py",
        "url:http://192.168.29.50:8000/api/v1/health", "mem:prov-refs"])
    body = _status_of(client, slug)
    assert body["status"] == "active" and body["slug"] == slug
    assert body["refs"] == ["git:a802674", "file:/home/astro/msc/backend/msc/main.py",
                            "url:http://192.168.29.50:8000/api/v1/health", "mem:prov-refs"]


def test_ref_without_a_known_prefix_is_rejected(client):
    bad = client.post("/api/v1/memories", json=_memo("Prov Bad Ref", refs=["commit:abc123"]))
    assert bad.status_code == 422, bad.text
    detail = bad.text
    for kind in ("git", "file", "url", "mem", "cmd", "host"):
        assert kind in detail, detail
    bad2 = client.post("/api/v1/memories", json=_memo("Prov Empty Ref", refs=["git:"]))
    assert bad2.status_code == 422, bad2.text


def test_refs_are_normalised_and_deduplicated(client):
    slug = _create(client, "Prov Dedup",
                   refs=["GIT:deadbeef", " git:deadbeef ", "mem:prov-dedup", ""])
    assert _status_of(client, slug)["refs"] == ["git:deadbeef", "mem:prov-dedup"]


def test_a_write_that_stores_no_refs_says_so(client):
    """The hint is a nudge on the response, never a reason to refuse the write."""
    bare = client.post("/api/v1/memories", json=_memo("Prov Hint Bare"))
    assert bare.status_code == 201, bare.text
    _CREATED.append(bare.json()["slug"])
    assert "X-Provenance-Hint" in bare.headers
    assert "host:" in bare.headers["X-Provenance-Hint"]

    cited = client.post("/api/v1/memories", json=_memo("Prov Hint Cited", refs=["mem:prov-hint-bare"]))
    assert cited.status_code == 201, cited.text
    _CREATED.append(cited.json()["slug"])
    assert "X-Provenance-Hint" not in cited.headers

    # PUT replaces refs when they are sent, and keeps them when they are not.
    stripped = client.put(f"/api/v1/memories/{cited.json()['slug']}", json={"refs": []})
    assert stripped.status_code == 200, stripped.text
    assert stripped.json()["refs"] == []
    assert "X-Provenance-Hint" in stripped.headers

    kept = client.put(f"/api/v1/memories/{bare.json()['slug']}", json={"refs": ["host:ci-runner"]})
    assert kept.status_code == 200, kept.text
    assert "X-Provenance-Hint" not in kept.headers
    untouched = client.put(f"/api/v1/memories/{bare.json()['slug']}", json={"body": "still cited"})
    assert untouched.status_code == 200, untouched.text
    assert untouched.json()["refs"] == ["host:ci-runner"]
    assert "X-Provenance-Hint" not in untouched.headers

    # upsert-create goes through a different path but gets the same treatment
    seeded = client.put("/api/v1/memories/prov-hint-upsert", json=_memo("Prov Hint Upsert"))
    assert seeded.status_code == 200, seeded.text
    _CREATED.append("prov-hint-upsert")
    assert "X-Provenance-Hint" in seeded.headers


def test_invalidate_hides_by_default_but_never_quietly(client):
    slug = _create(client, "Prov Contra")

    invalidated = client.post(f"/api/v1/memories/{slug}/invalidate", json={
        "reason": "git log shows a802674 shipped it; the body claimed the HTML was never wired"})
    assert invalidated.status_code == 200, invalidated.text
    data = invalidated.json()
    assert data["status"] == "contradicted"
    assert data["invalidated_at"] and "a802674" in data["invalidated_reason"]
    assert data["verified_at"] is None

    slugs = [m["slug"] for m in client.get("/api/v1/memories", params={"tag": "provtest"}).json()]
    assert slug not in slugs
    # The default pull announces what it hid; asking for everything back is the
    # opt-out, not the place the count lives.
    hiding = client.get("/api/v1/memories", params={"tag": "provtest"})
    assert int(hiding.headers["X-Contradicted-Hidden"]) >= 1
    kept = client.get("/api/v1/memories", params={"tag": "provtest", "include_invalidated": "true"})
    assert slug in [m["slug"] for m in kept.json()]

    # Detail by slug still works: contradicted is not deleted, only de-throned.
    assert client.get(f"/api/v1/memories/{slug}").status_code == 200


def test_search_drops_contradicted_and_reports_the_drop(client):
    slug = _create(client, "Prov Search Contra", body="provsearchonly body text")
    assert client.post(f"/api/v1/memories/{slug}/invalidate",
                       json={"reason": "superseded by a re-check"}).status_code == 200

    hits = client.get("/api/v1/search", params={"q": "provsearchonly"})
    assert hits.status_code == 200
    assert slug not in [h["slug"] for h in hits.json()]
    assert hits.headers["X-Contradicted-Hidden"] == "1"

    both = client.get("/api/v1/search", params={"q": "provsearchonly", "include_invalidated": "true"})
    found = [h for h in both.json() if h["slug"] == slug]
    assert found and found[0]["status"] == "contradicted"


def test_search_status_distinguishes_no_match_from_a_broken_index(client):
    none = client.get("/api/v1/search", params={"q": "zzz-no-such-memory-anywhere-zzz"})
    assert none.status_code == 200
    assert none.json() == []
    assert none.headers["X-Search-Status"] == "no_match"

    ok = client.get("/api/v1/search", params={"q": "provtest"})
    assert ok.headers["X-Search-Status"] == "ok"
    assert int(ok.headers["X-Search-Matched"]) >= 1


def test_superseded_by_creates_the_edge_and_verify_ok_removes_it(client):
    old = _create(client, "Prov Old")
    new = _create(client, "Prov New")

    done = client.post(f"/api/v1/memories/{old}/invalidate",
                       json={"reason": "state moved on", "superseded_by": new})
    assert done.status_code == 200, done.text

    from msc.database import SessionLocal
    from msc.models import Memory, MemoryRelation
    db = SessionLocal()
    try:
        old_id = db.query(Memory).filter(Memory.slug == old).first().id
        new_id = db.query(Memory).filter(Memory.slug == new).first().id
        rows = db.query(MemoryRelation).filter(
            MemoryRelation.memory_id_a == new_id,
            MemoryRelation.memory_id_b == old_id,
            MemoryRelation.relation_type == "supersedes").all()
        assert len(rows) == 1
    finally:
        db.close()

    verified = client.post(f"/api/v1/memories/{old}/verify", json={"verdict": "ok"})
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "active"
    assert verified.json()["invalidated_at"] is None
    assert verified.json()["verified_at"]

    db = SessionLocal()
    try:
        assert db.execute(text(
            "SELECT count(*) FROM memory_relations WHERE relation_type = 'supersedes' "
            "AND memory_id_b = (SELECT id FROM memories WHERE slug = :s)"),
            {"s": old}).scalar() == 0
    finally:
        db.close()


def test_editing_does_not_clear_a_contradiction(client):
    """The turn channel PUTs a whole body every round. If that revived a memory,
    every contradicted belief would evaporate on the next turn."""
    slug = _create(client, "Prov Edit Revive")
    client.post(f"/api/v1/memories/{slug}/invalidate", json={"reason": "wrong at the time"})
    updated = client.put(f"/api/v1/memories/{slug}", json={"body": "rewritten body provrewrite"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "contradicted"
    assert [m["slug"] for m in client.get("/api/v1/memories", params={"q": "provrewrite"}).json()] == []

    stale = client.put(f"/api/v1/memories/{slug}", json={"refs": ["mem:prov-edit-revive"]})
    assert stale.json()["status"] == "contradicted"


def test_verify_verdicts_and_the_reason_requirement(client):
    slug = _create(client, "Prov Verify")

    no_reason = client.post(f"/api/v1/memories/{slug}/verify", json={"verdict": "stale"})
    assert no_reason.status_code == 422, no_reason.text
    assert "reason" in no_reason.text

    stale = client.post(f"/api/v1/memories/{slug}/verify",
                        json={"verdict": "stale", "reason": "provider moved hosts"})
    assert stale.status_code == 200 and stale.json()["status"] == "stale"
    # stale is a warning, not a suppression: it stays in the default list
    assert slug in [m["slug"] for m in client.get("/api/v1/memories", params={"tag": "provtest"}).json()]

    assert client.post(f"/api/v1/memories/{slug}/verify",
                       json={"verdict": "nope"}).status_code == 422
    back = client.post(f"/api/v1/memories/{slug}/verify", json={"verdict": "ok"})
    assert back.json()["status"] == "active"


def test_invalidate_is_idempotent(client):
    slug = _create(client, "Prov Twice")
    first = client.post(f"/api/v1/memories/{slug}/invalidate", json={"reason": "same verdict"}).json()
    again = client.post(f"/api/v1/memories/{slug}/invalidate", json={"reason": "same verdict"}).json()
    assert first["invalidated_at"] == again["invalidated_at"]


def test_supersede_self_and_unknown_successor_are_rejected(client):
    slug = _create(client, "Prov Self")
    assert client.post(f"/api/v1/memories/{slug}/invalidate",
                       json={"reason": "x", "superseded_by": slug}).status_code == 422
    assert client.post(f"/api/v1/memories/{slug}/invalidate",
                       json={"reason": "x", "superseded_by": "no-such-slug-anywhere"}).status_code == 404


def test_preflight_reports_verdicts_without_writing_anything(client):
    live = _create(client, "Prov Probe Live",
                   refs=["mem:prov-probe-live", "file:/etc/hostname"])
    dead = _create(client, "Prov Probe Dead", refs=["mem:prov-nothing-has-this-slug"])
    remote = _create(client, "Prov Probe Remote", refs=["git:1234567"])

    report = client.get("/api/v1/memories/preflight",
                        params={"tag": "provtest", "limit": 100, "budget_ms": 3000})
    assert report.status_code == 200, report.text
    by_slug = {item["slug"]: item for item in report.json()["items"]}

    assert live in by_slug and by_slug[live]["verdict"] == "active"
    assert dead in by_slug and by_slug[dead]["verdict"] == "contradicted"
    assert remote in by_slug and by_slug[remote]["verdict"] == "uncheckable"
    assert by_slug[remote]["probes"][0]["verdict"] == "unchecked"

    # A GET that rewrote the library would also be a GET that corrupted it.
    assert _status_of(client, dead)["status"] == "active"


def test_null_status_from_an_upgraded_database_stays_visible(client):
    """ALTER TABLE leaves NULL on old rows; a naive != filter would hide them all."""
    slug = _create(client, "Prov Null Status")
    from msc.database import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("UPDATE memories SET status = NULL, refs = NULL WHERE slug = :s"), {"s": slug})
        db.commit()
    finally:
        db.close()

    listed = client.get("/api/v1/memories", params={"tag": "provtest"}).json()
    assert slug in [m["slug"] for m in listed]
    fetched = client.get(f"/api/v1/memories/{slug}").json()
    assert fetched["status"] == "active" and fetched["refs"] == []
    assert slug in [h["slug"] for h in client.get("/api/v1/search", params={"q": "provtest"}).json()]


def test_a_url_that_refuses_the_verb_is_not_a_contradiction(client):
    """405 means "not this method"; 401 means "here, but not for you". Reading
    either as a gone endpoint would let a probe invent staleness."""
    from msc.api.memories import _url_verdict

    assert _url_verdict(200)[0] == "ok"
    assert _url_verdict(301)[0] == "ok"
    assert _url_verdict(404)[0] == "missing"
    assert _url_verdict(405)[0] == "retry"
    assert _url_verdict(501)[0] == "retry"
    assert _url_verdict(401)[0] == "unchecked"
    assert _url_verdict(403)[0] == "unchecked"
    assert _url_verdict(500)[0] == "unchecked"
    assert "unwell" in _url_verdict(503)[1]


def test_preflight_of_a_contradicted_memory_offers_the_way_back(client):
    slug = _create(client, "Prov Preflight Contra")
    client.post(f"/api/v1/memories/{slug}/verify",
                json={"verdict": "contradicted", "reason": "checked and wrong"})
    client.put(f"/api/v1/memories/{slug}", json={"refs": ["mem:prov-preflight-contra"]})

    report = client.get("/api/v1/memories/preflight", params={"tag": "provtest", "limit": 100})
    item = [i for i in report.json()["items"] if i["slug"] == slug][0]
    assert item["status"] == "contradicted"      # still shown, so it can be re-checked
    assert item["verdict"] == "active"           # and the probe says it holds again
    assert report.json()["hidden"] >= 1


def test_contradicted_ids_is_the_single_null_safe_definition(client):
    """``status = 'contradicted'`` is NULL-safe in the direction that matters:
    a row upgraded in by ALTER TABLE has NULL and is not a contradiction."""
    from msc.api.rag import _contradicted_ids
    from msc.database import SessionLocal
    from msc.models import Memory

    slug = _create(client, "Prov Rag Legacy Null")
    db = SessionLocal()
    try:
        db.execute(text("UPDATE memories SET status = NULL WHERE slug = :s"), {"s": slug})
        db.commit()
        memory_id = db.query(Memory).filter_by(slug=slug).first().id
        assert memory_id not in _contradicted_ids(db)
    finally:
        db.close()

    assert client.post(f"/api/v1/memories/{slug}/invalidate",
                       json={"reason": "provtest rag leg"}).status_code == 200
    db = SessionLocal()
    try:
        assert memory_id in _contradicted_ids(db)
    finally:
        db.close()


def test_the_voice_and_daily_pick_legs_share_the_filter(client):
    """The voice 「similar」 list offers places to append a new fact; daily-pick
    chooses material for the long-form. Both read the same vectors, and both
    need a live provider to drive end to end, so what is pinned here is the
    thing that keeps them honest: they ask the same helper for the contradicted
    ids instead of inventing a predicate that forgets NULL."""
    import inspect
    from msc.api import rag

    for endpoint in (rag.rag_search, rag.transcribe, rag.daily_pick):
        assert "_contradicted_ids(db)" in inspect.getsource(endpoint), endpoint.__name__


def test_the_client_probe_answers_verdict_tab_detail():
    """cli/msc-verify.sh splits its own output on a tab, so every branch of
    probe_one must emit one. The host: branch shipped ``\\written`` — a stray
    backslash where the escape should be — so a ref that belongs to another
    machine came back as one unsplit blob, matched no case, and was counted as
    neither ok nor unchecked: the 「几条属于别的机器」 number read 0."""
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "cli" / "msc-verify.sh"
    source = script.read_text(encoding="utf-8")
    start = source.index("probe_one() {")
    body = source[start:source.index("\n}\n", start) + 3]

    for ref, want in (("host:mmm6", "unchecked"), ("host:dgx", "ok"),
                      ("cmd:ls -l", "unchecked"), ("file:/nonexistent/qorax", "missing")):
        run = subprocess.run(["sh", "-c", f'{body}\nHOST_NOW=dgx\nprobe_one {ref}'],
                             capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr
        verdict, separator, detail = run.stdout.partition("\t")
        assert separator == "\t", f"{ref} answered without a separator: {run.stdout!r}"
        assert verdict == want, f"{ref}: {run.stdout!r}"
        assert detail.strip(), f"{ref}: no detail for {want}"


def test_a_path_outside_the_mount_set_is_not_called_gone(client):
    """file:/home/astro/msc is real on the writer's machine and simply not
    mounted inside the container. Answering 「path is gone」 there contradicts a
    memory for a reason that has nothing to do with the memory — the file: twin
    of reading 405 as a dead endpoint. Absence only counts as evidence where
    this server could have seen the file."""
    from msc.api.memories import _mount_targets, _probe_ref
    from msc.config import get_settings
    from msc.database import SessionLocal

    memory_dir = str(get_settings().memory_dir)
    assert memory_dir in _mount_targets()

    db = SessionLocal()
    try:
        inside = _probe_ref(db, f"file:{memory_dir}/prov-note-nobody-wrote.md")
        assert inside.verdict == "missing", inside.detail      # mounted: absence is evidence
        elsewhere = _probe_ref(db, "file:/etc/prov-note-nobody-wrote.conf")
        assert elsewhere.verdict == "unchecked", elsewhere.detail  # not mounted: no verdict
        present = _probe_ref(db, f"file:{memory_dir}")
        assert present.verdict == "ok", present.detail         # exists: no argument needed
    finally:
        db.close()
