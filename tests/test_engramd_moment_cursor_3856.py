"""Contract cells for the owner's second write, the typed `advance_moment_cursor` op (#3856, U13 of #2652, phase 2).

Written BEFORE the implementation (contract-first; project CLAUDE.md). Wire:

  write("advance_moment_cursor", {"person_id": str, "from_cursor": int})
    -> Ok(result={"applied": bool, "cursor": int | None, "reason": None | "stale" | "not_person" | "not_object"})
    -> Rejected(reason=<one NAMED reason per refusal>)

NO request_id and no dedupe store: the shared compare-and-set makes a replay harmless (a retry after COMMIT
finds from + 1 and answers `stale` with that value).

The owner runs the SHARED `engram_moment_cursor.advance_moment_cursor` on its own connection under
`_owner_lock`, BEGIN IMMEDIATE, with an op-scoped RESTRICTIVE authorizer (column-exact: ONE UPDATE of
`nodes.metadata`, reads of id/type/is_current/metadata, the four json functions, BEGIN/COMMIT/ROLLBACK), COMMITs
only when `applied`, then re-reads the cursor from the COMMITTED store (design 5.4).

Where this does NOT carry over from U11's use_reward: trigger disjointness is false here by construction (the
metadata JSON guard trigger fires on EVERY metadata update), so the property cell is a named allow-set over
`sqlite_master`, not a disjointness check.

Self-contained: the store is built with the real schema under tmp; nothing touches ~/.engram.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
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

import engram_core  # noqa: E402
import engram_moment_cursor as emc  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

# helpers reused from the phase-1 file and from U11's file (not copied)
from test_moment_cursor_3856 import direct_write, make_store as _make_scratch_store, put, raw_meta, whole_row  # noqa: E402
from test_engramd_use_reward_3822 import (  # noqa: E402
    _FLAT_MODULES, _hold_write_lock, _release, _release_later, norm,
)

LO, HI = -(2 ** 53), 2 ** 53  # [LO, HI)
MOMENTS = [{"description": f"Moment {i}."} for i in range(3)]
PN_A_META = {"name": "石磊", "aliases": ["Léa"], "nested": {"k": [1, {"z": None}]}, "special_moments": MOMENTS,
             "special_moment_cursor": 2}

OP = "advance_moment_cursor"


def make_store(home: Path) -> Path:
    db = _make_scratch_store(home)
    put(db, "pn_a", json.dumps(PN_A_META))
    put(db, "pn_b", json.dumps({"name": "B"}))                      # cursor absent
    put(db, "pn_c", json.dumps({"name": "C", "special_moment_cursor": 7}))
    put(db, "pn_true", '{"special_moment_cursor": true}')
    put(db, "ob_1", json.dumps({"special_moment_cursor": 2}), type_="observation_factual")
    put(db, "pn_old", json.dumps({"special_moment_cursor": 2}), is_current=0)
    put(db, "pn_arr", "[1, 2]")
    put(db, "pn_null", None)
    put(db, "pn_str", '"s"')
    put(db, "pn_num", "5")
    return db


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="emc-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _start(home, **kw):
    owner = eo.EngramdOwner(home, **kw)
    owner.start(install_signal_handlers=False)
    return owner


@pytest.fixture
def owner(home):
    make_store(home)
    o = _start(home)
    try:
        yield o
    finally:
        o.stop()


def call(owner, person_id, from_cursor, *, params=None, timeout=3.0):
    if params is None:
        params = {"person_id": person_id, "from_cursor": from_cursor}
    return ec.write(OP, params, timeout=timeout, socket_path=owner.socket_path)


def ok_result(res):
    assert isinstance(res, ec.Ok), res
    return res.result


def tracing(owner):
    stmts = []
    owner._owner_conn.set_trace_callback(lambda s: stmts.append(norm(s)))
    return stmts


def cursor_of(db, nid):
    return json.loads(raw_meta(db, nid)).get("special_moment_cursor")


# --- constants, the kind registry, the version ----------------------------------------------------------


def test_write_set_is_exactly_the_two_ops_and_the_version_is_1_3():
    assert eo.WRITE_METHODS == frozenset({"use_reward", "advance_moment_cursor"})
    assert ec.CONTRACT_VERSION == "1.3"
    assert eo.METHOD_KINDS == frozenset({"read", "write"})


def test_named_constants_are_pinned():
    assert eo.MOMENT_CURSOR_PERSON_ID_MAX_BYTES == 256
    assert eo.MOMENT_CURSOR_ADMITTED_TRIGGERS == frozenset({"nodes_metadata_json_guard_update"})
    assert eo.MOMENT_CURSOR_READ_COLUMNS == frozenset({"id", "type", "is_current", "metadata"})
    assert eo.MOMENT_CURSOR_UPDATE_COLUMNS == frozenset({"metadata"})
    assert eo.MOMENT_CURSOR_FUNCTIONS == frozenset({"json_set", "json_type", "json_extract", "json_valid"})


@requires_linux_owner
def test_the_new_method_is_in_the_table_kind_write_and_the_read_set_is_unchanged(owner):
    assert owner._method_kinds[OP] == "write"
    assert OP in owner._methods
    assert {m for m, k in owner._method_kinds.items() if k == "write"} == set(eo.WRITE_METHODS)
    reads = {m for m, k in owner._method_kinds.items() if k == "read"}
    assert reads == {"ping", "snapshot", "read", "df", "chain", "confidence_by_type"}


@requires_linux_owner
def test_the_op_declared_as_a_read_is_refused_and_nothing_runs(owner):
    owner._method_kinds[OP] = "read"
    before = whole_row(owner.store_path, "pn_a")
    res = call(owner, "pn_a", 2)
    assert isinstance(res, ec.Rejected) and res.reason == "method_kind_mismatch", res
    assert whole_row(owner.store_path, "pn_a") == before


@requires_linux_owner
@pytest.mark.parametrize("kind", [None, "", "READ", "admin", 7])
def test_an_unclassified_method_is_still_default_deny(owner, kind):
    called = []
    owner._methods["dummy_unclassified"] = lambda p, d: called.append(1) or {"status": "ok", "result": {}}
    if kind is not None:
        owner._method_kinds["dummy_unclassified"] = kind
    res = ec.write("dummy_unclassified", {}, socket_path=owner.socket_path, timeout=3.0)
    assert isinstance(res, ec.Rejected) and res.reason == "method_kind_missing"
    assert called == []


def test_minor_skew_is_tolerated_both_ways():
    assert ec.check_contract_version("1.2", owner="1.3") is None
    assert ec.check_contract_version("1.3", owner="1.2") is None
    assert ec.check_contract_version("2.0", owner="1.3").reason == "contract_version"


@requires_linux_owner
def test_ping_reports_contract_1_3(owner):
    r = ec.read("ping", {}, socket_path=owner.socket_path, timeout=3.0)
    assert isinstance(r, ec.Ok) and r.result["contract_version"] == "1.3"
    assert owner.contract_version == "1.3"


@requires_linux_owner
def test_skew_an_older_owner_without_the_method_answers_unknown_operation_typed(owner):
    """Simulate a 1.2 owner: the method is gone from the table. The client returns it typed (no crash, no
    fallback, no Ok); phase 3's hook makes it loud."""
    del owner._methods[OP]
    owner._method_kinds.pop(OP, None)
    before = whole_row(owner.store_path, "pn_a")
    res = call(owner, "pn_a", 2)
    assert isinstance(res, ec.Rejected) and res.reason == "unknown_operation", res
    assert OP not in res.remedy
    assert whole_row(owner.store_path, "pn_a") == before


