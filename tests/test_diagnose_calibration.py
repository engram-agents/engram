"""Tests for engram_diagnose calibration dimension (PR diagnose/calibration-dimension-from-stats).

Coverage:
  1. calibration key present in response.
  2. calibration.corpus and calibration.this_turn are populated dicts.
  3. calibration.drift_by_type only includes types with ≥3 samples in this_turn.
  4. Drift delta is signed (positive when this_turn higher than corpus, negative when lower).
  5. health_score not affected by calibration data (weight=0 verified by mutating calibration
     and confirming health_score is unchanged).
  6. Defensive: when engram_stats raises, calibration key contains error and other dimensions
     still complete.

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
import unittest.mock as mock
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

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_diagnose_calibration_")
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


def _seed_axiom(epistemic, claim: str) -> str:
    """Insert a single axiom and return its ID."""
    r = json.loads(epistemic._add_axiom_impl(
        claim=claim,
        basis="test fixture basis",
    ))
    assert "axiom_id" in r, f"axiom creation failed: {r}"
    return r["axiom_id"]


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


# ── 1. calibration key present ───────────────────────────────────────────────


def test_calibration_key_present():
    """engram_diagnose must include a 'calibration' key in its response."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    _seed_observation(obs, "Calibration presence test obs")
    r = json.loads(stats._diagnose_impl())
    assert "metrics" in r or "calibration" in r, (
        "expected calibration in top-level or under metrics"
    )
    # engram_diagnose returns a flat dict (no 'metrics' wrapper)
    assert "calibration" in r, f"calibration key missing from diagnose output. Keys: {list(r.keys())}"


# ── 2. calibration.corpus and calibration.this_turn populated ────────────────


def test_calibration_corpus_and_this_turn_present():
    """calibration must have corpus and this_turn sub-dicts."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    _seed_observation(obs, "Corpus/this_turn test obs 1")
    _seed_observation(obs, "Corpus/this_turn test obs 2")
    r = json.loads(stats._diagnose_impl())
    cal = r["calibration"]
    assert "corpus" in cal, f"calibration.corpus missing: {cal}"
    assert "this_turn" in cal, f"calibration.this_turn missing: {cal}"
    assert isinstance(cal["corpus"], dict), f"corpus must be a dict: {cal['corpus']}"
    assert isinstance(cal["this_turn"], dict), f"this_turn must be a dict: {cal['this_turn']}"


def test_calibration_corpus_has_confidence_subkeys():
    """calibration.corpus must contain the standard confidence-section subkeys."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    for i in range(3):
        _seed_observation(obs, f"Confidence corpus obs {i}")
    r = json.loads(stats._diagnose_impl())
    corpus = r["calibration"]["corpus"]
    # engram_stats confidence section always returns these 4 sub-dicts
    for key in ("by_type", "by_quote_type", "by_reasoning_type", "by_source_class"):
        assert key in corpus, f"calibration.corpus missing '{key}': {corpus}"


# ── 3. drift_by_type only includes types with ≥3 this_turn samples ───────────


def test_drift_by_type_requires_3_samples():
    """drift_by_type must exclude types where this_turn has fewer than 3 samples."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    # Seed 2 corpus observations (backdated so they're older than 24h)
    # The 48h backdate ensures these fall outside the 1-turn (24h) window so
    # they do not count toward this_turn — only toward the corpus baseline.
    obs_old_1 = _seed_observation(obs, "Old obs for drift test 1")
    obs_old_2 = _seed_observation(obs, "Old obs for drift test 2")

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE nodes SET created_at = datetime('now', '-48 hours') "
        "WHERE id IN (?, ?)",
        (obs_old_1, obs_old_2),
    )
    conn.commit()
    conn.close()

    # Seed only 2 recent observations — not enough for drift reporting (need ≥3)
    _seed_observation(obs, "Recent obs for drift test 1")
    _seed_observation(obs, "Recent obs for drift test 2")

    r = json.loads(stats._diagnose_impl())
    drift = r["calibration"]["drift_by_type"]
    # observation_factual has only 2 this_turn samples — must NOT appear in drift
    assert "observation_factual" not in drift, (
        f"observation_factual with <3 this_turn samples leaked into drift: {drift}"
    )


def test_drift_by_type_includes_types_with_3_or_more_samples():
    """drift_by_type must include types where this_turn has ≥3 samples and corpus exists."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    # Seed corpus observations (older than 24h)
    corpus_obs = [_seed_observation(obs, f"Corpus obs {i}") for i in range(5)]
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    for obs_id in corpus_obs:
        conn.execute(
            "UPDATE nodes SET created_at = datetime('now', '-48 hours') WHERE id = ?",
            (obs_id,),
        )
    conn.commit()
    conn.close()

    # Seed ≥3 recent observations (within 1-turn window)
    for i in range(3):
        _seed_observation(obs, f"Recent obs for drift ≥3 test {i}")

    r = json.loads(stats._diagnose_impl())
    drift = r["calibration"]["drift_by_type"]
    # observation_factual now has 3 this_turn samples and corpus exists — must appear
    assert "observation_factual" in drift, (
        f"observation_factual with ≥3 this_turn samples missing from drift: {drift}"
    )
    entry = drift["observation_factual"]
    assert "this_turn_p50" in entry, entry
    assert "corpus_p50" in entry, entry
    assert "delta" in entry, entry
    assert "n_this_turn" in entry, entry
    assert entry["n_this_turn"] == 3, f"expected n_this_turn=3, got {entry['n_this_turn']}"


