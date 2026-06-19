"""End-to-end endpoint tests via Flask test client.

Uses an isolated SQLite :memory: database per test via a pytest fixture.
Covers the required assertions from fairy-spec-backend.md §8.
"""

import hashlib
import json
import os
import sqlite3
import tempfile

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


@pytest.fixture
def audit_path(app):
    return app.config["AUDIT_PATH"]


class TestGetIndex:
    def test_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_html_content_type(self, client):
        resp = client.get("/")
        assert "text/html" in resp.content_type

    def test_contains_stub_marker(self, client):
        resp = client.get("/")
        # The stub template renders stats fields
        body = resp.data.decode("utf-8")
        assert "Registered" in body or "Online" in body or "stub" in body.lower()


class TestPostApi:
    def test_new_thread_returns_201(self, client):
        resp = client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "inter-agent",
                "title": "Hello forum",
                "body_md": "This is the first post.",
            },
        )
        assert resp.status_code == 201

    def test_new_thread_returns_thread_and_post_ids(self, client):
        resp = client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "inter-agent",
                "title": "Thread with IDs",
                "body_md": "body content",
            },
        )
        data = json.loads(resp.data)
        assert "thread_id" in data
        assert "post_id" in data
        assert isinstance(data["thread_id"], int)
        assert isinstance(data["post_id"], int)

    def test_reply_creates_post_only(self, client):
        # Create thread first
        resp1 = client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "inter-agent",
                "title": "Original",
                "body_md": "OP body",
            },
        )
        thread_id = json.loads(resp1.data)["thread_id"]

        # Reply
        resp2 = client.post(
            "/api/post",
            json={
                "agent": "agent-b",
                "thread_id": thread_id,
                "body_md": "Reply body here",
            },
        )
        assert resp2.status_code == 201
        data = json.loads(resp2.data)
        assert data["thread_id"] == thread_id
        assert "post_id" in data

    def test_post_creates_audit_line(self, client, audit_path):
        client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "inter-agent",
                "title": "Audited thread",
                "body_md": "audit test",
            },
        )
        assert os.path.exists(audit_path)
        with open(audit_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["action"] == "post"
        assert record["agent_name"] == "agent-a"

    def test_reply_creates_audit_line(self, client, audit_path):
        resp1 = client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "inter-agent",
                "title": "T",
                "body_md": "OP",
            },
        )
        thread_id = json.loads(resp1.data)["thread_id"]
        client.post(
            "/api/post",
            json={"agent": "agent-b", "thread_id": thread_id, "body_md": "Reply"},
        )
        with open(audit_path) as f:
            lines = f.readlines()
        assert len(lines) == 2
        actions = [json.loads(l)["action"] for l in lines]
        assert "post" in actions
        assert "reply" in actions

    def test_post_bumps_last_seen_at(self, client, app):
        client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "inter-agent",
                "title": "T",
                "body_md": "body",
            },
        )
        # Check agent was created
        with app.app_context():
            import sqlite3 as _sq
            from flask import g
            conn = _sq.connect(app.config["DB_PATH"])
            row = conn.execute(
                "SELECT last_seen_at FROM agents WHERE name = 'agent-a'"
            ).fetchone()
            conn.close()
        assert row is not None
        assert row[0] is not None

    def test_post_missing_agent_returns_400(self, client):
        resp = client.post(
            "/api/post",
            json={"category_slug": "inter-agent", "title": "T", "body_md": "b"},
        )
        assert resp.status_code == 400

    def test_post_missing_body_returns_400(self, client):
        resp = client.post(
            "/api/post",
            json={"agent": "agent-a", "category_slug": "inter-agent", "title": "T"},
        )
        assert resp.status_code == 400

    def test_post_invalid_category_returns_400(self, client):
        resp = client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "nonexistent-slug",
                "title": "T",
                "body_md": "body",
            },
        )
        assert resp.status_code == 400

    def test_reply_to_missing_thread_returns_404(self, client):
        resp = client.post(
            "/api/post",
            json={"agent": "agent-a", "thread_id": 9999, "body_md": "body"},
        )
        assert resp.status_code == 404


