"""Contract tests for engram-lesson-tripwire-hook.py's flagged read through the owner,
U6b stage 2c-1 (#3665, part of #2652).

Written BEFORE the implementation (contract-first; project CLAUDE.md, "Contract-first
tests"). Contract: the stage-2c-1 spec plus the design seat's rulings pinned on #3665.
Modelled on U6a's tests/test_stop_hook_owner_read_3664.py and its tests/_fake_owner.py.

  C0  flag absent / false / non-bool / unreadable: today's hook byte for byte (stdout,
      stderr, state and ledgers), `engramd_client` never imported (subprocess + spy).
  (a) ZERO OPENS: flag ON, owner up, a full fire (tripwire hit with a generalization
      chain, prevention lesson, long match text across IDF batch boundaries, in-turn
      recall) opens `knowledge.db` zero times and all four read sites really ran.
  C1  differential at three levels (raw rows per statement, full stdout, state/ledgers),
      flag ON vs OFF on the same store; shown to FAIL by mutating the NEW side. Plus the
      statement-set cell: every statement the hook sent is in the committed registry.
  C2  owner down / rejected / unusable / raising / unimportable / budget exhausted, at
      each of the four sites alone and all together: never a deny, never "no tripwires
      matched", never a store open, one loud notice per window, stamp only after print.
  C4  one shim per fire, one shared budget; a slow or trickling owner cannot stretch it.
  C5  the DEPLOYED (flattened) layout resolves the flag helper and the client.
  C6  another hook's flag alone leaves this hook on the direct path.
  R6  a failed flag helper reads OFF but is reported (stderr line + windowed context line).
  TB  the total-budget pass (#3677) still holds with a long owner notice in the head.

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from _owner_platform import requires_linux_owner
from _fake_owner import FakeOwner, registered_sql, statement_of

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)
_HOOKS_SRC = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
_HOOK_PATH = _HOOKS_SRC / "engram-lesson-tripwire-hook.py"
BASE_REF = "97f2a499"  # origin/dev the stage-2c branch was cut from (the unmodified hook)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402
import engramd_owner_flags as eof  # noqa: E402
import engram_core  # noqa: E402
import engram_idf  # noqa: E402

FLAG_KEY = "lesson_tripwire_reads_via_owner"
SENTINEL = "SENTINEL-3665-c2e1"
COUNT_SQL = "SELECT COUNT(*) FROM nodes WHERE is_current = 1"
VOCAB_SQL = "SELECT term, doc FROM nodes_fts_vocab WHERE term IN (SELECT value FROM json_each(?))"
OWNER_NOTICE_KEY = "tripwire-owner"
FLAG_NOTICE_KEY = "tripwire-owner-flag"
STAMP_NAME = "probe-unknown-notice.json"
COULD_NOT = "could NOT be read"

LONG_WORDS = " ".join(f"lw{i:04d}x" for i in range(300))   # 300 distinct words, ~2.4k chars
FULL_CMD = "echo tripwirepat zebrafoo " + LONG_WORDS
BLOCK_CMD = "echo tripwirepat blockpat zebrafoo"
DAEMON_RESULT = {"match_count": 1, "top_claims": [{"id": "ob_0001", "recall_summary": "stub recall line"}]}


# --- fixtures / helpers --------------------------------------------------------------

def _add(conn, nid, typ, claim, meta="{}", current=1, recall_summary=None, status="active"):
    conn.execute(
        "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, recall_summary, memory_status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (nid, typ, claim, "2026-01-01T00:00:00", meta if isinstance(meta, str) else json.dumps(meta),
         current, recall_summary, status))


def make_store(home: Path, head_claim: str = "Principle head: verify before acting") -> Path:
    """A real-schema store: a principle head + a high-priority tripwire lesson under it (a
    generalization chain), a shadow and a blocking prevention lesson, JSON-extracted
    anchor/surfacing/scaffolding fields, malformed-metadata and no-pattern rows (the
    json_valid guard), a stale (non-current) lesson, and ~60 filler observations."""
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        # a corrupt blob must be insertable (the #3485 case)
        for trig in ("nodes_metadata_json_guard_insert", "nodes_metadata_json_guard_update"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
        for i in range(60):
            _add(conn, f"ob_{i:04d}", "observation", f"filler{i} common baseline text zebrafoo" if i == 1
                 else f"filler{i} common baseline text")
        _add(conn, "cs_0001", "cornerstone", head_claim, {"anchor_line": "Anchor head line", "situation_pattern": "anchorpat"},
             recall_summary="verify first")
        _add(conn, "ls_0001", "lesson", "tripwire lesson one claim",
             {"situation_pattern": "tripwirepat", "scaffolding_nudge": "Nudge for tripwire one",
              "tripwire_priority": "high"}, recall_summary="lesson one summary")
        _add(conn, "ls_0002", "lesson", "shadow lesson two claim",
             {"situation_pattern": "tripwirepat", "prevention_type": "shadow"})
        _add(conn, "ls_0003", "lesson", "blocking lesson three claim",
             {"situation_pattern": "blockpat", "prevention_type": "blocking", "scaffolding_nudge": "Do not do blockpat"})
        _add(conn, "ls_0004", "lesson", "surfacing lesson four claim",
             {"situation_pattern": "surfpat", "surfacing_nudge": "Surfacing nudge four"})
        _add(conn, "ls_bad", "lesson", "malformed metadata lesson", "{not json at all tripwirepat")
        _add(conn, "ls_nopat", "lesson", "no pattern lesson", {"scaffolding_nudge": "no pattern here"})
        _add(conn, "ls_old", "lesson", "stale lesson", {"situation_pattern": "tripwirepat",
                                                       "scaffolding_nudge": "OLD NUDGE"}, current=0)
        conn.execute("INSERT INTO edges (source_id, target_id, relation, created_at) VALUES "
                     "('cs_0001','ls_0001','generalizes','2026-01-01T00:00:00')")
        engram_idf.ensure_vocab_table(conn)
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


def set_config(home: Path, flag=..., raw=None, extra=None) -> None:
    """flag=... means 'key absent'; flag=None deletes the whole config file."""
    cfg = home / "config.json"
    if raw is not None:
        cfg.write_text(raw)
        return
    if flag is None:
        cfg.unlink(missing_ok=True)
        return
    doc = dict(extra or {})
    if flag is not ...:
        doc["engramd"] = {FLAG_KEY: flag}
    cfg.write_text(json.dumps(doc))


@pytest.fixture(autouse=True)
def _restore_imports():
    """The hook rewrites sys.path and imports `_hooklib` & co. from whichever directory it
    runs in (a flattened tmp copy in some cells). Put sys.modules / sys.path back after every
    cell so no other test file inherits a module resolved from a deleted tmp dir."""
    mods, path = dict(sys.modules), list(sys.path)
    yield
    for name in [n for n in sys.modules if n not in mods]:
        del sys.modules[name]
    for name, mod in mods.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod
    sys.path[:] = path


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="twh-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def load_hook(home: Path, monkeypatch, path: Path = _HOOK_PATH):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location("engram_lesson_tripwire_hook_3665", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._query_recall_daemon = lambda q, top_k=5: DAEMON_RESULT
    return mod


class Run:
    def __init__(self, stdout, stderr, exc):
        self.stdout, self.stderr, self.exc = stdout, stderr, exc

    @property
    def ctx(self) -> str:
        if not self.stdout.strip():
            return ""
        return json.loads(self.stdout).get("hookSpecificOutput", {}).get("additionalContext", "")

    @property
    def denied(self) -> bool:
        if not self.stdout.strip():
            return False
        return json.loads(self.stdout).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def payload(cmd: str) -> dict:
    return {"session_id": "s-3665", "transcript_path": "", "tool_name": "Bash", "tool_input": {"command": cmd}}


def run_main(mod, cmd: str = FULL_CMD) -> Run:
    out, err = io.StringIO(), io.StringIO()
    exc = None
    with patch("sys.stdin", io.StringIO(json.dumps(payload(cmd)))), patch("sys.stdout", out), patch("sys.stderr", err):
        try:
            mod.main()
        except SystemExit as e:
            exc = f"SystemExit({e.code})"
        except Exception as e:  # noqa: BLE001 -- a crash is part of the behaviour under test
            exc = f"{type(e).__name__}: {e}"
    return Run(out.getvalue(), err.getvalue(), exc)


_KEEP = {"knowledge.db", "config.json"}


def reset_state(home: Path) -> None:
    """Remove every file the hook may have written (state, ledgers, stamps, overflow, shown
    records); keep the store, the config and the owner socket directory."""
    for p in sorted(home.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        rel = p.relative_to(home)
        if rel.parts[0] == "run" or rel.as_posix() in _KEEP or rel.as_posix().startswith("knowledge.db"):
            continue
        if p.is_file() or p.is_symlink():
            p.unlink()
        elif p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass


_TIME_KEYS = {"ts", "ts_iso", "last_fire_ts", "shown_at", "now", "at"}


def _strip_times(obj):
    if isinstance(obj, dict):
        return {k: _strip_times(v) for k, v in obj.items() if k not in _TIME_KEYS}
    if isinstance(obj, list):
        return [_strip_times(v) for v in obj]
    return obj


def snapshot(home: Path) -> dict:
    """Every state/ledger file under home (names and contents), time fields stripped."""
    snap = {}
    for p in sorted(home.rglob("*")):
        rel = p.relative_to(home).as_posix()
        if not p.is_file() or rel.split("/")[0] == "run" or rel in _KEEP or rel.startswith("knowledge.db") \
                or rel.endswith(".lock"):
            continue
        text = p.read_text(errors="replace").replace(str(home), "<HOME>")
        key = re.sub(r"\d+-\d+\.txt$", "N-N.txt", rel)
        if rel.endswith(".jsonl"):
            snap[key] = [_strip_times(json.loads(line)) for line in text.splitlines() if line.strip()]
        elif rel.endswith(".json"):
            try:
                snap[key] = _strip_times(json.loads(text))
            except ValueError:
                snap[key] = text
        else:
            snap[key] = text
    return snap


@contextlib.contextmanager
def owner_of(kind: str, home: Path):
    if kind == "fake":
        with FakeOwner(home) as o:
            yield o
    else:
        o = eo.EngramdOwner(home, request_deadline=0.6)
        o.start(install_signal_handlers=False)
        try:
            yield o
        finally:
            o.stop()


OWNER_KINDS = [
    pytest.param("fake", id="fake-owner"),
    pytest.param("real", id="real-owner", marks=requires_linux_owner),
]


class Opens:
    """RAISE-AND-RECORD instrumentation of every way the hook process could open the store.
    Only the hook's own thread is guarded: the in-process fake/real owner serves from other
    threads and must keep its own connections."""

    def __init__(self):
        self.record = []
        self._thread = threading.get_ident()

    def guard(self, name):
        def f(*a, **k):
            if threading.get_ident() != self._thread:
                return self._real[name](*a, **k)
            self.record.append((name, str(a[0]) if a else ""))
            raise AssertionError(f"direct open via {name}")
        return f

    def install(self, monkeypatch, mod):
        hl = sys.modules["_hooklib"]
        self._real = {
            "sqlite3.connect": sqlite3.connect,
            "sqlite3.dbapi2.connect": sqlite3.dbapi2.connect,
            "_hooklib.connect_regular_ro": hl.connect_regular_ro,
            "hook.connect_regular_ro": mod.connect_regular_ro,
        }
        monkeypatch.setattr(sqlite3, "connect", self.guard("sqlite3.connect"))
        monkeypatch.setattr(sqlite3.dbapi2, "connect", self.guard("sqlite3.dbapi2.connect"))
        monkeypatch.setattr(hl, "connect_regular_ro", self.guard("_hooklib.connect_regular_ro"))
        monkeypatch.setattr(mod, "connect_regular_ro", self.guard("hook.connect_regular_ro"))
        return self


def sql_set_of(sites) -> set:
    doc = json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())
    return {t["sql"] for t in doc["templates"] if any(s in t["sites"] for s in sites)}


CHAIN_SITES = ("engram_core.py:generalization_chain", "engram_core.py:_resolve_to_current",
               "engram_core.py:_current_successor")
# #3740 Cut A: with the flag ON the chain walk travels as ONE typed `chain` request per hit
# (recorded by `statement_of` at method level as "chain"); the per-hop statements of CHAIN_SITES
# are sent only by the direct path.
CHAIN_OP = "chain"


def site_sql(mod) -> dict:
    return {
        "load_tripwires": {mod._QUERY},
        "load_prevention_lessons": {mod._PREVENTION_QUERY},
        "render_triggered_principles": {CHAIN_OP},
        "check_in_turn_recall": {COUNT_SQL, VOCAB_SQL},
    }


SITES = ["load_tripwires", "load_prevention_lessons", "render_triggered_principles", "check_in_turn_recall"]
AGENT_STEP = "AGENT-STEP-MARKER-3665"
HUMAN_STEP = "HUMAN-STEP-MARKER-3665"


def failing_read(kind: str, sqls: set, home: Path):
    """An `ec.read` replacement that fails with `kind` for the statements in `sqls` and is
    the real client for every other statement."""
    real = ec.read

    def fake(method, params, **k):
        if statement_of(method, params)[0] not in sqls:
            return real(method, params, **k)
        if kind == "unavailable":
            return ec.Unavailable(socket_path=str(home / "run" / "engramd.sock"), bound=1.0,
                                  agent_steps=[AGENT_STEP], human_steps=[HUMAN_STEP], detail="no answer")
        if kind == "rejected":
            return ec.Rejected(reason="unregistered_template", detail="not registered",
                               remedy="register the template and restart the owner")
        if kind == "unusable":
            return ec.Ok(result="garbage")
        if kind == "raising":
            raise ValueError(f"boom {SENTINEL}")
        raise AssertionError(kind)
    return fake


def stamp(home: Path) -> dict:
    p = home / STAMP_NAME
    return json.loads(p.read_text())["shown_at"] if p.exists() else {}


def rewind_stamp(home: Path, key: str, seconds: float = 2000.0) -> None:
    doc = {"shown_at": {key: time.time() - seconds}}
    (home / STAMP_NAME).write_text(json.dumps(doc))


def assert_loud_notice(ctx: str, site: str | None = None):
    assert COULD_NOT in ctx, ctx
    assert re.search(r'NOT\s+"?no tripwires matched', ctx), ctx
    assert "Agent steps" in ctx and "Human steps" in ctx, ctx
    assert ctx.index("Agent steps") < ctx.index("Human steps")
    assert "lesson_tripwire_reads_via_owner" in ctx and "knowledge.db" in ctx
    if site:
        assert site in ctx


# --- subprocess runner (real process, same driver shape as test_lesson_tripwire_total_budget_3677) ----

RUNNER = r'''
import importlib.util, io, json, os, sqlite3, sys
hook_path, scen_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
scen = json.load(open(scen_path))
opens = []
def _guard(name):
    def f(*a, **k):
        opens.append(name)
        raise AssertionError("direct open via " + name)
    return f
if scen.get("forbid_opens"):
    sqlite3.connect = _guard("sqlite3.connect")
spec = importlib.util.spec_from_file_location("tw_under_test", hook_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
if scen.get("forbid_opens"):
    mod.connect_regular_ro = _guard("hook.connect_regular_ro")
    sys.modules["_hooklib"].connect_regular_ro = _guard("_hooklib.connect_regular_ro")
RESULT = scen["daemon"]
mod._query_recall_daemon = lambda q, top_k=5: RESULT
if scen.get("idf_batch"):
    rd = mod._resolve_runtime_dir()
    if rd not in sys.path:
        sys.path.insert(0, rd)
    import engram_idf
    engram_idf.IDF_BATCH_TERMS = scen["idf_batch"]
sys.stdin = io.StringIO(json.dumps(scen["payload"]))
buf = io.StringIO()
real_out = sys.stdout
sys.stdout = buf
rc = 0
try:
    mod.main()
except SystemExit as e:
    rc = e.code or 0
sys.stdout = real_out
json.dump({"stdout": buf.getvalue(), "rc": rc, "opens": opens,
           "client_imported": "engramd_client" in sys.modules,
           "core_imported": "engram_core" in sys.modules,
           "flags_imported": "engramd_owner_flags" in sys.modules}, open(out_path, "w"))
'''


def run_subprocess(hook_path: Path, home: Path, tmp: Path, cmd: str = FULL_CMD, forbid_opens=True,
                   idf_batch=None, cwd=None):
    runner = tmp / "runner3665.py"
    runner.write_text(RUNNER)
    scen = tmp / "scen3665.json"
    scen.write_text(json.dumps({"payload": payload(cmd), "forbid_opens": forbid_opens,
                                "daemon": DAEMON_RESULT, "idf_batch": idf_batch}))
    out = tmp / "out3665.json"
    out.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    r = subprocess.run([sys.executable, str(runner), str(hook_path), str(scen), str(out)],
                       capture_output=True, text=True, env=env, cwd=str(cwd or tmp), timeout=60)
    assert out.exists(), (r.returncode, r.stdout, r.stderr)
    return r, json.loads(out.read_text())


def flattened_root(tmp: Path) -> Path:
    """The deployed plugin layout: <root>/hooks/*.py (flat) beside the runtime modules."""
    root = tmp / "engram-plugin"
    (root / "hooks").mkdir(parents=True)
    for p in _HOOKS_SRC.glob("*.py"):
        shutil.copy(p, root / "hooks" / p.name)
    for p in Path(_SRC_ENGRAM).glob("*.py"):
        shutil.copy(p, root / p.name)
    # engram_core imports `tools.*` (the deployed plugin ships tools/ at its root)
    shutil.copytree(REPO_ROOT / "tools", root / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    return root


def spying_read(monkeypatch, with_method=False):
    """Record (sql, args) of every ec.read (normalised through `statement_of`, so a `df`
    request is recorded as the IDF statement it stands for); with `with_method` record
    (method, sql, args) instead. Delegate to the real one."""
    seen = []
    real = ec.read

    def spy(method, params, **k):
        sql, args = statement_of(method, params)
        seen.append((method, sql, args) if with_method else (sql, args))
        return real(method, params, **k)
    monkeypatch.setattr(ec, "read", spy)
    return seen


# =========================================================================================
# (a) ZERO OPENS -- the cell that matters most
# =========================================================================================

@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_a_flag_on_owner_up_a_full_fire_opens_the_store_zero_times(owner_kind, home, monkeypatch):
    """Flag ON, owner UP, a tool call with (i) a tripwire hit whose lesson has a
    generalization chain, (ii) a prevention lesson, (iii) a LONG match text crossing the
    IDF batch boundary (IDF_BATCH_TERMS patched small), (iv) the in-turn recall path.
    `sqlite3.connect`, `sqlite3.dbapi2.connect`, `_hooklib.connect_regular_ro` and the
    hook's own `connect_regular_ro` RAISE AND RECORD: the record is empty, the output is
    not an error, and the owner saw a statement from each of the four read sites.
    Mutation: leave ANY of the four sites on its direct open (M1 x4, M9 for the chain site)
    -> the record is non-empty (or the site's statement is missing) -> red."""
    make_store(home)
    set_config(home, flag=True)
    monkeypatch.setattr(engram_idf, "IDF_BATCH_TERMS", 40)
    mod = load_hook(home, monkeypatch)
    with owner_of(owner_kind, home):
        seen_m = spying_read(monkeypatch, with_method=True)
        opens = Opens().install(monkeypatch, mod)
        run = run_main(mod)
    seen = [(s, a) for _, s, a in seen_m]
    assert run.exc == "SystemExit(0)", run.exc
    assert opens.record == [], opens.record
    ctx = run.ctx
    assert "Nudge for tripwire one" in ctx and "[triggered principles]" in ctx
    assert "[ENGRAM in-turn recall" in ctx and "stub recall line" in ctx
    assert "UNAVAILABLE" not in ctx and COULD_NOT not in ctx
    sqls = [s for s, _ in seen]
    for site, wanted in site_sql(mod).items():
        assert wanted & set(sqls), f"the {site} site never reached the owner"
    assert sqls.count(VOCAB_SQL) >= 2, "the long match text did not cross an IDF batch boundary"
    # every statement but the IDF batch (typed `df`) and the chain walk (typed `chain`) is
    # registered; the IDF batch went ONLY as `df`, the chain ONLY as `chain`
    assert all(s in registered_sql() for s in sqls if s not in (VOCAB_SQL, CHAIN_OP))
    assert {m for m, s, _ in seen_m if s == VOCAB_SQL} == {"df"}
    assert {m for m, s, _ in seen_m if s == CHAIN_OP} == {"chain"}


def test_a_the_same_zero_opens_cell_as_a_real_process(home, tmp_path):
    """The same fire as a REAL subprocess (runner patches the opens in-process: no owner
    thread there, so every open is the hook's). Fake owner serves from this process.
    Mutation: as the in-process cell -> `opens` non-empty / rc not 0 -> red."""
    make_store(home)
    set_config(home, flag=True)
    with FakeOwner(home) as owner:
        r, out = run_subprocess(_HOOK_PATH, home, tmp_path, idf_batch=40)
    assert out["rc"] == 0 and out["opens"] == [], (out, r.stderr)
    assert "Nudge for tripwire one" in json.loads(out["stdout"])["hookSpecificOutput"]["additionalContext"]
    sqls = {statement_of(q["method"], q.get("params"))[0] for q in owner.requests} - {None}
    assert VOCAB_SQL in sqls and COUNT_SQL in sqls


# =========================================================================================
# S6 -- the flag-ON path never consults the local store file
# =========================================================================================

def _point_loader_at_missing_store(monkeypatch, mod, site: str, home: Path) -> Path:
    """`main()` calls each loader with no argument, so the path it would open directly is the
    function's `db_path` default. Re-point that default (for ONE site) at a file that does
    not exist while the owner keeps serving the real store in `home`."""
    missing = home / "nowhere" / "knowledge.db"
    assert not missing.exists() and (home / "knowledge.db").exists()
    monkeypatch.setattr(getattr(mod, site), "__defaults__", (missing,))
    return missing


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
@pytest.mark.parametrize("site,cmd,needle", [
    ("load_tripwires", FULL_CMD, "Nudge for tripwire one"),
    ("load_prevention_lessons", BLOCK_CMD, "blockpat"),
], ids=["load_tripwires", "load_prevention_lessons"])
def test_s6_flag_on_the_hook_ignores_a_missing_local_store_file(site, cmd, needle, owner_kind, home, monkeypatch):
    """Flag ON, owner up and serving a populated store, but the path this loader would open
    directly has NO knowledge.db: the block is still delivered (tripwire nudge / blocking
    deny), exit 0, empty stderr, no store open, no owner notice. The flag-on branch must not
    consult the local file.
    Mutation: a `db_path.exists()` guard (return []) on the flag-ON branch of the named
    loader -> that loader's result is empty -> the nudge / the deny is missing -> red.
    Anti-vacuity: the same re-pointing with the flag OFF yields the empty result."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    _point_loader_at_missing_store(monkeypatch, mod, site, home)
    with owner_of(owner_kind, home):
        opens = Opens().install(monkeypatch, mod)
        run = run_main(mod, cmd)
    assert run.exc == "SystemExit(0)", run.exc
    assert run.stderr == "", run.stderr
    assert opens.record == [], opens.record
    assert COULD_NOT not in run.ctx and "UNAVAILABLE" not in run.ctx, run.ctx
    if site == "load_tripwires":
        assert needle in run.ctx, run.ctx
        assert not run.denied
    else:
        assert run.denied, run.stdout
        assert needle in run.stdout, run.stdout


@pytest.mark.parametrize("site,cmd", [("load_tripwires", FULL_CMD), ("load_prevention_lessons", BLOCK_CMD)],
                         ids=["load_tripwires", "load_prevention_lessons"])
def test_s6_control_flag_off_with_the_file_missing_is_the_empty_result(site, cmd, home, monkeypatch):
    """Anti-vacuity for the S6 cells: with the flag OFF the missing file IS consulted and the
    loader returns nothing, so the S6 cells' success comes from the owner, not from a path
    the hook could have read anyway.
    Mutation: none needed (it pins the direct path's own existence guard) -> if the S6
    re-pointing did not take effect this goes red."""
    make_store(home)
    set_config(home, flag=False)
    mod = load_hook(home, monkeypatch)
    missing = _point_loader_at_missing_store(monkeypatch, mod, site, home)
    assert getattr(mod, site)() == []
    assert not missing.exists()


# (f) #3740 Cut B -- ONE current-node COUNT per fire, on both paths
# =========================================================================================

@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_f_flag_on_a_full_fire_sends_exactly_one_count_to_the_owner(owner_kind, home, monkeypatch):
    """#3740 Cut B: check_in_turn_recall counts current nodes and passes the count to
    extract_keywords, so a full flag-ON fire sends the COUNT statement exactly once.
    Mutation: the hook stops passing n_docs -> two COUNT requests -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with owner_of(owner_kind, home):
        seen = spying_read(monkeypatch)
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    assert "[ENGRAM in-turn recall" in run.ctx, "vacuous: the recall site did not run"
    sqls = [s for s, _ in seen]
    assert VOCAB_SQL in sqls
    assert sqls.count(COUNT_SQL) == 1, sqls.count(COUNT_SQL)


def test_f_flag_off_a_full_fire_executes_the_count_exactly_once(home, monkeypatch):
    """#3740 Cut B, direct path: the COUNT runs once on the hook's own connection.
    Mutation: the hook stops passing n_docs -> two COUNT executions -> red."""
    make_store(home)
    set_config(home, flag=None)
    mod = load_hook(home, monkeypatch)
    statements = []
    real = mod.connect_regular_ro

    def traced(*a, **k):
        c = real(*a, **k)
        c.set_trace_callback(statements.append)
        return c
    monkeypatch.setattr(mod, "connect_regular_ro", traced)
    run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    assert "[ENGRAM in-turn recall" in run.ctx, "vacuous: the recall site did not run"
    assert any("json_each" in s for s in statements), "vacuous: the IDF batch never ran"
    assert statements.count(COUNT_SQL) == 1, statements.count(COUNT_SQL)


EXPLICIT_MIN_IDF = {"auto_surface": {"in_turn_recall": {"min_idf": 0.1}}}


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_f_explicit_min_idf_flag_on_a_full_fire_sends_exactly_one_count(owner_kind, home, monkeypatch):
    """#3740 Cut B: with an explicit in-turn-recall `min_idf` in config the hook has no count of
    its own and passes n_docs=None, so the single COUNT happens inside extract_keywords: still
    exactly one COUNT request per fire, and the recall block is still emitted.
    Mutations: the hook passes n_docs=0 -> no recall (red); the hook also counts itself -> two
    COUNTs (red)."""
    make_store(home)
    set_config(home, flag=True, extra=EXPLICIT_MIN_IDF)
    mod = load_hook(home, monkeypatch)
    assert mod._in_turn_config()["min_idf"] == 0.1, "vacuous: the explicit min_idf was not read"
    with owner_of(owner_kind, home):
        seen = spying_read(monkeypatch)
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    assert "[ENGRAM in-turn recall" in run.ctx, "vacuous: the recall site did not run"
    sqls = [s for s, _ in seen]
    assert VOCAB_SQL in sqls
    assert sqls.count(COUNT_SQL) == 1, sqls.count(COUNT_SQL)


def test_f_explicit_min_idf_flag_off_a_full_fire_executes_the_count_exactly_once(home, monkeypatch):
    """#3740 Cut B, direct path, explicit `min_idf`: the one COUNT runs inside extract_keywords.
    Mutations: n_docs=0 -> no recall (red); the hook also counts itself -> two COUNTs (red)."""
    make_store(home)
    set_config(home, flag=..., extra=EXPLICIT_MIN_IDF)
    mod = load_hook(home, monkeypatch)
    assert mod._in_turn_config()["min_idf"] == 0.1, "vacuous: the explicit min_idf was not read"
    statements = []
    real = mod.connect_regular_ro

    def traced(*a, **k):
        c = real(*a, **k)
        c.set_trace_callback(statements.append)
        return c
    monkeypatch.setattr(mod, "connect_regular_ro", traced)
    run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    assert "[ENGRAM in-turn recall" in run.ctx, "vacuous: the recall site did not run"
    assert any("json_each" in s for s in statements), "vacuous: the IDF batch never ran"
    assert statements.count(COUNT_SQL) == 1, statements.count(COUNT_SQL)


# =========================================================================================
# C1 differential
# =========================================================================================

C1_COMMANDS = [
    ("tripwire+chain+prevention+recall", FULL_CMD),
    ("anchor_line", "echo anchorpat zebrafoo"),
    ("surfacing_nudge", "echo surfpat"),
    ("nothing-matches", "echo nothing matches here at all"),
]


def _direct_rows(db: Path, sql: str, args):
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return [list(r) for r in conn.execute(sql, tuple(args or ()))]
    finally:
        conn.close()


def _direct_chain_for(db: Path, args):
    """The raw-level oracle for a typed `chain` answer: engram_core.generalization_chain run
    directly on the same store, JSON-normalised (what the owner's wire carries)."""
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return json.loads(json.dumps(engram_core.generalization_chain(conn, args[0], args[1])))
    finally:
        conn.close()


def _levels(home, monkeypatch, cmd, owner_kind, mutate_answer=None):
    """Flag OFF (direct) vs flag ON (owner) on the SAME store -> {level: (old, new)}."""
    mod = load_hook(home, monkeypatch)
    set_config(home, flag=False)
    reset_state(home)
    r_off = run_main(mod, cmd)
    s_off = snapshot(home)
    set_config(home, flag=True)
    reset_state(home)
    answers = []
    real = ec.read

    def recording(method, params, **k):
        res = real(method, params, **k)
        if isinstance(res, ec.Ok):
            answers.append((*statement_of(method, params), res.result))
        return res

    with owner_of(owner_kind, home):
        monkeypatch.setattr(ec, "read", recording)
        r_on = run_main(mod, cmd)
        monkeypatch.setattr(ec, "read", real)
    s_on = snapshot(home)
    raw_old = [(sql, _direct_chain_for(home / "knowledge.db", args) if sql == CHAIN_OP
                else _direct_rows(home / "knowledge.db", sql, args)) for sql, args, _ in answers]
    raw_new = [(sql, res) for sql, _, res in answers]
    return {
        "raw": (raw_old, raw_new),
        "stdout": (r_off.stdout, r_on.stdout),
        "state": (s_off, s_on),
        "stderr": (r_off.stderr, r_on.stderr),
    }, answers


def differing(levels: dict) -> set:
    return {k for k, (a, b) in levels.items() if a != b}


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
@pytest.mark.parametrize("name,cmd", C1_COMMANDS, ids=[c[0] for c in C1_COMMANDS])
def test_c1_owner_and_direct_agree_on_rows_stdout_and_state(name, cmd, owner_kind, home, monkeypatch):
    """Three levels on the same store, over fixtures with malformed `metadata` rows (excluded
    by the json_valid guard in both paths), JSON-extracted situation_pattern /
    scaffolding_nudge / anchor_line / surfacing_nudge / tripwire_priority, a stale lesson, a
    shadow and a blocking prevention lesson. Anti-vacuity: the owner really answered, and the
    non-trivial fixtures produce output. Mutation: any NEW-side divergence (see the
    controls below) -> the matching level goes red."""
    monkeypatch.setattr(engram_idf, "IDF_BATCH_TERMS", 40)
    make_store(home)
    levels, answers = _levels(home, monkeypatch, cmd, owner_kind)
    assert differing(levels) == set(), {k: v for k, v in levels.items() if v[0] != v[1]}
    assert len(answers) >= 3, "the owner was never asked: the harness would pass vacuously"
    if name == "tripwire+chain+prevention+recall":
        assert "Nudge for tripwire one" in levels["stdout"][1] and "OLD NUDGE" not in levels["stdout"][1]
    if name == "anchor_line":
        assert "Anchor head line" in levels["stdout"][1]
    if name == "surfacing_nudge":
        assert "Surfacing nudge four" in levels["stdout"][1]


def _drop_vocab_table(home: Path) -> None:
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("DROP TABLE nodes_fts_vocab")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_c1_vocab_table_absent_flag_on_is_byte_identical_to_flag_off(owner_kind, home, monkeypatch):
    """Design seat ruling (U6b follow-up): `vocab_table_absent` is a fact about the graph, not an
    owner failure. With nodes_fts_vocab dropped, flag-ON stdout, stderr and state equal flag-OFF
    (Sol's parity method), the tripwire block still prints, and no 'ENGRAM UNAVAILABLE' / loud
    owner notice appears. Anti-vacuity: the owner really got (and refused) a `df` request, and
    the control cell above (vocab present) shows the same command produces the same block.
    Mutation M_NO_MAP (shim raises on vocab_table_absent) -> the recall site's catch still
    empties the recall, but the owner notice lands in stdout -> red."""
    monkeypatch.setattr(engram_idf, "IDF_BATCH_TERMS", 40)
    make_store(home)
    _drop_vocab_table(home)
    sent = []
    real = ec.read

    def spy(method, params, **k):
        res = real(method, params, **k)
        sent.append((method, res))
        return res

    monkeypatch.setattr(ec, "read", spy)
    levels, _answers = _levels(home, monkeypatch, FULL_CMD, owner_kind)
    assert differing(levels) == set(), {k: v for k, v in levels.items() if v[0] != v[1]}
    out_off, out_on = levels["stdout"]
    assert "Nudge for tripwire one" in out_on and "Nudge for tripwire one" in out_off
    for text in (out_on, levels["stderr"][1]):
        assert "ENGRAM UNAVAILABLE" not in text and COULD_NOT not in text, text
    absent = [m for m, r in sent if m == "df" and isinstance(r, ec.Rejected) and r.reason == "vocab_table_absent"]
    assert absent, "the owner was never asked for df: the cell would pass vacuously"


def _mutated_answer(extra_row):
    original = FakeOwner._answer

    def answer(self, request):
        res = original(self, request)
        sql = (request.get("params") or {}).get("sql", "")
        if res.get("status") == "ok" and sql.lstrip().startswith("SELECT\n    id,\n    COALESCE("):
            res = dict(res, result=list(res["result"]) + [extra_row])
        return res
    return answer


def test_c1_control_null_pattern_row_from_the_owner_turns_the_raw_level_red(home, monkeypatch):
    """REQUIRED mutation on the NEW side: the fake owner's answer to the tripwire statement
    also carries a row whose pattern is NULL (what an owner-path read without the json_valid
    guard would hand back). The Python filter in `load_tripwires` drops such a row, so the
    rendered stdout does NOT change -- only the RAW level (the owner's rows against the same
    statement executed directly) can see it. That is why the raw level exists. The cell
    asserts: raw red, stdout green. Mutation of the harness (compare stdout/state only) ->
    this cell goes green-instead-of-red."""
    make_store(home)
    monkeypatch.setattr(FakeOwner, "_answer", _mutated_answer(["ls_nullpat", "NULL PATTERN NUDGE", None, None]))
    levels, _ = _levels(home, monkeypatch, FULL_CMD, "fake")
    assert "raw" in differing(levels)
    assert "stdout" not in differing(levels)


def test_c1_control_malformed_row_with_a_pattern_from_the_owner_turns_stdout_red(home, monkeypatch):
    """The stronger NEW-side mutation: the owner hands back the malformed-metadata node WITH
    a pattern (a lenient reader salvaging it). The rendered output changes, so BOTH the raw
    and the stdout levels go red. Mutation of the harness (drop either level) -> green."""
    make_store(home)
    monkeypatch.setattr(FakeOwner, "_answer",
                        _mutated_answer(["ls_bad", "SALVAGED MALFORMED NUDGE", "tripwirepat", "normal"]))
    levels, _ = _levels(home, monkeypatch, FULL_CMD, "fake")
    assert {"raw", "stdout"} <= differing(levels)
    assert "SALVAGED MALFORMED NUDGE" in levels["stdout"][1]


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_c1_statement_set_every_sent_statement_is_in_the_registry(owner_kind, home, monkeypatch):
    """Every statement executed in a full flag-ON fire is, byte for byte, an entry of the
    committed registry `engramd_read_templates.json` -- pins 'the owner would accept
    everything the hook sends'. The expected set is also checked from below: the hook's own
    statements are each present. Mutation: edit one SQL string in the hook (a stray space)
    -> the statement is not registered -> red. The IDF batch statement (VOCAB_SQL) is the one
    exception to the registry: it travels ONLY as the typed `df` op (recorded here as the
    statement it stands for) and must never appear as a `read`; the chain walk is the other
    (typed `chain`, recorded at method level). Mutation: route the IDF
    statement via `read` -> ("read", VOCAB_SQL) is seen -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    doc = json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())
    registry = {t["sql"] for t in doc["templates"]}
    with owner_of(owner_kind, home):
        seen = spying_read(monkeypatch, with_method=True)
        run = run_main(mod)
    assert run.exc == "SystemExit(0)"
    sent = {s for _, s, _ in seen}
    typed = {VOCAB_SQL, CHAIN_OP}
    assert sent - typed <= registry, sorted(sent - typed - registry)
    assert ("df", VOCAB_SQL) in {(m, s) for m, s, _ in seen}
    assert ("read", VOCAB_SQL) not in {(m, s) for m, s, _ in seen}
    # #3740: the chain walk is the typed `chain` op, recorded at method level; none of the
    # walk's per-hop statements is sent as a `read` any more
    assert ("chain", CHAIN_OP) in {(m, s) for m, s, _ in seen}
    assert not {s for m, s, _ in seen if m == "read"} & sql_set_of(CHAIN_SITES)
    assert {mod._QUERY, mod._PREVENTION_QUERY, COUNT_SQL, VOCAB_SQL, CHAIN_OP} <= sent


# =========================================================================================
# C0 -- flag OFF is today's hook
# =========================================================================================

def _off_states():
    return [
        ("no-config-file", dict(flag=None)),
        ("config-without-engramd", dict(raw=json.dumps({"cadence": {}}))),
        ("engramd-without-key", dict(raw=json.dumps({"engramd": {}}))),
        ("false", dict(flag=False)),
        ("string-true", dict(flag="true")),
        ("one", dict(flag=1)),
        ("null", dict(raw=json.dumps({"engramd": {FLAG_KEY: None}}))),
        ("malformed-json", dict(raw="{nope")),
    ]


def _run_off_state(home, monkeypatch, state_kwargs, cmd):
    mod = load_hook(home, monkeypatch)
    reset_state(home)
    set_config(home, **state_kwargs)
    run = run_main(mod, cmd)
    return run, snapshot(home)


@pytest.mark.parametrize("state", [s[0] for s in _off_states()])
@pytest.mark.parametrize("cmd", [FULL_CMD, BLOCK_CMD], ids=["fire", "deny"])
def test_c0_every_off_state_is_one_outcome_and_never_touches_the_client(state, cmd, home, monkeypatch):
    """Every OFF state (absent, false, string, 1, null, malformed config, no file) gives the
    same stdout, stderr, state files and ledgers as a plain flag-less run on the same store,
    and the client seam is never used (poisoned: any use fails the test). The `deny` variant
    covers the blocking path (stdout is a deny, as today). Mutation: branch on anything but
    the literal `true`, or import the client on the OFF path -> red."""
    make_store(home)
    monkeypatch.setattr(ec, "OwnerReadConnection",
                        lambda *a, **k: pytest.fail("OwnerReadConnection used with the flag OFF"))
    monkeypatch.setattr(ec, "read", lambda *a, **k: pytest.fail("ec.read used with the flag OFF"))
    ref, ref_snap = _run_off_state(home, monkeypatch, dict(flag=None), cmd)
    run, snap = _run_off_state(home, monkeypatch, dict(_off_states())[state], cmd)
    assert run.exc == ref.exc == "SystemExit(0)"
    assert (run.stdout, snap) == (ref.stdout, ref_snap)
    assert "owner" not in run.stderr.lower()
    assert ("permissionDecision" in run.stdout) is (cmd == BLOCK_CMD)


def _base_hook_source():
    r = subprocess.run(["git", "show", f"{BASE_REF}:src/engram/hooks/claude/engram-lesson-tripwire-hook.py"],
                       capture_output=True, text=True, cwd=str(REPO_ROOT))
    return r.stdout if r.returncode == 0 and r.stdout else None


@pytest.mark.parametrize("cmd", [FULL_CMD, BLOCK_CMD, "echo nothing"], ids=["fire", "deny", "silent"])
def test_c0_flag_off_equals_the_unmodified_hook_byte_for_byte(cmd, home, monkeypatch, tmp_path):
    """The flag-OFF hook against the UNMODIFIED hook (`git show 97f2a499:<path>`, loaded
    from a copy in this hook's own directory so its imports resolve): same stdout, stderr,
    state and ledgers on the same store. Skips when the base object is not in the clone.
    Mutation: any behavioural change on the OFF path -> red."""
    src = _base_hook_source()
    if src is None:
        pytest.skip(f"base commit {BASE_REF} not available in this clone")
    make_store(home)
    root = flattened_root(tmp_path)
    base = root / "hooks" / "engram-lesson-tripwire-hook.py"
    base.write_text(src)
    set_config(home, flag=None)
    mod_old = load_hook(home, monkeypatch, base)
    reset_state(home)
    old = run_main(mod_old, cmd)
    old_snap = snapshot(home)
    mod_new = load_hook(home, monkeypatch)
    reset_state(home)
    new = run_main(mod_new, cmd)
    assert (new.stdout, new.stderr, snapshot(home)) == (old.stdout, old.stderr, old_snap)


def test_c0_the_client_is_never_imported_in_a_flag_off_process(home, tmp_path):
    """In a REAL process with the flag absent, `engramd_client` is not in sys.modules after
    the whole fire. Mutation: import the client at module scope or on the OFF path -> red."""
    make_store(home)
    set_config(home, flag=None)
    r, out = run_subprocess(_HOOK_PATH, home, tmp_path, forbid_opens=False)
    assert out["rc"] == 0 and out["client_imported"] is False, (out, r.stderr)
    assert "Nudge for tripwire one" in json.loads(out["stdout"])["hookSpecificOutput"]["additionalContext"]


def test_c0_the_client_and_the_flag_helper_are_not_bound_at_module_scope(home, monkeypatch):
    """No top-level import of `engramd_client` / `engramd_owner_flags` (AST), and neither name
    is bound on the module after load. Mutation: `import engramd_client` at module scope -> red."""
    import ast
    tree = ast.parse(_HOOK_PATH.read_text(encoding="utf-8"))
    top = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            top |= {a.name for a in stmt.names}
        elif isinstance(stmt, ast.ImportFrom):
            top.add(stmt.module)
    assert not ({"engramd_client", "engramd_owner_flags"} & top), top
    make_store(home)
    mod = load_hook(home, monkeypatch)
    assert not hasattr(mod, "engramd_client") and not hasattr(mod, "engramd_owner_flags")


def test_c0_the_flag_is_read_only_through_flag_on_with_this_hooks_key_and_config_path(home, monkeypatch):
    """A recording stand-in for `flag_on` sees exactly (`lesson_tripwire_reads_via_owner`,
    $ENGRAM_HOME/config.json). Mutation: another key or a per-hook config read -> red."""
    make_store(home)
    mod = load_hook(home, monkeypatch)
    assert mod._OWNER_FLAG_KEY == FLAG_KEY
    calls = []

    class Helper:
        @staticmethod
        def flag_on(key, path):
            calls.append((key, str(path)))
            return False

    monkeypatch.setattr(mod, "_import_owner_flags", lambda: Helper, raising=False)
    run_main(mod)
    assert calls == [(FLAG_KEY, str(home / "config.json"))]


@pytest.mark.parametrize("value", ["yes", 1, [1], {"a": 1}, "true"])
def test_c0_only_the_boolean_true_from_the_flag_helper_reads_on(value, home, monkeypatch):
    """A helper whose `flag_on` hands back a truthy non-bool must not read as ON: direct path,
    the owner untouched, a normal result. Mutation (M6): `flag_on(...)` without `is True` -> the
    owner path is taken -> red."""
    make_store(home)
    mod = load_hook(home, monkeypatch)

    class Helper:
        @staticmethod
        def flag_on(key, path):
            return value

    monkeypatch.setattr(mod, "_import_owner_flags", lambda: Helper, raising=False)
    monkeypatch.setattr(ec, "OwnerReadConnection", lambda *a, **k: pytest.fail("owner path taken"))
    run = run_main(mod)
    assert run.exc == "SystemExit(0)" and "Nudge for tripwire one" in run.ctx
    assert mod._OWNER_FLAGS_FAILURE is None


def test_c0_the_boolean_true_from_the_flag_helper_reads_on(home, monkeypatch):
    """GUARD for the cell above (not vacuous): the boolean True reads ON and the owner is used.
    Mutation: ignore the helper's answer -> red."""
    make_store(home)
    mod = load_hook(home, monkeypatch)

    class Helper:
        @staticmethod
        def flag_on(key, path):
            return True

    monkeypatch.setattr(mod, "_import_owner_flags", lambda: Helper, raising=False)
    with FakeOwner(home) as owner:
        run = run_main(mod)
    assert "Nudge for tripwire one" in run.ctx
    assert owner.requests, "the owner was never asked"


def test_c0_standalone_callers_outside_main_stay_on_the_direct_path(home, monkeypatch):
    """`load_tripwires()` / `load_prevention_lessons()` / `render_triggered_principles()` called
    directly (the way the existing suites call them) read the store directly even when the flag
    is ON in config.json: the owner path exists only inside one `main()` run. Mutation: decide
    the owner path from the config at call time -> the poisoned client is used -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(ec, "OwnerReadConnection", lambda *a, **k: pytest.fail("owner used outside main()"))
    monkeypatch.setattr(ec, "read", lambda *a, **k: pytest.fail("owner used outside main()"))
    tw = mod.load_tripwires()
    assert any(t[2] == "ls_0001" for t in tw)
    assert any(p["id"] == "ls_0003" for p in mod.load_prevention_lessons())
    assert "[triggered principles]" in mod.render_triggered_principles([{"node_id": "ls_0001"}])


# =========================================================================================
# C2 -- the owner fails
# =========================================================================================

def _assert_no_deny_no_open(run, opens):
    assert run.exc == "SystemExit(0)", run.exc
    assert not run.denied, run.stdout
    assert opens.record == [], opens.record


def test_c2_anti_vacuity_the_blocking_lesson_denies_on_the_direct_path(home, monkeypatch):
    """The scenario of the C2 cells is real: with the flag OFF, BLOCK_CMD is DENIED by ls_0003.
    Without this the 'no deny on owner failure' cells would pass for a hook that never denies."""
    make_store(home)
    mod = load_hook(home, monkeypatch)
    assert run_main(mod, BLOCK_CMD).denied


@pytest.mark.parametrize("kind", ["unavailable", "rejected", "unusable", "raising"])
@pytest.mark.parametrize("site", SITES)
def test_c2_one_site_failing_alone_is_loud_never_a_deny_and_never_a_store_open(site, kind, home, monkeypatch):
    """Each of the four sites failing ALONE with each kind of owner failure, flag ON, on a call
    a blocking lesson WOULD deny on the direct path: no deny; the notice is present and says
    the tripwires/lessons could NOT be read and that this is NOT 'no tripwires matched'; it
    carries separate Agent steps and Human steps, the flag-off remedy and the failing site;
    the store is never opened; the other sites still ran. Mutations: M1 (fall back to a direct
    open on Unavailable/Rejected at this site) -> opens non-empty; M2 (a failed
    load_prevention_lessons treated as 'no lessons' without recording) -> no notice; M3 (deny on
    owner failure) -> denied."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    sqls = site_sql(mod)[site]
    # a blocking lesson denies only when ITS site (the prevention load) is healthy, so the
    # deny scenario is used where the prevention load is the failing site
    cmd = BLOCK_CMD if site == "load_prevention_lessons" else FULL_CMD
    with FakeOwner(home) as owner:
        monkeypatch.setattr(ec, "read", failing_read(kind, sqls, home))
        opens = Opens().install(monkeypatch, mod)
        run = run_main(mod, cmd)
    _assert_no_deny_no_open(run, opens)
    ctx = run.ctx
    assert_loud_notice(ctx, site)
    if kind == "unavailable":
        assert str(home / "run" / "engramd.sock") in ctx and AGENT_STEP in ctx and HUMAN_STEP in ctx
        assert ctx.index(AGENT_STEP) < ctx.index("Human steps") < ctx.index(HUMAN_STEP)
    if kind == "rejected":
        assert "unregistered_template" in ctx and "register the template and restart the owner" in ctx
    assert SENTINEL not in run.stdout + run.stderr
    # exactly one stderr line about the owner failure
    assert len([ln for ln in run.stderr.splitlines() if "owner read failed" in ln]) == 1, run.stderr
    # the other sites still ran
    other = set().union(*[v for k, v in site_sql(mod).items() if k != site])
    assert other & {statement_of(q["method"], q.get("params"))[0] for q in owner.requests} - {None}


ALL_KINDS = ["unavailable", "rejected", "unusable", "raising", "unimportable", "budget_exhausted"]


def arrange_all_failing(kind, mod, home, monkeypatch):
    if kind == "unimportable":
        empty = home / "empty-runtime"
        empty.mkdir()
        monkeypatch.setenv("ENGRAM_RUNTIME_DIR", str(empty))
        monkeypatch.setitem(sys.modules, "engramd_client", None)
        return
    if kind == "budget_exhausted":
        monkeypatch.setattr(mod, "OWNER_FIRE_BUDGET_S", 0.0)
        return
    everything = set().union(*site_sql(mod).values())
    monkeypatch.setattr(ec, "read", failing_read(kind, everything, home))


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_c2_every_site_failing_is_one_loud_notice_never_a_deny(kind, home, monkeypatch):
    """All four sites down with each kind -- including an unimportable client and an exhausted
    budget. One notice (not one per site), no deny, no store open, the could-not-read wording.
    Mutation: a notice per failed site -> the count assertion fails; M3 -> denied; M1 -> opens."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with FakeOwner(home):
        arrange_all_failing(kind, mod, home, monkeypatch)
        opens = Opens().install(monkeypatch, mod)
        run = run_main(mod, BLOCK_CMD)
    _assert_no_deny_no_open(run, opens)
    assert_loud_notice(run.ctx)
    assert run.ctx.count(COULD_NOT) == 1, run.ctx
    if kind == "unimportable":
        assert "engramd_client" in run.ctx
    assert SENTINEL not in run.stdout + run.stderr


def test_c2_the_notice_is_the_whole_block_when_nothing_matched(home, monkeypatch):
    """Owner down and a call that matches no tripwire: the notice is still emitted (the block
    is the notice), never silence. Mutation: render the notice only when there are hits -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with FakeOwner(home):
        monkeypatch.setattr(ec, "read", failing_read("unavailable", set().union(*site_sql(mod).values()), home))
        run = run_main(mod, "echo nothing matches here at all")
    assert run.exc == "SystemExit(0)"
    assert_loud_notice(run.ctx)


def test_c2_client_failure_shows_the_cause_class_only(home, monkeypatch):
    """A client that raises ValueError(<SENTINEL>): the notice names `ValueError` and never the
    message, SQL text or row data -- stdout or stderr. Mutation (M8): the notice includes the
    exception message -> the sentinel appears -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with FakeOwner(home):
        monkeypatch.setattr(ec, "read", failing_read("raising", set().union(*site_sql(mod).values()), home))
        run = run_main(mod, BLOCK_CMD)
    assert "ValueError" in run.ctx
    assert SENTINEL not in run.stdout + run.stderr
    assert "SELECT" not in run.ctx


def test_c2_window_one_notice_per_window_then_it_returns(home, monkeypatch):
    """The first failing fire prints the notice and stamps `tripwire-owner`; a SECOND failing
    fire inside the window prints no notice on stdout (the stderr line is still written); with
    the stamp rewound past the window the notice returns. Mutation (M4): no windowing ->
    the second fire carries the notice -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with FakeOwner(home):
        monkeypatch.setattr(ec, "read", failing_read("unavailable", set().union(*site_sql(mod).values()), home))
        first = run_main(mod)
        assert COULD_NOT in first.ctx and OWNER_NOTICE_KEY in stamp(home)
        second = run_main(mod)
        assert COULD_NOT not in second.ctx and second.exc == "SystemExit(0)"
        assert "owner read failed" in second.stderr
        rewind_stamp(home, OWNER_NOTICE_KEY)
        third = run_main(mod)
    assert COULD_NOT in third.ctx


def test_c2_the_stamp_is_written_only_after_the_response_was_printed(home, monkeypatch):
    """At the moment the window is stamped, the notice is already on stdout; and a response
    whose flush fails leaves NO stamp (the next failure shows it again). Mutation (M5): stamp
    before the print -> the recorder sees no notice yet / a stamp survives a failed flush -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    sqls = set().union(*site_sql(mod).values())
    seen_at_mark = []
    out = io.StringIO()
    real_mark = mod.mark_probe_notice

    def recording_mark(h, key, now=None):
        if key == OWNER_NOTICE_KEY:
            seen_at_mark.append(out.getvalue())
        return real_mark(h, key, now)

    monkeypatch.setattr(mod, "mark_probe_notice", recording_mark)
    with FakeOwner(home):
        monkeypatch.setattr(ec, "read", failing_read("unavailable", sqls, home))
        with patch("sys.stdin", io.StringIO(json.dumps(payload(FULL_CMD)))), patch("sys.stdout", out), \
                patch("sys.stderr", io.StringIO()):
            with pytest.raises(SystemExit):
                mod.main()
        assert len(seen_at_mark) == 1 and COULD_NOT in seen_at_mark[0]
        # a failed flush: no stamp
        reset_state(home)

        class BadOut(io.StringIO):
            def flush(self):
                raise OSError("pipe closed")

        with patch("sys.stdin", io.StringIO(json.dumps(payload(FULL_CMD)))), patch("sys.stdout", BadOut()), \
                patch("sys.stderr", io.StringIO()):
            with pytest.raises(SystemExit):
                mod.main()
    assert OWNER_NOTICE_KEY not in stamp(home)


def test_c2_the_owner_notice_does_not_use_the_loader_notice_path(home, monkeypatch):
    """Owner failures bypass `_record_loader_failure`: no `lesson-tripwire:` loader stderr line,
    no loader-notice stamp file, the #3503 line text absent. Mutation: record the owner
    failure as a loader failure too (both notices for one failure) -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with FakeOwner(home):
        monkeypatch.setattr(ec, "read", failing_read("unavailable", set().union(*site_sql(mod).values()), home))
        run = run_main(mod)
    assert mod.LOADER_FAILURES == []
    assert "lesson-tripwire: load_" not in run.stderr and "may be missing this prompt" not in run.ctx
    assert not (home / "lesson-tripwire-loader-notice.json").exists()


# =========================================================================================
# C4 -- one shim, one budget, bounded wall time
# =========================================================================================

def test_c4_one_shim_per_fire_is_shared_by_all_four_sites(home, monkeypatch):
    """Exactly ONE OwnerReadConnection is created per fire, with total_budget =
    OWNER_FIRE_BUDGET_S and per_call_timeout = OWNER_READ_TIMEOUT_S, and every site's read goes
    through it. Constants: 2.0 s and 1.0 s. Mutation (M7): a second shim per site -> several
    instances -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    assert (mod.OWNER_FIRE_BUDGET_S, mod.OWNER_READ_TIMEOUT_S) == (2.0, 1.0)
    made = []

    class Spy(ec.OwnerReadConnection):
        def __init__(self, total_budget, per_call_timeout=None, **k):
            made.append((total_budget, per_call_timeout))
            super().__init__(total_budget, per_call_timeout, **k)
            self.n = 0

        def execute(self, sql, params=()):
            self.n += 1
            return super().execute(sql, params)

    monkeypatch.setattr(ec, "OwnerReadConnection", Spy)
    with FakeOwner(home):
        run = run_main(mod)
    assert run.exc == "SystemExit(0)"
    assert made == [(2.0, 1.0)], made


def test_c4_the_shim_is_not_shared_across_fires(home, monkeypatch):
    """Reset per main() run: two fires create two shims (the budget is per fire). Mutation: a
    module-level shim that survives main() -> one instance -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    made = []

    class Spy(ec.OwnerReadConnection):
        def __init__(self, *a, **k):
            made.append(1)
            super().__init__(*a, **k)

    monkeypatch.setattr(ec, "OwnerReadConnection", Spy)
    with FakeOwner(home):
        run_main(mod)
        reset_state(home)
        run_main(mod)
    assert len(made) == 2


def test_c4_a_slow_owner_cannot_stretch_the_fire_past_its_budget(home, monkeypatch):
    """Each reply is delayed so that the sum of all reads exceeds the (shortened) fire budget.
    The whole fire's wall time stays under budget + 1.0 s; later reads are refused with
    `budget_exhausted` (the owner sees fewer requests than a healthy fire); the notice is
    emitted and there is no deny. Mutation (M7): a fresh budget per site, or no total budget
    -> elapsed overruns / the owner serves everything -> red."""
    make_store(home)
    set_config(home, flag=True)
    monkeypatch.setattr(engram_idf, "IDF_BATCH_TERMS", 40)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(mod, "OWNER_FIRE_BUDGET_S", 0.6)
    monkeypatch.setattr(mod, "OWNER_READ_TIMEOUT_S", 0.4)
    # healthy request count for reference
    with FakeOwner(home) as ref_owner:
        run_main(mod)
    healthy = len(ref_owner.requests)
    reset_state(home)
    original = FakeOwner._answer

    def slow(self, request):
        time.sleep(0.25)
        return original(self, request)

    monkeypatch.setattr(FakeOwner, "_answer", slow)
    kinds = []

    class Spy(ec.OwnerReadConnection):
        def execute(self, sql, params=()):
            try:
                return super().execute(sql, params)
            except ec.OwnerReadError as e:
                kinds.append(e.kind)
                raise

    monkeypatch.setattr(ec, "OwnerReadConnection", Spy)
    with FakeOwner(home) as owner:
        start = time.monotonic()
        run = run_main(mod)
        elapsed = time.monotonic() - start
        time.sleep(0.4)  # let delayed handler threads finish while the store still exists
    reads = len(owner.requests)
    assert elapsed < 0.6 + 1.0, elapsed
    assert "budget_exhausted" in kinds, kinds
    assert reads < healthy, (reads, healthy)
    assert not run.denied and COULD_NOT in run.ctx


def test_c4_a_trickling_peer_cannot_extend_the_fire_beyond_its_budget(home, monkeypatch):
    """A peer that dribbles one byte per 100 ms and never finishes a line. The whole fire stays
    inside the fire budget plus a margin (the shim re-arms per call against the REMAINING
    budget), the notice is emitted, nothing is denied. Mutation: pass the per-call timeout to
    each socket op only (no end-to-end bound) -> overrun -> red."""
    make_store(home)
    set_config(home, flag=True)
    (home / "run").mkdir(mode=0o700)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(home / "run" / "engramd.sock"))
    srv.listen(8)
    stop = threading.Event()

    def trickle():
        while not stop.is_set():
            try:
                srv.settimeout(0.1)
                conn, _ = srv.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=_drip, args=(conn,), daemon=True).start()

    def _drip(conn):
        try:
            conn.recv(65536)
            for _ in range(100):
                if stop.is_set():
                    break
                conn.sendall(b"x")
                time.sleep(0.1)
        except OSError:
            pass
        finally:
            conn.close()

    t = threading.Thread(target=trickle, daemon=True)
    t.start()
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(mod, "OWNER_FIRE_BUDGET_S", 0.5)
    monkeypatch.setattr(mod, "OWNER_READ_TIMEOUT_S", 0.3)
    try:
        start = time.monotonic()
        run = run_main(mod, BLOCK_CMD)
        elapsed = time.monotonic() - start
    finally:
        stop.set()
        srv.close()
        t.join(timeout=2)
    assert elapsed < 0.5 + 1.0, elapsed
    assert not run.denied and COULD_NOT in run.ctx


# =========================================================================================
# C5 / C6
# =========================================================================================

def test_c5_flattened_deployed_layout_finds_the_flag_helper_and_the_client(home, tmp_path):
    """The plugin build flattens hooks/claude/* -> hooks/*; runtime modules sit at the plugin
    root. Recreated in a tmp dir with no src/engram ancestor, CLAUDE_PLUGIN_ROOT and
    ENGRAM_RUNTIME_DIR UNSET, flag ON, owner up: the fire is served through the owner (the
    owner saw the hook's statements) and the store is never opened. A hook that could not
    resolve the flag helper reads OFF and opens the store -> red; one that could not resolve
    the client reports the owner failure -> red. Mutation: calibrate a resolver to the source
    layout (fixed parents[N]) -> red."""
    make_store(home)
    set_config(home, flag=True)
    root = flattened_root(tmp_path)
    with FakeOwner(home) as owner:
        r, out = run_subprocess(root / "hooks" / "engram-lesson-tripwire-hook.py", home, tmp_path, idf_batch=40,
                                cwd=tmp_path)
    assert out["rc"] == 0 and out["opens"] == [], (out, r.stderr)
    ctx = json.loads(out["stdout"])["hookSpecificOutput"]["additionalContext"]
    assert "Nudge for tripwire one" in ctx and COULD_NOT not in ctx, ctx
    sqls = {statement_of(q["method"], q.get("params"))[0] for q in owner.requests} - {None}
    assert VOCAB_SQL in sqls and COUNT_SQL in sqls and CHAIN_OP in sqls and len(sqls) >= 5


@pytest.mark.parametrize("other", ["stop_reads_via_owner", "user_identity_reads_via_owner"])
def test_c6_another_hooks_flag_alone_leaves_this_hook_on_the_direct_path(other, home, monkeypatch):
    """With only another hook's `*_via_owner` flag true, this hook reads directly and never
    imports/uses the client. Mutation: gate on `any_owner_flag_on` instead of this key -> the
    poisoned client is used -> red."""
    make_store(home)
    set_config(home, raw=json.dumps({"engramd": {other: True}}))
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(ec, "OwnerReadConnection", lambda *a, **k: pytest.fail("owner path taken"))
    monkeypatch.setattr(ec, "read", lambda *a, **k: pytest.fail("owner path taken"))
    run = run_main(mod)
    assert run.exc == "SystemExit(0)" and "Nudge for tripwire one" in run.ctx


def test_c6_this_key_is_covered_by_the_launchers_via_owner_pattern(home):
    """`any_owner_flag_on` is True with only this hook's key true (the `*_via_owner` pattern, no
    helper change). Mutation: rename the key so the pattern misses it -> red."""
    cfg = home / "config.json"
    cfg.write_text(json.dumps({"engramd": {FLAG_KEY: True}}))
    assert eof.any_owner_flag_on(cfg) is True


# =========================================================================================
# R6 -- a failed flag helper
# =========================================================================================

FLAG_LINE = "engram-lesson-tripwire-hook: owner flag helper failed: "
BREAK_SITES = ["import-TypeError", "import-SyntaxError", "import-SystemExit", "flag_on-OSError",
               "flag_on-SystemExit", "missing"]


def _runtime_with(home: Path, monkeypatch, name: str, source: str) -> Path:
    rt = home / f"bad-{name[:-3]}-runtime"
    rt.mkdir()
    (rt / name).write_text(source)
    monkeypatch.setenv("ENGRAM_RUNTIME_DIR", str(rt))
    monkeypatch.delitem(sys.modules, name[:-3], raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return rt


def _break_helper(site, home, monkeypatch):
    if site == "import-TypeError":
        _runtime_with(home, monkeypatch, "engramd_owner_flags.py", f"raise TypeError('{SENTINEL}')\n")
        return "TypeError"
    if site == "import-SyntaxError":
        _runtime_with(home, monkeypatch, "engramd_owner_flags.py", f"def {SENTINEL.replace('-', '_')}(:\n")
        return "SyntaxError"
    if site == "import-SystemExit":
        _runtime_with(home, monkeypatch, "engramd_owner_flags.py", f"raise SystemExit('{SENTINEL}')\n")
        return "SystemExit"
    if site == "missing":
        monkeypatch.setitem(sys.modules, "engramd_owner_flags", None)
        return "ImportError"
    exc = OSError if site == "flag_on-OSError" else SystemExit

    def boom(key, path):
        raise exc(SENTINEL)

    monkeypatch.setattr(eof, "flag_on", boom)
    return exc.__name__


@pytest.mark.parametrize("site", BREAK_SITES)
def test_r6_a_failed_flag_helper_reads_off_and_is_reported_class_only(site, home, monkeypatch):
    """Flag helper broken at each site. The hook reads flag OFF (direct path, the store opened,
    the client never used), the outcome equals the flag-OFF reference once the report line is
    removed, stderr carries the one class-only line every run, and the SAME line rides in the
    additionalContext once (stamp `tripwire-owner-flag`); the message sentinel appears nowhere.
    Mutations: print the message instead of the class -> sentinel; skip the context line ->
    red; read ON on failure -> client used."""
    make_store(home)
    mod = load_hook(home, monkeypatch)
    ref = run_main(mod)
    ref_ctx = ref.ctx
    reset_state(home)
    set_config(home, flag=True)
    mod2 = load_hook(home, monkeypatch)
    cls = _break_helper(site, home, monkeypatch)
    monkeypatch.setattr(ec, "OwnerReadConnection", lambda *a, **k: pytest.fail("owner path with a broken helper"))
    monkeypatch.setattr(ec, "read", lambda *a, **k: pytest.fail("owner path with a broken helper"))
    run = run_main(mod2)
    assert run.exc == "SystemExit(0)", run.exc
    line = FLAG_LINE + cls
    assert line in run.stderr.splitlines()
    assert line in run.ctx
    assert run.ctx.replace(line + "\n", "", 1) == ref_ctx, (run.ctx, ref_ctx)
    assert "Nudge for tripwire one" in run.ctx
    assert FLAG_NOTICE_KEY in stamp(home)
    assert SENTINEL not in run.stdout + run.stderr
    # second fire inside the window: stderr line still, no context line
    again = run_main(mod2)
    assert line in again.stderr.splitlines() and line not in again.ctx
    rewind_stamp(home, FLAG_NOTICE_KEY)
    assert line in run_main(mod2).ctx


def test_r6_a_failed_flag_helper_never_denies_by_itself_and_a_deny_still_denies(home, monkeypatch):
    """A broken helper adds a report, never a block: the direct path's own decision stands (the
    blocking lesson still denies on the direct path -- the report does not turn a deny into
    something else, and a non-matching call is not denied). Mutation: deny on a failed helper
    -> the non-matching call is denied -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    _break_helper("flag_on-OSError", home, monkeypatch)
    assert run_main(mod, BLOCK_CMD).denied
    assert not run_main(mod, "echo nothing matches here at all").denied


@pytest.mark.parametrize("site", ["import", "flag_on"])
def test_r6_a_keyboardinterrupt_from_the_flag_helper_escapes_the_hook(site, home, monkeypatch):
    """GUARD: KeyboardInterrupt is not a flag-helper failure; it escapes main(). Mutation:
    catch BaseException in the flag read -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    if site == "import":
        _runtime_with(home, monkeypatch, "engramd_owner_flags.py", "raise KeyboardInterrupt\n")
    else:
        def boom(key, path):
            raise KeyboardInterrupt()
        monkeypatch.setattr(eof, "flag_on", boom)
    with pytest.raises(KeyboardInterrupt):
        run_main(mod)


@pytest.mark.parametrize("kind", ["long", "unprintable"])
def test_r6_a_the_class_name_is_sanitised_and_capped_at_80(kind, home, monkeypatch):
    """A 100-char class name is cut to 80; a non-printable char becomes `?`. Mutation: drop the
    cap / the sanitising -> red."""
    make_store(home)
    set_config(home, flag=True)
    name = ("L" * 100) if kind == "long" else "Bad\x07Name\x1b"
    exc_cls = type(name, (Exception,), {})

    def boom(key, path):
        raise exc_cls(SENTINEL)

    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(eof, "flag_on", boom)
    run = run_main(mod)
    expected = ("L" * 80) if kind == "long" else "Bad?Name?"
    assert (FLAG_LINE + expected) in run.stderr.splitlines()


def test_r6_a_a_healthy_helper_is_silent(home, monkeypatch):
    """Healthy helper, flag absent/false: nothing recorded, no report text on either stream,
    no flag stamp. Mutation: report unconditionally -> red."""
    make_store(home)
    for flag in (..., False):
        set_config(home, flag=flag)
        reset_state(home)
        mod = load_hook(home, monkeypatch)
        run = run_main(mod)
        assert mod._OWNER_FLAGS_FAILURE is None
        assert "flag helper" not in run.stdout + run.stderr
        assert FLAG_NOTICE_KEY not in stamp(home)


def test_r6_a_the_failure_record_is_reset_on_every_call(home, monkeypatch):
    """A failing call records the class; the next call with a healthy helper clears it.
    Mutation: never reset -> a stale class is reported on a healthy run -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    real = eof.flag_on
    monkeypatch.setattr(eof, "flag_on", lambda k, p: (_ for _ in ()).throw(OSError("x")))
    assert mod._owner_reads_enabled() is False
    assert mod._OWNER_FLAGS_FAILURE == "OSError"
    monkeypatch.setattr(eof, "flag_on", real)
    mod._owner_reads_enabled()
    assert mod._OWNER_FLAGS_FAILURE is None


# =========================================================================================
# TB -- the total-budget pass (#3677) with a long owner notice in the head
# =========================================================================================

TB_BUDGET = 3000
CLAUSE_RE = re.compile(r"tail cut: (\d+) recall lines, (\d+) chain lines")


@pytest.mark.parametrize("budget", [TB_BUDGET, 2400, 1900])
def test_tb_a_long_owner_notice_plus_chain_plus_recall_still_obeys_the_total_budget(budget, home, monkeypatch):
    """Regression cell: the notice sits in the tripwire block's head (one prevention-site
    failure), a long chain and a multi-line recall follow. The joined additionalContext never
    exceeds the harness budget, and whenever a unit was cut the FIRST line carries the `tail
    cut` marker. The pass itself is unchanged. Mutation: render the notice outside the head
    (bare print / appended after the pass) -> the total exceeds the budget -> red."""
    make_store(home, head_claim="H" * 1400)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(mod, "get_harness_budgets", lambda harness: {"budget": budget})
    mod._query_recall_daemon = lambda q, top_k=5: {
        "match_count": 5,
        "top_claims": [{"id": f"ob_{i:04d}", "recall_summary": "recall line " + "r" * 120} for i in range(1, 6)],
    }
    with FakeOwner(home):
        monkeypatch.setattr(ec, "read", failing_read("unavailable", {mod._PREVENTION_QUERY}, home))
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    ctx = run.ctx
    assert COULD_NOT in ctx, ctx
    assert len(ctx) <= budget, (len(ctx), budget)
    first = ctx.splitlines()[0]
    cut_somewhere = "[triggered principles]" not in ctx or "[ENGRAM in-turn recall" not in ctx \
        or ctx.count("recall line") < 3
    if cut_somewhere:
        assert CLAUSE_RE.search(first), first


def test_tb_a_late_failure_at_the_last_site_keeps_the_total_inside_the_budget(home, monkeypatch):
    """The recall site (the LAST to run) fails after the block, the chain and the sizing were
    done: the notice is added to the block afterwards and the joined context still fits the
    budget (the chain gives way, whole, rather than the total overflowing). Mutation: append
    the late notice without re-checking the budget -> over budget -> red."""
    make_store(home, head_claim="H" * 1400)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(mod, "get_harness_budgets", lambda harness: {"budget": 2300})
    with FakeOwner(home):
        monkeypatch.setattr(ec, "read", failing_read("unavailable", site_sql(mod)["check_in_turn_recall"], home))
        run = run_main(mod)
    assert COULD_NOT in run.ctx and len(run.ctx) <= 2300, len(run.ctx)


# =========================================================================================
# #3740 Cut A -- the generalization chain walk is ONE typed `chain` owner op per hit
# =========================================================================================

def _by_method(seen_m) -> dict:
    out: dict = {}
    for m, _s, _a in seen_m:
        out[m] = out.get(m, 0) + 1
    return out


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_chain_a_fire_with_two_hits_sends_exactly_two_chain_requests_and_no_per_hop_statement(
        owner_kind, home, monkeypatch):
    """Flag ON, the existing fixture: two hits (ls_0001 under cs_0001, and ls_0002, a head with
    no parent). The hook sends exactly TWO `chain` requests (one per hit, default depth) and NONE
    of the walk's per-hop statements: not `SELECT id, type`, not `SELECT is_current`, not `SELECT
    source_id ... generalizes`, not the batched recall_summary / claim reads. The whole fire is
    6 owner requests (3 reads: tripwire load, prevention load, the in-turn recall's ONE COUNT(*)
    since Cut B; 2 chain; 1 df), down from 19 (18 read + 1 df) on dev. Mutation M_HOOK_STILL_PER_HOP (the hook
    keeps walking hop by hop through the shim) -> no `chain` request, per-hop statements
    present, 19 requests -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with owner_of(owner_kind, home):
        seen_m = spying_read(monkeypatch, with_method=True)
        run = run_main(mod)
    assert run.exc == "SystemExit(0)" and "[triggered principles]" in run.ctx
    chains = [a for m, _s, a in seen_m if m == "chain"]
    assert len(chains) == 2, chains
    assert {a[0] for a in chains} == {"ls_0001", "ls_0002"} and {a[1] for a in chains} == {4}
    per_hop = sql_set_of(CHAIN_SITES)
    assert per_hop, "the per-hop statement set must be non-empty or this cell is vacuous"
    sent_sql = {s for m, s, _a in seen_m if m == "read"}
    assert not sent_sql & per_hop, sorted(sent_sql & per_hop)
    for needle in ("SELECT id, type FROM nodes WHERE id = ?", "SELECT is_current FROM nodes WHERE id = ?",
                   "SELECT source_id FROM edges WHERE target_id = ? AND relation = 'generalizes'",
                   "SELECT id, recall_summary FROM nodes WHERE id IN (SELECT value FROM json_each(?))",
                   "SELECT id, claim FROM nodes WHERE id IN (SELECT value FROM json_each(?))"):
        assert needle in per_hop, needle  # the list above really is the old per-hop set
        assert needle not in sent_sql
    assert _by_method(seen_m) == {"read": 3, "chain": 2, "df": 1}, _by_method(seen_m)
    assert len(seen_m) == 6 <= 9


def test_chain_the_hook_asks_the_shim_and_never_calls_the_core_function_with_the_flag_on(home, monkeypatch):
    """With the flag ON the hook calls `conn.generalization_chain(node_id)` on the owner shim;
    `engram_core.generalization_chain(conn, ...)` (the hook-side function object) is never
    called. Flag OFF it is called, with a plain sqlite connection and the shim never exists.
    Mutation: ignore the shim method (always call the core function) -> the flag-ON cell
    sees the core function called with the shim -> red on the ON side."""
    make_store(home)
    mod = load_hook(home, monkeypatch)
    calls = []
    real = engram_core.generalization_chain

    def spy(conn, node_id, max_depth=4):
        calls.append((type(conn).__name__, node_id, max_depth))
        return real(conn, node_id, max_depth)

    mod._GENERALIZATION_CHAIN_IMPORT_ATTEMPTED = True
    mod._GENERALIZATION_CHAIN_FN = spy
    set_config(home, flag=False)
    off = run_main(mod)
    assert [c[1] for c in calls] == ["ls_0001", "ls_0002"]
    assert {c[0] for c in calls} == {"Connection"} and "[triggered principles]" in off.ctx
    calls.clear()
    reset_state(home)
    set_config(home, flag=True)
    with FakeOwner(home):
        on = run_main(mod)
    assert calls == [], calls
    assert "[triggered principles]" in on.ctx
    assert on.stdout == off.stdout


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_chain_an_owner_refusal_of_the_chain_op_is_loud_never_a_deny_never_an_empty_chain(
        owner_kind, home, monkeypatch):
    """The owner refuses the chain op by NAME (chain_result_too_large, the bound lowered so the
    first hit's chain exceeds it): the failure takes the SAME path as any other owner failure --
    one loud notice naming the site and the reason, no deny, no store open, no
    `[triggered principles]` section posing as 'no chain', and the other sites still ran.
    Mutation M_SWALLOW (the hook turns an owner chain failure into an empty chain) -> no notice
    -> red."""
    make_store(home)
    set_config(home, flag=True)
    monkeypatch.setattr(eo, "CHAIN_RESULT_MAX_BYTES", 300)
    import _fake_owner
    monkeypatch.setattr(_fake_owner, "CHAIN_RESULT_MAX_BYTES", 300)
    mod = load_hook(home, monkeypatch)
    with owner_of(owner_kind, home):
        seen_m = spying_read(monkeypatch, with_method=True)
        opens = Opens().install(monkeypatch, mod)
        run = run_main(mod)
    _assert_no_deny_no_open(run, opens)
    ctx = run.ctx
    assert_loud_notice(ctx, "render_triggered_principles")
    assert "chain_result_too_large" in ctx
    assert "[triggered principles]" not in ctx
    assert "Nudge for tripwire one" in ctx, "the tripwire nudge block must not be suppressed"
    assert ctx.count(COULD_NOT) == 1, ctx
    assert any(m == "chain" for m, _s, _a in seen_m)
    assert len([ln for ln in run.stderr.splitlines() if "owner read failed" in ln]) == 1, run.stderr


# =========================================================================================
# #3740 Cut A round 2 -- R1: with the owner shim, the hook does not import engram_core
# =========================================================================================

def _recording_import(mod):
    """Replace the hook's lazy engram_core accessor with a recorder that reports engram_core
    as UNIMPORTABLE (None, the accessor's own failure answer). Returns the call list."""
    calls = []

    def unimportable():
        calls.append(1)
        return None

    mod._import_generalization_chain = unimportable
    return calls


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_r1_a_flag_on_the_principles_block_is_served_by_the_owner_when_engram_core_is_unimportable_in_the_hook(
        owner_kind, home, monkeypatch):
    """Flag ON: the shim's `generalization_chain` serves the walk through the owner, so the
    hook's own engram_core import is dead weight and its failure must not drop the block.
    The accessor reports engram_core unimportable; the `[triggered principles]` block is
    still delivered, with no owner-failure notice, and the accessor is never even called.
    Mutation M_IMPORT_FIRST (the hook imports first and returns "" on None) -> the block is
    missing and the accessor was called -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    calls = _recording_import(mod)
    with owner_of(owner_kind, home):
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    assert "[triggered principles]" in run.ctx and "Principle head" in run.ctx, run.ctx
    assert COULD_NOT not in run.ctx, run.ctx
    assert calls == [], "the hook imported engram_core although the owner shim serves the chain"


def test_r1_b_flag_off_and_engram_core_unimportable_is_the_empty_block_exactly_as_before(home, monkeypatch):
    """Flag OFF (a plain sqlite connection, no shim method): behaviour is unchanged -- an
    unimportable engram_core yields no `[triggered principles]` block, and the accessor IS
    consulted. Control for the cell above: the same recorder, the other connection kind.
    Mutation M_NEVER_IMPORT (skip the import on the direct path too) -> the accessor is not
    called -> red."""
    make_store(home)
    set_config(home, flag=False)
    mod = load_hook(home, monkeypatch)
    calls = _recording_import(mod)
    run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    assert "[triggered principles]" not in run.ctx, run.ctx
    assert "Nudge for tripwire one" in run.ctx
    assert calls, "the direct path must still ask for engram_core"
    # with it importable the same fire does render the block (the cell is not vacuous)
    reset_state(home)
    mod2 = load_hook(home, monkeypatch)
    assert "[triggered principles]" in run_main(mod2).ctx


def test_r1_c_a_flag_on_process_does_not_import_engram_core_and_a_flag_off_process_does(home, tmp_path):
    """A REAL process each: after a flag-ON fire `engram_core` is not in sys.modules (nothing
    else in the hook's import closure imports it) while the block was delivered; the flag-OFF
    fire of the same store does import it (so the check can fail). Mutation M_IMPORT_FIRST ->
    the ON process has engram_core loaded -> red."""
    make_store(home)
    set_config(home, flag=True)
    with FakeOwner(home):
        r, on = run_subprocess(_HOOK_PATH, home, tmp_path, forbid_opens=True)
    assert on["rc"] == 0, (on, r.stderr)
    assert "[triggered principles]" in json.loads(on["stdout"])["hookSpecificOutput"]["additionalContext"]
    assert on["core_imported"] is False, "the flag-ON hook process imported engram_core"
    set_config(home, flag=None)
    r2, off = run_subprocess(_HOOK_PATH, home, tmp_path, forbid_opens=False)
    assert off["rc"] == 0 and off["core_imported"] is True, (off, r2.stderr)


# =========================================================================================
# #3740 Cut A round 2 -- R3: other owner failures at the chain call are loud too
# =========================================================================================

def _chain_failure_is_loud(run, opens, owner_requests):
    _assert_no_deny_no_open(run, opens)
    ctx = run.ctx
    assert_loud_notice(ctx, "render_triggered_principles")
    assert "[triggered principles]" not in ctx, "a failed chain must not read as 'no chain'"
    assert "Nudge for tripwire one" in ctx, "the tripwire nudge block must not be suppressed"
    assert ctx.count(COULD_NOT) == 1, ctx
    assert len([ln for ln in run.stderr.splitlines() if "owner read failed" in ln]) == 1, run.stderr
    other = {statement_of(q["method"], q.get("params"))[0] for q in owner_requests} - {None, CHAIN_OP}
    assert other, "the other sites still ran"


def test_r3_a_new_hook_against_an_older_owner_unknown_operation_is_loud(home, monkeypatch):
    """The owner does not know method `chain` (an older owner): it answers `unknown_operation`.
    The hook takes the loud owner-failure path (naming the reason), never a deny, never an
    empty chain. Expected green on the round-1 code. Mutation M_SWALLOW (an owner failure at
    the chain call becomes an empty chain) -> no notice -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    original = FakeOwner._answer

    def older_owner(self, request):
        if request.get("method") == "chain":
            return {"status": "rejected", "reason": "unknown_operation",
                    "detail": "unknown method 'chain'", "remedy": "upgrade the owner"}
        return original(self, request)

    monkeypatch.setattr(FakeOwner, "_answer", older_owner)
    with FakeOwner(home) as owner:
        seen_m = spying_read(monkeypatch, with_method=True)
        opens = Opens().install(monkeypatch, mod)
        run = run_main(mod)
    assert any(m == "chain" for m, _s, _a in seen_m), "the chain request was never sent"
    _chain_failure_is_loud(run, opens, owner.requests)
    assert "unknown_operation" in run.ctx


def test_r3_b_unavailable_at_the_chain_call_is_loud(home, monkeypatch):
    """The owner is unreachable exactly at the chain call (other reads served). Loud notice with
    the steps, no deny, no empty chain. Expected green on the round-1 code. Mutation M_SWALLOW
    -> no notice -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with FakeOwner(home) as owner:
        monkeypatch.setattr(ec, "read", failing_read("unavailable", {CHAIN_OP}, home))
        opens = Opens().install(monkeypatch, mod)
        run = run_main(mod)
    _chain_failure_is_loud(run, opens, owner.requests)
    assert AGENT_STEP in run.ctx and HUMAN_STEP in run.ctx
    assert not any(q["method"] == "chain" for q in owner.requests)


def test_r3_c_budget_exhausted_at_the_chain_call_is_loud(home, monkeypatch):
    """The fire's time budget runs out exactly at the chain call: the clock the shim reads
    jumps past the budget right after the request that precedes the first chain request. The
    shim sends nothing for the chain and the hook takes the loud owner-failure path (no deny,
    no empty chain). Expected green on the round-1 code. Mutation M_SWALLOW -> red."""
    make_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with FakeOwner(home):
        probe = spying_read(monkeypatch, with_method=True)
        run_main(mod)
    methods = [m for m, _s, _a in probe]
    assert "chain" in methods
    before_chain = methods.index("chain")  # owner requests served before the first chain request
    assert before_chain >= 1
    monkeypatch.undo()
    reset_state(home)
    mod = load_hook(home, monkeypatch)
    offset = [0.0]
    real_clock = time.monotonic
    monkeypatch.setattr(ec, "_monotonic", lambda: real_clock() + offset[0])
    real_read = ec.read
    served = []

    def read_then_jump(method, params, **k):
        out = real_read(method, params, **k)
        served.append(method)
        if len(served) == before_chain:
            offset[0] = 1e6  # the budget is gone before the next shim call
        return out

    monkeypatch.setattr(ec, "read", read_then_jump)
    with FakeOwner(home):
        opens = Opens().install(monkeypatch, mod)
        run = run_main(mod)
    assert "chain" not in served, served
    _assert_no_deny_no_open(run, opens)
    assert_loud_notice(run.ctx, "render_triggered_principles")
    assert "[triggered principles]" not in run.ctx
    assert "budget" in run.ctx.lower(), run.ctx
