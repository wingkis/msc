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
        "url:http://192.168.29.50:8000/api/v1/health", "mem:prov-refs",
        "ip:192.168.29.50"])
    body = _status_of(client, slug)
    assert body["status"] == "active" and body["slug"] == slug
    assert body["refs"] == ["git:a802674", "file:/home/astro/msc/backend/msc/main.py",
                            "url:http://192.168.29.50:8000/api/v1/health", "mem:prov-refs",
                            "ip:192.168.29.50"]


def test_ref_without_a_known_prefix_is_rejected(client):
    bad = client.post("/api/v1/memories", json=_memo("Prov Bad Ref", refs=["commit:abc123"]))
    assert bad.status_code == 422, bad.text
    detail = bad.text
    for kind in ("git", "file", "url", "mem", "cmd", "host", "ip"):
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


def test_one_ref_can_be_asked_who_believes_it(client):
    """The highest-leverage re-check is not per memory but per handle: a port is
    being retired, a file is about to move, a commit is getting reverted. Which
    memories stand on it? Matches whole array elements, so /etc/host does not
    answer for /etc/hostname, and a _ in a path is a character, not a wildcard.
    """
    narrow = _create(client, "Prov Impact Narrow", refs=["file:/etc/host"])
    broad = _create(client, "Prov Impact Broad", refs=["file:/etc/hostname"])
    _create(client, "Prov Impact None")

    found = client.get("/api/v1/memories", params={"ref": "file:/etc/host", "tag": "provtest"}).json()
    assert [item["slug"] for item in found] == [narrow]
    assert client.get("/api/v1/memories", params={"ref": "file:/etc/hostname",
                                                  "tag": "provtest"}).json()[0]["slug"] == broad
    assert client.get("/api/v1/memories", params={"ref": "file:/etc/nosuch",
                                                  "tag": "provtest"}).json() == []


def test_related_recommendations_drop_a_retired_belief(client):
    """The 「相关」 leg reads relations, and relations survive an invalidation:
    without this filter a contradicted memory keeps being handed back as the
    neighbour of whatever is being read."""
    live = _create(client, "Prov Rec Live")
    retired = _create(client, "Prov Rec Retired")
    client.post(f"/api/v1/memories/{live}/relate", params={"target": retired})
    assert client.post(f"/api/v1/memories/{retired}/invalidate",
                       json={"reason": "re-check said no"}).status_code == 200

    recommended = client.get(f"/api/v1/search/recommend/{live}").json()
    assert retired not in [item["slug"] for item in recommended]
    # the edge is still there — the belief is retired, not erased
    assert retired in [node["slug"] for node in
                       client.get("/api/v1/memories/graph", params={"center": live}).json()["nodes"]]


def test_the_graph_draws_a_retired_node_instead_of_hiding_it(client):
    """The graph is the audit surface: what a retired belief was still wired to
    is exactly what a reviewer needs. So the node stays, carrying its status."""
    live = _create(client, "Prov Graph Live")
    retired = _create(client, "Prov Graph Retired")
    client.post(f"/api/v1/memories/{live}/relate", params={"target": retired})
    client.post(f"/api/v1/memories/{retired}/invalidate", json={"reason": "stale by host"})

    nodes = {n["slug"]: n for n in
             client.get("/api/v1/memories/graph", params={"center": live}).json()["nodes"]}
    assert nodes[retired]["status"] == "contradicted"
    assert nodes[live]["status"] == "active"


def test_a_service_that_probes_itself_says_so_instead_of_guessing(client):
    """Answering a probe of your own endpoint from inside that request is not a
    check — it is queueing behind yourself. Silence here once read "the endpoint
    is gone", which contradicted memories for nothing."""
    base = client.get("/api/v1/meta").json()["base_url"]
    slug = _create(client, "Prov Self Probe", refs=[
        f"url:{base}/api/v1/help", f"url:{base}/api/v1/preflight",
        "url:http://127.0.0.1:1/nothing-listens-here"])

    item = client.get("/api/v1/memories/preflight", params={"slug": slug}).json()["items"][0]
    verdicts = {probe["ref"]: probe for probe in item["probes"]}
    for own in (f"url:{base}/api/v1/help", f"url:{base}/api/v1/preflight"):
        assert verdicts[own]["verdict"] == "unchecked" and "oneself" in verdicts[own]["detail"]
    # refused connection is real evidence, and stays `missing`
    assert verdicts["url:http://127.0.0.1:1/nothing-listens-here"]["verdict"] == "missing"
    # a memory whose only hard evidence is a self-probe is uncheckable, not dead
    assert item["verdict"] == "contradicted"  # the refused port is genuinely gone


