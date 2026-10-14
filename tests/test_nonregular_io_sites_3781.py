"""The codex watcher's log, state file and `start`, and the monitor supervisor's
status temp file, on a non-regular path -- issue #3781 (follow-up to #3773/#3779).

Contract (the design seat's ruling on #3781), asserted here BEFORE the sites were
changed. Each site is classified by what it protects:

  diagnostic sink   codex Logger.log (watcher.log); supervisor write_status
                    (<status>.tmp).  DEGRADE and keep running.  The log line goes
                    to STDERR instead and the refusal is reported ONCE per
                    transition (one stderr line naming path + kind).  A refused
                    status write is skipped and reported once; `--check` then
                    reads the status as stale.  Never a kill switch.
  state invariant   codex state.json read and the atomic-write .tmp.
                    REPORT-AND-IDLE (the #3779 pidfile contract): log once,
                    recheck every CODEX_UMWELT_WAKE_REFUSAL_RECHECK_S, NEVER
                    exit.  A refused READ must NOT fall back to default_state().
  interactive       `start`: the log path, for both the Logger and the handle
                    given to Popen.  REFUSE LOUDLY, spawn NOTHING, nonzero exit,
                    the reason names path + kind.

Refused = FIFO, directory, a symlink (even to a regular sentinel), a hard link
(nlink > 1).  Every call that can hang runs in a CHILD python (-I) under a hard
kill bound; a timeout is pytest.fail("HUNG").  Every cell's docstring names the
mutation that must redden it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
TOOLS = SRC_ENGRAM / "tools"
SUP = TOOLS / "monitor-supervisor.py"
WAKE = TOOLS / "codex-umwelt-wake.py"
HELPER = SRC_ENGRAM / "engram_regular_io.py"
BOUND_S = 8.0
SENTINEL_BYTES = b"x" * 135

KINDS = ["fifo", "symlink", "hardlink", "dir"]
KIND_WORD = {"fifo": "FIFO", "symlink": "symlink", "hardlink": "2 hard links", "dir": "directory"}


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


def identity(path):
    st = os.lstat(path)
    return (st.st_mode, st.st_ino, st.st_nlink)


def _reap_strays(d):
    """A cell where `start` WRONGLY spawned a watcher must not leave it running
    (it would idle forever): read the pid it wrote into THIS cell's own state dir,
    check the command line names this tool and this dir, SIGTERM that pid."""
    try:
        # lstat first: a planted FIFO at watcher.pid must not hang the cleanup.
        if not stat.S_ISREG(os.lstat(d / "watcher.pid").st_mode):
            return
        pid = int((d / "watcher.pid").read_text().strip())
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except (OSError, ValueError):
        return
    if "codex-umwelt-wake.py" in cmd and str(d) in cmd:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


@pytest.fixture
def work(tmp_path_factory):
    # A unique name, never reused: a stray watcher from an earlier (failed) cell
    # must not be able to write into a later cell's directory.
    d = Path(tempfile.mkdtemp(prefix="w3781_", dir=tmp_path_factory.getbasetemp()))
    yield d
    _reap_strays(d)
    shutil.rmtree(d, ignore_errors=True)


def make_sentinel(work):
    s = work / "SENTINEL"
    s.write_bytes(SENTINEL_BYTES)
    return s


def py(args, *, env=None, cwd=None, timeout=BOUND_S):
    try:
        return subprocess.run([sys.executable, "-I", *map(str, args)], capture_output=True,
                              text=True, timeout=timeout, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        pytest.fail(f"HUNG: no return within {timeout}s")


_PRELUDE = r"""
import importlib.util, json, os, sys
from pathlib import Path
sys.path.append(os.path.dirname(sys.argv[1]) if sys.argv[1].endswith("engram_regular_io.py")
                else os.path.dirname(os.path.dirname(sys.argv[1])))
def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m
m = load(sys.argv[1], "tool_under_test")
tmp = sys.argv[2]
"""


def child(tool, work, body, *, timeout=BOUND_S):
    code = _PRELUDE + body + "\nprint('RESULT:' + json.dumps(result))\n"
    done = py(["-c", code, tool, work], timeout=timeout)
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT:")]
    assert done.returncode == 0 and lines, (done.returncode, done.stdout, done.stderr)
    out = json.loads(lines[-1][len("RESULT:"):])
    out["_stderr"] = done.stderr
    return out


# =====================================================================
# The shared primitives (engram_regular_io)
# =====================================================================

@pytest.mark.parametrize("kind", KINDS)
def test_append_regular_line_refuses_and_touches_nothing(work, kind):
    """NotRegularFile naming the kind; the sentinel byte-identical; the planted path
    unchanged.  Mutations: the strict open drops O_NOFOLLOW (symlink cell),
    the nlink check (hardlink cell), or maps only one of ENXIO/ELOOP/EISDIR
    (the fifo / symlink / dir cell respectively)."""
    sentinel = make_sentinel(work)
    plant(kind, work / "p", sentinel)
    before = identity(work / "p") if kind != "symlink" else os.readlink(work / "p")
    r = child(HELPER, work, r"""
out = {"exc": None}
try:
    m.append_regular_line(os.path.join(tmp, "p"), "hello")
except BaseException as exc:
    out = {"exc": type(exc).__name__, "is_nrf": isinstance(exc, m.NotRegularFile), "text": str(exc)}
result = out
""")
    assert r["exc"] == "NotRegularFile" and r["is_nrf"], r
    assert KIND_WORD[kind] in r["text"] and str(work / "p") in r["text"]
    assert (identity(work / "p") if kind != "symlink" else os.readlink(work / "p")) == before
    assert sentinel.read_bytes() == SENTINEL_BYTES


@pytest.mark.parametrize("kind", KINDS)
def test_write_regular_bytes_synced_refuses_and_touches_nothing(work, kind):
    """Same contract as append_regular_line.  Mutations as above."""
    sentinel = make_sentinel(work)
    plant(kind, work / "p", sentinel)
    before = identity(work / "p") if kind != "symlink" else os.readlink(work / "p")
    r = child(HELPER, work, r"""
out = {"exc": None}
try:
    m.write_regular_bytes_synced(os.path.join(tmp, "p"), b"payload")
except BaseException as exc:
    out = {"exc": type(exc).__name__, "is_nrf": isinstance(exc, m.NotRegularFile), "text": str(exc)}
