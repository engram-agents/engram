"""Tests for tools/check_stale_processes.py -- the stale long-lived process gate (engram-alpha#3938).

Contract-first: these cells were written before the tool existed.  The tool compares the start
time of the three long-lived ENGRAM processes (engramd owner, surface daemon, MCP server) with
`deployed_at` in `.deployed-version`.  Per process ONE line, in fixed order (owner, surface-daemon,
mcp-server), then ONE summary line: always four lines.  Verdicts: FRESH, STALE, NOT_RUNNING,
UNVERIFIED.  Gate exit codes: 0 all FRESH; 1 any STALE; 3 no STALE but some NOT_RUNNING /
UNVERIFIED (a missing pidfile is NEVER clean); 2 `.deployed-version` unusable.  `--informational`
exits 0 always.

Time and process readers are injected, so no cell sleeps or touches a real process other than the
test's own pid.  Each cell's docstring names the mutation that must redden it, where one exists.
"""

import calendar
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = REPO_ROOT / "src" / "engram"
_TOOL_PATH = _SRC_ENGRAM / "tools" / "check_stale_processes.py"
if str(_SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(_SRC_ENGRAM))

DEPLOYED_AT = "2026-10-20T12:00:00Z"
DEPLOYED_EPOCH = calendar.timegm(time.strptime(DEPLOYED_AT, "%Y-%m-%dT%H:%M:%SZ"))

OWNER_PID, DAEMON_PID, MCP_PID = 40001, 40002, 40003
# The real argv shapes, measured 2026-10-20 from /proc/<pid>/cmdline (NUL-split): the interpreter,
# then the launched script, then (surface daemon only) the ENGRAM home.
ARGV = {
    OWNER_PID: ["/v/bin/python3", "/p/engramd_owner.py"],
    DAEMON_PID: ["/v/bin/python3", "/p/hooks/engram-surface-daemon.py", "/home"],
    MCP_PID: ["/v/bin/python3", "/p/server.py"],
}


def _load():
    spec = importlib.util.spec_from_file_location("check_stale_processes_under_test", _TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass annotations resolve through sys.modules
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tool():
    return _load()


@pytest.fixture()
def home(tmp_path):
    h = tmp_path / "eh"
    h.mkdir()
    (h / ".deployed-version").write_text(
        "alpha_sha=abc\nalpha_tag=none\ndeployed_at=%s\ndeployed_from=/x\n" % DEPLOYED_AT)
    (h / "engramd.pid").write_text("%d\n" % OWNER_PID)
    (h / "recall-daemon.pid").write_text("%d\n" % DAEMON_PID)
    (h / "mcp-tools-ready.json").write_text(json.dumps({"pid": MCP_PID, "registered_at": "x"}))
    return h


_PIDFILE_PID = {"engramd.pid": OWNER_PID, "recall-daemon.pid": DAEMON_PID,
                "mcp-tools-ready.json": MCP_PID}


def _readers(tool, starts, alive=None, argv=None, mtimes=None):
    """Fixed readers: `starts` pid -> epoch; liveness/argv default to the three fixture pids.
    Instrument B (pidfile mtime) defaults to the pid's own start, i.e. agreeing with A."""
    mt = dict(starts) if mtimes is None else mtimes
    alive_set = set(ARGV) if alive is None else set(alive)
    argv_map = dict(ARGV) if argv is None else argv
    return tool.Readers(
        alive=lambda pid: pid in alive_set,
        argv=lambda pid: argv_map.get(pid),
        start_epoch=lambda pid: starts.get(pid),
        mtime=lambda path: mt.get(_PIDFILE_PID.get(Path(path).name)),
    )


def _fresh_starts(offset=100):
    t = DEPLOYED_EPOCH + offset
    return {OWNER_PID: t, DAEMON_PID: t, MCP_PID: t}


def _verdicts(lines):
    return [ln.split()[1] for ln in lines[:3]]


def test_all_fresh_exit0_four_lines(tool, home):
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts()), system="Linux")
    assert code == 0
    assert len(lines) == 4
    assert _verdicts(lines) == ["FRESH"] * 3
    assert [ln.split(":")[0] for ln in lines[:3]] == ["owner", "surface-daemon", "mcp-server"]


