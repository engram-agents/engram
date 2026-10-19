"""engram-alpha#3872 (U15 of #2652) -- tools/backup_knowledge_db.py reads through the owner's snapshot().

Behind `engramd.backup_reads_via_owner` (default OFF) the nightly backup no longer opens the live
knowledge.db. It asks the engramd owner for ONE quiesced snapshot (`snapshot`, a typed owner op) into a
0700 temp dir inside --out-dir, reads the watermark from that snapshot, and feeds the same snapshot to
`dump_stripped`. The helper `_owner_read.snapshot` raises the tool's typed error on EVERY non-success,
the malformed-but-OK class included.

Contract (issue #3872; each cell red before the implementation):

  C1  differential: flag ON vs OFF on the same store -> equal watermark dicts, and archives restored into
      fresh DBs are row-identical across EVERY table (empty store, no edit_history, ~1,000 nodes). The
      harness is shown to FAIL by an owner that drops one table's rows from the snapshot.
  C2  owner down, flag ON: non-zero exit naming the reason; ZERO sqlite3.connect calls on the live store;
      no archive, no new/changed .watermark.json; the temp dir removed.
  C3  indeterminate is failure: Unavailable and Rejected(commit="indeterminate") -> non-zero, nothing
      archived, temp dir removed, the file at dest_path never opened. Malformed-but-OK (a different
      dest_path, a non-dict result, no file at dest) -> the same.
  C4  temp hygiene: 0700, inside --out-dir, gone after success, after a skip, after dump_stripped raising.
  C5  store selection: --db naming another file -> the direct mode=ro path, the owner never contacted.
  C6  the skip path is unchanged under the flag; --force still forces.
  C7  ENGRAM_HOME: a temp home and no --db/--out-dir -> that home's store into that home's db-backup/.
  C8  --dry-run under the flag pings the owner, does not snapshot, creates no files.
  C9  stale owner temp-dir sweep (round 6, security seat): a killed run leaves its 0700 `.engram-bk-owner-*`
      dir (a full store copy) in --out-dir. Each run sweeps it, on both flag paths: prefix-exact (no
      glob), age-bound (a concurrent run's dir is kept), real dirs only (no file, no symlink followed),
      and a sweep failure never fails the run. The `st_uid == getuid()` check is NOT pinned by any cell
      here: ownership cannot be faked without root, so on a single-uid test seat it is unpinned.

The in-process FakeOwner has no `snapshot` op (the real owner refuses to start on some seats), so
`SnapshotOwner` below adds one that does what the real op does for this tool's purposes: a consistent
copy of the store into the requested absolute dest_path, answered as {"dest_path": str(dest)}.
"""

from __future__ import annotations

import builtins
import contextlib
import datetime
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
from pathlib import Path

import pytest

from _fake_owner import FakeOwner
from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402
import engramd_owner as eo  # noqa: E402

TOOL_PATH = REPO_ROOT / "src" / "engram" / "tools" / "backup_knowledge_db.py"
FLAG_KEY = "backup_reads_via_owner"
TMP_PREFIX = ".engram-bk-owner-"
SNAP_NAME = "knowledge.snap.db"
N_BULK = 1000


@pytest.fixture(autouse=True)
def _restore_imports():
    mods, path = dict(sys.modules), list(sys.path)
    yield
    for name in [n for n in sys.modules if n not in mods]:
        del sys.modules[name]
    for name, mod in mods.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod
    sys.path[:] = path


@pytest.fixture
def home(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="bk3872-"))
    # HOME is a temp dir too: an unchanged tool defaults to ~/.engram, and a red run must never reach the
    # real seat's store or write into its db-backup/.
    fake_home = Path(tempfile.mkdtemp(prefix="bk3872-home-"))
    monkeypatch.setenv("HOME", str(fake_home))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(fake_home, ignore_errors=True)


