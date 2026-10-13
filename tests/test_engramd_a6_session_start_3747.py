"""Contract tests for the A6 `--hook session-start` arm (#3747, part of #2652): the merge gate of
U9 (#3735 / PR #3746), written BEFORE the arm (contract-first, project CLAUDE.md).

The design-seat ruling (Borges, #3747 comment 6135385845) is the contract. Shape (a), an
ALLOW-LIST: on the flag-ON path EVERY recorded open of knowledge.db must be attributed to the
calibration path; any other open is a violation naming the opener's frames. The calibration path
is identified by a stack frame whose file BASENAME equals exactly `engram_client.py` (not
`engramd_client.py` -- the owner shim, which must never open the store -- and not a substring
match). Calibration opens are counted and reported, never gated on their value. One constant,
`CALIBRATION_STILL_OPENS`, drives both modes (True: allow-list = calibration and the
must-engage raise is on; False, once #3741 lands: allow-list empty, raise off, `opens == 0`).
If no open is attributed to the calibration path on EITHER path the arm RAISES "exclusion never
engaged" (the #3739 lesson). WARM (owner up before the fire) is the gate; COLD is a reported row.

The contract cells below run against a SYNTHETIC mini session-start hook built here (six reader
functions with the real names, a calibration call into a stub `engram_client.py`, a stub owner shim,
a flag read from config.json). Mutants are produced by editing the mini hook's source text. The ONE
cell on the REAL hook was `xfail(strict=True)` while U9 (#3746) was not on dev; it was written to
XPASS, which fails the suite, which demanded its flip. It is flipped in #3746 itself: that cell
passing on the real hook is the gate's point.

Every cell's docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "engramd_a6_latency.py"
HOOK = "/plugin/hooks/engram-session-start-hook.py"
READERS = ("_check_backup_staleness", "_check_embedding_restore_flag", "focus_block",
           "saved_focus_sets_block", "session_checkpoint_block", "sleep_status_block")


def _load_tool():
    spec = importlib.util.spec_from_file_location("engramd_a6_latency_ss", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_TOOL = _load_tool()  # module level: the tool is loaded once for the whole file


@pytest.fixture(scope="module")
def tool():
    return _TOOL


# -- the synthetic mini hook ---------------------------------------------------------------

MINI_HOOK = '''
import json, os, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ENGRAM_HOME = os.environ["ENGRAM_HOME"]
DB = os.path.join(ENGRAM_HOME, "knowledge.db")
_RUN = None

def _owner_reads_enabled():
    try:
        with open(os.path.join(ENGRAM_HOME, "config.json")) as f:
            return json.load(f)["engramd"]["session_start_reads_via_owner"] is True
    except Exception:
        return False

def _direct(sql):
    c = sqlite3.connect(DB)
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()

def _read(sql):
    if _RUN is not None and _RUN["on"]:
        import engramd_client
        return engramd_client.OwnerReadConnection().execute(sql).fetchone()[0]
    return _direct(sql)

def _check_backup_staleness(h): return _read("SELECT count(*) FROM nodes")
def _check_embedding_restore_flag(h): return _read("SELECT count(*) FROM nodes")
def focus_block(h): return _read("SELECT count(*) FROM nodes")
def saved_focus_sets_block(h): return _read("SELECT count(*) FROM nodes")
def session_checkpoint_block(h, t): return _read("SELECT count(*) FROM nodes")
def sleep_status_block(): return _read("SELECT count(*) FROM nodes")

def main():
    global _RUN
    hook_input = json.load(sys.stdin)
    _RUN = {"on": _owner_reads_enabled()}
    #MAIN_EXTRA_BEFORE
    parts = [_check_backup_staleness(ENGRAM_HOME), _check_embedding_restore_flag(ENGRAM_HOME),
             focus_block(ENGRAM_HOME), saved_focus_sets_block(ENGRAM_HOME),
             session_checkpoint_block(ENGRAM_HOME, "2026-01-01"), sleep_status_block()]
    try:
        from engram_client import EngramClient
        parts.append(EngramClient().stats())
    except Exception:
        pass
    #MAIN_EXTRA_AFTER
    with open(os.path.join(ENGRAM_HOME, "last-mode"), "w") as f:
        f.write("owner" if _RUN["on"] else "direct")
    print(json.dumps({"session": hook_input.get("session_id"), "home": ENGRAM_HOME, "parts": parts}))

if __name__ == "__main__":
    main()
'''

MINI_ENGRAM_CLIENT = '''
import json, os, sqlite3
class EngramClient:
    def stats(self):
        n = 0
        times = 2  #TIMES
        for _ in range(times):
            c = sqlite3.connect(os.path.join(os.environ["ENGRAM_HOME"], "knowledge.db"))
            n += c.execute("SELECT count(*) FROM nodes").fetchone()[0]
            c.close()
        return n
'''

MINI_OWNER_SHIM = '''
class _Cur:
    def __init__(self, rows): self._r = rows
    def fetchone(self): return self._r[0]
    def fetchall(self): return self._r
class OwnerReadConnection:
    def execute(self, sql, params=()):
        return _Cur([(7,)])
'''


def _sub(src, old, new):
    assert src.count(old) == 1, old
    return src.replace(old, new)


FOCUS_LINE = 'def focus_block(h): return _read("SELECT count(*) FROM nodes")'
# the reader still reads through the owner AND re-opens the store on the flag-ON path (a reader that dropped the
# owner read altogether is the arm's other loud failure: "reader focus_block was not reached on the flag-ON path")
FOCUS_REOPENS_ON_THE_OWNER_PATH = ('def focus_block(h):\n    if _RUN["on"]:\n        _direct("SELECT count(*) FROM nodes")\n'
                                   '    return _read("SELECT count(*) FROM nodes")')


def _mini(tool, tmp_path, hook=None, client=MINI_ENGRAM_CLIENT, shim=MINI_OWNER_SHIM):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    hook_path = plugin / "engram-session-start-hook.py"
    hook_path.write_text(hook if hook is not None else MINI_HOOK)
    (plugin / "engram_client.py").write_text(client)
    (plugin / "engramd_client.py").write_text(shim)
    home = tmp_path / "home"
    home.mkdir()
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("CREATE TABLE nodes (id TEXT)")
    conn.executemany("INSERT INTO nodes VALUES (?)", [(f"a6_{i}",) for i in range(7)])  # 7 == the shim's canned count
    conn.commit()
    conn.close()
    (home / "config.json").write_text("{}")
    return tool.SessionStartRig(hook_path=hook_path, home=home, scratch=tmp_path / "scratch",
                                layout_root=plugin, installed=False)


def _opens_of(result, key):
    return result[key]


# -- (a) attribute_opens: pure ---------------------------------------------------------------

def _open(*frames, arg="/h/knowledge.db"):
    return {"arg": arg, "thread": "MainThread", "stack": list(frames)}


def test_a_calibration_frame_is_allowed_and_everything_else_is_a_violation_naming_its_frames(tool):
    """Allow-list by exact BASENAME `engram_client.py`. Mutations: match by substring ('client.py') ->
    the engramd_client.py open is allowed -> red; match by endswith('engram_client.py') -> the
    myengram_client.py open is allowed -> red; deny-list the six readers instead -> the unattributed
    open from main() passes -> red; name no frames in the violation -> red."""
    calib = _open("/plugin/hooks/engram-session-start-hook.py:main:4400",
                  "/plugin/engram_client.py:call:88", "/plugin/engram_core.py:_get_db:9")
    shim = _open("/plugin/hooks/engram-session-start-hook.py:focus_block:1790",
                 "/plugin/engramd_client.py:execute:1149")
    lookalike = _open("/plugin/hooks/engram-session-start-hook.py:main:4400",
                      "/plugin/myengram_client.py:call:3")
    neither = _open("/plugin/hooks/engram-session-start-hook.py:main:4100",
                    "/plugin/hooks/engram-session-start-hook.py:sleep_status_block:3182",
                    "/plugin/hooks/_hooklib.py:connect_regular_ro:77")
    got = tool.attribute_opens([calib, shim, lookalike, neither], Path(HOOK))
    assert got["calibration"] == [calib]
    assert len(got["violations"]) == 3
    by = {v.split(" innermost frame ")[1].split(";")[0]: v for v in got["violations"]}
    # the owner shim (basename engramd_client.py) is NOT on the allow-list; the violation names that frame
    assert "engramd_client.py:execute:1149" in by
    assert "myengram_client.py:call:3" in by
    inner = "_hooklib.py:connect_regular_ro:77"
    assert inner in by
    # every frame IN THE HOOK FILE is listed as file:function:line
    assert "engram-session-start-hook.py:main:4100" in by[inner]
    assert "engram-session-start-hook.py:sleep_status_block:3182" in by[inner]
    assert "engram-session-start-hook.py:focus_block:1790" in by["engramd_client.py:execute:1149"]


def test_an_open_with_no_stack_is_a_violation_not_silently_attributed(tool):
    """Mutation: treat an empty/missing stack as calibration (or crash) -> red."""
    got = tool.attribute_opens([{"arg": "/h/knowledge.db", "thread": "t", "stack": []}], Path(HOOK))
    assert got["calibration"] == [] and len(got["violations"]) == 1


# -- (h) the one constant ----------------------------------------------------------------------

def test_one_constant_drives_both_modes_and_they_cannot_drift(tool, monkeypatch):
    """CALIBRATION_STILL_OPENS True: calibration allowed, must-engage on. False (#3741 landed):
    allow-list empty -- a calibration open IS a violation -- and zero opens is clean. Mutations: a
    second constant / a hard-coded allow-list in either function, or must-engage reading its own
    flag -> the monkeypatch flips one mode and not the other -> red."""
    calib = _open("/plugin/hooks/engram-session-start-hook.py:main:4400", "/plugin/engram_client.py:call:88")
    assert tool.CALIBRATION_STILL_OPENS is True
    assert tool.attribute_opens([calib], Path(HOOK)) == {"calibration": [calib], "violations": []}
    assert tool.exclusion_must_engage() is True
    monkeypatch.setattr(tool, "CALIBRATION_STILL_OPENS", False)
    got = tool.attribute_opens([calib], Path(HOOK))
    assert got["calibration"] == [] and len(got["violations"]) == 1
    assert tool.attribute_opens([], Path(HOOK)) == {"calibration": [], "violations": []}
    assert tool.exclusion_must_engage() is False


# -- (b)-(f), (j) the sanity run on the mini hook -------------------------------------------------

def test_the_gate_passes_on_the_clean_mini_hook_and_reports_calibration_opens(tool, tmp_path):
    """Borges cell 1. Calibration opens counted (>= 1) on both paths, zero violations, readers reached on
    both paths, outputs equal after normalising, the flag state each path saw is the intended one.
    Mutation: gate on opens_flag_on == 0 -> red (calibration opens exist); report no calibration count -> red."""
    r = tool.session_start_sanity(_mini(tool, tmp_path))
    assert r["calibration_opens_off"] >= 1 and r["calibration_opens_on"] >= 1
    assert r["open_violations"] == []
    assert r["opens_flag_on"] == r["calibration_opens_on"] and r["opens_flag_off"] == 6 + r["calibration_opens_off"]
    assert sorted(r["readers_reached_off"]) == sorted(READERS) == sorted(r["readers_reached_on"])
    assert r["stdout_match"] is True
    assert r["flag_seen"] == {"off": False, "on": True}


def test_a_migrated_reader_that_reopens_the_store_is_a_violation_naming_that_reader(tool, tmp_path):
    """Borges cell 2. Mutation: attribute by 'any open under a reader function' / drop the violation list -> red."""
    hook = _sub(MINI_HOOK, FOCUS_LINE, FOCUS_REOPENS_ON_THE_OWNER_PATH)
    r = tool.session_start_sanity(_mini(tool, tmp_path, hook=hook))
    assert len(r["open_violations"]) == 1
    assert "focus_block" in r["open_violations"][0]


def test_an_extra_inline_connect_in_main_outside_every_reader_is_a_violation(tool, tmp_path):
    """Borges cell 3: the deny-list miss. Mutation: a deny-list of the six readers -> main()'s open is not a
    reader's -> passes -> red."""
    hook = _sub(MINI_HOOK, "    #MAIN_EXTRA_BEFORE", "    sqlite3.connect(DB).close()")
    r = tool.session_start_sanity(_mini(tool, tmp_path, hook=hook))
    assert len(r["open_violations"]) == 1
    assert ":main:" in r["open_violations"][0]


def test_a_compensating_mutant_that_keeps_the_open_count_identity_is_still_caught(tool, tmp_path):
    """Borges cell 4. One reader re-opens on the ON path AND the calibration opens one fewer on the ON
    path, so opens_on == opens_off - 6 still holds (asserted below, so this cell proves it separates the
    allow-list from a count-delta gate). Mutation: gate on the count identity instead of attribution -> red."""
    hook = _sub(MINI_HOOK, FOCUS_LINE, FOCUS_REOPENS_ON_THE_OWNER_PATH)
    client = _sub(MINI_ENGRAM_CLIENT, "times = 2  #TIMES",
                  "times = 1 if json.load(open(os.path.join(os.environ['ENGRAM_HOME'], 'config.json')))"
                  "['engramd']['session_start_reads_via_owner'] is True else 2")
    r = tool.session_start_sanity(_mini(tool, tmp_path, hook=hook, client=client))
    # OFF: 6 reader opens + 2 calibration = 8; ON: 1 calibration (the stub opens once on the ON path) + 1
    # re-open = 2 = OFF - 6: the count identity holds although the open is not the calibration's
    assert r["opens_flag_on"] == r["opens_flag_off"] - 6
    assert len(r["open_violations"]) == 1 and "focus_block" in r["open_violations"][0]


def test_an_inert_calibration_raises_exclusion_never_engaged_instead_of_passing(tool, tmp_path):
    """Borges cell 5: zero opens attributed to the calibration path means the attribution, the layout or the
    fixture is blind. Mutation: return a pass / put it in the violation list instead of raising; drop the
    must-engage check -> red."""
    rig = _mini(tool, tmp_path, client="raise ImportError('inert')\n")
    with pytest.raises(tool.ArmBlind, match="exclusion never engaged"):
        tool.session_start_sanity(rig)


def test_must_engage_is_off_when_the_constant_is_false(tool, tmp_path, monkeypatch):
    """Mutation: the must-engage raise ignores CALIBRATION_STILL_OPENS -> an inert calibration still raises
    after #3741 -> red. (With the constant False and migrated calibration the cell is `opens == 0`.)"""
    monkeypatch.setattr(tool, "CALIBRATION_STILL_OPENS", False)
    rig = _mini(tool, tmp_path, client="raise ImportError('inert')\n")
    r = tool.session_start_sanity(rig)
    assert r["opens_flag_on"] == 0 and r["open_violations"] == []


def test_a_reader_that_never_reads_makes_the_arm_raise_naming_it(tool, tmp_path):
    """The payload must reach every read site. Mutation: skip the reached-check, or only check the OFF
    path -> red (this mutant is silent on the OFF-path count alone)."""
    hook = _sub(MINI_HOOK, FOCUS_LINE, 'def focus_block(h): return 0')
    with pytest.raises(tool.ArmBlind, match="focus_block"):
        tool.session_start_sanity(_mini(tool, tmp_path, hook=hook))


def test_a_reader_missing_only_on_the_owner_path_also_raises(tool, tmp_path):
    """Mutation: check the OFF path only -> red."""
    hook = _sub(MINI_HOOK, 'def sleep_status_block(): return _read("SELECT count(*) FROM nodes")',
                'def sleep_status_block(): return _direct("SELECT count(*) FROM nodes") if not _RUN["on"] else 0')
    with pytest.raises(tool.ArmBlind, match="sleep_status_block"):
        tool.session_start_sanity(_mini(tool, tmp_path, hook=hook))


# -- (g) warm vs cold ----------------------------------------------------------------------

def test_a_warm_fire_that_prints_a_wait_notice_raises_warm_fire_waited(tool, tmp_path):
    """The warm assumption broke. Mutation: remove the warm-wait check -> the arm passes -> red."""
    hook = _sub(MINI_HOOK, "    #MAIN_EXTRA_AFTER",
                '    parts.append("waited 40 ms, then served") if _RUN["on"] else None')
    with pytest.raises(tool.ArmBlind, match="warm fire waited"):
        tool.session_start_sanity(_mini(tool, tmp_path, hook=hook))


def test_the_clean_warm_fire_has_no_wait_text_and_the_check_is_pure(tool):
    """Mutation: flag any 'ms' text / flag nothing -> red."""
    tool.assert_no_warm_wait(['{"parts": [1, 2]}', "took 40 ms to render"], "x")
    with pytest.raises(tool.ArmBlind, match="warm fire waited"):
        tool.assert_no_warm_wait(['ENGRAM substrate health: engramd owner was not ready at the first read; '
                                  'waited 12 ms, then served'], "x")


def test_the_cold_row_parses_the_wait_from_the_hooks_own_notice_and_is_never_a_violation(tool):
    """Mutation: parse the wrong number / treat a long cold wait as a violation -> red."""
    line = ("\\u2139\\ufe0f  ENGRAM substrate health: engramd owner was not ready at the first read; "
            "waited 412 ms, then served")
    assert tool.parse_owner_wait_ms(line) == 412
    assert tool.parse_owner_wait_ms("no notice here, 5 ms elsewhere") is None
    row = tool.summarise_cold([{"wall_ms": 700.0, "wait_ms": 412}, {"wall_ms": 650.0, "wait_ms": None},
                               {"wall_ms": 900.0, "wait_ms": 480}])
    assert row["n"] == 3 and row["served_without_wait"] == 1
    assert row["wait_ms_max"] == 480 and row["wait_ms_p50"] == 412
    result = _clear_result(cold=row)
    assert tool.ss_contract_violations(result) == []


# -- the table of the pure gate -------------------------------------------------------------------

def _clear_result(**over):
    r = {"hook": "session-start", "stdout_match": True, "open_violations": [],
         "opens_flag_on": 2, "opens_flag_off": 8, "calibration_opens_on": 2, "calibration_opens_off": 2}
    r.update(over)
    return r


def test_ss_contract_violations_table(tool):
    """Mutation: any row below not gated, or the missing key not failing closed -> red."""
    assert tool.ss_contract_violations(_clear_result()) == []
    v = tool.ss_contract_violations(_clear_result(stdout_match=False))
    assert len(v) == 1 and "stdout_match" in v[0]
    v = tool.ss_contract_violations(_clear_result(stdout_match=1))
    assert any("stdout_match" in x for x in v)
    v = tool.ss_contract_violations(_clear_result(open_violations=["unattributed open ... focus_block"]))
    assert len(v) == 1 and "focus_block" in v[0]
    bad = _clear_result()
    del bad["open_violations"]
    assert any("open_violations" in x for x in tool.ss_contract_violations(bad))


def test_ss_contract_violations_is_pure(tool, monkeypatch):
    """Mutation: do I/O or mutate the input -> red."""
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("I/O in a pure function"))
    r = _clear_result(open_violations=["x"])
    before = json.loads(json.dumps(r))
    assert tool.ss_contract_violations(r)
    assert r == before


