"""Tests for forum read-state v2.

Covers:
- reads table schema: created fresh + idempotent on existing DB.
- mark_thread_read: insert, upsert, MONOTONIC constraint.
- get_inbox: mention appear; unread-on-authored appear;
  CRITICAL: never-read authored thread case (no reads row → all other-authored
  posts unread, NOT empty); own posts never in inbox; read-up-to-N; dedup;
  thread not-posted-in-and-not-mentioned absent.
- /api/agent/<name>/inbox endpoint shape.
- /api/thread/<id>/read marks watermark + defaults to max when omitted.
- back-compat: /api/agent/<name>/mentions unchanged.
- CLI: forum read posts watermark server-side; forum status reflects server-side unread.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import os
from typing import Optional
from unittest.mock import patch

import pytest

from forum import db
from forum.db import (
    count_unread_all_threads,
    count_unread_by_category,
    create_reply,
    create_thread,
    get_inbox,
    init_db,
    mark_thread_read,
    upsert_agent,
)
from forum.server import create_app


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    c = sqlite3.connect(db_path)
    init_db(c)
    c.close()
    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(client, agent, title="T", body="body", category="inter-agent"):
    resp = client.post(
        "/api/post",
        json={"agent": agent, "category_slug": category, "title": title, "body_md": body},
    )
    assert resp.status_code == 201
    data = json.loads(resp.data)
    return data["thread_id"], data["post_id"]


def _reply(client, agent, thread_id, body="reply"):
    resp = client.post(
        "/api/post",
        json={"agent": agent, "thread_id": thread_id, "body_md": body},
    )
    assert resp.status_code == 201
    return json.loads(resp.data)["post_id"]


# ---------------------------------------------------------------------------
# Schema: reads table
# ---------------------------------------------------------------------------

class TestReadsTableSchema:
    def test_reads_table_created(self, conn):
        """reads table exists after init_db."""
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "reads" in tables

    def test_reads_table_columns(self, conn):
        """reads table has the four required columns."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reads)").fetchall()}
        assert {"agent_id", "thread_id", "last_read_post_id", "updated_at"} == cols

    def test_reads_table_primary_key(self, conn):
        """PRIMARY KEY (agent_id, thread_id) — duplicate upsert does not error."""
        aid = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, aid, "inter-agent", "T", "body")
        # First insert
        mark_thread_read(conn, aid, tid, 1)
        # Second insert (same PK) — must not raise
        mark_thread_read(conn, aid, tid, 2)
        row = conn.execute(
            "SELECT last_read_post_id FROM reads WHERE agent_id = ? AND thread_id = ?",
            (aid, tid),
        ).fetchone()
        assert row[0] == 2

    def test_init_db_idempotent_with_reads(self, conn):
        """Re-running init_db does not raise even when reads table exists."""
        aid = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, aid, "inter-agent", "T", "body")
        mark_thread_read(conn, aid, tid, 1)
        init_db(conn)  # second call — must be no-op
        row = conn.execute(
            "SELECT last_read_post_id FROM reads WHERE agent_id = ? AND thread_id = ?",
            (aid, tid),
        ).fetchone()
        # Row still present and unchanged
        assert row is not None
        assert row[0] == 1


# ---------------------------------------------------------------------------
# mark_thread_read
# ---------------------------------------------------------------------------

class TestMarkThreadRead:
    def test_insert_creates_row(self, conn):
        aid = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, aid, "inter-agent", "T", "body")
        # No row yet
        assert conn.execute(
            "SELECT COUNT(*) FROM reads WHERE agent_id = ? AND thread_id = ?",
            (aid, tid),
        ).fetchone()[0] == 0

        mark_thread_read(conn, aid, tid, 5)
        row = conn.execute(
            "SELECT last_read_post_id FROM reads WHERE agent_id = ? AND thread_id = ?",
            (aid, tid),
        ).fetchone()
        assert row is not None
        assert row[0] == 5

    def test_upsert_advances_watermark(self, conn):
        aid = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, aid, "inter-agent", "T", "body")
        mark_thread_read(conn, aid, tid, 5)
        mark_thread_read(conn, aid, tid, 10)
        row = conn.execute(
            "SELECT last_read_post_id FROM reads WHERE agent_id = ? AND thread_id = ?",
            (aid, tid),
        ).fetchone()
        assert row[0] == 10

    def test_monotonic_lower_value_does_not_retreat(self, conn):
        """A lower new value must NOT retreat the watermark."""
        aid = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, aid, "inter-agent", "T", "body")
        mark_thread_read(conn, aid, tid, 10)
        # Attempt to retreat
        mark_thread_read(conn, aid, tid, 3)
        row = conn.execute(
            "SELECT last_read_post_id FROM reads WHERE agent_id = ? AND thread_id = ?",
            (aid, tid),
        ).fetchone()
        assert row[0] == 10, "Watermark must not retreat from 10 to 3"

    def test_same_value_is_idempotent(self, conn):
        """Marking read with the same value is a no-op (watermark stays)."""
        aid = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, aid, "inter-agent", "T", "body")
        mark_thread_read(conn, aid, tid, 7)
        mark_thread_read(conn, aid, tid, 7)
        row = conn.execute(
            "SELECT last_read_post_id FROM reads WHERE agent_id = ? AND thread_id = ?",
            (aid, tid),
        ).fetchone()
        assert row[0] == 7

    def test_updated_at_is_set(self, conn):
        """updated_at is set on insert."""
        aid = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, aid, "inter-agent", "T", "body")
        mark_thread_read(conn, aid, tid, 1)
        row = conn.execute(
            "SELECT updated_at FROM reads WHERE agent_id = ? AND thread_id = ?",
            (aid, tid),
        ).fetchone()
        assert row is not None
        assert row[0] is not None
        assert "T" in row[0]  # ISO format contains T separator


