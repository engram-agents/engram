"""Tests for FTS5 index coverage of reported_state and trigger_text — issue #403.

Background:
    nodes_fts is a FTS5 contentless table wired via content='nodes'. Before #403,
    it only indexed claim, quoted_text, and interpretation — the three fields
    present for most node types. feeling_report nodes are unique in that their
    primary phenomenological content lives in reported_state (and, secondarily,
    in trigger_text); the auto-formatted claim field only captures a truncated
    summary ("I reported feeling: ..."). The three-column schema meant that
    engram_query could not match a feeling_report by the words describing the
    actual felt state — a systematic under-recall of the most self-specific nodes.

    Fix: extend nodes_fts to 5 columns (add reported_state, trigger_text), update
    the three FTS triggers accordingly, and add a user_version=2 migration that
    drops+rebuilds the FTS table and backfills all non-retracted nodes so existing
    installs pick up the fix automatically.

Coverage:
    1. test_reported_state_indexed_by_fts — file a feeling_report with a unique
       token in reported_state; verify direct FTS MATCH finds it.
    2. test_trigger_text_indexed_by_fts — file a feeling_report with a unique
       token in trigger_text; verify direct FTS MATCH finds it.
    3. test_migration_backfills_feeling_report_fields — simulate a pre-migration
       DB state (feeling_report exists but reported_state is not in the old
       3-column FTS index), run the migration SQL directly, verify reported_state
       is now findable via FTS MATCH.

Import-pattern contract (#948 wave 0 pilot — R1):
    This file imports engram_core and engram_tasks directly and contains no
    ``import server``. The module objects are passed to fresh_module() (R4) for
    per-test reload isolation.
"""

