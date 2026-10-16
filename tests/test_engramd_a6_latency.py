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

    # The fake owner answers on handler threads INSIDE this process; the real owner is a separate
    # process the open counter never sees. Its handler threads are therefore exempt from the
    # (all-threads) counter, else its own store reads would be charged to the hook.
    real_handle = FakeOwner._handle

    def exempt_handle(self, conn):
        tool.exempt_this_thread_from_open_counting()
        return real_handle(self, conn)

    monkeypatch.setattr(FakeOwner, "_handle", exempt_handle)
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
        tool.main(["--hook", "nonesuch"])  # (was "surface" until #3821 made that a real arm)
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


# --- U6b stage 2c-2 (#3665): `--hook tripwire` --------------------------------------
#
# Contract (stage 2c-2 spec, U8 #3710 gate): the arm measures ONE FULL FIRE of the
# lesson-tripwire hook's main() two ways on one store copy -- baseline (flag OFF, the
# hook opens the store) and owner (flag ON, a real owner) -- in the source layout and in
# the INSTALLED layout, with an in-context-exclusion switch. Result keys: `hook`,
# `exclusion`, `probe_dir_live`, `recall_reached` (per fire), `stdout_match`,
# `opens_flag_on`, `opens_flag_off`. `u8_contract_violations(result)` is the pure gate:
# opens_flag_on != 0 with exclusion ON, recall not reached, stdout differs,
# probe_dir_live != exclusion (exclusion ON only). `main` exits 3 on any violation.
# The cells use the in-process FAKE owner (every host) except the ones marked
# requires_linux_owner (a REAL owner process; cold-cache eviction stubbed on a Mac).

HOOKS_SRC = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
TRIPWIRE_RESULT_KEYS = ("hook", "exclusion", "probe_dir_live", "recall_reached", "stdout_match",
                        "opens_flag_on", "opens_flag_off")


@pytest.fixture(scope="module")
def tripwire_runs(tool):
    """One reduced-N fake-owner run per exclusion state (source layout, in process)."""
    mp = pytest.MonkeyPatch()
    _fake_launch(tool, mp)
    try:
        return {ex: tool.measure(n=20, nodes=300, warmup=3, hook="tripwire", exclusion=ex)
                for ex in (False, True)}
    finally:
        mp.undo()  # (not held across the module: the real-owner cells below must see the real launcher)


_MISSING = object()  # an override value meaning "this key is absent from the result"


def _clear_result(**over):
    r = {"hook": "tripwire", "exclusion": True, "probe_dir_live": True, "recall_reached": True,
         "stdout_match": True, "opens_flag_on": 0, "opens_flag_off": 4}
    r.update(over)
    return {k: v for k, v in r.items() if v is not _MISSING}


def _flat_variant(tmp_path, hook_transform=None, hooklib_transform=None):
    """A flattened plugin-root look-alike under tmp_path holding TRANSFORMED copies of the
    tripwire hook and/or _hooklib: the hook + its two sibling helpers in hooks/, every
    runtime module at the root as a symlink (the hook resolves its runtime dir at <root>)."""
    root = tmp_path / "variant"
    (root / "hooks").mkdir(parents=True)
    hook = root / "hooks" / "engram-lesson-tripwire-hook.py"
    for name, transform in (("engram-lesson-tripwire-hook.py", hook_transform), ("_hooklib.py", hooklib_transform),
                            ("_harness.py", None)):
        src = (HOOKS_SRC / name).read_text()
        out = transform(src) if transform else src
        assert (out != src) == (transform is not None)
        (root / "hooks" / name).write_text(out)
    for f in (REPO_ROOT / "src" / "engram").iterdir():
        if f.is_file() and f.suffix in (".py", ".json"):
            (root / f.name).symlink_to(f)
    return hook


def _focused_read_skipped(src):
    """CONTROL: `_hooklib._focused_ids` (the exclusion resolver's focus read) no longer opens
    the store -- the effect the U8 change (#3710) has on a flag-ON fire. Before U8, dev opened
    it directly (twice per exclusion-ON fire: the route-ledger resolver and the exclusion
    resolver); this control keeps the negative cell independent of the tree under test."""
    needle = "conn = connect_regular_ro(db_path, timeout=0.2)"
    assert src.count(needle) == 1
    return src.replace(needle, 'raise OSError("a6 control: focused read skipped")')


def _open_directly_on_flag_on_path(src):
    """The peer reviewer's mutant shape: load_tripwires opens knowledge.db itself even with
    the flag ON (the owner shim is never used by that site)."""
    needle = ("conn = _owner_conn() if owner else connect_regular_ro(db_path, timeout=2.0)\n"
              "        rows = conn.execute(_QUERY).fetchall()")
    assert src.count(needle) == 1
    return src.replace(needle, "conn = connect_regular_ro(db_path, timeout=2.0)\n"
                              "        rows = conn.execute(_QUERY).fetchall()")


# -- CLI --------------------------------------------------------------------------------

def test_tripwire_selector_and_exclusion_switch_pass_through_and_default_off(tool, monkeypatch):
    """`--hook tripwire` is selectable; `--exclusion {off,on}` reaches measure() as a bool
    and defaults to off. Mutation: drop `tripwire` from HOOKS, ignore --exclusion, or
    default it on -> red."""
    seen = []

    def fake_measure_in_layout(layout, **kw):
        seen.append((layout.name, kw["hook"], kw["exclusion"]))
        return dict(_clear_result(), answers_match=True, layout=layout.name)

    monkeypatch.setattr(tool, "measure_in_layout", fake_measure_in_layout)
    monkeypatch.setattr(tool, "build_installed_layout", lambda out: tool.Layout(
        name="installed", root=out, stop_hook=out / "hooks" / "s.py", user_hook=out / "hooks" / "u.py"))
    monkeypatch.setattr(tool, "format_layouts", lambda rs: "table")
    assert tool.main(["--hook", "tripwire", "--layout", "source"]) == 0
    assert tool.main(["--hook", "tripwire", "--exclusion", "on", "--layout", "source"]) == 0
    assert tool.main(["--hook", "tripwire", "--exclusion", "off", "--layout", "source"]) == 0
    assert seen == [("source", "tripwire", False), ("source", "tripwire", True), ("source", "tripwire", False)]


