"""Photo pipeline tests: source cards, resumable scan, thumbnails, gallery.

Same fake-driven style as test_rag.py: real tiny JPEGs on disk for the local
source, a scripted adapter for walk/resume behaviour, and pic_worker.run_once()
called directly instead of waiting on the 15s tick.
"""

import os
from pathlib import Path

import pytest
from PIL import Image

PIC_ROOTS = Path(os.environ["MSC_PIC_ROOTS"])


@pytest.fixture(scope="module")
def photos_dir():
    root = PIC_ROOTS / "library"
    root.mkdir(parents=True, exist_ok=True)
    (root / "2024" / "beach").mkdir(parents=True, exist_ok=True)
    # Two real jpegs (one in a subdir), a screenshot-named one, a text file
    # and a corrupt .jpg — the filter/failed behaviours read exactly these.
    Image.new("RGB", (40, 30), "red").save(root / "alpha.jpg")
    Image.new("RGB", (80, 60), "blue").save(root / "2024" / "beach" / "gamma.jpg")
    Image.new("RGB", (50, 50), "green").save(root / "Screenshot_2024-05-01.png")
    (root / "notes.txt").write_text("not a photo")
    (root / "broken.jpg").write_bytes(b"\xff\xd8\xff\x00garbage-not-an-image")
    return root


# ── unit helpers ─────────────────────────────────────────────────────

def test_taken_at_from_image_exif_markers():
    from msc.services.pic_index import taken_at_from_image

    class Sub(dict):
        pass

    class Exif:
        def __init__(self, sub):
            self._sub = sub

        def get_ifd(self, key):
            return self._sub

    class Im:
        def __init__(self, sub):
            self._sub = sub

        def getexif(self):
            return Exif(self._sub)

    stamped = Im({36867: "2024:05:01 12:00:00", 36881: "+08:00"})
    assert taken_at_from_image(stamped, "2025-01-01T00:00:00+00:00") == \
        ("2024-05-01T12:00:00+08:00", "exif")

    naive = Im({36867: "2024:05:01 12:00:00"})
    assert taken_at_from_image(naive, "2025-01-01T00:00:00+00:00") == \
        ("2024-05-01T12:00:00", "exif_naive")

    blank = Im({})
    assert taken_at_from_image(blank, "2025-01-01T00:00:00+00:00") == \
        ("2025-01-01T00:00:00+00:00", "mtime")


def test_taken_at_prefix_bounds():
    from msc.api.pic import taken_at_prefix

    assert taken_at_prefix("2026-11") == "2026-11-01"
    assert taken_at_prefix("2026-11", end=True) == "2026-12-01T00:00:00"
    assert taken_at_prefix("2026-11-10", end=True) == "2026-11-10T23:59:59"


def test_jail_join():
    from msc.picsources.base import SourceError, jail_join

    root = PIC_ROOTS / "library"
    assert jail_join(root, "2024/beach/gamma.jpg").name == "gamma.jpg"
    for bad in ("../escape.jpg", "/absolute/path", "", "a/../../b"):
        with pytest.raises(SourceError):
            jail_join(root, bad)


# ── source cards API ─────────────────────────────────────────────────

def test_source_crud_and_jail(client, photos_dir):
    # Outside MSC_PIC_ROOTS: refused with the remedy in the message.
    bad = client.post("/api/v1/pic/sources", json={
        "name": "越界", "kind": "local", "root_path": "/etc"})
    assert bad.status_code == 422
    assert "MSC_PIC_ROOTS" in bad.json()["detail"]

    created = client.post("/api/v1/pic/sources", json={
        "name": "我的照片库", "kind": "local", "root_path": str(photos_dir),
        "username": "guest", "password": "s3cret"})
    assert created.status_code == 200, created.text
    view = created.json()
    assert view["password_set"] is True and "s3cret" not in created.text
    sid = view["id"]

    # Password policy mirrors the model cards: omitted keeps, "" clears.
    kept = client.put(f"/api/v1/pic/sources/{sid}", json={"name": "照片库"})
    assert kept.json()["password_set"] is True
    cleared = client.put(f"/api/v1/pic/sources/{sid}", json={"password": ""})
    assert cleared.json()["password_set"] is False

    # Editing the address resets the scan schedule; probing works against
    # the real directory.
    probe = client.post(f"/api/v1/pic/sources/{sid}/probe")
    assert probe.status_code == 200 and probe.json()["ok"] is True

    listed = client.get("/api/v1/pic/sources").json()
    assert [s["id"] for s in listed] == [sid]


def test_source_validation(client):
    bad_smb = client.post("/api/v1/pic/sources", json={"name": "x", "kind": "smb", "host": "n1"})
    assert bad_smb.status_code == 422
    bad_kind = client.post("/api/v1/pic/sources", json={"name": "x", "kind": "ftp"})
    assert bad_kind.status_code == 422


def test_browse_jails_to_roots(client, photos_dir):
    ok = client.get("/api/v1/pic/browse", params={"path": str(photos_dir)})
    assert ok.status_code == 200
    names = [d["name"] for d in ok.json()["dirs"]]
    assert "2024" in names and not any(n.startswith(".") for n in names)
    assert client.get("/api/v1/pic/browse", params={"path": "/etc"}).status_code == 403


def _source_id(client):
    rows = client.get("/api/v1/pic/sources").json()
    return rows[0]["id"]


# ── scan + thumb through the worker ──────────────────────────────────