# -- (i) exit codes, CLI ----------------------------------------------------------------------------

def _patch_main(tool, monkeypatch, result):
    monkeypatch.setattr(tool, "measure_in_layout",
                        lambda layout, **kw: dict(result, answers_match=result.get("stdout_match", True),
                                                  layout=layout.name))
    monkeypatch.setattr(tool, "format_layouts", lambda rs: "THE-TABLE")


def test_hooks_contains_session_start_and_the_cli_accepts_it(tool, monkeypatch):
    """Mutation: drop 'session-start' from HOOKS -> argparse rejects it -> red."""
    assert "session-start" in tool.HOOKS
    seen = []
    monkeypatch.setattr(tool, "measure_in_layout",
                        lambda layout, **kw: seen.append(kw["hook"]) or dict(_clear_result(), answers_match=True,
                                                                              layout=layout.name))
    monkeypatch.setattr(tool, "format_layouts", lambda rs: "THE-TABLE")
    assert tool.main(["--hook", "session-start", "--layout", "source"]) == 0
    assert seen == ["session-start"]


def test_main_exits_3_on_a_contract_violation_and_prints_it_after_the_table(tool, monkeypatch, capsys):
    """Mutation: return 0/1 on violations, or print before the table -> red. Exit 3 outranks exit 1."""
    argv = ["--hook", "session-start", "--layout", "source"]
    _patch_main(tool, monkeypatch, _clear_result())
    assert tool.main(argv) == 0
    capsys.readouterr()
    _patch_main(tool, monkeypatch, _clear_result(open_violations=["unattributed open focus_block"]))
    assert tool.main(argv) == 3
    out = capsys.readouterr().out
    assert out.index("THE-TABLE") < out.index("focus_block")
    _patch_main(tool, monkeypatch, _clear_result(stdout_match=False))
    assert tool.main(argv) == 3


