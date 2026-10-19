"""engram-alpha#3879 (U16 of #2652), INTEGRATION round: tools/engram-regenerate-embeddings.py against the REAL owner.

The tool half (tests/test_regen_embeddings_owner_3879.py) drives the tool against a FAKE owner; the owner half
(tests/test_engramd_fill_embeddings_3879.py) drives the REAL owner with hand-built requests. Nothing yet ran the
two together. These cells do (a real `EngramdOwner` serving a temp home, the tool pointed at it, the shipped
client between them), and pin the four behaviours the integration round adds:

  (1) differential   flag OFF (the direct path) vs flag ON (a real owner) from identical stores -> identical
                     `--json`, identical `[regen] Done.` line, identical `nodes.embedding` bytes, identical FTS
                     doc count, identical `embedding_restore_state` row (updated_at aside), for BOTH finish
                     branches (clear: processed == 0; finalize: clean, with per-node failures while the embedder
                     is up, and with per-node failures while it went away). The vec_nodes half needs a Python
                     whose sqlite3 can load extensions plus sqlite_vec; it SKIPS elsewhere, with that reason.
  (2) CAS race       a concurrent writer sets some of the ids the tool read as NULL, between its read and its
                     fill; those rows keep the writer's value, the tool reports them as skipped_not_null,
                     `embedded` counts only what the owner filled, `after_null` is the FRESH read, exit 0.
  (3) ack accounting a `fill_embeddings` ack whose three counts do not sum to the items SENT is a malformed
                     answer: exit 3, the reason names the mismatch, no JSON (fake owner that drops / invents one).
  (4) owner down     the real owner answers the reads and the first fill, then stops: exit 3, no JSON, zero
                     direct connects to the live store, the first batch stays filled, and a re-run against a
                     restarted owner ends in exactly the flag-OFF state (the CAS makes the re-run safe).
  (5) probe          the embedder probe that picks the flag CAUSE runs in the TOOL's process and travels as
                     `embedder_available`; the owner process never probes (it is made to raise).

Every cell names its mutation in the docstring.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

from _owner_platform import requires_linux_owner
from test_regen_embeddings_owner_3879 import (
    DIM,
    RegenOwner,
    Result,
    Spy,
    configured_model,
    live_connects,
    load_tool,
    make_store,
    set_flag,
    stub_embed,
    vec_for,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
if str(SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(SRC_ENGRAM))

import engram_core  # noqa: E402,F401
import engramd_owner as eo  # noqa: E402
import server  # noqa: E402,F401

FLAG_KEY = "regen_embeddings_via_owner"
MODEL = "all-MiniLM-L6-v2"


# ---- isolation ---------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_imports(monkeypatch):
    mods, path = dict(sys.modules), list(sys.path)
    monkeypatch.setenv("ENGRAM_NO_DB_GUARDS", "1")
    monkeypatch.delenv("ENGRAM_NO_EMBEDDINGS", raising=False)
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    yield
    for name in [n for n in sys.modules if n not in mods]:
        del sys.modules[name]
    for name, mod in mods.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod
    sys.path[:] = path


@pytest.fixture
def root(monkeypatch):
    """A SHORT temp dir (the owner's AF_UNIX socket path must fit); HOME is a temp dir too, so an unchanged
    `~/.engram` default can never reach the real seat."""
    d = Path(tempfile.mkdtemp(prefix="rg3879r-"))
    fake_home = Path(tempfile.mkdtemp(prefix="rg3879rh-"))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("ENGRAM_HOME", str(d / "on"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(fake_home, ignore_errors=True)


# ---- the real-schema store ----------------------------------------------------------------------------

def _try_load_vec(conn) -> bool:
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return True
    except Exception:
        return False


def can_load_vec0() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        return _try_load_vec(conn)
    finally:
        conn.close()


def write_config(home: Path, flag: bool) -> None:
    (home / "config.json").write_text(json.dumps({
        "embedding": {"model": MODEL},
        "memory": {"current_turn": 3},
        "engramd": {FLAG_KEY: flag},
    }), encoding="utf-8")


def make_real_store(home: Path, kind: str = "mixed", n: int = 100, flag: bool = False) -> Path:
    """The REAL schema (`engram_core._run_db_one_time_setup`), then rows. Kinds:

    mixed   n observations, 30 NULL embeddings (i % 10 < 3) that have text; plus the permanent skips: 3
            evidence nodes without source_title, 2 empty claims (NULL, no text); one NULL non-current row
    full    n observations, every one embedded, nothing else (the processed == 0 path)
    many    n observations, every one NULL (several batches)
    A STALE restore-completeness flag is preset (what a crashed earlier attempt leaves), so both branches
    have something to clear or overwrite.
    """
    home.mkdir(parents=True, exist_ok=True)
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        _try_load_vec(conn)  # so the vec_nodes table exists wherever vec loads
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        rows = []
        for i in range(n):
            claim = f"claim text {i}"
            emb = None if kind == "many" or (kind == "mixed" and i % 10 < 3) else json.dumps(vec_for(claim))
            rows.append((f"ob_{i:04d}", "observation_factual", claim, emb, None, 1))
        if kind == "mixed":
            for i in range(3):
                rows.append((f"ev_{i:04d}", "evidence", "unused", None, None, 1))
            for i in range(2):
                rows.append((f"em_{i:04d}", "observation_factual", "", None, None, 1))
            rows.append(("old_0001", "observation_factual", "superseded claim", None, None, 0))
        for nid, typ, claim, emb, status, cur in rows:
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, memory_status, embedding, status)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (nid, typ, claim, "2026-01-01T00:00:00", "{}", cur, "active", emb, status))
        conn.commit()
        engram_core._ensure_embedding_restore_state_table(conn)
        engram_core._set_embedding_restore_flag(conn, null_count=7, cause="per-node-failures")
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    write_config(home, flag)
    return db


def snapshot(db: Path) -> dict:
    """Everything the issue's cell 1 compares, read straight from the store file."""
    conn = sqlite3.connect(str(db))
    try:
        emb = conn.execute("SELECT id, embedding FROM nodes ORDER BY id").fetchall()
        try:
            fts = conn.execute("SELECT COUNT(*) FROM nodes_fts_docsize").fetchone()[0]
        except sqlite3.OperationalError:
            fts = None
        try:
            state = conn.execute(
                "SELECT not_fully_restored, null_count, cause FROM embedding_restore_state WHERE singleton_key = 1"
            ).fetchall()
        except sqlite3.OperationalError:
            state = None
        return {"embeddings": emb, "fts_docs": fts, "restore_state": state}
    finally:
        conn.close()


def vec_rows(db: Path):
    """vec_nodes rows, or None when this Python cannot load the vec0 module."""
    conn = sqlite3.connect(str(db))
    try:
        if not _try_load_vec(conn):
            return None
        try:
            return conn.execute("SELECT node_id, embedding FROM vec_nodes ORDER BY node_id").fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()


def null_ids(db: Path) -> list:
    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute(
            "SELECT id FROM nodes WHERE is_current = 1 AND embedding IS NULL ORDER BY id")]
    finally:
        conn.close()