# --- parameter refusals: each its own named reason, nothing executed -------------------------------------


def _refusal_cases():
    good = {"person_id": "pn_a", "from_cursor": 2}

    def w(**kw):
        p = dict(good)
        p.update(kw)
        return p

    def wo(key):
        p = dict(good)
        del p[key]
        return p

    p = "advance_moment_cursor_"
    return [
        ("not_a_dict", ["pn_a", 2], p + "params_shape"),
        ("extra_request_id", w(request_id="r1"), p + "params_shape"),
        ("extra_sql", w(sql="UPDATE nodes SET claim=1"), p + "params_shape"),
        ("extra_to_cursor", w(to_cursor=9), p + "params_shape"),
        ("missing_from_cursor", wo("from_cursor"), p + "params_shape"),
        ("missing_person_id", wo("person_id"), p + "params_shape"),
        ("empty_dict", {}, p + "params_shape"),
        ("person_id_none", w(person_id=None), p + "person_id_not_str"),
        ("person_id_int", w(person_id=5), p + "person_id_not_str"),
        ("person_id_list", w(person_id=["pn_a"]), p + "person_id_not_str"),
        ("person_id_empty", w(person_id=""), p + "person_id_empty"),
        ("person_id_too_long", w(person_id="a" * 257), p + "person_id_too_long"),
        ("person_id_multibyte_too_long", w(person_id="é" * 129), p + "person_id_too_long"),
        ("person_id_lone_surrogate", w(person_id="pn_\ud800"), p + "person_id_not_utf8"),
        ("from_true", w(from_cursor=True), p + "from_cursor_not_int"),
        ("from_false", w(from_cursor=False), p + "from_cursor_not_int"),
        ("from_float_1_0", w(from_cursor=1.0), p + "from_cursor_not_int"),
        ("from_float_1_5", w(from_cursor=1.5), p + "from_cursor_not_int"),
        ("from_string", w(from_cursor="3"), p + "from_cursor_not_int"),
        ("from_null", w(from_cursor=None), p + "from_cursor_not_int"),
        ("from_list", w(from_cursor=[1]), p + "from_cursor_not_int"),
        ("from_dict", w(from_cursor={"a": 1}), p + "from_cursor_not_int"),
        ("from_2_53", w(from_cursor=2 ** 53), p + "from_cursor_out_of_range"),
        ("from_below_min", w(from_cursor=-(2 ** 53) - 1), p + "from_cursor_out_of_range"),
        ("from_2_63", w(from_cursor=2 ** 63), p + "from_cursor_out_of_range"),
    ]


@requires_linux_owner
@pytest.mark.parametrize("label,params,reason", _refusal_cases(), ids=[c[0] for c in _refusal_cases()])
def test_every_hostile_param_is_refused_by_name_with_no_statement_executed(owner, label, params, reason):
    before = [whole_row(owner.store_path, n) for n in ("pn_a", "pn_b")]
    stmts = tracing(owner)
    res = ec.write(OP, params, timeout=3.0, socket_path=owner.socket_path)
    assert isinstance(res, ec.Rejected), res
    assert res.reason == reason, (label, res)
    assert stmts == [], stmts                          # zero statements: not even a BEGIN
    assert owner._owner_conn.in_transaction is False
    assert [whole_row(owner.store_path, n) for n in ("pn_a", "pn_b")] == before
    blob = res.detail + res.remedy
    for needle in ("UPDATE nodes", "pn_a"):
        assert needle not in blob


@requires_linux_owner
def test_a_supplied_request_id_is_refused_as_an_unknown_param_and_there_is_no_dedupe_store(owner):
    res = call(owner, "pn_a", 2, params={"person_id": "pn_a", "from_cursor": 2, "request_id": "r-1"})
    assert isinstance(res, ec.Rejected) and res.reason == "advance_moment_cursor_params_shape", res
    assert cursor_of(owner.store_path, "pn_a") == 2
    assert not [a for a in vars(owner) if "moment" in a and "seen" in a]


@requires_linux_owner
@pytest.mark.parametrize("edge", [LO, HI - 1])
def test_the_range_edges_are_accepted(home, edge):
    db = make_store(home)
    put(db, "pn_edge", json.dumps({"special_moment_cursor": edge}))
    o = _start(home)
    try:
        r = ok_result(call(o, "pn_edge", edge))
        assert r == {"applied": True, "cursor": edge + 1, "reason": None}
    finally:
        o.stop()


# --- semantic cells on the real owner thread with the real client ------------------------------------------


