"""Contract tests for the A6 tool's per-layout child timeout (#3755, part of #2652).

Written BEFORE the change (contract-first). The finding: measure_in_layout ran each layout child under a
fixed `timeout=1800`; the session-start arm on a real-size store cannot finish in that, and the expiry
surfaced as a raw TimeoutExpired traceback (exit 1, no named error).

The contract:
  C1  `--layout-timeout SECONDS` (int > 0), default 1800 (no flag = unchanged); <= 0 or non-int is refused
      at argument parsing (argparse error, exit 2, one usage line + one error line, no traceback).
  C2  the value reaches the child subprocess.run `timeout=` for every hook and both layouts.
  C3  on TimeoutExpired: ONE stderr line naming the layout, the limit and the flag; exit code
      EXIT_LAYOUT_TIMEOUT (5), distinct from 0/1/2/3/4; no traceback.
  C4  C3 is exercised with a REAL expiry (a child that sleeps past a 1 s timeout).

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "engramd_a6_latency.py"
HOOK_NAMES = ("user-identity", "stop", "tripwire", "session-start")


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("engramd_a6_latency_timeout_3755", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_everything_but_run(tool, monkeypatch, run):
    """Stub the build and the post-run checks so main() reaches the child-run seam (_run_in_own_group) only."""
    def fake_build(out_dir):
        src = tool.source_layout()
        return tool.Layout("installed", src.root, src.stop_hook, src.user_hook,
                           src.tripwire_hook, src.session_start_hook)
    monkeypatch.setattr(tool, "build_installed_layout", fake_build)
    monkeypatch.setattr(tool, "check_installed_run", lambda result, layout: None)
    monkeypatch.setattr(tool, "format_layouts", lambda results: "")
    monkeypatch.setattr(tool, "u8_contract_violations", lambda r: [])
    monkeypatch.setattr(tool, "ss_contract_violations", lambda r: [])
    monkeypatch.setattr(tool, "_run_in_own_group", run)


def _recording_run(timeouts):
    def run(cmd, **kw):
        timeouts.append(kw.get("timeout"))
        return subprocess.CompletedProcess(cmd, 0, stdout='{"answers_match": true}\n', stderr="")
    return run


def _sleeping_run(real_run):
    """A _run_in_own_group that swaps the child command for a 5 s sleep but keeps the caller's timeout."""
    def run(cmd, **kw):
        return real_run([sys.executable, "-c", "import time; time.sleep(5)"], **kw)
    return run


def test_default_is_1800_and_reaches_child_for_every_hook_and_both_layouts(tool, monkeypatch):
    """C1+C2 default. Mutation: change the default, or ignore the option and hardcode a number."""
    for hook in HOOK_NAMES:
        timeouts: list = []
        _stub_everything_but_run(tool, monkeypatch, _recording_run(timeouts))
        assert tool.main(["--hook", hook, "--layout", "both"]) == 0
        assert timeouts == [1800, 1800], (hook, timeouts)


def test_custom_value_reaches_child_for_every_hook_and_both_layouts(tool, monkeypatch):
    """C2. Mutation: hardcode 1800 again (default test stays green, this one goes red)."""
    for hook in HOOK_NAMES:
        timeouts: list = []
        _stub_everything_but_run(tool, monkeypatch, _recording_run(timeouts))
        assert tool.main(["--hook", hook, "--layout", "both", "--layout-timeout", "4321"]) == 0
        assert timeouts == [4321, 4321], (hook, timeouts)


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "1.5", ""])
def test_bad_layout_timeout_refused_at_parse(tool, monkeypatch, capsys, bad):
    """C1. Mutation: drop the > 0 check (0 / -5 would run), or accept floats."""
    ran: list = []
    _stub_everything_but_run(tool, monkeypatch, _recording_run(ran))
    with pytest.raises(SystemExit) as ei:
        tool.main(["--layout", "source", "--layout-timeout", bad])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "--layout-timeout" in err and "Traceback" not in err
    assert ran == []


def test_exit_code_constant_is_new_and_distinct(tool):
    """C3. Mutation: reuse 1, 2, 3 or 4."""
    assert tool.EXIT_LAYOUT_TIMEOUT not in (0, 1, 2, 3, 4)


def test_timeout_expiry_is_one_named_line_and_its_own_exit_code(tool, monkeypatch, capsys):
    """C3+C4, a REAL expiry (a child sleeping 5 s against a 1 s limit). Mutation: re-raise TimeoutExpired
    (traceback, no exit code) or return an existing exit code."""
    _stub_everything_but_run(tool, monkeypatch, _sleeping_run(tool._run_in_own_group))
    rc = tool.main(["--layout", "installed", "--layout-timeout", "1"])
    assert rc == tool.EXIT_LAYOUT_TIMEOUT
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    assert "installed" in lines[0] and "1s" in lines[0] and "--layout-timeout" in lines[0]
    assert "Traceback" not in err


