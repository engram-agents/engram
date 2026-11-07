"""Contract cells for the owner's first write, the typed `use_reward` op (#3822, U11 of #2652, Stage A).

Written BEFORE the implementation (contract-first; project CLAUDE.md). Wire:

  write("use_reward", {"node_ids": [str], "action": "mention", "request_id": str})
    -> Ok(result={"updated": [...], "bumped": n, "restamped": n, "restamp_error": None | str,
                  "rows": {id: {"utility_score", "importance_score", "recall_turn"}},
                  "duplicate": bool})
    -> Rejected(reason=<one NAMED reason per refusal>)

The owner runs the SHARED `engram_restamp.use_reward` itself (bump THEN restamp, one definition of each)
on its own connection, in one transaction, under `_owner_lock`, with a RESTRICTIVE op-scoped authorizer
installed for the op only. After COMMIT it re-reads the three touched columns of every updated id and
returns `ok` only if they equal what the transaction computed (SS5.4 read-back).

Self-contained on dev: the store is built with the real schema (`engram_core._run_db_one_time_setup`) under
tmp; nothing here touches ~/.engram or its socket.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402
import engram_restamp as er  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

THREE = ("utility_score", "importance_score", "recall_turn")
CONFIG_TURN = 7

# --- the fixture store ---------------------------------------------------------------------


def _assert_scratch(home: Path) -> None:
    """Never the live seat: the home must be a fresh tmp dir, not ~/.engram, and no socket under ~/.engram."""
    live = (Path.home() / ".engram").resolve()
    h = Path(home).resolve()
    assert h != live and live not in h.parents, h
    assert str(h).startswith(str(Path(tempfile.gettempdir()).resolve())) or "/var/folders/" in str(h) or str(h).startswith("/tmp"), h


def make_store(home: Path, *, config: object = "default") -> Path:
    """Real-schema store with: n_null (NULL utility), n_mid, n_top (Q ~ 1.0), n_low, n_other (never
    targeted), plus an edge. config.json: memory block with current_turn=CONFIG_TURN."""
    _assert_scratch(home)
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        rows = [
            # id, utility, importance_base, recall_count, recall_turn, importance_score
            ("n_null", None, 0.8, 0, 1, 0.5),
            ("n_mid", 0.5, 0.6, 3, 2, 0.4),
            ("n_top", 0.9999996, 0.9, 1, 3, 0.3),
            ("n_low", 0.2, None, 0, 1, 0.9),
            ("n_other", 0.33, 0.7, 5, 4, 0.6),
        ]
        for nid, u, ib, rc, rt, isc in rows:
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, memory_status,"
                " utility_score, importance_base, recall_count, recall_turn, importance_score)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (nid, "observation_factual", f"claim of {nid}", "2026-01-01T00:00:00", "{}", 1, "active",
                 u, ib, rc, rt, isc))
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?,?,?,?)",
            ("n_mid", "n_low", "supports", "2026-01-01T00:00:00"))
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    if config == "default":
        (home / "config.json").write_text(json.dumps(
            {"memory": {"decay_base": 1.014, "current_turn": CONFIG_TURN, "tier2_max_nodes": 1000}, "x": 1}))
    elif config is not None:
        (home / "config.json").write_text(config)
    return db


def all_rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()
    finally:
        conn.close()


def node_columns(db: Path) -> list[str]:
    conn = sqlite3.connect(str(db))
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(nodes)")]
    finally:
        conn.close()


def three(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0]: tuple(r[1:]) for r in conn.execute(
            "SELECT id, utility_score, importance_score, recall_turn FROM nodes ORDER BY id")}
    finally:
        conn.close()


def table_counts(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            " AND name NOT LIKE '%_fts%' AND name NOT LIKE '%_vec%'")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        conn.close()


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="eur-"))
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


_rid_counter = [0]


def fresh_rid() -> str:
    _rid_counter[0] += 1
    return f"rid-{_rid_counter[0]}-{os.getpid()}"


def call(owner, node_ids, action="mention", request_id=None, *, extra=None, params=None, timeout=3.0):
    if params is None:
        params = {"node_ids": node_ids, "action": action, "request_id": request_id or fresh_rid()}
        if extra:
            params.update(extra)
    return ec.write("use_reward", params, request_id=params.get("request_id") if isinstance(params, dict) and isinstance(params.get("request_id"), str) and params.get("request_id") else None,
                    timeout=timeout, socket_path=owner.socket_path)


def ok_result(res):
    assert isinstance(res, ec.Ok), res
    return res.result


def tracing(owner):
    """Record every statement the owner connection executes (normalised: literals -> ?)."""
    stmts = []
    owner._owner_conn.set_trace_callback(lambda s: stmts.append(norm(s)))
    return stmts


_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.])")


def norm(sql: str) -> str:
    s = re.sub(r"'(?:[^']|'')*'", "?", sql.strip())
    return " ".join(_NUM.sub("?", s).split())


# --- constants and the kind registry (8a, 8a-default) ----------------------------------------


def test_constants_are_named_and_pinned():
    assert eo.USE_REWARD_ACTIONS == frozenset({"mention"})
    assert eo.USE_REWARD_MAX_IDS == 500
    assert eo.USE_REWARD_NODE_ID_MAX_BYTES == 256
    assert eo.USE_REWARD_REQUEST_ID_MAX_BYTES == 128
    assert eo.USE_REWARD_DEDUPE_CAP == 1024
    assert eo.METHOD_KINDS == frozenset({"read", "write"})
    assert ec.CONTRACT_VERSION == "1.3"


def test_the_closed_write_set_is_exactly_the_two_ops():
    """8a. Adding a write method must edit this pin (and the constant). (The name predates the 2nd-4th writes and is
    kept so the test id stays stable for the test-id-set gate; the set pinned here is now the four ops.)"""
    assert eo.WRITE_METHODS == frozenset({"use_reward", "advance_moment_cursor", "fill_embeddings", "finish_embedding_regen", "cascade_unblock_start", "cascade_unblock_end"})  # 2nd write: #3856; 3rd and 4th: #3879; 5th and 6th: #4421


@requires_linux_owner
def test_every_method_in_the_table_carries_a_kind_and_the_write_kinds_equal_the_pinned_set(owner):
    assert set(owner._method_kinds) == set(owner._methods)
    assert all(k in eo.METHOD_KINDS for k in owner._method_kinds.values())
    declared_writes = {m for m, k in owner._method_kinds.items() if k == "write"}
    assert declared_writes == {"use_reward", "advance_moment_cursor", "fill_embeddings", "finish_embedding_regen", "cascade_unblock_start", "cascade_unblock_end"} == set(eo.WRITE_METHODS)


@requires_linux_owner
def test_a_second_write_registered_without_updating_the_constant_is_refused_and_breaks_the_pin(owner):
    """The plausible-wrong case: a write registered as a write but never added to WRITE_METHODS. The
    dispatcher refuses it, and the table-derived write set no longer equals the pinned constant."""
    called = []
    owner._methods["sneaky_write"] = lambda p, d: called.append(p) or {"status": "ok", "result": {}}
    owner._method_kinds["sneaky_write"] = "write"
    res = ec.write("sneaky_write", {}, socket_path=owner.socket_path, timeout=3.0)
    assert isinstance(res, ec.Rejected) and res.reason == "method_kind_mismatch", res
    assert called == []
    assert {m for m, k in owner._method_kinds.items() if k == "write"} != set(eo.WRITE_METHODS)


@requires_linux_owner
def test_a_write_method_declared_as_a_read_is_refused(owner):
    """use_reward re-labelled `read` (the other plausible-wrong case) is refused, not served."""
    owner._method_kinds["use_reward"] = "read"
    res = call(owner, ["n_mid"])
    assert isinstance(res, ec.Rejected) and res.reason == "method_kind_mismatch", res
    assert three(owner.store_path)["n_mid"][0] == 0.5


@requires_linux_owner
@pytest.mark.parametrize("kind", [None, "", "READ", "admin", 7])
def test_an_unclassified_method_is_default_deny(owner, kind):
    """8a-default: no kind (or a kind outside {read, write}) -> refused at dispatch with a reason naming the
    missing kind; the handler is never called; never treated as a read."""
    called = []
    owner._methods["dummy_unclassified"] = lambda p, d: called.append(1) or {"status": "ok", "result": {}}
    if kind is not None:
        owner._method_kinds["dummy_unclassified"] = kind
    res = ec.read("dummy_unclassified", {}, socket_path=owner.socket_path, timeout=3.0)
    assert isinstance(res, ec.Rejected), res
    assert res.reason == "method_kind_missing", res
    assert "kind" in res.detail
    assert called == []
    res_w = ec.write("dummy_unclassified", {}, socket_path=owner.socket_path, timeout=3.0)
    assert isinstance(res_w, ec.Rejected) and res_w.reason == "method_kind_missing"
    assert called == []


# --- parameter refusals (each its own named reason; nothing echoed; nothing touches the store) ---


def _refusal_cases():
    good = {"node_ids": ["n_mid"], "action": "mention", "request_id": "r"}

    def w(**kw):
        p = dict(good)
        p.update(kw)
        return p

    def wo(key):
        p = dict(good)
        del p[key]
        return p

    return [
        ("not_a_dict", ["n_mid"], "use_reward_params_shape"),
        ("extra_alpha", w(alpha=0.9), "use_reward_params_shape"),
        ("extra_value", w(value=1.0), "use_reward_params_shape"),
        ("extra_sql", w(sql="UPDATE nodes SET claim=1"), "use_reward_params_shape"),
        ("extra_column", w(column="utility_score"), "use_reward_params_shape"),
        ("extra_config_path", w(config_path="/etc/passwd"), "use_reward_params_shape"),
        ("extra_utility_score", w(utility_score=1.0), "use_reward_params_shape"),
        ("missing_node_ids", wo("node_ids"), "use_reward_params_shape"),
        ("missing_action", wo("action"), "use_reward_params_shape"),
        ("missing_request_id", wo("request_id"), "use_reward_request_id_missing"),
        ("node_ids_not_list", w(node_ids="n_mid"), "use_reward_node_ids_not_list"),
        ("node_ids_tuple_like_dict", w(node_ids={"n_mid": 1}), "use_reward_node_ids_not_list"),
        ("node_ids_empty", w(node_ids=[]), "use_reward_node_ids_empty"),
        ("node_ids_too_many", w(node_ids=[f"x{i}" for i in range(501)]), "use_reward_node_ids_too_many"),
        ("element_int", w(node_ids=["n_mid", 5]), "use_reward_node_id_not_str"),
        ("element_none", w(node_ids=[None]), "use_reward_node_id_not_str"),
        ("element_list", w(node_ids=[["n_mid"]]), "use_reward_node_id_not_str"),
        ("element_empty", w(node_ids=["n_mid", ""]), "use_reward_node_id_empty"),
        ("element_too_long", w(node_ids=["a" * 257]), "use_reward_node_id_too_long"),
        ("element_multibyte_too_long", w(node_ids=["é" * 129]), "use_reward_node_id_too_long"),
        ("element_lone_surrogate", w(node_ids=["n_\ud800"]), "use_reward_node_id_not_utf8"),
        ("action_not_str", w(action=3), "use_reward_action_not_str"),
        ("action_none", w(action=None), "use_reward_action_not_str"),
        ("action_unknown", w(action="derive"), "use_reward_action_unknown"),
        ("action_case", w(action="Mention"), "use_reward_action_unknown"),
        ("action_empty", w(action=""), "use_reward_action_unknown"),
        ("request_id_not_str", w(request_id=5), "use_reward_request_id_not_str"),
        ("request_id_none", w(request_id=None), "use_reward_request_id_not_str"),
        ("request_id_empty", w(request_id=""), "use_reward_request_id_empty"),
        ("request_id_too_long", w(request_id="r" * 129), "use_reward_request_id_too_long"),
    ]


@requires_linux_owner
@pytest.mark.parametrize("label,params,reason", _refusal_cases(), ids=[c[0] for c in _refusal_cases()])
def test_every_parameter_refusal_has_its_own_named_reason_and_touches_nothing(owner, label, params, reason):
    before = all_rows(owner.store_path)
    stmts = tracing(owner)
    res = ec.write("use_reward", params, timeout=3.0, socket_path=owner.socket_path, request_id="wire-rid")
    assert isinstance(res, ec.Rejected), res
    assert res.reason == reason, (label, res)
    # nothing the caller sent is echoed back
    blob = res.detail + res.remedy
    for needle in ("/etc/passwd", "UPDATE nodes", "n_mid", "claim=1"):
        assert needle not in blob, blob
    # 8d: refused BEFORE any SQL: no statement, no transaction
    assert stmts == [], stmts
    assert owner._owner_conn.in_transaction is False
    assert all_rows(owner.store_path) == before


@requires_linux_owner
def test_check_order_shape_wins_over_everything_else(owner):
    res = ec.write("use_reward", {"node_ids": [], "action": "zzz", "request_id": "", "alpha": 1}, timeout=3.0,
                   socket_path=owner.socket_path)
    assert res.reason == "use_reward_params_shape"
    res = ec.write("use_reward", {"node_ids": [], "action": "zzz", "request_id": ""}, timeout=3.0,
                   socket_path=owner.socket_path)
    assert res.reason == "use_reward_node_ids_empty"


@requires_linux_owner
def test_the_id_cap_boundary_is_inclusive(owner):
    ids = [f"ghost{i}" for i in range(500)]
    r = ok_result(call(owner, ids))
    assert r["updated"] == [] and r["bumped"] == 0


# --- the happy path, the ack shape, parity (the differential at the write) --------------------

PARITY_IDS = ["n_null", "n_mid", "n_mid", "missing_id", "n_top", "n_low"]


def _direct_apply(db: Path, home: Path, ids, action="mention"):
    """The oracle: the same fixture, the shared function called directly (the hook's own call shape:
    config_path, then commit). Returns (updated ids, three columns)."""
    seen = []
    real_bump = er.bump_utility

    def recording_bump(conn, node_ids, alpha):
        out = real_bump(conn, node_ids, alpha)
        seen.append(list(out))
        return out

    er.bump_utility = recording_bump
    try:
        conn = sqlite3.connect(str(db), timeout=5.0)
        try:
            result = er.use_reward(conn, ids, action, config_path=str(home / "config.json"))
            conn.commit()
        finally:
            conn.close()
    finally:
        er.bump_utility = real_bump
    return seen[0], result


def _parity_pair(tmp_root: Path, *, config="default", ids=PARITY_IDS):
    ha, hb = tmp_root / "a", tmp_root / "b"
    ha.mkdir()
    hb.mkdir()
    dba = make_store(ha, config=config)
    dbb = make_store(hb, config=config)
    o = _start(ha)
    try:
        res = call(o, ids)
    finally:
        o.stop()
    updated_direct, result_direct = _direct_apply(dbb, hb, ids)
    return res, three(dba), updated_direct, result_direct, three(dbb)


@requires_linux_owner
def test_parity_owner_op_equals_the_direct_shared_function_on_all_three_columns(home):
    res, owner_cols, upd_direct, rd, direct_cols = _parity_pair(home)
    r = ok_result(res)
    assert r["updated"] == upd_direct == ["n_null", "n_mid", "n_top", "n_low"]
    assert (r["bumped"], r["restamped"], r["restamp_error"]) == (rd.bumped, rd.restamped, rd.restamp_error) == (4, 4, None)
    assert owner_cols == direct_cols
    # the fixtures actually exercised: NULL, duplicate, missing, Q~1.0, and each moved a column
    assert owner_cols["n_null"][0] == 0.1
    assert owner_cols["n_mid"][0] == 0.55
    assert owner_cols["n_top"][0] == 1.0
    assert owner_cols["n_mid"][2] == CONFIG_TURN and owner_cols["n_other"][2] == 4
    assert "missing_id" not in owner_cols


@requires_linux_owner
def test_ack_shape_and_rows_are_the_committed_values(owner):
    r = ok_result(call(owner, ["n_mid", "n_null"]))
    assert set(r) == {"updated", "bumped", "restamped", "restamp_error", "rows", "duplicate"}
    assert r["updated"] == ["n_mid", "n_null"] and r["bumped"] == 2 and r["restamped"] == 2
    assert r["restamp_error"] is None and r["duplicate"] is False
    committed = three(owner.store_path)
    for nid in ("n_mid", "n_null"):
        assert set(r["rows"][nid]) == set(THREE)
        assert tuple(r["rows"][nid][c] for c in THREE) == committed[nid]


@requires_linux_owner
def test_all_missing_ids_is_an_ok_with_nothing_updated(owner):
    before = all_rows(owner.store_path)
    r = ok_result(call(owner, ["nope", "nada"]))
    assert r["updated"] == [] and r["bumped"] == 0 and r["restamped"] == 0 and r["rows"] == {}
    assert all_rows(owner.store_path) == before


# --- read-back is real (SS5.4) ----------------------------------------------------------------


@requires_linux_owner
def test_a_post_commit_value_that_differs_is_not_ok_and_names_the_id(owner):
    """The discriminating cell: the committed row is perturbed between COMMIT and the re-read. A happy-path-only
    assertion cannot see a read-back that is never compared."""
    real = owner._use_reward_read_rows
    calls = []

    def perturbing(conn, ids):
        rows = real(conn, ids)
        calls.append(1)
        if len(calls) == 2:  # the post-COMMIT re-read
            rows = copy.deepcopy(rows)
            rows["n_mid"]["utility_score"] = rows["n_mid"]["utility_score"] + 0.000001
        return rows

    owner._use_reward_read_rows = perturbing
    res = call(owner, ["n_low", "n_mid"])
    assert isinstance(res, ec.Rejected), res
    assert res.reason == "readback_mismatch"
    assert "n_mid" in res.detail and "n_low" not in res.detail
    assert res.commit == "committed_unverified"
    assert "cannot be undone" in res.detail or "committed" in res.detail
    # the commit stands (it cannot be undone at that point): committed-but-unverified, reported loudly
    assert three(owner.store_path)["n_mid"][0] == 0.55
    assert len(calls) == 2


@requires_linux_owner
def test_read_back_compares_importance_and_recall_turn_too(owner):
    real = owner._use_reward_read_rows
    n = []

    def perturbing(conn, ids):
        rows = copy.deepcopy(real(conn, ids))
        n.append(1)
        if len(n) == 2:
            rows["n_low"]["recall_turn"] = 12345
        return rows

    owner._use_reward_read_rows = perturbing
    res = call(owner, ["n_low"])
    assert isinstance(res, ec.Rejected) and res.reason == "readback_mismatch" and "n_low" in res.detail


@requires_linux_owner
def test_a_readback_mismatch_replay_does_not_bump_twice(owner):
    real = owner._use_reward_read_rows
    n = []

    def perturbing(conn, ids):
        rows = copy.deepcopy(real(conn, ids))
        n.append(1)
        if len(n) == 2:
            rows["n_mid"]["utility_score"] = -1
        return rows

    owner._use_reward_read_rows = perturbing
    first = call(owner, ["n_mid"], request_id="rb-1")
    assert first.reason == "readback_mismatch"
    again = call(owner, ["n_mid"], request_id="rb-1")
    assert isinstance(again, ec.Rejected) and again.reason == "readback_mismatch"
    assert three(owner.store_path)["n_mid"][0] == 0.55  # one bump only: the replay re-reports, never re-bumps


# --- idempotency (SS5.4) -----------------------------------------------------------------------


@requires_linux_owner
def test_same_request_id_twice_bumps_and_restamps_once_second_reply_is_the_first_result(owner):
    a = ok_result(call(owner, ["n_mid", "n_null"], request_id="idem-1"))
    snap = all_rows(owner.store_path)
    b = ok_result(call(owner, ["n_mid", "n_null"], request_id="idem-1"))
    assert a["duplicate"] is False and b["duplicate"] is True
    assert {k: v for k, v in b.items() if k != "duplicate"} == {k: v for k, v in a.items() if k != "duplicate"}
    assert all_rows(owner.store_path) == snap
    assert three(owner.store_path)["n_mid"][0] == 0.55


@requires_linux_owner
def test_a_different_request_id_with_the_same_ids_bumps_again(owner):
    call(owner, ["n_mid"], request_id="idem-a")
    call(owner, ["n_mid"], request_id="idem-b")
    assert three(owner.store_path)["n_mid"][0] == round(0.55 + 0.1 * 0.45, 6)


@requires_linux_owner
def test_the_same_request_id_with_a_different_payload_is_refused_not_deduped_silently(owner):
    ok_result(call(owner, ["n_mid"], request_id="idem-c"))
    res = call(owner, ["n_low"], request_id="idem-c")
    assert isinstance(res, ec.Rejected) and res.reason == "use_reward_request_id_reused", res
    assert three(owner.store_path)["n_low"][0] == 0.2


@requires_linux_owner
def test_two_concurrent_same_request_id_calls_bump_once(owner):
    results = []
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        results.append(call(owner, ["n_mid"], request_id="idem-race", timeout=5.0))

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    oks = [ok_result(r) for r in results]
    assert sorted(o["duplicate"] for o in oks) == [False, True]
    assert three(owner.store_path)["n_mid"][0] == 0.55


@requires_linux_owner
def test_the_dedupe_store_is_bounded_lru_eviction_means_a_replay_bumps_again(owner, monkeypatch):
    """Pinned honestly: past the cap the OLDEST request_id is forgotten, and a replay of it bumps again."""
    monkeypatch.setattr(eo, "USE_REWARD_DEDUPE_CAP", 3)
    for i in range(4):
        ok_result(call(owner, ["n_other"], request_id=f"lru-{i}"))
    assert len(owner._use_reward_seen) == 3
    q = three(owner.store_path)["n_other"][0]
    again = ok_result(call(owner, ["n_other"], request_id="lru-0"))
    assert again["duplicate"] is False
    assert three(owner.store_path)["n_other"][0] > q
    # a recent id is still deduped (LRU: touching refreshes)
    r = ok_result(call(owner, ["n_other"], request_id="lru-3"))
    assert r["duplicate"] is True


@requires_linux_owner
def test_KNOWN_BOUND_an_owner_restart_forgets_request_ids_so_a_retry_double_bumps(home):
    """NOT coverage. The dedupe store is in-memory: an owner restart between commit and ack followed by a client
    retry of the same request_id bumps a SECOND time (a small monotone over-credit, not corruption). A persisted
    dedupe table is a schema change and out of scope (design seat). This cell pins the bound so it is never
    mistaken for idempotency across restarts."""
    db = make_store(home)
    o1 = _start(home)
    try:
        assert ok_result(call(o1, ["n_mid"], request_id="restart-1"))["duplicate"] is False
    finally:
        o1.stop()
    assert three(db)["n_mid"][0] == 0.55
    o2 = _start(home)
    try:
        r = ok_result(call(o2, ["n_mid"], request_id="restart-1"))
    finally:
        o2.stop()
    assert r["duplicate"] is False  # the bound: forgotten across the restart
    assert three(db)["n_mid"][0] == round(0.55 + 0.1 * 0.45, 6)  # bumped twice


# --- restamp failure, config parity -------------------------------------------------------------


@requires_linux_owner
def test_forced_restamp_failure_is_a_bumped_ack_with_restamp_error_and_recall_turn_unchanged(owner, monkeypatch):
    def boom(conn, node_ids, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(er, "restamp", boom)
    r = ok_result(call(owner, ["n_mid"]))
    assert r["bumped"] == 1 and r["restamped"] == 0
    assert r["restamp_error"].startswith("restamp: RuntimeError")
    cols = three(owner.store_path)["n_mid"]
    assert cols[0] == 0.55 and cols[2] == 2 and cols[1] == 0.4  # utility moved; recall_turn and importance untouched
    assert tuple(r["rows"]["n_mid"][c] for c in THREE) == cols  # it still reads back


def _cfg_cases():
    return {
        "absent": None,
        "no_memory_block": json.dumps({"other": 1}),
        "torn": "{ not json",
        "empty_file": "",
    }


@requires_linux_owner
@pytest.mark.parametrize("name", ["absent", "no_memory_block"])
def test_config_parity_absent_or_blockless_config_stamps_turn_zero_on_both_paths(home, name):
    cfg = _cfg_cases()[name]
    res, owner_cols, upd, rd, direct_cols = _parity_pair(home, config=cfg)
    r = ok_result(res)
    assert r["restamp_error"] is None and rd.restamp_error is None
    assert owner_cols == direct_cols
    assert owner_cols["n_mid"][2] == 0  # defaults: turn 0 -- NOT an error
    assert owner_cols["n_mid"][1] == er.compute_importance(0.6, 0, er.DEFAULT_DECAY_BASE)


@requires_linux_owner
@pytest.mark.parametrize("name", ["torn", "empty_file"])
def test_config_parity_torn_config_is_restamp_error_with_the_bump_standing_on_both_paths(home, name):
    res, owner_cols, upd, rd, direct_cols = _parity_pair(home, config=_cfg_cases()[name])
    r = ok_result(res)
    assert r["restamp_error"].startswith("config:") and rd.restamp_error.startswith("config:")
    assert r["bumped"] == rd.bumped == 4 and r["restamped"] == rd.restamped == 0
    assert owner_cols == direct_cols
    assert owner_cols["n_mid"][0] == 0.55 and owner_cols["n_mid"][2] == 2


@requires_linux_owner
@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_config_parity_fifo_or_directory_config_is_restamp_error_on_both_paths(home, kind):
    ha, hb = home / "a", home / "b"
    ha.mkdir()
    hb.mkdir()
    dba = make_store(ha, config=None)
    dbb = make_store(hb, config=None)
    # The owner is started BEFORE the special file appears: an existing owner defect (reported in the handoff,
    # not fixed here) is that `start()` -> `_read_install_tier` does a blocking `Path.read_text` on config.json
    # and hangs on a FIFO. The op itself reads config.json per call, so the swap-in after start is the real case.
    o = _start(ha)
    for h in (ha, hb):
        if kind == "fifo":
            os.mkfifo(h / "config.json")
        else:
            (h / "config.json").mkdir()
    try:
        r = ok_result(call(o, PARITY_IDS, timeout=5.0))
    finally:
        o.stop()
    upd, rd = _direct_apply(dbb, hb, PARITY_IDS)
    assert r["restamp_error"].startswith("config:") and rd.restamp_error.startswith("config:")
    assert three(dba) == three(dbb)
    assert three(dba)["n_mid"][0] == 0.55


@requires_linux_owner
def test_the_config_is_read_fresh_on_every_call_never_cached_at_start(owner):
    cfg = owner.engram_home / "config.json"
    ok_result(call(owner, ["n_low"]))
    assert three(owner.store_path)["n_low"][2] == CONFIG_TURN
    cfg.write_text(json.dumps({"memory": {"decay_base": 1.014, "current_turn": 41}}))
    ok_result(call(owner, ["n_low"]))
    assert three(owner.store_path)["n_low"][2] == 41


# --- 8b, 8c ---------------------------------------------------------------------------------------


@requires_linux_owner
def test_8b_repeated_calls_move_utility_monotonically_toward_one_and_never_above(owner):
    prev = 0.2
    for _ in range(40):
        ok_result(call(owner, ["n_low"]))
        q = three(owner.store_path)["n_low"][0]
        assert prev < q <= 1.0 or (prev == q == 1.0)
        prev = q
    # an id already at Q ~ 1.0 stays <= 1.0
    for _ in range(3):
        ok_result(call(owner, ["n_top"]))
        assert three(owner.store_path)["n_top"][0] <= 1.0


@requires_linux_owner
@pytest.mark.parametrize("extra", [{"alpha": 5}, {"value": 0.0}, {"utility_score": 0.0}, {"sql": "UPDATE nodes SET utility_score=0"},
                                   {"column": "claim"}, {"config_path": "/tmp/x"}, {"alpha": -1, "value": 9}])
def test_8b_no_parameter_shape_sets_a_value_extra_keys_are_rejected_not_ignored(owner, extra):
    before = all_rows(owner.store_path)
    res = call(owner, ["n_mid"], extra=extra)
    assert isinstance(res, ec.Rejected) and res.reason == "use_reward_params_shape", res
    assert all_rows(owner.store_path) == before


@requires_linux_owner
def test_8c_only_the_three_columns_of_existing_rows_change(owner):
    cols = node_columns(owner.store_path)
    before = all_rows(owner.store_path)
    counts = table_counts(owner.store_path)
    ok_result(call(owner, ["n_null", "n_mid", "n_mid", "missing_id", "n_top", "n_low", "another_ghost"]))
    after = all_rows(owner.store_path)
    assert len(after) == len(before)
    touched = {cols.index(c) for c in THREE}
    changed = set()
    for b, a in zip(before, after):
        for i, (x, y) in enumerate(zip(b, a)):
            if x != y:
                changed.add(cols[i])
            if i not in touched:
                assert x == y and type(x) is type(y), (cols[i], x, y)
    assert changed <= set(THREE) and "utility_score" in changed and "recall_turn" in changed
    assert [r for r in after if r[0] == "n_other"] == [r for r in before if r[0] == "n_other"]
    assert table_counts(owner.store_path) == counts  # no row created anywhere
    assert "missing_id" not in three(owner.store_path)


# --- the op-scoped restrictive authorizer, the statement-set pin, the measurement ----------------

EXPECTED_STATEMENTS = {
    "SELECT utility_score FROM nodes WHERE id = ?",
    "UPDATE nodes SET utility_score = ? WHERE id = ?",
    "SELECT importance_base, recall_count FROM nodes WHERE id = ?",
    "UPDATE nodes SET importance_score = ?, recall_turn = ? WHERE id = ?",
    "SELECT utility_score, importance_score, recall_turn FROM nodes WHERE id = ?",  # the read-back
    "BEGIN IMMEDIATE",  # fold 2: the op opens its own write transaction up front (pre-op read is inside it)
    "COMMIT",
    "SAVEPOINT engram_use_restamp",
    "RELEASE SAVEPOINT engram_use_restamp",
    # The op's own busy-wait bracket (#3822 fold 1), run under the PERMISSIVE authorizer, outside the allowlisted
    # window: read the connection's prior value, set 0 (the wait is done by the owner's retry loop, see the
    # handler), restore the prior value. Both literal forms normalise to these two shapes.
    "PRAGMA busy_timeout",
    "PRAGMA busy_timeout = ?",
}


@requires_linux_owner
def test_statement_set_pin_the_executed_set_equals_the_closed_list(owner):
    """Every SQL statement the op executes on the owner connection (trace callback; trigger-originated
    statements would appear as '--' lines and are NOT in the closed list, so they go red loudly)."""
    stmts = tracing(owner)
    ok_result(call(owner, ["n_null", "n_mid", "missing_id"]))
    assert set(stmts) == EXPECTED_STATEMENTS, set(stmts) ^ EXPECTED_STATEMENTS
    assert not [s for s in stmts if s.startswith("--")]


@requires_linux_owner
def test_statement_set_pin_goes_red_when_use_reward_also_writes_something_else(owner, monkeypatch):
    """The pin discriminates: use_reward mutated to also UPDATE a column the authorizer ALLOWS (recall_turn) but
    in a statement shape outside the closed list. The authorizer stays quiet; only the pin sees it."""
    real = er.use_reward

    def mutated(conn, node_ids, action, **kw):
        out = real(conn, node_ids, action, **kw)
        conn.execute("UPDATE nodes SET recall_turn = 99 WHERE id = 'n_mid'")
        return out

    monkeypatch.setattr(er, "use_reward", mutated)
    stmts = tracing(owner)
    res = call(owner, ["n_mid"])
    assert isinstance(res, ec.Ok), res  # allowed by the column-exact authorizer ...
    assert set(stmts) != EXPECTED_STATEMENTS  # ... and caught by the pin
    assert set(stmts) - EXPECTED_STATEMENTS == {"UPDATE nodes SET recall_turn = ? WHERE id = ?"}


MEASURED_TUPLES = {
    # (action name, arg1, arg2, db_name, trigger_or_view) from ONE real use_reward('mention') on the real schema
    (sqlite3.SQLITE_SELECT, None, None, None, None),
    (sqlite3.SQLITE_READ, "nodes", "id", "main", None),
    (sqlite3.SQLITE_READ, "nodes", "utility_score", "main", None),
    (sqlite3.SQLITE_UPDATE, "nodes", "utility_score", "main", None),
    (sqlite3.SQLITE_TRANSACTION, "BEGIN", None, None, None),
    (sqlite3.SQLITE_SAVEPOINT, "BEGIN", "engram_use_restamp", None, None),
    (sqlite3.SQLITE_READ, "nodes", "importance_base", "main", None),
    (sqlite3.SQLITE_READ, "nodes", "recall_count", "main", None),
    (sqlite3.SQLITE_UPDATE, "nodes", "importance_score", "main", None),
    (sqlite3.SQLITE_UPDATE, "nodes", "recall_turn", "main", None),
    (sqlite3.SQLITE_SAVEPOINT, "RELEASE", "engram_use_restamp", None, None),
    (sqlite3.SQLITE_TRANSACTION, "COMMIT", None, None, None),
}


def _measure(home: Path, extra_sql_after=None):
    db = make_store(home)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    calls = []

    def rec(*a):
        calls.append(tuple(a))
        return sqlite3.SQLITE_OK

    conn.set_authorizer(rec)
    er.use_reward(conn, ["n_null", "n_mid", "n_mid", "missing_id"], "mention", memory_config=lambda: er.read_memory_config(home / "config.json"))
    conn.commit()
    conn.close()
    return calls


def test_measured_authorizer_tuples_of_one_real_use_reward_equal_the_pinned_set(home):
    calls = _measure(home)
    assert set(calls) == MEASURED_TUPLES, set(calls) ^ MEASURED_TUPLES
    assert all(t[4] is None for t in calls), "no trigger body fired during use_reward"


def test_every_measured_tuple_is_allowed_by_the_op_authorizer_and_nothing_else_is(home):
    for t in MEASURED_TUPLES:
        assert eo._use_reward_allows(*t) is True, t
    D = sqlite3
    denied = [
        (D.SQLITE_UPDATE, "nodes", "claim", "main", None),
        (D.SQLITE_UPDATE, "nodes", "importance_base", "main", None),
        (D.SQLITE_UPDATE, "nodes", "recall_count", "main", None),
        (D.SQLITE_UPDATE, "nodes", "utility_score", "temp", None),
        (D.SQLITE_UPDATE, "edges", "utility_score", "main", None),
        (D.SQLITE_READ, "nodes", "claim", "main", None),
        (D.SQLITE_READ, "edges", "id", "main", None),
        (D.SQLITE_READ, "nodes", "id", "temp", None),
        (D.SQLITE_INSERT, "nodes", None, "main", None),
        (D.SQLITE_DELETE, "nodes", None, "main", None),
        (D.SQLITE_PRAGMA, "journal_mode", "wal", "main", None),
        (D.SQLITE_ATTACH, "/tmp/x", None, None, None),
        (D.SQLITE_DETACH, "x", None, None, None),
        (D.SQLITE_CREATE_TABLE, "t", None, "main", None),
        (D.SQLITE_DROP_TABLE, "t", None, "main", None),
        (D.SQLITE_ALTER_TABLE, "main", "nodes", None, None),
        (D.SQLITE_FUNCTION, None, "load_extension", None, None),
        (D.SQLITE_SAVEPOINT, "BEGIN", "some_other_savepoint", None, None),
        (D.SQLITE_SAVEPOINT, "RELEASE", "other", None, None),
        (D.SQLITE_TRANSACTION, "SOMETHING", None, None, None),
        (D.SQLITE_RECURSIVE, None, None, None, None),
        (D.SQLITE_UPDATE, "nodes", "utility_score", "main", "some_unadmitted_trigger"),
        (D.SQLITE_READ, "nodes", "id", "main", "nodes_fts_rewrite_resync"),
    ]
    for t in denied:
        assert eo._use_reward_allows(*t) is False, t


def test_trigger_inventory_on_nodes_is_pinned_against_sqlite_master(home):
    """(b) A newly added trigger on `nodes` fails THIS cell loudly instead of being silently allowed (or
    silently breaking the op). None of the pinned triggers fires on the three UPDATEd columns (measured above),
    so the admitted-by-name set is empty."""
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    try:
        live = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='nodes'")}
        views = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")]
    finally:
        conn.close()
    assert live == set(eo.USE_REWARD_NODES_TRIGGERS_PINNED), live ^ set(eo.USE_REWARD_NODES_TRIGGERS_PINNED)
    assert eo.USE_REWARD_ADMITTED_TRIGGERS == frozenset()
    assert eo.USE_REWARD_ADMITTED_TRIGGERS <= eo.USE_REWARD_NODES_TRIGGERS_PINNED
    assert views == []


@requires_linux_owner
@pytest.mark.parametrize("bad_sql", [
    "UPDATE nodes SET claim = 'pwned' WHERE id = 'n_mid'",
    "UPDATE nodes SET importance_base = 9 WHERE id = 'n_mid'",
    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES ('a','b','r','t')",
    "DELETE FROM edges",
    "PRAGMA journal_mode=DELETE",
    "CREATE TABLE evil (x)",
    "SELECT claim FROM nodes",
])
def test_a_denied_statement_refuses_the_whole_op_rolled_back_with_no_partial_bump(owner, monkeypatch, bad_sql):
    """(d) REQUIRED mutation cell: use_reward edited to also touch something outside the allowlist."""
    real = er.use_reward

    def mutated(conn, node_ids, action, **kw):
        out = real(conn, node_ids, action, **kw)
        conn.execute(bad_sql)
        return out

    monkeypatch.setattr(er, "use_reward", mutated)
    before = all_rows(owner.store_path)
    counts = table_counts(owner.store_path)
    res = call(owner, ["n_mid", "n_null"])
    assert isinstance(res, ec.Rejected) and res.reason == "write_denied", res
    assert all_rows(owner.store_path) == before  # not even the bump survives
    assert table_counts(owner.store_path) == counts
    assert owner._owner_conn.in_transaction is False


@requires_linux_owner
def test_a_commit_issued_from_inside_the_shared_function_is_denied_not_a_partial_commit(owner, monkeypatch):
    """COMMIT/ROLLBACK belong to the handler's own phases: a mid-op commit would persist the bump before the
    read-back could be compared."""
    real = er.use_reward

    def committing(conn, ids, a, **kw):
        out = real(conn, ids, a, **kw)
        conn.commit()
        return out

    monkeypatch.setattr(er, "use_reward", committing)
    before = all_rows(owner.store_path)
    res = call(owner, ["n_mid"])
    assert isinstance(res, ec.Rejected) and res.reason == "write_denied", res
    assert all_rows(owner.store_path) == before


@requires_linux_owner
def test_a_denial_inside_the_restamp_savepoint_is_not_swallowed_as_a_restamp_error(owner, monkeypatch):
    """er.use_reward turns ANY restamp-time exception into `restamp_error` with the bump standing; a denied
    statement there must still refuse the WHOLE op, not hide as a decorated success."""
    def evil_restamp(conn, node_ids, **kw):
        conn.execute("UPDATE nodes SET claim = 'pwned'")

    monkeypatch.setattr(er, "restamp", evil_restamp)
    before = all_rows(owner.store_path)
    res = call(owner, ["n_mid"])
    assert isinstance(res, ec.Rejected) and res.reason == "write_denied", res
    assert all_rows(owner.store_path) == before


@requires_linux_owner
def test_a_valid_call_after_a_restamp_savepoint_denial_is_ok_on_the_same_connection(owner, monkeypatch):
    """The denial path must leave the owner connection transaction-free: after the swallowed-then-refused denial above, a
    SECOND valid use_reward on the SAME owner and connection is `Ok` with the expected bump, not `Rejected
    use_reward_txn_open`. Mutation: remove the denial path's abort() AND the final belt-and-braces rollback together -> this
    cell (alone) goes red; each removed alone stays green because the other covers it."""
    def evil_restamp(conn, node_ids, **kw):
        conn.execute("UPDATE nodes SET claim = 'pwned'")

    monkeypatch.setattr(er, "restamp", evil_restamp)
    res = call(owner, ["n_mid"])
    assert isinstance(res, ec.Rejected) and res.reason == "write_denied", res
    monkeypatch.undo()
    res2 = call(owner, ["n_mid"])
    assert isinstance(res2, ec.Ok), res2
    assert res2.result["updated"] == ["n_mid"] and res2.result["bumped"] == 1
    assert three(owner.store_path)["n_mid"][0] == 0.55
    assert not owner._owner_conn.in_transaction


@requires_linux_owner
def test_a_surprise_trigger_is_denied_by_name_and_the_op_is_refused(owner):
    """A trigger added to the store that would fire on utility_score is not in the admitted set: its body is
    denied at prepare time, the op is refused and rolled back."""
    conn = sqlite3.connect(str(owner.store_path))
    conn.execute("CREATE TRIGGER surprise AFTER UPDATE OF utility_score ON nodes BEGIN"
                 " UPDATE nodes SET metadata = metadata WHERE id = NEW.id; END")
    conn.commit()
    conn.close()
    before = three(owner.store_path)
    res = call(owner, ["n_mid"])
    assert isinstance(res, ec.Rejected) and res.reason == "write_denied", res
    assert three(owner.store_path) == before


@requires_linux_owner
def test_the_authorizer_is_restored_after_ok_denied_and_raised_exits_and_snapshot_still_works(owner, monkeypatch, home, tmp_path):
    def snap(n):
        dest = tmp_path / f"snap{n}.db"
        r = ec.read("snapshot", {"dest_path": str(dest)}, timeout=5.0, socket_path=owner.socket_path)
        assert isinstance(r, ec.Ok), r
        assert dest.exists()

    def owner_conn_is_permissive_again():
        # the placeholder internal authorizer allows everything; the op's allowlist would deny this
        owner._owner_conn.execute("UPDATE nodes SET claim = claim WHERE id = 'n_other'")
        owner._owner_conn.rollback()

    ok_result(call(owner, ["n_mid"]))
    owner_conn_is_permissive_again()
    snap(1)

    real = er.use_reward
    monkeypatch.setattr(er, "use_reward", lambda conn, ids, a, **kw: (real(conn, ids, a, **kw), conn.execute("UPDATE nodes SET claim='x'"))[0])
    res = call(owner, ["n_mid"])
    assert res.reason == "write_denied"
    owner_conn_is_permissive_again()
    snap(2)

    def raiser(conn, ids, a, **kw):
        real(conn, ids, a, **kw)
        raise RuntimeError("bug mid-op")

    monkeypatch.setattr(er, "use_reward", raiser)
    res = call(owner, ["n_mid"])
    assert isinstance(res, ec.Rejected) and res.reason == "internal_error", res
    assert owner._owner_conn.in_transaction is False
    owner_conn_is_permissive_again()
    snap(3)
    monkeypatch.setattr(er, "use_reward", real)
    assert three(owner.store_path)["n_mid"][0] == 0.55  # only the first (ok) call ever bumped; the others rolled back


# --- store errors ---------------------------------------------------------------------------------


def _hold_write_lock(owner):
    blocker = sqlite3.connect(str(owner.store_path), timeout=0.1, isolation_level=None, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    return blocker


def _release(blocker):
    try:
        blocker.execute("ROLLBACK")
    except sqlite3.Error:
        pass
    finally:
        blocker.close()


def _release_later(blocker, seconds):
    t = threading.Timer(seconds, _release, args=(blocker,))
    t.start()
    return t


@requires_linux_owner
def test_a_locked_store_is_busy_rolled_back_and_not_recorded_as_done(owner, monkeypatch):
    """The server/hooks write the store directly too. The op waits at most OWNER_WRITE_LOCK_WAIT (shortened here
    for speed) and answers the typed, DETERMINATE `busy`; a later retry of the same request_id runs."""
    monkeypatch.setattr(eo, "OWNER_WRITE_LOCK_WAIT", 0.3)
    blocker = _hold_write_lock(owner)
    try:
        res = call(owner, ["n_mid"], request_id="busy-1")
        assert isinstance(res, ec.Rejected) and res.reason == "busy", res
        assert owner._owner_conn.in_transaction is False
    finally:
        _release(blocker)
    assert three(owner.store_path)["n_mid"] == (0.5, 0.4, 2)
    r = ok_result(call(owner, ["n_mid"], request_id="busy-1"))
    assert r["duplicate"] is False and three(owner.store_path)["n_mid"][0] == 0.55


def test_lock_wait_is_strictly_below_client_timeout_minus_reply_reserve():
    """THE inequality, asserted first and alone (no literal values here): the owner's whole waiting budget must be
    strictly less than what the client will wait minus the reply reserve, so a `busy` refusal ARRIVES before the
    client gives up. Raising OWNER_WRITE_LOCK_WAIT above CLIENT_TIMEOUT - RESERVE, or shrinking either client
    constant under it, reddens THIS cell by name."""
    assert eo.OWNER_WRITE_LOCK_WAIT < ec.OWNER_WRITE_CLIENT_TIMEOUT - ec.OWNER_WRITE_REPLY_RESERVE


def test_lock_wait_constant_values_and_the_client_timeout_tie_are_pinned():
    """The values (so a change is a visible decision) and the ties. OWNER_WRITE_CLIENT_TIMEOUT is DEFAULT_TIMEOUT on
    purpose (one number a hook already passes); if they ever need to differ this cell is where that is decided."""
    assert eo.OWNER_WRITE_LOCK_WAIT == 1.0
    assert ec.OWNER_WRITE_CLIENT_TIMEOUT == 2.0
    assert ec.OWNER_WRITE_REPLY_RESERVE == 0.5
    assert ec.OWNER_WRITE_CLIENT_TIMEOUT == ec.DEFAULT_TIMEOUT
    assert eo.OWNER_WRITE_LOCK_WAIT < eo.DEFAULT_REQUEST_DEADLINE  # and below the owner's own request deadline


@requires_linux_owner
def test_a_writer_holding_longer_than_the_budget_gets_busy_before_the_client_gives_up(owner):
    import time as _t
    blocker = _hold_write_lock(owner)
    timer = _release_later(blocker, 3.5)  # far longer than the budget and than the client timeout
    try:
        t0 = _t.monotonic()
        res = call(owner, ["n_mid", "n_null"], request_id="busy-long", timeout=ec.OWNER_WRITE_CLIENT_TIMEOUT)
        elapsed = _t.monotonic() - t0
    finally:
        timer.cancel()
        _release(blocker)
    assert isinstance(res, ec.Rejected) and res.reason == "busy", res  # NOT Unavailable: a determinate answer
    assert eo.OWNER_WRITE_LOCK_WAIT * 0.7 <= elapsed < ec.OWNER_WRITE_CLIENT_TIMEOUT - ec.OWNER_WRITE_REPLY_RESERVE + 0.4, elapsed
    cols = three(owner.store_path)
    assert cols["n_mid"] == (0.5, 0.4, 2) and cols["n_null"] == (None, 0.5, 1)  # all three columns unchanged
    assert owner._owner_conn.in_transaction is False


@requires_linux_owner
def test_a_writer_holding_shorter_than_the_budget_lets_the_op_succeed(owner):
    import time as _t
    blocker = _hold_write_lock(owner)
    timer = _release_later(blocker, 0.3)
    try:
        t0 = _t.monotonic()
        r = ok_result(call(owner, ["n_mid"], timeout=ec.OWNER_WRITE_CLIENT_TIMEOUT))
        elapsed = _t.monotonic() - t0
    finally:
        timer.join()
    assert r["updated"] == ["n_mid"] and elapsed >= 0.2
    assert three(owner.store_path)["n_mid"][0] == 0.55


@requires_linux_owner
def test_the_budget_is_a_TOTAL_owner_lock_contention_and_sqlite_busy_wait_share_it(owner):
    """Hold `_owner_lock` from another thread for part of the budget and the external writer for the rest: the op
    still answers `busy` within the budget plus a small tolerance, not budget + budget."""
    import time as _t
    gate = threading.Event()

    def hold_owner_lock():
        with owner._owner_lock:
            gate.set()
            _t.sleep(0.5)

    th = threading.Thread(target=hold_owner_lock)
    th.start()
    gate.wait(2.0)
    blocker = _hold_write_lock(owner)
    timer = _release_later(blocker, 3.5)
    try:
        t0 = _t.monotonic()
        res = call(owner, ["n_mid"], request_id="busy-total", timeout=ec.OWNER_WRITE_CLIENT_TIMEOUT)
        elapsed = _t.monotonic() - t0
    finally:
        timer.cancel()
        _release(blocker)
        th.join()
    assert isinstance(res, ec.Rejected) and res.reason == "busy", res
    assert eo.OWNER_WRITE_LOCK_WAIT * 0.8 <= elapsed < eo.OWNER_WRITE_LOCK_WAIT + 0.45, elapsed
    assert three(owner.store_path)["n_mid"] == (0.5, 0.4, 2)


@requires_linux_owner
def test_an_owner_lock_held_past_the_budget_is_busy_too(owner, monkeypatch):
    monkeypatch.setattr(eo, "OWNER_WRITE_LOCK_WAIT", 0.3)
    owner._owner_lock.acquire()
    try:
        res = call(owner, ["n_mid"])
    finally:
        owner._owner_lock.release()
    assert isinstance(res, ec.Rejected) and res.reason == "busy", res
    assert three(owner.store_path)["n_mid"] == (0.5, 0.4, 2)


@requires_linux_owner
def test_the_busy_timeout_is_set_for_the_op_only_and_restored_on_every_exit_path(owner, monkeypatch, tmp_path):
    """Sentinel 4321 ms is the connection's prior value; it is back after success, denial, busy and an exception,
    and snapshot (and a permissive owner-internal statement) still work after each."""
    conn = owner._owner_conn

    def prior_is_back(tag):
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321, tag
        conn.execute("UPDATE nodes SET claim = claim WHERE id = 'n_other'")  # permissive authorizer is back
        conn.rollback()
        r = ec.read("snapshot", {"dest_path": str(tmp_path / f"s-{tag}.db")}, timeout=5.0, socket_path=owner.socket_path)
        assert isinstance(r, ec.Ok), (tag, r)

    conn.execute("PRAGMA busy_timeout = 4321")
    ok_result(call(owner, ["n_mid"]))
    prior_is_back("ok")

    real = er.use_reward
    monkeypatch.setattr(er, "use_reward", lambda c, i, a, **kw: (real(c, i, a, **kw), c.execute("UPDATE nodes SET claim='x'"))[0])
    assert call(owner, ["n_low"]).reason == "write_denied"
    prior_is_back("denied")

    def raiser(c, i, a, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr(er, "use_reward", raiser)
    assert call(owner, ["n_low"]).reason == "internal_error"
    prior_is_back("raised")
    monkeypatch.setattr(er, "use_reward", real)

    monkeypatch.setattr(eo, "OWNER_WRITE_LOCK_WAIT", 0.2)
    blocker = _hold_write_lock(owner)
    try:
        assert call(owner, ["n_low"]).reason == "busy"
    finally:
        _release(blocker)
    prior_is_back("busy")

    owner._owner_lock.acquire()
    try:
        assert call(owner, ["n_low"]).reason == "busy"  # lock-acquire timeout path: never touched the pragma
    finally:
        owner._owner_lock.release()
    prior_is_back("lock_timeout")


@requires_linux_owner
def test_a_busy_refusal_is_not_recorded_so_the_same_request_id_retry_runs(owner, monkeypatch):
    monkeypatch.setattr(eo, "OWNER_WRITE_LOCK_WAIT", 0.2)
    blocker = _hold_write_lock(owner)
    try:
        assert call(owner, ["n_mid"], request_id="retry-1").reason == "busy"
    finally:
        _release(blocker)
    assert "retry-1" not in owner._use_reward_seen
    r = ok_result(call(owner, ["n_mid"], request_id="retry-1"))
    assert r["duplicate"] is False and three(owner.store_path)["n_mid"][0] == 0.55


# --- the one indeterminate case: the CLIENT's own timeout --------------------------------------


@requires_linux_owner
def test_a_client_timeout_on_a_write_echoes_the_request_id_and_a_same_id_retry_returns_the_stored_outcome(owner):
    """The reply is delayed past the client's timeout AFTER the op committed: the client sees Unavailable(timeout),
    which on a WRITE is INDETERMINATE (it committed). One retry with the SAME request_id returns the stored outcome
    with duplicate=true, and the node was bumped once."""
    import time as _t
    real_handler = owner._handle_use_reward
    stalls = []

    def commit_then_stall(params, deadline):
        out = real_handler(params, deadline)
        if not stalls:
            stalls.append(1)
            _t.sleep(1.0)
        return out

    owner._methods["use_reward"] = commit_then_stall
    first = call(owner, ["n_mid"], request_id="indet-1", timeout=0.4)
    assert isinstance(first, ec.Unavailable) and first.reason == "timeout", first
    assert first.request_id == "indet-1"
    assert three(owner.store_path)["n_mid"][0] == 0.55  # it DID commit
    _t.sleep(1.0)
    again = call(owner, ["n_mid"], request_id="indet-1", timeout=3.0)
    r = ok_result(again)
    assert r["duplicate"] is True and r["updated"] == ["n_mid"]
    assert three(owner.store_path)["n_mid"][0] == 0.55  # still one bump
    assert again.request_id == "indet-1"


def test_write_docstring_states_that_unavailable_on_a_write_is_indeterminate():
    doc = ec.write.__doc__
    assert "INDETERMINATE" in doc and "SAME request_id" in doc


@requires_linux_owner
def test_another_sqlite_error_is_write_failed_not_an_unhandled_exception(owner, monkeypatch):
    real = er.use_reward

    def failing(conn, ids, a, **kw):
        real(conn, ids, a, **kw)
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(er, "use_reward", failing)
    before = all_rows(owner.store_path)
    res = call(owner, ["n_mid"])
    assert isinstance(res, ec.Rejected) and res.reason == "use_reward_write_failed", res
    assert all_rows(owner.store_path) == before
    assert owner._owner_conn.in_transaction is False


# --- contract version and skew (item 9) ------------------------------------------------------------


@requires_linux_owner
def test_ping_reports_contract_1_3(owner):
    r = ec.read("ping", {}, socket_path=owner.socket_path, timeout=3.0)
    assert isinstance(r, ec.Ok) and r.result["contract_version"] == "1.3"
    assert owner.contract_version == "1.3"


def test_minor_skew_is_tolerated_both_ways_so_the_new_method_is_additive():
    assert ec.check_contract_version("1.2", owner="1.3") is None
    assert ec.check_contract_version("1.3", owner="1.2") is None
    assert ec.check_contract_version("2.0", owner="1.3").reason == "contract_version"


@requires_linux_owner
def test_skew_an_older_owner_without_use_reward_answers_unknown_operation_typed(owner):
    """Simulate an older owner: the method is gone from the table. The client returns it typed (no crash, no
    fallback, no Ok); Stage B's hook will make it loud."""
    del owner._methods["use_reward"]
    owner._method_kinds.pop("use_reward", None)
    before = all_rows(owner.store_path)
    res = call(owner, ["n_mid"])
    assert isinstance(res, ec.Rejected) and res.reason == "unknown_operation", res
    assert "ping" in res.remedy and "snapshot" in res.remedy and "use_reward" not in res.remedy
    assert all_rows(owner.store_path) == before


