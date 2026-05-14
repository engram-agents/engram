"""Tests for engram_goal_tension single-payload signature
(wave 3 of ob_1499 — issue #99).

The migration unifies the 4-arg surface (goal_id_a, goal_id_b, description,
analysis) into one payload_json object. Same pattern as PR #110 (derive),
PR #114 (update_task).

Coverage:
  1. Happy path — well-formed payload creates a goal tension.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing goal_id_a / goal_id_b / description.
  4. Business-logic errors — non-goal nodes rejected, self-tension rejected.
  5. Internal helper — _goal_tension_impl callable with named kwargs.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_payload_goal_tension")


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


def _two_goals(server):
    """Seed two goal nodes via the already-migrated engram_add_goal."""
    g1 = json.loads(server.engram_add_goal(payload_json=json.dumps({
        "claim": "Goal A: maximize curiosity-driven exploration.",
        "motivation": "Test fixture goal A motivation.",
    })))
    g2 = json.loads(server.engram_add_goal(payload_json=json.dumps({
        "claim": "Goal B: maintain strict resource discipline.",
        "motivation": "Test fixture goal B motivation.",
    })))
    assert "error" not in g1, g1
    assert "error" not in g2, g2
    return g1["goal_id"], g2["goal_id"]


def test_payload_json_creates_goal_tension():
    server = fresh_server()
    g1, g2 = _two_goals(server)
    payload = {
        "goal_id_a": g1,
        "goal_id_b": g2,
        "description": "Curiosity and discipline pull in opposite directions.",
        "analysis": "Resource conflict — exploration uses budget discipline saves.",
    }
    r = json.loads(server.engram_goal_tension(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("status") == "created"
    assert r.get("tension_id", "").startswith("gt_"), r


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_goal_tension(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_goal_tension(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    g1, g2 = _two_goals(server)
    payload = {
        "goal_id_a": g1,
        "goal_id_b": g2,
        "description": "x",
        "bogus_field": "reject me",
    }
    r = json.loads(server.engram_goal_tension(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


def test_payload_json_missing_goal_id_a_rejected():
    server = fresh_server()
    _, g2 = _two_goals(server)
    payload = {"goal_id_b": g2, "description": "x"}
    r = json.loads(server.engram_goal_tension(payload_json=json.dumps(payload)))
    assert "error" in r and "goal_id_a" in r["error"], r


def test_payload_json_missing_goal_id_b_rejected():
    server = fresh_server()
    g1, _ = _two_goals(server)
    payload = {"goal_id_a": g1, "description": "x"}
    r = json.loads(server.engram_goal_tension(payload_json=json.dumps(payload)))
    assert "error" in r and "goal_id_b" in r["error"], r


def test_payload_json_missing_description_rejected():
    server = fresh_server()
    g1, g2 = _two_goals(server)
    payload = {"goal_id_a": g1, "goal_id_b": g2}
    r = json.loads(server.engram_goal_tension(payload_json=json.dumps(payload)))
    assert "error" in r and "description" in r["error"], r


def test_payload_json_self_tension_rejected():
    server = fresh_server()
    g1, _ = _two_goals(server)
    payload = {"goal_id_a": g1, "goal_id_b": g1, "description": "self"}
    r = json.loads(server.engram_goal_tension(payload_json=json.dumps(payload)))
    assert "error" in r and "itself" in r["error"], r


def test_impl_callable_with_kwargs():
    """In-server callers should be able to call _goal_tension_impl directly."""
    server = fresh_server()
    g1, g2 = _two_goals(server)
    r = json.loads(server._goal_tension_impl(
        goal_id_a=g1,
        goal_id_b=g2,
        description="Internal kwargs path test.",
    ))
    assert "error" not in r, r
    assert r.get("status") == "created"
