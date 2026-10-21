"""engram-alpha#2652 (tools-9) -- `engram-pkg scope-export` reads ONE engramd owner snapshot behind its own flag.

Behind `engramd.scope_export_reads_via_owner` (default OFF) the tool no longer opens the live knowledge.db.
It asks the engramd owner for ONE quiesced copy (`snapshot`, a typed owner op) into a 0700 temp dir inside
ENGRAM_HOME, opens THAT copy `mode=ro`, and runs `_compute_provenance_closure` on it unchanged. Why one
snapshot and not the owner's read templates: the closure uses `sqlite3.Row`, its SELECTs are not registered
templates, the BFS makes one call per node under one budget, and successive owner reads share no point in
time. One snapshot is one consistent point in time and leaves the closure code untouched.

Contract (each cell red before the implementation; S8 may already pass: it pins what must NOT change):

  S1  differential: flag ON vs OFF on one store (3 roots, a closure of >= 50 nodes crossing derives_from /
      cites / supersedes / contradicts edges) -> byte-identical knowledge.sql, equal content_hash, node_count,
      edge_count, and an equal manifest apart from created_at. A control: an owner whose snapshot drops one
      edge row reddens the harness.
  S2  never the live store: flag ON -> zero connects that resolve to the live knowledge.db; the one store
      connect targets the snapshot path inside the temp dir.
  S3  owner down: non-zero exit naming the flag, no --out, no temp dir, no connect to the live store.
  S4  indeterminate is failure: Unavailable(not down), Rejected(indeterminate), and the malformed-but-OK class
      -> non-zero, no --out, no temp dir, the dest file never opened. S4b: an OK answer over a file that is
      not a database is an error too, never a fallback to the live store.
  S5  cleanup on closure exit: a missing root id, and --max-nodes below the closure size -> the tool's
      existing error text and exit, and the temp dir is gone.
  S6  temp hygiene: 0700, inside ENGRAM_HOME, never inside --out; gone after success.
  S6b the snapshot name stays git-ignored (ENGRAM_HOME is a git repo on seats with the git backup): it must
      match a pattern of engram_core.ENGRAM_GITIGNORE_PATTERNS. A pin cell holds that constant equal to the
      .gitignore `_ensure_engram_gitignore` writes.
  S7  stale sweep of `.engram-pkg-owner-*` in ENGRAM_HOME, on both flag paths (as backup C9).
  S8  flag OFF unchanged: the owner is never contacted; exactly one direct mode=ro connect to the live store.
  S9  the flag cannot apply (the flag is read from THIS ENGRAM_HOME's config; an owner serving another home
      is never contacted) -> the flag-OFF path.

The in-process FakeOwner has no `snapshot` op, so `SnapshotOwner` below adds one that does what the real op
does for this tool's purposes (copy of tests/test_backup_owner_snapshot_3872.py's).
"""

from __future__ import annotations

import builtins
import contextlib
import datetime
import fnmatch
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import types
import urllib.parse
from pathlib import Path

import pytest

from _fake_owner import FakeOwner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402

TOOL_PATH = REPO_ROOT / "src" / "engram" / "tools" / "engram-pkg" / "engram-pkg"
FLAG_KEY = "scope_export_reads_via_owner"
TMP_PREFIX = ".engram-pkg-owner-"
ROOTS = ["fxd-1", "fxd-2", "fxd-3"]


@pytest.fixture(autouse=True)
def _restore_imports():
    """Undo only what this tool's own imports add (the `tools` package and the engramd_* runtime modules:
    the tools/-package-name-clash class, #2565). Third-party modules the tool pulls in (beartype, ...) must
    stay: deleting them mid-process breaks their re-import."""
    mods, path = dict(sys.modules), list(sys.path)
    yield
    for name in [n for n in sys.modules if n not in mods and (n == "tools" or n.startswith(("tools.", "engramd_")))]:
        del sys.modules[name]
    for name, mod in mods.items():
        if (name == "tools" or name.startswith(("tools.", "engramd_"))) and sys.modules.get(name) is not mod:
            sys.modules[name] = mod
    sys.path[:] = path


