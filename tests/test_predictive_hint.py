"""Tests for the Stage 1.4 (#2252) advisory predictive_hint affordance.

Ratified design (#2252 SETTLED Q1): pr_* prediction nodes are non-claim-bearing;
the paired claim-bearing predictive OBSERVATION carries claim + confidence.
That pairing is what preserves calibration. The gap measured is discoverability,
not correctness — so this is a write-path hint, never a validator, never
blocking.

Trigger A only — the claim-text regex trigger (Trigger B) was removed
pre-merge: colleague review of #2260 measured it at zero precision on a
real corpus (0/22 sampled fires were genuine predictions). Redesign is
tracked in #2264.

Coverage:
  1. Trigger A — predicted_event passed without is_predictive -> hint key present.
  2. Negative control — ordinary claim, no predictive params -> no hint key.
  3. Suppression — is_predictive=true + predicted_event -> no hint key.
  4. Result invariants — hint presence changes nothing else in the result shape.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_predictive_hint_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
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


# ── 1. Trigger A — parameter-based ──────────────────────────────────────────


def test_trigger_a_predicted_event_without_is_predictive_hints():
    server = fresh_server()
    payload = {
        "url": "https://example.com/trigger-a",
        "title": "Trigger A",
        "quoted_text": "Analysts expect the merger to close",
        "interpretation": "Analysts stated a closing expectation",
        "claim": "The merger closes next fiscal quarter",
        "quote_type": "attributed_analysis",
        # is_predictive omitted (falsy) but predicted_event supplied.
        "predicted_event": "Merger closes",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "predictive_hint" in r, f"Expected predictive_hint key, got {r}"


# ── 2. Negative control ──────────────────────────────────────────────────


def test_negative_control_ordinary_claim_no_hint():
    # Under Trigger-A-only, this passes trivially — no predictive params
    # (predicted_event/resolution_timeframe) are supplied, so _predictive_hint
    # short-circuits regardless of claim text. Kept as the no-params
    # negative control (formerly also exercised the removed claim-text
    # regex trigger, Trigger B).
    server = fresh_server()
    payload = {
        "url": "https://example.com/negative-control",
        "title": "Negative control",
        "quoted_text": "The bridge was completed in 1937",
        "interpretation": "A historical fact about a completed structure",
        "claim": "The Golden Gate Bridge was completed in 1937",
        "quote_type": "hard_data",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "predictive_hint" not in r, f"Did not expect predictive_hint, got {r}"


# ── 3. Suppression ──────────────────────────────────────────────────────


def test_suppression_when_is_predictive_true():
    server = fresh_server()
    payload = {
        "url": "https://example.com/suppression",
        "title": "Suppression",
        "quoted_text": "We predict the merger will close by 2027",
        "interpretation": "Author's own prediction, properly filed",
        "claim": "The merger will close by 2027",
        "quote_type": "editorial",
        "is_predictive": True,
        "predicted_event": "Merger closes",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "predictive_hint" not in r, f"Did not expect predictive_hint, got {r}"
    # Sanity: the properly-paired path still creates the prediction node.
    assert r.get("prediction_status") in ("created", "linked_to_existing"), r


# ── 4. Result invariants ──────────────────────────────────────────────────


def test_result_invariants_hint_changes_nothing_else():
    server = fresh_server()
    hinted_payload = {
        "url": "https://example.com/invariants-hinted",
        "title": "Invariants hinted",
        "quoted_text": "The market will rebound",
        "interpretation": "A forward-looking market claim",
        "claim": "The market will rebound",
        "quote_type": "editorial",
        # Trigger A: predicted_event without is_predictive.
        "predicted_event": "Market rebounds",
    }
    plain_payload = {
        "url": "https://example.com/invariants-plain",
        "title": "Invariants plain",
        "quoted_text": "The market rebounded last year",
        "interpretation": "A past-tense market claim",
        "claim": "The market rebounded last year",
        "quote_type": "editorial",
    }
    r_hinted = json.loads(server.engram_add_observation(payload_json=json.dumps(hinted_payload)))
    r_plain = json.loads(server.engram_add_observation(payload_json=json.dumps(plain_payload)))

    assert "predictive_hint" in r_hinted, r_hinted
    assert "predictive_hint" not in r_plain, r_plain

    assert r_hinted.get("status") == "created", r_hinted
    assert r_hinted["observation_id"].startswith("ob_"), r_hinted

    # Every pre-existing key present on the no-hint filing must also be
    # present, with the same value shape, on the hinted filing (minus the
    # hint key itself and any fields that legitimately differ per-call,
    # e.g. observation_id/evidence_id which are freshly minted per node).
    _PER_CALL_KEYS = {"observation_id", "evidence_id"}
    for key in r_plain:
        if key in _PER_CALL_KEYS:
            continue
        assert key in r_hinted, (
            f"Key '{key}' present on the no-hint filing is missing on the "
            f"hinted filing: {r_hinted}"
        )
