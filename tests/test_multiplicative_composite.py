"""Tests for the multiplicative-amplifier composite formula (Lei 2026-05-19 design call).

Covers:
  1. _get_min_queryable_importance returns actual min from populated graph
  2. _get_min_queryable_importance returns 1.0 on empty graph (no div-by-zero)
  3. _get_min_queryable_importance excludes nodes with importance_score = 0
  4. _get_min_queryable_importance excludes retracted (is_current = 0) nodes
  5. composite = relevance × (1 + IMP_BETA×1) when util=0 and imp=min (floor case)
  6. utility=1 → util_amp = 1 + UTIL_BETA = 1.10
  7. high-importance node → imp_amp scales correctly via normalization
  8. no branching on utility=0 — imp_amp still applied (old UTILITY_LAMBDA=0 branch gone)
  9. constant values: UTIL_BETA == 0.10, IMP_BETA == 0.005
  10. UTILITY_LAMBDA is no longer defined in server
  11. return_debug=True exposes util_amp, imp_amp, imp_norm_factor per result
  12. with amps at unity, composite == relevance (sanity check)
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_multiplicative_composite_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Create a clean server module instance in a temp sandbox directory."""
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


def fresh_conn(server):
    """Open and return a fresh DB connection for direct manipulation."""
    return server._get_db()


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


# ---------------------------------------------------------------------------
# Tests 1–4: _get_min_queryable_importance helper
# ---------------------------------------------------------------------------


def test_min_queryable_importance_returns_actual_min():
    """Populate a small graph with known importance scores; verify helper returns min."""
    server = fresh_server()
    conn = fresh_conn(server)
    try:
        # Insert two nodes directly with known importance_score values.
        conn.execute(
            "INSERT INTO nodes (id, type, claim, confidence, is_current, importance_score, created_at) "
            "VALUES ('ax_0001', 'axiom', 'test axiom', 1.0, 1, 2.5, '2026-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO nodes (id, type, claim, confidence, is_current, importance_score, created_at) "
            "VALUES ('ax_0002', 'axiom', 'test axiom 2', 1.0, 1, 0.7, '2026-01-01T00:00:00')"
        )
        conn.commit()
        result = server._get_min_queryable_importance(conn)
    finally:
        conn.close()

    assert abs(result - 0.7) < 1e-9, (
        f"Expected min importance 0.7, got {result}"
    )


def test_min_queryable_importance_handles_empty_graph():
    """Returns 1.0 fallback on empty graph — no division by zero."""
    server = fresh_server()
    conn = fresh_conn(server)
    try:
        result = server._get_min_queryable_importance(conn)
    finally:
        conn.close()

    assert result == 1.0, (
        f"Expected 1.0 fallback on empty graph, got {result}"
    )


def test_min_queryable_importance_excludes_zero_importance():
    """Nodes with importance_score = 0 are excluded (WHERE importance_score > 0)."""
    server = fresh_server()
    conn = fresh_conn(server)
    try:
        # Only node has importance_score = 0; should trigger the 1.0 fallback.
        conn.execute(
            "INSERT INTO nodes (id, type, claim, confidence, is_current, importance_score, created_at) "
            "VALUES ('ax_0001', 'axiom', 'zero importance', 1.0, 1, 0.0, '2026-01-01T00:00:00')"
        )
        conn.commit()
        result = server._get_min_queryable_importance(conn)
    finally:
        conn.close()

    assert result == 1.0, (
        f"Expected 1.0 when only node has importance_score=0, got {result}"
    )


def test_min_queryable_importance_excludes_retracted():
    """Retracted nodes (is_current = 0) are excluded from the min computation."""
    server = fresh_server()
    conn = fresh_conn(server)
    try:
        # Retracted node with very low importance — should be ignored.
        conn.execute(
            "INSERT INTO nodes (id, type, claim, confidence, is_current, importance_score, created_at) "
            "VALUES ('ax_0001', 'axiom', 'retracted', 1.0, 0, 0.001, '2026-01-01T00:00:00')"
        )
        # Current node with higher importance — should be the min.
        conn.execute(
            "INSERT INTO nodes (id, type, claim, confidence, is_current, importance_score, created_at) "
            "VALUES ('ax_0002', 'axiom', 'current', 1.0, 1, 1.5, '2026-01-01T00:00:00')"
        )
        conn.commit()
        result = server._get_min_queryable_importance(conn)
    finally:
        conn.close()

    assert abs(result - 1.5) < 1e-9, (
        f"Expected min=1.5 (ignoring retracted 0.001), got {result}"
    )


# ---------------------------------------------------------------------------
# Tests 5–8: composite formula correctness
# ---------------------------------------------------------------------------


