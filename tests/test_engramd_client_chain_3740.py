"""Contract cells for the client shim's `generalization_chain` and the FakeOwner's `chain`
(#3740 Cut A).

Contract for `OwnerReadConnection.generalization_chain(node_id, max_depth=4)`:

  * sends ONE `chain` request {"node_id", "max_depth"} through the same budget / timeout /
    `.calls` accounting as `execute`: closed -> RuntimeError (nothing counted), budget
    exhausted -> OwnerReadError("budget_exhausted") (nothing sent, nothing counted), else
    one counted round trip with timeout min(per_call or total, remaining).
  * returns the owner's result dict as is; anything that is not a dict ->
    OwnerReadError("unusable_answer"); Unavailable -> "unavailable"; Rejected -> "rejected"
    carrying the exact result; a client crash -> "client_failure".
  * the client stays thin: an over-length node_id is SENT and the owner refuses it.

The FakeOwner serves `chain` faithfully: same validation order and reason strings, the walk
run by calling engram_core.generalization_chain on a read-only connection, same clamping,
same result bound. The faithfulness cells compare it to a REAL owner for every validation
reason and every equality fixture of tests/test_engramd_chain_3740.py.

Written BEFORE the implementation (contract-first).
"""
from __future__ import annotations

import inspect
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

import _fake_owner  # noqa: E402
from _fake_owner import FakeOwner, statement_of  # noqa: E402
from test_engramd_chain_3740 import (  # noqa: E402
    EQUALITY_CASES, ORDER_CASES, REASON_CASES, bare_store, chain_store, direct_chain,
)
from test_engramd_client_read_shim_3665 import Clock, StubRead  # noqa: E402

CHAIN_DICT = {"start_id": "ls_p", "start_resolved_id": "ls_p", "roots": ["ls_p"], "nodes": {}, "heads": [],
              "truncated": False, "cycle_detected": False}


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="chs-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# --- routing and accounting ---------------------------------------------------------------

def test_generalization_chain_sends_one_chain_request_and_returns_the_dict(monkeypatch):
    stub = StubRead(ec.Ok(result=CHAIN_DICT))
    monkeypatch.setattr(ec, "read", stub)
    sp = Path("/tmp/x.sock")
    conn = ec.OwnerReadConnection(2.0, socket_path=sp)
    got = conn.generalization_chain("ls_p", 3)
    assert got == CHAIN_DICT
    assert len(stub.calls) == 1
    method, params, _timeout, spath = stub.calls[0]
    assert (method, params, spath) == ("chain", {"node_id": "ls_p", "max_depth": 3}, sp)
    assert conn.calls == 1


def test_generalization_chain_default_depth_is_the_owners_cap(monkeypatch):
    """Mutation: a client default other than the function's own default -> red."""
    stub = StubRead(ec.Ok(result=CHAIN_DICT))
    monkeypatch.setattr(ec, "read", stub)
    ec.OwnerReadConnection(2.0).generalization_chain("ls_p")
    assert stub.calls[0][1] == {"node_id": "ls_p", "max_depth": 4}
    default = inspect.signature(ec.OwnerReadConnection.generalization_chain).parameters["max_depth"].default
    assert default == eo.CHAIN_MAX_DEPTH_CAP
    assert default == inspect.signature(engram_core.generalization_chain).parameters["max_depth"].default


def test_round_trip_accounting_budget_and_timeout(monkeypatch):
    """Same arithmetic as `execute` / the df route. Mutation M_SHIM_NO_COUNT (the round trip
    is not counted) -> `.calls` stays 0 -> red."""
    clk = Clock()
    monkeypatch.setattr(ec, "_monotonic", clk)
    stub = StubRead(ec.Ok(result=CHAIN_DICT))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(1.0, 0.6)
    conn.generalization_chain("ls_p")
    assert stub.calls[-1][2] == pytest.approx(0.6) and conn.calls == 1
    clk.t += 0.7
    conn.generalization_chain("ls_p")
    assert stub.calls[-1][2] == pytest.approx(0.3) and conn.calls == 2
    clk.t += 0.3  # remaining exactly 0
    with pytest.raises(ec.OwnerReadError) as ei:
        conn.generalization_chain("ls_p")
    assert ei.value.kind == "budget_exhausted" and ei.value.result is None
    assert len(stub.calls) == 2 and conn.calls == 2


