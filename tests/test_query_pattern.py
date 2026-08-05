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

Import-pattern contract (#948 wave 1 — R1):
    This file imports engram_query, engram_observation, engram_revision,
    engram_derive, engram_epistemic, engram_tasks, engram_cornerstone, and
    engram_core directly and contains no ``import server``. Module objects are
    passed to fresh_module() (R4) for per-test reload isolation.
"""

import atexit
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_cornerstone
import engram_derive
import engram_epistemic
import engram_observation
import engram_query
import engram_revision
import engram_tasks

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_query_pattern_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _fresh_mods():
    """Reload all family modules, configure a clean test DB."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    fresh_module(engram_query)
    for name in [
        "engram_observation", "engram_revision", "engram_derive",
        "engram_epistemic", "engram_tasks", "engram_cornerstone",
    ]:
        importlib.import_module(name)
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return core


def _mods():
    """Return the currently-loaded family module objects."""
    qry = sys.modules["engram_query"]
    obs = sys.modules["engram_observation"]
    rev = sys.modules["engram_revision"]
    drv = sys.modules["engram_derive"]
    epi = sys.modules["engram_epistemic"]
    tsk = sys.modules["engram_tasks"]
    cst = sys.modules["engram_cornerstone"]
    core = sys.modules["engram_core"]
    return qry, obs, rev, drv, epi, tsk, cst, core


def _add_observation(obs, claim: str, url: str = "https://example.com/test") -> str:
    """Create an observation, return its id."""
    r = json.loads(obs._add_observation_impl(
        url=url,
        title="Test source",
        quoted_text=claim,
        interpretation=f"Interpretation of: {claim}",
        claim=claim,
        quote_type="hard_data",
    ))
    return r["observation_id"]


def _add_active_contradiction(drv, obs, claim_a: str, claim_b: str) -> tuple:
    """Add two contradicting observations and a contradiction node. Returns (ct_id, a_id, b_id)."""
    a_id = _add_observation(obs, claim_a)
    b_id = _add_observation(obs, claim_b)
    r = json.loads(drv._contradict_impl(
        node_id_a=a_id,
        node_id_b=b_id,
        description=f"Contradiction: {claim_a[:30]}... vs {claim_b[:30]}...",
        root_cause="test fixture",
    ))
    return r["contradiction_id"], a_id, b_id


def test_unknown_pattern_name_errors():
    _fresh_mods()
    qry, *_ = _mods()
    r = json.loads(qry._query_pattern_impl(pattern_name="nonexistent_pattern"))
    assert "error" in r
    assert "nonexistent_pattern" in r["error"]
    assert "contradiction_obsolescence_ready" in r["error"]


def test_unknown_preset_errors():
    _fresh_mods()
    qry, *_ = _mods()
    r = json.loads(qry._query_pattern_impl(
        pattern_name="contradiction_obsolescence_ready", preset="ultra_precision",
    ))
    assert "error" in r
    assert "ultra_precision" in r["error"]


def test_preset_bundle_resolution():
    _fresh_mods()
    qry, *_ = _mods()
    # Verify preset values directly from PATTERN_QUERY_PRESETS
    high_precision = qry.PATTERN_QUERY_PRESETS["high_precision"]
    assert high_precision["top_k"] == 5
    # cosine_threshold recalibrated 2026-05-11 (PR #29 placeholder 0.80 →
    # principled 0.65 = balanced + 0.10 = upper edge of V4 useful range).
    assert high_precision["cosine_threshold"] == 0.65

    # Direct call: verify preset is echoed in the response
    r = json.loads(qry._query_pattern_impl(
        pattern_name="contradiction_obsolescence_ready", preset="high_precision",
    ))
    assert r["preset"] == "high_precision"
    # New shape: results + total_matches
    assert "results" in r
    assert "total_matches" in r


def test_preset_overrides_apply():
    _fresh_mods()
    qry, *_ = _mods()
    # Test override resolution via direct call
    params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])
    params["top_k"] = 42
    params["cosine_threshold"] = 0.10
    assert params["top_k"] == 42
    assert params["cosine_threshold"] == 0.10
    # min_confidence not overridden, so should fall through to balanced default
    assert params["min_confidence"] == 0.50

    # Smoke-test: overrides accepted without error
    r = json.loads(qry._query_pattern_impl(
        pattern_name="contradiction_obsolescence_ready",
        preset="balanced",
        top_k_override=42,
        cosine_threshold_override=0.10,
    ))
    assert "error" not in r
    assert "results" in r


