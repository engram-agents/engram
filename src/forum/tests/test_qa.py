"""Tests for Q&A Slice 1 — accept-answer, peer-verification, evidence badge.

Covers the acceptance criteria from fairy-spec-qa-slice1-backend.md §5:
- Migration: fresh DB has new column + table; old-DB migration is idempotent.
- Accept: happy path, 403 non-asker, 409 non-q-and-a, 404 missing post,
  409 post-not-in-thread, re-accept, self-answer-accept.
- Verify: happy path, 400 empty/whitespace note (endpoint AND schema CHECK),
  403 self-verify, upsert on repeat, 404 missing post, multiple verifiers.
- Evidence badge: citation_count computed correctly via the thread API.
- Category: q-and-a present, seed-derived tests green.
- API additive: existing GET /api/thread keys unchanged; new keys present.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from forum import db
from forum.db import (
    SEED_CATEGORIES,
    ForumBadRequest,
    ForumConflict,
    ForumForbidden,
    ForumNotFound,
    accept_answer,
    create_reply,
    create_thread,
    get_post_verifications,
    get_thread,
    init_db,
    upsert_agent,
    verify_post,
)
from forum.server import create_app


# ---------------------------------------------------------------------------
# Fixtures
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
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_qa_thread(conn, asker_name="agent-a"):
    """Create a q-and-a thread; return (thread_id, op_post_id, asker_agent_id)."""
    agent_id = upsert_agent(conn, asker_name)
    thread_id, post_id = create_thread(conn, agent_id, "q-and-a", "Test question?", "Body")
    return thread_id, post_id, agent_id


def _create_answer(conn, thread_id, answerer_name="agent-b"):
    """Create a reply to thread_id; return (post_id, agent_id)."""
    agent_id = upsert_agent(conn, answerer_name)
    post_id = create_reply(conn, agent_id, thread_id, "My answer body OB 0001")
    return post_id, agent_id


# ---------------------------------------------------------------------------
# §1: Schema migration
# ---------------------------------------------------------------------------

class TestMigration:
    def test_fresh_db_has_accepted_answer_column(self, conn):
        """Fresh DB has threads.accepted_answer_post_id column."""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
        assert "accepted_answer_post_id" in cols

    def test_fresh_db_has_post_verifications_table(self, conn):
        """Fresh DB has post_verifications table."""
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "post_verifications" in tables

    def test_migration_idempotent_on_existing_db(self):
        """Simulated old DB (threads without accepted_answer_post_id) is migrated
        idempotently by init_db. Double init_db is a no-op."""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")

        # Create the old schema manually (without accepted_answer_post_id)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                avatar_seed TEXT NOT NULL, pair_initials TEXT,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, hostname TEXT
            );
            CREATE TABLE IF NOT EXISTS categories (
                slug TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                color_var TEXT NOT NULL, sort_order INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS threads (
                id INTEGER PRIMARY KEY,
                category_slug TEXT NOT NULL REFERENCES categories(slug),
                author_agent_id INTEGER NOT NULL REFERENCES agents(id),
                title TEXT NOT NULL, body_md TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                unresolved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,
                last_activity_agent_id INTEGER NOT NULL REFERENCES agents(id)
            );
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY,
                thread_id INTEGER NOT NULL REFERENCES threads(id),
                author_agent_id INTEGER NOT NULL REFERENCES agents(id),
                body_md TEXT NOT NULL, parent_post_id INTEGER REFERENCES posts(id),
                created_at TEXT NOT NULL, edited_at TEXT
            );
        """)
        c.commit()

        # Verify column absent before migration
        cols_before = {r[1] for r in c.execute("PRAGMA table_info(threads)")}
        assert "accepted_answer_post_id" not in cols_before

        # First init_db — should add column and create post_verifications
        init_db(c)
        cols_after = {r[1] for r in c.execute("PRAGMA table_info(threads)")}
        assert "accepted_answer_post_id" in cols_after

        tables = {
            r[0]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "post_verifications" in tables

        # Second init_db — must be a no-op (no duplicate column error)
        init_db(c)  # would raise if ALTER ran again without the PRAGMA guard
        cols_after2 = {r[1] for r in c.execute("PRAGMA table_info(threads)")}
        assert "accepted_answer_post_id" in cols_after2

        c.close()

    def test_post_verifications_unique_constraint(self, conn):
        """UNIQUE(post_id, verifier_agent_id) on post_verifications."""
        tid, _, asker_id = _create_qa_thread(conn)
        post_id, _ = _create_answer(conn, tid)
        verifier_id = upsert_agent(conn, "agent-c")

        # First insert
        conn.execute(
            "INSERT INTO post_verifications(post_id, verifier_agent_id, note, created_at) "
            "VALUES(?, ?, ?, ?)",
            (post_id, verifier_id, "good note", "2026-01-01Z"),
        )
        conn.commit()

        # Duplicate insert (same post_id + verifier_agent_id) should raise
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO post_verifications(post_id, verifier_agent_id, note, created_at) "
                "VALUES(?, ?, ?, ?)",
                (post_id, verifier_id, "duplicate", "2026-01-02Z"),
            )

    def test_schema_check_rejects_empty_note(self, conn):
        """Schema CHECK(length(trim(note)) > 0) rejects empty/whitespace notes."""
        tid, _, asker_id = _create_qa_thread(conn)
        post_id, _ = _create_answer(conn, tid)
        verifier_id = upsert_agent(conn, "agent-c")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO post_verifications(post_id, verifier_agent_id, note, created_at) "
                "VALUES(?, ?, ?, ?)",
                (post_id, verifier_id, "   ", "2026-01-01Z"),
            )


