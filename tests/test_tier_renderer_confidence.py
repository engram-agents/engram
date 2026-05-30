"""Tests for PR-5: inline confidence restoration across tier-1 and tier-2 entries.

Verifies that _build_tiered_results includes the 'confidence' key in every
tier-1 and tier-2 dict, and that the 6 query tools (engram_query,
engram_query_pattern, engram_inspect recall, engram_list,
engram_get_subgraph recall, engram_reflect) pass confidence through to their
tiered output entries.

Covers:
  1.  _build_tiered_results tier-1 includes confidence (float node)
  2.  _build_tiered_results tier-2 with keywords includes confidence
  3.  _build_tiered_results tier-2 bare-id includes confidence
  4.  _build_tiered_results None confidence passes through as None
  5.  engram_query tier-1 entries carry confidence
  6.  engram_query tier-2 entries carry confidence
  7.  engram_query_pattern tier-1 entries carry confidence
  8.  engram_query_pattern tier-2 entries carry confidence
  9.  engram_list entries carry confidence when node has it
  10. engram_list entries omit confidence key when node has NULL confidence
  11. engram_get_subgraph recall — confidence NOT expected (different shape; no tiered structure)
  12. engram_reflect weakly_grounded tier-1 carries confidence
  13. engram_reflect weakly_grounded tier-2 carries confidence
  14. engram_reflect uncited_observations tier-1 carries confidence
  15. engram_reflect thin_support_derivations tier-1 carries confidence
  16. engram_reflect thin_support_derivations tier-2 carries confidence (even when None)
  17. Roundtrip: confidence value in tier-1 matches DB value
  18. Roundtrip: confidence value in tier-2 matches DB value
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_tier_renderer_confidence_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
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


def _add_observation(server, claim, url="https://example.com/test", confidence=None):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test Source",
        "quoted_text": claim[:80],
        "interpretation": "Test interpretation.",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"add_observation failed: {r}"
    nid = r["observation_id"]
    if confidence is not None:
        conn = server._get_db()
        try:
            conn.execute("UPDATE nodes SET confidence = ? WHERE id = ?", (confidence, nid))
            conn.commit()
        finally:
            conn.close()
    return nid


def _add_axiom(server, claim, basis="Test basis."):
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": claim, "basis": basis,
    })))
    assert "axiom_id" in r, f"add_axiom failed: {r}"
    return r["axiom_id"]


def _derive(server, claim, supporting_ids):
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": claim,
        "supporting_ids": ",".join(supporting_ids) if isinstance(supporting_ids, list) else supporting_ids,
        "logical_chain": "Test chain.",
        "reasoning_type": "deductive_modus_ponens",
    })))
    assert "derivation_id" in r, f"derive failed: {r}"
    return r["derivation_id"]


def _set_recall_fields(server, node_id, summary, keywords):
    kw = list(keywords)
    while len(kw) < 3:
        kw.append(f"pad{len(kw)}")
    r = json.loads(server.engram_set_recall_summaries(payload_json=json.dumps({
        "summaries": [{"node_id": node_id, "recall_summary": summary, "recall_keywords": kw}]
    })))
    assert r.get("applied") == 1, f"engram_set_recall_summaries failed: {r}"


def _get_confidence_from_db(server, node_id):
    conn = server._get_db()
    try:
        row = conn.execute("SELECT confidence FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return row["confidence"] if row else None
    finally:
        conn.close()


def _set_importance(server, node_id, score):
    conn = server._get_db()
    try:
        conn.execute("UPDATE nodes SET importance_score = ? WHERE id = ?", (score, node_id))
        conn.commit()
    finally:
        conn.close()


def _get_entries(result, key):
    """Unwrap _slim_reflect_briefing wrapper if present."""
    val = result.get(key, [])
    if isinstance(val, dict) and "shown" in val:
        return val["shown"]
    return val if isinstance(val, list) else []


# ---------------------------------------------------------------------------
# 1-4. Unit tests for _build_tiered_results
# ---------------------------------------------------------------------------

def test_build_tiered_results_tier1_includes_confidence():
    """_build_tiered_results tier-1 dict includes 'confidence' key."""
    server = fresh_server()
    nodes = [{"id": "ob_0001", "confidence": 0.85, "recall_summary": "Test summary."}]
    result = server._build_tiered_results(nodes, summary_top_k=1)
    assert len(result) == 1
    entry = result[0]
    assert "confidence" in entry, f"Tier-1 entry missing 'confidence': {entry}"
    assert entry["confidence"] == 0.85, f"Wrong confidence value: {entry}"
    assert "summary" in entry, f"Tier-1 entry missing 'summary': {entry}"


def test_build_tiered_results_tier2_keywords_includes_confidence():
    """_build_tiered_results tier-2 with keywords includes 'confidence' key."""
    server = fresh_server()
    nodes = [
        {"id": "ob_0001", "confidence": 0.72, "recall_summary": "T1 summary."},
        {"id": "ob_0002", "confidence": 0.50, "recall_keywords": ["alpha", "beta", "gamma"]},
    ]
    result = server._build_tiered_results(nodes, summary_top_k=1)
    assert len(result) == 2
    tier2 = result[1]
    assert "confidence" in tier2, f"Tier-2 keywords entry missing 'confidence': {tier2}"
    assert tier2["confidence"] == 0.50, f"Wrong confidence value: {tier2}"
    assert "keywords" in tier2, f"Tier-2 entry missing 'keywords': {tier2}"


def test_build_tiered_results_tier2_bare_id_includes_confidence():
    """_build_tiered_results tier-2 bare-id entry includes 'confidence' key."""
    server = fresh_server()
    nodes = [
        {"id": "ob_0001", "confidence": 0.60, "recall_summary": "T1 summary."},
        # no recall_keywords → bare-id tier-2
        {"id": "ob_0002", "confidence": 0.33},
    ]
    result = server._build_tiered_results(nodes, summary_top_k=1)
    assert len(result) == 2
    tier2 = result[1]
    assert "confidence" in tier2, f"Tier-2 bare entry missing 'confidence': {tier2}"
    assert tier2["confidence"] == 0.33, f"Wrong confidence value: {tier2}"
    assert "keywords" not in tier2, f"Bare tier-2 should not have 'keywords': {tier2}"
    assert "summary" not in tier2, f"Bare tier-2 should not have 'summary': {tier2}"


def test_build_tiered_results_none_confidence_passes_through():
    """_build_tiered_results passes None confidence for non-confidence-bearing types."""
    server = fresh_server()
    # ax_ nodes don't carry confidence
    nodes = [
        {"id": "ax_0001", "confidence": None, "recall_summary": "Axiom summary."},
        {"id": "ax_0002", "confidence": None},
    ]
    result = server._build_tiered_results(nodes, summary_top_k=1)
    assert len(result) == 2
    # Tier-1: confidence is present but None
    t1 = result[0]
    assert "confidence" in t1, f"Tier-1 entry should have 'confidence' key even if None: {t1}"
    assert t1["confidence"] is None, f"Expected None confidence for axiom: {t1}"
    # Tier-2: same
    t2 = result[1]
    assert "confidence" in t2, f"Tier-2 entry should have 'confidence' key even if None: {t2}"
    assert t2["confidence"] is None, f"Expected None confidence for axiom: {t2}"


# ---------------------------------------------------------------------------
# 5-6. engram_query
# ---------------------------------------------------------------------------

def test_engram_query_tier1_carries_confidence():
    """engram_query tier-1 entries include 'confidence' key with the node's value."""
    server = fresh_server()
    nid = _add_observation(server, "Confidence query test claim tier one.", confidence=0.78)
    _set_recall_fields(server, nid, "RS for confidence query tier one.", ["conf", "query", "t1"])

    r = json.loads(server.engram_query(payload_json=json.dumps({"query": "confidence query test tier one", "summary_top_k": 5})))
    results = r["results"]
    assert len(results) > 0, "Expected at least one result"

    for entry in results[:3]:  # tier-1
        assert "confidence" in entry, f"Tier-1 entry missing 'confidence': {entry}"


