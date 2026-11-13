"""#3514: a read-only SQLite URI must percent-encode the path.

``sqlite3.connect(f"file:{path}?mode=ro", uri=True)`` loses ``mode=ro`` when the path holds ``?`` or ``#``:
SQLite opens the truncated path read-write-create, leaves a stray empty file, and raises nothing.
The fix is ``Path(p).absolute().as_uri() + "?mode=ro"`` (the form bootstrap.py and viz/data/sqlite_ro.py use).

Cell 1 is a source scan over every tracked .py file outside tests and viz, with a positive control.
Cell 2 runs real readers against a database under a directory named ``q?x#y``.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src" / "engram", ROOT / "src" / "engram" / "tools"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))

_UNQUOTED_RE = re.compile(r'f"file:\{[^}]*\}\?mode=')


def _is_unquoted(line: str) -> bool:
    """The f-string shape, minus a line that wraps the path in ``quote(`` (cites_step3.py: already safe)."""
    return bool(_UNQUOTED_RE.search(line)) and "quote(" not in line


def _tracked_py() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [
        f for f in out
        if not (f.startswith("tests/") or "/tests/" in f or f.startswith("src/engram/viz/"))
    ]


def test_scan_positive_control_fires():
    planted = 'conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)'
    assert _UNQUOTED_RE.search(planted) and _is_unquoted(planted)
    assert _is_unquoted('c = sqlite3.connect(f"file:{x}?mode=ro&immutable=1", uri=True)')
    # the quoted forms must not match
    assert not _is_unquoted('uri = Path(db_path).absolute().as_uri() + "?mode=ro"')
    assert not _is_unquoted('return f"file:{urllib.parse.quote(str(path))}?mode=ro"')


def test_no_unquoted_ro_uri_outside_tests_and_viz():
    files = _tracked_py()
    assert len(files) > 100, "git ls-files returned too few files; the scan is not looking at the tree"
    hits = []
    for f in files:
        for n, line in enumerate((ROOT / f).read_text(errors="replace").splitlines(), 1):
            if _is_unquoted(line):
                hits.append(f"{f}:{n}: {line.strip()}")
    assert not hits, "unquoted read-only URI (loses mode=ro on ? or #):\n" + "\n".join(hits)


def _make_db(tmp_path: Path) -> Path:
    d = tmp_path / "q?x#y"
    d.mkdir()
    db = d / "k.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE nodes (id TEXT, created_at TEXT)")
    c.execute("INSERT INTO nodes VALUES ('ob_1', '2026-01-01T00:00:00Z')")
    c.commit()
    c.close()
    return db


def _no_stray(tmp_path: Path) -> None:
    assert not (tmp_path / "q").exists(), "stray file at the truncated path: mode=ro was lost"


def test_anchoring_report_open_readonly(tmp_path):
    import anchoring_report

    db = _make_db(tmp_path)
    conn = anchoring_report._open_readonly(db)
    try:
        assert conn.execute("SELECT id FROM nodes").fetchone()["id"] == "ob_1"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO nodes VALUES ('ob_2', 'x')")
    finally:
        conn.close()
    _no_stray(tmp_path)


def test_backup_read_live_watermark(tmp_path):
    import backup_knowledge_db

    db = _make_db(tmp_path)
    wm = backup_knowledge_db._read_live_watermark(str(db))
    assert wm["row_count"] == 1
    assert wm["max_created_at"] == "2026-01-01T00:00:00Z"
    _no_stray(tmp_path)


def test_backup_write_binary_copy_reads_source_read_only(tmp_path):
    import backup_knowledge_db

    db = _make_db(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    backup_knowledge_db._write_binary_copy(str(out), str(db))
    copies = sorted((out / "binary").glob("knowledge-*.db")) if (out / "binary").exists() else []
    if not copies:  # _binary_dir layout may differ; fall back to any copy under out
        copies = [p for p in out.rglob("knowledge-*.db")]
    assert copies, "no binary copy was written (a failure only warns on stderr)"
    c = sqlite3.connect(str(copies[0]))
    try:
        assert c.execute("SELECT id FROM nodes").fetchone()[0] == "ob_1"
    finally:
        c.close()
    _no_stray(tmp_path)


def test_series_export_reads_without_stray(tmp_path):
    from health import series

    d = tmp_path / "q?x#y"
    d.mkdir()
    db = d / "series.db"
    series.open_store(str(db)).close()
    result = series.export(db)
    assert result.get("reason") not in ("unreadable", "no_db"), result
    _no_stray(tmp_path)
