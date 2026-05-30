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
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_diagnose_calibration_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Return a freshly initialised server module with an empty test DB."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


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


def _seed_axiom(server, claim: str) -> str:
    """Insert a single axiom and return its ID."""
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": claim,
        "basis": "test fixture basis",
    })))
    assert "axiom_id" in r, f"axiom creation failed: {r}"
    return r["axiom_id"]


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


# ── 1. calibration key present ───────────────────────────────────────────────


def test_calibration_key_present():
    """engram_diagnose must include a 'calibration' key in its response."""
    server = fresh_server()
    _seed_observation(server, "Calibration presence test obs")
    r = json.loads(server.engram_diagnose())
    assert "metrics" in r or "calibration" in r, (
        "expected calibration in top-level or under metrics"
    )
    # engram_diagnose returns a flat dict (no 'metrics' wrapper)
    assert "calibration" in r, f"calibration key missing from diagnose output. Keys: {list(r.keys())}"


# ── 2. calibration.corpus and calibration.this_turn populated ────────────────


def test_calibration_corpus_and_this_turn_present():
    """calibration must have corpus and this_turn sub-dicts."""
    server = fresh_server()
    _seed_observation(server, "Corpus/this_turn test obs 1")
    _seed_observation(server, "Corpus/this_turn test obs 2")
    r = json.loads(server.engram_diagnose())
    cal = r["calibration"]
    assert "corpus" in cal, f"calibration.corpus missing: {cal}"
    assert "this_turn" in cal, f"calibration.this_turn missing: {cal}"
    assert isinstance(cal["corpus"], dict), f"corpus must be a dict: {cal['corpus']}"
    assert isinstance(cal["this_turn"], dict), f"this_turn must be a dict: {cal['this_turn']}"


def test_calibration_corpus_has_confidence_subkeys():
    """calibration.corpus must contain the standard confidence-section subkeys."""
    server = fresh_server()
    for i in range(3):
        _seed_observation(server, f"Confidence corpus obs {i}")
    r = json.loads(server.engram_diagnose())
    corpus = r["calibration"]["corpus"]
    # engram_stats confidence section always returns these 4 sub-dicts
    for key in ("by_type", "by_quote_type", "by_reasoning_type", "by_source_class"):
        assert key in corpus, f"calibration.corpus missing '{key}': {corpus}"


# ── 3. drift_by_type only includes types with ≥3 this_turn samples ───────────


def test_drift_by_type_requires_3_samples():
    """drift_by_type must exclude types where this_turn has fewer than 3 samples."""
    server = fresh_server()
    # Seed 2 corpus observations (backdated so they're older than 24h)
    # The 48h backdate ensures these fall outside the 1-turn (24h) window so
    # they do not count toward this_turn — only toward the corpus baseline.
    obs_old_1 = _seed_observation(server, "Old obs for drift test 1")
    obs_old_2 = _seed_observation(server, "Old obs for drift test 2")

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
    _seed_observation(server, "Recent obs for drift test 1")
    _seed_observation(server, "Recent obs for drift test 2")

    r = json.loads(server.engram_diagnose())
    drift = r["calibration"]["drift_by_type"]
    # observation_factual has only 2 this_turn samples — must NOT appear in drift
    assert "observation_factual" not in drift, (
        f"observation_factual with <3 this_turn samples leaked into drift: {drift}"
    )


def test_drift_by_type_includes_types_with_3_or_more_samples():
    """drift_by_type must include types where this_turn has ≥3 samples and corpus exists."""
    server = fresh_server()
    # Seed corpus observations (older than 24h)
    corpus_obs = [_seed_observation(server, f"Corpus obs {i}") for i in range(5)]
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
        _seed_observation(server, f"Recent obs for drift ≥3 test {i}")

    r = json.loads(server.engram_diagnose())
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
    server = fresh_server()
    # Seed corpus derivations with lower confidence (manually set via supersede pattern
    # is complex; instead, seed axioms which get 0.95 confidence, then seed derivations
    # with lower-confidence premises.
    # Simpler approach: use axioms for corpus (high conf ~0.95), then derivations for
    # this_turn — but derivation confidence inherits from premises.
    # Cleanest: backdated observations (editorial, conf ~0.35) for corpus,
    # recent observations (hard_data, conf ~0.95) for this_turn.
    # hard_data conf > editorial conf → delta positive for observation_factual.

    corpus_obs = [
        _seed_observation(server, f"Editorial corpus obs {i}", quote_type="editorial")
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
        _seed_observation(server, f"Hard data recent obs {i}", quote_type="hard_data")

    r = json.loads(server.engram_diagnose())
    drift = r["calibration"]["drift_by_type"]
    assert "observation_factual" in drift, f"drift missing obs_factual: {drift}"
    entry = drift["observation_factual"]
    # hard_data conf (0.95) > editorial conf (0.35) → delta > 0
    assert entry["delta"] > 0, (
        f"Expected positive delta (this_turn=hard_data, corpus=editorial), got {entry}"
    )


def test_drift_delta_signed_negative_when_this_turn_lower():
    """delta must be negative when this_turn p50 is below corpus p50."""
    server = fresh_server()
    # Corpus: hard_data (high conf); recent: editorial (lower conf)
    corpus_obs = [
        _seed_observation(server, f"HD corpus obs {i}", quote_type="hard_data")
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
        _seed_observation(server, f"Editorial recent obs {i}", quote_type="editorial")

    r = json.loads(server.engram_diagnose())
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
    server = fresh_server()
    for i in range(3):
        _seed_observation(server, f"Health score weight=0 obs {i}")

    # First run: real calibration
    r1 = json.loads(server.engram_diagnose())
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
    with mock.patch.object(
        server, "_compute_confidence_distribution", return_value=extreme_confidence
    ):
        r2 = json.loads(server.engram_diagnose())
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
    server = fresh_server()
    _seed_observation(server, "Defensive test obs")

    with mock.patch.object(
        server, "_compute_confidence_distribution",
        side_effect=RuntimeError("stats broken"),
    ):
        r = json.loads(server.engram_diagnose())

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
    server = fresh_server()
    _seed_observation(server, "Bad JSON defensive test obs")

    with mock.patch.object(
        server, "_compute_confidence_distribution",
        side_effect=ValueError("simulated helper failure"),
    ):
        r = json.loads(server.engram_diagnose())

    assert "calibration" in r
    cal = r["calibration"]
    assert "error" in cal, f"expected error key on helper failure, got: {cal}"


# ── drift_by_type keys structure ──────────────────────────────────────────────


def test_drift_by_type_entry_structure():
    """Each entry in drift_by_type has the expected 4 keys."""
    server = fresh_server()
    # corpus: 5 backdated obs; this_turn: 3 recent obs
    corpus_obs = [_seed_observation(server, f"Drift struct corpus obs {i}") for i in range(5)]
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
        _seed_observation(server, f"Drift struct recent obs {i}")

    r = json.loads(server.engram_diagnose())
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
