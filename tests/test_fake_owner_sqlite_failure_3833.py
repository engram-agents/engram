"""The FakeOwner answers a store failure as the real owner does, and does not die in its
handler thread on a store failure or an exception while answering (#3833, part of #2652).

Before this, a sqlite failure while the fake served `read`, `df` or `chain` (a garbled store:
`DatabaseError: file is not a database`; a missing table; a missing store file:
`OperationalError: unable to open database file`) was not handled in `read`/`df`, and the
`chain` guard sat after its `sqlite3.connect`. The exception killed the daemon handler thread:

  1. the client saw a closed connection (Unavailable), while the real owner answers a typed
     `read_failed` rejection -- so a `[fake-owner]` cell exercised a different client path
     than its `[real-owner]` twin; and
  2. pytest reported a PytestUnhandledThreadExceptionWarning, the signal engram-alpha #3784
     item (b) watches in the nightly log.

Contract (written BEFORE the fixture change):

  C1  any sqlite3.Error while serving read / df / chain (at connect, at execute, a missing
      store file) is answered as the real owner's `read_failed`:
      {"status": "rejected", "reason": "read_failed", "detail": "<Type>: <msg>"[:200],
       "remedy": <the real owner's string>}.
  C2  `_handle` answers like the real owner, and parsing is separated from answering as in the
      real owner: a request that is not UTF-8, not JSON, or not a JSON object (an empty line,
      `null`, a string, a number, `true` included) is DROPPED -- the connection closes with NO
      answer, the request is not recorded. An `Exception` while ANSWERING (an `OSError` too: the
      real owner's catch-all wraps all of its dispatch) is the real owner's `internal_error`
      shape; a failure of the transport write itself (`sendall`) ends with the connection
      closed and NO answer. A `BaseException` that is not an `Exception` is NOT converted (the
      real owner catches `Exception`, not `BaseException`). The connection is closed in every
      case. Like the real owner, a pathological request still kills the thread: a
      `RecursionError` from deeply nested JSON or an oversized-integer `ValueError` is outside
      the parse `try` (it catches only JSONDecodeError / UnicodeDecodeError), here and there.
  C3  a threading.excepthook around each request records ZERO exceptions. The exchange is
      deterministic: the cell reads the answer to EOF, then JOINS the handler threads the fake
      started (`FakeOwner.join_handlers`), then reads the hook list -- no sleep.
  C4  the fake's `read_failed` rejection EQUALS the real owner's `_read_failed(exc)` output;
      the literals are pinned here against drift. This file needs `engramd_owner` importable
      at collection, like its siblings (the helpers it imports from test_engramd_chain_3740
      import it unguarded); the `importorskip` calls below are harmless extra guards, not a
      tested skip path.

Mutations (each reddens at least one cell here; see the dispatch handoff).
"""
from __future__ import annotations

import inspect
import json
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402
import engramd_client as ec  # noqa: E402

import _fake_owner  # noqa: E402
from _fake_owner import FakeOwner  # noqa: E402
from test_engramd_chain_3740 import bare_store, chain_store  # noqa: E402

# Literal copies of the real owner's strings, on purpose: the fake holds its own literals and
# test_c4_* pins both against production (engramd_owner._read_failed and the dispatch catch-all).
READ_FAILED_REMEDY = (
    "the statement was admitted but could not complete (an over-limit value, a locked or "
    "damaged store); see the owner log"
)
INTERNAL_ERROR_REMEDY = (
    "owner bug (an unexpected exception inside the owner, not an outage): see the owner log"
)

READ_SQL = "SELECT count(*) FROM nodes"
assert READ_SQL in _fake_owner.registered_sql()


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="fo3833-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# --- the stores ------------------------------------------------------------------------------

def garbled_store(home: Path) -> None:
    """Bytes that are not a SQLite file: connect() succeeds, the first execute raises
    DatabaseError 'file is not a database'."""
    (home / "knowledge.db").write_bytes(b"this is not a sqlite database file\n" * 200)


def vocab_store(home: Path) -> None:
    """A healthy store for the `df` op: an fts5 table and its fts5vocab 'row' table."""
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("CREATE VIRTUAL TABLE nodes_fts USING fts5(claim)")
    conn.execute("INSERT INTO nodes_fts(claim) VALUES ('formula one formula two')")
    conn.execute("CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab(nodes_fts, row)")
    conn.commit()
    conn.close()