# --- packaging: the deployed FLAT layout ------------------------------------------------------------

_FLAT_MODULES = [
    "engramd_owner.py", "engramd_client.py", "engramd_read_registry.py", "engram_pidfile.py", "engram_walguard.py",
    "engram_regular_io.py", "engram_restamp.py", "engram_config_io.py", "engram_moment_cursor.py",
    "engram_home_resolver.py",  # #3863 step 2: engramd_client and engramd_owner import it
]


def test_the_owner_resolves_engram_restamp_and_config_io_in_the_flattened_layout(tmp_path):
    """CLAUDE.md 'Plugin build restructures code paths': the plugin root holds the runtime modules side by side.
    Recreate that, run with CLAUDE_PLUGIN_ROOT unset and a clean PYTHONPATH, from an unrelated cwd, and import the
    owner: its `engram_restamp` must come from the flat copy (not from src/)."""
    flat = tmp_path / "plugin"
    flat.mkdir()
    src = Path(_SRC_ENGRAM)
    for name in _FLAT_MODULES:
        shutil.copy2(src / name, flat / name)
    other = tmp_path / "elsewhere"
    other.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH", "ENGRAM_HOME")}
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import engramd_owner as o, engram_config_io as c;"
        "print(o.er.__file__); print(c.__file__); print(o.WRITE_METHODS == frozenset({'use_reward', 'advance_moment_cursor', 'fill_embeddings', 'finish_embedding_regen', 'cascade_unblock_start', 'cascade_unblock_end'}))"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code, str(flat)], cwd=other, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.split()
    assert Path(lines[0]).resolve().parent == flat.resolve()
    assert Path(lines[1]).resolve().parent == flat.resolve()
    assert lines[2] == "True"


