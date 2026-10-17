"""monitor-supervisor.py and codex-umwelt-wake.py on a non-regular pidfile / status
file -- issue #3773 (follow-up to the owner's #3768).

Contract (the rulings on #3773), asserted here BEFORE the tools were changed:

  READS never hang. A FIFO (or any non-regular file) at the supervisor's pidfile
  or status file, or at codex's pidfile or status.json, makes the read say what
  the file is in ONE line and carry on as "unknown": the supervisor's reap kills
  nothing; `run_check` returns 2 ("not armed"); a non-regular status file is
  "unknown" (FAIL lines carrying the kind); codex `reap_prior` kills nothing,
  `remove_pid` leaves the path alone, `current_status` keeps the SAME JSON keys
  as for an absent file, `cmd_stop` says "pidfile is a <kind>, not a regular
  file" (never "stale", never "no pidfile") and returns 1.

  WRITES go through engram_regular_io.write_pidfile. The SUPERVISOR refuses to
  start with exit 4 (EXIT_PIDFILE_NOT_REGULAR) before any child is spawned. The
  CODEX watcher NEVER exits on a refused pidfile: its unit is Restart=always, so
  an exit would be relaunched every few seconds. It reports the refusal ONCE and
  idles, rechecking every CODEX_UMWELT_WAKE_REFUSAL_RECHECK_S (default 60), and
  starts normally the moment the path becomes acceptable.

Every call that can hang runs in a CHILD python (`-I`) under a hard kill bound;
a timeout is pytest.fail("HUNG"), never a masked traceback. Each cell's
docstring names the mutation that must redden it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
TOOLS = SRC_ENGRAM / "tools"
SUP = TOOLS / "monitor-supervisor.py"
WAKE = TOOLS / "codex-umwelt-wake.py"
HELPER = SRC_ENGRAM / "engram_regular_io.py"
BOUND_S = 5.0
KINDS = ["fifo", "symlink", "hardlink", "dir", "sock"]
KIND_WORD = {"fifo": "FIFO", "symlink": "symlink", "hardlink": "2 hard links",
             "dir": "directory", "sock": "socket"}


def _load(path, name):
    if str(SRC_ENGRAM) not in sys.path:
        sys.path.append(str(SRC_ENGRAM))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sup():
    return _load(SUP, "msup_3773")


@pytest.fixture(scope="module")
def wake():
    return _load(WAKE, "cwake_3773")


@pytest.fixture
def work(tmp_path_factory):
    d = tmp_path_factory.mktemp("w")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def plant(kind, path, sentinel=None):
    path = str(path)
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        os.symlink(sentinel, path)
    elif kind == "hardlink":
        os.link(sentinel, path)
    elif kind == "dir":
        os.mkdir(path)
    elif kind == "sock":
        sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sk.bind(path)
        sk.close()


def make_sentinel(work):
    s = work / "SENTINEL"
    s.write_bytes(b"x" * 135)
    return s


def py(args, *, env=None, cwd=None, timeout=BOUND_S):
    """Run a python child (-I) under the kill bound; HUNG is a failure."""
    try:
        return subprocess.run([sys.executable, "-I", *map(str, args)], capture_output=True,
                              text=True, timeout=timeout, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        pytest.fail(f"HUNG: no return within {timeout}s")


_CHILD_PRELUDE = r"""
import importlib.util, json, os, sys
from pathlib import Path
sys.path.append(os.path.dirname(os.path.dirname(sys.argv[1])))  # <repo>/src/engram
def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m
m = load(sys.argv[1], "tool_under_test")
tmp = sys.argv[2]
"""


def child(tool, work, body, *, timeout=BOUND_S):
    code = _CHILD_PRELUDE + body + "\nprint('RESULT:' + json.dumps(result))\n"
    done = py(["-c", code, tool, work], timeout=timeout)
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT:")]
    assert done.returncode == 0 and lines, (done.returncode, done.stdout, done.stderr)
    out = json.loads(lines[-1][len("RESULT:"):])
    out["_stderr"] = done.stderr
    return out


# =====================================================================
# SUPERVISOR -- reads
# =====================================================================

def test_supervisor_reap_with_a_fifo_pidfile_returns_and_kills_nothing(work):
    """Mutation: reap reads with Path.read_text (hangs on the FIFO), or kills."""
    os.mkfifo(work / "p.pid")
    r = child(SUP, work, r"""
