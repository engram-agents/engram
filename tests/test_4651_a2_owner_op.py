"""Contract cells for the #4651 part A slice A2: the owner op `publish_prepared_embeddings`.

Written BEFORE the op exists (contract-first). The op publishes already-encoded vectors through the ONE shared
primitive (`engram_embedding_publish.publish_embedding`, prepared mode) on the owner's connection, gated by the NEW
routing flag `engramd.repair_embeddings_via_owner` (default OFF). The flag is a ROUTING switch checked per call from the
owner's own config; consent to overwrite lives in the repair tool's preview-then-apply, not here.

Every store is built with the real schema under a temp dir. Real sqlite-vec must RUN here: a store without a loadable
vec0 is a hard failure, never a skip. The authorizer admissions are MEASURED (a recording authorizer on the real schema);
cell A2 re-measures at test time and compares against the recorded set below, so a change to the primitive reddens it
with the op's own code untouched.

Families: F flag and routing; V validation and wire; S strict semantics through the owner; E vec gate; R request id,
dedupe, read-back; A / T / D authorizer and pins; P parity with the in-process primitive; N naming and docstring.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import math
import os
import re
import shutil
import socket as _socket
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
if str(SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(SRC_ENGRAM))

import engram_core as core  # noqa: E402
import engram_embedding_publish as pub  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402
import sqlite_vec  # noqa: E402

KEY = "repair_embeddings_via_owner"
OTHER_KEY = "regen_embeddings_via_owner"
OP = "publish_prepared_embeddings"
MODEL = "all-MiniLM-L6-v2"
EPOCH = {"model": MODEL, "dimension": 384, "normalisation": "model-default"}
MARKER = "embedding_refresh_pending"
LEDGER = REPO_ROOT / "docs" / "internal" / "2652-migration-ledger.md"


def vec(seed: float) -> list:
    return [seed + i / 1000 for i in range(384)]


OLD = vec(7.0)


@pytest.fixture(autouse=True)
def _pin_modules(monkeypatch):
    """The owner imports engram_core / engram_embedding_publish by name at call time. Another test file may have
    dropped them from sys.modules, so pin the objects THIS file imported (and keep them coherent) for each cell."""
    global pub
    monkeypatch.setitem(sys.modules, "engram_core", core)
    if getattr(pub, "core", None) is not core:
        monkeypatch.delitem(sys.modules, "engram_embedding_publish", raising=False)
        pub = importlib.import_module("engram_embedding_publish")
    monkeypatch.setitem(sys.modules, "engram_embedding_publish", pub)
    monkeypatch.setenv("ENGRAM_NO_DB_GUARDS", "1")
    yield


# ---- the real-schema store ---------------------------------------------------------------------------------

def _load_vec(conn) -> None:
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def write_config(home: Path, flag="on") -> None:
    """flag: "on" (JSON true), "off" (JSON false) or None (the key absent)."""
    engramd = {} if flag is None else {KEY: flag == "on"}
    cfg = {"embedding": {"model": MODEL}, "memory": {"current_turn": 3}, "engramd": engramd}
    tmp = home / "config.json.tmp"
    tmp.write_text(json.dumps(cfg), encoding="utf-8")
    os.replace(tmp, home / "config.json")


def make_store(home: Path, ids=("p0", "p1", "p2"), *, with_vec=True, flag="on", metadata=None) -> Path:
    """Real schema (nine pinned `nodes` triggers included), current observations carrying OLD in BOTH stores."""
    home.mkdir(parents=True, exist_ok=True)
    old_env = os.environ.get("ENGRAM_HOME")
    old_guard = os.environ.get("ENGRAM_NO_DB_GUARDS")
    os.environ["ENGRAM_HOME"] = str(home)
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        if with_vec:
            _load_vec(conn)
        core._run_db_one_time_setup(conn)
        conn.row_factory = None
        has_vec = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'vec_nodes'").fetchone() is not None
        assert has_vec == with_vec, "REAL vec_nodes must exist exactly when asked: these cells must run, not skip"
        for nid in ids:
            meta = (metadata or {}).get(nid, "{}")
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, memory_status, embedding, status)"
                " VALUES (?,?,?,?,?,1,'active',?,NULL)",
                (nid, "observation_factual", f"claim of {nid}", "2026-01-01T00:00:00", meta, json.dumps(OLD)))
            if with_vec:
                conn.execute("INSERT INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                             (nid, sqlite_vec.serialize_float32(OLD)))
        conn.commit()
        conn.close()
    finally:
        for name, old in (("ENGRAM_HOME", old_env), ("ENGRAM_NO_DB_GUARDS", old_guard)):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
    write_config(home, flag)
    return db


def sql(home: Path, statement: str, params=()) -> None:
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def state(home: Path, nid: str) -> tuple:
    """(nodes.embedding text, metadata text, vec_nodes bytes or None)."""
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        _load_vec(conn)
        row = conn.execute("SELECT embedding, metadata FROM nodes WHERE id = ?", (nid,)).fetchone()
        try:
            v = conn.execute("SELECT embedding FROM vec_nodes WHERE node_id = ?", (nid,)).fetchone()
        except sqlite3.OperationalError:
            v = None
        return (None if row is None else row[0], None if row is None else row[1], None if v is None else bytes(v[0]))
    finally:
        conn.close()


def all_state(home: Path, ids) -> dict:
    return {nid: state(home, nid) for nid in ids}


def recipe_sha(home: Path, nid: str) -> str:
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        return pub.current_recipe(conn, nid)[1]
    finally:
        conn.close()


def make_params(home: Path, ids, vectors=None, *, epoch=EPOCH) -> dict:
    vectors = vectors or {nid: vec(1.0 + k) for k, nid in enumerate(ids)}
    return {"epoch": dict(epoch),
            "items": [{"node_id": nid, "vector": vectors[nid], "text_sha256": recipe_sha(home, nid)} for nid in ids]}


def fvec(vector) -> bytes:
    return sqlite_vec.serialize_float32(vector)


# ---- running an owner ---------------------------------------------------------------------------------------

@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="p4651-"))
    try:
        yield d / "h"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def twins():
    d = Path(tempfile.mkdtemp(prefix="p4651-"))
    try:
        yield d / "a", d / "b"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def start(home: Path):
    o = eo.EngramdOwner(home)
    o.start(install_signal_handlers=False)
    return o


@pytest.fixture
def owner(home):
    make_store(home)
    o = start(home)
    try:
        yield o
    finally:
        o.stop()


_rid = [0]


def fresh_rid() -> str:
    _rid[0] += 1
    return f"p4651-rid-{_rid[0]}-{os.getpid()}"


def call(o, params, rid=None, timeout=10.0):
    return ec.write(OP, params, request_id=rid or fresh_rid(), timeout=timeout, socket_path=o.socket_path)


def raw_write(o, params, rid=None):
    """A non-`ec` client: the shipped client refuses to serialise NaN / Infinity, so only a raw socket delivers them."""
    msg = {"contract_version": ec.CONTRACT_VERSION, "method": OP, "params": params, "request_id": rid or fresh_rid()}
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(10.0)
    try:
        s.connect(str(o.socket_path))
        s.sendall((json.dumps(msg, allow_nan=True) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    reply = json.loads(data.decode("utf-8"))
    if reply.get("status") == "ok":
        return ec.Ok(result=reply.get("result"), request_id=msg["request_id"])
    return ec.Rejected(reason=reply["reason"], detail=reply["detail"], remedy=reply["remedy"],
                       request_id=msg["request_id"], commit=reply.get("commit"))


def ok(res):
    assert isinstance(res, ec.Ok), res
    return res.result


def refused(res, reason, token=None):
    assert isinstance(res, ec.Rejected), res
    assert res.reason == reason, res
    if token is not None:
        assert res.detail.startswith(token), (token, res.detail)
    return res


def statuses(result) -> list:
    return [(i["node_id"], i["status"], i["reason"]) for i in result["items"]]


def trace(o) -> list:
    stmts: list = []
    o._owner_conn.set_trace_callback(lambda s: stmts.append(" ".join(s.split())))
    return stmts


class ConnProxy:
    """Stands in for `owner._owner_conn`: forwards everything, records the order of `set_authorizer` / `execute`,
    and can make COMMIT fail once. (An sqlite3.Connection has read-only methods, so the owner's attribute is swapped.)"""

    def __init__(self, real, log, fail_commit=False):
        self._real, self.log, self.fail_commit = real, log, fail_commit

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, statement, *args):
        self.log.append(("exec", " ".join(statement.split())))
        return self._real.execute(statement, *args)

    def set_authorizer(self, fn):
        self.log.append(("auth", fn))
        return self._real.set_authorizer(fn)

    def commit(self):
        if self.fail_commit:
            self.log.append(("commit-failed", None))
            raise sqlite3.OperationalError("disk I/O error")
        self.log.append(("commit", None))
        return self._real.commit()


def warm(o, home):
    """One successful call so the owner's vec0 probe is cached before a proxy replaces its connection."""
    ids = ["warm0"]
    sql(home, "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, memory_status, embedding)"
              " VALUES ('warm0','observation_factual','warm claim','2026-01-01T00:00:00','{}',1,'active',NULL)")
    ok(call(o, make_params(home, ids)))


