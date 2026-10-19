"""engram-alpha#3954 (part of #2652) -- tools/inspect_raw.py, flag OFF, a store that exists but cannot be read.

With `engramd.inspect_raw_reads_via_owner` OFF the tool opens the store `mode=ro` and runs the one-row read.
When `$ENGRAM_HOME/knowledge.db` exists but is not a readable store, sqlite raises.  That used to escape as a
raw traceback with exit 1, and exit 1 is this tool's "node or store not found" code.

Contract (each cell red against fe97d376 before the fix):

  U1  flag OFF, each of three bad stores (a text file; a directory; a valid SQLite file with no `nodes`
      table), each output mode (list, --json, --field): exit 3, empty stdout, exactly one stderr line, no
      traceback, the line names the store path and the sqlite error text, and names neither the owner nor
      the flag
  U2  the exact sqlite exception class each bad store raises is a sqlite3.Error (measured, pinned)
  C1  control: a missing store is still exit 1 with "DB not found at"
  C2  control: a well-formed row, flag OFF, prints the same bytes as before
  W1  only sqlite3.Error is relabelled: a RuntimeError out of the read propagates as itself (not as the
      typed unreadable-store error)
  W3  the same for an OSError (PermissionError) raised by the read: it propagates as exactly itself, never
      the typed unreadable-store error, and the reader is closed
  W2  the reader is closed when the read raises, and a sqlite error is typed with the store path
  O1  only sqlite3.Error is relabelled at the OPEN site too: a RuntimeError raised by open_reader itself
      propagates as exactly RuntimeError (never the typed unreadable-store error, never exit 3)
  O2  the same for a bare OSError (PermissionError) raised by open_reader: it propagates as itself
  O3  measured: a chmod 000 store makes open_reader raise sqlite3.OperationalError (not a bare OSError),
      and the CLI reports it as exit 3
  R1  the remedy covers a transient lock as well as a damaged store, and a locked store is exit 3 with
      that one line
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "src" / "engram" / "tools" / "inspect_raw.py"
FLAG_KEY = "inspect_raw_reads_via_owner"

REMEDY = "check the store, or retry if another process holds a lock on it"
STORES = ["text_file", "directory", "no_nodes_table"]
#: store -> the sqlite3.Error subclass measured on this seat (python.org 3.12, macOS) and the message
EXPECTED = {
    "text_file": (sqlite3.DatabaseError, "file is not a database"),
    "directory": (sqlite3.OperationalError, "disk I/O error"),
    "no_nodes_table": (sqlite3.OperationalError, "no such table: nodes"),
}
MODES = {
    "list": ["ob_x"],
    "json": ["ob_x", "--json"],
    "field": ["ob_x", "--field", "git_sha"],
}


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="ir3954-", dir="/tmp"))
    (d / "config.json").write_text(json.dumps({"engramd": {FLAG_KEY: False}}), encoding="utf-8")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def build_store(home: Path, kind: str) -> Path:
    db = home / "knowledge.db"
    if kind == "text_file":
        db.write_text("this is not a sqlite database\n", encoding="utf-8")
    elif kind == "directory":
        db.mkdir()
    elif kind == "no_nodes_table":
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE other (a TEXT)")
        conn.commit()
        conn.close()
    else:  # pragma: no cover
        raise AssertionError(kind)
    return db


def cli(home: Path, *args: str):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    return subprocess.run([sys.executable, str(TOOL_PATH), *args], capture_output=True, text=True,
                          env=env, timeout=120)


def stderr_lines(text: str) -> list:
    return [ln for ln in text.splitlines() if ln.strip()]


def load_tool(home: Path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location("inspect_raw_3954", str(TOOL_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("mode", list(MODES))
@pytest.mark.parametrize("kind", STORES)
def test_u1_an_unreadable_store_is_exit_3_one_honest_stderr_line(home, kind, mode):
    db = build_store(home, kind)
    r = cli(home, *MODES[mode])
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == "", r.stdout
    assert "Traceback" not in r.stderr, r.stderr
    lines = stderr_lines(r.stderr)
    assert len(lines) == 1, r.stderr
    line = lines[0]
    assert str(db) in line, line
    assert EXPECTED[kind][1] in line, line
    # The "ERROR: InspectRawOwnerReadError:" prefix is the CLI's existing typed-error prefix on every exit-3 line
    # (the malformed-row line carries it too); the rule is about the message after it.
    prefix = "ERROR: InspectRawOwnerReadError: "
    assert line.startswith(prefix), line
    message = line[len(prefix):]
    for text in ("owner", "engramd", FLAG_KEY, "flag"):
        assert text not in message.lower(), (text, message)
    assert REMEDY in message, message


@pytest.mark.parametrize("kind", STORES)
def test_u2_each_bad_store_raises_a_sqlite3_error_subclass(home, kind):
    """Measured, pinned: the catch is sqlite3.Error only, so each store must raise exactly that family."""
    db = build_store(home, kind)
    with pytest.raises(sqlite3.Error) as ei:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            conn.execute("SELECT id, type, embedding, confidence_history, metadata FROM nodes WHERE id = ?",
                         ("ob_x",)).fetchall()
        finally:
            conn.close()
    assert type(ei.value) is EXPECTED[kind][0], (kind, type(ei.value))
    assert EXPECTED[kind][1] in str(ei.value)


def test_c1_a_missing_store_is_still_exit_1_db_not_found(home):
    r = cli(home, "ob_x")
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == ""
    assert f"ERROR: DB not found at {home / 'knowledge.db'}" in r.stderr


def _good_store(home: Path) -> None:
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, embedding TEXT, "
                 "confidence_history TEXT, metadata TEXT)")
    conn.execute("INSERT INTO nodes VALUES ('ob_x', 'observation_factual', '[0.5, 0.25]', "
                 "'[{\"value\": 0.8}]', '{\"git_sha\": \"abc123\", \"content_hash\": \"h\"}')")
    conn.commit()
    conn.close()


def test_c2_a_well_formed_row_flag_off_prints_the_same_bytes(home):
    _good_store(home)
    r = cli(home, "ob_x", "--field", "git_sha")
    assert (r.returncode, r.stderr, r.stdout) == (0, "", "git_sha for ob_x: 'abc123'\n")
    r = cli(home, "ob_x", "--field", "embedding", "--json")
    assert (r.returncode, r.stderr, r.stdout) == (0, "", json.dumps([0.5, 0.25], indent=2) + "\n")
    r = cli(home, "ob_x")
    assert r.returncode == 0 and r.stderr == ""
    assert r.stdout.startswith(f"Node: ob_x  (type=observation_factual)\nDB: {home / 'knowledge.db'}\n")
    assert "git_sha" in r.stdout and "'abc123'" in r.stdout


class _Boom:
    def __init__(self, exc):
        self.exc, self.closed = exc, False

    def execute(self, *a, **k):
        raise self.exc

    def close(self):
        self.closed = True


def test_w1_only_sqlite_errors_are_relabelled_a_runtimeerror_propagates_as_itself(home, monkeypatch):
    _good_store(home)
    mod = load_tool(home, monkeypatch)
    boom = _Boom(RuntimeError("not a sqlite problem"))
    monkeypatch.setattr(mod.owner_read, "open_reader", lambda spec, db_path, use_owner: boom)
    with pytest.raises(RuntimeError) as ei:
        mod.read_row(home / "knowledge.db", "ob_x", use_owner=False)
    assert type(ei.value) is RuntimeError, type(ei.value)   # InspectRawOwnerReadError is a RuntimeError subclass
    assert "not a sqlite problem" in str(ei.value)
    assert boom.closed


def test_w2_a_sqlite_error_from_the_read_is_typed_and_the_reader_is_closed(home, monkeypatch):
    _good_store(home)
    mod = load_tool(home, monkeypatch)
    boom = _Boom(sqlite3.OperationalError("disk I/O error"))
    monkeypatch.setattr(mod.owner_read, "open_reader", lambda spec, db_path, use_owner: boom)
    with pytest.raises(mod.InspectRawOwnerReadError) as ei:
        mod.read_row(home / "knowledge.db", "ob_x", use_owner=False)
    assert str(home / "knowledge.db") in str(ei.value) and "disk I/O error" in str(ei.value)
    assert boom.closed


def test_w3_an_oserror_from_the_read_propagates_as_itself_and_the_reader_is_closed(home, monkeypatch):
    _good_store(home)
    mod = load_tool(home, monkeypatch)
    boom = _Boom(PermissionError(13, "Permission denied at execute"))
    monkeypatch.setattr(mod.owner_read, "open_reader", lambda spec, db_path, use_owner: boom)
    with pytest.raises(PermissionError) as ei:
        mod.read_row(home / "knowledge.db", "ob_x", use_owner=False)
    assert type(ei.value) is PermissionError, type(ei.value)   # never InspectRawOwnerReadError / exit 3
    assert "Permission denied at execute" in str(ei.value)
    assert boom.closed


def _open_raises(mod, monkeypatch, exc):
    def _raise(spec, db_path, use_owner):
        raise exc
    monkeypatch.setattr(mod.owner_read, "open_reader", _raise)


def test_o1_a_runtimeerror_from_open_reader_propagates_as_itself(home, monkeypatch):
    _good_store(home)
    mod = load_tool(home, monkeypatch)
    _open_raises(mod, monkeypatch, RuntimeError("not a sqlite problem at open"))
    with pytest.raises(RuntimeError) as ei:
        mod.read_row(home / "knowledge.db", "ob_x", use_owner=False)
    assert type(ei.value) is RuntimeError, type(ei.value)   # InspectRawOwnerReadError is a RuntimeError subclass
    assert "not a sqlite problem at open" in str(ei.value)


def test_o2_an_oserror_from_open_reader_propagates_as_itself(home, monkeypatch):
    _good_store(home)
    mod = load_tool(home, monkeypatch)
    _open_raises(mod, monkeypatch, PermissionError(13, "Permission denied"))
    with pytest.raises(PermissionError):
        mod.read_row(home / "knowledge.db", "ob_x", use_owner=False)
    # and through the CLI's own read wrapper it is not turned into exit 3 either
    with pytest.raises(PermissionError):
        mod._get_row("ob_x")


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses chmod 000")
@pytest.mark.parametrize("kind", ["text_file", "no_nodes_table"])
def test_o3_a_chmod_000_store_raises_sqlite_operationalerror_at_open_and_is_exit_3(home, monkeypatch, kind):
    db = build_store(home, kind)
    os.chmod(db, 0)
    try:
        mod = load_tool(home, monkeypatch)
        with pytest.raises(sqlite3.OperationalError) as ei:
            mod.owner_read.open_reader(mod._owner_spec(), db, False)
        assert type(ei.value) is sqlite3.OperationalError and not isinstance(ei.value, OSError)
        assert "unable to open database file" in str(ei.value)
        r = cli(home, "ob_x")
        assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
        assert r.stdout == "" and len(stderr_lines(r.stderr)) == 1
        assert str(db) in r.stderr and "unable to open database file" in r.stderr and REMEDY in r.stderr
    finally:
        os.chmod(db, 0o600)


def test_r1_a_locked_store_is_exit_3_with_the_retry_remedy(home):
    _good_store(home)
    db = home / "knowledge.db"
    holder = sqlite3.connect(str(db), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        r = cli(home, "ob_x")
    finally:
        holder.close()
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == ""
    lines = stderr_lines(r.stderr)
    assert len(lines) == 1, r.stderr
    line = lines[0]
    assert str(db) in line and "database is locked" in line and REMEDY in line, line
    message = line[len("ERROR: InspectRawOwnerReadError: "):]
    for text in ("owner", "engramd", FLAG_KEY, "flag"):
        assert text not in message.lower(), (text, message)
