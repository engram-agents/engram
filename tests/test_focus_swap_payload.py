"""Tests for the engram_focus_swap payload_json wrapper (migrate/focus-cluster-payload-json).

Coverage:
  1. Unknown-field rejection — payload with an unrecognised key returns {"error": ...}
     with "Unknown fields" in the message.
  2. _focus_swap_impl callable with named kwargs — internal function works without
     going through the JSON wrapper.
  3. Invalid JSON rejection — non-parseable payload_json string returns {"error": ...}
     with "Invalid JSON" in the message.
  4. Non-object JSON payload rejection — valid JSON but non-object (e.g. a list)
     returns {"error": ...} with "must be a JSON object" in the message.
  5. Missing required field 'save_as' — payload without 'save_as' returns {"error": ...}.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_focus_swap_payload")


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
# 1. Unknown-field rejection
# ---------------------------------------------------------------------------

def test_unknown_field_rejected():
    """Payload containing an unrecognised field returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_focus_swap(payload_json=json.dumps({
        "save_as": "my-set",
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
# 2. _focus_swap_impl callable with named kwargs
# ---------------------------------------------------------------------------

def test_impl_callable_with_kwargs():
    """_focus_swap_impl is directly callable with named kwargs (in-server caller path)."""
    server = fresh_server()
    # Set up: two axioms, one focused (becomes active list), one in a saved set.
    r_a = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": "Focus swap impl kwargs test axiom A.",
        "basis": "Test basis A.",
    })))
    r_b = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": "Focus swap impl kwargs test axiom B.",
        "basis": "Test basis B.",
    })))
    assert "axiom_id" in r_a, f"add_axiom A failed: {r_a}"
    assert "axiom_id" in r_b, f"add_axiom B failed: {r_b}"
    ax_a = r_a["axiom_id"]
    ax_b = r_b["axiom_id"]

    # Focus ax_b and save as a named set, then switch active list to ax_a.
    server._focus_impl(node_ids=ax_b, reason="setup: save b as load target")
    server._focus_save_impl(name="swap-target-set", description="set to load in swap")
    server._unfocus_impl(node_ids=ax_b)
    server._focus_impl(node_ids=ax_a, reason="setup: ax_a is current active")

    # Call _focus_swap_impl directly with named kwargs — must not raise, must return valid JSON.
    result_str = server._focus_swap_impl(save_as="swap-source-set", load="swap-target-set")
    r = json.loads(result_str)
    assert "error" not in r, f"_focus_swap_impl returned error: {r}"
    assert "status" in r and "saved" in r, (
        f"_focus_swap_impl did not return expected shape: {r}"
    )


# ---------------------------------------------------------------------------
# 3. Invalid JSON rejection
# ---------------------------------------------------------------------------

def test_invalid_json_rejected():
    """A payload_json string that isn't valid JSON returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_focus_swap(payload_json='{"save_as": "my-set", not valid json'))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "Invalid JSON" in r["error"], (
        f"Expected 'Invalid JSON' in error message, got: {r['error']}"
    )


# ---------------------------------------------------------------------------
# 4. Non-object JSON payload rejection
# ---------------------------------------------------------------------------

def test_non_object_payload_rejected():
    """A valid-JSON but non-object payload (e.g. a list) returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_focus_swap(payload_json=json.dumps([1, 2, 3])))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "must be a JSON object" in r["error"], (
        f"Expected 'must be a JSON object' in error, got: {r['error']}"
    )


# ---------------------------------------------------------------------------
# 5. Missing required field 'save_as'
# ---------------------------------------------------------------------------

def test_missing_required_save_as_rejected():
    """A payload with optional fields but no 'save_as' returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_focus_swap(payload_json=json.dumps({"load": "some-set"})))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "save_as" in r["error"], (
        f"Expected required-field 'save_as' in error, got: {r['error']}"
    )