def dangling_vocab_store(home: Path) -> None:
    """The vocab table exists (so the DDL probe passes) but its fts5 table does not: the
    sqlite_master read succeeds and the IDF statement -- the SECOND execute -- raises
    'no such fts5 table'."""
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab(nodes_fts, row)")
    conn.commit()
    conn.close()


# --- the exchange: deterministic, sleep-free ----------------------------------------------------

def exchange(owner: FakeOwner, payload: bytes, *, timeout: float = 5.0):
    """Send raw bytes + newline, read to EOF, JOIN the handler threads, then read the hook.
    -> (answer dict or None when the connection closed with no data, [thread exceptions])."""
    records = []
    old = threading.excepthook
    threading.excepthook = records.append
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(str(owner.socket_path))
            s.sendall(payload + b"\n")
            buf = b""
            while True:  # EOF == the handler closed the connection (the `finally`)
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        # The excepthook of a dying thread can run AFTER the connection is closed: wait for the
        # handler to FINISH (Thread.join returns after _invoke_excepthook), then read the list.
        assert owner.join_handlers(timeout), "a handler thread did not finish"
    finally:
        threading.excepthook = old
    answer = json.loads(buf.decode("utf-8")) if buf else None
    return answer, records


def call(owner: FakeOwner, method: str, params):
    return exchange(owner, json.dumps({"contract_version": 1, "method": method,
                                       "params": params, "request_id": None}).encode("utf-8"))


def params_for(method: str):
    if method == "read":
        return {"sql": READ_SQL, "args": []}
    if method == "df":
        return {"terms": ["formula"]}
    return {"node_id": "ls_p", "max_depth": 2}


def assert_read_failed(answer, records, *, kind: str, fragment: str):
    assert records == [], f"the handler thread died: {records!r}"
    assert answer is not None, "no typed answer (the connection closed bare)"
    assert set(answer) == {"status", "reason", "detail", "remedy"}, answer
    assert answer["status"] == "rejected" and answer["reason"] == "read_failed", answer
    assert answer["detail"].startswith(f"{kind}: "), answer
    assert fragment in answer["detail"], answer
    assert len(answer["detail"]) <= 200
    assert answer["remedy"] == READ_FAILED_REMEDY


# --- C1 + C3: a store failure is a typed read_failed, the thread survives ----------------------

@pytest.mark.parametrize("method", ["read", "df", "chain"])
def test_garbled_store_is_a_read_failed_and_no_thread_exception(home, method):
    """C1 + C3 (the issue's deterministic source). Mutations: the method's branch left
    unguarded; a guard catching only OperationalError (a garbled store raises its PARENT,
    DatabaseError); wrong reason / remedy; detail not truncated -> red."""
    garbled_store(home)
    with FakeOwner(home) as o:
        answer, records = call(o, method, params_for(method))
    assert_read_failed(answer, records, kind="DatabaseError", fragment="file is not a database")


@pytest.mark.parametrize("method", ["read", "df", "chain"])
def test_missing_store_file_is_a_read_failed_and_no_thread_exception(home, method):
    """The failure is at sqlite3.connect (mode=ro on a file that is absent): the guard must
    cover the connect, not only the execute. Mutation: chain's connect left outside the guard.
    FAKE-ONLY behaviour: the real owner holds a persistent connection to the store, so it cannot
    be in a missing-file state mid-run; this cell pins the fake's own connect-per-request path."""
    assert not (home / "knowledge.db").exists()
    with FakeOwner(home) as o:
        answer, records = call(o, method, params_for(method))
    assert_read_failed(answer, records, kind="OperationalError", fragment="unable to open database file")


@pytest.mark.parametrize("method,make,fragment", [
    ("read", lambda h: bare_store(h), "no such table: nodes"),
    ("chain", lambda h: bare_store(h), "no such table"),
    ("df", dangling_vocab_store, "no such fts5 table"),
], ids=["read", "chain", "df-second-execute"])
def test_a_missing_table_is_a_read_failed_and_no_thread_exception(home, method, make, fragment):
    """A failure at a LATER point than connect / the first execute (the table is missing)."""
    make(home)
    with FakeOwner(home) as o:
        answer, records = call(o, method, params_for(method))
    assert_read_failed(answer, records, kind="OperationalError", fragment=fragment)


