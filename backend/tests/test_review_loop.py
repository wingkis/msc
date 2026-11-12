"""Ageing and the audit trail: decay makes a memory visibly unconfirmed, and
every verdict leaves a trace.

The provenance layer could already contradict a memory, but nothing noticed
silence: ``verified_at`` was written and never read, so a belief stayed
``active`` forever purely because nobody bothered. These tests hold the four
properties that make automatic decay safe to run unattended:

* a clock may downgrade, never contradict — that needs a reason;
* decay is idempotent and never launders its own age (an edit or a decay stamp
  does not count as a re-check);
* recovery stays an act of re-checking;
* a verdict written against a revision that already moved fails loudly.
"""

import datetime as dt

import pytest

_CREATED: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _clean_up(client):
    yield
    for slug in _CREATED:
        client.delete(f"/api/v1/memories/{slug}")
    _CREATED.clear()


def _create(client, name, **overrides):
    data = {"name": name, "body": f"{name} body about reviewloop", "category": "project",
            "priority": "medium", "tags": ["reviewloop"], "refs": ["host:testhost"]}
    data.update(overrides)
    created = client.post("/api/v1/memories", json=data)
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    _CREATED.append(slug)
    return slug


def _age(client, slug, days, verified_days_ago=None):
    """Backdate a memory the way time would, without waiting for it."""
    from msc.database import SessionLocal
    from sqlalchemy import text

    now = dt.datetime.now(dt.timezone.utc)
    db = SessionLocal()
    try:
        db.execute(
            text("UPDATE memories SET created_at = :born, updated_at = :born, "
                 "verified_at = :checked WHERE slug = :s"),
            {"born": (now - dt.timedelta(days=days)).isoformat(),
             "checked": (now - dt.timedelta(days=verified_days_ago)).isoformat()
             if verified_days_ago is not None else None,
             "s": slug},
        )
        db.commit()
    finally:
        db.close()


def _status(client, slug):
    row = client.get(f"/api/v1/memories/{slug}", params={"include_invalidated": "true"}).json()
    return row["status"]


# ── decay ─────────────────────────────────────────────────────────────

def test_decay_reports_before_it_writes_and_is_idempotent(client):
    slug = _create(client, "Decay Dry Run")
    _age(client, slug, days=400)

    preview = client.post("/api/v1/memories/decay", params={"stale_after_days": 90})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["dry_run"] is True and slug in body["slugs"]
    assert _status(client, slug) == "active", "a dry run that stamps rows is not a dry run"

    applied = client.post("/api/v1/memories/decay",
                          params={"stale_after_days": 90, "apply": "true"}).json()
    assert applied["dry_run"] is False and slug in applied["slugs"]
    assert _status(client, slug) == "stale"

    # Second sweep: the row is no longer active, so there is nothing to re-stamp
    # and no second history row. Without this the hourly loop would rewrite the
    # same memories forever.
    again = client.post("/api/v1/memories/decay",
                        params={"stale_after_days": 90, "apply": "true"}).json()
    assert slug not in again["slugs"]
    decay_rows = [r for r in client.get(f"/api/v1/memories/{slug}/reviews").json()
                  if r["action"] == "decay"]
    assert len(decay_rows) == 1


def test_decay_never_contradicts_and_never_restores(client):
    """A clock has no evidence. Only a re-check may break or fix a belief."""
    contradicted = _create(client, "Decay Leaves Contradicted")
    client.post(f"/api/v1/memories/{contradicted}/verify",
                json={"verdict": "contradicted", "reason": "checked, wrong"})
    _age(client, contradicted, days=400)

    slugs = client.post("/api/v1/memories/decay",
                        params={"stale_after_days": 90, "apply": "true"}).json()["slugs"]
    assert contradicted not in slugs
    assert _status(client, contradicted) == "contradicted"


def test_decay_off_returns_422_instead_of_a_silent_zero(client):
    """stale_after_days=0 means "disabled", and a POST that quietly did nothing
    would look like a sweep that found the library healthy."""
    response = client.post("/api/v1/memories/decay", params={"stale_after_days": 0})
    assert response.status_code == 422
    assert "MSC_STALE_AFTER_DAYS" in response.json()["detail"]


