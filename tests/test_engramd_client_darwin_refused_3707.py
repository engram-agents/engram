"""Contract tests for engram-alpha#3707 (part of #2652): on Darwin a refused
connect() to the engramd owner whose pidfile names a live owner is a
`busy_or_stuck` Unavailable (with `owner_pid` and a stated retry window), not
the down/restart one; plus a reason code on every Unavailable path.

Written BEFORE the change (contract-first). Measured on macOS 26.6.2: connect()
on an AF_UNIX path gives ENOENT (no file), ECONNREFUSED (full backlog, stale
file, bound-never-listening), ENOTSOCK (regular file). So on Darwin
ECONNREFUSED cannot separate a busy owner from a live owner that is not
listening; the pid check separates owner-present from owner-gone only.

The Darwin cells run a REAL owner (or real processes / sockets) on a real Mac
and skip elsewhere; no connect() errno and no pid primitive is simulated.
The one place a platform is patched (`test_e_darwin_gate...`) patches only the
platform discriminator and still uses a real refused connect().

Cell map: (a) busy owner, (b) dead owner, (c) live owner with its listener
closed, (d) reused pid, (e) Linux pin, (f) pid check never runs on other
errnos, (g) moved constants, (h) unknown pidfile location, (i) the one retry
constant, (j) reason codes on every path; plus recorded_live_pid parity and
the platform_arm delegation (item 10).
"""

from __future__ import annotations

import errno
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_pidfile  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

on_darwin = pytest.mark.skipif(
    sys.platform != "darwin", reason="Darwin ECONNREFUSED semantics; real-Mac cells only"
)
on_linux = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux semantics; runs in Linux CI"
)

TIMEOUT = 2.0
RESTART_STEP_PREFIX = "Check whether the engramd process is running and (re)start it"


@pytest.fixture
def home():
    # Short path: AF_UNIX paths are length-limited (103 bytes on Darwin).
    d = tempfile.mkdtemp(prefix="e37-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def children():
    procs = []

    def spawn(argv):
        p = subprocess.Popen(argv)
        procs.append(p)
        return p

    try:
        yield spawn
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait()


def _stale_socket(home: Path) -> Path:
    """A bound-then-closed socket file at the standard path (state 4)."""
    sp = home / ec.SOCKET_DIR_NAME / ec.DEFAULT_SOCKET_NAME
    sp.parent.mkdir(parents=True, exist_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sp))
    s.listen(1)
    s.close()
    return sp


def _write_pidfile(home: Path, pid: int, age_s: float = 0.0) -> Path:
    p = ec.pid_path_for_home(home)
    p.write_text(str(pid))
    if age_s:
        t = time.time() - age_s
        os.utime(p, (t, t))
    return p


def _start(home: Path, **kw) -> "eo.EngramdOwner":
    owner = eo.EngramdOwner(home, **kw)
    owner.start(install_signal_handlers=False)
    return owner


def _burst(owner, n: int, release=None, release_after: float = 0.0):
    barrier = threading.Barrier(n)
    out = [None] * n

    def run(i):
        barrier.wait()
        out[i] = ec.read("ping", {}, timeout=TIMEOUT, socket_path=owner.socket_path)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    if release is not None:
        time.sleep(release_after)
        release.set()
    for t in threads:
        t.join(TIMEOUT + 5)
    assert all(o is not None for o in out), "a client never returned"
    return out


def _all_steps(res) -> list[str]:
    return list(res.agent_steps) + list(res.human_steps)


def _assert_busy_or_stuck(res, pid):
    assert isinstance(res, ec.Unavailable), res
    assert res.reason == "busy_or_stuck", res
    assert res.owner_pid == pid, res
    agent = " ".join(res.agent_steps)
    assert any("retry" in s.lower() for s in res.agent_steps), res.agent_steps
    assert str(pid) in agent, res.agent_steps
    assert str(ec.OWNER_BUSY_RETRY_SECONDS) in agent, res.agent_steps
    assert not any(s.startswith(RESTART_STEP_PREFIX) for s in _all_steps(res)), res
    assert not any("do not restart" in s.lower() for s in _all_steps(res)), res
    assert res.detail.startswith("owner busy or stuck (connection refused, owner alive)"), res.detail
    assert str(pid) in res.detail, res.detail


def _assert_down(res, sp=None, reason="down"):
    # Same Unavailable shape on every client-side failure; only `reason` differs
    # (#3707: codes by meaning -- down / timeout / protocol).
    assert isinstance(res, ec.Unavailable), res
    assert res.reason == reason, res
    assert res.owner_pid is None, res
    # Only `down` carries the restart step (round 3: the remedy follows the code;
    # timeout and protocol have their own pinned steps, see the (l) cells).
    if reason == "down":
        assert any(s.startswith(RESTART_STEP_PREFIX) for s in res.agent_steps), res.agent_steps


# --- (a) live owner, backlog full -------------------------------------------


