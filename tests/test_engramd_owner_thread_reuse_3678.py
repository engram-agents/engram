"""Contract tests for the owner's connection-handler thread reuse (#3678 U7,
cut 1; part of #2652).

Written BEFORE the change (contract-first). The profile on #3678 put ~0.066 ms
of every owner round trip in starting a fresh thread per accepted connection.
The cut: hand each connection to an IDLE handler thread when one exists, and
spawn a new one only when none is idle.

The contract:

  * sequential connections are served by REUSED threads, not one new thread
    each;
  * capacity is never below today's: when every handler is busy -- including
    clients that connect and then hold their connection without sending, past
    the request deadline -- a fresh client still gets a handler at once
    (Kepler's rider: a bounded pool and the deadline guard can deadlock each
    other in a way unbounded spawn never did);
  * idle handlers beyond a small cap exit after an idle timeout, so a burst
    does not leave a crowd of threads behind;
  * stop() releases every idle handler: after it returns, no handler thread
    is left alive;
  * the per-request deadline and every existing owner check are unchanged
    (the existing owner suites run unmodified; this file adds no exemption).

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

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

REQUEST_DEADLINE = 0.6
# Bursts stay small. Before #3680 the owner's listen(5) backlog overflowed above ~5
# simultaneous connects (EAGAIN -> Unavailable; measured: 16 clients -> 8 Unavailable,
# 12 -> 5, 5 -> 0); the backlog is now SOMAXCONN and the burst/backlog contracts live in
# test_engramd_owner_backlog_3680.py. These tests check handler reuse, not the backlog.
BURST = 5


@pytest.fixture
def engram_home():
    d = tempfile.mkdtemp(prefix="edr-")
    seed_current_store(d)  # #3753: the owner refuses a home with no current-schema store
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _start(home: Path, **kw) -> "eo.EngramdOwner":
    owner = eo.EngramdOwner(home, request_deadline=REQUEST_DEADLINE, **kw)
    owner.start(install_signal_handlers=False)
    return owner


def _workers() -> list:
    return [t for t in threading.enumerate()
            if t.name.startswith(eo.WORKER_THREAD_PREFIX) and t.is_alive()]


def _ping(owner) -> "ec.EngramdResult":
    return ec.read("ping", {}, timeout=2.0, socket_path=owner.socket_path)


def _wait_until(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@requires_linux_owner
def test_sequential_connections_reuse_handler_threads(engram_home):
    """Thirty sequential pings are served by at most two distinct handler
    threads. Mutation: spawn a new thread per connection (today's code) ->
    thirty distinct handlers -> red."""
    owner = _start(engram_home)
    seen = set()
    real = owner._handle_connection

    def record(conn):
        seen.add(threading.current_thread().name)
        return real(conn)

    owner._handle_connection = record
    try:
        for _ in range(30):
            assert isinstance(_ping(owner), ec.Ok)
    finally:
        owner.stop()
    assert all(n.startswith(eo.WORKER_THREAD_PREFIX) for n in seen), seen
    assert 1 <= len(seen) <= 2, seen


@requires_linux_owner
def test_a_fresh_client_is_served_while_every_handler_is_held(engram_home):
    """Kepler's rider. Eight clients connect and send nothing (each holds a
    handler until the request deadline); a ninth client's ping must answer
    well inside that deadline. Mutation: a bounded pool (no spawn when no
    handler is idle) -> the ninth waits for a holder's deadline -> red."""
    owner = _start(engram_home, idle_worker_cap=2)
    holders = []
    try:
        for _ in range(8):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(owner.socket_path))
            holders.append(s)
        time.sleep(0.05)  # let the eight be accepted and parked in their reads
        t0 = time.monotonic()
        res = _ping(owner)
        took = time.monotonic() - t0
        assert isinstance(res, ec.Ok), res
        assert took < REQUEST_DEADLINE / 3, took
    finally:
        for s in holders:
            s.close()
        owner.stop()


