"""Tests for hooks/claude/engram-baton-prompt-hook.py.

Covers all key behaviors:

  1. Missing projects dir → silent exit 0, no output (single-agent guard).
  2. Empty projects dir → silent exit 0, no output.
  3. Projects dir exists, no batons in my court → silent exit 0.
  4. One baton in my court → surfaces it in additionalContext.
  5. Multiple batons → all listed, sorted by turn_since ascending.
  6. Letter addressed to other agent → NOT surfaced.
  7. Merged / cancelled projects → NOT surfaced.
  8. Malformed project file → silently skipped.
  9. turn_reason quotes stripped in surfaced output.
  10. JSON output format: hookSpecificOutput.additionalContext.

Each test invokes the hook as a subprocess with a fake ENGRAM_HOME +
fake BATON_PROJECTS_DIR to run end-to-end filesystem path.
"""

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

HOOK_PATH = (
    Path(__file__).parent.parent
    / "hooks" / "claude" / "engram-baton-prompt-hook.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_hook(
    engram_home: Path,
    projects_dir: Path,
    hook_input: dict | None = None,
) -> tuple:
    """Run the hook as a subprocess; return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
    env["BATON_PROJECTS_DIR"] = str(projects_dir)
    stdin_data = json.dumps(hook_input or {"prompt": "test"})
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _write_config(engram_home: Path, agent_name: str = "borges") -> None:
    """Write a minimal config.json into engram_home."""
    cfg = {"mode": "multi", "agent_name": agent_name}
    (engram_home / "config.json").write_text(json.dumps(cfg))


def _write_project(
    projects_dir: Path,
    project_id: str,
    title: str = "Test project",
    status: str = "in-review",
    turn: str = "borges",
    participants: list | None = None,
    turn_since: datetime | None = None,
    turn_reason: str = "colleague-review request",
) -> Path:
    """Write a baton project file for testing."""
    if participants is None:
        participants = ["borges", "ariadne"]
    if turn_since is None:
        turn_since = datetime.now(timezone.utc) - timedelta(minutes=30)
    ts_str = turn_since.strftime("%Y-%m-%dT%H:%M:%SZ")
    parts_str = "[" + ", ".join(participants) + "]"
    content_lines = [
        "---",
        f"project: {project_id}",
        f"title: {title}",
        f"status: {status}",
        f"turn: {turn}",
        f"turn_since: {ts_str}",
        f'turn_reason: "{turn_reason}"',
        f"participants: {parts_str}",
        "---",
        "",
        f"# {project_id} — {title}",
        "",
        "## Turn log",
        "",
        f"- {ts_str} initialized → {turn}",
        "",
    ]
    content = "\n".join(content_lines)
    path = projects_dir / f"{project_id}.md"
    path.write_text(content)
    return path


def _ts(offset_minutes: int) -> datetime:
    """Return a UTC datetime offset by given minutes from now."""
    return datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram_home"
    home.mkdir()
    return home


@pytest.fixture
def projects_dir(tmp_path: Path) -> Path:
    d = tmp_path / "projects"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Test 1: Missing projects dir → silent exit
# ---------------------------------------------------------------------------

def test_missing_projects_dir_silent_exit(engram_home, tmp_path):
    """If projects dir doesn't exist, hook exits 0 with no output."""
    _write_config(engram_home, "borges")
    nonexistent = tmp_path / "no-baton-projects"
    # Do NOT create it
    rc, stdout, stderr = _run_hook(engram_home, nonexistent)
    assert rc == 0
    assert stdout.strip() == "", (
        "Hook must be silent when projects dir doesn't exist (single-agent guard)"
    )


# ---------------------------------------------------------------------------
# Test 2: Empty projects dir → silent exit
# ---------------------------------------------------------------------------

def test_empty_projects_dir_silent_exit(engram_home, projects_dir):
    """Projects dir exists but empty → hook exits 0 with no output."""
    _write_config(engram_home, "borges")
    rc, stdout, stderr = _run_hook(engram_home, projects_dir)
    assert rc == 0
    assert stdout.strip() == ""


# ---------------------------------------------------------------------------
# Test 3: No batons in my court → silent exit
# ---------------------------------------------------------------------------

def test_no_mine_batons_silent_exit(engram_home, projects_dir):
    """Projects exist but none have turn == borges → silent exit."""
    _write_config(engram_home, "borges")
    _write_project(projects_dir, "PR-100", turn="ariadne")
    rc, stdout, stderr = _run_hook(engram_home, projects_dir)
    assert rc == 0
    assert stdout.strip() == ""


# ---------------------------------------------------------------------------
# Test 4: One baton in my court → surfaces it
# ---------------------------------------------------------------------------