@on_darwin
@requires_linux_owner
def test_a_live_owner_with_full_backlog_is_busy_or_stuck(home, monkeypatch):
    """Backlog of 1, accept loop held, burst of 8 (the #3680 shape). At least
    one result is busy_or_stuck with the owner's real pid. Mutation: the
    ECONNREFUSED branch ignores the pid check -> red; remedy loses the pid ->
    red; owner_pid unset on the live path -> red."""
    gate = threading.Event()
    real = eo.EngramdOwner._accept_loop

    def held(self):
        gate.wait(10)
        return real(self)

    monkeypatch.setattr(eo.EngramdOwner, "_accept_loop", held)
    owner = _start(home, listen_backlog=1)
    try:
        out = _burst(owner, 8, release=gate, release_after=1.0)
    finally:
        gate.set()
        owner.stop()
    stuck = [r for r in out if isinstance(r, ec.Unavailable) and r.reason == "busy_or_stuck"]
    assert len(stuck) >= 1, out
    for r in stuck:
        _assert_busy_or_stuck(r, os.getpid())
    for r in out:
        assert isinstance(r, (ec.Ok, ec.Unavailable)), r


# --- (b) owner gone, socket file left ---------------------------------------


@on_darwin
def test_b_dead_owner_with_stale_socket_is_down(home, children):
    """Stale socket file (state 4) plus a pidfile naming a dead pid -> down.
    Mutation: a dead pid reads as alive -> red."""
    sp = _stale_socket(home)
    p = children(["/usr/bin/true"])
    p.wait()
    _write_pidfile(home, p.pid)
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    _assert_down(res)
    assert "refused" in res.detail.lower()


@on_darwin
def test_b2_stale_socket_and_no_pidfile_is_down(home):
    sp = _stale_socket(home)
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    _assert_down(res)


# --- (c) live owner whose listener is closed --------------------------------


@on_darwin
@requires_linux_owner
def test_c_live_owner_with_closed_listener_is_busy_or_stuck(home):
    """CONSTRUCTION: a real EngramdOwner is started, then its listening socket
    is closed in-process (owner._server.close()). The accept loop's accept()
    raises OSError and the loop exits cleanly; the owner object, its pidfile
    and its socket file all remain (state 4 with a live pid). Not a stand-in
    process. Mutation: the ECONNREFUSED branch ignores the pid check -> red."""
    owner = _start(home)
    try:
        owner._server.close()
        owner._accept_thread.join(5)
        assert not owner._accept_thread.is_alive()
        res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=owner.socket_path)
        _assert_busy_or_stuck(res, os.getpid())
    finally:
        owner.stop()


# --- (d) reused pid ---------------------------------------------------------


@on_darwin
def test_d_reused_pid_is_down(home, children):
    """Stale socket file plus a pidfile naming a REUSED pid (mtime in the past,
    live child started after it, marker absent) -> down. Mutation: a bare
    kill(pid, 0) liveness (no identity check) -> red."""
    sp = _stale_socket(home)
    child = children(["/bin/sleep", "60"])
    _write_pidfile(home, child.pid, age_s=600)
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    _assert_down(res)


# --- (e) Linux pin ----------------------------------------------------------


@on_linux
def test_e_linux_econnrefused_is_still_down(home, children):
    """Linux: a refused connect() keeps today's down mapping even when a live
    pid is recorded. The #3680 cells are the EAGAIN pin and run unmodified in
    Linux CI."""
    sp = _stale_socket(home)
    child = children(["/bin/sleep", "60"])
    time.sleep(0.05)
    _write_pidfile(home, child.pid)
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    _assert_down(res)


@on_linux
def test_e_linux_refused_at_a_non_standard_path_is_still_down(home):
    """Linux: a refused connect() at a NON-standard socket path keeps today's
    down mapping. On Darwin this path's pidfile location is unknown and reads
    busy_or_stuck (cell h); on Linux the Darwin branch must never run, so it is
    down. This pins the case the #3680 nobody-listening cell used to cover
    before its fixture moved to the standard run/ layout (Kepler, #3707).
    Mutation: drop the `platform_arm() == "darwin"` guard -> busy_or_stuck -> red
    (the live-pid Linux cell above goes red on that too). This cell alone also
    catches the narrower mutation where only the unknown-pidfile-location branch
    escapes the gate."""
    sp = home / "stale.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sp))
    s.listen(1)
    s.close()
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    _assert_down(res)
    assert res.owner_pid is None


@on_darwin
def test_e_darwin_gate_follows_the_platform_arm(home, children, monkeypatch):
    """A real refused connect() with a live recorded pid, with the platform
    discriminator patched to Linux -> down, and the pid check is not run.
    Mutation: drop the platform gate -> red."""
    sp = _stale_socket(home)
    child = children(["/bin/sleep", "60"])
    time.sleep(0.05)
    _write_pidfile(home, child.pid)
    calls = []
    real = engram_pidfile.recorded_live_pid
    monkeypatch.setattr(
        engram_pidfile, "recorded_live_pid", lambda *a, **k: (calls.append(a), real(*a, **k))[1]
    )
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    _assert_down(res)
    assert calls == []