def test_scan_and_thumb_progressive(client, db, photos_dir):
    from msc.workers import pic_worker

    sid = _source_id(client)
    from msc.models import PicItem
    db.query(PicItem).filter_by(source_id=sid).delete()
    db.commit()

    report = pic_worker.run_once()
    assert report and report["scanned"] >= 4  # txt filtered out, broken.jpg counted
    items = {i.rel_path: i for i in db.query(PicItem).filter(PicItem.source_id == sid).all()}
    assert "notes.txt" not in items and "alpha.jpg" in items
    assert "broken.jpg" in items  # discovered first, decided at thumb time

    # One pass scans and thumbs: placeholders became tiles or failed.
    assert (items["alpha.jpg"].state, items["alpha.jpg"].thumb_path) == (
        "thumb_ready", f"thumbs/{sid}/{items['alpha.jpg'].id}.webp")
    thumb_file = Path(os.environ["MSC_PIC_DIR"]) / items["alpha.jpg"].thumb_path
    assert thumb_file.is_file() and thumb_file.stat().st_size > 0
    assert items["alpha.jpg"].content_hash and items["alpha.jpg"].width == 40
    assert items["alpha.jpg"].taken_at_src == "mtime"  # generated JPEGs have no EXIF
    assert items["Screenshot_2024-05-01.png"].kind == "screenshot"
    assert items["broken.jpg"].state == "failed" and items["broken.jpg"].last_error

    # The tile endpoint serves the webp that just landed.
    resp = client.get(f"/api/v1/pic/item/{items['alpha.jpg'].id}/thumb")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/webp"
    missing = client.get(f"/api/v1/pic/item/{items['broken.jpg'].id}/thumb")
    assert missing.status_code == 404


def test_gallery_listing(client, photos_dir):
    data = client.get("/api/v1/pic/items").json()
    assert data["total"] >= 4
    states = {i["state"] for i in data["items"]}
    assert "thumb_ready" in states
    withq = client.get("/api/v1/pic/items", params={"q": "beach"}).json()
    assert [i["rel_path"] for i in withq["items"]] == ["2024/beach/gamma.jpg"]
    assert client.get("/api/v1/pic/items", params={"state": "bogus"}).status_code == 422


def test_transient_error_backs_off_not_failed(client, db, photos_dir, monkeypatch, tmp_path):
    """A vanished file is the wire's problem, not the photo's: discovered
    stays, retry_at moves into the future, error_count ticks up."""
    from msc.models import PicItem
    from msc.workers import pic_worker

    sid = _source_id(client)
    # Fresh item pointing at a file that cannot be read: /scan re-arms the
    # walk (rescan_hours would otherwise park this source).
    gone = photos_dir / "vanished.jpg"
    Image.new("RGB", (30, 20), "teal").save(gone, "JPEG")  # a real photo, then pulled away
    assert client.post(f"/api/v1/pic/sources/{sid}/scan").status_code == 200
    pic_worker.run_once()
    gone.unlink()
    item = db.query(PicItem).filter_by(source_id=sid, rel_path="vanished.jpg").first()
    assert item is not None
    if item.state == "thumb_ready":  # raced the earlier pass; reset to replay
        item.state, item.thumb_path = "discovered", ""
        db.commit()

    class Flaky:
        kind = "local"

        def probe(self):
            return {"reachable": True, "root_ok": True, "error": None}

        def iter_entries(self, since=None):
            return iter(())

        def get_bytes(self, rel, max_bytes=None):
            raise OSError("SMB pipe broke")

        def close(self):
            pass

    monkeypatch.setattr(pic_worker, "build_source", lambda row: Flaky())
    report = pic_worker.run_once()
    db.expire_all()
    item = db.query(PicItem).filter_by(source_id=sid, rel_path="vanished.jpg").first()
    assert item.state == "discovered" and item.error_count >= 1
    assert item.retry_at and "SMB pipe broke" in item.last_error
    # retry_at is in the future: this tick did not retry it to death.
    assert report is None or report["thumbs"] == 0


def test_scan_resume_no_duplicates(client, db, monkeypatch):
    """A walk that dies mid-way resumes from the cursor; re-yielded entries
    hit UNIQUE and insert nothing twice."""
    from msc.models import PicItem, PicSource
    from msc.picsources.base import PicEntry, SourceError
    from msc.workers import pic_worker

    sid = _source_id(client)
    from msc.models import PicVector
    db.query(PicVector).delete()
    db.query(PicItem).filter_by(source_id=sid).delete()
    src = db.get(PicSource, sid)
    src.scan_cursor, src.last_scan_at, src.state = None, None, "idle"
    db.commit()

    walked = {"count": 0}

    class Walk:
        kind = "local"

        def probe(self):
            return {"reachable": True, "root_ok": True, "error": None}

        def iter_entries(self, since=None):
            for rel in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"):
                if since is not None and rel <= since:
                    continue
                yield PicEntry(rel_path=rel, name=rel, size=10,
                               mtime_iso="2024-01-01T00:00:00+00:00")
                walked["count"] += 1
                if walked["count"] == 2:
                    raise SourceError("share disconnected")  # die mid-walk

        def get_bytes(self, rel, max_bytes=None):
            raise SourceError("down")

        def close(self):
            pass

    monkeypatch.setattr(pic_worker, "build_source", lambda row: Walk())
    report = pic_worker.run_once()
    assert report and any("share disconnected" in e for e in report["errors"])
    db.expire_all()
    src = db.get(PicSource, sid)
    assert src.state == "error" and src.scan_cursor == "b.jpg"
    assert db.query(PicItem).filter_by(source_id=sid).count() == 2

    # Phase 2: a healthy adapter resuming from the cursor (and re-yielding
    # nothing already inserted).
    class Healthy:
        kind = "local"

        def probe(self):
            return {"reachable": True, "root_ok": True, "error": None}

        def iter_entries(self, since=None):
            for rel in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"):
                if since is not None and rel <= since:
                    continue
                yield PicEntry(rel_path=rel, name=rel, size=10, mtime_iso="2024-01-01T00:00:00+00:00")

        def get_bytes(self, rel, max_bytes=None):
            raise OSError("thumb stage irrelevant here")

        def close(self):
            pass

    monkeypatch.setattr(pic_worker, "build_source", lambda row: Healthy())
    pic_worker.run_once()
    db.expire_all()
    src = db.get(PicSource, sid)
    rows = db.query(PicItem).filter_by(source_id=sid).all()
    assert len(rows) == 4  # c.jpg and d.jpg added, a/b not duplicated
    assert src.state == "idle" and src.scan_cursor is None and src.last_scan_at


