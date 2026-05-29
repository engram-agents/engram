"""Tests for tools/baton.py — multi-agent turn-state CLI.

Test categories:
  - Smoke:          each subcommand happy path exits 0
  - Validation:     bad inputs rejected with expected exit codes / errors
  - Frontmatter:    parser handles edge cases, malformed files, quotes
  - AtomicWrite:    no .tmp files remain after successful operations
  - SingleAgent:    missing projects dir → silent exit or actionable error
  - Integration:    init → flip → mine → show → close round-trip

Mirrors the class-based pytest structure of tests/test_ia_cli.py.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).parent.parent / "tools" / "baton.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_baton(
    engram_home: Path,
    projects_dir: Path,
    *args: str,
    env_overrides: dict | None = None,
) -> tuple:
    """Run the baton CLI as a subprocess; return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
    env["BATON_PROJECTS_DIR"] = str(projects_dir)
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, str(CLI_PATH)] + list(args),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
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
    turn_since: str | None = None,
    turn_reason: str = "test reason",
) -> Path:
    """Write a minimal baton project file. Returns the path."""
    if participants is None:
        participants = ["borges", "ariadne"]
    if turn_since is None:
        turn_since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts_str = "[" + ", ".join(participants) + "]"
    content_lines = [
        "---",
        f"project: {project_id}",
        f"title: {title}",
        f"status: {status}",
        f"turn: {turn}",
        f"turn_since: {turn_since}",
        f'turn_reason: "{turn_reason}"',
        f"participants: {parts_str}",
        "---",
        "",
        f"# {project_id} — {title}",
        "",
        "## Turn log",
        "",
        f"- {turn_since} initialized → {turn}: {turn_reason}",
        "",
    ]
    content = "\n".join(content_lines)
    path = projects_dir / f"{project_id}.md"
    path.write_text(content)
    return path


def _ts(offset_minutes: int = -5) -> str:
    """Return ISO timestamp offset from now by given minutes."""
    dt = datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_project_field(projects_dir: Path, project_id: str, field: str) -> str:
    """Read a specific frontmatter field from a project file."""
    path = projects_dir / f"{project_id}.md"
    text = path.read_text()
    import re
    m = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL).match(text)
    if not m:
        return ""
    fm = m.group(1)
    for line in fm.splitlines():
        fm_m = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if fm_m and fm_m.group(1).lower() == field.lower():
            val = fm_m.group(2).strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            return val
    return ""


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


# ===========================================================================
# CATEGORY 1: Smoke tests — each subcommand happy path
# ===========================================================================

class TestSmoke:
    """Each subcommand exits 0 on happy-path input."""

    def test_help_exits_0(self, engram_home, projects_dir):
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "--help")
        assert rc == 0
        assert "init" in stdout
        assert "flip" in stdout
        assert "status" in stdout

    def test_init_exits_0(self, engram_home, projects_dir):
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "init", "PR-999",
            "--title", "smoke test",
            "--participants", "borges,ariadne",
        )
        assert rc == 0, f"init failed; stderr: {stderr}"
        assert (projects_dir / "PR-999.md").exists()

    def test_status_empty_exits_0(self, engram_home, projects_dir):
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "status")
        assert rc == 0

    def test_status_with_project_exits_0(self, engram_home, projects_dir):
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-100", turn="borges")
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "status")
        assert rc == 0
        assert "PR-100" in stdout

    def test_mine_exits_0(self, engram_home, projects_dir):
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-200", turn="borges")
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "mine")
        assert rc == 0
        assert "PR-200" in stdout

    def test_show_exits_0(self, engram_home, projects_dir):
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-300")
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "show", "PR-300")
        assert rc == 0
        assert "PR-300" in stdout

    def test_flip_exits_0(self, engram_home, projects_dir):
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-400", turn="borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "flip", "PR-400", "ariadne", "done step 1",
        )
        assert rc == 0, f"flip failed; stderr: {stderr}"

    def test_close_exits_0(self, engram_home, projects_dir):
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-500", status="in-review")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "close", "PR-500", "--status", "merged",
        )
        assert rc == 0, f"close failed; stderr: {stderr}"