# --- (f) the pid check never runs on other failures -------------------------


@pytest.fixture
def spy(monkeypatch):
    calls = []
    real = engram_pidfile.recorded_live_pid

    def wrapper(*a, **k):
        calls.append((a, k))
        return real(*a, **k)

    monkeypatch.setattr(engram_pidfile, "recorded_live_pid", wrapper)
    return calls


@on_darwin
def test_f_never_called_on_enoent(home, children, spy):
    """No socket file (ENOENT), even with a live pid recorded. Mutation: the
    check runs on every connect error -> red."""
    child = children(["/bin/sleep", "60"])
    time.sleep(0.05)
    (home / ec.SOCKET_DIR_NAME).mkdir()
    _write_pidfile(home, child.pid)
    sp = home / ec.SOCKET_DIR_NAME / ec.DEFAULT_SOCKET_NAME
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    _assert_down(res)
    assert "No such file" in res.detail
    assert spy == []


@on_darwin
def test_f_never_called_on_enotsock(home, children, spy):
    """A regular file at the socket path (ENOTSOCK). Mutation: the check runs
    on every connect error -> red."""
    child = children(["/bin/sleep", "60"])
    time.sleep(0.05)
    sp = home / ec.SOCKET_DIR_NAME / ec.DEFAULT_SOCKET_NAME
    sp.parent.mkdir()
    sp.write_text("not a socket")
    _write_pidfile(home, child.pid)
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    _assert_down(res)
    assert spy == []


@on_darwin
def test_f_never_called_on_timeout(home, children, spy):
    """A listener that accepts and never replies (a timeout, after a
    successful connect). Mutation: the check runs on every error -> red."""
    child = children(["/bin/sleep", "60"])
    time.sleep(0.05)
    sp = home / ec.SOCKET_DIR_NAME / ec.DEFAULT_SOCKET_NAME
    sp.parent.mkdir()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sp))
    srv.listen(4)
    held = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                held.append(srv.accept()[0])
            except OSError:
                pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    _write_pidfile(home, child.pid)
    try:
        res = ec.read("ping", {}, timeout=0.4, socket_path=sp)
    finally:
        stop.set()
        t.join(2)
        for c in held:
            c.close()
        srv.close()
    _assert_down(res, reason="timeout")
    assert "did not respond" in res.detail
    assert spy == []


@on_linux
def test_f_linux_never_called_on_econnrefused(home, children, spy):
    """Linux ECONNREFUSED: the pid check is not run. Mutation: drop the
    platform gate -> red."""
    sp = _stale_socket(home)
    child = children(["/bin/sleep", "60"])
    time.sleep(0.05)
    _write_pidfile(home, child.pid)
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    _assert_down(res)
    assert spy == []


# --- (g) moved constants ----------------------------------------------------


def test_g_constants_live_in_the_client_and_the_owner_aliases_them(tmp_path):
    assert eo.DEFAULT_PID_NAME == ec.DEFAULT_PID_NAME == "engramd.pid"
    assert eo._PIDFILE_PROGRAM_MARKER == ec.PIDFILE_PROGRAM_MARKER == "engramd_owner"
    owner = eo.EngramdOwner(tmp_path)
    assert owner.pid_path == ec.pid_path_for_home(owner.engram_home)
    assert owner.socket_path == ec.socket_path_for_home(owner.engram_home)
    assert ec.socket_path_for_home(tmp_path) == tmp_path / "run" / "engramd.sock"
    assert ec.pid_path_for_home(tmp_path) == tmp_path / "engramd.pid"


def test_g2_default_socket_path_uses_the_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    assert ec._resolve_socket_path(None) == ec.socket_path_for_home(tmp_path)


def test_g3_pid_path_for_socket(tmp_path):
    std = tmp_path / "run" / "engramd.sock"
    assert ec._pid_path_for_socket(std) == tmp_path / "engramd.pid"
    assert ec._pid_path_for_socket(tmp_path / "other" / "x.sock") is None
    # Round 3: the parent directory name alone is not the layout; the socket name
    # must be engramd.sock too (/var/run/foo.sock must not guess /var/engramd.pid).
    assert ec._pid_path_for_socket(tmp_path / "run" / "foo.sock") is None