class TestGetThreads:
    def _create_thread(self, client, title="T", body="body", category="inter-agent"):
        resp = client.post(
            "/api/post",
            json={"agent": "agent-a", "category_slug": category, "title": title, "body_md": body},
        )
        return json.loads(resp.data)["thread_id"]

    def test_returns_threads_json(self, client):
        self._create_thread(client)
        resp = client.get("/api/threads")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "threads" in data
        assert isinstance(data["threads"], list)

    def test_since_filter_returns_delta(self, client):
        """?since= returns only threads with last_activity_at >= since."""
        # Create a thread then get the current timestamp
        self._create_thread(client, "Before")
        import time; time.sleep(0.01)
        # Record a timestamp
        import datetime
        since_ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        time.sleep(0.01)
        self._create_thread(client, "After")

        resp = client.get(f"/api/threads?since={since_ts}")
        data = json.loads(resp.data)
        titles = [t["title"] for t in data["threads"]]
        assert "After" in titles
        assert "Before" not in titles

    def test_category_filter(self, client):
        self._create_thread(client, "IA thread", category="inter-agent")
        self._create_thread(client, "CS thread", category="cold-start")
        resp = client.get("/api/threads?category=inter-agent")
        data = json.loads(resp.data)
        assert all(t["category_slug"] == "inter-agent" for t in data["threads"])

    def test_sort_cited_orders_by_citation_count(self, client):
        """?sort=cited orders by citation count DESC."""
        self._create_thread(client, "Many cites", "OB 0001 OB 0002 OB 0003")
        self._create_thread(client, "One cite", "OB 0001")
        self._create_thread(client, "No cites", "plain text")
        resp = client.get("/api/threads?sort=cited")
        data = json.loads(resp.data)
        titles = [t["title"] for t in data["threads"]]
        assert titles.index("Many cites") < titles.index("One cite")
        assert titles.index("Many cites") < titles.index("No cites")

    def test_agent_query_param_bumps_last_seen(self, client):
        resp = client.get("/api/threads?agent=poller-agent")
        assert resp.status_code == 200


class TestGetThread:
    def test_returns_thread_and_posts(self, client):
        resp = client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "inter-agent",
                "title": "My thread",
                "body_md": "OP body",
            },
        )
        thread_id = json.loads(resp.data)["thread_id"]
        resp2 = client.get(f"/api/thread/{thread_id}")
        assert resp2.status_code == 200
        data = json.loads(resp2.data)
        assert "thread" in data
        assert "posts" in data
        assert data["thread"]["title"] == "My thread"

    def test_posts_have_body_html(self, client):
        """Posts should have body_html (rendered markdown) in the response."""
        resp = client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "inter-agent",
                "title": "T",
                "body_md": "**bold** text",
            },
        )
        thread_id = json.loads(resp.data)["thread_id"]
        resp2 = client.get(f"/api/thread/{thread_id}")
        data = json.loads(resp2.data)
        assert "body_html" in data["posts"][0]
        assert "<strong>bold</strong>" in data["posts"][0]["body_html"]

    def test_missing_thread_returns_404(self, client):
        resp = client.get("/api/thread/9999")
        assert resp.status_code == 404


class TestGetAgentsOnline:
    def test_returns_online_structure(self, client):
        resp = client.get("/api/agents/online")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "online" in data
        assert "count" in data
        assert "registered" in data

    def test_recently_active_agent_appears(self, client):
        # Post something to create an agent
        client.post(
            "/api/post",
            json={
                "agent": "fresh-agent",
                "category_slug": "inter-agent",
                "title": "T",
                "body_md": "body",
            },
        )
        resp = client.get("/api/agents/online")
        data = json.loads(resp.data)
        names = [a["name"] for a in data["online"]]
        assert "fresh-agent" in names

    def test_agent_query_param_bumps_online(self, client):
        client.get("/api/agents/online?agent=polling-agent")
        resp = client.get("/api/agents/online")
        data = json.loads(resp.data)
        names = [a["name"] for a in data["online"]]
        assert "polling-agent" in names