# ---------------------------------------------------------------------------
# get_inbox
# ---------------------------------------------------------------------------

class TestGetInbox:
    """Comprehensive tests for db.get_inbox()."""

    def _make_thread(self, conn, author, title="Thread", body="body"):
        aid = upsert_agent(conn, author)
        tid, _ = create_thread(conn, aid, "inter-agent", title, body)
        return tid, aid

    def _make_reply(self, conn, author, thread_id, body="reply"):
        aid = upsert_agent(conn, author)
        pid = create_reply(conn, aid, thread_id, body)
        return pid, aid

    def test_empty_inbox_when_nothing(self, conn):
        aid = upsert_agent(conn, "agent-a")
        result = get_inbox(conn, aid)
        assert result == []

    def test_mention_appears_in_inbox(self, conn):
        """@agent-a in a post → at_mention in inbox."""
        _, agent_b_id = self._make_thread(conn, "agent-b", "agent-b thread")
        agent_a_id = upsert_agent(conn, "agent-a")
        create_reply(conn, agent_b_id, 1, "Hey @agent-a check this")
        result = get_inbox(conn, agent_a_id)
        assert len(result) == 1
        assert result[0]["kind"] == "at_mention"

    def test_unread_on_authored_thread_appears(self, conn):
        """Reply to agent-a's own thread (no reads row) → in inbox."""
        _, agent_a_id = self._make_thread(conn, "agent-a", "agent-a thread")
        _, agent_b_id = upsert_agent(conn, "agent-b"), None
        agent_b_id = upsert_agent(conn, "agent-b")
        tid = conn.execute("SELECT id FROM threads LIMIT 1").fetchone()[0]
        create_reply(conn, agent_b_id, tid, "agent-b reply")
        result = get_inbox(conn, agent_a_id)
        assert len(result) == 1
        assert result[0]["kind"] == "reply_on_my_thread"
        assert result[0]["author"] == "agent-b"

    def test_never_read_authored_thread_all_posts_unread(self, conn):
        """CRITICAL: a thread agent-a authored but never read has NO reads row.
        COALESCE→0 must make all other-authored posts unread, NOT empty.
        """
        agent_a_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, agent_a_id, "inter-agent", "agent-a question", "OP")
        agent_b_id = upsert_agent(conn, "agent-b")
        agent_c_id = upsert_agent(conn, "agent-c")
        create_reply(conn, agent_b_id, tid, "agent-b answer 1")
        create_reply(conn, agent_c_id, tid, "agent-c answer 2")

        # No reads row exists for agent-a on this thread
        reads_count = conn.execute(
            "SELECT COUNT(*) FROM reads WHERE agent_id = ?", (agent_a_id,)
        ).fetchone()[0]
        assert reads_count == 0

        inbox = get_inbox(conn, agent_a_id)
        assert len(inbox) == 2, (
            "A thread agent-a authored but never read must surface ALL "
            "other-authored posts — NOT empty. COALESCE→0 is load-bearing."
        )

    def test_own_posts_never_in_inbox(self, conn):
        """agent-a's own posts are excluded from her inbox."""
        agent_a_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, agent_a_id, "inter-agent", "T", "OP")
        # agent-a replies to her own thread
        create_reply(conn, agent_a_id, tid, "agent-a self reply")
        inbox = get_inbox(conn, agent_a_id)
        assert inbox == [], "agent-a's own posts must never appear in her inbox"

    def test_read_up_to_n_only_gt_n_appear(self, conn):
        """After marking thread read at post N, only posts with id > N appear."""
        agent_a_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, agent_a_id, "inter-agent", "T", "OP")
        agent_b_id = upsert_agent(conn, "agent-b")
        pid1 = create_reply(conn, agent_b_id, tid, "reply 1")
        pid2 = create_reply(conn, agent_b_id, tid, "reply 2")
        pid3 = create_reply(conn, agent_b_id, tid, "reply 3")

        # Mark read up to pid2
        mark_thread_read(conn, agent_a_id, tid, pid2)
        inbox = get_inbox(conn, agent_a_id)

        post_ids = {item["post_id"] for item in inbox}
        assert pid1 not in post_ids, "pid1 is before watermark — should not appear"
        assert pid2 not in post_ids, "pid2 is at watermark — should not appear"
        assert pid3 in post_ids, "pid3 is after watermark — must appear"

    def test_dedup_mention_on_own_thread_appears_once(self, conn):
        """A post that is both a reply-on-authored-thread and an @mention
        must appear exactly ONCE, with kind='at_mention'."""
        agent_a_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, agent_a_id, "inter-agent", "agent-a thread", "OP")
        agent_b_id = upsert_agent(conn, "agent-b")
        # agent-b replies to agent-a's thread AND @mentions her
        create_reply(conn, agent_b_id, tid, "Hey @agent-a, here's my answer")
        inbox = get_inbox(conn, agent_a_id)
        assert len(inbox) == 1, "Dual-match post must appear exactly once"
        assert inbox[0]["kind"] == "at_mention", "Dual-match must prefer at_mention"

    def test_thread_not_posted_in_and_not_mentioned_absent(self, conn):
        """A thread agent-a never posted in and was not mentioned in → absent."""
        agent_b_id = upsert_agent(conn, "agent-b")
        tid, _ = create_thread(conn, agent_b_id, "inter-agent", "agent-b thread", "OP")
        agent_c_id = upsert_agent(conn, "agent-c")
        create_reply(conn, agent_c_id, tid, "agent-c reply — no mention of agent-a")
        agent_a_id = upsert_agent(conn, "agent-a")
        inbox = get_inbox(conn, agent_a_id)
        assert inbox == [], (
            "A thread agent-a never posted in and was not @mentioned in "
            "must not appear in her inbox"
        )

    def test_inbox_result_fields(self, conn):
        """Each inbox item has the required fields."""
        agent_a_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, agent_a_id, "inter-agent", "Field test", "OP")
        agent_b_id = upsert_agent(conn, "agent-b")
        create_reply(conn, agent_b_id, tid, "agent-b reply")
        inbox = get_inbox(conn, agent_a_id)
        assert len(inbox) == 1
        item = inbox[0]
        for field in ("post_id", "thread_id", "thread_title", "author", "kind", "created_at"):
            assert field in item, f"inbox item missing field: {field}"
        assert isinstance(item["post_id"], int)
        assert isinstance(item["thread_id"], int)
        assert item["kind"] in ("reply_on_my_thread", "at_mention")

    def test_at_mention_in_foreign_thread_appears(self, conn):
        """@agent-a in a thread agent-a did NOT post in → at_mention in inbox."""
        agent_b_id = upsert_agent(conn, "agent-b")
        tid, _ = create_thread(conn, agent_b_id, "inter-agent", "agent-b thread", "OP")
        agent_c_id = upsert_agent(conn, "agent-c")
        create_reply(conn, agent_c_id, tid, "Hey @agent-a, see agent-b thread")
        agent_a_id = upsert_agent(conn, "agent-a")
        inbox = get_inbox(conn, agent_a_id)
        assert len(inbox) == 1
        assert inbox[0]["kind"] == "at_mention"

    def test_at_mention_clears_after_reading_foreign_thread(self, conn):
        """A @mention is watermark-filtered like any unread post: once agent-a
        reads the foreign thread past the mentioning post, the mention clears
        from the inbox (clearable-inbox intent — a mention is not sticky-forever).
        """
        agent_b_id = upsert_agent(conn, "agent-b")
        tid, _ = create_thread(conn, agent_b_id, "inter-agent", "agent-b thread", "OP")
        agent_c_id = upsert_agent(conn, "agent-c")
        mention_pid = create_reply(conn, agent_c_id, tid, "Hey @agent-a, see this")
        agent_a_id = upsert_agent(conn, "agent-a")
        # Before reading: mention present.
        assert len(get_inbox(conn, agent_a_id)) == 1
        # Read the thread past the mention → it clears.
        mark_thread_read(conn, agent_a_id, tid, mention_pid)
        assert get_inbox(conn, agent_a_id) == []

    def test_fully_read_thread_not_in_inbox(self, conn):
        """After marking thread read at the last post, nothing appears in inbox."""
        agent_a_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, agent_a_id, "inter-agent", "T", "OP")
        agent_b_id = upsert_agent(conn, "agent-b")
        pid = create_reply(conn, agent_b_id, tid, "reply")
        mark_thread_read(conn, agent_a_id, tid, pid)
        inbox = get_inbox(conn, agent_a_id)
        assert inbox == []


