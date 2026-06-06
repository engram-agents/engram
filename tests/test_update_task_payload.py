"""Tests for engram_update_task single-payload signature (wave 3 of ob_1499 — issue #99).

The migration unifies the 3-arg surface (task_id, new_status, note) into one
payload_json object. Same pattern as PR #110 (derive), PR #111
(add_observation_batch), PR #113 (add_evidence).

Coverage:
  1. Happy path — well-formed payload updates the task status + importance.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing task_id / new_status rejected.
  4. Invalid status value rejected.
  5. Note round-trips into status_history.
  6. Internal helper — _update_task_impl callable with named kwargs.
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_update_task_")
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


def _seed_task(server, description="Test task for update payload"):
    """Create a task via the already-migrated engram_add_task and return its ID."""
    r = json.loads(server.engram_add_task(payload_json=json.dumps({
        "description": description,
    })))
    assert "error" not in r, r
    assert "task_id" in r and r["task_id"].startswith("tk_"), r
    return r["task_id"]


def test_payload_json_updates_task_status():
    server = fresh_server()
    task_id = _seed_task(server)
    payload = {
        "task_id": task_id,
        "new_status": "active",
    }
    r = json.loads(server.engram_update_task(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("status") == "updated"
    assert r.get("task_id") == task_id
    assert r.get("new_status") == "active"
    # active tasks get importance_base 2.5
    assert r.get("importance_base") == 2.5


def test_payload_json_status_transitions_rebalance_importance():
    """planned → active → done (routine) — verify importance rebalances each step."""
    server = fresh_server()
    task_id = _seed_task(server)

    r1 = json.loads(server.engram_update_task(payload_json=json.dumps({
        "task_id": task_id, "new_status": "active",
    })))
    assert r1.get("importance_base") == 2.5, r1

    r2 = json.loads(server.engram_update_task(payload_json=json.dumps({
        "task_id": task_id, "new_status": "done",
    })))
    # Routine scope (default) → done_routine importance = 0.5
    assert r2.get("importance_base") == 0.5, r2
    assert r2.get("scope") == "routine"


def _get_metadata_from_db(server, node_id):
    """Read node metadata directly from the DB (bypasses MCP-level stripping)."""
    conn = sqlite3.connect(str(server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return {}
        raw = row["metadata"] or "{}"
        return json.loads(raw)
    finally:
        conn.close()


def test_payload_json_note_round_trips_to_status_history():
    server = fresh_server()
    task_id = _seed_task(server)
    r = json.loads(server.engram_update_task(payload_json=json.dumps({
        "task_id": task_id,
        "new_status": "blocked",
        "note": "Waiting on Lei's decision about scope.",
    })))
    assert "error" not in r, r
    # Read metadata directly from DB (parsed_metadata stripped from MCP returns per #358).
    meta = _get_metadata_from_db(server, task_id)
    history = meta.get("status_history", [])
    assert len(history) == 1, history
    assert history[0]["note"] == "Waiting on Lei's decision about scope."
    assert history[0]["from"] == "planned"
    assert history[0]["to"] == "blocked"


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_update_task(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_update_task(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    task_id = _seed_task(server)
    payload = {
        "task_id": task_id,
        "new_status": "active",
        "bogus_field": "reject me",
    }
    r = json.loads(server.engram_update_task(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


def test_payload_json_missing_task_id_rejected():
    server = fresh_server()
    payload = {"new_status": "active"}
    r = json.loads(server.engram_update_task(payload_json=json.dumps(payload)))
    assert "error" in r and "task_id" in r["error"], r


def test_payload_json_missing_new_status_rejected():
    server = fresh_server()
    task_id = _seed_task(server)
    payload = {"task_id": task_id}
    r = json.loads(server.engram_update_task(payload_json=json.dumps(payload)))
    assert "error" in r and "new_status" in r["error"], r


def test_payload_json_invalid_status_rejected():
    server = fresh_server()
    task_id = _seed_task(server)
    payload = {"task_id": task_id, "new_status": "not_a_status"}
    r = json.loads(server.engram_update_task(payload_json=json.dumps(payload)))
    assert "error" in r and "Invalid status" in r["error"], r


def test_payload_json_nonexistent_task_rejected():
    server = fresh_server()
    payload = {"task_id": "tk_99999", "new_status": "active"}
    r = json.loads(server.engram_update_task(payload_json=json.dumps(payload)))
    assert "error" in r and "not found" in r["error"], r


def test_payload_json_no_note_status_history_unchanged():
    """Note-gated status_history is INTENTIONAL: a status transition without
    a note leaves metadata.status_history untouched. The durable audit channel
    is edit_history (logged regardless); status_history is the agent-facing
    narrative-only surface, populated only when the caller chooses to provide
    context. Fairy round-1 suggestion on PR #114: lock this in so a future
    'always record transition' simplification doesn't silently change semantics.
    """
    server = fresh_server()
    task_id = _seed_task(server)
    # Transition without note — status_history must stay empty.
    payload = {"task_id": task_id, "new_status": "active"}
    r = json.loads(server.engram_update_task(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("status") == "updated"
    # Read metadata from DB (parsed_metadata stripped from MCP returns per #358).
    meta = _get_metadata_from_db(server, task_id)
    history = meta.get("status_history", [])
    assert history == [], f"Expected empty status_history for note-free transition, got: {history}"
    # Now transition WITH note — status_history should grow by 1.
    payload2 = {"task_id": task_id, "new_status": "done", "note": "with note"}
    r2 = json.loads(server.engram_update_task(payload_json=json.dumps(payload2)))
    assert "error" not in r2, r2
    meta2 = _get_metadata_from_db(server, task_id)
    history2 = meta2.get("status_history", [])
    assert len(history2) == 1, f"Expected 1 entry after with-note transition, got {len(history2)}: {history2}"
    assert history2[0]["note"] == "with note"


def test_impl_callable_with_kwargs():
    """In-server callers should be able to call _update_task_impl directly."""
    server = fresh_server()
    task_id = _seed_task(server)
    r = json.loads(server._update_task_impl(
        task_id=task_id,
        new_status="active",
        note="Internal kwargs path test.",
    ))
    assert "error" not in r, r
    assert r.get("status") == "updated"
    assert r.get("new_status") == "active"
