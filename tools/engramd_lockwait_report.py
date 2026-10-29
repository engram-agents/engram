#!/usr/bin/env python3
"""tools/engramd_lockwait_report.py -- the read-only report on the engramd owner's per-day read-lock
wait counters (#4234, part of #3740 / #2652).

The owner writes `lockwait-YYYY-MM-DD.json` into its run dir (the directory holding the socket): per
request family, how many reads took `_client_read_lock`, how many of them waited more than 1 ms in the
acquire, the summed wait, a fixed-bucket histogram of the waited reads and its p95. This tool prints
those files; it never writes, never connects to the owner.

    python tools/engramd_lockwait_report.py --days 3 [--run-dir DIR]

For the last N UTC days present it prints a per-family table (reads, waited_reads, waited share,
wait_ms_sum, p95, timed_out, no_time_before_acquire), then one line per day:
`tripwire waited/reads = a/b`, and the same for every other family whose name contains `tripwire`
(a template shared between files is counted under a joined family such as `idf+tripwire`; the pure
`tripwire` family alone is not representative of every tripwire read site).

The default run dir is `<ENGRAM_HOME>/run`, resolved as the other engramd tools resolve it
(tools/_owner_read.engram_home(), then engramd_client.socket_path_for_home).

Exit 0 on success, and also when there are no lockwait files ("no lockwait files in <dir>"); exit 2
on a bad argument or a malformed file, with the file and the reason named on stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = 1
EDGE_KEYS = ["1", "2", "5", "10", "20", "50", "100", "200", "500", "1000", "2000", "inf"]
EDGES_MS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, "inf"]
COUNT_FIELDS = ("reads", "waited_reads", "timed_out", "no_time_before_acquire")


class MalformedFile(Exception):
    pass


def default_run_dir() -> Path:
    """<ENGRAM_HOME>/run -- the directory the owner's socket lives in. The repo root is appended to
    sys.path (never inserted, #2565) so `tools` resolves as it does under pytest; a relative
    ENGRAM_HOME raises ValueError, which main() turns into a refusal."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.append(str(REPO_ROOT))
    from tools import _owner_read as owner_read
    home = owner_read.engram_home()
    try:
        client = owner_read.import_runtime("engramd_client")
        return client.socket_path_for_home(home).parent
    except ImportError:
        return home / "run"


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_file(path: Path) -> Tuple[str, Dict[str, dict]]:
    """-> (date, {family: entry}); raises MalformedFile naming the reason."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MalformedFile(f"cannot read: {exc}")
    except ValueError as exc:
        raise MalformedFile(f"not valid JSON ({exc})")
    if not isinstance(doc, dict):
        raise MalformedFile("top level is not an object")
    if doc.get("schema") != SCHEMA:
        raise MalformedFile(f"schema is {doc.get('schema')!r}, this tool reads schema {SCHEMA}")
    families = doc.get("families")
    if not isinstance(families, dict):
        raise MalformedFile("families is missing or not an object")
    date = path.name[len("lockwait-"):-len(".json")]
    for name, entry in families.items():
        if not isinstance(entry, dict):
            raise MalformedFile(f"family {name!r} is not an object")
        for field in COUNT_FIELDS:
            if not _is_count(entry.get(field)):
                raise MalformedFile(f"family {name!r}: {field} is missing or not a non-negative integer")
        total = entry.get("wait_ms_sum")
        if not isinstance(total, (int, float)) or isinstance(total, bool) or total < 0:
            raise MalformedFile(f"family {name!r}: wait_ms_sum is missing or not a non-negative number")
        hist = entry.get("wait_hist")
        if not isinstance(hist, dict) or set(hist) != set(EDGE_KEYS) or not all(_is_count(v) for v in hist.values()):
            raise MalformedFile(f"family {name!r}: wait_hist is missing or does not carry exactly the fixed bucket edges")
        if "wait_ms_p95" not in entry:
            raise MalformedFile(f"family {name!r}: wait_ms_p95 is missing")
    return date, families


def _fmt_p95(value) -> str:
    return "-" if value is None else str(value)


def render_day(date: str, families: Dict[str, dict]) -> List[str]:
    lines = [f"== {date} =="]
    header = f"{'family':<28} {'reads':>8} {'waited_reads':>12} {'waited%':>8} {'wait_ms_sum':>12} {'p95_ms':>7} {'timed_out':>9} {'no_time_before_acquire':>22}"
    lines.append(header)
    for name in sorted(families):
        e = families[name]
        share = "-" if e["reads"] == 0 else f"{100.0 * e['waited_reads'] / e['reads']:.1f}%"
        lines.append(f"{name:<28} {e['reads']:>8} {e['waited_reads']:>12} {share:>8} {e['wait_ms_sum']:>12.3f} "
                     f"{_fmt_p95(e['wait_ms_p95']):>7} {e['timed_out']:>9} {e['no_time_before_acquire']:>22}")
    tripwire_names = ["tripwire"] + sorted(n for n in families if "tripwire" in n and n != "tripwire")
    for name in tripwire_names:
        e = families.get(name)
        lines.append(f"{name} waited/reads = {e['waited_reads'] if e else 0}/{e['reads'] if e else 0}")
    return lines


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer > 0 (got {text!r})")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be an integer > 0 (got {value})")
    return value


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Report the engramd owner's per-day read-lock wait counters (read-only).")
    p.add_argument("--days", type=_positive_int, required=True, metavar="N", help="the last N UTC days present")
    p.add_argument("--run-dir", type=Path, default=None, metavar="DIR",
                   help="the directory holding the lockwait-*.json files (default: the owner's run dir)")
    args = p.parse_args(argv)
    if args.run_dir is not None:
        run_dir = args.run_dir
    else:
        try:
            run_dir = default_run_dir()
        except ValueError as exc:
            print(f"engramd_lockwait_report: error -- {exc}", file=sys.stderr)
            return 2
    files = sorted(run_dir.glob("lockwait-????-??-??.json")) if run_dir.is_dir() else []
    if not files:
        print(f"no lockwait files in {run_dir}")
        return 0
    out: List[str] = []
    for path in files[-args.days:]:
        try:
            date, families = parse_file(path)
        except MalformedFile as exc:
            print(f"engramd_lockwait_report: error -- {path.name}: {exc}", file=sys.stderr)
            return 2
        out.extend(render_day(date, families))
        out.append("")
    print("\n".join(out).rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