@pytest.fixture
def serialize_fails_for(monkeypatch):
    """Make the vec mirror's serializer raise for vectors whose first element is in `seeds` (else the real one)."""
    def install(*seeds):
        real = sqlite_vec.serialize_float32

        def wrapper(vector):
            if vector and vector[0] in seeds:
                raise RuntimeError("injected vec mirror failure")
            return real(vector)

        monkeypatch.setattr(sqlite_vec, "serialize_float32", wrapper)
    return install


# =====================================================================================================  F
@requires_linux_owner
def test_f1_flag_on_the_op_runs(owner, home):
    res = call(owner, make_params(home, ["p0"]))
    assert statuses(ok(res)) == [("p0", "refreshed", None)]


@requires_linux_owner
def test_f2_flag_off_refuses_names_the_flag_and_runs_no_sql(home):
    """Mutant: the gate is absent (or runs after validation / the transaction)."""
    make_store(home, flag="off")
    o = start(home)
    try:
        before = all_state(home, ["p0", "p1", "p2"])
        stmts = trace(o)
        res = refused(call(o, make_params(home, ["p0"])), "flag_off")
        assert KEY in res.detail or KEY in res.remedy
        assert all_state(home, ["p0", "p1", "p2"]) == before
        assert stmts == [], stmts  # no transaction, no PRAGMA, no read: not one statement ran
        # the gate comes BEFORE param validation: garbage params are still `flag_off`
        refused(call(o, {"junk": True}), "flag_off")
        refused(call(o, {"epoch": 1, "items": 2, "extra": 3}), "flag_off")
        assert stmts == []
    finally:
        o.stop()


def _config_variants():
    k = KEY
    return {
        "key_missing": json.dumps({"engramd": {OTHER_KEY: True}}),
        "string_true": json.dumps({"engramd": {k: "true"}}),
        "int_one": json.dumps({"engramd": {k: 1}}),
        "null": json.dumps({"engramd": {k: None}}),
        "no_engramd_section": json.dumps({"embedding": {"model": MODEL}}),
        "malformed": "{not json",
        "non_object": "[true]",
        "deep_nesting": "[" * 100000,
    }


@requires_linux_owner
@pytest.mark.parametrize("variant", sorted(_config_variants()) + ["absent", "fifo"])
def test_f3_every_non_true_outcome_fails_closed(home, variant):
    """Mutant: an unreadable or non-true config reads as ON."""
    make_store(home)
    o = start(home)
    try:
        cfg = home / "config.json"
        before = all_state(home, ["p0"])
        params = make_params(home, ["p0"])  # computed while the config is still readable
        if variant == "absent":
            cfg.unlink()
        elif variant == "fifo":
            cfg.unlink()
            os.mkfifo(cfg)
        else:
            cfg.write_text(_config_variants()[variant], encoding="utf-8")
        refused(call(o, params), "flag_off")
        assert all_state(home, ["p0"]) == before
    finally:
        o.stop()


@requires_linux_owner
def test_f3_a_config_read_that_raises_fails_closed(owner, home, monkeypatch):
    """Mutant: `except Exception: flag = True` (a read error treated as ON)."""
    flags = importlib.import_module("engramd_owner_flags")
    before = all_state(home, ["p0"])
    params = make_params(home, ["p0"])

    def boom(*_a, **_k):
        raise OSError("config read blew up")

    monkeypatch.setattr(flags, "flag_on", boom)
    refused(call(owner, params), "flag_off")
    assert all_state(home, ["p0"]) == before


@requires_linux_owner
def test_f4_the_flag_is_read_per_call_not_latched(home):
    """Mutant: the flag is latched when the owner starts (the OFF flip is never seen by the second call)."""
    make_store(home)
    o = start(home)
    try:
        p0, p1 = make_params(home, ["p0"]), make_params(home, ["p1"])
        assert statuses(ok(call(o, p0))) == [("p0", "refreshed", None)]
        write_config(home, "off")
        before = all_state(home, ["p1", "p2"])
        refused(call(o, p1), "flag_off")
        assert all_state(home, ["p1", "p2"]) == before  # nothing written after the flip
        write_config(home, "on")
        assert statuses(ok(call(o, p1))) == [("p1", "refreshed", None)]  # and ON is seen again
    finally:
        o.stop()


@requires_linux_owner
def test_f5_a_caller_value_or_env_cannot_turn_the_gate_on(home, monkeypatch):
    """Mutant: the gate reads the flag from params or the environment instead of the owner's own config."""
    make_store(home, flag="off")
    elsewhere = home.parent / "elsewhere"
    elsewhere.mkdir()
    write_config(elsewhere, "on")
    monkeypatch.setenv("ENGRAM_HOME", str(elsewhere))
    monkeypatch.setenv("ENGRAM_REPAIR_EMBEDDINGS_VIA_OWNER", "1")
    monkeypatch.setenv("REPAIR_EMBEDDINGS_VIA_OWNER", "true")
    o = start(home)
    try:
        before = all_state(home, ["p0"])
        params = make_params(home, ["p0"])
        refused(call(o, params), "flag_off")
        loaded = dict(params, **{KEY: True, "flag": True})
        refused(call(o, loaded), "flag_off")
        assert all_state(home, ["p0"]) == before
        write_config(home, "on")
        refused(call(o, loaded), "invalid_params")  # ON: the extra key is a closed-key violation, never a switch
        assert all_state(home, ["p0"]) == before
    finally:
        o.stop()


def test_f6_any_owner_flag_on_starts_an_owner_for_the_new_flag_alone(home):
    flags = importlib.import_module("engramd_owner_flags")
    home.mkdir(parents=True)
    assert eo.PUBLISH_PREPARED_FLAG_KEY == KEY
    assert KEY.endswith(flags.OWNER_FLAG_SUFFIX)
    (home / "config.json").write_text(json.dumps({"engramd": {KEY: True}}))
    assert flags.any_owner_flag_on(home / "config.json") is True
    assert flags.owner_flags_on(home / "config.json") == [KEY]
    (home / "config.json").write_text(json.dumps({"engramd": {KEY: "true"}}))
    assert flags.any_owner_flag_on(home / "config.json") is False


# =====================================================================================================  V
@requires_linux_owner
def test_v4_the_op_is_in_the_closed_write_set_the_kind_table_and_the_wire_request_id_set(owner):
    assert eo.PUBLISH_PREPARED_EMBEDDINGS == OP
    assert OP in eo.WRITE_METHODS
    assert OP in eo.WIRE_REQUEST_ID_METHODS
    assert OP in owner._methods
    assert owner._method_kinds[OP] == "write"
    assert {m for m, k in owner._method_kinds.items() if k == "write"} == set(eo.WRITE_METHODS)
    assert eo.PUBLISH_PREPARED_MAX_ITEMS == 50


@requires_linux_owner
def test_v4_an_op_with_no_kind_entry_is_refused_at_dispatch(owner, home):
    """Mutant: the kind-table entry is forgotten (default-deny `method_kind_missing`)."""
    owner._method_kinds.pop(OP, None)
    before = all_state(home, ["p0"])
    refused(call(owner, make_params(home, ["p0"])), "method_kind_missing")
    assert all_state(home, ["p0"]) == before


def _shape_violations(home):
    good = make_params(home, ["p0"])

    def item(**kw):
        d = dict(good["items"][0])
        d.update(kw)
        return d

    def without(d, k):
        return {kk: v for kk, v in d.items() if kk != k}

    return {
        "top_extra": dict(good, extra=1),
        "top_missing_items": without(good, "items"),
        "top_missing_epoch": without(good, "epoch"),
        "epoch_extra": dict(good, epoch=dict(good["epoch"], extra=1)),
        "epoch_missing_model": dict(good, epoch=without(good["epoch"], "model")),
        "epoch_missing_dimension": dict(good, epoch=without(good["epoch"], "dimension")),
        "epoch_missing_normalisation": dict(good, epoch=without(good["epoch"], "normalisation")),
        "item_extra": dict(good, items=[item(extra=1)]),
        "item_missing_node_id": dict(good, items=[without(item(), "node_id")]),
        "item_missing_vector": dict(good, items=[without(item(), "vector")]),
        "item_missing_sha": dict(good, items=[without(item(), "text_sha256")]),
        "items_not_list": dict(good, items="x"),
        "item_not_dict": dict(good, items=[["p0"]]),
        "epoch_not_dict": dict(good, epoch=["m"]),
        "node_id_not_str": dict(good, items=[item(node_id=5)]),
        "node_id_empty": dict(good, items=[item(node_id="")]),
        "sha_not_str": dict(good, items=[item(text_sha256=5)]),
        "model_not_str": dict(good, epoch=dict(good["epoch"], model=5)),
        "dimension_not_int": dict(good, epoch=dict(good["epoch"], dimension="384")),
        "dimension_bool": dict(good, epoch=dict(good["epoch"], dimension=True)),
        "normalisation_not_str": dict(good, epoch=dict(good["epoch"], normalisation=None)),
    }


@requires_linux_owner
@pytest.mark.parametrize("name", [
    "top_extra", "top_missing_items", "top_missing_epoch", "epoch_extra", "epoch_missing_model",
    "epoch_missing_dimension", "epoch_missing_normalisation", "item_extra", "item_missing_node_id",
    "item_missing_vector", "item_missing_sha", "items_not_list", "item_not_dict", "epoch_not_dict",
    "node_id_not_str", "node_id_empty", "sha_not_str", "model_not_str", "dimension_not_int", "dimension_bool",
    "normalisation_not_str"])
