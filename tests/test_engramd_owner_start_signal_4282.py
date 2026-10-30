"""A SIGTERM / SIGINT that arrives while the owner is still inside start() -- issue #4282.

Contract (the design ruling on #4282, shape B: defer the stop while starting):

  C1. A signal that arrives during start() is recorded, not executed. start()
      completes, then runs stop() exactly once.
  C2. After C1: exit 0, the socket removed, our pidfile removed, and no
      traceback on stderr.
  C3. main() does not print "listening on ..." when a stop is already pending
      after start() returns, and wait_forever() returns promptly.
  C4. A signal after start() has returned behaves as before (the other owner
      test files are unchanged).
  C5. One deterministic injected-signal cell per window. Each runs the real
      main() in a CHILD process; a seam patched in before main() sends the
      signal to the child itself at the exact point (Python runs the handler at
      the next bytecode of the main thread, so this is deterministic):

        window 1: the accept thread is assigned but not started (the patched
                  Thread.start sends the signal, then starts) -- unfixed code:
                  stop() joins an unstarted thread, RuntimeError, exit 1.
        window 2: after the handlers are installed, at `_shutdown.clear()` (the
                  patched clear() sends the signal, then clears) -- unfixed
                  code: stop() runs, then start() clears _shutdown again, and
                  wait_forever() never returns: a HANG.
        window 3: right after the pidfile write, before any handler exists in
                  unfixed code (the patched _write_pidfile writes, then sends
                  the signal) -- unfixed code: default action, killed by the
                  signal, stale socket and pidfile.
        window 4: window 3's seam, then a SECOND signal sent from inside the
                  deferred stop() -- it must only be recorded (C1: stop() runs
                  exactly once even when a second signal lands then).

Every cell runs under a kill timeout, so a hang is a red cell. Each cell is
parametrised over SIGTERM and SIGINT.

Mutations that must redden these cells: the handler calls stop() directly again
(all windows); the deferring handler is installed after the pidfile write
(window 3).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

from _owner_platform import requires_supported_owner  # noqa: E402
from _owner_store import seed_current_store  # noqa: E402
import engramd_owner as eo  # noqa: E402

pytestmark = requires_supported_owner

CHILD_TIMEOUT = 15.0

_CHILD = r"""
import os, signal, sys, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import engramd_owner as eo

window = sys.argv[2]
signum = getattr(signal, sys.argv[3])
home = Path(sys.argv[4])
fired = []


def fire():
    if not fired:  # once only
        fired.append(1)
        os.kill(os.getpid(), signum)


# Count stop() calls (C1: exactly once), one line per call.
real_stop = eo.EngramdOwner.stop
second = []


def counting_stop(self):
    with open(home / "stop_calls.txt", "a") as fh:
        fh.write("stop\n")
    if window == "4" and not second:  # a second signal while the deferred stop() runs
        second.append(1)
        os.kill(os.getpid(), signum)
    return real_stop(self)


eo.EngramdOwner.stop = counting_stop

if window == "1":
    real_thread_start = threading.Thread.start

    def thread_start(self):
        if getattr(self._target, "__name__", "") == "_accept_loop":
            fire()  # the accept thread is assigned, not yet started
        return real_thread_start(self)

    threading.Thread.start = thread_start
elif window == "2":
    class SeamEvent(threading.Event):
        def clear(self):
            fire()  # after the handlers are installed, before the clear
            super().clear()

    real_init = eo.EngramdOwner.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        self._shutdown = SeamEvent()

    eo.EngramdOwner.__init__ = init
elif window in ("3", "4"):
    real_write = eo.EngramdOwner._write_pidfile

    def write_pidfile(self):
        real_write(self)
        fire()  # the pidfile exists; in unfixed code no handler does yet

    eo.EngramdOwner._write_pidfile = write_pidfile
elif window == "5":
    class SeamStopEvent(threading.Event):
        checks = 0

        def is_set(self):
            SeamStopEvent.checks += 1
            if SeamStopEvent.checks == 1:
                fire()  # recorded (still starting) after start()'s FIRST check has looked...
                return False  # ...so that check answers "nothing pending": only the second can see it
            return super().is_set()

    real_init = eo.EngramdOwner.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        self._stop_requested = SeamStopEvent()

    eo.EngramdOwner.__init__ = init
else:
    raise SystemExit("unknown window")

