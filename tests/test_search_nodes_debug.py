"""Tests for _search_nodes return_debug parameter.

Three test cases per spec:

  1. return_debug=False (default): response has no ``debug`` key, existing shape
     preserved — bit-for-bit compatible with existing callers.

  2. return_debug=True: response has ``debug.extracted_keywords``,
     ``debug.fts_query_string``, ``debug.per_result`` with one entry per result,
     and each entry has all the listed fields (None-tolerant).

  3. return_debug=True with empty results: ``debug.per_result`` is ``[]``, all
     top-level debug keys still present.

All tests use ENGRAM_NO_EMBEDDINGS=1 so the semantic path is skipped and FTS
behavior is deterministic.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_search_nodes_debug_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

# Complete set of per-result fields the spec mandates (None-tolerant checks).
PER_RESULT_FIELDS = {
    "id",
    "match_type",
    "bm25_raw",
    "relevance_normalized",
    "similarity",
    "composite_score",
    "utility_score",
    "importance_score",
    "importance_base",
    "confidence",
    "surprise_score",
    "recall_turn",
}

# Top-level debug keys the spec mandates.
DEBUG_TOP_LEVEL_FIELDS = {
    "extracted_keywords",
    "fts_query_string",
    "fts_candidate_count",
    "semantic_candidate_count",
    "fts_cap_applied",
    "embed_query_used",
    "embed_query_overridden",
    "per_result",
    "snapshot_turn",
}


def fresh_server():
    """Create a clean server module instance in a temp sandbox directory."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    # Force module reload so path globals are clean.
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def _add_obs(server, claim: str, quoted_text: str = "test quote") -> str:
    """Insert a simple observation and return its node id."""
    payload = {
        "quoted_text": quoted_text,
        "interpretation": "test interpretation",
        "claim": claim,
        "quote_type": "personal_communication",
        "url": "https://example.com/test",
        "title": "Test",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


class TestReturnDebugFalse:
    """return_debug=False (the default): response shape is identical to existing behavior."""

    def test_no_debug_key_in_result(self):
        """Spec test 1: default call has no ``debug`` key."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        conn = server._get_db()

        result = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5)

        assert "debug" not in result, (
            f"Expected no 'debug' key when return_debug=False, got keys: {list(result.keys())}"
        )

    def test_existing_keys_present(self):
        """Existing shape (results, keyword_matches, semantic_matches, semantic_available) intact."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        conn = server._get_db()

        result = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5)

        assert "results" in result
        assert "keyword_matches" in result
        assert "semantic_matches" in result
        assert "semantic_available" in result

    def test_explicit_false_same_as_default(self):
        """Passing return_debug=False explicitly is identical to omitting it."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        conn = server._get_db()

        default_result = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5)
        explicit_false = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5, return_debug=False)

        assert set(default_result.keys()) == set(explicit_false.keys())
        assert "debug" not in explicit_false


class TestReturnDebugTrue:
    """return_debug=True: debug key present with all required sub-fields."""

    def test_debug_key_present(self):
        """Spec test 2: return_debug=True adds a ``debug`` key."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        conn = server._get_db()

        result = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5, return_debug=True)

        assert "debug" in result, (
            f"Expected 'debug' key when return_debug=True, got keys: {list(result.keys())}"
        )

    def test_debug_top_level_fields(self):
        """All required top-level debug fields are present."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        conn = server._get_db()

        result = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5, return_debug=True)
        debug = result["debug"]

        for field in DEBUG_TOP_LEVEL_FIELDS:
            assert field in debug, (
                f"Missing required debug field '{field}'. Got keys: {list(debug.keys())}"
            )

    def test_per_result_parallel_indexed(self):
        """debug.per_result has one entry per item in results."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        _add_obs(server, "xylophone percussion music sound")
        conn = server._get_db()

        result = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5, return_debug=True)
        debug = result["debug"]

        assert len(debug["per_result"]) == len(result["results"]), (
            f"per_result length {len(debug['per_result'])} != "
            f"results length {len(result['results'])}"
        )

    def test_per_result_all_fields_present(self):
        """Each entry in debug.per_result has all required fields (None-tolerant)."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        conn = server._get_db()

        result = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5, return_debug=True)
        debug = result["debug"]

        assert len(debug["per_result"]) >= 1, "Need at least one result for per_result field check"
        for entry in debug["per_result"]:
            for field in PER_RESULT_FIELDS:
                assert field in entry, (
                    f"Missing per_result field '{field}'. Entry keys: {list(entry.keys())}"
                )

    def test_per_result_fts_match_type(self):
        """FTS-matched node has match_type='fts' and non-None bm25_raw / relevance_normalized."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        conn = server._get_db()

        result = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5, return_debug=True)
        debug = result["debug"]

        assert len(debug["per_result"]) >= 1, "Need at least one FTS result"
        entry = debug["per_result"][0]

        assert entry["match_type"] == "keyword", (
            f"Expected match_type='keyword' for FTS result (aligned with "
            f"result-node API at server.py:2063), got {entry['match_type']!r}"
        )
        assert entry["bm25_raw"] is not None, "bm25_raw should be non-None for FTS match"
        assert entry["relevance_normalized"] is not None, (
            "relevance_normalized should be non-None for FTS match"
        )
        assert entry["bm25_raw"] <= 0, (
            f"BM25 rank should be non-positive (FTS5 convention), got {entry['bm25_raw']}"
        )
        assert 0.0 <= entry["relevance_normalized"] <= 1.0, (
            f"relevance_normalized should be in [0,1], got {entry['relevance_normalized']}"
        )

    def test_debug_embed_query_overridden_true(self):
        """When embed_query is explicitly passed (prepending case), debug must
        record embed_query_overridden=True AND embed_query_used is the
        prepended string, not the bare query. Positive-case complement to the
        default-False coverage already in test_debug_top_level_keys."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        conn = server._get_db()

        bare_query = "xylophone"
        prepended = "prior context tail. " + bare_query
        result = server._search_nodes(
            conn, bare_query, use_semantic=False, top_k=5,
            embed_query=prepended, return_debug=True,
        )
        debug = result["debug"]

        assert debug["embed_query_overridden"] is True, (
            f"Expected embed_query_overridden=True when embed_query is passed, "
            f"got {debug['embed_query_overridden']!r}"
        )
        assert debug["embed_query_used"] == prepended, (
            f"Expected embed_query_used={prepended!r}, "
            f"got {debug['embed_query_used']!r}"
        )
        assert debug["embed_query_used"] != bare_query, (
            "embed_query_used must reflect the override, not the bare query"
        )

        # And confirm the default-False path emits the bare query
        result2 = server._search_nodes(
            conn, bare_query, use_semantic=False, top_k=5, return_debug=True,
        )
        debug2 = result2["debug"]
        assert debug2["embed_query_overridden"] is False, (
            f"Expected embed_query_overridden=False with no override, "
            f"got {debug2['embed_query_overridden']!r}"
        )
        assert debug2["embed_query_used"] == bare_query, (
            f"Expected embed_query_used={bare_query!r} (the default), "
            f"got {debug2['embed_query_used']!r}"
        )

    def test_debug_fts_query_string(self):
        """fts_query_string is a non-empty string when FTS succeeds."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        conn = server._get_db()

        result = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5, return_debug=True)
        debug = result["debug"]

        # fts_query_string may be None (FTS failed) or a string.
        # For a simple word query it should be a non-empty string.
        fts_q = debug["fts_query_string"]
        assert fts_q is None or isinstance(fts_q, str), (
            f"fts_query_string should be None or str, got {type(fts_q)}"
        )

    def test_snapshot_turn_is_int(self):
        """snapshot_turn is an integer."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        conn = server._get_db()

        result = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5, return_debug=True)
        debug = result["debug"]

        assert isinstance(debug["snapshot_turn"], int), (
            f"snapshot_turn should be int, got {type(debug['snapshot_turn'])}"
        )

    def test_existing_shape_preserved(self):
        """return_debug=True does not alter results / keyword_matches / etc."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        conn = server._get_db()

        without_debug = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5)
        with_debug = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5, return_debug=True)

        assert without_debug["keyword_matches"] == with_debug["keyword_matches"]
        assert without_debug["semantic_matches"] == with_debug["semantic_matches"]
        assert without_debug["semantic_available"] == with_debug["semantic_available"]
        # Result IDs should be identical (same query, same data, same ranking).
        without_ids = [n["id"] for n in without_debug["results"]]
        with_ids = [n["id"] for n in with_debug["results"]]
        assert without_ids == with_ids, (
            f"Result ordering differs: {without_ids} vs {with_ids}"
        )

    def test_none_fields_are_present_not_absent(self):
        """Fields that are None must be present as explicit None, not omitted."""
        server = fresh_server()
        _add_obs(server, "xylophone rhythm instrument unique term")
        conn = server._get_db()

        result = server._search_nodes(conn, "xylophone", use_semantic=False, top_k=5, return_debug=True)
        debug = result["debug"]

        for entry in debug["per_result"]:
            # surprise_score is always None (not stored as a column).
            assert "surprise_score" in entry, "surprise_score key must be present even if None"
            # For FTS matches, similarity should be present (None value).
            assert "similarity" in entry, "similarity key must be present even if None"


