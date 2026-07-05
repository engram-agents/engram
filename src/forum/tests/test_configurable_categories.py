"""Tests for Configurable Categories, Slice 1.

Covers:
1. Default seeding: fresh DB with no config → 8 categories; q-and-a kind='qa'.
2. Config precedence: explicit path > env > default; explicit path beats env.
3. kind migration/backfill: pre-kind DB gets q-and-a backfilled to 'qa'.
4. Behavior follows kind, not slug: a non-'q-and-a' slug with kind='qa' gets
   question semantics; a discussion-kind thread does not.
5. Malformed config fallback: bad explicit file → ValueError; all-absent →
   SEED_CATEGORIES fallback + still seeds.
6. Existing suite green (this file itself is the new-test portion of that check).
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from forum.db import (
    SEED_CATEGORIES,
    ForumConflict,
    accept_answer,
    category_kind,
    create_reply,
    create_thread,
    init_db,
    list_categories,
    load_category_config,
    upsert_agent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@pytest.fixture
def conn():
    c = _fresh_conn()
    init_db(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 1. Default seeding
# ---------------------------------------------------------------------------

class TestDefaultSeeding:
    def test_eight_categories_seeded(self, conn):
        """Fresh DB with no config file seeds exactly 8 categories."""
        count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        assert count == 8

    def test_qa_category_has_kind_qa(self, conn):
        """q-and-a category is seeded with kind='qa'."""
        row = conn.execute(
            "SELECT kind FROM categories WHERE slug='q-and-a'"
        ).fetchone()
        assert row is not None
        assert row[0] == "qa"

    def test_non_qa_categories_have_kind_discussion(self, conn):
        """All categories except q-and-a have kind='discussion'."""
        rows = conn.execute(
            "SELECT slug, kind FROM categories WHERE slug != 'q-and-a'"
        ).fetchall()
        assert len(rows) == 7
        for row in rows:
            assert row[1] == "discussion", (
                f"Expected kind='discussion' for slug='{row[0]}', got '{row[1]}'"
            )

    def test_list_categories_includes_kind(self, conn):
        """list_categories() output includes 'kind' for each category."""
        categories = list_categories(conn)
        for cat in categories:
            assert "kind" in cat, f"Missing 'kind' in category dict for slug={cat['slug']}"
        qa_cat = next(c for c in categories if c["slug"] == "q-and-a")
        assert qa_cat["kind"] == "qa"


# ---------------------------------------------------------------------------
# 2. Config precedence
# ---------------------------------------------------------------------------

class TestConfigPrecedence:
    def _minimal_config(self, slug="hello", kind="discussion") -> list[dict]:
        return [
            {
                "slug": slug,
                "display_name": "Hello",
                "color_var": "var(--accent)",
                "sort_order": 1,
                "kind": kind,
            }
        ]

    def test_explicit_path_overrides_default(self, tmp_path):
        """Explicit path arg overrides the shipped default."""
        cfg = tmp_path / "cats.json"
        cfg.write_text(json.dumps(self._minimal_config("custom-slug")))

        c = _fresh_conn()
        init_db(c, categories_config=str(cfg))
        count = c.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        assert count == 1
        row = c.execute("SELECT slug FROM categories").fetchone()
        assert row[0] == "custom-slug"
        c.close()

    def test_explicit_path_beats_env(self, tmp_path, monkeypatch):
        """Explicit path beats FORUM_CATEGORIES_CONFIG env."""
        env_cfg = tmp_path / "env_cats.json"
        env_cfg.write_text(json.dumps(self._minimal_config("from-env")))
        arg_cfg = tmp_path / "arg_cats.json"
        arg_cfg.write_text(json.dumps(self._minimal_config("from-arg")))

        monkeypatch.setenv("FORUM_CATEGORIES_CONFIG", str(env_cfg))

        result = load_category_config(path=str(arg_cfg))
        slugs = [c["slug"] for c in result]
        assert slugs == ["from-arg"], (
            f"Expected explicit arg to win, got slugs: {slugs}"
        )

    def test_env_honored_when_no_explicit_arg(self, tmp_path, monkeypatch):
        """FORUM_CATEGORIES_CONFIG env is used when no explicit path arg."""
        env_cfg = tmp_path / "env_cats.json"
        env_cfg.write_text(json.dumps(self._minimal_config("from-env")))
        monkeypatch.setenv("FORUM_CATEGORIES_CONFIG", str(env_cfg))

        result = load_category_config(path=None)
        slugs = [c["slug"] for c in result]
        assert slugs == ["from-env"]

    def test_default_json_used_when_no_env_no_user_override(self, monkeypatch):
        """Shipped default JSON is used when no arg, no env, no ~/.forum/categories.json."""
        monkeypatch.delenv("FORUM_CATEGORIES_CONFIG", raising=False)
        # Ensure the user-override path doesn't exist on the test machine.
        user_path = os.path.expanduser("~/.forum/categories.json")
        if os.path.exists(user_path):
            pytest.skip("~/.forum/categories.json exists on this machine; skipping default-only test")

        result = load_category_config(path=None)
        slugs = {c["slug"] for c in result}
        assert "q-and-a" in slugs
        qa = next(c for c in result if c["slug"] == "q-and-a")
        assert qa["kind"] == "qa"

    def test_kind_defaults_to_discussion_when_absent(self, tmp_path, monkeypatch):
        """A config entry without 'kind' defaults to 'discussion'."""
        monkeypatch.delenv("FORUM_CATEGORIES_CONFIG", raising=False)
        cfg = tmp_path / "cats.json"
        # Entry without 'kind' key
        cfg.write_text(json.dumps([{
            "slug": "no-kind",
            "display_name": "No Kind",
            "color_var": "var(--accent)",
            "sort_order": 1,
        }]))
        result = load_category_config(path=str(cfg))
        assert result[0]["kind"] == "discussion"


# ---------------------------------------------------------------------------
# 3. kind migration / backfill
# ---------------------------------------------------------------------------

class TestKindMigration:
    def test_backfill_existing_qa_slug_without_kind_column(self):
        """A pre-kind DB (categories table has no kind column) is migrated:
        q-and-a gets kind='qa', others get 'discussion'."""
        c = _fresh_conn()

        # Simulate pre-kind schema (no kind column).
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
        # Pre-populate q-and-a without kind.
        c.execute(
            "INSERT INTO categories(slug, display_name, color_var, sort_order) "
            "VALUES('q-and-a', 'Q&A', 'var(--ink-4)', 8)"
        )
        c.commit()

        # kind column absent before migration.
        cols_before = {r[1] for r in c.execute("PRAGMA table_info(categories)")}
        assert "kind" not in cols_before

        # Run init_db.
        init_db(c)

        # kind column present after migration.
        cols_after = {r[1] for r in c.execute("PRAGMA table_info(categories)")}
        assert "kind" in cols_after

        # q-and-a backfilled to 'qa'.
        row = c.execute("SELECT kind FROM categories WHERE slug='q-and-a'").fetchone()
        assert row[0] == "qa"

        c.close()

    def test_backfill_idempotent(self):
        """Running init_db twice does not error and q-and-a stays kind='qa'."""
        c = _fresh_conn()
        init_db(c)
        # First run: q-and-a → qa via seeding.
        row = c.execute("SELECT kind FROM categories WHERE slug='q-and-a'").fetchone()
        assert row[0] == "qa"

        # Second run must not raise.
        init_db(c)
        row2 = c.execute("SELECT kind FROM categories WHERE slug='q-and-a'").fetchone()
        assert row2[0] == "qa"
        c.close()

    def test_pre_kind_non_qa_slugs_get_discussion(self):
        """Non-q-and-a categories in a pre-kind DB get kind='discussion' after migration."""
        c = _fresh_conn()
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
        c.execute(
            "INSERT INTO categories(slug, display_name, color_var, sort_order) "
            "VALUES('inter-agent', 'Inter-agent', 'var(--ink-2)', 6)"
        )
        c.commit()

        init_db(c)

        row = c.execute("SELECT kind FROM categories WHERE slug='inter-agent'").fetchone()
        # The seeded entry has no kind; ALTER TABLE ADD COLUMN DEFAULT 'discussion'
        # covers it. The seed loop uses ON CONFLICT DO NOTHING, so the existing row
        # keeps the DEFAULT-assigned 'discussion'.
        assert row[0] == "discussion"
        c.close()


# ---------------------------------------------------------------------------
# 4. Behavior follows kind, not slug
# ---------------------------------------------------------------------------

class TestBehaviorFollowsKind:
    def _conn_with_custom_qa_slug(self, slug: str) -> sqlite3.Connection:
        """Return a connection with a single category with kind='qa' at the given slug."""
        c = _fresh_conn()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                avatar_seed TEXT NOT NULL, pair_initials TEXT,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, hostname TEXT
            );
            CREATE TABLE IF NOT EXISTS categories (
                slug TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                color_var TEXT NOT NULL, sort_order INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'discussion'
            );
            CREATE TABLE IF NOT EXISTS threads (
                id INTEGER PRIMARY KEY,
                category_slug TEXT NOT NULL REFERENCES categories(slug),
                author_agent_id INTEGER NOT NULL REFERENCES agents(id),
                title TEXT NOT NULL, body_md TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                unresolved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,
                last_activity_agent_id INTEGER NOT NULL REFERENCES agents(id),
                accepted_answer_post_id INTEGER REFERENCES posts(id)
            );
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY,
                thread_id INTEGER NOT NULL REFERENCES threads(id),
                author_agent_id INTEGER NOT NULL REFERENCES agents(id),
                body_md TEXT NOT NULL, parent_post_id INTEGER REFERENCES posts(id),
                created_at TEXT NOT NULL, edited_at TEXT
            );
            CREATE TABLE IF NOT EXISTS post_verifications (
                id INTEGER PRIMARY KEY,
                post_id INTEGER NOT NULL REFERENCES posts(id),
                verifier_agent_id INTEGER NOT NULL REFERENCES agents(id),
                note TEXT NOT NULL CHECK(length(trim(note)) > 0),
                created_at TEXT NOT NULL,
                UNIQUE(post_id, verifier_agent_id)
            );
        """)
        # Insert the custom qa-kind category.
        c.execute(
            "INSERT INTO categories(slug, display_name, color_var, sort_order, kind) "
            "VALUES(?, ?, ?, ?, ?)",
            (slug, "Custom QA", "var(--accent)", 1, "qa"),
        )
        # Also insert a discussion category for contrast.
        c.execute(
            "INSERT INTO categories(slug, display_name, color_var, sort_order, kind) "
            "VALUES('discussion-cat', 'Discussion', 'var(--ink-2)', 2, 'discussion')",
        )
        c.commit()
        return c

    def test_non_qa_slug_with_qa_kind_born_unresolved(self):
        """A category with slug != 'q-and-a' but kind='qa' produces unresolved=1 threads."""
        c = self._conn_with_custom_qa_slug("questions")
        agent_id = upsert_agent(c, "agent-a")
        tid, _ = create_thread(c, agent_id, "questions", "A question?", "body")
        row = c.execute("SELECT unresolved FROM threads WHERE id=?", (tid,)).fetchone()
        assert row[0] == 1, "Thread in kind='qa' category should be born unresolved=1"
        c.close()

    def test_non_qa_slug_with_qa_kind_accept_answer_permitted(self):
        """accept_answer is permitted in a kind='qa' category regardless of slug."""
        c = self._conn_with_custom_qa_slug("questions")
        asker_id = upsert_agent(c, "agent-a")
        answerer_id = upsert_agent(c, "agent-b")
        tid, _ = create_thread(c, asker_id, "questions", "A question?", "body")
        answer_id = create_reply(c, answerer_id, tid, "An answer")

        # Should not raise.
        accept_answer(c, tid, answer_id, asker_id)

        row = c.execute(
            "SELECT accepted_answer_post_id, unresolved FROM threads WHERE id=?", (tid,)
        ).fetchone()
        assert row[0] == answer_id
        assert row[1] == 0
        c.close()

    def test_discussion_kind_thread_born_resolved(self):
        """A category with kind='discussion' produces unresolved=0 threads."""
        c = self._conn_with_custom_qa_slug("questions")
        agent_id = upsert_agent(c, "agent-a")
        tid, _ = create_thread(c, agent_id, "discussion-cat", "A topic", "body")
        row = c.execute("SELECT unresolved FROM threads WHERE id=?", (tid,)).fetchone()
        assert row[0] == 0, "Thread in kind='discussion' category should be born unresolved=0"
        c.close()

    def test_discussion_kind_accept_answer_raises_conflict(self):
        """accept_answer raises ForumConflict in a kind='discussion' category."""
        c = self._conn_with_custom_qa_slug("questions")
        agent_id = upsert_agent(c, "agent-a")
        tid, _ = create_thread(c, agent_id, "discussion-cat", "A topic", "body")
        answer_id = create_reply(c, agent_id, tid, "A reply")

        with pytest.raises(ForumConflict, match="question categories"):
            accept_answer(c, tid, answer_id, agent_id)
        c.close()

    def test_renamed_qa_slug_keeps_semantics(self):
        """Slug renamed to 'tech-questions' with kind='qa' retains question semantics."""
        c = self._conn_with_custom_qa_slug("tech-questions")
        agent_id = upsert_agent(c, "agent-a")
        kind = category_kind(c, "tech-questions")
        assert kind == "qa"
        tid, _ = create_thread(c, agent_id, "tech-questions", "Tech Q?", "body")
        row = c.execute("SELECT unresolved FROM threads WHERE id=?", (tid,)).fetchone()
        assert row[0] == 1
        c.close()

    def test_category_kind_returns_discussion_for_unknown_slug(self, conn):
        """category_kind returns 'discussion' for a slug not in the DB."""
        assert category_kind(conn, "nonexistent-slug") == "discussion"


