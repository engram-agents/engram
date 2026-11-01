"""engram-alpha#3879 (U16 of #2652), TOOL half -- tools/engram-regenerate-embeddings.py writes through the owner.

Behind `engramd.regen_embeddings_via_owner` (default OFF) the tool no longer opens the live knowledge.db. It
still loads the server runtime and the embedder to COMPUTE vectors (the model stays out of the owner), but
every read goes through the owner's registered read templates and every write through the two typed owner
ops of the shared interface (/Users/<user>/.engram/cohort-specs-2026-10-17/spec-3879-interface.md):

  fill_embeddings        {"model", "items": [{"node_id", "vector"}]}, <= 50 items, CAS on the owner side
  finish_embedding_regen {"processed", "embedded", "skipped", "failed", "embedder_available"}: FTS rebuild +
                         clear/finalize flag; `embedder_available` is the TOOL's probe (spec C item 5, #3879)

The owner half is built in parallel, so every cell here drives the tool against `RegenOwner`, a FAKE owner
that implements EXACTLY that interface (validation, CAS classification, ack shapes) over the temp store. The
byte-level differential against the REAL owner is the integration round's.

Contract (cell numbers follow the issue / spec-3879-B-tool.md):

  (1) differential   flag ON vs OFF from identical stores -> same `--json`, same exit code, same stderr Done
                     line, same nodes.embedding per id, same restore-state. Shown to FAIL by an owner that
                     drops one item.
  (2) CAS            a planted race (an embedding written between the tool's read and its fill) is not
                     overwritten, comes back as skipped_not_null, and is reported, not counted as failed.
  (3) owner failure  down / Unavailable / indeterminate / refusing / readback_mismatch / malformed -> exit 3
                     (EXIT_OWNER_FAILURE, distinct from the exit 1 of failed>0), stderr names the reason, NO
                     JSON on stdout, ZERO sqlite3.connect calls on the live store, nothing written by the tool.
  (3b) Phase A       the real engram-sleep Step 3.9 shell block, fed this tool's output, yields the FAILED
                     line on an owner failure and the parsed OK line on success; never a zero-work success.
  (8) ENGRAM_HOME    a temp home, flag ON there, no --engram-home -> that home's store is written via its owner.

Plus: <= 50 per fill call and exactly one finish; the nothing-to-do run still finishes; `vec_mirrored: false`
is a stderr warning; flag OFF never contacts the owner; every read the flag-ON path sends is a registered
template, and the per-node text row is byte-equal to the SELECT engram_core issues.

Every cell names its mutation in the docstring.
"""

from __future__ import annotations

import builtins
import contextlib
import hashlib
import importlib.util
import json
import math
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

from _fake_owner import FakeOwner, registered_sql

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
TOOL_PATH = SRC_ENGRAM / "tools" / "engram-regenerate-embeddings.py"
SLEEP_SKILL = SRC_ENGRAM / "skills" / "claude" / "engram-sleep" / "SKILL.md"
TEMPLATES = SRC_ENGRAM / "engramd_read_templates.json"
FLAG_KEY = "regen_embeddings_via_owner"
SITE_PREFIX = "tools/engram-regenerate-embeddings.py:"
if str(SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(SRC_ENGRAM))
# Imported at module level so the import machinery (beartype's claw hook among it) is warm BEFORE the
# autouse fixture snapshots sys.modules; first importing it inside a test and then rolling it back breaks
# a later re-import with a circular-import error.
import engram_core  # noqa: E402,F401
import server  # noqa: E402,F401
DIM = 384

NULL_COUNT_SQL = "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
NULL_IDS_SQL = "SELECT id FROM nodes WHERE is_current=1 AND embedding IS NULL ORDER BY id"
TEXT_ROW_SQL = (
    "SELECT type, claim, source_title, predicted_event, recall_summary, recall_keywords "
    "FROM nodes WHERE id = ?"
)


# ---- isolation --------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_imports(monkeypatch):
    mods, path = dict(sys.modules), list(sys.path)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    if str(SRC_ENGRAM) not in sys.path:
        sys.path.insert(0, str(SRC_ENGRAM))
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
def homes(monkeypatch):
    """(home, fake_home): HOME is a temp dir too, so an unchanged `~/.engram` default can never reach the
    real seat; the fake home carries a decoy ~/.engram/ store a red run would write into."""
    home = Path(tempfile.mkdtemp(prefix="rg3879-"))
    fake_home = Path(tempfile.mkdtemp(prefix="rg3879-h-"))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    try:
        yield home, fake_home
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(fake_home, ignore_errors=True)


# ---- store + embedder stub --------------------------------------------------------------------------