result = out
""")
    assert r["exc"] == "NotRegularFile" and r["is_nrf"], r
    assert KIND_WORD[kind] in r["text"]
    assert (identity(work / "p") if kind != "symlink" else os.readlink(work / "p")) == before
    assert sentinel.read_bytes() == SENTINEL_BYTES


@pytest.mark.parametrize("kind", KINDS)
def test_read_regular_text_strict_refuses_every_kind(work, kind):
    """The strict READ (used for state.json) refuses what read_regular_text lets
    through: a symlink to a regular file and a hard link.  Mutations: O_NOFOLLOW
    dropped (symlink), nlink check dropped (hardlink)."""
    sentinel = make_sentinel(work)
    plant(kind, work / "p", sentinel)
    r = child(HELPER, work, r"""
out = {"exc": None}
try:
    m.read_regular_text_strict(os.path.join(tmp, "p"))
except BaseException as exc:
    out = {"exc": type(exc).__name__, "text": str(exc)}
result = out
""")
    assert r["exc"] == "NotRegularFile", r
    assert KIND_WORD[kind] in r["text"]
    assert sentinel.read_bytes() == SENTINEL_BYTES


@pytest.mark.parametrize("kind", KINDS)
def test_open_regular_append_strict_refuses_every_kind(work, kind):
    """The strict append HANDLE (given to Popen by `start`) refuses every kind."""
    sentinel = make_sentinel(work)
    plant(kind, work / "p", sentinel)
    r = child(HELPER, work, r"""
out = {"exc": None}
try:
    f = m.open_regular_append_strict(os.path.join(tmp, "p"))
    f.write(b"x")
    f.close()
except BaseException as exc:
    out = {"exc": type(exc).__name__, "text": str(exc)}
result = out
""")
    assert r["exc"] == "NotRegularFile", r
    assert KIND_WORD[kind] in r["text"]
    assert sentinel.read_bytes() == SENTINEL_BYTES


def test_primitives_work_on_regular_files(work):
    """Positive control: append adds one line each time and creates the file;
    the synced write replaces the whole content (a LONGER old content is cut)
    and calls os.fsync once; the strict read returns the text, and an absent
    file is FileNotFoundError (not NotRegularFile); the append handle is
    blocking and appends."""
    (work / "w").write_bytes(b"old-old-old-old-old")
    r = child(HELPER, work, r"""
p = os.path.join(tmp, "a")
m.append_regular_line(p, "one")
m.append_regular_line(p, "two")
fsyncs = []
real = os.fsync
os.fsync = lambda fd: (fsyncs.append(fd), real(fd))[1]
w = os.path.join(tmp, "w")
m.write_regular_bytes_synced(w, b"new")
c = os.path.join(tmp, "created")
m.write_regular_bytes_synced(c, b"made")
os.fsync = real
got = m.read_regular_text_strict(w)
try:
    m.read_regular_text_strict(os.path.join(tmp, "absent"))
    absent = None
except BaseException as exc:
    absent = type(exc).__name__
h = m.open_regular_append_strict(os.path.join(tmp, "h"))
h.write(b"1\n"); h.close()
h = m.open_regular_append_strict(os.path.join(tmp, "h"))
h.write(b"2\n"); blocking = os.get_blocking(h.fileno()); h.close()
result = {"a": open(p).read(), "w": open(w, "rb").read().decode(), "c": open(c).read(),
          "fsyncs": len(fsyncs), "read": got, "absent": absent,
          "h": open(os.path.join(tmp, "h")).read(), "blocking": blocking}
""")
    assert r["a"] == "one\ntwo\n"
    assert r["w"] == "new" and r["c"] == "made"
    assert r["fsyncs"] == 2, "the synced write must fsync before closing"
    assert r["read"] == "new"
    assert r["absent"] == "FileNotFoundError"
    assert r["h"] == "1\n2\n" and r["blocking"] is True


# =====================================================================
# S1 -- codex Logger.log: degrade
# =====================================================================

@pytest.mark.parametrize("kind", KINDS)
def test_logger_degrades_to_stderr_reports_once_and_keeps_running(work, kind):
    """3 log calls on a refused watcher.log: the call returns each time (no hang,
    no exception), stderr carries the refusal line EXACTLY ONCE (naming the path
    and kind), each message still reaches stderr, the sentinel is byte-identical,
    the planted path unchanged.  Mutations: Logger back to plain open("a") (hangs
    / writes through / raises); the refusal reported on every call."""
    sentinel = make_sentinel(work)
    plant(kind, work / "watcher.log", sentinel)
    before = identity(work / "watcher.log") if kind != "symlink" else os.readlink(work / "watcher.log")
    r = child(WAKE, work, r"""
lg = m.Logger(Path(tmp) / "watcher.log", stdout=False)
for i in (1, 2, 3):
    lg.log(f"message-{i}")
result = {"alive": True}
""")
    assert r["alive"]
    err = r["_stderr"].splitlines()
    refusals = [ln for ln in err if "watcher.log" in ln and KIND_WORD[kind] in ln and "stderr" in ln]
    assert len(refusals) == 1, err
    for i in (1, 2, 3):
        assert sum(f"message-{i}" in ln for ln in err) == 1, err
    assert "Traceback" not in r["_stderr"]
    assert sentinel.read_bytes() == SENTINEL_BYTES
    assert (identity(work / "watcher.log") if kind != "symlink"
            else os.readlink(work / "watcher.log")) == before


def test_logger_reports_again_on_the_next_transition(work):
    """refused -> accepted -> refused again: the line goes to the FILE while it is
    acceptable, and the second refusal is reported again (once per TRANSITION).
    Mutation: the reported flag never resets (the second refusal is silent)."""
    r = child(WAKE, work, r"""
log = Path(tmp) / "watcher.log"
os.mkfifo(log)
lg = m.Logger(log, stdout=False)
lg.log("while-refused-1"); lg.log("while-refused-2")
os.unlink(log)
lg.log("while-fine")
os.unlink(log)
os.mkfifo(log)
lg.log("refused-again")
result = {"file_has": False}
""")
    err = r["_stderr"].splitlines()
    assert sum("stderr" in ln and "FIFO" in ln for ln in err) == 2, err
    assert sum("while-fine" in ln for ln in err) == 0           # it went to the file
    assert sum("refused-again" in ln for ln in err) == 1


def test_logger_regular_file_behaves_as_before(work):
    """Positive control S7: the lines land in the file with the timestamp prefix,
    one per call, nothing on stderr, stdout echo unchanged."""
    r = child(WAKE, work, r"""