def test_a_sqlite_error_inside_the_chain_walk_is_a_read_failed_and_the_detail_is_cut_at_200(home, monkeypatch):
    """The walk itself raises (a failure point past connect and execute); a 500-char message is
    cut to 200 like the real owner's detail. Mutation: detail not truncated -> red."""
    chain_store(home)

    def boom(conn, node_id, depth):
        raise sqlite3.OperationalError("x" * 500)

    monkeypatch.setattr(engram_core, "generalization_chain", boom)
    with FakeOwner(home) as o:
        answer, records = call(o, "chain", params_for("chain"))
    assert records == []
    assert answer["reason"] == "read_failed" and answer["remedy"] == READ_FAILED_REMEDY, answer
    assert answer["detail"] == ("OperationalError: " + "x" * 500)[:200]
    assert len(answer["detail"]) == 200


def test_a_sqlite_error_class_other_than_operational_is_also_a_read_failed(home, monkeypatch):
    """The guard is `sqlite3.Error` (the real owner's), not OperationalError / DatabaseError
    alone: a DataError (an over-limit value) travels the same way."""
    chain_store(home)

    def boom(conn, node_id, depth):
        raise sqlite3.DataError("string or blob too big")

    monkeypatch.setattr(engram_core, "generalization_chain", boom)
    with FakeOwner(home) as o:
        answer, records = call(o, "chain", params_for("chain"))
    assert records == []
    assert answer["reason"] == "read_failed" and answer["detail"] == "DataError: string or blob too big", answer


# --- the client sees what a real owner would make it see --------------------------------------

@pytest.mark.parametrize("method", ["read", "df", "chain"])
def test_through_the_real_client_a_garbled_store_is_a_rejected_read_failed_not_unavailable(home, method):
    """The client path the [real-owner] twin takes: a Rejected(read_failed), not Unavailable."""
    garbled_store(home)
    with FakeOwner(home) as o:
        result = ec.read(method, params_for(method), timeout=5.0, socket_path=o.socket_path)
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "read_failed" and result.remedy == READ_FAILED_REMEDY, result


# --- positive control: a healthy store is not turned into read_failed -------------------------

def test_a_healthy_store_still_answers_ok_through_all_three_methods(home):
    """Over-tightening guard: a guard that answered read_failed for a healthy store would
    break every other [fake-owner] cell; pin it here, with zero thread exceptions."""
    chain_store(home)
    with FakeOwner(home) as o:
        read_answer, r1 = call(o, "read", params_for("read"))
        chain_answer, r2 = call(o, "chain", params_for("chain"))
    assert r1 == [] and r2 == []
    assert read_answer["status"] == "ok" and read_answer["result"][0][0] > 0, read_answer
    assert chain_answer["status"] == "ok" and chain_answer["result"]["start_id"] == "ls_p", chain_answer

    other = home / "df"
    other.mkdir()
    vocab_store(other)
    with FakeOwner(other) as o:
        df_answer, r3 = call(o, "df", params_for("df"))
    assert r3 == []
    assert df_answer == {"status": "ok", "result": [["formula", 1]]}, df_answer


def test_a_healthy_store_with_no_matching_rows_answers_ok_with_an_empty_result(home):
    """Over-tightening neighbour: an EMPTY result is a healthy answer, not a failure. Mutation:
    a guard that turns zero rows into read_failed -> red."""
    chain_store(home)
    with FakeOwner(home) as o:
        read_answer, r1 = call(o, "read", {"sql": "SELECT id, type FROM nodes WHERE id = ?", "args": ["no_such_id"]})
    assert r1 == []
    assert read_answer == {"status": "ok", "result": []}, read_answer

    other = home / "df"
    other.mkdir()
    vocab_store(other)
    with FakeOwner(other) as o:
        df_answer, r2 = call(o, "df", {"terms": ["absentterm"]})
    assert r2 == []
    assert df_answer == {"status": "ok", "result": []}, df_answer


