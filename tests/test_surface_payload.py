"""Tests for the engram_surface payload_json wrapper (migrate/read-cluster-payload-json).

Coverage:
  1. Unknown-field rejection — payload with an unrecognised key returns {"error": ...}
     with "Unknown fields" in the message.
  2. _surface_impl callable with named kwargs — internal function works without
     going through the JSON wrapper.
  3. Invalid JSON rejection — non-parseable payload_json string returns {"error": ...}
     with "Invalid JSON" in the message.
  4. Non-object JSON payload rejection — valid JSON but non-object (e.g. a list)
     returns {"error": ...} with "must be a JSON object" in the message.
  5. Missing required field 'query' — payload without 'query' returns {"error": ...}.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_surface_payload_")
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


# ---------------------------------------------------------------------------
# 1. Unknown-field rejection
# ---------------------------------------------------------------------------

def test_unknown_field_rejected():
    """Payload containing an unrecognised field returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_surface(payload_json=json.dumps({
        "query": "some search term",
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
# 2. _surface_impl callable with named kwargs
# ---------------------------------------------------------------------------

def test_impl_callable_with_kwargs():
    """_surface_impl is directly callable with named kwargs (in-server caller path)."""
    server = fresh_server()
    # Add an observation so the surface has something to return.
    r_add = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/surface-payload-test",
        "title": "Test Source",
        "quoted_text": "Surface impl kwargs test claim verbatim.",
        "interpretation": "Test interpretation for surface kwargs callable check.",
        "claim": "Surface impl kwargs callable test claim.",
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r_add, f"add_observation failed: {r_add}"

    # Call _surface_impl directly with named kwargs — must not raise, must return valid JSON.
    result_str = server._surface_impl(query="surface impl kwargs", top_k=5, semantic=False)
    r = json.loads(result_str)
    assert "error" not in r, f"_surface_impl returned error: {r}"
    assert "top_claims" in r or "type_counts" in r, (
        f"_surface_impl did not return expected shape: {r}"
    )


# ---------------------------------------------------------------------------
# 3. Invalid JSON rejection
# ---------------------------------------------------------------------------

def test_invalid_json_rejected():
    """A payload_json string that isn't valid JSON returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_surface(payload_json='{"query": "x", not valid json'))
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
    r = json.loads(server.engram_surface(payload_json=json.dumps([1, 2, 3])))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "must be a JSON object" in r["error"], (
        f"Expected 'must be a JSON object' in error, got: {r['error']}"
    )


# ---------------------------------------------------------------------------
# 5. Missing required field 'query'
# ---------------------------------------------------------------------------

def test_missing_required_query_rejected():
    """A payload with valid optional fields but no 'query' returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_surface(payload_json=json.dumps({"top_k": 5})))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "query" in r["error"], (
        f"Expected required-field 'query' in error, got: {r['error']}"
    )


# ---------------------------------------------------------------------------
# 6. Optional latency fields shape (when present)
# ---------------------------------------------------------------------------

def test_surface_impl_optional_latency_fields_shape():
    """If _surface_latency_ms appears in the result, validate its shape.

    The threshold (>500ms) will not fire in a fast CI environment, so this
    is a conditional shape test — it passes whether or not the threshold fires.
    When the fields ARE present (e.g. a slow test machine or future monkeypatching),
    they must satisfy their type contracts.
    """
    server = fresh_server()
    result_str = server._surface_impl(query="test", semantic=False)
    r = json.loads(result_str)
    assert "error" not in r, f"_surface_impl returned error: {r}"
    if "_surface_latency_ms" in r:
        assert isinstance(r["_surface_latency_ms"], int), (
            f"_surface_latency_ms must be int, got {type(r['_surface_latency_ms'])}"
        )
        assert "_surface_warning" in r, (
            "_surface_warning must be present when _surface_latency_ms is present"
        )
        assert isinstance(r["_surface_warning"], str), (
            f"_surface_warning must be str, got {type(r['_surface_warning'])}"
        )
        assert "500ms" in r["_surface_warning"], (
            f"_surface_warning must mention 500ms threshold, got: {r['_surface_warning']}"
        )
        assert "#819" in r["_surface_warning"], (
            f"_surface_warning must reference issue #819, got: {r['_surface_warning']}"
        )


# ---------------------------------------------------------------------------
# 7. Latency warning fires when _search_nodes is slow (mock-patched)
# ---------------------------------------------------------------------------

def test_surface_latency_warning_fires_when_slow():
    """Verify latency fields are injected when _search_nodes exceeds 500ms.

    Uses mock.patch to make time.perf_counter return values that simulate a
    600ms search phase, then asserts the fields appear with correct types.
    """
    import unittest.mock as mock
    server = fresh_server()

    # side_effect: first call (t0) returns 0.0, second call returns 0.6 (600ms elapsed).
    with mock.patch("engram_query.time.perf_counter", side_effect=[0.0, 0.6]):
        result_str = server._surface_impl(query="latency_test", semantic=False)

    r = json.loads(result_str)
    assert "error" not in r, f"_surface_impl returned error: {r}"
    assert "_surface_latency_ms" in r, (
        f"Expected _surface_latency_ms in result when simulated >500ms, got: {list(r.keys())}"
    )
    assert r["_surface_latency_ms"] == 600, (
        f"Expected 600ms, got {r['_surface_latency_ms']}"
    )
    assert "_surface_warning" in r, "_surface_warning must accompany _surface_latency_ms"
    assert "500ms" in r["_surface_warning"], (
        f"Warning must mention 500ms threshold: {r['_surface_warning']}"
    )
    assert "#819" in r["_surface_warning"], (
        f"Warning must reference issue #819: {r['_surface_warning']}"
    )
