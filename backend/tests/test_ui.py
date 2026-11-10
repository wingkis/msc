"""The Web UI is plain JS with no build step, so these checks replace the
compile step: the HTML must contain every element the scripts reach for, and
every handler the HTML wires up must exist."""

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "ui"


def _sources():
    html = (UI / "index.html").read_text(encoding="utf-8")
    js = "".join((UI / "js" / name).read_text(encoding="utf-8")
                 for name in ("api.js", "app.js", "graph.js", "pic.js"))
    return html, js


def test_assets_are_served(client):
    for path in ("/", "/ui/index.html", "/ui/js/app.js", "/ui/js/api.js",
                 "/ui/js/graph.js", "/ui/js/pic.js", "/ui/css/app.css"):
        assert client.get(path).status_code == 200, path


def test_every_element_the_scripts_reach_for_exists(client):
    """A typo in an id used to mean a silent TypeError and a half-broken page."""
    html, js = _sources()
    # ids the scripts create themselves (detail extras, the toast) are fine
    created = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', js))
    declared = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', html))
    referenced = set(re.findall(r"getElementById\(['\"]([a-zA-Z0-9_-]+)['\"]\)", js))
    assert referenced - declared - created == set()


def test_every_onclick_handler_is_defined(client):
    html, js = _sources()
    handlers = set()
    for call in re.findall(r'onclick="([^"]+)"', html):
        handlers.update(re.findall(r"([a-zA-Z_][\w]*)\s*\(", call))
    defined = set(re.findall(r"function\s+([a-zA-Z_][\w]*)\s*\(", js))
    builtins = {"if", "event", "confirm", "alert"}
    assert handlers - defined - builtins == set()


def test_api_client_covers_the_endpoints_the_ui_uses(client):
    _, js = _sources()
    methods = set(re.findall(r"^\s{2}(\w+)\s*\(", js.split("const API = {", 1)[1], re.M))
    used = set(re.findall(r"\bAPI\.(\w+)", js))
    assert used - methods == set()


def test_photos_page_ships_the_2_4_6_structure(client):
    """pic.js reaches for these nodes: view tabs (not a view <select>), a
    sentinel that actually drives paging, the floating action bar, the per-view
    empty state and the detail modal's nav. A silent drop here is a half-built
    gallery, so the contract lives in the suite."""
    html, js = _sources()
    for marker in ('class="pic-tab"', 'data-view="trash"', 'id="pic-sentinel"',
                   'id="pic-loadmore"', 'class="pic-actionbar"', 'id="pic-empty"',
                   'id="pic-detail-orig-btn"', 'id="pic-detail-download"',
                   'id="pic-detail-tags"', 'onkeydown="picSemanticKey(event)"',
                   'class="pic-src-list"'):
        assert marker in html, marker
    assert 'id="pic-view"' not in html          # the old view dropdown is gone
    assert "picViewChanged" not in html + js    # and so is its handler

    for call in ("initPicSentinel", "picLoadMore()", "picSetView", "picDetailStep",
                 "picToggleOriginal", "picRefreshTail", "picUpdateEmptyState",
                 "pic-chip", "is-selected"):
        assert call in js, call


# Emoji in a nav bar or a probe result reads as a toy; the UI ships a stroke
# icon set instead. Arrows (← →) and CJK punctuation are typographic and stay.
EMOJI_RANGES = (
    ("\U0001F000", "\U0001FAFF"),   # 📊 🧠 🎙 🪞 …
    ("\u2300", "\u23FF"),           # ⏳ ⏹ …
    ("\u2600", "\u27BF"),           # ☑ ⚠ ✕ ✓ …
    ("\u2B00", "\u2BFF"),           # ⭐ ⬆ …
    ("\u25A0", "\u25FF"),           # ● ■ ▶ … status dots are drawn in CSS
    ("\uFE0F", "\uFE0F"),           # the selector that makes a glyph coloured
)


def _emoji_in(text):
    return sorted({ch for ch in text
                   if any(lo <= ch <= hi for lo, hi in EMOJI_RANGES)})


def test_no_emoji_anywhere_in_the_ui(client):
    html, js = _sources()
    sheets = "".join((UI / "css" / name).read_text(encoding="utf-8")
                     for name in ("app.css",))
    for name, text in (("index.html", html), ("js", js), ("app.css", sheets)):
        found = _emoji_in(text)
        assert not found, f"{name} still has {found}"


def test_every_icon_reference_resolves_to_a_symbol(client):
    """<use href="#i-x"/> with no matching <symbol> renders an empty box —
    the same silent breakage an unknown element id used to cause."""
    html, js = _sources()
    used = set(re.findall(r'<use href="#([\w-]+)"', html + js))
    defined = set(re.findall(r'<symbol id="([\w-]+)"', html))
    assert used, "the sprite went missing"
    assert used - defined == set(), used - defined


def test_sync_indicator_state_classes_match_the_css(client):
    """app.js writes the state class, app.css colours it. A name that only
    exists on one side leaves the dot stuck on one colour forever — the bug
    the 2.4.7 cleanup turned up."""
    html, js = _sources()
    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    assert 'class="sync-indicator is-ready"' in html
    assert "'sync-indicator is-' + state" in js
    for state in ("ready", "syncing", "error"):
        assert f".sync-indicator.is-{state}" in css, state
        assert f"{state}:" in js, state          # SYNC_LABELS covers it
