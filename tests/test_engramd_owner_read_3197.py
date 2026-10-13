"""Contract tests for the owner's client-facing `read` method, cells O1-O6 of
U4d (#3197, part of #2652).

Written BEFORE the implementation (contract-first; project CLAUDE.md,
"Contract-first tests"). The contract is the U4d spec's O1-O6 plus design
decisions D1 (wire form: the client sends the SQL literal + args), D2 (rows
as JSON lists in SQLite order; None/int/float/str cells only; a bytes cell is
`Rejected(unsupported_value_type)`, never a silent encode or drop) and D3
(contract version "1.1").

Wire form pinned here:  method="read", params={"sql": <str>, "args": <list>}
  -> {"status": "ok", "result": [[cell, ...], ...]}          (rows, SQLite order)
  -> {"status": "rejected", "reason": ..., "detail": ..., "remedy": ...}

Every test docstring names the mutation it must turn red under. Tests run a
REAL `EngramdOwner` on a temp store carrying the REAL nodes schema, with the
REAL committed template list -- no fixture registry except where a cell's
subject is the deadline axis (O6), as test 12 does.
"""
from __future__ import annotations

import ast
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402
import engramd_read_registry as rr  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = json.loads((Path(_SRC_ENGRAM) / "engramd_read_templates.json").read_text())


def _registered_identity_sql() -> str:
    """The user-identity hook's registered statement, read out of the
    committed template list (so these owner tests do not depend on the hook)."""
    found = [t["sql"] for t in TEMPLATES["templates"]
             if any("engram-user-identity-hook" in s for s in t["sites"])]
    assert len(found) == 1, found
    return found[0]


IDENTITY_SQL = _registered_identity_sql()

PEOPLE = [
    # (id, claim, logical_chain, metadata)
    ("pn_0001", "Alice", "first person", json.dumps({"name": "Alice", "aliases": ["Al"], "role": "lead"})),
    ("pn_0002", "Zoë 李雷", None, json.dumps({"name": "Zoë 李雷", "role": "friend"})),
    ("pn_0003", "Carol", "third", None),
]


def make_real_store(home: Path, people=PEOPLE, monkeypatch=None) -> Path:
    """knowledge.db with the REAL schema (engram_core's one-time setup) plus
    the given current person rows. Returns the db path."""
    import os
    import engram_core

    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        for pid, claim, chain, meta in people:
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, logical_chain, metadata, is_current) "
                "VALUES (?, 'person', ?, '2026-01-01T00:00:00', ?, ?, 1)",
                (pid, claim, chain, meta),
            )
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="edo-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def real_owner(home):
    make_real_store(home)
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        yield owner
    finally:
        owner.stop()


