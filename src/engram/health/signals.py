"""health.signals — L1: signals as arithmetic formulas over L0 measurements
(engram-alpha#2537, slice 2 of the health-v2 SSoT refactor; engram-alpha#2518
adds ``SignalDef``/``SIGNAL_DEFS`` -- each signal's arithmetic declared as
DATA, with the callables in ``SIGNALS`` built from it, so the numerator /
denominator / gate keys are machine-readable rather than only living inside
function bodies).

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

engram-alpha#2518 (this commit) does not change any signal's arithmetic or
output -- every value is bit-identical by construction, since ``SIGNALS``
is now BUILT from ``SIGNAL_DEFS`` via ``_build`` rather than hand-written
independently. What's new is that the numerator/denominator/gate keys are
now data (``SIGNAL_DEFS``, machine-readable via ``signal_defs_jsonable``),
not only prose inside each function body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

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


# ---------------------------------------------------------------------------
# SignalDef -- declarative L1 arithmetic (engram-alpha#2518). Each entry
# names the L0 numerator/denominator keys, the formula shape, and any
# environment gates -- so a consumer (e.g. the viz loader) can read "what is
# this signal made of" without hand-copying a table out of function bodies.
# The callables in SIGNALS below are BUILT from this data, not hand-written
# independently of it -- one source.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalDef:
    """Declarative description of one L1 signal's arithmetic over L0.

    ``numerator`` / ``denominator`` are L0 measurement names (keys into the
    dict returned by ``health.measurements.compute_all_measurements``).
    ``form`` selects the arithmetic: ``"rate"`` is ``numerator /
    denominator``; ``"one_minus_rate"`` is ``1 - numerator / denominator``
    (the three coverage signals' polarity flip, engram-alpha#2518).
    ``gates`` are L0 boolean measurement names that must ALL be truthy
    before the rate is even attempted -- checked FIRST, before the
    empty-denominator question -- else the signal is UNAVAILABLE
    regardless of the counts. Only ``embedding_coverage`` has any.
    ``doc`` carries over the original hand-written function's docstring
    text, so porting to data loses no documentation.
    """

    name: str
    numerator: str
    denominator: str
    form: "Literal['rate', 'one_minus_rate']"
    gates: tuple[str, ...] = field(default_factory=tuple)
    doc: str = ""

    @property
    def formula_text(self) -> str:
        """Human-readable rendering of this signal's arithmetic, e.g.
        ``"numerator / denominator"`` or, for the coverage polarity,
        ``"1 - numerator / denominator"`` -- with any gate keys appended
        in words when present."""
        if self.form == "rate":
            text = f"{self.numerator} / {self.denominator}"
        else:
            text = f"1 - {self.numerator} / {self.denominator}"
        if self.gates:
            gate_words = " and ".join(self.gates)
            text += f" (requires {gate_words})"
        return text


SIGNAL_DEFS: "dict[str, SignalDef]" = {
    d.name: d
    for d in (
        SignalDef(
            "pending_premise_rate",
            numerator="claim_bearing_pending_marker",
            denominator="claim_bearing_current",
            form="rate",
            doc=(
                "Fraction of current claim-bearing nodes carrying a pending "
                "cascade marker (retract or supersede) -- premises currently "
                "not available for derivation. Unifies the prior taint_rate / "
                "stale_derivation_rate (engram-alpha#2905 PR-B): both measured "
                "the same underlying condition (a pending marker blocking a "
                "node as a premise) under two separate mechanisms."
            ),
        ),
        SignalDef(
            "open_contradiction_rate",
            numerator="contradictions_open",
            denominator="contradictions_current",
            form="rate",
            doc=(
                "Fraction of current contradictions still open. Mirrors "
                "``health.metrics.open_contradiction_rate``'s own SQL "
                "exactly -- including the engram-alpha#2520 round-2 flip "
                "to UNAVAILABLE-on-empty this module's ``_rate`` already "
                "encodes uniformly."
            ),
        ),
        SignalDef(
            "uncited_observation_rate",
            numerator="observations_without_evidence_id",
            denominator="observations_current",
            form="rate",
            doc=(
                "Fraction of current observations with no "
                "``evidence_id``. Mirrors "
                "``health.metrics.uncited_observation_rate``'s own SQL "
                "exactly."
            ),
        ),
        SignalDef(
            "dangling_evidence_rate",
            numerator="evidence_uncited",
            denominator="evidence_current",
            form="rate",
            doc=(
                "Fraction of current evidence nodes cited by no current "
                "observation. Mirrors "
                "``health.metrics.dangling_evidence_rate``'s own SQL "
                "exactly."
            ),
        ),
        SignalDef(
            "recall_summary_coverage",
            numerator="claim_bearing_without_recall_summary",
            denominator="claim_bearing_current",
            form="one_minus_rate",
            doc=(
                "Fraction of current claim-bearing nodes that DO have a "
                "``recall_summary`` -- literal coverage (1.0 = fully "
                "covered), per engram-alpha#2518's polarity flip "
                "(2026-09-04): the L0 numerator is still a \"without\" "
                "count (measurement definitions are untouched by that "
                "flip), so the L1 formula is ``1 - without/total``, not "
                "the raw rate. Mirrors "
                "``health.metrics.recall_summary_coverage``'s own "
                "arithmetic exactly (including that flip)."
            ),
        ),
        SignalDef(
            "recall_keywords_coverage",
            numerator="claim_bearing_without_recall_keywords",
            denominator="claim_bearing_current",
            form="one_minus_rate",
            doc=(
                "Fraction of current claim-bearing nodes that DO have "
                "parseable ``recall_keywords`` -- literal coverage, same "
                "#2518 polarity as ``recall_summary_coverage`` above. "
                "Mirrors ``health.metrics.recall_keywords_coverage``'s "
                "own arithmetic exactly."
            ),
        ),
        SignalDef(
            "embedding_coverage",
            numerator="claim_bearing_null_embedding",
            denominator="claim_bearing_current",
            form="one_minus_rate",
            gates=("embedder_installed_available", "embedder_config_enabled"),
            doc=(
                "Fraction of current claim-bearing nodes with a "
                "non-NULL embedding -- literal coverage, same #2518 "
                "polarity.\n\n"
                "THE ONE SIGNAL WITH A SEPARATE, EARLIER UNAVAILABLE "
                "GATE: the design charge names this explicitly -- "
                "\"embedding_coverage returns UNAVAILABLE when the "
                "embedder is structurally absent. That is not derivable "
                "from any count. Without it, an old snapshot cannot "
                "reconstruct why the metric was unavailable.\" The two "
                "environment-fact booleans "
                "(``embedder_installed_available``, "
                "``embedder_config_enabled`` -- L0, read directly off "
                "the environment, not the graph) are checked FIRST, "
                "before the empty-denominator question even applies. "
                "Mirrors ``health.metrics.embedding_coverage``'s own "
                "two-gate structure exactly."
            ),
        ),
        SignalDef(
            "giant_component_fraction",
            numerator="claim_bearing_giant_component_size",
            denominator="claim_bearing_current",
            form="rate",
            doc=(
                "The literal connected fraction "
                "(largest-component-size / total), NOT its complement -- "
                "no ``1 - `` here at all, unlike the three coverage "
                "signals above. The BFS that finds the giant component's "
                "size is L0 (``claim_bearing_giant_component_size`` -- "
                "\"a MEASUREMENT, not a formula\", per the design charge: "
                "\"the BFS is how it is read off the graph; the "
                "arithmetic 1 - size/total is L1\" -- NOTE: that line in "
                "the design charge predates engram-alpha#2518's polarity "
                "flip removing the subtraction; the arithmetic here is "
                "the current, post-flip form). Mirrors "
                "``health.metrics.giant_component_fraction``'s own "
                "arithmetic exactly."
            ),
        ),
    )
}


def _build(d: SignalDef) -> "Callable[[MeasurementDict], float | _Unavailable]":
    """Build the callable form of one ``SignalDef`` -- gates (any falsy ->
    UNAVAILABLE), then the shared ``_rate`` empty-denominator policy, then
    ``1.0 - rate`` for ``one_minus_rate`` when the rate is not UNAVAILABLE.
    This is the ONE place ``SignalDef`` data becomes an executable signal;
    ``SIGNALS`` below is built entirely from this."""

    def _fn(m: MeasurementDict) -> "float | _Unavailable":
        if d.gates and not all(m[gate] for gate in d.gates):
            return UNAVAILABLE
        rate = _rate(m[d.numerator], m[d.denominator])
        if rate is UNAVAILABLE:
            return UNAVAILABLE
        if d.form == "one_minus_rate":
            return 1.0 - rate
        return rate

    _fn.__name__ = d.name
    _fn.__doc__ = d.doc
    return _fn


# ---------------------------------------------------------------------------
# Registry -- name -> L1 formula, mirroring health.measurements.MEASUREMENTS'
# own flat-registry shape at the layer above it. Built from SIGNAL_DEFS
# above -- the callables and the declarative metadata share one source.
# ---------------------------------------------------------------------------

SIGNALS: dict[str, "Callable[[MeasurementDict], float | _Unavailable]"] = {
    name: _build(d) for name, d in SIGNAL_DEFS.items()
}

# Thin aliases so existing importers/tests referring to the module-level
# function names keep working unchanged.
pending_premise_rate = SIGNALS["pending_premise_rate"]
open_contradiction_rate = SIGNALS["open_contradiction_rate"]
uncited_observation_rate = SIGNALS["uncited_observation_rate"]
dangling_evidence_rate = SIGNALS["dangling_evidence_rate"]
recall_summary_coverage = SIGNALS["recall_summary_coverage"]
recall_keywords_coverage = SIGNALS["recall_keywords_coverage"]
embedding_coverage = SIGNALS["embedding_coverage"]
giant_component_fraction = SIGNALS["giant_component_fraction"]


def compute_all_signals(measurements: MeasurementDict) -> dict:
    """Run every registered L1 signal against one L0 measurements dict,
    returning ``{signal_name: float | _Unavailable}``. The snapshot-facing
    entry point for this layer -- mirrors
    ``health.measurements.compute_all_measurements``'s own shape at L0."""
    return {name: fn(measurements) for name, fn in SIGNALS.items()}


def signal_defs_jsonable() -> dict:
    """JSON-safe rendering of ``SIGNAL_DEFS`` -- ``{name: {"numerator",
    "denominator", "form", "gates" (list), "formula", "doc"}}``. This is the
    machine-readable source a consumer (e.g. the viz loader) reads to know
    which L0 key is the numerator and which is the denominator for each
    signal, without hand-copying a table out of this module's source.
    ``doc`` carries the signal's own hand-written description (see
    ``SignalDef.doc``'s own docstring) so a detail view can show it
    directly, without re-deriving it from the formula."""
    return {
        name: {
            "numerator": d.numerator,
            "denominator": d.denominator,
            "form": d.form,
            "gates": list(d.gates),
            "formula": d.formula_text,
            "doc": d.doc,
        }
        for name, d in SIGNAL_DEFS.items()
    }
