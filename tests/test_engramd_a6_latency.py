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

from _owner_platform import requires_linux_kernel, requires_linux_owner

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
    assert any("engramd_owner" in a for a in tool.owner_argv())
    handle = tool.launch_owner(tool.make_test_store(nodes=50))
    try:
        assert handle.proc.pid != os.getpid()
        if Path("/proc").is_dir():
            cmdline = Path(f"/proc/{handle.proc.pid}/cmdline").read_bytes()
        else:  # no /proc (macOS): the process table's own command line, same fact (#3666)
            import subprocess as _sp
            cmdline = _sp.run(["ps", "-o", "command=", "-p", str(handle.proc.pid)],
                              capture_output=True, check=True).stdout
        assert b"engramd_owner" in cmdline
    finally:
        handle.stop()
    assert handle.proc.poll() is not None


@requires_linux_owner
@requires_linux_kernel  # (a) Linux-only: the A6 tool reads /proc and calls os.posix_fadvise; porting the tool is out of scope (#3666)
def test_every_arm_has_a_same_run_baseline_and_both_percentiles(smoke):
    """Mutation: drop the baseline arm of any pair, or omit p99 -> red."""
    arms = {(r["arm"], r["path"]): r for r in smoke["rows"]}
    for arm in ("sequential", "contended", "cold-cache"):
        for path in ("baseline", "owner"):
            r = arms[(arm, path)]
            assert r["n"] >= 20
            assert 0 < r["p50_ms"] <= r["p99_ms"]


@requires_linux_owner
@requires_linux_kernel  # (a) Linux-only: the A6 tool reads /proc and calls os.posix_fadvise; porting the tool is out of scope (#3666)
def test_the_owner_arm_returns_the_same_people_as_the_baseline_arm(smoke):
    """Anti-vacuity: a fast wrong/empty answer must not count as fast. Mutation:
    make the owner arm return [] without reading -> red."""
    assert smoke["people_match"] is True
    assert smoke["people_count"] > 0
    assert smoke["owner_pid"] != os.getpid()


@requires_linux_owner
@requires_linux_kernel  # (a) Linux-only: the A6 tool reads /proc and calls os.posix_fadvise; porting the tool is out of scope (#3666)
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


# --- U6a (#3664): `--hook stop` ---------------------------------------------------
#
# Contract (U6a spec C7): the stop hook's owner read vs its direct-connect path, same
# store / host / warm cache; the user-identity default and its output unchanged; the
# N >= 300 floor and the live-store refusal kept for the new selector; the SQLite
# build named; contended and cold-cache arms reported but not gates. The live
# measurement is the Linux seat's (the owner cannot start on a Mac); the cells below
# that need a running owner either carry `requires_linux_owner` or use the in-process
# FAKE owner (tests/_fake_owner.py) with the launcher and the page-cache eviction
# stubbed, so the script's stop wiring is exercised on every host.

import types  # noqa: E402


def _fake_launch(tool, monkeypatch):
    from _fake_owner import FakeOwner

    def launch(home):
        owner = FakeOwner(home)
        owner.start()
        return types.SimpleNamespace(
            proc=types.SimpleNamespace(pid=os.getpid(), poll=lambda: None),
            home=home, socket_path=owner.socket_path, stop=owner.stop)

    monkeypatch.setattr(tool, "launch_owner", launch)
    monkeypatch.setattr(tool, "_fadvise_cold", lambda db: None)  # posix_fadvise is Linux-only


def test_hook_selector_defaults_to_user_identity_and_passes_stop_through(tool, monkeypatch):
    """The default stays `user-identity` (its measurement, table text and exit code
    unchanged) and `--hook stop` selects the new path. Mutation: default to `stop`,
    ignore `--hook`, or accept an unknown hook name -> red."""
    # #3678: main() now measures BOTH layouts (installed = the gate cell) through
    # measure_in_layout(); the hook routing pinned here is unchanged. Each run of
    # main() hands its hook to every layout it measures.
    seen = []

    def fake_measure_in_layout(layout, **kw):
        seen.append((layout.name, kw["hook"]))
        return {"answers_match": True, "layout": layout.name}

    monkeypatch.setattr(tool, "measure_in_layout", fake_measure_in_layout)
    monkeypatch.setattr(tool, "build_installed_layout", lambda out: tool.Layout(
        name="installed", root=out, stop_hook=out / "hooks" / "s.py", user_hook=out / "hooks" / "u.py"))
    monkeypatch.setattr(tool, "format_layouts", lambda rs: "table")
    assert tool.main([]) == 0
    assert tool.main(["--hook", "stop"]) == 0
    assert seen == [("installed", "user-identity"), ("source", "user-identity"),
                    ("installed", "stop"), ("source", "stop")]
    with pytest.raises(SystemExit) as exc:
        tool.main(["--hook", "surface"])
    assert exc.value.code == 2


