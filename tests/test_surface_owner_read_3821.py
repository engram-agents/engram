"""Contract tests for U10 (#3821, part of #2652): engram-surface-hook.py reads the store
through the engramd owner behind `engramd.surface_reads_via_owner` (default OFF).

Written BEFORE the implementation (docs/internal/CONTRACT-FIRST-TESTS.md). Design seat: Borges (issue
#3821). Modelled on U9's tests/test_session_start_owner_read_3735.py and U6b/U8's
tests/test_tripwire_owner_read_3665.py + tests/test_focused_ids_owner_3710.py.

SCOPE. Of the hook's eight store paths (site table of the issue, re-measured on this tip in
`test_site_table_*`), seven move behind the flag: rows 1-5 are READS through the owner, row 6
(`_check_db_liveness`) becomes the owner's `ping` plus one registered store read (amended 13:56Z), row 7 (`query_fts_fallback`, an in-hook
server runtime) is NOT TAKEN with the flag ON. Row 8 (`_advance_person_moment_cursor`, an RW
`sqlite3.connect`) stays DIRECT on both flag values: it is the named remainder and its single
RW open is asserted by frame name, so the remainder is visible, not excused.

  C0  flag OFF is today's hook, byte for byte (stdout, stderr, state) and executes the same
      store opens; default OFF; only the JSON boolean `true` is ON; read only via flag_on.
  C1  differential at EACH READ (raw rows / chain / df keywords / focused ids), then the
      rendered block, owner vs direct on one store; shown to FAIL by mutating the NEW side
      (the owner path drops one dormant-set row: the raw harness reddens while the rendered
      nudge is unchanged).
  C2  zero direct opens of knowledge.db with the flag ON across one full main() fire, with
      in-context exclusion ON and OFF; the opener counter keys on the resolved STORE PATH (a
      pack DB is neither counted nor hidden); a fire that reaches row 8 shows exactly ONE RW
      open, at `_advance_person_moment_cursor`.
  C3  owner down: every migrated read returns U1's typed Unavailable; no stale value, no
      plausible default, no direct fallback, no use of row 7's in-hook fallback.
  C6  row 6: ON, owner `ping` AND one registered store read (three distinct results: healthy /
      owner up but store unreadable / owner down; never fail-open); OFF, today's probe.
  C7  row 7: not taken ON; the existing daemon-down notice and no recall block.

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlparse

import pytest

from _fake_owner import FakeOwner, registered_sql
from _owner_platform import requires_linux_owner  # noqa: F401 -- OWNER_KINDS carries it

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)
_HOOKS_SRC = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
_HOOK_PATH = _HOOKS_SRC / "engram-surface-hook.py"
BASE_REF = "ae55c31a"  # origin/dev the U10 branch was cut from (the unmodified hook)
# (#4271: the owner stderr line may end with `, bound N ms` and/or `; failed E ms into the B ms fire budget`)
OWNER_LINE_TAIL = r"(?:, bound \d+ ms)?(?:; failed \d+ ms into the \d+ ms fire budget)?"

import engramd_client as ec  # noqa: E402
import engram_core  # noqa: E402
import engram_idf  # noqa: E402

from test_tripwire_owner_read_3665 import (  # noqa: E402,F401 -- fixtures and helpers reused
    OWNER_KINDS,
    _add,
    _restore_imports,
    flattened_root,
    home,
    make_store,
    owner_of,
    reset_state,
    snapshot,
)

FLAG_KEY = "surface_reads_via_owner"
NOTICE_KEY = "surface-owner"
SID = "s-3821"
PROMPT = "please check tripwirepat zebrafoo handling of the common baseline filler text in this repository"

SQL_EMBED = "SELECT not_fully_restored, null_count, cause FROM embedding_restore_state WHERE singleton_key = 1"
SQL_ACTIVE = "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
SQL_SETS = "SELECT name, node_ids, last_loaded_at FROM focus_sets"
SQL_FOCUSED = "SELECT id FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1 ORDER BY focused_at"
SQL_COUNT = "SELECT COUNT(*) FROM nodes WHERE is_current = 1"
SQL_VOCAB = "SELECT term, doc FROM nodes_fts_vocab WHERE term IN (SELECT value FROM json_each(?))"
READ_SQLS = {SQL_EMBED, SQL_ACTIVE, SQL_SETS, SQL_FOCUSED, SQL_COUNT, SQL_VOCAB}


# =========================================================================================
# store / config / hook helpers
# =========================================================================================

def make_surface_store(home: Path, restore: str | None = None) -> Path:
    """make_store (principle head cs_0001 over ls_0001 = a node WITH a chain; ob_0001 = a node
    WITHOUT one) plus: two DORMANT focus sets (`dormant-hit` overlaps rendered nodes,
    `dormant-miss` does not), one LIVE (active) set, a focused node (ob_0002, for the in-context
    exclusion), a person node with special moments, and the embedding-restore row set
    (restore="set") or unset (None / "unset")."""
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    sets = [
        ("dormant-hit", ["ls_0001", "ob_0001", "ob_0009"], "2026-10-01T00:00:00"),
        ("dormant-miss", ["ob_0040", "ob_0041"], "2026-10-02T00:00:00"),
        ("live-set", ["ob_0001", "ob_0002"], "2026-10-03T00:00:00"),
    ]
    for name, ids, loaded in sets:
        conn.execute(
            "INSERT INTO focus_sets (name, node_ids, description, created_at, last_loaded_at) "
            "VALUES (?,?,?,?,?)", (name, json.dumps(ids), "", "2026-09-01T00:00:00", loaded))
    conn.execute("INSERT OR REPLACE INTO focus_state (singleton_key, active_set_name) VALUES (1, 'live-set')")
    conn.execute("UPDATE nodes SET focused_at = '2026-10-10T00:00:00' WHERE id = 'ob_0002'")
    _add(conn, "pn_0001", "person", "Lei", {"special_moment_cursor": 0})
    if restore == "set":
        conn.execute(
            "INSERT OR REPLACE INTO embedding_restore_state "
            "(singleton_key, not_fully_restored, null_count, cause, updated_at) "
            "VALUES (1, 1, 7, 'test-cause', '2026-10-10T00:00:00')")
    elif restore == "unset":
        conn.execute(
            "INSERT OR REPLACE INTO embedding_restore_state "
            "(singleton_key, not_fully_restored, null_count, cause, updated_at) "
            "VALUES (1, 0, 0, NULL, '2026-10-10T00:00:00')")
    conn.commit()
    conn.close()
    return db


def _claim(nid, claim, summary=None):
    return {"id": nid, "type": "observation_factual", "claim": claim, "confidence": 0.85,
            "recall_summary": summary, "recall_keywords": ["kw"], "created_ago": "1h ago"}


def daemon_result(person=False):
    tc = [_claim("ls_0001", "tripwire lesson one claim", "lesson one summary"),
          _claim("ob_0001", "filler1 common baseline text zebrafoo", "filler summary one"),
          _claim("ob_0002", "filler2 common baseline text", "focused summary two")]
    special = []
    if person:
        special = [{"id": "pn_0001", "type": "person", "claim": "Lei", "confidence": 0.9,
                    "recall_summary": "the user", "recall_keywords": ["lei"], "created_ago": "1d ago",
                    "special_moments": [{"description": "moment alpha"}, {"description": "moment beta"}],
                    "special_moment_cursor": 0}]
    ids = [e["id"] for e in special + tc]
    return {"match_count": len(ids), "special_nodes": special, "top_claims": tc, "type_counts": {},
            "age": {"not_recalled_recently": []}, "issues": {}, "matched_ids": ids,
            "matched_meta": [{"id": i, "type": "observation_factual", "recall_keywords": ["kw"]} for i in ids]}


def set_config(home: Path, flag=..., exclusion=False, extra=None, raw=None) -> None:
    """flag=... : key absent; flag=None : no config file at all; raw : verbatim file text."""
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
    if exclusion:
        doc["recall_in_context_exclusion"] = {"enabled": True}
    cfg.write_text(json.dumps(doc))


def load_hook(home: Path, monkeypatch, path: Path = _HOOK_PATH, daemon="up", person=False):
    """Load the surface hook with ENGRAM_HOME/HOME pointed at tmp; the recall daemon is stubbed
    identically on every path (`daemon` "up" -> a fixed result, "down" -> None); the pgrep /
    marker probe and the telemetry emitters are deterministic stubs."""
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    (home / "h").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home / "h"))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    fake_emitter = types.ModuleType("engram_log_emitter")
    fake_emitter.Emitter = types.SimpleNamespace(init=lambda **k: None)
    monkeypatch.setitem(sys.modules, "engram_log_emitter", fake_emitter)
    spec = importlib.util.spec_from_file_location("engram_surface_hook_3821", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = daemon_result(person)
    mod.query_daemon = (lambda *a, **k: copy.deepcopy(result)) if daemon == "up" else (lambda *a, **k: None)
    mod._check_mcp_write_tool_marker = lambda h: (True, None)
    mod.write_telemetry_record = lambda *a, **k: None
    return mod


class Run:
    def __init__(self, stdout, stderr, exc):
        self.stdout, self.stderr, self.exc = stdout, stderr, exc

    @property
    def ctx(self) -> str:
        if not self.stdout.strip():
            return ""
        return json.loads(self.stdout).get("hookSpecificOutput", {}).get("additionalContext", "")


def run_main(mod, prompt: str = PROMPT) -> Run:
    out, err = io.StringIO(), io.StringIO()
    exc = None
    stdin = json.dumps({"prompt": prompt, "session_id": SID, "transcript_path": ""})
    with patch("sys.stdin", io.StringIO(stdin)), patch("sys.stdout", out), patch("sys.stderr", err):
        try:
            mod.main()
        except SystemExit as e:
            exc = f"SystemExit({e.code})"
        except Exception as e:  # noqa: BLE001 -- a crash is part of the behaviour under test
            exc = f"{type(e).__name__}: {e}"
    return Run(out.getvalue(), err.getvalue(), exc)


def owner_run_on(mod) -> None:
    """Put the module into the state main() gives a flag-ON fire (standalone callers of the
    sites are otherwise on the direct path)."""
    mod._OWNER_RUN = {"on": True, "conn": None, "failure": None, "degraded": []}


class StoreOpens:
    """Counts opens of THE STORE (keyed on the resolved path of the file opened, not on the
    caller): `sqlite3.connect` and `sqlite3.dbapi2.connect` are wrapped and pass through, so a
    pack DB (a different file) is neither counted nor hidden, and the one allowed RW open of
    row 8 still works. Only the hook's own thread is counted (a fake/real owner serves from
    others)."""

    def __init__(self, store: Path):
        import threading
        self.store = os.path.realpath(str(store))
        self.opens = []      # [{"rw": bool, "frames": [...]}] for the store
        self.others = []     # resolved paths of every other file opened
        self._thread = threading.get_ident()

    def _resolve(self, arg):
        s = str(arg)
        rw = True
        if s.startswith("file:"):
            u = urlparse(s)
            rw = "mode=ro" not in (u.query or "")
            s = unquote(u.path)
        return os.path.realpath(s), rw

    def install(self, monkeypatch):
        import threading
        real = sqlite3.connect

        def wrapped(*a, **k):
            if threading.get_ident() == self._thread and a:
                path, rw = self._resolve(a[0])
                if path == self.store:
                    frames = []
                    f = sys._getframe(1)
                    while f is not None and len(frames) < 14:
                        frames.append(f.f_code.co_name)
                        f = f.f_back
                    self.opens.append({"rw": rw, "frames": frames})
                elif a[0] != ":memory:":
                    self.others.append(path)
            return real(*a, **k)
        monkeypatch.setattr(sqlite3, "connect", wrapped)
        monkeypatch.setattr(sqlite3.dbapi2, "connect", wrapped)
        return self


@contextlib.contextmanager
def recording(rec: list):
    """Record every store statement and result at the connection boundary, on whichever path:
    the owner shim class (execute + the typed chain op) and the direct connection (a proxy
    returned by the hook's `connect_regular_ro`). Rows are normalised to plain tuples."""
    real_exec, real_chain = ec.OwnerReadConnection.execute, ec.OwnerReadConnection.generalization_chain

    def spy_exec(self, sql, params=()):
        cur = real_exec(self, sql, params)
        rec.append(("owner", sql, list(params), [tuple(r) for r in list(cur._rows)]))
        return cur

    def spy_chain(self, node_id, max_depth=4):
        res = real_chain(self, node_id, max_depth)
        rec.append(("owner", "chain", [node_id], json.loads(json.dumps(res))))
        return res
    with patch.object(ec.OwnerReadConnection, "execute", spy_exec), \
            patch.object(ec.OwnerReadConnection, "generalization_chain", spy_chain):
        yield


class DirectProxy:
    """Wraps a real sqlite3 connection; records execute() results like `recording`."""

    def __init__(self, conn, rec):
        object.__setattr__(self, "_c", conn)
        object.__setattr__(self, "_rec", rec)

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __setattr__(self, name, value):
        setattr(self._c, name, value)

    def execute(self, sql, params=()):
        if sql in READ_SQLS:
            rows = [tuple(r) for r in self._c.execute(sql, params).fetchall()]
            self._rec.append(("direct", sql, list(params), rows))
        return self._c.execute(sql, params)


def fire(mod, home, monkeypatch, flag: bool, prompt=PROMPT, owner_kind="fake", mutate=None):
    """One full main() fire with the flag ON/OFF (in-context exclusion enabled); returns
    (Run, raw) where raw is the list of (path, sql, args, rows) the hook read from the store, taken
    at the connection boundary on whichever path answered: the owner shim class (statements and the
    typed chain op) or a proxy on the hook's direct connections (plus the direct chain walk's
    result). `mutate`, if given, patches the NEW (owner) side only."""
    set_config(home, flag=flag, exclusion=True)
    reset_state(home)
    raw: list = []
    with monkeypatch.context() as m:
        owner = owner_of(owner_kind, home) if flag else contextlib.nullcontext()
        if flag and mutate is not None:
            mutate(m)    # BENEATH the recorder: the raw harness sees the mutated answer
        with owner, recording(raw):
            if not flag:
                real = mod.connect_regular_ro

                def proxy_open(*a, **k):
                    return DirectProxy(real(*a, **k), raw)
                m.setattr(mod, "connect_regular_ro", proxy_open)
                m.setattr(sys.modules["_hooklib"], "connect_regular_ro", proxy_open)
                real_chain = engram_core.generalization_chain

                def spy_chain(conn, node_id, *a, **k):
                    res = real_chain(conn, node_id, *a, **k)
                    raw.append(("direct", "chain", [node_id], json.loads(json.dumps(res))))
                    return res
                m.setattr(engram_core, "generalization_chain", spy_chain)
            run = run_main(mod, prompt)
    return run, raw


def canon(raw):
    """Group the raw reads by statement (order within a statement kept): the two paths differ
    only in WHO answered, so `path` is dropped. The chain op is keyed 'chain'; the direct path's
    per-hop chain statements are not READ_SQLS and are compared through the rendered block."""
    out = {}
    for _path, sql, args, rows in raw:
        out.setdefault(sql, []).append((args, rows))
    return out


# =========================================================================================
# the site table -- re-measured against the file (stated in the ledger and the handoff)
# =========================================================================================

def test_site_table_rows_1_to_8_are_where_the_issue_says_and_row_8_is_the_only_rw_connect():
    """The issue's site table, measured on this tip: rows 1-3, 6 open with connect_regular_ro,
    row 4 (main) too, row 7 builds an in-hook EngramClient, row 8 is the only raw
    `sqlite3.connect` of the file. A drift here means the ledger's site table needs re-measuring.
    Mutation: a new raw sqlite3.connect (a ninth store path) -> the count assertion fails."""
    src = _HOOK_PATH.read_text()
    raw = [ln for ln in src.splitlines() if re.search(r"(?<![\w.])sqlite3\.connect\(", ln) and not ln.lstrip().startswith("#")]
    assert len(raw) == 1 and "KNOWLEDGE_DB_PATH" in raw[0], raw
    import ast
    tree = ast.parse(src)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def calls(fn, name):
        return [c for c in ast.walk(funcs[fn]) if isinstance(c, ast.Call)
                and getattr(c.func, "id", getattr(c.func, "attr", None)) == name]
    for fn in ("_check_db_liveness", "_check_embedding_restore_flag", "_dormant_focus_set_candidates",
               "_render_triggered_principles_section", "_main_impl", "_query_attached_packs"):
        assert calls(fn, "connect_regular_ro"), fn
    assert calls("query_fts_fallback", "EngramClient")
    assert calls("_advance_person_moment_cursor", "connect")


# =========================================================================================
# C0 -- flag OFF: today's hook
# =========================================================================================

def _base_hook_source():
    r = subprocess.run(["git", "show", f"{BASE_REF}:src/engram/hooks/claude/engram-surface-hook.py"],
                       capture_output=True, text=True, cwd=str(REPO_ROOT))
    return r.stdout if r.returncode == 0 and r.stdout else None


@pytest.mark.parametrize("person", [False, True], ids=["plain", "person-moment"])
@pytest.mark.parametrize("restore", ["set", "unset"])
@pytest.mark.parametrize("state", ["absent", "false", "none-config"])
def test_c0_flag_off_equals_the_unmodified_hook_byte_for_byte(state, restore, person, home, monkeypatch, tmp_path):
    """The flag-OFF hook against the UNMODIFIED hook (`git show ae55c31a:<path>`, loaded from a
    flattened copy so its imports resolve): same stdout, stderr and state (ledgers, counters,
    shown records) on the same store, with the restore flag set and unset and with a person
    moment (the RW cursor write is part of the compared state). Skips when the base object is
    not in the clone. Mutation: any behavioural change on the OFF path -> red."""
    src = _base_hook_source()
    if src is None:
        pytest.skip(f"base commit {BASE_REF} not available in this clone")
    make_surface_store(home, restore=restore)
    root = flattened_root(tmp_path)
    base = root / "hooks" / "engram-surface-hook.py"
    base.write_text(src)
    set_config(home, flag={"absent": ..., "false": False, "none-config": None}[state])
    mod_old = load_hook(home, monkeypatch, base, person=person)
    reset_state(home)
    old = run_main(mod_old)
    old_snap = snapshot(home)
    mod_new = load_hook(home, monkeypatch, person=person)
    reset_state(home)
    # the old run advanced the cursor; both runs must start from the same store
    c = sqlite3.connect(str(home / "knowledge.db"))
    c.execute("UPDATE nodes SET metadata = ? WHERE id = 'pn_0001'", (json.dumps({"special_moment_cursor": 0}),))
    c.commit()
    c.close()
    new = run_main(mod_new)
    assert (new.stdout, new.stderr, snapshot(home)) == (old.stdout, old.stderr, old_snap)
    assert old.exc == new.exc == "SystemExit(0)"


_SITE_FRAMES = ("_check_db_liveness", "_check_embedding_restore_flag", "_dormant_focus_set_candidates",
                "_render_triggered_principles_section", "_focused_ids", "_advance_person_moment_cursor",
                "_query_attached_packs", "main", "_main_impl")


def _site_of(frames):
    """The innermost frame that is one of the hook's store sites (`_main_impl` is `main`'s body)."""
    f = next((f for f in frames if f in _SITE_FRAMES), "?")
    return "main" if f == "_main_impl" else f


@pytest.mark.parametrize("person", [False, True], ids=["plain", "person-moment"])
def test_c0_flag_off_executes_the_same_store_opens_as_the_unmodified_hook(person, home, monkeypatch, tmp_path):
    """Code-level: the OFF path makes the same store opens, in the same order, from the same
    functions with the same open mode, as the unmodified hook ((rw, innermost-hook-function)
    sequence). Mutation: any new or removed open on the OFF path (e.g. the flag read opening the
    store, or a probe moved) -> the sequences differ."""
    src = _base_hook_source()
    if src is None:
        pytest.skip(f"base commit {BASE_REF} not available in this clone")
    make_surface_store(home, restore="set")
    root = flattened_root(tmp_path)
    base = root / "hooks" / "engram-surface-hook.py"
    base.write_text(src)
    set_config(home, flag=False)

    def opens_of(path):
        mod = load_hook(home, monkeypatch, path, person=person)
        reset_state(home)
        c = sqlite3.connect(str(home / "knowledge.db"))
        c.execute("UPDATE nodes SET metadata = ? WHERE id = 'pn_0001'", (json.dumps({"special_moment_cursor": 0}),))
        c.commit()
        c.close()
        with monkeypatch.context() as m:
            so = StoreOpens(home / "knowledge.db").install(m)
            run_main(mod)
        return [(o["rw"], _site_of(o["frames"])) for o in so.opens]
    old = opens_of(base)
    new = opens_of(_HOOK_PATH)
    assert old and old == new, (old, new)


def test_c0_the_flag_is_off_by_default_and_the_client_is_not_imported_when_off(home, tmp_path):
    """A REAL process with the flag absent: `engramd_client` is not in sys.modules after a full
    fire. Mutation: importing the client (or opening the owner) on the OFF path -> red."""
    make_surface_store(home)
    set_config(home, flag=...)
    runner = tmp_path / "runner3821.py"
    runner.write_text(RUNNER)
    out = tmp_path / "out3821.json"
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home / "h")
    (home / "h").mkdir(exist_ok=True)
    scen = tmp_path / "scen.json"
    scen.write_text(json.dumps({"daemon": daemon_result(), "prompt": PROMPT}))
    r = subprocess.run([sys.executable, str(runner), str(_HOOK_PATH), str(scen), str(out)],
                       capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60)
    assert out.exists(), (r.stdout, r.stderr)
    res = json.loads(out.read_text())
    assert res["rc"] == 0 and res["client_imported"] is False, res