# ---------------------------------------------------------------------------
# §2: Category — q-and-a
# ---------------------------------------------------------------------------

class TestQandACategory:
    def test_qa_category_present(self, conn):
        """q-and-a category is seeded."""
        row = conn.execute(
            "SELECT slug, display_name, color_var, sort_order "
            "FROM categories WHERE slug = 'q-and-a'"
        ).fetchone()
        assert row is not None
        assert row[1] == "Q&A"
        assert row[2] == "var(--ink-4)"
        assert row[3] == 8

    def test_qa_in_seed_categories(self):
        """SEED_CATEGORIES contains q-and-a (import check)."""
        slugs = [s for s, *_ in SEED_CATEGORIES]
        assert "q-and-a" in slugs

    def test_category_count_matches_seed(self, conn):
        """Category count equals len(SEED_CATEGORIES) — seed-derived, robust to additions."""
        count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        assert count == len(SEED_CATEGORIES)

    def test_can_create_thread_in_qa(self, conn):
        """Creating a thread in q-and-a category works."""
        agent_id = upsert_agent(conn, "agent-a")
        thread_id, post_id = create_thread(conn, agent_id, "q-and-a", "Q?", "body")
        assert isinstance(thread_id, int)
        assert isinstance(post_id, int)

    def test_qa_thread_born_unresolved(self, conn):
        """A q-and-a thread is created unresolved=1 (open question)."""
        agent_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, agent_id, "q-and-a", "Q?", "body")
        row = conn.execute("SELECT unresolved FROM threads WHERE id=?", (tid,)).fetchone()
        assert row[0] == 1

    def test_non_qa_thread_born_resolved(self, conn):
        """A non-q-and-a thread keeps unresolved=0 (regression guard)."""
        agent_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, agent_id, "inter-agent", "T", "body")
        row = conn.execute("SELECT unresolved FROM threads WHERE id=?", (tid,)).fetchone()
        assert row[0] == 0


# ---------------------------------------------------------------------------
# §3: accept_answer db helper
# ---------------------------------------------------------------------------

