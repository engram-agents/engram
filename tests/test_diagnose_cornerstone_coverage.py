"""Tests for engram_diagnose cornerstone_coverage section (issue #931).

Coverage:
  1. cornerstone_coverage key present in diagnose output.
  2. Empty cornerstone set → total=0, covered=0, uncovered=0, uncovered_list=[].
  3. Cornerstone with ID in warm-briefing.md → covered.
  4. Cornerstone with ID in CLAUDE.md → covered.
  5. Cornerstone with tag (not ID) in CLAUDE.md → covered.
  6. Cornerstone not in any surface → appears in uncovered_list with correct fields.
  7. surfaces_checked lists only surfaces that actually exist.

Import-pattern contract (#948 wave A — R1):
    This file imports engram_stats, engram_cornerstone, engram_core directly
    and contains no ``import server``. Module objects are passed to
    fresh_module() (R4) for per-test reload isolation.
"""

from __future__ import annotations

import atexit
import contextlib
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402

import engram_cornerstone
import engram_core
import engram_stats

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_cs_coverage_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _make_fresh_mods(test_dir: str | None = None):
    """Reload modules, configure a clean test DB, return (stats, cs, core)."""
    td = test_dir or TEST_DIR
    if os.path.exists(td):
        shutil.rmtree(td)
    os.makedirs(td, exist_ok=True)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    stats = fresh_module(engram_stats)
    cs = importlib.import_module("engram_cornerstone")
    core = sys.modules["engram_core"]
    core._configure_paths(td)
    core._ensure_data_dir()
    return stats, cs, core


@contextlib.contextmanager
def _patch_home(fake_home: Path):
    """Patch Path.home() in engram_stats to return fake_home."""
    with mock.patch("engram_stats.Path") as m:
        m.home.return_value = fake_home
        yield m


def _add_cs(cs, tag: str, title: str) -> str:
    r = json.loads(cs._add_cornerstone_impl(
        tag=tag,
        title=title,
        new_frame=f"New frame for {title}",
        prior_frame=f"Old frame for {title}",
        triggering_experience="Test trigger",
        supporting_ids="",
    ))
    assert "cornerstone_id" in r, f"cornerstone creation failed: {r}"
    return r["cornerstone_id"]


def _write_claude_md(home_dir: Path, content: str) -> Path:
    claude_md = home_dir / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True, exist_ok=True)
    claude_md.write_text(content)
    return claude_md


# ── 1. Key present ───────────────────────────────────────────────────────────

def test_cornerstone_coverage_key_present():
    """engram_diagnose must include a 'cornerstone_coverage' key."""
    td = tempfile.mkdtemp(prefix="engram_test_cs_key_")
    try:
        stats, _cs, _core = _make_fresh_mods(td)
        with _patch_home(Path(td) / "no_home"):
            r = json.loads(stats._diagnose_impl())
        assert "cornerstone_coverage" in r, (
            f"cornerstone_coverage key missing. Keys: {list(r.keys())}"
        )
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ── 2. Empty cornerstone set ─────────────────────────────────────────────────

def test_empty_cornerstone_set():
    """No cornerstones → total=0, covered=0, uncovered_list=[]."""
    td = tempfile.mkdtemp(prefix="engram_test_cs_empty_")
    try:
        stats, _cs, _core = _make_fresh_mods(td)
        with _patch_home(Path(td) / "no_home"):
            r = json.loads(stats._diagnose_impl())
        cc = r["cornerstone_coverage"]
        assert cc["total"] == 0
        assert cc["covered"] == 0
        assert cc["uncovered"] == 0
        assert cc["uncovered_list"] == []
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ── 3. ID in warm-briefing.md → covered ─────────────────────────────────────

def test_cs_id_in_warm_briefing_covered():
    """A cornerstone whose ID appears in warm-briefing.md is counted as covered."""
    td = tempfile.mkdtemp(prefix="engram_test_cs_wb_")
    try:
        stats, cs, core = _make_fresh_mods(td)
        cs_id = _add_cs(cs, "test-wb-tag", "Test WB cornerstone")

        # Write the ID into warm-briefing.md (at core.DATA_DIR)
        wb = Path(td) / "warm-briefing.md"
        wb.write_text(f"Anchored: ({cs_id}) — test principle.\n")

        # Redirect home so CLAUDE.md is absent
        with _patch_home(Path(td) / "no_home"):
            r = json.loads(stats._diagnose_impl())

        cc = r["cornerstone_coverage"]
        assert cc["total"] == 1, f"expected total=1, got {cc}"
        assert cc["covered"] == 1
        assert cc["uncovered"] == 0
        assert cc["uncovered_list"] == []
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ── 4. ID in CLAUDE.md → covered ────────────────────────────────────────────

