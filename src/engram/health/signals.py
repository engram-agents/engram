"""health.signals — L1: signals as arithmetic formulas over L0 measurements
(engram-alpha#2537, slice 2 of the health-v2 SSoT refactor).

Design-of-record: engram-alpha#2537's design charge, the "five layers"
section — "L1 SIGNALS: arithmetic over L0" — plus the L0/L1 boundary rules
it states explicitly:

  - "The empty-denominator -> UNAVAILABLE policy is L1, not L0. It is
    decidable from denominator == 0, so changing it is a MINOR bump."
  - "embedding_coverage returns UNAVAILABLE when the embedder is
    structurally absent. That is not derivable from any count [at L1] --
    it must be STORED [at L0]" -- the environment-gate booleans
    (``embedder_installed_available`` / ``embedder_config_enabled``) are
    read here, at L1, to decide the gate; they are not re-derived.

Every function in this module takes the L0 measurements dict
(``health.measurements.compute_all_measurements(conn)``'s return value) --
never a ``sqlite3.Connection`` -- and returns ``float | _Unavailable``. This
is the load-bearing property the whole slice exists to establish: a signal
computed from a STORED L0 snapshot must be byte-identical to a signal
computed from a live graph read, because the arithmetic here is the only
thing standing between them.

Ported, not re-derived: this module is ``tests/test_health_measurements.py``'s
own ``_derive_signals_from_measurements`` helper turned into real shipped
code -- that test function's docstring says as much ("this is what slice 2
will ship as real code"), and its own acceptance test
(``TestAcceptanceCriterion::test_l0_decomposition_matches_live_signals_
exactly``) already proved this exact arithmetic reproduces every one of the
nine live ``health.metrics`` signal functions' output exactly, on the same
connection, for a genuinely-populated fixture -- not approximately, not on
a contrived all-zero graph. That test is the acceptance evidence for this
module's correctness; it predates this file by definition (slice 1 wrote
it before slice 2 could exist) and is why this port carries low risk
despite the formulas never having been "run for real" as shipped code
before this commit.

``health.metrics``'s nine SQL-querying functions are UNCHANGED and are NOT
retired by this module -- they remain the live acceptance-test oracle
(``tests/test_health_measurements.py`` and, going forward,
``tests/test_health_signals.py``) and the historical/documentary record of
each signal's exact SQL. What changes is which path ``health.engine``'s
``build_snapshot`` actually calls: after this slice, it calls THIS module
against a stored L0 dict, not ``health.metrics`` against a live connection.
"""

from __future__ import annotations

from typing import Callable

from .config import UNAVAILABLE, _Unavailable

MeasurementDict = dict  # health.measurements.compute_all_measurements()'s return shape


def _rate(numerator: int, denominator: int) -> "float | _Unavailable":
    """Shared empty-denominator policy for every rate/coverage signal below.

    ALL NINE signals declare UNAVAILABLE (never "clean" 0.0, never a
    ZeroDivisionError) on an empty population -- ``open_contradiction_rate``
    was the last holdout to flip from "clean" to "unavailable"
    (engram-alpha#2520 round 2). Centralized here so the policy is decided
    once, matching ``tests/test_health_measurements.py``'s own
    ``_rate`` helper exactly (that test's own comment: "a fresh throwaway
    derivation without this branch silently mismatched on
    ``contradictions_current == 0``, the exact condition this function
    exists to handle correctly instead of raising").

    This IS the L1 decision the design charge names explicitly: "The
    empty-denominator -> UNAVAILABLE policy is L1, not L0 ... this is what
    makes the PR5 round-2 change retro-applicable -- a direct, checkable
    win." A future change to this one function is a MINOR version bump,
    fully retro-applicable to every stored L0 snapshot -- the property this
    whole slice exists to buy.
    """
    if denominator == 0:
        return UNAVAILABLE
    return numerator / denominator


def stale_derivation_rate(m: MeasurementDict) -> "float | _Unavailable":
    """Fraction of current derivations carrying a pending supersede marker.
    Mirrors ``health.metrics.stale_derivation_rate``'s own SQL exactly."""
    return _rate(m["derivations_pending_supersede_marker"], m["derivations_current"])


def taint_rate(m: MeasurementDict) -> "float | _Unavailable":
    """Fraction of current claim-bearing nodes carrying a pending retract
    marker. Mirrors ``health.metrics.taint_rate``'s own SQL exactly."""
    return _rate(m["claim_bearing_pending_retract_marker"], m["claim_bearing_current"])


def open_contradiction_rate(m: MeasurementDict) -> "float | _Unavailable":
    """Fraction of current contradictions still open. Mirrors
    ``health.metrics.open_contradiction_rate``'s own SQL exactly --
    including the engram-alpha#2520 round-2 flip to UNAVAILABLE-on-empty
    this module's ``_rate`` already encodes uniformly."""
    return _rate(m["contradictions_open"], m["contradictions_current"])


