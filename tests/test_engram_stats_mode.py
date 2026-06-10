"""Tests for engram_stats mode/sections/confidence extensions (PR stats/mode-and-calibration-extension).

Coverage:
  1. Backward compat — no-arg call returns all sections including new `confidence` key.
  2. Single-payload signature — _stats_impl() works with no args.
  4. mode="all" — counts match unwindowed query.
  5. mode="1-turn" — counts match created_at >= now-24h filter.
  6. Confidence section structure — per-type has expected subkeys; empty buckets omitted.
  7. Confidence by_quote_type — matches direct SQL aggregation.
  8. Confidence by_reasoning_type — matches derivation metadata aggregation.
  9. Confidence by_source_class — matches observation metadata aggregation.
  10. sections selector — sections=["confidence"] returns only confidence + window.
  11. sections selector with unknown name — graceful, known sections returned.

  Wrapper-contract coverage (JSON-parse / non-dict / invalid-mode / non-string
  sections rejection) lives in tests/test_stats_payload.py — those test the
  server.py payload_json surface, not engram_stats internals (IMPORT_RUBRIC.md R2).

Import-pattern contract (#948 wave A — R1):
    This file imports engram_stats, engram_observation, engram_derive, engram_epistemic,
    and engram_core directly and contains no ``import server``. The module objects are
    passed to fresh_module() (R4) for per-test reload isolation.
"""

from __future__ import annotations

import atexit
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add tests/ dir so conftest.fresh_module is importable both under pytest and
# when the file is run as a standalone script.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_derive
import engram_epistemic
import engram_observation
import engram_stats

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_stats_mode_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _make_fresh_mods():
    """Reload all family modules, configure a clean test DB, and return modules.

    Uses conftest.fresh_module(engram_stats) per R4: the module object is
    passed (not a string), so the ``import engram_stats`` at the top of
    this file is the AST-visible dependency edge for run_touched_tests.py.

    Returns (stats, obs, derive, epistemic, core) — freshly reloaded module
    objects with engram_core pointing to TEST_DIR.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    # Purge server + engram_* from sys.modules and reload engram_stats.
    stats = fresh_module(engram_stats)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    epistemic = importlib.import_module("engram_epistemic")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return stats, obs, derive, epistemic, core


def _seed_axiom(epistemic, claim: str) -> str:
    """Insert a single axiom and return its ID."""
    r = json.loads(epistemic._add_axiom_impl(
        claim=claim,
        basis="test fixture basis",
    ))
    assert "axiom_id" in r, f"axiom creation failed: {r}"
    return r["axiom_id"]


def _seed_observation(obs, claim: str, quote_type: str = "hard_data",
                      source_class: str = "external") -> str:
    """Insert an observation and return its ID."""
    r = json.loads(obs._add_observation_impl(
        url="https://example.com/article",
        title="Test Article",
        quoted_text=f"Test quoted text for: {claim}",
        interpretation=f"Test interpretation for: {claim}",
        claim=claim,
        quote_type=quote_type,
        source_class=source_class,
    ))
    assert r.get("status") == "created", f"observation creation failed: {r}"
    return r["observation_id"]


def _seed_derivation(derive, claim: str, supporting_ids: list,
                     reasoning_type: str = "deductive_modus_ponens") -> str:
    """Insert a derivation and return its ID."""
    r = json.loads(derive._derive_impl(
        claim=claim,
        supporting_ids=",".join(supporting_ids),
        logical_chain=f"Test chain for: {claim}",
        reasoning_type=reasoning_type,
    ))
    assert "derivation_id" in r, f"derivation creation failed: {r}"
    return r["derivation_id"]


# ── 1. Backward compat ────────────────────────────────────────────────────


def test_no_arg_call_returns_all_sections():
    """_stats_impl() with no args must return all sections including confidence."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    _seed_observation(obs, "Backward compat claim A")
    r = json.loads(stats._stats_impl())
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
    """_stats_impl() with no args returns all sections (same as mode=all)."""
    stats, _obs, _derive, _epistemic, _core = _make_fresh_mods()
    r = json.loads(stats._stats_impl(mode="all"))
    assert "node_counts_by_type" in r, r
    assert "confidence" in r, r


