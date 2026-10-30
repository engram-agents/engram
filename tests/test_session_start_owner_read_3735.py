"""Contract tests for U9 (#3735, part of #2652): engram-session-start-hook.py reads the
store through the engramd owner behind `engramd.session_start_reads_via_owner` (default OFF).

Written BEFORE the implementation (docs/internal/CONTRACT-FIRST-TESTS.md). Design seat: Borges; ruling on
the calibration block: option A (issue comment on #3735). Modelled on U6b's
tests/test_tripwire_owner_read_3665.py and U8's tests/test_focused_ids_owner_3710.py.

SCOPE (option A). Six functions move behind the flag: `_check_backup_staleness` (its `count(*)`
sub-read), `_check_embedding_restore_flag`, `focus_block`, `saved_focus_sets_block`,
`session_checkpoint_block`, `sleep_status_block`. The calibration block in `main()`
(`EngramClient().call("engram_stats", ...)`) stays a direct store opener and is its own unit (#3741);
cell (a2) pins that residue. A flag-ON fire is NOT zero-open.

  (a)  flag ON, full fire, DEPLOYED (flattened) layout, owner up: zero store opens ATTRIBUTED to the
       six functions (stack-attributed audit instrument), every block delivered, the owner asked the
       registered statements;
  (a2) characterization: the calibration block is the only remaining opener, and on a fresh home it
       (not the six) builds the schema -- flip this cell deliberately when #3741 moves the block;
  (b)  per function, owner and direct return the same result on one store (by-index rewrite parity);
  (c)  per function, every owner failure gives that function's degraded result, ONE notice, one
       stderr line, no direct open afterwards;
  (d)  cold start: the wait rule (once per fire, only for Unavailable/down before any success,
       bounded), shown as a RUN of the migrated hook with and without the wait;
  (e)  store absent: no function creates a file (flag OFF and ON);
  (f)  flag OFF: byte-identical to dev 4f475cf4 (apart from (e));
  (g)  "S6 class": the local store file is never consulted on the flag-ON path;
  (h)  every statement sent is a registered template, byte for byte;
  (i)  the flag gate: only the JSON boolean true;
  (j)  budgets; (k) the notice; (l) standalone callers stay direct.

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import inspect
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
from unittest.mock import MagicMock, patch

import pytest

from _fake_owner import FakeOwner, registered_sql
from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)
_HOOKS_SRC = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
_HOOK_PATH = _HOOKS_SRC / "engram-session-start-hook.py"
BASE_REF = "4f475cf4"  # origin/dev the U9 branch was cut from (the unmodified hook)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402
import engram_core  # noqa: E402

from test_tripwire_owner_read_3665 import (  # noqa: E402,F401 -- fixtures and helpers reused
    OWNER_KINDS,
    _add,
    _restore_imports,
    flattened_root,
    home,
    make_store,
    owner_of,
    spying_read,
)

FLAG_KEY = "session_start_reads_via_owner"
NOTICE_KEY = "session-start-owner"
STAMP_NAME = "probe-unknown-notice.json"
HOOK_REL = "hooks/claude/engram-session-start-hook.py"
SIX = (
    "_check_backup_staleness",
    "_check_embedding_restore_flag",
    "focus_block",
    "saved_focus_sets_block",
    "session_checkpoint_block",
    "sleep_status_block",
)
FOUR_S6 = ("_check_embedding_restore_flag", "focus_block", "saved_focus_sets_block", "session_checkpoint_block")
CHECKPOINT_FROM = "2026-10-12T00:00:00Z"
NOTICE_PHRASE = "engramd owner read failed"
# the owner line on stderr (the embedding probe's own pre-existing UNKNOWN line also carries the words)
# (#4271: the line may end with `, bound N ms` and/or `; failed E ms into the B ms fire budget`)
OWNER_STDERR_RE = re.compile(
    r"^engram-session-start-hook: owner read failed: (\w+) at (\w+)"
    r"(?:, bound \d+ ms)?(?:; failed \d+ ms into the \d+ ms fire budget)?$", re.M)
WAIT_PHRASE = "was not ready at the first read"
UNAVAILABLE_REASONS_NO_WAIT = ["timeout", "protocol", "busy", "busy_or_stuck", "local", "internal", "contract_version"]

SQL_BACKUP = "SELECT count(*) FROM nodes"
SQL_EMBED = "SELECT not_fully_restored, null_count, cause FROM embedding_restore_state WHERE singleton_key = 1"
SQL_FOCUS = "SELECT id, claim, focus_reason, focused_at FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1 ORDER BY focused_at"
SQL_ACTIVE = "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
SQL_SETS = "SELECT name, node_ids, description, created_at FROM focus_sets ORDER BY name ASC"
# U9b (#3761): the checkpoint reader sends TWO statements (a COUNT, then a capped list); their
# text is read from the committed registry (never retyped) and told apart by shape. The
# independent text pin of the two statements lives in tests/test_session_start_checkpoint_u9b_3761.py.
_CK_SQLS = [t["sql"] for t in json.loads((Path(__file__).resolve().parent.parent / "src" / "engram"
            / "engramd_read_templates.json").read_text())["templates"]
            if "hooks/claude/engram-session-start-hook.py:session_checkpoint_block" in t["sites"]]
SQL_CK_COUNT = next((s for s in _CK_SQLS if s.startswith("SELECT COUNT(*) FROM nodes WHERE created_at >= ?")), "<no count template>")
SQL_CK_LIST = next((s for s in _CK_SQLS if " LIMIT " in s), "<no list template>")
SQL_SLEEP1 = "SELECT COUNT(*) FROM nodes"
SQL_SLEEP2 = "SELECT COUNT(*) FROM nodes WHERE created_at > ?"
ALL_NINE = {SQL_BACKUP, SQL_EMBED, SQL_FOCUS, SQL_ACTIVE, SQL_SETS, SQL_CK_COUNT, SQL_CK_LIST, SQL_SLEEP1, SQL_SLEEP2}
FN_SQL = {
    "_check_backup_staleness": {SQL_BACKUP},
    "_check_embedding_restore_flag": {SQL_EMBED},
    "focus_block": {SQL_FOCUS},
    "saved_focus_sets_block": {SQL_ACTIVE, SQL_SETS},
    "session_checkpoint_block": {SQL_CK_COUNT, SQL_CK_LIST},
    "sleep_status_block": {SQL_SLEEP1, SQL_SLEEP2},
}


# =========================================================================================
# store / config / hook helpers
# =========================================================================================

def set_flag(home: Path, flag=True, raw=None) -> None:
    """flag=... : key absent; flag=None : no config file at all; raw : verbatim file text."""
    cfg = home / "config.json"
    if raw is not None:
        cfg.write_text(raw)
    elif flag is None:
        cfg.unlink(missing_ok=True)
    elif flag is ...:
        cfg.write_text("{}")
    else:
        cfg.write_text(json.dumps({"engramd": {FLAG_KEY: flag}}))


_EMBED_DDL = """CREATE TABLE IF NOT EXISTS embedding_restore_state (
    singleton_key INTEGER PRIMARY KEY CHECK (singleton_key = 1),
    not_fully_restored INTEGER NOT NULL DEFAULT 0, null_count INTEGER NOT NULL DEFAULT 0,
    cause TEXT, updated_at TEXT)"""


def set_embed(db: Path, flagged=0, null_count=0, cause=None) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(_EMBED_DDL)
    conn.execute("DELETE FROM embedding_restore_state")
    conn.execute("INSERT INTO embedding_restore_state (singleton_key, not_fully_restored, null_count, cause) "
                 "VALUES (1, ?, ?, ?)", (flagged, null_count, cause))
    conn.commit()
    conn.close()


def build_store(home: Path) -> Path:
    """make_store plus everything the six functions read: >=100 nodes, three focused nodes (two
    sharing a reason, one stale pin), saved focus-sets (one active), a checkpoint window holding a
    superseded, a retracted and a blank node, an embedding_restore_state row (clear)."""
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    for i in range(60, 130):
        _add(conn, f"ob_{i:04d}", "observation", f"filler{i} extra baseline text")
    # twelve nodes after the stale sleep marker (read 2 needs >= SLEEP_DEBT_MIN_NODES_THRESHOLD)
    for i in range(12):
        _add(conn, f"sm_{i:04d}", "observation", f"since-marker node {i}")
        conn.execute("UPDATE nodes SET created_at = '2026-10-05T00:00:0%d' WHERE id = ?" % (i % 10,), (f"sm_{i:04d}",))
    conn.execute("UPDATE nodes SET focused_at = '2026-10-11T08:00:00Z', focus_reason = 'shared rationale' WHERE id = 'ob_0001'")
    conn.execute("UPDATE nodes SET focused_at = '2026-10-11T09:00:00Z', focus_reason = 'shared rationale' WHERE id = 'ob_0002'")
    conn.execute("UPDATE nodes SET focused_at = '2026-01-01T00:00:00Z', focus_reason = NULL WHERE id = 'ob_0003'")
    conn.execute("INSERT INTO focus_sets (name, node_ids, description, created_at) VALUES "
                 "('set-a', '[\"ob_0001\"]', 'alpha set', '2026-10-01T00:00:00Z')")
    conn.execute("INSERT INTO focus_sets (name, node_ids, description, created_at) VALUES "
                 "('set-b', '[\"ob_0002\", \"ob_0003\"]', NULL, '2026-10-02T00:00:00Z')")
    conn.execute("INSERT INTO focus_sets (name, node_ids, description, created_at) VALUES "
                 "('set-c', 'not json', '', '2026-10-03T00:00:00Z')")
    conn.execute("UPDATE focus_state SET active_set_name = 'set-b' WHERE singleton_key = 1")
    _add(conn, "ck_0001", "observation", "live checkpoint claim", recall_summary="checkpoint summary one")
    _add(conn, "ck_0002", "observation", "replaced checkpoint claim", current=0)
    _add(conn, "ck_0003", "observation", "never valid claim", current=0)
    _add(conn, "ck_0004", "observation", "", recall_summary=None)
    for i, nid in enumerate(("ck_0001", "ck_0002", "ck_0003", "ck_0004")):
        conn.execute("UPDATE nodes SET created_at = ? WHERE id = ?", (f"2026-10-12T01:0{i}:00Z", nid))
    conn.execute("UPDATE nodes SET status = 'retracted' WHERE id = 'ck_0003'")
    conn.commit()
    conn.close()
    set_embed(db, 0, 0, None)
    return db


def build_empty_store(home: Path) -> Path:
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


def sql_set_of_function(fn: str) -> set:
    doc = json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())
    return {t["sql"] for t in doc["templates"] if f"{HOOK_REL}:{fn}" in t["sites"]}


@pytest.fixture(autouse=True)
def stub_modules(monkeypatch):
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub_ct)

    class _NoCalibration:
        def call(self, *a, **k):
            return {}
    monkeypatch.setitem(sys.modules, "engram_client", MagicMock(EngramClient=_NoCalibration))


def stub_calibration_reads(monkeypatch):
    """#3741: the in-process twin of `stub_modules`' `_NoCalibration` for the flag-ON path -- the
    calibration block's owner request is answered locally with an empty distribution (renders no
    block), so a cell about the six blocks / the wait is not about calibration."""
    monkeypatch.setattr(
        ec.OwnerReadConnection, "confidence_by_type",
        lambda self, modes: {"current_turn": 0, "by_mode": {m: {"by_type": {}} for m in modes}},
        raising=False)


def quiet(mod):
    """Make a hook module hermetic: nothing but the store/state under ENGRAM_HOME is read."""
    mod.detect_harness = lambda: "claude-code"
    mod.tool_capability_index_block = lambda: ""
    mod.tool_capability_short_block = lambda: ""
    mod._check_mcp_health = lambda h: (True, None)
    mod._check_viz_server_health = lambda h: ("not-configured", None)
    mod.piece_c_git_log = lambda sid: ""
    mod.forum_monitor_block = lambda: ""
    mod.forum_updates_monitor_block = lambda: ""
    mod.peer_topology_block = lambda a: ""
    mod.starred_block = lambda h: ""
    return mod


def load_ss(home: Path, monkeypatch, path: Path = _HOOK_PATH, hermetic: bool = True):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.setenv("HOME", str(home / "userhome"))
    for var in ("ENGRAM_RUNTIME_DIR", "CLAUDE_PLUGIN_ROOT", "ENGRAM_SESSION_ROLE"):
        monkeypatch.delenv(var, raising=False)
    (home / "userhome").mkdir(exist_ok=True)
    spec = importlib.util.spec_from_file_location("engram_session_start_hook_3735", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return quiet(mod) if hermetic else mod


class Run:
    def __init__(self, stdout, stderr, exc, elapsed):
        self.stdout, self.stderr, self.exc, self.elapsed = stdout, stderr, exc, elapsed

    @property
    def ctx(self) -> str:
        if not self.stdout.strip():
            return ""
        return json.loads(self.stdout)["hookSpecificOutput"]["additionalContext"]


def stdin_for(home: Path, source: str = "startup", session: str = "s1") -> dict:
    return {"session_id": session, "transcript_path": str(home / "t.jsonl"), "source": source}


def run_main(mod, home: Path, source: str = "startup", session: str = "s1") -> Run:
    out, err = io.StringIO(), io.StringIO()
    exc = None
    t0 = time.perf_counter()
    with patch("sys.stdin", io.StringIO(json.dumps(stdin_for(home, source, session)))), \
            patch("sys.stdout", out), patch("sys.stderr", err):
        try:
            mod.main()
        except SystemExit as e:
            exc = f"SystemExit({e.code})"
        except Exception as e:  # noqa: BLE001 -- a crash is part of the behaviour under test
            exc = f"{type(e).__name__}: {e}"
    return Run(out.getvalue(), err.getvalue(), exc, time.perf_counter() - t0)


def seed_marker(home: Path, session: str = "s1", injected_through=CHECKPOINT_FROM) -> None:
    (home / "sessions").mkdir(exist_ok=True)
    (home / "sessions" / f"{session}.json").write_text(json.dumps({
        "session_id": session, "transcript_path": str(home / "t.jsonl"), "source": "startup",
        "started_at": "2026-10-12T00:00:00+00:00", "role": "interactive",
        "session_started_at": "2026-10-12T00:00:00+00:00", "injected_through": injected_through,
        "compaction_count": 0}))


def set_sleep_marker(home: Path, completed_at: str | None) -> None:
    p = home / "sessions" / "last-sleep-success.json"
    (home / "sessions").mkdir(exist_ok=True)
    if completed_at is None:
        p.unlink(missing_ok=True)
    else:
        p.write_text(json.dumps({"completed_at": completed_at, "turn_advanced_to": 7}))


STALE_MARKER = "2026-10-01T00:00:00+00:00"  # >28 h before any run date of this suite

BLOCK_MARKERS = {
    "focus": "Focused nodes",
    "saved": "Saved focus-sets",
    "checkpoint": "Nodes created since checkpoint",
    "sleep": "[SLEEP STATUS",
    "backup": "Git backup: knowledge.sql not found",
}


def full_fire_setup(home: Path, monkeypatch, *, sleep_marker=None):
    build_store(home)
    set_flag(home, True)
    seed_marker(home)
    set_sleep_marker(home, sleep_marker)
    return load_ss(home, monkeypatch)


class Fire:
    """Enter / leave the owner state of one fire around direct calls to the six functions."""

    def __init__(self, mod, on=True):
        self.mod, self.on = mod, on

    def __enter__(self):
        self.mod._OWNER_RUN = self.mod._fresh_owner_run(self.on)
        return self.mod._OWNER_RUN

    def __exit__(self, *exc):
        self.mod._OWNER_RUN = None


def call_fn(mod, name: str, home: Path):
    h = str(home)
    if name == "_check_backup_staleness":
        return mod._check_backup_staleness(h)
    if name == "_check_embedding_restore_flag":
        return mod._check_embedding_restore_flag(h)
    if name == "focus_block":
        return mod.focus_block(h)
    if name == "saved_focus_sets_block":
        return mod.saved_focus_sets_block(h)
    if name == "session_checkpoint_block":
        return mod.session_checkpoint_block(h, CHECKPOINT_FROM)
    if name == "sleep_status_block":
        return mod.sleep_status_block()
    raise AssertionError(name)


def norm_home(text: str, home: Path) -> str:
    return text.replace(str(home), "<HOME>")


# --- attribution instrument (in-process) -------------------------------------------------

class AttrOpens:
    """Record every open of the store path made in the hook's thread, attributed to the INNERMOST
    function of the hook file on the stack (lambdas skipped). With `raises` the open raises (a
    function that opens directly then degrades silently, so the record is the evidence)."""

    def __init__(self, mod, home: Path, raises: bool = True):
        self.mod, self.raises = mod, raises
        self.hook_file = os.path.realpath(mod.__file__)
        self.store = os.path.realpath(str(home / "knowledge.db"))
        self.record = []
        self.targets = []  # (caller, the string sqlite3.connect was given), sqlite3.connect only
        self._thread = threading.get_ident()

    def _caller(self):
        f = sys._getframe(2)
        while f is not None:
            if os.path.realpath(f.f_code.co_filename) == self.hook_file and not f.f_code.co_name.startswith("<"):
                return f.f_code.co_name
            f = f.f_back
        return None

    def _wrap(self, name, real):
        def f(*a, **k):
            if threading.get_ident() != self._thread:
                return real(*a, **k)
            target = str(a[0]) if a else ""
            path = os.path.realpath(target[5:].split("?", 1)[0] if target.startswith("file:") else target)
            if path.startswith(self.store):
                self.record.append((name, self._caller()))
                if name == "sqlite3.connect":
                    self.targets.append((self._caller(), target))
                if self.raises:
                    raise AssertionError(f"direct open via {name}")
            return real(*a, **k)
        return f

    def install(self, monkeypatch):
        hl = sys.modules["_hooklib"]
        monkeypatch.setattr(sqlite3, "connect", self._wrap("sqlite3.connect", sqlite3.connect))
        monkeypatch.setattr(sqlite3.dbapi2, "connect", self._wrap("sqlite3.dbapi2.connect", sqlite3.dbapi2.connect))
        monkeypatch.setattr(hl, "connect_regular_ro", self._wrap("_hooklib.connect_regular_ro", hl.connect_regular_ro))
        monkeypatch.setattr(self.mod, "connect_regular_ro", self._wrap("hook.connect_regular_ro", self.mod.connect_regular_ro))
        return self


# --- scripted client ---------------------------------------------------------------------

def _unavailable(reason, home=None):
    return ec.Unavailable(socket_path=str((home or Path("/nonexistent")) / "run" / "engramd.sock"), bound=1.0,
                          agent_steps=["AGENT-STEP"], human_steps=["HUMAN-STEP"], detail="no answer", reason=reason)


def _rejected(reason="unregistered_template"):
    return ec.Rejected(reason=reason, detail="not registered", remedy="register the template")


class Script:
    """Replaces `ec.read`. handler(method, sql, n) -> an ec result, or None to delegate to the
    real client. Records every call as (method, sql, timeout, socket_path)."""

    def __init__(self, monkeypatch, handler):
        self.handler = handler
        self.calls = []
        self.real = ec.read
        monkeypatch.setattr(ec, "read", self)

    def __call__(self, method, params, **k):
        sql = (params or {}).get("sql") if method == "read" else None
        self.calls.append((method, sql, k.get("timeout"), k.get("socket_path")))
        res = self.handler(method, sql, len(self.calls))
        if res is None:
            return self.real(method, params, **k)
        return res

    @property
    def pings(self):
        return [c for c in self.calls if c[0] == "ping"]

    @property
    def statements(self):
        return [c[1] for c in self.calls if c[0] == "read"]


class LateOwner:
    """An owner that starts `delay` seconds after __enter__ (a cold start)."""

    def __init__(self, kind, home, delay):
        self.kind, self.home, self.delay = kind, home, delay
        self._cm = None
        self.ready = threading.Event()

    def _run(self):
        time.sleep(self.delay)
        self._cm = owner_of(self.kind, self.home)
        self._cm.__enter__()
        self.ready.set()

    def __enter__(self):
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._t.join(15)
        if self._cm is not None:
            self._cm.__exit__(*exc)


class HungServer:
    """Accepts connections and never answers (a hung owner)."""

    def __init__(self, home: Path):
        self.path = home / "run" / "engramd.sock"
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(self.path))
        self.srv.listen(16)
        self.srv.settimeout(0.1)
        self.held = []
        self.stop_ev = threading.Event()
        self.t = threading.Thread(target=self._serve, daemon=True)
        self.t.start()

    def _serve(self):
        while not self.stop_ev.is_set():
            try:
                c, _ = self.srv.accept()
                self.held.append(c)
            except socket.timeout:
                continue
            except OSError:
                return

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop_ev.set()
        self.t.join(2)
        for c in self.held:
            c.close()
        self.srv.close()
        self.path.unlink(missing_ok=True)


# =========================================================================================
# subprocess runner (a real process: the DEPLOYED layout, the audit-event instrument)
# =========================================================================================

RUNNER = r'''
import importlib.util, io, json, os, sys, time, types
hook_path, scen_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
scen = json.load(open(scen_path))
home = scen["home"]
store = os.path.realpath(os.path.join(home, "knowledge.db"))
HOOK = os.path.realpath(hook_path)
opens = []

def _path_of(raw):
    raw = str(raw)
    if raw.startswith("file:"):
        raw = raw[5:]
    return os.path.realpath(raw.split("?", 1)[0])

def _chain():
    f = sys._getframe(2)
    names = []
    while f is not None:
        if os.path.realpath(f.f_code.co_filename) == HOOK and not f.f_code.co_name.startswith("<"):
            names.append(f.f_code.co_name)
        f = f.f_back
    return names

def _audit(event, args):
    try:
        if event == "sqlite3.connect":
            raw = str(args[0])
            p = _path_of(raw)
            if p.startswith(store):
                chain = _chain()
                opens.append({"event": "connect", "raw": raw, "ro": "mode=ro" in raw,
                              "exists_before": os.path.exists(p), "caller": chain[0] if chain else None,
                              "chain": chain, "t": time.time()})
        elif event == "open":
            a0 = args[0]
            if isinstance(a0, (str, bytes, os.PathLike)):
                p = _path_of(os.fsdecode(a0))
                if p.startswith(store):
                    chain = _chain()
                    opens.append({"event": "open", "raw": os.fsdecode(a0), "ro": False,
                                  "exists_before": os.path.exists(p), "caller": chain[0] if chain else None,
                                  "chain": chain, "t": time.time()})
    except Exception:
        pass

sys.modules["context_tracker"] = types.SimpleNamespace(write_baseline=lambda **k: None)
if scen.get("stub_calibration", True):
    class _C:
        def call(self, *a, **k):
            return {}
    sys.modules["engram_client"] = types.SimpleNamespace(EngramClient=_C)
calls = []
if scen.get("spy_read"):
    root = scen["runtime_dir"]
    sys.path.insert(0, root)
    import engramd_client as _ec
    _real = _ec.read
    def _spy(method, params, **k):
        calls.append([method, (params or {}).get("sql") if method == "read" else None])
        return _real(method, params, **k)
    _ec.read = _spy
sys.addaudithook(_audit)
spec = importlib.util.spec_from_file_location("ss_hook_under_test", hook_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.detect_harness = lambda: "claude-code"
mod.tool_capability_index_block = lambda: ""
mod.tool_capability_short_block = lambda: ""
mod._check_mcp_health = lambda h: (True, None)
mod._check_viz_server_health = lambda h: ("not-configured", None)
mod.piece_c_git_log = lambda sid: ""
mod.forum_monitor_block = lambda: ""
mod.forum_updates_monitor_block = lambda: ""
mod.peer_topology_block = lambda a: ""
mod.starred_block = lambda h: ""
for k, v in scen.get("module_attrs", {}).items():
    setattr(mod, k, v)
if scen.get("stub_calibration", True) and hasattr(mod, "_import_engramd_client"):  # the oracle hook has none
    # #3741: with the flag ON the calibration block asks the owner (`confidence_by_type`); "stubbed
    # away" now also means that request is answered locally with an empty distribution.
    _orig_import = mod._import_engramd_client
    def _stub_import():
        m = _orig_import()
        m.OwnerReadConnection.confidence_by_type = (
            lambda self, modes: {"current_turn": 0, "by_mode": {md: {"by_type": {}} for md in modes}})
        return m
    mod._import_engramd_client = _stub_import
sys.stdin = io.StringIO(json.dumps(scen["stdin"]))
real_out, real_err = sys.stdout, sys.stderr
sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
rc = 0
t0 = time.perf_counter()
try:
    mod.main()
except SystemExit as e:
    rc = e.code or 0
except BaseException as e:
    rc = "EXC " + repr(e)
elapsed = time.perf_counter() - t0
o, e = sys.stdout.getvalue(), sys.stderr.getvalue()
sys.stdout, sys.stderr = real_out, real_err
json.dump({"rc": rc, "stdout": o, "stderr": e, "elapsed": elapsed, "opens": opens, "calls": calls,
           "client_imported": "engramd_client" in sys.modules and not scen.get("spy_read"),
           "store_exists": os.path.exists(store),
           "store_size": os.path.getsize(store) if os.path.exists(store) else None}, open(out_path, "w"))
'''


def run_hook_subprocess(hook_path: Path, home: Path, tmp: Path, *, source="startup", stub_calibration=True,
                        spy_read=False, runtime_dir=None, module_attrs=None, timeout=90) -> dict:
    runner = tmp / "runner3735.py"
    runner.write_text(RUNNER)
    scen = tmp / "scen3735.json"
    scen.write_text(json.dumps({
        "home": str(home), "stdin": stdin_for(home, source), "stub_calibration": stub_calibration,
        "spy_read": spy_read, "runtime_dir": str(runtime_dir or ""), "module_attrs": module_attrs or {}}))
    out = tmp / "out3735.json"
    out.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH", "ENGRAM_SESSION_ROLE")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(tmp / "subhome")
    (tmp / "subhome").mkdir(exist_ok=True)
    t0 = time.perf_counter()
    launch_ts = time.time()
    r = subprocess.run([sys.executable, str(runner), str(hook_path), str(scen), str(out)],
                       capture_output=True, text=True, env=env, cwd=str(tmp), timeout=timeout)
    wall = time.perf_counter() - t0
    assert out.exists(), (r.returncode, r.stdout, r.stderr)
    data = json.loads(out.read_text())
    data["wall"] = wall
    data["launch_ts"] = launch_ts
    data["ctx"] = json.loads(data["stdout"])["hookSpecificOutput"]["additionalContext"] if data["stdout"].strip() else ""
    return data


def attributed_to_six(opens) -> list:
    return [o for o in opens if o["caller"] in SIX]


def _oracle_source():
    r = subprocess.run(["git", "show", f"{BASE_REF}:src/engram/hooks/claude/engram-session-start-hook.py"],
                       capture_output=True, text=True, cwd=str(REPO_ROOT))
    return r.stdout if r.returncode == 0 and r.stdout else None


def oracle_hook(tmp: Path) -> Path:
    """The UNMODIFIED hook (`git show 4f475cf4:<path>`) as a sibling of copies of the hook helpers
    in a throwaway directory, so its imports resolve. Skips when the base object is not in the clone."""
    src = _oracle_source()
    if src is None:
        pytest.skip(f"base commit {BASE_REF} not available in this clone")
    d = tmp / "oracle-hooks"
    d.mkdir(exist_ok=True)
    for p in _HOOKS_SRC.glob("*.py"):
        shutil.copy(p, d / p.name)
    (d / "engram-session-start-hook.py").write_text(src)
    return d / "engram-session-start-hook.py"


# =========================================================================================
# (a) ZERO OPENS attributed to the six functions, deployed layout
# =========================================================================================

@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_a_flag_on_full_fire_in_the_deployed_layout_has_zero_opens_attributed_to_the_six(
        owner_kind, home, tmp_path):
    """Flag ON, owner up, a REAL process loading the flattened plugin layout (hooks/*.py beside
    the runtime modules, CLAUDE_PLUGIN_ROOT unset): every `sqlite3.connect` / file open of the store
    path is captured by Python audit events and attributed by stack to the calling hook function.
    The calibration block is stubbed away here (cell (a2) pins it), so NO open at all may remain,
    and in particular none attributed to the six. Every block is delivered. Two fires (no sleep
    marker; stale marker) so that all nine registered statements are needed; with the fake owner
    its request log shows each of the nine was asked.
    Mutation: leave ONE function on the direct path (M1 x6) -> an attributed open -> red."""
    build_store(home)
    set_flag(home, True)
    seed_marker(home)
    root = flattened_root(tmp_path)
    hook = root / "hooks" / "engram-session-start-hook.py"
    seen = set()
    with owner_of(owner_kind, home) as owner:
        for marker in (None, STALE_MARKER):
            set_sleep_marker(home, marker)
            out = run_hook_subprocess(hook, home, tmp_path)
            assert out["rc"] == 0, out
            assert attributed_to_six(out["opens"]) == [], out["opens"]
            assert out["opens"] == [], out["opens"]
            for key in ("focus", "saved", "checkpoint", "sleep", "backup"):
                assert BLOCK_MARKERS[key] in out["ctx"], (key, out["ctx"], out["stderr"])
            assert NOTICE_PHRASE not in out["ctx"] and not OWNER_STDERR_RE.findall(out["stderr"])
        if owner_kind == "fake":
            seen = {(r.get("params") or {}).get("sql") for r in owner.requests if r.get("method") == "read"}
    if owner_kind == "fake":
        assert seen == ALL_NINE, sorted(ALL_NINE ^ seen)


@requires_linux_owner
def test_a2_post_3741_the_calibration_block_is_served_by_the_owner_and_nothing_opens(
        home, tmp_path, monkeypatch):
    """FLIPPED DELIBERATELY by #3741 (was `test_a2_characterization_the_calibration_block_is_the_only_
    remaining_opener`, which pinned option A: the calibration block still opened the store directly
    and, on a fresh home, built the schema). Post-#3741, flag ON with the REAL calibration block (not
    stubbed) and the owner up: the hook opens the store ZERO times, asks the owner for
    `confidence_by_type` (seen through a spy on the client's `read`), and on a FRESH home (no
    store, no owner) nothing is built: no store file, no open at all.
    It also pins WHAT the hook asks and renders: exactly one `confidence_by_type` request whose params
    are exactly `{"modes": ["all", "7-turn"]}` (the spy is widened here to record params), and a block
    equal to the one the current code renders for the same store (the history-free oracle of
    test_calibration_owner_op_3741, which also seeds confidence rows so the block is not empty).
    Carries the owner-platform marker: it starts a REAL owner.
    Red until #3741 lands (the block still goes through EngramClient and opens directly).
    Mutation: keep the old EngramClient path for flag ON (M1) -> opens -> red."""
    import test_calibration_owner_op_3741 as cal  # lazy: that module imports this one
    spy_line = 'calls.append([method, (params or {}).get("sql") if method == "read" else None])'
    widened = RUNNER.replace(spy_line, spy_line.replace("else None])", "else None, params])"))
    assert widened != RUNNER, "RUNNER's spy line changed shape: params could not be recorded"
    monkeypatch.setattr(sys.modules[__name__], "RUNNER", widened)
    cal.live_store(home, flag=True, turn=41)  # build_store + confidence rows + flag ON + turn 41
    seed_marker(home)
    expected = cal.oracle_calibration_block(home, tmp_path)
    assert cal.CAL_MARK in expected and "turn 41" in expected, expected  # the oracle is not vacuous
    root = flattened_root(tmp_path)
    hook = root / "hooks" / "engram-session-start-hook.py"
    with owner_of("real", home):
        out = run_hook_subprocess(hook, home, tmp_path, stub_calibration=False, spy_read=True,
                                  runtime_dir=root)
    assert out["rc"] == 0, out
    assert out["opens"] == [], out["opens"]
    asked = [c for c in out["calls"] if c[0] == "confidence_by_type"]
    assert len(asked) == 1, out["calls"]
    assert asked[0][2] == {"modes": ["all", "7-turn"]}, asked[0]
    assert cal._calibration_block(out["ctx"]) == cal._calibration_block(expected), (out["ctx"], expected)
    # fresh home: nothing opens and nothing is built (formerly: the calibration block built the schema)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    set_flag(fresh, True)
    out2 = run_hook_subprocess(hook, fresh, tmp_path, stub_calibration=False)
    assert out2["rc"] == 0, out2
    assert out2["opens"] == [], out2["opens"]
    assert out2["store_exists"] is False, out2["store_size"]


# =========================================================================================
# (b) parity: owner == direct, per function
# =========================================================================================

def _direct_and_owner(mod, name, home, owner_kind, monkeypatch, prep=None):
    """-> (direct result, owner result, ec.read calls made on the owner leg)."""
    if prep:
        prep()
    mod._OWNER_RUN = None
    direct = call_fn(mod, name, home)
    seen = spying_read(monkeypatch)
    with owner_of(owner_kind, home):
        with Fire(mod):
            via = call_fn(mod, name, home)
    return direct, via, list(seen)


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
@pytest.mark.parametrize("case", ["sql_absent_with_nodes", "sql_absent_empty_store", "sql_present_fresh", "sql_present_stale"])
def test_b_backup_staleness_owner_equals_direct(case, owner_kind, home, monkeypatch):
    """`_check_backup_staleness`: with knowledge.sql ABSENT the count comes through the owner (a
    store with nodes -> (False, ...); an empty schema -> (True, None)); with knowledge.sql PRESENT it
    is a file comparison and the owner is never asked. Direct and owner agree.
    Mutation: read a different column / skip the count -> the 'with nodes' case differs -> red."""
    if case == "sql_absent_empty_store":
        build_empty_store(home)
    else:
        build_store(home)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    sql = home / "knowledge.sql"
    if case.startswith("sql_present"):
        sql.write_text("-- dump")
        if case.endswith("stale"):
            old = time.time() - 3 * 3600
            os.utime(sql, (old - 4 * 3600, old - 4 * 3600))
    direct, via, calls = _direct_and_owner(mod, "_check_backup_staleness", home, owner_kind, monkeypatch)
    assert via == direct
    if case == "sql_absent_with_nodes":
        assert direct[0] is False and "knowledge.sql not found" in direct[1]
    if case == "sql_absent_empty_store":
        assert direct == (True, None)
    if case.startswith("sql_present"):
        assert [c for c in calls if c[0] == SQL_BACKUP] == []
        assert calls == [], "the file comparison must not read the store"
    else:
        assert [c[0] for c in calls] == [SQL_BACKUP]


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
@pytest.mark.parametrize("row", [(0, 0, None), (1, 5, "interrupted-restore"), (1, -1, "detection-failed: boom")])
def test_b_embedding_restore_flag_owner_equals_direct(row, owner_kind, home, monkeypatch):
    """`_check_embedding_restore_flag`: clear, set (count + cause), and the detection-failed sentinel
    all read the same through the owner as directly (the row is read by index).
    Mutation: swap two columns of the unpacking -> the message differs -> red."""
    db = build_store(home)
    set_embed(db, *row)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    direct, via, calls = _direct_and_owner(mod, "_check_embedding_restore_flag", home, owner_kind, monkeypatch)
    assert via == direct
    assert [c[0] for c in calls] == [SQL_EMBED]
    if row[0]:
        assert direct[0] is False and direct[1]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_b_embedding_restore_missing_table_is_healthy_directly_and_unknown_through_the_owner(owner_kind, home, monkeypatch):
    """DESIGN DECISION (D8, stated in the PR body for the design seat): a store without the
    embedding_restore_state table reads (True, None) directly ("no such table" is a sqlite3 error,
    healthy by design), but an OWNER failure to serve that statement is UNKNOWN: (None, "OwnerReadError: ...").
    The 'no such table' healthy branch stays for sqlite3 errors only.
    Mutation: treat an owner failure as healthy -> (True, None) -> red."""
    build_store(home)
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("DROP TABLE IF EXISTS embedding_restore_state")
    conn.commit()
    conn.close()
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    mod._OWNER_RUN = None
    assert mod._check_embedding_restore_flag(str(home)) == (True, None)
    with owner_of(owner_kind, home):
        with Fire(mod) as run:
            via = mod._check_embedding_restore_flag(str(home))
    assert via[0] is None and via[1].startswith("OwnerReadError: owner read failed: "), via
    assert run["failure"]["site"] == "_check_embedding_restore_flag"


@requires_linux_owner
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_embedding_check_flag_on_off_divergence_is_pinned_under_the_real_owner(home, monkeypatch):
    """PR #3746 design ruling (Borges), NO behaviour change, divergence PINNED against the REAL
    owner (the fake/Script stubs cannot show the registry's own `refused_at_load`) on a
    real-schema store whose `embedding_restore_state` table is DROPPED before the owner starts:
      flag OFF -> (True, None)  (sqlite3 "no such table" is healthy by design);
      flag ON  -> (None, "OwnerReadError...refused_at_load...") -- UNKNOWN, never healthy (since #3753
                  the reason is `refused_at_load`; an EMPTY registry, a real outage, is still
                  `unregistered_template`, and the hook does not branch on the reason);
                  the failure is recorded at this site with kind `rejected`, and a full fire carries
                  the owner-failure notice exactly once.
    Mutations: (M1) flag-ON branch maps a Rejected `refused_at_load` to (True, None) -> the ON
    assertions go red; (M2) flag-OFF branch treats "no such table" as UNKNOWN -> the OFF assertion
    goes red; (M3) no notice emitted for this failure -> the notice count goes red."""
    mod = full_fire_setup(home, monkeypatch)  # build_store + flag ON + marker; hermetic hook module
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("DROP TABLE IF EXISTS embedding_restore_state")
    conn.commit()
    conn.close()
    # flag OFF: the direct read; no owner is involved at all.
    set_flag(home, False)
    mod._OWNER_RUN = None
    assert mod._check_embedding_restore_flag(str(home)) == (True, None)
    # flag ON, real owner started AFTER the drop.
    set_flag(home, True)
    with owner_of("real", home):
        with Fire(mod) as run:
            via = mod._check_embedding_restore_flag(str(home))
        assert via[0] is None, via
        assert via[1].startswith("OwnerReadError"), via
        assert "refused_at_load" in via[1], via
        assert run["failure"]["site"] == "_check_embedding_restore_flag", run["failure"]
        assert run["failure"]["kind"] == "rejected", run["failure"]
        fire = run_main(mod, home)
    assert fire.exc is None, fire.exc
    assert fire.ctx.count(NOTICE_PHRASE) == 1, fire.ctx
    line = [ln for ln in fire.ctx.split("\n") if NOTICE_PHRASE in ln][0]
    assert "_check_embedding_restore_flag" in line and "rejected" in line, line


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
@pytest.mark.parametrize("case", ["populated", "empty"])
def test_b_focus_block_owner_equals_direct(case, owner_kind, home, monkeypatch):
    """`focus_block`: a populated list (shared-reason back-reference, a stale pin with its marker and
    summary line, a NULL reason) and an empty list render identically through the owner. Columns are
    read BY INDEX (the shim has no row_factory).
    Mutation: read a column by the wrong index (M9) / assign row_factory on the shim path (M17) -> red."""
    db = build_store(home)
    if case == "empty":
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE nodes SET focused_at = NULL, focus_reason = NULL")
        conn.commit()
        conn.close()
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    direct, via, calls = _direct_and_owner(mod, "focus_block", home, owner_kind, monkeypatch)
    assert via == direct
    assert [c[0] for c in calls] == [SQL_FOCUS]
    if case == "populated":
        assert "reason: same as ob_0001" in direct and "pinned" in direct and "stale" in direct
    else:
        assert direct == ""


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
@pytest.mark.parametrize("case", ["some", "none", "many"])
def test_b_saved_focus_sets_block_owner_equals_direct(case, owner_kind, home, monkeypatch):
    """`saved_focus_sets_block`: compact form (<=3 sets, the active one excluded, a non-JSON
    node_ids as 0 nodes), the empty table, and the one-line-per-set form (>3) with descriptions
    render identically through the owner (two statements, both by index).
    Mutation: read name/created_at at the wrong index -> red."""
    db = build_store(home)
    conn = sqlite3.connect(str(db))
    if case == "none":
        conn.execute("DELETE FROM focus_sets")
    if case == "many":
        for i in range(4):
            conn.execute("INSERT INTO focus_sets (name, node_ids, description, created_at) VALUES (?, '[\"ob_0001\"]', ?, ?)",
                         (f"more-{i}", f"describes {i}", f"2026-10-0{i + 4}T00:00:00Z"))
    conn.commit()
    conn.close()
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    direct, via, calls = _direct_and_owner(mod, "saved_focus_sets_block", home, owner_kind, monkeypatch)
    assert via == direct
    assert [c[0] for c in calls] == [SQL_ACTIVE, SQL_SETS]
    assert (direct == "") is (case == "none")
    if case == "some":
        assert "set-b" not in direct and "set-a (1 node, saved 2026-10-01)" in direct and "set-c (0 nodes" in direct
    if case == "many":
        assert direct.startswith("💼 Saved focus-sets:\n") and "describes 3" in direct


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
@pytest.mark.parametrize("case", ["window", "empty_window"])
def test_b_session_checkpoint_block_owner_equals_direct(case, owner_kind, home, monkeypatch):
    """`session_checkpoint_block`: a window holding a live, a superseded ([superseded]), a retracted
    ([retracted]) and a blank node (excluded) renders identically through the owner, with the
    liveness tag taken from the by-index row; an empty window renders ''.
    Mutation: tag from the wrong index / keep `_liveness_tag` name-based on the tuple (M9/M17) -> red."""
    build_store(home)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    if case == "empty_window":
        monkeypatch.setattr(sys.modules[__name__], "CHECKPOINT_FROM", "2030-01-01T00:00:00Z")
    direct, via, calls = _direct_and_owner(mod, "session_checkpoint_block", home, owner_kind, monkeypatch)
    assert via == direct
    assert [c[0] for c in calls] == [SQL_CK_COUNT, SQL_CK_LIST]
    if case == "window":
        assert "[ck_0002] (observation) [superseded]" in direct and "[ck_0003] (observation) [retracted]" in direct
        assert "ck_0004" not in direct and "checkpoint summary one" in direct
    else:
        assert direct == ""


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
@pytest.mark.parametrize("case", ["no_marker_established", "no_marker_fresh_install", "stale_busy", "stale_quiet", "fresh_marker"])
def test_b_sleep_status_block_owner_equals_direct(case, owner_kind, home, monkeypatch):
    """`sleep_status_block`: marker absent on an established graph (banner, read 1), marker absent on
    a small graph (silent), marker stale with many new nodes (banner with the count, read 2), stale
    with few (quiet day, silent) and fresh (no read at all) are the same through the owner.
    Mutation: swap the two reads' statements / count the wrong column -> red."""
    build_store(home)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    if case.startswith("no_marker"):
        set_sleep_marker(home, None)
    elif case == "fresh_marker":
        set_sleep_marker(home, "2999-01-01T00:00:00+00:00")
    else:
        set_sleep_marker(home, STALE_MARKER)
    if case == "no_marker_fresh_install":
        monkeypatch.setattr(mod, "SLEEP_BASELINE_NODE_THRESHOLD", 10_000)
    if case == "stale_quiet":
        monkeypatch.setattr(mod, "SLEEP_DEBT_MIN_NODES_THRESHOLD", 10_000)
    direct, via, calls = _direct_and_owner(mod, "sleep_status_block", home, owner_kind, monkeypatch)
    assert norm_home(via, home) == norm_home(direct, home)
    asked = [c[0] for c in calls]
    if case.startswith("no_marker"):
        assert asked == [SQL_SLEEP1]
    elif case == "fresh_marker":
        assert asked == [] and direct == ""
    else:
        assert asked == [SQL_SLEEP2]
    if case == "no_marker_established":
        assert "no baseline sleep marker" in direct and "DB has " in direct
    if case == "stale_busy":
        assert re.search(r"\d+ nodes added since", direct)
    if case in ("no_marker_fresh_install", "stale_quiet"):
        assert direct == ""


# =========================================================================================
# (c) owner failures: the degraded result of each function, ONE notice, no direct open
# =========================================================================================

FAILURES = [
    pytest.param(("unavailable", "down"), id="unavailable-down"),
    pytest.param(("unavailable", "timeout"), id="unavailable-timeout"),
    pytest.param(("unavailable", "protocol"), id="unavailable-protocol"),
    pytest.param(("rejected", None), id="rejected"),
    pytest.param(("client_import", None), id="client-import"),
]


def failing_handler(failure, sqls, home):
    kind, reason = failure

    def h(method, sql, n):
        if method == "read" and sql in sqls:
            if kind == "unavailable":
                return _unavailable(reason, home)
            if kind == "rejected":
                return _rejected()
        return None
    return h


def arm_failure(mod, monkeypatch, failure, sqls, home):
    """Make the statements in `sqls` fail with `failure`; the cold-start wait is disabled so a
    'down' failure is a plain failure here (the wait is cell (d)'s subject)."""
    monkeypatch.setattr(mod, "OWNER_COLD_START_WAIT_S", 0)
    if failure[0] == "client_import":
        def _no_client():
            raise ImportError("engramd_client unavailable (test)")
        monkeypatch.setattr(mod, "_import_engramd_client", _no_client)
        return None
    return Script(monkeypatch, failing_handler(failure, sqls, home))


def expected_degraded(name, failure):
    if name == "_check_backup_staleness":
        return lambda r: r == (True, None)
    if name == "_check_embedding_restore_flag":
        return lambda r: (r[0] is None and isinstance(r[1], str) and bool(r[1])
                          and (failure[0] == "client_import" or r[1].startswith("OwnerReadError: owner read failed: ")))
    return lambda r: r == ""


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("name", SIX)
def test_c_each_function_degrades_exactly_as_its_direct_failure_path_does(name, failure, home, monkeypatch):
    """The per-function degraded result (D8) under every owner failure, with the owner serving the
    other statements: `_check_backup_staleness` -> (True, None); the embedding check -> UNKNOWN
    (None, "OwnerReadError: ..."); focus / saved sets / checkpoint -> ''; sleep_status_block read 1
    -> ''. The failure is recorded ONCE with its site and kind, and NO function opens the store
    afterwards (every direct open raises and records; the record is empty).
    Mutation: a direct-open fallback in ONE function (M8 x6) -> a recorded open -> red; a degraded
    result differing in ONE function (M9 x6) -> that function's cell red."""
    build_store(home)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    if name == "sleep_status_block":
        set_sleep_marker(home, None)
    arm_failure(mod, monkeypatch, failure, FN_SQL[name] - {SQL_SLEEP2}, home)
    opens = AttrOpens(mod, home).install(monkeypatch)
    with FakeOwner(home):
        with Fire(mod) as run:
            res = call_fn(mod, name, home)
            again = call_fn(mod, name, home)
    assert expected_degraded(name, failure)(res), res
    assert expected_degraded(name, failure)(again), again
    assert opens.record == [], opens.record
    assert run["failure"] is not None and run["failure"]["site"] == name
    assert run["failure"]["kind"] == {"unavailable": "unavailable", "rejected": "rejected", "client_import": "client_import"}[failure[0]]


@pytest.mark.parametrize("failure", FAILURES)
def test_c_sleep_status_read_2_failure_prints_node_count_unavailable(failure, home, monkeypatch):
    """`sleep_status_block` read 2 (stale marker): an owner failure leaves `nodes_since = None`, so
    the banner still prints with 'node count unavailable' (an unknown count is not a quiet day).
    Mutation: return '' on a read-2 owner failure (M9) -> red."""
    build_store(home)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    set_sleep_marker(home, STALE_MARKER)
    arm_failure(mod, monkeypatch, failure, {SQL_SLEEP2}, home)
    opens = AttrOpens(mod, home).install(monkeypatch)
    with FakeOwner(home):
        with Fire(mod) as run:
            res = mod.sleep_status_block()
    assert "[SLEEP STATUS" in res and "node count unavailable" in res, res
    assert opens.record == []
    assert run["failure"]["site"] == "sleep_status_block"


@pytest.mark.parametrize("reason", UNAVAILABLE_REASONS_NO_WAIT + ["down"])
def test_c_every_unavailable_reason_degrades_the_same_way(reason, home, monkeypatch):
    """Whatever the Unavailable reason code (the wait is off here), the function degrades to its
    table value and records the kind. Mutation: special-case one reason into a different result -> red."""
    build_store(home)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    arm_failure(mod, monkeypatch, ("unavailable", reason), {SQL_FOCUS}, home)
    with FakeOwner(home):
        with Fire(mod) as run:
            assert mod.focus_block(str(home)) == ""
    assert run["failure"]["kind"] == "unavailable"


def full_fire_failing(mod, home, monkeypatch, failure, sqls, source="startup"):
    arm_failure(mod, monkeypatch, failure, sqls, home)
    opens = AttrOpens(mod, home).install(monkeypatch)
    with FakeOwner(home):
        run = run_main(mod, home, source)
    return run, opens


@pytest.mark.parametrize("failure", [f for f in FAILURES if f.values[0][0] != "client_import"])
@pytest.mark.parametrize("name", SIX)
def test_c_a_full_fire_with_one_failing_function_gives_one_notice_one_stderr_line_no_open(name, failure, home, monkeypatch):
    """A whole `main()` with ONE function's statements failing: that function's block is missing,
    the other blocks are delivered, EXACTLY ONE owner notice line names the site and the kind, ONE
    stderr line carries kind and site only, the exit is clean, and no function opened the store.
    (A client that cannot be imported fails every function at once: the next cell.)
    Mutation: no notice (or two); no stderr line; a direct-open fallback (M8) -> red."""
    full = full_fire_setup(home, monkeypatch)
    sqls = FN_SQL[name]
    run, opens = full_fire_failing(full, home, monkeypatch, failure, sqls)
    assert run.exc is None, run.exc
    ctx = run.ctx
    assert ctx.count(NOTICE_PHRASE) == 1, ctx
    line = [ln for ln in ctx.split("\n") if NOTICE_PHRASE in ln][0]
    assert name in line and failure[0] in line
    stderr_lines = OWNER_STDERR_RE.findall(run.stderr)
    assert stderr_lines == [(failure[0], name)], run.stderr
    assert opens.record == [], opens.record
    block_of = {"_check_backup_staleness": "backup", "focus_block": "focus", "saved_focus_sets_block": "saved",
                "session_checkpoint_block": "checkpoint", "sleep_status_block": "sleep"}
    for fn, key in block_of.items():
        assert (BLOCK_MARKERS[key] in ctx) is (fn != name), (fn, key, ctx)


@pytest.mark.parametrize("failure", FAILURES)
def test_c_the_owner_entirely_down_in_a_full_fire_gives_one_notice_naming_every_degraded_block(failure, home, monkeypatch):
    """Every statement fails in one fire: still ONE notice line (the first failing site), which
    names every degraded block, one stderr line, no direct open, a clean exit. The embedding probe's
    own UNKNOWN line (the function's degraded result, D8) is a separate pre-existing line.
    Mutation: one notice per failing function -> count > 1 -> red."""
    full = full_fire_setup(home, monkeypatch)
    run, opens = full_fire_failing(full, home, monkeypatch, failure, ALL_NINE)
    assert run.exc is None, run.exc
    assert run.ctx.count(NOTICE_PHRASE) == 1, run.ctx
    line = [ln for ln in run.ctx.split("\n") if NOTICE_PHRASE in ln][0]
    for blk in ("git backup", "embedding restore", "focus list", "saved focus-sets", "session checkpoint", "sleep status"):
        assert blk in line, (blk, line)
    assert len(OWNER_STDERR_RE.findall(run.stderr)) == 1, run.stderr
    assert opens.record == []
    for key in ("focus", "saved", "checkpoint"):
        assert BLOCK_MARKERS[key] not in run.ctx


# =========================================================================================
# (d) COLD START
# =========================================================================================

def test_d1_the_owner_comes_up_late_the_blocks_are_served_after_one_bounded_wait(home, monkeypatch):
    """The owner becomes ready ~150 ms after the hook starts (the measured shape of a cold start):
    the first read finds nothing listening (Unavailable/down), the hook waits for the socket (one
    ping poll loop), retries THAT statement once and every block is delivered. The notice is the
    informational 'waited N ms, then served' line, N between the owner delay and the wait bound.
    Mutation: no wait (M3-family: wait disabled) -> the blocks degrade -> red; the retry sends a
    different statement or twice (M19) -> the statement log differs -> red."""
    mod = full_fire_setup(home, monkeypatch)
    stub_calibration_reads(monkeypatch)
    seen = Script(monkeypatch, lambda m, s, n: None)
    with LateOwner("fake", home, 0.15):
        run = run_main(mod, home)
    assert run.exc is None, run.exc
    for key in ("focus", "saved", "checkpoint", "backup"):
        assert BLOCK_MARKERS[key] in run.ctx, (key, run.ctx)
    assert NOTICE_PHRASE not in run.ctx
    lines = [ln for ln in run.ctx.split("\n") if WAIT_PHRASE in ln]
    assert len(lines) == 1, run.ctx
    m = re.search(r"waited (\d+) ms, then served", lines[0])
    assert m and 100 <= int(m.group(1)) <= 600, lines[0]
    # exactly: the first statement failed once, the SAME statement was retried once, no other retry
    reads = seen.statements
    assert reads[0] == reads[1], reads[:3]
    assert reads.count(reads[0]) == 2, reads
    assert len(seen.pings) >= 1


def test_d1_without_the_wait_the_first_reads_lose_the_cold_start_race(home, monkeypatch):
    """THE PREDICTION RUN (parent's measurement: with the flag ON and NO wait, the first five reads
    of a cold start lose the race, the owner answering ~60-90 ms after they ask): the migrated hook
    with OWNER_COLD_START_WAIT_S = 0 against an owner that comes up after the reads degrades EVERY
    block it needed, with the failure notice (and no wait recorded).
    Mutation: ignore the disabled wait (wait anyway) -> the blocks are served -> red."""
    mod = full_fire_setup(home, monkeypatch)
    monkeypatch.setattr(mod, "OWNER_COLD_START_WAIT_S", 0)
    seen = Script(monkeypatch, lambda m, s, n: None)
    with LateOwner("fake", home, 0.6):
        run = run_main(mod, home)
    assert run.exc is None, run.exc
    for key in ("focus", "saved", "checkpoint", "sleep", "backup"):
        assert BLOCK_MARKERS[key] not in run.ctx, (key, run.ctx)
    assert run.ctx.count(NOTICE_PHRASE) == 1 and "waited" not in run.ctx
    assert seen.pings == []


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_d1_the_wait_is_the_same_under_the_real_owner(owner_kind, home, monkeypatch):
    """d1 against both owners: ready after 150 ms -> every block served, one 'then served' line.
    Mutation: a wait that never polls (sleeps the whole bound) -> N ~ 500 ms > the tolerance -> red."""
    mod = full_fire_setup(home, monkeypatch)
    stub_calibration_reads(monkeypatch)
    with LateOwner(owner_kind, home, 0.15):
        run = run_main(mod, home)
    assert run.exc is None, run.exc
    for key in ("focus", "saved", "checkpoint", "backup"):
        assert BLOCK_MARKERS[key] in run.ctx, (key, run.ctx)
    m = re.search(r"waited (\d+) ms, then served", run.ctx)
    assert m and 100 <= int(m.group(1)) < 480, run.ctx


def test_d2_an_owner_that_never_comes_up_degrades_within_the_bound_with_one_wait(home, tmp_path):
    """No owner at all, a REAL process, the real constants: every owner-read function degrades, the
    fire completes in well under the hook's 5 s timeout, the failure notice carries the wait in ms
    (~ the wait bound), exactly ONE wait happened (a bounded burst of pings, then NO further socket
    use: the owner is marked down for the rest of the fire), and the single statement attempt before
    the wait is the only statement ever sent.
    Mutation: wait forever / past the bound (M5), a wait per read (M6), the down mark not retained
    (M7: later reads use the socket again) -> red."""
    build_store(home)
    set_flag(home, True)
    seed_marker(home)
    root = flattened_root(tmp_path)
    hook = root / "hooks" / "engram-session-start-hook.py"
    mod_consts = _constants(root)
    out = run_hook_subprocess(hook, home, tmp_path, spy_read=True, runtime_dir=root, timeout=30)
    assert out["rc"] == 0, out
    assert out["elapsed"] < mod_consts["WAIT"] + 1.0 and out["wall"] < 4.0, (out["elapsed"], out["wall"])
    for key in ("focus", "saved", "checkpoint"):
        assert BLOCK_MARKERS[key] not in out["ctx"]
    assert out["ctx"].count(NOTICE_PHRASE) == 1, out["ctx"]
    m = re.search(r"waited (\d+) ms", out["ctx"])
    assert m and mod_consts["WAIT"] * 1000 * 0.9 <= int(m.group(1)) <= mod_consts["WAIT"] * 1000 + 400, out["ctx"]
    statements = [c for c in out["calls"] if c[0] == "read"]
    pings = [c for c in out["calls"] if c[0] == "ping"]
    assert len(statements) == 1, statements
    assert 2 <= len(pings) <= int(mod_consts["WAIT"] / mod_consts["POLL"]) + 4, len(pings)
    assert out["opens"] == []


def _constants(root: Path) -> dict:
    src = (root / "hooks" / "engram-session-start-hook.py").read_text()
    def grab(name):
        return float(re.search(rf"^{name}\s*=\s*([0-9.]+)", src, re.M).group(1))
    return {"WAIT": grab("OWNER_COLD_START_WAIT_S"), "POLL": grab("OWNER_COLD_START_POLL_S"),
            "BUDGET": grab("OWNER_FIRE_BUDGET_S")}


def test_d3_a_rejected_read_never_waits(home, monkeypatch):
    """A `Rejected` first read (the owner is up and refuses) never waits: zero ping attempts, no
    wait recorded, the fire is fast. Mutation: wait on Rejected (M3) -> pings > 0 -> red."""
    mod = full_fire_setup(home, monkeypatch)
    s = Script(monkeypatch, lambda m, sql, n: _rejected() if m == "read" else None)
    t0 = time.perf_counter()
    run = run_main(mod, home)
    assert run.exc is None
    assert s.pings == []
    assert time.perf_counter() - t0 < mod.OWNER_COLD_START_WAIT_S
    assert "waited" not in run.ctx and run.ctx.count(NOTICE_PHRASE) == 1


@pytest.mark.parametrize("reason", UNAVAILABLE_REASONS_NO_WAIT)
def test_d4_unavailable_other_than_down_never_waits(reason, home, monkeypatch):
    """Unavailable with any reason but 'down' never waits (the owner is not cold-starting: slow,
    busy, garbled or a client-side problem). Mutation: wait on reason != 'down' (M4) -> red."""
    mod = full_fire_setup(home, monkeypatch)
    s = Script(monkeypatch, lambda m, sql, n: _unavailable(reason, home) if m == "read" else None)
    run = run_main(mod, home)
    assert run.exc is None
    assert s.pings == [], s.pings
    assert "waited" not in run.ctx and run.ctx.count(NOTICE_PHRASE) == 1


def test_d5_an_owner_that_is_up_has_no_wait_and_no_wait_line(home, monkeypatch):
    """Owner already up: zero ping attempts, no wait line, no notice. Mutation: a ping before the
    first read (an unconditional probe) -> pings > 0 -> red."""
    mod = full_fire_setup(home, monkeypatch)
    stub_calibration_reads(monkeypatch)
    s = Script(monkeypatch, lambda m, sql, n: None)
    with FakeOwner(home):
        run = run_main(mod, home)
    assert run.exc is None
    assert SQL_FOCUS in s.statements and SQL_EMBED in s.statements, s.statements  # the owner path really ran
    assert s.pings == []
    assert WAIT_PHRASE not in run.ctx and NOTICE_PHRASE not in run.ctx and "waited" not in run.ctx


def test_d6_an_unavailable_after_a_success_does_not_wait(home, monkeypatch):
    """The owner served the first reads, then a later statement gets Unavailable/down (it died
    mid-fire): no wait (the wait is for the cold start only: 'no read has succeeded yet').
    Mutation: wait after a success (M6) -> pings > 0 -> red."""
    mod = full_fire_setup(home, monkeypatch)
    s = Script(monkeypatch, lambda m, sql, n: _unavailable("down", home) if (m == "read" and sql == SQL_FOCUS) else None)
    with FakeOwner(home):
        run = run_main(mod, home)
    assert run.exc is None
    assert s.pings == []
    assert BLOCK_MARKERS["focus"] not in run.ctx and BLOCK_MARKERS["saved"] in run.ctx
    assert run.ctx.count(NOTICE_PHRASE) == 1 and "waited" not in run.ctx


def test_d7_a_hung_owner_is_bounded_by_the_fire_budget(home, tmp_path):
    """An owner that accepts and never answers (a REAL process, the real budgets): each read times
    out at the per-read bound, the fire budget caps the sum, every block degrades, and the hook
    exits well inside its 5 s timeout. Reason 'timeout' never waits (zero pings).
    Mutation: budgets changed (M20), a wait on timeout (M4), a per-read timeout not re-armed
    against the remaining budget -> the elapsed time exceeds the bound -> red."""
    build_store(home)
    set_flag(home, True)
    seed_marker(home)
    root = flattened_root(tmp_path)
    hook = root / "hooks" / "engram-session-start-hook.py"
    consts = _constants(root)
    with HungServer(home):
        out = run_hook_subprocess(hook, home, tmp_path, spy_read=True, runtime_dir=root, timeout=30)
    assert out["rc"] == 0, out
    assert out["elapsed"] <= consts["BUDGET"] + 1.0 and out["wall"] < 5.0, (out["elapsed"], out["wall"])
    assert [c for c in out["calls"] if c[0] == "ping"] == []
    for key in ("focus", "saved", "checkpoint"):
        assert BLOCK_MARKERS[key] not in out["ctx"]
    assert out["ctx"].count(NOTICE_PHRASE) == 1


def test_d8_the_wait_is_bounded_by_the_remaining_fire_budget(home, monkeypatch):
    """The wait never outlives the fire budget: with a 0.2 s budget and a 0.5 s wait bound against an
    owner that never comes up, the recorded wait is ~the budget, not the wait bound.
    Mutation: ignore the fire budget in the wait loop -> N ~ 500 ms -> red."""
    mod = full_fire_setup(home, monkeypatch)
    monkeypatch.setattr(mod, "OWNER_FIRE_BUDGET_S", 0.2)
    run = run_main(mod, home)
    assert run.exc is None
    m = re.search(r"waited (\d+) ms", run.ctx)
    assert m and int(m.group(1)) <= 330, run.ctx


def test_d9_the_retry_after_the_wait_is_exactly_one_attempt(home, monkeypatch):
    """The owner answers the ping but the retried statement fails again: that statement was sent
    exactly TWICE in all (the first try and ONE retry), the function degrades, and no second wait
    follows. Mutation: retry twice (M19) -> 3 sends -> red."""
    mod = full_fire_setup(home, monkeypatch)
    state = {"n": 0}

    def h(method, sql, n):
        if method == "ping":
            return ec.Ok(result="pong")
        if method == "read" and sql == SQL_BACKUP:
            state["n"] += 1
            return _unavailable("down", home) if state["n"] == 1 else _unavailable("timeout", home)
        return None
    s = Script(monkeypatch, h)
    with FakeOwner(home):
        run = run_main(mod, home)
    assert run.exc is None
    assert s.statements.count(SQL_BACKUP) == 2, s.statements
    assert len(s.pings) == 1, s.pings


def test_d_a_refused_ping_ends_the_wait_at_once(home, monkeypatch):
    """The owner is reachable but REFUSES the readiness ping (a Rejected answer): waiting cannot
    help, so the wait ends after that one ping -- no polling to the bound -- and the owner is
    down for the rest of the fire. (A decision the spec left open: poll only while nothing
    answers.) Mutation: keep polling after a Rejected ping -> many pings, N ~ the bound -> red."""
    mod = full_fire_setup(home, monkeypatch)

    def h(method, sql, n):
        if method == "ping":
            return _rejected("ping_refused")
        return _unavailable("down", home) if method == "read" else None
    s = Script(monkeypatch, h)
    run = run_main(mod, home)
    assert run.exc is None
    assert len(s.pings) == 1, s.pings
    m = re.search(r"waited (\d+) ms", run.ctx)
    assert m and int(m.group(1)) < 150, run.ctx
    assert len(s.statements) == 1, s.statements  # no socket use after the wait ended


def test_d_the_ping_uses_the_same_socket_as_the_shim(home, monkeypatch):
    """The readiness ping goes to the SAME socket path as the reads (an explicit path would split
    them): each ping's `socket_path` equals the `socket_path` the fire's OwnerReadConnection was
    built with (None = the default resolution, shared). Mutation: ping a different path -> red."""
    mod = full_fire_setup(home, monkeypatch)
    built = []
    real_cls = ec.OwnerReadConnection

    class Spy(real_cls):
        __slots__ = ()

        def __init__(self, *a, **k):
            built.append(k.get("socket_path"))
            super().__init__(*a, **k)
    monkeypatch.setattr(ec, "OwnerReadConnection", Spy)
    monkeypatch.setattr(mod, "OWNER_COLD_START_WAIT_S", 0.15)
    s = Script(monkeypatch, lambda m, sql, n: None)
    run_main(mod, home)
    assert len(built) == 1
    assert s.pings and all(c[3] == built[0] for c in s.pings), (built, s.pings)


# =========================================================================================
# (e) store absent: no function creates a file
# =========================================================================================

@pytest.mark.parametrize("flag", [None, True], ids=["flag-off", "flag-on"])
def test_e_store_absent_a_full_fire_creates_no_store_file(flag, home, tmp_path):
    """A fire against a home with NO knowledge.db (flag OFF and flag ON; the calibration block is
    stubbed away -- it is the one thing that builds the schema, cell (a2)): the store file does not
    exist afterwards and no non-read-only connect to it was made. Before this fix
    `sleep_status_block`'s raw `sqlite3.connect` left a 0-byte file behind.
    Mutation: restore ONE of the three raw connects (M16 x3) -> the 0-byte file exists -> red."""
    set_flag(home, flag)
    root = flattened_root(tmp_path)
    hook = root / "hooks" / "engram-session-start-hook.py"
    out = run_hook_subprocess(hook, home, tmp_path)
    assert out["rc"] == 0, out
    assert out["store_exists"] is False, (out["store_size"], out["opens"])
    assert [o for o in out["opens"] if not o["ro"]] == [], out["opens"]


def test_e_control_the_unmodified_hook_does_create_the_zero_byte_file(home, tmp_path):
    """Anti-vacuity for (e): the SAME instrument run on the unmodified hook (4f475cf4) sees the
    0-byte store file created by `sleep_status_block`'s raw connect. Without this the 'no file'
    assertion could not tell a fixed hook from a blind instrument."""
    hook = oracle_hook(tmp_path)
    out = run_hook_subprocess(hook, home, tmp_path)
    assert out["rc"] == 0, out
    assert out["store_exists"] is True and out["store_size"] == 0, out
    assert [o["caller"] for o in out["opens"] if not o["ro"] and not o["exists_before"]] == ["sleep_status_block"]


# =========================================================================================
# (f) flag OFF == the unmodified hook, byte for byte
# =========================================================================================

def _snapshot(home: Path) -> dict:
    snap = {}
    for p in sorted(home.rglob("*")):
        rel = p.relative_to(home).as_posix()
        if not p.is_file() or rel.startswith(("logs/", "telemetry/", "userhome/")) or rel.startswith("knowledge.db"):
            continue
        text = p.read_text(errors="replace").replace(str(home), "<HOME>")
        if rel.endswith(".json"):
            def strip(o):
                if isinstance(o, dict):
                    return {k: strip(v) for k, v in o.items() if not (k.endswith("_at") or k in ("ts", "now", "injected_through"))}
                if isinstance(o, list):
                    return [strip(v) for v in o]
                return o
            try:
                snap[rel] = strip(json.loads(text))
                continue
            except ValueError:
                pass
        snap[rel] = text
    return snap


def _scenario(home, name):
    build_store(home)
    set_flag(home, None)
    db = home / "knowledge.db"
    source = "startup"
    marker = None
    injected = CHECKPOINT_FROM
    if name == "populated_startup_no_marker":
        pass
    elif name == "compact_with_checkpoint":
        source = "compact"
    elif name == "startup_stale_marker":
        marker = STALE_MARKER
    elif name == "empty_schema_store":
        db.unlink()
        for ext in ("-wal", "-shm"):
            (home / f"knowledge.db{ext}").unlink(missing_ok=True)
        build_empty_store(home)
    elif name == "embedding_flag_set":
        set_embed(db, 1, 9, "interrupted-restore")
    elif name == "no_focus_no_sets":
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE nodes SET focused_at = NULL")
        conn.execute("DELETE FROM focus_sets")
        conn.commit()
        conn.close()
    elif name == "knowledge_sql_present":
        (home / "knowledge.sql").write_text("-- dump")
    seed_marker(home, injected_through=injected)
    set_sleep_marker(home, marker)
    return source


F_SCENARIOS = ["populated_startup_no_marker", "compact_with_checkpoint", "startup_stale_marker", "empty_schema_store",
               "embedding_flag_set", "no_focus_no_sets", "knowledge_sql_present"]


@pytest.mark.parametrize("scenario", F_SCENARIOS)
def test_f_flag_off_equals_the_unmodified_hook_byte_for_byte(scenario, tmp_path, monkeypatch, stub_modules):
    """The flag-OFF hook against the UNMODIFIED hook (`git show 4f475cf4:<path>`) on identical
    copies of one home: the same stdout, the same stderr, the same session/state files (times
    stripped). This is what proves the by-index rewrite and fix B changed nothing observable.
    Mutation: any behavioural change on the OFF path (a changed SQL byte, a wrong index, a
    reordered block, an extra stderr line) -> red."""
    src_home = tmp_path / "src"
    src_home.mkdir()
    source = _scenario(src_home, scenario)
    homes = {}
    for label in ("old", "new"):
        h = tmp_path / label
        shutil.copytree(src_home, h)
        homes[label] = h
    oracle = oracle_hook(tmp_path)
    results = {}
    for label, path in (("old", oracle), ("new", _HOOK_PATH)):
        h = homes[label]
        mod = load_ss(h, monkeypatch, path)
        run = run_main(mod, h, source)
        assert run.exc is None, (label, run.exc)
        results[label] = (norm_home(run.stdout, h), norm_home(run.stderr, h), _snapshot(h))
    assert results["new"] == results["old"]
    assert results["old"][0].strip(), "the reference fire produced no output"


# =========================================================================================
# (g) the "S6 class": the local store file is never consulted on the flag-ON path
# =========================================================================================

def _s6_expected(name):
    return {
        "_check_embedding_restore_flag": lambda r: r[0] is False and "node(s) lack embeddings" in r[1],
        "focus_block": lambda r: "Focused nodes" in r,
        "saved_focus_sets_block": lambda r: "Saved focus-sets" in r,
        "session_checkpoint_block": lambda r: "Nodes created since checkpoint" in r,
    }[name]


@pytest.mark.parametrize("name", FOUR_S6)
def test_g_flag_on_a_store_the_hook_cannot_see_is_still_read_through_the_owner(name, home, tmp_path, monkeypatch):
    """The owner serves a populated store while the function is pointed at a directory with NO
    knowledge.db (in the owner's end state the hook cannot see the store): with the flag ON the block
    is still delivered, because the `os.path.exists(db_path)` guard is bypassed on the owner path.
    Each function is its own cell and must be red on its own.
    Mutation: restore the exists guard on the flag-ON branch of THIS function (M15 x4) -> red."""
    db = build_store(home)
    set_embed(db, 1, 5, "interrupted-restore")
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    blind = tmp_path / "blind"
    blind.mkdir()
    with FakeOwner(home):
        with Fire(mod):
            res = call_fn(mod, name, blind)
    assert _s6_expected(name)(res), res


@pytest.mark.parametrize("name", FOUR_S6)
def test_g_anti_vacuity_the_same_blind_path_with_the_flag_off_yields_the_empty_result(name, home, tmp_path, monkeypatch):
    """The same re-pointing with the flag OFF (no fire state) gives the empty / healthy result: so
    the delivery in the cell above is the owner's doing, not the fixture's."""
    db = build_store(home)
    set_embed(db, 1, 5, "interrupted-restore")
    mod = load_ss(home, monkeypatch)
    blind = tmp_path / "blind"
    blind.mkdir()
    res = call_fn(mod, name, blind)
    assert res == (True, None) or res == "", res


# =========================================================================================
# (h) every statement is a registered template, byte for byte
# =========================================================================================

@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_h_the_statements_sent_are_exactly_the_registered_templates_of_each_function(owner_kind, home, monkeypatch):
    """Across all scenarios of the six functions the owner receives, per function, exactly the
    statements the committed registry lists for that function's site, byte for byte (no extra
    whitespace, LIMIT or semicolon), and across the whole set all nine are exercised. The registry
    side is read from the committed JSON, never retyped.
    Mutation: change ONE SQL byte in ONE function (M18 x8) -> that statement is unregistered -> red."""
    assert {s for fn in SIX for s in sql_set_of_function(fn)} == ALL_NINE
    for fn in SIX:
        assert sql_set_of_function(fn) == FN_SQL[fn]
    build_store(home)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    per_fn = {}
    with owner_of(owner_kind, home):
        for name in SIX:
            for variant in (("none",) if name != "sleep_status_block" else ("no_marker", "stale")):
                if name == "sleep_status_block":
                    set_sleep_marker(home, None if variant == "no_marker" else STALE_MARKER)
                seen = spying_read(monkeypatch)
                with Fire(mod) as run:
                    call_fn(mod, name, home)
                assert run["failure"] is None, (name, run["failure"])
                per_fn.setdefault(name, set()).update(s for s, _ in seen)
    for name in SIX:
        assert per_fn[name] == sql_set_of_function(name), (name, per_fn[name] ^ sql_set_of_function(name))
        assert per_fn[name] <= registered_sql()


# =========================================================================================
# (i) the flag gate
# =========================================================================================

def _poison_client(mod, monkeypatch):
    monkeypatch.setattr(mod, "_import_engramd_client",
                        lambda: pytest.fail("engramd_client imported with the flag OFF"))
    monkeypatch.setattr(ec, "OwnerReadConnection", lambda *a, **k: pytest.fail("OwnerReadConnection built with the flag OFF"))
    monkeypatch.setattr(ec, "read", lambda *a, **k: pytest.fail("ec.read used with the flag OFF"))


OFF_STATES = [
    ("absent", dict(flag=...)),
    ("false", dict(flag=False)),
    ("string_true", dict(flag="true")),
    ("one", dict(flag=1)),
    ("null", dict(flag=None, raw=json.dumps({"engramd": {FLAG_KEY: None}}))),
    ("malformed_config", dict(raw="{not json")),
    ("no_config_file", dict(flag=None)),
    ("list_config", dict(raw="[1, 2]")),
]


@pytest.mark.parametrize("state", [s[0] for s in OFF_STATES])
def test_i_every_off_state_is_the_direct_path_and_never_touches_the_client(state, home, tmp_path, monkeypatch):
    """Only the JSON boolean true is ON. Absent / false / "true" / 1 / null / malformed / a missing
    config file all give the same stdout as the UNMODIFIED hook run on the same config (a config
    file changes other blocks, so the reference carries the same file), with the client seam
    poisoned (any use fails) and no owner trace on stderr.
    Mutation: branch on anything but the literal true (M13), or import the client on the OFF path
    (M14) -> red."""
    build_store(home)
    seed_marker(home)
    set_flag(home, **dict(OFF_STATES)[state])
    new_home, old_home = tmp_path / "new", tmp_path / "old"
    shutil.copytree(home, new_home)
    shutil.copytree(home, old_home)
    mod = load_ss(new_home, monkeypatch)
    _poison_client(mod, monkeypatch)
    run = run_main(mod, new_home)
    assert run.exc is None, run.exc
    ref = run_main(load_ss(old_home, monkeypatch, oracle_hook(tmp_path)), old_home)
    assert norm_home(run.stdout, new_home) == norm_home(ref.stdout, old_home)
    assert "owner" not in run.stderr.lower()
    assert BLOCK_MARKERS["focus"] in run.ctx


@pytest.mark.parametrize("value", ["yes", 1, [1], {"a": 1}, "true"])
def test_i_a_truthy_non_bool_from_the_flag_helper_reads_off(value, home, monkeypatch):
    """A helper whose `flag_on` returns a truthy non-bool must not read as ON: direct path, the
    client untouched. Mutation: `flag_on(...)` consumed without `is True` (M13) -> red."""
    build_store(home)
    mod = load_ss(home, monkeypatch)
    _poison_client(mod, monkeypatch)

    class Helper:
        @staticmethod
        def flag_on(key, path):
            return value
    monkeypatch.setattr(mod, "_import_owner_flags", lambda: Helper)
    assert mod._owner_reads_enabled() is False
    run = run_main(mod, home)
    assert run.exc is None and BLOCK_MARKERS["focus"] in run.ctx


def test_i_the_flag_is_read_only_through_flag_on_with_this_hooks_key_and_config_path(home, monkeypatch):
    """A recording stand-in sees exactly (`session_start_reads_via_owner`, $ENGRAM_HOME/config.json),
    ONCE per fire, before the first of the six functions runs. Mutation: another key, a per-hook
    config read, or a read per function -> red."""
    build_store(home)
    mod = load_ss(home, monkeypatch)
    assert mod._OWNER_FLAG_KEY == FLAG_KEY
    calls, order = [], []

    class Helper:
        @staticmethod
        def flag_on(key, path):
            calls.append((key, str(path)))
            order.append("flag")
            return False
    monkeypatch.setattr(mod, "_import_owner_flags", lambda: Helper)
    for fn in SIX:
        real = getattr(mod, fn)
        monkeypatch.setattr(mod, fn, (lambda real, fn: lambda *a, **k: (order.append(fn), real(*a, **k))[1])(real, fn))
    run_main(mod, home)
    assert calls == [(FLAG_KEY, str(home / "config.json"))]
    assert order[0] == "flag" and order.count("flag") == 1


@pytest.mark.parametrize("exc_cls", [ImportError, RuntimeError, SystemExit])
def test_i_a_failing_flag_helper_reads_off_and_reports_the_class_name_only(exc_cls, home, monkeypatch):
    """A flag helper that raises at import or at `flag_on` (even SystemExit) reads OFF, the fire
    completes on the direct path, and ONE stderr line names the exception CLASS, never its message.
    Mutation: let it propagate / print the message / read ON -> red."""
    build_store(home)
    mod = load_ss(home, monkeypatch)
    _poison_client(mod, monkeypatch)

    def boom():
        raise exc_cls("SECRET-MESSAGE-3735")
    monkeypatch.setattr(mod, "_import_owner_flags", boom)
    run = run_main(mod, home)
    assert run.exc is None, run.exc
    assert BLOCK_MARKERS["focus"] in run.ctx
    lines = [ln for ln in run.stderr.splitlines() if "flag helper failed" in ln]
    assert len(lines) == 1 and exc_cls.__name__ in lines[0], run.stderr
    assert "SECRET-MESSAGE-3735" not in run.stderr and "SECRET-MESSAGE-3735" not in run.ctx


def test_i_the_client_and_the_flag_helper_are_not_imported_at_module_scope(home, monkeypatch):
    """No top-level import of `engramd_client` / `engramd_owner_flags` (AST), and neither name is
    bound on the module. Mutation: `import engramd_client` at module scope (M14) -> red."""
    tree = ast.parse(_HOOK_PATH.read_text(encoding="utf-8"))
    top = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            top |= {a.name for a in stmt.names}
        elif isinstance(stmt, ast.ImportFrom):
            top.add(stmt.module)
    assert not ({"engramd_client", "engramd_owner_flags"} & top), top
    build_store(home)
    mod = load_ss(home, monkeypatch)
    assert not hasattr(mod, "engramd_client") and not hasattr(mod, "engramd_owner_flags")


def test_i_in_a_real_flag_off_process_the_client_is_never_imported(home, tmp_path):
    """A REAL flag-OFF process: `engramd_client` is not in sys.modules after the whole fire.
    Mutation: import the client on the OFF path (M14) -> red."""
    build_store(home)
    set_flag(home, None)
    seed_marker(home)
    root = flattened_root(tmp_path)
    out = run_hook_subprocess(root / "hooks" / "engram-session-start-hook.py", home, tmp_path)
    assert out["rc"] == 0 and out["client_imported"] is False, out
    assert BLOCK_MARKERS["focus"] in out["ctx"]


# =========================================================================================
# (j) budgets
# =========================================================================================

def test_j_the_budget_constants_are_pinned_and_leave_a_reserve_in_the_five_second_timeout(home, monkeypatch):
    """OWNER_FIRE_BUDGET_S = 3.0, OWNER_READ_TIMEOUT_S = 1.0, OWNER_COLD_START_WAIT_S = 0.5, the poll
    25 ms and the ping bound 0.2 s; wait + budget <= 4.0 leaves a 1.0 s reserve in the hook's 5 s
    registration (hooks.json), against ~0.75 s measured for the rest of the fire on the direct path.
    Mutation: change any constant (M20, each) -> red."""
    build_store(home)
    mod = load_ss(home, monkeypatch)
    assert mod.OWNER_FIRE_BUDGET_S == 3.0
    assert mod.OWNER_READ_TIMEOUT_S == 1.0
    assert mod.OWNER_COLD_START_WAIT_S == 0.5
    assert mod.OWNER_COLD_START_POLL_S == 0.025
    assert mod.OWNER_PING_TIMEOUT_S == 0.2
    assert mod.OWNER_COLD_START_WAIT_S + mod.OWNER_FIRE_BUDGET_S <= 4.0
    hooks_json = json.loads((REPO_ROOT / "src" / "engram" / "hooks" / "hooks.json").read_text())
    registered = [h for blk in hooks_json["hooks"]["SessionStart"] for h in blk["hooks"]
                  if "engram-session-start-hook.py" in h["command"]]
    assert registered and registered[0]["timeout"] == 5


def test_j_owner_time_in_one_fire_never_exceeds_the_fire_budget(home, monkeypatch):
    """A client whose every read takes 0.9 s (honouring the timeout it is given, as the real one
    does): the whole fire's owner time stays within the fire budget -- the shim re-arms each read
    against the REMAINING budget -- and the later reads are refused as budget_exhausted instead of
    being tried. (Constants scaled down for test time; the real values are pinned above.)
    Mutation: a per-read timeout not clipped to the remaining budget -> elapsed overshoots -> red."""
    mod = full_fire_setup(home, monkeypatch)
    monkeypatch.setattr(mod, "OWNER_FIRE_BUDGET_S", 0.6)
    monkeypatch.setattr(mod, "OWNER_READ_TIMEOUT_S", 0.5)
    monkeypatch.setattr(mod, "OWNER_COLD_START_WAIT_S", 0)

    s = Script(monkeypatch, lambda m, sql, n: None)

    def honouring(method, params, **k):
        s.calls.append((method, (params or {}).get("sql"), k.get("timeout"), k.get("socket_path")))
        t = k.get("timeout") or 1.0
        time.sleep(min(0.9, t))
        return _unavailable("timeout", home) if t < 0.9 else ec.Ok(result=[])
    monkeypatch.setattr(ec, "read", honouring)
    run = run_main(mod, home)
    assert run.exc is None
    assert run.elapsed <= 0.6 + 0.5, run.elapsed
    assert max(c[2] for c in s.calls) <= 0.5 + 1e-6
    assert len(s.calls) <= 3, s.calls  # later reads were refused (budget_exhausted), not tried


# =========================================================================================
# (k) the notice
# =========================================================================================

def _notice_line(ctx):
    lines = [ln for ln in ctx.split("\n") if NOTICE_PHRASE in ln]
    assert len(lines) == 1, ctx
    return lines[0]


def test_k_the_failure_notice_is_one_bounded_control_free_line_with_site_kind_and_wait(home, monkeypatch):
    """One line, <= 500 characters, no control character, naming the failing site and the failure
    kind and -- when a wait happened -- 'waited N ms for the owner socket'. Hostile dynamic text (a
    Rejected reason full of control characters and 600 characters long) is sanitized and cut.
    No ENGRAM node id appears. Mutation: omit the wait ms (M10); unbounded/unsanitized dynamic
    text -> red."""
    mod = full_fire_setup(home, monkeypatch)
    monkeypatch.setattr(mod, "OWNER_COLD_START_WAIT_S", 0.1)
    run = run_main(mod, home)  # no owner: the wait runs out, the owner is marked down
    line = _notice_line(run.ctx)
    assert re.search(r"waited \d+ ms for the owner socket", line), line
    assert len(line) <= 500 and all(ch.isprintable() for ch in line), line
    assert not re.search(r"\b[a-z]{2}_\d{4}\b", line), line
    # hostile dynamic text: a Rejected reason with control characters, 600 characters long
    (home / STAMP_NAME).unlink(missing_ok=True)
    hostile = "bad\nreason\x1b[31m\x00" + "X" * 600
    mod2 = load_ss(home, monkeypatch)
    monkeypatch.setattr(mod2, "OWNER_COLD_START_WAIT_S", 0)
    Script(monkeypatch, lambda m, sql, n: _rejected(hostile) if m == "read" else None)
    with FakeOwner(home):
        run2 = run_main(mod2, home)
    line2 = _notice_line(run2.ctx)
    assert len(line2) <= 500 and all(ch.isprintable() for ch in line2), line2
    assert "X" * 100 not in line2, line2
    # control characters are replaced by spaces, so the reason stays on the notice's own line and
    # nothing of it leaks into the rest of the output
    assert "bad reason" in line2, line2
    assert "\x1b" not in run2.ctx and "\x00" not in run2.ctx, repr(run2.ctx[:600])


def test_k_the_failure_notice_is_rate_limited_per_window_the_stderr_line_is_not(home, monkeypatch):
    """Under the probe-notice kind `session-start-owner`: the first failing fire injects the line, a
    second fire inside the window does not (the stderr line still appears every run), and a stamp
    older than the window shows it again. Mutation: not rate-limited (M11) -> the second fire shows
    it -> red."""
    mod = full_fire_setup(home, monkeypatch)
    monkeypatch.setattr(mod, "OWNER_COLD_START_WAIT_S", 0)
    first = run_main(mod, home)
    assert first.ctx.count(NOTICE_PHRASE) == 1
    stamp = json.loads((home / STAMP_NAME).read_text())["shown_at"]
    assert NOTICE_KEY in stamp

    def fresh():
        m = load_ss(home, monkeypatch)
        monkeypatch.setattr(m, "OWNER_COLD_START_WAIT_S", 0)
        return m
    second = run_main(fresh(), home)
    assert NOTICE_PHRASE not in second.ctx
    assert len(OWNER_STDERR_RE.findall(second.stderr)) == 1, second.stderr
    (home / STAMP_NAME).write_text(json.dumps({"shown_at": {NOTICE_KEY: time.time() - 2000}}))
    third = run_main(fresh(), home)
    assert third.ctx.count(NOTICE_PHRASE) == 1


def test_k_the_successful_wait_line_is_not_rate_limited(home, monkeypatch):
    """A cold start that was waited out is informational and happens only on a real cold start: the
    line shows on EVERY such fire (no stamp is consulted or written for it).
    Mutation: rate-limit the wait line (M11) -> the second fire lacks it -> red."""
    mod = full_fire_setup(home, monkeypatch)
    stub_calibration_reads(monkeypatch)
    for _ in range(2):
        with LateOwner("fake", home, 0.12):
            run = run_main(load_ss(home, monkeypatch), home)
        assert len([ln for ln in run.ctx.split("\n") if WAIT_PHRASE in ln]) == 1, run.ctx
    stamp = json.loads((home / STAMP_NAME).read_text())["shown_at"] if (home / STAMP_NAME).exists() else {}
    assert NOTICE_KEY not in stamp


def test_k_the_window_is_consumed_only_after_the_response_was_written(home, monkeypatch):
    """`mark_probe_notice` runs AFTER the response JSON is already in stdout, and a fire that dies
    before printing leaves no stamp. Mutation: mark before emit (M12) -> stdout empty at the call
    -> red."""
    mod = full_fire_setup(home, monkeypatch)
    monkeypatch.setattr(mod, "OWNER_COLD_START_WAIT_S", 0)
    seen = []
    real = mod.mark_probe_notice
    monkeypatch.setattr(mod, "mark_probe_notice",
                        lambda *a, **k: (seen.append((a[1], sys.stdout.getvalue())), real(*a, **k))[1])
    run = run_main(mod, home)
    assert [k for k, _ in seen] == [NOTICE_KEY], seen
    assert seen[0][1].strip() and NOTICE_PHRASE in seen[0][1]
    # a fire that fails before it prints stamps nothing
    (home / STAMP_NAME).unlink(missing_ok=True)
    mod2 = load_ss(home, monkeypatch)
    monkeypatch.setattr(mod2, "OWNER_COLD_START_WAIT_S", 0)
    monkeypatch.setattr(mod2, "emit_context", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("die before print")))
    run2 = run_main(mod2, home)
    assert run2.exc and "die before print" in run2.exc
    assert not (home / STAMP_NAME).exists()


def test_k_the_notice_survives_a_large_focus_block(home, monkeypatch):
    """A focus block far over the hook's whole budget (15 nodes of ~1.6k characters each) with ONE
    other function failing: the focus block is cut to make room, but the owner notice (priority
    class health) is still in the final output. Mutation: put the notice in a lower priority class
    -> it is the first thing cut -> red."""
    db = build_store(home)
    conn = sqlite3.connect(str(db))
    for i in range(15):
        conn.execute("UPDATE nodes SET claim = ?, focused_at = ?, focus_reason = ? WHERE id = ?",
                     ("L" * 1600, f"2026-10-11T08:{i:02d}:00Z", f"reason {i} " + "r" * 40, f"ob_{i + 10:04d}"))
    conn.commit()
    conn.close()
    set_flag(home, True)
    seed_marker(home)
    mod = load_ss(home, monkeypatch)
    Script(monkeypatch, lambda m, sql, n: _rejected() if (m == "read" and sql in (SQL_ACTIVE, SQL_SETS)) else None)
    with FakeOwner(home):
        run = run_main(mod, home)
    assert run.exc is None
    assert NOTICE_PHRASE in run.ctx, run.ctx[:1500]
    assert "Focused nodes" in run.ctx


def test_k_the_notice_is_in_the_head_preview_when_other_blocks_overflow_the_budget(home, monkeypatch):
    """A checkpoint block of ~190 nodes (far over the hook's whole budget, class REST) and ONE other
    function failing: the owner line is a substrate-health line, so it sits in the head -- inside
    the first 1,800 characters the harness previews -- not behind the overflowing block.
    (The focus-block cell above cannot tell the classes apart: the focus sub-cap makes room for
    every other block, whatever its class.)
    Mutation: queue the notice as an ordinary REST block (X1) -> it is cut -> red."""
    db = build_store(home)
    conn = sqlite3.connect(str(db))
    for i in range(190):
        _add(conn, f"bk{i:03d}", "observation", ("bulk checkpoint node %03d " % i) + "w" * 120)
        conn.execute("UPDATE nodes SET created_at = ? WHERE id = ?", (f"2026-10-12T02:{i // 60:02d}:{i % 60:02d}Z", f"bk{i:03d}"))
    conn.commit()
    conn.close()
    set_flag(home, True)
    seed_marker(home)
    mod = load_ss(home, monkeypatch)
    Script(monkeypatch, lambda m, sql, n: _rejected() if (m == "read" and sql in (SQL_ACTIVE, SQL_SETS)) else None)
    with FakeOwner(home):
        run = run_main(mod, home)
    assert run.exc is None
    assert re.search(r"[1-9]\d* cut", run.ctx.split("\n", 1)[0]), run.ctx[:300]  # the overflow is real
    assert NOTICE_PHRASE in run.ctx[:1800], run.ctx[:1800]


# =========================================================================================
# (l) standalone callers stay direct; the fire state is scoped to main()
# =========================================================================================

def test_l_with_no_fire_state_every_function_stays_on_the_direct_path(home, monkeypatch):
    """Called outside main() (the existing suites do) every one of the six reads directly: the owner
    client is poisoned, the store IS opened by the function, and the results are right.
    Mutation: a function consulting the flag/owner without a fire state -> the poison fires -> red."""
    build_store(home)
    set_flag(home, True)  # the flag file says ON; a standalone call must not care
    mod = load_ss(home, monkeypatch)
    _poison_client(mod, monkeypatch)
    assert mod._OWNER_RUN is None
    opens = AttrOpens(mod, home, raises=False).install(monkeypatch)
    for name in SIX:
        res = call_fn(mod, name, home)
        assert res is not None
    assert mod.focus_block(str(home)).startswith("📌")
    assert {c for _, c in opens.record} >= {"_check_backup_staleness", "_check_embedding_restore_flag", "focus_block",
                                            "saved_focus_sets_block", "session_checkpoint_block", "sleep_status_block"}


@pytest.mark.parametrize("marker", [None, STALE_MARKER], ids=["no-sleep-marker", "stale-sleep-marker"])
def test_l_every_direct_open_of_the_six_is_read_only(marker, home, monkeypatch):
    """Fix B, pinned on the direct path: each of the six functions opens the store through
    `connect_regular_ro` -- the string given to `sqlite3.connect` is a `?mode=ro` URI -- including
    the three that used to open a raw read-write connection (`_check_backup_staleness`,
    `sleep_status_block` read 1 and read 2). The file-absent cell (e) cannot see the first of these
    (it is guarded by an exists-and-size check) nor read 2 (a marker implies a store).
    Mutation: restore ONE raw `sqlite3.connect` (M16 x3) -> a non-read-only open -> red."""
    build_store(home)
    mod = load_ss(home, monkeypatch)
    set_sleep_marker(home, marker)
    opens = AttrOpens(mod, home, raises=False).install(monkeypatch)
    for name in SIX:
        call_fn(mod, name, home)
    assert {c for c, _ in opens.targets} == set(SIX), opens.targets
    assert [(c, t) for c, t in opens.targets if "mode=ro" not in t] == [], opens.targets


def test_l_signatures_are_unchanged_and_the_fire_state_is_reset_on_exit(home, monkeypatch):
    """The six functions keep their signatures (call sites in main() pass nothing new), and
    `_OWNER_RUN` is None before and after `main()` -- also after a fire that crashes.
    Mutation: a new required parameter / a fire state left set after main() -> red."""
    build_store(home)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    want = {"_check_backup_staleness": ["engram_home"], "_check_embedding_restore_flag": ["engram_home"],
            "focus_block": ["engram_home"], "saved_focus_sets_block": ["engram_home"],
            "session_checkpoint_block": ["engram_home", "injected_through"], "sleep_status_block": []}
    for name, params in want.items():
        assert list(inspect.signature(getattr(mod, name)).parameters) == params, name
    assert mod._OWNER_RUN is None
    with FakeOwner(home):
        run_main(mod, home)
    assert mod._OWNER_RUN is None
    monkeypatch.setattr(mod, "emit_context", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with FakeOwner(home):
        run_main(mod, home)
    assert mod._OWNER_RUN is None


# =========================================================================================
# the shared connection: one per fire, never closed by a function, no row_factory
# =========================================================================================

class SpyShim:
    """Looks like the real shim: __slots__, no row_factory, records close(), delegates."""
    __slots__ = ("_c", "closed", "executed")

    def __init__(self, real):
        self._c, self.closed, self.executed = real, 0, 0

    def execute(self, sql, params=()):
        self.executed += 1
        return self._c.execute(sql, params)

    def close(self):
        self.closed += 1
        self._c.close()


def test_one_owner_connection_per_fire_shared_by_all_six_and_never_closed_by_them(home, monkeypatch):
    """ONE OwnerReadConnection per fire (created lazily at first use), shared by all six functions;
    no function closes it (it must still serve the next function) and no function assigns
    `row_factory` on it (the shim has __slots__: assignment raises and the block would silently
    degrade). The results through the spy are the direct results.
    Mutation: a function closes the shared conn (M2) / assigns row_factory on the shim path (M17) /
    builds its own connection -> red."""
    build_store(home)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    set_sleep_marker(home, None)
    built = []
    real_cls = ec.OwnerReadConnection

    def factory(*a, **k):
        shim = SpyShim(real_cls(*a, **k))
        built.append(shim)
        return shim
    monkeypatch.setattr(ec, "OwnerReadConnection", factory)
    direct = {n: call_fn(mod, n, home) for n in SIX}
    with FakeOwner(home):
        with Fire(mod):
            assert built == []
            via = {n: call_fn(mod, n, home) for n in SIX}
    assert len(built) == 1, len(built)
    assert built[0].closed == 0 and built[0].executed >= 7
    for n in SIX:
        assert norm_home(str(via[n]), home) == norm_home(str(direct[n]), home), n


@pytest.mark.parametrize("name", SIX)
def test_each_function_alone_leaves_the_shared_connection_open(name, home, monkeypatch):
    """Per function (so a single closing function cannot hide behind the others): after the call the
    shared connection has not been closed and still answers. Mutation: that function closes the
    owner connection (M2 x6) -> red."""
    build_store(home)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    set_sleep_marker(home, None)
    built = []
    real_cls = ec.OwnerReadConnection
    monkeypatch.setattr(ec, "OwnerReadConnection", lambda *a, **k: (built.append(SpyShim(real_cls(*a, **k))), built[-1])[1])
    with FakeOwner(home):
        with Fire(mod):
            call_fn(mod, name, home)
            if built:
                assert built[0].closed == 0
                assert built[0].execute(SQL_BACKUP).fetchone()[0] > 0


# -- cells for two survivors of the colleague mutation pass -------------------------------------

def test_sanitize_replaces_c0_del_and_c1_controls_with_one_space_and_cuts(home, monkeypatch):
    """Each control character (C0, DEL, and the C1 range 0x80-0x9f) becomes ONE space; a non-control
    neighbour (0xa0) is kept; the text is cut to the limit. Mutation: the character class stops at
    0x7f (C1 controls such as 0x85 / 0x9b pass through unsanitized) -> red."""
    mod = load_ss(home, monkeypatch)
    assert mod._sanitize_owner_text("a\x00b\x1fc\x7fd\x85e\x9bf\x9fg\xa0h", 40) == "a b c d e f g\xa0h"
    assert mod._sanitize_owner_text("x" * 100, 10) == "x" * 10


def test_the_failure_notice_is_cut_at_the_cap_when_its_fields_run_long(home, monkeypatch):
    """With site / kind / reason at their sanitiser limits the line would exceed the cap; it is cut to
    exactly _OWNER_NOTICE_CAP (480). Real values never reach it (the longest real line is about 431
    characters), so this is the backstop's own cell. Mutation: raise or remove the cap -> red."""
    mod = load_ss(home, monkeypatch)
    six = ["_check_backup_staleness", "_check_embedding_restore_flag", "focus_block",
           "saved_focus_sets_block", "session_checkpoint_block", "sleep_status_block"]
    run = {"failure": {"site": "s" * 100, "kind": "k" * 100,
                       "result": type("R", (), {"reason": "r" * 100})(), "cause": None},
           "wait": {"ms": 10 ** 9, "outcome": "still_down"}, "degraded": six}
    assert len(mod._owner_failure_text(run)) == mod._OWNER_NOTICE_CAP == 480


# -- cells for the four round-2 suggestions (S1 closes, S2 propagation, S3 wait bound, S4 first site) --

class _SpyConn:
    """A proxy over a real read-only connection: counts `close()` calls; with `fail` every
    `execute` raises a sqlite3.Error (the read raising on the normal direct path)."""

    def __init__(self, real, fail):
        self._real, self._fail, self.closes = real, fail, 0

    def execute(self, *a, **k):
        if self._fail:
            raise sqlite3.OperationalError("injected read failure")
        return self._real.execute(*a, **k)

    def close(self):
        self.closes += 1
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


CLOSE_CASES = ["_check_backup_staleness", "_check_embedding_restore_flag", "focus_block",
               "saved_focus_sets_block", "session_checkpoint_block", "sleep_read_1", "sleep_read_2"]


@pytest.mark.parametrize("case,fail", [
    pytest.param(c, f, id=f"{c}-{'read-raises-sqlite-error' if f else 'normal'}")
    for c in CLOSE_CASES for f in (False, True)
    if not (f and c.startswith("sleep_read"))])
def test_s1_the_direct_path_closes_every_connection_it_opens(case, fail, home, monkeypatch):
    """Flag OFF, called outside a fire: each of the six readers opens through `connect_regular_ro`
    and calls `.close()` on the handle exactly once, on the normal path AND when the read raises a
    sqlite3.Error (sleep_status_block has two opens: marker absent = read 1, stale marker = read 2).
    Mutation: delete the close in ONE reader (one mutation per reader/read) -> that case's
    closes == 0 -> red.
    The sleep_status_block error path (read raises a sqlite3.Error) is deliberately NOT pinned: the
    base code leaks there too (close sits after the read, no finally); pre-existing, tracked in the PR body."""
    build_store(home)
    set_flag(home, False)
    mod = load_ss(home, monkeypatch)
    assert mod._OWNER_RUN is None
    opened = []
    real_open = mod.connect_regular_ro

    def spy(*a, **k):
        c = _SpyConn(real_open(*a, **k), fail)
        opened.append(c)
        return c
    monkeypatch.setattr(mod, "connect_regular_ro", spy)
    if case == "sleep_read_1":
        set_sleep_marker(home, None)
    elif case == "sleep_read_2":
        set_sleep_marker(home, STALE_MARKER)
    name = "sleep_status_block" if case.startswith("sleep_read") else case
    try:
        call_fn(mod, name, home)
    except Exception:  # noqa: BLE001 -- a re-raise on the failing read is allowed; the close is the subject
        pass
    assert len(opened) == 1, (case, len(opened))
    assert [c.closes for c in opened] == [1], (case, fail)


class _NoneRowConn:
    """execute(...).fetchone() yields None, so indexing [0] raises TypeError (not a sqlite3.Error)."""

    def execute(self, *a, **k):
        return self

    def fetchone(self):
        return None

    def close(self):
        pass


@pytest.mark.parametrize("flag_on", [False, True], ids=["flag-off", "flag-on"])
@pytest.mark.parametrize("read", ["read_1_marker_absent", "read_2_stale_marker"])
def test_s2_a_non_owner_non_sqlite_error_in_sleep_status_block_propagates(read, flag_on, home, monkeypatch):
    """A TypeError out of either read of `sleep_status_block` (fetchone() is None, then [0]) escapes
    the function on both flag paths: it is not turned into '' and is NOT recorded as an owner failure.
    Mutation: make the handler's condition never raise (`... and not isinstance(exc, sqlite3.Error)`
    -> `False`), for read 1 and for read 2 separately -> the matching cells get '' instead -> red."""
    build_store(home)
    set_flag(home, flag_on)
    mod = load_ss(home, monkeypatch)
    set_sleep_marker(home, None if read.startswith("read_1") else STALE_MARKER)
    if flag_on:
        monkeypatch.setattr(mod, "_owner_conn", lambda: _NoneRowConn())
    else:
        monkeypatch.setattr(mod, "connect_regular_ro", lambda *a, **k: _NoneRowConn())
    with Fire(mod, on=flag_on) as run:
        with pytest.raises(TypeError):
            mod.sleep_status_block()
        assert run["failure"] is None and run["degraded"] == []


@pytest.mark.parametrize("order", [("focus_block", "saved_focus_sets_block"),
                                   ("saved_focus_sets_block", "focus_block")], ids=["focus-first", "saved-first"])
def test_s4_the_notice_names_the_first_failing_site(order, home, monkeypatch):
    """Two different readers fail in one fire: `run["failure"]["site"]` and the substrate-health
    line name the FIRST one; `run["degraded"]` lists both, in order.
    Mutation: overwrite `run["failure"]` on every failure (keep the last) -> the second site is
    named -> red in the focus-first and saved-first orders alike."""
    build_store(home)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    first, second = order
    arm_failure(mod, monkeypatch, ("unavailable", "timeout"), FN_SQL[first] | FN_SQL[second], home)
    with FakeOwner(home):
        with Fire(mod) as run:
            assert call_fn(mod, first, home) == ""
            assert call_fn(mod, second, home) == ""
    assert run["failure"]["site"] == first
    assert run["degraded"] == [first, second]
    line = mod._owner_failure_text(run)
    assert f"owner read failed at {first} " in line and f"at {second}" not in line, line


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def mono(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_s3_a_cold_start_wait_of_hung_pings_is_bounded_by_the_wait_plus_one_poll(home, monkeypatch):
    """Every ping blocks for its whole timeout and answers Unavailable("timeout"): the wait still
    ends within OWNER_COLD_START_WAIT_S plus at most one poll sleep, outcome still_down, and every
    ping timeout is > 0 and never exceeds the time left to the wait deadline.
    Mutation: pass `min(OWNER_PING_TIMEOUT_S, left)` as the ping timeout again (deadline checked only
    after the ping) -> three hung pings reach 650 ms -> red."""
    mod = load_ss(home, monkeypatch)
    clock = _FakeClock()
    monkeypatch.setattr(mod, "_mono", clock.mono)
    monkeypatch.setattr(mod, "_sleep", clock.sleep)
    pings = []

    def fake_read(method, params, **k):
        assert method == "ping"
        pings.append((k["timeout"], clock.now))
        clock.now += k["timeout"]  # a hung ping blocks for its whole timeout
        return _unavailable("timeout", home)
    fake_ec = type("FakeEc", (), {"read": staticmethod(fake_read), "Ok": ec.Ok, "Rejected": ec.Rejected})
    down = ec.OwnerReadError("unavailable", _unavailable("down", home))
    with Fire(mod) as run:
        run["ec"], run["t0"] = fake_ec, clock.now
        start = clock.now
        deadline = start + mod.OWNER_COLD_START_WAIT_S
        assert mod._owner_cold_start_wait(down) is False
        wait = run["wait"]
    assert wait["outcome"] == "still_down"
    assert wait["ms"] <= (mod.OWNER_COLD_START_WAIT_S + mod.OWNER_COLD_START_POLL_S) * 1000, wait
    assert pings, "no ping was sent"
    for timeout, at in pings:
        assert 0 < timeout <= deadline - at + 1e-9, (timeout, at, deadline)
