"""Tests for the nodes_fts trigger that removes retracted nodes — issue #274.

Background:
    nodes_fts is a FTS5 contentless table wired via content='nodes'. It does
    not auto-update on UPDATE — so retracted nodes (status='retracted',
    is_current=0) previously leaked through FTS queries.

    Fix: nodes_retract_remove_from_fts trigger (server.py) fires AFTER UPDATE
    OF status WHEN NEW.status = 'retracted' AND OLD.is_current = 1. It issues
    a FTS5 'delete' magic-insert to remove the old column values from the index.

    Migration: a one-shot migration also runs at startup to clean pre-existing
    retracted nodes from the index.

    Design rev 2026-07-02 (#195): superseded nodes are NOW also removed from the
    FTS index by a separate trigger (nodes_fts_supersede_remove, AFTER UPDATE OF
    is_current WHEN is_current flips 1→0 AND status != 'retracted').  The retract
    trigger has OLD.is_current=1 guard so it does not double-delete an already-
    superseded node.

Coverage:
    1. test_retract_removes_from_fts — trigger path: retract fires trigger,
       node disappears from direct FTS rowid query.
    2. test_retract_does_not_remove_non_retracted_supersession — superseded
       node removed from FTS by supersede trigger; retracted node also absent;
       neither interferes with the other.
    3. test_retract_migration_cleans_existing_retracted — one-shot migration
       cleans pre-existing retracted nodes on next _init_db call.
    4. test_query_post_retract_does_not_return_retracted — end-to-end:
       engram_query excludes retracted nodes both before and after retraction,
       and also with include_superseded=True after #274.
    5. test_supersede_then_retract_still_removes_from_fts — node superseded
       first (is_current→0, removed from FTS by supersede trigger) then retracted
       (retract trigger does not fire on already-superseded node); node remains
       absent from FTS throughout.

Import-pattern contract (#948 wave 0 pilot — R1):
    This file imports engram_revision, engram_observation, engram_query, and
    engram_core directly and contains no ``import server``. The module objects
    are passed to fresh_module() (R4) for per-test reload isolation.
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
import engram_observation
import engram_query
import engram_revision

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_fts_retract_trigger_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _make_fresh_mods():
    """Reload all family modules, configure a clean test DB, and return modules.

    Uses conftest.fresh_module(engram_revision) per R4: the module object is
    passed (not a string), so the ``import engram_revision`` at the top of
    this file is the AST-visible dependency edge for run_touched_tests.py.

    Returns (rev, obs, qry, core) — freshly reloaded module objects with
    engram_core pointing to TEST_DIR.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    # Purge server + engram_* from sys.modules and reload engram_revision
    # (which also reloads its dep engram_core).
    rev = fresh_module(engram_revision)
    # engram_observation and engram_query are not transitive deps of
    # engram_revision — import them fresh now that sys.modules is clean.
    obs = importlib.import_module("engram_observation")
    qry = importlib.import_module("engram_query")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, qry, core


def _add_observation(obs, claim: str, quoted_text: str = None, url: str = "https://example.com/fts274") -> str:
    qt = quoted_text if quoted_text is not None else claim[:80]
    r = json.loads(obs._add_observation_impl(
        url=url,
        title="FTS274 Test Source",
        quoted_text=qt,
        interpretation="Test interpretation for FTS trigger tests.",
        claim=claim,
        quote_type="hard_data",
    ))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


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
# Test 1: trigger path — retract fires trigger, node disappears from FTS
# ---------------------------------------------------------------------------

def test_retract_removes_from_fts():
    """Trigger path: retracted node is removed from nodes_fts immediately.

    Files a node with a distinctive token. Confirms it appears in FTS via a MATCH
    query. Retracts it. Confirms the token no longer matches in the FTS index.

    NOTE: For FTS5 contentless tables, `SELECT WHERE rowid=?` scans the content
    table (nodes), not the FTS index. The correct presence check is a MATCH query
    on a distinctive token from the indexed text.
    """
    rev, obs, _qry, _core = _make_fresh_mods()

    token = "fts274retracttrigger_uniquetokenA"
    ob_id = _add_observation(
        obs,
        claim=f"Claim containing {token} for trigger test.",
        quoted_text=f"Quote with {token} in it.",
    )
    rowid = _get_node_rowid(ob_id)

    # Before retraction: token matches in FTS index.
    assert rowid in _fts_match_rowids(token), (
        f"Token '{token}' should match rowid {rowid} in nodes_fts before retraction."
    )

    # Retract the node.
    ret = json.loads(rev._retract_impl(
        node_id=ob_id,
        error_type="other",
        reason="FTS trigger regression test #274 — test 1",
    ))
    assert ret.get("status") == "retracted", f"retract failed: {ret}"

    # After retraction: token must not match in FTS index.
    assert rowid not in _fts_match_rowids(token), (
        f"Token '{token}' must not match rowid {rowid} in nodes_fts after retraction. "
        "The nodes_retract_remove_from_fts trigger should have fired and removed "
        "the node from the FTS index."
    )