def test_g4_import_does_not_load_ctypes():
    code = (
        "import sys; sys.path.insert(0, %r); import engramd_client; "
        "assert 'ctypes' not in sys.modules, 'ctypes loaded'" % _SRC_ENGRAM
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# --- (h) non-standard socket path -------------------------------------------


@on_darwin
def test_h_unknown_pidfile_location_is_busy_or_stuck_without_a_pid(home):
    """A refused connect() on an explicit socket path outside <home>/run/ ->
    busy_or_stuck, owner_pid None, the remedy says the pid is unknown.
    Mutation: unknown location leans to down -> red."""
    sp = home / "elsewhere" / "x.sock"
    sp.parent.mkdir()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sp))
    s.listen(1)
    s.close()
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    assert isinstance(res, ec.Unavailable), res
    assert res.reason == "busy_or_stuck", res
    assert res.owner_pid is None, res
    steps = " ".join(_all_steps(res)).lower()
    assert "pid is unknown" in steps, res.agent_steps
    assert "retry" in steps
    assert str(ec.OWNER_BUSY_RETRY_SECONDS) in " ".join(res.agent_steps)
    assert not any(s.startswith(RESTART_STEP_PREFIX) for s in _all_steps(res))
    assert not any("do not restart" in s.lower() for s in _all_steps(res))


# --- (i) the one retry constant ---------------------------------------------


def test_i_both_remedies_follow_the_constant(monkeypatch):
    """Mutation: a literal 10 in the Linux human step (or the busy_or_stuck
    remedy) -> the patched value does not appear -> red."""
    monkeypatch.setattr(ec, "OWNER_BUSY_RETRY_SECONDS", 37)
    kw = dict(socket_path="/x/run/engramd.sock", bound=2.0, detail="d", request_id=None)
    busy = ec._busy_unavailable(**kw)
    assert "37" in " ".join(busy.human_steps), busy.human_steps
    stuck = ec._busy_or_stuck_unavailable(**kw, owner_pid=4242)
    assert "37" in " ".join(stuck.agent_steps), stuck.agent_steps
    assert "37" in " ".join(stuck.human_steps), stuck.human_steps
    unknown = ec._busy_or_stuck_unavailable(**kw, owner_pid=None)
    assert "37" in " ".join(unknown.agent_steps), unknown.agent_steps
    assert "37" in " ".join(unknown.human_steps), unknown.human_steps


def test_i2_the_constant_is_ten():
    assert ec.OWNER_BUSY_RETRY_SECONDS == 10


def test_i3_busy_or_stuck_builder_shapes():
    kw = dict(socket_path="/x/run/engramd.sock", bound=2.0, detail="boom", request_id="r")
    known = ec._busy_or_stuck_unavailable(**kw, owner_pid=4242)
    assert known.reason == "busy_or_stuck" and known.owner_pid == 4242
    assert known.request_id == "r"
    assert "4242" in " ".join(known.agent_steps)
    assert "4242" in known.detail and known.detail.endswith("boom")
    unknown = ec._busy_or_stuck_unavailable(**kw, owner_pid=None)
    assert unknown.owner_pid is None
    assert "unknown" in " ".join(unknown.agent_steps).lower()
    for r in (known, unknown):
        assert not any(s.startswith(RESTART_STEP_PREFIX) for s in _all_steps(r))
        assert not any("do not restart" in s.lower() for s in _all_steps(r))
        assert any("restart" in s.lower() and "stop" in s.lower() for s in r.agent_steps)


def test_i4_stop_step_reconfirms_the_pid_before_stopping_it():
    """The pid was checked when the call failed; the stop comes at least
    OWNER_BUSY_RETRY_SECONDS later, and the pid may have been reused by then
    (Clio, #3716 review). So every step that says to stop pid N first says to
    confirm pid N still runs the owner, names how, and says not to stop it
    otherwise. Mutation: drop the confirm sentence -> red."""
    r = ec._busy_or_stuck_unavailable(
        socket_path="/x/run/engramd.sock", bound=2.0, detail="d", request_id=None, owner_pid=4242
    )
    # Any step that tells the reader to end a process (stop/kill/terminate, in
    # any wording) is an act on the pid, so every such step must carry the
    # re-check. Selecting on the act, not on one phrasing, keeps a reworded
    # step ("kill pid 4242 ...") from slipping past (round-4 review).
    acts = ("stop", "kill", "terminate")
    acting = {"agent": [], "human": []}
    for side, steps in (("agent", r.agent_steps), ("human", r.human_steps)):
        for s in steps:
            low = s.lower()
            if not any(a in low for a in acts):
                continue
            acting[side].append(s)
            assert "confirm" in low, s
            assert ec.PIDFILE_PROGRAM_MARKER in s, s
            assert "ps -p 4242" in s, s
            first_act = min(low.index(a) for a in acts if a in low)
            assert low.index("confirm") < first_act, s
            assert "first confirm" in low or "before stopping" in low, s
            assert "do not stop" in low, s
    # Both audiences must actually get the stop instruction, so the checks
    # above cannot pass by finding nothing to check.
    assert acting["agent"] and acting["human"], acting


# --- (j) reason codes on every path -----------------------------------------


def test_j_busy_builder_sets_reason_busy():
    """Mutation: drop the Linux busy reason -> red while the #3680 cells stay
    green."""
    r = ec._busy_unavailable(socket_path="/x", bound=2.0, detail="d", request_id=None)
    assert r.reason == "busy" and r.owner_pid is None