lg = m.Logger(Path(tmp) / "watcher.log", stdout=True)
lg.log("alpha"); lg.log("beta")
result = {"text": (Path(tmp) / "watcher.log").read_text()}
""")
    lines = r["text"].splitlines()
    assert len(lines) == 2 and lines[0].endswith(" alpha") and lines[1].endswith(" beta")
    assert r["_stderr"] == ""


# =====================================================================
# watcher / supervisor processes
# =====================================================================

def _run_args(work, extra=()):
    return [WAKE, "run", "--state-dir", work, "--quiet-stdout", "--interval", "0.2",
            "--sessions-dir", work / "sessions", "--umwelt-config", work / "no-umwelt.json",
            "--app-server", "ws://127.0.0.1:1", *extra]


def _env(work, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH", "CODEX_UMWELT_WAKE_NO_REAP", "CODEX_THREAD_ID")}
    env.update({"ENGRAM_HOME": str(work), "CODEX_UMWELT_WAKE_REFUSAL_RECHECK_S": "0.2", **extra})
    return env


def _spawn(work, extra=()):
    return subprocess.Popen([sys.executable, "-I", *map(str, _run_args(work, extra))],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(work))


def _end(proc, sig=signal.SIGTERM, within=3.0):
    proc.send_signal(sig)
    try:
        out, err = proc.communicate(timeout=within + 3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"did not end within {within}s of the signal")
    return out, err


def _alive_for(proc, seconds):
    time.sleep(seconds)
    if proc.poll() is not None:
        out, err = proc.communicate()
        pytest.fail(f"the process EXITED (rc {proc.returncode}); stderr: {err[-400:]!r}")


def _kill(proc):
    if proc.poll() is None:
        proc.kill()
    proc.communicate()


def _log_lines(work):
    p = work / "watcher.log"
    return p.read_text().splitlines() if p.is_file() else []


# =====================================================================
# S3 -- state.json READ: report-and-idle, never default_state()
# =====================================================================

@pytest.mark.parametrize("kind", KINDS)
def test_state_read_refused_idles_reports_once_and_leaves_the_path_alone(work, kind):
    """A planted state.json: the watcher stays ALIVE (>= 10 rechecks at 0.2 s), the
    watcher.log holds exactly ONE 'idling' line naming the state file and kind,
    the planted path (and the sentinel) is byte-identical -- so default_state()
    was NOT written over it -- no state.json.tmp appeared, no cycle ran
    ('cycle failed' absent).  Mutations: the read falls back to default_state()
    (the planted path gets replaced / a cycle runs); exit instead of idle; a line per
    recheck; a plain read_text (hangs on the FIFO)."""
    sentinel = make_sentinel(work)
    plant(kind, work / "state.json", sentinel)
    before = identity(work / "state.json")
    proc = _spawn(work)
    try:
        _alive_for(proc, 3.0)
    except BaseException:
        _kill(proc)
        raise
    _out, err = _end(proc)
    assert "Traceback" not in err, err
    idle = [ln for ln in _log_lines(work) if "idling, will recheck" in ln]
    assert len(idle) == 1, _log_lines(work)
    assert "state file" in idle[0] and KIND_WORD[kind] in idle[0] and "state.json" in idle[0]
    assert identity(work / "state.json") == before
    assert sentinel.read_bytes() == SENTINEL_BYTES
    assert not (work / "state.json.tmp").exists()
    assert not any("cycle failed" in ln for ln in _log_lines(work))


def test_state_read_refused_then_removed_resumes_on_a_fresh_state(work):
    """Idle is not terminal: removing the planted path lets the next recheck run a
    cycle (state.json becomes a regular file).  Mutation: idles forever."""
    os.mkfifo(work / "state.json")
    proc = _spawn(work)
    try:
        _alive_for(proc, 1.0)
        os.unlink(work / "state.json")
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and not (work / "state.json").is_file():
            time.sleep(0.1)
        assert (work / "state.json").is_file(), "never resumed after the path became acceptable"
        assert proc.poll() is None
    except BaseException:
        _kill(proc)
        raise
    _end(proc)


def test_state_regular_control_runs_cycles_as_before(work):
    """S7: a regular state.json is read and rewritten, no 'idling' line."""
    (work / "state.json").write_text(json.dumps({"state_version": 6, "seeded": False}))
    proc = _spawn(work)
    try:
        _alive_for(proc, 2.0)
    except BaseException:
        _kill(proc)
        raise
    _end(proc)
    assert not any("idling" in ln for ln in _log_lines(work))
    assert json.loads((work / "status.json").read_text())["mode"] in ("stopped", "stopping", "idle", "cycle_failed")
    assert isinstance(json.loads((work / "state.json").read_text()), dict)


def test_state_corrupt_json_still_falls_back_to_default_out_of_scope(work):
    """#3786 is NOT this change: corrupt JSON in a REGULAR state.json still reads as
    the default and the watcher runs.  This pins that I did not change it."""
    (work / "state.json").write_text("{not json")
    proc = _spawn(work)
    try:
        _alive_for(proc, 2.0)
    except BaseException:
        _kill(proc)
        raise
    _end(proc)
    assert not any("idling" in ln for ln in _log_lines(work))


# =====================================================================
# S4 -- state.json.tmp WRITE: report-and-idle
# =====================================================================

@pytest.mark.parametrize("kind", KINDS)
def test_state_write_refused_idles_reports_once_and_leaves_state_alone(work, kind):
    """A planted state.json.tmp with a VALID regular state.json: the watcher is
    ALIVE, exactly ONE 'idling' line naming the .tmp path and kind, state.json is
    byte-identical, the sentinel behind the symlink/hard link untouched, the
    planted tmp unchanged.  Mutations: atomic_write_json back to tmp.open("w")
    (hangs / writes through the link); the error escapes and ends the watcher."""
    sentinel = make_sentinel(work)
    state_bytes = json.dumps({"state_version": 6, "seeded": False}).encode()
    (work / "state.json").write_bytes(state_bytes)
    plant(kind, work / "state.json.tmp", sentinel)
    before = identity(work / "state.json.tmp")
    proc = _spawn(work)
    try:
        _alive_for(proc, 3.0)
    except BaseException:
        _kill(proc)
        raise
    _out, err = _end(proc)
    assert "Traceback" not in err, err
    idle = [ln for ln in _log_lines(work) if "idling, will recheck" in ln]
    assert len(idle) == 1, _log_lines(work)
    assert "state.json.tmp" in idle[0] and KIND_WORD[kind] in idle[0]
    assert (work / "state.json").read_bytes() == state_bytes
    assert sentinel.read_bytes() == SENTINEL_BYTES
    assert identity(work / "state.json.tmp") == before


# =====================================================================
# status.json.tmp (codex): a diagnostic write must not end the watcher
# =====================================================================

@pytest.mark.parametrize("kind", KINDS)
def test_codex_status_tmp_refused_does_not_end_the_watcher(work, kind):
    """A planted status.json.tmp: the watcher keeps running (it still reads state
    and cycles), the sentinel is untouched, no traceback.  Mutation: update_status
    lets NotRegularFile escape (the strict tmp write would otherwise make the
    status file a kill switch)."""
    sentinel = make_sentinel(work)
    plant(kind, work / "status.json.tmp", sentinel)
    proc = _spawn(work)
    try:
        _alive_for(proc, 2.5)
    except BaseException:
        _kill(proc)
        raise
    _out, err = _end(proc)
    assert "Traceback" not in err, err
    assert sentinel.read_bytes() == SENTINEL_BYTES


_PATHS = r"""
paths = m.RuntimePaths(Path(tmp), Path(tmp) / "state.json", Path(tmp) / "status.json",
                       Path(tmp) / "watcher.log", Path(tmp) / "watcher.pid")