# Wrapper-contract tests (JSON parse / non-dict payload / invalid-mode error
# envelope / non-string-sections rejection) moved to tests/test_stats_payload.py
# per IMPORT_RUBRIC.md R2 — those exercise the server.py payload_json surface,
# not engram_stats internals. This file holds engram_stats family-internals only.


# ── 4. mode="all" counts match unwindowed query ───────────────────────────


def test_mode_all_matches_unwindowed():
    """mode='all' results must match the default (no-mode) call."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    _seed_observation(obs, "All-mode claim 1")
    _seed_observation(obs, "All-mode claim 2")
    r_default = json.loads(stats._stats_impl())
    r_all = json.loads(stats._stats_impl(mode="all"))
    assert r_default["node_counts_by_type"] == r_all["node_counts_by_type"]
    assert r_default["confidence"] == r_all["confidence"]
    assert "window" not in r_all, "mode=all must not include window key"


# ── 5. mode="1-turn" respects created_at window ───────────────────────────


def test_mode_1_turn_filters_old_nodes():
    """mode='1-turn' must exclude nodes created more than 24h ago.

    Implementation: directly manipulate created_at on a few nodes in the
    test DB to simulate old nodes, then verify they don't appear in counts.
    """
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    # Seed two observations
    obs_new = _seed_observation(obs, "New node — within 1-turn window")
    obs_old = _seed_observation(obs, "Old node — outside 1-turn window")

    # Back-date obs_old to 48 hours ago by direct SQL
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE nodes SET created_at = datetime('now', '-48 hours') WHERE id = ?",
        (obs_old,),
    )
    conn.commit()
    conn.close()

    r_1t = json.loads(stats._stats_impl(mode="1-turn"))

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
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    obs_a = _seed_observation(obs, "Node A — 2 days ago")

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE nodes SET created_at = datetime('now', '-2 days') WHERE id = ?",
        (obs_a,),
    )
    conn.commit()
    conn.close()

    r_7t = json.loads(stats._stats_impl(mode="7-turn"))
    assert "window" in r_7t
    assert r_7t["window"]["approx_hours"] == 168
    obs_counts = r_7t["node_counts_by_type"].get("observation_factual", {})
    assert obs_counts.get("total", 0) == 1, (
        f"Expected 1 observation in 7-turn window, got {obs_counts}"
    )


def test_mode_30_turn_includes_3_week_old_nodes():
    """mode='30-turn' must include nodes created 20 days ago."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    obs_a = _seed_observation(obs, "Node A — 20 days ago")

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE nodes SET created_at = datetime('now', '-20 days') WHERE id = ?",
        (obs_a,),
    )
    conn.commit()
    conn.close()

    r_30t = json.loads(stats._stats_impl(mode="30-turn"))
    assert "window" in r_30t
    assert r_30t["window"]["approx_hours"] == 720
    obs_counts = r_30t["node_counts_by_type"].get("observation_factual", {})
    assert obs_counts.get("total", 0) == 1, (
        f"Expected 1 observation in 30-turn window, got {obs_counts}"
    )


# ── 6. Confidence section structure ──────────────────────────────────────


def test_confidence_section_present_when_nodes_exist():
    """When confidence-bearing nodes exist, confidence section has expected subkeys."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    _seed_observation(obs, "Confidence section obs 1", quote_type="hard_data")
    r = json.loads(stats._stats_impl())
    conf = r["confidence"]
    assert "by_type" in conf, conf
    assert "by_quote_type" in conf, conf
    assert "by_reasoning_type" in conf, conf
    assert "by_source_class" in conf, conf


def test_confidence_by_type_subkeys():
    """Each populated by_type entry has n, mean, p25, p50, p75, p90, p95."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    for i in range(5):
        _seed_observation(obs, f"Confidence subkeys obs {i}", quote_type="hard_data")
    r = json.loads(stats._stats_impl())
    by_type = r["confidence"]["by_type"]
    assert "observation_factual" in by_type, by_type
    entry = by_type["observation_factual"]
    for key in ("n", "mean", "p25", "p50", "p75", "p90", "p95"):
        assert key in entry, f"Missing key '{key}' in by_type entry: {entry}"
    assert entry["n"] == 5


