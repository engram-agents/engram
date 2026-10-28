"""Contract cells for the owner's third and fourth writes, the typed `fill_embeddings` and `finish_embedding_regen`
ops (#3879, U16 of #2652; the OWNER half. The tool half is a separate unit against the same interface).

Written BEFORE the implementation (contract-first; project CLAUDE.md). Wire:

  write("fill_embeddings", {"model": str, "items": [{"node_id": str, "vector": [number * 384]}, ...]}, request_id=...)
    -> Ok(result={"filled", "skipped_not_null", "skipped_not_current_or_missing", "vec_mirrored"})   (+ "duplicate": True on a replay)
    -> Rejected(reason in invalid_params | bad_dimension | non_finite | model_mismatch | busy | write_denied |
                readback_mismatch ...)
  write("finish_embedding_regen", {"processed": int, "embedded": int, "skipped": int, "failed": int,
                                   "embedder_available": bool})        (the tool probes ITS embedder; the owner never does)
    -> Ok(result={"fts_rows": int, "restore_state": <cause string> | None})

The CAS is `UPDATE nodes SET embedding = ? WHERE id = ? AND is_current = 1 AND embedding IS NULL`; the whole call
is validated before any write; the vec mirror is best-effort and reported (`vec_mirrored`), never fatal; finish
rebuilds FTS on the owner's connection and runs today's clear-or-finalize branch through the SAME core functions
with `respect_lease=True`.

Self-contained: every store is built with the real schema (`engram_core._run_db_one_time_setup`) under tmp; nothing
here touches ~/.engram or its socket. The REAL vec0 cells need a Python whose sqlite3 can load extensions (Linux);
they skip elsewhere, and the mirror SQL is exercised everywhere against a plain stand-in `vec_nodes` table.
"""
from __future__ import annotations

import copy
import importlib.util
import inspect
import json
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_backup  # noqa: E402
import engram_core  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

MODEL = "all-MiniLM-L6-v2"
DIM = 384
TOOL_PATH = Path(_SRC_ENGRAM) / "tools" / "engram-regenerate-embeddings.py"


# --- fixtures ----------------------------------------------------------------------------------------


def _assert_scratch(home: Path) -> None:
    live = (Path.home() / ".engram").resolve()
    h = Path(home).resolve()
    assert h != live and live not in h.parents, h


def vec(i: int, dim: int = DIM) -> list:
    """A deterministic vector of long-repr doubles (the shape a float32 model's `.tolist()` produces)."""
    return [math.sin(i * 1000.0 + j) * 0.0371 for j in range(dim)]


def make_store(home: Path, *, n: int = 12, with_embedding=(), config="default", extra_null_evidence: bool = True,
               vec: bool = True) -> Path:
    """Real-schema store. Nodes e000..e{n-1}: observation_factual with a claim, current, embedding NULL, except the
    ids in `with_embedding` (a preset sentinel embedding). `vec=False` skips the vec0 load, so the store has NO real `vec_nodes` virtual table even
    on a seat where vec0 loads (the fake-vec-table cells then create their own plain stand-in). Plus: `gone` (is_current=0, NULL embedding), `ret`
    (retracted, current), `ev_blank` (evidence with no source_title: a permanent skip) and an edge."""
    _assert_scratch(home)
    home.mkdir(parents=True, exist_ok=True)
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        # The vec0 module must be loaded on THIS connection BEFORE the one-time setup: its `CREATE VIRTUAL TABLE
        # vec_nodes USING vec0(...)` otherwise raises OperationalError, which the setup swallows ("fall back to Python
        # cosine"), leaving the store without vec_nodes and the real-vec0 cell skipping everywhere (#3879, U16-E).
        if vec and can_load_vec0():
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        rows = []
        for i in range(n):
            nid = f"e{i:03d}"
            emb = "[0.5]" if nid in with_embedding else None
            rows.append((nid, "observation_factual", f"claim number {i} about thing {i % 7}", "2026-01-01T00:00:00", "{}", 1, "active", emb, None))
        rows.append(("gone", "observation_factual", "a superseded claim", "2026-01-01T00:00:00", "{}", 0, "active", None, None))
        rows.append(("ret", "observation_factual", "a retracted claim", "2026-01-01T00:00:00", "{}", 1, "active", None, "retracted"))
        if extra_null_evidence:
            rows.append(("ev_blank", "evidence", "", "2026-01-01T00:00:00", "{}", 1, "active", None, None))
        for r in rows:
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, memory_status, embedding, status)"
                " VALUES (?,?,?,?,?,?,?,?,?)", r)
        conn.execute("INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?,?,?,?)",
                     ("e000", "e001", "supports", "2026-01-01T00:00:00"))
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    if config == "default":
        (home / "config.json").write_text(json.dumps({"embedding": {"model": MODEL}, "memory": {"current_turn": 3}}))
    elif config is not None:
        (home / "config.json").write_text(config)
    return db


