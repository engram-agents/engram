#!/usr/bin/env python3
"""Score the probe's arms against the agent's ratings: a per-arm metrics table.

    python3 probe_eval.py --golden <out>/golden --test <out>/test \
        [--pairs <out>/pairs.jsonl] [--shown-only] --out report.md

Inputs are what convert_ratings.py wrote. The metrics, the golden loader and the
RankingResultUnit dataclasses are the repo's own (`tools/recall_harness`) and are
reused unchanged; only the driver here is probe-specific (the harness's own
runner drives a snapshot DB, which a live probe has none of).

Two modes: the default scores each arm's full ranking; --shown-only first truncates
each test RRU to the ids the rating request actually SHOWED for that step/arm (the
rater never saw the rest, so those ids are unrated, not neutral). `unshown_rate` in
the table says how much of an arm's output sat in that unrated region.

A test RRU whose step has no golden is reported as UNRATED and never scored.
Arms that were skipped or errored on a step (metadata.status) are excluded from
the scores and counted in the report, so a failed arm cannot masquerade as a
zero-recall arm.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def _repo_root() -> Path:
    """Nearest ancestor holding tools/recall_harness (never a fixed parents[N])."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "tools" / "recall_harness" / "rru.py").exists():
            return parent
    raise SystemExit("probe_eval: cannot find tools/recall_harness above " + __file__)


_ROOT = str(_repo_root())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.recall_harness.golden_loader import load_golden, load_golden_set  # noqa: E402
from tools.recall_harness.metrics import ranking  # noqa: E402,F401  (registers metrics)
from tools.recall_harness.metrics.registry import apply_metric  # noqa: E402
from tools.recall_harness.cli import METRICS_TO_APPLY, metric_key  # noqa: E402
from tools.recall_harness.rru import RankedNode, RankingResultUnit  # noqa: E402

_QUERY_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_KNOWN_ARMS = ("thinking_full", "thinking_tail", "extract_model", "tool_input", "novel_terms")

TOP_K = 5  # the "top-k" the inContext / principle rates are read over (= p@5's k)

# The golden grades the probe writes top out at 2 (good); the harness's default
# MRR threshold is 3, which would score every probe step 0. Same metric function,
# threshold lowered to the probe's top grade.
MRR_KWARGS = {"relevance_threshold": 2}


def _kwargs_for(name: str, kwargs: dict) -> dict:
    return {**kwargs, **MRR_KWARGS} if name == "mean_reciprocal_rank" else kwargs


def _load_test_rru(path: str) -> RankingResultUnit:
    try:
        with open(path) as f:
            data = json.load(f)
        nodes = [RankedNode(id=n["id"], rank=n["rank"], grading=n.get("grading"),
                            signals=n.get("signals"), notes=n.get("notes", "") or "")
                 for n in data["ranked_nodes"]]
        rru = RankingResultUnit(query_id=data["query_id"], query_text=data["query_text"],
                                ranked_nodes=nodes, metadata=data.get("metadata") or {})
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise SystemExit(f"probe_eval: cannot load test RRU {os.path.basename(path)}: {err!r}")
    # Defensive: refuse what convert_ratings.py would have refused (it writes
    # these files, so a hand-edited or foreign one is the only way to get here).
    name = os.path.basename(path)
    if not _QUERY_ID.fullmatch(str(rru.query_id)):
        raise SystemExit(f"probe_eval: {name} field query_id {rru.query_id!r} is not a "
                         "safe id (letters, digits, _ and -)")
    arm = rru.metadata.get("arm")
    # A known arm, or at least a safe name: the eval is a generic scorer and its
    # contract tests use synthetic arm names; path characters are what is refused.
    if arm not in _KNOWN_ARMS and not _SAFE_NAME.fullmatch(str(arm)):
        raise SystemExit(f"probe_eval: {name} metadata arm {arm!r} is not a safe arm name")
    return rru