# ===================================================================================================
# Fold 2 (reviewer-fairy round 1 + security seat): read-back strength, BaseException, fingerprint size,
# trigger-column disjointness, the second displacement check.
# ===================================================================================================


def _fakes_that_write_nothing(monkeypatch):
    """Probe P1: the shared bump / restamp claim success and write NOTHING."""
    monkeypatch.setattr(er, "bump_utility", lambda conn, ids, alpha: list(dict.fromkeys(ids)))
    monkeypatch.setattr(er, "restamp", lambda conn, ids, **kw: len(list(ids)))


@requires_linux_owner
def test_the_two_read_back_reads_run_in_the_right_phases_expected_in_the_txn_actual_after_commit(owner):
    """(a) PHASE ordering. EXPECTED is read with the transaction open, ACTUAL after COMMIT. Mutation M8 (the
    EXPECTED read moved after COMMIT, which makes the compare vacuous) turns the first into False."""
    real = owner._use_reward_read_rows
    phases = []

    def recording(conn, ids):
        phases.append(conn.in_transaction)
        return real(conn, ids)

    owner._use_reward_read_rows = recording
    ok_result(call(owner, ["n_mid"]))
    assert phases == [True, False], phases


@requires_linux_owner
def test_P1_a_shared_function_that_claims_an_update_but_writes_nothing_is_not_ok(owner, monkeypatch):
    """EXPECTED is observed state, so on its own it cannot see a write that never happened. The independent
    monotone invariant can: a claimed update of an id whose pre-op utility was < 0.99 must leave ACTUAL strictly
    greater than the pre-op value (NULL = 0.0)."""
    _fakes_that_write_nothing(monkeypatch)
    res = call(owner, ["n_mid", "n_null"])
    assert isinstance(res, ec.Rejected) and res.reason == "readback_mismatch", res
    assert "n_mid" in res.detail and "n_null" in res.detail and "write did not land" in res.detail
    assert res.commit == "committed_unverified"
    assert three(owner.store_path)["n_mid"] == (0.5, 0.4, 2)