def test_v1_closed_keys_and_types_refuse_with_nothing_written(owner, home, name):
    """Mutant: the validator accepts extra keys or skips a key / type check."""
    before = all_state(home, ["p0", "p1", "p2"])
    stmts = trace(owner)
    refused(call(owner, _shape_violations(home)[name]), "invalid_params")
    assert all_state(home, ["p0", "p1", "p2"]) == before
    assert not any(s.startswith("BEGIN") for s in stmts), stmts


@requires_linux_owner
def test_v2_item_count_bounds(owner, home):
    """Mutant: the cap is off by one, or an empty batch is accepted."""
    good = make_params(home, ["p0"])
    refused(call(owner, dict(good, items=[])), "invalid_params")
    over = dict(good, items=[dict(good["items"][0], node_id=f"x{i}") for i in range(eo.PUBLISH_PREPARED_MAX_ITEMS + 1)])
    before = all_state(home, ["p0", "p1", "p2"])
    refused(call(owner, over), "invalid_params")
    assert all_state(home, ["p0", "p1", "p2"]) == before
    at_cap = dict(good, items=[dict(good["items"][0], node_id=f"x{i}") for i in range(eo.PUBLISH_PREPARED_MAX_ITEMS)])
    result = ok(call(owner, at_cap))
    assert result["counts"] == {"refreshed": 0, "pending": 0, "deferred": eo.PUBLISH_PREPARED_MAX_ITEMS}


def _bad_vectors():
    good = vec(1.0)
    return {
        "short": good[:-1],
        "long": good + [0.0],
        "nan": good[:5] + [float("nan")] + good[6:],
        "inf": good[:5] + [float("inf")] + good[6:],
        "neg_inf": good[:5] + [float("-inf")] + good[6:],
        "bool": good[:5] + [True] + good[6:],
        "string_element": good[:5] + ["1.0"] + good[6:],
        "none_element": good[:5] + [None] + good[6:],
        "huge_int": good[:5] + [10 ** 400] + good[6:],
        "not_a_list": "not a list",
        "none": None,
        "dict": {"a": 1},
        "empty": [],
    }


@requires_linux_owner
@pytest.mark.parametrize("name", sorted(_bad_vectors()))
def test_v3_a_bad_vector_in_any_item_refuses_the_whole_call_nothing_written(owner, home, name):
    """Mutant: only the first item is validated, or a bad item is skipped instead of refusing the call."""
    ids = ["p0", "p1", "p2"]
    params = make_params(home, ids)
    params["items"][2] = dict(params["items"][2], vector=_bad_vectors()[name])  # the LAST item is bad
    before = all_state(home, ids)
    res = raw_write(owner, params)  # a raw client, so NaN / Infinity can actually reach the owner
    refused(res, "invalid_params")
    assert all_state(home, ids) == before  # the two valid items before it were NOT written


@requires_linux_owner
def test_v5_a_missing_or_empty_request_id_is_refused(owner, home):
    """The envelope's request_id is the idempotency key (WIRE_REQUEST_ID_METHODS): the handler refuses without one."""
    params = make_params(home, ["p0"])
    for bad in (None, "", 5, "x" * 129):
        res = owner._handle_publish_prepared_embeddings(params, time.monotonic() + 5, bad)
        assert res["status"] == "rejected" and res["reason"] == "invalid_params", (bad, res)
        assert res["detail"].startswith("request_id_invalid"), res


# =====================================================================================================  S
@requires_linux_owner
def test_s1_a_valid_batch_refreshes_both_stores_and_reports_counts(owner, home):
    """Mutant: the owner writes only nodes.embedding, or reports counts that do not match the items."""
    ids = ["p0", "p1", "p2"]
    vectors = {nid: vec(1.0 + k) for k, nid in enumerate(ids)}
    result = ok(call(owner, make_params(home, ids, vectors)))
    assert statuses(result) == [(nid, "refreshed", None) for nid in ids]
    assert result["counts"] == {"refreshed": 3, "pending": 0, "deferred": 0}
    assert set(result) == {"items", "counts"}
    for item in result["items"]:
        assert set(item) == {"node_id", "status", "reason", "vector_changed", "debt_cleared"}
        assert item["vector_changed"] is True and item["debt_cleared"] is False
    for nid in ids:
        emb, meta, vbytes = state(home, nid)
        assert emb == json.dumps(vectors[nid])
        assert vbytes == fvec(vectors[nid])
        assert json.loads(meta) == {}  # a no-marker success performs no metadata UPDATE


@requires_linux_owner
def test_s2_one_mirror_failure_is_pending_with_old_stores_intact_and_the_others_refresh(owner, home, serialize_fails_for):
    """Mutant: a mirror failure degrades to `vec_mirrored: false` (the U16 path) instead of the strict pending."""
    ids = ["p0", "p1", "p2"]
    vectors = {"p0": vec(1.0), "p1": vec(2.0), "p2": vec(3.0)}
    serialize_fails_for(2.0)  # B's vector cannot be mirrored
    result = ok(call(owner, make_params(home, ids, vectors)))
    assert statuses(result) == [("p0", "refreshed", None), ("p1", "pending", "mirror"), ("p2", "refreshed", None)]
    assert result["counts"] == {"refreshed": 2, "pending": 1, "deferred": 0}
    assert "vec_mirrored" not in json.dumps(result)
    for nid in ("p0", "p2"):
        emb, _meta, vbytes = state(home, nid)
        assert emb == json.dumps(vectors[nid]) and vbytes == fvec(vectors[nid])
    emb, meta, vbytes = state(home, "p1")
    assert emb == json.dumps(OLD) and vbytes == fvec(OLD)  # BOTH old stores intact
    assert MARKER in json.loads(meta)  # and the debt marker is set


def _deferral_cases():
    return {
        "recipe_changed": ("recipe_changed", EPOCH, "UPDATE nodes SET recall_summary = 'a different summary' WHERE id = 'p0'"),
        "epoch_model": ("epoch_changed", dict(EPOCH, model="some-other-model"), None),
        "epoch_dimension": ("epoch_changed", dict(EPOCH, dimension=385), None),
        "epoch_normalisation": ("epoch_changed", dict(EPOCH, normalisation="unit"), None),
        "not_current": ("not_current", EPOCH, "UPDATE nodes SET is_current = 0 WHERE id = 'p0'"),
        "empty_text": ("empty_text", EPOCH, "UPDATE nodes SET claim = '', recall_summary = NULL, recall_keywords = NULL WHERE id = 'p0'"),
        "malformed_metadata": ("malformed_metadata", EPOCH, "UPDATE nodes SET metadata = '[1]' WHERE id = 'p0'"),
    }


@requires_linux_owner
@pytest.mark.parametrize("name", sorted(_deferral_cases()))
def test_s3_every_deferral_reason_through_the_owner_writes_nothing(owner, home, name):
    """Mutant: the owner path skips one of the primitive's six checks (see P1 for the divergence mutant)."""
    reason, epoch, mutation = _deferral_cases()[name]
    marker_meta = json.dumps({MARKER: {"reason": "x", "stage": "strict_refresh", "at": "2026-01-01T00:00:00Z"}, "keep": 1})
    sql(home, "UPDATE nodes SET metadata = ? WHERE id = 'p0'", (marker_meta,))
    params = make_params(home, ["p0"], epoch=epoch)  # hash taken BEFORE the store changes
    if mutation:
        sql(home, mutation)
    before = state(home, "p0")
    result = ok(call(owner, params))
    assert statuses(result) == [("p0", "deferred", reason)]
    assert result["counts"] == {"refreshed": 0, "pending": 0, "deferred": 1}
    assert result["items"][0]["vector_changed"] is None and result["items"][0]["debt_cleared"] is False
    assert state(home, "p0") == before  # old vectors AND the marker, byte for byte


@requires_linux_owner
def test_s4_a_marker_write_that_would_be_invalid_json_aborts_and_everything_stays_old(owner, home, serialize_fails_for, monkeypatch):
    """The `nodes_metadata_json_guard_update` trigger aborts an UPDATE of metadata to invalid JSON: fail closed, the
    whole call rolls back, no old vector is lost. Mutant: the owner catches the abort and commits the rest."""
    ids = ["p0", "p1"]
    vectors = {"p0": vec(1.0), "p1": vec(2.0)}
    real = core._metadata_dict_for_write

    def poisoned(raw, node_id, tool):
        meta, warning = real(raw, node_id, tool)
        meta["bad"] = float("nan")  # json.dumps emits NaN: not valid JSON, so the guard trigger must fire
        return meta, warning

    serialize_fails_for(2.0)  # p1 needs the marker write
    monkeypatch.setattr(core, "_metadata_dict_for_write", poisoned)
    before = all_state(home, ids + ["p2"])
    res = call(owner, make_params(home, ids, vectors))
    refused(res, "publish_write_failed")
    assert all_state(home, ids + ["p2"]) == before  # p0's refresh was rolled back with it; nothing invalid landed
    for nid in ids:
        json.loads(state(home, nid)[1])


@requires_linux_owner
def test_s4_control_the_valid_marker_write_succeeds(owner, home, serialize_fails_for):
    serialize_fails_for(2.0)
    result = ok(call(owner, make_params(home, ["p1"], {"p1": vec(2.0)})))
    assert statuses(result) == [("p1", "pending", "mirror")]
    assert MARKER in json.loads(state(home, "p1")[1])