# --- C2: _handle never lets an exception escape the thread --------------------------------------

@pytest.mark.parametrize("payload", [
    b"this is not json",
    b"\xff\xfe\x00 not utf-8",
    b"[1, 2, 3]",  # valid JSON, not an object
    b"null",
    b'"a string"',
    b"42",
    b"true",
    b"",  # an empty line (the helper adds the newline): real owner json.loads("") -> drop
], ids=["malformed-json", "non-utf8", "json-not-an-object", "json-null", "json-string",
        "json-number", "json-true", "empty-line"])
def test_a_malformed_request_is_dropped_silently_like_the_real_owner(home, payload):
    """The real owner drops an undecodable / non-JSON / non-object request before dispatch
    (engramd_owner: `return`, 'a U1 client sees Unavailable, not a hang'); only an UNEXPECTED
    exception while ANSWERING is an `internal_error`. So: NO answer (EOF), zero thread
    exceptions, the handler finished, and the request is NOT recorded (the real owner returns
    before dispatch). Mutations: typed internal_error restored for parse failures; parse errors
    escaping the thread; the request recorded anyway; the non-dict check removed, or narrowed to
    lists only (null / a string / a number / true then reach `_answer`) -> red."""
    with FakeOwner(home) as o:
        answer, records = exchange(o, payload)
        assert o.join_handlers(5.0), "the handler thread did not finish"
        recorded = list(o.requests)
    assert records == [], f"the handler thread died: {records!r}"
    assert answer is None, f"a malformed request got an answer: {answer!r}"
    assert recorded == [], f"a dropped request was recorded: {recorded!r}"


class _SendFails:
    """The accepted socket with a `sendall` that fails like a broken transport (the real
    owner's `sendall` is the one place an OSError means 'no answer'). Everything else
    delegates, so recv / settimeout / close are the real ones."""

    def __init__(self, conn):
        self._conn = conn

    def sendall(self, data):
        raise OSError("simulated broken pipe")

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _fail_sendall(monkeypatch, owner, seen=None):
    """Make every handler the fake starts write through a failing `sendall` (`_serve` reads
    self._handle when it starts a thread). `seen` collects the REAL accepted sockets."""
    real_handle = type(owner)._handle

    def handle(conn):
        if seen is not None:
            seen.append(conn)
        real_handle(owner, _SendFails(conn))

    monkeypatch.setattr(owner, "_handle", handle)


def test_through_the_real_client_a_no_answer_exchange_reads_as_unavailable(home, monkeypatch):
    """The real client cannot SEND a malformed request (it builds its own JSON), so the dropped
    request itself is pinned on the raw wire above; here is the client half: a connection the
    fake closes with no answer (the transport write failing: same wire outcome as a dropped
    request) reads as Unavailable, not Rejected(internal_error). An OSError while ANSWERING is
    NOT this case any more (it is an internal_error, like the real owner's). Mutation: the
    outer `except OSError` removed, or the failed write answered as a typed rejection -> red."""
    with FakeOwner(home) as o:
        _fail_sendall(monkeypatch, o)
        result = ec.read("ping", None, timeout=5.0, socket_path=o.socket_path)
    assert isinstance(result, ec.Unavailable), result


def test_an_unexpected_exception_in_answer_is_an_internal_error_cut_at_200(home, monkeypatch):
    """An exception that is not sqlite3.Error and not OSError. Mutation: `_handle` catches
    Exception but writes NO answer (the connection closes silently) -> red; detail not cut -> red."""
    with FakeOwner(home) as o:
        def boom(request):
            raise RuntimeError("y" * 500)

        monkeypatch.setattr(o, "_answer", boom)
        answer, records = call(o, "ping", None)
    assert records == []
    assert answer == {"status": "rejected", "reason": "internal_error",
                      "detail": ("RuntimeError: " + "y" * 500)[:200], "remedy": INTERNAL_ERROR_REMEDY}
    assert len(answer["detail"]) == 200


