"""Contract tests for tools/engramd_a6_latency.py, the U4d A6 latency-delta
measurement script (#3197, part of #2652). The measurement itself is owed in
the PR body and is NOT a CI gate; these tests pin that the SCRIPT measures what
it claims to.

Written BEFORE the script (contract-first). The contract, from the U4d spec:

  * the owner arm talks to a REAL owner process (never an echo server) -- a
    separate process, launched with `engramd_owner` in its argv (#3285: the
    pid-liveness drift guard keys on that marker);
  * baseline and owner arms are measured in the SAME run, same store copy, same
    N (>= 300 enforced by the CLI), and report p50 and p99;
  * extra arms -- contended (two clients) and cold cache -- each carry their own
    same-arm baseline;
  * it never touches the live store: a --store that resolves to the seat's own
    $ENGRAM_HOME/knowledge.db is refused before anything is launched;
  * the table names host, Python and SQLite build.

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import sqlite3
import sys
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "engramd_a6_latency.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("engramd_a6_latency", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def smoke(tool):
    return tool.measure(n=20, nodes=300, warmup=5)


@requires_linux_owner
def test_launcher_argv_carries_the_owner_marker_and_the_process_is_real(tool):
    """#3285 obligation: the launched owner's REAL command line (read from
    /proc, not from the argv we built) contains `engramd_owner`, and it is a
    different process from the test. Mutation: launch via `python -c` / a
    wrapper without the module path in argv -> red."""
    if not Path("/proc").is_dir():
        pytest.skip("needs /proc")
    assert any("engramd_owner" in a for a in tool.owner_argv())
    handle = tool.launch_owner(tool.make_test_store(nodes=50))
    try:
        assert handle.proc.pid != os.getpid()
        cmdline = Path(f"/proc/{handle.proc.pid}/cmdline").read_bytes()
        assert b"engramd_owner" in cmdline
    finally:
        handle.stop()
    assert handle.proc.poll() is not None


@requires_linux_owner
def test_every_arm_has_a_same_run_baseline_and_both_percentiles(smoke):
    """Mutation: drop the baseline arm of any pair, or omit p99 -> red."""
    arms = {(r["arm"], r["path"]): r for r in smoke["rows"]}
    for arm in ("sequential", "contended", "cold-cache"):
        for path in ("baseline", "owner"):
            r = arms[(arm, path)]
            assert r["n"] >= 20
            assert 0 < r["p50_ms"] <= r["p99_ms"]


@requires_linux_owner
def test_the_owner_arm_returns_the_same_people_as_the_baseline_arm(smoke):
    """Anti-vacuity: a fast wrong/empty answer must not count as fast. Mutation:
    make the owner arm return [] without reading -> red."""
    assert smoke["people_match"] is True
    assert smoke["people_count"] > 0
    assert smoke["owner_pid"] != os.getpid()


@requires_linux_owner
def test_the_table_names_host_python_and_sqlite_build(tool, smoke):
    """Mutation: omit any of the three -> red."""
    table = tool.format_table(smoke)
    assert platform.platform() in table
    assert sys.version.split()[0] in table
    assert sqlite3.sqlite_version in table
    assert "baseline" in table and "owner" in table


def test_cli_refuses_a_store_that_is_the_seats_live_store(tool, tmp_path, monkeypatch, capsys):
    """Never test against production ENGRAM. Mutation: skip the live-store
    check -> an owner would be launched (the launcher is rigged to fail)."""
    home = tmp_path / "h"
    home.mkdir()
    (home / "knowledge.db").write_bytes(b"")
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.setattr(tool, "launch_owner", lambda *a, **k: pytest.fail("launched against the live store"))
    assert tool.main(["--store", str(home / "knowledge.db")]) == 2
    assert "live" in capsys.readouterr().err.lower()


def test_cli_enforces_the_n_floor(tool, monkeypatch):
    """Spec: N >= 300 for the measurement that goes in the PR. Mutation: drop
    the floor -> red."""
    monkeypatch.setattr(tool, "launch_owner", lambda *a, **k: pytest.fail("should refuse first"))
    assert tool.main(["--n", "50"]) == 2
