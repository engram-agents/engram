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

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_dream_fairy_patterns_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Reset state and return the server module rebound to the test dir."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
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


# ── stale_load_bearing protection-channel floor (forum #275) ──────────────────
#
# A high-inflation-gap node is only a genuine erosion candidate when it has NO
# visible protection channel. recall_count / inflation_gap is one observation
# port; a node kept live by a tripwire, a doc citation, or edge-traversal has a
# benign gap (importance_score never inflated because those channels aren't
# deliberate recall). Each test isolates ONE channel: the node is made a genuine
# stale candidate, its baseline edges are cleared, and only the channel under
# test is added — so the exclusion (or, for the control, the inclusion) is
# attributable to exactly that channel.


def _set_node_metadata(server, node_id: str, meta: dict):
    """Overwrite a node's metadata JSON (for situation_pattern setup)."""
    conn = server._get_db()
    try:
        conn.execute("UPDATE nodes SET metadata=? WHERE id=?", (json.dumps(meta), node_id))
        conn.commit()
    finally:
        conn.close()


def _clear_edges(server, node_id: str):
    """Remove all edges touching a node, so edge-degree starts at a known 0."""
    conn = server._get_db()
    try:
        conn.execute("DELETE FROM edges WHERE source_id=? OR target_id=?", (node_id, node_id))
        conn.commit()
    finally:
        conn.close()


def _retract_node(server, node_id: str):
    """Mark a node retracted so it should not count as a live edge endpoint."""
    conn = server._get_db()
    try:
        conn.execute("UPDATE nodes SET status='retracted' WHERE id=?", (node_id,))
        conn.commit()
    finally:
        conn.close()


def _make_stale_candidate(server, claim: str) -> str:
    """A high-importance, long-uncrecalled observation with edges cleared — a
    genuine erosion candidate (large inflation gap at turn 100) absent any
    protection channel. Callers advance the turn and add the channel under test."""
    nid = _add_observation(server, claim)
    _set_node_importance(server, nid, importance_base=2.0, importance_score=2.0,
                         recall_count=0, recall_turn=0)
    _clear_edges(server, nid)
    return nid


def _hermetic_surfaces(server):
    """Point the host-derived protection surfaces (CLAUDE.md, plugin skills/) at
    nonexistent paths under the test dir, so the host's real CLAUDE.md or an
    exported CLAUDE_PLUGIN_ROOT never leaks a false doc_cited hit into tests not
    exercising those channels. (warm-briefing.md + principle_triggers.json are
    already DATA_DIR-isolated by fresh_server, so they need no neutralizing.)"""
    os.environ["ENGRAM_CLAUDE_MD_PATH"] = os.path.join(TEST_DIR, "no-such-claude.md")
    os.environ["CLAUDE_PLUGIN_ROOT"] = os.path.join(TEST_DIR, "no-such-plugin-root")


def _run_stale_scan(server, top_k: int = 100):
    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["balanced"])
        params["top_k"] = top_k
        return server._pattern_stale_load_bearing(conn, params)
    finally:
        conn.close()


def test_protection_unprotected_node_still_flagged():
    """CONTROL: a genuine stale candidate with NO protection channel (no
    situation_pattern, not doc-cited, edge-degree 0) MUST still be flagged.
    Guards against the fix over-suppressing real erosion candidates."""
    server = fresh_server()
    _hermetic_surfaces(server)
    server._set_current_turn(100)

    nid = _make_stale_candidate(server, "Genuinely eroding, unprotected node")

    ids = [c["id"] for c in _run_stale_scan(server)]
    assert nid in ids, (
        "An unprotected high-gap node must still be flagged as an erosion "
        "candidate — the protection floor must not suppress real candidates."
    )


def test_protection_skips_own_situation_pattern():
    """A node with situation_pattern on its OWN metadata is tripwire-protected
    and must be excluded despite a large gap."""
    server = fresh_server()
    _hermetic_surfaces(server)
    server._set_current_turn(100)

    nid = _make_stale_candidate(server, "Tripwire-protected node")
    _set_node_metadata(server, nid, {"situation_pattern": "about to assert.*first"})

    ids = [c["id"] for c in _run_stale_scan(server)]
    assert nid not in ids, (
        "A node with its own situation_pattern tripwire must be excluded."
    )


def test_protection_skips_owning_lesson_situation_pattern():
    """The ob_0668 case: the scanned node has NO own situation_pattern, but it
    `exemplifies` a lesson whose metadata DOES — the node->owning-lesson
    traversal must find that protection. Without the traversal the exemplar
    observation looks unprotected and false-alarms forever."""
    server = fresh_server()
    _hermetic_surfaces(server)
    server._set_current_turn(100)

    nid = _make_stale_candidate(server, "Exemplar of a tripwire-protected lesson")
    # The owning lesson (any node row will do for the traversal mechanism);
    # give it the situation_pattern the scanned node lacks.
    lesson_id = _add_observation(server, "Lesson: probe before asserting first-ness")
    _set_node_metadata(server, lesson_id, {"situation_pattern": "first.*contact"})
    # node --exemplifies--> lesson (source=exemplar, target=lesson).
    _add_edge(server, nid, lesson_id, "exemplifies")

    ids = [c["id"] for c in _run_stale_scan(server)]
    assert nid not in ids, (
        "A node exemplifying a lesson whose situation_pattern protects it must "
        "be excluded via the node->owning-lesson traversal (the ob_0668 miss)."
    )