@requires_linux_owner
def test_the_monotone_invariant_is_scoped_to_ids_below_0_99_so_the_rounding_parity_cases_stay_authoritative(owner, monkeypatch):
    """An id at Q ~ 1.0 may legitimately round to an unchanged value, so it is exempt: the fake-writes-nothing
    probe on n_top alone is NOT a violation. Real parity (n_top really bumped, to 1.0) is the parity cell's job."""
    _fakes_that_write_nothing(monkeypatch)
    r = ok_result(call(owner, ["n_top"]))
    assert r["updated"] == ["n_top"]


@requires_linux_owner
def test_the_invariant_catches_only_the_ids_that_did_not_move(owner, monkeypatch):
    """A shared function that bumps n_mid for real but silently skips n_low: only n_low is named."""
    real = er.bump_utility

    def partial(conn, ids, alpha):
        real(conn, [i for i in ids if i != "n_low"], alpha)
        return list(dict.fromkeys(ids))

    monkeypatch.setattr(er, "bump_utility", partial)
    res = call(owner, ["n_mid", "n_low"])
    assert isinstance(res, ec.Rejected) and res.reason == "readback_mismatch"
    assert "n_low" in res.detail and "n_mid" not in res.detail


# --- (2) BaseException ----------------------------------------------------------------------------


