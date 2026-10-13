"""Contract cells for the owner's typed `chain` method (#3740 Cut A; design seat ruling,
issue comment 6133024506).

The tripwire hook's generalization-chain render walked `engram_core.generalization_chain`
hop by hop through the owner shim: 12-14 owner round trips per fire for two hits. Cut A
makes the whole walk ONE typed owner op that RUNS `engram_core.generalization_chain`
itself on the owner's client-read connection and returns its dict. No second
implementation (no recursive-CTE rewrite): the owner and direct paths are equal by
construction, and the cells below assert the equality instead of hoping for it.

Written BEFORE the implementation (contract-first; project CLAUDE.md). Wire:

  read("chain", {"node_id": <str>, "max_depth": <int>})
    -> Ok(result=<the dict generalization_chain returned, JSON round-tripped>)
    -> Rejected(reason=<one NAMED reason per violation>)

Reasons, in CHECK ORDER (the first violated wins), all before anything is prepared on
the store connection:
  chain_params_shape, chain_node_id_not_str, chain_node_id_empty,
  chain_node_id_too_long (UTF-8 BYTES), chain_node_id_not_utf8 (a lone surrogate cannot be
  bound as a SQL parameter), chain_max_depth_not_int, chain_max_depth_negative;
  after execution: chain_result_too_large (named, never truncated).
`max_depth` above CHAIN_MAX_DEPTH_CAP is CLAMPED, not rejected.
Store-side: a store without the tables the walk reads -> `read_failed`; the walk's
interrupt at the request deadline -> `deadline_exceeded`; no `engram_core` in the owner's
runtime -> `chain_runtime_unavailable`.

Self-contained on dev: the store is built by `chain_store` below with the real schema.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
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

import engram_core  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402


# --- the fixture store ----------------------------------------------------------------

def _node(conn, nid, typ, claim, *, current=1, summary=None, superseded_by=None):
    conn.execute(
        "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, recall_summary, memory_status,"
        " superseded_by) VALUES (?,?,?,?,?,?,?,?,?)",
        (nid, typ, claim, "2026-01-01T00:00:00", "{}", current, summary, "active", superseded_by))


def _edge(conn, source, target, relation):
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?,?,?,?)",
        (source, target, relation, "2026-01-01T00:00:00"))


def chain_store(home: Path, *, head_claim: str = "TOP principle: verify before acting") -> Path:
    """A real-schema store holding one fixture per equality case (ids in the comments):

      (a) principle start      ls_p      <- cs_a <- cs_top
      (b) concrete start       ob_c exemplifies ls_p ; ob_i instantiates ls_m
      (c) supersede-resolved   ls_s <- cs_old(superseded by cs_new) ; ls_old2 superseded by ls_s
      (d) multiple parents     ls_m <- {cs_a, cs_b} <- cs_top   (cs_top reached twice, listed once)
      (e) depth truncation     g6 <- g5 <- ... <- g0   (7 rungs)
      (f) a cycle              cy1 <-> cy2   (raw edge inserts: the write-time guard is bypassed)
      (g) a missing start      'nope' is not in the store
      (h) dead-end supersedes  ds_dead (retracted, no successor), ds_dangling (successor missing)
    """
    import os

    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        _node(conn, "cs_top", "cornerstone", head_claim, summary="top summary")
        _node(conn, "cs_a", "cornerstone", "cornerstone A claim", summary="A summary")
        _node(conn, "cs_b", "cornerstone", "cornerstone B claim", summary="B summary")
        _edge(conn, "cs_top", "cs_a", "generalizes")
        _edge(conn, "cs_top", "cs_b", "generalizes")
        _node(conn, "ls_p", "lesson", "principle lesson P", summary="P summary")
        _edge(conn, "cs_a", "ls_p", "generalizes")
        _node(conn, "ob_c", "observation_factual", "a concrete observation")
        _edge(conn, "ob_c", "ls_p", "exemplifies")
        _node(conn, "ls_m", "lesson", "multi-parent lesson M", summary="M summary")
        _edge(conn, "cs_a", "ls_m", "generalizes")
        _edge(conn, "cs_b", "ls_m", "generalizes")
        _node(conn, "ob_i", "observation_factual", "another concrete observation")
        _edge(conn, "ob_i", "ls_m", "instantiates")
        _node(conn, "cs_new", "cornerstone", "the CURRENT version of the superseded cornerstone", summary="new summary")
        _node(conn, "cs_old", "cornerstone", "RETIRED text", current=0, superseded_by="cs_new")
        _node(conn, "ls_s", "lesson", "lesson under a superseded cornerstone", summary="S summary")
        _edge(conn, "cs_old", "ls_s", "generalizes")
        _node(conn, "ls_old2", "lesson", "OLD text of the start", current=0, superseded_by="ls_s")
        for i in range(7):
            _node(conn, f"g{i}", "lesson", f"rung {i} claim", summary=f"rung {i}")
        for i in range(6):
            _edge(conn, f"g{i}", f"g{i + 1}", "generalizes")
        _node(conn, "cy1", "lesson", "cycle one", summary="cy1")
        _node(conn, "cy2", "lesson", "cycle two", summary="cy2")
        _edge(conn, "cy1", "cy2", "generalizes")
        _edge(conn, "cy2", "cy1", "generalizes")
        _node(conn, "ds_dead", "lesson", "retracted, no successor", current=0)
        _node(conn, "ds_dangling", "lesson", "successor missing", current=0, superseded_by="ghost_id")
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


def bare_store(home: Path, *, with_nodes: bool = False) -> Path:
    """A knowledge.db WITHOUT the tables the walk needs (optionally with `nodes` only)."""
    db = home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE unrelated (x TEXT)")
    if with_nodes:
        conn.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT, is_current INTEGER,"
            " superseded_by TEXT, recall_summary TEXT)")
        conn.execute("INSERT INTO nodes VALUES ('ls_x', 'lesson', 'c', 1, NULL, NULL)")
    conn.commit()
    conn.close()
    return db


def direct_chain(db: Path, node_id, max_depth) -> dict:
    """The oracle: engram_core.generalization_chain on a read-only connection (row_factory
    sqlite3.Row, like the hook's direct path), JSON round-tripped."""
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return json.loads(json.dumps(engram_core.generalization_chain(conn, node_id, max_depth)))
    finally:
        conn.close()


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="ech-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _start(home, request_deadline=0.6):
    owner = eo.EngramdOwner(home, request_deadline=request_deadline)
    owner.start(install_signal_handlers=False)
    return owner


@pytest.fixture
def chain_owner(home):
    chain_store(home)
    owner = _start(home)
    try:
        yield owner
    finally:
        owner.stop()


def _chain(owner, node_id="ls_p", max_depth=4, timeout=3.0):
    return ec.read("chain", {"node_id": node_id, "max_depth": max_depth}, timeout=timeout,
                   socket_path=owner.socket_path)


def _ping_ok(owner) -> bool:
    return isinstance(ec.read("ping", {}, timeout=2.0, socket_path=owner.socket_path), ec.Ok)


def _recording(owner):
    calls = []

    def recording(*a):
        calls.append(a)
        return eo._client_read_authorizer(*a)

    owner._client_read_conn.set_authorizer(recording)
    return calls


# --- constants ---------------------------------------------------------------------------

def test_chain_constants_are_named_and_pinned():
    assert eo.CHAIN_NODE_ID_MAX_BYTES == 256
    assert eo.CHAIN_MAX_DEPTH_CAP == 4


def test_chain_depth_cap_equals_the_functions_own_default():
    """The clamp ceiling is the default cap of generalization_chain's signature; if the
    function's default moves, this goes red instead of the two silently disagreeing."""
    default = inspect.signature(engram_core.generalization_chain).parameters["max_depth"].default
    assert eo.CHAIN_MAX_DEPTH_CAP == default == 4


def test_chain_result_bound_is_the_bound_read_applies():
    """One number, not two: the chain bound IS the read path's response bound (8 MiB)."""
    assert eo.CHAIN_RESULT_MAX_BYTES == eo.MAX_READ_RESPONSE_BYTES == 8 * 1024 * 1024


# --- equality by construction (the load-bearing cell) -------------------------------------

def _principle(c):
    assert c["start_resolved_id"] == "ls_p" and c["roots"] == ["ls_p"]
    assert c["heads"] == ["cs_top"] and set(c["nodes"]) == {"ls_p", "cs_a", "cs_top"}
    assert c["nodes"]["cs_top"]["claim"].startswith("TOP principle") and "claim" not in c["nodes"]["cs_a"]


def _concrete_exemplifies(c):
    assert c["start_resolved_id"] == "ob_c" and c["roots"] == ["ls_p"] and c["heads"] == ["cs_top"]


def _concrete_instantiates(c):
    assert c["roots"] == ["ls_m"] and c["heads"] == ["cs_top"]


def _superseded_rung(c):
    assert "cs_new" in c["nodes"] and "cs_old" not in c["nodes"]
    assert c["nodes"]["cs_new"]["original_id"] == "cs_old" and c["heads"] == ["cs_new"]
    assert "RETIRED" not in json.dumps(c)


def _superseded_start(c):
    assert c["start_id"] == "ls_old2" and c["start_resolved_id"] == "ls_s" and "ls_s" in c["nodes"]


def _multi_parent(c):
    assert sorted(c["nodes"]["ls_m"]["parents"]) == ["cs_a", "cs_b"]
    assert list(c["nodes"]).count("cs_top") == 1 and c["heads"] == ["cs_top"]


def _truncated(c):
    assert c["truncated"] is True and c["heads"] == []


def _truncated_shallow(c):
    assert c["truncated"] is True


def _full_depth(c):
    assert c["truncated"] is False and c["heads"] == ["g0"] and len(c["nodes"]) == 4


def _cycle(c):
    assert c["cycle_detected"] is True and c["heads"] == []


def _missing(c):
    assert c["start_resolved_id"] is None and c["nodes"] == {} and c["heads"] == []
    assert c["cycle_detected"] is False  # the documented key is present (KeyError otherwise)


def _dead_end(c):
    assert c["start_resolved_id"] is None and c["nodes"] == {} and c["roots"] == []


EQUALITY_CASES = [
    pytest.param("ls_p", 4, _principle, id="a-principle-start"),
    pytest.param("ob_c", 4, _concrete_exemplifies, id="b-concrete-exemplifies"),
    pytest.param("ob_i", 4, _concrete_instantiates, id="b-concrete-instantiates"),
    pytest.param("ls_s", 4, _superseded_rung, id="c-supersede-resolved-rung"),
    pytest.param("ls_old2", 4, _superseded_start, id="c-supersede-resolved-start"),
    pytest.param("ls_m", 4, _multi_parent, id="d-multiple-parents"),
    pytest.param("g6", 4, _truncated, id="e-depth-truncation-default-cap"),
    pytest.param("g6", 2, _truncated_shallow, id="e-depth-truncation-depth-2"),
    pytest.param("g3", 4, _full_depth, id="e-no-truncation-when-it-fits"),
    pytest.param("cy1", 4, _cycle, id="f-cycle"),
    pytest.param("nope", 4, _missing, id="g-missing-start"),
    pytest.param("ds_dead", 4, _dead_end, id="h-dead-end-supersede"),
    pytest.param("ds_dangling", 4, _dead_end, id="h-dangling-supersede"),
]


@pytest.mark.parametrize("node_id,max_depth,shape", EQUALITY_CASES)
@requires_linux_owner
def test_chain_equals_a_direct_generalization_chain_on_the_same_store(chain_owner, node_id, max_depth, shape):
    """For every fixture the owner's result == engram_core.generalization_chain(direct_conn,
    ...) on the same store, JSON-normalised. `shape` pins what the ORACLE looks like so a
    fixture that stopped exercising its case cannot pass vacuously.
    Mutation M_REIMPLEMENT (the owner returns a hand-rolled walk, e.g. direct parents only)
    -> the multi-rung / supersede / cycle / truncation cases go red."""
    oracle = direct_chain(chain_owner.store_path, node_id, max_depth)
    shape(oracle)
    result = _chain(chain_owner, node_id, max_depth)
    assert isinstance(result, ec.Ok), result
    assert result.result == oracle


@pytest.mark.parametrize("node_id,max_depth,shape", EQUALITY_CASES)
@requires_linux_owner
def test_chain_result_has_the_same_key_order_as_the_direct_result(chain_owner, node_id, max_depth, shape):
    """Equality by construction includes ORDER: `dict ==` ignores key order, but the hook renders
    non-head nodes sorted by depth with a STABLE sort, so same-depth nodes appear in the `nodes`
    mapping's insertion order. Compare the serialised text (order at every level).
    Mutations: sort the result's top-level keys; reorder the `nodes` mapping; `sort_keys=True` on
    the wire text -- each passed every dict-equality cell -> red here."""
    oracle = direct_chain(chain_owner.store_path, node_id, max_depth)
    shape(oracle)
    result = _chain(chain_owner, node_id, max_depth)
    assert isinstance(result, ec.Ok), result
    assert json.dumps(result.result) == json.dumps(oracle)


def test_chain_missing_start_direct_result_carries_cycle_detected_false(home):
    """#3740 contract fix: the documented `cycle_detected` key is present (and False -- the node
    set is empty) on the missing-start early return of the core function too.
    Mutation: drop the key from that return -> KeyError/False-ness lost -> red."""
    got = direct_chain(chain_store(home), "nope", 4)
    assert "cycle_detected" in got and got["cycle_detected"] is False, got
    assert got["nodes"] == {} and got["start_resolved_id"] is None


def _core_chain_raw(home, node_id, max_depth):
    """engram_core.generalization_chain called DIRECTLY (no owner, no JSON round trip), so the
    core function's own depth contract is what is exercised."""
    conn = sqlite3.connect(chain_store(home).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return engram_core.generalization_chain(conn, node_id, max_depth)
    finally:
        conn.close()


@pytest.mark.parametrize("bad", [
    pytest.param(-1, id="minus-1"),
    pytest.param(-(10 ** 30), id="huge-negative"),
    pytest.param(True, id="bool-true"),
    pytest.param(False, id="bool-false"),
    pytest.param(2.0, id="float"),
    pytest.param("4", id="str"),
    pytest.param(None, id="none"),
])
def test_core_generalization_chain_refuses_a_negative_or_non_int_depth(home, bad):
    """#3740 design ruling: the CORE function refuses (ValueError) a negative or non-int
    max_depth (bool excluded), instead of returning nodes=1/truncated=True for -1 -- a false
    statement. Mutation: remove the core check -> -1 returns a truncated stub, True/False
    count as ints 1/0 -> red here, while the owner op's own named refusals stay green."""
    with pytest.raises(ValueError):
        _core_chain_raw(home, "ls_p", bad)


def test_core_generalization_chain_depth_zero_is_roots_only(home):
    """0 stays valid: roots only. Mutation: tighten the check to `<= 0` -> ValueError -> red."""
    got = _core_chain_raw(home, "ls_p", 0)
    assert list(got["nodes"]) == got["roots"] == ["ls_p"], got
    assert got["nodes"]["ls_p"]["depth"] == 0


@requires_linux_owner
def test_chain_missing_start_owner_reply_carries_cycle_detected_false(chain_owner):
    """Same contract on the owner `chain` reply for a missing node id.
    Mutation: remove the added key in engram_core's early return -> red."""
    result = _chain(chain_owner, "nope", 4)
    assert isinstance(result, ec.Ok), result
    assert "cycle_detected" in result.result and result.result["cycle_detected"] is False, result.result
    assert result.result["nodes"] == {} and result.result["heads"] == []


@requires_linux_owner
def test_chain_runs_engram_cores_function_on_the_client_read_connection(chain_owner, monkeypatch):
    """The owner CALLS engram_core.generalization_chain itself, with the client-read
    connection, the requested node id and the (clamped) depth -- not a copy of the walk."""
    seen = []
    real = engram_core.generalization_chain

    def spy(conn, node_id, max_depth=4):
        seen.append((conn is chain_owner._client_read_conn, node_id, max_depth))
        return real(conn, node_id, max_depth)

    monkeypatch.setattr(engram_core, "generalization_chain", spy)
    assert isinstance(_chain(chain_owner, "ls_p", 3), ec.Ok)
    assert seen == [(True, "ls_p", 3)]


@requires_linux_owner
def test_chain_max_depth_above_the_cap_is_clamped_not_rejected(chain_owner):
    """max_depth=99 equals the cap-4 result on a chain deeper than 4, and the function is
    called with 4. Mutation M_CLAMP_OFF (pass 99 through) -> the 7-rung chain is walked
    in full -> the result differs from the cap-4 one -> red. M_CLAMP_REJECT (reject above
    the cap) -> Rejected -> red."""
    at_cap = _chain(chain_owner, "g6", 4)
    huge = _chain(chain_owner, "g6", 99)
    assert isinstance(at_cap, ec.Ok) and at_cap.result["truncated"] is True and len(at_cap.result["nodes"]) == 5
    assert isinstance(huge, ec.Ok), huge
    assert huge.result == at_cap.result
    assert huge.result == direct_chain(chain_owner.store_path, "g6", 4)
    vast = _chain(chain_owner, "g6", 10 ** 30)
    assert isinstance(vast, ec.Ok) and vast.result == at_cap.result


@requires_linux_owner
def test_chain_clamp_reaches_the_function_as_the_cap(chain_owner, monkeypatch):
    seen = []
    real = engram_core.generalization_chain

    def spy(conn, node_id, max_depth=4):
        seen.append(max_depth)
        return real(conn, node_id, max_depth)

    monkeypatch.setattr(engram_core, "generalization_chain", spy)
    for depth in (99, 5, 4, 3, 0):
        assert isinstance(_chain(chain_owner, "g6", depth), ec.Ok)
    assert seen == [4, 4, 4, 3, 0]


@requires_linux_owner
def test_chain_depth_zero_is_served(chain_owner):
    result = _chain(chain_owner, "ls_p", 0)
    assert isinstance(result, ec.Ok) and result.result == direct_chain(chain_owner.store_path, "ls_p", 0)


# --- per-reason rejection ---------------------------------------------------------------

REASON_CASES = [
    pytest.param(None, "chain_params_shape", id="params-none"),
    pytest.param(["ls_p", 4], "chain_params_shape", id="params-list"),
    pytest.param("ls_p", "chain_params_shape", id="params-str"),
    pytest.param({}, "chain_params_shape", id="params-empty"),
    pytest.param({"node_id": "ls_p"}, "chain_params_shape", id="max-depth-missing"),
    pytest.param({"max_depth": 4}, "chain_params_shape", id="node-id-missing"),
    pytest.param({"node_id": "ls_p", "max_depth": 4, "extra": 1}, "chain_params_shape", id="extra-key"),
    pytest.param({"node_id": 5, "max_depth": 4}, "chain_node_id_not_str", id="node-id-int"),
    pytest.param({"node_id": None, "max_depth": 4}, "chain_node_id_not_str", id="node-id-none"),
    pytest.param({"node_id": True, "max_depth": 4}, "chain_node_id_not_str", id="node-id-bool"),
    pytest.param({"node_id": 1.5, "max_depth": 4}, "chain_node_id_not_str", id="node-id-float"),
    pytest.param({"node_id": ["ls_p"], "max_depth": 4}, "chain_node_id_not_str", id="node-id-list"),
    pytest.param({"node_id": {"a": 1}, "max_depth": 4}, "chain_node_id_not_str", id="node-id-dict"),
    pytest.param({"node_id": "", "max_depth": 4}, "chain_node_id_empty", id="node-id-empty"),
    pytest.param({"node_id": "a" * 257, "max_depth": 4}, "chain_node_id_too_long", id="node-id-257-bytes"),
    pytest.param({"node_id": "x" * 100000, "max_depth": 4}, "chain_node_id_too_long", id="node-id-huge"),
    pytest.param({"node_id": "é" * 129, "max_depth": 4}, "chain_node_id_too_long", id="node-id-129-two-byte-chars"),
    pytest.param({"node_id": "\ud800", "max_depth": 4}, "chain_node_id_not_utf8", id="node-id-lone-surrogate"),
    pytest.param({"node_id": "ls_p", "max_depth": True}, "chain_max_depth_not_int", id="depth-true"),
    pytest.param({"node_id": "ls_p", "max_depth": False}, "chain_max_depth_not_int", id="depth-false"),
    pytest.param({"node_id": "ls_p", "max_depth": 4.0}, "chain_max_depth_not_int", id="depth-float"),
    pytest.param({"node_id": "ls_p", "max_depth": 2.5}, "chain_max_depth_not_int", id="depth-fraction"),
    pytest.param({"node_id": "ls_p", "max_depth": "4"}, "chain_max_depth_not_int", id="depth-str"),
    pytest.param({"node_id": "ls_p", "max_depth": None}, "chain_max_depth_not_int", id="depth-none"),
    pytest.param({"node_id": "ls_p", "max_depth": [4]}, "chain_max_depth_not_int", id="depth-list"),
    pytest.param({"node_id": "ls_p", "max_depth": -1}, "chain_max_depth_negative", id="depth-minus-1"),
    pytest.param({"node_id": "ls_p", "max_depth": -(10 ** 30)}, "chain_max_depth_negative", id="depth-huge-negative"),
]

ORDER_CASES = [
    pytest.param({"node_id": 5, "max_depth": "x", "extra": 1}, "chain_params_shape", id="shape-before-node-id"),
    pytest.param({"node_id": 5, "max_depth": "x"}, "chain_node_id_not_str", id="node-id-type-before-depth-type"),
    pytest.param({"node_id": "", "max_depth": -1}, "chain_node_id_empty", id="empty-before-negative-depth"),
    pytest.param({"node_id": "a" * 300, "max_depth": "x"}, "chain_node_id_too_long", id="too-long-before-depth-type"),
    pytest.param({"node_id": "\ud800" * 300, "max_depth": 4}, "chain_node_id_too_long", id="too-long-before-not-utf8"),
    pytest.param({"node_id": "\ud800", "max_depth": "x"}, "chain_node_id_not_utf8", id="not-utf8-before-depth-type"),
    pytest.param({"node_id": "ls_p", "max_depth": "x"}, "chain_max_depth_not_int", id="depth-type-before-negative"),
    pytest.param({"node_id": "ls_p", "max_depth": True}, "chain_max_depth_not_int", id="bool-is-not-an-int"),
]


@pytest.mark.parametrize("params,reason", REASON_CASES + ORDER_CASES)
@requires_linux_owner
def test_chain_each_violation_has_its_own_named_reason(chain_owner, params, reason):
    """Each violation -> Rejected with exactly its own reason, a detail and a remedy.
    Mutation M_ANY_REASON_COLLAPSE (two reasons returned as one) -> the collapsed pair's
    cells go red. Also checks the owner is healthy afterwards (`ping`)."""
    result = ec.read("chain", params, timeout=3.0, socket_path=chain_owner.socket_path)
    assert isinstance(result, ec.Rejected), result
    assert result.reason == reason
    assert result.detail and result.remedy
    assert _ping_ok(chain_owner)


@pytest.mark.parametrize("params,reason", REASON_CASES + ORDER_CASES)
@requires_linux_owner
def test_chain_param_refusal_prepares_nothing_on_the_store_connection(chain_owner, params, reason):
    """Validation is FIRST: a refused request prepares nothing on the store connection
    (the authorizer, called once per prepared statement, is never called).
    Mutation M_VALIDATE_AFTER_EXECUTE -> the authorizer sees the walk's statements -> red."""
    calls = _recording(chain_owner)
    result = ec.read("chain", params, timeout=3.0, socket_path=chain_owner.socket_path)
    assert isinstance(result, ec.Rejected) and result.reason == reason, result
    assert calls == [], "a refused chain must prepare nothing on the store connection"
    assert _ping_ok(chain_owner)


@requires_linux_owner
def test_chain_a_valid_request_does_prepare_statements(chain_owner):
    """Anti-vacuity for the prepares-nothing cell: the recorder does see a served request."""
    calls = _recording(chain_owner)
    assert isinstance(_chain(chain_owner, "ls_p"), ec.Ok)
    assert calls


@requires_linux_owner
def test_chain_node_id_boundary_is_measured_in_utf8_bytes_not_characters(chain_owner):
    """256 bytes is served (here: an id that is not in the store -> an empty chain), 257 is
    too long; 128 two-byte characters = 256 bytes is served although 129 would be fewer than
    256 CHARACTERS yet 258 bytes. Mutation M_BYTES_NOT_CHARS (len(node_id) instead of the
    encoded length) -> the 129-character id is served -> red."""
    cap = eo.CHAIN_NODE_ID_MAX_BYTES
    assert isinstance(_chain(chain_owner, "a" * cap), ec.Ok)
    over = _chain(chain_owner, "a" * (cap + 1))
    assert isinstance(over, ec.Rejected) and over.reason == "chain_node_id_too_long"
    two_byte = "é" * (cap // 2)
    assert len(two_byte.encode("utf-8")) == cap and len(two_byte) < cap
    assert isinstance(_chain(chain_owner, two_byte), ec.Ok)
    over2 = "é" * (cap // 2 + 1)
    assert len(over2) <= cap < len(over2.encode("utf-8"))
    r = _chain(chain_owner, over2)
    assert isinstance(r, ec.Rejected) and r.reason == "chain_node_id_too_long"


@requires_linux_owner
def test_chain_node_id_is_bound_data_never_sql(chain_owner):
    """A hostile id reaches SQL only as a bound parameter of the walk's fixed statements:
    served as a missing node, nothing else read, nothing mutated."""
    hostile = "x') OR 1=1 --"
    seen = []
    chain_owner._client_read_conn.set_trace_callback(seen.append)
    try:
        result = _chain(chain_owner, hostile)
    finally:
        chain_owner._client_read_conn.set_trace_callback(None)
    assert isinstance(result, ec.Ok) and result.result["start_resolved_id"] is None
    top_level = [s for s in seen if not s.startswith("-- ")]
    assert top_level == ["SELECT id, type FROM nodes WHERE id = '%s'" % hostile.replace("'", "''")], top_level


# --- result bound ---------------------------------------------------------------------------

@requires_linux_owner
def test_chain_oversized_result_is_a_named_rejection_never_a_truncation(home, monkeypatch):
    """A head whose claim is large: with the bound lowered, the reply is
    Rejected(chain_result_too_large), carries detail + remedy, and the owner stays healthy.
    Mutation M_SIZE_TRUNCATE (shorten the claim to fit) -> an Ok with a short claim -> red;
    M_SIZE_OFF (no bound) -> an Ok -> red."""
    chain_store(home, head_claim="Z" * 5000)
    owner = _start(home)
    try:
        small = _chain(owner, "ls_p")
        assert isinstance(small, ec.Ok) and len(small.result["nodes"]["cs_top"]["claim"]) == 5000
        monkeypatch.setattr(eo, "CHAIN_RESULT_MAX_BYTES", 2000)
        result = _chain(owner, "ls_p")
        assert isinstance(result, ec.Rejected), result
        assert result.reason == "chain_result_too_large"
        assert result.detail and result.remedy
        assert _ping_ok(owner)
    finally:
        owner.stop()


@requires_linux_owner
def test_chain_result_bound_boundary_counts_the_exact_reply_bytes(home, monkeypatch):
    """The bound is on the serialised REPLY (the exact json.dumps of {"status","result"}):
    a bound equal to that length serves, one byte less refuses."""
    chain_store(home)
    owner = _start(home)
    try:
        oracle = direct_chain(owner.store_path, "ls_p", 4)
        reply_len = len(json.dumps({"status": "ok", "result": oracle}))
        monkeypatch.setattr(eo, "CHAIN_RESULT_MAX_BYTES", reply_len)
        assert isinstance(_chain(owner, "ls_p"), ec.Ok)
        monkeypatch.setattr(eo, "CHAIN_RESULT_MAX_BYTES", reply_len - 1)
        r = _chain(owner, "ls_p")
        assert isinstance(r, ec.Rejected) and r.reason == "chain_result_too_large"
    finally:
        owner.stop()


@requires_linux_owner
def test_chain_the_real_bound_refuses_a_genuinely_oversized_chain(home):
    """With the REAL constant: five heads of ~1.9 MB (each under the connection's per-value
    length limit) sum past 8 MiB -> chain_result_too_large; the same store's small chain
    is still served."""
    chain_store(home)
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, memory_status)"
                 " VALUES ('big_root', 'lesson', 'start', '2026-01-01T00:00:00', '{}', 1, 'active')")
    for i in range(5):
        conn.execute("INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, memory_status)"
                     " VALUES (?, 'cornerstone', ?, '2026-01-01T00:00:00', '{}', 1, 'active')",
                     (f"big_head{i}", "B" * 1_900_000))
        conn.execute("INSERT INTO edges (source_id, target_id, relation, created_at)"
                     " VALUES (?, 'big_root', 'generalizes', '2026-01-01T00:00:00')", (f"big_head{i}",))
    conn.commit()
    conn.close()
    owner = _start(home, request_deadline=20.0)
    try:
        assert 5 * 1_900_000 > eo.CHAIN_RESULT_MAX_BYTES
        result = _chain(owner, "big_root", timeout=30.0)
        assert isinstance(result, ec.Rejected), type(result)
        assert result.reason == "chain_result_too_large"
        assert isinstance(_chain(owner, "ls_p"), ec.Ok)
    finally:
        owner.stop()


# --- routing ------------------------------------------------------------------------------

class _Tripwire:
    """Stand-in for `_owner_conn` / `_owner_lock`: any use fails loudly."""

    def __getattr__(self, name):
        if name == "close":
            return lambda: None
        raise AssertionError(f"`chain` touched an owner-internal object: .{name}")

    def __enter__(self):
        raise AssertionError("`chain` took the owner-internal lock")

    def __exit__(self, *exc):
        return False


@requires_linux_owner
def test_chain_executes_on_the_client_read_connection_only(chain_owner):
    """Mutation M_OWNER_CONN (run the walk on `_owner_conn` / under `_owner_lock`) -> the
    stand-ins raise -> internal_error / no preparation on the client-read connection -> red."""
    calls = _recording(chain_owner)
    original_conn, original_lock = chain_owner._owner_conn, chain_owner._owner_lock
    chain_owner._owner_conn = _Tripwire()
    chain_owner._owner_lock = _Tripwire()
    try:
        result = _chain(chain_owner, "ls_p")
    finally:
        chain_owner._owner_conn, chain_owner._owner_lock = original_conn, original_lock
    assert isinstance(result, ec.Ok), result
    assert result.result == direct_chain(chain_owner.store_path, "ls_p", 4)
    assert calls, "the walk must have been prepared on the client-read connection"


@requires_linux_owner
def test_chain_statements_all_pass_the_client_read_authorizer_floor(chain_owner):
    """The walk's own fixed statements are all admitted by the permanent authorizer floor:
    none of the recorded actions is denied (a denial would surface as read_failed)."""
    seen = []

    def recording(*a):
        verdict = eo._client_read_authorizer(*a)
        seen.append(verdict)
        return verdict

    chain_owner._client_read_conn.set_authorizer(recording)
    result = _chain(chain_owner, "ls_p")
    assert isinstance(result, ec.Ok), result
    assert seen and all(v == sqlite3.SQLITE_OK for v in seen)


# --- method table / displacement guard -------------------------------------------------------

@requires_linux_owner
def test_chain_is_in_the_method_table_displacement_guarded_with_no_registration_verb(chain_owner):
    """Mutation M_NO_GUARD (chain added to NO_STORE_METHODS, or dropped from `_methods`)
    -> red. No registration verb exists on the dispatch surface."""
    assert "chain" in chain_owner._methods
    assert "chain" not in eo.NO_STORE_METHODS
    assert {"ping", "snapshot", "read", "df", "chain", "confidence_by_type"} == set(chain_owner._methods)
    assert not [m for m in chain_owner._methods if re.search(r"regist", m, re.I)]
    tree = ast.parse(Path(eo.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_dispatch")
    lits = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not [v for v in lits if re.search(r"regist", v, re.I)], lits


@requires_linux_owner
def test_chain_is_displacement_guarded(chain_owner, monkeypatch):
    import engram_walguard

    def displaced(path):
        raise engram_walguard.SubstrateDisplaced({"reason": "test"}, "n/a")

    monkeypatch.setattr(engram_walguard, "assert_not_displaced", displaced)
    result = _chain(chain_owner)
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "substrate_displaced"


@requires_linux_owner
def test_chain_leaves_read_and_df_behaviour_unchanged(chain_owner):
    """The other store-touching methods still answer as before on the same owner."""
    rows = ec.read("read", {"sql": "SELECT is_current FROM nodes WHERE id = ?", "args": ["ls_p"]},
                   timeout=3.0, socket_path=chain_owner.socket_path)
    assert isinstance(rows, ec.Ok) and rows.result == [[1]]
    bad = ec.read("read", {"sql": "SELECT 1", "args": []}, timeout=3.0, socket_path=chain_owner.socket_path)
    assert isinstance(bad, ec.Rejected) and bad.reason == "unregistered_template"
    df = ec.read("df", {"terms": []}, timeout=3.0, socket_path=chain_owner.socket_path)
    assert isinstance(df, ec.Rejected) and df.reason == "df_terms_empty"


def test_chain_does_not_widen_the_typed_statement_set():
    """`chain` runs engram_core's own fixed code; it adds NO client text to the typed
    statements the gate prepares (`_TYPED_STATEMENTS` is still exactly df's two)."""
    assert eo._TYPED_STATEMENTS == frozenset({eo.DF_SQL, eo.DF_VOCAB_DDL_SQL})


@requires_linux_owner
def test_chain_additive_contract_version_stays_1_1(chain_owner):
    assert ec.CONTRACT_VERSION == "1.1"
    assert chain_owner.contract_version == "1.1"


# --- deadline --------------------------------------------------------------------------------

@requires_linux_owner
def test_chain_waiting_for_the_client_read_lock_stops_at_the_deadline(chain_owner):
    lock = chain_owner._client_read_lock
    assert lock.acquire(timeout=1.0)
    try:
        start = time.monotonic()
        result = _chain(chain_owner, timeout=3.0)
        elapsed = time.monotonic() - start
    finally:
        lock.release()
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "deadline_exceeded"
    assert elapsed < chain_owner.request_deadline + 0.5, elapsed


@requires_linux_owner
def test_chain_with_no_time_left_inside_the_deadline_is_deadline_exceeded(home):
    chain_store(home)
    owner = _start(home, request_deadline=eo.READ_RESPONSE_RESERVE / 2)
    try:
        result = _chain(owner, timeout=3.0)
    finally:
        owner.stop()
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "deadline_exceeded"


@requires_linux_owner
def test_chain_walk_is_armed_with_the_request_deadline_and_aborts_at_it(chain_owner, monkeypatch):
    """The walk runs with the progress handler armed against the execution deadline
    (request deadline minus the delivery reserve). Forcing an already-expired deadline at
    a one-instruction tick aborts the very first statement -> deadline_exceeded."""
    armed = []
    real = eo._arm_read_deadline

    def spy(conn, deadline, *, n=eo.PROGRESS_HANDLER_INSTRUCTION_COUNT):
        armed.append((conn is chain_owner._client_read_conn, deadline))
        return real(conn, deadline, n=n)

    monkeypatch.setattr(eo, "_arm_read_deadline", spy)
    before = time.monotonic()
    assert isinstance(_chain(chain_owner), ec.Ok)
    assert armed and all(is_client for is_client, _ in armed)
    assert before < armed[0][1] <= before + chain_owner.request_deadline

    def expired(conn, deadline, *, n=eo.PROGRESS_HANDLER_INSTRUCTION_COUNT):
        return real(conn, time.monotonic() - 1.0, n=1)

    monkeypatch.setattr(eo, "_arm_read_deadline", expired)
    result = _chain(chain_owner)
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "deadline_exceeded"
    monkeypatch.setattr(eo, "_arm_read_deadline", real)
    assert isinstance(_chain(chain_owner), ec.Ok), "the expired handler must not outlive its request"


# --- store-side and error mapping --------------------------------------------------------------

@pytest.mark.parametrize("with_nodes", [False, True], ids=["no-tables", "nodes-only"])
@requires_linux_owner
def test_chain_store_without_the_walk_tables_is_a_named_read_failed(home, with_nodes):
    """Follows what `df` does for a missing table: a named rejection, never a crash, and
    the owner stays healthy."""
    bare_store(home, with_nodes=with_nodes)
    owner = _start(home)
    try:
        result = _chain(owner, "ls_x")
        assert isinstance(result, ec.Rejected), result
        assert result.reason == "read_failed"
        assert result.detail and result.remedy
        assert _ping_ok(owner)
    finally:
        owner.stop()


@pytest.mark.parametrize("exc,reason", [
    pytest.param(sqlite3.OperationalError("database is locked"), "read_failed", id="operational-error"),
    pytest.param(sqlite3.DataError("string or blob too big"), "read_failed", id="data-error"),
    pytest.param(sqlite3.OperationalError("interrupted"), "deadline_exceeded", id="interrupt-by-message"),
])
@requires_linux_owner
def test_chain_sqlite_errors_map_to_their_named_reasons(chain_owner, monkeypatch, exc, reason):
    def boom(conn, node_id, max_depth=4):
        raise exc

    monkeypatch.setattr(engram_core, "generalization_chain", boom)
    result = _chain(chain_owner)
    assert isinstance(result, ec.Rejected) and result.reason == reason, result


@requires_linux_owner
def test_chain_interrupt_by_errorcode_is_deadline_exceeded(chain_owner, monkeypatch):
    err = sqlite3.OperationalError("some other text")
    err.sqlite_errorcode = getattr(sqlite3, "SQLITE_INTERRUPT", 9)

    def boom(conn, node_id, max_depth=4):
        raise err

    monkeypatch.setattr(engram_core, "generalization_chain", boom)
    result = _chain(chain_owner)
    assert isinstance(result, ec.Rejected) and result.reason == "deadline_exceeded", result


@requires_linux_owner
def test_chain_with_no_client_read_gate_is_owner_stopping_but_validation_wins(chain_owner):
    real = chain_owner._client_read_gate
    chain_owner._client_read_gate = None
    try:
        wire = chain_owner._handle_chain({"node_id": "ls_p", "max_depth": 4}, time.monotonic() + 5.0)
        assert wire["status"] == "rejected" and wire["reason"] == "owner_stopping", wire
        invalid = chain_owner._handle_chain({"node_id": "", "max_depth": 4}, time.monotonic() + 5.0)
        assert invalid["reason"] == "chain_node_id_empty", invalid
    finally:
        chain_owner._client_read_gate = real


@requires_linux_owner
def test_chain_without_engram_core_in_the_runtime_is_a_named_rejection(chain_owner, monkeypatch):
    """A partial install: engram_core cannot be imported by the owner -> a NAMED rejection
    (not internal_error / an owner-bug remedy) and the owner stays healthy."""
    monkeypatch.setitem(sys.modules, "engram_core", None)
    result = _chain(chain_owner)
    assert isinstance(result, ec.Rejected), result
    assert result.reason == "chain_runtime_unavailable"
    assert result.detail and result.remedy
    monkeypatch.undo()
    assert _ping_ok(chain_owner)


# --- importing engram_core in the owner has no side effects -----------------------------------

def test_the_owner_does_not_import_engram_core_at_module_import():
    """Lazy: engram_core is large and must not slow the owner's startup. Checked in a fresh
    interpreter (this process already has engram_core loaded)."""
    import subprocess

    code = ("import sys; sys.path.insert(0, %r); import engramd_owner; "
            "print('engram_core' in sys.modules)" % _SRC_ENGRAM)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_importing_engram_core_opens_no_store_and_creates_no_files(tmp_path):
    """The owner imports engram_core lazily at the first `chain`; that import must open no
    database and create nothing under ENGRAM_HOME (checked in a fresh interpreter)."""
    import os
    import subprocess

    code = (
        "import os, sqlite3, sys\n"
        "sys.path.insert(0, %r)\n"
        "calls = []\n"
        "orig = sqlite3.connect\n"
        "def spy(*a, **k):\n"
        "    calls.append(a)\n"
        "    return orig(*a, **k)\n"
        "sqlite3.connect = spy\n"
        "import engram_core\n"
        "print(len(calls), sorted(os.listdir(os.environ['ENGRAM_HOME'])))\n" % _SRC_ENGRAM
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "CLAUDE_PLUGIN_ROOT")}
    env["ENGRAM_HOME"] = str(tmp_path)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0 []", out.stdout


# =========================================================================================
# #3740 Cut A round 2
# =========================================================================================

class _HandlerRecordingConnection:
    """A proxy over a real sqlite3.Connection that records every `set_progress_handler` call
    (the C type's methods cannot be monkeypatched) and delegates everything else. `armed` is
    the handler state after the last call: None = no handler on the connection."""

    def __init__(self, conn):
        self._real = conn
        self.handler_calls = []

    def set_progress_handler(self, handler, n):
        self.handler_calls.append((handler, n))
        return self._real.set_progress_handler(handler, n)

    @property
    def armed(self):
        return self.handler_calls[-1][0] if self.handler_calls else None

    def __getattr__(self, name):
        return getattr(self._real, name)


def _proxy_gate(home):
    db = chain_store(home)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    proxy = _HandlerRecordingConnection(conn)
    return conn, proxy, eo._ClientReadGate(proxy, home=home)


def test_r2_run_chain_leaves_no_progress_handler_armed_after_it_returns(home):
    """R2: `run_chain` arms the request deadline on the connection and MUST clear it, or the
    next call inherits a deadline that has already passed. After a normal return the last
    `set_progress_handler` call installed no handler (and one was armed during the walk).
    Mutation M_NO_CLEAR (drop the clear in the `finally`) -> the armed handler survives -> red."""
    conn, proxy, gate = _proxy_gate(home)
    try:
        result = gate.run_chain("ls_p", 4, deadline=time.monotonic() + 30.0)
        assert result["start_resolved_id"] == "ls_p"
        assert proxy.handler_calls and proxy.handler_calls[0][0] is not None, "the walk was never armed"
        assert proxy.armed is None, "a progress handler is still armed after run_chain returned"
    finally:
        conn.close()


def test_r2_run_chain_leaves_no_progress_handler_armed_after_it_raises(home, monkeypatch):
    """R2, the raising path: the walk fails (a SQLite error) and the handler is still cleared
    by the `finally`. Mutation M_NO_CLEAR -> red."""
    conn, proxy, gate = _proxy_gate(home)

    def boom(c, node_id, max_depth=4):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(engram_core, "generalization_chain", boom)
    try:
        with pytest.raises(sqlite3.OperationalError):
            gate.run_chain("ls_p", 4, deadline=time.monotonic() + 30.0)
        assert proxy.handler_calls and proxy.handler_calls[0][0] is not None, "the walk was never armed"
        assert proxy.armed is None, "a progress handler is still armed after run_chain raised"
    finally:
        conn.close()


# --- R4: engram_core is imported OUTSIDE the client-read lock, after validation --------------------

def _unstarted_owner(home):
    """An EngramdOwner that never starts (no socket, no thread) with a client-read gate over a
    real connection to the fixture store: `_handle_chain` is a plain method call on it."""
    db = chain_store(home)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    owner = eo.EngramdOwner(home)
    owner._client_read_gate = eo._ClientReadGate(conn, home=home)
    return owner, conn


class _AccessorSpy:
    """Counts calls to `engramd_owner._engram_core`, noting whether the client-read lock was
    held at each call; delegates to the real accessor unless `raises` is set."""

    def __init__(self, owner, raises=None):
        self.owner, self.raises = owner, raises
        self.locked_at_call = []

    def __call__(self, home):
        self.locked_at_call.append(self.owner._client_read_lock.locked())
        if self.raises is not None:
            raise self.raises
        import engram_core as core

        return core


def test_r4_engram_core_is_imported_before_the_client_read_lock_is_taken(home, monkeypatch):
    """R4: the first chain request imports engram_core OUTSIDE `_client_read_lock`, so a cold
    import cannot block `read` / `df`. The accessor's FIRST call sees the lock free.
    Mutation M_IMPORT_IN_LOCK (the import back inside the lock, i.e. only `run_chain` calls
    the accessor) -> the first call sees the lock held -> red."""
    owner, conn = _unstarted_owner(home)
    spy = _AccessorSpy(owner)
    monkeypatch.setattr(eo, "_engram_core", spy)
    try:
        wire = owner._handle_chain({"node_id": "ls_p", "max_depth": 4}, time.monotonic() + 5.0)
    finally:
        conn.close()
    assert wire["status"] == "ok", wire
    assert spy.locked_at_call, "the accessor was never called"
    assert spy.locked_at_call[0] is False, "engram_core was imported while the client-read lock was held"


def test_r4_a_request_that_waits_for_the_lock_has_already_imported_engram_core(home, monkeypatch):
    """R4, behavioural: another thread holds the client-read lock, so the request can only
    time out waiting for it -- and the import has ALREADY happened (before the wait), so the
    cold import was not charged to the locked section. Mutation M_IMPORT_IN_LOCK -> the
    request never reaches the import (no calls) -> red."""
    owner, conn = _unstarted_owner(home)
    spy = _AccessorSpy(owner)
    monkeypatch.setattr(eo, "_engram_core", spy)
    assert owner._client_read_lock.acquire(timeout=1.0)
    try:
        wire = owner._handle_chain(
            {"node_id": "ls_p", "max_depth": 4}, time.monotonic() + eo.READ_RESPONSE_RESERVE + 0.2)
    finally:
        owner._client_read_lock.release()
        conn.close()
    assert wire["status"] == "rejected" and wire["reason"] == "deadline_exceeded", wire
    assert len(spy.locked_at_call) == 1, spy.locked_at_call


def test_r4_a_failed_import_answers_chain_runtime_unavailable_and_acquires_no_lock(home, monkeypatch):
    """R4: the import fails (a partial install) -> the named `chain_runtime_unavailable`, and the
    client-read lock was never acquired. Mutation (the lock is taken first, then the import
    fails inside it) -> an acquire is recorded -> red."""
    owner, conn = _unstarted_owner(home)

    class CountingLock:
        def __init__(self):
            self.acquires = 0
            self._lock = threading.Lock()

        def acquire(self, *a, **k):
            self.acquires += 1
            return self._lock.acquire(*a, **k)

        def release(self):
            self._lock.release()

        def locked(self):
            return self._lock.locked()

    lock = CountingLock()
    owner._client_read_lock = lock
    spy = _AccessorSpy(owner, raises=ImportError("no engram_core"))
    monkeypatch.setattr(eo, "_engram_core", spy)
    try:
        wire = owner._handle_chain({"node_id": "ls_p", "max_depth": 4}, time.monotonic() + 5.0)
    finally:
        conn.close()
    assert wire["status"] == "rejected" and wire["reason"] == "chain_runtime_unavailable", wire
    assert wire["detail"] and wire["remedy"]
    assert lock.acquires == 0, "the lock was taken although the import could not succeed"
    assert len(spy.locked_at_call) == 1


@pytest.mark.parametrize("params,reason", [
    pytest.param("not-a-dict", "chain_params_shape", id="shape"),
    pytest.param({"node_id": "", "max_depth": 4}, "chain_node_id_empty", id="empty-node"),
    pytest.param({"node_id": "x" * 300, "max_depth": 4}, "chain_node_id_too_long", id="too-long"),
    pytest.param({"node_id": "ls_p", "max_depth": -1}, "chain_max_depth_negative", id="negative-depth"),
])
def test_r4_a_refused_request_does_not_trigger_the_import(home, monkeypatch, params, reason):
    """R4: validation refusals import nothing. Mutation (import before validation) -> the
    accessor is called -> red."""
    owner, conn = _unstarted_owner(home)
    spy = _AccessorSpy(owner)
    monkeypatch.setattr(eo, "_engram_core", spy)
    try:
        wire = owner._handle_chain(params, time.monotonic() + 5.0)
    finally:
        conn.close()
    assert wire["reason"] == reason, wire
    assert spy.locked_at_call == [], "a refused request imported engram_core"


def test_r4_an_owner_with_no_gate_imports_nothing(home, monkeypatch):
    """R4 (a judgement call, pinned): `owner_stopping` is answered before the import, as it is
    before the lock -- nothing is served, so nothing is imported."""
    owner, conn = _unstarted_owner(home)
    owner._client_read_gate = None
    spy = _AccessorSpy(owner)
    monkeypatch.setattr(eo, "_engram_core", spy)
    try:
        wire = owner._handle_chain({"node_id": "ls_p", "max_depth": 4}, time.monotonic() + 5.0)
    finally:
        conn.close()
    assert wire["reason"] == "owner_stopping", wire
    assert spy.locked_at_call == []
