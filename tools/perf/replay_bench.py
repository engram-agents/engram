#!/usr/bin/env python3
"""Replay-bench runner + reporter for the ENGRAM MCP tool surface.

Part of the replay-bench harness (blueprint §6, docs/perf/optimization-blueprint.md,
umbrella #1668). Reads a trace JSONL (from trace_from_telemetry.py, or hand-authored),
executes each `{tool_name, params}` call against `server.engram_<tool_name>` inside
an isolated sandbox copy of a target graph, and reports p50/p90/p99 + total + call
count per tool — plus an optional "faster, never looser" semantic-equivalence oracle
(--record / --compare) that future optimization PRs use to prove they didn't change
any tool's observable content, only its speed.

Data-dir isolation: this module copies knowledge.db (+ WAL/SHM sidecars + config.json,
if present) from a REAL data dir into a fresh scratch directory, then runs the trace
against that scratch copy via `server.engram_sandbox(data_dir=<scratch>)`
(src/engram/engram_core.py ~line 284). `_ensure_data_dir()` only creates config/dirs
when missing — it never overwrites an existing knowledge.db — so a scratch dir
pre-populated with a real DB copy is safe. The real graph is never touched.

v1.1 (--concurrency) adds a CONCURRENT replay scenario alongside v1's
sequential run: the same trace is submitted through a
concurrent.futures.ThreadPoolExecutor at one or more worker-count levels
against ONE shared sandboxed server, reproducing FastMCP's real dispatch model
(sync MCP tool calls run in a thread pool) so latency-under-contention can
actually be observed. See run_concurrent_replay()'s docstring + the
module-level comment above it for the full mechanism writeup.
"""

import argparse
import json
import math
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# This file lives in the source-repo's tools/ tree (a plain dev script, not a
# packaged plugin artifact) — a fixed parents[N] is fine here, unlike the
# plugin-hook path-resolution caveat in CLAUDE.md (which is about the BUILD
# output's flattened hooks/skills layout, a different code path entirely).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ENGRAM = _REPO_ROOT / "src" / "engram"
if str(_SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(_SRC_ENGRAM))


# ---------------------------------------------------------------------------
# Semantic-equivalence oracle: volatile-field masking
# ---------------------------------------------------------------------------
#
# These field names are masked (value replaced, key kept) wherever they occur
# in a response, regardless of nesting path, before storing/comparing — so the
# oracle diffs *epistemic content* (claims, structure, topology), not
# incidental housekeeping counters that legitimately change between runs or
# across future optimizations that are explicitly anticipated by the
# blueprint (e.g. QW3's batched importance-refresh will change recall_count /
# importance_score update cadence without changing any claim's content — that
# is a legitimate "faster" change, not a "looser" one).
#
# Categories:
#   - Recall/importance/utility counters that increment or decay on every read
#     access (engram_query._refresh_recall, engram_core._compute_importance):
#     recall_count, recall_turn, recall_turn_range, importance_score,
#     importance_base, utility_score, surprise_score, util_amp, imp_amp,
#     imp_norm_factor, current_turn, not_recalled_recently, confidence_history.
#   - Wall-clock-relative humanized-time strings (core._humanized_ago), which
#     drift purely with when the call executed, never with content:
#     created_ago, neighbor_created_ago, newest_ago, oldest_ago,
#     wall_clock_range.
#   - Live wall-clock latency diagnostics embedded directly in engram_surface
#     responses (engram_query.py ~1716, ~1966): _surface_latency_ms is a raw
#     per-call millisecond reading, and _surface_warning interpolates that same
#     reading into a formatted advisory string — masked wholesale (the whole
#     string, not just the number) since the number isn't cleanly separable
#     from the surrounding text. Caught empirically: an embeddings-enabled
#     self-compare run on a real graph copy flagged exactly these two fields
#     as the only mismatch before this entry was added — see PR body.
#   - `focused_ago` / `last_assessed_ago` (engram_query.py ~2055-2057): siblings
#     of `created_ago` — the identical `core._humanized_ago(...)` call, just on
#     `focused_at` / `last_assessed_at` instead of `created_at`, in engram_inspect's
#     recall/deep view. Caught in colleague review (reviewer-fairy on this PR):
#     focusing a node, calling engram_inspect, masking, waiting ~1.2s, and
#     calling again reproduced a spurious `focused_ago` mismatch ('0s ago' →
#     '1s ago') with zero real change — the same co-location trap as
#     `_surface_latency_ms` above, just in a different function.
#
# When in doubt, mask conservatively (over-masking a volatile-looking field is
# safer than a false-positive on optimization-irrelevant noise) — see the PR
# body / spec for the rationale restated in full.
MASKED_FIELD_NAMES = frozenset({
    "recall_count",
    "recall_turn",
    "recall_turn_range",
    "importance_score",
    "importance_base",
    "utility_score",
    "surprise_score",
    "util_amp",
    "imp_amp",
    "imp_norm_factor",
    "current_turn",
    "not_recalled_recently",
    "confidence_history",
    "created_ago",
    "neighbor_created_ago",
    "newest_ago",
    "oldest_ago",
    "focused_ago",
    "last_assessed_ago",
    "wall_clock_range",
    "_surface_latency_ms",
    "_surface_warning",
})