def vec_for(text: str) -> list:
    """Deterministic 384-dim stub vector. No model is ever loaded."""
    d = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 for b in d] * (DIM // len(d))


def stub_embed(texts, model_name):
    return [vec_for(t) for t in texts]


def make_store(db: Path, kind: str = "mixed", n: int = 100) -> Path:
    """A minimal `nodes` table (the shape tests/test_345_pr1_backfill_json.py uses).

    empty      no rows
    full       n rows, every one embedded (the nothing-to-do path)
    mixed      n rows, ~30 NULL, among them 3 evidence nodes without source_title and 2 empty claims
               (permanent skips) and one NULL-embedding row that is not current (never counted)
    many       n rows, all NULL (for the batch-size cell)
    """
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT, source_title TEXT, "
        "predicted_event TEXT, recall_summary TEXT, recall_keywords TEXT, embedding TEXT, "
        "is_current INTEGER DEFAULT 1)"
    )
    rows = []
    if kind in ("full", "mixed", "many"):
        for i in range(n):
            claim = f"claim text {i}"
            emb = None
            if kind == "full" or (kind == "mixed" and i % 10 >= 3):
                emb = json.dumps(vec_for(claim))
            rows.append((f"ob_{i:04d}", "observation", claim, None, None, None, None, emb, 1))
    if kind == "mixed":
        for i in range(3):
            rows.append((f"ev_{i:04d}", "evidence", "unused", None, None, None, None, None, 1))
        for i in range(2):
            rows.append((f"em_{i:04d}", "observation", "", None, None, None, None, None, 1))
        rows.append(("old_0001", "observation", "superseded claim", None, None, None, None, None, 0))
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def embeddings(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        return dict(conn.execute("SELECT id, embedding FROM nodes ORDER BY id").fetchall())
    finally:
        conn.close()


def restore_state(db: Path):
    conn = sqlite3.connect(str(db))
    try:
        try:
            return conn.execute(
                "SELECT not_fully_restored, null_count, cause FROM embedding_restore_state"
            ).fetchall()
        except sqlite3.OperationalError:
            return None
    finally:
        conn.close()


def set_flag(home: Path, value=True) -> None:
    (home / "config.json").write_text(json.dumps({"engramd": {FLAG_KEY: value}}), encoding="utf-8")


# ---- the FAKE owner: EXACTLY the interface's two write ops -----------------------------------------

FILL_MAX = 50


def _rej(reason, detail="d", remedy="r", commit=None):
    out = {"status": "rejected", "reason": reason, "detail": detail, "remedy": remedy}
    if commit is not None:
        out["commit"] = commit
    return out


class RegenOwner(FakeOwner):
    """FakeOwner + `fill_embeddings` + `finish_embedding_regen` per spec-3879-interface.md.

    `behavior` picks a failure (default "ok"):
      fill_garbage / fill_indeterminate / fill_readback / fill_rejected / fill_malformed / fill_drop_one
      finish_rejected / finish_malformed / finish_readback
    `before_fill(home, params)` runs after validation and before the CAS: the planted race.
    """

    def __init__(self, home, model, behavior="ok", vec_mirrored=True, before_fill=None,
                 fill_edit=None, finish_edit=None, read_rows=None):
        super().__init__(home)
        # spec D (#3879): `fill_edit(result, params)` / `finish_edit(result, params)` rewrite the otherwise
        # correct ack (a malformed-ack owner); `read_rows` maps a registered SQL to the rows it answers instead.
        self.fill_edit, self.finish_edit, self.read_rows = fill_edit, finish_edit, read_rows or {}
        # #3879 read-shape: a `read_rows` value may be a callable `(call_index) -> rows` (call_index counts the
        # calls of THAT sql, from 0) so a test can make only a later call malformed.
        self._read_calls = {}
        self.model = model
        self.behavior = behavior
        self.vec_mirrored = vec_mirrored
        self.before_fill = before_fill
        self.writes = []  # (method, params, request_id)

    @property
    def fills(self):
        return [w for w in self.writes if w[0] == "fill_embeddings"]

    @property
    def finishes(self):
        return [w for w in self.writes if w[0] == "finish_embedding_regen"]

    @property
    def reads(self):
        return [r["params"]["sql"] for r in self.requests if r.get("method") == "read"]

    def _answer(self, request):
        method = request.get("method")
        if method == "read" and (request.get("params") or {}).get("sql") in self.read_rows:
            sql = request["params"]["sql"]
            rows = self.read_rows[sql]
            if callable(rows):
                idx = self._read_calls.get(sql, 0)
                self._read_calls[sql] = idx + 1
                rows = rows(idx)
            return {"status": "ok", "result": rows}
        if method not in ("fill_embeddings", "finish_embedding_regen"):
            return super()._answer(request)
        params, rid = request.get("params"), request.get("request_id")
        self.writes.append((method, params, rid))
        if not isinstance(rid, str) or not rid:
            return _rej("invalid_request_id")
        return self._fill(params) if method == "fill_embeddings" else self._finish(params)

    # -- validation: the interface, over ALL items before any write
    def _validate(self, params):
        if not isinstance(params, dict) or set(params) != {"model", "items"}:
            return _rej("invalid_params", "params must be exactly {model, items}")
        items = params["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= FILL_MAX:
            return _rej("invalid_params", f"items must hold 1..{FILL_MAX} entries")
        for it in items:
            if not isinstance(it, dict) or set(it) != {"node_id", "vector"}:
                return _rej("invalid_params", "item must be exactly {node_id, vector}")
            if not isinstance(it["node_id"], str) or len(it["node_id"].encode("utf-8")) > 256:
                return _rej("invalid_params", "bad node_id")
            vec = it["vector"]
            if not isinstance(vec, list) or len(vec) != DIM:
                return _rej("bad_dimension", f"vector must have {DIM} elements")
            for x in vec:
                if isinstance(x, bool) or not isinstance(x, (int, float)):
                    return _rej("invalid_params", "vector element not a number")
                if not math.isfinite(x):
                    return _rej("non_finite", "vector element not finite")
        if params["model"] != self.model:
            return _rej("model_mismatch", f"{params['model']!r} != {self.model!r}")
        return None

    def _fill(self, params):
        b = self.behavior
        if b == "fill_garbage":
            return {"status": "bogus"}
        err = self._validate(params)
        if err is not None:
            return err
        if b == "fill_indeterminate":
            return _rej("deadline_exceeded", commit="indeterminate")
        if b == "fill_rejected":
            return _rej("busy", "the write lock was not free within the budget")
        if self.before_fill is not None:
            self.before_fill(self.home, params)
        items = params["items"]
        if b == "fill_drop_one":
            items = items[:-1]
        filled = skipped_not_null = skipped_gone = 0
        conn = sqlite3.connect(str(self.home / "knowledge.db"))
        try:
            for it in items:
                cur = conn.execute(
                    "UPDATE nodes SET embedding = ? WHERE id = ? AND is_current = 1 AND embedding IS NULL",
                    (json.dumps(it["vector"]), it["node_id"]),
                )
                if cur.rowcount == 1:
                    filled += 1
                    continue
                row = conn.execute(
                    "SELECT is_current, embedding FROM nodes WHERE id = ?", (it["node_id"],)
                ).fetchone()
                if row is not None and row[0] == 1 and row[1] is not None:
                    skipped_not_null += 1
                else:
                    skipped_gone += 1
            conn.commit()
        finally:
            conn.close()
        if b == "fill_readback":
            return _rej("readback_mismatch", commit="committed_unverified")
        if b == "fill_malformed":
            return {"status": "ok", "result": "done"}
        result = {
            "filled": filled, "skipped_not_null": skipped_not_null,
            "skipped_not_current_or_missing": skipped_gone, "vec_mirrored": self.vec_mirrored}
        if self.fill_edit is not None:
            result = self.fill_edit(result, params)
        return {"status": "ok", "result": result}

    def _finish(self, params):
        b = self.behavior
        count_keys = {"processed", "embedded", "skipped", "failed"}
        keys = count_keys | {"embedder_available"}  # spec C item 5: the tool sends its own probe result
        if (not isinstance(params, dict) or set(params) != keys
                or not isinstance(params["embedder_available"], bool)
                or any(isinstance(params[k], bool) or not isinstance(params[k], int) or params[k] < 0
                       for k in count_keys)):
            return _rej("invalid_params",
                        "params must be exactly {processed, embedded, skipped, failed, embedder_available}")
        if b == "finish_rejected":
            return _rej("busy", "the write lock was not free within the budget")
        if b == "finish_malformed":
            return {"status": "ok", "result": ["not", "a", "dict"]}
        if b == "finish_readback":
            return _rej("readback_mismatch", commit="committed_unverified")
        import engram_core as core  # lazy: the autouse fixture puts src/engram on sys.path
        conn = sqlite3.connect(str(self.home / "knowledge.db"))
        try:
            # like the real owner: ALWAYS the recount path, never a branch on the client's `processed`
            core._finalize_embedding_restore_flag(
                conn, {k: params[k] for k in count_keys}, respect_lease=True,
                embedder_available=params["embedder_available"])
            conn.commit()
            row = conn.execute("SELECT cause FROM embedding_restore_state").fetchone()
        finally:
            conn.close()
        result = {"fts_rows": 0, "restore_state": row[0] if row else None}
        if self.finish_edit is not None:
            result = self.finish_edit(result, params)
        return {"status": "ok", "result": result}


# ---- driving the tool -------------------------------------------------------------------------------

def load_tool():
    spec = importlib.util.spec_from_file_location("regen_embeddings_3879", str(TOOL_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Spy:
    """Record, on THIS thread only (the in-process fake owner serves from other threads), every
    sqlite3.connect target."""

    def __init__(self):
        self.connects = []
        self._thread = threading.get_ident()

    def install(self, monkeypatch):
        real_connect, real_dbapi = sqlite3.connect, sqlite3.dbapi2.connect

        def guard(real):
            def f(*a, **k):
                if threading.get_ident() == self._thread:
                    self.connects.append(str(a[0]) if a else "")
                return real(*a, **k)
            return f

        monkeypatch.setattr(sqlite3, "connect", guard(real_connect))
        monkeypatch.setattr(sqlite3.dbapi2, "connect", guard(real_dbapi))
        return self


def live_connects(spy: Spy, live: Path) -> list:
    """Every connect whose target resolves (realpath; plain path or `file:` URI, query dropped) to `live`."""
    import urllib.parse
    want = os.path.realpath(str(live))
    hits = []
    for c in spy.connects:
        target = c
        if target.startswith("file:"):
            target = urllib.parse.unquote(urllib.parse.urlparse(target).path)
        if target and os.path.realpath(target) == want:
            hits.append(c)
    return hits


class Result:
    def __init__(self, rc, out, err, spy):
        self.rc, self.out, self.err, self.spy = rc, out, err, spy

    @property
    def json(self):
        lines = [ln for ln in self.out.splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected exactly one JSON line, got {self.out!r}"
        return json.loads(lines[0])

    @property
    def done_line(self):
        lines = [ln for ln in self.err.splitlines() if ln.startswith("[regen] Done.")]
        assert len(lines) == 1, self.err
        return lines[0]

    @property
    def last_err_line(self):
        return [ln for ln in self.err.splitlines() if ln.strip()][-1]


def run_tool(monkeypatch, capsys, home, argv, embed=stub_embed, mod=None):
    """Run main() in-process with the real server module and a stubbed embedder; return Result."""
    import server as srv

    srv._configure_paths(home)
    mod = mod or load_tool()
    spy = Spy().install(monkeypatch)
    rc = 0
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(srv._embedder, "is_available", return_value=True))
        stack.enter_context(mock.patch.object(srv._embedder, "_load_model", return_value=None))
        stack.enter_context(mock.patch.object(srv._embedder, "_model", "fake_model", create=True))
        stack.enter_context(mock.patch.object(srv._embedder, "embed_batch", side_effect=embed))
        import_mock = stack.enter_context(mock.patch.object(mod, "_import_server", return_value=srv))
        stack.enter_context(mock.patch.object(sys, "argv", ["engram-regenerate-embeddings.py", *argv]))
        capsys.readouterr()
        try:
            mod.main()
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    res = Result(rc, captured.out, captured.err, spy)
    res.import_server_mock = import_mock
    return res


def configured_model(home) -> str:
    import server as srv
    srv._configure_paths(home)
    return srv._get_embedding_config().get("model", srv.DEFAULT_EMBEDDING_MODEL)


def comparable(res: Result, db: Path) -> dict:
    return {
        "rc": res.rc,
        "json": res.json if res.out.strip() else None,
        "done": res.done_line,
        "embeddings": embeddings(db),
        "restore_state": restore_state(db),
    }


def differential(monkeypatch, capsys, tmp_root: Path, kind: str, behavior="ok", n=100):
    """Identical store copies; flag OFF on one, flag ON (through the fake owner) on the other."""
    off_home, on_home = tmp_root / "off", tmp_root / "on"
    for h in (off_home, on_home):
        h.mkdir()
    make_store(off_home / "knowledge.db", kind, n)
    shutil.copy(off_home / "knowledge.db", on_home / "knowledge.db")
    set_flag(on_home)

    off = run_tool(monkeypatch, capsys, off_home, ["--engram-home", str(off_home), "--json"])
    model = configured_model(on_home)
    with RegenOwner(on_home, model, behavior) as owner:
        on = run_tool(monkeypatch, capsys, on_home, ["--engram-home", str(on_home), "--json"])
    assert live_connects(on.spy, on_home / "knowledge.db") == [], on.spy.connects
    return comparable(off, off_home / "knowledge.db"), comparable(on, on_home / "knowledge.db"), owner


# ---- (1) the differential ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["empty", "full", "mixed"])
def test_c1_flag_on_and_off_give_the_same_json_exit_stderr_done_line_embeddings_and_restore_state(
        homes, monkeypatch, capsys, kind):
    """Mutation: the owner op drops one item (see the next cell) / the tool counts the wrong number as
    embedded -> the comparison goes red."""
    home, _ = homes
    off, on, owner = differential(monkeypatch, capsys, home, kind)
    assert on == off, (kind, on["json"], off["json"], on["done"], off["done"])
    if kind == "mixed":
        assert off["json"]["before_null"] == 30 + 5 and off["json"]["embedded"] == 30
        assert off["json"]["after_null"] == 5  # the permanent skips stay NULL on both paths
        assert owner.fills, "vacuous: the flag-ON run never filled through the owner"


def test_c1_an_owner_that_silently_drops_one_item_is_refused_as_a_malformed_ack_not_counted(homes, monkeypatch, capsys):
    """Since spec C (#3879): the ack accounting check turns the silent drop the differential used to merely SHOW into
    exit 3. Mutation: remove the tool's accounting check -> the drop is silent again (exit 0, JSON, after_null
    higher than the flag-OFF run) -> red."""
    home, _ = homes
    off_home, on_home = home / "off", home / "on"
    for h in (off_home, on_home):
        h.mkdir()
    make_store(off_home / "knowledge.db", "mixed")
    shutil.copy(off_home / "knowledge.db", on_home / "knowledge.db")
    set_flag(on_home)
    with RegenOwner(on_home, configured_model(on_home), "fill_drop_one"):
        on = run_tool(monkeypatch, capsys, on_home, ["--engram-home", str(on_home), "--json"])
    assert on.rc == 3, (on.rc, on.err)
    assert on.out.strip() == ""


# ---- batches, protocol -----------------------------------------------------------------------------

def test_fills_are_at_most_50_items_then_exactly_one_finish_with_the_configured_model(
        homes, monkeypatch, capsys):
    """Mutation: batch of 100 -> sizes differ; a second/zero finish; wrong model name -> red."""
    home, _ = homes
    make_store(home / "knowledge.db", "many", 120)
    set_flag(home)
    model = configured_model(home)
    with RegenOwner(home, model) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.rc == 0, res.err
    assert [len(w[1]["items"]) for w in owner.fills] == [50, 50, 20]
    assert all(w[1]["model"] == model and set(w[1]) == {"model", "items"} for w in owner.fills)
    assert all(set(it) == {"node_id", "vector"} and len(it["vector"]) == DIM
               for w in owner.fills for it in w[1]["items"])
    assert [w[0] for w in owner.writes] == ["fill_embeddings"] * 3 + ["finish_embedding_regen"]
    assert owner.finishes[0][1] == {"processed": 120, "embedded": 120, "skipped": 0, "failed": 0,
                                    "embedder_available": True}
    rids = [w[2] for w in owner.writes]
    assert len(set(rids)) == len(rids) and all(isinstance(r, str) and r for r in rids)
    assert res.json == {"before_null": 120, "embedded": 120, "failed": 0, "after_null": 0}


def test_nothing_to_do_still_finishes_with_zero_counts_and_never_fills(homes, monkeypatch, capsys):
    """Mutation: skip the finish op when there is nothing to embed -> the nightly FTS rebuild and the
    stale-flag clear never happen -> red."""
    home, _ = homes
    make_store(home / "knowledge.db", "full", 20)
    set_flag(home)
    with RegenOwner(home, configured_model(home)) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.rc == 0, res.err
    assert owner.fills == []
    assert [w[1] for w in owner.finishes] == [
        {"processed": 0, "embedded": 0, "skipped": 0, "failed": 0, "embedder_available": True}]
    assert "nothing to do" in res.err
    assert res.done_line == "[regen] Done. processed=0 embedded=0 skipped=0 failed=0"
    assert res.json == {"before_null": 0, "embedded": 0, "failed": 0, "after_null": 0}


def test_a_per_node_embed_failure_still_exits_1_with_json_and_reports_failed_to_finish(
        homes, monkeypatch, capsys):
    """Mutation: exit 3 (or 0) for failed>0 -> red; the failed count not forwarded to finish -> red."""
    home, _ = homes
    make_store(home / "knowledge.db", "many", 4)
    set_flag(home)

    def embed(texts, model_name):
        return [vec_for(texts[0]), [], vec_for(texts[2]), vec_for(texts[3])]

    with RegenOwner(home, configured_model(home)) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"], embed=embed)
    assert res.rc == 1, (res.rc, res.err)
    assert res.json == {"before_null": 4, "embedded": 3, "failed": 1, "after_null": 1}
    assert len(owner.fills[0][1]["items"]) == 3
    assert owner.finishes[0][1] == {"processed": 4, "embedded": 3, "skipped": 0, "failed": 1,
                                    "embedder_available": True}


# ---- (2) CAS -----------------------------------------------------------------------------------------

def test_c2_a_planted_race_is_skipped_not_null_reported_and_not_failed(homes, monkeypatch, capsys):
    """Mutation: after_null computed as before_null - embedded -> 1 != the fresh read's 0 -> red.
    Mutation: the skip counted as failed -> exit 1 -> red."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 10)
    set_flag(home)
    planted = json.dumps([9.0] * DIM)

    def plant(owner_home, params):
        conn = sqlite3.connect(str(owner_home / "knowledge.db"))
        conn.execute("UPDATE nodes SET embedding = ? WHERE id = 'ob_0003'", (planted,))
        conn.commit()
        conn.close()

    with RegenOwner(home, configured_model(home), before_fill=plant) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.rc == 0, res.err
    assert embeddings(db)["ob_0003"] == planted, "the CAS overwrote a vector something else wrote"
    assert "skipped_not_null" in res.err and "1" in res.err
    assert res.json == {"before_null": 10, "embedded": 9, "failed": 0, "after_null": 0}
    assert owner.finishes[0][1]["embedded"] == 9


# ---- vec_mirrored ----------------------------------------------------------------------------------

def test_vec_mirrored_false_is_a_stderr_warning_and_true_is_silent(homes, monkeypatch, capsys):
    """Mutation: swallow vec_mirrored=false (today's `except: pass`) -> no warning -> red. Not a failure:
    exit stays 0."""
    home, _ = homes
    make_store(home / "knowledge.db", "many", 5)
    set_flag(home)
    with RegenOwner(home, configured_model(home), vec_mirrored=False):
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.rc == 0, res.err
    assert any("vec_mirrored" in ln and "WARNING" in ln for ln in res.err.splitlines()), res.err

    home2 = home / "again"
    home2.mkdir()
    make_store(home2 / "knowledge.db", "many", 5)
    set_flag(home2)
    with RegenOwner(home2, configured_model(home2), vec_mirrored=True):
        res = run_tool(monkeypatch, capsys, home2, ["--engram-home", str(home2), "--json"])
    assert res.rc == 0 and "vec_mirrored" not in res.err, res.err


# ---- (3) owner failure ------------------------------------------------------------------------------

# (behavior, store had to stay all-NULL, a token the last stderr line must carry)
OWNER_FAILURES = [
    ("down", True, "unavailable"),
    ("fill_garbage", True, "protocol"),
    ("fill_indeterminate", True, "INDETERMINATE"),
    ("fill_rejected", True, "busy"),
    ("fill_readback", False, "readback_mismatch"),
    ("fill_malformed", False, "malformed"),
    ("finish_rejected", False, "busy"),
    ("finish_malformed", False, "malformed"),
    ("finish_readback", False, "readback_mismatch"),
]


@pytest.mark.parametrize("behavior,untouched,token", OWNER_FAILURES)
def test_c3_owner_failure_exits_3_names_the_reason_prints_no_json_and_never_touches_the_live_store(
        homes, monkeypatch, capsys, behavior, untouched, token):
    """Mutation: fall back to the direct path on an owner failure -> a live connect / exit 0 -> red.
    Mutation: print the JSON on owner failure -> stdout not empty -> red."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 6)
    set_flag(home)
    mod = load_tool()
    assert mod.EXIT_OWNER_FAILURE == 3 and mod.EXIT_OWNER_FAILURE != 1
    if behavior == "down":
        res = run_tool(monkeypatch, capsys, home, ["--json"], mod=mod)
    else:
        with RegenOwner(home, configured_model(home), behavior):
            res = run_tool(monkeypatch, capsys, home, ["--json"], mod=mod)
    assert res.rc == 3, (res.rc, res.err)
    assert res.out == "", f"JSON on stdout after an owner failure: {res.out!r}"
    assert token.lower() in res.last_err_line.lower(), res.err
    assert "owner" in res.last_err_line.lower(), res.err
    assert live_connects(res.spy, db) == [], res.spy.connects
    if untouched:
        assert all(v is None for v in embeddings(db).values()), "the tool wrote around a failed owner"


def test_c3_the_exit_code_is_distinct_from_failed_gt_0(homes, monkeypatch, capsys):
    """Mutation: EXIT_OWNER_FAILURE = 1 -> an owner outage reads as per-node failures -> red."""
    home, _ = homes
    make_store(home / "knowledge.db", "many", 3)
    set_flag(home)
    down = run_tool(monkeypatch, capsys, home, ["--json"])
    assert down.rc == 3
    home2 = home / "b"
    home2.mkdir()
    make_store(home2 / "knowledge.db", "many", 3)
    set_flag(home2)
    with RegenOwner(home2, configured_model(home2)):
        failed = run_tool(monkeypatch, capsys, home2, ["--engram-home", str(home2), "--json"],
                          embed=lambda t, m: [[] for _ in t])
    assert failed.rc == 1 and down.rc != failed.rc


# ---- (3b) Phase A consumption: the real Step 3.9 block ---------------------------------------------

def step_3_9_block() -> str:
    text = SLEEP_SKILL.read_text(encoding="utf-8")
    start = text.index("### Step 3.9")
    fence = text.index("```bash", start)
    body_start = text.index("\n", fence) + 1
    return text[body_start:text.index("```", body_start)]


def _venv_python_wrapper(login_home: Path) -> None:
    """$HOME/.engram/venv/bin/python3 as Step 3.9 invokes it: a wrapper onto THIS interpreter. The plugin
    venv is always under the LOGIN home, whatever ENGRAM_HOME is (Borges' ruling on #3863)."""
    exe = login_home / ".engram" / "venv" / "bin" / "python3"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)


# #4313: the spec's path root is the fallback expansion (CLAUDE_PLUGIN_ROOT is unset in an agent's Bash)
_PLUGIN_ROOT_EXPR = "${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/marketplace/plugins/engram}"


def phase_a_status(home: Path, fake_home: Path, tool_path: str) -> str:
    """Run the Step 3.9 shell block verbatim (tool path substituted when replaying) and return
    $embed_backfill_status."""
    block = step_3_9_block().replace('"'+_PLUGIN_ROOT_EXPR+'/tools/engram-regenerate-embeddings.py"',
                                    f'"{tool_path}"')
    env = dict(os.environ, ENGRAM_HOME=str(home), HOME=str(fake_home), CLAUDE_PLUGIN_ROOT=str(SRC_ENGRAM))
    done = subprocess.run(["bash", "-c", block + '\nprintf "%s" "$embed_backfill_status"'],
                          capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _replay_tool(tmp: Path, res: Result) -> str:
    script = tmp / "replay_tool.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({res.out!r})\n"
        f"sys.stderr.write({res.err!r})\n"
        f"sys.exit({res.rc})\n")
    return str(script)


def test_c3b_step_3_9_reads_a_real_owner_down_run_as_failed_never_as_zero_work(homes):
    """Mutation: print the JSON on owner failure / exit 0 -> the shell block parses a success line -> red.
    The REAL tool is run as a subprocess exactly as Step 3.9 does (--json, stdout captured), owner down."""
    home, fake_home = homes
    db = make_store(home / "knowledge.db", "many", 6)
    set_flag(home)
    _venv_python_wrapper(fake_home)
    status = phase_a_status(home, fake_home, str(TOOL_PATH))
    assert status.startswith("⚠ embedding backfill FAILED (exit 3):"), status
    assert "owner" in status.lower() and "unavailable" in status.lower(), status
    assert not status.startswith("✓") and "OK" not in status
    assert all(v is None for v in embeddings(db).values()), "the tool wrote around a down owner"


def test_c3b_step_3_9_parses_the_json_of_a_successful_owner_run(homes, monkeypatch, capsys):
    """Mutation: a changed JSON shape (renamed key) -> the shell block's parse fails -> red."""
    home, fake_home = homes
    make_store(home / "knowledge.db", "many", 7)
    set_flag(home)
    _venv_python_wrapper(fake_home)
    with RegenOwner(home, configured_model(home)) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.rc == 0, res.err
    assert owner.fills, "vacuous: the flag-ON run never wrote through the owner"
    status = phase_a_status(home, fake_home, _replay_tool(home, res))
    assert status == "✓ embedding backfill OK (before=7, embedded=7, failed=0)", status


def test_c3b_step_3_9_reads_a_replayed_owner_failure_as_failed(homes, monkeypatch, capsys):
    """Mutation: an owner failure that exits 0 -> '✓' -> red (in-process variant: a finish rejection)."""
    home, fake_home = homes
    make_store(home / "knowledge.db", "many", 7)
    set_flag(home)
    _venv_python_wrapper(fake_home)
    with RegenOwner(home, configured_model(home), "finish_rejected"):
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    status = phase_a_status(home, fake_home, _replay_tool(home, res))
    assert status.startswith("⚠ embedding backfill FAILED (exit 3):") and "busy" in status, status


# ---- (8) ENGRAM_HOME --------------------------------------------------------------------------------

def test_c8_a_temp_engram_home_with_the_flag_on_and_no_engram_home_arg_writes_that_homes_store(
        homes, monkeypatch, capsys):
    """Mutation: the `~/.engram` default restored -> the decoy under HOME is read/written and this home is
    not -> red."""
    home, fake_home = homes
    db = make_store(home / "knowledge.db", "many", 5)
    set_flag(home)
    decoy_dir = fake_home / ".engram"
    decoy_dir.mkdir()
    decoy = make_store(decoy_dir / "knowledge.db", "many", 5)
    with RegenOwner(home, configured_model(home)) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])  # no --engram-home
    assert res.rc == 0, res.err
    assert owner.fills, "the tool never wrote through this home's owner"
    assert all(v is not None for v in embeddings(db).values())
    assert all(v is None for v in embeddings(decoy).values()), "the tool wrote ~/.engram"


# ---- flag OFF ---------------------------------------------------------------------------------------

def test_flag_off_never_contacts_the_owner_and_writes_directly(homes, monkeypatch, capsys):
    """Mutation: route through the owner with the flag OFF -> requests recorded -> red."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 5)
    with RegenOwner(home, configured_model(home)) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.rc == 0, res.err
    assert owner.requests == [] and owner.writes == []
    assert all(v is not None for v in embeddings(db).values())


def test_flag_on_for_another_home_keeps_the_direct_path(homes, monkeypatch, capsys):
    """Mutation: apply the flag to a store that is not this home's -> red. (--engram-home naming a second
    home whose OWN config is OFF while ENGRAM_HOME's config is ON.)"""
    home, _ = homes
    set_flag(home)
    other = home / "other"
    other.mkdir()
    db = make_store(other / "knowledge.db", "many", 3)
    with RegenOwner(home, configured_model(home)) as owner:
        res = run_tool(monkeypatch, capsys, other, ["--engram-home", str(other), "--json"])
    assert res.rc == 0 and owner.requests == []
    assert all(v is not None for v in embeddings(db).values())


# ---- reads go through registered templates ---------------------------------------------------------

def _registry() -> dict:
    return {t["sql"]: t["sites"] for t in json.loads(TEMPLATES.read_text())["templates"]}


def test_every_read_the_flag_on_path_sends_is_a_registered_template_of_this_tool(homes, monkeypatch, capsys):
    """Mutation: a read built by string formatting / an unregistered SELECT -> the fake owner answers
    unregistered_template (exit 3) and the registry lacks it -> red."""
    home, _ = homes
    make_store(home / "knowledge.db", "mixed")
    set_flag(home)
    with RegenOwner(home, configured_model(home)) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.rc == 0, res.err
    sent = set(owner.reads)
    assert {NULL_COUNT_SQL, NULL_IDS_SQL, TEXT_ROW_SQL} <= sent, sent
    registry = _registry()
    assert sent <= set(registry) and sent <= registered_sql(), sent - set(registry)
    for sql, fn in ((NULL_COUNT_SQL, "_owner_null_count"), (NULL_IDS_SQL, "_owner_null_ids"),
                    (TEXT_ROW_SQL, "_owner_text_row")):
        assert SITE_PREFIX + fn in registry[sql], (fn, registry[sql])


def test_the_text_row_template_is_byte_equal_to_the_select_engram_core_issues():
    """Mutation: edit one byte of the tool's copy of the SELECT -> the owner would build the embedding text
    from a different row than the direct path does -> red."""
    import engram_core as core

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE nodes (id TEXT, type TEXT, claim TEXT, source_title TEXT, "
                 "predicted_event TEXT, recall_summary TEXT, recall_keywords TEXT)")
    seen = []
    conn.set_trace_callback(seen.append)
    core._embedding_text_for_node(conn, "x")
    selects = [s for s in seen if s.startswith("SELECT type, claim")]
    assert len(selects) == 1, seen
    assert selects[0].replace("'x'", "?") == TEXT_ROW_SQL


def test_the_flag_on_text_for_a_node_equals_the_direct_texts_for_every_node_type(homes, monkeypatch, capsys):
    """Mutation: a hand-rolled text builder in the tool that drops the feeling_report prefix strip, the
    recall_summary or the keywords -> a different vector for those nodes -> red."""
    home, _ = homes
    db = home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT, source_title TEXT, "
        "predicted_event TEXT, recall_summary TEXT, recall_keywords TEXT, embedding TEXT, "
        "is_current INTEGER DEFAULT 1)")
    conn.executemany("INSERT INTO nodes (id,type,claim,source_title,predicted_event,recall_summary,"
                     "recall_keywords) VALUES (?,?,?,?,?,?,?)", [
        ("a1", "observation", "plain claim", None, None, "a summary", json.dumps(["kw1", "kw2"])),
        ("e1", "evidence", "ignored", "a source title", None, None, None),
        ("p1", "prediction", "fallback claim", None, "the predicted event", None, None),
        ("f1", "feeling_report", "I reported feeling: calm", None, None, None, None),
    ])
    conn.commit()
    conn.close()
    off_home = home / "off"
    off_home.mkdir()
    shutil.copy(db, off_home / "knowledge.db")
    set_flag(home)
    off = run_tool(monkeypatch, capsys, off_home, ["--engram-home", str(off_home), "--json"])
    with RegenOwner(home, configured_model(home)) as owner:
        on = run_tool(monkeypatch, capsys, home, ["--json"])
    assert off.rc == on.rc == 0
    assert owner.fills, "vacuous: the flag-ON run never wrote through the owner"
    assert embeddings(db) == embeddings(off_home / "knowledge.db")
    assert all(v is not None for v in embeddings(db).values())


# ---- spec D (#3879): pin the per-contract-class mutants that survived -------------------------------------

def _assert_owner_failure(res: Result, db: Path, token: str) -> None:
    """exit 3, no JSON, the reason on the LAST stderr line, zero direct connects to the live store."""
    assert res.rc == 3, (res.rc, res.err)
    assert res.out == "", f"JSON on stdout after an owner failure: {res.out!r}"
    assert token.lower() in res.last_err_line.lower(), res.err
    assert "owner failure" in res.last_err_line, res.err
    assert live_connects(res.spy, db) == [], res.spy.connects


def test_d_t1_owner_down_exits_3_before_the_server_runtime_is_imported(homes, monkeypatch, capsys):
    """Mutation: swallow the ping failure in backfill_embeddings (`pass`) -> the run still dies later at the first
    read, but only AFTER _import_server (the runtime + model load) was paid for -> the import spy is called -> red."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.import_server_mock.call_count == 0, "the owner outage was discovered only after the runtime load"
    _assert_owner_failure(res, db, "unavailable")
    assert all(v is None for v in embeddings(db).values())


def _malformed_fill_run(monkeypatch, capsys, home, fill_edit):
    db = make_store(home / "knowledge.db", "many", 6)
    set_flag(home)
    with RegenOwner(home, configured_model(home), fill_edit=fill_edit) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    return db, res, owner


@pytest.mark.parametrize("bad", [1, 0, "false", "true", None, [True]])
def test_d_t2_a_vec_mirrored_that_is_not_a_bool_is_a_malformed_ack_exit_3(homes, monkeypatch, capsys, bad):
    """Mutation: remove the vec_mirrored type check -> the counts add up, so the run succeeds (exit 0, JSON) with a
    garbage flag believed -> red."""
    home, _ = homes
    db, res, owner = _malformed_fill_run(monkeypatch, capsys, home, lambda r, p: {**r, "vec_mirrored": bad})
    _assert_owner_failure(res, db, "vec_mirrored")
    assert not owner.finishes, "no finish after a malformed fill answer"


def test_d_t2_a_missing_vec_mirrored_is_a_malformed_ack_exit_3(homes, monkeypatch, capsys):
    """Mutation: same as above with the key absent (`ack.get` -> None is not a bool)."""
    home, _ = homes
    db, res, _owner = _malformed_fill_run(
        monkeypatch, capsys, home, lambda r, p: {k: v for k, v in r.items() if k != "vec_mirrored"})
    _assert_owner_failure(res, db, "vec_mirrored")


@pytest.mark.parametrize("negate", ["filled", "skipped_not_null", "skipped_not_current_or_missing"])
def test_d_t3_a_negative_count_whose_sum_still_matches_the_items_sent_is_malformed_exit_3(
        homes, monkeypatch, capsys, negate):
    """Mutation: drop `value < 0` from _int_field -> -1 + (n+1) + 0 == n passes the accounting sum -> the run
    succeeds on a fabricated ack -> red."""
    def edit(result, params):
        n = len(params["items"])
        counts = {"filled": 0, "skipped_not_null": 0, "skipped_not_current_or_missing": 0}
        counts[negate] = -1
        counts["skipped_not_null" if negate != "skipped_not_null" else "filled"] = n + 1
        assert sum(counts.values()) == n
        return {**result, **counts}

    home, _ = homes
    db, res, _owner = _malformed_fill_run(monkeypatch, capsys, home, edit)
    _assert_owner_failure(res, db, "not a count")


def test_d_t3_a_negative_fts_rows_on_the_finish_ack_is_malformed_exit_3(homes, monkeypatch, capsys):
    """Mutation: drop `value < 0` from _int_field -> fts_rows=-1 accepted, exit 0 -> red."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    with RegenOwner(home, configured_model(home), finish_edit=lambda r, p: {**r, "fts_rows": -1}):
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    _assert_owner_failure(res, db, "fts_rows")


@pytest.mark.parametrize("bad", [7, 0, ["a"], {"cause": "x"}, True])
def test_d_t4_a_restore_state_that_is_neither_null_nor_a_string_is_malformed_exit_3(
        homes, monkeypatch, capsys, bad):
    """Mutation: remove the restore_state type check -> any value accepted, exit 0 -> red."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    with RegenOwner(home, configured_model(home), finish_edit=lambda r, p: {**r, "restore_state": bad}):
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    _assert_owner_failure(res, db, "restore_state")


@pytest.mark.parametrize("answer", [True, False])
def test_d_t5_a_boolean_cell_in_the_null_count_read_is_exit_3_nothing_filled(homes, monkeypatch, capsys, answer):
    """End to end, the shared client (engramd_client._cells_ok) already refuses a bool cell as `unusable_answer`
    BEFORE the tool sees it, so the tool's own bool exclusion is not reachable over the wire (the mutant is
    end-to-end equivalent; the unit cell below is the one that pins it). This cell pins the observable contract:
    a bool count is exit 3, nothing written, no JSON. Mutation: swallow the failure / treat True as 1 -> red."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    with RegenOwner(home, configured_model(home), read_rows={NULL_COUNT_SQL: [[answer]]}) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    _assert_owner_failure(res, db, "owner read failed")
    assert owner.writes == []
    assert all(v is None for v in embeddings(db).values())


