"""viz.data.calibration — confidence-calibration data assembly for
/api/calibration (engram-alpha#2555 PR A, pure relocation of
viz_server.py's ``get_calibration_data``/``_conf_dist_for_window``/
``_viz_percentile``, byte-identical function bodies).

No HTTP, no HTML — unit-testable in isolation.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Queries replicated from server.py:_compute_confidence_distribution.
# viz_server.py is stdlib-only; importing server.py would pull in the full
# MCP server stack, so the SQL is dual-sourced here intentionally.
#
# WARNING: SQL queries in _conf_dist_for_window mirror
# server.py:_compute_confidence_distribution (around lines 6971-7103).
# If server.py's confidence aggregation logic changes, update both.
# ---------------------------------------------------------------------------


def _viz_percentile(sorted_vals: list, p: float) -> float:
    """Compute p-th percentile of a pre-sorted list. Linear interpolation.

    Mirrors server.py:_percentile exactly (same algorithm, same edge-case
    handling). Duplicated here to keep viz_server.py self-contained.
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_vals[-1]
    frac = idx - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


def _conf_dist_for_window(
    conn: sqlite3.Connection,
    created_at_filter: str,
    filter_args: list,
) -> dict:
    """Compute confidence distribution for a single time window.

    Returns dict with by_type, by_quote_type, by_reasoning_type,
    by_source_class sub-keys.  Empty results use empty dicts, never None.
    Mirrors server.py:_compute_confidence_distribution.

    INTENTIONAL DIVERGENCE: emits {"n": 0, ...} placeholder entries for empty
    types so the UI always has consistent rows; server.py skips them via
    continue.
    """
    _base_filter = (
        " AND is_current = 1"
        " AND (status IS NULL OR status != 'retracted')"
    )
    _CONFIDENCE_BEARING_TYPES = (
        "observation_factual",
        "derivation",
        "conjecture",
        "lesson",
        "axiom",
        "definition",
    )

    # ── by_type ──────────────────────────────────────────────────────────
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
            # Emit explicit empty placeholder so the UI can render a row
            by_type[ntype] = {"n": 0, "mean": None, "p25": None, "p50": None,
                              "p75": None, "p90": None, "p95": None}
            continue
        n = len(confidences)
        by_type[ntype] = {
            "n": n,
            "mean": round(sum(confidences) / n, 3),
            "p25": round(_viz_percentile(confidences, 25), 2),
            "p50": round(_viz_percentile(confidences, 50), 2),
            "p75": round(_viz_percentile(confidences, 75), 2),
            "p90": round(_viz_percentile(confidences, 90), 2),
            "p95": round(_viz_percentile(confidences, 95), 2),
        }

    # ── by_quote_type ─────────────────────────────────────────────────────
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
        by_quote_type[qt] = {"n": row["n"], "mean": round(row["mean_conf"], 3)}

    # ── by_reasoning_type ────────────────────────────────────────────────
    by_reasoning_type: dict = {}
    deriv_rows = conn.execute(
        "SELECT confidence, metadata FROM nodes WHERE type = 'derivation'"
        + _base_filter
        + created_at_filter
        + " AND confidence IS NOT NULL AND metadata IS NOT NULL",
        filter_args,
    ).fetchall()
    _rt_buckets: dict = {}
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

    # ── by_source_class ──────────────────────────────────────────────────
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


def get_calibration_data(db_path: str) -> dict:
    """Compute confidence calibration data across three time windows.

    Windows mirror engram_stats / engram_diagnose conventions:
      corpus       — all time (no created_at filter)
      this_turn    — last 24 h (≈ 1-turn window in engram_stats terms)
      rolling_7    — last 168 h (≈ 7-turn window)
      drift_by_type — per-type p50 delta (this_turn vs corpus, only when n≥3)

    Returns {"error": ...} on failure.  Never raises.
    """
    if not db_path or not os.path.exists(db_path):
        return {"error": f"Database not found: {db_path!r}"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {"error": f"Could not open DB: {exc}"}
    try:
        now_utc = datetime.now(timezone.utc)

        # Corpus: all-time, no filter
        corpus = _conf_dist_for_window(conn, "", [])

        # this_turn: last 24 h (matches engram_stats mode="1-turn")
        cutoff_1t = (now_utc - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        this_turn = _conf_dist_for_window(conn, " AND created_at >= ?", [cutoff_1t])

        # rolling_7: last 168 h (matches engram_stats mode="7-turn")
        cutoff_7t = (now_utc - timedelta(hours=168)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rolling_7 = _conf_dist_for_window(conn, " AND created_at >= ?", [cutoff_7t])

        # Drift: this_turn p50 vs corpus p50, only when this_turn has ≥3 samples
        # (mirrors engram_diagnose gate: avoids noisy single-node drift reports)
        drift_by_type: dict = {}
        for ntype, corpus_stats in corpus["by_type"].items():
            this_stats = this_turn["by_type"].get(ntype, {})
            corpus_p50 = corpus_stats.get("p50")
            this_p50 = this_stats.get("p50")
            n_this = this_stats.get("n", 0)
            if corpus_p50 is None or this_p50 is None or n_this < 3:
                continue
            drift_by_type[ntype] = {
                "this_turn_p50": this_p50,
                "corpus_p50": corpus_p50,
                "delta": round(this_p50 - corpus_p50, 3),
                "n_this_turn": n_this,
            }

        return {
            "corpus": corpus,
            "this_turn": this_turn,
            "rolling_7": rolling_7,
            "drift_by_type": drift_by_type,
        }
    finally:
        conn.close()
