"""health_engine_metrics — signal + scoring function catalogue for aspects A
(epistemic integrity) and C (provenance integrity) of the health-score
redesign engine (engram-alpha#1991, PR2 of 4).

Design-of-record: engram-alpha#1991, issue comment 5372283060 (Borges,
2026-08-21, "Health-score redesign — DESIGN v2 (build basis)") §2 (aspects)
and §5 (engine contract).

Scope of this module: the actual signal functions (``Callable[[sqlite3.
Connection], float | _Unavailable]`` per health_engine_config.MetricDef's
type) for aspect A's three metrics and aspect C's two metrics, plus one
shared aspect-level scoring function used by every real AspectDef in
health_engine.py (including the aspect-B stub). Aspect B's real signal
functions (recall_summary/keywords/embedding coverage, giant-component
fraction) are PR3's job and do not live here — see health_engine.py for the
stub that stands in for aspect B until then.

Each signal function is SELF-CONTAINED: it takes only ``conn`` (per PR1's
fixed ``Callable[[sqlite3.Connection], float | _Unavailable]`` signature —
signal functions do not receive their own MetricDef, so the empty_denominator
policy each function implements below is *by construction* identical to the
policy declared on its corresponding MetricDef in health_engine.py, not
independently checked against it at runtime; validate_config only checks
that the declared policy is a real enum value, not that a signal_fn's
runtime behaviour matches it — see design doc §5 contract item 5).

House rule (mirrors engram_core.py / engram_lifecycle.py): access shared
state ONLY via ``import engram_core as core; core.NAME`` — never via
``from engram_core import NAME``. Unlike the ``engram_*``-prefixed family
modules, this import is done LAZILY, per-function, rather than once at
module top level — deliberately, not an oversight. The test-suite's
``fresh_server()`` fixture pattern (tests/test_nap_payload.py and siblings)
reloads ``engram_core`` per test via ``del sys.modules['engram_core']`` +
re-``import``, but its module-clearing predicate only matches ``"server"``
or a ``"engram_"`` prefix — ``health_engine_metrics`` isn't swept by that
loop. A module-level ``import engram_core as core`` here would bind ONCE,
at whichever test happens to import this module first, and silently keep
referencing that test's stale (possibly since-deleted) ``engram_core``
module object + DATA_DIR across every later test in the same pytest
process — a real cross-test contamination risk, not hypothetical (verified:
Python's import system does not retroactively update an existing name
binding when ``sys.modules`` is mutated elsewhere). Importing inside each
function that needs ``core`` re-resolves via ``sys.modules`` on every call,
so it always sees whichever ``engram_core`` module is currently live.
"""

from __future__ import annotations

import sqlite3

from health_engine_config import UNAVAILABLE, _Unavailable


# ---------------------------------------------------------------------------
# Aspect A — Epistemic integrity — signal functions
# ---------------------------------------------------------------------------


def stale_derivation_rate(conn: sqlite3.Connection) -> float | _Unavailable:
    """Fraction of current derivations with an unresolved cascade marker
    from a supersede event (i.e. currently stale per the live cascade
    machine).

    Reads ``cascade_markers`` (root_op='supersede', status='pending'),
    NOT ``metadata.stale_by``/``stale_via`` — those metadata fields were
    retired by the cascade machine (commit 7f068db1, 2026-08-28) and no
    longer update on retract/supersede. See engram-alpha#1991 comment
    5478591738 §1 for the measured defect this replaces.

    UNIT: the scored quantity is NODES with a pending marker (one per
    node, regardless of how many pending marker rows that node has), NOT
    marker rows — a node can be caught by more than one cascade_event.
    State this explicitly because the OLD formula shipped for months with
    the unit unstated and it was a real spec error then.

    Scope: derivations only (``type = 'derivation'``), matching "stale-
    DERIVATION rate" literally.

    empty_denominator: UNAVAILABLE when there are zero current derivations
    — a graph with no derivations can't have a meaningful stale-derivation
    RATE (0/0, not 0%).
    """
    total = conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE type = 'derivation' AND is_current = 1"
    ).fetchone()["c"]
    if total == 0:
        return UNAVAILABLE

    stale = conn.execute(
        """SELECT COUNT(DISTINCT n.id) as c
           FROM nodes n
           JOIN cascade_markers m ON m.node_id = n.id
           WHERE n.type = 'derivation' AND n.is_current = 1
             AND m.root_op = 'supersede' AND m.status = 'pending'"""
    ).fetchone()["c"]
    return stale / total


