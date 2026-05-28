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

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