def _raising_measure(tool, monkeypatch, exc):
    def boom(layout, **kw):
        raise exc
    monkeypatch.setattr(tool, "measure_in_layout", boom)
    monkeypatch.setattr(tool, "format_layouts", lambda rs: "THE-TABLE")


@pytest.mark.parametrize("hook", ["session-start", "tripwire"])
def test_main_exits_4_on_a_blind_arm_or_a_layout_violation_and_never_raises(tool, monkeypatch, capsys, hook):
    """A blind arm is not "the two paths answered differently" (exit 1): it is exit 4, with one loud stderr line
    naming the exception. Mutation: drop the catch in main() -> the exception propagates -> red."""
    argv = ["--hook", hook, "--layout", "source"]
    for exc in (tool.ArmBlind("the flag-OFF fire did not see the flag state"),
                tool.LayoutViolation("the owner did not run from the build")):
        _raising_measure(tool, monkeypatch, exc)
        assert tool.main(argv) == 4
        err = capsys.readouterr().err
        assert type(exc).__name__ in err and str(exc) in err and len(err.strip().splitlines()) == 1


def test_a_layout_child_that_dies_with_an_arm_blind_traceback_surfaces_as_arm_blind_and_exit_4(tool, monkeypatch, capsys):
    """measure_in_layout sees only the child's stderr; the child's ArmBlind / LayoutViolation traceback must come
    back typed (still a RuntimeError), and any OTHER child death must stay a plain RuntimeError.
    Mutation: keep the old blanket RuntimeError -> red."""
    class P:
        returncode = 1
        stdout = ""
        def __init__(self, err):
            self.stderr = err
    layout = tool.source_layout()
    tb = 'Traceback (most recent call last):\n  File "x", line 1, in <module>\n{}: {}\n'
    monkeypatch.setattr(tool.subprocess, "run", lambda *a, **k: P(tb.format("ArmBlind", "reader focus_block missed")))
    with pytest.raises(tool.ArmBlind, match="focus_block"):
        tool.measure_in_layout(layout, hook="session-start")
    assert tool.main(["--hook", "session-start", "--layout", "source"]) == 4
    monkeypatch.setattr(tool.subprocess, "run", lambda *a, **k: P(tb.format("LayoutViolation", "drifted")))
    with pytest.raises(tool.LayoutViolation, match="drifted"):
        tool.measure_in_layout(layout, hook="session-start")
    monkeypatch.setattr(tool.subprocess, "run", lambda *a, **k: P(tb.format("KeyError", "boom")))
    with pytest.raises(RuntimeError) as exc:
        tool.measure_in_layout(layout, hook="session-start")
    assert not isinstance(exc.value, (tool.ArmBlind, tool.LayoutViolation))