def test_preflight_can_narrow_to_a_single_memory(client):
    """The UI re-checks the row it has open. Sweeping the whole library to ask
    about one belief is the wrong tool, and a GET must still never write."""
    slug = _create(client, "Prov Preflight One", refs=["mem:prov-nothing-has-this-slug"])
    _create(client, "Prov Preflight Two", refs=["file:/etc/hostname"])

    report = client.get("/api/v1/memories/preflight",
                        params={"slug": slug, "budget_ms": 4000}).json()
    assert report["checked"] == 1
    item = report["items"][0]
    assert item["slug"] == slug and item["verdict"] == "contradicted"
    assert item["probes"][0]["verdict"] == "missing"
    # reported, not applied
    assert _status_of(client, slug)["status"] == "active"

    # an unknown slug narrows to nothing rather than falling back to the sweep
    assert client.get("/api/v1/memories/preflight",
                      params={"slug": "prov-nothing-has-this-slug"}).json()["items"] == []


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
    neither ok nor unchecked: the 「几条属于别的机器」 number read 0.

    The slice starts at ``norm_host`` because probe_one leans on it, and on
    ``HOST_ME``/``MY_IPS``: comparing ``hostname -s`` raw against a ref lost a
    host to its own mDNS suffix and to case."""
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "cli" / "msc-verify.sh"
    source = script.read_text(encoding="utf-8")
    body = source[source.index("norm_host() {"):
                  source.index("\n}\n", source.index("probe_one() {")) + 3]
    # a nonexistent path under our own home: same OS, same owner, so the only
    # thing left standing between it and "missing" is the machine-scope rule
    dead = f"file:{Path.home()}/msc-probe-qorax-{__name__}"
    # 192.0.2.0/24 is TEST-NET: reserved, so it can never be a real address of
    # the box running this test, which is what makes the two cases below safe
    HERE, THERE = "192.0.2.1", "192.0.2.9"

    def ask(ref, *, host_me="dgx", origin=(("name", "dgx"),), my_ips=HERE):
        # set_scope, not hand-set globals: the scope decision is derived from
        # the document, so a test that wrote _file_scope itself could pass
        # while check_slug still derived it wrong. MY_IPS is assigned *after*
        # the slice because the slice works out the real box's addresses.
        shell = f'HOST_NOW=dgx.local\n{body}\nMY_IPS="{my_ips}"\nMY_LABEL="{my_ips}"\n'
        shell += f'HOST_ME="{host_me}"\nset_scope "'
        shell += "".join(f"{kind}\t{value}\n" for kind, value in origin)
        shell += f'"\nprobe_one "{ref}"'
        run = subprocess.run(["sh", "-c", shell],
                             capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr
        verdict, separator, detail = run.stdout.partition("\t")
        assert separator == "\t", f"{ref} answered without a separator: {run.stdout!r}"
        return verdict, detail

    for ref, want in (("host:mmm6", "unchecked"), ("host:dgx", "ok"),
                      ("host:DGX", "ok"), ("host:dgx.local", "ok"),
                      (f"ip:{HERE}", "ok"), (f"ip:{THERE}", "unchecked"),
                      (f"host:{HERE}", "ok"), ("ip:localhost", "unchecked"),
                      ("cmd:ls -l", "unchecked"), ("file:/nonexistent/qorax", "missing"),
                      (dead, "missing")):
        verdict, detail = ask(ref)
        assert verdict == want, f"{ref}: got {verdict} / {detail}"
        assert detail.strip(), f"{ref}: no detail for {want}"

    # machine scope by address: a path that lives on some other box is not ours
    # to declare gone — this is what stopped n1 and n4 contradicting dgx
    # memories over /home/astro/msc and /etc/X11/xorg.conf.
    verdict, detail = ask(dead, origin=(("ip", THERE),))
    assert verdict == "unchecked", f"foreign origin judged anyway: {verdict} / {detail}"
    assert f"origin is {THERE} (ip)" in detail

    # an address outranks a name and a name may not veto an address: the weaker
    # identity is a fallback, not a second opinion
    verdict, _ = ask(dead, origin=(("ip", THERE), ("name", "dgx")))
    assert verdict == "unchecked", "a stale name overrode the address"
    verdict, _ = ask(dead, origin=(("ip", HERE), ("name", "somewhere")))
    assert verdict == "missing", "this box holds the address and was not allowed to judge"

    # legacy: a memory that recorded only a name is still handled
    verdict, detail = ask(dead, host_me="n1", origin=(("name", "dgx"),))
    assert verdict == "unchecked"
    assert "origin is dgx (name)" in detail

    # a memory with no box on record names no filesystem: no box may judge its
    # paths, least of all the one that happens to be asking. Left open, six
    # dgx-born memories read CONTRADICTED on n1 and n4.
    verdict, detail = ask(dead, origin=())
    assert verdict == "unchecked", f"origin-less memory judged: {verdict} / {detail}"
    assert "no origin on record" in detail

    # and a box that cannot name any address of its own judges nothing
    verdict, _ = ask(dead, origin=(("ip", HERE),), my_ips="")
    assert verdict == "unchecked", "a box with no address confirmed an origin"

    # OS shape: a path that cannot exist on this OS is not evidence either.
    # macOS has /etc (a symlink into /private/etc) but no systemd; Linux has no
    # /Applications. mmm6 called three dgx memories CONTRADICTED over exactly
    # this on 2026-11-13.
    elsewhere = ("/etc/systemd/journald.conf" if sys.platform == "darwin"
                 else "/Applications/Qoder.app/Contents")
    verdict, detail = ask(f"file:{elsewhere}")
    assert verdict == "unchecked", f"{elsewhere} judged on the wrong OS: {verdict} / {detail}"
    assert "not a" in detail
    if sys.platform != "darwin":
        # the guard must not eat real Linux system paths either
        verdict, detail = ask("file:/etc/systemd/system")
        assert verdict in ("ok", "missing"), f"own-OS path blocked by the guard: {detail}"


def test_a_probe_without_an_identity_asserts_nothing():
    """No ``HOST_ME`` must never read as confirmation.

    probe_one grew a dependency on its own helpers with the machine-scope rule;
    a slice that lost them used to answer ``ok\tthis is that host`` for every
    host, because an unset variable equals an unset variable. A probe that
    cannot say where it is has no authority over any ref at all."""
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "cli" / "msc-verify.sh"
    source = script.read_text(encoding="utf-8")
    body = source[source.index("norm_host() {"):
                  source.index("\n}\n", source.index("probe_one() {")) + 3]

    for ref in ("host:dgx", "host:n1", "host:mmm6"):
        run = subprocess.run(["sh", "-c", f'HOST_NOW=dgx\n{body}\nHOST_ME=""\nprobe_one "{ref}"'],
                             capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr
        verdict, separator, _ = run.stdout.partition("\t")
        assert separator == "\t", f"{ref} answered without a separator: {run.stdout!r}"
        assert verdict == "unchecked", f"{ref} confirmed by a probe with no identity: {run.stdout!r}"

    # and the origin list keeps its half of the bargain: no identity means the
    # path is not judged either
    run = subprocess.run(
        ["sh", "-c", f'HOST_NOW=dgx\n{body}\nHOST_ME=""\nMY_IPS=""\nset_scope "name\tdgx"\n'
                     f'probe_one "file:/etc/hosts"'],
        capture_output=True, text=True, timeout=30)
    assert run.stdout.startswith("unchecked\t"), run.stdout

    # an address ref needs an address to compare against, not a hostname: with
    # neither, even the box's own address must not read as confirmation
    run = subprocess.run(
        ["sh", "-c", f'HOST_NOW=dgx\n{body}\nHOST_ME=""\nMY_IPS=""\n'
                     f'probe_one "ip:192.0.2.1"'],
        capture_output=True, text=True, timeout=30)
    assert run.stdout.startswith("unchecked\t"), run.stdout


def test_silence_from_an_endpoint_is_not_a_contradiction():
    """Only an answer can contradict a memory.

    curl reports ``000`` for a timeout, a refused port and an unresolved name
    alike, and the probe mapped all three onto 「missing」. A sleeping Mac is
    therefore enough to contradict every memory that points at one of its
    ports — and with ``--yes`` to invalidate them. Measured 2026-11-13 from dgx,
    where github.com answers HEAD 200 for a repo that exists and times out on
    the very next GET: silence describes the route, not the endpoint. The
    server-side twin of this rule is ``_probe_ref``, which already separates
    ``TimeoutException`` (unchecked) from ``ConnectError``."""
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "cli" / "msc-verify.sh"
    source = script.read_text(encoding="utf-8")
    body = source[source.index("norm_host() {"):
                  source.index("\n}\n", source.index("probe_one() {")) + 3]

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200 if self.path == "/alive" else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def ask(ref):
        run = subprocess.run(["sh", "-c", f'HOST_NOW=dgx\n{body}\nprobe_one "{ref}"'],
                             capture_output=True, text=True, timeout=60)
        assert run.returncode == 0, run.stderr
        verdict, separator, detail = run.stdout.partition("\t")
        assert separator == "\t", f"{ref} answered without a separator: {run.stdout!r}"
        return verdict, detail

    try:
        assert ask(f"url:http://127.0.0.1:{port}/alive")[0] == "ok"
        # an endpoint that answers 「not here」 is the one thing that does
        # contradict: it is an answer, not an absence of one
        verdict, detail = ask(f"url:http://127.0.0.1:{port}/gone")
        assert verdict == "missing", f"a 404 was not read as gone: {verdict} / {detail}"
        # refused, unresolvable, and an unreachable library: all silence, none
        # of it evidence
        for ref in ("url:http://127.0.0.1:1/", "url:http://qorax.invalid/",
                    "mem:whatever-the-library-holds"):
            verdict, detail = ask(ref)
            assert verdict == "unchecked", f"{ref} contradicted by silence: {verdict} / {detail}"
    finally:
        srv.shutdown()


def test_a_claim_is_anchored_by_address_and_only_falls_back_to_a_name():
    """origin_of emits ``kind<TAB>value`` for ONE tier: address before name,
    refs before hostline, and never a union of the two.

    Address before name because a hostname is a label that gets renamed — n1
    answered to note02 until 2026-11-12 — and nothing on this LAN resolves
    names, so every box here is reached by number anyway.

    Refs before hostline because they answer different questions: a ref is a
    deliberate statement about what a memory concerns, while the hostline only
    records who wrote it. An agent that ssh'd from mmm6 into dgx writes
    ``host: mmm6`` over paths that only ever lived on dgx; unioned, those
    memories stayed judgeable on mmm6, which called
    /etc/security/limits.d/60-memlock.conf 「gone」 and reported three dgx
    memories CONTRADICTED. Measured 2026-11-13."""
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "cli" / "msc-verify.sh"
    source = script.read_text(encoding="utf-8")
    fn = source[source.index("origin_of() {"):
                source.index("\n}\n", source.index("origin_of() {")) + 3]

    def origin(doc):
        run = subprocess.run(["sh", "-c", f"{fn}\norigin_of"], input=doc,
                             capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr
        return [tuple(line.split("\t")) for line in run.stdout.split("\n") if line.strip()]

    # ip: ref — the form a writer uses to say which box a claim belongs to
    assert origin('{"refs":["ip:192.168.29.50","file:/etc/x"],"body":"# none"}') \
        == [("ip", "192.168.29.50")]
    # a host: ref holding an address is an address, not a name
    assert origin('{"refs":["host:192.168.29.11","file:/x"],"body":"host: dgx\\nx"}') \
        == [("ip", "192.168.29.11")]
    # an ip: ref outranks a hostline that names a different box
    assert origin('{"refs":["ip:192.168.29.50"],"body":"host: mmm6 ip: 192.168.29.100\\nx"}') \
        == [("ip", "192.168.29.50")]
    # the mis-anchored shape from the incident: writer mmm6, subject dgx
    assert origin('{"refs":["host:mmm6","ip:192.168.29.50","file:/etc/security'
                  '/limits.d/60-memlock.conf"],"body":"host: mmm6 ip: 192.168.29.100\\nx"}') \
        == [("ip", "192.168.29.50")]
    # no ref with an address: the hostline ip: stands in — 记忆命名原则 puts it
    # there, which is why it survives a rename
    assert origin('{"refs":["host:mmm6","file:/etc/x"],"body":"host: mmm6 ip: 192.168.29.100\\nx"}') \
        == [("ip", "192.168.29.100")]
    # nothing has an address at all — only then do names decide, refs first
    assert origin('{"refs":["host:mmm6","file:/x"],"body":"host: dgx\\nx"}') == [("name", "mmm6")]
    # the same normalisation the live hostname gets: mDNS suffix and case
    assert origin('{"refs":["host:DGX.local","host:n1"],"body":"# no hostline"}') \
        == [("name", "dgx"), ("name", "n1")]
    # nothing on record is not the same as "this box"
    assert origin('{"refs":["file:/etc/x"],"body":"# no hostline here"}') == []

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
