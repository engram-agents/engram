"""Tests for tools/worktree-gc.py — classify() decision matrix.

Coverage (all branches of classify()):
  1. is_main=True → MAIN
  2. dir missing → PRUNE (stale admin record)
  3. dirty (_is_clean→False) → SKIP even when PR merged (dirty gate wins)
  4. detached HEAD (branch=None) + clean → PRUNE
  5. branch "worktree-agent-xyz" + clean → PRUNE
  6. branch "review-123" + clean → PRUNE
  7. normal branch + _pr_state→"MERGED" + not in use → PRUNE
  8. normal branch + _pr_state→"CLOSED" → PRUNE
  9. normal branch + _pr_state→"OPEN" → KEEP
 10. normal branch + _pr_state→None (no PR) → KEEP
 11. normal branch + _pr_state→"MERGED" + IN USE → SKIP (in-use guard on the
     PR-state prune path — a fairy can still be writing to a worktree whose
     PR just merged; see worktree-gc.py's classify() comment at the fix site).

All tests monkeypatch _is_clean, _pr_state, and _is_in_use on the loaded
module so no real git or gh calls are made.
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
# 7. normal branch + PR MERGED + not in use → PRUNE
# ---------------------------------------------------------------------------


def test_classify_pr_merged(tmp_path):
    t = _make_worktree(str(tmp_path), branch="feat/awesome")
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_pr_state", return_value="MERGED"),
        patch.object(_mod, "_is_in_use", return_value=False),
    ):
        action, reason = _mod.classify(t, ROOT)
    assert action == "PRUNE"
    assert "merged" in reason.lower()


# ---------------------------------------------------------------------------
# 8. normal branch + PR CLOSED + not in use → PRUNE
# ---------------------------------------------------------------------------


def test_classify_pr_closed(tmp_path):
    t = _make_worktree(str(tmp_path), branch="feat/abandoned")
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_pr_state", return_value="CLOSED"),
        patch.object(_mod, "_is_in_use", return_value=False),
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


# ---------------------------------------------------------------------------
# 11. normal branch + PR MERGED + IN USE → SKIP (in-use guard on the
#     PR-state prune path — a fairy can still be writing to a worktree whose
#     PR just merged).
# ---------------------------------------------------------------------------


def test_classify_pr_merged_but_in_use_skips(tmp_path):
    """A worktree whose PR merged is still SKIPped, not PRUNEd, if a live
    process (the fairy) still has its CWD inside it."""
    t = _make_worktree(str(tmp_path), branch="feat/still-running")
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_pr_state", return_value="MERGED"),
        patch.object(_mod, "_is_in_use", return_value=True),
    ):
        action, reason = _mod.classify(t, ROOT)
    assert action == "SKIP"
    assert "process active" in reason
    assert "merged" in reason.lower()


def test_classify_pr_merged_but_in_use_is_never_removed(tmp_path):
    """Stronger than the classification-string check above: mirror main()'s
    own --apply gate (`if match["action"] != "PRUNE": continue`) and assert
    that the removal call (`git worktree remove`, which remove_worktree()
    issues via _run) never fires for a clean, PR-merged, still-in-use
    worktree -- an absent removal, not just a SKIP-shaped reason string.

    Mutation sensitivity: with the in-use guard removed from classify()'s
    PR-state path, this worktree reclassifies as PRUNE, the mirrored gate
    below calls remove_worktree(), and _run is invoked -- so
    mock_run.assert_not_called() catches the regression directly, the same
    way main()'s real --apply loop would actually delete the worktree.
    """
    t = _make_worktree(str(tmp_path), branch="feat/still-running")
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_pr_state", return_value="MERGED"),
        patch.object(_mod, "_is_in_use", return_value=True),
        patch.object(_mod, "_run") as mock_run,
    ):
        action, _reason = _mod.classify(t, ROOT)
        # Mirror main()'s apply-loop gate exactly (see worktree-gc.py main()).
        if action == "PRUNE":
            _mod.remove_worktree(t, ROOT)
    assert action == "SKIP"
    mock_run.assert_not_called()
    assert tmp_path.exists()


# ---------------------------------------------------------------------------
# 12. _prune_orphan_agent_refs — deletes orphans, keeps checked-out refs
# ---------------------------------------------------------------------------


def test_prune_orphan_agent_refs_deletes_orphans():
    """Orphan worktree-agent-* refs (not checked out) are deleted."""
    branch_list_out = "  worktree-agent-aaa\n  worktree-agent-bbb\n  worktree-agent-ccc\n"
    calls = []

    def fake_run(args, cwd=None):
        calls.append(args)
        if args[:3] == ["git", "branch", "--list"]:
            return 0, branch_list_out, ""
        if args[:3] == ["git", "branch", "-D"]:
            return 0, "", ""
        return 0, "", ""

    with patch.object(_mod, "_run", side_effect=fake_run):
        pruned, total = _mod._prune_orphan_agent_refs(ROOT, checked_out=set())

    assert total == 3
    assert pruned == 3
    deleted = [c[3] for c in calls if c[:3] == ["git", "branch", "-D"]]
    assert sorted(deleted) == ["worktree-agent-aaa", "worktree-agent-bbb", "worktree-agent-ccc"]


def test_prune_orphan_agent_refs_keeps_checked_out():
    """A worktree-agent-* ref still checked out in a worktree is not deleted."""
    branch_list_out = "  worktree-agent-aaa\n* worktree-agent-bbb\n"
    calls = []

    def fake_run(args, cwd=None):
        calls.append(args)
        if args[:3] == ["git", "branch", "--list"]:
            return 0, branch_list_out, ""
        if args[:3] == ["git", "branch", "-D"]:
            return 0, "", ""
        return 0, "", ""

    with patch.object(_mod, "_run", side_effect=fake_run):
        pruned, total = _mod._prune_orphan_agent_refs(
            ROOT, checked_out={"worktree-agent-bbb"}
        )

    assert total == 1  # only aaa is an orphan
    assert pruned == 1
    deleted = [c[3] for c in calls if c[:3] == ["git", "branch", "-D"]]
    assert deleted == ["worktree-agent-aaa"]  # bbb was not deleted


def test_prune_orphan_agent_refs_no_refs():
    """Returns (0, 0) when no worktree-agent-* refs exist."""
    with patch.object(_mod, "_run", return_value=(0, "", "")):
        pruned, total = _mod._prune_orphan_agent_refs(ROOT, checked_out=set())
    assert pruned == 0
    assert total == 0