def test_composite_zero_utility_zero_importance_floor_equals_relevance_times_floor_amps():
    """Base case: when utility=0 and imp=min_importance, composite = rel × (1+IMP_BETA×1).

    With utility=0: util_amp = 1.0 + UTIL_BETA*0 = 1.0
    With imp=min_queryable_imp: imp*imp_norm_factor = 1.0 → imp_amp = 1.0 + IMP_BETA*1.0
    composite = rel × 1.0 × (1 + IMP_BETA)
    """
    server = fresh_server()
    # Use engram_add_observation to create a node, then query it.
    nid = _add_obs(server, "unique xylophone zero utility composite floor test")
    conn = fresh_conn(server)
    try:
        # Verify utility_score is 0 (fresh node).
        row = conn.execute(
            "SELECT utility_score, importance_score FROM nodes WHERE id = ?", (nid,)
        ).fetchone()
        assert (row["utility_score"] or 0.0) == 0.0, "Expected 0.0 utility for fresh node"
        imp = row["importance_score"] or 0.0
        min_imp = server._get_min_queryable_importance(conn)
    finally:
        conn.close()

    result = server._search_nodes(
        server._get_db(), "xylophone zero utility composite floor", use_semantic=False, top_k=5,
        return_debug=True,
    )
    conn2 = server._get_db()
    conn2.close()

    assert len(result["results"]) >= 1, "Expected at least one result"
    debug_entry = None
    for entry in result["debug"]["per_result"]:
        if entry["id"] == nid:
            debug_entry = entry
            break

    assert debug_entry is not None, f"Node {nid} not in debug per_result"
    # util_amp should be 1.0 (utility=0)
    assert abs(debug_entry["util_amp"] - 1.0) < 1e-9, (
        f"Expected util_amp=1.0 with utility=0, got {debug_entry['util_amp']}"
    )
    expected_imp_amp = 1.0 + server.IMP_BETA * (imp / min_imp)
    assert abs(debug_entry["imp_amp"] - expected_imp_amp) < 1e-9, (
        f"Expected imp_amp={expected_imp_amp}, got {debug_entry['imp_amp']}"
    )
    # composite = relevance * 1.0 * imp_amp
    expected_composite = debug_entry["relevance_normalized"] * 1.0 * expected_imp_amp
    assert abs(debug_entry["composite_score"] - expected_composite) < 1e-9, (
        f"Expected composite={expected_composite}, got {debug_entry['composite_score']}"
    )


def test_composite_full_utility_maximizes_util_amp():
    """utility=1 → util_amp = 1 + UTIL_BETA = 1.5."""
    server = fresh_server()
    nid = _add_obs(server, "unique kazoo instrument full utility test xylophone")
    conn = fresh_conn(server)
    try:
        conn.execute(
            "UPDATE nodes SET utility_score = 1.0 WHERE id = ?", (nid,)
        )
        conn.commit()
    finally:
        conn.close()

    result = server._search_nodes(
        server._get_db(), "kazoo instrument full utility", use_semantic=False, top_k=5,
        return_debug=True,
    )

    debug_entry = None
    for entry in result["debug"]["per_result"]:
        if entry["id"] == nid:
            debug_entry = entry
            break

    assert debug_entry is not None, f"Node {nid} not in debug per_result"
    expected_util_amp = 1.0 + server.UTIL_BETA  # = 1.10 with UTIL_BETA=0.10
    assert abs(debug_entry["util_amp"] - expected_util_amp) < 1e-9, (
        f"Expected util_amp={expected_util_amp} with utility=1.0, got {debug_entry['util_amp']}"
    )


def test_composite_cornerstone_amp():
    """High-importance node: imp_amp scales correctly relative to a low-importance node.

    Set one node to high importance. The imp_amp should be larger than the
    imp_amp for a node at min_importance.
    """
    server = fresh_server()
    low_nid = _add_obs(server, "unique marimba low importance amp test base")
    high_nid = _add_obs(server, "unique marimba high importance amp test cornerstone")
    conn = fresh_conn(server)
    try:
        # Set low-importance node to exactly 1.0, high to 10.0.
        conn.execute("UPDATE nodes SET importance_score = 1.0 WHERE id = ?", (low_nid,))
        conn.execute("UPDATE nodes SET importance_score = 10.0 WHERE id = ?", (high_nid,))
        conn.commit()
        min_imp = server._get_min_queryable_importance(conn)
        imp_norm = 1.0 / min_imp
    finally:
        conn.close()

    result = server._search_nodes(
        server._get_db(), "marimba importance amp", use_semantic=False, top_k=10,
        return_debug=True,
    )

    entries = {e["id"]: e for e in result["debug"]["per_result"]}
    assert low_nid in entries, f"low node {low_nid} not in results"
    assert high_nid in entries, f"high node {high_nid} not in results"

    low_imp_amp = entries[low_nid]["imp_amp"]
    high_imp_amp = entries[high_nid]["imp_amp"]

    expected_low = 1.0 + server.IMP_BETA * (1.0 * imp_norm)
    expected_high = 1.0 + server.IMP_BETA * (10.0 * imp_norm)

    assert abs(low_imp_amp - expected_low) < 1e-9, (
        f"Expected low imp_amp={expected_low}, got {low_imp_amp}"
    )
    assert abs(high_imp_amp - expected_high) < 1e-9, (
        f"Expected high imp_amp={expected_high}, got {high_imp_amp}"
    )
    assert high_imp_amp > low_imp_amp, (
        "High-importance node must have higher imp_amp than low-importance node"
    )