class _Boom(BaseException):
    pass


@requires_linux_owner
def test_a_BaseException_rolls_the_transaction_back_and_the_next_op_does_not_commit_its_bump(owner, monkeypatch):
    import time as _t
    real = er.use_reward

    def bump_then_boom(conn, ids, a, **kw):
        real(conn, ids, a, **kw)
        raise _Boom()

    monkeypatch.setattr(er, "use_reward", bump_then_boom)
    params = {"node_ids": ["n_mid"], "action": "mention", "request_id": "boom-1"}
    with pytest.raises(_Boom):
        owner._handle_use_reward(params, _t.monotonic() + 5.0)
    monkeypatch.setattr(er, "use_reward", real)
    assert owner._owner_conn.in_transaction is False
    assert "boom-1" not in owner._use_reward_seen
    owner._owner_conn.execute("UPDATE nodes SET claim = claim WHERE id = 'n_other'")  # permissive authorizer is back
    owner._owner_conn.rollback()
    assert three(owner.store_path)["n_mid"] == (0.5, 0.4, 2)  # nothing committed by the aborted request
    ok_result(call(owner, ["n_low"], request_id="boom-2"))  # the NEXT op must not commit boom-1's bump
    assert three(owner.store_path)["n_mid"] == (0.5, 0.4, 2)
    r = ok_result(call(owner, ["n_mid"], request_id="boom-1"))  # a retry of the first id RUNS
    assert r["duplicate"] is False and three(owner.store_path)["n_mid"][0] == 0.55