RUNNER = r'''
import importlib.util, io, json, sys, types
hook_path, scen_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
scen = json.load(open(scen_path))
fake = types.ModuleType("engram_log_emitter"); fake.Emitter = types.SimpleNamespace(init=lambda **k: None)
sys.modules["engram_log_emitter"] = fake
spec = importlib.util.spec_from_file_location("surface_under_test", hook_path)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
mod.query_daemon = lambda *a, **k: scen["daemon"]
mod._check_mcp_write_tool_marker = lambda h: (True, None)
mod.write_telemetry_record = lambda *a, **k: None
sys.stdin = io.StringIO(json.dumps({"prompt": scen["prompt"], "session_id": "s-3821", "transcript_path": ""}))
buf = io.StringIO(); real = sys.stdout; sys.stdout = buf; rc = 0
try:
    mod.main()
except SystemExit as e:
    rc = e.code or 0
sys.stdout = real
json.dump({"stdout": buf.getvalue(), "rc": rc, "client_imported": "engramd_client" in sys.modules,
           "core_imported": "engram_core" in sys.modules}, open(out_path, "w"))
'''


def test_c0_the_hook_exposes_the_module_level_config_path_the_a6_arm_drives(home, monkeypatch):
    """The A6 `--hook surface` arm switches the flag by pointing the hook's module-level CONFIG_PATH
    at a scratch config (the stop and tripwire hooks' convention); a hook without it is a blind arm
    (exit 4). Mutation: build the path inline in _owner_reads_enabled -> no CONFIG_PATH, and
    re-pointing it cannot change what the flag reads."""
    make_surface_store(home)
    set_config(home, flag=False)
    mod = load_hook(home, monkeypatch)
    assert mod.CONFIG_PATH == str(home / "config.json")
    other = home / "other-config.json"
    other.write_text(json.dumps({"engramd": {FLAG_KEY: True}}))
    mod.CONFIG_PATH = str(other)
    assert mod._owner_reads_enabled() is True