def test_a_decay_stamp_does_not_reset_the_clock_it_measures(client):
    """The trap this exists to avoid: decay moves updated_at, so if age were
    measured from "last touched", every sweep would buy another full interval
    of unexamined confidence."""
    slug = _create(client, "Decay Keeps Age")
    _age(client, slug, days=400)
    client.post("/api/v1/memories/decay", params={"stale_after_days": 90, "apply": "true"})

    due = client.get("/api/v1/memories/review-due").json()["items"]
    item = [i for i in due if i["slug"] == slug][0]
    assert item["age_days"] >= 399, item
    assert item["last_confirmed"] == "created"
    assert any("stale" in reason for reason in item["reasons"])


def test_only_a_re_check_clears_the_age(client):
    """An edit is not a check — the same rule that keeps PUT from restoring a
    contradicted memory, applied to the decay timer."""
    slug = _create(client, "Edit Is Not A Check")
    _age(client, slug, days=400)
    client.put(f"/api/v1/memories/{slug}", json={"body": "rewritten, still unchecked"})

    slugs = client.post("/api/v1/memories/decay",
                        params={"stale_after_days": 90, "apply": "true"}).json()["slugs"]
    assert slug in slugs, "rewriting prose laundered the age"

    assert client.post(f"/api/v1/memories/{slug}/verify",
                       json={"verdict": "ok", "reason": "re-read the source"}).status_code == 200
    due = client.get("/api/v1/memories/review-due").json()["items"]
    assert slug not in [i["slug"] for i in due]


# ── the queue ─────────────────────────────────────────────────────────

def test_review_queue_names_the_missing_kind_of_check(client):
    aged = _create(client, "Queue Aged", priority="critical")
    _age(client, aged, days=400)
    refless = _create(client, "Queue No Refs", refs=[])

    queue = client.get("/api/v1/memories/review-due", params={"tag": "reviewloop"}).json()
    by_slug = {i["slug"]: i for i in queue["items"]}
    assert by_slug[aged]["reasons"] and by_slug[refless]["reasons"]
    assert any("出处" in reason for reason in by_slug[refless]["reasons"])
    # priority first: a critical belief nobody has looked at outranks a note
    assert queue["items"][0]["slug"] == aged
    # a fresh memory that carries refs is not "due" merely because it exists
    fresh = _create(client, "Queue Not Due")
    assert fresh not in by_slug


def test_review_queue_is_read_only(client):
    slug = _create(client, "Queue Read Only")
    _age(client, slug, days=400)
    before = _status(client, slug)
    client.get("/api/v1/memories/review-due")
    assert _status(client, slug) == before


def test_stale_memories_are_still_listed_and_searchable(client):
    """stale is suspicion, not evidence. Hiding them would turn a clock into a
    censor, which is exactly the failure contradicted already guards against."""
    slug = _create(client, "Stale Still Listed")
    client.post(f"/api/v1/memories/{slug}/verify",
                json={"verdict": "stale", "reason": "probably out of date"})
    listed = client.get("/api/v1/memories", params={"tag": "reviewloop", "limit": 500}).json()
    assert slug in [m["slug"] for m in listed]
    assert client.get("/api/v1/search", params={"q": "reviewloop"}).json()


# ── optimistic locking + history ──────────────────────────────────────

def test_a_verdict_written_against_a_moved_memory_gets_409(client):
    slug = _create(client, "Optimistic Lock")
    stale_read = client.get(f"/api/v1/memories/{slug}").json()

    assert client.post(f"/api/v1/memories/{slug}/verify",
                       json={"verdict": "contradicted", "reason": "agent A",
                             "if_status": "active"}).status_code == 200
    # agent B read the row before A wrote, and must not overwrite silently
    lost = client.post(f"/api/v1/memories/{slug}/invalidate",
                       json={"reason": "agent B", "if_status": "active"})
    assert lost.status_code == 409
    assert "moved" in lost.json()["detail"]

    also_lost = client.post(f"/api/v1/memories/{slug}/verify",
                            json={"verdict": "ok", "reason": "agent B",
                                  "if_updated_at": stale_read["updated_at"]})
    assert also_lost.status_code == 409

    fresh = client.get(f"/api/v1/memories/{slug}").json()
    assert client.post(f"/api/v1/memories/{slug}/verify",
                       json={"verdict": "ok", "reason": "agent B re-read",
                             "if_status": fresh["status"],
                             "if_updated_at": fresh["updated_at"]}).status_code == 200