"""


def test_codex_update_status_destination_directory_degrades_leaves_no_tmp(work):
    """G1: the DESTINATION status.json is a directory -> os.replace raises
    IsADirectoryError. update_status returns, writes nothing under the directory,
    leaves no status.json.tmp, reports ONE line across 3 calls.
    Mutations: catch only NotRegularFile; tmp not unlinked after a failed replace."""
    (work / "status.json").mkdir()
    r = child(WAKE, work, _PATHS + r"""
for _ in range(3):
    m.update_status(paths, {"a": 1})
result = {"under": sorted(os.listdir(os.path.join(tmp, "status.json"))),
          "tmp_left": os.path.lexists(os.path.join(tmp, "status.json.tmp"))}
""")
    assert r["under"] == [] and r["tmp_left"] is False
    lines = [ln for ln in r["_stderr"].splitlines() if "status not written" in ln]
    assert len(lines) == 1, r["_stderr"]
    assert "Traceback" not in r["_stderr"]


def test_codex_watcher_survives_a_directory_at_the_status_destination(work):
    """G1 (process): the real cmd_run, status.json a directory: alive after 2.5 s,
    no traceback (the finally-block update_status must not raise either), no stale
    status.json.tmp.  Mutation: update_status catches only NotRegularFile."""
    (work / "status.json").mkdir()
    proc = _spawn(work)
    try:
        _alive_for(proc, 2.5)
    except BaseException:
        _kill(proc)
        raise
    _out, err = _end(proc)
    assert "Traceback" not in err, err
    assert not os.path.lexists(work / "status.json.tmp")
    assert list((work / "status.json").iterdir()) == []


def test_codex_update_status_reports_again_on_the_next_transition(work):
    """G3: refused tmp reported ONCE across 3 calls; once the path is cleared a call
    succeeds and writes; a later re-refusal reports AGAIN.
    Mutations: flag never set (every call reports); flag never resets."""
    r = child(WAKE, work, _PATHS + r"""
tmpf = os.path.join(tmp, "status.json.tmp")
os.mkfifo(tmpf)
for _ in range(3):
    m.update_status(paths, {"a": 1})
os.unlink(tmpf)
m.update_status(paths, {"a": 2})
ok = json.load(open(os.path.join(tmp, "status.json")))
os.mkfifo(tmpf)
m.update_status(paths, {"a": 3})
result = {"ok": ok}
""")
    assert r["ok"] == {"a": 2}
    assert sum("FIFO" in ln for ln in r["_stderr"].splitlines()) == 2, r["_stderr"]


def test_codex_write_state_lets_a_plain_oserror_propagate(work):
    """G5: the state invariant is unchanged -- only NotRegularFile becomes
    StateRefused; any other OSError neither converts nor is swallowed.
    Mutation: write_state also swallows OSError."""
    r = child(WAKE, work, _PATHS + r"""
def boom(path, payload):
    raise OSError(28, "no space")
m.atomic_write_json = boom
try:
    m.write_state(paths, {})
    out = "swallowed"
except m.StateRefused:
    out = "staterefused"
except OSError as exc:
    out = "oserror:%d" % exc.errno