@requires_linux_owner
def test_s5_set_and_clear_merge_the_metadata_blob(home, serialize_fails_for):
    """Mutant: the marker write replaces the whole blob (another writer's key is lost on set or on clear)."""
    make_store(home, ids=("p0", "p1"), metadata={
        "p0": json.dumps({"other_writer": {"k": 1}}),
        "p1": json.dumps({"other_writer": {"k": 2}, MARKER: {"reason": "old", "stage": "strict_refresh", "at": "t"}}),
    })
    o = start(home)
    try:
        serialize_fails_for(1.0)
        res = ok(call(o, make_params(home, ["p0"], {"p0": vec(1.0)})))  # failure: marker SET beside the other key
        assert statuses(res) == [("p0", "pending", "mirror")]
        meta = json.loads(state(home, "p0")[1])
        assert meta["other_writer"] == {"k": 1} and MARKER in meta
        res = ok(call(o, make_params(home, ["p1"], {"p1": vec(5.0)})))  # success: marker CLEARED, the other key stays
        assert statuses(res) == [("p1", "refreshed", None)]
        assert res["items"][0]["debt_cleared"] is True
        meta = json.loads(state(home, "p1")[1])
        assert meta == {"other_writer": {"k": 2}}
    finally:
        o.stop()


# =====================================================================================================  E
@requires_linux_owner
def test_e1_vec_table_present_but_vec0_not_loadable_refuses_the_whole_call(owner, home, monkeypatch):
    """Mutant: the owner proceeds without the vec store (writes nodes.embedding only) or refuses per row."""
    monkeypatch.setattr(owner, "_fill_vec_ready", lambda conn: None)
    before = all_state(home, ["p0", "p1", "p2"])
    stmts = trace(owner)
    refused(call(owner, make_params(home, ["p0", "p1"])), "vec_unavailable")
    assert all_state(home, ["p0", "p1", "p2"]) == before
    assert not any(s.startswith("BEGIN") for s in stmts), stmts


@requires_linux_owner
def test_e2_no_vec_nodes_table_at_all_the_op_runs_under_the_primitives_rules(twins):
    """No `vec_nodes` table: the gate does not refuse, and the verdicts are exactly the in-process primitive's."""
    home_a, home_b = twins
    make_store(home_a, with_vec=False)
    make_store(home_b, with_vec=False)
    params = make_params(home_a, ["p0"])
    conn = sqlite3.connect(str(home_a / "knowledge.db"))
    core._configure_paths(home_a)
    expected = pub.publish_embedding(conn, "p0", prepared=pub.PreparedVector(
        vector=params["items"][0]["vector"], text_sha256=params["items"][0]["text_sha256"],
        epoch=pub.EmbeddingEpoch(**EPOCH)))
    conn.commit()
    conn.close()
    o = start(home_b)
    try:
        result = ok(call(o, params))
        item = result["items"][0]
        assert (item["status"], item["reason"]) == (expected.status, expected.reason)
        assert state(home_a, "p0")[0] == state(home_b, "p0")[0]
    finally:
        o.stop()


@requires_linux_owner
def test_e2_no_vec_store_and_no_vec_backend_refreshes(twins, monkeypatch):
    home_a, home_b = twins
    make_store(home_b, with_vec=False)
    monkeypatch.setattr(core, "_VEC_BACKEND_AVAILABLE", False)
    o = start(home_b)
    try:
        result = ok(call(o, make_params(home_b, ["p0"])))
        assert statuses(result) == [("p0", "refreshed", None)]
        assert state(home_b, "p0")[0] == json.dumps(vec(1.0))
    finally:
        o.stop()


# =====================================================================================================  R
@requires_linux_owner
def test_r1_replay_answers_the_stored_outcome_writes_nothing_and_a_changed_payload_is_refused(owner, home):
    """Mutant: a replay re-executes, or the same id with another payload is accepted."""
    params = make_params(home, ["p0"])
    first = ok(call(owner, params, rid="rid-r1"))
    assert "duplicate" not in first or first["duplicate"] is False
    sql(home, "UPDATE nodes SET embedding = ? WHERE id = 'p0'", (json.dumps(vec(9.0)),))  # an outside write after the first call
    stmts = trace(owner)
    again = ok(call(owner, params, rid="rid-r1"))
    assert again["duplicate"] is True
    assert again["items"] == first["items"] and again["counts"] == first["counts"]
    assert state(home, "p0")[0] == json.dumps(vec(9.0))  # the replay wrote nothing
    assert not any(s.startswith("BEGIN") for s in stmts), stmts
    other = make_params(home, ["p0"], {"p0": vec(4.0)})
    refused(call(owner, other, rid="rid-r1"), "invalid_params", "request_id_reused")
    assert state(home, "p0")[0] == json.dumps(vec(9.0))


@requires_linux_owner
@pytest.mark.parametrize("which", ["embedding_text", "vec_bytes"])
def test_r2_a_readback_that_differs_is_committed_unverified_and_replays(owner, home, which):
    """Mutant: the read-back checks only nodes.embedding, or only the vec bytes."""
    real = owner._publish_prepared_readback

    def perturbing(conn, ids, *a, **k):
        out = real(conn, ids, *a, **k)
        fixed = {}
        for nid, (emb, vbytes) in out.items():
            fixed[nid] = ((emb or "") + "x", vbytes) if which == "embedding_text" else (emb, (vbytes or b"") + b"x")
        return fixed

    owner._publish_prepared_readback = perturbing
    params = make_params(home, ["p0"])
    res = refused(call(owner, params, rid="rid-r2"), "readback_mismatch")
    assert res.commit == "committed_unverified"
    assert "p0" in res.detail
    assert state(home, "p0")[0] == json.dumps(params["items"][0]["vector"])  # the commit stands
    sql(home, "UPDATE nodes SET embedding = ? WHERE id = 'p0'", (json.dumps(vec(9.0)),))
    owner._publish_prepared_readback = real
    again = refused(call(owner, params, rid="rid-r2"), "readback_mismatch")  # recorded: replay re-reports
    assert again.commit == "committed_unverified"
    assert state(home, "p0")[0] == json.dumps(vec(9.0))  # and does not re-write


@requires_linux_owner
def test_r2_a_readback_that_raises_is_also_committed_unverified(owner, home):
    def boom(*_a, **_k):
        raise RuntimeError("readback exploded")

    owner._publish_prepared_readback = boom
    res = refused(call(owner, make_params(home, ["p0"])), "readback_mismatch")
    assert res.commit == "committed_unverified"


@requires_linux_owner
def test_r3_a_failed_commit_rolls_the_whole_batch_back_and_a_same_id_retry_runs(owner, home):
    """Mutant: a failed COMMIT is reported ok, or recorded for replay."""
    warm(owner, home)
    ids = ["p0", "p1"]
    params = make_params(home, ids)
    before = all_state(home, ids)
    real = owner._owner_conn
    owner._owner_conn = ConnProxy(real, [], fail_commit=True)
    try:
        refused(call(owner, params, rid="rid-r3"), "publish_write_failed")
    finally:
        owner._owner_conn = real
    assert all_state(home, ids) == before
    assert not real.in_transaction
    result = ok(call(owner, params, rid="rid-r3"))  # nothing was recorded: the retry runs fresh
    assert statuses(result) == [("p0", "refreshed", None), ("p1", "refreshed", None)]
    assert result.get("duplicate") is not True


# =====================================================================================================  A / T / D
# The recording authorizer: every (action, arg1, arg2, db_name, trigger_or_view) the primitive produces, in PREPARED
# mode, on the REAL schema, on a FRESH vec0-loaded connection per scenario (so the vec0 constructor's own events are
# part of the record), over five scenarios: refreshed without a marker, refreshed with a marker (the metadata UPDATE and
# its trigger), a mirror failure (ROLLBACK TO + the marker SET), a malformed-metadata deferral and a recipe deferral.
# Recorded from the primitive at A1 (SQLite 3.45.1, sqlite_vec 0.1.9, venv python); a change here is a change to the
# surface the owner's allowlist must admit.
S = sqlite3
EXPECTED_EVENTS = frozenset({
    (S.SQLITE_DELETE, "vec_nodes", None, "main", None),
    (S.SQLITE_DELETE, "vec_nodes_rowids", None, "main", None),
    (S.SQLITE_INSERT, "vec_nodes", None, "main", None),
    (S.SQLITE_INSERT, "vec_nodes_rowids", None, "main", None),
    (S.SQLITE_READ, "nodes", "claim", "main", None),
    (S.SQLITE_READ, "nodes", "embedding", "main", None),
    (S.SQLITE_READ, "nodes", "id", "main", None),
    (S.SQLITE_READ, "nodes", "is_current", "main", None),
    (S.SQLITE_READ, "nodes", "metadata", "main", None),
    (S.SQLITE_READ, "nodes", "metadata", "main", "nodes_metadata_json_guard_update"),
    (S.SQLITE_READ, "nodes", "predicted_event", "main", None),
    (S.SQLITE_READ, "nodes", "recall_keywords", "main", None),
    (S.SQLITE_READ, "nodes", "recall_summary", "main", None),
    (S.SQLITE_READ, "nodes", "source_title", "main", None),
    (S.SQLITE_READ, "nodes", "type", "main", None),
    (S.SQLITE_READ, "sqlite_master", "name", "main", None),
    (S.SQLITE_READ, "sqlite_master", "type", "main", None),
    (S.SQLITE_READ, "vec_nodes", "embedding", "main", None),
    (S.SQLITE_READ, "vec_nodes", "node_id", "main", None),
    (S.SQLITE_READ, "vec_nodes_chunks", "", "main", None),
    (S.SQLITE_READ, "vec_nodes_chunks", "chunk_id", "main", None),
    (S.SQLITE_READ, "vec_nodes_rowids", "chunk_id", "main", None),
    (S.SQLITE_READ, "vec_nodes_rowids", "chunk_offset", "main", None),
    (S.SQLITE_READ, "vec_nodes_rowids", "id", "main", None),
    (S.SQLITE_READ, "vec_nodes_rowids", "rowid", "main", None),
    (S.SQLITE_READ, "x", "node_id", "main", None),
    (S.SQLITE_SELECT, None, None, None, None),
    (S.SQLITE_SELECT, None, None, None, "nodes_metadata_json_guard_update"),
    (S.SQLITE_TRANSACTION, "BEGIN", None, None, None),
    (S.SQLITE_TRANSACTION, "COMMIT", None, None, None),
    (S.SQLITE_UPDATE, "nodes", "embedding", "main", None),
    (S.SQLITE_UPDATE, "nodes", "metadata", "main", None),
    (S.SQLITE_UPDATE, "vec_nodes_rowids", "chunk_id", "main", None),
    (S.SQLITE_UPDATE, "vec_nodes_rowids", "chunk_offset", "main", None),
    (S.SQLITE_FUNCTION, None, "json_valid", None, "nodes_metadata_json_guard_update"),
    (S.SQLITE_FUNCTION, None, "max", None, None),
    (S.SQLITE_SAVEPOINT, "BEGIN", "recall_summary_vector_refresh", None, None),
    (S.SQLITE_SAVEPOINT, "RELEASE", "recall_summary_vector_refresh", None, None),
    (S.SQLITE_SAVEPOINT, "ROLLBACK", "recall_summary_vector_refresh", None, None),
})
EXPECTED_ADMITTED_TRIGGERS = frozenset({"nodes_metadata_json_guard_update"})


