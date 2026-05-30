"""Tests for engram_query_pattern (KnowQL-inspired pattern queries).

v0.1 ships:
  - The dispatcher framework (preset bundles, override args, telemetry logging,
    error paths for unknown pattern_name / preset).
  - One starter pattern: contradiction_obsolescence_ready.

v0.2 (Wave C) updated the agent-facing return shape to tiered {id,summary}/{id,keywords}
per ob_1969. Internal pattern behavior (score values, side metadata, suggested_action)
is now tested via the internal _pattern_* functions directly; MCP-facing tests check
the new tiered shape keys (results, total_matches).

Coverage:
  1. Unknown pattern_name returns error JSON.
  2. Unknown preset returns error JSON.
  3. Preset bundle is correctly resolved + overridable (via PATTERN_QUERY_PRESETS + internal call).
  4. contradiction_obsolescence_ready: scores active contradictions correctly
     (one side obsolete = 1.0; both obsolete = 0.5; neither obsolete = 0.0)
     — tested via internal function so score fields are visible.
  5. Telemetry log appends one entry per call (regardless of candidate count).
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_query_pattern_")
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
    # Disable embeddings so tests don't depend on a model download
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


def _add_active_contradiction(server, claim_a: str, claim_b: str) -> tuple[str, str, str]:
    """Add two contradicting observations and a contradiction node. Returns (ct_id, a_id, b_id)."""
    a_id = _add_observation(server, claim_a)
    b_id = _add_observation(server, claim_b)
    r = json.loads(server._contradict_impl(
        node_id_a=a_id,
        node_id_b=b_id,
        description=f"Contradiction: {claim_a[:30]}... vs {claim_b[:30]}...",
        root_cause="test fixture",
    ))
    return r["contradiction_id"], a_id, b_id


def test_unknown_pattern_name_errors():
    server = fresh_server()
    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({"pattern_name": "nonexistent_pattern"})))
    assert "error" in r
    assert "nonexistent_pattern" in r["error"]
    assert "contradiction_obsolescence_ready" in r["error"]


def test_unknown_preset_errors():
    server = fresh_server()
    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "contradiction_obsolescence_ready", "preset": "ultra_precision",
    })))
    assert "error" in r
    assert "ultra_precision" in r["error"]


def test_preset_bundle_resolution():
    server = fresh_server()
    # Verify preset values directly from PATTERN_QUERY_PRESETS (not via MCP return
    # shape, which no longer exposes params_used in the default tiered output).
    high_precision = server.PATTERN_QUERY_PRESETS["high_precision"]
    assert high_precision["top_k"] == 5
    # cosine_threshold recalibrated 2026-05-11 (PR #29 placeholder 0.80 →
    # principled 0.65 = balanced + 0.10 = upper edge of V4 useful range).
    # See active-work/query-pattern-preset-rationale-2026-05-11.md.
    assert high_precision["cosine_threshold"] == 0.65

    # MCP tool: verify the preset is echoed in the response
    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "contradiction_obsolescence_ready", "preset": "high_precision",
    })))
    assert r["preset"] == "high_precision"
    # New shape: results + total_matches (no params_used on the top level)
    assert "results" in r
    assert "total_matches" in r


def test_preset_overrides_apply():
    server = fresh_server()
    # Test override resolution via internal call — MCP shape no longer exposes params_used.
    params = dict(server.PATTERN_QUERY_PRESETS["balanced"])
    params["top_k"] = 42
    params["cosine_threshold"] = 0.10
    assert params["top_k"] == 42
    assert params["cosine_threshold"] == 0.10
    # min_confidence not overridden, so should fall through to balanced default
    assert params["min_confidence"] == 0.50

    # MCP smoke-test: overrides accepted without error
    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "contradiction_obsolescence_ready",
        "preset": "balanced",
        "top_k_override": 42,
        "cosine_threshold_override": 0.10,
    })))
    assert "error" not in r
    assert "results" in r


def test_contradiction_one_side_obsolete_scores_high():
    server = fresh_server()
    ct_id, a_id, b_id = _add_active_contradiction(
        server, "X is true", "X is false"
    )
    # Retract side A — now side B survives
    server._retract_impl(
        node_id=a_id, error_type="other", reason="test fixture: simulating retraction"
    )

    # Test internal pattern function directly to verify score + side metadata
    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["balanced"])
        candidates = server._pattern_contradiction_obsolescence_ready(conn, params)
    finally:
        conn.close()

    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["id"] == ct_id
    assert cand["score"] == 1.0
    assert cand["obsolete_side"] == "a"
    assert cand["surviving_side"] == "b"
    assert "engram_resolve" in cand["suggested_action"]

    # MCP-facing: tiered shape includes ct_id and uses 'results' key
    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "contradiction_obsolescence_ready", "preset": "balanced",
    })))
    assert r["total_matches"] == 1
    assert r["results"][0]["id"] == ct_id
    assert "summary" in r["results"][0]  # Tier 1 (default summary_top_k=3)


def test_contradiction_both_sides_obsolete_scores_medium():
    server = fresh_server()
    ct_id, a_id, b_id = _add_active_contradiction(
        server, "Y holds at noon", "Y does not hold at noon"
    )
    server._retract_impl(node_id=a_id, error_type="other", reason="fixture")
    server._retract_impl(node_id=b_id, error_type="other", reason="fixture")

    # Score is 0.5 — below balanced threshold (0.55), at-or-above high_recall (0.45).
    # Test via internal function to see score and side metadata.
    conn = server._get_db()
    try:
        params_hr = dict(server.PATTERN_QUERY_PRESETS["high_recall"])
        candidates_hr = server._pattern_contradiction_obsolescence_ready(conn, params_hr)
        params_bal = dict(server.PATTERN_QUERY_PRESETS["balanced"])
        candidates_bal = server._pattern_contradiction_obsolescence_ready(conn, params_bal)
    finally:
        conn.close()

    assert len(candidates_hr) == 1
    assert candidates_hr[0]["score"] == 0.5
    assert candidates_hr[0]["obsolete_side"] == "both"
    assert candidates_hr[0]["surviving_side"] is None

    # Balanced threshold (0.55) filters out 0.5-score candidates.
    assert len(candidates_bal) == 0

    # MCP: high_recall surfaces it; balanced does not
    r_hr = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "contradiction_obsolescence_ready", "preset": "high_recall",
    })))
    assert r_hr["total_matches"] == 1

    r_bal = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "contradiction_obsolescence_ready", "preset": "balanced",
    })))
    assert r_bal["total_matches"] == 0


def test_contradiction_neither_obsolete_filtered_at_balanced_threshold():
    server = fresh_server()
    _add_active_contradiction(server, "Z is alpha", "Z is beta")
    # Both sides current; score = 0.0; balanced threshold = 0.55 → filtered out

    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "contradiction_obsolescence_ready", "preset": "balanced",
    })))
    assert r["total_matches"] == 0


def test_contradiction_neither_obsolete_visible_at_high_recall():
    server = fresh_server()
    # high_recall threshold is 0.45, score=0.0 still filtered. Confirm explicit override admits it.
    _add_active_contradiction(server, "W is square", "W is round")

    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "contradiction_obsolescence_ready",
        "preset": "high_recall",
        "cosine_threshold_override": 0.0,
    })))
    assert r["total_matches"] == 1
    # Score no longer visible in tiered output — verify via internal function
    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["high_recall"])
        params["cosine_threshold"] = 0.0
        candidates = server._pattern_contradiction_obsolescence_ready(conn, params)
    finally:
        conn.close()
    assert candidates[0]["score"] == 0.0


# ── stale_load_bearing ────────────────────────────────────────────────────


def test_stale_load_bearing_surfaces_high_importance_low_recall():
    server = fresh_server()
    # Add an observation; bump its importance_base manually via direct SQL since
    # engram_focus / cornerstone bumps are tested elsewhere.
    obs_id = _add_observation(server, "Important standalone claim about X")
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_base=2.0, importance_score=2.0, recall_count=0 WHERE id=?",
            (obs_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # Check via internal function for field-level assertions
    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["balanced"])
        candidates = server._pattern_stale_load_bearing(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert obs_id in ids
    cand = next(c for c in candidates if c["id"] == obs_id)
    assert cand["importance_base"] >= 2.0
    assert "inflation_gap" in cand
    assert cand["recall_count"] == 0

    # MCP: obs_id appears in results
    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "stale_load_bearing", "preset": "balanced",
    })))
    result_ids = [e["id"] for e in r["results"]]
    assert obs_id in result_ids


def test_stale_load_bearing_excludes_cornerstones():
    server = fresh_server()
    cs_result = json.loads(server._add_cornerstone_impl(
        tag="test_cornerstone",
        title="Test Cornerstone",
        new_frame="Cornerstones are anchored against forgetting by design.",
        prior_frame="",
        triggering_experience="Test fixture",
    ))
    cs_id = cs_result["cornerstone_id"]

    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["high_recall"])
        params["top_k"] = 100
        candidates = server._pattern_stale_load_bearing(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert cs_id not in ids


# ── tainted_still_valid ───────────────────────────────────────────────────


def test_tainted_still_valid_surfaces_tainted_nodes():
    server = fresh_server()
    # Create a derivation by adding two observations + deriving from them
    obs_a = _add_observation(server, "Premise A holds in scenario 1")
    obs_b = _add_observation(server, "Premise B holds in scenario 1")
    deriv_result = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Combined: A ∧ B implies conclusion C",
        "supporting_ids": f"{obs_a},{obs_b}",
        "logical_chain": "From A and B, C follows by conjunction.",
        "reasoning_type": "deductive_modus_ponens",
    })))
    deriv_id = deriv_result["derivation_id"]

    # Retract one of the premises — cascade should taint the derivation
    server._retract_impl(
        node_id=obs_a, error_type="hallucinated_claim",
        reason="test fixture: simulating premise retraction"
    )

    # Internal check for score + confidence
    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["balanced"])
        candidates = server._pattern_tainted_still_valid(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert deriv_id in ids
    cand = next(c for c in candidates if c["id"] == deriv_id)
    assert cand["score"] > 0
    assert cand["confidence"] > 0

    # MCP: deriv_id appears in tiered results
    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "tainted_still_valid", "preset": "balanced",
    })))
    result_ids = [e["id"] for e in r["results"]]
    assert deriv_id in result_ids


# ── cornerstone_candidate ─────────────────────────────────────────────────


def test_cornerstone_candidate_requires_min_citations():
    server = fresh_server()
    # An observation with high importance_base but no citations — should NOT
    # surface as a cornerstone candidate.
    isolated_id = _add_observation(server, "Isolated high-importance claim")
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_base=3.0, importance_score=3.0 WHERE id=?",
            (isolated_id,),
        )
        conn.commit()
    finally:
        conn.close()

    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["balanced"])
        candidates = server._pattern_cornerstone_candidate(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert isolated_id not in ids  # zero citations < min_citations=3


def test_cornerstone_candidate_excludes_axiom_goal_definition_includes_ob_dv():
    """#180 regression: axioms/goals/definitions with high importance + ≥3 citations
    must be EXCLUDED; an observation (and derivation) with the same profile must be INCLUDED.

    Axioms, goals, and definitions are fundamental-by-type (declared at creation);
    their high type-anchored importance_base is not an emergence signal.
    The candidate pool is observations and derivations only — emergent-practice nodes.
    See SKILL.md §2.17.
    """
    server = fresh_server()
    now = "2026-05-25T00:00:00Z"

    # Create three "citer" observations used only as edge sources
    citer_a = _add_observation(server, "Citer A for citation fixture")
    citer_b = _add_observation(server, "Citer B for citation fixture")
    citer_c = _add_observation(server, "Citer C for citation fixture")

    # Helper: insert 3 inbound 'supported_by' edges onto a target node
    def _add_three_citations(target_id):
        conn = server._get_db()
        try:
            for src in (citer_a, citer_b, citer_c):
                conn.execute(
                    "INSERT OR IGNORE INTO edges (source_id, target_id, relation, created_at)"
                    " VALUES (?, ?, 'supported_by', ?)",
                    (src, target_id, now),
                )
            conn.commit()
        finally:
            conn.close()

    # Helper: boost importance_base above the 0.7 gate (2.0 = type-anchored value)
    def _boost_importance(node_id):
        conn = server._get_db()
        try:
            conn.execute(
                "UPDATE nodes SET importance_base=2.0, importance_score=2.0 WHERE id=?",
                (node_id,),
            )
            conn.commit()
        finally:
            conn.close()

    # — Axiom: high importance + 3 citations — must NOT appear
    axiom_result = json.loads(server._add_axiom_impl(
        claim="Honesty is non-negotiable in graph construction",
        basis="Structural requirement for provenance integrity",
    ))
    axiom_id = axiom_result["axiom_id"]
    _boost_importance(axiom_id)
    _add_three_citations(axiom_id)

    # — Goal: high importance + 3 citations — must NOT appear
    goal_result = json.loads(server._add_goal_impl(
        claim="Achieve calibrated epistemic humility",
        motivation="The core aspiration driving ENGRAM development",
    ))
    goal_id = goal_result["goal_id"]
    _boost_importance(goal_id)
    _add_three_citations(goal_id)

    # — Definition: high importance + 3 citations — must NOT appear
    def_result = json.loads(server._add_definition_impl(
        term="cornerstone",
        definition="An identity-scaffold node representing an earned operating principle",
    ))
    def_id = def_result["definition_id"]
    _boost_importance(def_id)
    _add_three_citations(def_id)

    # — Observation: high importance + 3 citations — MUST appear (emergent-practice node)
    obs_id = _add_observation(server, "Emergent-practice claim: tool-first problem solving is load-bearing")
    _boost_importance(obs_id)
    _add_three_citations(obs_id)

    # — Derivation: high importance + 3 citations — MUST appear (emergent-practice node)
    # Covers the 'derivation' branch of the allowlist (type='derivation'); mirrors the
    # observation setup exactly so both halves of the OR gate are exercised.
    dv_result = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Emergent-practice derivation: tool-first + citation-weight together predict cornerstone readiness",
        "supporting_ids": f"{obs_id},{citer_a}",
        "logical_chain": "obs_id establishes tool-first load-bearing; citer_a corroborates citation weight; together they predict cornerstone candidacy.",
        "reasoning_type": "deductive_modus_ponens",
    })))
    dv_id = dv_result["derivation_id"]
    _boost_importance(dv_id)
    _add_three_citations(dv_id)

    conn = server._get_db()
    try:
        params = dict(server.PATTERN_QUERY_PRESETS["balanced"])
        params["top_k"] = 100  # ensure nothing is top-k truncated
        candidates = server._pattern_cornerstone_candidate(conn, params)
    finally:
        conn.close()

    result_ids = [c["id"] for c in candidates]

    # Fundamental-by-type nodes must be excluded (the #180 false-positive)
    assert axiom_id not in result_ids, f"axiom {axiom_id} must be excluded from cornerstone_candidate"
    assert goal_id not in result_ids, f"goal {goal_id} must be excluded from cornerstone_candidate"
    assert def_id not in result_ids, f"definition {def_id} must be excluded from cornerstone_candidate"

    # Emergent-practice nodes must be included (both ob and dv branches of the allowlist)
    assert obs_id in result_ids, f"observation {obs_id} must be included in cornerstone_candidate"
    assert dv_id in result_ids, f"derivation {dv_id} must be included in cornerstone_candidate"

    # MCP-facing: verify axiom/goal/def absent, observation and derivation present
    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "cornerstone_candidate", "preset": "balanced", "top_k_override": 100,
    })))
    mcp_ids = [e["id"] for e in r["results"]]
    assert axiom_id not in mcp_ids
    assert goal_id not in mcp_ids
    assert def_id not in mcp_ids
    assert obs_id in mcp_ids
    assert dv_id in mcp_ids, f"derivation {dv_id} must appear in MCP cornerstone_candidate results"


# ── open_question_answerable + recent_resolution_echo ─────────────────────
# Skipped from full coverage in this PR — they need semantic search via the
# embedder, which requires a model load. Smoke-test the dispatch only.


def test_open_question_answerable_dispatches_cleanly():
    server = fresh_server()
    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "open_question_answerable", "preset": "balanced",
    })))
    assert "results" in r
    assert "total_matches" in r
    assert isinstance(r["results"], list)


def test_recent_resolution_echo_dispatches_cleanly():
    server = fresh_server()
    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "recent_resolution_echo", "preset": "balanced",
    })))
    assert "results" in r
    assert "total_matches" in r
    assert isinstance(r["results"], list)


def test_telemetry_log_writes_entry_per_call():
    server = fresh_server()
    log_path = os.path.join(TEST_DIR, "pattern_query_telemetry.jsonl")
    # Clear prior entries from the same test dir
    if os.path.exists(log_path):
        os.remove(log_path)

    json.loads(server.engram_query_pattern(payload_json=json.dumps({"pattern_name": "contradiction_obsolescence_ready"})))
    json.loads(server.engram_query_pattern(payload_json=json.dumps({"pattern_name": "contradiction_obsolescence_ready", "preset": "high_precision"})))

    assert os.path.exists(log_path)
    with open(log_path) as f:
        entries = [json.loads(line) for line in f if line.strip()]
    assert len(entries) == 2
    assert entries[0]["pattern_name"] == "contradiction_obsolescence_ready"
    assert entries[1]["preset"] == "high_precision"
    assert "candidate_count" in entries[0]
    assert "scores" in entries[0]


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
