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


COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)")


def _channels(value):
    if value.startswith("#"):
        h = value[1:]
        if len(h) in (3, 4):
            return [int(c * 2, 16) for c in h[:3]]
        return [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    return [float(x) for x in re.findall(r"[\d.]+", value)[:3]]


def test_ui_ships_only_black_white_and_grey(client):
    """No hue anywhere: every colour literal must have equal channels. Hierarchy
    is brightness, weight and hairline borders - a stray indigo is a regression."""
    html, _ = _sources()
    texts = [(UI / "css" / "app.css").read_text(encoding="utf-8"),
             (UI / "js" / "graph.js").read_text(encoding="utf-8"), html]
    for text in texts:
        for value in COLOR_RE.findall(text):
            r, g, b = _channels(value)
            assert r == g == b, f"coloured value {value} came back"


def test_one_font_size_for_the_whole_ui(client):
    """Two steps used to sprawl into ten. --fs is the only text size in the app;
    --fs-micro is the single exception, the header wordmark at half of it."""
    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    assert set(re.findall(r"font-size:\s*([^;]+);", css)) == {"var(--fs)", "var(--fs-micro)"}
    html, js = _sources()
    assert "font-size" not in html + js          # no inline or scripted sizes
    assert "--fs: 13px" in css and "--fs-micro: calc(var(--fs) / 2)" in css


def test_graph_canvas_labels_use_the_same_one_size(client):
    js = (UI / "js" / "graph.js").read_text(encoding="utf-8")
    assert "const GRAPH_FONT_SIZE = 13;" in js
    assert not re.search(r"font:\s*{[^}]*size:\s*\d", js)   # no per-label px


def test_bare_links_stay_greyscale(client):
    """An <a> with no rule at all paints in the browser's blue/purple: that was
    the one way hue still reached the page (the summarise run log's slug)."""
    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    assert re.search(r"^a \{\n  color: var\(--text\);", css, re.M), "no base link colour"
    assert "a:visited { color: var(--text); }" in css


def test_search_page_offers_a_digest_of_the_hits(client):
    """「搜索并总结」 works on the rows currently on screen, so the button, the
    panel it writes into and the API method are one contract."""
    html, js = _sources()
    for marker in ('id="search-digest-btn"', 'id="search-digest"', 'onclick="searchAndSummarize()"'):
        assert marker in html, marker
    assert "API.summarizeSearch" in js and "/summarize/search" in js
    for fn in ("searchAndSummarize", "renderSearchDigest", "hideSearchDigest",
               "updateSearchDigestGate"):
        assert f"function {fn}" in js, fn
    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    for selector in (".search-digest", ".search-digest-head", ".search-digest-body",
                     ".search-digest-actions"):
        assert selector in css, selector


def test_graph_view_can_cap_edges_per_node(client):
    """The auto-linker writes thousands of keyword edges; one hub then keeps
    the Vis.js layout rotating forever. The cap control and the freeze-on-settle
    behaviour are one feature, so they are pinned together."""
    html, js = _sources()
    for marker in ('id="graph-degree-select"', 'onchange="graphDegreeChanged()"',
                   'onclick="relayoutGraph()"', 'id="graph-note"'):
        assert marker in html, marker
    assert "max_degree" in js
    assert "graphDegreeChanged" in js and "relayoutGraph" in js
    # physics must be switched off once the layout settles
    assert re.search(r"physics:\s*\{\s*enabled:\s*false", js), "layout never freezes"
    assert "stabilizationIterationsDone" in js
    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    assert ".graph-note" in css


def test_graph_view_can_filter_edges_by_relation_type(client):
    """Degree caps hide the busiest edges; type filters hide whole classes of
    them, and 「清除自动关联」 deletes only what the auto-linker wrote. The
    control, the API param and the delete route are one contract."""
    html, js = _sources()
    for marker in ('id="graph-relation-filters"', 'onclick="clearAutoLinks()"'):
        assert marker in html, marker
    assert "renderRelationFilters" in js and "graphRelationToggled" in js
    assert "edges_by_type" in js, "the checkbox counts come from the response"
    assert "getGraph(center, maxDegree, relations)" in js    # api.js takes the whitelist
    assert "params.relations" in js and "API.getGraph(null, cap, graphRelationsParam())" in js
    assert "`/memories/graph/auto-links`" in js or "'/memories/graph/auto-links'" in js
    assert "clearAutoLinks()" in js
    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    assert ".graph-filters" in css


def test_graph_relation_filter_reads_one_kind_of_collection(client):
    """The selection is a Set, the query string wants an array. The first build
    called .has() on the array inside loadGraph, and the catch then replaced a
    perfectly good graph with the fallback list. One predicate reads the Set,
    and decoration errors never reach that catch again."""
    js = (UI / "js" / "graph.js").read_text(encoding="utf-8")
    assert "function graphRelationHidden(type)" in js
    assert not re.search(r"graphRelationsParam\(\)\s*\.has\(", js)
    body = re.search(r"function graphNoteText.*?\n}", js, re.S).group(0)
    assert ".filter(graphRelationHidden)" in body and "graphRelationsParam" not in body
    # fetch errors fall back; drawing/summary errors must not
    assert "renderGraphFallback(allGraphData" in js
    assert "Graph summary error" in js


def test_memory_cards_fit_three_across_without_widening_the_page(client):
    """The excerpt was white-space: nowrap, so a 200-character body gave the
    card a huge min-content width; the sidebar grid's bare 1fr track takes its
    minimum from the content, so the page grew past the window and 多选 /
    小结所选 sat off-screen to the right. Both halves are pinned: the excerpt is
    clamped, and every card track is minmax(0, 1fr)."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    section = html.split('<section id="page-memories"', 1)[1].split('</section>')[0]
    assert 'filters-sidebar' not in html and 'memories-layout' not in html + css
    assert '<div class="memories-filters">' in section
    assert section.index('memories-filters') < section.index('id="memories-list"'), \
        "the filter bar must sit above the cards"
    assert 'id="memories-list" class="memory-list memory-grid"' in section
    assert 'repeat(3, minmax(0, 1fr))' in css
    declarations = re.sub(r"/\*.*?\*/", "", css, flags=re.S)   # comments are free text
    desc = re.search(r"\.memory-desc \{[^}]*\}", declarations).group(0)
    assert "white-space" not in desc, "the excerpt can widen the page again"
    assert "-webkit-line-clamp: 2" in desc
    # app.js reads these ids to build the query; renaming one breaks filtering
    # silently, which is exactly what the 2.4.7 sync-indicator bug was.
    for marker in ('id="filter-categories"', 'id="filter-priorities"', 'id="filter-branch"',
                   'id="filter-archived"', 'id="select-mode-btn"', 'id="memories-count"'):
        assert marker in section, marker


def test_pic_tuning_row_cannot_widen_the_settings_page(client):
    """Six 摄入调参 fields plus a status line in one flex row: a bare 1fr track
    takes its minimum from its content, so a long value (or the old raw
    JSON.stringify dump) widens the card, then the page — the row reads as
    跳歪. Pinned: every settings/tuning track is minmax(0, 1fr), the status
    lines own a line with min-width 0, the save handler reports the values the
    server kept in words, and each field carries min/max so the client can
    clamp a typo instead of bouncing a validation error."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    pic_js = (UI / "js" / "pic.js").read_text(encoding="utf-8")
    api_js = (UI / "js" / "api.js").read_text(encoding="utf-8")
    declarations = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    grid = re.search(r"\.settings-grid \{[^}]*\}", declarations).group(0)
    assert "minmax(0, 1fr)" in grid, "a bare 1fr column inherits its min from content"

    tuning = re.search(r"\.form-row\.pic-tuning \{[^}]*\}", declarations).group(0)
    assert "repeat(3, minmax(0, 1fr))" in tuning
    after = html.split('<div class="form-row pic-tuning">', 1)[1]
    row = after.split('<div class="sync-controls">', 1)[0]
    for field in ("pic-set-scan-seconds", "pic-set-thumb-batch", "pic-set-rescan",
                  "pic-set-thumb-px", "pic-set-burst-seconds", "pic-set-caption-workers"):
        marker = f'id="{field}"'
        assert marker in row, marker
        input_tag = row[row.index(marker):row.index(">", row.index(marker)) + 1]
        assert 'min="' in input_tag and 'max="' in input_tag, f"{field} needs a clamp range"

    for selector in ("#pic-settings-status", "#pic-cap-pause-status"):
        rule = re.search(rf"{re.escape(selector)}[^{{]*\{{[^}}]*\}}", declarations).group(0)
        assert "min-width: 0" in rule and "flex: 1 1 100%" in rule, selector

    assert "JSON.stringify(r.settings)" not in pic_js
    assert "function picSettingsSummary" in pic_js and "picSettingsSummary(s)" in pic_js
    assert "function apiErrorText" in api_js
    assert "apiErrorText(data, res.status)" in api_js
    assert "new Error((data && data.detail)" not in api_js, \
        "a FastAPI validation list stringifies to [object Object]"