def test_confidence_empty_buckets_omitted():
    """Types with no nodes are omitted from by_type; we do not return {n:0, mean:0}."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    # Only seed observations — no derivations, no conjectures
    _seed_observation(obs, "Only obs", quote_type="hard_data")
    r = json.loads(stats._stats_impl())
    by_type = r["confidence"]["by_type"]
    assert "observation_factual" in by_type
    # derivation and conjecture have no nodes — must not appear
    assert "derivation" not in by_type, f"Empty derivation bucket leaked: {by_type}"
    assert "conjecture" not in by_type, f"Empty conjecture bucket leaked: {by_type}"


def test_confidence_empty_graph():
    """With no nodes, confidence section returns all-empty dicts (no crash)."""
    stats, _obs, _derive, _epistemic, _core = _make_fresh_mods()
    r = json.loads(stats._stats_impl())
    conf = r["confidence"]
    assert conf["by_type"] == {}
    assert conf["by_quote_type"] == {}
    assert conf["by_reasoning_type"] == {}
    assert conf["by_source_class"] == {}


# ── 7. Confidence by_quote_type ───────────────────────────────────────────


def test_confidence_by_quote_type_matches_aggregation():
    """by_quote_type values should match direct SQL aggregation."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    # Insert 3 hard_data obs and 2 editorial obs
    for i in range(3):
        _seed_observation(obs, f"hard_data obs {i}", quote_type="hard_data")
    for i in range(2):
        _seed_observation(obs, f"editorial obs {i}", quote_type="editorial")

    r = json.loads(stats._stats_impl())
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
    stats, obs, _derive, epistemic, _core = _make_fresh_mods()
    # Seed an axiom — axioms are confidence-bearing but NOT observation_factual
    _seed_axiom(epistemic, "Axiom claim for quote_type isolation test")
    # Seed one observation
    _seed_observation(obs, "Obs for isolation test", quote_type="hard_data")
    r = json.loads(stats._stats_impl())
    # by_quote_type should only have the observation's quote_type (not axiom's)
    # Axioms don't have quote_type, so this is really a count sanity check
    bqt = r["confidence"]["by_quote_type"]
    assert "hard_data" in bqt
    assert bqt["hard_data"]["n"] == 1


# ── 8. Confidence by_reasoning_type ──────────────────────────────────────


def test_confidence_by_reasoning_type_matches_derivations():
    """by_reasoning_type must aggregate derivation confidences by metadata field."""
    stats, obs, derive, epistemic, _core = _make_fresh_mods()
    ax_a = _seed_axiom(epistemic, "Premise A for reasoning_type test")
    ax_b = _seed_axiom(epistemic, "Premise B for reasoning_type test")
    # Two deductive derivations
    _seed_derivation(derive, "Deduction 1", [ax_a, ax_b], "deductive_modus_ponens")
    _seed_derivation(derive, "Deduction 2", [ax_a, ax_b], "deductive_modus_ponens")
    # One inductive derivation
    _seed_derivation(derive, "Induction 1", [ax_a, ax_b], "inductive_generalization")

    r = json.loads(stats._stats_impl())
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
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    # Only observations
    _seed_observation(obs, "Obs only for reasoning_type test", quote_type="hard_data")
    r = json.loads(stats._stats_impl())
    # by_reasoning_type should be empty since no derivations exist
    assert r["confidence"]["by_reasoning_type"] == {}, r["confidence"]["by_reasoning_type"]


# ── 9. Confidence by_source_class ─────────────────────────────────────────


