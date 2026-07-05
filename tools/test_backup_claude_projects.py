"""Tests for src/engram/tools/backup_claude_projects.py (issue #1098).

Coverage:
  1.  Top-level file in src appears in archive at same relative path
  2.  Nested subdirectory file is mirrored at correct relative path
  3.  Second run with unchanged file → 0 copied, 1 skipped
  4.  Re-copy when source size grows
  5.  Re-copy when source mtime changes (>1s difference)
  6.  --dry-run makes no writes
  7.  --retain prunes archive files older than threshold
  8.  Empty source dir → exit 0, "0 files copied"
  9.  Nonexistent source dir → exit 0, "0 files copied"
  10. --compress → dest has .gz suffix, content is valid gzip of source
  11. --compress dedup: second run skips (os.utime mtime-stamp mechanism)
  12. --retain + --dry-run: prints would-prune but does not delete
"""

from __future__ import annotations

import gzip
import io
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Locate and import the module under test
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent
_ROOT = _THIS_DIR.parent  # repo root

_TOOLS_SRC = _ROOT / "src" / "engram" / "tools"
if str(_TOOLS_SRC) not in sys.path:
    sys.path.insert(0, str(_TOOLS_SRC))

import backup_claude_projects as bcp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_file(path: Path, content: str = "hello") -> Path:
    """Write a file at the given path (creates parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _run_main(
    *,
    projects_dir: Path,
    archive_dir: Path,
    retain: int = 0,
    dry_run: bool = False,
    compress: bool = False,
) -> tuple[int, str]:
    """Call bcp.main() with the given args; capture printed output.

    Returns (exit_code, captured_stdout).
    """
    argv = [
        "--claude-projects-dir", str(projects_dir),
        "--archive-dir", str(archive_dir),
    ]
    if retain:
        argv += ["--retain", str(retain)]
    if dry_run:
        argv.append("--dry-run")
    if compress:
        argv.append("--compress")

    buf = io.StringIO()
    with mock.patch("sys.stdout", buf):
        rc = bcp.main(argv)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# Test 1: top-level file IS copied at same relative path
# ---------------------------------------------------------------------------

def test_top_level_file_copy(tmp_path):
    src = tmp_path / "projects"
    archive = tmp_path / "archive"

    _make_file(src / "config.json", '{"key":"value"}')

    rc, out = _run_main(projects_dir=src, archive_dir=archive)

    assert rc == 0
    dest = archive / "config.json"
    assert dest.exists(), "top-level file was not copied to archive"
    assert dest.read_text() == '{"key":"value"}'
    assert "1 files copied" in out


# ---------------------------------------------------------------------------
# Test 2: nested subdirectory file is mirrored at correct relative path
# ---------------------------------------------------------------------------

def test_nested_subdirectory_copy(tmp_path):
    src = tmp_path / "projects"
    archive = tmp_path / "archive"

    _make_file(src / "subdir" / "file.jsonl", '{"nested":true}')

    rc, out = _run_main(projects_dir=src, archive_dir=archive)

    assert rc == 0
    dest = archive / "subdir" / "file.jsonl"
    assert dest.exists(), "nested file was not mirrored at correct relative path"
    assert dest.read_text() == '{"nested":true}'
    assert "1 files copied" in out


# ---------------------------------------------------------------------------
# Test 3: skip if unchanged (same size + mtime within 1s)
# ---------------------------------------------------------------------------

def test_skip_if_unchanged(tmp_path):
    src = tmp_path / "projects"
    archive = tmp_path / "archive"

    content = '{"data":"unchanged"}'
    _make_file(src / "session.jsonl", content)

    # First run: copies the file.
    rc1, out1 = _run_main(projects_dir=src, archive_dir=archive)
    assert rc1 == 0
    assert "1 files copied" in out1

    # Second run: file is unchanged → skip.
    rc2, out2 = _run_main(projects_dir=src, archive_dir=archive)
    assert rc2 == 0
    assert "0 files copied" in out2
    assert "1 skipped" in out2


# ---------------------------------------------------------------------------
# Test 4: re-copy if source size grew
# ---------------------------------------------------------------------------

def test_recopy_if_size_differs(tmp_path):
    src = tmp_path / "projects"
    archive = tmp_path / "archive"

    f = _make_file(src / "growing.jsonl", '{"v":1}')

    rc1, _ = _run_main(projects_dir=src, archive_dir=archive)
    assert rc1 == 0

    # Grow the source file so sizes differ.
    f.write_text('{"v":1,"extra":"data appended to grow the file"}')

    rc2, out2 = _run_main(projects_dir=src, archive_dir=archive)
    assert rc2 == 0
    assert "1 files copied" in out2


# ---------------------------------------------------------------------------
# Test 5: re-copy if mtime differs (>= 1s)
# ---------------------------------------------------------------------------

def test_recopy_if_mtime_differs(tmp_path):
    src = tmp_path / "projects"
    archive = tmp_path / "archive"

    f = _make_file(src / "touched.jsonl", '{"same":"content"}')

    rc1, _ = _run_main(projects_dir=src, archive_dir=archive)
    assert rc1 == 0

    # Advance mtime by 2 seconds — beyond the 1s tolerance.
    old_mtime = os.path.getmtime(str(f))
    new_mtime = old_mtime + 2.0
    os.utime(str(f), (new_mtime, new_mtime))

    rc2, out2 = _run_main(projects_dir=src, archive_dir=archive)
    assert rc2 == 0
    assert "1 files copied" in out2


# ---------------------------------------------------------------------------
# Test 6: --dry-run makes no writes
# ---------------------------------------------------------------------------

def test_dry_run(tmp_path):
    src = tmp_path / "projects"
    archive = tmp_path / "archive"

    _make_file(src / "dryrun.jsonl", '{"dry":true}')

    rc, out = _run_main(projects_dir=src, archive_dir=archive, dry_run=True)

    assert rc == 0
    # Archive dir either doesn't exist or is empty.
    if archive.exists():
        files = [p for p in archive.rglob("*") if p.is_file()]
        assert not files, f"dry-run wrote files: {files}"
    # Output should mention the would-copy action.
    assert "would copy" in out or "[dry-run]" in out


# ---------------------------------------------------------------------------
# Test 7: --retain prunes old archive files
# ---------------------------------------------------------------------------

def test_retain_pruning(tmp_path):
    src = tmp_path / "projects"
    archive = tmp_path / "archive"
    archive.mkdir(parents=True)

    # A fresh source file (so we don't hit the "0 files" early exit).
    _make_file(src / "fresh.jsonl", '{"fresh":true}')

    # Plant a stale archive file (400 days old).
    stale = archive / "old-backup.jsonl"
    stale.write_text('{"stale":true}')
    old_mtime = time.time() - (400 * 86400)
    os.utime(str(stale), (old_mtime, old_mtime))

    rc, out = _run_main(projects_dir=src, archive_dir=archive, retain=365)

    assert rc == 0
    assert not stale.exists(), "stale file should have been pruned by --retain 365"
    assert "pruned" in out


# ---------------------------------------------------------------------------
# Test 8: empty source dir → exit 0, "0 files copied"
# ---------------------------------------------------------------------------

def test_empty_source_dir(tmp_path):
    src = tmp_path / "projects"
    src.mkdir(parents=True)
    archive = tmp_path / "archive"

    rc, out = _run_main(projects_dir=src, archive_dir=archive)

    assert rc == 0
    assert "0 files copied" in out


# ---------------------------------------------------------------------------
# Test 9: nonexistent source dir → exit 0, "0 files copied"
# ---------------------------------------------------------------------------

def test_nonexistent_source_dir(tmp_path):
    src = tmp_path / "does-not-exist"
    archive = tmp_path / "archive"

    rc, out = _run_main(projects_dir=src, archive_dir=archive)

    assert rc == 0
    assert "0 files copied" in out


# ---------------------------------------------------------------------------
# Test 10: --compress → dest has .gz suffix, content is valid gzip of source
# ---------------------------------------------------------------------------

def test_compress_flag(tmp_path):
    src = tmp_path / "projects"
    archive = tmp_path / "archive"

    original = '{"compress":"me"}'
    _make_file(src / "data.jsonl", original)

    rc, out = _run_main(projects_dir=src, archive_dir=archive, compress=True)

    assert rc == 0
    dest_gz = archive / "data.jsonl.gz"
    assert dest_gz.exists(), "compressed file not found at .gz path"

    # Decompress and verify content matches original.
    with gzip.open(str(dest_gz), "rb") as f:
        recovered = f.read().decode()
    assert recovered == original, (
        f"decompressed content mismatch: expected {original!r}, got {recovered!r}"
    )
    assert "1 files copied" in out


# ---------------------------------------------------------------------------
# Test 11: --compress dedup second-run (os.utime stamp makes second run skip)
# ---------------------------------------------------------------------------

def test_compress_dedup_second_run(tmp_path):
    src = tmp_path / "projects"
    archive = tmp_path / "archive"

    _make_file(src / "data.jsonl", '{"compress":"dedup"}')

    # First run: copies and stamps dest mtime = src mtime.
    rc1, out1 = _run_main(projects_dir=src, archive_dir=archive, compress=True)
    assert rc1 == 0
    assert "1 files copied" in out1

    # Second run: mtime matches → skip.
    rc2, out2 = _run_main(projects_dir=src, archive_dir=archive, compress=True)
    assert rc2 == 0
    assert "0 files copied" in out2
    assert "1 skipped" in out2


# ---------------------------------------------------------------------------
# Test 12: --retain + --dry-run doesn't actually delete
# ---------------------------------------------------------------------------

def test_retain_dry_run(tmp_path):
    src = tmp_path / "projects"
    archive = tmp_path / "archive"
    archive.mkdir(parents=True)

    _make_file(src / "fresh.jsonl", '{"fresh":true}')

    # Plant a stale archive file (400 days old).
    stale = archive / "old-backup.jsonl"
    stale.write_text('{"stale":true}')
    old_mtime = time.time() - (400 * 86400)
    os.utime(str(stale), (old_mtime, old_mtime))

    rc, out = _run_main(projects_dir=src, archive_dir=archive, retain=365, dry_run=True)

    assert rc == 0
    # Dry-run: stale file must NOT be deleted.
    assert stale.exists(), "--dry-run must not delete stale files"
    assert "would prune" in out or "[dry-run]" in out