def test_disable_and_pause_resume(client, db):
    from msc.models import PicSource

    sid = _source_id(client)
    r = client.post(f"/api/v1/pic/sources/{sid}/pause")
    assert r.status_code == 200 and r.json()["state"] == "paused"
    src = db.get(PicSource, sid)
    db.expire_all()
    src = db.get(PicSource, sid)
    assert src.state == "paused"
    r = client.post(f"/api/v1/pic/sources/{sid}/resume")
    assert r.json()["state"] == "idle"


def test_delete_source_keeps_originals(client, db, photos_dir):
    from msc.models import PicItem

    sid = _source_id(client)
    originals = {p.name for p in photos_dir.iterdir() if p.suffix in (".jpg", ".png")}
    r = client.delete(f"/api/v1/pic/sources/{sid}")
    assert r.status_code == 200
    assert db.query(PicItem).filter_by(source_id=sid).count() == 0
    after = {p.name for p in photos_dir.iterdir() if p.suffix in (".jpg", ".png")}
    assert originals == after  # deleting the card never deletes the photos


# ── kill switch & read-only ──────────────────────────────────────────

def test_pic_kill_switch(client, monkeypatch):
    from msc.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "pic_enabled", False)
    assert client.get("/api/v1/pic/status").status_code == 503
    assert client.get("/api/v1/pic/items").status_code == 503
    assert client.post("/api/v1/pic/sources", json={"name": "x", "kind": "local",
                                                    "root_path": str(PIC_ROOTS)}).status_code == 503

    from msc.workers import pic_worker
    assert pic_worker.run_once() is None  # worker stays parked while off


def test_pic_read_only(client):
    from msc.config import get_settings

    settings = get_settings()
    old = settings.read_only
    settings.read_only = True
    try:
        assert client.post("/api/v1/pic/sources", json={"name": "x", "kind": "local",
                                                        "root_path": str(PIC_ROOTS)}).status_code == 423
        assert client.put("/api/v1/pic/settings", json={"thumb_batch": 4}).status_code == 423
    finally:
        settings.read_only = old


def test_settings_round_trip(client):
    from msc.services.pic_index import PIC_TUNABLES

    before = client.get("/api/v1/pic/status").json()["settings"]
    assert set(before) == set(PIC_TUNABLES)
    after = client.put("/api/v1/pic/settings", json={"thumb_batch": 8}).json()["settings"]
    assert after["thumb_batch"] == 8
    assert client.put("/api/v1/pic/settings", json={"thumb_batch": 0}).status_code == 422


# ── stage 2: vision card, caption, embed, semantic search ────────────

_DIM = 64
_PIC_AXIS = {"红": 0, "海边": 1, "截图": 2}


def _pic_fake_embed(cfg, texts):
    """Directional axes like test_rag: a caption naming 红 lands on axis 0 etc."""
    import hashlib
    import random
    out = []
    for text in texts:
        for token, axis in _PIC_AXIS.items():
            if token in text:
                vec = [0.0] * _DIM
                vec[axis] = 1.0
                out.append(vec)
                break
        else:
            rng = random.Random(hashlib.md5(text.encode()).hexdigest())
            out.append([rng.uniform(-1, 1) for _ in range(_DIM)])
    return out


def _fake_chat_result(text):
    return {"text": text, "model": "fake-vl", "finish_reason": "stop",
            "usage": {}, "latency_ms": 3, "reasoning_only": False}


def _caption_chat(state):
    """Caption depends on the filename hint in the message: deterministic
    per-photo captions make the search assertions exact. state counts calls."""
    def fake(cfg, messages, max_tokens=None):
        state["calls"] = state.get("calls", 0) + 1
        blob = str(messages)
        if "gamma" in blob:
            return _fake_chat_result("一辆红色的车停在路边\n标签: 红色, 街道")
        if "Screenshot" in blob:
            return _fake_chat_result("一张软件界面的截图")
        return _fake_chat_result("海边日落时分的照片")
    return fake