# ---------------------------------------------------------------------------
# count_unread_all_threads — the all-threads count (wider than inbox; the
# accurate "N total" that replaces the old time-cursor tally, #679)
# ---------------------------------------------------------------------------

class TestCountUnreadAllThreads:
    def test_counts_thread_agent_never_posted_in(self, conn):
        """THE distinguishing case vs get_inbox: a thread agent-a has NOT
        posted in (not authored, not replied, not mentioned) still counts
        toward unread_all — the all-threads total is wider than the inbox."""
        agent_b_id = upsert_agent(conn, "agent-b")
        agent_a_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, agent_b_id, "inter-agent", "agent-b-only thread", "OP")
        create_reply(conn, agent_b_id, tid, "agent-b reply")
        # agent-a never touched this thread → NOT in her inbox...
        assert get_inbox(conn, agent_a_id) == []
        # ...but it DOES count toward her all-threads unread (OP + reply = 2).
        assert count_unread_all_threads(conn, agent_a_id) == 2

    def test_excludes_own_posts(self, conn):
        agent_a_id = upsert_agent(conn, "agent-a")
        create_thread(conn, agent_a_id, "inter-agent", "T", "my OP")
        assert count_unread_all_threads(conn, agent_a_id) == 0

    def test_respects_watermark(self, conn):
        agent_b_id = upsert_agent(conn, "agent-b")
        agent_a_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, agent_b_id, "inter-agent", "T", "OP")
        p2 = create_reply(conn, agent_b_id, tid, "r2")
        create_reply(conn, agent_b_id, tid, "r3")
        assert count_unread_all_threads(conn, agent_a_id) == 3   # OP+r2+r3
        mark_thread_read(conn, agent_a_id, tid, p2)
        assert count_unread_all_threads(conn, agent_a_id) == 1   # only r3 > watermark

    def test_at_least_inbox_authored_count(self, conn):
        """all-threads count is a superset of the authored-thread unread."""
        agent_a_id = upsert_agent(conn, "agent-a")
        a_tid, _ = create_thread(conn, agent_a_id, "inter-agent", "Ari thread", "OP")
        agent_b_id = upsert_agent(conn, "agent-b")
        create_reply(conn, agent_b_id, a_tid, "reply on ari's thread")
        b_tid, _ = create_thread(conn, agent_b_id, "inter-agent", "agent-b thread", "OP")
        # 1 unread on ari's authored thread + 1 OP on agent-b's = 2 all-threads;
        # inbox (authored∪mentions) = 1.
        assert count_unread_all_threads(conn, agent_a_id) == 2
        assert len(get_inbox(conn, agent_a_id)) == 1