calls = []
m.os.killpg = lambda *a: calls.append(("killpg", a))
m.os.kill = lambda *a: calls.append(("kill", a))
m.reap_stale_supervisor(os.path.join(tmp, "p.pid"))
result = {"calls": calls}
""")
    assert r["calls"] == []
    lines = r["_stderr"].strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("[supervisor] pidfile ") and "is a FIFO, not a regular file; not reaping" in lines[0]


def test_supervisor_reap_regular_controls_behave_as_before(sup, work, monkeypatch, capsys):
    """Absent, garbage, a dead pid, and a LIVE non-supervisor pid: no SIGKILL
    in any of them (the live one is spared by the cmdline identity check)."""
    calls = []
    monkeypatch.setattr(sup.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    p = work / "p.pid"
    sup.reap_stale_supervisor(str(p))                       # absent
    p.write_text("not-a-pid")
    sup.reap_stale_supervisor(str(p))                       # garbage
    assert calls == [] and capsys.readouterr().err == ""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    p.write_text(str(dead.pid))
    monkeypatch.setattr(sup.os, "killpg", lambda pid, sig: (calls.append((pid, sig)), (_ for _ in ()).throw(ProcessLookupError()))[1])
    sup.reap_stale_supervisor(str(p))                       # stale: probe only
    assert calls == [(dead.pid, 0)]
    calls.clear()
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        p.write_text(str(live.pid))
        monkeypatch.setattr(sup.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
        sup.reap_stale_supervisor(str(p))                   # live, not a supervisor
        assert calls == [(live.pid, 0)]                     # the probe, never SIGKILL
        assert "NOT reaping" in capsys.readouterr().err
        assert live.poll() is None
    finally:
        live.kill()
        live.wait()


def test_supervisor_run_check_fifo_pidfile_is_not_armed_in_one_line(work):
    """rc 2, ONE stdout line naming the kind. Mutation: Path.read_text (hang),
    or the generic OSError text without the kind."""
    os.mkfifo(work / "p.pid")
    r = child(SUP, work, r"""
