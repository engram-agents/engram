"""Tests for the post-approval re-check guard in tools/baton.py (issue #1002).

Companion to test_baton_ci_guard.py (#974/#685): that guard checks CI is
green NOW; this one checks the green tip is the APPROVED tip.

Coverage — wiring (cmd_flip):
  1. fresh approval → flip proceeds (no stderr)
  2. stale approval (tip moved after approval) → refuses (EXIT_VALIDATION), no file mutation
  3. stale + --force → proceeds with warning
  4. no_approval → passes through silently (colleague-layer's jurisdiction)
  5. unknown verdict → proceeds with advisory warning
  6. flip to NON-sentinel participant → guard not consulted
  7. red CI rejects BEFORE the approval guard runs (fail-fast ordering)

Coverage — _pr_approval_state parsing (mocked subprocess, two-call shape):
  8. approved-review oid == tip oid → fresh
  9. approved review exists, oid != tip (rebase case) → stale
 10. no APPROVED review (COMMENTED/CHANGES_REQUESTED only) → no_approval
 11. DISMISSED only → no_approval
 12. review with commit null + one matching approval → fresh (null tolerated)
 13. gh GraphQL failure / empty / unparseable → unknown
 14. repo-view (first call) failure → unknown

All tests are hermetic: no real `gh` calls, no real baton files outside tmp_path.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_THIS_DIR = Path(__file__).parent
_ROOT = _THIS_DIR.parent

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.baton as baton  # noqa: E402


SENTINEL = "maintainer"


@pytest.fixture(autouse=True)
def reset_pool_sentinel_cache():
    baton._POOL_SENTINEL_CACHE.clear()
    yield
    baton._POOL_SENTINEL_CACHE.clear()


@pytest.fixture(autouse=True)
def ci_always_green(monkeypatch):
    """The CI guard (#974) runs before the approval guard on the same flip
    path; pin it green so these tests isolate the #1002 guard. Test 7
    overrides this locally to verify fail-fast ordering."""
    monkeypatch.setattr(
        baton, "_pr_ci_state", lambda pr_num: ("green", "all checks passed")
    )


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    d = tmp_path / "projects"
    d.mkdir()
    monkeypatch.setattr(baton, "BATON_PROJECTS_DIR", str(d))
    return d


@pytest.fixture
def engram_home(tmp_path, monkeypatch):
    home = tmp_path / "engram"
    home.mkdir()
    config = {"agent_name": "borges", "primary_user": SENTINEL}
    (home / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr(baton, "ENGRAM_HOME", str(home))
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    baton._POOL_SENTINEL_CACHE.clear()
    return home


def _make_baton_file(projects_dir: Path, project_id: str, github: str = "") -> Path:
    extra = f"\ngithub: {github}" if github else ""
    content = (
        "---\n"
        f"project: {project_id}\n"
        "title: Test PR\n"
        "status: in-review\n"
        "turn: borges\n"
        "turn_since: 2026-01-01T00:00:00Z\n"
        'turn_reason: "ready for review"\n'
        "participants: [borges, ariadne]\n"
        f"{extra}\n"
        "---\n\n"
        "# Test\n\n"
        "## Turn log\n\n"
        "- 2026-01-01T00:00:00Z borges → ariadne: initial\n"
    )
    content = content.replace("\n\n---\n", "\n---\n")
    p = projects_dir / f"{project_id}.md"
    p.write_text(content)
    return p


def _make_flip_args(project_id, to, reason, force=False):
    import argparse
    return argparse.Namespace(
        project_id=project_id,
        to=to,
        reason=reason,
        force=force,
    )


def _invoke_flip(project_id, to, reason, force=False):
    args = _make_flip_args(project_id, to, reason, force=force)
    config = {}
    agent_name = "borges"

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    exit_code = 0
    with mock.patch("sys.stdout", stdout_buf), \
         mock.patch("sys.stderr", stderr_buf):
        try:
            baton.cmd_flip(args, config, agent_name)
        except SystemExit as exc:
            exit_code = int(exc.code) if exc.code is not None else 0

    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# Wiring tests (cmd_flip)
# ---------------------------------------------------------------------------

def test_flip_fresh_approval_proceeds(projects_dir, engram_home, monkeypatch):
    _make_baton_file(projects_dir, "PR-200", github="pr/200")
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("fresh", "latest approval covers the tip"),
    )
    code, out, err = _invoke_flip("PR-200", SENTINEL, "ready to merge")
    assert code == 0, f"expected exit 0, got {code}; stderr={err!r}"
    assert "PR-200" in out
    assert err == ""


def test_flip_stale_approval_refuses(projects_dir, engram_home, monkeypatch):
    baton_file = _make_baton_file(projects_dir, "PR-201", github="pr/201")
    original_content = baton_file.read_text()
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("stale", "tip committed 2026-01-02T00:00:00Z is newer "
                                 "than the latest approval 2026-01-01T00:00:00Z"),
    )
    code, out, err = _invoke_flip("PR-201", SENTINEL, "ready to merge")
    assert code == baton.EXIT_VALIDATION, f"expected EXIT_VALIDATION, got {code}"
    assert "tip moved after approval" in err
    assert "201" in err
    assert baton_file.read_text() == original_content, (
        "baton file was mutated despite stale approval"
    )


def test_flip_stale_with_force_proceeds(projects_dir, engram_home, monkeypatch):
    _make_baton_file(projects_dir, "PR-202", github="pr/202")
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("stale", "tip newer than approval"),
    )
    code, out, err = _invoke_flip("PR-202", SENTINEL, "ready to merge", force=True)
    assert code == 0, f"expected exit 0 with --force, got {code}; stderr={err!r}"
    assert "warning" in err.lower()
    assert "post-approval" in err


def test_flip_no_approval_passes_through(projects_dir, engram_home, monkeypatch):
    _make_baton_file(projects_dir, "PR-203", github="pr/203")
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("no_approval", "no APPROVED review on the PR"),
    )
    code, out, err = _invoke_flip("PR-203", SENTINEL, "ready to merge")
    assert code == 0, f"no_approval must pass through, got {code}; stderr={err!r}"
    assert err == "", f"no_approval must be silent, got stderr={err!r}"


def test_flip_unknown_warns_and_proceeds(projects_dir, engram_home, monkeypatch):
    _make_baton_file(projects_dir, "PR-204", github="pr/204")
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("unknown", "gh timed out after 15s"),
    )
    code, out, err = _invoke_flip("PR-204", SENTINEL, "ready to merge")
    assert code == 0, f"unknown must proceed, got {code}; stderr={err!r}"
    assert "could not verify post-approval state" in err


def test_flip_to_non_sentinel_skips_guard(projects_dir, engram_home, monkeypatch):
    _make_baton_file(projects_dir, "PR-205", github="pr/205")

    def _boom(pr_num):
        raise AssertionError("approval guard must not be consulted on non-sentinel flips")

    monkeypatch.setattr(baton, "_pr_approval_state", _boom)
    code, out, err = _invoke_flip("PR-205", "ariadne", "your round")
    assert code == 0, f"non-sentinel flip must skip the guard, got {code}; stderr={err!r}"


def test_red_ci_rejects_before_approval_guard(projects_dir, engram_home, monkeypatch):
    """Fail-fast ordering: a red-CI flip exits at the CI guard; the approval
    guard is never consulted."""
    _make_baton_file(projects_dir, "PR-206", github="pr/206")
    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("red", "check 'tests' conclusion=FAILURE"),
    )

    def _boom(pr_num):
        raise AssertionError("approval guard must not run after a CI rejection")

    monkeypatch.setattr(baton, "_pr_approval_state", _boom)
    code, out, err = _invoke_flip("PR-206", SENTINEL, "ready to merge")
    assert code == baton.EXIT_VALIDATION
    assert "red" in err.lower()


# ---------------------------------------------------------------------------
# _pr_approval_state parsing tests (mocked subprocess — two-call shape)
#
# _pr_approval_state now makes TWO subprocess.run calls:
#   call 1: gh repo view --json nameWithOwner
#   call 2: gh api graphql -f query=...
#
# Helpers below build mock side-effect sequences for the two-call shape.
# ---------------------------------------------------------------------------

_REPO_PAYLOAD = {"nameWithOwner": "engram-agents/engram-alpha"}


def _gh_result(payload, returncode: int = 0, stderr: str = ""):
    """Build a mock subprocess.CompletedProcess-like object."""
    m = mock.Mock()
    m.returncode = returncode
    m.stdout = json.dumps(payload) if payload is not None else ""
    m.stderr = stderr
    return m


def _gql_payload(head_oid: str, review_nodes: list) -> dict:
    """Build the GraphQL response envelope expected by _pr_approval_state."""
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "headRefOid": head_oid,
                    "reviews": {"nodes": review_nodes},
                }
            }
        }
    }


def _patch_two_calls(monkeypatch, gql_payload, *, repo_returncode=0,
                     gql_returncode=0, repo_stderr="", gql_stderr=""):
    """Mock subprocess.run to return repo-view result then graphql result."""
    repo_result = _gh_result(_REPO_PAYLOAD, returncode=repo_returncode,
                             stderr=repo_stderr)
    gql_result = _gh_result(gql_payload, returncode=gql_returncode,
                            stderr=gql_stderr)
    side_effects = [repo_result, gql_result]
    monkeypatch.setattr(
        baton.subprocess, "run",
        mock.Mock(side_effect=side_effects),
    )


def _patch_repo_fail(monkeypatch, returncode=1, stderr="gh: auth error"):
    """Mock subprocess.run so repo-view call fails (only one call made)."""
    repo_result = _gh_result(None, returncode=returncode, stderr=stderr)
    monkeypatch.setattr(
        baton.subprocess, "run",
        mock.Mock(side_effect=[repo_result]),
    )


# --- core verdict tests ---

def test_state_fresh_when_approval_oid_matches_tip(monkeypatch):
    """APPROVED review with commit.oid == headRefOid → fresh."""
    tip = "aabbccdd1122334455667788"
    _patch_two_calls(monkeypatch, _gql_payload(tip, [
        {"state": "APPROVED", "commit": {"oid": tip}},
    ]))
    state, detail = baton._pr_approval_state("1")
    assert state == "fresh"
    assert tip[:8] in detail


def test_state_stale_when_approval_oid_differs(monkeypatch):
    """APPROVED review exists but commit.oid != headRefOid (rebase case)
    — stale even if the approval's committedDate would be OLDER than the tip."""
    tip = "aaaa0000111122223333"
    old_oid = "bbbb9999888877776666"
    _patch_two_calls(monkeypatch, _gql_payload(tip, [
        {"state": "APPROVED", "commit": {"oid": old_oid}},
    ]))
    state, detail = baton._pr_approval_state("1")
    assert state == "stale"
    assert tip[:8] in detail
    assert old_oid[:8] in detail


def test_state_no_approval_without_approved_reviews(monkeypatch):
    """Only COMMENTED / CHANGES_REQUESTED reviews → no_approval."""
    tip = "deadbeef00001111"
    _patch_two_calls(monkeypatch, _gql_payload(tip, [
        {"state": "COMMENTED", "commit": {"oid": "old111"}},
        {"state": "CHANGES_REQUESTED", "commit": {"oid": "old222"}},
    ]))
    state, _ = baton._pr_approval_state("1")
    assert state == "no_approval"


def test_state_dismissed_only_is_no_approval(monkeypatch):
    """DISMISSED reviews only → no_approval (not stale)."""
    tip = "cafebabe12345678"
    _patch_two_calls(monkeypatch, _gql_payload(tip, [
        {"state": "DISMISSED", "commit": {"oid": "old333"}},
    ]))
    state, _ = baton._pr_approval_state("1")
    assert state == "no_approval"


def test_state_fresh_with_commit_null_and_matching_approval(monkeypatch):
    """A review node with commit: null is non-matching but not an error;
    a second node that matches the tip still produces fresh."""
    tip = "1234567890abcdef"
    _patch_two_calls(monkeypatch, _gql_payload(tip, [
        {"state": "APPROVED", "commit": None},
        {"state": "APPROVED", "commit": {"oid": tip}},
    ]))
    state, detail = baton._pr_approval_state("1")
    assert state == "fresh"
    assert tip[:8] in detail


# --- gh failure ladder tests ---

def test_state_unknown_on_gql_failure(monkeypatch):
    """GraphQL call failure → unknown (with gh's stderr text)."""
    _patch_two_calls(monkeypatch, None,
                     gql_returncode=1, gql_stderr="gh: not logged in")
    state, detail = baton._pr_approval_state("1")
    assert state == "unknown"
    assert "not logged in" in detail


def test_state_unknown_on_gql_malformed_json(monkeypatch):
    """GraphQL returns non-JSON → unknown."""
    repo_result = _gh_result(_REPO_PAYLOAD)
    bad_result = mock.Mock(returncode=0, stdout="not json", stderr="")
    monkeypatch.setattr(
        baton.subprocess, "run",
        mock.Mock(side_effect=[repo_result, bad_result]),
    )
    state, _ = baton._pr_approval_state("1")
    assert state == "unknown"


def test_state_unknown_on_repo_view_failure(monkeypatch):
    """repo-view call failure → unknown before GraphQL is attempted."""
    _patch_repo_fail(monkeypatch, returncode=1, stderr="gh: auth error")
    state, detail = baton._pr_approval_state("1")
    assert state == "unknown"
    assert "auth error" in detail


def test_state_unknown_on_empty_gql_response(monkeypatch):
    """GraphQL returns empty body → unknown."""
    repo_result = _gh_result(_REPO_PAYLOAD)
    empty_result = mock.Mock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(
        baton.subprocess, "run",
        mock.Mock(side_effect=[repo_result, empty_result]),
    )
    state, _ = baton._pr_approval_state("1")
    assert state == "unknown"


def test_state_non_numeric_pr_number_is_unknown(monkeypatch):
    """#1007 hardening: pr_number is interpolated into the GraphQL query, so a
    non-numeric value must return unknown without invoking gh at all."""
    def _boom(*args, **kwargs):
        raise AssertionError("gh must not be invoked for a non-numeric PR number")
    monkeypatch.setattr(baton.subprocess, "run", _boom)
    state, detail = baton._pr_approval_state('123") { id } } evil')
    assert state == "unknown"
    assert "not numeric" in detail


def test_state_hash_prefixed_numeric_pr_number_ok(monkeypatch):
    """A '#123'-shaped value is tolerated (stripped), matching CLI ergonomics."""
    calls = []
    def _fake_run(argv, **kwargs):
        calls.append(argv)
        class R:
            returncode = 0
            stderr = ""
            stdout = (
                '{"nameWithOwner": "o/r"}' if argv[:3] == ["gh", "repo", "view"]
                else '{"data": {"repository": {"pullRequest": {"headRefOid": "abc123ff",'
                     ' "reviews": {"nodes": [{"state": "APPROVED", "commit": {"oid": "abc123ff"}}]}}}}}'
            )
        return R()
    monkeypatch.setattr(baton.subprocess, "run", _fake_run)
    state, detail = baton._pr_approval_state("#123")
    assert state == "fresh"
    assert any("123" in " ".join(c) for c in calls if c[:2] == ["gh", "api"])


def test_state_no_approval_empty_review_list(monkeypatch):
    """A PR with zero reviews of any kind is no_approval (reviewer-fairy
    suggestion on PR #1064: pin the empty-list path so a future edit can't
    silently turn it into unknown)."""
    _patch_two_calls(monkeypatch, _gql_payload("abc123ff", []))
    state, detail = baton._pr_approval_state("123")
    assert state == "no_approval"