@pytest.mark.parametrize("argv", [
    ["--exclusion", "on"],                       # default hook (user-identity)
    ["--hook", "stop", "--exclusion", "on"],
    ["--hook", "user-identity", "--exclusion", "off"],
    ["--hook", "tripwire", "--exclusion", "maybe"],
])
def test_exclusion_is_valid_only_with_the_tripwire_hook(tool, monkeypatch, argv):
    """Any other combination is an argparse error (exit 2), before anything is launched.
    Mutation: accept --exclusion for every hook, or accept a value outside {off,on} -> red."""
    monkeypatch.setattr(tool, "launch_owner", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(SystemExit) as exc:
        tool.main(argv)
    assert exc.value.code == 2


def test_tripwire_keeps_the_n_floor_and_the_live_store_refusal(tool, tmp_path, monkeypatch, capsys):
    """Mutation: skip either check on the tripwire branch -> an owner would be launched."""
    monkeypatch.setattr(tool, "launch_owner", lambda *a, **k: pytest.fail("launched"))
    assert tool.main(["--hook", "tripwire", "--n", "50"]) == 2
    home = tmp_path / "h"
    home.mkdir()
    (home / "knowledge.db").write_bytes(b"")
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    assert tool.main(["--hook", "tripwire", "--store", str(home / "knowledge.db")]) == 2
    assert "live" in capsys.readouterr().err.lower()


# -- the pure gate ------------------------------------------------------------------------

VIOLATION_TABLE = [
    # (id, overrides, one expected substring per expected violation)
    ("all-clear exclusion ON", {}, []),
    ("all-clear exclusion OFF, opens reported not gated",
     {"exclusion": False, "probe_dir_live": False, "opens_flag_on": 7}, []),
    ("exclusion OFF ignores probe_dir_live",
     {"exclusion": False, "probe_dir_live": True}, []),
    ("opens_flag_on != 0 with exclusion ON", {"opens_flag_on": 1}, ["opens_flag_on"]),
    ("opens_flag_on missing with exclusion ON", {"opens_flag_on": None}, ["opens_flag_on"]),
    ("recall not reached (exclusion ON)", {"recall_reached": False}, ["recall_reached"]),
    ("recall not reached (exclusion OFF)",
     {"exclusion": False, "probe_dir_live": False, "recall_reached": False}, ["recall_reached"]),
    ("stdout differs", {"stdout_match": False}, ["stdout_match"]),
    ("stdout differs (exclusion OFF)",
     {"exclusion": False, "probe_dir_live": False, "stdout_match": False}, ["stdout_match"]),
    ("probe dir not live with exclusion ON", {"probe_dir_live": False}, ["probe_dir_live"]),
    ("two at once", {"opens_flag_on": 2, "stdout_match": False}, ["opens_flag_on", "stdout_match"]),
    # F1: `exclusion` is itself checked, and an unreadable one fails CLOSED (gates run as if ON)
    ("exclusion missing", {"exclusion": _MISSING}, ["exclusion"]),
    ("exclusion None", {"exclusion": None}, ["exclusion"]),
    ("exclusion 1 (not a bool)", {"exclusion": 1}, ["exclusion"]),
    ("exclusion 'on' (not a bool)", {"exclusion": "on"}, ["exclusion"]),
    ("exclusion missing fails closed: opens gated", {"exclusion": _MISSING, "opens_flag_on": 3},
     ["exclusion", "opens_flag_on"]),
    ("exclusion None fails closed: probe gated", {"exclusion": None, "probe_dir_live": False},
     ["exclusion", "probe_dir_live"]),
    ("exclusion 1 fails closed: opens gated", {"exclusion": 1, "opens_flag_on": 1},
     ["exclusion", "opens_flag_on"]),
    ("exclusion 'on' fails closed: probe gated", {"exclusion": "on", "probe_dir_live": None},
     ["exclusion", "probe_dir_live"]),
    # F2: a missing or None gated value is a violation, never a pass (exclusion ON)
    ("recall_reached missing", {"recall_reached": _MISSING}, ["recall_reached"]),
    ("recall_reached None", {"recall_reached": None}, ["recall_reached"]),
    ("stdout_match missing", {"stdout_match": _MISSING}, ["stdout_match"]),
    ("stdout_match None", {"stdout_match": None}, ["stdout_match"]),
    ("probe_dir_live missing", {"probe_dir_live": _MISSING}, ["probe_dir_live"]),
    ("probe_dir_live None", {"probe_dir_live": None}, ["probe_dir_live"]),
    ("opens_flag_on missing", {"opens_flag_on": _MISSING}, ["opens_flag_on"]),
    # F2, exclusion OFF: recall_reached and stdout_match still apply; probe_dir_live and the open count do not
    ("recall_reached missing (exclusion OFF)", {"exclusion": False, "recall_reached": _MISSING}, ["recall_reached"]),
    ("recall_reached None (exclusion OFF)", {"exclusion": False, "recall_reached": None}, ["recall_reached"]),
    ("stdout_match missing (exclusion OFF)", {"exclusion": False, "stdout_match": _MISSING}, ["stdout_match"]),
    ("stdout_match None (exclusion OFF)", {"exclusion": False, "stdout_match": None}, ["stdout_match"]),
    ("probe_dir_live missing (exclusion OFF) does not apply", {"exclusion": False, "probe_dir_live": _MISSING}, []),
    ("probe_dir_live None (exclusion OFF) does not apply", {"exclusion": False, "probe_dir_live": None}, []),
    ("opens_flag_on missing (exclusion OFF) does not apply", {"exclusion": False, "opens_flag_on": _MISSING}, []),
    ("opens_flag_on None (exclusion OFF) does not apply", {"exclusion": False, "opens_flag_on": None}, []),
]


@pytest.mark.parametrize("name,over,expect", VIOLATION_TABLE, ids=[r[0] for r in VIOLATION_TABLE])
def test_u8_contract_violations_table(tool, name, over, expect):
    """Each violation is reported by name, one line per violation, and the all-clear case
    (both exclusion states) reports none. Mutation: ignore `opens_flag_on`, apply the open
    gate with exclusion OFF, ignore probe_dir_live, or return a bare [] -> a row goes red."""
    got = tool.u8_contract_violations(_clear_result(**over))
    assert isinstance(got, list) and all(isinstance(g, str) for g in got)
    assert len(got) == len(expect), got
    for key in expect:
        assert any(key in g for g in got), (key, got)


def test_u8_contract_violations_does_no_io_and_does_not_mutate_its_input(tool, monkeypatch):
    """A pure function of the result dict. Mutation: open a file / mutate `result` -> red."""
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("I/O in a pure function"))
    r = _clear_result(opens_flag_on=3)
    before = dict(r)
    assert tool.u8_contract_violations(r)
    assert r == before


def test_main_exits_3_on_a_violation_and_prints_it_after_the_table(tool, monkeypatch, capsys):
    """exit 3 is the contract-violation code (0 ok, 1 answers differ, 2 bad input stay).
    The violations print AFTER the table. A clear run and an exclusion-OFF run that merely
    REPORTS opens still exit 0. Mutation: return 0 on violations, print before the table,
    or gate the open count with exclusion OFF -> red."""
    state = {"result": _clear_result()}
    monkeypatch.setattr(tool, "measure_in_layout",
                        lambda layout, **kw: dict(state["result"], answers_match=True, layout=layout.name))
    monkeypatch.setattr(tool, "format_layouts", lambda rs: "THE-TABLE")
    argv = ["--hook", "tripwire", "--layout", "source"]
    assert tool.main(argv) == 0
    state["result"] = _clear_result(exclusion=False, probe_dir_live=False, opens_flag_on=9)
    assert tool.main(argv) == 0
    capsys.readouterr()
    state["result"] = _clear_result(opens_flag_on=2)
    assert tool.main(argv) == 3
    out = capsys.readouterr().out
    assert out.index("THE-TABLE") < out.index("opens_flag_on")
    # exit 1 ("the two paths answered differently") cannot occur for the tripwire arm: a differing
    # stdout is itself a violation, and exit 3 outranks it
    state["result"] = _clear_result(stdout_match=False)
    monkeypatch.setattr(tool, "measure_in_layout",
                        lambda layout, **kw: dict(state["result"], answers_match=False, layout=layout.name))
    assert tool.main(argv) == 3


# -- the direct-open counter (F3) -------------------------------------------------------------
#
# Contract: while `_counting_store_opens(db)` is active, EVERY open of the store file by any route
# in the process is counted exactly once, whatever its spelling and whichever thread makes it; an
# open of another file or of `:memory:` is not counted; an open outside the window is not counted.
# It is built on the `sqlite3.connect` audit event, so the early-bound / `Connection(...)` /
# `_sqlite3.connect` routes are seen too (the spelling cells below are also the check that the
# event fires for each of those routes on this Python).

import threading
from urllib.parse import quote

import _sqlite3
from sqlite3 import connect as _EARLY_CONNECT  # bound at import, before any counter exists