def taint_rate(conn: sqlite3.Connection) -> float | _Unavailable:
    """Fraction of current claim-bearing nodes with an unresolved cascade
    marker from a retract event (i.e. currently tainted per the live
    cascade machine).

    Reads ``cascade_markers`` (root_op='retract', status='pending'), NOT
    ``metadata.tainted_by`` — see stale_derivation_rate's docstring for why.

    UNIT: NODES with a pending marker, not marker rows — same convention
    as stale_derivation_rate.

    Denominator is claim-bearing nodes (``engram_core.CLAIM_BEARING_TYPES``),
    not every current node — a definition/person/task/goal node is never
    claim-bearing and can't be tainted in the epistemic sense.

    empty_denominator: UNAVAILABLE when there are zero current claim-bearing
    nodes (degenerate, but handled per contract rather than assumed
    impossible).
    """
    import engram_core as core  # lazy — see module docstring

    claim_bearing_types = tuple(sorted(core.CLAIM_BEARING_TYPES))
    placeholders = ",".join("?" * len(claim_bearing_types))
    total = conn.execute(
        f"SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND type IN ({placeholders})",
        claim_bearing_types,
    ).fetchone()["c"]
    if total == 0:
        return UNAVAILABLE

    tainted = conn.execute(
        f"""SELECT COUNT(DISTINCT n.id) as c
            FROM nodes n
            JOIN cascade_markers m ON m.node_id = n.id
            WHERE n.is_current = 1 AND n.type IN ({placeholders})
              AND m.root_op = 'retract' AND m.status = 'pending'""",
        claim_bearing_types,
    ).fetchone()["c"]
    return tainted / total


def open_contradiction_rate(conn: sqlite3.Connection) -> float:
    """Fraction of current contradiction nodes that are still OPEN
    (``status IN ('active', 'partially_resolved')``), i.e. not resolved.

    empty_denominator: CLEAN (0.0) — the design doc's own worked example
    (§5 contract item 5): a contradiction-free graph has genuinely 0% open
    contradictions, not an unknown rate. n=0 stays visible in the raw
    signal via the caller (health_engine.build_snapshot); this function
    itself only returns the rate.
    """
    rows = conn.execute(
        "SELECT status FROM nodes WHERE type = 'contradiction' AND is_current = 1"
    ).fetchall()
    total = len(rows)
    if total == 0:
        return 0.0

    open_count = sum(1 for r in rows if r["status"] in ("active", "partially_resolved"))
    return open_count / total


# ---------------------------------------------------------------------------
# Aspect C — Provenance integrity — signal functions
# ---------------------------------------------------------------------------


def uncited_observation_rate(conn: sqlite3.Connection) -> float | _Unavailable:
    """Fraction of current observations (observation_factual /
    observation_predictive) with no ``evidence_id``.

    empty_denominator: UNAVAILABLE — a graph can't meaningfully have an
    "uncited rate" over zero observations.
    """
    rows = conn.execute(
        "SELECT evidence_id FROM nodes WHERE is_current = 1"
        " AND type IN ('observation_factual', 'observation_predictive')"
    ).fetchall()
    total = len(rows)
    if total == 0:
        return UNAVAILABLE

    uncited = sum(1 for r in rows if r["evidence_id"] is None)
    return uncited / total


def dangling_evidence_rate(conn: sqlite3.Connection) -> float | _Unavailable:
    """Fraction of current evidence nodes cited by NO current observation.

    The underlying query already exists as a raw COUNT (engram_stats.py
    "Dangling evidence" block); this turns it into a RATE (count / total
    current evidence nodes).

    empty_denominator: UNAVAILABLE — same 0/0 reasoning as
    uncited_observation_rate.
    """
    total = conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE type = 'evidence' AND is_current = 1"
    ).fetchone()["c"]
    if total == 0:
        return UNAVAILABLE

    dangling = conn.execute(
        """SELECT COUNT(*) as c FROM nodes n
           WHERE n.type = 'evidence' AND n.is_current = 1
             AND NOT EXISTS (
                 SELECT 1 FROM nodes o
                 WHERE o.evidence_id = n.id AND o.is_current = 1
             )"""
    ).fetchone()["c"]
    return dangling / total