@requires_linux_owner
def test_applied_advance_changes_only_the_cursor(owner):
    before = json.loads(raw_meta(owner.store_path, "pn_a"))
    r = ok_result(call(owner, "pn_a", 2))
    assert r == {"applied": True, "cursor": 3, "reason": None}
    after = json.loads(raw_meta(owner.store_path, "pn_a"))
    assert after["special_moment_cursor"] == 3
    assert [k for k in before if k != "special_moment_cursor"] == [k for k in after if k != "special_moment_cursor"]
    assert all(after[k] == before[k] for k in before if k != "special_moment_cursor")
    assert owner._owner_conn.in_transaction is False


@requires_linux_owner
def test_absent_cursor_and_stored_true_both_read_as_zero(owner):
    assert ok_result(call(owner, "pn_b", 0)) == {"applied": True, "cursor": 1, "reason": None}
    assert ok_result(call(owner, "pn_true", 0)) == {"applied": True, "cursor": 1, "reason": None}


@requires_linux_owner
def test_stale_returns_the_current_committed_value_and_commits_nothing(owner):
    before = whole_row(owner.store_path, "pn_a")
    r = ok_result(call(owner, "pn_a", 1))
    assert r == {"applied": False, "cursor": 2, "reason": "stale"}
    assert whole_row(owner.store_path, "pn_a") == before


@requires_linux_owner
@pytest.mark.parametrize("nid,reason", [
    ("ob_1", "not_person"), ("nope", "not_person"), ("pn_old", "not_person"),
    ("pn_arr", "not_object"), ("pn_null", "not_object"), ("pn_str", "not_object"), ("pn_num", "not_object"),
])
def test_refusals_by_the_shared_function_are_typed_answers_and_the_row_is_unchanged(owner, nid, reason):
    """not_object rows are sent from=0, the cursor value that WOULD match (a non-object blob reads 0); sending 2
    never matches with or without the object test and would not discriminate."""
    before = whole_row(owner.store_path, nid)
    r = ok_result(call(owner, nid, 0 if reason == "not_object" else 2))
    assert r == {"applied": False, "cursor": None, "reason": reason}
    assert whole_row(owner.store_path, nid) == before
    assert owner._owner_conn.in_transaction is False


@requires_linux_owner
@pytest.mark.parametrize("text", ["{not json", '{"a":007}'])
def test_a_malformed_text_row_answers_not_object_through_the_owner_without_raising(home, text):
    """The hardened statement never reaches json_type / json_extract on an invalid blob: a legacy malformed row is
    a typed not_object ANSWER through the owner too, row byte-unchanged (sent the cursor 0 that WOULD match)."""
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TRIGGER nodes_metadata_json_guard_update")
    conn.execute("DROP TRIGGER nodes_metadata_json_guard_insert")
    conn.commit()
    conn.close()
    put(db, "pn_bad", text)
    before = whole_row(db, "pn_bad")
    o = _start(home)
    try:
        r = ok_result(call(o, "pn_bad", 0))
    finally:
        o.stop()
    assert r == {"applied": False, "cursor": None, "reason": "not_object"}
    assert whole_row(db, "pn_bad") == before


@requires_linux_owner
def test_concurrent_field_survives_through_the_owner(owner):
    """A direct writer edits `pronouns` between the caller's read and the owner call."""
    frm = json.loads(raw_meta(owner.store_path, "pn_a"))["special_moment_cursor"]      # the caller's read
    direct_write(owner.store_path, "pn_a", "pronouns", "they/them")                      # lands in between
    r = ok_result(call(owner, "pn_a", frm))
    assert r["applied"] is True and r["cursor"] == frm + 1
    after = json.loads(raw_meta(owner.store_path, "pn_a"))
    assert after["pronouns"] == "they/them" and after["special_moment_cursor"] == frm + 1


@requires_linux_owner
def test_two_concurrent_callers_with_the_same_from_exactly_one_applies(owner):
    barrier = threading.Barrier(2)
    out = []

    def go():
        barrier.wait()
        out.append(call(owner, "pn_a", 2, timeout=5.0))

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    results = [ok_result(r) for r in out]
    assert sorted(r["applied"] for r in results) == [False, True]
    assert {r["cursor"] for r in results} == {3}
    assert [r["reason"] for r in results if not r["applied"]] == ["stale"]
    assert cursor_of(owner.store_path, "pn_a") == 3


@requires_linux_owner
def test_a_client_timeout_on_a_write_is_indeterminate_and_the_one_retry_answers_stale_with_from_plus_one(owner):
    """The op committed, the reply is lost: Unavailable(timeout). The ONE retry with the SAME from_cursor finds
    from + 1 and answers `stale` -- the replay is harmless, with no dedupe store."""
    real_handler = owner._handle_advance_moment_cursor
    stalls = []

    def commit_then_stall(params, deadline):
        out = real_handler(params, deadline)
        if not stalls:
            stalls.append(1)
            time.sleep(1.0)
        return out

    owner._methods[OP] = commit_then_stall
    first = call(owner, "pn_a", 2, timeout=0.4)
    assert isinstance(first, ec.Unavailable) and first.reason == "timeout", first
    assert cursor_of(owner.store_path, "pn_a") == 3                      # it DID commit
    time.sleep(1.0)
    again = ok_result(call(owner, "pn_a", 2))
    assert again == {"applied": False, "cursor": 3, "reason": "stale"}   # == from + 1
    assert cursor_of(owner.store_path, "pn_a") == 3                      # advanced once


# --- the shared function is what runs (and it is not committed on stale) ------------------------------------


@requires_linux_owner
def test_statement_trace_applied_commits_then_reads_back_and_stale_rolls_back(owner):
    stmts = tracing(owner)
    ok_result(call(owner, "pn_a", 2))
    assert "COMMIT" in stmts and "ROLLBACK" not in stmts, stmts
    after_commit = stmts[stmts.index("COMMIT") + 1:]
    assert any(s.startswith("SELECT") for s in after_commit), stmts          # the read-back is after COMMIT
    ups = {s for s in stmts if s.startswith("UPDATE")}
    assert len(ups) == 1, ups
    assert re.fullmatch(r"UPDATE nodes SET metadata = json_set\(metadata, \?, \? \+ \?\) WHERE id = \? AND type = \? "
                        r"AND is_current = \? AND .*", next(iter(ups))), ups
    stmts.clear()
    ok_result(call(owner, "pn_a", 2))                                          # now stale
    assert "ROLLBACK" in stmts and "COMMIT" not in stmts, stmts
    assert not [s for s in stmts if s.startswith(("INSERT", "DELETE"))]
    assert {s for s in stmts if s.startswith("UPDATE")} == ups