_MASK_SENTINEL = "<masked>"


def _mask(obj):
    """Recursively replace any dict value keyed by a MASKED_FIELD_NAMES entry
    with a sentinel, wherever it occurs in the structure. Keys/structure are
    preserved so a diff still catches a field appearing/disappearing."""
    if isinstance(obj, dict):
        return {
            k: (_MASK_SENTINEL if k in MASKED_FIELD_NAMES else _mask(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask(x) for x in obj]
    return obj


def _diff(before, after, path=""):
    """Recursively diff two masked structures. Returns a list of
    (path, before_value, after_value) mismatch tuples."""
    mismatches = []
    if isinstance(before, dict) and isinstance(after, dict):
        keys = sorted(set(before.keys()) | set(after.keys()))
        for k in keys:
            p = f"{path}.{k}" if path else k
            if k not in before:
                mismatches.append((p, "<absent>", after[k]))
            elif k not in after:
                mismatches.append((p, before[k], "<absent>"))
            else:
                mismatches.extend(_diff(before[k], after[k], p))
    elif isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            mismatches.append((path, f"len={len(before)}", f"len={len(after)}"))
        else:
            for i, (b, a) in enumerate(zip(before, after)):
                mismatches.extend(_diff(b, a, f"{path}[{i}]"))
    else:
        if before != after:
            mismatches.append((path, before, after))
    return mismatches


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default 'linear' method).
    sorted_vals must already be sorted ascending and non-empty."""
    if not sorted_vals:
        raise ValueError("_percentile requires a non-empty list")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[int(f)] * (c - k) + sorted_vals[int(c)] * (k - f)


# ---------------------------------------------------------------------------
# Data-dir isolation
# ---------------------------------------------------------------------------

def _copy_data_dir(src_dir, dst_dir) -> None:
    """Copy knowledge.db (+ WAL/SHM sidecars, if present) + config.json (if
    present) from src_dir into dst_dir. Never touches src_dir. Raises
    FileNotFoundError if src_dir has no knowledge.db at all.

    Caveat (reviewer-fairy note): this is a plain shutil.copy2, not a
    transactionally-consistent SQLite backup (no sqlite3 .backup() API, no
    checkpoint-first). Fine against a static/stopped-server snapshot, which is
    the intended --data-dir input; not guaranteed consistent if --data-dir
    points at a live directory being actively written by a running server.
    """
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    db = src_dir / "knowledge.db"
    if not db.exists():
        raise FileNotFoundError(f"No knowledge.db found in {src_dir}")
    shutil.copy2(db, dst_dir / "knowledge.db")

    for sidecar in ("knowledge.db-wal", "knowledge.db-shm"):
        p = src_dir / sidecar
        if p.exists():
            shutil.copy2(p, dst_dir / sidecar)

    cfg = src_dir / "config.json"
    if cfg.exists():
        shutil.copy2(cfg, dst_dir / "config.json")


def make_scratch_graph(dest_dir, n_nodes: int = 3):
    """Create a tiny synthetic ENGRAM graph at dest_dir — a test-fixture helper.

    NOT part of the CLI surface; exists so tests/test_replay_bench.py can seed
    a throwaway graph without itself importing `server`. Per
    tests/IMPORT_RUBRIC.md R1/R2, a non-`test_*_payload.py` test file that
    imports `server` directly needs a server_import_allowlist entry in
    src/engram/packaging/test-map.json — outside this PR's stated file scope
    (tools/perf/ + tests/). Keeping the one `import server` call inside this
    tool module (not the test file) avoids that entirely.

    Content here is throwaway synthetic fixture text (not sourced from any
    real graph) — the content-anonymization requirement governs
    trace_from_telemetry.py's sampling of REAL graphs, not this test fixture.
    """
    import server  # local import: only needed by this helper + run_replay

    dest_dir = Path(dest_dir)
    with server.engram_sandbox(data_dir=dest_dir):
        for i in range(n_nodes):
            server.engram_add_observation(payload_json=json.dumps({
                "url": f"https://example.com/scratch-{i}",
                "title": f"Scratch source {i}",
                "claim": f"Scratch test claim number {i}.",
                "quoted_text": f"Scratch test claim number {i}.",
                "quote_type": "hard_data",
                "interpretation": "replay-bench smoke-test fixture",
            }))
    return dest_dir


# ---------------------------------------------------------------------------
# Trace loading
# ---------------------------------------------------------------------------

def _load_trace(path) -> list[dict]:
    calls = []
    with Path(path).open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            calls.append(json.loads(line))
    return calls


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_replay(trace_path, data_dir, mode: str | None = None, baseline_path=None):
    """Replay a trace against a sandboxed copy of data_dir.

    Args:
        trace_path: JSONL trace (see trace_from_telemetry.py).
        data_dir: real ENGRAM data dir to copy from (never modified).
        mode: None (plain timing run), "record" (also capture a baseline at
            baseline_path), or "compare" (also diff against baseline_path).
        baseline_path: required when mode is "record" or "compare".

    Returns:
        (report_dict, per_call_results_list)

    Note: each trace line's "ts" field (see trace_from_telemetry.py) is not
    read here — v1 replays sequentially in file order regardless of "ts".
    It's forward-looking scaffolding for a future concurrent-load scenario
    (v1.1), not consumed by this single-client runner today.
    """
    import server  # local import: keeps trace-only callers free of the fastmcp dep

    calls_in = _load_trace(trace_path)

    baseline = None
    if mode == "compare":
        baseline = json.loads(Path(baseline_path).read_text())

    scratch = Path(tempfile.mkdtemp(prefix="engram_replay_bench_"))
    results = []
    try:
        _copy_data_dir(data_dir, scratch)
        # data_dir is explicit here, so engram_sandbox never auto-deletes it
        # (that only applies to its own auto-created tmpdirs) — we own cleanup
        # of `scratch` ourselves, in the `finally` below.
        with server.engram_sandbox(data_dir=scratch):
            for i, call in enumerate(calls_in):
                tool_name = call["tool_name"]
                params = call.get("params", {})
                func = getattr(server, tool_name, None)
                if func is None:
                    raise AttributeError(f"server has no tool function {tool_name!r} (call {i})")
                start = time.perf_counter()
                raw = func(payload_json=json.dumps(params))
                duration_ms = (time.perf_counter() - start) * 1000.0
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {"_unparseable_response": raw}
                results.append({
                    "idx": i,
                    "tool_name": tool_name,
                    "params": params,
                    "duration_ms": duration_ms,
                    "response_masked": _mask(parsed),
                })
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    compare_result = None
    if mode == "record":
        _write_baseline(baseline_path, trace_path, data_dir, results)
    elif mode == "compare":
        compare_result = _compare_against_baseline(results, baseline)

    report = _build_report(trace_path, data_dir, results, mode, compare_result)
    return report, results


# Bumped whenever the report/baseline JSON shape changes (added/removed/
# renamed top-level or per-call keys). The M5 CI perf-gate (future PR) will
# diff these files across commits; a schema_version field lets it detect a
# shape change instead of misreading old and new fields as a regression.
SCHEMA_VERSION = 1


def _write_baseline(path, trace_path, data_dir, results) -> None:
    baseline = {
        "schema_version": SCHEMA_VERSION,
        "trace": str(trace_path),
        "data_dir": str(data_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "calls": [
            {
                "idx": r["idx"],
                "tool_name": r["tool_name"],
                "params": r["params"],
                "response_masked": r["response_masked"],
            }
            for r in results
        ],
    }
    Path(path).write_text(json.dumps(baseline, indent=1, sort_keys=True))


def _compare_against_baseline(results, baseline) -> dict:
    baseline_calls = baseline.get("calls", [])
    trace_mismatches = []
    mismatches = []

    n = min(len(results), len(baseline_calls))
    if len(results) != len(baseline_calls):
        trace_mismatches.append(
            f"call count differs: current={len(results)} baseline={len(baseline_calls)}"
        )

    for i in range(n):
        cur = results[i]
        base = baseline_calls[i]
        if cur["tool_name"] != base["tool_name"] or cur["params"] != base["params"]:
            trace_mismatches.append(
                f"call {i}: trace diverges from baseline "
                f"(current={cur['tool_name']}/{cur['params']!r} "
                f"baseline={base['tool_name']}/{base['params']!r})"
            )
            continue
        for path_, before, after in _diff(base["response_masked"], cur["response_masked"]):
            mismatches.append({
                "idx": i,
                "tool_name": cur["tool_name"],
                "path": path_,
                "before": before,
                "after": after,
            })

    return {
        "compared": n,
        "trace_mismatches": trace_mismatches,
        "mismatched_calls": len({m["idx"] for m in mismatches}),
        "mismatch_count": len(mismatches),
        # Capped detail list — mismatch_count above still reports the true total.
        "mismatches": mismatches[:200],
    }


def _build_report(trace_path, data_dir, results, mode, compare_result=None) -> dict:
    per_tool_durations: dict[str, list[float]] = {}
    for r in results:
        per_tool_durations.setdefault(r["tool_name"], []).append(r["duration_ms"])

    per_tool_stats = {}
    total_ms = 0.0
    for tool_name, durations in per_tool_durations.items():
        s = sorted(durations)
        per_tool_stats[tool_name] = {
            "count": len(s),
            "p50_ms": round(_percentile(s, 50), 3),
            "p90_ms": round(_percentile(s, 90), 3),
            "p99_ms": round(_percentile(s, 99), 3),
            "total_ms": round(sum(s), 3),
        }
        total_ms += sum(s)

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace": str(trace_path),
        "data_dir": str(data_dir),
        "call_count": len(results),
        "total_ms": round(total_ms, 3),
        "mode": mode or "plain",
        "per_tool": per_tool_stats,
    }
    if compare_result is not None:
        report["compare_result"] = compare_result
    return report


# ---------------------------------------------------------------------------
# Concurrency scenario (mode 1) — v1.1
# ---------------------------------------------------------------------------
#
# v1's run_replay() above is strictly sequential (never more than one call in
# flight), which structurally cannot reproduce the May 2026 perf incident this
# harness is chasing: telemetry forensics found the incident was NOT "reads
# are slow" (steady-state p50 is 48ms) but a burst where many concurrent tool
# calls arrived at ONE MCP server process (agent fairies sharing their parent
# session's server) and queued/degraded under contention — durations
# escalating call-over-call within a single 47-minute burst, ~15s -> ~24s.
#
# FastMCP's actual dispatch model: every ENGRAM MCP tool function
# (server.engram_inspect, server.engram_query, ...) is a plain `def`, not
# `async def`. FastMCP (fastmcp/server/dependencies.py's
# call_sync_fn_in_threadpool) dispatches sync tool calls to a THREAD POOL, not
# the asyncio event loop directly — so real concurrent tool-call arrival is
# real OS threads calling into the same imported `server` module concurrently.
# A concurrent.futures.ThreadPoolExecutor reproduces this faithfully.
#
# Design: ONE scratch data-dir copy + ONE `server.engram_sandbox(...)` + ONE
# `import server` are shared across every concurrency level and every worker
# thread in a run_concurrent_replay() call — this is what makes the scenario
# "real concurrent arrival at one server," not N independent processes. For
# each concurrency level N, the SAME trace is resubmitted from scratch through
# a fresh ThreadPoolExecutor(max_workers=N) so results are directly comparable
# across N (1 -> 4 -> 8, ...).
#
# Does NOT run the --record/--compare semantic-equivalence oracle (out of
# scope for this scenario — v1's sequential mode already covers content
# correctness; this scenario is about latency/contention shape only). Also
# does not attempt multi-process / separate-DB-connection concurrency (a
# different, lower-priority mode deferred out of this PR).

# Independent from v1's SCHEMA_VERSION above — this is a structurally
# different report shape (per-N results, not a single flat per-tool table)
# serving a different purpose, so it gets its own version counter rather than
# being conflated with the sequential-mode schema.
CONCURRENCY_SCHEMA_VERSION = 1


def _run_one_concurrency_level(calls_in: list[dict], n: int) -> list[dict]:
    """Submit every call in calls_in through a ThreadPoolExecutor(max_workers=n)
    against the CURRENTLY ACTIVE server.engram_sandbox (the caller is
    responsible for having one open) and return one result dict per call,
    ordered by trace idx (NOT by completion order — as_completed() is used
    only to collect results as they finish; the return list is re-sorted by
    idx so downstream stats are trace-order-stable regardless of thread
    scheduling)."""
    import server  # local import: mirrors run_replay()'s per-function import style

    def _call_one(idx: int, call: dict) -> dict:
        tool_name = call["tool_name"]
        params = call.get("params", {})
        func = getattr(server, tool_name, None)
        if func is None:
            raise AttributeError(f"server has no tool function {tool_name!r} (call {idx})")
        start = time.perf_counter()
        func(payload_json=json.dumps(params))
        duration_ms = (time.perf_counter() - start) * 1000.0
        return {
            "idx": idx,
            "tool_name": tool_name,
            "start_s": start,
            "duration_ms": duration_ms,
            "thread_id": threading.get_ident(),
        }

    results = []
    with ThreadPoolExecutor(max_workers=n) as executor:
        futures = [executor.submit(_call_one, i, call) for i, call in enumerate(calls_in)]
        for fut in as_completed(futures):
            results.append(fut.result())
    results.sort(key=lambda r: r["idx"])
    return results


def _queueing_signature(durations_ms_by_start: list[float]) -> dict:
    """Split call durations (already ordered by call START time, not trace idx
    or completion order) into a first-half / second-half and report the ratio
    of second-half mean duration to first-half mean duration.

    This is the specific signal the May 2026 perf incident showed: durations
    escalating call-over-call WITHIN a single burst as contention built up
    (~15s -> ~24s across a 47-minute window) — a shape of degradation, not
    mere steady-state slowness. escalation_ratio near 1.0 means no escalation
    (durations stayed flat across the run); > 1.0 means later calls in the
    SAME run were slower than earlier ones — the queueing signature this
    scenario is built to surface.

    Deliberately a simple mean-ratio over a 50/50 split rather than a fancier
    trend statistic (e.g. a linear-regression slope) — a clear, correctly
    computed signal beats an over-engineered one here (see PR body for the
    call).
    """
    n = len(durations_ms_by_start)
    if n < 2:
        return {
            "first_half_mean_ms": None,
            "second_half_mean_ms": None,
            "escalation_ratio": None,
            "note": "fewer than 2 calls — no signature computed",
        }
    mid = n // 2
    first_half = durations_ms_by_start[:mid]
    second_half = durations_ms_by_start[mid:]
    first_mean = sum(first_half) / len(first_half)
    second_mean = sum(second_half) / len(second_half)
    ratio = (second_mean / first_mean) if first_mean > 0 else None
    return {
        "first_half_mean_ms": round(first_mean, 3),
        "second_half_mean_ms": round(second_mean, 3),
        "escalation_ratio": round(ratio, 3) if ratio is not None else None,
        # Reviewer-fairy finding (PR #1677 round 2): this harness is a CLOSED-LOOP
        # benchmark (all calls submitted at once, pool drains) — every run also
        # pays a one-time per-thread first-use cost (first sqlite3.connect() on a
        # fresh worker thread, cold caches) concentrated in roughly the first N-ish
        # calls. That decay, not queueing, is why escalation_ratio reads BELOW 1.0
        # on essentially any invocation of this harness — it does NOT mean "no
        # contention happened." The real May-2026 incident was OPEN-LOOP (sustained
        # arrival rate over 47 minutes, queue depth GREW over time) — a different
        # queue topology than this batch-submission scenario reproduces. For the
        # incident's actual signature, read the per-tool p90/p99 TAIL LATENCY
        # against concurrency level N in the same report (`by_concurrency.*.per_tool`)
        # — that cross-N comparison is the reliable signal this scenario provides;
        # escalation_ratio is a secondary, warm-up-confounded diagnostic, not the
        # headline number.
        "note": (
            "closed-loop/warm-up-confounded: expect <1.0 by construction (per-thread "
            "first-use decay dominates the first half of any run); this is NOT a "
            "queueing-absence signal. Use per-tool p90/p99 vs concurrency level for "
            "the incident's actual (open-loop) escalation signature instead."
        ),
    }


def run_concurrent_replay(trace_path, data_dir, concurrency_levels: list[int]):
    """Replay a trace CONCURRENTLY against ONE shared sandboxed server, once
    per concurrency level in concurrency_levels (in the given order), and
    report per-N latency stats including the queueing-signature check.

    Args:
        trace_path: JSONL trace (see trace_from_telemetry.py).
        data_dir: real ENGRAM data dir to copy from (never modified).
        concurrency_levels: list of worker-count values (e.g. [1, 4, 8]) to
            run the SAME trace through, once each, so results are comparable.

    Returns:
        (report_dict, {n: [per-call-result-dict, ...], ...})

    See the module-level comment above this function for the full mechanism
    writeup (why threads-in-one-process reproduces FastMCP's real dispatch
    model, and why this is a separate scenario from run_replay()'s sequential
    mode).
    """
    import server  # local import: keeps trace-only callers free of the fastmcp dep

    calls_in = _load_trace(trace_path)

    scratch = Path(tempfile.mkdtemp(prefix="engram_replay_bench_concurrency_"))
    per_n_results: dict[int, list[dict]] = {}
    try:
        _copy_data_dir(data_dir, scratch)
        # One shared sandbox for every concurrency level below — all worker
        # threads across all N values hit this same imported `server` module /
        # same underlying data dir, matching the "one server process" shape
        # the incident showed.
        with server.engram_sandbox(data_dir=scratch):
            for n in concurrency_levels:
                per_n_results[n] = _run_one_concurrency_level(calls_in, n)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    report = _build_concurrency_report(trace_path, data_dir, per_n_results, concurrency_levels)
    return report, per_n_results


def _build_concurrency_report(trace_path, data_dir, per_n_results, concurrency_levels) -> dict:
    by_concurrency = {}
    for n in concurrency_levels:
        results = per_n_results[n]
        by_start = sorted(results, key=lambda r: r["start_s"])
        durations_ms_by_start = [r["duration_ms"] for r in by_start]

        per_tool_durations: dict[str, list[float]] = {}
        for r in results:
            per_tool_durations.setdefault(r["tool_name"], []).append(r["duration_ms"])
        per_tool_stats = {}
        for tool_name, durations in per_tool_durations.items():
            s = sorted(durations)
            per_tool_stats[tool_name] = {
                "count": len(s),
                "p50_ms": round(_percentile(s, 50), 3),
                "p90_ms": round(_percentile(s, 90), 3),
                "p99_ms": round(_percentile(s, 99), 3),
                "total_ms": round(sum(s), 3),
            }

        wall_clock_ms = None
        if by_start:
            first_start = by_start[0]["start_s"]
            last_end = max(r["start_s"] + r["duration_ms"] / 1000.0 for r in by_start)
            wall_clock_ms = round((last_end - first_start) * 1000.0, 3)

        by_concurrency[str(n)] = {
            "concurrency": n,
            "call_count": len(results),
            "wall_clock_ms": wall_clock_ms,
            "distinct_threads_used": len({r["thread_id"] for r in results}),
            "per_tool": per_tool_stats,
            "queueing_signature": _queueing_signature(durations_ms_by_start),
        }

    return {
        "schema_version": CONCURRENCY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace": str(trace_path),
        "data_dir": str(data_dir),
        "concurrency_levels": list(concurrency_levels),
        "by_concurrency": by_concurrency,
    }


def _render_concurrency_markdown(report: dict) -> str:
    lines = [
        "# Replay bench concurrency report",
        "",
        f"- trace: `{report['trace']}`",
        f"- data_dir: `{report['data_dir']}`",
        f"- concurrency levels: {report['concurrency_levels']}",
        "",
    ]
    for n in report["concurrency_levels"]:
        entry = report["by_concurrency"][str(n)]
        qs = entry["queueing_signature"]
        wall_clock = f"{entry['wall_clock_ms']:.1f} ms" if entry["wall_clock_ms"] is not None else "n/a"
        lines += [
            f"## N = {n}",
            "",
            f"- calls: {entry['call_count']}",
            f"- wall clock: {wall_clock}",
            f"- distinct threads used: {entry['distinct_threads_used']}",
        ]
        if qs["escalation_ratio"] is not None:
            lines.append(
                f"- queueing signature: first-half mean {qs['first_half_mean_ms']:.1f} ms -> "
                f"second-half mean {qs['second_half_mean_ms']:.1f} ms "
                f"(escalation ratio {qs['escalation_ratio']:.2f}x)"
            )
            # Always render the caveat alongside the number itself — a bare ratio
            # with no note reads as "no contention" to a reader who wasn't in this
            # PR's discussion (PR #1677 review finding). See _queueing_signature's
            # `note` field for the full explanation.
            lines.append(f"  - ⚠️ {qs['note']}")
        else:
            lines.append(f"- queueing signature: {qs.get('note', 'n/a')}")
        lines += [
            "",
            "| tool | count | p50 ms | p90 ms | p99 ms | total ms |",
            "|---|---|---|---|---|---|",
        ]
        for tool_name in sorted(entry["per_tool"]):
            s = entry["per_tool"][tool_name]
            lines.append(
                f"| {tool_name} | {s['count']} | {s['p50_ms']:.1f} | "
                f"{s['p90_ms']:.1f} | {s['p99_ms']:.1f} | {s['total_ms']:.1f} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _render_markdown(report: dict) -> str:
    lines = [
        "# Replay bench report",
        "",
        f"- trace: `{report['trace']}`",
        f"- data_dir: `{report['data_dir']}`",
        f"- mode: {report['mode']}",
        f"- calls: {report['call_count']}",
        f"- total: {report['total_ms']:.1f} ms",
        "",
        "| tool | count | p50 ms | p90 ms | p99 ms | total ms |",
        "|---|---|---|---|---|---|",
    ]
    for tool_name in sorted(report["per_tool"]):
        s = report["per_tool"][tool_name]
        lines.append(
            f"| {tool_name} | {s['count']} | {s['p50_ms']:.1f} | "
            f"{s['p90_ms']:.1f} | {s['p99_ms']:.1f} | {s['total_ms']:.1f} |"
        )

    if "compare_result" in report:
        cr = report["compare_result"]
        lines += [
            "",
            "## Semantic-equivalence compare",
            "",
            f"- calls compared: {cr['compared']}",
            f"- calls with mismatches: {cr['mismatched_calls']}",
            f"- total mismatches: {cr['mismatch_count']}",
        ]
        if cr["trace_mismatches"]:
            lines.append("")
            lines.append("Trace divergences (baseline was recorded against a different trace):")
            for m in cr["trace_mismatches"][:20]:
                lines.append(f"- {m}")
        if cr["mismatches"]:
            lines.append("")
            lines.append("First mismatches:")
            for m in cr["mismatches"][:20]:
                lines.append(
                    f"- call {m['idx']} ({m['tool_name']}) field `{m['path']}`: "
                    f"before=`{m['before']}` after=`{m['after']}`"
                )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_concurrency_levels(raw: str) -> list[int]:
    """argparse `type=` callback for --concurrency: 'N1,N2,...' -> [N1, N2, ...]."""
    levels = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid concurrency level {part!r} (must be an int)")
        if n < 1:
            raise argparse.ArgumentTypeError(f"concurrency level must be >= 1, got {n}")
        levels.append(n)
    if not levels:
        raise argparse.ArgumentTypeError("--concurrency requires at least one level, e.g. '1,4,8'")
    return levels


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Replay an ENGRAM MCP trace against a sandboxed graph copy "
                    "and report per-tool timing (+ optional semantic-equivalence oracle, "
                    "or a concurrent-load scenario — see --concurrency)."
    )
    parser.add_argument("--trace", required=True, help="Trace JSONL (see trace_from_telemetry.py).")
    parser.add_argument("--data-dir", required=True, help="Real ENGRAM data dir to copy from.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--record", metavar="BASELINE_PATH",
                        help="Capture a masked-response baseline at this path.")
    group.add_argument("--compare", metavar="BASELINE_PATH",
                        help="Diff this run's masked responses against a recorded baseline.")
    group.add_argument("--concurrency", metavar="N1,N2,...", type=_parse_concurrency_levels,
                        help="Run the CONCURRENT replay scenario (mode 1) instead of the "
                             "sequential replay: for each comma-separated worker-count N, "
                             "submit the whole trace through a ThreadPoolExecutor(max_workers=N) "
                             "against ONE shared sandboxed server, and report per-N latency "
                             "stats + the queueing-signature (does the run's later calls run "
                             "slower than its earlier ones?). Mutually exclusive with "
                             "--record/--compare — the semantic-equivalence oracle does not run "
                             "under concurrency in this mode (see run_concurrent_replay's "
                             "docstring).")
    parser.add_argument("--json-out", help="Write the machine-readable report as JSON here.")
    parser.add_argument("--md-out", help="Write the human-readable report as markdown here "
                                          "(default: print to stdout).")
    args = parser.parse_args(argv)

    if args.concurrency:
        report, _per_n_results = run_concurrent_replay(args.trace, args.data_dir, args.concurrency)

        if args.json_out:
            Path(args.json_out).write_text(json.dumps(report, indent=1, sort_keys=True))

        md = _render_concurrency_markdown(report)
        if args.md_out:
            Path(args.md_out).write_text(md)
        else:
            print(md)
        return

    mode = None
    baseline_path = None
    if args.record:
        mode = "record"
        baseline_path = args.record
    elif args.compare:
        mode = "compare"
        baseline_path = args.compare

    report, _results = run_replay(args.trace, args.data_dir, mode=mode, baseline_path=baseline_path)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=1, sort_keys=True))

    md = _render_markdown(report)
    if args.md_out:
        Path(args.md_out).write_text(md)
    else:
        print(md)

    if mode == "compare":
        cr = report["compare_result"]
        if cr["mismatch_count"] or cr["trace_mismatches"]:
            # Signals a semantic-equivalence regression (or a stale/mismatched
            # baseline trace) to any caller checking the exit code — e.g. a
            # future CI perf-gate PR (not part of this harness's own scope).
            sys.exit(1)


if __name__ == "__main__":
    main()
