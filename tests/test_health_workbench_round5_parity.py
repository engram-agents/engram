"""Tests for the Curve Workbench's round-5 pure helpers
(engram-alpha#2738, round 5): ``viz/static/health-workbench-curve.js``'s
``formatCheckpointLabel``, ``rescaleToReference``, ``collapseUnavailableRuns``,
and ``dominantReasonKey``.

Same node-under-subprocess convention as the sibling
``tests/test_health_workbench_round4_parity.py`` (run the real standalone
JS file under ``node``, cleanly skip if ``node`` is absent on this seat --
never fake the JS side).

None of these four helpers have a Python original to compare against --
all are new, workbench-only UI helpers, not ports of anything in
``health/``. So, like the round-3/round-4 helpers, this file asserts each
helper's documented CONTRACT (see its own comment in
health-workbench-curve.js) by running it under node and checking the
returned values directly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

_CURVE_JS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "viz"
    / "static"
    / "health-workbench-curve.js"
)

_NODE_AVAILABLE = shutil.which("node") is not None


@unittest.skipUnless(_NODE_AVAILABLE, "node is not available on this seat")
class TestRound5Helpers(unittest.TestCase):
    def _run_js(self, expr):
        script = f"""
        const mod = require({json.dumps(str(_CURVE_JS_PATH))});
        console.log(JSON.stringify({expr}));
        """
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return json.loads(result.stdout)

    # -- formatCheckpointLabel -----------------------------------------

    def test_fresh_checkpoint_no_age_suffix(self):
        # written_at == nowMs -- fresh, no "(<N> h/d ago)" suffix at all.
        now_ms = 1893456000000  # 2030-01-01T00:00:00Z, arbitrary fixed instant
        provenance = {
            "snapshot_id": "5-nap-20300101T000000Z",
            "turn": 5,
            "checkpoint_mode": "nap",
            "written_at": "2030-01-01T00:00:00+00:00",
        }
        out = self._run_js(
            f"mod.formatCheckpointLabel({json.dumps(provenance)}, {now_ms})"
        )
        self.assertTrue(out.startswith("latest checkpoint · "))
        self.assertIn("(nap, turn 5)", out)
        self.assertNotRegex(out, r"\(\d+ [hd] ago\)")

    def test_30h_old_shows_hours_suffix(self):
        written_ms = 1893456000000
        now_ms = written_ms + 30 * 3600000  # 30 hours later
        provenance = {
            "turn": 7, "checkpoint_mode": "sleep",
            "written_at": "2030-01-01T00:00:00+00:00",
        }
        out = self._run_js(
            f"mod.formatCheckpointLabel({json.dumps(provenance)}, {now_ms})"
        )
        self.assertIn("(30 h ago)", out)

    def test_9d_old_shows_days_suffix(self):
        written_ms = 1893456000000
        now_ms = written_ms + 9 * 86400000  # 9 days later
        provenance = {
            "turn": 1, "checkpoint_mode": "first_session",
            "written_at": "2030-01-01T00:00:00+00:00",
        }
        out = self._run_js(
            f"mod.formatCheckpointLabel({json.dumps(provenance)}, {now_ms})"
        )
        self.assertIn("(9 d ago)", out)

    def test_missing_provenance_returns_no_snapshot(self):
        out = self._run_js("mod.formatCheckpointLabel(null, 1893456000000)")
        self.assertEqual(out, "no snapshot")

    def test_missing_written_at_returns_no_snapshot(self):
        out = self._run_js(
            'mod.formatCheckpointLabel({"turn": 1, "checkpoint_mode": "nap"}, 1893456000000)'
        )
        self.assertEqual(out, "no snapshot")

    # -- rescaleToReference ----------------------------------------------

    def test_rescale_preserves_aspect_score_and_sets_ref_to_one(self):
        members = [
            {"signal": "a", "importance": 2, "curveScore": 0.5},
            {"signal": "b", "importance": 3, "curveScore": 0.8},
            {"signal": "c", "importance": 0, "curveScore": 0.1},  # hold-out
        ]

        def admitted(ms):
            return [m for m in ms if m["importance"] > 0]

        before_admitted = admitted(members)
        before_score = self._run_js(f"mod.aspectScore({json.dumps(before_admitted)})")

        out = self._run_js(f'mod.rescaleToReference({json.dumps(members)}, "b")')
        self.assertIsNotNone(out)
        by_signal = {m["signal"]: m for m in out}
        self.assertAlmostEqual(by_signal["b"]["importance"], 1.0, places=12)
        self.assertEqual(by_signal["c"]["importance"], 0)

        after_admitted = admitted(out)
        after_score = self._run_js(f"mod.aspectScore({json.dumps(after_admitted)})")
        self.assertAlmostEqual(before_score, after_score, places=12)

    def test_rescale_hold_out_ineligible_returns_null(self):
        members = [
            {"signal": "a", "importance": 2, "curveScore": 0.5},
            {"signal": "c", "importance": 0, "curveScore": 0.1},
        ]
        out = self._run_js(f'mod.rescaleToReference({json.dumps(members)}, "c")')
        self.assertIsNone(out)

    def test_rescale_unknown_signal_returns_null(self):
        members = [{"signal": "a", "importance": 2, "curveScore": 0.5}]
        out = self._run_js(f'mod.rescaleToReference({json.dumps(members)}, "nope")')
        self.assertIsNone(out)

    # -- collapseUnavailableRuns ------------------------------------------

    def test_taint_rate_shape_one_run_of_102(self):
        points = [{"t": f"t{i}", "raw": "UNAVAILABLE", "score": "UNAVAILABLE",
                   "unreplayable": {"cascade_markers": {
                       "class": "schema", "reason": "no such table: cascade_markers"}}}
                  for i in range(102)]
        points += [{"t": f"t{102 + i}", "raw": 0.05, "score": 0.9, "unreplayable": None}
                   for i in range(49)]
        out = self._run_js(f"mod.collapseUnavailableRuns({json.dumps(points)})")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["n"], 102)
        self.assertEqual(out[0]["t0"], "t0")
        self.assertEqual(out[0]["t1"], "t101")
        self.assertEqual(
            out[0]["reasons"], {"schema: no such table: cascade_markers": 102}
        )

    def test_interleaved_runs_1_and_2(self):
        # n u n u u n -> runs of length [1, 2]
        def numeric(t):
            return {"t": t, "raw": 0.1, "score": 0.5, "unreplayable": None}

        def unavail(t):
            return {"t": t, "raw": "UNAVAILABLE", "score": "UNAVAILABLE", "unreplayable": None}

        points = [numeric("t0"), unavail("t1"), numeric("t2"),
                  unavail("t3"), unavail("t4"), numeric("t5")]
        out = self._run_js(f"mod.collapseUnavailableRuns({json.dumps(points)})")
        self.assertEqual([r["n"] for r in out], [1, 2])

    def test_no_unavailable_points_returns_empty(self):
        points = [{"t": f"t{i}", "raw": 0.1, "score": 0.5, "unreplayable": None}
                  for i in range(5)]
        out = self._run_js(f"mod.collapseUnavailableRuns({json.dumps(points)})")
        self.assertEqual(out, [])

    def test_dominant_reason_key_picks_highest_count(self):
        reasons = {"schema: x": 3, "timeout: y": 5, "other: z": 1}
        out = self._run_js(f"mod.dominantReasonKey({json.dumps(reasons)})")
        self.assertEqual(out, "timeout: y")

    def test_dominant_reason_key_empty_returns_null(self):
        out = self._run_js("mod.dominantReasonKey({})")
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