@pytest.mark.parametrize("cell", [True, False])
def test_d_t5_cell_int_refuses_a_bool_as_a_count_directly(cell):
    """Mutation: drop the bool exclusion in _cell_int (bool is an int subclass) -> True is read as 1 and False as
    0, a non-count believed as a count -> red. Unit-level because the shared client refuses a bool cell first."""
    mod = load_tool()
    with pytest.raises(mod.RegenOwnerError, match="not an integer"):
        mod._cell_int((cell,), "NULL-embedding count")
    assert mod._cell_int((0,), "x") == 0 and mod._cell_int((7,), "x") == 7  # real ints still pass
    for bad in (None, "3", 1.5, ()):
        with pytest.raises(mod.RegenOwnerError):
            mod._cell_int(bad if bad == () else (bad,), "x")


@pytest.mark.parametrize("width", [5, 7])
def test_d_t6_a_text_row_that_is_not_six_columns_is_malformed_exit_3_nothing_filled(
        homes, monkeypatch, capsys, width):
    """Mutation: remove the text-row column-count check -> the in-memory INSERT gets the wrong arity and the tool
    dies with a raw sqlite3 error instead of exit 3 -> red."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    with RegenOwner(home, configured_model(home), read_rows={TEXT_ROW_SQL: [["observation"] * width]}) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    _assert_owner_failure(res, db, "column count")
    assert owner.fills == []
    assert all(v is None for v in embeddings(db).values())


@pytest.mark.parametrize("bad", [5, None, 1.5])
def test_d_t7_a_non_string_id_in_the_id_list_is_malformed_exit_3_nothing_filled(
        homes, monkeypatch, capsys, bad):
    """Mutation: remove the id-list type check -> the non-string id flows on and the run exits 0 having 'done' a
    list it never understood -> red."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    with RegenOwner(home, configured_model(home), read_rows={NULL_IDS_SQL: [["ob_0000"], [bad]]}) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    _assert_owner_failure(res, db, "non-string id")
    assert owner.fills == []
    assert all(v is None for v in embeddings(db).values())