def test_owner_one_second_before_deploy_is_stale_exit1(tool, home):
    starts = _fresh_starts()
    starts[OWNER_PID] = DEPLOYED_EPOCH - 1
    lines, code = tool.run_check(home, False, _readers(tool, starts), system="Linux")
    assert code == 1
    assert _verdicts(lines) == ["STALE", "FRESH", "FRESH"]
    # the STALE line names pid, start time (UTC ISO), deployed_at, and a restart command
    assert str(OWNER_PID) in lines[0]
    assert "2026-10-20T11:59:59Z" in lines[0]
    assert DEPLOYED_AT in lines[0]
    assert "setup-surface-daemon-service.sh --owner" in lines[0]


def test_surface_daemon_stale_alone_exit1(tool, home):
    starts = _fresh_starts()
    starts[DAEMON_PID] = DEPLOYED_EPOCH - 3600
    lines, code = tool.run_check(home, False, _readers(tool, starts), system="Linux")
    assert code == 1
    assert _verdicts(lines) == ["FRESH", "STALE", "FRESH"]
    assert "start-engram-daemon.sh" in lines[1]


def test_stale_takes_precedence_over_not_running(tool, home):
    starts = _fresh_starts()
    starts[OWNER_PID] = DEPLOYED_EPOCH - 5
    (home / "recall-daemon.pid").unlink()
    lines, code = tool.run_check(home, False, _readers(tool, starts), system="Linux")
    assert _verdicts(lines) == ["STALE", "NOT_RUNNING", "FRESH"]
    assert code == 1


def test_missing_pidfile_is_not_running_exit3_never_clean(tool, home):
    """Mutation: map a missing pidfile to FRESH -> this cell (and only the missing-pidfile cells) reddens."""
    (home / "engramd.pid").unlink()
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts()), system="Linux")
    assert _verdicts(lines) == ["NOT_RUNNING", "FRESH", "FRESH"]
    assert code == 3
    assert len(lines) == 4


def test_missing_mcp_marker_is_not_running_exit3(tool, home):
    (home / "mcp-tools-ready.json").unlink()
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts()), system="Linux")
    assert _verdicts(lines) == ["FRESH", "FRESH", "NOT_RUNNING"]
    assert code == 3


def test_dead_pid_is_not_running_exit3(tool, home):
    alive = {DAEMON_PID, MCP_PID}
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts(), alive=alive), system="Linux")
    assert _verdicts(lines) == ["NOT_RUNNING", "FRESH", "FRESH"]
    assert code == 3


def test_live_pid_with_foreign_argv_is_unverified_exit3(tool, home):
    """A recycled pid: alive, but argv is some other program."""
    argv = dict(ARGV)
    argv[OWNER_PID] = ["/usr/bin/vim", "notes.txt"]
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts(), argv=argv), system="Linux")
    assert _verdicts(lines) == ["UNVERIFIED", "FRESH", "FRESH"]
    assert code == 3


def test_own_pid_with_real_readers_is_unverified(tool, home):
    """The test's own python pid is alive and its argv is not an ENGRAM process (real readers)."""
    (home / "engramd.pid").write_text("%d\n" % os.getpid())
    lines, code = tool.run_check(home, False, None, system="Linux")
    assert lines[0].split()[1] == "UNVERIFIED"
    assert code == 3


def test_unreadable_start_time_is_unverified(tool, home):
    starts = _fresh_starts()
    del starts[MCP_PID]
    lines, code = tool.run_check(home, False, _readers(tool, starts), system="Linux")
    assert _verdicts(lines) == ["FRESH", "FRESH", "UNVERIFIED"]
    assert code == 3


@pytest.mark.parametrize("content", ["abc", "-5", "0", "", "  \n", "12 13"])
def test_bad_pidfile_content_is_unverified_exit3(tool, home, content):
    (home / "engramd.pid").write_text(content)
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts()), system="Linux")
    assert lines[0].split()[1] == "UNVERIFIED"
    assert code == 3
    assert len(lines) == 4


