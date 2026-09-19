"""health.model — the scoring-model file (engram-alpha#1991, PR5 of 5,
the dream-side contract).

Design-of-record: engram-alpha#1991, issue comment 5527066386 (Kepler,
2026-09-03, "The dream-side contract for health-v2 — DRAFT 2") §1 (the
scoring model's shape), plus five follow-up clarifications from Kepler
confirming how to resolve the ambiguities a literal reading of §1 leaves
open (recorded inline below, at the field/section they resolve).

Scope of this module: the DATA — curves, signals, aspects, metric bindings —
that drives the real per-metric / per-aspect / composed scoring formula
(clarification 1, wired into ``health/engine.py``'s ``build_snapshot``), plus
the validator, ``validate_model``, and the ``load()`` entry point that runs
it.

ROUND 3 (engram-alpha#1991/#2520, Kepler's ruling, 2026-09-03) CHANGED THE
SHAPE OF VALIDATION ITSELF, replacing round 2's row-local population check.
Round 2's check compared a detector's declared population against a
``population`` field living on the SAME ``MetricBinding`` row — both written
by the same author in the same breath, so it could not catch a detector
pointed at a population that matches its own row's belief but not the
metric's REAL denominator (declared on ``health.config.MetricDef.
denominator_population``). Kepler's ruling: "the check is only a check if it
reads that field." This module therefore now has **no import-time side
effects** — it defines the model DATA (``MODEL_SPEC``, an unvalidated
``ScoringModel``) and the dataclasses, but does NOT construct a validated
model at import time. The real, validated ``MODEL`` is built by
``health/engine.py``, via this module's ``load(spec, config)`` function,
*after* both ``MODEL_SPEC`` (this module) and the real ``CONFIG`` (health.
config.EngineConfig, populated in health/engine.py) exist — only then
can ``validate_model`` cross-check a detector's population against
``config.metric(signal).denominator_population``, the real source of truth.
This is why ``ScoringModel`` no longer validates itself in ``__post_init__``
the way ``health.config.EngineConfig`` still does (see that class's
own docstring for the "validate on every construction" discipline this
module deliberately DIVERGES from, and why: this module's validation is not
expressible without a config that does not exist until after CONFIG is
built).

This module is DELIBERATELY SEPARATE from ``health/config.py``'s
``EngineConfig``/``AspectDef``/``MetricDef`` — the two are NOT redundant,
they cover different layers (Kepler, draft-2, clarification on
denominators): ``health.config``'s schema is the RAW-SIGNAL-COMPUTATION
layer (denominator population, empty-denominator policy, the actual
``signal_fn`` callable) and is UNCHANGED by this PR. This module is the
SCORING layer draft-2 describes: which aspect a signal belongs to, its
curve, its importance, and its detector/repair binding for the dream side.
``health/engine.py``'s ``build_snapshot`` now reads raw signals via the OLD
layer (``health.config.EngineConfig`` / ``health.engine.CONFIG``) and
turns them into scores via ``health.engine.MODEL`` — a validated
``ScoringModel`` health/engine.py builds from THIS module's ``MODEL_SPEC``
via ``load()`` (round 3, see below) — see that file's module docstring for
the fuller "two systems, on purpose, here's why" account, including the
parts of the old layer that are now vestigial for scoring specifically
(flagged there, not glossed over here).
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from typing import Callable

from . import metrics
from .config import EngineConfig, _Unavailable, _check_weights_sum
from .curves import (
    BADNESS_DEFAULT_CURVE,
    BADNESS_PENDING_PREMISE_CURVE,
    CurveSpec,
    GOODNESS_COVERAGE_CURVE,
    GOODNESS_DEFAULT_CURVE,
)


def _log(msg: str) -> None:
    """Loud, unconditional stderr log — mirrors engram_snapshot_worker.py's
    own ``_log`` convention ("a silent failure ... is exactly the class
    this module exists to eliminate, so every failure path here is noisy
    by design"). Used for the zero-admitted-aspect WARNING in
    ``validate_model`` below (design ruling: Kepler, engram-alpha#2520
    review comment, 2026-09-03, "the validator WARNS ... so the condition
    is visible at build time, not discovered from a snapshot") — same
    spirit applies here: a warning nobody is forced to look at (e.g.
    buried in a return value most callers never inspect) defeats the
    point of surfacing it loudly.
    """
    print(f"[health.model] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Model error
# ---------------------------------------------------------------------------


class ModelError(Exception):
    """Raised by ``validate_model`` / ``ScoringModel.__post_init__`` when the
    scoring model fails a sanity check. Lists EVERY violation found in one
    pass, not just the first — mirrors ``health.config.
    EngineConfigError``'s "see the whole problem at once" contract exactly
    (see that class's docstring for the Borges-incident reasoning behind it).

    JUDGMENT CALL (flagged in the PR5 report): a NEW exception type rather
    than reusing ``health.config.EngineConfigError`` — this model is a
    genuinely separate object (a ``ScoringModel``, not an ``EngineConfig``),
    and reusing the old error name would read as "something is wrong with
    the EngineConfig" when the actual failure is in the new scoring model.
    """


# ---------------------------------------------------------------------------
# Signals registry — clarification 2 (Kepler, 2026-09-03)
# ---------------------------------------------------------------------------
#
# "`signals:` = a name registry onto the existing nine functions. Nothing in
# their bodies changes. 'operator + population' in my draft is the
# descriptive shape the doc's closed-operator-set rule points at for the
# future; for PR5 the registry maps name -> existing function, and D2's
# requirement is satisfied by the model file naming rather than encoding.
# Say so in a comment so nobody later 'fixes' it into a DSL."
#
# THIS IS THAT COMMENT: SIGNALS below is a plain name -> callable mapping.
# It is NOT a DSL, it does NOT parse an "operator + population" expression,
# and it must never be "improved" into one inside this PR's scope — that is
# explicitly future work, gated on a ruling that has not been made.

SIGNALS: dict[str, Callable[[sqlite3.Connection], float | _Unavailable]] = {
    # engram-alpha#2905 PR-B round 2: stale_derivation_rate + taint_rate
    # retired from health/metrics.py and unified into pending_premise_rate
    # -- this registry (a plain name -> metrics.py-callable mapping, NOT
    # the same object as health.signals.SIGNALS, which round 1 already
    # updated) must name exactly what MODEL_SPEC's MetricBinding.signal
    # values resolve against (validate_model's "does not resolve in the
    # signals registry" check reads THIS dict's keys), or the real
    # pending_premise_rate MetricBinding below fails to validate.
    "pending_premise_rate": metrics.pending_premise_rate,
    "open_contradiction_rate": metrics.open_contradiction_rate,
    "uncited_observation_rate": metrics.uncited_observation_rate,
    "dangling_evidence_rate": metrics.dangling_evidence_rate,
    "recall_summary_coverage": metrics.recall_summary_coverage,
    "recall_keywords_coverage": metrics.recall_keywords_coverage,
    "embedding_coverage": metrics.embedding_coverage,
    "giant_component_fraction": metrics.giant_component_fraction,
}


# ---------------------------------------------------------------------------
# Curves registry — clarification 3 (Kepler, 2026-09-03)
# ---------------------------------------------------------------------------
#
# "One named curve for v1, the legacy transition shape re-purposed — accepted
# as the judgment call. Name it honestly (e.g. `badness_default`) and state
# in the model file that per-metric differentiation is post-v1 and that
# Lei's curve-carries-sensitivity argument is what the slot exists for."
#
# BADNESS_DEFAULT_CURVE itself is DECLARED in health/curves.py (the
# Layer-3 curve LIBRARY — this module only binds names to library objects,
# it does not define curve shapes) — see that file's docstring on
# BADNESS_DEFAULT_CURVE for the full "why a separate constant, why no
# observed_range" reasoning. Originally all six admitted metrics in
# MODEL_SPEC below shared this ONE curve — a DELIBERATE v1 simplification,
# not an oversight: per-metric curve differentiation is explicitly future
# work, and the slot exists in the MetricBinding schema (the ``curve``
# field is a per-metric name, not a global constant) so that future
# differentiation is a data change, not a code change. Lei's
# curve-carries-sensitivity argument (2026-09-02T14:32Z ruling, cited in
# health/engine.py's compose_aspect_scores docstring) is EXACTLY why that
# slot exists on every metric individually rather than once globally.
#
# [engram-alpha#2518, 2026-09-04] A second named curve,
# GOODNESS_DEFAULT_CURVE ("goodness_default"), joined the registry: Lei's
# ruling that raw signal = literal meaning of its own name, with ALL
# direction/polarity moved into the curve's own monotone_direction. The
# retrievability aspect's four metrics (embedding_coverage,
# recall_summary_coverage, recall_keywords_coverage,
# giant_component_fraction — all naturally coverage-style, HIGH is good)
# were bound to the increasing "goodness_default" curve instead of the
# decreasing "badness_default" curve; every other metric is unchanged.
# This is STILL the same "one curve per direction, not per metric" v1
# simplification the paragraph above describes — per-metric curve
# differentiation beyond direction remains future work — it is just no
# longer literally ONE shared curve object for all six admitted metrics.
#
# [engram-alpha#2905 PR-A, 2026-09-13] TWO MORE curves joined the registry
# from the 2026-09-13 Lei+Kepler curves session (see docs/design/
# health-v1-curves-session-2026-09-13.md): BADNESS_PENDING_PREMISE_CURVE
# ("badness_pending_premise") and GOODNESS_COVERAGE_CURVE
# ("goodness_coverage"). The four-metric "goodness_default" statement two
# paragraphs up is now ONLY TRUE OF "goodness_default" ITSELF — three of
# those four (embedding_coverage, recall_summary_coverage,
# recall_keywords_coverage) moved to the new increasing "goodness_coverage"
# curve instead (same shape shared by all three; importance differentiates
# them — see MODEL_SPEC below). "giant_component_fraction" alone remains
# bound to "goodness_default". taint_rate / stale_derivation_rate moved
# from "badness_default" to the new decreasing "badness_pending_premise"
# curve, as an interim per-metric rebind ahead of PR-B's unification of
# both signals into one L0 measurement (pending_premise_rate).

CURVES: dict[str, CurveSpec] = {
    "badness_default": BADNESS_DEFAULT_CURVE,
    "goodness_default": GOODNESS_DEFAULT_CURVE,
    "badness_pending_premise": BADNESS_PENDING_PREMISE_CURVE,
    "goodness_coverage": GOODNESS_COVERAGE_CURVE,
}


# ---------------------------------------------------------------------------
# Detector / repair registries — clarification 4 (Kepler, 2026-09-03)
# ---------------------------------------------------------------------------
#
# "Detector/repair registries are NAME-ONLY validation sets, never
# executables." A closed frozenset of valid `scan:<name>` / `lottery:<name>`
# / `tool:<name>` / `reflect` strings, checked at model-construction time.
# NO scanner or repair-tool logic is implemented here or anywhere in this
# PR — those arrive in the sleep-skill/dream-master change (draft-2 §7 item
# 6, explicitly out of scope for PR5).

REGISTERED_DETECTORS: frozenset[str] = frozenset({
    "scan:cascade-frontier",
    "scan:open-contradictions",
    "scan:null-embeddings",
    "scan:summary-absent",
    "lottery:tainted-valid",
    "lottery:stale-load-bearing",
    "lottery:contradictions",
    "reflect",
})

REGISTERED_REPAIRS: frozenset[str] = frozenset({
    "tool:cascade-resolve",
    "tool:resolve",
    "tool:embedding-backfill",
    "tool:batch-summary-fairy",
})


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectorRef:
    """One detector/lottery ROUTE reference: the scanner/lottery NAME plus
    the denominator POPULATION it ranges over (Kepler, addendum 3 on
    engram-alpha#1991, 2026-09-03, via Borges's colleague read of draft 2 —
    "the alias we both missed in the cat-3 row would have failed at
    construction instead of surviving four sections away from the rule").

    ``name`` is checked against ``REGISTERED_DETECTORS`` exactly as before
    this fold (a closed, name-only validation set — unaffected by this
    change). ``population`` is checked by ``validate_model`` below —
    ROUND 3 (Kepler's ruling on engram-alpha#1991/#2520, 2026-09-03)
    REPLACED the round-2 row-local check ("must equal the SAME metric's own
    ``MetricBinding.population`` field") with a real cross-module check:
    ``population`` must equal ``config.metric(the owning MetricBinding's
    signal).denominator_population`` — the metric's REAL declared
    denominator on ``health.config.MetricDef``, read via
    ``EngineConfig.metric()``, not a second independently-declared field on
    this module's own side that could drift from it while still agreeing
    with itself. This is THE field the row-local check could not validate
    (Kepler: "the check is only a check if it reads that field").
    """

    name: str
    population: str


@dataclass(frozen=True)
class MetricBinding:
    """One config-declared metric — draft-2 §1's "a metric IS a (signal,
    aspect) binding" (Lei's nuance, one syntax, no special case): EVERY
    metric, admitted or diagnostic-only, is declared with exactly this same
    shape; only ``importance`` differs (0.0 for a diagnostic-only metric).

    ROUND 3 (Kepler's ruling on engram-alpha#1991/#2520, 2026-09-03) REMOVED
    the round-2 ``population: str`` field this class used to carry. That
    field was an INDEPENDENTLY-declared duplicate of the metric's real
    denominator population (``health.config.MetricDef.
    denominator_population``), added under round 2's stated reasoning that a
    cross-module reference to the real ``CONFIG`` was blocked by a circular
    import. That reasoning was narrower than it read: an EAGER or LAZY
    IMPORT of ``health/engine.py``'s real ``CONFIG`` from inside THIS module
    genuinely is circular (``health/engine.py`` imports
    ``health/model.py`` first, confirmed by direct import-order
    inspection) — but a DEFERRED, parameter-PASSED reference never was
    blocked, since the cycle is created by the ``import`` statement itself,
    not by merely needing the value. Round 3 adopts exactly that
    deferred-injection shape, rather than "resolving" a cycle that a
    parameter-passed check could always have sidestepped: this module builds
    an UNVALIDATED spec at import time (``MODEL_SPEC``, below) and exposes a
    ``load(spec, config)`` function; ``health/engine.py`` calls ``load()``
    only AFTER its own real ``CONFIG`` exists and passes it in as an
    ordinary argument (no import of ``health/engine.py`` from this module,
    at any point), so ``validate_model`` can read ``config.metric(m.signal)
    .denominator_population`` directly — the real source of truth, per
    Kepler's ruling ("the check is only a check if it reads that field"). A
    row now declares ONLY the detector's own population (on ``DetectorRef``,
    below) — there is no second, independently-declared ``population`` on
    this class for it to silently agree OR disagree with; one fact, one
    declaration.

    ``detector`` is the PRIMARY detector (draft-2 §1's table: "primary,
    required for every admitted metric"), a ``DetectorRef`` (name +
    population) rather than a bare string (addendum 3). ``additional_
    detector`` is the OPTIONAL second route (draft-2's "additional"
    `lottery:` column), same ``DetectorRef`` shape — e.g. ``taint_rate``'s
    primary is ``scan:cascade-frontier`` and its additional route is
    ``lottery:tainted-valid``, both ranging over ``claim_bearing_nodes``.
    JUDGMENT CALL (flagged in the PR5 report, unchanged by addendum 3 and by
    round 3): modeled as two separate optional fields rather than a
    tuple/list of detectors — reads more clearly given the primary/
    additional asymmetry draft-2 itself describes (the primary is
    load-bearing per §1's "Change from draft 1" note; the additional route
    is opportunistic only), and the spec explicitly left the shape as "your
    call, document it." ``repair`` stays a bare string (addendum 3 scopes
    the new struct shape to ``detector``/``additional_detector`` only).

    ``curve``/``aspect``/``signal`` are NAMES resolved against the
    surrounding ``ScoringModel``'s ``curves``/``aspects``/``signals``
    registries — see ``validate_model`` below for the resolution checks.
    """

    signal: str
    aspect: str
    curve: str
    importance: float
    detector: DetectorRef | None = None
    additional_detector: DetectorRef | None = None
    repair: str | None = None
    dive_target: str | None = None


@dataclass(frozen=True)
class AspectSpec:
    """One config-declared aspect (draft-2 §1's ``aspects:`` section) —
    ``points`` is this aspect's share of the composed 0-100 score (design
    ruling: aspect ``points`` sum to 100 across all declared aspects, same
    numeric shape as ``health.config.AspectDef.weight`` before it,
    see ``health/engine.py``'s module docstring for why both exist).

    ``reference_metric`` is a PURE ANNOTATION (draft-2 §1: "reference =
    annotation, never a computational input") naming this aspect's most
    direct/representative admitted metric — e.g. retrievability's
    ``embedding_coverage``. It plays NO role in the aspect_score arithmetic
    (clarification 1's formula sums over every admitted metric by
    importance, not just the reference one).

    JUDGMENT CALL (flagged in the PR5 report, load-bearing): made OPTIONAL
    (``None`` default) rather than required on every aspect. Draft-2's own
    example annotates only ONE aspect (retrievability) with a
    reference_metric; the PR5 spec's own metric table likewise names a
    reference_metric only for retrievability. Requiring one on every aspect
    would be UNSATISFIABLE for ``provenance_integrity`` under the v1 metric
    table: BOTH of its metrics ship with importance 0 (diagnostic-only, per
    draft-2 §4 — "diagnostic until a repair exists"), so it has literally
    ZERO admitted metrics to name as a reference. See the validator below,
    and health/engine.py's module docstring for the significant downstream
    consequence this has for the composed score.
    """

    name: str
    points: float
    reference_metric: str | None = None


@dataclass(frozen=True)
class ScoringModel:
    """The full scoring model: curves + signals registries, the declared
    aspects, and the metric bindings that tie them together.

    DELIBERATELY NO ``__post_init__`` validation (round 3, Kepler's ruling on
    engram-alpha#1991/#2520, 2026-09-03) — this is a deliberate DIVERGENCE
    from ``health.config.EngineConfig``'s own "validate on every
    construction, not just the loader" discipline, not an oversight; see
    that class's docstring for the discipline this class no longer follows,
    and why: ``validate_model`` (below) now needs a real ``EngineConfig`` to
    cross-check a detector's declared population against the metric's real
    ``denominator_population`` — a ``ScoringModel`` can be constructed with
    only its own data (this module's own module-level ``MODEL_SPEC`` is
    exactly such a construction, deliberately UNVALIDATED at import time),
    but it cannot be VALIDATED without a config that, for the real
    production model, does not exist until ``health/engine.py`` builds its
    own ``CONFIG``. Use ``load(spec, config)`` (below) to construct-then-
    validate in one step once both halves exist; constructing a
    ``ScoringModel`` directly is for building an unvalidated spec (or a
    small fake fixture in a test), never a way to get an implicitly-checked
    production object.
    """

    curves: dict[str, CurveSpec]
    signals: dict[str, Callable[[sqlite3.Connection], float | _Unavailable]]
    aspects: tuple[AspectSpec, ...]
    metrics: tuple[MetricBinding, ...]


# ---------------------------------------------------------------------------
# validate_model
# ---------------------------------------------------------------------------


def validate_model(model: ScoringModel, config: EngineConfig) -> None:
    """Validate a ``ScoringModel`` against draft-2 §1's contract, cross-
    checked against the real ``EngineConfig`` (``config``). Raises
    ``ModelError`` listing EVERY violation found (not just the first) if any
    check fails — mirrors ``health.config.validate_config``'s "see
    the whole problem at once" contract; read that function before changing
    this one, per the PR5 spec's explicit instruction.

    ``config`` (NEW, round 3, Kepler's ruling on engram-alpha#1991/#2520,
    2026-09-03) is the real source of truth for a metric's denominator
    population: every ``MetricBinding``'s ``detector``/``additional_
    detector`` population is now checked against ``config.metric(m.signal)
    .denominator_population`` — see the population-check block below and
    ``EngineConfig.metric``'s own docstring. This REPLACES round 2's
    row-local check (a detector's population compared only against a
    ``population`` field on the same ``MetricBinding`` row, removed by this
    round) with a genuine cross-module check, per Kepler's ruling: "the
    check is only a check if it reads that field." Call via ``load(spec,
    config)`` below, not directly against a bare, still-unvalidated spec.

    Also runs a SEPARATE, NEW, non-raising code path (design ruling: Kepler,
    engram-alpha#2520 review comment, 2026-09-03, "hold out, renormalize,
    report", item 3) — a zero-admitted-metrics aspect WARNS via ``_log``
    rather than being collected into ``violations``: it is a valid,
    buildable model (see ``AspectSpec``'s own docstring for why
    ``provenance_integrity`` is unsatisfiable any other way under the v1
    table), not a schema error, but the condition must be visible at BUILD
    time per the ruling, "not discovered from a snapshot".
    """
    violations: list[str] = []

    if not model.aspects:
        violations.append(
            "model.aspects is empty — a model with zero aspects is a config "
            "error, not \"nothing to score\""
        )

    # --- aspect points sum to 100 -------------------------------------------
    if model.aspects:
        violations.extend(
            _check_weights_sum(model.aspects, "points", 100.0, "aspect points")
        )

    aspect_names = {a.name for a in model.aspects}
    curve_names = set(model.curves.keys())
    signal_names = set(model.signals.keys())

    if not model.metrics:
        violations.append(
            "model.metrics is empty — a model with zero metric bindings is a "
            "config error, not \"nothing to score\""
        )

    seen_pairs: set[tuple[str, str]] = set()
    admitted_by_aspect: dict[str, list[MetricBinding]] = {name: [] for name in aspect_names}

    for m in model.metrics:
        if m.signal not in signal_names:
            violations.append(
                f"metric binding {m.signal!r} does not resolve in the "
                f"signals registry"
            )
        if m.aspect not in aspect_names:
            violations.append(
                f"metric binding {m.signal!r} names undeclared aspect "
                f"{m.aspect!r}"
            )
        if m.curve not in curve_names:
            violations.append(
                f"metric binding {m.signal!r} names undeclared curve "
                f"{m.curve!r}"
            )

        pair = (m.signal, m.aspect)
        if pair in seen_pairs:
            violations.append(f"duplicate (signal, aspect) binding: {pair!r}")
        seen_pairs.add(pair)

        if m.importance < 0:
            violations.append(
                f"metric {m.signal!r} importance must be >= 0, got "
                f"{m.importance!r}"
            )

        # --- round 3 (Kepler, 2026-09-03): cross-validate every detector's
        # declared population against the metric's REAL denominator
        # population, read from the real EngineConfig — not a second,
        # independently-declared field on this same row (round 2's check,
        # replaced). A miss on config.metric() (no declared metric by this
        # name in config at all) is itself a violation, collected once per
        # metric binding rather than silently skipping the population check.
        config_metric = config.metric(m.signal)
        if config_metric is None:
            violations.append(
                f"metric {m.signal!r} is not declared in the EngineConfig "
                f"(config.metric({m.signal!r}) found nothing) — cannot "
                f"cross-validate its detector population against the real "
                f"denominator_population"
            )

        if m.detector is not None:
            if m.detector.name not in REGISTERED_DETECTORS:
                violations.append(
                    f"metric {m.signal!r} detector {m.detector.name!r} is "
                    f"not a member of REGISTERED_DETECTORS"
                )
            if (
                config_metric is not None
                and m.detector.population != config_metric.denominator_population
            ):
                violations.append(
                    f"metric {m.signal!r} detector population "
                    f"{m.detector.population!r} does not match the real "
                    f"denominator_population {config_metric.denominator_population!r} "
                    f"declared for this metric in EngineConfig"
                )
        if m.additional_detector is not None:
            if m.additional_detector.name not in REGISTERED_DETECTORS:
                violations.append(
                    f"metric {m.signal!r} additional_detector "
                    f"{m.additional_detector.name!r} is not a member of "
                    f"REGISTERED_DETECTORS"
                )
            if (
                config_metric is not None
                and m.additional_detector.population != config_metric.denominator_population
            ):
                violations.append(
                    f"metric {m.signal!r} additional_detector population "
                    f"{m.additional_detector.population!r} does not match "
                    f"the real denominator_population "
                    f"{config_metric.denominator_population!r} declared for "
                    f"this metric in EngineConfig"
                )
        if m.repair is not None and m.repair not in REGISTERED_REPAIRS:
            violations.append(
                f"metric {m.signal!r} repair {m.repair!r} is not a member "
                f"of REGISTERED_REPAIRS"
            )

        admitted = m.importance > 0
        if admitted:
            if m.detector is None:
                violations.append(
                    f"metric {m.signal!r} is admitted (importance="
                    f"{m.importance!r} > 0) but has no detector — every "
                    f"admitted metric must name a primary detector"
                )
            if m.repair is None:
                violations.append(
                    f"metric {m.signal!r} is admitted (importance="
                    f"{m.importance!r} > 0) but has no repair — every "
                    f"admitted metric must name a repair"
                )
            if m.aspect in admitted_by_aspect:
                admitted_by_aspect[m.aspect].append(m)

    # --- zero-admitted-aspect WARNING (NOT a violation) ---------------------
    # Design ruling (Kepler, engram-alpha#2520 review comment, 2026-09-03,
    # "hold out, renormalize, report", item 3): an aspect with zero admitted
    # metrics is a valid, buildable model — this fires UNCONDITIONALLY via
    # _log() at every construction where the condition holds, never
    # collected into `violations`, never raised. See health/engine.py's
    # compose_aspect_scores / _composition_coverage for what happens to a
    # zero-admitted aspect downstream (its point allotment is held out and
    # `composed` is renormalized over the rest).
    zero_admitted = sorted(
        name for name in aspect_names if not admitted_by_aspect.get(name)
    )
    if zero_admitted:
        _log(
            f"aspect(s) {zero_admitted!r} have ZERO admitted metrics "
            f"(importance > 0) declared — excluded from composition; each "
            f"one's point allotment is held out and `composed` is "
            f"renormalized over the remaining, scored aspects (design "
            f"ruling: Kepler, engram-alpha#2520 review comment, "
            f"2026-09-03, \"hold out, renormalize, report\")"
        )

    # --- reference_metric, when present, must name an admitted metric ------
    for a in model.aspects:
        if a.reference_metric is None:
            continue
        candidates = [
            m for m in model.metrics
            if m.aspect == a.name and m.signal == a.reference_metric
        ]
        if not candidates:
            violations.append(
                f"aspect {a.name!r} reference_metric {a.reference_metric!r} "
                f"does not name a metric bound to this aspect"
            )
        elif candidates[0].importance <= 0:
            violations.append(
                f"aspect {a.name!r} reference_metric {a.reference_metric!r} "
                f"is not admitted (importance must be > 0)"
            )

    if violations:
        raise ModelError("\n".join(violations))


# ---------------------------------------------------------------------------
# load — construct-then-validate against a real EngineConfig (round 3,
# Kepler's ruling on engram-alpha#1991/#2520, 2026-09-03)
# ---------------------------------------------------------------------------


def load(spec: ScoringModel, config: EngineConfig) -> ScoringModel:
    """The single entry point that turns an unvalidated ``ScoringModel``
    spec (e.g. ``MODEL_SPEC`` below) into a validated one, cross-checked
    against the real ``EngineConfig`` — ``config``'s denominator populations
    are the real source of truth for every detector's declared population
    (see ``validate_model``'s own docstring).

    Called from ``health/engine.py`` as ``health.model.load(
    health.model.MODEL_SPEC, CONFIG)``, placed textually AFTER
    ``CONFIG``'s own definition there — this is the whole point of
    splitting construction (this module, at import time, needs nothing but
    its own data) from validation (needs the real ``CONFIG``, which does
    not exist until ``health/engine.py`` builds it). Raises ``ModelError``
    (via ``validate_model``) naming every violation at once if the spec
    fails; returns ``spec`` unchanged (this function performs no copying or
    transformation — the ``ScoringModel`` object identity is the same
    before and after a successful ``load()``) on success.
    """
    validate_model(spec, config)
    return spec


# ---------------------------------------------------------------------------
# The real production model SPEC — draft-2 §4's metric table
# (clarification 5, originally: taint_rate / stale_derivation_rate stay two
# separate metrics; the pending_marker_ratio merge was Lei's open call,
# non-blocking, at PR5 time. As of engram-alpha#2905 PR-A that call was
# DECIDED, and PR-B (round 2, this same commit) IS the merge: taint_rate and
# stale_derivation_rate are unified below into a single pending_premise_rate
# MetricBinding, backed by one L0 measurement (claim_bearing_pending_marker).
# PR-A only rebound their curve, interim-bound to "badness_pending_premise"
# pending this unification; see each binding's own #2905 comment below).
#
# UNVALIDATED at this point (round 3): constructing this ``ScoringModel``
# does NOT run ``validate_model`` — see that dataclass's own docstring for
# why. ``health/engine.py`` validates it for real via ``load(MODEL_SPEC,
# CONFIG)``, once its own ``CONFIG`` exists. Do not treat ``MODEL_SPEC``
# itself as "the real MODEL" anywhere — the real, validated model is
# ``health.engine.MODEL``.
# ---------------------------------------------------------------------------

_ASPECT_EPISTEMIC_INTEGRITY = AspectSpec(name="epistemic_integrity", points=40.0)
_ASPECT_PROVENANCE_INTEGRITY = AspectSpec(name="provenance_integrity", points=25.0)
_ASPECT_RETRIEVABILITY = AspectSpec(
    name="retrievability", points=35.0, reference_metric="embedding_coverage"
)

# JUDGMENT CALL (flagged in the PR5 report, load-bearing): importance values
# for the five non-reference admitted metrics. Draft-2 gives an exact number
# only for the reference metric (embedding_coverage: 1.0) — for the rest it
# says "unit of importance: relative, set reasonably for v1, calibrated from
# measured impact later" (Lei, 2026-09-02T14:46Z) and, separately, that
# recall_keywords_coverage should read "a little below" recall_summary_
# coverage IF importances are differentiated at all. This picks the SIMPLER
# v1 default: uniform importance=1.0 for every non-reference admitted
# metric, rather than inventing a specific "a little below" numeric ratio
# nobody specified — per-metric importance differentiation beyond what
# draft-2 states explicitly (the reference metric's 1.0) is future
# calibration work, not this PR's job.
_DEFAULT_IMPORTANCE = 1.0

MODEL_SPEC = ScoringModel(
    curves=CURVES,
    signals=SIGNALS,
    aspects=(
        _ASPECT_EPISTEMIC_INTEGRITY,
        _ASPECT_PROVENANCE_INTEGRITY,
        _ASPECT_RETRIEVABILITY,
    ),
    metrics=(
        # --- epistemic_integrity (40 points) --------------------------------
        # engram-alpha#2905 PR-B: unifies taint_rate + stale_derivation_rate
        # (PR-A's interim curve rebind onto badness_pending_premise) into
        # ONE signal, pending_premise_rate, backed by ONE L0 measurement
        # (claim_bearing_pending_marker -- a claim-bearing node with EITHER
        # a pending retract OR a pending supersede marker, deduped). Curve
        # stays badness_pending_premise (PR-A's rebind was already the
        # right shape for this). additional_detector "lottery:tainted-valid"
        # carries over from taint_rate's binding; stale_derivation_rate's
        # own additional_detector was already dropped by addendum 2 on
        # engram-alpha#1991 (Kepler via Borges's colleague read of draft 2,
        # 2026-09-03): "category 3 in the lottery is a different notion of
        # 'stale' than this metric measures" -- so there is no second
        # additional_detector to reconcile here.
        MetricBinding(
            signal="pending_premise_rate",
            aspect="epistemic_integrity",
            curve="badness_pending_premise",
            importance=_DEFAULT_IMPORTANCE,
            detector=DetectorRef(name="scan:cascade-frontier", population="claim_bearing_nodes"),
            additional_detector=DetectorRef(name="lottery:tainted-valid", population="claim_bearing_nodes"),
            repair="tool:cascade-resolve",
        ),
        # engram-alpha#2905 PR-A: importance _DEFAULT_IMPORTANCE -> 0.0
        # (HOLD-OUT, informational — Lei's 2026-09-13 ruling: this signal
        # measures a resolution HABIT, not what unresolved contradictions
        # DO to daily engram activity; the contradiction subsystem needs
        # dedicated design before a score depends on it). Curve stays
        # "badness_default" — deliberately NOT rebound. Detector/repair
        # unchanged.
        MetricBinding(
            signal="open_contradiction_rate",
            aspect="epistemic_integrity",
            curve="badness_default",
            importance=0.0,
            detector=DetectorRef(name="scan:open-contradictions", population="contradictions"),
            additional_detector=DetectorRef(name="lottery:contradictions", population="contradictions"),
            repair="tool:resolve",
        ),
        # --- provenance_integrity (25 points) -------------------------------
        # Both metrics ship importance=0.0 (diagnostic-only) per draft-2 §4:
        # "diagnostic until a repair exists" / "predicate to split before
        # scoring". See ScoringModel/AspectSpec docstrings above for the
        # significant consequence this has: this aspect currently has ZERO
        # admitted metrics.
        MetricBinding(
            signal="uncited_observation_rate",
            aspect="provenance_integrity",
            curve="badness_default",
            importance=0.0,
            detector=DetectorRef(name="reflect", population="observations"),
            repair=None,
        ),
        MetricBinding(
            signal="dangling_evidence_rate",
            aspect="provenance_integrity",
            curve="badness_default",
            importance=0.0,
            detector=None,
            repair=None,
        ),
        # --- retrievability (35 points) -------------------------------------
        # engram-alpha#2905 PR-A: curve rebind goodness_default ->
        # goodness_coverage (shared with recall_summary_coverage /
        # recall_keywords_coverage below, same knots, importance
        # differentiates). Importance stays 1.0 (the aspect's reference
        # metric, unchanged). Detector/repair unchanged.
        MetricBinding(
            signal="embedding_coverage",
            aspect="retrievability",
            curve="goodness_coverage",
            importance=1.0,  # the aspect's reference metric (draft-2 §1)
            detector=DetectorRef(name="scan:null-embeddings", population="claim_bearing_nodes"),
            repair="tool:embedding-backfill",
        ),
        # engram-alpha#2905 PR-A: curve rebind goodness_default ->
        # goodness_coverage; importance _DEFAULT_IMPORTANCE -> 0.5 (Kepler's
        # proposal, Lei-confirmed 2026-09-13: Tier-1 recall scent, weighted
        # below the reference metric embedding_coverage). Detector/repair
        # unchanged.
        MetricBinding(
            signal="recall_summary_coverage",
            aspect="retrievability",
            curve="goodness_coverage",
            importance=0.5,
            detector=DetectorRef(name="scan:summary-absent", population="claim_bearing_nodes"),
            repair="tool:batch-summary-fairy",
        ),
        # engram-alpha#2905 PR-A: curve rebind goodness_default ->
        # goodness_coverage; importance _DEFAULT_IMPORTANCE -> 0.25
        # (Kepler's proposal, Lei-confirmed 2026-09-13: Tier-2 recall scent,
        # and the tie-break for recall_summary_coverage/recall_keywords_
        # coverage's near-coincident missing sets — see the curves-session
        # doc's M4/M5 section). Detector/repair unchanged.
        MetricBinding(
            signal="recall_keywords_coverage",
            aspect="retrievability",
            curve="goodness_coverage",
            importance=0.25,
            detector=DetectorRef(name="scan:summary-absent", population="claim_bearing_nodes"),
            repair="tool:batch-summary-fairy",
        ),
        MetricBinding(
            signal="giant_component_fraction",
            aspect="retrievability",
            curve="goodness_default",
            importance=0.0,  # diagnostic until a scanner proposes edges
            detector=None,
            repair=None,
        ),
    ),
)

# Convenience lookup: signal name -> its MetricBinding. Built off MODEL_SPEC
# (not the validated MODEL, which lives in health/engine.py) — this index is
# a pure name->binding derivation, needs no cross-config validation, and is
# used by tests/ and health/engine.py alike purely to look up a binding by name.
# Every signal appears in exactly one binding today (validate_model's
# duplicate-pair check would catch a signal reused with a DIFFERENT aspect;
# a signal bound twice to the SAME aspect is caught by the same check too,
# since the pair is identical).
METRIC_BY_SIGNAL: dict[str, MetricBinding] = {m.signal: m for m in MODEL_SPEC.metrics}


# ---------------------------------------------------------------------------
# model_defs_jsonable / model_from_defs — engram-alpha#2575 PR C0
# ---------------------------------------------------------------------------


def model_defs_jsonable(model: ScoringModel) -> dict:
    """JSON-able declaration of a ``ScoringModel``'s aspect/metric BINDINGS
    (engram-alpha#2575 PR C0, design doc D4.1) — the model-layer twin of
    ``health.signals.signal_defs_jsonable`` / ``health.curves.
    curve_defs_jsonable``. Closes the two gaps ``viz/data/health_v2.py``'s
    own docstrings already name: "no field anywhere in the snapshot
    declares which curve a given SIGNAL uses", and "per-metric importance
    serialized into the snapshot, which isn't yet" (#2580). Without this
    blob a stored snapshot's SIGNALS can be re-derived as of its own
    ``health_version`` (via ``signal_defs``/``curve_defs``), but its SCORES
    cannot — D4.1's "versions must be executable, not merely recorded",
    one layer up.

    Emits LISTS, not dicts, so declaration ORDER is preserved: composition
    (``health.engine``'s ``build_snapshot``/``score_signals``) iterates
    ``model.aspects``/``model.metrics`` in declaration order (PR C0 round
    2: ``score_signals`` iterates the MODEL, not ``config.aspects`` — see
    that function's own docstring for why), and a consumer reconstructing a
    historical model from this blob must be able to reproduce the SAME
    iteration.

    A consumer reconstructs ``AspectSpec``/``MetricBinding`` (with
    ``DetectorRef`` where present) from THIS dict, and ``CurveSpec`` from a
    snapshot's own ``curve_defs`` blob, separately — see ``model_from_defs``
    below — NEVER from any callable: per Borges's closed-operator-set
    ruling, the scoring model ships as frozen DATA, and ``signal_fn``
    callables are inadmissible in this blob (there are none here to leak;
    ``ScoringModel.signals`` is not emitted at all).
    """
    return {
        "aspects": [
            {
                "name": aspect.name,
                "points": aspect.points,
                "reference_metric": aspect.reference_metric,
            }
            for aspect in model.aspects
        ],
        "metrics": [
            {
                "signal": metric.signal,
                "aspect": metric.aspect,
                "curve": metric.curve,
                "importance": metric.importance,
                "detector": (
                    {
                        "name": metric.detector.name,
                        "population": metric.detector.population,
                    }
                    if metric.detector is not None
                    else None
                ),
                "additional_detector": (
                    {
                        "name": metric.additional_detector.name,
                        "population": metric.additional_detector.population,
                    }
                    if metric.additional_detector is not None
                    else None
                ),
                "repair": metric.repair,
                "dive_target": metric.dive_target,
            }
            for metric in model.metrics
        ],
    }


def _detector_ref_from_dict(raw: dict | None) -> DetectorRef | None:
    """``{"name", "population"}`` -> ``DetectorRef``, or ``None`` -> ``None``
    — the one shared sub-shape ``model_from_defs`` reconstructs twice
    (``detector`` and ``additional_detector``). Raises ``KeyError`` on a
    malformed dict — see ``model_from_defs``'s own docstring for why this
    never fills a missing field with a default.
    """
    if raw is None:
        return None
    return DetectorRef(name=raw["name"], population=raw["population"])


def model_from_defs(model_defs: dict, curve_defs: dict) -> ScoringModel:
    """Reconstruct an UNVALIDATED ``ScoringModel`` from a snapshot's own
    ``model_defs``/``curve_defs`` blobs (data only) — engram-alpha#2575 PR
    C0, design doc D4.1's "recomputed ... through the declared ... model
    AS OF X" requirement: a consumer (PR C1's series deriver) rebuilds the
    historical model these two blobs describe, never HEAD's ``MODEL``.

    Builds, per ``curve_defs`` entry, ``CurveSpec(name, knots=tuple(tuple(k)
    for k in knots), monotone_direction, input_domain=tuple(...),
    output_range=tuple(...), observed_range=tuple(...) or None)``; and, per
    ``model_defs`` entry, ``AspectSpec`` / ``MetricBinding`` (with
    ``DetectorRef`` where present, via ``_detector_ref_from_dict`` above).
    ``signals={}`` on the returned model — the scoring functions
    (``_score_aspect``, ``compose_aspect_scores``, and this PR's
    ``health.engine.score_signals``) never read ``ScoringModel.signals``,
    only ``.curves``/``.aspects``/``.metrics``.

    Deliberately NOT passed through ``load()``/``validate_model`` —
    validation needs the live ``CONFIG``, and a historical version's own
    declarations are not HEAD's ``CONFIG`` to validate against (D4.1); a
    past version was valid against ITS OWN config when it was recorded, and
    re-validating it against a config that may have since changed would be
    validating the wrong thing.

    Raises ``KeyError``/``TypeError`` on a malformed blob — never fills a
    missing field with a HEAD default, so a version whose own declarations
    are broken fails loud instead of silently borrowing HEAD's shape.
    """
    curves = {
        name: CurveSpec(
            name=name,
            knots=tuple(tuple(k) for k in spec["knots"]),
            monotone_direction=spec["monotone_direction"],
            input_domain=tuple(spec["input_domain"]),
            output_range=tuple(spec["output_range"]),
            observed_range=(
                tuple(spec["observed_range"])
                if spec["observed_range"] is not None
                else None
            ),
        )
        for name, spec in curve_defs.items()
    }
    aspects = tuple(
        AspectSpec(
            name=a["name"],
            points=a["points"],
            reference_metric=a["reference_metric"],
        )
        for a in model_defs["aspects"]
    )
    metric_bindings = tuple(
        MetricBinding(
            signal=m["signal"],
            aspect=m["aspect"],
            curve=m["curve"],
            importance=m["importance"],
            detector=_detector_ref_from_dict(m["detector"]),
            additional_detector=_detector_ref_from_dict(m["additional_detector"]),
            repair=m["repair"],
            dive_target=m["dive_target"],
        )
        for m in model_defs["metrics"]
    )
    return ScoringModel(curves=curves, signals={}, aspects=aspects, metrics=metric_bindings)
