"""Tests for the CI-green guard in tools/baton.py (issue #685).

Coverage:
  1. pr/N anchor + flip to pool-sentinel + green CI    → flip proceeds
  2. pr/N anchor + flip to pool-sentinel + red CI      → refuses (EXIT_VALIDATION), no file mutation
  3. pr/N anchor + flip to pool-sentinel + pending CI  → refuses (EXIT_VALIDATION), no file mutation
  4. pr/N anchor + flip to pool-sentinel + red + force → proceeds (with warning on stderr)
  4b. pr/N anchor + flip to pool-sentinel + pending + force → proceeds (with warning on stderr)
  5. gh missing / unknown verdict                      → proceeds (advisory warning, no exit)
  6. pr/N anchor + flip to NON-sentinel participant    → NO check (proceeds even if red)
  7. project/N anchor + flip to pool-sentinel          → NO check (proceeds)

All tests are hermetic: no real `gh` calls, no real baton files outside tmp_path.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Locate and import the module under test
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent
_ROOT = _THIS_DIR.parent  # repo root (worktree root)

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.baton as baton  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SENTINEL = "maintainer"  # generic pool-sentinel name used in all tests


@pytest.fixture(autouse=True)
def reset_pool_sentinel_cache():
    """Clear _POOL_SENTINEL_CACHE between tests so sentinel is re-resolved each time."""
    baton._POOL_SENTINEL_CACHE.clear()
    yield
    baton._POOL_SENTINEL_CACHE.clear()


@pytest.fixture(autouse=True)
def approval_always_fresh(monkeypatch):
    """The post-approval guard (#1002) now runs on the same sentinel-flip path
    after the CI guard; pin it fresh so this file keeps isolating the #685/#974
    CI guard (and stays hermetic — no real gh call from the new helper).
    The #1002 guard has its own suite: test_baton_approval_guard.py."""
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("fresh", "latest approval covers the tip"),
    )


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    """Temporary baton projects directory; patched into the module."""
    d = tmp_path / "projects"
    d.mkdir()
    monkeypatch.setattr(baton, "BATON_PROJECTS_DIR", str(d))
    return d


@pytest.fixture
def engram_home(tmp_path, monkeypatch):
    """Temp ENGRAM_HOME with primary_user=SENTINEL; patches both the module attr
    and the ENGRAM_HOME env var so _pool_sentinel() resolves correctly."""
    home = tmp_path / "engram"
    home.mkdir()
    config = {"agent_name": "borges", "primary_user": SENTINEL}
    (home / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr(baton, "ENGRAM_HOME", str(home))
    # _pool_sentinel() reads os.environ, not the module attribute
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    # clear cache so the next call picks up the new config
    baton._POOL_SENTINEL_CACHE.clear()
    return home


def _make_baton_file(projects_dir: Path, project_id: str, github: str = "") -> Path:
    """Write a minimal baton file for tests."""
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
    # Strip extra blank line when no github anchor
    content = content.replace("\n\n---\n", "\n---\n")
    p = projects_dir / f"{project_id}.md"
    p.write_text(content)
    return p


def _make_flip_args(project_id, to, reason, force=False):
    """Build a minimal argparse.Namespace for cmd_flip."""
    import argparse
    return argparse.Namespace(
        project_id=project_id,
        to=to,
        reason=reason,
        force=force,
    )


# ---------------------------------------------------------------------------
# Helper: run cmd_flip, capture stdout/stderr, return (exit_code, out, err)
# ---------------------------------------------------------------------------

def _invoke_flip(project_id, to, reason, force=False):
    """Invoke cmd_flip, capture I/O, return (exit_code, stdout_str, stderr_str)."""
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
# Test 1: green CI → flip proceeds
# ---------------------------------------------------------------------------

def test_flip_green_proceeds(projects_dir, engram_home, monkeypatch):
    _make_baton_file(projects_dir, "PR-100", github="pr/100")

    monkeypatch.setattr(baton, "_pr_ci_state", lambda pr_num: ("green", "all checks passed"))

    code, out, err = _invoke_flip("PR-100", SENTINEL, "ready to merge")
    assert code == 0, f"expected exit 0, got {code}; stderr={err!r}"
    assert "PR-100" in out
    assert err == ""


# ---------------------------------------------------------------------------
# Test 2: red CI → refuses, no file mutation
# ---------------------------------------------------------------------------

def test_flip_red_refuses(projects_dir, engram_home, monkeypatch):
    baton_file = _make_baton_file(projects_dir, "PR-101", github="pr/101")
    original_content = baton_file.read_text()

    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("red", "check 'lint' conclusion=FAILURE"),
    )

    code, out, err = _invoke_flip("PR-101", SENTINEL, "ready to merge")
    assert code == baton.EXIT_VALIDATION, f"expected EXIT_VALIDATION, got {code}"
    assert "red" in err.lower()
    assert "101" in err
    # File must be unchanged
    assert baton_file.read_text() == original_content, "baton file was mutated despite red CI"


