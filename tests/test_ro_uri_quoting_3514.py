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

# The spellings of an unquoted read-only URI. Each regex captures the path expression as ``e``; the line is
# SAFE only when that expression itself starts with / contains a quote() call (never a quote( elsewhere on the
# line, never one in a trailing comment).
_FSTRING_RE = re.compile(r"""(?<![\w])(?:[rR][fF]|[fF][rR]?)(["'])file:\{(?P<e>[^}]*)\}[^"']*mode=""")
_CONCAT_RE = re.compile(r"""["']file:["']\s*\+\s*(?P<e>.*)""")
_PERCENT_RE = re.compile(r"""["']file:%s[^"']*mode=[^"']*["']\s*%\s*(?P<e>.*)""")
_FORMAT_RE = re.compile(r"""["']file:\{[^}]*\}[^"']*mode=[^"']*["']\s*\.format\((?P<e>.*)""")
_QUOTE_CALL = re.compile(r"^\s*(?:[\w.]*\.)?quote(?:_plus)?\(")
_QUOTE_ANY = re.compile(r"(?:^|[^\w])quote(?:_plus)?\(")


def _is_unquoted(line: str) -> bool:
    """True when ``line`` builds a ``file:`` URI with ``mode=`` from a path that is not percent-encoded."""
    if "mode=" not in line:
        return False
    m = _FSTRING_RE.search(line)
    if m:  # the path expression is the braces' content: quote() must wrap it THERE
        return not _QUOTE_ANY.search(m.group("e"))
    for rx in (_CONCAT_RE, _PERCENT_RE, _FORMAT_RE):
        m = rx.search(line)
        if m:  # the expression must START with a quote() call, not merely mention one later or in a comment
            return not _QUOTE_CALL.match(m.group("e"))
    return False


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    picked = []
    for f in out:
        if f.startswith("tests/") or "/tests/" in f or f.startswith("src/engram/viz/"):
            continue
        p = ROOT / f
        if not p.is_file() or p.is_symlink() and not p.exists():
            continue
        if p.suffix in (".py", ".sh"):
            picked.append(f)
        elif p.suffix == "":
            try:
                with open(p, "rb") as fh:
                    first = fh.readline(200)
            except OSError:
                continue
            if first.startswith(b"#!") and b"python" in first:
                picked.append(f)
    return picked


_PLANTED_UNSAFE = {
    "fstring_dq": 'c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)',
    "fstring_sq": "c = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)",
    "fstring_immutable": 'c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)',
    "rfstring": 'c = sqlite3.connect(rf"file:{db_path}?mode=ro", uri=True)',
    "fstring_rf_swapped": 'c = sqlite3.connect(fr"file:{db_path}?mode=ro", uri=True)',
    "concat": 'c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)',
    "concat_sq": "c = sqlite3.connect('file:' + str(p) + '?mode=ro', uri=True)",
    "percent": 'uri = "file:%s?mode=ro" % db_path',
    "format": 'uri = "file:{}?mode=ro".format(db_path)',
    "fstring_comment_quote": 'c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)  # quote(db) someday',
    "fstring_unrelated_quote": 'c = connect(f"file:{db}?mode=ro", name=quote(x))',
    "concat_unrelated_quote": 'c = connect("file:" + db + "?mode=ro", name=quote(x))',
    "percent_comment_quote": 'uri = "file:%s?mode=ro" % db  # quote(db)',
    "format_unrelated_quote": 'uri = "file:{}?mode=ro".format(db); n = quote(x)',
}
_PLANTED_SAFE = {
    "cites_step3": 'return f"file:{urllib.parse.quote(str(path))}?mode=ro"',
    "install_selfcheck": 'uri = "file:%s?mode=ro" % quote(str(ctx.db_path))',
    "propose_migration": 'uri = "file:" + urllib.parse.quote(str(db_path.absolute())) + "?mode=ro"',
    "format_quoted": 'uri = "file:{}?mode=ro".format(urllib.parse.quote(db))',
    "as_uri": 'uri = Path(db_path).absolute().as_uri() + "?mode=ro"',
    "no_mode": 'uri = f"file:{db_path}"',
}


@pytest.mark.parametrize("name", sorted(_PLANTED_UNSAFE))
def test_scan_positive_control_fires(name):
    assert _is_unquoted(_PLANTED_UNSAFE[name]), name


@pytest.mark.parametrize("name", sorted(_PLANTED_SAFE))
def test_scan_leaves_safe_forms_alone(name):
    assert not _is_unquoted(_PLANTED_SAFE[name]), name


def test_scan_file_selection_covers_py_sh_and_python_shebang_scripts():
    files = _tracked_files()
    assert len(files) > 100, "git ls-files returned too few files; the scan is not looking at the tree"
    assert "src/engram/tools/engram-pkg/engram-pkg" in files  # extensionless python script
    assert "src/forum/deploy/install-forum-service.sh" in files  # shell with inline python
    assert not any(f.startswith(("tests/", "src/engram/viz/")) or "/tests/" in f for f in files)
    assert not any(f.endswith(".md") for f in files)


def test_no_unquoted_ro_uri_outside_tests_and_viz():
    hits = []
    for f in _tracked_files():
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


def test_engram_pkg_render_read_node(tmp_path):
    import importlib.machinery
    import importlib.util

    cli = ROOT / "src" / "engram" / "tools" / "engram-pkg" / "engram-pkg"
    spec = importlib.util.spec_from_loader(
        "engram_pkg_cli_3514", importlib.machinery.SourceFileLoader("engram_pkg_cli_3514", str(cli))
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db = _make_db(tmp_path)
    row = mod._render_read_pkg_node(db, "ob_1")
    assert row is not None and row["id"] == "ob_1"
    _no_stray(tmp_path)