rc = m.run_check(os.path.join(tmp, "p.pid"), os.path.join(tmp, "status.json"), None)
result = {"rc": rc}
""")
    assert r["rc"] == 2
    # the child printed the line before RESULT; run the whole command for the exact stdout
    done = py([SUP, "--check", "--pidfile", work / "p.pid", "--status-file", work / "s.json"])
    assert done.returncode == 2
    out = done.stdout.strip().splitlines()
    assert len(out) == 1
    assert out[0].startswith("CHECK FAIL supervisor not armed: pidfile ")
    assert "is a FIFO, not a regular file" in out[0]


def test_supervisor_run_check_fifo_status_file_is_unknown_and_named(work):
    """A FIFO at the STATUS file (pidfile regular): rc 1, FAIL lines carrying the
    kind. Mutation: _load_status reads with Path.read_text (hang) or reports
    'missing/stale (OSError)' without the kind."""
    (work / "p.pid").write_text(f"{os.getpid()}")
    os.mkfifo(work / "status.json")
    done = py([SUP, "--check", "--pidfile", work / "p.pid", "--status-file", work / "status.json"])
    assert done.returncode == 1
    lines = done.stdout.strip().splitlines()
    assert len(lines) == 5
    child_lines = [ln for ln in lines if ln.split(":")[0] in ("updates", "mention", "bell")]
    assert len(child_lines) == 3
    for ln in child_lines:
        assert "FAIL" in ln and "status file is a FIFO, not a regular file" in ln


def test_supervisor_check_regular_controls(work):
    """Absent pidfile -> rc 2 'not armed' with the exception text, garbage -> rc 2;
    a regular status file that is garbage stays 'missing/stale (...)'."""
    done = py([SUP, "--check", "--pidfile", work / "none.pid", "--status-file", work / "s.json"])
    assert done.returncode == 2 and "not armed" in done.stdout and "FileNotFoundError" in done.stdout
    (work / "g.pid").write_text("garbage")
    done = py([SUP, "--check", "--pidfile", work / "g.pid", "--status-file", work / "s.json"])
    assert done.returncode == 2 and "ValueError" in done.stdout
    (work / "p.pid").write_text(f"{os.getpid()}")
    (work / "bad.json").write_text("{nope")
    done = py([SUP, "--check", "--pidfile", work / "p.pid", "--status-file", work / "bad.json"])
    assert done.returncode == 1 and "status file missing/stale (JSONDecodeError)" in done.stdout
    assert "is a " not in done.stdout.split("CHECK")[0].replace("is a monitor", "")


# =====================================================================
# SUPERVISOR -- the start write
# =====================================================================

def _start_args(work, pidfile):
    marker = work / "CHILD_SPAWNED"
    cmd = json.dumps([sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"])
    return marker, [SUP, "--pidfile", pidfile, "--status-file", work / "status.json",
                    "--bell-cmd-json", cmd, "--updates-cmd-json", cmd, "--mention-cmd-json", cmd]


@pytest.mark.parametrize("kind", KINDS)
def test_supervisor_start_refuses_with_exit_4_and_spawns_nothing(work, kind):
    """Exit 4 within the bound; ONE refusal line naming the kind; the sentinel
    byte-identical; NO child spawned. Mutation: the write goes back to
    write_text (follows the link / hangs), or exits 1, or spawns first."""
    sentinel = make_sentinel(work)
    pidfile = work / "p.pid"
    plant(kind, pidfile, sentinel)
    marker, args = _start_args(work, pidfile)
    done = py(args)
    assert done.returncode == 4, (done.returncode, done.stderr)
    refusal = [ln for ln in done.stderr.splitlines() if "refusing to start" in ln]
    assert len(refusal) == 1
    assert refusal[0].startswith("monitor-supervisor: refusing to start: the pidfile path ")
    assert KIND_WORD[kind] in refusal[0]
    assert "nothing was started. Remove it and start again" in refusal[0]
    assert sentinel.read_bytes() == b"x" * 135
    assert not marker.exists()
    assert "Traceback" not in done.stderr


def test_supervisor_start_documents_exit_4():
    """The module docstring's Exit codes paragraph names 4 and the constant exists."""
    text = SUP.read_text()
    assert "EXIT_PIDFILE_NOT_REGULAR = 4" in text
    exit_para = text.split("Exit codes:")[1].split("See --help")[0]
    assert "4" in exit_para and "pidfile" in exit_para


# =====================================================================
# CODEX -- reads
# =====================================================================

class _Rec:
    def __init__(self):
        self.msgs = []

    def log(self, message):
        self.msgs.append(message)


def test_codex_reap_prior_fifo_kills_nothing_and_logs_one_line(work):
    """Mutation: reap_prior reads with Path.read_text (hang), or kills."""
    os.mkfifo(work / "watcher.pid")
    r = child(WAKE, work, r"""
calls = []
m.os.kill = lambda *a: calls.append(a)
class L:
    msgs = []
    def log(self, s): self.msgs.append(s)
paths = m.build_paths(Path(tmp))
m.reap_prior(paths, L())
result = {"calls": calls, "msgs": L.msgs}
""")
    assert r["calls"] == []
    assert len(r["msgs"]) == 1 and "FIFO" in r["msgs"][0]


