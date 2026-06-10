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

Coverage — _pr_approval_state parsing (mocked subprocess):
  8. commit newer than latest approval → stale
  9. approval at/after latest commit → fresh
 10. approvals absent (only COMMENTED/CHANGES_REQUESTED) → no_approval
 11. gh failure / empty / unparseable → unknown
 12. multiple approvals: the LATEST approval is the comparand

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
# _pr_approval_state parsing tests (mocked subprocess)
# ---------------------------------------------------------------------------

def _gh_result(payload: dict, returncode: int = 0, stderr: str = ""):
    m = mock.Mock()
    m.returncode = returncode
    m.stdout = json.dumps(payload) if payload is not None else ""
    m.stderr = stderr
    return m


def _patch_gh(monkeypatch, payload, returncode=0, stderr=""):
    monkeypatch.setattr(
        baton.subprocess, "run",
        lambda *a, **k: _gh_result(payload, returncode, stderr),
    )


def test_state_stale_when_commit_newer(monkeypatch):
    _patch_gh(monkeypatch, {
        "commits": [
            {"committedDate": "2026-01-01T00:00:00Z"},
            {"committedDate": "2026-01-03T00:00:00Z"},
        ],
        "reviews": [
            {"state": "APPROVED", "submittedAt": "2026-01-02T00:00:00Z"},
        ],
    })
    state, detail = baton._pr_approval_state("1")
    assert state == "stale"
    assert "2026-01-03T00:00:00Z" in detail


def test_state_fresh_when_approval_covers_tip(monkeypatch):
    _patch_gh(monkeypatch, {
        "commits": [{"committedDate": "2026-01-01T00:00:00Z"}],
        "reviews": [
            {"state": "APPROVED", "submittedAt": "2026-01-02T00:00:00Z"},
        ],
    })
    state, _ = baton._pr_approval_state("1")
    assert state == "fresh"


def test_state_no_approval_without_approved_reviews(monkeypatch):
    _patch_gh(monkeypatch, {
        "commits": [{"committedDate": "2026-01-01T00:00:00Z"}],
        "reviews": [
            {"state": "COMMENTED", "submittedAt": "2026-01-02T00:00:00Z"},
            {"state": "CHANGES_REQUESTED", "submittedAt": "2026-01-03T00:00:00Z"},
        ],
    })
    state, _ = baton._pr_approval_state("1")
    assert state == "no_approval"


def test_state_latest_approval_is_comparand(monkeypatch):
    """An old stale approval must not shadow a newer one that covers the tip."""
    _patch_gh(monkeypatch, {
        "commits": [{"committedDate": "2026-01-02T00:00:00Z"}],
        "reviews": [
            {"state": "APPROVED", "submittedAt": "2026-01-01T00:00:00Z"},
            {"state": "APPROVED", "submittedAt": "2026-01-03T00:00:00Z"},
        ],
    })
    state, _ = baton._pr_approval_state("1")
    assert state == "fresh"


def test_state_unknown_on_gh_failure(monkeypatch):
    _patch_gh(monkeypatch, None, returncode=1, stderr="gh: not logged in")
    state, detail = baton._pr_approval_state("1")
    assert state == "unknown"
    assert "not logged in" in detail


def test_state_unknown_on_empty_or_malformed(monkeypatch):
    _patch_gh(monkeypatch, {"commits": [], "reviews": []})
    state, _ = baton._pr_approval_state("1")
    assert state == "unknown"

    monkeypatch.setattr(
        baton.subprocess, "run",
        lambda *a, **k: mock.Mock(returncode=0, stdout="not json", stderr=""),
    )
    state, _ = baton._pr_approval_state("1")
    assert state == "unknown"