def test_an_unserialisable_answer_is_an_internal_error(home, monkeypatch):
    """The reply is built INSIDE the guard: json.dumps raising must not escape either.
    FAKE-SIDE LENIENCY, not parity: the real owner's `json.dumps(response)` is outside its
    catch-all, so an unserialisable answer kills the real thread; the fake answers
    `internal_error` instead. Pinned as the fake's own choice, not as 'mirrors the real owner'."""
    with FakeOwner(home) as o:
        monkeypatch.setattr(o, "_answer", lambda request: {"status": "ok", "result": object()})
        answer, records = call(o, "ping", None)
    assert records == []
    assert answer["reason"] == "internal_error" and answer["detail"].startswith("TypeError: "), answer


def test_an_oserror_while_answering_is_an_internal_error_like_the_real_owner(home, monkeypatch):
    """The real owner's catch-all wraps ALL of `_dispatch` with `except Exception`, and OSError
    is an Exception: an OSError raised while ANSWERING is the typed `internal_error`, the thread
    exits cleanly, zero thread exceptions. Mutation: the old inner `except OSError: raise`
    restored (closed, no answer) -> red."""
    with FakeOwner(home) as o:
        monkeypatch.setattr(o, "_answer", _boom_oserror)
        answer, records = call(o, "ping", None)
    assert records == []
    assert answer == {"status": "rejected", "reason": "internal_error",
                      "detail": "OSError: simulated transport failure", "remedy": INTERNAL_ERROR_REMEDY}


def test_a_failure_of_sendall_itself_closes_the_connection_with_no_answer_and_no_thread_exception(home, monkeypatch):
    """The transport write failing (connection gone) is the one OSError that is NOT answered:
    nothing can be sent. The outer `except OSError` swallows it; the connection is closed (EOF,
    no data), zero thread exceptions, the request was recorded (it parsed). Mutation: the outer
    handler removed (the OSError escapes the thread) -> red; the failed write answered with a
    typed rejection -> red."""
    with FakeOwner(home) as o:
        _fail_sendall(monkeypatch, o)
        answer, records = call(o, "ping", None)
        recorded = list(o.requests)
    assert records == [], f"the handler thread died: {records!r}"
    assert answer is None
    assert len(recorded) == 1 and recorded[0]["method"] == "ping"


def _boom_oserror(request):
    raise OSError("simulated transport failure")


def _boom_runtime(request):
    raise RuntimeError("boom")


@pytest.mark.parametrize("scenario", ["read-failed", "internal-error", "oserror", "sendall-failure", "healthy"])
def test_the_connection_is_explicitly_closed_whatever_the_outcome(home, monkeypatch, scenario):
    """The `finally: conn.close()` must run on every path. The test holds a reference to the
    connection object, so garbage collection cannot close it for the handler: a handler that
    forgot would leave fileno() != -1. Mutation: the close removed -> red."""
    if scenario == "read-failed":
        garbled_store(home)
    conns = []
    with FakeOwner(home) as o:
        real_handle = type(o)._handle

        def spy(conn):
            conns.append(conn)
            real_handle(o, conn)

        monkeypatch.setattr(o, "_handle", spy)  # _serve reads self._handle when it starts a thread
        if scenario == "sendall-failure":
            _fail_sendall(monkeypatch, o, conns)  # records the real socket, then runs the real handler
        if scenario == "internal-error":
            monkeypatch.setattr(o, "_answer", _boom_runtime)
        if scenario == "oserror":
            monkeypatch.setattr(o, "_answer", _boom_oserror)
        method = "read" if scenario == "read-failed" else "ping"
        call(o, method, params_for("read") if method == "read" else None)
    assert len(conns) == 1
    assert conns[0].fileno() == -1, "the handler left its connection open"


def test_a_second_request_after_a_typed_failure_still_works(home):
    """The fake keeps serving after a failure answer (the thread died with the old fixture, the
    serve loop did not; pin that a failure leaves the fake healthy)."""
    garbled_store(home)
    with FakeOwner(home) as o:
        first, r1 = call(o, "read", params_for("read"))
        second, r2 = call(o, "ping", None)
    assert first["reason"] == "read_failed"
    assert second == {"status": "ok", "result": "pong"}
    assert r1 == [] and r2 == []


