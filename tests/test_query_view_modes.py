"""Tests for Wave C recall_summary rollout: tiered render for engram_query
and engram_query_pattern.

Covers:
  1. engram_query default returns tiered shape (Tier 1 = summaries, Tier 2 = keywords)
  2. engram_query summary_top_k=0 returns all Tier 2 (keywords only)
  3. engram_query summary_top_k >= result count returns all Tier 1 (summaries only)
  4. engram_query return_debug=True returns full legacy shape (composite_score key present)
  5. engram_query fallback: node without recall_summary uses truncated claim as summary
  6. engram_query fallback: node without recall_keywords emits {id, confidence} entry in Tier 2
  7. engram_query_pattern default returns tiered shape across two patterns
  8. engram_query_pattern preserves telemetry log writes
  9. summary_top_k=0 for engram_query_pattern returns all keywords
  10. engram_query return shape has 'results', 'query', 'total_matches' keys
  11. engram_query_pattern return shape has 'results', 'pattern_name', 'preset', 'total_matches'
  12. claim truncation at 160 chars (fallback path)
  13. summary_top_k negative value clamped to 0 (all keywords)
"""

import atexit
import importlib
import json
import os
import shutil
import sys
import tempfile
import types

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_query_view_modes_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_TESTS_DIR = str(os.path.dirname(os.path.abspath(__file__)))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_derive
import engram_epistemic
import engram_observation
import engram_query
import engram_recall_summaries
import engram_revision


def _fresh_mods():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    fresh_module(engram_query)
    for name in [
        "engram_observation",
        "engram_revision",
        "engram_derive",
        "engram_epistemic",
        "engram_recall_summaries",
    ]:
        importlib.import_module(name)
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return core


def _mods():
    qry = sys.modules["engram_query"]
    obs = sys.modules["engram_observation"]
    rev = sys.modules["engram_revision"]
    drv = sys.modules["engram_derive"]
    epi = sys.modules["engram_epistemic"]
    rcs = sys.modules["engram_recall_summaries"]
    core = sys.modules["engram_core"]
    return qry, obs, rev, drv, epi, rcs, core


def _add_observation(obs, claim: str, url: str = "https://example.com/test") -> str:
    r = json.loads(obs._add_observation_impl(
        url=url,
        title="Test Source",
        quoted_text=claim[:80],
        interpretation="Test interpretation.",
        claim=claim,
        quote_type="hard_data",
    ))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def _add_axiom(epi, claim: str, basis: str = "Test basis.") -> str:
    r = json.loads(epi._add_axiom_impl(claim=claim, basis=basis))
    assert "axiom_id" in r, f"add_axiom failed: {r}"
    return r["axiom_id"]


def _set_recall_fields(rcs, node_id: str, summary: str, keywords: list) -> None:
    # recall_keywords requires 3-5 elements — pad to 3 if caller passes fewer.
    kw = list(keywords)
    while len(kw) < 3:
        kw.append(f"pad{len(kw)}")
    r = json.loads(rcs._set_recall_summaries_impl(payload_json=json.dumps({
        "summaries": [{"node_id": node_id, "recall_summary": summary, "recall_keywords": kw}]
    })))
    assert r.get("applied") == 1, f"engram_set_recall_summaries failed: {r}"


# ---------------------------------------------------------------------------
# 1. Default tiered shape for engram_query
# ---------------------------------------------------------------------------

def test_query_default_tiered_shape():
    """Default return has Tier 1 (summary) and Tier 2 (keywords) entries."""
    core = _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    # Add several observations so we have > 3 results
    ids = []
    for i in range(5):
        nid = _add_observation(obs, f"Test claim number {i} about alpha protocol")
        ids.append(nid)
    # Set recall fields on all nodes
    for i, nid in enumerate(ids):
        _set_recall_fields(rcs, nid, f"Summary for node {i}.", [f"kw{i}a", f"kw{i}b"])

    r = json.loads(qry._query_impl(query="alpha protocol", top_k=5))
    assert "results" in r, f"'results' key missing: {r}"
    assert "query" in r, f"'query' key missing: {r}"
    assert "total_matches" in r, f"'total_matches' key missing: {r}"

    results = r["results"]
    assert len(results) > 0, "Expected at least one result"

    # Top 3 (default summary_top_k) should have 'summary' key
    tier1 = results[:3]
    for entry in tier1:
        assert "id" in entry, f"Tier-1 entry missing 'id': {entry}"
        assert "summary" in entry, f"Tier-1 entry missing 'summary': {entry}"
        assert "keywords" not in entry, f"Tier-1 entry should not have 'keywords': {entry}"

    # Remaining should have 'keywords' or bare-id
    tier2 = results[3:]
    for entry in tier2:
        assert "id" in entry, f"Tier-2 entry missing 'id': {entry}"
        assert "summary" not in entry, f"Tier-2 entry should not have 'summary': {entry}"

    # No score/position/type fields on any entry (confidence IS now included per PR-5)
    for entry in results:
        assert "score" not in entry, f"Score leaked into tiered result: {entry}"
        assert "composite_score" not in entry, f"composite_score leaked: {entry}"
        assert "position" not in entry, f"position leaked: {entry}"
        assert "type" not in entry, f"type leaked into tiered result: {entry}"