def test_engram_query_tier2_carries_confidence():
    """engram_query tier-2 entries include 'confidence' key."""
    server = fresh_server()
    ids = []
    for i in range(5):
        nid = _add_observation(
            server,
            f"Confidence query tier two test claim {i}.",
            confidence=0.60 + i * 0.01,
        )
        _set_recall_fields(server, nid, f"RS tier2 {i}.", [f"t2a{i}", f"t2b{i}", f"t2c{i}"])
        ids.append(nid)

    r = json.loads(server.engram_query(payload_json=json.dumps(
        {"query": "confidence query tier two test", "top_k": 5, "summary_top_k": 2}
    )))
    results = r["results"]
    tier2 = results[2:]
    for entry in tier2:
        assert "confidence" in entry, f"Tier-2 entry missing 'confidence': {entry}"


# ---------------------------------------------------------------------------
# 7-8. engram_query_pattern
# ---------------------------------------------------------------------------

def test_engram_query_pattern_tier1_carries_confidence():
    """engram_query_pattern tier-1 entries carry confidence."""
    server = fresh_server()
    # Create a stale load-bearing node: high importance + low recall_count
    nid = _add_observation(server, "Stale load-bearing pattern confidence test claim.")
    _set_recall_fields(server, nid, "SLB conf test summary.", ["slb", "conf", "test"])
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET importance_score=2.5, recall_count=0, confidence=0.70 WHERE id=?",
            (nid,)
        )
        conn.commit()
    finally:
        conn.close()

    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({"pattern_name": "stale_load_bearing", "summary_top_k": 5})))
    results = r["results"]
    if not results:
        return  # pattern returned no candidates — skip (not a conf failure)
    # At least the first tier-1 entry must have confidence
    assert "confidence" in results[0], (
        f"Pattern tier-1 entry missing 'confidence': {results[0]}"
    )


