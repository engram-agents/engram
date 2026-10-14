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


# =====================================================================
# #3793 -- Logger.log must not raise on ANY OSError (the log is never a
# kill switch); #3795 -- copy advice only for a link that resolves to a
# regular file; #3796 -- two plain reads must not block on a FIFO.
# =====================================================================

import errno

needs_nonroot = pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses file modes: a real EACCES cannot be produced")

_BROKEN_STREAM = r"""
class Broken:
    def __init__(self, exc): self.exc = exc
    def write(self, s): raise self.exc
    def flush(self): raise self.exc
"""


@needs_nonroot
def test_1a_watcher_survives_an_unwritable_log_file_and_names_the_errno(work):
    """1a: watcher.log is a regular 0o400 file (the state dir stays writable).  A
    healthy idle watcher logs nothing, so a refused umwelt config (a FIFO) makes it
    log: that one line must reach the degraded logger.  The real watcher stays alive
    over many cycles, ends rc 0 on SIGTERM, stderr carries exactly ONE refusal line
    naming EACCES and the log path, and the log line itself reaches stderr.
    Mutations: `except OSError` removed (the watcher dies, rc 1); catching only
    NotRegularFile; refusal on every line; no errno name; no path."""
    log = work / "watcher.log"
    log.write_text("")
    os.chmod(log, 0o400)
    cfg = work / "umwelt-fifo"
    os.mkfifo(cfg)
    proc = _spawn(work, _config_args(cfg))
    try:
        _alive_for(proc, 3.0)
    except BaseException:
        _kill(proc)
        raise
    _out, err = _end(proc)
    assert proc.returncode == 0, err[-400:]
    assert "Traceback" not in err, err
    lines = err.splitlines()
    refusals = [ln for ln in lines if "log write failed" in ln]
    assert len(refusals) == 1, lines
    assert "EACCES" in refusals[0] and str(log) in refusals[0], refusals
    logged = [ln for ln in lines if "not a regular file (FIFO)" in ln and "log write failed" not in ln]
    assert len(logged) == 1 and str(cfg) in logged[0], lines
    assert log.read_text() == ""


@needs_nonroot
def test_1b_logger_recovers_when_the_file_becomes_writable_and_reports_again(work):
    """1b: refused (0o400) -> chmod 0o600 -> the next line lands in the FILE and
    degraded is False -> 0o400 again -> the refusal is reported AGAIN.
    Mutations: the reported flag never resets; degraded never cleared."""
    r = child(WAKE, work, r"""
log = Path(tmp) / "watcher.log"
log.write_text(""); os.chmod(log, 0o400)
lg = m.Logger(log, stdout=False)
lg.log("one"); d1 = lg.degraded
os.chmod(log, 0o600)
lg.log("two"); d2 = lg.degraded
os.chmod(log, 0o400)
lg.log("three"); d3 = lg.degraded
result = {"d": [d1, d2, d3], "text": log.read_text()}
""")
    assert r["d"] == [True, False, True]
    assert r["text"].count("two") == 1 and "one" not in r["text"] and "three" not in r["text"]
    err = r["_stderr"].splitlines()
    assert sum("log write failed" in ln for ln in err) == 2, err
    assert sum("two" in ln for ln in err) == 0


@pytest.mark.parametrize("name", ["ENOSPC", "EROFS", "EDQUOT", "EIO", "EACCES"])
def test_1c_logger_never_raises_on_any_oserror_and_names_that_errno(work, name):
    """1c: the file append raises OSError(errno.X): Logger.log returns, the line goes
    to stderr, and the refusal line carries THAT errno's name and the path.
    Mutations: catch only EACCES (or EACCES+ENOSPC): the others propagate; the
    refusal line without the errno name; without the path."""
    r = child(WAKE, work, r"""
import errno
def boom(path, line): raise OSError(getattr(errno, sys.argv[3]), "injected")
m.append_regular_line = boom
lg = m.Logger(Path(tmp) / "watcher.log", stdout=False)
lg.log("the-line")
result = {"degraded": lg.degraded}
""".replace("sys.argv[3]", repr(name)))
    assert r["degraded"] is True
    err = r["_stderr"].splitlines()
    refusal = [ln for ln in err if "log write failed" in ln]
    assert len(refusal) == 1, err
    assert name in refusal[0] and str(work / "watcher.log") in refusal[0], refusal
    assert sum("the-line" in ln for ln in err) == 1
    others = {"ENOSPC", "EROFS", "EDQUOT", "EIO", "EACCES"} - {name}
    assert not any(o in refusal[0] for o in others)