def make_store(home: Path, kind: str = "bulk") -> Path:
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        if kind == "bulk":
            for i in range(N_BULK):
                conn.execute(
                    "INSERT INTO nodes (id, type, claim, quoted_text, created_at) "
                    "VALUES (?, 'observation', ?, ?, ?)",
                    (f"ob_{i:04d}", f"claim {i}", f"quote {i}" if i % 3 == 0 else None,
                     f"2026-01-01T00:{i // 60 % 60:02d}:{i % 60:02d}"),
                )
            for i in range(1, N_BULK):
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supports', ?)",
                    (f"ob_{i:04d}", f"ob_{i - 1:04d}", "2026-01-02T00:00:00"))
            for i in range(0, N_BULK, 7):
                conn.execute(
                    "INSERT INTO edit_history (timestamp, turn, action, node_id, node_type) "
                    "VALUES (?, 0, 'created', ?, 'observation')", ("2026-01-03T00:00:00", f"ob_{i:04d}"))
        if kind == "no_edit_history":
            for i in range(5):
                conn.execute(
                    "INSERT INTO nodes (id, type, claim, created_at) VALUES (?, 'observation', 'c', ?)",
                    (f"ob_{i:04d}", "2026-01-01T00:00:00"))
            conn.execute("DROP TABLE edit_history")
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
    sys.path.append(str(REPO_ROOT / "src" / "engram"))
    spec = importlib.util.spec_from_file_location("backup_knowledge_db_3872", str(TOOL_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mod.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ---- the fake owner's snapshot op ---------------------------------------------------------------

class SnapshotOwner(FakeOwner):
    """FakeOwner + a `snapshot` op. `behavior` picks the answer:

    ok            a consistent copy at dest_path, answered {"dest_path": dest}
    drop_edges    like ok, but the copy lost every `edges` row (a faulty NEW side)
    indeterminate Rejected(deadline_exceeded, commit="indeterminate"), the copy written
    garbage       an unrecognised status -> the client's Unavailable("protocol")
    other_dest    the copy written at dest_path, but the answer names a DIFFERENT path
    non_dict      the copy written, the answer's result is a string
    no_file       a well-formed answer, but nothing was written
    rejected      Rejected(some_reason)
    """

    def __init__(self, home, behavior="ok"):
        super().__init__(home)
        self.behavior = behavior
        self.snapshot_requests = []
        self.dest_dir_modes = []

    def _copy(self, dest: str, *, drop_edges=False) -> None:
        src = sqlite3.connect((self.home / "knowledge.db").resolve().as_uri() + "?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
                if drop_edges:
                    dst.execute("DELETE FROM edges")
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
        self._copy(dest, drop_edges=(b == "drop_edges"))
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

    def connects_to(self, live: Path) -> list:
        needles = {str(live), str(live.resolve()), live.resolve().as_uri()}
        return [c for c in self.connects if any(n in c for n in needles)]


def live_connects(spy: "Spy", live: Path) -> list:
    """Every connect on the spy's thread whose target resolves (realpath, plain path / Path / `file:` URI,
    query string dropped) to the live store. Stricter than Spy.connects_to: no substring matching."""
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


def snapshot_connects(spy: "Spy") -> list:
    return [c for c in spy.connects if SNAP_NAME in c]


def archives(out_dir: Path) -> list:
    return sorted(p.name for p in out_dir.glob("knowledge-*.sql")) if out_dir.exists() else []


def tmpdirs(out_dir: Path) -> list:
    return sorted(p.name for p in out_dir.glob(TMP_PREFIX + "*")) if out_dir.exists() else []


def restore(sql_path: Path) -> dict:
    """Restore an archive into a fresh DB and return {table: sorted rows}, every non-internal table."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(sql_path.read_text(encoding="utf-8"))
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: sorted(repr(r) for r in conn.execute(f'SELECT * FROM "{n}"')) for n in names}
    finally:
        conn.close()


def watermark(out_dir: Path) -> dict:
    wm = json.loads((out_dir / ".watermark.json").read_text(encoding="utf-8"))
    wm.pop("archive", None)
    return wm


def backup_both(home, monkeypatch, owner_behavior="ok"):
    """One store, two runs differing only in the flag. Returns (off_out, on_out)."""
    off_out, on_out = home / "out-off", home / "out-on"
    set_flag(home, False)
    mod = load_tool(home, monkeypatch)
    rc, so, se = run(mod, ["--db", str(home / "knowledge.db"), "--out-dir", str(off_out)])
    assert rc == 0, (so, se)
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with SnapshotOwner(home, owner_behavior) as owner:
        rc, so, se = run(mod, ["--db", str(home / "knowledge.db"), "--out-dir", str(on_out)])
        assert rc == 0, (so, se)
    assert owner.snapshot_requests, "the flag-ON leg never asked the owner for a snapshot"
    return off_out, on_out


def assert_identical(off_out: Path, on_out: Path) -> None:
    assert watermark(on_out) == watermark(off_out)
    off, on = restore(next(off_out.glob("knowledge-*.sql"))), restore(next(on_out.glob("knowledge-*.sql")))
    assert sorted(off) == sorted(on), "the restored table lists differ"
    for table in off:
        assert on[table] == off[table], f"table {table!r} differs between the owner and the direct archive"


# ---- C1: differential ------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["empty", "no_edit_history", "bulk"])
def test_c1_owner_and_direct_archives_are_row_identical_in_every_table(home, monkeypatch, kind):
    make_store(home, kind)
    off_out, on_out = backup_both(home, monkeypatch)
    assert_identical(off_out, on_out)
    if kind == "bulk":  # the comparison is not vacuous: the tables it compares are populated
        rows = restore(next(on_out.glob("knowledge-*.sql")))
        assert len(rows["nodes"]) == N_BULK and len(rows["edges"]) == N_BULK - 1 and rows["edit_history"]
    if kind == "no_edit_history":
        assert "edit_history" not in restore(next(on_out.glob("knowledge-*.sql")))


def test_c1_control_an_owner_that_drops_a_tables_rows_reddens_the_harness(home, monkeypatch):
    make_store(home, "bulk")
    off_out, on_out = backup_both(home, monkeypatch, owner_behavior="drop_edges")
    with pytest.raises(AssertionError):
        assert_identical(off_out, on_out)


# ---- C2: owner down is an error ------------------------------------------------------------------------

def test_c2_owner_down_exits_nonzero_never_opens_the_live_store_and_leaves_no_trace(home, monkeypatch):
    live = make_store(home)
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    # a previous good run, so "no .watermark.json written or changed" is checked against something
    with SnapshotOwner(home):
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert rc == 0, (so, se)
    before = ((out / ".watermark.json").read_bytes(), archives(out))
    conn = sqlite3.connect(str(live))
    conn.execute("INSERT INTO nodes (id, type, claim, created_at) VALUES ('ob_new', 'observation', 'n', '2026-02-01T00:00:00')")
    conn.commit()
    conn.close()
    spy = Spy().install(monkeypatch)
    rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert rc != 0, (so, se)
    assert "owner" in se.lower() and "unavailable" in se.lower(), se
    assert spy.connects_to(live) == [], spy.connects
    assert ((out / ".watermark.json").read_bytes(), archives(out)) == before
    assert tmpdirs(out) == []


# ---- C2b / C2c: flag ON never opens the live store on the SUCCESS and SKIP paths ---------------------------

def test_c2b_success_with_the_flag_on_never_opens_the_live_store(home, monkeypatch):
    live = make_store(home)
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(home) as owner:
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert rc == 0 and "backup written" in so, (so, se)
    assert len(owner.snapshot_requests) == 1 and len(archives(out)) == 1
    assert snapshot_connects(spy), ("vacuous: the tool never opened the snapshot", spy.connects)
    assert live_connects(spy, live) == [], spy.connects


def test_c2c_skip_with_the_flag_on_never_opens_the_live_store(home, monkeypatch):
    live = make_store(home)
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    with SnapshotOwner(home) as owner:
        assert run(mod, ["--db", str(live), "--out-dir", str(out)])[0] == 0
        spy = Spy().install(monkeypatch)
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert rc == 0 and "up to date" in so, (so, se)
    assert len(owner.snapshot_requests) == 2 and len(archives(out)) == 1
    assert snapshot_connects(spy), ("vacuous: the skip decision never read the snapshot", spy.connects)
    assert live_connects(spy, live) == [], spy.connects


def test_c2b_the_live_connect_matcher_catches_path_uri_and_symlink_forms(home):
    live = make_store(home)
    link = home / "link.db"
    link.symlink_to(live)
    for form in (str(live), live.resolve().as_uri() + "?mode=ro", str(link), str(live) + "/../knowledge.db"):
        spy = Spy()
        spy.connects.append(form)
        assert live_connects(spy, live) == [form], form
    spy = Spy()
    spy.connects += [str(home / "out" / SNAP_NAME), ":memory:"]
    assert live_connects(spy, live) == []


def test_c2_owner_down_on_a_first_run_writes_no_archive_and_no_watermark(home, monkeypatch):
    live = make_store(home)
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert rc != 0 and "owner" in se.lower(), (so, se)
    assert archives(out) == [] and not (out / ".watermark.json").exists() and tmpdirs(out) == []


# ---- C3: indeterminate is failure --------------------------------------------------------------------

@pytest.mark.parametrize("behavior", ["garbage", "indeterminate", "rejected", "other_dest", "non_dict", "no_file"])
def test_c3_every_non_success_answer_is_an_error_with_nothing_archived(home, monkeypatch, behavior):
    live = make_store(home, "no_edit_history")
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(home, behavior) as owner:
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert owner.snapshot_requests, "the tool never asked the owner for a snapshot"
    assert rc != 0, (so, se)
    assert se.startswith("error:") or "\nerror:" in se, se
    assert archives(out) == [] and not (out / ".watermark.json").exists()
    assert tmpdirs(out) == []
    touched = [p for p in spy.opens + spy.connects if SNAP_NAME in p]
    assert touched == [], f"the file at dest_path was opened on a failure path: {touched}"
    assert spy.connects_to(live) == []


def test_c3_indeterminate_wording_says_so(home, monkeypatch):
    live = make_store(home, "no_edit_history")
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    for behavior in ("garbage", "indeterminate"):
        with SnapshotOwner(home, behavior):
            rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(home / "out")])
        assert rc != 0 and "indeterminate" in se.lower() and "nothing was archived" in se.lower(), se


def test_c3_owner_down_wording_says_down_not_indeterminate(home, monkeypatch):
    # No owner listening: no request reached it, so the outcome is not "indeterminate" (R12 wording split).
    live = make_store(home, "no_edit_history")
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(home / "out")])
    assert rc != 0 and "down" in se.lower() and "indeterminate" not in se.lower(), se
    assert "nothing was archived" in se.lower() and "unavailable" in se.lower(), se


def test_c3e_a_raising_client_is_a_typed_error_not_a_traceback_with_nothing_left_behind(home, monkeypatch):
    live = make_store(home, "no_edit_history")
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    from tools import _owner_read
    client = _owner_read.import_runtime("engramd_client")

    def boom(*a, **kw):
        raise RuntimeError("client blew up")

    monkeypatch.setattr(client, "read", boom)
    spy = Spy().install(monkeypatch)
    rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])  # a traceback would propagate and fail here
    assert rc == 1, (so, se)
    assert se.startswith("error:") and "RuntimeError" in se and "Traceback" not in se, se
    assert archives(out) == [] and not (out / ".watermark.json").exists() and tmpdirs(out) == []
    assert spy.connects_to(live) == []


# ---- C3b / C3c / C3d: the helper's contract edges (round-1 reviewer's surviving mutants) ----------------------

class _NotADbOwner(SnapshotOwner):
    """Answers a well-formed Ok naming dest_path, after writing a file there that is NOT a SQLite store."""

    def _copy(self, dest: str, *, drop_edges=False) -> None:
        Path(dest).write_bytes(b"not a database")


def test_c3b_an_ok_answer_over_an_unreadable_snapshot_is_an_error_and_never_falls_back_to_the_live_store(
        home, monkeypatch):
    live = make_store(home, "no_edit_history")
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    with _NotADbOwner(home, "ok") as owner:
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert owner.snapshot_requests, "the tool never asked the owner for a snapshot"
    assert rc != 0, (so, se)
    assert "error:" in se, se
    assert archives(out) == [] and not (out / ".watermark.json").exists()
    assert tmpdirs(out) == []
    assert live_connects(spy, live) == [], "the unreadable-snapshot handler reached for the live store"


def _spec(home: Path):
    from tools import _owner_read
    return _owner_read.OwnerReadSpec(
        flag_key=FLAG_KEY, error_cls=RuntimeError, home=home, never_empty="An empty answer is not a backup.")


def test_c3c_a_relative_dest_path_raises_before_any_request_reaches_the_owner(home, monkeypatch):
    from tools import _owner_read
    make_store(home, "no_edit_history")
    with SnapshotOwner(home, "ok") as owner:
        with pytest.raises(RuntimeError, match="absolute"):
            _owner_read.snapshot(_spec(home), Path("rel/knowledge.snap.db"))
    assert owner.snapshot_requests == []


def test_c3c_the_dest_path_the_tool_sends_is_absolute(home, monkeypatch):
    live = make_store(home, "no_edit_history")
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    # a RELATIVE --out-dir: the tool must still send the owner an absolute dest_path
    monkeypatch.chdir(home)
    with SnapshotOwner(home, "ok") as owner:
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", "rel-out"])
    assert rc == 0, (so, se)
    assert owner.snapshot_requests and all(os.path.isabs(d) for d in owner.snapshot_requests), \
        owner.snapshot_requests


def test_c3d_the_snapshot_call_uses_the_snapshot_timeout_not_the_per_call_timeout(home, monkeypatch):
    from tools import _owner_read
    import engramd_client
    live = make_store(home, "no_edit_history")
    spec = _spec(home)
    assert spec.snapshot_timeout != spec.per_call
    seen = []
    real_read = engramd_client.read

    def spy_read(method, params, **kw):
        seen.append((method, kw.get("timeout")))
        return real_read(method, params, **kw)

    monkeypatch.setattr(engramd_client, "read", spy_read)
    dest = home / "snap.db"
    with SnapshotOwner(home, "ok"):
        _owner_read.snapshot(spec, dest)
    assert seen == [("snapshot", spec.snapshot_timeout)], seen


def test_c3d_the_default_snapshot_timeout_outlasts_the_owners_own_snapshot_deadline():
    from tools import _owner_read
    assert _owner_read.DEFAULT_SNAPSHOT_SECONDS > eo.SNAPSHOT_REQUEST_DEADLINE


# ---- C4: temp hygiene --------------------------------------------------------------------------------

def test_c4_the_temp_dir_is_0700_inside_out_dir_and_gone_after_success(home, monkeypatch):
    live = make_store(home, "no_edit_history")
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(home) as owner:
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert rc == 0, (so, se)
    made = [m for m in spy.mkdtemps if m[2] == TMP_PREFIX]
    assert len(made) == 1, spy.mkdtemps
    path, parent, _ = made[0]
    assert Path(parent).resolve() == out.resolve() and Path(path).parent.resolve() == out.resolve()
    assert owner.dest_dir_modes == [0o700]
    assert not Path(path).exists() and tmpdirs(out) == []


def test_c4_the_temp_dir_is_gone_after_a_skip(home, monkeypatch):
    live = make_store(home, "no_edit_history")
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    with SnapshotOwner(home) as owner:
        assert run(mod, ["--db", str(live), "--out-dir", str(out)])[0] == 0
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert rc == 0 and "up to date" in so, (so, se)
    assert len(owner.snapshot_requests) == 2, "the skip must be decided on an owner snapshot"
    assert tmpdirs(out) == []


def test_c4_the_temp_dir_is_gone_after_dump_stripped_raises(home, monkeypatch):
    live = make_store(home, "no_edit_history")
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("dump exploded")

    monkeypatch.setattr(mod.engram_backup, "dump_stripped", boom)
    with SnapshotOwner(home) as owner:
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert owner.snapshot_requests, "dump_stripped was never reached via an owner snapshot"
    assert rc == 1 and "dump exploded" in se, (so, se)
    assert tmpdirs(out) == [] and archives(out) == [] and not (out / ".watermark.json").exists()


# ---- C5: store selection ---------------------------------------------------------------------------------

def test_c5_an_explicit_db_naming_another_file_stays_direct_and_never_contacts_the_owner(home, monkeypatch):
    live = make_store(home, "no_edit_history")
    other = home / "other"
    other.mkdir()
    shutil.copy(live, other / "k.db")
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    with SnapshotOwner(home) as owner:
        # this home's store goes through the owner ...
        assert run(mod, ["--db", str(live), "--out-dir", str(home / "out-a")])[0] == 0
        assert len(owner.snapshot_requests) == 1
        # ... another file never does
        spy = Spy().install(monkeypatch)
        rc, so, se = run(mod, ["--db", str(other / "k.db"), "--out-dir", str(home / "out-b")])
        assert rc == 0, (so, se)
        assert len(owner.snapshot_requests) == 1 and [r for r in owner.requests if r["method"] == "ping"] == []
    assert any("mode=ro" in c and "k.db" in c for c in spy.connects), spy.connects
    assert archives(home / "out-b") != [] and tmpdirs(home / "out-b") == []


def test_c5b_a_literal_tilde_db_is_expanded_before_the_flag_check_and_takes_the_owner_path(home, monkeypatch):
    # HOME is the fixture's temp home; this home's store lives at <HOME>/.engram, and --db arrives unexpanded.
    eng = Path(os.environ["HOME"]) / ".engram"
    eng.mkdir()
    live = make_store(eng, "no_edit_history")
    out = home / "out"
    set_flag(eng)
    mod = load_tool(eng, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(eng) as owner:
        rc, so, se = run(mod, ["--db", "~/.engram/knowledge.db", "--out-dir", str(out)])
    assert rc == 0, (so, se)
    assert len(owner.snapshot_requests) == 1, "the tilde --db did not take the owner path"
    assert live_connects(spy, live) == [], spy.connects
    assert archives(out) != [] and tmpdirs(out) == []


# ---- C6: skip path unchanged under the flag ----------------------------------------------------------------

def test_c6_skip_and_force_behave_as_before_with_the_flag_on(home, monkeypatch):
    live = make_store(home, "no_edit_history")
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    with SnapshotOwner(home) as owner:
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
        assert rc == 0 and "backup written" in so and len(owner.snapshot_requests) == 1, (so, se)
        first = archives(out)
        assert len(first) == 1
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
        assert rc == 0 and "up to date" in so and archives(out) == first, (so, se)
        assert len(owner.snapshot_requests) == 2, "the skip decision is made on a fresh snapshot"
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out), "--force"])
        assert rc == 0 and "backup written" in so and len(archives(out)) == 2, (so, se)
        assert len(owner.snapshot_requests) == 3
    assert tmpdirs(out) == []


# ---- C7: ENGRAM_HOME ----------------------------------------------------------------------------------------

def test_c7_with_no_db_and_no_out_dir_the_tool_backs_up_this_homes_store_into_this_homes_db_backup(home, monkeypatch):
    make_store(home, "no_edit_history")
    mod = load_tool(home, monkeypatch)  # flag OFF: the defaults alone are under test
    rc, so, se = run(mod, [])
    assert rc == 0, (so, se)
    assert len(archives(home / "db-backup")) == 1
    assert not (Path(os.environ["HOME"]) / ".engram").exists(), "the old ~/.engram default was used"


def test_c7_the_defaults_follow_the_flag_on_path_too(home, monkeypatch):
    make_store(home, "no_edit_history")
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    with SnapshotOwner(home) as owner:
        rc, so, se = run(mod, [])
    assert rc == 0 and len(owner.snapshot_requests) == 1, (so, se)
    assert len(archives(home / "db-backup")) == 1


def test_c7_no_home_literal_remains_in_the_tool():
    assert "~/.engram" not in TOOL_PATH.read_text(encoding="utf-8")


# ---- C8: --dry-run under the flag ------------------------------------------------------------------------------

def test_c8_dry_run_pings_the_owner_does_not_snapshot_and_creates_no_files(home, monkeypatch):
    live = make_store(home, "no_edit_history")
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    spy = Spy().install(monkeypatch)
    with SnapshotOwner(home) as owner:
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out), "--dry-run"])
    assert rc == 0, (so, se)
    assert "[dry-run] would snapshot via the engramd owner and write backup:" in so, so
    assert [r["method"] for r in owner.requests] == ["ping"]
    assert not out.exists() and spy.connects_to(live) == []


def test_c8_dry_run_with_the_owner_down_is_an_error(home, monkeypatch):
    live = make_store(home, "no_edit_history")
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(home / "out"), "--dry-run"])
    assert rc != 0 and "owner" in se.lower(), (so, se)


# ---- C9: stale owner temp-dir sweep -------------------------------------------------------------------------

def plant(path: Path, hours_old: float, *, with_file=True) -> Path:
    path.mkdir(parents=True)
    if with_file:
        (path / "knowledge.snap.db").write_bytes(b"orphan store copy")
    t = datetime.datetime.now().timestamp() - hours_old * 3600
    os.utime(path, (t, t))
    return path


def run_mode(mode, home, monkeypatch, out, extra=()):
    """One run of the tool in 'on' (flag ON, fake owner) or 'off' (flag OFF, direct) mode."""
    live = home / "knowledge.db"
    argv = ["--db", str(live), "--out-dir", str(out), *extra]
    set_flag(home, mode == "on")
    mod = load_tool(home, monkeypatch)
    if mode == "on":
        with SnapshotOwner(home):
            return mod, run(mod, argv)
    return mod, run(mod, argv)


@pytest.mark.parametrize("mode", ["on", "off"])
def test_c9a_a_stale_owner_temp_dir_is_removed_on_both_flag_paths(home, monkeypatch, mode):
    make_store(home, "no_edit_history")
    out = home / "out"
    stale = plant(out / (TMP_PREFIX + "xxxx"), 7)
    mod, (rc, so, se) = run_mode(mode, home, monkeypatch, out)
    assert rc == 0, (so, se)
    assert not stale.exists()
    assert "removed stale owner temp dir" in so and str(stale) in so, so
    assert len(archives(out)) == 1


@pytest.mark.parametrize("mode", ["on", "off"])
def test_c9b_a_fresh_owner_temp_dir_is_kept_a_concurrent_run_is_protected(home, monkeypatch, mode):
    make_store(home, "no_edit_history")
    out = home / "out"
    fresh = plant(out / (TMP_PREFIX + "xxxx"), 1)
    mod, (rc, so, se) = run_mode(mode, home, monkeypatch, out)
    assert rc == 0, (so, se)
    assert fresh.is_dir() and (fresh / "knowledge.snap.db").read_bytes() == b"orphan store copy"
    assert "removed stale" not in so


@pytest.mark.parametrize("mode", ["on", "off"])
def test_c9c_the_match_is_the_exact_prefix_not_a_glob(home, monkeypatch, mode):
    make_store(home, "no_edit_history")
    out = home / "out"
    names = [".engram-bk-owner", "x.engram-bk-owner-old", "engram-bk-owner-old"]
    planted = [plant(out / n, 7) for n in names]
    mod, (rc, so, se) = run_mode(mode, home, monkeypatch, out)
    assert rc == 0, (so, se)
    assert all(p.is_dir() and (p / "knowledge.snap.db").exists() for p in planted)
    assert "removed stale" not in so


@pytest.mark.parametrize("mode", ["on", "off"])
def test_c9d_a_file_or_a_symlink_with_the_prefix_is_never_removed_or_followed(home, monkeypatch, mode):
    make_store(home, "no_edit_history")
    out = home / "out"
    out.mkdir()
    old = datetime.datetime.now().timestamp() - 7 * 3600
    f = out / (TMP_PREFIX + "file")
    f.write_text("x")
    os.utime(f, (old, old))
    outside = plant(home / "elsewhere" / "old-dir", 7)
    link = out / (TMP_PREFIX + "link")
    link.symlink_to(outside)
    mod, (rc, so, se) = run_mode(mode, home, monkeypatch, out)
    assert rc == 0, (so, se)
    assert f.is_file() and link.is_symlink()
    assert outside.is_dir() and (outside / "knowledge.snap.db").exists()
    assert "removed stale" not in so
    # Skipped silently: a symlink that is followed and then refused by rmtree would also leave both in
    # place, but only by way of the sweep's failure warning.
    assert "sweep failed" not in se, se


@pytest.mark.parametrize("mode", ["on", "off"])
def test_c9e_a_failing_sweep_never_fails_the_run(home, monkeypatch, mode):
    make_store(home, "no_edit_history")
    out = home / "out"
    stale = plant(out / (TMP_PREFIX + "xxxx"), 7)
    set_flag(home, mode == "on")
    mod = load_tool(home, monkeypatch)
    real_rmtree = shutil.rmtree

    def rmtree_guard(path, *a, **k):
        # Fail ONLY the sweep's call: its path is the planted stale dir. The run's own temp-dir
        # cleanup (a different, mkdtemp-made path) goes through the real rmtree.
        if os.path.abspath(str(path)) == os.path.abspath(str(stale)):
            raise PermissionError("denied")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(shutil, "rmtree", rmtree_guard)
    argv = ["--db", str(home / "knowledge.db"), "--out-dir", str(out)]
    if mode == "on":
        with SnapshotOwner(home):
            rc, so, se = run(mod, argv)
    else:
        rc, so, se = run(mod, argv)
    assert rc == 0, (so, se)
    assert len(archives(out)) == 1
    warnings = [ln for ln in se.splitlines() if "warning: stale owner temp-dir sweep failed" in ln]
    assert len(warnings) == 1 and "PermissionError" in warnings[0], se
    assert stale.is_dir()  # it was not removed, and the run still succeeded


def test_c9f_a_dry_run_reports_but_does_not_remove(home, monkeypatch):
    make_store(home, "no_edit_history")
    out = home / "out"
    stale = plant(out / (TMP_PREFIX + "xxxx"), 7)
    mod, (rc, so, se) = run_mode("off", home, monkeypatch, out, extra=["--dry-run"])
    assert rc == 0, (so, se)
    assert stale.is_dir() and (stale / "knowledge.snap.db").exists()
    assert f"[dry-run] would remove stale owner temp dir: {stale}" in so, so


def test_c9f_a_dry_run_under_the_flag_also_only_reports(home, monkeypatch):
    make_store(home, "no_edit_history")
    out = home / "out"
    stale = plant(out / (TMP_PREFIX + "xxxx"), 7)
    mod, (rc, so, se) = run_mode("on", home, monkeypatch, out, extra=["--dry-run"])
    assert rc == 0, (so, se)
    assert stale.is_dir()
    assert "[dry-run] would remove stale owner temp dir" in so, so


def test_c9g_the_sweep_runs_before_the_runs_own_mkdtemp(home, monkeypatch):
    make_store(home, "no_edit_history")
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    events = []
    real_sweep, real_mkdtemp = mod._sweep_stale_owner_tmpdirs, tempfile.mkdtemp

    def sweep_spy(*a, **k):
        events.append("sweep")
        return real_sweep(*a, **k)

    def mkdtemp_spy(*a, **k):
        events.append("mkdtemp")
        return real_mkdtemp(*a, **k)

    monkeypatch.setattr(mod, "_sweep_stale_owner_tmpdirs", sweep_spy)
    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp_spy)
    with SnapshotOwner(home):
        rc, so, se = run(mod, ["--db", str(home / "knowledge.db"), "--out-dir", str(out)])
    assert rc == 0, (so, se)
    assert events == ["sweep", "mkdtemp"], events
    assert tmpdirs(out) == []


# ---- L: the REAL owner's `snapshot` op (every cell above runs a fake) -----------------------------------
# Gated by the one shared marker (tests/_owner_platform.py), the same as U14's live-owner cell.

@contextlib.contextmanager
def real_owner(home: Path):
    owner = eo.EngramdOwner(home, request_deadline=5.0)
    owner.start(install_signal_handlers=False)
    try:
        yield owner
    finally:
        owner.stop()


@requires_linux_owner
@pytest.mark.parametrize("kind", ["empty", "no_edit_history", "bulk"])
def test_l1_real_owner_and_direct_archives_are_row_identical_in_every_table(home, monkeypatch, kind):
    live = make_store(home, kind)
    off_out, on_out = home / "out-off", home / "out-on"
    set_flag(home, False)
    rc, so, se = run(load_tool(home, monkeypatch), ["--db", str(live), "--out-dir", str(off_out)])
    assert rc == 0, (so, se)
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with real_owner(home):
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(on_out)])
    assert rc == 0, (so, se)
    assert_identical(off_out, on_out)
    if kind == "bulk":  # not vacuous
        rows = restore(next(on_out.glob("knowledge-*.sql")))
        assert len(rows["nodes"]) == N_BULK and len(rows["edges"]) == N_BULK - 1 and rows["edit_history"]
    if kind == "no_edit_history":
        assert "edit_history" not in restore(next(on_out.glob("knowledge-*.sql")))


@requires_linux_owner
def test_l2_real_owner_success_leaves_no_temp_dir_and_writes_archive_and_watermark(home, monkeypatch):
    live = make_store(home, "no_edit_history")
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    with real_owner(home):
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert rc == 0, (so, se)
    assert tmpdirs(out) == [] and not list(out.glob(TMP_PREFIX + "*"))
    assert len(archives(out)) == 1 and (out / ".watermark.json").is_file()


def test_l3_real_owner_down_exits_nonzero_with_no_archive_no_watermark_no_temp_dir(home, monkeypatch):
    # No owner is started here, so no marker: it runs on every platform.
    live = make_store(home)
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert rc != 0, (so, se)
    assert archives(out) == [] and not (out / ".watermark.json").exists() and tmpdirs(out) == []


@requires_linux_owner
@pytest.mark.parametrize("path", ["success", "skip"])
def test_c2d_real_owner_flag_on_never_opens_the_live_store_from_the_tool(home, monkeypatch, path):
    # Spy method: the REAL owner runs in this process but on its own threads, and it opens the live store
    # there. Spy records only connects made on the thread that calls the tool (threading.get_ident() equals
    # the thread that built the Spy), and the tool's code path (main -> _main_via_owner -> dump_stripped)
    # runs synchronously on that thread, so a tool-side connect cannot be missed; owner-thread connects are
    # excluded by construction. The spy is installed AFTER owner.start() so start-up work is not counted,
    # and the vacuity guard below proves the filter sees this thread's snapshot connects.
    live = make_store(home, "no_edit_history")
    out = home / "out"
    set_flag(home)
    mod = load_tool(home, monkeypatch)
    with real_owner(home):
        if path == "skip":
            assert run(mod, ["--db", str(live), "--out-dir", str(out)])[0] == 0
        spy = Spy().install(monkeypatch)
        rc, so, se = run(mod, ["--db", str(live), "--out-dir", str(out)])
    assert rc == 0, (so, se)
    assert ("up to date" if path == "skip" else "backup written") in so, so
    assert len(archives(out)) == 1 or path == "success"
    assert snapshot_connects(spy), ("vacuous: no snapshot connect seen on the tool's thread", spy.connects)
    assert live_connects(spy, live) == [], spy.connects
