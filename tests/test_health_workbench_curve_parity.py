"""JS<->Python parity test for the Curve Workbench (engram-alpha#2727):
``viz/static/health-workbench-curve.js``'s ``piecewiseLinear`` must agree
with ``health.curves.piecewise_linear`` to 1e-12 on every knot set below.

Same convention as ``tests/test_viz_health_v2_detail_view.py``'s own
node-under-subprocess pattern for ``health-detail-math.js`` (run the real
standalone JS file under ``node``, cleanly skip if ``node`` is absent on
this seat -- never fake the JS side).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from health.curves import piecewise_linear  # noqa: E402

_CURVE_JS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "viz"
    / "static"
    / "health-workbench-curve.js"
)

_NODE_AVAILABLE = shutil.which("node") is not None

# (knots, [sample rates]) -- includes: an ordinary 3-knot curve, the
# single-knot degenerate/flat case, out-of-span-low, out-of-span-high, and
# two coincident-x knots (a zero-width segment -- piecewise_linear's own
# "no meaningful slope" branch).
_CASES = [
    ("ordinary_3_knot", [(0.0, 1.0), (0.5, 0.4), (1.0, 0.0)],
     [-0.5, 0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0, 1.5]),
    ("badness_default_shape", [(0.0, 1.0), (0.99, 0.01), (1.0, 0.01)],
     [-1.0, 0.0, 0.5, 0.98, 0.99, 0.995, 1.0, 2.0]),
    ("single_knot_flat", [(0.5, 0.7)],
     [-10.0, 0.0, 0.5, 0.7, 1.0, 10.0]),
    ("out_of_span_low_only", [(0.2, 0.0), (0.8, 1.0)],
     [-5.0, -0.5, 0.0, 0.1, 0.2]),
    ("out_of_span_high_only", [(0.2, 0.0), (0.8, 1.0)],
     [0.8, 0.9, 1.0, 5.0, 50.0]),
    ("coincident_x_knots", [(0.0, 0.0), (0.5, 0.3), (0.5, 0.9), (1.0, 1.0)],
     [0.0, 0.25, 0.4, 0.5, 0.6, 0.75, 1.0]),
]


@unittest.skipUnless(_NODE_AVAILABLE, "node is not available on this seat")
class TestPiecewiseLinearParity(unittest.TestCase):
    def _run_js(self, knots, rates):
        script = f"""
        const {{ piecewiseLinear }} = require({json.dumps(str(_CURVE_JS_PATH))});
        const knots = {json.dumps(knots)};
        const rates = {json.dumps(rates)};
        console.log(JSON.stringify(rates.map(r => piecewiseLinear(r, knots))));
        """
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return json.loads(result.stdout)

    def test_parity_on_all_cases(self):
        for name, knots, rates in _CASES:
            with self.subTest(case=name):
                js_scores = self._run_js(knots, rates)
                py_scores = [
                    piecewise_linear(r, knots=tuple(tuple(k) for k in knots))
                    for r in rates
                ]
                self.assertEqual(len(js_scores), len(py_scores))
                for r, js_v, py_v in zip(rates, js_scores, py_scores):
                    self.assertAlmostEqual(
                        js_v, py_v, places=12,
                        msg=f"case={name} rate={r}: js={js_v!r} py={py_v!r}",
                    )


if __name__ == "__main__":
    unittest.main()