def test_a_layout_violation_raised_by_the_build_step_itself_is_exit_4_not_a_traceback(tool, monkeypatch, capsys):
    """build_installed_layout raises LayoutViolation ("the build differs from the tree under test") BEFORE any
    measurement runs; main() must still answer 4. Mutation: construct the layout outside the try -> red."""
    def boom(root):
        raise tool.LayoutViolation("build differs from the tree under test: x.py")
    monkeypatch.setattr(tool, "build_installed_layout", boom)
    monkeypatch.setattr(tool, "measure_in_layout", lambda *a, **k: pytest.fail("measured"))
    assert tool.main(["--hook", "session-start", "--layout", "installed"]) == 4
    err = capsys.readouterr().err
    assert "LayoutViolation" in err and "build differs" in err and len(err.strip().splitlines()) == 1


def test_exit_1_still_means_only_that_the_two_paths_answered_differently(tool, monkeypatch):
    """Control for exit 4: the genuine differing-answers case (user-identity, which has no contract gate) is 1."""
    _patch_main(tool, monkeypatch, _clear_result(stdout_match=False))
    assert tool.main(["--hook", "user-identity", "--layout", "source"]) == 1
    _patch_main(tool, monkeypatch, _clear_result())
    assert tool.main(["--hook", "user-identity", "--layout", "source"]) == 0


