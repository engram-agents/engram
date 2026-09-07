"""Export-decisions round-trip test (engram-alpha#2727): the Curve
Workbench's "Export decisions" JSON shape,
``{signal: {knots: [[x, y], ...], importance: n}}``, must validate through
``health.curves.CurveSpec`` construction (knots sorted ascending, a
well-formed curve) for every REAL model signal -- using each signal's own
real ``monotone_direction`` (read off the live ``health.engine.MODEL``,
never invented), so this is a genuine round-trip through the real model's
own curves, not a structural check against an arbitrary direction.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from health import engine as health_engine  # noqa: E402
from health import model as health_model  # noqa: E402
from health.curves import CurveSpec  # noqa: E402


class TestExportDecisionsRoundtrip(unittest.TestCase):
    def _export_payload_for_current_model(self):
        """Build the SAME shape the page's 'Export decisions' box produces
        for a hand-edited signal: knots (unmodified from shipped, to keep
        this a pure shape/plumbing check) + importance, for every real
        model signal."""
        model = health_engine.MODEL
        model_defs = health_model.model_defs_jsonable(model)
        payload = {}
        for m in model_defs["metrics"]:
            curve_def = model.curves[m["curve"]]
            payload[m["signal"]] = {
                "knots": [[x, y] for x, y in curve_def.knots],
                "importance": m["importance"],
            }
        return payload

    def test_every_real_signal_round_trips_through_curvespec(self):
        model = health_engine.MODEL
        payload = self._export_payload_for_current_model()
        self.assertTrue(payload, "expected at least one real model signal")
        for signal, proposal in payload.items():
            binding = next(mm for mm in model.metrics if mm.signal == signal)
            real_curve = model.curves[binding.curve]
            with self.subTest(signal=signal):
                spec = CurveSpec(
                    name=f"{signal}_proposal",
                    knots=tuple(tuple(p) for p in proposal["knots"]),
                    monotone_direction=real_curve.monotone_direction,
                )
                # Constructing without raising IS the round-trip check
                # (CurveSpec.__post_init__ validates non-empty, ascending
                # x, and a consistent input_domain) -- also spot-check the
                # curve evaluates sanely at both knot endpoints.
                lo, hi = spec.input_domain
                self.assertEqual(spec.curve_fn(lo), proposal["knots"][0][1])
                self.assertEqual(spec.curve_fn(hi), proposal["knots"][-1][1])

    def test_a_hand_edited_proposal_also_round_trips(self):
        """A genuinely EDITED proposal (not just the shipped knots passed
        through) -- an added knot, still ascending -- must also
        construct cleanly, since this is what the page actually produces
        once a user edits a curve."""
        model = health_engine.MODEL
        binding = model.metrics[0]
        real_curve = model.curves[binding.curve]
        lo, hi = real_curve.input_domain
        mid_x = (lo + hi) / 2.0
        edited_knots = [[lo, real_curve.knots[0][1]], [mid_x, 0.5], [hi, real_curve.knots[-1][1]]]
        spec = CurveSpec(
            name="hand_edited_proposal",
            knots=tuple(tuple(p) for p in edited_knots),
            monotone_direction=real_curve.monotone_direction,
        )
        self.assertEqual(spec.curve_fn(mid_x), 0.5)


if __name__ == "__main__":
    unittest.main()