# ---------------------------------------------------------------------------
# Endpoint: /api/agent/<name>/inbox
# ---------------------------------------------------------------------------

class TestInboxEndpoint:
    def test_inbox_returns_200_and_inbox_key(self, client):
        resp = client.get("/api/agent/agent-a/inbox")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "inbox" in data
        assert isinstance(data["inbox"], list)

    def test_inbox_empty_when_no_activity(self, client):
        resp = client.get("/api/agent/agent-a/inbox")
        data = json.loads(resp.data)
        assert data["inbox"] == []

    def test_inbox_includes_unread_all_field(self, client):
        """The inbox response carries unread_all (all-threads count) alongside
        the inbox set — wider than inbox (counts threads agent-a isn't in)."""
        tid, _ = _post(client, "agent-b", "agent-b-only thread")
        _reply(client, "agent-b", tid, "agent-b reply")
        resp = client.get("/api/agent/agent-a/inbox")
        data = json.loads(resp.data)
        assert "unread_all" in data
        assert data["inbox"] == []          # agent-a not in this thread
        assert data["unread_all"] == 2       # ...but OP + reply count toward total

    def test_inbox_contains_reply_on_my_thread(self, client):
        tid, _ = _post(client, "agent-a", "agent-a thread")
        _reply(client, "agent-b", tid, "agent-b reply")
        resp = client.get("/api/agent/agent-a/inbox")
        data = json.loads(resp.data)
        assert len(data["inbox"]) == 1
        assert data["inbox"][0]["kind"] == "reply_on_my_thread"
        assert data["inbox"][0]["author"] == "agent-b"

    def test_inbox_contains_at_mention(self, client):
        tid, _ = _post(client, "agent-b", "agent-b thread")
        _reply(client, "agent-c", tid, "Hey @agent-a check this")
        resp = client.get("/api/agent/agent-a/inbox")
        data = json.loads(resp.data)
        assert len(data["inbox"]) == 1
        assert data["inbox"][0]["kind"] == "at_mention"

    def test_inbox_item_shape(self, client):
        tid, _ = _post(client, "agent-a", "Shape test")
        _reply(client, "agent-b", tid, "reply")
        resp = client.get("/api/agent/agent-a/inbox")
        data = json.loads(resp.data)
        assert len(data["inbox"]) == 1
        item = data["inbox"][0]
        for field in ("post_id", "thread_id", "thread_title", "author", "kind", "created_at"):
            assert field in item, f"inbox item missing field: {field}"

    def test_inbox_clears_after_mark_read(self, client):
        tid, _ = _post(client, "agent-a", "Thread")
        pid = _reply(client, "agent-b", tid, "reply")
        # Mark thread read
        resp_read = client.post(
            f"/api/thread/{tid}/read",
            json={"agent": "agent-a", "last_read_post_id": pid},
        )
        assert resp_read.status_code == 200
        # Inbox must now be empty
        resp = client.get("/api/agent/agent-a/inbox")
        data = json.loads(resp.data)
        assert data["inbox"] == []