def test_fifo_pidfile_is_unverified_and_returns_promptly(tmp_path):
    """A FIFO pidfile must not hang the gate (#3591/#3628).  Run as a subprocess with a hard timeout."""
    h = tmp_path / "eh"
    h.mkdir()
    (h / ".deployed-version").write_text("deployed_at=%s\n" % DEPLOYED_AT)
    os.mkfifo(h / "engramd.pid")
    env = dict(os.environ, ENGRAM_HOME=str(h))
    r = subprocess.run([sys.executable, str(_TOOL_PATH)], env=env, capture_output=True,
                       text=True, timeout=5)
    out = r.stdout.splitlines()
    assert len(out) == 4, r.stdout + r.stderr
    assert out[0].split()[1] == "UNVERIFIED"
    assert "FIFO" in out[0]
    assert r.returncode == 3


def test_deployed_version_missing_exit2_four_lines(tool, home):
    (home / ".deployed-version").unlink()
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts()), system="Linux")
    assert code == 2
    assert len(lines) == 4
    assert _verdicts(lines) == ["UNVERIFIED"] * 3


def test_deployed_version_without_deployed_at_exit2(tool, home):
    (home / ".deployed-version").write_text("alpha_sha=abc\n")
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts()), system="Linux")
    assert code == 2
    assert len(lines) == 4


def test_deployed_version_unparseable_deployed_at_exit2(tool, home):
    (home / ".deployed-version").write_text("deployed_at=yesterday\n")
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts()), system="Linux")
    assert code == 2


def test_deployed_version_fifo_exit2_promptly(tmp_path):
    h = tmp_path / "eh"
    h.mkdir()
    os.mkfifo(h / ".deployed-version")
    env = dict(os.environ, ENGRAM_HOME=str(h))
    r = subprocess.run([sys.executable, str(_TOOL_PATH)], env=env, capture_output=True,
                       text=True, timeout=5)
    assert r.returncode == 2
    assert len(r.stdout.splitlines()) == 4
    assert "FIFO" in r.stdout


def test_informational_stale_exit0_and_command_shown(tool, home):
    starts = _fresh_starts()
    starts[OWNER_PID] = DEPLOYED_EPOCH - 60
    lines, code = tool.run_check(home, True, _readers(tool, starts), system="Linux")
    assert code == 0
    assert lines[0].split()[1] == "STALE"
    assert "systemctl --user restart" in lines[0]


def test_informational_exit0_even_when_unjudgeable(tool, home):
    (home / ".deployed-version").unlink()
    lines, code = tool.run_check(home, True, _readers(tool, _fresh_starts()), system="Linux")
    assert code == 0
    assert len(lines) == 4


def test_summary_states_the_mcp_bound(tool, home):
    lines, _ = tool.run_check(home, False, _readers(tool, _fresh_starts()), system="Linux")
    assert "mcp-tools-ready.json" in lines[3]
    assert "other" in lines[3]


def test_boundary_start_equals_deployed_is_fresh(tool, home):
    """Mutation: compare with <= instead of < -> this cell reddens (and no other)."""
    starts = {p: DEPLOYED_EPOCH for p in (OWNER_PID, DAEMON_PID, MCP_PID)}
    lines, code = tool.run_check(home, False, _readers(tool, starts), system="Linux")
    assert _verdicts(lines) == ["FRESH"] * 3
    assert code == 0


def test_fractional_start_is_floored(tool, home):
    """start 11:59:59.9 floors to 11:59:59 < deployed -> STALE; 12:00:00.4 floors to deployed -> FRESH."""
    starts = _fresh_starts()
    starts[OWNER_PID] = DEPLOYED_EPOCH - 0.1
    starts[DAEMON_PID] = DEPLOYED_EPOCH + 0.4
    lines, _ = tool.run_check(home, False, _readers(tool, starts), system="Linux")
    assert _verdicts(lines) == ["STALE", "FRESH", "FRESH"]


