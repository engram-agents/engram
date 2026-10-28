"""bootstrap.py `_db_node_count` opens the store read-only [part of #2652, tools-11].

Contract cells:

  B1  the connection is opened `mode=ro` (URI) with `timeout=0` kept, and a
      write attempted through that same connection fails.
  B2  a read-only count never changes the store. The invariant is: the main
      database file's bytes are unchanged, and the `-wal` is unchanged (absent
      before -> absent or 0 bytes after; byte-identical if it existed). ONE
      exception is named and allowed: SQLite may create a `-shm` next to a
      cleanly closed WAL store on a `mode=ro` open. The `-shm` is a
      shared-memory index, not data. (Measured on SQLite 3.49.1: the open
      works and creates the sidecars.)
  B3  behaviour is otherwise unchanged: a zero-byte DB counts 0, a DB with no
      `nodes` table counts 0, a non-empty DB counts its rows, and a missing path
      raises SystemExit with the "cannot read" message and is not created. The
      locked and unreadable cases (SystemExit with today's messages) are not
      tested here: they are covered end to end by
      tests/test_bootstrap_fresh_db.py (`test_bootstrap_force_on_locked_db_refuses`
      and `test_bootstrap_force_on_unreadable_db_refuses`).
  B4  the sidecars a read-only open creates take the MAIN FILE's permission
      bits, not the process umask, so a reader cannot leave a `-shm` that
      another account cannot open.

Each cell drives `_db_node_count` in a fresh interpreter (importing bootstrap
needs its env vars and imports the server).
"""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"

DRIVER = r"""
import json, os, sqlite3, sys
sys.path.insert(0, os.environ["DRIVER_SRC"])
import bootstrap

db = sys.argv[1]
real_connect = sqlite3.connect
seen = {"connects": [], "probe": None}


class Proxy:
    def __init__(self, conn):
        self._c = conn

    def execute(self, sql, *a):
        if seen["probe"] is None:
            try:
                self._c.execute("CREATE TABLE _write_probe (x)")
                seen["probe"] = "WRITE SUCCEEDED"
            except sqlite3.Error as exc:
                seen["probe"] = str(exc)
        return self._c.execute(sql, *a)

    def close(self):
        return self._c.close()


def spy(*args, **kwargs):
    seen["connects"].append([list(args), kwargs])
    return Proxy(real_connect(*args, **kwargs))


# The function imports sqlite3 itself, so patching the module attribute reaches it.
sqlite3.connect = spy
out = {"count": None, "exit": None}
try:
    out["count"] = bootstrap._db_node_count(__import__("pathlib").Path(db))
except SystemExit as exc:
    out["exit"] = str(exc)
out.update(seen)
print("RESULT:" + json.dumps(out))
"""


def _env(home: Path) -> dict:
    env = os.environ.copy()
    env.update({
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(SRC / "templates"),
        "ENGRAM_HOME": str(home),
        "CLAUDE_HOME": str(home.parent / ".claude"),
        "PYTHON_BIN": sys.executable,
        "TARGET": "claude",
        "DRIVER_SRC": str(SRC),
    })
    env.pop("FORCE", None)
    env.pop("FORCE_RESEED_EMPTY", None)
    return env


def _count(db: Path, home: Path) -> dict:
    r = subprocess.run(
        [sys.executable, "-c", DRIVER, str(db)],
        env=_env(home), capture_output=True, text=True, timeout=60,
    )
    lines = [l for l in r.stdout.splitlines() if l.startswith("RESULT:")]
    assert lines, f"driver failed:\n{r.stdout}\n{r.stderr}"
    return json.loads(lines[-1][len("RESULT:"):])