result = {"out": out}
""")
    assert r["out"] == "oserror:28"


# =====================================================================
# S6 -- the idle path with a degraded logger
# =====================================================================

def test_refused_pidfile_with_a_fifo_log_still_idles(work):
    """FIFO at watcher.log AND FIFO at watcher.pid: the watcher does not hang before
    its first report; it idles, ALIVE; stderr carries the pidfile refusal ONCE and
    the log refusal ONCE.  Mutation: Logger back to plain open("a") (the idle-path
    report blocks on the FIFO)."""
    os.mkfifo(work / "watcher.log")
    os.mkfifo(work / "watcher.pid")
    proc = _spawn(work)
    try:
        _alive_for(proc, 2.5)
    except BaseException:
        _kill(proc)
        raise
    _out, err = _end(proc)
    lines = err.splitlines()
    assert sum("refusing to start: the pidfile path" in ln for ln in lines) == 1, lines
    assert sum("watcher.log" in ln and "stderr" in ln and "FIFO" in ln for ln in lines) == 1, lines
    assert "Traceback" not in err
    assert stat.S_ISFIFO(os.lstat(work / "watcher.log").st_mode)


# =====================================================================
# S5 -- `start`: refuse loudly, spawn nothing
# =====================================================================

def _start_args(work):
    return [WAKE, "start", "--state-dir", work, "--sessions-dir", work / "sessions",
            "--umwelt-config", work / "no-umwelt.json", "--app-server", "ws://127.0.0.1:1",
            "--interval", "0.2"]


@pytest.mark.parametrize("kind", KINDS)
def test_start_refuses_loudly_and_spawns_nothing(work, kind):
    """Nonzero exit within the bound, ONE reason line naming path + kind, the
    sentinel byte-identical, NO child (the pidfile is absent even after the child
    would have written it).  Mutation: the log handle opened with plain
    open("a") (hangs / writes through)."""
    sentinel = make_sentinel(work)
    plant(kind, work / "watcher.log", sentinel)
    before = identity(work / "watcher.log") if kind != "symlink" else os.readlink(work / "watcher.log")
    done = py(_start_args(work), env=_env(work))
    time.sleep(1.5)                      # a wrongly spawned child has written its pidfile by now
    spawned = (work / "watcher.pid").exists()
    assert done.returncode != 0, (done.stdout, done.stderr)
    reason = [ln for ln in done.stderr.splitlines() if "refusing to start" in ln]
    assert len(reason) == 1, done.stderr
    assert str(work / "watcher.log") in reason[0] and KIND_WORD[kind] in reason[0]
    assert "Traceback" not in done.stderr
    assert not spawned, "a child was spawned"
    assert sentinel.read_bytes() == SENTINEL_BYTES
    assert (identity(work / "watcher.log") if kind != "symlink"
            else os.readlink(work / "watcher.log")) == before


@pytest.mark.parametrize("kind", KINDS)
def test_start_never_calls_popen_on_a_refused_log(work, kind):
    """In-process view of the same: Popen is replaced by a recorder; zero calls and
    a nonzero return.  Mutation: spawn BEFORE the check (one call recorded)."""
    sentinel = make_sentinel(work)
    plant(kind, work / "watcher.log", sentinel)
    r = child(WAKE, work, r"""
calls = []
m.subprocess.Popen = lambda *a, **k: calls.append((a, k))
import argparse
args = argparse.Namespace(state_dir=Path(tmp), sessions_dir=Path(tmp) / "s", umwelt_config=Path(tmp) / "u",
                          app_server="ws://127.0.0.1:1", interval=0.2, thread_id=None)
rc = m.cmd_start(args)
result = {"rc": rc, "calls": len(calls)}
""")
    assert r["calls"] == 0 and r["rc"] != 0
    assert KIND_WORD[kind] in r["_stderr"] and "watcher.log" in r["_stderr"]


def test_start_regular_control_spawns_with_the_log_handle(work):
    """S7: a regular log: rc 0, ONE Popen call whose stdout is an open handle on the
    log, and the 'spawned' line in the log."""
    r = child(WAKE, work, r"""
calls = []
m.subprocess.Popen = lambda *a, **k: calls.append(k)
import argparse
args = argparse.Namespace(state_dir=Path(tmp), sessions_dir=Path(tmp) / "s", umwelt_config=Path(tmp) / "u",
                          app_server="ws://127.0.0.1:1", interval=0.2, thread_id=None)
rc = m.cmd_start(args)
k = calls[0]
result = {"rc": rc, "n": len(calls), "has_fd": k["stdout"].fileno() >= 0 if not k["stdout"].closed else "closed-ok",
          "log": (Path(tmp) / "watcher.log").read_text()}
""")
    assert r["rc"] == 0 and r["n"] == 1
    assert "spawned background watcher" in r["log"]


# =====================================================================
# S2 -- supervisor write_status: degrade, `--check` reads it stale
# =====================================================================

def _sup_obj(body):
    return r"""
sup = m.Supervisor({}, os.path.join(tmp, "status.json"), 0.3, os.path.join(tmp, "p.pid"))
""" + body


@pytest.mark.parametrize("kind", KINDS)
def test_supervisor_write_status_degrades_reports_once_and_returns(work, kind):
    """3 write_status calls on a refused status.json.tmp: each returns (no hang, no
    exception), stderr carries ONE refusal line naming the path and kind, the
    sentinel and planted path are untouched, status.json is not created.
    Mutations: back to open(tmp, "w"); write_status raises; reported every call."""
    sentinel = make_sentinel(work)
    plant(kind, work / "status.json.tmp", sentinel)
    before = identity(work / "status.json.tmp") if kind != "symlink" else os.readlink(work / "status.json.tmp")
    r = child(SUP, work, _sup_obj(r"""
for _ in range(3):
    sup.write_status()
result = {"exists": os.path.exists(os.path.join(tmp, "status.json"))}
"""))
    assert r["exists"] is False
    refusals = [ln for ln in r["_stderr"].splitlines() if "status.json.tmp" in ln and KIND_WORD[kind] in ln]
    assert len(refusals) == 1, r["_stderr"]
    assert "Traceback" not in r["_stderr"]
    assert sentinel.read_bytes() == SENTINEL_BYTES
    assert (identity(work / "status.json.tmp") if kind != "symlink"
            else os.readlink(work / "status.json.tmp")) == before


def test_supervisor_write_status_reports_again_on_the_next_transition(work):
    """refused -> ok -> refused: 2 reports, and the good write lands.  Mutation:
    the flag never resets."""
    r = child(SUP, work, _sup_obj(r"""
tmpf = os.path.join(tmp, "status.json.tmp")
os.mkfifo(tmpf); sup.write_status(); sup.write_status()
os.unlink(tmpf); sup.write_status()
ok = os.path.isfile(os.path.join(tmp, "status.json"))
os.mkfifo(tmpf); sup.write_status()
result = {"ok": ok}
"""))
    assert r["ok"] is True
    assert sum("FIFO" in ln for ln in r["_stderr"].splitlines()) == 2, r["_stderr"]


def test_supervisor_write_status_regular_control(work):
    """S7: the status file is written, valid JSON with the same keys, no tmp left,
    nothing on stderr."""
    r = child(SUP, work, _sup_obj(r"""
sup.write_status()
result = {"data": json.load(open(os.path.join(tmp, "status.json"))),
          "tmp_left": os.path.exists(os.path.join(tmp, "status.json.tmp"))}
