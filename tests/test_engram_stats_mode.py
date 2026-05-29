"""Tests for engram_stats mode/sections/confidence extensions (PR stats/mode-and-calibration-extension).

Coverage:
  1. Backward compat — no-arg call returns all sections including new `confidence` key.
  2. Single-payload signature — engram_stats('{}') works.
  3. Mode validation — invalid mode returns error.
  4. mode="all" — counts match unwindowed query.
  5. mode="1-turn" — counts match created_at >= now-24h filter.
  6. Confidence section structure — per-type has expected subkeys; empty buckets omitted.
  7. Confidence by_quote_type — matches direct SQL aggregation.
  8. Confidence by_reasoning_type — matches derivation metadata aggregation.
  9. Confidence by_source_class — matches observation metadata aggregation.
  10. sections selector — sections=["confidence"] returns only confidence + window.
  11. sections selector with unknown name — graceful, known sections returned.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_stats_mode_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Return a freshly initialised server module with an empty test DB."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _seed_axiom(server, claim: str) -> str:
    """Insert a single axiom and return its ID."""
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": claim,
        "basis": "test fixture basis",
    })))
    assert "axiom_id" in r, f"axiom creation failed: {r}"
    return r["axiom_id"]


def _seed_observation(server, claim: str, quote_type: str = "hard_data",
                      source_class: str = "external") -> str:
    """Insert an observation and return its ID."""
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/article",
        "title": "Test Article",
        "quoted_text": f"Test quoted text for: {claim}",
        "interpretation": f"Test interpretation for: {claim}",
        "claim": claim,
        "quote_type": quote_type,
        "source_class": source_class,
    })))
    assert r.get("status") == "created", f"observation creation failed: {r}"
    return r["observation_id"]


def _seed_derivation(server, claim: str, supporting_ids: list[str],
                     reasoning_type: str = "deductive_modus_ponens") -> str:
    """Insert a derivation and return its ID."""
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": claim,
        "supporting_ids": ",".join(supporting_ids),
        "logical_chain": f"Test chain for: {claim}",
        "reasoning_type": reasoning_type,
    })))
    assert "derivation_id" in r, f"derivation creation failed: {r}"
    return r["derivation_id"]


# ── 1. Backward compat ────────────────────────────────────────────────────


def test_no_arg_call_returns_all_sections():
    """engram_stats() with no argument must return all sections including confidence."""
    server = fresh_server()
    _seed_observation(server, "Backward compat claim A")
    r = json.loads(server.engram_stats())
    # Legacy fields still present
    assert "node_counts_by_type" in r, r
    assert "edge_counts_by_relation" in r, r
    assert "reasoning_type_breakdown" in r, r
    assert "open_questions_count" in r, r
    assert "open_questions_recent" in r, r
    assert "open_predictions_count" in r, r
    assert "open_predictions_recent" in r, r
    assert "weakest_nodes" in r, r
    assert "memory" in r, r
    # New section present
    assert "confidence" in r, r
    # No window key when mode=all (default)
    assert "window" not in r, r


# ── 2. Single-payload signature ───────────────────────────────────────────


def test_empty_payload_json_works():
    """engram_stats('{}') must work identically to engram_stats()."""
    server = fresh_server()
    r = json.loads(server.engram_stats("{}"))
    assert "node_counts_by_type" in r, r
    assert "confidence" in r, r


def test_invalid_json_returns_error():
    """Malformed JSON payload returns a structured error."""
    server = fresh_server()
    r = json.loads(server.engram_stats("{not-json}"))
    assert "error" in r, r
    assert "Invalid JSON" in r["error"], r


def test_non_object_payload_returns_error():
    """Array payload returns a structured error."""
    server = fresh_server()
    r = json.loads(server.engram_stats("[1, 2, 3]"))
    assert "error" in r, r
    assert "JSON object" in r["error"], r


# ── 3. Mode validation ────────────────────────────────────────────────────


def test_invalid_mode_returns_error():
    """An unknown mode value returns a structured error."""
    server = fresh_server()
    r = json.loads(server.engram_stats('{"mode": "invalid"}'))
    assert "error" in r, r
    assert "mode" in r["error"].lower() or "Invalid" in r["error"], r


# ── 4. mode="all" counts match unwindowed query ───────────────────────────


def test_mode_all_matches_unwindowed():
    """mode='all' results must match the default (no-mode) call."""
    server = fresh_server()
    _seed_observation(server, "All-mode claim 1")
    _seed_observation(server, "All-mode claim 2")
    r_default = json.loads(server.engram_stats("{}"))
    r_all = json.loads(server.engram_stats('{"mode": "all"}'))
    assert r_default["node_counts_by_type"] == r_all["node_counts_by_type"]
    assert r_default["confidence"] == r_all["confidence"]
    assert "window" not in r_all, "mode=all must not include window key"


# ── 5. mode="1-turn" respects created_at window ───────────────────────────


def test_mode_1_turn_filters_old_nodes():
    """mode='1-turn' must exclude nodes created more than 24h ago.

    Implementation: directly manipulate created_at on a few nodes in the
    test DB to simulate old nodes, then verify they don't appear in counts.
    """
    server = fresh_server()
    # Seed two observations
    obs_new = _seed_observation(server, "New node — within 1-turn window")
    obs_old = _seed_observation(server, "Old node — outside 1-turn window")

    # Back-date obs_old to 48 hours ago by direct SQL
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE nodes SET created_at = datetime('now', '-48 hours') WHERE id = ?",
        (obs_old,),
    )
    conn.commit()
    conn.close()

    r_1t = json.loads(server.engram_stats('{"mode": "1-turn"}'))

    # Window metadata must be present
    assert "window" in r_1t, r_1t
    assert r_1t["window"]["mode"] == "1-turn"
    assert r_1t["window"]["approx_hours"] == 24

    # node_counts_by_type should reflect only the 1 new node
    obs_counts = r_1t["node_counts_by_type"].get("observation_factual", {})
    assert obs_counts.get("total", 0) == 1, (
        f"Expected 1 observation in 1-turn window, got {obs_counts}"
    )


def test_mode_7_turn_includes_week_old_nodes():
    """mode='7-turn' must include nodes created 2 days ago."""
    server = fresh_server()
    obs_a = _seed_observation(server, "Node A — 2 days ago")

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE nodes SET created_at = datetime('now', '-2 days') WHERE id = ?",
        (obs_a,),
    )
    conn.commit()
    conn.close()

    r_7t = json.loads(server.engram_stats('{"mode": "7-turn"}'))
    assert "window" in r_7t
    assert r_7t["window"]["approx_hours"] == 168
    obs_counts = r_7t["node_counts_by_type"].get("observation_factual", {})
    assert obs_counts.get("total", 0) == 1, (
        f"Expected 1 observation in 7-turn window, got {obs_counts}"
    )


def test_mode_30_turn_includes_3_week_old_nodes():
    """mode='30-turn' must include nodes created 20 days ago."""
    server = fresh_server()
    obs_a = _seed_observation(server, "Node A — 20 days ago")

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE nodes SET created_at = datetime('now', '-20 days') WHERE id = ?",
        (obs_a,),
    )
    conn.commit()
    conn.close()

    r_30t = json.loads(server.engram_stats('{"mode": "30-turn"}'))
    assert "window" in r_30t
    assert r_30t["window"]["approx_hours"] == 720
    obs_counts = r_30t["node_counts_by_type"].get("observation_factual", {})
    assert obs_counts.get("total", 0) == 1, (
        f"Expected 1 observation in 30-turn window, got {obs_counts}"
    )


# ── 6. Confidence section structure ──────────────────────────────────────


def test_confidence_section_present_when_nodes_exist():
    """When confidence-bearing nodes exist, confidence section has expected subkeys."""
    server = fresh_server()
    _seed_observation(server, "Confidence section obs 1", quote_type="hard_data")
    r = json.loads(server.engram_stats("{}"))
    conf = r["confidence"]
    assert "by_type" in conf, conf
    assert "by_quote_type" in conf, conf
    assert "by_reasoning_type" in conf, conf
    assert "by_source_class" in conf, conf


def test_confidence_by_type_subkeys():
    """Each populated by_type entry has n, mean, p25, p50, p75, p90, p95."""
    server = fresh_server()
    for i in range(5):
        _seed_observation(server, f"Confidence subkeys obs {i}", quote_type="hard_data")
    r = json.loads(server.engram_stats("{}"))
    by_type = r["confidence"]["by_type"]
    assert "observation_factual" in by_type, by_type
    entry = by_type["observation_factual"]
    for key in ("n", "mean", "p25", "p50", "p75", "p90", "p95"):
        assert key in entry, f"Missing key '{key}' in by_type entry: {entry}"
    assert entry["n"] == 5


def test_confidence_empty_buckets_omitted():
    """Types with no nodes are omitted from by_type; we do not return {n:0, mean:0}."""
    server = fresh_server()
    # Only seed observations — no derivations, no conjectures
    _seed_observation(server, "Only obs", quote_type="hard_data")
    r = json.loads(server.engram_stats("{}"))
    by_type = r["confidence"]["by_type"]
    assert "observation_factual" in by_type
    # derivation and conjecture have no nodes — must not appear
    assert "derivation" not in by_type, f"Empty derivation bucket leaked: {by_type}"
    assert "conjecture" not in by_type, f"Empty conjecture bucket leaked: {by_type}"


def test_confidence_empty_graph():
    """With no nodes, confidence section returns all-empty dicts (no crash)."""
    server = fresh_server()
    r = json.loads(server.engram_stats("{}"))
    conf = r["confidence"]
    assert conf["by_type"] == {}
    assert conf["by_quote_type"] == {}
    assert conf["by_reasoning_type"] == {}
    assert conf["by_source_class"] == {}


# ── 7. Confidence by_quote_type ───────────────────────────────────────────


def test_confidence_by_quote_type_matches_aggregation():
    """by_quote_type values should match direct SQL aggregation."""
    server = fresh_server()
    # Insert 3 hard_data obs and 2 editorial obs
    for i in range(3):
        _seed_observation(server, f"hard_data obs {i}", quote_type="hard_data")
    for i in range(2):
        _seed_observation(server, f"editorial obs {i}", quote_type="editorial")

    r = json.loads(server.engram_stats("{}"))
    bqt = r["confidence"]["by_quote_type"]

    assert "hard_data" in bqt, bqt
    assert "editorial" in bqt, bqt
    assert bqt["hard_data"]["n"] == 3
    assert bqt["editorial"]["n"] == 2
    # hard_data confidence = 0.95 (no discount, external)
    assert bqt["hard_data"]["mean"] == 0.95, bqt["hard_data"]
    # editorial confidence = 0.35
    assert bqt["editorial"]["mean"] == 0.35, bqt["editorial"]


def test_confidence_by_quote_type_only_observation_factual():
    """by_quote_type must NOT include non-observation_factual types (e.g. axioms)."""
    server = fresh_server()
    # Seed an axiom — axioms are confidence-bearing but NOT observation_factual
    _seed_axiom(server, "Axiom claim for quote_type isolation test")
    # Seed one observation
    _seed_observation(server, "Obs for isolation test", quote_type="hard_data")
    r = json.loads(server.engram_stats("{}"))
    # by_quote_type should only have the observation's quote_type (not axiom's)
    # Axioms don't have quote_type, so this is really a count sanity check
    bqt = r["confidence"]["by_quote_type"]
    assert "hard_data" in bqt
    assert bqt["hard_data"]["n"] == 1


# ── 8. Confidence by_reasoning_type ──────────────────────────────────────


def test_confidence_by_reasoning_type_matches_derivations():
    """by_reasoning_type must aggregate derivation confidences by metadata field."""
    server = fresh_server()
    ax_a = _seed_axiom(server, "Premise A for reasoning_type test")
    ax_b = _seed_axiom(server, "Premise B for reasoning_type test")
    # Two deductive derivations
    _seed_derivation(server, "Deduction 1", [ax_a, ax_b], "deductive_modus_ponens")
    _seed_derivation(server, "Deduction 2", [ax_a, ax_b], "deductive_modus_ponens")
    # One inductive derivation
    _seed_derivation(server, "Induction 1", [ax_a, ax_b], "inductive_generalization")

    r = json.loads(server.engram_stats("{}"))
    brt = r["confidence"]["by_reasoning_type"]

    assert "deductive_modus_ponens" in brt, brt
    assert "inductive_generalization" in brt, brt
    assert brt["deductive_modus_ponens"]["n"] == 2
    assert brt["inductive_generalization"]["n"] == 1
    # Each entry must have n and mean
    for entry in brt.values():
        assert "n" in entry and "mean" in entry, entry


def test_confidence_by_reasoning_type_only_derivations():
    """by_reasoning_type must not include observation_factual nodes."""
    server = fresh_server()
    # Only observations
    _seed_observation(server, "Obs only for reasoning_type test", quote_type="hard_data")
    r = json.loads(server.engram_stats("{}"))
    # by_reasoning_type should be empty since no derivations exist
    assert r["confidence"]["by_reasoning_type"] == {}, r["confidence"]["by_reasoning_type"]


# ── 9. Confidence by_source_class ─────────────────────────────────────────


def test_confidence_by_source_class_matches_aggregation():
    """by_source_class must aggregate observation_factual confidences by source_class."""
    server = fresh_server()
    # 2 external + 1 introspective
    _seed_observation(server, "External obs 1", quote_type="hard_data", source_class="external")
    _seed_observation(server, "External obs 2", quote_type="hard_data", source_class="external")
    _seed_observation(server, "Introspective obs 1", quote_type="hard_data", source_class="introspective")

    r = json.loads(server.engram_stats("{}"))
    bsc = r["confidence"]["by_source_class"]

    assert "external" in bsc, bsc
    assert "introspective" in bsc, bsc
    assert bsc["external"]["n"] == 2
    assert bsc["introspective"]["n"] == 1
    # external hard_data = 0.95; introspective hard_data = 0.95 × 0.95 = 0.9025 → 0.902
    assert bsc["external"]["mean"] == 0.95, bsc["external"]
    assert bsc["introspective"]["mean"] == 0.902, bsc["introspective"]


# ── 10. sections selector ─────────────────────────────────────────────────


def test_sections_confidence_only():
    """sections=['confidence'] returns confidence + window metadata; other sections absent."""
    server = fresh_server()
    _seed_observation(server, "Sections-only obs", quote_type="hard_data")
    r = json.loads(server.engram_stats('{"sections": ["confidence"]}'))

    assert "confidence" in r, r
    # Legacy keys must be absent
    assert "node_counts_by_type" not in r, r
    assert "edge_counts_by_relation" not in r, r
    assert "reasoning_type_breakdown" not in r, r
    assert "open_questions_count" not in r, r
    assert "open_predictions_count" not in r, r
    assert "weakest_nodes" not in r, r
    assert "memory" not in r, r
    # window only present when mode != all (default is all here)
    assert "window" not in r, r


def test_sections_confidence_with_mode():
    """sections=['confidence'] + mode='1-turn' returns confidence + window."""
    server = fresh_server()
    _seed_observation(server, "Sections+mode obs", quote_type="editorial")
    r = json.loads(server.engram_stats('{"mode": "1-turn", "sections": ["confidence"]}'))
    assert "confidence" in r, r
    assert "window" in r, r
    assert r["window"]["mode"] == "1-turn"
    assert "node_counts_by_type" not in r, r


def test_sections_multiple():
    """sections=['structure', 'memory'] returns only those two sections."""
    server = fresh_server()
    _seed_observation(server, "Multi-section obs")
    r = json.loads(server.engram_stats('{"sections": ["structure", "memory"]}'))
    assert "node_counts_by_type" in r, r
    assert "memory" in r, r
    assert "confidence" not in r, r
    assert "edge_counts_by_relation" not in r, r
    assert "reasoning_type_breakdown" not in r, r


def test_sections_health_score_no_op():
    """health_score is a recognized section name but engram_stats doesn't compute it;
    requesting it should not error and the other sections should still work."""
    server = fresh_server()
    # health_score is a known name — requesting it alone is a no-op (nothing returned)
    # but must not crash
    r = json.loads(server.engram_stats('{"sections": ["health_score"]}'))
    assert "error" not in r, r


# ── 11. sections selector with unknown name ───────────────────────────────


def test_sections_unknown_name_graceful():
    """Unknown section names must be silently ignored; known names still returned."""
    server = fresh_server()
    _seed_observation(server, "Unknown section obs")
    r = json.loads(server.engram_stats('{"sections": ["structure", "totally_unknown_key"]}'))
    # No error
    assert "error" not in r, r
    # Known section returned
    assert "node_counts_by_type" in r, r
    # Unknown section not fabricated
    assert "totally_unknown_key" not in r, r


def test_sections_all_unknown_graceful():
    """All unknown section names — should return empty (but no error)."""
    server = fresh_server()
    r = json.loads(server.engram_stats('{"sections": ["nonexistent_a", "nonexistent_b"]}'))
    assert "error" not in r, r
    # No known sections in response (just window if mode != all, but default is all)
    assert "node_counts_by_type" not in r, r
    assert "confidence" not in r, r


def test_sections_non_string_elements_rejected():
    """sections list with non-string elements must return an error."""
    server = fresh_server()
    r = json.loads(server.engram_stats('{"sections": [1, "structure"]}'))
    assert "error" in r, r
    assert "strings" in r["error"]


# ── Additional: _percentile helper ───────────────────────────────────────


def test_percentile_helper_edge_cases():
    """_percentile should handle edge cases without crashing."""
    import server as srv
    assert srv._percentile([], 50) == 0.0
    assert srv._percentile([0.5], 50) == 0.5
    assert abs(srv._percentile([0.3, 0.7], 50) - 0.5) < 1e-10
    # [0.2, 0.4, 0.6, 0.8] at p25: idx=0.75, lo=0, hi=1, frac=0.75
    # result = 0.2 + 0.75*(0.4-0.2) = 0.2 + 0.15 = 0.35
    assert abs(srv._percentile([0.2, 0.4, 0.6, 0.8], 25) - 0.35) < 1e-10
    # p0 = min, p100 = max
    vals = [0.1, 0.5, 0.9]
    assert srv._percentile(vals, 0) == 0.1
    assert srv._percentile(vals, 100) == 0.9


if __name__ == "__main__":
    tests = [
        test_no_arg_call_returns_all_sections,
        test_empty_payload_json_works,
        test_invalid_json_returns_error,
        test_non_object_payload_returns_error,
        test_invalid_mode_returns_error,
        test_mode_all_matches_unwindowed,
        test_mode_1_turn_filters_old_nodes,
        test_mode_7_turn_includes_week_old_nodes,
        test_mode_30_turn_includes_3_week_old_nodes,
        test_confidence_section_present_when_nodes_exist,
        test_confidence_by_type_subkeys,
        test_confidence_empty_buckets_omitted,
        test_confidence_empty_graph,
        test_confidence_by_quote_type_matches_aggregation,
        test_confidence_by_quote_type_only_observation_factual,
        test_confidence_by_reasoning_type_matches_derivations,
        test_confidence_by_reasoning_type_only_derivations,
        test_confidence_by_source_class_matches_aggregation,
        test_sections_confidence_only,
        test_sections_confidence_with_mode,
        test_sections_multiple,
        test_sections_health_score_no_op,
        test_sections_unknown_name_graceful,
        test_sections_all_unknown_graceful,
        test_percentile_helper_edge_cases,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS: {test.__name__}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL: {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed}/{passed + failed} tests passed")
    if failed:
        sys.exit(1)
