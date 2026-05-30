"""Tests for the engram_query payload_json wrapper (migrate/engram-query-payload-json).

Coverage:
  1. Unknown-field rejection — payload with an unrecognised key returns {"error": ...}
     with "Unknown fields" in the message.
  2. _query_impl callable with named kwargs — internal function works without
     going through the JSON wrapper (mirrors test_resolve_payload.py:test_impl_callable_with_kwargs).
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_query_payload_")
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
    r = json.loads(server.engram_query(payload_json=json.dumps({
        "query": "some search query",
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
# 2. _query_impl callable with named kwargs
# ---------------------------------------------------------------------------

def test_impl_callable_with_kwargs():
    """_query_impl is directly callable with named kwargs (in-server caller path)."""
    server = fresh_server()
    # Add an observation so the query has something to find.
    r_add = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/test",
        "title": "Test Source",
        "quoted_text": "Payload kwargs test claim verbatim.",
        "interpretation": "Test interpretation for kwargs callable check.",
        "claim": "Payload kwargs impl callable test claim.",
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r_add, f"add_observation failed: {r_add}"

    # Call _query_impl directly with named kwargs — must not raise, must return valid JSON.
    result_str = server._query_impl(query="payload kwargs impl callable", top_k=5)
    r = json.loads(result_str)
    # The result must be either the tiered shape (has "results" key) or debug shape
    # (has "count" key) — not an error.
    assert "error" not in r, f"_query_impl returned error: {r}"
    assert "results" in r or "count" in r, (
        f"_query_impl did not return expected shape: {r}"
    )


# ---------------------------------------------------------------------------
# 3. Invalid JSON rejection
# ---------------------------------------------------------------------------

def test_invalid_json_rejected():
    """A payload_json string that isn't valid JSON returns an error dict."""
    server = fresh_server()
    r = json.loads(server.engram_query(payload_json='{"query": "x", not valid json'))
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
    r = json.loads(server.engram_query(payload_json=json.dumps([1, 2, 3])))
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
    r = json.loads(server.engram_query(payload_json=json.dumps({"top_k": 5})))
    assert "error" in r, f"Expected error key, got: {r}"
    assert "query" in r["error"], (
        f"Expected required-field 'query' in error, got: {r['error']}"
    )