def test_a_non_sqlite_exception_in_the_guarded_read_region_is_an_internal_error_not_a_read_failed(home):
    """Over-tightening neighbour: the guard is `sqlite3.Error`, not `Exception`. `args: 5` makes
    `tuple(5)` raise TypeError INSIDE the read branch's guarded region. Mutation: the read
    guard widened to `except Exception` -> a read_failed answer -> red."""
    chain_store(home)
    with FakeOwner(home) as o:
        answer, records = call(o, "read", {"sql": READ_SQL, "args": 5})
    assert records == []
    assert answer["reason"] == "internal_error" and answer["detail"].startswith("TypeError: "), answer
    assert answer["remedy"] == INTERNAL_ERROR_REMEDY


def test_a_non_sqlite_exception_in_the_chain_walk_is_an_internal_error_not_a_read_failed(home, monkeypatch):
    """The same over-tightening neighbour for `chain`. Mutation: the chain guard widened to
    `except Exception` -> read_failed -> red."""
    chain_store(home)

    def boom(conn, node_id, depth):
        raise RuntimeError("walk bug")

    monkeypatch.setattr(engram_core, "generalization_chain", boom)
    with FakeOwner(home) as o:
        answer, records = call(o, "chain", params_for("chain"))
    assert records == []
    assert answer == {"status": "rejected", "reason": "internal_error",
                      "detail": "RuntimeError: walk bug", "remedy": INTERNAL_ERROR_REMEDY}


def test_join_handlers_waits_for_a_running_handler_and_reports_a_timeout(home, monkeypatch):
    """The deterministic-exchange helper itself. A handler held inside `_answer` is alive:
    join_handlers with a short timeout returns False; once released it returns True. Mutation:
    a join that returns True without joining (or never times out) -> red."""
    gate = threading.Event()
    entered = threading.Event()
    with FakeOwner(home) as o:
        def held(request):
            entered.set()
            assert gate.wait(5.0)
            return {"status": "ok", "result": "pong"}

        monkeypatch.setattr(o, "_answer", held)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(str(o.socket_path))
        s.sendall(json.dumps({"method": "ping"}).encode("utf-8") + b"\n")
        try:
            assert entered.wait(5.0), "the handler never reached _answer"
            assert o.join_handlers(0.05) is False
            gate.set()
            assert o.join_handlers(5.0) is True
            assert not any(t.is_alive() for t in o._handlers)
        finally:
            gate.set()
            s.close()


def test_join_handlers_waits_for_every_handler_not_only_the_first(home, monkeypatch):
    """Two handlers alive at once, held inside `_answer`. The FIRST is released and finishes;
    `join_handlers` must still report False while the SECOND is alive, and True only once it
    finished too. Mutation: a join that waits for the first handler only -> red."""
    gates = {1: threading.Event(), 2: threading.Event()}
    entered = {1: threading.Event(), 2: threading.Event()}
    socks = []
    with FakeOwner(home) as o:
        def held(request):
            n = request["params"]["n"]
            entered[n].set()
            assert gates[n].wait(5.0)
            return {"status": "ok", "result": "pong"}

        monkeypatch.setattr(o, "_answer", held)
        try:
            for n in (1, 2):  # sequential: handler 1 is in `_handlers` before handler 2
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(5.0)
                s.connect(str(o.socket_path))
                socks.append(s)
                s.sendall(json.dumps({"method": "ping", "params": {"n": n}}).encode("utf-8") + b"\n")
                assert entered[n].wait(5.0), f"handler {n} never reached _answer"
            assert len(o._handlers) == 2
            gates[1].set()
            o._handlers[0].join(5.0)
            assert not o._handlers[0].is_alive() and o._handlers[1].is_alive()
            assert o.join_handlers(0.1) is False, "join_handlers returned True with a handler still alive"
            gates[2].set()
            assert o.join_handlers(5.0) is True
        finally:
            gates[1].set()
            gates[2].set()
            for s in socks:
                s.close()


class _Fatal(BaseException):
    """A private BaseException that is not an Exception (never KeyboardInterrupt / SystemExit)."""