"""))
    assert sorted(r["data"]) == ["children", "generated_at", "status_interval"]
    assert r["tmp_left"] is False and r["_stderr"] == ""


def _sup_proc(work):
    sleeper = json.dumps([sys.executable, "-c", "import time; time.sleep(60)"])
    return subprocess.Popen(
        [sys.executable, "-I", str(SUP), "--pidfile", str(work / "p.pid"),
         "--status-file", str(work / "status.json"), "--status-interval", "0.3",
         "--bell-cmd-json", sleeper, "--updates-cmd-json", sleeper, "--mention-cmd-json", sleeper],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(work))


def _check(work):
    return py([SUP, "--check", "--pidfile", work / "p.pid", "--status-file", work / "status.json"])


@pytest.mark.parametrize("kind", KINDS)
def test_supervisor_process_survives_a_refused_status_tmp_and_check_reads_stale(work, kind):
    """The REAL supervisor with a planted status.json.tmp: still ALIVE after 2.5 s
    (it did not hang in start_all and did not exit), status.json never appears,
    and the REAL `--check` output reports the status file missing/stale.
    Mutations: open(tmp, "w") (hangs start_all on the FIFO); write_status raises."""
    sentinel = make_sentinel(work)
    plant(kind, work / "status.json.tmp", sentinel)
    proc = _sup_proc(work)
    try:
        _alive_for(proc, 2.5)
        assert not (work / "status.json").exists()
        done = _check(work)
        assert done.returncode != 0
        assert "status file missing/stale" in done.stdout, done.stdout
    except BaseException:
        _kill(proc)
        raise
    # A supervisor HUNG in start_all is alive and reads stale too; only a prompt
    # exit on SIGTERM tells hung from degraded.
    proc.send_signal(signal.SIGTERM)
    try:
        _o, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("the supervisor did not end on SIGTERM (hung in start_all?)")
    assert "Traceback" not in err
    assert sentinel.read_bytes() == SENTINEL_BYTES


def test_supervisor_process_writes_status_once_the_refused_tmp_is_removed(work):
    """Degrade is not terminal: after the FIFO at the tmp path is removed, the status
    loop's next tick writes status.json.  Mutation: the loop thread dies on the
    first refusal (write_status raises inside _status_loop)."""
    os.mkfifo(work / "status.json.tmp")
    proc = _sup_proc(work)
    try:
        _alive_for(proc, 1.5)
        os.unlink(work / "status.json.tmp")
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not (work / "status.json").is_file():
            time.sleep(0.1)
        assert (work / "status.json").is_file(), "status never written after the path became acceptable"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


def test_supervisor_process_regular_control_check_reads_fresh(work):
    """S7: with no planted path the status file is fresh and `--check` does NOT report
    it missing/stale."""
    proc = _sup_proc(work)
    try:
        _alive_for(proc, 2.0)
        done = _check(work)
        assert "status file missing/stale" not in done.stdout, done.stdout
        assert (work / "status.json").is_file()
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


def test_supervisor_write_status_destination_directory_degrades_leaves_no_tmp(work):
    """G2: the DESTINATION status file is a directory -> os.replace raises. write_status
    returns, no tmp left, ONE report across 3 calls.
    Mutations: catch only NotRegularFile; tmp not unlinked after a failed replace."""
    (work / "status.json").mkdir()
    r = child(SUP, work, _sup_obj(r"""
for _ in range(3):
    sup.write_status()
result = {"under": sorted(os.listdir(os.path.join(tmp, "status.json"))),
          "tmp_left": os.path.lexists(os.path.join(tmp, "status.json.tmp"))}
