"""Tests for health/curves.py — the Layer-3 scoring-curve library
(engram-alpha#1991, PR4 of 4, buildable slice).

Two things live in this file, per the dispatching spec:

1. The PROPERTY-CONTRACT GATE (design doc "Layer 3 — Scoring curves"): for
   every shipped ``CurveSpec``, dense-sample its declared ``input_domain``
   and assert the four Layer-3 properties (total / bounded / monotone in
   the declared direction / non-saturating over the observed range) hold.
   Parametrized over a list of ``CurveSpec`` fixtures, not hard-coded to
   one curve instance, so a later curve SHAPE (sigmoid/step, design doc
   Q1) can plug into the same suite by adding a fixture.

2. The v1 NON-SATURATION DEMONSTRATION (the artifact Borges specifically
   wants to exist): a test that reproduces engram_stats.py's OLD v1
   penalty SHAPE (``score -= min(count * multiplier, cap)``, lines
   ~272/~281 of ``_compute_health_score``) as a tiny local helper — NOT
   importing ``_compute_health_score`` itself (that needs a live
   ``sqlite3.Connection`` and computes the WHOLE v1 score; only the
   penalty SHAPE matters here) — and asserts, ON PURPOSE, that it fails
   the non-saturation property over the design doc's own named observed
   ranges (stale-node count [14, 124]; orphan count [149, 174]).

   This test PASSES by correctly documenting v1's known-bad behavior — it
   is not a bug in this PR's new code if it "fails" in the sense of
   demonstrating brokenness; the test itself must PASS by correctly
   asserting the known-bad behavior. If a future accidental fix to v1's
   penalty shape ever makes one of these assertions false, that IS the fix
   working: update or retire the test then. It must never be allowed to
   silently rot into a false claim about current behavior.

``hypothesis`` is not a project dependency (checked requirements/*.txt and
this venv before writing this file: ``ModuleNotFoundError``) — the property
tests below use plain dense manual sampling (200+ evenly-spaced points)
instead, per the spec's own fallback instruction.
"""

from __future__ import annotations

import functools
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from health.curves import (  # noqa: E402
    BADNESS_DEFAULT_CURVE,
    CurveSpec,
    GOODNESS_DEFAULT_CURVE,
    LEGACY_GOODNESS_TRANSITION_CURVE,
    piecewise_linear,
)


def _dense_sample(domain: tuple[float, float], n: int = 201) -> list[float]:
    """``n`` evenly-spaced points across ``[lo, hi]`` inclusive of both
    endpoints (n=201 by default — the spec's "200+ evenly-spaced points"
    dense-sampling choice, since ``hypothesis`` is not available)."""
    lo, hi = domain
    if n < 2:
        return [lo]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


# ---------------------------------------------------------------------------
# Example CurveSpec fixtures exercised by the property-contract gate below.
# ---------------------------------------------------------------------------

# The exact rescaled transition curve from metric_goodness (see
# tests/test_health_engine_curve_transition.py for the full derivation):
# decreasing, (0,1) -> (0.99, 0.01) -> (1, 0.01) flat floor, with
# observed_range=(0.012, 0.105) — the design doc's own two measured
# stale-derivation-rate points from the natural experiment, Panel B: "1.2%
# and 10.5%". Round 4 built this as a LOCAL fixture (`dataclasses.replace`
# off LEGACY_GOODNESS_TRANSITION_CURVE, overriding both `name` and
# `observed_range`), which meant the non-saturation property test below
# never actually exercised the shipped object. Round 5 (colleague review,
# Borges, PR #2494) puts `observed_range` directly on the shipped constant
# itself, so this file now references LEGACY_GOODNESS_TRANSITION_CURVE
# directly — no local copy, no risk of the two drifting apart, and the
# property suite runs against the real shipped object.

# A coverage-style INCREASING curve (deliberately trivial — identity from 0
# to 1) so the property suite below is exercised in both monotone
# directions, not just "decreasing". No observed_range: this fixture exists
# only to cover the "increasing" branch of the total/bounded/monotone
# properties, not to make a non-saturation claim.
_TRIVIAL_INCREASING_CURVE = CurveSpec(
    name="trivial_increasing_identity_curve",
    curve_fn=functools.partial(
        piecewise_linear,
        knots=((0.0, 0.0), (1.0, 1.0)),
    ),
    input_domain=(0.0, 1.0),
    monotone_direction="increasing",
    observed_range=None,
)