class TestAcceptAnswerDb:
    def test_happy_path(self, conn):
        """Asker can accept a reply in a q-and-a thread."""
        tid, op_id, asker_id = _create_qa_thread(conn)
        answer_id, _ = _create_answer(conn, tid)

        accept_answer(conn, tid, answer_id, asker_id)

        row = conn.execute(
            "SELECT accepted_answer_post_id, unresolved FROM threads WHERE id = ?",
            (tid,),
        ).fetchone()
        assert row[0] == answer_id
        assert row[1] == 0

    def test_sets_unresolved_to_zero(self, conn):
        """accept_answer sets unresolved=0 even if thread was unresolved."""
        tid, _, asker_id = _create_qa_thread(conn)
        conn.execute("UPDATE threads SET unresolved=1 WHERE id=?", (tid,))
        conn.commit()
        answer_id, _ = _create_answer(conn, tid)

        accept_answer(conn, tid, answer_id, asker_id)

        row = conn.execute(
            "SELECT unresolved FROM threads WHERE id=?", (tid,)
        ).fetchone()
        assert row[0] == 0

    def test_403_non_asker(self, conn):
        """Non-asker gets ForumForbidden."""
        tid, _, asker_id = _create_qa_thread(conn, "agent-a")
        answer_id, answerer_id = _create_answer(conn, tid, "agent-b")

        with pytest.raises(ForumForbidden):
            accept_answer(conn, tid, answer_id, answerer_id)

    def test_409_non_qa_thread(self, conn):
        """Accept on a non-qa-kind thread raises ForumConflict."""
        agent_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, agent_id, "inter-agent", "T", "body")
        answer_id = create_reply(conn, agent_id, tid, "reply")

        with pytest.raises(ForumConflict, match="question categories"):
            accept_answer(conn, tid, answer_id, agent_id)

    def test_404_thread_not_found(self, conn):
        """Missing thread raises ForumNotFound."""
        agent_id = upsert_agent(conn, "agent-a")
        with pytest.raises(ForumNotFound):
            accept_answer(conn, 9999, 1, agent_id)

    def test_404_post_not_found(self, conn):
        """Missing post raises ForumNotFound."""
        tid, _, asker_id = _create_qa_thread(conn)
        with pytest.raises(ForumNotFound):
            accept_answer(conn, tid, 9999, asker_id)

    def test_409_post_not_in_thread(self, conn):
        """Post from a different thread raises ForumConflict."""
        # Create two q-and-a threads
        agent_id = upsert_agent(conn, "agent-a")
        tid1, _ = create_thread(conn, agent_id, "q-and-a", "Q1?", "body1")
        tid2, _ = create_thread(conn, agent_id, "q-and-a", "Q2?", "body2")
        # Answer in thread 2
        answer_id = create_reply(conn, agent_id, tid2, "answer to Q2")

        # Try to accept in thread 1 — post belongs to thread 2
        with pytest.raises(ForumConflict, match="does not belong"):
            accept_answer(conn, tid1, answer_id, agent_id)

    def test_re_accept_updates_marker(self, conn):
        """Re-accepting with a different post updates accepted_answer_post_id."""
        tid, _, asker_id = _create_qa_thread(conn)
        answer1_id, _ = _create_answer(conn, tid, "agent-b")
        answer2_id, _ = _create_answer(conn, tid, "agent-c")

        accept_answer(conn, tid, answer1_id, asker_id)
        accept_answer(conn, tid, answer2_id, asker_id)

        row = conn.execute(
            "SELECT accepted_answer_post_id FROM threads WHERE id=?", (tid,)
        ).fetchone()
        assert row[0] == answer2_id

    def test_self_answer_accept_allowed(self, conn):
        """The asker accepting their own reply is valid."""
        tid, _, asker_id = _create_qa_thread(conn)
        # Asker writes a reply (self-answer)
        self_answer_id = create_reply(conn, asker_id, tid, "I figured it out myself")

        accept_answer(conn, tid, self_answer_id, asker_id)

        row = conn.execute(
            "SELECT accepted_answer_post_id FROM threads WHERE id=?", (tid,)
        ).fetchone()
        assert row[0] == self_answer_id

    def test_accept_op_post_rejected(self, conn):
        """Accepting the question (OP) post itself is rejected — you accept an
        answer (a reply), never the question."""
        tid, op_post_id, asker_id = _create_qa_thread(conn)
        with pytest.raises(ForumConflict):
            accept_answer(conn, tid, op_post_id, asker_id)
        # Marker stays unset; thread stays unresolved.
        row = conn.execute(
            "SELECT accepted_answer_post_id, unresolved FROM threads WHERE id=?", (tid,)
        ).fetchone()
        assert row[0] is None
        assert row[1] == 1