@requires_linux_owner
def test_a_plain_refusal_opens_no_transaction_and_a_not_person_rolls_back(owner):
    stmts = tracing(owner)
    ok_result(call(owner, "ob_1", 2))
    assert "COMMIT" not in stmts and "ROLLBACK" in stmts, stmts


# --- the authorizer (the part that does NOT carry over unchanged from U11) ---------------------------------

D = sqlite3
MEASURED_TUPLES = {
    # (action, arg1, arg2, db_name, trigger_or_view) from real runs of the shared function on the real schema:
    # applied, stale, not_person, not_object, and the read-back SELECT (the trace under the plain recorder)
    (D.SQLITE_TRANSACTION, "BEGIN", None, None, None),
    (D.SQLITE_FUNCTION, None, "json_set", None, None),
    (D.SQLITE_READ, "nodes", "metadata", "main", None),
    (D.SQLITE_UPDATE, "nodes", "metadata", "main", None),
    (D.SQLITE_READ, "nodes", "id", "main", None),
    (D.SQLITE_READ, "nodes", "type", "main", None),
    (D.SQLITE_READ, "nodes", "is_current", "main", None),
    (D.SQLITE_FUNCTION, None, "json_valid", None, None),
    (D.SQLITE_FUNCTION, None, "json_type", None, None),
    (D.SQLITE_FUNCTION, None, "json_extract", None, None),
    # the guard trigger's WHEN clause and body (BEFORE UPDATE OF metadata): a READ of NEW.metadata, json_valid,
    # a SELECT (the RAISE). RAISE(ABORT) itself needs NO authorization code (measured: an invalid write shows
    # nothing beyond these three).
    (D.SQLITE_READ, "nodes", "metadata", "main", "nodes_metadata_json_guard_update"),
    (D.SQLITE_FUNCTION, None, "json_valid", None, "nodes_metadata_json_guard_update"),
    (D.SQLITE_SELECT, None, None, None, "nodes_metadata_json_guard_update"),
    (D.SQLITE_SELECT, None, None, None, None),
    (D.SQLITE_TRANSACTION, "COMMIT", None, None, None),
}


def _measure(home: Path):
    db = make_store(home)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    calls = []

    def rec(*a):
        calls.append(tuple(a))
        return sqlite3.SQLITE_OK

    conn.set_authorizer(rec)
    for nid, frm in (("pn_a", 2), ("pn_a", 2), ("ob_1", 2), ("pn_arr", 0)):
        conn.execute("BEGIN IMMEDIATE")
        emc.advance_moment_cursor(conn, nid, frm)
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("SELECT " + emc.CURSOR_SQL_EXPR + " FROM nodes WHERE id = ? AND type = 'person' AND is_current = 1",
                 ("pn_a",)).fetchone()
    conn.commit()
    conn.close()
    return calls


def test_measured_authorizer_tuples_of_real_runs_equal_the_pinned_set(home):
    calls = _measure(home)
    # ROLLBACK appears only in the owner's own abort path; the plain runs commit.
    assert set(calls) == MEASURED_TUPLES, set(calls) ^ MEASURED_TUPLES


def test_every_measured_tuple_is_allowed_and_nothing_else_is(home):
    for t in MEASURED_TUPLES:
        assert eo._moment_cursor_allows(*t) is True, t
    assert eo._moment_cursor_allows(D.SQLITE_TRANSACTION, "ROLLBACK", None, None, None) is True
    denied = [
        (D.SQLITE_UPDATE, "nodes", "claim", "main", None),
        (D.SQLITE_UPDATE, "nodes", "utility_score", "main", None),
        (D.SQLITE_UPDATE, "nodes", "metadata", "temp", None),
        (D.SQLITE_UPDATE, "edges", "metadata", "main", None),
        (D.SQLITE_UPDATE, "nodes", "metadata", "main", "nodes_fts_rewrite_resync"),
        (D.SQLITE_READ, "nodes", "claim", "main", None),
        (D.SQLITE_READ, "nodes", "recall_summary", "main", None),
        (D.SQLITE_READ, "edges", "id", "main", None),
        (D.SQLITE_READ, "nodes", "id", "temp", None),
        (D.SQLITE_READ, "nodes", "metadata", "main", "nodes_fts_rewrite_resync"),
        (D.SQLITE_INSERT, "nodes", None, "main", None),
        (D.SQLITE_INSERT, "nodes_fts", None, "main", "nodes_metadata_json_guard_update"),
        (D.SQLITE_DELETE, "nodes", None, "main", None),
        (D.SQLITE_PRAGMA, "journal_mode", "wal", "main", None),
        (D.SQLITE_PRAGMA, "busy_timeout", None, None, None),
        (D.SQLITE_ATTACH, "/tmp/x", None, None, None),
        (D.SQLITE_CREATE_TABLE, "t", None, "main", None),
        (D.SQLITE_DROP_TABLE, "t", None, "main", None),
        (D.SQLITE_FUNCTION, None, "load_extension", None, None),
        (D.SQLITE_FUNCTION, None, "json_remove", None, None),
        (D.SQLITE_FUNCTION, None, "json_insert", None, None),
        (D.SQLITE_FUNCTION, None, "json_set", None, "some_unadmitted_trigger"),
        (D.SQLITE_SAVEPOINT, "BEGIN", "some_savepoint", None, None),
        (D.SQLITE_TRANSACTION, "SOMETHING", None, None, None),
        (D.SQLITE_RECURSIVE, None, None, None, None),
        (D.SQLITE_SELECT, None, None, None, "some_unadmitted_trigger"),
    ]
    for t in denied:
        assert eo._moment_cursor_allows(*t) is False, t