# ---- the tool's embedder, and the owner-side tripwire --------------------------------------------------

class ToolEmbedder:
    """Stands in for `server._embedder` (the embedder the TOOL just used). `is_available` answers True for the
    tool's own start-up check (call 1) and `later` for every call after it: the embedder that went away
    between the start of the pass and its finalize."""

    _model = "fake_model"

    def __init__(self, embed=stub_embed, later=True):
        self.embed, self.later, self.calls = embed, later, 0

    def is_available(self) -> bool:
        self.calls += 1
        return True if self.calls == 1 else self.later

    def _load_model(self, name):
        return None

    def embed_batch(self, texts, model_name):
        return self.embed(texts, model_name)


class OwnerSideProbe:
    """Stands in for `engram_core._embedder` while a REAL owner (same process) serves: any probe from the
    owner raises, which the owner surfaces as a failed op (exit 3), never as a quiet wrong cause."""

    _model = None

    def __init__(self):
        self.calls = 0

    def is_available(self) -> bool:
        self.calls += 1
        raise AssertionError("the owner called _embedder.is_available(): the probe belongs to the tool (#3879)")


def failing_on_1(texts, model_name):
    """A vector for every text, except [] (a per-node failure) for the texts ending in 1: 10 of the 30."""
    return [[] if t.endswith("1") else vec_for(t) for t in texts]