@pytest.fixture
def home(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="se2652-"))
    # HOME is a temp dir too: an unchanged tool defaults to ~/.engram, and a red run must never reach the
    # real seat's store.
    fake_home = Path(tempfile.mkdtemp(prefix="se2652-home-"))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    monkeypatch.setenv("ENGRAM_NO_POLARITY", "1")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(fake_home, ignore_errors=True)


# ---- the fixture store ---------------------------------------------------------------------------------

def make_store(home: Path) -> Path:
    """3 roots (derivations), 45 observations, 8 evidence nodes -> a closure of 56 nodes. derives_from edges
    root -> observation, cites edges observation -> evidence, one supersedes edge, one contradicts edge across
    root groups. Outside the closure: 20 unrelated nodes and one edge INTO the closure from outside (it must
    not be exported). Fixture ids deliberately avoid the real graph's `xx_NNNN` shape."""
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        ts = "2026-01-01T00:00:00"

        def node(nid, typ, claim, conf=None, quote=None):
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, confidence, quoted_text) VALUES (?, ?, ?, ?, ?, ?)",
                (nid, typ, claim, ts, conf, quote))

        def edge(src, dst, rel):
            conn.execute("INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
                         (src, dst, rel, "2026-01-02T00:00:00"))

        for i, r in enumerate(ROOTS):
            node(r, "derivation", f"root {i} -- O'Brien said \"yes\"", conf=0.75 + i / 100)
        for k in range(1, 9):
            node(f"fxe-{k:02d}", "evidence", f"evidence {k}")
        for i in range(1, 46):
            node(f"fxo-{i:02d}", "observation", f"observation {i}", conf=0.85, quote=f"quote {i}\nline two")
        for i in range(1, 46):
            edge(ROOTS[(i - 1) // 15], f"fxo-{i:02d}", "derives_from")
            edge(f"fxo-{i:02d}", f"fxe-{(i % 8) + 1:02d}", "cites")
        edge("fxo-02", "fxo-01", "supersedes")
        edge("fxo-20", "fxo-35", "contradicts")
        for i in range(20):
            node(f"fxu-{i:02d}", "observation", f"unrelated {i}")
        edge("fxu-00", "fxo-05", "supports")  # incoming from OUTSIDE the closure
        edge("fxu-01", "fxu-02", "supports")
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


def set_flag(home: Path, value=True) -> None:
    (home / "config.json").write_text(json.dumps({"engramd": {FLAG_KEY: value}}), encoding="utf-8")


def load_tool(home: Path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_loader(
        "engram_pkg_cli_2652", importlib.machinery.SourceFileLoader("engram_pkg_cli_2652", str(TOOL_PATH)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scope_export(mod, out: Path, nodes=None, **kw):
    """One run of cmd_scope_export -> (rc, stdout, stderr, message). A SystemExit carrying a string is the
    tool's error exit (rc 1, the string is the message)."""
    args = types.SimpleNamespace(nodes=",".join(nodes or ROOTS), out=str(out), name=None,
                                 max_nodes=kw.get("max_nodes", 200), max_edges=kw.get("max_edges", 400))
    so, se = io.StringIO(), io.StringIO()
    rc, msg = 0, ""
    with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
        try:
            rc = mod.cmd_scope_export(args) or 0
        except SystemExit as exc:
            if exc.code is None or exc.code == 0:
                rc = 0
            elif isinstance(exc.code, int):
                rc = exc.code
            else:
                rc, msg = 1, str(exc.code)
    return rc, so.getvalue(), se.getvalue(), msg


# ---- the fake owner's snapshot op ----------------------------------------------------------------------

class SnapshotOwner(FakeOwner):
    """FakeOwner + a `snapshot` op. `behavior` picks the answer:

    ok             a consistent copy at dest_path, answered {"dest_path": dest}
    drop_one_edge  like ok, but the copy lost ONE edge row (a faulty NEW side)
    indeterminate  Rejected(deadline_exceeded, commit="indeterminate"), the copy written
    garbage        an unrecognised status -> the client's Unavailable("protocol")
    other_dest     the copy written at dest_path, but the answer names a DIFFERENT path
    non_dict       the copy written, the answer's result is a string
    no_file        a well-formed answer, but nothing was written
    rejected       Rejected(some_reason)
    not_a_db       a well-formed answer over a file that is not a SQLite store
    unreadable     a well-formed answer over a regular file of mode 000 (sqlite3.connect itself cannot open it)
    """

    def __init__(self, home, behavior="ok"):
        super().__init__(home)
        self.behavior = behavior
        self.snapshot_requests = []
        self.dest_dir_modes = []

    def _copy(self, dest: str, *, drop_one_edge=False) -> None:
        src = sqlite3.connect((self.home / "knowledge.db").resolve().as_uri() + "?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
                if drop_one_edge:
                    dst.execute("DELETE FROM edges WHERE rowid = "
                                "(SELECT MIN(rowid) FROM edges WHERE relation = 'contradicts')")
                    dst.commit()
            finally:
                dst.close()
        finally:
            src.close()

    def _answer(self, request):
        if request.get("method") != "snapshot":
            return super()._answer(request)
        dest = request["params"]["dest_path"]
        self.snapshot_requests.append(dest)
        self.dest_dir_modes.append(stat.S_IMODE(os.stat(os.path.dirname(dest)).st_mode))
        b = self.behavior
        if b == "garbage":
            return {"status": "bogus"}
        if b == "rejected":
            return {"status": "rejected", "reason": "snapshot_failed", "detail": "d", "remedy": "r"}
        if b == "indeterminate":
            self._copy(dest)
            return {"status": "rejected", "reason": "deadline_exceeded", "detail": "d", "remedy": "r",
                    "commit": "indeterminate"}
        if b == "no_file":
            return {"status": "ok", "result": {"dest_path": dest}}
        if b == "unreadable":
            Path(dest).write_bytes(b"x")
            os.chmod(dest, 0)
            return {"status": "ok", "result": {"dest_path": dest}}
        if b == "not_a_db":
            Path(dest).write_bytes(b"not a database")
            return {"status": "ok", "result": {"dest_path": dest}}
        self._copy(dest, drop_one_edge=(b == "drop_one_edge"))
        if b == "other_dest":
            return {"status": "ok", "result": {"dest_path": dest + ".elsewhere"}}
        if b == "non_dict":
            return {"status": "ok", "result": "done"}
        return {"status": "ok", "result": {"dest_path": dest}}


class Spy:
    """Record, on THIS thread only (the in-process fake owner serves from other threads), every sqlite3
    connect and every builtins.open, plus every tempfile.mkdtemp result."""

    def __init__(self):
        self.connects, self.opens, self.mkdtemps = [], [], []
        self._thread = threading.get_ident()

    def install(self, monkeypatch):
        real_connect, real_dbapi, real_open, real_mkdtemp = (
            sqlite3.connect, sqlite3.dbapi2.connect, builtins.open, tempfile.mkdtemp)

        def conn_guard(real):
            def f(*a, **k):
                if threading.get_ident() == self._thread:
                    self.connects.append(str(a[0]) if a else "")
                return real(*a, **k)
            return f

        def open_guard(*a, **k):
            if threading.get_ident() == self._thread:
                self.opens.append(str(a[0]) if a else "")
            return real_open(*a, **k)

        def mkdtemp_guard(*a, **k):
            path = real_mkdtemp(*a, **k)
            if threading.get_ident() == self._thread:
                self.mkdtemps.append((path, k.get("dir"), k.get("prefix")))
            return path

        monkeypatch.setattr(sqlite3, "connect", conn_guard(real_connect))
        monkeypatch.setattr(sqlite3.dbapi2, "connect", conn_guard(real_dbapi))
        monkeypatch.setattr(builtins, "open", open_guard)
        monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp_guard)
        return self


def _target(connect_arg: str) -> str:
    """The filesystem path a connect argument names (plain path or `file:` URI, query string dropped)."""
    if connect_arg.startswith("file:"):
        return urllib.parse.unquote(urllib.parse.urlparse(connect_arg).path)
    return connect_arg


def live_connects(spy: Spy, live: Path) -> list:
    want = os.path.realpath(str(live))
    return [c for c in spy.connects if c and os.path.realpath(_target(c)) == want]


def snapshot_connects(spy: Spy) -> list:
    return [c for c in spy.connects if TMP_PREFIX in c]


def tmpdirs(home: Path) -> list:
    return sorted(p.name for p in home.glob(TMP_PREFIX + "*"))


def read_manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def export_both(home, monkeypatch, owner_behavior="ok"):
    """One store, two runs differing only in the flag. Returns (off_out, on_out)."""
    off_out, on_out = home / "out-off", home / "out-on"
    set_flag(home, False)
    rc, so, se, msg = scope_export(load_tool(home, monkeypatch), off_out)
    assert rc == 0, (so, se, msg)
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with SnapshotOwner(home, owner_behavior) as owner:
        rc, so, se, msg = scope_export(mod, on_out)
        assert rc == 0, (so, se, msg)
    assert owner.snapshot_requests, "the flag-ON leg never asked the owner for a snapshot"
    return off_out, on_out


def assert_identical(off_out: Path, on_out: Path) -> None:
    assert (on_out / "knowledge.sql").read_bytes() == (off_out / "knowledge.sql").read_bytes()
    off, on = read_manifest(off_out), read_manifest(on_out)
    for m in (off, on):
        m.pop("created_at")
    assert on == off


# ---- S1: differential ----------------------------------------------------------------------------------

def test_s1_owner_and_direct_exports_are_byte_identical(home, monkeypatch):
    make_store(home)
    off_out, on_out = export_both(home, monkeypatch)
    assert_identical(off_out, on_out)
    off, on = read_manifest(off_out), read_manifest(on_out)
    assert on["content_hash"] == off["content_hash"]
    # not vacuous: the closure is big enough and crosses all four relations
    assert on["node_count"] >= 50 and on["edge_count"] > 0
    conn = sqlite3.connect(str(on_out / "knowledge.db"))
    try:
        rels = {r[0] for r in conn.execute("SELECT DISTINCT relation FROM edges")}
        n_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        outside = conn.execute("SELECT COUNT(*) FROM nodes WHERE id LIKE 'fxu-%'").fetchone()[0]
    finally:
        conn.close()
    assert rels == {"derives_from", "cites", "supersedes", "contradicts"}, rels
    assert n_nodes == on["node_count"] and outside == 0


def test_s1_control_an_owner_that_drops_one_edge_row_reddens_the_harness(home, monkeypatch):
    make_store(home)
    off_out, on_out = export_both(home, monkeypatch, owner_behavior="drop_one_edge")
    with pytest.raises(AssertionError):
        assert_identical(off_out, on_out)


# ---- S2: never the live store --------------------------------------------------------------------------

def test_s2_flag_on_never_connects_to_the_live_store_and_opens_the_snapshot_in_the_temp_dir(home, monkeypatch):
    live = make_store(home)
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(home) as owner:
        rc, so, se, msg = scope_export(mod, out)
    assert rc == 0, (so, se, msg)
    assert len(owner.snapshot_requests) == 1
    assert live_connects(spy, live) == [], spy.connects
    made = [m for m in spy.mkdtemps if m[2] == TMP_PREFIX]
    assert len(made) == 1, spy.mkdtemps
    snap_connects = snapshot_connects(spy)
    assert len(snap_connects) == 1, ("the store connect count is not exactly one", spy.connects)
    assert os.path.realpath(_target(snap_connects[0])) == os.path.realpath(os.path.join(made[0][0], "knowledge.db"))
    assert "mode=ro" in snap_connects[0]


# ---- S3: owner down ------------------------------------------------------------------------------------

def test_s3_owner_down_exits_nonzero_names_the_flag_and_leaves_nothing(home, monkeypatch):
    live = make_store(home)
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    rc, so, se, msg = scope_export(mod, out)
    assert rc != 0, (so, se)
    assert msg.startswith("ERROR: scope-export:"), msg
    assert f"engramd.{FLAG_KEY}" in msg, msg
    assert "down" in msg.lower() and "indeterminate" not in msg.lower(), msg
    assert not out.exists()
    assert tmpdirs(home) == []
    assert live_connects(spy, live) == [], spy.connects


# ---- S4: indeterminate is failure ----------------------------------------------------------------------

@pytest.mark.parametrize("behavior", ["garbage", "indeterminate", "rejected", "other_dest", "non_dict", "no_file"])
def test_s4_every_non_success_answer_is_an_error_with_nothing_written(home, monkeypatch, behavior):
    live = make_store(home)
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(home, behavior) as owner:
        rc, so, se, msg = scope_export(mod, out)
    assert owner.snapshot_requests, "the tool never asked the owner for a snapshot"
    assert rc != 0, (so, se)
    assert msg.startswith("ERROR: scope-export:"), msg
    assert f"engramd.{FLAG_KEY}" in msg, msg
    assert not out.exists()
    assert tmpdirs(home) == []
    touched = [p for p in spy.opens + spy.connects if TMP_PREFIX in p]
    assert touched == [], f"the file at dest_path was opened on a failure path: {touched}"
    assert live_connects(spy, live) == [], spy.connects


def test_s4_indeterminate_wording_says_so(home, monkeypatch):
    make_store(home)
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    for behavior in ("garbage", "indeterminate"):
        with SnapshotOwner(home, behavior):
            rc, so, se, msg = scope_export(mod, home / "out")
        assert rc != 0 and "indeterminate" in msg.lower() and "nothing was archived" in msg.lower(), msg


def test_s4b_an_ok_answer_over_a_file_that_is_not_a_database_is_an_error_never_a_fallback(home, monkeypatch):
    live = make_store(home)
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(home, "not_a_db") as owner:
        rc, so, se, msg = scope_export(mod, out)  # an uncaught sqlite3 error would propagate and fail here
    assert owner.snapshot_requests
    assert rc != 0 and msg.startswith("ERROR: scope-export:"), (rc, msg)
    assert not out.exists() and tmpdirs(home) == []
    assert live_connects(spy, live) == [], "the unreadable-snapshot handler reached for the live store"


def test_s4c_a_snapshot_copy_that_cannot_be_opened_is_a_typed_error_never_a_fallback(home, monkeypatch):
    """Measured: sqlite3.connect(file:<path>?mode=ro) raises OperationalError at the CONNECT itself for a mode-000
    file (also for a directory), unlike a non-database file, which only fails at the first query (S4b)."""
    live = make_store(home)
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(home, "unreadable") as owner:
        rc, so, se, msg = scope_export(mod, out)  # an uncaught sqlite3 error would propagate and fail here
    assert owner.snapshot_requests
    assert len(snapshot_connects(spy)) == 1, ("the connect under test was never reached", spy.connects)
    assert rc != 0 and msg.startswith("ERROR: scope-export:"), (rc, msg)
    assert f"engramd.{FLAG_KEY}" in msg, msg
    assert not out.exists() and tmpdirs(home) == []
    assert live_connects(spy, live) == []


# ---- S5: cleanup on closure exit -----------------------------------------------------------------------

@pytest.mark.parametrize("case", ["missing_root", "size_guard"])
def test_s5_a_closure_exit_keeps_the_existing_error_and_removes_the_temp_dir(home, monkeypatch, case):
    live = make_store(home)
    kw, nodes = {}, None
    if case == "missing_root":
        nodes = ["fxd-1", "fxd-absent"]
    else:
        kw = {"max_nodes": 10}
    set_flag(home, False)
    rc_off, _, _, msg_off = scope_export(load_tool(home, monkeypatch), home / "out-off", nodes=nodes, **kw)
    assert rc_off == 1 and msg_off.startswith("ERROR: scope-export:"), msg_off
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(home) as owner:
        rc, so, se, msg = scope_export(mod, home / "out-on", nodes=nodes, **kw)
    assert owner.snapshot_requests, "the closure ran without an owner snapshot"
    assert (rc, msg) == (rc_off, msg_off)
    assert not (home / "out-on").exists()
    assert tmpdirs(home) == []
    assert live_connects(spy, live) == [], spy.connects


# ---- S6: temp hygiene ----------------------------------------------------------------------------------

def test_s6_the_temp_dir_is_0700_inside_engram_home_never_inside_out_and_gone_after_success(home, monkeypatch):
    make_store(home)
    out = home / "elsewhere" / "pkg"
    out.parent.mkdir()
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(home) as owner:
        rc, so, se, msg = scope_export(mod, out)
    assert rc == 0, (so, se, msg)
    made = [m for m in spy.mkdtemps if m[2] == TMP_PREFIX]
    assert len(made) == 1, spy.mkdtemps
    path, parent, _ = made[0]
    assert Path(parent).resolve() == home.resolve() and Path(path).parent.resolve() == home.resolve()
    assert out.resolve() not in Path(path).resolve().parents and Path(path).resolve() != out.resolve()
    assert owner.dest_dir_modes == [0o700]
    assert os.path.isabs(owner.snapshot_requests[0])
    assert not Path(path).exists() and tmpdirs(home) == []
    # nothing of the store copy reached the package
    assert not any(p.name.startswith(TMP_PREFIX) for p in out.rglob("*"))


# ---- S6b: the snapshot name stays git-ignored ----------------------------------------------------------

def ignored(rel: str, patterns) -> bool:
    """Approximate git's matching for the shapes engram_core's list uses: `dir/` matches any path component,
    a slash-less pattern matches the basename."""
    parts = rel.split("/")
    for p in patterns:
        if p.startswith("!"):
            continue
        if p.endswith("/"):
            if any(fnmatch.fnmatch(c, p[:-1]) for c in parts[:-1]):
                return True
        elif fnmatch.fnmatch(parts[-1], p):
            return True
    return False


def test_s6b_the_snapshot_name_matches_the_canonical_gitignore_list(home, monkeypatch):
    make_store(home)
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    with SnapshotOwner(home) as owner:
        rc, so, se, msg = scope_export(mod, home / "out")
    assert rc == 0, (so, se, msg)
    rel = os.path.relpath(owner.snapshot_requests[0], str(home.resolve())).replace(os.sep, "/")
    if rel.startswith(".."):
        rel = os.path.relpath(owner.snapshot_requests[0], str(home)).replace(os.sep, "/")
    assert rel.startswith(TMP_PREFIX), rel
    # the matcher has teeth: a .sqlite name is NOT ignored, a .db one is
    assert not ignored(f"{TMP_PREFIX}x/knowledge.sqlite", engram_core.ENGRAM_GITIGNORE_PATTERNS)
    assert ignored(f"{TMP_PREFIX}x/knowledge.db", engram_core.ENGRAM_GITIGNORE_PATTERNS)
    assert ignored(rel, engram_core.ENGRAM_GITIGNORE_PATTERNS), f"{rel} is not covered by the canonical .gitignore list"
    # SQLite's side files for that name are covered too
    for suffix in ("-wal", "-shm", "-journal"):
        assert ignored(rel + suffix, engram_core.ENGRAM_GITIGNORE_PATTERNS), rel + suffix


def test_s6b_pin_the_constant_equals_the_gitignore_the_core_writes(tmp_path):
    engram_core._ensure_engram_gitignore(tmp_path)
    lines = [ln for ln in (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("!")]
    assert lines == list(engram_core.ENGRAM_GITIGNORE_PATTERNS)


# ---- S7: stale sweep -----------------------------------------------------------------------------------

def plant(path: Path, hours_old: float) -> Path:
    path.mkdir(parents=True)
    (path / "knowledge.db").write_bytes(b"orphan store copy")
    t = datetime.datetime.now().timestamp() - hours_old * 3600
    os.utime(path, (t, t))
    return path


def run_mode(mode, home, monkeypatch, out):
    set_flag(home, mode == "on")
    mod = load_tool(home, monkeypatch)
    if mode == "on":
        with SnapshotOwner(home):
            return mod, scope_export(mod, out)
    return mod, scope_export(mod, out)


@pytest.mark.parametrize("mode", ["on", "off"])
def test_s7a_a_stale_owner_temp_dir_is_removed_on_both_flag_paths(home, monkeypatch, mode):
    make_store(home)
    stale = plant(home / (TMP_PREFIX + "xxxx"), 7)
    mod, (rc, so, se, msg) = run_mode(mode, home, monkeypatch, home / "out")
    assert rc == 0, (so, se, msg)
    assert not stale.exists()
    assert "removed stale owner temp dir" in so and str(stale) in so, so


@pytest.mark.parametrize("mode", ["on", "off"])
def test_s7b_a_fresh_dir_is_kept_a_concurrent_run_is_protected(home, monkeypatch, mode):
    make_store(home)
    fresh = plant(home / (TMP_PREFIX + "xxxx"), 1)
    mod, (rc, so, se, msg) = run_mode(mode, home, monkeypatch, home / "out")
    assert rc == 0, (so, se, msg)
    assert fresh.is_dir() and (fresh / "knowledge.db").read_bytes() == b"orphan store copy"
    assert "removed stale" not in so


@pytest.mark.parametrize("mode", ["on", "off"])
def test_s7c_the_match_is_the_exact_prefix_not_a_glob(home, monkeypatch, mode):
    make_store(home)
    planted = [plant(home / n, 7) for n in (".engram-pkg-owner", "x.engram-pkg-owner-old", "engram-pkg-owner-old")]
    mod, (rc, so, se, msg) = run_mode(mode, home, monkeypatch, home / "out")
    assert rc == 0, (so, se, msg)
    assert all(p.is_dir() and (p / "knowledge.db").exists() for p in planted)
    assert "removed stale" not in so


@pytest.mark.parametrize("mode", ["on", "off"])
def test_s7d_a_file_or_a_symlink_with_the_prefix_is_never_removed_or_followed(home, monkeypatch, mode):
    make_store(home)
    old = datetime.datetime.now().timestamp() - 7 * 3600
    f = home / (TMP_PREFIX + "file")
    f.write_text("x")
    os.utime(f, (old, old))
    outside = plant(home / "elsewhere" / "old-dir", 7)
    link = home / (TMP_PREFIX + "link")
    link.symlink_to(outside)
    mod, (rc, so, se, msg) = run_mode(mode, home, monkeypatch, home / "out")
    assert rc == 0, (so, se, msg)
    assert f.is_file() and link.is_symlink()
    assert outside.is_dir() and (outside / "knowledge.db").exists()
    assert "removed stale" not in so and "sweep failed" not in se, (so, se)


@pytest.mark.parametrize("mode", ["on", "off"])
def test_s7e_a_failing_sweep_never_fails_the_run(home, monkeypatch, mode):
    make_store(home)
    stale = plant(home / (TMP_PREFIX + "xxxx"), 7)
    set_flag(home, mode == "on")
    mod = load_tool(home, monkeypatch)
    real_rmtree = shutil.rmtree

    def rmtree_guard(path, *a, **k):
        # Fail ONLY the sweep's call (the planted stale dir); the run's own cleanup goes through.
        if os.path.abspath(str(path)) == os.path.abspath(str(stale)):
            raise PermissionError("denied")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(shutil, "rmtree", rmtree_guard)
    if mode == "on":
        with SnapshotOwner(home):
            rc, so, se, msg = scope_export(mod, home / "out")
    else:
        rc, so, se, msg = scope_export(mod, home / "out")
    assert rc == 0, (so, se, msg)
    warnings = [ln for ln in se.splitlines() if "warning: stale owner temp-dir sweep failed" in ln]
    assert len(warnings) == 1 and "PermissionError" in warnings[0], se
    assert stale.is_dir()


# ---- S8: flag OFF unchanged ----------------------------------------------------------------------------

def test_s8_flag_off_never_contacts_the_owner_and_connects_to_the_live_store_exactly_once(home, monkeypatch):
    live = make_store(home)
    set_flag(home, False)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(home) as owner:
        rc, so, se, msg = scope_export(mod, home / "out")
    assert rc == 0, (so, se, msg)
    assert owner.requests == [] and owner.snapshot_requests == []
    hits = live_connects(spy, live)
    assert len(hits) == 1 and "mode=ro" in hits[0], spy.connects
    assert [m for m in spy.mkdtemps if m[2] == TMP_PREFIX] == []
    assert f"computing closure from {live.resolve()}" in so, so


# ---- S9: the flag cannot apply -------------------------------------------------------------------------

def test_s9_an_owner_serving_another_home_is_never_contacted(home, monkeypatch):
    """The flag is read from THIS ENGRAM_HOME's config. Home A has it ON and an owner running; the tool runs
    against home B (flag absent): the direct path, the owner untouched, the output equal to a flag-OFF run."""
    home_a, home_b = home / "a", home / "b"
    home_a.mkdir()
    home_b.mkdir()
    make_store(home_a)
    live_b = make_store(home_b)
    set_flag(home_a, True)
    mod = load_tool(home_b, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(home_a) as owner:
        rc, so, se, msg = scope_export(mod, home / "out-b")
    assert rc == 0, (so, se, msg)
    assert owner.requests == []
    assert len(live_connects(spy, live_b)) == 1
    assert tmpdirs(home_a) == [] and tmpdirs(home_b) == []
    set_flag(home_b, False)
    rc2, so2, se2, msg2 = scope_export(load_tool(home_b, monkeypatch), home / "out-b-off")
    assert rc2 == 0
    assert (home / "out-b" / "knowledge.sql").read_bytes() == (home / "out-b-off" / "knowledge.sql").read_bytes()


# ---- B1: a relative ENGRAM_HOME with the flag OFF is today's path, byte for byte -----------------------

def test_b1_a_relative_engram_home_takes_the_flag_off_path_with_dev_s_exact_message(home, monkeypatch):
    """Dev resolves a relative ENGRAM_HOME and reports a missing store in the tool's own words. The owner path
    refuses a relative home (ValueError); that must never surface when the flag cannot apply."""
    cwd = home / "cwd"
    cwd.mkdir()
    (cwd / "relhome").mkdir()
    mod = load_tool(home, monkeypatch)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("ENGRAM_HOME", "relhome")
    with SnapshotOwner(home) as owner:
        rc, so, se, msg = scope_export(mod, home / "out")
    assert owner.requests == [] and owner.snapshot_requests == []
    assert rc == 1
    assert msg == (f"ERROR: scope-export: source graph not found at {cwd.resolve()}/relhome/knowledge.db. "
                   f"Set $ENGRAM_HOME to point at your ENGRAM data directory."), msg
    assert so == "" and se == ""
    assert not (home / "out").exists()


# ---- S-b: an unwritable ENGRAM_HOME under the flag is a typed error ------------------------------------

def test_sb_mkdtemp_failure_under_the_flag_is_typed_and_names_the_way_out(home, monkeypatch):
    live = make_store(home)
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)

    def boom(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "mkdtemp", boom)
    with SnapshotOwner(home) as owner:
        rc, so, se, msg = scope_export(mod, out)  # an uncaught OSError would propagate and fail here
    assert owner.snapshot_requests == []
    assert rc != 0 and msg.startswith("ERROR: scope-export:"), (rc, msg)
    assert f"engramd.{FLAG_KEY}" in msg and "false" in msg, msg
    assert not out.exists()
    assert live_connects(spy, live) == []