# ---------------------------------------------------------------------------
# Test 2: superseded nodes stay in FTS; only retracted ones are removed
# ---------------------------------------------------------------------------

def test_retract_does_not_remove_non_retracted_supersession():
    """Superseded nodes are removed from nodes_fts by the supersede trigger;
    retracting an unrelated node does not affect the superseded node's FTS state.

    Files two nodes A (to be superseded) and B (to be retracted). Supersedes A
    with a replacement C — the supersede trigger removes A from FTS. Retracts B —
    the retract trigger removes B from FTS. Verifies both are absent.

    Design rev 2026-07-02 (#195): superseded nodes are no longer kept in FTS.
    They remain in the graph and are reachable via edge traversal / engram_list.
    """
    rev, obs, _qry, _core = _make_fresh_mods()

    token_a = "fts274superseded_uniquetokenA2"
    token_b = "fts274retracted_uniquetokenB2"

    # Node A: will be superseded.
    a_id = _add_observation(
        obs,
        claim=f"Superseded claim {token_a} for trigger test 2.",
        quoted_text=f"Quote with {token_a}.",
        url="https://example.com/fts274-a",
    )
    rowid_a = _get_node_rowid(a_id)

    # Node C: replacement for A.
    c_id = _add_observation(
        obs,
        claim=f"Replacement claim {token_a} refined for trigger test 2.",
        quoted_text=f"Quote with {token_a} refined.",
        url="https://example.com/fts274-c",
    )

    # Node B: will be retracted.
    b_id = _add_observation(
        obs,
        claim=f"Retracted claim {token_b} for trigger test 2.",
        quoted_text=f"Quote with {token_b}.",
        url="https://example.com/fts274-b",
    )
    rowid_b = _get_node_rowid(b_id)

    # Supersede A with C — A becomes non-current but NOT retracted.
    sup = json.loads(rev._supersede_impl(
        a_id, c_id, supersede_reason="FTS trigger regression test #274 — test 2"
    ))
    assert sup.get("status") == "superseded", f"supersede failed: {sup}"

    # Retract B.
    ret = json.loads(rev._retract_impl(
        node_id=b_id,
        error_type="other",
        reason="FTS trigger regression test #274 — test 2",
    ))
    assert ret.get("status") == "retracted", f"retract failed: {ret}"

    # A (superseded) must be ABSENT from nodes_fts — removed by supersede trigger.
    assert rowid_a not in _fts_match_rowids(token_a), (
        f"Token '{token_a}' must not match superseded rowid {rowid_a}. "
        "The supersede trigger (nodes_fts_supersede_remove) removes nodes from FTS "
        "when is_current flips to 0 (#195)."
    )

    # B (retracted) must also be absent — removed by the retract trigger.
    assert rowid_b not in _fts_match_rowids(token_b), (
        f"Token '{token_b}' must not match retracted rowid {rowid_b}. "
        "Retracted nodes are removed from nodes_fts by the retract trigger."
    )


# ---------------------------------------------------------------------------
# Test 3: one-shot migration cleans pre-existing retracted nodes
# ---------------------------------------------------------------------------