def test_chain_and_execute_share_one_call_counter_and_budget(monkeypatch):
    clk = Clock()
    monkeypatch.setattr(ec, "_monotonic", clk)
    stub = StubRead(ec.Ok(result=[[1]]))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(1.0, 0.6)
    conn.execute("SELECT count(*) FROM nodes WHERE is_current = 1")
    stub.outcome = ec.Ok(result=CHAIN_DICT)
    conn.generalization_chain("ls_p")
    assert conn.calls == 2
    clk.t += 1.0
    with pytest.raises(ec.OwnerReadError) as ei:
        conn.generalization_chain("ls_p")
    assert ei.value.kind == "budget_exhausted"


def test_a_failed_round_trip_is_still_counted(monkeypatch):
    for outcome in (ec.Rejected(reason="r", detail="d", remedy="m"), ValueError("boom"), ec.Ok(result="x")):
        stub = StubRead(outcome)
        monkeypatch.setattr(ec, "read", stub)
        conn = ec.OwnerReadConnection(2.0)
        with pytest.raises(ec.OwnerReadError):
            conn.generalization_chain("ls_p")
        assert conn.calls == 1 and len(stub.calls) == 1


def test_closed_is_a_runtimeerror_before_the_budget_check_and_counts_nothing(monkeypatch):
    clk = Clock()
    monkeypatch.setattr(ec, "_monotonic", clk)
    stub = StubRead(ec.Ok(result=CHAIN_DICT))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(1.0)
    conn.close()
    clk.t += 5.0  # the budget is also gone: closed wins
    with pytest.raises(RuntimeError):
        conn.generalization_chain("ls_p")
    assert stub.calls == [] and conn.calls == 0


def test_the_context_manager_closes_the_chain_route_too(monkeypatch):
    monkeypatch.setattr(ec, "read", StubRead(ec.Ok(result=CHAIN_DICT)))
    with ec.OwnerReadConnection(2.0) as conn:
        conn.generalization_chain("ls_p")
    with pytest.raises(RuntimeError):
        conn.generalization_chain("ls_p")


def test_the_client_is_thin_an_over_length_node_id_is_sent_and_the_owner_refuses_it(monkeypatch):
    long_id = "a" * 300
    stub = StubRead(ec.Ok(result=CHAIN_DICT))
    monkeypatch.setattr(ec, "read", stub)
    ec.OwnerReadConnection(2.0).generalization_chain(long_id, 99)
    assert stub.calls[0][1] == {"node_id": long_id, "max_depth": 99}


# --- failure mapping ------------------------------------------------------------------------

def test_rejected_and_unavailable_map_like_execute(monkeypatch):
    rj = ec.Rejected(reason="chain_result_too_large", detail="d", remedy="m")
    monkeypatch.setattr(ec, "read", StubRead(rj))
    with pytest.raises(ec.OwnerReadError) as ei:
        ec.OwnerReadConnection(2.0).generalization_chain("ls_p")
    assert ei.value.kind == "rejected" and ei.value.result is rj
    assert "chain_result_too_large" in str(ei.value)
    un = ec.Unavailable(socket_path="/tmp/x.sock", bound=1.0, agent_steps=["a"], human_steps=["h"], detail="no answer")
    monkeypatch.setattr(ec, "read", StubRead(un))
    with pytest.raises(ec.OwnerReadError) as ei2:
        ec.OwnerReadConnection(2.0).generalization_chain("ls_p")
    assert ei2.value.kind == "unavailable" and ei2.value.result is un


@pytest.mark.parametrize("exc", [ValueError("boom"), SystemExit(3)], ids=["exception", "systemexit"])
def test_a_client_crash_is_client_failure_with_the_cause_kept(monkeypatch, exc):
    monkeypatch.setattr(ec, "read", StubRead(exc))
    with pytest.raises(ec.OwnerReadError) as ei:
        ec.OwnerReadConnection(2.0).generalization_chain("ls_p")
    assert ei.value.kind == "client_failure" and ei.value.__cause__ is exc


