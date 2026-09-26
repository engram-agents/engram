"""Tests for engram_add_task single-payload signature (wave 2c-ii of ob_1499).

Coverage:
  1. Happy path — well-formed payload creates a task.
  2. Optional fields (goal_id, implements_ids, scope) respected.
  3. Schema enforcement — invalid JSON, non-object, unknown fields.
  4. Required-field validation — missing description rejected.
  5. Scope validator preserved — invalid scope rejected.
  6. Internal helper — _add_task_impl works with named kwargs.
  7. is_current guards (issue #1031): non-current goal_id skipped + warned;
     non-current parent_task_id skipped + warned; current nodes linked normally.
  8. importance_base anchor (#2268) — new tasks are importance-anchored at
     TASK_IMPORTANCE["planned"] via _stamp_new_node's importance_base
     override (one-pass write, no interim confidence/surprise-derived value
     + follow-up UPDATE).
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_task_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def test_payload_json_creates_task():
    server = fresh_server()
    payload = {
        "description": "Migrate the remaining four add_* tools to single-payload signature.",
    }
    r = json.loads(server.engram_add_task(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "task_id" in r and r["task_id"].startswith("tk_"), r
    assert r["scope"] == "routine"
    assert r["task_status"] == "planned"


def test_payload_json_with_milestone_scope():
    server = fresh_server()
    payload = {
        "description": "Ship paper preprint to arXiv.",
        "scope": "milestone",
    }
    r = json.loads(server.engram_add_task(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r["scope"] == "milestone"


def test_payload_json_invalid_scope_rejected():
    server = fresh_server()
    payload = {"description": "x", "scope": "bogus"}
    r = json.loads(server.engram_add_task(payload_json=json.dumps(payload)))
    assert "error" in r and "scope" in r["error"], r


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_task(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_task(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {"description": "x", "bogus": "reject"}
    r = json.loads(server.engram_add_task(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus" in r["error"]


def test_payload_json_missing_description_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_task(payload_json=json.dumps({})))
    assert "error" in r and "description" in r["error"], r


def test_payload_json_empty_description_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_task(payload_json=json.dumps({"description": "  "})))
    assert "error" in r and "description" in r["error"], r


def test_impl_callable_with_kwargs():
    server = fresh_server()
    r = json.loads(server._add_task_impl(
        description="Internal kwargs task.",
    ))
    assert "error" not in r, r
    assert "task_id" in r


def _seed_goal(server):
    """Create a current goal and return its id."""
    r = json.loads(server.engram_add_goal(payload_json=json.dumps({
        "claim": "Test goal for task is_current guard.",
        "motivation": "Fixture.",
    })))
    assert "error" not in r, r
    return r["goal_id"]


def _retract_node(server, node_id):
    """Retract a node so is_current = 0."""
    r = json.loads(server._retract_impl(
        node_id=node_id,
        error_type="other",
        reason="test: verify non-current guard",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement decision not under test",
    ))
    assert "error" not in r, r


def test_add_task_skips_non_current_goal():
    """Non-current goal_id: serves edge skipped; non_current_goal_id reported."""
    server = fresh_server()
    goal_id = _seed_goal(server)
    _retract_node(server, goal_id)

    r = json.loads(server._add_task_impl(
        description="Task with retracted goal.",
        goal_id=goal_id,
    ))
    assert "error" not in r, r
    assert r.get("status") == "created"
    assert r.get("non_current_goal_id") == goal_id, r
    assert "serves_goal" not in r, r


def test_add_task_links_current_goal():
    """Current goal_id: serves edge created normally."""
    server = fresh_server()
    goal_id = _seed_goal(server)

    r = json.loads(server._add_task_impl(
        description="Task with active goal.",
        goal_id=goal_id,
    ))
    assert "error" not in r, r
    assert r.get("serves_goal") == goal_id, r
    assert "non_current_goal_id" not in r, r


def test_add_task_skips_non_current_parent():
    """Non-current parent_task_id: subtask_of edge skipped; non_current_parent_task_id reported."""
    server = fresh_server()
    parent_r = json.loads(server._add_task_impl(description="Parent task."))
    parent_id = parent_r["task_id"]
    _retract_node(server, parent_id)

    r = json.loads(server._add_task_impl(
        description="Subtask with retracted parent.",
        parent_task_id=parent_id,
    ))
    assert "error" not in r, r
    assert r.get("status") == "created"
    assert r.get("non_current_parent_task_id") == parent_id, r
    assert "parent_task" not in r, r


def test_add_task_links_current_parent():
    """Current parent_task_id: subtask_of edge created; parent auto-promoted to milestone."""
    server = fresh_server()
    parent_r = json.loads(server._add_task_impl(description="Parent task."))
    parent_id = parent_r["task_id"]

    r = json.loads(server._add_task_impl(
        description="Subtask with active parent.",
        parent_task_id=parent_id,
    ))
    assert "error" not in r, r
    assert r.get("parent_task") == parent_id, r
    assert "non_current_parent_task_id" not in r, r


# ── 8. importance_base anchor (#2268 dead-write cleanup) ────────────────────


def _read_importance(server, node_id):
    """Read node importance_base/importance_score directly from the DB."""
    conn = sqlite3.connect(str(server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT importance_base, importance_score FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
    finally:
        conn.close()


def test_task_importance_base_anchored_at_planned():
    """New task creation ends at importance_base=TASK_IMPORTANCE["planned"]
    with the matching decay-anchored importance_score, even though the
    interim confidence=0.5/surprise=0.0 write (#2268) is no longer performed.

    This is the correctness invariant that actually matters — the final
    stored values are unchanged from before the cleanup, not an assertion
    on the removed dead write itself.
    """
    server = fresh_server()
    r = json.loads(server.engram_add_task(payload_json=json.dumps({
        "description": "Task for the importance-anchor invariant test.",
    })))
    assert "error" not in r, r
    task_id = r["task_id"]

    planned = server._tasks_mod.TASK_IMPORTANCE["planned"]
    row = _read_importance(server, task_id)
    assert row["importance_base"] == planned, (
        f"Expected importance_base={planned} (unchanged anchor value), got {row['importance_base']}"
    )
    expected_score = server.core._compute_importance(planned, server.core._get_current_turn())
    assert row["importance_score"] == expected_score, (
        f"Expected importance_score={expected_score} (anchor decayed to "
        f"current turn), got {row['importance_score']}"
    )
    # Never left at the interim confidence=0.5/surprise=0.0 value.
    assert row["importance_base"] != 0.5
