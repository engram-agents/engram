"""health_engine_curves — Layer 3 scoring-curve library (engram-alpha#1991,
PR4 of 4, buildable slice: curve library + property contract + transition
rule).

Design-of-record: docs/health-score-v2-design.md (origin/docs/health-score-
v2-design @ 18fa26820a761b7d79c583ea2e025c455cacc8a4), section "Layer 3 —
Scoring curves" (the four-property contract: Total / Bounded / Monotone /
Non-saturating over the observed range) and "The property that would have
caught v1" (why non-saturation is the load-bearing one of the four).

Scope of THIS module: the curve function(s) themselves, plus a small
dataclass pairing a curve with its declared contract. Nothing here reads a
live sqlite3.Connection, writes anything, or imports engram_core /
health_engine_config / health_engine_metrics — Layer 3 sits one level above
the raw signal functions (Layer 1) and metrics (Layer 2), and is pure
arithmetic over a float, by design (design doc: "a score is a curve applied
to a metric, with explicit input and output ranges").

Score polarity: this engine's score is always oriented HEALTHIER-IS-HIGHER —
a higher composed/aspect/metric score always means a healthier graph, never
the reverse. Given that invariant, a metric's polarity (is a high raw signal
good or bad?) is fully determined by, and only represented by, its curve's
``monotone_direction``: an increasing curve means "higher raw signal is
better" (e.g. a coverage-style metric), a decreasing curve means "higher raw
signal is worse" (e.g. a badness-rate metric — like the ones this PR's nine
real metrics are). There is deliberately no separate polarity field on
``CurveSpec`` — that would be a second encoding of the same fact, exactly the
kind of drift surface this redesign exists to remove.

This PR ships exactly ONE curve shape — ``piecewise_linear`` — per the
design doc's own "piecewise-linear ... being the default shape" and this
dispatch's explicit scope. The data-file-format change (MetricDef.signal_fn
as frozen data), the D1 (Layer-4 aggregation) / D2 (data-file-vs-callables)
rulings, and the ``observed_range`` "carrier" (auto-populating it from
historical snapshot data) are all explicitly OUT of scope for this PR — see
the dispatching spec's "What NOT to do".

Additive-only: this module is new and is not imported by health_engine.py,
health_engine_metrics.py, health_engine_config.py, or engram_stats.py in
this PR — wiring a curve into the real per-metric scoring path is future
work, gated on the D1/D2 rulings still pending with Lei.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Callable, Literal


def piecewise_linear(
    rate: float,
    *,
    knots: tuple[tuple[float, float], ...],
) -> float:
    """Piecewise-linear interpolation through ``knots`` (a tuple of
    ``(x, y)`` pairs, sorted ascending by ``x``, spanning the curve's
    declared input domain).

    ``rate`` outside the knots' x-span is CLAMPED to the nearest endpoint's
    ``y`` value — never extrapolated, never raises. A curve's contract is
    TOTAL over its declared input domain (design doc "Layer 3 — Scoring
    curves"): clamping is what makes an out-of-declared-range input still
    return a number rather than raising, and it is a deliberate, documented
    choice, not a silent default.

    Pure function: no I/O, no global state, no ``sqlite3.Connection``
    argument (Layer 3 is one level above the raw Layer-1/2 signals).
    Garbage-in-garbage-out is explicit and intentional — this function
    correctly implements piecewise-linear interpolation for whatever knots
    it is given; it does not itself enforce that a knot set is monotone,
    spans the intended domain, or stays within ``[0, 1]`` output. Those
    PROPERTIES are enforced by the property-contract test suite in
    tests/test_health_engine_curves.py, run against knot sets the caller
    has already declared should satisfy them — the curve doesn't have to
    fix a badly-specified knot set, only correctly implement interpolation
    over whatever knot set it's given.

    Raises ``ValueError`` only for a structurally empty knot tuple — that
    is a caller precondition violation (there is no curve to interpolate
    at all), not a "garbage rate value" case; every real ``rate`` input,
    anywhere on the real number line, returns a plain float without
    raising once a non-empty knot set is supplied.
    """
    if not knots:
        raise ValueError("piecewise_linear requires at least one (x, y) knot")

    xs = [k[0] for k in knots]
    ys = [k[1] for k in knots]

    # Clamp: rate at or below the first knot's x -> first knot's y; at or
    # above the last knot's x -> last knot's y. This also correctly
    # handles the single-knot case (xs[0] == xs[-1]): every rate clamps to
    # that one knot's y, i.e. a flat curve.
    if rate <= xs[0]:
        return float(ys[0])
    if rate >= xs[-1]:
        return float(ys[-1])

    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if x0 <= rate <= x1:
            if x1 == x0:
                # Degenerate (zero-width) segment: no meaningful slope —
                # return the segment's own y rather than dividing by zero.
                return float(y0)
            t = (rate - x0) / (x1 - x0)
            return float(y0 + t * (y1 - y0))

    # Unreachable given the two clamp branches above (every rate strictly
    # between xs[0] and xs[-1] falls into exactly one knot-to-knot segment)
    # — kept as an explicit invariant guard rather than a silent
    # fallthrough, so "never raises for an in-domain rate" holds even if
    # that invariant is ever violated by a future edit.
    return float(ys[-1])


MonotoneDirection = Literal["increasing", "decreasing"]


@dataclass(frozen=True)
class CurveSpec:
    """Pairs a fully-specified, single-argument curve function with its
    declared Layer-3 contract (design doc "Layer 3 — Scoring curves").

    ``curve_fn`` is ``Callable[[float], float]`` — a curve shape with all
    of its shape-specific parameters already bound (e.g.
    ``functools.partial(piecewise_linear, knots=...)``), NOT the raw
    multi-parameter ``piecewise_linear`` itself. This lets the
    property-test suite (tests/test_health_engine_curves.py) iterate over
    a list of ``CurveSpec`` instances and call ``spec.curve_fn(x)``
    uniformly, regardless of which curve SHAPE backs any given one —
    piecewise-linear today; sigmoid/step per the design doc's open Q1
    ("where does the curve library stop?"), later, without changing this
    contract.

    Fields:
      name               — human-readable label, for test IDs/messages
                            only (not part of the design-doc contract
                            itself; added here as a convenience for the
                            parametrized property-test suite).
      curve_fn           — see above.
      input_domain       — ``(lo, hi)`` the curve is declared TOTAL over.
      monotone_direction — "increasing" or "decreasing"; which inequality
                            the MONOTONE property tests for.
      output_range       — ``(lo, hi)`` the curve is declared BOUNDED to.
                            Defaults to ``(0.0, 1.0)`` per the design doc's
                            "bounded to [0,1]" rule — every curve this
                            engine ships is expected to satisfy this, so a
                            caller only needs to override it for a
                            deliberately different contract.
      observed_range     — the empirically-observed sub-range of
                            ``input_domain`` the NON-SATURATION property is
                            checked against, or ``None`` if no such range
                            has been recorded yet for this curve.
                            Deliberately NOT auto-populated from historical
                            snapshot data — that "carrier" mechanism is
                            explicitly out of scope for this PR (see the
                            dispatching spec's "What NOT to do"); a curve
                            with no recorded history simply doesn't carry a
                            non-saturation check yet.
    """

    name: str
    curve_fn: Callable[[float], float]
    input_domain: tuple[float, float]
    monotone_direction: MonotoneDirection
    output_range: tuple[float, float] = (0.0, 1.0)
    observed_range: tuple[float, float] | None = None


# ---------------------------------------------------------------------------
# LEGACY_GOODNESS_TRANSITION_CURVE — engram-alpha#1991 PR4, round 2 (forum
# #382, Kepler's request during round-1 review).
# ---------------------------------------------------------------------------

LEGACY_GOODNESS_TRANSITION_CURVE = CurveSpec(
    name="legacy_goodness_transition_curve",
    curve_fn=functools.partial(
        piecewise_linear,
        knots=((0.0, 1.0), (0.99, 0.01), (1.0, 0.01)),
    ),
    input_domain=(0.0, 1.0),
    monotone_direction="decreasing",
    observed_range=(0.012, 0.105),
)
"""A named, shipped ``CurveSpec`` reproducing ``health_engine_metrics.py``'s
``metric_goodness`` function's exact shape — ``max(100.0 * (1.0 - rate),
1.0)`` — rescaled from its native ``[1, 100]`` output range down to this
library's ``[0, 1]`` curve contract as ``max(1.0 - rate, 0.01)``. That
rescaled shape is exactly a decreasing piecewise-linear curve from
``(0.0, 1.0)`` down to ``(0.99, 0.01)`` (slope -1, i.e. ``curve(x) == 1 - x``
for ``x`` in ``[0, 0.99]``), then FLAT at the ``0.01`` floor from
``(0.99, 0.01)`` to ``(1.0, 0.01)`` — see
tests/test_health_engine_curve_transition.py for the full derivation this
constant is built from.

WHY THIS EXISTS: the design doc's transition rule requires that "the first
shipped model must reproduce today's nine scores" — i.e. wiring any of the
nine real metrics through this curve library must not silently change their
current scoring behavior. Before this constant existed, that regression
guarantee lived only as knots re-derived by hand, independently, in two
places (tests/test_health_engine_curve_transition.py and this file's own
sibling test suite's ``_BADNESS_TRANSITION_CURVE`` fixture) — two
re-derivations of the same shape that could silently drift apart. This
constant is the ONE canonical, importable object either of those (or a
future PR wiring a real metric through the curve library) should reference,
so "the curve that reproduces today's pre-curve-layer scoring" has exactly
one definition.

WHAT THIS IS **NOT**: this is NOT a general-purpose default curve for future
metrics to use. It exists solely to make TODAY's transition rule (matching
``metric_goodness``'s specific shape) reusable and regression-safe. Once the
model-as-data format lands (the D1/D2 rulings still pending with Lei) and
metrics get their own declared curves, a given metric may well want a
different shape entirely — nothing about this constant should be read as
"the" curve the library recommends in general.

``observed_range=(0.012, 0.105)`` — engram-alpha#1991 PR4, round 5 (colleague
review, Borges, PR #2494): the design doc's own Panel B measured
stale-derivation-rate natural experiment (two plotted points, 1.2% and
10.5% — see docs/health-score-v2-design.md, "Layer 3" section). This is
reused here, on the shipped constant itself, as the one shipped example of a
real ``observed_range`` — round 4 had only ever set this same value on a
LOCAL test-fixture copy (``tests/test_health_engine_curves.py``'s
``_BADNESS_TRANSITION_CURVE``, built via ``dataclasses.replace`` off this
very object), which meant the non-saturation property test never actually
exercised anything this PR ships. Putting the value on the base object
closes that gap. As with the rest of this docstring, this is NOT a claim
that ``(0.012, 0.105)`` is "the" observed range for every future use of this
curve — it is the one natural-experiment range on record for the metric this
constant's shape reproduces (stale-derivation rate), reused here for the
same reason the curve shape itself is reused: one canonical definition
instead of a second independent re-derivation.

V1 SUCCESSOR (engram-alpha#1991 PR5, "the dream-side contract"): this
constant is superseded, for REAL per-metric scoring purposes, by
``BADNESS_DEFAULT_CURVE`` below — the same shape, under a name that does not
claim to be "legacy" or "the goodness transition" but simply "the v1 default
badness curve", per Kepler's PR5 clarification 3 ("Name it honestly ... and
state in the model file that per-metric differentiation is post-v1"). This
constant (``LEGACY_GOODNESS_TRANSITION_CURVE``) is kept, UNCHANGED, as the
object tests/test_health_engine_curve_transition.py and
tests/test_health_engine_curves.py already reference by name — it continues
to serve its original regression-safety role (reproducing
``metric_goodness``'s exact shape) and is not itself wired into the real
scoring path health_engine_model.py builds. A new metric wanting the v1
default badness curve should reference ``BADNESS_DEFAULT_CURVE``, not this
one.
"""


# ---------------------------------------------------------------------------
# BADNESS_DEFAULT_CURVE — engram-alpha#1991 PR5 ("the dream-side contract"),
# Kepler's clarification 3, 2026-09-03.
# ---------------------------------------------------------------------------

BADNESS_DEFAULT_CURVE = CurveSpec(
    name="badness_default",
    curve_fn=functools.partial(
        piecewise_linear,
        knots=((0.0, 1.0), (0.99, 0.01), (1.0, 0.01)),
    ),
    input_domain=(0.0, 1.0),
    monotone_direction="decreasing",
    # JUDGMENT CALL (flagged in the PR5 report): no observed_range set here,
    # unlike LEGACY_GOODNESS_TRANSITION_CURVE's (0.012, 0.105) — that range
    # is the stale-derivation-rate natural experiment specifically, and this
    # curve is now shared across the health-v2 model's SIX admitted metrics
    # (health_engine_model.py), with plausibly very different natural
    # ranges (embedding_coverage's typical badness rate is not
    # stale_derivation_rate's). Stamping one metric's observed range onto a
    # now general-purpose curve would misrepresent it as the shared
    # normal-operating range for all six; leaving it unset is the honest
    # default until real per-metric ranges are recorded (this PR's own
    # "observed_range auto-population" carrier, still out of scope, per
    # PR4's original deferral).
    observed_range=None,
)
"""The v1 default curve for EVERY admitted metric in health_engine_model.py's
``MODEL`` (engram-alpha#1991 PR5) — the SAME decreasing piecewise-linear
shape as ``LEGACY_GOODNESS_TRANSITION_CURVE`` above (``(0.0,1.0) ->
(0.99,0.01) -> (1.0,0.01)``), reproducing ``health_engine_metrics.
metric_goodness``'s exact transform rescaled to this library's ``[0,1]``
contract — see that constant's own docstring for the full derivation; it is
not repeated here since the shape is byte-for-byte identical.

WHY A SEPARATE CONSTANT rather than reusing ``LEGACY_GOODNESS_TRANSITION_
CURVE`` directly (Kepler's clarification 3, "the judgment call, accepted"):
one curve, shipped under an HONEST v1 name — "badness_default" says what it
is (the default curve for badness-rate metrics, v1) rather than "legacy" or
"transition", which would misdescribe its role once it is the REAL curve
driving REAL scores, not a regression-safety artifact for a formula that no
longer exists in the composition path. Building it as a genuinely separate
object (not a ``dataclasses.replace`` of the legacy one) also means
tests/test_health_engine_curves.py's existing property-contract suite,
which imports ``LEGACY_GOODNESS_TRANSITION_CURVE`` by name, needs ZERO
changes — "least churn", per the PR5 spec's explicit preference.

WHAT THIS IS **NOT**: not a claim that one curve shape suits every metric
forever — see health_engine_model.py's module docstring and its
``CURVES``/``MetricBinding`` docstrings for the explicit "per-metric curve
differentiation is future work" statement this constant's shared use is
scoped by.
"""


# ---------------------------------------------------------------------------
# GOODNESS_DEFAULT_CURVE — engram-alpha#2518 (health-v2 engine: flip
# Aspect-B (retrievability) signals from badness-complement to literal
# coverage, with direction moved into the curve). Lei's ruling,
# 2026-09-03T23:22Z, charge-board record: raw signal = literal meaning of
# its own name; ALL direction, including polarity, lives in the curve.
# ---------------------------------------------------------------------------

GOODNESS_DEFAULT_CURVE = CurveSpec(
    name="goodness_default",
    curve_fn=functools.partial(
        piecewise_linear,
        knots=((0.0, 0.01), (0.01, 0.01), (1.0, 1.0)),
    ),
    input_domain=(0.0, 1.0),
    monotone_direction="increasing",
    # Same reasoning as BADNESS_DEFAULT_CURVE's own observed_range=None
    # above: no natural-experiment data has been recorded yet for any of
    # the four literal-coverage metrics this curve now scores
    # (embedding_coverage, recall_summary_coverage, recall_keywords_
    # coverage, giant_component_fraction) — leaving it unset is the honest
    # default until real per-metric ranges exist.
    observed_range=None,
)
"""The v1 default curve for the four literal-coverage Aspect-B
(retrievability) metrics in health_engine_model.py's ``MODEL``
(``embedding_coverage``, ``recall_summary_coverage``,
``recall_keywords_coverage``, ``giant_component_fraction``) — bound as
``"goodness_default"`` in that module's ``CURVES`` registry, replacing
``"badness_default"`` on exactly those four ``MetricBinding``s.

MIRROR DERIVATION (so a future reader can re-verify these knots without
re-deriving from scratch): this is NOT a new curve shape — it is
``BADNESS_DEFAULT_CURVE`` reflected through the badness/coverage identity
``c = 1 - r``, chosen so the SAME underlying badness level always produces
the SAME score whether read through the old badness-rate signal or the new
literal-coverage signal (i.e. an unchanged graph's composed score does not
move because of this PR). Concretely, for every literal-coverage input
``c`` this curve must satisfy ``g(c) == f(1 - c)``, where ``f`` is
``BADNESS_DEFAULT_CURVE``'s own ``curve_fn`` (knots
``((0.0, 1.0), (0.99, 0.01), (1.0, 0.01))`` on badness-rate input ``r``).
Evaluating that identity at ``f``'s own three knots gives this curve's three
knots directly: ``g(0.0) = f(1.0) = 0.01``; ``g(0.01) = f(0.99) = 0.01``;
``g(1.0) = f(0.0) = 1.0`` — already in increasing-``c`` order, no
re-sorting needed. Verified by direct computation (both curve functions
evaluated and compared, not just algebraically reasoned) at
``c in {0.0, 0.005, 0.01, 0.02, 0.3, 0.5, 0.7, 0.9, 0.99, 0.995, 1.0}``,
matching to floating-point precision at every sample; the same property is
pinned as a permanent regression test in
tests/test_health_engine_curves.py.
"""