def test_cs_id_in_claude_md_covered():
    """A cornerstone whose ID appears in CLAUDE.md is counted as covered."""
    td = tempfile.mkdtemp(prefix="engram_test_cs_claude_")
    try:
        stats, cs, core = _make_fresh_mods(td)
        cs_id = _add_cs(cs, "test-claude-tag", "Test CLAUDE cornerstone")

        # No warm-briefing.md; write the ID into CLAUDE.md
        fake_home = Path(td) / "fake_home"
        _write_claude_md(fake_home, f"Rule: {cs_id} always applies.\n")

        with _patch_home(fake_home):
            r = json.loads(stats._diagnose_impl())

        cc = r["cornerstone_coverage"]
        assert cc["total"] == 1
        assert cc["covered"] == 1
        assert cc["uncovered_list"] == []
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ── 5. Tag in CLAUDE.md → covered ───────────────────────────────────────────

def test_cs_tag_in_claude_md_covered():
    """A cornerstone whose tag (not ID) appears in CLAUDE.md is counted as covered."""
    td = tempfile.mkdtemp(prefix="engram_test_cs_tag_")
    try:
        stats, cs, core = _make_fresh_mods(td)
        _add_cs(cs, "write-discipline", "Write now not later")

        fake_home = Path(td) / "fake_home"
        # CLAUDE.md carries the tag name, not the cs_ ID
        _write_claude_md(fake_home, "## write-discipline section\nAlways write immediately.\n")

        with _patch_home(fake_home):
            r = json.loads(stats._diagnose_impl())

        cc = r["cornerstone_coverage"]
        assert cc["total"] == 1
        assert cc["covered"] == 1
        assert cc["uncovered_list"] == []
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ── 6. Cornerstone not in any surface → uncovered ───────────────────────────

def test_cs_not_in_any_surface_uncovered():
    """A cornerstone absent from all surfaces appears in uncovered_list."""
    td = tempfile.mkdtemp(prefix="engram_test_cs_uncov_")
    try:
        stats, cs, core = _make_fresh_mods(td)
        cs_id = _add_cs(cs, "orphan-tag", "Orphan cornerstone")

        with _patch_home(Path(td) / "empty_home"):
            r = json.loads(stats._diagnose_impl())

        cc = r["cornerstone_coverage"]
        assert cc["total"] == 1
        assert cc["covered"] == 0
        assert cc["uncovered"] == 1
        ids_in_uncovered = [e["id"] for e in cc["uncovered_list"]]
        assert cs_id in ids_in_uncovered
        # uncovered entries must have id, tag, title fields
        entry = cc["uncovered_list"][0]
        assert "id" in entry and "tag" in entry and "title" in entry
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ── 7. surfaces_checked reflects existing files ──────────────────────────────

def test_surfaces_checked_lists_existing_files_only():
    """surfaces_checked includes only surfaces that actually exist on disk."""
    td = tempfile.mkdtemp(prefix="engram_test_cs_surfaces_")
    try:
        stats, cs, core = _make_fresh_mods(td)

        # Neither file exists yet
        with _patch_home(Path(td) / "no_home"):
            r = json.loads(stats._diagnose_impl())
        assert r["cornerstone_coverage"]["surfaces_checked"] == []

        # Add warm-briefing.md
        (Path(td) / "warm-briefing.md").write_text("anchor\n")
        with _patch_home(Path(td) / "no_home"):
            r = json.loads(stats._diagnose_impl())
        assert "warm-briefing.md" in r["cornerstone_coverage"]["surfaces_checked"]
        assert "CLAUDE.md" not in r["cornerstone_coverage"]["surfaces_checked"]

        # Add CLAUDE.md
        fake_home = Path(td) / "fake_home"
        _write_claude_md(fake_home, "rules\n")
        with _patch_home(fake_home):
            r = json.loads(stats._diagnose_impl())
        checked = r["cornerstone_coverage"]["surfaces_checked"]
        assert "warm-briefing.md" in checked
        assert "CLAUDE.md" in checked
    finally:
        shutil.rmtree(td, ignore_errors=True)
