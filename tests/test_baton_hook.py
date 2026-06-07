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
    # the counterpart's baton (not mine)
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
        """baton close is called when a PR-baton's live status returns MERGED.

        After the reachability fix, auto-archive runs as a second scan pass
        in _auto_archive_merged_pr_batons(), regardless of which agent holds
        the turn.  The baton here has turn==borges (in-court) but the test
        verifies the second-pass code path, not the old rendering-loop path.
        """
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1100",
            turn="borges",
            status="in-progress",
            turn_reason="fix pushed, awaiting merge",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        def _which(name):
            """Return a fake binary path for known tools, None otherwise."""
            return name  # "baton" → "baton", "gh" → "gh"

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod.shutil, "which", side_effect=_which):
                with patch.object(mod, "_fetch_pr_status", return_value="MERGED"):
                    with patch.object(mod, "subprocess") as mock_subproc:
                        # subprocess.run is called for baton close; ensure it
                        # doesn't raise so the hook completes.
                        mock_subproc.run.return_value = type(
                            "CompletedProcess", (), {"returncode": 0}
                        )()
                        mod.main()

        # Verify baton close was called exactly once with the expected args.
        # _fetch_pr_status is patched so no subprocess.run for gh is invoked;
        # the only subprocess.run in this code path is the baton close call.
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

        def _which(name):
            return name  # "baton" → "baton", "gh" → "gh"

        def _fake_run(args, **kwargs):
            if args[:2] == ["baton", "close"]:
                baton_close_calls.append(args)
            return type("CompletedProcess", (), {"returncode": 0})()

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod.shutil, "which", side_effect=_which):
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

        def _which(name):
            return name  # "baton" → "baton", "gh" → "gh"

        def _fake_run(args, **kwargs):
            if args[:2] == ["baton", "close"]:
                baton_close_calls.append(args)
            return type("CompletedProcess", (), {"returncode": 0})()

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod.shutil, "which", side_effect=_which):
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
        """If baton close subprocess raises, hook completes normally.

        After the reachability fix, the baton close call is in
        _auto_archive_merged_pr_batons().  When subprocess.run raises, the
        exception is caught, a stderr line is emitted, and the hook exits
        cleanly — the JSON output must still appear on stdout.
        """
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-1300",
            turn="borges",
            status="in-review",
            turn_reason="awaiting merge",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        def _which(name):
            return name  # "baton" → "baton", "gh" → "gh"

        def _exploding_run(args, **kwargs):
            if args and args[0] == "baton" and len(args) > 1 and args[1] == "close":
                raise OSError("baton not found")
            return type("CompletedProcess", (), {"returncode": 0})()

        stdout_output = []

        def _fake_print(*args, **kwargs):
            # Only capture stdout prints (no file= kwarg, or file=sys.stdout).
            # stderr prints (file=sys.stderr) are not captured here.
            file = kwargs.get("file", None)
            if file is None or file is sys.stdout:
                stdout_output.append(args[0] if args else "")

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod.shutil, "which", side_effect=_which):
                with patch.object(mod, "_fetch_pr_status", return_value="MERGED"):
                    with patch.object(mod, "subprocess") as mock_subproc:
                        mock_subproc.run.side_effect = _exploding_run
                        with patch("builtins.print", side_effect=_fake_print):
                            # Should not raise despite OSError in baton close
                            mod.main()

        # Hook must have produced exactly one JSON output on stdout
        assert len(stdout_output) == 1, (
            f"Expected 1 stdout print (JSON response), got {len(stdout_output)}: "
            f"{stdout_output}"
        )
        response = json.loads(stdout_output[0])
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


# ---------------------------------------------------------------------------
# Tests 18–24: Second-pass auto-archive — spec cases (a)–(g)
#
# These tests exercise the new _auto_archive_merged_pr_batons() function,
# which runs as a second scan pass over ALL open PR-batons regardless of
# whose turn it is.  This is the reachability fix for issue #874.
# ---------------------------------------------------------------------------