def measure_prepared_events(root: Path) -> frozenset:
    """Run the primitive in PREPARED mode under a recording authorizer (answers OK, logs every call) on the real schema."""
    home = root / "meas"
    make_store(home, ids=("m1", "m2", "m3", "m4"), metadata={
        "m2": json.dumps({MARKER: {"reason": "x", "stage": "strict_refresh", "at": "t"}, "other": 1}),
        "m4": "[1]",
    })
    core._configure_paths(home)
    epoch = pub.current_epoch()
    log: list = []

    def recorder(action, a1, a2, db, trig):
        log.append((action, a1, a2, db, trig))
        return sqlite3.SQLITE_OK

    def scenario(nid, *, sha=None, fail=False):
        prep_conn = sqlite3.connect(str(home / "knowledge.db"))
        digest = sha or pub.current_recipe(prep_conn, nid)[1]
        prep_conn.close()
        prepared = pub.PreparedVector(vector=vec(0.5), text_sha256=digest, epoch=epoch)
        conn = sqlite3.connect(str(home / "knowledge.db"))
        _load_vec(conn)
        log.clear()
        conn.set_authorizer(recorder)
        conn.execute("BEGIN IMMEDIATE")
        real = sqlite_vec.serialize_float32
        if fail:
            def boom(_v):
                raise RuntimeError("injected")
            sqlite_vec.serialize_float32 = boom
        try:
            pub.publish_embedding(conn, nid, prepared=prepared)
        finally:
            sqlite_vec.serialize_float32 = real
        conn.commit()
        conn.set_authorizer(None)
        conn.close()
        return set(log)

    events = set()
    events |= scenario("m1")
    events |= scenario("m2")
    events |= scenario("m3", fail=True)
    events |= scenario("m4")
    events |= scenario("m1", sha="0" * 64)
    return frozenset(events)


