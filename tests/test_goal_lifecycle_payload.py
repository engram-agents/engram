"""MCP-contract tests for engram_deactivate_goal / engram_activate_goal wrappers (#736).

Covers the server.py payload_json wrapper layer — JSON parse, isinstance check,
and unknown-field guard — which lives in the @mcp.tool() wrapper, NOT in
engram_tasks._deactivate_goal_impl / _activate_goal_impl.

Per IMPORT_RUBRIC.md R2: this file imports server and uses the test_*_payload.py
name so the audit gate classifies it as contract-tier.

Coverage:
  1. Bad JSON payload → structured error (deactivate)
  2. Non-object payload → structured error (deactivate)
  3. Unknown field in payload → structured error (deactivate)
  4. Bad JSON payload → structured error (activate)
  5. Unknown field in payload → structured error (activate)
  6. Missing goal_id → structured error (deactivate)
  7. Happy-path round-trip via MCP wrappers
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_goal_lifecycle_payload_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Return a freshly initialised server module with an empty test DB."""
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


def _seed_goal(server, claim="Test goal", motivation="For testing"):
    r = json.loads(server.engram_add_goal(json.dumps({"claim": claim, "motivation": motivation})))
    assert "goal_id" in r, r
    return r["goal_id"]


def test_deactivate_bad_json_returns_error():
    """Malformed JSON payload returns a structured error."""
    srv = fresh_server()
    r = json.loads(srv.engram_deactivate_goal("{not-json}"))
    assert "error" in r, r
    assert "Invalid JSON" in r["error"], r


def test_deactivate_non_object_returns_error():
    """Array payload returns a structured error."""
    srv = fresh_server()
    r = json.loads(srv.engram_deactivate_goal("[1, 2]"))
    assert "error" in r, r
    assert "JSON object" in r["error"], r


def test_deactivate_unknown_field_returns_error():
    """Unknown field in payload returns a structured error."""
    srv = fresh_server()
    gid = _seed_goal(srv)
    r = json.loads(srv.engram_deactivate_goal(json.dumps({"goal_id": gid, "typo_field": "x"})))
    assert "error" in r, r
    assert "typo_field" in r["error"] or "Unknown" in r["error"], r


def test_activate_bad_json_returns_error():
    """Malformed JSON payload returns a structured error."""
    srv = fresh_server()
    r = json.loads(srv.engram_activate_goal("{not-json}"))
    assert "error" in r, r
    assert "Invalid JSON" in r["error"], r


def test_activate_unknown_field_returns_error():
    """Unknown field in activate payload returns a structured error."""
    srv = fresh_server()
    gid = _seed_goal(srv)
    # Deactivate first so activate would otherwise succeed
    json.loads(srv.engram_deactivate_goal(json.dumps({"goal_id": gid})))
    r = json.loads(srv.engram_activate_goal(json.dumps({"goal_id": gid, "unknown": "x"})))
    assert "error" in r, r
    assert "unknown" in r["error"] or "Unknown" in r["error"], r


def test_deactivate_missing_goal_id_returns_error():
    """Missing goal_id returns a structured error."""
    srv = fresh_server()
    r = json.loads(srv.engram_deactivate_goal(json.dumps({"reason": "no id"})))
    assert "error" in r, r
    assert "goal_id" in r["error"].lower(), r


def test_mcp_wrapper_round_trip():
    """Deactivate then activate a goal via the MCP wrappers end-to-end."""
    srv = fresh_server()
    gid = _seed_goal(srv)
    r1 = json.loads(srv.engram_deactivate_goal(json.dumps({"goal_id": gid, "reason": "testing"})))
    assert r1.get("status") == "deactivated", r1
    assert r1.get("lifecycle_state") == "paused", r1
    r2 = json.loads(srv.engram_activate_goal(json.dumps({"goal_id": gid, "reason": "resuming"})))
    assert r2.get("status") == "activated", r2
    assert r2.get("lifecycle_state") == "active", r2
