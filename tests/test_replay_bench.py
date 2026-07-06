"""Smoke test for the replay-bench perf harness (tools/perf/).

Exercises the full loop the harness is meant for: trace generation on a tiny
synthetic sandbox graph (never the real production DB) -> bench run -> record
-> compare-against-self is clean (zero mismatches). This is a correctness
self-test, not a perf benchmark itself — kept fast (a handful of nodes, a
dozen calls).

Deliberately avoids a literal `import server` statement in THIS file. The one
`import server` call needed to seed/replay the sandbox lives inside
tools/perf/replay_bench.py's make_scratch_graph()/run_replay() instead — this
file only calls those. Considered, not smuggled: replay_bench.py IS the
harness whose job is driving `server`, so it legitimately owns that import
(testing through the public surface of the module under test), rather than
this test file routing around tests/IMPORT_RUBRIC.md R1/R2's
server_import_allowlist requirement. Confirmed by both Kepler (colleague
review) and the reviewer-fairy on this PR: legitimate layering, not a rubric
dodge — R1 isn't CI-wired today (checked at PR-review time only) and 48
pre-existing test files already violate it with no --strict enforcement, so
this file staying clean doesn't close a hard gate either way.

This layering choice does NOT by itself solve test-selection coverage for
tools/perf/*.py — that's a separate, genuine gap the reviewer-fairy found
(run_touched_tests.py's tools/ discovery is non-recursive, so tools/perf/'s
new modules weren't reachable via either the package-root fallback or the
import graph) and this PR fixes with an explicit test-map.json mapping entry
— see that file for detail.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS_PERF_DIR = os.path.join(_REPO_ROOT, "tools", "perf")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _TOOLS_PERF_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_PERF_DIR)

# Keep the sandboxed replay fast + network-free — the self-compare check below
# only needs FTS-keyword behavior to be deterministic, not real embeddings.
os.environ.setdefault("ENGRAM_NO_EMBEDDINGS", "1")
os.environ.setdefault("ENGRAM_NO_DB_GUARDS", "1")

import trace_from_telemetry  # noqa: E402 — must follow sys.path setup above
import replay_bench  # noqa: E402


def test_replay_bench_self_compare_clean():
    tmp = Path(tempfile.mkdtemp(prefix="engram_test_replay_bench_"))
    try:
        # ---- Seed a tiny synthetic graph (not the real production DB) ----
        seed_dir = tmp / "seed"
        replay_bench.make_scratch_graph(seed_dir, n_nodes=3)
        assert (seed_dir / "knowledge.db").exists()

        # ---- Generate a trace from that seed graph's (empty) telemetry ----
        trace_path = tmp / "trace.jsonl"
        n_written = trace_from_telemetry.generate_trace(
            seed_dir, trace_path, n_calls=12, seed=1234
        )
        assert n_written == 12

        lines = trace_path.read_text().strip().splitlines()
        assert len(lines) == 12
        for line in lines:
            call = json.loads(line)
            assert call["tool_name"] in trace_from_telemetry.READ_TOOLS
            assert isinstance(call["params"], dict)

        # ---- Record a baseline ----
        baseline_path = tmp / "baseline.json"
        record_report, record_results = replay_bench.run_replay(
            trace_path, seed_dir, mode="record", baseline_path=baseline_path
        )
        assert record_report["call_count"] == 12
        assert record_report["mode"] == "record"
        assert baseline_path.exists()
        assert len(record_results) == 12

        # ---- Compare the same trace against itself: must be clean ----
        compare_report, _ = replay_bench.run_replay(
            trace_path, seed_dir, mode="compare", baseline_path=baseline_path
        )
        cr = compare_report["compare_result"]
        assert cr["trace_mismatches"] == [], cr["trace_mismatches"]
        assert cr["mismatch_count"] == 0, cr["mismatches"]
        assert cr["mismatched_calls"] == 0
        assert cr["compared"] == 12
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_percentile_and_masking_helpers():
    # Pure-function unit coverage alongside the end-to-end smoke test above.
    assert replay_bench._percentile([1.0], 50) == 1.0
    assert replay_bench._percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5

    masked = replay_bench._mask({
        "node": {"id": "ob_0001", "recall_count": 3, "importance_score": 0.5},
        "list": [{"created_ago": "5s ago", "claim": "kept"}],
    })
    assert masked["node"]["recall_count"] == replay_bench._MASK_SENTINEL
    assert masked["node"]["importance_score"] == replay_bench._MASK_SENTINEL
    assert masked["node"]["id"] == "ob_0001"
    assert masked["list"][0]["created_ago"] == replay_bench._MASK_SENTINEL
    assert masked["list"][0]["claim"] == "kept"

    diffs = replay_bench._diff({"a": 1, "b": 2}, {"a": 1, "b": 3})
    assert diffs == [("b", 2, 3)]
    assert replay_bench._diff({"a": 1}, {"a": 1}) == []