def test_j_unavailable_builder_requires_a_reason():
    """No default: a call site that forgets the keyword fails loudly."""
    with pytest.raises(TypeError):
        ec._unavailable(socket_path="/x", bound=2.0, detail="d", request_id=None)


@pytest.mark.parametrize("code", ["down", "timeout", "protocol"])
def test_j_unavailable_builder_carries_the_given_reason(code):
    r = ec._unavailable(
        socket_path="/x", bound=2.0, detail="d", request_id=None, reason=code
    )
    assert r.reason == code and r.owner_pid is None


def test_j_defaults_keep_positional_construction():
    r = ec.Unavailable("/x", 2.0, ["a"], ["h"])
    assert r.reason is None and r.owner_pid is None


# down: no owner listening ---------------------------------------------------


def test_j_too_long_path_is_down(tmp_path):
    long = tmp_path / ("x" * 200) / "engramd.sock"
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=long)
    _assert_down(res)


def test_j_enoent_is_down(home):
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=home / "nope.sock")
    _assert_down(res)


def test_j_enotsock_is_down(home):
    """A regular file at the socket path: connect() gives ENOTSOCK."""
    sp = home / "file.sock"
    sp.write_text("not a socket")
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    _assert_down(res)


@on_linux
def test_j_linux_refused_stale_socket_is_down(home):
    """Linux: ECONNREFUSED on a stale socket file is plain down (Linux-only)."""
    sp = home / "stale.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sp))
    s.close()
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    _assert_down(res)


# timeout: no valid reply within the bound -----------------------------------


def test_j_deadline_exhausted_is_timeout(home):
    """timeout=0: the budget is already spent at the first arm step, before any
    I/O -- the `_arm_timeout_or_unavailable` site, reached for real."""
    res = ec.read("ping", {}, timeout=0, socket_path=home / "x.sock")
    _assert_down(res, reason="timeout")
    assert "did not respond" in res.detail


