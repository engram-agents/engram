#!/usr/bin/env python3
"""Baseline summary for the antml-prefix swallow-pattern failure mode.

Reads two JSONL files written by the engram-toolcall-repair hook:
  - ~/.engram/toolcall-invocations.jsonl  (every mcp__engram__* call)
  - ~/.engram/toolcall-repairs.jsonl      (only calls that needed repair)

Computes baseline repair rates per tool and per day so that a future
migration (e.g. the telemetry-discipline lesson's antml-fix per-tool single-JSON reshape) can be
empirically validated against pre-migration error rates.

Usage:
    python3 tools/antml_repair_baseline.py [--since YYYY-MM-DD] [--by-tool] [--by-day]

Without flags, prints an overall summary plus per-tool and per-day breakdowns.

Notes:
  - The invocation log was added 2026-05-06; entries before that date will
    only appear in the repair log. Baseline rate computation should use
    --since 2026-05-06 once enough post-instrumentation data accumulates.
  - All data is local — no network calls, no shared state.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

ENGRAM_HOME = Path(
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)
INVOCATION_LOG = Path(
    os.environ.get("ENGRAM_INVOCATION_LOG", str(ENGRAM_HOME / "toolcall-invocations.jsonl"))
)
REPAIR_LOG = Path(
    os.environ.get("ENGRAM_REPAIR_LOG", str(ENGRAM_HOME / "toolcall-repairs.jsonl"))
)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _parse_date(stamp: str) -> date | None:
    try:
        return datetime.fromisoformat(stamp).date()
    except (ValueError, TypeError):
        return None


def _filter_since(entries: list[dict], since: date | None) -> list[dict]:
    if since is None:
        return entries
    return [e for e in entries if (d := _parse_date(e.get("timestamp", ""))) and d >= since]


def _short(tool_name: str) -> str:
    return tool_name.replace("mcp__engram__", "")


def overall(invocations: list[dict], repairs: list[dict]) -> None:
    total = len(invocations)
    repaired = sum(1 for e in invocations if e.get("repaired"))
    legacy_repairs_no_invocation = max(0, len(repairs) - repaired)
    rate = (repaired / total * 100) if total else 0.0
    print("=== Overall ===")
    print(f"  invocations:                    {total:>6,}")
    print(f"  repaired (numerator):           {repaired:>6,}")
    print(f"  repair rate:                    {rate:>6.2f}%")
    if legacy_repairs_no_invocation:
        print(f"  legacy repairs (pre-counter):   {legacy_repairs_no_invocation:>6,}  (in repair log but predate invocation counter)")


def by_tool(invocations: list[dict]) -> None:
    if not invocations:
        return
    totals: Counter[str] = Counter()
    repaired: Counter[str] = Counter()
    for e in invocations:
        t = e.get("tool_name", "?")
        totals[t] += 1
        if e.get("repaired"):
            repaired[t] += 1
    print("\n=== By tool ===")
    print(f"  {'tool':<32} {'calls':>8} {'repairs':>8} {'rate':>7}")
    for t, n in totals.most_common():
        r = repaired[t]
        rate = (r / n * 100) if n else 0.0
        print(f"  {_short(t):<32} {n:>8,} {r:>8,} {rate:>6.2f}%")


def by_day(invocations: list[dict]) -> None:
    if not invocations:
        return
    daily_total: dict[date, int] = defaultdict(int)
    daily_repaired: dict[date, int] = defaultdict(int)
    for e in invocations:
        d = _parse_date(e.get("timestamp", ""))
        if d is None:
            continue
        daily_total[d] += 1
        if e.get("repaired"):
            daily_repaired[d] += 1
    print("\n=== By day ===")
    print(f"  {'date':<12} {'calls':>8} {'repairs':>8} {'rate':>7}")
    for d in sorted(daily_total):
        n = daily_total[d]
        r = daily_repaired[d]
        rate = (r / n * 100) if n else 0.0
        print(f"  {d.isoformat():<12} {n:>8,} {r:>8,} {rate:>6.2f}%")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--since", type=str, default=None, help="Filter to entries on/after this ISO date.")
    p.add_argument("--by-tool", action="store_true", help="Show only per-tool breakdown.")
    p.add_argument("--by-day", action="store_true", help="Show only per-day breakdown.")
    args = p.parse_args()

    since = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since).date()
        except ValueError:
            print(f"error: --since must be ISO YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
            return 2

    invocations = _filter_since(_read_jsonl(INVOCATION_LOG), since)
    repairs = _filter_since(_read_jsonl(REPAIR_LOG), since)

    if not invocations and not repairs:
        print("(no data — has the hook fired yet?)")
        return 0

    if args.by_tool:
        by_tool(invocations)
    elif args.by_day:
        by_day(invocations)
    else:
        overall(invocations, repairs)
        by_tool(invocations)
        by_day(invocations)
    return 0


if __name__ == "__main__":
    sys.exit(main())
