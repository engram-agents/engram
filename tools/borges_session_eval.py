#!/usr/bin/env python3
"""Borges session-quality evaluator — scaffolding for the rubric in ob_NNNN.

Computes the trajectory + outcome metrics that data sources currently
support; leaves explicit placeholders for metrics that need external
input (Lei-correction rate, bias audits, post-session reflection).

Reads:
  - ~/.engram/deference-detector.log      (T1 — empty until PR #31 deploys)
  - ~/.engram/knowledge.db                (outcome metrics: nodes added,
                                            question states, lessons, etc.)

Usage:
    python3 tools/borges_session_eval.py [--since YYYY-MM-DD] [--json]

Without --json, prints a human-readable report.
With --json, emits a structured report suitable for nap-checkpoint
diagnostic ingestion.

Per ob_NNNN, the M-tier metrics (M1 Lei-correction rate, M2 self-confidence
calibration, M3 bias-vector audits) require external input and are
recorded as null/pending in the JSON output. The tool is the SCAFFOLD;
filling M1-M3 happens via separate ritual (post-session Lei review).
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# The ENGRAM home has ONE definition: tools/_owner_read.engram_home() (#3863 step 3c-iii; the rulings:
# engram_home_resolver's docstring). The repo root (the parent of this file's directory) is appended, never inserted
# at [0] (#2565, #4054).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)
from tools import _owner_read as owner_read  # noqa: E402

# A RELATIVE ENGRAM_HOME must not raise at import: the refusal is recorded, the constants stay None, and main()
# refuses at its first action (one stderr line naming the value, exit 2).
try:
    ENGRAM_HOME = owner_read.engram_home()
    _HOME_REFUSAL = None
except ValueError as _exc:
    ENGRAM_HOME = None
    _HOME_REFUSAL = str(_exc)
DEFERENCE_LOG = ENGRAM_HOME / "deference-detector.log" if ENGRAM_HOME is not None else None
KNOWLEDGE_DB = ENGRAM_HOME / "knowledge.db" if ENGRAM_HOME is not None else None


def trajectory_metrics() -> dict:
    """T1-T6 — was reasoning sound in process?

    A metric that used to live here was retired [part of #3215]: its two
    source logs had no producer left once the hook that wrote them was
    deprecated and removed (closed #172). The numbering gap (T1, T3-T6) is
    left as-is rather than renumbered.
    """
    # T1: deference-detector fire rate (PR #31 not yet deployed → log absent)
    t1_data = "unavailable: PR #31 not yet deployed"
    if DEFERENCE_LOG.exists():
        try:
            with open(DEFERENCE_LOG) as f:
                fires = [line for line in f if "Detected" in line]
            t1_data = {"fires": len(fires)}
        except OSError:
            t1_data = "log read error"

    return {
        "T1_deference_rate": t1_data,
        "T3_structural_stop_catches": "pending: needs JSONL post-hoc analysis tooling",
        "T4_observation_derivation_ratio": "see outcome.O1 — ratio computed there",
        "T5_retraction_within_N_turns": "pending: needs ENGRAM history scan",
        "T6_decision_vs_deferral_ratio": "pending: needs JSONL post-hoc analysis tooling",
    }


def outcome_metrics(since: date | None) -> dict:
    """O1-O6 — did the work land?"""
    if not KNOWLEDGE_DB.exists():
        return {"error": f"knowledge.db not found at {KNOWLEDGE_DB}"}

    try:
        conn = sqlite3.connect(str(KNOWLEDGE_DB), timeout=2.0)
    except sqlite3.OperationalError as exc:
        return {"error": f"knowledge.db connect failed: {exc}"}
    conn.row_factory = sqlite3.Row
    try:
        # O1: ENGRAM nodes added — counts by type, since-filtered
        if since:
            since_str = since.isoformat()
            rows = conn.execute(
                "SELECT type, COUNT(*) as n FROM nodes "
                "WHERE created_at >= ? AND is_current = 1 GROUP BY type",
                (since_str,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT type, COUNT(*) as n FROM nodes WHERE is_current = 1 GROUP BY type"
            ).fetchall()
        node_counts = {r["type"]: r["n"] for r in rows}
        total_added = sum(node_counts.values())
        observations = node_counts.get("observation_factual", 0)
        derivations = node_counts.get("derivation", 0)
        synthesis_ratio = (derivations / observations) if observations else None

        # O3: open-question state-transitions — questions changing status since
        if since:
            q_resolved = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE type='question' "
                "AND status IN ('resolved', 'partially_resolved') AND is_current=1 "
                "AND created_at >= ?", (since_str,)
            ).fetchone()[0]
        else:
            q_resolved = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE type='question' "
                "AND status IN ('resolved', 'partially_resolved') AND is_current=1"
            ).fetchone()[0]

        # O4: lessons registered
        lesson_count = node_counts.get("lesson", 0)

        # Confidence distribution of added claims (for slop-vs-substance)
        conf_dist = {}
        if since:
            rows = conn.execute(
                "SELECT confidence FROM nodes WHERE created_at >= ? AND is_current=1 "
                "AND confidence IS NOT NULL", (since_str,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT confidence FROM nodes WHERE is_current=1 AND confidence IS NOT NULL"
            ).fetchall()
        bucketed = Counter()
        for r in rows:
            c = r["confidence"]
            if c is None:
                continue
            if c >= 0.9:
                bucketed["[0.9, 1.0]"] += 1
            elif c >= 0.7:
                bucketed["[0.7, 0.9)"] += 1
            elif c >= 0.5:
                bucketed["[0.5, 0.7)"] += 1
            else:
                bucketed["[0.0, 0.5)"] += 1
        conf_dist = dict(bucketed)
    except sqlite3.OperationalError as exc:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        return {"error": f"knowledge.db query failed: {exc}"}
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    return {
        "O1_engram_nodes": {
            "total_added": total_added,
            "by_type": node_counts,
            "confidence_distribution": conf_dist,
        },
        "O2_pr_state": "pending: needs gh integration (gh pr list --author @me --json state,title)",
        "O3_question_state_transitions": {
            "questions_resolved_or_partial_in_window": q_resolved,
        },
        "O4_lesson_registrations": lesson_count,
        "O5_syllabus_coverage": "pending: needs syllabus-skill diff analysis",
        "O6_autonomy_goal_session_hours_fraction": "pending: needs JSONL turn-by-turn classification",
        "synthesis_ratio_dv_over_ob": round(synthesis_ratio, 3) if synthesis_ratio is not None else None,
    }


def meta_metrics() -> dict:
    """M1-M3 — is the eval itself well-calibrated? (Requires external input.)"""
    return {
        "M1_lei_correction_rate": {
            "value": None,
            "note": (
                "Load-bearing external calibration signal per the LLM-as-judge >50% error literature. "
                "Filled post-session by counting Lei's catches that diverge from my own self-assessment. "
                "Track in ~/.engram/lei-corrections.jsonl as future-work."
            ),
        },
        "M2_self_confidence_brier_score": {
            "value": None,
            "note": (
                "Brier score over the agent's claimed-confidence vs actual outcome. "
                "Requires accumulating self-evaluations + ground-truth resolutions over time. "
                "Pending: needs longitudinal data + outcome resolver."
            ),
        },
        "M3_bias_vector_audits": {
            "length_bias": "pending: needs response-length × Lei-correction correlation",
            "novelty_bias": "pending: needs node-novelty × eventual-retraction correlation",
            "agreeableness_bias": "pending: needs deference-detector hits × self-eval-quality correlation",
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--since", type=str, default=None, help="Filter to entries on/after this ISO date.")
    p.add_argument("--json", action="store_true", help="Emit structured JSON report.")
    args = p.parse_args()

    if _HOME_REFUSAL is not None:
        print(f"borges_session_eval: {_HOME_REFUSAL}", file=sys.stderr)
        return 2

    since = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since).date()
        except ValueError:
            print(f"error: --since must be ISO YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
            return 2

    report = {
        "schema_version": "0.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since": since.isoformat() if since else "all-time",
        "trajectory": trajectory_metrics(),
        "outcome": outcome_metrics(since),
        "meta": meta_metrics(),
        "rubric_source": "ob_NNNN — candidate Borges self-evaluation rubric (2026-05-06)",
    }

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    # Human-readable
    print(f"=== Borges session-eval report ===")
    print(f"  generated:  {report['generated_at']}")
    print(f"  since:      {report['since']}")
    print()
    print("--- Trajectory (was reasoning sound?) ---")
    t = report["trajectory"]
    print(f"  T1 deference-rate: {t['T1_deference_rate']}")
    print(f"  T3-T6: pending (see JSON for status)")
    print()
    print("--- Outcome (did work land?) ---")
    o = report["outcome"]
    if "error" in o:
        print(f"  ERROR: {o['error']}")
    else:
        print(f"  O1 ENGRAM nodes added: {o['O1_engram_nodes']['total_added']} total")
        for nt, n in sorted(o["O1_engram_nodes"]["by_type"].items()):
            print(f"     {nt:30s} {n:>5}")
        print(f"  O1 confidence distribution: {o['O1_engram_nodes']['confidence_distribution']}")
        print(f"  O3 questions resolved/partial: {o['O3_question_state_transitions']['questions_resolved_or_partial_in_window']}")
        print(f"  O4 lessons registered: {o['O4_lesson_registrations']}")
        print(f"  Synthesis ratio (dv / ob): {o.get('synthesis_ratio_dv_over_ob')}")
    print()
    print("--- Meta (eval calibration — pending external signals) ---")
    print("  M1 Lei-correction rate: pending (load-bearing external signal)")
    print("  M2 self-confidence Brier: pending (longitudinal)")
    print("  M3 bias-vector audits: pending (need correlation studies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