@requires_linux_owner
def test_an_op_refuses_to_start_while_the_shared_connection_already_has_a_transaction_open(owner):
    owner._owner_conn.execute("UPDATE nodes SET claim = claim WHERE id = 'n_other'")
    assert owner._owner_conn.in_transaction is True
    stmts = tracing(owner)
    try:
        res = call(owner, ["n_mid"])
        assert isinstance(res, ec.Rejected) and res.reason == "use_reward_txn_open", res
        assert stmts == []
        assert owner._owner_conn.in_transaction is True  # the other user's transaction is left alone
    finally:
        owner._owner_conn.rollback()
    assert three(owner.store_path)["n_mid"] == (0.5, 0.4, 2)


# --- (3) fingerprint size --------------------------------------------------------------------------


@requires_linux_owner
def test_the_dedupe_entry_stores_a_32_byte_digest_not_the_ids(owner):
    ids = [("x" * 249) + f"{i:07d}" for i in range(500)]  # 500 ids of 256 bytes
    assert all(len(i.encode()) == 256 for i in ids)
    ok_result(call(owner, ids, request_id="big-1"))
    fp = owner._use_reward_seen["big-1"]["fp"]
    assert isinstance(fp, bytes) and len(fp) == 32, type(fp)
    res = call(owner, ids[:499], request_id="big-1")
    assert isinstance(res, ec.Rejected) and res.reason == "use_reward_request_id_reused", res
    again = ok_result(call(owner, ids, request_id="big-1"))
    assert again["duplicate"] is True


