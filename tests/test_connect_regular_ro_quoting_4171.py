"""#4171: connect_regular_ro must percent-quote the path it puts in the sqlite URI.

CONTRACT (written before the fix):

  Q1  For a real db at a path whose directory name contains '#', '?' or '%',
      connect_regular_ro(path) opens THAT file: PRAGMA database_list reports
      os.path.realpath(path) and the db's own table is visible. (Unquoted, '#'
      starts the URI fragment: the path is truncated, mode=ro is dropped and the
      connect succeeds onto a DIFFERENT database -- a fail-open.)
  Q2  For an ABSENT path containing '#', connect_regular_ro raises
      sqlite3.OperationalError and creates NO file anywhere in the temp tree.
  Q3  The existing contract is unchanged: a FIFO is refused with
      NotRegularDatabaseError, extra kwargs (timeout) pass through, and
      uri_params such as "mode=ro&immutable=1" still apply.
  Q4  A relative db_path still works (the URI form needs an absolute path).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

HOOK_DIR = Path(__file__).resolve().parent.parent / "src" / "engram" / "hooks" / "claude"
sys.path.insert(0, str(HOOK_DIR))
import _hooklib  # noqa: E402

SPECIAL = ["a#b", "a?b", "a%b", "a%23b"]


def _make_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE marker_4171 (x)")
    c.execute("INSERT INTO marker_4171 VALUES (1)")
    c.commit()
    c.close()


def _tree(root: Path):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.parametrize("dirname", SPECIAL)
def test_q1_opens_the_named_file(tmp_path, dirname):
    db = tmp_path / dirname / "k.db"
    _make_db(db)
    before = _tree(tmp_path)
    conn = _hooklib.connect_regular_ro(str(db))
    try:
        files = [r[2] for r in conn.execute("PRAGMA database_list")]
        assert files == [os.path.realpath(db)]
        assert conn.execute("SELECT x FROM marker_4171").fetchall() == [(1,)]
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO marker_4171 VALUES (2)")  # mode=ro applied
    finally:
        conn.close()
    assert _tree(tmp_path) == before


# Only "a#b" and "a?b" discriminate here (red before the fix). "a%23b" and "a%b" are regression
# controls, green before the fix too (measured): unquoted, SQLite decodes %23 to a
# directory that does not exist and refuses. The percent signal is in Q1 ("a%23b").
@pytest.mark.parametrize("dirname", ["a#b", "a?b", "a%23b", "a%b"])
def test_q2_absent_path_raises_and_creates_nothing(tmp_path, dirname):
    (tmp_path / dirname).mkdir()
    # unquoted, '#' truncates the path to '<tmp>/a' (a fail-open would create it)
    before = _tree(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        _hooklib.connect_regular_ro(str(tmp_path / dirname / "absent.db"))
    assert _tree(tmp_path) == before, (before, _tree(tmp_path))


def test_q3_fifo_refused(tmp_path):
    p = tmp_path / "fifo.db"
    os.mkfifo(p)
    with pytest.raises(_hooklib.NotRegularDatabaseError if hasattr(_hooklib, "NotRegularDatabaseError")
                       else sqlite3.OperationalError) as ei:
        _hooklib.connect_regular_ro(str(p))
    assert "not a regular file" in str(ei.value)
    assert isinstance(ei.value, OSError) and isinstance(ei.value, sqlite3.OperationalError)


def test_q3_kwargs_and_uri_params_pass_through(tmp_path):
    db = tmp_path / "d#x" / "k.db"
    _make_db(db)
    conn = _hooklib.connect_regular_ro(str(db), uri_params="mode=ro&immutable=1", timeout=0.5)
    try:
        assert conn.execute("SELECT x FROM marker_4171").fetchall() == [(1,)]
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO marker_4171 VALUES (2)")
    finally:
        conn.close()


def test_q4_relative_path(tmp_path, monkeypatch):
    _make_db(tmp_path / "rel#dir" / "k.db")
    monkeypatch.chdir(tmp_path)
    conn = _hooklib.connect_regular_ro("rel#dir/k.db")
    try:
        assert [r[2] for r in conn.execute("PRAGMA database_list")] == [os.path.realpath(tmp_path / "rel#dir" / "k.db")]
        assert conn.execute("SELECT x FROM marker_4171").fetchall() == [(1,)]
    finally:
        conn.close()