def test_exit_2_for_exclusion_floor_and_live_store_with_this_hook(tool, monkeypatch, tmp_path):
    """--exclusion stays valid only with tripwire; the n floor and the live-store refusal hold. Mutation: accept
    --exclusion here, or skip the floor on this branch -> red."""
    monkeypatch.setattr(tool, "launch_owner", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(SystemExit) as exc:
        tool.main(["--hook", "session-start", "--exclusion", "on"])
    assert exc.value.code == 2
    assert tool.main(["--hook", "session-start", "--n", "50"]) == 2
    home = tmp_path / "h"
    home.mkdir()
    (home / "knowledge.db").write_bytes(b"")
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    assert tool.main(["--hook", "session-start", "--store", str(home / "knowledge.db")]) == 2


# -- (k) layout / closure ---------------------------------------------------------------------------

def test_layout_carries_the_session_start_hook_and_the_old_payload_still_loads(tool, tmp_path):
    """Mutation: drop the field from to_json/from_json -> the child would measure the source hook -> red;
    break the old three-hook payload -> red."""
    lay = tool.Layout(name="installed", root=tmp_path, stop_hook=tmp_path / "hooks" / "s.py",
                      user_hook=tmp_path / "hooks" / "u.py", tripwire_hook=tmp_path / "hooks" / "t.py",
                      session_start_hook=tmp_path / "hooks" / "ss.py")
    assert tool.Layout.from_json(lay.to_json()).session_start_hook == tmp_path / "hooks" / "ss.py"
    old = json.dumps({"name": "installed", "root": str(tmp_path), "stop_hook": str(tmp_path / "hooks" / "s.py"),
                      "user_hook": str(tmp_path / "hooks" / "u.py"), "tripwire_hook": str(tmp_path / "hooks" / "t.py")})
    loaded = tool.Layout.from_json(old)
    assert loaded.session_start_hook == tmp_path / "hooks" / "engram-session-start-hook.py"
    assert tool.source_layout().session_start_hook == tool.SESSION_START_HOOK_PATH
    assert tool.SESSION_START_HOOK_PATH.name == "engram-session-start-hook.py"


def test_every_runtime_file_is_a_tiered_shippable_in_the_manifest(tool):
    """The installed-layout byte check lists only files the build ships. Mutation: add a RUNTIME_FILES entry the
    manifest does not tier (a typo, a test helper) -> red."""
    manifest = json.loads((REPO_ROOT / "src" / "build" / "packaging" / "tiers.json").read_text())
    root = manifest["source_root"]
    paths = [m["path"] for m in manifest["mechanisms"]]
    for built, src in tool.RUNTIME_FILES.items():
        assert src.startswith(root + "/"), src
        rel = src[len(root) + 1:]
        assert any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in paths), (built, rel)
    assert tool.RUNTIME_FILES["hooks/engram-session-start-hook.py"] == "src/engram/hooks/claude/engram-session-start-hook.py"
    assert tool.RUNTIME_FILES["engram_client.py"] == "src/engram/engram_client.py"


