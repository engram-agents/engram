"""Contract tests for the read-only owner connection shim in ``engramd_client``
(#3665 U6b stage 2b; design seat's ruling, #3665 comment 6103407104, item 1).

Written BEFORE the implementation (contract-first). Contract:

  * ``OwnerReadError`` carries ``.result`` (the exact Unavailable/Rejected, or None)
    and ``.kind``; ``str()`` is one short line naming the kind (+ the Rejected
    reason); never SQL text or row data.
  * ``OwnerReadConnection(total_budget, per_call_timeout=None, *, socket_path=None)``;
    ``execute(sql, params)`` calls ``read("read", {"sql", "args"}, ...)`` with
    ``timeout = min(per_call or total, remaining)``; ``remaining <= 0`` raises
    ``budget_exhausted`` WITHOUT calling the owner; ``.calls`` counts attempted round trips.
  * Ok -> validated rows (None/int/float/str cells) -> cursor of TUPLES; any other
    result raises; it never falls back to a direct open.
  * Cursor: fetchone / fetchall only (consuming); iteration -> TypeError;
    no rowcount / description. Connection: no row_factory / commit / cursor.
  * close() is a no-op after which execute raises RuntimeError("closed").

The existing tests/test_engramd_client*.py suites pass UNMODIFIED (the diff to
engramd_client.py is additive).
"""
from __future__ import annotations

import math
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from _fake_owner import FakeOwner
from _owner_platform import requires_linux_owner  # noqa: F401  (live-owner variants)

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core as core  # noqa: E402
import engram_idf  # noqa: E402
import engramd_client as ec  # noqa: E402

from test_chain_positional_rows_3665 import CASES as CHAIN_CASES  # noqa: E402
from test_chain_positional_rows_3665 import _new_conn as _chain_conn  # noqa: E402
from test_idf_batch_3665 import _SMALL_CORPUS, _make_db  # noqa: E402

COUNT_SQL = "SELECT count(*) FROM nodes WHERE is_current = 1"


# --- helpers -----------------------------------------------------------------

@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="rs-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _to_file(mem, home):
    mem.commit()
    dest = sqlite3.connect(str(home / "knowledge.db"))
    mem.backup(dest)
    dest.close()


