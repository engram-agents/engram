"""Contract tests for viz.data.sqlite_ro.connect_ro (engram-alpha#3513 round 2).

The old ``f"file:{db_path}?mode=ro"`` URI split on ``?`` / ``#`` in the path:
``mode=ro`` was lost and SQLite opened the truncated path read-write-create,
leaving a stray empty file. connect_ro percent-encodes the path.
"""

from __future__ import annotations

import ast
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz.data.sqlite_ro import connect_ro

_VIZ = Path(__file__).resolve().parent.parent / "src" / "engram" / "viz"
_NASTY = ["q?x", "q#x", "q%x", "q x", "q%3Fx"]


def _snapshot(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


class TestConnectRo(unittest.TestCase):
    def _make(self, root: Path, rel: str) -> Path:
        db = root / rel
        db.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(db))
        c.execute("CREATE TABLE t (v TEXT)")
        c.execute("INSERT INTO t VALUES ('real')")
        c.commit()
        c.close()
        return db

    def test_special_chars_in_dir_and_filename(self):
        for bad in _NASTY:
            for where in ("dir", "file"):
                with self.subTest(bad=bad, where=where), \
                        tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    rel = f"{bad}/k.db" if where == "dir" else f"k{bad}.db"
                    db = self._make(root, rel)
                    before = _snapshot(root)
                    conn = connect_ro(str(db))
                    try:
                        self.assertEqual(
                            conn.execute("SELECT v FROM t").fetchone()[0], "real")
                        with self.assertRaises(sqlite3.OperationalError):
                            conn.execute("CREATE TABLE w (x)")
                    finally:
                        conn.close()
                    self.assertEqual(_snapshot(root), before)

    def test_immutable_reads_and_uri_carries_flag(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._make(Path(td), "a?b#c/k.db")
            seen = []
            real = sqlite3.connect

            def spy(*a, **k):
                seen.append((a, k))
                return real(*a, **k)

            with unittest.mock.patch.object(sqlite3, "connect", spy):
                conn = connect_ro(str(db), immutable=True)
            try:
                self.assertEqual(conn.execute("SELECT v FROM t").fetchone()[0], "real")
            finally:
                conn.close()
            self.assertIn("immutable=1", seen[0][0][0])
            self.assertIn("mode=ro", seen[0][0][0])
            self.assertTrue(seen[0][1].get("uri"))

    def test_timeout_passed_only_when_given(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._make(Path(td), "k.db")
            seen = []
            real = sqlite3.connect

            def spy(*a, **k):
                seen.append(k)
                return real(*a, **k)

            with unittest.mock.patch.object(sqlite3, "connect", spy):
                connect_ro(str(db)).close()
                connect_ro(str(db), timeout=0.5).close()
            self.assertNotIn("timeout", seen[0])
            self.assertEqual(seen[1].get("timeout"), 0.5)

    def test_missing_db_raises_and_creates_nothing(self):
        for bad in ("m?x", "m#x", "plain"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                before = _snapshot(root)
                with self.assertRaises(sqlite3.OperationalError):
                    connect_ro(str(root / bad / "nope.db"))
                with self.assertRaises(sqlite3.OperationalError):
                    connect_ro(str(root / f"{bad}.db"))
                self.assertEqual(_snapshot(root), before)


def _is_sqlite_connect(call: ast.Call, bare_names: set[str]) -> bool:
    f = call.func
    if isinstance(f, ast.Attribute):
        return (f.attr == "connect" and isinstance(f.value, ast.Name)
                and f.value.id == "sqlite3")
    return isinstance(f, ast.Name) and f.id in bare_names


def find_uri_connects(root: Path) -> list[str]:
    """file:line of every sqlite3.connect(..., uri=True) under root, except in
    data/sqlite_ro.py. Spelling-independent: looks at the call's ``uri``
    keyword, not at how the URI string is built."""
    offenders = []
    for p in sorted(root.rglob("*.py")):
        if p.relative_to(root).as_posix() == "data/sqlite_ro.py":
            continue
        tree = ast.parse(p.read_text(), filename=str(p))
        bare = {
            (a.asname or a.name)
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "sqlite3"
            for a in n.names if a.name == "connect"
        }
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and _is_sqlite_connect(n, bare):
                for kw in n.keywords:
                    if kw.arg == "uri" and not (
                            isinstance(kw.value, ast.Constant)
                            and kw.value.value is False):
                        offenders.append(
                            f"{p.relative_to(root).as_posix()}:{n.lineno}")
    return offenders


class TestNoRawUriConstruction(unittest.TestCase):
    def test_no_uri_connect_under_viz(self):
        self.assertEqual(find_uri_connects(_VIZ), [])

    def test_multiline_variable_built_uri_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir()
            (root / "data" / "sqlite_ro.py").write_text(
                "import sqlite3\nsqlite3.connect('file:x', uri=True)\n")
            (root / "bad.py").write_text(
                "import sqlite3\n"
                "sfx = '?mode=' + 'ro'\n"
                "conn = sqlite3.connect(\n"
                "    f'file:{p}' + sfx,\n"
                "    uri=True,\n"
                ")\n")
            (root / "bare.py").write_text(
                "from sqlite3 import connect as c\nc('x', uri=True)\n")
            self.assertEqual(find_uri_connects(root), ["bad.py:3", "bare.py:2"])

    def test_plain_path_connect_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ok.py").write_text(
                "import sqlite3\nsqlite3.connect(path)\n"
                "sqlite3.connect(path, timeout=1)\n")
            self.assertEqual(find_uri_connects(root), [])


if __name__ == "__main__":
    unittest.main()