def test_proc_stat_parse_comm_with_parens_and_spaces(tool):
    # fields: pid (comm) state ppid ... ; starttime is field 22 (index 19 after the last ")").
    after_comm = ["S"] + ["0"] * 18 + ["987654"] + ["0"] * 10
    text = "123 (we) (ird ) name) " + " ".join(after_comm) + "\n"
    assert tool.parse_proc_stat_starttime_ticks(text) == 987654


@pytest.mark.parametrize("text,secs", [
    ("05:07", 5 * 60 + 7),
    ("01:05:07", 3600 + 5 * 60 + 7),
    ("2-01:05:07", 2 * 86400 + 3600 + 5 * 60 + 7),
    ("  00:09 \n", 9),
])
def test_etime_parse(tool, text, secs):
    assert tool.parse_etime(text) == secs


def test_etime_garbage_is_none(tool):
    assert tool.parse_etime("") is None
    assert tool.parse_etime("abc") is None


def test_parse_deployed_at(tool):
    assert tool.parse_deployed_at("x=1\ndeployed_at=%s\n" % DEPLOYED_AT) == DEPLOYED_EPOCH
    assert tool.parse_deployed_at("x=1\n") is None


def test_proc_fresh_but_pidfile_mtime_stale_is_stale(tool, home):
    """The false-all-clear guard (WSL2 btime moved forward): A says fresh, B says stale -> STALE.
    Mutation: drop instrument B -> this cell reddens, the others stay green."""
    mt = _fresh_starts()
    mt[OWNER_PID] = DEPLOYED_EPOCH - 30
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts(), mtimes=mt), system="Linux")
    assert _verdicts(lines) == ["STALE", "FRESH", "FRESH"]
    assert code == 1
    assert "pidfile mtime" in lines[0]


def test_proc_stale_but_pidfile_mtime_fresh_is_stale(tool, home):
    starts = _fresh_starts()
    starts[DAEMON_PID] = DEPLOYED_EPOCH - 30
    lines, code = tool.run_check(home, False, _readers(tool, starts, mtimes=_fresh_starts()), system="Linux")
    assert _verdicts(lines) == ["FRESH", "STALE", "FRESH"]
    assert code == 1


def test_instruments_120s_apart_both_fresh_is_fresh_with_note(tool, home):
    mt = _fresh_starts()
    mt[MCP_PID] = DEPLOYED_EPOCH + 100 + 120
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts(), mtimes=mt), system="Linux")
    assert _verdicts(lines) == ["FRESH"] * 3
    assert code == 0
    assert "instruments disagree" in lines[2]
    assert "instruments disagree" not in lines[0]


def test_huge_pid_is_not_running_not_a_traceback(tool, home):
    """os.kill raises OverflowError for a pid >= 2**31; the real reader must answer 'not alive'."""
    (home / "engramd.pid").write_text("99999999999999999999\n")
    r = tool.real_readers()
    assert r.alive(99999999999999999999) is False
    lines, code = tool.run_check(home, False, None, system="Linux")
    assert lines[0].split()[1] == "NOT_RUNNING"
    assert len(lines) == 4


# ── round 2, class 2: identity = interpreter + launched script, element-level ─────────────────

def _owner_verdict(tool, home, owner_argv):
    argv = dict(ARGV)
    argv[OWNER_PID] = owner_argv
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts(), argv=argv), system="Linux")
    return lines[0].split()[1], code


def test_editor_with_the_script_name_in_argv_is_not_the_owner(tool, home):
    """`vim engramd_owner.py` on a recycled pid is UNVERIFIED, never FRESH.
    Mutation: revert to a substring match on the joined argv -> this cell reddens."""
    assert _owner_verdict(tool, home, ["vim", "engramd_owner.py"]) == ("UNVERIFIED", 3)
    assert _owner_verdict(tool, home, ["tail", "-f", "/p/engram-surface-daemon.log"])[0] == "UNVERIFIED"