def test_j_arm_helper_with_spent_deadline_is_timeout():
    """Targeted: the same helper site, called directly with an expired
    deadline (a mid-call exhaustion cannot be timed reliably end to end)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        res = ec._arm_timeout_or_unavailable(
            s, deadline=time.monotonic() - 1, socket_path="/x", bound=1.0,
            request_id=None,
        )
    finally:
        s.close()
    assert isinstance(res, ec.Unavailable) and res.reason == "timeout"
    assert res.owner_pid is None


def _serve_once(home: Path, reply: bytes | None, hold: float = 0.0):
    sp = home / "s.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sp))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        try:
            conn.settimeout(2)
            try:
                conn.recv(65536)
            except OSError:
                pass
            if reply:
                conn.sendall(reply)
            time.sleep(hold)
        finally:
            conn.close()
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return sp, t


def test_j_socket_timeout_is_timeout(home):
    """A real listener that accepts and never replies."""
    sp, t = _serve_once(home, None, hold=1.0)
    res = ec.read("ping", {}, timeout=0.3, socket_path=sp)
    t.join(3)
    _assert_down(res, reason="timeout")
    assert "did not respond" in res.detail


# protocol: the owner answered, but not validly ------------------------------


def test_j_response_cap_is_protocol(home, monkeypatch):
    """Bytes over the cap with no newline (real server, cap lowered)."""
    monkeypatch.setattr(ec, "MAX_RESPONSE_BYTES", 16)
    sp, t = _serve_once(home, b"x" * 64)
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    t.join(3)
    _assert_down(res, reason="protocol")
    assert "cap" in res.detail


def test_j_empty_response_is_protocol(home):
    sp, t = _serve_once(home, None)
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    t.join(3)
    _assert_down(res, reason="protocol")
    assert "without responding" in res.detail


def test_j_unparseable_response_is_protocol(home):
    sp, t = _serve_once(home, b"garbage\n")
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    t.join(3)
    _assert_down(res, reason="protocol")
    assert "unparseable" in res.detail


def test_j_unrecognized_status_is_protocol(home):
    sp, t = _serve_once(home, b'{"status": "weird"}\n')
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    t.join(3)
    _assert_down(res, reason="protocol")
    assert "unrecognized status" in res.detail


@on_linux
@requires_linux_owner
def test_j_linux_eagain_is_busy(home, monkeypatch):
    """Real Linux full backlog -> reason 'busy' end to end (Linux CI)."""
    gate = threading.Event()
    real = eo.EngramdOwner._accept_loop

    def held(self):
        gate.wait(10)
        return real(self)

    monkeypatch.setattr(eo.EngramdOwner, "_accept_loop", held)
    owner = _start(home, listen_backlog=1)
    try:
        out = _burst(owner, 8, release=gate, release_after=1.0)
    finally:
        gate.set()
        owner.stop()
    assert any(isinstance(r, ec.Unavailable) and r.reason == "busy" for r in out), out


@on_darwin
def test_j_sigstopped_live_owner_never_reads_down(home):
    """The measurement behind the `timeout` code: a REAL owner in its own
    process, frozen with SIGSTOP (listener open, never accepting). A burst
    larger than the accept backlog splits into refused calls (busy_or_stuck,
    owner_pid == the child, not us) and backlog-admitted calls that time out
    (`timeout`). None may read `down` while the owner exists. After SIGKILL
    (socket file + pidfile left behind) the same call reads `down`.
    Mutation: timeout -> down makes the backlog-admitted calls read down."""
    env = dict(os.environ, ENGRAM_HOME=str(home), PYTHONPATH=_SRC_ENGRAM)
    child = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "src" / "engram" / "engramd_owner.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sp = home / ec.SOCKET_DIR_NAME / ec.DEFAULT_SOCKET_NAME
    stopped = False
    try:
        deadline = time.monotonic() + 15
        while not sp.exists():
            assert child.poll() is None, "owner exited early"
            assert time.monotonic() < deadline, "owner socket never appeared"
            time.sleep(0.05)
        assert isinstance(ec.read("ping", {}, timeout=5.0, socket_path=sp), ec.Ok)
        os.kill(child.pid, signal.SIGSTOP)
        stopped = True

        n = 400
        out = [None] * n

        def run(i):
            out[i] = ec.read("ping", {}, timeout=1.5, socket_path=sp)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        assert all(r is not None for r in out), "a client never returned"
        assert all(isinstance(r, ec.Unavailable) for r in out)
        codes = [r.reason for r in out]
        assert "down" not in codes, {c: codes.count(c) for c in set(codes)}
        for r in out:
            if r.reason == "busy_or_stuck":
                assert r.owner_pid == child.pid and r.owner_pid != os.getpid(), r
            else:
                assert r.reason == "timeout", r
        assert "busy_or_stuck" in codes and "timeout" in codes, {
            c: codes.count(c) for c in set(codes)
        }
    finally:
        # Never leave a stopped process behind.
        if stopped:
            os.kill(child.pid, signal.SIGCONT)
        child.kill()
        child.wait()
    assert sp.exists(), "premise: SIGKILL leaves the socket file behind"
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    _assert_down(res)


# --- recorded_live_pid parity -----------------------------------------------


def test_recorded_live_pid_matches_recorded_pid_is_alive(tmp_path, children):
    pf = tmp_path / "engramd.pid"
    live = children(["/bin/sleep", "60"])
    time.sleep(0.05)
    dead = children(["/usr/bin/true"])
    dead.wait()
    shapes = []
    pf.write_text(str(live.pid))
    shapes.append((pf, None, live.pid))  # live
    shapes.append((tmp_path / "missing.pid", None, None))  # missing
    pf2 = tmp_path / "dead.pid"
    pf2.write_text(str(dead.pid))
    shapes.append((pf2, None, None))  # dead
    pf3 = tmp_path / "reused.pid"
    pf3.write_text(str(live.pid))
    t = time.time() - 600
    os.utime(pf3, (t, t))
    shapes.append((pf3, "engramd_owner_nomatch", None))  # reused, marker absent
    pf4 = tmp_path / "marker.pid"
    pf4.write_text(str(live.pid))
    os.utime(pf4, (t, t))
    shapes.append((pf4, "sleep", live.pid))  # old pidfile but marker present
    pf5 = tmp_path / "garbage.pid"
    pf5.write_text("not-a-pid")
    shapes.append((pf5, None, None))
    for path, marker, expected in shapes:
        alive = engram_pidfile.recorded_pid_is_alive(path, program_marker=marker)
        got = engram_pidfile.recorded_live_pid(path, program_marker=marker)
        assert got == expected, (path.name, got, expected)
        assert alive is (got is not None), (path.name, alive, got)


# --- platform_arm delegation (item 10) --------------------------------------


@pytest.mark.parametrize(
    "system,arm", [("Linux", "linux"), ("Darwin", "darwin"), ("FreeBSD", None)]
)
def test_platform_arm_agrees_and_is_keyed_on_platform_system(monkeypatch, system, arm):
    monkeypatch.setattr(platform, "system", lambda: system)
    assert engram_pidfile.platform_arm() == arm
    assert ec.platform_arm() == arm


def test_client_platform_arm_delegates_to_the_pidfile_module(monkeypatch):
    """Mutation: the client keeps its own body -> red."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(engram_pidfile, "platform_arm", lambda: "darwin")
    assert ec.platform_arm() == "darwin"


# =============================================================================
# Round 3 (design-seat rulings on #3707): oversize requests are refused before
# connecting; a post-connect OSError is `protocol`; the remedy follows the code
# (one table); the pidfile guess needs the socket name too.
# =============================================================================

import inspect  # noqa: E402
import json  # noqa: E402
import struct  # noqa: E402


def _request_bytes(method: str, params: dict, request_id=None) -> int:
    """The exact byte length the client puts on the wire (mirrors _call)."""
    req = {
        "contract_version": ec.CONTRACT_VERSION,
        "method": method,
        "params": params,
        "request_id": request_id,
    }
    return len((json.dumps(req) + "\n").encode("utf-8"))


