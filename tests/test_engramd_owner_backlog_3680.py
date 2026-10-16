"""Contract tests for the owner's accept backlog and the client's busy-vs-down
split (#3680; part of #2652).

Written BEFORE the change (contract-first). Before this change the owner
called `listen(5)`: a burst of more than a handful of simultaneous clients
overflowed the kernel accept backlog, the client's connect() failed at once
with EAGAIN, and the client reported that as `Unavailable` carrying the
"restart the owner" remedy -- for an owner that was healthy, only busy.

The contract:

  * the backlog is a named constant, `OWNER_LISTEN_BACKLOG == socket.SOMAXCONN`,
    passed to listen() (constructor kwarg `listen_backlog`, tests only);
  * a burst of 32 simultaneous pings is fully served;
  * a connect that meets a FULL backlog returns `Unavailable` whose detail
    says busy and whose remedy says retry, NOT restart the owner (it is still
    an Unavailable: the section-5.5 two-kind shape is unchanged);
  * an owner that is really down (no socket file / nobody listening) keeps
    today's `Unavailable`, byte for byte, restart remedy included.

Platform: the full-backlog errno is EAGAIN on Linux. On Darwin it is
ECONNREFUSED (measured by Luria on Darwin 27), the same errno as owner-down,
so errno alone cannot tell the two apart there; the backlog-full cell is a
strict xfail on Darwin until #3707 adds a different discriminator.

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import errno
import platform
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner
from _owner_store import seed_current_store

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

PING_TIMEOUT = 2.0
SLACK = 0.5

# Today's owner-down remedy line, verbatim prefix (engramd_client._unavailable).
RESTART_STEP_PREFIX = "Check whether the engramd process is running and (re)start it"


@pytest.fixture
def engram_home():
    d = tempfile.mkdtemp(prefix="edb-")
    seed_current_store(d)  # #3753: the owner refuses a home with no current-schema store
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _start(home: Path, **kw) -> "eo.EngramdOwner":
    owner = eo.EngramdOwner(home, **kw)
    owner.start(install_signal_handlers=False)
    return owner


def _burst(owner, n: int, release=None, release_after: float = 0.0):
    """n pings behind a barrier; returns [(result, seconds)]. `release` (an
    Event) is set `release_after` seconds after the clients are launched."""
    barrier = threading.Barrier(n)
    out = [None] * n

    def run(i):
        barrier.wait()
        t0 = time.monotonic()
        res = ec.read("ping", {}, timeout=PING_TIMEOUT, socket_path=owner.socket_path)
        out[i] = (res, time.monotonic() - t0)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    if release is not None:
        time.sleep(release_after)
        release.set()
    for t in threads:
        t.join(PING_TIMEOUT + 5)
    assert all(o is not None for o in out), "a client never returned"
    return out


def _carries_restart_remedy(res) -> bool:
    return any(s.startswith(RESTART_STEP_PREFIX) for s in res.agent_steps)


def _is_busy(res) -> bool:
    return (
        isinstance(res, ec.Unavailable)
        and "busy" in res.detail.lower()
        and not _carries_restart_remedy(res)
        and any("retry" in s.lower() for s in res.agent_steps)
        # The busy remedy's whole promise: it must SAY not to restart, not merely
        # omit the restart step (Sol's surviving mutant F on #3690).
        and any("do not restart" in s.lower() for s in res.agent_steps)
    )


def test_backlog_is_a_named_constant_equal_to_somaxconn():
    """Mutation: a hand-picked number (e.g. 5 or 128) for the constant or the
    constructor default -> red."""
    assert eo.OWNER_LISTEN_BACKLOG == socket.SOMAXCONN
    assert eo.EngramdOwner(Path("/nonexistent-edb")).listen_backlog == socket.SOMAXCONN


@requires_linux_owner
def test_start_passes_the_owners_backlog_to_listen(engram_home, monkeypatch):
    """Mutation: start() calls listen(5) (or any literal) -> the recorded
    backlog differs -> red."""
    seen = []

    class Recording(socket.socket):
        def listen(self, *a):
            seen.append(a)
            return super().listen(*a)

    class Proxy:
        socket = Recording

        def __getattr__(self, name):
            return getattr(socket, name)

    monkeypatch.setattr(eo, "socket", Proxy())
    owner = _start(engram_home, listen_backlog=7)
    try:
        assert seen == [(7,)], seen
    finally:
        owner.stop()
    seen.clear()
    owner = _start(engram_home)
    try:
        assert seen == [(eo.OWNER_LISTEN_BACKLOG,)], seen
    finally:
        owner.stop()


@requires_linux_owner
def test_a_burst_of_32_simultaneous_pings_is_all_ok(engram_home):
    """Mutation: listen(5) (or a default listen_backlog of 5) -> the burst
    overflows the backlog -> some clients get Unavailable -> red."""
    owner = _start(engram_home)
    try:
        out = _burst(owner, 32)
    finally:
        owner.stop()
    bad = [r for r, _ in out if not isinstance(r, ec.Ok)]
    assert not bad, bad[:3]


@requires_linux_owner
@pytest.mark.xfail(
    platform.system() == "Darwin",
    reason="Darwin cannot tell busy from stuck: a full AF_UNIX backlog fails with "
    "ECONNREFUSED, as does a live owner that is not listening. The Darwin "
    "counterpart is #3707 cell (a), busy_or_stuck (tests/"
    "test_engramd_client_darwin_refused_3707.py); this cell stays the Linux-busy "
    "contract, so strict: it must keep failing on Darwin.",
    strict=True,
)
def test_a_full_backlog_is_busy_loud_and_never_restart(engram_home, monkeypatch):
    """Backlog of 1, accept loop held, burst of 8. Every client is Ok or a
    busy Unavailable; at least one IS busy (positive control: without it the
    cell proves nothing); none carries the restart remedy; none outlasts its
    timeout. Mutation: map the connect EAGAIN back to the generic OSError
    path -> busy clients carry the restart remedy / detail lacks 'busy' -> red."""
    gate = threading.Event()
    real = eo.EngramdOwner._accept_loop

    def held(self):
        gate.wait(10)
        return real(self)

    monkeypatch.setattr(eo.EngramdOwner, "_accept_loop", held)
    owner = _start(engram_home, listen_backlog=1)
    try:
        out = _burst(owner, 8, release=gate, release_after=1.0)
    finally:
        gate.set()
        owner.stop()
    busy = [r for r, _ in out if _is_busy(r)]
    assert len(busy) >= 1, [r for r, _ in out]
    for res, took in out:
        assert isinstance(res, ec.Ok) or _is_busy(res), res
        if isinstance(res, ec.Unavailable):
            assert not _carries_restart_remedy(res), res
        assert took < PING_TIMEOUT + SLACK, took
    # The original errno text stays in the detail, for diagnosis.
    assert all("resource temporarily unavailable" in r.detail.lower() for r in busy)


def _assert_todays_owner_down(res, sp, detail_check):
    assert isinstance(res, ec.Unavailable), res
    assert detail_check(res.detail), res.detail
    assert "busy" not in res.detail.lower()
    assert res.agent_steps == [
        "Retry the call after a short backoff -- engramd may recover on its own.",
        f"Check whether the engramd process is running and (re)start it if not "
        f"(the owner should be listening on {sp}).",
    ]
    assert res.human_steps == [
        "If retries keep failing, restart your Claude Code session.",
        "Run /mcp to reconnect the MCP server, or check the engramd logs for "
        "the underlying failure.",
    ]


def test_owner_down_no_socket_file_is_unchanged(engram_home):
    """FileNotFoundError. Mutation: route every OSError to the busy path ->
    detail says busy / remedy changes -> red."""
    sp = str(engram_home / "nope.sock")
    res = ec.read("ping", {}, timeout=PING_TIMEOUT, socket_path=sp)
    _assert_todays_owner_down(res, sp, lambda d: "No such file" in d)


def test_owner_down_nobody_listening_is_unchanged(engram_home):
    """ConnectionRefusedError: a socket file left behind with nothing
    listening. Mutation: route every OSError to the busy path -> red.

    #3707: the stale socket sits at the standard <home>/run/ layout with no
    pidfile, so "no owner" is known on every platform. At a non-standard path
    the pidfile location is unknown and Darwin answers busy_or_stuck by
    contract (#3707 cell (h))."""
    run_dir = engram_home / ec.SOCKET_DIR_NAME
    run_dir.mkdir(exist_ok=True)
    sp = str(run_dir / ec.DEFAULT_SOCKET_NAME)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(sp)
    s.close()  # the file stays, nobody listens
    res = ec.read("ping", {}, timeout=PING_TIMEOUT, socket_path=sp)
    _assert_todays_owner_down(res, sp, lambda d: "refused" in d.lower())


def test_eagain_from_sendall_keeps_the_generic_path(engram_home, monkeypatch):
    """The busy mapping covers the CONNECT step only. A socket whose connect()
    succeeds but whose sendall() raises EAGAIN must get the generic
    post-connect Unavailable (now `protocol`, per #3707 ruling 6112946218),
    never the busy one. Mutation: map
    EAGAIN from sendall() to the busy path too -> detail says busy -> red
    (Sol's surviving mutant H on #3690)."""
    real_socket = ec.socket.socket

    class _SendallEagain(real_socket):
        def connect(self, address):
            return None

        def sendall(self, data, *args):
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(ec.socket, "socket", _SendallEagain)
    sp = str(engram_home / "x.sock")
    res = ec.read("ping", {}, timeout=PING_TIMEOUT, socket_path=sp)
    assert isinstance(res, ec.Unavailable), res
    assert res.reason == "protocol", res
    assert "temporarily unavailable" in res.detail.lower(), res.detail
    assert "busy" not in res.detail.lower()
    assert not any("restart" in s.lower() for s in res.agent_steps + res.human_steps)
    assert res.agent_steps == [
        "The owner was reached, but the exchange failed without a valid answer. "
        "Retry once. If the retry returns `down`, follow its steps. If it fails "
        "as `protocol` again, check the engramd logs for why the owner dropped "
        "this request.",
    ]
    assert res.human_steps == [
        "Check the engramd logs for why the owner dropped this request.",
    ]