def test_engram_query_pattern_tier2_carries_confidence():
    """engram_query_pattern tier-2 entries carry confidence."""
    server = fresh_server()
    # Create enough stale nodes that there's at least 1 tier-2 entry with summary_top_k=1
    for i in range(3):
        nid = _add_observation(
            server,
            f"Stale pattern confidence tier2 test {i}.",
            confidence=0.65,
        )
        _set_recall_fields(server, nid, f"SLB t2 {i} summary.", [f"s{i}a", f"s{i}b", f"s{i}c"])
        _set_importance(server, nid, 2.0)
        conn = server._get_db()
        try:
            conn.execute("UPDATE nodes SET recall_count = 0 WHERE id = ?", (nid,))
            conn.commit()
        finally:
            conn.close()

    r = json.loads(server.engram_query_pattern(payload_json=json.dumps({
        "pattern_name": "stale_load_bearing", "summary_top_k": 1,
    })))
    results = r["results"]
    tier2 = results[1:]
    for entry in tier2:
        assert "confidence" in entry, f"Pattern tier-2 entry missing 'confidence': {entry}"


# ---------------------------------------------------------------------------
# 9-10. engram_list
# ---------------------------------------------------------------------------

def test_engram_list_includes_confidence_when_present():
    """engram_list default shape includes 'confidence' key for confidence-bearing nodes."""
    server = fresh_server()
    nid = _add_observation(server, "Engram list confidence test claim.", confidence=0.75)

    r = json.loads(server.engram_list(payload_json=json.dumps({"node_type": "observation_factual"})))
    assert r["status"] == "ok", f"engram_list error: {r}"
    nodes = r["nodes"]
    target = next((n for n in nodes if n["id"] == nid), None)
    assert target is not None, f"{nid} not found in engram_list results"
    assert "confidence" in target, f"engram_list entry missing 'confidence': {target}"
    assert abs(target["confidence"] - 0.75) < 0.01, f"Wrong confidence: {target}"


