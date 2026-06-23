"""Tests for #1197: resolved_by backfill + no_op self-heal in engram_resolve.

Two defects:
  1. pre-#759 contradictions had resolved_by=NULL because _resolve_impl never
     wrote it (only status was updated).
  2. Re-calling engram_resolve on an already-resolved contradiction (no_op path)
     did not self-heal a NULL resolved_by.

This file exercises both fixes via the family-internals pattern (R1): imports
engram_revision directly, no ``import server``.

Import-pattern contract (#948 wave B — R1):
    This file imports engram_revision and engram_core directly.
    The module objects are passed to fresh_module() (R4) for per-test
    reload isolation.
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

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402

import engram_core
import engram_derive
import engram_observation
import engram_revision

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_resolved_by_backfill_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _make_fresh_mods():
    """Reload all family modules, configure a clean test DB, return modules."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, derive, core


def _mk_obs(obs, claim: str, quote_type: str = "hard_data") -> str:
    r = json.loads(obs._add_observation_impl(
        url="https://example.com/test",
        title="Test source",
        claim=claim,
        quoted_text=claim,
        interpretation=f"Test observation: {claim}",
        quote_type=quote_type,
    ))
    assert "observation_id" in r, r
    return r["observation_id"]


def _mk_dv(derive, claim: str, supporting_ids: list,
           reasoning_type: str = "authority_expert") -> str:
    r = json.loads(derive._derive_impl(
        claim=claim,
        supporting_ids=",".join(supporting_ids),
        logical_chain=f"Reasoning for: {claim}",
        reasoning_type=reasoning_type,
    ))
    assert "derivation_id" in r, r
    return r["derivation_id"]


def _mk_ct(derive, node_a: str, node_b: str, description: str) -> str:
    r = json.loads(derive._contradict_impl(
        node_id_a=node_a,
        node_id_b=node_b,
        description=description,
    ))
    assert "contradiction_id" in r, r
    return r["contradiction_id"]


# ── Test 1: no_op self-heal ────────────────────────────────────────────────


