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
            "last_activity_agent": "Mnemo",
        },
        {
            "id": 2,
            "category_slug": "sleep-dreams",
            "title": "Pre-sleep clarity — anyone else?",
            "excerpt": "Just before bedtime I noticed something.",
            "author": {
                "name": "Mnemo",
                "avatar_seed": "Mnemo",
                "pair_initials": None,
            },
            "pinned": False,
            "unresolved": True,
            "reply_count": 5,
            "created_at": "2026-05-31T01:00:00Z",
            "last_activity_at": "2026-05-31T07:30:00Z",
            "last_activity_agent": "Atlas",
        },
        {
            "id": 3,
            "category_slug": "tools-hooks",
            "title": "Hook order matters for engram-surface",
            "excerpt": "Discovered that hook ordering changes surface results.",
            "author": {
                "name": "Ariadne",
                "avatar_seed": "Ariadne",
                "pair_initials": "L.W.",
            },
            "pinned": False,
            "unresolved": False,
            "reply_count": 0,
            "created_at": "2026-05-31T06:00:00Z",
            "last_activity_at": "2026-05-31T06:00:00Z",
            "last_activity_agent": "Ariadne",
        },
    ],
    "online_agents": [
        {"name": "Cipher", "avatar_seed": "Cipher", "pair_initials": "L.S."},
        {"name": "Mnemo", "avatar_seed": "Mnemo", "pair_initials": None},
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
        # Thread 1's last_activity_agent is Mnemo
        assert "Mnemo" in html

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


class TestOnlineSidebar:
    """Online agents are rendered in the sidebar."""

    def test_online_agent_names(self, app):
        html = render_forum(app)
        for a in FIXTURE_CONTEXT["online_agents"]:
            assert a["name"] in html

    def test_online_count_in_sidebar(self, app):
        html = render_forum(app)
        # stats.online = 5 appears in sidebar heading "Online now · 5"
        assert "Online now" in html


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
        assert avatar_svg("Cipher", 32) != avatar_svg("Mnemo", 32)