def _make_db(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "knowledge.db"
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE IF NOT EXISTS t (x)")
    c.commit()
    c.close()
    return path


def _hooklib_ro(path):
    sys.path.insert(0, str(HOOKS_SRC))
    try:
        import _hooklib
    finally:
        sys.path.remove(str(HOOKS_SRC))
    return _hooklib.connect_regular_ro(path, timeout=0.2)


def _symlink_to(db, d):
    link = d / "link.db"
    if not link.exists():
        link.symlink_to(db)
    return link


def _dirlink_to(db, d):
    link = d / "dirlink"
    if not link.exists():
        link.symlink_to(db.parent, target_is_directory=True)
    return link


OPEN_SPELLINGS = [
    ("connect_regular_ro", lambda db, d: _hooklib_ro(db)),
    ("sqlite3.connect(abs)", lambda db, d: sqlite3.connect(str(db))),
    ("sqlite3.connect(bytes)", lambda db, d: sqlite3.connect(os.fsencode(str(db)))),
    ("sqlite3.connect(PathLike)", lambda db, d: sqlite3.connect(db)),
    ("sqlite3.Connection(abs)", lambda db, d: sqlite3.Connection(str(db))),
    ("early-bound from-import connect", lambda db, d: _EARLY_CONNECT(str(db))),
    ("_sqlite3.connect", lambda db, d: _sqlite3.connect(str(db))),
    ("symlinked file spelling", lambda db, d: sqlite3.connect(str(_symlink_to(db, d)))),
    ("symlinked directory spelling", lambda db, d: sqlite3.connect(str(_dirlink_to(db, d) / "knowledge.db"))),
    ("dotdot spelling", lambda db, d: sqlite3.connect(str(db.parent / ".." / db.parent.name / "knowledge.db"))),
    ("file:abs?mode=ro", lambda db, d: sqlite3.connect(f"file:{db}?mode=ro", uri=True)),
    ("file:///abs", lambda db, d: sqlite3.connect(f"file://{db}?mode=ro", uri=True)),
    ("file://localhost/abs", lambda db, d: sqlite3.connect(f"file://localhost{db}?mode=ro", uri=True)),
    ("percent-encoded URI", lambda db, d: sqlite3.connect(
        "file:" + quote(str(db), safe="/").replace(".", "%2E") + "?mode=ro", uri=True)),
]


@pytest.mark.parametrize("name,opener", OPEN_SPELLINGS, ids=[r[0] for r in OPEN_SPELLINGS])
def test_every_spelling_of_an_open_of_the_store_is_counted_exactly_once(tool, tmp_path, name, opener):
    """One open of the store, however it is spelled, reads exactly 1 -- including the routes the
    old wrapping missed (Connection(), an early-bound `connect`, `_sqlite3.connect`, a localhost
    or percent-encoded URI) and `connect_regular_ro`, which calls sqlite3.connect inside (so a
    counter that also counted the wrapper would read 2). Mutation: compare unresolved paths,
    drop `file:` URI normalisation, or drop the percent-decode -> the spellings that need them
    read 0."""
    db = _make_db(tmp_path / "store")
    with tool._counting_store_opens(db) as rec:
        opener(db, tmp_path).close()
    assert len(rec) == 1, (name, rec)


def test_a_relative_spelling_resolves_against_the_cwd_at_the_time_of_the_open(tool, tmp_path, monkeypatch):
    """Mutation: resolve relative paths against the cwd at counter start (or not at all) -> red."""
    db = _make_db(tmp_path / "store")
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    with tool._counting_store_opens(db) as rec:
        monkeypatch.chdir(db.parent)
        sqlite3.connect("knowledge.db").close()
        sqlite3.connect("file:knowledge.db?mode=ro", uri=True).close()
        monkeypatch.chdir(other)
        sqlite3.connect("knowledge.db").close()  # a DIFFERENT file now (created empty in `other`)
    assert len(rec) == 2, rec


def test_opens_of_other_files_and_memory_are_not_counted(tool, tmp_path):
    """Mutation: count every sqlite3.connect, or match on the file NAME alone -> red."""
    db = _make_db(tmp_path / "store")
    sibling = _make_db(tmp_path / "pack")  # same file name, different directory
    with tool._counting_store_opens(db) as rec:
        sqlite3.connect(":memory:").close()
        sqlite3.connect(str(tmp_path / "pack" / "other.db")).close()
        sqlite3.connect(str(sibling)).close()
        sqlite3.connect("file::memory:", uri=True).close()
    assert rec == []


def test_the_counter_counts_only_while_active_and_a_stopped_window_stays_stopped(tool, tmp_path):
    """Before the window: not counted. After it: not counted, and the stopped window's record does
    not grow (a flag that is never cleared leaks counts across windows). Two sequential windows
    each read their own 1 (a hook installed per window would double-count). Mutation: never clear
    the active flag; install a fresh audit hook per window -> red."""
    db = _make_db(tmp_path / "store")
    sqlite3.connect(str(db)).close()  # before any window
    with tool._counting_store_opens(db) as first:
        sqlite3.connect(str(db)).close()
    sqlite3.connect(str(db)).close()  # after it stopped
    with tool._counting_store_opens(db) as second:
        sqlite3.connect(str(db)).close()
    sqlite3.connect(str(db)).close()
    assert len(first) == 1 and len(second) == 1, (first, second)


def test_two_opens_read_two(tool, tmp_path):
    """Exact counts, not just zero / non-zero. Mutation: dedupe by path -> red."""
    db = _make_db(tmp_path / "store")
    with tool._counting_store_opens(db) as rec:
        sqlite3.connect(str(db)).close()
        sqlite3.connect(str(db)).close()
    assert len(rec) == 2


def test_an_open_from_a_helper_thread_while_active_is_counted(tool, tmp_path):
    """All threads are counted while the window is active (the contended arm runs two fire threads).
    Mutation: count only the thread that started the window -> red."""
    db = _make_db(tmp_path / "store")
    with tool._counting_store_opens(db) as rec:
        t = threading.Thread(target=lambda: sqlite3.connect(str(db)).close())
        t.start()
        t.join()
    assert len(rec) == 1


def test_a_thread_that_exempts_itself_is_not_counted_and_others_still_are(tool, tmp_path):
    """The in-process fake owner's handler threads exempt themselves (the real owner is another
    process). The exemption is per thread. Mutation: make the exemption process-wide, or ignore
    it -> red."""
    db = _make_db(tmp_path / "store")

    def exempt_open():
        tool.exempt_this_thread_from_open_counting()
        sqlite3.connect(str(db)).close()

    with tool._counting_store_opens(db) as rec:
        t = threading.Thread(target=exempt_open)
        t.start()
        t.join()
        assert rec == []
        sqlite3.connect(str(db)).close()  # this thread never exempted itself
    assert len(rec) == 1


def test_the_counter_never_breaks_an_open_it_cannot_read(tool, tmp_path):
    """An audit hook that raises aborts the open it observes, so the hook must swallow anything it
    cannot normalise: a bad argument fails in sqlite3 itself exactly as it would without the
    counter, and is not counted. The audit event is emitted BEFORE sqlite3 converts its argument,
    so a path-like whose first `__fspath__` call raises (the hook's) and whose later calls work
    (sqlite3's) is an open that succeeds without the counter and must still succeed with it.
    Mutation: let the normaliser raise inside the hook -> that open dies -> red."""
    db = _make_db(tmp_path / "store")

    class FlakyPath:
        calls = 0

        def __fspath__(self):
            FlakyPath.calls += 1
            if FlakyPath.calls == 1:
                raise RuntimeError("a6 control: unreadable on the first look")
            return str(db)

    with tool._counting_store_opens(db) as rec:
        with pytest.raises(TypeError):
            sqlite3.connect(12345)
        sqlite3.connect(FlakyPath()).close()   # opens fine; the hook could not read it, so it is not counted
        sqlite3.connect(str(db)).close()
    assert len(rec) == 1


# -- Layout / RUNTIME_FILES ------------------------------------------------------------------

NEW_RUNTIME_FILES = {
    "hooks/engram-lesson-tripwire-hook.py": "src/engram/hooks/claude/engram-lesson-tripwire-hook.py",
    "engramd_owner_flags.py": "src/engram/engramd_owner_flags.py",
    "engram_idf.py": "src/engram/engram_idf.py",
    "engram_core.py": "src/engram/engram_core.py",
}


def test_runtime_files_cover_the_tripwire_hook_and_its_flag_and_idf_dependencies(tool):
    """Mutation: drop any of these entries (e.g. engramd_owner_flags.py) -> red."""
    for built, src in NEW_RUNTIME_FILES.items():
        assert tool.RUNTIME_FILES.get(built) == src, built


def _scratch_build(tool, tmp_path):
    root = tmp_path / "build"
    for built, src in tool.RUNTIME_FILES.items():
        dest = root / built
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((REPO_ROOT / src).read_bytes())
    return tool.Layout(name="installed", root=root, stop_hook=root / "hooks" / "engram-stop-hook.py",
                       user_hook=root / "hooks" / "engram-user-identity-hook.py",
                       tripwire_hook=root / "hooks" / "engram-lesson-tripwire-hook.py")


@pytest.mark.parametrize("built", sorted(NEW_RUNTIME_FILES))
def test_layout_drift_in_a_new_runtime_file_is_detected(tool, tmp_path, built):
    """The installed-layout byte check covers the files the tripwire arm needs: a byte drift
    in, or the absence of, each one is reported by name; the pristine scratch build reports
    none. Mutation: leave the file out of RUNTIME_FILES -> its drift goes unreported -> red."""
    layout = _scratch_build(tool, tmp_path)
    assert tool.layout_matches_tree(layout) == []
    target = layout.root / built
    target.write_bytes(target.read_bytes() + b"\n# drift\n")
    assert built in tool.layout_matches_tree(layout)
    target.unlink()
    assert built in tool.layout_matches_tree(layout)


def test_layout_carries_the_tripwire_hook_and_roundtrips_it(tool, tmp_path):
    """Layout gains `tripwire_hook` (and old four-argument constructions keep working).
    Mutation: drop the field from to_json/from_json -> the child process would measure the
    source hook -> red."""
    lay = tool.Layout(name="installed", root=tmp_path, stop_hook=tmp_path / "hooks" / "s.py",
                      user_hook=tmp_path / "hooks" / "u.py", tripwire_hook=tmp_path / "hooks" / "t.py")
    assert tool.Layout.from_json(lay.to_json()).tripwire_hook == tmp_path / "hooks" / "t.py"
    assert tool.source_layout().tripwire_hook == tool.TRIPWIRE_HOOK_PATH
    old = tool.Layout(name="installed", root=tmp_path, stop_hook=tmp_path / "hooks" / "s.py",
                      user_hook=tmp_path / "hooks" / "u.py")
    assert old.tripwire_hook == tmp_path / "hooks" / "engram-lesson-tripwire-hook.py"


# -- a real (reduced-N) run of the arm -------------------------------------------------------

@pytest.mark.parametrize("exclusion", [False, True])
def test_the_tripwire_arm_reports_every_result_key_and_the_fire_reaches_every_site(tool, tripwire_runs, exclusion):
    """Reduced-N fake-owner run, exclusion off and on. Every key is present with its type;
    `recall_reached` and `stdout_match` hold; probe_dir_live == exclusion; every timing arm
    has both paths and both percentiles. Mutation: omit a key, report recall_reached without
    checking each fire, or compare stdout of the wrong paths -> red."""
    r = tripwire_runs[exclusion]
    for key in TRIPWIRE_RESULT_KEYS:
        assert key in r, key
    assert r["hook"] == "tripwire" and r["exclusion"] is exclusion
    assert r["probe_dir_live"] is exclusion
    assert r["recall_reached"] is True and r["stdout_match"] is True and r["answers_match"] is True
    assert isinstance(r["opens_flag_on"], int) and isinstance(r["opens_flag_off"], int)
    arms = {(x["arm"], x["path"]): x for x in r["rows"]}
    for arm in ("sequential", "contended", "cold-cache"):
        for path in ("baseline", "owner"):
            assert 0 < arms[(arm, path)]["p50_ms"] <= arms[(arm, path)]["p99_ms"]
    # the gate agrees with the count it reports (the exclusion-ON zero itself is the U8 cell below)
    assert (tool.u8_contract_violations(r) == []) == (not exclusion or r["opens_flag_on"] == 0)


def test_the_owner_path_opens_the_store_zero_times_with_exclusion_off_and_the_counter_can_see_an_open(tripwire_runs):
    """The gate quantity, exclusion OFF: owner path (flag ON) makes zero direct opens of the
    store copy. Baseline path (flag OFF): more than zero -- the positive control that the
    counter is not blind (a counter that always says 0 would make the zero vacuous), for
    both exclusion states. Mutation: a counter that always returns 0 -> the baseline side
    goes red."""
    assert tripwire_runs[False]["opens_flag_on"] == 0
    assert tripwire_runs[False]["opens_flag_off"] > 0 and tripwire_runs[True]["opens_flag_off"] > 0


def test_u8_exclusion_on_owner_path_opens_the_store_zero_times(tripwire_runs):
    """THE LIVE U8 GATE CELL (U8 #3710 is on dev): flag ON, exclusion ON, in-context probe live, a fire that reaches
    the in-turn recall -> ZERO direct opens of the store copy. Mutation: leave the exclusion
    resolver's focus read on its own connection (what dev did before U8) -> red."""
    r = tripwire_runs[True]
    assert r["probe_dir_live"] and r["recall_reached"]
    assert r["opens_flag_on"] == 0


def test_exclusion_on_creates_the_probe_dir_under_the_hooks_home_and_exclusion_off_does_not(tripwire_runs):
    """`probe_dir_live` is the fact observed during the owner-path fires, not an echo of the
    switch. Mutation: never create the probe dir when exclusion is ON -> red."""
    assert tripwire_runs[True]["probe_dir_live"] is True
    assert tripwire_runs[False]["probe_dir_live"] is False


def test_the_table_for_the_tripwire_arm_names_the_u8_quantities_and_keeps_the_caveats(tool, tripwire_runs):
    """Mutation: omit exclusion state, probe_dir_live, recall_reached, stdout_match or either
    open count, or drop the ratio columns / caveats block -> red."""
    text = tool.format_table(tripwire_runs[True])
    low = text.lower()
    for needle in ("exclusion", "probe_dir_live", "recall_reached", "stdout_match",
                   "opens_flag_on", "opens_flag_off", "p50 owner/baseline", "p99 owner/baseline", "notes:"):
        assert needle in low, needle
    assert platform.platform() in text and sqlite3.sqlite_version in text
    assert "exclusion: on" in low and "exclusion: off" in tool.format_table(tripwire_runs[False]).lower()


def test_home_and_streams_are_restored_after_a_tripwire_run(tool, tripwire_runs):
    """The arm points HOME at a scratch dir for the fire (route_probe_dir uses Path.home());
    the process's HOME and streams come back. Mutation: leave HOME or a stream proxy
    behind -> red."""
    assert os.environ.get("HOME") not in (None, "") and "a6-" not in os.environ["HOME"]
    assert sys.stdout.__class__.__name__ != "_ThreadStreams"
    assert sys.stdin.__class__.__name__ != "_ThreadStreams"
    assert sys.stderr.__class__.__name__ != "_ThreadStreams"


# -- the NEGATIVE cell: a hook that opens the store directly on the flag-on path ----------------

def test_a_hook_that_opens_the_store_directly_on_the_flag_on_path_is_caught(tool, tmp_path, monkeypatch, capsys):
    """The control for the whole gate. A variant of the hook whose load_tripwires opens
    knowledge.db itself even with the flag ON makes opens_flag_on > 0 and the run exit 3.
    (`db_path.exists()` is a stat, not an open, so the variant restores the direct
    connect_regular_ro call itself.) Mutation: an opens counter that always returns 0, or an
    exit code of 0 on violations -> red."""
    control = _flat_variant(tmp_path / "control", hooklib_transform=_focused_read_skipped)
    variant = _flat_variant(tmp_path / "variant", hook_transform=_open_directly_on_flag_on_path,
                            hooklib_transform=_focused_read_skipped)
    _fake_launch(tool, monkeypatch)
    state = {"hook": control}

    def in_process(layout, **kw):
        kw = dict(kw, n=20, warmup=2, tripwire_hook_path=state["hook"])
        kw.pop("layout_timeout", None)  # the child-process limit (#3755) is not a measure() argument
        return dict(tool.measure(**kw), layout=layout.name, layout_root=str(layout.root), parent_walk_calls=0)

    monkeypatch.setattr(tool, "measure_in_layout", in_process)
    argv = ["--hook", "tripwire", "--exclusion", "on", "--layout", "source"]
    # the control (exclusion ON, nothing opens the store on the flag-on path) is clean: exit 0
    r0 = tool.measure(n=20, nodes=300, warmup=2, hook="tripwire", exclusion=True, tripwire_hook_path=control)
    assert r0["opens_flag_on"] == 0 and tool.u8_contract_violations(r0) == []
    assert tool.main(argv) == 0
    # the variant opens the store itself on the flag-on path: counted, reported, exit 3
    state["hook"] = variant
    r = tool.measure(n=20, nodes=300, warmup=2, hook="tripwire", exclusion=True, tripwire_hook_path=variant)
    assert r["opens_flag_on"] > 0
    assert any("opens_flag_on" in v for v in tool.u8_contract_violations(r))
    capsys.readouterr()
    assert tool.main(argv) == 3
    assert "opens_flag_on" in capsys.readouterr().out


# -- a REAL owner process, and the INSTALLED layout ----------------------------------------------

@requires_linux_owner
def test_the_tripwire_arm_against_a_real_owner_process(tool, monkeypatch):
    """The same fire against a REAL engramd_owner process (a separate pid). The page-cache
    eviction is stubbed off Linux. Mutation: a launcher that does not start a real owner, or
    a hook path that never reaches it -> red."""
    if not hasattr(os, "posix_fadvise"):
        monkeypatch.setattr(tool, "_fadvise_cold", lambda db: None)
    r = tool.measure(n=20, nodes=300, warmup=3, hook="tripwire", exclusion=True)
    assert r["owner_pid"] != os.getpid()
    assert r["recall_reached"] and r["stdout_match"] and r["probe_dir_live"]
    assert (tool.u8_contract_violations(r) == []) == (r["opens_flag_on"] == 0)  # the zero itself: the U8 cell


@pytest.fixture(scope="module")
def installed_layout(tool, tmp_path_factory):
    return tool.build_installed_layout(tmp_path_factory.mktemp("a6-tw-build"))


@requires_linux_owner
def test_the_installed_layout_run_resolves_shim_and_flags_at_the_plugin_root(tool, installed_layout):
    """The installed cell (the U8 gate cell): a plugin built from this tree, the hook loaded
    from its flat hooks/, the owner from its root; zero parent walks (check_installed_run
    would have raised); and every runtime module the fire loaded from the build is listed in
    RUNTIME_FILES, so the byte-identity check really covers what ran. Mutation: drop a needed
    file from RUNTIME_FILES, or load the hook from the source tree -> red."""
    assert installed_layout.tripwire_hook == installed_layout.root / "hooks" / "engram-lesson-tripwire-hook.py"
    assert tool.layout_matches_tree(installed_layout) == []
    r = tool.measure_in_layout(installed_layout, hook="tripwire", exclusion=True, n=20, nodes=300, warmup=3)
    assert r["layout"] == "installed" and r["parent_walk_calls"] == 0
    assert r["recall_reached"] and r["stdout_match"] and r["probe_dir_live"] and r["exclusion_engaged"] is True
    assert (tool.u8_contract_violations(r) == []) == (r["opens_flag_on"] == 0)  # the zero itself: the U8 cell
    loaded = set(r["runtime_modules_loaded"])
    assert "hooks/engram-lesson-tripwire-hook.py" in loaded and "engramd_owner_flags.py" in loaded
    assert loaded <= set(tool.RUNTIME_FILES), sorted(loaded - set(tool.RUNTIME_FILES))


# -- the exclusion must really ENGAGE (a fail-open feature that is silently inert is a vacuous zero) ------

def test_the_parent_walk_probe_leaves_path_usable_for_class_level_calls(tool, monkeypatch):
    """The layout child counts parent walks by swapping `_hooklib.Path`. A swap that is a bare
    function breaks `Path.home()` inside _hooklib (route_probe_dir -> AttributeError, swallowed
    fail-open), which silently disabled the exclusion in every layout-child run and made the
    flag-ON zero-opens reading vacuous. Mutation: make `_hooklib.Path` a plain function -> red."""
    import pathlib

    monkeypatch.syspath_prepend(str(HOOKS_SRC))
    import _hooklib as hl
    monkeypatch.setattr(hl, "Path", hl.Path)  # restored at teardown
    tool._count_parent_walks(tool.source_layout())
    assert hl.Path.home() == pathlib.Path.home()
    assert hl.Path("/x") == pathlib.Path("/x")


def test_exclusion_on_reports_that_the_exclusion_engaged(tripwire_runs):
    """`exclusion_engaged`: with exclusion ON the exclusion resolver was built (non-None) and
    the probe's route ledger was written during the fire; False when exclusion is OFF.
    Mutation: report True unconditionally, or never wrap the resolver -> red."""
    assert tripwire_runs[True]["exclusion_engaged"] is True
    assert tripwire_runs[False]["exclusion_engaged"] is False


def test_an_exclusion_that_cannot_engage_is_an_error_not_a_clean_zero(tool, tmp_path, monkeypatch):
    """A hook process whose probe-dir lookup raises (the failure mode above) swallows it
    fail-open and drops nothing, so `opens_flag_on` would read 0 for the wrong reason. With
    exclusion ON the arm must refuse such a run loudly. Mutation: drop the engagement check
    -> measure() returns a clean-looking result -> red."""
    needle = "return Path.home().joinpath(*ROUTE_PROBE_SUBPATH)"

    def break_probe(src):
        assert src.count(needle) == 1
        return src.replace(needle, 'raise AttributeError("a6 control: probe dir lookup broken")')

    broken = _flat_variant(tmp_path, hooklib_transform=break_probe)
    _fake_launch(tool, monkeypatch)
    with pytest.raises(RuntimeError, match="exclusion"):
        tool.measure(n=20, nodes=300, warmup=2, hook="tripwire", exclusion=True, tripwire_hook_path=broken)




# -- survivors of the colleague mutation pass (each cell names the mutant it kills) -------------

def test_a_symlinked_spelling_of_the_WINDOW_target_still_counts_a_real_path_open(tool, tmp_path):
    """The window's target is resolved too, not only the opens. Mutation: store the target
    unresolved (str(db)) -> a window opened with a symlinked / non-canonical spelling of the store
    (macOS temp dirs are one: /var -> /private/var) reads 0 for a real-path open -> red."""
    db = _make_db(tmp_path / "store")
    via_link = _dirlink_to(db, tmp_path) / "knowledge.db"
    with tool._counting_store_opens(via_link) as rec:
        sqlite3.connect(str(db)).close()
    assert len(rec) == 1, rec


def test_files_that_only_start_with_the_store_path_are_not_counted(tool, tmp_path):
    """Equality, not prefix. Mutation: `resolved.startswith(target)` -> the store's own siblings
    (a backup, a numbered copy, a -wal named file) are counted as opens of the store -> red."""
    db = _make_db(tmp_path / "store")
    with tool._counting_store_opens(db) as rec:
        for suffix in ("2", ".bak", "-wal"):
            sqlite3.connect(str(db) + suffix).close()
    assert rec == []


def test_a_uri_fragment_spelling_is_counted(tool, tmp_path):
    """SQLite accepts `file:/path#fragment` and opens the store. Mutation: stop stripping the
    fragment -> the open is missed (reads 0) and the gate could pass with a direct open -> red."""
    db = _make_db(tmp_path / "store")
    with tool._counting_store_opens(db) as rec:
        sqlite3.connect(f"file:{db}?mode=ro#frag", uri=True).close()
        sqlite3.connect(f"file:{db}#frag", uri=True).close()
    assert len(rec) == 2, rec


@pytest.mark.parametrize("value", [1, "yes", [0]], ids=["int-1", "str", "list"])
@pytest.mark.parametrize("key", ["recall_reached", "stdout_match", "probe_dir_live"])
def test_a_gated_value_that_is_truthy_but_not_True_is_a_violation(tool, key, value):
    """The gate is `is not True`, as its docstring says. Mutation: truthiness (`not value`) for
    recall_reached / stdout_match / probe_dir_live -> a truthy non-bool passes -> red."""
    violations = tool.u8_contract_violations(_clear_result(**{key: value}))
    assert any(key in v for v in violations), (key, value, violations)


@pytest.mark.parametrize("bad_layout", ["installed", "source"])
def test_a_violation_in_only_one_layout_is_reported_and_exits_3(tool, monkeypatch, capsys, bad_layout):
    """`--layout both` gates EVERY layout, not the first. Mutation: iterate results[:1] -> a
    violation only in the second layout is dropped and the run exits 0 -> red."""
    layout = lambda name: types.SimpleNamespace(name=name)
    monkeypatch.setattr(tool, "build_installed_layout", lambda out: layout("installed"))
    monkeypatch.setattr(tool, "source_layout", lambda: layout("source"))
    monkeypatch.setattr(tool, "format_layouts", lambda rs: "THE-TABLE")
    monkeypatch.setattr(
        tool, "measure_in_layout",
        lambda lay, **kw: dict(_clear_result(opens_flag_on=3 if lay.name == bad_layout else 0),
                               answers_match=True, layout=lay.name))
    assert tool.main(["--hook", "tripwire", "--layout", "both"]) == 3
    out = capsys.readouterr().out
    assert f"[{bad_layout} layout]" in out and "opens_flag_on" in out


@pytest.mark.parametrize("value", [False, 0.0, True, 2, None, "0"],
                         ids=["False", "float-0.0", "True", "int-2", "None", "str-0"])
def test_opens_flag_on_gate_accepts_only_a_real_int_zero(tool, value):
    """With exclusion ON the gate is `type(x) is int and x == 0`. Mutation: `!= 0` -> False and
    0.0 compare equal to 0 and slip through as 'no direct opens' -> red."""
    violations = tool.u8_contract_violations(_clear_result(opens_flag_on=value))
    assert any("opens_flag_on" in v for v in violations), (value, violations)


def test_opens_flag_on_missing_is_a_violation(tool):
    violations = tool.u8_contract_violations(_clear_result(opens_flag_on=_MISSING))
    assert any("opens_flag_on" in v for v in violations), violations


def test_opens_flag_on_int_zero_is_clean(tool):
    assert tool.u8_contract_violations(_clear_result(opens_flag_on=0)) == []


# =====================================================================================================
# --hook surface (A6 arm for the surface hook's owner reads, #3821 contract item 4, part of #2652)
#
# Written BEFORE the arm (contract-first). The contract, from the #3821 spec:
#   * ONE full fire of the surface hook's main() per sample, flag OFF vs flag ON
#     (engramd.surface_reads_via_owner, named by ONE constant), the recall daemon (`query_daemon`)
#     stubbed IDENTICALLY on both paths, installed layout, N >= 300;
#   * a hook that lacks the flag is a clear named error (ArmBlind -> exit 4), never a crash or a number;
#   * BOTH a synthetic store and a REAL-STORE COPY are reported; the real-store cell on the INSTALLED layout is
#     THE GATE CELL and the verdict line says so; a synthetic-only result is never a pass (exit 3);
#   * the real store is only ever COPIED (never opened for write, never measured in place);
#   * contended and cold arms are reported as owed measurements; any arm the owner loses is a finding;
#   * exit codes and result shape as the tripwire arm: 3 = contract violation, 4 = blind arm, 2 = bad input.
# Every test docstring names the mutation it must turn red under.
# =====================================================================================================

import hashlib  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import textwrap  # noqa: E402

MINI_SURFACE_SRC = textwrap.dedent('''
    import json, os, sqlite3, sys
    ENGRAM_HOME = os.environ["ENGRAM_HOME"]
    KNOWLEDGE_DB_PATH = os.path.join(ENGRAM_HOME, "knowledge.db")
    CONFIG_PATH = os.path.join(ENGRAM_HOME, "config.json")
    COUNTER_PATH = os.path.join(ENGRAM_HOME, "prompt-counter.json")
    def _owner_reads_enabled():
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f).get("engramd", {}).get("surface_reads_via_owner") is True
        except Exception:
            return False
    def query_daemon(prompt, top_k=10, embed_query=None):
        raise AssertionError("the real recall daemon was reached: the arm did not stub it")
    def _advance_person_moment_cursor():
        c = sqlite3.connect(KNOWLEDGE_DB_PATH); c.execute("SELECT 1"); c.close()
    def _read_nodes():
        if _owner_reads_enabled():
            return 1
        c = sqlite3.connect(KNOWLEDGE_DB_PATH)
        try:
            return c.execute("SELECT count(*) FROM nodes").fetchone()[0]
        finally:
            c.close()
    def main():
        hook_input = json.load(sys.stdin)
        n = 0
        try:
            with open(COUNTER_PATH) as f:
                n = json.load(f)["n"]
        except Exception:
            pass
        with open(COUNTER_PATH, "w") as f:
            json.dump({"n": n + 1}, f)
        _read_nodes()
        _advance_person_moment_cursor()
        res = query_daemon(hook_input["prompt"])
        text = "[ENGRAM Recall: %d nodes match your query]\\n" % res["match_count"]
        text += "\\n".join(c["recall_summary"] for c in res["top_claims"]) + "\\nstate n=%d" % n
        print(json.dumps({"hookSpecificOutput": {"additionalContext": text}}))
        sys.exit(0)
''')


def _mini_surface(tmp_path, transform=None, name="engram-surface-hook.py"):
    src = MINI_SURFACE_SRC
    if transform is not None:
        out = transform(src)
        assert out != src
        src = out
    path = tmp_path / name
    path.write_text(src)
    return path


def _no_flag(src):
    """A hook as it is on a base WITHOUT #3821's flag: no _owner_reads_enabled, no flag key."""
    return (src.replace("def _owner_reads_enabled():", "def _something_else():")
            .replace("surface_reads_via_owner", "other_thing"))


def _owner_path_still_reads(src):
    """The plausible-wrong hook: the flag-ON path still opens the store from `_read_nodes` (an un-migrated read)."""
    needle = "    if _owner_reads_enabled():\n        return 1\n"
    assert src.count(needle) == 1
    return src.replace(needle, "")


def _surface_measure(tool, hook, **over):
    kw = dict(n=20, nodes=300, warmup=3, hook="surface", surface_hook_path=hook)
    kw.update(over)
    return tool.measure(**kw)


@pytest.fixture()
def surface_run(tool, tmp_path, monkeypatch):
    _fake_launch(tool, monkeypatch)
    return _surface_measure(tool, _mini_surface(tmp_path))


# -- the selector, the constants, the layout ----------------------------------------------------------

def test_surface_is_a_hook_the_flag_has_one_name_and_the_default_stays_user_identity(tool):
    """Mutation: leave `surface` out of HOOKS, rename the flag constant, or change the default hook -> red."""
    assert "surface" in tool.HOOKS and tool.HOOKS[0] == "user-identity"
    assert tool.SURFACE_FLAG_KEY == "surface_reads_via_owner"
    assert tool.SURFACE_EXPECTED_SQLITE == "3.53.0"
    assert tool.SURFACE_HOOK_PATH.name == "engram-surface-hook.py"


def test_layout_carries_the_surface_hook_and_the_old_payload_still_loads(tool, tmp_path):
    """Mutation: drop the field from to_json/from_json (the child would measure the SOURCE hook), or require it of
    an old payload -> red."""
    lay = tool.Layout(name="installed", root=tmp_path, stop_hook=tmp_path / "hooks" / "s.py",
                      user_hook=tmp_path / "hooks" / "u.py", surface_hook=tmp_path / "hooks" / "x.py")
    assert tool.Layout.from_json(lay.to_json()).surface_hook == tmp_path / "hooks" / "x.py"
    assert tool.source_layout().surface_hook == tool.SURFACE_HOOK_PATH
    old = tool.Layout(name="installed", root=tmp_path, stop_hook=tmp_path / "hooks" / "s.py",
                      user_hook=tmp_path / "hooks" / "u.py")
    assert old.surface_hook == tmp_path / "hooks" / "engram-surface-hook.py"
    payload = json.loads(old.to_json())
    payload.pop("surface_hook")
    assert tool.Layout.from_json(json.dumps(payload)).surface_hook == tmp_path / "hooks" / "engram-surface-hook.py"


def test_the_installed_layout_covers_the_surface_hook_and_detects_drift_in_it(tool, tmp_path):
    """The byte check covers the surface hook: a drift in, or the absence of, the built copy is reported by name.
    Mutation: leave the hook out of RUNTIME_FILES -> its drift goes unreported -> red."""
    assert tool.RUNTIME_FILES.get("hooks/engram-surface-hook.py") == "src/engram/hooks/claude/engram-surface-hook.py"
    layout = _scratch_build(tool, tmp_path)
    assert tool.layout_matches_tree(layout) == []
    target = layout.root / "hooks" / "engram-surface-hook.py"
    target.write_bytes(target.read_bytes() + b"\n# drift\n")
    assert "hooks/engram-surface-hook.py" in tool.layout_matches_tree(layout)
    target.unlink()
    assert "hooks/engram-surface-hook.py" in tool.layout_matches_tree(layout)


def test_the_installed_layout_surface_fire_loads_only_files_listed_in_runtime_files(tool, tmp_path):
    """The closure cell: a flat plugin root holding ONLY the RUNTIME_FILES copies can import and fire the surface
    hook (a missing runtime file would raise ImportError in the child). Mutation: remove any file the fire needs
    from RUNTIME_FILES -> the fire fails in the scratch build -> red."""
    layout = _scratch_build(tool, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    driver = textwrap.dedent('''
        import io, json, os, sys, sqlite3, importlib.util
        root = sys.argv[1]
        sys.path[:0] = [root + "/hooks"]; sys.path.append(root)
        import engram_core
        c = sqlite3.connect(os.environ["ENGRAM_HOME"] + "/knowledge.db"); c.execute("PRAGMA journal_mode=WAL")
        c.row_factory = sqlite3.Row; engram_core._run_db_one_time_setup(c); c.commit(); c.close()
        sp = importlib.util.spec_from_file_location("surf", root + "/hooks/engram-surface-hook.py")
        mod = importlib.util.module_from_spec(sp); sp.loader.exec_module(mod)
        mod.query_daemon = lambda prompt, top_k=10, embed_query=None: {"match_count": 1, "top_claims": [{"id": "ob_x", "recall_summary": "s"}]}
        sys.stdin = io.StringIO(json.dumps({"session_id": "s1", "prompt": "what is the plan for the owner reads today", "transcript_path": ""}))
        out = io.StringIO(); real = sys.stdout; sys.stdout = out
        try:
            mod.main()
        except SystemExit:
            pass
        sys.stdout = real
        assert "[ENGRAM Recall:" in out.getvalue(), out.getvalue()
        print("FIRED")
    ''')
    env = {"PATH": os.environ.get("PATH", ""), "ENGRAM_HOME": str(home), "HOME": str(home),
           "CLAUDE_PLUGIN_ROOT": str(layout.root)}
    proc = subprocess.run([sys.executable, "-c", driver, str(layout.root)], env=env, capture_output=True,
                          text=True, timeout=300)
    assert proc.returncode == 0 and "FIRED" in proc.stdout, proc.stderr[-1500:]


# -- the hook lacks the flag -----------------------------------------------------------------------------

def test_a_hook_without_the_flag_is_a_named_arm_blind_not_a_crash_or_a_number(tool, tmp_path, monkeypatch):
    """The arm must say WHY: it names the flag key and does not return a result. Mutation: measure the two
    identical (flag-less) paths and report a ratio, or raise a bare AttributeError -> red."""
    _fake_launch(tool, monkeypatch)
    hook = _mini_surface(tmp_path, _no_flag)
    with pytest.raises(tool.ArmBlind) as exc:
        _surface_measure(tool, hook)
    msg = str(exc.value)
    assert tool.SURFACE_FLAG_KEY in msg and "no number" in msg.lower()


def test_main_exits_4_with_a_clear_stderr_line_when_the_hook_lacks_the_flag(tool, monkeypatch, capsys):
    """Exit 4 is the blind-arm code (a layout child's ArmBlind traceback comes back typed). Mutation: let it
    surface as RuntimeError (exit traceback) or exit 1 -> red."""
    layout = lambda name: types.SimpleNamespace(name=name)
    monkeypatch.setattr(tool, "build_installed_layout", lambda out: layout("installed"))
    monkeypatch.setattr(tool, "source_layout", lambda: layout("source"))

    def blind(lay, **kw):
        raise tool.ArmBlind(f"the surface hook has no engramd.{tool.SURFACE_FLAG_KEY} flag: no number produced")

    monkeypatch.setattr(tool, "measure_in_layout", blind)
    assert tool.main(["--hook", "surface", "--no-real-store", "--layout", "source"]) == 4
    err = capsys.readouterr().err
    assert "BLIND ARM" in err and tool.SURFACE_FLAG_KEY in err


# -- one full fire, flag OFF vs ON, the daemon stubbed identically ------------------------------------------

def test_the_surface_arm_fires_main_on_both_paths_and_reports_the_tripwire_result_shape(tool, surface_run):
    """Same result shape downstream readers parse: rows (arm x path, six rows), answers_match, answer_size,
    env, owner_pid ... plus the surface keys. Mutation: drop an arm, a key, or return the rows unlabelled -> red."""
    r = surface_run
    assert r["hook"] == "surface"
    assert {(x["arm"], x["path"]) for x in r["rows"]} == {
        (a, p) for a in ("sequential", "contended", "cold-cache") for p in ("baseline", "owner")}
    assert all(x["n"] >= 1 and x["p50_ms"] > 0 for x in r["rows"])
    for key in ("answers_match", "answer_size", "owner_pid", "owner_cmdline", "node_count", "journal_mode",
                "db_bytes", "env", "stdout_match", "recall_reached", "opens_flag_on", "opens_flag_off",
                "open_violations"):
        assert key in r, key
    assert r["env"]["sqlite"] == sqlite3.sqlite_version


def test_the_recall_daemon_is_stubbed_identically_on_both_paths(tool, surface_run):
    """The mini hook's real `query_daemon` raises; the arm only gets a number if BOTH paths run with the stub, and
    the stub's text appears in both fires' (equal) stdout. Mutation: stub only one path, or stub neither -> the
    unstubbed fire raises AssertionError -> red."""
    assert surface_run["recall_reached"] is True
    assert surface_run["stdout_match"] is True
    assert surface_run["answers_match"] is True
    assert surface_run["answer_size"] > 0


def test_per_fire_state_is_reset_so_the_paths_stay_comparable(tool, surface_run):
    """The mini hook prints a per-fire counter it persists in ENGRAM_HOME (as the real hook's prompt counter does).
    With N fires the paths only keep printing equal bytes if state is cleared between fires. Mutation: skip the
    reset -> the sanity fires of the two paths differ -> stdout_match False -> red."""
    assert surface_run["stdout_match"] is True


def test_the_flag_off_path_opens_the_store_and_the_flag_on_path_only_the_named_remainder(tool, surface_run):
    """OFF counts both opens (the read and the person-moment cursor); ON keeps exactly the cursor write, which the
    allow-list attributes by frame (`_advance_person_moment_cursor`, the #3821 row-8 remainder), so it is NOT a
    violation. Mutation: an opens counter stuck at 0, or no allow-list (every ON open a violation) -> red."""
    assert surface_run["opens_flag_off"] == 2
    assert surface_run["opens_flag_on"] == 1
    assert surface_run["open_violations"] == []


def test_a_flag_on_path_that_still_reads_directly_is_a_violation_naming_the_reader(tool, tmp_path, monkeypatch):
    """The control: an un-migrated read on the flag-ON path (`_read_nodes` ignores the flag) opens the store and is
    reported with its frame; surface_contract_violations carries it. Mutation: allow every ON open -> red."""
    _fake_launch(tool, monkeypatch)
    r = _surface_measure(tool, _mini_surface(tmp_path, _owner_path_still_reads))
    assert r["opens_flag_on"] == 2
    assert len(r["open_violations"]) == 1 and "_read_nodes" in r["open_violations"][0]
    assert any("_read_nodes" in v for v in tool.surface_contract_violations(r))


def test_the_arm_never_touches_the_process_home_streams_or_environment(tool, tmp_path, monkeypatch, surface_run):
    """HOME / streams / ENGRAM_HOME come back after a surface run. Mutation: skip a cleanup -> red."""
    assert "a6-" not in os.environ.get("HOME", "")
    assert sys.stdout.__class__.__name__ != "_ThreadStreams"
    assert sys.stdin.__class__.__name__ != "_ThreadStreams"
    assert "a6-" not in os.environ.get("ENGRAM_HOME", "")


# -- the pure contract gate -------------------------------------------------------------------------------

def _surface_clear(**over):
    r = {"hook": "surface", "recall_reached": True, "stdout_match": True, "open_violations": [],
         "opens_flag_on": 1, "opens_flag_off": 6}
    r.update(over)
    return {k: v for k, v in r.items() if v is not _MISSING}


SURFACE_VIOLATIONS = [
    ("all clear", {}, []),
    ("opens reported, never gated by count", {"opens_flag_on": 9}, []),
    ("recall not reached", {"recall_reached": False}, ["recall_reached"]),
    ("recall_reached missing", {"recall_reached": _MISSING}, ["recall_reached"]),
    ("recall_reached truthy but not True", {"recall_reached": 1}, ["recall_reached"]),
    ("stdout differs", {"stdout_match": False}, ["stdout_match"]),
    ("stdout_match missing", {"stdout_match": _MISSING}, ["stdout_match"]),
    ("stdout_match None", {"stdout_match": None}, ["stdout_match"]),
    ("an unattributed ON open", {"open_violations": ["unattributed open of the store: frame x:_read_nodes:3"]},
     ["_read_nodes"]),
    ("open_violations missing fails closed", {"open_violations": _MISSING}, ["open_violations"]),
    ("open_violations not a list fails closed", {"open_violations": None}, ["open_violations"]),
    ("two at once", {"stdout_match": False, "recall_reached": False}, ["stdout_match", "recall_reached"]),
]


@pytest.mark.parametrize("name,over,expect", SURFACE_VIOLATIONS, ids=[r[0] for r in SURFACE_VIOLATIONS])
def test_surface_contract_violations_table(tool, name, over, expect):
    """One line per violation, by name. Mutation: ignore a key, treat truthy as True, or fail OPEN on a
    missing open_violations -> a row goes red."""
    got = tool.surface_contract_violations(_surface_clear(**over))
    assert len(got) == len(expect), got
    for key in expect:
        assert any(key in g for g in got), (key, got)


def test_surface_contract_violations_is_pure(tool, monkeypatch):
    """Mutation: do I/O or mutate the input -> red."""
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("I/O in a pure function"))
    r = _surface_clear(stdout_match=False)
    before = dict(r)
    assert tool.surface_contract_violations(r)
    assert r == before


def _open(*frames, arg="/h/knowledge.db"):
    return {"arg": arg, "thread": "MainThread", "stack": list(frames)}


def test_the_allow_list_is_the_person_moment_cursor_and_nothing_else(tool):
    """An ON open with an `_advance_person_moment_cursor` frame in the hook's own file is the named remainder; any
    other open is a violation naming the innermost frame and the hook's own frames. Mutation: allow by any file,
    allow everything, or allow nothing -> red."""
    hook = "/plugin/hooks/engram-surface-hook.py"
    cursor = _open(f"{hook}:main:3700", f"{hook}:_advance_person_moment_cursor:1137", "/py/sqlite3.py:connect:1")
    reader = _open(f"{hook}:main:3700", f"{hook}:_check_embedding_restore_flag:318",
                   "/plugin/_hooklib.py:connect_regular_ro:9")
    got = tool.surface_attribute_opens([cursor, reader], hook)
    assert got["remainder"] == [cursor]
    assert len(got["violations"]) == 1
    assert "_check_embedding_restore_flag" in got["violations"][0] and "connect_regular_ro" in got["violations"][0]
    assert tool.surface_attribute_opens([_open()], hook)["violations"]          # no stack: a violation, not allowed
    spoof = _open("/x/other.py:_advance_person_moment_cursor:1")                 # same name, not the hook's own file
    assert tool.surface_attribute_opens([spoof], hook)["violations"]


# -- the gate cell and the verdict ---------------------------------------------------------------------------

def _rows(ratio, contended=0.9, cold=0.9):
    """Six rows whose sequential owner/baseline p50 is `ratio` (and contended/cold as given)."""
    out = []
    for arm, rt in (("sequential", ratio), ("contended", contended), ("cold-cache", cold)):
        base = {"arm": arm, "n": 300, "p50_ms": 10.0, "p99_ms": 12.0, "mean_ms": 10.0, "min_ms": 9.0}
        out.append(dict(base, path="baseline"))
        out.append(dict(base, path="owner", p50_ms=10.0 * rt, p99_ms=12.0 * rt, mean_ms=10.0 * rt))
    return out


def _cell(layout, cell, ratio, sqlite="3.53.0", **kw):
    r = dict(_surface_clear(), layout=layout, layout_root="/r", parent_walk_calls=0, store_cell=cell,
             rows=_rows(ratio, **kw), answers_match=True,
             env={"platform": "x", "cpus": 1, "python": "3.12", "sqlite": sqlite})
    return r


def test_the_gate_cell_is_the_real_store_copy_on_the_installed_layout(tool):
    """Synthetic 0.94x passes, the real copy misses at 1.04x (the tripwire's #3740 numbers): the verdict is the
    REAL cell's, FAIL, and it names itself as the gate cell. Mutation: compute the verdict from the synthetic
    cell, from the source layout, or from the worst-of-all-cells -> red."""
    results = [_cell("installed", "synthetic", 0.94), _cell("installed", "real-store", 1.04),
               _cell("source", "synthetic", 0.50), _cell("source", "real-store", 0.50)]
    gate = tool.surface_gate(results)
    assert gate["gate_cell_present"] is True
    assert gate["passes"] is False
    assert gate["ratio"] == pytest.approx(1.04)
    v = gate["verdict"]
    assert "GATE CELL" in v and "real-store" in v and "installed" in v
    assert "1.04x" in v and "FAIL" in v and "PASS" not in v.replace("NOT A PASS", "")


def test_the_real_store_cell_passing_while_the_synthetic_misses_is_a_pass(tool):
    """The other direction (kills 'worst of both' and 'synthetic decides'). Mutation: gate on the synthetic -> red."""
    gate = tool.surface_gate([_cell("installed", "synthetic", 1.20), _cell("installed", "real-store", 0.97)])
    assert gate["passes"] is True and gate["ratio"] == pytest.approx(0.97)
    assert "PASS" in gate["verdict"] and "GATE CELL" in gate["verdict"]
    assert "synthetic" in gate["verdict"] and "1.20x" in gate["verdict"]   # reported, labelled not-the-gate


@pytest.mark.parametrize("results", [
    [_cell("installed", "synthetic", 0.80)],                                          # synthetic-only
    [_cell("source", "synthetic", 0.80), _cell("source", "real-store", 0.80)],        # no installed layout
    [],
], ids=["synthetic-only", "source-only", "empty"])
def test_without_the_real_store_cell_on_the_installed_layout_the_result_is_not_a_pass(tool, results):
    """Mutation: pass on the synthetic number alone (or on a source-layout real cell) -> red."""
    gate = tool.surface_gate(results)
    assert gate["gate_cell_present"] is False and gate["passes"] is False
    assert "NO GATE CELL" in gate["verdict"] and "NOT A PASS" in gate["verdict"]
    assert tool.surface_gate_violations(results), "a missing gate cell must be a contract violation (exit 3)"


def test_a_present_gate_cell_is_no_gate_violation(tool):
    """Mutation: always report the missing-cell violation -> red."""
    assert tool.surface_gate_violations([_cell("installed", "real-store", 1.5)]) == []


def test_the_sqlite_build_is_named_and_a_non_expected_build_is_flagged(tool):
    """3.53.0 is expected; another build is named in the verdict as the build axis, never silently passed.
    Mutation: drop the version from the verdict, or flag the expected build -> red."""
    ok = tool.surface_gate([_cell("installed", "real-store", 0.9, sqlite="3.53.0")])["verdict"]
    other = tool.surface_gate([_cell("installed", "real-store", 0.9, sqlite="3.45.1")])["verdict"]
    assert "3.53.0" in ok and "expected" not in ok.lower()
    assert "3.45.1" in other and "expected 3.53.0" in other


def test_an_arm_the_owner_loses_is_a_finding_and_contended_and_cold_are_owed(tool):
    """Findings name the cell, arm and ratio; the contended and cold arms are always listed as owed measurements.
    Mutation: drop the findings, or list owed only when they lose -> red."""
    gate = tool.surface_gate([_cell("installed", "real-store", 0.95, contended=1.3, cold=0.8)])
    joined = "\n".join(gate["findings"])
    assert "contended" in joined and "1.30x" in joined and "real-store" in joined
    assert "cold-cache" not in joined                                  # the owner wins cold here: not a finding
    owed = "\n".join(gate["owed"])
    assert "contended" in owed and "cold" in owed and "owed" in owed.lower()
    clean = tool.surface_gate([_cell("installed", "real-store", 0.95)])
    assert clean["findings"] == [] and "contended" in "\n".join(clean["owed"])


# -- the CLI: two cells, the copy, exit codes --------------------------------------------------------------------

def _patch_cli(tool, monkeypatch, seen, ratios=None, **over):
    layout = lambda name: types.SimpleNamespace(name=name)
    monkeypatch.setattr(tool, "build_installed_layout", lambda out: layout("installed"))
    monkeypatch.setattr(tool, "source_layout", lambda: layout("source"))
    ratios = ratios or {"synthetic": 0.94, "real-store": 0.97}
    monkeypatch.setattr(tool, "format_layouts", lambda rs: "TABLE")   # (the table has its own cells)

    def fake(lay, **kw):
        cell = "real-store" if kw.get("store") is not None else "synthetic"
        seen.append((lay.name, cell, kw.get("store"), kw["hook"]))
        r = _cell(lay.name, cell, ratios[cell])
        r.pop("store_cell")                      # the CLI tags it; a child process result does not carry it
        r.update(over)
        return r

    monkeypatch.setattr(tool, "measure_in_layout", fake)
    return fake


def _live_home(tmp_path, monkeypatch):
    home = tmp_path / "live"
    home.mkdir()
    db = home / "knowledge.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE nodes (id TEXT)")
    c.executemany("INSERT INTO nodes VALUES (?)", [(f"n{i}",) for i in range(7)])
    c.commit()
    c.close()
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    return db


