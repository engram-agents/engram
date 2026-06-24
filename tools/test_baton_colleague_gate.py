"""Tests for the colleague-gate enforcement in tools/baton.py (issue #1267, Phase 1).

This covers Fix A' (cmd_flip no_approval enforcement) and Fix B (cmd_init
--colleague + PR-baton init warning).

Fix A' — flip-to-sentinel with no_approval, enforcement depends on colleague presence:
  1. no_approval + colleague participant → REJECTED (EXIT_VALIDATION)
  2. no_approval + colleague + --force → proceeds with warning
  3. no_approval + NO colleague (single-agent) → warn-only, proceeds
  4. fresh approval + colleague → proceeds silently (no regression)
  5. stale approval + colleague → REJECTED by the stale branch (no regression)
  6. flip to NON-sentinel → guard not consulted (no regression)
  7. unknown approval verdict → advisory warn, proceeds (no regression)

Fix B — init:
  8. init PR-N without --colleague, only one participant → warns (no colleague)
  9. init PR-N with --colleague → colleague added to participants + body note
 10. init non-PR baton without colleague → no warning (only for PR-named batons)
 11. init PR-N with colleague already in --participants → no dup, note still written

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
    """Pin CI to green so these tests isolate the colleague gate, not the CI gate."""
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


# ---------------------------------------------------------------------------
# Baton file factory
# ---------------------------------------------------------------------------

def _make_baton_file(
    projects_dir: Path,
    project_id: str,
    *,
    github: str = "",
    participants: list | None = None,
    turn: str = "borges",
) -> Path:
    """Create a minimal baton file with the given project_id and participants."""
    if participants is None:
        participants = ["borges", "ariadne"]
    participants_str = "[" + ", ".join(participants) + "]"
    extra = f"\ngithub: {github}" if github else ""
    content = (
        "---\n"
        f"project: {project_id}\n"
        "title: Test PR\n"
        "status: in-review\n"
        f"turn: {turn}\n"
        "turn_since: 2026-01-01T00:00:00Z\n"
        'turn_reason: "ready for review"\n'
        f"participants: {participants_str}\n"
        f"{extra}\n"
        "---\n\n"
        "# Test\n\n"
        "## Turn log\n\n"
        f"- 2026-01-01T00:00:00Z borges → ariadne: initial\n"
    )
    # Remove accidental double blank lines introduced by the optional extra line
    content = content.replace("\n\n---\n", "\n---\n")
    p = projects_dir / f"{project_id}.md"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Flip invocation helper
# ---------------------------------------------------------------------------

def _invoke_flip(project_id, to, reason, force=False, agent_name="borges"):
    args = argparse.Namespace(
        project_id=project_id,
        to=to,
        reason=reason,
        force=force,
    )
    config = {}
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
# Fix A' tests — flip enforcement
# ---------------------------------------------------------------------------

def test_no_approval_with_colleague_rejected(projects_dir, engram_home, monkeypatch):
    """Test 1: no_approval + colleague participant → REJECTED."""
    _make_baton_file(
        projects_dir, "PR-300",
        github="pr/300",
        participants=["borges", "ariadne"],
    )
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("no_approval", "no APPROVED review on the PR"),
    )
    baton_file = projects_dir / "PR-300.md"
    original_content = baton_file.read_text()
    code, out, err = _invoke_flip("PR-300", SENTINEL, "ready to merge")
    assert code == baton.EXIT_VALIDATION, f"expected EXIT_VALIDATION, got {code}; stderr={err!r}"
    assert "no colleague approval" in err
    assert "300" in err
    assert "non-author colleague" in err
    # File must not have been mutated
    assert baton_file.read_text() == original_content, (
        "baton file was mutated despite colleague-gate rejection"
    )


def test_no_approval_with_colleague_force_proceeds(projects_dir, engram_home, monkeypatch):
    """Test 2: no_approval + colleague + --force → proceeds with warning."""
    _make_baton_file(
        projects_dir, "PR-301",
        github="pr/301",
        participants=["borges", "ariadne"],
    )
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("no_approval", "no APPROVED review on the PR"),
    )
    code, out, err = _invoke_flip("PR-301", SENTINEL, "ready to merge", force=True)
    assert code == 0, f"expected exit 0 with --force, got {code}; stderr={err!r}"
    assert "warning" in err.lower()
    assert "no colleague approval" in err


def test_no_approval_without_colleague_warns_and_proceeds(
    projects_dir, engram_home, monkeypatch
):
    """Test 3: no_approval + no colleague (single-agent) → warn-only, proceeds."""
    # participants: only borges (the author); no third party
    _make_baton_file(
        projects_dir, "PR-302",
        github="pr/302",
        participants=["borges"],
    )
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("no_approval", "no APPROVED review on the PR"),
    )
    code, out, err = _invoke_flip("PR-302", SENTINEL, "ready to merge")
    assert code == 0, f"single-agent no_approval must proceed, got {code}; stderr={err!r}"
    assert "single-agent mode" in err
    assert "warning" in err.lower()


def test_fresh_approval_with_colleague_proceeds_silently(
    projects_dir, engram_home, monkeypatch
):
    """Test 4: fresh approval + colleague → proceeds silently (no regression)."""
    _make_baton_file(
        projects_dir, "PR-303",
        github="pr/303",
        participants=["borges", "ariadne"],
    )
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("fresh", "approval covers tip oid aabbccdd"),
    )
    code, out, err = _invoke_flip("PR-303", SENTINEL, "ready to merge")
    assert code == 0, f"fresh approval must proceed, got {code}; stderr={err!r}"
    assert err == "", f"fresh approval must be silent, got stderr={err!r}"


def test_stale_approval_with_colleague_rejected_by_stale_branch(
    projects_dir, engram_home, monkeypatch
):
    """Test 5: stale approval + colleague → rejected by the stale branch (no regression).
    The stale branch must fire before the no_approval branch is reached."""
    _make_baton_file(
        projects_dir, "PR-304",
        github="pr/304",
        participants=["borges", "ariadne"],
    )
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("stale", "tip moved after approval"),
    )
    code, out, err = _invoke_flip("PR-304", SENTINEL, "ready to merge")
    assert code == baton.EXIT_VALIDATION, f"stale must reject, got {code}"
    assert "tip moved after approval" in err


def test_flip_to_non_sentinel_skips_colleague_gate(
    projects_dir, engram_home, monkeypatch
):
    """Test 6: flip to non-sentinel participant → guard not consulted."""
    _make_baton_file(
        projects_dir, "PR-305",
        github="pr/305",
        participants=["borges", "ariadne"],
    )

    def _boom(pr_num):
        raise AssertionError("approval guard must not be consulted on non-sentinel flips")

    monkeypatch.setattr(baton, "_pr_approval_state", _boom)
    code, out, err = _invoke_flip("PR-305", "ariadne", "your review please")
    assert code == 0, f"non-sentinel flip must skip the guard, got {code}; stderr={err!r}"


def test_unknown_approval_warns_and_proceeds(projects_dir, engram_home, monkeypatch):
    """Test 7: unknown verdict → advisory warn, proceeds (no regression)."""
    _make_baton_file(
        projects_dir, "PR-306",
        github="pr/306",
        participants=["borges", "ariadne"],
    )
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("unknown", "gh timed out after 15s"),
    )
    code, out, err = _invoke_flip("PR-306", SENTINEL, "ready to merge")
    assert code == 0, f"unknown must proceed, got {code}; stderr={err!r}"
    assert "could not verify post-approval state" in err


# ---------------------------------------------------------------------------
# Init invocation helper
# ---------------------------------------------------------------------------

def _invoke_init(
    project_id,
    title,
    participants,
    *,
    turn=None,
    colleague=None,
    status="in-progress",
    github=None,
    agent_name="borges",
):
    args = argparse.Namespace(
        project_id=project_id,
        title=title,
        participants=participants,
        turn=turn,
        colleague=colleague,
        status=status,
        github=github,
    )
    config = {}
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    exit_code = 0
    with mock.patch("sys.stdout", stdout_buf), \
         mock.patch("sys.stderr", stderr_buf):
        try:
            baton.cmd_init(args, config, agent_name)
        except SystemExit as exc:
            exit_code = int(exc.code) if exc.code is not None else 0
    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# Fix B tests — init --colleague + no-colleague warning
# ---------------------------------------------------------------------------

def test_init_pr_baton_without_colleague_warns(projects_dir, engram_home):
    """Test 8: init PR-N without --colleague, only one participant → warns."""
    code, out, err = _invoke_init(
        "PR-400",
        "My PR",
        "borges",  # only one participant, no colleague
        turn="borges",
    )
    assert code == 0, f"init should succeed even without colleague, got {code}; stderr={err!r}"
    assert "no colleague participant" in err or "colleague gate" in err.lower()
    # Baton file should have been created
    assert (projects_dir / "PR-400.md").exists()


def test_init_pr_baton_with_colleague_adds_participant_and_body_note(
    projects_dir, engram_home
):
    """Test 9: init PR-N with --colleague → participant added + body note."""
    code, out, err = _invoke_init(
        "PR-401",
        "My PR",
        "borges",  # start with only borges in --participants
        turn="borges",
        colleague="ariadne",
    )
    assert code == 0, f"init with --colleague should succeed, got {code}; stderr={err!r}"
    # No warning about no-colleague since colleague was provided
    assert "no colleague participant" not in err
    baton_file = projects_dir / "PR-401.md"
    assert baton_file.exists()
    content = baton_file.read_text()
    # ariadne must appear in participants frontmatter
    assert "ariadne" in content
    # Body note must appear
    assert "Colleague reviewer: ariadne" in content
    # colleague printed on stdout
    assert "ariadne" in out


def test_init_non_pr_baton_no_colleague_warning(projects_dir, engram_home):
    """Test 10: init non-PR baton without colleague → no warning (only PR-named batons)."""
    code, out, err = _invoke_init(
        "DESIGN-trust-tier",
        "Trust tier design",
        "borges",
        turn="borges",
    )
    assert code == 0, f"init should succeed, got {code}; stderr={err!r}"
    # No colleague warning for non-PR batons
    assert "no colleague participant" not in err
    assert "colleague gate" not in err.lower()


def test_init_pr_baton_colleague_already_in_participants_no_dup(
    projects_dir, engram_home
):
    """Test 11: init PR-N with colleague already in --participants → no dup, note still written."""
    code, out, err = _invoke_init(
        "PR-402",
        "My PR",
        "borges,ariadne",  # ariadne already in participants
        turn="borges",
        colleague="ariadne",  # also specified as --colleague
    )
    assert code == 0, f"init should succeed, got {code}; stderr={err!r}"
    baton_file = projects_dir / "PR-402.md"
    content = baton_file.read_text()
    # ariadne should appear exactly once in participants line (no dup)
    import re
    fm_match = re.search(r"participants: \[([^\]]+)\]", content)
    assert fm_match, "participants field not found"
    participant_list = [p.strip() for p in fm_match.group(1).split(",")]
    assert participant_list.count("ariadne") == 1, (
        f"ariadne appears more than once: {participant_list}"
    )
    # Body note should still appear
    assert "Colleague reviewer: ariadne" in content


# ---------------------------------------------------------------------------
# Inverted-holder scenario — colleague holds the baton, flips to sentinel
# ---------------------------------------------------------------------------

def test_inverted_holder_colleague_flips_to_sentinel_no_approval_rejected(
    projects_dir, engram_home, monkeypatch
):
    """Test 12: colleague holds the baton and flips to sentinel with no_approval → REJECTED.

    When the colleague (ariadne) holds the baton and flips to the sentinel
    without an approval, _colleague_participants resolves to the author (borges),
    not ariadne.  The old message would have said "Have the colleague (borges)
    review and approve" — naming the author as the required approver, which is
    wrong.  The fixed message must be generic (no names) and the flip must still
    be rejected.
    """
    # Colleague (ariadne) currently holds the baton
    _make_baton_file(
        projects_dir, "PR-500",
        github="pr/500",
        participants=["borges", "ariadne"],
        turn="ariadne",
    )
    monkeypatch.setattr(
        baton, "_pr_approval_state",
        lambda pr_num: ("no_approval", "no APPROVED review on the PR"),
    )
    baton_file = projects_dir / "PR-500.md"
    original_content = baton_file.read_text()

    # Flip is invoked as ariadne (the current holder)
    code, out, err = _invoke_flip("PR-500", SENTINEL, "ready to merge", agent_name="ariadne")

    # Must be rejected
    assert code == baton.EXIT_VALIDATION, (
        f"expected EXIT_VALIDATION, got {code}; stderr={err!r}"
    )
    # Generic phrasing must be present
    assert "non-author colleague" in err, (
        f"expected generic 'non-author colleague' message, got stderr={err!r}"
    )
    # The author's name must NOT appear as the named required approver
    assert "borges" not in err, (
        f"author name 'borges' must not appear in the rejection message, got stderr={err!r}"
    )
    # File must not have been mutated
    assert baton_file.read_text() == original_content, (
        "baton file was mutated despite colleague-gate rejection"
    )