@pytest.fixture(scope="module")
def measured():
    """The re-measurement, once per module run (it is a fresh measurement of the primitive as it is NOW)."""
    d = Path(tempfile.mkdtemp(prefix="p4651m-"))
    try:
        return measure_prepared_events(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a1_every_event_the_primitive_produces_is_admitted_by_the_ops_allowlist(measured):
    """Mutant: the allowlist drops one measured column (the op would fail at run time with a permission error that
    reads like a data problem)."""
    refused_events = sorted(e for e in measured if not eo._publish_prepared_embeddings_allows(*e))
    assert refused_events == []


def test_a2_the_recorded_event_set_fails_on_primitive_drift(measured):
    """Re-measures NOW and compares with the committed record. Mutant (op untouched): the primitive reads one more
    column, for example `nodes.quoted_text`, or writes one more."""
    assert measured - EXPECTED_EVENTS == frozenset(), "the primitive now produces events the op never measured"
    assert EXPECTED_EVENTS - measured == frozenset(), "the primitive no longer produces events the op admits"


def test_a3_column_exact_denials_and_the_positive_controls():
    allows = eo._publish_prepared_embeddings_allows
    sp = "recall_summary_vector_refresh"
    # positive controls: the instrument says yes to what the op needs
    assert allows(S.SQLITE_UPDATE, "nodes", "embedding", "main", None)
    assert allows(S.SQLITE_UPDATE, "nodes", "metadata", "main", None)
    assert allows(S.SQLITE_READ, "nodes", "recall_summary", "main", None)
    assert allows(S.SQLITE_SAVEPOINT, "BEGIN", sp, None, None)
    assert allows(S.SQLITE_FUNCTION, None, "max", None, None)
    assert allows(S.SQLITE_FUNCTION, None, "json_valid", None, "nodes_metadata_json_guard_update")
    # denials
    denied = [
        (S.SQLITE_UPDATE, "nodes", "claim", "main", None),
        (S.SQLITE_UPDATE, "nodes", "status", "main", None),
        (S.SQLITE_UPDATE, "nodes", "is_current", "main", None),
        (S.SQLITE_UPDATE, "nodes", "recall_summary", "main", None),
        (S.SQLITE_UPDATE, "edges", "relation", "main", None),
        (S.SQLITE_READ, "nodes", "quoted_text", "main", None),
        (S.SQLITE_READ, "nodes", "interpretation", "main", None),
        (S.SQLITE_READ, "edges", "source_id", "main", None),
        (S.SQLITE_READ, "sqlite_master", "sql", "main", None),
        (S.SQLITE_READ, "nodes", "embedding", "temp", None),
        (S.SQLITE_INSERT, "nodes", None, "main", None),
        (S.SQLITE_DELETE, "nodes", None, "main", None),
        (S.SQLITE_INSERT, "edges", None, "main", None),
        (S.SQLITE_FUNCTION, None, "load_extension", None, None),
        (S.SQLITE_FUNCTION, None, "random", None, None),
        (S.SQLITE_FUNCTION, None, "json_valid", None, None),  # only the admitted trigger may call it
        (S.SQLITE_SAVEPOINT, "BEGIN", "engram_fill_vec", None, None),
        (S.SQLITE_SAVEPOINT, "BEGIN", "other", None, None),
        (S.SQLITE_SAVEPOINT, "BEGIN", None, None, None),
        (S.SQLITE_PRAGMA, "table_info", "nodes", "main", None),
        (S.SQLITE_PRAGMA, "busy_timeout", None, None, None),
        (S.SQLITE_ATTACH, "/tmp/elsewhere.db", None, None, None),
        (S.SQLITE_DETACH, "x", None, None, None),
        # statements born in any trigger that is not the one admitted trigger
        (S.SQLITE_READ, "nodes", "metadata", "main", "nodes_text_nul_guard_update"),
        (S.SQLITE_SELECT, None, None, None, "nodes_fts_insert"),
        (S.SQLITE_UPDATE, "nodes", "embedding", "main", "nodes_metadata_json_guard_update"),
        (S.SQLITE_INSERT, "vec_nodes", None, "main", "nodes_metadata_json_guard_update"),
    ]
    for event in denied:
        assert not allows(*event), event


def _trigger_rows(conn) -> dict:
    return {n: s for n, s in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'nodes'")}


# The SHA-256 of the admitted trigger's CREATE statement, from sqlite_master on the real schema. A name is a label; the
# body is the artifact (it can write any table, update any row, RAISE).
PINNED_TRIGGER_SQL_SHA256 = {
    "nodes_metadata_json_guard_update": "5f4999f920d0ebfdf97b5aa892db5207845134d2eeecb992fa9d54c480c71e65",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_t1_the_admitted_triggers_are_pinned_by_sql_body_not_by_name(home):
    """Mutant: the trigger's body is edited and its name kept (a name-only pin would stay green)."""
    make_store(home)
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        rows = _trigger_rows(conn)
        for name in eo.PUBLISH_PREPARED_ADMITTED_TRIGGERS:
            assert name in rows
            assert _sha(rows[name]) == PINNED_TRIGGER_SQL_SHA256[name], f"{name}: body changed"
        assert set(PINNED_TRIGGER_SQL_SHA256) == set(eo.PUBLISH_PREPARED_ADMITTED_TRIGGERS)
        # the discriminating case: change the body, keep the name -> the pin check reports it
        conn.execute("DROP TRIGGER nodes_metadata_json_guard_update")
        conn.execute(
            "CREATE TRIGGER nodes_metadata_json_guard_update BEFORE UPDATE OF metadata ON nodes "
            "BEGIN SELECT 1; END")
        changed = _trigger_rows(conn)["nodes_metadata_json_guard_update"]
        assert _sha(changed) != PINNED_TRIGGER_SQL_SHA256["nodes_metadata_json_guard_update"]
    finally:
        conn.close()


def test_t2_the_measured_admitted_trigger_set_is_exactly_one_and_the_instrument_discriminates(measured, home):
    """The set of triggers that fire under the primitive is {nodes_metadata_json_guard_update}, size ONE; the negative
    control in the same harness shows an UPDATE OF status fires a DIFFERENT single trigger."""
    fired = {e[4] for e in measured if e[4] is not None}
    assert fired == EXPECTED_ADMITTED_TRIGGERS
    assert len(fired) == 1
    assert eo.PUBLISH_PREPARED_ADMITTED_TRIGGERS == EXPECTED_ADMITTED_TRIGGERS
    make_store(home)
    conn = sqlite3.connect(str(home / "knowledge.db"))
    log: list = []
    conn.set_authorizer(lambda a, b, c, d, e: (log.append((a, b, c, d, e)), sqlite3.SQLITE_OK)[1])
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE nodes SET status = 'retracted' WHERE id = 'p0'")
    conn.rollback()
    assert {e[4] for e in log if e[4]} == {"nodes_retract_remove_from_fts"}
    log.clear()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE nodes SET metadata = '{}' WHERE id = 'p0'")
    conn.rollback()
    assert {e[4] for e in log if e[4]} == {"nodes_metadata_json_guard_update"}
    conn.close()


@pytest.mark.parametrize("statement", [
    "CREATE TRIGGER zz_new AFTER INSERT ON nodes BEGIN SELECT 1; END",
    "ALTER TABLE nodes ADD COLUMN zz_extra TEXT",
    "DROP TRIGGER nodes_metadata_json_guard_update",
    "DROP TABLE edges",
    "CREATE TABLE zz_new_table (a TEXT)",
])
def test_d1_ddl_is_denied_on_the_owner_connection_for_this_op(home, statement):
    """Mutant: the allowlist admits the DDL family. Each statement is stopped by its action code AND by the other
    admissions it needs (CREATE / DROP TRIGGER and CREATE TABLE need writes to `sqlite_master`; ALTER TABLE also calls
    `printf` / `substr` / `length` and reads `sqlite_master.sql`; DROP TABLE also deletes the table's rows), so a
    single admitted action code leaves every cell green. Measured: admitting the codes plus `sqlite_master` writes
    reddens the two TRIGGER cells; adding `sqlite_master` reads reddens CREATE TABLE; adding any SQL function reddens
    ALTER TABLE. `DROP TABLE edges` stays denied by its row DELETE, which test_a3 pins for `nodes`."""
    make_store(home)
    conn = sqlite3.connect(str(home / "knowledge.db"))
    guard = eo._UseRewardGuard(allows=eo._publish_prepared_embeddings_allows)
    conn.set_authorizer(guard.authorizer)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(statement)
    finally:
        conn.set_authorizer(None)
    assert guard.denials, "the denial must be recorded so the op answers write_denied"
    assert "zz_new" not in " ".join(r[0] for r in conn.execute("SELECT name FROM sqlite_master"))
    assert "nodes_metadata_json_guard_update" in _trigger_rows(conn)
    conn.close()


@requires_linux_owner
def test_a4_the_authorizer_is_installed_before_begin_immediate_and_everything_is_restored(owner, home):
    """Mutant: BEGIN IMMEDIATE runs before the op authorizer is installed, or an exit path leaves it in place."""
    warm(owner, home)
    real = owner._owner_conn
    prev_busy = real.execute("PRAGMA busy_timeout").fetchone()[0]
    log: list = []
    owner._owner_conn = ConnProxy(real, log)
    try:
        ok(call(owner, make_params(home, ["p0"])))
    finally:
        owner._owner_conn = real
    kinds = [k for k, _ in log]
    first_auth = kinds.index("auth")
    begin = next(i for i, (k, v) in enumerate(log) if k == "exec" and v.startswith("BEGIN IMMEDIATE"))
    assert first_auth < begin, log
    installed = log[first_auth][1]
    assert installed is not eo._owner_internal_authorizer
    last_auth = max(i for i, k in enumerate(kinds) if k == "auth")
    assert log[last_auth][1] is eo._owner_internal_authorizer
    assert ("exec", f"PRAGMA busy_timeout = {prev_busy}") in log[last_auth:]


def _assert_restored(owner, prev_busy):
    conn = owner._owner_conn
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == prev_busy
    assert not conn.in_transaction
    # the permissive internal authorizer is back: statements the op's allowlist would deny now run. Two probes, because
    # the write_denied cell swaps in an allowlist that admits `claim` and denies `embedding`: a leaked authorizer of
    # EITHER kind must fail one of them.
    conn.execute("UPDATE nodes SET claim = claim WHERE id = 'p0'")
    conn.execute("UPDATE nodes SET embedding = embedding WHERE id = 'p0'")
    conn.rollback()


@requires_linux_owner
@pytest.mark.parametrize("exit_path", ["ok", "vec_unavailable", "readback_mismatch", "write_denied", "primitive_raises", "failed_commit"])
def test_a4_restored_on_every_exit_path(owner, home, monkeypatch, exit_path):
    warm(owner, home)
    prev_busy = owner._owner_conn.execute("PRAGMA busy_timeout").fetchone()[0]
    params = make_params(home, ["p0"])
    real_conn = owner._owner_conn
    if exit_path == "vec_unavailable":
        monkeypatch.setattr(owner, "_fill_vec_ready", lambda conn: None)
    elif exit_path == "readback_mismatch":
        monkeypatch.setattr(owner, "_publish_prepared_readback", lambda *a, **k: {})
    elif exit_path == "write_denied":
        monkeypatch.setattr(eo, "_publish_prepared_embeddings_allows",
                            lambda action, a1, a2, db, trig: not (action == sqlite3.SQLITE_UPDATE and a2 == "embedding"))
    elif exit_path == "primitive_raises":
        def boom(*_a, **_k):
            raise RuntimeError("primitive blew up")
        monkeypatch.setattr(pub, "publish_embedding", boom)
    elif exit_path == "failed_commit":
        owner._owner_conn = ConnProxy(real_conn, [], fail_commit=True)
    try:
        res = call(owner, params)
    finally:
        owner._owner_conn = real_conn
    expected = {"ok": None, "vec_unavailable": "vec_unavailable", "readback_mismatch": "readback_mismatch",
                "write_denied": "write_denied", "failed_commit": "publish_write_failed"}.get(exit_path, "x")
    if exit_path == "ok":
        ok(res)
    elif exit_path == "primitive_raises":
        assert not isinstance(res, ec.Ok), res
    else:
        refused(res, expected)
    _assert_restored(owner, prev_busy)


@requires_linux_owner
def test_a5_a_denied_statement_rolls_the_whole_op_back(owner, home, monkeypatch):
    """The allowlist denies a measured statement: `write_denied`, nothing written (not a half-published batch)."""
    ids = ["p0", "p1"]
    before = all_state(home, ids)
    monkeypatch.setattr(eo, "_publish_prepared_embeddings_allows",
                        lambda action, a1, a2, db, trig: not (action == sqlite3.SQLITE_INSERT and str(a1).startswith("vec_nodes")))
    refused(call(owner, make_params(home, ids)), "write_denied")
    assert all_state(home, ids) == before


# =====================================================================================================  P
def _parity_cases():
    """(name, ids, vectors-by-id seed, epoch, mutation applied to BOTH stores AFTER the hashes were taken, failing seeds)."""
    marker = json.dumps({MARKER: {"reason": "old", "stage": "strict_refresh", "at": "t"}, "keep": {"k": 1}})
    return [
        ("refreshed", ["q0"], {"q0": 1.0}, EPOCH, None, ()),
        ("refreshed_clears_marker", ["q1"], {"q1": 2.0}, EPOCH, f"UPDATE nodes SET metadata = '{marker}' WHERE id = 'q1'", ()),
        ("refreshed_same_vector", ["q2"], {"q2": 7.0}, EPOCH, None, ()),
        ("recipe_changed", ["q3"], {"q3": 3.0}, EPOCH, "UPDATE nodes SET recall_summary = 'changed later' WHERE id = 'q3'", ()),
        ("epoch_model", ["q4"], {"q4": 4.0}, dict(EPOCH, model="other-model"), None, ()),
        ("epoch_dimension", ["q5"], {"q5": 4.5}, dict(EPOCH, dimension=385), None, ()),
        ("not_current", ["q6"], {"q6": 5.0}, EPOCH, "UPDATE nodes SET is_current = 0 WHERE id = 'q6'", ()),
        ("empty_text", ["q7"], {"q7": 6.0}, EPOCH,
         "UPDATE nodes SET claim = '', recall_summary = NULL, recall_keywords = NULL WHERE id = 'q7'", ()),
        ("malformed_metadata", ["q8"], {"q8": 6.5}, EPOCH, "UPDATE nodes SET metadata = '[1]' WHERE id = 'q8'", ()),
        ("mirror_failure", ["q9"], {"q9": 8.0}, EPOCH, None, (8.0,)),
        ("mixed_batch", ["r0", "r1", "r2"], {"r0": 11.0, "r1": 12.0, "r2": 13.0}, EPOCH, None, (12.0,)),
    ]


def _normalise(snap: dict) -> dict:
    """The marker's `at` is a wall-clock second: the only field that may legitimately differ between two runs."""
    out = {}
    for nid, (emb, meta, vbytes) in snap.items():
        if meta:
            parsed = json.loads(meta)
            if isinstance(parsed, dict) and isinstance(parsed.get(MARKER), dict):
                parsed[MARKER] = dict(parsed[MARKER], at="<time>")
            meta = json.dumps(parsed, sort_keys=True)
        out[nid] = (emb, meta, vbytes)
    return out


@requires_linux_owner
def test_p1_identical_verification_in_process_and_through_the_owner(twins, serialize_fails_for):
    """Same inputs through the in-process primitive and the real owner op: per-item status, reason, vector_changed,
    debt_cleared and the final embedding, vec bytes and metadata are IDENTICAL. Mutant: a divergence in the owner path
    only (for example the owner recomputes the text hash instead of comparing the caller's)."""
    home_a, home_b = twins
    all_ids = [i for c in _parity_cases() for i in c[1]]
    for h in (home_a, home_b):
        make_store(h, ids=tuple(all_ids))
    # `q2` is asked to publish vec(7.0), which is exactly what both stores already hold (the unchanged-but-verified case)
    serialize_fails_for(8.0, 12.0)
    o = start(home_b)
    try:
        compared = 0
        for name, ids, seeds, epoch, mutation, _fails in _parity_cases():
            vectors = {nid: vec(seeds[nid]) for nid in ids}
            params = make_params(home_a, ids, vectors, epoch=epoch)
            if mutation:
                sql(home_a, mutation)
                sql(home_b, mutation)
            conn = sqlite3.connect(str(home_a / "knowledge.db"))
            _load_vec(conn)
            core._configure_paths(home_a)
            expected = []
            for it in params["items"]:
                expected.append(pub.publish_embedding(conn, it["node_id"], prepared=pub.PreparedVector(
                    vector=it["vector"], text_sha256=it["text_sha256"], epoch=pub.EmbeddingEpoch(**epoch))))
            conn.commit()
            conn.close()
            got = ok(call(o, params))["items"]
            assert len(got) == len(expected), name
            for g, e in zip(got, expected):
                assert (g["node_id"], g["status"], g["reason"], g["vector_changed"], g["debt_cleared"]) == (
                    e.node_id, e.status, e.reason, e.vector_changed, e.debt_cleared), (name, g, e)
                compared += 1
        assert compared >= 8
        assert _normalise(all_state(home_a, all_ids)) == _normalise(all_state(home_b, all_ids))
        final = {nid: state(home_b, nid) for nid in all_ids}
        assert any(MARKER in (m or "") for _e, m, _v in final.values())  # the mirror-failure rows carry their markers
    finally:
        o.stop()


# =====================================================================================================  N
def _ledger_section() -> str:
    text = LEDGER.read_text(encoding="utf-8")
    start_at = text.index("publish_prepared_embeddings")
    heading_start = text.rfind("\n### ", 0, start_at)
    nxt = text.find("\n## ", start_at)
    return text[heading_start:nxt if nxt != -1 else len(text)]


def _first_sentence(paragraph: str) -> str:
    return " ".join(paragraph.split()).split(". ")[0]


def test_n1_the_ledger_row_and_the_op_docstring_each_say_what_the_neighbouring_flag_is_not():
    """Kepler's naming condition: the two flag names sit side by side, so the FIRST sentence of the ledger row and of
    the op's docstring each say that `regen_embeddings_via_owner` routes the writes that FILL vectors that are missing
    and `repair_embeddings_via_owner` routes the one-time repair that REWRITES vectors that already exist, and only
    after the repair tool's explicit apply."""
    section = _ledger_section()
    body = [p for p in section.split("\n\n") if p.strip() and not p.lstrip().startswith("###")]
    row_first = _first_sentence(body[0])
    doc_first = _first_sentence(inspect.getdoc(eo.EngramdOwner._handle_publish_prepared_embeddings))
    for sentence in (row_first, doc_first):
        assert OTHER_KEY in sentence and KEY in sentence, sentence
        assert "FILL vectors that are missing" in sentence, sentence
        assert "REWRITES vectors that already exist" in sentence, sentence
        assert "explicit apply" in sentence, sentence


def test_n2_the_docstring_says_the_flag_guarantee_is_per_call():
    doc = " ".join(inspect.getdoc(eo.EngramdOwner._handle_publish_prepared_embeddings).split())
    assert "per CALL, not per statement" in doc
    assert "does not abort that call" in doc
    assert "flag_off" in doc


# =====================================================================================================  K (A2 delta)
# Cells added after the independent review of the owner op: the vec0 allowlist width (K1), the idempotency fingerprint
# (K2), replay of a raising read-back (K3), the dedupe cap (K4), validation edges (K5), a competing writer and an injected
# KeyboardInterrupt (K6), and two kept behaviours of the vector validation (K7).
VEC_FAMILY = ["vec_nodes", "vec_nodes_chunks", "vec_nodes_rowids", "vec_nodes_info", "vec_nodes_auxiliary",
              "vec_nodes_vector_chunks00", "vec_nodes_metadatachunks00", "vec_nodes_metadatatext00"]
ADMITTED_TRIGGER = "nodes_metadata_json_guard_update"


def test_k1_the_allowlist_admits_exactly_the_measured_events_not_the_vec0_shadow_family():
    """Positive controls: every measured tuple is admitted. Sweep: over a generated grid of (action, table, column, db,
    trigger), the allowlist says yes to a combination iff it is one of the measured tuples. Mutants: the vec0 family
    admitted by name (`vec_nodes_info`, `_auxiliary`, ... pass), `x.<any column>`, UPDATE for any db."""
    allows = eo._publish_prepared_embeddings_allows
    for event in sorted(EXPECTED_EVENTS, key=repr):
        assert allows(*event), event
    tables = VEC_FAMILY + ["x", "nodes", "sqlite_master", "edges"]
    columns = [None, "", "embedding", "node_id", "chunk_id", "chunk_offset", "id", "rowid", "validity", "vectors",
               "metadata", "claim", "is_current", "type", "name", "quoted_text", "status", "other"]
    dbs = ["main", "temp", "attached", None]
    triggers = [None, ADMITTED_TRIGGER, "some_other_trigger"]
    wrong = []
    checked = 0
    for action in (S.SQLITE_READ, S.SQLITE_UPDATE, S.SQLITE_INSERT, S.SQLITE_DELETE):
        for table in tables:
            for column in columns:
                for db in dbs:
                    for trig in triggers:
                        event = (action, table, column, db, trig)
                        checked += 1
                        if allows(*event) != (event in EXPECTED_EVENTS):
                            wrong.append(event)
    assert checked == 4 * len(tables) * len(columns) * len(dbs) * len(triggers)
    assert wrong == [], wrong[:12]
    # From INSIDE the admitted trigger: a FUNCTION other than json_valid, and an UPDATE, are denied; a function the op
    # allows OUTSIDE a trigger (`max`) is denied inside it.
    for fn in ("json_valid", "max", "load_extension", "random", "json_extract", "printf"):
        event = (S.SQLITE_FUNCTION, None, fn, None, ADMITTED_TRIGGER)
        assert allows(*event) == (event in EXPECTED_EVENTS), event
        assert allows(S.SQLITE_FUNCTION, None, fn, None, "some_other_trigger") is False, fn
    for table, column in (("nodes", "metadata"), ("nodes", "embedding"), ("vec_nodes_rowids", "chunk_id")):
        assert allows(S.SQLITE_UPDATE, table, column, "main", ADMITTED_TRIGGER) is False, (table, column)


# ---- K2: the fingerprint covers text_sha256 and each epoch field -----------------------------------------------------

@requires_linux_owner
@pytest.mark.parametrize("what", ["text_sha256", "epoch_model", "epoch_dimension", "epoch_normalisation"])
def test_k2_the_same_request_id_with_a_changed_field_is_request_id_reused_and_writes_nothing(owner, home, what):
    """Mutants: the fingerprint ignores `text_sha256`; the fingerprint ignores the epoch (one cell per field)."""
    params = make_params(home, ["p0"])
    first = ok(call(owner, params, rid="rid-k2"))
    assert statuses(first) == [("p0", "refreshed", None)]
    after_first = all_state(home, ["p0", "p1", "p2"])
    changed = json.loads(json.dumps(params))
    if what == "text_sha256":
        changed["items"][0]["text_sha256"] = "0" * 64
    elif what == "epoch_model":
        changed["epoch"]["model"] = "some-other-model"
    elif what == "epoch_dimension":
        changed["epoch"]["dimension"] = 385
    else:
        changed["epoch"]["normalisation"] = "unit"
    refused(call(owner, changed, rid="rid-k2"), "invalid_params", "request_id_reused")
    assert all_state(home, ["p0", "p1", "p2"]) == after_first
    again = ok(call(owner, params, rid="rid-k2"))  # the original payload still replays
    assert again["duplicate"] is True


# ---- K3: a replayed raising read-back ---------------------------------------------------------------------------------

@requires_linux_owner
def test_k3_a_raising_readback_is_recorded_and_a_same_id_retry_replays_it_without_writing(owner, home):
    """Mutant: the raising-read-back path does not record its outcome (the retry re-runs and answers ok)."""
    real = owner._publish_prepared_readback

    def boom(*_a, **_k):
        raise RuntimeError("readback exploded")

    owner._publish_prepared_readback = boom
    params = make_params(home, ["p0"])
    first = refused(call(owner, params, rid="rid-k3"), "readback_mismatch")
    assert first.commit == "committed_unverified"
    assert state(home, "p0")[0] == json.dumps(params["items"][0]["vector"])  # the commit stands
    owner._publish_prepared_readback = real
    sql(home, "UPDATE nodes SET embedding = ? WHERE id = 'p0'", (json.dumps(vec(9.0)),))
    stmts = trace(owner)
    again = refused(call(owner, params, rid="rid-k3"), "readback_mismatch")
    assert again.commit == "committed_unverified"
    assert state(home, "p0")[0] == json.dumps(vec(9.0))  # the retry wrote nothing
    assert not any(s.startswith("BEGIN") for s in stmts), stmts


# ---- K4: the dedupe store is bounded ---------------------------------------------------------------------------------

@requires_linux_owner
def test_k4_past_the_cap_the_oldest_request_id_is_forgotten_and_a_recent_one_replays(owner, home, monkeypatch):
    """Mutant: no eviction (the oldest id still replays)."""
    monkeypatch.setattr(eo, "USE_REWARD_DEDUPE_CAP", 2)
    for rid, nid in (("rid-k4-a", "p0"), ("rid-k4-b", "p1"), ("rid-k4-c", "p2")):
        ok(call(owner, make_params(home, [nid]), rid=rid))
    assert len(owner._publish_prepared_seen) == 2
    recent = ok(call(owner, make_params(home, ["p2"]), rid="rid-k4-c"))
    assert recent["duplicate"] is True  # a recent id replays
    oldest = ok(call(owner, make_params(home, ["p0"]), rid="rid-k4-a"))
    assert "duplicate" not in oldest  # the oldest was forgotten: it re-ran
    assert statuses(oldest) == [("p0", "refreshed", None)]


# ---- K5: validation edges ---------------------------------------------------------------------------------------------

@requires_linux_owner
def test_k5a_a_repeated_node_id_in_one_call_is_refused_and_nothing_is_written(owner, home):
    """Mutant: the repeated-node-id check is removed."""
    params = make_params(home, ["p0", "p1"])
    params["items"][1] = dict(params["items"][1], node_id="p0")
    before = all_state(home, ["p0", "p1", "p2"])
    refused(call(owner, params), "invalid_params", "node_id_repeated")
    assert all_state(home, ["p0", "p1", "p2"]) == before


@requires_linux_owner
def test_k5b_the_node_id_limit_is_in_bytes_not_characters(owner, home):
    """Mutant: the byte limit is swapped for a character limit (a multi-byte id of fewer characters slips through)."""
    limit = eo.USE_REWARD_NODE_ID_MAX_BYTES
    assert limit % 2 == 0
    good = make_params(home, ["p0"])

    def with_id(nid):
        return dict(good, items=[dict(good["items"][0], node_id=nid)])

    over = "é" * (limit // 2 + 1)  # limit + 2 bytes, but only limit/2 + 1 characters
    assert len(over) < limit and len(over.encode("utf-8")) > limit
    before = all_state(home, ["p0", "p1", "p2"])
    refused(call(owner, with_id(over)), "invalid_params", "node_id_too_long")
    assert all_state(home, ["p0", "p1", "p2"]) == before
    exactly = "é" * (limit // 2)  # exactly `limit` bytes in limit/2 characters: within the byte limit
    assert len(exactly.encode("utf-8")) == limit
    for nid in (exactly, "a" * limit):  # and `limit` characters that are `limit` bytes: also within it
        result = ok(call(owner, with_id(nid)))  # accepted by validation (the node does not exist: it defers per item)
        assert result["counts"]["deferred"] == 1, nid[:3]
    refused(call(owner, with_id("a" * (limit + 1))), "invalid_params", "node_id_too_long")


@requires_linux_owner
def test_k5c_a_lone_surrogate_node_id_is_refused_by_name_not_as_a_generic_failure(owner, home):
    """Mutant: the lone-surrogate check is removed (the id reaches SQL binding and fails there)."""
    params = make_params(home, ["p0"])
    params["items"][0] = dict(params["items"][0], node_id="p\ud800x")
    before = all_state(home, ["p0", "p1", "p2"])
    refused(raw_write(owner, params), "invalid_params", "node_id_not_utf8")
    assert all_state(home, ["p0", "p1", "p2"]) == before


# ---- K6: a competing writer; a KeyboardInterrupt ----------------------------------------------------------------------

@requires_linux_owner
def test_k6_a_competing_writer_past_the_wait_budget_is_busy_nothing_committed_and_the_retry_succeeds(owner, home):
    """The write lock is held by another connection for longer than OWNER_WRITE_LOCK_WAIT: `busy`, determinate. Mutant:
    `BEGIN IMMEDIATE` becomes `BEGIN` (the collision is then met half way through, after reads ran): so no statement that
    names `nodes` may have run before the refusal."""
    params = make_params(home, ["p0"])
    before = all_state(home, ["p0", "p1", "p2"])
    rival = sqlite3.connect(str(home / "knowledge.db"), timeout=0)
    stmts = trace(owner)
    try:
        rival.execute("BEGIN IMMEDIATE")
        refused(call(owner, params, rid="rid-k6", timeout=10.0), "busy")
        busy_stmts = list(stmts)  # what ran during the refused call only (the retry below adds its own)
    finally:
        rival.rollback()
        rival.close()
    assert all_state(home, ["p0", "p1", "p2"]) == before  # nothing committed
    assert any(s.startswith("BEGIN IMMEDIATE") for s in busy_stmts), busy_stmts
    # the collision was met at BEGIN, before any statement named the `nodes` table (`vec_nodes` does not count)
    assert not any(re.search(r"\bnodes\b", s) for s in busy_stmts), busy_stmts
    assert not owner._owner_conn.in_transaction
    result = ok(call(owner, params, rid="rid-k6"))  # busy is not recorded: the same call runs once the lock is free
    assert statuses(result) == [("p0", "refreshed", None)]
    assert result.get("duplicate") is not True


@requires_linux_owner
def test_k6_a_keyboard_interrupt_from_the_primitive_propagates_leaves_no_transaction_and_the_next_call_works(owner, home, monkeypatch):
    """Mutant: the BaseException is swallowed or converted (the interrupt does not propagate), or the transaction is
    left open. Item 2 of 3 raises; item 1's write is rolled back with it."""
    warm(owner, home)
    prev_busy = owner._owner_conn.execute("PRAGMA busy_timeout").fetchone()[0]
    ids = ["p0", "p1", "p2"]
    params = make_params(home, ids)
    before = all_state(home, ids)
    real = pub.publish_embedding
    seen = []

    def interrupting(conn, nid, **kw):
        seen.append(nid)
        if len(seen) == 2:
            raise KeyboardInterrupt()
        return real(conn, nid, **kw)

    monkeypatch.setattr(pub, "publish_embedding", interrupting)
    with pytest.raises(KeyboardInterrupt):
        owner._handle_publish_prepared_embeddings(params, time.monotonic() + 10, "rid-k6-ki")
    assert seen == ["p0", "p1"]
    assert not owner._owner_conn.in_transaction
    assert all_state(home, ids) == before  # item 1 (written before the interrupt) stays old: rolled back
    monkeypatch.setattr(pub, "publish_embedding", real)
    _assert_restored(owner, prev_busy)
    result = ok(call(owner, params, rid="rid-k6-ki"))  # nothing was recorded: the same id runs fresh
    assert statuses(result) == [(nid, "refreshed", None) for nid in ids]
    assert result.get("duplicate") is not True


# ---- K7: two kept behaviours of the vector validation -----------------------------------------------------------------

@requires_linux_owner
def test_k7_a_finite_float64_beyond_the_float32_range_defers_that_item_and_the_call_is_ok(owner, home):
    """KEPT behaviour: the owner's validation (finite float64) passes 1e39; the primitive defers THAT item as
    `deferred / invalid_vector`, and the other items refresh."""
    ids = ["p0", "p1", "p2"]
    vectors = {"p0": vec(1.0), "p1": [1e39] + vec(2.0)[1:], "p2": vec(3.0)}
    assert math.isfinite(vectors["p1"][0])
    result = ok(call(owner, make_params(home, ids, vectors)))
    assert statuses(result) == [("p0", "refreshed", None), ("p1", "deferred", "invalid_vector"), ("p2", "refreshed", None)]
    assert result["counts"] == {"refreshed": 2, "pending": 0, "deferred": 1}
    assert state(home, "p1")[0] == json.dumps(OLD) and state(home, "p1")[2] == fvec(OLD)  # the deferred item is untouched
    assert state(home, "p0")[0] == json.dumps(vectors["p0"]) and state(home, "p2")[0] == json.dumps(vectors["p2"])


@requires_linux_owner
def test_k7_an_int_vector_is_stored_as_sent(owner, home):
    """KEPT behaviour: ints are passed on as sent (the stored text is `[1, 1, ...]`, as the in-process primitive writes
    it when given ints), not normalised to floats."""
    ints = [1] * 384
    result = ok(call(owner, make_params(home, ["p0"], {"p0": ints})))
    assert statuses(result) == [("p0", "refreshed", None)]
    assert state(home, "p0")[0] == json.dumps(ints) and state(home, "p0")[0].startswith("[1, 1,")
