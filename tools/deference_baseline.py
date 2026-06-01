#!/usr/bin/env python3
"""Baseline summary for deference-detector hook fires.

Reads the append-only text log written by the engram-deference-detector-stop
hook (~/.engram/deference-detector.log) and reports per-fire rates over time
so the qu_NNNN longitudinal validation thread (~30-day window) has a
queryable view of the wait-for-approval reflex's trajectory.

Usage:
    python3 tools/deference_baseline.py [--since YYYY-MM-DD] [--by-day] [--by-label] [--by-category]

Without flags, prints overall + by-category + by-label + by-day breakdowns.

Log format (two line shapes):
  Hit fires:    [TS] Detected N hit(s) (M unique): [labels]
  No-hit fires: [TS] Scanned: 0 hits  (also: descriptive variants)

The no-hit "heartbeat" form was added 2026-05-07 alongside the stdin-based
JSONL-flush-race fix in the hook (ob_NNNN / ls_NNNN). Entries logged BEFORE
2026-05-07 only contain hit lines, so per-fire rates are only computable
from data on/after 2026-05-07. For older windows, the script still reports
absolute hit counts.

Notes:
  - All data is local — no network calls, no shared state.
  - Label categorization (phrase-vs-intent) is hardcoded against the rule sets
    in hooks/claude/engram-deference-detector-stop.py; if those change, update
    _PHRASE_LABELS / _INTENT_LABELS here too.
"""

import argparse
import ast
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

ENGRAM_HOME = Path(
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)
LOG_PATH = Path(
    os.environ.get("ENGRAM_DEFERENCE_LOG", str(ENGRAM_HOME / "deference-detector.log"))
)

# Mirrors hooks/claude/engram-deference-detector-stop.py rule sets.
_PHRASE_LABELS = {
    "let-me-know-if", "should-i-q", "do-you-want-me", "want-me-to-q",
    "shall-i", "if-youd-like-i", "if-you-want-i", "confirm-before",
    "or-should-i", "do-you-prefer", "want-me-to",
}
_INTENT_LABELS = {
    "doing-it-now", "starting-now", "ill-add-fix-etc", "let-me-verb",
    "going-to-verb", "moving-on-to", "diving-in", "next-up", "ill-get-to",
    "on-it", "next-pass-will", "when-X-fires-ill", "ill-pick-X-next",
    "preserved-for",
}

_HIT_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\] Detected (?P<n>\d+) hit\(s\) \((?P<u>\d+) unique\): (?P<labels>\[.*\])\s*$"
)
# Heartbeat form (2026-05-07+): every Stop fire logs a line, no-hit fires
# emit "Scanned: 0 hits" (with optional descriptive suffix). Lets us compute
# true per-fire rates rather than absolute counts.
_NOHIT_RE = re.compile(r"^\[(?P<ts>[^\]]+)\] Scanned: 0 hits")


def _parse_log(path: Path) -> list[dict]:
    """Return list of fire records.

    Each record is a dict:
      {timestamp: datetime, kind: "hit"|"nohit",
       n_hits: int, n_unique: int, labels: list[str]}
    No-hit records have n_hits=0, n_unique=0, labels=[].
    """
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path) as f:
        for raw in f:
            line = raw.rstrip()
            if not line:
                continue
            m = _HIT_RE.match(line)
            if m:
                try:
                    ts = datetime.fromisoformat(m.group("ts"))
                except ValueError:
                    continue
                try:
                    labels = ast.literal_eval(m.group("labels"))
                except (ValueError, SyntaxError):
                    labels = []
                if not isinstance(labels, list):
                    labels = []
                out.append({
                    "timestamp": ts,
                    "kind": "hit",
                    "n_hits": int(m.group("n")),
                    "n_unique": int(m.group("u")),
                    "labels": [str(x) for x in labels],
                })
                continue
            m = _NOHIT_RE.match(line)
            if m:
                try:
                    ts = datetime.fromisoformat(m.group("ts"))
                except ValueError:
                    continue
                out.append({
                    "timestamp": ts, "kind": "nohit",
                    "n_hits": 0, "n_unique": 0, "labels": [],
                })
    return out


def _filter_since(entries: list[dict], since: date | None) -> list[dict]:
    if since is None:
        return entries
    return [e for e in entries if e["timestamp"].date() >= since]


def _categorize(label: str) -> str:
    if label in _PHRASE_LABELS:
        return "phrase"
    if label in _INTENT_LABELS:
        return "intent"
    return "unknown"