@requires_linux_owner
def test_live_owner_run_every_authorized_action_is_in_the_named_list(owner, monkeypatch):
    """The authority is the trace over a REAL owner run: record every tuple the op authorizer is asked about
    (delegating to the real decision) while a real client drives applied, stale, not_person and not_object."""
    asked = []
    real = eo._UseRewardGuard.authorizer

    def recording(self, *a):
        asked.append((tuple(a), real(self, *a)))
        return asked[-1][1]

    monkeypatch.setattr(eo._UseRewardGuard, "authorizer", recording)
    for nid, frm in (("pn_a", 2), ("pn_a", 2), ("ob_1", 2), ("pn_arr", 0)):
        ok_result(call(owner, nid, frm))
    ok_tuples = {t for t, verdict in asked if verdict == sqlite3.SQLITE_OK}
    assert not [t for t, verdict in asked if verdict != sqlite3.SQLITE_OK], "something was denied in a clean run"
    allowed = MEASURED_TUPLES | {(D.SQLITE_TRANSACTION, "ROLLBACK", None, None, None)}
    assert ok_tuples <= allowed, ok_tuples - allowed
    assert (D.SQLITE_TRANSACTION, "COMMIT", None, None, None) in ok_tuples
    assert (D.SQLITE_TRANSACTION, "ROLLBACK", None, None, None) in ok_tuples
    assert (D.SQLITE_FUNCTION, None, "json_valid", None, "nodes_metadata_json_guard_update") in ok_tuples


@requires_linux_owner
@pytest.mark.parametrize("bad_sql", [
    "UPDATE nodes SET claim = 'pwned' WHERE id = 'pn_a'",
    "UPDATE nodes SET importance_base = 9 WHERE id = 'pn_a'",
    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES ('a','b','r','t')",
    "DELETE FROM edges",
    "PRAGMA journal_mode=DELETE",
    "CREATE TABLE evil (x)",
    "SELECT claim FROM nodes",
    "SELECT load_extension('x')",
])
def test_a_denied_statement_refuses_the_whole_op_rolled_back(owner, monkeypatch, bad_sql):
    """REQUIRED mutation cell: the shared function edited to also run a statement outside the allowlist."""
    real = emc.advance_moment_cursor

    def evil(conn, pid, frm):
        out = real(conn, pid, frm)
        try:
            conn.execute(bad_sql)
        except sqlite3.Error:
            pass
        return out

    monkeypatch.setattr(emc, "advance_moment_cursor", evil)
    before = whole_row(owner.store_path, "pn_a")
    res = call(owner, "pn_a", 2)
    assert isinstance(res, ec.Rejected) and res.reason == "write_denied", res
    assert whole_row(owner.store_path, "pn_a") == before          # the advance itself rolled back
    assert owner._owner_conn.in_transaction is False


@requires_linux_owner
def test_a_commit_issued_from_inside_the_shared_function_is_denied_not_a_partial_commit(owner, monkeypatch):
    real = emc.advance_moment_cursor

    def committing(conn, pid, frm):
        out = real(conn, pid, frm)
        conn.commit()
        return out

    monkeypatch.setattr(emc, "advance_moment_cursor", committing)
    before = whole_row(owner.store_path, "pn_a")
    res = call(owner, "pn_a", 2)
    assert isinstance(res, ec.Rejected) and res.reason == "write_denied", res
    assert whole_row(owner.store_path, "pn_a") == before


@requires_linux_owner
def test_without_json_valid_in_the_allowlist_the_real_advance_fails_closed(owner, monkeypatch):
    """(iii) json_valid arrives through the guard trigger's WHEN clause and body. Drop it from the allowlist and
    the real advance is DENIED (write_denied), rolled back, never silently run."""
    monkeypatch.setattr(eo, "MOMENT_CURSOR_FUNCTIONS", frozenset({"json_set", "json_type", "json_extract"}))
    before = whole_row(owner.store_path, "pn_a")
    res = call(owner, "pn_a", 2)
    assert isinstance(res, ec.Rejected) and res.reason == "write_denied", res
    assert whole_row(owner.store_path, "pn_a") == before


def test_the_guard_trigger_still_protects_under_the_op_allowlist(home):
    """(iii) With the op's authorizer installed, an invalid-JSON write is stopped by the TRIGGER (its RAISE),
    not by an authorization denial; the trigger's json_valid call is authorized; a valid write passes."""
    db = make_store(home)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    guard = eo._UseRewardGuard(allows=eo._moment_cursor_allows)
    conn.set_authorizer(guard.authorizer)
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="valid JSON"):
        conn.execute("UPDATE nodes SET metadata = '{bad' WHERE id = 'pn_a'")
    assert guard.denials == []
    conn.execute("UPDATE nodes SET metadata = '{\"ok\": 1}' WHERE id = 'pn_a'")
    assert guard.denials == []
    guard.allow_rollback = True          # the rollback is the handler's own phase
    conn.rollback()
    conn.close()


# --- the PROPERTY cell (in place of U11's trigger disjointness) --------------------------------------------

_TRIGGER_HEAD = re.compile(
    r"CREATE\s+(?:TEMP\s+|TEMPORARY\s+)?TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+\s+"
    r"(?:BEFORE\s+|AFTER\s+|INSTEAD\s+OF\s+)?(INSERT|DELETE|UPDATE)(?:\s+OF\s+(.*?))?\s+ON\s+", re.I | re.S)


def triggers_firing_on_metadata_update(conn) -> set:
    """Parse the live schema: every trigger on `nodes` that can fire on `UPDATE nodes SET metadata` (an UPDATE
    trigger with no OF list, or an OF list containing `metadata`). Conservative: ignores WHEN."""
    fires = set()
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='nodes'"):
        m = _TRIGGER_HEAD.match(sql)
        assert m, f"could not parse trigger {name}"
        event, cols = m.group(1).upper(), m.group(2)
        if event != "UPDATE":
            continue
        if cols is None or "metadata" in [c.strip().strip('"`[]').lower() for c in cols.split(",")]:
            fires.add(name)
    return fires