def make_big_store(home: Path, n: int = 1000, null_every: int = 3) -> Path:
    """~1,000 nodes, about a third NULL: the 'bulk fixture' of the concurrency and hold-time cells."""
    db = make_store(home, n=0, extra_null_evidence=False)
    conn = sqlite3.connect(str(db))
    rows = [(f"b{i:04d}", "observation_factual", f"bulk claim {i} word{i % 50} topic{i % 13}", "2026-01-01T00:00:00", "{}", 1,
             "active", None if i % null_every == 0 else "[0.25]", 0.3, 0.5, 1, 1, 0.4) for i in range(n)]
    conn.executemany(
        "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, memory_status, embedding,"
        " utility_score, importance_base, recall_count, recall_turn, importance_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def embed_all_embeddable(db: Path) -> None:
    """Give every current node that has embedding text an embedding, so a fresh recount finds nothing missing."""
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE nodes SET embedding = '[0.5]' WHERE embedding IS NULL AND is_current = 1 AND id <> 'ev_blank'")
    conn.commit()
    conn.close()


def embeddings(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT id, embedding FROM nodes ORDER BY id")}
    finally:
        conn.close()


def all_rows(db: Path) -> list:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()
    finally:
        conn.close()


def restore_row(db: Path):
    conn = sqlite3.connect(str(db))
    try:
        try:
            return conn.execute(
                "SELECT not_fully_restored, null_count, cause, updated_at FROM embedding_restore_state WHERE singleton_key = 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return "no-table"
    finally:
        conn.close()


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="efe-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _start(home, **kw):
    owner = eo.EngramdOwner(home, **kw)
    owner.start(install_signal_handlers=False)
    return owner


@pytest.fixture
def twins():
    """Two sibling homes under a SHORT temp dir (a pytest tmp_path puts the owner socket over the AF_UNIX limit)."""
    d = Path(tempfile.mkdtemp(prefix="efe-"))
    try:
        yield d / "a", d / "b"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def owner(home):
    make_store(home)
    o = _start(home)
    try:
        yield o
    finally:
        o.stop()


@pytest.fixture
def big_owner(home):
    make_big_store(home)
    o = _start(home)
    try:
        yield o
    finally:
        o.stop()


_rid = [0]


def fresh_rid() -> str:
    _rid[0] += 1
    return f"fe-rid-{_rid[0]}-{os.getpid()}"


def items_for(ids, start=0):
    return [(nid, vec(start + k)) for k, nid in enumerate(ids)]


def fill(owner, items=None, *, model=MODEL, request_id=None, params=None, timeout=5.0):
    if params is None:
        params = {"model": model, "items": [{"node_id": n, "vector": v} for n, v in items]}
    return ec.write("fill_embeddings", params, request_id=request_id or fresh_rid(), timeout=timeout,
                    socket_path=owner.socket_path)


def raw_write(owner, method, params, request_id=None):
    """Send a write over a raw AF_UNIX socket with `json.dumps(allow_nan=True)`: the shipped client refuses to serialise
    NaN / Infinity (`request_not_serializable`), so only a non-ec client can deliver them, and that is exactly the
    client the owner's own `math.isfinite` check exists for. Returns an `ec.Rejected` / `ec.Ok` built from the reply."""
    import socket as _socket
    msg = {"contract_version": ec.CONTRACT_VERSION, "method": method, "params": params, "request_id": request_id or fresh_rid()}
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(5.0)
    try:
        s.connect(str(owner.socket_path))
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


def finish(owner, processed=0, embedded=0, skipped=0, failed=0, *, embedder_available=True, params=None, timeout=5.0):
    if params is None:
        params = {"processed": processed, "embedded": embedded, "skipped": skipped, "failed": failed,
                  "embedder_available": embedder_available}
    return ec.write("finish_embedding_regen", params, timeout=timeout, socket_path=owner.socket_path)


def ok_result(res):
    assert isinstance(res, ec.Ok), res
    return res.result


def assert_rejected(res, reason, token=None):
    assert isinstance(res, ec.Rejected), res
    assert res.reason == reason, res
    if token is not None:
        assert res.detail.startswith(token), (token, res.detail)


def tracing(owner):
    stmts = []
    owner._owner_conn.set_trace_callback(lambda s: stmts.append(" ".join(s.split())))
    return stmts


def norm_json(vector) -> str:
    """What the direct path writes: json.dumps of the embedder's list of floats."""
    return json.dumps([float(x) for x in vector])


def struct_serializer(v):
    return struct.pack(f"{len(v)}f", *v)


def can_load_vec0() -> bool:
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        return False
    try:
        import sqlite_vec  # noqa: F401
    except Exception:
        return False
    return True


# --- constants, the closed write set, the kind registry ---------------------------------------------


def test_constants_are_named_and_pinned():
    assert eo.FILL_EMBEDDINGS_MAX_ITEMS == 50
    assert eo.FILL_EMBEDDINGS_DIM == DIM
    assert eo.USE_REWARD_NODE_ID_MAX_BYTES == 256
    assert eo.FILL_EMBEDDINGS_ADMITTED_TRIGGERS == frozenset()
    assert eo.FINISH_EMBEDDING_REGEN_ADMITTED_TRIGGERS == frozenset()
    assert engram_core.DEFAULT_EMBEDDING_MODEL == MODEL


def test_the_dimension_constant_is_the_one_the_server_stores():
    """There is no single DIM constant in engram_core (the 384 is a literal in the vec0 DDL and the vec mirror):
    this cell is the drift tripwire tying the owner's constant to those literals."""
    src = inspect.getsource(engram_core)
    assert f"embedding float[{eo.FILL_EMBEDDINGS_DIM}]" in src
    assert f"len(vector) == {eo.FILL_EMBEDDINGS_DIM}" in src


def test_the_closed_write_set_is_exactly_the_four_ops():
    assert eo.WRITE_METHODS == frozenset({"use_reward", "advance_moment_cursor", "fill_embeddings", "finish_embedding_regen"})


@requires_linux_owner
def test_both_new_ops_are_in_the_table_classified_write_and_the_read_set_is_unchanged(owner):
    assert set(owner._method_kinds) == set(owner._methods)
    assert owner._method_kinds["fill_embeddings"] == "write"
    assert owner._method_kinds["finish_embedding_regen"] == "write"
    assert {m for m, k in owner._method_kinds.items() if k == "write"} == set(eo.WRITE_METHODS)
    assert {m for m, k in owner._method_kinds.items() if k == "read"} == {
        "ping", "snapshot", "read", "df", "chain", "confidence_by_type"}


@requires_linux_owner
@pytest.mark.parametrize("op,params", [
    ("fill_embeddings", {"model": MODEL, "items": [{"node_id": "e000", "vector": vec(0)}]}),
    ("finish_embedding_regen", {"processed": 0, "embedded": 0, "skipped": 0, "failed": 0}),
])
def test_a_new_write_declared_as_a_read_is_refused(owner, op, params):
    owner._method_kinds[op] = "read"
    res = ec.write(op, params, request_id=fresh_rid(), timeout=3.0, socket_path=owner.socket_path)
    assert_rejected(res, "method_kind_mismatch")
    assert all(v is None for k, v in embeddings(owner.store_path).items() if k.startswith("e"))


def test_the_handlers_run_through_the_one_shared_write_scaffold():
    """No parallel scaffold: both handlers go through `_owner_write`."""
    for name in ("_handle_fill_embeddings", "_handle_finish_embedding_regen"):
        assert "self._owner_write(" in inspect.getsource(getattr(eo.EngramdOwner, name)), name


def test_importing_the_owner_does_not_import_core_or_backup_and_works_in_the_flattened_layout(tmp_path):
    """The new imports are LAZY (engram_core is large; engram_backup is only needed by finish): a flattened plugin
    layout holding the owner's side-by-side modules imports it with neither of them loaded."""
    flat = tmp_path / "plugin"
    flat.mkdir()
    src = Path(_SRC_ENGRAM)
    for name in ["engramd_owner.py", "engramd_client.py", "engramd_read_registry.py", "engram_pidfile.py", "engram_walguard.py",
                 "engram_regular_io.py", "engram_restamp.py", "engram_config_io.py", "engram_moment_cursor.py",
                 "engram_home_resolver.py"]:
        shutil.copy2(src / name, flat / name)
    other = tmp_path / "elsewhere"
    other.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH", "ENGRAM_HOME")}
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import engramd_owner as o;"
        "print(sorted(o.WRITE_METHODS)); print('engram_core' in sys.modules, 'engram_backup' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code, str(flat)], cwd=other, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert "fill_embeddings" in lines[0] and "finish_embedding_regen" in lines[0]
    assert lines[1] == "False False"


# --- (4) validation is atomic: each violation its own typed reason, NOTHING written -------------------


def _bad_cases():
    good = vec(1)

    def w(items, **top):
        p = {"model": MODEL, "items": items}
        p.update(top)
        return p

    def item(vector=None, node_id="e002", **extra):
        d = {"node_id": node_id, "vector": good if vector is None else vector}
        d.update(extra)
        return d

    def with_good(bad_item):
        # a VALID first item, so a validator that writes as it goes would visibly write e001
        return w([{"node_id": "e001", "vector": vec(9)}, bad_item])

    nan, inf, ninf = float("nan"), float("inf"), float("-inf")
    return [
        ("wrong_dimension_short", with_good(item(good[:383])), "bad_dimension", "bad_dimension"),
        ("wrong_dimension_long", with_good(item(good + [0.1])), "bad_dimension", "bad_dimension"),
        ("empty_vector", with_good(item([])), "bad_dimension", "bad_dimension"),
        ("nan", with_good(item(good[:5] + [nan] + good[6:])), "non_finite", "non_finite"),
        ("plus_inf", with_good(item(good[:5] + [inf] + good[6:])), "non_finite", "non_finite"),
        ("minus_inf", with_good(item(good[:5] + [ninf] + good[6:])), "non_finite", "non_finite"),
        ("int_too_large_for_float", with_good(item(good[:5] + [10 ** 400] + good[6:])), "non_finite", "non_finite"),
        ("bool_element", with_good(item(good[:5] + [True] + good[6:])), "invalid_params", "vector_element_type"),
        ("str_element", with_good(item(good[:5] + ["0.1"] + good[6:])), "invalid_params", "vector_element_type"),
        ("none_element", with_good(item(good[:5] + [None] + good[6:])), "invalid_params", "vector_element_type"),
        ("vector_not_list", with_good(item("0.1,0.2")), "invalid_params", "vector_not_list"),
        ("vector_tuple_like_dict", with_good(item({"0": 1})), "invalid_params", "vector_not_list"),
        ("fifty_one_items", w([{"node_id": f"e{i:03d}", "vector": vec(i)} for i in range(51)]), "invalid_params", "items_too_many"),
        ("zero_items", w([]), "invalid_params", "items_empty"),
        ("items_not_list", w("e001"), "invalid_params", "items_not_list"),
        ("unknown_top_level_key", w([item()], extra="x"), "invalid_params", "params_shape"),
        ("request_id_in_params", w([item()], request_id="r"), "invalid_params", "params_shape"),
        ("missing_model", {"items": [item()]}, "invalid_params", "params_shape"),
        ("missing_items", {"model": MODEL}, "invalid_params", "params_shape"),
        ("params_not_dict", [item()], "invalid_params", "params_shape"),
        ("unknown_item_key", with_good(item(extra="x")), "invalid_params", "item_shape"),
        ("item_missing_vector", with_good({"node_id": "e002"}), "invalid_params", "item_shape"),
        ("item_not_dict", with_good(["e002", good]), "invalid_params", "item_shape"),
        ("model_not_str", w([item()], model=7), "invalid_params", "model_not_str"),
        ("model_mismatch", w([item()], model="some-other-model"), "model_mismatch", "model_mismatch"),
        ("model_mismatch_case", w([item()], model="ALL-MINILM-L6-V2"), "model_mismatch", "model_mismatch"),
        ("node_id_not_str", with_good(item(node_id=5)), "invalid_params", "node_id_not_str"),
        ("node_id_empty", with_good(item(node_id="")), "invalid_params", "node_id_empty"),
        ("node_id_257_bytes", with_good(item(node_id="a" * 257)), "invalid_params", "node_id_too_long"),
        ("node_id_multibyte_too_long", with_good(item(node_id="é" * 129)), "invalid_params", "node_id_too_long"),
        ("node_id_lone_surrogate", with_good(item(node_id="e\ud800")), "invalid_params", "node_id_not_utf8"),
    ]


def test_every_violation_class_in_the_matrix_is_distinct():
    """Each (reason, token) pair is its own typed refusal, except the grouped numeric cases (NaN / +-Infinity / an int
    that cannot be a float are all `non_finite`; short / long / empty are all `bad_dimension`)."""
    cases = _bad_cases()
    pairs = {(reason, token) for _l, _p, reason, token in cases}
    assert len(pairs) == 15, sorted(pairs)  # 12 `invalid_params` tokens + bad_dimension + non_finite + model_mismatch
    assert {r for r, _t in pairs} == {"invalid_params", "bad_dimension", "non_finite", "model_mismatch"}


@requires_linux_owner
@pytest.mark.parametrize("label,params,reason,token", _bad_cases(), ids=[c[0] for c in _bad_cases()])
def test_validation_is_atomic_each_violation_has_its_typed_reason_and_nothing_is_written(owner, label, params, reason, token):
    before = all_rows(owner.store_path)
    stmts = tracing(owner)
    res = raw_write(owner, "fill_embeddings", params) if label in ("nan", "plus_inf", "minus_inf") else fill(owner, params=params)
    assert_rejected(res, reason, token)
    # nothing the caller sent is echoed back (a vector is large, an id is caller text)
    assert "claim number" not in res.detail + res.remedy
    # refused BEFORE any SQL: no statement, no transaction; the valid item that preceded the bad one is untouched
    assert stmts == [], stmts
    assert owner._owner_conn.in_transaction is False
    assert all_rows(owner.store_path) == before
    assert embeddings(owner.store_path)["e001"] is None


@requires_linux_owner
def test_the_item_count_boundary_is_inclusive_fifty_is_ok(home):
    make_store(home, n=60)
    o = _start(home)
    try:
        r = ok_result(fill(o, items_for([f"e{i:03d}" for i in range(50)])))
        assert r["filled"] == 50
    finally:
        o.stop()


@requires_linux_owner
def test_a_torn_config_is_a_typed_refusal_not_a_guess_and_writes_nothing(home):
    make_store(home, config="{torn")
    o = _start(home)
    try:
        before = all_rows(o.store_path)
        res = fill(o, items_for(["e001"]))
        assert_rejected(res, "model_config_unreadable")
        assert all_rows(o.store_path) == before
    finally:
        o.stop()


@requires_linux_owner
def test_the_model_comes_from_the_owners_own_home_config_never_from_the_client(home):
    make_store(home, config=json.dumps({"embedding": {"model": "custom-model-v2"}}))
    o = _start(home)
    try:
        assert_rejected(fill(o, items_for(["e001"]), model=MODEL), "model_mismatch")
        r = ok_result(fill(o, items_for(["e001"]), model="custom-model-v2"))
        assert r["filled"] == 1
    finally:
        o.stop()


@requires_linux_owner
def test_a_config_without_an_embedding_block_means_the_default_model(home):
    make_store(home, config=json.dumps({"memory": {"current_turn": 1}}))
    o = _start(home)
    try:
        assert ok_result(fill(o, items_for(["e001"]), model=MODEL))["filled"] == 1
    finally:
        o.stop()


@requires_linux_owner
def test_a_missing_wire_request_id_is_refused_and_nothing_is_written(owner):
    before = all_rows(owner.store_path)
    params = {"model": MODEL, "items": [{"node_id": "e001", "vector": vec(1)}]}
    for bad in (None, "", 5, "r" * 129):
        res = owner._handle_fill_embeddings(copy.deepcopy(params), time.monotonic() + 5.0, bad)
        assert res["status"] == "rejected" and res["reason"] == "invalid_params" and res["detail"].startswith("request_id"), (bad, res)
    assert all_rows(owner.store_path) == before


# --- the happy path, the ack, the CAS (2), encoding parity ------------------------------------------


@requires_linux_owner
def test_fill_writes_the_embedding_text_and_acks_the_four_counts(owner):
    items = items_for(["e001", "e002", "e003"])
    r = ok_result(fill(owner, items))
    assert set(r) == {"filled", "skipped_not_null", "skipped_not_current_or_missing", "vec_mirrored"}
    assert (r["filled"], r["skipped_not_null"], r["skipped_not_current_or_missing"]) == (3, 0, 0)
    assert isinstance(r["vec_mirrored"], bool)
    emb = embeddings(owner.store_path)
    for nid, v in items:
        assert emb[nid] == norm_json(v)
    assert emb["e004"] is None  # an id not in the call is never touched


@requires_linux_owner
def test_cas_a_node_embedded_before_the_call_comes_back_skipped_not_null_and_is_not_overwritten(home):
    make_store(home, with_embedding={"e002"})
    o = _start(home)
    try:
        r = ok_result(fill(o, items_for(["e001", "e002", "e003"])))
        assert (r["filled"], r["skipped_not_null"], r["skipped_not_current_or_missing"]) == (2, 1, 0)
        emb = embeddings(o.store_path)
        assert emb["e002"] == "[0.5]"  # the sentinel survives: the CAS never overwrites
        assert emb["e001"] == norm_json(vec(0)) and emb["e003"] == norm_json(vec(2))
    finally:
        o.stop()


@requires_linux_owner
def test_cas_a_missing_id_and_a_non_current_id_are_skipped_not_current_or_missing(owner):
    r = ok_result(fill(owner, [("nope", vec(0)), ("gone", vec(1)), ("e001", vec(2))]))
    assert (r["filled"], r["skipped_not_null"], r["skipped_not_current_or_missing"]) == (1, 0, 2)
    emb = embeddings(owner.store_path)
    assert emb["gone"] is None and "nope" not in emb and emb["e001"] == norm_json(vec(2))


@requires_linux_owner
def test_cas_mixed_call_exact_counts(home):
    make_store(home, with_embedding={"e002", "e004"})
    o = _start(home)
    try:
        items = [("e001", vec(0)), ("e002", vec(1)), ("nope", vec(2)), ("e003", vec(3)), ("gone", vec(4)), ("e004", vec(5)), ("ghost", vec(6))]
        r = ok_result(fill(o, items))
        assert (r["filled"], r["skipped_not_null"], r["skipped_not_current_or_missing"]) == (2, 2, 3)
        emb = embeddings(o.store_path)
        assert emb["e002"] == "[0.5]" and emb["e004"] == "[0.5]" and emb["gone"] is None
    finally:
        o.stop()


@requires_linux_owner
def test_cas_a_non_current_node_that_already_has_an_embedding_is_not_current_or_missing_not_skipped_not_null(home):
    """Mutation: drop `row[0] == 1` from the skip classification -> a superseded node holding a vector is counted
    skipped_not_null (it is not CURRENT; the 'already embedded' bucket is for current rows only) -> red."""
    make_store(home, with_embedding={"e002"})
    db = home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE nodes SET embedding = '[0.5]' WHERE id = 'gone'")  # non-current AND embedded
    conn.commit()
    conn.close()
    o = _start(home)
    try:
        items = [("e001", vec(0)), ("e002", vec(1)), ("gone", vec(2)), ("nope", vec(3)), ("e003", vec(4))]
        r = ok_result(fill(o, items))
        # e001/e003 filled; e002 current+embedded -> skipped_not_null; gone (non-current, embedded) and nope -> the other bucket
        assert (r["filled"], r["skipped_not_null"], r["skipped_not_current_or_missing"]) == (2, 1, 2)
        assert embeddings(o.store_path)["gone"] == "[0.5]"  # and it is never overwritten
    finally:
        o.stop()


@requires_linux_owner
def test_cas_the_same_id_twice_in_one_call_fills_once_and_skips_the_second(owner):
    r = ok_result(fill(owner, [("e001", vec(0)), ("e001", vec(1))]))
    assert (r["filled"], r["skipped_not_null"], r["skipped_not_current_or_missing"]) == (1, 1, 0)
    assert embeddings(owner.store_path)["e001"] == norm_json(vec(0))


@requires_linux_owner
def test_cas_a_retracted_current_node_is_still_filled_the_predicate_is_current_and_null_only(owner):
    r = ok_result(fill(owner, [("ret", vec(0))]))
    assert r["filled"] == 1


@requires_linux_owner
def test_nothing_but_the_embedding_column_of_the_changed_rows_changes(owner):
    cols = [r[1] for r in sqlite3.connect(str(owner.store_path)).execute("PRAGMA table_info(nodes)")]
    before = all_rows(owner.store_path)
    ok_result(fill(owner, items_for(["e001", "e002"])))
    after = all_rows(owner.store_path)
    emb_i = cols.index("embedding")
    changed = set()
    for b, a in zip(before, after):
        for i, (x, y) in enumerate(zip(b, a)):
            if x != y:
                changed.add((b[0], cols[i]))
    assert changed == {("e001", "embedding"), ("e002", "embedding")}
    assert emb_i >= 0


def _direct_tool_core():
    spec = importlib.util.spec_from_file_location("regen_tool_under_test_3879", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@requires_linux_owner
def test_encoding_parity_the_text_written_is_byte_identical_to_the_direct_cores(twins):
    """Run the DIRECT core (`_backfill_embeddings_core`) on a twin store with a stub embedder that returns the
    vectors; the owner fills the same ids with the same vectors, SENT AS A MIX OF INTS AND FLOATS (an int `0` would
    serialise as `0` where the embedder's float serialises as `0.0`: the owner normalises to float)."""
    ha, hb = twins
    dba = make_store(ha, n=6)
    dbb = make_store(hb, n=6)
    ids = [f"e{i:03d}" for i in range(6)]
    mixed = []
    for k in range(6):
        v = vec(k)
        v[0] = 0.0
        v[1] = 1.0
        v[2] = -0.0
        v[3] = 1e-7
        v[4] = 12345678.5
        v[5] = 5e-324
        mixed.append(v)
    wire = copy.deepcopy(mixed)
    for v in wire:  # what a client might send: integral floats as ints
        v[0] = 0
        v[1] = 1
    # the direct path
    tool = _direct_tool_core()
    table = {f"claim number {k} about thing {k % 7}": mixed[k] for k in range(6)}
    stub = types.SimpleNamespace(
        _embedding_text_for_node=engram_core._embedding_text_for_node,
        _embedder=types.SimpleNamespace(embed_batch=lambda texts, model: [table.get(t, vec(900)) for t in texts]),
    )
    conn = sqlite3.connect(str(dbb))
    conn.row_factory = sqlite3.Row
    counts = tool._backfill_embeddings_core(conn, stub, MODEL, False)
    conn.close()
    assert counts["embedded"] >= 6
    # the owner
    o = _start(ha)
    try:
        r = ok_result(fill(o, list(zip(ids, wire))))
        assert r["filled"] == 6
    finally:
        o.stop()
    ea, eb = embeddings(dba), embeddings(dbb)
    for nid in ids:
        assert ea[nid] == eb[nid], nid
    assert ea["e000"].startswith("[0.0, 1.0, -0.0, 1e-07, 12345678.5, 5e-324")


# --- (5) read-back mismatch --------------------------------------------------------------------------


@requires_linux_owner
def test_a_post_commit_embedding_that_differs_is_readback_mismatch_committed_unverified_and_names_the_id(owner):
    real = owner._fill_embeddings_readback

    def perturbing(conn, ids):
        out = dict(real(conn, ids))
        out["e002"] = out["e002"] + " "
        return out

    owner._fill_embeddings_readback = perturbing
    res = fill(owner, items_for(["e001", "e002"]))
    assert_rejected(res, "readback_mismatch")
    assert res.commit == "committed_unverified"
    assert "e002" in res.detail and "e001" not in res.detail
    # the commit stands (it cannot be undone), reported loudly
    assert embeddings(owner.store_path)["e002"] == norm_json(vec(1))


@requires_linux_owner
def test_a_readback_that_raises_is_also_committed_unverified_never_a_bare_error(owner):
    def boom(conn, ids):
        raise sqlite3.OperationalError("disk I/O error")

    owner._fill_embeddings_readback = boom
    res = fill(owner, items_for(["e001"]))
    assert_rejected(res, "readback_mismatch")
    assert res.commit == "committed_unverified"
    assert embeddings(owner.store_path)["e001"] == norm_json(vec(0))


@requires_linux_owner
def test_a_readback_mismatch_replay_returns_the_same_refusal_and_writes_nothing_twice(owner):
    real = owner._fill_embeddings_readback
    owner._fill_embeddings_readback = lambda conn, ids: {k: (v or "") + "x" for k, v in real(conn, ids).items()}
    rid = fresh_rid()
    first = fill(owner, items_for(["e001"]), request_id=rid)
    assert_rejected(first, "readback_mismatch")
    # something else clears the row; the replay must NOT re-run the write
    conn = sqlite3.connect(str(owner.store_path))
    conn.execute("UPDATE nodes SET embedding = NULL WHERE id = 'e001'")
    conn.commit()
    conn.close()
    again = fill(owner, items_for(["e001"]), request_id=rid)
    assert_rejected(again, "readback_mismatch")
    assert embeddings(owner.store_path)["e001"] is None


# --- idempotency -------------------------------------------------------------------------------------


@requires_linux_owner
def test_the_same_request_id_replayed_returns_the_stored_outcome_with_duplicate_and_does_not_write_twice(owner):
    rid = fresh_rid()
    items = items_for(["e001", "e002"])
    first = ok_result(fill(owner, items, request_id=rid))
    assert "duplicate" not in first and first["filled"] == 2
    # externally clear one row: a re-executed write would fill it again
    conn = sqlite3.connect(str(owner.store_path))
    conn.execute("UPDATE nodes SET embedding = NULL WHERE id = 'e001'")
    conn.commit()
    conn.close()
    stmts = tracing(owner)
    again = ok_result(fill(owner, items, request_id=rid))
    assert again.get("duplicate") is True
    assert {k: v for k, v in again.items() if k != "duplicate"} == first
    assert embeddings(owner.store_path)["e001"] is None  # NOT written twice
    assert not [s for s in stmts if s.startswith("UPDATE")], stmts


@requires_linux_owner
def test_a_request_id_reused_for_a_different_payload_is_refused(owner):
    rid = fresh_rid()
    ok_result(fill(owner, items_for(["e001"]), request_id=rid))
    res = fill(owner, items_for(["e002"]), request_id=rid)
    assert_rejected(res, "invalid_params", "request_id_reused")
    res = fill(owner, [("e001", vec(5))], request_id=rid)  # same id, different vector
    assert_rejected(res, "invalid_params", "request_id_reused")
    assert embeddings(owner.store_path)["e002"] is None


@requires_linux_owner
def test_a_rejected_call_is_not_recorded_so_the_same_request_id_retry_runs(owner):
    rid = fresh_rid()
    bad = {"model": "wrong", "items": [{"node_id": "e001", "vector": vec(0)}]}
    assert_rejected(fill(owner, params=bad, request_id=rid), "model_mismatch")
    r = ok_result(fill(owner, items_for(["e001"]), request_id=rid))
    assert r["filled"] == 1 and "duplicate" not in r


@requires_linux_owner
def test_two_concurrent_same_request_id_calls_write_once(owner):
    rid = fresh_rid()
    items = items_for([f"e{i:03d}" for i in range(10)])
    out = []

    def go():
        out.append(fill(owner, items, request_id=rid))

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    results = [ok_result(r) for r in out]
    assert sorted(r.get("duplicate", False) for r in results) == [False, True]
    assert all(r["filled"] == 10 for r in results)


# --- the vec mirror ----------------------------------------------------------------------------------


# The stand-in must be a PLAIN table. Callers build the store with `make_store(..., vec=False)`: with vec0 loaded the store
# already has the real `vec_nodes` virtual table and this CREATE would collide.
def _fake_vec_table(db: Path, preexisting=()):
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE vec_nodes (node_id TEXT, embedding BLOB)")
    for nid, blob in preexisting:
        conn.execute("INSERT INTO vec_nodes VALUES (?, ?)", (nid, blob))
    conn.commit()
    conn.close()


def _vec_rows(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        out: dict = {}
        for nid, blob in conn.execute("SELECT node_id, embedding FROM vec_nodes ORDER BY rowid"):
            out.setdefault(nid, []).append(bytes(blob))
        return out
    finally:
        conn.close()


@requires_linux_owner
def test_vec_mirror_only_the_changed_rows_are_mirrored_with_serialize_float32_bytes(home, monkeypatch):
    db = make_store(home, with_embedding={"e002"}, vec=False)
    _fake_vec_table(db, preexisting=[("e001", b"stale"), ("e002", b"untouched")])
    o = _start(home)
    try:
        monkeypatch.setattr(o, "_fill_vec_ready", lambda conn: struct_serializer)
        r = ok_result(fill(o, [("e001", vec(0)), ("e002", vec(1)), ("nope", vec(2)), ("e003", vec(3))]))
        assert (r["filled"], r["skipped_not_null"], r["skipped_not_current_or_missing"]) == (2, 1, 1)
        assert r["vec_mirrored"] is True
        rows = _vec_rows(db)
        assert rows["e001"] == [struct_serializer(vec(0))]  # DELETE + INSERT replaced the stale row
        assert rows["e003"] == [struct_serializer(vec(3))]
        assert rows["e002"] == [b"untouched"]  # a skipped row is never mirrored (nor touched)
        assert "nope" not in rows
    finally:
        o.stop()


def test_the_stand_in_fixture_yields_a_plain_vec_nodes_table_not_a_vec0_virtual_table(home):
    """Guards the fake-vec-table cells' fixture: `make_store(vec=False)` + `_fake_vec_table` must give a plain table on
    every seat. On a seat where vec0 loads, a regression (vec0 loaded before setup) makes this fail."""
    db = make_store(home, vec=False)
    _fake_vec_table(db)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'vec_nodes'").fetchone()
    finally:
        conn.close()
    assert row is not None and row[0]
    assert "USING VEC0" not in " ".join(row[0].split()).upper(), row[0]


@requires_linux_owner
def test_vec_mirror_unavailable_the_fill_still_succeeds_and_says_so(owner, monkeypatch):
    monkeypatch.setattr(owner, "_fill_vec_ready", lambda conn: None)
    r = ok_result(fill(owner, items_for(["e001", "e002"])))
    assert r["filled"] == 2 and r["vec_mirrored"] is False
    assert embeddings(owner.store_path)["e001"] == norm_json(vec(0))


@requires_linux_owner
def test_a_vec_statement_failure_is_vec_mirrored_false_not_a_failed_fill_and_the_transaction_is_not_poisoned(owner, monkeypatch):
    """The serializer says 'ready' but there is no vec_nodes table: the mirror statement fails inside its own
    savepoint, the nodes writes stand, and the ack reports it (today's direct path swallows this silently)."""
    monkeypatch.setattr(owner, "_fill_vec_ready", lambda conn: struct_serializer)
    r = ok_result(fill(owner, items_for(["e001", "e002"])))
    assert r["filled"] == 2 and r["vec_mirrored"] is False
    emb = embeddings(owner.store_path)
    assert emb["e001"] == norm_json(vec(0)) and emb["e002"] == norm_json(vec(1))
    assert owner._owner_conn.in_transaction is False


@requires_linux_owner
def test_an_authorizer_denial_inside_the_vec_phase_degrades_the_mirror_only(home, monkeypatch):
    db = make_store(home, vec=False)
    _fake_vec_table(db)
    o = _start(home)
    try:
        real = eo._fill_embeddings_allows
        monkeypatch.setattr(eo, "_fill_embeddings_allows",
                            lambda a, b, c, d, e: False if (b == "vec_nodes" and a in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE)) else real(a, b, c, d, e))
        monkeypatch.setattr(o, "_fill_vec_ready", lambda conn: struct_serializer)
        r = ok_result(fill(o, items_for(["e001"])))
        assert r["filled"] == 1 and r["vec_mirrored"] is False
        assert _vec_rows(db) == {}
        assert embeddings(db)["e001"] == norm_json(vec(0))
    finally:
        o.stop()


@requires_linux_owner
def test_vec_mirror_unchanged_rows_are_never_mirrored_even_when_every_item_skips(home, monkeypatch):
    db = make_store(home, with_embedding={"e001", "e002"}, vec=False)
    _fake_vec_table(db)
    o = _start(home)
    try:
        monkeypatch.setattr(o, "_fill_vec_ready", lambda conn: struct_serializer)
        r = ok_result(fill(o, items_for(["e001", "e002"])))
        assert r["filled"] == 0 and r["skipped_not_null"] == 2
        assert _vec_rows(db) == {}
    finally:
        o.stop()


def _fake_sqlite_vec(monkeypatch, calls, *, load_raises=False):
    fake = types.ModuleType("sqlite_vec")

    def load(conn):
        calls.append("load")
        if load_raises:
            raise sqlite3.OperationalError("cannot open shared object file")

    fake.load = load
    fake.serialize_float32 = struct_serializer
    monkeypatch.setitem(sys.modules, "sqlite_vec", fake)


def test_vec_ready_turns_extension_loading_on_only_around_the_load_and_caches_the_verdict(home, monkeypatch):
    """`sqlite_vec.load` does NOT enable extension loading itself (the direct path enables it first): the probe must,
    and must switch it back off. The verdict is cached for the owner connection's life."""
    calls: list = []
    _fake_sqlite_vec(monkeypatch, calls)

    class Conn:
        def enable_load_extension(self, on):
            calls.append(("enable", on))

    o = eo.EngramdOwner(home)
    assert o._fill_vec_ready(Conn()) is struct_serializer
    assert calls == [("enable", True), "load", ("enable", False)]
    assert o._fill_vec_ready(object()) is struct_serializer  # cached: the second connection object is never touched
    assert len(calls) == 3


def test_vec_ready_is_none_and_cached_where_the_python_cannot_enable_extension_loading(home, monkeypatch):
    calls: list = []
    _fake_sqlite_vec(monkeypatch, calls)
    o = eo.EngramdOwner(home)
    assert o._fill_vec_ready(object()) is None  # no enable_load_extension attribute (the macOS framework Python)
    assert calls == []

    class Capable:
        def enable_load_extension(self, on):
            calls.append(("enable", on))

    assert o._fill_vec_ready(Capable()) is None  # the verdict is per connection life: it is not retried per call
    assert calls == []


def test_a_failed_vec_load_still_switches_extension_loading_back_off(home, monkeypatch):
    calls: list = []
    _fake_sqlite_vec(monkeypatch, calls, load_raises=True)

    class Conn:
        def enable_load_extension(self, on):
            calls.append(("enable", on))

    o = eo.EngramdOwner(home)
    assert o._fill_vec_ready(Conn()) is None
    assert calls == [("enable", True), "load", ("enable", False)]


def test_the_default_vec_serializer_is_sqlite_vecs_own():
    sqlite_vec = pytest.importorskip("sqlite_vec")
    v = vec(3)
    assert sqlite_vec.serialize_float32(v) == struct_serializer(v)


@requires_linux_owner
def test_the_real_vec0_table_is_mirrored_by_the_owner(home):
    """REAL vec0 (Linux seats; skipped where this Python cannot load an extension)."""
    if not can_load_vec0():
        pytest.skip("this Python's sqlite3 cannot enable_load_extension, or sqlite_vec is not installed")
    import sqlite_vec
    db = make_store(home)
    probe = sqlite3.connect(str(db))
    has = probe.execute("SELECT 1 FROM sqlite_master WHERE name = 'vec_nodes'").fetchone()
    probe.close()
    if not has:
        pytest.skip("the fixture store has no vec_nodes (engram_core's vec backend is off in this process)")
    o = _start(home)
    try:
        r = ok_result(fill(o, items_for(["e001", "e002"])))
        assert r["filled"] == 2 and r["vec_mirrored"] is True
    finally:
        o.stop()
    conn = sqlite3.connect(str(db))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    got = {nid: bytes(b) for nid, b in conn.execute("SELECT node_id, embedding FROM vec_nodes")}
    conn.close()
    assert got["e001"] == sqlite_vec.serialize_float32(vec(0)) and got["e002"] == sqlite_vec.serialize_float32(vec(1))


@requires_linux_owner
def test_where_this_python_cannot_load_vec0_the_real_default_reports_vec_mirrored_false(owner):
    if can_load_vec0():
        pytest.skip("this Python can load vec0; the default path is covered by the real-vec0 cell")
    r = ok_result(fill(owner, items_for(["e001"])))
    assert r["filled"] == 1 and r["vec_mirrored"] is False


# --- the op-scoped authorizer, the statement-set pin, the measurement --------------------------------


def _norm(s: str) -> str:
    import re
    s = re.sub(r"'(?:[^']|'')*'", "?", s.strip())
    return " ".join(re.sub(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])", "?", s).split())


FILL_STATEMENTS = {
    "BEGIN IMMEDIATE",
    "UPDATE nodes SET embedding = ? WHERE id = ? AND is_current = ? AND embedding IS NULL",
    "SELECT is_current, embedding IS NULL FROM nodes WHERE id = ?",
    "SELECT embedding FROM nodes WHERE id = ?",  # the read-back
    "COMMIT",
    "PRAGMA busy_timeout",
    "PRAGMA busy_timeout = ?",
}


@requires_linux_owner
def test_statement_set_pin_the_fill_executes_exactly_the_closed_list(owner, monkeypatch):
    monkeypatch.setattr(owner, "_fill_vec_ready", lambda conn: None)
    stmts = tracing(owner)
    ok_result(fill(owner, [("e001", vec(0)), ("gone", vec(1)), ("e002", vec(2))]))
    seen = {_norm(s) for s in stmts}
    assert seen == FILL_STATEMENTS, seen ^ FILL_STATEMENTS
    assert not [s for s in stmts if s.startswith("--")]  # no trigger-originated statement


FILL_MEASURED_TUPLES = {
    # (action, arg1, arg2, db_name, trigger_or_view) from ONE real fill on the real schema (vec phase excluded:
    # it needs vec0, which this Python may not be able to load; its allowlist is pinned below)
    (sqlite3.SQLITE_TRANSACTION, "BEGIN", None, None, None),
    (sqlite3.SQLITE_UPDATE, "nodes", "embedding", "main", None),
    (sqlite3.SQLITE_READ, "nodes", "id", "main", None),
    (sqlite3.SQLITE_READ, "nodes", "is_current", "main", None),
    (sqlite3.SQLITE_READ, "nodes", "embedding", "main", None),
    (sqlite3.SQLITE_SELECT, None, None, None, None),
    (sqlite3.SQLITE_TRANSACTION, "COMMIT", None, None, None),
}


def _record_authorizer_calls(owner, run):
    """Wrap the op guard's authorizer so every authorizer call during `run()` is recorded, then delegate to it."""
    calls = []
    real_cls = eo._UseRewardGuard

    class Recording(real_cls):
        def authorizer(self, *a):
            calls.append(tuple(a))
            return super().authorizer(*a)

    eo._UseRewardGuard = Recording
    try:
        run()
    finally:
        eo._UseRewardGuard = real_cls
    return calls


@requires_linux_owner
def test_authorizer_measurement_one_real_fill_touches_exactly_the_measured_tuples_and_fires_no_trigger(owner, monkeypatch):
    monkeypatch.setattr(owner, "_fill_vec_ready", lambda conn: None)
    calls = _record_authorizer_calls(owner, lambda: ok_result(fill(owner, [("e001", vec(0)), ("gone", vec(1)), ("e002", vec(2))])))
    assert calls, "the recording wrapper saw nothing: the guard class is not looked up at call time"
    assert set(calls) == FILL_MEASURED_TUPLES, set(calls) ^ FILL_MEASURED_TUPLES
    assert not [c for c in calls if c[4] is not None]  # no trigger or view fired: the admitted set is empty
    for c in calls:
        assert eo._fill_embeddings_allows(*c) is True, c


def test_fill_allowlist_admits_exactly_what_it_needs_and_nothing_else():
    A = eo._fill_embeddings_allows
    for t in FILL_MEASURED_TUPLES:
        assert A(*t) is True, t
    sp = eo.FILL_EMBEDDINGS_VEC_SAVEPOINT
    assert sp == "engram_fill_vec"
    ok = [
        (sqlite3.SQLITE_SAVEPOINT, "BEGIN", sp, None, None),
        (sqlite3.SQLITE_SAVEPOINT, "RELEASE", sp, None, None),
        (sqlite3.SQLITE_SAVEPOINT, "ROLLBACK", sp, None, None),
        (sqlite3.SQLITE_INSERT, "vec_nodes", None, "main", None),
        (sqlite3.SQLITE_DELETE, "vec_nodes", None, "main", None),
        (sqlite3.SQLITE_READ, "vec_nodes", "node_id", "main", None),
        (sqlite3.SQLITE_INSERT, "vec_nodes_chunks", None, "main", None),
        (sqlite3.SQLITE_UPDATE, "vec_nodes_chunks", "validity", "main", None),
        (sqlite3.SQLITE_READ, "vec_nodes_rowids", "id", "main", None),
        (sqlite3.SQLITE_DELETE, "vec_nodes_vector_chunks00", None, "main", None),
    ]
    for t in ok:
        assert A(*t) is True, t
    D = sqlite3
    denied = [
        (D.SQLITE_UPDATE, "nodes", "claim", "main", None),
        (D.SQLITE_UPDATE, "nodes", "utility_score", "main", None),
        (D.SQLITE_UPDATE, "nodes", "embedding", "temp", None),
        (D.SQLITE_UPDATE, "nodes", "embedding", "main", "nodes_fts_rewrite_resync"),  # a trigger: none is admitted
        (D.SQLITE_READ, "nodes", "claim", "main", None),
        (D.SQLITE_READ, "nodes", "id", "temp", None),
        (D.SQLITE_INSERT, "nodes", None, "main", None),
        (D.SQLITE_DELETE, "nodes", None, "main", None),
        (D.SQLITE_INSERT, "edges", None, "main", None),
        (D.SQLITE_INSERT, "vec_nodes", None, "temp", None),
        (D.SQLITE_INSERT, "vec_nodes_evil_extra", None, "main", None),  # only the vec0 shadow-table NAMES are admitted
        (D.SQLITE_PRAGMA, "journal_mode", "wal", "main", None),
        (D.SQLITE_ATTACH, "/tmp/x", None, None, None),
        (D.SQLITE_CREATE_TABLE, "t", None, "main", None),
        (D.SQLITE_DROP_TABLE, "t", None, "main", None),
        (D.SQLITE_FUNCTION, None, "load_extension", None, None),
        (D.SQLITE_SAVEPOINT, "BEGIN", "other", None, None),
        (D.SQLITE_TRANSACTION, "SOMETHING", None, None, None),
        (D.SQLITE_RECURSIVE, None, None, None, None),
    ]
    for t in denied:
        assert A(*t) is False, t


def test_trigger_inventory_on_nodes_is_the_pinned_seven_and_none_is_admitted_by_either_op(home):
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    try:
        live = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='nodes'")}
    finally:
        conn.close()
    assert live == set(eo.USE_REWARD_NODES_TRIGGERS_PINNED), live ^ set(eo.USE_REWARD_NODES_TRIGGERS_PINNED)
    assert eo.FILL_EMBEDDINGS_ADMITTED_TRIGGERS <= eo.USE_REWARD_NODES_TRIGGERS_PINNED
    assert eo.FINISH_EMBEDDING_REGEN_ADMITTED_TRIGGERS <= eo.USE_REWARD_NODES_TRIGGERS_PINNED


@requires_linux_owner
def test_the_pinned_trigger_set_does_fire_on_columns_it_names_so_the_empty_admitted_set_is_a_real_measurement(home):
    """Positive control for 'no trigger fires on UPDATE nodes SET embedding': the same recorder DOES see a trigger
    fire when the statement touches a column a trigger is declared on."""
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    calls = []
    conn.set_authorizer(lambda *a: (calls.append(tuple(a)), sqlite3.SQLITE_OK)[1])
    conn.execute("UPDATE nodes SET claim = 'changed' WHERE id = 'e001'")
    conn.rollback()
    conn.execute("UPDATE nodes SET embedding = '[1]' WHERE id = 'e001'")
    conn.rollback()
    conn.close()
    claim_phase = [c for c in calls if c[4] is not None]
    assert claim_phase, "positive control: a claim update must fire a trigger on the real schema"
    emb_calls = calls[calls.index(next(c for c in calls if c == (sqlite3.SQLITE_UPDATE, "nodes", "embedding", "main", None))):]
    assert not [c for c in emb_calls if c[4] is not None]


@requires_linux_owner
def test_a_denied_statement_in_the_fill_refuses_the_whole_op_rolled_back(owner, monkeypatch):
    real = eo._fill_embeddings_allows
    monkeypatch.setattr(eo, "_fill_embeddings_allows",
                        lambda a, b, c, d, e: False if (a == sqlite3.SQLITE_UPDATE and b == "nodes") else real(a, b, c, d, e))
    before = all_rows(owner.store_path)
    res = fill(owner, items_for(["e001", "e002"]))
    assert_rejected(res, "write_denied")
    assert all_rows(owner.store_path) == before
    assert owner._owner_conn.in_transaction is False


@requires_linux_owner
def test_the_authorizer_and_busy_timeout_are_restored_after_ok_denied_and_raised_exits(owner, monkeypatch):
    conn = owner._owner_conn
    before = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    ok_result(fill(owner, items_for(["e001"])))
    monkeypatch.setattr(eo, "_fill_embeddings_allows", lambda *a: False)
    assert_rejected(fill(owner, items_for(["e002"])), "write_denied")
    monkeypatch.undo()

    def boom(c, ids):
        raise RuntimeError("injected")

    monkeypatch.setattr(owner, "_fill_embeddings_readback", boom)
    res = fill(owner, items_for(["e003"]))
    assert_rejected(res, "readback_mismatch")
    monkeypatch.undo()
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == before
    assert conn.in_transaction is False
    ok_result(fill(owner, items_for(["e004"])))  # the connection is still usable


# --- busy ---------------------------------------------------------------------------------------------


@requires_linux_owner
def test_an_owner_lock_held_past_the_budget_is_busy_nothing_written_and_not_recorded(owner, monkeypatch):
    monkeypatch.setattr(eo, "OWNER_WRITE_LOCK_WAIT", 0.3)
    rid = fresh_rid()
    before = all_rows(owner.store_path)
    owner._owner_lock.acquire()
    try:
        res = fill(owner, items_for(["e001"]), request_id=rid)
    finally:
        owner._owner_lock.release()
    assert_rejected(res, "busy")
    assert all_rows(owner.store_path) == before
    ok = ok_result(fill(owner, items_for(["e001"]), request_id=rid))  # a retry with the SAME id runs fresh
    assert ok["filled"] == 1 and "duplicate" not in ok


@requires_linux_owner
def test_a_store_locked_by_a_direct_writer_is_busy_rolled_back(owner, monkeypatch):
    monkeypatch.setattr(eo, "OWNER_WRITE_LOCK_WAIT", 0.3)
    holder = sqlite3.connect(str(owner.store_path), timeout=5.0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        before = all_rows(owner.store_path)
        res = fill(owner, items_for(["e001"]))
    finally:
        holder.rollback()
        holder.close()
    assert_rejected(res, "busy")
    assert all_rows(owner.store_path) == before


# --- (7) concurrency: a use_reward issued while a 50-item fill is in flight completes Ok --------------


@requires_linux_owner
def test_a_use_reward_issued_while_a_fifty_item_fill_is_in_flight_completes_ok_not_busy(big_owner, monkeypatch):
    """Deterministic overlap: a hook inside the fill (while it holds the owner lock, before its transaction)
    signals 'in flight' and then holds for 0.3 s. The use_reward is issued the moment the signal arrives, so it is
    guaranteed to overlap, and must wait for the lock and complete Ok inside its 1.0 s budget."""
    o = big_owner
    conn = sqlite3.connect(str(o.store_path))
    null_ids = [r[0] for r in conn.execute("SELECT id FROM nodes WHERE embedding IS NULL ORDER BY id LIMIT 50")]
    conn.close()
    assert len(null_ids) == 50
    in_flight, release = threading.Event(), 0.3
    real_ready = o._fill_vec_ready

    def hooked(c):
        in_flight.set()
        time.sleep(release)
        return real_ready(c)

    monkeypatch.setattr(o, "_fill_vec_ready", hooked)
    fill_out = []
    t = threading.Thread(target=lambda: fill_out.append(fill(o, items_for(null_ids))))
    t.start()
    assert in_flight.wait(5.0)
    t0 = time.monotonic()
    res = ec.write("use_reward", {"node_ids": ["b0001"], "action": "mention", "request_id": "ur-1"},
                   request_id="ur-1", timeout=eo.OWNER_WRITE_LOCK_WAIT + 1.0, socket_path=o.socket_path)
    waited = time.monotonic() - t0
    t.join()
    assert isinstance(res, ec.Ok), res
    assert waited >= 0.2, f"the use_reward did not overlap the fill (waited {waited:.3f}s)"
    assert ok_result(fill_out[0])["filled"] == 50


@requires_linux_owner
def test_a_use_reward_storm_during_a_real_fifty_item_fill_is_never_busy_and_hold_time_is_reported(big_owner, capsys):
    o = big_owner
    conn = sqlite3.connect(str(o.store_path))
    null_ids = [r[0] for r in conn.execute("SELECT id FROM nodes WHERE embedding IS NULL ORDER BY id LIMIT 50")]
    conn.close()
    holds = []
    real_exec = o._fill_embeddings_execute

    def timed(*a, **kw):
        t0 = time.monotonic()
        try:
            return real_exec(*a, **kw)
        finally:
            holds.append(time.monotonic() - t0)

    o._fill_embeddings_execute = timed
    stop = threading.Event()
    results = []

    def storm():
        i = 0
        while not stop.is_set():
            i += 1
            results.append(ec.write("use_reward", {"node_ids": ["b0002"], "action": "mention", "request_id": f"st-{i}"},
                                    request_id=f"st-{i}", timeout=3.0, socket_path=o.socket_path))
            time.sleep(0.002)

    th = threading.Thread(target=storm)
    th.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    res = fill(o, items_for(null_ids))
    wall = time.monotonic() - t0
    stop.set()
    th.join()
    assert ok_result(res)["filled"] == 50
    assert results and all(isinstance(r, ec.Ok) for r in results), [r for r in results if not isinstance(r, ec.Ok)][:3]
    with capsys.disabled():
        print(f"\n[3879 hold time] 50-item fill on the {len(all_rows(o.store_path))}-node fixture: owner-side hold "
              f"{holds[0] * 1000:.1f} ms (client wall {wall * 1000:.1f} ms; {len(results)} concurrent use_rewards, none busy); "
              f"OWNER_WRITE_LOCK_WAIT={eo.OWNER_WRITE_LOCK_WAIT}s")
    assert holds


# --- finish_embedding_regen ---------------------------------------------------------------------------


def _finish_bad_cases():
    good = {"processed": 3, "embedded": 2, "skipped": 1, "failed": 0, "embedder_available": True}

    def w(**kw):
        p = dict(good)
        p.update(kw)
        return p

    def wo(k):
        p = dict(good)
        del p[k]
        return p

    return [
        ("not_a_dict", [1, 2, 3, 4], "params_shape"),
        ("extra_key", w(request_id="r"), "params_shape"),
        ("extra_sql", w(sql="DROP TABLE nodes"), "params_shape"),
        ("missing_processed", wo("processed"), "params_shape"),
        ("missing_failed", wo("failed"), "params_shape"),
        ("bool_count", w(embedded=True), "count_not_int"),
        ("float_count", w(embedded=2.0), "count_not_int"),
        ("str_count", w(skipped="1"), "count_not_int"),
        ("none_count", w(failed=None), "count_not_int"),
        ("negative_count", w(failed=-1), "count_negative"),
        # spec C item 5 (#3879): `embedder_available` is REQUIRED and a real bool, never an int
        ("missing_embedder_available", wo("embedder_available"), "params_shape"),
        ("int_embedder_available_1", w(embedder_available=1), "embedder_available_not_bool"),
        ("int_embedder_available_0", w(embedder_available=0), "embedder_available_not_bool"),
        ("none_embedder_available", w(embedder_available=None), "embedder_available_not_bool"),
        ("str_embedder_available", w(embedder_available="true"), "embedder_available_not_bool"),
    ]


@requires_linux_owner
@pytest.mark.parametrize("label,params,token", _finish_bad_cases(), ids=[c[0] for c in _finish_bad_cases()])
def test_finish_param_refusals_are_typed_and_touch_nothing(owner, label, params, token):
    before = all_rows(owner.store_path)
    stmts = tracing(owner)
    res = finish(owner, params=params)
    assert_rejected(res, "invalid_params", token)
    assert stmts == [] and owner._owner_conn.in_transaction is False
    assert all_rows(owner.store_path) == before


def _indexable_count(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM nodes WHERE status IS NOT 'retracted' AND is_current IS NOT 0").fetchone()[0]
    finally:
        conn.close()


def _docsize_ids(db: Path) -> list:
    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute("SELECT id FROM nodes_fts_docsize ORDER BY id")]
    finally:
        conn.close()


@requires_linux_owner
def test_finish_rebuilds_the_fts_index_and_reads_back_the_row_count(owner):
    r = ok_result(finish(owner, 0, 0, 0, 0))
    assert set(r) == {"fts_rows", "restore_state"}
    assert r["fts_rows"] == _indexable_count(owner.store_path) > 0
    assert len(_docsize_ids(owner.store_path)) == r["fts_rows"]  # retracted and superseded rows are out of the index


@requires_linux_owner
def test_finish_fts_is_the_same_as_the_direct_rebuild_on_a_twin_store(twins):
    ha, hb = twins
    dba, dbb = make_store(ha), make_store(hb)
    conn = sqlite3.connect(str(dba))
    uv_before = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    o = _start(ha)
    try:
        ok_result(finish(o, 0, 0, 0, 0))
    finally:
        o.stop()
    assert engram_backup.rebuild_fts_index(str(dbb)) is True
    assert _docsize_ids(dba) == _docsize_ids(dbb)
    for db in (dba, dbb):
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT COUNT(*) FROM nodes_fts WHERE nodes_fts MATCH 'claim'").fetchone()[0] > 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == uv_before
        conn.close()


@requires_linux_owner
def test_finish_recreates_a_missing_nodes_fts_the_restore_case(home):
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE nodes_fts")  # the SQL dump strips it
    conn.commit()
    conn.close()
    o = _start(home)
    try:
        r = ok_result(finish(o, 0, 0, 0, 0))
    finally:
        o.stop()
    assert r["fts_rows"] == _indexable_count(db) == len(_docsize_ids(db))


@requires_linux_owner
def test_finish_repairs_a_stale_under_indexed_nodes_fts(home):
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE nodes_fts")
    conn.execute("CREATE VIRTUAL TABLE nodes_fts USING fts5(claim, content='nodes', content_rowid='rowid')")
    conn.commit()
    conn.close()
    o = _start(home)
    try:
        r = ok_result(finish(o, 0, 0, 0, 0))
    finally:
        o.stop()
    conn = sqlite3.connect(str(db))
    cols = [x[1] for x in conn.execute("PRAGMA table_info(nodes_fts)")]
    conn.close()
    assert cols == list(engram_backup.NODES_FTS_COLUMNS)
    assert r["fts_rows"] == _indexable_count(db)


def _set_flag(db: Path, *, cause: str, null_count: int = 3, age_seconds: int = 0):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    engram_core._set_embedding_restore_flag(conn, null_count=null_count, cause=cause)
    if age_seconds:
        conn.execute("UPDATE embedding_restore_state SET updated_at = ?", (engram_core._iso_seconds_ago(age_seconds),))
        conn.commit()
    conn.close()


def _state_without_time(row):
    return None if row in (None, "no-table") else tuple(row[:3])


@requires_linux_owner
def test_finish_processed_zero_clears_a_stale_flag_only_when_the_fresh_recount_is_zero(home):
    """The owner never trusts the client's `processed` for the branch: it always takes the recount path, which clears
    when no embeddable NULL remains. (Replaces `..._processed_zero_clears_a_stale_flag`, which ran on a store holding
    13 embeddable NULL nodes and pinned the client's claim deciding the clear.)"""
    db = make_store(home, n=4)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE nodes SET embedding = '[0.5]' WHERE embedding IS NULL AND is_current = 1 AND id <> 'ev_blank'")
    conn.commit()
    conn.close()
    _set_flag(db, cause="per-node-failures")
    o = _start(home)
    try:
        r = ok_result(finish(o, 0, 0, 0, 0))
    finally:
        o.stop()
    assert r["restore_state"] is None
    assert _state_without_time(restore_row(db)) == (0, 0, None)


@requires_linux_owner
@pytest.mark.parametrize("available,cause", [(True, "per-node-failures"), (False, "embedder-unavailable")])
def test_finish_processed_zero_over_embeddable_nulls_sets_the_flag_it_does_not_clear(home, available, cause):
    """A stale or lying `processed == 0` can no longer clear the flag over real NULLs: 13 embeddable NULL current nodes
    remain (e000..e011 and `ret`), so the recount sets it, with the cause taken from the tool's `embedder_available`."""
    db = make_store(home)
    _set_flag(db, cause="per-node-failures", null_count=0)
    o = _start(home)
    try:
        r = ok_result(finish(o, 0, 0, 0, 0, embedder_available=available))
    finally:
        o.stop()
    row = restore_row(db)
    assert row[0] == 1 and row[1] == 13 and row[2] == cause, row
    assert r["restore_state"] == cause


def test_the_finish_op_does_not_branch_on_processed():
    import inspect as _inspect
    src = _inspect.getsource(eo.EngramdOwner._finish_embedding_regen_execute)
    assert "branch_clear" not in src and '["processed"] == 0' not in src
    assert "_clear_embedding_restore_flag(conn" not in src  # only the recounting finalize is called


@requires_linux_owner
def test_finish_finalize_branch_with_nothing_missing_clears_the_flag(home):
    """processed > 0 and every embeddable NULL now embedded: finalize's fresh recount clears the flag."""
    db = make_store(home, n=4)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE nodes SET embedding = '[0.5]' WHERE embedding IS NULL AND is_current = 1 AND id <> 'ev_blank'")
    conn.commit()
    conn.close()
    _set_flag(db, cause="per-node-failures")
    o = _start(home)
    try:
        r = ok_result(finish(o, 4, 4, 0, 0))
    finally:
        o.stop()
    assert r["restore_state"] is None
    assert _state_without_time(restore_row(db)) == (0, 0, None)


@requires_linux_owner
@pytest.mark.parametrize("available,cause", [(True, "per-node-failures"), (False, "embedder-unavailable")])
def test_finish_finalize_branch_with_embeddable_nodes_still_missing_sets_the_flag_like_the_tool_does(twins, monkeypatch, available, cause):
    """Differential against the DIRECT finalize on a twin store (same core function, same cause derivation)."""
    monkeypatch.setattr(engram_core._embedder, "is_available", lambda: available)
    ha, hb = twins
    dba, dbb = make_store(ha), make_store(hb)
    counts = {"processed": 10, "embedded": 3, "skipped": 1, "failed": 6}
    o = _start(ha)
    try:
        r = ok_result(finish(o, **counts, embedder_available=available))
    finally:
        o.stop()
    conn = sqlite3.connect(str(dbb))
    engram_core._finalize_embedding_restore_flag(conn, counts, respect_lease=True)  # the direct path probes (patched above)
    conn.close()
    assert _state_without_time(restore_row(dba)) == _state_without_time(restore_row(dbb))
    row = restore_row(dba)
    # 13 embeddable NULL current nodes remain: e000..e011 and the retracted-but-current `ret` (ev_blank has no text; `gone` is not current)
    assert row[0] == 1 and row[2] == cause and row[1] == 13
    assert r["restore_state"] == cause


@requires_linux_owner
@pytest.mark.parametrize("branch", [(0, 0, 0, 0), (10, 3, 1, 6)], ids=["clear_branch", "finalize_branch"])
def test_lease_a_fresh_foreign_regeneration_running_lease_survives_finish_exactly_as_the_direct_call(twins, branch, monkeypatch):
    monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)  # no real sentence_transformers import in a unit cell
    ha, hb = twins
    dba, dbb = make_store(ha), make_store(hb)
    for db in (dba, dbb):
        _set_flag(db, cause="regeneration-running", null_count=9)
    counts = dict(zip(("processed", "embedded", "skipped", "failed"), branch))
    o = _start(ha)
    try:
        r = ok_result(finish(o, **counts))
    finally:
        o.stop()
    conn = sqlite3.connect(str(dbb))
    if counts["processed"] == 0:
        assert engram_core._clear_embedding_restore_flag(conn, respect_lease=True) is False
    else:
        engram_core._finalize_embedding_restore_flag(conn, counts, respect_lease=True)
    conn.close()
    ra, rb = restore_row(dba), restore_row(dbb)
    # positive control: the lease IS in place and the DIRECT call left it standing; the owner left it identically
    assert rb[2] == "regeneration-running"
    assert _state_without_time(ra) == _state_without_time(rb) == (1, 9, "regeneration-running")
    assert r["restore_state"] == "regeneration-running"


@requires_linux_owner
def test_lease_a_stale_lease_is_cleared_by_finish_like_the_direct_call(twins):
    ha, hb = twins
    dba, dbb = make_store(ha), make_store(hb)
    for db in (dba, dbb):
        embed_all_embeddable(db)  # the owner recounts: with nothing missing the stale lease is cleared, as the direct call does
        _set_flag(db, cause="regeneration-running", null_count=9, age_seconds=11 * 60)
    o = _start(ha)
    try:
        ok_result(finish(o, 0, 0, 0, 0))
    finally:
        o.stop()
    conn = sqlite3.connect(str(dbb))
    assert engram_core._clear_embedding_restore_flag(conn, respect_lease=True) is True
    conn.close()
    assert _state_without_time(restore_row(dba)) == _state_without_time(restore_row(dbb)) == (0, 0, None)


@requires_linux_owner
def test_finish_creates_the_restore_state_table_when_absent_like_the_core_function_does(home):
    db = make_store(home)
    embed_all_embeddable(db)  # nothing missing: the recount clears
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE IF EXISTS embedding_restore_state")
    conn.commit()
    conn.close()
    o = _start(home)
    try:
        r = ok_result(finish(o, 0, 0, 0, 0))
    finally:
        o.stop()
    assert r["restore_state"] is None
    assert _state_without_time(restore_row(db)) == (0, 0, None)


FINISH_MEASURED_NAMES = {
    "nodes", "embedding_restore_state", "sqlite_master", "nodes_fts", "nodes_fts_config", "nodes_fts_data", "nodes_fts_docsize",
    "nodes_fts_idx",
}


@requires_linux_owner
@pytest.mark.parametrize("branch", [(0, 0, 0, 0), (10, 3, 1, 6)], ids=["clear_branch", "finalize_branch"])
def test_authorizer_measurement_one_real_finish_is_allowed_end_to_end_and_fires_no_trigger(owner, branch, monkeypatch):
    monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)  # no real sentence_transformers import in a unit cell
    calls = _record_authorizer_calls(owner, lambda: ok_result(finish(owner, *branch)))
    assert calls
    assert not [c for c in calls if c[4] is not None]  # no trigger or view fired: the admitted set is empty
    for c in calls:
        assert eo._finish_embedding_regen_allows(*c) is True, c
    touched = {c[1] for c in calls if c[0] in (sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)}
    assert touched <= FINISH_MEASURED_NAMES, touched - FINISH_MEASURED_NAMES
    # the op never UPDATEs nodes
    assert not [c for c in calls if c[0] == sqlite3.SQLITE_UPDATE and c[1] == "nodes"]


def test_finish_allowlist_denies_everything_outside_the_measured_set():
    A = eo._finish_embedding_regen_allows
    D = sqlite3
    ok = [
        (D.SQLITE_SELECT, None, None, None, None),
        (D.SQLITE_READ, "nodes", "claim", "main", None),
        (D.SQLITE_READ, "embedding_restore_state", "cause", "main", None),
        (D.SQLITE_READ, "nodes_fts_docsize", "id", "main", None),
        (D.SQLITE_INSERT, "nodes_fts", None, "main", None),
        (D.SQLITE_INSERT, "embedding_restore_state", None, "main", None),
        (D.SQLITE_UPDATE, "embedding_restore_state", "cause", "main", None),
        (D.SQLITE_DELETE, "nodes_fts_data", None, "main", None),
        (D.SQLITE_PRAGMA, "table_info", "nodes", None, None),
        (D.SQLITE_PRAGMA, "user_version", "8", None, None),
        (D.SQLITE_PRAGMA, "user_version", None, None, None),
        (D.SQLITE_FUNCTION, None, "coalesce", None, None),
        (D.SQLITE_FUNCTION, None, "count", None, None),
        (D.SQLITE_TRANSACTION, "COMMIT", None, None, None),
        (D.SQLITE_CREATE_TABLE, "embedding_restore_state", None, "main", None),
        (D.SQLITE_CREATE_VTABLE, "nodes_fts", "fts5", "main", None),
        (D.SQLITE_DROP_VTABLE, "nodes_fts", "fts5", "main", None),
    ]
    for t in ok:
        assert A(*t) is True, t
    denied = [
        (D.SQLITE_UPDATE, "nodes", "embedding", "main", None),
        (D.SQLITE_UPDATE, "nodes", "claim", "main", None),
        (D.SQLITE_INSERT, "nodes", None, "main", None),
        (D.SQLITE_DELETE, "nodes", None, "main", None),
        (D.SQLITE_INSERT, "edges", None, "main", None),
        (D.SQLITE_DELETE, "edges", None, "main", None),
        (D.SQLITE_DROP_TABLE, "nodes", None, "main", None),
        (D.SQLITE_DROP_TABLE, "edges", None, "main", None),
        (D.SQLITE_CREATE_TABLE, "evil", None, "main", None),
        (D.SQLITE_READ, "edges", "source_id", "main", None),
        (D.SQLITE_READ, "nodes", "claim", "temp", None),
        (D.SQLITE_PRAGMA, "journal_mode", "wal", "main", None),
        (D.SQLITE_PRAGMA, "table_info", "edges", None, None),
        (D.SQLITE_ATTACH, "/tmp/x", None, None, None),
        (D.SQLITE_FUNCTION, None, "load_extension", None, None),
        (D.SQLITE_SAVEPOINT, "BEGIN", "x", None, None),
        (D.SQLITE_READ, "nodes_fts", "claim", "main", "nodes_fts_rewrite_resync"),  # a trigger: none is admitted
    ]
    for t in denied:
        assert A(*t) is False, t


@requires_linux_owner
def test_finish_readback_mismatch_is_committed_unverified(owner):
    real = owner._finish_embedding_regen_readback
    owner._finish_embedding_regen_readback = lambda conn: {**real(conn), "fts_rows": real(conn)["fts_rows"] + 1}
    res = finish(owner, 0, 0, 0, 0)
    assert_rejected(res, "readback_mismatch")
    assert res.commit == "committed_unverified"
    assert _docsize_ids(owner.store_path)  # the rebuild stands


@requires_linux_owner
def test_finish_restore_state_that_is_not_what_the_branch_wrote_is_readback_mismatch(owner):
    _set_flag(owner.store_path, cause="per-node-failures")
    real = owner._finish_embedding_regen_readback

    def perturbing(conn):
        out = dict(real(conn))
        out["state"] = (1, 7, "not-a-real-cause", out["state"][3] if out["state"] else "x")
        return out

    owner._finish_embedding_regen_readback = perturbing
    res = finish(owner, 0, 0, 0, 0)
    assert_rejected(res, "readback_mismatch")
    assert res.commit == "committed_unverified"


@requires_linux_owner
def test_a_failure_inside_the_flag_write_rolls_the_whole_finish_back_including_the_fts_rebuild(home, monkeypatch):
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE nodes_fts")
    conn.commit()
    conn.close()
    o = _start(home)
    try:
        def boom(*a, **kw):
            raise sqlite3.OperationalError("injected flag failure")

        monkeypatch.setattr(engram_core, "_finalize_embedding_restore_flag", boom)
        res = finish(o, 0, 0, 0, 0)
        assert isinstance(res, ec.Rejected) and res.reason == "finish_embedding_regen_write_failed", res
        probe = sqlite3.connect(str(db))
        has = probe.execute("SELECT 1 FROM sqlite_master WHERE name = 'nodes_fts'").fetchone()
        probe.close()
        assert has is None  # the rebuild (a CREATE) was rolled back with it
        assert o._owner_conn.in_transaction is False
    finally:
        o.stop()


@requires_linux_owner
def test_finish_while_the_lock_is_held_past_the_budget_is_busy(owner, monkeypatch):
    monkeypatch.setattr(eo, "OWNER_WRITE_LOCK_WAIT", 0.3)
    owner._owner_lock.acquire()
    try:
        res = finish(owner, 0, 0, 0, 0)
    finally:
        owner._owner_lock.release()
    assert_rejected(res, "busy")


# --- hold times (REPORTED, not gated) -----------------------------------------------------------------


@requires_linux_owner
def test_report_owner_side_hold_time_of_finish_and_of_rebuild_fts_index_against_the_lock_budget(big_owner, capsys):
    o = big_owner
    holds = []
    real = o._finish_embedding_regen_execute

    def timed(*a, **kw):
        t0 = time.monotonic()
        try:
            return real(*a, **kw)
        finally:
            holds.append(time.monotonic() - t0)

    o._finish_embedding_regen_execute = timed
    for _ in range(3):
        ok_result(finish(o, 0, 0, 0, 0))
    n = len(all_rows(o.store_path))
    with capsys.disabled():
        print(f"\n[3879 hold time] finish_embedding_regen on the {n}-node fixture: owner-side hold "
              + ", ".join(f"{h * 1000:.1f} ms" for h in holds) + f" (OWNER_WRITE_LOCK_WAIT={eo.OWNER_WRITE_LOCK_WAIT}s, "
              f"client write timeout {ec.OWNER_WRITE_CLIENT_TIMEOUT}s)")
    assert len(holds) == 3


# --- the wire: the largest legal request fits the owner's request cap ---------------------------------


def test_the_largest_legal_fill_request_fits_the_wire_cap():
    worst = [-1.2345678901234567e-05 * (1 + k * 1e-17) for k in range(DIM)]
    msg = {"contract_version": ec.CONTRACT_VERSION, "method": "fill_embeddings", "request_id": "r" * 36,
           "params": {"model": MODEL, "items": [{"node_id": "n" * 256, "vector": worst} for _ in range(50)]}}
    size = len(json.dumps(msg).encode("utf-8"))
    assert size < eo.MAX_REQUEST_BYTES, size


# --- two defence layers, and the embedder probe's placement -------------------------------------------


@requires_linux_owner
def test_the_shipped_client_refuses_to_serialise_nan_so_the_owners_own_check_is_the_second_layer(owner):
    """The control for the raw-socket NaN / Infinity cells: through `engramd_client.write` the value never leaves the
    client (`request_not_serializable`), so only a non-ec client reaches the owner's `non_finite` refusal."""
    v = vec(1)
    v[3] = float("nan")
    res = fill(owner, params={"model": MODEL, "items": [{"node_id": "e001", "vector": v}]})
    assert isinstance(res, ec.Rejected) and res.reason == "request_not_serializable", res
    assert embeddings(owner.store_path)["e001"] is None


def _probe_that_must_not_run():
    raise AssertionError("the owner called _embedder.is_available(): the probe belongs to the tool's process (#3879)")


@requires_linux_owner
@pytest.mark.parametrize("available,cause", [(False, "embedder-unavailable"), (True, "per-node-failures")])
def test_finish_takes_the_flag_cause_from_the_embedder_available_param(owner, monkeypatch, available, cause):
    """Spec C item 5(a). With leftovers, the cause is what the CALLER says its embedder was. The owner's own module
    reports the OPPOSITE, so a mutation that probes in the owner (or ignores the param) lands on the wrong cause."""
    monkeypatch.setattr(engram_core._embedder, "is_available", lambda: not available)
    r = ok_result(finish(owner, 10, 3, 1, 6, embedder_available=available))
    assert r["restore_state"] == cause
    assert restore_row(owner.store_path)[2] == cause


@requires_linux_owner
@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("branch", [(0, 0, 0, 0), (10, 3, 1, 6), (10, 10, 0, 0)], ids=["clear", "finalize_leftovers", "finalize_clean"])
def test_finish_never_calls_the_embedder_probe_nor_imports_sentence_transformers_in_the_owner(owner, monkeypatch, available, branch):
    """Spec C item 5(b), the spy. `_embedder.is_available` RAISES in the owner's module; finish must still succeed on
    every branch. Mutation: the owner ignores the param and probes (or warms the probe before the lock) -> red.
    Also: the owner process must not have imported sentence_transformers (torch, ~4 s resident) because of finish."""
    had_st = "sentence_transformers" in sys.modules
    monkeypatch.setattr(engram_core._embedder, "is_available", _probe_that_must_not_run)
    ok_result(finish(owner, *branch, embedder_available=available))
    if not had_st:
        assert "sentence_transformers" not in sys.modules


def test_the_direct_path_still_probes_when_no_embedder_available_is_given_and_never_when_one_is(twins, monkeypatch):
    """Spec C item 5(c). `_finalize_embedding_restore_flag(..., embedder_available=None)` is today's behaviour byte for
    byte (it probes `_embedder`); a bool is used as given and `_embedder` is not touched."""
    ha, hb = twins
    dba, dbb = make_store(ha), make_store(hb)
    counts = {"processed": 10, "embedded": 3, "skipped": 1, "failed": 6}
    calls = []
    monkeypatch.setattr(engram_core._embedder, "is_available", lambda: calls.append(1) or False)
    conn = sqlite3.connect(str(dba))
    engram_core._finalize_embedding_restore_flag(conn, counts, respect_lease=True)  # default: probes
    conn.close()
    assert calls == [1] and restore_row(dba)[2] == "embedder-unavailable"
    monkeypatch.setattr(engram_core._embedder, "is_available", _probe_that_must_not_run)
    conn = sqlite3.connect(str(dbb))
    engram_core._finalize_embedding_restore_flag(conn, counts, respect_lease=True, embedder_available=True)
    conn.close()
    assert restore_row(dbb)[2] == "per-node-failures"
    sig = inspect.signature(engram_core._finalize_embedding_restore_flag).parameters["embedder_available"]
    assert sig.default is None and sig.kind is inspect.Parameter.KEYWORD_ONLY


# --- U16-E: the vec mirror's phase-scoped admissions (#3879, Borges's Linux census on PR #3900) ----------
#
# SQLite 3.53.0 / sqlite_vec 0.1.9: on a real vec0 the mirror sequence (SAVEPOINT, DELETE, INSERT, ROLLBACK TO, RELEASE)
# makes exactly two authorizer calls outside the vec_nodes name family: SQLITE_READ on table `x` column `node_id` (vec0's
# constructor declares its schema under the name `x`) and SQLITE_FUNCTION `max`. Both are admitted ONLY while the guard's
# `vec_phase` is open. The pure `_fill_embeddings_allows` never admits them.

X_NODE_ID_READ = (sqlite3.SQLITE_READ, "x", "node_id", "main", None)
MAX_FUNCTION = (sqlite3.SQLITE_FUNCTION, None, "max", None, None)


def _fill_guard(vec_phase: bool):
    g = eo._UseRewardGuard(allows=eo._fill_embeddings_allows, vec_allows=eo._fill_vec_phase_allows)
    g.vec_phase = vec_phase
    return g


def test_vec_phase_admits_x_node_id_read_and_max_only_inside_the_phase():
    for t in (X_NODE_ID_READ, MAX_FUNCTION):
        g = _fill_guard(True)
        assert g.authorizer(*t) == sqlite3.SQLITE_OK, t
        assert g.denials == []
        g = _fill_guard(False)
        assert g.authorizer(*t) == sqlite3.SQLITE_DENY, t
        assert g.denials == [t]
        assert eo._fill_embeddings_allows(*t) is False, t  # the phase-free allowlist alone never carries them


def test_vec_phase_does_not_admit_other_columns_of_x_or_other_functions_or_other_tables():
    D = sqlite3
    never = [
        (D.SQLITE_READ, "x", "embedding", "main", None),  # not in the census
        (D.SQLITE_READ, "x", "rowid", "main", None),
        (D.SQLITE_READ, "x", "node_id", "temp", None),
        (D.SQLITE_READ, "y", "node_id", "main", None),
        (D.SQLITE_READ, "nodes", "claim", "main", None),
        (D.SQLITE_UPDATE, "x", "node_id", "main", None),
        (D.SQLITE_INSERT, "x", None, "main", None),
        (D.SQLITE_DELETE, "x", None, "main", None),
        (D.SQLITE_READ, "x", "node_id", "main", "nodes_fts_rewrite_resync"),  # a trigger-originated statement
        (D.SQLITE_FUNCTION, None, "load_extension", None, None),
        (D.SQLITE_FUNCTION, None, "min", None, None),
        (D.SQLITE_FUNCTION, None, "json_set", None, None),
        (D.SQLITE_FUNCTION, None, "MAX", None, None),
        (D.SQLITE_FUNCTION, "max", None, None, None),
        (D.SQLITE_PRAGMA, "journal_mode", "wal", "main", None),
        (D.SQLITE_ATTACH, "/tmp/x", None, None, None),
        (D.SQLITE_CREATE_TABLE, "x", None, "main", None),
    ]
    for t in never:
        for phase in (True, False):
            g = _fill_guard(phase)
            assert g.authorizer(*t) == sqlite3.SQLITE_DENY, (phase, t)


def test_a_guard_without_a_vec_predicate_never_admits_them_even_in_a_phase():
    g = eo._UseRewardGuard(allows=eo._fill_embeddings_allows)
    g.vec_phase = True
    assert g.authorizer(*X_NODE_ID_READ) == sqlite3.SQLITE_DENY
    assert g.authorizer(*MAX_FUNCTION) == sqlite3.SQLITE_DENY


def test_the_vec_phase_is_open_only_around_the_mirror_and_closed_on_success_and_on_failure():
    seen: list = []

    class Conn:
        def __init__(self, fail):
            self.fail = fail

        def execute(self, sql, *a):
            seen.append((sql.split()[0], g.vec_phase))
            if self.fail and sql.startswith("INSERT"):
                raise sqlite3.OperationalError("vec0 constructor error")

    for fail in (False, True):
        seen.clear()
        g = _fill_guard(False)
        out = eo.EngramdOwner._fill_vec_mirror(None, Conn(fail), g, [("n1", "[0.5]", [0.5])], lambda v: b"x")
        assert out is (not fail)
        assert seen[0] == ("SAVEPOINT", False)  # the savepoint is opened BEFORE the phase (the base allowlist covers it)
        assert len(seen) > 1 and all(phase for _s, phase in seen[1:]), seen  # every later mirror statement ran inside it
        assert g.vec_phase is False  # closed again, whichever way the mirror ended
        assert g.denials == []


def test_a_denied_vec_statement_still_degrades_to_vec_mirrored_false_and_closes_the_phase():
    g = _fill_guard(False)

    class Conn:
        def execute(self, sql, *a):
            if sql.startswith("INSERT"):
                assert g.authorizer(sqlite3.SQLITE_INSERT, "nodes", None, "main", None) == sqlite3.SQLITE_DENY
                raise sqlite3.OperationalError("not authorized")

    assert eo.EngramdOwner._fill_vec_mirror(None, Conn(), g, [("n1", "[0.5]", [0.5])], lambda v: b"x") is False
    assert g.denials == [] and g.vec_phase is False  # the degrade drops the denials recorded since the savepoint


def test_the_real_vec0_sequence_runs_under_the_fills_authorizer_with_zero_denials(home):
    """The cell that would have caught the bug on any vec-capable seat: vec0's own constructor / `max` calls."""
    if not can_load_vec0():
        pytest.skip("this Python's sqlite3 cannot enable_load_extension, or sqlite_vec is not installed: "
                    "vec0 cannot be created, so the real constructor / `max` authorizer calls cannot be exercised here")
    import sqlite_vec
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(home / "v.db"))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("CREATE VIRTUAL TABLE vec_nodes USING vec0(node_id TEXT PRIMARY KEY, embedding float[384] distance_metric=cosine)")
    conn.commit()
    g = _fill_guard(False)
    conn.set_authorizer(g.authorizer)
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = [("e001", "t", vec(0)), ("e002", "t", vec(1))]
        assert eo.EngramdOwner._fill_vec_mirror(None, conn, g, changed, sqlite_vec.serialize_float32) is True
        assert g.denials == []
        g.allow_commit = True
        conn.commit()
        g.allow_commit = False
    finally:
        conn.set_authorizer(None)
    got = {n: bytes(b) for n, b in conn.execute("SELECT node_id, embedding FROM vec_nodes")}
    conn.close()
    assert got == {"e001": sqlite_vec.serialize_float32(vec(0)), "e002": sqlite_vec.serialize_float32(vec(1))}


# U16-E addendum (A): the full call set recorded on a real vec0 on a vec-capable seat (Linux and macOS; macOS: SQLite 3.53.4, sqlite-vec 0.1.9) for BEGIN IMMEDIATE +
# SAVEPOINT engram_fill_vec + DELETE/INSERT twice + RELEASE + COMMIT, replayed through the fill authorizer in the vec phase.
# Needs no vec0. Not exercised by the recording: the ROLLBACK TO leg and vec_nodes_info.
VEC0_RECORDED_CALL_SET = [
    (sqlite3.SQLITE_TRANSACTION, "BEGIN", None, None, None),
    (sqlite3.SQLITE_TRANSACTION, "COMMIT", None, None, None),
    (sqlite3.SQLITE_SAVEPOINT, "BEGIN", "engram_fill_vec", None, None),
    (sqlite3.SQLITE_SAVEPOINT, "RELEASE", "engram_fill_vec", None, None),
    (sqlite3.SQLITE_READ, "x", "node_id", "main", None),
    (sqlite3.SQLITE_SELECT, None, None, None, None),
    (sqlite3.SQLITE_DELETE, "vec_nodes", None, "main", None),
    (sqlite3.SQLITE_INSERT, "vec_nodes", None, "main", None),
    (sqlite3.SQLITE_READ, "vec_nodes", "node_id", "main", None),
]
for _tbl, _cols in (("vec_nodes_rowids", ("rowid", "id", "chunk_id", "chunk_offset")),
                    ("vec_nodes_chunks", ("chunk_id", "")),
                    ("vec_nodes_vector_chunks00", ("rowid",))):
    for _c in _cols:
        VEC0_RECORDED_CALL_SET += [(sqlite3.SQLITE_READ, _tbl, _c, "main", None), (sqlite3.SQLITE_UPDATE, _tbl, _c, "main", None)]
    VEC0_RECORDED_CALL_SET += [(sqlite3.SQLITE_INSERT, _tbl, None, "main", None), (sqlite3.SQLITE_DELETE, _tbl, None, "main", None)]
VEC0_RECORDED_CALL_SET.append(MAX_FUNCTION)


def test_every_authorizer_call_in_the_recorded_vec0_mirror_call_set_is_allowed_in_the_vec_phase():
    g = _fill_guard(True)
    g.allow_commit = True
    for t in VEC0_RECORDED_CALL_SET:
        assert g.authorizer(*t) == sqlite3.SQLITE_OK, t
    assert g.denials == []


def test_of_the_recorded_vec0_mirror_call_set_only_x_node_id_and_max_depend_on_the_vec_phase():
    for t in VEC0_RECORDED_CALL_SET:
        g = _fill_guard(False)
        g.allow_commit = True
        want = sqlite3.SQLITE_DENY if t in (X_NODE_ID_READ, MAX_FUNCTION) else sqlite3.SQLITE_OK
        assert g.authorizer(*t) == want, t
