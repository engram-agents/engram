"""Tests for tools/worktree-gc.py — classify() decision matrix.

Coverage (all branches of classify()):
  1. is_main=True → MAIN
  2. dir missing → PRUNE (stale admin record)
  3. dirty (_is_clean→False) → SKIP even when PR merged (dirty gate wins)
  4. detached HEAD (branch=None) + clean → PRUNE
  5. branch "worktree-agent-xyz" + clean → PRUNE
  6. branch "review-123" + clean → PRUNE
  7. normal branch + _pr_state→"MERGED" → PRUNE
  8. normal branch + _pr_state→"CLOSED" → PRUNE
  9. normal branch + _pr_state→"OPEN" → KEEP
 10. normal branch + _pr_state→None (no PR) → KEEP

All tests monkeypatch _is_clean and _pr_state on the loaded module so no real
git or gh calls are made.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Load the module (hyphenated filename requires importlib)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKTREE_GC = REPO_ROOT / "src" / "engram" / "tools" / "worktree-gc.py"

_spec = importlib.util.spec_from_file_location("worktree_gc", WORKTREE_GC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worktree(
    path: str,
    branch: str | None = "some-feature",
    detached: bool = False,
    is_main: bool = False,
) -> dict:
    """Build a minimal worktree dict as returned by _list_worktrees."""
    return {
        "path": path,
        "head": "abc1234",
        "branch": branch,
        "detached": detached,
        "is_main": is_main,
    }


ROOT = "/fake/repo"


# ---------------------------------------------------------------------------
# 1. is_main → MAIN
# ---------------------------------------------------------------------------


def test_classify_main(tmp_path):
    t = _make_worktree(str(tmp_path), is_main=True)
    action, reason = _mod.classify(t, ROOT)
    assert action == "MAIN"
    assert "main checkout" in reason


# ---------------------------------------------------------------------------
# 2. directory missing → PRUNE (stale admin record)
# ---------------------------------------------------------------------------


def test_classify_missing_dir():
    t = _make_worktree("/nonexistent/path/does/not/exist")
    action, reason = _mod.classify(t, ROOT)
    assert action == "PRUNE"
    assert "missing" in reason


# ---------------------------------------------------------------------------
# 3. dirty → SKIP (even when PR merged — dirty gate wins first)
# ---------------------------------------------------------------------------


def test_classify_dirty_gate_wins_over_merged(tmp_path):
    """SKIP is returned for a dirty worktree even when the PR is merged."""
    t = _make_worktree(str(tmp_path), branch="feat/done")
    with (
        patch.object(_mod, "_is_clean", return_value=False),
        patch.object(_mod, "_pr_state", return_value="MERGED"),
    ):
        action, reason = _mod.classify(t, ROOT)
    assert action == "SKIP"
    assert "DIRTY" in reason


def test_classify_dirty_returns_skip(tmp_path):
    """Any dirty worktree is SKIP regardless of branch name."""
    t = _make_worktree(str(tmp_path), branch="worktree-agent-123")
    with patch.object(_mod, "_is_clean", return_value=False):
        action, reason = _mod.classify(t, ROOT)
    assert action == "SKIP"
    assert "DIRTY" in reason


# ---------------------------------------------------------------------------
# 4. detached HEAD + clean → PRUNE
# ---------------------------------------------------------------------------


def test_classify_detached_clean(tmp_path):
    t = _make_worktree(str(tmp_path), branch=None, detached=True)
    with patch.object(_mod, "_is_clean", return_value=True):
        action, reason = _mod.classify(t, ROOT)
    assert action == "PRUNE"
    assert "detached" in reason.lower()


# ---------------------------------------------------------------------------
# 5. worktree-agent-* branch + clean → PRUNE
# ---------------------------------------------------------------------------


def test_classify_worktree_agent_branch(tmp_path):
    t = _make_worktree(str(tmp_path), branch="worktree-agent-abc123")
    with patch.object(_mod, "_is_clean", return_value=True):
        action, reason = _mod.classify(t, ROOT)
    assert action == "PRUNE"
    assert "worktree-agent-abc123" in reason


# ---------------------------------------------------------------------------
# 6. review-* branch + clean → PRUNE
# ---------------------------------------------------------------------------


def test_classify_review_branch(tmp_path):
    t = _make_worktree(str(tmp_path), branch="review-456")
    with patch.object(_mod, "_is_clean", return_value=True):
        action, reason = _mod.classify(t, ROOT)
    assert action == "PRUNE"
    assert "review-456" in reason


# ---------------------------------------------------------------------------
# 7. normal branch + PR MERGED → PRUNE
# ---------------------------------------------------------------------------


def test_classify_pr_merged(tmp_path):
    t = _make_worktree(str(tmp_path), branch="feat/awesome")
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_pr_state", return_value="MERGED"),
    ):
        action, reason = _mod.classify(t, ROOT)
    assert action == "PRUNE"
    assert "merged" in reason.lower()


# ---------------------------------------------------------------------------
# 8. normal branch + PR CLOSED → PRUNE
# ---------------------------------------------------------------------------


def test_classify_pr_closed(tmp_path):
    t = _make_worktree(str(tmp_path), branch="feat/abandoned")
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_pr_state", return_value="CLOSED"),
    ):
        action, reason = _mod.classify(t, ROOT)
    assert action == "PRUNE"
    assert "closed" in reason.lower()


# ---------------------------------------------------------------------------
# 9. normal branch + PR OPEN → KEEP
# ---------------------------------------------------------------------------


def test_classify_pr_open(tmp_path):
    t = _make_worktree(str(tmp_path), branch="feat/wip")
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_pr_state", return_value="OPEN"),
    ):
        action, reason = _mod.classify(t, ROOT)
    assert action == "KEEP"
    assert "OPEN" in reason


# ---------------------------------------------------------------------------
# 10. normal branch + no PR (None) → KEEP
# ---------------------------------------------------------------------------


def test_classify_no_pr(tmp_path):
    t = _make_worktree(str(tmp_path), branch="feat/not-pushed-yet")
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_pr_state", return_value=None),
    ):
        action, reason = _mod.classify(t, ROOT)
    assert action == "KEEP"
    assert "no PR" in reason
