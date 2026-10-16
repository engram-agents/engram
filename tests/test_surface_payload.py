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


# ---------------------------------------------------------------------------
# 8. include_query_embedding (#257 rec 1, extends #1700 — issue #1778):
#    opt-in machinery flag for the surface daemon's prompt-similarity decay.
# ---------------------------------------------------------------------------

def test_query_embedding_absent_by_default():
    """Ordinary engram_surface calls (the flag omitted) never get a
    query_embedding key — it's daemon-only machinery, not agent-facing."""
    server = fresh_server()
    r = json.loads(server.engram_surface(payload_json=json.dumps({"query": "some query"})))
    assert "error" not in r, f"Unexpected error: {r}"
    assert "query_embedding" not in r, (
        f"query_embedding must be absent when include_query_embedding is omitted, got keys: {list(r.keys())}"
    )


def test_query_embedding_field_accepted_not_unknown():
    """include_query_embedding is a recognised field, not rejected as unknown."""
    server = fresh_server()
    r = json.loads(server.engram_surface(payload_json=json.dumps({
        "query": "some query", "include_query_embedding": True,
    })))
    assert "error" not in r, f"include_query_embedding should be accepted, got: {r}"


def _seed_one_observation(server, url_suffix: str):
    """Add one observation so a subsequent surface query has a match — the
    query_embedding tests below need match_count > 0 (the no_match early
    return never carries query_embedding: with nothing to render, the hook
    never reaches the ledger write that would need it)."""
    r_add = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": f"https://example.com/{url_suffix}",
        "title": "Test Source",
        "quoted_text": "Query embedding test claim verbatim.",
        "interpretation": "Test interpretation for query_embedding check.",
        "claim": "Query embedding test claim.",
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r_add, f"add_observation failed: {r_add}"


def test_query_embedding_none_when_embedder_unavailable():
    """With the flag set but embeddings disabled (this file's fixture sets
    ENGRAM_NO_EMBEDDINGS=1), the key is present but explicitly None — the
    opt-in adds the key regardless of whether a real vector was computed,
    so the daemon can distinguish 'no vector' from 'didn't ask'."""
    server = fresh_server()
    _seed_one_observation(server, "query-embedding-none-test")
    r = json.loads(server.engram_surface(payload_json=json.dumps({
        "query": "query embedding test claim", "include_query_embedding": True,
    })))
    assert "error" not in r, f"Unexpected error: {r}"
    assert r.get("match_count", 0) > 0, f"Expected a match, got: {r}"
    assert "query_embedding" in r, f"Expected query_embedding key, got keys: {list(r.keys())}"
    assert r["query_embedding"] is None


def test_query_embedding_returns_computed_vector_when_available():
    """When the embedder IS available, include_query_embedding=True returns
    the actual vector computed for this query (mock-patched, mirroring the
    latency test's monkeypatch style above)."""
    import unittest.mock as mock
    server = fresh_server()
    _seed_one_observation(server, "query-embedding-vector-test")
    fake_vector = [0.1, 0.2, 0.3]
    with mock.patch("engram_query.core._embedder.is_available", return_value=True), \
         mock.patch("engram_query.core._get_embedding_config", return_value={"model": "fake-model", "enabled": True}), \
         mock.patch("engram_query.core._embedder.embed", return_value=fake_vector):
        result_str = server._surface_impl(
            query="query embedding test claim", semantic=False, include_query_embedding=True,
        )
    r = json.loads(result_str)
    assert "error" not in r, f"Unexpected error: {r}"
    assert r.get("match_count", 0) > 0, f"Expected a match, got: {r}"
    assert r.get("query_embedding") == fake_vector, (
        f"Expected the mocked vector to pass through unchanged, got: {r.get('query_embedding')}"
    )


def test_query_embedding_ignores_enabled_false():
    """(#345 PR2, A2) The `include_query_embedding` gate (engram_query.py
    ~2456) no longer consults the retired `enabled` key: with
    `enabled: False` mocked (embedder otherwise available), the computed
    vector must still pass through -- identical setup to
    test_query_embedding_returns_computed_vector_when_available, just
    enabled=False.

    Mutation target: restoring the
    `core._get_embedding_config().get("enabled", True)` check here turns
    this RED (query_embedding reverts to None).
    """
    import unittest.mock as mock
    server = fresh_server()
    _seed_one_observation(server, "query-embedding-enabled-false-test")
    fake_vector = [0.4, 0.5, 0.6]
    with mock.patch("engram_query.core._embedder.is_available", return_value=True), \
         mock.patch("engram_query.core._get_embedding_config", return_value={"model": "fake-model", "enabled": False}), \
         mock.patch("engram_query.core._embedder.embed", return_value=fake_vector):
        result_str = server._surface_impl(
            query="query embedding test claim", semantic=False, include_query_embedding=True,
        )
    r = json.loads(result_str)
    assert "error" not in r, f"Unexpected error: {r}"
    assert r.get("match_count", 0) > 0, f"Expected a match, got: {r}"
    assert r.get("query_embedding") == fake_vector, (
        f"expected the mocked vector despite enabled=False, got: {r.get('query_embedding')}"
    )


# ---------------------------------------------------------------------------
# 9. _created_minutes_ago (engram_core) — companion to _humanized_ago, feeds
#    the same-session-echo guard's numeric N-minutes threshold check.
# ---------------------------------------------------------------------------

def test_created_minutes_ago_basic():
    from datetime import datetime, timedelta, timezone
    server = fresh_server()
    now = datetime.now(timezone.utc)
    then_iso = (now - timedelta(minutes=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mins = server.core._created_minutes_ago(then_iso, now=now)
    assert mins is not None
    # Tolerance covers the up-to-1s quantization from then_iso's whole-second
    # ISO format (no fractional seconds) against a microsecond-precision `now`.
    assert abs(mins - 7.0) < 0.02


def test_created_minutes_ago_missing_input_is_none():
    server = fresh_server()
    assert server.core._created_minutes_ago(None) is None
    assert server.core._created_minutes_ago("") is None


def test_created_minutes_ago_malformed_is_none():
    server = fresh_server()
    assert server.core._created_minutes_ago("not-a-timestamp") is None


def test_created_minutes_ago_future_timestamp_is_none():
    """A timestamp ahead of `now` (clock skew / bad data) returns None rather
    than a negative number — callers gate on `< N minutes`, and a negative
    value would incorrectly satisfy that check."""
    from datetime import datetime, timedelta, timezone
    server = fresh_server()
    now = datetime.now(timezone.utc)
    future_iso = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert server.core._created_minutes_ago(future_iso, now=now) is None
