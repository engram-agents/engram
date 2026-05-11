"""Tests for engram_query_pattern (KnowQL-inspired pattern queries).

v0.1 ships:
  - The dispatcher framework (preset bundles, override args, telemetry logging,
    error paths for unknown pattern_name / preset).
  - One starter pattern: contradiction_obsolescence_ready.

Coverage:
  1. Unknown pattern_name returns error JSON.
  2. Unknown preset returns error JSON.
  3. Preset bundle is correctly resolved + overridable.
  4. contradiction_obsolescence_ready: scores active contradictions correctly
     (one side obsolete = 1.0; both obsolete = 0.5; neither obsolete = 0.0).
  5. Telemetry log appends one entry per call (regardless of candidate count).
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_query_pattern")


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
    r = json.loads(server.engram_query_pattern("nonexistent_pattern"))
    assert "error" in r
    assert "nonexistent_pattern" in r["error"]
    assert "contradiction_obsolescence_ready" in r["error"]


def test_unknown_preset_errors():
    server = fresh_server()
    r = json.loads(server.engram_query_pattern(
        "contradiction_obsolescence_ready", preset="ultra_precision"
    ))
    assert "error" in r
    assert "ultra_precision" in r["error"]


def test_preset_bundle_resolution():
    server = fresh_server()
    # Empty graph: no candidates, but params_used should reflect the preset
    r = json.loads(server.engram_query_pattern(
        "contradiction_obsolescence_ready", preset="high_precision"
    ))
    assert r["preset"] == "high_precision"
    assert r["params_used"]["top_k"] == 5
    # cosine_threshold recalibrated 2026-05-11 (PR #29 placeholder 0.80 →
    # principled 0.65 = balanced + 0.10 = upper edge of V4 useful range).
    # See active-work/query-pattern-preset-rationale-2026-05-11.md.
    assert r["params_used"]["cosine_threshold"] == 0.65


def test_preset_overrides_apply():
    server = fresh_server()
    r = json.loads(server.engram_query_pattern(
        "contradiction_obsolescence_ready",
        preset="balanced",
        top_k_override=42,
        cosine_threshold_override=0.10,
    ))
    assert r["params_used"]["top_k"] == 42
    assert r["params_used"]["cosine_threshold"] == 0.10
    # min_confidence not overridden, so should fall through to balanced default
    assert r["params_used"]["min_confidence"] == 0.50


def test_contradiction_one_side_obsolete_scores_high():
    server = fresh_server()
    ct_id, a_id, b_id = _add_active_contradiction(
        server, "X is true", "X is false"
    )
    # Retract side A — now side B survives
    server._retract_impl(
        node_id=a_id, error_type="other", reason="test fixture: simulating retraction"
    )

    r = json.loads(server.engram_query_pattern(
        "contradiction_obsolescence_ready", preset="balanced"
    ))
    assert r["candidate_count"] == 1
    cand = r["candidates"][0]
    assert cand["id"] == ct_id
    assert cand["score"] == 1.0
    assert cand["obsolete_side"] == "a"
    assert cand["surviving_side"] == "b"
    assert "engram_resolve" in cand["suggested_action"]


def test_contradiction_both_sides_obsolete_scores_medium():
    server = fresh_server()
    ct_id, a_id, b_id = _add_active_contradiction(
        server, "Y holds at noon", "Y does not hold at noon"
    )
    server._retract_impl(node_id=a_id, error_type="other", reason="fixture")
    server._retract_impl(node_id=b_id, error_type="other", reason="fixture")

    # Score is 0.5 — below balanced threshold (0.55), at-or-above high_recall (0.45).
    # Confirms: high_recall surfaces the both-obsolete case for cleanup; balanced
    # filters it as a less-clean candidate than the one-side case.
    r = json.loads(server.engram_query_pattern(
        "contradiction_obsolescence_ready", preset="high_recall"
    ))
    assert r["candidate_count"] == 1
    cand = r["candidates"][0]
    assert cand["score"] == 0.5
    assert cand["obsolete_side"] == "both"
    assert cand["surviving_side"] is None

    # Balanced should NOT surface this case (verifies threshold semantics).
    r2 = json.loads(server.engram_query_pattern(
        "contradiction_obsolescence_ready", preset="balanced"
    ))
    assert r2["candidate_count"] == 0


def test_contradiction_neither_obsolete_filtered_at_balanced_threshold():
    server = fresh_server()
    _add_active_contradiction(server, "Z is alpha", "Z is beta")
    # Both sides current; score = 0.0; balanced threshold = 0.55 → filtered out

    r = json.loads(server.engram_query_pattern(
        "contradiction_obsolescence_ready", preset="balanced"
    ))
    assert r["candidate_count"] == 0


def test_contradiction_neither_obsolete_visible_at_high_recall():
    server = fresh_server()
    # high_recall threshold is 0.45, score=0.0 still filtered. Confirm explicit override admits it.
    _add_active_contradiction(server, "W is square", "W is round")

    r = json.loads(server.engram_query_pattern(
        "contradiction_obsolescence_ready",
        preset="high_recall",
        cosine_threshold_override=0.0,
    ))
    assert r["candidate_count"] == 1
    assert r["candidates"][0]["score"] == 0.0


# ── stale_load_bearing ────────────────────────────────────────────────────


def test_stale_load_bearing_surfaces_high_importance_low_recall():
    server = fresh_server()
    # Add an observation; bump its importance manually via direct SQL since
    # engram_focus / cornerstone bumps are tested elsewhere.
    obs_id = _add_observation(server, "Important standalone claim about X")
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_score=2.5, recall_count=0 WHERE id=?",
            (obs_id,),
        )
        conn.commit()
    finally:
        conn.close()

    r = json.loads(server.engram_query_pattern(
        "stale_load_bearing", preset="balanced"
    ))
    ids = [c["id"] for c in r["candidates"]]
    assert obs_id in ids
    cand = next(c for c in r["candidates"] if c["id"] == obs_id)
    assert cand["importance"] >= 2.5
    assert cand["recall_count"] == 0


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
    r = json.loads(server.engram_query_pattern(
        "stale_load_bearing", preset="high_recall", top_k_override=100
    ))
    ids = [c["id"] for c in r["candidates"]]
    assert cs_id not in ids


# ── tainted_still_valid ───────────────────────────────────────────────────


def test_tainted_still_valid_surfaces_tainted_nodes():
    server = fresh_server()
    # Create a derivation by adding two observations + deriving from them
    obs_a = _add_observation(server, "Premise A holds in scenario 1")
    obs_b = _add_observation(server, "Premise B holds in scenario 1")
    deriv_result = json.loads(server.engram_derive(
        claim="Combined: A ∧ B implies conclusion C",
        supporting_ids=f"{obs_a},{obs_b}",
        logical_chain="From A and B, C follows by conjunction.",
        reasoning_type="deductive_modus_ponens",
    ))
    deriv_id = deriv_result["derivation_id"]

    # Retract one of the premises — cascade should taint the derivation
    server._retract_impl(
        node_id=obs_a, error_type="hallucinated_claim",
        reason="test fixture: simulating premise retraction"
    )

    r = json.loads(server.engram_query_pattern(
        "tainted_still_valid", preset="balanced"
    ))
    ids = [c["id"] for c in r["candidates"]]
    assert deriv_id in ids
    cand = next(c for c in r["candidates"] if c["id"] == deriv_id)
    assert cand["score"] > 0
    assert cand["confidence"] > 0


# ── cornerstone_candidate ─────────────────────────────────────────────────


def test_cornerstone_candidate_requires_min_citations():
    server = fresh_server()
    # An observation with high importance but no citations — should NOT
    # surface as a cornerstone candidate.
    isolated_id = _add_observation(server, "Isolated high-importance claim")
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_score=3.0 WHERE id=?", (isolated_id,)
        )
        conn.commit()
    finally:
        conn.close()
    r = json.loads(server.engram_query_pattern(
        "cornerstone_candidate", preset="balanced"
    ))
    ids = [c["id"] for c in r["candidates"]]
    assert isolated_id not in ids  # zero citations < min_citations=3


# ── open_question_answerable + recent_resolution_echo ─────────────────────
# Skipped from full coverage in this PR — they need semantic search via the
# embedder, which requires a model load. Smoke-test the dispatch only.


def test_open_question_answerable_dispatches_cleanly():
    server = fresh_server()
    r = json.loads(server.engram_query_pattern(
        "open_question_answerable", preset="balanced"
    ))
    assert "candidates" in r
    assert "candidate_count" in r
    assert isinstance(r["candidates"], list)


def test_recent_resolution_echo_dispatches_cleanly():
    server = fresh_server()
    r = json.loads(server.engram_query_pattern(
        "recent_resolution_echo", preset="balanced"
    ))
    assert "candidates" in r
    assert "candidate_count" in r
    assert isinstance(r["candidates"], list)


def test_telemetry_log_writes_entry_per_call():
    server = fresh_server()
    log_path = os.path.join(TEST_DIR, "pattern_query_telemetry.jsonl")
    # Clear prior entries from the same test dir
    if os.path.exists(log_path):
        os.remove(log_path)

    json.loads(server.engram_query_pattern("contradiction_obsolescence_ready"))
    json.loads(server.engram_query_pattern("contradiction_obsolescence_ready", preset="high_precision"))

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