def test_main_measures_a_synthetic_cell_and_a_real_store_copy_per_layout_and_never_the_live_file(
        tool, tmp_path, monkeypatch, capsys):
    """Four measure calls (installed/source x synthetic/real-store). The real cell's `store` is a COPY (a different
    path, a valid db with the live node count, existing during the call and removed after); the live file is
    byte-identical after. Mutation: pass the live path itself, skip the synthetic cell, skip the real cell, or
    leave the copy behind -> red."""
    live = _live_home(tmp_path, monkeypatch)
    before = hashlib.sha256(live.read_bytes()).hexdigest()
    seen, copies = [], []
    real_fake = _patch_cli(tool, monkeypatch, seen)

    def spying(lay, **kw):
        if kw.get("store") is not None:
            p = Path(kw["store"])
            c = sqlite3.connect(str(p))
            copies.append((p, c.execute("SELECT count(*) FROM nodes").fetchone()[0]))
            c.close()
        return real_fake(lay, **kw)

    monkeypatch.setattr(tool, "measure_in_layout", spying)
    assert tool.main(["--hook", "surface"]) == 0
    assert sorted((a, b) for a, b, _s, _h in seen) == [
        ("installed", "real-store"), ("installed", "synthetic"), ("source", "real-store"), ("source", "synthetic")]
    assert {h for *_x, h in seen} == {"surface"}
    assert len(copies) == 2
    for p, count in copies:
        assert p.resolve() != live.resolve() and count == 7
        assert not p.exists(), "the real-store copy must be removed after the run"
    assert hashlib.sha256(live.read_bytes()).hexdigest() == before
    out = capsys.readouterr().out
    assert "GATE CELL" in out and "real-store" in out