def test_protection_skips_doc_cited_warm_briefing():
    """A node whose id appears in warm-briefing.md is read-time-load-protected
    and must be excluded."""
    server = fresh_server()
    _hermetic_surfaces(server)
    server._set_current_turn(100)

    nid = _make_stale_candidate(server, "Node cited in warm-briefing")
    with open(os.path.join(TEST_DIR, "warm-briefing.md"), "w") as f:
        f.write(f"Currently focused:\n- [{nid}] some load-bearing claim\n")

    ids = [c["id"] for c in _run_stale_scan(server)]
    assert nid not in ids, "A node cited in warm-briefing.md must be excluded."


def test_protection_skips_doc_cited_claude_md():
    """A node whose id appears in the CLAUDE.md surface (via the env seam) is
    read-time-load-protected and must be excluded."""
    server = fresh_server()
    server._set_current_turn(100)

    nid = _make_stale_candidate(server, "Node cited in CLAUDE.md")
    claude_path = os.path.join(TEST_DIR, "test-claude.md")
    with open(claude_path, "w") as f:
        f.write(f"Operating principle ({nid}, focus-pinned): ...\n")
    os.environ["ENGRAM_CLAUDE_MD_PATH"] = claude_path

    ids = [c["id"] for c in _run_stale_scan(server)]
    assert nid not in ids, "A node cited in the CLAUDE.md surface must be excluded."


def test_protection_skips_high_edge_degree():
    """A node with live edge-degree >= the floor (2) is traversal-reachable and
    must be excluded."""
    server = fresh_server()
    _hermetic_surfaces(server)
    server._set_current_turn(100)

    nid = _make_stale_candidate(server, "Highly-connected node")
    n1 = _add_observation(server, "Live neighbor 1")
    n2 = _add_observation(server, "Live neighbor 2")
    _add_edge(server, nid, n1, "derives_from")
    _add_edge(server, n2, nid, "cites")  # in-edge counts too

    ids = [c["id"] for c in _run_stale_scan(server)]
    assert nid not in ids, (
        "A node with live edge-degree >= 2 must be excluded (traversal-protected)."
    )


def test_protection_edge_degree_below_floor_still_flagged():
    """A node with edge-degree 1 (below the floor of 2) is NOT edge-protected and
    must still be flagged — verifies the floor boundary."""
    server = fresh_server()
    _hermetic_surfaces(server)
    server._set_current_turn(100)

    nid = _make_stale_candidate(server, "Weakly-connected node")
    n1 = _add_observation(server, "Sole live neighbor")
    _add_edge(server, nid, n1, "derives_from")  # degree 1 < floor

    ids = [c["id"] for c in _run_stale_scan(server)]
    assert nid in ids, (
        "A node with edge-degree 1 (below floor 2) must still be flagged."
    )


def test_protection_retracted_edges_do_not_count():
    """Edges to retracted/non-current nodes must NOT count toward edge-degree —
    a node connected only to dead nodes is not traversal-protected."""
    server = fresh_server()
    _hermetic_surfaces(server)
    server._set_current_turn(100)

    nid = _make_stale_candidate(server, "Connected only to retracted nodes")
    d1 = _add_observation(server, "Dead neighbor 1")
    d2 = _add_observation(server, "Dead neighbor 2")
    _add_edge(server, nid, d1, "derives_from")
    _add_edge(server, nid, d2, "cites")
    _retract_node(server, d1)
    _retract_node(server, d2)

    ids = [c["id"] for c in _run_stale_scan(server)]
    assert nid in ids, (
        "Edges to retracted nodes must not protect a node — it should still be "
        "flagged when its only edges point to dead nodes."
    )


def test_protection_skips_doc_cited_registry():
    """The registry_trigger branch of the doc_cited channel: a node whose id is
    registered as a principle in principle_triggers.json is read-time-load
    protected and must be excluded. (Complements the warm-briefing/CLAUDE.md
    doc_cited tests, which don't exercise the registry branch.)"""
    server = fresh_server()
    _hermetic_surfaces(server)
    server._set_current_turn(100)

    nid = _make_stale_candidate(server, "Node registered in principle_triggers.json")
    # principle_triggers.json lives at DATA_DIR/principle_triggers.json (= TEST_DIR,
    # test-isolated). Registry values are lists of entries keyed by principle_id.
    with open(os.path.join(TEST_DIR, "principle_triggers.json"), "w") as f:
        json.dump({"some_trigger_node": [{"principle_id": nid}]}, f)

    ids = [c["id"] for c in _run_stale_scan(server)]
    assert nid not in ids, (
        "A node registered in principle_triggers.json must be excluded "
        "(registry_trigger branch of doc_cited)."
    )


def test_protection_skips_doc_cited_skill_file():
    """The skill-file branch of the doc_cited channel: a node whose id appears in
    a *.md under $CLAUDE_PLUGIN_ROOT/skills/ is read-time-load protected and must
    be excluded. (Not covered by the warm-briefing/CLAUDE.md doc_cited tests.)"""
    server = fresh_server()
    # Hermetic CLAUDE.md, but set CLAUDE_PLUGIN_ROOT explicitly to the temp skill
    # tree under test (do NOT use _hermetic_surfaces, which blanks it).
    os.environ["ENGRAM_CLAUDE_MD_PATH"] = os.path.join(TEST_DIR, "no-such-claude.md")
    server._set_current_turn(100)

    nid = _make_stale_candidate(server, "Node cited in a skill file")
    plugin_root = os.path.join(TEST_DIR, "plugin")
    skill_dir = os.path.join(plugin_root, "skills", "some-skill")
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
        f.write(f"# Some skill\n\nSee {nid} for the load-bearing rule.\n")
    os.environ["CLAUDE_PLUGIN_ROOT"] = plugin_root

    ids = [c["id"] for c in _run_stale_scan(server)]
    assert nid not in ids, (
        "A node cited in a skill file under CLAUDE_PLUGIN_ROOT/skills must be "
        "excluded (skill-file branch of doc_cited)."
    )


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