def test_tail_of_the_daemon_log_is_not_the_surface_daemon(tool, home):
    argv = dict(ARGV)
    argv[DAEMON_PID] = ["tail", "-f", "/p/engram-surface-daemon.py"]
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts(), argv=argv), system="Linux")
    assert _verdicts(lines) == ["FRESH", "UNVERIFIED", "FRESH"]
    assert code == 3


def test_python_running_the_owner_script_is_the_owner(tool, home):
    assert _owner_verdict(tool, home, ["python3", "/x/engramd_owner.py"]) == ("FRESH", 0)


def test_other_script_ending_in_the_owner_name_is_not_the_owner(tool, home):
    assert _owner_verdict(tool, home, ["python3", "/x/not_engramd_owner.py"]) == ("UNVERIFIED", 3)


def test_versioned_interpreter_running_server_py_is_the_mcp_server(tool, home):
    argv = dict(ARGV)
    argv[MCP_PID] = ["/usr/bin/python3.12", "/p/server.py"]
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts(), argv=argv), system="Linux")
    assert _verdicts(lines) == ["FRESH"] * 3 and code == 0


def test_server_py_backup_is_not_the_mcp_server(tool, home):
    argv = dict(ARGV)
    argv[MCP_PID] = ["python3", "/p/other/server.py.bak"]
    lines, code = tool.run_check(home, False, _readers(tool, _fresh_starts(), argv=argv), system="Linux")
    assert _verdicts(lines) == ["FRESH", "FRESH", "UNVERIFIED"]
    assert code == 3


def test_identity_is_exact_no_python_flags_between_interpreter_and_script(tool):
    """None of the three launchers passes python flags, so the rule stays exact."""
    assert tool.identity_ok(["python3", "-u", "/p/server.py"], "server.py") is False
    assert tool.identity_ok(["python3"], "server.py") is False
    assert tool.identity_ok([], "server.py") is False
    assert tool.identity_ok(["/v/bin/python3", "/p/server.py"], "server.py") is True
    assert tool.identity_ok(["/v/bin/pythonw", "/p/server.py"], "server.py") is False


def test_real_argv_shapes_are_all_ok(tool):
    for pid, script in ((OWNER_PID, "engramd_owner.py"), (DAEMON_PID, "engram-surface-daemon.py"),
                        (MCP_PID, "server.py")):
        assert tool.identity_ok(ARGV[pid], script) is True


@pytest.mark.skipif(not os.path.isfile("/proc/self/cmdline"), reason="needs /proc")
def test_real_argv_reader_returns_a_list_split_on_nul(tool):
    argv = tool.real_readers().argv(os.getpid())
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
    assert os.path.basename(argv[0]).startswith(("python", "pytest")) or len(argv) >= 1


# ── round 2, class 1: every exit that is not a verdict is exit 4 ──────────────────────────────

def _flat_layout(tmp_path, with_owner_read=True):
    """The deployed (flattened) layout: engram_regular_io.py at the root, tools/ beside it."""
    root = tmp_path / "plugin"
    (root / "tools").mkdir(parents=True)
    (root / "engram_regular_io.py").write_text((_SRC_ENGRAM / "engram_regular_io.py").read_text())
    (root / "tools" / "__init__.py").write_text("")
    (root / "tools" / "check_stale_processes.py").write_text(_TOOL_PATH.read_text())
    if with_owner_read:
        (root / "tools" / "_owner_read.py").write_text(
            (_SRC_ENGRAM / "tools" / "_owner_read.py").read_text())
    return root


def _run_flat(root, *args, home=None):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    if home is not None:
        env["ENGRAM_HOME"] = str(home)
    return subprocess.run([sys.executable, str(root / "tools" / "check_stale_processes.py"), *args],
                          env=env, cwd=str(root.parent), capture_output=True, text=True, timeout=15)


def test_flattened_layout_runs(tmp_path):
    """The deployed layout works at all (the baseline for the failing-import cell below)."""
    root = _flat_layout(tmp_path)
    r = _run_flat(root, home=tmp_path / "empty-home")
    assert r.returncode == 2, r.stderr
    assert len(r.stdout.splitlines()) == 4


