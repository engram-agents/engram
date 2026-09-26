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
    5. test_supersede_then_plain_retract_is_refused_and_fts_is_untouched —
       #2396 (round 2): a plain engram_retract of an already-superseded node
       (is_current→0 via supersede) is now REFUSED before it reaches the DB.
       The refusal names the successor, carries no "status" key, and mutates
       no FTS state — this is the verb-route replacement for what used to be
       test 5 (pre-#2396, that route reached a real retract of a superseded
       node; #2396 closes it).
    6. test_db_level_retract_of_a_superseded_node_does_not_double_delete_from_fts —
       #2396: _retract_impl can no longer reach the supersede-then-retract DB
       transition (see test 5 above), but the retract trigger's
       OLD.is_current=1 guard remains load-bearing for any OTHER writer that
       might reach it (a migration, a future code path, a hand-run UPDATE).
       Exercises the guard directly in SQL, bypassing _retract_impl entirely
       (same idiom as test 3), and asserts no double-delete ("database disk
       image is malformed" — see test 3's docstring for that failure mode).

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
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
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
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
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
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
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
# Test 5: supersede-then-retract, the VERB route — #2396 refuses it
# ---------------------------------------------------------------------------

def test_supersede_then_plain_retract_is_refused_and_fts_is_untouched():
    """A plain retract of an already-superseded node is REFUSED (#2396).

    Pre-#2396, this file's test 5 drove supersede(A, B) then a real
    ``_retract_impl(a_id, ...)`` and asserted the node stayed absent from FTS
    across both steps. #2396 (round 2) bans that second step outright: a
    plain ``engram_retract`` of a NON-CURRENT node (already superseded or
    retracted) is refused before it ever reaches the DB, mirroring
    ``_supersede_impl``'s own ``is_current`` guard. Retracting an
    already-superseded node used to reopen #2343's double-adjudication
    through a second door and was not idempotent — see #2396 / #2013 for the
    full argument; this test only exercises the VERB-level consequence.

    Supersede A with B — A leaves FTS via the supersede trigger
    (nodes_fts_supersede_remove, #195). Then call ``_retract_impl(a_id,
    ...)`` and assert it is refused: the return carries an "error" key that
    names the successor, carries NO "status" key (nothing was retracted),
    and A is still absent from FTS afterward — i.e. the refusal mutated no
    FTS state. The sibling test below,
    ``test_db_level_retract_of_a_superseded_node_does_not_double_delete_from_fts``,
    covers the DB-level trigger guard that this refusal makes unreachable
    through the verb but which remains load-bearing for any other writer.
    """
    rev, obs, _qry, _core = _make_fresh_mods()

    token_a = "fts280supretract_uniquetokenA5"
    token_b = "fts280supretract_uniquetokenB5"

    # Node A: will be superseded, then have a plain retract refused.
    a_id = _add_observation(
        obs,
        claim=f"Supersede-then-retract claim {token_a} for trigger test 5.",
        quoted_text=f"Quote with {token_a} to be superseded then retract-refused.",
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

    # Step 1: Supersede A with B — A becomes is_current=0 (status column is
    # untouched by supersede — _supersede_impl only ever writes is_current
    # and superseded_by, never status; see engram_revision.py:2285).
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

    # Step 2: A plain retract of A — now non-current — must be REFUSED (#2396).
    ret = json.loads(rev._retract_impl(
        node_id=a_id,
        error_type="other",
        reason="FTS trigger regression test #280 — test 5 step 2 (#2396 refusal)",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" in ret, (
        f"a plain retract of an already-superseded node must be refused (#2396): {ret}"
    )
    assert b_id in ret["error"], (
        f"the refusal must name the current successor '{b_id}': {ret['error']}"
    )
    assert "status" not in ret, (
        f"a refused retract must report no status transition: {ret}"
    )

    # After the refused retract: A is STILL absent from FTS (unchanged from
    # the post-supersede state) — a refusal must mutate no FTS state.
    assert rowid_a not in _fts_match_rowids(token_a), (
        f"Token '{token_a}' must still NOT match rowid {rowid_a} after the refused "
        "retract — a refusal must leave FTS state exactly as the supersede left it."
    )


# ---------------------------------------------------------------------------
# Test 6: supersede-then-retract, the DB-LEVEL route — the trigger guard
# ---------------------------------------------------------------------------

def test_db_level_retract_of_a_superseded_node_does_not_double_delete_from_fts():
    """The retract trigger's OLD.is_current=1 guard stays load-bearing (#2396).

    #2396 closes the VERB route to "retract an already-superseded node" (see
    test 5 above) — but the guard clause itself
    (``AND OLD.is_current = 1``, engram_core.py's nodes_retract_remove_from_fts
    trigger, ~line 2012) is NOT deleted, because it protects the DB layer
    against ANY writer that reaches this state, not only the verb: a
    migration, a future code path, or a hand-run UPDATE. Without the guard, a
    SECOND FTS5 'delete' magic-insert on a rowid the supersede trigger has
    already removed raises "database disk image is malformed" (see test 3's
    docstring above for that failure mode) — real corruption, not a wrong
    answer.

    This test reaches the same DB transition directly in SQL, bypassing
    ``_retract_impl`` entirely (the same idiom test 3 already uses): after a
    real supersede via the verb, issue a raw
    ``UPDATE nodes SET status = 'retracted' WHERE id = ?`` on the connection
    and commit. Asserts no exception is raised and that the node stays
    absent from FTS — i.e. the guard still prevents the double-delete for a
    writer that isn't ``_retract_impl``.

    Mutation-checked (#2396 round 2): with the ``AND OLD.is_current = 1``
    clause deleted from the trigger, this exact test goes red with
    ``sqlite3.OperationalError: database disk image is malformed`` on the
    ``UPDATE`` below — confirming the test would actually catch a regression
    of the guard, not merely exercise a no-op.
    """
    rev, obs, _qry, _core = _make_fresh_mods()

    token_a = "fts280supretractdb_uniquetokenA6"
    token_b = "fts280supretractdb_uniquetokenB6"

    # Node A: will be superseded, then retracted at the raw-SQL level.
    a_id = _add_observation(
        obs,
        claim=f"DB-level supersede-then-retract claim {token_a} for trigger test 6.",
        quoted_text=f"Quote with {token_a} to be superseded then DB-level retracted.",
        url="https://example.com/fts280-a6",
    )
    rowid_a = _get_node_rowid(a_id)

    # Node B: the superseder (just needs to exist).
    b_id = _add_observation(
        obs,
        claim=f"Superseder claim {token_b} for trigger test 6.",
        quoted_text=f"Quote with {token_b} as superseder.",
        url="https://example.com/fts280-b6",
    )

    # Before anything: A is in FTS.
    assert rowid_a in _fts_match_rowids(token_a), (
        f"Token '{token_a}' should match rowid {rowid_a} in nodes_fts before supersede."
    )

    # Step 1: Supersede A with B via the real verb — A becomes is_current=0.
    sup = json.loads(rev._supersede_impl(
        a_id, b_id, supersede_reason="FTS trigger regression test #280 — test 6 step 1"
    ))
    assert sup.get("status") == "superseded", f"supersede failed: {sup}"

    # After supersede: A is already ABSENT from FTS — supersede trigger fired.
    assert rowid_a not in _fts_match_rowids(token_a), (
        f"Token '{token_a}' must NOT match rowid {rowid_a} after supersede. "
        "The supersede trigger (nodes_fts_supersede_remove) removes nodes from FTS "
        "when is_current flips to 0 (#195)."
    )

    # Step 2: Bypass _retract_impl entirely — raw SQL retract of the
    # already-superseded node, same idiom as test 3's migration-bypass SQL.
    # This must NOT raise "database disk image is malformed": the trigger's
    # OLD.is_current=1 guard sees OLD.is_current=0 (set by the supersede
    # above) and does not fire a second 'delete' magic-insert.
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "UPDATE nodes SET status = 'retracted' WHERE id = ?",
        (a_id,),
    )
    conn.commit()
    conn.close()

    # After the raw SQL retract: A remains absent from FTS — the guard
    # prevented a double-delete, and no corruption occurred.
    assert rowid_a not in _fts_match_rowids(token_a), (
        f"Token '{token_a}' must NOT match rowid {rowid_a} after the raw SQL retract. "
        "The retract trigger's OLD.is_current=1 guard must prevent a second FTS5 "
        "'delete' magic-insert on an already-removed rowid."
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
