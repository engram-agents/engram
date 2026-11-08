#!/usr/bin/env python3
"""Split the nightly suite's test FILES into N contiguous shards (#4420).

The nightly suite runs as N sequential pytest processes. This tool decides which
files go to which process, from pytest's own collection, so a test file added
under ``tests/`` or ``src/forum/tests/`` is picked up with no workflow edit.

* Files are ordered by path with ``tests/`` before ``src/forum/tests/``, whatever
  order pytest collected in (that order differs between pytest versions).
* Each shard is a contiguous run of that order, so a file is never split: cells
  that rely on their predecessors in the same file stay in one process.
* Shards are balanced by collected test count: a file goes to the shard its
  midpoint (in cumulative test count) falls in.
* The output is a pure function of the input counts.

Usage:
    nightly_shards.py plan --n N --out PLAN.json [--collect-file F] [roots...]
    nightly_shards.py files --plan PLAN.json --shard K
    nightly_shards.py table --plan PLAN.json
"""

import argparse
import json
import re
import subprocess
import sys

ROOTS = ("tests", "src/forum/tests")
ROOT_ORDER = ("tests/", "src/forum/tests/")
COLLECT_ARGS = ["--collect-only", "-qq", "-m", "not timing", "-p", "no:cacheprovider"]

_QQ = re.compile(r"^(?P<file>\S+?\.py): (?P<n>\d+)\s*$")
_NODEID = re.compile(r"^(?P<file>\S+?\.py)::")


def parse_collect(text):
    """Return [(file, test_count)] in first-seen order from ``--collect-only`` output.

    Reads both ``-qq`` (``path: N``) and ``-q`` (``path::name``) shapes; anything
    else (blank lines, the trailing summary) is ignored.
    """
    counts = {}
    for line in text.splitlines():
        m = _QQ.match(line)
        if m:
            counts[m.group("file")] = counts.get(m.group("file"), 0) + int(m.group("n"))
            continue
        m = _NODEID.match(line)
        if m:
            counts[m.group("file")] = counts.get(m.group("file"), 0) + 1
    return list(counts.items())


def _rank(path):
    for i, root in enumerate(ROOT_ORDER):
        if path.startswith(root):
            return i
    return len(ROOT_ORDER)


def order_files(files):
    return sorted(files, key=lambda f: (_rank(f), f))


def split_shards(counts, n):
    """Split ``[(file, count)]`` into exactly ``n`` contiguous groups (some may be empty)."""
    if n < 1:
        raise ValueError(f"shard count must be >= 1, got {n}")
    merged = {}
    for f, c in counts:
        merged[f] = merged.get(f, 0) + c
    ordered = [(f, merged[f]) for f in order_files(merged)]
    total = sum(c for _, c in ordered)
    shards = [[] for _ in range(n)]
    before = 0
    for i, (f, c) in enumerate(ordered):
        if total > 0:
            idx = int(((before + c / 2.0) * n) // total)
        else:
            idx = (i * n) // max(len(ordered), 1)
        shards[min(idx, n - 1)].append((f, c))
        before += c
    return shards


def collect(roots):
    cmd = [sys.executable, "-m", "pytest", *COLLECT_ARGS, *roots]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout[-4000:] + r.stderr[-4000:])
        sys.stderr.write(f"nightly_shards: collection failed (pytest exit {r.returncode}); "
                         "refusing to plan a partial suite\n")
        raise SystemExit(2)
    return r.stdout


def make_plan(counts, n):
    shards = split_shards(counts, n)
    return {
        "n": n,
        "files": sum(len(s) for s in shards),
        "tests": sum(c for s in shards for _, c in s),
        "shards": [
            {"index": i, "files": [f for f, _ in s], "tests": sum(c for _, c in s)}
            for i, s in enumerate(shards, start=1)
        ],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--collect-file", help="read a saved --collect-only output instead of running pytest")
    p.add_argument("roots", nargs="*", default=list(ROOTS))
    f = sub.add_parser("files")
    f.add_argument("--plan", required=True)
    f.add_argument("--shard", type=int, required=True)
    t = sub.add_parser("table")
    t.add_argument("--plan", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "plan":
        if args.n < 1:
            sys.stderr.write("nightly_shards: --n must be >= 1\n")
            return 2
        if args.collect_file:
            with open(args.collect_file, encoding="utf-8") as fh:
                text = fh.read()
        else:
            text = collect(args.roots)
        counts = parse_collect(text)
        if not counts:
            sys.stderr.write("nightly_shards: collection produced no test files; refusing to plan\n")
            return 2
        plan = make_plan(counts, args.n)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=1, sort_keys=True)
            fh.write("\n")
        return 0

    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    if args.cmd == "files":
        if not 1 <= args.shard <= plan["n"]:
            sys.stderr.write(f"nightly_shards: shard {args.shard} out of range 1..{plan['n']}\n")
            return 2
        for path in plan["shards"][args.shard - 1]["files"]:
            print(path)
        return 0
    print("shard\tfiles\ttests")
    for s in plan["shards"]:
        print(f"{s['index']}\t{len(s['files'])}\t{s['tests']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