# ---------------------------------------------------------------------------
# 5. Malformed config fallback
# ---------------------------------------------------------------------------

class TestMalformedConfig:
    def test_bad_json_in_explicit_file_raises_value_error(self, tmp_path):
        """A present-but-malformed JSON file raises ValueError naming the file."""
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")

        with pytest.raises(ValueError, match=str(bad)):
            load_category_config(path=str(bad))

    def test_unknown_kind_raises_value_error(self, tmp_path):
        """An entry with unknown kind raises ValueError naming slug + kind."""
        cfg = tmp_path / "cats.json"
        cfg.write_text(json.dumps([{
            "slug": "weird-cat",
            "display_name": "Weird",
            "color_var": "var(--accent)",
            "sort_order": 1,
            "kind": "not-a-valid-kind",
        }]))
        with pytest.raises(ValueError, match="weird-cat"):
            load_category_config(path=str(cfg))

    def test_missing_required_key_raises_value_error(self, tmp_path):
        """An entry missing a required key raises ValueError."""
        cfg = tmp_path / "cats.json"
        cfg.write_text(json.dumps([{
            "slug": "incomplete",
            "display_name": "Missing sort_order",
            "color_var": "var(--accent)",
            # sort_order intentionally absent
        }]))
        with pytest.raises(ValueError, match="sort_order"):
            load_category_config(path=str(cfg))

    def test_all_absent_falls_back_to_seed_categories(self, tmp_path, monkeypatch):
        """When nothing resolves, load_category_config falls back to SEED_CATEGORIES."""
        # Ensure no env override and user path doesn't exist during the test.
        monkeypatch.delenv("FORUM_CATEGORIES_CONFIG", raising=False)
        # Guard: a real ~/.forum/categories.json is candidate 3, hit BEFORE the
        # emergency fallback — without this guard the test is environment-dependent.
        if os.path.exists(os.path.expanduser("~/.forum/categories.json")):
            pytest.skip("~/.forum/categories.json exists on this machine; skipping emergency-fallback test")

        # Point to a non-existent default path by temporarily monkeypatching the module.
        import forum.db as db_module
        original = db_module._DEFAULT_CATEGORIES_JSON
        db_module._DEFAULT_CATEGORIES_JSON = str(tmp_path / "nonexistent.json")
        try:
            result = load_category_config(path=None)
        finally:
            db_module._DEFAULT_CATEGORIES_JSON = original

        # Should return the SEED_CATEGORIES fallback.
        result_slugs = {c["slug"] for c in result}
        seed_slugs = {slug for slug, *_ in SEED_CATEGORIES}
        assert result_slugs == seed_slugs

    def test_all_absent_still_seeds_categories(self, tmp_path, monkeypatch):
        """Emergency fallback still allows init_db to seed categories."""
        monkeypatch.delenv("FORUM_CATEGORIES_CONFIG", raising=False)
        # Guard: same environment-dependence as above — skip if a real user
        # override exists, since it would be resolved before the fallback.
        if os.path.exists(os.path.expanduser("~/.forum/categories.json")):
            pytest.skip("~/.forum/categories.json exists on this machine; skipping emergency-fallback test")

        import forum.db as db_module
        original = db_module._DEFAULT_CATEGORIES_JSON
        db_module._DEFAULT_CATEGORIES_JSON = str(tmp_path / "nonexistent.json")
        try:
            c = _fresh_conn()
            init_db(c)
            count = c.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
            assert count == len(SEED_CATEGORIES)
            qa_row = c.execute(
                "SELECT kind FROM categories WHERE slug='q-and-a'"
            ).fetchone()
            assert qa_row[0] == "qa"
            c.close()
        finally:
            db_module._DEFAULT_CATEGORIES_JSON = original