"""))
    assert r["under"] == [] and r["tmp_left"] is False
    lines = [ln for ln in r["_stderr"].splitlines() if "status not written" in ln]
    assert len(lines) == 1, r["_stderr"]
    assert "Traceback" not in r["_stderr"]


def test_supervisor_process_survives_a_directory_at_the_status_destination(work):
    """G2 (process): the real supervisor stays alive, ends promptly on SIGTERM, and
    `--check` reports the directory (never a fresh status).  Mutation: catch only NotRegularFile."""
    (work / "status.json").mkdir()
    proc = _sup_proc(work)
    try:
        _alive_for(proc, 2.0)
        done = _check(work)
        assert done.returncode != 0
        # --check reads a directory at the status path as unreadable (its own
        # refusal text), not as "missing/stale"; either way the children FAIL.
        assert "status file is a directory" in done.stdout, done.stdout
    except BaseException:
        _kill(proc)
        raise
    proc.send_signal(signal.SIGTERM)
    try:
        _o, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("the supervisor did not end on SIGTERM")
    assert "Traceback" not in err
    assert not os.path.lexists(work / "status.json.tmp")


# =====================================================================
# run_cycle's three persist CALLBACKS (in-process): each must surface
# StateRefused, a BaseException, through the `except Exception` handlers
# =====================================================================

import contextlib
import importlib.util

_spec = importlib.util.spec_from_file_location("codex_umwelt_wake_3781", WAKE)
assert _spec and _spec.loader
wake = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = wake
_spec.loader.exec_module(wake)

CALLBACKS = ["drain_persist_state", "delivery_persist_state", "delivery_after_inflight_persist"]


@contextlib.contextmanager
def _alarm(seconds=int(BOUND_S)):
    """A hang fails the cell instead of freezing the suite (the callee under test
    runs in THIS process, so a child-process kill bound is not available)."""
    def _boom(_signum, _frame):
        pytest.fail(f"HUNG: no return within {seconds}s")

    old = signal.signal(signal.SIGALRM, _boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


class _Monitor:
    def __init__(self, on_drain):
        self.on_drain = on_drain

    def drain(self, **kwargs):
        self.on_drain(kwargs["persist_state"])
        return 0


def _cycle(work, callback, plant_tmp, marker, monkeypatch, box=None):
    """run_cycle with the callee that receives `callback` replaced by a stub that
    (a) plants the .tmp path (when plant_tmp is not None) and (b) invokes exactly
    that callback once inside `try/except Exception`, as the real handlers in
    attempt_delivery / run_cycle would.  Returns (paths, tmp, status, calls)."""
    paths = wake.build_paths(work / "codex-wake")
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = paths.state_path.with_name(paths.state_path.name + ".tmp")
    calls = []

    def invoke(cb):
        if plant_tmp is not None:
            plant_tmp(tmp)
        payload = {"marker": marker}
        try:
            if callback == "delivery_after_inflight_persist":
                cb(payload, {})
            else:
                cb(payload)
            calls.append("returned")
        except Exception:
            calls.append("swallowed")  # what the real handlers do to an Exception
        finally:
            # Clear the planted path again: otherwise run_cycle's own LATER plain
            # write_state would refuse on it and mask a callback that let the
            # error get swallowed.  Only a StateRefused out of the callback itself
            # may reach the assertion.
            if plant_tmp is not None:
                if box is not None:
                    box["after"] = identity(tmp)
                if tmp.is_dir() and not tmp.is_symlink():
                    tmp.rmdir()
                else:
                    tmp.unlink()

    idle = wake.DeliveryResult(status="idle", detail="stub", thread_id=None)
    monitor = None
    if callback == "drain_persist_state":
        monitor = _Monitor(invoke)
        monkeypatch.setattr(wake, "attempt_delivery", lambda s, **k: idle)
    else:
        def fake_attempt_delivery(current_state, **kwargs):
            key = "persist_state" if callback == "delivery_persist_state" else "after_inflight_persist"
            invoke(kwargs[key])
            return idle

        monkeypatch.setattr(wake, "attempt_delivery", fake_attempt_delivery)
    with _alarm():
        status = wake.run_cycle(
            paths=paths, umwelt_config=work / "missing-umwelt.json", app_server="ws://127.0.0.1:4500",
            sessions_dir=work / "sessions", thread_override="thread-1",
            logger=wake.Logger(work / "watcher.log", stdout=False),
            started_at="2026-07-22T12:00:00+00:00", codex_monitor=monitor,
        )
    return paths, tmp, status, calls


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("callback", CALLBACKS)
def test_run_cycle_persist_callback_on_refused_tmp_raises_staterefused(work, monkeypatch, callback, kind):
    """Each of the three persist lambdas, invoked on a refused state.json.tmp,
    raises StateRefused('write') OUT of run_cycle and through the stub's
    `except Exception`; sentinel and the planted tmp untouched.  Mutations: that
    lambda calls raw atomic_write_json (NotRegularFile is an OSError -> swallowed,
    state unsaved); StateRefused made an Exception subclass."""
    sentinel = make_sentinel(work)
    box = {}

    def plant_tmp(tmp):
        plant(kind, tmp, sentinel)
        box["before"] = identity(tmp)

    with pytest.raises(wake.StateRefused) as ei:
        _cycle(work, callback, plant_tmp, "cb", monkeypatch, box)
    assert ei.value.op == "write"
    assert not isinstance(ei.value, Exception)
    assert isinstance(ei.value.exc, wake.NotRegularFile)
    assert sentinel.read_bytes() == SENTINEL_BYTES
    assert box["after"] == box["before"]  # the refused path was not touched by the callback


@pytest.mark.parametrize("callback", CALLBACKS)
def test_run_cycle_persist_callback_regular_control_writes_state(work, monkeypatch, callback):
    """Positive control: no planted tmp, the callback writes state.json with its
    payload and run_cycle returns normally (nothing swallowed).  Mutation: a
    callback that drops the write."""
    seen = {}
    real = wake.atomic_write_json

    def spy(path, payload):
        real(path, payload)
        if payload.get("marker") == "cb":
            seen["bytes"] = Path(path).read_bytes()

    monkeypatch.setattr(wake, "atomic_write_json", spy)
    paths, tmp, status, calls = _cycle(work, callback, None, "cb", monkeypatch)
    assert calls == ["returned"]
    assert json.loads(seen["bytes"]) == {"marker": "cb"}
    assert status["mode"] in ("idle", "waiting_for_umwelt")
    assert not tmp.exists()


# =====================================================================
# The operator advice on a refused state file branches by what the path IS
# (#3781, colleague review of #3792): a symlink / hard link to a regular file
# IS the state, so "remove it" would silently reset it; a FIFO / directory
# holds no state, so "remove it" is right.
# =====================================================================

QUEUE_STATE = {"state_version": 6, "seeded": True,
               "queue": [{"id": "crossing-1", "text": "queued while idle"}],
               "cursors": {"place-a": "pos-41"}}
REMOVE_ADVICE = "Remove it to resume"
LOSS_SENTENCE = "removing it instead restarts from now and LOSES the queue"


def _await_idle_line(work, timeout=8.0):
    """The first 'idling, will recheck' line of watcher.log (waits for it)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for ln in _log_lines(work):
            if "idling, will recheck" in ln:
                return ln
        time.sleep(0.1)
    pytest.fail(f"no 'idling' line within {timeout}s: {_log_lines(work)}")


def _refused_line(work, kind, *, target="state.json"):
    """Plant `kind` at `target` (state.json: with a queued state behind a symlink
    / hard link; state.json.tmp: beside a valid regular state.json), run the REAL
    watcher until it logs the refusal, and return that one log line."""
    real = work / "real-state.json"
    real.write_text(json.dumps(QUEUE_STATE))
    if target == "state.json":
        plant(kind, work / "state.json", real)
    else:
        (work / "state.json").write_text(json.dumps(QUEUE_STATE))
        plant(kind, work / target, real)
    proc = _spawn(work)
    try:
        line = _await_idle_line(work)
    finally:
        _kill(proc)
    return line


def _printed_command(line):
    """The ready-to-run command out of the logged line: the text after 'run: ' up
    to the '; removing it instead' clause that follows it."""
    after = line.split("run: ", 1)[1]
    return after.split("; removing it instead", 1)[0]


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_h1_state_read_refused_symlink_or_hardlink_says_copy_not_remove(work, kind):
    """H1: a link to a state file with a queue -> the line carries the cp command and
    the loss sentence, and does NOT say 'Remove it to resume'.  Mutations: always
    'Remove it to resume'; branch inverted; branch on symlink only."""
    line = _refused_line(work, kind)
    assert "cp -- " in line and " && mv -- " in line, line
    assert LOSS_SENTENCE in line, line
    assert REMOVE_ADVICE not in line, line
    assert "if the link target is gone the state is gone" in line, line


@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_h2_state_read_refused_fifo_or_directory_still_says_remove(work, kind):
    """H2: nothing to preserve in a FIFO / directory -> 'Remove it to resume', no cp
    command.  Mutation: branch inverted."""
    line = _refused_line(work, kind)
    assert REMOVE_ADVICE in line, line
    assert "cp --" not in line, line


