"""Tests for the per-thread HTML read-view (GET /thread/<id>).

Verifies:
- GET /thread/<valid_id> → 200 + contains thread title and post body HTML.
- GET /thread/<nonexistent_id> → 404.
- Index page (/) thread titles contain href="/thread/" links.
- Markdown in post body renders to HTML (e.g. **bold** → <strong>).

Fixtures mirror test_endpoints.py: Flask test client + seeded temp DB.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from forum.db import init_db
from forum.server import create_app


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


def _create_thread(client, title="Test thread", body="Hello **world**.", category="inter-agent"):
    """Helper: create a thread via the API and return its thread_id."""
    resp = client.post(
        "/api/post",
        json={
            "agent": "agent-a",
            "category_slug": category,
            "title": title,
            "body_md": body,
        },
    )
    assert resp.status_code == 201
    return json.loads(resp.data)["thread_id"]


class TestThreadReadView:
    def test_valid_thread_returns_200(self, client):
        tid = _create_thread(client)
        resp = client.get(f"/thread/{tid}")
        assert resp.status_code == 200

    def test_valid_thread_is_html(self, client):
        tid = _create_thread(client)
        resp = client.get(f"/thread/{tid}")
        assert "text/html" in resp.content_type

    def test_thread_title_in_response(self, client):
        tid = _create_thread(client, title="My great thread title")
        resp = client.get(f"/thread/{tid}")
        body = resp.data.decode("utf-8")
        assert "My great thread title" in body

    def test_post_body_rendered_as_html(self, client):
        """Markdown in post body must render to HTML in the read-view."""
        tid = _create_thread(client, body="This is **bold** text.")
        resp = client.get(f"/thread/{tid}")
        body = resp.data.decode("utf-8")
        assert "<strong>bold</strong>" in body

    def test_nonexistent_thread_returns_404(self, client):
        resp = client.get("/thread/99999")
        assert resp.status_code == 404

    def test_post_anchor_ids_present(self, client):
        """Each post must have an id="post-N" deep-link anchor."""
        tid = _create_thread(client)
        resp = client.get(f"/thread/{tid}")
        body = resp.data.decode("utf-8")
        assert 'id="post-' in body

    def test_back_link_to_index(self, client):
        """Read-view must contain a link back to /."""
        tid = _create_thread(client)
        resp = client.get(f"/thread/{tid}")
        body = resp.data.decode("utf-8")
        assert 'href="/"' in body

    def test_reply_post_body_html_present(self, client):
        """Replies are also rendered; reply body HTML appears in read-view."""
        tid = _create_thread(client, body="Original post.")
        client.post(
            "/api/post",
            json={"agent": "agent-b", "thread_id": tid, "body_md": "Reply with *em*."},
        )
        resp = client.get(f"/thread/{tid}")
        body = resp.data.decode("utf-8")
        assert "<em>em</em>" in body

    def test_multiple_posts_all_rendered(self, client):
        """All posts (OP + replies) appear in the read-view."""
        tid = _create_thread(client, body="OP body text.")
        client.post(
            "/api/post",
            json={"agent": "agent-b", "thread_id": tid, "body_md": "First reply text."},
        )
        client.post(
            "/api/post",
            json={"agent": "cipher", "thread_id": tid, "body_md": "Second reply text."},
        )
        resp = client.get(f"/thread/{tid}")
        body = resp.data.decode("utf-8")
        assert "OP body text." in body
        assert "First reply text." in body
        assert "Second reply text." in body


class TestIndexThreadLinks:
    def test_index_thread_titles_have_links(self, client):
        """Thread titles on the index page must link to /thread/<id>."""
        _create_thread(client, title="Linked thread title")
        resp = client.get("/")
        body = resp.data.decode("utf-8")
        assert 'href="/thread/' in body

    def test_index_link_points_to_correct_thread(self, client):
        """The /thread/ href on the index page matches the thread's actual id."""
        tid = _create_thread(client, title="Specific link test")
        resp = client.get("/")
        body = resp.data.decode("utf-8")
        assert f'href="/thread/{tid}"' in body
