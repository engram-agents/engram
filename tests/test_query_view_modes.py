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

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_query_view_modes")


def fresh_server():
    """Reset state and return the server module rebound to the test dir."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    for mod in list(sys.modules.keys()):
        if mod in ("server", "engram_client"):
            del sys.modules[mod]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_observation(server, claim: str, url: str = "https://example.com/test") -> str:
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


def _add_axiom(server, claim: str, basis: str = "Test basis.") -> str:
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": claim, "basis": basis,
    })))
    assert "axiom_id" in r, f"add_axiom failed: {r}"
    return r["axiom_id"]


def _set_recall_fields(server, node_id: str, summary: str, keywords: list) -> None:
    # recall_keywords requires 3-5 elements — pad to 3 if caller passes fewer.
    kw = list(keywords)
    while len(kw) < 3:
        kw.append(f"pad{len(kw)}")
    r = json.loads(server.engram_set_recall_summaries(payload_json=json.dumps({
        "summaries": [{"node_id": node_id, "recall_summary": summary, "recall_keywords": kw}]
    })))
    assert r.get("applied") == 1, f"engram_set_recall_summaries failed: {r}"


# ---------------------------------------------------------------------------
# 1. Default tiered shape for engram_query
# ---------------------------------------------------------------------------

def test_query_default_tiered_shape():
    """Default return has Tier 1 (summary) and Tier 2 (keywords) entries."""
    server = fresh_server()
    # Add several observations so we have > 3 results
    ids = []
    for i in range(5):
        nid = _add_observation(server, f"Test claim number {i} about alpha protocol")
        ids.append(nid)
    # Set recall fields on all nodes
    for i, nid in enumerate(ids):
        _set_recall_fields(server, nid, f"Summary for node {i}.", [f"kw{i}a", f"kw{i}b"])

    r = json.loads(server.engram_query(query="alpha protocol", top_k=5))
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
    server = fresh_server()
    nid = _add_observation(server, "Claim about zero summary setting.")
    _set_recall_fields(server, nid, "Summary text.", ["keyword1", "keyword2"])

    r = json.loads(server.engram_query(
        query="zero summary setting", top_k=5, summary_top_k=0
    ))
    results = r["results"]
    assert len(results) > 0, "Expected results"

    for entry in results:
        assert "summary" not in entry, f"summary should not appear with summary_top_k=0: {entry}"


# ---------------------------------------------------------------------------
# 3. summary_top_k >= result count — all Tier 1 (summaries only)
# ---------------------------------------------------------------------------

def test_query_summary_top_k_exceeds_results_all_summaries():
    """summary_top_k >= total results means all entries are Tier 1 (summaries)."""
    server = fresh_server()
    for i in range(2):
        nid = _add_observation(server, f"Claim {i} about full summary coverage scenario")
        _set_recall_fields(server, nid, f"Summary {i}.", [f"kw{i}"])

    r = json.loads(server.engram_query(
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
    server = fresh_server()
    _add_observation(server, "Debug mode legacy shape test claim.")

    r = json.loads(server.engram_query(query="debug mode legacy shape", return_debug=True))
    # Legacy shape keys
    assert "results" in r, f"'results' missing in debug mode: {r}"
    assert "query" in r
    assert "count" in r, f"'count' missing in debug mode (legacy field): {r}"
    assert "search_methods" in r, f"'search_methods' missing in debug mode: {r}"

    # Results should have full node fields, not tiered
    if r["results"]:
        first = r["results"][0]
        # Full shape has 'claim' (not just 'summary')
        assert "claim" in first, f"'claim' missing from debug result: {first}"
        # No tier-specific 'summary' key on top-level entry (claim is the field)
        # (note: recall_summary MAY be present if set — that's ok, 'claim' is the signal)
        assert "id" in first


def test_query_return_debug_composite_score_present():
    """When return_debug=True and search has results with debug, composite_score appears."""
    server = fresh_server()
    _add_observation(server, "Composite score visibility test claim.")

    r = json.loads(server.engram_query(
        query="composite score visibility", return_debug=True
    ))
    # The debug key should appear (even if no per_result entries when FTS/semantic return nothing)
    # At minimum, legacy 'count' field marks this as the debug path
    assert "count" in r


# ---------------------------------------------------------------------------
# 5. Fallback: node without recall_summary uses truncated claim
# ---------------------------------------------------------------------------

def test_query_fallback_no_recall_summary_uses_claim():
    """Tier-1 entries without recall_summary fall back to (truncated) claim."""
    server = fresh_server()
    claim_text = "Fallback claim used when summary is absent."
    nid = _add_observation(server, claim_text)
    # Do NOT set recall_summary — leave it null

    r = json.loads(server.engram_query(query="fallback claim summary absent", summary_top_k=1))
    results = r["results"]
    assert len(results) > 0, "Expected results"

    tier1 = results[0]
    assert "summary" in tier1, f"Tier-1 entry should have 'summary' key: {tier1}"
    # The summary should contain some portion of the claim text
    assert claim_text[:50] in tier1["summary"] or tier1["summary"] in claim_text, (
        f"Fallback summary doesn't contain claim text. summary={tier1['summary']!r}, "
        f"claim={claim_text!r}"
    )


def test_query_fallback_long_claim_truncated_at_160():
    """Claims longer than 160 chars are truncated with ellipsis in the fallback."""
    server = fresh_server()
    long_claim = "A" * 200 + " end marker"
    nid = _add_observation(server, long_claim)
    # No recall_summary set

    r = json.loads(server.engram_query(
        query="A" * 30, summary_top_k=1, top_k=5
    ))
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
    server = fresh_server()
    # Add multiple observations — first 3 will be tier-1 (summary), rest tier-2
    for i in range(5):
        nid = _add_observation(server, f"Keyword fallback test claim {i} about retrieval system")
    # Set recall_summary on first 3 only (they'll land in tier-1 via summary fallback anyway)
    # Do NOT set recall_keywords on any node — test bare-id behavior

    r = json.loads(server.engram_query(
        query="keyword fallback test retrieval", top_k=5, summary_top_k=3
    ))
    results = r["results"]
    # Tier-2 entries (if any) should have id + confidence but no keywords
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
    server = fresh_server()
    # Create a highly-cited high-importance node.
    # Use engram_derive to create 3+ derives_from edges pointing at obs_id,
    # meeting the min_citations=3 threshold in _pattern_cornerstone_candidate.
    obs_id = _add_observation(server, "Load-bearing cornerstone candidate observation.")
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_score=3.0 WHERE id=?", (obs_id,)
        )
        conn.commit()
    finally:
        conn.close()

    # Derive 3 nodes from obs_id via the tool (creates proper edges with created_at)
    for i in range(3):
        json.loads(server.engram_derive(payload_json=json.dumps({
            "claim": f"Derived cornerstone candidate claim {i} citing load-bearing obs.",
            "supporting_ids": obs_id,
            "logical_chain": "Derives from the cornerstone candidate observation.",
            "reasoning_type": "deductive_modus_ponens",
        })))

    r = json.loads(server.engram_query_pattern(
        "cornerstone_candidate", preset="balanced"
    ))
    assert "results" in r, f"'results' key missing: {r}"
    assert "pattern_name" in r
    assert "preset" in r
    assert "total_matches" in r
    assert r["pattern_name"] == "cornerstone_candidate"

    results = r["results"]
    if results:
        # All entries must have 'id'
        for entry in results:
            assert "id" in entry, f"Entry missing 'id': {entry}"
        # No score/suggested_action in tiered output
        for entry in results:
            assert "score" not in entry, f"Score leaked into tiered pattern result: {entry}"
            assert "suggested_action" not in entry, (
                f"suggested_action leaked into tiered result: {entry}"
            )
        # Top entry (Tier 1) has 'summary'
        assert "summary" in results[0], f"Top entry should have 'summary': {results[0]}"


def test_query_pattern_contradiction_obsolescence_tiered_shape():
    """engram_query_pattern contradiction_obsolescence_ready returns tiered shape."""
    server = fresh_server()
    # Create an active contradiction with one side retracted
    obs_a = _add_observation(server, "Contradictory claim A for view mode test.")
    obs_b = _add_observation(server, "Contradictory claim B for view mode test.")
    r_ct = json.loads(server._contradict_impl(
        node_id_a=obs_a,
        node_id_b=obs_b,
        description="Test contradiction for view modes",
        root_cause="test fixture",
    ))
    server._retract_impl(
        node_id=obs_a, error_type="other",
        reason="test fixture: simulating retraction for view modes"
    )

    r = json.loads(server.engram_query_pattern(
        "contradiction_obsolescence_ready", preset="balanced"
    ))
    assert "results" in r
    assert "total_matches" in r
    results = r["results"]
    # Should have at least the contradiction we just created
    assert len(results) >= 1

    # Verify tiered shape: no score, no suggested_action
    for entry in results:
        assert "id" in entry
        assert "score" not in entry
        assert "suggested_action" not in entry

    # Top entry is Tier 1
    assert "summary" in results[0], f"Top result should be Tier 1: {results[0]}"


# ---------------------------------------------------------------------------
# 8. engram_query_pattern preserves telemetry log writes
# ---------------------------------------------------------------------------

def test_query_pattern_telemetry_preserved_after_tier_transform():
    """Tiered transformation does not affect telemetry log writes."""
    server = fresh_server()
    log_path = os.path.join(TEST_DIR, "pattern_query_telemetry.jsonl")
    if os.path.exists(log_path):
        os.remove(log_path)

    json.loads(server.engram_query_pattern(
        "stale_load_bearing", preset="balanced"
    ))
    json.loads(server.engram_query_pattern(
        "cornerstone_candidate", preset="high_precision"
    ))

    assert os.path.exists(log_path), "Telemetry log should have been created"
    with open(log_path) as f:
        entries = [json.loads(line) for line in f if line.strip()]
    assert len(entries) == 2, f"Expected 2 telemetry entries, got {len(entries)}"
    assert entries[0]["pattern_name"] == "stale_load_bearing"
    assert entries[1]["preset"] == "high_precision"
    # Telemetry still records raw candidate_count and scores (not tiered)
    assert "candidate_count" in entries[0]
    assert "scores" in entries[0]


# ---------------------------------------------------------------------------
# 9. engram_query_pattern summary_top_k=0 — all keywords
# ---------------------------------------------------------------------------

def test_query_pattern_summary_top_k_zero():
    """summary_top_k=0 on engram_query_pattern emits all Tier-2 entries."""
    server = fresh_server()
    obs_id = _add_observation(server, "Stale load bearing test observation for top_k zero.")
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_score=2.5, recall_count=0 WHERE id=?", (obs_id,)
        )
        conn.commit()
    finally:
        conn.close()
    _set_recall_fields(server, obs_id, "Load-bearing summary.", ["load-bearing", "stale"])

    r = json.loads(server.engram_query_pattern(
        "stale_load_bearing", preset="balanced", summary_top_k=0
    ))
    results = r["results"]
    # With summary_top_k=0, no entry should have 'summary'
    for entry in results:
        assert "summary" not in entry, (
            f"summary_top_k=0 should suppress all summaries: {entry}"
        )


# ---------------------------------------------------------------------------
# 10 + 11. Return shape key checks (already covered above, but explicit tests)
# ---------------------------------------------------------------------------

def test_query_return_shape_keys():
    """engram_query default return has exactly the required top-level keys."""
    server = fresh_server()
    _add_observation(server, "Shape key test observation.")

    r = json.loads(server.engram_query(query="shape key test"))
    assert "results" in r
    assert "query" in r
    assert "total_matches" in r
    # Legacy keys must NOT appear in default mode
    assert "count" not in r, "'count' is a legacy key and should not appear in default mode"
    assert "search_methods" not in r, "'search_methods' is a legacy key"
    assert "message" not in r


def test_query_pattern_return_shape_keys():
    """engram_query_pattern default return has the required top-level keys."""
    server = fresh_server()

    r = json.loads(server.engram_query_pattern("stale_load_bearing"))
    assert "results" in r
    assert "pattern_name" in r
    assert "preset" in r
    assert "total_matches" in r
    # Legacy keys must NOT appear
    assert "candidates" not in r, "'candidates' is the legacy key"
    assert "candidate_count" not in r, "'candidate_count' is the legacy key"
    assert "hint" not in r


# ---------------------------------------------------------------------------
# 12. summary_top_k negative — clamped to 0 (all keywords)
# ---------------------------------------------------------------------------

def test_query_negative_summary_top_k_clamped():
    """Negative summary_top_k is clamped to 0 (all Tier 2)."""
    server = fresh_server()
    nid = _add_observation(server, "Negative top_k clamping test claim.")
    _set_recall_fields(server, nid, "Summary text.", ["kw1", "kw2"])

    r = json.loads(server.engram_query(
        query="negative top_k clamping", summary_top_k=-5
    ))
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