# ---- #3879 / PR #3900 (Mira's security HOLD): each owner read validates ITS OWN answer shape ---------------
# COUNT: exactly 1 row x 1 cell, a non-negative int that is not a bool (initial AND final after_null).
# ID list: 0+ rows, each exactly one str cell, no duplicate id.
# Text row: 0 or 1 rows, a row exactly 6 cells, each str or None.

def _assert_shape_refused(res: Result, db: Path, read_name: str) -> None:
    """exit 3, no JSON, zero live connects, and the reason names the read. The shared client
    (engramd_client._cells_ok) refuses some primitive cells itself as `owner read failed (unusable_answer)`
    BEFORE the tool sees them; every other malformed shape must be named by the tool's own message."""
    last = res.last_err_line.lower()
    if "owner read failed" in last:
        _assert_owner_failure(res, db, "owner read failed")
    else:
        _assert_owner_failure(res, db, read_name)


BAD_COUNTS = {
    "zero_rows": [], "two_rows": [[0], [999]], "two_cells": [[0, 999]], "zero_cells": [[]],
    "negative": [[-1]], "bool": [[True]], "str": [["3"]], "float": [[1.0]], "none": [[None]],
}


@pytest.mark.parametrize("name", list(BAD_COUNTS))
def test_shape_count_initial_read_must_be_one_row_one_nonnegative_int_cell(homes, monkeypatch, capsys, name):
    """Mutations: drop `>= 0`; accept >1 row; accept >1 cell; accept a missing row/cell -> a class goes red."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    with RegenOwner(home, configured_model(home), read_rows={NULL_COUNT_SQL: BAD_COUNTS[name]}) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    _assert_shape_refused(res, db, "NULL-embedding count")
    assert owner.fills == [] and owner.finishes == [], owner.writes
    assert all(v is None for v in embeddings(db).values())


@pytest.mark.parametrize("bad", [[[-1]], [[0, 999]], [[0], [999]], [], [[True]], [[]], [["3"]], [[None]]])
def test_shape_count_final_after_null_read_must_be_one_row_one_nonnegative_int_cell(
        homes, monkeypatch, capsys, bad):
    """Mira's wire case: the first COUNT is truthful, fills and the finish run, only the LAST COUNT is malformed
    -> exit 3, no JSON (never `after_null: -1` / `0` from a malformed answer)."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    rows = lambda i: [[4]] if i == 0 else bad
    with RegenOwner(home, configured_model(home), read_rows={NULL_COUNT_SQL: rows}) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    _assert_shape_refused(res, db, "NULL-embedding count")
    assert owner.fills and owner.finishes, "vacuous: the run never reached the final count"