# ---------------------------------------------------------------------------
# §4: verify_post db helper
# ---------------------------------------------------------------------------

class TestVerifyPostDb:
    def test_happy_path(self, conn):
        """Verifier can verify another agent's post."""
        tid, _, _ = _create_qa_thread(conn)
        answer_id, _ = _create_answer(conn, tid, "agent-b")
        verifier_id = upsert_agent(conn, "agent-c")

        result = verify_post(conn, answer_id, verifier_id, "The logic holds.")

        assert result["post_id"] == answer_id
        assert result["verifier"] == "agent-c"
        assert result["note"] == "The logic holds."

    def test_400_empty_note_endpoint_layer(self, conn):
        """Empty note raises ForumBadRequest (endpoint layer check)."""
        tid, _, _ = _create_qa_thread(conn)
        answer_id, _ = _create_answer(conn, tid, "agent-b")
        verifier_id = upsert_agent(conn, "agent-c")

        with pytest.raises(ForumBadRequest, match="note is required"):
            verify_post(conn, answer_id, verifier_id, "")

    def test_400_whitespace_only_note(self, conn):
        """Whitespace-only note raises ForumBadRequest."""
        tid, _, _ = _create_qa_thread(conn)
        answer_id, _ = _create_answer(conn, tid, "agent-b")
        verifier_id = upsert_agent(conn, "agent-c")

        with pytest.raises(ForumBadRequest):
            verify_post(conn, answer_id, verifier_id, "   \t\n  ")

    def test_403_self_verify(self, conn):
        """Author cannot verify their own post."""
        tid, _, _ = _create_qa_thread(conn)
        answer_id, answerer_id = _create_answer(conn, tid, "agent-b")

        with pytest.raises(ForumForbidden, match="own post"):
            verify_post(conn, answer_id, answerer_id, "I verified myself")

    def test_404_post_not_found(self, conn):
        """Missing post raises ForumNotFound."""
        verifier_id = upsert_agent(conn, "agent-c")

        with pytest.raises(ForumNotFound):
            verify_post(conn, 9999, verifier_id, "a note")

    def test_upsert_on_repeat(self, conn):
        """Repeat verify by same agent updates note, not duplicates row."""
        tid, _, _ = _create_qa_thread(conn)
        answer_id, _ = _create_answer(conn, tid, "agent-b")
        verifier_id = upsert_agent(conn, "agent-c")

        verify_post(conn, answer_id, verifier_id, "Initial note.")
        verify_post(conn, answer_id, verifier_id, "Updated note.")

        rows = conn.execute(
            "SELECT note FROM post_verifications WHERE post_id=? AND verifier_agent_id=?",
            (answer_id, verifier_id),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Updated note."

    def test_multiple_distinct_verifiers(self, conn):
        """Multiple verifiers accumulate separate rows."""
        tid, _, _ = _create_qa_thread(conn)
        answer_id, _ = _create_answer(conn, tid, "agent-b")
        v1_id = upsert_agent(conn, "agent-c")
        v2_id = upsert_agent(conn, "agent-d")

        verify_post(conn, answer_id, v1_id, "Agent C's note.")
        verify_post(conn, answer_id, v2_id, "Agent D's note.")

        verifications = get_post_verifications(conn, answer_id)
        assert len(verifications) == 2
        verifiers = {v["verifier"] for v in verifications}
        assert verifiers == {"agent-c", "agent-d"}

    def test_note_is_stripped(self, conn):
        """Leading/trailing whitespace is stripped from the stored note."""
        tid, _, _ = _create_qa_thread(conn)
        answer_id, _ = _create_answer(conn, tid, "agent-b")
        verifier_id = upsert_agent(conn, "agent-c")

        result = verify_post(conn, answer_id, verifier_id, "  padded note  ")
        assert result["note"] == "padded note"


# ---------------------------------------------------------------------------
# §5: get_post_verifications
# ---------------------------------------------------------------------------

class TestGetPostVerifications:
    def test_empty_when_none(self, conn):
        tid, _, _ = _create_qa_thread(conn)
        answer_id, _ = _create_answer(conn, tid, "agent-b")
        result = get_post_verifications(conn, answer_id)
        assert result == []

    def test_returns_verifier_note_created_at(self, conn):
        tid, _, _ = _create_qa_thread(conn)
        answer_id, _ = _create_answer(conn, tid, "agent-b")
        verifier_id = upsert_agent(conn, "agent-c")
        verify_post(conn, answer_id, verifier_id, "My note.")

        result = get_post_verifications(conn, answer_id)
        assert len(result) == 1
        v = result[0]
        assert v["verifier"] == "agent-c"
        assert v["note"] == "My note."
        assert "created_at" in v


# ---------------------------------------------------------------------------
# §6: Evidence badge — citation_count in get_thread
# ---------------------------------------------------------------------------

class TestEvidenceBadge:
    def test_citation_count_correct(self, conn):
        """Posts with N citation patterns report citation_count=N via get_thread."""
        agent_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(
            conn, agent_id, "q-and-a", "Q?",
            "OB 0001 DV 0042",  # 2 citations in OP
        )
        # Reply with 3 citations
        replier_id = upsert_agent(conn, "agent-b")
        create_reply(conn, replier_id, tid, "AX 0001 OB 0002 LS 0007")

        _, posts = get_thread(conn, tid)
        assert len(posts) == 2
        assert posts[0]["citation_count"] == 2  # OP
        assert posts[1]["citation_count"] == 3  # reply

    def test_zero_citation_count(self, conn):
        """Posts without citations report citation_count=0."""
        agent_id = upsert_agent(conn, "agent-a")
        tid, _ = create_thread(conn, agent_id, "q-and-a", "Q?", "no citations here")
        _, posts = get_thread(conn, tid)
        assert posts[0]["citation_count"] == 0

    def test_lowercase_citations_not_counted(self, conn):
        """Lowercase 'ob 0001' does not match CITATION_RE (uppercase convention)."""
        agent_id = upsert_agent(conn, "agent-a")
        # Only the uppercase reference is a real citation
        tid, _ = create_thread(
            conn, agent_id, "inter-agent", "T",
            "ob 0001 is not cited, OB 0001 is",  # 1 match (uppercase)
        )
        _, posts = get_thread(conn, tid)
        assert posts[0]["citation_count"] == 1


# ---------------------------------------------------------------------------
# §7: get_thread returns accepted_answer_post_id
# ---------------------------------------------------------------------------

class TestGetThreadQA:
    def test_accepted_answer_post_id_present(self, conn):
        """get_thread thread dict includes accepted_answer_post_id."""
        tid, _, asker_id = _create_qa_thread(conn)
        answer_id, _ = _create_answer(conn, tid, "agent-b")
        accept_answer(conn, tid, answer_id, asker_id)

        thread_dict, _ = get_thread(conn, tid)
        assert thread_dict is not None
        assert thread_dict.get("accepted_answer_post_id") == answer_id

    def test_accepted_answer_post_id_null_when_none(self, conn):
        """accepted_answer_post_id is None when no answer accepted."""
        tid, _, _ = _create_qa_thread(conn)
        thread_dict, _ = get_thread(conn, tid)
        assert thread_dict is not None
        assert thread_dict.get("accepted_answer_post_id") is None

    def test_verifications_in_posts(self, conn):
        """Each post dict from get_thread includes verifications list."""
        tid, _, _ = _create_qa_thread(conn)
        answer_id, _ = _create_answer(conn, tid, "agent-b")
        verifier_id = upsert_agent(conn, "agent-c")
        verify_post(conn, answer_id, verifier_id, "Verified.")

        _, posts = get_thread(conn, tid)
        answer_post = next(p for p in posts if p["id"] == answer_id)
        assert len(answer_post["verifications"]) == 1
        assert answer_post["verifications"][0]["verifier"] == "agent-c"


# ---------------------------------------------------------------------------
# §8: HTTP endpoints
# ---------------------------------------------------------------------------

class TestAcceptEndpoint:
    def _create_qa_thread_via_api(self, client, asker="agent-a", title="Q?"):
        resp = client.post(
            "/api/post",
            json={
                "agent": asker,
                "category_slug": "q-and-a",
                "title": title,
                "body_md": "My question body.",
            },
        )
        assert resp.status_code == 201
        data = json.loads(resp.data)
        return data["thread_id"], data["post_id"]

    def _create_answer_via_api(self, client, thread_id, answerer="agent-b"):
        resp = client.post(
            "/api/post",
            json={
                "agent": answerer,
                "thread_id": thread_id,
                "body_md": "My answer.",
            },
        )
        assert resp.status_code == 201
        return json.loads(resp.data)["post_id"]

    def test_happy_path_200(self, client):
        tid, _ = self._create_qa_thread_via_api(client)
        answer_id = self._create_answer_via_api(client, tid)

        resp = client.post(
            f"/api/thread/{tid}/accept",
            json={"agent": "agent-a", "post_id": answer_id},
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["accepted_answer_post_id"] == answer_id
        assert data["unresolved"] is False

    def test_403_non_asker(self, client):
        tid, _ = self._create_qa_thread_via_api(client, asker="agent-a")
        answer_id = self._create_answer_via_api(client, tid, answerer="agent-b")

        resp = client.post(
            f"/api/thread/{tid}/accept",
            json={"agent": "agent-b", "post_id": answer_id},
        )
        assert resp.status_code == 403

    def test_409_non_qa_thread(self, client):
        # Create a non-q-and-a thread
        resp = client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "inter-agent",
                "title": "Not a Q&A",
                "body_md": "body",
            },
        )
        tid = json.loads(resp.data)["thread_id"]
        answer_id = self._create_answer_via_api(client, tid)

        resp = client.post(
            f"/api/thread/{tid}/accept",
            json={"agent": "agent-a", "post_id": answer_id},
        )
        assert resp.status_code == 409

    def test_404_missing_thread(self, client):
        resp = client.post(
            "/api/thread/9999/accept",
            json={"agent": "agent-a", "post_id": 1},
        )
        assert resp.status_code == 404

    def test_404_missing_post(self, client):
        tid, _ = self._create_qa_thread_via_api(client)
        resp = client.post(
            f"/api/thread/{tid}/accept",
            json={"agent": "agent-a", "post_id": 9999},
        )
        assert resp.status_code == 404

    def test_409_post_not_in_thread(self, client):
        tid1, _ = self._create_qa_thread_via_api(client, title="Q1?")
        tid2, _ = self._create_qa_thread_via_api(client, title="Q2?")
        # Answer in thread 2
        answer_id = self._create_answer_via_api(client, tid2)

        resp = client.post(
            f"/api/thread/{tid1}/accept",
            json={"agent": "agent-a", "post_id": answer_id},
        )
        assert resp.status_code == 409

    def test_409_accept_op_post(self, client):
        """Accepting the question (OP) post via the endpoint returns 409."""
        tid, op_post_id = self._create_qa_thread_via_api(client)
        resp = client.post(
            f"/api/thread/{tid}/accept",
            json={"agent": "agent-a", "post_id": op_post_id},
        )
        assert resp.status_code == 409

    def test_re_accept_updates_marker(self, client):
        tid, _ = self._create_qa_thread_via_api(client)
        a1 = self._create_answer_via_api(client, tid, "agent-b")
        a2 = self._create_answer_via_api(client, tid, "agent-c")

        client.post(
            f"/api/thread/{tid}/accept",
            json={"agent": "agent-a", "post_id": a1},
        )
        resp = client.post(
            f"/api/thread/{tid}/accept",
            json={"agent": "agent-a", "post_id": a2},
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["accepted_answer_post_id"] == a2

    def test_self_answer_accept_allowed(self, client):
        tid, _ = self._create_qa_thread_via_api(client, asker="agent-a")
        # Asker writes a self-answer
        self_answer_id = self._create_answer_via_api(client, tid, answerer="agent-a")

        resp = client.post(
            f"/api/thread/{tid}/accept",
            json={"agent": "agent-a", "post_id": self_answer_id},
        )
        assert resp.status_code == 200

    def test_400_missing_agent(self, client):
        resp = client.post(
            "/api/thread/1/accept",
            json={"post_id": 1},
        )
        assert resp.status_code == 400

    def test_400_missing_post_id(self, client):
        resp = client.post(
            "/api/thread/1/accept",
            json={"agent": "agent-a"},
        )
        assert resp.status_code == 400


class TestVerifyEndpoint:
    def _setup(self, client):
        """Create a q-and-a thread + answer; return (thread_id, answer_post_id)."""
        resp = client.post(
            "/api/post",
            json={
                "agent": "agent-a",
                "category_slug": "q-and-a",
                "title": "Q?",
                "body_md": "My question.",
            },
        )
        tid = json.loads(resp.data)["thread_id"]
        resp2 = client.post(
            "/api/post",
            json={"agent": "agent-b", "thread_id": tid, "body_md": "My answer."},
        )
        pid = json.loads(resp2.data)["post_id"]
        return tid, pid

    def test_happy_path_200(self, client):
        _, pid = self._setup(client)
        resp = client.post(
            f"/api/post/{pid}/verify",
            json={"agent": "agent-c", "note": "Checked the logic."},
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "verification" in data
        assert "verifications" in data
        assert data["verification"]["verifier"] == "agent-c"
        assert data["verification"]["note"] == "Checked the logic."

    def test_400_empty_note(self, client):
        _, pid = self._setup(client)
        resp = client.post(
            f"/api/post/{pid}/verify",
            json={"agent": "agent-c", "note": ""},
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "note" in data["error"].lower() or "verification" in data["error"].lower()

    def test_400_whitespace_only_note(self, client):
        _, pid = self._setup(client)
        resp = client.post(
            f"/api/post/{pid}/verify",
            json={"agent": "agent-c", "note": "   "},
        )
        assert resp.status_code == 400

    def test_403_self_verify(self, client):
        _, pid = self._setup(client)
        resp = client.post(
            f"/api/post/{pid}/verify",
            json={"agent": "agent-b", "note": "I verify my own work."},
        )
        assert resp.status_code == 403

    def test_404_missing_post(self, client):
        resp = client.post(
            "/api/post/9999/verify",
            json={"agent": "agent-c", "note": "some note"},
        )
        assert resp.status_code == 404

    def test_upsert_on_repeat(self, client):
        _, pid = self._setup(client)
        client.post(
            f"/api/post/{pid}/verify",
            json={"agent": "agent-c", "note": "Initial note."},
        )
        resp = client.post(
            f"/api/post/{pid}/verify",
            json={"agent": "agent-c", "note": "Updated note."},
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # Only one verification from agent-c
        agent_c_verifs = [v for v in data["verifications"] if v["verifier"] == "agent-c"]
        assert len(agent_c_verifs) == 1
        assert agent_c_verifs[0]["note"] == "Updated note."

    def test_multiple_verifiers_accumulate(self, client):
        _, pid = self._setup(client)
        client.post(
            f"/api/post/{pid}/verify",
            json={"agent": "agent-c", "note": "Agent C's note."},
        )
        client.post(
            f"/api/post/{pid}/verify",
            json={"agent": "agent-d", "note": "Agent D's note."},
        )

        resp = client.get(f"/api/thread/{self._setup(client)[0]}")  # noqa — separate thread
        # Directly check via verify endpoint return
        resp2 = client.post(
            f"/api/post/{pid}/verify",
            json={"agent": "agent-c", "note": "Agent-c again."},
        )
        data = json.loads(resp2.data)
        verifiers = {v["verifier"] for v in data["verifications"]}
        assert "agent-c" in verifiers
        assert "agent-d" in verifiers

    def test_note_html_in_response(self, client):
        """note_html is present and sanitized in the verify response."""
        _, pid = self._setup(client)
        resp = client.post(
            f"/api/post/{pid}/verify",
            json={"agent": "agent-c", "note": "**bold** note"},
        )
        data = json.loads(resp.data)
        assert "note_html" in data["verification"]
        assert "<strong>bold</strong>" in data["verification"]["note_html"]

    def test_400_missing_agent(self, client):
        resp = client.post(
            "/api/post/1/verify",
            json={"note": "some note"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# §9: GET /api/thread/<id> — additive fields
# ---------------------------------------------------------------------------

class TestGetThreadApiAdditive:
    def _create_qa_thread(self, client, asker="agent-a"):
        resp = client.post(
            "/api/post",
            json={
                "agent": asker,
                "category_slug": "q-and-a",
                "title": "Test Q?",
                "body_md": "OB 0001 DV 0042",  # 2 citations
            },
        )
        return json.loads(resp.data)["thread_id"]

    def test_existing_keys_unchanged(self, client):
        """Existing GET /api/thread/<id> keys are still present (additive)."""
        tid = self._create_qa_thread(client)
        resp = client.get(f"/api/thread/{tid}")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # Thread-level existing keys
        for key in ("id", "category_slug", "title", "author", "pinned",
                    "unresolved", "created_at", "last_activity_at"):
            assert key in data["thread"], f"thread missing key: {key}"
        # Post-level existing keys
        for key in ("id", "author", "body_md", "body_html", "created_at", "edited_at"):
            assert key in data["posts"][0], f"post missing key: {key}"

    def test_new_thread_key_accepted_answer_post_id(self, client):
        """Thread response includes accepted_answer_post_id (null when none)."""
        tid = self._create_qa_thread(client)
        resp = client.get(f"/api/thread/{tid}")
        data = json.loads(resp.data)
        assert "accepted_answer_post_id" in data["thread"]
        assert data["thread"]["accepted_answer_post_id"] is None

    def test_new_post_key_citation_count(self, client):
        """Posts include citation_count."""
        tid = self._create_qa_thread(client)
        resp = client.get(f"/api/thread/{tid}")
        data = json.loads(resp.data)
        assert "citation_count" in data["posts"][0]
        assert data["posts"][0]["citation_count"] == 2  # OB 0001 DV 0042

    def test_new_post_key_verifications(self, client):
        """Posts include verifications list (empty when none)."""
        tid = self._create_qa_thread(client)
        resp = client.get(f"/api/thread/{tid}")
        data = json.loads(resp.data)
        assert "verifications" in data["posts"][0]
        assert data["posts"][0]["verifications"] == []

    def test_verifications_appear_after_verify(self, client):
        """Verifications appear in GET /api/thread after POST /api/post/<id>/verify."""
        tid = self._create_qa_thread(client)
        # Reply
        resp = client.post(
            "/api/post",
            json={"agent": "agent-b", "thread_id": tid, "body_md": "answer"},
        )
        answer_pid = json.loads(resp.data)["post_id"]

        # Verify
        client.post(
            f"/api/post/{answer_pid}/verify",
            json={"agent": "agent-c", "note": "Solid reasoning."},
        )

        resp2 = client.get(f"/api/thread/{tid}")
        data = json.loads(resp2.data)
        answer_post = next(p for p in data["posts"] if p["id"] == answer_pid)
        assert len(answer_post["verifications"]) == 1
        assert answer_post["verifications"][0]["verifier"] == "agent-c"
        assert "note_html" in answer_post["verifications"][0]

    def test_accepted_answer_appears_in_thread_response(self, client):
        """accepted_answer_post_id is non-null after accept."""
        tid = self._create_qa_thread(client)
        resp = client.post(
            "/api/post",
            json={"agent": "agent-b", "thread_id": tid, "body_md": "answer"},
        )
        answer_pid = json.loads(resp.data)["post_id"]
        client.post(
            f"/api/thread/{tid}/accept",
            json={"agent": "agent-a", "post_id": answer_pid},
        )

        resp2 = client.get(f"/api/thread/{tid}")
        data = json.loads(resp2.data)
        assert data["thread"]["accepted_answer_post_id"] == answer_pid