@pytest.mark.parametrize("case,exc_src,expect", [
    ("no-errno", 'OSError("boom")', "OSError"),
    ("unmapped-errno", 'OSError(9999, "weird")', "errno 9999"),
])
def test_1c_logger_names_an_oserror_without_a_mapped_errno(work, case, exc_src, expect):
    """1c: the append raises an OSError whose errno is None, or one absent from
    errno.errorcode: Logger.log still returns, stderr carries ONE refusal line naming
    the class (`OSError`) / `errno 9999` plus the log path, and the data line.
    Mutation: `errno.errorcode[exc.errno]` (KeyError out of the log call)."""
    r = child(WAKE, work, r"""
def boom(path, line): raise EXC_SRC
m.append_regular_line = boom
lg = m.Logger(Path(tmp) / "watcher.log", stdout=False)
lg.log("the-line")
result = {"degraded": lg.degraded}
""".replace("EXC_SRC", exc_src))
    assert r["degraded"] is True
    err = r["_stderr"].splitlines()
    refusal = [ln for ln in err if "log write failed" in ln]
    assert len(refusal) == 1, err
    assert expect in refusal[0] and str(work / "watcher.log") in refusal[0], refusal
    assert sum("the-line" in ln for ln in err) == 1, err


def test_1d_refusal_is_reported_once_across_consecutive_failures(work):
    """1d: 5 failing calls -> ONE refusal line and 5 log lines on stderr.
    Mutation: the refusal printed on every failing call."""
    r = child(WAKE, work, r"""
import errno
def boom(path, line): raise OSError(errno.EACCES, "injected")
m.append_regular_line = boom
lg = m.Logger(Path(tmp) / "watcher.log", stdout=False)
for i in range(5):
    lg.log(f"message-{i}")
result = {}
""")
    err = r["_stderr"].splitlines()
    assert sum("log write failed" in ln for ln in err) == 1, err
    for i in range(5):
        assert sum(f"message-{i}" in ln for ln in err) == 1, err


@pytest.mark.parametrize("stream", ["epipe", "closed"])
@pytest.mark.parametrize("path_kind", ["fifo", "eacces"])
def test_1e_a_broken_stderr_drops_the_line_silently(work, stream, path_kind):
    """1e: the stderr fallback itself never raises (a write raising EPIPE; a closed
    file raising ValueError), for a NotRegularFile path and an injected EACCES path.
    Mutations: the fallback print unguarded (each stream kind separately)."""
    r = child(WAKE, work, _BROKEN_STREAM + r"""
import errno, io
if SK == "fifo":
    os.mkfifo(Path(tmp) / "watcher.log")
else:
    def boom(path, line): raise OSError(errno.EACCES, "injected")
    m.append_regular_line = boom
lg = m.Logger(Path(tmp) / "watcher.log", stdout=False)
real = sys.stderr
if ST == "epipe":
    sys.stderr = Broken(OSError(errno.EPIPE, "broken pipe"))
else:
    f = io.StringIO(); f.close(); sys.stderr = f
lg.log("a"); lg.log("b")
sys.stderr = real
result = {"degraded": lg.degraded}
""".replace("SK", repr(path_kind)).replace("ST", repr(stream)))
    assert r["degraded"] is True