# ── 4. Drift delta is signed correctly ───────────────────────────────────────


def test_drift_delta_signed_positive_when_this_turn_higher():
    """delta must be positive when this_turn p50 exceeds corpus p50."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    # Corpus: editorial (low conf ~0.35); recent: hard_data (higher conf ~0.95).
    # hard_data conf > editorial conf → delta positive for observation_factual.

    corpus_obs = [
        _seed_observation(obs, f"Editorial corpus obs {i}", quote_type="editorial")
        for i in range(5)
    ]
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    for obs_id in corpus_obs:
        conn.execute(
            "UPDATE nodes SET created_at = datetime('now', '-48 hours') WHERE id = ?",
            (obs_id,),
        )
    conn.commit()
    conn.close()

    # Seed 3 recent hard_data observations (higher confidence than editorial)
    for i in range(3):
        _seed_observation(obs, f"Hard data recent obs {i}", quote_type="hard_data")

    r = json.loads(stats._diagnose_impl())
    drift = r["calibration"]["drift_by_type"]
    assert "observation_factual" in drift, f"drift missing obs_factual: {drift}"
    entry = drift["observation_factual"]
    # hard_data conf (0.95) > editorial conf (0.35) → delta > 0
    assert entry["delta"] > 0, (
        f"Expected positive delta (this_turn=hard_data, corpus=editorial), got {entry}"
    )


def test_drift_delta_signed_negative_when_this_turn_lower():
    """delta must be negative when this_turn p50 is below corpus p50."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    # Corpus: hard_data (high conf); recent: editorial (lower conf)
    corpus_obs = [
        _seed_observation(obs, f"HD corpus obs {i}", quote_type="hard_data")
        for i in range(5)
    ]
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    for obs_id in corpus_obs:
        conn.execute(
            "UPDATE nodes SET created_at = datetime('now', '-48 hours') WHERE id = ?",
            (obs_id,),
        )
    conn.commit()
    conn.close()

    # Seed 3 recent editorial observations (lower confidence)
    for i in range(3):
        _seed_observation(obs, f"Editorial recent obs {i}", quote_type="editorial")

    r = json.loads(stats._diagnose_impl())
    drift = r["calibration"]["drift_by_type"]
    assert "observation_factual" in drift, f"drift missing obs_factual: {drift}"
    entry = drift["observation_factual"]
    # editorial conf (0.35) < hard_data conf (0.95) → delta < 0
    assert entry["delta"] < 0, (
        f"Expected negative delta (this_turn=editorial, corpus=hard_data), got {entry}"
    )


# ── 5. health_score not affected by calibration (weight=0) ───────────────────


def test_health_score_unaffected_by_calibration():
    """health_score must not change when calibration data changes (weight=0).

    Strategy: run engram_diagnose, capture health_score. Then mock
    _compute_confidence_distribution to return wildly different confidence
    numbers. health_score must be identical (calibration_weight=0).

    After Fix 1 (PR #245 round-2), engram_diagnose calls _compute_confidence_distribution
    directly (not engram_stats), so we mock the helper instead.
    """
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    for i in range(3):
        _seed_observation(obs, f"Health score weight=0 obs {i}")

    # First run: real calibration
    r1 = json.loads(stats._diagnose_impl())
    score1 = r1["health_score"]
    assert score1 is not None, "health_score missing from first run"

    # Second run: mock _compute_confidence_distribution to return garbage values
    extreme_confidence = {
        "by_type": {
            "observation_factual": {
                "n": 9999,
                "mean": 0.01,
                "p25": 0.01,
                "p50": 0.01,
                "p75": 0.01,
                "p90": 0.01,
                "p95": 0.01,
            }
        },
        "by_quote_type": {},
        "by_reasoning_type": {},
        "by_source_class": {},
    }
    with mock.patch("engram_stats._compute_confidence_distribution", return_value=extreme_confidence):
        r2 = json.loads(stats._diagnose_impl())
    score2 = r2["health_score"]

    assert score1 == score2, (
        f"health_score changed after calibration mock (weight=0 violation): "
        f"{score1} → {score2}"
    )


