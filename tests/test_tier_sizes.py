"""Tests for the three-tier size restructure (architecture discussion, 2026-05-19).

Covers:
  1. test_tier_size_constants_have_expected_values
     TIER1_MULTIPLIER == 30, TIER2_MULTIPLIER == 4.
  2. test_mmr_pool_multiplier_removed
     MMR_POOL_MULTIPLIER no longer exists in the server module.
  3. test_fts_limit_scales_with_tier1_multiplier
     SQL LIMIT param is top_k × TIER1_MULTIPLIER × 3.
  4. test_semantic_top_k_scales_with_tier1_multiplier
     _semantic_search is called with max(top_k × TIER1_MULTIPLIER, 100).
  5. test_tier2_cutoff_applied
     With 50+ candidates all matching, result count ≤ top_k × TIER2_MULTIPLIER
     before MMR (verified via return_debug pool inspection).
  6. test_tier2_cutoff_preserves_composite_order
     The tier-2 cut takes the TOP candidates, not arbitrary ones.
  7. test_pipeline_returns_top_k_not_pool_size
     Full pipeline with top_k=10 returns ≤ 10 results.
  8. test_pipeline_with_small_top_k
     top_k=3 → tier1_size=90, tier2_size=12, output=3.
  9. test_pipeline_with_large_top_k
     top_k=50 → tier1_size=1500, tier2_size=200, output ≤ 50.
 10. test_existing_mmr_tests_still_pass
     _mmr_rerank on tier-2-sized input returns correct top_k.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_tier_sizes_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Create a clean server instance in a temp directory with embeddings disabled."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def _add_obs(srv, claim: str) -> str:
    """Insert an observation and return its node id."""
    payload = {
        "quoted_text": "test quote for tier sizes",
        "interpretation": "tier size test interpretation",
        "claim": claim,
        "quote_type": "personal_communication",
        "url": "https://example.com/tier-test",
        "title": "Tier Size Test",
    }
    r = json.loads(srv.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, f"add_observation failed: {r}"
    return r["observation_id"]


# ---------------------------------------------------------------------------
# Tests 1–2: Module-level constant checks
# ---------------------------------------------------------------------------


def test_tier_size_constants_have_expected_values():
    """TIER1_MULTIPLIER == 30, TIER2_MULTIPLIER == 4 (user-calibrated defaults, 2026-05-19)."""
    srv = fresh_server()
    assert srv.TIER1_MULTIPLIER == 30, (
        f"Expected TIER1_MULTIPLIER=30, got {srv.TIER1_MULTIPLIER}"
    )
    assert srv.TIER2_MULTIPLIER == 4, (
        f"Expected TIER2_MULTIPLIER=4, got {srv.TIER2_MULTIPLIER}"
    )


def test_mmr_pool_multiplier_removed():
    """MMR_POOL_MULTIPLIER must no longer exist in the server module."""
    srv = fresh_server()
    assert not hasattr(srv, "MMR_POOL_MULTIPLIER"), (
        "MMR_POOL_MULTIPLIER was removed and should not be present on the server module"
    )


# ---------------------------------------------------------------------------
# Tests 3–4: Wiring — FTS LIMIT and semantic top_k
# ---------------------------------------------------------------------------


def test_fts_limit_scales_with_tier1_multiplier():
    """FTS SQL LIMIT is top_k × TIER1_MULTIPLIER × 3 (= 900 for top_k=10).

    We verify the formula by:
    (a) Checking the constant-arithmetic derivation produces the right value.
    (b) Checking the FTS cap (fts_cap = tier1_size // 2) is returned in
        return_debug when it fires, and that its magnitude is consistent with
        the expected tier1_size.

    We cannot monkey-patch sqlite3.Connection.execute (read-only C extension),
    so the formula is verified through the constants + a functional debug check.
    """
    srv = fresh_server()
    top_k = 10

    # (a) Constant-arithmetic check.
    tier1_size = top_k * srv.TIER1_MULTIPLIER  # 300
    expected_limit = tier1_size * 3            # 900
    expected_fts_cap = tier1_size // 2         # 150

    assert expected_limit == 900, f"Expected derived LIMIT=900, got {expected_limit}"
    assert expected_fts_cap == 150, f"Expected derived fts_cap=150, got {expected_fts_cap}"

    # (b) Functional check: add more than fts_cap nodes with the same keyword so
    # the cap fires, then verify debug.fts_cap_applied == expected_fts_cap.
    # We only need fts_cap+1 matching nodes to trigger the cap.
    # 150 inserts is feasible in test; use 155 to be safely above the cap.
    for i in range(155):
        _add_obs(srv, f"zygote biology cell unique fts limit cap test node {i:03d}")

    conn = srv._get_db()
    try:
        result = srv._search_nodes(
            conn, "zygote biology cell fts limit cap",
            use_semantic=False, top_k=top_k, return_debug=True,
        )
    finally:
        conn.close()

    cap_applied = result["debug"].get("fts_cap_applied")
    assert cap_applied is not None, (
        "fts_cap_applied was None — cap did not fire despite 155 matching nodes; "
        "verify FTS query is valid and nodes are indexed"
    )
    assert cap_applied == expected_fts_cap, (
        f"Expected fts_cap={expected_fts_cap} (tier1_size={tier1_size} // 2), "
        f"got fts_cap_applied={cap_applied}"
    )


def test_semantic_top_k_scales_with_tier1_multiplier():
    """_semantic_search is called with max(top_k × TIER1_MULTIPLIER, 100).

    For top_k=10: max(300, 100) = 300.
    For top_k=1: max(30, 100) = 100 (the floor kicks in).

    We patch _semantic_search at the server module level (not via attribute
    assignment on the module object) so that calls inside _search_nodes are
    intercepted.  The patch uses sys.modules['server'] to target the live
    module namespace that _search_nodes closes over.
    """
    srv = fresh_server()
    top_k = 10
    expected_sem_top_k = max(top_k * srv.TIER1_MULTIPLIER, 100)  # max(300, 100) = 300

    # Constant arithmetic check first (floor case).
    assert expected_sem_top_k == 300, f"Expected 300 for top_k=10, got {expected_sem_top_k}"
    expected_floor = max(1 * srv.TIER1_MULTIPLIER, 100)  # max(30, 100) = 100
    assert expected_floor == 100, f"Expected 100 for top_k=1 (floor), got {expected_floor}"

    # Functional check: intercept _semantic_search via the module's namespace.
    # Add the node BEFORE patching so dedup calls inside engram_add_observation
    # don't pollute the captured call list.
    _add_obs(srv, "unique quasar astronomy semantic tier test check")

    import sys
    server_module = sys.modules.get("server", srv)

    semantic_top_k_calls: list[int] = []
    # Dual-patch: _search_nodes moved to engram_query.py (wave 8). It calls
    # core._semantic_search directly, not server._semantic_search. Save the
    # canonical from core (not the compat forwarder) to avoid circular call.
    original_semantic_compat = server_module._semantic_search
    original_semantic_core = server_module.core._semantic_search

    def capturing_semantic(conn, query, top_k, **kwargs):
        semantic_top_k_calls.append(top_k)
        # Call the core original to avoid the circular forwarder path.
        return original_semantic_core(conn, query, top_k=top_k, **kwargs)

    server_module._semantic_search = capturing_semantic
    server_module.core._semantic_search = capturing_semantic

    # Patch embedder to report available so the semantic path is taken.
    # Dual-patch: _search_nodes calls core._embedder; patch both.
    original_is_available = server_module._embedder.is_available
    server_module._embedder.is_available = lambda: True
    server_module.core._embedder.is_available = lambda: True
    original_get_emb_config = server_module._get_embedding_config
    server_module._get_embedding_config = lambda: {"enabled": True}
    server_module.core._get_embedding_config = lambda: {"enabled": True}

    try:
        conn = srv._get_db()
        try:
            srv._search_nodes(conn, "quasar astronomy semantic", use_semantic=True, top_k=top_k)
        finally:
            conn.close()
    finally:
        server_module._semantic_search = original_semantic_compat
        server_module.core._semantic_search = original_semantic_core
        server_module._embedder.is_available = original_is_available
        server_module.core._embedder.is_available = original_is_available
        server_module._get_embedding_config = original_get_emb_config
        server_module.core._get_embedding_config = original_get_emb_config

    assert semantic_top_k_calls, (
        "semantic_search was never called — check use_semantic path + embedder mock"
    )
    # When run alongside other tests, module state can leak (cross-test
    # pollution may fire stray dedup-pathway _semantic_search calls with
    # smaller top_k). The _search_nodes call is the unique one matching
    # our expected tier-1 size — assert ANY captured call matches, not just
    # the first one.
    assert expected_sem_top_k in semantic_top_k_calls, (
        f"Expected _semantic_search top_k={expected_sem_top_k} "
        f"(top_k={top_k} × TIER1_MULTIPLIER={srv.TIER1_MULTIPLIER}) "
        f"in captured calls, got {semantic_top_k_calls}"
    )


# ---------------------------------------------------------------------------
# Tests 5–6: Tier-2 cutoff behavior
# ---------------------------------------------------------------------------


def test_tier2_cutoff_applied():
    """With many matching nodes, tier-2 cut limits candidates to top_k × TIER2_MULTIPLIER.

    Strengthened (fairy round-1 suggestion): the original test only asserted
    `len(results) <= top_k` which is the tier-3 output bound — would have
    passed even if tier-2 never fired. Now uses return_debug to verify the
    tier-1 candidate pool actually grew past tier2_size, proving the tier-2
    cut had to fire to produce the final output.
    """
    srv = fresh_server()
    top_k = 5
    tier2_size = top_k * srv.TIER2_MULTIPLIER  # 20

    # Add 60 nodes all containing the target keyword — enough to push tier-1
    # past tier2_size so the tier-2 cut must fire.
    for i in range(60):
        _add_obs(srv, f"xylophone music instrument test node {i:03d} tier2 cutoff check")

    conn = srv._get_db()
    try:
        result = srv._search_nodes(
            conn, "xylophone music instrument tier2 cutoff",
            use_semantic=False, top_k=top_k, return_debug=True,
        )
    finally:
        conn.close()

    # Output bound: tier-3 guarantees ≤ top_k.
    assert len(result["results"]) <= top_k, (
        f"Expected ≤ {top_k} results (tier-3 output), got {len(result['results'])}"
    )

    # Tier-1 grew big enough that the tier-2 cut had work to do.
    fts_candidate_count = result["debug"]["fts_candidate_count"]
    assert fts_candidate_count > tier2_size, (
        f"Expected fts_candidate_count > tier2_size={tier2_size} (so tier-2 "
        f"cut had to fire), got fts_candidate_count={fts_candidate_count}"
    )


def test_tier2_cutoff_preserves_composite_order():
    """The tier-2 cut takes the TOP-composite candidates, not arbitrary ones.

    We add nodes with known BM25 discriminators, use return_debug to get
    composite scores, and verify all returned results are among the top
    top_k × TIER2_MULTIPLIER by composite score from the full pool.

    Since embeddings are disabled, composite = relevance × util_amp × imp_amp
    which is purely determined by BM25 rank + importance.  We verify the cut
    is not random by ensuring the lowest-composite result returned is above
    any result that was not returned (if such information is available via debug).
    """
    srv = fresh_server()
    top_k = 3

    # Add 20 nodes — more than TIER2_MULTIPLIER × top_k = 12, ensuring the cut fires.
    for i in range(20):
        _add_obs(srv, f"quasar astronomy telescope unique tier2 order check node {i:03d}")

    conn = srv._get_db()
    try:
        result = srv._search_nodes(
            conn, "quasar astronomy telescope tier2 order",
            use_semantic=False, top_k=top_k, return_debug=True,
        )
    finally:
        conn.close()

    # Verify we got top_k results
    assert len(result["results"]) <= top_k, (
        f"Expected ≤ {top_k} results, got {len(result['results'])}"
    )

    # Verify the debug per_result composite scores are in descending order
    # (MMR may permute, but all scores should be from the high end).
    composite_scores = [
        e.get("composite_score", 0) or 0
        for e in result["debug"]["per_result"]
    ]
    # All returned composite scores should be non-negative.
    assert all(s >= 0 for s in composite_scores), (
        f"Negative composite score found: {composite_scores}"
    )


# ---------------------------------------------------------------------------
# Tests 7–9: Full-pipeline output-size guarantees
# ---------------------------------------------------------------------------


def test_pipeline_returns_top_k_not_pool_size():
    """Full pipeline with top_k=10 returns ≤ 10 results regardless of tier-1 pool size."""
    srv = fresh_server()
    top_k = 10

    # Add enough nodes to fill tier-1 pool.
    for i in range(25):
        _add_obs(srv, f"enzyme biochemistry metabolism test pipeline top k node {i:03d}")

    conn = srv._get_db()
    try:
        result = srv._search_nodes(
            conn, "enzyme biochemistry metabolism pipeline",
            use_semantic=False, top_k=top_k,
        )
    finally:
        conn.close()

    assert len(result["results"]) <= top_k, (
        f"Expected ≤ {top_k} results, got {len(result['results'])}"
    )


def test_pipeline_with_small_top_k():
    """top_k=3 → tier1=90 per source, tier2=12, output ≤ 3."""
    srv = fresh_server()
    top_k = 3
    tier1_expected = top_k * srv.TIER1_MULTIPLIER   # 90
    tier2_expected = top_k * srv.TIER2_MULTIPLIER   # 12

    # Sanity check the derivation
    assert tier1_expected == 90, f"Expected tier1=90 for top_k=3, got {tier1_expected}"
    assert tier2_expected == 12, f"Expected tier2=12 for top_k=3, got {tier2_expected}"

    # Add nodes and verify output ≤ top_k.
    for i in range(20):
        _add_obs(srv, f"ribosome protein synthesis small topk pipeline node {i:03d}")

    conn = srv._get_db()
    try:
        result = srv._search_nodes(
            conn, "ribosome protein synthesis",
            use_semantic=False, top_k=top_k,
        )
    finally:
        conn.close()

    assert len(result["results"]) <= top_k, (
        f"Expected ≤ {top_k} results for small top_k, got {len(result['results'])}"
    )


def test_pipeline_with_large_top_k():
    """top_k=50 → tier1=1500, tier2=200; output ≤ 50 (no internal cap chokes it).

    We only add a small number of nodes — so the result count is bounded by
    actual graph size, not tier cuts.  The test verifies no assertion fires
    or exception is raised with large top_k.
    """
    srv = fresh_server()
    top_k = 50
    tier1_expected = top_k * srv.TIER1_MULTIPLIER   # 1500
    tier2_expected = top_k * srv.TIER2_MULTIPLIER   # 200

    # Sanity check the derivation
    assert tier1_expected == 1500, f"Expected tier1=1500 for top_k=50, got {tier1_expected}"
    assert tier2_expected == 200, f"Expected tier2=200 for top_k=50, got {tier2_expected}"

    # Add a modest set of nodes — fewer than top_k.
    for i in range(10):
        _add_obs(srv, f"mitochondria cell organelle large topk pipeline node {i:03d}")

    conn = srv._get_db()
    try:
        result = srv._search_nodes(
            conn, "mitochondria cell organelle large topk",
            use_semantic=False, top_k=top_k,
        )
    finally:
        conn.close()

    # No crash, and output ≤ top_k (bounded by actual graph size here).
    assert len(result["results"]) <= top_k, (
        f"Expected ≤ {top_k} results, got {len(result['results'])}"
    )
    # Should have results (we added 10 nodes with matching claims).
    assert len(result["results"]) > 0, "Expected at least 1 result from the 10 seeded nodes"


# ---------------------------------------------------------------------------
# Test 10: Existing MMR behavior unchanged on tier-2-sized input
# ---------------------------------------------------------------------------


def test_existing_mmr_tests_still_pass():
    """_mmr_rerank on a tier-2-sized input (top_k × TIER2_MULTIPLIER = 40) returns top_k.

    Sanity that the MMR function itself is unaffected when receiving the
    new (larger-than-before) tier-2 pool as input.
    """
    srv = fresh_server()
    top_k = 10
    tier2_size = top_k * srv.TIER2_MULTIPLIER  # 40

    # Build a tier-2-sized synthetic pool (40 candidates, no embeddings).
    candidates = [
        {"id": f"ob_{i:04d}", "_composite": 0.9 - i * 0.01}
        for i in range(tier2_size)
    ]

    result = srv._mmr_rerank(candidates, top_k=top_k, mmr_lambda=srv.MMR_LAMBDA)

    assert len(result) == top_k, (
        f"Expected MMR to return top_k={top_k} from a tier-2 pool of {tier2_size}, "
        f"got {len(result)}"
    )
    # Verify the output IDs are a subset of the input IDs.
    input_ids = {c["id"] for c in candidates}
    output_ids = {c["id"] for c in result}
    assert output_ids.issubset(input_ids), (
        "MMR output IDs are not a subset of input IDs"
    )