# ---------------------------------------------------------------------------
# Test 3: pending CI → refuses, no file mutation
# ---------------------------------------------------------------------------

def test_flip_pending_refuses(projects_dir, engram_home, monkeypatch):
    baton_file = _make_baton_file(projects_dir, "PR-102", github="pr/102")
    original_content = baton_file.read_text()

    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("pending", "check 'tests' status=IN_PROGRESS"),
    )

    code, out, err = _invoke_flip("PR-102", SENTINEL, "ready to merge")
    assert code == baton.EXIT_VALIDATION, f"expected EXIT_VALIDATION, got {code}"
    assert "pending" in err.lower()
    assert baton_file.read_text() == original_content, "baton file was mutated despite pending CI"


# ---------------------------------------------------------------------------
# Test 4: red + --force → proceeds, warning on stderr
# ---------------------------------------------------------------------------

def test_flip_red_force_proceeds(projects_dir, engram_home, monkeypatch):
    _make_baton_file(projects_dir, "PR-103", github="pr/103")

    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("red", "check 'flaky-test' conclusion=FAILURE"),
    )

    code, out, err = _invoke_flip("PR-103", SENTINEL, "override flaky", force=True)
    assert code == 0, f"expected exit 0 with --force, got {code}; stderr={err!r}"
    assert "--force" in err, f"expected --force warning in stderr, got: {err!r}"
    assert "red" in err.lower()


def test_flip_pending_force_proceeds(projects_dir, engram_home, monkeypatch):
    # pending + --force takes the same override branch as red + --force;
    # cover it explicitly so the branch isn't only exercised via 'red'.
    _make_baton_file(projects_dir, "PR-108", github="pr/108")

    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("pending", "check 'integration' status=IN_PROGRESS"),
    )

    code, out, err = _invoke_flip("PR-108", SENTINEL, "override pending", force=True)
    assert code == 0, f"expected exit 0 with --force, got {code}; stderr={err!r}"
    assert "--force" in err, f"expected --force warning in stderr, got: {err!r}"
    assert "pending" in err.lower()


# ---------------------------------------------------------------------------
# Test 5: gh missing / unknown verdict → advisory warning, proceeds
# ---------------------------------------------------------------------------

def test_flip_gh_missing_proceeds(projects_dir, engram_home, monkeypatch):
    _make_baton_file(projects_dir, "PR-104", github="pr/104")

    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("unknown", "gh not found on PATH"),
    )

    code, out, err = _invoke_flip("PR-104", SENTINEL, "ready to merge")
    assert code == 0, f"expected exit 0 on unknown/gh-missing, got {code}; stderr={err!r}"
    assert "warning" in err.lower() or "could not verify" in err.lower(), \
        f"expected advisory warning in stderr, got: {err!r}"


# ---------------------------------------------------------------------------
# Test 6: pr/N anchor + flip to NON-sentinel participant → no CI check
# ---------------------------------------------------------------------------

def test_flip_to_non_sentinel_no_check(projects_dir, engram_home, monkeypatch):
    _make_baton_file(projects_dir, "PR-105", github="pr/105")

    # If _pr_ci_state were called, it would return red and refuse.
    # The guard must NOT call it for a non-sentinel flip.
    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("red", "should never be called"),
    )

    # Flip to "ariadne" (a participant, not the sentinel)
    code, out, err = _invoke_flip("PR-105", "ariadne", "passing to reviewer")
    assert code == 0, f"expected exit 0 for non-sentinel flip, got {code}; stderr={err!r}"
    assert "red" not in err.lower(), f"unexpected CI refusal for non-sentinel flip: {err!r}"


# ---------------------------------------------------------------------------
# Test 7: project/N anchor + flip to pool-sentinel → no CI check
# ---------------------------------------------------------------------------

def test_flip_project_anchor_no_check(projects_dir, engram_home, monkeypatch):
    _make_baton_file(projects_dir, "PR-106", github="project/4")

    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("red", "should never be called"),
    )

    code, out, err = _invoke_flip("PR-106", SENTINEL, "handing to pool")
    assert code == 0, f"expected exit 0 for project/ anchor, got {code}; stderr={err!r}"
    assert "red" not in err.lower(), f"unexpected CI refusal for project/ anchor: {err!r}"