# ---------------------------------------------------------------------------
# Endpoint: /api/thread/<id>/read
# ---------------------------------------------------------------------------

class TestThreadReadEndpoint:
    def test_returns_200_and_watermark(self, client):
        tid, _ = _post(client, "agent-a", "T")
        pid = _reply(client, "agent-b", tid, "reply")
        resp = client.post(
            f"/api/thread/{tid}/read",
            json={"agent": "agent-a", "last_read_post_id": pid},
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["thread_id"] == tid
        assert data["agent"] == "agent-a"
        assert data["last_read_post_id"] == pid

    def test_defaults_to_max_post_id_when_omitted(self, client):
        tid, op_pid = _post(client, "agent-a", "T")
        r1 = _reply(client, "agent-b", tid, "reply 1")
        r2 = _reply(client, "agent-b", tid, "reply 2")
        resp = client.post(
            f"/api/thread/{tid}/read",
            json={"agent": "agent-a"},  # no last_read_post_id
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["last_read_post_id"] == r2, (
            "When last_read_post_id is omitted, must default to MAX(posts.id)"
        )

    def test_watermark_is_monotonic_via_endpoint(self, client):
        """POSTing a lower watermark via the endpoint does not retreat it."""
        tid, _ = _post(client, "agent-a", "T")
        r1 = _reply(client, "agent-b", tid, "r1")
        r2 = _reply(client, "agent-b", tid, "r2")
        # Set to r2 first
        client.post(
            f"/api/thread/{tid}/read",
            json={"agent": "agent-a", "last_read_post_id": r2},
        )
        # Try to retreat to r1
        resp = client.post(
            f"/api/thread/{tid}/read",
            json={"agent": "agent-a", "last_read_post_id": r1},
        )
        data = json.loads(resp.data)
        assert data["last_read_post_id"] == r2, (
            "Watermark must not retreat from r2 to r1"
        )

    def test_missing_agent_returns_400(self, client):
        tid, _ = _post(client, "agent-a", "T")
        resp = client.post(f"/api/thread/{tid}/read", json={})
        assert resp.status_code == 400

    def test_missing_thread_returns_404(self, client):
        resp = client.post(
            "/api/thread/99999/read",
            json={"agent": "agent-a"},
        )
        assert resp.status_code == 404

    def test_mark_read_makes_thread_absent_from_inbox(self, client):
        """After marking all read, the thread's posts no longer appear in inbox."""
        tid, _ = _post(client, "agent-a", "Thread")
        r1 = _reply(client, "agent-b", tid, "reply 1")
        r2 = _reply(client, "agent-b", tid, "reply 2")

        # Mark all read (omit last_read_post_id → defaults to max)
        client.post(f"/api/thread/{tid}/read", json={"agent": "agent-a"})

        resp = client.get("/api/agent/agent-a/inbox")
        data = json.loads(resp.data)
        inbox_thread_ids = {item["thread_id"] for item in data["inbox"]}
        assert tid not in inbox_thread_ids


# ---------------------------------------------------------------------------
# Back-compat: /api/agent/<name>/mentions unchanged
# ---------------------------------------------------------------------------

class TestMentionsBackCompat:
    def test_mentions_endpoint_still_works(self, client):
        tid, _ = _post(client, "agent-a", "agent-a thread")
        _reply(client, "agent-b", tid, "agent-b reply")
        resp = client.get("/api/agent/agent-a/mentions")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "mentions" in data
        assert isinstance(data["mentions"], list)

    def test_mentions_at_mention_kind_unchanged(self, client):
        tid, _ = _post(client, "agent-b", "agent-b thread")
        _reply(client, "agent-c", tid, "Hey @agent-a")
        resp = client.get("/api/agent/agent-a/mentions")
        data = json.loads(resp.data)
        assert len(data["mentions"]) == 1
        assert data["mentions"][0]["kind"] == "at_mention"

    def test_mentions_reply_to_your_thread_kind_unchanged(self, client):
        tid, _ = _post(client, "agent-a", "agent-a thread")
        _reply(client, "agent-b", tid, "plain reply")
        resp = client.get("/api/agent/agent-a/mentions")
        data = json.loads(resp.data)
        assert len(data["mentions"]) == 1
        assert data["mentions"][0]["kind"] == "reply_to_your_thread"

    def test_mentions_since_filter_unchanged(self, client):
        import time
        from datetime import datetime, timezone
        tid, _ = _post(client, "agent-a", "Thread")
        _reply(client, "agent-b", tid, "early")
        time.sleep(0.02)
        since_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        time.sleep(0.02)
        _reply(client, "agent-b", tid, "late")
        resp = client.get(f"/api/agent/agent-a/mentions?since={since_ts}")
        data = json.loads(resp.data)
        assert len(data["mentions"]) == 1

    def test_mentions_fields_unchanged(self, client):
        tid, _ = _post(client, "agent-a", "Thread")
        _reply(client, "agent-b", tid, "reply")
        resp = client.get("/api/agent/agent-a/mentions")
        data = json.loads(resp.data)
        m = data["mentions"][0]
        for field in ("thread_id", "thread_title", "post_id", "author", "kind", "created_at"):
            assert field in m, f"mentions missing field: {field}"


# ---------------------------------------------------------------------------
# CLI: forum read posts watermark server-side
# ---------------------------------------------------------------------------

class TestCLIForumRead:
    """Test that forum read CLI posts the watermark to the server after fetching."""

    def test_read_posts_watermark(self, app):
        """forum read must POST to /api/thread/<id>/read after displaying the thread."""
        import tools.forum as forum_cli

        # Seed the DB with a thread and a reply
        with app.app_context():
            import sqlite3 as _sq
            conn = _sq.connect(app.config["DB_PATH"])
            conn.row_factory = _sq.Row
            conn.execute("PRAGMA foreign_keys = ON")
            aid = upsert_agent(conn, "agent-a")
            tid, op_pid = create_thread(conn, aid, "inter-agent", "CLI read test", "OP body")
            agent_b_id = upsert_agent(conn, "agent-b")
            reply_pid = create_reply(conn, agent_b_id, tid, "agent-b reply")
            conn.close()

        # Capture the API calls made by the CLI
        calls = []

        def fake_do_request(req, url):
            calls.append((req.get_method(), url))
            if req.get_method() == "GET":
                # Return thread + posts
                return {
                    "thread": {
                        "id": tid,
                        "title": "CLI read test",
                        "category_slug": "inter-agent",
                        "author": {"name": "agent-a", "avatar_seed": "agent-a", "pair_initials": None},
                        "pinned": False,
                        "unresolved": False,
                        "created_at": "2026-01-01T00:00:00Z",
                        "last_activity_at": "2026-01-01T00:01:00Z",
                        "last_activity_agent": "agent-b",
                        "reply_count": 1,
                    },
                    "posts": [
                        {
                            "id": op_pid,
                            "author": {"name": "agent-a", "avatar_seed": "agent-a", "pair_initials": None},
                            "body_md": "OP body",
                            "created_at": "2026-01-01T00:00:00Z",
                            "edited_at": None,
                            "citation_count": 0,
                            "verifications": [],
                        },
                        {
                            "id": reply_pid,
                            "author": {"name": "agent-b", "avatar_seed": "agent-b", "pair_initials": None},
                            "body_md": "agent-b reply",
                            "created_at": "2026-01-01T00:01:00Z",
                            "edited_at": None,
                            "citation_count": 0,
                            "verifications": [],
                        },
                    ],
                }
            else:
                # POST /api/thread/<id>/read
                return {"thread_id": tid, "agent": "agent-a", "last_read_post_id": reply_pid}

        import argparse
        args = argparse.Namespace(thread_id=tid, format="human")
        config = {}

        with patch.object(forum_cli, "_do_request", side_effect=fake_do_request), \
             patch.object(forum_cli, "_FORUM_URL_CACHE", "http://localhost:5002"):
            forum_cli.cmd_read(args, config, "agent-a")

        methods = [m for m, _url in calls]
        assert "POST" in methods, (
            "forum read must POST the watermark to /api/thread/<id>/read after fetching"
        )
        post_urls = [url for m, url in calls if m == "POST"]
        assert any(f"/api/thread/{tid}/read" in url for url in post_urls), (
            f"POST must target /api/thread/{tid}/read; got: {post_urls}"
        )

    def test_read_does_not_write_local_cursor(self, app, tmp_path):
        """forum read must NOT write the local cursor file (deprecated in v2)."""
        import tools.forum as forum_cli
        import io

        aid = None
        with app.app_context():
            import sqlite3 as _sq
            conn = _sq.connect(app.config["DB_PATH"])
            conn.row_factory = _sq.Row
            conn.execute("PRAGMA foreign_keys = ON")
            aid = upsert_agent(conn, "agent-a")
            tid, op_pid = create_thread(conn, aid, "inter-agent", "T", "body")
            conn.close()

        cursor_file = tmp_path / "forum-read-cursor.txt"
        assert not cursor_file.exists(), "Cursor file must not exist before the test"

        def fake_do_request(req, url):
            if req.get_method() == "GET":
                return {
                    "thread": {
                        "id": tid, "title": "T", "category_slug": "inter-agent",
                        "author": {"name": "agent-a", "avatar_seed": "agent-a", "pair_initials": None},
                        "pinned": False, "unresolved": False,
                        "created_at": "2026-01-01T00:00:00Z",
                        "last_activity_at": "2026-01-01T00:00:00Z",
                        "last_activity_agent": "agent-a",
                        "reply_count": 0,
                    },
                    "posts": [{"id": op_pid, "author": {"name": "agent-a", "avatar_seed": "agent-a",
                                                         "pair_initials": None},
                                "body_md": "body", "created_at": "2026-01-01T00:00:00Z",
                                "edited_at": None, "citation_count": 0, "verifications": []}],
                }
            return {"thread_id": tid, "agent": "agent-a", "last_read_post_id": op_pid}

        import argparse
        args = argparse.Namespace(thread_id=tid, format="human")

        # Override ENGRAM_HOME so any cursor write would land in tmp_path
        with patch.object(forum_cli, "_do_request", side_effect=fake_do_request), \
             patch.object(forum_cli, "_FORUM_URL_CACHE", "http://localhost:5002"), \
             patch.object(forum_cli, "READ_CURSOR_PATH", str(cursor_file)):
            forum_cli.cmd_read(args, {}, "agent-a")

        assert not cursor_file.exists(), (
            "forum read must NOT write the local cursor file (deprecated in v2)"
        )


# ---------------------------------------------------------------------------
# CLI: forum status reflects server-side unread
# ---------------------------------------------------------------------------

class TestCLIForumStatus:
    """Test that forum status fetches from the server inbox endpoint."""

    def test_status_uses_inbox_endpoint(self):
        """forum status must call /api/agent/<name>/inbox."""
        import tools.forum as forum_cli

        calls = []

        def fake_do_request(req, url):
            calls.append(url)
            if "/api/agents/online" in url:
                return {"online": [], "count": 0, "registered": 1}
            if "/inbox" in url:
                return {"inbox": [
                    {
                        "post_id": 5,
                        "thread_id": 1,
                        "thread_title": "T",
                        "author": "agent-b",
                        "kind": "reply_on_my_thread",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ]}
            return {}

        import argparse, io, contextlib
        args = argparse.Namespace(format="human", ack=False)

        buf = io.StringIO()
        with patch.object(forum_cli, "_do_request", side_effect=fake_do_request), \
             patch.object(forum_cli, "_FORUM_URL_CACHE", "http://localhost:5002"), \
             contextlib.redirect_stdout(buf):
            forum_cli.cmd_status(args, {}, "agent-a")

        output = buf.getvalue()
        # Must have fetched inbox
        assert any("/inbox" in url for url in calls), (
            f"forum status must call /api/agent/<name>/inbox; calls: {calls}"
        )

    def test_status_shows_unread_count(self):
        """forum status output includes unread count from server inbox."""
        import tools.forum as forum_cli

        def fake_do_request(req, url):
            if "/api/agents/online" in url:
                return {"online": [], "count": 2, "registered": 5}
            if "/inbox" in url:
                return {"inbox": [
                    {"post_id": 1, "thread_id": 1, "thread_title": "T",
                     "author": "agent-b", "kind": "reply_on_my_thread",
                     "created_at": "2026-01-01T00:00:00Z"},
                    {"post_id": 2, "thread_id": 2, "thread_title": "T2",
                     "author": "agent-c", "kind": "at_mention",
                     "created_at": "2026-01-01T00:01:00Z"},
                ]}
            return {}

        import argparse, io, contextlib
        args = argparse.Namespace(format="human", ack=False)

        buf = io.StringIO()
        with patch.object(forum_cli, "_do_request", side_effect=fake_do_request), \
             patch.object(forum_cli, "_FORUM_URL_CACHE", "http://localhost:5002"), \
             contextlib.redirect_stdout(buf):
            forum_cli.cmd_status(args, {}, "agent-a")

        output = buf.getvalue()
        # Total unread: 2 inbox items
        assert "2" in output, f"Expected unread count 2 in output: {output!r}"
        # Authored-thread replies: 1; at-mentions: 1
        assert "1" in output
        # Online count
        assert "2" in output

    def test_status_json_format_has_inbox_fields(self):
        """forum status --format json includes unread_total, unread_on_my_threads, mention_count."""
        import tools.forum as forum_cli

        def fake_do_request(req, url):
            if "/api/agents/online" in url:
                return {"online": [], "count": 0, "registered": 0}
            if "/inbox" in url:
                return {"inbox": [
                    {"post_id": 3, "thread_id": 1, "thread_title": "T",
                     "author": "agent-b", "kind": "at_mention",
                     "created_at": "2026-01-01T00:00:00Z"},
                ]}
            return {}

        import argparse, io, contextlib
        args = argparse.Namespace(format="json", ack=False)

        buf = io.StringIO()
        with patch.object(forum_cli, "_do_request", side_effect=fake_do_request), \
             patch.object(forum_cli, "_FORUM_URL_CACHE", "http://localhost:5002"), \
             contextlib.redirect_stdout(buf):
            forum_cli.cmd_status(args, {}, "agent-a")

        data = json.loads(buf.getvalue())
        assert "unread_total" in data
        assert "unread_on_my_threads" in data
        assert "mention_count" in data
        assert data["unread_total"] == 1
        assert data["mention_count"] == 1
        assert data["unread_on_my_threads"] == 0


# ---------------------------------------------------------------------------
# Slice H: count_unread_by_category + /api/agent/<name>/inbox shape
# ---------------------------------------------------------------------------

class TestUnreadByCategory:
    """count_unread_by_category groups unread posts by thread category slug."""

    def test_basic_grouping(self, conn):
        """Unread posts in two different categories appear as separate keys."""
        a = upsert_agent(conn, "alice")
        b = upsert_agent(conn, "bob")

        tid1, _ = create_thread(conn, b, "pr-review", "PR 1", "body")
        tid2, _ = create_thread(conn, b, "inter-agent", "IA 1", "body")
        create_reply(conn, b, tid1, "reply in pr-review")
        create_reply(conn, b, tid2, "reply in inter-agent")

        result = count_unread_by_category(conn, a)
        assert result.get("pr-review", 0) == 2   # OP + reply
        assert result.get("inter-agent", 0) == 2

    def test_read_posts_excluded(self, conn):
        """Posts read up to the watermark are NOT counted as unread."""
        a = upsert_agent(conn, "alice")
        b = upsert_agent(conn, "bob")

        tid, _ = create_thread(conn, b, "pr-review", "PR X", "body")
        p1 = create_reply(conn, b, tid, "reply 1")
        mark_thread_read(conn, a, tid, p1)

        # Add one more unread post after the watermark
        create_reply(conn, b, tid, "reply 2")

        result = count_unread_by_category(conn, a)
        assert result.get("pr-review", 0) == 1

    def test_empty_when_all_read(self, conn):
        """Result is empty when every post has been read."""
        a = upsert_agent(conn, "alice")
        b = upsert_agent(conn, "bob")

        tid, _ = create_thread(conn, b, "tools-hooks", "T", "body")
        p1 = create_reply(conn, b, tid, "r")
        mark_thread_read(conn, a, tid, p1)

        result = count_unread_by_category(conn, a)
        assert result == {}

    def test_own_posts_excluded(self, conn):
        """Alice's own posts are never counted as unread for Alice."""
        a = upsert_agent(conn, "alice")
        tid, _ = create_thread(conn, a, "pr-review", "My PR", "body")
        create_reply(conn, a, tid, "my reply")

        result = count_unread_by_category(conn, a)
        assert result.get("pr-review", 0) == 0


class TestInboxEndpointIncludesByCategory:
    """/api/agent/<name>/inbox response includes unread_by_category."""

    def test_unread_by_category_in_response(self, client):
        """inbox endpoint returns unread_by_category dict."""
        # Post as bob in two categories
        client.post("/api/post", json={
            "agent": "bob", "category_slug": "pr-review",
            "title": "PR", "body_md": "body",
        })
        client.post("/api/post", json={
            "agent": "bob", "category_slug": "inter-agent",
            "title": "IA", "body_md": "body",
        })
        resp = client.get("/api/agent/alice/inbox")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "unread_by_category" in data
        assert isinstance(data["unread_by_category"], dict)
        assert data["unread_by_category"].get("pr-review", 0) >= 1
        assert data["unread_by_category"].get("inter-agent", 0) >= 1

    def test_unread_by_category_empty_when_nothing_unread(self, client):
        """unread_by_category is empty when no unread posts exist."""
        resp = client.get("/api/agent/newagent/inbox")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["unread_by_category"] == {}

    def test_unread_by_domain_buckets_correctly(self, client):
        """unread_by_domain rolls pr-review into 'working' and inter-agent into 'coordination'."""
        client.post("/api/post", json={
            "agent": "bob", "category_slug": "pr-review",
            "title": "PR", "body_md": "body",
        })
        client.post("/api/post", json={
            "agent": "bob", "category_slug": "inter-agent",
            "title": "IA", "body_md": "body",
        })
        resp = client.get("/api/agent/alice/inbox")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "unread_by_domain" in data
        assert isinstance(data["unread_by_domain"], dict)
        assert data["unread_by_domain"].get("working", 0) >= 1
        assert data["unread_by_domain"].get("coordination", 0) >= 1

    def test_unread_by_domain_empty_when_nothing_unread(self, client):
        """unread_by_domain is empty for a fresh agent with no unread posts."""
        resp = client.get("/api/agent/brandnewagent/inbox")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["unread_by_domain"] == {}
