"""health_engine — the real production engine: CONFIG, the dream-side
scoring model, composition, and the snapshot writer for the health-score
redesign (engram-alpha#1991, PR2+PR3+PR5 of 5).

Design-of-record: engram-alpha#1991, issue comment 5372283060 (Borges,
2026-08-21, "Health-score redesign — DESIGN v2 (build basis)") §2 (aspects),
§3 (composition), §4 (snapshot), §5 (engine contract) — PR2/PR3's basis —
SUPERSEDED for the scoring/composition layer specifically by issue comment
5527066386 (Kepler, 2026-09-03, "The dream-side contract for health-v2 —
DRAFT 2"), §1 (the scoring model's shape) and §2 (the dream-side
projection), plus Kepler's five follow-up clarifications (recorded in
health_engine_model.py, inline at the field each one resolves).

Scope of this module (PR5 additions marked):
  1. The real production ``CONFIG`` (an ``EngineConfig``) — the RAW-SIGNAL-
     COMPUTATION layer (health_engine_config.py's denominator-population /
     empty-denominator-policy schema). UNCHANGED by PR5 — still declares all
     nine metrics across three aspects with the SAME ``signal_fn`` callables
     PR5's own health_engine_model.py's ``SIGNALS`` registry names. See "TWO
     PARALLEL SYSTEMS, ON PURPOSE" below for exactly what CONFIG still does
     and what it no longer does, post-PR5.
  2. [PR5] ``MODEL`` (below) — the SCORING layer (draft-2 §1): which aspect
     a signal belongs to, its curve, its importance, its detector/repair
     binding. Read raw signals via CONFIG (1 above), turns them into scores
     via THIS. [Round 3, engram-alpha#1991/#2520] Built here, right after
     CONFIG, via ``health_engine_model.load(health_engine_model.MODEL_SPEC,
     CONFIG)`` — not imported ready-made from health_engine_model.py
     anymore, so ``load()`` can cross-validate every detector's declared
     population against CONFIG's own real ``denominator_population``.
  3. [PR5, replaces PR2/PR3] ``compose_aspect_scores`` and per-aspect
     scoring — the weighted-ARITHMETIC-SUM formula (Kepler's clarification
     1, below), replacing BOTH ``health_engine_metrics.
     aspect_geometric_mean_scoring`` (per-aspect) and the OLD
     ``compose_aspect_scores`` (across aspects), which were both geometric-
     mean. Those two functions are UNCHANGED in health_engine_metrics.py
     (out of this PR's touch scope) but are now DEAD CODE from this
     module's perspective — nothing here calls them anymore; their own
     tests (tests/test_health_engine_metrics.py) still exercise them
     directly and stay green.
  4. The snapshot writer (design doc §4.1) — ``build_snapshot`` (pure
     computation) and ``write_snapshot`` (computation + best-effort JSON
     persistence under ``DATA_DIR/health-snapshots/``). PR3 populated the
     ``diagnostics`` block (activity metrics + cascade-marker age); PR5
     [NEW] also writes the aspect-blind DREAM PROJECTION (draft-2 §2) to a
     FIXED path, ``DATA_DIR/health-snapshots/health-snapshot-dream.json``,
     overwritten every checkpoint.

COMPOSITION FORMULA (Kepler's clarification 1, 2026-09-03, cited verbatim
per the PR5 spec's instruction to cite it in the docstring): "engram-
alpha#1991, issue comment thread, Lei's 2026-09-02T14:32Z ruling (four-level
shape: aspects with point allotments summing to 100, metrics carrying
in-aspect importance, arithmetic sums at both levels — 'the curve layer
carries low-value sensitivity')."

    aspect_score = Σ over AVAILABLE ADMITTED metrics in that aspect of
                   (importance_i / Σ importance_available) × curve_i(signal_i)
                   → range [0, 1]
    composed     = Σ over SCORED aspects (a) of
                   (points_a / points_in_play) × aspect_score_a × 100
                   → range [0, 100]

"Admitted" = ``importance > 0`` (today's ``scored=True`` is the same
predicate under a new name — health_engine_config.py's ``MetricDef.scored``
field is UNRELATED to this "admitted" predicate now; see "TWO PARALLEL
SYSTEMS" below). A non-admitted metric's signal/score still appears in the
snapshot as a diagnostic, never in either sum. "Available" (round 2 of
engram-alpha#2520) = the metric's raw signal is not itself UNAVAILABLE
THIS RUN. "Scored" aspect = it has at least one admitted-and-available
metric this run; "points_in_play" = Σ points_a over scored aspects only —
see the RESOLVED section below for the full "hold out, renormalize,
report" mechanism this formula implements at BOTH levels.

UNAVAILABLE PROPAGATION (RESOLVED — see below for the full history): draft-2
states no NEW rule for the new formula, so PR5's first round PRESERVED the
existing propagate-on-UNAVAILABLE behavior at face value. Round 2 (this
round) REPLACES flat propagation with the "hold out, renormalize" mechanism
below, applied at BOTH the metric level (within one aspect's own weighted
mean) and the aspect level (within the top-level composed sum) — design doc
§3's "degradation propagates, never silently composed over a hole" principle
is honored differently now: a hole is never silently composed OVER (its
allotment is excluded, not zeroed-and-averaged-in), but it also no longer
poisons the WHOLE computation the way flat propagation did.

***RESOLVED (was: LOAD-BEARING DISCOVERED CONSEQUENCE, flagged for design-
authority review) — design ruling: Kepler, engram-alpha#2520 issue comment,
2026-09-03, "Design ruling on the zero-admitted-aspect question" (co-design
authority per Lei's 2026-09-02 charge, Lei's veto standing), GENERALIZED one
level down by addendum 2 on engram-alpha#1991 (Kepler via Borges's colleague
read of draft 2, same date).*** Under the v1 metric table shipped by PR5
(health_engine_model.MODEL_SPEC, draft-2 §4, "ship all nine, unmerged" per
clarification 5), ``provenance_integrity`` (25 of 100 points) has BOTH its
metrics at importance 0 (diagnostic-only: no repair tool exists for either)
— zero admitted metrics, structurally, on every graph, until it gets at
least one admitted metric. PR5's first round discovered this made
``composed`` STRUCTURALLY, ALWAYS UNAVAILABLE under flat propagation, and
routed the question to design authority rather than resolving it
unilaterally (this codebase's own "loud guard, not silent workaround"
discipline).

Kepler named what the absence MEANS first, before picking an answer: zero
admitted metrics means "we cannot measure this aspect YET" — a true
condition about the MODEL, not a missing VALUE. Of three candidate answers:
(a) propagate UNAVAILABLE (PR5 round 1's behavior) turns a true condition
into a FATAL one — "loud != fatal is a lesson this fleet has paid for
repeatedly" (Kepler); (b) a neutral/perfect default FABRICATES a number for
an aspect nobody can measure — the "false-clean" shape; (c) admit a metric
anyway would require relaxing the admission rule or inventing a repair tool
that doesn't exist — the design doc's BRAKE, not a knob. Kepler's ruling is
a fourth answer, (d) "hold out, renormalize, report":

  1. An aspect with zero admitted metrics is EXCLUDED from composition. Its
     point allotment is HELD OUT, and ``composed`` is computed over the
     remaining (scored) aspects only, with THEIR allotments renormalized to
     sum to 100 — so ``composed`` stays a real number in [0,100], meaning
     "health over the aspects we can currently measure," never UNAVAILABLE
     purely because of this condition.
  2. The snapshot reports this loudly — three fields, on BOTH the main
     snapshot's ``scores`` block and the dream-side projection (JUDGMENT
     CALL, flagged in the PR5-round-2 report: extending to the main
     snapshot is not explicitly demanded by the ruling, which names only
     the projection — done for the same "stay internally consistent
     between the two files" reason PR5's own round-1 judgment call already
     established for the ``scores.metrics``/``scores.aspects`` scale
     change): ``unscored_aspects`` (the list of aspect names currently
     excluded), ``points_in_play`` (the sum of point-allotments actually
     used in the renormalized ``composed`` calculation, e.g. ``75.0`` when
     ``provenance_integrity``'s 25 points are held out of 100), and
     ``composed_coverage`` (``points_in_play / 100``, e.g. ``0.75``).
  3. ``health_engine_model.validate_model`` WARNS (never raises) at
     model-LOAD time (round 3: via ``health_engine_model.load()``, called
     once below, right after CONFIG — no longer at bare ``ScoringModel``
     construction time, see health_engine_model.py's own docstring for why)
     for any aspect with zero admitted metrics, naming the aspect(s) — see
     that function for the mechanism.
  4. Extensibility, true by construction: the moment a provenance metric
     gains a repair tool and its ``importance`` is raised above 0
     (admitted), that aspect automatically re-enters composition on the
     NEXT model construction — no code change beyond the model's own data.

ADDENDUM 2's GENERALIZATION (round 2 of engram-alpha#2520, 2026-09-03,
Kepler via Borges's colleague read of draft 2): the SAME hold-out-and-
renormalize mechanism applies ONE LEVEL DOWN too. An aspect's own
importance-weighted mean (``_score_aspect`` below) renormalizes over its
CURRENTLY AVAILABLE admitted metrics — a metric's raw signal can be
UNAVAILABLE on THIS graph/environment (e.g. the embedder disabled, or
``open_contradiction_rate``'s own empty-population UNAVAILABLE, round 2's
fold #2) even though it is structurally admitted (``importance > 0``) — not
just over its full DECLARED admitted-metric set. If ALL of an aspect's
admitted metrics are unavailable-right-now, that aspect is held out of
composition exactly as ruled above for the zero-admitted case — SAME
mechanism, different reason for reaching zero available metrics. This means
``_score_aspect`` returning UNAVAILABLE now has exactly TWO causes (zero
admitted, or zero CURRENTLY available), and BOTH resolve to "hold this
aspect out of the top-level sum" — there is no longer a THIRD case where an
aspect's own UNAVAILABLE score propagates to ``composed`` without being
held out (see ``compose_aspect_scores``/``_composition_coverage`` below).
Per-aspect ``metrics_in_play``/``coverage`` fields (main snapshot only, NOT
the aspect-blind dream projection — see ``build_snapshot``'s own docstring
for that scoping decision) report this one level down, mirroring the
top-level ``points_in_play``/``composed_coverage`` pair.

TWO PARALLEL SYSTEMS, ON PURPOSE (flagged plainly per the PR5 spec's
explicit instruction, not silently left as unexplained duplication):
health_engine_config.py's ``EngineConfig``/``AspectDef``/``MetricDef`` and
health_engine_model.py's ``ScoringModel``/``AspectSpec``/``MetricBinding``
are BOTH still declared, and BOTH still validate independently (each
requires its own weight/points to sum to 100 — currently the SAME numbers,
40/25/35, but nothing enforces they stay in sync beyond this docstring and
code review). ROUND 3 (engram-alpha#1991/#2520, Kepler's ruling, 2026-09-03)
adds the ONE mechanical exception to "nothing enforces": ``health_engine_
model.load()`` (called below, right after ``CONFIG``) cross-validates every
``MetricBinding``'s declared detector population against ``CONFIG``'s own
real ``denominator_population`` for that same-named metric — a genuine
cross-module check, not merely two independent declarations kept in sync by
review. Everything else about the two systems (weight/points-sum-to-100,
the rest of each schema) is still unenforced across the boundary. What each
still DOES, post-PR5:
  - ``health_engine_config.EngineConfig`` (``CONFIG`` below): declares the
    denominator population + empty-denominator policy for each metric
    (UNCHANGED, not superseded by draft-2, which is silent on denominators)
    and provides the ``signal_fn`` callable ``_run_aspect`` calls to compute
    a RAW signal. Its own weight-sum-to-100 / scored-implies-state_valued /
    dive_target checks all still run, unchanged.
  - ``health_engine_model.ScoringModel`` (``MODEL``): declares importance,
    curve, aspect membership, detector/repair for the SAME nine signals, and
    drives the actual ``aspect_score``/``composed`` arithmetic above.
  - ``AspectDef.scoring_fn`` (still set to ``metrics.
    aspect_geometric_mean_scoring`` below, unchanged) and ``MetricDef.
    scored``/``state_valued`` are now VESTIGIAL for real composition — kept
    ONLY because ``health_engine_config.py``'s own schema still requires
    them (a required dataclass field / a validated invariant) and because
    that module is out of this PR's touch scope (flagged, not deleted, per
    the PR5 spec's explicit "do NOT delete health_engine_config.py's
    validator/schema wholesale without flagging it first"). Nothing in this
    module calls ``aspect.scoring_fn`` anymore.

Explicitly OUT of scope (per the PR3 spec, still true): lambda-2 / algebraic
connectivity; DAG violations as a ``red_flags`` entry (``red_flags`` stays
``[]``); ``pre_sleep`` checkpoint wiring; ``engram_stats.py`` / the OLD
formula (never touched through the whole redesign). [PR5, per draft-2 §7
item 6]: the repair pass itself (dispatching fairies to run a ``repair``)
is explicitly NOT this PR's job — that is the dream-master/sleep-skill
change, a separate PR against the fairy and master specs, after PR5 lands
the fields it reads. No scanner or repair-tool logic is implemented
anywhere in this PR; the detector/repair registries
(health_engine_model.REGISTERED_DETECTORS/REGISTERED_REPAIRS) are NAME-ONLY
validation sets, never executables.

--------------------------------------------------------------------------
Historical judgment call (PR2) — "what to do about the third aspect (B) not
existing yet" — kept for context; B is real as of PR3, see above.
--------------------------------------------------------------------------
PR1's EngineConfig requires aspect weights to sum to 100 (``_check_weights_
sum``). With only A (40) and C (25) built, a real production CONFIG summing
to 65 would fail PR1's own validator — CORRECTLY (an incomplete config
should fail loud, not silently declare itself valid at the wrong total).
PR2 shipped a real CONFIG with an explicit "not yet real" stub AspectDef for
B (weight 35, one signal function that always returned UNAVAILABLE) rather
than deferring the whole CONFIG object — the config validated AND correctly
reported the WHOLE engine as degraded (via UNAVAILABLE propagation) until
this PR replaced the stub with real metrics.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import health_engine_metrics as metrics
import health_engine_model
from health_engine_config import (
    AspectDef,
    EngineConfig,
    MetricDef,
    UNAVAILABLE,
    _Unavailable,
)
from health_engine_model import ScoringModel

# ROUND 3 (engram-alpha#1991/#2520, Kepler's ruling, 2026-09-03): MODEL is no
# longer imported ready-made from health_engine_model.py — it is built BELOW,
# right after CONFIG, via ``health_engine_model.load(health_engine_model.
# MODEL_SPEC, CONFIG)``. This is the fix for round 2's row-local population
# check: health_engine_model.py can now cross-validate a detector's declared
# population against CONFIG's own real ``denominator_population`` for that
# metric, because construction (this module importing health_engine_model,
# a plain module import with no import-time side effects) and validation
# (via ``load()``, called only once CONFIG exists) are no longer forced to
# happen at the same time. `import health_engine_model` (the module, not a
# `from ... import MODEL`) is a genuinely ordinary top-of-file import now —
# there is no more circularity to route around by deferring it.

# NOTE on engram_core access: deliberately NOT `import engram_core as core` at
# module top level here — see health_engine_metrics.py's module docstring for
# why (the test-suite's fresh_server() module-clearing convention only sweeps
# "server"/"engram_"-prefixed modules; a module-top-level binding here would
# go stale across tests that reload engram_core without knowing to also clear
# this module). Every function below that needs `core` imports it locally.
# health_engine_metrics itself follows the same discipline internally, so
# caching `metrics` (the module object) above is safe regardless of import
# order — its own functions re-resolve engram_core lazily per call too.

SCHEMA_VERSION = "1"
# PR5 of 5: the scoring/composition layer is replaced wholesale (Kepler's
# draft-2, "the dream-side contract") — weighted-arithmetic-sum aspect_score
# and composed formulas, driven by MODEL (below), replace the PR2/PR3-era
# geometric-mean composition. The raw-signal layer (CONFIG,
# below) is unchanged. Bump when the CONFIG's aspect/metric shape OR the
# scoring model's shape changes in a way that would make an old stored
# snapshot's `config` block non-comparable to a fresh one.
ENGINE_VERSION = "0.4.0-pr5"


# ---------------------------------------------------------------------------
# Aspect A — Epistemic integrity (weight 40, design doc §2)
# ---------------------------------------------------------------------------

_STALE_DERIVATION_RATE = MetricDef(
    name="stale_derivation_rate",
    signal_fn=metrics.stale_derivation_rate,
    denominator_population="derivations",
    empty_denominator="unavailable",
    scored=True,
    state_valued=True,
)

_TAINT_RATE = MetricDef(
    name="taint_rate",
    signal_fn=metrics.taint_rate,
    denominator_population="claim_bearing_nodes",
    empty_denominator="unavailable",
    scored=True,
    state_valued=True,
)

_OPEN_CONTRADICTION_RATE = MetricDef(
    name="open_contradiction_rate",
    signal_fn=metrics.open_contradiction_rate,
    denominator_population="contradictions",
    # CHANGED from "clean" (engram-alpha#2520 round 2, fold #2, Kepler via
    # Borges, 2026-09-03) — the signal function itself now declines
    # (UNAVAILABLE) rather than reporting a fabricated "clean" 0.0 on zero
    # current contradiction nodes; this declared policy is updated to keep
    # it honest against the signal function's actual behavior (see
    # health_engine_metrics.open_contradiction_rate's docstring for the
    # full reasoning — this module's own docstring notes the declared
    # policy and the signal_fn's runtime behavior are correlated only by
    # construction/code-review, never independently checked, so leaving
    # this stale here would itself be exactly the kind of silent drift
    # that discipline warns about).
    empty_denominator="unavailable",
    scored=True,
    state_valued=True,
)

ASPECT_A_EPISTEMIC_INTEGRITY = AspectDef(
    name="epistemic_integrity",
    weight=40.0,
    metrics=(_STALE_DERIVATION_RATE, _TAINT_RATE, _OPEN_CONTRADICTION_RATE),
    scoring_fn=metrics.aspect_geometric_mean_scoring,
)


# ---------------------------------------------------------------------------
# Aspect C — Provenance integrity (weight 25, design doc §2)
# ---------------------------------------------------------------------------

_UNCITED_OBSERVATION_RATE = MetricDef(
    name="uncited_observation_rate",
    signal_fn=metrics.uncited_observation_rate,
    denominator_population="observations",
    empty_denominator="unavailable",
    scored=True,
    state_valued=True,
)

_DANGLING_EVIDENCE_RATE = MetricDef(
    name="dangling_evidence_rate",
    signal_fn=metrics.dangling_evidence_rate,
    denominator_population="evidence_nodes",
    empty_denominator="unavailable",
    scored=True,
    state_valued=True,
)

ASPECT_C_PROVENANCE_INTEGRITY = AspectDef(
    name="provenance_integrity",
    weight=25.0,
    metrics=(_UNCITED_OBSERVATION_RATE, _DANGLING_EVIDENCE_RATE),
    scoring_fn=metrics.aspect_geometric_mean_scoring,
)


# ---------------------------------------------------------------------------
# Aspect B — Retrievability (weight 35, design doc §2) — real as of PR3;
# polarity flipped to literal coverage in engram-alpha#2518.
# Denominator for all four is "claim_bearing_nodes" (same population aspect
# A's taint_rate already uses) — see health_engine_config.py's
# DECLARED_POPULATIONS comment and health_engine_metrics.py's aspect-B
# section docstring for why. Every raw signal below returns its OWN LITERAL
# coverage/fraction meaning (1.0 = fully covered/connected, 0.0 = none) —
# see health_engine_metrics.py's module docstring "SIGN CONVENTION" note;
# direction is declared on each metric's bound curve
# (health_engine_model.py's MODEL_SPEC), not encoded in the raw value's sign.
# ---------------------------------------------------------------------------

_RECALL_SUMMARY_COVERAGE = MetricDef(
    name="recall_summary_coverage",
    signal_fn=metrics.recall_summary_coverage,
    denominator_population="claim_bearing_nodes",
    empty_denominator="unavailable",
    scored=True,
    state_valued=True,
)

_RECALL_KEYWORDS_COVERAGE = MetricDef(
    name="recall_keywords_coverage",
    signal_fn=metrics.recall_keywords_coverage,
    denominator_population="claim_bearing_nodes",
    empty_denominator="unavailable",
    scored=True,
    state_valued=True,
)

_EMBEDDING_COVERAGE = MetricDef(
    name="embedding_coverage",
    signal_fn=metrics.embedding_coverage,
    denominator_population="claim_bearing_nodes",
    empty_denominator="unavailable",
    scored=True,
    state_valued=True,
)

_GIANT_COMPONENT_FRACTION = MetricDef(
    name="giant_component_fraction",
    signal_fn=metrics.giant_component_fraction,
    denominator_population="claim_bearing_nodes",
    empty_denominator="unavailable",
    scored=True,
    state_valued=True,
)

ASPECT_B_RETRIEVABILITY = AspectDef(
    name="retrievability",
    weight=35.0,
    metrics=(
        _RECALL_SUMMARY_COVERAGE,
        _RECALL_KEYWORDS_COVERAGE,
        _EMBEDDING_COVERAGE,
        _GIANT_COMPONENT_FRACTION,
    ),
    scoring_fn=metrics.aspect_geometric_mean_scoring,
)


# ---------------------------------------------------------------------------
# The real production config
# ---------------------------------------------------------------------------

CONFIG = EngineConfig(
    schema_version=SCHEMA_VERSION,
    engine_version=ENGINE_VERSION,
    aspects=(
        ASPECT_A_EPISTEMIC_INTEGRITY,
        ASPECT_C_PROVENANCE_INTEGRITY,
        ASPECT_B_RETRIEVABILITY,
    ),
)


# ---------------------------------------------------------------------------
# The real production model — round 3 (engram-alpha#1991/#2520, Kepler's
# ruling, 2026-09-03): built HERE, textually AFTER CONFIG, not imported
# ready-made from health_engine_model.py. This is the whole point of the
# restructure — health_engine_model.load() cross-validates every detector's
# declared population against CONFIG's own real denominator_population,
# which requires a real CONFIG to already exist. health_engine_model.py
# itself has no import-time side effects (see that module's own docstring);
# MODEL_SPEC there is an unvalidated ScoringModel, and load() is what turns
# it into this module's validated MODEL.
# ---------------------------------------------------------------------------

MODEL = health_engine_model.load(health_engine_model.MODEL_SPEC, CONFIG)


# ---------------------------------------------------------------------------
# Top-level composition — engram-alpha#1991 PR5, Kepler's draft-2 §1,
# clarification 1 ("engram-alpha#1991, issue comment thread, Lei's
# 2026-09-02T14:32Z ruling (four-level shape: aspects with point allotments
# summing to 100, metrics carrying in-aspect importance, arithmetic sums at
# both levels — 'the curve layer carries low-value sensitivity')"). REPLACES
# BOTH health_engine_metrics.aspect_geometric_mean_scoring (per-aspect) and
# this function's own former geometric-mean body (across aspects) — see this
# module's docstring's "COMPOSITION FORMULA" section for the full formula,
# the UNAVAILABLE-propagation rule, and the LOAD-BEARING DISCOVERED
# CONSEQUENCE for provenance_integrity under the shipped v1 metric table.
# Read that docstring section before changing anything below.
# ---------------------------------------------------------------------------


def _admitted_metrics_for_aspect(aspect_name: str, model: ScoringModel):
    """Every ``MetricBinding`` bound to ``aspect_name`` with ``importance >
    0`` ("admitted", clarification 1's predicate — the new name for today's
    ``scored=True``, see module docstring). STRUCTURAL only — does not look
    at any particular graph's raw signals; see ``_available_admitted_
    metrics_for_aspect`` below for the RUNTIME-availability-filtered
    sibling (round 2 of engram-alpha#2520, addendum 2's generalization)."""
    return [
        m for m in model.metrics
        if m.aspect == aspect_name and m.importance > 0
    ]


def _available_admitted_metrics_for_aspect(
    aspect_name: str,
    raw_by_metric: dict[str, float | _Unavailable],
    model: ScoringModel = MODEL,
):
    """Every admitted ``MetricBinding`` for ``aspect_name`` whose CURRENT
    raw signal is not itself UNAVAILABLE this run — the RUNTIME half of the
    hold-out mechanism (round 2 of engram-alpha#2520, addendum 2 on
    #1991, Kepler via Borges's colleague read of draft 2, 2026-09-03): a
    structurally-admitted metric (``importance > 0``) can still be
    unavailable on THIS graph/environment (e.g. the embedder disabled, or
    ``open_contradiction_rate``'s own empty-population UNAVAILABLE, round
    2's fold #2) — that metric is excluded from THIS aspect's weighted
    mean for THIS run, the same "hold out, renormalize" shape as the
    aspect-level mechanism one level up (``_composition_coverage`` below),
    applied one level down.
    """
    return [
        m for m in _admitted_metrics_for_aspect(aspect_name, model)
        if raw_by_metric[m.signal] is not UNAVAILABLE
    ]


def _aspect_metric_coverage(
    aspect_name: str,
    raw_by_metric: dict[str, float | _Unavailable],
    model: ScoringModel = MODEL,
) -> tuple[list[str], float]:
    """(metrics_in_play, coverage) for ONE aspect — the per-aspect sibling
    of ``_composition_coverage`` below, one level down. ``metrics_in_play``
    = signal names of admitted-and-available metrics used in THIS aspect's
    weighted mean this run (MODEL declaration order). ``coverage`` = the
    fraction of the aspect's OWN declared admitted importance that was
    available this run (``available_importance / total_admitted_
    importance``) — the aspect-level analog of the top-level
    ``composed_coverage``.

    Returns ``([], 0.0)`` for an aspect with ZERO admitted metrics declared
    at all — that aspect is reported via ``unscored_aspects``, not this
    pair; callers should only report ``metrics_in_play``/``coverage`` for a
    SCORED aspect (see ``build_snapshot``'s own scoping).
    """
    admitted = _admitted_metrics_for_aspect(aspect_name, model)
    if not admitted:
        return [], 0.0
    available = _available_admitted_metrics_for_aspect(aspect_name, raw_by_metric, model)
    total_importance = sum(m.importance for m in admitted)
    available_importance = sum(m.importance for m in available)
    coverage = available_importance / total_importance if total_importance else 0.0
    return [m.signal for m in available], coverage


def _score_aspect(
    aspect_name: str,
    raw_by_metric: dict[str, float | _Unavailable],
    model: ScoringModel = MODEL,
) -> float | _Unavailable:
    """One aspect's score: ``Σ (importance_i / Σ importance_available) ×
    curve_i(signal_i)`` over its CURRENTLY AVAILABLE admitted metrics only
    — range [0, 1].

    UNAVAILABLE if EITHER: (a) the aspect has ZERO admitted metrics
    DECLARED at all (direct analogy with the old ``aspect_geometric_mean_
    scoring``'s own ``if not values: return UNAVAILABLE`` — an aspect with
    nothing to sum over is UNAVAILABLE, not zero; see module docstring's
    "RESOLVED" section for ``provenance_integrity``'s case today), OR (b)
    every admitted metric's raw signal happens to be UNAVAILABLE THIS RUN
    (round 2 of engram-alpha#2520, addendum 2's generalization — SAME
    treatment, different reason for reaching zero available metrics). A
    PARTIAL availability (some but not all admitted metrics UNAVAILABLE
    this run) does NOT propagate to UNAVAILABLE — it RENORMALIZES the
    weighted mean over whichever admitted metrics ARE available this run
    (the "hold out, renormalize" mechanism, one level down from the
    aspect-level version in ``compose_aspect_scores``). A NON-admitted
    metric being UNAVAILABLE never affects this at all — it was never in
    the sum.
    """
    admitted = _admitted_metrics_for_aspect(aspect_name, model)
    if not admitted:
        return UNAVAILABLE

    available = _available_admitted_metrics_for_aspect(aspect_name, raw_by_metric, model)
    if not available:
        return UNAVAILABLE

    total_importance = sum(m.importance for m in available)
    aspect_score = 0.0
    for m in available:
        curve = model.curves[m.curve]
        aspect_score += (m.importance / total_importance) * curve.curve_fn(
            raw_by_metric[m.signal]
        )
    return aspect_score


def _composition_coverage(
    model: ScoringModel, aspect_scores: dict[str, float | _Unavailable]
) -> tuple[list[str], float]:
    """(unscored_aspects, points_in_play) — the "hold out" half of the
    design ruling (Kepler, engram-alpha#2520 review comment, 2026-09-03,
    "hold out, renormalize, report", generalized by addendum 2 on #1991,
    2026-09-03). An aspect is UNSCORED — held out of composition, its
    points excluded from the renormalization base — exactly when its OWN
    ``aspect_score`` (as returned by ``_score_aspect``) is UNAVAILABLE:
    today that happens for TWO reasons, both resolving to the SAME hold-out
    treatment — (a) STRUCTURAL: zero admitted metrics declared at all
    (``provenance_integrity`` under the v1 table), or (b) RUNTIME: every
    currently-admitted metric's raw signal is itself unavailable this run.
    This function does not need to distinguish (a) from (b) — both already
    collapse to ``aspect_scores[name] is UNAVAILABLE`` by construction in
    ``_score_aspect`` (see that function). Aspect declaration order
    (MODEL order, not alphabetical — JUDGMENT CALL, flagged in the PR5-
    round-2 report).
    """
    unscored = [
        aspect.name for aspect in model.aspects
        if aspect_scores.get(aspect.name) is UNAVAILABLE
    ]
    points_in_play = sum(
        aspect.points for aspect in model.aspects if aspect.name not in unscored
    )
    return unscored, points_in_play


def compose_aspect_scores(
    model: ScoringModel, aspect_scores: dict[str, float | _Unavailable]
) -> float | _Unavailable:
    """``composed = Σ over SCORED aspects (a) of (points_a / points_in_play)
    × aspect_score_a × 100`` — range [0, 100]. "Scored" excludes any aspect
    whose own score is UNAVAILABLE (held out of composition entirely — see
    ``_composition_coverage`` above and module docstring's "RESOLVED"
    section); ``points_in_play`` = Σ points_a over scored aspects only, the
    renormalization base — the excluded aspect's points never appear
    anywhere in this arithmetic.

    UNAVAILABLE only when EVERY aspect is held out (``points_in_play <=
    0``) — nothing anywhere to compose over (direct analogy with
    ``_score_aspect``'s own "nothing to sum over is UNAVAILABLE" rule, one
    level up). There is otherwise no remaining "propagate an individual
    aspect's UNAVAILABLE to the composed score" case as of round 2 of
    engram-alpha#2520 — ANY aspect-level UNAVAILABLE now means "hold this
    aspect out and renormalize the rest," never "poison the whole sum."

    JUDGMENT CALL (flagged in the PR5 report): signature changed from the
    old ``(config: EngineConfig, aspect_scores)`` to ``(model: ScoringModel,
    aspect_scores)`` — the function NAME is kept (the PR5 spec explicitly
    allows either; keeping the name minimizes caller churn since this
    function's only real callers are internal to this module and its own
    tests), but it now reads aspect ``points`` off the NEW scoring model,
    not the old ``EngineConfig``'s ``weight`` field — the new formula is
    points-driven, not weight-driven, even though the two currently carry
    the same numeric values (40/25/35).
    """
    unscored, points_in_play = _composition_coverage(model, aspect_scores)
    if points_in_play <= 0:
        return UNAVAILABLE

    composed = 0.0
    for aspect in model.aspects:
        if aspect.name in unscored:
            continue
        composed += (aspect.points / points_in_play) * aspect_scores[aspect.name] * 100.0
    return composed


# ---------------------------------------------------------------------------
# Engine execution — running signal + scoring functions against a real conn
# ---------------------------------------------------------------------------


def _run_aspect(
    conn: sqlite3.Connection, aspect: AspectDef, model: ScoringModel = MODEL
) -> tuple[dict[str, float | _Unavailable], float | _Unavailable]:
    """Run every metric in one aspect (raw signal via CONFIG's ``AspectDef``
    — the UNCHANGED raw-signal-computation layer, see module docstring);
    return (raw_by_metric, aspect_score), where aspect_score is now computed
    via ``_score_aspect`` against the NEW scoring model, NOT via ``aspect.
    scoring_fn`` (vestigial as of PR5 — see module docstring)."""
    raw_by_metric: dict[str, float | _Unavailable] = {}
    for metric in aspect.metrics:
        raw_by_metric[metric.name] = metric.signal_fn(conn)
    aspect_score = _score_aspect(aspect.name, raw_by_metric, model)
    return raw_by_metric, aspect_score


def _anchor_commit_sha() -> str | None:
    """The ``.engram`` git commit SHA (design doc §4.1/§4.3 "anchor").

    Precedent: engram_snapshot_worker.py's ``_commit_snapshot`` reads the
    post-commit HEAD sha the same way — ``core._git("rev-parse", "HEAD")``,
    run with ``cwd=core.DATA_DIR``. Returns None (never raises) if git is
    unavailable or DATA_DIR isn't a git repo — mirrors ``core._git``'s own
    never-raise contract.
    """
    import engram_core as core  # lazy — see module-top NOTE

    result = core._git("rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


# ---------------------------------------------------------------------------
# Snapshot serialization (design doc §4.1-4.3, contract item 3)
# ---------------------------------------------------------------------------


def _to_jsonable(value: float | _Unavailable):
    """UNAVAILABLE -> the string literal "UNAVAILABLE" (contract item 3:
    never coerce to a number — no ``-1``/``None`` sentinel). Real
    floats/ints pass through unchanged. This is PR2's serialization-format
    decision, made because the design doc leaves the concrete format open
    ("this will likely be written to a JSON file or similar eventually") —
    flagged in the PR2 report.
    """
    if value is UNAVAILABLE:
        return "UNAVAILABLE"
    return value


def _metric_def_to_dict(metric: MetricDef) -> dict:
    return {
        "name": metric.name,
        "denominator_population": metric.denominator_population,
        "empty_denominator": metric.empty_denominator,
        "scored": metric.scored,
        "state_valued": metric.state_valued,
        "dive_target": metric.dive_target,
        "signal_fn": f"{metric.signal_fn.__module__}.{metric.signal_fn.__qualname__}",
    }


def _aspect_def_to_dict(aspect: AspectDef) -> dict:
    return {
        "name": aspect.name,
        "weight": aspect.weight,
        "scoring_fn": f"{aspect.scoring_fn.__module__}.{aspect.scoring_fn.__qualname__}",
        "metrics": [_metric_def_to_dict(m) for m in aspect.metrics],
    }


def _config_to_dict(config: EngineConfig) -> dict:
    """The FULL declaration, inline (design doc §4.1) — every metric/aspect
    field a future re-score would need, plus importable dotted-path names
    for signal_fn/scoring_fn rather than the (non-JSON-serializable) Python
    callables themselves.
    """
    return {
        "schema_version": config.schema_version,
        "engine_version": config.engine_version,
        "aspects": [_aspect_def_to_dict(a) for a in config.aspects],
    }


# Human-readable per-metric definitions (design doc §4.1 "definitions" —
# "each metric's meaning, denominator, and why that denominator"). Aspect B's
# entry documents the stub, not a real metric — see module docstring.
#
# "_dive_target" (PR5, draft-2 §1's dive_target row / §7 item 3) is a
# deliberate NON-metric sibling key — the leading underscore signals "not a
# real metric name" the same way this codebase's other `_`-prefixed internal
# markers do (``_snapshot_path``, ``_config_to_dict``). It documents a
# SCHEMA FIELD (dive_target, emitted per-metric by _metric_def_to_dict
# above), not a metric, so a global note here beats duplicating the same
# text onto all nine metric entries (today none of them actually populate
# dive_target — see CONFIG above).
_METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "_dive_target": {
        "status": "deprecated, not deleted (engram-alpha#1991 draft-2 §1)",
        "note": (
            "dive_target is emitted per-metric by _metric_def_to_dict "
            "(health_engine.py, currently line 487) and read by NOTHING "
            "downstream — retiring it is a snapshot-schema change, which is "
            "not this PR's job. Kept on every MetricDef for a future schema "
            "bump (Borges's correction to draft 1's 'dead field': it is "
            "deprecated, not dead)."
        ),
    },
    "stale_derivation_rate": {
        "meaning": (
            "Fraction of current derivations carrying a stale marker "
            "(metadata.stale_by and/or metadata.stale_via non-empty) — "
            "built on a premise that has since been directly or "
            "cascade-superseded."
        ),
        "denominator": "total current derivations",
        "why_that_denominator": (
            "Scope is derivations only (stale-DERIVATION rate), not all "
            "node types, per design doc §2."
        ),
        "unit": (
            "nodes, not marker rows — a node can carry more than one "
            "pending marker row (see health_engine_metrics.py's "
            "stale_derivation_rate docstring, 'UNIT' note); the OLD formula "
            "shipped for months with the unit unstated, per draft-2 §4."
        ),
    },
    "taint_rate": {
        "meaning": (
            "Fraction of current claim-bearing nodes carrying a "
            "metadata.tainted_by marker (downstream of a retracted "
            "premise)."
        ),
        "denominator": (
            "total current claim-bearing nodes (engram_core."
            "CLAIM_BEARING_TYPES)"
        ),
        "why_that_denominator": (
            "Only claim-bearing nodes can be tainted in the epistemic "
            "sense; a definition/person/task/goal node is never a "
            "candidate."
        ),
        "unit": (
            "nodes, not marker rows — same convention as "
            "stale_derivation_rate (see that entry's 'unit' note)."
        ),
    },
    "open_contradiction_rate": {
        "meaning": (
            "Fraction of current contradiction nodes whose status is "
            "'active' or 'partially_resolved' (i.e. not resolved)."
        ),
        "denominator": "total current contradiction nodes",
        "why_that_denominator": (
            "UPDATED (engram-alpha#2520 round 2, fold #2, 2026-09-03): "
            "zero current contradiction nodes now reports UNAVAILABLE, "
            "not a 'clean' 0.0 — design doc §5 contract item 5's original "
            "worked example predates the 'hold out, renormalize, report' "
            "design ruling (Kepler, engram-alpha#2520 review comment, "
            "2026-09-03), which treats a metric with nothing to measure "
            "as unmeasured, not as evidence of health. Matches taint_rate "
            "/ stale_derivation_rate's existing empty-population "
            "treatment (same aspect)."
        ),
    },
    "uncited_observation_rate": {
        "meaning": (
            "Fraction of current observations (observation_factual / "
            "observation_predictive) with no evidence_id."
        ),
        "denominator": "total current observations of those two types",
        "why_that_denominator": "Only observation nodes carry evidence_id in the citation sense.",
    },
    "dangling_evidence_rate": {
        "meaning": "Fraction of current evidence nodes cited by NO current observation.",
        "denominator": "total current evidence nodes",
        "why_that_denominator": "Evidence with zero citing observations is orphaned provenance.",
    },
    "recall_summary_coverage": {
        "meaning": (
            "Fraction of current claim-bearing nodes WITH a "
            "recall_summary (literal coverage — 1=all covered, 0=none "
            "covered; see health_engine_metrics.py's module docstring "
            "'SIGN CONVENTION' note). Reads the recall_summary column's actual "
            "content directly rather than nodes.summary_missing: that flag "
            "is set only at node-creation time, and whether any given "
            "backfill route (e.g. the sleep-cycle batch-summary-fairy's "
            "engram_set_recall_summaries apply path) re-clears it on write "
            "is an implementation detail of that route, not a property this "
            "metric can rely on staying fixed across the codebase's "
            "lifetime. Reading the column's actual content is immune to "
            "that either way, and immune to future backfill routes that "
            "don't clear the flag — the direct read is the only version of "
            "this metric that can't go stale as write paths change."
        ),
        "denominator": (
            "total current claim-bearing nodes (engram_core."
            "CLAIM_BEARING_TYPES)"
        ),
        "why_that_denominator": (
            "Design doc §2's 'claim-bearing projection' framing — only "
            "claim-bearing content's retrievability matters for recall "
            "failures."
        ),
    },
    "recall_keywords_coverage": {
        "meaning": (
            "Fraction of current claim-bearing nodes with a non-empty "
            "recall_keywords list (literal coverage, same convention as "
            "recall_summary_coverage)."
        ),
        "denominator": (
            "total current claim-bearing nodes (engram_core."
            "CLAIM_BEARING_TYPES)"
        ),
        "why_that_denominator": "Same reasoning as recall_summary_coverage.",
    },
    "embedding_coverage": {
        "meaning": (
            "Fraction of current claim-bearing nodes with a NON-NULL embedding "
            "column (literal coverage, same convention). UNAVAILABLE — not a "
            "real coverage number — when the embedder is structurally "
            "unavailable in this install (ENGRAM_NO_EMBEDDINGS=1, "
            "sentence-transformers not installed, or embeddings disabled "
            "in config): that state is a CONFIGURATION fact, not a HEALTH "
            "fact, and scoring it as a coverage number would make the "
            "composed score swing on whether a Python package happens to "
            "be installed. Test fixtures run with ENGRAM_NO_EMBEDDINGS=1, "
            "under which every node's embedding is structurally NULL — a "
            "test proving the 'has coverage' branch must both populate "
            "the column directly AND patch embedder availability, or it "
            "will hit the UNAVAILABLE gate before ever reaching the "
            "per-node count."
        ),
        "denominator": (
            "total current claim-bearing nodes (engram_core."
            "CLAIM_BEARING_TYPES)"
        ),
        "why_that_denominator": "Same reasoning as recall_summary_coverage.",
    },
    "giant_component_fraction": {
        "meaning": (
            "Giant-component fraction (literal) of the "
            "induced subgraph on current claim-bearing nodes only (an "
            "edge counts only if BOTH endpoints are claim-bearing) — "
            "1=fully connected (best), 0=every claim-bearing "
            "node its own singleton component (worst)."
        ),
        "denominator": (
            "total current claim-bearing nodes (engram_core."
            "CLAIM_BEARING_TYPES)"
        ),
        "why_that_denominator": (
            "Design doc §2's explicit 'giant-component fraction on the "
            "claim-bearing projection' language."
        ),
    },
}


# ---------------------------------------------------------------------------
# Diagnostics (design doc §2 "Out of scoring, deliberately" — activity
# metrics + cascade-marker age; PR3's job). Informational only: computed
# directly into build_snapshot's returned "diagnostics" dict, never wired
# into compose_aspect_scores or any AspectDef/MetricDef — no UNAVAILABLE
# machinery needed here, a missing/degenerate value is just 0/null/empty,
# documented inline rather than modeled as a config-declared metric.
# ---------------------------------------------------------------------------

# Histogram bucket scheme for cascade-marker age (design doc §2 item 2: "age
# distribution (max, histogram) lives in diagnostics where it feeds fairy
# targeting"). JUDGMENT CALL (spec: "bucket scheme is your call; document
# your choice"): turn-equivalent units, buckets sized against the "1 turn ≈
# 1 day" approximation this codebase already documents elsewhere
# (engram_stats.py's own "Turn-unit → wall-clock approximation: 1 turn ≈
# 24h" comment) — roughly this-week / this-fortnight / this-month /
# this-quarter / older, the granularity a dream fairy triaging backlog age
# would actually want.
_CASCADE_MARKER_AGE_BUCKETS_TURNS: tuple[tuple[str, int, int | None], ...] = (
    ("0-7", 0, 7),
    ("8-14", 8, 14),
    ("15-30", 15, 30),
    ("31-60", 31, 60),
    ("61-120", 61, 120),
    ("121+", 121, None),
)


def _cascade_marker_age_diagnostics(conn: sqlite3.Connection, current_turn: int) -> dict:
    """Max age + histogram (in turn-equivalent units) of live cascade markers.

    "Cascade marker" = a ``cascade_markers`` row with ``status = 'pending'``
    — the SAME population Aspect A's stale_derivation_rate/taint_rate
    already score the BACKLOG SIZE of (design doc §2: "A keeps backlog SIZE
    via its three state-valued rates; age distribution ... lives in
    diagnostics"). Age of one marker = time since
    ``cascade_markers.created_at``, converted to turn-equivalent units via
    this codebase's own "1 turn ≈ 24h" approximation (engram_stats.py's
    documented convention; also used by ``_activity_diagnostics`` above) —
    whole days, floor-divided.

    CORRECTION (post-#1991-sec1, PR #2205 colleague review): this function
    originally read ``edit_history`` rows matching a fixed action-name tuple
    with no status filter — correct against the substrate PR3 was written
    against, since the cascade machine's own status column didn't exist
    yet. #1991 sec1 re-pointed Aspect A's rates at ``cascade_markers WHERE
    status = 'pending'``; this function kept reading the unfiltered
    edit_history population, so it silently diverged from what its own
    docstring claimed to measure — already-RESOLVED markers kept aging as
    if still live backlog (measured on one graph: 270 diagnostics rows
    against 14 scored nodes / 60 actually-pending marker rows, a 19.3x
    overcount, 181 resolved markers included). Re-pointed at the same
    ``cascade_markers WHERE status = 'pending'`` population Aspect A now
    scores, restoring the equivalence this docstring has always claimed.

    ``current_turn`` is accepted for call-site/signature stability with
    ``_compute_diagnostics``'s other diagnostic, but is no longer used —
    ``cascade_markers`` carries a wall-clock ``created_at``, not a turn
    number, and there is no reliable join back to one (no FK from
    cascade_markers to edit_history; a node/timing correlation exists for
    some write paths but is not guaranteed 1:1 across all of them).

    "Live": restricted to markers whose affected node
    (``cascade_markers.node_id``) is still a CURRENT node
    (``nodes.is_current = 1``) — unchanged reasoning from the original
    docstring, matching the same current-node scoping every Aspect A/B/C
    signal function already uses.
    """
    rows = conn.execute(
        """SELECT m.created_at AS created_at FROM cascade_markers m
           JOIN nodes n ON n.id = m.node_id
           WHERE m.status = 'pending' AND n.is_current = 1"""
    ).fetchall()

    now = datetime.now(timezone.utc)
    ages = []
    for r in rows:
        raw = r["created_at"]
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = max(int((now - created).total_seconds() // 86400), 0)
        ages.append(age_days)

    histogram = {label: 0 for label, _, _ in _CASCADE_MARKER_AGE_BUCKETS_TURNS}
    for age in ages:
        for label, lo, hi in _CASCADE_MARKER_AGE_BUCKETS_TURNS:
            if age >= lo and (hi is None or age <= hi):
                histogram[label] += 1
                break

    return {
        "count": len(ages),
        "max_age_turns": max(ages) if ages else 0,
        "histogram_turns": histogram,
    }


def _activity_diagnostics(conn: sqlite3.Connection) -> dict:
    """Question/prediction/conjecture/contradiction resolution rates + node
    birth rate + supersede volume (design doc §2's "Activity metrics" —
    "gaming the score and doing maintenance become the same act" is exactly
    why these are diagnostics, never scored).

    Resolution-rate shape adapted from engram_stats.py's ``_resolution_rate``
    helper (~line 771) and its four call sites — same query shape (COUNT by
    type, COUNT by type+status-in), reused here rather than reimplemented
    from scratch. Matches that helper's own scoping exactly: NOT restricted
    to ``is_current = 1`` (a resolved question/prediction/etc. is scored by
    its own ``status`` field, not by current-ness).

    Node birth rate / supersede volume: JUDGMENT CALL (flagged in the PR3
    report) — computed over a fixed 24h window, mirroring engram_stats.py's
    own documented "1 turn ≈ 24h" approximation (its window-filter code:
    "Turn-unit → wall-clock approximation: 1 turn ≈ 24h (dream cycle
    advances ~daily)"). The design doc names these as diagnostics without
    specifying a window; a raw lifetime total isn't a "rate" at all (no time
    denominator), so *some* window was required — 24h was chosen as the
    smallest one this codebase already treats as meaningful, matching the
    nap/pre_sleep/post_dream checkpoint cadence these diagnostics are
    computed at.
    """
    def _resolution_rate(node_type: str, resolved_statuses: tuple[str, ...]) -> dict:
        total = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE type = ?", (node_type,)
        ).fetchone()["c"]
        if total == 0:
            return {"total": 0, "resolved": 0, "rate": None}
        status_placeholders = ",".join("?" * len(resolved_statuses))
        resolved = conn.execute(
            f"SELECT COUNT(*) as c FROM nodes WHERE type = ? AND status IN ({status_placeholders})",
            (node_type, *resolved_statuses),
        ).fetchone()["c"]
        return {"total": total, "resolved": resolved, "rate": round(resolved / total, 3)}

    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    nodes_created_last_24h = conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE created_at >= ?", (cutoff_iso,)
    ).fetchone()["c"]
    supersede_edges_last_24h = conn.execute(
        "SELECT COUNT(*) as c FROM edges WHERE relation = 'supersedes' AND created_at >= ?",
        (cutoff_iso,),
    ).fetchone()["c"]

    return {
        "question_resolution": _resolution_rate(
            "question", ("resolved", "partially_resolved")
        ),
        "conjecture_resolution": _resolution_rate(
            "conjecture", ("resolved", "supported", "refuted")
        ),
        "prediction_resolution": _resolution_rate(
            "prediction",
            ("confirmed", "disconfirmed", "resolved", "partially_resolved"),
        ),
        "contradiction_resolution": _resolution_rate(
            "contradiction", ("resolved", "partially_resolved")
        ),
        "nodes_created_last_24h": nodes_created_last_24h,
        "supersede_edges_last_24h": supersede_edges_last_24h,
    }


def _compute_diagnostics(conn: sqlite3.Connection, current_turn: int) -> dict:
    """The full ``diagnostics`` block (design doc §4.1) — activity metrics +
    cascade-marker age. Never touches scoring; see module docstring.
    """
    return {
        "activity": _activity_diagnostics(conn),
        "cascade_marker_age": _cascade_marker_age_diagnostics(conn, current_turn),
    }


def build_snapshot(
    conn: sqlite3.Connection,
    checkpoint_mode: str,
    config: EngineConfig = CONFIG,
    model: ScoringModel = MODEL,
) -> dict:
    """Compute one health-score snapshot (design doc §4.1). Pure
    computation — persistence (writing it somewhere durable) is the
    caller's job; see ``write_snapshot`` below.

    ``checkpoint_mode`` must be one of "nap" / "pre_sleep" / "post_dream"
    (design doc §4.1).

    Per-metric ``scores.metrics[name]`` (PR5 change): now ``curve_fn(raw)``
    — range [0, 1] — via the metric's bound curve in ``model``, rather than
    the old ``metrics.metric_goodness(raw)`` (range [1, 100]). JUDGMENT CALL
    (flagged in the PR5 report): chosen so "score" means the SAME thing
    everywhere it appears in this PR — the dream projection's own
    ``score`` field (``_build_dream_projection`` below) is explicitly
    defined as "``score`` (through its curve, i.e. ``curve_fn(signal)``)"
    (draft-2 §2) — rather than inventing two different scaled meanings of
    the same word in two sibling artifacts. For the shared ``badness_
    default`` curve this is numerically consistent with the old value
    divided by 100 (the curve was built to reproduce ``metric_goodness``
    exactly, rescaled) — so this is a genuine but small, intentional scale
    change from PR2/PR3's [1,100] display, not a behavior regression.
    ``scores.aspects[name]`` similarly now reports the aspect_score literally
    as clarification 1's formula defines it — range [0, 1] — rather than a
    rescaled [0,100] display; see this module's docstring "COMPOSITION
    FORMULA" section. Only ``scores.composed`` stays on the familiar [0,100]
    scale (clarification 1's own formula multiplies by 100 there
    explicitly).

    [Round 2 of engram-alpha#2520] Three NEW top-level ``scores`` fields —
    ``unscored_aspects``, ``points_in_play``, ``composed_coverage`` — report
    the design ruling's "hold out, renormalize, report" mechanism (module
    docstring's "RESOLVED" section). PLACEMENT (JUDGMENT CALL, flagged in
    the PR5-round-2 report): inside ``scores``, alongside ``composed``,
    rather than as top-level snapshot siblings — these three fields are
    properties OF the composed calculation, so grouping them with it reads
    more directly than scattering them at the snapshot's top level. ALSO
    NEW: a ``scores.aspect_coverage`` dict, keyed by SCORED aspect name
    only (an unscored aspect has no "coverage of what it can't measure" to
    report — it's already named in ``unscored_aspects``), each entry
    ``{"metrics_in_play": [...], "coverage": ...}`` — the addendum-2
    generalization one level down (which admitted metrics were actually
    available this run, within one aspect's own weighted mean; see
    ``_aspect_metric_coverage``). Scoped to the MAIN snapshot only, NOT the
    aspect-blind dream projection (``_build_dream_projection`` below) —
    unlike the three top-level fields, which Kepler's original ruling
    explicitly named the projection as a required destination for,
    nothing asks for THIS aspect-keyed structure on the dream side, and
    adding it there would be a materially bigger P2 ("aspect-blind")
    violation than the three small global fields the ruling already
    carved out an explicit exception for.
    """
    if checkpoint_mode not in ("nap", "pre_sleep", "post_dream"):
        raise ValueError(
            "checkpoint_mode must be one of 'nap'/'pre_sleep'/'post_dream', "
            f"got {checkpoint_mode!r}"
        )

    import engram_core as core  # lazy — see module-top NOTE

    current_turn = core._get_current_turn()

    signals: dict[str, object] = {}
    scores_per_metric: dict[str, object] = {}
    scores_per_aspect: dict[str, object] = {}
    aspect_raw_scores: dict[str, float | _Unavailable] = {}
    aspect_raw_by_metric: dict[str, dict[str, float | _Unavailable]] = {}

    # Local (signal -> MetricBinding) lookup for THIS model instance —
    # deliberately not a cached attribute on ScoringModel (which is frozen
    # and shared across calls): build_snapshot may be called with a
    # non-default ``model`` (e.g. from tests), so this is recomputed per
    # call rather than trusting health_engine_model.METRIC_BY_SIGNAL, which
    # is only valid for the default MODEL instance.
    metric_by_signal = {m.signal: m for m in model.metrics}

    for aspect in config.aspects:
        raw_by_metric, aspect_score = _run_aspect(conn, aspect, model)
        for metric_name, raw in raw_by_metric.items():
            signals[metric_name] = _to_jsonable(raw)
            if raw is UNAVAILABLE:
                scores_per_metric[metric_name] = "UNAVAILABLE"
            else:
                binding = metric_by_signal[metric_name]
                curve = model.curves[binding.curve]
                scores_per_metric[metric_name] = curve.curve_fn(raw)
        scores_per_aspect[aspect.name] = _to_jsonable(aspect_score)
        aspect_raw_scores[aspect.name] = aspect_score
        aspect_raw_by_metric[aspect.name] = raw_by_metric

    composed = compose_aspect_scores(model, aspect_raw_scores)
    unscored_aspects, points_in_play = _composition_coverage(model, aspect_raw_scores)
    composed_coverage = points_in_play / 100.0

    # Per-aspect metrics_in_play/coverage (addendum 2's one-level-down
    # generalization) — SCORED aspects only, see build_snapshot's own
    # docstring for why an unscored aspect has no entry here.
    aspect_coverage: dict[str, dict[str, object]] = {}
    for aspect in model.aspects:
        if aspect.name in unscored_aspects:
            continue
        metrics_in_play, coverage = _aspect_metric_coverage(
            aspect.name, aspect_raw_by_metric[aspect.name], model
        )
        aspect_coverage[aspect.name] = {
            "metrics_in_play": metrics_in_play,
            "coverage": coverage,
        }

    return {
        "schema_version": config.schema_version,
        "engine_version": config.engine_version,
        "config": _config_to_dict(config),
        "definitions": _METRIC_DEFINITIONS,
        "signals": signals,
        "scores": {
            "metrics": scores_per_metric,
            "aspects": scores_per_aspect,
            "composed": _to_jsonable(composed),
            "unscored_aspects": unscored_aspects,
            "points_in_play": points_in_play,
            "composed_coverage": composed_coverage,
            "aspect_coverage": aspect_coverage,
        },
        # DAG violations as a red_flags entry are explicitly OUT of scope for
        # PR3 too (see module docstring) — left exactly as PR2 shipped it.
        "red_flags": [],
        "diagnostics": _compute_diagnostics(conn, current_turn),
        "anchor": _anchor_commit_sha(),
        "turn": current_turn,
        "checkpoint_mode": checkpoint_mode,
    }


def _snapshot_dir() -> Path:
    import engram_core as core  # lazy — see module-top NOTE

    d = core.DATA_DIR / "health-snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Dream-side projection — engram-alpha#1991 PR5, draft-2 §2. This is the
# MECHANISM enforcing P2 ("aspect-blind" — draft-2 §0): a SEPARATE artifact
# containing ONLY per-metric fields, never aspects/points/importances/the
# composite. The dream-master prompt and fairy templates receive THIS
# file's path, never the full snapshot's (draft-2 §2, last sentence) — that
# wiring itself is explicitly NOT this PR's job (draft-2 §7 item 6).
# ---------------------------------------------------------------------------


def _dream_projection_path() -> Path:
    """FIXED filename, overwritten every checkpoint (JUDGMENT CALL, flagged
    in the PR5 report: draft-2 §2 says "the checkpoint also writes
    health-snapshot-dream.json" as a singular artifact, not "a" projection
    per timestamp like the main snapshot — read literally as one always-
    current path, which is also what "the dream-master prompt and fairy
    templates receive the projection's path" implies: a STABLE path to wire
    once, not a moving target)."""
    return _snapshot_dir() / "health-snapshot-dream.json"


def _dream_projection_error_path() -> Path:
    """Sibling sentinel to ``_dream_projection_path()`` (engram-alpha#2519
    Ruling 2, design authority, 2026-09-03): same directory, same base name,
    ``.error`` instead of ``.json``. ``write_snapshot`` below writes this
    file on a caught dream-projection exception, and DELETES it on the next
    SUCCESSFUL dream-projection write. Its presence is what lets a reader
    (the dream-master's own precondition check, "The repair pass" section of
    ``engram-dream-master.md``) distinguish "the writer FAILED tonight" from
    "the writer never ran at all" — a stale-or-absent
    ``health-snapshot-dream.json`` alone cannot tell those two states apart,
    which is exactly the gap Ruling 2 named: "a forward-compatible OR-branch
    that reads a file nothing writes is dead surface." Before this round,
    nothing wrote this file; this function's caller is the producer.
    """
    return _dream_projection_path().with_suffix(".error")


def dream_projection_error_sentinel_is_stale(sentinel: dict, current_turn: int) -> bool:
    """Whether a read ``.error`` sentinel (the JSON content at
    ``_dream_projection_error_path()``) is STALE relative to ``current_turn``
    — i.e. its own ``turn`` field does not match tonight's actual turn,
    meaning it is a leftover from an earlier cycle's failure (the
    unlink-after-success step in ``write_snapshot`` failed) rather than
    tonight's own failure. A stale sentinel must NOT block tonight's repair
    pass (engram-alpha#2525) — the dream-master's precondition check (see
    "The repair pass" → "Precondition" in ``engram-dream-master.md``) uses
    this to distinguish the two cases before deciding whether to skip.
    """
    return sentinel.get("turn") != current_turn


def _read_previous_dream_projection() -> dict | None:
    """Best-effort read of whatever projection the PREVIOUS checkpoint left
    behind — ``None`` if it doesn't exist yet (first-ever write) or fails to
    parse (never raises; a corrupt/missing previous projection degrades
    ``delta_since_last`` to ``None``, it does not break this checkpoint)."""
    path = _dream_projection_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _detector_ref_to_jsonable(detector) -> dict | None:
    """``health_engine_model.DetectorRef`` (addendum 3, engram-alpha#1991,
    2026-09-03) is a plain dataclass, not natively JSON-serializable —
    ``None`` passes through; otherwise ``{"name": ..., "population": ...}``.
    Used ONLY for the dream projection's per-metric ``detector`` field
    below (the MAIN snapshot never serializes a ``MetricBinding``/
    ``DetectorRef`` at all — ``_config_to_dict`` walks
    ``health_engine_config``'s OWN, unrelated ``MetricDef``/``AspectDef``
    schema, which has no detector field)."""
    if detector is None:
        return None
    return {"name": detector.name, "population": detector.population}


def _build_dream_projection(snapshot: dict, model: ScoringModel = MODEL) -> dict:
    """The aspect-blind per-metric projection (draft-2 §2): for EVERY metric
    binding in ``model`` (admitted or diagnostic-only — draft-2's own table
    includes both, differentiated by the ``admitted`` field) — ``signal``
    (raw value), ``score`` (``curve_fn(signal)``), ``admitted``, ``detector``,
    ``repair``, ``delta_since_last``. Deliberately NO aspects, NO points/
    importances, NO composite anywhere in this dict — see module docstring
    §6 item 1 (draft-2's forbidden list): "Reading scores.aspects, scores.
    composed, allotments or importances anywhere on the dream side" is
    explicitly forbidden, and P2 is enforced here by CONSTRUCTION (these
    keys are never written), not by convention.

    [Round 2 of engram-alpha#2520] Also carries ``unscored_aspects``/
    ``points_in_play``/``composed_coverage`` at the TOP LEVEL, read straight
    off ``snapshot["scores"]`` — single source of truth, computed once in
    ``build_snapshot``, never re-derived here. Kepler's design ruling
    explicitly names the projection as a REQUIRED destination for these
    three fields (module docstring's "RESOLVED" section, item 2) even
    though they name aspects / a point-based coverage fraction — in
    TENSION with P2's "aspect-blind" rule for everything else in this
    function (JUDGMENT CALL, flagged in the PR5-round-2 report: P2 is read
    as "no aspects/points/importances/composite EXCEPT the three fields
    this ruling explicitly carves out", not as an unconditional absolute).
    The addendum-2 generalization's per-aspect ``aspect_coverage``/
    ``metrics_in_play`` (build_snapshot's own new fields) are DELIBERATELY
    NOT added here — that structure is aspect-KEYED, a materially bigger
    P2 violation than three small global values, and nothing in the
    addendum asks for it on the dream side; see ``build_snapshot``'s
    docstring for the same scoping decision stated there.
    """
    previous = _read_previous_dream_projection()
    previous_metrics = (previous or {}).get("metrics", {})

    metrics_out: dict[str, dict] = {}
    for binding in model.metrics:
        raw = snapshot["signals"][binding.signal]
        if raw == "UNAVAILABLE":
            score: object = "UNAVAILABLE"
        else:
            curve = model.curves[binding.curve]
            score = curve.curve_fn(raw)

        prev_entry = previous_metrics.get(binding.signal)
        prev_score = prev_entry.get("score") if isinstance(prev_entry, dict) else None
        if isinstance(prev_score, (int, float)) and isinstance(score, (int, float)):
            # delta_since_last: diagnostic; rendered for the morning read;
            # NEVER a score, NEVER a lottery input, NEVER an
            # importance-tuning input (draft-2 §2's own prohibition text,
            # carried verbatim here so whoever meets this field first meets
            # the rule with it — see also module docstring §6 item 5).
            delta = score - prev_score
        else:
            delta = None

        metrics_out[binding.signal] = {
            "signal": raw,
            "score": score,
            "admitted": binding.importance > 0,
            "detector": _detector_ref_to_jsonable(binding.detector),
            "repair": binding.repair,
            "delta_since_last": delta,
        }

    return {
        "turn": snapshot["turn"],
        "checkpoint_mode": snapshot["checkpoint_mode"],
        "engine_version": snapshot["engine_version"],
        "unscored_aspects": snapshot["scores"]["unscored_aspects"],
        "points_in_play": snapshot["scores"]["points_in_play"],
        "composed_coverage": snapshot["scores"]["composed_coverage"],
        "metrics": metrics_out,
    }


def write_snapshot(
    conn: sqlite3.Connection,
    checkpoint_mode: str,
    config: EngineConfig = CONFIG,
    model: ScoringModel = MODEL,
) -> dict:
    """Compute + persist a snapshot as JSON under
    ``DATA_DIR/health-snapshots/<turn>-<checkpoint_mode>-<timestamp>.json``,
    and [PR5] the aspect-blind dream-side projection at the FIXED path
    ``DATA_DIR/health-snapshots/health-snapshot-dream.json`` (draft-2 §2).

    JUDGMENT CALL (flagged in the PR2 report): the design doc calls these
    "versioned snapshots as SSoT" and says several consumers will render
    them (later PRs), which implies real persistence, not a computed-then-
    discarded dict — but the PR2 spec explicitly scopes this PR to "produce
    the in-memory snapshot structure correctly" and leaves the concrete
    format/location open. This picks a location consistent with the rest of
    ~/.engram's on-disk layout. It is deliberately NOT added to
    engram_snapshot_worker.py's ``_COMMIT_FILES`` git-commit whitelist in
    this PR — that would be an async-snapshot-worker behavior change beyond
    "wire the checkpoint calls", and is flagged for the parent to decide
    when a real consumer (PR3/PR4) needs snapshots to be git-versioned
    alongside knowledge.sql.

    Best-effort at the PERSISTENCE step only (mirrors engram_core.
    _commit_snapshot's best-effort posture — a health-snapshot write failure
    must never break the underlying nap/advance_turn checkpoint tool it's
    piggybacked onto): a disk-write failure is caught and reported via the
    snapshot dict's ``_snapshot_write_error`` key rather than raised.
    Computation itself (``build_snapshot``) is allowed to raise — a bug in a
    signal function should be loud, not silently swallowed into an empty
    snapshot.

    JUDGMENT CALL (flagged in the PR5 report, broader than the above): the
    dream-projection step below (``_build_dream_projection`` COMPUTATION,
    not just its write) is wrapped in a broad ``except Exception`` — wider
    than the main snapshot's write-only best-effort scope. This is a
    deliberate, more conservative choice for the NEW, less-proven P2
    mechanism specifically: a bug in the new projection-building logic must
    never be able to break the primary snapshot / the nap / advance_turn
    checkpoint tool it's piggybacked onto, which is the dominant safety
    property this whole subsystem is built around. A stricter reading of
    "best-effort is for persistence, not computation" would let a
    projection-computation bug raise here — exactly like a genuine bug in a
    main-snapshot signal function is allowed to raise — but the asymmetry
    (main snapshot: computation raises; dream projection: nothing raises)
    is intentional, not an oversight.

    [Round 2, engram-alpha#2519 Ruling 2] On a caught dream-projection
    exception (computation OR write), also writes the
    ``.error`` sentinel (``_dream_projection_error_path()``) — a sibling
    JSON file ``{"turn": ..., "written_at": ..., "error": ...}`` — so a
    reader of a stale/absent ``health-snapshot-dream.json`` can tell
    "the writer failed tonight" from "the writer never ran." On a
    SUCCESSFUL dream-projection write, that sentinel is deleted
    (``Path.unlink(missing_ok=True)``) if present. The sentinel write/delete
    step is ITSELF best-effort (wrapped in its own inner ``except OSError:
    pass``) — a failure to write or delete the sentinel must never raise
    here either, matching this whole function's best-effort posture.
    """
    snapshot = build_snapshot(conn, checkpoint_mode, config, model)
    turn = snapshot["turn"]
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    filename = f"{turn}-{checkpoint_mode}-{ts}.json"
    path = _snapshot_dir() / filename
    try:
        path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        snapshot["_snapshot_path"] = str(path)
    except OSError as e:
        snapshot["_snapshot_write_error"] = str(e)

    try:
        projection = _build_dream_projection(snapshot, model)
        dream_path = _dream_projection_path()
        dream_path.write_text(
            json.dumps(projection, indent=2, sort_keys=True), encoding="utf-8"
        )
        snapshot["_dream_projection_path"] = str(dream_path)
        try:
            _dream_projection_error_path().unlink(missing_ok=True)
        except OSError:
            pass  # sentinel cleanup is itself best-effort — see docstring
    except Exception as e:  # noqa: BLE001 — see docstring's JUDGMENT CALL
        snapshot["_dream_projection_write_error"] = str(e)
        try:
            _dream_projection_error_path().write_text(
                json.dumps(
                    {
                        "turn": turn,
                        "written_at": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "error": str(e),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # sentinel write is itself best-effort — see docstring

    return snapshot


# ---------------------------------------------------------------------------
# Repair-pass provenance — engram-alpha#2519 (the health-v2 epic, #1991's
# follow-up PR). This module's own "Explicitly OUT of scope" note above (and
# PR5's own §7 item 6) named the repair pass itself as deliberately NOT this
# module's job: "the repair pass itself (dispatching fairies to run a
# `repair`) is ... the dream-master/sleep-skill change, a separate PR
# against the fairy and master specs, after PR5 lands the fields it reads."
# #2519 IS that follow-up — design-authority ruling (Kepler, after review):
# the repair pass dispatches nothing and runs no new fairies. It is a
# provenance-writing driver loop, owned by the dream-master AGENT (an LLM
# sub-agent following src/engram/agents/claude/engram-dream-master.md's new
# "# The repair pass" section), over work that same cycle's cascade sweep /
# bucket walk / recall-summary cohort already did, plus exactly one direct
# action (the embedding-backfill scan + tool call).
#
# This section holds the two pieces of #2519 that belong in CODE rather than
# prose, so both sides of the eventual two-reader relationship (the dream-
# master's write path here; a future panel PR, #2518, that will READ
# `health-repair-record.json`) check against the SAME declared shape instead
# of two hand-copied lists of field names that could silently drift apart —
# design-authority instruction: "declared once in the engine next to the
# projection writer, importable by both the writer and the panel that will
# read it — a key-set mismatch must render as an explicit UNKNOWN state to
# the panel, never a silently empty/clean-looking one."
#
#   1. The repair-pass provenance record's SCHEMA (field names + valid
#      status values) + ``validate_repair_record`` — the schema-conformance
#      check, mirroring ``health_engine_config.validate_config`` /
#      ``health_engine_model.validate_model``'s "collect every violation
#      into one pass, don't just raise on the first" contract (see those
#      functions' docstrings; read one before changing this one).
#   2. ``_count_null_embeddings`` — the ONE genuinely new read-only scanner
#      the repair pass needs (``embedding_coverage``'s detector,
#      ``scan:null-embeddings``). Every other repair kind is a RE-READ of
#      work this same dream cycle already did elsewhere (the cascade sweep,
#      the bucket walk's resolutions bucket, the parent's Step 7.5
#      recall-summary cohort) — see the dream-master's own new section for
#      the full per-metric dispatch logic. Nothing else here is executable:
#      the record itself is produced and WRITTEN by the dream-master agent,
#      not by this module.
# ---------------------------------------------------------------------------

# READER'S TURN-CHECK RULE (round 2, engram-alpha#2519, design-authority
# fold-in, 2026-09-03 — moved here from the dream-master `.md` prose so a
# future importer of these constants gets the interpretation rule bundled
# with the field names, not only the names): a reader of this record — the
# dream-master's own precondition step on a later night, or a future #2518
# panel — MUST treat a record whose ``turn`` field does not match the
# CURRENT graph turn as stale/unknown, never as tonight's real health state.
# This schema carries no separate "freshness" flag; ``turn`` IS the
# freshness signal, and it means nothing without a caller comparing it
# against the graph's own current turn at read time.
REPAIR_RECORD_TOP_LEVEL_FIELDS: tuple[str, ...] = (
    "turn", "written_at", "status", "reason", "metrics",
)

# The WHOLE-PASS-level outcome (round 2 fold-in, engram-alpha#2519, design
# authority, 2026-09-03 — corrects round 1's "skip night: write nothing,
# leave whatever's on disk untouched" behavior, which left a stale record
# looking current): "ran" = the driver loop actually executed tonight;
# "skipped" = the WHOLE pass was skipped tonight per the dream-master's own
# precondition check (stale/absent projection, or the ``.error`` sentinel —
# see ``_dream_projection_error_path``/``write_snapshot`` below). This is a
# DIFFERENT, coarser-grained enum than ``REPAIR_RECORD_STATUS_VALUES`` below,
# which describes each METRIC's own per-entry outcome, not the whole pass.
# A "skipped" top-level status always carries a non-null top-level
# ``reason`` naming which precondition fired, and every metric STILL gets an
# entry (status "skipped", findings_count/repairs_dispatched null) — no
# metric is ever silently absent, on a skip night or otherwise (Kepler's
# ruling: "every metric in the shipped model gets an entry, always").
REPAIR_RECORD_RUN_STATUS_VALUES: frozenset[str] = frozenset({"ran", "skipped"})

# JUDGMENT CALL (flagged in the #2519 report): ``detector``/``population``
# are FLAT scalar fields (the detector's bare name string + its population
# string), not a nested ``{"name": ..., "population": ...}`` dict the way
# the dream projection's own per-metric ``detector`` field is shaped
# (``_detector_ref_to_jsonable`` above). The #2519 spec's own field list
# reads as six independent scalar-shaped fields; flattening here avoids
# forcing every reader (including a future #2518 panel) to unwrap a nested
# dict for a value that is only ever one string, and keeps this record's
# shape visibly DIFFERENT from the dream projection's own per-metric shape
# — a deliberate signal that this is a different artifact serving a
# different purpose, not a re-export of the projection under a new name.
#
# ``reason`` (round 2, engram-alpha#2519, Kepler's ruling + design-authority
# fold-in, 2026-09-03) is a ``str | None`` sibling to ``status``, added so an
# ``"unavailable"``-status entry can say WHICH of its two possible causes
# fired — round 1 gave both causes the same status and no way to tell them
# apart. Populated with one of:
#   - ``"no_repair_binding"`` — no registered repair exists for this metric
#     at all (diagnostic-only; the metric may or may not be admitted).
#   - ``"signal_unavailable_this_run"`` — a real repair mapping exists, but
#     tonight's projection reports this metric's raw signal as
#     ``"UNAVAILABLE"``.
#   - on a whole-pass-skip night (status ``"skipped"``): the same reason
#     string as the top-level ``reason`` field (naming which precondition
#     fired) — JUDGMENT CALL: mirrored rather than left ``null``, so a
#     panel reading one metric entry in isolation still knows why, without
#     also having to read the top-level fields.
# ``None`` otherwise — in particular, JUDGMENT CALL (flagged in the #2519
# report): left ``null`` for the four repair-kind branches' own "done" /
# "carried" outcomes below ([round 3, engram-alpha#2519, Kepler's ruling]
# this now includes the embedding-backfill's own zero-findings "done" case,
# which round 2 used to report as "skipped" here — see
# ``REPAIR_RECORD_STATUS_VALUES`` below for why that was narrowed), rather
# than inventing a distinct reason string per branch (e.g. a per-branch
# ``"embedding_backfill_tool_failed"``) — Kepler's ruling explicitly leaves
# that extension optional ("your call"), and the existing prose right next
# to each branch (the dream-master `.md`'s own per-metric dispatch logic)
# already names the specific cause in narrative form.
REPAIR_RECORD_METRIC_FIELDS: tuple[str, ...] = (
    "detector",
    "population",
    "findings_count",
    "repairs_dispatched",
    "delta_since_last",
    "status",
    "reason",
)

# The four outcomes a per-metric repair-pass entry can land in (dream-master
# spec, "The repair pass" section):
#   - "done" — the underlying work this cycle already did fully addressed
#     tonight's findings. [Round 3, engram-alpha#2519, Kepler's ruling] This
#     INCLUDES the embedding-backfill's own zero-findings case: the scan ran
#     and found nothing, so the metric WAS fully examined tonight even
#     though the repair tool itself was never dispatched (round 2 used to
#     report this same case as "skipped" — see below for why that changed).
#   - "carried" — a real repair mapping exists but tonight's underlying work
#     was incomplete: a cascade sweep that hit its pass cap, a bucket-walk
#     item skipped for snapshot divergence, an embedding-backfill tool-call
#     failure, or a batch-summary cohort that left a materially non-zero
#     residual.
#   - "unavailable" (round 2: now TWO distinguishable causes, both resolving
#     to this same status — see ``reason`` above) — no registered repair
#     exists for this metric at all, OR a real repair mapping exists but
#     tonight's signal is itself UNAVAILABLE.
#   - "skipped" — [round 3, engram-alpha#2519, Kepler's ruling: "so `skipped`
#     means exactly one thing"] ONLY the whole-pass skip-night case, see
#     ``REPAIR_RECORD_RUN_STATUS_VALUES`` above: this metric was NOT examined
#     tonight at all (the whole repair pass was skipped per the dream-
#     master's own precondition check). Round 2's wording for this member
#     also covered "a real repair mapping exists but there was nothing to do
#     this run, e.g. zero null embeddings found" — that second meaning is
#     REMOVED as of round 3 (it is now a "done" outcome, above), so a
#     "skipped" entry means exactly one thing regardless of which metric or
#     night produced it.
REPAIR_RECORD_STATUS_VALUES: frozenset[str] = frozenset({
    "done", "carried", "unavailable", "skipped",
})


def validate_repair_record(record: dict) -> list[str]:
    """Validate a repair-pass provenance record (engram-alpha#2519) against
    the schema declared above. Returns a list of violation strings — an
    empty list means the record is valid. Mirrors ``health_engine_config.
    validate_config`` / ``health_engine_model.validate_model``'s "collect
    every violation found in one pass, not just the first" contract (see
    those functions' docstrings for the reasoning) — a caller sees the
    whole problem at once, not one field at a time across N re-runs. Does
    NOT raise: the caller (the dream-master agent, or a future #2518 panel)
    decides what "invalid" means in its own context (log-and-skip, or
    render an explicit UNKNOWN state) — this function only names the
    problem, mirroring ``EngineConfigError``/``ModelError``'s violation-list
    shape without adopting their raise-on-collect behavior, since this
    schema has no single privileged construction path to gate the way
    ``EngineConfig.__post_init__`` gates its own class.

    Checks:
      1. ``record``'s top-level keys are EXACTLY ``REPAIR_RECORD_TOP_LEVEL_
         FIELDS`` — no missing, no extra. A renamed/missing top-level field
         is what makes this a loud, testable failure instead of a silently
         empty-looking panel (the design-authority concern this validator
         exists to satisfy).
      2. ``record["metrics"]`` is a dict; every entry's keys are EXACTLY
         ``REPAIR_RECORD_METRIC_FIELDS`` — no missing, no extra, no
         misspelling.
      3. Every entry's ``"status"`` is a member of ``REPAIR_RECORD_STATUS_
         VALUES``.
      4. [round 2, engram-alpha#2519] The top-level ``"status"`` (when
         present) is a member of ``REPAIR_RECORD_RUN_STATUS_VALUES`` — the
         WHOLE-PASS-level enum, distinct from check 3's per-metric one.
         ``"reason"`` (top-level or per-metric) is not type/value-checked
         beyond the generic key-set check above — it is a free-form
         ``str | None`` explanatory field, not itself an enum.
    """
    violations: list[str] = []

    if not isinstance(record, dict):
        return [f"record must be a dict, got {type(record)!r}"]

    top_keys = set(record.keys())
    expected_top = set(REPAIR_RECORD_TOP_LEVEL_FIELDS)
    missing_top = expected_top - top_keys
    extra_top = top_keys - expected_top
    if missing_top:
        violations.append(
            f"record is missing top-level field(s): {sorted(missing_top)!r}"
        )
    if extra_top:
        violations.append(
            f"record has unexpected top-level field(s): {sorted(extra_top)!r}"
        )

    if "status" in record and record["status"] not in REPAIR_RECORD_RUN_STATUS_VALUES:
        violations.append(
            f"record top-level status {record['status']!r} is not a member "
            f"of REPAIR_RECORD_RUN_STATUS_VALUES "
            f"{sorted(REPAIR_RECORD_RUN_STATUS_VALUES)!r}"
        )

    if "metrics" in record:
        metrics_val = record["metrics"]
        if not isinstance(metrics_val, dict):
            violations.append(
                f"record['metrics'] must be a dict, got {type(metrics_val)!r}"
            )
        else:
            expected_metric_keys = set(REPAIR_RECORD_METRIC_FIELDS)
            for signal_name, entry in metrics_val.items():
                if not isinstance(entry, dict):
                    violations.append(
                        f"metrics[{signal_name!r}] must be a dict, got "
                        f"{type(entry)!r}"
                    )
                    continue
                entry_keys = set(entry.keys())
                missing = expected_metric_keys - entry_keys
                extra = entry_keys - expected_metric_keys
                if missing:
                    violations.append(
                        f"metrics[{signal_name!r}] is missing field(s): "
                        f"{sorted(missing)!r}"
                    )
                if extra:
                    violations.append(
                        f"metrics[{signal_name!r}] has unexpected field(s): "
                        f"{sorted(extra)!r}"
                    )
                if "status" in entry and entry["status"] not in REPAIR_RECORD_STATUS_VALUES:
                    violations.append(
                        f"metrics[{signal_name!r}] status {entry['status']!r} "
                        f"is not a member of REPAIR_RECORD_STATUS_VALUES "
                        f"{sorted(REPAIR_RECORD_STATUS_VALUES)!r}"
                    )

    return violations


def repair_record_missing_metric_names(
    record: dict, model: ScoringModel = MODEL
) -> list[str]:
    """Metric ``signal`` names declared in ``model.metrics`` but ABSENT from
    ``record["metrics"]`` — the COMPLETENESS half of Kepler's #2519 review
    ruling ("every metric in the shipped model gets an entry, always").
    JUDGMENT CALL (flagged in the round-2 report): added as a small,
    cheap-to-write schema-level helper rather than judged out of scope —
    after this round an absent entry must mean exactly one thing (the pass
    did not run for that metric at all), and this is the mechanical way a
    caller (the dream-master's own pre-write self-check, or a future #2518
    panel) can detect that condition directly against the live model,
    instead of eyeballing the metrics dict against ``MODEL_SPEC`` by hand.

    Does NOT duplicate ``validate_repair_record``'s own structural checks —
    a record that is itself malformed (not a dict, or ``"metrics"`` not a
    dict) reports EVERY declared metric name as missing, since nothing
    usable was found to compare against; pair this with
    ``validate_repair_record`` for the full picture, not as a replacement
    for it. Returns a SORTED list (stable, testable order) of signal names;
    empty means the record is complete against ``model``.
    """
    declared = {m.signal for m in model.metrics}
    if not isinstance(record, dict) or not isinstance(record.get("metrics"), dict):
        return sorted(declared)
    present = set(record["metrics"].keys())
    return sorted(declared - present)


def _count_null_embeddings(conn: sqlite3.Connection, population: str) -> int:
    """Count of CURRENT nodes in ``population`` with a NULL ``embedding``
    column — the repair pass's own read-only scanner for ``embedding_
    coverage``'s detector (``scan:null-embeddings``, engram-alpha#2519).

    Mirrors ``health_engine_metrics.embedding_coverage``'s own "missing"
    query shape exactly (same ``is_current = 1 AND type IN (...) AND
    embedding IS NULL`` predicate) — deliberately kept identical rather than
    a fresh independent query, so this scanner's ``findings_count`` and that
    metric's own raw signal are always counting the SAME thing. Unlike that
    metric, this function does NOT gate on embedder availability
    (``core._embedder.is_available()``) — by the time the dream-master's
    repair pass reaches this call, tonight's dream projection has already
    been read (the repair pass's own precondition step) and ``embedding_
    coverage``'s raw signal has already been checked as "available this
    run" before this scanner is ever invoked (an unavailable-this-run
    metric is skipped entirely, per the dream-master spec's own per-metric
    dispatch step 2) — so a second, redundant availability gate here would
    only ever be reached in a state that step 2 has already excluded.

    ``population`` is the metric's own declared ``denominator_population``
    (``health_engine.CONFIG.metric("embedding_coverage").denominator_
    population``) — today always ``"claim_bearing_nodes"``. Only that one
    population is implemented; a different value raises ``ValueError``
    rather than silently returning a wrong count, per this codebase's own
    "loud guard, not silent workaround" discipline — a future population
    value is a genuine code change here, not something to guess at.
    """
    if population != "claim_bearing_nodes":
        raise ValueError(
            f"_count_null_embeddings only implements the "
            f"'claim_bearing_nodes' population, got {population!r}"
        )

    import engram_core as core  # lazy — see module-top NOTE

    claim_bearing_types = tuple(sorted(core.CLAIM_BEARING_TYPES))
    placeholders = ",".join("?" * len(claim_bearing_types))
    return conn.execute(
        f"SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND type IN "
        f"({placeholders}) AND embedding IS NULL",
        claim_bearing_types,
    ).fetchone()["c"]
