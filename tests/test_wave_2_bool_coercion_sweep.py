"""Tests for the wave-2 same-shape bool/numeric coercion fixes (issues #70 + #69).

Three impls hardened at their entry boundary:
  * _add_observation_impl — is_predictive isinstance check (catches both
    single-observation wrapper and the batch processor)
  * _resolve_impl — use_stale isinstance check
  * _report_feeling_impl — intensity_hint try/float-coerce + range check

All three follow the ls_0034 pattern: JSON-string "false" / "0.6" is
truthy / non-numeric in Python and would silently corrupt state without
explicit type discipline at the wrapper boundary.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_wave2_sweep")


def fresh_server():
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
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_observation(server, claim="x claim", url="https://example.com/test", is_predictive=None) -> str:
    payload = {
        "url": url,
        "title": "Test source",
        "quoted_text": claim,
        "interpretation": f"Interpretation: {claim}",
        "claim": claim,
        "quote_type": "hard_data",
    }
    if is_predictive is not None:
        payload["is_predictive"] = is_predictive
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    return r


# ---------------------------------------------------------------------------
# Issue #70 — is_predictive bool-string truthy
# ---------------------------------------------------------------------------


class TestObservationIsPredictiveBool:
    def test_actual_bool_false_creates_factual(self):
        server = fresh_server()
        r = _add_observation(server, is_predictive=False)
        assert r.get("status") == "created"
        # Verify type tag in DB
        conn = server._get_db()
        node_type = conn.execute(
            "SELECT type FROM nodes WHERE id = ?", (r["observation_id"],)
        ).fetchone()[0]
        assert node_type == "observation_factual"

    def test_string_false_rejected(self):
        # Reproducer from issue #70: "false" string should NOT silently
        # create a predictive observation.
        server = fresh_server()
        r = _add_observation(server, is_predictive="false")
        assert "error" in r
        assert "is_predictive" in r["error"]
        assert "boolean" in r["error"].lower()

    def test_string_true_rejected(self):
        server = fresh_server()
        r = _add_observation(server, is_predictive="true")
        assert "error" in r
        assert "is_predictive" in r["error"]

    def test_integer_one_rejected(self):
        # int 1 is also falsely truthy through `**params` — covered by the
        # same isinstance(...) check (since `isinstance(1, bool)` is False
        # in Python despite True/False being int subclasses; the check
        # accepts True/False and rejects int).
        # Actually: in Python, `isinstance(True, int) == True` BUT
        # `isinstance(1, bool) == False`. The check `isinstance(is_predictive, bool)`
        # accepts True/False (which are also bool instances) but rejects int 1.
        server = fresh_server()
        r = _add_observation(server, is_predictive=1)
        assert "error" in r

    def test_batch_string_false_rejected_per_obs(self):
        # Batch path: each per-obs is_predictive passes through to
        # _add_observation_impl, which now enforces the check.
        server = fresh_server()
        observations = [
            {
                "quoted_text": "ok obs",
                "interpretation": "valid",
                "claim": "valid claim",
                "quote_type": "hard_data",
                "is_predictive": False,  # actual bool — should succeed
            },
            {
                "quoted_text": "bad obs",
                "interpretation": "string-bool",
                "claim": "bad claim",
                "quote_type": "hard_data",
                "is_predictive": "false",  # STRING — should error per-obs
            },
        ]
        r = json.loads(server.engram_add_observation_batch(payload_json=json.dumps({
            "observations_json": json.dumps(observations),
            "url": "https://example.com/batch",
            "title": "Batch test source",
        })))
        # Per-obs semantics: obs[0] succeeds (actual bool), obs[1] errors
        # with an is_predictive message. Whole-batch isn't aborted.
        per_obs = r.get("results") or r.get("observations") or []
        assert isinstance(per_obs, list)
        assert len(per_obs) == 2, f"expected 2 per-obs entries, got {len(per_obs)}: {per_obs}"
        # Index 0: actual bool → success path
        assert per_obs[0].get("status") == "created" or "error" not in per_obs[0], (
            f"expected obs[0] to succeed, got {per_obs[0]}"
        )
        # Index 1: string "false" → per-obs error mentioning is_predictive
        obs1_error = per_obs[1].get("error", "")
        assert "is_predictive" in obs1_error, (
            f"expected obs[1] error to mention is_predictive, got {per_obs[1]}"
        )


# ---------------------------------------------------------------------------
# Issue #70 — use_stale bool-string truthy on engram_resolve
# ---------------------------------------------------------------------------


class TestResolveUseStaleBool:
    def test_actual_bool_use_stale_accepted(self):
        server = fresh_server()
        # Just call with use_stale=False; we don't need a real resolve to fire
        # — the isinstance check happens before any validation of target_id.
        r = json.loads(server.engram_resolve(payload_json=json.dumps({
            "target_id": "qu_nonexistent",
            "claim": "test resolution",
            "supporting_ids": "",
            "logical_chain": "test chain",
            "use_stale": False,
        })))
        # The isinstance check passes; downstream may error on target lookup
        # but the error won't be about use_stale.
        if "error" in r:
            assert "use_stale" not in r["error"]

    def test_string_false_rejected(self):
        server = fresh_server()
        r = json.loads(server.engram_resolve(payload_json=json.dumps({
            "target_id": "qu_nonexistent",
            "claim": "test resolution",
            "supporting_ids": "",
            "logical_chain": "test chain",
            "use_stale": "false",  # STRING — should error
        })))
        assert "error" in r
        assert "use_stale" in r["error"]
        assert "boolean" in r["error"].lower()


# ---------------------------------------------------------------------------
# Issue #69 — intensity_hint type coercion on engram_report_feeling
# ---------------------------------------------------------------------------


class TestReportFeelingIntensityHintCoercion:
    def test_float_accepted(self):
        server = fresh_server()
        r = json.loads(server.engram_report_feeling(payload_json=json.dumps({
            "reported_state": "curious",
            "trigger": "test trigger",
            "intensity_hint": 0.6,
        })))
        assert "error" not in r or "intensity_hint" not in r.get("error", "")

    def test_int_accepted(self):
        server = fresh_server()
        r = json.loads(server.engram_report_feeling(payload_json=json.dumps({
            "reported_state": "test",
            "trigger": "test trigger",
            "intensity_hint": 1,  # int 1 — should coerce cleanly
        })))
        assert "error" not in r or "intensity_hint" not in r.get("error", "")

    def test_string_numeric_coerced(self):
        # Reproducer from issue #69 — string "0.6" should now coerce to
        # 0.6 cleanly, not raise TypeError downstream.
        server = fresh_server()
        r = json.loads(server.engram_report_feeling(payload_json=json.dumps({
            "reported_state": "curious",
            "trigger": "test trigger",
            "intensity_hint": "0.6",  # STRING — should coerce
        })))
        assert "error" not in r or "intensity_hint" not in r.get("error", "")

    def test_string_non_numeric_rejected(self):
        server = fresh_server()
        r = json.loads(server.engram_report_feeling(payload_json=json.dumps({
            "reported_state": "test",
            "trigger": "test trigger",
            "intensity_hint": "very high",  # NON-NUMERIC string — should reject
        })))
        assert "error" in r
        assert "intensity_hint" in r["error"]

    def test_sentinel_minus_one_accepted_without_coerce(self):
        # -1.0 is the documented "not provided" sentinel and should pass
        # through unchanged (no coercion attempted).
        server = fresh_server()
        r = json.loads(server.engram_report_feeling(payload_json=json.dumps({
            "reported_state": "test",
            "trigger": "test trigger",
            "intensity_hint": -1.0,
        })))
        assert "error" not in r or "intensity_hint" not in r.get("error", "")
