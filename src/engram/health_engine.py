"""health_engine — the real production engine: CONFIG, composition, and the
snapshot writer for the health-score redesign (engram-alpha#1991, PR2 of 4).

Design-of-record: engram-alpha#1991, issue comment 5372283060 (Borges,
2026-08-21, "Health-score redesign — DESIGN v2 (build basis)") §2 (aspects),
§3 (composition), §4 (snapshot), §5 (engine contract).

Scope of this module:
  1. The real production ``CONFIG`` (an ``EngineConfig``) — aspect A
     (epistemic integrity, weight 40) and aspect C (provenance integrity,
     weight 25) are REAL, built from health_engine_metrics.py's signal
     functions. Aspect B (retrievability, weight 35) is a STUB — see the
     "aspect B" section below for why a stub ships in this PR rather than
     deferring the whole CONFIG object.
  2. The top-level composition function (design doc §3's weighted geometric
     mean across aspects).
  3. The snapshot writer (design doc §4.1) — ``build_snapshot`` (pure
     computation) and ``write_snapshot`` (computation + best-effort JSON
     persistence under ``DATA_DIR/health-snapshots/``).

Explicitly OUT of scope (per the PR2 spec): aspect B's real signals (PR3),
red flags / unscored diagnostics beyond bare empty placeholders (PR3/PR4),
dual-formula transition + consumer wiring / engram_stats.py itself (PR4).

--------------------------------------------------------------------------
Judgment call — "what to do about the third aspect (B) not existing yet"
--------------------------------------------------------------------------
PR1's EngineConfig requires aspect weights to sum to 100 (``_check_weights_
sum``). With only A (40) and C (25) built, a real production CONFIG summing
to 65 would fail PR1's own validator — CORRECTLY (an incomplete config
should fail loud, not silently declare itself valid at the wrong total).

Two honest options (both discussed in the PR2 spec):
  (a) Don't ship a real production CONFIG at all this PR — defer the whole
      object to PR3/PR4, use only fixture configs in this PR's own tests.
  (b) Ship a real CONFIG now, with an explicit "not yet real" stub AspectDef
      for B (weight 35, one signal function that always returns
      UNAVAILABLE) — the config validates AND correctly reports the WHOLE
      engine as degraded (via UNAVAILABLE propagation through both the
      aspect-level and top-level composition) until PR3 lands.

This module takes option (b), matching the spec author's stated mild
preference: it is more honest about "the composed score isn't trustworthy
yet" than deferring the whole CONFIG object, and it means the snapshot
writer + checkpoint wiring this PR also builds can be exercised against a
REAL (if partially-degraded) production config rather than only fixtures —
closer to the actual PR3/PR4 end state, and closer to design doc §5.1's
point that a contract should make the wrong state hard to reach silently,
not merely possible to reach correctly later.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import health_engine_metrics as metrics
from health_engine_config import (
    AspectDef,
    EngineConfig,
    MetricDef,
    UNAVAILABLE,
    _Unavailable,
)

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
# PR2 of 4: aspects A + C are real signal functions; aspect B is a stub
# (always UNAVAILABLE) until PR3. Bump when the CONFIG's aspect/metric shape
# changes in a way that would make an old stored snapshot's `config` block
# non-comparable to a fresh one.
ENGINE_VERSION = "0.2.0-pr2"


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
    empty_denominator="clean",
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
# Aspect B — Retrievability (weight 35, design doc §2) — STUB, see module
# docstring's "what to do about the third aspect" section. PR3's real job.
# ---------------------------------------------------------------------------

_RETRIEVABILITY_NOT_YET_AVAILABLE = MetricDef(
    name="retrievability_not_yet_available",
    signal_fn=metrics.not_yet_available_signal,
    denominator_population="not_yet_implemented",
    empty_denominator="unavailable",
    scored=True,
    state_valued=True,
)

ASPECT_B_RETRIEVABILITY_STUB = AspectDef(
    name="retrievability",
    weight=35.0,
    metrics=(_RETRIEVABILITY_NOT_YET_AVAILABLE,),
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
        ASPECT_B_RETRIEVABILITY_STUB,
    ),
)


# ---------------------------------------------------------------------------
# Top-level composition (design doc §3)
# ---------------------------------------------------------------------------


def compose_aspect_scores(
    config: EngineConfig, aspect_scores: dict[str, float | _Unavailable]
) -> float | _Unavailable:
    """Weighted geometric mean across aspects: ``composed = 100 * Π
    (aᵢ/100)^wᵢ``, weights normalized to 1 (design doc §3).

    Each aspect score is floored at 1 before composing (avoids the zero
    singularity while keeping catastrophic visibly catastrophic). UNAVAILABLE
    in ANY aspect propagates to the composed score — never silently composed
    over a hole (design doc §3, "degradation propagates").
    """
    if any(v is UNAVAILABLE for v in aspect_scores.values()):
        return UNAVAILABLE

    total_weight = sum(aspect.weight for aspect in config.aspects)
    product = 1.0
    for aspect in config.aspects:
        score = aspect_scores[aspect.name]
        floored = max(score, 1.0)
        normalized_weight = aspect.weight / total_weight
        product *= (floored / 100.0) ** normalized_weight
    return 100.0 * product


# ---------------------------------------------------------------------------
# Engine execution — running signal + scoring functions against a real conn
# ---------------------------------------------------------------------------


def _run_aspect(
    conn: sqlite3.Connection, aspect: AspectDef
) -> tuple[dict[str, float | _Unavailable], float | _Unavailable]:
    """Run every metric in one aspect; return (raw_by_metric, aspect_score)."""
    raw_by_metric: dict[str, float | _Unavailable] = {}
    for metric in aspect.metrics:
        raw_by_metric[metric.name] = metric.signal_fn(conn)
    aspect_score = aspect.scoring_fn(raw_by_metric)
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
_METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
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
    },
    "open_contradiction_rate": {
        "meaning": (
            "Fraction of current contradiction nodes whose status is "
            "'active' or 'partially_resolved' (i.e. not resolved)."
        ),
        "denominator": "total current contradiction nodes",
        "why_that_denominator": (
            "Design doc §5 contract item 5's own worked example: a "
            "contradiction-free graph is genuinely 0% open, not unknown."
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
    "retrievability_not_yet_available": {
        "meaning": (
            "STUB. Aspect B's real signals (recall_summary/keywords/"
            "embedding coverage, giant-component fraction) are PR3's job; "
            "this always returns UNAVAILABLE so the engine validates at "
            "weight-100 total while honestly reporting the whole engine as "
            "degraded until PR3 lands."
        ),
        "denominator": "not yet implemented",
        "why_that_denominator": "n/a — stub, see health_engine.py module docstring.",
    },
}


def build_snapshot(
    conn: sqlite3.Connection, checkpoint_mode: str, config: EngineConfig = CONFIG
) -> dict:
    """Compute one health-score snapshot (design doc §4.1). Pure
    computation — persistence (writing it somewhere durable) is the
    caller's job; see ``write_snapshot`` below.

    ``checkpoint_mode`` must be one of "nap" / "pre_sleep" / "post_dream"
    (design doc §4.1).
    """
    if checkpoint_mode not in ("nap", "pre_sleep", "post_dream"):
        raise ValueError(
            "checkpoint_mode must be one of 'nap'/'pre_sleep'/'post_dream', "
            f"got {checkpoint_mode!r}"
        )

    import engram_core as core  # lazy — see module-top NOTE

    signals: dict[str, object] = {}
    scores_per_metric: dict[str, object] = {}
    scores_per_aspect: dict[str, object] = {}
    aspect_raw_scores: dict[str, float | _Unavailable] = {}

    for aspect in config.aspects:
        raw_by_metric, aspect_score = _run_aspect(conn, aspect)
        for metric_name, raw in raw_by_metric.items():
            signals[metric_name] = _to_jsonable(raw)
            if raw is UNAVAILABLE:
                scores_per_metric[metric_name] = "UNAVAILABLE"
            else:
                scores_per_metric[metric_name] = metrics.metric_goodness(raw)
        scores_per_aspect[aspect.name] = _to_jsonable(aspect_score)
        aspect_raw_scores[aspect.name] = aspect_score

    composed = compose_aspect_scores(config, aspect_raw_scores)

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
        },
        # Out of scope for PR2 (design doc §2 "out of scoring, deliberately"
        # + PR2 spec's explicit "empty for this PR" instruction) — DAG
        # violations / unscored activity+age diagnostics land in PR3/PR4.
        "red_flags": [],
        "diagnostics": {},
        "anchor": _anchor_commit_sha(),
        "turn": core._get_current_turn(),
        "checkpoint_mode": checkpoint_mode,
    }


def _snapshot_dir() -> Path:
    import engram_core as core  # lazy — see module-top NOTE

    d = core.DATA_DIR / "health-snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_snapshot(
    conn: sqlite3.Connection, checkpoint_mode: str, config: EngineConfig = CONFIG
) -> dict:
    """Compute + persist a snapshot as JSON under
    ``DATA_DIR/health-snapshots/<turn>-<checkpoint_mode>-<timestamp>.json``.

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
    """
    snapshot = build_snapshot(conn, checkpoint_mode, config)
    turn = snapshot["turn"]
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    filename = f"{turn}-{checkpoint_mode}-{ts}.json"
    path = _snapshot_dir() / filename
    try:
        path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        snapshot["_snapshot_path"] = str(path)
    except OSError as e:
        snapshot["_snapshot_write_error"] = str(e)
    return snapshot