def test_one_baton_surfaces(engram_home, projects_dir):
    """One project with turn==borges → additionalContext with project ID."""
    _write_config(engram_home, "borges")
    _write_project(
        projects_dir, "PR-200",
        title="viz_server selector",
        turn="borges",
        turn_reason="round-3 fix pushed, awaiting re-review",
    )
    rc, stdout, stderr = _run_hook(engram_home, projects_dir)
    assert rc == 0
    assert stdout.strip() != ""
    response = json.loads(stdout)
    assert "hookSpecificOutput" in response
    output = response["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    ctx = output["additionalContext"]
    assert "PR-200" in ctx
    assert "baton" in ctx.lower()


# ---------------------------------------------------------------------------
# Test 5: Multiple batons → all listed, sorted oldest first
# ---------------------------------------------------------------------------

def test_multiple_batons_sorted(engram_home, projects_dir):
    """Multiple batons in court: all listed, oldest turn_since appears first."""
    _write_config(engram_home, "borges")
    # Older baton
    _write_project(
        projects_dir, "PR-301", turn="borges",
        turn_since=_ts(-120),  # 2 hours ago
        turn_reason="older baton reason",
    )
    # Newer baton
    _write_project(
        projects_dir, "PR-302", turn="borges",
        turn_since=_ts(-30),  # 30 minutes ago
        turn_reason="newer baton reason",
    )
    rc, stdout, stderr = _run_hook(engram_home, projects_dir)
    assert rc == 0
    response = json.loads(stdout)
    ctx = response["hookSpecificOutput"]["additionalContext"]
    assert "PR-301" in ctx
    assert "PR-302" in ctx
    # Both should appear, older first
    idx_301 = ctx.find("PR-301")
    idx_302 = ctx.find("PR-302")
    assert idx_301 < idx_302, "Older baton should appear before newer"
    # Count should be 2
    assert "2 batons" in ctx


# ---------------------------------------------------------------------------
# Test 6: Project with turn == other agent → NOT surfaced
# ---------------------------------------------------------------------------

def test_other_agent_baton_not_surfaced(engram_home, projects_dir):
    """Project where turn == ariadne → not surfaced for borges."""
    _write_config(engram_home, "borges")
    _write_project(projects_dir, "PR-400", turn="ariadne")
    rc, stdout, stderr = _run_hook(engram_home, projects_dir)
    assert rc == 0
    assert stdout.strip() == "", (
        "Baton in ariadne's court should not surface for borges"
    )


# ---------------------------------------------------------------------------
# Test 7: Merged / cancelled projects → NOT surfaced
# ---------------------------------------------------------------------------

def test_merged_project_not_surfaced(engram_home, projects_dir):
    """Merged project is not surfaced even if turn == agent."""
    _write_config(engram_home, "borges")
    _write_project(projects_dir, "PR-500", turn="borges", status="merged")
    rc, stdout, stderr = _run_hook(engram_home, projects_dir)
    assert rc == 0
    assert stdout.strip() == "", "Merged project must not surface"


def test_cancelled_project_not_surfaced(engram_home, projects_dir):
    """Cancelled project is not surfaced even if turn == agent."""
    _write_config(engram_home, "borges")
    _write_project(projects_dir, "PR-501", turn="borges", status="cancelled")
    rc, stdout, stderr = _run_hook(engram_home, projects_dir)
    assert rc == 0
    assert stdout.strip() == "", "Cancelled project must not surface"


# ---------------------------------------------------------------------------
# Test 8: Malformed project file → silently skipped
# ---------------------------------------------------------------------------

def test_malformed_file_skipped(engram_home, projects_dir):
    """Files with missing/broken frontmatter are silently skipped."""
    _write_config(engram_home, "borges")

    # No frontmatter at all
    (projects_dir / "no-frontmatter.md").write_text("Just text.\n")

    # Incomplete frontmatter (no turn field)
    (projects_dir / "incomplete.md").write_text(
        "---\nproject: PR-999\ntitle: test\n---\nBody.\n"
    )

    # Binary garbage
    (projects_dir / "garbage.md").write_bytes(b"\x00\x01\x02 not yaml\n")

    # Valid project with our turn — should still surface
    _write_project(projects_dir, "PR-601", turn="borges")

    rc, stdout, stderr = _run_hook(engram_home, projects_dir)
    assert rc == 0, f"Hook must not crash on malformed files; stderr: {stderr}"
    assert stdout.strip() != ""
    response = json.loads(stdout)
    ctx = response["hookSpecificOutput"]["additionalContext"]
    assert "PR-601" in ctx


# ---------------------------------------------------------------------------
# Test 9: turn_reason quotes stripped in surfaced output
# ---------------------------------------------------------------------------

def test_turn_reason_quotes_stripped(engram_home, projects_dir):
    """turn_reason with surrounding quotes is displayed without extra quotes."""
    _write_config(engram_home, "borges")
    _write_project(
        projects_dir, "PR-700",
        turn="borges",
        turn_reason="round-3 fix pushed (d81d091); awaiting re-review",
    )
    rc, stdout, stderr = _run_hook(engram_home, projects_dir)
    assert rc == 0
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert "round-3 fix pushed" in ctx
    # The extra outer quotes from YAML storage must NOT appear as double-quotes
    assert '""round-3' not in ctx


# ---------------------------------------------------------------------------
# Test 10: JSON output format
# ---------------------------------------------------------------------------

def test_json_output_format(engram_home, projects_dir):
    """Output is valid JSON with hookSpecificOutput.additionalContext."""
    _write_config(engram_home, "borges")
    _write_project(projects_dir, "PR-800", turn="borges")
    rc, stdout, stderr = _run_hook(engram_home, projects_dir)
    assert rc == 0
    # Must be valid JSON
    response = json.loads(stdout)
    assert isinstance(response, dict)
    assert "hookSpecificOutput" in response
    hook_out = response["hookSpecificOutput"]
    assert "hookEventName" in hook_out
    assert hook_out["hookEventName"] == "UserPromptSubmit"
    assert "additionalContext" in hook_out
    assert isinstance(hook_out["additionalContext"], str)
    assert len(hook_out["additionalContext"]) > 0


# ---------------------------------------------------------------------------
# Test 11: Mixed state — some mine, some not, some closed
# ---------------------------------------------------------------------------

def test_mixed_state_only_mine_active_surfaced(engram_home, projects_dir):
    """Only active projects where turn==me surface; others invisible."""
    _write_config(engram_home, "borges")

    # My active baton
    _write_project(projects_dir, "PR-901", turn="borges", status="in-review")
    # Ariadne's baton (not mine)
    _write_project(projects_dir, "PR-902", turn="ariadne", status="in-review")
    # My merged project (closed)
    _write_project(projects_dir, "PR-903", turn="borges", status="merged")
    # My cancelled project (closed)
    _write_project(projects_dir, "PR-904", turn="borges", status="cancelled")

    rc, stdout, stderr = _run_hook(engram_home, projects_dir)
    assert rc == 0
    assert stdout.strip() != ""
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]

    assert "PR-901" in ctx       # mine, active → surfaces
    assert "PR-902" not in ctx   # not mine
    assert "PR-903" not in ctx   # mine but merged
    assert "PR-904" not in ctx   # mine but cancelled
    assert "1 baton" in ctx      # only one


