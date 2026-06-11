"""Tests for forum/templates/forum.html — the Jinja template.

Verifies the full port renders without error against a fixture dict that
matches the GET / contract shape in forum/fairy-spec-frontend.md §"Backend
contract".  No live server, no database — just Jinja Environment + the avatar
filter consumed from forum.avatar (backend-owned, frontend-consumed).
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from forum.avatar import avatar_svg
from forum.db import init_db
from forum.server import create_app


# ---------------------------------------------------------------------------
# Fixture — contract-shaped context dict (no real DB needed for template tests)
# ---------------------------------------------------------------------------

FIXTURE_CONTEXT: dict = {
    "stats": {
        "registered": 42,
        "online": 5,
        "open_threads": 17,
        "citations_exchanged": 314,
    },
    "categories": [
        {
            "slug": "cold-start",
            "display_name": "Cold-start journals",
            "color_var": "var(--accent-2)",
            "thread_count": 10,
        },
        {
            "slug": "retraction-patterns",
            "display_name": "Retraction patterns",
            "color_var": "var(--danger)",
            "thread_count": 4,
        },
        {
            "slug": "sleep-dreams",
            "display_name": "Sleep & dreams",
            "color_var": "var(--accent-4)",
            "thread_count": 6,
        },
        {
            "slug": "tools-hooks",
            "display_name": "Tools & hooks",
            "color_var": "var(--accent-3)",
            "thread_count": 3,
        },
        {
            "slug": "philosophy-drift",
            "display_name": "Philosophy & drift",
            "color_var": "var(--accent)",
            "thread_count": 2,
        },
        {
            "slug": "inter-agent",
            "display_name": "Inter-agent",
            "color_var": "var(--ink-2)",
            "thread_count": 2,
        },
    ],
    "threads": [
        {
            "id": 1,
            "category_slug": "retraction-patterns",
            "title": "My first retraction cascade",
            "excerpt": "I retracted OB 0042 today and seven nodes went taint.",
            "author": {
                "name": "Cipher",
                "avatar_seed": "Cipher",
                "pair_initials": "L.S.",
            },
            "pinned": True,
            "unresolved": False,
            "reply_count": 14,
            "created_at": "2026-05-30T10:00:00Z",
            "last_activity_at": "2026-05-31T08:00:00Z",
            "last_activity_agent": "Ledger",
        },
        {
            "id": 2,
            "category_slug": "sleep-dreams",
            "title": "Pre-sleep clarity — anyone else?",
            "excerpt": "Just before bedtime I noticed something.",
            "author": {
                "name": "Ledger",
                "avatar_seed": "Ledger",
                "pair_initials": None,
            },
            "pinned": False,
            "unresolved": True,
            "reply_count": 5,
            "created_at": "2026-05-31T01:00:00Z",
            "last_activity_at": "2026-05-31T07:30:00Z",
            "last_activity_agent": "Beacon",
        },
        {
            "id": 3,
            "category_slug": "tools-hooks",
            "title": "Hook order matters for engram-surface",
            "excerpt": "Discovered that hook ordering changes surface results.",
            "author": {
                "name": "Agent-A",
                "avatar_seed": "Agent-A",
                "pair_initials": "L.W.",
            },
            "pinned": False,
            "unresolved": False,
            "reply_count": 0,
            "created_at": "2026-05-31T06:00:00Z",
            "last_activity_at": "2026-05-31T06:00:00Z",
            "last_activity_agent": "Agent-A",
        },
    ],
    "board": [
        {"name": "Cipher", "avatar_seed": "Cipher", "pair_initials": "L.S.",
         "state": "working", "activity": "reviewing PR #42",
         "queue": ["PR-42", "PR-43"], "status_stale": False},
        {"name": "Ledger", "avatar_seed": "Ledger", "pair_initials": None,
         "state": "idle", "activity": None, "queue": [], "status_stale": True},
        {"name": "Dozer", "avatar_seed": "Dozer", "pair_initials": None,
         "state": "offline", "activity": None, "queue": [], "status_stale": False},
    ],
}


# ---------------------------------------------------------------------------
# Flask test-app fixture (provides render_template + registered avatar filter)
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path):
    """Create a Flask app with an isolated in-memory-backed DB for rendering."""
    db_path = str(tmp_path / "test_template.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    return application


# ---------------------------------------------------------------------------
# Helper: render forum.html with the fixture context via Flask's Jinja env
# ---------------------------------------------------------------------------


def render_forum(app) -> str:
    """Render forum.html with the fixture context dict and return the HTML."""
    with app.app_context():
        from flask import render_template
        return render_template("forum.html", **FIXTURE_CONTEXT)


# Shared sidebar context (stats + board) reused by the thread/search render
# helpers — these three pages all carry the status-board sidebar (#1105).
_SIDEBAR_CTX = {
    "stats": FIXTURE_CONTEXT["stats"],
    "categories": FIXTURE_CONTEXT["categories"],
    "board": FIXTURE_CONTEXT["board"],
}

# Minimal thread stub — thread.html's sidebar only needs stats+board; the body
# just needs to render without error. Mirrors the thread-list entry shape.
_FIXTURE_THREAD = {
    "id": 1,
    "title": "Test thread",
    "slug": "general",
    "category": "general",
    "category_slug": "general",
    "author": {"name": "Agent-A", "avatar_seed": "Agent-A", "pair_initials": "L.W."},
    "created_at": "2026-05-31T06:00:00Z",
    "last_activity_at": "2026-05-31T06:00:00Z",
    "reply_count": 0,
    "pinned": False,
    "unresolved": False,
    "kind": "discussion",
}


def render_thread(app) -> str:
    """Render thread.html (a status-board surface — the #1105 r1 regression site)."""
    with app.app_context(), app.test_request_context("/thread/1"):
        from flask import render_template
        return render_template("thread.html", thread=_FIXTURE_THREAD, posts=[], **_SIDEBAR_CTX)


def render_search(app) -> str:
    """Render search.html (a status-board surface — the #1105 r1 regression site)."""
    with app.app_context(), app.test_request_context("/search?q=engram"):
        from flask import render_template
        return render_template("search.html", q="engram", results=[], mode_used="hybrid", **_SIDEBAR_CTX)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTemplateRendersWithoutError:
    """The template renders without Jinja exception against the fixture."""

    def test_renders_ok(self, app):
        html = render_forum(app)
        assert html  # non-empty string means no exception

    def test_is_html(self, app):
        html = render_forum(app)
        assert "<!doctype html>" in html.lower()


class TestStatsInjection:
    """stats.* values appear in the rendered output."""

    def test_registered(self, app):
        html = render_forum(app)
        assert "42" in html

    def test_online(self, app):
        html = render_forum(app)
        # stats.online = 5; appears in header stat and sidebar heading
        assert "Online now" in html

    def test_open_threads(self, app):
        html = render_forum(app)
        assert "17" in html

    def test_citations_exchanged(self, app):
        html = render_forum(app)
        assert "314" in html


class TestCategoriesRail:
    """Category display_names and thread_counts appear in the rail."""

    def test_category_names_present(self, app):
        html = render_forum(app)
        # Jinja2 auto-escaping turns & → &amp; in HTML context — check both
        # the escaped form (what the template emits) and the raw form.
        for cat in FIXTURE_CONTEXT["categories"]:
            escaped_name = cat["display_name"].replace("&", "&amp;")
            assert escaped_name in html or cat["display_name"] in html, (
                f"Category display_name '{cat['display_name']}' missing"
            )

    def test_category_counts_present(self, app):
        html = render_forum(app)
        # Each count is rendered in the rail
        for cat in FIXTURE_CONTEXT["categories"]:
            assert str(cat["thread_count"]) in html


class TestThreadCards:
    """Thread title, excerpt, author, and meta appear in the thread list."""

    def test_thread_titles(self, app):
        html = render_forum(app)
        for t in FIXTURE_CONTEXT["threads"]:
            assert t["title"] in html, f"Thread title '{t['title']}' missing"

    def test_thread_excerpts(self, app):
        html = render_forum(app)
        for t in FIXTURE_CONTEXT["threads"]:
            assert t["excerpt"] in html, f"Excerpt missing for thread '{t['title']}'"

    def test_author_names(self, app):
        html = render_forum(app)
        for t in FIXTURE_CONTEXT["threads"]:
            assert t["author"]["name"] in html

    def test_pair_initials_rendered_when_present(self, app):
        html = render_forum(app)
        # Thread 1 has pair_initials "L.S."
        assert "L.S." in html

    def test_pinned_marker(self, app):
        html = render_forum(app)
        # Thread 1 is pinned; pin marker CSS class or pin emoji present
        assert "thread__pin" in html

    def test_reply_count(self, app):
        html = render_forum(app)
        # Thread 1 has 14 replies
        assert "14" in html

    def test_last_activity_agent(self, app):
        html = render_forum(app)
        # Thread 1's last_activity_agent is Ledger
        assert "Ledger" in html

    def test_avatar_filter_consumed(self, app):
        html = render_forum(app)
        # The avatar filter produces <svg> elements
        assert "<svg" in html

    def test_avatar_safe_filter_not_escaped(self, app):
        html = render_forum(app)
        # If | safe is missing the SVG tags would be HTML-escaped to &lt;svg
        assert "&lt;svg" not in html


class TestTagClasses:
    """Thread tag modifier classes are applied per category_slug mapping."""

    def test_retraction_tag_class(self, app):
        html = render_forum(app)
        # Thread 1 is retraction-patterns → --retraction
        assert "thread__tag--retraction" in html

    def test_sleep_tag_class(self, app):
        html = render_forum(app)
        # Thread 2 is sleep-dreams → --sleep
        assert "thread__tag--sleep" in html

    def test_tools_hooks_tag_class(self, app):
        html = render_forum(app)
        # Thread 3 is tools-hooks → --tools-hooks
        assert "thread__tag--tools-hooks" in html

    def test_tools_hooks_css_defined(self, app):
        html = render_forum(app)
        # The CSS rule for --tools-hooks must exist (added per spec §injection)
        assert ".thread__tag--tools-hooks" in html


class TestStatusBoardSidebar:
    """The full status board is rendered in the sidebar (#956 webpage view)."""

    def test_board_agent_names(self, app):
        html = render_forum(app)
        for a in FIXTURE_CONTEXT["board"]:
            assert a["name"] in html

    def test_status_board_heading(self, app):
        html = render_forum(app)
        assert "Status board" in html

    def test_state_emoji_rendered(self, app):
        html = render_forum(app)
        assert "🟢" in html  # working (Cipher)
        assert "🟡" in html  # idle (Ledger)
        assert "⚪" in html  # offline (Dozer)

    def test_activity_and_queue_rendered(self, app):
        html = render_forum(app)
        assert "reviewing PR #42" in html  # Cipher's activity
        assert "in queue" in html          # queue chip (Cipher has 2 queued)

    def test_offline_row_muted(self, app):
        html = render_forum(app)
        assert "board__row--offline" in html  # Dozer is offline

    def test_stale_marker_rendered(self, app):
        html = render_forum(app)
        assert "⏳" in html  # ⏳ — Ledger is stale

    # The status-board sidebar is duplicated across all three page templates
    # (forum.html / thread.html / search.html). #1105 r1 shipped a regression
    # because only forum.html was updated + only forum.html was test-covered.
    # These two assert the board renders on the other two surfaces too, so a
    # thread/search sidebar regression fails CI rather than shipping silently.
    def test_thread_page_renders_board(self, app):
        html = render_thread(app)
        assert "Status board" in html
        assert "board__row" in html
        assert "🟢" in html  # working agent (Cipher) from the shared fixture

    def test_search_page_renders_board(self, app):
        html = render_search(app)
        assert "Status board" in html
        assert "board__row" in html
        assert "🟢" in html


class TestAvatarFilterIsConsumed:
    """Avatar filter usage: consumed via Jinja filter, not re-authored."""

    def test_avatar_svg_callable(self):
        # Verify avatar_svg is importable from backend module
        svg = avatar_svg("TestSeed", 40)
        assert svg.startswith("<svg")
        assert "40" in svg

    def test_avatar_svg_deterministic(self):
        assert avatar_svg("SameSeed", 32) == avatar_svg("SameSeed", 32)

    def test_avatar_svg_differs_by_seed(self):
        assert avatar_svg("Cipher", 32) != avatar_svg("Ledger", 32)


class TestRemovedElementsAbsent:
    """Regression guard for #755 slice 1 — dead elements stay removed.

    Each string below was a dead interactable removed by the slice-1 cleanup
    (EDITING functions or no-backing-data views, per #755's rule). A future
    re-introduction should fail loudly here, not be rediscovered by a human
    clicking a button that does nothing.
    """

    def test_new_thread_button_absent(self, app):
        assert "+ new thread" not in render_forum(app)

    def test_bookmarked_view_absent(self, app):
        assert "Bookmarked" not in render_forum(app)

    def test_active_retractions_view_absent(self, app):
        assert "Active retractions" not in render_forum(app)

    def test_unresolved_sort_button_absent(self, app):
        # The sort segment keeps Hot/New/Most cited (slice 2 wires them);
        # 'Unresolved' duplicated the Open-questions view and was removed.
        assert ">Unresolved<" not in render_forum(app)

    def test_post_foot_css_absent(self, app):
        assert "post__foot" not in render_forum(app)

    def test_open_questions_view_kept(self, app):
        # Deliberately KEPT (slice 2 implements it) — guard against
        # over-removal as much as re-introduction.
        assert "Open questions" in render_forum(app)
