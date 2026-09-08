"""Tests for the Curve Workbench's axis/focus-marker pure helpers
(engram-alpha#2738, round 3): ``viz/static/health-workbench-curve.js``'s
``computeAxisTicks`` / ``formatAxisNumber`` / ``linearScale`` /
``nearestIndexByValue`` / ``computeTimeTicks`` / ``formatTimeTickLabel`` /
``formatLocalDateTime`` / ``computeTimeTickLabels`` / ``shouldTiltLabels`` /
``formatBinPercent`` / ``binIndexForX`` / ``clamp``.

Same node-under-subprocess convention as the sibling
``tests/test_health_workbench_curve_parity.py`` and
``tests/test_health_workbench_edit_checks_parity.py`` (run the real
standalone JS file under ``node``, cleanly skip if ``node`` is absent on
this seat -- never fake the JS side).

Unlike those two siblings, most of these helpers have NO Python original to
compare against -- they are new, workbench-only UI helpers (axis ticks, a
focus-marker nearest-point lookup, a real-time x-axis, histogram-bin
percentage formatting), not ports of anything in ``health/``. So this file
is a JS-behavior test, not a cross-language parity test: it asserts the
documented CONTRACT of each helper (see each function's own comment in
health-workbench-curve.js) by running it under node and checking the
returned values/structure directly, the way it would compare against a
Python twin if one existed.

``formatTimeTickLabel``/``formatLocalDateTime`` read viewer-LOCAL time
fields (``Date#getMonth``/``getDate``/etc, no explicit TZ), so their exact
string output depends on the host's local timezone. This file asserts only
the STRUCTURE of that output (format shape, year-prefix presence) for
inputs whose local calendar date could plausibly vary by TZ, and pins an
exact string only for a UTC-midnight input padded well away from any real
timezone's day boundary shift, to keep the test host-independent.
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
class TestAxisHelpers(unittest.TestCase):
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

    def test_clamp(self):
        self.assertEqual(self._run_js("mod.clamp(5, 0, 10)"), 5)
        self.assertEqual(self._run_js("mod.clamp(-5, 0, 10)"), 0)
        self.assertEqual(self._run_js("mod.clamp(15, 0, 10)"), 10)

    def test_compute_axis_ticks_includes_both_endpoints_and_count_bounds(self):
        for desired, lo, hi in [(5, 0, 1), (3, 0, 1), (5, -2, 8), (4, 0.5, 0.5)]:
            with self.subTest(desired=desired, lo=lo, hi=hi):
                ticks = self._run_js(f"mod.computeAxisTicks({lo}, {hi}, {desired})")
                if hi <= lo:
                    self.assertEqual(ticks, [lo])
                    continue
                self.assertGreaterEqual(len(ticks), 3)
                self.assertLessEqual(len(ticks), 5)
                self.assertAlmostEqual(ticks[0], lo, places=9)
                self.assertAlmostEqual(ticks[-1], hi, places=9)
                # monotone ascending
                self.assertEqual(ticks, sorted(ticks))

    def test_format_axis_number_strips_trailing_zeros(self):
        self.assertEqual(self._run_js("mod.formatAxisNumber(0)"), "0")
        self.assertEqual(self._run_js("mod.formatAxisNumber(0.5)"), "0.5")
        self.assertEqual(self._run_js("mod.formatAxisNumber(1)"), "1")
        self.assertEqual(self._run_js("mod.formatAxisNumber(0.25)"), "0.25")

    def test_linear_scale_forward_and_inverted(self):
        # forward: domain [0,1] -> range [0,100]
        self.assertAlmostEqual(self._run_js("mod.linearScale(0.5, 0, 1, 0, 100)"), 50.0)
        self.assertAlmostEqual(self._run_js("mod.linearScale(0, 0, 1, 0, 100)"), 0.0)
        self.assertAlmostEqual(self._run_js("mod.linearScale(1, 0, 1, 0, 100)"), 100.0)
        # inverted (swap domain/range args) recovers the original value
        self.assertAlmostEqual(
            self._run_js("mod.linearScale(mod.linearScale(0.37, 0, 1, 10, 50), 10, 50, 0, 1)"),
            0.37, places=9,
        )
        # degenerate domain maps everything to the range's low end
        self.assertAlmostEqual(self._run_js("mod.linearScale(5, 3, 3, 10, 20)"), 10.0)

    def test_nearest_index_by_value(self):
        self.assertEqual(self._run_js("mod.nearestIndexByValue([1, 5, 9], 4)"), 1)
        self.assertEqual(self._run_js("mod.nearestIndexByValue([1, 5, 9], 0)"), 0)
        self.assertEqual(self._run_js("mod.nearestIndexByValue([1, 5, 9], 100)"), 2)
        # null entries skipped
        self.assertEqual(self._run_js("mod.nearestIndexByValue([1, null, 9], 8)"), 2)
        self.assertEqual(self._run_js("mod.nearestIndexByValue([], 1)"), -1)

    def test_compute_time_ticks_spans_and_bounds_count(self):
        t_min, t_max = 1_000_000_000_000, 1_000_000_600_000
        ticks = self._run_js(f"mod.computeTimeTicks({t_min}, {t_max}, 5)")
        self.assertEqual(ticks[0], t_min)
        self.assertEqual(ticks[-1], t_max)
        self.assertGreaterEqual(len(ticks), 3)
        self.assertLessEqual(len(ticks), 5)

    def test_format_time_tick_label_shape_and_year_prefix(self):
        # 2026-06-09T12:00:00.000Z -- padded well inside the UTC day so any
        # real timezone offset (-12..+14) keeps the same local calendar date.
        ms = 1_781_006_400_000  # 2026-06-09T12:00:00Z
        no_year = self._run_js(f"mod.formatTimeTickLabel({ms}, false)")
        with_year = self._run_js(f"mod.formatTimeTickLabel({ms}, true)")
        self.assertRegex(no_year, r"^\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertRegex(with_year, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertTrue(with_year.endswith(no_year))
        self.assertTrue(with_year.startswith("2026-"))

    def test_format_local_date_time_shape(self):
        ms = 1_781_006_400_000
        out = self._run_js(f"mod.formatLocalDateTime({ms})")
        self.assertRegex(out, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertTrue(out.startswith("2026-06-"))

    def test_compute_time_tick_labels_year_prefix_rule(self):
        # first tick always carries the year; a same-year later tick does not;
        # a tick crossing into a new year carries it again.
        # noon UTC (not midnight) so any real timezone offset (-12..+14)
        # keeps the same local calendar YEAR as the UTC one named here.
        jan1_2026 = 1_767_268_800_000  # 2026-01-01T12:00:00Z
        jun1_2026 = 1_780_401_600_000  # 2026-06-01T12:00:00Z
        jan1_2027 = 1_798_804_800_000  # 2027-01-01T12:00:00Z
        labels = self._run_js(
            f"mod.computeTimeTickLabels([{jan1_2026}, {jun1_2026}, {jan1_2027}])"
        )
        self.assertTrue(labels[0].startswith("2026-"))
        self.assertNotRegex(labels[1], r"^\d{4}-")
        self.assertTrue(labels[2].startswith("2027-"))

    def test_should_tilt_labels(self):
        # short labels, generous spacing -- no tilt
        self.assertFalse(self._run_js('mod.shouldTiltLabels(40, ["06-09", "06-10"], 6, 6)'))
        # long labels, tight spacing -- tilt
        self.assertTrue(
            self._run_js('mod.shouldTiltLabels(10, ["2026-06-09 12:00", "2026-06-10 12:00"], 6, 6)')
        )

    def test_format_bin_percent(self):
        self.assertEqual(self._run_js("mod.formatBinPercent(0, 0)"), "0%")
        self.assertEqual(self._run_js("mod.formatBinPercent(5, 100)"), "5.0%")
        self.assertEqual(self._run_js("mod.formatBinPercent(50, 100)"), "50%")
        self.assertEqual(self._run_js("mod.formatBinPercent(9, 100)"), "9.0%")
        self.assertEqual(self._run_js("mod.formatBinPercent(10, 100)"), "10%")

    def test_bin_index_for_x_clamped(self):
        self.assertEqual(self._run_js("mod.binIndexForX(0, 0, 10, 5)"), 0)
        self.assertEqual(self._run_js("mod.binIndexForX(25, 0, 10, 5)"), 2)
        self.assertEqual(self._run_js("mod.binIndexForX(-5, 0, 10, 5)"), 0)
        self.assertEqual(self._run_js("mod.binIndexForX(1000, 0, 10, 5)"), 4)
        self.assertEqual(self._run_js("mod.binIndexForX(5, 0, 10, 0)"), -1)


if __name__ == "__main__":
    unittest.main()