def test_main_prints_the_verdict_after_the_table_and_a_clear_gate_exits_0(tool, tmp_path, monkeypatch, capsys):
    """Mutation: print the verdict before the table, or exit non-zero on a clear run -> red."""
    _live_home(tmp_path, monkeypatch)
    _patch_cli(tool, monkeypatch, [])
    monkeypatch.setattr(tool, "format_layouts", lambda rs: "THE-TABLE")
    assert tool.main(["--hook", "surface"]) == 0
    out = capsys.readouterr().out
    assert out.index("THE-TABLE") < out.index("GATE CELL") and "PASS" in out


def test_main_a_gate_miss_or_a_lost_arm_is_a_stated_finding_not_an_exit_code(tool, tmp_path, monkeypatch, capsys):
    """Exit codes are the tripwire arm's: 0 ok (a ratio above 1.0 is a finding on stdout), 3 only for a contract
    violation. Mutation: exit 1/3 on a ratio miss, or print no finding -> red."""
    _live_home(tmp_path, monkeypatch)
    _patch_cli(tool, monkeypatch, [], ratios={"synthetic": 0.94, "real-store": 1.04})
    assert tool.main(["--hook", "surface"]) == 0
    out = capsys.readouterr().out
    assert "FAIL" in out and "1.04x" in out and "FINDING" in out