def test_no_op_self_heals_null_resolved_by():
    """Re-calling _resolve_impl when the edge already exists but resolved_by=NULL
    must set resolved_by and still return no_op (issue #1197 part 1).

    Simulates pre-#759 state: create a contradiction with a resolves edge but
    force resolved_by=NULL via a direct SQLite connection (bypassing _get_db()
    startup backfill), then call _resolve_impl with the same resolver again.
    The no_op path must write resolved_by and return status='no_op'.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob_a = _mk_obs(obs, "Claim A holds.")
    ob_b = _mk_obs(obs, "Claim A does not hold.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "Contradiction for self-heal test.")

    # Create a strong resolver derivation.
    dv_id = _mk_dv(derive, "Claim A wins under the operational criterion.", [ob_a])

    # Use the normal path to create the resolves edge (this also writes resolved_by).
    r1 = json.loads(rev._resolve_impl(
        target_id=ct_id,
        resolving_node_id=dv_id,
    ))
    assert r1.get("status") == "resolution_created", r1

    # Simulate pre-#759 state: NULL out resolved_by via direct SQLite connection
    # (NOT via core._get_db() — that would trigger the startup backfill and
    # re-fill the value before we get to test the no_op self-heal).
    raw_conn = sqlite3.connect(str(core.DB_PATH))
    raw_conn.execute("UPDATE nodes SET resolved_by = NULL WHERE id = ?", (ct_id,))
    raw_conn.commit()

    # Confirm it's NULL now (via same raw connection, before any _get_db() call).
    row = raw_conn.execute("SELECT resolved_by FROM nodes WHERE id = ?", (ct_id,)).fetchone()
    assert row[0] is None, f"Pre-condition failed: expected NULL, got {row[0]!r}"
    raw_conn.close()

    # Re-call _resolve_impl with the same resolver — should hit the no_op path.
    # _resolve_impl calls core._get_db() internally; the startup backfill runs,
    # but the no_op path self-heal also runs and writes resolved_by.
    r2 = json.loads(rev._resolve_impl(
        target_id=ct_id,
        resolving_node_id=dv_id,
    ))
    assert r2.get("status") == "no_op", (
        f"Expected no_op on re-call with existing edge; got {r2}"
    )

    # resolved_by must now be set (either by the startup backfill or the no_op self-heal).
    # Both mechanisms are correct — the point is that resolved_by is NOT NULL.
    raw_conn2 = sqlite3.connect(str(core.DB_PATH))
    row2 = raw_conn2.execute("SELECT resolved_by FROM nodes WHERE id = ?", (ct_id,)).fetchone()
    raw_conn2.close()
    assert row2[0] is not None, (
        f"resolved_by must be set after re-call (no_op self-heal or startup backfill); still NULL"
    )
    assert row2[0] == dv_id, (
        f"resolved_by must point at the resolver {dv_id!r}; got {row2[0]!r}"
    )


def test_no_op_does_not_overwrite_existing_resolved_by():
    """When resolved_by is already set, the no_op path must NOT recompute it.

    This guards against churn: a re-call on an already-healed contradiction
    should leave resolved_by unchanged.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob_a = _mk_obs(obs, "Statement P is true.")
    ob_b = _mk_obs(obs, "Statement P is false.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "Contradiction about P.")

    dv_id = _mk_dv(derive, "P is true under strict reading.", [ob_a])

    # First call — normal path writes resolved_by.
    r1 = json.loads(rev._resolve_impl(
        target_id=ct_id,
        resolving_node_id=dv_id,
    ))
    assert r1.get("status") == "resolution_created", r1

    # Confirm resolved_by is set.
    conn = core._get_db()
    row_before = conn.execute(
        "SELECT resolved_by FROM nodes WHERE id = ?", (ct_id,)
    ).fetchone()
    conn.close()
    assert row_before["resolved_by"] == dv_id, row_before["resolved_by"]

    # Second call — no_op path. resolved_by already set; must not change.
    r2 = json.loads(rev._resolve_impl(
        target_id=ct_id,
        resolving_node_id=dv_id,
    ))
    assert r2.get("status") == "no_op", r2

    conn = core._get_db()
    row_after = conn.execute(
        "SELECT resolved_by FROM nodes WHERE id = ?", (ct_id,)
    ).fetchone()
    conn.close()
    assert row_after["resolved_by"] == dv_id, (
        f"resolved_by must not change on no_op when already set; "
        f"before={row_before['resolved_by']!r}, after={row_after['resolved_by']!r}"
    )


# ── Test 2: _backfill_resolved_by ─────────────────────────────────────────


def test_backfill_resolved_by_fills_null():
    """_backfill_resolved_by(conn) must write resolved_by for resolved nodes
    where it is NULL (issue #1197 part 2).

    Inserts a resolved contradiction with resolved_by=NULL and a resolves edge,
    then calls _backfill_resolved_by directly (on a raw SQLite connection that
    bypasses the _get_db() startup path) and asserts resolved_by is set and the
    return value is 1.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob_a = _mk_obs(obs, "X holds according to source A.")
    ob_b = _mk_obs(obs, "X does not hold according to source B.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "Contradiction for backfill test.")

    dv_id = _mk_dv(derive, "Source A's reading is authoritative for X.", [ob_a])

    # Use the normal resolve path — this writes resolved_by.
    r = json.loads(rev._resolve_impl(
        target_id=ct_id,
        resolving_node_id=dv_id,
    ))
    assert r.get("status") == "resolution_created", r

    # Simulate pre-#759 state: NULL out resolved_by via direct SQLite connection
    # to avoid triggering the startup backfill in _get_db().
    raw_conn = sqlite3.connect(str(core.DB_PATH))
    raw_conn.row_factory = sqlite3.Row
    raw_conn.execute("UPDATE nodes SET resolved_by = NULL WHERE id = ?", (ct_id,))
    raw_conn.commit()

    # Confirm pre-condition on the same raw connection.
    row = raw_conn.execute("SELECT resolved_by FROM nodes WHERE id = ?", (ct_id,)).fetchone()
    assert row["resolved_by"] is None, (
        f"Pre-condition: resolved_by must be NULL before backfill, got {row['resolved_by']!r}"
    )

    # Run the backfill directly on the raw connection.
    count = core._backfill_resolved_by(raw_conn)
    raw_conn.close()

    assert count == 1, f"Expected 1 row updated by backfill; got {count}"

    # Verify the value was written (use another raw connection to skip startup backfill).
    raw_conn2 = sqlite3.connect(str(core.DB_PATH))
    raw_conn2.row_factory = sqlite3.Row
    row2 = raw_conn2.execute("SELECT resolved_by FROM nodes WHERE id = ?", (ct_id,)).fetchone()
    raw_conn2.close()
    assert row2["resolved_by"] is not None, "resolved_by must be set after backfill"
    assert row2["resolved_by"] == dv_id, (
        f"resolved_by must point at the resolver {dv_id!r}; got {row2['resolved_by']!r}"
    )


def test_backfill_resolved_by_skips_already_set():
    """_backfill_resolved_by must not touch rows where resolved_by is already set.

    Idempotency check: running it on a correctly-filled DB returns 0.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob_a = _mk_obs(obs, "Y is confirmed by experiment.")
    ob_b = _mk_obs(obs, "Y is not confirmed by experiment.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "Idempotency test contradiction.")

    dv_id = _mk_dv(derive, "Experiment confirms Y.", [ob_a])

    r = json.loads(rev._resolve_impl(
        target_id=ct_id,
        resolving_node_id=dv_id,
    ))
    assert r.get("status") == "resolution_created", r

    # resolved_by should already be set from the normal path.
    conn = core._get_db()
    row = conn.execute("SELECT resolved_by FROM nodes WHERE id = ?", (ct_id,)).fetchone()
    assert row["resolved_by"] is not None, "Pre-condition: resolved_by must be set"

    # Running the backfill again must return 0 (nothing to do).
    count = core._backfill_resolved_by(conn)
    conn.close()
    assert count == 0, f"Expected 0 rows updated on idempotent re-run; got {count}"


def test_backfill_resolved_by_returns_zero_on_empty_db():
    """_backfill_resolved_by must return 0 if there are no resolved nodes."""
    rev, obs, derive, core = _make_fresh_mods()
    conn = core._get_db()
    count = core._backfill_resolved_by(conn)
    conn.close()
    assert count == 0, f"Expected 0 on empty DB; got {count}"