@pytest.mark.parametrize("answer", ["garbage", [], [["ls_p"]], None, 5, True], ids=repr)
def test_an_ok_answer_that_is_not_a_dict_is_unusable(monkeypatch, answer):
    """Mutation: pass any Ok result through -> a non-dict reaches the hook -> red."""
    monkeypatch.setattr(ec, "read", StubRead(ec.Ok(result=answer)))
    with pytest.raises(ec.OwnerReadError) as ei:
        ec.OwnerReadConnection(2.0).generalization_chain("ls_p")
    assert ei.value.kind == "unusable_answer"


def test_a_non_result_object_is_unusable(monkeypatch):
    monkeypatch.setattr(ec, "read", StubRead(object()))
    with pytest.raises(ec.OwnerReadError) as ei:
        ec.OwnerReadConnection(2.0).generalization_chain("ls_p")
    assert ei.value.kind == "unusable_answer"


# --- end to end over the fake and a real owner ---------------------------------------------------

def test_the_shim_over_the_fake_owner_equals_the_direct_function(home):
    db = chain_store(home)
    with FakeOwner(home) as owner:
        got = ec.OwnerReadConnection(5.0, socket_path=owner.socket_path).generalization_chain("ls_m")
    assert got == direct_chain(db, "ls_m", 4)
    assert sorted(got["nodes"]["ls_m"]["parents"]) == ["cs_a", "cs_b"]


@requires_linux_owner
def test_the_shim_over_a_real_owner_equals_the_direct_function(home):
    db = chain_store(home)
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        conn = ec.OwnerReadConnection(5.0, socket_path=owner.socket_path)
        got = conn.generalization_chain("ls_s")
        rejected = None
        try:
            conn.generalization_chain("")
        except ec.OwnerReadError as exc:
            rejected = exc
    finally:
        owner.stop()
    assert got == direct_chain(db, "ls_s", 4) and set(got["nodes"]) == {"ls_s"}  # #3542: live edges only
    assert rejected is not None and rejected.kind == "rejected"
    assert rejected.result.reason == "chain_node_id_empty"


# --- statement_of: a chain request is not a SQL statement -----------------------------------------

def test_statement_of_records_a_chain_request_at_method_level():
    assert statement_of("chain", {"node_id": "ls_p", "max_depth": 3}) == ("chain", ["ls_p", 3])
    assert statement_of("chain", None) == ("chain", [None, None])
    # the existing records are unchanged
    assert statement_of("df", {"terms": ["a"]})[1] == [json.dumps(["a"])]
    assert statement_of("read", {"sql": "SELECT 1", "args": [1]}) == ("SELECT 1", [1])
    assert statement_of("ping", {}) == (None, None)


def test_the_chain_marker_is_not_a_registered_template():
    """The method-level record can never be mistaken for (or registered as) read SQL."""
    assert "chain" not in _fake_owner.registered_sql()


# --- pins: the fake carries literal copies --------------------------------------------------------

def test_pin_fake_owner_constants_equal_the_production_ones():
    assert _fake_owner.CHAIN_NODE_ID_MAX_BYTES == eo.CHAIN_NODE_ID_MAX_BYTES
    assert _fake_owner.CHAIN_MAX_DEPTH_CAP == eo.CHAIN_MAX_DEPTH_CAP
    assert _fake_owner.CHAIN_RESULT_MAX_BYTES == eo.CHAIN_RESULT_MAX_BYTES


# --- FAITHFULNESS: the fake's `chain` against a REAL owner -----------------------------------------

class _Pair:
    """A FakeOwner and a real owner, each over its own copy of the same store."""

    def __init__(self, builder=chain_store):
        self.fake_home = Path(tempfile.mkdtemp(prefix="chf-"))
        self.real_home = Path(tempfile.mkdtemp(prefix="chr-"))
        for h in (self.fake_home, self.real_home):
            builder(h)

    def __enter__(self):
        self.fake = FakeOwner(self.fake_home)
        self.fake.start()
        self.real = eo.EngramdOwner(self.real_home, request_deadline=0.6)
        self.real.start(install_signal_handlers=False)
        return self

    def __exit__(self, *exc):
        self.real.stop()
        self.fake.stop()
        shutil.rmtree(self.fake_home, ignore_errors=True)
        shutil.rmtree(self.real_home, ignore_errors=True)

    def ask(self, params):
        f = ec.read("chain", params, timeout=5.0, socket_path=self.fake.socket_path)
        r = ec.read("chain", params, timeout=5.0, socket_path=self.real.socket_path)
        return f, r


