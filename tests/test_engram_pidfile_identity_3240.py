"""Contract tests for engram-alpha#3240 item 1 -- a recorded pid counts as
alive only if that process could have WRITTEN the pidfile.

Written BEFORE src/engram/engram_pidfile.py exists (contract-first). The
defect: os.kill(pid, 0) alone reads a pid REUSED by an unrelated live
process as a live instance, so a restart is refused until that process
exits. Contract: never refuse forever. A process that started after the
pidfile was last written cannot be its writer (Linux, via /proc).

Test names P1..P7 follow the spec. Helper-internal names the spec leaves to
the implementer and these tests pin: `recorded_pid_is_alive(pid_path)`,
`_read_process_start_time(pid) -> float | None`,
`_parse_starttime_ticks(stat_text) -> int`,
`_started_after_pidfile(start_time, pidfile_mtime) -> bool`,
`_START_TIME_TOLERANCE_S`.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

DAEMON_PY_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py"

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="start-time identity uses /proc (Linux)"
)


@pytest.fixture
def home():
    d = tempfile.mkdtemp(prefix="epi-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def sleeper():
    proc = subprocess.Popen(["sleep", "30"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=5)


def _stale_pidfile(path: Path, pid: int, age_s: float = 60.0) -> None:
    """A pidfile 'written by a crashed instance long ago' naming `pid`."""
    path.write_text(str(pid))
    past = time.time() - age_s
    os.utime(path, (past, past))


def _helper():
    import engram_pidfile

    return engram_pidfile


def _load_daemon_module():
    spec = importlib.util.spec_from_file_location("engram_surface_daemon_3240", str(DAEMON_PY_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- P1: reused pid -> not alive (the defect), at three levels -------------


@linux_only
def test_P1_helper_reused_pid_is_not_alive(home, sleeper):
    pidfile = home / "x.pid"
    _stale_pidfile(pidfile, sleeper.pid)
    assert _helper().recorded_pid_is_alive(pidfile) is False


@linux_only
def test_P1_owner_start_succeeds_over_reused_pid(home, sleeper):
    import engramd_owner as eo

    probe = eo.EngramdOwner(home)
    probe.socket_path.parent.mkdir(parents=True, exist_ok=True)
    dummy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dummy.bind(str(probe.socket_path))
    dummy.close()
    _stale_pidfile(probe.pid_path, sleeper.pid)

    owner = eo.EngramdOwner(home)
    owner.start(install_signal_handlers=False)  # must NOT raise OwnerAlreadyRunning
    owner.stop()


@linux_only
def test_P1_daemon_reused_pid_is_not_alive(home, sleeper):
    mod = _load_daemon_module()
    pidfile = home / "recall-daemon.pid"
    _stale_pidfile(pidfile, sleeper.pid)
    mod.PID_PATH = str(pidfile)
    assert mod._recorded_pid_is_alive() is False


# -- P2: the genuine writer -> alive (guard) -------------------------------


def test_P2_live_writer_is_alive(home):
    pidfile = home / "x.pid"
    pidfile.write_text(str(os.getpid()))
    assert _helper().recorded_pid_is_alive(pidfile) is True


# -- P3: dead pid -> not alive (guard) -------------------------------------


def test_P3_dead_pid_is_not_alive(home):
    proc = subprocess.Popen(["sleep", "30"])
    proc.kill()
    proc.wait(timeout=5)
    pidfile = home / "x.pid"
    pidfile.write_text(str(proc.pid))
    assert _helper().recorded_pid_is_alive(pidfile) is False


# -- P4: garbage / missing pidfile -> not alive (guard) --------------------


def test_P4_missing_and_garbage_pidfile_not_alive(home):
    h = _helper()
    assert h.recorded_pid_is_alive(home / "absent.pid") is False
    garbage = home / "g.pid"
    garbage.write_text("not-a-pid\n")
    assert h.recorded_pid_is_alive(garbage) is False


# -- P5: /proc unreadable -> conservative True (never guess dead) ----------


def test_P5_unreadable_proc_is_conservatively_alive(home, monkeypatch):
    h = _helper()
    pidfile = home / "x.pid"
    pidfile.write_text(str(os.getpid()))
    past = time.time() - 3600
    os.utime(pidfile, (past, past))  # would read dead if start time WERE known
    monkeypatch.setattr(h, "_read_process_start_time", lambda pid: None)
    assert h.recorded_pid_is_alive(pidfile) is True


# -- P6: comm with spaces/parens parses ------------------------------------


def test_P6_stat_parser_handles_comm_with_spaces_and_parens():
    h = _helper()
    # Fields after the last ')' start at field 3 (state); starttime is field 22,
    # i.e. index 19 of the post-')' split. Build 20 such fields, last = 987654.
    rest = ["S"] + [str(i) for i in range(1, 19)] + ["987654"]
    assert len(rest) == 20
    line = "1234 (a b) c) " + " ".join(rest) + " 0 0"
    assert h._parse_starttime_ticks(line) == 987654


# -- P7: mtime tolerance ---------------------------------------------------


def test_P7_tolerance_boundary():
    h = _helper()
    tol = h._START_TIME_TOLERANCE_S
    assert tol == 2.0
    m = 1_000_000.0
    assert h._started_after_pidfile(m + tol, m) is False  # == edge: still the writer
    assert h._started_after_pidfile(m + tol - 0.5, m) is False
    assert h._started_after_pidfile(m + tol + 0.5, m) is True
    assert h._started_after_pidfile(m - 100, m) is False


def test_P7b_tolerance_behavioural_boundary():
    """Behavioural boundary only (no constant-equality): 1.9 s after the
    pidfile mtime still reads as the writer; 2.1 s reads as started-after."""
    h = _helper()
    m = 1_000_000.0
    assert h._started_after_pidfile(m + 1.9, m) is False
    assert h._started_after_pidfile(m + 2.1, m) is True


# -- Round 2: program-identity guard (clock drift must not read live as dead) --
#
# `btime + starttime/CLK_TCK` drifts later when the kernel's boottime clock
# stops across a host suspend (WSL2 after sleep + time sync), so a LIVE
# long-running instance can compute as "started after its own pidfile".
# Contract: that condition alone is not enough to call a pid reused -- its
# /proc/<pid>/cmdline must ALSO not carry the caller's program marker.
# Helper-internal name pinned here: `_read_process_cmdline(pid) -> str | None`.

OWNER_MARKER = "engramd_owner"
DAEMON_MARKER = "engram-surface-daemon"


@pytest.fixture
def marker_proc(request):
    """A live process whose argv carries the requested marker (via
    request.param), so /proc/<pid>/cmdline contains it."""
    marker = request.param
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", f"{marker}-probe"]
    )
    # Wait until exec has happened so /proc cmdline shows the final argv.
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with open(f"/proc/{proc.pid}/cmdline", "rb") as f:
                if marker.encode() in f.read():
                    break
        except OSError:
            pass
        time.sleep(0.02)
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=5)


# -- P8: drift case -> alive -----------------------------------------------


@linux_only
@pytest.mark.parametrize("marker_proc", [OWNER_MARKER], indirect=True)
def test_P8_drift_live_process_with_marker_is_alive(home, marker_proc):
    pidfile = home / "x.pid"
    _stale_pidfile(pidfile, marker_proc.pid)  # mtime 60 s BEFORE the process started
    assert _helper().recorded_pid_is_alive(pidfile, program_marker=OWNER_MARKER) is True


# -- P9: reuse with a marker given -> still dead ---------------------------


@linux_only
def test_P9_reused_pid_with_marker_given_is_still_dead(home, sleeper):
    pidfile = home / "x.pid"
    _stale_pidfile(pidfile, sleeper.pid)
    assert _helper().recorded_pid_is_alive(pidfile, program_marker=OWNER_MARKER) is False


# -- P10: cmdline unreadable in the started-after case -> conservative True ---


@linux_only
def test_P10_unreadable_cmdline_is_conservatively_alive(home, sleeper, monkeypatch):
    h = _helper()
    pidfile = home / "x.pid"
    _stale_pidfile(pidfile, sleeper.pid)  # started-after-mtime holds
    monkeypatch.setattr(h, "_read_process_cmdline", lambda pid: None)
    assert h.recorded_pid_is_alive(pidfile, program_marker=OWNER_MARKER) is True


# -- P11: owner and daemon actually pass their markers ---------------------


@linux_only
@pytest.mark.parametrize("marker_proc", [OWNER_MARKER], indirect=True)
def test_P11_owner_does_not_steal_from_drifted_live_owner(home, marker_proc):
    import engramd_owner as eo

    probe = eo.EngramdOwner(home)
    probe.socket_path.parent.mkdir(parents=True, exist_ok=True)
    dummy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dummy.bind(str(probe.socket_path))
    dummy.close()
    _stale_pidfile(probe.pid_path, marker_proc.pid)

    owner = eo.EngramdOwner(home)
    with pytest.raises(eo.OwnerAlreadyRunning):
        owner.start(install_signal_handlers=False)
    assert probe.socket_path.exists()  # the live owner's socket was not unlinked


@linux_only
@pytest.mark.parametrize("marker_proc", [DAEMON_MARKER], indirect=True)
def test_P11_daemon_reads_drifted_live_daemon_as_alive(home, marker_proc):
    mod = _load_daemon_module()
    pidfile = home / "recall-daemon.pid"
    _stale_pidfile(pidfile, marker_proc.pid)
    mod.PID_PATH = str(pidfile)
    assert mod._recorded_pid_is_alive() is True


# -- P12: a non-positive pid in the pidfile reads NOT alive -----------------
# os.kill(0, 0) signals the caller's process group and os.kill(-1, 0) every
# process the caller may signal -- both succeed, and /proc/0|-1/stat is
# unreadable -> the conservative-True branch, so such a pidfile would refuse
# a restart forever. (pid 1 is deliberately NOT special-cased: inside a
# container the owner can legitimately be pid 1.)


@pytest.mark.parametrize("marker", [None, "engramd_owner"])
@pytest.mark.parametrize("content", ["0", "-1", "-12345"])
def test_P12_non_positive_pid_is_not_alive(home, content, marker):
    pidfile = home / "x.pid"
    pidfile.write_text(content)
    assert _helper().recorded_pid_is_alive(pidfile, program_marker=marker) is False


# -- P13: an unrepresentable pid in the pidfile reads NOT alive ---------------
# Contract: recorded_pid_is_alive NEVER raises for any pidfile content. A pid
# >= 2**31 makes os.kill raise OverflowError (not an OSError), which would
# crash the starting process on a corrupt pidfile. Same class as P12.


@pytest.mark.parametrize("marker", [None, "engramd_owner"])
@pytest.mark.parametrize(
    "content",
    ["2147483648", str(2**63 - 1), str(2**63), "99999999999999999999999"],
)
def test_P13_unrepresentable_pid_is_not_alive(home, content, marker):
    pidfile = home / "x.pid"
    pidfile.write_text(content)
    assert _helper().recorded_pid_is_alive(pidfile, program_marker=marker) is False