BAD_IDS = {
    "two_cells": [["a", "b"]], "zero_cells": [[]], "int": [[1]], "none": [[None]], "duplicate": [["a"], ["a"]],
}


@pytest.mark.parametrize("name", list(BAD_IDS))
def test_shape_id_list_rows_are_one_unique_string_cell_each(homes, monkeypatch, capsys, name):
    """Mutations: drop the 1-cell-per-row check; drop the duplicate check -> red."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    with RegenOwner(home, configured_model(home), read_rows={NULL_IDS_SQL: BAD_IDS[name]}) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    _assert_shape_refused(res, db, "NULL-embedding id")
    assert owner.fills == [], owner.writes
    assert all(v is None for v in embeddings(db).values())


_SIX = ["observation", "a claim", None, None, None, None]
BAD_TEXT_ROWS = {
    "two_rows": [_SIX, _SIX], "five_cells": [_SIX[:5]], "seven_cells": [_SIX + [None]],
    "int_first": [[7] + _SIX[1:]], "list_middle": [_SIX[:3] + [["x"]] + _SIX[4:]],
    "int_last": [_SIX[:5] + [3]],
}


@pytest.mark.parametrize("name", list(BAD_TEXT_ROWS))
def test_shape_text_row_is_zero_or_one_row_of_six_str_or_none_cells(homes, monkeypatch, capsys, name):
    """Mutations: back to fetchone() (accepts 2 rows); drop the str-or-None cell check -> red."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    with RegenOwner(home, configured_model(home), read_rows={TEXT_ROW_SQL: BAD_TEXT_ROWS[name]}) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    _assert_shape_refused(res, db, "embedding-text")
    assert owner.fills == [], owner.writes
    assert all(v is None for v in embeddings(db).values())


