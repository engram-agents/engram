"""Tests for tools/worktree-gc.py — classify() decision matrix.

Coverage (all branches of classify()):
  1. is_main=True → MAIN
  2. dir missing → PRUNE (stale admin record)
  3. dirty (_is_clean→False) → SKIP even when PR merged (dirty gate wins)
  4. detached HEAD (branch=None) + clean + NO open PR at that SHA → PRUNE
  5. branch "worktree-agent-xyz" + clean → PRUNE
  6. branch "review-123" + clean → PRUNE
  7. normal branch + _pr_state→"MERGED" + not in use → PRUNE
  8. normal branch + _pr_state→"CLOSED" → PRUNE
  9. normal branch + _pr_state→"OPEN" → KEEP
 10. normal branch + _pr_state→None (no PR) → KEEP
 11. normal branch + _pr_state→"MERGED" + IN USE → SKIP (in-use guard on the
     PR-state prune path — a fairy can still be writing to a worktree whose
     PR just merged; see worktree-gc.py's classify() comment at the fix site).
 12. detached HEAD whose SHA is an OPEN PR's head → SKIP naming that PR
     (the reviewer-workflow `git worktree add --detach … refs/pr/<N>` shape).
 13. detached HEAD where the open-PR lookup fails / gh is unavailable → SKIP
     (a delete verb must fail toward keeping when the state is unknown).

classify() tests monkeypatch _is_clean, _pr_state, _is_in_use and (for the
detached cases) _pr_for_sha on the loaded module. The direct _pr_for_sha unit
cells patch _run instead (they exercise the real helper). Nothing here makes a
real git or gh call.
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
    head: str = "abc1234",
) -> dict:
    """Build a minimal worktree dict as returned by _list_worktrees."""
    return {
        "path": path,
        "head": head,
        "branch": branch,
        "detached": detached,
        "is_main": is_main,
    }


ROOT = "/fake/repo"


@pytest.fixture(autouse=True)
def _clear_open_pr_heads():
    """Isolate the module-global open-PR cache between cells.

    ``_OPEN_PR_HEADS`` is keyed on repo root and lives for the whole process,
    so a cell that populates (or fails to populate) it would otherwise change
    the next cell's behaviour, making pass/fail depend on execution order.
    Cleared before and after every cell.
    """
    _mod._OPEN_PR_HEADS.clear()
    yield
    _mod._OPEN_PR_HEADS.clear()


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
    """Control: a clean detached-HEAD worktree whose SHA matches NO open PR is
    still a prunable reviewer leftover. _pr_for_sha is stubbed to 'verified, no
    open PR' — this is the no-open-PR control for the two cells below."""
    t = _make_worktree(str(tmp_path), branch=None, detached=True)
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_pr_for_sha", return_value=(True, None)),
    ):
        action, reason = _mod.classify(t, ROOT)
    assert action == "PRUNE"
    assert "detached" in reason.lower()


def test_classify_detached_head_matching_open_pr_skips(tmp_path):
    """A clean detached-HEAD worktree at an OPEN PR's head is a reviewer's
    checkout (`git worktree add --detach … refs/pr/<N>`), not a leftover: SKIP,
    and the reason names the PR so the human can tell why."""
    t = _make_worktree(
        str(tmp_path), branch=None, detached=True, head="deadbeef"
    )
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_pr_for_sha", return_value=(True, 4496)) as mock_lookup,
    ):
        action, reason = _mod.classify(t, ROOT)
    assert action == "SKIP"
    assert "#4496" in reason
    # The worktree's own HEAD SHA must be what classify looks up (not "" /
    # a constant) — otherwise the lookup is wired to the wrong value.
    mock_lookup.assert_called_once_with("deadbeef", ROOT)


def test_classify_detached_head_lookup_failure_skips(tmp_path):
    """If the open-PR lookup itself fails (gh unavailable / raised), the state
    is unknown — a delete verb must fail toward keeping, so SKIP, unverified."""
    t = _make_worktree(str(tmp_path), branch=None, detached=True, head="deadbeef")
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_pr_for_sha", side_effect=FileNotFoundError("gh")),
    ):
        action, reason = _mod.classify(t, ROOT)
    assert action == "SKIP"
    assert "unverified" in reason.lower()