# ---------------------------------------------------------------------------
# Test 8: no github anchor → no CI check
# ---------------------------------------------------------------------------

def test_flip_no_anchor_no_check(projects_dir, engram_home, monkeypatch):
    _make_baton_file(projects_dir, "PR-107")  # no github anchor

    monkeypatch.setattr(
        baton, "_pr_ci_state",
        lambda pr_num: ("red", "should never be called"),
    )

    code, out, err = _invoke_flip("PR-107", SENTINEL, "handing to pool")
    assert code == 0, f"expected exit 0 for no anchor, got {code}; stderr={err!r}"
    assert "red" not in err.lower(), f"unexpected CI refusal for no anchor: {err!r}"


# ---------------------------------------------------------------------------
# Tests for _pr_ci_state directly (unit tests for the helper)
# ---------------------------------------------------------------------------

def test_pr_ci_state_green_empty_checks(monkeypatch):
    """Empty statusCheckRollup → green."""
    fake = mock.MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({"statusCheckRollup": [], "mergeStateStatus": "CLEAN"})
    fake.stderr = ""

    with mock.patch("subprocess.run", return_value=fake):
        state, detail = baton._pr_ci_state("42")
    assert state == "green"


def test_pr_ci_state_green_all_success(monkeypatch):
    """All SUCCESS conclusions → green."""
    fake = mock.MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({
        "statusCheckRollup": [
            {"name": "lint", "conclusion": "SUCCESS", "status": "COMPLETED"},
            {"name": "tests", "conclusion": "NEUTRAL", "status": "COMPLETED"},
        ],
        "mergeStateStatus": "CLEAN",
    })
    fake.stderr = ""

    with mock.patch("subprocess.run", return_value=fake):
        state, detail = baton._pr_ci_state("42")
    assert state == "green"


def test_pr_ci_state_red_failure_conclusion(monkeypatch):
    """A FAILURE conclusion → red."""
    fake = mock.MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({
        "statusCheckRollup": [
            {"name": "tests", "conclusion": "FAILURE", "status": "COMPLETED"},
        ],
        "mergeStateStatus": "DIRTY",
    })
    fake.stderr = ""

    with mock.patch("subprocess.run", return_value=fake):
        state, detail = baton._pr_ci_state("42")
    assert state == "red"
    assert "tests" in detail


def test_pr_ci_state_pending_in_progress(monkeypatch):
    """IN_PROGRESS status → pending."""
    fake = mock.MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({
        "statusCheckRollup": [
            {"name": "build", "conclusion": None, "status": "IN_PROGRESS"},
        ],
        "mergeStateStatus": "UNKNOWN",
    })
    fake.stderr = ""

    with mock.patch("subprocess.run", return_value=fake):
        state, detail = baton._pr_ci_state("42")
    assert state == "pending"
    assert "build" in detail


def test_pr_ci_state_gh_not_found():
    """FileNotFoundError (gh not on PATH) → unknown."""
    with mock.patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
        state, detail = baton._pr_ci_state("42")
    assert state == "unknown"
    assert "PATH" in detail or "not found" in detail.lower()


def test_pr_ci_state_nonzero_exit():
    """Non-zero exit from gh → unknown."""
    fake = mock.MagicMock()
    fake.returncode = 1
    fake.stdout = ""
    fake.stderr = "no repo found in cwd"

    with mock.patch("subprocess.run", return_value=fake):
        state, detail = baton._pr_ci_state("42")
    assert state == "unknown"


def test_pr_ci_state_timeout():
    """Timeout → unknown."""
    import subprocess as _sp
    with mock.patch("subprocess.run", side_effect=_sp.TimeoutExpired(cmd="gh", timeout=15)):
        state, detail = baton._pr_ci_state("42")
    assert state == "unknown"
    assert "timed out" in detail.lower()


def test_pr_ci_state_bad_json():
    """Unparseable JSON → unknown."""
    fake = mock.MagicMock()
    fake.returncode = 0
    fake.stdout = "not json at all {"
    fake.stderr = ""

    with mock.patch("subprocess.run", return_value=fake):
        state, detail = baton._pr_ci_state("42")
    assert state == "unknown"


def test_pr_ci_state_status_context_shape():
    """Status-context shape (state key instead of conclusion) → handled."""
    fake = mock.MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({
        "statusCheckRollup": [
            # status-context shape: uses "state" not "conclusion"
            {"context": "ci/travis", "state": "FAILURE"},
        ],
        "mergeStateStatus": "DIRTY",
    })
    fake.stderr = ""

    with mock.patch("subprocess.run", return_value=fake):
        state, detail = baton._pr_ci_state("42")
    assert state == "red"
    assert "travis" in detail
