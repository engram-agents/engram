"""CLI orchestrator — load goldens, run system-under-test, apply metrics,
emit markdown + JSON report with per-query gap analysis.

Usage:
    python -m tools.recall_harness.cli                    # run + emit report to default path
    python -m tools.recall_harness.cli --top-k 30         # widen test top-k
    python -m tools.recall_harness.cli --out /tmp/eval.md # custom output path
    python -m tools.recall_harness.cli --snapshot /path/to/snapshot.db

Requires (no repo-default; every one is a per-seat/per-run private artifact):
    ENGRAM_RECALL_GOLDEN_DIR      directory of q_*.json golden files
    ENGRAM_RECALL_SNAPSHOT        snapshot .db to measure against (or --snapshot)
    ENGRAM_RECALL_EVAL_RUNS_DIR   output directory (or --out)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import statistics
import subprocess
from collections import defaultdict

from .runner import run_all, ENGRAM_ALPHA_DIR
from .golden_loader import load_golden_set
from .metrics import ranking  # registers metrics
from .metrics.registry import apply_metric
from .report import BranchRun, aggregate


def git_metadata(repo: str = ENGRAM_ALPHA_DIR) -> dict:
    """Capture dev SHA + dirty flag + branch + commit subject. Best-effort —
    if git fails for any reason, returns 'unknown' fields so the eval still
    runs without metadata."""
    def _g(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", repo, *args],
                stderr=subprocess.DEVNULL, timeout=2,
            ).decode().strip()
        except Exception:
            return "unknown"
    sha = _g("rev-parse", "--short", "HEAD")
    full_sha = _g("rev-parse", "HEAD")
    branch = _g("rev-parse", "--abbrev-ref", "HEAD")
    subject = _g("log", "-1", "--pretty=%s")
    status = _g("status", "--porcelain")
    dirty = bool(status) and status != "unknown"
    return {
        "sha": sha,
        "full_sha": full_sha,
        "branch": branch,
        "commit_subject": subject,
        "dirty": dirty,
        "repo": repo,
    }


METRICS_TO_APPLY = [
    ("precision_at_k", {"k": 5}),
    ("precision_at_k", {"k": 10}),
    ("recall_at_k", {"k": 10}),
    ("recall_at_k", {"k": 20}),
    ("ndcg_at_k", {"k": 10}),
    ("mean_reciprocal_rank", {}),
]


def metric_key(name: str, kwargs: dict) -> str:
    if "k" in kwargs:
        return f"{name}_{kwargs['k']}"
    return name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=20, help="system-under-test top-k")
    ap.add_argument("--snapshot", default=None,
                    help="snapshot .db path to measure against; defaults to "
                         "$ENGRAM_RECALL_SNAPSHOT (fails loudly if neither is set)")
    ap.add_argument("--out", default=None,
                    help="markdown report output path; defaults to "
                         "$ENGRAM_RECALL_EVAL_RUNS_DIR/{sha}[_dirty]_{utc_iso}.md "
                         "(fails loudly if neither is set)")
    ap.add_argument("--label", default=None,
                    help="optional short label appended to the filename "
                         "(e.g. 'pr199', 'tech-deep'); informational")
    args = ap.parse_args()

    gmeta = git_metadata()
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.out is None:
        eval_runs_dir = os.environ.get("ENGRAM_RECALL_EVAL_RUNS_DIR")
        if not eval_runs_dir:
            raise RuntimeError(
                "No --out given and ENGRAM_RECALL_EVAL_RUNS_DIR is not set. "
                "Set ENGRAM_RECALL_EVAL_RUNS_DIR to the directory reports "
                "should be written to, or pass --out explicitly."
            )
        parts = [gmeta["sha"]]
        if gmeta["dirty"]:
            parts.append("dirty")
        parts.append(ts)
        if args.label:
            parts.append(args.label)
        fname = "_".join(parts) + ".md"
        args.out = os.path.join(eval_runs_dir, fname)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    print(f"Loading golden set...")
    goldens = load_golden_set()
    print(f"  → {len(goldens)} goldens")

    print(f"Running system-under-test (top_k={args.top_k}) against snapshot...")
    # run_all raises loudly (never falls back to a guessed path) if the
    # snapshot cannot be resolved from --snapshot or $ENGRAM_RECALL_SNAPSHOT —
    # snapshot_used below is therefore always the REAL path actually opened,
    # never a default/placeholder value.
    test_rrus, snapshot_used = run_all(snapshot_path=args.snapshot, top_k=args.top_k)

    # Belt-and-suspenders provenance guard: a report field that can only
    # ever be present is not a pin (that was the original defect — a
    # constant that could never disagree with reality). Don't rely solely on
    # run_all's own internal guarantee that it never returns falsy — assert
    # it explicitly here too, so this script FAILS LOUDLY rather than ever
    # writing a report with a guessed/defaulted snapshot value. No `or
    # "unknown"`, no `.get(..., default)` on this field, anywhere below.
    if not snapshot_used:
        raise RuntimeError(
            "run_all() did not report which snapshot it opened "
            f"(got {snapshot_used!r}). Refusing to write a report with "
            "unknown snapshot provenance."
        )
    print(f"  → {len(test_rrus)} test RRUs (snapshot={snapshot_used})")

    print(f"Applying metrics ({len(METRICS_TO_APPLY)})...")
    per_query: dict[str, dict[str, dict]] = {}
    per_query_system_ranked: dict[str, list[dict]] = {}
    per_query_query_debug: dict[str, dict | None] = {}
    for qid, golden in goldens.items():
        test = test_rrus[qid]
        per_query[qid] = {}
        for name, kwargs in METRICS_TO_APPLY:
            result = apply_metric(name, test, golden, **kwargs)
            per_query[qid][metric_key(name, kwargs)] = result
        # Capture the system's ranked top-k so build_ranked_lists.py can
        # produce SxS-comparable ranked-list JSONs without re-running the
        # harness. See SXS-DESIGN.md.
        per_query_system_ranked[qid] = [
            {
                "id": n.id,
                "rank": n.rank,
                "signals": n.signals,
            }
            for n in test.ranked_nodes
        ]
        # Per-query debug payload (PR #206 — return_debug). Kept in its own
        # top-level dict (not co-mingled with per_query_system_ranked) so
        # downstream consumers can iterate either dict cleanly.
        per_query_query_debug[qid] = test.metadata.get("query_debug")

    run = BranchRun(name="dev_head_v4_2_golden", metric_results=per_query)
    agg = aggregate(run)

    # --- Per-category aggregation ---
    by_category: dict[str, list[str]] = defaultdict(list)
    for qid, golden in goldens.items():
        cat = golden.metadata.get("primary_category", "unknown")
        by_category[cat].append(qid)

    cat_agg: dict[str, dict[str, float]] = {}
    for cat, qids in by_category.items():
        cat_agg[cat] = {}
        for mk in [metric_key(n, k) for n, k in METRICS_TO_APPLY]:
            vals = [per_query[q][mk]["value"] for q in qids
                    if per_query[q][mk].get("value") is not None]
            cat_agg[cat][mk] = statistics.mean(vals) if vals else None

    # --- Gap ranking: lowest NDCG@10 = biggest opportunity ---
    gap_key = "ndcg_at_k_10"
    by_gap = sorted(
        ((qid, per_query[qid][gap_key].get("value", 0) or 0,
          goldens[qid].metadata.get("primary_category", "unknown"))
         for qid in per_query),
        key=lambda x: x[1],
    )

    # One worst-per-category for review
    worst_per_cat: dict[str, tuple[str, float]] = {}
    for qid, score, cat in by_gap:
        if cat not in worst_per_cat:
            worst_per_cat[cat] = (qid, score)

    # --- Render markdown ---
    lines: list[str] = []
    lines.append(f"# Recall Eval — `{gmeta['sha']}` ({gmeta['branch']}) against v4.2 golden set")
    lines.append("")
    lines.append(f"**Run timestamp (UTC)**: {ts}")
    lines.append(f"**Dev SHA**: `{gmeta['full_sha']}`"
                 + (" **(working tree DIRTY)**" if gmeta['dirty'] else ""))
    lines.append(f"**Branch**: `{gmeta['branch']}`")
    lines.append(f"**Latest commit**: {gmeta['commit_subject']}")
    lines.append(f"**Snapshot**: {snapshot_used}")
    lines.append(f"**System under test**: `_search_nodes` (top_k={args.top_k}, "
                  f"include_superseded=False, semantic=True)")
    lines.append(f"**Goldens evaluated**: {len(goldens)}")
    lines.append("")

    lines.append("## Overall metrics")
    lines.append("")
    lines.append("| Metric | Mean | Stdev | n | skipped |")
    lines.append("|---|---|---|---|---|")
    for mk in [metric_key(n, k) for n, k in METRICS_TO_APPLY]:
        a = agg.get(mk, {})
        mean = a.get("mean")
        stdev = a.get("stdev")
        mean_s = f"{mean:.4f}" if mean is not None else "—"
        stdev_s = f"{stdev:.4f}" if stdev is not None else "—"
        lines.append(f"| {mk} | {mean_s} | {stdev_s} | {a.get('count', 0)} | {a.get('skipped', 0)} |")
    lines.append("")

    lines.append("## By primary_category")
    lines.append("")
    cats = sorted(cat_agg)
    header = ["category", "n"] + [metric_key(n, k) for n, k in METRICS_TO_APPLY]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for cat in cats:
        row = [cat, str(len(by_category[cat]))]
        for mk in [metric_key(n, k) for n, k in METRICS_TO_APPLY]:
            v = cat_agg[cat].get(mk)
            row.append(f"{v:.4f}" if v is not None else "—")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lines.append(f"## Worst-NDCG-per-category (review candidates)")
    lines.append("")
    lines.append("| category | query_id | NDCG@10 | golden_size | golden_g3_count | top-3 system output |")
    lines.append("|---|---|---|---|---|---|")
    for cat in sorted(worst_per_cat):
        qid, score = worst_per_cat[cat]
        golden = goldens[qid]
        test = test_rrus[qid]
        g3_count = len(golden.highly_relevant_ids)
        top3 = ", ".join(test.top_k_ids(3)) or "—"
        lines.append(f"| {cat} | {qid} | {score:.4f} | {len(golden.ranked_nodes)} | {g3_count} | {top3} |")
    lines.append("")

    lines.append("## Worst-10 queries overall (by NDCG@10)")
    lines.append("")
    lines.append("| rank | query_id | category | NDCG@10 | P@5 | recall@10 | MRR | top-3 system | top-3 golden grade-3 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, (qid, score, cat) in enumerate(by_gap[:10], start=1):
        m = per_query[qid]
        p5 = m["precision_at_k_5"].get("value")
        r10 = m["recall_at_k_10"].get("value")
        mrr = m["mean_reciprocal_rank"].get("value")
        top3_sys = ", ".join(test_rrus[qid].top_k_ids(3))
        g3_ids = list(goldens[qid].highly_relevant_ids)[:3]
        top3_gold = ", ".join(g3_ids) if g3_ids else "—"
        def f(v): return f"{v:.3f}" if v is not None else "—"
        lines.append(f"| {i} | {qid} | {cat} | {f(score)} | {f(p5)} | {f(r10)} | {f(mrr)} | {top3_sys} | {top3_gold} |")
    lines.append("")

    md = "\n".join(lines)
    with open(args.out, "w") as f:
        f.write(md)

    json_path = args.out.replace(".md", ".json")
    with open(json_path, "w") as f:
        json.dump({
            "git": gmeta,
            "run_ts_utc": ts,
            "snapshot": snapshot_used,
            "top_k": args.top_k,
            "label": args.label,
            "overall": agg,
            "by_category": cat_agg,
            "per_query": per_query,
            "per_query_system_ranked": per_query_system_ranked,
            "per_query_query_debug": per_query_query_debug,
            "worst_per_category": worst_per_cat,
            "worst_overall_10": [(q, s, c) for q, s, c in by_gap[:10]],
        }, f, indent=2, default=str)

    print(f"\nWrote {args.out}")
    print(f"Wrote {json_path}")

    # Print quick summary
    print("\n=== Overall ===")
    for mk in [metric_key(n, k) for n, k in METRICS_TO_APPLY]:
        a = agg.get(mk, {})
        mean = a.get("mean")
        if mean is not None:
            print(f"  {mk}: {mean:.4f} (n={a.get('count')})")


if __name__ == "__main__":
    main()