@pytest.mark.parametrize("value", [1, "true", "yes", [True], {"x": 1}, None, 0, "false"])
def test_c0_only_the_json_boolean_true_reads_on(value, home, monkeypatch):
    """Anything but the JSON boolean true is OFF (the read is flag_on's, not the hook's).
    Mutation: truthy coercion in the hook -> a non-bool value turns the owner path on."""
    make_surface_store(home)
    set_config(home, raw=json.dumps({"engramd": {FLAG_KEY: value}}))
    mod = load_hook(home, monkeypatch)
    assert mod._owner_reads_enabled() is False


def test_c0_the_boolean_true_reads_on_and_the_flag_is_read_only_through_flag_on(home, monkeypatch):
    """The boolean true reads ON and the read goes through engramd_owner_flags.flag_on with THIS
    hook's key and the config path. Mutation: the hook parsing config.json itself -> the spy
    sees no call."""
    make_surface_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    seen = []
    real = mod._import_owner_flags()

    class Spy:
        @staticmethod
        def flag_on(key, path):
            seen.append((key, path))
            return real.flag_on(key, path)
    monkeypatch.setattr(mod, "_import_owner_flags", lambda: Spy)
    assert mod._owner_reads_enabled() is True
    assert seen == [(FLAG_KEY, str(home / "config.json"))]


def test_c0_standalone_callers_outside_main_stay_on_the_direct_path(home, monkeypatch):
    """Outside main() `_OWNER_RUN` is None: the site functions read the store directly even if
    the config flag is ON (the owner state belongs to one fire). Mutation: deciding ON from the
    config at call time -> the owner is asked."""
    make_surface_store(home, restore="set")
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    assert mod._OWNER_RUN is None
    ok, msg = mod._check_embedding_restore_flag(str(home / "knowledge.db"))
    assert ok is False and "NOT FULLY RESTORED" in msg


def test_c0_owner_state_does_not_outlive_main(home, monkeypatch):
    """After a flag-ON main() returns, `_OWNER_RUN` is None again. Mutation: no reset -> a
    later standalone caller reads through a stale shim."""
    make_surface_store(home)
    mod = load_hook(home, monkeypatch)
    run, _ = fire(mod, home, monkeypatch, True)
    assert run.exc == "SystemExit(0)"
    assert mod._OWNER_RUN is None


# =========================================================================================
# C1 -- differential at EACH READ, then the rendered block
# =========================================================================================

def _diff_fire(home, monkeypatch, owner_kind, restore, person=False, mutate=None, fresh=True):
    """OFF fire then ON fire on one store: ((run_off, raw_off), (run_on, raw_on))."""
    if fresh:
        make_surface_store(home, restore=restore)
    off = fire(load_hook(home, monkeypatch, person=person), home, monkeypatch, False)
    on = fire(load_hook(home, monkeypatch, person=person), home, monkeypatch, True,
              owner_kind=owner_kind, mutate=mutate)
    return off, on


def raw_differences(raw_off, raw_on) -> dict:
    """The raw reads that differ between the paths. Row 6 adds NO read of its own (its liveness is
    derived from the restore-flag read, issued once on each path), so nothing is set aside: the
    liveness COUNT must be absent on both sides and the restore read present once on each."""
    a, b = canon(raw_off), canon(raw_on)
    assert SQL_LIVENESS not in a and SQL_LIVENESS not in b, (a.get(SQL_LIVENESS), b.get(SQL_LIVENESS))
    assert len(a.get(SQL_EMBED, [])) == len(b.get(SQL_EMBED, [])) == 1, (a.get(SQL_EMBED), b.get(SQL_EMBED))
    return {sql: (a.get(sql), b.get(sql)) for sql in (set(a) | set(b)) if a.get(sql) != b.get(sql)}


@pytest.mark.parametrize("restore", ["set", "unset"])
@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_c1_every_read_returns_the_same_raw_result_on_both_paths_and_the_block_is_identical(owner_kind, restore, home, monkeypatch):
    """Rows 1-5 on one store (>=1 dormant and >=1 live focus set; a node with a principle chain
    (ls_0001) and one without (ob_0001); the restore row both set and unset; a focused node under
    exclusion ON): the RAW result of each read (restore row, active-set name + set rows, count +
    df rows of the IDF gate, focused ids) is equal on both paths, the chain of each rendered node
    is equal, THEN the rendered block and the stdout are byte-identical. Every read must really
    have run (non-vacuous). Mutation: any owner-side read returning something else (see the
    required mutation cells below) -> raw differences non-empty."""
    (off_run, off_raw), (on_run, on_raw) = _diff_fire(home, monkeypatch, owner_kind, restore)
    assert off_run.exc == on_run.exc == "SystemExit(0)"
    diffs = raw_differences(off_raw, on_raw)
    assert diffs == {}, diffs
    sqls = set(canon(on_raw))
    assert {SQL_EMBED, SQL_ACTIVE, SQL_SETS, SQL_FOCUSED, SQL_COUNT, SQL_VOCAB, "chain"} <= sqls, sqls
    sets = canon(on_raw)[SQL_SETS][0][1]
    assert {r[0] for r in sets} == {"dormant-hit", "dormant-miss", "live-set"}
    chains = {a[0]: rows for a, rows in canon(on_raw)["chain"]}
    assert set(chains) >= {"ls_0001", "ob_0001"} and chains["ls_0001"]["heads"] == ["cs_0001"]
    assert not chains["ob_0001"]["heads"]
    assert canon(on_raw)[SQL_FOCUSED][0][1] == [("ob_0002",)]
    ctx = on_run.ctx
    assert "saved focus-set 'dormant-hit'" in ctx and "dormant-miss" not in ctx
    assert "↑ principle: cs_0001" in ctx
    assert ("NOT FULLY RESTORED" in ctx) is (restore == "set")
    assert on_run.stdout == off_run.stdout and on_run.stderr == off_run.stderr


def _drop_last_dormant_row(monkeypatch):
    """The required NEW-side mutation: the owner path for `_dormant_focus_set_candidates`
    drops one row (the last set, `dormant-miss`... whichever is last in rowid order)."""
    real = ec.OwnerReadConnection.execute

    def lossy(self, sql, params=()):
        cur = real(self, sql, params)
        if sql == SQL_SETS and cur._rows:
            cur._rows = cur._rows[:-1]
        return cur
    monkeypatch.setattr(ec.OwnerReadConnection, "execute", lossy)


def test_c1_required_mutation_a_dropped_dormant_row_reddens_the_raw_harness_while_the_nudge_is_unchanged(home, monkeypatch):
    """THE required mutation of the issue: the owner path of the dormant-set read drops one row
    (a set that overlaps NO rendered node, so the render cannot show the loss). The RAW harness
    reports a difference at the focus_sets statement; the rendered stdout is byte-identical to
    the direct path's. A render-only harness would pass this fixture -- that is the case this
    cell exists to prove. Control: without the mutation the same fixture has no differences.
    Mutation: that one (done here on the NEW side only)."""
    (off_run, off_raw), (on_run, on_raw) = _diff_fire(home, monkeypatch, "fake", "set")
    assert raw_differences(off_raw, on_raw) == {}
    (off2_run, off2_raw), (mut_run, mut_raw) = _diff_fire(home, monkeypatch, "fake", "set",
                                                          mutate=_drop_last_dormant_row, fresh=False)
    diffs = raw_differences(off2_raw, mut_raw)
    assert set(diffs) == {SQL_SETS}, diffs
    assert len(diffs[SQL_SETS][0][0][1]) == len(diffs[SQL_SETS][1][0][1]) + 1
    # ...and the nudge does not show it
    assert mut_run.stdout == off2_run.stdout


def test_c1_a_dropped_dormant_row_that_matches_changes_the_nudge_too(home, monkeypatch):
    """Control for the required mutation: dropping the row that DOES overlap rendered nodes
    changes both the raw harness and the nudge (so the fixture can show a render loss at all).
    Mutation: dropping the FIRST row (dormant-hit) on the owner side -> raw differs AND the
    rendered pointer line is gone."""
    def drop_first(monkeypatch):
        real = ec.OwnerReadConnection.execute

        def lossy(self, sql, params=()):
            cur = real(self, sql, params)
            if sql == SQL_SETS and cur._rows:
                cur._rows = cur._rows[1:]
            return cur
        monkeypatch.setattr(ec.OwnerReadConnection, "execute", lossy)
    (off_run, off_raw), (mut_run, mut_raw) = _diff_fire(home, monkeypatch, "fake", "set", mutate=drop_first)
    assert SQL_SETS in raw_differences(off_raw, mut_raw)
    assert "saved focus-set 'dormant-hit'" in off_run.ctx and "saved focus-set 'dormant-hit'" not in mut_run.ctx