def test_timeout_names_the_layout_that_expired(tool, monkeypatch, capsys):
    """C3. Mutation: hardcode the layout name in the message."""
    _stub_everything_but_run(tool, monkeypatch, _sleeping_run(tool._run_in_own_group))
    assert tool.main(["--layout", "source", "--layout-timeout", "1"]) == tool.EXIT_LAYOUT_TIMEOUT
    assert "layout 'source'" in capsys.readouterr().err


def test_flag_is_documented_in_module_docstring(tool):
    """C5. Mutation: omit the flag from the usage text."""
    assert "--layout-timeout" in tool.__doc__


def test_build_step_timeout_is_not_blamed_on_layout_timeout(tool, monkeypatch, capsys):
    """C6. The installed-layout BUILD has its own fixed 600 s limit; its TimeoutExpired must not be reported
    as a --layout-timeout overrun (advice that cannot help). One stderr line naming the build step and 600 s,
    exit 4 (no number produced, like the build's LayoutViolation), no traceback. Mutation: put the build back
    inside the layout-timeout handler (this goes red, the child-run cell above stays green)."""
    def fake_build(out_dir):
        raise subprocess.TimeoutExpired("bash build-plugin.sh", 600)
    _stub_everything_but_run(tool, monkeypatch, _recording_run([]))
    monkeypatch.setattr(tool, "build_installed_layout", fake_build)
    rc = tool.main(["--layout", "installed", "--layout-timeout", "7"])
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert rc == 4 and rc != tool.EXIT_LAYOUT_TIMEOUT
    assert len(lines) == 1, lines
    assert "build" in lines[0] and "600" in lines[0]
    assert "--layout-timeout" not in err and "Traceback" not in err


# ---- C7 / C8: no descendant of the layout child outlives a timeout (the orphaned engramd owner) ----------
import json
import os
import time

_FAKE_CHILD = '''
import json, os, subprocess, sys, time
gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
with open(os.environ["A6_GC_PIDFILE"], "w") as f:
    f.write(str(gc.pid))
if os.environ.get("A6_FAKE_MODE") == "ok":
    print(json.dumps({"answers_match": True}))
    sys.exit(0)
time.sleep(120)
'''


def _alive(pid: int) -> bool:
    """True while the pid is a live (non-zombie) process; an orphan reaped late by init shows as Z.
    /proc where it exists (Linux), else `ps -o stat=` (macOS has no /proc)."""
    if os.path.isdir("/proc/self"):
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _wait_gone(pid: int, secs: float = 5.0) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


@pytest.fixture
def fake_child(tool, tmp_path, monkeypatch):
    """Make measure_in_layout's child a script that spawns a long-sleeping grandchild (pid recorded)."""
    script = tmp_path / "fake_child.py"
    script.write_text(_FAKE_CHILD)
    pidfile = tmp_path / "gc.pid"
    monkeypatch.setattr(tool, "__file__", str(script))
    monkeypatch.setenv("A6_GC_PIDFILE", str(pidfile))
    yield pidfile
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text()), 9)
        except (ProcessLookupError, ValueError):
            pass


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_timeout_leaves_no_descendant_of_the_layout_child(tool, fake_child, monkeypatch):
    """C7, a REAL child + grandchild + 1 s limit. The grandchild (the engramd owner in production) must be
    gone shortly after the timeout. Mutation: kill only the direct child (subprocess.run's own behaviour) --
    the grandchild survives and this goes red; the other cells stay green."""
    monkeypatch.setenv("A6_FAKE_MODE", "hang")
    with pytest.raises(subprocess.TimeoutExpired):
        tool.measure_in_layout(tool.source_layout(), layout_timeout=1)
    gc = int(fake_child.read_text())
    assert _wait_gone(gc), f"grandchild {gc} survived the layout timeout"


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_main_timeout_path_leaves_no_descendant_and_still_exits_5(tool, fake_child, monkeypatch, capsys):
    """C7 through main(): exit 5 and one line as before, and no survivor. Mutation: as above."""
    monkeypatch.setenv("A6_FAKE_MODE", "hang")
    monkeypatch.setattr(tool, "check_installed_run", lambda result, layout: None)
    assert tool.main(["--layout", "source", "--layout-timeout", "1"]) == tool.EXIT_LAYOUT_TIMEOUT
    assert _wait_gone(int(fake_child.read_text()))


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_success_path_returns_result_and_kills_nothing(tool, fake_child, monkeypatch):
    """C8. A completing child returns its parsed result and the group handling signals nobody: a descendant
    the child left running is still alive afterwards. Mutation: kill the group on success too (this goes red,
    the C7 cells stay green)."""
    monkeypatch.setenv("A6_FAKE_MODE", "ok")
    result = tool.measure_in_layout(tool.source_layout(), layout_timeout=30)
    assert result["answers_match"] is True and result["layout"] == "source"
    gc = int(fake_child.read_text())
    # A group kill on the success path lands a moment after the call returns, so a single immediate
    # alive-check passes under that mutation. Require the descendant alive throughout a settle window.
    end = time.time() + 1.0
    while time.time() < end:
        assert _alive(gc), f"descendant {gc} was killed on the success path"
        time.sleep(0.05)