def test_stop_selector_keeps_the_n_floor_and_the_live_store_refusal(tool, tmp_path, monkeypatch, capsys):
    """The floor and the live-store refusal apply to `--hook stop` exactly as to the
    default, before anything is launched. Mutation: skip either check on the stop
    branch -> an owner would be launched (the launcher is rigged to fail)."""
    monkeypatch.setattr(tool, "launch_owner", lambda *a, **k: pytest.fail("launched"))
    assert tool.main(["--hook", "stop", "--n", "50"]) == 2
    home = tmp_path / "h"
    home.mkdir()
    (home / "knowledge.db").write_bytes(b"")
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    assert tool.main(["--hook", "stop", "--store", str(home / "knowledge.db")]) == 2
    assert "live" in capsys.readouterr().err.lower()


def test_stop_measurement_compares_the_raw_count_and_names_the_build_on_any_host(tool, monkeypatch):
    """The stop wiring end to end against the fake owner: every arm has a same-run
    baseline with both percentiles; the owner count equals the direct count (305 =
    300 observations + 5 person nodes, all current); the table names host, Python and
    the SQLite build, and says the arms other than sequential are not gates.
    Mutation: drop the SQLite build, drop a baseline arm, or report the count of
    another population -> red."""
    _fake_launch(tool, monkeypatch)
    result = tool.measure(n=20, nodes=300, warmup=5, hook="stop")
    assert result["hook"] == "stop" and result["answers_match"] is True
    assert result["answer_count"] == 305
    arms = {(r["arm"], r["path"]): r for r in result["rows"]}
    for arm in ("sequential", "contended", "cold-cache"):
        for path in ("baseline", "owner"):
            assert 0 < arms[(arm, path)]["p50_ms"] <= arms[(arm, path)]["p99_ms"]
    table = tool.format_table(result)
    assert platform.platform() in table and sys.version.split()[0] in table
    assert sqlite3.sqlite_version in table
    assert "305 current nodes" in table and "not gates" in table


def test_stop_measurement_goes_red_when_the_owner_count_is_off_by_one(tool, monkeypatch):
    """The measurement must be able to fail: shift the owner's count by one in flight
    and `answers_match` is False. Mutation: compare only the decisions (305 vs 306 are
    both 'muted' at the default threshold) -> stays True -> red."""
    import engramd_client as ec

    _fake_launch(tool, monkeypatch)
    original = ec.read

    def shifted(*a, **k):
        r = original(*a, **k)
        return ec.Ok(result=[[r.result[0][0] + 1]]) if isinstance(r, ec.Ok) and a and a[0] == "read" else r

    monkeypatch.setattr(ec, "read", shifted)
    assert tool.measure(n=20, nodes=300, warmup=5, hook="stop")["answers_match"] is False


def test_default_measurement_output_shape_is_unchanged_for_user_identity(tool, monkeypatch):
    """The user-identity result keeps its keys and its table its wording (the
    existing live smoke tests read them). Mutation: rename `people_match` /
    `people_count` or reword the rows line -> red. (Uses the fake owner: the person
    read goes through the same client wire.)"""
    _fake_launch(tool, monkeypatch)
    result = tool.measure(n=20, nodes=100, people=3, warmup=2)
    assert result["hook"] == "user-identity"
    assert result["people_match"] is True and result["people_count"] == 3
    table = tool.format_table(result)
    assert "owner rows == baseline rows: True" in table and "person rows" in table


@requires_linux_owner
@requires_linux_kernel  # (a) Linux-only: the A6 tool reads /proc and calls os.posix_fadvise; porting the tool is out of scope (#3666)
def test_stop_measurement_against_a_real_owner(tool):
    """Live cell (Linux seat): the stop selector against a REAL owner process, with
    the real page-cache eviction. Mutation: a launcher that does not start a real
    owner, or a stop wiring that never reaches it -> red."""
    result = tool.measure(n=20, nodes=300, warmup=5, hook="stop")
    assert result["answers_match"] is True and result["answer_count"] == 305
    assert result["owner_pid"] != os.getpid()