def run(monkeypatch, capsys, home: Path, *, tool_embedder, core_embedder=None, mod=None, argv=None):
    """Run the tool's main() in-process against `home`. `server._embedder` is the tool's embedder;
    `engram_core._embedder` (what a direct-path finalize and a probing owner would use) is `core_embedder`,
    by default the SAME object, as in a real process."""
    import server as srv

    srv._configure_paths(home)
    mod = mod or load_tool()
    monkeypatch.setattr(srv, "_embedder", tool_embedder)
    monkeypatch.setattr(srv.core, "_embedder", core_embedder if core_embedder is not None else tool_embedder)
    spy = Spy().install(monkeypatch)
    rc = 0
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(mod, "_import_server", return_value=srv))
        stack.enter_context(mock.patch.object(
            sys, "argv", ["engram-regenerate-embeddings.py", *(argv or ["--engram-home", str(home), "--json"])]))
        capsys.readouterr()
        try:
            mod.main()
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return Result(rc, captured.out, captured.err, spy)


@contextlib.contextmanager
def real_owner(home: Path):
    owner = eo.EngramdOwner(home, request_deadline=5.0)
    owner.start(install_signal_handlers=False)
    try:
        yield owner
    finally:
        try:
            owner.stop()
        except Exception:
            pass


def comparable(res: Result) -> dict:
    return {"rc": res.rc, "stdout": res.out, "json": res.json if res.out.strip() else None, "done": res.done_line}


# ---- (1) the differential ------------------------------------------------------------------------------

# (id, store kind, embed function, `later` availability, expected restore row after the run, expected rc)
VARIANTS = [
    ("clear_branch", "full", stub_embed, True, [(0, 0, None)], 0),
    ("finalize_clean", "mixed", stub_embed, True, [(0, 0, None)], 0),
    ("finalize_failures_embedder_up", "mixed", failing_on_1, True, [(1, 10, "per-node-failures")], 1),
    ("finalize_failures_embedder_gone", "mixed", failing_on_1, False, [(1, 10, "embedder-unavailable")], 1),
]


def run_pair(monkeypatch, capsys, root: Path, kind, embed, later):
    """Two identical stores. OFF: the direct path, flag OFF. ON: flag ON, a REAL owner serving that home.
    The direct run's finalize probes `engram_core._embedder` (the same object the tool used); the owner run has the
    tool's embedder on `server._embedder` and a TRIPWIRE on `engram_core._embedder`, so an owner that probes fails
    the run instead of agreeing by luck."""
    off_home, on_home = root / "off", root / "on"
    off_db = make_real_store(off_home, kind, flag=False)
    on_db = make_real_store(on_home, kind, flag=True)
    assert snapshot(off_db) == snapshot(on_db)  # the twins really are identical before the run

    off_emb = ToolEmbedder(embed, later)
    off = run(monkeypatch, capsys, off_home, tool_embedder=off_emb)

    on_emb, tripwire = ToolEmbedder(embed, later), OwnerSideProbe()
    with real_owner(on_home):
        on = run(monkeypatch, capsys, on_home, tool_embedder=on_emb, core_embedder=tripwire)
    assert live_connects(on.spy, on_db) == [], on.spy.connects
    assert tripwire.calls == 0
    return off, on, off_db, on_db


@requires_linux_owner
@pytest.mark.parametrize("variant", VARIANTS, ids=[v[0] for v in VARIANTS])
def test_c1_real_owner_and_direct_path_end_in_the_same_state_on_every_finish_branch(
        root, monkeypatch, capsys, variant):
    """Mutation: the owner drops/rewrites an item, mis-counts `filled`, runs the wrong finish branch, or picks its
    cause from its own embedder -> some comparison below goes red. Mutation: the tool's `embedded` or `after_null`
    wrong -> the JSON comparison goes red."""
    name, kind, embed, later, want_state, want_rc = variant
    off, on, off_db, on_db = run_pair(monkeypatch, capsys, root, kind, embed, later)
    assert off.rc == want_rc, (off.rc, off.err)
    assert comparable(on) == comparable(off), (on.err, off.err)
    a, b = snapshot(off_db), snapshot(on_db)
    assert b["embeddings"] == a["embeddings"], "nodes.embedding differs between the direct path and the owner"
    assert b["fts_docs"] == a["fts_docs"] and a["fts_docs"] is not None
    assert b["restore_state"] == a["restore_state"] == want_state
    # not vacuous: the permanent skips stayed NULL, and the embeddable NULLs were filled (all but the failures)
    if kind == "mixed":
        assert off.json["before_null"] == 35 and off.json["after_null"] == (5 if not name.startswith("finalize_fail") else 15)
        assert on.json["embedded"] == off.json["embedded"] == (30 if not name.startswith("finalize_fail") else 20)
    if kind == "full":
        assert off.json == {"before_null": 0, "embedded": 0, "failed": 0, "after_null": 0}