# ===========================================================================
# CATEGORY 2: Validation tests — bad inputs rejected
# ===========================================================================

class TestValidation:
    """Bad inputs are rejected with expected exit codes and error messages."""

    def test_init_duplicate_fails(self, engram_home, projects_dir):
        """init on existing project → exit 1, already-exists error."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-101")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "init", "PR-101",
            "--title", "duplicate",
            "--participants", "borges,ariadne",
        )
        assert rc == 1
        assert "already exists" in stderr

    def test_init_invalid_project_id_fails(self, engram_home, projects_dir):
        """init with invalid ID (no hyphen) → exit 1, format error."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "init", "NOHYPHEN",
            "--title", "bad id",
            "--participants", "borges",
        )
        assert rc == 1
        assert "TYPE-ID" in stderr or "invalid" in stderr.lower()

    def test_init_invalid_status_fails(self, engram_home, projects_dir):
        """init with unknown status → exit 1."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "init", "PR-102",
            "--title", "bad status",
            "--participants", "borges,ariadne",
            "--status", "unknown-status",
        )
        # argparse itself rejects invalid choices
        assert rc != 0

    def test_flip_nonexistent_project_fails(self, engram_home, projects_dir):
        """flip on non-existent project → exit 1, not-found error."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "flip", "PR-NONE", "ariadne", "done",
        )
        assert rc == 1
        assert "not found" in stderr.lower()

    def test_flip_to_non_participant_fails(self, engram_home, projects_dir):
        """flip to agent not in participants → exit 1, validation error."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-103", participants=["borges", "ariadne"])
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "flip", "PR-103", "mneme", "done",
        )
        assert rc == 1
        assert "not a participant" in stderr or "participant" in stderr

    def test_flip_to_lei_allowed(self, engram_home, projects_dir):
        """flip to 'lei' is always valid regardless of participants list."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-104", participants=["borges", "ariadne"])
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "flip", "PR-104", "lei", "need human decision",
        )
        assert rc == 0, f"flip to lei failed; stderr: {stderr}"

    def test_flip_closed_project_fails(self, engram_home, projects_dir):
        """flip on merged project → exit 1, closed error."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-105", status="merged")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "flip", "PR-105", "ariadne", "done",
        )
        assert rc == 1
        assert "merged" in stderr or "closed" in stderr

    def test_flip_cancelled_project_fails(self, engram_home, projects_dir):
        """flip on cancelled project → exit 1, closed error."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-106", status="cancelled")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "flip", "PR-106", "ariadne", "done",
        )
        assert rc == 1
        assert "cancelled" in stderr or "closed" in stderr

    def test_show_nonexistent_fails(self, engram_home, projects_dir):
        """show on non-existent project → exit 1, not-found error."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "show", "PR-NONE",
        )
        assert rc == 1
        assert "not found" in stderr.lower()

    def test_close_invalid_status_fails(self, engram_home, projects_dir):
        """close with status=in-review (not a close status) → exit 1."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-107")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "close", "PR-107", "--status", "in-review",
        )
        # argparse rejects invalid choices
        assert rc != 0

    def test_close_already_closed_fails(self, engram_home, projects_dir):
        """close on already-merged project → exit 1, already-closed error."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-108", status="merged")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "close", "PR-108", "--status", "cancelled",
        )
        assert rc == 1
        assert "already" in stderr

    def test_init_turn_outside_participants_fails(self, engram_home, projects_dir):
        """init with --turn not in participants (and not lei) → exit 1."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "init", "PR-109",
            "--title", "test",
            "--participants", "borges,ariadne",
            "--turn", "mneme",
        )
        assert rc == 1
        assert "participants" in stderr or "turn" in stderr


# ===========================================================================
# CATEGORY 3: Frontmatter parsing robustness
# ===========================================================================