import atexit
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add tests/ dir so conftest.fresh_module is importable both under pytest and
# when the file is run as a standalone script.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_tasks

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_fts_feeling_report_fields_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _make_fresh_mods():
    """Reload engram_core and engram_tasks, configure a clean test DB.

    Uses conftest.fresh_module(engram_core) per R4: the module object is
    passed (not a string), so the ``import engram_core`` at the top of
    this file is the AST-visible dependency edge for run_touched_tests.py.

    Returns (core, tasks) — freshly reloaded module objects pointing to TEST_DIR.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    # Purge server + engram_* from sys.modules and reload engram_core.
    core = fresh_module(engram_core)
    # engram_tasks is not a transitive dep of engram_core — import fresh now.
    tasks = importlib.import_module("engram_tasks")
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return core, tasks


def _fts_match_rowids(token: str) -> set:
    """Return set of rowids matching the FTS token via the MATCH operator.

    Uses `WHERE nodes_fts MATCH ?` which queries the FTS index directly.
    NOTE: For FTS5 contentless tables, SELECT WHERE rowid=? scans the content
    table (nodes), not the FTS index — so it returns the rowid regardless of
    whether it has been removed from the index. The correct check for FTS index
    presence is a MATCH query on a distinctive token that was in the indexed text.
    """
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?", (token,)
    ).fetchall()
    conn.close()
    return {r["rowid"] for r in rows}


def _get_node_rowid(node_id: str) -> int:
    """Return the SQLite rowid for a node by its ENGRAM ID."""
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT rowid FROM nodes WHERE id = ?", (node_id,)).fetchone()
    conn.close()
    assert row is not None, f"Node {node_id} not found in DB"
    return row["rowid"]


# ---------------------------------------------------------------------------
# Test 1: reported_state is indexed — FTS MATCH finds the unique token
# ---------------------------------------------------------------------------

def test_reported_state_indexed_by_fts():
    """FTS index covers reported_state: a token in reported_state is findable.

    Files a feeling_report whose reported_state contains a unique token not
    present in the claim, quoted_text, or interpretation. Verifies that a
    direct FTS MATCH query on that token returns the node's rowid.

    Before #403, this test would fail because reported_state was not in the
    3-column FTS schema and the token would not be indexed.
    """
    core, tasks = _make_fresh_mods()

    token = "fts403reportedstate_uniquetokenA1"
    result = json.loads(tasks._report_feeling_impl(
        reported_state=f"I noticed a distinct {token} quality in my processing.",
        trigger="Testing FTS coverage of reported_state field for issue #403.",
        categorical_tag="curiosity",
    ))
    assert "feeling_id" in result or "node_id" in result or "id" in result, (
        f"_report_feeling_impl failed: {result}"
    )
    # Determine the node ID from whichever key the impl returns.
    node_id = result.get("feeling_id") or result.get("node_id") or result.get("id")
    assert node_id, f"No node ID in result: {result}"
    rowid = _get_node_rowid(node_id)

    # The unique token is in reported_state — must match via FTS index.
    matched = _fts_match_rowids(token)
    assert rowid in matched, (
        f"Token '{token}' (in reported_state) must match rowid {rowid} via FTS MATCH. "
        f"Matched rowids: {matched}. "
        "Before #403 fix: reported_state was not in the 3-column FTS schema."
    )


# ---------------------------------------------------------------------------
# Test 2: trigger_text is indexed — FTS MATCH finds the unique token
# ---------------------------------------------------------------------------

def test_trigger_text_indexed_by_fts():
    """FTS index covers trigger_text: a token in trigger (trigger_text) is findable.

    Files a feeling_report whose trigger field contains a unique token not
    present in the claim, quoted_text, or interpretation. Verifies that a
    direct FTS MATCH query on that token returns the node's rowid.

    Note: _report_feeling_impl's ``trigger`` parameter maps to the ``trigger_text``
    column in the nodes table.

    Before #403, this test would fail because trigger_text was not in the
    3-column FTS schema.
    """
    core, tasks = _make_fresh_mods()

    token = "fts403triggertext_uniquetokenB2"
    result = json.loads(tasks._report_feeling_impl(
        reported_state="I noticed something worth recording about my internal state.",
        trigger=f"The exchange that precipitated this had a {token} character.",
        categorical_tag="",
    ))
    assert "feeling_id" in result or "node_id" in result or "id" in result, (
        f"_report_feeling_impl failed: {result}"
    )
    node_id = result.get("feeling_id") or result.get("node_id") or result.get("id")
    assert node_id, f"No node ID in result: {result}"
    rowid = _get_node_rowid(node_id)

    # The unique token is in trigger_text — must match via FTS index.
    matched = _fts_match_rowids(token)
    assert rowid in matched, (
        f"Token '{token}' (in trigger_text) must match rowid {rowid} via FTS MATCH. "
        f"Matched rowids: {matched}. "
        "Before #403 fix: trigger_text was not in the 3-column FTS schema."
    )


# ---------------------------------------------------------------------------
# Test 3: migration backfills reported_state into nodes_fts for existing nodes
# ---------------------------------------------------------------------------

def test_migration_backfills_feeling_report_fields():
    """Migration rebuilds nodes_fts with 5 columns and backfills existing nodes.

    Simulates a pre-migration DB state:
    1. Trigger DB creation via _report_feeling_impl so the nodes table and schema
       exist. Then tear down the 5-column FTS and replace it with the old 3-column
       schema, simulating a pre-#403 install.
    2. Insert a node whose reported_state has a unique token not present in claim.
       Insert it into the 3-column FTS (without reported_state).
    3. Verify the pre-migration gap: rs_token is NOT found via FTS MATCH.
    4. Run the migration SQL directly — as _init_db would on next startup.
    5. Verify the token IS now found via FTS MATCH.

    The migration SQL is exercised directly here (not via a second _init_db call)
    because the schema init is monolithic inside _get_db() and re-calling it on
    the test DB is fragile. The migration statement is the load-bearing check.
    """
    core, tasks = _make_fresh_mods()

    # Trigger DB creation by filing a throw-away feeling report.
    _init = json.loads(tasks._report_feeling_impl(
        reported_state="Initial node to force DB creation.",
        trigger="DB init trigger for migration test.",
    ))
    assert "feeling_id" in _init or "node_id" in _init or "id" in _init, (
        f"Failed to create initial node: {_init}"
    )

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rs_token = "fts403migration_rsonly_uniquetokenC3x"
    node_id = "fl_9998"  # synthetic node ID for this test
    now = "2026-01-01T00:00:00Z"

    # Step 1: Tear down 5-column FTS and rebuild old 3-column schema.
    conn.execute("DROP TABLE IF EXISTS nodes_fts_vocab")
    conn.execute("DROP TABLE IF EXISTS nodes_fts")
    conn.execute("DROP TRIGGER IF EXISTS nodes_fts_insert")
    conn.execute("DROP TRIGGER IF EXISTS nodes_fts_delete")
    conn.execute("DROP TRIGGER IF EXISTS nodes_retract_remove_from_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        )
        """
    )
    conn.commit()

    # Step 2: Insert a feeling_report node with rs_token ONLY in reported_state.
    conn.execute(
        """
        INSERT INTO nodes (id, type, claim, created_at, status, reported_state, trigger_text)
        VALUES (?, 'feeling_report', ?, ?, 'active', ?, ?)
        """,
        (
            node_id,
            "I reported feeling: something",
            now,
            f"I noticed a distinct {rs_token} quality.",
            "Testing the migration backfill path for issue #403.",
        ),
    )
    conn.commit()
    rowid = conn.execute("SELECT rowid FROM nodes WHERE id = ?", (node_id,)).fetchone()["rowid"]
    # Insert into old 3-column FTS without rs_token (only claim indexed, no reported_state).
    conn.execute(
        "INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation) VALUES (?, ?, '', '')",
        (rowid, "I reported feeling: something"),
    )
    conn.commit()
    conn.close()

    # Confirm the gap: rs_token is NOT found via FTS MATCH (pre-migration state).
    pre_migration_match = _fts_match_rowids(rs_token)
    assert rowid not in pre_migration_match, (
        f"Token '{rs_token}' must NOT match rowid {rowid} in pre-migration 3-column FTS. "
        f"Matched: {pre_migration_match}. "
        "This confirms the pre-#403 gap that the migration is designed to fix."
    )

    # Step 3: Run the migration SQL directly — same as _init_db would on next startup.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.execute("DROP TABLE IF EXISTS nodes_fts_vocab")
    conn.execute("DROP TABLE IF EXISTS nodes_fts")
    conn.execute("DROP TRIGGER IF EXISTS nodes_fts_insert")
    conn.execute("DROP TRIGGER IF EXISTS nodes_fts_delete")
    conn.execute("DROP TRIGGER IF EXISTS nodes_retract_remove_from_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation, reported_state, trigger_text,
            content='nodes', content_rowid='rowid'
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER nodes_fts_insert AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
            VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation, new.reported_state, new.trigger_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER nodes_fts_delete AFTER DELETE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
            VALUES ('delete', old.rowid, old.claim, old.quoted_text, old.interpretation, old.reported_state, old.trigger_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER nodes_retract_remove_from_fts
        AFTER UPDATE OF status ON nodes
        WHEN NEW.status = 'retracted' AND COALESCE(OLD.status, '') != 'retracted'
        BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
            VALUES ('delete', OLD.rowid,
                    COALESCE(OLD.claim, ''),
                    COALESCE(OLD.quoted_text, ''),
                    COALESCE(OLD.interpretation, ''),
                    COALESCE(OLD.reported_state, ''),
                    COALESCE(OLD.trigger_text, ''));
        END
        """
    )
    # Backfill all non-retracted nodes into the new 5-column schema.
    conn.execute(
        """
        INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
        SELECT rowid,
               COALESCE(claim, ''),
               COALESCE(quoted_text, ''),
               COALESCE(interpretation, ''),
               COALESCE(reported_state, ''),
               COALESCE(trigger_text, '')
        FROM nodes
        WHERE status != 'retracted'
        """
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    # Step 4: Verify the token IS now found after migration.
    post_migration_match = _fts_match_rowids(rs_token)
    assert rowid in post_migration_match, (
        f"Token '{rs_token}' (in reported_state) must match rowid {rowid} after migration. "
        f"Matched: {post_migration_match}. "
        "The migration backfill must index reported_state into the new 5-column nodes_fts."
    )


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed = []
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS  {t.__name__}")
        except Exception:
            failed.append(t.__name__)
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        sys.exit(1)