# ---------------------------------------------------------------------------
# Aspect B stub signal function (design doc §7 PR2 slicing: PR2 ships an
# honest "not yet real" stub for aspect B rather than deferring the whole
# production CONFIG object — see health_engine.py's module docstring for the
# (a)/(b) choice this implements).
# ---------------------------------------------------------------------------


def not_yet_available_signal(conn: sqlite3.Connection) -> _Unavailable:
    """Aspect B (retrievability) is explicitly NOT this PR — PR3 builds the
    real recall_summary/keywords/embedding coverage + giant-component
    signals. This stub always reports UNAVAILABLE, so the real production
    CONFIG both (a) validates at the required weight-100 total (PR1's
    ``_check_weights_sum``) and (b) correctly reports the WHOLE engine as
    degraded (UNAVAILABLE propagates through aspect_geometric_mean_scoring
    and the top-level composition — see health_engine.compose_aspect_scores)
    until PR3 lands, rather than silently composing a 65/100-of-real-work
    score as if it were complete.
    """
    return UNAVAILABLE


# ---------------------------------------------------------------------------
# Shared aspect-level scoring function
# ---------------------------------------------------------------------------


def metric_goodness(rate: float) -> float:
    """Convert one metric's raw RATE (0=perfect .. 1=fully collapsed) into a
    0-100 "goodness" score, floored at 1 (mirrors the top-level composition's
    own floor — avoids the zero singularity while keeping catastrophic
    visibly catastrophic; design doc §3).

    Single source of truth for this transform — reviewer-fairy round 1 on
    PR2 found it independently re-implemented inline in
    ``health_engine.build_snapshot``'s per-metric display computation
    (``scores.metrics``) as well as here; extracted so the two call sites
    can't silently diverge if the transform or floor value ever changes.
    """
    return max(100.0 * (1.0 - rate), 1.0)


def aspect_geometric_mean_scoring(
    raw_by_metric: dict[str, float | _Unavailable],
) -> float | _Unavailable:
    """Combine one aspect's per-metric raw RATES into a single 0-100 aspect
    score.

    JUDGMENT CALL (flagged in the PR2 report — the design doc's §3 "weighted
    geometric mean ... floored at 1 ... degradation propagates" describes the
    TOP-level aspect-to-composed-score combination; it is silent on how an
    aspect's own multiple metrics combine into that aspect's single score).
    This applies the same shape one level down, per metric instead of per
    aspect, as the most defensible default consistent with §3's own stated
    principles (smooth, no cliffs, a collapsed metric should drag the aspect
    down, not get averaged away):

    1. Each metric's raw rate converts to a "goodness" score via
       ``metric_goodness`` (0=perfect .. 1=fully collapsed -> 100..1).
    2. Unweighted geometric mean across the aspect's metrics (equal weighting
       within an aspect — the design doc does not specify per-metric weights
       inside an aspect, only fixed weights ACROSS aspects; equal-weighting
       within an aspect is this PR's default, also flagged in the report).
    3. UNAVAILABLE in ANY of the aspect's metrics propagates to the whole
       aspect score (same "degradation propagates, never silently composed
       over a hole" rule as the top-level composition, design doc §3).

    Shared by every real AspectDef this PR builds (A, C) and reused as-is by
    the aspect-B stub (a single UNAVAILABLE-valued metric propagates through
    step 3 with no special-casing needed).
    """
    values = list(raw_by_metric.values())
    if not values or any(v is UNAVAILABLE for v in values):
        return UNAVAILABLE

    per_metric_good = [metric_goodness(v) for v in values]
    product = 1.0
    for good in per_metric_good:
        product *= good / 100.0
    n = len(per_metric_good)
    return 100.0 * (product ** (1.0 / n))
