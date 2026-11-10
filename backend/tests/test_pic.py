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
