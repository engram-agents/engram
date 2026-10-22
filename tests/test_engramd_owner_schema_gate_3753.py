"""Contract cells for #3753: the owner's schema-version gate, the ONE home of the latest
schema version, and the `refused_at_load` reason (written before the implementation).

The ruling (design seat, on the issue):
  1. `EngramdOwner.start()` refuses while `PRAGMA user_version` < the latest schema version,
     read on a `mode=ro` connection BEFORE anything creates or opens knowledge.db, so a missing
     file is refused with no empty-file side effect and no run/. (A WAL-mode store may gain
     -wal/-shm sidecars (no row data) from the probe; the WAL cell below measures exactly that.) Own
     exit code from `main()`. `>=` passes.
  2. The latest version has ONE home, a module-level constant in engram_core that the migration
     code and the owner both read. The owner holds no literal copy.
  3. `Registry.check()` answers `refused_at_load` (detail = the original reason) for a template
     the loader dropped, and still `unregistered_template` for one never listed.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402
import engramd_read_registry as rr  # noqa: E402

LATEST = engram_core.LATEST_SCHEMA_VERSION

# This file tests the gate for real: opt out of the conftest default that stamps a behind store.
pytestmark = pytest.mark.owner_schema_gate_real


@pytest.fixture
def home():
    d = tempfile.mkdtemp(prefix="g53-")
    try:
        yield Path(d)
    finally:
        os.chmod(d, 0o700)
        shutil.rmtree(d, ignore_errors=True)


def make_store(home: Path, user_version: int) -> Path:
    """The minimum the owner needs to serve: a `nodes` table, and an explicit user_version."""
    db = home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT)")
        conn.execute(f"PRAGMA user_version = {int(user_version)}")
        conn.commit()
    finally:
        conn.close()
    return db


def _no_serving(monkeypatch):
    """main() must refuse before serving; if it ever starts serving, fail instead of hanging."""
    def boom(self):
        raise AssertionError("main() reached wait_forever(): the owner started instead of refusing")
    monkeypatch.setattr(eo.EngramdOwner, "wait_forever", boom)
    monkeypatch.setattr(eo.EngramdOwner, "stop", lambda self: None)


def _listing(home: Path):
    return sorted(str(p.relative_to(home)) for p in home.rglob("*")) if home.exists() else None


def _try_start(home: Path):
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    owner.stop()


# --- 1: a fresh home -------------------------------------------------------------------

@requires_linux_owner
def test_1_fresh_home_is_refused_by_name_and_creates_nothing(home):
    before = _listing(home)
    with pytest.raises(eo.OwnerStoreBehindSchema) as ei:
        _try_start(home)
    msg = str(ei.value)
    assert "no knowledge.db" in msg and str(LATEST) in msg, msg
    assert _listing(home) == before == []  # rollback/absent store: no knowledge.db, no run/, nothing


@requires_linux_owner
def test_1_a_home_directory_that_does_not_exist_is_not_created(home):
    missing = home / "nope"  # short path: AF_UNIX limit
    with pytest.raises(eo.OwnerStoreBehindSchema):
        _try_start(missing)
    assert not missing.exists()


@requires_linux_owner
def test_1_main_maps_it_to_its_own_exit_code_with_one_stderr_line(home, monkeypatch, capsys):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    _no_serving(monkeypatch)
    assert eo.main() == eo.EXIT_STORE_BEHIND_SCHEMA
    assert eo.EXIT_STORE_BEHIND_SCHEMA not in (0, 1, eo.EXIT_DISPLACED)
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "no knowledge.db" in err, err
    assert _listing(home) == []


# --- 2: the >= rule ----------------------------------------------------------------------

@requires_linux_owner
def test_2_behind_by_one_refuses_and_names_both_versions(home):
    make_store(home, LATEST - 1)
    with pytest.raises(eo.OwnerStoreBehindSchema) as ei:
        _try_start(home)
    msg = str(ei.value)
    assert str(LATEST - 1) in msg and str(LATEST) in msg, msg


@requires_linux_owner
def test_2_behind_by_one_via_main_returns_the_exit_code(home, monkeypatch, capsys):
    make_store(home, LATEST - 1)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    _no_serving(monkeypatch)
    assert eo.main() == eo.EXIT_STORE_BEHIND_SCHEMA
    assert capsys.readouterr().err.strip()


@requires_linux_owner
@pytest.mark.parametrize("delta", [0, 1])
def test_2_at_or_above_latest_starts(home, delta):
    make_store(home, LATEST + delta)
    _try_start(home)  # an older owner on a newer store is the upgrade-restart case


@requires_linux_owner
def test_2_user_version_zero_with_a_nodes_table_is_refused(home):
    make_store(home, 0)
    with pytest.raises(eo.OwnerStoreBehindSchema):
        _try_start(home)


@requires_linux_owner
def test_2_a_file_that_is_not_sqlite_is_refused_not_crashed(home):
    (home / "knowledge.db").write_bytes(b"this is not a sqlite database" * 50)
    with pytest.raises(eo.OwnerStoreBehindSchema):
        _try_start(home)


# --- 3: ONE constant ---------------------------------------------------------------------

@requires_linux_owner
def test_3_the_owner_reads_the_constant_at_call_time_not_a_copy(home, monkeypatch):
    make_store(home, LATEST)
    _try_start(home)  # starts at the real latest
    monkeypatch.setattr(engram_core, "LATEST_SCHEMA_VERSION", LATEST + 1)
    with pytest.raises(eo.OwnerStoreBehindSchema) as ei:
        _try_start(home)
    assert str(LATEST + 1) in str(ei.value)


def test_3_the_migration_reads_the_constant_at_call_time(tmp_path, monkeypatch):
    seen = []

    def spy(conn, current_user_version, latest):
        seen.append(latest)
        return True

    monkeypatch.setattr(engram_core, "_take_pre_migration_backup_if_pending", spy)
    monkeypatch.setattr(engram_core, "LATEST_SCHEMA_VERSION", LATEST + 5)
    conn = sqlite3.connect(str(tmp_path / "k.db"))
    conn.row_factory = sqlite3.Row
    try:
        engram_core._run_db_one_time_setup(conn)
    finally:
        conn.close()
    assert seen == [LATEST + 5], seen


def test_3_no_literal_copy_in_the_sources():
    core_src = (REPO_ROOT / "src" / "engram" / "engram_core.py").read_text()
    core_tree = ast.parse(core_src)
    fn = next(n for n in ast.walk(core_tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_db_one_time_setup")
    fn_src = ast.get_source_segment(core_src, fn)
    assert "LATEST_SCHEMA_VERSION" in fn_src
    assert not re.search(r"_PRE_MIGRATION_LATEST_VERSION\s*=\s*\d", fn_src)
    # the constant is module level and an int
    assigns = [n for n in core_tree.body if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "LATEST_SCHEMA_VERSION" for t in n.targets)]
    assert len(assigns) == 1 and isinstance(assigns[0].value, ast.Constant)
    # the owner holds no assignment of a number to anything version-latest-shaped, and the gate
    # names the constant
    owner_src = (REPO_ROOT / "src" / "engram" / "engramd_owner.py").read_text()
    assert "LATEST_SCHEMA_VERSION" in owner_src
    assert not re.search(r"(?i)(latest|required|schema)\w*version\w*\s*=\s*\d", owner_src)
    assert not re.search(r"user_version\s*(<|<=|>|>=|==)\s*\d", owner_src)


# --- 4: refused_at_load vs unregistered_template -------------------------------------------

DROPPED_SQL = "SELECT id FROM no_such_table_3753 WHERE id = ?"
NEVER_LISTED_SQL = "SELECT id FROM nodes WHERE id = ? AND 3753 = 3753"


def _registry_with_one_dropped():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    reg = rr.build_registry([DROPPED_SQL, "SELECT id FROM nodes WHERE id = ?"], conn)
    return reg


def test_4_a_template_dropped_at_load_answers_refused_at_load_with_the_original_reason():
    reg = _registry_with_one_dropped()
    original = reg.refused[DROPPED_SQL]
    out = reg.check(DROPPED_SQL, ["x"])
    assert isinstance(out, rr.Rejected)
    assert out.reason == "refused_at_load"
    assert out.detail == original.reason and original.reason  # exactly, not a paraphrase
    assert out.remedy and out.remedy != rr._REMEDY_REGISTER
    assert "restart" in out.remedy


def test_4_a_never_listed_statement_stays_unregistered_template():
    reg = _registry_with_one_dropped()
    out = reg.check(NEVER_LISTED_SQL, ["x"])
    assert isinstance(out, rr.Rejected) and out.reason == "unregistered_template"


def test_4_the_original_reason_is_passed_through_for_every_refusal_kind():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE nodes (id TEXT)")
    bad = "SELECT id FROM nodes WHERE id = ?1"  # numbered placeholder
    reg = rr.build_registry([bad], conn)
    why = reg.refused[bad]
    out = reg.check(bad, ["x"])
    assert out.reason == "refused_at_load" and out.detail == why.reason


@requires_linux_owner
def test_4_over_the_wire_against_a_real_owner(home):
    """A store at the latest version that LACKS the tables the committed templates read: every
    listed template is dropped at load, so a listed statement answers refused_at_load."""
    make_store(home, LATEST)
    import json
    listed = json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())
    probe = sqlite3.connect((home / "knowledge.db").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        offline = rr.build_registry([e["sql"] for e in listed["templates"]], probe)
    finally:
        probe.close()
    assert offline.refused, "the minimal store must make the loader drop at least one listed template"
    sql = next(iter(offline.refused))
    original = offline.refused[sql]
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        dropped = ec.read("read", {"sql": sql, "args": [""] * sql.count("?")},
                          timeout=5.0, socket_path=owner.socket_path)
        never = ec.read("read", {"sql": NEVER_LISTED_SQL, "args": ["x"]},
                        timeout=5.0, socket_path=owner.socket_path)
    finally:
        owner.stop()
    assert isinstance(dropped, ec.Rejected) and dropped.reason == "refused_at_load", dropped
    assert dropped.detail == original.reason and dropped.remedy
    assert isinstance(never, ec.Rejected) and never.reason == "unregistered_template", never


# --- 5: mode=ro ----------------------------------------------------------------------------

@requires_linux_owner
def test_5_the_gate_connects_read_only(home, monkeypatch):
    make_store(home, LATEST - 1)
    calls = []
    real = sqlite3.connect

    def spy(*a, **k):
        calls.append((a, k))
        return real(*a, **k)

    monkeypatch.setattr(eo.sqlite3, "connect", spy)
    with pytest.raises(eo.OwnerStoreBehindSchema):
        _try_start(home)
    assert len(calls) == 1, calls  # refused before any other connection to the store
    (target,), kw = calls[0]
    assert "mode=ro" in target and kw.get("uri") is True, calls


@requires_linux_owner
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_5_a_read_only_home_is_refused_by_name_not_by_a_write_error(home):
    os.chmod(home, 0o500)
    with pytest.raises(eo.OwnerStoreBehindSchema):
        _try_start(home)
    assert _listing(home) == []


# --- reviewer round: WAL sidecars, ImportError, locked vs corrupt ---------------------------

def make_wal_store(home: Path, user_version: int) -> Path:
    """A WAL-mode store (as the real one is), checkpointed and closed so no sidecars remain."""
    db = home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT)")
        conn.execute(f"PRAGMA user_version = {int(user_version)}")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        side = Path(str(db) + suffix)
        if side.exists():
            side.unlink()
    return db


@requires_linux_owner
def test_wal_store_below_latest_is_refused_and_the_probe_leaves_only_sidecars(home):
    """What a refusal on a WAL-mode store really leaves (measured, #3753 review): the database
    file is byte-identical, no run/ exists, and the only new directory entries are the
    `knowledge.db-wal` / `knowledge.db-shm` sidecars that SQLite creates for ANY connection to a
    WAL database, `mode=ro` included. Measured on Darwin arm64 / SQLite 3.53.4: -wal 0 bytes,
    -shm 32768 bytes (the wal-index, a shared-memory map, no row data). Asserted: no new
    entries besides those two, -wal size 0 or absent, database bytes unchanged, no run/. Not
    claimed: that no sidecar appears."""
    db = make_wal_store(home, LATEST - 1)
    before = _listing(home)
    assert before == ["knowledge.db"]
    image = db.read_bytes()
    with pytest.raises(eo.OwnerStoreBehindSchema):
        _try_start(home)
    after = _listing(home)
    # #3882: the owner takes the store-maintenance lock before the probe, so the lock directory (and the
    # holder file in it) is the one further new entry a refusal against an EXISTING store leaves.
    assert set(after) - set(before) <= {
        "knowledge.db-wal", "knowledge.db-shm",
        "knowledge.db.maint.lockdir", "knowledge.db.maint.lockdir/holder",
    }, after
    assert set(before) <= set(after)
    assert not (home / "run").exists()
    assert db.read_bytes() == image  # no NEW knowledge.db and no change to the old one
    wal = home / "knowledge.db-wal"
    assert (not wal.exists()) or wal.stat().st_size == 0


@requires_linux_owner
def test_import_error_in_the_gate_refuses_by_name_and_creates_nothing(home, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "engram_core", None)  # `import engram_core` -> ImportError
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    _no_serving(monkeypatch)
    with pytest.raises(eo.OwnerStoreBehindSchema) as ei:
        _try_start(home)
    assert "engram_core" in str(ei.value) and "import" in str(ei.value).lower()
    assert eo.main() == eo.EXIT_STORE_BEHIND_SCHEMA
    err = capsys.readouterr().err
    assert "engram_core" in err and "Traceback" not in err
    assert _listing(home) == []


@requires_linux_owner
def test_a_locked_store_is_refused_as_locked_not_as_behind(home, monkeypatch):
    make_store(home, LATEST)  # NOT behind: the refusal must not read as if it were

    def locked(_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(eo, "_connect_store_ro", locked)
    with pytest.raises(eo.OwnerStoreBehindSchema) as ei:
        _try_start(home)
    msg = str(ei.value).lower()
    assert "locked" in msg and "retry" in msg and "migration" in msg, msg
    assert "behind" not in msg and "not a valid" not in msg, msg


@requires_linux_owner
def test_a_corrupt_store_is_refused_as_corrupt_not_as_locked(home):
    (home / "knowledge.db").write_bytes(b"this is not a sqlite database" * 50)
    with pytest.raises(eo.OwnerStoreBehindSchema) as ei:
        _try_start(home)
    msg = str(ei.value).lower()
    assert "not a valid" in msg and "locked" not in msg and "retry" not in msg, msg


# --- security read: a non-regular file at the store path (FIFO would HANG a read-open) ------

_BOUND = 10  # seconds; a hang must FAIL the cell, never the suite

_CHILD = r"""
import sys
sys.path.insert(0, {src!r})
import engramd_owner as eo
mode, home = sys.argv[1], sys.argv[2]
if mode == "start":
    o = eo.EngramdOwner(home, request_deadline=0.6)
    try:
        o.start(install_signal_handlers=False)
    except Exception as exc:
        print("RAISED:" + type(exc).__name__ + ":" + str(exc))
    else:
        print("STARTED")
        o.stop()
elif mode == "connect":
    try:
        eo._connect_store_ro(home + "/knowledge.db").close()
    except Exception as exc:
        print("RAISED:" + type(exc).__name__ + ":" + str(exc))
    else:
        print("OPENED")
elif mode == "main":
    sys.exit(eo.main())
"""


def _child(mode: str, home: Path):
    """Run the probe in a child with a hard bound. Returns (returncode, stdout, stderr); a hang
    fails the calling cell with a message instead of hanging the suite."""
    import subprocess
    env = dict(os.environ, ENGRAM_HOME=str(home))
    try:
        r = subprocess.run([sys.executable, "-c", _CHILD.format(src=_SRC_ENGRAM), mode, str(home)],
                           capture_output=True, text=True, timeout=_BOUND, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{mode} on a non-regular store path did not return within {_BOUND}s (it hangs)")
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _plant_fifo(home: Path) -> Path:
    p = home / "knowledge.db"
    os.mkfifo(str(p))
    return p


@requires_linux_owner
def test_fifo_at_the_store_path_is_refused_promptly_by_name(home):
    fifo = _plant_fifo(home)
    before = _listing(home)
    _rc, out, _err = _child("start", home)
    assert out.startswith("RAISED:OwnerStorePathNotRegular:"), out
    assert "not a regular file" in out and "fifo" in out and str(fifo) in out, out
    assert _listing(home) == before == ["knowledge.db"]  # no run/, no sidecars


@requires_linux_owner
def test_fifo_at_the_store_path_via_main_exits_with_its_code_and_one_line(home):
    _plant_fifo(home)
    rc, _out, err = _child("main", home)
    assert rc == eo.EXIT_PATH_NOT_REGULAR == 4, (rc, err)
    assert len(err.splitlines()) == 1 and "fifo" in err, err
    assert _listing(home) == ["knowledge.db"]


@requires_linux_owner
def test_directory_at_the_store_path_is_refused_as_not_a_regular_file(home):
    (home / "knowledge.db").mkdir()
    _rc, out, _err = _child("start", home)
    assert out.startswith("RAISED:OwnerStorePathNotRegular:"), out
    assert "not a regular file" in out and "directory" in out, out


@requires_linux_owner
def test_unix_socket_at_the_store_path_is_refused_as_not_a_regular_file(home):
    import socket as _socket
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s.bind(str(home / "knowledge.db"))
        _rc, out, _err = _child("start", home)
    finally:
        s.close()
    assert out.startswith("RAISED:OwnerStorePathNotRegular:"), out
    assert "not a regular file" in out and "socket" in out, out


@requires_linux_owner
def test_symlink_to_a_regular_current_store_still_starts(home):
    target = home / "real.db"
    make_store(home, LATEST)
    (home / "knowledge.db").rename(target)
    (home / "knowledge.db").symlink_to(target)  # stat FOLLOWS it; lstat would call it a symlink
    _try_start(home)


@requires_linux_owner
def test_symlink_to_a_fifo_is_refused_promptly(home):
    fifo = home / "the-fifo"
    os.mkfifo(str(fifo))
    (home / "knowledge.db").symlink_to(fifo)
    _rc, out, _err = _child("start", home)
    assert out.startswith("RAISED:OwnerStorePathNotRegular:"), out
    assert "not a regular file" in out and "fifo" in out, out


@requires_linux_owner
def test_connect_store_ro_itself_refuses_a_fifo_promptly(home):
    _plant_fifo(home)
    _rc, out, _err = _child("connect", home)
    assert out.startswith("RAISED:OwnerStorePathNotRegular:"), out
    assert "not a regular file" in out and "fifo" in out, out



# --- the REAL PROCESS exit status (the supervisor reads this, not the exception type) -------

_OWNER_SCRIPT = str(REPO_ROOT / "src" / "engram" / "engramd_owner.py")


def _run_owner_script(home: Path) -> int:
    """Run engramd_owner.py as a script against `home`; return its exit status. A 10 s bound,
    so a hang fails the cell instead of the suite."""
    import subprocess
    env = dict(os.environ, ENGRAM_HOME=str(home))
    try:
        r = subprocess.run([sys.executable, _OWNER_SCRIPT], capture_output=True, text=True,
                           timeout=_BOUND, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pytest.fail(f"the owner script did not exit within {_BOUND}s (it hangs or is serving)")
    return r.returncode


@requires_linux_owner
def test_process_exit_4_for_a_fifo_at_the_store_path(home):
    _plant_fifo(home)
    assert _run_owner_script(home) == 4


@requires_linux_owner
def test_process_exit_4_for_a_directory_at_the_store_path(home):
    (home / "knowledge.db").mkdir()
    assert _run_owner_script(home) == 4


@requires_linux_owner
def test_process_exit_4_for_a_unix_socket_at_the_store_path(home):
    import socket as _socket
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s.bind(str(home / "knowledge.db"))
        rc = _run_owner_script(home)
    finally:
        s.close()
    assert rc == 4


@requires_linux_owner
def test_process_exit_4_for_a_symlink_to_a_fifo_at_the_store_path(home):
    fifo = home / "the-fifo"
    os.mkfifo(str(fifo))
    (home / "knowledge.db").symlink_to(fifo)
    assert _run_owner_script(home) == 4


@requires_linux_owner
def test_process_exit_5_for_an_absent_store(home):
    assert _run_owner_script(home) == 5


@requires_linux_owner
def test_process_exit_5_for_a_store_one_version_behind(home):
    make_store(home, LATEST - 1)
    assert _run_owner_script(home) == 5


# ---------------------------------------------------------------------------
# The owner's exit codes must not collide with the interpreter's own (design seat)
# ---------------------------------------------------------------------------


def test_exit_codes_are_pairwise_distinct_and_avoid_the_interpreters_1_and_2():
    """CPython exits 2 when it cannot open the script it was asked to run (the
    deleted-ExecStart case) and 1 on an uncaught exception, so a refusal code of the
    owner's must not be 1 or 2, or a unit's exit table reads an interpreter failure as an
    owner verdict. The ONE named exception is OwnerAlreadyRunning's historic 1 (main()
    returns the literal 1; it has no constant). Enumerated from the module, so an
    EXIT_* constant added later is covered too."""
    named = {n: v for n, v in vars(eo).items() if n.startswith("EXIT_") and isinstance(v, int)}
    assert "EXIT_STORE_BEHIND_SCHEMA" in named, named
    assert len(set(named.values())) == len(named), f"two EXIT_* constants share a value: {named}"
    assert 0 not in named.values(), named  # 0 is a clean stop, never a refusal
    owner_already_running_historic = 1  # the named exception: main() returns the literal 1
    assert owner_already_running_historic not in named.values(), named
    colliding = {n: v for n, v in named.items() if v in (1, 2)}
    assert not colliding, f"an owner exit code shares the interpreter's own 1/2: {colliding}"
    # Pinned once, on purpose: 5 is the next code free of the interpreter's and of 0/1/3/4.
    assert eo.EXIT_STORE_BEHIND_SCHEMA == 5
