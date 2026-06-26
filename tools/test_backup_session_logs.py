"""Tests for src/engram/tools/backup_session_logs.py (issue #1129).

Coverage:
  1. Top-level .jsonl IS copied to archive
  2. A .jsonl under some/subagents/file.jsonl is NOT copied
  3. Skip-if-already-archived-and-same-size works
  4. Re-copy if size differs
  5. Basename collision → hash-prefix dedup (two project dirs, same filename → two entries)
  6. --dry-run makes no writes
  7. --retain prunes old entries by mtime
  8. Empty projects dir → no error, 0-backed-up summary
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Locate and import the module under test
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent
_ROOT = _THIS_DIR.parent  # repo root

_TOOLS_SRC = _ROOT / "src" / "engram" / "tools"
if str(_TOOLS_SRC) not in sys.path:
    sys.path.insert(0, str(_TOOLS_SRC))

import backup_session_logs as bsl  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jsonl(path: Path, content: str = "{}") -> Path:
    """Write a minimal .jsonl file at the given path (creates parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _run_main(
    *,
    projects_dir: Path,
    archive_dir: Path,
    retain: int = 0,
    dry_run: bool = False,
) -> tuple[int, str]:
    """Call bsl.main() with the given dirs; capture printed output.

    Returns (exit_code, captured_stdout).
    """
    import io
    from unittest import mock

    argv = [
        "--claude-projects-dir", str(projects_dir),
        "--archive-dir", str(archive_dir),
    ]
    if retain:
        argv += ["--retain", str(retain)]
    if dry_run:
        argv.append("--dry-run")

    buf = io.StringIO()
    with mock.patch("sys.stdout", buf):
        rc = bsl.main(argv)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# Test 1: top-level .jsonl IS copied
# ---------------------------------------------------------------------------

def test_toplevel_log_is_copied(tmp_path):
    projects = tmp_path / "projects"
    archive = tmp_path / "archive"

    log = _make_jsonl(projects / "proj-abc" / "session-1.jsonl", '{"event":"start"}')

    rc, out = _run_main(projects_dir=projects, archive_dir=archive)

    assert rc == 0
    dest = archive / "session-1.jsonl"
    assert dest.exists(), "top-level log was not copied to archive"
    assert dest.read_text() == '{"event":"start"}'
    assert "Backed up 1 session logs" in out


# ---------------------------------------------------------------------------
# Test 2: .jsonl under subagents/ is NOT copied
# ---------------------------------------------------------------------------

def test_subagent_log_is_skipped(tmp_path):
    projects = tmp_path / "projects"
    archive = tmp_path / "archive"

    # A subagent log — must NOT be archived.
    _make_jsonl(projects / "proj-abc" / "subagents" / "fairy-1.jsonl", '{"fairy":true}')
    # A top-level log in a different project — MUST be archived.
    _make_jsonl(projects / "proj-xyz" / "session-main.jsonl", '{"main":true}')

    rc, out = _run_main(projects_dir=projects, archive_dir=archive)

    assert rc == 0
    assert not (archive / "fairy-1.jsonl").exists(), "subagent log must not be archived"
    assert (archive / "session-main.jsonl").exists(), "top-level log must be archived"
    assert "Backed up 1 session logs" in out


# ---------------------------------------------------------------------------
# Test 3: skip if already archived AND same size
# ---------------------------------------------------------------------------

def test_skip_already_archived_same_size(tmp_path):
    projects = tmp_path / "projects"
    archive = tmp_path / "archive"
    archive.mkdir()

    content = '{"event":"start"}\n'
    _make_jsonl(projects / "proj-abc" / "session-1.jsonl", content)

    # Pre-populate archive with an identical file.
    (archive / "session-1.jsonl").write_text(content)

    rc, out = _run_main(projects_dir=projects, archive_dir=archive)

    assert rc == 0
    assert "Backed up 0 session logs" in out
    assert "1 skipped" in out


# ---------------------------------------------------------------------------
# Test 4: re-copy if size differs
# ---------------------------------------------------------------------------

def test_recopy_if_size_differs(tmp_path):
    projects = tmp_path / "projects"
    archive = tmp_path / "archive"
    archive.mkdir()

    new_content = '{"event":"start","extra":"data"}\n'
    _make_jsonl(projects / "proj-abc" / "session-1.jsonl", new_content)

    # Stale archive entry — shorter than the source.
    (archive / "session-1.jsonl").write_text('{"event":"start"}\n')

    rc, out = _run_main(projects_dir=projects, archive_dir=archive)

    assert rc == 0
    assert "Backed up 1 session logs" in out
    # Content must have been updated.
    assert (archive / "session-1.jsonl").read_text() == new_content


# ---------------------------------------------------------------------------
# Test 5: basename collision → hash-prefix dedup
# ---------------------------------------------------------------------------

def test_basename_collision_hash_prefix(tmp_path):
    projects = tmp_path / "projects"
    archive = tmp_path / "archive"

    # Two different project dirs, same session filename.
    _make_jsonl(projects / "proj-alpha" / "session.jsonl", '{"proj":"alpha"}')
    _make_jsonl(projects / "proj-beta" / "session.jsonl", '{"proj":"beta"}')

    rc, out = _run_main(projects_dir=projects, archive_dir=archive)

    assert rc == 0
    archive_files = list(archive.iterdir())
    jsonl_files = [f for f in archive_files if f.suffix == ".jsonl"]
    assert len(jsonl_files) == 2, (
        f"expected 2 archive entries for 2 colliding basenames, got: {[f.name for f in jsonl_files]}"
    )

    names = {f.name for f in jsonl_files}
    # One bare name, one with hash prefix.
    assert "session.jsonl" in names, "bare basename entry missing"
    prefixed = [n for n in names if n != "session.jsonl"]
    assert len(prefixed) == 1
    assert prefixed[0].endswith("-session.jsonl"), f"expected hash-prefixed name, got: {prefixed[0]}"
    # Hash prefix must be 8 hex chars.
    prefix_part = prefixed[0][: -len("-session.jsonl")]
    assert len(prefix_part) == 8 and all(c in "0123456789abcdef" for c in prefix_part), (
        f"hash prefix not 8 hex chars: {prefix_part!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: --dry-run makes no writes
# ---------------------------------------------------------------------------

def test_dry_run_makes_no_writes(tmp_path):
    projects = tmp_path / "projects"
    archive = tmp_path / "archive"

    _make_jsonl(projects / "proj-abc" / "session-dryrun.jsonl", '{"dry":true}')

    rc, out = _run_main(projects_dir=projects, archive_dir=archive, dry_run=True)

    assert rc == 0
    # Archive directory must not have been created with actual files.
    if archive.exists():
        assert not any(archive.iterdir()), "dry-run must not write any files"
    assert "[dry-run]" in out
    assert "would copy" in out


# ---------------------------------------------------------------------------
# Test 7: --retain prunes old entries by mtime
# ---------------------------------------------------------------------------

def test_retain_prunes_old_entries(tmp_path):
    projects = tmp_path / "projects"
    archive = tmp_path / "archive"
    archive.mkdir()

    # A fresh source log (to avoid the 0-candidates early return).
    _make_jsonl(projects / "proj-abc" / "fresh.jsonl", '{"fresh":true}')

    # Plant two stale archive entries with old mtime (10 days ago).
    stale_1 = archive / "old-session-1.jsonl"
    stale_2 = archive / "old-session-2.jsonl"
    stale_1.write_text('{"stale":1}')
    stale_2.write_text('{"stale":2}')
    old_mtime = time.time() - (10 * 86400)
    os.utime(str(stale_1), (old_mtime, old_mtime))
    os.utime(str(stale_2), (old_mtime, old_mtime))

    # Plant one recent archive entry (1 second ago) — must NOT be pruned.
    recent = archive / "recent-session.jsonl"
    recent.write_text('{"recent":true}')
    # Its mtime is already ~now, so no adjustment needed.

    rc, out = _run_main(projects_dir=projects, archive_dir=archive, retain=5)

    assert rc == 0
    assert not stale_1.exists(), "stale entry 1 should have been pruned"
    assert not stale_2.exists(), "stale entry 2 should have been pruned"
    assert recent.exists(), "recent entry must not be pruned"


# ---------------------------------------------------------------------------
# Test 8: empty projects dir → no error, 0 backed up
# ---------------------------------------------------------------------------

def test_empty_projects_dir(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    archive = tmp_path / "archive"

    rc, out = _run_main(projects_dir=projects, archive_dir=archive)

    assert rc == 0
    assert "Backed up 0 session logs" in out


# ---------------------------------------------------------------------------
# Test 9: nonexistent projects dir → no error, 0 backed up
# ---------------------------------------------------------------------------

def test_nonexistent_projects_dir(tmp_path):
    projects = tmp_path / "does-not-exist"
    archive = tmp_path / "archive"

    rc, out = _run_main(projects_dir=projects, archive_dir=archive)

    assert rc == 0
    assert "Backed up 0 session logs" in out