def _params_of_total_size(total: int) -> dict:
    base = _request_bytes("ping", {"pad": ""})
    assert total >= base
    params = {"pad": "x" * (total - base)}
    assert _request_bytes("ping", params) == total
    return params


# --- (k) item 1: the client refuses an oversize request before connecting -----


def test_k_cap_lives_in_the_client_and_the_owner_aliases_it():
    assert ec.MAX_REQUEST_BYTES == 1024 * 1024
    assert eo.MAX_REQUEST_BYTES == ec.MAX_REQUEST_BYTES
    default = inspect.signature(eo.EngramdOwner.__init__).parameters["max_request_bytes"].default
    assert default == ec.MAX_REQUEST_BYTES


def test_k_two_mb_request_is_rejected_and_no_socket_is_ever_created(home, monkeypatch):
    """The path does not exist, so a client that tried would return Unavailable
    (down); the socket factory is also booby-trapped, so no connect() is even
    possible. Mutation: remove the pre-flight check -> red."""

    def boom(*a, **k):
        raise AssertionError("a socket was created for an oversize request")

    monkeypatch.setattr(ec.socket, "socket", boom)
    params = {"pad": "x" * (2 * 1024 * 1024)}
    res = ec.write("ping", params, request_id="rid-1", timeout=TIMEOUT,
                   socket_path=home / "nope.sock")
    assert isinstance(res, ec.Rejected), res
    assert res.reason == "request_too_large", res
    assert res.request_id == "rid-1"
    assert str(ec.MAX_REQUEST_BYTES) in res.detail, res.detail
    assert str(_request_bytes("ping", params, "rid-1")) in res.detail, res.detail
    assert res.remedy and "split" in res.remedy.lower()


def test_k_cap_boundary_one_byte_over_is_rejected(home):
    params = _params_of_total_size(ec.MAX_REQUEST_BYTES + 1)
    res = ec.read("ping", params, timeout=TIMEOUT, socket_path=home / "nope.sock")
    assert isinstance(res, ec.Rejected) and res.reason == "request_too_large", res


@requires_linux_owner
def test_k_request_exactly_at_the_cap_reaches_a_real_owner(home):
    """What the owner counts: every byte it has received from the connection
    (the line INCLUDING its newline), dropping when that total is > its cap. The
    client sends exactly its payload (the line + newline), so len(payload) > cap
    is the same comparison. At the cap -> a normal answer from a real owner; one
    over -> Rejected by the client (never a broken pipe)."""
    owner = _start(home)
    try:
        at_cap = _params_of_total_size(ec.MAX_REQUEST_BYTES)
        res = ec.read("ping", at_cap, timeout=10.0, socket_path=owner.socket_path)
        assert isinstance(res, ec.Ok), res
        over = _params_of_total_size(ec.MAX_REQUEST_BYTES + 1)
        res = ec.read("ping", over, timeout=10.0, socket_path=owner.socket_path)
        assert isinstance(res, ec.Rejected) and res.reason == "request_too_large", res
        assert isinstance(
            ec.read("ping", {}, timeout=TIMEOUT, socket_path=owner.socket_path), ec.Ok
        )
    finally:
        owner.stop()


# --- (m) item 2: an OSError after a successful connect is `protocol` ---------


def _has_restart_step(res) -> bool:
    return any("restart" in s.lower() for s in _all_steps(res))


def _serve_then_reset(home: Path):
    """A stand-in owner that accepts, reads a little, then closes with SO_LINGER
    0 so the client sees a reset mid-exchange (EPIPE / ECONNRESET)."""
    sp = home / "r.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sp))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        try:
            conn.settimeout(2)
            try:
                conn.recv(1024)
            except OSError:
                pass
            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except OSError:
                pass
        finally:
            conn.close()
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return sp, t


def test_m_oserror_after_connect_is_protocol_with_no_restart_step(home):
    """Mutation: drop the connected flag -> reason reads `down` -> red."""
    sp, t = _serve_then_reset(home)
    params = {"pad": "x" * 600_000}  # under the cap, big enough to be mid-send
    res = ec.read("ping", params, timeout=TIMEOUT, socket_path=sp)
    t.join(3)
    assert isinstance(res, ec.Unavailable), res
    assert "errno" in res.detail.lower(), ("expected the OSError path, got", res.detail)
    assert res.reason == "protocol", res
    assert res.owner_pid is None
    assert not _has_restart_step(res), _all_steps(res)


def test_m_oserror_before_connect_stays_down(home):
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=home / "nope.sock")
    assert res.reason == "down"


# --- (l) item 3: the remedy follows the code (one table) --------------------


def _mk(reason: str) -> "ec.Unavailable":
    return ec._unavailable(
        socket_path="/x/run/engramd.sock", bound=2.0, detail="d", request_id=None,
        reason=reason,
    )


