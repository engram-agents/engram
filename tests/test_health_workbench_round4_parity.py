"""Tests for the Curve Workbench's round-4 pure helpers
(engram-alpha#2738, round 4): ``viz/static/health-workbench-curve.js``'s
``insertMidKnot`` and ``padFlatRange``.

Same node-under-subprocess convention as the sibling
``tests/test_health_workbench_axis_helpers_parity.py`` (run the real
standalone JS file under ``node``, cleanly skip if ``node`` is absent on
this seat -- never fake the JS side).

Neither helper has a Python original to compare against -- both are new,
workbench-only UI helpers (curve-preserving knot insertion; a flat-range
axis fallback), not ports of anything in ``health/``. So, like the round-3
axis helpers, this file asserts each helper's documented CONTRACT (see its
own comment in health-workbench-curve.js) by running it under node and
checking the returned values directly.
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
class TestRound4Helpers(unittest.TestCase):
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

    # -- insertMidKnot -----------------------------------------------

    def test_insert_mid_knot_interior_is_curve_preserving(self):
        # knots=[[0,0],[0.5,0.5],[1,1]], insert after i=0 -> midpoint of
        # (0,0)-(0.5,0.5) is (0.25, 0.25), already on the existing
        # (linear) segment.
        knots = [[0, 0], [0.5, 0.5], [1, 1]]
        out = self._run_js(f"mod.insertMidKnot({json.dumps(knots)}, 0, null)")
        self.assertEqual(out, [[0, 0], [0.25, 0.25], [0.5, 0.5], [1, 1]])

    def test_insert_mid_knot_last_with_room_clamps_to_last_y(self):
        # domain extends past the last knot (1 < 2) -- new knot sits in
        # the clamp-to-endpoint region, at the last knot's own y.
        knots = [[0, 0], [1, 0.8]]
        out = self._run_js(
            f"mod.insertMidKnot({json.dumps(knots)}, 1, [0, 2])"
        )
        self.assertEqual(out, [[0, 0], [1, 0.8], [1.5, 0.8]])

    def test_insert_mid_knot_last_at_domain_edge_returns_null(self):
        # last knot's x already equals the domain's high edge -- nothing
        # to insert past it; caller uses this null to omit the "+" button.
        knots = [[0, 0], [1, 1]]
        out = self._run_js(
            f"mod.insertMidKnot({json.dumps(knots)}, 1, [0, 1])"
        )
        self.assertIsNone(out)

    def test_insert_mid_knot_last_without_domain_returns_null(self):
        knots = [[0, 0], [1, 1]]
        out = self._run_js(f"mod.insertMidKnot({json.dumps(knots)}, 1, null)")
        self.assertIsNone(out)

    def test_insert_mid_knot_zero_width_segment_uses_left_y(self):
        # a degenerate (zero-width) segment: piecewiseLinear's own
        # no-meaningful-slope branch returns the segment's own y -- the
        # midpoint x equals the shared x, so the new knot collapses onto
        # the same point.
        knots = [[0, 0], [0.5, 0.3], [0.5, 0.9], [1, 1]]
        out = self._run_js(f"mod.insertMidKnot({json.dumps(knots)}, 1, null)")
        self.assertEqual(out[2], [0.5, 0.3])

    # -- padFlatRange --------------------------------------------------

    def test_pad_flat_range_all_zero_raw(self):
        out = self._run_js("mod.padFlatRange(0, 'raw', [0, 1], null)")
        self.assertEqual(out, [0, 0.05])

    def test_pad_flat_range_constant_score_uses_output_range(self):
        out = self._run_js("mod.padFlatRange(0.5, 'score', null, [0, 1])")
        self.assertEqual(out, [0, 1])

    def test_pad_flat_range_constant_score_defaults_when_no_output_range(self):
        out = self._run_js("mod.padFlatRange(0.5, 'score', null, null)")
        self.assertEqual(out, [0, 1])

    def test_pad_flat_range_constant_raw_at_domain_edge(self):
        out = self._run_js("mod.padFlatRange(1.0, 'raw', [0, 1], null)")
        self.assertEqual(out, [0.95, 1.0])

    def test_pad_flat_range_raw_without_domain_clamps_low_only(self):
        out = self._run_js("mod.padFlatRange(0, 'raw', null, null)")
        self.assertEqual(out, [0, 0.05])
        out2 = self._run_js("mod.padFlatRange(10, 'raw', null, null)")
        self.assertEqual(out2, [9.95, 10.05])


if __name__ == "__main__":
    unittest.main()