def test_vision_probe_sends_an_image(client, monkeypatch):
    """The vision probe must fail a text-only server: it posts real image
    bytes and asserts the transport used an image content part."""
    from msc.services import llm

    seen = {}

    def fake_chat(cfg, messages, max_tokens=None):
        has_image = any(isinstance(m.get("content"), list) and
                        any(p.get("type") == "image_url" for p in m["content"])
                        for m in messages)
        seen["has_image"] = has_image
        return _fake_chat_result("红色")

    monkeypatch.setattr(llm, "chat", fake_chat)
    created = client.put("/api/v1/model?purpose=vision",
                         json={"base_url": "http://vision.test:8080", "model": "fake-vl"})
    assert created.status_code == 200
    report = client.post("/api/v1/model/test?purpose=vision").json()
    assert report["ok"] is True and seen["has_image"] is True


def test_caption_embed_and_search_flow(client, db, photos_dir, monkeypatch):
    from msc.models import PicItem, PicVector
    from msc.services import llm, rag_providers
    from msc.workers import pic_worker

    # Put the pic items of this fixture dir through the semantic stages.
    sid = client.post("/api/v1/pic/sources", json={
        "name": "语义源", "kind": "local", "root_path": str(photos_dir)}).json()["id"]
    assert client.put("/api/v1/model?purpose=embed",
                      json={"base_url": "http://embed.test", "model": "picembed-v1"}
                      ).status_code == 200

    state = {}
    monkeypatch.setattr(llm, "chat", _caption_chat(state))
    monkeypatch.setattr(rag_providers, "embed", _pic_fake_embed)

    for _ in range(15):
        pic_worker.run_once()
        pending = (db.query(PicItem).filter_by(source_id=sid)
                   .filter(PicItem.state.in_(["discovered", "thumb_ready"])).count())
        if not pending:
            break

    by_rel = {i.rel_path: i for i in db.query(PicItem).filter_by(source_id=sid).all()}
    alpha, gamma = by_rel["alpha.jpg"], by_rel["2024/beach/gamma.jpg"]
    assert alpha.state == "embedded" and "海边" in alpha.caption
    assert gamma.tags == "红色, 街道"  # the 标签: line parsed out
    # caption_batch=1 default: one vision call per photo, not one mega-request
    assert state.get("calls", 0) >= 3

    vecs = db.query(PicVector).filter(PicVector.model == "picembed-v1").all()
    assert len(vecs) >= 2 and all(len(v.vec) == v.dim * 4 for v in vecs)

    # Semantic photo search: axis vectors decide the winner exactly.
    hits = client.get("/api/v1/pic/search", params={"q": "红色的车"}).json()
    top_ids = [r["id"] for r in hits["results"]]
    assert top_ids and top_ids[0] == gamma.id
    assert hits["results"][0]["match"] in ("semantic", "both")

    # Isolation: memory search must never see photo ids.
    created = client.post("/api/v1/memories", json={
        "name": "红色记忆", "body": "一辆红色的车", "category": "knowledge",
        "priority": "medium"}).json()
    from msc.workers import rag_worker
    rag_worker.run_once()
    mem_hits = client.get("/api/v1/rag/search", params={"q": "红色的车"}).json()
    assert mem_hits["results"], "memory leg should find the new memory"
    # Isolation is structural — photo ids live in pic_vectors, never in the
    # embeddings table the memory scan reads. The observable proxy: every hit
    # is a real memory row, no photo file ever surfaces as a memory.
    assert created["slug"] in [r["slug"] for r in mem_hits["results"]]
    assert not any(r["slug"].endswith((".jpg", ".png", ".webp"))
                   for r in mem_hits["results"])
    # Ids are per-table, so the check that matters is on the embedding side:
    # every row of the memories' embeddings table belongs to a real memory.
    from msc.models import Embedding, Memory
    embedding_memory_ids = {e.memory_id for e in db.query(Embedding).all()}
    live_memory_ids = {m.id for m in db.query(Memory).all()}
    assert embedding_memory_ids <= live_memory_ids


def test_caption_outage_backs_off(client, db, photos_dir, monkeypatch):
    from msc.models import PicItem
    from msc.services import llm
    from msc.services.llm import LLMError
    from msc.workers import pic_worker

    item = (db.query(PicItem).filter(PicItem.state == "thumb_ready")
            .order_by(PicItem.id.desc()).first())
    if item is None:  # everything already captioned: force one back
        item = db.query(PicItem).filter(PicItem.state == "embedded").first()
        item.state = "thumb_ready"
        db.commit()
    target_id = item.id

    def broken(cfg, messages, max_tokens=None):
        raise LLMError("vision endpoint down")

    monkeypatch.setattr(llm, "chat", broken)
    report = pic_worker.run_once()
    db.expire_all()
    item = db.get(PicItem, target_id)
    assert item.state == "thumb_ready" and item.retry_at
    assert report and any("vision endpoint down" in e for e in report["errors"])
    cards = {c["purpose"]: c for c in client.get("/api/v1/model/cards").json()}
    assert "vision endpoint down" in (cards["vision"]["last_error"] or "")


def test_caption_reuse_for_duplicate_content(client, db, photos_dir, monkeypatch):
    """A byte-identical copy of an already-captioned photo skips the vision
    call entirely — burst shots cost nothing after the first."""
    from msc.models import PicItem
    from msc.services import llm
    from msc.workers import pic_worker

    alpha = db.query(PicItem).filter_by(rel_path="alpha.jpg").first()
    assert alpha and alpha.caption
    twin = photos_dir / "alpha_copy.jpg"
    twin.write_bytes((photos_dir / "alpha.jpg").read_bytes())

    state = {}
    monkeypatch.setattr(llm, "chat", _caption_chat(state))
    before = state.get("calls", 0)
    sid = alpha.source_id
    assert client.post(f"/api/v1/pic/sources/{sid}/scan").status_code == 200
    pic_worker.run_once()
    db.expire_all()
    copy_item = db.query(PicItem).filter_by(source_id=sid, rel_path="alpha_copy.jpg").first()
    assert copy_item is not None
    assert copy_item.state in ("captioned", "embedded")
    assert copy_item.caption == alpha.caption
    assert state.get("calls", 0) == before  # no vision call spent on the twin
    twin.unlink()