def test_contradiction_one_side_obsolete_scores_high():
    core = _fresh_mods()
    qry, obs, rev, drv, epi, tsk, cst, core = _mods()
    ct_id, a_id, b_id = _add_active_contradiction(drv, obs, "X is true", "X is false")
    # Retract side A — now side B survives
    rev._retract_impl(
        node_id=a_id, error_type="other", reason="test fixture: simulating retraction"
    )

    # Test internal pattern function directly to verify score + side metadata
    conn = core._get_db()
    try:
        params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])
        candidates = qry._pattern_contradiction_obsolescence_ready(conn, params)
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
    r = json.loads(qry._query_pattern_impl(
        pattern_name="contradiction_obsolescence_ready", preset="balanced",
    ))
    assert r["total_matches"] == 1
    assert r["results"][0]["id"] == ct_id
    assert "summary" in r["results"][0]  # Tier 1 (default summary_top_k=3)


def test_contradiction_both_sides_obsolete_scores_medium():
    core = _fresh_mods()
    qry, obs, rev, drv, epi, tsk, cst, core = _mods()
    ct_id, a_id, b_id = _add_active_contradiction(drv, obs, "Y holds at noon", "Y does not hold at noon")
    rev._retract_impl(node_id=a_id, error_type="other", reason="fixture")
    rev._retract_impl(node_id=b_id, error_type="other", reason="fixture")

    # Score is 0.5 — below balanced threshold (0.55), at-or-above high_recall (0.45).
    conn = core._get_db()
    try:
        params_hr = dict(qry.PATTERN_QUERY_PRESETS["high_recall"])
        candidates_hr = qry._pattern_contradiction_obsolescence_ready(conn, params_hr)
        params_bal = dict(qry.PATTERN_QUERY_PRESETS["balanced"])
        candidates_bal = qry._pattern_contradiction_obsolescence_ready(conn, params_bal)
    finally:
        conn.close()

    assert len(candidates_hr) == 1
    assert candidates_hr[0]["score"] == 0.5
    assert candidates_hr[0]["obsolete_side"] == "both"
    assert candidates_hr[0]["surviving_side"] is None

    # Balanced threshold (0.55) filters out 0.5-score candidates.
    assert len(candidates_bal) == 0

    # high_recall surfaces it; balanced does not
    r_hr = json.loads(qry._query_pattern_impl(
        pattern_name="contradiction_obsolescence_ready", preset="high_recall",
    ))
    assert r_hr["total_matches"] == 1

    r_bal = json.loads(qry._query_pattern_impl(
        pattern_name="contradiction_obsolescence_ready", preset="balanced",
    ))
    assert r_bal["total_matches"] == 0


def test_contradiction_neither_obsolete_filtered_at_balanced_threshold():
    _fresh_mods()
    qry, obs, rev, drv, *_ = _mods()
    _add_active_contradiction(drv, obs, "Z is alpha", "Z is beta")
    # Both sides current; score = 0.0; balanced threshold = 0.55 → filtered out

    r = json.loads(qry._query_pattern_impl(
        pattern_name="contradiction_obsolescence_ready", preset="balanced",
    ))
    assert r["total_matches"] == 0


def test_contradiction_neither_obsolete_visible_at_high_recall():
    core = _fresh_mods()
    qry, obs, rev, drv, *_ = _mods()
    # high_recall threshold is 0.45, score=0.0 still filtered. Confirm explicit override admits it.
    _add_active_contradiction(drv, obs, "W is square", "W is round")

    r = json.loads(qry._query_pattern_impl(
        pattern_name="contradiction_obsolescence_ready",
        preset="high_recall",
        cosine_threshold_override=0.0,
    ))
    assert r["total_matches"] == 1
    # Score no longer visible in tiered output — verify via internal function
    conn = core._get_db()
    try:
        params = dict(qry.PATTERN_QUERY_PRESETS["high_recall"])
        params["cosine_threshold"] = 0.0
        candidates = qry._pattern_contradiction_obsolescence_ready(conn, params)
    finally:
        conn.close()
    assert candidates[0]["score"] == 0.0