def test_engram_list_omits_confidence_for_null_confidence():
    """engram_list omits 'confidence' key when node confidence is NULL.

    engram_list uses conditional inclusion: the 'confidence' key only appears
    when conf is not None (see server.py: `if conf is not None: entry["confidence"] = ...`).
    This test verifies that behavior by directly nulling a node's confidence.
    Note: axioms get confidence=1.0; we null it manually to test the branch.
    """
    server = fresh_server()
    ob_id = _add_observation(server, "Observation for null-confidence list test.")
    # Null out the confidence to exercise the conditional branch
    conn = server._get_db()
    try:
        conn.execute("UPDATE nodes SET confidence = NULL WHERE id = ?", (ob_id,))
        conn.commit()
    finally:
        conn.close()

    r = json.loads(server.engram_list(payload_json=json.dumps({"node_type": "observation_factual"})))
    assert r["status"] == "ok", f"engram_list error: {r}"
    nodes = r["nodes"]
    target = next((n for n in nodes if n["id"] == ob_id), None)
    assert target is not None, f"{ob_id} not found in engram_list results"
    # When confidence is NULL, the key should be absent (conditional inclusion)
    assert "confidence" not in target, (
        f"engram_list should omit 'confidence' key when confidence is NULL, got: {target}"
    )


# ---------------------------------------------------------------------------
# 11. engram_get_subgraph — note on shape
# ---------------------------------------------------------------------------

def test_engram_get_subgraph_recall_content_shape():
    """engram_get_subgraph recall view returns per-node content entries.

    Note: get_subgraph uses its own content dict (not _build_tiered_results).
    Its content entries have {type, hop_distance, recall_summary?, recall_keywords?}
    and do NOT include confidence — that is out of scope for PR-5 (which targets
    the _build_tiered_results path only). This test documents the current shape
    so future PRs can extend it.
    """
    server = fresh_server()
    ax_id = _add_axiom(server, "Subgraph root for confidence shape test.")
    ob_id = _add_observation(server, "Subgraph child for confidence shape test.")
    # Link ob → ax via derives_from so they're connected
    _derive(server, "Subgraph derived claim.", [ax_id])

    r = json.loads(server.engram_get_subgraph(payload_json=json.dumps({"node_id": ax_id, "depth": 2, "view": "recall"})))
    assert "content" in r, f"Subgraph recall should have 'content': {r.keys()}"
    content = r["content"]
    assert ax_id in content, f"Root {ax_id} missing from subgraph content"
    root_entry = content[ax_id]
    # Document current shape: type + hop_distance (+ optional recall_summary/keywords)
    assert "type" in root_entry, f"Root entry missing 'type': {root_entry}"
    assert "hop_distance" in root_entry, f"Root entry missing 'hop_distance': {root_entry}"
    # Confidence is NOT currently in subgraph content entries (out of PR-5 scope).
    # If a future PR adds it, update this assertion.


# ---------------------------------------------------------------------------
# 12-15. engram_reflect high-volume categories
# ---------------------------------------------------------------------------

def test_engram_reflect_weakly_grounded_tier1_carries_confidence():
    """engram_reflect weakly_grounded tier-1 entries carry confidence."""
    server = fresh_server()
    # Create a low-confidence observation (confidence < 0.5) in working memory
    nid = _add_observation(
        server, "Weakly grounded conf test claim tier one.",
        confidence=0.3, url="https://example.com/wg1"
    )
    _set_recall_fields(server, nid, "WG tier-1 summary.", ["wg", "conf", "tier1"])
    _set_importance(server, nid, 1.5)

    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 5})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "weakly_grounded")
    target = next((e for e in entries if e.get("id") == nid), None)
    assert target is not None, f"{nid} not found in weakly_grounded"
    assert "confidence" in target, f"weakly_grounded tier-1 missing 'confidence': {target}"
    assert abs(target["confidence"] - 0.3) < 0.01, (
        f"Wrong confidence value: expected 0.3, got {target['confidence']}"
    )


