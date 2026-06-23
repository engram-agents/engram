"""Tests for the counterfactual_inference quote_type (issue #1160).

Covers:
  1. counterfactual_inference is a valid quote_type (not rejected by validation)
  2. Confidence calibrated to 0.65 (below attributed_analysis=0.70)
  3. source_class=introspective discount still applies (0.65 × 0.95 ≈ 0.618)
  4. CONFIDENCE_MAP contains the new key
  5. engram-pkg CLI choices include counterfactual_inference
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_counterfactual_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server  # noqa: F401 — side effect: initialises fresh DB in TEST_DIR


def setup_module():
    os.environ["ENGRAM_DATA_DIR"] = TEST_DIR
    fresh_server()


def teardown_module():
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ.pop("ENGRAM_DATA_DIR", None)


# ── Unit: CONFIDENCE_MAP contains the new key ─────────────────────────────


def test_confidence_map_contains_counterfactual_inference():
    from engram_confidence import CONFIDENCE_MAP, VALID_QUOTE_TYPES
    assert "counterfactual_inference" in CONFIDENCE_MAP, (
        "counterfactual_inference missing from CONFIDENCE_MAP"
    )
    assert "counterfactual_inference" in VALID_QUOTE_TYPES, (
        "counterfactual_inference missing from VALID_QUOTE_TYPES"
    )


def test_counterfactual_confidence_below_attributed_analysis():
    from engram_confidence import CONFIDENCE_MAP
    assert CONFIDENCE_MAP["counterfactual_inference"] < CONFIDENCE_MAP["attributed_analysis"], (
        "counterfactual_inference should be below attributed_analysis (carries approximation residual)"
    )
    assert CONFIDENCE_MAP["counterfactual_inference"] == 0.65


# ── Integration: observation creation ────────────────────────────────────


def test_counterfactual_inference_is_valid_quote_type():
    """Filing with counterfactual_inference must succeed (not raise invalid quote_type)."""
    import server
    payload = {
        "quoted_text": "The policy reduced unemployment by ~2pp in treated counties.",
        "interpretation": "Difference-in-differences estimate of the labor-market effect.",
        "claim": "The policy reduced unemployment by ~2pp in treated counties.",
        "quote_type": "counterfactual_inference",
        "url": "https://example.com/study",
        "title": "Labor market impact study",
    }
    r = json.loads(server.engram_add_observation(json.dumps(payload)))
    assert "error" not in r, f"Unexpected error: {r.get('error')}"
    assert r.get("observation_id"), "Expected an observation_id in the response"


def test_counterfactual_inference_confidence():
    """Confidence stored must be 0.65 for external source_class."""
    import server
    payload = {
        "quoted_text": "Treated cohort showed 3.1% lower recidivism.",
        "interpretation": "IV estimate from prison reform RCT.",
        "claim": "Prison reform reduced recidivism by ~3pp in the treated cohort.",
        "quote_type": "counterfactual_inference",
        "url": "https://example.com/rct",
        "title": "RCT study",
        "source_class": "external",
    }
    r = json.loads(server.engram_add_observation(json.dumps(payload)))
    assert "error" not in r, r.get("error")
    assert r.get("confidence") == 0.65, (
        f"Expected confidence 0.65 for counterfactual_inference/external, got {r.get('confidence')}"
    )


def test_counterfactual_inference_introspective_discount():
    """Introspective source_class applies ×0.95 discount → 0.65 × 0.95 ≈ 0.618."""
    import server
    payload = {
        "quoted_text": "Had the model trained on more diverse data, bias would have been lower.",
        "interpretation": "My own counterfactual reasoning about training data.",
        "claim": "More diverse training data would have reduced model bias.",
        "quote_type": "counterfactual_inference",
        "source_class": "introspective",
        "url": "https://example.com/training-data-analysis",
        "title": "Training data diversity analysis",
    }
    r = json.loads(server.engram_add_observation(json.dumps(payload)))
    assert "error" not in r, r.get("error")
    expected = round(0.65 * 0.95, 3)
    assert abs(r.get("confidence", 0) - expected) < 0.002, (
        f"Expected confidence ≈{expected} for counterfactual_inference/introspective, "
        f"got {r.get('confidence')}"
    )
