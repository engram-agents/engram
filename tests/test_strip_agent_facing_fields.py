"""Tests for agent-facing field stripping (issue #358 / qu_0016).

Coverage:
  1. _strip_agent_facing helper — removes strip-list keys recursively,
     preserves all other keys, handles lists and nested dicts.
     Note: match_type + similarity are NOT globally stripped by this helper;
     they are stripped only at similar_existing / similar_existing_lessons
     assembly sites via _strip_similar_block().
  2. engram_inspect (recall view) — stripped fields absent from node dict.
  3. engram_inspect (deep view) — stripped fields absent from node dict.
  4. engram_add_observation — match_type + similarity absent from
     similar_existing entries; action_hint KEPT.
  5. engram_add_lesson — match_type + similarity absent from
     similar_existing_lessons entries.
  6. engram_query (debug mode) — match_type + similarity PRESENT in result
     nodes and debug.per_result (eval-harness contract, not stripped globally).
  7. DB integrity — stripped fields still present in DB after stripping.
  8. inspect_raw.py CLI — returns the raw field correctly from DB.
  9. engram_query_pattern: alternates/candidate_resolution/suggested_action are
     discarded by _build_tiered_results and do NOT reach the agent (tracked in
     #366); only the tiered {id, confidence, summary} shape is returned.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSPECT_RAW = os.path.join(REPO_ROOT, "tools", "inspect_raw.py")

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_strip_agent_facing_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

# fresh_server() mutates these env vars process-wide; capture their pre-test
# values and restore them at module teardown so they don't leak into later test
# modules. Without this, ENGRAM_HOME=TEST_DIR leaks and a later test that sets
# neither var would resolve to the leaked TEST_DIR instead of its real default.
# The leaker restoring its own mutation is the fix; well-behaved scoped patch.dict
# victims need no change.
_ORIG_ENV = {k: os.environ.get(k) for k in ("ENGRAM_HOME", "ENGRAM_NO_EMBEDDINGS")}


def teardown_module(module):
    for _k, _v in _ORIG_ENV.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_HOME"] = TEST_DIR
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def _add_obs(server, claim="Test claim", url="https://example.com/src"):
    """Insert a minimal observation, return its id."""
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test Source",
        "quoted_text": claim[:80],
        "interpretation": "Test interpretation.",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def _db_path(server):
    return str(server.core.DB_PATH)


# ---------------------------------------------------------------------------
# 1. _strip_agent_facing helper unit tests
# ---------------------------------------------------------------------------

class TestStripAgentFacingHelper:
    def test_removes_embedding(self):
        server = fresh_server()
        obj = {"id": "ob_0001", "claim": "x", "embedding": [0.1, 0.2]}
        result = server._strip_agent_facing(obj)
        assert "embedding" not in result
        assert result["id"] == "ob_0001"
        assert result["claim"] == "x"

    def test_content_hash_not_stripped_globally(self):
        """content_hash is stored in the metadata JSON blob, not surfaced as a
        top-level key by _node_to_dict — so stripping it globally is dead code.
        It has been removed from _AGENT_STRIP_KEYS. If it somehow appeared at
        top level it would NOT be stripped (which is fine — it never does in
        practice). This test documents the current behavior."""
        server = fresh_server()
        obj = {"id": "ob_0001", "content_hash": "abc123"}
        result = server._strip_agent_facing(obj)
        # content_hash is not in _AGENT_STRIP_KEYS (dead code — stored in
        # metadata blob, never a top-level key from _node_to_dict)
        assert "content_hash" in result, (
            "content_hash is not globally stripped (dead code path — "
            "it lives in the metadata blob, not as a top-level node key)"
        )

    def test_git_sha_not_stripped_globally(self):
        """git_sha is stored in the metadata JSON blob, not surfaced as a
        top-level key by _node_to_dict — so stripping it globally is dead code.
        It has been removed from _AGENT_STRIP_KEYS. Documents the behavior."""
        server = fresh_server()
        obj = {"id": "ob_0001", "git_sha": "deadbeef"}
        result = server._strip_agent_facing(obj)
        # git_sha is not in _AGENT_STRIP_KEYS (dead code — stored in
        # metadata blob, never a top-level key from _node_to_dict)
        assert "git_sha" in result, (
            "git_sha is not globally stripped (dead code path — "
            "it lives in the metadata blob, not as a top-level node key)"
        )

    def test_removes_parsed_metadata(self):
        server = fresh_server()
        obj = {"id": "ob_0001", "parsed_metadata": {"source_class": "external"}}
        result = server._strip_agent_facing(obj)
        assert "parsed_metadata" not in result

    def test_removes_confidence_history(self):
        server = fresh_server()
        obj = {"id": "ob_0001", "confidence_history": [{"value": 0.85}]}
        result = server._strip_agent_facing(obj)
        assert "confidence_history" not in result

    def test_preserves_match_type_globally(self):
        """match_type is NOT stripped globally — only from similar_existing blocks
        via _strip_similar_block(). This preserves it in engram_query debug results."""
        server = fresh_server()
        obj = {"id": "ob_0001", "match_type": "semantic"}
        result = server._strip_agent_facing(obj)
        assert "match_type" in result, (
            "match_type should NOT be stripped globally — only from similar_existing blocks"
        )

    def test_preserves_similarity_globally(self):
        """similarity is NOT stripped globally — only from similar_existing blocks
        via _strip_similar_block(). This preserves it in engram_query debug results."""
        server = fresh_server()
        obj = {"id": "ob_0001", "similarity": 0.92}
        result = server._strip_agent_facing(obj)
        assert "similarity" in result, (
            "similarity should NOT be stripped globally — only from similar_existing blocks"
        )

    def test_preserves_action_hint(self):
        server = fresh_server()
        obj = {"id": "ob_0001", "action_hint": "CORROBORATE — ...", "similarity": 0.9}
        result = server._strip_agent_facing(obj)
        assert "action_hint" in result
        # similarity preserved globally (stripped only in similar_existing context)
        assert "similarity" in result

    def test_recursive_strip_in_nested_list(self):
        """Strip applies recursively through lists of dicts, removing _AGENT_STRIP_KEYS.
        match_type is NOT in the global strip — it is preserved here."""
        server = fresh_server()
        obj = {
            "results": [
                {"id": "ob_0001", "embedding": [1, 2, 3], "claim": "x"},
                {"id": "ob_0002", "match_type": "keyword", "claim": "y"},
            ]
        }
        result = server._strip_agent_facing(obj)
        for item in result["results"]:
            assert "embedding" not in item
        # match_type is preserved globally (not in _AGENT_STRIP_KEYS)
        assert "match_type" in result["results"][1]
        assert result["results"][0]["claim"] == "x"
        assert result["results"][1]["claim"] == "y"

    def test_recursive_strip_in_nested_dict(self):
        """Strip applies recursively through nested dicts."""
        server = fresh_server()
        obj = {
            "node": {
                "id": "ob_0001",
                "confidence_history": [{"value": 0.85}],
                "claim": "x",
            }
        }
        result = server._strip_agent_facing(obj)
        assert "confidence_history" not in result["node"]
        assert result["node"]["claim"] == "x"

    def test_preserves_all_keep_fields(self):
        """Fields that must be kept are not removed."""
        server = fresh_server()
        keep_fields = {
            "id": "ob_0001",
            "type": "observation_factual",
            "claim": "Test claim",
            "confidence": 0.85,
            "status": "active",
            "recall_summary": "A summary",
            "recall_keywords": ["kw1", "kw2"],
            "importance_score": 0.7,
            "recall_count": 5,
            "recall_turn": 3,
            "neighbors": [{"neighbor_id": "dv_0001"}],
            "action_hint": "CORROBORATE — ...",
            "created_ago": "5 minutes ago",
            "last_assessed_ago": "1 hour ago",
            "memory_status": "active",
            "quoted_text": "exact quote",
            "interpretation": "agent interpretation",
            "quote_type": "hard_data",
            "source_class": "external",
            "url": "https://example.com",
            "title": "Example",
            "evidence_id": "ev_0001",
        }
        result = server._strip_agent_facing(keep_fields)
        for k in keep_fields:
            assert k in result, f"Keep field '{k}' was incorrectly stripped"

    def test_non_dict_passthrough(self):
        """Strings, ints, None pass through unchanged."""
        server = fresh_server()
        assert server._strip_agent_facing("hello") == "hello"
        assert server._strip_agent_facing(42) == 42
        assert server._strip_agent_facing(None) is None
        assert server._strip_agent_facing(3.14) == 3.14


# ---------------------------------------------------------------------------
# 2. engram_inspect recall view — parsed_metadata + confidence_history absent
# ---------------------------------------------------------------------------

class TestInspectRecallStripping:
    def test_parsed_metadata_absent_from_recall_view(self):
        server = fresh_server()
        obs_id = _add_obs(server, "Test claim for recall inspect.")
        r = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": obs_id, "view": "recall"})))
        assert "error" not in r, r
        node = r["node"]
        assert "parsed_metadata" not in node, (
            "parsed_metadata should be stripped from recall view"
        )

    def test_confidence_history_absent_from_recall_view(self):
        server = fresh_server()
        obs_id = _add_obs(server, "Another test claim for recall.")
        r = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": obs_id, "view": "recall"})))
        node = r["node"]
        assert "confidence_history" not in node, (
            "confidence_history should be absent from recall view node"
        )

    def test_embedding_absent_from_recall_view(self):
        """embedding should be absent (was already stripped by _node_to_dict,
        but confirmed end-to-end at the MCP return boundary too)."""
        server = fresh_server()
        obs_id = _add_obs(server, "Embedding-free recall view test.")
        r = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": obs_id, "view": "recall"})))
        node = r["node"]
        assert "embedding" not in node


# ---------------------------------------------------------------------------
# 3. engram_inspect deep view — parsed_metadata + confidence_history absent
# ---------------------------------------------------------------------------

class TestInspectDeepStripping:
    def test_parsed_metadata_absent_from_deep_view(self):
        server = fresh_server()
        obs_id = _add_obs(server, "Deep view test claim.")
        r = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": obs_id, "view": "deep"})))
        assert "error" not in r, r
        node = r["node"]
        assert "parsed_metadata" not in node, (
            "parsed_metadata should be stripped from deep view"
        )

    def test_confidence_history_absent_from_deep_view(self):
        """confidence_history should be stripped from deep view (previously present)."""
        server = fresh_server()
        obs_id = _add_obs(server, "Confidence history deep view test.")
        r = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": obs_id, "view": "deep"})))
        node = r["node"]
        assert "confidence_history" not in node, (
            "confidence_history should be stripped from deep view node"
        )

    def test_embedding_absent_from_deep_view(self):
        server = fresh_server()
        obs_id = _add_obs(server, "Embedding deep view test.")
        r = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": obs_id, "view": "deep"})))
        node = r["node"]
        assert "embedding" not in node


# ---------------------------------------------------------------------------
# 4. engram_add_observation — similar_existing stripping
# ---------------------------------------------------------------------------

class TestSimilarExistingStripping:
    def _stub_semantic_with_match(self, server, target_id, target_claim, similarity=0.85):
        """Force _semantic_search to return one controlled hit."""
        def stub(conn, query_text, **kwargs):
            return [{
                "id": target_id,
                "type": "observation_factual",
                "claim": target_claim,
                "confidence": 0.8,
                "similarity": similarity,
                "importance_score": 1.0,
            }]
        # _similar_existing_matches now lives in engram_core — patch core.
        server.core._semantic_search = stub

        class _FakeEmb:
            def is_available(self_): return True
            # #345 PR3 R3: a double substituting the whole _embedder
            # singleton must carry the same _revive_cooldown dict + revive()
            # method as the real EmbeddingManager -- production code no
            # longer hasattr-guards around a double lacking them.
            def __init__(self_):
                self_._revive_cooldown = {}
            def revive(self_, model_name): pass
        server.core._embedder = _FakeEmb()
        server.core._get_embedding_config = lambda: {"enabled": True}

    def test_match_type_absent_from_similar_existing(self):
        server = fresh_server()
        first_id = _add_obs(server, "Original anchor claim about topic Z.",
                            url="https://example.com/source-A")
        self._stub_semantic_with_match(server, first_id,
                                       "Original anchor claim about topic Z.")
        r = json.loads(server.engram_add_observation(payload_json=json.dumps({
            "url": "https://example.com/source-B",
            "title": "Source B",
            "quoted_text": "independent confirmation text",
            "interpretation": "Independent confirmation.",
            "claim": "Independent confirmation of topic Z.",
            "quote_type": "attributed_analysis",
        })))
        assert r.get("similar_count", 0) >= 1, (
            f"Expected at least 1 similar match, got: {r}"
        )
        for entry in r.get("similar_existing", []):
            assert "match_type" not in entry, (
                f"match_type should be stripped from similar_existing: {entry}"
            )

    def test_similarity_absent_from_similar_existing(self):
        server = fresh_server()
        first_id = _add_obs(server, "Original anchor claim B.",
                            url="https://example.com/source-A2")
        self._stub_semantic_with_match(server, first_id, "Original anchor claim B.")
        r = json.loads(server.engram_add_observation(payload_json=json.dumps({
            "url": "https://example.com/source-B2",
            "title": "Source B2",
            "quoted_text": "second claim text for sim test",
            "interpretation": "Confirms topic B.",
            "claim": "Confirmation of anchor B.",
            "quote_type": "attributed_analysis",
        })))
        for entry in r.get("similar_existing", []):
            assert "similarity" not in entry, (
                f"similarity should be stripped from similar_existing: {entry}"
            )

    def test_action_hint_kept_in_similar_existing(self):
        """action_hint must survive stripping — it encodes the actionable summary."""
        server = fresh_server()
        first_id = _add_obs(server, "Original anchor claim C.",
                            url="https://example.com/source-A3")
        self._stub_semantic_with_match(server, first_id, "Original anchor claim C.",
                                       similarity=0.90)
        r = json.loads(server.engram_add_observation(payload_json=json.dumps({
            "url": "https://example.com/source-B3",
            "title": "Source B3",
            "quoted_text": "confirming text for action hint check",
            "interpretation": "Confirms topic C.",
            "claim": "Confirmation of anchor C.",
            "quote_type": "attributed_analysis",
        })))
        for entry in r.get("similar_existing", []):
            assert "action_hint" in entry, (
                f"action_hint must NOT be stripped from similar_existing: {entry}"
            )


# ---------------------------------------------------------------------------
# 5. engram_add_lesson — similar_existing_lessons stripping
# ---------------------------------------------------------------------------

class TestSimilarExistingLessonsStripping:
    def _stub_semantic_lesson_match(self, server, target_id, target_claim, similarity=0.80):
        """Force _semantic_search to return one controlled lesson match."""
        def stub(conn, query_text, **kwargs):
            type_filter = kwargs.get("type_filter", set())
            if "lesson" not in type_filter:
                return []
            return [{
                "id": target_id,
                "type": "lesson",
                "claim": target_claim,
                "confidence": 0.75,
                "similarity": similarity,
                "importance_score": 1.0,
            }]
        # _similar_existing_matches now lives in engram_core — patch core.
        server.core._semantic_search = stub

        class _FakeEmb:
            def is_available(self_): return True
            # #345 PR3 R3: a double substituting the whole _embedder
            # singleton must carry the same _revive_cooldown dict + revive()
            # method as the real EmbeddingManager -- production code no
            # longer hasattr-guards around a double lacking them.
            def __init__(self_):
                self_._revive_cooldown = {}
            def revive(self_, model_name): pass
        server.core._embedder = _FakeEmb()
        server.core._get_embedding_config = lambda: {"enabled": True}

    def _create_lesson(self, server, claim, incident_id):
        r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
            "claim": claim,
            "scaffolding_nudge": "Watch for this pattern.",
            "incident_ids": incident_id,
            "logical_chain": "Pattern observed from incident.",
            "reasoning_type": "inductive_enumeration",
        })))
        assert "lesson_id" in r, f"add_lesson failed: {r}"
        return r["lesson_id"]

    def test_match_type_absent_from_similar_lessons(self):
        server = fresh_server()
        obs_id = _add_obs(server, "An incident observation for lesson.",
                          url="https://example.com/incident-1")
        lesson_id = self._create_lesson(server, "Pattern: do X not Y.", obs_id)
        # Now stub so next lesson creation gets a match against the first lesson
        self._stub_semantic_lesson_match(server, lesson_id, "Pattern: do X not Y.")

        obs_id2 = _add_obs(server, "Another incident.",
                           url="https://example.com/incident-2")
        r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
            "claim": "Pattern: do X not Y consistently.",
            "scaffolding_nudge": "Watch for this pattern.",
            "incident_ids": obs_id2,
            "logical_chain": "Pattern confirmed from second incident.",
            "reasoning_type": "inductive_enumeration",
        })))
        for entry in r.get("similar_existing_lessons", []):
            assert "match_type" not in entry, (
                f"match_type should be stripped from similar_existing_lessons: {entry}"
            )

    def test_similarity_absent_from_similar_lessons(self):
        server = fresh_server()
        obs_id = _add_obs(server, "Incident for lesson sim check.",
                          url="https://example.com/incident-3")
        lesson_id = self._create_lesson(server, "Pattern: avoid Z.", obs_id)
        self._stub_semantic_lesson_match(server, lesson_id, "Pattern: avoid Z.")

        obs_id2 = _add_obs(server, "Another incident for Z.",
                           url="https://example.com/incident-4")
        r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
            "claim": "Pattern: avoid Z in all cases.",
            "scaffolding_nudge": "Avoid Z.",
            "incident_ids": obs_id2,
            "logical_chain": "Confirmed avoiding Z.",
            "reasoning_type": "inductive_enumeration",
        })))
        for entry in r.get("similar_existing_lessons", []):
            assert "similarity" not in entry, (
                f"similarity should be stripped from similar_existing_lessons: {entry}"
            )


# ---------------------------------------------------------------------------
# 6. engram_query debug mode — match_type + similarity PRESENT in results
#    (eval-harness contract: these are NOT stripped in debug mode)
# ---------------------------------------------------------------------------

class TestQueryDebugModeStripping:
    """Verifies the eval-harness contract for engram_query(return_debug=True).

    match_type and similarity are diagnostic signals that ARE meaningful to
    SxS comparison / retrieval diagnostics / eval-harness consumers.
    They must be PRESENT in debug-mode result nodes and debug.per_result.

    The prior implementation stripped them globally (BLOCKER 1 regression).
    These tests assert the corrected behavior: scoped strip only in
    similar_existing / similar_existing_lessons, not in query results.
    """

    def test_match_type_present_in_debug_results(self):
        """match_type must survive in engram_query(return_debug=True) result nodes."""
        server = fresh_server()
        _add_obs(server, "Unique test claim for query debug match_type check.",
                 url="https://example.com/query-strip-test")
        r = json.loads(server.engram_query(payload_json=json.dumps({
            "query": "unique test claim query debug",
            "return_debug": True,
        })))
        assert "error" not in r, r
        results = r.get("results", [])
        assert results, "Expected at least one result in debug mode"
        for node in results:
            assert "match_type" in node, (
                f"match_type must be PRESENT in engram_query debug results: {node.get('id')}"
            )

    def test_similarity_present_in_debug_results(self):
        """similarity must survive in engram_query(return_debug=True) result nodes
        (when semantic search is available — with ENGRAM_NO_EMBEDDINGS=1 this
        field is absent for semantic-only hits; keyword hits also lack it unless
        the embedder ran. We assert it's not INCORRECTLY stripped by checking
        that the debug key is present and match_type is readable)."""
        server = fresh_server()
        _add_obs(server, "Query debug similarity field presence test.",
                 url="https://example.com/query-sim-test")
        r = json.loads(server.engram_query(payload_json=json.dumps({
            "query": "query debug similarity presence",
            "return_debug": True,
        })))
        # The debug key itself must be present (signals the debug path ran).
        assert "debug" in r, "debug key missing from return_debug=True response"
        per_result = r["debug"].get("per_result", [])
        # per_result entries include similarity (may be None for keyword-only
        # hits when semantic unavailable, but the KEY must be present).
        for entry in per_result:
            assert "similarity" in entry, (
                f"similarity key missing from debug.per_result entry: {entry}"
            )

    def test_confidence_history_absent_from_debug_results(self):
        """confidence_history is still stripped globally — this is correct behavior."""
        server = fresh_server()
        _add_obs(server, "Confidence history query stripping test.",
                 url="https://example.com/query-conf-test")
        r = json.loads(server.engram_query(payload_json=json.dumps({
            "query": "confidence history query stripping",
            "return_debug": True,
        })))
        for node in r.get("results", []):
            assert "confidence_history" not in node, (
                "confidence_history should be stripped from engram_query debug results"
            )

    def test_debug_per_result_match_type_present(self):
        """debug.per_result entries must include match_type (keyword or semantic)."""
        server = fresh_server()
        _add_obs(server, "Per-result debug match_type presence test.",
                 url="https://example.com/query-per-result-test")
        r = json.loads(server.engram_query(payload_json=json.dumps({
            "query": "per result debug match type presence",
            "return_debug": True,
        })))
        assert "debug" in r, "debug key missing"
        per_result = r["debug"].get("per_result", [])
        assert per_result, "Expected per_result entries in debug output"
        for entry in per_result:
            assert "match_type" in entry, (
                f"match_type missing from debug.per_result entry: {entry}"
            )


# ---------------------------------------------------------------------------
# 9. DB integrity — stripped fields still present in the database
# ---------------------------------------------------------------------------

class TestDbIntegrityAfterStripping:
    def test_confidence_history_in_db(self):
        """confidence_history stripped from MCP return but still in DB."""
        server = fresh_server()
        obs_id = _add_obs(server, "DB integrity test: confidence history.")
        conn = sqlite3.connect(_db_path(server))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT confidence_history FROM nodes WHERE id = ?", (obs_id,)
            ).fetchone()
            assert row is not None, f"Node {obs_id} not found in DB"
            raw = row["confidence_history"]
            assert raw is not None, "confidence_history should be in DB"
            parsed = json.loads(raw)
            assert isinstance(parsed, list) and len(parsed) > 0, (
                "confidence_history in DB should be a non-empty list"
            )
        finally:
            conn.close()

    def test_embedding_not_in_db_when_disabled(self):
        """With ENGRAM_NO_EMBEDDINGS=1, embedding is null in DB (expected)."""
        server = fresh_server()
        obs_id = _add_obs(server, "Embedding DB null check.")
        conn = sqlite3.connect(_db_path(server))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT embedding FROM nodes WHERE id = ?", (obs_id,)
            ).fetchone()
            assert row is not None
            # With ENGRAM_NO_EMBEDDINGS=1, embedding is null — that's the
            # expected state in test env. The strip helper doesn't need to
            # remove it from the DB, just from the response.
            # We verify the column EXISTS (not that it's populated).
        finally:
            conn.close()

    def test_confidence_history_in_db_after_inspect(self):
        """Calling engram_inspect (which strips) should not mutate the DB."""
        server = fresh_server()
        obs_id = _add_obs(server, "DB immutability after inspect strip test.")
        # Call inspect to trigger strip at MCP boundary
        r = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": obs_id, "view": "deep"})))
        assert "confidence_history" not in r["node"]
        # Verify DB is untouched
        conn = sqlite3.connect(_db_path(server))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT confidence_history FROM nodes WHERE id = ?", (obs_id,)
            ).fetchone()
            raw = row["confidence_history"]
            assert raw is not None
            parsed = json.loads(raw)
            assert isinstance(parsed, list) and len(parsed) > 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 10. inspect_raw.py CLI
# ---------------------------------------------------------------------------

class TestInspectRawCli:
    def test_list_mode_shows_fields(self):
        """Running without --field lists available stripped fields."""
        server = fresh_server()
        obs_id = _add_obs(server, "CLI list mode test.")
        env = {**os.environ, "ENGRAM_HOME": TEST_DIR}
        result = subprocess.run(
            [sys.executable, INSPECT_RAW, obs_id],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, f"inspect_raw.py failed: {result.stderr}"
        out = result.stdout
        assert "confidence_history" in out
        assert "embedding" in out
        assert "parsed_metadata" in out

    def test_confidence_history_field(self):
        """--field confidence_history returns a non-empty list."""
        server = fresh_server()
        obs_id = _add_obs(server, "CLI confidence_history field test.")
        env = {**os.environ, "ENGRAM_HOME": TEST_DIR}
        result = subprocess.run(
            [sys.executable, INSPECT_RAW, obs_id, "--field", "confidence_history"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, f"inspect_raw.py failed: {result.stderr}"
        assert "confidence_history" in result.stdout
        assert len(result.stdout.strip()) > 0

    def test_confidence_history_json_output(self):
        """--field confidence_history --json produces valid JSON list."""
        server = fresh_server()
        obs_id = _add_obs(server, "CLI JSON output test.")
        env = {**os.environ, "ENGRAM_HOME": TEST_DIR}
        result = subprocess.run(
            [sys.executable, INSPECT_RAW, obs_id, "--field", "confidence_history", "--json"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, f"inspect_raw.py failed: {result.stderr}"
        parsed = json.loads(result.stdout)
        assert isinstance(parsed, list) and len(parsed) > 0, (
            f"Expected non-empty list, got: {parsed}"
        )
        assert "value" in parsed[0], f"Expected confidence entry with 'value': {parsed[0]}"

    def test_parsed_metadata_json_output(self):
        """--field parsed_metadata --json returns a dict with source_class."""
        server = fresh_server()
        obs_id = _add_obs(server, "CLI parsed_metadata JSON test.")
        env = {**os.environ, "ENGRAM_HOME": TEST_DIR}
        result = subprocess.run(
            [sys.executable, INSPECT_RAW, obs_id, "--field", "parsed_metadata", "--json"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, f"inspect_raw.py failed: {result.stderr}"
        parsed = json.loads(result.stdout)
        assert isinstance(parsed, dict), f"Expected dict, got: {type(parsed)}"
        assert "source_class" in parsed, (
            f"parsed_metadata should contain source_class: {parsed}"
        )

    def test_unknown_node_exits_1(self):
        """Non-existent node ID should exit with code 1."""
        fresh_server()
        env = {**os.environ, "ENGRAM_HOME": TEST_DIR}
        result = subprocess.run(
            [sys.executable, INSPECT_RAW, "ob_9999"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 1
        assert "not found" in result.stderr.lower()
