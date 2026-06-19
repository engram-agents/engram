"""engram_stats — family H: stats + diagnose impls for the ENGRAM MCP server.

Extracted from server.py as part of #872 wave 7.

Family H covers the two read-only reporting tools:
  - ``engram_stats``: time-windowed graph statistics (structure, edges,
    confidence distribution, open questions/predictions, reasoning breakdown,
    weakest nodes, memory tiers).
  - ``engram_diagnose``: seven-dimension quantitative health audit (structure,
    epistemic, memory, provenance, experience, calibration, read-tool
    contention) plus a 0-100 health score.

NOTE: module name ``engram_stats`` collides with the ``@mcp.tool``-decorated
``engram_stats`` function in server.py.  The wave convention therefore mandates
the alias form: ``import engram_stats as _stats_mod``.

House rules (wave pattern):
  - Shared state ONLY via ``import engram_core as core`` + call-time ``core.X``.
  - Never import from server.py (acyclic: server -> family -> core).
  - Stateless beyond constants.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict as _ddict
from datetime import datetime, timedelta, timezone
from typing import Optional

import engram_core as core


# ---------------------------------------------------------------------------
# _compute_confidence_distribution — shared helper used by both _stats_impl
# and _diagnose_impl
# ---------------------------------------------------------------------------

# Module-level SSoT for the node types that carry a calibratable confidence
# distribution. Promoted from a function-local tuple so other surfaces (the viz
# /api/schema endpoint, its CI drift gate) can import the canonical set instead
# of replicating it (#1225).
_CONFIDENCE_BEARING_TYPES = (
    "observation_factual",
    "derivation",
    "conjecture",
    "lesson",
    "axiom",
    "definition",
)


def _compute_confidence_distribution(
    conn: sqlite3.Connection,
    created_at_filter: str,
    filter_args: list,
) -> dict:
    """Compute the confidence distribution section for engram_stats.

    Returns a dict with by_type, by_quote_type, by_reasoning_type,
    by_source_class sub-keys. Empty buckets are omitted. Quantiles are
    computed via Python after fetching confidence values (simpler than
    SQLite window functions, and the dataset is small enough that the
    extra round-trip is negligible).

    Status filter applied: is_current=1 AND (status IS NULL OR status != 'retracted').
    The created_at_filter / filter_args are passed through from the caller
    to respect the mode window.
    """
    # Common status + current filter
    _base_filter = (
        " AND is_current = 1"
        " AND (status IS NULL OR status != 'retracted')"
    )

    # ── by_type: full quantile stats ──────────────────────────────────────
    # _CONFIDENCE_BEARING_TYPES is now a module-level constant (promoted #1225).
    by_type: dict = {}
    for ntype in _CONFIDENCE_BEARING_TYPES:
        rows = conn.execute(
            "SELECT confidence FROM nodes WHERE type = ?"
            + _base_filter
            + created_at_filter
            + " AND confidence IS NOT NULL ORDER BY confidence ASC",
            [ntype] + filter_args,
        ).fetchall()
        confidences = [r["confidence"] for r in rows]
        if not confidences:
            continue
        n = len(confidences)
        mean_val = round(sum(confidences) / n, 3)
        by_type[ntype] = {
            "n": n,
            "mean": mean_val,
            "p25": round(_percentile(confidences, 25), 2),
            "p50": round(_percentile(confidences, 50), 2),
            "p75": round(_percentile(confidences, 75), 2),
            "p90": round(_percentile(confidences, 90), 2),
            "p95": round(_percentile(confidences, 95), 2),
        }

    # ── by_quote_type: mean only, restricted to observation_factual ────────
    by_quote_type: dict = {}
    for row in conn.execute(
        "SELECT quote_type, COUNT(*) as n, AVG(confidence) as mean_conf"
        " FROM nodes WHERE type = 'observation_factual'"
        + _base_filter
        + created_at_filter
        + " AND confidence IS NOT NULL AND quote_type IS NOT NULL"
        " GROUP BY quote_type",
        filter_args,
    ).fetchall():
        qt = row["quote_type"]
        if not qt:
            continue
        by_quote_type[qt] = {
            "n": row["n"],
            "mean": round(row["mean_conf"], 3),
        }

    # ── by_reasoning_type: mean only, restricted to derivation ─────────────
    # reasoning_type lives in metadata JSON; fetch all derivations and
    # aggregate in Python to avoid fragile json_extract across SQLite versions.
    by_reasoning_type: dict = {}
    deriv_rows = conn.execute(
        "SELECT confidence, metadata FROM nodes WHERE type = 'derivation'"
        + _base_filter
        + created_at_filter
        + " AND confidence IS NOT NULL AND metadata IS NOT NULL",
        filter_args,
    ).fetchall()
    _rt_buckets: dict = {}  # reasoning_type → list of confidence values
    for row in deriv_rows:
        try:
            meta = json.loads(row["metadata"])
            rtype = meta.get("reasoning_type")
        except (json.JSONDecodeError, TypeError):
            rtype = None
        if not rtype:
            rtype = "legacy_untyped"
        _rt_buckets.setdefault(rtype, []).append(row["confidence"])
    for rtype, vals in _rt_buckets.items():
        by_reasoning_type[rtype] = {
            "n": len(vals),
            "mean": round(sum(vals) / len(vals), 3),
        }

    # ── by_source_class: mean only, restricted to observation_factual ──────
    # source_class lives in metadata JSON; aggregate in Python.
    by_source_class: dict = {}
    obs_rows = conn.execute(
        "SELECT confidence, metadata FROM nodes WHERE type = 'observation_factual'"
        + _base_filter
        + created_at_filter
        + " AND confidence IS NOT NULL AND metadata IS NOT NULL",
        filter_args,
    ).fetchall()
    _sc_buckets: dict = {}
    for row in obs_rows:
        try:
            meta = json.loads(row["metadata"])
            sc = meta.get("source_class", "external")
        except (json.JSONDecodeError, TypeError):
            sc = "external"
        _sc_buckets.setdefault(sc, []).append(row["confidence"])
    for sc, vals in _sc_buckets.items():
        by_source_class[sc] = {
            "n": len(vals),
            "mean": round(sum(vals) / len(vals), 3),
        }

    return {
        "by_type": by_type,
        "by_quote_type": by_quote_type,
        "by_reasoning_type": by_reasoning_type,
        "by_source_class": by_source_class,
    }


def _percentile(sorted_vals: list, p: float) -> float:
    """Compute the p-th percentile of a pre-sorted list of floats.

    Uses linear interpolation between the two nearest ranks (same as
    numpy's `percentile` with method='linear'). If the list has one
    element, returns that element regardless of p.

    Tie handling: when multiple values are identical at a percentile
    boundary, interpolation still applies to the two boundary values,
    which happen to be equal — so the result is unambiguous.
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    # Map p ∈ [0, 100] to an index in [0, n-1]
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_vals[-1]
    frac = idx - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


# ---------------------------------------------------------------------------
# _stats_impl — impl for engram_stats
# ---------------------------------------------------------------------------



def _compute_health_score(conn) -> float:
    """THE health-score formula — single source of truth (charter §6 fold).

    Self-contained: runs all of its own queries so both consuming surfaces
    (engram_stats sections=["health_score"] and engram_diagnose) report the
    SAME number from the SAME implementation. The cycle's frozen stability
    metric reads this function; tests/test_health_score_pinning.py pins it
    in both probe directions (D3), and the stats↔diagnose parity test pins
    the single-source property itself.

    Formula (frozen for the duration of the epistemic-foundations cycle):
    100 minus capped penalties (DAG violations, tainted, stale, orphans,
    embedding-coverage gap, excess retraction rate, excess uncited-obs
    ratio) plus the question-resolution bonus, clamped to [0, 100].
    """
    score = 100.0

    # DAG violations (source created_at < target created_at, non-exempt)
    _exempt = ",".join("?" * len(core.DAG_EXEMPT_RELATIONS))
    dag_violations = conn.execute(
        f"""SELECT COUNT(*) as c FROM edges e
            JOIN nodes s ON e.source_id = s.id
            JOIN nodes t ON e.target_id = t.id
            WHERE s.created_at < t.created_at
              AND e.relation NOT IN ({_exempt})""",
        tuple(core.DAG_EXEMPT_RELATIONS),
    ).fetchone()["c"]
    score -= min(dag_violations * 5, 20)

    # Tainted nodes (downstream of retracted premises)
    tainted = conn.execute(
        "SELECT COUNT(*) as c FROM nodes"
        " WHERE is_current = 1 AND metadata LIKE '%\"tainted_by\"%'"
    ).fetchone()["c"]
    score -= min(tainted * 3, 15)

    # Stale nodes (downstream of superseded premises)
    stale = conn.execute(
        "SELECT COUNT(*) as c FROM nodes"
        " WHERE is_current = 1 AND metadata LIKE '%\"stale_by\": [%'"
    ).fetchone()["c"]
    score -= min(stale * 2, 10)

    # Orphan nodes (no edges at all)
    orphans = conn.execute("""
        SELECT COUNT(*) as c FROM nodes n
        WHERE n.is_current = 1
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.source_id = n.id)
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.target_id = n.id)
    """).fetchone()["c"]
    score -= min(orphans * 1, 10)

    # Embedding coverage gap
    emb_row = conn.execute(
        "SELECT COUNT(*) as total,"
        " SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) as has_emb"
        " FROM nodes WHERE is_current = 1"
    ).fetchone()
    emb_total = emb_row["total"] or 0
    emb_has = emb_row["has_emb"] or 0
    emb_pct = (100 * emb_has / emb_total) if emb_total else 100
    score -= min((100 - emb_pct) * 0.1, 10)

    # Excess retraction rate (>10% of total nodes)
    all_row = conn.execute(
        "SELECT COUNT(*) as total,"
        " SUM(CASE WHEN status = 'retracted' THEN 1 ELSE 0 END) as retracted"
        " FROM nodes"
    ).fetchone()
    all_total = all_row["total"] or 0
    all_retracted = all_row["retracted"] or 0
    if all_total > 0:
        retract_rate = all_retracted / all_total
        if retract_rate > 0.1:
            score -= min((retract_rate - 0.1) * 100, 15)

    # Excess uncited observations (>50% of observations)
    obs_row = conn.execute(
        "SELECT COUNT(*) as total,"
        " SUM(CASE WHEN evidence_id IS NULL THEN 1 ELSE 0 END) as uncited"
        " FROM nodes WHERE is_current = 1"
        " AND type IN ('observation_factual', 'observation_predictive')"
    ).fetchone()
    obs_total = obs_row["total"] or 0
    obs_uncited = obs_row["uncited"] or 0
    if obs_total > 0 and obs_uncited / obs_total > 0.5:
        score -= min((obs_uncited / obs_total - 0.5) * 20, 10)

    # Bonus: active resolution work (+5 if >50% questions resolved)
    qu_row = conn.execute(
        "SELECT COUNT(*) as total,"
        " SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) as resolved"
        " FROM nodes WHERE type = 'question'"
    ).fetchone()
    qu_total = qu_row["total"] or 0
    qu_resolved = qu_row["resolved"] or 0
    if qu_total > 0 and qu_resolved / qu_total > 0.5:
        score = min(score + 5, 100)

    return round(max(score, 0), 1)


def _stats_impl(mode: str = "all", sections=None) -> str:
    """Impl for engram_stats — callable with named kwargs for in-server callers.

    ``mode`` and ``sections`` are the validated params from the wrapper's parse
    layer.  The wrapper handles JSON decode + field validation; this function
    handles the business logic.
    """
    _VALID_SECTIONS = {
        "structure", "edges", "confidence", "open_questions",
        "open_predictions", "reasoning_breakdown", "weakest_nodes",
        "health_score", "memory",
    }
    if sections is not None:
        unknown = [s for s in sections if s not in _VALID_SECTIONS]
        if unknown:
            logging.getLogger(__name__).warning(
                "engram_stats: unknown section name(s) ignored: %s", unknown
            )
        wanted = {s for s in sections if s in _VALID_SECTIONS}
    else:
        wanted = None  # None = all sections

    def _want(section: str) -> bool:
        return wanted is None or section in wanted

    # ── Build window filter ────────────────────────────────────────────────
    # Turn-unit → wall-clock approximation: 1 turn ≈ 24h (dream cycle advances ~daily).
    # Accurate in steady-state; drifts during high/low-velocity periods. Future: record
    # last_turn_advance in config.json and use that boundary directly.
    _MODE_HOURS = {"1-turn": 24, "7-turn": 168, "30-turn": 720}
    now_utc = datetime.now(timezone.utc)
    window_meta: Optional[dict] = None
    created_at_filter = ""   # empty string = no filter (mode="all")
    filter_args: list = []

    if mode != "all":
        hours = _MODE_HOURS[mode]
        cutoff_dt = now_utc - timedelta(hours=hours)
        cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        created_at_filter = " AND created_at >= ?"
        filter_args = [cutoff_iso]
        window_meta = {
            "mode": mode,
            "approx_hours": hours,
            "cutoff_iso": cutoff_iso,
        }

    conn = core._get_db()
    try:
        result: dict = {}

        # Attach window metadata first so callers see it regardless of sections.
        if window_meta is not None:
            result["window"] = window_meta

        # ── structure ──────────────────────────────────────────────────────
        if _want("structure"):
            type_counts = {}
            for row in conn.execute(
                "SELECT type, COUNT(*) as c, SUM(is_current) as current_c"
                " FROM nodes WHERE 1=1" + created_at_filter + " GROUP BY type",
                filter_args,
            ).fetchall():
                type_counts[row["type"]] = {
                    "total": row["c"],
                    "current": row["current_c"],
                }
            result["node_counts_by_type"] = type_counts

        # ── edges ──────────────────────────────────────────────────────────
        if _want("edges"):
            if mode == "all":
                edge_rows = conn.execute(
                    "SELECT relation, COUNT(*) as c FROM edges GROUP BY relation"
                ).fetchall()
            else:
                # edges table has its own created_at column — use it directly.
                edge_rows = conn.execute(
                    "SELECT relation, COUNT(*) as c FROM edges"
                    " WHERE created_at >= ? GROUP BY relation",
                    filter_args,
                ).fetchall()
            edge_counts = {row["relation"]: row["c"] for row in edge_rows}
            result["edge_counts_by_relation"] = edge_counts

        # ── open_questions ─────────────────────────────────────────────────
        if _want("open_questions"):
            # Open / partially-resolved questions: count + top 5 most-recent
            # highlights. The full enumeration belongs to engram_reflect; bulk-
            # listing all of them here ballooned the response past the MCP
            # token budget when the graph accumulated >100 open questions
            # (ob_NNNN cohort, observed 2026-05-03 — 111K bytes, harness fell
            # back to file redirection). Pattern matches weakest_nodes (top 5).
            open_question_count = conn.execute(
                "SELECT COUNT(*) as c FROM nodes WHERE type = 'question'"
                " AND status IN ('open', 'partially_resolved') AND is_current = 1"
                + created_at_filter,
                filter_args,
            ).fetchone()["c"]
            open_questions = conn.execute(
                "SELECT id, claim, status, question_category, question_lacks"
                " FROM nodes WHERE type = 'question'"
                " AND status IN ('open', 'partially_resolved') AND is_current = 1"
                + created_at_filter
                + " ORDER BY created_at DESC LIMIT 5",
                filter_args,
            ).fetchall()
            result["open_questions_count"] = open_question_count
            result["open_questions_recent"] = [
                {k: v for k, v in {
                    "id": q["id"], "question": q["claim"], "status": q["status"],
                    "category": q["question_category"], "lacks": q["question_lacks"],
                }.items() if v is not None}
                for q in open_questions
            ]

        # ── open_predictions ───────────────────────────────────────────────
        if _want("open_predictions"):
            # Open / partially-resolved predictions: count + top 5 most-recent.
            open_prediction_count = conn.execute(
                "SELECT COUNT(*) as c FROM nodes WHERE type = 'prediction'"
                " AND status IN ('open', 'partially_resolved') AND is_current = 1"
                + created_at_filter,
                filter_args,
            ).fetchone()["c"]
            open_predictions = conn.execute(
                "SELECT id, predicted_event, resolution_timeframe, status"
                " FROM nodes WHERE type = 'prediction'"
                " AND status IN ('open', 'partially_resolved') AND is_current = 1"
                + created_at_filter
                + " ORDER BY created_at DESC LIMIT 5",
                filter_args,
            ).fetchall()
            result["open_predictions_count"] = open_prediction_count
            result["open_predictions_recent"] = [
                {
                    "id": p["id"],
                    "event": p["predicted_event"],
                    "timeframe": p["resolution_timeframe"],
                    "status": p["status"],
                }
                for p in open_predictions
            ]

        # ── reasoning_breakdown ────────────────────────────────────────────
        if _want("reasoning_breakdown"):
            reasoning_breakdown: dict = {}
            derivations = conn.execute(
                "SELECT metadata FROM nodes WHERE type = 'derivation'"
                " AND is_current = 1 AND metadata IS NOT NULL"
                + created_at_filter,
                filter_args,
            ).fetchall()
            for d in derivations:
                try:
                    meta = json.loads(d["metadata"])
                    rtype = meta.get("reasoning_type", "legacy_untyped")
                except (json.JSONDecodeError, TypeError):
                    rtype = "legacy_untyped"
                reasoning_breakdown[rtype] = reasoning_breakdown.get(rtype, 0) + 1
            result["reasoning_type_breakdown"] = reasoning_breakdown

        # ── weakest_nodes ──────────────────────────────────────────────────
        if _want("weakest_nodes"):
            weak_nodes = conn.execute(
                "SELECT id, type, claim, confidence FROM nodes"
                " WHERE is_current = 1 AND confidence IS NOT NULL AND type != 'evidence'"
                + created_at_filter
                + " ORDER BY confidence ASC LIMIT 5",
                filter_args,
            ).fetchall()
            result["weakest_nodes"] = [
                {
                    "id": w["id"],
                    "type": w["type"],
                    "claim": w["claim"],
                    "confidence": w["confidence"],
                }
                for w in weak_nodes
            ]

        # ── memory ─────────────────────────────────────────────────────────
        if _want("memory"):
            result["memory"] = {
                "current_turn": core._get_current_turn(),
                "tier1_threshold": core._get_tier_threshold(conn, 1),
                "tier2_threshold": core._get_tier_threshold(conn, 2),
            }

        # ── confidence ─────────────────────────────────────────────────────
        if _want("confidence"):
            result["confidence"] = _compute_confidence_distribution(
                conn, created_at_filter, filter_args
            )

        # ── health_score ────────────────────────────────────────────────────
        # Self-contained computation: all queries run inline so this section
        # is correct whether or not "structure" was also requested.
        # ── health_score ────────────────────────────────────────────────────
        # Single source of truth: the shared helper (charter §6).
        if _want("health_score"):
            result["health_score"] = _compute_health_score(conn)

        banner = core._walguard_degraded_banner()
        if banner:
            result["_walguard_warning"] = banner
        return json.dumps(result)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _diagnose_impl — impl for engram_diagnose
# ---------------------------------------------------------------------------


def _diagnose_impl() -> str:
    """Impl for engram_diagnose — callable for in-server callers.

    Side-effect-free: no memory refresh, no turn advance, no state mutation.
    """
    conn = core._get_db()
    try:
        metrics: dict = {}

        # ── Graph Structure ──────────────────────────────────────────────
        structure: dict = {}

        # Node counts by type and currency
        type_rows = conn.execute(
            """SELECT type,
                      COUNT(*) as total,
                      SUM(is_current) as current_count,
                      SUM(CASE WHEN is_current = 0 THEN 1 ELSE 0 END) as superseded_count
               FROM nodes GROUP BY type"""
        ).fetchall()
        type_counts = {}
        total_nodes = 0
        total_current = 0
        for r in type_rows:
            type_counts[r["type"]] = {
                "total": r["total"],
                "current": r["current_count"],
                "superseded": r["superseded_count"],
            }
            total_nodes += r["total"]
            total_current += r["current_count"]
        structure["node_counts"] = type_counts
        structure["total_nodes"] = total_nodes
        structure["total_current"] = total_current

        # Edge counts by relation
        edge_rows = conn.execute(
            "SELECT relation, COUNT(*) as c FROM edges GROUP BY relation"
        ).fetchall()
        edge_counts = {r["relation"]: r["c"] for r in edge_rows}
        total_edges = sum(edge_counts.values())
        structure["edge_counts"] = edge_counts
        structure["total_edges"] = total_edges

        # DAG violations
        exempt_placeholders = ",".join("?" * len(core.DAG_EXEMPT_RELATIONS))
        dag_violations = conn.execute(
            f"""SELECT COUNT(*) as c FROM edges e
                JOIN nodes s ON e.source_id = s.id
                JOIN nodes t ON e.target_id = t.id
                WHERE s.created_at < t.created_at
                  AND e.relation NOT IN ({exempt_placeholders})""",
            tuple(core.DAG_EXEMPT_RELATIONS),
        ).fetchone()["c"]
        structure["dag_violations"] = dag_violations

        # Orphan nodes (no edges at all)
        orphans = conn.execute("""
            SELECT COUNT(*) as c FROM nodes n
            WHERE n.is_current = 1
              AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.source_id = n.id)
              AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.target_id = n.id)
        """).fetchone()["c"]
        structure["orphan_nodes"] = orphans

        # Average in/out degree (current nodes only)
        if total_current > 0:
            out_degree = conn.execute("""
                SELECT AVG(deg) as avg_deg FROM (
                    SELECT COUNT(*) as deg FROM edges e
                    JOIN nodes n ON e.source_id = n.id
                    WHERE n.is_current = 1
                    GROUP BY e.source_id
                )
            """).fetchone()["avg_deg"] or 0
            in_degree = conn.execute("""
                SELECT AVG(deg) as avg_deg FROM (
                    SELECT COUNT(*) as deg FROM edges e
                    JOIN nodes n ON e.target_id = n.id
                    WHERE n.is_current = 1
                    GROUP BY e.target_id
                )
            """).fetchone()["avg_deg"] or 0
            structure["avg_out_degree"] = round(out_degree, 2)
            structure["avg_in_degree"] = round(in_degree, 2)
        else:
            structure["avg_out_degree"] = 0
            structure["avg_in_degree"] = 0

        # Dangling evidence (evidence with no observations)
        dangling_ev = conn.execute("""
            SELECT COUNT(*) as c FROM nodes n
            WHERE n.type = 'evidence' AND n.is_current = 1
              AND NOT EXISTS (
                  SELECT 1 FROM nodes o
                  WHERE o.evidence_id = n.id AND o.is_current = 1
              )
        """).fetchone()["c"]
        structure["dangling_evidence"] = dangling_ev

        metrics["structure"] = structure

        # ── Epistemic Health ─────────────────────────────────────────────
        epistemic: dict = {}

        # Confidence distribution (buckets)
        conf_buckets = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0,
                        "0.6-0.8": 0, "0.8-1.0": 0}
        conf_rows = conn.execute(
            """SELECT confidence FROM nodes
               WHERE is_current = 1 AND confidence IS NOT NULL"""
        ).fetchall()
        for cr in conf_rows:
            c = cr["confidence"]
            if c < 0.2:
                conf_buckets["0.0-0.2"] += 1
            elif c < 0.4:
                conf_buckets["0.2-0.4"] += 1
            elif c < 0.6:
                conf_buckets["0.4-0.6"] += 1
            elif c < 0.8:
                conf_buckets["0.6-0.8"] += 1
            else:
                conf_buckets["0.8-1.0"] += 1
        epistemic["confidence_distribution"] = conf_buckets

        # Mean and median confidence
        if conf_rows:
            confs = sorted(cr["confidence"] for cr in conf_rows)
            epistemic["confidence_mean"] = round(sum(confs) / len(confs), 3)
            mid = len(confs) // 2
            epistemic["confidence_median"] = confs[mid] if len(confs) % 2 else round((confs[mid - 1] + confs[mid]) / 2, 3)
        else:
            epistemic["confidence_mean"] = 0
            epistemic["confidence_median"] = 0

        # Weakly grounded (conf < 0.5, claim-bearing types)
        weakly_grounded = conn.execute(
            """SELECT COUNT(*) as c FROM nodes
               WHERE is_current = 1 AND confidence IS NOT NULL
               AND confidence < 0.5
               AND type NOT IN ('evidence', 'contradiction', 'question',
                                'definition', 'goal', 'goal_tension',
                                'feeling_report', 'prediction')"""
        ).fetchone()["c"]
        epistemic["weakly_grounded"] = weakly_grounded

        # Single-source derivations
        single_source_dvs = conn.execute("""
            SELECT COUNT(*) as c FROM nodes n
            WHERE n.type = 'derivation' AND n.is_current = 1
              AND (SELECT COUNT(*) FROM edges e
                   WHERE e.source_id = n.id AND e.relation = 'derives_from') <= 1
        """).fetchone()["c"]
        epistemic["single_source_derivations"] = single_source_dvs

        # Tainted nodes
        tainted_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND metadata LIKE '%\"tainted_by\"%'"
        ).fetchone()["c"]
        epistemic["tainted_nodes"] = tainted_count

        # Stale nodes
        stale_count = conn.execute(
            # Anchor to JSON list opening — metadata written with default json.dumps separators.
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND metadata LIKE '%\"stale_by\": [%'"
        ).fetchone()["c"]
        epistemic["stale_nodes"] = stale_count

        # Support-lost cornerstones/lessons (§2.3)
        support_lost_count = conn.execute(
            # Anchor to serialized boolean — metadata written with default json.dumps separators.
            """SELECT COUNT(*) as c FROM nodes
               WHERE is_current = 1
                 AND type IN ('cornerstone', 'lesson')
                 AND metadata LIKE '%\"support_lost\": true%'"""
        ).fetchone()["c"]
        epistemic["support_lost_nodes"] = support_lost_count

        # Retracted nodes
        retracted_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE status = 'retracted'"
        ).fetchone()["c"]
        epistemic["retracted_nodes"] = retracted_count

        # Uncited observations (not referenced by any derivation)
        uncited = conn.execute("""
            SELECT COUNT(*) as c FROM nodes n
            WHERE n.type IN ('observation_factual', 'observation_predictive')
              AND n.is_current = 1
              AND NOT EXISTS (
                  SELECT 1 FROM edges e
                  WHERE e.target_id = n.id
                    AND e.relation IN ('derives_from', 'supported_by')
                    AND e.source_id IN (SELECT id FROM nodes WHERE type IN ('derivation', 'theory'))
              )
              AND NOT EXISTS (
                  SELECT 1 FROM edges e
                  WHERE e.target_id = n.id AND e.relation = 'supported_by'
                    AND e.source_id IN (SELECT id FROM nodes WHERE type = 'prediction')
              )
        """).fetchone()["c"]
        epistemic["uncited_observations"] = uncited

        # Resolution rates
        def _resolution_rate(node_type, resolved_statuses=("resolved",)):
            total = conn.execute(
                "SELECT COUNT(*) as c FROM nodes WHERE type = ?", (node_type,)
            ).fetchone()["c"]
            if total == 0:
                return {"total": 0, "resolved": 0, "rate": 0}
            resolved = conn.execute(
                f"SELECT COUNT(*) as c FROM nodes WHERE type = ? AND status IN ({','.join('?' * len(resolved_statuses))})",
                (node_type, *resolved_statuses),
            ).fetchone()["c"]
            return {"total": total, "resolved": resolved,
                    "rate": round(resolved / total, 3)}

        epistemic["contradiction_resolution"] = _resolution_rate(
            "contradiction", ("resolved", "partially_resolved"))
        epistemic["question_resolution"] = _resolution_rate(
            "question", ("resolved", "partially_resolved"))
        epistemic["conjecture_resolution"] = _resolution_rate(
            "conjecture", ("resolved", "supported", "refuted"))
        epistemic["prediction_resolution"] = _resolution_rate(
            "prediction", ("confirmed", "disconfirmed", "resolved", "partially_resolved"))

        # Reasoning type breakdown for derivations
        reasoning_breakdown = {}
        dvs = conn.execute(
            "SELECT metadata FROM nodes WHERE type = 'derivation' AND is_current = 1 AND metadata IS NOT NULL"
        ).fetchall()
        for d in dvs:
            try:
                meta = json.loads(d["metadata"])
                rtype = meta.get("reasoning_type", "legacy_untyped")
            except (json.JSONDecodeError, TypeError):
                rtype = "legacy_untyped"
            reasoning_breakdown[rtype] = reasoning_breakdown.get(rtype, 0) + 1
        epistemic["reasoning_types"] = reasoning_breakdown

        metrics["epistemic"] = epistemic

        # ── Memory Health ────────────────────────────────────────────────
        memory: dict = {}
        current_turn = core._get_current_turn()
        memory["current_turn"] = current_turn

        t1 = core._get_tier_threshold(conn, 1)
        t2 = core._get_tier_threshold(conn, 2)
        tier1_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND COALESCE(importance_score, 0) >= ?",
            (t1,),
        ).fetchone()["c"] if t1 > 0 else total_current
        tier2_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND COALESCE(importance_score, 0) >= ?",
            (t2,),
        ).fetchone()["c"] if t2 > 0 else total_current
        memory["tiers"] = {
            "tier1_working": tier1_count,
            "tier1_threshold": round(t1, 4),
            "tier2_searchable": tier2_count,
            "tier2_threshold": round(t2, 4),
            "tier3_total": total_current,
        }

        # Importance score distribution
        imp_rows = conn.execute(
            "SELECT importance_score FROM nodes WHERE is_current = 1 AND importance_score IS NOT NULL"
        ).fetchall()
        if imp_rows:
            scores = sorted(r["importance_score"] for r in imp_rows)
            n = len(scores)
            memory["importance_distribution"] = {
                "min": round(scores[0], 4),
                "p25": round(scores[n // 4], 4),
                "median": round(scores[n // 2], 4),
                "p75": round(scores[3 * n // 4], 4),
                "max": round(scores[-1], 4),
                "mean": round(sum(scores) / n, 4),
            }
        else:
            memory["importance_distribution"] = {}

        # Recall stats
        never_recalled = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND recall_count = 0"
        ).fetchone()["c"]
        recall_stats = conn.execute(
            "SELECT AVG(recall_count) as avg_rc, MAX(recall_count) as max_rc FROM nodes WHERE is_current = 1"
        ).fetchone()
        memory["recall_stats"] = {
            "never_recalled": never_recalled,
            "avg_recall_count": round(recall_stats["avg_rc"] or 0, 2),
            "max_recall_count": recall_stats["max_rc"] or 0,
        }

        # Embedding coverage
        with_emb = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND embedding IS NOT NULL"
        ).fetchone()["c"]
        memory["embedding_coverage"] = {
            "with_embedding": with_emb,
            "without_embedding": total_current - with_emb,
            "coverage_pct": round(with_emb / total_current * 100, 1) if total_current else 0,
        }

        metrics["memory"] = memory

        # ── Provenance ───────────────────────────────────────────────────
        provenance: dict = {}

        # Evidence by source_domain
        domain_rows = conn.execute(
            """SELECT source_domain, COUNT(*) as c FROM nodes
               WHERE type = 'evidence' AND is_current = 1 AND source_domain IS NOT NULL
               GROUP BY source_domain ORDER BY c DESC"""
        ).fetchall()
        provenance["evidence_by_domain"] = {r["source_domain"]: r["c"] for r in domain_rows}

        # Evidence by source_type
        stype_rows = conn.execute(
            """SELECT COALESCE(source_type, 'unknown') as st, COUNT(*) as c FROM nodes
               WHERE type = 'evidence' AND is_current = 1
               GROUP BY st ORDER BY c DESC"""
        ).fetchall()
        provenance["evidence_by_source_type"] = {r["st"]: r["c"] for r in stype_rows}

        # Trust status check
        _diag_config = json.loads(core.CONFIG_PATH.read_text()) if core.CONFIG_PATH.exists() else {}
        trusted_domains = set(_diag_config.get("trust_pool", []))
        untrusted = conn.execute(
            """SELECT source_domain, COUNT(*) as c FROM nodes
               WHERE type = 'evidence' AND is_current = 1
               AND source_domain IS NOT NULL AND source_domain != ''
               GROUP BY source_domain"""
        ).fetchall()
        untrusted_domains = {
            r["source_domain"]: r["c"]
            for r in untrusted
            if r["source_domain"] not in trusted_domains
        }
        provenance["untrusted_domain_evidence"] = untrusted_domains

        metrics["provenance"] = provenance

        # ── Agent Experience ─────────────────────────────────────────────
        experience: dict = {}

        # Feeling reports
        total_feelings = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE type = 'feeling_report' AND is_current = 1"
        ).fetchone()["c"]
        experience["total_feeling_reports"] = total_feelings

        # By categorical_tag
        tag_rows = conn.execute(
            """SELECT COALESCE(categorical_tag, '<tagless>') as tag, COUNT(*) as c
               FROM nodes WHERE type = 'feeling_report' AND is_current = 1
               GROUP BY tag ORDER BY c DESC"""
        ).fetchall()
        experience["by_categorical_tag"] = {r["tag"]: r["c"] for r in tag_rows}

        # By nudge_source
        nudge_rows = conn.execute(
            """SELECT COALESCE(nudge_source, 'unknown') as ns, COUNT(*) as c
               FROM nodes WHERE type = 'feeling_report' AND is_current = 1
               GROUP BY ns ORDER BY c DESC"""
        ).fetchall()
        nudge_dist = {r["ns"]: r["c"] for r in nudge_rows}
        experience["by_nudge_source"] = nudge_dist
        voluntary = nudge_dist.get("voluntary", 0)
        nudged = total_feelings - voluntary
        experience["voluntary_vs_nudged"] = {
            "voluntary": voluntary,
            "nudged": nudged,
            "voluntary_pct": round(voluntary / total_feelings * 100, 1) if total_feelings else 0,
        }

        metrics["experience"] = experience

        # ── Connectivity / Synthesis ─────────────────────────────────────
        connectivity: dict = {}

        # Cross-evidence derivation density: how many derivations
        # connect observations from different evidence sources?
        dv_rows = conn.execute(
            "SELECT id FROM nodes WHERE type = 'derivation' AND is_current = 1"
        ).fetchall()
        cross_ev = 0
        single_ev = 0
        pure_chain = 0
        for dv in dv_rows:
            premises = conn.execute(
                """SELECT n.evidence_id FROM edges e
                   JOIN nodes n ON e.target_id = n.id
                   WHERE e.source_id = ? AND e.relation = 'derives_from'""",
                (dv["id"],),
            ).fetchall()
            ev_set = {p["evidence_id"] for p in premises if p["evidence_id"]}
            if len(ev_set) > 1:
                cross_ev += 1
            elif len(ev_set) == 1:
                single_ev += 1
            else:
                pure_chain += 1
        total_dvs = len(dv_rows)
        connectivity["total_derivations"] = total_dvs
        connectivity["cross_evidence_derivations"] = cross_ev
        connectivity["single_evidence_derivations"] = single_ev
        connectivity["pure_chain_derivations"] = pure_chain
        connectivity["synthesis_density"] = round(
            cross_ev / total_dvs, 3) if total_dvs else 0

        # Connected components (undirected reachability)
        current_ids = {r["id"] for r in conn.execute(
            "SELECT id FROM nodes WHERE is_current = 1"
        ).fetchall()}
        all_edges = conn.execute("SELECT source_id, target_id FROM edges").fetchall()
        adj: dict = _ddict(set)
        for e in all_edges:
            if e["source_id"] in current_ids or e["target_id"] in current_ids:
                adj[e["source_id"]].add(e["target_id"])
                adj[e["target_id"]].add(e["source_id"])
        visited: set = set()
        comp_sizes = []
        for nid in current_ids:
            if nid not in visited:
                comp = set()
                queue = [nid]
                while queue:
                    c = queue.pop(0)
                    if c in visited:
                        continue
                    visited.add(c)
                    comp.add(c)
                    for nb in adj.get(c, ()):
                        if nb not in visited and nb in current_ids:
                            queue.append(nb)
                comp_sizes.append(len(comp))
        comp_sizes.sort(reverse=True)
        connectivity["connected_components"] = len(comp_sizes)
        connectivity["largest_component"] = comp_sizes[0] if comp_sizes else 0
        connectivity["largest_component_pct"] = round(
            comp_sizes[0] / total_current * 100, 1) if total_current and comp_sizes else 0
        connectivity["isolated_singletons"] = sum(1 for s in comp_sizes if s == 1)

        # Evidence integration: fraction of evidence nodes in the main component
        ev_ids = {r["id"] for r in conn.execute(
            "SELECT id FROM nodes WHERE type = 'evidence' AND is_current = 1"
        ).fetchall()}
        # Recompute main component membership
        main_comp: set = set()
        if comp_sizes:
            visited2: set = set()
            for nid in current_ids:
                if nid not in visited2:
                    comp = set()
                    queue = [nid]
                    while queue:
                        c = queue.pop(0)
                        if c in visited2:
                            continue
                        visited2.add(c)
                        comp.add(c)
                        for nb in adj.get(c, ()):
                            if nb not in visited2 and nb in current_ids:
                                queue.append(nb)
                    if len(comp) == comp_sizes[0] and not main_comp:
                        main_comp = comp
                        break
        ev_in_main = len(ev_ids & main_comp)
        connectivity["evidence_total"] = len(ev_ids)
        connectivity["evidence_in_main_component"] = ev_in_main
        connectivity["evidence_integration_pct"] = round(
            ev_in_main / len(ev_ids) * 100, 1) if ev_ids else 0

        metrics["connectivity"] = connectivity

        # ── Edit History Summary ─────────────────────────────────────────
        edit_summary: dict = {}
        action_rows = conn.execute(
            "SELECT action, COUNT(*) as c FROM edit_history GROUP BY action ORDER BY c DESC"
        ).fetchall()
        edit_summary["by_action"] = {r["action"]: r["c"] for r in action_rows}
        total_edits = conn.execute("SELECT COUNT(*) as c FROM edit_history").fetchone()["c"]
        edit_summary["total_events"] = total_edits

        # Recent activity (last 24h)
        yesterday = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        recent = conn.execute(
            "SELECT action, COUNT(*) as c FROM edit_history WHERE timestamp > ? GROUP BY action",
            (yesterday,),
        ).fetchall()
        edit_summary["last_24h"] = {r["action"]: r["c"] for r in recent}

        metrics["edit_history"] = edit_summary

        # ── Diagnostic History (trend data) ──────────────────────────────
        last_snapshots = conn.execute(
            "SELECT turn, timestamp, checkpoint_mode FROM diagnostic_history ORDER BY id DESC LIMIT 10"
        ).fetchall()
        metrics["diagnostic_history"] = {
            "snapshot_count": conn.execute("SELECT COUNT(*) as c FROM diagnostic_history").fetchone()["c"],
            "recent_snapshots": [
                {"turn": s["turn"], "timestamp": s["timestamp"], "mode": s["checkpoint_mode"]}
                for s in last_snapshots
            ],
        }

        # ── Tool Timing (latency by tool, last 7 days) ──────────────────
        timing_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        per_tool = {}
        timing_rows = conn.execute(
            "SELECT tool_name, duration_ms FROM tool_timing WHERE timestamp >= ? ORDER BY tool_name",
            (timing_cutoff,),
        ).fetchall()
        buckets: dict[str, list[int]] = {}
        for r in timing_rows:
            buckets.setdefault(r["tool_name"], []).append(r["duration_ms"])
        for tool_name, durations in buckets.items():
            durations.sort()
            n = len(durations)
            per_tool[tool_name] = {
                "count": n,
                "mean_ms": round(sum(durations) / n, 1),
                "p50_ms": durations[n // 2],
                "p95_ms": durations[min(int(n * 0.95), n - 1)],
                "max_ms": durations[-1],
            }
        error_count = conn.execute(
            "SELECT COUNT(*) as c FROM tool_timing WHERE timestamp >= ? AND status = 'error'",
            (timing_cutoff,),
        ).fetchone()["c"]
        metrics["tool_timing"] = {
            "window_days": 7,
            "total_calls": len(timing_rows),
            "error_calls": error_count,
            "per_tool": per_tool,
        }

        # ── Read-Tool Contention Tripwire ────────────────────────────────
        # Flags hour-buckets where read-tool latency exceeded thresholds
        # (mean > 5s OR max > 15s) over the last 7 days.  Surface only when
        # any hours are flagged — no false-positive noise in the clean case.
        # Mechanism: the cornerstone-evolution derivation.  Dream-cycle mitigation: the cornerstone-evolution derivation.
        # Open case (awake-burst): the cornerstone-frame-evolution open question.
        _READ_TOOLS = (
            "engram_query",
            "engram_inspect",
            "engram_list",
            "engram_get_subgraph",
        )
        _CONTENTION_MEAN_THRESHOLD_MS = 5000
        _CONTENTION_MAX_THRESHOLD_MS = 15000
        _contention_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=7)
        ).isoformat()
        _contention_placeholders = ",".join("?" * len(_READ_TOOLS))
        # total_flagged_buckets: un-limited count of all flagged (hour, tool) buckets.
        _total_flagged_buckets = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT 1
                FROM tool_timing
                WHERE timestamp >= ?
                  AND tool_name IN ({_contention_placeholders})
                GROUP BY SUBSTR(timestamp, 1, 13), tool_name
                HAVING AVG(duration_ms) > ? OR MAX(duration_ms) > ?
            )
            """,
            (_contention_cutoff, *_READ_TOOLS,
             _CONTENTION_MEAN_THRESHOLD_MS, _CONTENTION_MAX_THRESHOLD_MS),
        ).fetchone()["c"]
        # worst_hours_shown: the up-to-10 worst rows (for display in "worst" list).
        _contention_rows = conn.execute(
            f"""
            SELECT
                SUBSTR(timestamp, 1, 13) AS hour_utc,
                tool_name,
                COUNT(*) AS calls,
                CAST(ROUND(AVG(duration_ms), 0) AS INTEGER) AS mean_ms,
                CAST(MAX(duration_ms) AS INTEGER) AS max_ms
            FROM tool_timing
            WHERE timestamp >= ?
              AND tool_name IN ({_contention_placeholders})
            GROUP BY hour_utc, tool_name
            HAVING AVG(duration_ms) > ? OR MAX(duration_ms) > ?
            ORDER BY mean_ms DESC, max_ms DESC
            LIMIT 10
            """,
            (_contention_cutoff, *_READ_TOOLS,
             _CONTENTION_MEAN_THRESHOLD_MS, _CONTENTION_MAX_THRESHOLD_MS),
        ).fetchall()
        if _contention_rows:
            metrics["read_tool_contention"] = {
                "total_flagged_buckets": _total_flagged_buckets,
                "worst_hours_shown": len(_contention_rows),
                "see": (
                    "the cornerstone-evolution derivation (mechanism), the cornerstone-evolution derivation (dream-cycle mitigation), "
                    "the cornerstone-frame-evolution open question (open: awake-burst)"
                ),
                "worst": [
                    {
                        "hour": r["hour_utc"],
                        "tool": r["tool_name"],
                        "calls": r["calls"],
                        "mean_ms": r["mean_ms"],
                        "max_ms": r["max_ms"],
                    }
                    for r in _contention_rows
                ],
            }

        # ── Calibration ──────────────────────────────────────────────────
        # Use _compute_confidence_distribution directly (not engram_stats) so
        # this tool stays side-effect-free: engram_stats drives _timing_mcp_tool
        # which would write tool_timing + logs/index.db rows.
        try:
            # Corpus: all-time (no time filter)
            calibration_corpus = _compute_confidence_distribution(conn, "", [])
            # This-turn: 1-turn window ≈ 24h (matches engram_stats mode="1-turn")
            _now_utc = datetime.now(timezone.utc)
            _cutoff_iso = (_now_utc - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
            calibration_now = _compute_confidence_distribution(
                conn, " AND created_at >= ?", [_cutoff_iso]
            )
            calibration: dict = {}
            calibration["corpus"] = calibration_corpus
            calibration["this_turn"] = calibration_now

            # Drift indicators: per-type median delta (this_turn vs corpus)
            drift: dict = {}
            for node_type, corpus_stats in calibration["corpus"].get("by_type", {}).items():
                now_stats = calibration["this_turn"].get("by_type", {}).get(node_type)
                if now_stats and now_stats.get("n", 0) >= 3:
                    # Only report drift when this-turn has enough samples (≥3) to be meaningful
                    delta = round(now_stats["p50"] - corpus_stats["p50"], 3)
                    drift[node_type] = {
                        "this_turn_p50": now_stats["p50"],
                        "corpus_p50": corpus_stats["p50"],
                        "delta": delta,
                        "n_this_turn": now_stats["n"],
                    }
            calibration["drift_by_type"] = drift
        except Exception:
            calibration = {"error": "calibration stats unavailable"}

        # Calibration dimension is currently descriptive (weight=0). V2 will add
        # drift-based scoring once thresholds are calibrated from observed data.
        calibration_weight = 0  # noqa: F841

        metrics["calibration"] = calibration

        # ── Health Score (0-100) ─────────────────────────────────────────
        # Single source of truth: the shared helper (charter §6 fold). The
        # intermediates computed above remain for their own report sections;
        # the SCORE comes from one implementation shared with engram_stats.
        metrics["health_score"] = _compute_health_score(conn)

        # ── Config Summary (the dream-mode context-hunger observation: prevent doc-config drift) ──────────
        mem = core._get_memory_config()
        test_config = mem.get("test", {})
        # Utility scoring stats
        util_rows = conn.execute(
            "SELECT COUNT(*) as c, AVG(utility_score) as avg_u, MAX(utility_score) as max_u "
            "FROM nodes WHERE is_current = 1 AND COALESCE(utility_score, 0) > 0"
        ).fetchone()

        metrics["config_summary"] = {
            "tier1_max_nodes": mem.get("tier1_max_nodes", 200),
            "tier2_max_nodes": mem.get("tier2_max_nodes", 1000),
            "decay_base": mem.get("decay_base", 1.014),
            "current_turn": mem.get("current_turn", 0),
            "ab_test_enabled": test_config.get("enabled", False),
            "ab_test_tier2": test_config.get("tier2_max_nodes") if test_config.get("enabled") else None,
            "utility_scoring": {
                "alpha_by_action": core.USE_ALPHA,
                "util_beta": core.UTIL_BETA,
                "imp_beta": core.IMP_BETA,
                "nodes_with_utility": util_rows["c"] if util_rows else 0,
                "avg_utility": round(util_rows["avg_u"] or 0, 4) if util_rows else 0,
                "max_utility": round(util_rows["max_u"] or 0, 4) if util_rows else 0,
            },
        }

        banner = core._walguard_degraded_banner()
        if banner:
            metrics["_walguard_warning"] = banner
        return json.dumps(metrics)
    finally:
        conn.close()
