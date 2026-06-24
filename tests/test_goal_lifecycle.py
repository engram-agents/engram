"""Tests for goal lifecycle — engram_deactivate_goal / engram_activate_goal (issue #736).

Coverage:
  1. test_deactivate_goal_pauses_goal — deactivate sets lifecycle_state='paused';
     is_current still 1, status still 'open'.
  2. test_activate_goal_restores_goal — deactivate then activate; lifecycle_state
     is 'active' (or absent).
  3. test_deactivate_goal_not_in_reflect — after deactivate, _reflect_impl's
     active_goals excludes the goal.
  4. test_activate_goal_restores_to_reflect — after deactivate + activate,
     _reflect_impl includes the goal again.
  5. test_deactivate_nonexistent_goal_returns_error — error on bad goal_id.
  6. test_deactivate_wrong_type_returns_error — error if node is not a goal.
  7. test_activate_nonpaused_goal_returns_error — error if goal is already active.
  8. test_deactivate_already_paused_returns_error — error if goal is already paused.

Import-pattern contract (#948 — R1):
    This file imports engram_tasks, engram_lifecycle, engram_observation, and
    engram_core directly. Module objects are passed to fresh_module() (R4) for
    per-test reload isolation.

    R2 (payload-wrapper tests) live in a separate test_goal_lifecycle_payload.py
    if needed; this file covers the impl layer only via R1 + direct server R2
    coverage of the unknown-field / JSON-parse paths.
"""

from __future__ import annotations

import atexit
import importlib
import json
import os
import shutil
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
import engram_lifecycle
import engram_observation
import engram_tasks

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_goal_lifecycle_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


# ---------------------------------------------------------------------------
# Module factory helpers
# ---------------------------------------------------------------------------

def _make_fresh_mods():
    """Reload all family modules, configure a clean test DB, return modules.

    Uses conftest.fresh_module(engram_tasks) per R4: the module object is
    passed (not a string) so run_touched_tests.py sees the dependency edge.

    Returns (tasks, lifecycle, obs, core) — freshly reloaded.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    tasks = fresh_module(engram_tasks)
    lifecycle = importlib.import_module("engram_lifecycle")
    obs = importlib.import_module("engram_observation")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return tasks, lifecycle, obs, core


def _add_goal(tasks, claim: str, motivation: str = "test motivation") -> str:
    """Helper: add a goal and return its goal_id."""
    r = json.loads(tasks._add_goal_impl(claim=claim, motivation=motivation))
    assert "error" not in r, f"_add_goal_impl failed: {r}"
    return r["goal_id"]


def _add_observation(obs, claim: str = "Test ob.") -> str:
    """Helper: add an observation (for wrong-type test) and return its ob_id."""
    r = json.loads(obs._add_observation_impl(
        url="https://example.com/source",
        title="Source",
        quoted_text=claim,
        interpretation=f"Interpretation: {claim}",
        claim=claim,
        quote_type="attributed_analysis",
    ))
    assert "error" not in r, f"_add_observation_impl failed: {r}"
    return r["observation_id"]


def _get_node_metadata(core, node_id: str) -> dict:
    """Helper: fetch the metadata dict for a node directly from the DB."""
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT metadata, is_current, status FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        assert row is not None, f"Node {node_id} not found in DB"
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        meta["__is_current"] = row["is_current"]
        meta["__status"] = row["status"]
        return meta
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 1: deactivate sets lifecycle_state='paused'; node stays valid
# ---------------------------------------------------------------------------

def test_deactivate_goal_pauses_goal():
    """Deactivating a goal sets lifecycle_state='paused' in metadata.

    is_current must remain 1 and status must remain 'open' — we are NOT
    retracting.
    """
    tasks, lifecycle, obs, core = _make_fresh_mods()
    gid = _add_goal(tasks, "Study epistemics in depth.")

    r = json.loads(tasks._deactivate_goal_impl(goal_id=gid, reason="ARC paused"))
    assert "error" not in r, f"deactivate returned error: {r}"
    assert r["status"] == "deactivated", r
    assert r["node_id"] == gid, r
    assert r["lifecycle_state"] == "paused", r

    meta = _get_node_metadata(core, gid)
    assert meta.get("lifecycle_state") == "paused", (
        f"Expected lifecycle_state='paused' in metadata, got: {meta}"
    )
    assert meta.get("deactivation_reason") == "ARC paused", (
        f"Expected deactivation_reason in metadata, got: {meta}"
    )
    assert meta["__is_current"] == 1, (
        f"is_current must stay 1 after deactivate, got: {meta['__is_current']}"
    )
    assert meta["__status"] == "open", (
        f"status must stay 'open' after deactivate, got: {meta['__status']}"
    )


# ---------------------------------------------------------------------------
# Test 2: activate restores lifecycle_state to 'active'
# ---------------------------------------------------------------------------

def test_activate_goal_restores_goal():
    """Deactivate then activate — lifecycle_state should be 'active'."""
    tasks, lifecycle, obs, core = _make_fresh_mods()
    gid = _add_goal(tasks, "Build epistemological calibration system.")

    # Deactivate first
    r_deact = json.loads(tasks._deactivate_goal_impl(goal_id=gid))
    assert r_deact.get("lifecycle_state") == "paused", r_deact

    # Now reactivate
    r_act = json.loads(tasks._activate_goal_impl(goal_id=gid, reason="Picking it back up"))
    assert "error" not in r_act, f"activate returned error: {r_act}"
    assert r_act["status"] == "activated", r_act
    assert r_act["node_id"] == gid, r_act
    assert r_act["lifecycle_state"] == "active", r_act

    meta = _get_node_metadata(core, gid)
    assert meta.get("lifecycle_state") == "active", (
        f"Expected lifecycle_state='active' after reactivation, got: {meta}"
    )
    # deactivation_reason should have been cleared
    assert "deactivation_reason" not in meta, (
        f"deactivation_reason should be cleared after activate, got: {meta}"
    )
    assert meta["__is_current"] == 1
    assert meta["__status"] == "open"


# ---------------------------------------------------------------------------
# Test 3: deactivated goal excluded from _reflect_impl active_goals
# ---------------------------------------------------------------------------

def test_deactivate_goal_not_in_reflect():
    """After deactivation, _reflect_impl's active_goals excludes the goal."""
    tasks, lifecycle, obs, core = _make_fresh_mods()
    gid = _add_goal(tasks, "Active goal that will be paused.")

    # Confirm it appears in reflect before deactivation
    r_before = json.loads(lifecycle._reflect_impl())
    assert "error" not in r_before, r_before
    goal_ids_before = [g["id"] for g in r_before.get("active_goals", [])]
    assert gid in goal_ids_before, (
        f"Goal {gid} should appear in active_goals before deactivation, "
        f"got: {goal_ids_before}"
    )

    # Deactivate
    tasks._deactivate_goal_impl(goal_id=gid)

    # Confirm it is now absent from reflect
    r_after = json.loads(lifecycle._reflect_impl())
    assert "error" not in r_after, r_after
    goal_ids_after = [g["id"] for g in r_after.get("active_goals", [])]
    assert gid not in goal_ids_after, (
        f"Goal {gid} should NOT appear in active_goals after deactivation, "
        f"got: {goal_ids_after}"
    )


