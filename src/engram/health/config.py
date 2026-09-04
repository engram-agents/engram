"""health.config — config schema, loader, and sanity checks for the
health-score redesign engine (engram-alpha#1991, PR1 of 4).

Design-of-record: engram-alpha#1991, issue comment 5372283060 (Borges,
2026-08-21, "Health-score redesign — DESIGN v2 (build basis)").

Scope of this module: the engine's config SCHEMA, its LOADER, and its
SANITY-CHECK VALIDATOR (design doc §5, contract items 1-6). No aspects, no
metrics, and no signal functions for the real graph live here — those are
PR2 (aspects A/C) and PR3 (aspect B). This module's own tests
(tests/test_health_engine_config.py) exercise small fake fixture configs to
prove the validator works; they never reference the real health-score
aspects.

This module does not touch, import, or modify ``engram_stats.py`` /
``_compute_health_score`` — the OLD formula. It stays untouched through this
PR and is only wired in as a diagnostics-only comparison line in a later PR.

Why the sanity checks matter more than the schema itself (design doc §5.1):
Borges introduced a real bug into the FIRST prototype of this exact system —
he read ``DAG_EXEMPT_RELATIONS`` (a derived ``frozenset`` with no source
literal to match) via regex instead of importing it. The empty regex match
silently produced a wrong health score (60.0 instead of the true 70.0) with
zero DAG-violation exemptions applied, and no layer of the code caught it —
the second time he personally hit that exact defect class after already
having filed the general remedy eight days earlier. Holding the remedy in a
doc did not prevent the recurrence; only a mechanical check would have. The
checks in ``validate_config`` below exist so that a config which passes them
is structurally impossible to get wrong the way that bug did.
"""

from __future__ import annotations

import sqlite3
import types
import typing
from dataclasses import dataclass
from typing import Callable, Literal


# ---------------------------------------------------------------------------
# UNAVAILABLE sentinel (design doc §5, contract item 3)
# ---------------------------------------------------------------------------


class _Unavailable:
    """Sentinel: a signal that could not be computed. Never a number, never
    coerced to 0 anywhere downstream.

    Deliberately a dedicated singleton class rather than ``None``: an
    unpopulated field could plausibly mean "not yet computed", whereas
    UNAVAILABLE specifically means "the population this metric needs doesn't
    exist on this graph" (design doc §5, contract item 3 — "UNAVAILABLE is
    unrepresentable as a number ... cannot be coerced to 0 downstream").
    """

    __slots__ = ()

    def __repr__(self):
        return "UNAVAILABLE"

    def __bool__(self):
        # Deliberate: the whole point of this sentinel is that nothing may
        # treat it as falsy-zero-ish by accident. An explicit `is UNAVAILABLE`
        # check is required, not an implicit bool test that could silently
        # pass or fail. If this raise causes real friction in a later PR's
        # actual signal functions, that friction is itself information —
        # surface it rather than removing the raise.
        raise TypeError(
            "UNAVAILABLE has no truthiness — an explicit check (is UNAVAILABLE) "
            "is required, not an implicit bool test that could silently pass or fail"
        )


UNAVAILABLE = _Unavailable()


# ---------------------------------------------------------------------------
# Declared denominator populations
# ---------------------------------------------------------------------------
#
# Known denominator populations this engine can ever compute a metric over.
# PR1 shipped a PLACEHOLDER set (claim_bearing_nodes / observations /
# derivations / contradictions / edges) with no real aspect/metric
# definitions to back them yet — those names existed only so PR1's own
# schema tests had real population strings to exercise the "denominator must
# be a declared population" check against a live frozenset object.
#
# PR2 (aspects A + C, engram-alpha#1991) is the first PR to actually BACK
# those names with real signal functions (see health/metrics.py /
# health/engine.py) — claim_bearing_nodes / observations / derivations /
# contradictions are now load-bearing, not placeholders. PR2 also added
# "evidence_nodes" (dangling-evidence rate's denominator — aspect C has no
# existing placeholder for it) and a PR2-only "not_yet_implemented" stand-in
# for aspect B's then-stub metric (design doc §7's "PR2 ships a stub AspectDef
# for B" choice).
#
# PR3 backs aspect B's REAL metrics (recall_summary/keywords/embedding
# coverage, giant-component fraction) — all four are scored over the SAME
# "claim_bearing_nodes" population aspects A/C already use (design doc §2's
# "claim-bearing projection" language; see health/metrics.py's aspect-B
# section for why), so no new population name was needed for them. PR3 also
# retires "not_yet_implemented" — nothing references it once the aspect-B
# stub metric it existed for is gone (a stale, unreferenced population name
# left in this set would itself be the kind of "one fact drifting out of
# sync" this module's own docstring warns against).