# -- (l) the normaliser ----------------------------------------------------------------------------

def _out(sid, home, overflow="20261013T005318Z-22121.md", body="focus: alpha", extra=""):
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": (
        f"▸ 5 shown · 1 cut — see {home}/session-start-overflow/{overflow}\n"
        f"[SessionStart {sid}]\n[Active session: {sid} (source=startup) — transcript at {home}/t.jsonl]\n"
        f"{body}{extra}")}})


def test_two_outputs_differing_only_in_noise_normalise_equal(tool):
    """(l-i) Session ids, the scratch home, the overflow file's timestamp-pid name are noise. Mutation: leave
    any of the three unnormalised -> red."""
    a = _out("0e81ba8b-a1ad-4434-8b3a-7317e4cdda3b", "/var/f/a6-aaa")
    b = _out("55962ec6-ae38-4035-b8a4-9fe434655bfb", "/var/f/a6-bbb", overflow="20261013T005400Z-9.md")
    assert a != b
    assert tool.normalise_session_start_output(a, "/var/f/a6-aaa") == tool.normalise_session_start_output(b, "/var/f/a6-bbb")


def test_changing_one_real_content_line_makes_the_normalised_outputs_differ(tool):
    """(l-ii) The discrimination: a normaliser that masks everything passes (i). Mutation: normalise by
    blanking the whole text / the whole additionalContext -> red."""
    home = "/var/f/a6-aaa"
    sid = "0e81ba8b-a1ad-4434-8b3a-7317e4cdda3b"
    a = tool.normalise_session_start_output(_out(sid, home, body="  - [a6_focus_1] focus text one"), home)
    b = tool.normalise_session_start_output(_out(sid, home, body="  - [a6_focus_1] focus text TWO"), home)
    assert a != b


