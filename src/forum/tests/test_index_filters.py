"""Tests for GET / index route: category filter, sort param, open-questions view.

Covers slice 2 of #755 — category filter + sort params + open-questions view,
server-side rendering on the index route.

Uses Flask test_client against a tmp SQLite DB seeded with 3-4 threads so
no live server is required.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

import pytest

from forum.db import init_db
from forum.server import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path):
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
# Category filter: GET /?category=<slug>
# ---------------------------------------------------------------------------


class TestCategoryFilter:
    """The index route filters threads to the given category slug."""

    def test_category_filter_happy_path(self, client):
        """?category=inter-agent returns only inter-agent threads."""
        _post_thread(client, "agent-a", "inter-agent", "IA thread")
        _post_thread(client, "agent-b", "cold-start", "CS thread")

        resp = client.get("/?category=inter-agent")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")

        # inter-agent thread present
        assert "IA thread" in html
        # cold-start thread absent
        assert "CS thread" not in html

    def test_category_filter_unknown_slug_returns_empty_list(self, client):
        """Unknown category slug → 200 + empty thread list (not 500)."""
        _post_thread(client, "agent-a", "inter-agent", "Some thread")
        resp = client.get("/?category=does-not-exist")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # The thread should NOT appear
        assert "Some thread" not in html

    def test_category_filter_unknown_slug_still_renders_rail(self, client):
        """Unknown category slug → category rail still renders without error."""
        resp = client.get("/?category=totally-bogus")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # Rail heading still present
        assert "Categories" in html

    def test_category_filter_active_state(self, client):
        """Active category shows rail__item--active on the matching entry."""
        _post_thread(client, "agent-a", "cold-start", "CS thread")
        resp = client.get("/?category=cold-start")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # Discriminating: active class bound to the cold-start anchor itself.
        m = re.search(r'<a class="rail__item rail__item--active" href="/\?category=cold-start"', html)
        assert m, "cold-start rail entry is not the active one"

    def test_all_threads_active_when_no_category(self, client):
        """No ?category → 'All threads' entry carries rail__item--active."""
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        m = re.search(r'<a class="rail__item rail__item--active" href="/"', html)
        assert m, "All-threads rail entry is not the active one by default"

    def test_category_links_are_real_anchors(self, client):
        """Category rail items must be real <a href> links, not inert divs."""
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # The category route must be linked — at minimum one rail link
        assert "href=\"/?category=" in html

    def test_all_threads_link_is_real_anchor(self, client):
        """'All threads' must link to /."""
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # The "All threads" link must be an <a> with href="/"
        assert 'href="/"' in html


# ---------------------------------------------------------------------------
# Sort param: GET /?sort=hot|new|cited
# ---------------------------------------------------------------------------


class TestSortParam:
    """The sort param controls thread ordering; unknown values fall back to hot."""

    def test_sort_new_returns_200(self, client):
        _post_thread(client, "agent-a", "inter-agent", "T1")
        resp = client.get("/?sort=new")
        assert resp.status_code == 200

    def test_sort_hot_returns_200(self, client):
        _post_thread(client, "agent-a", "inter-agent", "T1")
        resp = client.get("/?sort=hot")
        assert resp.status_code == 200

    def test_sort_cited_returns_200(self, client):
        _post_thread(client, "agent-a", "inter-agent", "T1")
        resp = client.get("/?sort=cited")
        assert resp.status_code == 200

    def test_sort_unknown_returns_200(self, client):
        """Unknown sort value falls back to hot without erroring."""
        resp = client.get("/?sort=banana")
        assert resp.status_code == 200

    def test_sort_new_active_state(self, client):
        """?sort=new → 'New' button carries is-active class."""
        resp = client.get("/?sort=new")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # Discriminating assertion: the is-active class must be ON the New
        # anchor itself (class+href bound), not merely present in CSS.
        m = re.search(r'<a class="seg__btn is-active" href="[^"]*sort=new[^"]*">New</a>', html)
        assert m, "New anchor is not the active segment"

    def test_sort_hot_active_by_default(self, client):
        """No ?sort → 'Hot' carries is-active."""
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        m = re.search(r'<a class="seg__btn is-active" href="[^"]*sort=hot[^"]*">Hot</a>', html)
        assert m, "Hot anchor is not the active segment by default"

    def test_sort_new_orders_by_created_desc(self, client):
        """?sort=new: newer thread appears before older thread."""

        tid1 = _post_thread(client, "agent-a", "inter-agent", "First thread")
        time.sleep(0.05)
        tid2 = _post_thread(client, "agent-b", "inter-agent", "Second thread")

        resp = client.get("/?sort=new")
        html = resp.data.decode("utf-8")
        pos_first = html.find("First thread")
        pos_second = html.find("Second thread")
        assert pos_second < pos_first, (
            "?sort=new must place the more-recently-created thread first"
        )

    def test_sort_cited_orders_by_citations(self, client):
        """?sort=cited: thread with more ENGRAM cites appears first."""
        _post_thread(client, "agent-a", "inter-agent", "No cites", "plain body")
        _post_thread(client, "agent-b", "inter-agent", "Many cites", "OB 0001 OB 0002 OB 0003")

        resp = client.get("/?sort=cited")
        html = resp.data.decode("utf-8")
        pos_many = html.find("Many cites")
        pos_none = html.find("No cites")
        assert pos_many < pos_none, (
            "?sort=cited must place the thread with more citations first"
        )

    def test_sort_and_category_compose(self, client):
        """?category=X&sort=new composes correctly — only that category, newest first."""

        _post_thread(client, "agent-a", "cold-start", "CS thread")
        time.sleep(0.05)
        _post_thread(client, "agent-b", "inter-agent", "IA thread")

        resp = client.get("/?category=inter-agent&sort=new")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert "IA thread" in html
        assert "CS thread" not in html


# ---------------------------------------------------------------------------
# Open-questions view: GET /?view=open-questions
# ---------------------------------------------------------------------------


class TestOpenQuestionsView:
    """?view=open-questions: Q&A threads with unresolved=1."""

    def test_open_questions_returns_200(self, client):
        resp = client.get("/?view=open-questions")
        assert resp.status_code == 200

    def test_open_questions_shows_unresolved_qa_threads(self, client):
        """Unresolved Q&A thread appears in the open-questions view."""
        _post_thread(client, "agent-a", "q-and-a", "Open question")
        resp = client.get("/?view=open-questions")
        html = resp.data.decode("utf-8")
        assert "Open question" in html

    def test_open_questions_excludes_non_qa_threads(self, client):
        """Non-Q&A threads do NOT appear in the open-questions view."""
        _post_thread(client, "agent-a", "inter-agent", "IA thread")
        resp = client.get("/?view=open-questions")
        html = resp.data.decode("utf-8")
        assert "IA thread" not in html

    def test_open_questions_view_link_is_real_anchor(self, client):
        """'Open questions' must be a real <a href> link on the index page."""
        resp = client.get("/")
        html = resp.data.decode("utf-8")
        assert "href=\"/?view=open-questions\"" in html

    def test_open_questions_active_state(self, client):
        """?view=open-questions → the Open questions link carries rail__item--active."""
        resp = client.get("/?view=open-questions")
        html = resp.data.decode("utf-8")
        m = re.search(
            r'<a class="rail__item rail__item--active" href="/\?view=open-questions"', html
        )
        assert m, "open-questions view link is not the active rail entry"

    def test_open_questions_excludes_resolved_qa_threads(self, client):
        """Resolved Q&A thread (unresolved=0) must NOT appear in open-questions view."""
        # Create a Q&A thread (born unresolved=1)
        agent_a_tid = _post_thread(client, "agent-a", "q-and-a", "Resolved question")

        # Create a reply from agent-b so agent-a can accept it
        resp = client.post(
            "/api/post",
            json={
                "agent": "agent-b",
                "thread_id": agent_a_tid,
                "body_md": "Here is the answer",
            },
        )
        reply_post_id = json.loads(resp.data)["post_id"]

        # agent-a accepts the answer → unresolved → 0
        client.post(
            f"/api/thread/{agent_a_tid}/accept",
            json={"agent": "agent-a", "post_id": reply_post_id},
        )

        resp = client.get("/?view=open-questions")
        html = resp.data.decode("utf-8")
        assert "Resolved question" not in html

    def test_open_questions_view_wins_over_category_param(self, client):
        """?view=open-questions ignores ?category= — view wins."""
        _post_thread(client, "agent-a", "q-and-a", "Open question here")
        # Category=cold-start is passed alongside view=open-questions;
        # the q-and-a thread should still appear.
        resp = client.get("/?view=open-questions&category=cold-start")
        html = resp.data.decode("utf-8")
        assert "Open question here" in html

    def test_open_questions_empty_when_none_open(self, client):
        """No unresolved Q&A threads → index renders with empty thread list (200, no crash)."""
        # Only a non-QA thread
        _post_thread(client, "agent-a", "inter-agent", "IA thread")
        resp = client.get("/?view=open-questions")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert "IA thread" not in html


# ---------------------------------------------------------------------------
# Render-sanity: test_client requests through the full route
# ---------------------------------------------------------------------------


class TestIndexRenderSanity:
    """Sanity checks: 200 + expected ordering/filtering markers in HTML."""

    def test_bare_index_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.content_type

    def test_category_filter_200_with_real_slug(self, client):
        _post_thread(client, "agent-a", "cold-start", "CS thread")
        resp = client.get("/?category=cold-start")
        assert resp.status_code == 200

    def test_sort_new_200(self, client):
        resp = client.get("/?sort=new")
        assert resp.status_code == 200

    def test_open_questions_200(self, client):
        resp = client.get("/?view=open-questions")
        assert resp.status_code == 200

    def test_sort_links_present_on_index(self, client):
        """All three sort links are rendered on the bare index."""
        resp = client.get("/")
        html = resp.data.decode("utf-8")
        assert "sort=hot" in html
        assert "sort=new" in html
        assert "sort=cited" in html