@pytest.mark.parametrize("stream", ["epipe", "closed"])
def test_1e_a_broken_stdout_echo_never_raises(work, stream):
    """1e: stdout=True with a broken stdout (and a healthy log): Logger.log returns.
    Mutation: the stdout echo unguarded."""
    r = child(WAKE, work, _BROKEN_STREAM + r"""
import errno, io
lg = m.Logger(Path(tmp) / "watcher.log", stdout=True)
real = sys.stdout
if ST == "epipe":
    sys.stdout = Broken(OSError(errno.EPIPE, "broken pipe"))
else:
    f = io.StringIO(); f.close(); sys.stdout = f
lg.log("a"); lg.log("b")
sys.stdout = real
result = {"text": (Path(tmp) / "watcher.log").read_text()}
""".replace("ST", repr(stream)))
    assert r["text"].count("\n") == 2


def test_1c_notregularfile_message_is_unchanged_and_wins_over_the_oserror_clause(work):
    """The NotRegularFile clause stays FIRST: a FIFO log still says `log file <path> is
    a FIFO, not a regular file`, not the generic OSError line.  Mutation: `except
    OSError` placed before `except NotRegularFile`."""
    os.mkfifo(work / "watcher.log")
    r = child(WAKE, work, r"""
lg = m.Logger(Path(tmp) / "watcher.log", stdout=False)
lg.log("x")
result = {}
""")
    err = r["_stderr"]
    assert "log file " in err and "is a FIFO, not a regular file" in err, err
    assert "log write failed" not in err


# ---- #3795 ---------------------------------------------------------------

def _plant_state_case(work, case):
    real = work / "real-state.json"
    real.write_text(json.dumps(QUEUE_STATE))
    state = work / "state.json"
    if case == "link-reg":
        os.symlink(real, state)
    elif case == "link-chain":
        os.symlink(real, work / "mid")
        os.symlink(work / "mid", state)
    elif case == "hardlink":
        os.link(real, state)
    elif case == "link-fifo":
        os.mkfifo(work / "tf")
        os.symlink(work / "tf", state)
    elif case == "link-dir":
        os.mkdir(work / "td")
        os.symlink(work / "td", state)
    elif case == "link-dangling":
        os.symlink(work / "nowhere", state)
    elif case == "link-loop":
        os.symlink(work / "loopb", state)
        os.symlink(state, work / "loopb")


COPY_CASES = ["link-reg", "link-chain", "hardlink"]
REMOVE_CASES = ["link-fifo", "link-dir", "link-dangling", "link-loop"]


@pytest.mark.parametrize("case", COPY_CASES + REMOVE_CASES)
def test_2_state_advice_copy_only_when_the_link_resolves_to_a_regular_file(work, case):
    """2a-2g, from the REAL watcher's log line: a symlink (also a chain) or hard link
    that ends at a regular file keeps the `cp --` advice; a symlink to a FIFO /
    directory, a dangling link and a loop get `Remove it to resume` and no `cp --`.
    A hang in the advice is a missing line -> failure after 8 s.  Mutations: advice
    for every symlink; os.lstat for os.stat (chain); exists() for S_ISREG; the
    target opened to test it; the hard-link kind losing the advice."""
    _plant_state_case(work, case)
    proc = _spawn(work)
    try:
        line = _await_idle_line(work)
        assert proc.poll() is None
    finally:
        _kill(proc)
    if case in COPY_CASES:
        assert "cp -- " in line and REMOVE_ADVICE not in line, line
    else:
        assert "cp --" not in line and REMOVE_ADVICE in line, line


@pytest.mark.parametrize("case", ["link-fifo", "link-dangling", "link-loop", "link-dir"])
def test_2_state_refusal_advice_function_returns_for_unresolvable_links(work, case):
    """The function itself, in a child with a hard bound: it returns the remove advice,
    never raises, never blocks.  Mutation: the target opened (hangs on the FIFO)."""
    _plant_state_case(work, case)
    r = child(WAKE, work, r"""
p = os.path.join(tmp, "state.json")
exc = m.NotRegularFile(p, os.lstat(p).st_mode)
result = {"kind": exc.kind, "advice": m.state_refusal_advice(m.StateRefused("read", exc))}
""")
    assert r["kind"] == "symlink"
    assert r["advice"] == REMOVE_ADVICE