def overall(entries: list[dict]) -> None:
    total_fires = len(entries)
    hit_fires = sum(1 for e in entries if e["kind"] == "hit")
    nohit_fires = sum(1 for e in entries if e["kind"] == "nohit")
    total_hits = sum(e["n_hits"] for e in entries)
    total_unique = sum(e["n_unique"] for e in entries)
    print("=== Overall ===")
    print(f"  total Stop-hook fires:          {total_fires:>6,}")
    print(f"  hit fires:                      {hit_fires:>6,}")
    print(f"  no-hit fires (heartbeat):       {nohit_fires:>6,}")
    print(f"  total hits (sum of n_hits):     {total_hits:>6,}")
    if total_fires:
        # Per-fire rate is meaningful only when heartbeats are present.
        if nohit_fires > 0:
            rate = (hit_fires / total_fires) * 100
            print(f"  hit rate (hit / total):         {rate:>6.2f}%")
        else:
            print(f"  hit rate:                       (no heartbeat data; pre-2026-05-07 or hook misconfigured)")
    if hit_fires:
        print(f"  unique labels per hit (avg):    {(total_unique / hit_fires):>6.2f}")
    if entries:
        first = min(e["timestamp"] for e in entries)
        last = max(e["timestamp"] for e in entries)
        days = max(1, (last.date() - first.date()).days + 1)
        print(f"  first fire:                     {first.isoformat()}")
        print(f"  last fire:                      {last.isoformat()}")
        print(f"  span:                           {days} day(s); {total_fires / days:.2f} fires/day")


def by_category(entries: list[dict]) -> None:
    hit_entries = [e for e in entries if e["kind"] == "hit"]
    if not hit_entries:
        return
    cats: Counter[str] = Counter()
    for e in hit_entries:
        for label in e["labels"]:
            cats[_categorize(label)] += 1
    print("\n=== By category (hit fires only) ===")
    print(f"  {'category':<12} {'unique-label fires':>20}")
    for cat in ("phrase", "intent", "unknown"):
        print(f"  {cat:<12} {cats[cat]:>20,}")


def by_label(entries: list[dict]) -> None:
    hit_entries = [e for e in entries if e["kind"] == "hit"]
    if not hit_entries:
        return
    counts: Counter[str] = Counter()
    for e in hit_entries:
        for label in e["labels"]:
            counts[label] += 1
    print("\n=== By label (hit fires only) ===")
    print(f"  {'label':<22} {'category':<10} {'fires':>8}")
    for label, n in counts.most_common():
        print(f"  {label:<22} {_categorize(label):<10} {n:>8,}")


def by_day(entries: list[dict]) -> None:
    if not entries:
        return
    daily_fires: dict[date, int] = defaultdict(int)
    daily_hit_fires: dict[date, int] = defaultdict(int)
    daily_hits: dict[date, int] = defaultdict(int)
    daily_phrase: dict[date, int] = defaultdict(int)
    daily_intent: dict[date, int] = defaultdict(int)
    for e in entries:
        d = e["timestamp"].date()
        daily_fires[d] += 1
        if e["kind"] == "hit":
            daily_hit_fires[d] += 1
            daily_hits[d] += e["n_hits"]
            for label in e["labels"]:
                cat = _categorize(label)
                if cat == "phrase":
                    daily_phrase[d] += 1
                elif cat == "intent":
                    daily_intent[d] += 1
    print("\n=== By day ===")
    print(f"  {'date':<12} {'fires':>8} {'hit_fires':>10} {'rate%':>7} {'hits':>8} {'phrase':>8} {'intent':>8}")
    for d in sorted(daily_fires):
        rate_str = f"{(daily_hit_fires[d] / daily_fires[d] * 100):>6.2f}" if daily_fires[d] else "  -  "
        print(f"  {d.isoformat():<12} {daily_fires[d]:>8,} {daily_hit_fires[d]:>10,} {rate_str:>7} {daily_hits[d]:>8,} {daily_phrase[d]:>8,} {daily_intent[d]:>8,}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--since", type=str, default=None, help="Filter to entries on/after this ISO date.")
    p.add_argument("--by-day", action="store_true", help="Show only per-day breakdown.")
    p.add_argument("--by-label", action="store_true", help="Show only per-label breakdown.")
    p.add_argument("--by-category", action="store_true", help="Show only per-category breakdown.")
    args = p.parse_args()

    since = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since).date()
        except ValueError:
            print(f"error: --since must be ISO YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
            return 2

    entries = _filter_since(_parse_log(LOG_PATH), since)
    if not entries:
        print(f"(no data — log empty or filtered to nothing; path: {LOG_PATH})")
        return 0

    only_one = sum(int(b) for b in (args.by_day, args.by_label, args.by_category))
    if only_one == 1:
        if args.by_day:
            by_day(entries)
        elif args.by_label:
            by_label(entries)
        elif args.by_category:
            by_category(entries)
    else:
        overall(entries)
        by_category(entries)
        by_label(entries)
        by_day(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