def test_a_baseexception_in_answer_is_not_converted_into_a_typed_answer(home, monkeypatch):
    """Pins DELIBERATELY the real owner's `except Exception` (its own comment: 'Exception, not
    BaseException: SystemExit / KeyboardInterrupt still propagate'). A BaseException that is not
    an Exception gets NO typed answer (EOF) and reaches the thread's excepthook; the connection
    is still closed. Mutation: `except BaseException` in `_handle` (an internal_error answer,
    zero hook records) -> red."""
    def boom(request):
        raise _Fatal("fatal")

    with FakeOwner(home) as o:
        monkeypatch.setattr(o, "_answer", boom)
        answer, records = call(o, "ping", None)
    assert answer is None, f"a BaseException was converted into an answer: {answer!r}"
    assert len(records) == 1 and records[0].exc_type is _Fatal, records


@pytest.mark.parametrize("exc_factory,detail", [
    (lambda: RuntimeError("store bug"), "RuntimeError: store bug"),
    (lambda: OSError("disk gone"), "OSError: disk gone"),
], ids=["runtime-error", "oserror"])
@pytest.mark.parametrize("method", ["read", "df", "chain"])
def test_a_non_sqlite_exception_in_the_read_region_is_an_internal_error_not_a_read_failed(
        home, monkeypatch, method, exc_factory, detail):
    """For EVERY method (the `df` region had no non-sqlite neighbour): the guard is
    `sqlite3.Error` only. The exception is raised at `sqlite3.connect`, inside each guarded
    region. An OSError is not a read failure either: the real owner maps it to `internal_error`.
    Mutations: the `df` guard widened to `except Exception`; a read guard of
    `(sqlite3.Error, OSError)` -> a read_failed answer -> red."""
    def boom(*args, **kwargs):
        raise exc_factory()

    monkeypatch.setattr(sqlite3, "connect", boom)
    with FakeOwner(home) as o:
        answer, records = call(o, method, params_for(method))
    assert records == []
    assert answer == {"status": "rejected", "reason": "internal_error", "detail": detail,
                      "remedy": INTERNAL_ERROR_REMEDY}, answer


@pytest.mark.parametrize("method", ["read", "df", "chain"])
def test_the_read_failed_detail_is_cut_at_200_for_every_method(home, monkeypatch, method):
    """Per-method truncation (the chain-walk cell above covers only `chain`): a sqlite error
    with a 500-character message yields a `detail` of exactly 200 characters for `read` and
    `df` too. Mutation: an inline untruncated `detail` in the `read` (or the `df`) branch -> red."""
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("x" * 500)

    monkeypatch.setattr(sqlite3, "connect", boom)
    with FakeOwner(home) as o:
        answer, records = call(o, method, params_for(method))
    assert records == []
    assert answer["reason"] == "read_failed" and answer["remedy"] == READ_FAILED_REMEDY, answer
    assert answer["detail"] == ("OperationalError: " + "x" * 500)[:200]
    assert len(answer["detail"]) == 200


# --- C4: pinned against the real owner ------------------------------------------------------

@pytest.mark.parametrize("exc", [
    sqlite3.DatabaseError("file is not a database"),
    sqlite3.OperationalError("unable to open database file"),
    sqlite3.OperationalError("z" * 500),
    sqlite3.DataError("string or blob too big"),
], ids=["database-error", "operational-error", "long-message", "data-error"])
def test_c4_the_fakes_read_failed_equals_the_real_owners(exc):
    """Needs `engramd_owner` importable (the module import above already does; see the module
    docstring). Mutation: any of reason / detail / remedy
    / truncation different from `_read_failed` -> red."""
    eo = pytest.importorskip("engramd_owner")
    real = eo.EngramdOwner._read_failed(None, exc)  # only logs; never reads self
    assert _fake_owner._read_failed(exc) == real


def test_c4_the_fakes_internal_error_remedy_is_the_real_owners():
    """The real owner builds `internal_error` inline in its dispatch catch-all, so the remedy
    literal is pinned by presence in the owner's source, next to its reason and detail shape."""
    eo = pytest.importorskip("engramd_owner")
    src = inspect.getsource(eo)
    assert _fake_owner.INTERNAL_ERROR_REMEDY == INTERNAL_ERROR_REMEDY
    assert INTERNAL_ERROR_REMEDY in src
    assert '"reason": "internal_error"' in src
    assert 'f"{type(exc).__name__}: {exc}"[:200]' in src
    assert _fake_owner.READ_FAILED_REMEDY == READ_FAILED_REMEDY
    assert READ_FAILED_REMEDY in src