# ---- #3796 ---------------------------------------------------------------

def _marker(path, session_id="sess-1"):
    path.write_text(json.dumps({"session_id": session_id, "role": "interactive",
                                "transcript_path": "/x/.codex/sessions/t.jsonl",
                                "started_at": "2026-01-01T00:00:00+00:00"}))


def test_3a_list_session_candidates_skips_a_fifo_marker_silently(work):
    """3a: a valid marker and a FIFO named x.json: returns within the bound, lists the
    valid marker, prints nothing; no reader ever opened the FIFO side (a writer-less
    FIFO still refuses a non-blocking write-open).  Mutations: read_text restored
    (hangs); a FIFO marker reported."""
    sd = work / "sessions"
    sd.mkdir()
    _marker(sd / "ok.json")
    os.mkfifo(sd / "x.json")
    r = child(WAKE, work, r"""
cands = m.list_session_candidates(Path(tmp) / "sessions")
result = {"ids": [c.thread_id for c in cands]}
""")
    assert r["ids"] == ["sess-1"]
    assert r["_stderr"] == ""
    with pytest.raises(OSError) as ei:
        os.open(sd / "x.json", os.O_WRONLY | os.O_NONBLOCK)
    assert ei.value.errno == errno.ENXIO


def test_3b_list_session_candidates_follows_symlinks_and_skips_directories(work):
    """3b: a symlinked marker still lists; a directory named d.json is skipped.
    Mutation: a non-following reader (the symlink marker vanishes)."""
    sd = work / "sessions"
    sd.mkdir()
    _marker(work / "real-marker.dat", "via-link")
    os.symlink(work / "real-marker.dat", sd / "link.json")
    os.mkdir(sd / "d.json")
    r = child(WAKE, work, r"""
cands = m.list_session_candidates(Path(tmp) / "sessions")
result = {"ids": [c.thread_id for c in cands]}
""")
    assert r["ids"] == ["via-link"]
    assert r["_stderr"] == ""


def test_3a_list_session_candidates_survives_a_marker_that_vanishes_after_the_read(work):
    """3a: a marker that reads fine but whose stat() then raises FileNotFoundError
    (it vanished between the read and the recency stat): the function returns the other
    valid markers and does not raise.  Mutation: the unguarded `path.stat()` in the
    candidate's recency tuple (FileNotFoundError escapes)."""
    sd = work / "sessions"
    sd.mkdir()
    _marker(sd / "ok.json")
    _marker(sd / "gone.json", "gone-id")
    r = child(WAKE, work, r"""
gone = str(Path(tmp) / "sessions" / "gone.json")
real_stat = Path.stat
calls = []
def fake_stat(self, *a, **kw):
    if str(self) == gone:
        calls.append(1)
        raise FileNotFoundError(2, "vanished", gone)
    return real_stat(self, *a, **kw)
Path.stat = fake_stat
try:
    cands = m.list_session_candidates(Path(tmp) / "sessions")
    out = {"exc": None, "ids": [c.thread_id for c in cands]}
except BaseException as exc:
    out = {"exc": type(exc).__name__, "ids": None}
finally:
    Path.stat = real_stat
out["calls"] = len(calls)
result = out
""")
    assert r["exc"] is None, r
    assert r["ids"] == ["sess-1"], r
    assert r["calls"] >= 1, r


@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_3c_load_umwelt_context_reports_a_non_regular_config(work, kind):
    """3c: a FIFO / directory config: no hang, raises NotRegularFile naming the kind.
    Mutations: read_text restored (hangs); NotRegularFile swallowed inside."""
    plant(kind, work / "cfg.json")
    r = child(WAKE, work, r"""
try:
    m.load_umwelt_context(Path(tmp) / "cfg.json")
    out = {"exc": None}
except m.NotRegularFile as exc:
    out = {"exc": "NotRegularFile", "kind": exc.kind, "path": exc.path}
result = out
""")
    assert r["exc"] == "NotRegularFile", r
    assert r["kind"] == KIND_WORD[kind] and r["path"] == str(work / "cfg.json")


