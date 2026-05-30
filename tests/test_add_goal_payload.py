"""Tests for engram_add_goal single-payload signature (wave 2c-i of ob_1499).

Coverage:
  1. Happy path — well-formed payload creates a goal.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing claim / motivation rejected.
  4. Internal helper — _add_goal_impl works with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_goal_")
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


def test_payload_json_creates_goal():
    server = fresh_server()
    payload = {
        "claim": "Close the loop from static graph nodes to active habits.",
        "motivation": "Stored knowledge should change behavior, not just be retrievable.",
    }
    r = json.loads(server.engram_add_goal(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "goal_id" in r and r["goal_id"].startswith("gl_"), r


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_goal(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_goal(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {"claim": "x", "motivation": "y", "bogus": "reject"}
    r = json.loads(server.engram_add_goal(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r


def test_payload_json_missing_claim_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_goal(payload_json=json.dumps({"motivation": "y"})))
    assert "error" in r and "claim" in r["error"], r


def test_payload_json_missing_motivation_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_goal(payload_json=json.dumps({"claim": "x"})))
    assert "error" in r and "motivation" in r["error"], r


def test_impl_callable_with_kwargs():
    server = fresh_server()
    r = json.loads(server._add_goal_impl(
        claim="Internal kwargs test goal.",
        motivation="testing _impl is callable.",
    ))
    assert "error" not in r, r
    assert "goal_id" in r