def test_codex_reap_prior_fifo_without_a_logger_writes_stderr(work):
    os.mkfifo(work / "watcher.pid")
    r = child(WAKE, work, r"""
m.reap_prior(m.build_paths(Path(tmp)))
result = {}
""")
    lines = r["_stderr"].strip().splitlines()
    assert len(lines) == 1 and "FIFO" in lines[0]


def test_codex_remove_pid_leaves_a_fifo_alone(work):
    """Mutation: remove_pid reads with Path.read_text (hang) or unlinks."""
    os.mkfifo(work / "watcher.pid")
    r = child(WAKE, work, r"""
m.remove_pid(m.build_paths(Path(tmp)))
import stat
result = {"fifo": stat.S_ISFIFO(os.lstat(os.path.join(tmp, "watcher.pid")).st_mode)}
""")
    assert r["fifo"] is True


def test_codex_remove_pid_regular_controls(wake, work):
    paths = wake.build_paths(work)
    paths.pid_path.write_text(f"{os.getpid()}\n")
    wake.remove_pid(paths)
    assert not paths.pid_path.exists()                      # ours: removed
    paths.pid_path.write_text(f"{os.getpid() + 1}\n")
    wake.remove_pid(paths)
    assert paths.pid_path.exists()                          # someone else's: kept
    paths.pid_path.write_text("garbage")
    wake.remove_pid(paths)
    assert paths.pid_path.exists()
    paths.pid_path.unlink()
    wake.remove_pid(paths)                                  # absent: no exception


_STATUS_BODY = r"""
import io, contextlib
absent_dir = Path(tmp) / "absent"; absent_dir.mkdir()
absent = m.current_status(m.build_paths(absent_dir))
got = m.current_status(m.build_paths(Path(tmp)))
result = {"keys_equal": sorted(got) == sorted(absent), "pid": got["pid"], "running": got["running"]}
"""


def test_codex_current_status_fifo_pidfile(work):
    """pid None, running False, same keys as an absent file, ONE stderr line naming
    the kind. Mutation: pidfile read with Path.read_text (hang)."""
    os.mkfifo(work / "watcher.pid")
    r = child(WAKE, work, _STATUS_BODY)
    assert r["keys_equal"] and r["pid"] is None and r["running"] is False
    lines = r["_stderr"].strip().splitlines()
    assert len(lines) == 1 and "FIFO" in lines[0]


def test_codex_current_status_fifo_status_json(work):
    """A FIFO at status.json: the default status dict, ONE stderr line naming the
    kind. Mutation: load_json on the status path (hang)."""
    os.mkfifo(work / "status.json")
    r = child(WAKE, work, _STATUS_BODY)
    assert r["keys_equal"] and r["pid"] is None and r["running"] is False
    lines = r["_stderr"].strip().splitlines()
    assert len(lines) == 1 and "FIFO" in lines[0]


def test_codex_current_status_regular_controls(wake, work):
    paths = wake.build_paths(work)
    absent = wake.current_status(paths)
    paths.pid_path.write_text(f"{os.getpid()}\n")
    paths.status_path.write_text(json.dumps({"mode": "idle", "extra": 1}))
    got = wake.current_status(paths)
    assert got["mode"] == "idle" and got["extra"] == 1       # the status file is used
    assert got["running"] is False and got["pid"] is None    # this pytest is not the watcher
    paths.pid_path.write_text("garbage")
    paths.status_path.write_text("{nope")
    bad = wake.current_status(paths)
    assert sorted(bad) == sorted(absent) and bad["running"] is False


def test_codex_cmd_stop_fifo_names_the_kind(work):
    """rc 1; stderr names the kind; says neither 'stale' nor 'no pidfile'.
    Mutation: Path.read_text (hang), or falling into the 'no pidfile' branch."""
    os.mkfifo(work / "watcher.pid")
    done = py([WAKE, "stop", "--state-dir", work])
    assert done.returncode == 1
    assert "pidfile is a FIFO, not a regular file" in done.stderr
    assert "stale" not in done.stderr and "no pidfile" not in done.stderr


