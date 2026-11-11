"""Photo index glue: tuning table, state counts, thumbnail and EXIF helpers.

Same "env seeds, DB wins" doctrine as services/rag.py's setting(). The
pipeline state machine lives on pic_items.state; these helpers keep the
worker and the API reading the same definitions.
"""

import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from msc.config import get_settings
from msc.models import PicSetting

_log = logging.getLogger("msc.pic")

PIC_TUNABLES = ("scan_seconds", "thumb_batch", "rescan_hours", "thumb_px",
                "caption_batch", "embed_batch", "caption_paused",
                "caption_burst", "burst_seconds", "caption_workers")

# Filename patterns that mean "this is a screenshot, not a camera photo".
_SCREENSHOT_MARKS = ("screenshot", "screen_shot", "screen shot", "screencapture",
                     "screen capture", "截图", "截屏")

# HEIC/HEIF needs the pillow-heif plugin; when it is missing (or the wheel
# failed to build on this platform) HEIC files mark themselves failed
# instead of taking the whole pipeline down.
HEIF_OK = False
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception as exc:  # pragma: no cover - platform dependent
    _log.info("pillow-heif unavailable, HEIC photos will be skipped: %s", exc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pic_defaults() -> dict:
    return {"scan_seconds": 8, "thumb_batch": 16, "rescan_hours": 24,
            "thumb_px": get_settings().pic_thumb_px,
            # 1 by default: captioning shares a busy local GPU (the vision card
            # usually sits on the same box that answers chat). Raise it when
            # the endpoint can take it.
            "caption_batch": 1, "embed_batch": 8,
            # 1 = the vision stage rests: thumb/scan keep flowing, queue waits.
            "caption_paused": 0,
            # 能力全开 (a button on the 照片源 card, off by default — the paced
            # default is what protects a GPU that is also answering chat).
            # caption_burst 1 lifts the one-batch-per-tick pacing: the vision
            # stage keeps pulling the queue until burst_seconds of model time
            # is used, embed follows the same budget, and the worker idles
            # seconds between passes instead of 15. caption_workers > 1 puts
            # that many vision requests on the wire at once — it only pays off
            # when the vision server has more than one slot, otherwise the
            # extra requests just queue there, so 1 stays the default.
            "caption_burst": 0, "burst_seconds": 300, "caption_workers": 1}


def pic_settings(db: Session) -> dict:
    """Every tunable in ONE query.

    The worker used to ask for thumb_px once per photo and /pic/status asked
    six times per poll — a SELECT per key on every call, while a thousand-photo
    ingest is committing on the same connection. Anything that needs more than
    one key takes this instead of pic_setting().
    """
    out = _pic_defaults()
    for row in db.query(PicSetting).all():
        if row.key not in out:
            continue
        try:
            out[row.key] = int(row.value)
        except ValueError:
            _log.warning("pic_settings.%s is not an integer: %r", row.key, row.value)
    return out


def pic_setting(db: Session, key: str) -> int:
    row = db.query(PicSetting).filter(PicSetting.key == key).first()
    if row:
        try:
            return int(row.value)
        except ValueError:
            _log.warning("pic_settings.%s is not an integer: %r", key, row.value)
    return _pic_defaults()[key]


def set_pic_setting(db: Session, key: str, value: int) -> None:
    row = db.query(PicSetting).filter(PicSetting.key == key).first()
    if row is None:
        row = PicSetting(key=key, value=str(int(value)), updated_at=_now())
        db.add(row)
    else:
        row.value = str(int(value))
        row.updated_at = _now()
    db.commit()


def state_counts(db: Session) -> dict:
    """Live (non-trashed) photo counts per pipeline state."""
    rows = db.execute(sql_text(
        "SELECT state, count(*) FROM pic_items WHERE archived = 0 GROUP BY state"
    )).fetchall()
    return {state: count for state, count in rows}


def source_counts(db: Session, source_id: int) -> dict:
    rows = db.execute(sql_text(
        "SELECT state, count(*) FROM pic_items WHERE archived = 0 AND source_id = :p0 "
        "GROUP BY state"), {"p0": source_id}).fetchall()
    return {state: count for state, count in rows}


def source_counts_all(db: Session) -> dict:
    """{source_id: {state: count}} from one grouped query.

    /pic/status and /pic/sources render every card on every poll; calling
    source_counts() per card made that O(sources) queries for the same rows.
    """
    out: dict = {}
    for source_id, state, count in db.execute(sql_text(
            "SELECT source_id, state, count(*) FROM pic_items WHERE archived = 0 "
            "GROUP BY source_id, state")):
        out.setdefault(source_id, {})[state] = count
    return out


def lifecycle_counts(db: Session) -> dict:
    """missing / trashed in one pass instead of two full COUNT scans."""
    missing, trashed = db.execute(sql_text(
        "SELECT sum(CASE WHEN archived = 0 AND missing = 1 THEN 1 ELSE 0 END), "
        "sum(CASE WHEN archived = 1 THEN 1 ELSE 0 END) FROM pic_items")).fetchone()
    return {"missing": int(missing or 0), "trashed": int(trashed or 0)}


def guess_kind(name: str) -> str:
    low = (name or "").lower()
    return "screenshot" if any(mark in low for mark in _SCREENSHOT_MARKS) else "photo"


def content_hash(size: int, head: bytes) -> str:
    """blake2b over the size plus the first megabyte — cheap on remote sources
    (one Range GET) and enough to spot burst shots and exact duplicates."""
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    h.update(b"|")
    h.update(head)
    return h.hexdigest()


def thumb_abs_path(rel_thumb: str):
    from pathlib import Path
    return Path(get_settings().pic_dir) / rel_thumb


# EXIF tag ids (0x8769 Exif sub-IFD: 36867 DateTimeOriginal, 36881 OffsetTimeOriginal).
_EXIF_IFD = 0x8769
_TAG_DATETIME_ORIGINAL = 36867
_TAG_OFFSET_TIME_ORIGINAL = 36881


def taken_at_from_image(im, mtime_iso: str):
    """(taken_at ISO, source marker) — EXIF first, filesystem mtime second.

    Naive EXIF timestamps (the overwhelming majority: cameras store local
    wall time) are kept as written and marked 'exif_naive' — for a "browse
    by the day the photo was taken" gallery the camera's local day IS the
    right answer, and pretending it is UTC would shift night shots to noon.
    """
    try:
        exif = im.getexif()
        sub = exif.get_ifd(_EXIF_IFD) if exif else {}
        raw = str(sub.get(_TAG_DATETIME_ORIGINAL) or "").strip()
        if len(raw) >= 19 and raw[4] == ":":
            local = f"{raw[0:4]}-{raw[5:7]}-{raw[8:10]}T{raw[11:13]}:{raw[14:16]}:{raw[17:19]}"
            offset = str(sub.get(_TAG_OFFSET_TIME_ORIGINAL) or "").strip()
            if len(offset) >= 5 and offset[0] in "+-" and offset[3] == ":":
                return f"{local}{offset}", "exif"
            return local, "exif_naive"
    except Exception as exc:  # malformed EXIF is common, never fatal
        _log.debug("exif taken_at parse failed: %s", exc)
    return mtime_iso, "mtime"


def image_size(im):
    w, h = im.size
    return int(w), int(h)


def pic_semantic_ids(db: Session, q: str, top: int):
    """Ranked photo ids for a semantic pass. Returns (ids, cos_by_id, error).

    Same embed card as memories (bge-m3) but scans pic_vectors only — photos
    and memories stay in their own corpora on purpose.
    """
    from msc.models import PicVector
    from msc.services.rag import embed_query, scan_vector_table

    vec, model, error = embed_query(db, q)
    if vec is None:
        return [], {}, error
    # The live-id gate only ever filters rows the cosine scan is about to read,
    # so restrict it to photos that actually hold a vector for this model:
    # the set is as small as the corpus, not one int per photo in the library.
    live = {row[0] for row in db.execute(sql_text(
        "SELECT i.id FROM pic_items i JOIN pic_vectors v "
        "ON v.pic_id = i.id AND v.model = :p0 "
        "WHERE i.archived = 0 AND i.missing = 0"), {"p0": model})}
    scored = scan_vector_table(db, PicVector, PicVector.pic_id, PicVector.model,
                               vec, model, top, live)
    return [pic_id for pic_id, _ in scored], dict(scored), None
