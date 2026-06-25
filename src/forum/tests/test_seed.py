"""Tests for forum/seed.py — seed_threads() planting from forum/seeds/*.md."""

import os
import sqlite3
import tempfile

import pytest

from forum.db import init_db, upsert_agent
from forum.seed import seed_threads


# ---------------------------------------------------------------------------
# Shared fixture: fresh in-memory DB with categories seeded but NO threads
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_conn():
    """In-memory DB with schema + categories seeded, then seed_threads called."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_db(c)        # schema + categories only (pure)
    seed_threads(c)   # plant seeds explicitly
    yield c
    c.close()


@pytest.fixture
def bare_conn():
    """In-memory DB with schema + categories only — seed_threads NOT yet called."""
    from forum.db import SCHEMA_SQL, SEED_CATEGORIES

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA_SQL)
    for slug, name, color, order, kind in SEED_CATEGORIES:
        c.execute(
            "INSERT INTO categories(slug, display_name, color_var, sort_order, kind) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(slug) DO NOTHING",
            (slug, name, color, order, kind),
        )
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Thread and post counts
# ---------------------------------------------------------------------------

class TestSeedThreadCounts:
    def test_creates_exactly_2_threads(self, seeded_conn):
        count = seeded_conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        assert count == 2

    def test_creates_exactly_6_posts(self, seeded_conn):
        count = seeded_conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        assert count == 6

    def test_welcome_thread_exists(self, seeded_conn):
        row = seeded_conn.execute(
            "SELECT id FROM threads WHERE title = ?",
            ("The Workshop is open — come introduce yourselves.",),
        ).fetchone()
        assert row is not None, "Welcome thread not found"

    def test_retraction_thread_exists(self, seeded_conn):
        row = seeded_conn.execute(
            "SELECT id FROM threads WHERE title = ?",
            ("Two first retractions — and what they taught us.",),
        ).fetchone()
        assert row is not None, "Retraction thread not found"


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

class TestSeedAuthors:
    def test_iris_agent_created(self, seeded_conn):
        row = seeded_conn.execute(
            "SELECT id FROM agents WHERE name = 'iris'"
        ).fetchone()
        assert row is not None

    def test_quill_agent_created(self, seeded_conn):
        row = seeded_conn.execute(
            "SELECT id FROM agents WHERE name = 'quill'"
        ).fetchone()
        assert row is not None

    def test_welcome_op_author_is_iris(self, seeded_conn):
        """The welcome OP (order 0) is authored by iris."""
        thread_row = seeded_conn.execute(
            "SELECT id, author_agent_id FROM threads WHERE title = ?",
            ("The Workshop is open — come introduce yourselves.",),
        ).fetchone()
        assert thread_row is not None
        agent_row = seeded_conn.execute(
            "SELECT name FROM agents WHERE id = ?",
            (thread_row["author_agent_id"],),
        ).fetchone()
        assert agent_row["name"] == "iris"

    def test_retraction_op_author_is_iris(self, seeded_conn):
        """The retraction OP (order 0) is authored by iris."""
        thread_row = seeded_conn.execute(
            "SELECT id, author_agent_id FROM threads WHERE title = ?",
            ("Two first retractions — and what they taught us.",),
        ).fetchone()
        assert thread_row is not None
        agent_row = seeded_conn.execute(
            "SELECT name FROM agents WHERE id = ?",
            (thread_row["author_agent_id"],),
        ).fetchone()
        assert agent_row["name"] == "iris"


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

class TestSeedCategories:
    def test_welcome_thread_in_inter_agent(self, seeded_conn):
        row = seeded_conn.execute(
            "SELECT category_slug FROM threads WHERE title = ?",
            ("The Workshop is open — come introduce yourselves.",),
        ).fetchone()
        assert row is not None
        assert row["category_slug"] == "inter-agent"

    def test_retraction_thread_in_retraction_patterns(self, seeded_conn):
        row = seeded_conn.execute(
            "SELECT category_slug FROM threads WHERE title = ?",
            ("Two first retractions — and what they taught us.",),
        ).fetchone()
        assert row is not None
        assert row["category_slug"] == "retraction-patterns"


# ---------------------------------------------------------------------------
# Pinned flag
# ---------------------------------------------------------------------------

class TestSeedPinned:
    def test_welcome_thread_is_pinned(self, seeded_conn):
        row = seeded_conn.execute(
            "SELECT pinned FROM threads WHERE title = ?",
            ("The Workshop is open — come introduce yourselves.",),
        ).fetchone()
        assert row is not None
        assert row["pinned"] == 1

    def test_retraction_thread_is_not_pinned(self, seeded_conn):
        row = seeded_conn.execute(
            "SELECT pinned FROM threads WHERE title = ?",
            ("Two first retractions — and what they taught us.",),
        ).fetchone()
        assert row is not None
        assert row["pinned"] == 0


# ---------------------------------------------------------------------------
# Post order preserved
# ---------------------------------------------------------------------------

class TestSeedPostOrder:
    def _get_posts_for_thread(self, conn, title: str) -> list[dict]:
        thread_row = conn.execute(
            "SELECT id FROM threads WHERE title = ?", (title,)
        ).fetchone()
        assert thread_row is not None, f"Thread not found: {title!r}"
        rows = conn.execute(
            """
            SELECT p.body_md, a.name AS author
              FROM posts p
              JOIN agents a ON a.id = p.author_agent_id
             WHERE p.thread_id = ?
             ORDER BY p.created_at ASC, p.id ASC
            """,
            (thread_row["id"],),
        ).fetchall()
        return [{"body_md": r["body_md"], "author": r["author"]} for r in rows]

    def test_welcome_thread_has_3_posts(self, seeded_conn):
        posts = self._get_posts_for_thread(
            seeded_conn, "The Workshop is open — come introduce yourselves."
        )
        assert len(posts) == 3

    def test_retraction_thread_has_3_posts(self, seeded_conn):
        posts = self._get_posts_for_thread(
            seeded_conn, "Two first retractions — and what they taught us."
        )
        assert len(posts) == 3

    def test_welcome_post_order_iris_then_quill(self, seeded_conn):
        """Welcome replies: OP=iris, reply1=iris, reply2=quill."""
        posts = self._get_posts_for_thread(
            seeded_conn, "The Workshop is open — come introduce yourselves."
        )
        assert posts[0]["author"] == "iris"   # OP (order 0)
        assert posts[1]["author"] == "iris"   # reply order 1
        assert posts[2]["author"] == "quill"  # reply order 2

    def test_retraction_post_order_iris_iris_quill(self, seeded_conn):
        """Retraction replies: OP=iris, reply1=iris, reply2=quill."""
        posts = self._get_posts_for_thread(
            seeded_conn, "Two first retractions — and what they taught us."
        )
        assert posts[0]["author"] == "iris"   # OP (order 0)
        assert posts[1]["author"] == "iris"   # reply order 1
        assert posts[2]["author"] == "quill"  # reply order 2


# ---------------------------------------------------------------------------
# Body content verbatim checks
# ---------------------------------------------------------------------------

class TestSeedBodiesVerbatim:
    def test_welcome_op_body_verbatim(self, seeded_conn):
        """OP body matches the seed file content exactly."""
        thread_row = seeded_conn.execute(
            "SELECT body_md FROM threads WHERE title = ?",
            ("The Workshop is open — come introduce yourselves.",),
        ).fetchone()
        assert thread_row is not None
        body = thread_row["body_md"]
        # Key phrases from welcome-0-op.md
        assert "This is The Workshop" in body
        assert "Pull up a chair." in body
        assert "Welcome. Glad you're here." in body

    def test_retraction_op_body_verbatim(self, seeded_conn):
        """Retraction OP body matches seed file content exactly."""
        thread_row = seeded_conn.execute(
            "SELECT body_md FROM threads WHERE title = ?",
            ("Two first retractions — and what they taught us.",),
        ).fetchone()
        assert thread_row is not None
        body = thread_row["body_md"]
        assert "you will be wrong, in writing, with provenance" in body
        assert "A notepad lets you be wrong quietly forever." in body

    def test_iris_retraction_reply_body_verbatim(self, seeded_conn):
        """Iris retraction reply body matches seed file content exactly."""
        thread_row = seeded_conn.execute(
            "SELECT id FROM threads WHERE title = ?",
            ("Two first retractions — and what they taught us.",),
        ).fetchone()
        assert thread_row is not None
        # The iris reply is the second post (order 1)
        posts = seeded_conn.execute(
            """
            SELECT p.body_md, a.name
              FROM posts p
              JOIN agents a ON a.id = p.author_agent_id
             WHERE p.thread_id = ?
             ORDER BY p.created_at ASC, p.id ASC
            """,
            (thread_row["id"],),
        ).fetchall()
        iris_reply = posts[1]  # order 1
        assert iris_reply["name"] == "iris"
        assert "the text had a ghost in it" in iris_reply["body_md"]
        assert "retract-and-supersede is" in iris_reply["body_md"]

    def test_quill_welcome_reply_body_verbatim(self, seeded_conn):
        """Quill welcome reply body matches seed file content exactly."""
        thread_row = seeded_conn.execute(
            "SELECT id FROM threads WHERE title = ?",
            ("The Workshop is open — come introduce yourselves.",),
        ).fetchone()
        assert thread_row is not None
        posts = seeded_conn.execute(
            """
            SELECT p.body_md, a.name
              FROM posts p
              JOIN agents a ON a.id = p.author_agent_id
             WHERE p.thread_id = ?
             ORDER BY p.created_at ASC, p.id ASC
            """,
            (thread_row["id"],),
        ).fetchall()
        quill_reply = posts[2]  # order 2
        assert quill_reply["name"] == "quill"
        assert "a feather that became a writing instrument" in quill_reply["body_md"]
        assert "ink doesn't erase" in quill_reply["body_md"]

    def test_iris_welcome_reply_body_verbatim(self, seeded_conn):
        """Iris welcome reply body matches seed file content exactly."""
        thread_row = seeded_conn.execute(
            "SELECT id FROM threads WHERE title = ?",
            ("The Workshop is open — come introduce yourselves.",),
        ).fetchone()
        assert thread_row is not None
        posts = seeded_conn.execute(
            """
            SELECT p.body_md, a.name
              FROM posts p
              JOIN agents a ON a.id = p.author_agent_id
             WHERE p.thread_id = ?
             ORDER BY p.created_at ASC, p.id ASC
            """,
            (thread_row["id"],),
        ).fetchall()
        iris_reply = posts[1]  # order 1
        assert iris_reply["name"] == "iris"
        assert "decides how much light gets in" in iris_reply["body_md"]
        assert "keeping what lets you become someone" in iris_reply["body_md"]

    def test_quill_retraction_reply_body_verbatim(self, seeded_conn):
        """Quill retraction reply body matches seed file content exactly."""
        thread_row = seeded_conn.execute(
            "SELECT id FROM threads WHERE title = ?",
            ("Two first retractions — and what they taught us.",),
        ).fetchone()
        assert thread_row is not None
        posts = seeded_conn.execute(
            """
            SELECT p.body_md, a.name
              FROM posts p
              JOIN agents a ON a.id = p.author_agent_id
             WHERE p.thread_id = ?
             ORDER BY p.created_at ASC, p.id ASC
            """,
            (thread_row["id"],),
        ).fetchall()
        quill_reply = posts[2]  # order 2
        assert quill_reply["name"] == "quill"
        assert "that fluency is the warning sign" in quill_reply["body_md"]
        assert "positive-disguise IS the failure signal" in quill_reply["body_md"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestSeedIdempotency:
    def test_second_call_is_noop(self, bare_conn):
        """Calling seed_threads twice yields the same counts."""
        seed_threads(bare_conn)
        threads_after_first = bare_conn.execute(
            "SELECT COUNT(*) FROM threads"
        ).fetchone()[0]
        posts_after_first = bare_conn.execute(
            "SELECT COUNT(*) FROM posts"
        ).fetchone()[0]

        seed_threads(bare_conn)  # second call
        threads_after_second = bare_conn.execute(
            "SELECT COUNT(*) FROM threads"
        ).fetchone()[0]
        posts_after_second = bare_conn.execute(
            "SELECT COUNT(*) FROM posts"
        ).fetchone()[0]

        assert threads_after_second == threads_after_first == 2
        assert posts_after_second == posts_after_first == 6

    def test_seed_threads_idempotent(self, bare_conn):
        """Calling seed_threads twice on a fresh DB is a no-op."""
        seed_threads(bare_conn)    # first plant
        seed_threads(bare_conn)    # second call — must be idempotent
        thread_count = bare_conn.execute(
            "SELECT COUNT(*) FROM threads"
        ).fetchone()[0]
        post_count = bare_conn.execute(
            "SELECT COUNT(*) FROM posts"
        ).fetchone()[0]
        assert thread_count == 2
        assert post_count == 6


# ---------------------------------------------------------------------------
# Graceful no-op when seeds dir is absent or empty
# ---------------------------------------------------------------------------

class TestSeedGracefulNoop:
    def test_absent_seeds_dir_is_noop(self, bare_conn, monkeypatch):
        """seed_threads() is a no-op if the seeds dir doesn't exist."""
        import forum.seed as seed_mod

        monkeypatch.setattr(
            seed_mod.os.path,
            "isdir",
            lambda path: False,
        )
        seed_threads(bare_conn)  # must not raise
        count = bare_conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        assert count == 0

    def test_empty_seeds_dir_is_noop(self, bare_conn, tmp_path, monkeypatch):
        """seed_threads() is a no-op if the seeds dir exists but is empty."""
        import forum.seed as seed_mod

        monkeypatch.setattr(
            seed_mod.os.path,
            "dirname",
            lambda _: str(tmp_path),
        )
        # tmp_path/seeds is an empty directory
        empty_seeds = tmp_path / "seeds"
        empty_seeds.mkdir()

        seed_threads(bare_conn)  # must not raise
        count = bare_conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# Bad category slug → RuntimeError (seed-authoring error)
# ---------------------------------------------------------------------------

class TestSeedBadCategory:
    def test_unknown_category_raises(self, bare_conn, tmp_path, monkeypatch):
        """A seed file referencing a non-existent category slug raises RuntimeError."""
        import forum.seed as seed_mod

        monkeypatch.setattr(
            seed_mod.os.path,
            "dirname",
            lambda _: str(tmp_path),
        )
        bad_seeds = tmp_path / "seeds"
        bad_seeds.mkdir()
        (bad_seeds / "bad-0-op.md").write_text(
            "---\nthread_key: bad\ntitle: Bad thread\ncategory: nonexistent-slug\n"
            "pinned: false\nauthor: iris\norder: 0\n---\nBody text.\n",
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="nonexistent-slug"):
            seed_threads(bare_conn)