@requires_linux_owner
def test_held_handlers_come_back_after_the_deadline(engram_home):
    """After holders time out at the request deadline, their handlers are free
    again and serve new pings. Mutation: a handler that never returns to idle
    after a deadline drop (or dies) while a cap forbids replacements -> red."""
    owner = _start(engram_home, idle_worker_cap=2)
    holders = []
    try:
        for _ in range(4):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(owner.socket_path))
            holders.append(s)
        time.sleep(REQUEST_DEADLINE + 0.3)
        for _ in range(10):
            assert isinstance(_ping(owner), ec.Ok)
    finally:
        for s in holders:
            s.close()
        owner.stop()


@requires_linux_owner
def test_idle_handlers_beyond_the_cap_exit(engram_home):
    """BURST clients each hold a connection without sending, so each needs its
    own handler at once (a fast ping burst does NOT: handlers get reused inside
    the burst, measured 10 of 25 runs spawning only 2). Once the deadline drops
    them and they sit idle past the idle timeout, at most the cap remain.
    Mutation: never reap idle handlers -> BURST stay -> red."""
    owner = _start(engram_home, idle_worker_cap=2, idle_worker_timeout=0.3)
    holders = []
    try:
        for _ in range(BURST):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(owner.socket_path))
            holders.append(s)
        assert _wait_until(lambda: len(_workers()) == BURST, timeout=1.0), \
            f"control: {BURST} held connections need {BURST} handlers, saw {len(_workers())}"
        assert _wait_until(lambda: len(_workers()) <= 2, timeout=REQUEST_DEADLINE + 3.0), len(_workers())
        assert isinstance(_ping(owner), ec.Ok)
    finally:
        for s in holders:
            s.close()
        owner.stop()


@requires_linux_owner
def test_stop_leaves_no_handler_thread_alive(engram_home):
    """stop() wakes and ends every idle handler. Mutation: idle handlers that
    are never told to exit (they would linger until their idle timeout, or
    forever with no timeout) -> red."""
    owner = _start(engram_home, idle_worker_cap=4, idle_worker_timeout=60.0)
    for _ in range(5):
        assert isinstance(_ping(owner), ec.Ok)
    assert _workers(), "control: a handler should be idle before stop()"
    t0 = time.monotonic()
    owner.stop()
    assert time.monotonic() - t0 < owner.accept_poll_interval + 2.5
    assert _wait_until(lambda: not _workers(), timeout=2.0), _workers()


@requires_linux_owner
def test_concurrent_reads_all_answer(engram_home):
    """BURST concurrent clients, each ten pings, all Ok (handlers are reused
    across the run, so hand-offs happen many times). Mutation: a hand-off that
    can drop or double-serve a connection -> red."""
    owner = _start(engram_home, idle_worker_cap=2, idle_worker_timeout=0.5)
    out = []
    lock = threading.Lock()

    def client():
        mine = [_ping(owner) for _ in range(10)]
        with lock:
            out.extend(mine)

    try:
        ts = [threading.Thread(target=client) for _ in range(BURST)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    finally:
        owner.stop()
    assert len(out) == BURST * 10
    assert all(isinstance(r, ec.Ok) for r in out)


@requires_linux_owner
def test_a_fresh_client_is_served_after_a_handler_was_reused_then_held(engram_home):
    """The never-queued property on the path where a handler HAS gone idle once
    (Kepler's deep review of #3686; the rider cell above holds its connections
    before any handler was ever idle, so it cannot see this path).
    Mutation: dispatch hands a connection to an idle handler without decrementing
    _idle -> the next fresh client queues behind the held handler -> red."""
    owner = _start(engram_home, idle_worker_cap=2)
    holder = None
    try:
        assert isinstance(_ping(owner), ec.Ok)          # handler 1 goes idle
        time.sleep(0.05)
        holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        holder.connect(str(owner.socket_path))          # taken by the idle handler, held
        time.sleep(0.05)
        t0 = time.monotonic()
        res = _ping(owner)
        took = time.monotonic() - t0
        assert isinstance(res, ec.Ok), res
        assert took < REQUEST_DEADLINE / 3, took
    finally:
        if holder is not None:
            holder.close()
        owner.stop()