def test_embed_card_switch_reembeds(client, db, monkeypatch):
    """Switching the embed model demotes stragglers (self-heal) and rewrites
    vectors under the new model key; the old key's rows survive until pruned."""
    from msc.models import PicVector
    from msc.services import rag_providers
    from msc.workers import pic_worker

    monkeypatch.setattr(rag_providers, "embed", _pic_fake_embed)
    assert client.put("/api/v1/model?purpose=embed",
                      json={"base_url": "http://embed.test", "model": "picembed-v2"}
                      ).status_code == 200
    for _ in range(6):
        pic_worker.run_once()
    db.expire_all()
    models = {r.model for r in db.query(PicVector).all()}
    assert {"picembed-v1", "picembed-v2"} <= models
    from msc.models import PicItem
    embedded = db.query(PicItem).filter(PicItem.state == "embedded").count()
    v2 = db.query(PicVector).filter(PicVector.model == "picembed-v2").count()
    assert v2 == embedded  # every live captioned photo now sits on the new model


def test_stage2_leaves_shared_cards_clean(client):
    """Cards are one DB row per purpose, shared with test_rag — undo the
    embed/vision wiring this file's flow tests needed."""
    for purpose in ("embed", "vision"):
        cleared = client.put(f"/api/v1/model?purpose={purpose}",
                             json={"base_url": "", "model": ""})
        assert cleared.status_code == 200 and cleared.json()["base_url"] == ""


# ── stage 3: originals, trash, bulk, purge, remote adapters ──────────

def test_original_streams_from_source(client, photos_dir):
    items = client.get("/api/v1/pic/items", params={"q": "alpha.jpg"}).json()["items"]
    alpha = next(i for i in items if i["rel_path"] == "alpha.jpg")
    r = client.get(f"/api/v1/pic/item/{alpha['id']}/original")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert "inline" in r.headers["content-disposition"]
    assert r.content == (photos_dir / "alpha.jpg").read_bytes()
    assert client.get("/api/v1/pic/item/999999/original").status_code == 404


def test_trash_restore_and_purge_touches_original(client, db, photos_dir):
    from msc.models import PicItem, PicVector
    from msc.workers import pic_worker

    sid = client.get("/api/v1/pic/sources").json()[0]["id"]
    victim = photos_dir / "purge_me.jpg"
    Image.new("RGB", (20, 20), "orange").save(victim, "JPEG")
    assert client.post(f"/api/v1/pic/sources/{sid}/scan").status_code == 200
    pic_worker.run_once()

    found = client.get("/api/v1/pic/items", params={"q": "purge_me"}).json()["items"]
    assert found, "purge_me.jpg should be discovered and thumbnailed"
    item_id = found[0]["id"]

    # Soft delete: out of the gallery, into the trash; the file stays.
    assert client.delete(f"/api/v1/pic/item/{item_id}").status_code == 200
    assert not client.get("/api/v1/pic/items", params={"q": "purge_me"}).json()["items"]
    trashed = client.get("/api/v1/pic/items", params={"trash": True}).json()["items"]
    assert item_id in [i["id"] for i in trashed]
    assert victim.is_file()

    # Bulk restore brings it back.
    restored = client.post("/api/v1/pic/items/batch",
                           json={"ids": [item_id], "action": "restore"}).json()
    assert restored["changed"] == 1
    assert client.get("/api/v1/pic/items", params={"q": "purge_me"}).json()["items"]

    # Purge refuses without the confirm gate, then really deletes the file.
    refused = client.post("/api/v1/pic/items/purge",
                          json={"ids": [item_id], "confirm": False})
    assert refused.status_code == 422
    assert victim.is_file()
    done = client.post("/api/v1/pic/items/purge",
                       json={"ids": [item_id], "confirm": True}).json()
    assert done["purged"] == 1 and not done["errors"]
    assert not victim.exists()
    db.expire_all()
    assert db.get(PicItem, item_id) is None
    assert db.query(PicVector).filter(PicVector.pic_id == item_id).count() == 0


def test_bulk_archive_retry_actions(client, db):
    from msc.models import PicItem

    items = client.get("/api/v1/pic/items").json()["items"]
    two = [i["id"] for i in items if i["state"] != "failed"][:2]
    r = client.post("/api/v1/pic/items/batch", json={"ids": two, "action": "archive"})
    assert r.json()["changed"] == 2
    db.expire_all()
    assert all(db.get(PicItem, i).archived == 1 for i in two)
    client.post("/api/v1/pic/items/batch", json={"ids": two, "action": "restore"})

    failed = db.query(PicItem).filter(PicItem.state == "failed").first()
    if failed is not None:
        r = client.post("/api/v1/pic/items/batch",
                        json={"ids": [failed.id], "action": "retry"}).json()
        assert r["changed"] == 1
        db.expire_all()
        assert db.get(PicItem, failed.id).state == "discovered"
        assert client.post("/api/v1/pic/items/batch",
                           json={"ids": [10 ** 9], "action": "bogus"}).status_code == 422