def test_codex_cmd_stop_regular_controls(work):
    done = py([WAKE, "stop", "--state-dir", work])
    assert done.returncode == 1 and "no pidfile" in done.stderr
    (work / "watcher.pid").write_text(f"{os.getpid() + 7}\n")
    done = py([WAKE, "stop", "--state-dir", work])
    assert done.returncode == 1 and "stale" in done.stderr


def test_codex_whole_status_command_with_a_fifo_pidfile(work):
    """rc 0, JSON with the same keys as for an empty state dir."""
    empty = work / "empty"
    empty.mkdir()
    base = py([WAKE, "status", "--state-dir", empty])
    os.mkfifo(work / "watcher.pid")
    done = py([WAKE, "status", "--state-dir", work])
    assert done.returncode == 0
    assert sorted(json.loads(done.stdout)) == sorted(json.loads(base.stdout))


def test_codex_whole_status_command_with_a_fifo_status_json(work):
    empty = work / "empty"
    empty.mkdir()
    base = py([WAKE, "status", "--state-dir", empty])
    os.mkfifo(work / "status.json")
    done = py([WAKE, "status", "--state-dir", work])
    assert done.returncode == 0
    assert sorted(json.loads(done.stdout)) == sorted(json.loads(base.stdout))


# =====================================================================
# CODEX -- the pidfile write and the idle-on-refusal start
# =====================================================================

@pytest.mark.parametrize("kind", KINDS)
def test_codex_write_pid_raises_not_regular_file_and_leaves_the_sentinel(work, kind):
    """Mutation: write_pid goes back to write_text (follows / hangs)."""
    sentinel = make_sentinel(work)
    plant(kind, work / "watcher.pid", sentinel)
    r = child(WAKE, work, r"""
out = {"exc": None}
try:
    m.write_pid(m.build_paths(Path(tmp)))
except BaseException as exc:
    out = {"exc": type(exc).__name__, "is_nrf": isinstance(exc, m.NotRegularFile), "text": str(exc)}
out["sentinel"] = Path(tmp, "SENTINEL").read_text()
result = out
""")
    assert r["exc"] == "NotRegularFile" and r["is_nrf"]
    assert KIND_WORD[kind] in r["text"]
    assert r["sentinel"] == "x" * 135


def test_codex_write_pid_regular_writes_pid_and_newline(wake, work):
    paths = wake.build_paths(work / "fresh")                # the state dir is created
    wake.write_pid(paths)
    assert paths.pid_path.read_text() == f"{os.getpid()}\n"
    paths.pid_path.write_text("9" * 80)
    wake.write_pid(paths)
    assert paths.pid_path.read_text() == f"{os.getpid()}\n"


def test_wait_for_acceptable_pidfile_refused_n_times_then_accepted(wake, work, monkeypatch, capsys):
    """Refused 3 times, then accepted: 4 attempts, 3 sleeps of the configured
    interval, the refusal logged ONCE and printed ONCE. Mutation: no loop; a
    line per attempt; exiting instead of waiting."""
    monkeypatch.setenv("CODEX_UMWELT_WAKE_REFUSAL_RECHECK_S", "7")
    attempts = []

    def fake_write_pid(paths):
        attempts.append(1)
        if len(attempts) <= 3:
            raise wake.NotRegularFile(str(paths.pid_path), stat.S_IFIFO | 0o600, 1)

    monkeypatch.setattr(wake, "write_pid", fake_write_pid)
    sleeps = []
    log = _Rec()
    wake.wait_for_acceptable_pidfile(wake.build_paths(work), log, sleep=sleeps.append)
    assert len(attempts) == 4 and sleeps == [7.0, 7.0, 7.0]
    assert len(log.msgs) == 1 and "FIFO" in log.msgs[0]
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "refusing to start" in err[0] and "will recheck every 7" in err[0]