# --- (4) Zephyr: trigger-column disjointness, parsed from the schema --------------------------------

_TRIGGER_UPDATE = re.compile(r"\bUPDATE\b(?:\s+OF\s+(?P<cols>[\w\s,]+?))?\s+ON\s+\"?nodes\"?\b", re.I)


def trigger_fired_columns(db: Path) -> set:
    """Columns any trigger on `nodes` fires on, from sqlite_master.sql. A plain `UPDATE ON nodes` (no OF list)
    fires on EVERY column update: it is returned as {"*"}."""
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='nodes'").fetchall()
    finally:
        conn.close()
    fired: set = set()
    for _name, sql in rows:
        head = " ".join(sql.split()).split(" BEGIN ", 1)[0]  # the trigger header, not its body
        m = _TRIGGER_UPDATE.search(head)
        if m is None:
            continue
        cols = m.group("cols")
        fired |= {c.strip().lower() for c in cols.split(",")} if cols else {"*"}
    return fired


def _violates(fired: set) -> bool:
    written = set(eo.USE_REWARD_UPDATE_COLUMNS)
    return "*" in fired or bool(fired & written)


def test_disjointness_use_reward_writes_no_column_any_trigger_on_nodes_fires_on(home):
    """The property, not the current membership: {columns use_reward writes} and {columns any trigger on `nodes`
    fires on} are disjoint in the real schema (the by-name pin cannot catch a migration that couples them)."""
    db = make_store(home)
    fired = trigger_fired_columns(db)
    assert {"claim", "is_current", "status", "metadata"} <= fired  # the parser really sees the OF lists
    assert not _violates(fired), fired & set(eo.USE_REWARD_UPDATE_COLUMNS)


@pytest.mark.parametrize("ddl", [
    "CREATE TRIGGER scratch AFTER UPDATE OF utility_score ON nodes BEGIN SELECT 1; END",
    "CREATE TRIGGER scratch AFTER UPDATE OF claim, recall_turn ON nodes BEGIN SELECT 1; END",
    "CREATE TRIGGER scratch AFTER UPDATE ON nodes BEGIN SELECT 1; END",
])
def test_disjointness_goes_red_on_a_scratch_trigger_that_couples_them(home, ddl):
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    conn.execute(ddl)
    conn.commit()
    conn.close()
    assert _violates(trigger_fired_columns(db))


# --- (5) the second displacement check -------------------------------------------------------------


@requires_linux_owner
def test_displacement_detected_only_after_the_lock_wait_is_rejected_and_nothing_is_written(owner, monkeypatch):
    import engram_walguard
    calls = []

    def second_call_displaced(path):
        calls.append(path)
        if len(calls) >= 2:  # the dispatch check passed; the in-lock re-check finds it
            raise engram_walguard.SubstrateDisplaced({"reason": "test"}, "n/a")

    monkeypatch.setattr(engram_walguard, "assert_not_displaced", second_call_displaced)
    before = all_rows(owner.store_path)
    res = call(owner, ["n_mid"])
    assert isinstance(res, ec.Rejected) and res.reason == "substrate_displaced", res
    assert len(calls) == 2
    assert owner._displaced is True
    assert all_rows(owner.store_path) == before


# --- (4)(c) the ledger states the limits and the authorizer supersede -------------------------------


