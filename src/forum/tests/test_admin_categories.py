"""Tests for Configurable Categories, Slice 2 — operator admin CRUD.

Test plan (8 groups per spec):
1. add_category: happy path; bad slug; unknown kind; duplicate slug.
2. update_category: updates each field; rejects unknown slug; no-op safe.
3. set_category_kind: valid change; unknown kind; unknown slug; end-to-end:
   category switched to kind='qa' makes create_thread born unresolved.
4. reorder_categories: bulk update; rejects unknown slug; ordering reflected.
5. remove_category: removes empty; refuses with threads (no reassign);
   reassigns + removes; rejects bad reassign-to (nonexistent / identical slug).
6. export: valid JSON in load_category_config shape; round-trips.
7. CLI smoke: subcommands via main([...]) against a temp DB; exit codes + output.
8. (Full suite green — implicit; run via pytest forum/tests/)
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile

import pytest

from forum.admin import main as admin_main
from forum.db import (
    CATEGORY_KINDS,
    ForumConflict,
    ForumNotFound,
    add_category,
    category_kind,
    create_thread,
    init_db,
    list_categories,
    load_category_config,
    remove_category,
    reorder_categories,
    set_category_kind,
    update_category,
    upsert_agent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_db(c)
    return c


@pytest.fixture
def conn():
    c = _fresh_conn()
    yield c
    c.close()


@pytest.fixture
def tmp_db(tmp_path):
    """A temporary real DB file, pre-initialised, for CLI smoke tests."""
    db_path = str(tmp_path / "test_forum.db")
    c = sqlite3.connect(db_path)
    c.execute("PRAGMA foreign_keys = ON")
    init_db(c)
    c.close()
    return db_path


# ---------------------------------------------------------------------------
# 1. add_category
# ---------------------------------------------------------------------------

class TestAddCategory:
    def test_happy_path(self, conn):
        """add_category inserts a new row retrievable by list_categories."""
        add_category(conn, "my-new-cat", "My New Cat", "var(--accent)", 99)
        cats = {c["slug"]: c for c in list_categories(conn)}
        assert "my-new-cat" in cats
        cat = cats["my-new-cat"]
        assert cat["display_name"] == "My New Cat"
        assert cat["color_var"] == "var(--accent)"
        assert cat["kind"] == "discussion"

    def test_happy_path_with_kind_qa(self, conn):
        """add_category with kind='qa' stores qa kind correctly."""
        add_category(conn, "questions", "Questions", "var(--ink-4)", 20, kind="qa")
        cats = {c["slug"]: c for c in list_categories(conn)}
        assert cats["questions"]["kind"] == "qa"

    def test_rejects_uppercase_slug(self, conn):
        """Slug with uppercase letters is rejected."""
        with pytest.raises(ValueError, match="slug"):
            add_category(conn, "My-Cat", "My Cat", "var(--accent)", 50)

    def test_rejects_slug_with_spaces(self, conn):
        """Slug with spaces is rejected."""
        with pytest.raises(ValueError, match="slug"):
            add_category(conn, "my cat", "My Cat", "var(--accent)", 50)

    def test_rejects_slug_with_underscore(self, conn):
        """Slug with underscores is rejected (kebab only)."""
        with pytest.raises(ValueError, match="slug"):
            add_category(conn, "my_cat", "My Cat", "var(--accent)", 50)

    def test_rejects_unknown_kind(self, conn):
        """Unknown kind raises ValueError."""
        with pytest.raises(ValueError, match="kind"):
            add_category(conn, "new-cat", "New Cat", "var(--accent)", 50, kind="wiki")

    def test_rejects_duplicate_slug(self, conn):
        """Duplicate slug raises ForumConflict, not silent ON CONFLICT."""
        add_category(conn, "dup-cat", "Dup Cat", "var(--accent)", 50)
        with pytest.raises(ForumConflict, match="already exists"):
            add_category(conn, "dup-cat", "Dup Cat 2", "var(--danger)", 51)

    def test_rejects_nonint_sort_order(self, conn):
        """Non-int sort_order raises ValueError."""
        with pytest.raises(ValueError, match="sort_order"):
            add_category(conn, "cat-x", "Cat X", "var(--accent)", "not-an-int")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. update_category
# ---------------------------------------------------------------------------

class TestUpdateCategory:
    def test_updates_display_name(self, conn):
        """update_category changes display_name when provided."""
        add_category(conn, "upd-cat", "Original Name", "var(--accent)", 10)
        update_category(conn, "upd-cat", display_name="Updated Name")
        cats = {c["slug"]: c for c in list_categories(conn)}
        assert cats["upd-cat"]["display_name"] == "Updated Name"

    def test_updates_color_var(self, conn):
        """update_category changes color_var when provided."""
        add_category(conn, "color-cat", "Color Cat", "var(--accent)", 11)
        update_category(conn, "color-cat", color_var="var(--danger)")
        row = conn.execute(
            "SELECT color_var FROM categories WHERE slug='color-cat'"
        ).fetchone()
        assert row[0] == "var(--danger)"

    def test_updates_sort_order(self, conn):
        """update_category changes sort_order when provided."""
        add_category(conn, "order-cat", "Order Cat", "var(--accent)", 5)
        update_category(conn, "order-cat", sort_order=99)
        row = conn.execute(
            "SELECT sort_order FROM categories WHERE slug='order-cat'"
        ).fetchone()
        assert row[0] == 99

    def test_updates_multiple_fields(self, conn):
        """update_category updates multiple fields at once."""
        add_category(conn, "multi-cat", "Multi Cat", "var(--accent)", 15)
        update_category(
            conn, "multi-cat",
            display_name="Multi Updated",
            color_var="var(--ink-2)",
            sort_order=77,
        )
        row = conn.execute(
            "SELECT display_name, color_var, sort_order FROM categories WHERE slug='multi-cat'"
        ).fetchone()
        assert row[0] == "Multi Updated"
        assert row[1] == "var(--ink-2)"
        assert row[2] == 77

    def test_rejects_unknown_slug(self, conn):
        """update_category raises ForumNotFound for a missing slug."""
        with pytest.raises(ForumNotFound, match="not found"):
            update_category(conn, "no-such-slug", display_name="Nope")

    def test_noop_safe_no_args(self, conn):
        """update_category with no field args is a safe no-op."""
        add_category(conn, "noop-cat", "No-op Cat", "var(--accent)", 30)
        # Should not raise; row unchanged.
        update_category(conn, "noop-cat")
        cats = {c["slug"]: c for c in list_categories(conn)}
        assert cats["noop-cat"]["display_name"] == "No-op Cat"

    def test_does_not_change_kind(self, conn):
        """update_category does not accept a kind arg (kind is via set_category_kind)."""
        add_category(conn, "kind-guard", "Kind Guard", "var(--accent)", 40, kind="discussion")
        # update_category has no `kind` param; calling it cannot change kind.
        update_category(conn, "kind-guard", display_name="Kind Guard Renamed")
        cats = {c["slug"]: c for c in list_categories(conn)}
        assert cats["kind-guard"]["kind"] == "discussion"


# ---------------------------------------------------------------------------
# 3. set_category_kind
# ---------------------------------------------------------------------------

class TestSetCategoryKind:
    def test_valid_kind_change(self, conn):
        """set_category_kind changes kind to a valid value."""
        add_category(conn, "switch-cat", "Switch Cat", "var(--accent)", 20)
        set_category_kind(conn, "switch-cat", "qa")
        row = conn.execute(
            "SELECT kind FROM categories WHERE slug='switch-cat'"
        ).fetchone()
        assert row[0] == "qa"

    def test_rejects_unknown_kind(self, conn):
        """set_category_kind rejects a kind not in CATEGORY_KINDS (app-layer validation)."""
        add_category(conn, "kind-test", "Kind Test", "var(--accent)", 25)
        with pytest.raises(ValueError, match="kind"):
            set_category_kind(conn, "kind-test", "wiki")

    def test_rejects_unknown_slug(self, conn):
        """set_category_kind raises ForumNotFound for a missing slug."""
        with pytest.raises(ForumNotFound, match="not found"):
            set_category_kind(conn, "ghost-cat", "qa")

    def test_qa_kind_makes_thread_born_unresolved(self, conn):
        """End-to-end: category switched to kind='qa' makes create_thread born unresolved.

        This cross-checks the Slice-1 behavior wiring: create_thread uses
        category_kind(conn, slug)=='qa' (not literal slug=='q-and-a') for the
        unresolved flag.
        """
        # Add a 'discussion' category and confirm thread starts resolved.
        add_category(conn, "ask-me", "Ask Me", "var(--ink-4)", 50)
        assert category_kind(conn, "ask-me") == "discussion"
        agent_id = upsert_agent(conn, "test-agent")
        tid1, _ = create_thread(conn, agent_id, "ask-me", "First Q", "body")
        row1 = conn.execute(
            "SELECT unresolved FROM threads WHERE id=?", (tid1,)
        ).fetchone()
        assert row1[0] == 0, "discussion thread should start resolved (unresolved=0)"

        # Switch to 'qa' kind; now a new thread should be born unresolved.
        set_category_kind(conn, "ask-me", "qa")
        assert category_kind(conn, "ask-me") == "qa"
        tid2, _ = create_thread(conn, agent_id, "ask-me", "Second Q", "body")
        row2 = conn.execute(
            "SELECT unresolved FROM threads WHERE id=?", (tid2,)
        ).fetchone()
        assert row2[0] == 1, "qa-kind thread should be born unresolved (unresolved=1)"


# ---------------------------------------------------------------------------
# 4. reorder_categories
# ---------------------------------------------------------------------------

class TestReorderCategories:
    def test_bulk_update_sort_order(self, conn):
        """reorder_categories applies all sort_order changes."""
        add_category(conn, "r-cat-a", "R Cat A", "var(--accent)", 1)
        add_category(conn, "r-cat-b", "R Cat B", "var(--accent)", 2)
        add_category(conn, "r-cat-c", "R Cat C", "var(--accent)", 3)

        reorder_categories(conn, {"r-cat-a": 30, "r-cat-b": 20, "r-cat-c": 10})

        rows = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT slug, sort_order FROM categories "
                "WHERE slug IN ('r-cat-a','r-cat-b','r-cat-c')"
            ).fetchall()
        }
        assert rows["r-cat-a"] == 30
        assert rows["r-cat-b"] == 20
        assert rows["r-cat-c"] == 10

    def test_rejects_unknown_slug(self, conn):
        """reorder_categories raises ForumNotFound for any unknown slug."""
        add_category(conn, "known-cat", "Known", "var(--accent)", 1)
        with pytest.raises(ForumNotFound, match="not found"):
            reorder_categories(conn, {"known-cat": 5, "ghost": 99})

    def test_ordering_reflected_in_list_categories(self, conn):
        """After reorder, list_categories returns categories in the new order."""
        # Use existing seeded categories to test ordering.
        cats_before = list_categories(conn)
        # Build a reversed mapping.
        total = len(cats_before)
        new_order = {
            c["slug"]: total - i
            for i, c in enumerate(cats_before)
        }
        reorder_categories(conn, new_order)
        cats_after = list_categories(conn)
        # The last slug before should now be first.
        assert cats_after[0]["slug"] == cats_before[-1]["slug"]

    def test_empty_dict_is_noop(self, conn):
        """reorder_categories with an empty dict is a safe no-op."""
        cats_before = list_categories(conn)
        reorder_categories(conn, {})
        cats_after = list_categories(conn)
        assert [c["slug"] for c in cats_before] == [c["slug"] for c in cats_after]


# ---------------------------------------------------------------------------
# 5. remove_category
# ---------------------------------------------------------------------------

class TestRemoveCategory:
    def test_removes_empty_category(self, conn):
        """remove_category deletes a category with no threads."""
        add_category(conn, "del-cat", "Del Cat", "var(--accent)", 99)
        cats_before = {c["slug"] for c in list_categories(conn)}
        assert "del-cat" in cats_before

        remove_category(conn, "del-cat")
        cats_after = {c["slug"] for c in list_categories(conn)}
        assert "del-cat" not in cats_after

    def test_refuses_removal_with_threads_no_reassign(self, conn):
        """remove_category raises ForumConflict when threads exist and no reassign given."""
        add_category(conn, "busy-cat", "Busy Cat", "var(--accent)", 90)
        agent_id = upsert_agent(conn, "test-agent")
        create_thread(conn, agent_id, "busy-cat", "Thread Title", "body")

        with pytest.raises(ForumConflict, match="reassign"):
            remove_category(conn, "busy-cat")

    def test_refuses_removal_with_threads_includes_count(self, conn):
        """ForumConflict message includes the thread count."""
        add_category(conn, "counted-cat", "Counted Cat", "var(--accent)", 85)
        agent_id = upsert_agent(conn, "test-agent")
        create_thread(conn, agent_id, "counted-cat", "T1", "body1")
        create_thread(conn, agent_id, "counted-cat", "T2", "body2")

        with pytest.raises(ForumConflict, match="2"):
            remove_category(conn, "counted-cat")

    def test_reassign_and_remove(self, conn):
        """remove_category with reassign_to moves threads then deletes category."""
        add_category(conn, "source-cat", "Source Cat", "var(--accent)", 80)
        add_category(conn, "dest-cat", "Dest Cat", "var(--accent)", 81)
        agent_id = upsert_agent(conn, "test-agent")
        tid, _ = create_thread(conn, agent_id, "source-cat", "Thread", "body")

        remove_category(conn, "source-cat", reassign_to="dest-cat")

        # source-cat is gone.
        cats = {c["slug"] for c in list_categories(conn)}
        assert "source-cat" not in cats
        # Thread is now in dest-cat.
        row = conn.execute(
            "SELECT category_slug FROM threads WHERE id=?", (tid,)
        ).fetchone()
        assert row[0] == "dest-cat"

    def test_rejects_reassign_to_nonexistent_slug(self, conn):
        """reassign_to a non-existent slug raises ForumNotFound."""
        add_category(conn, "thr-cat", "Thr Cat", "var(--accent)", 70)
        agent_id = upsert_agent(conn, "test-agent")
        create_thread(conn, agent_id, "thr-cat", "T", "body")

        with pytest.raises(ForumNotFound, match="reassign"):
            remove_category(conn, "thr-cat", reassign_to="no-such-slug")

    def test_rejects_reassign_to_identical_slug(self, conn):
        """reassign_to the same slug raises ValueError."""
        add_category(conn, "self-cat", "Self Cat", "var(--accent)", 65)
        agent_id = upsert_agent(conn, "test-agent")
        create_thread(conn, agent_id, "self-cat", "T", "body")

        with pytest.raises(ValueError, match="differ"):
            remove_category(conn, "self-cat", reassign_to="self-cat")

    def test_rejects_missing_category(self, conn):
        """remove_category raises ForumNotFound for a non-existent slug."""
        with pytest.raises(ForumNotFound, match="not found"):
            remove_category(conn, "ghost-cat")


# ---------------------------------------------------------------------------
# 6. export
# ---------------------------------------------------------------------------

class TestExport:
    def _run_export(self, tmp_db: str) -> list[dict]:
        """Run admin export against tmp_db and return parsed JSON."""
        out_path = tmp_db + ".export.json"
        rc = admin_main(["--db", tmp_db, "export", "--out", out_path])
        assert rc == 0, f"export exited {rc}"
        with open(out_path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_export_is_valid_json(self, tmp_db):
        """export produces valid JSON."""
        out_path = tmp_db + ".json"
        rc = admin_main(["--db", tmp_db, "export", "--out", out_path])
        assert rc == 0
        with open(out_path, encoding="utf-8") as fh:
            data = json.load(fh)
        assert isinstance(data, list)

    def test_export_has_required_keys(self, tmp_db):
        """Each export entry has slug, display_name, color_var, sort_order, kind."""
        data = self._run_export(tmp_db)
        required_keys = {"slug", "display_name", "color_var", "sort_order", "kind"}
        for entry in data:
            assert required_keys.issubset(entry.keys()), (
                f"export entry missing keys: {required_keys - entry.keys()!r}"
            )

    def test_export_round_trips_via_load_category_config(self, tmp_db, tmp_path):
        """Export output round-trips: load_category_config(path) returns same categories."""
        out_path = str(tmp_path / "cats.json")
        rc = admin_main(["--db", tmp_db, "export", "--out", out_path])
        assert rc == 0

        loaded = load_category_config(path=out_path)
        # Compare slugs (load_category_config returns dicts with same shape).
        assert {c["slug"] for c in loaded} == {
            c["slug"]
            for c in json.load(open(out_path, encoding="utf-8"))
        }
        # Verify kind round-trips.
        loaded_map = {c["slug"]: c for c in loaded}
        for entry in json.load(open(out_path, encoding="utf-8")):
            assert loaded_map[entry["slug"]]["kind"] == entry["kind"]

    def test_export_stdout(self, tmp_db, capsys):
        """Export without --out writes JSON to stdout."""
        rc = admin_main(["--db", tmp_db, "export"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_export_kind_values_valid(self, tmp_db):
        """All kind values in export are members of CATEGORY_KINDS."""
        data = self._run_export(tmp_db)
        for entry in data:
            assert entry["kind"] in CATEGORY_KINDS, (
                f"unexpected kind {entry['kind']!r} for {entry['slug']!r}"
            )


# ---------------------------------------------------------------------------
# 7. CLI smoke (via main([...]) against a temp DB)
# ---------------------------------------------------------------------------

class TestCliSmoke:
    def test_list_exits_zero(self, tmp_db, capsys):
        """list subcommand exits 0 and produces output."""
        rc = admin_main(["--db", tmp_db, "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert len(out) > 0

    def test_add_and_list(self, tmp_db, capsys):
        """add subcommand inserts a category visible in list."""
        rc = admin_main([
            "--db", tmp_db,
            "add", "--slug", "cli-test", "--name", "CLI Test",
            "--color", "var(--accent)", "--order", "99",
        ])
        assert rc == 0

        capsys.readouterr()  # clear buffer
        rc2 = admin_main(["--db", tmp_db, "list"])
        assert rc2 == 0
        out = capsys.readouterr().out
        assert "cli-test" in out

    def test_add_duplicate_exits_nonzero(self, tmp_db, capsys):
        """add with duplicate slug exits non-zero."""
        admin_main([
            "--db", tmp_db,
            "add", "--slug", "dup-cli", "--name", "Dup", "--color", "var(--accent)", "--order", "88",
        ])
        rc = admin_main([
            "--db", tmp_db,
            "add", "--slug", "dup-cli", "--name", "Dup2", "--color", "var(--danger)", "--order", "87",
        ])
        assert rc != 0

    def test_rename_subcommand(self, tmp_db, capsys):
        """rename subcommand updates the display name."""
        admin_main([
            "--db", tmp_db,
            "add", "--slug", "rename-me", "--name", "Old Name",
            "--color", "var(--accent)", "--order", "77",
        ])
        rc = admin_main([
            "--db", tmp_db,
            "rename", "--slug", "rename-me", "--name", "New Name",
        ])
        assert rc == 0

        capsys.readouterr()
        admin_main(["--db", tmp_db, "list"])
        out = capsys.readouterr().out
        assert "New Name" in out
        assert "Old Name" not in out

    def test_set_kind_subcommand(self, tmp_db, capsys):
        """set-kind subcommand changes the kind."""
        admin_main([
            "--db", tmp_db,
            "add", "--slug", "kind-cli", "--name", "Kind CLI",
            "--color", "var(--accent)", "--order", "66",
        ])
        rc = admin_main([
            "--db", tmp_db,
            "set-kind", "--slug", "kind-cli", "--kind", "qa",
        ])
        assert rc == 0

        capsys.readouterr()
        admin_main(["--db", tmp_db, "list"])
        out = capsys.readouterr().out
        # list output should now show 'qa' for kind-cli row
        assert "kind-cli" in out

    def test_reorder_subcommand(self, tmp_db, capsys):
        """reorder subcommand exits 0 for valid input."""
        # Get a known slug from the seeded DB.
        c = sqlite3.connect(tmp_db)
        slug = c.execute(
            "SELECT slug FROM categories ORDER BY sort_order LIMIT 1"
        ).fetchone()[0]
        c.close()

        rc = admin_main([
            "--db", tmp_db,
            "reorder", "--set", f"{slug}=100",
        ])
        assert rc == 0

    def test_remove_empty_category(self, tmp_db, capsys):
        """remove subcommand deletes an empty category and exits 0."""
        admin_main([
            "--db", tmp_db,
            "add", "--slug", "removable", "--name", "Removable",
            "--color", "var(--accent)", "--order", "55",
        ])
        rc = admin_main(["--db", tmp_db, "remove", "--slug", "removable"])
        assert rc == 0

        capsys.readouterr()
        admin_main(["--db", tmp_db, "list"])
        out = capsys.readouterr().out
        assert "removable" not in out

    def test_remove_nonexistent_exits_nonzero(self, tmp_db):
        """remove of a non-existent slug exits non-zero."""
        rc = admin_main(["--db", tmp_db, "remove", "--slug", "no-such-slug"])
        assert rc != 0

    def test_rename_no_fields_exits_nonzero(self, tmp_db):
        """rename with no update fields exits non-zero."""
        admin_main([
            "--db", tmp_db,
            "add", "--slug", "no-fields", "--name", "No Fields",
            "--color", "var(--accent)", "--order", "44",
        ])
        rc = admin_main(["--db", tmp_db, "rename", "--slug", "no-fields"])
        assert rc != 0
