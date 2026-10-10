"""Completeness guard for docs/2652-migration-ledger.md [part of #2652].

The ledger lists every `sqlite3.connect(` line in tracked `src/engram`, one
Section-2 row per line. tools/check_2652_ledger.py compares, PER FILE, the
number of rows naming the file with the number of matching lines in it.
Per-file (not file:line) so unrelated edits that shift line numbers stay green.

Each test runs the script as a subprocess against a temp git repo; the real
ledger is only read, never edited.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "check_2652_ledger.py"

HEADER = "# ledger\n\n## 1. Population\n\ntext\n\n## 2. The ledger\n\n| # | site |\n|---|---|\n"
FOOTER = "\nPaths are relative to `src/engram/`.\n\n## 3. Registry\n\nnothing path-shaped here\n"


def _row(n, spec):
    return f"| {n} | `{spec}` (fn) | call | live store | rw | x | direct | — | — |\n"


def _ledger(rows):
    body = "".join(_row(i + 1, r) for i, r in enumerate(rows))
    return HEADER + body + FOOTER


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _make_repo(tmp_path, files, rows, untracked=None, ledger_text=None):
    _git(tmp_path, "init", "-q")
    for rel, text in files.items():
        p = tmp_path / "src" / "engram" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "2652-migration-ledger.md").write_text(
        ledger_text if ledger_text is not None else _ledger(rows), encoding="utf-8"
    )
    _git(tmp_path, "add", "-A")
    for rel, text in (untracked or {}).items():
        p = tmp_path / "src" / "engram" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def _run(root):
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root)],
        capture_output=True, text=True,
    )


CONNECT = "conn = sqlite3.connect(p)\n"


def test_clause1_real_ledger_matches_real_tree():
    r = _run(REPO)
    assert r.returncode == 0, r.stdout + r.stderr


def test_clause2_added_connect_without_row_fails_naming_file_and_counts(tmp_path):
    root = _make_repo(
        tmp_path,
        {"a.py": "x\n" + CONNECT + CONNECT, "b.py": CONNECT},
        ["a.py:2", "b.py:1"],
    )
    r = _run(root)
    assert r.returncode == 1
    assert "a.py: ledger 1 rows, tree 2 lines" in r.stdout
    assert "b.py" not in r.stdout.split("add/remove")[0]
    assert "add/remove a Section-2 row in docs/2652-migration-ledger.md" in r.stdout


def test_clause3_row_for_file_without_connect_fails(tmp_path):
    root = _make_repo(
        tmp_path,
        {"a.py": CONNECT, "gone.py": "nothing here\n"},
        ["a.py:1", "gone.py:7"],
    )
    r = _run(root)
    assert r.returncode == 1
    assert "gone.py: ledger 1 rows, tree 0 lines" in r.stdout


def test_clause4_line_drift_only_passes(tmp_path):
    root = _make_repo(
        tmp_path,
        {"a.py": "\n\n\n" + CONNECT + CONNECT},
        ["a.py:1", "a.py:2"],
    )
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_clause5_untracked_file_not_counted(tmp_path):
    root = _make_repo(
        tmp_path,
        {"a.py": CONNECT},
        ["a.py:1"],
        untracked={"scratch.py": CONNECT},
    )
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_clause6_unparseable_section2_fails_loudly(tmp_path):
    root = _make_repo(
        tmp_path,
        {"a.py": CONNECT},
        [],
        ledger_text="# ledger\n\n## 2. The ledger\n\nno table here\n\n## 3. x\n",
    )
    r = _run(root)
    assert r.returncode == 1
    assert "could not parse" in (r.stdout + r.stderr)


def test_clause6b_no_section2_heading_fails_loudly(tmp_path):
    root = _make_repo(tmp_path, {"a.py": CONNECT}, [], ledger_text="# nothing\n")
    r = _run(root)
    assert r.returncode == 1
    assert "could not parse" in (r.stdout + r.stderr)


def test_clause7_unreadable_tracked_file_fails_loudly_naming_it(tmp_path):
    # An unreadable file may hide connect lines; skipping it would let the
    # guard report consistent. Root reads anyway, so the setup cannot hold.
    import os

    import pytest

    if os.geteuid() == 0:
        pytest.skip("root can read a chmod 000 file")
    root = _make_repo(
        tmp_path, {"a.py": CONNECT, "locked.py": CONNECT}, ["a.py:1", "locked.py:1"]
    )
    locked = root / "src" / "engram" / "locked.py"
    locked.chmod(0)
    try:
        r = _run(root)
    finally:
        locked.chmod(0o644)
    assert r.returncode == 1
    assert "could not read src/engram/locked.py:" in r.stdout
    assert "a.py" not in r.stdout


def test_clause8_section3_row_naming_a_file_is_not_counted(tmp_path):
    # Section 3 rows have the same `| N | `file:line` ...` shape as Section 2
    # rows. Counting them would turn a consistent ledger red (a.py would read
    # 2 rows vs 1 line; zzz.py 1 row vs 0 lines).
    text = _ledger(["a.py:1"]).replace(
        "nothing path-shaped here\n",
        "| 1 | `a.py:1` (x) |\n| 2 | `zzz.py:9` (x) |\n",
    )
    assert "`zzz.py:9`" in text and "## 3." in text
    root = _make_repo(tmp_path, {"a.py": CONNECT}, [], ledger_text=text)
    r = _run(root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_clause9_outside_a_checkout_exits_1_with_one_line(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "2652-migration-ledger.md").write_text(
        _ledger(["a.py:1"]), encoding="utf-8"
    )
    r = _run(tmp_path)
    assert r.returncode == 1
    assert r.stdout.strip() == f"not a git checkout: {tmp_path}"
    assert "Traceback" not in r.stderr