def empirically_firing_on_metadata_update(conn) -> set:
    """Ground truth: prepare `UPDATE nodes SET metadata = ...` under a recording authorizer; the 5th argument
    names every trigger whose body is compiled into it."""
    seen = set()

    def rec(action, a1, a2, db, src):
        if src:
            seen.add(src)
        return sqlite3.SQLITE_OK

    conn.set_authorizer(rec)
    try:
        conn.execute("UPDATE nodes SET metadata = '{}' WHERE id = 'no-such-id'")
    finally:
        conn.set_authorizer(None)
    return seen


def test_property_the_real_schema_has_exactly_the_named_trigger_on_metadata_updates(home):
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    try:
        parsed = triggers_firing_on_metadata_update(conn)
        assert parsed == {"nodes_metadata_json_guard_update"}
        assert parsed == set(eo.MOMENT_CURSOR_ADMITTED_TRIGGERS)
        # the parse agrees with the ground truth (so the cell cannot pass by mis-parsing the dynamic FTS trigger)
        assert empirically_firing_on_metadata_update(conn) == parsed
        # the dynamically built FTS resync trigger fires on its column list, which does not include metadata
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name='nodes_fts_rewrite_resync'").fetchone()[0]
        assert "metadata" not in row.lower().split(" on nodes")[0]
    finally:
        conn.close()


@pytest.mark.parametrize("ddl,name", [
    ("CREATE TRIGGER t_any AFTER UPDATE ON nodes BEGIN SELECT 1; END", "t_any"),
    ("CREATE TRIGGER t_list AFTER UPDATE OF claim, metadata ON nodes BEGIN SELECT 1; END", "t_list"),
    ('CREATE TRIGGER t_quoted BEFORE UPDATE OF "metadata" ON nodes BEGIN SELECT 1; END', "t_quoted"),
    ("CREATE TRIGGER t_default UPDATE OF Metadata ON nodes BEGIN SELECT 1; END", "t_default"),
])
def test_property_a_new_trigger_on_metadata_turns_the_cell_red(home, ddl, name):
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(ddl)
        parsed = triggers_firing_on_metadata_update(conn)
        assert name in parsed
        assert parsed != set(eo.MOMENT_CURSOR_ADMITTED_TRIGGERS)       # the property cell would be red
        assert empirically_firing_on_metadata_update(conn) == parsed   # and the parse is still the ground truth
    finally:
        conn.close()


@pytest.mark.parametrize("ddl", [
    "CREATE TRIGGER t_other AFTER UPDATE OF claim ON nodes BEGIN SELECT 1; END",
    "CREATE TRIGGER t_ins AFTER INSERT ON nodes BEGIN SELECT 1; END",
    "CREATE TRIGGER t_del AFTER DELETE ON nodes BEGIN SELECT 1; END",
])
def test_property_a_trigger_on_other_events_or_columns_does_not_turn_it_red(home, ddl):
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(ddl)
        parsed = triggers_firing_on_metadata_update(conn)
        assert parsed == set(eo.MOMENT_CURSOR_ADMITTED_TRIGGERS)
        assert empirically_firing_on_metadata_update(conn) == parsed
    finally:
        conn.close()


@requires_linux_owner
def test_a_surprise_trigger_on_metadata_is_denied_by_name_and_the_op_is_refused(owner):
    owner._owner_conn.execute("CREATE TRIGGER t_surprise AFTER UPDATE OF metadata ON nodes BEGIN SELECT 1; END")
    owner._owner_conn.commit()
    before = whole_row(owner.store_path, "pn_a")
    res = call(owner, "pn_a", 2)
    assert isinstance(res, ec.Rejected) and res.reason == "write_denied", res
    assert whole_row(owner.store_path, "pn_a") == before


# --- authorizer / busy_timeout restoration, busy, displacement, transaction hygiene ---------------------


@requires_linux_owner
def test_the_authorizer_and_busy_timeout_are_restored_after_ok_denied_and_raised_exits(owner, monkeypatch, tmp_path):
    conn = owner._owner_conn
    conn.execute("PRAGMA busy_timeout = 4321")

    def prior_is_back(tag):
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321, tag
        conn.execute("UPDATE nodes SET claim = claim WHERE id = 'pn_b'")        # the permissive authorizer is back
        conn.rollback()
        r = ec.read("snapshot", {"dest_path": str(tmp_path / f"s-{tag}.db")}, timeout=5.0, socket_path=owner.socket_path)
        assert isinstance(r, ec.Ok), (tag, r)

    ok_result(call(owner, "pn_a", 2))
    prior_is_back("ok")
    real = emc.advance_moment_cursor
    monkeypatch.setattr(emc, "advance_moment_cursor", lambda c, p, f: (real(c, p, f), c.execute("UPDATE nodes SET claim='x'"))[0])
    assert call(owner, "pn_b", 0).reason == "write_denied"
    prior_is_back("denied")

    def raiser(c, p, f):
        real(c, p, f)
        raise RuntimeError("bug mid-op")

    monkeypatch.setattr(emc, "advance_moment_cursor", raiser)
    res = call(owner, "pn_b", 0)
    assert isinstance(res, ec.Rejected) and res.reason == "internal_error", res
    assert conn.in_transaction is False
    prior_is_back("raised")
    monkeypatch.setattr(emc, "advance_moment_cursor", real)
    assert cursor_of(owner.store_path, "pn_b") is None           # only the first (ok) call ever advanced anything


class _Boom(BaseException):
    pass