# ── stale_load_bearing ────────────────────────────────────────────────────


def test_stale_load_bearing_surfaces_high_importance_low_recall():
    core = _fresh_mods()
    qry, obs, *_ = _mods()
    core = sys.modules["engram_core"]
    obs_id = _add_observation(obs, "Important standalone claim about X")
    conn = core._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_base=2.0, importance_score=2.0, recall_count=0 WHERE id=?",
            (obs_id,),
        )
        conn.commit()
    finally:
        conn.close()

    conn = core._get_db()
    try:
        params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])
        candidates = qry._pattern_stale_load_bearing(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert obs_id in ids
    cand = next(c for c in candidates if c["id"] == obs_id)
    assert cand["importance_base"] >= 2.0
    assert "inflation_gap" in cand
    assert cand["recall_count"] == 0

    r = json.loads(qry._query_pattern_impl(
        pattern_name="stale_load_bearing", preset="balanced",
    ))
    result_ids = [e["id"] for e in r["results"]]
    assert obs_id in result_ids


def test_stale_load_bearing_excludes_cornerstones():
    core = _fresh_mods()
    qry, obs, rev, drv, epi, tsk, cst, core = _mods()
    cs_result = json.loads(cst._add_cornerstone_impl(
        tag="test_cornerstone",
        title="Test Cornerstone",
        new_frame="Cornerstones are anchored against forgetting by design.",
        prior_frame="",
        triggering_experience="Test fixture",
    ))
    cs_id = cs_result["cornerstone_id"]

    conn = core._get_db()
    try:
        params = dict(qry.PATTERN_QUERY_PRESETS["high_recall"])
        params["top_k"] = 100
        candidates = qry._pattern_stale_load_bearing(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert cs_id not in ids


# ── tainted_still_valid ───────────────────────────────────────────────────


def test_tainted_still_valid_surfaces_tainted_nodes():
    core = _fresh_mods()
    qry, obs, rev, drv, epi, tsk, cst, core = _mods()
    obs_a = _add_observation(obs, "Premise A holds in scenario 1")
    obs_b = _add_observation(obs, "Premise B holds in scenario 1")
    deriv_result = json.loads(drv._derive_impl(
        claim="Combined: A ∧ B implies conclusion C",
        supporting_ids=f"{obs_a},{obs_b}",
        logical_chain="From A and B, C follows by conjunction.",
        reasoning_type="deductive_modus_ponens",
    ))
    deriv_id = deriv_result["derivation_id"]

    # Retract one of the premises — cascade should taint the derivation
    rev._retract_impl(
        node_id=obs_a, error_type="hallucinated_claim",
        reason="test fixture: simulating premise retraction"
    )

    conn = core._get_db()
    try:
        params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])
        candidates = qry._pattern_tainted_still_valid(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert deriv_id in ids
    cand = next(c for c in candidates if c["id"] == deriv_id)
    assert cand["score"] > 0
    assert cand["confidence"] > 0

    r = json.loads(qry._query_pattern_impl(
        pattern_name="tainted_still_valid", preset="balanced",
    ))
    result_ids = [e["id"] for e in r["results"]]
    assert deriv_id in result_ids


# ── cornerstone_candidate ─────────────────────────────────────────────────


def test_cornerstone_candidate_requires_min_citations():
    core = _fresh_mods()
    qry, obs, *_ = _mods()
    core = sys.modules["engram_core"]
    isolated_id = _add_observation(obs, "Isolated high-importance claim")
    conn = core._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_base=3.0, importance_score=3.0 WHERE id=?",
            (isolated_id,),
        )
        conn.commit()
    finally:
        conn.close()

    conn = core._get_db()
    try:
        params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])
        candidates = qry._pattern_cornerstone_candidate(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert isolated_id not in ids  # zero citations < min_citations=3


def test_cornerstone_candidate_excludes_axiom_goal_definition_includes_ob_dv():
    """#180 regression: axioms/goals/definitions with high importance + ≥3 citations
    must be EXCLUDED; an observation (and derivation) with the same profile must be INCLUDED.
    """
    core = _fresh_mods()
    qry, obs, rev, drv, epi, tsk, cst, core = _mods()
    now = "2026-05-25T00:00:00Z"

    # Create three "citer" observations used only as edge sources
    citer_a = _add_observation(obs, "Citer A for citation fixture")
    citer_b = _add_observation(obs, "Citer B for citation fixture")
    citer_c = _add_observation(obs, "Citer C for citation fixture")

    def _add_three_citations(target_id):
        conn = core._get_db()
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

    def _boost_importance(node_id):
        conn = core._get_db()
        try:
            conn.execute(
                "UPDATE nodes SET importance_base=2.0, importance_score=2.0 WHERE id=?",
                (node_id,),
            )
            conn.commit()
        finally:
            conn.close()

    # Axiom: high importance + 3 citations — must NOT appear
    axiom_result = json.loads(epi._add_axiom_impl(
        claim="Honesty is non-negotiable in graph construction",
        basis="Structural requirement for provenance integrity",
    ))
    axiom_id = axiom_result["axiom_id"]
    _boost_importance(axiom_id)
    _add_three_citations(axiom_id)

    # Goal: high importance + 3 citations — must NOT appear
    goal_result = json.loads(tsk._add_goal_impl(
        claim="Achieve calibrated epistemic humility",
        motivation="The core aspiration driving ENGRAM development",
    ))
    goal_id = goal_result["goal_id"]
    _boost_importance(goal_id)
    _add_three_citations(goal_id)

    # Definition: high importance + 3 citations — must NOT appear
    def_result = json.loads(epi._add_definition_impl(
        term="cornerstone",
        definition="An identity-scaffold node representing an earned operating principle",
    ))
    def_id = def_result["definition_id"]
    _boost_importance(def_id)
    _add_three_citations(def_id)

    # Observation: high importance + 3 citations — MUST appear (emergent-practice node)
    obs_id = _add_observation(obs, "Emergent-practice claim: tool-first problem solving is load-bearing")
    _boost_importance(obs_id)
    _add_three_citations(obs_id)

    # Derivation: high importance + 3 citations — MUST appear (emergent-practice node)
    dv_result = json.loads(drv._derive_impl(
        claim="Emergent-practice derivation: tool-first + citation-weight together predict cornerstone readiness",
        supporting_ids=f"{obs_id},{citer_a}",
        logical_chain="obs_id establishes tool-first load-bearing; citer_a corroborates citation weight; together they predict cornerstone candidacy.",
        reasoning_type="deductive_modus_ponens",
    ))
    dv_id = dv_result["derivation_id"]
    _boost_importance(dv_id)
    _add_three_citations(dv_id)

    conn = core._get_db()
    try:
        params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])
        params["top_k"] = 100
        candidates = qry._pattern_cornerstone_candidate(conn, params)
    finally:
        conn.close()

    result_ids = [c["id"] for c in candidates]

    assert axiom_id not in result_ids, f"axiom {axiom_id} must be excluded from cornerstone_candidate"
    assert goal_id not in result_ids, f"goal {goal_id} must be excluded from cornerstone_candidate"
    assert def_id not in result_ids, f"definition {def_id} must be excluded from cornerstone_candidate"
    assert obs_id in result_ids, f"observation {obs_id} must be included in cornerstone_candidate"
    assert dv_id in result_ids, f"derivation {dv_id} must be included in cornerstone_candidate"

    # MCP-facing: verify axiom/goal/def absent, observation and derivation present
    r = json.loads(qry._query_pattern_impl(
        pattern_name="cornerstone_candidate", preset="balanced", top_k_override=100,
    ))
    mcp_ids = [e["id"] for e in r["results"]]
    assert axiom_id not in mcp_ids
    assert goal_id not in mcp_ids
    assert def_id not in mcp_ids
    assert obs_id in mcp_ids
    assert dv_id in mcp_ids, f"derivation {dv_id} must appear in MCP cornerstone_candidate results"


