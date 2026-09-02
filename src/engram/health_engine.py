"""health_engine — the real production engine: CONFIG, composition, and the
snapshot writer for the health-score redesign (engram-alpha#1991, PR2+PR3 of
4).

Design-of-record: engram-alpha#1991, issue comment 5372283060 (Borges,
2026-08-21, "Health-score redesign — DESIGN v2 (build basis)") §2 (aspects),
§3 (composition), §4 (snapshot), §5 (engine contract).

Scope of this module:
  1. The real production ``CONFIG`` (an ``EngineConfig``) — all three scored
     aspects are now REAL: A (epistemic integrity, weight 40), C (provenance
     integrity, weight 25), and B (retrievability, weight 35, PR3), all
     built from health_engine_metrics.py's signal functions. PR2 shipped a
     stub AspectDef for B (see the historical "judgment call" note below,
     kept for context); PR3 replaces it with the 4 real metrics.
  2. The top-level composition function (design doc §3's weighted geometric
     mean across aspects).
  3. The snapshot writer (design doc §4.1) — ``build_snapshot`` (pure
     computation) and ``write_snapshot`` (computation + best-effort JSON
     persistence under ``DATA_DIR/health-snapshots/``). PR3 also populates
     the previously-empty ``diagnostics`` block (activity metrics +
     cascade-marker age — design doc §2 "Out of scoring, deliberately");
     these are informational only and never feed ``compose_aspect_scores``.

Explicitly OUT of scope (per the PR3 spec): lambda-2 / algebraic
connectivity (design doc's own "unscored" item — no numpy/scipy/networkx
dependency exists in this codebase and this PR does not add one, and a
hand-rolled eigenvalue solver is exactly the kind of numerically-subtle code
this project's contract-over-discipline philosophy argues against writing
informally); DAG violations as a ``red_flags`` entry (``red_flags`` stays
``[]``, exactly as PR2 left it — the design doc's build slicing doesn't
explicitly assign this to PR3, and PR2's own stub comment already scoped it
to "PR3/PR4" ambiguously, so this PR leaves it for the next slice rather
than silently absorbing it); ``pre_sleep`` checkpoint wiring (PR2 left this
deliberately unwired, not PR3's job either); ``engram_stats.py`` / the OLD
formula (never touched through the whole redesign).

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
# PR3 of 4: aspects A + B + C are all real signal functions now (B replaces
# PR2's always-UNAVAILABLE stub). Bump when the CONFIG's aspect/metric shape
# changes in a way that would make an old stored snapshot's `config` block
# non-comparable to a fresh one.
ENGINE_VERSION = "0.3.0-pr3"


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
# Aspect B — Retrievability (weight 35, design doc §2) — real as of PR3.
# Denominator for all four is "claim_bearing_nodes" (same population aspect
# A's taint_rate already uses) — see health_engine_config.py's
# DECLARED_POPULATIONS comment and health_engine_metrics.py's aspect-B
# section docstring for why. Every raw signal below is the badness-rate
# COMPLEMENT of its coverage-style name — see health_engine_metrics.py's
# module docstring "SIGN CONVENTION" note before reading `signals` values
# from a snapshot as plain coverage percentages.
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
    "recall_summary_coverage": {
        "meaning": (
            "Fraction of current claim-bearing nodes MISSING a "
            "recall_summary (badness rate — 0=all covered, 1=none covered; "
            "see health_engine_metrics.py's module docstring 'SIGN "
            "CONVENTION' note). Reads the recall_summary column's actual "
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
            "Fraction of current claim-bearing nodes with no non-empty "
            "recall_keywords list (badness rate, same convention as "
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
            "Fraction of current claim-bearing nodes with a NULL embedding "
            "column (badness rate, same convention). UNAVAILABLE — not a "
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
            "Fragmentation rate (1 - giant-component fraction) of the "
            "induced subgraph on current claim-bearing nodes only (an "
            "edge counts only if BOTH endpoints are claim-bearing) — "
            "badness rate: 0=fully connected (best), 1=every claim-bearing "
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

    current_turn = core._get_current_turn()

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