@requires_linux_owner
def test_a_BaseException_rolls_back_and_the_next_op_does_not_commit_it(owner, monkeypatch):
    real = emc.advance_moment_cursor

    def advance_then_boom(conn, pid, frm):
        real(conn, pid, frm)
        raise _Boom()

    monkeypatch.setattr(emc, "advance_moment_cursor", advance_then_boom)
    with pytest.raises(_Boom):
        owner._handle_advance_moment_cursor({"person_id": "pn_b", "from_cursor": 0}, time.monotonic() + 5.0)
    monkeypatch.setattr(emc, "advance_moment_cursor", real)
    assert owner._owner_conn.in_transaction is False
    owner._owner_conn.execute("UPDATE nodes SET claim = claim WHERE id = 'pn_a'")      # permissive authorizer is back
    owner._owner_conn.rollback()
    assert cursor_of(owner.store_path, "pn_b") is None
    ok_result(call(owner, "pn_c", 7))                                                    # the NEXT op must not commit pn_b's
    assert cursor_of(owner.store_path, "pn_b") is None
    assert ok_result(call(owner, "pn_b", 0))["applied"] is True


@requires_linux_owner
def test_an_op_refuses_to_start_while_the_shared_connection_has_a_transaction_open(owner):
    owner._owner_conn.execute("UPDATE nodes SET claim = claim WHERE id = 'pn_b'")
    assert owner._owner_conn.in_transaction is True
    stmts = tracing(owner)
    try:
        res = call(owner, "pn_a", 2)
        assert isinstance(res, ec.Rejected) and res.reason == "advance_moment_cursor_txn_open", res
        assert stmts == []
        assert owner._owner_conn.in_transaction is True
    finally:
        owner._owner_conn.rollback()
    assert cursor_of(owner.store_path, "pn_a") == 2


@requires_linux_owner
def test_a_locked_store_is_busy_typed_determinate_and_nothing_is_committed(owner, monkeypatch):
    monkeypatch.setattr(eo, "OWNER_WRITE_LOCK_WAIT", 0.3)
    before = whole_row(owner.store_path, "pn_a")
    blocker = _hold_write_lock(owner)
    try:
        res = call(owner, "pn_a", 2)
        assert isinstance(res, ec.Rejected) and res.reason == "busy", res
        assert owner._owner_conn.in_transaction is False
    finally:
        _release(blocker)
    assert whole_row(owner.store_path, "pn_a") == before
    assert ok_result(call(owner, "pn_a", 2))["applied"] is True              # a retry runs fresh


@requires_linux_owner
def test_a_writer_holding_longer_than_the_budget_gets_busy_before_the_client_gives_up(owner):
    blocker = _hold_write_lock(owner)
    timer = _release_later(blocker, 3.5)
    try:
        t0 = time.monotonic()
        res = call(owner, "pn_a", 2, timeout=ec.OWNER_WRITE_CLIENT_TIMEOUT)
        elapsed = time.monotonic() - t0
    finally:
        timer.cancel()
        _release(blocker)
    assert isinstance(res, ec.Rejected) and res.reason == "busy", res        # NOT Unavailable
    assert eo.OWNER_WRITE_LOCK_WAIT * 0.7 <= elapsed < ec.OWNER_WRITE_CLIENT_TIMEOUT - ec.OWNER_WRITE_REPLY_RESERVE + 0.4, elapsed
    assert cursor_of(owner.store_path, "pn_a") == 2


@requires_linux_owner
def test_an_owner_lock_held_past_the_budget_is_busy_too(owner, monkeypatch):
    monkeypatch.setattr(eo, "OWNER_WRITE_LOCK_WAIT", 0.2)
    owner._owner_lock.acquire()
    try:
        assert call(owner, "pn_a", 2).reason == "busy"
    finally:
        owner._owner_lock.release()
    assert cursor_of(owner.store_path, "pn_a") == 2


@requires_linux_owner
def test_displacement_detected_by_the_in_lock_recheck_refuses_and_nothing_is_committed(owner, monkeypatch):
    import engram_walguard
    calls = []

    def second_call_displaced(path):
        calls.append(path)
        if len(calls) >= 2:
            raise engram_walguard.SubstrateDisplaced({"reason": "test"}, "n/a")

    monkeypatch.setattr(engram_walguard, "assert_not_displaced", second_call_displaced)
    before = whole_row(owner.store_path, "pn_a")
    res = call(owner, "pn_a", 2)
    assert isinstance(res, ec.Rejected) and res.reason == "substrate_displaced", res
    assert len(calls) == 2 and owner._displaced is True
    assert whole_row(owner.store_path, "pn_a") == before


@requires_linux_owner
def test_a_displaced_store_refuses_the_write_at_dispatch_nothing_committed(owner, monkeypatch):
    import engram_walguard

    def displaced(path):
        raise engram_walguard.SubstrateDisplaced({"reason": "test"}, "n/a")

    monkeypatch.setattr(engram_walguard, "assert_not_displaced", displaced)
    before = whole_row(owner.store_path, "pn_a")
    stmts = tracing(owner)
    res = call(owner, "pn_a", 2)
    assert isinstance(res, ec.Rejected) and res.reason == "substrate_displaced", res
    assert stmts == []
    assert whole_row(owner.store_path, "pn_a") == before


# --- read-back (design 5.4): after COMMIT, from the committed store -------------------------------------


@requires_linux_owner
def test_a_post_commit_value_that_differs_is_readback_mismatch_committed_unverified(owner):
    """The discriminating cell: the committed cursor is perturbed between COMMIT and the re-read. A read-back
    that is never compared (or never run) cannot see it."""
    real = owner._moment_cursor_readback
    calls = []

    def perturbing(conn, person_id):
        calls.append(1)
        return real(conn, person_id) + 5

    owner._moment_cursor_readback = perturbing
    res = call(owner, "pn_a", 2)
    assert isinstance(res, ec.Rejected) and res.reason == "readback_mismatch", res
    assert res.commit == "committed_unverified"
    assert "COMMITTED but UNVERIFIED" in res.detail
    assert cursor_of(owner.store_path, "pn_a") == 3          # the commit stands
    assert len(calls) == 1


@requires_linux_owner
def test_the_read_back_runs_once_per_commit_and_not_on_stale(owner):
    real = owner._moment_cursor_readback
    calls = []
    owner._moment_cursor_readback = lambda conn, pid: calls.append(1) or real(conn, pid)
    ok_result(call(owner, "pn_a", 2))
    assert len(calls) == 1
    ok_result(call(owner, "pn_a", 2))                        # stale: nothing was committed, nothing to verify
    assert len(calls) == 1