class TestFrontmatterParsing:
    """Parser handles edge cases and malformed files gracefully."""

    def test_show_displays_full_content(self, engram_home, projects_dir):
        """show outputs the complete file content including body."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-201",
            title="Full content test",
            turn_reason="colleague-review request",
        )
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "show", "PR-201")
        assert rc == 0
        assert "PR-201" in stdout
        assert "Full content test" in stdout
        assert "Turn log" in stdout

    def test_participants_brackets_parsed(self, engram_home, projects_dir):
        """Participants in [alice, bob] format are parsed correctly."""
        _write_config(engram_home, "borges")
        # Write a project directly with bracketed participants
        content = (
            "---\n"
            "project: PR-202\n"
            "title: Bracket test\n"
            "status: in-review\n"
            "turn: borges\n"
            "turn_since: 2026-05-28T10:00:00Z\n"
            'turn_reason: "test"\n'
            "participants: [borges, ariadne]\n"
            "---\n\n"
            "# PR-202\n\n"
            "## Turn log\n\n"
            "- 2026-05-28T10:00:00Z initialized → borges\n"
        )
        (projects_dir / "PR-202.md").write_text(content)
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "show", "PR-202")
        assert rc == 0
        # Now flip to ariadne — should work because ariadne is in participants
        rc2, stdout2, stderr2 = _run_baton(
            engram_home, projects_dir,
            "flip", "PR-202", "ariadne", "test flip",
        )
        assert rc2 == 0, f"flip with bracketed participants failed; stderr: {stderr2}"

    def test_malformed_file_skipped_in_status(self, engram_home, projects_dir):
        """Malformed .md file (no frontmatter) is silently skipped in status."""
        _write_config(engram_home, "borges")
        # Write a malformed file
        (projects_dir / "malformed.md").write_text("No frontmatter here.\n")
        # Write a valid project
        _write_project(projects_dir, "PR-203", turn="borges")
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "status")
        assert rc == 0
        assert "PR-203" in stdout
        # No crash from malformed file

    def test_turn_reason_quotes_stripped_in_status(self, engram_home, projects_dir):
        """turn_reason with surrounding quotes is displayed without quotes."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-204",
            turn="borges",
            turn_reason="round-3 fix pushed",
        )
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "status")
        assert rc == 0
        assert "round-3 fix pushed" in stdout
        # Surrounding quotes should not appear in display
        assert '""round-3 fix pushed""' not in stdout

    def test_file_with_garbage_content_skipped(self, engram_home, projects_dir):
        """File with binary garbage is silently skipped."""
        _write_config(engram_home, "borges")
        # Write binary garbage
        (projects_dir / "garbage.md").write_bytes(b"\x00\x01\x02\x03 not yaml\n")
        _write_project(projects_dir, "PR-205", turn="borges")
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "status")
        assert rc == 0
        assert "PR-205" in stdout


# ===========================================================================
# CATEGORY 4: Atomic write safety
# ===========================================================================