def test_confidence_by_source_class_matches_aggregation():
    """by_source_class must aggregate observation_factual confidences by source_class."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    # 2 external + 1 introspective
    _seed_observation(obs, "External obs 1", quote_type="hard_data", source_class="external")
    _seed_observation(obs, "External obs 2", quote_type="hard_data", source_class="external")
    _seed_observation(obs, "Introspective obs 1", quote_type="hard_data", source_class="introspective")

    r = json.loads(stats._stats_impl())
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
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    _seed_observation(obs, "Sections-only obs", quote_type="hard_data")
    r = json.loads(stats._stats_impl(sections=["confidence"]))

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
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    _seed_observation(obs, "Sections+mode obs", quote_type="editorial")
    r = json.loads(stats._stats_impl(mode="1-turn", sections=["confidence"]))
    assert "confidence" in r, r
    assert "window" in r, r
    assert r["window"]["mode"] == "1-turn"
    assert "node_counts_by_type" not in r, r


def test_sections_multiple():
    """sections=['structure', 'memory'] returns only those two sections."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    _seed_observation(obs, "Multi-section obs")
    r = json.loads(stats._stats_impl(sections=["structure", "memory"]))
    assert "node_counts_by_type" in r, r
    assert "memory" in r, r
    assert "confidence" not in r, r
    assert "edge_counts_by_relation" not in r, r
    assert "reasoning_type_breakdown" not in r, r


def test_sections_health_score_no_op():
    """health_score is a recognized section name but _stats_impl doesn't compute it;
    requesting it should not error and other sections should still work."""
    stats, _obs, _derive, _epistemic, _core = _make_fresh_mods()
    # health_score is a known name — requesting it alone is a no-op (nothing returned)
    # but must not crash
    r = json.loads(stats._stats_impl(sections=["health_score"]))
    assert "error" not in r, r


# ── 11. sections selector with unknown name ───────────────────────────────


def test_sections_unknown_name_graceful():
    """Unknown section names must be silently ignored; known names still returned."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    _seed_observation(obs, "Unknown section obs")
    r = json.loads(stats._stats_impl(sections=["structure", "totally_unknown_key"]))
    # No error
    assert "error" not in r, r
    # Known section returned
    assert "node_counts_by_type" in r, r
    # Unknown section not fabricated
    assert "totally_unknown_key" not in r, r


def test_sections_all_unknown_graceful():
    """All unknown section names — should return empty (but no error)."""
    stats, _obs, _derive, _epistemic, _core = _make_fresh_mods()
    r = json.loads(stats._stats_impl(sections=["nonexistent_a", "nonexistent_b"]))
    assert "error" not in r, r
    # No known sections in response (just window if mode != all, but default is all)
    assert "node_counts_by_type" not in r, r
    assert "confidence" not in r, r


# (test_sections_non_string_elements_rejected moved to tests/test_stats_payload.py
#  — non-string-element rejection is a wrapper-layer validation, R2.)


# ── Additional: _percentile helper ───────────────────────────────────────


def test_percentile_helper_edge_cases():
    """_percentile should handle edge cases without crashing."""
    assert engram_stats._percentile([], 50) == 0.0
    assert engram_stats._percentile([0.5], 50) == 0.5
    assert abs(engram_stats._percentile([0.3, 0.7], 50) - 0.5) < 1e-10
    # [0.2, 0.4, 0.6, 0.8] at p25: idx=0.75, lo=0, hi=1, frac=0.75
    # result = 0.2 + 0.75*(0.4-0.2) = 0.2 + 0.15 = 0.35
    assert abs(engram_stats._percentile([0.2, 0.4, 0.6, 0.8], 25) - 0.35) < 1e-10
    # p0 = min, p100 = max
    vals = [0.1, 0.5, 0.9]
    assert engram_stats._percentile(vals, 0) == 0.1
    assert engram_stats._percentile(vals, 100) == 0.9


if __name__ == "__main__":
    tests = [
        test_no_arg_call_returns_all_sections,
        test_empty_payload_json_works,
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