def test_retract_migration_cleans_existing_retracted():
    """One-shot migration removes pre-existing retracted nodes from nodes_fts.

    Simulates the state that existed before #274: a node is inserted into
    nodes_fts at add time, then retracted via a direct SQL UPDATE that bypasses
    the FTS trigger (by dropping the trigger first), leaving the retracted node
    in the FTS index. Then the migration SQL is run directly — as it would be
    run inside _get_db() on next server startup. The migration must remove it.

    The migration SQL is exercised directly here (not via a second _get_db() call)
    because the schema init is monolithic inside _get_db() and re-calling it on
    the test DB is fragile. The migration statement is the load-bearing check.

    The migration is gated by PRAGMA user_version: _get_db() sets user_version=1
    after the migration runs, so re-running it on subsequent startups is a no-op.
    The FTS5 'delete' magic-insert is NOT idempotent — a second delete on an
    already-absent rowid raises "database disk image is malformed". The gate
    prevents that.

    NOTE: FTS5 contentless-table presence is checked via MATCH on a distinctive
    token, not via SELECT WHERE rowid=? (which scans the content table).
    """
    _rev, obs, _qry, _core = _make_fresh_mods()

    token = "fts274migration_uniquetokenC3"
    ob_id = _add_observation(
        obs,
        claim=f"Pre-existing retracted claim {token} for migration test.",
        quoted_text=f"Quote with {token}.",
    )
    rowid = _get_node_rowid(ob_id)

    # Confirm the node is in FTS before the simulated bypass.
    assert rowid in _fts_match_rowids(token), (
        f"Token '{token}' should match rowid {rowid} before simulated retraction."
    )

    # Simulate pre-trigger retraction: UPDATE nodes directly without firing the
    # FTS trigger. Drop the trigger, do the update, then leave trigger dropped —
    # the migration SQL is what we're testing, not the trigger.
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("DROP TRIGGER IF EXISTS nodes_retract_remove_from_fts")
    conn.execute(
        "UPDATE nodes SET status = 'retracted', is_current = 0 WHERE id = ?",
        (ob_id,),
    )
    conn.commit()

    # At this point: retracted in nodes, still indexed in nodes_fts (pre-#274 state).
    # Confirm the stale FTS entry is still there.
    assert rowid in _fts_match_rowids(token), (
        f"Token '{token}' should still match rowid {rowid} after bypassed retraction "
        "(simulates pre-trigger state that the migration must clean up)."
    )

    # Run the migration SQL directly — same statement as in _get_db().
    conn.execute(
        """
        INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation)
        SELECT 'delete', rowid,
               COALESCE(claim, ''),
               COALESCE(quoted_text, ''),
               COALESCE(interpretation, '')
        FROM nodes
        WHERE status = 'retracted'
        """
    )
    conn.commit()
    conn.close()

    # After migration: the pre-existing retracted node must be gone from FTS index.
    assert rowid not in _fts_match_rowids(token), (
        f"Token '{token}' must not match rowid {rowid} after migration SQL runs. "
        "The one-shot startup migration in _get_db() (#274) must clean pre-existing "
        "retracted nodes from nodes_fts."
    )


# ---------------------------------------------------------------------------
# Test 4: end-to-end — engram_query never returns retracted nodes
# ---------------------------------------------------------------------------

def test_query_post_retract_does_not_return_retracted():
    """End-to-end: engram_query excludes retracted nodes before and after #274.

    Files a node with a distinctive token. Queries and confirms it surfaces.
    Retracts it. Queries again — must not surface with include_superseded=False
    OR include_superseded=True (the bug documented in #274).
    """
    rev, obs, qry, _core = _make_fresh_mods()

    token = "fts274endtoend_uniquetokenD4"
    ob_id = _add_observation(
        obs,
        claim=f"End-to-end retract FTS test claim {token}.",
        quoted_text=f"Quote containing {token} end-to-end.",
    )

    def _query_ids(include_superseded: bool) -> set:
        r = json.loads(qry._query_impl(
            query=token,
            include_superseded=include_superseded,
            top_k=20,
            return_debug=True,
        ))
        return {n["id"] for n in r.get("results", [])}

    # Before retraction: node surfaces in both modes.
    ids_before = _query_ids(include_superseded=False)
    assert ob_id in ids_before, (
        f"Node {ob_id} must surface before retraction (include_superseded=False). "
        f"Results: {ids_before}"
    )

    # Retract.
    ret = json.loads(rev._retract_impl(
        node_id=ob_id,
        error_type="other",
        reason="FTS end-to-end retract test #274",
    ))
    assert ret.get("status") == "retracted", f"retract failed: {ret}"

    # After retraction: must not surface with flag=False.
    ids_false = _query_ids(include_superseded=False)
    assert ob_id not in ids_false, (
        f"Retracted node {ob_id} must not appear with include_superseded=False. "
        f"Results: {ids_false}"
    )

    # After retraction: must not surface with flag=True either (#274 fix).
    ids_true = _query_ids(include_superseded=True)
    assert ob_id not in ids_true, (
        f"Retracted node {ob_id} must not appear with include_superseded=True after #274. "
        "The nodes_retract_remove_from_fts trigger removes it from nodes_fts at retraction "
        "time, so it is absent from FTS results regardless of the flag. "
        f"Results: {ids_true}"
    )