def test_precondition_vocabulary_is_validated(client):
    slug = _create(client, "Bad Precondition")
    response = client.post(f"/api/v1/memories/{slug}/verify",
                           json={"verdict": "ok", "if_status": "maybe"})
    assert response.status_code == 422


def test_the_review_history_keeps_every_verdict_including_the_restore(client):
    """The status column says what is believed now; only the history can say the
    same claim was contradicted and then restored."""
    slug = _create(client, "Review History")
    client.post(f"/api/v1/memories/{slug}/invalidate",
                json={"reason": "endpoint moved", "superseded_by": None},
                headers={"X-Agent": "agent-a"})
    client.post(f"/api/v1/memories/{slug}/verify", json={"verdict": "ok", "reason": "moved back"},
                headers={"X-Agent": "agent-b"})

    history = client.get(f"/api/v1/memories/{slug}/reviews").json()
    actions = [row["action"] for row in history]
    assert actions == ["verify", "invalidate"], history       # newest first
    assert history[0]["from_status"] == "contradicted" and history[0]["to_status"] == "active"
    assert history[1]["reason"] == "endpoint moved"
    assert {row["actor"] for row in history} == {"agent-a", "agent-b"}


def test_meta_reports_how_much_of_the_library_is_unconfirmed(client):
    body = client.get("/api/v1/meta").json()
    for key in ("stale", "never_verified", "no_refs", "decay"):
        assert key in body, key
    assert set(body["decay"]) == {"stale_after_days", "enabled"}
    assert body["decay"]["enabled"] is (body["decay"]["stale_after_days"] > 0)
    assert body["never_verified"] <= body["total"]


# ── the small accounts (#10) ──────────────────────────────────────────

def test_list_counts_match_the_rows_after_pagination(client):
    slugs = [_create(client, f"Count Check {i}", tags=["reviewloop", "countcheck"])
             for i in range(3)]
    page = client.get("/api/v1/memories", params={"tag": "countcheck", "limit": 2})
    first = page.json()
    total = int(page.headers["X-Total-Count"])
    assert total >= 3
    assert len(first) == 2
    rest = client.get("/api/v1/memories", params={"tag": "countcheck", "limit": 2,
                                                  "offset": 2}).json()
    seen = {m["slug"] for m in first} | {m["slug"] for m in rest}
    assert set(slugs) <= seen, "SQL-side pagination dropped rows the old slice returned"


def test_a_memory_with_two_matching_tags_is_listed_once(client):
    slug = _create(client, "Double Tagged", tags=["reviewloop", "doubletagged"])
    listed = client.get("/api/v1/memories", params={"tag": "reviewloop,doubletagged"}).json()
    assert [m["slug"] for m in listed].count(slug) == 1
    assert int(client.get("/api/v1/memories",
                          params={"tag": "reviewloop,doubletagged"}).headers["X-Total-Count"]) \
        == len(listed)


def test_preflight_only_reads_memories_that_can_have_refs(client):
    """The old filter ran in Python after the ORM had built every object, so an
    unbounded sweep read the whole library to report on its ref-bearing part."""
    from msc.database import SessionLocal
    from msc.models import Memory

    refless = _create(client, "Preflight Refless", refs=[])
    report = client.get("/api/v1/memories/preflight", params={"tag": "reviewloop", "limit": 100})
    assert refless not in [i["slug"] for i in report.json()["items"]]

    # the SQL predicate is the one doing the work, not the leftover Python check
    db = SessionLocal()
    try:
        rows = db.query(Memory).filter(
            Memory.archived == 0, Memory.tags.any(name="reviewloop"),
            Memory.refs.isnot(None), Memory.refs != "", Memory.refs != "[]").all()
        assert refless not in [m.slug for m in rows]
    finally:
        db.close()


def test_timestamp_parsing_survives_the_formats_this_database_has():
    from msc.services.freshness import parse_ts

    assert parse_ts("2026-11-13T01:00:00Z").year == 2026
    assert parse_ts("2026-11-13T01:00:00+00:00").utcoffset().total_seconds() == 0
    assert parse_ts("2026-11-13 01:00:00") is not None
    assert parse_ts("not a date") is None and parse_ts(None) is None and parse_ts("") is None