@pytest.fixture(scope="module")
def pair():
    with _Pair() as p:
        yield p


@pytest.mark.parametrize("params,reason", REASON_CASES + ORDER_CASES)
@requires_linux_owner
def test_fake_chain_gives_the_same_status_and_reason_as_the_real_owner(pair, params, reason):
    """FAITHFULNESS: for every validation reason (and every order case) the fake and a REAL
    owner both reject with the same reason. Mutation M_FAKE_ACCEPTS_ALL (the fake skips
    validation) -> red for every id."""
    f, r = pair.ask(params)
    assert isinstance(r, ec.Rejected) and r.reason == reason, r
    assert isinstance(f, ec.Rejected), f
    assert f.reason == r.reason == reason
    assert f.detail and f.remedy


@pytest.mark.parametrize("node_id,max_depth,shape", EQUALITY_CASES)
@requires_linux_owner
def test_fake_chain_gives_the_same_result_as_the_real_owner_on_every_fixture(pair, node_id, max_depth, shape):
    f, r = pair.ask({"node_id": node_id, "max_depth": max_depth})
    assert isinstance(f, ec.Ok) and isinstance(r, ec.Ok), (f, r)
    shape(r.result)
    assert f.result == r.result == direct_chain(pair.real.store_path, node_id, max_depth)


@requires_linux_owner
def test_fake_chain_clamps_like_the_real_owner(pair):
    f, r = pair.ask({"node_id": "g6", "max_depth": 99})
    assert isinstance(f, ec.Ok) and isinstance(r, ec.Ok)
    assert f.result == r.result == direct_chain(pair.real.store_path, "g6", 4)
    assert r.result["truncated"] is True


@pytest.mark.parametrize("with_nodes", [False, True], ids=["no-tables", "nodes-only"])
@requires_linux_owner
def test_fake_chain_matches_the_real_owner_on_a_store_without_the_walk_tables(with_nodes):
    with _Pair(lambda h: bare_store(h, with_nodes=with_nodes)) as p:
        f, r = p.ask({"node_id": "ls_x", "max_depth": 4})
    assert isinstance(r, ec.Rejected) and r.reason == "read_failed", r
    assert isinstance(f, ec.Rejected) and f.reason == "read_failed", f


@requires_linux_owner
def test_fake_chain_matches_the_real_owner_on_the_result_bound(monkeypatch):
    with _Pair(lambda h: chain_store(h, head_claim="Z" * 5000)) as p:
        ok_f, ok_r = p.ask({"node_id": "ls_p", "max_depth": 4})
        assert isinstance(ok_f, ec.Ok) and isinstance(ok_r, ec.Ok) and ok_f.result == ok_r.result
        monkeypatch.setattr(eo, "CHAIN_RESULT_MAX_BYTES", 2000)
        monkeypatch.setattr(_fake_owner, "CHAIN_RESULT_MAX_BYTES", 2000)
        f, r = p.ask({"node_id": "ls_p", "max_depth": 4})
    assert isinstance(r, ec.Rejected) and r.reason == "chain_result_too_large", r
    assert isinstance(f, ec.Rejected) and f.reason == "chain_result_too_large", f


def test_fake_chain_runs_engram_cores_function_on_a_read_only_connection(home, monkeypatch):
    """The fake does not carry a copy of the walk: it calls engram_core.generalization_chain,
    and on a connection that cannot write."""
    chain_store(home)
    seen = []
    real = engram_core.generalization_chain

    def spy(conn, node_id, max_depth=4):
        try:
            conn.execute("CREATE TABLE should_not_exist (x)")
            writable = True
        except sqlite3.OperationalError:
            writable = False
        seen.append((node_id, max_depth, writable))
        return real(conn, node_id, max_depth)

    monkeypatch.setattr(engram_core, "generalization_chain", spy)
    with FakeOwner(home) as owner:
        r = ec.read("chain", {"node_id": "ls_p", "max_depth": 99}, timeout=5.0, socket_path=owner.socket_path)
    assert isinstance(r, ec.Ok)
    assert seen == [("ls_p", 4, False)]
