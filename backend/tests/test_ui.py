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