@pytest.mark.skipif(not can_load_vec0(), reason="this Python's sqlite3 cannot enable_load_extension, or sqlite_vec is not installed: the vec0 virtual table cannot be written or read here")
@requires_linux_owner
def test_c1_vec_nodes_rows_are_identical_between_the_direct_path_and_the_real_owner(root, monkeypatch, capsys):
    """The vec half of the differential. Skips (with the reason above) where vec0 cannot load: it did NOT run on
    the seat that built this unit. Mutation: the owner mirrors a different row set / blob -> red."""
    off, on, off_db, on_db = run_pair(monkeypatch, capsys, root, "mixed", stub_embed, True)
    a, b = vec_rows(off_db), vec_rows(on_db)
    assert a and b == a, (len(a or []), len(b or []))
    assert "vec_mirrored" not in on.err


# ---- (2) the CAS race ------------------------------------------------------------------------------------

@requires_linux_owner
def test_c2_a_concurrent_writer_between_the_tools_read_and_its_fill_is_not_overwritten(root, monkeypatch, capsys):
    """The tool reads its NULL ids, then (hooked just before its first fill) another writer sets three of them.
    Mutation: drop `AND embedding IS NULL` from the owner's CAS predicate -> the writer's vectors are overwritten
    -> red. Mutation: `after_null = before_null - embedded` -> 35 - 27 = 8, not the fresh read's 5 -> red.
    Mutation: the skips counted as filled -> `embedded` is 30, not 27 -> red."""
    home = root / "on"
    db = make_real_store(home, "mixed", flag=True)
    planted = json.dumps([9.0] * DIM)
    mod = load_tool()
    real_fill = mod._owner_fill
    state = {"ids": None}

    def racing_fill(spec, model_name, items):
        if state["ids"] is None:
            state["ids"] = [it["node_id"] for it in items[:3]]

            def plant():
                conn = sqlite3.connect(str(db), timeout=5)
                conn.executemany("UPDATE nodes SET embedding = ? WHERE id = ?", [(planted, i) for i in state["ids"]])
                conn.commit()
                conn.close()

            t = threading.Thread(target=plant)  # off the tool's thread: the spy counts only the tool's own connects
            t.start()
            t.join()
        return real_fill(spec, model_name, items)

    monkeypatch.setattr(mod, "_owner_fill", racing_fill)
    with real_owner(home):
        res = run(monkeypatch, capsys, home, tool_embedder=ToolEmbedder(), mod=mod)
    assert res.rc == 0, res.err
    assert live_connects(res.spy, db) == []
    assert len(state["ids"]) == 3
    emb = dict(snapshot(db)["embeddings"])
    assert all(emb[i] == planted for i in state["ids"]), "the CAS overwrote a vector another writer set"
    assert "skipped_not_null" in res.err and "skipped 3 node(s) that already hold a vector" in res.err
    assert res.json == {"before_null": 35, "embedded": 27, "failed": 0, "after_null": 5}
    assert res.done_line == "[regen] Done. processed=35 embedded=27 skipped=5 failed=0"
    assert len(null_ids(db)) == 5  # exactly the permanent skips remain NULL


# ---- (3) ack accounting ---------------------------------------------------------------------------------

class OvercountOwner(RegenOwner):
    """A fake owner that answers one MORE filled than it was sent."""

    def _fill(self, params):
        out = super()._fill(params)
        if out.get("status") == "ok":
            out["result"]["filled"] += 1
        return out