def test_classify_detached_head_in_use_beats_open_pr_lookup(tmp_path):
    """The in-use guard runs BEFORE the open-PR lookup, so a detached worktree
    that is both in use and sitting at an OPEN PR's head is SKIPped for the
    in-use reason: the PR lookup is never even consulted. This is the cell
    behind the PR body's claim that the in-use guard "keeps precedence over
    all of it" — without it, a refactor could reorder the two checks and no
    test would notice."""
    t = _make_worktree(str(tmp_path), branch=None, detached=True, head="deadbeef")
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_is_in_use", return_value=True),
        patch.object(_mod, "_pr_for_sha", return_value=(True, 7)) as mock_lookup,
    ):
        action, reason = _mod.classify(t, ROOT)
    assert action == "SKIP"
    # Distinguishable from the open-PR SKIP reason: that one reads "matches
    # OPEN PR #N", this one "process active".
    assert "process active" in reason
    assert "OPEN PR" not in reason
    mock_lookup.assert_not_called()


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


# ---------------------------------------------------------------------------
# 14. _pr_for_sha — DIRECT unit cells (patching _run, not _pr_for_sha)
#
# Every detached-HEAD cell above mocks _pr_for_sha at the classify() boundary,
# so the helper that carries this tool's whole safety property ("never prune
# on unknown") had no direct cell: replacing its body with `raise` or with
# `return (True, None)` left the suite green. These cells pin the contract:
#   (True, N)    -> an OPEN PR #N has this head SHA
#   (True, None) -> verified: no open PR has this head SHA  (only for `[]`)
#   (False, None)-> the lookup could not be made; caller must not prune
# ---------------------------------------------------------------------------

MATCH_PAYLOAD = '[{"number": 7, "headRefOid": "deadbeef"}]'
NONMATCH_PAYLOAD = '[{"number": 9, "headRefOid": "cafebabe"}]'
# Two open PRs, the matching one SECOND: a helper that returns any open PR's
# number instead of looking the SHA up reports 9 here, not 7.
MIXED_PAYLOAD = (
    '[{"number": 9, "headRefOid": "cafebabe"},'
    ' {"number": 7, "headRefOid": "deadbeef"}]'
)


def test_pr_for_sha_matching_open_pr_returns_number():
    """A well-formed gh payload with a matching head SHA yields (True, 7)."""
    with patch.object(_mod, "_run", return_value=(0, MATCH_PAYLOAD, "")):
        result = _mod._pr_for_sha("deadbeef", ROOT)
    assert result == (True, 7)


def test_pr_for_sha_nonmatching_sha_against_nonempty_list_is_verified_no_pr():
    """The SHA is actually MATCHED, not merely used as a cache key: a non-empty
    table with no entry for this SHA is still an affirmative (True, None).

    Without this cell the lookup half is unpinned — a helper returning *some*
    open PR whenever any PR is open (`next(iter(heads.values()))`) passes every
    other cell here, and silently makes every detached worktree unprunable."""
    with patch.object(_mod, "_run", return_value=(0, NONMATCH_PAYLOAD, "")):
        result = _mod._pr_for_sha("deadbeef", ROOT)
    assert result == (True, None)


def test_pr_for_sha_picks_the_matching_entry_from_a_multi_pr_payload():
    """Two open PRs, only the second matching: the helper returns ITS number.

    A payload whose first entry matches would let a position-based lookup pass
    by accident, so the matching entry is deliberately not first."""
    with patch.object(_mod, "_run", return_value=(0, MIXED_PAYLOAD, "")):
        result = _mod._pr_for_sha("deadbeef", ROOT)
    assert result == (True, 7)


def test_pr_for_sha_asks_gh_for_open_prs_with_both_fields_and_a_high_limit():
    """The production argv is part of the safety property, and nothing else
    asserts what we actually ask `gh` for. Each token below decides a failure
    mode: `--state all` lets merged/closed heads match; a missing or small
    `--limit` hides open PRs past the cut (gh's default is 30); and
    `--json number` alone makes every entry unreadable, which is the bug this
    function exists to prevent, reached by a one-token edit."""
    with patch.object(_mod, "_run", return_value=(0, "[]", "")) as mock_run:
        _mod._pr_for_sha("deadbeef", ROOT)
    argv = mock_run.call_args[0][0]
    assert argv[:3] == ["gh", "pr", "list"], argv
    assert argv[argv.index("--state") + 1] == "open", argv
    assert int(argv[argv.index("--limit") + 1]) >= 1000, argv
    fields = argv[argv.index("--json") + 1].split(",")
    assert "headRefOid" in fields and "number" in fields, argv