@pytest.mark.parametrize("target", ["restore", "chain", "focused", "df"])
def test_c1_the_raw_harness_reddens_for_each_read_mutated_on_the_new_side(target, home, monkeypatch):
    """The harness is not specific to the dormant read: the owner side returning a different
    restore row, a different chain, other focused ids or other df rows each shows up as a raw
    difference at that read. Mutation: those, one per parametrisation."""
    real_exec, real_chain = ec.OwnerReadConnection.execute, ec.OwnerReadConnection.generalization_chain

    def mutate(m):
        def lossy(self, sql, params=()):
            cur = real_exec(self, sql, params)
            if target == "restore" and sql == SQL_EMBED:
                cur._rows = [(0, 0, None)]
            if target == "focused" and sql == SQL_FOCUSED:
                cur._rows = []
            if target == "df" and sql == SQL_VOCAB and cur._rows:
                cur._rows = [(t, d + 1) for t, d in cur._rows]
            return cur

        def lossy_chain(self, node_id, max_depth=4):
            res = real_chain(self, node_id, max_depth)
            if target == "chain" and node_id == "ls_0001":
                res = dict(res, heads=[])
            return res
        m.setattr(ec.OwnerReadConnection, "execute", lossy)
        m.setattr(ec.OwnerReadConnection, "generalization_chain", lossy_chain)
    (off_run, off_raw), (mut_run, mut_raw) = _diff_fire(home, monkeypatch, "fake", "set", mutate=mutate)
    diffs = raw_differences(off_raw, mut_raw)
    expect = {"restore": SQL_EMBED, "chain": "chain", "focused": SQL_FOCUSED, "df": SQL_VOCAB}[target]
    assert expect in diffs, (target, list(diffs))


def _site_results(mod, home):
    """What each migrated READ hands to the rest of the hook, as a plain value (the level BETWEEN
    the connection boundary and the rendered block): the restore probe's tuple; the dormant-set
    candidates AND the number of sets read, for rendered-id sets that match, miss and are empty;
    the principles section per node; the IDF gate decision; the focused ids."""
    db = str(home / "knowledge.db")
    out = {"embed": mod._check_embedding_restore_flag(db)}
    for name, rendered in (("hit", {"ls_0001", "ob_0001"}), ("miss", {"ob_0099"}), ("other", {"ob_0040"}), ("none", set())):
        out[f"dormant-{name}"] = mod._dormant_focus_set_candidates(rendered)
    for ids in (["ls_0001"], ["ob_0001"], ["ls_0001", "ob_0001"], []):
        out[f"principles-{'+'.join(ids) or 'empty'}"] = mod._render_triggered_principles_section(ids)
    cfg = mod._get_auto_surface_config()
    conn = mod._owner_conn() if mod._owner_active() else mod.connect_regular_ro(db)
    try:
        out["prepend-long"] = mod._should_prepend(PROMPT, conn, cfg)
        out["prepend-short"] = mod._should_prepend("hi", conn, cfg)
    finally:
        if not mod._owner_active():
            conn.close()
    build, rc = mod._focused_read_conn()
    out["focused"] = sys.modules["_hooklib"]._focused_ids(read_conn=rc)
    return out


@pytest.mark.parametrize("restore", ["set", "unset"])
@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_c1_each_read_hands_the_hook_the_same_value_on_both_paths(owner_kind, restore, home, monkeypatch):
    """The level between the connection boundary and the rendered block: the value each migrated
    read hands to the rest of the hook (dormant candidates AND `sets_checked`, the restore tuple,
    the principles text per node with and without a chain, the IDF gate decision, the focused ids)
    is equal on both paths. A set that overlaps nothing rendered is counted in `sets_checked`, so a
    hook-side drop of such a row is visible HERE while the rendered block is unchanged. Mutation:
    dropping a row (or a chain, a restore flag, a focused id) on the owner path inside the hook ->
    this cell reddens (documented source mutation in the handoff)."""
    make_surface_store(home, restore=restore)
    set_config(home, flag=True)
    direct = load_hook(home, monkeypatch)
    want = _site_results(direct, home)
    mod = load_hook(home, monkeypatch)
    owner_run_on(mod)
    with owner_of(owner_kind, home):
        got = _site_results(mod, home)
    assert got == want
    assert want["dormant-hit"][1] == 3 and want["dormant-hit"][0][0]["name"] == "dormant-hit"
    assert want["dormant-miss"][0] == [] and want["dormant-miss"][1] == 3
    assert "cs_0001" in want["principles-ls_0001"] and want["principles-ob_0001"] == ""
    assert want["focused"] == ({"ob_0002"}, True)
    assert (want["embed"][0] is False) is (restore == "set")


def test_c1_a_person_moment_fire_is_identical_on_both_paths(home, monkeypatch):
    """The row-8 fixture: same stdout on both paths when a person moment renders (the cursor
    write goes direct on both). Mutation: routing the cursor read/write differently on the ON
    path -> the rendered line or the stored cursor differs."""
    make_surface_store(home, restore="unset")
    mod = load_hook(home, monkeypatch, person=True)
    off = fire(mod, home, monkeypatch, False)[0]
    cur_off = json.loads(sqlite3.connect(str(home / "knowledge.db")).execute(
        "SELECT metadata FROM nodes WHERE id='pn_0001'").fetchone()[0])["special_moment_cursor"]
    c = sqlite3.connect(str(home / "knowledge.db"))
    c.execute("UPDATE nodes SET metadata = ? WHERE id = 'pn_0001'", (json.dumps({"special_moment_cursor": 0}),))
    c.commit()
    c.close()
    mod2 = load_hook(home, monkeypatch, person=True)
    on = fire(mod2, home, monkeypatch, True)[0]
    cur_on = json.loads(sqlite3.connect(str(home / "knowledge.db")).execute(
        "SELECT metadata FROM nodes WHERE id='pn_0001'").fetchone()[0])["special_moment_cursor"]
    assert "moment alpha" in on.ctx
    assert cur_off == cur_on == 1
    assert on.stdout == off.stdout


# =========================================================================================
# C2 -- zero opens of the store, keyed on the store path
# =========================================================================================

@pytest.mark.parametrize("exclusion", [True, False], ids=["exclusion-on", "exclusion-off"])
@pytest.mark.parametrize("probe_live", [False, True], ids=["no-probe", "probe-live"])
@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_c2_flag_on_a_full_fire_opens_the_store_zero_times(owner_kind, probe_live, exclusion, home, monkeypatch, tmp_path):
    """Flag ON, owner up, one full main(): the opener counter (keyed on the resolved path of
    knowledge.db, wrapping sqlite3.connect and dbapi2.connect) records ZERO opens, with in-context
    exclusion ON and with it OFF (the route-ledger resolver is U8's hole: with the probe live for
    the session the ledger builds a resolver too), an attached PACK db (a different file) is opened
    and is neither counted as the store nor hidden (it shows in `others`), and every read site
    reached the owner. Mutation: leave ANY of rows 1-6 on its direct open, or pass no read_conn to
    the ledger's resolver (U8's hole) -> opens non-empty."""
    make_surface_store(home, restore="set")
    pack = tmp_path / "pack"
    pack.mkdir()
    sqlite3.connect(str(pack / "knowledge.db")).close()
    set_config(home, flag=True, exclusion=exclusion,
               extra={"attached_packs": [{"id": "pk", "path": str(pack), "enabled": True}]})
    if probe_live:
        probe = Path(str(home)) / "probe" / "mods-recall" / SID
        (probe / "routes").mkdir(parents=True)
    mod = load_hook(home, monkeypatch)
    seen = []
    real_read = ec.read

    def spy(method, params, **k):
        seen.append(method if method != "read" else (params or {}).get("sql"))
        return real_read(method, params, **k)
    monkeypatch.setattr(ec, "read", spy)
    with owner_of(owner_kind, home):
        so = StoreOpens(home / "knowledge.db").install(monkeypatch)
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    assert so.opens == [], so.opens
    assert any(p.endswith("pack/knowledge.db") or p.endswith("pack" + os.sep + "knowledge.db") for p in so.others), so.others
    assert {SQL_EMBED, SQL_ACTIVE, SQL_SETS, SQL_COUNT, "df", "chain"} <= set(seen), seen
    assert "ping" not in seen, seen   # row 6 is derived from the SQL_EMBED read: no ping on a healthy fire
    if exclusion or probe_live:
        assert SQL_FOCUSED in seen, seen
    else:
        assert SQL_FOCUSED not in seen, seen
    assert "saved focus-set 'dormant-hit'" in run.ctx and "UNAVAILABLE" not in run.ctx


def test_c2_the_counter_keys_on_the_store_path_not_on_the_caller(home, monkeypatch, tmp_path):
    """Instrument self-test: the same direct open through a DIFFERENT function name, and an open of
    a different file by the same function, are told apart. Opening the store by any name counts;
    opening another file does not. Mutation: keying the counter on a function name -> the first
    assertion fails; keying on 'any sqlite3.connect' -> the second fails."""
    make_surface_store(home)
    other = tmp_path / "other.db"
    sqlite3.connect(str(other)).close()
    so = StoreOpens(home / "knowledge.db").install(monkeypatch)

    def some_unrelated_helper():
        return sqlite3.connect(f"file:{home / 'knowledge.db'}?mode=ro", uri=True)
    some_unrelated_helper().close()
    sqlite3.connect(str(other)).close()
    assert len(so.opens) == 1 and so.opens[0]["rw"] is False and so.opens[0]["frames"][0] == "some_unrelated_helper"
    assert so.others == [os.path.realpath(str(other))]


@pytest.mark.parametrize("flag", [True, False], ids=["flag-on", "flag-off"])
def test_c2_remainder_a_person_moment_fire_shows_exactly_one_rw_open_at_the_cursor_write(flag, home, monkeypatch):
    """Row 8 stays DIRECT on both flag values: a fire that renders a person moment shows
    exactly ONE RW open of the store, and its frames name `_advance_person_moment_cursor`; with the
    flag ON that is the ONLY open (every read-only open is gone), with it OFF the reads are
    there too. Mutation: routing the write through the owner (zero RW opens) or opening the store
    a second time for it -> the count assertion fails; any RO open with the flag ON -> fails."""
    make_surface_store(home, restore="unset")
    set_config(home, flag=flag, exclusion=True)
    mod = load_hook(home, monkeypatch, person=True)
    owner = FakeOwner(home) if flag else contextlib.nullcontext()
    with owner:
        so = StoreOpens(home / "knowledge.db").install(monkeypatch)
        run = run_main(mod)
    assert run.exc == "SystemExit(0)" and "moment alpha" in run.ctx
    rw = [o for o in so.opens if o["rw"]]
    assert len(rw) == 1 and "_advance_person_moment_cursor" in rw[0]["frames"], so.opens
    if flag:
        assert len(so.opens) == 1, so.opens
    else:
        assert len(so.opens) > 1


