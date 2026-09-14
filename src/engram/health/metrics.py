"""health.metrics — signal + scoring function catalogue for aspects A
(epistemic integrity), B (retrievability), and C (provenance integrity) of
the health-score redesign engine (engram-alpha#1991, PR2+PR3 of 4).

Design-of-record: engram-alpha#1991, issue comment 5372283060 (Borges,
2026-08-21, "Health-score redesign — DESIGN v2 (build basis)") §2 (aspects)
and §5 (engine contract).

Scope of this module: the actual signal functions (``Callable[[sqlite3.
Connection], float | _Unavailable]`` per health.config.MetricDef's
type) for aspect A's two metrics (engram-alpha#2905 PR-B unified the prior
three-metric stale_derivation_rate/taint_rate/open_contradiction_rate set
into pending_premise_rate/open_contradiction_rate), aspect B's four metrics
(PR3), and aspect C's two metrics, plus one shared aspect-level scoring
function used by every real AspectDef in health/engine.py.

Each signal function is SELF-CONTAINED: it takes only ``conn`` (per PR1's
fixed ``Callable[[sqlite3.Connection], float | _Unavailable]`` signature —
signal functions do not receive their own MetricDef, so the empty_denominator
policy each function implements below is *by construction* identical to the
policy declared on its corresponding MetricDef in health/engine.py, not
independently checked against it at runtime; validate_config only checks
that the declared policy is a real enum value, not that a signal_fn's
runtime behaviour matches it — see design doc §5 contract item 5).

SIGN CONVENTION — every raw signal in this catalogue returns its OWN LITERAL
MEANING, full stop: a ``*_rate`` name returns that rate (0.0 = none affected,
1.0 = fully affected) and a ``*_coverage``/``*_fraction`` name returns that
literal coverage/fraction (0.0 = none covered, 1.0 = fully covered).
Direction/polarity is NOT encoded in the sign of the raw value anywhere in
this module — it is declared exactly once per metric, on that metric's bound
``CurveSpec.monotone_direction`` (health/curves.py), via
health/model.py's ``CURVES``/``MetricBinding`` wiring: a "decreasing"
curve (``BADNESS_DEFAULT_CURVE``, bound as ``"badness_default"``) for the
five aspect A/C badness-rate metrics (higher raw value = worse), an
"increasing" curve (``GOODNESS_DEFAULT_CURVE``, bound as
``"goodness_default"``) for the four aspect B coverage-style metrics (higher
raw value = better). ``metric_goodness`` below still computes ``max(100 *
(1 - rate), 1)`` — that formula is unchanged and still describes what IT
does — but it (and ``aspect_geometric_mean_scoring``) is dead code from
health/engine.py's real-scoring-path perspective as of the curve-library
wiring (PR5; see tests/test_health_engine_curve_transition.py's own note);
the live path always goes through a metric's bound curve, never through
``metric_goodness`` directly.

Historical note (engram-alpha#1991 PR3 → engram-alpha#2518): aspect B's four
metrics were always natively coverage-style ("fraction WITH a recall_summary"
— HIGH is good), but at PR3-time the only scoring machinery available
(``metric_goodness``/``aspect_geometric_mean_scoring``) had no per-metric
polarity concept, only a single badness-rate assumption, and adding a
polarity field to the PR1-approved ``MetricDef`` schema or a second
aspect-scoring function was out of scope for that PR. So PR3 made each
aspect-B signal function return the coverage metric's COMPLEMENT (the
missing/uncovered/disconnected rate) instead, so it would compose correctly
through the shared badness-rate machinery of the time. Lei's ruling
(2026-09-03T23:22Z, charge-board record) reversed that stopgap: raw signal =
literal meaning of its own name; ALL direction, including polarity, lives in
the curve. By the time this ruling landed, ``CurveSpec.monotone_direction``
(PR4/PR5) already supported both directions as a tested, first-class
contract, so this flip needed no schema change at all — only one new
increasing curve (``GOODNESS_DEFAULT_CURVE``) plus rebinding the four
aspect-B ``MetricBinding``s from ``"badness_default"`` to
``"goodness_default"``.

House rule (mirrors engram_core.py / engram_lifecycle.py): access shared
state ONLY via ``import engram_core as core; core.NAME`` — never via
``from engram_core import NAME``. Unlike the ``engram_*``-prefixed family
modules, this import is done LAZILY, per-function, rather than once at
module top level — deliberately, not an oversight. The test-suite's
``fresh_server()`` fixture pattern (tests/test_nap_payload.py and siblings)
reloads ``engram_core`` per test via ``del sys.modules['engram_core']`` +
re-``import``, but its module-clearing predicate only matches ``"server"``
or a ``"engram_"`` prefix — ``health.metrics`` isn't swept by that
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

import json
import sqlite3

from .config import UNAVAILABLE, _Unavailable


# ---------------------------------------------------------------------------
# Aspect A — Epistemic integrity — signal functions
# ---------------------------------------------------------------------------


def pending_premise_rate(conn: sqlite3.Connection) -> float | _Unavailable:
    """Fraction of current claim-bearing nodes carrying an unresolved
    cascade marker from EITHER a retract OR a supersede event (i.e.
    currently blocked as a premise per the live cascade machine).

    Reads ``cascade_markers`` (root_op IN ('retract', 'supersede'),
    status='pending'), NOT ``metadata.tainted_by``/``stale_by``/
    ``stale_via`` — those metadata fields were retired by the cascade
    machine (commit 7f068db1, 2026-08-28) and no longer update on
    retract/supersede. See engram-alpha#1991 comment 5478591738 §1 for the
    measured defect this replaces.

    Unifies the prior separate ``stale_derivation_rate`` (supersede-only,
    derivations-only) and ``taint_rate`` (retract-only, claim-bearing-wide)
    metrics (engram-alpha#2905 PR-B): post-cascade-machine, a pending
    retract marker and a pending supersede marker do the same thing to a
    node's usability as a premise — block it — regardless of cause, so
    this is scored as one metric, not two.

    UNIT: the scored quantity is NODES with a pending marker (one per
    node, regardless of how many pending marker rows — of either kind —
    that node has), NOT marker rows — a node can be caught by more than
    one cascade_event, and a node caught by BOTH a retract and a supersede
    event still counts once. State this explicitly because the OLD
    formula shipped for months with the unit unstated and it was a real
    spec error then.

    Denominator is claim-bearing nodes (``engram_core.CLAIM_BEARING_TYPES``),
    not every current node — a definition/person/task/goal node is never
    claim-bearing and can't carry a premise-blocking marker in the
    epistemic sense. This is ``taint_rate``'s prior denominator, not
    ``stale_derivation_rate``'s narrower derivations-only one — the
    unified numerator can touch any claim-bearing node type, not
    derivations alone.

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

    pending = conn.execute(
        f"""SELECT COUNT(DISTINCT n.id) as c
            FROM nodes n
            JOIN cascade_markers m ON m.node_id = n.id
            WHERE n.is_current = 1 AND n.type IN ({placeholders})
              AND m.root_op IN ('retract', 'supersede') AND m.status = 'pending'""",
        claim_bearing_types,
    ).fetchone()["c"]
    return pending / total


def open_contradiction_rate(conn: sqlite3.Connection) -> float | _Unavailable:
    """Fraction of current contradiction nodes that are still OPEN
    (``status IN ('active', 'partially_resolved')``), i.e. not resolved.

    empty_denominator: UNAVAILABLE (CHANGED — engram-alpha#2520 round 2,
    fold #2: Kepler via Borges's colleague read of draft 2, addendum 2 on
    #1991, 2026-09-03). PREVIOUSLY this returned CLEAN (0.0) on zero
    current contradiction nodes, reasoning that a contradiction-free graph
    has genuinely 0% open contradictions (design doc §5 contract item 5's
    own worked example). That reasoning PREDATES the "hold out,
    renormalize, report" design ruling (Kepler, engram-alpha#2520 review
    comment, 2026-09-03) and, per Kepler, now FABRICATES a real number on
    every brand-new/tiny graph: a graph with literally zero contradiction
    NODES (not zero OPEN ones) hasn't demonstrated anything about its
    open-contradiction rate at all, "clean" or otherwise — there is
    nothing to report a rate over. This now matches pending_premise_rate's
    existing UNAVAILABLE-on-empty-population convention exactly (same
    aspect, same treatment; pending_premise_rate unifies the prior
    separate taint_rate/stale_derivation_rate siblings referenced here
    originally — see engram-alpha#2905 PR-B) — the prior asymmetry (this
    one epistemic_integrity metric alone returning a "clean" 0.0 for an
    empty population, its sibling(s) returning UNAVAILABLE for the same
    condition) was itself the inconsistency this fold closes.
    """
    rows = conn.execute(
        "SELECT status FROM nodes WHERE type = 'contradiction' AND is_current = 1"
    ).fetchall()
    total = len(rows)
    if total == 0:
        return UNAVAILABLE

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
# Aspect B — Retrievability — signal functions (engram-alpha#1991, PR3 of 4;
# polarity flipped to literal coverage in engram-alpha#2518)
#
# All four are scored over CURRENT claim-bearing nodes (engram_core.
# CLAIM_BEARING_TYPES) — design doc §2's "claim-bearing projection" framing:
# only claim-bearing content's retrievability matters for recall failures.
# All four now return their OWN LITERAL coverage/goodness fraction (0.0 =
# none covered/worst, 1.0 = fully covered/best) — see module docstring's
# "SIGN CONVENTION" note. Each is bound to the increasing "goodness_default"
# curve (health/model.py), not "badness_default".
# ---------------------------------------------------------------------------


def recall_summary_coverage(conn: sqlite3.Connection) -> float | _Unavailable:
    """Fraction of current claim-bearing nodes WITH a ``recall_summary``
    (literal coverage — see module docstring's sign-convention note; the
    name now agrees with the meaning: 1.0 means "all covered", 0.0 means
    "none covered").

    JUDGMENT CALL (flagged in the PR3 report): reads the ``recall_summary``
    column DIRECTLY (present iff non-NULL and non-whitespace, mirroring
    engram_core._validate_recall_fields's own ".strip()" absence check)
    rather than the ``nodes.summary_missing`` flag the design doc names for
    this metric ("recall_summary coverage (free from #1952's summary_missing
    flag)"). ``summary_missing`` is set at node-creation time (engram_derive.py
    / engram_epistemic.py / engram_observation.py, via
    engram_core._validate_recall_fields); whether a given backfill route
    (e.g. engram_recall_summaries.py's ``engram_set_recall_summaries``, the
    batch tool the sleep-cycle batch-summary-fairy uses to backfill
    recall_summary onto nodes created without one) re-clears that flag on
    write is a detail of that route's own implementation, not something this
    metric can assume stays fixed as the codebase evolves — the exact
    ``UPDATE`` this docstring originally cited as evidence changed under this
    PR while it was still stacked (Borges, PR3 colleague review, 2026-08-23,
    blocker 2: correct on this branch's own tip, already false on ``dev``).
    Reading the column's actual content sidesteps that entirely: it's correct
    regardless of whether ``summary_missing`` gets re-cleared on any given
    write path, now or later. That's the actual reason to prefer it over the
    flag, and it's the one reason that can't go stale.

    empty_denominator: UNAVAILABLE when zero current claim-bearing nodes.
    """
    import engram_core as core  # lazy — see module docstring

    claim_bearing_types = tuple(sorted(core.CLAIM_BEARING_TYPES))
    placeholders = ",".join("?" * len(claim_bearing_types))
    rows = conn.execute(
        f"SELECT recall_summary FROM nodes WHERE is_current = 1 AND type IN ({placeholders})",
        claim_bearing_types,
    ).fetchall()
    total = len(rows)
    if total == 0:
        return UNAVAILABLE

    missing = sum(
        1 for r in rows
        if r["recall_summary"] is None or not r["recall_summary"].strip()
    )
    return 1.0 - (missing / total)


def recall_keywords_coverage(conn: sqlite3.Connection) -> float | _Unavailable:
    """Fraction of current claim-bearing nodes WITH a non-empty
    ``recall_keywords`` list (literal coverage — see module docstring's
    sign-convention note; 1.0 = all covered, 0.0 = none covered).

    Reads ``recall_keywords`` the same way engram_core._reflect_keywords
    does: JSON-parse, and treat a missing/non-list/malformed value as absent
    rather than raising — reuses the existing read pattern (per the PR3 spec)
    instead of inventing a new parser.

    empty_denominator: UNAVAILABLE when zero current claim-bearing nodes.
    """
    import engram_core as core  # lazy — see module docstring

    claim_bearing_types = tuple(sorted(core.CLAIM_BEARING_TYPES))
    placeholders = ",".join("?" * len(claim_bearing_types))
    rows = conn.execute(
        f"SELECT recall_keywords FROM nodes WHERE is_current = 1 AND type IN ({placeholders})",
        claim_bearing_types,
    ).fetchall()
    total = len(rows)
    if total == 0:
        return UNAVAILABLE

    missing = 0
    for r in rows:
        raw = r["recall_keywords"]
        if not raw:
            missing += 1
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            missing += 1
            continue
        if not isinstance(parsed, list) or len(parsed) == 0:
            missing += 1
    return 1.0 - (missing / total)


def embedding_coverage(conn: sqlite3.Connection) -> float | _Unavailable:
    """Fraction of current claim-bearing nodes with a NON-NULL ``embedding``
    column (literal coverage — see module docstring's sign-convention note;
    1.0 = all covered, 0.0 = none covered). Direct SQL, no JSON parsing
    needed (the column itself is either NULL or a populated vector, never
    inspected for shape here).

    Test-fixture note (flagged in the PR3 report, per the build spec's own
    warning): this repo's test suite runs with ``ENGRAM_NO_EMBEDDINGS=1``
    (see ``fresh_server()`` in the sibling checkpoint-wiring test file),
    under which embeddings are never generated — every node created through
    the normal write path has ``embedding IS NULL``. A test exercising the
    "has coverage" branch of this metric must set the ``embedding`` column
    directly in its own fixture setup — AND, since the embedder-availability
    gate below was added, also patch ``core._embedder.is_available()`` to
    True (e.g. ``mock.patch("engram_core._embedder.is_available", ...)``),
    or it will hit that gate and return UNAVAILABLE before ever reaching the
    per-node count, regardless of what the ``embedding`` column holds. This
    now fails loudly (an assertion mismatch on UNAVAILABLE), not silently —
    but the fixture still needs both pieces to exercise the "has coverage"
    branch at all.

    empty_denominator: UNAVAILABLE when zero current claim-bearing nodes.

    UNAVAILABLE ALSO when the embedder is structurally unavailable in this
    install (``ENGRAM_NO_EMBEDDINGS=1``, or ``sentence-transformers`` not
    installed, or embeddings disabled in config) — checked via the same
    ``core._embedder.is_available() and core._get_embedding_config().get(
    "enabled", True)`` idiom used at engram_core.py:3497/4836/5053 and
    engram_query.py:1638. Structurally-NULL embeddings in that state are a
    CONFIGURATION fact, not a HEALTH fact: scoring it as 1.0 (maximally bad)
    makes a graph's composed score swing ~32 points purely on whether a
    Python package happens to be installed — permanent, unfixable by the
    operator, and exactly the "UNAVAILABLE ≠ 0 + degraded rendering" failure
    mode the design contract calls out (Borges, PR3 colleague review,
    2026-08-23, blocker 1).
    """
    import engram_core as core  # lazy — see module docstring

    if not (core._embedder.is_available() and core._get_embedding_config().get("enabled", True)):
        return UNAVAILABLE

    claim_bearing_types = tuple(sorted(core.CLAIM_BEARING_TYPES))
    placeholders = ",".join("?" * len(claim_bearing_types))
    total = conn.execute(
        f"SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND type IN ({placeholders})",
        claim_bearing_types,
    ).fetchone()["c"]
    if total == 0:
        return UNAVAILABLE

    missing = conn.execute(
        f"SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND type IN ({placeholders})"
        " AND embedding IS NULL",
        claim_bearing_types,
    ).fetchone()["c"]
    return 1.0 - (missing / total)


def giant_component_fraction(conn: sqlite3.Connection) -> float | _Unavailable:
    """Giant-component fraction (literal — see module docstring's
    sign-convention note) of the INDUCED subgraph on current claim-bearing
    nodes only: the largest connected component's size divided by the total
    claim-bearing population. 1.0 = fully connected (best), 0.0 = every
    claim-bearing node its own singleton component (worst).

    "Induced subgraph" (design doc §2's "claim-bearing projection"): an edge
    counts toward connectivity only if BOTH its source and target are
    current claim-bearing nodes. An edge touching a non-claim-bearing node
    (e.g. an ``evidence`` or ``definition`` node) never bridges two
    claim-bearing components through it.

    Adapted from engram_stats.py's "Connected components (undirected
    reachability)" block (plain adjacency-dict + BFS queue, no networkx/
    numpy — this codebase has neither as a dependency and this PR does not
    add one) — NOT copied verbatim: that block computes over ALL current
    nodes and ALL edges; this restricts both to the claim-bearing population.

    empty_denominator: UNAVAILABLE when zero current claim-bearing nodes.
    """
    import engram_core as core  # lazy — see module docstring

    claim_bearing_types = tuple(sorted(core.CLAIM_BEARING_TYPES))
    placeholders = ",".join("?" * len(claim_bearing_types))
    node_rows = conn.execute(
        f"SELECT id FROM nodes WHERE is_current = 1 AND type IN ({placeholders})",
        claim_bearing_types,
    ).fetchall()
    claim_bearing_ids = {r["id"] for r in node_rows}
    total = len(claim_bearing_ids)
    if total == 0:
        return UNAVAILABLE

    edge_rows = conn.execute("SELECT source_id, target_id FROM edges").fetchall()
    adj: dict = {}
    for e in edge_rows:
        s, t = e["source_id"], e["target_id"]
        if s in claim_bearing_ids and t in claim_bearing_ids:
            adj.setdefault(s, set()).add(t)
            adj.setdefault(t, set()).add(s)

    visited: set = set()
    largest = 0
    for nid in claim_bearing_ids:
        if nid in visited:
            continue
        size = 0
        queue = [nid]
        visited.add(nid)
        while queue:
            c = queue.pop(0)
            size += 1
            for nb in adj.get(c, ()):
                if nb not in visited:
                    visited.add(nb)
                    queue.append(nb)
        largest = max(largest, size)

    return largest / total


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
    ``health.engine.build_snapshot``'s per-metric display computation
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