def test_the_normaliser_does_not_mask_the_owner_cold_start_notice(tool):
    """(l-iii) The ON path's `waited N ms` text must survive normalisation, so a stdout_match that compares a
    cold-start fire to a clean one fails rather than being masked. Mutation: strip digits-ms spans / the whole
    health line -> red."""
    home = "/var/f/a6-aaa"
    sid = "0e81ba8b-a1ad-4434-8b3a-7317e4cdda3b"
    notice = ("\nINFO ENGRAM substrate health: engramd owner was not ready at the first read; "
              "waited 40 ms, then served")
    plain = tool.normalise_session_start_output(_out(sid, home), home)
    waited = tool.normalise_session_start_output(_out(sid, home, extra=notice), home)
    assert plain != waited
    assert tool.parse_owner_wait_ms(waited) == 40


# -- the per-fire flag (decision 2) and stack capture (m) ------------------------------------------

def test_each_timed_fire_sees_its_own_path_flag_when_the_paths_interleave(tool, tmp_path):
    """The flag is rewritten before EVERY timed fire (the sequential arm interleaves the paths). Mutation: write
    the config once (per phase) in the sequential arm -> the second path's fire reads the first path's flag -> red."""
    rig = _mini(tool, tmp_path)
    baseline, owner = rig.make_paths()
    modes = []
    for fire in (baseline, owner, baseline, owner):
        fire.reset()
        json.loads(fire())
        modes.append((rig.home / "last-mode").read_text())
    assert modes == ["direct", "owner", "direct", "owner"]


