"""Tests for _as_csv() list-coercion helper and observation type alias (issue #1049).

Coverage:
  1. _as_csv(["ob_0001", "ob_0002"]) → "ob_0001,ob_0002"
  2. _as_csv("ob_0001,ob_0002") → "ob_0001,ob_0002" (passthrough)
  3. _as_csv(None) → ""
  4. _as_csv([]) → ""
  5. _as_csv applied to context_ids: _as_csv(["ob_0001"]).split(",") → ["ob_0001"]
  6. Integration: engram_query with types=["observation_factual"] — no AttributeError
  7. Integration: engram_query with types="observation" — alias resolves, returns
     same results as types="observation_factual" (Bug 2 alias fix)
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_coerce_csv_")
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
# Unit tests for _as_csv() helper
# ---------------------------------------------------------------------------

def test_as_csv_list_two_items():
    """_as_csv(["ob_0001", "ob_0002"]) → "ob_0001,ob_0002"."""
    import engram_core as core
    result = core._as_csv(["ob_0001", "ob_0002"])
    assert result == "ob_0001,ob_0002", repr(result)


def test_as_csv_string_passthrough():
    """_as_csv("ob_0001,ob_0002") passes through unchanged."""
    import engram_core as core
    result = core._as_csv("ob_0001,ob_0002")
    assert result == "ob_0001,ob_0002", repr(result)


def test_as_csv_none():
    """_as_csv(None) → empty string."""
    import engram_core as core
    result = core._as_csv(None)
    assert result == "", repr(result)


def test_as_csv_empty_list():
    """_as_csv([]) → empty string."""
    import engram_core as core
    result = core._as_csv([])
    assert result == "", repr(result)


def test_as_csv_split_roundtrip():
    """_as_csv(["ob_0001"]).split(",") → ["ob_0001"] (context_ids round-trip)."""
    import engram_core as core
    ids = [s.strip() for s in core._as_csv(["ob_0001"]).split(",") if s.strip()]
    assert ids == ["ob_0001"], ids


# ---------------------------------------------------------------------------
# Integration: types=list does not crash (Bug 1 fix)
# ---------------------------------------------------------------------------

def _add_observation(server):
    """Seed one observation_factual node and return its ID."""
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/coerce-test",
        "title": "Coerce CSV Test Source",
        "quoted_text": "List-coerce integration test claim verbatim.",
        "interpretation": "Integration test for list-coerce fix.",
        "claim": "List-coerce integration test claim.",
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def test_query_types_list_no_crash():
    """engram_query with types=['observation_factual'] must not raise AttributeError."""
    server = fresh_server()
    _add_observation(server)

    result_str = server._query_impl(
        query="list-coerce integration test",
        types=["observation_factual"],
        top_k=5,
    )
    r = json.loads(result_str)
    assert "error" not in r, f"_query_impl returned error: {r}"
    assert "results" in r or "count" in r, f"Unexpected shape: {r}"


def test_query_types_empty_list_no_crash():
    """engram_query with types=[] must not crash and returns all types (no filter)."""
    server = fresh_server()
    _add_observation(server)

    result_str = server._query_impl(
        query="list-coerce integration test",
        types=[],
        top_k=5,
    )
    r = json.loads(result_str)
    assert "error" not in r, f"_query_impl returned error: {r}"


# ---------------------------------------------------------------------------
# Integration: types="observation" alias resolves to "observation_factual" (Bug 2 fix)
# ---------------------------------------------------------------------------

def test_query_types_observation_alias():
    """types='observation' alias must return the same results as types='observation_factual'."""
    server = fresh_server()
    obs_id = _add_observation(server)

    # Query with the explicit full type name.
    r_full = json.loads(server._query_impl(
        query="list-coerce integration test",
        types="observation_factual",
        top_k=20,
    ))
    assert "error" not in r_full, f"full-type query failed: {r_full}"

    # Query with the short alias.
    r_alias = json.loads(server._query_impl(
        query="list-coerce integration test",
        types="observation",
        top_k=20,
    ))
    assert "error" not in r_alias, f"alias query failed: {r_alias}"

    # Both must return the seeded observation.
    def _ids(r):
        results = r.get("results", [])
        # Results may be tiered (list of dicts with "nodes") or flat (list of node dicts).
        ids = set()
        for item in results:
            if "nodes" in item:
                ids.update(n.get("id") for n in item["nodes"])
            elif "id" in item:
                ids.add(item["id"])
        return ids

    full_ids = _ids(r_full)
    alias_ids = _ids(r_alias)
    assert obs_id in full_ids, f"observation not in full-type results: {full_ids}"
    assert obs_id in alias_ids, f"observation not in alias results (Bug 2 not fixed): {alias_ids}"


def test_query_types_ob_prefix_alias():
    """types='ob' prefix alias must also resolve to observation_factual."""
    server = fresh_server()
    obs_id = _add_observation(server)
    r = json.loads(server._query_impl(
        query="list-coerce integration test",
        types="ob",
        top_k=20,
    ))
    assert "error" not in r, f"ob-alias query failed: {r}"
    ids = set()
    for item in r.get("results", []):
        if "nodes" in item:
            ids.update(n.get("id") for n in item["nodes"])
        elif "id" in item:
            ids.add(item["id"])
    assert obs_id in ids, f"ob alias did not resolve to observation_factual: {ids}"