# GOODNESS_DEFAULT_CURVE (engram-alpha#2518) is the shipped increasing
# mirror of BADNESS_DEFAULT_CURVE — genuinely NEW as of this PR (unlike
# BADNESS_DEFAULT_CURVE itself, whose shape is byte-for-byte identical to
# LEGACY_GOODNESS_TRANSITION_CURVE above and so has no separate entry here),
# so it gets its own property-contract coverage.
_ALL_CURVE_SPECS: tuple[CurveSpec, ...] = (
    LEGACY_GOODNESS_TRANSITION_CURVE,
    _TRIVIAL_INCREASING_CURVE,
    GOODNESS_DEFAULT_CURVE,
)


def _assert_non_saturating(
    curve_fn, sub_range: tuple[float, float], *, label: str, n: int = 25
) -> None:
    """The design doc's load-bearing property ("The property that would
    have caught v1"): the curve must NOT be constant across an empirically
    observed sub-range of its input domain.

    JUDGMENT CALL (flagged in the coder-fairy report, per the spec's own
    "pick whichever check you judge correctly falsifies 'flat/saturated'"
    instruction): rather than the weaker two-endpoint-only check
    (``f(lo) != f(hi)``), this densely samples the sub-range and asserts
    the SPREAD across every sampled point exceeds a tiny float tolerance —
    the spec's own "more robustly, the curve's value actually varies
    somewhere inside that sub-range" alternative. This also catches a
    curve that happens to have equal endpoint values but a genuine
    bump/dip strictly between them (not a real risk for piecewise-linear
    specifically, but the property itself is stated shape-agnostically in
    the design doc, and a future curve shape — sigmoid, step — could have
    exactly that failure mode).
    """
    lo, hi = sub_range
    xs = [lo, hi] if hi <= lo else _dense_sample(sub_range, n=n)
    ys = [curve_fn(x) for x in xs]
    spread = max(ys) - min(ys)
    assert spread > 1e-9, (
        f"{label}: curve is constant ({ys[0]!r}) across observed_range "
        f"{sub_range!r} — this IS the non-saturation property the design "
        f"doc names as load-bearing ('The property that would have caught "
        f"v1')"
    )


# ---------------------------------------------------------------------------
# Property-contract gate (design doc "Layer 3 — Scoring curves"):
# Total / Bounded / Monotone / Non-saturating-over-the-observed-range.
# Parametrized over CurveSpec fixtures, not hard-coded to one curve
# instance — a later curve SHAPE (design doc Q1) plugs into the same suite
# by adding a CurveSpec to _ALL_CURVE_SPECS above.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", _ALL_CURVE_SPECS, ids=lambda s: s.name)
class TestCurvePropertyContract:
    def test_total_never_raises_never_none_never_nan(self, spec: CurveSpec):
        for x in _dense_sample(spec.input_domain):
            y = spec.curve_fn(x)
            assert y is not None
            assert isinstance(y, float)
            assert not math.isnan(y)

    def test_bounded_to_output_range(self, spec: CurveSpec):
        lo, hi = spec.output_range
        for x in _dense_sample(spec.input_domain):
            y = spec.curve_fn(x)
            assert lo <= y <= hi, (
                f"{spec.name}: f({x})={y} outside {spec.output_range}"
            )

    def test_monotone_in_declared_direction(self, spec: CurveSpec):
        """Pairwise across the WHOLE dense sample, not just adjacent points
        or a coarse handful — per the spec's explicit requirement."""
        xs = _dense_sample(spec.input_domain)
        ys = [spec.curve_fn(x) for x in xs]
        n = len(xs)
        tol = 1e-9
        for i in range(n):
            for j in range(i + 1, n):
                if spec.monotone_direction == "decreasing":
                    assert ys[i] >= ys[j] - tol, (
                        f"{spec.name}: expected decreasing but "
                        f"f({xs[i]})={ys[i]} < f({xs[j]})={ys[j]}"
                    )
                else:
                    assert ys[i] <= ys[j] + tol, (
                        f"{spec.name}: expected increasing but "
                        f"f({xs[i]})={ys[i]} > f({xs[j]})={ys[j]}"
                    )


@pytest.mark.parametrize(
    "spec",
    [s for s in _ALL_CURVE_SPECS if s.observed_range is not None],
    ids=lambda s: s.name,
)
def test_non_saturating_over_observed_range(spec: CurveSpec):
    _assert_non_saturating(spec.curve_fn, spec.observed_range, label=spec.name)


# ---------------------------------------------------------------------------
# v1 non-saturation DEMONSTRATION — the artifact Borges specifically wants
# to exist (spec item 3). See module docstring: these tests PASS by
# correctly asserting v1's KNOWN-BAD behavior, on purpose, as living
# documentation. If a future accidental fix to v1's penalty shape ever
# makes one of these fail, that is the fix working — update/retire the
# test then; it must never be allowed to silently rot into a false
# assertion about current behavior.
# ---------------------------------------------------------------------------