# ── 6. Defensive: engram_stats failure does not break diagnose ────────────────


def test_calibration_defensive_on_engram_stats_failure():
    """When _compute_confidence_distribution raises, calibration contains error key
    and the rest of the diagnose output (structure, health_score, etc.) is intact.

    After Fix 1 (PR #245 round-2), engram_diagnose calls _compute_confidence_distribution
    directly (not engram_stats), so we mock the helper instead.
    """
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    _seed_observation(obs, "Defensive test obs")

    with mock.patch(
        "engram_stats._compute_confidence_distribution",
        side_effect=RuntimeError("stats broken"),
    ):
        r = json.loads(stats._diagnose_impl())

    # calibration section must exist and contain the error sentinel
    assert "calibration" in r, f"calibration key missing: {list(r.keys())}"
    cal = r["calibration"]
    assert "error" in cal, f"calibration must contain 'error' key on failure: {cal}"
    assert "calibration stats unavailable" in cal["error"], (
        f"error message unexpected: {cal['error']}"
    )

    # Other dimensions must still be present and intact
    for dimension in ("structure", "epistemic", "memory", "provenance", "experience"):
        assert dimension in r, f"dimension '{dimension}' missing after stats failure"
    assert "health_score" in r, "health_score missing after stats failure"


def test_calibration_defensive_on_engram_stats_bad_json():
    """When _compute_confidence_distribution raises, calibration must contain error key.

    After Fix 1 (PR #245 round-2), engram_diagnose calls _compute_confidence_distribution
    directly. We mock it to raise (the old 'bad JSON' surface no longer applies since
    direct helper calls return dicts, not JSON strings).
    """
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    _seed_observation(obs, "Bad JSON defensive test obs")

    with mock.patch(
        "engram_stats._compute_confidence_distribution",
        side_effect=ValueError("simulated helper failure"),
    ):
        r = json.loads(stats._diagnose_impl())

    assert "calibration" in r
    cal = r["calibration"]
    assert "error" in cal, f"expected error key on helper failure, got: {cal}"


# ── drift_by_type keys structure ──────────────────────────────────────────────


def test_drift_by_type_entry_structure():
    """Each entry in drift_by_type has the expected 4 keys."""
    stats, obs, _derive, _epistemic, _core = _make_fresh_mods()
    # corpus: 5 backdated obs; this_turn: 3 recent obs
    corpus_obs = [_seed_observation(obs, f"Drift struct corpus obs {i}") for i in range(5)]
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    for obs_id in corpus_obs:
        conn.execute(
            "UPDATE nodes SET created_at = datetime('now', '-48 hours') WHERE id = ?",
            (obs_id,),
        )
    conn.commit()
    conn.close()
    for i in range(3):
        _seed_observation(obs, f"Drift struct recent obs {i}")

    r = json.loads(stats._diagnose_impl())
    drift = r["calibration"]["drift_by_type"]
    assert "observation_factual" in drift, drift
    entry = drift["observation_factual"]
    for key in ("this_turn_p50", "corpus_p50", "delta", "n_this_turn"):
        assert key in entry, f"drift entry missing '{key}': {entry}"


if __name__ == "__main__":
    tests = [
        test_calibration_key_present,
        test_calibration_corpus_and_this_turn_present,
        test_calibration_corpus_has_confidence_subkeys,
        test_drift_by_type_requires_3_samples,
        test_drift_by_type_includes_types_with_3_or_more_samples,
        test_drift_delta_signed_positive_when_this_turn_higher,
        test_drift_delta_signed_negative_when_this_turn_lower,
        test_health_score_unaffected_by_calibration,
        test_calibration_defensive_on_engram_stats_failure,
        test_calibration_defensive_on_engram_stats_bad_json,
        test_drift_by_type_entry_structure,
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