DECLARED_POPULATIONS: frozenset[str] = frozenset({
    "claim_bearing_nodes",
    "observations",
    "derivations",
    "contradictions",
    "edges",
    "evidence_nodes",
})


# ---------------------------------------------------------------------------
# Config error
# ---------------------------------------------------------------------------


class EngineConfigError(Exception):
    """Raised by ``validate_config`` / ``load_config`` when a config fails a
    sanity check. The message lists EVERY violation found in one pass, not
    just the first — a builder should see the whole problem at once rather
    than fix-and-rerun N times.
    """


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

EmptyDenominatorPolicy = Literal["clean", "unavailable"]


@dataclass(frozen=True)
class MetricDef:
    """One config-declared metric (design doc §5, contract item 1).

    ``empty_denominator`` deliberately has NO default value in this
    definition (contract item 5). "clean" means an empty population is a
    legitimate zero (e.g. zero open contradictions on a contradiction-free
    graph is genuinely 0%, not unknown); "unavailable" means an empty
    population makes the metric meaningless (e.g. a coverage rate over zero
    candidate nodes). Because the field is required, Python's own dataclass
    machinery enforces "every metric must declare a policy" at construction
    time, before ``validate_config`` ever runs — do NOT "helpfully" add a
    default here. Doing so would silently reopen the exact 0/0 conflation
    that this contract item exists to close (a metric could then omit the
    choice and fall through to an implicit default, which is precisely the
    bug class contract item 5 forbids).
    """

    name: str
    signal_fn: Callable[[sqlite3.Connection], float | _Unavailable]
    denominator_population: str
    empty_denominator: EmptyDenominatorPolicy
    scored: bool
    state_valued: bool
    dive_target: str | None = None


@dataclass(frozen=True)
class AspectDef:
    """One config-declared aspect (design doc §2 — e.g. epistemic integrity,
    retrievability, provenance integrity), holding its weight and metrics."""

    name: str
    weight: float
    metrics: tuple[MetricDef, ...]
    scoring_fn: Callable


@dataclass(frozen=True)
class EngineConfig:
    """The full engine config: schema/engine version plus the aspect list.

    ``__post_init__`` runs ``validate_config(self)`` on every construction —
    not just on the ``load_config()`` path — so it is impossible to LOAD or
    even directly CONSTRUCT an ``EngineConfig`` that fails a sanity check,
    not merely impossible to declare one via the intended entry point
    (Borges's colleague-review finding on #2187: constructing ``EngineConfig``
    directly bypassed validation entirely, the same "guard upstream of the
    effect that the effect can route around" shape as the class of bug this
    whole PR exists to close).

    Deliberately scoped to THIS class only, not to ``MetricDef``/``AspectDef``
    — by the time ``EngineConfig.__post_init__`` fires, every nested metric
    and aspect object already exists in full, so ``validate_config`` still
    walks the complete tree and still collects every violation into one
    message. Adding ``__post_init__`` validation to the inner dataclasses too
    would break that: a single bad ``MetricDef`` would raise the moment it's
    constructed, before the surrounding ``AspectDef``/``EngineConfig`` even
    exist to be checked as a whole — silently reverting to first-violation
    reporting for that violation instead of the "see everything at once"
    contract the top-level check exists to give. Verified empirically before
    landing this: constructing a config with two simultaneous violations
    (bad weight sum + bad denominator) through this path still raises ONE
    ``EngineConfigError`` naming both, not two separate failures.
    """

    schema_version: str
    engine_version: str
    aspects: tuple[AspectDef, ...]

    def __post_init__(self) -> None:
        validate_config(self)

    def metric(self, name: str) -> MetricDef | None:
        """Look up a declared ``MetricDef`` by name across every aspect.

        Added for engram-alpha#1991/#2520 round 3 (Kepler's ruling on PR5's
        row-local population check): ``health.model.validate_model``
        cross-validates a ``MetricBinding``'s ``detector``/``additional_
        detector`` population against THIS config's own real ``denominator_
        population`` for the same-named metric — ``config.metric(row.signal)
        .denominator_population`` is the real source of truth, per Kepler's
        ruling verbatim. Returns ``None`` (not a raised exception) when no
        metric with this name is declared anywhere in this config — the
        caller treats a miss as a violation to collect, not an exception to
        propagate, matching this module's own "collect every violation, not
        just the first" discipline.

        Deliberately a plain linear scan over ``self.aspects`` rather than a
        cached name->MetricDef dict: the real config has 9 metrics total, so
        an O(n) scan per lookup is not a performance concern at this scale,
        and a frozen dataclass caching a derived index would add complexity
        (bypassing ``__setattr__``, e.g. via ``object.__setattr__`` or
        ``functools.cached_property``) for no measurable benefit here.

        FIRST-MATCH SEMANTICS, STATED EXPLICITLY (reviewer-fairy suggestion,
        PR5 round 3 review, #2520): ``validate_config`` does NOT currently
        enforce that a metric name is unique across aspects — nothing stops
        two different ``AspectDef``s from each declaring a ``MetricDef`` with
        the same ``name``. If that ever happens, this method silently
        returns whichever one it encounters first in ``self.aspects`` order,
        which would mask exactly the kind of cross-module drift the caller
        above uses this method to catch. Not exploitable under the real
        9-metric config today (all 9 names are distinct); flagged here
        rather than fixed with a raising check, because a hard uniqueness
        constraint has real blast radius on this file's own test fixtures
        (several pre-existing tests reuse the same default metric name
        across multiple aspects for unrelated reasons, e.g. weight-sum
        checks) that a narrow fast-follow shouldn't take on. If this class
        of drift becomes a live concern, add the uniqueness check to
        ``validate_config`` as its own deliberate change, not folded
        silently into an unrelated commit.
        """
        for aspect in self.aspects:
            for metric in aspect.metrics:
                if metric.name == name:
                    return metric
        return None


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