def test_ledger_section_6_states_the_readback_limits_and_the_authorizer_supersede():
    text = (Path(__file__).resolve().parent.parent / "docs" / "internal" / "2652-migration-ledger.md").read_text(encoding="utf-8")
    sec = text[text.index("## 6. Owner writes"):]
    for needle in ("CANNOT detect", "CAN detect", "supersedes", "no-swapping", "fails closed", "backup()", "write did not land"):
        assert needle in sec, needle


# ===================================================================================================
# Fold 3 (reviewer-fairy round 2): set-up exception safety, any failure after COMMIT, doc ties.
# ===================================================================================================


class _ProxyConn:
    """Delegates to the real connection; `set_authorizer(<the permissive placeholder>)` raises (the finally's
    restore fails). Everything else, including the op's own authorizer, passes through."""

    def __init__(self, real, fail_on="permissive"):
        self._real = real
        self._fail_on = fail_on

    def __getattr__(self, name):
        return getattr(self._real, name)

    def set_authorizer(self, cb):
        is_permissive = cb is eo._owner_internal_authorizer
        if (self._fail_on == "permissive") == is_permissive:
            raise sqlite3.OperationalError("simulated authorizer failure")
        return self._real.set_authorizer(cb)


@requires_linux_owner
def test_a_BaseException_between_the_pragma_and_the_authorizer_install_restores_both(owner, monkeypatch):
    """The busy_timeout = 0 was set; constructing the guard (before the authorizer is installed) blows up with a
    BaseException. The prior busy_timeout must be back and the permissive authorizer still in place."""
    import time as _t
    conn = owner._owner_conn
    conn.execute("PRAGMA busy_timeout = 4321")

    class ExplodingGuard:
        def __init__(self):
            raise _Boom()

    monkeypatch.setattr(eo, "_UseRewardGuard", ExplodingGuard)
    params = {"node_ids": ["n_mid"], "action": "mention", "request_id": "setup-1"}
    with pytest.raises(_Boom):
        owner._handle_use_reward(params, _t.monotonic() + 5.0)
    monkeypatch.undo()
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
    conn.execute("UPDATE nodes SET claim = claim WHERE id = 'n_other'")  # permissive authorizer in place
    conn.rollback()
    assert owner._owner_lock.acquire(timeout=1.0)  # and the lock was released
    owner._owner_lock.release()
    assert three(owner.store_path)["n_mid"] == (0.5, 0.4, 2)


@requires_linux_owner
def test_a_failing_op_authorizer_install_still_restores_the_busy_timeout(owner):
    import time as _t
    real = owner._owner_conn
    real.execute("PRAGMA busy_timeout = 4321")
    owner._owner_conn = _ProxyConn(real, fail_on="op")
    try:
        with pytest.raises(sqlite3.OperationalError):
            owner._handle_use_reward({"node_ids": ["n_mid"], "action": "mention", "request_id": "setup-2"}, _t.monotonic() + 5.0)
    finally:
        owner._owner_conn = real
    assert real.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
    real.execute("UPDATE nodes SET claim = claim WHERE id = 'n_other'")  # still the permissive authorizer
    real.rollback()


@requires_linux_owner
def test_a_failing_permissive_restore_in_the_finally_does_not_skip_the_busy_timeout_restore(owner):
    real = owner._owner_conn
    real.execute("PRAGMA busy_timeout = 4321")
    owner._owner_conn = _ProxyConn(real)
    try:
        res = call(owner, ["n_mid"], request_id="restore-1")
        assert isinstance(res, ec.Ok), res  # the committed outcome is still answered
        assert real.execute("PRAGMA busy_timeout").fetchone()[0] == 4321  # not skipped by the failed restore
    finally:
        owner._owner_conn = real
        real.set_authorizer(eo._owner_internal_authorizer)  # the simulated failure left the op authorizer installed


# --- (2) any failure after COMMIT ----------------------------------------------------------------


@requires_linux_owner
def test_an_Exception_in_the_post_commit_verification_is_committed_but_unverified_recorded_and_not_replayed_as_a_bump(owner):
    real = owner._use_reward_read_rows
    n = []

    class Bad(dict):
        def items(self):
            raise ValueError("bug in the verification")

    def second_read_is_bad(conn, ids):
        n.append(1)
        rows = real(conn, ids)
        return Bad(rows) if len(n) == 2 else rows

    owner._use_reward_read_rows = second_read_is_bad
    res = call(owner, ["n_mid"], request_id="post-1")
    assert isinstance(res, ec.Rejected) and res.reason == "readback_mismatch", res  # typed, NOT internal_error
    assert "COMMITTED but UNVERIFIED" in res.detail and res.commit == "committed_unverified"
    assert three(owner.store_path)["n_mid"][0] == 0.55  # the commit stands
    assert "post-1" in owner._use_reward_seen
    owner._use_reward_read_rows = real
    again = call(owner, ["n_mid"], request_id="post-1")
    assert isinstance(again, ec.Rejected) and again.reason == "readback_mismatch" and again.commit == "committed_unverified"
    assert three(owner.store_path)["n_mid"][0] == 0.55  # NOT bumped again


@requires_linux_owner
def test_a_SystemExit_in_the_post_commit_re_read_is_recorded_then_propagates_and_the_next_op_is_unaffected(owner):
    import time as _t
    real = owner._use_reward_read_rows
    n = []

    def second_read_exits(conn, ids):
        n.append(1)
        if len(n) == 2:
            raise SystemExit(3)
        return real(conn, ids)

    owner._use_reward_read_rows = second_read_exits
    conn = owner._owner_conn
    conn.execute("PRAGMA busy_timeout = 4321")
    with pytest.raises(SystemExit):
        owner._handle_use_reward({"node_ids": ["n_mid"], "action": "mention", "request_id": "post-2"}, _t.monotonic() + 5.0)
    owner._use_reward_read_rows = real
    entry = owner._use_reward_seen["post-2"]["response"]  # recorded BEFORE the re-raise
    assert entry["reason"] == "readback_mismatch" and entry["commit"] == "committed_unverified"
    assert conn.in_transaction is False and conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
    conn.execute("UPDATE nodes SET claim = claim WHERE id = 'n_other'")  # permissive authorizer back
    conn.rollback()
    ok_result(call(owner, ["n_low"], request_id="post-3"))  # lock free, next op unaffected
    again = call(owner, ["n_mid"], request_id="post-2")
    assert isinstance(again, ec.Rejected) and again.commit == "committed_unverified"
    assert three(owner.store_path)["n_mid"][0] == 0.55  # one bump only


def test_write_docstring_and_ledger_say_determinate_only_up_to_the_commit_and_carry_the_client_timeout_obligation():
    assert "up to the COMMIT" in ec.write.__doc__
    text = (Path(__file__).resolve().parent.parent / "docs" / "internal" / "2652-migration-ledger.md").read_text(encoding="utf-8")
    sec = text[text.index("## 6. Owner writes"):]
    assert "up to the COMMIT" in sec
    assert "request deadline, never to the client's own timeout" in sec and "OWNER_WRITE_CLIENT_TIMEOUT" in sec
    assert "use_reward_owner_busy" not in sec and "use_reward_store_busy" not in sec


# ===================================================================================================
# Fold 4: the PRAGMA admission is pinned exactly (round 3 mutations M7 / M8 left every cell green).
# ===================================================================================================

_BUSY_FORMS = ["PRAGMA busy_timeout", "PRAGMA busy_timeout = 123", "PRAGMA main.busy_timeout"]
_OTHER_PRAGMAS = [
    "PRAGMA journal_mode", "PRAGMA journal_mode = DELETE", "PRAGMA foreign_keys", "PRAGMA foreign_keys = ON",
    "PRAGMA query_only", "PRAGMA query_only = 1", "PRAGMA writable_schema = 1", "PRAGMA user_version",
    "PRAGMA user_version = 5",
]


def _pragma_outcomes(flag: bool, statements) -> dict:
    """Run each statement on a real connection under a real guard's authorizer; True = admitted."""
    guard = eo._UseRewardGuard()
    guard.allow_busy_pragma = flag
    conn = sqlite3.connect(":memory:")
    conn.set_authorizer(guard.authorizer)
    out = {}
    for sql in statements:
        try:
            conn.execute(sql).fetchall()
            out[sql] = True
        except sqlite3.DatabaseError:
            out[sql] = False
    conn.close()
    return out


def test_a_fresh_guard_does_not_admit_the_busy_pragma():
    assert eo._UseRewardGuard().allow_busy_pragma is False  # (a) kills "initialise the flag to True" (M8)


def test_with_the_flag_off_every_pragma_form_is_denied_busy_timeout_included():
    out = _pragma_outcomes(False, _BUSY_FORMS + _OTHER_PRAGMAS)
    assert not any(out.values()), {k: v for k, v in out.items() if v}  # (b)


def test_with_the_flag_on_only_the_busy_timeout_forms_are_admitted():
    out = _pragma_outcomes(True, _BUSY_FORMS + _OTHER_PRAGMAS)
    assert all(out[s] for s in _BUSY_FORMS), out  # (c) the admission is real ...
    assert not any(out[s] for s in _OTHER_PRAGMAS), {s: out[s] for s in _OTHER_PRAGMAS if out[s]}  # ... and exact


@requires_linux_owner
def test_the_pragma_flag_is_false_for_every_authorizer_call_while_the_op_statements_run(owner, monkeypatch):
    seen = []

    class RecordingGuard(eo._UseRewardGuard):
        def authorizer(self, *a):
            seen.append(self.allow_busy_pragma)
            return super().authorizer(*a)

    monkeypatch.setattr(eo, "_UseRewardGuard", RecordingGuard)
    ok_result(call(owner, ["n_mid", "n_null"]))
    assert seen and set(seen) == {False}, set(seen)  # (d) the flag only opens in the handler's own restore
