"""Tests for tools/baton.py — multi-agent turn-state CLI.

Test categories:
  - Smoke:          each subcommand happy path exits 0
  - Validation:     bad inputs rejected with expected exit codes / errors
  - Frontmatter:    parser handles edge cases, malformed files, quotes
  - SingleAgent:    missing projects dir → silent exit or actionable error
  - Integration:    init → flip → mine → show → close round-trip
  - CI-state dedup: _dedupe_checks_by_latest + _pr_ci_state (#1366)

Write commands (init, flip, close, reopen, rename, claim, release, anchor) are
tested via direct module calls + a mock ForumClient — no live forum server
needed.  Read commands (status, mine, show) use subprocess as before.

Phase 3 migration note: baton.py write commands now call the forum API rather
than writing directly to disk.  Tests that previously asserted on on-disk state
after a write are ported to assert on the mock client's post call_args instead.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

CLI_PATH = Path(__file__).parent.parent / "tools" / "baton.py"

# ---------------------------------------------------------------------------
# Module-level baton import for direct-call tests (same pattern as
# src/forum/tests/test_baton_thin_client.py).  Adding tools/ to sys.path
# once here covers both baton.py and forum_api.py.
# ---------------------------------------------------------------------------
_TOOLS_DIR = str(Path(__file__).parent.parent / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import baton as _baton  # noqa: E402 (after sys.path insert)


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


def _make_client(return_value=None) -> MagicMock:
    """Return a mock ForumClient whose post() returns the given dict."""
    client = MagicMock()
    client.post.return_value = return_value or {"seq": 42}
    return client


def _write_config(
    engram_home: Path,
    agent_name: str = "borges",
    primary_user: str = "casey",
) -> None:
    """Write a minimal config.json into engram_home.

    primary_user defaults to "casey" so that the pool sentinel resolves to
    "casey" on all existing tests (issue #606 fix — the sentinel is now
    read from config.json, not hardcoded).
    """
    cfg = {"mode": "multi", "agent_name": agent_name, "primary_user": primary_user}
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
    import re
    path = projects_dir / f"{project_id}.md"
    text = path.read_text()
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

    def test_init_exits_0(self, engram_home, projects_dir, monkeypatch):
        """init happy path: API is called and no exception is raised."""
        _write_config(engram_home, "borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-999",
            title="smoke test",
            participants="borges,ariadne",
            status="planning",
            turn=None,
            github=None,
            colleague=None,
        )
        _baton.cmd_init(args, {}, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects"
        assert body["project_id"] == "PR-999"

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

    def test_flip_exits_0(self, engram_home, projects_dir, monkeypatch):
        """flip happy path: API is called and no exception is raised."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-400", turn="borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-400",
            to="ariadne",
            reason="done step 1",
            force=False,
        )
        _baton.cmd_flip(args, {}, "borges", client)
        client.post.assert_called_once()

    def test_close_exits_0(self, engram_home, projects_dir, monkeypatch):
        """close happy path: API is called and no exception is raised."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-500", status="in-review")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-500", status="merged")
        _baton.cmd_close(args, {}, "borges", client)
        client.post.assert_called_once()


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

    def test_flip_to_casey_allowed(self, engram_home, projects_dir, monkeypatch):
        """flip to 'casey' (pool sentinel) is always valid regardless of participants list."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-104", participants=["borges", "ariadne"])
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-104",
            to="casey",
            reason="need human decision",
            force=False,
        )
        # Should not raise — casey is the pool sentinel and is always a valid recipient
        _baton.cmd_flip(args, {}, "borges", client)
        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["to_agent"] == "casey"

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
        """init with --turn not in participants (and not casey) → exit 1."""
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

    def test_participants_brackets_parsed(self, engram_home, projects_dir, monkeypatch):
        """Participants in [alice, bob] format are parsed correctly for flip validation."""
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

        # flip: port to direct call + mock — validates that bracketed participants
        # are correctly parsed so ariadne is accepted as a valid flip target.
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-202",
            to="ariadne",
            reason="test flip",
            force=False,
        )
        # Should not raise — proves ariadne was found in bracketed participants
        _baton.cmd_flip(args, {}, "borges", client)
        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["to_agent"] == "ariadne"

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

    @pytest.mark.skip(
        reason="requires live forum server; multi-step state chain can't be "
               "reproduced with mocks alone (mine/status/show read server-written files). "
               "Individual write operations are covered by the other Integration tests."
    )
    def test_full_round_trip(self, engram_home, projects_dir):
        """Complete lifecycle: init, flip twice, mine filtering, close."""

    def test_flip_updates_turn_field(self, engram_home, projects_dir, monkeypatch):
        """After flip, the API receives to_agent=ariadne (server updates turn field)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-501", turn="borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-501", to="ariadne", reason="done step 1", force=False,
        )
        _baton.cmd_flip(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert body["to_agent"] == "ariadne"

    def test_flip_updates_turn_reason(self, engram_home, projects_dir, monkeypatch):
        """After flip, the API receives the provided reason (server updates turn_reason)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-502", turn="borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-502", to="ariadne", reason="round-3 fix pushed", force=False,
        )
        _baton.cmd_flip(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert "round-3 fix pushed" in body["reason"]

    def test_flip_appends_turn_log_entry(self, engram_home, projects_dir, monkeypatch):
        """After flip, the API is called with to_agent and reason (server appends turn log)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-503", turn="borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-503", to="ariadne", reason="step complete", force=False,
        )
        _baton.cmd_flip(args, {}, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-503/flip"
        assert body["to_agent"] == "ariadne"
        assert "step complete" in body["reason"]

    def test_close_updates_status_field(self, engram_home, projects_dir, monkeypatch):
        """After close, the API receives new_status=merged (server updates status field)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-504", status="in-review")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-504", status="merged")
        _baton.cmd_close(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert body["new_status"] == "merged"

    def test_close_appends_turn_log_entry(self, engram_home, projects_dir, monkeypatch):
        """baton close calls the API with status=merged (server appends audit log entry)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-505", status="in-review")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-505", status="merged")
        _baton.cmd_close(args, {}, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-505/status"
        assert body["new_status"] == "merged"
        assert "agent" in body

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

    def test_multiple_flips_accumulate_log_entries(self, engram_home, projects_dir, monkeypatch):
        """Multiple flip API calls are each made with their distinct reasons."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-605", turn="borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client({"seq": 1})

        # First flip borges → ariadne
        args1 = argparse.Namespace(
            project_id="PR-605", to="ariadne", reason="first flip", force=False,
        )
        _baton.cmd_flip(args1, {}, "borges", client)

        # Simulate server write so the file reflects ariadne as turn holder for next flip
        _write_project(projects_dir, "PR-605", turn="ariadne")

        # Second flip ariadne → borges
        args2 = argparse.Namespace(
            project_id="PR-605", to="borges", reason="second flip", force=False,
        )
        _baton.cmd_flip(args2, {}, "ariadne", client)

        assert client.post.call_count == 2
        first_body = client.post.call_args_list[0][0][1]
        second_body = client.post.call_args_list[1][0][1]
        assert first_body["reason"] == "first flip"
        assert second_body["reason"] == "second flip"

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

    def test_init_default_turn_is_invoking_agent(self, engram_home, projects_dir, monkeypatch):
        """init without --turn posts body with turn=borges (the invoking agent)."""
        _write_config(engram_home, "borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-801",
            title="default turn test",
            participants="borges,ariadne",
            status="planning",
            turn=None,
            github=None,
            colleague=None,
        )
        _baton.cmd_init(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert body["turn"] == "borges"

    def test_init_explicit_turn_override(self, engram_home, projects_dir, monkeypatch):
        """init with --turn ariadne posts body with turn=ariadne."""
        _write_config(engram_home, "borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-802",
            title="explicit turn",
            participants="borges,ariadne",
            status="planning",
            turn="ariadne",
            github=None,
            colleague=None,
        )
        _baton.cmd_init(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert body["turn"] == "ariadne"

    def test_init_casey_as_participant_allowed(self, engram_home, projects_dir, monkeypatch):
        """casey can be a participant and initial turn holder — API is called."""
        _write_config(engram_home, "borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-803",
            title="with casey",
            participants="borges,ariadne,casey",
            status="planning",
            turn="casey",
            github=None,
            colleague=None,
        )
        _baton.cmd_init(args, {}, "borges", client)
        client.post.assert_called_once()

    def test_descriptive_project_id_accepted(self, engram_home, projects_dir, monkeypatch):
        """DESIGN-style IDs are accepted and posted to the API."""
        _write_config(engram_home, "borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="DESIGN-trust-tier-v2",
            title="trust tier design",
            participants="borges,ariadne",
            status="planning",
            turn=None,
            github=None,
            colleague=None,
        )
        _baton.cmd_init(args, {}, "borges", client)
        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["project_id"] == "DESIGN-trust-tier-v2"


# ===========================================================================
# CATEGORY 7: rename subcommand
# ===========================================================================

class TestRename:
    """baton rename: round-trip, validation, and audit-line tests."""

    def test_rename_round_trip(self, engram_home, projects_dir, monkeypatch):
        """init → rename: the rename API is called with the new title."""
        _write_config(engram_home, "borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})

        # Step 1: init (API call, no file written)
        args_init = argparse.Namespace(
            project_id="PR-901",
            title="original title",
            participants="borges,ariadne",
            status="planning",
            turn=None,
            github=None,
            colleague=None,
        )
        _baton.cmd_init(args_init, {}, "borges", client)
        client.post.assert_called_once()

        # Simulate server write so rename can read and parse the file
        _write_project(projects_dir, "PR-901", title="original title", turn="borges",
                       status="planning")

        # Step 2: rename
        client.reset_mock()
        args_rename = argparse.Namespace(project_id="PR-901", title="new shiny title")
        _baton.cmd_rename(args_rename, {}, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-901/rename"
        assert body["new_title"] == "new shiny title"

    def test_rename_audit_line_appended(self, engram_home, projects_dir, monkeypatch):
        """After rename, the API is called with the new title (server appends audit line)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-902", title="before rename")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-902", title="after rename")
        _baton.cmd_rename(args, {}, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-902/rename"
        assert body["new_title"] == "after rename"

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

    def test_rename_exact_200_char_title_accepted(self, engram_home, projects_dir, monkeypatch):
        """Title of exactly 200 chars is accepted and reaches the API."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-908", title="original")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-908", title="a" * 200)
        _baton.cmd_rename(args, {}, "borges", client)

        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert len(body["new_title"]) == 200


# ===========================================================================
# CATEGORY 8: claim subcommand
# ===========================================================================

class TestClaim:
    """baton claim: happy path, state-guard refusals, and audit-line tests."""

    def test_claim_happy_path(self, engram_home, projects_dir, monkeypatch):
        """Claim posts to the claim route with agent=borges and pool_sentinel=casey."""
        _write_config(engram_home, "borges")
        # Project sits in pool (turn=casey)
        _write_project(
            projects_dir, "PR-1001",
            title="pool project",
            turn="casey",
            participants=["borges", "ariadne"],
            turn_reason="released",
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n == "casey")

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-1001")
        _baton.cmd_claim(args, {}, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1001/claim"
        assert body["agent"] == "borges"
        assert body["pool_sentinel"] == "casey"

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
            turn="casey",
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

    def test_release_happy_path(self, engram_home, projects_dir, monkeypatch):
        """Release posts to the release route with pool_sentinel=casey."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1101",
            title="claimed project",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-1101", done=False, reason="released")
        _baton.cmd_release(args, {}, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1101/release"
        assert body["pool_sentinel"] == "casey"
        assert body["reason"] == "released"
        assert body["done"] is False

    def test_release_custom_reason(self, engram_home, projects_dir, monkeypatch):
        """Release with --reason passes the provided reason in the POST body."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1102",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-1102", done=False, reason="stepping back for review",
        )
        _baton.cmd_release(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert "stepping back for review" in body["reason"]

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

    def test_release_done_appends_to_title(self, engram_home, projects_dir, monkeypatch):
        """release --done passes done=True in POST body (server appends '(done)' to title)."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1104",
            title="cli gap work",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-1104", done=True, reason="released")
        _baton.cmd_release(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert body["done"] is True

    def test_release_done_idempotent(self, engram_home, projects_dir, monkeypatch):
        """release --done on already-(done) title passes done=True (server handles idempotency)."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1105",
            title="cli gap work (done)",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-1105", done=True, reason="released")
        _baton.cmd_release(args, {}, "borges", client)

        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["done"] is True


# ===========================================================================
# CATEGORY 10: anchor subcommand + --github init flag
# ===========================================================================

# GitHub anchor pattern that the hook's _resolve_anchor expects.
# Mirrors ^(pr|project)/\d+$ — do NOT import the hook directly.
import re as _re
_GITHUB_ANCHOR_PATTERN = _re.compile(r"^(pr|project)/\d+$")


class TestAnchor:
    """baton anchor subcommand and baton init --github flag."""

    def test_init_github_pr_writes_frontmatter(self, engram_home, projects_dir, monkeypatch):
        """init --github passes the anchor in the API body (server writes frontmatter)."""
        _write_config(engram_home, "borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-env-home",
            title="env home test",
            participants="borges,ariadne",
            status="planning",
            turn=None,
            github="pr/123",
            colleague=None,
        )
        _baton.cmd_init(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert body["github"] == "pr/123"
        # Confirm the value matches the hook's expected format
        assert _GITHUB_ANCHOR_PATTERN.match(body["github"]), (
            f"Posted github value '{body['github']}' does not match hook's anchor pattern"
        )

    def test_init_github_pr_reflected_in_show(self, engram_home, projects_dir, monkeypatch):
        """init --github is sent to the API; baton show reflects the field when file exists."""
        _write_config(engram_home, "borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-show-test",
            title="show test",
            participants="borges,ariadne",
            status="planning",
            turn=None,
            github="pr/456",
            colleague=None,
        )
        _baton.cmd_init(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert body["github"] == "pr/456"

        # Simulate server write with the github field so that show can display it
        content = (
            "---\n"
            "project: PR-show-test\n"
            "title: show test\n"
            "status: planning\n"
            "turn: borges\n"
            "turn_since: 2026-01-01T00:00:00Z\n"
            'turn_reason: "initialized"\n'
            "participants: [borges, ariadne]\n"
            "github: pr/456\n"
            "---\n\n"
            "# PR-show-test\n\n"
            "## Turn log\n\n"
            "- 2026-01-01T00:00:00Z initialized\n"
        )
        (projects_dir / "PR-show-test.md").write_text(content)

        rc, stdout, stderr = _run_baton(engram_home, projects_dir, "show", "PR-show-test")
        assert rc == 0
        assert "pr/456" in stdout

    def test_init_github_rejects_malformed_value(self, engram_home, projects_dir):
        """init --github with malformed value exits non-zero."""
        _write_config(engram_home, "borges")
        for bad_val in ("xyz", "pr/abc", "pr/", "project/", "123"):
            rc, stdout, stderr = _run_baton(
                engram_home, projects_dir,
                "init", "PR-bad-anchor",
                "--title", "bad anchor test",
                "--participants", "borges,ariadne",
                "--github", bad_val,
            )
            assert rc != 0, (
                f"Expected non-zero exit for --github '{bad_val}'; got rc={rc}"
            )

    def test_anchor_sets_github_on_existing_baton(self, engram_home, projects_dir, monkeypatch):
        """Round-trip: init → anchor: the anchor API is called with github=project/4."""
        _write_config(engram_home, "borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})

        # Step 1: init without --github
        args_init = argparse.Namespace(
            project_id="pool-data-correctness",
            title="data correctness work",
            participants="borges,ariadne",
            status="planning",
            turn=None,
            github=None,
            colleague=None,
        )
        _baton.cmd_init(args_init, {}, "borges", client)
        init_body = client.post.call_args[0][1]
        assert init_body.get("github") is None

        # Simulate server write (so anchor can read/parse the file)
        _write_project(
            projects_dir, "pool-data-correctness",
            title="data correctness work",
            turn="borges",
            status="planning",
        )

        # Step 2: set anchor
        client.reset_mock()
        args_anchor = argparse.Namespace(
            project_id="pool-data-correctness", github="project/4",
        )
        _baton.cmd_anchor(args_anchor, {}, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/pool-data-correctness/anchor"
        github_val = body["github"]
        assert github_val == "project/4"
        assert _GITHUB_ANCHOR_PATTERN.match(github_val), (
            f"Posted github value '{github_val}' does not match hook's anchor pattern"
        )

    def test_anchor_updates_existing_github_field(self, engram_home, projects_dir, monkeypatch):
        """anchor on a baton that already has github: field calls API with new value."""
        _write_config(engram_home, "borges")
        # Write file with existing github field (simulating a server-written file)
        content = (
            "---\n"
            "project: PR-update-anchor\n"
            "title: update anchor test\n"
            "status: in-progress\n"
            "turn: borges\n"
            "turn_since: 2026-01-01T00:00:00Z\n"
            'turn_reason: "test"\n'
            "participants: [borges, ariadne]\n"
            "github: pr/100\n"
            "---\n\n"
            "# PR-update-anchor\n\n"
            "## Turn log\n\n"
            "- 2026-01-01T00:00:00Z initialized\n"
        )
        (projects_dir / "PR-update-anchor.md").write_text(content)
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-update-anchor", github="pr/200")
        _baton.cmd_anchor(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert body["github"] == "pr/200"

    def test_anchor_missing_project_exits_nonzero(self, engram_home, projects_dir):
        """anchor on a non-existent project exits non-zero."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "anchor", "PR-NONE",
            "--github", "pr/99",
        )
        assert rc != 0
        assert "not found" in stderr.lower() or "PR-NONE" in stderr

    def test_anchor_rejects_malformed_value(self, engram_home, projects_dir):
        """anchor with malformed --github value exits non-zero."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-1201", turn="borges")
        for bad_val in ("xyz", "pr/abc", "github.com/org/repo", "notaformat"):
            rc, stdout, stderr = _run_baton(
                engram_home, projects_dir,
                "anchor", "PR-1201",
                "--github", bad_val,
            )
            assert rc != 0, (
                f"Expected non-zero exit for --github '{bad_val}'; got rc={rc}"
            )

    def test_anchor_audit_line_appended(self, engram_home, projects_dir, monkeypatch):
        """anchor calls the API (server appends audit line to the turn log)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-1202", turn="borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-1202", github="pr/77")
        _baton.cmd_anchor(args, {}, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1202/anchor"
        assert body["github"] == "pr/77"

    def test_anchor_no_tmp_remains(self, engram_home, projects_dir):
        """After anchor, no .tmp file remains (atomic write hygiene)."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-1203", turn="borges")
        _run_baton(
            engram_home, projects_dir,
            "anchor", "PR-1203",
            "--github", "pr/55",
        )
        tmp_files = list(projects_dir.glob("*.tmp"))
        assert tmp_files == [], f"Tmp files must not remain: {tmp_files}"

    def test_init_without_github_does_not_write_github_field(
        self, engram_home, projects_dir, monkeypatch,
    ):
        """init without --github posts no github field (or null) to the API."""
        _write_config(engram_home, "borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-no-github",
            title="no anchor",
            participants="borges,ariadne",
            status="planning",
            turn=None,
            github=None,
            colleague=None,
        )
        _baton.cmd_init(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        github_val = body.get("github")
        assert github_val is None, (
            f"Expected no github field (None); got: '{github_val}'"
        )

    def test_anchor_github_project_format(self, engram_home, projects_dir, monkeypatch):
        """anchor with project/<N> value is accepted and posted to the API correctly."""
        _write_config(engram_home, "borges")
        _write_project(projects_dir, "PR-1204", turn="borges")
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-1204", github="project/4")
        _baton.cmd_anchor(args, {}, "borges", client)

        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        github_val = body["github"]
        assert github_val == "project/4"
        assert _GITHUB_ANCHOR_PATTERN.match(github_val)


# ===========================================================================
# CATEGORY 11: reopen subcommand
# ===========================================================================

class TestReopen:
    """baton reopen: happy path, state-guard refusals, audit-line, and argparse tests."""

    def test_reopen_merged_baton_to_in_progress(self, engram_home, projects_dir, monkeypatch):
        """Reopen a merged baton: API is called with new_status=in-progress."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1301",
            title="reopened project",
            status="merged",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-1301", status="in-progress")
        _baton.cmd_reopen(args, {}, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1301/status"
        assert body["new_status"] == "in-progress"

    def test_reopen_cancelled_baton_to_in_progress(
        self, engram_home, projects_dir, monkeypatch,
    ):
        """Reopen a cancelled baton: API is called with new_status=in-progress."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1302",
            status="cancelled",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-1302", status="in-progress")
        _baton.cmd_reopen(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert body["new_status"] == "in-progress"

    def test_reopen_with_explicit_status_flag(self, engram_home, projects_dir, monkeypatch):
        """Reopen with --status in-review: API called with new_status=in-review."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1303",
            status="merged",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-1303", status="in-review")
        _baton.cmd_reopen(args, {}, "borges", client)

        _, body = client.post.call_args[0]
        assert body["new_status"] == "in-review"

    def test_reopen_refuses_already_active(self, engram_home, projects_dir):
        """Reopen on already-active baton → exit 1 with message naming current status."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1304",
            status="in-progress",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "reopen", "PR-1304",
        )
        assert rc == 1, f"Expected exit 1 for already-active baton; got rc={rc}"
        assert "in-progress" in stderr
        assert "not closed" in stderr or "already" in stderr.lower() or "flip" in stderr

    def test_reopen_refuses_invalid_target_status(self, engram_home, projects_dir):
        """Reopen with --status merged (closed status) → argparse rejects."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1305",
            status="merged",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "reopen", "PR-1305", "--status", "merged",
        )
        # argparse rejects invalid choices; stderr names the bad value
        assert rc != 0
        assert "invalid choice" in stderr or "merged" in stderr

    def test_reopen_with_status_planning(self, engram_home, projects_dir, monkeypatch):
        """Reopen with --status planning calls API with new_status=planning."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1306",
            status="merged",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-1306", status="planning")
        _baton.cmd_reopen(args, {}, "borges", client)

        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["new_status"] == "planning"

    def test_reopen_refuses_missing_file(self, engram_home, projects_dir):
        """Reopen of non-existent project → exit 1 with not-found error."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "reopen", "PR-NONE",
        )
        assert rc == 1, f"Expected exit 1 for missing project; got rc={rc}"
        assert "not found" in stderr.lower() or "PR-NONE" in stderr

    def test_reopen_appends_audit_log(self, engram_home, projects_dir, monkeypatch):
        """reopen API call includes a reason with the status transition (server appends audit)."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1306",
            status="merged",
            turn="borges",
            participants=["borges", "ariadne"],
            turn_reason="merged successfully",
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-1306", status="in-progress")
        _baton.cmd_reopen(args, {}, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1306/status"
        # cmd_reopen sets reason = f"reopened by {invoker}: status was {old} → {new}"
        assert "reopened" in body["reason"]
        assert "borges" in body["reason"]
        assert "merged" in body["reason"]
        assert "in-progress" in body["reason"]

    def test_reopen_no_tmp_remains(self, engram_home, projects_dir):
        """After reopen, no .tmp file remains (atomic write hygiene)."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1307",
            status="cancelled",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        _run_baton(
            engram_home, projects_dir,
            "reopen", "PR-1307",
        )
        tmp_files = list(projects_dir.glob("*.tmp"))
        assert tmp_files == [], f"Tmp files must not remain: {tmp_files}"

    def test_reopen_output_message_style(self, engram_home, projects_dir, monkeypatch, capsys):
        """reopen prints a confirmation line with project ID and new status."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1308",
            status="merged",
            turn="borges",
            participants=["borges", "ariadne"],
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))

        client = _make_client({"seq": 1})
        args = argparse.Namespace(project_id="PR-1308", status="in-progress")
        _baton.cmd_reopen(args, {}, "borges", client)

        captured = capsys.readouterr()
        assert "PR-1308" in captured.out
        assert "in-progress" in captured.out


# ===========================================================================
# CATEGORY 13: Pool sentinel resolution (issue #606)
# ===========================================================================

class TestPoolSentinel:
    """Pool sentinel is derived from config primary_user, not hardcoded."""

    def test_sentinel_resolves_to_non_casey_primary_user(
        self, engram_home, projects_dir, monkeypatch,
    ):
        """With primary_user='alice', pool sentinel is 'alice' — claim/release use alice.

        This is the core fresh-install correctness test from issue #606:
        a non-casey primary_user install must work end-to-end.
        Ported to direct call + mock; we verify pool_sentinel appears in API bodies.
        """
        _write_config(engram_home, agent_name="borges", primary_user="alice")
        _write_project(
            projects_dir, "pool-alice-test",
            title="non-casey sentinel test",
            turn="alice",
            participants=["borges", "ariadne"],
            status="in-progress",
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n in ("alice",))

        # Claim: _pool_sentinel() must read config → "alice" (not "casey")
        client = _make_client({"seq": 1})
        args_claim = argparse.Namespace(project_id="pool-alice-test")
        _baton.cmd_claim(args_claim, {}, "borges", client)

        _, claim_body = client.post.call_args[0]
        assert claim_body["pool_sentinel"] == "alice", (
            f"Expected pool_sentinel='alice'; got '{claim_body['pool_sentinel']}'"
        )
        assert "casey" not in claim_body["pool_sentinel"], (
            "pool_sentinel must not fall back to 'casey' for an alice install"
        )

        # Simulate server write (borges now holds baton)
        _write_project(
            projects_dir, "pool-alice-test",
            turn="borges",
            participants=["borges", "ariadne"],
        )

        # Release: sentinel should still be "alice"
        client.reset_mock()
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        args_release = argparse.Namespace(
            project_id="pool-alice-test", done=False, reason="released",
        )
        _baton.cmd_release(args_release, {}, "borges", client)

        _, release_body = client.post.call_args[0]
        assert release_body["pool_sentinel"] == "alice", (
            f"Expected pool_sentinel='alice' in release; got '{release_body['pool_sentinel']}'"
        )

    def test_sentinel_case_normalizes_capitalized_primary_user(
        self, engram_home, projects_dir, monkeypatch,
    ):
        """primary_user='Alice' (capitalized) in config normalizes to sentinel 'alice'.

        Regression test for PR #612 round-2: _pool_sentinel() used to return
        the raw config string, so a capitalized primary_user would silently
        fail all sentinel comparisons.  The fix applies .lower() in _pool_sentinel().
        Ported to direct call + mock; we verify pool_sentinel='alice' (lowercased).
        """
        _write_config(engram_home, agent_name="borges", primary_user="Alice")
        _write_project(
            projects_dir, "pool-alice-cap-test",
            title="capitalized primary_user test",
            turn="alice",  # lowercase — the bug trigger
            participants=["borges", "ariadne"],
            status="in-progress",
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n in ("alice",))

        # Claim: sentinel must resolve to lowercase "alice" despite "Alice" in config
        client = _make_client({"seq": 1})
        args_claim = argparse.Namespace(project_id="pool-alice-cap-test")
        _baton.cmd_claim(args_claim, {}, "borges", client)

        _, claim_body = client.post.call_args[0]
        assert claim_body["pool_sentinel"] == "alice", (
            f"Expected lowercased sentinel 'alice'; got '{claim_body['pool_sentinel']}'"
        )

        # Simulate server write
        _write_project(
            projects_dir, "pool-alice-cap-test",
            turn="borges",
            participants=["borges", "ariadne"],
        )

        # Release: sentinel still lowercase "alice"
        client.reset_mock()
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        args_release = argparse.Namespace(
            project_id="pool-alice-cap-test", done=False, reason="released",
        )
        _baton.cmd_release(args_release, {}, "borges", client)

        _, release_body = client.post.call_args[0]
        assert release_body["pool_sentinel"] == "alice", (
            f"Expected lowercased sentinel 'alice'; got '{release_body['pool_sentinel']}'"
        )

    def test_sentinel_accepts_short_form_first_token(
        self, engram_home, projects_dir, monkeypatch,
    ):
        """Pool initialized with turn='lei' works when primary_user='Lei Shi'.

        Regression test for issue #1309: _pool_sentinel() returns 'lei shi',
        but pools are often initialized with just the first token ('lei').
        _is_pool_sentinel must accept both forms.
        Ported to direct call + mock; we verify claim succeeds and the full
        sentinel appears in the release body.
        """
        _write_config(engram_home, agent_name="borges", primary_user="Lei Shi")
        _write_project(
            projects_dir, "pool-short-form-test",
            title="short-form sentinel test",
            turn="lei",  # short form — the bug trigger
            participants=["borges", "ariadne"],
            status="in-progress",
        )
        monkeypatch.setattr(_baton, "BATON_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))

        # Claim: _is_pool_sentinel("lei") must return True for sentinel "lei shi"
        client = _make_client({"seq": 1})
        args_claim = argparse.Namespace(project_id="pool-short-form-test")
        _baton.cmd_claim(args_claim, {}, "borges", client)

        _, claim_body = client.post.call_args[0]
        assert claim_body["agent"] == "borges"
        assert claim_body["pool_sentinel"] == "lei shi", (
            f"Expected full sentinel 'lei shi'; got '{claim_body['pool_sentinel']}'"
        )

        # Simulate server write (borges holds baton after claim)
        _write_project(
            projects_dir, "pool-short-form-test",
            turn="borges",
            participants=["borges", "ariadne"],
        )

        # Release: sentinel must be full "lei shi"
        client.reset_mock()
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        args_release = argparse.Namespace(
            project_id="pool-short-form-test", done=False, reason="released",
        )
        _baton.cmd_release(args_release, {}, "borges", client)

        _, release_body = client.post.call_args[0]
        assert release_body["pool_sentinel"] == "lei shi", (
            f"Expected full sentinel 'lei shi' after release; got '{release_body['pool_sentinel']}'"
        )

    def test_merge_gate2_accepts_short_form_sentinel(
        self, engram_home, projects_dir
    ):
        """baton merge Gate 2 accepts short-form sentinel 'lei' with primary_user='Lei Shi'.

        Regression test for the missed comparison site in cmd_merge (issue #1309):
        Gate 2 used raw != sentinel instead of _is_pool_sentinel, so a PR baton
        with turn='lei' (short form, written by baton flip) would block merge
        with "turn is with lei — not presented for merge."
        """
        _write_config(engram_home, agent_name="borges", primary_user="Lei Shi")

        # Baton with short-form sentinel in turn (the bug trigger).
        # No github: anchor → Gates 3/4 (CI + approval) are skipped automatically.
        _write_project(
            projects_dir, "PR-9999",
            title="short-form merge gate test",
            turn="lei",
            participants=["borges", "ariadne"],
            status="in-progress",
        )

        # --dry-run --force: skips CI (Gate 3) + approval (Gate 4); no real gh call.
        # Should pass Gate 2 without "not presented for merge" error.
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "merge", "PR-9999", "--dry-run", "--force",
        )
        assert rc == 0, (
            f"merge --dry-run failed at Gate 2 for short-form sentinel 'lei'; "
            f"stderr: {stderr}"
        )
        assert "gate 2 (turn=sentinel): PASS" in stdout, (
            f"Expected Gate 2 PASS in dry-run output; got: {stdout!r}"
        )

    def test_missing_config_no_env_fails_loud(self, engram_home, projects_dir):
        """Missing config.json + no BATON_POOL_SENTINEL → exit non-zero with message.

        Any operation that resolves the pool sentinel must fail loud (not
        silently return a wrong name) when the sentinel cannot be determined.
        """
        # Do NOT write config.json — engram_home exists but has no config file.
        _write_project(
            projects_dir, "pool-no-config",
            title="no config test",
            turn="someone",
            participants=["borges", "ariadne"],
            status="in-progress",
        )

        # claim triggers sentinel resolution; must fail loud.
        rc, stdout, stderr = _run_baton(
            engram_home, projects_dir,
            "claim", "pool-no-config",
            env_overrides={"BATON_POOL_SENTINEL": ""},  # empty → not set
        )
        assert rc != 0, (
            f"Expected non-zero exit when config missing; got rc={rc}"
        )
        assert "sentinel" in stderr.lower() or "primary_user" in stderr.lower(), (
            f"Expected error message mentioning sentinel/primary_user; got: {stderr!r}"
        )


# ===========================================================================
# CATEGORY 12: CI-state dedup helper + mergeStateStatus=CLEAN short-circuit (#1366)
# ===========================================================================

import importlib.util as _ilu
import unittest.mock as _mock


def _load_baton_module():
    spec = _ilu.spec_from_file_location("baton", CLI_PATH)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCiStateDedup:
    """Unit tests for _dedupe_checks_by_latest and the _pr_ci_state fix (#1366).

    Root cause: statusCheckRollup contains ALL runs including superseded ones.
    A stale FAILURE from a pre-push commit falsely blocked baton flip even when
    the latest run of the same check was SUCCESS.
    """

    @pytest.fixture
    def baton(self):
        return _load_baton_module()

    def test_dedupe_keeps_latest_by_completed_at(self, baton):
        checks = [
            {"name": "trap-check", "conclusion": "FAILURE", "completedAt": "2026-06-24T10:00:00Z"},
            {"name": "trap-check", "conclusion": "SUCCESS", "completedAt": "2026-06-24T12:00:00Z"},
        ]
        result = baton._dedupe_checks_by_latest(checks)
        assert len(result) == 1
        assert result[0]["conclusion"] == "SUCCESS"

    def test_dedupe_null_completed_at_loses_to_finished(self, baton):
        # In-progress re-run (null completedAt) should not displace a finished run,
        # regardless of which ordering they appear in the list.
        checks_finished_first = [
            {"name": "ci", "conclusion": "SUCCESS", "completedAt": "2026-06-24T12:00:00Z"},
            {"name": "ci", "conclusion": None, "completedAt": None},
        ]
        result = baton._dedupe_checks_by_latest(checks_finished_first)
        assert len(result) == 1
        assert result[0]["conclusion"] == "SUCCESS"

        checks_null_first = [
            {"name": "ci", "conclusion": None, "completedAt": None},
            {"name": "ci", "conclusion": "SUCCESS", "completedAt": "2026-06-24T12:00:00Z"},
        ]
        result = baton._dedupe_checks_by_latest(checks_null_first)
        assert len(result) == 1
        assert result[0]["conclusion"] == "SUCCESS"

    def test_dedupe_preserves_distinct_names(self, baton):
        checks = [
            {"name": "manifest-gate", "conclusion": "SUCCESS", "completedAt": "2026-06-24T12:00:00Z"},
            {"name": "trap-check",    "conclusion": "SUCCESS", "completedAt": "2026-06-24T12:01:00Z"},
        ]
        result = baton._dedupe_checks_by_latest(checks)
        assert len(result) == 2

    def test_pr_ci_state_clean_short_circuits(self, baton):
        # mergeStateStatus=CLEAN should return green immediately, even if the
        # rollup contains a stale FAILURE entry.
        payload = json.dumps({
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {"name": "trap-check", "conclusion": "FAILURE",
                 "completedAt": "2026-06-24T10:00:00Z", "state": "COMPLETED"},
            ],
        })
        mock_result = _mock.MagicMock(returncode=0, stdout=payload, stderr="")
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, detail = baton._pr_ci_state("123")
        assert state == "green"
        assert "CLEAN" in detail

    def test_pr_ci_state_stale_failure_with_latest_success_is_green(self, baton):
        # Two runs of the same check: old FAILURE, new SUCCESS.
        # Without dedup the old code returned red; with dedup it's green.
        payload = json.dumps({
            "mergeStateStatus": "UNKNOWN",
            "statusCheckRollup": [
                {"name": "trap-check", "conclusion": "FAILURE",
                 "completedAt": "2026-06-24T10:00:00Z", "state": "COMPLETED"},
                {"name": "trap-check", "conclusion": "SUCCESS",
                 "completedAt": "2026-06-24T12:00:00Z", "state": "COMPLETED"},
            ],
        })
        mock_result = _mock.MagicMock(returncode=0, stdout=payload, stderr="")
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, detail = baton._pr_ci_state("123")
        assert state == "green", f"expected green, got {state!r}: {detail}"

    def test_pr_ci_state_genuine_failure_is_red(self, baton):
        payload = json.dumps({
            "mergeStateStatus": "BLOCKED",
            "statusCheckRollup": [
                {"name": "trap-check", "conclusion": "FAILURE",
                 "completedAt": "2026-06-24T12:00:00Z", "state": "COMPLETED"},
            ],
        })
        mock_result = _mock.MagicMock(returncode=0, stdout=payload, stderr="")
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, detail = baton._pr_ci_state("123")
        assert state == "red"
        assert "trap-check" in detail
