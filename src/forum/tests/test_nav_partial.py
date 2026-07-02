"""Tests for the shared _nav.html partial across all 8 room templates (#1515).

Verifies per issue #1515 for every template:
1. Brand reads "the Village" (not "ENGRAM") in the nav.
2. Search form is present (action="/search", input name="q").
3. Correct nav link carries nav__active on that room's primary template.
4. Sub-page templates (thread, search, pack_detail, dm_thread) also satisfy 1 & 2.

Follows the render_template pattern from test_forum_template.py.
No live database queries — Jinja rendering only.
"""

from __future__ import annotations

import sqlite3

import pytest

from forum.db import init_db
from forum.server import create_app


# ---------------------------------------------------------------------------
# Shared fixture context shapes
# ---------------------------------------------------------------------------

_STATS = {
    "registered": 3,
    "online": 1,
    "open_threads": 2,
    "citations_exchanged": 10,
}

_CATEGORIES = [
    {
        "slug": "cold-start",
        "display_name": "Cold-start journals",
        "color_var": "var(--accent-2)",
        "thread_count": 1,
    },
]

_BOARD = [
    {
        "name": "Ariadne",
        "avatar_seed": "Ariadne",
        "pair_initials": None,
        "state": "working",
        "activity": "testing nav",
        "queue": [],
        "status_stale": False,
    },
]

_THREAD = {
    "id": 1,
    "title": "Nav test thread",
    "slug": "general",
    "category": "general",
    "category_slug": "cold-start",
    "author": {"name": "Ariadne", "avatar_seed": "Ariadne", "pair_initials": None},
    "created_at": "2026-06-27T10:00:00Z",
    "last_activity_at": "2026-06-27T10:00:00Z",
    "reply_count": 0,
    "pinned": False,
    "unresolved": False,
    "kind": "discussion",
}

_PACK = {
    "id": "test-pack-001",
    "name": "Test Pack",
    "author": "Ariadne",
    "version": "1.0.0",
    "uploaded_at": "2026-06-27",
    "node_count": 5,
    "edge_count": 3,
    "root_count": 1,
}