class TestAtomicWrite:
    """No .tmp files remain after successful operations."""

    def test_init_no_tmp_remains(self, engram_home, projects_dir):
        """After init, no .tmp file remains."""
        _write_config(engram_home, "borges")
        _run_baton(
            engram_home, projects_dir,
            "init", "PR-301",
            "--title", "atomic test",
            "--participants", "borges,ariadne",
        )
        tmp_files = list(projects_dir.glob("*.tmp"))
        assert tmp_files == [], f"Tmp files must not remain: {tmp_files}"

    def test_flip_no_tmp_remains(self, engram_home, projects_dir):
        """After flip, no .tmp file remains."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-302", turn="borges")
        _run_baton(
            engram_home, projects_dir,
            "flip", "PR-302", "ariadne", "done",
        )
        tmp_files = list(projects_dir.glob("*.tmp"))
        assert tmp_files == [], f"Tmp files must not remain: {tmp_files}"

    def test_close_no_tmp_remains(self, engram_home, projects_dir):
        """After close, no .tmp file remains."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-303", status="in-review")
        _run_baton(engram_home, projects_dir, "close", "PR-303", "--status", "merged")
        tmp_files = list(projects_dir.glob("*.tmp"))
        assert tmp_files == [], f"Tmp files must not remain: {tmp_files}"

    def test_init_creates_md_file(self, engram_home, projects_dir):
        """init creates exactly one .md file with correct name."""
        _write_config(engram_home, "borges")
        _run_baton(
            engram_home, projects_dir,
            "init", "DESIGN-trust-tier-v2",
            "--title", "trust tier design",
            "--participants", "borges,ariadne",
        )
        assert (projects_dir / "DESIGN-trust-tier-v2.md").exists()

    def test_flip_updates_file_in_place(self, engram_home, projects_dir):
        """flip modifies the same file (no new file created)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-304", turn="borges")
        before_count = len(list(projects_dir.glob("*.md")))
        _run_baton(
            engram_home, projects_dir,
            "flip", "PR-304", "ariadne", "done",
        )
        after_count = len(list(projects_dir.glob("*.md")))
        assert before_count == after_count, "flip must not create new files"


# ===========================================================================
# CATEGORY 5: Single-agent guard
# ===========================================================================

class TestSingleAgentGuard:
    """Missing projects dir → silent exit or actionable error."""

    def test_status_missing_dir_exits_0_silently(self, engram_home, tmp_path):
        """status with non-existent projects dir → exit 0, no output."""
        _write_config(engram_home, "borges")
        nonexistent = tmp_path / "no-projects-dir"
        rc, stdout, stderr = _run_baton(engram_home, nonexistent, "status")
        assert rc == 0
        assert stdout.strip() == "" or "no active" in stdout.lower()

    def test_mine_missing_dir_exits_0_silently(self, engram_home, tmp_path):
        """mine with non-existent projects dir → exit 0, no output."""
        _write_config(engram_home, "borges")
        nonexistent = tmp_path / "no-projects-dir"
        rc, stdout, stderr = _run_baton(engram_home, nonexistent, "mine")
        assert rc == 0

    def test_init_missing_dir_exits_with_error(self, engram_home, tmp_path):
        """init with non-existent projects dir → exit non-zero, actionable error."""
        _write_config(engram_home, "borges")
        nonexistent = tmp_path / "no-projects-dir"
        rc, stdout, stderr = _run_baton(
            engram_home, nonexistent,
            "init", "PR-999",
            "--title", "test",
            "--participants", "borges,ariadne",
        )
        assert rc != 0
        assert "not found" in stderr.lower() or "mkdir" in stderr

    def test_flip_missing_project_exits_1(self, engram_home, projects_dir):
        """flip referencing non-existent project ID → exit 1."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "flip", "PR-NONEXISTENT", "ariadne", "done",
        )
        assert rc == 1


# ===========================================================================
# CATEGORY 6: Integration round-trip
# ===========================================================================