def test_cornerstone_candidate_excludes_nodes_already_anchored_to_a_cornerstone():
    """#1913 regression: a node wired as an existing cornerstone's own
    supporting evidence (cornerstone --supported_by--> node) must NOT be
    nominated as a candidate for a *new* cornerstone, even with high
    importance + >=3 citations from ordinary observations. Counting the
    cornerstone's own edge toward the node's citation score was the bug —
    it double-counts the very relationship that disqualifies the node.
    """
    core = _fresh_mods()
    qry, obs, rev, drv, epi, tsk, cst, core = _mods()
    now = "2026-05-25T00:00:00Z"

    citer_a = _add_observation(obs, "Citer A for anchored-node fixture")
    citer_b = _add_observation(obs, "Citer B for anchored-node fixture")
    citer_c = _add_observation(obs, "Citer C for anchored-node fixture")

    def _boost_importance(node_id):
        conn = core._get_db()
        try:
            conn.execute(
                "UPDATE nodes SET importance_base=2.0, importance_score=2.0 WHERE id=?",
                (node_id,),
            )
            conn.commit()
        finally:
            conn.close()

    # Evidence node: high importance, 2 ordinary citations + 1 edge FROM an
    # existing cornerstone (its own supported_by link to this evidence).
    # Total incoming edges = 3 (>= min_citations), but it must still be
    # excluded because one of those edges comes from a current cornerstone.
    evidence_id = _add_observation(obs, "Evidence already anchored to an existing cornerstone")
    _boost_importance(evidence_id)
    conn = core._get_db()
    try:
        for src in (citer_a, citer_b):
            conn.execute(
                "INSERT OR IGNORE INTO edges (source_id, target_id, relation, created_at)"
                " VALUES (?, ?, 'supported_by', ?)",
                (src, evidence_id, now),
            )
        conn.commit()
    finally:
        conn.close()

    cst_result = json.loads(cst._add_cornerstone_impl(
        tag="test-anchor",
        title="Test cornerstone anchoring evidence_id",
        new_frame="A durable operating principle for this test",
        prior_frame="No such principle yet",
        triggering_experience="A fixture needed a cornerstone to anchor evidence to",
        supporting_ids=evidence_id,
    ))
    cornerstone_id = cst_result["cornerstone_id"]

    # Untouched control: same importance + citation count, but all 3 citing
    # edges come from ordinary observations, not the cornerstone — must
    # still be nominated.
    control_id = _add_observation(obs, "Load-bearing claim with no cornerstone anchor")
    _boost_importance(control_id)
    conn = core._get_db()
    try:
        for src in (citer_a, citer_b, citer_c):
            conn.execute(
                "INSERT OR IGNORE INTO edges (source_id, target_id, relation, created_at)"
                " VALUES (?, ?, 'cites', ?)",
                (src, control_id, now),
            )
        conn.commit()
    finally:
        conn.close()

    conn = core._get_db()
    try:
        params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])
        params["top_k"] = 100
        candidates = qry._pattern_cornerstone_candidate(conn, params)
    finally:
        conn.close()

    result_ids = [c["id"] for c in candidates]
    assert evidence_id not in result_ids, (
        f"{evidence_id} is already anchored to cornerstone {cornerstone_id} "
        "and must be excluded, not merely down-scored"
    )
    assert control_id in result_ids, (
        f"{control_id} has the same importance/citation profile but no "
        "cornerstone edge and must still be nominated"
    )