def test_wait_for_acceptable_pidfile_default_interval_is_60(wake, work, monkeypatch):
    monkeypatch.delenv("CODEX_UMWELT_WAKE_REFUSAL_RECHECK_S", raising=False)
    n = []

    def fake(paths):
        n.append(1)
        if len(n) == 1:
            raise wake.NotRegularFile("p", stat.S_IFIFO, 1)

    monkeypatch.setattr(wake, "write_pid", fake)
    sleeps = []
    wake.wait_for_acceptable_pidfile(wake.build_paths(work), _Rec(), sleep=sleeps.append)
    assert sleeps == [60.0]


def test_wait_for_acceptable_pidfile_accepted_at_once_is_silent(wake, work, monkeypatch, capsys):
    monkeypatch.setattr(wake, "write_pid", lambda paths: None)
    sleeps, log = [], _Rec()
    wake.wait_for_acceptable_pidfile(wake.build_paths(work), log, sleep=sleeps.append)
    assert sleeps == [] and log.msgs == [] and capsys.readouterr().err == ""


def _run_args(work, extra=()):
    return [WAKE, "run", "--state-dir", work, "--quiet-stdout", "--interval", "0.2",
            "--sessions-dir", work / "sessions", "--umwelt-config", work / "no-umwelt.json",
            "--app-server", "ws://127.0.0.1:1", *extra]


def _env(work, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH", "CODEX_UMWELT_WAKE_NO_REAP", "CODEX_THREAD_ID")}
    env.update({"ENGRAM_HOME": str(work), "CODEX_UMWELT_WAKE_REFUSAL_RECHECK_S": "0.2", **extra})
    return env


def _spawn(work, extra=(), **env_extra):
    return subprocess.Popen([sys.executable, "-I", *map(str, _run_args(work, extra))],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=_env(work, **env_extra))


def _end(proc, sig=signal.SIGTERM, within=2.0):
    """Signal and require a prompt end; returns (seconds, stdout, stderr)."""
    t0 = time.monotonic()
    proc.send_signal(sig)
    try:
        out, err = proc.communicate(timeout=within + 3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"did not end within {within}s of the signal")
    took = time.monotonic() - t0
    assert took < within, took
    return took, out, err


@pytest.mark.parametrize("kind", KINDS)
def test_codex_run_on_a_refused_pidfile_idles_reports_once_and_ends_on_sigterm(work, kind):
    """The process is STILL RUNNING after 2 s (it idles; it never exits), stderr
    holds exactly ONE refusal line however many rechecks happen (>= 4 at 0.2 s),
    the planted path and the sentinel are untouched, the watcher loop was never
    entered (no status.json / state.json), and SIGTERM ends it in < 2 s with no
    traceback. Mutation: exit on refusal; a line per recheck; entering the loop."""
    sentinel = make_sentinel(work)
    planted = work / "watcher.pid"
    plant(kind, planted, sentinel)
    before = os.lstat(planted).st_mode
    proc = _spawn(work)
    try:
        time.sleep(2.0)
        assert proc.poll() is None, "the watcher exited on a refused pidfile"
    except BaseException:
        proc.kill()
        proc.communicate()
        raise
    _took, _out, err = _end(proc)
    assert proc.returncode == -signal.SIGTERM
    assert "Traceback" not in err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    assert lines[0].startswith("codex-umwelt-wake: refusing to start: the pidfile path ")
    assert KIND_WORD[kind] in lines[0] and "will recheck every 0.2s. Remove it to start" in lines[0]
    assert os.lstat(planted).st_mode == before
    assert sentinel.read_bytes() == b"x" * 135
    assert not (work / "status.json").exists() and not (work / "state.json").exists()


def test_codex_run_on_a_refused_pidfile_ends_promptly_on_sigint_without_a_traceback(work):
    """SIGINT while idling: ends in < 2 s, no traceback, planted path untouched.
    Mutation: the KeyboardInterrupt propagates as a traceback."""
    os.mkfifo(work / "watcher.pid")
    proc = _spawn(work)
    time.sleep(1.0)
    assert proc.poll() is None
    _took, _out, err = _end(proc, signal.SIGINT)
    assert "Traceback" not in err and "KeyboardInterrupt" not in err
    assert stat.S_ISFIFO(os.lstat(work / "watcher.pid").st_mode)


@pytest.mark.parametrize("kind", ["fifo", "symlink", "dir"])
def test_codex_run_proceeds_the_moment_the_refused_path_is_removed(work, kind):
    """Removing the planted path while it idles makes it write its OWN pid, then
    SIGTERM ends it. Mutation: idles forever; gives up after the first recheck."""
    sentinel = make_sentinel(work)
    planted = work / "watcher.pid"
    plant(kind, planted, sentinel)
    proc = _spawn(work)
    try:
        time.sleep(1.0)
        assert proc.poll() is None
        if kind == "dir":
            planted.rmdir()
        else:
            planted.unlink()
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if planted.is_file() and planted.read_text() == f"{proc.pid}\n":
                break
            time.sleep(0.1)
        else:
            pytest.fail("the watcher never wrote its pidfile after the path became acceptable")
        assert proc.poll() is None
    except BaseException:
        proc.kill()
        proc.communicate()
        raise
    _end(proc, within=4.0)
    assert sentinel.read_bytes() == b"x" * 135


def test_codex_run_with_a_fifo_at_status_json_never_exits_and_does_not_hang(work):
    """A FIFO planted at <state-dir>/status.json. cmd_run does not READ
    status.json at start; its first cycle WRITES it with atomic_write_json (a
    temp file + os.replace), which REPLACES the FIFO rather than blocking on it.
    So what the code does, asserted exactly: the process starts, is still
    running at the bound, never exits on its own, status.json ends up a regular
    file written by the watcher, and anything printed about the status file is
    at most ONE line and names the kind. This cell is not red on dev (nothing
    reads status.json at start there either); it pins the behaviour so a later
    read at start cannot hang or exit unnoticed."""
    os.mkfifo(work / "status.json")
    proc = _spawn(work)
    try:
        time.sleep(3.0)
        assert proc.poll() is None, "the watcher exited"
    except BaseException:
        proc.kill()
        proc.communicate()
        raise
    _t, _o, err = _end(proc, within=4.0)
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) <= 1 and all("FIFO" in ln for ln in lines)
    assert "Traceback" not in err
    st = os.lstat(work / "status.json")
    assert stat.S_ISREG(st.st_mode), "atomic_write_json should have replaced the FIFO"
    assert json.loads((work / "status.json").read_text())["mode"] in ("stopped", "stopping", "idle", "cycle_failed")