def _percentile_nearest_rank(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def _median(values: list[float]):
    return statistics.median(values) if values else None


def _mean(values: list):
    present = [v for v in values if v is not None]
    return statistics.mean(present) if present else None


def _load_goldens(golden_dir: str) -> dict[str, RankingResultUnit]:
    # load_golden_set silently skips archived (`_v`) names and the pilot file;
    # a golden that vanishes unannounced is exactly the failure this guards.
    on_disk = sorted(glob.glob(os.path.join(golden_dir, "q_*.json")))
    goldens = load_golden_set(golden_dir=golden_dir)
    skipped = [os.path.basename(p) for p in on_disk
               if load_golden(p).query_id not in goldens]
    if skipped or len(goldens) != len(on_disk):
        raise SystemExit(f"probe_eval: {len(on_disk)} golden files on disk but "
                         f"{len(goldens)} loaded; skipped by the loader: {skipped}")
    return goldens


def evaluate(golden_dir: str, test_dir: str, pairs_path: str | None = None,
             shown_only: bool = False) -> dict:
    goldens = _load_goldens(golden_dir)
    test_rrus = [_load_test_rru(p) for p in sorted(glob.glob(os.path.join(test_dir, "*.json")))]

    if shown_only:
        for rru in test_rrus:
            shown = rru.metadata.get("shownIds")
            if shown is None:
                continue  # no shown-list for this step: nothing to truncate to
            keep = set(shown)
            rru.ranked_nodes = [RankedNode(id=n.id, rank=i, grading=n.grading,
                                           signals=n.signals, notes=n.notes)
                                for i, n in enumerate((n for n in rru.ranked_nodes if n.id in keep), start=1)]

    unrated = sorted({r.query_id for r in test_rrus if r.query_id not in goldens})
    by_arm: dict[str, list[RankingResultUnit]] = defaultdict(list)
    excluded_status: dict[str, int] = defaultdict(int)
    surfaced: dict[str, set[str]] = defaultdict(set)
    for rru in test_rrus:
        surfaced[rru.query_id].update(n.id for n in rru.ranked_nodes)
        arm = rru.metadata.get("arm", "unknown")
        if rru.query_id not in goldens:
            continue
        if rru.metadata.get("status", "ok") != "ok":
            excluded_status[arm] += 1
            continue
        by_arm[arm].append(rru)

    # golden rows the converter marked "unshown": surfaced, never shown, unrated
    unshown_ids = {qid: {n.id for n in g.ranked_nodes if n.notes == "unshown"}
                   for qid, g in goldens.items()}
    arms: dict[str, dict] = {}
    for arm in sorted(set(by_arm) | set(excluded_status)):
        rrus = by_arm.get(arm, [])
        per_metric: dict[str, list] = defaultdict(list)
        for rru in rrus:
            golden = goldens[rru.query_id]
            for name, kwargs in METRICS_TO_APPLY:
                result = apply_metric(name, rru, golden, **_kwargs_for(name, kwargs))
                per_metric[metric_key(name, kwargs)].append(result.get("value"))
        mss = [r.metadata.get("ms") for r in rrus if r.metadata.get("ms") is not None]
        rats = [r.metadata.get("msOverStep") for r in rrus if r.metadata.get("msOverStep") is not None]
        topk = [n for r in rrus for n in r.ranked_nodes[:TOP_K]]
        all_ids = [(r.query_id, n.id) for r in rrus for n in r.ranked_nodes]
        unshown = sum(1 for qid, i in all_ids if unshown_ids.get(qid) and i in unshown_ids[qid])
        flag = lambda key: (sum(1 for n in topk if (n.signals or {}).get(key)) / len(topk)) if topk else None  # noqa: E731
        arms[arm] = {
            "n_steps": len(rrus),
            "excluded_arm_runs": excluded_status.get(arm, 0),
            "precision_at_k_5": _mean(per_metric["precision_at_k_5"]),
            "recall_at_k_10": _mean(per_metric["recall_at_k_10"]),
            "ndcg_at_k_10": _mean(per_metric["ndcg_at_k_10"]),
            "mean_reciprocal_rank": _mean(per_metric["mean_reciprocal_rank"]),
            "median_ms": _median(mss),
            "p90_ms": _percentile_nearest_rank(mss, 90) if mss else None,
            "ms_excluded": len(rrus) - len(mss),
            "median_ms_over_step": _median(rats),
            "ms_over_step_excluded": len(rrus) - len(rats),
            "unshown_rate": (unshown / len(all_ids)) if all_ids else None,
            "in_context_rate": flag("inContext"),
            "principle_rate": flag("principle"),
        }

    pairs: dict[tuple[str, str], dict[str, int]] = {}
    if pairs_path and os.path.exists(pairs_path):
        with open(pairs_path) as f:
            for lineno, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    cell = pairs.setdefault((row["armA"], row["armB"]),
                                            {"better": 0, "same": 0, "worse": 0})
                    cell[row["verdict"]] += 1
                except (ValueError, KeyError) as err:
                    raise SystemExit(f"probe_eval: {pairs_path} line {lineno}: {err!r}")

    misses = {}
    for qid, golden in goldens.items():
        missed = sorted(i for i in golden.relevant_ids if i not in surfaced.get(qid, set()))
        if missed:
            misses[qid] = missed

    return {"shown_only": shown_only, "arms": arms, "pairs": pairs, "misses": misses, "unrated": unrated,
            "n_goldens": len(goldens), "n_test_rrus": len(test_rrus)}


def _fmt(value, spec=".3f"):
    return "-" if value is None else format(value, spec)


def render_report(result: dict) -> str:
    mode = ("shown-only (each ranking truncated to the ids the request showed)" if result["shown_only"]
            else "full (every surfaced id scored; unshown ids count as neutral)")
    lines = ["# Probe eval", "", f"mode: {mode}", "",
             f"goldens: {result['n_goldens']} · test RRUs: {result['n_test_rrus']} · "
             f"unrated steps (not scored): {len(result['unrated'])}", "",
             "## Per arm", "",
             "| arm | n steps | p@5 | r@10 | nDCG@10 | MRR | median ms | p90 ms | "
             "median msOverStep | unshown | inContext (top-5) | principle (top-5) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm, s in result["arms"].items():
        lines.append(
            f"| {arm} | {s['n_steps']} | {_fmt(s['precision_at_k_5'])} | {_fmt(s['recall_at_k_10'])} | "
            f"{_fmt(s['ndcg_at_k_10'])} | {_fmt(s['mean_reciprocal_rank'])} | "
            f"{_fmt(s['median_ms'], '.0f')} | {_fmt(s['p90_ms'], '.0f')} | "
            f"{_fmt(s['median_ms_over_step'])} | {_fmt(s['unshown_rate'])} | {_fmt(s['in_context_rate'])} | "
            f"{_fmt(s['principle_rate'])} |")
    lines += ["", "Latency excluded (value absent): " + (", ".join(
        f"{arm}: ms {s['ms_excluded']}, msOverStep {s['ms_over_step_excluded']}"
        for arm, s in result["arms"].items()) or "none"),
        "", "Arm runs excluded (skipped or errored): " + (", ".join(
            f"{arm}: {s['excluded_arm_runs']}" for arm, s in result["arms"].items()
            if s["excluded_arm_runs"]) or "none"),
        "", "## Pair verdicts (armA vs armB, from armA's side)", ""]
    if result["pairs"]:
        lines += ["| armA | armB | better | same | worse |", "|---|---|---|---|---|"]
        for (a, b), c in sorted(result["pairs"].items()):
            lines.append(f"| {a} | {b} | {c['better']} | {c['same']} | {c['worse']} |")
    else:
        lines.append("no pair verdicts")
    lines += ["", "## Arm misses (golden relevance-2 ids no arm surfaced)", ""]
    if result["misses"]:
        lines += [f"- {qid}: {', '.join(ids)}" for qid, ids in sorted(result["misses"].items())]
    else:
        lines.append("none")
    lines += ["", "## Unrated steps (no golden; not scored)", ""]
    lines += [f"- {qid}" for qid in result["unrated"]] or ["none"]
    return "\n".join(lines) + "\n"


def _write_private(path: Path, text: str) -> None:
    """Report file 0600 (it names every arm and query-derived figure); never
    Path.write_text, which is 0644 under umask 022. Replaces an existing report."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--golden", required=True, help="dir of q_*.json goldens")
    parser.add_argument("--test", required=True, help="dir of test RRU json files")
    parser.add_argument("--pairs", help="pairs.jsonl from convert_ratings.py")
    parser.add_argument("--shown-only", action="store_true",
                        help="truncate each test RRU to the ids the request showed before scoring")
    parser.add_argument("--out", required=True, help="markdown report path")
    args = parser.parse_args(argv)
    text = render_report(evaluate(args.golden, args.test, args.pairs, shown_only=args.shown_only))
    _write_private(Path(args.out), text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