def test_composite_no_branching_on_utility_disabled():
    """With all utility=0, composite still applies imp_amp (no old UTILITY_LAMBDA=0 branch).

    Previously, when UTILITY_LAMBDA==0, the composite was just relevance with no
    amplification. Under the new formula, imp_amp is always applied — so composite
    > relevance even when utility=0.
    """
    server = fresh_server()
    nid = _add_obs(server, "unique oboe instrument no branch utility disabled test")
    conn = fresh_conn(server)
    try:
        # Verify utility is 0.
        row = conn.execute(
            "SELECT utility_score FROM nodes WHERE id = ?", (nid,)
        ).fetchone()
        assert (row["utility_score"] or 0.0) == 0.0
        # Set a non-trivial importance so imp_amp > 1.
        conn.execute("UPDATE nodes SET importance_score = 2.0 WHERE id = ?", (nid,))
        conn.commit()
        min_imp = server._get_min_queryable_importance(conn)
    finally:
        conn.close()

    result = server._search_nodes(
        server._get_db(), "oboe instrument no branch", use_semantic=False, top_k=5,
        return_debug=True,
    )

    debug_entry = None
    for entry in result["debug"]["per_result"]:
        if entry["id"] == nid:
            debug_entry = entry
            break

    assert debug_entry is not None, f"Node {nid} not in debug per_result"
    # imp_amp should be > 1.0 (importance > min)
    assert debug_entry["imp_amp"] > 1.0, (
        f"imp_amp should be > 1.0 even with utility=0; got {debug_entry['imp_amp']}"
    )
    # composite > relevance (because imp_amp > 1, util_amp == 1)
    assert debug_entry["composite_score"] > debug_entry["relevance_normalized"], (
        f"composite={debug_entry['composite_score']} should exceed "
        f"relevance={debug_entry['relevance_normalized']} when imp_amp>1"
    )


# ---------------------------------------------------------------------------
# Tests 9–10: constant values + UTILITY_LAMBDA removal
# ---------------------------------------------------------------------------


def test_composite_constants_have_expected_values():
    """UTIL_BETA == 0.10, IMP_BETA == 0.005 (calibrated 2026-05-19 PM harness sweep)."""
    server = fresh_server()
    assert server.UTIL_BETA == 0.10, (
        f"Expected UTIL_BETA=0.10, got {server.UTIL_BETA}"
    )
    assert server.IMP_BETA == 0.005, (
        f"Expected IMP_BETA=0.005, got {server.IMP_BETA}"
    )


def test_composite_removes_old_utility_lambda_constant():
    """UTILITY_LAMBDA is no longer defined in server.py."""
    server = fresh_server()
    assert not hasattr(server, "UTILITY_LAMBDA"), (
        "UTILITY_LAMBDA should be removed; found it still defined in server"
    )


# ---------------------------------------------------------------------------
# Test 11: debug payload includes amp decomposition fields
# ---------------------------------------------------------------------------


def test_engram_query_includes_amp_debug_fields_when_return_debug():
    """return_debug=True: per-result entries include util_amp, imp_amp, imp_norm_factor."""
    server = fresh_server()
    nid = _add_obs(server, "unique trombone jazz debug amp fields test")

    result = server._search_nodes(
        server._get_db(), "trombone jazz", use_semantic=False, top_k=5,
        return_debug=True,
    )

    assert len(result["debug"]["per_result"]) >= 1, "Expected at least one result for debug check"
    for entry in result["debug"]["per_result"]:
        assert "util_amp" in entry, (
            f"Missing 'util_amp' in debug per_result entry: {list(entry.keys())}"
        )
        assert "imp_amp" in entry, (
            f"Missing 'imp_amp' in debug per_result entry: {list(entry.keys())}"
        )
        assert "imp_norm_factor" in entry, (
            f"Missing 'imp_norm_factor' in debug per_result entry: {list(entry.keys())}"
        )
        # All should be positive floats.
        assert debug_float_positive(entry["util_amp"]), (
            f"util_amp should be a positive float, got {entry['util_amp']!r}"
        )
        assert debug_float_positive(entry["imp_amp"]), (
            f"imp_amp should be a positive float, got {entry['imp_amp']!r}"
        )
        assert debug_float_positive(entry["imp_norm_factor"]), (
            f"imp_norm_factor should be a positive float, got {entry['imp_norm_factor']!r}"
        )


