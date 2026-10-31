#!/usr/bin/env python3
"""One markdown fleet report across several probe sessions (engram-alpha#3549).

    ~/.engram/venv/bin/python probe_fleet_report.py <converter-out-dir> [<dir> ...] \
        --label <seat>:<yyyy-mm-dd> [--label ...] [--shown-only] --out fleet.md

Each input is a `convert_ratings.py --out` dir (golden/, test/, pairs.jsonl,
summary.json). Labels are given once per dir, in the same order. A dir whose
summary.json itself carries `seat` and `date` needs no label; a dir with neither
is an error naming it (there is no default seat). The scoring is probe_eval's,
imported by path from this folder (`evaluate`, `signal_sweep`); no metric code
lives here, and probe_eval is not modified.

Sections: (1) arm x (seat:date) tables for p@5, nDCG@10, median ms, cell =
value (n); a cell with n < 10 shows the value in parentheses too and is never
named in a "best arm" line; (2) the querySource split per arm and column;
(3) the route signal sweep, per route two tables with their populations named: 3a pooled
over EVERY input, 3b pooled over the inputs with no null similarity (excluded inputs and null
rows counted, never dropped silently); the sweep never runs --shown-only;
(4) pair verdicts pooled; (5) caveats rendered from each input's own data; (6) the automatic inContext rate,
only when some flag is known; (7) the rater's `in_context` share per arm, only when some input offered the label.

Output is deterministic: columns sort by (date, seat), rows by arm name, so the
same inputs in any CLI order give the same bytes.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import re
import sys
from pathlib import Path

MIN_N = 10  # a cell with fewer steps than this is shown in parentheses and never "best"

_HERE = Path(__file__).resolve().parent
_LABEL = re.compile(r"^([A-Za-z0-9_-]+):(\d{4}-\d{2}-\d{2})$")
_SEAT = re.compile(r"^[A-Za-z0-9_-]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _load_probe_eval():
    """probe_eval.py sits beside this file in a hyphenated folder, so it is imported by
    path (it resolves tools/recall_harness itself by walking parents)."""
    path = _HERE / "probe_eval.py"
    spec = importlib.util.spec_from_file_location("probe_eval_for_fleet", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"probe_fleet_report: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_PE = _load_probe_eval()


def _die(msg: str):
    raise SystemExit(f"probe_fleet_report: {msg}")


def _valid_date(text: str) -> bool:
    if not _DATE.fullmatch(text):
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _read_summary(root: Path) -> dict:
    try:
        data = json.loads((root / "summary.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _one_line(value, limit=80) -> str:
    return " ".join(str(value).split())[:limit]


def _resolve_label(root: Path, label, summary: dict) -> tuple[str, str]:
    if label is not None:
        m = _LABEL.fullmatch(label)
        if not m or not _valid_date(m.group(2)):
            _die(f"{root}: label {label!r} is not <seat>:<yyyy-mm-dd> (seat letters, digits, _ and -; ISO date)")
        return m.group(1), m.group(2)
    seat, date = summary.get("seat"), summary.get("date")
    if isinstance(seat, str) and _SEAT.fullmatch(seat) and isinstance(date, str) and _valid_date(date):
        return seat, date
    _die(f"{root}: no --label for this dir and its summary.json does not carry a valid seat and date")


def parse_sidecar_specs(specs: list[str]) -> dict[str, str]:
    """['SEAT:DATE=DIR', ...] -> {'SEAT:DATE': 'DIR'}; a malformed or repeated spec is an error naming it."""
    out: dict[str, str] = {}
    for spec in specs:
        col, sep, path = spec.partition("=")
        m = _LABEL.fullmatch(col)
        if not sep or not path or not m or not _valid_date(m.group(2)):
            _die(f"--in-context-sidecar {spec!r} is not <seat>:<yyyy-mm-dd>=<dir>")
        if col in out:
            _die(f"--in-context-sidecar given twice for {col}")
        out[col] = path
    return out


def load_inputs(dirs: list[str], labels: list[str], shown_only: bool,
                sidecars: dict[str, str] | None = None) -> list[dict]:
    if len(labels) > len(dirs):
        _die(f"{len(labels)} --label values for {len(dirs)} input dirs")
    sidecars = dict(sidecars or {})
    inputs, seen = [], {}
    for i, d in enumerate(dirs):
        root = Path(d)
        if not root.is_dir():
            _die(f"{root}: not a directory")
        gdir, tdir = root / "golden", root / "test"
        if not sorted(gdir.glob("q_*.json")):
            _die(f"{root}: no goldens (golden/q_*.json) -- an unrated or empty converter dir")
        if not sorted(tdir.glob("*.json")):
            _die(f"{root}: no test RRUs (test/*.json)")
        summary = _read_summary(root)
        seat, date = _resolve_label(root, labels[i] if i < len(labels) else None, summary)
        key = (seat, date)
        if key in seen:
            _die(f"{root}: duplicate {seat}:{date} (already used by {seen[key]})")
        seen[key] = root
        pairs_path = root / "pairs.jsonl"
        try:
            result = _PE.evaluate(str(gdir), str(tdir), str(pairs_path), shown_only=shown_only,
                                  in_context_sidecar=sidecars.pop(f"{seat}:{date}", None))
            sweep = _PE.signal_sweep(str(gdir), str(tdir))
        except SystemExit as err:
            _die(f"{root}: {err.code}")
        inputs.append({"root": root, "seat": seat, "date": date, "summary": summary,
                       "result": result, "sweep": sweep, "has_pairs": pairs_path.exists(), "labelled": i < len(labels),
                       "col": f"{seat}:{date}"})
    if sidecars:
        _die(f"--in-context-sidecar {sorted(sidecars)[0]} is not the seat:date of any input "
             f"(inputs: {', '.join(f'{s}:{d}' for s, d in seen)})")
    inputs.sort(key=lambda x: (x["date"], x["seat"]))
    return inputs


# ----------------------------------------------------------------------------- rendering
def _num(value, spec=".3f") -> str:
    text = format(value, spec)
    return text[1:] if text.startswith("0.") else text


def _cell_fmt(value, n, spec) -> str:
    if value is None:
        return "-"
    text = _num(value, spec)
    return f"({text}) ({n})" if n < MIN_N else f"{text} ({n})"


def _table(inputs, arms, cell_of) -> list[str]:
    lines = ["| arm | " + " | ".join(x["col"] for x in inputs) + " |",
             "|---|" + "---|" * len(inputs)]
    for arm in arms:
        lines.append(f"| {arm} | " + " | ".join(cell_of(x["result"]["arms"].get(arm)) for x in inputs) + " |")
    return lines


def _best_line(label: str, inputs, arms, key: str) -> str:
    parts = []
    for x in inputs:
        eligible = [(a, s) for a in arms if (s := x["result"]["arms"].get(a)) is not None
                    and s[key] is not None and s["n_steps"] >= MIN_N]
        if not eligible:
            parts.append(f"{x['col']} none (no arm with n >= {MIN_N})")
            continue
        top = max(s[key] for _, s in eligible)
        winners = [f"{a} {_num(s[key])} ({s['n_steps']})" for a, s in eligible if s[key] == top]
        parts.append(f"{x['col']} " + ", ".join(winners))
    return f"best {label} per column: " + " · ".join(parts)


def _query_source_cell(s) -> str:
    if not s or not s["by_query_source"]:
        return "-"
    return ", ".join(f"{'none' if k == 'None' else k}: {v}" for k, v in s["by_query_source"].items())


def _mixed_source_lines(inputs) -> list[str]:
    """The mixed-querySource warnings (probe_eval's wording): per input in input order, each led by its
    `[seat:date] `, then pooled over all inputs, led by `[pooled] `. Proxy arms only (see probe_eval)."""
    lines, pooled = [], {}
    for x in inputs:
        per_arm = {arm: s["proxy_sources"] for arm, s in x["result"]["arms"].items()}
        lines += _PE.mixed_source_warnings(per_arm, prefix=f"[{x['col']}] ")
        for arm, counts in per_arm.items():
            into = pooled.setdefault(arm, {})
            for src, n in counts.items():
                into[src] = into.get(src, 0) + n
    return lines + _PE.mixed_source_warnings(pooled, prefix="[pooled] ")


def _route_nulls(rows) -> int:
    """Null-similarity candidates an input's route recorded. probe_eval reports 0 on its as-recorded row
    by its own semantics, so the count is read off the floor rows (identical on every floor)."""
    return max((r["null_signal_rows"] for r in rows if r["setting"].startswith("floor")), default=0)


def _pool_sweep(inputs) -> dict:
    """{route: {"all": [(setting, pooled p@5 or None, n steps, inputs pooled, null rows counted)],
                "clean": [(setting, pooled p@5 or None, n steps, inputs pooled, inputs excluded, null rows excluded)]}}.
    "all" pools EVERY input (nothing excluded; probe_eval's own per-input rows already count a null similarity
    as 0 on a floor). "clean" pools only the inputs with no null similarity on the route, the SAME inputs in
    every row (as recorded included), and counts the excluded inputs and their null rows."""
    routes = sorted({r for x in inputs for r in x["sweep"]})
    out = {}
    for route in routes:
        settings: list[str] = []
        for x in inputs:
            for row in x["sweep"].get(route, []):
                if row["setting"] not in settings:
                    settings.append(row["setting"])
        everything, clean = [], []
        for setting in settings:
            num = n = pooled = null_counted = 0
            cnum = cn = cpooled = excluded = null_excluded = 0
            for x in inputs:
                rows = x["sweep"].get(route, [])
                row = next((r for r in rows if r["setting"] == setting), None)
                if row is None:
                    continue
                if row["precision_at_k_5"] is not None:
                    num += row["precision_at_k_5"] * row["n_steps"]
                    n += row["n_steps"]
                    pooled += 1
                null_counted += row["null_signal_rows"]
                nulls = _route_nulls(rows)
                if nulls > 0:
                    excluded += 1
                    null_excluded += nulls
                elif row["precision_at_k_5"] is not None:
                    cnum += row["precision_at_k_5"] * row["n_steps"]
                    cn += row["n_steps"]
                    cpooled += 1
            everything.append((setting, (num / n) if n else None, n, pooled, null_counted))
            clean.append((setting, (cnum / cn) if cn else None, cn, cpooled, excluded, null_excluded))
        out[route] = {"all": everything, "clean": clean}
    return out


def _pool_pairs(inputs) -> dict:
    pooled: dict[tuple[str, str], dict[str, int]] = {}
    for x in inputs:
        for key, counts in x["result"]["pairs"].items():
            cell = pooled.setdefault(key, {"better": 0, "same": 0, "worse": 0})
            for verdict, k in counts.items():
                cell[verdict] += k
    return pooled


def render(inputs: list[dict], shown_only: bool) -> str:
    arms = sorted({a for x in inputs for a in x["result"]["arms"]})
    mode = ("shown-only (each ranking truncated to the ids the request showed)" if shown_only
            else "full (every surfaced id scored; unshown ids count as neutral)")
    lines = ["# Probe fleet report", "", f"mode: {mode}",
             f"inputs: {len(inputs)} · columns ordered by date, then seat", "",
             "## 1. Per-arm tables (arm x seat:date)", "",
             f"Cell = value (n steps). A cell with n < {MIN_N} shows its value in parentheses too and is never "
             "named in a best-arm line.", "", "### p@5", ""]
    lines += _table(inputs, arms, lambda s: "-" if s is None else
                    _cell_fmt(s["precision_at_k_5"], s["n_steps"], ".3f"))
    lines += ["", _best_line("p@5", inputs, arms, "precision_at_k_5"), "", "### nDCG@10", ""]
    lines += _table(inputs, arms, lambda s: "-" if s is None else
                    _cell_fmt(s["ndcg_at_k_10"], s["n_steps"], ".3f"))
    lines += ["", _best_line("nDCG@10", inputs, arms, "ndcg_at_k_10"), "", "### median ms (n = steps with a latency)", ""]
    lines += _table(inputs, arms, lambda s: "-" if s is None else
                    _cell_fmt(s["median_ms"], s["n_steps"] - s["ms_excluded"], ".0f"))

    lines += ["", "## 2. querySource split (steps per arm; the day-2 numbers differ by seat because of it)", ""]
    lines += _table(inputs, arms, lambda s: _query_source_cell(s))
    warned = _mixed_source_lines(inputs)
    if warned:
        lines += [""] + warned

    lines += ["", "## 3. Signal sweep (route arms; p@5 pooled over inputs, two populations per route)", "",
              "The sweep scores the full recorded candidates; it never runs with --shown-only, "
              "whatever the flag says.", ""]
    sweep = _pool_sweep(inputs)
    if not sweep:
        lines.append("no route arms with goldens in any input (nothing to sweep)")
    for route, tables in sweep.items():
        clean = tables["clean"]
        lines += [f"### {route}", "",
                  "#### 3a. pooled over EVERY input (nothing excluded; a null similarity counts as 0 on a floor, "
                  "as probe_eval does; weighted by steps)", "",
                  "| setting | pooled p@5 | n steps pooled | inputs pooled | null rows counted |",
                  "|---|---|---|---|---|"]
        lines += [f"| {s} | {'-' if p is None else _num(p)} | {n} | {k} | {nr} |"
                  for s, p, n, k, nr in tables["all"]]
        lines += ["", "#### 3b. pooled over inputs with NO null similarity on this route (same inputs in every row, "
                  "as recorded included, so the rows compare top to bottom)", ""]
        if clean and clean[0][3] == 0:
            lines.append(f"no input qualifies: every input has a null similarity on this route "
                         f"({clean[0][4]} excluded, {clean[0][5]} null rows)")
        else:
            lines += ["| setting | pooled p@5 | n steps pooled | inputs pooled | inputs excluded (null signals) | "
                      "null rows excluded |", "|---|---|---|---|---|---|"]
            lines += [f"| {s} | {'-' if p is None else _num(p)} | {n} | {k} | {ex} | {nr} |"
                      for s, p, n, k, ex, nr in clean]
        lines.append("")
    if sweep:
        lines.pop()

    lines += ["", "## 4. Pair verdicts (pooled; armA vs armB from armA's side)", ""]
    if not any(x["has_pairs"] for x in inputs):
        lines.append("skipped: no input has pairs.jsonl")
    else:
        pooled = _pool_pairs(inputs)
        if pooled:
            lines += ["| armA | armB | better | same | worse | n |", "|---|---|---|---|---|---|"]
            lines += [f"| {a} | {b} | {c['better']} | {c['same']} | {c['worse']} | {sum(c.values())} |"
                      for (a, b), c in sorted(pooled.items())]
        else:
            lines.append("no pair verdicts in the inputs that have pairs.jsonl")

    dates = [x["date"] for x in inputs]
    lines += ["", "## 5. Caveats (rendered from each input's data)", "",
              f"date range across inputs: {dates[0]} .. {dates[-1]}", ""]
    for x in inputs:
        res, summary = x["result"], x["summary"]
        rated = summary.get("steps_rated")
        excluded = ", ".join(f"{a} {s['excluded_arm_runs']}" for a, s in res["arms"].items()
                             if s["excluded_arm_runs"]) or "none"
        rater = summary.get("rater")
        rater_text = (f"rater: {_one_line(rater)}" if isinstance(rater, str) and rater.strip()
                      else "rater provenance not recorded in this input")
        override = ""
        if x["labelled"]:
            sseat, sdate = summary.get("seat"), summary.get("date")
            if (isinstance(sseat, str) or isinstance(sdate, str)) and (sseat, sdate) != (x["seat"], x["date"]):
                override = (f" · label overrides summary seat/date (summary: "
                            f"{sseat if isinstance(sseat, str) else '?'}:{sdate if isinstance(sdate, str) else '?'})")
        lines.append(f"- {x['col']} — date {x['date']} · steps rated "
                     f"{rated if isinstance(rated, int) and not isinstance(rated, bool) else 'unknown'} · "
                     f"goldens {res['n_goldens']} · arms: {', '.join(res['arms']) or 'none'} · "
                     f"arm runs excluded by status: {excluded} · {rater_text}{override}")
        m, u = res["sidecar_matched"], res["sidecar_unmatched"]
        lines.append(f"  - in-context flag, {x['col']}: " + (
            f"offline sidecar (matched {m} of {m + u} route steps) for route rows · inContext for arm rows"
            if res["in_context_source"] == "offline sidecar"
            else "ledger/hook value for route rows · inContext for arm rows"))
    if has_known_in_context(inputs):
        lines += _render_in_context(inputs, arms)
    if any(x["result"].get("labels_offered") for x in inputs):
        lines += _render_rater_share(inputs, arms)
    return "\n".join(lines) + "\n"


def _render_rater_share(inputs, arms) -> list[str]:
    """The rater's exclusive `in_context` mark (NOT section 6's automatic inContext flag), present only when
    some input offered the label. An input whose goldens never offered it reads the em dash, never 0."""
    def cell(s):
        if s is None:
            return "-"
        if s["rater_in_context_share"] is None:
            return "—"
        # thin evidence is counted in STEPS (as MIN_N is everywhere else), not in shown ids: about 5 ids per arm
        # per step would make a cell look plain at two steps
        text = _num(s["rater_in_context_share"], ".3f")
        n = f"{s['rater_in_context_n']} ids, {s['rater_in_context_steps']} steps"
        return f"({text}) ({n})" if s["rater_in_context_steps"] < MIN_N else f"{text} ({n})"

    lines = ["", "## 7. in_context share per arm per seat:date (cell = share (n shown ids, n steps))", "",
             "The rater's own `in_context` mark (\"already in my context window when it was shown; would not have "
             "helped whatever its relevance\"): shown ids marked in_context over shown ids, pooled over the steps "
             f"whose ratings offered the label. A cell over fewer than {MIN_N} steps shows its value in parentheses too. "
             "`—` = no step offered the label, or no id was shown; `-` = the input has no such arm. "
             "This is not section 6's automatic inContext flag.", ""]
    lines += _table(inputs, arms, cell)
    return lines


def has_known_in_context(inputs) -> bool:
    """True when at least one arm in at least one input has a step with a known in-context flag."""
    return any(s["in_context_n"] for x in inputs for s in x["result"]["arms"].values())


def _render_in_context(inputs, arms) -> list[str]:
    lines = ["", "## 6. In-context rate (top-5 of each step)", "",
             f"Cell = share of the top-5 candidates whose in-context flag is known and true, over the known ones "
             f"(n steps contributing at least one known flag). A cell with n < {MIN_N} shows its value in "
             "parentheses too. `-` = no known flag.", ""]
    lines += _table(inputs, arms, lambda s: "-" if s is None or s["in_context_rate"] is None else
                    _cell_fmt(s["in_context_rate"], s["in_context_n"], ".3f"))
    lines.append("")
    for x in inputs:
        res = x["result"]
        if res["in_context_source"] == "offline sidecar":
            src = (f"offline sidecar (route rows: matched {res['sidecar_matched']}, "
                   f"unmatched {res['sidecar_unmatched']}; arm rows: inContext)")
        else:
            src = "ledger/hook value (route rows) · inContext (arm rows)"
        lines.append(f"- {x['col']}: flag source: {src}")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dirs", nargs="+", help="converter output dirs (convert_ratings.py --out)")
    parser.add_argument("--label", action="append", default=[], metavar="SEAT:DATE",
                        help="seat:yyyy-mm-dd for the dir at the same position; repeat once per dir")
    parser.add_argument("--shown-only", action="store_true",
                        help="score each ranking truncated to the ids the rating request showed")
    parser.add_argument("--in-context-sidecar", action="append", default=[], metavar="SEAT:DATE=DIR",
                        help="recompute_in_context.py output dir for the input labelled SEAT:DATE; repeatable. "
                             "That input's route rows read in_context_offline instead of the ledger value")
    parser.add_argument("--out", required=True, help="markdown report path")
    args = parser.parse_args(argv)
    out_dir = Path(args.out).resolve().parent
    if not out_dir.is_dir():
        _die(f"--out {args.out}: directory {out_dir} does not exist (create it first)")
    inputs = load_inputs(args.dirs, args.label, args.shown_only, parse_sidecar_specs(args.in_context_sidecar))
    if not has_known_in_context(inputs):
        print("probe_fleet_report: Section 6 omitted: no known in-context flag in any input", file=sys.stderr)
    try:
        _PE._write_private(Path(args.out), render(inputs, args.shown_only))
    except OSError as err:
        _die(f"cannot write {args.out}: {err.strerror or err}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
