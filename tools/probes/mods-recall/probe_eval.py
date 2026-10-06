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

The live routes (#3458) are arms like the others: `route:prompt_surface` and
`route:tool_recall` rows appear in the same table. --signal-sweep re-ranks each route's
recorded candidates offline (a similarity floor sweep and an optional --formula over the
recorded signal names) and scores p@5 per setting against the same goldens.

A test RRU whose step has no golden is reported as UNRATED and never scored.
Arms that were skipped or errored on a step (metadata.status) are excluded from
the scores and counted in the report, so a failed arm cannot masquerade as a
zero-recall arm.
"""
from __future__ import annotations

import argparse
import ast
import glob
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
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
_KNOWN_ARMS = ("thinking_full", "thinking_tail", "extract_model", "tool_input", "novel_terms",
               "route:prompt_surface", "route:tool_recall")

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


def _skipped_by_reason(test_dir: str):
    """The converter's per-reason skipped-step counts, from the summary.json it
    writes beside test/ and golden/; None when that file is absent or unreadable
    (the report then says so rather than print a made-up "none")."""
    path = Path(test_dir).resolve().parent / "summary.json"
    try:
        counts = json.loads(path.read_text()).get("steps_skipped_by_reason")
    except (OSError, ValueError, AttributeError):
        return None
    return counts if isinstance(counts, dict) else None


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
            "by_query_source": dict(sorted(Counter(
                str(r.metadata.get("querySource")) for r in rrus).items())),
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
            "n_goldens": len(goldens), "n_test_rrus": len(test_rrus),
            "steps_skipped_by_reason": _skipped_by_reason(test_dir)}


# ---------------------------------------------------------------------------
# --signal-sweep (#3458): re-rank each live route's recorded candidates offline
# ---------------------------------------------------------------------------

FLOOR_SWEEP = [round(0.30 + 0.05 * i, 2) for i in range(9)]  # 0.30 .. 0.70
_FORMULA_FUNCS = {"abs": abs, "min": min, "max": max, "log": math.log, "exp": math.exp, "sqrt": math.sqrt}
_FORMULA_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow)


_FORMULA_MAX_POW_NESTING = 3


def _pow_depth(node) -> int:
    """The deepest chain of `**` operators in the tree (a `**` inside either operand of another counts)."""
    here = 1 if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow) else 0
    return here + max((_pow_depth(c) for c in ast.iter_child_nodes(node)), default=0)


def _parse_formula(formula: str):
    """Validate `formula` (an arithmetic expression over signal names) and return
    (compiled code, the signal names it uses). Refuses anything else, naming it."""
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as err:
        raise SystemExit(f"probe_eval: --formula {formula!r} is not a valid expression: {err.msg}")
    names: list[str] = []
    if _pow_depth(tree.body) > _FORMULA_MAX_POW_NESTING:
        raise SystemExit(f"probe_eval: --formula nests ** more than {_FORMULA_MAX_POW_NESTING} deep")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div,
                             ast.Mod, ast.Pow, ast.UAdd, ast.USub)):
            continue
        if isinstance(node, ast.BinOp) and isinstance(node.op, _FORMULA_BINOPS):
            if isinstance(node.op, ast.Pow) and not (
                    isinstance(node.right, ast.Constant) and abs(node.right.value) <= 10):
                raise SystemExit("probe_eval: --formula may only raise to a constant power of at most 10")
            continue
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _FORMULA_FUNCS and not node.keywords:
            continue
        if isinstance(node, ast.Name):
            if node.id not in _FORMULA_FUNCS and node.id not in names:
                names.append(node.id)
            continue
        raise SystemExit(f"probe_eval: --formula {formula!r} uses {type(node).__name__}, which is not "
                         f"allowed (numbers, signal names, + - * / % **, and {sorted(_FORMULA_FUNCS)})")
    # a function name used as a bare value is not a signal either
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FORMULA_FUNCS:
            parents_call = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and c.func is node]
            if not parents_call:
                raise SystemExit(f"probe_eval: --formula {formula!r} uses the function {node.id!r} without calling it")
    return compile(tree, "<formula>", "eval"), names


def _num_or_none(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def rerank_by_formula(candidates: list[dict], formula: str, known: set[str] | None = None):
    """Re-rank recorded candidates (dicts: id + signals) by `formula`, highest first, ties
    keeping the recorded order. A signal that is null/missing/non-numeric on a candidate is
    0 for that candidate, and the candidate is counted once in the returned null-row count.
    `known` = the signal names that exist on at least one candidate (default: derived from
    `candidates`); a name outside it is refused naming it."""
    code, names = _parse_formula(formula)
    present = known if known is not None else {k for c in candidates for k in c if k != "id"}
    for name in names:
        if name not in present:
            raise SystemExit(f"probe_eval: --formula names signal {name!r}, which no recorded candidate "
                             f"carries (recorded signals: {sorted(present)})")
    scored = []
    null_rows = 0
    for position, cand in enumerate(candidates):
        values, has_null = {}, False
        for name in names:
            v = _num_or_none(cand.get(name))
            if v is None:
                has_null = True
            values[name] = 0 if v is None else v
        null_rows += 1 if has_null else 0
        try:
            score = eval(code, {"__builtins__": {}}, {**_FORMULA_FUNCS, **values})  # noqa: S307 (validated AST)
        except (ArithmeticError, ValueError, TypeError) as err:
            raise SystemExit(f"probe_eval: --formula {formula!r} failed on candidate {cand.get('id')!r}: {err!r}")
        scored.append((-score, position, cand))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [c for _, _, c in scored], null_rows


def _p_at_5(rru: RankingResultUnit, nodes: list[RankedNode], golden: RankingResultUnit):
    renumbered = RankingResultUnit(
        query_id=rru.query_id, query_text=rru.query_text, metadata=rru.metadata,
        ranked_nodes=[RankedNode(id=n.id, rank=i, grading=n.grading, signals=n.signals, notes=n.notes)
                      for i, n in enumerate(nodes, start=1)])
    for name, kwargs in METRICS_TO_APPLY:
        if metric_key(name, kwargs) == "precision_at_k_5":
            return apply_metric(name, renumbered, golden, **_kwargs_for(name, kwargs)).get("value")
    raise SystemExit("probe_eval: the harness no longer computes precision_at_k_5")


def signal_sweep(golden_dir: str, test_dir: str, formula: str | None = None) -> dict:
    """For each route arm: p@5 against the goldens with the route's recorded candidates
    (a) as recorded, (b) cut at each similarity floor 0.30..0.70 (kept order), (c) re-ranked by
    `formula`. Returns {route arm: [row, ...]}; a row is {setting, n_steps, precision_at_k_5,
    null_signal_rows}. Steps without a golden are not scored."""
    goldens = _load_goldens(golden_dir)
    rrus = [_load_test_rru(p) for p in sorted(glob.glob(os.path.join(test_dir, "*.json")))]
    by_route: dict[str, list[RankingResultUnit]] = defaultdict(list)
    for rru in rrus:
        arm = str(rru.metadata.get("arm", ""))
        if arm.startswith("route:") and rru.query_id in goldens and rru.metadata.get("status", "ok") == "ok":
            by_route[arm].append(rru)
    if formula is not None:
        _parse_formula(formula)  # a bad formula is refused even when no route has data
    out: dict[str, list[dict]] = {}
    for route, items in sorted(by_route.items()):
        known = {k for r in items for n in r.ranked_nodes for k in (n.signals or {})}
        rows: list[dict] = []

        def score(settings_nodes) -> float | None:
            vals = [_p_at_5(r, nodes, goldens[r.query_id]) for r, nodes in settings_nodes]
            return _mean(vals)

        rows.append({"setting": "as recorded", "n_steps": len(items),
                     "precision_at_k_5": score([(r, r.ranked_nodes) for r in items]), "null_signal_rows": 0})
        sim_nulls = sum(1 for r in items for n in r.ranked_nodes
                        if _num_or_none((n.signals or {}).get("similarity")) is None)
        for floor in FLOOR_SWEEP:
            kept = [(r, [n for n in r.ranked_nodes
                         if (_num_or_none((n.signals or {}).get("similarity")) or 0) >= floor])
                    for r in items]
            rows.append({"setting": f"floor {floor:.2f}", "n_steps": len(items),
                         "precision_at_k_5": score(kept), "null_signal_rows": sim_nulls})
        if formula is not None:
            ranked, nulls = [], 0
            for r in items:
                cands = [{"id": n.id, **(n.signals or {})} for n in r.ranked_nodes]
                ordered, n_null = rerank_by_formula(cands, formula, known)
                nulls += n_null
                lookup = {n.id: n for n in r.ranked_nodes}
                ranked.append((r, [lookup[c["id"]] for c in ordered]))
            rows.append({"setting": f"formula {formula}", "n_steps": len(items),
                         "precision_at_k_5": score(ranked), "null_signal_rows": nulls})
        out[route] = rows
    return out


def render_sweep(result: dict, formula: str | None = None) -> str:
    lines = ["# Probe signal sweep", "",
             "Each route's recorded candidates re-ranked offline; p@5 against the same goldens "
             "(unrated candidates count as not relevant). A signal null on a candidate is 0 for it "
             "and counted in null_signal_rows.", ""]
    if formula is not None:
        lines += [f"formula: `{formula}`", ""]
    if not result:
        lines += ["no route arms with goldens (nothing to sweep)", ""]
    for route, rows in result.items():
        lines += [f"## {route}", "", "| setting | n steps | p@5 | null_signal_rows |", "|---|---|---|---|"]
        lines += [f"| {r['setting']} | {r['n_steps']} | {_fmt(r['precision_at_k_5'])} | {r['null_signal_rows']} |"
                  for r in rows]
        lines.append("")
    return "\n".join(lines) + "\n"


def _fmt(value, spec=".3f"):
    return "-" if value is None else format(value, spec)


def _skipped_line(counts) -> str:
    if counts is None:
        return "steps skipped by reason: unknown (summary.json absent or unreadable beside the test dir)"
    return "steps skipped by reason: " + (" · ".join(f"{r} {n}" for r, n in counts.items()) or "none")


def render_report(result: dict) -> str:
    mode = ("shown-only (each ranking truncated to the ids the request showed)" if result["shown_only"]
            else "full (every surfaced id scored; unshown ids count as neutral)")
    lines = ["# Probe eval", "", f"mode: {mode}", "",
             f"goldens: {result['n_goldens']} · test RRUs: {result['n_test_rrus']} · "
             f"unrated steps (not scored): {len(result['unrated'])}",
             _skipped_line(result.get("steps_skipped_by_reason")), "",
             "## Per arm", "",
             "| arm | n steps | p@5 | r@10 | nDCG@10 | MRR | median ms | p90 ms | "
             "median msOverStep | unshown | steps by querySource | inContext (top-5) | principle (top-5) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm, s in result["arms"].items():
        lines.append(
            f"| {arm} | {s['n_steps']} | {_fmt(s['precision_at_k_5'])} | {_fmt(s['recall_at_k_10'])} | "
            f"{_fmt(s['ndcg_at_k_10'])} | {_fmt(s['mean_reciprocal_rank'])} | "
            f"{_fmt(s['median_ms'], '.0f')} | {_fmt(s['p90_ms'], '.0f')} | "
            f"{_fmt(s['median_ms_over_step'])} | {_fmt(s['unshown_rate'])} | {', '.join(f'{k}: {v}' for k, v in s['by_query_source'].items()) or '-'} | {_fmt(s['in_context_rate'])} | "
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
    parser.add_argument("--signal-sweep", action="store_true",
                        help="re-rank each live route's recorded candidates (similarity floor sweep "
                             "0.30..0.70, plus --formula) and report p@5 per setting instead of the arm table")
    parser.add_argument("--formula",
                        help="with --signal-sweep: a python expression over the recorded signal names, "
                             "e.g. '0.6*similarity+0.4*utility'")
    parser.add_argument("--out", required=True, help="markdown report path")
    args = parser.parse_args(argv)
    if args.formula is not None and not args.signal_sweep:
        parser.error("--formula needs --signal-sweep")
    if args.signal_sweep:
        text = render_sweep(signal_sweep(args.golden, args.test, args.formula), args.formula)
    else:
        text = render_report(evaluate(args.golden, args.test, args.pairs, shown_only=args.shown_only))
    _write_private(Path(args.out), text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
