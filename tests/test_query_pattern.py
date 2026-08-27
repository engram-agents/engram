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


def test_tainted_still_valid_min_confidence_is_cb_only():
    """#2252 stage 1.7 regression (site 3, _pattern_tainted_still_valid).

    Before the fix, `conf = conf or 0` coerced a NULL confidence to 0
    *before* the threshold check, so any NULL-confidence node -- CB or NCB
    -- was silently dropped whenever min_confidence > 0. The fix asks
    "is this claim-bearing" first: an NCB node (cornerstone) with NULL
    confidence must survive regardless of threshold, while a CB node
    (observation) with a genuinely low, non-null confidence below
    threshold must still be filtered.

    The `tainted_by` marker is set directly via SQL rather than through a
    real retraction cascade -- the query itself has no type gate (it is a
    Layer-C graph-conformance read over metadata, not a re-derivation of
    the cascade walker), so this exercises the filter's actual contract.
    Note: today's cascade walker has an explicit cornerstone/lesson skip
    rule (PR-A-SPEC Sec2.0, engram_revision.py's `_walk_cascade_downstream`)
    that means a cornerstone would not naturally acquire `tainted_by` via
    the live write path -- flagged here rather than silently assumed.

    A third fixture covers the CB-side NULL case (reviewer-fairy suggestion,
    round 1): before the fix, `conf = conf or 0` dropped ANY NULL-confidence
    node regardless of type, so a CB node (observation) that happens to carry
    NULL confidence -- exceptional, since observations normally get a real
    confidence at write time, but not schema-enforced -- must also survive,
    exercising the "CB and confidence is None" branch of the gate distinctly
    from the NCB-null and CB-low-non-null cases already covered above.
    """
    core = _fresh_mods()
    qry, obs, rev, drv, epi, tsk, cst, core = _mods()

    cst_result = json.loads(cst._add_cornerstone_impl(
        tag="test-min-confidence-axis",
        title="Test cornerstone for min_confidence CB-only gate",
        new_frame="New frame for regression fixture.",
    ))
    cst_id = cst_result["cornerstone_id"]

    low_conf_obs_id = _add_observation(obs, "Low-confidence tainted observation fixture")
    null_conf_obs_id = _add_observation(obs, "NULL-confidence CB tainted observation fixture")

    conn = core._get_db()
    try:
        row = conn.execute("SELECT confidence FROM nodes WHERE id=?", (cst_id,)).fetchone()
        assert row["confidence"] is None  # sanity: cornerstones never carry confidence

        conn.execute(
            "UPDATE nodes SET metadata = json_set(COALESCE(metadata, '{}'), "
            "'$.tainted_by', json(?)) WHERE id=?",
            (json.dumps(["some_upstream_id"]), cst_id),
        )
        conn.execute(
            "UPDATE nodes SET confidence=0.10, "
            "metadata = json_set(COALESCE(metadata, '{}'), '$.tainted_by', json(?)) "
            "WHERE id=?",
            (json.dumps(["some_upstream_id"]), low_conf_obs_id),
        )
        conn.execute(
            "UPDATE nodes SET confidence=NULL, "
            "metadata = json_set(COALESCE(metadata, '{}'), '$.tainted_by', json(?)) "
            "WHERE id=?",
            (json.dumps(["some_upstream_id"]), null_conf_obs_id),
        )
        conn.commit()

        params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])  # min_confidence 0.50
        candidates = qry._pattern_tainted_still_valid(conn, params)
    finally:
        conn.close()

    ids = [c["id"] for c in candidates]
    assert cst_id in ids, (
        "NULL-confidence NCB node (cornerstone) must survive min_confidence filtering"
    )
    assert null_conf_obs_id in ids, (
        "NULL-confidence CB node (observation) must also survive min_confidence filtering"
    )
    assert low_conf_obs_id not in ids, (
        "genuinely low, non-null CB confidence must still be filtered"
    )


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