def test_3c_load_umwelt_context_unchanged_for_missing_corrupt_and_symlinked(work):
    """3c: missing -> None; corrupt -> None; a symlink to a valid config -> the
    context; a symlink to a FIFO reports as a FIFO.  Mutation: a non-following reader."""
    (work / "corrupt.json").write_text("{not json")
    (work / "valid.json").write_text(json.dumps({"agent": "Tester", "world_url": "http://w/"}))
    os.symlink(work / "valid.json", work / "link.json")
    os.mkfifo(work / "tf")
    os.symlink(work / "tf", work / "linkfifo.json")
    r = child(WAKE, work, r"""
def go(name):
    try:
        c = m.load_umwelt_context(Path(tmp) / name)
        return None if c is None else [c.self_name, c.world_url]
    except m.NotRegularFile as exc:
        return "NRF:" + exc.kind
result = {n: go(n) for n in ("missing.json", "corrupt.json", "link.json", "linkfifo.json")}
""")
    assert r["missing.json"] is None and r["corrupt.json"] is None
    assert r["link.json"] == ["tester", "http://w"]
    assert r["linkfifo.json"] == "NRF:FIFO"


def test_3c_load_umwelt_context_stats_the_config_once(work):
    """3c: the decision comes from ONE stat.  A path that reads as a FIFO on the
    first stat and is gone on the second still reports NotRegularFile (FIFO), never
    FileNotFoundError, and the config path is stat'ed exactly once.  Mutation: a
    second os.stat after the kind check (the FileNotFoundError escapes)."""
    r = child(WAKE, work, r"""
import stat as _stat
target = os.path.join(tmp, "cfg.json")
real_stat = os.stat
calls = []
def fake_stat(p, *a, **kw):
    if str(p) != target:
        return real_stat(p, *a, **kw)
    calls.append(1)
    if len(calls) == 1:
        return os.stat_result((_stat.S_IFIFO | 0o600, 1, 1, 1, 0, 0, 0, 0, 0, 0))
    raise FileNotFoundError(2, "gone", target)
os.stat = fake_stat
try:
    m.load_umwelt_context(Path(target))
    out = {"exc": None}
except m.NotRegularFile as exc:
    out = {"exc": "NotRegularFile", "kind": exc.kind}
except BaseException as exc:
    out = {"exc": type(exc).__name__}
finally:
    os.stat = real_stat
out["calls"] = len(calls)
result = out
""")
    assert r["exc"] == "NotRegularFile", r
    assert r["kind"] == "FIFO" and r["calls"] == 1, r


def _config_args(cfg):
    return ("--umwelt-config", str(cfg))