sys.exit(eo.main())
"""


@pytest.fixture
def home():
    d = tempfile.mkdtemp(prefix="p68-")  # short: AF_UNIX path limits
    seed_current_store(d)
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _run(home: Path, window: str, signame: str):
    env = dict(os.environ, ENGRAM_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, _SRC_ENGRAM, window, signame, str(home)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        _out, err = proc.communicate(timeout=CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        _out, err = proc.communicate()
        pytest.fail(
            f"window {window}/{signame}: owner HUNG past {CHILD_TIMEOUT}s (killed); stderr={err!r}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return proc.returncode, err


def _assert_clean_stop(home: Path, rc: int, err: str, window: str, signame: str) -> None:
    tag = f"window {window}/{signame}"
    assert rc == 0, f"{tag}: exit {rc}, expected 0; stderr={err!r}"
    assert "Traceback" not in err, f"{tag}: traceback on stderr: {err!r}"
    assert not os.path.lexists(home / "run" / "engramd.sock"), f"{tag}: socket left behind"
    assert not os.path.lexists(home / eo.DEFAULT_PID_NAME), f"{tag}: pidfile left behind"
    # C3: a pending stop must not announce a listening owner.
    assert "listening on" not in err, f"{tag}: printed 'listening on' with a stop pending: {err!r}"
    # C1: stop() ran exactly once.
    calls = (home / "stop_calls.txt").read_text().splitlines() if (home / "stop_calls.txt").exists() else []
    assert calls == ["stop"], f"{tag}: stop() ran {len(calls)} times, expected exactly 1"


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT"])
def test_window1_signal_before_accept_thread_started(home, signame):
    """Unfixed: stop() joins an unstarted thread -> RuntimeError, exit 1."""
    rc, err = _run(home, "1", signame)
    _assert_clean_stop(home, rc, err, "1", signame)


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT"])
def test_window2_signal_between_handler_install_and_shutdown_clear(home, signame):
    """Unfixed: stop() runs, start() then clears _shutdown, wait_forever() never returns (HANG)."""
    rc, err = _run(home, "2", signame)
    _assert_clean_stop(home, rc, err, "2", signame)


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT"])
def test_window3_signal_after_pidfile_write_before_handlers(home, signame):
    """Unfixed: no handler yet -> default action, killed by the signal, stale socket and pidfile."""
    rc, err = _run(home, "3", signame)
    _assert_clean_stop(home, rc, err, "3", signame)


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT"])
def test_window4_second_signal_during_the_deferred_stop(home, signame):
    """C1 with two signals: one recorded during start() (window 3's seam), then a second sent while
    the deferred stop() runs. The second must only be recorded, not run stop() again. Unfixed
    (start() clears `_starting` before running the deferred stop()): stop() runs twice."""
    rc, err = _run(home, "4", signame)
    _assert_clean_stop(home, rc, err, "4", signame)


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT"])
def test_window5_signal_in_the_gap_between_the_two_checks(home, signame):
    """A signal recorded after start()'s first `_stop_requested` check has answered "nothing pending"
    and before `_starting` drops. Only the second check (after the clear) can see it. Without that
    check stop() never runs and wait_forever() never returns (HANG)."""
    rc, err = _run(home, "5", signame)
    _assert_clean_stop(home, rc, err, "5", signame)


_CHILD_RESTORE = r"""
import json, os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import engramd_owner as eo

home = Path(sys.argv[2])
during_cleanup = sys.argv[3] == "1"
stops = []
real_stop = eo.EngramdOwner.stop


def counting_stop(self):
    stops.append(1)
    return real_stop(self)


eo.EngramdOwner.stop = counting_stop
before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
if during_cleanup:
    # A SIGTERM while the failed start's cleanup runs (the maintenance-lock release, the last cleanup
    # step). With the handlers already restored to the default, it would kill this process mid-cleanup.
    real_release = eo.EngramdOwner._release_maint_lock

    def release(self):
        os.kill(os.getpid(), signal.SIGTERM)
        return real_release(self)

    eo.EngramdOwner._release_maint_lock = release
owner = eo.EngramdOwner(home)
os.mkfifo(owner.pid_path)  # a planted FIFO pidfile: start() must refuse AFTER installing its handlers
refused = None
try:
    owner.start()
except BaseException as exc:
    refused = type(exc).__name__
after = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
print(json.dumps({"refused": refused, "stops": len(stops),
                  "restored": all(before[s] is after[s] or before[s] == after[s] for s in before)}))
"""


@pytest.mark.parametrize("during_cleanup", ["0", "1"], ids=["quiet", "sigterm_during_cleanup"])
def test_failed_start_restores_the_previous_handlers(home, during_cleanup):
    """The docstring's promise: a start() that fails leaves the process's SIGTERM/SIGINT handlers as it
    found them. Unrestored, the failed owner's deferring handler would stay installed. With a SIGTERM
    sent during the failure cleanup, the deferring handler must still be in place (the signal is only
    recorded, then dropped): restored too early, the default action kills the process mid-cleanup."""
    env = dict(os.environ, ENGRAM_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_RESTORE, _SRC_ENGRAM, str(home), during_cleanup],
        env=env, capture_output=True, text=True, timeout=CHILD_TIMEOUT,
    )
    assert proc.returncode == 0, f"child exited {proc.returncode}; stderr={proc.stderr[-2000:]!r}"
    import json
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["refused"], f"start() did not refuse with a FIFO pidfile: {result}"
    assert result["restored"], f"a failed start() left its own signal handlers installed: {result}"
    # The signal during cleanup is RECORDED, never executed: no stop() runs on the failed owner.
    assert result["stops"] == 0, f"stop() ran during a failed start's cleanup: {result}"
