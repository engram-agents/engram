#!/usr/bin/env python3
"""Check that the shard plan covers a reference test-id set exactly once (tests.yml).

tests.yml runs the full suite as N sequential pytest processes (the shard runner
nightly_run_shards.sh). The old shape was ONE process over ``tests/ src/forum/tests/``.
This check proves the split lost nothing and doubled nothing, BEFORE any shard runs:

* the reference is a saved ``pytest --collect-only -q`` output of the old step's
  roots (no ``-m`` filter);
* for each shard with files, collect again with exactly that shard's files (and the
  marker expression, if any) and parse the node ids;
* assert the shard sets are pairwise disjoint and their union equals the reference.

A shard collection that exits non-zero fails the check (the shard is named): a
partial collection must never read as a clean split.

Stdlib only. The python that collects is injectable (``--python`` or the
``SHARD_IDSET_PYTHON`` environment variable) so tests need no real suite.

Usage:
    shard_idset_check.py --plan PLAN.json --reference REF.txt [--marker EXPR]

Exit 0 on PASS (one summary line on stdout), 1 on any failure (``::error::`` lines
on stdout), 2 on unusable input.

A reference that is PARTIAL is unusable input, even when it still parses to a
non-empty id set. A test file that fails to collect yields no ids, so the planner never
plans it and the shard collections never see it: it vanishes from BOTH sides and the
comparison would still read "union == reference". So the reference is refused (exit 2,
named ``::error::`` line) when its own pytest output reports collection errors
(``N errors`` in the summary line, the ``Interrupted: N errors during collection``
banner, ``ERROR collecting`` lines), has no pytest summary line (a truncated file), or
when the summary's collected count differs from the number of ids parsed.
"""

import argparse
import itertools
import json
import os
import re
import subprocess
import sys

# A node id line: ``<path>.py::<rest>``. The whole line (trailing space stripped) is
# the id, since parametrize ids may hold spaces. Blank lines, the summary line and
# warnings never match.
_NODEID = re.compile(r"^\S+?\.py::")
LIST_CAP = 20
# Names of the settings nightly_run_shards.sh and this check read for themselves.
RUNNER_ENV_PREFIXES = ("NIGHTLY_", "SHARD_IDSET_")

# The pytest summary line of ``--collect-only -q`` (pytest 9), e.g.
#   ``33375 tests collected in 16.12s``      ``1 test collected in 0.00s``
#   ``33375 tests collected, 72 errors in 16.12s``   ``no tests collected, 1 error in 0.04s``
#   ``no tests collected in 0.00s``          ``no tests ran in 0.01s``
#   ``1/2 tests collected (1 deselected) in 0.00s``  (a ``-m``/``-k`` selection)
# Group 1 = the selected count (``K`` of ``K/M``, or ``N``); ``no`` means 0.
_SUMMARY = re.compile(r"^(?:(\d+)(?:/\d+)?|no) tests? (?:collected|ran)\b(.*)$")
_SUMMARY_ERRORS = re.compile(r"\b(\d+) errors?\b")
_BANNER_ERRORS = re.compile(r"Interrupted: (\d+) errors? during collection")
_ERROR_COLLECTING = re.compile(r"^_* ?ERROR collecting\b")


def parse_ids(text):
    """Return the set of node ids in ``--collect-only -q`` output."""
    return {line.rstrip() for line in text.splitlines() if _NODEID.match(line)}


def parse_summary(text):
    """Return (selected_count, error_count, line) of the LAST pytest summary line, or None."""
    for line in reversed(text.splitlines()):
        m = _SUMMARY.match(line.strip())
        if m:
            n = int(m.group(1)) if m.group(1) is not None else 0
            e = _SUMMARY_ERRORS.search(m.group(2))
            return n, int(e.group(1)) if e else 0, line.strip()
    return None


