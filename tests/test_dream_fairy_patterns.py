"""Tests for dream-fairy scan calibration: importance_base gating + inflation-gap sort.

Covers:
  1. stale_load_bearing filters feeling_report nodes.
  2. stale_load_bearing sorts by inflation-gap (old-recall > recent-recall).
  3. stale_load_bearing gates on importance_base floor (0.7); nodes below are excluded.
  4. cornerstone_candidate filters feeling_report nodes.
  5. cornerstone_candidate gates on importance_base floor (0.7); low-base nodes excluded
     even when heavily cited. High-base nodes with sufficient citations are included.

All tests call internal _pattern_* functions directly so score/field assertions are
visible. Follows the fresh_server() pattern from test_query_pattern.py.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_dream_fairy_patterns")


def fresh_server():
    """Reset state and return the server module rebound to the test dir."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    for mod in ("server", "engram_client"):
        sys.modules.pop(mod, None)
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_observation(server, claim: str, url: str = "https://example.com/test") -> str:
    """Create an observation, return its id."""
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test source",
        "quoted_text": claim,
        "interpretation": f"Interpretation of: {claim}",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    return r["observation_id"]


def _add_feeling(server, claim: str) -> str:
    """Create a feeling_report, return its id."""
    r = json.loads(server.engram_report_feeling(payload_json=json.dumps({
        "reported_state": claim,
        "trigger": "test fixture",
        "categorical_tag": "curiosity",
        "intensity_hint": 0.5,
    })))
    return r["feeling_id"]


def _set_node_importance(server, node_id: str, importance_base: float,
                         importance_score: float, recall_count: int = 0,
                         recall_turn: int = 0):
    """Directly set importance fields on a node for test setup."""
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_base=?, importance_score=?, "
            "recall_count=?, recall_turn=? WHERE id=?",
            (importance_base, importance_score, recall_count, recall_turn, node_id),
        )
        conn.commit()
    finally:
        conn.close()


def _add_edge(server, source_id: str, target_id: str, relation: str = "derives_from"):
    """Add a citation edge directly to the DB."""
    import datetime
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = server._get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, ?, ?)",
            (source_id, target_id, relation, created_at),
        )
        conn.commit()
    finally:
        conn.close()


# ── stale_load_bearing ────────────────────────────────────────────────────────


