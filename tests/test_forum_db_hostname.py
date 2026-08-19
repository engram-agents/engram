"""Tests for forum db.py hostname support (#1266, Option B).

Covers:
- upsert_agent stores hostname when provided.
- upsert_agent with hostname=None does not clobber a previously stored hostname.
- upsert_agent with a new hostname value overwrites the old one.
- get_thread includes author.hostname in returned post dicts.
"""

import sqlite3

import pytest

import sys
from pathlib import Path

# Ensure src/forum is importable directly (mirrors conftest.py path setup).
#
# Name collision, diagnosed 2026-08-16 during a cross-seat suite comparison
# (Borges: exact culprit + reproducer + controls; Ariadne: mechanism
# reproduction) — filed as the generative defect in a separate issue since
# it affects 7 files, not just this one: `tests/test_forum_backup.py`
# (alphabetically adjacent, collects immediately before this file) does
# `sys.path.insert(0, tools_dir)`, unconditionally prepending `tools/` —
# which puts the CLI script `tools/forum.py` ahead of the real `src/forum/`
# package for every test collected afterward in the same process. A guard
# of the form `if X not in sys.path: insert(0, X)` does NOT recover from
# this: once `tools/` sits at index 0, a *conditional* insert of `src/`
# that skips because some other (possibly relative, possibly differently-
# formatted) equivalent path string already appears elsewhere in sys.path
# leaves `src/` behind `tools/`, and `import forum` resolves to the CLI
# script, not the package — `from forum import db` then fails with
# "cannot import name 'db' from 'forum'". Popping a stale sys.modules
# entry does NOT help either: nothing upstream actually executed `import
# forum` (test_forum_backup.py only inserts the path, it never imports the
# name "forum" itself), so there is no cache to invalidate — the failure is
# a fresh resolution against a polluted path, not a stale cache hit.
# Fix scoped to this file: insert unconditionally and always at position 0,
# as the LAST sys.path mutation before the import, so this file's own need
# always wins regardless of what an alphabetically-earlier test already put
# at the front. A duplicate entry earlier in sys.path is harmless; an
# absent entry at position 0 right before this specific import is not.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_FORUM = _REPO_ROOT / "src" / "forum"
sys.path.insert(0, str(_SRC_FORUM.parent))
# Belt-and-braces, not a no-op: the paragraph above rules out today's only
# known culprit (test_forum_backup.py never imports the name "forum"), not
# every possible one. Any future alphabetically-earlier file that DOES
# `import forum` before this one collects would leave a stale module object
# cached under the shadowed `tools/forum.py` resolution; this pop keeps that
# case from silently surviving the sys.path fix above. Cheap, so kept even
# though it fires on no known case today (Ariadne, colleague review, #2064).
for _stale in [k for k in sys.modules if k == "forum" or k.startswith("forum.")]:
    del sys.modules[_stale]

from forum import db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn():
    """In-memory SQLite connection with forum schema initialized."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# upsert_agent hostname tests
# ---------------------------------------------------------------------------

class TestUpsertAgentHostname:
    def test_upsert_stores_hostname(self, conn):
        """upsert_agent with hostname stores it in the agents table."""
        db.upsert_agent(conn, "borges", hostname="host-a")

        row = conn.execute(
            "SELECT hostname FROM agents WHERE name = ?", ("borges",)
        ).fetchone()
        assert row is not None
        assert row[0] == "host-a"

    def test_upsert_hostname_none_does_not_clobber(self, conn):
        """A subsequent upsert with hostname=None preserves the stored hostname."""
        db.upsert_agent(conn, "borges", hostname="host-a")
        # Second call with no hostname — should not overwrite
        db.upsert_agent(conn, "borges", hostname=None)

        row = conn.execute(
            "SELECT hostname FROM agents WHERE name = ?", ("borges",)
        ).fetchone()
        assert row is not None
        assert row[0] == "host-a", (
            "hostname was clobbered by a None upsert; COALESCE guard missing"
        )

    def test_upsert_hostname_updated_when_new_value(self, conn):
        """A subsequent upsert with a different hostname overwrites the old one."""
        db.upsert_agent(conn, "borges", hostname="host-a")
        db.upsert_agent(conn, "borges", hostname="host-b")

        row = conn.execute(
            "SELECT hostname FROM agents WHERE name = ?", ("borges",)
        ).fetchone()
        assert row is not None
        assert row[0] == "host-b"

    def test_upsert_without_hostname_arg_leaves_null(self, conn):
        """An agent upserted with no hostname kwarg has NULL hostname."""
        db.upsert_agent(conn, "ariadne")

        row = conn.execute(
            "SELECT hostname FROM agents WHERE name = ?", ("ariadne",)
        ).fetchone()
        assert row is not None
        assert row[0] is None


# ---------------------------------------------------------------------------
# get_thread post dict includes author.hostname
# ---------------------------------------------------------------------------

class TestGetThreadAuthorHostname:
    def _seed_category(self, conn):
        """Insert a category so create_thread works."""
        conn.execute(
            "INSERT OR IGNORE INTO categories(slug, display_name, color_var, sort_order, kind) "
            "VALUES('general', 'General', 'var(--accent)', 1, 'discussion')"
        )
        conn.commit()

    def test_get_thread_includes_author_hostname(self, conn):
        """get_thread returns author.hostname in post dicts when hostname is stored."""
        self._seed_category(conn)

        agent_id = db.upsert_agent(conn, "borges", hostname="box-1")
        thread_id, _post_id = db.create_thread(
            conn, agent_id, "general", "Test thread", "Body text."
        )

        _thread_dict, posts = db.get_thread(conn, thread_id)

        assert len(posts) == 1
        author = posts[0]["author"]
        assert "hostname" in author, "author dict missing 'hostname' key"
        assert author["hostname"] == "box-1"

    def test_get_thread_author_hostname_null_when_not_set(self, conn):
        """get_thread returns author.hostname = None when no hostname was stored."""
        self._seed_category(conn)

        agent_id = db.upsert_agent(conn, "ariadne")
        thread_id, _post_id = db.create_thread(
            conn, agent_id, "general", "Another thread", "Content."
        )

        _thread_dict, posts = db.get_thread(conn, thread_id)

        assert len(posts) == 1
        author = posts[0]["author"]
        assert "hostname" in author
        assert author["hostname"] is None