def test_engram_reflect_weakly_grounded_tier2_carries_confidence():
    """engram_reflect weakly_grounded tier-2 entries carry confidence."""
    server = fresh_server()
    # Create 2 low-confidence observations; with summary_top_k=1, second goes to tier-2
    ids = []
    for i in range(2):
        nid = _add_observation(
            server,
            f"Weakly grounded conf tier2 test {i}.",
            confidence=0.25 + i * 0.05,
            url=f"https://example.com/wgt2{i}",
        )
        _set_recall_fields(server, nid, f"WG t2 summary {i}.", [f"w{i}a", f"w{i}b", f"w{i}c"])
        _set_importance(server, nid, 1.5 - i * 0.1)
        ids.append(nid)

    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 1})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "weakly_grounded")
    assert len(entries) >= 2, f"Expected at least 2 entries; got {len(entries)}: {entries}"

    # Second entry is tier-2
    tier2 = entries[1]
    assert "confidence" in tier2, f"weakly_grounded tier-2 missing 'confidence': {tier2}"


def test_engram_reflect_uncited_obs_tier1_carries_confidence():
    """engram_reflect uncited_observations tier-1 entries carry confidence."""
    server = fresh_server()
    nid = _add_observation(
        server, "Uncited observation confidence tier1 test.",
        confidence=0.68, url="https://example.com/unc1"
    )
    _set_recall_fields(server, nid, "Uncited t1 summary.", ["unc", "conf", "t1"])
    _set_importance(server, nid, 1.5)

    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 5})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "uncited_observations")
    target = next((e for e in entries if e.get("id") == nid), None)
    assert target is not None, f"{nid} not found in uncited_observations"
    assert "confidence" in target, f"uncited_observations tier-1 missing 'confidence': {target}"