def test_cornerstone_candidate_exclusion_is_relation_agnostic():
    """#1913 colleague-review follow-up (Borges, PR #1917): the exclusion
    check is deliberately relation-agnostic -- ANY incoming edge from a
    current cornerstone disqualifies a candidate, not just the
    supported_by/cites/derives_from relations the citation count tallies.
    The prior test only ever exercised supported_by; this one exercises a
    DIFFERENT relation (about, which cornerstones use in the wild per
    Borges's live-graph check on the review) to pin the anti-drift
    property the code comment claims but didn't have a test for -- so a
    future 'optimize the query with a relation IN (...) filter' change
    can't silently narrow the exclusion back to the three counted
    relations without a test catching it.
    """
    core = _fresh_mods()
    qry, obs, rev, drv, epi, tsk, cst, core = _mods()
    now = "2026-05-25T00:00:00Z"

    citer_a = _add_observation(obs, "Citer A for relation-agnostic fixture")
    citer_b = _add_observation(obs, "Citer B for relation-agnostic fixture")
    citer_c = _add_observation(obs, "Citer C for relation-agnostic fixture")

    def _boost_importance(node_id):
        conn = core._get_db()
        try:
            conn.execute(
                "UPDATE nodes SET importance_base=2.0, importance_score=2.0 WHERE id=?",
                (node_id,),
            )
            conn.commit()
        finally:
            conn.close()

    # High importance + 3 ordinary citations (clears min_citations on its
    # own merit, unlike the supported_by fixture above where the
    # cornerstone's own edge was needed to cross the threshold) -- isolates
    # the relation-agnostic check from the double-counting check.
    node_id = _add_observation(obs, "Node cited by a cornerstone via a non-supported_by relation")
    _boost_importance(node_id)
    conn = core._get_db()
    try:
        for src in (citer_a, citer_b, citer_c):
            conn.execute(
                "INSERT OR IGNORE INTO edges (source_id, target_id, relation, created_at)"
                " VALUES (?, ?, 'cites', ?)",
                (src, node_id, now),
            )
        conn.commit()
    finally:
        conn.close()

    cst_result = json.loads(cst._add_cornerstone_impl(
        tag="test-anchor-relation-agnostic",
        title="Test cornerstone anchoring node_id via a non-supported_by edge",
        new_frame="A durable operating principle for this test",
        prior_frame="No such principle yet",
        triggering_experience="A fixture needed a cornerstone to anchor node_id to, via a different relation",
        supporting_ids="",
    ))
    cornerstone_id = cst_result["cornerstone_id"]
    conn = core._get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO edges (source_id, target_id, relation, created_at)"
            " VALUES (?, ?, 'about', ?)",
            (cornerstone_id, node_id, now),
        )
        conn.commit()
    finally:
        conn.close()

    conn = core._get_db()
    try:
        params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])
        params["top_k"] = 100
        candidates = qry._pattern_cornerstone_candidate(conn, params)
    finally:
        conn.close()

    result_ids = [c["id"] for c in candidates]
    assert node_id not in result_ids, (
        f"{node_id} has an incoming 'about' edge from cornerstone {cornerstone_id} "
        "-- the exclusion is relation-agnostic and must catch this, not just supported_by"
    )


# ── open_question_answerable + recent_resolution_echo ─────────────────────

def test_open_question_answerable_dispatches_cleanly():
    _fresh_mods()
    qry, *_ = _mods()
    r = json.loads(qry._query_pattern_impl(
        pattern_name="open_question_answerable", preset="balanced",
    ))
    assert "results" in r
    assert "total_matches" in r
    assert isinstance(r["results"], list)


def test_recent_resolution_echo_dispatches_cleanly():
    _fresh_mods()
    qry, *_ = _mods()
    r = json.loads(qry._query_pattern_impl(
        pattern_name="recent_resolution_echo", preset="balanced",
    ))
    assert "results" in r
    assert "total_matches" in r
    assert isinstance(r["results"], list)


def test_telemetry_log_writes_entry_per_call():
    _fresh_mods()
    qry, *_ = _mods()
    log_path = os.path.join(TEST_DIR, "pattern_query_telemetry.jsonl")
    if os.path.exists(log_path):
        os.remove(log_path)

    json.loads(qry._query_pattern_impl(pattern_name="contradiction_obsolescence_ready"))
    json.loads(qry._query_pattern_impl(
        pattern_name="contradiction_obsolescence_ready", preset="high_precision"
    ))

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