def test_stack_capture_leaves_the_existing_record_contract_alone(tool, tmp_path):
    """(m) A stacks window yields a record that is still a list of the opened strings (`len(rec)`, `rec == []`) with
    the stacks beside it. Mutation: put the stack dicts IN the record list -> `rec == [str]` breaks -> red; capture
    stacks for a window that did not ask -> the plain window grows a `details` -> red."""
    db = tmp_path / "k.db"
    sqlite3.connect(str(db)).close()
    with tool._counting_store_opens(db) as plain:
        assert plain == [] and not hasattr(plain, "details")
        sqlite3.connect(str(db)).close()
    assert len(plain) == 1 and plain == [str(db)]
    with tool._counting_store_opens(db, stacks=True) as rec:
        assert rec == [] and len(rec) == 0
        sqlite3.connect(str(db)).close()
        sqlite3.connect(str(tmp_path / "other.db")).close()
    assert rec == [str(db)] and len(rec) == 1
    d = rec.details
    assert len(d) == 1 and d[0]["arg"] == str(db) and d[0]["thread"]
    assert any(f.split(":")[1] == "test_stack_capture_leaves_the_existing_record_contract_alone" for f in d[0]["stack"])
    assert d[0]["stack"][-1].split(":")[1] == "test_stack_capture_leaves_the_existing_record_contract_alone"


def test_the_audit_hook_never_raises_when_stack_capture_fails(tool, tmp_path, monkeypatch):
    """An audit hook that raises aborts the open it observes. Mutation: let the stack walk's exception out -> the
    open fails -> red."""
    db = tmp_path / "k.db"
    sqlite3.connect(str(db)).close()
    monkeypatch.setattr(tool.traceback, "walk_stack", lambda f: (_ for _ in ()).throw(RuntimeError("boom")))
    with tool._counting_store_opens(db, stacks=True) as rec:
        sqlite3.connect(str(db)).close()
    assert len(rec) in (0, 1)


# -- the REAL hook: written before U9, flipped from xfail(strict=True) in #3746 itself ----------------

@requires_linux_owner
def test_the_arm_on_the_real_session_start_hook_passes_the_gate_on_the_installed_and_source_layouts(tool, tmp_path):
    """End to end on the real hook (needs the U9 flag in the hook: `engramd.session_start_reads_via_owner`).
    Was xfail(strict=True) until #3746; the mark is removed in the same PR that lands U9, so this now runs and
    must pass. Mutation: any cell above's mutant reaching the real hook -> red."""
    for layout in (tool.build_installed_layout(tmp_path / "plugin"), tool.source_layout()):
        r = tool.measure_in_layout(layout, hook="session-start", n=12, nodes=300, warmup=3, n_cold=3)
        assert tool.ss_contract_violations(r) == []
        assert r["calibration_opens_on"] >= 1 and r["calibration_opens_off"] >= 1
        assert sorted(r["readers_reached_on"]) == sorted(READERS)


@requires_linux_owner
def test_the_installed_layout_session_start_fire_loads_only_files_listed_in_runtime_files(tool, tmp_path_factory):
    """The closure cell for the new arm: every runtime file a session-start fire loads from the BUILD (both flag
    states) is in RUNTIME_FILES, so the byte-identity check covers what ran. Mutation: drop any loaded file from
    RUNTIME_FILES, or load the hook from the source tree -> red."""
    layout = tool.build_installed_layout(tmp_path_factory.mktemp("a6-ss-build"))
    assert layout.session_start_hook == layout.root / "hooks" / "engram-session-start-hook.py"
    assert tool.layout_matches_tree(layout) == []
    loaded = set(tool.session_start_loaded_files(layout))
    assert "hooks/engram-session-start-hook.py" in loaded and "engram_client.py" in loaded
    assert loaded <= set(tool.RUNTIME_FILES), sorted(loaded - set(tool.RUNTIME_FILES))