def test_failing_owner_read_import_is_exit_4_not_1(tmp_path):
    """A broken or partial install (tools/_owner_read.py missing) must not traceback to exit 1,
    which the upgrade skill reads as STALE.  Mutation: put the import back unguarded -> rc 1."""
    root = _flat_layout(tmp_path, with_owner_read=False)
    r = _run_flat(root, home=tmp_path / "h")
    assert r.returncode == 4, (r.returncode, r.stderr)
    assert "Traceback" not in r.stderr
    assert "_owner_read" in r.stderr


def test_unknown_flag_is_exit_4_not_2(tmp_path):
    r = _run_flat(_flat_layout(tmp_path), "--bogus", home=tmp_path / "h")
    assert r.returncode == 4
    assert "usage" in r.stderr.lower() and "--bogus" in r.stderr


def test_help_stays_exit_0(tmp_path):
    r = _run_flat(_flat_layout(tmp_path), "--help", home=tmp_path / "h")
    assert r.returncode == 0
    assert "--informational" in r.stdout


def test_print_failure_is_exit_4_not_1(tool, home, monkeypatch):
    class Broken:
        def write(self, *_a):
            raise BrokenPipeError("pipe closed")

        def flush(self):
            raise BrokenPipeError("pipe closed")

    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.setattr(sys, "stdout", Broken())
    assert tool.main([]) == 4


# ── round 3: macOS interpreter basename, and SystemExit from a helper ─────────────────────────

_MAC_PYTHON = "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python"


def test_macos_framework_python_basename_is_the_interpreter(tool):
    """macOS framework/Homebrew Python execs .../MacOS/Python (capital P).
    Mutation: drop re.IGNORECASE -> the first two asserts redden, the rejections stay green."""
    assert tool.identity_ok([_MAC_PYTHON, "/p/engramd_owner.py"], "engramd_owner.py") is True
    assert tool.identity_ok(["/usr/bin/PYTHON3", "/p/server.py"], "server.py") is True
    assert tool.identity_ok(["/usr/bin/pypy3", "/p/server.py"], "server.py") is False
    assert tool.identity_ok(["/usr/bin/Pythonista", "/p/server.py"], "server.py") is False


def test_macos_python_basename_owner_reads_fresh(tool, home):
    assert _owner_verdict(tool, home, [_MAC_PYTHON, "/p/engramd_owner.py"]) == ("FRESH", 0)


@pytest.mark.parametrize("target,code", [("tools/_owner_read.py", 1), ("engram_regular_io.py", 2)])
def test_helper_calling_sys_exit_at_import_is_exit_4(tmp_path, target, code):
    """A helper's SystemExit(1) would read as STALE and SystemExit(2) as NO_DEPLOY.
    Mutation: `except Exception` at that import -> rc is the helper's own code."""
    root = _flat_layout(tmp_path)
    (root / target).write_text("import sys\nsys.exit(%d)\n" % code)
    r = _run_flat(root, home=tmp_path / "h")
    assert r.returncode == 4, (r.returncode, r.stderr)
    assert "Traceback" not in r.stderr


def test_system_exit_inside_main_is_exit_4(tool, home, monkeypatch):
    def boom():
        raise SystemExit(2)

    monkeypatch.setattr(tool.owner_read, "engram_home", boom)
    assert tool.main([]) == 4


# ── round 4: printed restart commands name the DEPLOYED plugin and the host's own service manager ──

def _stale_all():
    t = DEPLOYED_EPOCH - 3600
    return {OWNER_PID: t, DAEMON_PID: t, MCP_PID: t}


def _fake_root(base, marker="tools/check_stale_processes.py"):
    root = base / "deployed-plugin"
    (root / marker).parent.mkdir(parents=True)
    (root / marker).write_text("")
    return root