def debug_float_positive(val) -> bool:
    """Return True if val is a float/int > 0."""
    try:
        return float(val) > 0
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Test 12: composite == relevance when amps are unity
# ---------------------------------------------------------------------------


def test_composite_relevance_dominates_when_amps_are_unity():
    """Sanity: when imp is exactly min_importance and utility=0, composite proportional to rel.

    util_amp = 1.0 (utility=0)
    imp_amp = 1 + IMP_BETA (imp = min, so normalized = 1)
    composite = relevance * 1.0 * (1 + IMP_BETA)

    Two nodes with different relevance — the higher-relevance node should rank above
    the lower-relevance node (composite is a monotone function of relevance when amps
    are equal).
    """
    server = fresh_server()
    # Two obs: strong FTS match vs weaker match.
    # We'll set their importance equal to ensure amps are equal.
    nid_strong = _add_obs(
        server,
        "unique vibraphone vibraphone vibraphone ranking dominance test",
        quoted_text="vibraphone vibraphone",
    )
    nid_weak = _add_obs(
        server,
        "unique vibraphone ranking dominance secondary term",
        quoted_text="vibraphone test",
    )
    conn = fresh_conn(server)
    try:
        # Make importance equal for both so imp_amp is identical.
        conn.execute("UPDATE nodes SET importance_score = 1.0 WHERE id = ?", (nid_strong,))
        conn.execute("UPDATE nodes SET importance_score = 1.0 WHERE id = ?", (nid_weak,))
        conn.commit()
    finally:
        conn.close()

    result = server._search_nodes(
        server._get_db(), "vibraphone ranking dominance", use_semantic=False, top_k=10,
        return_debug=True,
    )

    entries = {e["id"]: e for e in result["debug"]["per_result"]}
    if nid_strong not in entries or nid_weak not in entries:
        pytest.skip("Both nodes must be in results for this sanity test")

    strong_entry = entries[nid_strong]
    weak_entry = entries[nid_weak]

    # imp_amp should be equal for both (same importance).
    assert abs(strong_entry["imp_amp"] - weak_entry["imp_amp"]) < 1e-9, (
        f"imp_amp should be equal when importance is equal: "
        f"{strong_entry['imp_amp']} vs {weak_entry['imp_amp']}"
    )
    # composite ordering should follow relevance ordering.
    if strong_entry["relevance_normalized"] > weak_entry["relevance_normalized"]:
        assert strong_entry["composite_score"] > weak_entry["composite_score"], (
            "Higher relevance node should have higher composite when amps are equal"
        )


def test_min_queryable_importance_respects_tier2_threshold_when_positive():
    """When tier2_threshold > 0, the helper returns the min OF nodes above the threshold.

    Round-2 fix (PR #218 fairy review): the original implementation queried
    the GLOBAL min, ignoring the tier-2 filter that `_search_nodes` actually
    applies. On a graph where tier2_threshold rises (post-cap), cornerstone
    amp would have run away. Helper now bounds the query by the tier-2
    threshold so the normalization anchor matches the queryable subset.
    """
    server = fresh_server()
    conn = server._get_db()

    # Seed graph with nodes spanning a wide importance range
    conn.executemany(
        "INSERT INTO nodes (id, type, claim, status, is_current, "
        "created_at, importance_score) "
        "VALUES (?, ?, ?, 'active', 1, ?, ?)",
        [
            ("ob_0001", "observation_factual", "low",     "2026-01-01T00:00:00", 0.1),
            ("ob_0002", "observation_factual", "low-mid", "2026-01-01T00:00:00", 0.5),
            ("ob_0003", "observation_factual", "mid",     "2026-01-01T00:00:00", 1.0),
            ("ob_0004", "observation_factual", "high",    "2026-01-01T00:00:00", 2.0),
            ("ob_0005", "observation_factual", "top",     "2026-01-01T00:00:00", 5.0),
        ],
    )
    conn.commit()

    # Force a positive tier2_threshold by stubbing the helper for this test.
    # We bypass _get_tier_threshold and verify the SQL filter uses our value.
    original = server.core._get_tier_threshold
    server.core._get_tier_threshold = lambda c, tier: 0.8 if tier == 2 else 0.0
    try:
        result = server._get_min_queryable_importance(conn)
    finally:
        server.core._get_tier_threshold = original

    # With threshold=0.8, only ob_0003/0004/0005 qualify (imp >= 0.8).
    # Min among those = 1.0.
    assert abs(result - 1.0) < 1e-9, (
        f"Expected min above tier-2 threshold (0.8) = 1.0, got {result}"
    )