def _direct_ro(db: Path, sql: str, args=()) -> list:
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return [list(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _read(owner, sql, args, timeout=2.0):
    return ec.read("read", {"sql": sql, "args": args}, timeout=timeout, socket_path=owner.socket_path)


# --- O1 ----------------------------------------------------------------------

@requires_linux_owner
def test_o1_registered_template_returns_rows_equal_to_a_direct_ro_execution(real_owner):
    """Mutation: reverse the row order, drop a row, or turn a None cell into ''
    in the owner's `read` handler -> red. Non-vacuous: the store holds three
    people, one with a NULL logical_chain and one NULL metadata, one non-ASCII."""
    direct = _direct_ro(real_owner.store_path, IDENTITY_SQL)
    assert len(direct) == 3 and any(None in r for r in direct)
    result = _read(real_owner, IDENTITY_SQL, [])
    assert isinstance(result, ec.Ok), result
    assert result.result == direct
    assert all(isinstance(r, list) for r in result.result)


# --- O2 ----------------------------------------------------------------------

@pytest.mark.parametrize("mutated", [
    IDENTITY_SQL + " ",
    IDENTITY_SQL.lower(),
    IDENTITY_SQL.replace("= 1", "=  1"),
])
@requires_linux_owner
def test_o2_one_byte_mutation_is_rejected_unregistered_and_nothing_prepared(real_owner, mutated):
    """Reuses the R2 technique: a recording authorizer wrapped around the REAL
    client-read connection's own authorizer sees every statement preparation.
    Mutation: bypass the registry in `read` (execute `sql` directly) -> the
    reason is wrong / the recorder is non-empty -> red. Never `Unavailable`."""
    calls = []

    def recording(*a):
        calls.append(a)
        return eo._client_read_authorizer(*a)

    real_owner._client_read_conn.set_authorizer(recording)
    result = _read(real_owner, mutated, [])
    assert not isinstance(result, ec.Unavailable), result
    assert isinstance(result, ec.Rejected)
    assert result.reason == "unregistered_template"
    assert calls == [], "a refused read must prepare nothing on the client-read connection"


@pytest.mark.parametrize("params", [
    {"sql": 5, "args": []},
    {"sql": IDENTITY_SQL, "args": ["extra"]},
    {"sql": IDENTITY_SQL, "args": "notalist"},
    {"args": []},
])
@requires_linux_owner
def test_o2b_malformed_read_params_are_rejected_not_dropped(real_owner, params):
    """Mutation: let a malformed `read` raise in the handler (connection dropped
    -> the client sees Unavailable) -> red."""
    result = ec.read("read", params, timeout=2.0, socket_path=real_owner.socket_path)
    assert isinstance(result, ec.Rejected), result
    assert result.reason in {"invalid_read_request", "invalid_parameter"}


# --- O3 (test 22's `read` half lives in test_engramd_owner.py, extended) -------

@requires_linux_owner
def test_o3_read_executes_on_the_client_read_connection_only(real_owner):
    """Behavioural half of O3 on a REAL schema: the answer is correct while
    `_owner_conn` is a tripwire, and the gate's own connection IS
    `_client_read_conn`. Mutation: route `read` through `_owner_conn` -> the
    tripwire fires -> red."""
    class Tripwire:
        def __getattr__(self, name):
            if name == "close":
                return lambda: None
            raise AssertionError(f"`read` touched the owner-internal connection: .{name}")

    original = real_owner._owner_conn
    real_owner._owner_conn = Tripwire()
    try:
        result = _read(real_owner, IDENTITY_SQL, [])
    finally:
        real_owner._owner_conn = original
    assert isinstance(result, ec.Ok), result
    assert vars(real_owner._client_read_gate).get("_conn") is real_owner._client_read_conn


# --- O4 ----------------------------------------------------------------------

@requires_linux_owner
def test_o4_method_table_has_read_and_no_registration_verb(real_owner):
    """Static + live: `read` IS a method, nothing in the table or `_dispatch`
    literals is registration-shaped. Mutation: add a `register_template`
    entry -> red (the existing test_08 pair stays the primary control)."""
    import re
    assert "read" in real_owner._methods
    assert not [m for m in real_owner._methods if re.search(r"regist", m, re.I)]
    tree = ast.parse(Path(eo.__file__).read_text(encoding="utf-8"))
    for fn_name in ("_dispatch", "_handle_read"):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn_name)
        lits = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert not [v for v in lits if re.search(r"regist", v, re.I)], (fn_name, lits)


# --- O5 ----------------------------------------------------------------------

@requires_linux_owner
def test_o5_bytes_cell_is_rejected_unsupported_value_type(home):
    """D2. Mutation: encode bytes (hex/base64/latin-1), or drop the cell, or
    let json.dumps raise -> red. The first person's `claim` is a BLOB."""
    make_real_store(home, people=[("pn_0001", b"\x00\xffblob", "c", json.dumps({"name": "A"}))])
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        result = _read(owner, IDENTITY_SQL, [])
    finally:
        owner.stop()
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "unsupported_value_type"
    assert result.remedy


# --- O6 ----------------------------------------------------------------------

_BIG = (
    "WITH nums(x) AS (VALUES " + ", ".join("(%d)" % i for i in range(400)) + ") "
    "SELECT COUNT(*) FROM nums a, nums b, nums c WHERE a.x + b.x + c.x >= 0"
)


@requires_linux_owner
def test_o6_overrunning_read_gets_the_deadline_rejection_not_a_hang(real_owner, tmp_path):
    """The request deadline (0.6 s here) applies end to end to `read`: an
    expensive admitted statement is aborted and the client gets
    Rejected(deadline_exceeded) well inside the client's own 3 s bound.
    Mutation: drop the deadline from the `read` handler's execute call (pass
    a far-future one) -> the client sees Unavailable after its bound -> red.
    Mutation: let the OperationalError escape the handler -> connection
    dropped -> Unavailable -> red."""
    fixture_registry = rr.build_registry([_BIG], sqlite3.connect(":memory:"))
    real_owner._client_read_gate = eo._ClientReadGate(real_owner._client_read_conn, registry=fixture_registry, home=tmp_path)
    start = time.monotonic()
    result = _read(real_owner, _BIG, [], timeout=3.0)
    elapsed = time.monotonic() - start
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "deadline_exceeded"
    assert elapsed < real_owner.request_deadline + 0.5, elapsed


@requires_linux_owner
def test_o6b_a_read_waiting_for_the_client_read_lock_stops_at_the_deadline(real_owner):
    """Reads share ONE connection, so the owner serialises them; the wait for
    that serialisation is bounded by the same deadline. Mutation: a bare
    `lock.acquire()` with no timeout -> the client sees Unavailable -> red."""
    lock = real_owner._client_read_lock
    assert lock.acquire(timeout=1.0)
    try:
        start = time.monotonic()
        result = _read(real_owner, IDENTITY_SQL, [], timeout=3.0)
        elapsed = time.monotonic() - start
    finally:
        lock.release()
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "deadline_exceeded"
    assert elapsed < real_owner.request_deadline + 0.5, elapsed


@requires_linux_owner
def test_o6c_concurrent_reads_do_not_clear_each_others_deadline(real_owner):
    """Two clients at once, many times: every answer is the full correct row
    set. Mutation: drop the `_client_read_lock` around the gate call -> one
    thread's progress-handler reset races the other's statement (flaky-red
    by construction, so this is a smoke control; the lock's presence is
    pinned by test_o6b)."""
    direct = _direct_ro(real_owner.store_path, IDENTITY_SQL)
    failures = []

    def worker():
        for _ in range(30):
            r = _read(real_owner, IDENTITY_SQL, [], timeout=3.0)
            if not (isinstance(r, ec.Ok) and r.result == direct):
                failures.append(r)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert failures == []


@requires_linux_owner
def test_o7_oversized_cell_is_a_loud_rejection_not_a_dropped_connection(home):
    """The client-read connection's SQLITE_LIMIT_LENGTH makes an over-limit
    cell raise sqlite3.DataError inside the handler. Mutation: let it escape
    -> the client sees Unavailable -> red."""
    make_real_store(home, people=[("pn_0001", "x" * (eo.CLIENT_READ_LIMIT_LENGTH + 10), "c", "{}")])
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        result = _read(owner, IDENTITY_SQL, [])
    finally:
        owner.stop()
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "read_failed"


@requires_linux_owner
def test_contract_version_is_1_1_on_the_wire_and_on_ping(real_owner):
    """D3: adding a method is a MINOR bump, in the one shared constant.
    Mutation: leave the constant at "1.0" -> red."""
    assert ec.CONTRACT_VERSION == "1.1"
    assert real_owner.contract_version == "1.1"



# --- S3 (round 2): an oversized result is a refusal, never Unavailable ---------------

def _fat_people(n: int):
    """n person rows whose `claim` is just under the client-read connection's
    per-cell SQLITE_LIMIT_LENGTH, so the REGISTERED identity template returns a
    genuinely large result (~1.9 MB per row)."""
    cell = "x" * (eo.CLIENT_READ_LIMIT_LENGTH - 100_000)
    return [("pn_%04d" % i, cell, "c", "{}") for i in range(n)]


def _owner_with_fat_store(home, n):
    make_real_store(home, people=_fat_people(n))
    owner = eo.EngramdOwner(home, request_deadline=5.0)
    owner.start(install_signal_handlers=False)
    return owner


def test_s3_bound_is_strictly_below_the_clients_cap():
    """Mutation: raise the bound to (or past) the client cap -> red."""
    assert eo.MAX_READ_RESPONSE_BYTES < ec.MAX_RESPONSE_BYTES
    assert ec.MAX_RESPONSE_BYTES - eo.MAX_READ_RESPONSE_BYTES >= 1024 * 1024  # envelope headroom


@requires_linux_owner
def test_s3_a_result_over_the_bound_is_rejected_result_too_large_not_unavailable(home):
    """6 rows x ~1.9 MB = ~11 MB, over the client's 10 MiB cap: before the fix
    the client saw Unavailable and the hook told the agent to restart a
    healthy owner. Now: Rejected(result_too_large) with the byte count and a
    remedy, and the owner still answers the next read. Mutation: remove the
    bound -> Unavailable (client cap) -> red; truncate instead of refusing ->
    an Ok result -> red."""
    owner = _owner_with_fat_store(home, 6)
    try:
        result = _read(owner, IDENTITY_SQL, [], timeout=5.0)
        assert isinstance(result, ec.Rejected), result
        assert result.reason == "result_too_large"
        assert result.remedy
        assert result.detail and any(ch.isdigit() for ch in result.detail)
        again = _read(owner, IDENTITY_SQL, [], timeout=5.0)
        assert isinstance(again, ec.Rejected) and again.reason == "result_too_large", "owner stays healthy"
    finally:
        owner.stop()


@requires_linux_owner
def test_s3_a_result_under_the_bound_still_returns_ok(home):
    """3 rows x ~1.9 MB = ~5.7 MB: over any naive small bound, under ours.
    Mutation: a bound far too tight (e.g. 1 MiB) -> red, so the bound is not
    simply 'refuse big results'."""
    owner = _owner_with_fat_store(home, 3)
    try:
        result = _read(owner, IDENTITY_SQL, [], timeout=5.0)
        assert isinstance(result, ec.Ok), result
        assert len(result.result) == 3
    finally:
        owner.stop()


@requires_linux_owner
def test_s3_non_ascii_cells_are_measured_at_wire_size_not_utf8_size(home):
    """The bound is on the bytes the send path emits: json.dumps with the default
    ensure_ascii=True, so a CJK char is a 6-byte \\uXXXX escape on the wire but
    3 bytes in UTF-8. Three rows sized to land OVER the bound on the wire and
    UNDER it as ensure_ascii=False, so only a wire-size measure rejects them.
    Mutation: measure with json.dumps(cells, ensure_ascii=False) -> Ok -> red."""
    rows = 3
    chars = int(eo.MAX_READ_RESPONSE_BYTES / (rows * 4.5))  # wire ~1.33x bound, raw ~0.67x
    assert chars * 3 < eo.CLIENT_READ_LIMIT_LENGTH  # one cell fits the per-cell limit
    cell = "漢" * chars
    wire = rows * len(json.dumps([cell]).encode("utf-8"))
    raw = rows * len(json.dumps([cell], ensure_ascii=False).encode("utf-8"))
    assert wire > eo.MAX_READ_RESPONSE_BYTES > raw  # the test's own premise
    make_real_store(home, people=[("pn_%04d" % i, cell, "c", "{}") for i in range(rows)])
    owner = eo.EngramdOwner(home, request_deadline=5.0)
    owner.start(install_signal_handlers=False)
    try:
        result = _read(owner, IDENTITY_SQL, [], timeout=5.0)
        assert isinstance(result, ec.Rejected), result
        assert result.reason == "result_too_large"
    finally:
        owner.stop()


# --- Python < 3.11: no bare 3.11-only attribute in the read error path ---------------

class _RaisingGate:
    """A stand-in client-read gate whose execute raises the given error."""

    def __init__(self, exc):
        self._exc = exc

    def execute(self, sql, params=(), *, deadline):
        raise self._exc


@pytest.mark.parametrize("message,reason", [
    ("interrupted", "deadline_exceeded"),
    ("database is locked", "read_failed"),
])
@requires_linux_owner
def test_read_error_path_survives_a_python_without_sqlite_interrupt(real_owner, monkeypatch, message, reason):
    """`sqlite3.SQLITE_INTERRUPT` and `OperationalError.sqlite_errorcode` are
    3.11 additions; README-AGENT.md supports 3.10.7+. Simulated on any Python
    by deleting the constant. Mutation: restore the bare
    `sqlite3.SQLITE_INTERRUPT` in `_handle_read` -> both cells raise
    AttributeError out of the handler -> red."""
    monkeypatch.delattr(sqlite3, "SQLITE_INTERRUPT", raising=False)
    assert not hasattr(sqlite3, "SQLITE_INTERRUPT")
    real_owner._client_read_gate = _RaisingGate(sqlite3.OperationalError(message))
    wire = real_owner._handle_read({"sql": IDENTITY_SQL, "args": []}, time.monotonic() + 5.0)
    assert wire["status"] == "rejected" and wire["reason"] == reason, wire


# --- Root cause: an unexpected exception in a method is never a dead connection -------

@requires_linux_owner
def test_unexpected_exception_in_a_method_is_internal_error_and_the_owner_keeps_serving(
        real_owner, caplog):
    """`_handle_connection` used to have no catch-all around `_dispatch`, so any
    unexpected exception killed the connection thread, the client saw a closed
    socket (Unavailable) and told the agent to restart a healthy owner.
    Contract: Rejected(internal_error) with the exception type and message in
    `detail` and an owner-bug remedy; the traceback is logged at error level;
    later `ping` and `read` still work. Mutation: remove the catch-all ->
    Unavailable -> red."""
    def boom(params, deadline):
        raise RuntimeError("kaboom " + "x" * 500)

    real_owner._methods["read"] = boom
    with caplog.at_level("ERROR", logger="engramd_owner"):
        result = _read(real_owner, IDENTITY_SQL, [])
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "internal_error"
    assert result.detail.startswith("RuntimeError: kaboom")
    assert len(result.detail) <= 200
    assert "owner bug" in result.remedy and "owner log" in result.remedy
    errs = [r for r in caplog.records if r.levelname == "ERROR" and r.exc_info]
    assert errs and errs[0].exc_info[0] is RuntimeError

    ping = ec.read("ping", {}, timeout=2.0, socket_path=real_owner.socket_path)
    assert isinstance(ping, ec.Ok), ping
    real_owner._methods["read"] = lambda p, d: real_owner._handle_read(p, d)
    ok = _read(real_owner, IDENTITY_SQL, [])
    assert isinstance(ok, ec.Ok) and len(ok.result) == 3


@requires_linux_owner
def test_catch_all_does_not_swallow_baseexception_or_the_mapped_aborts(real_owner):
    """The wrap is `except Exception`: a BaseException (SystemExit /
    KeyboardInterrupt) raised by a method is NOT converted to a Rejected
    (it propagates out of the handler, as before), and the deadline abort
    the owner already maps still arrives as `deadline_exceeded`, not
    `internal_error`. Mutation: widen to `except BaseException` -> the first
    half goes red; wrap the mapped path too -> the second half goes red."""
    def stop(params, deadline):
        raise SystemExit(3)

    real_owner._methods["read"] = stop
    # Drive the connection handler directly (a socketpair) so the escaping
    # BaseException is observable here, not swallowed by a thread.
    import json as _json
    import socket as _socket

    server_side, client_side = _socket.socketpair()
    try:
        request = {"contract_version": ec.CONTRACT_VERSION, "method": "read",
                   "params": {"sql": IDENTITY_SQL, "args": []}}
        client_side.sendall((_json.dumps(request) + "\n").encode())
        with pytest.raises(SystemExit):
            real_owner._handle_connection(server_side)
    finally:
        client_side.close()
        server_side.close()

    real_owner._methods["read"] = lambda p, d: real_owner._handle_read(p, d)
    real_owner._client_read_gate = _RaisingGate(sqlite3.OperationalError("interrupted"))
    mapped = _read(real_owner, IDENTITY_SQL, [])
    assert isinstance(mapped, ec.Rejected) and mapped.reason == "deadline_exceeded", mapped