# =====================================================================
# Import resolution: installed layout, source layout, helper absent
# =====================================================================

def _tree(root, tool, layout):
    if layout == "installed":
        helper_dir, tools_dir = root, root / "tools"
    else:
        helper_dir = root / "src" / "engram"
        tools_dir = helper_dir / "tools"
    tools_dir.mkdir(parents=True)
    shutil.copy(HELPER, helper_dir / "engram_regular_io.py")
    shutil.copy(tool, tools_dir / tool.name)
    # The deployed plugin ships the `tools` package and the shared ENGRAM-home resolver beside every
    # tool (monitor-supervisor imports tools._owner_read since #3863); recreate that, not a bare script.
    for sibling in ("__init__.py", "_owner_read.py"):
        shutil.copy(TOOLS / sibling, tools_dir / sibling)
    return tools_dir / tool.name


def _clean_env():
    return {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH")}


@pytest.mark.parametrize("layout", ["installed", "source"])
@pytest.mark.parametrize("which", ["supervisor", "codex"])
def test_fifo_behaviour_survives_the_deployed_layouts(work, layout, which):
    """The helper is found by walking parents with CLAUDE_PLUGIN_ROOT UNSET and
    the cwd elsewhere, in the flattened plugin layout and in the source layout.
    Mutation: a fixed parents[N] (overshoots in one of the two layouts)."""
    root = work / "tree"
    elsewhere = work / "cwd"
    elsewhere.mkdir()
    os.mkfifo(work / "the.pid")
    if which == "supervisor":
        script = _tree(root, SUP, layout)
        done = py([script, "--check", "--pidfile", work / "the.pid", "--status-file", work / "s.json"],
                  env=_clean_env(), cwd=elsewhere)
        assert done.returncode == 2 and "is a FIFO, not a regular file" in done.stdout
    else:
        script = _tree(root, WAKE, layout)
        os.rename(work / "the.pid", work / "watcher.pid")
        done = py([script, "stop", "--state-dir", work], env=_clean_env(), cwd=elsewhere)
        assert done.returncode == 1 and "pidfile is a FIFO, not a regular file" in done.stderr


@pytest.mark.parametrize("tool", [SUP, WAKE], ids=["supervisor", "codex"])
def test_a_missing_helper_is_one_line_exit_1_no_traceback(work, tool):
    """Mutation: the ImportError escapes as a traceback, or exits 0/2."""
    tools_dir = work / "lonely" / "tools"
    tools_dir.mkdir(parents=True)
    shutil.copy(tool, tools_dir / tool.name)
    args = [tools_dir / tool.name, "--check"] if tool == SUP else [tools_dir / tool.name, "status"]
    done = py(args, env=_clean_env(), cwd=work)
    assert done.returncode == 1
    lines = [ln for ln in done.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1 and "engram_regular_io" in lines[0]
    assert "Traceback" not in done.stderr


# =====================================================================
# Fold round 1: a non-finite recheck interval, and sys.path is append-only
# =====================================================================

@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "1e999"])
def test_non_finite_recheck_interval_falls_back_to_the_default(wake, monkeypatch, value):
    """time.sleep(inf) raises OverflowError and would end the watcher with a
    traceback; any non-finite value falls back to 60 s like the other bad
    values. Mutation: the math.isfinite condition removed ('inf' and '1e999'
    pass `> 0`; 'nan' fails it and so stays green)."""
    monkeypatch.setenv("CODEX_UMWELT_WAKE_REFUSAL_RECHECK_S", value)
    assert wake._refusal_recheck_seconds() == 60.0