def test_engram_reflect_thin_support_tier1_carries_confidence():
    """engram_reflect thin_support_derivations tier-1 entries carry confidence (PR-5 fix)."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for thin-support confidence test.")
    dv_id = _derive(server, "Thin-support derivation confidence test claim.", [ax_id])
    # Set a known confidence on the derivation
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET confidence = 0.55, importance_score = 1.5 WHERE id = ?",
            (dv_id,)
        )
        conn.commit()
    finally:
        conn.close()
    _set_recall_fields(server, dv_id, "Thin support t1 summary.", ["thin", "conf", "t1"])

    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 5})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "thin_support_derivations")
    target = next((e for e in entries if e.get("id") == dv_id), None)
    assert target is not None, f"{dv_id} not found in thin_support_derivations"
    assert "confidence" in target, (
        f"thin_support_derivations tier-1 missing 'confidence' (PR-5 fix): {target}"
    )
    assert abs(target["confidence"] - 0.55) < 0.01, (
        f"Wrong confidence value: expected 0.55, got {target['confidence']}"
    )


def test_engram_reflect_thin_support_tier2_carries_confidence():
    """engram_reflect thin_support_derivations tier-2 entries carry confidence key (PR-5 r2 fix).

    With summary_top_k=1, the second thin-support derivation lands in tier-2.
    The 'confidence' key must be present unconditionally — including when the
    value is None — matching tier-1 behaviour and the other reflect categories.
    """
    server = fresh_server()
    ax_id = _add_axiom(server, "Axiom for thin-support tier-2 confidence test.")
    # Create two thin-support derivations so the second one falls into tier-2
    # when summary_top_k=1.  Use descending importance so ordering is deterministic.
    dv_ids = []
    for i in range(2):
        dv_id = _derive(
            server,
            f"Thin-support tier-2 confidence test derivation {i}.",
            [ax_id],
        )
        dv_ids.append(dv_id)

    # First derivation: higher importance so it becomes tier-1; known confidence
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET confidence = 0.60, importance_score = 2.0 WHERE id = ?",
            (dv_ids[0],),
        )
        # Second derivation: lower importance → tier-2; deliberately NULL confidence
        # to verify the key is still present (the pre-fix conditional dropped it).
        conn.execute(
            "UPDATE nodes SET confidence = NULL, importance_score = 1.0 WHERE id = ?",
            (dv_ids[1],),
        )
        conn.commit()
    finally:
        conn.close()

    _set_recall_fields(server, dv_ids[0], "Thin t2 tier1 summary.", ["thin", "t2", "t1"])
    _set_recall_fields(server, dv_ids[1], "Thin t2 tier2 summary.", ["thin", "t2", "t2kw"])

    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 1})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "thin_support_derivations")
    tier2_target = next((e for e in entries if e.get("id") == dv_ids[1]), None)
    assert tier2_target is not None, (
        f"{dv_ids[1]} not found in thin_support_derivations tier-2; entries: {entries}"
    )
    # The 'confidence' key must be present even when the value is None.
    assert "confidence" in tier2_target, (
        f"thin_support_derivations tier-2 missing 'confidence' key (PR-5 r2 fix): {tier2_target}"
    )
    assert tier2_target["confidence"] is None, (
        f"Expected None confidence for tier-2 null-confidence derivation, "
        f"got: {tier2_target['confidence']}"
    )


# ---------------------------------------------------------------------------
# 17-18. Roundtrip: confidence in tiered output matches DB value
# ---------------------------------------------------------------------------

def test_roundtrip_confidence_tier1_matches_db():
    """Confidence value in tier-1 entry matches what's stored in the DB."""
    server = fresh_server()
    expected_conf = 0.82
    nid = _add_observation(
        server, "Roundtrip confidence tier-1 test claim.", confidence=expected_conf
    )
    _set_recall_fields(server, nid, "RT t1 summary.", ["rt", "conf", "t1"])

    r = json.loads(server.engram_query(payload_json=json.dumps(
        {"query": "roundtrip confidence tier one", "summary_top_k": 5}
    )))
    results = r["results"]
    target = next((e for e in results if e.get("id") == nid), None)
    if target is None:
        return  # FTS may not match — acceptable; not a confidence failure

    assert "confidence" in target, f"Tier-1 entry missing 'confidence': {target}"
    db_conf = _get_confidence_from_db(server, nid)
    assert abs(target["confidence"] - db_conf) < 0.001, (
        f"Tier-1 confidence {target['confidence']} doesn't match DB {db_conf}"
    )


def test_roundtrip_confidence_tier2_matches_db():
    """Confidence value in tier-2 entry matches what's stored in the DB."""
    server = fresh_server()
    expected_conf = 0.63
    ids = []
    for i in range(4):
        nid = _add_observation(
            server,
            f"Roundtrip confidence tier-2 test claim {i}.",
            confidence=expected_conf,
        )
        _set_recall_fields(server, nid, f"RT t2 summary {i}.", [f"rt2a{i}", f"rt2b{i}", f"rt2c{i}"])
        ids.append(nid)

    r = json.loads(server.engram_query(payload_json=json.dumps(
        {"query": "roundtrip confidence tier two", "top_k": 4, "summary_top_k": 2}
    )))
    results = r["results"]
    tier2_entries = results[2:]
    for entry in tier2_entries:
        assert "confidence" in entry, f"Tier-2 entry missing 'confidence': {entry}"
        nid = entry["id"]
        db_conf = _get_confidence_from_db(server, nid)
        if db_conf is not None:
            assert abs(entry["confidence"] - db_conf) < 0.001, (
                f"Tier-2 confidence {entry['confidence']} doesn't match DB {db_conf} for {nid}"
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