# =========================================================================================
# C3 -- owner down: typed Unavailable, never a stale value or a plausible default
# =========================================================================================

DOWN = pytest.mark.parametrize("kind", ["unavailable-down", "unavailable", "rejected", "unusable", "raising"])


def make_failing_read(kind, home):
    if kind == "unavailable-down":
        return None   # no owner started: the real client answers Unavailable
    real = ec.read

    def fake(method, params, **k):
        if method == "ping":
            return real(method, params, **k)
        if kind == "unavailable":
            return ec.Unavailable(socket_path=str(home / "run" / "engramd.sock"), bound=1.0,
                                  agent_steps=["AGENT-STEP-3821"], human_steps=["HUMAN-STEP-3821"], detail="no answer")
        if kind == "rejected":
            return ec.Rejected(reason="unregistered_template", detail="not registered", remedy="register it")
        if kind == "unusable":
            return ec.Ok(result="garbage")
        raise ValueError("boom-3821")
    return fake


def expected_kind(kind):
    return {"unavailable-down": "unavailable", "unavailable": "unavailable", "rejected": "rejected",
            "unusable": "unusable_answer", "raising": "client_failure"}[kind]


@DOWN
def test_c3_the_wrapped_sites_record_a_typed_failure_and_degrade_without_a_default_value(kind, home, monkeypatch):
    """Every migrated read, called as the fire calls it with the owner failing: the site records
    ONE typed failure for itself (kind, the exact result object), opens the store zero times, and
    returns its degraded value (embedding probe: UNKNOWN (None, ...), never True/healthy;
    focused ids: (set(), False) = unknown; principles: ''; dormant: ([], 0)); the failure, not the
    degraded value, is what the notice carries. Mutations: report healthy on an outage (the
    embedding probe returning (True, None)); not recording at a site; a direct fallback."""
    make_surface_store(home, restore="set")
    mod = load_hook(home, monkeypatch)
    f = make_failing_read(kind, home)
    if f is not None:
        monkeypatch.setattr(ec, "read", f)
    so = StoreOpens(home / "knowledge.db").install(monkeypatch)

    def run_site(name, call, degraded):
        owner_run_on(mod)
        got = call()
        assert degraded(got), (name, got)
        fail = mod._OWNER_RUN["failure"]
        assert fail is not None and fail["site"] == name and fail["kind"] == expected_kind(kind), (name, fail)
        if kind in ("unavailable-down", "unavailable"):
            assert isinstance(fail["result"], ec.Unavailable)
    run_site("_check_embedding_restore_flag", lambda: mod._check_embedding_restore_flag(str(home / "knowledge.db")),
             lambda r: r[0] is None and r[1])
    run_site("_dormant_focus_set_candidates", lambda: mod._dormant_focus_set_candidates({"ls_0001", "ob_0001"}),
             lambda r: r == ([], 0))
    run_site("_render_triggered_principles_section", lambda: mod._render_triggered_principles_section(["ls_0001"]),
             lambda r: r == "")

    run_site("_should_prepend", lambda: mod._should_prepend(PROMPT, mod._owner_conn(), mod._get_auto_surface_config()),
             lambda r: isinstance(r, bool))
    # row 5: a read that fails INSIDE _focused_ids is that function's own 'unknown' state
    # ((set(), False), never the readable-empty (set(), True)); it opens nothing. It is not
    # recorded on its own (tripwire parity): the other sites of the same outage are.
    owner_run_on(mod)
    build, conn = mod._focused_read_conn()
    assert build is True and conn is mod._owner_conn()
    assert sys.modules["_hooklib"]._focused_ids(read_conn=conn) == (set(), False)
    assert so.opens == [], so.opens


def test_c3_an_unimportable_client_builds_no_focused_ids_connection_and_is_recorded(home, monkeypatch):
    """Row 5 with the client unimportable: `_focused_read_conn` is (False, None) -- the callers
    build NO resolver and write no ledger line, because the alternative is a direct open -- and
    the failure is recorded at `_focused_ids` as a client_import. Mutation: returning (True, None)
    (which `_focused_ids` reads as 'open it yourself') -> red."""
    make_surface_store(home)
    mod = load_hook(home, monkeypatch)
    owner_run_on(mod)
    monkeypatch.setitem(sys.modules, "engramd_client", None)
    empty = home / "empty-runtime"
    empty.mkdir()
    monkeypatch.setenv("ENGRAM_RUNTIME_DIR", str(empty))
    assert mod._focused_read_conn() == (False, None)
    f = mod._OWNER_RUN["failure"]
    assert f["site"] == "_focused_ids" and f["kind"] == "client_import"


def test_c3_ping_failure_is_a_typed_failure_not_a_healthy_probe(home, monkeypatch):
    """Row 6 with the owner down: `_check_db_liveness` is NOT (True, 'ok') (a stale/plausible
    answer): it is (False, ...) with a recorded unavailable failure at its own site; no store
    open. Mutation: fail-open (True, 'ok') on a failed ping, or probing the file instead -> red."""
    make_surface_store(home)
    mod = load_hook(home, monkeypatch)
    owner_run_on(mod)
    so = StoreOpens(home / "knowledge.db").install(monkeypatch)
    ok, reason = mod._check_db_liveness(str(home / "knowledge.db"))
    assert ok is False and reason
    fail = mod._OWNER_RUN["failure"]
    assert fail["site"] == "_check_db_liveness" and fail["kind"] == "unavailable" and isinstance(fail["result"], ec.Unavailable)
    assert so.opens == []


@DOWN
def test_c3_a_full_fire_with_the_owner_down_renders_owner_unavailable_text_and_no_stale_block(kind, home, monkeypatch):
    """Full main(), flag ON, owner failing, daemon up: ONE owner-unavailable notice that says the
    reads could NOT be read (and is not the DB-liveness 'check disk health' text), names the
    failing site and the flag-off remedy; NO dormant-set pointer, NO principle line, NO restore
    claim (the store has all of them: showing none of them is the outage rendered, not an empty
    answer); the store is opened zero times; one stderr line. Mutation: swallowing the failure
    silently (no notice) / a direct fallback (the pointer line appears and opens > 0)."""
    make_surface_store(home, restore="set")
    set_config(home, flag=True, exclusion=True)
    mod = load_hook(home, monkeypatch)
    f = make_failing_read(kind, home)
    if f is not None:
        monkeypatch.setattr(ec, "read", f)
    so = StoreOpens(home / "knowledge.db").install(monkeypatch)
    run = run_main(mod)
    ctx = run.ctx
    assert run.exc == "SystemExit(0)", run.exc
    assert so.opens == [], so.opens
    assert "engramd owner" in ctx and "could NOT be read" in ctx and FLAG_KEY in ctx
    assert "disk health" not in ctx
    assert "saved focus-set 'dormant-hit'" not in ctx and "↑ principle" not in ctx and "NOT FULLY RESTORED" not in ctx
    assert ctx.count("could NOT be read") == 1, ctx
    # (the embedding probe's own pre-existing UNKNOWN stderr line also carries the words)
    lines = re.findall(r"^engram-surface-hook: owner read failed: (\w+) at (\w+)" + OWNER_LINE_TAIL + "$", run.stderr, re.M)
    assert len(lines) == 1 and lines[0][0] == expected_kind(kind), run.stderr
    assert "boom-3821" not in run.stdout + run.stderr


def test_c3_no_row_7_fallback_when_the_owner_is_down(home, monkeypatch):
    """Owner down AND recall daemon down: `query_fts_fallback` (the in-hook server runtime) is
    NOT taken, and neither is any other EngramClient. Mutation: calling it on the owner-failure
    path -> the spy fires."""
    make_surface_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch, daemon="down")
    calls = []
    mod.query_fts_fallback = lambda *a, **k: calls.append(a) or {"match_count": 0}
    run = run_main(mod)
    assert calls == [] and run.exc == "SystemExit(0)"


def test_c3_the_notice_is_rate_limited_per_window_and_the_stamp_follows_the_print(home, monkeypatch):
    """Two consecutive down fires: the first carries the notice and stamps the window under kind
    `surface-owner`; the second (inside the window) carries none on stdout but still writes the
    stderr line. Mutation: no rate limit -> the second fire repeats the notice; stamping before the
    print -> a print that raises would still consume the window."""
    make_surface_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    first = run_main(mod)
    stamp = json.loads((home / "probe-unknown-notice.json").read_text())["shown_at"]
    assert NOTICE_KEY in stamp
    second = run_main(load_hook(home, monkeypatch))
    assert "could NOT be read" in first.ctx and "could NOT be read" not in second.ctx
    assert re.search(r"^engram-surface-hook: owner read failed: \w+ at \w+" + OWNER_LINE_TAIL + "$", second.stderr, re.M)


# =========================================================================================
# C6 / C7 -- rows 6 and 7
# =========================================================================================

# Row 6, flag ON (issue #3821 amended 13:56Z, contract item 3b; derivation changed in fold 3 so the
# store-touching half costs NO extra trip): liveness is DERIVED from the outcome of the fire's own
# store read, `_check_embedding_restore_flag` (a registered template read on every fire):
#   read Ok                     -> healthy (no `ping`, no extra statement);
#   read failed                 -> ONE `ping`: it fails -> owner down; it answers -> the owner is up
#                                  and the store did not serve the read -> 'owner up, store unreadable';
#   read answered 'no such table' (a store without that table: `refused_at_load`, or `read_failed`
#     naming the RESTORE table) -> the owner served/answered a read of that table: healthy for liveness, the restore flag
#     itself stays UNKNOWN as before.
# Scope: 'healthy' = the owner served a read of the restore table (or reported that one table absent); it does
# NOT assert the rest of the store is intact (a store with `nodes` dropped reads liveness-healthy and the
# fire is loud elsewhere: see the gutted-store cell).
# The restore read is issued ONCE per fire; its value and the liveness verdict come from that one read.
SQL_LIVENESS = "SELECT count(*) FROM nodes WHERE is_current = 1"   # the fold-1 extra COUNT: must be gone
HEALTHY = (True, "ok")


def break_store(home: Path, how: str) -> None:
    """Break the store AFTER the owner started: unlink it, or replace it with a non-database."""
    db = home / "knowledge.db"
    if how == "overwritten":
        # IN PLACE (same inode): a running owner holds the old inode open, so an unlink would not
        # break its reads; the bytes under its feet are what breaks them.
        with open(db, "r+b") as fh:
            fh.truncate(0)
            fh.write(b"this is not a sqlite database " * 400)
        for suffix in ("-wal", "-shm"):
            (home / f"knowledge.db{suffix}").unlink(missing_ok=True)
        return
    for suffix in ("", "-wal", "-shm"):
        (home / f"knowledge.db{suffix}").unlink(missing_ok=True)
    if how == "garbage":
        db.write_bytes(b"this is not a sqlite database " * 200)