def test_stale_load_bearing_filters_feeling_report():
    """feeling_report nodes must not appear in stale_load_bearing results,
    even when they have importance_base=2.0 and a stale recall_turn."""
    server = fresh_server()

    fl_id = _add_feeling(server, "I notice something interesting here")
    # Set the feeling to look maximally stale: high base, old recall_turn
    _set_node_importance(server, fl_id, importance_base=2.0, importance_score=2.0,
                         recall_count=0, recall_turn=0)

    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["balanced"])
        params["top_k"] = 100
        candidates = server._pattern_stale_load_bearing(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert fl_id not in ids, (
        f"feeling_report {fl_id} appeared in stale_load_bearing results; "
        f"feeling_reports must be filtered at SQL level."
    )


def test_stale_load_bearing_sorts_by_inflation_gap():
    """The node with the larger inflation gap must rank first.

    Setup: advance current_turn so gap arithmetic is meaningful.
      Node A: importance_base=2.0, importance_score=2.0 (recalled at turn 0,
              so score froze at base; now many turns later its gap is large).
      Node B: importance_base=2.0, importance_score=current_inflated_value
              (recalled "just now" so gap is ~0).
    """
    server = fresh_server()

    # Advance the turn counter so inflation is non-trivial
    server._set_current_turn(100)
    mem = server._get_memory_config()
    current_turn = mem.get("current_turn", 100)
    decay_base = mem.get("decay_base", 1.014)

    # Node A: frozen at importance_score=2.0 (as if never recalled since write)
    nid_a = _add_observation(server, "Node A: high importance, old recall")
    _set_node_importance(server, nid_a, importance_base=2.0, importance_score=2.0,
                         recall_count=1, recall_turn=0)

    # Node B: refreshed just now — score equals what base would be at current_turn
    nid_b = _add_observation(server, "Node B: high importance, recent recall")
    fresh_score = round(2.0 * (decay_base ** current_turn), 6)
    _set_node_importance(server, nid_b, importance_base=2.0,
                         importance_score=fresh_score,
                         recall_count=5, recall_turn=current_turn)

    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["balanced"])
        params["top_k"] = 10
        candidates = server._pattern_stale_load_bearing(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert nid_a in ids, "Node A (large gap) must appear in results"
    assert nid_b in ids, "Node B (small gap) must appear in results"

    pos_a = ids.index(nid_a)
    pos_b = ids.index(nid_b)
    assert pos_a < pos_b, (
        f"Node A (large inflation gap) should rank before Node B (small gap), "
        f"but got positions A={pos_a}, B={pos_b}."
    )

    # Verify gap values are in the result dict and correctly ordered
    cand_a = next(c for c in candidates if c["id"] == nid_a)
    cand_b = next(c for c in candidates if c["id"] == nid_b)
    assert cand_a["inflation_gap"] > cand_b["inflation_gap"], (
        f"inflation_gap A={cand_a['inflation_gap']} should exceed B={cand_b['inflation_gap']}"
    )
    # Node A gap should be positive and substantial (2.0 * decay^100 - 2.0 >> 0)
    assert cand_a["inflation_gap"] > 1.0, (
        f"Expected Node A inflation_gap > 1.0 at turn 100, got {cand_a['inflation_gap']}"
    )
    # Node B gap should be near 0
    assert abs(cand_b["inflation_gap"]) < 0.1, (
        f"Expected Node B inflation_gap near 0, got {cand_b['inflation_gap']}"
    )


def test_stale_load_bearing_gates_on_importance_base_floor():
    """Nodes with importance_base <= 0.7 must not appear even if they have a large
    inflation gap (e.g., due to a very old recall_turn)."""
    server = fresh_server()

    nid_low = _add_observation(server, "Low base node, very old, never recalled")
    # importance_base=0.5 < 0.7 floor; importance_score=0.5 (never recalled)
    # Gap would be large because recall_turn=0 and current turn may be > 0,
    # but the SQL gate should exclude it before gap is even computed.
    _set_node_importance(server, nid_low, importance_base=0.5, importance_score=0.5,
                         recall_count=0, recall_turn=0)

    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["balanced"])
        params["top_k"] = 100
        candidates = server._pattern_stale_load_bearing(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert nid_low not in ids, (
        f"Node with importance_base=0.5 (below 0.7 floor) appeared in results. "
        f"SQL gate must exclude it."
    )


# ── cornerstone_candidate ─────────────────────────────────────────────────────


def test_cornerstone_candidate_filters_feeling_report():
    """feeling_report nodes must not appear in cornerstone_candidate results,
    even when they have importance_base=2.0 and are heavily cited."""
    server = fresh_server()

    fl_id = _add_feeling(server, "I feel strongly that this is important")
    _set_node_importance(server, fl_id, importance_base=2.0, importance_score=2.0)

    # Add 5 citation edges pointing to the feeling node to clear the min_citations=3 gate
    for i in range(5):
        src_id = _add_observation(server, f"Citation source {i} for feeling test")
        _add_edge(server, src_id, fl_id, "supports")

    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["balanced"])
        params["top_k"] = 100
        candidates = server._pattern_cornerstone_candidate(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert fl_id not in ids, (
        f"feeling_report {fl_id} appeared in cornerstone_candidate results; "
        f"feeling_reports must be filtered at SQL level."
    )


def test_cornerstone_candidate_gates_on_importance_base_floor():
    """A node with importance_base=0.6 (below 0.7 floor) should NOT appear as a
    cornerstone candidate even when heavily cited. A node with importance_base=1.5
    and sufficient citations SHOULD appear."""
    server = fresh_server()

    # Low-base node with many citations — should not pass the gate
    nid_low = _add_observation(server, "Low base but heavily cited")
    _set_node_importance(server, nid_low, importance_base=0.6, importance_score=0.6)
    for i in range(5):
        src = _add_observation(server, f"Citer of low-base node {i}")
        _add_edge(server, src, nid_low, "derives_from")

    # High-base node with sufficient citations — should pass
    nid_high = _add_observation(server, "High base and heavily cited")
    _set_node_importance(server, nid_high, importance_base=1.5, importance_score=1.5)
    for i in range(5):
        src = _add_observation(server, f"Citer of high-base node {i}")
        _add_edge(server, src, nid_high, "cites")

    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["balanced"])
        params["top_k"] = 100
        candidates = server._pattern_cornerstone_candidate(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert nid_low not in ids, (
        f"Node with importance_base=0.6 (below 0.7 floor) appeared in cornerstone_candidate."
    )
    assert nid_high in ids, (
        f"Node with importance_base=1.5 and 5 citations should appear in cornerstone_candidate."
    )

    # Verify the returned field is importance_base, not importance_score
    cand = next(c for c in candidates if c["id"] == nid_high)
    assert "importance_base" in cand, "Result dict should have importance_base field"
    assert cand["importance_base"] == 1.5


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed = []
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS  {t.__name__}")
        except Exception:
            failed.append(t.__name__)
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        sys.exit(1)