@pytest.mark.parametrize("owner_cls,behavior,said", [
    (RegenOwner, "fill_drop_one", "accounts for 29 of 30"),
    (OvercountOwner, "ok", "accounts for 31 of 30"),
])
def test_c3_an_ack_that_does_not_account_for_every_item_sent_is_malformed_exit_3_no_json(
        root, monkeypatch, capsys, owner_cls, behavior, said):
    """Mutation: remove the tool's accounting check -> a silent drop (or an invented fill) is believed: exit 0 or 1
    with JSON on stdout, and `embedded` wrong -> red."""
    home = root / "on"
    home.mkdir()
    make_store(home / "knowledge.db", "mixed")
    set_flag(home)
    with owner_cls(home, configured_model(home), behavior) as owner:
        res = run(monkeypatch, capsys, home, tool_embedder=ToolEmbedder())
    assert res.rc == 3, (res.rc, res.err)
    assert res.out.strip() == ""
    assert said in res.last_err_line and "malformed" in res.last_err_line, res.last_err_line
    assert live_connects(res.spy, home / "knowledge.db") == []
    assert not owner.finishes, "no finish after a malformed fill answer"


# ---- (4) the owner goes away mid-run ---------------------------------------------------------------------

@requires_linux_owner
def test_c4_the_owner_stopping_after_the_first_fill_is_exit_3_and_a_rerun_converges_on_the_flag_off_state(
        root, monkeypatch, capsys):
    """The real owner answers the reads and the first fill, then is shut down before the next batch.
    Mutation: the tool falls back to the live store, prints JSON, or exits 0/1 -> red. Mutation: the fill were not a
    compare-and-set -> the re-run would double-write; the final-state comparison with the flag-OFF twin catches
    any divergence."""
    on_home, off_home = root / "on", root / "off"
    on_db = make_real_store(on_home, "many", 120, flag=True)
    off_db = make_real_store(off_home, "many", 120, flag=False)
    mod = load_tool()
    real_fill = mod._owner_fill
    calls = {"n": 0}
    holder = {}

    def fill_then_stop(spec, model_name, items):
        ack = real_fill(spec, model_name, items)
        calls["n"] += 1
        if calls["n"] == 1:
            holder["owner"].stop()
        return ack

    monkeypatch.setattr(mod, "_owner_fill", fill_then_stop)
    with real_owner(on_home) as owner:
        holder["owner"] = owner
        res = run(monkeypatch, capsys, on_home, tool_embedder=ToolEmbedder(), mod=mod)
    assert res.rc == 3, (res.rc, res.err)
    assert res.out.strip() == "", res.out
    assert "unavailable" in res.last_err_line.lower(), res.last_err_line
    assert "50 embedding(s) were already written" in res.err
    assert live_connects(res.spy, on_db) == [], res.spy.connects
    assert calls["n"] == 1
    left = null_ids(on_db)
    assert len(left) == 70 and left[0] == "ob_0050", (len(left), left[:2])  # the first batch (ids 0..49) stayed filled
    assert snapshot(on_db)["restore_state"] == [(1, 7, "per-node-failures")]  # no finish ran: the stale flag is untouched

    run(monkeypatch, capsys, off_home, tool_embedder=ToolEmbedder())  # the flag-OFF reference
    with real_owner(on_home):
        again = run(monkeypatch, capsys, on_home, tool_embedder=ToolEmbedder())
    assert again.rc == 0, again.err
    assert again.json == {"before_null": 70, "embedded": 70, "failed": 0, "after_null": 0}
    assert snapshot(on_db) == snapshot(off_db)


# ---- (5) the probe travels from the tool ------------------------------------------------------------------

@pytest.mark.parametrize("later", [True, False])
def test_the_tool_sends_the_result_of_its_own_probe_evaluated_where_the_direct_finalize_would(
        root, monkeypatch, capsys, later):
    """The probe is taken after the pass (the same point the direct path's finalize reads it), from the embedder the
    TOOL used. Mutation: the tool sends a constant True, or probes before the run -> the `later=False` run is red."""
    home = root / "on"
    home.mkdir()
    make_store(home / "knowledge.db", "mixed")
    set_flag(home)
    with RegenOwner(home, configured_model(home)) as owner:
        res = run(monkeypatch, capsys, home, tool_embedder=ToolEmbedder(later=later))
    assert res.rc == 0, res.err
    assert [w[1]["embedder_available"] for w in owner.finishes] == [later]
    assert isinstance(owner.finishes[0][1]["embedder_available"], bool)