@pytest.mark.parametrize(
    "payload",
    [
        '[{"number": 7}]',                              # headRefOid missing
        '[{"headRefOid": "deadbeef"}]',                 # number missing
        '[{"number": 7, "headRefOid": 123}]',           # headRefOid not a string
        '[{"number": true, "headRefOid": "deadbeef"}]',  # bool is not a PR number
        '["not-an-object"]',                            # entry is not a dict
        '[{"number": 7, "headRefOid": "deadbeef"}, "x"]',  # one good, one unreadable
    ],
    ids=["no-head-oid", "no-number", "non-string-oid", "bool-number",
         "non-dict-entry", "mixed-good-and-unreadable"],
)
def test_pr_for_sha_unreadable_entry_makes_the_lookup_unverified(payload):
    """An entry we cannot read must make the WHOLE answer unknown.

    Skipping it instead is the dangerous default: with `--json number` alone
    every entry would be skipped, `heads` would be empty, and the caller would
    be told "verified: no open PR" — every open PR's checkout pruned again."""
    with patch.object(_mod, "_run", return_value=(0, payload, "")):
        result = _mod._pr_for_sha("deadbeef", ROOT)
    assert result == (False, None)


def test_pr_for_sha_empty_list_is_verified_no_pr():
    """`[]` is a correct AFFIRMATIVE answer: gh ran, opened PRs are the empty
    set, so this SHA matches none of them — (True, None), the one shape that
    lets the caller prune. Must not be swept up by the abnormal-response
    tightening below."""
    with patch.object(_mod, "_run", return_value=(0, "[]", "")):
        result = _mod._pr_for_sha("deadbeef", ROOT)
    assert result == (True, None)


@pytest.mark.parametrize(
    "run_return",
    [
        (0, "{}", ""),        # JSON object — not the list gh emits
        (0, '"x"', ""),       # bare JSON string likewise
        (0, "", ""),          # rc=0 but NO output: unknown, not "no open PR"
        (1, "", "boom"),      # gh exited non-zero
        (0, "not json", ""),  # unparseable
    ],
    ids=["json-object", "json-string", "empty-stdout", "rc-nonzero", "unparseable"],
)
def test_pr_for_sha_abnormal_response_is_unverified(run_return):
    """Any response that is not a normal `gh pr list` list is UNVERIFIED, never
    an affirmative "no open PR". Treating these as (True, None) is the bug
    that would prune open-PR heads."""
    with patch.object(_mod, "_run", return_value=run_return):
        result = _mod._pr_for_sha("deadbeef", ROOT)
    assert result == (False, None)


def test_pr_for_sha_oserror_is_unverified():
    """A subprocess/OSError (gh missing) is unverified, never raises out."""
    with patch.object(_mod, "_run", side_effect=OSError("gh missing")):
        result = _mod._pr_for_sha("deadbeef", ROOT)
    assert result == (False, None)


def test_pr_for_sha_empty_sha_is_unverified_without_running_gh():
    """An empty SHA short-circuits to unverified and never invokes gh."""
    with patch.object(_mod, "_run") as mock_run:
        result = _mod._pr_for_sha("", ROOT)
    assert result == (False, None)
    mock_run.assert_not_called()


def test_pr_for_sha_caches_successful_lookup_per_root():
    """A second call for the same root reuses the cache: one _run total."""
    with patch.object(_mod, "_run", return_value=(0, MATCH_PAYLOAD, "")) as mock_run:
        first = _mod._pr_for_sha("deadbeef", ROOT)
        second = _mod._pr_for_sha("deadbeef", ROOT)
    assert first == (True, 7)
    assert second == (True, 7)
    assert mock_run.call_count == 1


def test_pr_for_sha_does_not_cache_a_failed_lookup():
    """A FAILED lookup is never cached: two failing calls make two _run calls,
    and the root never enters _OPEN_PR_HEADS."""
    with patch.object(_mod, "_run", return_value=(1, "", "boom")) as mock_run:
        first = _mod._pr_for_sha("deadbeef", ROOT)
        second = _mod._pr_for_sha("deadbeef", ROOT)
    assert first == (False, None)
    assert second == (False, None)
    assert mock_run.call_count == 2
    assert ROOT not in _mod._OPEN_PR_HEADS
