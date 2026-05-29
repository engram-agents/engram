"""Tests for the engram_nap payload_json wrapper (migrate/session-cluster-payload-json).

Coverage:
  1. Invalid JSON rejection — non-parseable payload_json string returns {"error": ...}
     with "Invalid JSON" in the message.
  2. Non-object JSON payload rejection — valid JSON but non-object (e.g. a list)
     returns {"error": ...} with "must be a JSON object" in the message.
  3. Unknown-field rejection — payload with an unrecognised key returns {"error": ...}
     with "Unknown fields" in the message.
  4. Missing required field 'message' — payload without 'message' returns {"error": ...}.
  5. _nap_impl callable with named kwargs — internal function works without
     going through the JSON wrapper; verifies real return-key shape.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_nap_payload_")
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


# ---------------------------------------------------------------------------
# 1. Invalid JSON rejection
# ---------------------------------------------------------------------------

def test_invalid_json_rejected():
    """A payload_json string that isn't valid JSON returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_nap(payload_json='{"message": "x", not valid json'))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "Invalid JSON" in r["error"], (
        f"Expected 'Invalid JSON' in error message, got: {r['error']}"
    )


# ---------------------------------------------------------------------------
# 2. Non-object JSON payload rejection
# ---------------------------------------------------------------------------

def test_non_object_payload_rejected():
    """A valid-JSON but non-object payload (e.g. a list) returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_nap(payload_json=json.dumps([1, 2, 3])))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "must be a JSON object" in r["error"], (
        f"Expected 'must be a JSON object' in error, got: {r['error']}"
    )


# ---------------------------------------------------------------------------
# 3. Unknown-field rejection
# ---------------------------------------------------------------------------

def test_unknown_field_rejected():
    """Payload containing an unrecognised field returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_nap(payload_json=json.dumps({
        "message": "test nap message",
        "bogus_field": "should be rejected",
    })))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "Unknown fields" in r["error"], (
        f"Expected 'Unknown fields' in error message, got: {r['error']}"
    )
    assert "bogus_field" in r["error"], (
        f"Expected rejected field name in error, got: {r['error']}"
    )


# ---------------------------------------------------------------------------
# 4. Missing required field 'message'
# ---------------------------------------------------------------------------

def test_missing_required_message_rejected():
    """A payload with no 'message' returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_nap(payload_json=json.dumps({})))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "message" in r["error"], (
        f"Expected required-field 'message' in error, got: {r['error']}"
    )


# ---------------------------------------------------------------------------
# 5. _nap_impl callable with named kwargs
# ---------------------------------------------------------------------------

def test_impl_callable_with_kwargs():
    """_nap_impl is directly callable with named kwargs (in-server caller path).

    Verifies real return-key shape from _checkpoint_internal (nap mode):
    status, mode, previous_turn, current_turn, turn_advanced.
    """
    server = fresh_server()
    result_str = server._nap_impl(message="Test nap impl callable check.")
    r = json.loads(result_str)
    assert "error" not in r, f"_nap_impl returned error: {r}"
    assert "status" in r, f"_nap_impl missing 'status' key: {r}"
    assert "mode" in r, f"_nap_impl missing 'mode' key: {r}"
    assert "previous_turn" in r, f"_nap_impl missing 'previous_turn' key: {r}"
    assert "current_turn" in r, f"_nap_impl missing 'current_turn' key: {r}"
    assert "turn_advanced" in r, f"_nap_impl missing 'turn_advanced' key: {r}"
    # Nap must NOT advance the turn.
    assert r["turn_advanced"] is False, (
        f"_nap_impl unexpectedly advanced the turn: {r}"
    )
    assert r["previous_turn"] == r["current_turn"], (
        f"_nap_impl changed turn in nap mode: previous={r['previous_turn']}, current={r['current_turn']}"
    )