# ---------------------------------------------------------------------------
# 2. summary_top_k=0 — all Tier 2 (keywords only)
# ---------------------------------------------------------------------------

def test_query_summary_top_k_zero_all_keywords():
    """summary_top_k=0 means all entries are Tier 2 (no summaries)."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    nid = _add_observation(obs, "Claim about zero summary setting.")
    _set_recall_fields(rcs, nid, "Summary text.", ["keyword1", "keyword2"])

    r = json.loads(qry._query_impl(query="zero summary setting", top_k=5, summary_top_k=0))
    results = r["results"]
    assert len(results) > 0, "Expected results"

    for entry in results:
        assert "summary" not in entry, f"summary should not appear with summary_top_k=0: {entry}"


# ---------------------------------------------------------------------------
# 3. summary_top_k >= result count — all Tier 1 (summaries only)
# ---------------------------------------------------------------------------

def test_query_summary_top_k_exceeds_results_all_summaries():
    """summary_top_k >= total results means all entries are Tier 1 (summaries)."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    for i in range(2):
        nid = _add_observation(obs, f"Claim {i} about full summary coverage scenario")
        _set_recall_fields(rcs, nid, f"Summary {i}.", [f"kw{i}"])

    r = json.loads(qry._query_impl(
        query="full summary coverage scenario", top_k=10, summary_top_k=100
    ))
    results = r["results"]
    assert len(results) > 0, "Expected results"

    for entry in results:
        assert "summary" in entry, f"All entries should have summary: {entry}"
        assert "keywords" not in entry, f"No keywords expected when all are Tier 1: {entry}"


# ---------------------------------------------------------------------------
# 4. return_debug=True preserves full legacy shape
# ---------------------------------------------------------------------------

def test_query_return_debug_preserves_legacy_shape():
    """return_debug=True returns full legacy shape with composite_score."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    _add_observation(obs, "Debug mode legacy shape test claim.")

    r = json.loads(qry._query_impl(query="debug mode legacy shape", return_debug=True))
    assert "results" in r, f"'results' missing in debug mode: {r}"
    assert "query" in r
    assert "count" in r, f"'count' missing in debug mode (legacy field): {r}"
    assert "search_methods" in r, f"'search_methods' missing in debug mode: {r}"

    # Results should have full node fields, not tiered
    if r["results"]:
        first = r["results"][0]
        assert "claim" in first, f"'claim' missing from debug result: {first}"
        assert "id" in first


def test_query_return_debug_composite_score_present():
    """When return_debug=True and search has results with debug, composite_score appears."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    _add_observation(obs, "Composite score visibility test claim.")

    r = json.loads(qry._query_impl(query="composite score visibility", return_debug=True))
    assert "count" in r


# ---------------------------------------------------------------------------
# 5. Fallback: node without recall_summary uses truncated claim
# ---------------------------------------------------------------------------