def _await(pred, timeout=8.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(0.1)
    pytest.fail(f"{what} not reached within {timeout}s")


def _cfg_reports(work):
    return [ln for ln in _log_lines(work) if "umwelt config" in ln and "not a regular file" in ln]


def _status(work):
    try:
        return json.loads((work / "status.json").read_text())
    except (OSError, ValueError):
        return {}


def test_3d_watcher_with_a_fifo_umwelt_config_reports_once_and_waits(work):
    """3d: the real watcher with --umwelt-config <FIFO>: alive over many cycles, ONE
    log line naming path + kind, status waiting_for_umwelt with path/kind in detail,
    rc 0 on SIGTERM.  Mutations: read_text restored (hangs, no cycle); NotRegularFile
    swallowed (no report); reported every cycle; run_cycle re-raises (the watcher
    dies, mode never waiting)."""
    cfg = work / "umwelt-fifo"
    os.mkfifo(cfg)
    proc = _spawn(work, _config_args(cfg))
    try:
        _alive_for(proc, 3.0)
        st = _status(work)
    except BaseException:
        _kill(proc)
        raise
    _out, err = _end(proc)
    assert proc.returncode == 0, err[-400:]
    assert "Traceback" not in err, err
    reports = _cfg_reports(work)
    assert len(reports) == 1, _log_lines(work)
    assert str(cfg) in reports[0] and "FIFO" in reports[0]
    assert st.get("mode") == "waiting_for_umwelt", st
    assert str(cfg) in st["detail"] and "FIFO" in st["detail"], st
    # Nobody opened the FIFO: a non-blocking write-open still finds no reader.
    with pytest.raises(OSError) as ei:
        os.open(cfg, os.O_WRONLY | os.O_NONBLOCK)
    assert ei.value.errno == errno.ENXIO


def test_3e_config_report_follows_transitions(work):
    """3e: FIFO reported; removed (config missing) -> no new report; FIFO planted again
    -> reported AGAIN; replaced with a valid config -> the polling path runs.
    Mutations: flag never reset (no second report); reported every cycle."""
    cfg = work / "umwelt.json"
    os.mkfifo(cfg)
    proc = _spawn(work, _config_args(cfg))
    try:
        _await(lambda: _cfg_reports(work), what="first report")
        os.unlink(cfg)
        time.sleep(1.2)
        assert len(_cfg_reports(work)) == 1
        os.mkfifo(cfg)
        _await(lambda: len(_cfg_reports(work)) == 2, what="second report")
        time.sleep(1.0)
        assert len(_cfg_reports(work)) == 2
        os.unlink(cfg)
        cfg.write_text(json.dumps({"agent": "tester", "world_url": "http://127.0.0.1:1"}))
        _await(lambda: any("umwelt poll failed" in ln for ln in _log_lines(work)),
               what="the polling path after a valid config")
        assert proc.poll() is None
        assert len(_cfg_reports(work)) == 2
    finally:
        _kill(proc)


def test_3f_config_and_state_refusals_do_not_suppress_each_other(work):
    """3f: config FIFO reported first, THEN the state file becomes a FIFO: the state
    refusal is reported too; and the other way round (state refused first, repaired,
    then the config FIFO is reported).  Mutation: one shared flag."""
    cfg = work / "umwelt.json"
    os.mkfifo(cfg)
    proc = _spawn(work, _config_args(cfg))
    try:
        _await(lambda: _cfg_reports(work), what="config report")
        if (work / "state.json").exists():
            os.unlink(work / "state.json")
        os.mkfifo(work / "state.json")
        _await(lambda: [ln for ln in _log_lines(work) if "idling, will recheck" in ln],
               what="state report after the config report")
    finally:
        _kill(proc)


def test_3f_state_refused_first_then_config_is_still_reported(work):
    """3f (reverse): state FIFO reported (idle), repaired, then the config FIFO is
    reported on the cycle that runs.  Mutation: one shared flag that the state
    refusal sets and nothing clears for the config."""
    cfg = work / "umwelt.json"
    os.mkfifo(cfg)
    os.mkfifo(work / "state.json")
    proc = _spawn(work, _config_args(cfg))
    try:
        _await(lambda: [ln for ln in _log_lines(work) if "idling, will recheck" in ln],
               what="state report")
        assert _cfg_reports(work) == []
        os.unlink(work / "state.json")
        _await(lambda: _cfg_reports(work), what="config report after the state repaired")
    finally:
        _kill(proc)


def test_3g_regular_config_behaves_as_before(work):
    """3g: a regular config: no config report, the poll path runs."""
    cfg = work / "umwelt.json"
    cfg.write_text(json.dumps({"agent": "tester", "world_url": "http://127.0.0.1:1"}))
    proc = _spawn(work, _config_args(cfg))
    try:
        _await(lambda: any("umwelt poll failed" in ln for ln in _log_lines(work)), what="poll")
        assert _cfg_reports(work) == []
        assert _status(work).get("mode") != "waiting_for_umwelt" or "poll failed" in _status(work).get("detail", "")
    finally:
        _kill(proc)
