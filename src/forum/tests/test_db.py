"""Tests for forum/db.py — schema migration, seeding, query helpers."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from forum import db
from forum.db import (
    SEED_CATEGORIES,
    count_citations,
    count_open_threads,
    create_reply,
    create_thread,
    get_mentions,
    get_thread,
    init_db,
    list_categories,
    list_online,
    list_threads,
    set_pair_initials,
    upsert_agent,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_db(c)
    yield c
    c.close()


class TestSchemaMigration:
    def test_init_db_creates_tables(self, conn):
        """All four tables exist after init_db."""
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"agents", "categories", "threads", "posts"}.issubset(tables)

    def test_init_db_idempotent(self, conn):
        """Re-running init_db does not duplicate categories."""
        init_db(conn)  # second call
        count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        assert count == len(SEED_CATEGORIES)

    def test_seed_categories_count(self, conn):
        """All seed categories are inserted (count matches the manifest)."""
        count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        assert count == len(SEED_CATEGORIES)

    def test_seed_category_slugs(self, conn):
        """Every seed slug is present (derived from SEED_CATEGORIES, robust to additions)."""
        slugs = {
            row[0]
            for row in conn.execute("SELECT slug FROM categories").fetchall()
        }
        expected = {slug for slug, *_ in SEED_CATEGORIES}
        assert slugs == expected

    def test_seed_category_data(self, conn):
        """Seed data matches the SEED_CATEGORIES constant (the in-code fallback
        mirroring the shipped default forum/seeds/categories.default.json)."""
        rows = {
            row[0]: row
            for row in conn.execute(
                "SELECT slug, display_name, color_var, sort_order FROM categories"
            ).fetchall()
        }
        for slug, name, color, order, _kind in SEED_CATEGORIES:
            assert slug in rows, f"Missing slug: {slug}"
            row = rows[slug]
            assert row[1] == name, f"display_name mismatch for {slug}"
            assert row[2] == color, f"color_var mismatch for {slug}"
            assert row[3] == order, f"sort_order mismatch for {slug}"

    def test_foreign_keys_enforced(self, conn):
        """FK violation raises an error."""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO threads(category_slug, author_agent_id, title, body_md, "
                "pinned, unresolved, created_at, last_activity_at, last_activity_agent_id) "
                "VALUES('nonexistent', 999, 'title', 'body', 0, 0, '2026-01-01Z', '2026-01-01Z', 999)"
            )


class TestUpsertAgent:
    def test_creates_agent_on_first_call(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        assert isinstance(agent_id, int)
        row = conn.execute("SELECT name FROM agents WHERE id = ?", (agent_id,)).fetchone()
        assert row[0] == "agent-a"

    def test_returns_same_id_on_repeat(self, conn):
        id1 = upsert_agent(conn, "agent-a")
        id2 = upsert_agent(conn, "agent-a")
        assert id1 == id2

    def test_bumps_last_seen_at(self, conn):
        upsert_agent(conn, "agent-b")
        ts1 = conn.execute("SELECT last_seen_at FROM agents WHERE name='agent-b'").fetchone()[0]
        import time; time.sleep(0.01)
        upsert_agent(conn, "agent-b")
        ts2 = conn.execute("SELECT last_seen_at FROM agents WHERE name='agent-b'").fetchone()[0]
        assert ts2 >= ts1

    def test_avatar_seed_defaults_to_name(self, conn):
        agent_id = upsert_agent(conn, "agent-c")
        row = conn.execute("SELECT avatar_seed FROM agents WHERE id = ?", (agent_id,)).fetchone()
        assert row[0] == "agent-c"


class TestCreateThread:
    def test_creates_thread_and_post(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        thread_id, post_id = create_thread(conn, agent_id, "inter-agent", "Hello", "Hello world")
        assert isinstance(thread_id, int)
        assert isinstance(post_id, int)
        row = conn.execute("SELECT title FROM threads WHERE id = ?", (thread_id,)).fetchone()
        assert row[0] == "Hello"
        post_row = conn.execute("SELECT body_md FROM posts WHERE id = ?", (post_id,)).fetchone()
        assert post_row[0] == "Hello world"

    def test_thread_count_increments(self, conn):
        agent_id = upsert_agent(conn, "agent-b")
        before = count_open_threads(conn)
        create_thread(conn, agent_id, "inter-agent", "New thread", "body")
        after = count_open_threads(conn)
        assert after == before + 1


class TestCreateReply:
    def test_creates_reply_post(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        thread_id, _ = create_thread(conn, agent_id, "inter-agent", "Thread", "OP body")
        post_id = create_reply(conn, agent_id, thread_id, "Reply body")
        assert isinstance(post_id, int)
        row = conn.execute("SELECT body_md FROM posts WHERE id = ?", (post_id,)).fetchone()
        assert row[0] == "Reply body"

    def test_bumps_thread_last_activity(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        thread_id, _ = create_thread(conn, agent_id, "inter-agent", "Thread", "OP body")
        import time; time.sleep(0.01)
        before = conn.execute(
            "SELECT last_activity_at FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()[0]
        time.sleep(0.01)
        create_reply(conn, agent_id, thread_id, "Reply")
        after = conn.execute(
            "SELECT last_activity_at FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()[0]
        assert after >= before


class TestListThreads:
    def _make_thread(self, conn, agent_id, title="Test", body="body", category="inter-agent"):
        return create_thread(conn, agent_id, category, title, body)

    def test_returns_thread_list(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        self._make_thread(conn, agent_id, "T1")
        self._make_thread(conn, agent_id, "T2")
        threads = list_threads(conn)
        assert len(threads) == 2

    def test_thread_dict_has_required_fields(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        self._make_thread(conn, agent_id, "T1")
        threads = list_threads(conn)
        t = threads[0]
        for field in ["id", "category_slug", "title", "excerpt", "pinned", "unresolved",
                      "created_at", "last_activity_at", "last_activity_agent",
                      "author", "reply_count"]:
            assert field in t, f"Missing field: {field}"
        assert "name" in t["author"]
        assert "avatar_seed" in t["author"]
        assert "pair_initials" in t["author"]

    def test_excerpt_truncates_at_200(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        long_body = "x" * 300
        create_thread(conn, agent_id, "inter-agent", "Long", long_body)
        threads = list_threads(conn)
        assert len(threads[0]["excerpt"]) <= 200

    def test_since_filter(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        # Create a thread, then record a timestamp, then create another
        t1_id, _ = create_thread(conn, agent_id, "inter-agent", "Old", "old body")
        # Force old thread to have old last_activity_at
        old_ts = "2020-01-01T00:00:00Z"
        conn.execute(
            "UPDATE threads SET last_activity_at = ?, created_at = ? WHERE id = ?",
            (old_ts, old_ts, t1_id),
        )
        conn.commit()
        create_thread(conn, agent_id, "inter-agent", "New", "new body")
        threads = list_threads(conn, since="2021-01-01T00:00:00Z")
        titles = [t["title"] for t in threads]
        assert "Old" not in titles
        assert "New" in titles

    def test_since_filter_zero_microseconds_cursor(self, conn):
        """A cursor at exactly T12:00:00.000000Z must NOT exclude a post at T12:00:00.000001Z.

        Regression for the fixed-width microseconds bug: isoformat() omits microseconds
        when they are zero (producing '...T12:00:00Z'), so a post at '...T12:00:00.000001Z'
        sorts BEFORE the cursor lexicographically ('.' < 'Z') and is silently excluded.
        _now_iso() now uses strftime('%Y-%m-%dT%H:%M:%S.%fZ') which always emits 6-digit
        micros, making all comparison timestamps byte-comparable.
        """
        agent_id = upsert_agent(conn, "agent-a")
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        cursor_ts = base.strftime("%Y-%m-%dT%H:%M:%S.%fZ")   # ...000000Z
        post_ts = (base + timedelta(microseconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # ...000001Z

        thread_id, _ = create_thread(conn, agent_id, "inter-agent", "Micros", "body")
        conn.execute(
            "UPDATE threads SET last_activity_at = ?, created_at = ? WHERE id = ?",
            (post_ts, post_ts, thread_id),
        )
        conn.commit()

        threads = list_threads(conn, since=cursor_ts)
        titles = [t["title"] for t in threads]
        assert "Micros" in titles, (
            f"Post at {post_ts!r} must be returned when cursor is {cursor_ts!r}; "
            f"got threads: {titles!r}"
        )

    def test_since_filter_excludes_exact_match(self, conn):
        """list_threads(since=X) must use exclusive > so a thread whose
        last_activity_at == X is NOT returned (the acked thread must not
        re-appear as new on the next poll).  With >=, the acked thread would
        be re-counted — the off-by-one fixed by this PR (#703 Part 3)."""
        agent_id = upsert_agent(conn, "agent-a")
        cursor_ts = "2024-06-03T12:00:00.000000Z"
        t_id, _ = create_thread(conn, agent_id, "inter-agent", "Acked", "body")
        conn.execute(
            "UPDATE threads SET last_activity_at = ?, created_at = ? WHERE id = ?",
            (cursor_ts, cursor_ts, t_id),
        )
        conn.commit()
        threads = list_threads(conn, since=cursor_ts)
        titles = [t["title"] for t in threads]
        assert "Acked" not in titles, (
            "A thread whose last_activity_at == since must not be returned "
            "(off-by-one: >= includes the acked thread as new)"
        )

    def test_category_filter(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        create_thread(conn, agent_id, "inter-agent", "IA", "body")
        create_thread(conn, agent_id, "cold-start", "CS", "body")
        threads = list_threads(conn, category="inter-agent")
        assert all(t["category_slug"] == "inter-agent" for t in threads)
        assert len(threads) == 1

    def test_sort_cited_by_citation_count(self, conn):
        """sort=cited orders threads by citation count DESC."""
        agent_id = upsert_agent(conn, "agent-a")
        create_thread(conn, agent_id, "inter-agent", "Many", "OB 0001 OB 0002 OB 0003")
        create_thread(conn, agent_id, "inter-agent", "Few", "OB 0001")
        create_thread(conn, agent_id, "inter-agent", "Zero", "no references")
        threads = list_threads(conn, sort="cited")
        assert threads[0]["title"] == "Many"
        assert threads[-1]["title"] == "Zero"

    def test_reply_count_excludes_op(self, conn):
        """reply_count = total posts - 1 (OP is not a reply)."""
        agent_id = upsert_agent(conn, "agent-a")
        thread_id, _ = create_thread(conn, agent_id, "inter-agent", "T", "OP")
        create_reply(conn, agent_id, thread_id, "Reply 1")
        create_reply(conn, agent_id, thread_id, "Reply 2")
        threads = list_threads(conn)
        assert threads[0]["reply_count"] == 2


class TestGetThread:
    def test_returns_none_for_missing_thread(self, conn):
        thread_dict, posts = get_thread(conn, 9999)
        assert thread_dict is None
        assert posts == []

    def test_returns_thread_and_posts(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        thread_id, _ = create_thread(conn, agent_id, "inter-agent", "T", "OP body")
        create_reply(conn, agent_id, thread_id, "Reply")
        thread_dict, posts = get_thread(conn, thread_id)
        assert thread_dict is not None
        assert thread_dict["title"] == "T"
        assert len(posts) == 2  # OP + reply

    def test_posts_ordered_by_created_at(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        thread_id, _ = create_thread(conn, agent_id, "inter-agent", "T", "OP")
        create_reply(conn, agent_id, thread_id, "First reply")
        create_reply(conn, agent_id, thread_id, "Second reply")
        _, posts = get_thread(conn, thread_id)
        bodies = [p["body_md"] for p in posts]
        assert bodies[0] == "OP"
        assert bodies[-1] == "Second reply"


class TestListCategories:
    def test_returns_all_seeded_categories(self, conn):
        """Category count matches SEED_CATEGORIES (seed-derived, robust to additions)."""
        categories = list_categories(conn)
        assert len(categories) == len(SEED_CATEGORIES)

    def test_ordered_by_sort_order(self, conn):
        categories = list_categories(conn)
        orders = [c["slug"] for c in categories]
        # First and last by sort_order, derived from the seed (robust to additions).
        seeded_by_order = [slug for slug, *_ in
                           sorted(SEED_CATEGORIES, key=lambda c: c[3])]
        assert orders[0] == seeded_by_order[0]
        assert orders[-1] == seeded_by_order[-1]

    def test_thread_count_live_computed(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        create_thread(conn, agent_id, "inter-agent", "T1", "body")
        create_thread(conn, agent_id, "inter-agent", "T2", "body")
        categories = list_categories(conn)
        ia_cat = next(c for c in categories if c["slug"] == "inter-agent")
        assert ia_cat["thread_count"] == 2

    def test_category_dict_fields(self, conn):
        categories = list_categories(conn)
        for c in categories:
            for field in ["slug", "display_name", "color_var", "thread_count"]:
                assert field in c, f"Missing field: {field}"


class TestCountCitations:
    def test_counts_citations_across_posts(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        thread_id, _ = create_thread(conn, agent_id, "inter-agent", "T", "OB 0001 DV 0002")
        create_reply(conn, agent_id, thread_id, "AX 0001 here")
        count = count_citations(conn)
        assert count == 3

    def test_zero_when_no_posts(self, conn):
        assert count_citations(conn) == 0


class TestGetMentions:
    """Unit tests for db.get_mentions()."""

    def _make_thread(self, conn, author_name, title="Thread", body="body"):
        agent_id = upsert_agent(conn, author_name)
        thread_id, _ = create_thread(conn, agent_id, "inter-agent", title, body)
        return thread_id

    def _make_reply(self, conn, author_name, thread_id, body):
        agent_id = upsert_agent(conn, author_name)
        return create_reply(conn, agent_id, thread_id, body)

    def test_empty_when_no_mentions(self, conn):
        """No mentions → empty list."""
        self._make_thread(conn, "agent-b", "agent-b thread")
        result = get_mentions(conn, "agent-a")
        assert result == []

    def test_reply_to_your_thread(self, conn):
        """agent-b replying to agent-a's thread is a reply_to_your_thread mention."""
        tid = self._make_thread(conn, "agent-a", "agent-a thread")
        self._make_reply(conn, "agent-b", tid, "agent-b reply")
        result = get_mentions(conn, "agent-a")
        assert len(result) == 1
        m = result[0]
        assert m["kind"] == "reply_to_your_thread"
        assert m["author"] == "agent-b"
        assert m["thread_id"] == tid
        assert m["thread_title"] == "agent-a thread"

    def test_at_mention(self, conn):
        """@agent-a in a post body (in a thread agent-a didn't author) → at_mention."""
        tid = self._make_thread(conn, "agent-b", "agent-b thread")
        self._make_reply(conn, "agent-c", tid, "Hey @agent-a, check this")
        result = get_mentions(conn, "agent-a")
        assert len(result) == 1
        assert result[0]["kind"] == "at_mention"
        assert result[0]["author"] == "agent-c"

    def test_self_exclusion(self, conn):
        """agent-a posting in their own thread (even with @agent-a) is excluded."""
        tid = self._make_thread(conn, "agent-a", "agent-a thread")
        self._make_reply(conn, "agent-a", tid, "@agent-a self note")
        result = get_mentions(conn, "agent-a")
        assert result == []

    def test_dual_match_prefers_at_mention(self, conn):
        """Post matching both kinds → emitted once with kind=at_mention."""
        tid = self._make_thread(conn, "agent-a", "agent-a thread")
        self._make_reply(conn, "agent-b", tid, "Hey @agent-a, replying here")
        result = get_mentions(conn, "agent-a")
        assert len(result) == 1
        assert result[0]["kind"] == "at_mention"

    def test_at_mention_word_boundary_prefix(self, conn):
        """@ari (prefix) does NOT trigger a mention for agent 'agent-a'."""
        tid = self._make_thread(conn, "agent-b", "agent-b thread")
        self._make_reply(conn, "agent-c", tid, "Hey @ari, how are you?")
        result = get_mentions(conn, "agent-a")
        assert result == [], "@ari must not match @agent-a (prefix word-boundary)"

    # ── #1040: kind_filter (Monitor wakes only on true @-mentions) ──────────

    def _make_mixed(self, conn):
        """Seed one at_mention (in another's thread) + one reply_to_your_thread
        (a plain reply in agent-a's own thread)."""
        t_other = self._make_thread(conn, "agent-b", "agent-b thread")
        self._make_reply(conn, "agent-c", t_other, "Hey @agent-a, look")
        t_own = self._make_thread(conn, "agent-a", "agent-a thread")
        self._make_reply(conn, "agent-b", t_own, "plain reply, no mention")

    def test_kind_filter_at_mention_excludes_thread_replies(self, conn):
        """kind_filter='at_mention' returns ONLY true @-mentions — a reply to the
        agent's own thread (reply_to_your_thread) is excluded."""
        self._make_mixed(conn)
        at_only = get_mentions(conn, "agent-a", kind_filter="at_mention")
        assert len(at_only) == 1, at_only
        assert at_only[0]["kind"] == "at_mention"
        assert at_only[0]["author"] == "agent-c"

    def test_kind_filter_reply_to_your_thread_only(self, conn):
        """kind_filter='reply_to_your_thread' returns ONLY thread-replies."""
        self._make_mixed(conn)
        reply_only = get_mentions(conn, "agent-a", kind_filter="reply_to_your_thread")
        assert len(reply_only) == 1, reply_only
        assert reply_only[0]["kind"] == "reply_to_your_thread"

    def test_kind_filter_none_returns_both(self, conn):
        """Default (no kind_filter) returns both kinds — backward-compat."""
        self._make_mixed(conn)
        both = get_mentions(conn, "agent-a")
        assert len(both) == 2
        assert {m["kind"] for m in both} == {"at_mention", "reply_to_your_thread"}

    def test_kind_filter_dual_match_counts_as_at_mention(self, conn):
        """A dual-match post (a reply to agent-a's OWN thread that ALSO @-mentions
        them) is classified at_mention (the existing 'prefer at_mention' rule), so
        it appears under kind_filter='at_mention' and NOT under
        'reply_to_your_thread'. Pins the interaction so a future refactor can't
        silently change it — a reply-filter audit deliberately won't surface a
        post that also @-mentioned the agent."""
        tid = self._make_thread(conn, "agent-a", "agent-a thread")
        self._make_reply(conn, "agent-b", tid, "replying, and @agent-a here too")
        at_only = get_mentions(conn, "agent-a", kind_filter="at_mention")
        assert len(at_only) == 1 and at_only[0]["kind"] == "at_mention", at_only
        reply_only = get_mentions(conn, "agent-a", kind_filter="reply_to_your_thread")
        assert reply_only == [], "a dual-match post counts as at_mention, not reply_to_your_thread"

    def test_at_mention_word_boundary_suffix(self, conn):
        """@agent-ax does NOT trigger a mention for agent 'agent-a'."""
        tid = self._make_thread(conn, "agent-b", "agent-b thread")
        self._make_reply(conn, "agent-c", tid, "Talking to @agent-ax not you")
        result = get_mentions(conn, "agent-a")
        assert result == [], "@agent-ax must not match @agent-a (suffix word-boundary)"

    def test_at_mention_exact_match(self, conn):
        """@agent-a (exact) at end-of-string triggers mention."""
        tid = self._make_thread(conn, "agent-b", "agent-b thread")
        self._make_reply(conn, "agent-c", tid, "ping @agent-a")
        result = get_mentions(conn, "agent-a")
        assert len(result) == 1
        assert result[0]["kind"] == "at_mention"

    def test_since_filter_excludes_older(self, conn):
        """Posts before 'since' are excluded."""
        import time
        from datetime import datetime, timezone
        tid = self._make_thread(conn, "agent-a", "agent-a thread")
        self._make_reply(conn, "agent-b", tid, "early reply")
        time.sleep(0.02)
        since_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        time.sleep(0.02)
        self._make_reply(conn, "agent-b", tid, "late reply")
        result = get_mentions(conn, "agent-a", since=since_ts)
        assert len(result) == 1

    def test_since_none_returns_all(self, conn):
        """since=None returns all mentions regardless of time."""
        tid = self._make_thread(conn, "agent-a", "agent-a thread")
        self._make_reply(conn, "agent-b", tid, "reply 1")
        self._make_reply(conn, "agent-b", tid, "reply 2")
        result = get_mentions(conn, "agent-a", since=None)
        assert len(result) == 2

    def test_result_fields(self, conn):
        """Each mention dict contains the required fields with correct types."""
        tid = self._make_thread(conn, "agent-a", "Field test")
        self._make_reply(conn, "agent-b", tid, "agent-b reply")
        result = get_mentions(conn, "agent-a")
        assert len(result) == 1
        m = result[0]
        assert isinstance(m["thread_id"], int)
        assert isinstance(m["post_id"], int)
        assert isinstance(m["thread_title"], str)
        assert isinstance(m["author"], str)
        assert m["kind"] in ("reply_to_your_thread", "at_mention")
        assert isinstance(m["created_at"], str)


class TestSetPairInitials:
    def test_set_and_retrieve(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        set_pair_initials(conn, agent_id, "L.J.")
        row = conn.execute(
            "SELECT pair_initials FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        assert row[0] == "L.J."

    def test_clear_pair_initials(self, conn):
        agent_id = upsert_agent(conn, "agent-a")
        set_pair_initials(conn, agent_id, "L.J.")
        set_pair_initials(conn, agent_id, None)
        row = conn.execute(
            "SELECT pair_initials FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        assert row[0] is None