def liveness_cell(mod, home, monkeypatch, owner_kind, breaker=None):
    """One `_check_db_liveness` call in the flag-ON state with an owner up (fake or real), the
    client spied; returns (result, seen, opens, run_state). `breaker(home)` runs after the owner
    started and before the call."""
    owner_run_on(mod)
    seen = []
    real = ec.read
    monkeypatch.setattr(ec, "read", lambda m, p, **k: seen.append((m, (p or {}).get("sql"))) or real(m, p, **k))
    with owner_of(owner_kind, home):
        # installed AFTER the owner started: a real in-process owner opens the store on this thread
        so = StoreOpens(home / "knowledge.db").install(monkeypatch)
        if breaker is not None:
            breaker()
        res = mod._check_db_liveness(str(home / "no-such-dir" / "knowledge.db"))
    return res, seen, so.opens, mod._OWNER_RUN


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_c6_healthy_is_the_fires_own_store_read_alone_no_ping_no_extra_statement(owner_kind, home, monkeypatch):
    """Row 6 case (i), ON: the restore-flag read answers Ok -> (True, 'ok') from that ONE read:
    the client sees exactly [read SQL_EMBED] -- NO `ping` and NO liveness COUNT; the store is
    opened zero times, a missing LOCAL file is irrelevant, nothing is recorded. Mutation: a ping
    on the healthy path; the extra COUNT (`SQL_LIVENESS`) again; probing the file."""
    make_surface_store(home)
    mod = load_hook(home, monkeypatch)
    res, seen, opens, run = liveness_cell(mod, home, monkeypatch, owner_kind)
    assert res == HEALTHY, res
    assert seen == [("read", SQL_EMBED)], seen
    assert opens == [] and run["failure"] is None and run["degraded"] == []


@pytest.mark.parametrize("how,owner_kind", [
    pytest.param("unlinked", "fake", id="fake-owner-unlinked"),
    pytest.param("garbage", "fake", id="fake-owner-replaced"),
    pytest.param("overwritten", "fake", id="fake-owner-overwritten"),
    # a RUNNING real owner holds the old inode open: unlink / replace do not reach its reads
    # (measured), so for it the store is broken in place
    pytest.param("overwritten", "real", id="real-owner-overwritten", marks=requires_linux_owner),
])
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")  # the FAKE owner lets the sqlite error escape its handler thread (closed socket = no answer); the real owner answers a typed rejection
def test_c6_ping_ok_but_the_store_read_failing_is_owner_up_store_unreadable(how, owner_kind, home, monkeypatch):
    """Row 6 case (ii), ON (item 3b's REQUIRED CONTROL): the owner started, THEN the store is
    unlinked / replaced / overwritten: the fire's store read fails, ONE `ping` follows and still
    answers (it is store-free) -> NOT healthy, NOT owner-down: (False, <'owner is up but the store
    is unreadable'>), the typed failure recorded at `_check_db_liveness`, stage store_unreadable.
    Against a ping-only implementation this reads (True, 'ok') and goes red. Mutation: ping-only;
    fail-open on the failed read."""
    make_surface_store(home)
    mod = load_hook(home, monkeypatch)
    res, seen, opens, run = liveness_cell(mod, home, monkeypatch, owner_kind, lambda: break_store(home, how))
    assert [m for m, _ in seen] == ["read", "ping"], seen
    assert res[0] is False and res != HEALTHY, res
    assert "store is unreadable" in res[1] and "did not answer ping" not in res[1], res
    assert run["liveness_stage"] == "store_unreadable"
    assert run["failure"]["site"] == "_check_db_liveness" and "_check_db_liveness" in run["degraded"]
    assert opens == []


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_c6_owner_down_is_owner_down_and_distinct_from_store_unreadable(owner_kind, home, monkeypatch):
    """Row 6 case (iii), ON: no owner answers the read, the one `ping` does not either ->
    (False, 'engramd owner did not answer ping: <kind>'), stage owner_down, the typed Unavailable
    recorded at `_check_db_liveness`; distinct from the store-unreadable result. Mutation:
    fail-open (True, 'ok'); collapsing owner-down into store-unreadable text."""
    make_surface_store(home)
    mod = load_hook(home, monkeypatch)
    owner_run_on(mod)
    seen = []
    real = ec.read
    monkeypatch.setattr(ec, "read", lambda m, p, **k: seen.append(m) or real(m, p, **k))
    res = mod._check_db_liveness(str(home / "knowledge.db"))
    assert res[0] is False and "did not answer ping: unavailable" in res[1] and "store is unreadable" not in res[1], res
    assert seen == ["read", "ping"], seen
    run = mod._OWNER_RUN
    assert run["liveness_stage"] == "owner_down"
    assert run["failure"]["site"] == "_check_db_liveness" and isinstance(run["failure"]["result"], ec.Unavailable)


@pytest.mark.parametrize("answer", ["garbage", ["not-a-count"], [[1]]],
                         ids=["not-a-list", "row-not-a-row", "row-too-short"])
def test_c6_a_malformed_answer_from_the_restore_read_is_not_healthy(answer, home, monkeypatch):
    """Row 6, ON (Clio's M2, restated for the new derivation): the owner answers the restore
    read with something that is not a usable restore row -- not a list, a row that is not a row,
    or a row too short to unpack -- while `ping` is fine: NOT healthy; the liveness reads 'owner is
    up but the store is unreadable' (stage store_unreadable) and records a failure. Mutation: treat
    any Ok as healthy; drop the shape guard (the client's `unusable_answer`) or the short-row
    handling in the restore read."""
    make_surface_store(home)
    mod = load_hook(home, monkeypatch)
    owner_run_on(mod)
    real = ec.read
    monkeypatch.setattr(ec, "read", lambda m, p, **k: ec.Ok(result=answer)
                        if (m == "read" and (p or {}).get("sql") == SQL_EMBED) else real(m, p, **k))
    with FakeOwner(home):
        res = mod._check_db_liveness(str(home / "knowledge.db"))
    assert res[0] is False and "store is unreadable" in res[1], res
    run = mod._OWNER_RUN
    assert run["liveness_stage"] == "store_unreadable" and run["failure"]["site"] == "_check_db_liveness"


def test_c6_budget_exhausted_on_the_restore_read_is_the_owner_side_not_the_store(home, monkeypatch):
    """Row 6, ON (Clio's M7, restated): the fire's owner budget is spent before the restore read
    (fire budget 0 reproduces it) -> nothing was learned about the store: the result is attributed
    to the owner side ('did not answer the liveness read: budget_exhausted'), stage owner_down,
    NEVER 'store is unreadable', and no `ping` is spent on it. Mutation: letting budget_exhausted
    fall into the store-unreadable branch."""
    make_surface_store(home)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(mod, "OWNER_FIRE_BUDGET_S", 0)
    owner_run_on(mod)
    seen = []
    real = ec.read
    monkeypatch.setattr(ec, "read", lambda m, p, **k: seen.append(m) or real(m, p, **k))
    with FakeOwner(home):
        res = mod._check_db_liveness(str(home / "knowledge.db"))
    assert res == (False, "engramd owner did not answer the liveness read: budget_exhausted"), res
    assert mod._OWNER_RUN["liveness_stage"] == "owner_down" and seen == []


def _drop_restore_table(home):
    c = sqlite3.connect(str(home / "knowledge.db"))
    c.execute("DROP TABLE IF EXISTS embedding_restore_state")
    c.commit()
    c.close()


@pytest.mark.parametrize("when", ["before-start", "after-start"])
@requires_linux_owner
def test_c6_a_missing_restore_table_is_not_a_liveness_failure_and_the_flag_stays_unknown(when, home, monkeypatch):
    """The CAREFUL POINT, REAL owner: the store lacks `embedding_restore_state` (dropped before the
    owner started -> the owner answers `refused_at_load`; dropped after -> `read_failed` naming the
    table). The owner AND the store answered, so liveness is HEALTHY with no `ping`, no failure
    recorded by liveness and no store-unreadable line; the restore flag itself stays UNKNOWN
    (None) exactly as before, recorded once at its own site. Mutation: a missing table read as
    'store unreadable' or as 'owner down'; pinging on it; making the restore flag healthy."""
    make_surface_store(home, restore="set")
    mod = load_hook(home, monkeypatch)
    owner_run_on(mod)
    seen = []
    real = ec.read
    monkeypatch.setattr(ec, "read", lambda m, p, **k: seen.append(m) or real(m, p, **k))
    if when == "before-start":
        _drop_restore_table(home)
    with owner_of("real", home):
        if when == "after-start":
            _drop_restore_table(home)
        res = mod._check_db_liveness(str(home / "knowledge.db"))
        flag = mod._check_embedding_restore_flag(str(home / "knowledge.db"))
    assert res == HEALTHY, res
    assert seen == ["read"], seen          # ONE restore read serves both, and no ping
    assert flag[0] is None and "OwnerReadError" in flag[1], flag
    run = mod._OWNER_RUN
    assert run.get("liveness_stage") is None
    assert run["failure"]["site"] == "_check_embedding_restore_flag" and run["degraded"] == ["_check_embedding_restore_flag"]


@pytest.mark.parametrize("rejected,expect_ok", [
    (("refused_at_load", "classification_incomplete"), True),
    (("read_failed", "OperationalError: no such table: embedding_restore_state"), True),
    # ANOTHER missing table is a failed read, not the restore table's absence: it goes the failed-read
    # way (ping answers -> stage store_unreadable, 'the store is unreadable: rejected')
    (("read_failed", "OperationalError: no such table: nodes"), False),
    (("read_failed", "DatabaseError: file is not a database"), False),
    (("read_failed", "OperationalError: database is locked"), False),
], ids=["refused-at-load", "no-such-restore-table", "no-such-table-nodes", "not-a-database", "locked"])
def test_c6_which_rejections_of_the_restore_read_are_a_missing_table(rejected, expect_ok, home, monkeypatch):
    """Fake-owner side of the missing-table decision: ONLY a rejection that says the RESTORE table
    is missing (`refused_at_load` with the registry's `classification_incomplete`, or `read_failed`
    naming exactly `no such table: embedding_restore_state`) reads healthy; any other rejection of
    the read (another missing table such as `nodes`, a damaged or locked store) is a failed read:
    'owner up, store unreadable' when `ping` answers. Mutation: matching any 'no such table' (or any
    `read_failed`) as healthy -> the `nodes` row (and the last two) go red."""
    reason, detail = rejected
    make_surface_store(home)
    mod = load_hook(home, monkeypatch)
    owner_run_on(mod)
    real = ec.read
    monkeypatch.setattr(ec, "read", lambda m, p, **k: ec.Rejected(reason=reason, detail=detail, remedy="n/a")
                        if (m == "read" and (p or {}).get("sql") == SQL_EMBED) else real(m, p, **k))
    with FakeOwner(home):
        res = mod._check_db_liveness(str(home / "knowledge.db"))
    if expect_ok:
        assert res == HEALTHY, res
    else:
        assert res[0] is False and "store is unreadable" in res[1], res