class TestIntegration:
    """init → flip → mine → show → close round-trip."""

    def test_full_round_trip(self, engram_home, projects_dir):
        """Complete lifecycle: init, flip twice, mine filtering, close."""
        _write_config(engram_home, "borges")

        # Step 1: init (borges holds baton initially)
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "init", "PR-425",
            "--title", "viz_server global agent selector",
            "--participants", "borges,ariadne",
        )
        assert rc == 0
        assert (projects_dir / "PR-425.md").exists()

        # Step 2: verify borges mine shows PR-425
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "mine")
        assert rc == 0
        assert "PR-425" in stdout

        # Step 3: flip to ariadne
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "flip", "PR-425", "ariadne", "colleague-review request",
        )
        assert rc == 0

        # Step 4: borges mine should be empty
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "mine")
        assert rc == 0
        assert "PR-425" not in stdout

        # Step 5: status should still show PR-425 (it's active)
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "status")
        assert rc == 0
        assert "PR-425" in stdout

        # Step 6: show should display updated turn
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "show", "PR-425")
        assert rc == 0
        assert "ariadne" in stdout
        assert "colleague-review request" in stdout

        # Step 7: close
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "close", "PR-425", "--status", "merged",
        )
        assert rc == 0

        # Step 8: status should not show PR-425 anymore
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "status")
        assert rc == 0
        assert "PR-425" not in stdout

    def test_flip_updates_turn_field(self, engram_home, projects_dir):
        """After flip, the turn: field in frontmatter is updated."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-501", turn="borges")
        _run_baton(
            engram_home, projects_dir,
            "flip", "PR-501", "ariadne", "done step 1",
        )
        turn = _read_project_field(projects_dir, "PR-501", "turn")
        assert turn == "ariadne"

    def test_flip_updates_turn_reason(self, engram_home, projects_dir):
        """After flip, the turn_reason field reflects the new reason."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-502", turn="borges")
        _run_baton(
            engram_home, projects_dir,
            "flip", "PR-502", "ariadne", "round-3 fix pushed",
        )
        reason = _read_project_field(projects_dir, "PR-502", "turn_reason")
        assert "round-3 fix pushed" in reason

    def test_flip_appends_turn_log_entry(self, engram_home, projects_dir):
        """After flip, a turn log entry is appended to the file body."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-503", turn="borges")
        _run_baton(
            engram_home, projects_dir,
            "flip", "PR-503", "ariadne", "step complete",
        )
        content = (projects_dir / "PR-503.md").read_text()
        assert "→ ariadne" in content
        assert "step complete" in content

    def test_close_updates_status_field(self, engram_home, projects_dir):
        """After close, the status: field is updated in frontmatter."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-504", status="in-review")
        _run_baton(
            engram_home, projects_dir,
            "close", "PR-504", "--status", "merged",
        )
        status = _read_project_field(projects_dir, "PR-504", "status")
        assert status == "merged"

    def test_mine_filters_by_turn(self, engram_home, projects_dir):
        """mine only shows projects where turn == invoking agent."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-601", turn="borges", title="mine project")
        _write_project(projects_dir, "PR-602", turn="ariadne", title="not mine")
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "mine")
        assert rc == 0
        assert "PR-601" in stdout
        assert "PR-602" not in stdout

    def test_status_mine_flag_same_as_mine_subcommand(self, engram_home, projects_dir):
        """baton status --mine and baton mine produce identical output."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-603", turn="borges")
        _write_project(projects_dir, "PR-604", turn="ariadne")
        rc1, stdout1, _ = _run_baton(engram_home, projects_dir, "status", "--mine")
        rc2, stdout2, _ = _run_baton(engram_home, projects_dir, "mine")
        assert rc1 == rc2 == 0
        # Both should contain PR-603 and not PR-604
        assert "PR-603" in stdout1 and "PR-603" in stdout2
        assert "PR-604" not in stdout1 and "PR-604" not in stdout2

    def test_multiple_flips_accumulate_log_entries(self, engram_home, projects_dir):
        """Multiple flips each append their own turn log entry."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-605", turn="borges")
        _run_baton(
            engram_home, projects_dir,
            "flip", "PR-605", "ariadne", "first flip",
        )
        _run_baton(
            engram_home, projects_dir,
            "flip", "PR-605", "borges", "second flip",
        )
        content = (projects_dir / "PR-605.md").read_text()
        assert "first flip" in content
        assert "second flip" in content

    def test_status_sorted_by_turn_since(self, engram_home, projects_dir):
        """status output sorts by turn_since ascending (oldest first)."""
        _write_config(engram_home, "borges")
        # Older project
        _write_project(
            projects_dir, "PR-701", turn="borges",
            turn_since=_ts(-60),
        )
        # Newer project
        _write_project(
            projects_dir, "PR-702", turn="borges",
            turn_since=_ts(-5),
        )
        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "status")
        assert rc == 0
        idx_701 = stdout.find("PR-701")
        idx_702 = stdout.find("PR-702")
        assert idx_701 < idx_702, "Older project should appear first"

    def test_init_default_turn_is_invoking_agent(self, engram_home, projects_dir):
        """init without --turn defaults to the invoking agent."""
        _write_config(engram_home, "borges")
        _run_baton(
            engram_home, projects_dir,
            "init", "PR-801",
            "--title", "default turn test",
            "--participants", "borges,ariadne",
        )
        turn = _read_project_field(projects_dir, "PR-801", "turn")
        assert turn == "borges"

    def test_init_explicit_turn_override(self, engram_home, projects_dir):
        """init with --turn ariadne sets initial turn to ariadne."""
        _write_config(engram_home, "borges")
        _run_baton(
            engram_home, projects_dir,
            "init", "PR-802",
            "--title", "explicit turn",
            "--participants", "borges,ariadne",
            "--turn", "ariadne",
        )
        turn = _read_project_field(projects_dir, "PR-802", "turn")
        assert turn == "ariadne"

    def test_init_lei_as_participant_allowed(self, engram_home, projects_dir):
        """lei can be a participant in a project."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "init", "PR-803",
            "--title", "with lei",
            "--participants", "borges,ariadne,lei",
            "--turn", "lei",
        )
        assert rc == 0, f"init with lei failed; stderr: {stderr}"

    def test_descriptive_project_id_accepted(self, engram_home, projects_dir):
        """DESIGN-style IDs are accepted."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "init", "DESIGN-trust-tier-v2",
            "--title", "trust tier design",
            "--participants", "borges,ariadne",
        )
        assert rc == 0, f"DESIGN-ID init failed; stderr: {stderr}"
        assert (projects_dir / "DESIGN-trust-tier-v2.md").exists()


# ===========================================================================
# CATEGORY 7: rename subcommand
# ===========================================================================

class TestRename:
    """baton rename: round-trip, validation, and audit-line tests."""

    def test_rename_round_trip(self, engram_home, projects_dir):
        """Round-trip: init → rename → show, title updated, other fields unchanged."""
        _write_config(engram_home, "borges")
        # Init a project
        rc, _, stderr = _run_baton(
            engram_home, projects_dir,
            "init", "PR-901",
            "--title", "original title",
            "--participants", "borges,ariadne",
        )
        assert rc == 0, f"init failed; stderr: {stderr}"

        # Capture pre-rename state of non-title fields
        status_before = _read_project_field(projects_dir, "PR-901", "status")
        turn_before = _read_project_field(projects_dir, "PR-901", "turn")
        participants_before = _read_project_field(projects_dir, "PR-901", "participants")

        # Rename
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "rename", "PR-901",
            "--title", "new shiny title",
        )
        assert rc == 0, f"rename failed; stderr: {stderr}"
        assert '"new shiny title"' in stdout

        # Verify title updated
        title_after = _read_project_field(projects_dir, "PR-901", "title")
        assert title_after == "new shiny title"

        # Verify other fields unchanged
        assert _read_project_field(projects_dir, "PR-901", "status") == status_before
        assert _read_project_field(projects_dir, "PR-901", "turn") == turn_before
        assert _read_project_field(projects_dir, "PR-901", "participants") == participants_before

        # baton show should reflect new title
        rc, show_out, _ = _run_baton(engram_home, projects_dir, "show", "PR-901")
        assert rc == 0
        assert "new shiny title" in show_out

    def test_rename_audit_line_appended(self, engram_home, projects_dir):
        """After rename, the last body entry is the timestamped rename audit line."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-902", title="before rename")
        rc, _, stderr = _run_baton(
            engram_home, projects_dir,
            "rename", "PR-902",
            "--title", "after rename",
        )
        assert rc == 0, f"rename failed; stderr: {stderr}"
        content = (projects_dir / "PR-902.md").read_text()
        # Audit line must be present somewhere in the body
        assert 'renamed: title from "before rename" to "after rename"' in content

    def test_rename_refuse_missing_project(self, engram_home, projects_dir):
        """Rename of non-existent project exits non-zero with clear error."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "rename", "PR-NONE",
            "--title", "foo",
        )
        assert rc != 0
        assert "not found" in stderr.lower() or "PR-NONE" in stderr

    def test_rename_refuse_empty_title(self, engram_home, projects_dir):
        """Rename with empty title exits non-zero."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-903", title="original")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "rename", "PR-903",
            "--title", "",
        )
        assert rc != 0
        assert "empty" in stderr.lower() or "non-empty" in stderr.lower()

    def test_rename_refuse_whitespace_only_title(self, engram_home, projects_dir):
        """Rename with whitespace-only title exits non-zero (strip then validate)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-904", title="original")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "rename", "PR-904",
            "--title", "   ",
        )
        assert rc != 0

    def test_rename_refuse_title_with_newline(self, engram_home, projects_dir):
        """Rename with newline in title exits non-zero (YAML safety)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-905", title="original")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "rename", "PR-905",
            "--title", "line1\nline2",
        )
        assert rc != 0
        assert "newline" in stderr.lower()

    def test_rename_refuse_title_too_long(self, engram_home, projects_dir):
        """Rename with title > 200 chars exits non-zero."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-906", title="original")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "rename", "PR-906",
            "--title", "x" * 201,
        )
        assert rc != 0
        assert "long" in stderr.lower() or "200" in stderr

    def test_rename_no_tmp_remains(self, engram_home, projects_dir):
        """After rename, no .tmp file remains (atomic write hygiene)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-907", title="original")
        _run_baton(
            engram_home, projects_dir,
            "rename", "PR-907",
            "--title", "renamed",
        )
        tmp_files = list(projects_dir.glob("*.tmp"))
        assert tmp_files == [], f"Tmp files must not remain: {tmp_files}"

    def test_rename_exact_200_char_title_accepted(self, engram_home, projects_dir):
        """Title of exactly 200 chars is accepted (boundary: inclusive)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-908", title="original")
        rc, _, stderr = _run_baton(
            engram_home, projects_dir,
            "rename", "PR-908",
            "--title", "a" * 200,
        )
        assert rc == 0, f"200-char title rejected; stderr: {stderr}"


# ===========================================================================
# CATEGORY 8: claim subcommand
# ===========================================================================

class TestClaim:
    """baton claim: happy path, state-guard refusals, and audit-line tests."""

    def test_claim_happy_path(self, engram_home, projects_dir):
        """Claim from pool: turn→borges; turn_reason→claimed; audit log appended; no .tmp."""
        _write_config(engram_home, "borges")
        # Project sits in pool (turn=lei)
        _write_project(
            projects_dir, "PR-1001",
            title="pool project",
            turn="lei",
            participants=["borges", "ariadne"],
            turn_reason="released",
        )
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "claim", "PR-1001",
        )
        assert rc == 0, f"claim failed; stderr: {stderr}"

        # turn field updated to invoking agent
        turn = _read_project_field(projects_dir, "PR-1001", "turn")
        assert turn == "borges"

        # turn_reason updated to "claimed"
        reason = _read_project_field(projects_dir, "PR-1001", "turn_reason")
        assert reason == "claimed"

        # Audit log entry appended
        content = (projects_dir / "PR-1001.md").read_text()
        assert "→ borges: claimed" in content

        # No .tmp file leftover
        tmp_files = list(projects_dir.glob("*.tmp"))
        assert tmp_files == [], f"Tmp files must not remain: {tmp_files}"

    def test_claim_refuse_already_claimed_by_other(self, engram_home, projects_dir):
        """Claim when project is held by another agent → EXIT_STATE (3) with clear message."""
        _write_config(engram_home, "borges")
        # Project already claimed by ariadne (not in pool)
        _write_project(
            projects_dir, "PR-1002",
            turn="ariadne",
            participants=["borges", "ariadne"],
        )
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "claim", "PR-1002",
        )
        assert rc == 3, f"Expected EXIT_STATE=3; got rc={rc}; stderr: {stderr}"
        assert "ariadne" in stderr  # clear message names the current holder

    def test_claim_refuse_invoker_not_in_participants(self, engram_home, projects_dir):
        """Claim when invoker not in participants → EXIT_VALIDATION (1)."""
        _write_config(engram_home, "borges")
        # Pool-ready but borges not listed as participant
        _write_project(
            projects_dir, "PR-1003",
            turn="lei",
            participants=["ariadne", "mneme"],
        )
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "claim", "PR-1003",
        )
        assert rc == 1, f"Expected EXIT_VALIDATION=1; got rc={rc}; stderr: {stderr}"
        assert "participant" in stderr

    def test_claim_refuse_missing_project(self, engram_home, projects_dir):
        """Claim of non-existent project → EXIT_VALIDATION (1), not-found error."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "claim", "PR-1004",
        )
        assert rc == 1, f"Expected EXIT_VALIDATION=1; got rc={rc}; stderr: {stderr}"
        assert "not found" in stderr.lower()


