#!/usr/bin/env python3
"""Fork-1 (#1690 P2) flip-or-hold benchmark: added latency of
check_in_turn_recall() itself, isolated from the PreToolUse hook's existing
~300ms subprocess-spawn cost (which is paid regardless of this feature and
is not what's being decided here).

Not a replay_bench.py extension: that harness replays MCP-server tool calls
against a sandboxed graph copy and has no path to invoke a PreToolUse hook's
Python function directly (confirmed by reading its source, 2026-07-08 —
no `in_turn_recall` / `PreToolUse` reference anywhere in it). in_turn_recall
lives entirely inside one hook-script function call, so a direct in-process
timing harness is the right-sized tool -- no sandboxed-server machinery
needed, since the function only opens a read-only sqlite connection + a
unix-socket query to the (already-running) recall daemon.

Uses the REAL ENGRAM_HOME (real knowledge.db via mode=ro connection -- no
write path exists through that connection; real live recall-daemon.sock) so
numbers reflect actual production cost. The one production side effect the
function has (writing in-turn-recall-state.json) is redirected to a scratch
path post-import so this run never perturbs live cooldown/dedup state.

Three scenarios:
  A. baseline (enabled=False, today's default) -- expected near-zero.
  B. worst-case (enabled=True, cooldown=0, forced-novel terms every call) --
     upper bound: cost when the gate passes and the daemon is queried
     every single time.
  C. realistic-mix (enabled=True, default cooldown=60s, terms drawn from a
     small repeating vocabulary so novelty decays like a real session) --
     the actual expected added cost under normal use.

Each scenario runs sequentially AND under a ThreadPoolExecutor at a few
worker counts (mirrors replay_bench.py's --concurrency levels convention),
since PreToolUse hooks CAN fire concurrently for parallel tool calls in one
turn.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"

DEFAULT_ENGRAM_HOME = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")

# Realistic match_target strings -- rich prose shaped like real MCP tool_input
# payloads (claim/interpretation text), NOT short generic CLI commands.
# CALIBRATION NOTE (found empirically 2026-07-08, see report): short strings
# like "baton status" or "ia read ariadne" never clear the real default
# min_idf=6.0 bar in this graph -- extract_keywords returns terms=[] and gate 3
# short-circuits before ever reaching the daemon, which silently collapses the
# "worst case" and "realistic mix" scenarios into the SAME cheap no-op path.
# These targets deliberately mirror real engram_add_observation/add_edge
# tool_input shapes (this session's own recent calls) so the IDF gate behaves
# the way it does in production.
#
# INSTRUMENT-HONESTY NOTE (Kepler's colleague review, #1734): the IDF ceiling
# is ln(N) for N current nodes, so min_idf=6.0 needs a graph of ~400+ current
# nodes before it can EVER pass -- on smaller graphs gate 3 returns terms=[]
# for every target, rich prose included, and scenarios B/C silently collapse
# into the same no-daemon path as A while still rendering plausible-looking
# latency numbers. Two mitigations below: --min-idf is now a CLI flag (not
# hardcoded), and every scenario/level reports daemon_queries so a silent
# collapse is visible in the report rather than misread as a real measurement.
_SAMPLE_TARGETS = [
    'mcp__plugin_engram_engram__engram_add_observation quoted_text hand-typed ScheduleWakeup prompt produced HTML-escaped loop-wake marker interpretation situation_pattern PreToolUse tripwire recurrence claim',
    'mcp__plugin_engram_engram__engram_add_edge source_id trigger target_id principle relation exemplifies instantiates cornerstone lesson axiom goal',
    'grep check_incident_tripwire check_cornerstone_anchor engram_core rebuild principle triggers is_current bidirectional tensions registry',
    'baton init pool-recall-continuity participants ariadne sol turn sol status in-progress continuity channel recall precision',
    'gh issue view 1698 unified principle-triggers registry migration shim rebuild cache habituation decay enactment telemetry',
    'forum reply team distribution axis calibration recall precision mind-correctness invariant contradiction machinery focus render',
    'python3 tools perf replay_bench concurrency scenario worker pool sandboxed graph copy semantic equivalence oracle latency percentile',
    'mcp__plugin_engram_engram__engram_inspect node_id task claim replay bench in_turn_recall goal trigger threshold pending queue',
]


class _DaemonQueryCounter:
    """Wraps _query_recall_daemon to count real daemon hits, thread-safely.

    check_in_turn_recall() calls `_query_recall_daemon(...)` as a bare
    module-level name, so monkeypatching `mod._query_recall_daemon` to this
    wrapper is sufficient -- the lookup happens at call time in the module's
    own globals. Used to make silent gate-3 collapse (see the
    INSTRUMENT-HONESTY note above) visible in the report instead of invisible.
    """

    def __init__(self, real_fn):
        self._real_fn = real_fn
        self._lock = threading.Lock()
        self.count = 0

    def __call__(self, *args, **kwargs):
        with self._lock:
            self.count += 1
        return self._real_fn(*args, **kwargs)

    def reset(self) -> None:
        with self._lock:
            self.count = 0


def _load_hook(engram_home: str):
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = engram_home
    try:
        spec = importlib.util.spec_from_file_location("bench_tripwire_hook", HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old


def _percentile(vals: list[float], pct: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * (pct / 100.0)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _time_calls(fn, targets: list[str], n_workers: int) -> list[float]:
    """Run fn(target) for each target, n_workers concurrently; return per-call ms."""
    durations = []

    def _one(t):
        start = time.perf_counter()
        fn(t)
        return (time.perf_counter() - start) * 1000.0

    if n_workers <= 1:
        for t in targets:
            durations.append(_one(t))
    else:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            durations = list(ex.map(_one, targets))
    return durations


def run(engram_home: str, n_calls: int, concurrency_levels: list[int], min_idf: float = 6.0) -> dict:
    mod = _load_hook(engram_home)

    # Redirect the one write side-effect to a PER-RUN unique scratch dir --
    # never touch the real production state file, and never reuse a constant
    # /tmp path either (Kepler's colleague review, #1734 round 2: a constant
    # path collides across agents on a shared /tmp -- the hook derives its
    # lockfile as "<state>.lock", creates it mode 0600, and never deletes it.
    # A second agent's run hits EACCES opening the first agent's lockfile;
    # the hook's deliberate non-POSIX/permissions fallback then silently
    # degrades to the UNLOCKED path ("a perf issue, not a correctness one"),
    # so the second run measures a genuine stampede with locking effectively
    # disabled -- an artifact of shared /tmp, not the hook or the #1714 fix).
    # mkdtemp gives each run its own directory; rmtree in finally cleans up the
    # state file AND its derived lockfile together, so nothing outlives this run.
    scratch_dir = Path(tempfile.mkdtemp(prefix="bench-in-turn-recall-"))
    scratch_state = scratch_dir / "state.json"
    mod.STATE_PATH = scratch_state

    try:
        # Wrap _query_recall_daemon to count real daemon hits per level, so a
        # silent gate-3 collapse (see INSTRUMENT-HONESTY note above) is visible
        # in the report instead of rendering plausible-but-fake latency numbers.
        daemon_counter = _DaemonQueryCounter(mod._query_recall_daemon)
        mod._query_recall_daemon = daemon_counter

        report = {"engram_home": engram_home, "n_calls": n_calls, "min_idf": min_idf, "scenarios": {}}

        # --- Scenario A: baseline (disabled) ---
        mod._in_turn_config = lambda: {
            "enabled": False, "cooldown_seconds": 60, "max_lines": 3, "min_idf": min_idf,
        }
        scratch_state.unlink(missing_ok=True)
        targets = [_SAMPLE_TARGETS[i % len(_SAMPLE_TARGETS)] for i in range(n_calls)]
        a_results = {}
        for nw in concurrency_levels:
            daemon_counter.reset()
            durations = _time_calls(mod.check_in_turn_recall, targets, nw)
            a_results[nw] = {
                "p50_ms": round(_percentile(durations, 50), 4),
                "p95_ms": round(_percentile(durations, 95), 4),
                "max_ms": round(max(durations), 4),
                "daemon_queries": daemon_counter.count,
            }
        report["scenarios"]["A_baseline_disabled"] = a_results

        # --- Scenario B: worst-case (enabled, cooldown=0, forced-novel every call) ---
        mod._in_turn_config = lambda: {
            "enabled": True, "cooldown_seconds": 0, "max_lines": 3, "min_idf": min_idf,
        }
        b_results = {}
        for nw in concurrency_levels:
            scratch_state.unlink(missing_ok=True)  # reset cooldown/seen-terms each level
            daemon_counter.reset()
            # Force novelty every call: unique numeric token appended so the
            # seen-terms set from a prior call in this batch never suppresses it.
            unique_targets = [f"{_SAMPLE_TARGETS[i % len(_SAMPLE_TARGETS)]} uniquetoken{i}" for i in range(n_calls)]
            durations = _time_calls(mod.check_in_turn_recall, unique_targets, nw)
            b_results[nw] = {
                "p50_ms": round(_percentile(durations, 50), 4),
                "p95_ms": round(_percentile(durations, 95), 4),
                "max_ms": round(max(durations), 4),
                "daemon_queries": daemon_counter.count,
            }
        report["scenarios"]["B_worst_case_enabled"] = b_results

        # --- Scenario C: realistic mix (enabled, default cooldown, repeating vocab) ---
        mod._in_turn_config = lambda: {
            "enabled": True, "cooldown_seconds": 60, "max_lines": 3, "min_idf": min_idf,
        }
        c_results = {}
        for nw in concurrency_levels:
            scratch_state.unlink(missing_ok=True)
            daemon_counter.reset()
            durations = _time_calls(mod.check_in_turn_recall, targets, nw)
            c_results[nw] = {
                "p50_ms": round(_percentile(durations, 50), 4),
                "p95_ms": round(_percentile(durations, 95), 4),
                "max_ms": round(max(durations), 4),
                "daemon_queries": daemon_counter.count,
            }
        report["scenarios"]["C_realistic_mix"] = c_results

        return report
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--engram-home", default=DEFAULT_ENGRAM_HOME)
    p.add_argument("--n-calls", type=int, default=40)
    p.add_argument("--concurrency", default="1,4,8")
    p.add_argument(
        "--min-idf", type=float, default=6.0,
        help="IDF threshold for gate 3 (default 6.0, today's production default). "
             "The IDF ceiling is ln(N) for N current nodes -- on graphs under "
             "~400 nodes, 6.0 never clears and every call short-circuits before "
             "the daemon. Lower this to calibrate against a smaller graph "
             "(Kepler used 4.0 live per #1734).",
    )
    p.add_argument("--json-out")
    args = p.parse_args(argv)

    levels = [int(x) for x in args.concurrency.split(",")]
    report = run(args.engram_home, args.n_calls, levels, min_idf=args.min_idf)
    print(json.dumps(report, indent=2))
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(report, f, indent=2)


if __name__ == "__main__":
    main()