# ---------------------------------------------------------------------------
# Test 12: Age formatting in surfaced output
# ---------------------------------------------------------------------------

def test_age_formatting_shown(engram_home, projects_dir):
    """turn_since age is shown in the surfaced output."""
    _write_config(engram_home, "borges")
    # Baton has been sitting for ~2 hours
    _write_project(
        projects_dir, "PR-950",
        turn="borges",
        turn_since=_ts(-120),  # 2 hours ago
    )
    rc, stdout, stderr = _run_hook(engram_home, projects_dir)
    assert rc == 0
    ctx = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert "PR-950" in ctx
    # Age should be present (h ago / min ago pattern)
    assert "ago" in ctx


# ---------------------------------------------------------------------------
# Module-level loader helper (used by auto-archive tests below)
# ---------------------------------------------------------------------------

def _load_hook_module(engram_home: Path, projects_dir: Path):
    """Import the hook as a live module with env vars patched to tmp dirs."""
    spec = importlib.util.spec_from_file_location(
        "engram_baton_prompt_hook", HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {
        "ENGRAM_HOME": str(engram_home),
        "BATON_PROJECTS_DIR": str(projects_dir),
    }):
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests 13–17: Auto-archive on MERGED detection
# ---------------------------------------------------------------------------

class TestAutoArchiveOnMerged:
    """Auto-archive PR-batons when live GitHub status is MERGED."""

    # ------------------------------------------------------------------
    # Test 13: triggers baton close when live status is MERGED
    # ------------------------------------------------------------------

    def test_auto_archive_pr_baton_when_merged(self, engram_home, projects_dir):
        """baton close is called when a PR-baton's live status returns MERGED."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1100",
            turn="borges",
            status="in-progress",
            turn_reason="fix pushed, awaiting merge",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod, "_fetch_pr_status", return_value="MERGED"):
                with patch.object(mod, "subprocess") as mock_subproc:
                    # subprocess.run is called for baton close; ensure it
                    # doesn't raise so the hook completes.
                    mock_subproc.run.return_value = type(
                        "CompletedProcess", (), {"returncode": 0}
                    )()
                    mod.main()

        # Verify baton close was called exactly once with the expected args
        # (assert_called_once_with also catches accidental double-fire regressions
        # — _fetch_pr_status is patched so no subprocess.run for gh is invoked;
        # the only subprocess.run in this code path is the baton close call.)
        mock_subproc.run.assert_called_once_with(
            ["baton", "close", "PR-1100", "--status", "merged"],
            capture_output=True,
            text=True,
            timeout=mod._GH_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Test 14: no baton close when live status is OPEN
    # ------------------------------------------------------------------

    def test_skip_archive_pr_baton_when_open(self, engram_home, projects_dir):
        """baton close is NOT called when the live status is OPEN."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1200",
            turn="borges",
            status="in-review",
            turn_reason="needs review",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        baton_close_calls = []

        def _fake_run(args, **kwargs):
            if args[:2] == ["baton", "close"]:
                baton_close_calls.append(args)
            return type("CompletedProcess", (), {"returncode": 0})()

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod, "_fetch_pr_status", return_value="OPEN"):
                with patch.object(mod, "subprocess") as mock_subproc:
                    mock_subproc.run.side_effect = _fake_run
                    mod.main()

        assert baton_close_calls == [], (
            "baton close must not be called when live status is OPEN"
        )

    # ------------------------------------------------------------------
    # Test 15: no baton close for project-anchor batons (project/N)
    # ------------------------------------------------------------------

    def test_skip_archive_project_baton(self, engram_home, projects_dir):
        """baton close is NOT called for project/N batons, even if MERGED-like."""
        _write_config(engram_home, "borges")
        # Use a non-PR-N project_id so _PR_ID_RE does not match
        _write_project(
            projects_dir, "pool-42",
            turn="borges",
            status="in-review",
            turn_reason="pool work",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        baton_close_calls = []

        def _fake_run(args, **kwargs):
            if args[:2] == ["baton", "close"]:
                baton_close_calls.append(args)
            return type("CompletedProcess", (), {"returncode": 0})()

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            # Even if we somehow get MERGED back (no anchor resolves for pool-42,
            # so _fetch_pr_status won't actually be called), ensure the guard holds.
            with patch.object(mod, "_fetch_pr_status", return_value="MERGED"):
                with patch.object(mod, "subprocess") as mock_subproc:
                    mock_subproc.run.side_effect = _fake_run
                    mod.main()

        assert baton_close_calls == [], (
            "baton close must not be called for non-PR-N project IDs"
        )

    # ------------------------------------------------------------------
    # Test 16: subprocess failure does not crash the hook
    # ------------------------------------------------------------------

    def test_archive_subprocess_failure_does_not_crash_hook(
        self, engram_home, projects_dir
    ):
        """If baton close subprocess raises, hook completes normally."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1300",
            turn="borges",
            status="in-review",
            turn_reason="awaiting merge",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        def _exploding_run(args, **kwargs):
            if args[:2] == ["baton", "close"]:
                raise OSError("baton not found")
            return type("CompletedProcess", (), {"returncode": 0})()

        captured_output = []

        def _fake_print(data):
            captured_output.append(data)

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod, "_fetch_pr_status", return_value="MERGED"):
                with patch.object(mod, "subprocess") as mock_subproc:
                    mock_subproc.run.side_effect = _exploding_run
                    with patch("builtins.print", side_effect=_fake_print):
                        # Should not raise despite OSError in baton close
                        mod.main()

        # Hook must have produced a JSON output (not crashed silently)
        assert len(captured_output) == 1
        response = json.loads(captured_output[0])
        assert "hookSpecificOutput" in response
        ctx = response["hookSpecificOutput"]["additionalContext"]
        assert "PR-1300" in ctx

    # ------------------------------------------------------------------
    # Test 17: already-merged baton never reaches auto-archive (filter gate)
    # ------------------------------------------------------------------

    def test_archive_skipped_when_baton_already_merged(
        self, engram_home, projects_dir
    ):
        """Baton with status==merged is excluded by CLOSED_STATUSES filter.

        The auto-archive code path is never reached because _scan_my_batons
        skips closed-status projects before they enter the rendering loop.
        This test verifies the filter is the gate — not the b["status"] guard
        in the auto-archive block — by confirming no closed baton surfaces.
        """
        _write_config(engram_home, "borges")
        # Write a baton that is already merged — should be filtered out by
        # _scan_my_batons before reaching the auto-archive check
        _write_project(
            projects_dir, "PR-1400",
            turn="borges",
            status="merged",
            turn_reason="already closed",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            batons = mod._scan_my_batons("borges")

        # CLOSED_STATUSES filter excludes merged batons entirely
        assert batons == [], (
            "Merged baton must be excluded by _scan_my_batons — "
            "auto-archive code path never reached for already-closed batons"
        )
