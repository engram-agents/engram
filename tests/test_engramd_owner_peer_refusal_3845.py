"""Contract tests for the peer-credential refusal path of the engramd owner
(#3845, part of #2652): `EngramdOwner._handle_connection` on a peer-identity
rejection.

Two operability defects, neither a security weakness (nothing is served):

  1. The owner sent the typed rejection and closed WITHOUT reading the
     client's request. On Linux, closing a socket that still holds unread
     received data resets the peer: the typed rejection is delivered, but a
     client that sends first and reads to EOF then gets ConnectionResetError.
  2. The owner logged nothing on a refusal.

The contract (tests written before the change; each cell was red on the
unmodified owner where it states a new behaviour):

  C1  after the typed rejection the owner does shutdown(SHUT_WR), DRAINS the
      request until EOF / the drain budget / the byte cap, never parsing it,
      then closes. A send-first read-to-EOF client gets `peer_identity` and no
      reset, 20 of 20.
  C2  the drain is bounded: min(REFUSAL_DRAIN_BUDGET, remaining deadline), and
      at most `max_request_bytes` consumed.
  C3  one bounded WARNING line per refusal on logger "engramd_owner": peer uid,
      owner uid, reason, no request bytes; rate-limited per peer uid with a lazy
      window (no timer), the table of tracked uids bounded.
  C4  the wire shape, the same-uid path and the uid-0 branch are unchanged.

How a refusal is driven: through the REAL `_check_peer_credentials`. Most
cells hand `_handle_connection` a thin proxy over one end of a socketpair
whose `getsockopt(SO_PEERCRED)` reports a chosen peer uid (the fake-conn
pattern of tests/test_engramd_owner.py's short-buffer cell), so the real
parse and the real mismatch branch run. The 20-of-20 cells use a REAL started
owner and make its view of its own uid differ (the #3097 experiment).

Darwin: the cells need a Linux kernel (SO_PEERCRED); a Mac seat cannot run
them here, the drain and the log live in the platform-agnostic part of
`_handle_connection` so both arms share them.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from _owner_platform import requires_linux_kernel, requires_linux_owner
from _owner_store import seed_current_store

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

REQUEST_DEADLINE = 0.6
MARGIN = 0.3  # seconds of slack on a "released within the budget" bound
LOOPS = 20
FOREIGN_UID = 424242  # obviously fake; never an owner uid
SENTINEL = "SENTINEL-FAKE-PAYLOAD-3845"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def engram_home():
    d = tempfile.mkdtemp(prefix="edo-")
    seed_current_store(d)
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def started_owner(engram_home):
    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        yield owner
    finally:
        owner.stop()


class _OsView:
    """`os` as the owner module sees it, except `getuid` -- so the REAL
    `_check_peer_credentials` finds the (genuinely same-uid) peer foreign."""

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def getuid():
        return os.getuid() + 1


@pytest.fixture
def foreign_view(monkeypatch):
    monkeypatch.setattr(eo, "os", _OsView())


class _Clock:
    now = 1000.0


class _TimeView:
    """`time` as the owner module sees it, with a hand-driven monotonic clock
    (no real 60-second sleeps)."""

    def __getattr__(self, name):
        return getattr(time, name)

    @staticmethod
    def monotonic():
        return _Clock.now


@pytest.fixture
def clock(monkeypatch):
    _Clock.now = 1000.0
    monkeypatch.setattr(eo, "time", _TimeView())
    return _Clock


class _PeerConn:
    """A proxy over one end of a socketpair: SO_PEERCRED reports `peer_uid`
    (None = the kernel's real answer) and every recv() is counted."""

    def __init__(self, sock, peer_uid=None):
        self._sock = sock
        self._peer_uid = peer_uid
        self.recv_bytes = 0

    def getsockopt(self, level, optname, buflen=0):
        if self._peer_uid is not None and optname == socket.SO_PEERCRED:
            return struct.pack("@3i", 4242, self._peer_uid, 4242)
        return self._sock.getsockopt(level, optname, buflen)

    def recv(self, n):
        chunk = self._sock.recv(n)
        self.recv_bytes += len(chunk)
        return chunk

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _bare_owner(engram_home, **kw):
    kw.setdefault("request_deadline", 5.0)
    return eo.EngramdOwner(engram_home, **kw)


def _refuse(owner, *, peer_uid=FOREIGN_UID, request=b"", half_close=True):
    """Run one refusal through `_handle_connection` on a socketpair. The client
    sends `request`, optionally half-closes (EOF), the handler runs
    synchronously, then the client reads whatever the owner sent. Returns
    (reply_dict_or_None, counted_conn)."""
    srv, cli = socket.socketpair()
    conn = _PeerConn(srv, peer_uid)
    try:
        if request:
            cli.sendall(request)
        if half_close:
            cli.shutdown(socket.SHUT_WR)
        owner._handle_connection(conn)
        cli.settimeout(2.0)
        data = b""
        while b"\n" not in data:
            chunk = cli.recv(65536)
            if not chunk:
                break
            data += chunk
        return (json.loads(data.decode()) if data.strip() else None), conn
    finally:
        cli.close()
        srv.close()


def _refusal_lines(caplog):
    return [
        r for r in caplog.records
        if r.name == "engramd_owner" and "peer refused" in r.getMessage()
    ]


def _ping_line() -> bytes:
    return (json.dumps({"contract_version": ec.CONTRACT_VERSION, "method": "ping",
                        "params": {}, "request_id": None}) + "\n").encode()


# ---------------------------------------------------------------------------
# C1 -- a client that reads to EOF after sending first gets a clean rejection
#
# Measured mechanism: the typed rejection IS delivered, but a client that
# reads until EOF then gets ConnectionResetError, because the owner closed
# with the client's request still unread in its receive queue. shutdown(SHUT_WR)
# alone does not prevent that; shutdown plus a drain does. Clients that stop
# at the first newline never see the reset (the controls below).
# ---------------------------------------------------------------------------


def _client(socket_path, *, send_first, style):
    """Returns (parsed_rejection_or_None, error_or_None). style: 'eof' reads
    until b'' (a reset raised during the loop is returned as the error),
    'newline' reads until a newline, 'one' does a single recv."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    data = b""
    try:
        sock.connect(str(socket_path))
        if send_first:
            try:
                sock.sendall(_ping_line())
            except OSError:
                pass  # the owner may already have closed; the reply is still readable
        if style == "one":
            data = sock.recv(65536)
        else:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
                if style == "newline" and b"\n" in data:
                    break
        err = None
    except OSError as exc:
        err = exc
    finally:
        sock.close()
    try:
        parsed = json.loads(data.decode().strip()) if data.strip() else None
    except ValueError:
        parsed = None
    return parsed, err


def _assert_clean_rejections(results, label):
    good = [r for r, e in results if r is not None and r.get("reason") == "peer_identity" and e is None]
    assert len(good) == LOOPS, f"{label}: clean rejection {len(good)}/{LOOPS}: {results!r}"


@requires_linux_owner
@requires_linux_kernel
def test_send_first_read_to_eof_client_gets_a_clean_rejection_every_time(started_owner, foreign_view):
    results = []
    for _ in range(LOOPS):
        t0 = time.monotonic()
        results.append(_client(started_owner.socket_path, send_first=True, style="eof"))
        # EOF must arrive from shutdown(SHUT_WR), not from the owner's drain budget running out.
        assert time.monotonic() - t0 < eo.REFUSAL_DRAIN_BUDGET - 0.2, "EOF came late: no shutdown(SHUT_WR)?"
    _assert_clean_rejections(results, "send-first read-to-EOF")


@requires_linux_owner
@requires_linux_kernel
def test_control_read_only_read_to_eof_client(started_owner, foreign_view):
    results = [_client(started_owner.socket_path, send_first=False, style="eof") for _ in range(LOOPS)]
    _assert_clean_rejections(results, "read-only read-to-EOF")


@requires_linux_owner
@requires_linux_kernel
def test_control_send_first_one_recv_client(started_owner, foreign_view):
    results = [_client(started_owner.socket_path, send_first=True, style="one") for _ in range(LOOPS)]
    _assert_clean_rejections(results, "send-first one-recv")


@requires_linux_owner
@requires_linux_kernel
def test_control_send_first_read_until_newline_client(started_owner, foreign_view):
    results = [_client(started_owner.socket_path, send_first=True, style="newline") for _ in range(LOOPS)]
    _assert_clean_rejections(results, "send-first read-until-newline")


@requires_linux_owner
@requires_linux_kernel
def test_control_shipped_client_read(started_owner, foreign_view):
    results = [
        ec.read("ping", {}, timeout=3.0, socket_path=started_owner.socket_path)
        for _ in range(LOOPS)
    ]
    ok = [r for r in results if isinstance(r, ec.Rejected) and r.reason == "peer_identity"]
    assert len(ok) == LOOPS, f"engramd_client.read got peer_identity {len(ok)}/{LOOPS}: {results!r}"


# ---------------------------------------------------------------------------
# C2 -- the drain is bounded
# ---------------------------------------------------------------------------


def test_budget_constants_are_named_and_pinned():
    assert eo.REFUSAL_DRAIN_BUDGET == 0.5
    assert eo.PEER_REFUSAL_LOG_WINDOW == 60
    assert eo.PEER_REFUSAL_LOG_MAX_UIDS == 64


@requires_linux_kernel
def test_peer_that_never_sends_and_never_closes_is_released_within_the_budget(engram_home):
    # Replaces today's instant close with a bounded hold of <= REFUSAL_DRAIN_BUDGET.
    owner = _bare_owner(engram_home, request_deadline=5.0)
    t0 = time.monotonic()
    reply, _ = _refuse(owner, half_close=False)
    elapsed = time.monotonic() - t0
    assert reply is not None and reply["reason"] == "peer_identity"
    assert elapsed <= eo.REFUSAL_DRAIN_BUDGET + MARGIN, f"held {elapsed:.3f}s"
    assert elapsed >= eo.REFUSAL_DRAIN_BUDGET - 0.1, f"released early, {elapsed:.3f}s (no drain?)"


@requires_linux_kernel
def test_drain_budget_is_capped_by_the_remaining_deadline(engram_home):
    owner = _bare_owner(engram_home, request_deadline=0.2)
    t0 = time.monotonic()
    reply, _ = _refuse(owner, half_close=False)
    elapsed = time.monotonic() - t0
    assert reply is not None and reply["reason"] == "peer_identity"
    assert elapsed <= 0.2 + 0.15, f"held {elapsed:.3f}s past a 0.2s request deadline"


@requires_linux_kernel
def test_endless_sender_is_released_within_the_budget(engram_home):
    # A cap far above what the trickle can reach, so ONLY the budget can end it;
    # a request deadline far above the budget, so a drain that used the deadline
    # would run for 5 s.
    owner = _bare_owner(engram_home, request_deadline=5.0, max_request_bytes=10**12)
    srv, cli = socket.socketpair()
    conn = _PeerConn(srv, FOREIGN_UID)
    stop = threading.Event()

    def trickle():
        while not stop.is_set():
            try:
                cli.sendall(b"x")
            except OSError:
                return
            time.sleep(0.005)

    sender = threading.Thread(target=trickle, daemon=True)
    sender.start()
    try:
        t0 = time.monotonic()
        owner._handle_connection(conn)
        elapsed = time.monotonic() - t0
    finally:
        stop.set()
        sender.join(timeout=2)
        cli.close()
        srv.close()
    assert elapsed <= eo.REFUSAL_DRAIN_BUDGET + MARGIN, f"endless sender held the thread {elapsed:.3f}s"
    assert conn.recv_bytes > 0, "nothing was drained"


@requires_linux_kernel
def test_over_cap_sender_is_cut_off_at_the_cap(engram_home):
    owner = _bare_owner(engram_home, request_deadline=5.0, max_request_bytes=1000)
    t0 = time.monotonic()
    reply, conn = _refuse(owner, request=b"y" * 5000, half_close=False)
    elapsed = time.monotonic() - t0
    assert reply is not None and reply["reason"] == "peer_identity"
    assert conn.recv_bytes == 1000, f"consumed {conn.recv_bytes} bytes, cap is 1000"
    assert elapsed <= eo.REFUSAL_DRAIN_BUDGET, "a capped drain should end at the cap, not the budget"


@requires_linux_kernel
def test_the_drained_request_is_never_parsed(engram_home, monkeypatch):
    owner = _bare_owner(engram_home)
    srv, cli = socket.socketpair()
    try:
        cli.sendall(_ping_line() + b"\xff\xfe not json \x00\n")
        cli.shutdown(socket.SHUT_WR)

        def boom(*a, **k):
            raise AssertionError("the refusal path parsed JSON")

        monkeypatch.setattr(json, "loads", boom)
        owner._handle_connection(_PeerConn(srv, FOREIGN_UID))
        monkeypatch.undo()
        assert json.loads(cli.recv(65536).decode())["reason"] == "peer_identity"
    finally:
        srv.close()
        cli.close()


# ---------------------------------------------------------------------------
# C3 -- one bounded log line per refusal
# ---------------------------------------------------------------------------


@pytest.fixture
def warn_log(caplog):
    caplog.set_level(logging.WARNING, logger="engramd_owner")
    return caplog


@requires_linux_kernel
def test_one_refusal_logs_exactly_one_warning_naming_both_uids_and_the_reason(engram_home, warn_log, clock):
    owner = _bare_owner(engram_home)
    _refuse(owner, peer_uid=FOREIGN_UID)
    lines = _refusal_lines(warn_log)
    assert len(lines) == 1, [r.getMessage() for r in warn_log.records]
    rec = lines[0]
    msg = rec.getMessage()
    assert rec.levelno == logging.WARNING
    assert f"uid {FOREIGN_UID}" in msg
    assert f"owner uid {os.getuid()}" in msg
    assert "peer_identity" in msg
    assert f"suppressed for {eo.PEER_REFUSAL_LOG_WINDOW}s" in msg
    assert "in the previous window" not in msg


@requires_linux_kernel
def test_request_bytes_never_reach_the_log(engram_home, warn_log, clock):
    owner = _bare_owner(engram_home)
    request = (json.dumps({"method": "ping", "note": SENTINEL}) + "\n").encode()
    _refuse(owner, request=request)
    assert _refusal_lines(warn_log), "no refusal line was logged"
    assert SENTINEL not in warn_log.text
    for r in warn_log.records:
        assert SENTINEL not in r.getMessage()
        assert SENTINEL not in repr(r.args)


@requires_linux_kernel
def test_burst_logs_once_then_reports_n_minus_one_suppressed_in_the_next_window(engram_home, warn_log, clock):
    owner = _bare_owner(engram_home)
    n = 5
    for _ in range(n):
        _refuse(owner)
    assert len(_refusal_lines(warn_log)) == 1

    clock.now += eo.PEER_REFUSAL_LOG_WINDOW + 1
    _refuse(owner)
    lines = _refusal_lines(warn_log)
    assert len(lines) == 2
    second = lines[1].getMessage()
    assert f"{n - 1} suppressed in the previous window" in second, second
    assert f"suppressed for {eo.PEER_REFUSAL_LOG_WINDOW}s" in second

    # the new window is itself rate-limited
    _refuse(owner)
    assert len(_refusal_lines(warn_log)) == 2


@requires_linux_kernel
def test_burst_followed_by_silence_never_prints_its_count(engram_home, warn_log, clock):
    # Lazy window, no timer: the count is carried by the NEXT refusal from that
    # uid. If none comes, it is never printed. That is the design, not a bug.
    owner = _bare_owner(engram_home)
    for _ in range(4):
        _refuse(owner)
    clock.now += 10 * eo.PEER_REFUSAL_LOG_WINDOW
    assert len(_refusal_lines(warn_log)) == 1
    assert not any("in the previous window" in r.getMessage() for r in warn_log.records)


@requires_linux_kernel
def test_window_edge_holds_inside_and_opens_after(engram_home, warn_log, clock):
    owner = _bare_owner(engram_home)
    _refuse(owner)
    clock.now += eo.PEER_REFUSAL_LOG_WINDOW - 1
    _refuse(owner)
    assert len(_refusal_lines(warn_log)) == 1, "still inside the window: must be suppressed"
    clock.now += 2
    _refuse(owner)
    assert len(_refusal_lines(warn_log)) == 2


@requires_linux_kernel
def test_two_peer_uids_are_limited_independently(engram_home, warn_log, clock):
    owner = _bare_owner(engram_home)
    uid_a, uid_b = FOREIGN_UID, FOREIGN_UID + 1
    for _ in range(3):
        _refuse(owner, peer_uid=uid_a)
    for _ in range(2):
        _refuse(owner, peer_uid=uid_b)
    lines = _refusal_lines(warn_log)
    assert len(lines) == 2
    assert f"uid {uid_a}" in lines[0].getMessage() and f"uid {uid_b}" in lines[1].getMessage()

    clock.now += eo.PEER_REFUSAL_LOG_WINDOW + 1
    _refuse(owner, peer_uid=uid_a)
    _refuse(owner, peer_uid=uid_b)
    lines = _refusal_lines(warn_log)
    assert len(lines) == 4
    assert "2 suppressed in the previous window" in lines[2].getMessage()
    assert f"uid {uid_a}" in lines[2].getMessage()
    assert "1 suppressed in the previous window" in lines[3].getMessage()
    assert f"uid {uid_b}" in lines[3].getMessage()


@requires_linux_kernel
def test_table_is_bounded_evicts_the_oldest_and_an_evicted_uid_starts_fresh(engram_home, warn_log, clock):
    owner = _bare_owner(engram_home)
    cap = eo.PEER_REFUSAL_LOG_MAX_UIDS
    uids = [FOREIGN_UID + i for i in range(cap)]
    for u in uids:
        _refuse(owner, peer_uid=u)
        clock.now += 0.001
    # the oldest uid carries a suppressed count that eviction will discard
    _refuse(owner, peer_uid=uids[0])
    assert len(owner._peer_refusal_log) == cap
    assert len(_refusal_lines(warn_log)) == cap

    newcomer = FOREIGN_UID + cap
    _refuse(owner, peer_uid=newcomer)
    assert len(owner._peer_refusal_log) == cap, "table grew past its bound"
    assert uids[0] not in owner._peer_refusal_log, "the oldest was not the one evicted"
    assert newcomer in owner._peer_refusal_log
    assert all(u in owner._peer_refusal_log for u in uids[1:]), "a non-oldest uid was evicted"

    # the evicted uid returns inside the original window: fresh window, no stale count
    before = len(_refusal_lines(warn_log))
    _refuse(owner, peer_uid=uids[0])
    lines = _refusal_lines(warn_log)
    assert len(lines) == before + 1, "an evicted uid's return must log (fresh window)"
    assert f"uid {uids[0]}" in lines[-1].getMessage()
    assert "in the previous window" not in lines[-1].getMessage()
    assert len(owner._peer_refusal_log) == cap


@requires_linux_owner
@requires_linux_kernel
def test_same_uid_path_logs_nothing_and_still_serves(started_owner, warn_log):
    result = ec.read("ping", {}, timeout=3.0, socket_path=started_owner.socket_path)
    assert isinstance(result, ec.Ok)
    assert [r for r in warn_log.records if r.name == "engramd_owner"] == []


@requires_linux_kernel
def test_uid_zero_branch_is_refused_and_logged_exactly_once(engram_home, warn_log, clock):
    if os.getuid() == 0:
        pytest.skip("the uid-0 defence-in-depth clause only differs from the mismatch rule for a non-root owner")
    owner = _bare_owner(engram_home)
    reply, _ = _refuse(owner, peer_uid=0)
    assert reply is not None and reply["reason"] == "peer_identity"
    lines = _refusal_lines(warn_log)
    assert len(lines) == 1
    assert "uid 0" in lines[0].getMessage()


# ---------------------------------------------------------------------------
# C4 -- the wire shape is unchanged
# ---------------------------------------------------------------------------


@requires_linux_kernel
def test_rejection_wire_shape_is_unchanged(engram_home, clock):
    owner = _bare_owner(engram_home)
    reply, _ = _refuse(owner, peer_uid=FOREIGN_UID)
    assert reply == {
        "status": "rejected",
        "reason": "peer_identity",
        "detail": f"connecting uid {FOREIGN_UID} is not this owner's uid {os.getuid()}",
        "remedy": "connect to the socket under your own $ENGRAM_HOME",
    }


@requires_linux_kernel
def test_check_peer_credentials_return_value_is_the_bare_wire_dict(engram_home):
    owner = _bare_owner(engram_home)
    srv, cli = socket.socketpair()
    try:
        rejection = owner._check_peer_credentials(_PeerConn(srv, FOREIGN_UID))
        assert set(rejection) == {"status", "reason", "detail", "remedy"}
        assert owner._check_peer_credentials(_PeerConn(srv, None)) is None
    finally:
        srv.close()
        cli.close()


# ---------------------------------------------------------------------------
# Drain cap (#3845 addendum): at most MAX_REFUSAL_DRAINS drains in flight,
# global across peers. A flood of silent foreign connections must not cost ~N
# handler threads. Overflow: the rejection is sent and the connection closed
# at once, with no shutdown and no drain (an overflowing read-to-EOF
# send-first client may then see ConnectionResetError after the delivered
# rejection; the rejection itself is still delivered).
# ---------------------------------------------------------------------------

FLOOD = 200
# Measured on this seat with the cap in place (see the PR report): the thread
# delta of a 200-connection silent flood. The bound separates cleanly from the
# ~200 an uncapped drain costs; it is stated in terms of the cap plus a
# generous allowance for the worker pool's idle handlers.
FLOOD_THREAD_BOUND = 40


@pytest.fixture
def long_owner(engram_home, monkeypatch):
    """A started owner whose drains last long enough (5 s deadline, 3 s drain
    budget) that a cell can hold slots open deterministically."""
    monkeypatch.setattr(eo, "REFUSAL_DRAIN_BUDGET", 3.0)
    owner = eo.EngramdOwner(engram_home, request_deadline=5.0)
    owner.start(install_signal_handlers=False)
    try:
        yield owner
    finally:
        owner.stop()


def _read_rejection(sock):
    data = b""
    while b"\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return json.loads(data.decode())


def _open_silent(owner, n, holders):
    """n connections that send nothing and stay open; each must still receive
    its typed rejection. Appended to `holders` so the caller can close them."""
    for _ in range(n):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3.0)
        holders.append(s)
        s.connect(str(owner.socket_path))
        assert _read_rejection(s)["reason"] == "peer_identity"


def _wait_drains(owner, n, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if owner._refusal_drains == n:
            return True
        time.sleep(0.005)
    return owner._refusal_drains == n


def _close_all(socks):
    for s in socks:
        try:
            s.close()
        except OSError:
            pass


@requires_linux_owner
@requires_linux_kernel
def test_flood_of_silent_foreign_connections_does_not_cost_one_thread_each(long_owner, foreign_view):
    holders = []
    before = threading.active_count()
    try:
        _open_silent(long_owner, FLOOD, holders)  # every one of the 200 got its rejection
        delta = threading.active_count() - before
    finally:
        _close_all(holders)
    assert delta <= FLOOD_THREAD_BOUND, f"{delta} extra threads for {FLOOD} silent connections"


@requires_linux_owner
@requires_linux_kernel
def test_under_the_cap_a_read_to_eof_client_is_still_drained_cleanly(long_owner, foreign_view):
    holders = []
    try:
        _open_silent(long_owner, eo.MAX_REFUSAL_DRAINS - 1, holders)
        assert _wait_drains(long_owner, eo.MAX_REFUSAL_DRAINS - 1)
        parsed, err = _client(long_owner.socket_path, send_first=True, style="eof")
    finally:
        _close_all(holders)
    assert err is None, f"clean EOF expected under the cap, got {err!r}"
    assert parsed is not None and parsed["reason"] == "peer_identity"


@requires_linux_owner
@requires_linux_kernel
def test_at_the_cap_the_extra_client_gets_the_rejection_and_is_closed_promptly(long_owner, foreign_view):
    holders = []
    try:
        _open_silent(long_owner, eo.MAX_REFUSAL_DRAINS, holders)
        assert _wait_drains(long_owner, eo.MAX_REFUSAL_DRAINS)
        t0 = time.monotonic()
        parsed, _err = _client(long_owner.socket_path, send_first=True, style="eof")  # a reset is permitted, not asserted
        elapsed = time.monotonic() - t0
        in_flight = long_owner._refusal_drains
    finally:
        _close_all(holders)
    assert parsed is not None and parsed["reason"] == "peer_identity", "the rejection bytes did not arrive"
    # the drain budget is 3.0 s here; an undrained close must come far sooner
    assert elapsed < 1.0, f"overflow client held {elapsed:.3f}s (budget {eo.REFUSAL_DRAIN_BUDGET}s)"
    assert in_flight == eo.MAX_REFUSAL_DRAINS, "overflow must not take a slot"


@requires_linux_owner
@requires_linux_kernel
def test_slots_come_back_after_the_holders_drains_expire(engram_home, foreign_view, monkeypatch):
    monkeypatch.setattr(eo, "REFUSAL_DRAIN_BUDGET", 0.3)
    owner = eo.EngramdOwner(engram_home, request_deadline=5.0)
    owner.start(install_signal_handlers=False)
    holders = []
    try:
        _open_silent(owner, eo.MAX_REFUSAL_DRAINS, holders)
        assert _wait_drains(owner, eo.MAX_REFUSAL_DRAINS)
        assert _wait_drains(owner, 0, timeout=3.0), "slots were not released when the drains expired"
        parsed, err = _client(owner.socket_path, send_first=True, style="eof")
        assert err is None and parsed is not None and parsed["reason"] == "peer_identity"
    finally:
        _close_all(holders)
        owner.stop()


@requires_linux_owner
@requires_linux_kernel
def test_a_drain_that_raises_does_not_leak_its_slot(started_owner, foreign_view):
    called = threading.Event()

    def boom(conn, deadline):
        called.set()
        raise RuntimeError("drain failed (test)")

    started_owner._drain_refused_request = boom  # instance attribute shadows the method
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    try:
        sock.connect(str(started_owner.socket_path))
        assert _read_rejection(sock)["reason"] == "peer_identity"
    finally:
        sock.close()
    assert called.wait(2.0), "the drain was never reached"
    assert _wait_drains(started_owner, 0, timeout=2.0), f"slot leaked: {started_owner._refusal_drains}"


@requires_linux_owner
@requires_linux_kernel
def test_a_failed_rejection_send_does_not_take_or_leak_a_slot(started_owner, foreign_view):
    # Clients that connect and vanish before the owner replies: its sendall()
    # fails, so no drain runs and no slot may be left taken.
    for _ in range(eo.MAX_REFUSAL_DRAINS + 4):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(started_owner.socket_path))
        s.close()
    assert _wait_drains(started_owner, 0, timeout=2.0), f"slots taken/leaked: {started_owner._refusal_drains}"
    time.sleep(0.2)
    assert started_owner._refusal_drains == 0
    parsed, err = _client(started_owner.socket_path, send_first=True, style="eof")
    assert err is None and parsed is not None and parsed["reason"] == "peer_identity"


# ---------------------------------------------------------------------------
# Review round on #3857: the rejection is ALWAYS attempted, the slot is not even
# taken when the send fails, and the Darwin arm of the uid re-read is exercised.
# ---------------------------------------------------------------------------


class _LogFilterBoom(Exception):
    """Not a RuntimeError and not an OSError: pins `except Exception`."""


class _RaisingFilter(logging.Filter):
    """A logging.Filter that raises. A raising *handler* is swallowed by the
    logging module; a raising *filter* propagates out of Logger.warning()."""

    def __init__(self, exc):
        super().__init__()
        self.exc = exc
        self.calls = 0

    def filter(self, record):
        self.calls += 1
        raise self.exc


# 20 repetitions: each is a socketpair and a fresh owner, well under a second in total.
FILTER_LOOPS = 20


def _refusals_with_raising_log_filter(engram_home, exc):
    flt = _RaisingFilter(exc)
    logger = logging.getLogger("engramd_owner")
    logger.addFilter(flt)
    results = []
    try:
        for _ in range(FILTER_LOOPS):
            # a fresh owner each time: the per-uid rate limit would otherwise
            # suppress the log call (and so the filter) from the second one on
            owner = _bare_owner(engram_home)
            request = _ping_line()
            reply, conn = _refuse(owner, request=request)  # send-first, then EOF; any raise fails the cell
            results.append((reply, conn.recv_bytes, len(request)))
    finally:
        logger.removeFilter(flt)
    return flt, results


@requires_linux_kernel
@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(RuntimeError("log filter failed (test)"), id="runtime-error"),
        pytest.param(_LogFilterBoom("log filter failed (test)"), id="custom-exception"),
    ],
)
def test_a_failing_log_call_does_not_stop_the_rejection_or_the_drain(engram_home, exc):
    flt, results = _refusals_with_raising_log_filter(engram_home, exc)
    assert flt.calls == FILTER_LOOPS, f"the filter ran {flt.calls}/{FILTER_LOOPS}: the cell would be vacuous"
    for reply, drained, sent in results:
        assert reply is not None and reply["reason"] == "peer_identity", f"no typed rejection: {reply!r}"
        assert drained == sent, f"the drain did not run: read {drained} of {sent} request bytes"


class _TakeSpy:
    def __init__(self, real):
        self.real = real
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.real()


@requires_linux_kernel
def test_the_drain_slot_is_not_even_taken_when_the_rejection_send_fails(engram_home):
    owner = _bare_owner(engram_home)
    spy = _TakeSpy(owner._take_refusal_drain_slot)
    owner._take_refusal_drain_slot = spy  # instance attribute shadows the method
    try:
        # the peer is already gone, so the owner's rejection sendall() fails
        srv, cli = socket.socketpair()
        cli.close()
        try:
            owner._handle_connection(_PeerConn(srv, FOREIGN_UID))
        finally:
            srv.close()
        assert spy.calls == 0, "a slot was taken before the rejection was delivered"
        assert owner._refusal_drains == 0

        # control: the spy is live, a delivered rejection does take (and release) one
        reply, _ = _refuse(owner, request=_ping_line())
        assert reply is not None and reply["reason"] == "peer_identity"
        assert spy.calls == 1
        assert owner._refusal_drains == 0
    finally:
        del owner._take_refusal_drain_slot


# Darwin arm of `_refused_peer_uids`. This exercises the arm with a FAKE socket
# on Linux, not a real macOS kernel: it pins which struct field is read and that
# a failed read never raises, not what a Mac kernel actually returns.

DARWIN_CR_VERSION = 0  # xucred's real version; deliberately not the uid under test


class _DarwinPeerConn:
    def __init__(self, *, uid=None, error=None, raw=None):
        self.uid = uid
        self.error = error
        self.raw = raw
        self.calls = []

    def getsockopt(self, level, optname, buflen=0):
        self.calls.append((level, optname, buflen))
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        groups = [0] * 16
        return struct.pack(eo.DARWIN_PEERCRED_STRUCT_FORMAT, DARWIN_CR_VERSION, self.uid, 1, *groups)


@pytest.fixture
def darwin_arm(monkeypatch):
    monkeypatch.setattr(eo, "_owner_arm", lambda: "darwin")
    monkeypatch.setattr(eo.socket, "LOCAL_PEERCRED", 1, raising=False)  # absent on Linux


@pytest.mark.parametrize("chosen_uid", [FOREIGN_UID, 501])
def test_darwin_arm_reports_cr_uid_not_cr_version(engram_home, darwin_arm, chosen_uid):
    owner = _bare_owner(engram_home)
    conn = _DarwinPeerConn(uid=chosen_uid)
    peer_uid, own_uid = owner._refused_peer_uids(conn)
    assert peer_uid == chosen_uid
    assert peer_uid != DARWIN_CR_VERSION
    assert own_uid == os.getuid()
    assert conn.calls == [(eo.SOL_LOCAL, socket.LOCAL_PEERCRED, eo.DARWIN_PEERCRED_STRUCT_SIZE)]


def test_darwin_arm_uid_reaches_the_refusal_log_line(engram_home, darwin_arm, warn_log, clock):
    owner = _bare_owner(engram_home)
    owner._log_peer_refusal(_DarwinPeerConn(uid=FOREIGN_UID), {"reason": "peer_identity"})
    lines = _refusal_lines(warn_log)
    assert len(lines) == 1
    assert f"uid {FOREIGN_UID} (owner uid {os.getuid()})" in lines[0].getMessage()


@pytest.mark.parametrize(
    "conn",
    [
        pytest.param(_DarwinPeerConn(error=OSError("getsockopt failed (test)")), id="oserror"),
        pytest.param(_DarwinPeerConn(raw=b"\x00" * 8), id="short-buffer"),
    ],
)
def test_darwin_arm_failed_read_returns_none_and_does_not_raise(engram_home, darwin_arm, conn):
    owner = _bare_owner(engram_home)
    assert owner._refused_peer_uids(conn) == (None, os.getuid())


# ---------------------------------------------------------------------------
# Review round 2 on #3857: pin the drain loop, the cap, the log-before-send
# order, and the log when no deadline is left.
# ---------------------------------------------------------------------------


@requires_linux_kernel
def test_a_request_larger_than_one_read_is_drained_to_eof(engram_home):
    # No other cell needs more than one recv: a drain that reads once and
    # returns would reset a read-to-EOF client whose request exceeds one read.
    owner = _bare_owner(engram_home, request_deadline=5.0, max_request_bytes=10**6)
    reply, conn = _refuse(owner, request=b"z" * 70000, half_close=True)
    assert reply is not None and reply["reason"] == "peer_identity"
    assert conn.recv_bytes == 70000, f"drained {conn.recv_bytes} of 70000"


@requires_linux_owner
@requires_linux_kernel
def test_without_the_cap_the_same_flood_exceeds_the_thread_bound(long_owner, foreign_view, monkeypatch):
    # Pins the instrument of the flood cell above: MAX_REFUSAL_DRAINS is what
    # keeps the delta under FLOOD_THREAD_BOUND. Same N, same measured quantity.
    monkeypatch.setattr(eo, "MAX_REFUSAL_DRAINS", 10_000)
    holders = []
    before = threading.active_count()
    try:
        _open_silent(long_owner, FLOOD, holders)
        delta = threading.active_count() - before
    finally:
        _close_all(holders)
    assert delta > FLOOD_THREAD_BOUND, (
        f"only {delta} extra threads for {FLOOD} silent connections with the cap lifted: "
        "the flood bound is not produced by MAX_REFUSAL_DRAINS"
    )


class _OrderHandler(logging.Handler):
    def __init__(self, events):
        super().__init__(level=logging.WARNING)
        self.events = events

    def emit(self, record):
        if "peer refused" in record.getMessage():
            self.events.append("log")


@requires_linux_kernel
def test_the_refusal_is_logged_before_the_rejection_is_sent(engram_home, monkeypatch):
    # A slow log handler would delay the peer's rejection if the order flipped.
    events = []

    def sendall(self, data, *a):
        events.append("send")
        return self._sock.sendall(data, *a)

    monkeypatch.setattr(_PeerConn, "sendall", sendall, raising=False)
    handler = _OrderHandler(events)
    logger = logging.getLogger("engramd_owner")
    logger.addHandler(handler)
    try:
        owner = _bare_owner(engram_home)
        reply, _ = _refuse(owner)
    finally:
        logger.removeHandler(handler)
    assert reply is not None and reply["reason"] == "peer_identity"
    assert events == ["log", "send"], events


@requires_linux_kernel
def test_a_refusal_is_logged_even_when_no_deadline_is_left_and_nothing_is_sent(engram_home, warn_log):
    # request_deadline 0 -> remaining <= 0 at the send guard: the old behaviour
    # (no send, no drain) is kept, but the log call sits before that guard.
    owner = _bare_owner(engram_home, request_deadline=0)
    srv, cli = socket.socketpair()
    try:
        owner._handle_connection(_PeerConn(srv, FOREIGN_UID))
        srv.close()
        cli.settimeout(2.0)
        assert cli.recv(65536) == b"", "a rejection was sent with no deadline left"
    finally:
        srv.close()
        cli.close()
    lines = _refusal_lines(warn_log)
    assert len(lines) == 1, [r.getMessage() for r in warn_log.records]
    assert f"uid {FOREIGN_UID}" in lines[0].getMessage()


@requires_linux_kernel
def test_a_request_of_several_full_reads_is_drained_to_eof(engram_home):
    # 300000 bytes is four full 65536-byte reads plus a tail: a drain that gives
    # up after one, two, or a fixed number of bytes still leaves data unread.
    # It exceeds a socketpair's send buffer (~212992), so `_refuse`'s synchronous
    # send would block: a thread sends while the owner drains.
    size = 300000
    owner = _bare_owner(engram_home, request_deadline=5.0, max_request_bytes=10**6)
    srv, cli = socket.socketpair()
    conn = _PeerConn(srv, FOREIGN_UID)

    def send_then_half_close():
        try:
            cli.sendall(b"z" * size)
            cli.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    sender = threading.Thread(target=send_then_half_close, daemon=True)
    sender.start()
    try:
        owner._handle_connection(conn)
        sender.join(timeout=3)
        cli.settimeout(2.0)
        reply = _read_rejection(cli)
    finally:
        cli.close()
        srv.close()
    assert reply["reason"] == "peer_identity"
    assert conn.recv_bytes == size, f"drained {conn.recv_bytes} of {size}"