@requires_linux_owner
def test_an_Exception_in_the_post_commit_verification_is_committed_but_unverified_not_internal_error(owner):
    def bad(conn, pid):
        raise ValueError("bug in the verification")

    owner._moment_cursor_readback = bad
    res = call(owner, "pn_a", 2)
    assert isinstance(res, ec.Rejected) and res.reason == "readback_mismatch", res
    assert res.commit == "committed_unverified" and "COMMITTED but UNVERIFIED" in res.detail
    assert cursor_of(owner.store_path, "pn_a") == 3


@requires_linux_owner
def test_a_SystemExit_in_the_post_commit_re_read_propagates_with_the_connection_restored(owner):
    def exits(conn, pid):
        raise SystemExit(3)

    owner._moment_cursor_readback = exits
    conn = owner._owner_conn
    conn.execute("PRAGMA busy_timeout = 4321")
    with pytest.raises(SystemExit):
        owner._handle_advance_moment_cursor({"person_id": "pn_a", "from_cursor": 2}, time.monotonic() + 5.0)
    assert conn.in_transaction is False and conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
    conn.execute("UPDATE nodes SET claim = claim WHERE id = 'pn_b'")        # permissive authorizer back
    conn.rollback()
    del owner._moment_cursor_readback                                         # back to the class method
    assert cursor_of(owner.store_path, "pn_a") == 3                          # the commit stands
    assert ok_result(call(owner, "pn_c", 7))["applied"] is True              # the lock is free, the next op works


@requires_linux_owner
def test_another_sqlite_error_is_write_failed_not_an_unhandled_exception(owner, monkeypatch):
    real = emc.advance_moment_cursor

    def failing(conn, pid, frm):
        real(conn, pid, frm)
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(emc, "advance_moment_cursor", failing)
    before = whole_row(owner.store_path, "pn_a")
    res = call(owner, "pn_a", 2)
    assert isinstance(res, ec.Rejected) and res.reason == "advance_moment_cursor_write_failed", res
    assert whole_row(owner.store_path, "pn_a") == before
    assert owner._owner_conn.in_transaction is False


# --- differential (design 6.1): the owner path vs the phase-1 direct path --------------------------------

DIFF_FIXTURES = {
    "absent": ({"name": "A", "special_moments": MOMENTS}, 0),
    "two": ({"name": "B", "special_moments": MOMENTS, "special_moment_cursor": 2}, 2),
    "stale": ({"name": "C", "special_moment_cursor": 5}, 4),
    "true": ({"name": "D", "special_moment_cursor": True}, 0),
    "string": ({"name": "E", "special_moment_cursor": "3"}, 0),
    "past_int64": ({"name": "F", "special_moment_cursor": 2 ** 63}, 0),
    "non_ascii_nested": ({"name": "石磊", "aliases": ["Léa"], "nested": {"k": [1, 2]}, "pronouns": "他",
                          "special_moment_cursor": 7}, 7),
}


@requires_linux_owner
@pytest.mark.parametrize("name", sorted(DIFF_FIXTURES))
def test_differential_owner_path_vs_direct_path_same_parsed_metadata_and_same_ack(home, name):
    meta, frm = DIFF_FIXTURES[name]
    owner_home, direct_home = home / "o", home / "d"
    owner_home.mkdir()
    direct_home.mkdir()
    dbs = {}
    for side, h in (("owner", owner_home), ("direct", direct_home)):
        dbs[side] = _make_scratch_store(h)
        put(dbs[side], "pn_1", json.dumps(meta))
    # direct side: the phase-1 shared function on a direct connection
    conn = sqlite3.connect(str(dbs["direct"]))
    direct_ack = emc.advance_moment_cursor(conn, "pn_1", frm)
    conn.commit()
    conn.close()
    o = _start(owner_home)
    try:
        owner_ack = ok_result(call(o, "pn_1", frm))
    finally:
        o.stop()
    assert owner_ack == direct_ack
    assert json.loads(raw_meta(dbs["owner"], "pn_1")) == json.loads(raw_meta(dbs["direct"], "pn_1"))


@requires_linux_owner
@pytest.mark.parametrize("nid", ["ob_1", "nope", "pn_old", "pn_arr", "pn_null", "pn_str", "pn_num"])
def test_differential_refusals_agree_too(home, nid):
    owner_home, direct_home = home / "o", home / "d"
    owner_home.mkdir()
    direct_home.mkdir()
    do, dd = make_store(owner_home), make_store(direct_home)
    conn = sqlite3.connect(str(dd))
    frm = 0 if nid in ("pn_arr", "pn_null", "pn_str", "pn_num") else 2     # the value that WOULD match
    direct_ack = emc.advance_moment_cursor(conn, nid, frm)
    conn.commit()
    conn.close()
    o = _start(owner_home)
    try:
        owner_ack = ok_result(call(o, nid, frm))
    finally:
        o.stop()
    assert owner_ack == direct_ack
    assert whole_row(do, nid) == whole_row(dd, nid)


# --- packaging: the deployed FLAT layout ------------------------------------------------------------


@requires_linux_owner
def test_the_owner_resolves_the_shared_cursor_module_in_the_flattened_layout(tmp_path):
    """CLAUDE.md 'Plugin build restructures code paths': recreate the flat plugin root (the modules side by side),
    run with CLAUDE_PLUGIN_ROOT unset and a clean PYTHONPATH, from an unrelated cwd, and import the owner: its
    `engram_moment_cursor` must come from the flat copy (not from src/)."""
    flat = tmp_path / "plugin"
    flat.mkdir()
    src = Path(_SRC_ENGRAM)
    for name in [*_FLAT_MODULES, "engram_moment_cursor.py"]:
        shutil.copy2(src / name, flat / name)
    other = tmp_path / "elsewhere"
    other.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH", "ENGRAM_HOME")}
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import engramd_owner as o;"
        "print(o.emc.__file__); print(sorted(o.WRITE_METHODS))"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code, str(flat)], cwd=other, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.split("\n")[0]).resolve().parent == flat.resolve()
    assert "advance_moment_cursor" in out.stdout
