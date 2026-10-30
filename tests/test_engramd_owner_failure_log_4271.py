"""Contract tests for the owner's failed-request log (#4271, part of #2652).

Before this change `EngramdOwner._handle_connection` logged only an unexpected
exception; every other failed request (unread request, malformed JSON, a deadline
that ran out before the send, a send to a client that had already timed out, a
`rejected` reply) returned silently, so a client-side "reason: timeout" left the
owner log with nothing to say.

The contract (tests written before the change):

  C1  every request that does NOT end in a delivered, non-rejected reply emits
      exactly ONE WARNING on logger "engramd_owner" (the unexpected-exception path
      keeps its single .error() and gets no WARNING on top).
  C2  the record names route (the method, or `<unparsed>`), cause (a closed set)
      and an integer elapsed ms against the integer deadline ms.
  C3  never request params, SQL, args, row data, request_id or a response body.
  C4  never logged: a delivered non-rejected reply; a connection that closed
      having sent zero bytes.
  C5  rate-limited per (route, cause): 1 record per 60 s window, the first record
      after it carries "N suppressed in the previous window"; at most 64 keys.
  C6  a raising logging.Filter changes nothing on the wire.

Drives the REAL `_handle_connection` over a socketpair (the neighbour pattern in
test_engramd_owner_peer_refusal_3845.py). Linux kernel only (SO_PEERCRED).
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import sys
import tempfile
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

SENTINEL = "SENTINEL-SECRET-SQL-4271"
REQUEST_ID = "rid-SENTINEL-4271"


@pytest.fixture
def owner():
    d = tempfile.mkdtemp(prefix="edo-")
    seed_current_store(d)
    o = eo.EngramdOwner(Path(d), request_deadline=5.0)
    o.start(install_signal_handlers=False)
    try:
        yield o
    finally:
        o.stop()
        shutil.rmtree(d, ignore_errors=True)


class _Clock:
    now = 1000.0


class _TimeView:
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


class _Conn:
    """A proxy over the server end of a socketpair; `send_error` makes sendall raise."""

    def __init__(self, sock, send_error=None):
        self._sock = sock
        self._send_error = send_error

    def sendall(self, data):
        if self._send_error is not None:
            raise self._send_error
        return self._sock.sendall(data)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _req(method="ping", params=None, **extra) -> bytes:
    body = {"contract_version": ec.CONTRACT_VERSION, "method": method,
            "params": params if params is not None else {}, "request_id": None}
    body.update(extra)
    return (json.dumps(body) + "\n").encode()


def _drive(owner, request: bytes = b"", *, half_close=True, send_error=None, conn_factory=None):
    """Run one request through `_handle_connection`; return the raw bytes the client got.
    `conn_factory(srv)` (round 3) swaps in a different server-side proxy."""
    srv, cli = socket.socketpair()
    try:
        if request:
            cli.sendall(request)
        if half_close:
            cli.shutdown(socket.SHUT_WR)
        owner._handle_connection(conn_factory(srv) if conn_factory else _Conn(srv, send_error))
        cli.settimeout(2.0)
        data = b""
        while True:
            try:
                chunk = cli.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        return data
    finally:
        cli.close()
        srv.close()


@pytest.fixture
def warn_log(caplog):
    caplog.set_level(logging.WARNING, logger="engramd_owner")
    return caplog


def _failures(caplog):
    return [r for r in caplog.records
            if r.name == "engramd_owner" and r.getMessage().startswith("request failed")]


def _text(rec) -> str:
    return rec.getMessage() + " " + repr(rec.args) + " " + repr(rec.msg)


# -- T1..T4: the four failure causes ---------------------------------------------


@requires_linux_owner
@requires_linux_kernel
def test_t1_send_failure_logs_one_warning_with_route_cause_and_int_elapsed(owner, warn_log):
    _drive(owner, _req("ping"), send_error=BrokenPipeError())
    recs = _failures(warn_log)
    assert len(recs) == 1, [r.getMessage() for r in warn_log.records]
    assert recs[0].levelno == logging.WARNING
    msg = recs[0].getMessage()
    assert "route ping" in msg
    assert "cause send_failed:BrokenPipeError" in msg
    import re
    m = re.search(r"elapsed (\d+) ms of a (\d+) ms deadline", msg)
    assert m, msg
    assert int(m.group(2)) == 5000


@requires_linux_owner
@requires_linux_kernel
def test_t2_deadline_passed_before_send(owner, warn_log, clock):
    def slow_dispatch(request, deadline):
        _Clock.now += 100.0  # the handler "took" far longer than the 5 s deadline
        return {"status": "ok", "result": {}}

    owner._dispatch = slow_dispatch
    data = _drive(owner, _req("ping"))
    assert data == b"", "the response must still be dropped on this path (wire unchanged)"
    recs = _failures(warn_log)
    assert len(recs) == 1
    assert "cause deadline_before_send" in recs[0].getMessage()
    assert "route ping" in recs[0].getMessage()


@requires_linux_owner
@requires_linux_kernel
def test_t3_rejected_reply_logs_the_sanitized_reason(owner, warn_log):
    data = _drive(owner, _req("read", {"sql": 5, "args": []}))
    wire = json.loads(data.decode())
    assert wire["status"] == "rejected"
    recs = _failures(warn_log)
    assert len(recs) == 1
    assert f"cause rejected:{wire['reason']}" in recs[0].getMessage()
    assert "route read" in recs[0].getMessage()


@requires_linux_owner
@requires_linux_kernel
def test_t3b_reason_is_sanitized_and_cut_to_40_chars(owner, warn_log):
    owner._dispatch = lambda request, deadline: {
        "status": "rejected", "reason": "bad\nreason\x00" + "x" * 80, "detail": "d", "remedy": "r"}
    _drive(owner, _req("ping"))
    msg = _failures(warn_log)[0].getMessage()
    assert "\n" not in msg and "\x00" not in msg
    cause = msg.split("cause ", 1)[1].split(", elapsed", 1)[0]
    assert cause.startswith("rejected:bad reason ")
    assert len(cause) == len("rejected:") + 40


@requires_linux_owner
@requires_linux_kernel
def test_t4_malformed_json_is_unparsed_route(owner, warn_log):
    _drive(owner, b"{not json at all\n")
    recs = _failures(warn_log)
    assert len(recs) == 1
    msg = recs[0].getMessage()
    assert "route <unparsed>" in msg and "cause malformed_request" in msg


@requires_linux_owner
@requires_linux_kernel
def test_t4b_json_that_is_not_a_dict_is_malformed(owner, warn_log):
    _drive(owner, b"[1, 2]\n")
    assert "cause malformed_request" in _failures(warn_log)[0].getMessage()


@requires_linux_owner
@requires_linux_kernel
def test_t4c_partial_line_then_eof_is_request_unread(owner, warn_log):
    _drive(owner, b'{"method": "pi')  # bytes arrived, no newline, then EOF
    recs = _failures(warn_log)
    assert len(recs) == 1
    assert "route <unparsed>" in recs[0].getMessage()
    assert "cause request_unread" in recs[0].getMessage()


@requires_linux_owner
@requires_linux_kernel
def test_t4d_silent_peer_until_the_deadline_is_request_unread(owner, warn_log):
    owner.request_deadline = 0.2
    _drive(owner, b"", half_close=False)  # connected, sent nothing, never closed
    recs = _failures(warn_log)
    assert len(recs) == 1
    assert "cause request_unread" in recs[0].getMessage()


# -- T5, T6: never logged ---------------------------------------------------------


@requires_linux_owner
@requires_linux_kernel
def test_t5_zero_byte_connect_and_close_is_not_logged(owner, warn_log):
    _drive(owner, b"", half_close=True)
    assert _failures(warn_log) == []


@requires_linux_owner
@requires_linux_kernel
def test_t6_a_successful_request_logs_nothing(owner, warn_log):
    warn_log.set_level(logging.DEBUG, logger="engramd_owner")
    data = _drive(owner, _req("ping"))
    assert json.loads(data.decode())["status"] == "ok"
    assert [r for r in warn_log.records if r.name == "engramd_owner"] == []


@requires_linux_owner
@requires_linux_kernel
def test_unexpected_exception_keeps_its_one_error_and_gets_no_warning(owner, warn_log):
    def boom(request, deadline):
        raise RuntimeError("boom")

    owner._dispatch = boom
    data = _drive(owner, _req("ping"))
    assert json.loads(data.decode())["reason"] == "internal_error"
    recs = [r for r in warn_log.records if r.name == "engramd_owner"]
    assert [r.levelno for r in recs] == [logging.ERROR], [r.getMessage() for r in recs]


# -- T7: rate limit ---------------------------------------------------------------


@requires_linux_owner
@requires_linux_kernel
def test_t7_rate_limit_one_per_window_then_carries_the_suppressed_count(owner, warn_log, clock):
    k = 5
    for _ in range(k):
        _drive(owner, b"{bad\n")
    assert len(_failures(warn_log)) == 1
    _Clock.now += eo.REQUEST_FAILURE_LOG_WINDOW + 1
    _drive(owner, b"{bad\n")
    recs = _failures(warn_log)
    assert len(recs) == 2
    assert f"{k - 1} suppressed in the previous window" in recs[1].getMessage()
    assert "suppressed" not in recs[0].getMessage()


@requires_linux_owner
@requires_linux_kernel
def test_t7b_the_key_is_route_and_cause_not_cause_alone(owner, warn_log, clock):
    _drive(owner, b"{bad\n")
    _drive(owner, _req("ping"), send_error=BrokenPipeError())
    _drive(owner, _req("ping"), send_error=ConnectionResetError())
    assert len(_failures(warn_log)) == 3


@requires_linux_owner
@requires_linux_kernel
def test_t7c_the_failure_table_is_bounded_and_evicts_the_oldest(owner, warn_log, clock):
    cap = eo.REQUEST_FAILURE_LOG_MAX_KEYS
    assert cap == 64 and eo.REQUEST_FAILURE_LOG_WINDOW == 60
    for i in range(cap + 3):
        owner._log_request_failure(f"m{i}", "malformed_request", _Clock.now, _Clock.now + 5.0)
    assert len(owner._request_failure_log) == cap
    assert ("m0", "malformed_request") not in owner._request_failure_log
    assert (f"m{cap + 2}", "malformed_request") in owner._request_failure_log


def test_the_peer_refusal_and_request_failure_logs_share_one_limiter_class():
    assert eo.EngramdOwner.__init__.__globals__["_KeyedRateLimiter"] is eo._KeyedRateLimiter
    o = eo.EngramdOwner.__new__(eo.EngramdOwner)  # no start; only the limiters matter
    eo.EngramdOwner.__init__(o, Path(tempfile.mkdtemp(prefix="edo-")))
    assert type(o._peer_refusal_limiter) is type(o._request_failure_limiter) is eo._KeyedRateLimiter
    assert o._peer_refusal_limiter is not o._request_failure_limiter


# -- T8: privacy ------------------------------------------------------------------


@requires_linux_owner
@requires_linux_kernel
@pytest.mark.parametrize("send_error", [None, BrokenPipeError()])
def test_t8_no_sql_args_or_request_id_ever_reaches_a_record(owner, warn_log, send_error):
    sql = f"SELECT '{SENTINEL}' FROM nodes WHERE id = ?"
    _drive(owner, _req("read", {"sql": sql, "args": [SENTINEL]}, request_id=REQUEST_ID),
           send_error=send_error)
    recs = _failures(warn_log)
    assert len(recs) == 1, [r.getMessage() for r in warn_log.records]
    for rec in warn_log.records:
        blob = _text(rec)
        assert SENTINEL not in blob and REQUEST_ID not in blob
    assert "route read" in recs[0].getMessage()


@requires_linux_owner
@requires_linux_kernel
def test_t8b_a_hostile_method_name_cannot_inject_into_the_route(owner, warn_log):
    _drive(owner, _req("bad\nmethod " + "y" * 500))
    msg = _failures(warn_log)[0].getMessage()
    assert "\n" not in msg
    assert len(msg) < 400


# -- T9: a raising logging.Filter changes nothing on the wire ----------------------


class _RaisingFilter(logging.Filter):
    def filter(self, record):
        raise RuntimeError("filter blew up")


@requires_linux_owner
@requires_linux_kernel
@pytest.mark.parametrize("request_bytes", [
    _req("read", {"sql": 5, "args": []}),  # a rejected reply
    _req("ping"),                          # an ok reply (nothing logged)
])
def test_t9_a_raising_filter_leaves_the_client_bytes_identical(owner, warn_log, request_bytes):
    baseline = _drive(owner, request_bytes)
    logger = logging.getLogger("engramd_owner")
    flt = _RaisingFilter()
    logger.addFilter(flt)
    try:
        with_filter = _drive(owner, request_bytes)
        # the other failure paths also complete (no raise) under a raising filter
        _drive(owner, b"{bad\n")
        _drive(owner, _req("ping"), send_error=BrokenPipeError())
    finally:
        logger.removeFilter(flt)
    assert baseline and with_filter == baseline


@requires_linux_owner
@requires_linux_kernel
def test_t9b_a_raising_log_call_does_not_stop_the_connection_closing(owner, monkeypatch):
    closed = []

    class _C(_Conn):
        def close(self):
            closed.append(True)
            self._sock.close()

    def raising(*a, **k):
        raise RuntimeError("log layer down")

    monkeypatch.setattr(owner, "_log_request_failure", raising)
    srv, cli = socket.socketpair()
    try:
        cli.sendall(b"{bad\n")
        cli.shutdown(socket.SHUT_WR)
        owner._handle_connection(_C(srv))
    finally:
        cli.close()
    assert closed == [True]


# -- Round 3 (F1, F3, F4): the exits the first round left unpinned ----------------------------


def _owner_records(caplog):
    return [r for r in caplog.records if r.name == "engramd_owner"]


def _boom(request, deadline):
    raise RuntimeError("boom")


@requires_linux_owner
@requires_linux_kernel
def test_f1a_unexpected_exception_then_failed_send_logs_exactly_one_record(owner, warn_log):
    """Mutation: `if not already_logged:` -> `if True:` at the send-failure site -> a second
    (WARNING send_failed) record -> red."""
    owner._dispatch = _boom
    _drive(owner, _req("ping"), send_error=BrokenPipeError())
    recs = _owner_records(warn_log)
    assert [r.levelno for r in recs] == [logging.ERROR], [r.getMessage() for r in recs]
    assert not _failures(warn_log)


@requires_linux_owner
@requires_linux_kernel
def test_f1b_unexpected_exception_then_expired_deadline_logs_exactly_one_record(owner, warn_log, clock):
    """Mutation: `if not already_logged:` -> `if True:` at the deadline_before_send site -> a second
    (WARNING deadline_before_send) record -> red."""
    def slow_boom(request, deadline):
        _Clock.now += 100.0  # past the 5 s deadline, then it raises
        raise RuntimeError("boom")

    owner._dispatch = slow_boom
    data = _drive(owner, _req("ping"))
    assert data == b"", "the reply is still dropped on this path (wire unchanged)"
    recs = _owner_records(warn_log)
    assert [r.levelno for r in recs] == [logging.ERROR], [r.getMessage() for r in recs]
    assert not _failures(warn_log)


@requires_linux_owner
@requires_linux_kernel
def test_f3_an_exception_outside_dispatch_logs_exactly_one_handler_error_record(owner, warn_log):
    """Deeply nested JSON: json.loads raises RecursionError, which the (JSONDecodeError,
    UnicodeDecodeError) clause does not catch. Round 4: the guard now SWALLOWS it (no exception reaches
    the caller) and leaves ONE ERROR record, cause handler_error:RecursionError. Mutation: delete the
    outer guard -> zero records (and the exception escapes) -> red."""
    depth = 100000
    _drive(owner, b"[" * depth + b"]" * depth + b"\n")  # does not raise
    recs = _owner_records(warn_log)
    assert len(recs) == 1, [r.getMessage() for r in recs]
    assert recs[0].levelno == logging.ERROR
    msg = recs[0].getMessage()
    assert "cause handler_error:RecursionError" in msg and "route <unparsed>" in msg, msg


class _SettimeoutRaisesAfter(_Conn):
    """settimeout raises once `armed` is set (the dispatch stub arms it), i.e. AFTER a record exists."""
    armed = False

    def settimeout(self, value):
        if _SettimeoutRaisesAfter.armed:
            raise RuntimeError("settimeout died")
        return self._sock.settimeout(value)


@requires_linux_owner
@requires_linux_kernel
def test_f3b_an_exception_after_a_record_exists_is_a_labelled_secondary_record(owner, warn_log):
    """The dispatch raises (its one .error() is the record), then conn.settimeout dies outside
    _dispatch. Round 4: the guard swallows it and logs one labelled secondary ERROR (see T-G2)."""
    _SettimeoutRaisesAfter.armed = False

    def boom_and_arm(request, deadline):
        _SettimeoutRaisesAfter.armed = True
        raise RuntimeError("boom")

    owner._dispatch = boom_and_arm
    try:
        _drive(owner, _req("ping"), conn_factory=lambda srv: _SettimeoutRaisesAfter(srv))  # does not raise
    finally:
        _SettimeoutRaisesAfter.armed = False
    recs = _owner_records(warn_log)
    assert [r.levelno for r in recs] == [logging.ERROR, logging.ERROR], [r.getMessage() for r in recs]
    assert recs[1].getMessage().startswith("secondary exception after this request's failure record")


class _CloseRaises(_Conn):
    def close(self):
        self._sock.close()
        raise RuntimeError("close died")


@requires_linux_owner
@requires_linux_kernel
def test_f3c_an_exception_after_a_fail_record_adds_no_second_record(owner, warn_log):
    """A send failure records via fail(); conn.close() then raises. The failure of the CLOSE is not a
    second request failure: still exactly one record (cause send_failed)."""
    with pytest.raises(RuntimeError, match="close died"):
        _drive(owner, _req("ping"),
               conn_factory=lambda srv: _CloseRaises(srv, BrokenPipeError()))
    recs = _owner_records(warn_log)
    assert len(recs) == 1 and "cause send_failed:BrokenPipeError" in recs[0].getMessage(), \
        [r.getMessage() for r in recs]


@requires_linux_owner
@requires_linux_kernel
def test_f4_the_unexpected_exception_error_logs_the_sanitized_route_and_is_rate_limited(
        owner, warn_log, clock):
    """The client-controlled method reaches the ERROR line only as the sanitized route (control
    characters out, at most 40 chars), and a second identical failure inside the window logs nothing.
    Mutation: log the raw `request.get("method")` -> newline and 200 chars in the record -> red;
    mutation: skip the limiter -> a second record -> red."""
    owner._dispatch = _boom
    method = "bad\nmethod " + "y" * 200
    _drive(owner, _req(method))
    recs = _owner_records(warn_log)
    assert len(recs) == 1 and recs[0].levelno == logging.ERROR
    msg = recs[0].getMessage()
    assert msg.startswith("unexpected exception in method bad method yyyy"), msg
    # the route stays bounded at 40; the suffix is the owner's own class name (RuntimeError, from _boom), never client input
    suffix = f" ({RuntimeError.__name__})"
    assert "\n" not in msg and len(msg) <= len("unexpected exception in method ") + 40 + len(suffix), msg
    assert msg.endswith(suffix), msg
    assert recs[0].exc_info is not None, "the traceback is kept"
    _drive(owner, _req(method))
    assert len(_owner_records(warn_log)) == 1, "a second identical failure in the window is suppressed"
    _Clock.now += 61.0
    _drive(owner, _req(method))
    recs = _owner_records(warn_log)
    assert len(recs) == 2 and recs[1].getMessage().endswith("; 1 suppressed in the previous window")


@requires_linux_owner
@requires_linux_kernel
def test_r5_unexpected_exception_is_rate_limited_per_exception_class(owner, warn_log, clock):
    """Luria's L1: the key is (route, "unexpected_exception:<Class>"), so a different bug on the same
    route inside the window is not suppressed by the first. ValueError, KeyError, ValueError in one
    window -> exactly 2 ERROR records (each with a traceback, each naming its class); after the window
    the next ValueError carries the suppressed count.
    Mutation: drop the class from the key -> the KeyError is suppressed -> red."""
    state = {"cls": ValueError}

    def _raise(request, deadline):
        raise state["cls"]("x")

    owner._dispatch = _raise
    _drive(owner, _req("ping"))
    state["cls"] = KeyError
    _drive(owner, _req("ping"))
    state["cls"] = ValueError
    _drive(owner, _req("ping"))
    recs = _owner_records(warn_log)
    assert len(recs) == 2, [r.getMessage() for r in recs]
    assert all(r.levelno == logging.ERROR and r.exc_info is not None for r in recs)
    assert recs[0].getMessage() == "unexpected exception in method ping (ValueError)"
    assert recs[1].getMessage() == "unexpected exception in method ping (KeyError)"
    _Clock.now += 61.0
    _drive(owner, _req("ping"))
    recs = _owner_records(warn_log)
    assert len(recs) == 3
    assert recs[2].getMessage() == (
        "unexpected exception in method ping (ValueError); 1 suppressed in the previous window")


# -- Round 4: the outer guard is the single record, not a second one -------------------------


def _drive_worker(owner, monkeypatch, request: bytes, conn_factory=None):
    """Run ONE connection through `_worker` (the real caller of `_handle_connection`); the loop ends
    because `_next_connection` returns None. Returns nothing: the point is what got logged."""
    monkeypatch.setattr(owner, "_next_connection", lambda: None)
    srv, cli = socket.socketpair()
    try:
        cli.sendall(request)
        cli.shutdown(socket.SHUT_WR)
        owner._worker(conn_factory(srv) if conn_factory else _Conn(srv))
    finally:
        cli.close()
        srv.close()


@requires_linux_owner
@requires_linux_kernel
def test_tg1_a_handler_error_through_the_worker_is_one_error_record_with_a_traceback(
        owner, warn_log, monkeypatch):
    """Deeply nested JSON through _worker: exactly ONE record in total, ERROR, cause
    handler_error:RecursionError, a traceback, and no worker 'unexpected exception in a connection
    handler' record. Mutations: put `raise` back -> the worker's ERROR appears -> red; log without
    exc_info -> exc_info is None -> red."""
    depth = 100000
    _drive_worker(owner, monkeypatch, b"[" * depth + b"]" * depth + b"\n")
    recs = _owner_records(warn_log)
    assert len(recs) == 1, [r.getMessage() for r in recs]
    rec = recs[0]
    assert rec.levelno == logging.ERROR
    assert "cause handler_error:RecursionError" in rec.getMessage()
    assert rec.exc_info is not None, "the traceback survives"
    assert not any("connection handler" in r.getMessage() for r in recs)


@requires_linux_owner
@requires_linux_kernel
def test_tg2_an_exception_after_a_record_exists_is_one_secondary_error_not_silence(
        owner, warn_log, monkeypatch):
    """The unexpected-exception ERROR is logged, then settimeout raises: exactly two records, the first
    failure record then the 'secondary exception' ERROR with a traceback; no worker ERROR. Mutation: make
    the secondary branch silent (`pass`) -> one record -> red."""
    _SettimeoutRaisesAfter.armed = False

    def boom_and_arm(request, deadline):
        _SettimeoutRaisesAfter.armed = True
        raise RuntimeError("boom")

    owner._dispatch = boom_and_arm
    try:
        _drive_worker(owner, monkeypatch, _req("ping"),
                      conn_factory=lambda srv: _SettimeoutRaisesAfter(srv))
    finally:
        _SettimeoutRaisesAfter.armed = False
    recs = _owner_records(warn_log)
    assert len(recs) == 2, [r.getMessage() for r in recs]
    assert recs[0].getMessage().startswith("unexpected exception in method ping")
    assert recs[1].levelno == logging.ERROR
    assert recs[1].getMessage() == ("secondary exception after this request's failure record: "
                                    "route ping, cause handler_error:RuntimeError")
    assert recs[1].exc_info is not None
    assert not any("connection handler" in r.getMessage() for r in recs)


@requires_linux_owner
@requires_linux_kernel
def test_tg1b_identical_handler_errors_inside_the_window_log_once(owner, warn_log, monkeypatch, clock):
    """Two identical handler errors inside the window give one record; after the window the next one
    carries the suppressed count. Mutation: skip the limiter -> two records -> red."""
    depth = 100000
    body = b"[" * depth + b"]" * depth + b"\n"
    _drive_worker(owner, monkeypatch, body)
    _drive_worker(owner, monkeypatch, body)
    assert len(_owner_records(warn_log)) == 1
    _Clock.now += 61.0
    _drive_worker(owner, monkeypatch, body)
    recs = _owner_records(warn_log)
    assert len(recs) == 2 and recs[1].getMessage().endswith("; 1 suppressed in the previous window")
