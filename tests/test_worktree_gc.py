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

14. AGE GUARD (#2808): an agent-owned worktree (branch `worktree-agent-*` /
    `review-*`, OR any worktree under `<main>/.claude/worktrees/`) younger than
    AGE_GUARD_SECONDS is SKIPped on every prune path; age unreadable -> SKIP;
    the /proc in-use guard is still honoured past the age; non-agent-owned
    worktrees are unchanged. These cells use REAL temp repos and worktrees
    (age is read from the worktree's git admin metadata) with os.utime.

classify() tests monkeypatch _is_clean, _pr_state, _is_in_use and (for the
detached cases) _pr_for_sha on the loaded module. The direct _pr_for_sha unit
cells patch _run instead (they exercise the real helper). Nothing here makes a
real git or gh call.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
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


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _real_worktree(tmp_path, branch, under_claude=True):
    """Make a real main repo + one real worktree. Returns (main_root, wt_path).

    branch=None -> detached HEAD. under_claude -> the worktree sits at
    <main>/.claude/worktrees/<name>, else at a sibling directory outside it.
    """
    main = tmp_path / "main"
    if not main.exists():
        main.mkdir()
        _git("init", "-q", "-b", "main", cwd=main)
        (main / "f").write_text("x")
        _git("add", "f", cwd=main)
        _git("commit", "-q", "-m", "init", cwd=main)
    name = (branch or "detached").replace("/", "_")
    if under_claude:
        wt = main / ".claude" / "worktrees" / name
    else:
        wt = tmp_path / "elsewhere" / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    if branch is None:
        _git("worktree", "add", "-q", "--detach", str(wt), cwd=main)
    else:
        _git("worktree", "add", "-q", "-b", branch, str(wt), cwd=main)
    return str(main), wt


def _admin_dir(wt):
    line = (wt / ".git").read_text().strip()
    assert line.startswith("gitdir: ")
    return Path(line[len("gitdir: "):])


def _set_age(wt, seconds):
    """Make every metadata file the guard may read exactly `seconds` old."""
    admin = _admin_dir(wt)
    when = time.time() - seconds
    for rel in ("HEAD", "index", "logs/HEAD"):
        f = admin / rel
        if f.exists():
            os.utime(f, (when, when))


def _classify_real(root, wt, branch, *, pr_state=None, in_use=False, **kw):
    t = _make_worktree(str(wt), branch=branch, detached=branch is None,
                       head="deadbeef")
    with (
        patch.object(_mod, "_is_clean", return_value=True),
        patch.object(_mod, "_pr_state", return_value=pr_state),
        patch.object(_mod, "_is_in_use", return_value=in_use),
        patch.object(_mod, "_pr_for_sha", return_value=(True, None)),
    ):
        return _mod.classify(t, root, **kw)


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
    # Agent-owned since #2808: needs a readable age, so a real worktree aged
    # past the guard (the decision under test is unchanged: old + clean -> PRUNE).
    _root, wt = _real_worktree(tmp_path, "worktree-agent-abc123", under_claude=False)
    _set_age(wt, 6 * 3600 + 60)
    t = _make_worktree(str(wt), branch="worktree-agent-abc123")
    with patch.object(_mod, "_is_clean", return_value=True):
        action, reason = _mod.classify(t, ROOT)
    assert action == "PRUNE"
    assert "worktree-agent-abc123" in reason


# ---------------------------------------------------------------------------
# 6. review-* branch + clean → PRUNE
# ---------------------------------------------------------------------------


def test_classify_review_branch(tmp_path):
    # Agent-owned since #2808 (see test_classify_worktree_agent_branch).
    _root, wt = _real_worktree(tmp_path, "review-456", under_claude=False)
    _set_age(wt, 6 * 3600 + 60)
    t = _make_worktree(str(wt), branch="review-456")
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


# ---------------------------------------------------------------------------
# 14. AGE GUARD (#2808)
# ---------------------------------------------------------------------------

# (id, branch, under_claude, pr_state) -- the four agent-owned shapes
_AGENT_SHAPES = [
    ("agent-branch", "worktree-agent-abc", True, None),
    ("review-branch", "review-77", True, None),
    ("detached-under-claude", None, True, None),
    ("named-merged-under-claude", "fix/some-thing", True, "MERGED"),
]
_SHAPE_IDS = [x[0] for x in _AGENT_SHAPES]


def test_age_guard_constant_is_six_hours():
    assert _mod.AGE_GUARD_SECONDS == 6 * 3600


@pytest.mark.parametrize("_id,branch,under,pr", _AGENT_SHAPES, ids=_SHAPE_IDS)
def test_age_guard_young_agent_worktree_is_skipped(tmp_path, _id, branch, under, pr):
    root, wt = _real_worktree(tmp_path, branch, under_claude=under)
    _set_age(wt, _mod.AGE_GUARD_SECONDS - 60)
    action, reason = _classify_real(root, wt, branch, pr_state=pr)
    assert action == "SKIP"
    assert "age guard" in reason
    assert "6h" in reason
    assert "359m" in reason  # age rounded to minutes


@pytest.mark.parametrize("_id,branch,under,pr", _AGENT_SHAPES, ids=_SHAPE_IDS)
def test_age_guard_old_agent_worktree_still_prunes(tmp_path, _id, branch, under, pr):
    root, wt = _real_worktree(tmp_path, branch, under_claude=under)
    _set_age(wt, _mod.AGE_GUARD_SECONDS + 60)
    action, reason = _classify_real(root, wt, branch, pr_state=pr)
    assert action == "PRUNE", reason


def test_age_guard_is_newest_of_head_index_logs(tmp_path):
    """The newest of HEAD / index / logs/HEAD counts: one young file keeps it."""
    root, wt = _real_worktree(tmp_path, "worktree-agent-new")
    _set_age(wt, _mod.AGE_GUARD_SECONDS + 3600)
    young = time.time() - 120
    os.utime(_admin_dir(wt) / "index", (young, young))
    action, reason = _classify_real(root, wt, "worktree-agent-new")
    assert action == "SKIP" and "age guard" in reason


def test_age_guard_unreadable_age_fails_closed(tmp_path):
    root, wt = _real_worktree(tmp_path, "worktree-agent-x")
    _set_age(wt, _mod.AGE_GUARD_SECONDS + 3600)  # old, but we cannot see it
    (wt / ".git").write_text("gitdir: " + str(tmp_path / "no-such-admin") + "\n")
    action, reason = _classify_real(root, wt, "worktree-agent-x")
    assert action == "SKIP"
    assert "could not be read" in reason


def test_age_guard_missing_dotgit_fails_closed(tmp_path):
    root, wt = _real_worktree(tmp_path, None)
    (wt / ".git").unlink()
    # main() always passes main_path (trees[0]); without a `.git` file a direct
    # caller cannot derive it, so pass it as main() does.
    action, reason = _classify_real(root, wt, None, main_path=root)
    assert action == "SKIP"
    assert "could not be read" in reason


@pytest.mark.parametrize("_id,branch,under,pr", _AGENT_SHAPES, ids=_SHAPE_IDS)
def test_age_guard_old_but_in_use_is_still_skipped(tmp_path, _id, branch, under, pr):
    root, wt = _real_worktree(tmp_path, branch, under_claude=under)
    _set_age(wt, _mod.AGE_GUARD_SECONDS + 60)
    action, reason = _classify_real(root, wt, branch, pr_state=pr, in_use=True)
    assert action == "SKIP"
    assert "process active" in reason


def test_age_guard_named_merged_outside_claude_worktrees_unchanged(tmp_path):
    """Not agent-owned (named branch, outside .claude/worktrees): young is fine."""
    root, wt = _real_worktree(tmp_path, "feat/mine", under_claude=False)
    _set_age(wt, 60)
    action, reason = _classify_real(root, wt, "feat/mine", pr_state="MERGED")
    assert action == "PRUNE", reason


def test_age_guard_missing_dir_is_exempt(tmp_path):
    root, _wt = _real_worktree(tmp_path, "worktree-agent-gone")
    t = _make_worktree(str(tmp_path / "gone"), branch="worktree-agent-gone")
    action, reason = _mod.classify(t, root)
    assert action == "PRUNE" and "missing" in reason


def test_age_guard_seconds_argument_overrides_default(tmp_path):
    root, wt = _real_worktree(tmp_path, "worktree-agent-arg")
    _set_age(wt, 600)
    assert _classify_real(root, wt, "worktree-agent-arg")[0] == "SKIP"
    assert _classify_real(root, wt, "worktree-agent-arg",
                          age_guard_seconds=300)[0] == "PRUNE"


def test_cli_flag_reaches_classify(tmp_path):
    seen = {}

    def fake_classify(t, root, **kw):
        seen.update(kw)
        return "KEEP", "stub"

    with (
        patch.object(sys, "argv", ["worktree-gc", "--age-guard-seconds", "1234"]),
        patch.object(_mod, "_repo_root", return_value=str(tmp_path)),
        patch.object(_mod, "_list_worktrees",
                     return_value=[_make_worktree(str(tmp_path))]),
        patch.object(_mod, "classify", side_effect=fake_classify),
    ):
        assert _mod.main() == 0
    assert seen.get("age_guard_seconds") == 1234


def test_main_passes_main_checkout_not_run_root_as_main_path(tmp_path):
    """main() run from a sibling worktree must hand classify trees[0]'s path
    (the main checkout), not the run root (#2808 round 3)."""
    main_dir = str(tmp_path / "main-checkout")
    run_root = str(tmp_path / "sibling-run-dir")
    trees = [_make_worktree(main_dir), _make_worktree(run_root)]
    seen = []

    def fake_classify(t, root, **kw):
        seen.append((root, kw))
        return "KEEP", "stub"

    with (
        patch.object(sys, "argv", ["worktree-gc"]),
        patch.object(_mod, "_repo_root", return_value=run_root),
        patch.object(_mod, "_list_worktrees", return_value=trees),
        patch.object(_mod, "classify", side_effect=fake_classify),
    ):
        assert _mod.main() == 0
    assert len(seen) == len(trees)
    for root, kw in seen:
        assert root == run_root
        assert kw["main_path"] == trees[0]["path"]
        assert kw["main_path"] != run_root


# ---------------------------------------------------------------------------
# 15. AGE GUARD uses the MAIN checkout's folder, not the directory the tool
#     runs from (#2808 round 2)
# ---------------------------------------------------------------------------


def _sibling_root(tmp_path, main):
    """A second linked worktree OUTSIDE <main>/.claude/worktrees/: the
    `--show-toplevel` a run from inside a linked worktree would report."""
    sib = tmp_path / "sibling-run-dir"
    _git("worktree", "add", "-q", "--detach", str(sib), cwd=main)
    return str(sib)


@pytest.mark.parametrize("branch,pr", [(None, None), ("fix/named-thing", "MERGED")],
                         ids=["detached", "named-merged"])
def test_age_guard_uses_main_checkout_when_run_from_sibling_worktree(tmp_path, branch, pr):
    main, wt = _real_worktree(tmp_path, branch)
    _set_age(wt, 120)
    sib = _sibling_root(tmp_path, main)
    action, reason = _classify_real(sib, wt, branch, pr_state=pr)
    assert action == "SKIP", reason
    assert "age guard" in reason


@pytest.mark.parametrize("branch,pr", [(None, None), ("fix/named-thing", "MERGED")],
                         ids=["detached", "named-merged"])
def test_age_guard_explicit_main_path_beats_run_root(tmp_path, branch, pr):
    main, wt = _real_worktree(tmp_path, branch)
    _set_age(wt, 120)
    sib = _sibling_root(tmp_path, main)
    action, reason = _classify_real(sib, wt, branch, pr_state=pr, main_path=main)
    assert action == "SKIP", reason


def test_age_guard_main_path_underivable_means_branch_rule_only(tmp_path):
    """No derivable main path: the under-folder test is False (never `root`)."""
    main, wt = _real_worktree(tmp_path, None)
    _set_age(wt, 120)
    with patch.object(_mod, "_main_checkout_path", return_value=None):
        action, reason = _classify_real(main, wt, None)
    assert action == "PRUNE", reason


def test_age_guard_boundary_age_equal_to_guard_prunes(tmp_path):
    """The rule is `age < guard` keeps: age == guard is old enough to prune."""
    root, wt = _real_worktree(tmp_path, "worktree-agent-edge")
    _set_age(wt, 0)
    newest = max(p.stat().st_mtime for p in
                 (_admin_dir(wt) / r for r in ("HEAD", "index", "logs/HEAD"))
                 if p.exists())
    guard = 1000
    with patch.object(_mod.time, "time", return_value=newest + guard):
        action, reason = _classify_real(root, wt, "worktree-agent-edge",
                                        age_guard_seconds=guard)
    assert action == "PRUNE", reason
    with patch.object(_mod.time, "time", return_value=newest + guard - 1):
        action, reason = _classify_real(root, wt, "worktree-agent-edge",
                                        age_guard_seconds=guard)
    assert action == "SKIP", reason
