"""Tests for engram_add_task single-payload signature (wave 2c-ii of ob_1499).

Coverage:
  1. Happy path — well-formed payload creates a task.
  2. Optional fields (goal_id, implements_ids, scope) respected.
  3. Schema enforcement — invalid JSON, non-object, unknown fields.
  4. Required-field validation — missing description rejected.
  5. Scope validator preserved — invalid scope rejected.
  6. Internal helper — _add_task_impl works with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_task_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    if "server" in sys.modules:
        del sys.modules["server"]
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