# ---------------------------------------------------------------------------
# Test 5: supersede-then-retract path — trigger fires on status transition
# ---------------------------------------------------------------------------

def test_supersede_then_retract_still_removes_from_fts():
    """A node that was first superseded, then retracted, is absent from FTS throughout.

    Design rev 2026-07-02 (#195): the supersede trigger (nodes_fts_supersede_remove)
    removes the node from FTS when is_current flips to 0.  By the time the
    subsequent retract fires, the node is already absent — the retract trigger
    (nodes_retract_remove_from_fts) has OLD.is_current=1 guard so it does NOT
    fire on an already-superseded node, preventing a double-delete.

    Net result: node is absent from FTS after supersede AND after retract.
    """
    rev, obs, qry, _core = _make_fresh_mods()

    token_a = "fts280supretract_uniquetokenA5"
    token_b = "fts280supretract_uniquetokenB5"

    # Node A: will be superseded, then retracted.
    a_id = _add_observation(
        obs,
        claim=f"Supersede-then-retract claim {token_a} for trigger test 5.",
        quoted_text=f"Quote with {token_a} to be superseded then retracted.",
        url="https://example.com/fts280-a5",
    )
    rowid_a = _get_node_rowid(a_id)

    # Node B: the superseder (just needs to exist).
    b_id = _add_observation(
        obs,
        claim=f"Superseder claim {token_b} for trigger test 5.",
        quoted_text=f"Quote with {token_b} as superseder.",
        url="https://example.com/fts280-b5",
    )

    # Before anything: A is in FTS.
    assert rowid_a in _fts_match_rowids(token_a), (
        f"Token '{token_a}' should match rowid {rowid_a} in nodes_fts before supersede."
    )

    # Step 1: Supersede A with B — A becomes is_current=0, status='superseded'.
    sup = json.loads(rev._supersede_impl(
        a_id, b_id, supersede_reason="FTS trigger regression test #280 — test 5 step 1"
    ))
    assert sup.get("status") == "superseded", f"supersede failed: {sup}"

    # After supersede: A is already ABSENT from FTS — supersede trigger fired.
    assert rowid_a not in _fts_match_rowids(token_a), (
        f"Token '{token_a}' must NOT match rowid {rowid_a} after supersede. "
        "The supersede trigger (nodes_fts_supersede_remove) removes nodes from FTS "
        "when is_current flips to 0 (#195)."
    )

    # Step 2: Retract A — now is_current=0 AND status transitions to 'retracted'.
    ret = json.loads(rev._retract_impl(
        node_id=a_id,
        error_type="other",
        reason="FTS trigger regression test #280 — test 5 step 2",
    ))
    assert ret.get("status") == "retracted", f"retract failed: {ret}"

    # After retract: A remains absent from FTS (was already removed by supersede trigger;
    # retract trigger has OLD.is_current=1 guard so it does not fire on this path).
    assert rowid_a not in _fts_match_rowids(token_a), (
        f"Token '{token_a}' must NOT match rowid {rowid_a} after supersede+retract. "
        "Node was removed from FTS by the supersede trigger; retract trigger correctly "
        "does not fire (OLD.is_current was already 0)."
    )

    # Also verify via end-to-end query: include_superseded=True must not return A.
    query_result = json.loads(qry._query_impl(
        query=token_a,
        include_superseded=True,
        top_k=20,
        return_debug=True,
    ))
    returned_ids = {n["id"] for n in query_result.get("results", [])}
    assert a_id not in returned_ids, (
        f"Retracted node {a_id} must not appear in engram_query with include_superseded=True "
        f"after supersede+retract. Results: {returned_ids}"
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
