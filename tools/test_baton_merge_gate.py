"""Tests for the `baton merge` gate ladder (issues #999 + #1000).

Companion to test_baton_ci_guard.py and test_baton_approval_guard.py.
This file tests the new `cmd_merge` subcommand end-to-end.

Coverage:
  1.  no baton → refuse (gate 1), no gh call
  2.  baton turn=author → refuse names holder (gate 2), no gh call
  3.  turn=sentinel + green + fresh → gh merge called with --squash; baton closed
  4.  red CI → refuse (gate 3); pending → refuse (gate 3)
  5.  --force → proceeds with forced log (gates 3-4 skipped)
  6.  stale approval → refuse (gate 4)
  7.  no_approval → proceeds (colleague-layer jurisdiction, passes through)
  8.  --force does NOT skip gates 1-2
  9.  --dry-run → full verdict printed, gh merge NOT called
 10.  gh merge subprocess failure → nonzero exit, baton state unchanged
 11.  turn=sentinel + unknown CI → warning + proceeds
 12.  turn=sentinel + unknown approval → warning + proceeds
 13.  PR-N and bare N resolution
 14.  already-merged baton → refuse (already closed)

All tests are hermetic: no real `gh` calls, no real baton files outside tmp_path.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).parent
_ROOT = _THIS_DIR.parent

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.baton as baton  # noqa: E402


SENTINEL = "lei"  # pool sentinel for all tests


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_pool_sentinel_cache():
    """Clear _POOL_SENTINEL_CACHE between tests."""
    baton._POOL_SENTINEL_CACHE.clear()
    yield
    baton._POOL_SENTINEL_CACHE.clear()


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    """Temporary baton projects directory wired into the module."""
    d = tmp_path / "projects"
    d.mkdir()
    monkeypatch.setattr(baton, "BATON_PROJECTS_DIR", str(d))
    return d


@pytest.fixture
def engram_home(tmp_path, monkeypatch):
    """Temp ENGRAM_HOME with primary_user=SENTINEL; wires _pool_sentinel()."""
    home = tmp_path / "engram"
    home.mkdir()
    config = {"agent_name": "borges", "primary_user": SENTINEL}
    (home / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr(baton, "ENGRAM_HOME", str(home))
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    baton._POOL_SENTINEL_CACHE.clear()
    return home


@pytest.fixture(autouse=True)
def ci_green(monkeypatch):
    """Default CI state to green; individual tests override as needed."""
    monkeypatch.setattr(
        baton, "_pr_ci_state", lambda pr_num: ("green", "all checks passed")
    )


@pytest.fixture(autouse=True)
def approval_fresh(monkeypatch):
    """Default approval state to fresh; individual tests override as needed."""
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("fresh", "approval covers tip oid abc12345"),
    )


def _make_baton_file(
    projects_dir: Path,
    project_id: str,
    turn: str = SENTINEL,
    status: str = "in-review",
    github: str = "pr/500",
) -> Path:
    """Write a minimal baton file for merge tests."""
    github_line = f"github: {github}\n" if github else ""
    content = (
        "---\n"
        f"project: {project_id}\n"
        "title: Test PR\n"
        f"status: {status}\n"
        f"turn: {turn}\n"
        "turn_since: 2026-01-01T00:00:00Z\n"
        f'turn_reason: "colleague-APPROVED; ready for merge"\n'
        "participants: [borges, ariadne]\n"
        f"{github_line}"
        "---\n\n"
        "# Test\n\n"
        "## Turn log\n\n"
        f"- 2026-01-01T00:00:00Z borges → {turn}: ready\n"
    )
    p = projects_dir / f"{project_id}.md"
    p.write_text(content)
    return p


def _make_merge_args(pr: str, force: bool = False, dry_run: bool = False):
    """Build a minimal argparse.Namespace for cmd_merge."""
    return argparse.Namespace(pr=pr, force=force, dry_run=dry_run)


def _invoke_merge(pr: str, force: bool = False, dry_run: bool = False):
    """Run cmd_merge, capture I/O; return (exit_code, stdout, stderr)."""
    args = _make_merge_args(pr, force=force, dry_run=dry_run)
    config = {}
    agent_name = "borges"

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    exit_code = 0

    with mock.patch("sys.stdout", stdout_buf), \
         mock.patch("sys.stderr", stderr_buf):
        try:
            baton.cmd_merge(args, config, agent_name)
        except SystemExit as exc:
            exit_code = int(exc.code) if exc.code is not None else 0

    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# Test 1: no baton → gate 1 refuses, no gh call
# ---------------------------------------------------------------------------

def test_merge_no_baton_refuses(projects_dir, engram_home):
    """Missing baton file → gate 1 refuses."""
    gh_called = []
    with mock.patch("subprocess.run", side_effect=lambda *a, **kw: gh_called.append(a)):
        code, out, err = _invoke_merge("PR-500")

    assert code == baton.EXIT_VALIDATION, f"expected EXIT_VALIDATION, got {code}"
    assert "no baton" in err.lower() or "not merge-ready" in err.lower(), \
        f"expected 'no baton' refusal in stderr, got: {err!r}"
    assert not gh_called, "gh should not be called when baton is missing"


# ---------------------------------------------------------------------------
# Test 2: baton turn=author → gate 2 refuses, names holder
# ---------------------------------------------------------------------------

def test_merge_turn_not_sentinel_refuses(projects_dir, engram_home):
    """Baton turn held by author → gate 2 refuses and names the holder."""
    _make_baton_file(projects_dir, "PR-501", turn="borges")

    gh_called = []
    with mock.patch("subprocess.run", side_effect=lambda *a, **kw: gh_called.append(a)):
        code, out, err = _invoke_merge("PR-501")

    assert code == baton.EXIT_VALIDATION, f"expected EXIT_VALIDATION, got {code}"
    assert "borges" in err, f"expected holder name in stderr, got: {err!r}"
    assert "not presented for merge" in err or "turn is with" in err, \
        f"expected refusal message, got: {err!r}"
    assert not gh_called, "gh should not be called when turn is wrong"


# ---------------------------------------------------------------------------
# Test 3: turn=sentinel + green + fresh → merge succeeds, baton closed
# ---------------------------------------------------------------------------

def test_merge_happy_path(projects_dir, engram_home, monkeypatch):
    """All gates pass → gh called with --squash; baton status becomes merged."""
    baton_file = _make_baton_file(projects_dir, "PR-502", turn=SENTINEL)

    # Simulate successful gh pr merge
    fake_merge = mock.MagicMock()
    fake_merge.returncode = 0
    fake_merge.stdout = ""
    fake_merge.stderr = ""

    with mock.patch("subprocess.run", return_value=fake_merge) as mock_run:
        code, out, err = _invoke_merge("PR-502")

    assert code == 0, f"expected exit 0, got {code}; stderr={err!r}"

    # Verify gh was called with --squash
    call_args = mock_run.call_args
    cmd = call_args[0][0]
    assert "gh" in cmd
    assert "pr" in cmd
    assert "merge" in cmd
    assert "502" in cmd
    assert "--squash" in cmd

    # Verify baton is closed
    final_text = baton_file.read_text()
    assert "status: merged" in final_text, \
        f"expected status: merged in baton after merge, got:\n{final_text}"

    # Verify turn-log entry was appended
    assert "merged via baton merge" in final_text, \
        f"expected turn-log entry in baton, got:\n{final_text}"


# ---------------------------------------------------------------------------
# Test 4a: red CI → refuse
# ---------------------------------------------------------------------------

def test_merge_red_ci_refuses(projects_dir, engram_home, monkeypatch):
    """Red CI → gate 3 refuses; baton unchanged."""
    baton_file = _make_baton_file(projects_dir, "PR-503", turn=SENTINEL)
    original = baton_file.read_text()

    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("red", "check 'lint' conclusion=FAILURE"),
    )

    gh_called = []
    with mock.patch("subprocess.run", side_effect=lambda *a, **kw: gh_called.append(a)):
        code, out, err = _invoke_merge("PR-503")

    assert code == baton.EXIT_VALIDATION, f"expected EXIT_VALIDATION, got {code}"
    assert "red" in err.lower(), f"expected 'red' in stderr, got: {err!r}"
    assert baton_file.read_text() == original, "baton must be unchanged on CI refusal"
    assert not gh_called, "gh should not be called on CI red"


# ---------------------------------------------------------------------------
# Test 4b: pending CI → refuse
# ---------------------------------------------------------------------------

def test_merge_pending_ci_refuses(projects_dir, engram_home, monkeypatch):
    """Pending CI → gate 3 refuses; baton unchanged."""
    baton_file = _make_baton_file(projects_dir, "PR-504", turn=SENTINEL)
    original = baton_file.read_text()

    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("pending", "check 'tests' status=IN_PROGRESS"),
    )

    gh_called = []
    with mock.patch("subprocess.run", side_effect=lambda *a, **kw: gh_called.append(a)):
        code, out, err = _invoke_merge("PR-504")

    assert code == baton.EXIT_VALIDATION, f"expected EXIT_VALIDATION, got {code}"
    assert "pending" in err.lower(), f"expected 'pending' in stderr, got: {err!r}"
    assert baton_file.read_text() == original, "baton must be unchanged on CI pending"
    assert not gh_called


# ---------------------------------------------------------------------------
# Test 5: --force → proceeds despite red CI, forced log entry
# ---------------------------------------------------------------------------

def test_merge_force_skips_ci_and_approval(projects_dir, engram_home, monkeypatch):
    """--force skips gates 3+4; merge proceeds; log says FORCED."""
    _make_baton_file(projects_dir, "PR-505", turn=SENTINEL)

    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("red", "check 'lint' conclusion=FAILURE"),
    )
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("stale", "tip oid abc not covered by approval"),
    )

    fake_merge = mock.MagicMock()
    fake_merge.returncode = 0
    fake_merge.stdout = ""
    fake_merge.stderr = ""

    with mock.patch("subprocess.run", return_value=fake_merge):
        code, out, err = _invoke_merge("PR-505", force=True)

    assert code == 0, f"expected exit 0 with --force, got {code}; stderr={err!r}"
    assert "--force" in err, f"expected --force warning in stderr, got: {err!r}"

    # Verify the baton log entry says FORCED
    baton_text = (Path(baton.BATON_PROJECTS_DIR) / "PR-505.md").read_text()
    assert "FORCED" in baton_text, \
        f"expected FORCED in turn-log when --force used, got:\n{baton_text}"


# ---------------------------------------------------------------------------
# Test 6: stale approval → refuse (gate 4)
# ---------------------------------------------------------------------------

def test_merge_stale_approval_refuses(projects_dir, engram_home, monkeypatch):
    """Stale approval → gate 4 refuses; baton unchanged."""
    baton_file = _make_baton_file(projects_dir, "PR-506", turn=SENTINEL)
    original = baton_file.read_text()

    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("stale", "tip oid abc1234 not covered by any approval"),
    )

    gh_called = []
    with mock.patch("subprocess.run", side_effect=lambda *a, **kw: gh_called.append(a)):
        code, out, err = _invoke_merge("PR-506")

    assert code == baton.EXIT_VALIDATION, f"expected EXIT_VALIDATION, got {code}"
    assert "stale" in err.lower() or "tip moved" in err.lower() or "approval" in err.lower(), \
        f"expected stale-approval refusal in stderr, got: {err!r}"
    assert baton_file.read_text() == original, "baton must be unchanged on stale approval"
    assert not gh_called


# ---------------------------------------------------------------------------
# Test 7: no_approval → passes through (colleague-layer jurisdiction)
# ---------------------------------------------------------------------------

def test_merge_no_approval_proceeds(projects_dir, engram_home, monkeypatch):
    """no_approval passes through gate 4; merge proceeds."""
    _make_baton_file(projects_dir, "PR-507", turn=SENTINEL)

    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("no_approval", "no APPROVED review on the PR"),
    )

    fake_merge = mock.MagicMock()
    fake_merge.returncode = 0
    fake_merge.stdout = ""
    fake_merge.stderr = ""

    with mock.patch("subprocess.run", return_value=fake_merge):
        code, out, err = _invoke_merge("PR-507")

    assert code == 0, \
        f"expected exit 0 for no_approval (passes through), got {code}; stderr={err!r}"


# ---------------------------------------------------------------------------
# Test 8: --force does NOT skip gates 1-2
# ---------------------------------------------------------------------------

def test_merge_force_does_not_skip_gate1(projects_dir, engram_home):
    """--force cannot bypass a missing baton (gate 1)."""
    gh_called = []
    with mock.patch("subprocess.run", side_effect=lambda *a, **kw: gh_called.append(a)):
        code, out, err = _invoke_merge("PR-508", force=True)

    assert code == baton.EXIT_VALIDATION, \
        f"expected EXIT_VALIDATION even with --force when baton missing, got {code}"
    assert not gh_called


def test_merge_force_does_not_skip_gate2(projects_dir, engram_home):
    """--force cannot bypass a wrong turn (gate 2)."""
    _make_baton_file(projects_dir, "PR-509", turn="borges")  # not sentinel

    gh_called = []
    with mock.patch("subprocess.run", side_effect=lambda *a, **kw: gh_called.append(a)):
        code, out, err = _invoke_merge("PR-509", force=True)

    assert code == baton.EXIT_VALIDATION, \
        f"expected EXIT_VALIDATION even with --force when turn is wrong, got {code}"
    assert not gh_called


# ---------------------------------------------------------------------------
# Test 9: --dry-run → prints verdict, no gh call
# ---------------------------------------------------------------------------

def test_merge_dry_run(projects_dir, engram_home):
    """--dry-run evaluates all gates and prints verdict; no merge performed."""
    baton_file = _make_baton_file(projects_dir, "PR-510", turn=SENTINEL)
    original = baton_file.read_text()

    gh_called = []
    with mock.patch("subprocess.run", side_effect=lambda *a, **kw: gh_called.append(a)):
        code, out, err = _invoke_merge("PR-510", dry_run=True)

    assert code == 0, f"expected exit 0 for --dry-run, got {code}; stderr={err!r}"
    assert "dry-run" in out.lower() or "dry_run" in out.lower() or "DRY-RUN" in out, \
        f"expected dry-run output, got: {out!r}"
    # Gates should appear in output
    assert "gate 1" in out.lower() or "baton exists" in out.lower(), \
        f"expected gate verdict in dry-run output, got: {out!r}"
    # Baton unchanged
    assert baton_file.read_text() == original, "baton must be unchanged on --dry-run"
    assert not gh_called, "gh should not be called on --dry-run"


# ---------------------------------------------------------------------------
# Test 10: gh merge subprocess failure → nonzero exit, baton unchanged
# ---------------------------------------------------------------------------

def test_merge_gh_failure_baton_unchanged(projects_dir, engram_home):
    """gh pr merge nonzero exit → EXIT_IO, baton state preserved."""
    baton_file = _make_baton_file(projects_dir, "PR-511", turn=SENTINEL)
    original = baton_file.read_text()

    fake_fail = mock.MagicMock()
    fake_fail.returncode = 1
    fake_fail.stdout = ""
    fake_fail.stderr = "PR is already merged"

    with mock.patch("subprocess.run", return_value=fake_fail):
        code, out, err = _invoke_merge("PR-511")

    assert code == baton.EXIT_IO, \
        f"expected EXIT_IO on gh failure, got {code}; stderr={err!r}"
    assert baton_file.read_text() == original, \
        "baton must be unchanged when gh pr merge fails"


def test_merge_gh_not_found_baton_unchanged(projects_dir, engram_home):
    """gh not on PATH → EXIT_IO, baton state preserved."""
    baton_file = _make_baton_file(projects_dir, "PR-512", turn=SENTINEL)
    original = baton_file.read_text()

    with mock.patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
        code, out, err = _invoke_merge("PR-512")

    assert code == baton.EXIT_IO, \
        f"expected EXIT_IO when gh missing, got {code}; stderr={err!r}"
    assert baton_file.read_text() == original


def test_merge_gh_timeout_baton_unchanged(projects_dir, engram_home):
    """gh timeout → EXIT_IO, baton state preserved."""
    import subprocess as _sp
    baton_file = _make_baton_file(projects_dir, "PR-513", turn=SENTINEL)
    original = baton_file.read_text()

    with mock.patch(
        "subprocess.run",
        side_effect=_sp.TimeoutExpired(cmd="gh", timeout=60),
    ):
        code, out, err = _invoke_merge("PR-513")

    assert code == baton.EXIT_IO, \
        f"expected EXIT_IO on timeout, got {code}; stderr={err!r}"
    assert baton_file.read_text() == original


# ---------------------------------------------------------------------------
# Test 11: unknown CI → warning, proceeds
# ---------------------------------------------------------------------------

def test_merge_unknown_ci_warns_and_proceeds(projects_dir, engram_home, monkeypatch):
    """Unknown CI state → advisory warning, merge proceeds."""
    _make_baton_file(projects_dir, "PR-514", turn=SENTINEL)

    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("unknown", "gh not found on PATH"),
    )

    fake_merge = mock.MagicMock()
    fake_merge.returncode = 0
    fake_merge.stdout = ""
    fake_merge.stderr = ""

    with mock.patch("subprocess.run", return_value=fake_merge):
        code, out, err = _invoke_merge("PR-514")

    assert code == 0, f"expected exit 0 on unknown CI, got {code}; stderr={err!r}"
    assert "warning" in err.lower() or "could not verify" in err.lower(), \
        f"expected advisory warning in stderr, got: {err!r}"


# ---------------------------------------------------------------------------
# Test 12: unknown approval → warning, proceeds
# ---------------------------------------------------------------------------

def test_merge_unknown_approval_warns_and_proceeds(projects_dir, engram_home, monkeypatch):
    """Unknown approval state → advisory warning, merge proceeds."""
    _make_baton_file(projects_dir, "PR-515", turn=SENTINEL)

    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("unknown", "gh api error"),
    )

    fake_merge = mock.MagicMock()
    fake_merge.returncode = 0
    fake_merge.stdout = ""
    fake_merge.stderr = ""

    with mock.patch("subprocess.run", return_value=fake_merge):
        code, out, err = _invoke_merge("PR-515")

    assert code == 0, f"expected exit 0 on unknown approval, got {code}; stderr={err!r}"
    assert "warning" in err.lower() or "could not verify" in err.lower(), \
        f"expected advisory warning in stderr, got: {err!r}"


# ---------------------------------------------------------------------------
# Test 13: PR-N and bare N input resolution
# ---------------------------------------------------------------------------

def test_merge_pr_prefix_resolution(projects_dir, engram_home):
    """PR-N input resolves to project ID PR-N."""
    _make_baton_file(projects_dir, "PR-516", turn=SENTINEL)

    fake_merge = mock.MagicMock()
    fake_merge.returncode = 0
    fake_merge.stdout = ""
    fake_merge.stderr = ""

    with mock.patch("subprocess.run", return_value=fake_merge) as mock_run:
        code, out, err = _invoke_merge("PR-516")

    assert code == 0, f"expected exit 0, got {code}; stderr={err!r}"
    cmd = mock_run.call_args[0][0]
    assert "516" in cmd, f"expected PR number 516 in gh call, got cmd={cmd}"


def test_merge_bare_number_resolution(projects_dir, engram_home):
    """Bare number input resolves to project ID PR-N."""
    _make_baton_file(projects_dir, "PR-517", turn=SENTINEL)

    fake_merge = mock.MagicMock()
    fake_merge.returncode = 0
    fake_merge.stdout = ""
    fake_merge.stderr = ""

    with mock.patch("subprocess.run", return_value=fake_merge) as mock_run:
        code, out, err = _invoke_merge("517")  # bare number

    assert code == 0, f"expected exit 0 for bare number input, got {code}; stderr={err!r}"
    cmd = mock_run.call_args[0][0]
    assert "517" in cmd, f"expected PR number 517 in gh call, got cmd={cmd}"


# ---------------------------------------------------------------------------
# Test 14: already-merged baton → refuse (already closed)
# ---------------------------------------------------------------------------

def test_merge_already_merged_refuses(projects_dir, engram_home):
    """Baton already status:merged → refuse as already closed."""
    _make_baton_file(projects_dir, "PR-518", turn=SENTINEL, status="merged")

    gh_called = []
    with mock.patch("subprocess.run", side_effect=lambda *a, **kw: gh_called.append(a)):
        code, out, err = _invoke_merge("PR-518")

    assert code == baton.EXIT_VALIDATION, \
        f"expected EXIT_VALIDATION for already-merged baton, got {code}"
    assert "merged" in err.lower() or "already" in err.lower(), \
        f"expected already-merged message in stderr, got: {err!r}"
    assert not gh_called


def test_merge_cancelled_baton_refuses(projects_dir, engram_home):
    """Baton status:cancelled → refuse."""
    _make_baton_file(projects_dir, "PR-519", turn=SENTINEL, status="cancelled")

    gh_called = []
    with mock.patch("subprocess.run", side_effect=lambda *a, **kw: gh_called.append(a)):
        code, out, err = _invoke_merge("PR-519")

    assert code == baton.EXIT_VALIDATION, \
        f"expected EXIT_VALIDATION for cancelled baton, got {code}"
    assert not gh_called


# ---------------------------------------------------------------------------
# Round 3: review folds
# ---------------------------------------------------------------------------

def test_merge_hash_prefixed_number_accepted(projects_dir, engram_home):
    """'#N' input form resolves to the same baton as bare N."""
    _make_baton_file(projects_dir, "PR-777")
    fake_merge = mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch("subprocess.run", return_value=fake_merge) as mock_run:
        exit_code, out, err = _invoke_merge("#777")
    assert exit_code == 0, err
    assert mock_run.called


def test_merge_force_logs_actual_states(projects_dir, engram_home, monkeypatch):
    """--force still queries CI/approval and logs the actual bypassed state
    (audit-trail parity with cmd_flip --force)."""
    _make_baton_file(projects_dir, "PR-778")
    monkeypatch.setattr(baton, "_pr_ci_state", lambda n: ("red", "lint FAILURE"))
    monkeypatch.setattr(baton, "_pr_approval_state", lambda n: ("stale", "tip oid mismatch"))
    fake_merge = mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch("subprocess.run", return_value=fake_merge):
        exit_code, out, err = _invoke_merge("778", force=True)
    assert exit_code == 0, err
    assert "actual: red" in err
    assert "actual: stale" in err