class TestAutoArchiveSecondPass:
    """Second-pass auto-archive: scans ALL open PR-batons regardless of turn."""

    # ------------------------------------------------------------------
    # Helper: mock shutil.which to return the binary name for known tools
    # ------------------------------------------------------------------

    @staticmethod
    def _which(name: str):
        """Fake shutil.which: returns the name itself for known tools."""
        if name in ("baton", "gh"):
            return name
        return None

    # ------------------------------------------------------------------
    # (a) Merged PR-baton at turn==someoneelse → baton close invoked
    #
    # The critical reachability case: baton has turn=maintainer (not borges),
    # so it never appears in _scan_my_batons("borges").  The second pass must
    # find and archive it.
    # ------------------------------------------------------------------

    def test_a_merged_pr_baton_turn_other_agent_invokes_baton_close(
        self, engram_home, projects_dir
    ):
        """baton close is called for a merged PR-baton held by another agent."""
        _write_config(engram_home, "borges")
        # Baton is at turn=maintainer — NOT in borges's court
        _write_project(
            projects_dir, "PR-2100",
            turn="maintainer",
            status="in-progress",
            turn_reason="colleague-approved; ready for merge",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod.shutil, "which", side_effect=self._which):
                with patch.object(mod, "_fetch_pr_status", return_value="MERGED"):
                    with patch.object(mod, "subprocess") as mock_subproc:
                        mock_subproc.run.return_value = type(
                            "CompletedProcess", (), {"returncode": 0}
                        )()
                        mod.main()

        mock_subproc.run.assert_called_once_with(
            ["baton", "close", "PR-2100", "--status", "merged"],
            capture_output=True,
            text=True,
            timeout=mod._GH_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # (b) Merged PR-baton already status=merged → NOT invoked (idempotency)
    # ------------------------------------------------------------------

    def test_b_already_closed_baton_not_archived_again(
        self, engram_home, projects_dir
    ):
        """baton close is NOT called for a baton already in CLOSED_STATUSES."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-2200",
            turn="maintainer",
            status="merged",          # already closed
            turn_reason="done",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        baton_close_calls = []

        def _fake_run(args, **kwargs):
            if args and args[0] == "baton" and len(args) > 1 and args[1] == "close":
                baton_close_calls.append(args)
            return type("CompletedProcess", (), {"returncode": 0})()

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod.shutil, "which", side_effect=self._which):
                with patch.object(mod, "_fetch_pr_status", return_value="MERGED"):
                    with patch.object(mod, "subprocess") as mock_subproc:
                        mock_subproc.run.side_effect = _fake_run
                        mod.main()

        assert baton_close_calls == [], (
            "baton close must not be called for an already-merged baton"
        )

    # ------------------------------------------------------------------
    # (c) Non-PR project baton (e.g. pool-x) → never considered
    # ------------------------------------------------------------------

    def test_c_non_pr_project_baton_never_archived(
        self, engram_home, projects_dir
    ):
        """Pool/project batons (non-PR-NNN ids) are never auto-archived."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "pool-coordination",
            turn="maintainer",
            status="in-progress",
            turn_reason="pool work ongoing",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        baton_close_calls = []

        def _fake_run(args, **kwargs):
            if args and args[0] == "baton" and len(args) > 1 and args[1] == "close":
                baton_close_calls.append(args)
            return type("CompletedProcess", (), {"returncode": 0})()

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod.shutil, "which", side_effect=self._which):
                # Even if fetch returns MERGED, pool-coordination is not PR-NNN
                with patch.object(mod, "_fetch_pr_status", return_value="MERGED"):
                    with patch.object(mod, "subprocess") as mock_subproc:
                        mock_subproc.run.side_effect = _fake_run
                        mod.main()

        assert baton_close_calls == [], (
            "baton close must not be called for non-PR-NNN project IDs"
        )

    # ------------------------------------------------------------------
    # (d) gh unavailable (gh_ok=False) → auto-archive pass skipped entirely
    # ------------------------------------------------------------------

    def test_d_gh_unavailable_skips_auto_archive(
        self, engram_home, projects_dir
    ):
        """When gh is unavailable, _auto_archive_merged_pr_batons is not called."""
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-2400",
            turn="maintainer",
            status="in-progress",
            turn_reason="awaiting merge",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        baton_close_calls = []

        def _fake_run(args, **kwargs):
            if args and args[0] == "baton" and len(args) > 1 and args[1] == "close":
                baton_close_calls.append(args)
            return type("CompletedProcess", (), {"returncode": 0})()

        def _which_no_gh(name):
            # gh is absent; baton is present but should never be reached
            if name == "gh":
                return None
            return name

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod.shutil, "which", side_effect=_which_no_gh):
                with patch.object(mod, "_fetch_pr_status", return_value="MERGED"):
                    with patch.object(mod, "subprocess") as mock_subproc:
                        mock_subproc.run.side_effect = _fake_run
                        mod.main()

        assert baton_close_calls == [], (
            "baton close must not be called when gh is unavailable"
        )

    # ------------------------------------------------------------------
    # (e) baton close returns nonzero → stderr contains auto-archive + pid;
    #     hook stdout is unaffected (no output when no in-court batons)
    # ------------------------------------------------------------------

    def test_e_nonzero_returncode_emits_stderr_and_leaves_stdout_clean(
        self, engram_home, projects_dir
    ):
        """Non-zero baton close returncode → stderr log; stdout empty."""
        _write_config(engram_home, "borges")
        # Baton at turn=maintainer — no in-court baton for borges
        _write_project(
            projects_dir, "PR-2500",
            turn="maintainer",
            status="in-progress",
            turn_reason="ready for merge",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        stderr_lines = []
        stdout_lines = []

        def _fake_print(*args, **kwargs):
            file = kwargs.get("file", None)
            text = args[0] if args else ""
            if file is sys.stderr:
                stderr_lines.append(str(text))
            else:
                stdout_lines.append(str(text))

        def _failing_run(cmd_args, **kwargs):
            cp = type(
                "CompletedProcess", (),
                {
                    "returncode": 1,
                    "stderr": "baton: no such file",
                    "stdout": "",
                },
            )()
            return cp

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod.shutil, "which", side_effect=self._which):
                with patch.object(mod, "_fetch_pr_status", return_value="MERGED"):
                    with patch.object(mod, "subprocess") as mock_subproc:
                        mock_subproc.run.side_effect = _failing_run
                        with patch("builtins.print", side_effect=_fake_print):
                            mod.main()

        # stdout must be empty (no in-court batons)
        assert stdout_lines == [], (
            f"stdout must be empty when no batons in court; got: {stdout_lines}"
        )
        # stderr must contain auto-archive and the pid
        assert any("auto-archive" in line for line in stderr_lines), (
            f"stderr must mention 'auto-archive'; got: {stderr_lines}"
        )
        assert any("PR-2500" in line for line in stderr_lines), (
            f"stderr must mention the pid 'PR-2500'; got: {stderr_lines}"
        )

    # ------------------------------------------------------------------
    # (f) baton binary missing → stderr skip line, no exception
    # ------------------------------------------------------------------

    def test_f_baton_binary_missing_emits_skip_line_no_exception(
        self, engram_home, projects_dir, tmp_path
    ):
        """When baton binary is absent, a skip line is written to stderr.

        Calls the real _auto_archive_merged_pr_batons() with shutil.which
        mocked to return None and _BATON_FALLBACK patched to a nonexistent
        path, so the production binary-check guard is exercised directly.
        """
        _write_config(engram_home, "borges")
        _write_project(
            projects_dir, "PR-2600",
            turn="maintainer",
            status="in-progress",
            turn_reason="ready for merge",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        stderr_lines = []

        def _fake_print(*args, **kwargs):
            file = kwargs.get("file", None)
            text = args[0] if args else ""
            if file is sys.stderr:
                stderr_lines.append(str(text))

        # Use a nonexistent path so Path(_BATON_FALLBACK).exists() is False
        fake_fallback = str(tmp_path / "bin" / "baton")

        cache: dict = {}

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            # shutil.which returns None for all names (baton not on PATH)
            with patch.object(mod.shutil, "which", return_value=None):
                # Patch the module-level _BATON_FALLBACK to the nonexistent path
                with patch.object(mod, "_BATON_FALLBACK", fake_fallback):
                    with patch("builtins.print", side_effect=_fake_print):
                        # Must not raise
                        mod._auto_archive_merged_pr_batons(cache)

        assert any("baton CLI not found" in line for line in stderr_lines), (
            f"Expected 'baton CLI not found; skipping' in stderr; got: {stderr_lines}"
        )

    # ------------------------------------------------------------------
    # (g) Zero in-court batons + one merged PR-baton at turn=maintainer →
    #     auto-archive still runs; hook prints nothing to stdout
    #
    # This is the key early-exit restructure: the old code did
    # `if not my_batons: sys.exit(0)` BEFORE the auto-archive pass, so
    # the idle agent never archived anything.  After the fix, the pass
    # runs even when my_batons is empty.
    # ------------------------------------------------------------------

    def test_g_zero_in_court_batons_auto_archive_still_runs(
        self, engram_home, projects_dir
    ):
        """Auto-archive runs even with zero in-court batons; stdout stays empty."""
        _write_config(engram_home, "borges")
        # Only baton: turn=maintainer → NOT in borges's court
        _write_project(
            projects_dir, "PR-2700",
            turn="maintainer",
            status="in-progress",
            turn_reason="colleague-approved",
        )

        mod = _load_hook_module(engram_home, projects_dir)

        baton_close_calls = []
        stdout_lines = []

        def _fake_run(cmd_args, **kwargs):
            if cmd_args and cmd_args[0] == "baton" and len(cmd_args) > 1 and cmd_args[1] == "close":
                baton_close_calls.append(cmd_args)
            return type(
                "CompletedProcess", (),
                {"returncode": 0, "stderr": "", "stdout": ""},
            )()

        def _fake_print(*args, **kwargs):
            file = kwargs.get("file", None)
            if file is None or file is sys.stdout:
                stdout_lines.append(args[0] if args else "")

        with patch.dict(os.environ, {
            "ENGRAM_HOME": str(engram_home),
            "BATON_PROJECTS_DIR": str(projects_dir),
        }):
            with patch.object(mod.shutil, "which", side_effect=self._which):
                with patch.object(mod, "_fetch_pr_status", return_value="MERGED"):
                    with patch.object(mod, "subprocess") as mock_subproc:
                        mock_subproc.run.side_effect = _fake_run
                        with patch("builtins.print", side_effect=_fake_print):
                            mod.main()

        # Auto-archive must have fired (the merged PR-baton was archived)
        assert len(baton_close_calls) == 1, (
            f"Expected 1 baton close call (for PR-2700); got: {baton_close_calls}"
        )
        assert baton_close_calls[0][2] == "PR-2700", (
            f"Expected baton close for PR-2700; got: {baton_close_calls[0]}"
        )
        # stdout must be empty — no in-court batons to surface
        assert stdout_lines == [], (
            f"stdout must be empty when no batons in court; got: {stdout_lines}"
        )