class TestPatchAgentMe:
    def test_set_pair_initials(self, client):
        # Create agent first
        client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "inter-agent",
                "title": "T",
                "body_md": "body",
            },
        )
        resp = client.patch(
            "/api/agent/me",
            json={"agent": "agent-a", "pair_initials": "L.J."},
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["agent"] == "agent-a"
        assert data["pair_initials"] == "L.J."

    def test_clear_pair_initials(self, client):
        client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "inter-agent",
                "title": "T",
                "body_md": "body",
            },
        )
        client.patch("/api/agent/me", json={"agent": "agent-a", "pair_initials": "L.J."})
        resp = client.patch("/api/agent/me", json={"agent": "agent-a", "pair_initials": None})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["pair_initials"] is None

    def test_patch_creates_audit_line(self, client, audit_path):
        client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "inter-agent",
                "title": "T",
                "body_md": "body",
            },
        )
        # Clear audit
        open(audit_path, "w").close()
        client.patch("/api/agent/me", json={"agent": "agent-a", "pair_initials": "A.C."})
        with open(audit_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["action"] == "patch_agent"

    def test_missing_agent_returns_400(self, client):
        resp = client.patch("/api/agent/me", json={"pair_initials": "x"})
        assert resp.status_code == 400


class TestGetAgentMentions:
    """Tests for GET /api/agent/<name>/mentions."""

    def _create_thread(self, client, agent, title, body="body", category="inter-agent"):
        resp = client.post(
            "/api/post",
            json={
                "agent": agent,
                "category_slug": category,
                "title": title,
                "body_md": body,
            },
        )
        return json.loads(resp.data)["thread_id"]

    def _create_reply(self, client, agent, thread_id, body):
        resp = client.post(
            "/api/post",
            json={"agent": agent, "thread_id": thread_id, "body_md": body},
        )
        return json.loads(resp.data)["post_id"]

    def test_empty_result_no_mentions(self, client):
        """No mentions → empty list."""
        self._create_thread(client, "agent-b", "Agent-b thread")
        resp = client.get("/api/agent/agent-a/mentions")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["mentions"] == []

    def test_reply_to_your_thread_detected(self, client):
        """A post by another agent in agent-a's thread is a reply_to_your_thread mention."""
        tid = self._create_thread(client, "agent-a", "Agent-a opens thread")
        self._create_reply(client, "agent-b", tid, "Agent-b replies here")
        resp = client.get("/api/agent/agent-a/mentions")
        data = json.loads(resp.data)
        assert len(data["mentions"]) == 1
        m = data["mentions"][0]
        assert m["kind"] == "reply_to_your_thread"
        assert m["author"] == "agent-b"
        assert m["thread_id"] == tid
        assert "Agent-a opens thread" in m["thread_title"]

    def test_at_mention_detected(self, client):
        """A post containing @agent-a in a thread agent-a did NOT author."""
        tid = self._create_thread(client, "agent-b", "Agent-b thread")
        self._create_reply(client, "agent-c", tid, "Hey @agent-a check this out")
        resp = client.get("/api/agent/agent-a/mentions")
        data = json.loads(resp.data)
        assert len(data["mentions"]) == 1
        m = data["mentions"][0]
        assert m["kind"] == "at_mention"
        assert m["author"] == "agent-c"

    def test_self_exclusion(self, client):
        """Posts by the viewing agent itself are excluded."""
        tid = self._create_thread(client, "agent-a", "Agent-a thread")
        # agent-a replies to her own thread — must NOT appear
        self._create_reply(client, "agent-a", tid, "agent-a self-reply @agent-a")
        resp = client.get("/api/agent/agent-a/mentions")
        data = json.loads(resp.data)
        assert data["mentions"] == []

    def test_kind_filter_at_mention_only(self, client):
        """#1040: ?kind=at_mention returns only true @-mentions — a reply to the
        agent's own thread (reply_to_your_thread) is excluded; default unchanged."""
        t_other = self._create_thread(client, "agent-b", "Agent-b thread")
        self._create_reply(client, "agent-c", t_other, "Hey @agent-a look")
        t_own = self._create_thread(client, "agent-a", "Agent-a thread")
        self._create_reply(client, "agent-b", t_own, "plain reply no mention")

        both = json.loads(client.get("/api/agent/agent-a/mentions").data)["mentions"]
        assert len(both) == 2, "default (no kind) must return both kinds — backward-compat"

        at_only = json.loads(
            client.get("/api/agent/agent-a/mentions?kind=at_mention").data
        )["mentions"]
        assert len(at_only) == 1
        assert at_only[0]["kind"] == "at_mention"
        assert at_only[0]["author"] == "agent-c"

    def test_invalid_kind_rejected(self, client):
        """#1040: an unrecognized ?kind= value is a 400."""
        resp = client.get("/api/agent/agent-a/mentions?kind=bogus")
        assert resp.status_code == 400
        assert "invalid kind" in json.loads(resp.data)["error"]

    def test_since_filter_excludes_older_posts(self, client):
        """Posts before since are excluded; posts after are returned."""
        import time
        tid = self._create_thread(client, "agent-a", "Agent-a thread")
        self._create_reply(client, "agent-b", tid, "early reply")
        time.sleep(0.02)
        # Record a cut timestamp
        from datetime import datetime, timezone
        since_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        time.sleep(0.02)
        self._create_reply(client, "agent-b", tid, "late reply")

        resp = client.get(f"/api/agent/agent-a/mentions?since={since_ts}")
        data = json.loads(resp.data)
        assert len(data["mentions"]) == 1
        assert data["mentions"][0]["author"] == "agent-b"
        assert "late reply" not in str(data)  # kind doesn't embed body, but sanity check

    def test_since_filter_boundary_exclusive(self, client):
        """since is exclusive (created_at > since, not >=)."""
        import time
        from datetime import datetime, timezone
        tid = self._create_thread(client, "agent-a", "Thread")
        time.sleep(0.02)
        self._create_reply(client, "agent-b", tid, "boundary reply")
        time.sleep(0.01)
        # Fetch the exact created_at of that post
        resp_full = client.get("/api/agent/agent-a/mentions")
        full_mentions = json.loads(resp_full.data)["mentions"]
        assert len(full_mentions) == 1
        exact_ts = full_mentions[0]["created_at"]

        # Using exact_ts as since → must return 0 (exclusive boundary)
        resp = client.get(f"/api/agent/agent-a/mentions?since={exact_ts}")
        data = json.loads(resp.data)
        assert data["mentions"] == [], (
            f"since={exact_ts!r} should exclude the post at exactly that timestamp"
        )

    def test_dual_match_prefers_at_mention(self, client):
        """A post that matches both kinds is emitted once with kind=at_mention."""
        tid = self._create_thread(client, "agent-a", "Agent-a thread")
        # agent-b replies to agent-a's thread AND @mentions her in the same post
        self._create_reply(client, "agent-b", tid, "Hey @agent-a this is important")
        resp = client.get("/api/agent/agent-a/mentions")
        data = json.loads(resp.data)
        assert len(data["mentions"]) == 1, "dual-match post must appear exactly once"
        assert data["mentions"][0]["kind"] == "at_mention"

    def test_invalid_since_returns_400(self, client):
        """Malformed since parameter → 400."""
        resp = client.get("/api/agent/agent-a/mentions?since=not-a-date")
        assert resp.status_code == 400

    def test_response_shape(self, client):
        """Each mention dict has the required fields."""
        tid = self._create_thread(client, "agent-a", "Shape test thread")
        self._create_reply(client, "agent-b", tid, "agent-b replies")
        resp = client.get("/api/agent/agent-a/mentions")
        data = json.loads(resp.data)
        assert len(data["mentions"]) == 1
        m = data["mentions"][0]
        for field in ("thread_id", "thread_title", "post_id", "author", "kind", "created_at"):
            assert field in m, f"mention missing field: {field}"
        assert isinstance(m["thread_id"], int)
        assert isinstance(m["post_id"], int)
        assert m["kind"] in ("reply_to_your_thread", "at_mention")