def test_printed_paths_use_the_given_root(tool, home, tmp_path):
    """Mutation: derive the root from __file__ again -> this cell reddens."""
    root = tmp_path / "somewhere" / "plugin"
    lines, _ = tool.run_check(home, False, _readers(tool, _stale_all()), root=root, system="Linux")
    assert "%s/tools/setup-surface-daemon-service.sh" % root in lines[0]
    assert "%s/tools/engramd-owner-home-key.sh" % root in lines[0]
    assert "%s/hooks/start-engram-daemon.sh" % root in lines[1]
    assert "CLAUDE_PLUGIN_ROOT=%s" % root in lines[1]
    assert "/src/engram/" not in "\n".join(lines)


def test_linux_lines_unchanged(tool, home, tmp_path):
    root = tmp_path / "plugin"
    lines, _ = tool.run_check(home, False, _readers(tool, _stale_all()), root=root, system="Linux")
    assert "&& systemctl --user restart \"engramd-owner-$(bash " in lines[0]
    # #4305: the unit is keyed on the home by the ONE helper, called in the printed command
    assert ("systemctl --user restart \"engram-surface-daemon-$(bash %s/tools/engramd-owner-home-key.sh %s).service\""
            " (if it runs under a unit); else kill -TERM %d" % (root, home, DAEMON_PID)) in lines[1]
    assert "launchctl" not in "\n".join(lines)


def test_darwin_lines_have_no_systemctl(tool, home, tmp_path):
    root = tmp_path / "plugin"
    lines, _ = tool.run_check(home, False, _readers(tool, _stale_all()), root=root, system="Darwin")
    assert "systemctl" not in "\n".join(lines)
    assert "setup-surface-daemon-service.sh --owner" in lines[0]
    assert "&&" not in lines[0]
    assert ("launchctl kickstart -k gui/$(id -u)/com.engram.surface-daemon.$(bash %s/tools/engramd-owner-home-key.sh %s)"
            % (root, home)) in lines[1]
    assert "kill -TERM %d" % DAEMON_PID in lines[1]
    assert ("CLAUDE_PLUGIN_ROOT=%s bash %s/hooks/start-engram-daemon.sh </dev/null" % (root, root)) in lines[1]


def test_root_from_claude_plugin_root_env(tool, tmp_path):
    root = _fake_root(tmp_path / "a")
    assert tool.resolve_plugin_root(tmp_path / "eh", {"CLAUDE_PLUGIN_ROOT": str(root)}) == root
    root2 = _fake_root(tmp_path / "b", "hooks/start-engram-daemon.sh")
    assert tool.resolve_plugin_root(tmp_path / "eh", {"CLAUDE_PLUGIN_ROOT": str(root2)}) == root2


def test_root_env_without_marker_falls_back_to_engram_home(tool, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    eh = tmp_path / "eh"
    want = eh / "marketplace" / "plugins" / "engram"
    assert tool.resolve_plugin_root(eh, {"CLAUDE_PLUGIN_ROOT": str(bare)}) == want
    assert tool.resolve_plugin_root(eh, {}) == want


def test_source_tree_run_never_prints_src_engram(tmp_path):
    """A real STALE (a python process running a script named engramd_owner.py, deployed_at in the
    future) run from the source tree with a scratch ENGRAM_HOME and no CLAUDE_PLUGIN_ROOT: the
    printed restart line names <ENGRAM_HOME>/marketplace/plugins/engram, never /src/engram/.
    Mutation: root from __file__ -> reddens."""
    eh = tmp_path / "eh"
    eh.mkdir()
    (eh / ".deployed-version").write_text("deployed_at=2999-01-01T00:00:00Z\n")
    script = tmp_path / "engramd_owner.py"
    script.write_text("import time\ntime.sleep(60)\n")
    child = subprocess.Popen([sys.executable, str(script)])
    try:
        (eh / "engramd.pid").write_text("%d\n" % child.pid)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
        env["ENGRAM_HOME"] = str(eh)
        r = subprocess.run([sys.executable, str(_TOOL_PATH), "--informational"], env=env,
                           capture_output=True, text=True, timeout=15)
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0].startswith("owner: STALE"), r.stdout
    assert "/src/engram/" not in r.stdout
    assert str(eh / "marketplace" / "plugins" / "engram" / "tools") in r.stdout