# ---- _alive works without /proc (the macOS branch, exercised on Linux) ----------------------------------
def _no_proc(monkeypatch):
    real_isdir = os.path.isdir
    monkeypatch.setattr(os.path, "isdir", lambda p: False if str(p) == "/proc/self" else real_isdir(p))


def test_alive_ps_branch_reads_live_zombie_and_reaped(monkeypatch):
    """The ps branch of the test helper _alive (macOS has no /proc). Forces the non-/proc path and spies
    that ps is really consulted. Live sleeper -> alive; unreaped zombie -> not alive; reaped -> not alive.
    Mutation: ps branch ignores the Z state (the zombie assertion goes red); /proc used unconditionally
    (the ps-was-called assertion goes red)."""
    _no_proc(monkeypatch)
    real_run = subprocess.run
    ps_calls: list = []

    def spy(cmd, *a, **kw):
        if cmd and cmd[0] == "ps":
            ps_calls.append(cmd)
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "run", spy)
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert _alive(p.pid) is True
        assert ps_calls, "the ps branch was never taken"
        p.kill()
        end = time.time() + 5
        while time.time() < end:   # wait for the Z state WITHOUT reaping (no p.wait / poll)
            out = real_run(["ps", "-o", "stat=", "-p", str(p.pid)], capture_output=True, text=True).stdout
            if out.strip().startswith("Z"):
                break
            time.sleep(0.02)
        assert _alive(p.pid) is False, "an unreaped zombie must read as not alive"
        p.wait()
        assert _alive(p.pid) is False
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


# ---- C9: _kill_group survives EPERM (Darwin: killpg on a zombie-only group) -----------------------------
_PLAIN_SLEEP = "import time; time.sleep(30)"
_IGNORE_TERM = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"


def _reaped(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return False


def _killpg_raising_on(monkeypatch, tool, raise_on: int, exc: BaseException, pids: list):
    """os.killpg that really delivers the signal, then raises `exc` when the signal is `raise_on`."""
    real = os.killpg

    def fake(pgid, sig):
        pids.append(pgid)
        real(pgid, sig)
        if sig == raise_on:
            raise exc
    monkeypatch.setattr(tool.os, "killpg", fake)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
@pytest.mark.parametrize("raise_on", [15, 9])  # EPERM on SIGTERM (child dies of it) / on SIGKILL (child ignored TERM)
def test_permission_error_from_killpg_still_gives_timeout_expired(tool, monkeypatch, raise_on):
    """C9. killpg raising PermissionError (Darwin, zombie-only group) on SIGTERM or on SIGKILL is treated
    like ProcessLookupError: the caller still gets TimeoutExpired, the child is reaped, and it does not
    hang past grace + margin. Mutation: drop PermissionError from the except tuple (this goes red)."""
    monkeypatch.setattr(tool, "_KILL_GRACE_S", 0.3)
    pids: list = []
    _killpg_raising_on(monkeypatch, tool, raise_on, PermissionError(1, "Operation not permitted"), pids)
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        tool._run_in_own_group([sys.executable, "-c", _IGNORE_TERM if raise_on == 9 else _PLAIN_SLEEP], timeout=1)
    assert time.time() - t0 < 1 + 0.3 + 3, "hung past timeout + grace + margin"
    assert pids and _reaped(pids[0])


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_permission_error_through_main_is_exit_5_one_line(tool, monkeypatch, capsys):
    """C9 through main(): exit 5, one stderr line, no traceback. Mutation: as above."""
    monkeypatch.setattr(tool, "_KILL_GRACE_S", 0.3)
    _stub_everything_but_run(tool, monkeypatch, _sleeping_run(tool._run_in_own_group))
    _killpg_raising_on(monkeypatch, tool, 15, PermissionError(1, "Operation not permitted"), [])
    assert tool.main(["--layout", "source", "--layout-timeout", "1"]) == tool.EXIT_LAYOUT_TIMEOUT
    err = capsys.readouterr().err
    assert len([ln for ln in err.splitlines() if ln.strip()]) == 1 and "Traceback" not in err


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_other_oserror_from_killpg_still_propagates(tool, monkeypatch):
    """C9 bound: only ProcessLookupError and PermissionError are absorbed; any other OSError propagates.
    Mutation: broaden the except to bare OSError (this goes red)."""
    import errno
    monkeypatch.setattr(tool, "_KILL_GRACE_S", 0.3)
    pids: list = []
    _killpg_raising_on(monkeypatch, tool, 15, OSError(errno.EIO, "boom"), pids)
    try:
        with pytest.raises(OSError) as ei:
            tool._run_in_own_group([sys.executable, "-c", _PLAIN_SLEEP], timeout=1)
        assert not isinstance(ei.value, (subprocess.TimeoutExpired, PermissionError, ProcessLookupError))
    finally:
        for pid in pids[:1]:
            try:
                os.killpg(pid, 9)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError, PermissionError):
                pass