# ---------------------------------------------------------------------------
# Test 4: activated goal re-appears in _reflect_impl
# ---------------------------------------------------------------------------

def test_activate_goal_restores_to_reflect():
    """After deactivate + activate, _reflect_impl includes the goal again."""
    tasks, lifecycle, obs, core = _make_fresh_mods()
    gid = _add_goal(tasks, "Goal that will be paused and resumed.")

    # Deactivate
    tasks._deactivate_goal_impl(goal_id=gid)
    r_paused = json.loads(lifecycle._reflect_impl())
    assert gid not in [g["id"] for g in r_paused.get("active_goals", [])], (
        "Goal should be absent from reflect after deactivation"
    )

    # Reactivate
    tasks._activate_goal_impl(goal_id=gid)
    r_active = json.loads(lifecycle._reflect_impl())
    assert "error" not in r_active, r_active
    goal_ids = [g["id"] for g in r_active.get("active_goals", [])]
    assert gid in goal_ids, (
        f"Goal {gid} should re-appear in active_goals after reactivation, "
        f"got: {goal_ids}"
    )


# ---------------------------------------------------------------------------
# Test 5: deactivating a nonexistent node returns error
# ---------------------------------------------------------------------------

def test_deactivate_nonexistent_goal_returns_error():
    """Error returned for a goal_id that does not exist in the graph."""
    tasks, lifecycle, obs, core = _make_fresh_mods()

    r = json.loads(tasks._deactivate_goal_impl(goal_id="gl_9999"))
    assert "error" in r, f"Expected error for nonexistent goal, got: {r}"
    assert "not found" in r["error"].lower(), r


# ---------------------------------------------------------------------------
# Test 6: deactivating a non-goal node returns error
# ---------------------------------------------------------------------------

def test_deactivate_wrong_type_returns_error():
    """Error returned when goal_id refers to a node that is not type='goal'."""
    tasks, lifecycle, obs, core = _make_fresh_mods()
    ob_id = _add_observation(obs, "This is an observation, not a goal.")

    r = json.loads(tasks._deactivate_goal_impl(goal_id=ob_id))
    assert "error" in r, f"Expected error for non-goal node, got: {r}"
    assert "not 'goal'" in r["error"] or "goal" in r["error"].lower(), r


# ---------------------------------------------------------------------------
# Test 7: activating an already-active goal returns error
# ---------------------------------------------------------------------------

def test_activate_nonpaused_goal_returns_error():
    """Error returned when trying to activate a goal that is not paused."""
    tasks, lifecycle, obs, core = _make_fresh_mods()
    gid = _add_goal(tasks, "A fresh, never-deactivated goal.")

    r = json.loads(tasks._activate_goal_impl(goal_id=gid))
    assert "error" in r, f"Expected error for already-active goal, got: {r}"
    assert "not deactivated" in r["error"].lower(), r


# ---------------------------------------------------------------------------
# Test 8: deactivating an already-paused goal returns error
# ---------------------------------------------------------------------------

def test_deactivate_already_paused_returns_error():
    """Error returned when trying to deactivate a goal that is already paused."""
    tasks, lifecycle, obs, core = _make_fresh_mods()
    gid = _add_goal(tasks, "Goal to be double-deactivated.")

    # First deactivate — should succeed
    r1 = json.loads(tasks._deactivate_goal_impl(goal_id=gid))
    assert "error" not in r1, f"First deactivate failed unexpectedly: {r1}"

    # Second deactivate — should error
    r2 = json.loads(tasks._deactivate_goal_impl(goal_id=gid))
    assert "error" in r2, f"Expected error for already-paused goal, got: {r2}"
    assert "already deactivated" in r2["error"].lower(), r2