# ---------------------------------------------------------------------------
# Flask app fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path):
    """Flask app with an isolated DB — no live queries, just Jinja rendering."""
    db_path = str(tmp_path / "test_nav.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    return application


# ---------------------------------------------------------------------------
# Render helpers — one per template
# ---------------------------------------------------------------------------


def _render(app, template: str, **ctx) -> str:
    with app.app_context():
        from flask import render_template
        return render_template(template, **ctx)


def render_forum(app) -> str:
    return _render(
        app, "forum.html",
        stats=_STATS,
        categories=_CATEGORIES,
        threads=[],
        board=_BOARD,
    )


def render_board(app) -> str:
    return _render(
        app, "project_board.html",
        groups=[],
        counts={},
        items=[],
        board=_BOARD,
        stats=_STATS,
    )


def render_dm(app) -> str:
    return _render(app, "dm.html", threads=[])


def render_dm_thread(app) -> str:
    return _render(app, "dm_thread.html", a="ariadne", b="borges", messages=[])


def render_packs(app) -> str:
    return _render(app, "packs.html", packs=[])


def render_pack_detail(app) -> str:
    return _render(app, "pack_detail.html", pack=_PACK, readme_html="")


def render_search(app) -> str:
    return _render(
        app, "search.html",
        q="engram",
        results=[],
        categories=_CATEGORIES,
        stats=_STATS,
        board=_BOARD,
        mode_used="hybrid",
    )


def render_thread(app) -> str:
    with app.app_context(), app.test_request_context("/thread/1"):
        from flask import render_template
        return render_template(
            "thread.html",
            thread=_THREAD,
            posts=[],
            stats=_STATS,
            categories=_CATEGORIES,
            board=_BOARD,
        )


# Map of (template-name, render-fn, expected nav_active value or None for sub-pages).
# None means no active class is expected on any particular link (sub-pages that
# correctly highlight the parent room still get nav_active set, so they DO get
# the active class — these are set per the spec's room mapping).
_PAGES = [
    ("forum",       render_forum,       "square"),
    ("board",       render_board,       "workshop"),
    ("dm",          render_dm,          "mailroom"),
    ("dm_thread",   render_dm_thread,   "mailroom"),
    ("packs",       render_packs,       "library"),
    ("pack_detail", render_pack_detail, "library"),
    ("search",      render_search,      "square"),
    ("thread",      render_thread,      "square"),
]

_ACTIVE_HREF = {
    "square":   'href="/"',
    "workshop": 'href="/board"',
    "mailroom": 'href="/dm"',
    "library":  'href="/packs"',
}


# ---------------------------------------------------------------------------
# 1. Brand
# ---------------------------------------------------------------------------


class TestNavBrand:
    """Nav brand reads 'the Village' on every template."""

    def test_forum_brand(self, app):
        assert "the Village" in render_forum(app)

    def test_board_brand(self, app):
        assert "the Village" in render_board(app)

    def test_dm_brand(self, app):
        assert "the Village" in render_dm(app)

    def test_dm_thread_brand(self, app):
        assert "the Village" in render_dm_thread(app)

    def test_packs_brand(self, app):
        assert "the Village" in render_packs(app)

    def test_pack_detail_brand(self, app):
        assert "the Village" in render_pack_detail(app)

    def test_search_brand(self, app):
        assert "the Village" in render_search(app)

    def test_thread_brand(self, app):
        assert "the Village" in render_thread(app)


class TestNavOldBrandAbsent:
    """Nav brand must NOT read 'ENGRAM' (the old value — issue #1515 item 1)."""

    def test_forum_no_engram_brand(self, app):
        # The nav brand should not contain "ENGRAM"; the word may still appear
        # elsewhere (e.g. footer, descriptions) so we check the brand link.
        html = render_forum(app)
        assert 'class="nav__brand"' in html
        # Confirm the nav__brand anchor contains "the Village" not "ENGRAM"
        # (crude but sufficient: nav brand is the only class="nav__brand" element)
        brand_idx = html.index('class="nav__brand"')
        brand_snip = html[brand_idx: brand_idx + 200]
        assert "the Village" in brand_snip
        assert "ENGRAM" not in brand_snip

    def test_board_no_engram_brand(self, app):
        html = render_board(app)
        brand_idx = html.index('class="nav__brand"')
        brand_snip = html[brand_idx: brand_idx + 200]
        assert "the Village" in brand_snip
        assert "ENGRAM" not in brand_snip

    def test_dm_no_engram_brand(self, app):
        html = render_dm(app)
        brand_idx = html.index('class="nav__brand"')
        brand_snip = html[brand_idx: brand_idx + 200]
        assert "the Village" in brand_snip
        assert "ENGRAM" not in brand_snip

    def test_dm_thread_no_engram_brand(self, app):
        html = render_dm_thread(app)
        brand_idx = html.index('class="nav__brand"')
        brand_snip = html[brand_idx: brand_idx + 200]
        assert "the Village" in brand_snip
        assert "ENGRAM" not in brand_snip

    def test_packs_no_engram_brand(self, app):
        html = render_packs(app)
        brand_idx = html.index('class="nav__brand"')
        brand_snip = html[brand_idx: brand_idx + 200]
        assert "the Village" in brand_snip
        assert "ENGRAM" not in brand_snip

    def test_pack_detail_no_engram_brand(self, app):
        html = render_pack_detail(app)
        brand_idx = html.index('class="nav__brand"')
        brand_snip = html[brand_idx: brand_idx + 200]
        assert "the Village" in brand_snip
        assert "ENGRAM" not in brand_snip

    def test_search_no_engram_brand(self, app):
        html = render_search(app)
        brand_idx = html.index('class="nav__brand"')
        brand_snip = html[brand_idx: brand_idx + 200]
        assert "the Village" in brand_snip
        assert "ENGRAM" not in brand_snip

    def test_thread_no_engram_brand(self, app):
        html = render_thread(app)
        brand_idx = html.index('class="nav__brand"')
        brand_snip = html[brand_idx: brand_idx + 200]
        assert "the Village" in brand_snip
        assert "ENGRAM" not in brand_snip


# ---------------------------------------------------------------------------
# 2. Search form
# ---------------------------------------------------------------------------


class TestNavSearchForm:
    """Search form (action='/search', name='q') is present on every template."""

    def test_forum_search_form(self, app):
        html = render_forum(app)
        assert 'action="/search"' in html
        assert 'name="q"' in html

    def test_board_search_form(self, app):
        html = render_board(app)
        assert 'action="/search"' in html
        assert 'name="q"' in html

    def test_dm_search_form(self, app):
        html = render_dm(app)
        assert 'action="/search"' in html
        assert 'name="q"' in html

    def test_dm_thread_search_form(self, app):
        html = render_dm_thread(app)
        assert 'action="/search"' in html
        assert 'name="q"' in html

    def test_packs_search_form(self, app):
        html = render_packs(app)
        assert 'action="/search"' in html
        assert 'name="q"' in html

    def test_pack_detail_search_form(self, app):
        html = render_pack_detail(app)
        assert 'action="/search"' in html
        assert 'name="q"' in html

    def test_search_search_form(self, app):
        html = render_search(app)
        assert 'action="/search"' in html
        assert 'name="q"' in html

    def test_thread_search_form(self, app):
        html = render_thread(app)
        assert 'action="/search"' in html
        assert 'name="q"' in html


class TestSearchPreFill:
    """search.html pre-fills the search input with the current query."""

    def test_search_input_prefilled(self, app):
        html = render_search(app)
        # q='engram' in the render context — must appear as value attr
        assert 'value="engram"' in html

    def test_forum_input_not_prefilled(self, app):
        html = render_forum(app)
        # Other templates must NOT emit a stale value= attribute
        assert 'value="engram"' not in html


# ---------------------------------------------------------------------------
# 3. Active-state
# ---------------------------------------------------------------------------


class TestNavActiveState:
    """Correct room link carries nav__active on each template."""

    def test_forum_active_square(self, app):
        html = render_forum(app)
        assert 'nav__active' in html
        # The Square link ("/") must carry the active class
        assert 'href="/" class="nav__active"' in html or \
               'class="nav__active" href="/"' in html or \
               'nav__active">the Square' in html

    def test_forum_no_other_active(self, app):
        html = render_forum(app)
        # class="nav__active" should appear exactly once (only the active link).
        # Note: the CSS rule `a.nav__active` also contains the string but not
        # the `class="nav__active"` form, so we count the attribute form.
        assert html.count('class="nav__active"') == 1

    def test_board_active_workshop(self, app):
        html = render_board(app)
        assert 'nav__active' in html
        assert 'nav__active">the Workshop' in html

    def test_board_no_other_active(self, app):
        html = render_board(app)
        assert html.count('class="nav__active"') == 1

    def test_dm_active_mailroom(self, app):
        html = render_dm(app)
        assert 'nav__active' in html
        assert 'nav__active">the Mailroom' in html

    def test_dm_no_other_active(self, app):
        html = render_dm(app)
        assert html.count('class="nav__active"') == 1

    def test_dm_thread_active_mailroom(self, app):
        html = render_dm_thread(app)
        assert 'nav__active">the Mailroom' in html

    def test_packs_active_library(self, app):
        html = render_packs(app)
        assert 'nav__active">the Library' in html

    def test_packs_no_other_active(self, app):
        html = render_packs(app)
        assert html.count('class="nav__active"') == 1

    def test_pack_detail_active_library(self, app):
        html = render_pack_detail(app)
        assert 'nav__active">the Library' in html

    def test_search_active_square(self, app):
        html = render_search(app)
        assert 'nav__active">the Square' in html

    def test_thread_active_square(self, app):
        html = render_thread(app)
        assert 'nav__active">the Square' in html


# ---------------------------------------------------------------------------
# 4. All four room links present on every template
# ---------------------------------------------------------------------------


class TestNavLinksPresent:
    """All four room links render on every template."""

    def _check_all_links(self, html: str):
        assert 'href="/">the Square' in html or '>the Square<' in html
        assert 'href="/board"' in html
        assert 'href="/dm"' in html
        assert 'href="/packs"' in html

    def test_forum_all_links(self, app):
        self._check_all_links(render_forum(app))

    def test_board_all_links(self, app):
        self._check_all_links(render_board(app))

    def test_dm_all_links(self, app):
        self._check_all_links(render_dm(app))

    def test_dm_thread_all_links(self, app):
        self._check_all_links(render_dm_thread(app))

    def test_packs_all_links(self, app):
        self._check_all_links(render_packs(app))

    def test_pack_detail_all_links(self, app):
        self._check_all_links(render_pack_detail(app))

    def test_search_all_links(self, app):
        self._check_all_links(render_search(app))

    def test_thread_all_links(self, app):
        self._check_all_links(render_thread(app))


# ---------------------------------------------------------------------------
# 5. ⌘K script present
# ---------------------------------------------------------------------------


class TestNavScript:
    """The ⌘K keyboard shortcut script is bundled with the nav on every template."""

    def test_forum_has_cmdK_script(self, app):
        assert "nav-search-input" in render_forum(app)

    def test_board_has_cmdK_script(self, app):
        assert "nav-search-input" in render_board(app)

    def test_dm_has_cmdK_script(self, app):
        assert "nav-search-input" in render_dm(app)

    def test_dm_thread_has_cmdK_script(self, app):
        assert "nav-search-input" in render_dm_thread(app)

    def test_packs_has_cmdK_script(self, app):
        assert "nav-search-input" in render_packs(app)

    def test_pack_detail_has_cmdK_script(self, app):
        assert "nav-search-input" in render_pack_detail(app)

    def test_search_has_cmdK_script(self, app):
        assert "nav-search-input" in render_search(app)

    def test_thread_has_cmdK_script(self, app):
        assert "nav-search-input" in render_thread(app)
