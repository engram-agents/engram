"""Aggregate metric results into a markdown A/B report.

Per HARNESS-DESIGN.md "v1 components / report.py". Consumes per-query metric
dicts for one or more algorithm branches, aggregates per-metric (mean over
non-skipped queries), and emits a markdown delta table when 2+ branches are
present.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass


@dataclass
class BranchRun:
    """Per-branch result set: branch name + per-query metric dicts.

    metric_results layout: {query_id: {metric_name: metric_result_dict}}
    """
    name: str
    metric_results: dict[str, dict[str, dict]]


def aggregate(run: BranchRun) -> dict[str, dict]:
    """Aggregate per-metric across queries. Skipped queries (value=None)
    excluded from mean. Returns {metric_name: {mean, count, skipped}}.
    """
    by_metric: dict[str, list[float]] = {}
    skipped: dict[str, int] = {}
    for qid, metrics in run.metric_results.items():
        for mname, mres in metrics.items():
            v = mres.get("value")
            if v is None:
                skipped[mname] = skipped.get(mname, 0) + 1
            else:
                by_metric.setdefault(mname, []).append(v)
    return {
        m: {
            "mean": statistics.mean(vs) if vs else None,
            "stdev": statistics.stdev(vs) if len(vs) > 1 else None,
            "count": len(vs),
            "skipped": skipped.get(m, 0),
        }
        for m, vs in by_metric.items()
    }


def render_markdown(runs: list[BranchRun]) -> str:
    """Markdown report. With 1 run: just the per-metric table. With 2+ runs:
    side-by-side + delta column."""
    if not runs:
        return "# No runs to report.\n"

    aggs = {r.name: aggregate(r) for r in runs}
    metric_names = sorted(set().union(*[set(a) for a in aggs.values()]))

    lines = []
    lines.append("# Recall A/B Eval Report\n")
    lines.append(f"**Branches**: {', '.join(r.name for r in runs)}")
    lines.append(f"**Queries**: {len(runs[0].metric_results)}\n")

    # Header
    if len(runs) == 1:
        lines.append("| Metric | Mean | Stdev | Count | Skipped |")
        lines.append("|---|---|---|---|---|")
        agg = aggs[runs[0].name]
        for m in metric_names:
            r = agg.get(m, {})
            lines.append(
                f"| {m} | "
                f"{r.get('mean'):.4f} | "
                f"{r.get('stdev'):.4f} | "
                f"{r.get('count')} | "
                f"{r.get('skipped')} |"
                if r.get('mean') is not None and r.get('stdev') is not None
                else f"| {m} | — | — | {r.get('count', 0)} | {r.get('skipped', 0)} |"
            )
    else:
        baseline = runs[0]
        cmp_names = [r.name for r in runs[1:]]
        header = ["Metric", baseline.name] + [f"{n} (Δ)" for n in cmp_names]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for m in metric_names:
            base_mean = aggs[baseline.name].get(m, {}).get("mean")
            row = [m, f"{base_mean:.4f}" if base_mean is not None else "—"]
            for r in runs[1:]:
                v = aggs[r.name].get(m, {}).get("mean")
                if v is None or base_mean is None:
                    row.append("—")
                else:
                    delta = v - base_mean
                    sign = "+" if delta >= 0 else ""
                    row.append(f"{v:.4f} ({sign}{delta:.4f})")
            lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines) + "\n"


def write_report(runs: list[BranchRun], output_path: str) -> str:
    """Write markdown + JSON side-by-side. Returns markdown path."""
    md = render_markdown(runs)
    with open(output_path, "w") as f:
        f.write(md)
    # Also dump raw aggregates as JSON
    json_path = output_path.replace(".md", ".json")
    payload = {r.name: aggregate(r) for r in runs}
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    return output_path
