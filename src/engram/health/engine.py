"""health.engine — the real production engine: CONFIG, the dream-side
scoring model, composition, and the snapshot writer for the health-score
redesign (engram-alpha#1991, PR2+PR3+PR5 of 5).

Design-of-record: engram-alpha#1991, issue comment 5372283060 (Borges,
2026-08-21, "Health-score redesign — DESIGN v2 (build basis)") §2 (aspects),
§3 (composition), §4 (snapshot), §5 (engine contract) — PR2/PR3's basis —
SUPERSEDED for the scoring/composition layer specifically by issue comment
5527066386 (Kepler, 2026-09-03, "The dream-side contract for health-v2 —
DRAFT 2"), §1 (the scoring model's shape) and §2 (the dream-side
projection), plus Kepler's five follow-up clarifications (recorded in
health/model.py, inline at the field each one resolves).

Scope of this module (PR5 additions marked):
  1. The real production ``CONFIG`` (an ``EngineConfig``) — the RAW-SIGNAL-
     COMPUTATION layer (health/config.py's denominator-population /
     empty-denominator-policy schema). UNCHANGED by PR5 — still declares all
     nine metrics across three aspects with the SAME ``signal_fn`` callables
     PR5's own health/model.py's ``SIGNALS`` registry names. See "TWO
     PARALLEL SYSTEMS, ON PURPOSE" below for exactly what CONFIG still does
     and what it no longer does, post-PR5.
  2. [PR5] ``MODEL`` (below) — the SCORING layer (draft-2 §1): which aspect
     a signal belongs to, its curve, its importance, its detector/repair
     binding. Read raw signals via CONFIG (1 above), turns them into scores
     via THIS. [Round 3, engram-alpha#1991/#2520] Built here, right after
     CONFIG, via ``health_engine_model.load(health_engine_model.MODEL_SPEC,
     CONFIG)`` — not imported ready-made from health/model.py
     anymore, so ``load()`` can cross-validate every detector's declared
     population against CONFIG's own real ``denominator_population``.
  3. [PR5, replaces PR2/PR3] ``compose_aspect_scores`` and per-aspect
     scoring — the weighted-ARITHMETIC-SUM formula (Kepler's clarification
     1, below), replacing BOTH ``health.metrics.
     aspect_geometric_mean_scoring`` (per-aspect) and the OLD
     ``compose_aspect_scores`` (across aspects), which were both geometric-
     mean. Those two functions are UNCHANGED in health/metrics.py
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

SLICE 2 ADDITION (engram-alpha#2537, "signals as formulas over L0") — the
raw signal path this module drives ``scores``/``aspects``/``composed``
from changes, and TWO PARALLEL SYSTEMS above becomes THREE:

  - ``health.measurements`` (L0, slice 1) — bare graph counts + environment
    facts, read once per ``build_snapshot`` call.
  - ``health.signals`` (L1, THIS slice) — pure arithmetic over that SAME L0
    read. ``_run_aspect`` now sources every metric's raw value from here,
    NOT from calling CONFIG's ``metric.signal_fn(conn)`` directly (that was
    the ENTIRE pre-slice-2 path).
  - ``health.config.CONFIG``'s ``signal_fn`` bindings (still the same nine
    ``health.metrics`` functions, still taking ``conn`` directly) are NOT
    retired and NOT dead code — they are now the SSoT verification oracle:
    ``_verify_l1_against_live_signal_fns`` calls every one of them, on the
    SAME connection the L0 read just used, and asserts exact equality
    against L1's output BEFORE ``build_snapshot`` returns. A mismatch
    raises ``HealthEngineSSoTError`` — not silently absorbed inside THIS
    module (see that exception's own docstring for the precise scope of
    "loud": the sole production caller, ``engram_lifecycle.py``'s
    checkpoint flow, has a pre-existing broad catch that applies uniformly
    to every health-snapshot failure, by design). This is the
    design charge's "recompute L1-L4 from L0 and assert equality against
    the stored cache ... a test that has never been seen to fail is not a
    control" requirement, run on every fresh build rather than deferred to
    a not-yet-built re-score-an-old-snapshot path (storage/migration
    infrastructure the design charge explicitly does not ask this slice to
    build).
  - Version stamps: ``health_version``/``raw_schema_version``/
    ``model_version`` (health/version.py's MAJOR/MINOR) are written into
    every snapshot at the top level, making a stored artifact
    self-describing without needing this module's CURRENT values to
    interpret it later — the whole point of storing L0 as the SSoT
    (design charge: "as long as we don't change the bare data items, the
    whole health score can be reconstructed from previous snapshots").

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
predicate under a new name — health/config.py's ``MetricDef.scored``
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
     construction time, see health/model.py's own docstring for why)
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
health/config.py's ``EngineConfig``/``AspectDef``/``MetricDef`` and
health/model.py's ``ScoringModel``/``AspectSpec``/``MetricBinding``
are BOTH still declared, and BOTH still validate independently (each
requires its own weight/points to sum to 100 — currently the SAME numbers,
40/25/35, but nothing enforces they stay in sync beyond this docstring and
code review). ROUND 3 (engram-alpha#1991/#2520, Kepler's ruling, 2026-09-03)
adds the ONE mechanical exception to "nothing enforces": ``health.
model.load()`` (called below, right after ``CONFIG``) cross-validates every
``MetricBinding``'s declared detector population against ``CONFIG``'s own
real ``denominator_population`` for that same-named metric — a genuine
cross-module check, not merely two independent declarations kept in sync by
review. Everything else about the two systems (weight/points-sum-to-100,
the rest of each schema) is still unenforced across the boundary. What each
still DOES, post-PR5:
  - ``health.config.EngineConfig`` (``CONFIG`` below): declares the
    denominator population + empty-denominator policy for each metric
    (UNCHANGED, not superseded by draft-2, which is silent on denominators)
    and provides the ``signal_fn`` callable. Pre-slice-2, ``_run_aspect``
    called this directly to compute the RAW signal; as of slice 2
    (engram-alpha#2537, see "SLICE 2 ADDITION" below), ``_run_aspect``
    sources raw signals from ``health.signals`` (L1) instead, and
    ``signal_fn`` is called only by the SSoT verification oracle,
    ``_verify_l1_against_live_signal_fns``. Its own weight-sum-to-100 /
    scored-implies-state_valued / dive_target checks all still run,
    unchanged.
  - ``health_engine_model.ScoringModel`` (``MODEL``): declares importance,
    curve, aspect membership, detector/repair for the SAME nine signals, and
    drives the actual ``aspect_score``/``composed`` arithmetic above.
  - ``AspectDef.scoring_fn`` (still set to ``metrics.
    aspect_geometric_mean_scoring`` below, unchanged) and ``MetricDef.
    scored``/``state_valued`` are now VESTIGIAL for real composition — kept
    ONLY because ``health/config.py``'s own schema still requires
    them (a required dataclass field / a validated invariant) and because
    that module is out of this PR's touch scope (flagged, not deleted, per
    the PR5 spec's explicit "do NOT delete health/config.py's
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
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import curves as health_curves
from . import measurements as health_measurements
from . import metrics
from . import model as health_engine_model
from . import signals as health_signals
from . import version as health_version
from .config import (
    AspectDef,
    EngineConfig,
    MetricDef,
    UNAVAILABLE,
    _Unavailable,
)
from .model import ScoringModel

# ROUND 3 (engram-alpha#1991/#2520, Kepler's ruling, 2026-09-03): MODEL is no
# longer imported ready-made from health/model.py — it is built BELOW,
# right after CONFIG, via ``health_engine_model.load(health_engine_model.
# MODEL_SPEC, CONFIG)``. This is the fix for round 2's row-local population
# check: health/model.py can now cross-validate a detector's declared
# population against CONFIG's own real ``denominator_population`` for that
# metric, because construction (this module importing health_engine_model,
# a plain module import with no import-time side effects) and validation
# (via ``load()``, called only once CONFIG exists) are no longer forced to
# happen at the same time. `from . import model as health_engine_model`
# (the module, not a `from .model import MODEL`) is a genuinely ordinary
# top-of-file import now — there is no more circularity to route around
# by deferring it.

# NOTE on engram_core access: deliberately NOT `import engram_core as core` at
# module top level here — see health/metrics.py's module docstring for
# why (the test-suite's fresh_server() module-clearing convention only sweeps
# "server"/"engram_"-prefixed modules; a module-top-level binding here would
# go stale across tests that reload engram_core without knowing to also clear
# this module). Every function below that needs `core` imports it locally.
# health.metrics itself follows the same discipline internally, so
# caching `metrics` (the module object) above is safe regardless of import
# order — its own functions re-resolve engram_core lazily per call too.

SCHEMA_VERSION = "1"
# Bump when the CONFIG's aspect/metric shape OR the scoring model's shape
# changes in a way that would make an old stored snapshot's `config` block
# non-comparable to a fresh one. That rule fires here: engram-alpha#2905
# PR-B unifies aspect A's stale_derivation_rate + taint_rate into one
# pending_premise_rate metric, changing both Aspect A's metric count and
# membership (3 -> 2 MetricDefs). The `-prN` suffix on the prior value
# tracked the closed 5-part health-v1 redesign series ("PR5 of 5" — the
# ENGINE_VERSION git history shows 0.2.0-pr2 -> 0.3.0-pr3 -> 0.4.0-pr5,
# each tied to that series' own numbered PRs, engram-alpha#1991). This
# change is NOT part of that series — it is #2905's independent
# taint/stale-derivation unification — so continuing as "-pr6" would
# misleadingly imply membership in a closed series; the suffix is dropped.
ENGINE_VERSION = "0.5.0"


# ---------------------------------------------------------------------------
# Aspect A — Epistemic integrity (weight 40, design doc §2)
# ---------------------------------------------------------------------------

_PENDING_PREMISE_RATE = MetricDef(
    name="pending_premise_rate",
    signal_fn=metrics.pending_premise_rate,
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
    # health.metrics.open_contradiction_rate's docstring for the
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
    metrics=(_PENDING_PREMISE_RATE, _OPEN_CONTRADICTION_RATE),
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
# A's pending_premise_rate already uses) — see health/config.py's
# DECLARED_POPULATIONS comment and health/metrics.py's aspect-B
# section docstring for why. Every raw signal below returns its OWN LITERAL
# coverage/fraction meaning (1.0 = fully covered/connected, 0.0 = none) —
# see health/metrics.py's module docstring "SIGN CONVENTION" note;
# direction is declared on each metric's bound curve
# (health/model.py's MODEL_SPEC), not encoded in the raw value's sign.
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
# ready-made from health/model.py. This is the whole point of the
# restructure — health_engine_model.load() cross-validates every detector's
# declared population against CONFIG's own real denominator_population,
# which requires a real CONFIG to already exist. health/model.py
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
# BOTH health.metrics.aspect_geometric_mean_scoring (per-aspect) and
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


# _run_aspect (config-driven: iterated CONFIG's AspectDef.metrics) was
# deleted here in engram-alpha#2575 PR C0 round 2 — it became unused once
# score_signals switched to iterating model.aspects/model.metrics directly
# (see score_signals's own docstring for why). Grepped for other
# callers/tests first: none found — every reference elsewhere in this
# module and in tests/test_health_engine.py is prose (docstrings, comments,
# an error-message string) naming the old code path, not a call.


class HealthEngineSSoTError(Exception):
    """Raised by ``_verify_l1_against_live_signal_fns`` when L1 (formulas
    over L0) and the live health.metrics SQL path disagree on the same
    connection, OR at import time if CONFIG's metric names,
    ``health.signals.SIGNALS``, and ``_LIVE_SIGNAL_FNS_FOR_VERIFICATION``
    do not all name exactly the same set of signals (see the COMPLETENESS
    ASSERTION below — a three-way equality check, not just one direction:
    a CONFIG metric absent from SIGNALS would otherwise be an undiagnosed
    ``KeyError`` from ``_run_aspect``'s hot path, not only a silently
    unverified SIGNALS entry). Deliberately loud within THIS module —
    never caught/swallowed here — see ``_verify_l1_against_live_signal_
    fns``'s own docstring for why.

    Not loud all the way to a user, and this class does not claim otherwise:
    the sole production caller, ``engram_lifecycle.py``'s checkpoint flow,
    wraps ``health.engine.write_snapshot`` in a broad ``except Exception``
    that predates this slice and applies uniformly to every health-snapshot
    failure mode, by design ("a health-snapshot failure must never break
    the underlying nap/advance_turn checkpoint tool it's piggybacked onto").
    This exception gets that same treatment — downgraded to a soft
    ``error`` string in ``health_snapshot_summary``, not a crash — which is
    the correct behavior for THAT caller's contract, not a gap this class
    needs to route around. "Loud" here means "not silently absorbed inside
    health.engine itself, at the layer where the divergence is actually
    detected" — reviewer-fairy finding, PR#2556 round 1, flagging that the
    PR's own prose had overstated this."""


def _verify_l1_against_live_signal_fns(
    conn: sqlite3.Connection, l1_signals: dict[str, float | _Unavailable]
) -> None:
    """THE SSoT equality control (engram-alpha#2537 design charge: "keep the
    derived block in the snapshot as a cache ... recompute L1-L4 from L0 and
    assert equality against the stored cache. If they differ, either the
    spec moved or there is a bug, and both are things you want to be loud").

    A stored-snapshot re-score path (recomputing L1-L4 from an OLD
    snapshot's own stored L0) needs snapshot-versioning/migration
    infrastructure this slice does not build (explicitly not this PR's job
    — design charge, "What I am NOT specifying"). This function is the
    control that IS available today, run on every fresh build instead of
    only at some future re-score time: it recomputes every signal via
    CONFIG's ORIGINAL live-connection path (``health.metrics``'s SQL
    functions, unchanged since PR1) on the SAME connection the L0 read
    just used, and asserts exact equality against the L1-derived value.
    Same connection, no reconnect — Ariadne's amendment #3 discipline from
    slice 1's own acceptance test (``tests/test_health_measurements.py``),
    applied here as a live production check rather than only a unit test.

    Raises ``HealthEngineSSoTError`` — see that class's own docstring for
    the precise scope of "loud" — the moment L1 (arithmetic over L0) and
    the live SQL path disagree on the SAME graph state. That divergence
    means one of exactly two things: an L0 measurement's SQL and an L1
    formula have drifted apart (a real bug), or ``health.metrics``'s own
    SQL changed without the L0/L1 layers being updated to match (a
    migration that missed a spot) — both are exactly the class of silent
    SSoT-identity failure this whole slice exists to make impossible.
    """
    mismatches: list[str] = []
    for name, live_fn in _LIVE_SIGNAL_FNS_FOR_VERIFICATION.items():
        live_value = live_fn(conn)
        l1_value = l1_signals[name]
        if l1_value != live_value:
            mismatches.append(f"{name}: L1={l1_value!r} live={live_value!r}")
    if mismatches:
        raise HealthEngineSSoTError(
            "L1 (health.signals, arithmetic over L0) disagrees with the "
            "live health.metrics SQL path on the same connection — the "
            "SSoT identity this design exists to guarantee has broken:\n"
            + "\n".join(f"  {m}" for m in mismatches)
        )


# Name -> live signal_fn, for _verify_l1_against_live_signal_fns above.
# Deliberately a SEPARATE table from CONFIG's own signal_fn bindings
# (health/config.py's MetricDef.signal_fn, which still points at the same
# nine health.metrics functions) rather than reading CONFIG's bindings —
# this table's whole job is being an independent second source, not merely
# CONFIG's list under a new name; the two happening to name the same
# functions today is a fact this control's own logic does not depend on.
_LIVE_SIGNAL_FNS_FOR_VERIFICATION: dict[str, "Callable[[sqlite3.Connection], float | _Unavailable]"] = {
    "pending_premise_rate": metrics.pending_premise_rate,
    "open_contradiction_rate": metrics.open_contradiction_rate,
    "uncited_observation_rate": metrics.uncited_observation_rate,
    "dangling_evidence_rate": metrics.dangling_evidence_rate,
    "recall_summary_coverage": metrics.recall_summary_coverage,
    "recall_keywords_coverage": metrics.recall_keywords_coverage,
    "embedding_coverage": metrics.embedding_coverage,
    "giant_component_fraction": metrics.giant_component_fraction,
}

# COMPLETENESS ASSERTION (reviewer-fairy finding, PR#2556 round 1, extended
# per Ariadne's colleague-review suggestion, round 2): three sets need to
# stay in lockstep, and only one direction was originally guarded.
#
#   CONFIG metric names  --  the names _run_aspect looks up in l1_signals
#   health_signals.SIGNALS.keys()  --  what L1 actually knows how to compute
#   _LIVE_SIGNAL_FNS_FOR_VERIFICATION.keys()  --  what the SSoT oracle checks
#
# The fairy's original check covered SIGNALS -> VERIFICATION (a signal with
# no verification entry silently narrows SSoT coverage). Ariadne's
# colleague pass found the direction this PR ITSELF newly introduces is
# unguarded: _run_aspect changed from metric.signal_fn(conn) (always
# resolvable — CONFIG defines signal_fn per metric) to
# l1_signals[metric.name] (a bare dict subscript) — so a CONFIG metric
# with no matching SIGNALS entry is now a raw KeyError from inside
# build_snapshot's hot path, not the loud, precise diagnostic this
# assertion gives everywhere else. The reverse direction (a stray
# verification-table entry with no SIGNALS match) would raise inside the
# verifier the same ungraceful way. Checking all three sets for exact
# equality closes every direction at once.
#
# Deliberately NOT "derive these from health_signals.SIGNALS" — that would
# make the tables share their key SOURCE, undermining the "independent
# second source" property _LIVE_SIGNAL_FNS_FOR_VERIFICATION's own comment
# explains — so the fix is an explicit CHECKED assertion (import-time,
# fail loud immediately, matching this codebase's "the check is only a
# check if it reads that field" precedent — health/model.py's own
# cross-validation of MetricBinding populations against CONFIG) rather than
# either silent drift or a shared source of truth.
_config_metric_names = {m.name for aspect in CONFIG.aspects for m in aspect.metrics}
_signals_names = set(health_signals.SIGNALS.keys())
_verification_names = set(_LIVE_SIGNAL_FNS_FOR_VERIFICATION.keys())

if _config_metric_names != _signals_names or _signals_names != _verification_names:
    _details = []
    if _config_metric_names - _signals_names:
        _details.append(
            "CONFIG metric(s) with no health.signals.SIGNALS entry "
            f"(_run_aspect would KeyError on these): {sorted(_config_metric_names - _signals_names)}"
        )
    if _signals_names - _config_metric_names:
        _details.append(
            "health.signals.SIGNALS entries with no matching CONFIG metric "
            f"(computed but never used): {sorted(_signals_names - _config_metric_names)}"
        )
    if _signals_names - _verification_names:
        _details.append(
            "health.signals.SIGNALS declares signal(s) with no matching entry "
            "in _LIVE_SIGNAL_FNS_FOR_VERIFICATION — the SSoT check would "
            f"silently skip verifying them: {sorted(_signals_names - _verification_names)}"
        )
    if _verification_names - _signals_names:
        _details.append(
            "_LIVE_SIGNAL_FNS_FOR_VERIFICATION has entry/entries with no "
            "matching health.signals.SIGNALS name (the verifier would "
            f"KeyError on l1_signals[name]): {sorted(_verification_names - _signals_names)}"
        )
    raise HealthEngineSSoTError(
        "CONFIG metric names, health.signals.SIGNALS, and "
        "_LIVE_SIGNAL_FNS_FOR_VERIFICATION must name EXACTLY the same set "
        "of signals — they have drifted apart:\n"
        + "\n".join(f"  {d}" for d in _details)
    )


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
# text onto all eight metric entries (today none of them actually populate
# dive_target — see CONFIG above).
_METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "_dive_target": {
        "status": "deprecated, not deleted (engram-alpha#1991 draft-2 §1)",
        "note": (
            "dive_target is emitted per-metric by _metric_def_to_dict "
            "(health/engine.py, currently line 487) and read by NOTHING "
            "downstream — retiring it is a snapshot-schema change, which is "
            "not this PR's job. Kept on every MetricDef for a future schema "
            "bump (Borges's correction to draft 1's 'dead field': it is "
            "deprecated, not dead)."
        ),
    },
    "pending_premise_rate": {
        "meaning": (
            "Fraction of current claim-bearing nodes carrying an unresolved "
            "cascade marker (root_op IN ('retract', 'supersede'), status "
            "'pending') -- downstream of a retracted OR superseded premise. "
            "Unifies the prior separate stale_derivation_rate / taint_rate "
            "metrics (engram-alpha#2905 PR-B): post-cascade-machine, a "
            "pending retract marker and a pending supersede marker do the "
            "same thing to a node's usability as a premise, regardless of "
            "cause -- so this is scored as one metric, not two."
        ),
        "denominator": (
            "total current claim-bearing nodes (engram_core."
            "CLAIM_BEARING_TYPES) -- same population taint_rate used; "
            "stale_derivation_rate's narrower derivations-only denominator "
            "is retired along with the metric, since the unified numerator "
            "can touch any claim-bearing node type, not derivations alone."
        ),
        "why_that_denominator": (
            "Only claim-bearing nodes can carry a premise-blocking marker "
            "in the epistemic sense; a definition/person/task/goal node is "
            "never a candidate."
        ),
        "unit": (
            "nodes, not marker rows -- a node carrying both a pending "
            "retract AND a pending supersede marker counts once (see "
            "health/metrics.py's pending_premise_rate docstring, 'UNIT' "
            "note)."
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
            "as unmeasured, not as evidence of health. Matches "
            "pending_premise_rate's existing empty-population treatment "
            "(same aspect; pending_premise_rate unifies the prior separate "
            "taint_rate/stale_derivation_rate referenced here originally — "
            "engram-alpha#2905 PR-B)."
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
            "covered; see health/metrics.py's module docstring "
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
    — the SAME population Aspect A's pending_premise_rate (engram-alpha#2905
    PR-B; formerly the separate stale_derivation_rate/taint_rate) already
    scores the BACKLOG SIZE of (design doc §2: "A keeps backlog SIZE
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


def score_signals(
    l1_signals: dict[str, float | _Unavailable],
    model: ScoringModel = MODEL,
) -> tuple[dict, dict]:
    """(signals_jsonable, scores) for one L1 signals dict — EXACTLY the
    block ``build_snapshot`` computes between its L1 read and its return:
    per-metric curve scores, per-aspect scores, composed, unscored_aspects,
    points_in_play, composed_coverage, aspect_coverage. ``build_snapshot``
    calls THIS; the series deriver (PR C1) calls THIS on a stored L0 row
    with a model reconstructed from the snapshot's own
    ``model_defs``/``curve_defs`` (``health.model.model_from_defs``) — one
    function, two callers, so "the same functions the live engine uses"
    (design doc §2) is true by construction rather than by copy.

    RULING (PR C0 round 2, overriding round 1's ``config.aspects``
    decision): iterates ``model.aspects``/``model.metrics`` — NEVER
    ``config.aspects`` — because this function's whole point is to score an
    L1 dict through a model that may be a HISTORICAL reconstruction
    (``health.model.model_from_defs``, PR C1's deriver) of a version's OWN
    declared bindings, not necessarily HEAD's live ``MODEL``. Reading
    HEAD's ``CONFIG.aspects`` names inside this function would read HEAD's
    metric set into a historical model — a ``KeyError`` when a version
    lacks a signal HEAD has, silent omission when it has one HEAD lacks,
    and HEAD's aspect set imposed on a version that declared a different
    one. There is no ``config`` parameter for this reason.

    Key ORDER in ``signals``/``scores.metrics`` is model declaration order
    (``model.aspects``, and within each aspect, ``model.metrics`` filtered
    to that aspect, in ``model.metrics`` declaration order) — NOT a
    contract. It changes from before this extraction for two aspects
    (round 1 measured: ``epistemic_integrity`` config order
    ``[stale, taint, open_contradiction]`` vs model order
    ``[taint, stale, open_contradiction]``; ``retrievability`` config order
    ``[summary, keywords, embedding, giant]`` vs model order
    ``[embedding, summary, keywords, giant]``) — every stored comparison in
    this codebase goes through parsed dicts or ``sort_keys=True`` canonical
    JSON, so key order in a JSON dict is presentation, not a contract.

    A signal present in ``l1_signals`` but bound to no metric in ``model``
    is simply not in the output. A metric in ``model`` whose signal is
    absent from ``l1_signals`` raises ``KeyError`` naming the signal (a
    bare ``l1_signals[m.signal]`` subscript) — never a silent default.
    """
    signals: dict[str, object] = {}
    scores_per_metric: dict[str, object] = {}
    scores_per_aspect: dict[str, object] = {}
    aspect_raw_scores: dict[str, float | _Unavailable] = {}
    aspect_raw_by_metric: dict[str, dict[str, float | _Unavailable]] = {}

    for aspect in model.aspects:
        aspect_metrics = [m for m in model.metrics if m.aspect == aspect.name]
        raw_by_metric: dict[str, float | _Unavailable] = {}
        for m in aspect_metrics:
            raw_by_metric[m.signal] = l1_signals[m.signal]
        aspect_score = _score_aspect(aspect.name, raw_by_metric, model)
        for m in aspect_metrics:
            raw = raw_by_metric[m.signal]
            signals[m.signal] = _to_jsonable(raw)
            if raw is UNAVAILABLE:
                scores_per_metric[m.signal] = "UNAVAILABLE"
            else:
                curve = model.curves[m.curve]
                scores_per_metric[m.signal] = curve.curve_fn(raw)
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

    scores = {
        "metrics": scores_per_metric,
        "aspects": scores_per_aspect,
        "composed": _to_jsonable(composed),
        "unscored_aspects": unscored_aspects,
        "points_in_play": points_in_play,
        "composed_coverage": composed_coverage,
        "aspect_coverage": aspect_coverage,
    }
    return signals, scores


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

    # L0 -> L1 (engram-alpha#2537 slice 2): ONE L0 read, reused for BOTH the
    # stored "measurements" block below AND as L1's own input — never a
    # second, separate L0 read for the two. Same discipline slice 1's own
    # acceptance test established (Ariadne's amendment #3, "one populated
    # fixture, one conn, read twice: once through compute_all_measurements,
    # once through each live signal_fn — SAME connection, no reconnect"),
    # applied here to guarantee snapshot-internal consistency: the stored L0
    # and the L1 derived from it must come from the exact same instant, not
    # two reads that could straddle a concurrent write.
    l0_measurements = health_measurements.compute_all_measurements(conn)
    l1_signals = health_signals.compute_all_signals(l0_measurements)

    # THE SSoT equality control (design charge, see
    # _verify_l1_against_live_signal_fns's own docstring for the full
    # reasoning) — run on every build, on the SAME connection L0 just read,
    # so L1-vs-live divergence is caught the moment it happens rather than
    # only at some future re-score time.
    _verify_l1_against_live_signal_fns(conn, l1_signals)

    # engram-alpha#2575 PR C0: the per-metric/per-aspect scoring block
    # (curve scores, composition, coverage) is now ONE shared function —
    # see score_signals's own docstring for why (design doc §2's "the same
    # functions the live engine uses" made true by construction). Round 2:
    # score_signals iterates the MODEL, not config.aspects — see its own
    # docstring — so no config is passed here.
    signals, scores = score_signals(l1_signals, model)

    return {
        "schema_version": config.schema_version,
        "engine_version": config.engine_version,
        # engram-alpha#2537's two independent version axes (health.version):
        # raw_schema_version (MAJOR — the L0 set's own identity; a snapshot
        # from an older MAJOR is not fully reconstructible) and model_version
        # (MINOR — everything L1-L4; a snapshot is FULLY reconstructible
        # across any MINOR change, which is the whole point of storing L0 as
        # the SSoT). Both stamped here so a stored snapshot is
        # self-describing without needing this module's own current values
        # to interpret it later.
        "health_version": health_version.CURRENT,
        "raw_schema_version": health_version.MAJOR,
        "model_version": health_version.MINOR,
        "config": _config_to_dict(config),
        "definitions": _METRIC_DEFINITIONS,
        # L0 measurement layer (engram-alpha#2537, PR1). Bare graph counts +
        # environment facts — the SSoT. "signals" below is now (slice 2) a
        # formula over THIS SAME dict (health.signals.compute_all_signals),
        # verified equal to the live health.metrics SQL path on the same
        # connection above (_verify_l1_against_live_signal_fns) before this
        # return is reached — see health/measurements.py's module
        # docstring for L0's own design, and health/signals.py's for L1's.
        "measurements": l0_measurements,
        # engram-alpha#2580 -- display labels for the L0 measurement keys
        # above ({measurement_name: label}), so a consumer building the
        # #2518 detail view's numerator/denominator rows doesn't need its
        # own viz-side key->name table. Presentation only: does not affect
        # canonical_l0_hash (see health/measurements.py's MeasurementDef.label
        # docstring).
        "measurement_labels": health_measurements.measurement_labels_jsonable(),
        # Declarative L1 arithmetic metadata (engram-alpha#2518) -- what
        # numerator/denominator/gate L0 keys each signal below is made of,
        # machine-readable so a consumer (e.g. the viz loader) doesn't have
        # to hand-copy a table out of health/signals.py's source. Purely
        # additive: no signal value above is affected by this key's
        # presence.
        "signal_defs": health_signals.signal_defs_jsonable(),
        # engram-alpha#2576, the curve-layer twin of PR #2577's
        # "signal_defs" — declared curve data (knots + contract), never
        # curve_fn.keywords introspection, so a stored snapshot is
        # self-describing about which curve shape actually scored it.
        "curve_defs": health_curves.curve_defs_jsonable(model.curves),
        # engram-alpha#2575 PR C0 — the model-layer twin of
        # signal_defs/curve_defs: which curve/aspect/importance each SIGNAL
        # is bound to, and each aspect's points — so a stored snapshot can
        # be RE-SCORED as of its own version (design doc D4.1), never
        # re-scored through HEAD's model.
        "model_defs": health_engine_model.model_defs_jsonable(model),
        "signals": signals,
        "scores": scores,
        # DAG violations as a red_flags entry are explicitly OUT of scope for
        # PR3 too (see module docstring) — left exactly as PR2 shipped it.
        "red_flags": [],
        "diagnostics": _compute_diagnostics(conn, current_turn),
        "anchor": _anchor_commit_sha(),
        "turn": current_turn,
        "checkpoint_mode": checkpoint_mode,
    }


# engram-alpha#2720: explicit, umask-independent permissions for
# health-snapshots/ and everything written into it, so the operator viz
# (spt, cross-user) can read snapshots without depending on a seat's
# ~/.engram ACL having been set up (or set up with `-d`, or set up at all
# before this directory first existed) -- see the module docstring's
# "The multi-tenant viz" framing in the issue for the failure this fixes.
# Group-read, NOT group-write or world-anything: spt is a member of every
# seat's private group (`id spt` lists every `agent-*` group), so plain
# POSIX group permissions already suffice without relying on ACLs at all;
# ACLs (agentctl Step 7 / tools/*viz-operator-acl* one-shot repair) are a
# belt-and-suspenders grant on TOP of these bits, not a replacement.
_SNAPSHOT_DIR_MODE = 0o750
_SNAPSHOT_FILE_MODE = 0o640


def _snapshot_dir() -> Path:
    import engram_core as core  # lazy — see module-top NOTE

    d = core.DATA_DIR / "health-snapshots"
    existed_before = d.exists()
    d.mkdir(parents=True, exist_ok=True)
    if not existed_before:
        # Set the mode explicitly ONLY at first-creation time -- never
        # re-chmod an already-existing directory. A directory that already
        # carries POSIX ACLs (from agentctl's rename-time grant or the
        # one-shot repair tool) has its ACL MASK entry silently rewritten
        # to match chmod's "group" class bits whenever chmod runs on it
        # (that's how extended-ACL semantics work) -- reapplying 0750 on
        # every checkpoint would repeatedly reclip whatever mask the ACL
        # tooling set, fighting item 2's mask fix instead of complementing
        # it. Only a brand-new directory has no such grant yet to clobber.
        try:
            os.chmod(d, _SNAPSHOT_DIR_MODE)
        except OSError:
            pass  # best-effort -- see write_snapshot's docstring
    return d


def _snapshot_dir_for_read() -> Path:
    """Read-only twin of ``_snapshot_dir()`` (engram-alpha#2940 round 3,
    reviewer-fairy blocker): resolves the SAME path
    (``core.DATA_DIR / "health-snapshots"``) but never creates or chmods
    it. ``_snapshot_dir()`` is the WRITE-path helper -- its
    mkdir/first-creation-chmod is correct there (a checkpoint writer must
    ensure the directory exists), but ``read_latest_composed_summary()``'s
    default (no explicit ``snapshots_dir`` override) path had been calling
    ``_snapshot_dir()`` too, which meant a plain READ (``engram_stats``/
    ``engram_diagnose``, both declared side-effect-free / readOnlyHint=True)
    silently CREATED ``health-snapshots/`` on disk as a side effect --
    and made ``read_latest_composed_summary``'s own
    ``not os.path.isdir(snapshots_dir)`` "no directory yet" branch
    unreachable, since the directory had just been created by the very
    call that was about to check for its absence. This function is what a
    read path should call instead: same resolution, zero side effects."""
    import engram_core as core  # lazy — see module-top NOTE

    return core.DATA_DIR / "health-snapshots"


# ---------------------------------------------------------------------------
# Latest composed-score reader (engram-alpha#2940) -- retires the OLD
# pre-v2 health formula (engram_stats.py's now-retired penalty function) in favor of
# THIS module's own composed score as the single source of truth for
# engram_stats/engram_diagnose/the legacy viz /api/health endpoint. There is
# NO live-compute path for the v2 score -- it is per-checkpoint only (see
# write_snapshot above) -- so this function reads back whatever the LAST
# checkpoint wrote, never recomputes one live. Mirrors
# viz.data.health_v2.get_health_v2_data's own has_snapshot/reason convention
# (see that module's docstring) but is a plain function in THIS module
# rather than an import from viz -- viz depends on health, never the
# other way around, and viz's own filename-discovery/multi-tenant AGENTS
# resolution is deliberately not reused here (this module only ever reads
# its OWN process's snapshot dir, already known via _snapshot_dir() above).
# ---------------------------------------------------------------------------

# Matches write_snapshot's own filename shape exactly -- a deliberate SEPARATE
# copy of viz.data.health_v2's identical regex (same rationale as
# _LIVE_SIGNAL_FNS_FOR_VERIFICATION being a separate table above: this
# module's own contract, not a shared import). See that module's comment for
# the full "why not zero-padded, why `[^-]+`" reasoning -- unchanged here.
_SNAPSHOT_FILENAME_RE = re.compile(r"^(\d+)-([^-]+)-(\d{8}T\d{6}Z)\.json$")


def _latest_snapshot_filename(snapshots_dir: Path) -> "str | None":
    """Pick the highest-turn-number snapshot filename in *snapshots_dir*,
    ignoring anything that doesn't match ``_SNAPSHOT_FILENAME_RE`` (excludes
    the fixed-name dream projection and any ``.error`` sentinel sibling).
    Sorts by a TOTAL, deterministic ordering key -- ``(turn, ts_token,
    mtime_ns, name)`` -- so no two distinct files can ever tie (round-3.6
    fix, engram-alpha#2940 reviewer-fairy round on Mira's own colleague
    review):

    1. Parsed integer ``turn`` -- turn only advances at sleep, so any
       number of same-turn checkpoints (multiple naps in one day) is the
       NORMAL case, not an edge case (round-3 fix).
    2. The filename's own ``ts_token`` (``%Y%m%dT%H%M%SZ``, fixed width,
       zero-padded, UTC -- sorts correctly as a plain string, no datetime
       parsing needed) -- but ``write_snapshot`` stamps only SECOND
       resolution, and the checkpoint_mode is a separate filename
       component, so two DIFFERENT-mode files (e.g. a ``nap`` and a
       ``pre_sleep``) genuinely written within the same wall-clock second
       still tie here (round-3.5's fix stopped one level too early --
       Mira's own exact-head repro: same turn, same ts_token, later
       ``mtime_ns`` file lost to an earlier-listed one).
    3. The file's own ``mtime_ns`` (nanosecond resolution, far finer than
       the filename's second-resolution token) -- breaks the case above by
       reading real write-order off the filesystem directly, independent
       of anything embedded in the name. A file that fails to ``stat()``
       (e.g. deleted between ``listdir`` and this read -- a real, if rare,
       race) sorts as the lowest possible key (``-1``) rather than raising,
       so a vanished file never wins a tie it can't prove it belongs in.
    4. The filename string itself, as the final, purely mechanical
       tiebreaker -- guarantees a TOTAL ordering (no two distinct filenames
       are equal) even in the vanishingly unlikely case two files share
       identical turn, timestamp token, AND mtime_ns (e.g. a filesystem
       with coarser mtime resolution than nanoseconds). This step never
       fires in practice; it exists so the comparison can never raise or
       return a nondeterministic choice, full stop.

    Returns ``None`` if the directory doesn't exist or has no matching
    files."""
    try:
        names = os.listdir(snapshots_dir)
    except OSError:
        return None
    best_name = None
    best_key = None
    for name in names:
        m = _SNAPSHOT_FILENAME_RE.match(name)
        if not m:
            continue
        turn = int(m.group(1))
        ts_token = m.group(3)
        try:
            mtime_ns = os.stat(os.path.join(snapshots_dir, name)).st_mtime_ns
        except OSError:
            mtime_ns = -1  # vanished between listdir() and stat() -- never wins a tie
        key = (turn, ts_token, mtime_ns, name)
        if best_key is None or key > best_key:
            best_key = key
            best_name = name
    return best_name


def _written_at_for_summary(snapshot_path: Path, filename: str) -> str:
    """Best-effort ISO-8601 write timestamp -- same derivation as
    ``viz.data.health_v2._written_at`` (parse the filename's own
    ``%Y%m%dT%H%M%SZ`` timestamp token; fall back to the file's mtime when
    that fails), re-derived here rather than imported (see this section's
    own header comment)."""
    m = _SNAPSHOT_FILENAME_RE.match(filename)
    if m:
        ts_token = m.group(3)
        try:
            dt = datetime.strptime(ts_token, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass  # fall through to the mtime fallback below
    try:
        return datetime.fromtimestamp(
            os.path.getmtime(snapshot_path), tz=timezone.utc
        ).isoformat()
    except OSError:
        return ""


def read_latest_composed_summary(snapshots_dir: "Path | None" = None) -> dict:
    """Read the most recent health-v2 snapshot (by parsed turn number,
    matching ``write_snapshot``'s own filename convention) and extract the
    composed-score summary that ``engram_stats``/``engram_diagnose``/the
    legacy ``/api/health`` endpoint need (engram-alpha#2940). Never raises --
    always returns a dict, with ``has_snapshot=False`` + a ``reason`` string
    for every empty/error/old-format case (mirrors
    ``viz.data.health_v2.get_health_v2_data``'s own has_snapshot/reason
    convention -- see that module for the precedent this follows).

    There is NO live-compute path here -- v2 is per-checkpoint only (written
    by ``write_snapshot`` from a nap/sleep checkpoint) -- so this reads back
    whatever the LAST checkpoint wrote; it never recomputes a live score.

    ``snapshots_dir``, round-2 addition (engram-alpha#2940 reviewer-fairy
    blocker 2): defaults to ``None``, which resolves to this PROCESS's own
    ``_snapshot_dir_for_read()`` (read-only twin of ``_snapshot_dir()``,
    same path -- ``core.DATA_DIR / "health-snapshots"`` -- with no
    mkdir/chmod side effect; round 3 fix, see that function's own
    docstring) -- correct for ``engram_stats``/``engram_diagnose``, which
    only ever report on their
    own live agent. An explicit ``snapshots_dir`` lets a MULTI-TENANT caller
    (the legacy ``viz/data/health.py`` ``/api/health`` endpoint, which is
    handed an arbitrary agent's ``db_path`` and must report THAT agent's
    score, not the viz process's own) point this reader at a different
    agent's snapshot directory instead of silently substituting the wrong
    agent's number under the requested agent's identity -- the regression
    reviewer-fairy round 1 caught and reproduced directly. Every other
    behavior (UNAVAILABLE-string guard, has_snapshot/reason contract) is
    identical regardless of which directory is read.

    Returns, when ``has_snapshot`` is True:
        {
          "has_snapshot": True,
          "score": <float, the composed score, 0-100 -- OR None when the
                    checkpoint's own composed score is UNAVAILABLE (every
                    aspect held out, e.g. a genuinely empty graph); in that
                    case ``unscored_aspects`` below names every held-out
                    aspect, so ``score: None`` is self-explanatory, never
                    an unqualified null>,
          "points_in_play": <float>,
          "unscored_aspects": [<str>, ...],
          "composed_coverage": <float>,
          "snapshot_id": <str, filename minus ".json">,
          "checkpoint_mode": <str, "nap"/"pre_sleep"/"post_dream">,
          "written_at": <str, ISO-8601 UTC>,
          "turn": <int>,
        }
    When ``has_snapshot`` is False: ``{"has_snapshot": False, "reason": <str>}``
    -- the ``score: None``-because-UNAVAILABLE case above is distinct from
    this: the checkpoint DOES exist (``has_snapshot`` stays True), only its
    composed score does not.
    """
    if snapshots_dir is None:
        snapshots_dir = _snapshot_dir_for_read()
    filename = _latest_snapshot_filename(snapshots_dir)
    if filename is None:
        if not os.path.isdir(snapshots_dir):
            return {
                "has_snapshot": False,
                "reason": "No health-snapshots directory yet -- has a checkpoint ever run?",
            }
        return {
            "has_snapshot": False,
            "reason": "health-snapshots directory exists but has no snapshot files yet",
        }

    snapshot_path = snapshots_dir / filename
    try:
        with open(snapshot_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "has_snapshot": False,
            "reason": f"Failed to read/parse snapshot {filename!r}: {exc}",
        }

    # Guard against an old-format (pre-health-v2) snapshot -- same signal
    # viz.data.health_v2.get_health_v2_data uses: it's missing the
    # "measurements" key. Never fall back to an older, usable snapshot
    # nearby -- see that module's docstring for why (a stale writer's
    # higher-turn old-format file sitting beside a lower-turn current-format
    # one means the fix is rebuilding/reconnecting the stale writer, not
    # silently reporting stale data as current).
    if "measurements" not in snapshot:
        return {
            "has_snapshot": False,
            "reason": (
                f"Snapshot {filename!r} is an old-format (pre-health-v2) "
                "snapshot -- it is missing the 'measurements' key. Rebuild "
                "the plugin build and reconnect the MCP server to write a "
                "current-format snapshot."
            ),
        }

    scores = snapshot.get("scores") or {}
    snapshot_id = filename[:-len(".json")] if filename.endswith(".json") else filename

    # engram-alpha#2940 round-2 (reviewer-fairy blocker 1): `scores["composed"]`
    # can legitimately be the SENTINEL STRING "UNAVAILABLE" -- serialized by
    # health.engine._to_jsonable / compose_aspect_scores whenever EVERY aspect
    # is held out (points_in_play <= 0, e.g. a genuinely empty graph). That is
    # a real, reachable state (reproduced directly against a fresh graph with
    # zero claim-bearing nodes), not a hypothetical -- and `has_snapshot` is
    # still True there (the checkpoint file exists and is current-format), so
    # it is NOT the has_snapshot=False path above. Without this guard, callers
    # (engram_stats/engram_diagnose/legacy /api/health, none of which expect a
    # non-numeric health_score) would silently receive the literal string
    # "UNAVAILABLE" where this function's own docstring promises a float.
    # unscored_aspects already names every held-out aspect in this case, so
    # the caller has the "why" without a separate reason field.
    composed = scores.get("composed")
    score = None if composed == "UNAVAILABLE" else composed

    return {
        "has_snapshot": True,
        "score": score,
        "points_in_play": scores.get("points_in_play"),
        "unscored_aspects": scores.get("unscored_aspects"),
        "composed_coverage": scores.get("composed_coverage"),
        "snapshot_id": snapshot_id,
        "checkpoint_mode": snapshot.get("checkpoint_mode"),
        "written_at": _written_at_for_summary(snapshot_path, filename),
        "turn": snapshot.get("turn"),
    }


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


def dream_projection_metric_mismatch(
    projection: dict, model: ScoringModel = MODEL
) -> dict:
    """Compare a dream projection's metric keys against ``model``'s declared
    metric set (engram-alpha#2980). Returns ``{"missing": [...], "extra":
    [...]}`` — sorted signal-name lists; both empty means clean.

    ``missing`` = declared in ``model`` but absent from the projection
    (``model`` knows about a metric the projection's WRITER did not).
    ``extra`` = present in the projection but not declared in ``model``
    (the writer emitted a metric ``model`` no longer declares).

    This is the OUT-OF-PROCESS staleness check a bare ``turn`` comparison
    (the existing precondition check, "The repair pass" → "Precondition" in
    ``engram-dream-master.md``) cannot provide: ``turn`` can match while the
    projection's CONTENT is still stale, because ``write_snapshot`` runs
    inside the long-lived MCP server process, whose in-memory ``MODEL``
    reflects whatever code it was STARTED with — a directory-source plugin
    install updates the on-disk code in place without the running process
    reloading (this repo's own ``CLAUDE.md``, "Plugin deploy" section).
    ``model`` here defaults to the CALLER's own freshly-imported ``MODEL``,
    so a caller running in a fresh process (the dream-master's repair pass,
    a new agent session each night) genuinely compares against TODAY's
    model, even when the projection was written by a stale process — the
    exact gap that let `pending_premise_rate` go without a repair-record
    row for 2+ cycles with the existing `turn`-only check reporting clean.

    JUDGMENT CALL (reviewer-fairy suggestion, engram-alpha#2985, sharpened
    in the delta round): why a content diff and not the projection's own
    ``engine_version`` field, or ``health.version.MAJOR`` (explicitly
    designed to bump "when a measurement is added, removed, or redefined"
    — exactly this scenario, and mechanically hash-gated by
    ``tests/test_health_version_gate.py`` against forgetting -- NOT the
    purely memory-dependent proxy an earlier draft of this note called it).
    The airtight reason is narrower and structural, not a discipline
    argument: the dream-side projection (``_build_dream_projection``,
    above) deliberately narrows what it forwards from the full snapshot to
    ``turn``/``checkpoint_mode``/``engine_version``/``unscored_aspects``/
    ``points_in_play``/``composed_coverage``/``metrics`` only -- it does
    NOT carry ``health_version``/``MAJOR``/``MINOR`` at all. ``engine_version``
    itself is the legacy hardcoded ``ENGINE_VERSION = "0.5.0"`` string this
    module's own docstring says ``health.version`` exists to replace --
    a dead comparand, not merely a weaker one. Using ``MAJOR`` here would
    need a broader, out-of-scope schema change to the projection just to
    have something to compare against. A content diff over the metric KEYS
    THEMSELVES needs no such change and is self-verifying regardless -- the
    same reasoning ``recall_summary_coverage``'s own docstring gives for
    reading literal coverage instead of a lacking-fraction complement (this
    module prefers reading real content over trusting a proxy field
    wherever the two diverge).
    """
    declared = {m.signal for m in model.metrics}
    present = set(projection.get("metrics", {}).keys())
    return {
        "missing": sorted(declared - present),
        "extra": sorted(present - declared),
    }


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
    ``health.config``'s OWN, unrelated ``MetricDef``/``AspectDef``
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

    [engram-alpha#2720] Every file this function writes into
    ``health-snapshots/`` (the main snapshot, the dream projection, and its
    ``.error`` sentinel) gets its mode set explicitly to
    ``_SNAPSHOT_FILE_MODE`` (0o640: owner rw, group r, other none) via
    ``os.chmod`` right after each ``write_text`` call, rather than relying
    on the process umask -- a restrictive umask (or none at all) previously
    left snapshots unreadable by the cross-user, multi-tenant operator viz
    on seats whose ``~/.engram`` never got a `default:user:spt:r-x` ACL (or
    got one before ``health-snapshots/`` existed). Group-read is
    sufficient because the operator viz account is a member of every
    seat's private group; this is belt-and-suspenders with, not a
    replacement for, the ACL grant agentctl/the one-shot repair tool set
    separately. The containing directory gets the same treatment (0o750)
    but ONLY at first-creation time -- see ``_snapshot_dir()``'s comment
    for why re-chmod'ing an already-existing directory is deliberately
    avoided.

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
        # engram-alpha#2720: explicit, umask-independent mode -- see
        # _SNAPSHOT_FILE_MODE's comment above _snapshot_dir(). A chmod
        # failure here is folded into the same best-effort
        # _snapshot_write_error path as the write itself (both OSError).
        os.chmod(path, _SNAPSHOT_FILE_MODE)
        snapshot["_snapshot_path"] = str(path)
    except OSError as e:
        snapshot["_snapshot_write_error"] = str(e)

    try:
        projection = _build_dream_projection(snapshot, model)
        dream_path = _dream_projection_path()
        dream_path.write_text(
            json.dumps(projection, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.chmod(dream_path, _SNAPSHOT_FILE_MODE)  # engram-alpha#2720
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
            os.chmod(_dream_projection_error_path(), _SNAPSHOT_FILE_MODE)  # #2720
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
#      check, mirroring ``health.config.validate_config`` /
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
    empty list means the record is valid. Mirrors ``health.config.
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


def validate_repair_record_full(record: dict, model: ScoringModel = MODEL) -> list[str]:
    """Combined entry point: ``validate_repair_record(record)`` (structural
    shape) PLUS ``repair_record_missing_metric_names(record, model)``
    (completeness against ``model``), merged into one violation list.

    Exists because those two checks disagreeing was a real, silent bug
    (engram-alpha#2980): ``validate_repair_record`` alone returned ``[]``
    on a record missing ``pending_premise_rate``'s entry for at least two
    cycles, because completeness was never its job — but "the natural
    single call" (a caller who wants to know "is this record OK?" and
    reaches for the one function named ``validate_...``) had no way to
    learn that a second, separately-named function existed to catch what
    it didn't. Kept ``validate_repair_record`` and
    ``repair_record_missing_metric_names`` themselves UNCHANGED, not merged
    into each other — the schema-shape check has no reason to depend on a
    live ``model`` object, and folding completeness into it would change
    its signature and contract for every existing caller. This function is
    purely additive: the new "I want the full picture" entry point, not a
    replacement for either half.

    A missing-metric violation is formatted the same shape as
    ``repair_record_missing_metric_names``'s own return value, prefixed for
    consistency with ``validate_repair_record``'s existing message style.
    """
    violations = list(validate_repair_record(record))
    missing = repair_record_missing_metric_names(record, model)
    if missing:
        violations.append(
            f"record is missing metric entries declared in model: {missing!r}"
        )
    return violations


def _count_null_embeddings(conn: sqlite3.Connection, population: str) -> int:
    """Count of CURRENT nodes in ``population`` with a NULL ``embedding``
    column — the repair pass's own read-only scanner for ``embedding_
    coverage``'s detector (``scan:null-embeddings``, engram-alpha#2519).

    Mirrors ``health.metrics.embedding_coverage``'s own "missing"
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
    (``health.engine.CONFIG.metric("embedding_coverage").denominator_
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