def reference_problems(text, ids):
    """Return ``::error::`` lines for every way the reference is PARTIAL (empty list: usable).

    The empty-id-set refusal is separate (main) and runs first."""
    problems = []
    errs = [int(m.group(1)) for m in map(_BANNER_ERRORS.search, text.splitlines()) if m]
    n_collecting = sum(1 for ln in text.splitlines() if _ERROR_COLLECTING.match(ln.strip()))
    summary = parse_summary(text)
    n_errors = max(errs + [summary[1] if summary else 0])
    if n_errors or n_collecting:
        problems.append(f"::error::idset: the reference reports collection errors "
                        f"({n_errors or n_collecting} error(s), {n_collecting} 'ERROR collecting' line(s)): "
                        "a file that fails to collect yields no ids, so it would vanish from both the plan "
                        "and the shards; the reference is partial")
    if summary is None:
        problems.append("::error::idset: the reference has no pytest summary line "
                        "(e.g. '33375 tests collected in 16.12s'): the file looks truncated, so it is partial")
    elif summary[0] != len(ids):
        problems.append(f"::error::idset: the reference summary says {summary[0]} test(s) collected "
                        f"({summary[2]!r}) but {len(ids)} id(s) were parsed from it: the reference is partial "
                        "or corrupt")
    return problems


def collect_shard(python, files, marker):
    """Return (returncode, ids, tail_of_output) for one shard's collection."""
    cmd = [python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    if marker:
        cmd += ["-m", marker]
    cmd += list(files)
    # The runner's own settings (the reference file, the marker, ...) are for the runner and
    # this check; the collection must not see them.
    env = {k: v for k, v in os.environ.items() if not k.startswith(RUNNER_ENV_PREFIXES)}
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return r.returncode, parse_ids(r.stdout), (r.stdout[-2000:] + r.stderr[-2000:])


def _lines(kind, ids, where=""):
    ids = sorted(ids)
    shown = ids[:LIST_CAP]
    more = len(ids) - len(shown)
    out = [f"::error::{kind}: {len(ids)} id(s){where}"]
    out += [f"::error::  {i}" for i in shown]
    if more > 0:
        out.append(f"::error::  ... and {more} more")
    return out


def check(plan, reference_ids, marker, python):
    """Return (ok, lines). ``lines`` is the PASS line or the ``::error::`` lines."""
    errors = []
    per_shard = {}
    for shard in plan["shards"]:
        files = shard["files"]
        if not files:
            continue
        k = shard["index"]
        rc, ids, tail = collect_shard(python, files, marker)
        if rc != 0:
            errors.append(f"::error::shard {k} collection failed (pytest exit {rc}); "
                          "the id-set cannot be trusted")
            errors += [f"::error::  {ln}" for ln in tail.splitlines()[-10:] if ln.strip()]
            continue
        per_shard[k] = ids
    if errors:
        return False, errors

    for (ka, a), (kb, b) in itertools.combinations(sorted(per_shard.items()), 2):
        both = a & b
        if both:
            errors += _lines("id collected by two shards", both, f" (shard {ka} and shard {kb})")
    union = set().union(*per_shard.values()) if per_shard else set()
    missing = reference_ids - union
    extra = union - reference_ids
    if missing:
        errors += _lines("id in the reference but in NO shard", missing)
    if extra:
        errors += _lines("id in a shard but NOT in the reference", extra)
    if errors:
        errors.append(f"::error::idset: {len(per_shard)} shards, reference {len(reference_ids)} ids, "
                      f"union {len(union)} ids: split does not match the reference")
        return False, errors
    return True, [f"idset: {len(per_shard)} shards, {len(reference_ids)} ids, union == reference, disjoint"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--plan", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--marker", default="", help="-m expression used for the shard collections (default: none)")
    ap.add_argument("--python", default=os.environ.get("SHARD_IDSET_PYTHON") or sys.executable)
    args = ap.parse_args(argv)

    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    with open(args.reference, encoding="utf-8") as fh:
        reference_text = fh.read()
    reference = parse_ids(reference_text)
    if not reference:
        print("::error::idset: the reference holds no test ids; refusing to pass an empty comparison")
        return 2
    problems = reference_problems(reference_text, reference)
    if problems:
        print("\n".join(problems))
        return 2
    ok, lines = check(plan, reference, args.marker, args.python)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