def _wal_store(path: Path, rows: int) -> None:
    """A cleanly closed WAL-mode store holding a `nodes` table with `rows` rows."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT)")
    for i in range(rows):
        conn.execute("INSERT INTO nodes VALUES (?, 'axiom', 'x')", (f"fixture-node-{i}",))
    conn.commit()
    conn.close()
    for suffix in ("-wal", "-shm"):
        # Precondition of B2/B4: a cleanly closed store has no sidecars.
        assert not Path(str(path) + suffix).exists(), suffix


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scratch():
    d = Path(tempfile.mkdtemp(prefix="engram_dbcount_ro_"))
    home = d / ".engram"
    home.mkdir()
    return d, home


def test_b1_opens_read_only_with_timeout_zero_and_a_write_fails():
    d, home = _scratch()
    try:
        db = home / "knowledge.db"
        _wal_store(db, 0)
        out = _count(db, home)
        assert out["count"] == 0
        assert len(out["connects"]) == 1, out["connects"]
        args, kwargs = out["connects"][0]
        assert "mode=ro" in args[0], args
        assert kwargs.get("uri") is True, kwargs
        assert kwargs.get("timeout") == 0, kwargs
        # Behavioural: a write through the same connection is refused.
        assert out["probe"] and "readonly" in out["probe"].lower(), out["probe"]
        conn = sqlite3.connect(str(db))
        try:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master")]
        finally:
            conn.close()
        assert "_write_probe" not in tables
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _assert_store_unchanged(db: Path, main_sha: str, wal_before):
    wal = Path(str(db) + "-wal")
    assert _sha(db) == main_sha, "the main database file changed"
    if wal_before is None:
        assert (not wal.exists()) or wal.stat().st_size == 0, (
            f"-wal appeared with {wal.stat().st_size} bytes"
        )
    else:
        assert wal.read_bytes() == wal_before, "the -wal content changed"
    # A -shm is the one allowed sidecar (shared-memory index, not data).


def test_b2_count_of_clean_wal_store_changes_no_data_empty_and_nonempty():
    for rows in (0, 3):
        d, home = _scratch()
        try:
            db = home / "knowledge.db"
            _wal_store(db, rows)
            before = _sha(db)
            out = _count(db, home)
            assert out["count"] == rows
            _assert_store_unchanged(db, before, None)
        finally:
            shutil.rmtree(d, ignore_errors=True)


def test_b2b_a_wal_that_exists_is_byte_identical_after_the_count():
    d, home = _scratch()
    try:
        db = home / "knowledge.db"
        _wal_store(db, 0)
        # Hold a writer open with an uncheckpointed commit so a -wal exists.
        w = sqlite3.connect(str(db))
        w.execute("PRAGMA wal_autocheckpoint=0")
        w.execute("INSERT INTO nodes VALUES ('fixture-node-extra', 'axiom', 'x')")
        w.commit()
        try:
            wal = Path(str(db) + "-wal")
            assert wal.stat().st_size > 0
            main_sha, wal_bytes = _sha(db), wal.read_bytes()
            out = _count(db, home)
            assert out["count"] == 1
            _assert_store_unchanged(db, main_sha, wal_bytes)
        finally:
            w.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_b2c_non_empty_refusal_run_changes_no_data():
    """The real `FORCE=1` run on a non-empty WAL store refuses and leaves the
    main file and the -wal untouched."""
    d, home = _scratch()
    try:
        for fname in ("server.py",):
            src = SRC / fname
            if src.exists():
                shutil.copy2(src, home / fname)
        db = home / "knowledge.db"
        _wal_store(db, 2)
        before = _sha(db)
        env = _env(home)
        env["FORCE"] = "1"
        r = subprocess.run(
            [sys.executable, str(SRC / "bootstrap.py")],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert r.returncode != 0
        assert "non-empty" in (r.stdout + r.stderr)
        _assert_store_unchanged(db, before, None)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_b2d_no_write_reaches_main_file_or_wal_with_live_wal_rows_counted_twice():
    # Kept separate from the -wal/-shm allowance above. The fixture really has
    # live -wal content: a writer holds rows that are committed but not
    # checkpointed, so the rows exist only in the -wal. The count is taken
    # twice; the main file and the -wal bytes must both be unchanged.
    d, home = _scratch()
    try:
        db = home / "knowledge.db"
        _wal_store(db, 5)
        w = sqlite3.connect(str(db))
        w.execute("PRAGMA wal_autocheckpoint=0")
        w.execute("INSERT INTO nodes VALUES ('fixture-node-live', 'axiom', 'x')")
        w.commit()
        try:
            wal = Path(str(db) + "-wal")
            assert wal.stat().st_size > 0, "fixture has no live -wal content"
            main_sha, wal_bytes = _sha(db), wal.read_bytes()
            for _ in range(2):
                assert _count(db, home)["count"] == 6  # 5 + the wal-only row
            _assert_store_unchanged(db, main_sha, wal_bytes)
        finally:
            w.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_b3_behaviour_otherwise_unchanged():
    d, home = _scratch()
    try:
        # No schema at all (a zero-byte file): counts 0.
        empty = home / "empty.db"
        sqlite3.connect(str(empty)).close()
        assert _count(empty, home)["count"] == 0
        # Another table, but no `nodes`: counts 0.
        other = home / "other.db"
        c = sqlite3.connect(str(other))
        c.execute("CREATE TABLE t (x)")
        c.commit()
        c.close()
        assert _count(other, home)["count"] == 0
        # Non-empty rollback-journal store: counts its rows.
        plain = home / "plain.db"
        c = sqlite3.connect(str(plain))
        c.execute("CREATE TABLE nodes (id TEXT)")
        c.executemany("INSERT INTO nodes VALUES (?)", [("a",), ("b",)])
        c.commit()
        c.close()
        assert _count(plain, home)["count"] == 2
        # An unreadable path still raises SystemExit with today's message,
        # and a read-only open does not create the missing file.
        missing = home / "missing.db"
        out = _count(missing, home)
        assert out["count"] is None
        assert "[bootstrap] ERROR: cannot read" in out["exit"], out
        assert "Refusing to proceed" in out["exit"]
        assert not missing.exists()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_b4_sidecars_take_the_main_files_mode_not_the_umask():
    d, home = _scratch()
    old = os.umask(0o077)
    try:
        # Control: a plain file made under this umask is 0600, so the umask is live.
        control = home / "control"
        control.write_text("x")
        assert stat.S_IMODE(control.stat().st_mode) == 0o600
        db = home / "knowledge.db"
        _wal_store(db, 1)
        os.chmod(db, 0o644)
        out = _count(db, home)
        assert out["count"] == 1
        want = stat.S_IMODE(db.stat().st_mode)
        assert want == 0o644
        for suffix in ("-wal", "-shm"):
            side = Path(str(db) + suffix)
            assert side.exists(), f"{suffix} was not created; the cell proves nothing"
            assert stat.S_IMODE(side.stat().st_mode) == want, (
                suffix, oct(stat.S_IMODE(side.stat().st_mode))
            )
    finally:
        os.umask(old)
        shutil.rmtree(d, ignore_errors=True)