@pytest.mark.parametrize("kind", KINDS)
def test_h3_state_write_refused_always_says_remove_the_tmp(work, kind):
    """H3: the refused path is state.json.tmp, which holds no state: every kind
    (symlink and hard link included) keeps 'Remove it to resume', no cp command.
    Mutation: the copy advice keyed on the kind alone (ignoring op)."""
    line = _refused_line(work, kind, target="state.json.tmp")
    assert "state.json.tmp" in line
    assert REMOVE_ADVICE in line, line
    assert "cp --" not in line, line


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_h4_the_printed_command_run_as_printed_resumes_with_the_state_intact(work, kind):
    """H4 (behavioural): the REAL watcher refuses a link to a queued state; the
    command EXACTLY AS PRINTED, run with bash in the state dir (whose name holds a
    space and a single quote), leaves state.json a regular single-link file; the
    watcher resumes and the queue, seeded flag and cursors survive.  Mutations:
    shlex.quote dropped (the quoted path breaks); the copy without the mv (the link
    stays, the watcher never resumes)."""
    sd = work / "it's a state dir"
    sd.mkdir()
    real = sd / "real-state.json"
    original = json.dumps(QUEUE_STATE)
    real.write_text(original)
    plant(kind, sd / "state.json", real)
    proc = _spawn(sd)
    try:
        command = _printed_command(_await_idle_line(sd))
        done = subprocess.run(["bash", "-c", command], cwd=sd, capture_output=True,
                              text=True, timeout=10)
        assert done.returncode == 0, (command, done.stderr)
        st = os.lstat(sd / "state.json")
        assert stat.S_ISREG(st.st_mode) and st.st_nlink == 1, st
        # Resume: the next recheck runs a cycle, which rewrites the status file.
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and not (sd / "status.json").is_file():
            time.sleep(0.1)
        assert (sd / "status.json").is_file(), "never resumed after the copy"
        assert proc.poll() is None
        resumed = json.loads((sd / "state.json").read_text())
    finally:
        _kill(proc)
    assert resumed["seeded"] is True
    assert resumed["queue"] == QUEUE_STATE["queue"]
    assert resumed["cursors"]["place-a"] == "pos-41"
    assert real.read_text() == original


def test_h4_the_copy_itself_is_byte_for_byte(work):
    """H4: the printed command alone (the watcher already stopped, so nothing
    rewrites state.json) yields a regular file whose bytes equal the original."""
    sd = work / "it's a state dir"
    sd.mkdir()
    real = sd / "real-state.json"
    real.write_text(json.dumps(QUEUE_STATE))
    os.symlink(real, sd / "state.json")
    proc = _spawn(sd)
    try:
        command = _printed_command(_await_idle_line(sd))
    finally:
        _kill(proc)
    done = subprocess.run(["bash", "-c", command], cwd=sd, capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, (command, done.stderr)
    assert (sd / "state.json").read_bytes() == real.read_bytes()
    assert stat.S_ISREG(os.lstat(sd / "state.json").st_mode)
    assert not (sd / "state.json.new").exists()


# ---- H5: EACCES on the status temp file degrades, never ends the process ----

def _unwritable_dir(work):
    """A 0500 directory holding no status file, or skip when the mode does not make
    creating a file fail (root, or a filesystem that ignores modes)."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    d = work / "ro"
    d.mkdir()
    os.chmod(d, 0o500)
    try:
        fd = os.open(d / "probe", os.O_WRONLY | os.O_CREAT, 0o600)
    except PermissionError:
        return d
    os.close(fd)
    os.chmod(d, 0o700)
    pytest.skip("chmod 0500 does not stop file creation on this filesystem")


@pytest.fixture
def ro_dir(work):
    d = _unwritable_dir(work)
    try:
        yield d
    finally:
        os.chmod(d, 0o700)


def test_h5_codex_update_status_eacces_degrades_with_one_line(work, ro_dir):
    """H5: PermissionError opening status.json.tmp -> update_status returns, ONE
    stderr line across 3 calls, no traceback.  Mutation: the catch narrowed to
    (NotRegularFile, IsADirectoryError)."""
    r = child(WAKE, ro_dir, _PATHS + r"""
for _ in range(3):
    m.update_status(paths, {"a": 1})
result = {"exists": os.path.lexists(os.path.join(tmp, "status.json"))}
""")
    assert r["exists"] is False
    lines = [ln for ln in r["_stderr"].splitlines() if "status not written" in ln]
    assert len(lines) == 1, r["_stderr"]
    assert "Traceback" not in r["_stderr"]


def test_h5_supervisor_write_status_eacces_degrades_with_one_line(work, ro_dir):
    """H5: the supervisor's write_status on an unwritable directory returns and
    reports ONE line.  Mutation: the supervisor catch narrowed likewise."""
    r = child(SUP, ro_dir, _sup_obj(r"""
for _ in range(3):
    sup.write_status()
result = {"exists": os.path.lexists(os.path.join(tmp, "status.json"))}
"""))
    assert r["exists"] is False
    lines = [ln for ln in r["_stderr"].splitlines() if "status not written" in ln]
    assert len(lines) == 1, r["_stderr"]
    assert "Traceback" not in r["_stderr"]


def test_h5_supervisor_process_survives_eacces_on_the_status_directory(work):
    """H5 (process): the real supervisor, whose status directory turns unwritable
    after it started, stays alive and ends on SIGTERM.  Mutation: supervisor
    catch narrowed."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    d = work / "sup"
    d.mkdir()
    sup_status = d / "status.json"
    sleeper = json.dumps([sys.executable, "-c", "import time; time.sleep(60)"])
    proc = subprocess.Popen(
        [sys.executable, "-I", str(SUP), "--pidfile", str(work / "p.pid"),
         "--status-file", str(sup_status), "--status-interval", "0.3",
         "--bell-cmd-json", sleeper, "--updates-cmd-json", sleeper, "--mention-cmd-json", sleeper],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(work))
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not sup_status.is_file():
            time.sleep(0.1)
        assert sup_status.is_file()
        sup_status.unlink()
        os.chmod(d, 0o500)
        try:
            os.close(os.open(d / "probe", os.O_WRONLY | os.O_CREAT, 0o600))
        except PermissionError:
            pass
        else:
            pytest.skip("chmod 0500 does not stop file creation on this filesystem")
        _alive_for(proc, 1.5)
    except BaseException:
        os.chmod(d, 0o700)
        _kill(proc)
        raise
    try:
        proc.send_signal(signal.SIGTERM)
        _o, err = proc.communicate(timeout=6)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("the supervisor did not end on SIGTERM")
    finally:
        os.chmod(d, 0o700)
    assert "Traceback" not in err, err
    assert err.count("status not written") == 1, err