def test_shape_control_count_zero_is_nothing_to_do_and_still_finishes(homes, monkeypatch, capsys):
    home, _ = homes
    make_store(home / "knowledge.db", "full", 5)
    set_flag(home)
    with RegenOwner(home, configured_model(home), read_rows={NULL_COUNT_SQL: [[0]]}) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.rc == 0, res.err
    assert owner.fills == [] and len(owner.finishes) == 1
    assert res.json == {"before_null": 0, "embedded": 0, "failed": 0, "after_null": 0}


def test_shape_control_positive_count_and_multi_row_id_list_run_normally(homes, monkeypatch, capsys):
    """Also the guard against a uniform scalar 1x1 check on the id list (the legitimate list has many rows)."""
    home, _ = homes
    db = make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    with RegenOwner(home, configured_model(home)) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.rc == 0, res.err
    assert res.json["before_null"] == 4 and res.json["after_null"] == 0 and owner.fills
    assert all(v is not None for v in embeddings(db).values())


def test_shape_control_empty_id_list_is_nothing_to_fill(homes, monkeypatch, capsys):
    home, _ = homes
    make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    with RegenOwner(home, configured_model(home), read_rows={NULL_IDS_SQL: []}) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.rc == 0, res.err
    assert owner.fills == []


def test_shape_control_vanished_node_empty_text_row_is_skipped_not_failed(homes, monkeypatch, capsys):
    home, _ = homes
    make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    with RegenOwner(home, configured_model(home), read_rows={TEXT_ROW_SQL: []}) as owner:
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.rc == 0, res.err
    assert res.json is not None and res.json["failed"] == 0


def test_shape_control_text_row_with_none_cells_succeeds(homes, monkeypatch, capsys):
    home, _ = homes
    make_store(home / "knowledge.db", "many", 4)
    set_flag(home)
    with RegenOwner(home, configured_model(home), read_rows={TEXT_ROW_SQL: [_SIX]}):
        res = run_tool(monkeypatch, capsys, home, ["--json"])
    assert res.rc == 0, res.err