def _check_weights_sum(
    items,
    weight_attr: str,
    expected_total: float,
    level_name: str,
    tolerance: float = 0.01,
) -> list[str]:
    """Generic weight-sum check (contract item 2).

    Written as a standalone helper — not inlined into ``validate_config`` —
    so a future PR that adds per-metric weights inside an aspect can call
    this same helper (e.g. ``_check_weights_sum(aspect.metrics, "weight",
    100.0, f"aspect {aspect.name!r} metric")``) without rewriting the check.
    """
    violations: list[str] = []
    total = sum(getattr(item, weight_attr) for item in items)
    if abs(total - expected_total) > tolerance:
        violations.append(
            f"{level_name} weights sum to {total!r}, expected {expected_total!r} "
            f"(tolerance +/-{tolerance})"
        )
    return violations


def _check_fn(fn, label: str, violations: list[str]) -> None:
    """Check item 2/6: fn must be callable, and must resolve to something
    with a real, importable __module__ — the mechanism behind contract item
    6 (signal functions IMPORT their constants; config holds a live Python
    object, never a string name to be resolved later, so there is no "parse
    the source to find the constant" step anywhere in the pipeline).

    The __module__ check is a best-effort MECHANICAL PROXY for "this is a
    genuine imported function", not a full guarantee — a module-level lambda
    in a real file would still pass this check even though it's poor style.
    It exists to catch the cheap, common failure: None, a bare string, or a
    throwaway lambda evaluated in the interactive/script scope (__module__
    == "__main__", which is never the module a real production config
    imports its functions from).
    """
    if fn is None or not callable(fn):
        violations.append(f"{label} is not callable: {fn!r}")
        return
    module_name = getattr(fn, "__module__", None)
    if not module_name or not isinstance(module_name, str) or module_name == "__main__":
        violations.append(
            f"{label} has no real importable __module__ (got {module_name!r}) — "
            f"signal_fn/scoring_fn must be genuine imported function references, "
            f"not e.g. a throwaway lambda run in script/interactive scope"
        )