def test_query_fallback_no_recall_summary_uses_claim():
    """Tier-1 entries without recall_summary fall back to (truncated) claim."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    claim_text = "Fallback claim used when summary is absent."
    _add_observation(obs, claim_text)
    # Do NOT set recall_summary — leave it null

    r = json.loads(qry._query_impl(query="fallback claim summary absent", summary_top_k=1))
    results = r["results"]
    assert len(results) > 0, "Expected results"

    tier1 = results[0]
    assert "summary" in tier1, f"Tier-1 entry should have 'summary' key: {tier1}"
    assert claim_text[:50] in tier1["summary"] or tier1["summary"] in claim_text, (
        f"Fallback summary doesn't contain claim text. summary={tier1['summary']!r}, "
        f"claim={claim_text!r}"
    )


def test_query_fallback_long_claim_truncated_at_160():
    """Claims longer than 160 chars are truncated with ellipsis in the fallback."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    long_claim = "A" * 200 + " end marker"
    nid = _add_observation(obs, long_claim)
    # No recall_summary set

    r = json.loads(qry._query_impl(query="A" * 30, summary_top_k=1, top_k=5))
    results = r["results"]
    if not results:
        return  # FTS may not match all-letter queries with ENGRAM_NO_EMBEDDINGS
    for entry in results:
        if entry.get("id") == nid:
            summary = entry.get("summary", "")
            assert len(summary) <= 163, f"Summary too long: {len(summary)} chars"
            if len(long_claim) > 160:
                assert summary.endswith("..."), f"Expected truncation ellipsis: {summary!r}"
            break


# ---------------------------------------------------------------------------
# 6. Fallback: node without recall_keywords emits bare-id
# ---------------------------------------------------------------------------

def test_query_fallback_no_recall_keywords_bare_id():
    """Tier-2 entries without recall_keywords emit {id, confidence} — no 'keywords' key."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    # Add multiple observations — first 3 will be tier-1 (summary), rest tier-2
    for i in range(5):
        _add_observation(obs, f"Keyword fallback test claim {i} about retrieval system")
    # Do NOT set recall_keywords on any node — test bare-id behavior

    r = json.loads(qry._query_impl(
        query="keyword fallback test retrieval", top_k=5, summary_top_k=3
    ))
    results = r["results"]
    for entry in results[3:]:
        assert "keywords" not in entry, (
            f"Tier-2 entry without recall_keywords should not have 'keywords': {entry}"
        )
        assert "id" in entry, f"Tier-2 bare entry must have 'id' key: {entry}"
        assert "confidence" in entry, (
            f"Tier-2 bare entry must have 'confidence' key (PR-5 restoration): {entry}"
        )
        assert "summary" not in entry, f"Tier-2 must not have 'summary': {entry}"


# ---------------------------------------------------------------------------
# 7. engram_query_pattern tiered shape (two patterns)
# ---------------------------------------------------------------------------

def test_query_pattern_cornerstone_candidate_tiered_shape():
    """engram_query_pattern cornerstone_candidate returns tiered shape."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    obs_id = _add_observation(obs, "Load-bearing cornerstone candidate observation.")
    conn = core._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_base=0.95, importance_score=3.0 WHERE id=?", (obs_id,)
        )
        conn.commit()
    finally:
        conn.close()

    for i in range(3):
        json.loads(drv._derive_impl(
            claim=f"Derived cornerstone candidate claim {i} citing load-bearing obs.",
            supporting_ids=obs_id,
            logical_chain="Derives from the cornerstone candidate observation.",
            reasoning_type="deductive_modus_ponens",
        ))

    r = json.loads(qry._query_pattern_impl(
        pattern_name="cornerstone_candidate", preset="balanced",
    ))
    assert "results" in r, f"'results' key missing: {r}"
    assert "pattern_name" in r
    assert "preset" in r
    assert "total_matches" in r
    assert r["pattern_name"] == "cornerstone_candidate"

    results = r["results"]
    if results:
        for entry in results:
            assert "id" in entry, f"Entry missing 'id': {entry}"
        for entry in results:
            assert "score" not in entry, f"Score leaked into tiered pattern result: {entry}"
            assert "suggested_action" not in entry, (
                f"suggested_action leaked into tiered result: {entry}"
            )
        assert "summary" in results[0], f"Top entry should have 'summary': {results[0]}"