SKEW_CASES = [
    # (reason, detail, text that must appear, text that must NOT appear (the store's advice))
    ("unregistered_template", "deadbeef", "does not know the liveness read", "unreadable"),
    ("refused_at_load", "virtual_table_template", "dropped the liveness read at load", "unreadable"),
    ("read_failed", "DatabaseError: file is not a database", "the store is unreadable", "does not know"),
]


@pytest.mark.parametrize("reason,detail,present,absent", SKEW_CASES, ids=[c[0] for c in SKEW_CASES])
def test_c6_each_refusal_kind_has_its_own_reason_text(reason, detail, present, absent, home, monkeypatch):
    """Row 6, ON, `ping` answering, the restore read REFUSED: the three-way classification is
    unchanged (all three are 'owner up, the read did not serve', stage store_unreadable) but each
    kind names itself: `unregistered_template` = the owner's build is older than this hook (version
    skew); `refused_at_load` (not the missing-table detail) = the live store failed re-classification
    (schema skew); a typed `read_failed` = the store itself. Mutation: one text for all three."""
    make_surface_store(home)
    mod = load_hook(home, monkeypatch)
    owner_run_on(mod)
    real = ec.read
    monkeypatch.setattr(ec, "read", lambda m, p, **k: ec.Rejected(reason=reason, detail=detail, remedy="n/a")
                        if (m == "read" and (p or {}).get("sql") == SQL_EMBED) else real(m, p, **k))
    with FakeOwner(home):
        res = mod._check_db_liveness(str(home / "knowledge.db"))
    assert res[0] is False and present in res[1] and absent not in res[1], res
    assert mod._OWNER_RUN["liveness_stage"] == "store_unreadable"


@pytest.mark.parametrize("reason,detail,present,absent", SKEW_CASES, ids=[c[0] for c in SKEW_CASES])
def test_c6_the_disk_health_advice_is_only_for_the_store_itself(reason, detail, present, absent, home, monkeypatch):
    """Full main(), flag ON: the head block for a skew refusal carries its own remedy and NOT the
    'check disk health / WAL' advice (it cannot help a build/schema condition); the typed
    `read_failed` store fault keeps that advice. Mutation: the one block for every kind."""
    make_surface_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    real = ec.read
    monkeypatch.setattr(ec, "read", lambda m, p, **k: ec.Rejected(reason=reason, detail=detail, remedy="n/a")
                        if (m == "read" and (p or {}).get("sql") == SQL_EMBED) else real(m, p, **k))
    with FakeOwner(home):
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    assert "DB liveness: FAILED" in run.ctx, run.ctx
    if reason == "read_failed":
        assert "disk health" in run.ctx
    else:
        assert "disk health" not in run.ctx and "WAL" not in run.ctx
        assert ("redeploy" if reason == "unregistered_template" else "migrate the store") in run.ctx


@pytest.mark.parametrize("break_it", [False, True], ids=["healthy", "store-unreadable"])
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_c6_the_restore_read_is_issued_once_per_fire_and_serves_both_answers(break_it, home, monkeypatch):
    """Full main(), flag ON: the restore-flag statement reaches the owner EXACTLY ONCE (the value of
    the restore flag and the liveness verdict come from that one read), on a healthy fire and on a
    fire whose store broke after the owner started (where one `ping` follows). Mutation: reading
    it again in main() after liveness."""
    make_surface_store(home, restore="set")
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with FakeOwner(home) as owner:
        if break_it:
            break_store(home, "unlinked")
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    sqls = [r.get("params", {}).get("sql") for r in owner.requests if r["method"] == "read"]
    assert sqls.count(SQL_EMBED) == 1, owner.requests
    assert SQL_LIVENESS not in sqls
    pings = [r for r in owner.requests if r["method"] == "ping"]
    assert len(pings) == (1 if break_it else 0), owner.requests


# Owner requests of ONE healthy flag-ON fire (exclusion ON, restore set, daemon stubbed, FakeOwner):
# N at bc845fac (ping-only liveness) and K now (liveness derived from the fire's own restore read).
HEALTHY_CALLS_BC845FAC = 9
HEALTHY_CALLS_NOW = 8


def _owner_calls(mod, home, monkeypatch):
    set_config(home, flag=True, exclusion=True)
    reset_state(home)
    with FakeOwner(home) as owner:
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    return owner.requests


def test_c6_a_healthy_flag_on_fire_makes_exactly_the_pinned_owner_calls(home, monkeypatch):
    """UNCONDITIONAL (needs no baseline commit; `git`/`subprocess` are made to fail to prove it):
    a healthy flag-ON fire issues the restore statement EXACTLY ONCE, no `ping`, no liveness COUNT,
    and its TOTAL owner requests EQUAL the pinned K (Zephyr, Kepler: an equality, not a ceiling -- a
    change that skips, caches or short-circuits the restore read would weaken liveness back to
    ping-only with NO extra call). Mutation: an extra liveness COUNT / ping back -> the total goes UP;
    skipping / caching the restore read -> the total and the restore count go DOWN; each reddens this
    cell (and it does so in a clone that has no bc845fac)."""
    def no_git(*a, **k):
        raise AssertionError("this cell must not need git or any baseline")
    monkeypatch.setattr(subprocess, "run", no_git)
    make_surface_store(home, restore="set")
    new = _owner_calls(load_hook(home, monkeypatch), home, monkeypatch)
    methods = [q["method"] for q in new]
    sqls = [q.get("params", {}).get("sql") for q in new if q["method"] == "read"]
    assert sqls.count(SQL_EMBED) == 1, new
    assert "ping" not in methods, new
    assert SQL_LIVENESS not in sqls
    assert len(new) == HEALTHY_CALLS_NOW, (len(new), HEALTHY_CALLS_NOW, methods)


def test_c6_a_healthy_fire_makes_one_call_fewer_than_the_pre_amendment_build(home, monkeypatch, tmp_path):
    """The old-build COMPARISON only (skips when bc845fac is not in the clone: a shallow checkout, or
    the branch gone after the squash-merge): the ping-only build sends N = 9 requests for the same
    healthy fire and the derived build K = N - 1. The unconditional cell above carries the equality
    itself; this one only ties the pinned numbers to the measured baseline."""
    r = subprocess.run(["git", "show", "bc845fac:src/engram/hooks/claude/engram-surface-hook.py"],
                       capture_output=True, text=True, cwd=str(REPO_ROOT))
    if r.returncode != 0 or not r.stdout:
        pytest.skip("bc845fac is not available in this clone")
    make_surface_store(home, restore="set")
    root = flattened_root(tmp_path)
    base = root / "hooks" / "engram-surface-hook.py"
    base.write_text(r.stdout)
    old = _owner_calls(load_hook(home, monkeypatch, base), home, monkeypatch)
    new = _owner_calls(load_hook(home, monkeypatch), home, monkeypatch)
    print(f"owner requests per healthy flag-ON fire: bc845fac N={len(old)} now K={len(new)}")
    assert len(old) == HEALTHY_CALLS_BC845FAC, (len(old), "the pre-amendment build's count moved: re-pin")
    assert len(new) == HEALTHY_CALLS_NOW == len(old) - 1, (len(new), len(old))


def _leading_block(ctx: str) -> str:
    """The first delivered block: the context minus the leading `▸ n shown` trailer line(s)."""
    lines = [ln for ln in ctx.splitlines()]
    while lines and (lines[0].startswith("▸") or not lines[0].strip()):
        lines.pop(0)
    return lines[0] if lines else ""


@pytest.mark.parametrize("when", ["before-start", "after-start"])
@requires_linux_owner
def test_c6_a_gutted_store_makes_the_flag_on_fire_lead_with_the_owner_unavailable_line(when, home, monkeypatch):
    """Where the loudness for a store with `nodes` gone actually lives (Borges, measured): liveness
    reads healthy there (the restore read selects only `embedding_restore_state`; it asserts "the owner
    served a read of that table", never "the store is intact"), but a FULL flag-ON fire on a REAL owner
    delivers a context whose LEADING block is the `ENGRAM UNAVAILABLE` owner line, naming the first
    failing read site (`_should_prepend`). Pins existing behaviour so a future change that made those reads fail soft would go
    silent loudly. Mutation: the `_should_prepend` owner failure not recorded / not rendered."""
    make_surface_store(home, restore="set")
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)

    def gut():
        c = sqlite3.connect(str(home / "knowledge.db"))
        c.execute("DROP TABLE IF EXISTS nodes")
        c.commit()
        c.close()
    if when == "before-start":
        gut()
    with owner_of("real", home):
        if when == "after-start":
            gut()
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    head = _leading_block(run.ctx)
    # the FIRST failing read of a fire is the keyword gate's (`_should_prepend`, the COUNT over nodes):
    # that site leads the line (measured on a real owner, before and after the drop)
    assert head.startswith("⚠️  ENGRAM UNAVAILABLE: the engramd owner read failed at _should_prepend (rejected"), run.ctx[:400]
    assert "could NOT be read" in head, head


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_c6_a_full_fire_with_a_store_broken_after_the_owner_started_shows_the_store_unreadable_line(owner_kind, home, monkeypatch):
    """Full main(), flag ON, owner up, the store broken after it started: the head carries the
    distinct 'owner is up but the store is unreadable' warning (own text; not the owner-down
    'could NOT be read ... flag' text and not silence), WITH the disk-health / WAL recovery advice
    (the owner answers, so the store is what to check) and the owner-restart step; no direct open.
    Mutation: ping-only (no warning); reusing the owner-down line; blaming the owner."""
    make_surface_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with owner_of(owner_kind, home):
        break_store(home, "overwritten")
        so = StoreOpens(home / "knowledge.db").install(monkeypatch)
        run = run_main(mod)
    ctx = run.ctx
    assert run.exc == "SystemExit(0)", run.exc
    assert so.opens == [], so.opens
    assert "owner is up but the store is unreadable" in ctx, ctx
    assert "disk health" in ctx and "did not answer ping" not in ctx


def test_c6_a_full_fire_with_the_owner_down_never_says_store_unreadable(home, monkeypatch):
    """Full main(), flag ON, no owner: the owner-down notice only (no 'store is unreadable', no
    disk-health block: the ping failure case keeps suppressing the WAL block). Mutation: always
    emitting the store line on a liveness failure."""
    make_surface_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    run = run_main(mod)
    assert "store is unreadable" not in run.ctx and "disk health" not in run.ctx
    assert "could NOT be read" in run.ctx


def test_c6_flag_off_liveness_is_todays_probe(home, monkeypatch):
    """Row 6, OFF: the unchanged file probe (missing file -> (False, 'knowledge.db not found'); a
    healthy file -> (True, 'ok'); no ping). Mutation: pinging on the OFF path -> the client spy
    fires."""
    make_surface_store(home)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(ec, "read", lambda *a, **k: pytest.fail("ping on the OFF path"))
    assert mod._check_db_liveness(str(home / "nope.db")) == (False, "knowledge.db not found")
    assert mod._check_db_liveness(str(home / "knowledge.db")) == (True, "ok")


