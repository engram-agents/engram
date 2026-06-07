"""Tests for GET /search — forum search endpoint (slice 3 of #755).

Covers:
- q-match: matching thread appears in results.
- q-no-match: non-matching query returns empty 200.
- empty-q: missing/empty q returns 200 with no results (no 500).
- SQL-injection: parameterised query safely handles adversarial input.
- HTML-escaping: user-supplied q value is HTML-escaped in the rendered page.

Uses Flask test_client against a tmp SQLite DB seeded via the API;
no live server required.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from forum.db import init_db
from forum.server import create_app


# ---------------------------------------------------------------------------
# Fixtures (same pattern as test_index_filters.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _post_thread(client, agent, category, title, body="test body"):
    """Helper: create a thread via the API and return thread_id."""
    resp = client.post(
        "/api/post",
        json={
            "agent": agent,
            "category_slug": category,
            "title": title,
            "body_md": body,
        },
    )
    assert resp.status_code == 201, f"Failed to create thread: {resp.data}"
    return json.loads(resp.data)["thread_id"]


# ---------------------------------------------------------------------------
# Core search tests
# ---------------------------------------------------------------------------


class TestSearchEndpoint:
    """GET /search?q=<term> — happy path and edge cases."""

    def test_q_match_title_returns_thread(self, client):
        """A thread whose title matches q appears in results."""
        _post_thread(client, "agent-a", "inter-agent", "unique-title-xyz", "some body text")
        resp = client.get("/search?q=unique-title-xyz")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert "unique-title-xyz" in html

    def test_q_match_body_returns_thread(self, client):
        """A thread with a matching post body appears in results even if title doesn't match."""
        _post_thread(client, "agent-b", "cold-start", "Unrelated title", "distinctive-body-content-abc")
        resp = client.get("/search?q=distinctive-body-content-abc")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert "Unrelated title" in html

    def test_q_no_match_returns_empty_200(self, client):
        """A query that matches nothing returns 200 with no thread cards."""
        _post_thread(client, "agent-a", "inter-agent", "Something here", "body content here")
        resp = client.get("/search?q=zzz-no-match-zzz")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # Results list is empty; the no-match message appears
        assert "Something here" not in html

    def test_empty_q_returns_200(self, client):
        """Missing or empty q returns 200 (not 500) with no results."""
        _post_thread(client, "agent-a", "inter-agent", "Some thread", "body")
        resp = client.get("/search")
        assert resp.status_code == 200
        resp2 = client.get("/search?q=")
        assert resp2.status_code == 200

    def test_empty_q_does_not_show_threads(self, client):
        """Empty q renders the search page without listing any threads."""
        _post_thread(client, "agent-a", "inter-agent", "Some thread", "body")
        resp = client.get("/search?q=")
        html = resp.data.decode("utf-8")
        assert "Some thread" not in html

    def test_sql_injection_shape_returns_safely(self, client):
        """q=%' OR 1=1 -- is handled safely (parameterised; no 500)."""
        _post_thread(client, "agent-a", "inter-agent", "Normal thread", "body text")
        resp = client.get("/search?q=%25%27+OR+1%3D1+--")
        assert resp.status_code == 200
        # Must not return ALL threads (injection would match everything)
        html = resp.data.decode("utf-8")
        assert "Normal thread" not in html

    def test_html_in_q_renders_escaped(self, client):
        """User-supplied q containing HTML is escaped — no raw tag in output."""
        resp = client.get("/search?q=<script>alert(1)</script>")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # Raw <script> tag must NOT appear verbatim
        assert "<script>alert(1)</script>" not in html
        # The escaped form must appear (Jinja autoescape)
        assert "&lt;script&gt;" in html

    def test_search_results_link_to_thread(self, client):
        """Each result links to /thread/<id>."""
        tid = _post_thread(client, "agent-a", "inter-agent", "Link-test thread", "link body")
        resp = client.get("/search?q=Link-test")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert f"/thread/{tid}" in html

    def test_search_result_shows_category_chip(self, client):
        """Results include the category chip."""
        _post_thread(client, "agent-a", "cold-start", "Chip thread title", "body")
        resp = client.get("/search?q=Chip+thread+title")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # Category display_name appears somewhere in the result
        assert "Cold-start journals" in html  # full display_name — rename-detecting  # partial match on display_name prefix

    def test_nonmatching_thread_not_in_results(self, client):
        """Threads that do NOT match q are absent from the results."""
        _post_thread(client, "agent-a", "inter-agent", "Alpha thread", "alpha body")
        _post_thread(client, "agent-b", "cold-start", "Beta thread", "beta body")
        resp = client.get("/search?q=Alpha+thread")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert "Alpha thread" in html
        assert "Beta thread" not in html

    def test_search_page_has_form_action(self, client):
        """The search page renders the search form pointing at /search."""
        resp = client.get("/search")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert 'action="/search"' in html