def test_main_a_synthetic_only_run_is_exit_3_and_says_not_a_pass(tool, tmp_path, monkeypatch, capsys):
    """`--no-real-store` still measures (the numbers are reportable) but is never a pass. Mutation: exit 0 on a
    synthetic-only run, or omit 'NOT A PASS' -> red."""
    _live_home(tmp_path, monkeypatch)
    seen = []
    _patch_cli(tool, monkeypatch, seen)
    assert tool.main(["--hook", "surface", "--no-real-store"]) == 3
    assert {cell for _l, cell, _s, _h in seen} == {"synthetic"}
    out = capsys.readouterr().out
    assert "NO GATE CELL" in out and "NOT A PASS" in out


def test_main_a_source_layout_only_run_has_no_gate_cell_and_exits_3(tool, tmp_path, monkeypatch, capsys):
    """Mutation: treat the source-layout real-store cell as the gate -> exit 0 -> red."""
    _live_home(tmp_path, monkeypatch)
    _patch_cli(tool, monkeypatch, [])
    assert tool.main(["--hook", "surface", "--layout", "source"]) == 3
    assert "NOT A PASS" in capsys.readouterr().out


def test_main_exits_3_and_names_the_layout_and_cell_on_a_per_cell_violation(tool, tmp_path, monkeypatch, capsys):
    """A violation in only the real-store cell is reported with its layout and cell. Mutation: gate only the
    first/synthetic cell -> exit 0 -> red."""
    _live_home(tmp_path, monkeypatch)
    fake = _patch_cli(tool, monkeypatch, [])

    def bad_real(lay, **kw):
        r = fake(lay, **kw)
        if kw.get("store") is not None:
            r["stdout_match"] = False
        return r

    monkeypatch.setattr(tool, "measure_in_layout", bad_real)
    assert tool.main(["--hook", "surface"]) == 3
    out = capsys.readouterr().out
    assert "[installed layout, real-store cell]" in out and "stdout_match" in out