def test_cornerstone_candidate_excludes_predicted_by_from_citation_count():
    """#2252 W6 design decision (coordinator-ruled, not a judgement call):
    `predicted_by` must NOT count toward cornerstone-candidate cite_count.

    Pins the exclusion so a future 'why isn't predicted_by in this tuple'
    cleanup cannot silently add it back: a prediction-paired observation
    with exactly 2 real citations (below min_citations=3) plus its one
    predicted_by edge must NOT become a candidate (3 total edges would
    cross the threshold if predicted_by counted, but it must not count).
    Adding a 3rd real citation must then cross the threshold on its own,
    proving the pattern still works normally and the miss above wasn't an
    unrelated scoring quirk.
    """
    core = _fresh_mods()
    qry, obs, *_ = _mods()
    now = "2026-08-27T00:00:00Z"

    citer_a = _add_observation(obs, "Citer A for predicted_by-exclusion fixture")
    citer_b = _add_observation(obs, "Citer B for predicted_by-exclusion fixture")
    citer_c = _add_observation(obs, "Citer C for predicted_by-exclusion fixture")

    # The candidate: a predictive observation (mints a paired prediction via
    # a predicted_by edge targeting this node).
    r = json.loads(obs._add_observation_impl(
        url="https://example.com/predicted-by-exclusion",
        title="Predictive source for exclusion fixture",
        quoted_text="G predicts H will occur.",
        interpretation="G predicts H.",
        claim="H will occur.",
        quote_type="hard_data",
        is_predictive=True,
        predicted_event="H occurs within 10 days (predicted_by-exclusion fixture).",
    ))
    assert "error" not in r, r
    node_id = r["observation_id"]
    pred_id = r["prediction_id"]

    conn = core._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_base=2.0, importance_score=2.0 WHERE id=?",
            (node_id,),
        )
        # Exactly 2 real citations — below min_citations=3 on their own.
        for src in (citer_a, citer_b):
            conn.execute(
                "INSERT OR IGNORE INTO edges (source_id, target_id, relation, created_at)"
                " VALUES (?, ?, 'cites', ?)",
                (src, node_id, now),
            )
        conn.commit()
    finally:
        conn.close()

    # Sanity: the predicted_by edge from pred_id -> node_id really exists.
    conn = core._get_db()
    try:
        pairing = conn.execute(
            "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND relation='predicted_by'",
            (pred_id, node_id),
        ).fetchone()
    finally:
        conn.close()
    assert pairing is not None, f"expected a predicted_by edge {pred_id} -> {node_id}"

    conn = core._get_db()
    try:
        params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])
        params["top_k"] = 100
        candidates = qry._pattern_cornerstone_candidate(conn, params)
    finally:
        conn.close()
    result_ids = [c["id"] for c in candidates]
    assert node_id not in result_ids, (
        f"{node_id} has only 2 real citations plus 1 predicted_by edge; "
        f"predicted_by must not count toward cite_count, so this must stay "
        f"below min_citations=3. Got candidates: {result_ids}"
    )

    # Add the 3rd real citation — must now cross the threshold on its own.
    conn = core._get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO edges (source_id, target_id, relation, created_at)"
            " VALUES (?, ?, 'cites', ?)",
            (citer_c, node_id, now),
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
    assert node_id in result_ids, (
        f"{node_id} now has 3 real citations and should cross min_citations=3 "
        f"on its own merit. Got candidates: {result_ids}"
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


def test_open_question_answerable_min_confidence_is_cb_only(monkeypatch):
    """#2252 stage 1.7 regression (site 2, _pattern_open_question_answerable).

    Before the fix, `(m.get("confidence") or 0) >= min_conf` coerced a NULL
    confidence to 0, so a NULL-confidence candidate derivation was silently
    dropped whenever min_confidence > 0 -- the LIVE DEFECT named in the
    issue. This pattern's inner semantic search is hardcoded to
    ``type_filter={"derivation"}`` (derivation is claim-bearing), so an NCB
    node can never appear in ``matches`` here -- there is no NCB case to
    exercise at this site. What the fix does change: a NULL-confidence CB
    candidate (an unresolved derivation) must now survive, while a
    genuinely low, non-null CB confidence must still be filtered.

    core._semantic_search is monkeypatched to return a controlled fixture
    because ENGRAM_NO_EMBEDDINGS=1 (set by _fresh_mods) makes the real
    semantic search always return [] in this test environment.
    """
    core = _fresh_mods()
    qry, obs, rev, drv, epi, tsk, cst, core = _mods()

    q_result = json.loads(drv._ask_impl(
        question="What causes the min_confidence NULL-coercion bug?",
    ))
    q_id = q_result["question_id"]

    null_conf_premise = _add_observation(obs, "Premise for the NULL-confidence derivation")
    low_conf_premise = _add_observation(obs, "Premise for the low-confidence derivation")
    deriv_null = json.loads(drv._derive_impl(
        claim="NULL-confidence CB candidate for the site-2 regression",
        supporting_ids=null_conf_premise,
        logical_chain="Trivial chain for fixture.",
        reasoning_type="deductive_modus_ponens",
    ))["derivation_id"]
    deriv_low = json.loads(drv._derive_impl(
        claim="Low, non-null-confidence CB candidate for the site-2 regression",
        supporting_ids=low_conf_premise,
        logical_chain="Trivial chain for fixture.",
        reasoning_type="deductive_modus_ponens",
    ))["derivation_id"]

    conn = core._get_db()
    try:
        conn.execute("UPDATE nodes SET confidence=NULL WHERE id=?", (deriv_null,))
        conn.execute("UPDATE nodes SET confidence=0.10 WHERE id=?", (deriv_low,))
        conn.commit()
    finally:
        conn.close()

    fake_matches = [
        {"id": deriv_null, "type": "derivation", "claim": "NULL-confidence candidate",
         "confidence": None, "similarity": 0.90},
        {"id": deriv_low, "type": "derivation", "claim": "Low-confidence candidate",
         "confidence": 0.10, "similarity": 0.85},
    ]

    def fake_semantic_search(conn, query_text, top_k=5, min_similarity=0.0,
                              type_filter=None, importance_threshold=0.0,
                              include_superseded=False):
        return list(fake_matches)

    monkeypatch.setattr(core, "_semantic_search", fake_semantic_search)

    conn = core._get_db()
    try:
        params = dict(qry.PATTERN_QUERY_PRESETS["balanced"])  # min_confidence 0.50
        candidates = qry._pattern_open_question_answerable(conn, params)
    finally:
        conn.close()

    cand = next(c for c in candidates if c["id"] == q_id)
    assert cand["candidate_resolution"]["id"] == deriv_null, (
        "NULL-confidence CB candidate must survive min_confidence filtering"
    )
    alt_ids = [a["id"] for a in cand["alternates"]]
    assert deriv_low not in alt_ids, (
        "genuinely low, non-null CB confidence must still be filtered"
    )


def test_recent_resolution_echo_dispatches_cleanly():
    _fresh_mods()
    qry, *_ = _mods()
    r = json.loads(qry._query_pattern_impl(
        pattern_name="recent_resolution_echo", preset="balanced",
    ))
    assert "results" in r
    assert "total_matches" in r
    assert isinstance(r["results"], list)


# ── min_confidence CB-only predicate, main hybrid-search path ─────────────
# (#2252 stage 1.7, site 1 -- engram_query._search_nodes, the plumbing behind
# query_hybrid / engram_recall's main path, not a _pattern_* function. Kept
# in this file per the spec's "extend rather than create a parallel file"
# instruction, since this file already carries the family-module fixtures
# and the sibling min_confidence regression tests for sites 2/3 above.)


def test_search_nodes_min_confidence_is_cb_only():
    """#2252 stage 1.7 regression (site 1, FTS branch of _search_nodes).

    This site never had the NULL-coerces-to-zero defect (`r["confidence"]
    is not None and ... < min_confidence` already kept NULL) -- so this is
    a no-regression check: the CB-only gate must preserve that NULL is kept
    for an NCB node (goal), not incidentally narrow it. A genuinely low,
    non-null CB confidence must still be filtered, matching pre-fix
    behavior for that case.
    """
    core = _fresh_mods()
    qry, obs, rev, drv, epi, tsk, cst, core = _mods()

    goal_result = json.loads(tsk._add_goal_impl(
        claim="Xenoglot calibration goal for min_confidence regression",
        motivation="Testing #2252 stage 1.7",
    ))
    goal_id = goal_result["goal_id"]

    low_conf_obs_id = _add_observation(obs, "Xenoglot calibration observation with low confidence")

    conn = core._get_db()
    try:
        row = conn.execute("SELECT confidence FROM nodes WHERE id=?", (goal_id,)).fetchone()
        assert row["confidence"] is None  # sanity: goals never carry confidence

        conn.execute("UPDATE nodes SET confidence=0.10 WHERE id=?", (low_conf_obs_id,))
        conn.commit()

        result = qry._search_nodes(
            conn,
            query="Xenoglot calibration",
            use_semantic=False,
            top_k=10,
            min_confidence=0.90,
        )
    finally:
        conn.close()

    ids = [n["id"] for n in result["results"]]
    assert goal_id in ids, (
        "NULL-confidence NCB node (goal) must survive min_confidence filtering"
    )
    assert low_conf_obs_id not in ids, (
        "genuinely low, non-null CB confidence must still be filtered"
    )


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