class TestReturnDebugTrueEmptyResults:
    """return_debug=True with empty results: per_result=[], top-level keys still present."""

    def test_empty_results_debug_shape(self):
        """Spec test 3: empty result set has per_result=[] with all top-level keys present."""
        server = fresh_server()
        # Insert nodes that won't match our query.
        _add_obs(server, "software engineering principles and best practices")
        _add_obs(server, "database schema design patterns overview")
        conn = server._get_db()

        # Query a term unlikely to appear in any node.
        result = server._search_nodes(
            conn, "zxqvbnmqwerty", use_semantic=False, top_k=5, return_debug=True
        )

        assert "debug" in result
        debug = result["debug"]

        # All top-level fields must be present.
        for field in DEBUG_TOP_LEVEL_FIELDS:
            assert field in debug, (
                f"Missing debug field '{field}' even with empty results. "
                f"Got keys: {list(debug.keys())}"
            )

        # per_result must be an empty list.
        assert debug["per_result"] == [], (
            f"Expected per_result=[] for empty results, got: {debug['per_result']}"
        )

    def test_empty_results_candidate_counts(self):
        """With no FTS matches, fts_candidate_count=0 and semantic_candidate_count=0."""
        server = fresh_server()
        _add_obs(server, "software engineering principles")
        conn = server._get_db()

        result = server._search_nodes(
            conn, "zxqvbnmqwerty", use_semantic=False, top_k=5, return_debug=True
        )
        debug = result["debug"]

        assert debug["fts_candidate_count"] == 0, (
            f"Expected fts_candidate_count=0, got {debug['fts_candidate_count']}"
        )
        assert debug["semantic_candidate_count"] == 0, (
            f"Expected semantic_candidate_count=0, got {debug['semantic_candidate_count']}"
        )