def test_l_down_steps_are_unchanged_byte_for_byte():
    r = _mk("down")
    assert r.agent_steps == [
        "Retry the call after a short backoff -- engramd may recover on its own.",
        "Check whether the engramd process is running and (re)start it if not "
        "(the owner should be listening on /x/run/engramd.sock).",
    ]
    assert r.human_steps == [
        "If retries keep failing, restart your Claude Code session.",
        "Run /mcp to reconnect the MCP server, or check the engramd logs for "
        "the underlying failure.",
    ]


def test_l_timeout_steps_are_pinned_and_escalate_stop_then_restart():
    n = ec.OWNER_BUSY_RETRY_SECONDS
    r = _mk("timeout")
    assert r.agent_steps == [
        "Retry the call after a short backoff.",
        f"If calls keep timing out for more than {n} seconds, the owner may be "
        "stuck. Check whether the engramd process is running; if it is running "
        "but not answering, stop it, then restart it.",
    ]
    assert not any(s.startswith(RESTART_STEP_PREFIX) for s in _all_steps(r))
    for s in _all_steps(r):
        low = s.lower()
        if "restart" in low:
            assert "stop" in low and low.index("stop") < low.index("restart"), s


def test_l_timeout_steps_follow_the_retry_constant(monkeypatch):
    monkeypatch.setattr(ec, "OWNER_BUSY_RETRY_SECONDS", 37)
    assert "37" in " ".join(_mk("timeout").agent_steps)


def test_l_protocol_steps_are_pinned_with_no_restart_and_no_claim_about_the_owner():
    r = _mk("protocol")
    assert r.agent_steps == [
        "The owner was reached, but the exchange failed without a valid answer. "
        "Retry once. If the retry returns `down`, follow its steps. If it fails "
        "as `protocol` again, check the engramd logs for why the owner dropped "
        "this request.",
    ]
    assert r.human_steps == [
        "Check the engramd logs for why the owner dropped this request.",
    ]
    assert not _has_restart_step(r)
    text = " ".join(_all_steps(r)).lower()
    assert "it is up" not in text and "do not restart" not in text and "is healthy" not in text


def test_l_protocol_does_not_get_the_down_steps():
    """Mutation: protocol given the down steps -> red."""
    assert _mk("protocol").agent_steps != _mk("down").agent_steps
    assert _mk("protocol").human_steps != _mk("down").human_steps
    assert _mk("timeout").agent_steps != _mk("down").agent_steps


def test_l_every_reason_has_a_row_and_the_set_agrees_with_the_builders():
    assert set(ec.UNAVAILABLE_REASONS) == {
        "down", "timeout", "protocol", "local", "internal", "busy", "busy_or_stuck"
    }
    for reason in ec.UNAVAILABLE_REASONS:
        if reason not in ec._BUILDER_REASONS:
            assert reason in ec._REMEDY_STEPS, reason
    assert set(ec._REMEDY_STEPS) | set(ec._BUILDER_REASONS) == set(ec.UNAVAILABLE_REASONS)
    assert not set(ec._REMEDY_STEPS) & set(ec._BUILDER_REASONS)
    kw = dict(socket_path="/x", bound=2.0, detail="d", request_id=None)
    assert ec._busy_unavailable(**kw).reason in ec._BUILDER_REASONS
    assert ec._busy_or_stuck_unavailable(**kw, owner_pid=None).reason in ec._BUILDER_REASONS


def test_l_unknown_reason_raises_at_construction():
    with pytest.raises((KeyError, ValueError)):
        _mk("not-a-reason")
    with pytest.raises((KeyError, ValueError)):
        _mk("busy")  # builder-owned, not constructible through the table


# --- (n) item 4: the pidfile guess needs the socket name too -----------------


def test_n_run_dir_with_another_socket_name_is_unknown(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    assert ec._pid_path_for_socket(d / "foo.sock") is None
    assert ec._pid_path_for_socket(Path("/var/run/foo.sock")) is None
    assert ec._pid_path_for_socket(d / ec.DEFAULT_SOCKET_NAME) == tmp_path / ec.DEFAULT_PID_NAME


def test_n_engramd_sock_outside_run_is_still_unknown(tmp_path):
    assert ec._pid_path_for_socket(tmp_path / "other" / ec.DEFAULT_SOCKET_NAME) is None


@on_darwin
def test_n_refused_stale_socket_under_run_with_another_name_is_busy_or_stuck_without_pid(home):
    """<tmp>/run/foo.sock, stale: before the name check this guessed
    <tmp>/engramd.pid (missing) and read `down`; the location is unknown, so it
    leans to retry. Mutation: drop the name check -> red."""
    sp = home / "run" / "foo.sock"
    sp.parent.mkdir()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sp))
    s.listen(1)
    s.close()
    res = ec.read("ping", {}, timeout=TIMEOUT, socket_path=sp)
    assert isinstance(res, ec.Unavailable), res
    assert res.reason == "busy_or_stuck" and res.owner_pid is None, res
