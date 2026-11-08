#!/usr/bin/env python3
"""Per-shard summary for the nightly suite (#4420). Stdlib only.

Reads the sampler log (``[shard k start]`` / ``[shard k end]`` markers and the
``[sampler]`` lines between them), each shard's pytest log and its RSS-delta report,
and writes one markdown row per shard to ``$GITHUB_STEP_SUMMARY`` (stdout when unset):
shard, files, passed, failed, errors, peak RSS, peak swap, status.

A peak that was not measured is printed ``unmeasured``, never ``0``. The script then
exits non-zero and emits an ``::error::`` annotation naming the shard, so a gap in
the measurement is loud. (The workflow step is ``continue-on-error``: measurement
never decides the night.)

Usage: nightly_shard_summary.py --n N --sampler-log FILE --dir DIR
  DIR holds nightly-shard-<k>.log and nightly-rss-shard-<k>.md
"""

import argparse
import os
import re
import sys

UNMEASURED = "unmeasured"
TIMED_OUT_RC = (124, 137)
CAVEAT = (
    "RSS deltas show where memory was first touched, not who holds it "
    "(the allocator rarely returns pages)."
)

_START = re.compile(r"^\[shard (\d+) start\]\s*(?P<rest>.*)$")
_END = re.compile(r"^\[shard (\d+) end\]\s*(?P<rest>.*)$")
_RSS = re.compile(r"\bpytest_rss=(\d+)M\b")
_SWAP = re.compile(r"\bswap_used=(\d+)M\b")
_PIDS = re.compile(r"\bpids=(\S*)")
_FILES = re.compile(r"\bfiles=(\d+)")
_RC = re.compile(r"\brc=(\d+)")
_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")
_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+?\.py)(?:::|\s|$)")


def parse_sampler(path, n):
    """Return {k: {"files", "rc", "rss": [..], "swap": [..]}} from the sampler log."""
    info = {k: {"files": None, "rc": None, "rss": [], "swap": []} for k in range(1, n + 1)}
    current = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return info
    for line in lines:
        m = _START.match(line)
        if m and int(m.group(1)) in info:
            current = int(m.group(1))
            f = _FILES.search(m.group("rest"))
            info[current]["files"] = int(f.group(1)) if f else None
            continue
        m = _END.match(line)
        if m and int(m.group(1)) in info:
            rc = _RC.search(m.group("rest"))
            info[int(m.group(1))]["rc"] = int(rc.group(1)) if rc else None
            current = None
            continue
        if current is not None and line.startswith("[sampler]"):
            pids = _PIDS.search(line)
            rss = _RSS.search(line)
            if rss and pids and pids.group(1) not in ("", "none"):
                info[current]["rss"].append(int(rss.group(1)))
                swap = _SWAP.search(line)
                if swap:
                    info[current]["swap"].append(int(swap.group(1)))
    return info


def parse_shard_log(path):
    """Return (passed, failed, errors, failing_files) or Nones when the log has no summary line."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None, None, None, []
    failing = []
    for line in lines:
        m = _FAILED.match(line)
        if m and m.group(1) not in failing:
            failing.append(m.group(1))
    counts = None
    for line in reversed(lines):
        if re.search(r"\bin \d+(\.\d+)?s\b", line) and _COUNT.search(line):
            counts = {"passed": 0, "failed": 0, "errors": 0}
            for num, word in _COUNT.findall(line):
                key = "errors" if word.startswith("error") else word
                counts[key] = int(num)
            break
    if counts is None:
        return None, None, None, failing
    return counts["passed"], counts["failed"], counts["errors"], failing


def _mb(values):
    return "%d MB" % max(values) if values else UNMEASURED


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--sampler-log", required=True)
    ap.add_argument("--dir", required=True)
    args = ap.parse_args(argv)

    info = parse_sampler(args.sampler_log, args.n)
    out = ["### Nightly shards\n",
           "| shard | files | passed | failed | errors | peak RSS | peak swap | status |",
           "|---|---|---|---|---|---|---|---|"]
    failing_by_shard = {}
    missing = []
    for k in range(1, args.n + 1):
        d = info[k]
        passed, failed, errors, failing = parse_shard_log(os.path.join(args.dir, "nightly-shard-%d.log" % k))
        failing_by_shard[k] = failing
        if d["rc"] in TIMED_OUT_RC:
            status = "TIMED OUT"
        elif d["rc"] is None:
            status = "no end marker"
        elif d["rc"] == 0:
            status = "ok"
        else:
            status = "rc=%d" % d["rc"]
        rss, swap = _mb(d["rss"]), _mb(d["swap"])
        if rss == UNMEASURED or swap == UNMEASURED:
            what = [w for w, v in (("RSS", rss), ("swap", swap)) if v == UNMEASURED]
            missing.append((k, what))
        fmt = lambda v: UNMEASURED if v is None else str(v)  # noqa: E731
        out.append("| %d | %s | %s | %s | %s | %s | %s | %s |" % (
            k, fmt(d["files"]), fmt(passed), fmt(failed), fmt(errors), rss, swap, status))
    out.append("")
    any_failing = {k: v for k, v in failing_by_shard.items() if v}
    if any_failing:
        out.append("#### Failing files\n")
        for k, files in any_failing.items():
            out.append("- shard %d: %s" % (k, ", ".join("`%s`" % f for f in files)))
        out.append("")
    for k, d in info.items():
        if d["rc"] in TIMED_OUT_RC:
            out.append("- shard %d TIMED OUT (rc=%d)" % (k, d["rc"]))
    out.append("")
    reports = []
    for k in range(1, args.n + 1):
        try:
            with open(os.path.join(args.dir, "nightly-rss-shard-%d.md" % k), encoding="utf-8") as fh:
                reports.append("#### Shard %d: RSS deltas\n\n%s" % (k, fh.read()))
        except OSError:
            pass
    if reports:
        out.append("### Per-test memory (RSS deltas)\n")
        out.append(CAVEAT + "\n")
        out.extend(reports)
    text = "\n".join(out) + "\n"

    dest = os.environ.get("GITHUB_STEP_SUMMARY")
    if dest:
        try:
            with open(dest, "a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as e:
            sys.stdout.write(text)
            sys.stderr.write("could not write the step summary: %s\n" % e)
    else:
        sys.stdout.write(text)

    for k, what in missing:
        print("::error::shard %d has no sampler peak line (%s unmeasured)" % (k, ", ".join(what)))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