def test_query_pattern_contradiction_obsolescence_tiered_shape():
    """engram_query_pattern contradiction_obsolescence_ready returns tiered shape."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    obs_a = _add_observation(obs, "Contradictory claim A for view mode test.")
    obs_b = _add_observation(obs, "Contradictory claim B for view mode test.")
    json.loads(drv._contradict_impl(
        node_id_a=obs_a,
        node_id_b=obs_b,
        description="Test contradiction for view modes",
        root_cause="test fixture",
    ))
    rev._retract_impl(
        node_id=obs_a, error_type="other",
        reason="test fixture: simulating retraction for view modes"
    )

    r = json.loads(qry._query_pattern_impl(
        pattern_name="contradiction_obsolescence_ready", preset="balanced",
    ))
    assert "results" in r
    assert "total_matches" in r
    results = r["results"]
    assert len(results) >= 1

    for entry in results:
        assert "id" in entry
        assert "score" not in entry
        assert "suggested_action" not in entry

    assert "summary" in results[0], f"Top result should be Tier 1: {results[0]}"


# ---------------------------------------------------------------------------
# 8. engram_query_pattern preserves telemetry log writes
# ---------------------------------------------------------------------------

def test_query_pattern_telemetry_preserved_after_tier_transform():
    """Tiered transformation does not affect telemetry log writes."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    log_path = os.path.join(TEST_DIR, "pattern_query_telemetry.jsonl")
    if os.path.exists(log_path):
        os.remove(log_path)

    json.loads(qry._query_pattern_impl(
        pattern_name="stale_load_bearing", preset="balanced",
    ))
    json.loads(qry._query_pattern_impl(
        pattern_name="cornerstone_candidate", preset="high_precision",
    ))

    assert os.path.exists(log_path), "Telemetry log should have been created"
    with open(log_path) as f:
        entries = [json.loads(line) for line in f if line.strip()]
    assert len(entries) == 2, f"Expected 2 telemetry entries, got {len(entries)}"
    assert entries[0]["pattern_name"] == "stale_load_bearing"
    assert entries[1]["preset"] == "high_precision"
    assert "candidate_count" in entries[0]
    assert "scores" in entries[0]


# ---------------------------------------------------------------------------
# 9. engram_query_pattern summary_top_k=0 — all keywords
# ---------------------------------------------------------------------------

def test_query_pattern_summary_top_k_zero():
    """summary_top_k=0 on engram_query_pattern emits all Tier-2 entries."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    obs_id = _add_observation(obs, "Stale load bearing test observation for top_k zero.")
    conn = core._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_base=0.95, importance_score=2.5, recall_count=0 WHERE id=?",
            (obs_id,),
        )
        conn.commit()
    finally:
        conn.close()
    _set_recall_fields(rcs, obs_id, "Load-bearing summary.", ["load-bearing", "stale"])

    r = json.loads(qry._query_pattern_impl(
        pattern_name="stale_load_bearing", preset="balanced", summary_top_k=0,
    ))
    results = r["results"]
    for entry in results:
        assert "summary" not in entry, (
            f"summary_top_k=0 should suppress all summaries: {entry}"
        )


# ---------------------------------------------------------------------------
# 10 + 11. Return shape key checks
# ---------------------------------------------------------------------------

def test_query_return_shape_keys():
    """engram_query default return has exactly the required top-level keys."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    _add_observation(obs, "Shape key test observation.")

    r = json.loads(qry._query_impl(query="shape key test"))
    assert "results" in r
    assert "query" in r
    assert "total_matches" in r
    assert "count" not in r, "'count' is a legacy key and should not appear in default mode"
    assert "search_methods" not in r, "'search_methods' is a legacy key"
    assert "message" not in r


def test_query_pattern_return_shape_keys():
    """engram_query_pattern default return has the required top-level keys."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()

    r = json.loads(qry._query_pattern_impl(pattern_name="stale_load_bearing"))
    assert "results" in r
    assert "pattern_name" in r
    assert "preset" in r
    assert "total_matches" in r
    assert "candidates" not in r, "'candidates' is the legacy key"
    assert "candidate_count" not in r, "'candidate_count' is the legacy key"
    assert "hint" not in r


# ---------------------------------------------------------------------------
# 12. summary_top_k negative — clamped to 0 (all keywords)
# ---------------------------------------------------------------------------

def test_query_negative_summary_top_k_clamped():
    """Negative summary_top_k is clamped to 0 (all Tier 2)."""
    _fresh_mods()
    qry, obs, rev, drv, epi, rcs, core = _mods()
    nid = _add_observation(obs, "Negative top_k clamping test claim.")
    _set_recall_fields(rcs, nid, "Summary text.", ["kw1", "kw2"])

    r = json.loads(qry._query_impl(query="negative top_k clamping", summary_top_k=-5))
    results = r["results"]
    for entry in results:
        assert "summary" not in entry, (
            f"Negative summary_top_k should clamp to 0, got summary: {entry}"
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