def _v1_penalty_shape(count: float, *, multiplier: float, cap: float) -> float:
    """Local reproduction of engram_stats.py's OLD v1 penalty SHAPE
    (``score -= min(count * multiplier, cap)``), lines ~272 (stale) and
    ~281 (orphans) of ``_compute_health_score`` — confirmed byte-for-byte
    against src/engram/engram_stats.py before writing this test
    (multiplier/cap pairs: stale=(2, 10), orphans=(1, 10)).

    Deliberately NOT importing ``_compute_health_score`` itself: it takes
    a live ``sqlite3.Connection`` and computes the WHOLE v1 score from real
    tables; this test only needs the penalty SHAPE in isolation, not a
    live DB call, per the spec.
    """
    return min(count * multiplier, cap)


class TestV1NonSaturationDemonstration:
    def test_stale_penalty_saturates_over_the_observed_range(self):
        """engram_stats.py:272 ``score -= min(stale * 2, 10)``, cap binds
        at stale=5. Design doc's own natural experiment: stale-node count
        went [124 -> 14] between 2026-08-21 and 2026-09-01 — an 89%
        REDUCTION, the largest cascade remediation the graph has ever
        undergone — and the v1 SCORE did not move at all, because both
        endpoints of that range sit past the cap.

        This test PASSES by proving that: it asserts the two endpoint
        penalties ARE EQUAL (both saturated at the cap), which is exactly
        v1's known-bad, non-saturation-VIOLATING behavior.
        """
        lo_count, hi_count = 14, 124
        lo_penalty = _v1_penalty_shape(lo_count, multiplier=2, cap=10)
        hi_penalty = _v1_penalty_shape(hi_count, multiplier=2, cap=10)
        assert lo_penalty == hi_penalty == 10.0

    def test_orphan_penalty_saturates_over_the_observed_range(self):
        """engram_stats.py:281 ``score -= min(orphans * 1, 10)``, cap binds
        at orphans=10. Same window: orphan count moved [149 -> 174] (a 17%
        REGRESSION, the wrong direction) and the v1 SCORE, again, did not
        move — both endpoints sit past the cap.

        PASSES by proving the two endpoint penalties ARE EQUAL.
        """
        lo_count, hi_count = 149, 174
        lo_penalty = _v1_penalty_shape(lo_count, multiplier=1, cap=10)
        hi_penalty = _v1_penalty_shape(hi_count, multiplier=1, cap=10)
        assert lo_penalty == hi_penalty == 10.0


# ---------------------------------------------------------------------------
# GOODNESS_DEFAULT_CURVE mirror-derivation regression guard (engram-alpha
# #2518) — pins the property GOODNESS_DEFAULT_CURVE.curve_fn(c) ==
# BADNESS_DEFAULT_CURVE.curve_fn(1 - c) for every c, which is exactly what
# makes this PR's flip (Aspect-B signals: badness-complement -> literal
# coverage, direction moved into the curve) produce byte-identical composed
# scores for an unchanged graph. Was a throwaway scratch check during
# development (per the dispatching spec's step 2); committed here as a
# permanent regression guard instead of a one-off.
# ---------------------------------------------------------------------------


class TestGoodnessDefaultCurveMirrorsBadnessDefault:
    @pytest.mark.parametrize(
        "c", [0.0, 0.005, 0.01, 0.02, 0.3, 0.5, 0.7, 0.9, 0.99, 0.995, 1.0]
    )
    def test_mirror_identity_holds_at_sample_points(self, c: float):
        assert GOODNESS_DEFAULT_CURVE.curve_fn(c) == pytest.approx(
            BADNESS_DEFAULT_CURVE.curve_fn(1 - c), abs=1e-9
        )

    def test_mirror_identity_holds_densely(self):
        """Same identity, densely sampled across the whole [0,1] domain —
        the parametrized spot-check above pins named values for a readable
        failure message; this catches anything a sparse sample could miss.
        """
        for c in _dense_sample((0.0, 1.0)):
            assert GOODNESS_DEFAULT_CURVE.curve_fn(c) == pytest.approx(
                BADNESS_DEFAULT_CURVE.curve_fn(1 - c), abs=1e-9
            ), f"mirror identity failed at c={c}"

    def test_knots_match_hand_derivation(self):
        """Pins the exact three knots derived in GOODNESS_DEFAULT_CURVE's
        own docstring (health/curves.py), so a future edit to either
        curve's knots is caught here even if the dense mirror-identity
        check above were ever weakened."""
        assert GOODNESS_DEFAULT_CURVE.curve_fn(0.0) == pytest.approx(0.01)
        assert GOODNESS_DEFAULT_CURVE.curve_fn(0.01) == pytest.approx(0.01)
        assert GOODNESS_DEFAULT_CURVE.curve_fn(1.0) == pytest.approx(1.0)