def test_main_real_store_source_can_be_named_and_a_missing_one_is_exit_2(tool, tmp_path, monkeypatch, capsys):
    """`--real-store PATH` is the copy source; an absent one (explicit or the default) is bad input, exit 2,
    before anything is launched. Mutation: silently fall back to synthetic-only -> red."""
    monkeypatch.setattr(tool, "measure_in_layout", lambda *a, **k: pytest.fail("measured"))
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "nowhere"))
    assert tool.main(["--hook", "surface"]) == 2
    assert tool.main(["--hook", "surface", "--real-store", str(tmp_path / "absent.db")]) == 2
    assert "real-store" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["--hook", "surface", "--exclusion", "on"],
    ["--hook", "surface", "--store", "x.db"],
    ["--hook", "tripwire", "--real-store", "x.db"],
    ["--real-store", "x.db"],
    ["--hook", "stop", "--no-real-store"],
    ["--hook", "surface", "--real-store", "x.db", "--no-real-store"],
])
def test_surface_only_switches_are_valid_only_with_the_surface_hook(tool, monkeypatch, argv):
    """Argparse errors (exit 2) before anything is launched. Mutation: accept the surface switches for other
    hooks, or accept --store/--exclusion for surface -> red."""
    monkeypatch.setattr(tool, "launch_owner", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(SystemExit) as exc:
        tool.main(argv)
    assert exc.value.code == 2


def test_surface_keeps_the_n_floor_before_anything_is_launched(tool, tmp_path, monkeypatch):
    """N below 300 is exit 2, with no copy made and no owner launched. Mutation: skip the floor on the surface
    branch -> red."""
    _live_home(tmp_path, monkeypatch)
    monkeypatch.setattr(tool, "launch_owner", lambda *a, **k: pytest.fail("launched"))
    monkeypatch.setattr(tool, "measure_in_layout", lambda *a, **k: pytest.fail("measured"))
    assert tool.main(["--hook", "surface", "--n", "50"]) == 2


def test_the_default_n_is_the_floor_for_the_surface_arm(tool, tmp_path, monkeypatch):
    """N >= 300 by default: the n handed to every cell is at least N_FLOOR. Mutation: a smaller default -> red."""
    _live_home(tmp_path, monkeypatch)
    ns = []
    fake = _patch_cli(tool, monkeypatch, [])
    monkeypatch.setattr(tool, "measure_in_layout", lambda lay, **kw: (ns.append(kw["n"]), fake(lay, **kw))[1])
    tool.main(["--hook", "surface"])
    assert ns and min(ns) >= 300


def test_format_layouts_labels_the_cell_and_leaves_cell_less_results_unchanged(tool, monkeypatch):
    """A cell-tagged result's heading names the cell (the real-store one says it is the gate cell on installed);
    an untagged result's output has no cell text. Mutation: drop the label, or add it to untagged -> red."""
    def res(layout, cell):
        r = {"layout": layout, "layout_root": "/r", "parent_walk_calls": 0}
        if cell:
            r["store_cell"] = cell
        return r

    monkeypatch.setattr(tool, "format_table", lambda r: "TBL")
    plain = tool.format_layouts([res("installed", None)])
    both = tool.format_layouts([res("installed", "synthetic"), res("installed", "real-store")])
    assert "real-store" not in plain and "synthetic" not in plain
    assert "real-store" in both and "synthetic" in both and both.index("real-store") > both.index("synthetic")
    assert "gate" in both.split("real-store", 1)[1].lower()


def test_surface_table_names_the_hook_and_the_stub_caveat_and_the_cell(tool, surface_run):
    """Mutation: print the surface arm with the user-identity wording -> red."""
    text = tool.format_table(surface_run)
    assert "Surface hook" in text and "query_daemon" in text and "owed" in text.lower()
    assert f"SQLite {sqlite3.sqlite_version}" in text