def test_c7_flag_on_with_the_recall_daemon_down_emits_the_existing_notice_and_no_recall_block(home, monkeypatch):
    """Row 7, ON: daemon down -> `query_fts_fallback` is NOT called, the hook's existing daemon
    notice (CRITICAL offline text) is emitted, there is no '[ENGRAM Recall' block, and the store is
    opened zero times. Mutation: taking the fallback (spy fires, or a recall block appears)."""
    make_surface_store(home)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch, daemon="down")
    calls = []
    mod.query_fts_fallback = lambda *a, **k: calls.append(a) or {"match_count": 1, "top_claims": [_claim("ob_0001", "x", "y")],
                                                                  "matched_ids": ["ob_0001"], "special_nodes": []}
    so = StoreOpens(home / "knowledge.db").install(monkeypatch)
    with FakeOwner(home):
        run = run_main(mod)
    assert calls == [] and so.opens == []
    assert "surface daemon offline" in run.ctx and "[ENGRAM Recall" not in run.ctx


def test_c7_flag_off_with_the_recall_daemon_down_still_takes_the_fallback(home, monkeypatch):
    """Row 7, OFF: unchanged -- the fallback is called. Mutation: skipping it on the OFF path."""
    make_surface_store(home)
    set_config(home, flag=False)
    mod = load_hook(home, monkeypatch, daemon="down")
    calls = []
    mod.query_fts_fallback = lambda *a, **k: calls.append(a) or None
    run_main(mod)
    assert len(calls) == 1


# =========================================================================================
# CONFIG_PATH is the single source of every config.json read of the hook (Borges, #3827)
# =========================================================================================

ALT_CONFIG = {
    "primary_user": "AltUser",
    "auto_surface": {"short_prompt_threshold_chars": 7},
    "recall_suppression": {"k": 9},
    "same_session_echo": {"minutes": 99},
    "person_moment_cooldown": {"seconds": 123},
    "recall_budget": {"total_chars": 777},
    "attached_packs": [{"id": "altpack", "path": "/alt/pack", "enabled": True}],
}

CONFIG_READERS = [
    ("get_user_name", lambda m: m.get_user_name(), "the human", "AltUser"),
    ("_get_auto_surface_config", lambda m: m._get_auto_surface_config()["short_prompt_threshold_chars"], 100, 7),
    ("_get_suppression_config", lambda m: m._get_suppression_config()["k"], 5, 9),
    ("_get_echo_guard_config", lambda m: m._get_echo_guard_config()["minutes"], 10, 99.0),
    ("_get_person_moment_cooldown_config", lambda m: m._get_person_moment_cooldown_config()["seconds"], None, 123.0),
    ("_get_budget_config", lambda m: m._get_budget_config()["total_chars"], None, 777),
    ("_get_attached_packs", lambda m: [p["id"] for p in m._get_attached_packs()], [], ["altpack"]),
]


@pytest.mark.parametrize("name,read,default,alt", CONFIG_READERS, ids=[r[0] for r in CONFIG_READERS])
def test_config_path_is_the_single_source_for_each_reader(name, read, default, alt, home, monkeypatch, tmp_path):
    """Re-pointing the module-level CONFIG_PATH at another config.json changes what EACH config
    reader of the hook sees (the A6 arm re-points only that constant); with CONFIG_PATH untouched
    the reader still sees $ENGRAM_HOME/config.json (here: absent -> its default). Mutation: any
    reader rebuilding os.path.join(ENGRAM_HOME, "config.json") itself -> it reads the default
    after the re-point -> red."""
    make_surface_store(home)
    set_config(home, flag=None)
    mod = load_hook(home, monkeypatch)
    before = read(mod)
    if default is not None:
        assert before == default, before
    other = tmp_path / "other"
    other.mkdir()
    (other / "config.json").write_text(json.dumps(ALT_CONFIG))
    monkeypatch.setattr(mod, "CONFIG_PATH", str(other / "config.json"))
    assert read(mod) == alt != before


def test_config_path_is_the_single_source_for_the_in_context_exclusion_switch(home, monkeypatch, tmp_path):
    """main() asks `in_context_exclusion_enabled` for its config's HOME directory: it is derived
    from CONFIG_PATH, not from ENGRAM_HOME, so the re-point reaches the kill switch too. Mutation:
    passing ENGRAM_HOME -> the spy sees the real home -> red."""
    make_surface_store(home)
    set_config(home, flag=False)
    mod = load_hook(home, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    (other / "config.json").write_text(json.dumps({"recall_in_context_exclusion": {"enabled": True}}))
    monkeypatch.setattr(mod, "CONFIG_PATH", str(other / "config.json"))
    asked = []
    monkeypatch.setattr(mod, "in_context_exclusion_enabled", lambda h: asked.append(str(h)) or False)
    run_main(mod)
    assert asked == [str(other)], asked


def test_no_inline_config_json_join_remains_in_the_hook():
    """Static guard: the only `os.path.join(ENGRAM_HOME, "config.json")` left is the CONFIG_PATH
    definition itself. Mutation: reintroducing an inline join -> red."""
    src = _HOOK_PATH.read_text()
    assert len(re.findall(r'os\.path\.join\(ENGRAM_HOME,\s*"config\.json"\)', src)) == 1
    assert re.search(r'^CONFIG_PATH = os\.path\.join\(ENGRAM_HOME, "config\.json"\)', src, re.M)


# =========================================================================================
# a refusal confined to the focused-ids template is recorded, not silent (Borges, #3827)
# =========================================================================================

FOCUSED_SQL = "SELECT id FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1 ORDER BY focused_at"
REFUSALS = ["refused_at_load", "unregistered_template", "result_too_large"]


def refuse_only_focused(monkeypatch, reason):
    real = ec.read

    def fake(method, params, **k):
        if method == "read" and (params or {}).get("sql") == FOCUSED_SQL:
            return ec.Rejected(reason=reason, detail="template-scoped", remedy="n/a")
        return real(method, params, **k)
    monkeypatch.setattr(ec, "read", fake)


@pytest.mark.parametrize("probe_live", [False, True], ids=["no-probe", "probe-live"])
@pytest.mark.parametrize("reason", REFUSALS)
def test_focused_ids_template_refusal_is_recorded_once_and_the_fire_still_works(reason, probe_live, home, monkeypatch):
    """Flag ON, exclusion ON, the owner refuses ONLY the focused-ids template: the fire records
    exactly ONE owner-failure line at site `_focused_ids` (kind rejected; with the probe live two
    resolvers read it and it is still one line), and fails open as before: nothing is dropped (the
    focused node's line is still rendered), the other owner reads work (the dormant pointer is
    there), exit 0, and no other site is blamed. Mutation: removing the recording in the hook
    (or in _hooklib) -> zero lines -> red."""
    make_surface_store(home)
    set_config(home, flag=True, exclusion=True)
    if probe_live:
        (Path(str(home / "h")) / ".engram" / "probe" / "mods-recall" / SID / "routes").mkdir(parents=True)
    mod = load_hook(home, monkeypatch)
    refuse_only_focused(monkeypatch, reason)
    with FakeOwner(home):
        run = run_main(mod)
    assert run.exc == "SystemExit(0)", run.exc
    lines = re.findall(r"^engram-surface-hook: owner read failed: (\w+) at (\w+)" + OWNER_LINE_TAIL + "$", run.stderr, re.M)
    assert lines == [("rejected", "_focused_ids")], (lines, run.stderr)
    assert "focused summary two" in run.ctx and "saved focus-set 'dormant-hit'" in run.ctx, run.ctx


def test_focused_ids_failure_with_the_flag_off_records_nothing_and_changes_nothing(home, monkeypatch):
    """Flag OFF, exclusion ON, the direct focused read fails: exactly as before -- no owner line,
    no owner stderr, the fire exits 0 and drops nothing. Mutation: passing the recorder on the
    direct path -> an `owner read failed` line appears -> red."""
    make_surface_store(home)
    set_config(home, flag=False, exclusion=True)
    mod = load_hook(home, monkeypatch)
    hl = sys.modules["_hooklib"]

    def boom(*a, **k):
        raise sqlite3.OperationalError("focused read down (test)")
    real = hl.connect_regular_ro

    def selective(path, *a, **k):
        # only the focused-ids read (timeout 0.2) fails; every other direct read works
        if k.get("timeout") == 0.2:
            boom()
        return real(path, *a, **k)
    monkeypatch.setattr(hl, "connect_regular_ro", selective)
    run = run_main(mod)
    assert run.exc == "SystemExit(0)"
    assert "owner read failed" not in run.stderr and "could NOT be read" not in run.ctx
    assert "focused summary two" in run.ctx


# =========================================================================================
# registry / ledger
# =========================================================================================

def test_statements_sent_to_the_owner_are_registered_templates_or_typed_ops(home, monkeypatch):
    """Every statement a flag-ON fire sends is a registered template (byte for byte) or the typed
    df / chain / ping op. Mutation: a reworded SQL string in the owner path -> unregistered."""
    make_surface_store(home, restore="set")
    set_config(home, flag=True, exclusion=True)
    mod = load_hook(home, monkeypatch)
    with FakeOwner(home) as owner:
        run_main(mod)
    from _fake_owner import statement_of
    stmts = [statement_of(r["method"], r.get("params"))[0] for r in owner.requests if r["method"] != "ping"]
    assert stmts
    assert all(s in registered_sql() or s in (SQL_VOCAB, "chain") for s in stmts), stmts
    # the in-context-exclusion-ON path IS in this fire (set_config exclusion=True): its focused-ids
    # SQL was sent and is registered, so template drift on it reddens here; so is the row-6 read.
    assert SQL_FOCUSED in stmts and SQL_FOCUSED in registered_sql(), stmts
    assert SQL_LIVENESS not in stmts and SQL_EMBED in stmts, stmts   # row 6 adds no statement


def test_ledger_row_4_is_behind_flag_with_the_true_site_table_and_the_row_8_remainder():
    """docs/internal/2652-migration-ledger.md section 3, the surface hook's row: `behind-flag`, the flag
    name, and `_advance_person_moment_cursor` named as the remainder that stays direct.
    Mutation: leaving the row 'direct' -> red."""
    text = (REPO_ROOT / "docs" / "internal" / "2652-migration-ledger.md").read_text()
    row = next(ln for ln in text.splitlines() if ln.startswith("| 4 |") and "engram-surface-hook.py" in ln)
    assert "behind-flag" in row and "engramd.surface_reads_via_owner" in row
    assert "_advance_person_moment_cursor" in row and "remainder" in row.lower()
    assert "U10" in row and "3821" in row
