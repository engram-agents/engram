"""Contract tests for #3232 -- write_durable_snapshot backs up from a FRESH
read-only connection on conn's own DB file, never from the caller's ``conn``.

Why: ``conn.backup(dest)`` on a connection holding an UNCOMMITTED WRITE
transaction loops forever (sqlite 3.45.1, WAL). A read transaction (the #3224
read-mark shape) does not hang and must keep working.

Contract (written before the implementation):
  C1  uncommitted write on caller conn: no hang, snapshot == committed rows only
      (wal + delete journal modes)
  C2  caller's transaction untouched (still open, own row visible, commit works)
  C3  read-mark case (BEGIN DEFERRED + SELECT) still works, txn still open
  C4  no file behind conn (":memory:" / "") -> {"error"}, no snapshot, no .tmp
  C5  DB path comes from conn's database_list, not from <data_dir>/knowledge.db
  C6  paths containing space ? # % work (URI is percent-encoded)

The hang-prone scenarios run in a SUBPROCESS with a hard timeout because
pytest-timeout is not installed.
"""

import json
import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram"))
import engram_snapshot_worker as w  # noqa: E402

_SRC_ENGRAM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram")
_TIMEOUT = 20

_CHILD = textwrap.dedent(
    """
    import json, sqlite3, sys
    from pathlib import Path
    sys.path.append(sys.argv[1])
    import engram_snapshot_worker as w

    scenario, mode, db_dir, data_dir = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
    w._data_dir = lambda: Path(data_dir)
    db_path = str(Path(db_dir) / "live.db")

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=%s" % mode)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.execute("COMMIT")

    out = {}
    if scenario == "write":
        conn.execute("BEGIN")
        conn.execute("INSERT INTO t VALUES (2)")
    elif scenario == "read":
        conn.execute("BEGIN DEFERRED")
        conn.execute("SELECT count(*) FROM t").fetchall()
    res = w.write_durable_snapshot(conn, 7, 1)
    out["res"] = {k: v for k, v in res.items() if k != "bytes"}
    if "path" in res:
        s = sqlite3.connect(res["path"])
        out["snap_rows"] = [r[0] for r in s.execute("SELECT id FROM t ORDER BY id")]
        s.close()
    out["in_txn"] = conn.in_transaction
    out["conn_rows"] = [r[0] for r in conn.execute("SELECT id FROM t ORDER BY id")]
    if scenario == "write":
        conn.execute("COMMIT")
        f = sqlite3.connect(db_path)
        out["live_rows_after_commit"] = [r[0] for r in f.execute("SELECT id FROM t ORDER BY id")]
        f.close()
    print("RESULT:" + json.dumps(out))
    """
)


def _run_child(scenario, mode, tmp_path):
    db_dir = tmp_path / "db"
    data_dir = tmp_path / "data"
    db_dir.mkdir()
    data_dir.mkdir(exist_ok=True)
    try:
        p = subprocess.run(
            [sys.executable, "-c", _CHILD, _SRC_ENGRAM, scenario, mode, str(db_dir), str(data_dir)],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"snapshot hung (scenario={scenario!r}, journal_mode={mode!r}; "
            f"no return within {_TIMEOUT}s) -- #3232"
        )
    assert p.returncode == 0, f"child failed: {p.stderr}"
    line = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):])


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "_data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir(exist_ok=True)
    yield tmp_path


@pytest.mark.parametrize("mode", ["wal", "delete"])
def test_c1_uncommitted_write_does_not_hang_and_snapshot_is_committed_state(tmp_path, mode):
    out = _run_child("write", mode, tmp_path)
    assert out["res"].get("fsynced") is True, out
    assert out["snap_rows"] == [1]


@pytest.mark.parametrize("mode", ["wal", "delete"])
def test_c2_callers_transaction_is_untouched(tmp_path, mode):
    out = _run_child("write", mode, tmp_path)
    assert out["in_txn"] is True
    assert out["conn_rows"] == [1, 2]
    assert out["live_rows_after_commit"] == [1, 2]


@pytest.mark.parametrize("mode", ["wal", "delete"])
def test_c3_read_mark_case_still_works(tmp_path, mode):
    out = _run_child("read", mode, tmp_path)
    assert out["res"].get("fsynced") is True, out
    assert out["snap_rows"] == [1]
    assert out["in_txn"] is True


@pytest.mark.parametrize("target", [":memory:", ""])
def test_c4_no_file_behind_conn_is_loud_error(tmp_path, target):
    conn = sqlite3.connect(target)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    res = w.write_durable_snapshot(conn, 3, 1)
    assert "error" in res, res
    assert "fsynced" not in res
    pending = w._pending_dir()
    assert not (pending / w._snapshot_name(3, 1)).exists()
    assert list(pending.glob("*.tmp")) == []
    conn.close()


def test_c5_path_comes_from_conn_not_knowledge_db(tmp_path):
    decoy = sqlite3.connect(str(tmp_path / "data" / "knowledge.db"))
    decoy.execute("CREATE TABLE t (v TEXT)")
    decoy.execute("INSERT INTO t VALUES ('decoy')")
    decoy.commit()
    decoy.close()
    live = sqlite3.connect(str(tmp_path / "live.db"))
    live.execute("CREATE TABLE t (v TEXT)")
    live.execute("INSERT INTO t VALUES ('real')")
    live.commit()
    res = w.write_durable_snapshot(live, 4, 1)
    assert res.get("fsynced") is True, res
    s = sqlite3.connect(res["path"])
    vals = [r[0] for r in s.execute("SELECT v FROM t")]
    s.close()
    live.close()
    assert vals == ["real"]


def test_c6_special_character_path(tmp_path):
    d = tmp_path / "we?ird #dir %20 x"
    d.mkdir()
    conn = sqlite3.connect(str(d / "live.db"))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
    conn.commit()
    res = w.write_durable_snapshot(conn, 5, 1)
    assert res.get("fsynced") is True, res
    s = sqlite3.connect(res["path"])
    rows = [r[0] for r in s.execute("SELECT id FROM t ORDER BY id")]
    s.close()
    conn.close()
    assert rows == [1, 2]
