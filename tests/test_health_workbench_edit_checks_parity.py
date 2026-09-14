"""JS<->Python parity test for the Curve Workbench's edit-enter-time value
checks (engram-alpha#2738, round 2): ``viz/static/health-workbench-curve.js``'s
``validateImportance``/``validateKnots`` must agree with the equivalent
Python-side rules on accept/refuse for every case below.

Same node-under-subprocess convention as the sibling
``tests/test_health_workbench_curve_parity.py`` (run the real standalone JS
file under ``node``, cleanly skip if ``node`` is absent on this seat -- never
fake the JS side).

Two different notions of "the Python rule" are exercised here, and this
module is explicit about which is which -- see each TestCase's own
docstring:

  * ``validateKnots``'s structural rules (non-empty, sorted ascending by x,
    and input-domain consistency at the knot-set's own endpoints) really
    ARE ``health.curves.CurveSpec.__post_init__`` -- constructing a
    ``CurveSpec`` with the candidate knots and the declared
    ``input_domain`` either raises or it doesn't, and that IS the
    Python-side ground truth these cases are checked against.

  * ``validateKnots``'s per-knot ``output_range`` bound and
    ``validateImportance``'s hold-out-lock/positive-range rule are NOT
    checked anywhere in ``health.curves`` or ``health.model`` -- they are
    workbench-specific editor policy (Lei's ruling on engram-alpha#2738:
    "0 is reserved ... not purely a mathematical tuning factor" for
    importance; "y inside output_range ... enforce on entry" for knots).
    For these, this file's own small reference functions ARE the Python
    side of the parity check -- they are deliberately NOT re-derivations
    of anything in health/, just the same rule written twice (JS + Python)
    so the two implementations can be compared mechanically. Test cases
    for these are chosen so the CurveSpec-governed rules never fire
    (e.g. an out-of-output-range case still keeps its knots sorted, and
    keeps its knots' first/last x pinned to the declared input_domain), so
    the "true" CurveSpec pass and the "reference" output_range pass can be
    checked independently without one masking the other.
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

from health.curves import CurveSpec  # noqa: E402

_CURVE_JS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "viz"
    / "static"
    / "health-workbench-curve.js"
)

_NODE_AVAILABLE = shutil.which("node") is not None


def _curvespec_accepts(knots, input_domain):
    """True iff ``CurveSpec.__post_init__`` accepts *knots* under the
    declared *input_domain* -- real ground truth for the structural rules
    (non-empty, sorted ascending, domain-consistent)."""
    try:
        CurveSpec(
            name="parity_check",
            knots=tuple(tuple(p) for p in knots),
            monotone_direction="increasing",
            input_domain=tuple(input_domain),
        )
    except ValueError:
        return False
    return True


def _reference_validate_knots(knots, input_domain, output_range):
    """Python mirror of ``validateKnots`` -- see this module's own
    docstring for which parts of this are real CurveSpec parity (the
    ``_curvespec_accepts`` call) and which part (the output_range bound)
    is workbench-only policy with no CurveSpec equivalent."""
    if not _curvespec_accepts(knots, input_domain):
        return False
    ylo, yhi = output_range
    for _, y in knots:
        if y < ylo or y > yhi:
            return False
    return True


def _reference_validate_importance(new_value, stored_value):
    """Python mirror of ``validateImportance`` -- workbench-only policy
    (see module docstring); not derived from health.model's own
    ``importance >= 0`` admission check."""
    if stored_value == 0:
        return False
    if new_value <= 0:
        return False
    return True


# (knots, input_domain, output_range, expect_ok) -- covers: a valid case,
# empty knots, unsorted knots, x below domain, x above domain, y below
# range, y above range.
_KNOT_CASES = [
    ("valid", [(0.0, 0.0), (0.5, 0.4), (1.0, 1.0)], (0.0, 1.0), (0.0, 1.0), True),
    ("empty", [], (0.0, 1.0), (0.0, 1.0), False),
    ("unsorted", [(0.0, 0.0), (0.6, 0.3), (0.4, 0.5), (1.0, 1.0)], (0.0, 1.0), (0.0, 1.0), False),
    ("x_below_domain", [(-0.2, 0.0), (1.0, 1.0)], (0.0, 1.0), (0.0, 1.0), False),
    ("x_above_domain", [(0.0, 0.0), (1.3, 1.0)], (0.0, 1.0), (0.0, 1.0), False),
    ("y_below_range", [(0.0, -0.5), (1.0, 1.0)], (0.0, 1.0), (0.0, 1.0), False),
    ("y_above_range", [(0.0, 0.0), (1.0, 1.4)], (0.0, 1.0), (0.0, 1.0), False),
]

# (new_value, stored_value, expect_ok) -- covers: a valid case, negative
# importance, and "zero importance both directions" (a hold-out's stored-0
# importance can't be moved away from 0, and a non-zero importance can't be
# set to 0).
_IMPORTANCE_CASES = [
    ("valid", 3.0, 2.0, True),
    ("negative", -1.0, 1.0, False),
    ("hold_out_locked_away_from_zero", 5.0, 0.0, False),
    ("nonzero_set_to_zero", 0.0, 2.0, False),
]


@unittest.skipUnless(_NODE_AVAILABLE, "node is not available on this seat")
class TestValidateKnotsParity(unittest.TestCase):
    def _run_js(self, knots, input_domain, output_range):
        script = f"""
        const {{ validateKnots }} = require({json.dumps(str(_CURVE_JS_PATH))});
        const result = validateKnots(
          {json.dumps(knots)}, {json.dumps(list(input_domain))}, {json.dumps(list(output_range))}
        );
        console.log(JSON.stringify(result.ok));
        """
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return json.loads(result.stdout)

    def test_parity_on_all_knot_cases(self):
        for name, knots, input_domain, output_range, expect_ok in _KNOT_CASES:
            with self.subTest(case=name):
                js_ok = self._run_js(knots, input_domain, output_range)
                py_ok = _reference_validate_knots(knots, input_domain, output_range)
                self.assertEqual(py_ok, expect_ok, f"case={name}: python reference disagrees with expectation")
                self.assertEqual(js_ok, expect_ok, f"case={name}: JS disagrees with expectation")
                self.assertEqual(js_ok, py_ok, f"case={name}: JS and python disagree with each other")


@unittest.skipUnless(_NODE_AVAILABLE, "node is not available on this seat")
class TestValidateImportanceParity(unittest.TestCase):
    def _run_js(self, new_value, stored_value):
        script = f"""
        const {{ validateImportance }} = require({json.dumps(str(_CURVE_JS_PATH))});
        const result = validateImportance({json.dumps(new_value)}, {json.dumps(stored_value)});
        console.log(JSON.stringify(result.ok));
        """
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return json.loads(result.stdout)

    def test_parity_on_all_importance_cases(self):
        for name, new_value, stored_value, expect_ok in _IMPORTANCE_CASES:
            with self.subTest(case=name):
                js_ok = self._run_js(new_value, stored_value)
                py_ok = _reference_validate_importance(new_value, stored_value)
                self.assertEqual(py_ok, expect_ok, f"case={name}: python reference disagrees with expectation")
                self.assertEqual(js_ok, expect_ok, f"case={name}: JS disagrees with expectation")
                self.assertEqual(js_ok, py_ok, f"case={name}: JS and python disagree with each other")


if __name__ == "__main__":
    unittest.main()