def test_webdav_propfind_parser():
    from msc.picsources.webdav import parse_propfind

    body = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:s="http://apache.org/dav/propset/fs/1">
 <d:response>
  <d:href>/dav/photos/</d:href>
  <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
 <d:response>
  <d:href>/dav/photos/2024/</d:href>
  <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
 <d:response>
  <d:href>/dav/photos/2024/beach.jpg</d:href>
  <d:propstat><d:prop>
    <d:resourcetype/><d:getcontentlength>12345</d:getcontentlength>
    <d:getlastmodified>Tue, 01 May 2024 10:00:00 GMT</d:getlastmodified>
   </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
 <d:response>
  <d:href>/dav/photos/private.png</d:href>
  <d:propstat><d:prop><d:resourcetype/></d:prop>
   <d:status>HTTP/1.1 403 Forbidden</d:status></d:propstat>
 </d:response>
</d:multistatus>"""
    dirs, files = parse_propfind(body, "/dav/photos")
    assert dirs == [("2024", 0, "")]
    assert files == [("2024/beach.jpg", 12345, "Tue, 01 May 2024 10:00:00 GMT")]


def test_remote_source_construction_and_probe(client):
    """Remote cards construct adapters lazily; probe must never raise —
    a missing smbprotocol wheel comes back as a readable error line."""
    created = client.post("/api/v1/pic/sources", json={
        "name": "nas", "kind": "smb", "host": "nas.invalid", "share": "photos"}).json()
    assert created["state"] == "idle"

    try:
        import smbclient  # noqa: F401
        has_lib = True
    except ImportError:
        has_lib = False
    probe = client.post(f"/api/v1/pic/sources/{created['id']}/probe").json()
    assert probe["ok"] is False and probe["error"]
    if not has_lib:
        assert "smbprotocol" in probe["error"]

    wd = client.post("/api/v1/pic/sources", json={
        "name": "云盘", "kind": "webdav", "url": "https://wd.invalid/dav/photos"}).json()
    assert wd["url"].startswith("https://")

    # One worker tick against two unreachable remote sources must degrade
    # into error lines on the cards, not an exception through the loop.
    from msc.workers import pic_worker
    report = pic_worker.run_once()
    assert report is None or "error" not in report

    # cleanup so nothing else sees these two dead cards
    client.delete(f"/api/v1/pic/sources/{created['id']}")
    client.delete(f"/api/v1/pic/sources/{wd['id']}")


# ── stage 4: maintenance reaps, duplicate badge ──────────────────────

def test_duplicate_badge_marks_identical_content(client, photos_dir):
    # include_missing: the copy's FILE was unlinked by the reuse test, so a
    # completed walk may have marked it missing — the dup grouping still counts it.
    items = client.get("/api/v1/pic/items",
                       params={"q": "alpha", "include_missing": True}).json()["items"]
    by_rel = {i["rel_path"]: i for i in items}
    assert "alpha.jpg" in by_rel and "alpha_copy.jpg" in by_rel  # created by the reuse test
    assert by_rel["alpha.jpg"]["dup"] is True
    assert by_rel["alpha_copy.jpg"]["dup"] is True
    single = client.get("/api/v1/pic/items", params={"q": "gamma"}).json()["items"]
    assert single[0]["dup"] is False


def test_maintenance_prunes_pic_vectors_and_sweeps_thumbs(client, db):
    from msc.config import get_settings
    from msc.migrations import prune_broken_pic_vectors, sweep_orphan_thumbs
    from msc.models import PicItem, PicVector

    alpha = db.query(PicItem).filter_by(rel_path="alpha.jpg").first()
    assert alpha is not None
    db.add(PicVector(pic_id=alpha.id, model="broken-v", dim=4,
                     vec=b"\x00\x00", embedded_at="2026-01-01T00:00:00"))  # len != dim*4
    db.add(PicVector(pic_id=987654, model="orphan-v", dim=2,
                     vec=b"\x00" * 8, embedded_at="2026-01-01T00:00:00"))  # no such item
    db.commit()
    assert prune_broken_pic_vectors() == 2
    db.expire_all()
    assert db.query(PicVector).filter(PicVector.model.in_(["broken-v", "orphan-v"])).count() == 0

    orphan_dir = Path(get_settings().pic_dir) / "thumbs" / "424242"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / "999999.webp"
    orphan.write_bytes(b"stale tile")
    live_thumb = None
    if alpha.thumb_path:
        live_thumb = Path(get_settings().pic_dir) / alpha.thumb_path
        assert live_thumb.is_file()
    swept = sweep_orphan_thumbs()
    assert swept >= 1 and not orphan.exists()
    if live_thumb:
        assert live_thumb.is_file()  # the live tile of a real item survives


# ── stage 5: missing GC, folder filter, ids_only, keep/forget ────────

def _live_sid(client, photos_dir):
    """A local source over the shared fixture dir — create one when earlier
    tests have cleaned the table away, so these tests never inherit order."""
    rows = client.get("/api/v1/pic/sources").json()
    if rows:
        return rows[0]["id"]
    return client.post("/api/v1/pic/sources", json={
        "name": "阶段5源", "kind": "local", "root_path": str(photos_dir)}).json()["id"]


def test_missing_gc_hides_recovers_and_forget_keeps_original(client, db, photos_dir):
    from msc.models import PicItem
    from msc.workers import pic_worker

    sid = _live_sid(client, photos_dir)
    ghost = photos_dir / "ghost.jpg"
    Image.new("RGB", (12, 12), "gray").save(ghost, "JPEG")
    client.post(f"/api/v1/pic/sources/{sid}/scan")
    pic_worker.run_once()
    db.expire_all()
    item = db.query(PicItem).filter_by(rel_path="ghost.jpg").first()
    assert item is not None and item.missing == 0 and item.last_seen_at

    # original deleted behind MSC's back → completed walk marks it missing
    ghost.unlink()
    client.post(f"/api/v1/pic/sources/{sid}/scan")
    pic_worker.run_once()
    db.expire_all()
    assert db.get(PicItem, item.id).missing == 1
    assert client.get("/api/v1/pic/items", params={"q": "ghost"}).json()["items"] == []
    miss_view = client.get("/api/v1/pic/items", params={"view": "missing"}).json()["items"]
    assert item.id in [i["id"] for i in miss_view]

    # the file reappears → the next walk revives the row in place
    Image.new("RGB", (12, 12), "gray").save(ghost, "JPEG")
    client.post(f"/api/v1/pic/sources/{sid}/scan")
    pic_worker.run_once()
    db.expire_all()
    revived = db.get(PicItem, item.id)
    assert revived is not None and revived.missing == 0

    # keep (unmissing) is a user-side visibility toggle
    client.post(f"/api/v1/pic/sources/{sid}/scan")
    pic_worker.run_once()
    db.expire_all()
    if db.get(PicItem, item.id).missing:
        client.post("/api/v1/pic/items/batch", json={"ids": [item.id], "action": "unmissing"})
        db.expire_all()
        assert db.get(PicItem, item.id).missing == 0
        client.post(f"/api/v1/pic/sources/{sid}/scan")
        pic_worker.run_once()
        db.expire_all()
        assert db.get(PicItem, item.id).missing == 1

    # forget deletes ONLY derived data — an untouched original stays on disk
    keeper = photos_dir / "keepme.jpg"
    Image.new("RGB", (10, 10), "green").save(keeper, "JPEG")
    client.post(f"/api/v1/pic/sources/{sid}/scan")
    pic_worker.run_once()
    db.expire_all()
    keep_item = db.query(PicItem).filter_by(rel_path="keepme.jpg").first()
    assert keep_item is not None
    keep_id = keep_item.id  # plain int up front: after the delete the ORM
    # instance is a trap — any attribute touch raises ObjectDeletedError.
    r = client.post("/api/v1/pic/items/batch",
                    json={"ids": [keep_id], "action": "forget"}).json()
    assert r["changed"] == 1
    db.expire_all()
    assert db.query(PicItem).filter(PicItem.id == keep_id).count() == 0
    assert keeper.is_file()  # forget never touches the original
    keeper.unlink()
    ghost.unlink()


def test_ids_only_and_folder_filter(client, photos_dir):
    sid = _live_sid(client, photos_dir)
    allids = client.get("/api/v1/pic/items", params={"ids_only": True}).json()
    assert allids["total"] and not allids["truncated"]
    assert len(allids["ids"]) == allids["total"]

    folders = client.get("/api/v1/pic/folders", params={"source_id": sid}).json()["folders"]
    assert "2024" in folders and "2024/beach" in folders
    sub = client.get("/api/v1/pic/items", params={"folder": "2024"}).json()
    assert sub["total"] >= 1
    assert all(i["rel_path"].startswith("2024/") for i in sub["items"])
    assert client.get("/api/v1/pic/items", params={"folder": "../etc"}).status_code == 422


def test_batched_pic_helpers_match_the_per_key_ones(client, db):
    """pic_settings / source_counts_all / lifecycle_counts are batched
    equivalents, not new semantics: they must agree with the per-key and
    per-source calls they replaced in /pic/status and /pic/sources."""
    from msc.models import PicItem, PicSource, PicSetting
    from msc.services.pic_index import (
        PIC_TUNABLES, lifecycle_counts, pic_setting, pic_settings,
        set_pic_setting, source_counts, source_counts_all,
    )

    set_pic_setting(db, "thumb_batch", 42)
    batched = pic_settings(db)
    assert set(batched) == set(PIC_TUNABLES)
    assert batched["thumb_batch"] == 42  # DB wins over the env default
    assert all(batched[key] == pic_setting(db, key) for key in PIC_TUNABLES)

    grouped = source_counts_all(db)
    for sid in [row[0] for row in db.query(PicSource.id).all()]:
        assert grouped.get(sid, {}) == source_counts(db, sid)

    direct = {
        "missing": db.query(PicItem).filter(PicItem.archived == 0,
                                            PicItem.missing == 1).count(),
        "trashed": db.query(PicItem).filter(PicItem.archived == 1).count(),
    }
    assert lifecycle_counts(db) == direct

    status = client.get("/api/v1/pic/status").json()
    assert status["settings"] == {key: batched[key] for key in PIC_TUNABLES}
    assert status["missing"] == direct["missing"] and status["trashed"] == direct["trashed"]

    db.query(PicSetting).filter(PicSetting.key == "thumb_batch").delete()
    db.commit()


def test_walk_writes_defaults_then_touches_existing_rows(client, db, photos_dir):
    """The scan INSERT is raw SQL: it bypasses the ORM column defaults, so
    missing/error_count/last_seen_at must be written explicitly — a NULL
    missing hides a photo from the gallery AND from every queue. A second walk
    inserts nothing and instead refreshes size/mtime and revives the row."""
    from msc.models import PicItem, PicSource
    from msc.picsources.base import PicEntry
    from msc.workers import pic_worker

    sid = client.post("/api/v1/pic/sources", json={
        "name": "touch-walk", "kind": "local", "root_path": str(photos_dir)}).json()["id"]
    src = db.get(PicSource, sid)
    walk = {"size": 10}

    class TwoFiles:
        kind = "local"

        def probe(self):
            return {"reachable": True, "root_ok": True, "error": None}

        def iter_entries(self, since=None):
            for rel in ("x.jpg", "y.jpg"):
                if since is not None and rel <= since:
                    continue
                yield PicEntry(rel_path=rel, name=rel, size=walk["size"],
                               mtime_iso="2024-01-01T00:00:00+00:00")

        def close(self):
            pass

    report = {"scanned": 0, "thumbs": 0, "failed": 0, "errors": []}
    pic_worker._scan(db, src, TwoFiles(), report)
    db.expire_all()
    rows = db.query(PicItem).filter(PicItem.source_id == sid).all()
    assert len(rows) == 2 and report["scanned"] == 2
    assert all(row.missing == 0 and row.error_count == 0 and row.archived == 0
               and row.last_seen_at and row.size_bytes == 10 for row in rows)

    # Second walk over the same paths: nothing new lands, the touch refreshes
    # metadata and brings a vanished-then-back photo into the gallery.
    one = db.get(PicItem, rows[0].id)
    one.missing, one.size_bytes = 1, 7
    src.last_scan_at, src.scan_cursor = None, None
    db.commit()
    walk["size"] = 999
    report2 = {"scanned": 0, "thumbs": 0, "failed": 0, "errors": []}
    pic_worker._scan(db, src, TwoFiles(), report2)
    db.expire_all()
    assert report2["scanned"] == 0  # INSERT OR IGNORE inserted nothing
    assert db.query(PicItem).filter(PicItem.source_id == sid).count() == 2
    revived = db.get(PicItem, one.id)
    assert revived.missing == 0 and revived.size_bytes == 999 and revived.last_seen_at
    assert db.get(PicSource, sid).state == "idle"

    db.query(PicItem).filter(PicItem.source_id == sid).delete()
    db.query(PicSource).filter(PicSource.id == sid).delete()
    db.commit()


def test_tile_urls_carry_the_content_version(client, photos_dir):
    """A photo id can outlive its bytes (purge + rescan, or an old database
    that reused ids). The tile URL must move with the content, or a browser
    cache paints the previous photo's picture over the new row — the exact
   错位 the gallery showed on macOS."""
    data = client.get("/api/v1/pic/items").json()
    with_thumb = [i for i in data["items"] if i["thumb_url"]]
    assert with_thumb
    for item in with_thumb:
        assert item["content_hash"]
        assert item["thumb_url"] == (
            f"/api/v1/pic/item/{item['id']}/thumb?v={item['content_hash']}")


def test_replaced_file_requeues_and_drops_the_stale_vector(client, db, photos_dir):
    """Same path, new bytes: thumb, caption and vector all describe a file
    that no longer exists — the walk demotes the row and evicts the vector so
    search and display can never keep answering with the old content."""
    from msc.models import PicItem, PicSource, PicVector
    from msc.picsources.base import PicEntry
    from msc.services.rag import pack_vec
    from msc.workers import pic_worker

    sid = client.post("/api/v1/pic/sources", json={
        "name": "swap-src", "kind": "local", "root_path": str(photos_dir)}).json()["id"]
    src = db.get(PicSource, sid)
    walk = {"size": 10}

    class OneFile:
        kind = "local"

        def probe(self):
            return {"reachable": True, "root_ok": True, "error": None}

        def iter_entries(self, since=None):
            if since is not None and "z.jpg" <= since:
                return
            yield PicEntry(rel_path="z.jpg", name="z.jpg", size=walk["size"],
                           mtime_iso="2024-01-01T00:00:00+00:00")

        def close(self):
            pass

    report = {"scanned": 0, "thumbs": 0, "failed": 0, "errors": []}
    pic_worker._scan(db, src, OneFile(), report)
    item = db.query(PicItem).filter_by(source_id=sid, rel_path="z.jpg").one()
    item.state, item.caption = "captioned", "旧照片的内容"
    db.add(PicVector(pic_id=item.id, model="test-embed", dim=1,
                     vec=pack_vec([1.0]), embedded_at="2026-01-01"))
    db.commit()
    item_id = item.id

    # The file behind z.jpg is replaced (size moves): the next walk must
    # rebuild everything derived from it.
    walk["size"] = 42
    src.last_scan_at, src.scan_cursor = None, None
    db.commit()
    report2 = {"scanned": 0, "thumbs": 0, "failed": 0, "errors": []}
    pic_worker._scan(db, src, OneFile(), report2)
    db.expire_all()
    assert report2.get("replaced") == 1
    reborn = db.get(PicItem, item_id)
    assert reborn.state == "discovered" and (reborn.caption or "") == ""
    assert reborn.size_bytes == 42
    assert db.query(PicVector).filter_by(pic_id=item_id).count() == 0

    db.query(PicItem).filter(PicItem.source_id == sid).delete()
    db.query(PicSource).filter(PicSource.id == sid).delete()
    db.commit()