def uncited_observation_rate(m: MeasurementDict) -> "float | _Unavailable":
    """Fraction of current observations with no ``evidence_id``. Mirrors
    ``health.metrics.uncited_observation_rate``'s own SQL exactly."""
    return _rate(m["observations_without_evidence_id"], m["observations_current"])


def dangling_evidence_rate(m: MeasurementDict) -> "float | _Unavailable":
    """Fraction of current evidence nodes cited by no current observation.
    Mirrors ``health.metrics.dangling_evidence_rate``'s own SQL exactly."""
    return _rate(m["evidence_uncited"], m["evidence_current"])


def recall_summary_coverage(m: MeasurementDict) -> "float | _Unavailable":
    """Fraction of current claim-bearing nodes that DO have a
    ``recall_summary`` -- literal coverage (1.0 = fully covered), per
    engram-alpha#2518's polarity flip (2026-09-04): the L0 numerator is
    still a "without" count (measurement definitions are untouched by that
    flip), so the L1 formula is ``1 - without/total``, not the raw rate.
    Mirrors ``health.metrics.recall_summary_coverage``'s own arithmetic
    exactly (including that flip)."""
    rate = _rate(m["claim_bearing_without_recall_summary"], m["claim_bearing_current"])
    if rate is UNAVAILABLE:
        return UNAVAILABLE
    return 1.0 - rate


def recall_keywords_coverage(m: MeasurementDict) -> "float | _Unavailable":
    """Fraction of current claim-bearing nodes that DO have parseable
    ``recall_keywords`` -- literal coverage, same #2518 polarity as
    ``recall_summary_coverage`` above. Mirrors
    ``health.metrics.recall_keywords_coverage``'s own arithmetic exactly."""
    rate = _rate(m["claim_bearing_without_recall_keywords"], m["claim_bearing_current"])
    if rate is UNAVAILABLE:
        return UNAVAILABLE
    return 1.0 - rate


def embedding_coverage(m: MeasurementDict) -> "float | _Unavailable":
    """Fraction of current claim-bearing nodes with a non-NULL embedding --
    literal coverage, same #2518 polarity.

    THE ONE SIGNAL WITH A SEPARATE, EARLIER UNAVAILABLE GATE: the design
    charge names this explicitly -- "embedding_coverage returns UNAVAILABLE
    when the embedder is structurally absent. That is not derivable from
    any count. Without it, an old snapshot cannot reconstruct why the
    metric was unavailable." The two environment-fact booleans
    (``embedder_installed_available``, ``embedder_config_enabled`` -- L0,
    read directly off the environment, not the graph) are checked FIRST,
    before the empty-denominator question even applies. Mirrors
    ``health.metrics.embedding_coverage``'s own two-gate structure exactly."""
    if not (m["embedder_installed_available"] and m["embedder_config_enabled"]):
        return UNAVAILABLE
    rate = _rate(m["claim_bearing_null_embedding"], m["claim_bearing_current"])
    if rate is UNAVAILABLE:
        return UNAVAILABLE
    return 1.0 - rate


def giant_component_fraction(m: MeasurementDict) -> "float | _Unavailable":
    """The literal connected fraction (largest-component-size / total),
    NOT its complement -- no ``1 - `` here at all, unlike the four coverage
    signals above. The BFS that finds the giant component's size is L0
    (``claim_bearing_giant_component_size`` -- "a MEASUREMENT, not a
    formula", per the design charge: "the BFS is how it is read off the
    graph; the arithmetic 1 - size/total is L1" -- NOTE: that line in the
    design charge predates engram-alpha#2518's polarity flip removing the
    subtraction; the arithmetic here is the current, post-flip form).
    Mirrors ``health.metrics.giant_component_fraction``'s own arithmetic
    exactly."""
    return _rate(m["claim_bearing_giant_component_size"], m["claim_bearing_current"])


# ---------------------------------------------------------------------------
# Registry -- name -> L1 formula, mirroring health.measurements.MEASUREMENTS'
# own flat-registry shape at the layer above it.
# ---------------------------------------------------------------------------

SIGNALS: dict[str, "Callable[[MeasurementDict], float | _Unavailable]"] = {
    "stale_derivation_rate": stale_derivation_rate,
    "taint_rate": taint_rate,
    "open_contradiction_rate": open_contradiction_rate,
    "uncited_observation_rate": uncited_observation_rate,
    "dangling_evidence_rate": dangling_evidence_rate,
    "recall_summary_coverage": recall_summary_coverage,
    "recall_keywords_coverage": recall_keywords_coverage,
    "embedding_coverage": embedding_coverage,
    "giant_component_fraction": giant_component_fraction,
}


def compute_all_signals(measurements: MeasurementDict) -> dict:
    """Run every registered L1 signal against one L0 measurements dict,
    returning ``{signal_name: float | _Unavailable}``. The snapshot-facing
    entry point for this layer -- mirrors
    ``health.measurements.compute_all_measurements``'s own shape at L0."""
    return {name: fn(measurements) for name, fn in SIGNALS.items()}