@pytest.mark.parametrize("value, expected", [("0.2", 0.2), ("7", 7.0), ("0", 60.0), ("-3", 60.0), ("junk", 60.0)])
def test_finite_recheck_interval_is_honoured_and_bad_values_default(wake, monkeypatch, value, expected):
    monkeypatch.setenv("CODEX_UMWELT_WAKE_REFUSAL_RECHECK_S", value)
    assert wake._refusal_recheck_seconds() == expected


@pytest.mark.parametrize("tool", [SUP, WAKE], ids=["supervisor", "codex"])
def test_the_tools_only_ever_append_to_sys_path(tool):
    """Every `sys.path.<attr>(...)` call in the tool is `append`, and there is at
    least one. An insert(0, ...) lets a planted module shadow the stdlib or an
    installed package. Mutation: the helper import's append -> insert(0, ...)."""
    import ast
    attrs = []
    for node in ast.walk(ast.parse(tool.read_text())):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if (isinstance(owner, ast.Attribute) and owner.attr == "path"
                    and isinstance(owner.value, ast.Name) and owner.value.id == "sys"):
                attrs.append(node.func.attr)
    assert "append" in attrs
    assert not {"insert", "extend", "remove", "pop"} & set(attrs), attrs


# =====================================================================
# macOS-only rows (a Mac seat runs them; no macOS runners in CI)
# =====================================================================

@pytest.mark.skipif(sys.platform != "darwin", reason="needs a Mac seat; no macOS runners in CI")
def test_darwin_supervisor_start_refuses_a_real_socket_file(work):
    """On macOS a socket-type file gives EOPNOTSUPP at open, not ENXIO."""
    plant("sock", work / "p.pid")
    marker, args = _start_args(work, work / "p.pid")
    done = py(args)
    assert done.returncode == 4 and "socket" in done.stderr and not marker.exists()
