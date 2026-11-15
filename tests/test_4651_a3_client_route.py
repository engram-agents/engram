"""Contract cells for the #4651 part A slice A3: the client route for part B (`open_route`, `publish_batch`).

Written BEFORE the module exists (contract-first). The module is `src/engram/tools/_embedding_repair_route.py`; the contract is
the declared interface note `docs/internal/4651-part-a-interface.md` sections 3 and 3a, with the final names the code now has.

    open_route(engram_home) -> Route
    publish_batch(route, items, request_id=None) -> list[PublishResult]

Families: C1 open_route; C2 publish_batch validation and the per-batch flag re-read; C3 route "direct"; C4 route "owner" (wire
and the strict answer check); C5 the failures on the owner route; C7 parity of the two routes against the REAL owner; C8 the flag
is read only through `flag_applies`. (C6, the shared `_owner_read.write` attributes, has its own file.)

Every cell names its mutation in the docstring. The module is loaded lazily by the `rt` fixture, so while it does not exist each
cell is individually red (not one collection error).
"""
from __future__ import annotations

import ast
import dataclasses
import importlib
import importlib.util
import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner
from test_4651_a2_owner_op import (  # noqa: F401  (fixtures and helpers are REUSED from the A2 file, not copied)
    EPOCH, KEY, MARKER, MODEL, _normalise, _pin_modules, all_state, core, ec, eo, home, make_store, owner,
    recipe_sha, serialize_fails_for, sql, start, state, twins, vec, write_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "src" / "engram" / "tools" / "_embedding_repair_route.py"
FLAG = f"engramd.{KEY}"

_LOADED: dict = {}


def _load():
    """The module under test, loaded by path once (the tools load this way in their own cells)."""
    if "mod" not in _LOADED:
        spec = importlib.util.spec_from_file_location("p4651_a3_route", MODULE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LOADED["mod"] = mod
    return _LOADED["mod"]


@pytest.fixture
def rt():
    return _load()


@pytest.fixture
def pubm(_pin_modules):
    return importlib.import_module("engram_embedding_publish")


# ---- a fake client (patched onto the REAL engramd_client module, so the REAL `_owner_read` helpers run on top of it) -------------

class Spy:
    def __init__(self):
        self.reads: list = []
        self.writes: list = []   # (method, params, request_id)
        self.connects: list = []
        self.trace: list = []
        self.read_answer = None  # a client result, or a callable returning one / raising
        self.write_answer = None
        self.conns: list = []

    def ok_batch(self, params, statuses=None):
        """An owner answer shaped exactly like the real handler's, built from the params the client sent."""
        items = []
        for k, it in enumerate(params["items"]):
            status = (statuses or {}).get(it["node_id"], "refreshed")
            items.append({"node_id": it["node_id"], "status": status, "reason": None if status == "refreshed" else "x",
                          "vector_changed": True if status == "refreshed" else None, "debt_cleared": False})
        counts = {"refreshed": 0, "pending": 0, "deferred": 0}
        for i in items:
            counts[i["status"]] += 1
        return {"items": items, "counts": counts}


@pytest.fixture
def spy(monkeypatch):
    s = Spy()
    real_connect = sqlite3.connect

    def spy_connect(*a, **k):
        s.connects.append((a, k))
        conn = real_connect(*a, **k)
        conn.set_trace_callback(lambda stmt: s.trace.append(" ".join(stmt.split())))
        s.conns.append(conn)
        return conn

    def fake_read(method, params, **kw):
        s.reads.append((method, params))
        ans = s.read_answer
        if callable(ans):
            return ans(method, params)
        return ans if ans is not None else ec.Ok(result={"pong": True})

    def fake_write(method, params, *, request_id=None, **kw):
        s.writes.append((method, params, request_id))
        ans = s.write_answer
        if ans is None:
            raise AssertionError("an owner write was not expected here")
        if callable(ans):
            return ans(method, params, request_id)
        return ans

    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    monkeypatch.setattr(ec, "read", fake_read)
    monkeypatch.setattr(ec, "write", fake_write)
    return s


def unavailable(reason):
    return ec.Unavailable(socket_path="x", bound=1.0, agent_steps=["a"], human_steps=["h"], detail="d", reason=reason)


def rejected(reason, commit=None, detail="the detail", remedy="the remedy"):
    return ec.Rejected(reason=reason, detail=detail, remedy=remedy, commit=commit)


@pytest.fixture
def h(tmp_path):
    """A bare home with the flag ON and NO store (the owner route never opens one)."""
    d = tmp_path / "h"
    d.mkdir()
    write_config(d, "on")
    return d


def items_for(pubm, ids, *, epoch=None, vector=None):
    ep = epoch or pubm.EmbeddingEpoch(MODEL, 384, "model-default")
    return [(nid, pubm.PreparedVector(vector=(vector or vec(1.0 + k)), text_sha256=f"{k:064x}", epoch=ep))
            for k, nid in enumerate(ids)]


def owner_route(rt, h, spy):
    return rt.open_route(h)


def set_flag(home_dir: Path, flag):
    write_config(home_dir, flag)


# =====================================================================================================  C1
@pytest.mark.parametrize("variant", ["false", "absent", "no_config", "string_true", "int_one", "null", "malformed", "unreadable"])
def test_c1_every_non_true_flag_is_direct_and_the_owner_is_never_contacted(rt, h, spy, variant):
    """OFF, absent, unreadable or any non-`true` value -> Route(kind="direct"), no ping, no socket. Mutant: a truthy test
    instead of `is True` (the string and the 1 go red), or a ping before the flag is read (any variant goes red)."""
    cfg = h / "config.json"
    if variant == "false":
        write_config(h, "off")
    elif variant == "absent":
        write_config(h, None)
    elif variant == "no_config":
        cfg.unlink()
    elif variant == "string_true":
        cfg.write_text(json.dumps({"engramd": {KEY: "true"}}))
    elif variant == "int_one":
        cfg.write_text(json.dumps({"engramd": {KEY: 1}}))
    elif variant == "null":
        cfg.write_text(json.dumps({"engramd": {KEY: None}}))
    elif variant == "malformed":
        cfg.write_text("{not json")
    elif variant == "unreadable":
        cfg.unlink()
        cfg.mkdir()
    route = rt.open_route(h)
    assert route.kind == "direct"
    assert spy.reads == [] and spy.writes == []
    assert spy.connects == []  # open_route holds no connection and opens nothing


def test_c1_flag_on_gives_the_owner_route_after_exactly_one_ping(rt, h, spy):
    """Mutant: no ping (reads == 0), or a ping per call (reads == 2)."""
    route = rt.open_route(h)
    assert route.kind == "owner"
    assert [m for m, _p in spy.reads] == ["ping"]
    assert spy.connects == []
    assert route.spec is not None and route.spec.flag_key == KEY and route.spec.error_cls is rt.OwnerUnavailable
    assert Path(route.home) == h


def test_c1_a_ping_failure_raises_owner_unavailable_and_falls_back_to_nothing(rt, h, spy):
    """Mutant: the ping failure is swallowed and the route degrades to "direct" (no raise), or the store is opened."""
    spy.read_answer = unavailable("down")
    with pytest.raises(rt.OwnerUnavailable) as e:
        rt.open_route(h)
    assert FLAG in str(e.value)
    assert isinstance(e.value, rt.RouteError)
    assert spy.connects == [] and spy.writes == []


def test_c1_a_str_home_and_a_path_home_are_the_same_route(rt, h, spy):
    """Mutant: a str is treated differently from a Path (a str has no `/` operator)."""
    assert rt.open_route(str(h)).kind == rt.open_route(h).kind == "owner"


def test_c1_route_is_a_frozen_dataclass_holding_no_connection(rt, h, spy):
    """Mutant: a mutable Route, or one carrying a connection field."""
    route = rt.open_route(h)
    assert dataclasses.is_dataclass(route)
    assert [f.name for f in dataclasses.fields(route)] == ["kind", "home", "spec"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        route.kind = "direct"
    assert not any(isinstance(getattr(route, f.name), sqlite3.Connection) for f in dataclasses.fields(route))


def test_c1_the_flag_is_read_once_by_open_route_through_flag_applies(rt, h, spy, monkeypatch):
    """Mutant: open_route reads the flag twice, or by a path other than `flag_applies`."""
    seen = []
    real = rt.owner_read.flag_applies
    monkeypatch.setattr(rt.owner_read, "flag_applies", lambda spec, db: (seen.append((spec.flag_key, Path(db))), real(spec, db))[1])
    rt.open_route(h)
    assert seen == [(KEY, h / "knowledge.db")]


# =====================================================================================================  C2
def test_c2_max_batch_is_pinned_equal_to_the_owners_cap(rt):
    """Mutant: either number moves alone."""
    assert rt.MAX_BATCH == eo.PUBLISH_PREPARED_MAX_ITEMS == 50


def _bad_batches(pubm):
    good = items_for(pubm, ["a", "b"])
    other_epoch = pubm.EmbeddingEpoch("other-model", 384, "model-default")
    return {
        "empty": [],
        "fifty_one": items_for(pubm, [f"n{i}" for i in range(51)]),
        "non_pair_three": [("a", good[0][1], 1)],
        "non_pair_str": ["ab"],
        "not_a_list": (good[0],),
        "repeated_node_id": [good[0], ("a", good[1][1])],
        "not_a_prepared_vector": [("a", {"vector": [0.0] * 384})],
        "mixed_epoch": [good[0], ("b", pubm.PreparedVector(vector=good[1][1].vector, text_sha256="0" * 64, epoch=other_epoch))],
    }


@pytest.mark.parametrize("name", ["empty", "fifty_one", "non_pair_three", "non_pair_str", "not_a_list", "repeated_node_id",
                                  "not_a_prepared_vector", "mixed_epoch"])
def test_c2_a_bad_batch_is_a_value_error_before_the_flag_is_read_or_anything_is_opened(rt, h, spy, pubm, monkeypatch, name):
    """Mutant: the check runs after the flag read or after the owner call; one of the eight checks is missing."""
    route = rt.open_route(h)
    spy.reads.clear()
    seen = []
    monkeypatch.setattr(rt.owner_read, "flag_applies", lambda *a: seen.append(a) or True)
    spy.write_answer = lambda *a: pytest.fail("an owner write happened for an invalid batch")
    with pytest.raises(ValueError):
        rt.publish_batch(route, _bad_batches(pubm)[name])
    assert seen == [] and spy.writes == [] and spy.connects == [] and spy.reads == []


def test_c2_fifty_items_are_accepted(rt, h, spy, pubm):
    """Mutant: an off-by-one cap (50 refused)."""
    route = rt.open_route(h)
    items = items_for(pubm, [f"n{i}" for i in range(50)])
    spy.write_answer = lambda method, params, rid: ec.Ok(result=spy.ok_batch(params), request_id=rid)
    assert len(rt.publish_batch(route, items)) == 50


def test_c2_the_flag_is_reread_before_each_batch(rt, h, spy, pubm, monkeypatch):
    """Mutant: the flag is read only at open (the count stays at one)."""
    route = rt.open_route(h)
    seen = []
    real = rt.owner_read.flag_applies
    monkeypatch.setattr(rt.owner_read, "flag_applies", lambda spec, db: (seen.append(1), real(spec, db))[1])
    spy.write_answer = lambda method, params, rid: ec.Ok(result=spy.ok_batch(params), request_id=rid)
    rt.publish_batch(route, items_for(pubm, ["a"]))
    rt.publish_batch(route, items_for(pubm, ["b"]))
    assert len(seen) == 2


@pytest.mark.parametrize("opened,after", [("off", "on"), ("on", "off"), ("on", "unreadable")])
def test_c2_a_flag_that_no_longer_matches_the_route_raises_route_changed_and_writes_nothing(rt, h, spy, pubm, opened, after):
    """Both directions raise, and an unreadable flag reads OFF. Mutants: only OFF->ON checked; only ON->OFF checked; an
    unreadable flag treated as 'unchanged'; the route silently switched to the new flag value."""
    set_flag(h, opened)
    route = rt.open_route(h)
    if after == "unreadable":
        cfg = h / "config.json"
        cfg.unlink()
        cfg.mkdir()
    else:
        set_flag(h, after)
    spy.write_answer = lambda *a: pytest.fail("an owner write happened after the route changed")
    with pytest.raises(rt.RouteChanged) as e:
        rt.publish_batch(route, items_for(pubm, ["a"]))
    assert FLAG in str(e.value) and isinstance(e.value, rt.RouteError) and e.value.results == []
    assert spy.writes == []
    assert not [c for c in spy.connects if "knowledge.db" in str(c[0])]


def test_c2_an_unchanged_off_route_does_not_raise_route_changed(rt, spy, pubm, tmp_path):
    """Control for the table above: with the flag still OFF a direct route runs (a flag compare that always raises would go red)."""
    d = tmp_path / "off"
    make_store(d, ids=("a",), flag="off")
    core._configure_paths(d)
    route = rt.open_route(d)
    assert route.kind == "direct"
    items = [("a", pubm.PreparedVector(vector=vec(2.0), text_sha256=recipe_sha(d, "a"),
                                       epoch=pubm.EmbeddingEpoch(MODEL, 384, "model-default")))]
    assert [r.status for r in rt.publish_batch(route, items)] == ["refreshed"]


# =====================================================================================================  C3
def _direct_setup(rt, tmp_path, ids=("a", "b", "c")):
    d = tmp_path / "dh"
    make_store(d, ids=ids, flag="off")
    core._configure_paths(d)
    return d, rt.open_route(d)


def _prepared(pubm, d, ids, *, seeds=None):
    ep = pubm.EmbeddingEpoch(MODEL, 384, "model-default")
    return [(nid, pubm.PreparedVector(vector=vec((seeds or {}).get(nid, 1.0 + k)), text_sha256=recipe_sha(d, nid), epoch=ep))
            for k, nid in enumerate(ids)]


def test_c3_direct_one_connection_one_transaction_per_row_closed_at_the_end(rt, spy, pubm, tmp_path):
    """Results in request order. Mutants: one transaction per BATCH (one BEGIN), one connection per row, the connection left open."""
    d, route = _direct_setup(rt, tmp_path)
    items = _prepared(pubm, d, ["c", "a", "b"])
    spy.trace.clear()
    spy.connects.clear()
    results = rt.publish_batch(route, items)
    assert [r.node_id for r in results] == ["c", "a", "b"]
    assert [r.status for r in results] == ["refreshed"] * 3
    assert len([c for c in spy.connects if "knowledge.db" in str(c[0])]) == 1
    assert len([s for s in spy.trace if s.upper().startswith("BEGIN IMMEDIATE")]) == 3
    assert len([s for s in spy.trace if s.upper() == "COMMIT"]) == 3
    with pytest.raises(sqlite3.ProgrammingError):
        spy.conns[-1].execute("SELECT 1")  # closed
    assert state(d, "a")[0] == json.dumps(vec(2.0))  # the per-row publishes really landed


def test_c3_direct_the_owner_is_never_contacted(rt, spy, pubm, tmp_path):
    """Mutant: a direct route that pings or writes through the owner."""
    d, route = _direct_setup(rt, tmp_path)
    rt.publish_batch(route, _prepared(pubm, d, ["a"]))
    assert spy.reads == [] and spy.writes == []


@pytest.mark.parametrize("exc_cls", [sqlite3.OperationalError, RuntimeError])
def test_c3_a_mid_batch_error_rolls_the_row_back_stops_and_carries_the_committed_rows(rt, spy, pubm, tmp_path, monkeypatch, exc_cls):
    """Row 2 fails after writing something: rolled back; row 1 stays committed and is in `.results`; row 3 is untouched.
    Mutants: the batch continues after the error (row 3 changed); `.results` empty; the failed row's write not rolled back;
    the cause not carried."""
    d, route = _direct_setup(rt, tmp_path)
    items = _prepared(pubm, d, ["a", "b", "c"])
    before = all_state(d, ["a", "b", "c"])
    real = pubm.publish_embedding
    calls = []

    def flaky(conn, node_id, *, prepared=None):
        calls.append(node_id)
        if node_id == "b":
            conn.execute("UPDATE nodes SET embedding = ? WHERE id = ?", (json.dumps(vec(99.0)), node_id))
            raise exc_cls("injected")
        return real(conn, node_id, prepared=prepared)

    monkeypatch.setattr(pubm, "publish_embedding", flaky)
    with pytest.raises(rt.DirectWriteError) as e:
        rt.publish_batch(route, items)
    assert isinstance(e.value, rt.RouteError)
    assert [r.node_id for r in e.value.results] == ["a"] and e.value.results[0].status == "refreshed"
    assert isinstance(e.value.__cause__, exc_cls) or isinstance(getattr(e.value, "cause", None), exc_cls)
    after = all_state(d, ["a", "b", "c"])
    assert after["a"] != before["a"]          # committed
    assert after["b"] == before["b"]          # rolled back
    assert after["c"] == before["c"]          # untouched
    assert calls == ["a", "b"]                # stopped: row c never attempted


def test_c3_a_first_row_error_gives_empty_results(rt, spy, pubm, tmp_path, monkeypatch):
    """Mutant: `.results` is filled with something other than the committed rows."""
    d, route = _direct_setup(rt, tmp_path)
    items = _prepared(pubm, d, ["a", "b"])

    def boom(conn, node_id, *, prepared=None):
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(pubm, "publish_embedding", boom)
    with pytest.raises(rt.DirectWriteError) as e:
        rt.publish_batch(route, items)
    assert e.value.results == []


def test_c3_a_missing_store_is_a_direct_write_error_and_creates_no_file(rt, spy, pubm, tmp_path):
    """Mutant: sqlite3.connect creates an empty knowledge.db where none exists."""
    d = tmp_path / "nostore"
    d.mkdir()
    write_config(d, "off")
    core._configure_paths(d)
    route = rt.open_route(d)
    with pytest.raises(rt.DirectWriteError) as e:
        rt.publish_batch(route, items_for(pubm, ["a"]))
    assert e.value.results == []
    assert not (d / "knowledge.db").exists()


# =====================================================================================================  C4
def _wire(spy, pubm_items):
    return spy.writes[-1]


def test_c4_the_owner_params_are_exactly_the_wire_shape_vectors_as_sent_the_request_id_forwarded(rt, h, spy, pubm):
    """Mutants: a normalised vector, an extra or missing key, a different epoch spelling, the request_id not forwarded."""
    route = rt.open_route(h)
    raw = [3.0] * 384  # un-normalised on purpose
    ints = list(range(384))
    ep = pubm.EmbeddingEpoch(MODEL, 384, "model-default")
    items = [("a", pubm.PreparedVector(vector=raw, text_sha256="aa" * 32, epoch=ep)),
             ("b", pubm.PreparedVector(vector=ints, text_sha256="bb" * 32, epoch=ep))]
    spy.write_answer = lambda method, params, rid: ec.Ok(result=spy.ok_batch(params), request_id=rid)
    rt.publish_batch(route, items, request_id="my-fixed-id")
    method, params, rid = spy.writes[-1]
    assert method == "publish_prepared_embeddings"
    assert rid == "my-fixed-id"
    assert params == {"epoch": {"model": MODEL, "dimension": 384, "normalisation": "model-default"},
                      "items": [{"node_id": "a", "vector": raw, "text_sha256": "aa" * 32},
                                {"node_id": "b", "vector": ints, "text_sha256": "bb" * 32}]}
    assert params["items"][1]["vector"] == ints and all(type(x) is int for x in params["items"][1]["vector"])


def test_c4_a_fresh_uuid_per_call_when_no_request_id_is_given(rt, h, spy, pubm):
    """Mutant: a constant id, or an id minted at import / open_route."""
    route = rt.open_route(h)
    spy.write_answer = lambda method, params, rid: ec.Ok(result=spy.ok_batch(params), request_id=rid)
    rt.publish_batch(route, items_for(pubm, ["a"]))
    rt.publish_batch(route, items_for(pubm, ["a"]))
    ids = [w[2] for w in spy.writes]
    assert len(set(ids)) == 2
    assert all(str(uuid.UUID(i)) == i for i in ids)


def test_c4_a_given_request_id_is_used_on_every_call_never_replaced_by_a_fresh_uuid(rt, h, spy, pubm):
    """Mutant: a fresh uuid on every call even when `request_id` is given (the retry would not replay)."""
    route = rt.open_route(h)
    spy.write_answer = lambda method, params, rid: ec.Ok(result=spy.ok_batch(params), request_id=rid)
    rt.publish_batch(route, items_for(pubm, ["a"]), request_id="same-id")
    rt.publish_batch(route, items_for(pubm, ["a"]), request_id="same-id")
    assert [w[2] for w in spy.writes] == ["same-id", "same-id"]


def test_c4_the_answer_becomes_publish_results_in_order_with_no_warning(rt, h, spy, pubm):
    """Mutant: results re-ordered by status; a warning invented; fields dropped."""
    route = rt.open_route(h)
    answer_statuses = {"b": "pending", "c": "deferred"}
    spy.write_answer = lambda method, params, rid: ec.Ok(result=spy.ok_batch(params, answer_statuses), request_id=rid)
    out = rt.publish_batch(route, items_for(pubm, ["a", "b", "c"]))
    assert all(isinstance(r, pubm.PublishResult) for r in out)
    assert [(r.node_id, r.status, r.reason, r.vector_changed, r.debt_cleared, r.warning) for r in out] == [
        ("a", "refreshed", None, True, False, None),
        ("b", "pending", "x", None, False, None),
        ("c", "deferred", "x", None, False, None)]


def test_c4_the_owner_route_never_opens_the_store(rt, h, spy, pubm):
    """Mutant: any sqlite3.connect on the owner route (a read-back, a fallback)."""
    route = rt.open_route(h)
    spy.write_answer = lambda method, params, rid: ec.Ok(result=spy.ok_batch(params), request_id=rid)
    rt.publish_batch(route, items_for(pubm, ["a"]))
    assert spy.connects == []


def _malformed(name, params):
    good = Spy().ok_batch(params)
    items = good["items"]
    mk = lambda **kw: {"items": kw.get("items", items), "counts": kw.get("counts", good["counts"])}  # noqa: E731
    sh = lambda i, **kw: [dict(x, **kw) if k == i else x for k, x in enumerate(items)]  # noqa: E731
    table = {
        "items_not_a_list": {"items": "nope", "counts": good["counts"]},
        "items_missing": {"counts": good["counts"]},
        "one_short": mk(items=items[:-1]),
        "one_extra": mk(items=items + [dict(items[0], node_id="zz")]),
        "reordered": mk(items=list(reversed(items))),
        "wrong_node_id": mk(items=sh(0, node_id="other")),
        "bad_status": mk(items=sh(0, status="ok")),
        "reason_not_str": mk(items=sh(0, reason=5)),
        "vector_changed_not_bool": mk(items=sh(0, vector_changed="yes")),
        "debt_cleared_not_bool": mk(items=sh(0, debt_cleared=1)),
        "entry_not_a_dict": mk(items=["x"] + items[1:]),
        "missing_key": mk(items=[{k: v for k, v in items[0].items() if k != "debt_cleared"}] + items[1:]),
        "counts_wrong": mk(counts={"refreshed": 0, "pending": 0, "deferred": 2}),
        "counts_missing": {"items": items},
        "counts_not_a_dict": mk(counts=[2, 0, 0]),
    }
    return table[name]


_MALFORMED = ["items_not_a_list", "items_missing", "one_short", "one_extra", "reordered", "wrong_node_id", "bad_status",
              "reason_not_str", "vector_changed_not_bool", "debt_cleared_not_bool", "entry_not_a_dict", "missing_key",
              "counts_wrong", "counts_missing", "counts_not_a_dict"]


@pytest.mark.parametrize("name", _MALFORMED)
def test_c4_a_malformed_answer_is_indeterminate_never_believed(rt, h, spy, pubm, name):
    """The owner may have committed, so a malformed answer is INDETERMINATE with the request id to retry by. Mutants: the
    accounting check removed (one_short / one_extra / counts_*), the order check removed (reordered / wrong_node_id), a
    field check removed."""
    route = rt.open_route(h)
    spy.write_answer = lambda method, params, rid: ec.Ok(result=_malformed(name, params), request_id=rid)
    with pytest.raises(rt.OwnerUnavailable) as e:
        rt.publish_batch(route, items_for(pubm, ["a", "b"]), request_id="rid-1")
    assert e.value.commit == "indeterminate" and e.value.reason == "malformed" and e.value.request_id == "rid-1"
    assert FLAG in str(e.value)
    assert spy.connects == []


# =====================================================================================================  C5
_REFUSALS = [
    ("rejected_invalid_params", lambda: rejected("invalid_params"), "invalid_params", None),
    ("rejected_busy", lambda: rejected("busy"), "busy", None),
    ("rejected_vec_unavailable", lambda: rejected("vec_unavailable"), "vec_unavailable", None),
    ("rejected_request_id_reused", lambda: rejected("request_id_reused"), "request_id_reused", None),
    ("rejected_indeterminate", lambda: rejected("publish_write_failed", commit="indeterminate"), "publish_write_failed", "indeterminate"),
    ("committed_unverified", lambda: rejected("readback_mismatch", commit="committed_unverified"), "readback_mismatch",
     "committed_unverified"),
    ("down", lambda: unavailable("down"), "down", None),
    ("timeout", lambda: unavailable("timeout"), "timeout", "indeterminate"),
]


@pytest.mark.parametrize("name,make,reason,commit", _REFUSALS, ids=[r[0] for r in _REFUSALS])
def test_c5_every_other_failure_is_owner_unavailable_with_reason_commit_and_the_request_id(rt, h, spy, pubm, name, make, reason,
                                                                                        commit):
    """Never followed by a direct write. Mutants: a failure class collapsed into OwnerFlagOff; `.request_id` missing; `.commit`
    not carried; a direct write after the failure."""
    route = rt.open_route(h)
    spy.write_answer = lambda method, params, rid: make()
    with pytest.raises(rt.OwnerUnavailable) as e:
        rt.publish_batch(route, items_for(pubm, ["a"]), request_id="rid-xyz")
    assert not isinstance(e.value, rt.OwnerFlagOff)
    assert (e.value.reason, e.value.commit, e.value.request_id) == (reason, commit, "rid-xyz")
    assert isinstance(e.value, rt.RouteError) and e.value.results == []
    assert FLAG in str(e.value)
    assert spy.connects == []


def test_c5_a_client_that_raises_is_indeterminate(rt, h, spy, pubm):
    """`client.write` is not supposed to raise; if it does the outcome is indeterminate. Mutant: the exception escapes raw."""
    route = rt.open_route(h)

    def boom(method, params, rid):
        raise OSError("socket gone")

    spy.write_answer = boom
    with pytest.raises(rt.OwnerUnavailable) as e:
        rt.publish_batch(route, items_for(pubm, ["a"]), request_id="r9")
    assert (e.value.reason, e.value.commit, e.value.request_id) == ("indeterminate", "indeterminate", "r9")


def test_c5_the_generated_request_id_is_on_the_error_so_part_b_can_retry_the_same_batch(rt, h, spy, pubm):
    """Mutant: `.request_id` is None when the id was generated by publish_batch."""
    route = rt.open_route(h)
    spy.write_answer = lambda method, params, rid: rejected("busy")
    with pytest.raises(rt.OwnerUnavailable) as e:
        rt.publish_batch(route, items_for(pubm, ["a"]))
    assert e.value.request_id == spy.writes[-1][2] and e.value.request_id


def test_c5_flag_off_from_the_owner_is_owner_flag_off_distinct_from_the_others(rt, h, spy, pubm):
    """The owner's route-integrity refusal. Mutant: it is mapped to OwnerUnavailable, or to RouteChanged."""
    route = rt.open_route(h)
    spy.write_answer = lambda method, params, rid: rejected("flag_off", detail=f"{FLAG} is not on")
    with pytest.raises(rt.OwnerFlagOff) as e:
        rt.publish_batch(route, items_for(pubm, ["a"]), request_id="r1")
    assert not isinstance(e.value, (rt.OwnerUnavailable, rt.RouteChanged))
    assert isinstance(e.value, rt.RouteError) and e.value.results == []
    assert FLAG in str(e.value)
    assert spy.connects == []


def test_c5_flag_off_is_decided_by_the_reason_not_by_message_text(rt, h, spy, pubm):
    """A refusal whose TEXT mentions flag_off and the flag but whose reason is something else stays an OwnerUnavailable.
    Mutant: `flag_off` matched by message text anywhere."""
    route = rt.open_route(h)
    spy.write_answer = lambda method, params, rid: rejected(
        "invalid_params", detail="flag_off flag_off engramd.repair_embeddings_via_owner is not on", remedy="flag_off")
    with pytest.raises(rt.OwnerUnavailable) as e:
        rt.publish_batch(route, items_for(pubm, ["a"]), request_id="r2")
    assert not isinstance(e.value, rt.OwnerFlagOff) and e.value.reason == "invalid_params"


def test_c5_the_module_source_never_matches_message_text(rt):
    """Static form of the cell above: no `flag_off` string comparison against a message. Mutant: `"flag_off" in str(exc)`."""
    src = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            text = ast.unparse(node)
            assert not ("str(" in text and "flag_off" in text), text
            assert not ("args" in text and "flag_off" in text), text
    assert "owner_reason" in src


# =====================================================================================================  C7
@requires_linux_owner
def test_c7_parity_direct_and_owner_routes_give_equal_results_and_equal_stores(rt, pubm, twins, serialize_fails_for):
    """Wiring: NOT a monkeypatch. The owner route runs the REAL `_owner_read.write` over the REAL socket
    (`socket_path_for_home(home)`, which is where `EngramdOwner(home)` listens) into the REAL
    `_handle_publish_prepared_embeddings` on a real store from the A2 fixtures (real schema, real vec0). The direct route runs
    the in-process primitive on a twin store. Items: a no-marker success, a marker-clearing success, an unchanged-but-verified
    one, a recipe-changed one (deferred), a not-current one (deferred) and a mirror failure (pending), then a second batch with
    a changed epoch. Mutant: a divergence on either route only (the route's own validation, a dropped field, a reordering)."""
    home_a, home_b = twins
    ids = ["q0", "q1", "q2", "q3", "q6", "q9", "q4"]
    make_store(home_a, ids=tuple(ids), flag="off")
    make_store(home_b, ids=tuple(ids), flag="on")
    marker = json.dumps({MARKER: {"reason": "old", "stage": "strict_refresh", "at": "t"}, "keep": {"k": 1}})
    seeds = {"q0": 1.0, "q1": 2.0, "q2": 7.0, "q3": 3.0, "q6": 5.0, "q9": 8.0, "q4": 4.0}
    ep = pubm.EmbeddingEpoch(MODEL, 384, "model-default")
    batch1_ids = ["q0", "q1", "q2", "q3", "q6", "q9"]
    items1 = [(n, pubm.PreparedVector(vector=vec(seeds[n]), text_sha256=recipe_sha(home_a, n), epoch=ep)) for n in batch1_ids]
    items2 = [("q4", pubm.PreparedVector(vector=vec(seeds["q4"]), text_sha256=recipe_sha(home_a, "q4"),
                                         epoch=pubm.EmbeddingEpoch("other-model", 384, "model-default")))]
    for h_ in (home_a, home_b):
        sql(h_, f"UPDATE nodes SET metadata = '{marker}' WHERE id = 'q1'")
        sql(h_, "UPDATE nodes SET recall_summary = 'changed later' WHERE id = 'q3'")
        sql(h_, "UPDATE nodes SET is_current = 0 WHERE id = 'q6'")
    serialize_fails_for(8.0)
    o = start(home_b)
    try:
        route_a = rt.open_route(home_a)
        route_b = rt.open_route(home_b)
        assert (route_a.kind, route_b.kind) == ("direct", "owner")
        compared = 0
        seen_results = []
        for items in (items1, items2):
            core._configure_paths(home_a)
            got_a = rt.publish_batch(route_a, items)
            got_b = rt.publish_batch(route_b, items)
            assert got_a == got_b, (got_a, got_b)
            compared += len(got_a)
            seen_results += got_a
        assert compared == 7
        assert {r.status for r in seen_results} == {"refreshed", "deferred", "pending"}
        assert _normalise(all_state(home_a, ids)) == _normalise(all_state(home_b, ids))
        final = {n: state(home_b, n) for n in ids}
        assert any(MARKER in (m or "") for _e, m, _v in final.values())      # the mirror-failure row carries its marker
        assert MARKER not in (final["q1"][1] or "") and '"keep"' in final["q1"][1]  # the debt cleared, the other key kept
    finally:
        o.stop()


@requires_linux_owner
def test_c7_the_statuses_cover_refreshed_pending_and_deferred_on_both_routes(rt, pubm, twins, serialize_fails_for):
    """The parity cell must not pass on a table that never produced a pending or a deferred row. Mutant: a fixture that
    stops producing one of the three statuses."""
    home_a, home_b = twins
    ids = ["q0", "q3", "q9"]
    make_store(home_a, ids=tuple(ids), flag="off")
    make_store(home_b, ids=tuple(ids), flag="on")
    ep = pubm.EmbeddingEpoch(MODEL, 384, "model-default")
    items = [(n, pubm.PreparedVector(vector=vec(s), text_sha256=recipe_sha(home_a, n), epoch=ep))
             for n, s in (("q0", 1.0), ("q3", 3.0), ("q9", 8.0))]
    for h_ in (home_a, home_b):
        sql(h_, "UPDATE nodes SET recall_summary = 'changed later' WHERE id = 'q3'")
    serialize_fails_for(8.0)
    o = start(home_b)
    try:
        core._configure_paths(home_a)
        a = rt.publish_batch(rt.open_route(home_a), items)
        b = rt.publish_batch(rt.open_route(home_b), items)
        assert [r.status for r in a] == [r.status for r in b] == ["refreshed", "deferred", "pending"]
    finally:
        o.stop()


# =====================================================================================================  C8
def test_c8_the_flag_is_read_only_through_flag_applies():
    """The module names no other flag reader: no `flag_on`, no `engramd_owner_flags`, no config.json path of its own, and
    `flag_applies` is called from exactly open_route and publish_batch. Mutant: a private flag read added anywhere."""
    src = MODULE_PATH.read_text(encoding="utf-8")
    assert "flag_on" not in src and "engramd_owner_flags" not in src and "config.json" not in src
    tree = ast.parse(src)
    callers = set()
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "flag_applies":
                callers.add(fn.name)
    assert callers == {"open_route", "publish_batch"} or callers <= {"open_route", "publish_batch", "_flag_now"}
    assert callers


def test_c8_the_module_is_tiered_essential_beside_its_sibling():
    """Mutant: the module ships untiered, or at a lower tier than the helper it imports."""
    manifest = json.loads((REPO_ROOT / "src" / "build" / "packaging" / "tiers.json").read_text(encoding="utf-8"))
    tiers = {m["path"]: m["tier"] for m in manifest["mechanisms"]}
    assert tiers.get("tools/_embedding_repair_route.py") == tiers.get("tools/_owner_read.py") == "essential"