def _direct_row_conn(home):
    uri = (home / "knowledge.db").resolve().as_uri() + "?mode=ro"
    c = sqlite3.connect(uri, uri=True)
    c.row_factory = sqlite3.Row
    return c


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class StubRead:
    """Replacement for ec.read: records calls, returns/raises a scripted outcome."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def __call__(self, method, params, *, timeout=None, socket_path=None):
        self.calls.append((method, params, timeout, socket_path))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def no_direct_open(monkeypatch):
    """Patch every direct-open path to raise-and-record."""
    hooks_dir = str(REPO_ROOT / "src" / "engram" / "hooks" / "claude")
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    import _hooklib

    recorded = []

    def boom(*a, **k):
        recorded.append((a, k))
        raise AssertionError("direct open attempted")

    monkeypatch.setattr(sqlite3, "connect", boom)
    monkeypatch.setattr(_hooklib, "connect_regular_ro", boom, raising=False)
    return recorded


def _unavailable():
    return ec._unavailable(socket_path="/x", bound=1.0, detail="d", request_id=None, reason="down")


# --- (d) refusal paths -------------------------------------------------------

def test_rejected_raises_and_no_direct_open(home, no_direct_open):
    """M3 (Rejected as empty) / M1 (fallback) turn this red."""
    # make a real store BEFORE patching is impossible (fixture patches first) -> no db needed:
    # the fake owner rejects unregistered SQL before touching the store.
    with FakeOwner(home) as o:
        conn = ec.OwnerReadConnection(2.0, socket_path=o.socket_path)
        with pytest.raises(ec.OwnerReadError) as ei:
            conn.execute("SELECT 1 /* not registered */")
    err = ei.value
    assert err.kind == "rejected"
    assert isinstance(err.result, ec.Rejected) and err.result.reason
    assert err.result.reason in str(err)
    assert "not registered" not in str(err)
    assert no_direct_open == []


def test_unavailable_raises_and_no_direct_open(home, no_direct_open):
    """M1 (fallback to sqlite3.connect on Unavailable) turns this red."""
    conn = ec.OwnerReadConnection(2.0, socket_path=home / "run" / "nope.sock")
    with pytest.raises(ec.OwnerReadError) as ei:
        conn.execute(COUNT_SQL)
    err = ei.value
    assert err.kind == "unavailable"
    assert isinstance(err.result, ec.Unavailable)
    assert err.result.agent_steps and err.result.human_steps
    assert "unavailable" in str(err) and COUNT_SQL not in str(err)
    assert no_direct_open == []


@pytest.mark.parametrize("answer", [
    "x", [["a"], 5], [[b"bytes"]], None, {"a": 1}, [[[1]]], [[True]], [{"a": 1}],
])
def test_unusable_answers(monkeypatch, answer):
    """M6 (accept bytes cells) turns the bytes case red."""
    monkeypatch.setattr(ec, "read", StubRead(ec.Ok(result=answer)))
    conn = ec.OwnerReadConnection(2.0)
    with pytest.raises(ec.OwnerReadError) as ei:
        conn.execute(COUNT_SQL)
    assert ei.value.kind == "unusable_answer"
    assert ei.value.result is None


def test_valid_answer_cells_accepted(monkeypatch):
    rows = [["a", 1, 2.5, None], ("b", 2, 3.5, None)]
    monkeypatch.setattr(ec, "read", StubRead(ec.Ok(result=rows)))
    out = ec.OwnerReadConnection(2.0).execute(COUNT_SQL).fetchall()
    assert out == [("a", 1, 2.5, None), ("b", 2, 3.5, None)]


@pytest.mark.parametrize("exc", [RuntimeError("secret sql SELECT x"), SystemExit(3)])
def test_read_raising_is_client_failure(monkeypatch, exc):
    monkeypatch.setattr(ec, "read", StubRead(exc))
    with pytest.raises(ec.OwnerReadError) as ei:
        ec.OwnerReadConnection(2.0).execute(COUNT_SQL)
    assert ei.value.kind == "client_failure"
    assert ei.value.__cause__ is exc
    assert ei.value.result is None
    assert "secret" not in str(ei.value)


def test_keyboard_interrupt_escapes(monkeypatch):
    """M5 (swallow KeyboardInterrupt) turns this red."""
    monkeypatch.setattr(ec, "read", StubRead(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        ec.OwnerReadConnection(2.0).execute(COUNT_SQL)


def test_typed_results_carried(monkeypatch):
    un = _unavailable()
    monkeypatch.setattr(ec, "read", StubRead(un))
    with pytest.raises(ec.OwnerReadError) as ei:
        ec.OwnerReadConnection(2.0).execute(COUNT_SQL)
    assert ei.value.result is un
    rj = ec.Rejected(reason="r_code", detail="d", remedy="m")
    monkeypatch.setattr(ec, "read", StubRead(rj))
    with pytest.raises(ec.OwnerReadError) as ei:
        ec.OwnerReadConnection(2.0).execute(COUNT_SQL)
    assert ei.value.result is rj and ei.value.kind == "rejected"
    assert "r_code" in str(ei.value) and "\n" not in str(ei.value)


# --- request shape / argument checks ----------------------------------------------

def test_request_shape_and_sql_sent_literally(monkeypatch):
    stub = StubRead(ec.Ok(result=[[1]]))
    monkeypatch.setattr(ec, "read", stub)
    sp = Path("/tmp/x.sock")
    conn = ec.OwnerReadConnection(2.0, socket_path=sp)
    sql = "SELECT  a\n  FROM t WHERE x = ?"
    conn.execute(sql, ("v", 3))
    method, params, timeout, spath = stub.calls[0]
    assert method == "read"
    assert params == {"sql": sql, "args": ["v", 3]}
    assert spath == sp


@pytest.mark.parametrize("sql,params", [(b"SELECT 1", ()), (None, ()), ("SELECT 1", "ab"),
                                         ("SELECT 1", {"a": 1}), ("SELECT 1", None)])
def test_bad_arguments_are_typeerror(monkeypatch, sql, params):
    stub = StubRead(ec.Ok(result=[]))
    monkeypatch.setattr(ec, "read", stub)
    with pytest.raises(TypeError):
        ec.OwnerReadConnection(2.0).execute(sql, params)
    assert stub.calls == []


def test_list_params_accepted(monkeypatch):
    stub = StubRead(ec.Ok(result=[]))
    monkeypatch.setattr(ec, "read", stub)
    ec.OwnerReadConnection(2.0).execute(COUNT_SQL, [1, 2])
    assert stub.calls[0][1]["args"] == [1, 2]


# --- cursor ---------------------------------------------------------------------

def test_cursor_tuples_and_consumption(monkeypatch):
    """M2 (lists instead of tuples) turns this red."""
    monkeypatch.setattr(ec, "read", StubRead(ec.Ok(result=[[1, "a"], [2, "b"], [3, "c"]])))
    cur = ec.OwnerReadConnection(2.0).execute(COUNT_SQL)
    first = cur.fetchone()
    assert type(first) is tuple and first == (1, "a")
    rest = cur.fetchall()
    assert type(rest) is list and rest == [(2, "b"), (3, "c")]
    assert all(type(r) is tuple for r in rest)
    assert cur.fetchone() is None
    assert cur.fetchall() == []
    assert cur.fetchone() is None


def test_cursor_fetchall_then_fetchone(monkeypatch):
    monkeypatch.setattr(ec, "read", StubRead(ec.Ok(result=[[1], [2]])))
    cur = ec.OwnerReadConnection(2.0).execute(COUNT_SQL)
    assert cur.fetchall() == [(1,), (2,)]
    assert cur.fetchone() is None


def test_empty_result(monkeypatch):
    monkeypatch.setattr(ec, "read", StubRead(ec.Ok(result=[])))
    cur = ec.OwnerReadConnection(2.0).execute(COUNT_SQL)
    assert cur.fetchone() is None and cur.fetchall() == []


def test_cursor_iteration_unsupported_and_minimal(monkeypatch):
    monkeypatch.setattr(ec, "read", StubRead(ec.Ok(result=[[1]])))
    cur = ec.OwnerReadConnection(2.0).execute(COUNT_SQL)
    with pytest.raises(TypeError):
        iter(cur)
    with pytest.raises(TypeError):
        for _ in cur:
            pass
    with pytest.raises(AttributeError):
        cur.rowcount
    with pytest.raises(AttributeError):
        cur.description


def test_connection_surface_is_minimal_and_close(monkeypatch):
    stub = StubRead(ec.Ok(result=[[1]]))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(2.0)
    for name in ("row_factory", "commit", "cursor"):
        with pytest.raises(AttributeError):
            getattr(conn, name)
    with pytest.raises(AttributeError):
        conn.row_factory = sqlite3.Row
    conn.close()
    with pytest.raises(RuntimeError, match="closed"):
        conn.execute(COUNT_SQL)
    assert stub.calls == []
    with ec.OwnerReadConnection(2.0) as c2:
        assert c2.execute(COUNT_SQL).fetchone() == (1,)
    with pytest.raises(RuntimeError, match="closed"):
        c2.execute(COUNT_SQL)


# --- budget ----------------------------------------------------------------------

def test_budget_exhaustion_and_per_call_timeout(monkeypatch):
    """M4 (ignore the total budget) turns this red."""
    clk = Clock()
    monkeypatch.setattr(ec, "_monotonic", clk)
    stub = StubRead(ec.Ok(result=[[1]]))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(1.0, 0.6)
    conn.execute(COUNT_SQL)                      # remaining 1.0 -> min(0.6, 1.0)
    assert stub.calls[-1][2] == pytest.approx(0.6)
    clk.t += 0.7
    conn.execute(COUNT_SQL)                      # remaining 0.3 -> min(0.6, 0.3)
    assert stub.calls[-1][2] == pytest.approx(0.3)
    assert conn.calls == 2
    clk.t += 0.3                                 # remaining exactly 0 -> refuse
    with pytest.raises(ec.OwnerReadError) as ei:
        conn.execute(COUNT_SQL)
    assert ei.value.kind == "budget_exhausted" and ei.value.result is None
    assert len(stub.calls) == 2                  # owner NOT called
    assert conn.calls == 2                       # refused call not counted
    clk.t += 5
    with pytest.raises(ec.OwnerReadError):
        conn.execute(COUNT_SQL)
    assert len(stub.calls) == 2


def test_default_per_call_is_total_budget(monkeypatch):
    clk = Clock()
    monkeypatch.setattr(ec, "_monotonic", clk)
    stub = StubRead(ec.Ok(result=[[1]]))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(2.5)
    clk.t += 0.5
    conn.execute(COUNT_SQL)
    assert stub.calls[0][2] == pytest.approx(2.0)


def test_calls_counts_failed_round_trips(monkeypatch):
    monkeypatch.setattr(ec, "read", StubRead(_unavailable()))
    conn = ec.OwnerReadConnection(2.0)
    for _ in range(2):
        with pytest.raises(ec.OwnerReadError):
            conn.execute(COUNT_SQL)
    assert conn.calls == 2


# --- never opens the store ----------------------------------------------------------

def test_every_path_never_opens_the_store(home, monkeypatch, no_direct_open):
    clk = Clock()
    outcomes = [ec.Ok(result=[[1]]), _unavailable(),
                ec.Rejected(reason="r", detail="d", remedy="m"),
                ec.Ok(result="x"), RuntimeError("x")]
    for oc in outcomes:
        monkeypatch.setattr(ec, "read", StubRead(oc))
        try:
            ec.OwnerReadConnection(2.0).execute(COUNT_SQL).fetchall()
        except ec.OwnerReadError:
            pass
    monkeypatch.setattr(ec, "_monotonic", clk)
    c = ec.OwnerReadConnection(0)
    with pytest.raises(ec.OwnerReadError):
        c.execute(COUNT_SQL)
    assert no_direct_open == []


# --- (b)-third-path: equivalence against the fake owner --------------------------------

@pytest.mark.parametrize("name,fixture,start,depth", CHAIN_CASES, ids=[c[0] for c in CHAIN_CASES])
def test_generalization_chain_via_shim_equals_direct(home, name, fixture, start, depth):
    mem = _chain_conn(sqlite3.Row)
    fixture(mem)
    _to_file(mem, home)
    direct = core.generalization_chain(_direct_row_conn(home), start, max_depth=depth)
    with FakeOwner(home) as o:
        shim = ec.OwnerReadConnection(5.0, socket_path=o.socket_path)
        via = core.generalization_chain(shim, start, max_depth=depth)
        assert shim.calls >= 1
    assert via == direct


def _idf_home(home, docs=None):
    _to_file(_make_db(docs if docs is not None else _SMALL_CORPUS), home)


TEXTS = {
    "plain": "the ranking formula orders nodes with algorithm convergence",
    "unicode": "café über naïve résumé xylophone",
    "duplicates": "formula formula algorithm algorithm xylophone xylophone",
    "stopwords": "the and of to a in is it formula",
    "unknown": "qqqzzz wwwvvv xylophone nonexistentterm",
    "empty": "",
    "mixed": "Mixed CASE Token here calibration 3 provenance 7",
}


@pytest.mark.parametrize("key", sorted(TEXTS))
@pytest.mark.parametrize("min_idf", [0.0, 4.0])
def test_extract_keywords_via_shim_equals_direct(home, key, min_idf):
    _idf_home(home)
    direct = engram_idf.extract_keywords(_direct_row_conn(home), TEXTS[key], min_idf=min_idf)
    with FakeOwner(home) as o:
        shim = ec.OwnerReadConnection(5.0, socket_path=o.socket_path)
        via = engram_idf.extract_keywords(shim, TEXTS[key], min_idf=min_idf)
    assert via == direct
    if key == "plain" and min_idf == 0.0:
        assert direct  # the cell is not vacuous


def test_extract_keywords_multi_chunk_equals_direct(home, monkeypatch):
    _idf_home(home)
    monkeypatch.setattr(engram_idf, "IDF_BATCH_TERMS", 3)
    text = " ".join(["formula", "algorithm", "xylophone", "ranking", "nodes", "weights",
                    "convergence", "embedding", "calibration", "provenance", "tunable", "unknownx"])
    direct = engram_idf.extract_keywords(_direct_row_conn(home), text, min_idf=0.0, top_k=50)
    with FakeOwner(home) as o:
        shim = ec.OwnerReadConnection(5.0, socket_path=o.socket_path)
        via = engram_idf.extract_keywords(shim, text, min_idf=0.0, top_k=50)
    assert len(direct) > 3
    assert via == direct


# --- round trips ---------------------------------------------------------------------

def _forty_tokens():
    return " ".join("zqtok%s" % chr(97 + i // 26) + chr(97 + i % 26) for i in range(40))


@pytest.mark.parametrize("batch", [1000, 16])
def test_idf_round_trips_are_count_plus_batches(home, monkeypatch, batch):
    _idf_home(home)
    monkeypatch.setattr(engram_idf, "IDF_BATCH_TERMS", batch)
    text = _forty_tokens()
    distinct = len(set(t for t in engram_idf.tokenize(text) if t not in engram_idf.STOPWORDS))
    assert distinct == 40
    with FakeOwner(home) as o:
        shim = ec.OwnerReadConnection(5.0, socket_path=o.socket_path)
        engram_idf.extract_keywords(shim, text, min_idf=0.0)
        assert shim.calls == 1 + math.ceil(distinct / batch)
        assert len(o.requests) == shim.calls
        methods = [q["method"] for q in o.requests]
        assert methods.count("df") == math.ceil(distinct / batch)
        assert methods.count("read") == 1


def test_chain_walk_round_trip_count_recorded(home):
    name, fixture, start, depth = CHAIN_CASES[1]  # concrete
    mem = _chain_conn(sqlite3.Row)
    fixture(mem)
    _to_file(mem, home)
    with FakeOwner(home) as o:
        shim = ec.OwnerReadConnection(5.0, socket_path=o.socket_path)
        core.generalization_chain(shim, start, max_depth=depth)
        assert shim.calls == len(o.requests) >= 3


# --- live owner variants (Linux seat) ------------------------------------------------------

@requires_linux_owner
def test_live_owner_chain_and_idf_equal_direct(home):
    import engramd_owner as eo

    mem = _chain_conn(sqlite3.Row)
    CHAIN_CASES[1][1](mem)
    _to_file(mem, home)
    direct = core.generalization_chain(_direct_row_conn(home), "ob_1", max_depth=4)
    o = eo.EngramdOwner(home, request_deadline=0.6)
    o.start(install_signal_handlers=False)
    try:
        shim = ec.OwnerReadConnection(5.0, socket_path=home / "run" / "engramd.sock")
        assert core.generalization_chain(shim, "ob_1", max_depth=4) == direct
    finally:
        o.stop()