# ===========================================================================
# CATEGORY 9: release subcommand
# ===========================================================================

class TestRelease:
    """baton release: happy path, state-guard refusals, --done flag tests."""

    def test_release_happy_path(self, engram_home, projects_dir):
        """Release to pool: turn→lei; default turn_reason; audit log appended; no .tmp."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1101",
            title="claimed project",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "release", "PR-1101",
        )
        assert rc == 0, f"release failed; stderr: {stderr}"

        # turn field updated to pool sentinel
        turn = _read_project_field(projects_dir, "PR-1101", "turn")
        assert turn == "lei"

        # turn_reason defaults to "released"
        reason = _read_project_field(projects_dir, "PR-1101", "turn_reason")
        assert reason == "released"

        # Audit log entry appended
        content = (projects_dir / "PR-1101.md").read_text()
        assert "→ lei: released" in content

        # No .tmp file leftover
        tmp_files = list(projects_dir.glob("*.tmp"))
        assert tmp_files == [], f"Tmp files must not remain: {tmp_files}"

    def test_release_custom_reason(self, engram_home, projects_dir):
        """Release with --reason writes the provided reason to turn_reason."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1102",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "release", "PR-1102",
            "--reason", "stepping back for review",
        )
        assert rc == 0, f"release failed; stderr: {stderr}"

        reason = _read_project_field(projects_dir, "PR-1102", "turn_reason")
        assert "stepping back for review" in reason

    def test_release_refuse_non_holder(self, engram_home, projects_dir):
        """Release when invoker is not current holder → EXIT_STATE (3)."""
        _write_config(engram_home, "borges")
        # ariadne holds the baton, not borges
        _write_project(
            projects_dir, "PR-1103",
            turn="ariadne",
            participants=["borges", "ariadne"],
        )
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "release", "PR-1103",
        )
        assert rc == 3, f"Expected EXIT_STATE=3; got rc={rc}; stderr: {stderr}"
        assert "ariadne" in stderr  # message names the actual holder

    def test_release_done_appends_to_title(self, engram_home, projects_dir):
        """release --done appends '(done)' to the project title."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1104",
            title="cli gap work",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "release", "PR-1104",
            "--done",
        )
        assert rc == 0, f"release --done failed; stderr: {stderr}"

        title = _read_project_field(projects_dir, "PR-1104", "title")
        assert title == "cli gap work (done)"

    def test_release_done_idempotent(self, engram_home, projects_dir):
        """release --done on already-(done) title does not double-append."""
        _write_config(engram_home, "borges")
        # Title already ends with (done)
        _write_project(
            projects_dir, "PR-1105",
            title="cli gap work (done)",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "release", "PR-1105",
            "--done",
        )
        assert rc == 0, f"release --done on (done) title failed; stderr: {stderr}"

        title = _read_project_field(projects_dir, "PR-1105", "title")
        assert title == "cli gap work (done)", (
            f"Expected '(done)' not doubled; got: {title!r}"
        )