def validate_config(config: EngineConfig) -> None:
    """Validate an ``EngineConfig`` against the design-doc §5 sanity-check
    contract (items 1-6). Raises ``EngineConfigError`` listing EVERY
    violation found (not just the first) if any check fails. Returns
    ``None`` on success.
    """
    violations: list[str] = []

    # --- contract item 4: non-empty required config sets --------------------
    if len(config.aspects) == 0:
        violations.append(
            "config.aspects is empty — a config with zero aspects is a config "
            "error, not \"nothing to score\""
        )

    for aspect in config.aspects:
        if len(aspect.metrics) == 0:
            violations.append(
                f"aspect {aspect.name!r} has zero metrics — an aspect with no "
                f"metrics is a config error, not \"an aspect that matches nothing\""
            )

    # --- aspect weight must be > 0 ------------------------------------------
    # Not one of the six enumerated contract-item checks, but stated as a
    # hard "must be > 0" on AspectDef.weight itself; enforcing it here keeps
    # a zero/negative weight from silently passing the weight-SUM check by
    # being offset elsewhere.
    for aspect in config.aspects:
        if not (aspect.weight > 0):
            violations.append(
                f"aspect {aspect.name!r} weight must be > 0, got {aspect.weight!r}"
            )

    # --- contract item 2: aspect weights sum to 100 -------------------------
    # Skip when aspects is empty — that case is already reported above, and
    # a sum-of-zero-vs-100 message would just restate the same problem.
    if config.aspects:
        violations.extend(
            _check_weights_sum(config.aspects, "weight", 100.0, "aspect")
        )

    # --- contract item 2/6: every declared fn exists and is callable --------
    for aspect in config.aspects:
        _check_fn(aspect.scoring_fn, f"aspect {aspect.name!r} scoring_fn", violations)
        for metric in aspect.metrics:
            _check_fn(
                metric.signal_fn, f"metric {metric.name!r} signal_fn", violations
            )

    # --- contract item 2: denominator is a declared population -------------
    for aspect in config.aspects:
        for metric in aspect.metrics:
            if metric.denominator_population not in DECLARED_POPULATIONS:
                violations.append(
                    f"metric {metric.name!r} denominator_population "
                    f"{metric.denominator_population!r} is not a member of "
                    f"DECLARED_POPULATIONS"
                )

    # --- contract item 5: empty_denominator must be a valid policy value ---
    # The dataclass field being required (no default, see MetricDef's
    # docstring) only closes the "no implicit default" half of contract item
    # 5. It does NOT stop a syntactically well-formed but semantically wrong
    # value (a typo, a copy-paste mistake) from sailing through — the exact
    # "valid-looking input that silently does the wrong thing" shape as the
    # Borges incident this whole PR exists to prevent (§5.1). Caught by
    # reviewer-fairy round 1 via a direct adversarial construction.
    # Derived from the type alias itself (typing.get_args), not a hand-typed
    # duplicate — reviewer-fairy round 2 suggestion: a duplicated literal
    # tuple would fail CLOSED (reject a legitimate new policy value) if a
    # future PR extends EmptyDenominatorPolicy and forgets to update this
    # list too. Not Borges-class silent corruption, but the same "one fact,
    # two unlinked places" shape this module exists to eliminate elsewhere.
    _VALID_EMPTY_DENOMINATOR_POLICIES = typing.get_args(EmptyDenominatorPolicy)
    for aspect in config.aspects:
        for metric in aspect.metrics:
            if metric.empty_denominator not in _VALID_EMPTY_DENOMINATOR_POLICIES:
                violations.append(
                    f"metric {metric.name!r} empty_denominator "
                    f"{metric.empty_denominator!r} is not one of "
                    f"{_VALID_EMPTY_DENOMINATOR_POLICIES!r}"
                )

    # --- contract item 2 (FOLD): scored => state_valued ---------------------
    # An explicit check + message, not a bare Python `assert` — `assert`
    # strips under `-O`, which would silently disable this contract item.
    for aspect in config.aspects:
        for metric in aspect.metrics:
            if metric.scored and not metric.state_valued:
                violations.append(
                    f"metric {metric.name!r} has scored=True but "
                    f"state_valued=False — a turn-delta/recency surface may "
                    f"never be scored, only an unscored diagnostic"
                )

    # --- dive_target: if present, must be a non-empty string ---------------
    # design doc §5 item 10 / this PR's schema section: "No machinery
    # consumes it yet ... No validation beyond 'if present, non-empty
    # string.'" — the field is a bare fairy-hint name, so this is the entire
    # check; do not add anything more elaborate.
    for aspect in config.aspects:
        for metric in aspect.metrics:
            if metric.dive_target is not None:
                if not isinstance(metric.dive_target, str) or not metric.dive_target:
                    violations.append(
                        f"metric {metric.name!r} dive_target must be a "
                        f"non-empty string when present, got "
                        f"{metric.dive_target!r}"
                    )

    if violations:
        raise EngineConfigError("\n".join(violations))


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def load_config(module: types.ModuleType) -> EngineConfig:
    """Load an ``EngineConfig`` from an already-imported module's ``CONFIG``
    attribute, running it through ``validate_config`` before returning it.

    Takes a real, already-imported Python module object — never a path
    string to ``importlib``-resolve, and never a YAML/JSON path. This is
    contract item 6's loader-side counterpart: config never needs
    string-based resolution of anything (a function, a constant), so a real
    production config is just a real ``.py`` file the caller ``import``s
    normally and passes in here, e.g. ``load_config(engram.health_config_real)``.

    Raises ``EngineConfigError`` (not ``AttributeError``/``TypeError``) if
    the module has no ``CONFIG`` attribute, if that attribute isn't an
    ``EngineConfig`` instance, or if the config fails ``validate_config``.
    """
    module_label = getattr(module, "__name__", repr(module))

    if not hasattr(module, "CONFIG"):
        raise EngineConfigError(
            f"module {module_label!r} has no CONFIG attribute"
        )

    config = module.CONFIG
    if not isinstance(config, EngineConfig):
        raise EngineConfigError(
            f"module {module_label!r} CONFIG attribute is not an EngineConfig "
            f"instance (got {type(config).__name__})"
        )

    # Technically redundant now that EngineConfig.__post_init__ validates on
    # every construction (an already-built, already-isinstance-checked
    # EngineConfig has necessarily already passed this) — kept intentionally
    # as explicit defense-in-depth on this entry point rather than removed,
    # since the whole point of this module is never relying on an upstream
    # guard alone.
    validate_config(config)
    return config
