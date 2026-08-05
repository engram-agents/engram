"""Tests for tools/baton.py — multi-agent turn-state CLI.

Test categories:
  - Smoke:          each subcommand happy path exits 0
  - Validation:     bad inputs rejected with expected exit codes / errors
  - Frontmatter:    parser handles edge cases
  - SingleAgent:    config-based mode gate → silent exit (reads) or error (writes)
  - Integration:    init → flip → mine → show → close round-trip
  - CI-state dedup: _dedupe_checks_by_latest + _pr_ci_state (#1366)
  - PoolSentinel:   sentinel resolved from config primary_user

All commands are tested via direct module calls + a mock ForumClient — no live
forum server needed and no local baton filesystem.  Subprocess is used only for
pure argparse-validation tests (those that fail before any API call).

PR1 migration note: baton.py is now 100% pure-API — no local BATON_PROJECTS_DIR
reads.  Tests that previously wrote local *.md files now use
`client.get.return_value = {"raw": _make_raw_baton(...)}` for state setup and
`client.post.call_args` for post-write assertions.  Local-FS feature tests
(malformed-file skip, tmp-file hygiene) are deleted as those code paths are gone.
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
# Module-level baton import for direct-call tests.
# Adding tools/ to sys.path covers both baton.py and forum_api.py.
# ---------------------------------------------------------------------------
_TOOLS_DIR = str(Path(__file__).parent.parent / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import baton as _baton  # noqa: E402
from forum_api import ForumHttpError, ForumNetworkError  # noqa: E402

# Exit codes (mirror baton.py constants for readability in assertions)
EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_IO = 2
EXIT_STATE = 3

# Config used for all multi-agent direct-call tests
MULTI_CONFIG = {"mode": "multi", "agent_name": "borges", "primary_user": "casey"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_baton(engram_home: Path, *args: str, env_overrides: dict | None = None) -> tuple:
    """Run the baton CLI as a subprocess; return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
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


def _make_client(post_return=None, get_return=None) -> MagicMock:
    """Return a mock ForumClient with configurable post/get return values."""
    client = MagicMock()
    client.post.return_value = post_return or {"seq": 42}
    if get_return is not None:
        client.get.return_value = get_return
    return client


def _write_config(
    engram_home: Path,
    agent_name: str = "borges",
    primary_user: str = "casey",
) -> None:
    """Write a minimal config.json into engram_home with mode=multi."""
    cfg = {"mode": "multi", "agent_name": agent_name, "primary_user": primary_user}
    (engram_home / "config.json").write_text(json.dumps(cfg))


def _make_raw_baton(project_id: str, **frontmatter) -> str:
    """Return a minimal baton file as a raw markdown string (no file I/O).

    The returned string matches what GET /api/projects/<pid>["raw"] returns.
    Override any field via keyword args (e.g. status="merged", turn="ariadne").
    """
    fm = {
        "project": project_id,
        "title": f"Test {project_id}",
        "status": "in-progress",
        "turn": "borges",
        "turn_since": "2026-06-26T12:00:00Z",
        "turn_reason": "start",
        "participants": "[borges, ariadne]",
    }
    fm.update(frontmatter)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines += ["---", "", "## Turn log", "", "- 2026-06-26T12:00:00Z initialized", ""]
    return "\n".join(lines)


def _ts(offset_minutes: int = -5) -> str:
    """Return ISO timestamp offset from now by given minutes."""
    dt = datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram_home"
    home.mkdir()
    return home


# ===========================================================================
# CATEGORY 1: Smoke tests — each subcommand happy path
# ===========================================================================

class TestSmoke:
    """Each subcommand exits 0 on happy-path input."""

    def test_help_exits_0(self, engram_home):
        rc, stdout, stderr = _run_baton(engram_home, "--help")
        assert rc == 0
        assert "init" in stdout
        assert "flip" in stdout
        assert "status" in stdout

    def test_init_exits_0(self, monkeypatch):
        """init happy path: API is called and no exception is raised."""
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
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects"
        assert body["project_id"] == "PR-999"

    def test_status_empty_exits_0(self, capsys):
        """status with empty project list prints 'no active' and returns normally."""
        client = _make_client(get_return={"projects": []})
        args = argparse.Namespace()
        _baton.cmd_status(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "no active" in captured.out.lower() or captured.out.strip() == ""

    def test_status_with_project_exits_0(self, capsys):
        """status with one project lists it."""
        client = _make_client(get_return={"projects": [{
            "project_id": "PR-100",
            "title": "Test project",
            "status": "in-review",
            "turn": "borges",
            "turn_since": _ts(-5),
            "turn_reason": "test",
            "participants": ["borges", "ariadne"],
            "seq": 1,
        }]})
        args = argparse.Namespace()
        _baton.cmd_status(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "PR-100" in captured.out

    def test_mine_exits_0(self, capsys):
        """mine with a project in turn lists it."""
        client = _make_client(get_return={"projects": [{
            "project_id": "PR-200",
            "title": "mine project",
            "status": "in-review",
            "turn": "borges",
            "turn_since": _ts(-5),
            "turn_reason": "test",
            "participants": ["borges", "ariadne"],
            "seq": 1,
        }]})
        args = argparse.Namespace()
        _baton.cmd_mine(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "PR-200" in captured.out

    def test_show_exits_0(self, capsys):
        """show displays the raw markdown of a project."""
        raw = _make_raw_baton("PR-300", title="show test")
        client = _make_client(get_return={"raw": raw})
        args = argparse.Namespace(project_id="PR-300")
        _baton.cmd_show(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "PR-300" in captured.out

    def test_flip_exits_0(self, monkeypatch):
        """flip happy path: API is called and no exception is raised."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-400")},
        )
        args = argparse.Namespace(
            project_id="PR-400",
            to="ariadne",
            reason="done step 1",
            force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()

    def test_close_exits_0(self, monkeypatch):
        """close happy path: API is called and no exception is raised."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-500", status="in-review")},
        )
        args = argparse.Namespace(project_id="PR-500", status="merged")
        _baton.cmd_close(args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()


# ===========================================================================
# CATEGORY 2: Validation tests — bad inputs rejected
# ===========================================================================

class TestValidation:
    """Bad inputs are rejected with expected exit codes and error messages."""

    def test_init_duplicate_fails(self, capsys, monkeypatch):
        """init on existing project → EXIT_VALIDATION, already-exists error."""
        # _pool_sentinel() reads the ambient $ENGRAM_HOME/config.json file, not the
        # passed config dict — mock it (as the sibling tests do) so the turn
        # validation resolves in a clean CI env instead of erroring on a missing
        # primary_user.
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client()
        client.post.side_effect = ForumHttpError(409, '{"error": "already exists"}')
        args = argparse.Namespace(
            project_id="PR-101",
            title="duplicate",
            participants="borges,ariadne",
            status="planning",
            turn=None,
            github=None,
            colleague=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "already exists" in captured.err or "PR-101" in captured.err

    def test_init_invalid_project_id_fails(self, engram_home):
        """init with invalid ID (no hyphen) → exit 1, format error."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "init", "NOHYPHEN",
            "--title", "bad id",
            "--participants", "borges",
        )
        assert rc == 1
        assert "TYPE-ID" in stderr or "invalid" in stderr.lower()

    def test_init_invalid_status_fails(self, engram_home):
        """init with unknown status → argparse rejects (exit non-zero)."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "init", "PR-102",
            "--title", "bad status",
            "--participants", "borges,ariadne",
            "--status", "unknown-status",
        )
        assert rc != 0

    def test_flip_nonexistent_project_fails(self, capsys):
        """flip on non-existent project → EXIT_VALIDATION, not-found error."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(
            project_id="PR-NONE", to="ariadne", reason="done", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_flip_to_non_participant_fails(self, monkeypatch, capsys):
        """flip to agent not in participants → EXIT_VALIDATION, validation error."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-103", participants="[borges, ariadne]")},
        )
        args = argparse.Namespace(
            project_id="PR-103", to="mneme", reason="done", force=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not a participant" in captured.err or "participant" in captured.err

    def test_flip_to_casey_allowed(self, monkeypatch):
        """flip to 'casey' (pool sentinel) is always valid regardless of participants."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-104", participants="[borges, ariadne]")},
        )
        args = argparse.Namespace(
            project_id="PR-104", to="casey", reason="need human decision", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["to_agent"] == "casey"

    def test_flip_closed_project_fails(self, monkeypatch, capsys):
        """flip on merged project → EXIT_VALIDATION, closed error."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            get_return={"raw": _make_raw_baton("PR-105", status="merged")},
        )
        args = argparse.Namespace(
            project_id="PR-105", to="ariadne", reason="done", force=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "merged" in captured.err or "closed" in captured.err

    def test_flip_cancelled_project_fails(self, monkeypatch, capsys):
        """flip on cancelled project → EXIT_VALIDATION, closed error."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            get_return={"raw": _make_raw_baton("PR-106", status="cancelled")},
        )
        args = argparse.Namespace(
            project_id="PR-106", to="ariadne", reason="done", force=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "cancelled" in captured.err or "closed" in captured.err

    def test_show_nonexistent_fails(self, capsys):
        """show on non-existent project → EXIT_VALIDATION, not-found error."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-NONE")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_show(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_close_invalid_status_fails(self, engram_home):
        """close with status=in-review (not a close status) → argparse rejects."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "close", "PR-107", "--status", "in-review",
        )
        assert rc != 0

    def test_close_already_closed_fails(self, capsys):
        """close on already-merged project → EXIT_VALIDATION, already-closed error."""
        client = _make_client(
            get_return={"raw": _make_raw_baton("PR-108", status="merged")},
        )
        args = argparse.Namespace(project_id="PR-108", status="cancelled")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_close(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "already" in captured.err

    def test_init_turn_outside_participants_fails(self, engram_home):
        """init with --turn not in participants (and not sentinel) → exit 1."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
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
    """Parser handles edge cases gracefully."""

    def test_show_displays_full_content(self, capsys):
        """show outputs the complete file content including body."""
        raw = _make_raw_baton(
            "PR-201", title="Full content test", turn_reason="colleague-review request",
        )
        client = _make_client(get_return={"raw": raw})
        args = argparse.Namespace(project_id="PR-201")
        _baton.cmd_show(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "PR-201" in captured.out
        assert "Full content test" in captured.out
        assert "Turn log" in captured.out

    def test_participants_brackets_parsed(self, monkeypatch):
        """Participants in [alice, bob] format are parsed correctly for flip validation."""
        raw = (
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
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(post_return={"seq": 1}, get_return={"raw": raw})
        args = argparse.Namespace(
            project_id="PR-202", to="ariadne", reason="test flip", force=False,
        )
        # Should not raise — proves ariadne was found in bracketed participants
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["to_agent"] == "ariadne"

    def test_turn_reason_quotes_stripped_in_status(self, capsys):
        """turn_reason with surrounding quotes is displayed without double-quoting."""
        client = _make_client(get_return={"projects": [{
            "project_id": "PR-204",
            "title": "Test project",
            "status": "in-review",
            "turn": "borges",
            "turn_since": _ts(-5),
            "turn_reason": '"round-3 fix pushed"',  # raw YAML echoed with quotes
            "participants": ["borges", "ariadne"],
            "seq": 1,
        }]})
        args = argparse.Namespace()
        _baton.cmd_status(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "round-3 fix pushed" in captured.out
        assert '""round-3 fix pushed""' not in captured.out


# ===========================================================================
# CATEGORY 5: Single-agent guard (config-based)
#
# Changed from directory-existence check to config mode check.
# Read commands → silent exit 0 in single-agent mode.
# Write commands → EXIT_STATE with "single-agent mode" message.
# ===========================================================================

class TestSingleAgentGuard:
    """Config mode gate: single-agent → silent exit (reads) or error (writes)."""

    def test_status_single_agent_exits_0_silently(self, capsys):
        """cmd_status with single-agent config exits 0 silently, no API calls."""
        client = _make_client()
        args = argparse.Namespace()
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_status(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_OK
        captured = capsys.readouterr()
        assert captured.out.strip() == ""
        client.get.assert_not_called()

    def test_mine_single_agent_exits_0_silently(self, capsys):
        """cmd_mine with single-agent config exits 0 silently."""
        client = _make_client()
        args = argparse.Namespace()
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_mine(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_OK

    def test_show_single_agent_exits_0_silently(self, capsys):
        """cmd_show with single-agent config exits 0 silently, no API call."""
        client = _make_client()
        args = argparse.Namespace(project_id="PR-999")
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_show(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_OK
        client.get.assert_not_called()

    def test_init_single_agent_exits_state(self, capsys):
        """cmd_init with single-agent config exits EXIT_STATE with message."""
        client = _make_client()
        args = argparse.Namespace(
            project_id="PR-999", title="test", participants="borges,ariadne",
            status="planning", turn=None, github=None, colleague=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()
        assert "single-agent" in captured.err

    def test_flip_single_agent_exits_state(self, capsys):
        """cmd_flip with single-agent config exits EXIT_STATE with message."""
        client = _make_client()
        args = argparse.Namespace(
            project_id="PR-1", to="ariadne", reason="done", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()
        assert "single-agent" in captured.err

    def test_flip_missing_project_exits_1(self, capsys):
        """flip referencing non-existent project ID → EXIT_VALIDATION."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(
            project_id="PR-NONEXISTENT", to="ariadne", reason="done", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION


# ===========================================================================
# CATEGORY 6: Integration round-trip
# ===========================================================================

class TestIntegration:
    """init → flip → mine → show → close round-trip."""

    @pytest.mark.skip(
        reason="requires live forum server; multi-step state chain can't be "
               "reproduced with mocks alone (mine/status/show read server-written state). "
               "Individual write operations are covered by the other Integration tests."
    )
    def test_full_round_trip(self):
        """Complete lifecycle: init, flip twice, mine filtering, close."""

    def test_flip_updates_turn_field(self, monkeypatch):
        """After flip, the API receives to_agent=ariadne (server updates turn field)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-501")},
        )
        args = argparse.Namespace(
            project_id="PR-501", to="ariadne", reason="done step 1", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["to_agent"] == "ariadne"

    def test_flip_updates_turn_reason(self, monkeypatch):
        """After flip, the API receives the provided reason."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-502")},
        )
        args = argparse.Namespace(
            project_id="PR-502", to="ariadne", reason="round-3 fix pushed", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert "round-3 fix pushed" in body["reason"]

    def test_flip_appends_turn_log_entry(self, monkeypatch):
        """After flip, the API is called with to_agent and reason."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-503")},
        )
        args = argparse.Namespace(
            project_id="PR-503", to="ariadne", reason="step complete", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-503/flip"
        assert body["to_agent"] == "ariadne"
        assert "step complete" in body["reason"]

    def test_close_updates_status_field(self, monkeypatch):
        """After close, the API receives new_status=merged."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-504", status="in-review")},
        )
        args = argparse.Namespace(project_id="PR-504", status="merged")
        _baton.cmd_close(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["new_status"] == "merged"

    def test_close_appends_turn_log_entry(self, monkeypatch):
        """baton close calls the API with status=merged."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-505", status="in-review")},
        )
        args = argparse.Namespace(project_id="PR-505", status="merged")
        _baton.cmd_close(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-505/status"
        assert body["new_status"] == "merged"
        assert "agent" in body

    def test_mine_filters_by_turn(self, capsys):
        """mine only shows projects where turn == invoking agent."""
        client = _make_client(get_return={"projects": [
            {
                "project_id": "PR-601", "title": "mine project", "status": "in-review",
                "turn": "borges", "turn_since": _ts(-5), "turn_reason": "test",
                "participants": ["borges", "ariadne"], "seq": 1,
            },
            {
                "project_id": "PR-602", "title": "not mine", "status": "in-review",
                "turn": "ariadne", "turn_since": _ts(-5), "turn_reason": "test",
                "participants": ["borges", "ariadne"], "seq": 2,
            },
        ]})
        args = argparse.Namespace()
        _baton.cmd_mine(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "PR-601" in captured.out
        assert "PR-602" not in captured.out

    def test_status_mine_flag_same_as_mine_subcommand(self, capsys):
        """baton status --mine and baton mine produce identical filtering."""
        # NOTE: cmd_status mutates p["turn_since"] in-place (str → datetime).
        # Use a factory to get fresh list instances so the second call sees
        # clean string values rather than already-parsed datetimes.
        def _projects():
            return [
                {
                    "project_id": "PR-603", "title": "mine", "status": "in-review",
                    "turn": "borges", "turn_since": _ts(-5), "turn_reason": "test",
                    "participants": ["borges", "ariadne"], "seq": 1,
                },
                {
                    "project_id": "PR-604", "title": "not mine", "status": "in-review",
                    "turn": "ariadne", "turn_since": _ts(-5), "turn_reason": "test",
                    "participants": ["borges", "ariadne"], "seq": 2,
                },
            ]

        client1 = _make_client(get_return={"projects": _projects()})
        args1 = argparse.Namespace(mine=True)
        _baton.cmd_status(args1, MULTI_CONFIG, "borges", client1)
        captured1 = capsys.readouterr()

        client2 = _make_client(get_return={"projects": _projects()})
        args2 = argparse.Namespace()
        _baton.cmd_mine(args2, MULTI_CONFIG, "borges", client2)
        captured2 = capsys.readouterr()

        assert "PR-603" in captured1.out and "PR-603" in captured2.out
        assert "PR-604" not in captured1.out and "PR-604" not in captured2.out

    def test_multiple_flips_accumulate_log_entries(self, monkeypatch):
        """Multiple flip API calls are each made with their distinct reasons."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(post_return={"seq": 1})
        client.get.return_value = {"raw": _make_raw_baton("PR-605")}

        # First flip borges → ariadne
        args1 = argparse.Namespace(
            project_id="PR-605", to="ariadne", reason="first flip", force=False,
        )
        _baton.cmd_flip(args1, MULTI_CONFIG, "borges", client)

        # Simulate server write (ariadne now holds baton)
        client.get.return_value = {"raw": _make_raw_baton("PR-605", turn="ariadne")}

        # Second flip ariadne → borges
        args2 = argparse.Namespace(
            project_id="PR-605", to="borges", reason="second flip", force=False,
        )
        _baton.cmd_flip(args2, MULTI_CONFIG, "ariadne", client)

        assert client.post.call_count == 2
        first_body = client.post.call_args_list[0][0][1]
        second_body = client.post.call_args_list[1][0][1]
        assert first_body["reason"] == "first flip"
        assert second_body["reason"] == "second flip"

    def test_status_sorted_by_turn_since(self, capsys):
        """status output sorts by turn_since ascending (oldest first)."""
        client = _make_client(get_return={"projects": [
            {
                "project_id": "PR-702", "title": "newer", "status": "in-review",
                "turn": "borges", "turn_since": _ts(-5), "turn_reason": "test",
                "participants": ["borges", "ariadne"], "seq": 2,
            },
            {
                "project_id": "PR-701", "title": "older", "status": "in-review",
                "turn": "borges", "turn_since": _ts(-60), "turn_reason": "test",
                "participants": ["borges", "ariadne"], "seq": 1,
            },
        ]})
        args = argparse.Namespace()
        _baton.cmd_status(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        idx_701 = captured.out.find("PR-701")
        idx_702 = captured.out.find("PR-702")
        assert idx_701 < idx_702, "Older project should appear first"

    def test_init_default_turn_is_invoking_agent(self, monkeypatch):
        """init without --turn posts body with turn=borges (the invoking agent)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-801", title="default turn test",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["turn"] == "borges"

    def test_init_explicit_turn_override(self, monkeypatch):
        """init with --turn ariadne posts body with turn=ariadne."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-802", title="explicit turn",
            participants="borges,ariadne", status="planning",
            turn="ariadne", github=None, colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["turn"] == "ariadne"

    def test_init_casey_as_participant_allowed(self, monkeypatch):
        """casey can be a participant and initial turn holder — API is called."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-803", title="with casey",
            participants="borges,ariadne,casey", status="planning",
            turn="casey", github=None, colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()

    def test_descriptive_project_id_accepted(self, monkeypatch):
        """DESIGN-style IDs are accepted and posted to the API."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="DESIGN-trust-tier-v2", title="trust tier design",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["project_id"] == "DESIGN-trust-tier-v2"


# ===========================================================================
# CATEGORY 7: rename subcommand
# ===========================================================================

class TestRename:
    """baton rename: round-trip, validation, and audit-line tests."""

    def test_rename_round_trip(self, monkeypatch):
        """init → rename: the rename API is called with the new title."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})

        # Step 1: init (API call, no file written)
        args_init = argparse.Namespace(
            project_id="PR-901", title="original title",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, colleague=None,
        )
        _baton.cmd_init(args_init, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()

        # Simulate server write: update client.get for subsequent rename read
        client.get.return_value = {"raw": _make_raw_baton(
            "PR-901", title="original title", turn="borges", status="planning",
        )}

        # Step 2: rename
        client.reset_mock()
        client.post.return_value = {"seq": 1}
        args_rename = argparse.Namespace(project_id="PR-901", title="new shiny title")
        _baton.cmd_rename(args_rename, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-901/rename"
        assert body["new_title"] == "new shiny title"

    def test_rename_audit_line_appended(self):
        """After rename, the API is called with the new title."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-902", title="before rename")},
        )
        args = argparse.Namespace(project_id="PR-902", title="after rename")
        _baton.cmd_rename(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-902/rename"
        assert body["new_title"] == "after rename"

    def test_rename_refuse_missing_project(self, capsys):
        """Rename of non-existent project → EXIT_VALIDATION, not-found error."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-NONE", title="foo")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_rename(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_rename_refuse_empty_title(self, engram_home):
        """Rename with empty title exits non-zero (validation fires before API)."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "rename", "PR-903", "--title", "",
        )
        assert rc != 0
        assert "empty" in stderr.lower() or "non-empty" in stderr.lower()

    def test_rename_refuse_whitespace_only_title(self, engram_home):
        """Rename with whitespace-only title exits non-zero."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "rename", "PR-904", "--title", "   ",
        )
        assert rc != 0

    def test_rename_refuse_title_with_newline(self, engram_home):
        """Rename with newline in title exits non-zero (YAML safety)."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "rename", "PR-905", "--title", "line1\nline2",
        )
        assert rc != 0
        assert "newline" in stderr.lower()

    def test_rename_refuse_title_too_long(self, engram_home):
        """Rename with title > 200 chars exits non-zero."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "rename", "PR-906", "--title", "x" * 201,
        )
        assert rc != 0
        assert "long" in stderr.lower() or "200" in stderr

    def test_rename_exact_200_char_title_accepted(self):
        """Title of exactly 200 chars is accepted and reaches the API."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-908")},
        )
        args = argparse.Namespace(project_id="PR-908", title="a" * 200)
        _baton.cmd_rename(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert len(body["new_title"]) == 200


# ===========================================================================
# CATEGORY 8: claim subcommand
# ===========================================================================

class TestClaim:
    """baton claim: happy path, state-guard refusals, and audit-line tests."""

    def test_claim_happy_path(self, monkeypatch):
        """Claim posts to the claim route with agent=borges and pool_sentinel=casey."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n == "casey")

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1001", title="pool project", turn="casey",
                participants="[borges, ariadne]", turn_reason="released",
            )},
        )
        args = argparse.Namespace(project_id="PR-1001")
        _baton.cmd_claim(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1001/claim"
        assert body["agent"] == "borges"
        assert body["pool_sentinel"] == "casey"

    def test_claim_refuse_already_claimed_by_other(self, monkeypatch, capsys):
        """Claim when project held by another agent → EXIT_STATE with clear message."""
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n == "casey")

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-1002", turn="ariadne", participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1002")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()
        assert "ariadne" in captured.err

    def test_claim_refuse_invoker_not_in_participants(self, monkeypatch, capsys):
        """Claim when invoker not in participants → EXIT_VALIDATION."""
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n == "casey")

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-1003", turn="casey", participants="[ariadne, mneme]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1003")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "participant" in captured.err

    def test_claim_refuse_missing_project(self, capsys):
        """Claim of non-existent project → EXIT_VALIDATION, not-found error."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-1004")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()


# ===========================================================================
# CATEGORY 9: release subcommand
# ===========================================================================

class TestRelease:
    """baton release: happy path, state-guard refusals, --done flag tests."""

    def test_release_happy_path(self, monkeypatch):
        """Release posts to the release route with pool_sentinel=casey."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1101", title="claimed project", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1101", done=False, reason="released")
        _baton.cmd_release(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1101/release"
        assert body["pool_sentinel"] == "casey"
        assert body["reason"] == "released"
        assert body["done"] is False

    def test_release_custom_reason(self, monkeypatch):
        """Release with --reason passes the provided reason in the POST body."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1102", turn="borges", participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-1102", done=False, reason="stepping back for review",
        )
        _baton.cmd_release(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert "stepping back for review" in body["reason"]

    def test_release_refuse_non_holder(self, monkeypatch, capsys):
        """Release when invoker is not current holder → EXIT_STATE."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-1103", turn="ariadne", participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1103", done=False, reason="released")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_release(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()
        assert "ariadne" in captured.err

    def test_release_done_appends_to_title(self, monkeypatch):
        """release --done passes done=True in POST body (server marks title)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1104", title="cli gap work", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1104", done=True, reason="released")
        _baton.cmd_release(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["done"] is True

    def test_release_done_idempotent(self, monkeypatch):
        """release --done on already-(done) title passes done=True (server handles it)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1105", title="cli gap work (done)", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1105", done=True, reason="released")
        _baton.cmd_release(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["done"] is True


# ===========================================================================
# CATEGORY 10: anchor subcommand + --github init flag
# ===========================================================================

import re as _re
_GITHUB_ANCHOR_PATTERN = _re.compile(r"^(pr|project)/\d+$")


class TestAnchor:
    """baton anchor subcommand and baton init --github flag."""

    def test_init_github_pr_writes_frontmatter(self, monkeypatch):
        """init --github passes the anchor in the API body (server writes frontmatter)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-env-home", title="env home test",
            participants="borges,ariadne", status="planning",
            turn=None, github="pr/123", colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["github"] == "pr/123"
        assert _GITHUB_ANCHOR_PATTERN.match(body["github"])

    def test_init_github_pr_reflected_in_show(self, monkeypatch, capsys):
        """init --github is sent to the API; baton show reflects the field via API."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        # Step 1: init — verify github field in POST body
        client = _make_client({"seq": 1})
        args_init = argparse.Namespace(
            project_id="PR-show-test", title="show test",
            participants="borges,ariadne", status="planning",
            turn=None, github="pr/456", colleague=None,
        )
        _baton.cmd_init(args_init, MULTI_CONFIG, "borges", client)
        _, init_body = client.post.call_args[0]
        assert init_body["github"] == "pr/456"

        # Step 2: show — API returns the server-written state including github field
        raw_with_github = (
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
        client2 = _make_client(get_return={"raw": raw_with_github})
        args_show = argparse.Namespace(project_id="PR-show-test")
        _baton.cmd_show(args_show, MULTI_CONFIG, "borges", client2)
        captured = capsys.readouterr()
        assert "pr/456" in captured.out

    def test_init_github_rejects_malformed_value(self, engram_home):
        """init --github with malformed value exits non-zero (validation before API)."""
        _write_config(engram_home, "borges")
        for bad_val in ("xyz", "pr/abc", "pr/", "project/", "123"):
            rc, stdout, stderr = _run_baton(
                engram_home,
                "init", "PR-bad-anchor",
                "--title", "bad anchor test",
                "--participants", "borges,ariadne",
                "--github", bad_val,
            )
            assert rc != 0, (
                f"Expected non-zero exit for --github '{bad_val}'; got rc={rc}"
            )

    def test_anchor_sets_github_on_existing_baton(self, monkeypatch):
        """init → anchor: the anchor API is called with github=project/4."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})

        # Step 1: init without --github
        args_init = argparse.Namespace(
            project_id="pool-data-correctness", title="data correctness work",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, colleague=None,
        )
        _baton.cmd_init(args_init, MULTI_CONFIG, "borges", client)
        init_body = client.post.call_args[0][1]
        assert init_body.get("github") is None

        # Simulate server write: anchor can now read the project via API
        client.get.return_value = {"raw": _make_raw_baton(
            "pool-data-correctness", title="data correctness work",
            turn="borges", status="planning",
        )}

        # Step 2: set anchor
        client.reset_mock()
        client.post.return_value = {"seq": 1}
        args_anchor = argparse.Namespace(
            project_id="pool-data-correctness", github="project/4",
        )
        _baton.cmd_anchor(args_anchor, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/pool-data-correctness/anchor"
        github_val = body["github"]
        assert github_val == "project/4"
        assert _GITHUB_ANCHOR_PATTERN.match(github_val)

    def test_anchor_updates_existing_github_field(self):
        """anchor on a baton that already has github: field calls API with new value."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-update-anchor", github="pr/100")},
        )
        args = argparse.Namespace(project_id="PR-update-anchor", github="pr/200")
        _baton.cmd_anchor(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["github"] == "pr/200"

    def test_anchor_missing_project_exits_nonzero(self, capsys):
        """anchor on a non-existent project exits non-zero."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-NONE", github="pr/99")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_anchor(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_anchor_rejects_malformed_value(self, engram_home):
        """anchor with malformed --github value exits non-zero (validation before API)."""
        _write_config(engram_home, "borges")
        for bad_val in ("xyz", "pr/abc", "github.com/org/repo", "notaformat"):
            rc, stdout, stderr = _run_baton(
                engram_home,
                "anchor", "PR-1201",
                "--github", bad_val,
            )
            assert rc != 0, (
                f"Expected non-zero exit for --github '{bad_val}'; got rc={rc}"
            )

    def test_anchor_audit_line_appended(self):
        """anchor calls the API; server appends audit line to the turn log."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-1202")},
        )
        args = argparse.Namespace(project_id="PR-1202", github="pr/77")
        _baton.cmd_anchor(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1202/anchor"
        assert body["github"] == "pr/77"

    def test_init_without_github_does_not_write_github_field(self, monkeypatch):
        """init without --github posts no github field (or null) to the API."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-no-github", title="no anchor",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        github_val = body.get("github")
        assert github_val is None

    def test_anchor_github_project_format(self):
        """anchor with project/<N> value is accepted and posted to the API correctly."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-1204")},
        )
        args = argparse.Namespace(project_id="PR-1204", github="project/4")
        _baton.cmd_anchor(args, MULTI_CONFIG, "borges", client)

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

    def test_reopen_merged_baton_to_in_progress(self):
        """Reopen a merged baton: API is called with new_status=in-progress."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1301", title="reopened project", status="merged",
                turn="borges", participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1301", status="in-progress")
        _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1301/status"
        assert body["new_status"] == "in-progress"

    def test_reopen_cancelled_baton_to_in_progress(self):
        """Reopen a cancelled baton: API is called with new_status=in-progress."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1302", status="cancelled", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1302", status="in-progress")
        _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["new_status"] == "in-progress"

    def test_reopen_with_explicit_status_flag(self):
        """Reopen with --status in-review: API called with new_status=in-review."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1303", status="merged", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1303", status="in-review")
        _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["new_status"] == "in-review"

    def test_reopen_refuses_already_active(self, capsys):
        """Reopen on already-active baton → EXIT_VALIDATION with status in message."""
        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-1304", status="in-progress", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1304", status="in-progress")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "in-progress" in captured.err
        assert "not closed" in captured.err or "already" in captured.err.lower() or "flip" in captured.err

    def test_reopen_refuses_invalid_target_status(self, engram_home):
        """Reopen with --status merged (closed status) → argparse rejects."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "reopen", "PR-1305", "--status", "merged",
        )
        assert rc != 0
        assert "invalid choice" in stderr or "merged" in stderr

    def test_reopen_with_status_planning(self):
        """Reopen with --status planning calls API with new_status=planning."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1306", status="merged", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1306", status="planning")
        _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["new_status"] == "planning"

    def test_reopen_refuses_missing_file(self, capsys):
        """Reopen of non-existent project → EXIT_VALIDATION, not-found error."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-NONE", status="in-progress")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_reopen_appends_audit_log(self):
        """reopen API call includes a reason with the status transition (server appends)."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1306", status="merged", turn="borges",
                participants="[borges, ariadne]", turn_reason="merged successfully",
            )},
        )
        args = argparse.Namespace(project_id="PR-1306", status="in-progress")
        _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1306/status"
        assert "reopened" in body["reason"]
        assert "borges" in body["reason"]
        assert "merged" in body["reason"]
        assert "in-progress" in body["reason"]

    def test_reopen_output_message_style(self, capsys):
        """reopen prints a confirmation line with project ID and new status."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1308", status="merged", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1308", status="in-progress")
        _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)

        captured = capsys.readouterr()
        assert "PR-1308" in captured.out
        assert "in-progress" in captured.out


# ===========================================================================
# CATEGORY 13: Pool sentinel resolution (issue #606)
# ===========================================================================

class TestPoolSentinel:
    """Pool sentinel is derived from config primary_user, not hardcoded."""

    def test_sentinel_resolves_to_non_casey_primary_user(self, engram_home, monkeypatch):
        """With primary_user='alice', pool sentinel is 'alice' — claim/release use alice.

        This is the core fresh-install correctness test from issue #606:
        a non-casey primary_user install must work end-to-end.
        """
        alice_config = {"mode": "multi", "agent_name": "borges", "primary_user": "alice"}
        _write_config(engram_home, agent_name="borges", primary_user="alice")
        # _pool_sentinel() reads os.environ.get("ENGRAM_HOME") directly, not the
        # module-level constant.  Use setenv (not setattr) to redirect the config read.
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n in ("alice",))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "pool-alice-test", title="non-casey sentinel test", turn="alice",
                participants="[borges, ariadne]", status="in-progress",
            )},
        )

        # Claim: _pool_sentinel() must read config → "alice" (not "casey")
        args_claim = argparse.Namespace(project_id="pool-alice-test")
        _baton.cmd_claim(args_claim, alice_config, "borges", client)

        _, claim_body = client.post.call_args[0]
        assert claim_body["pool_sentinel"] == "alice"
        assert "casey" not in claim_body["pool_sentinel"]

        # Simulate server write (borges now holds baton)
        client.reset_mock()
        client.post.return_value = {"seq": 1}
        client.get.return_value = {"raw": _make_raw_baton(
            "pool-alice-test", turn="borges", participants="[borges, ariadne]",
        )}
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        # Release: sentinel should still be "alice"
        args_release = argparse.Namespace(
            project_id="pool-alice-test", done=False, reason="released",
        )
        _baton.cmd_release(args_release, alice_config, "borges", client)

        _, release_body = client.post.call_args[0]
        assert release_body["pool_sentinel"] == "alice"

    def test_sentinel_case_normalizes_capitalized_primary_user(self, engram_home, monkeypatch):
        """primary_user='Alice' (capitalized) in config normalizes to sentinel 'alice'.

        Regression test for PR #612 round-2: _pool_sentinel() used to return
        the raw config string, so a capitalized primary_user would silently
        fail all sentinel comparisons.  The fix applies .lower() in _pool_sentinel().
        """
        alice_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alice"}
        _write_config(engram_home, agent_name="borges", primary_user="Alice")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n in ("alice",))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "pool-alice-cap-test", title="capitalized primary_user test", turn="alice",
                participants="[borges, ariadne]", status="in-progress",
            )},
        )

        # Claim: sentinel must resolve to lowercase "alice" despite "Alice" in config
        args_claim = argparse.Namespace(project_id="pool-alice-cap-test")
        _baton.cmd_claim(args_claim, alice_config, "borges", client)

        _, claim_body = client.post.call_args[0]
        assert claim_body["pool_sentinel"] == "alice"

        # Simulate server write
        client.reset_mock()
        client.post.return_value = {"seq": 1}
        client.get.return_value = {"raw": _make_raw_baton(
            "pool-alice-cap-test", turn="borges", participants="[borges, ariadne]",
        )}
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        # Release: sentinel still lowercase "alice"
        args_release = argparse.Namespace(
            project_id="pool-alice-cap-test", done=False, reason="released",
        )
        _baton.cmd_release(args_release, alice_config, "borges", client)

        _, release_body = client.post.call_args[0]
        assert release_body["pool_sentinel"] == "alice"

    def test_sentinel_accepts_short_form_first_token(self, engram_home, monkeypatch):
        """Pool initialized with turn='alex' works when primary_user='Alex Chen'.

        Regression test for issue #1309: _pool_sentinel() returns 'alex chen',
        but pools are often initialized with just the first token ('alex').
        _is_pool_sentinel must accept both forms.
        """
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "pool-short-form-test", title="short-form sentinel test", turn="alex",
                participants="[borges, ariadne]", status="in-progress",
            )},
        )

        # Claim: _is_pool_sentinel("alex") must return True for sentinel "alex chen"
        args_claim = argparse.Namespace(project_id="pool-short-form-test")
        _baton.cmd_claim(args_claim, owner_config, "borges", client)

        _, claim_body = client.post.call_args[0]
        assert claim_body["agent"] == "borges"
        assert claim_body["pool_sentinel"] == "alex chen"

        # Simulate server write (borges holds baton after claim)
        client.reset_mock()
        client.post.return_value = {"seq": 1}
        client.get.return_value = {"raw": _make_raw_baton(
            "pool-short-form-test", turn="borges", participants="[borges, ariadne]",
        )}
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        # Release: sentinel must be full "alex chen"
        args_release = argparse.Namespace(
            project_id="pool-short-form-test", done=False, reason="released",
        )
        _baton.cmd_release(args_release, owner_config, "borges", client)

        _, release_body = client.post.call_args[0]
        assert release_body["pool_sentinel"] == "alex chen"

    def test_merge_gate2_accepts_short_form_sentinel(self, engram_home, monkeypatch, capsys):
        """baton merge Gate 2 accepts short-form sentinel 'alex' with primary_user='Alex Chen'.

        Regression test for the missed comparison site in cmd_merge (issue #1309):
        Gate 2 used raw != sentinel instead of _is_pool_sentinel, so a PR baton
        with turn='alex' (short form, written by baton flip) would block merge
        with "turn is with lei — not presented for merge."
        """
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr, repo=None: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("unknown", "no gh"))

        # Baton with short-form sentinel in turn (the bug trigger). Repo-
        # qualified github: anchor so this reaches gate 2 rather than
        # tripping the #1918 no-repo refusal gate that now sits before it;
        # the monkeypatched CI/approval fakes above make gates 3/4 no-ops
        # regardless of repo.
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-9999", title="short-form merge gate test", turn="alex",
                participants="[borges, ariadne]", status="in-progress",
                github="pr/example-org/example-repo/9999",
            )},
        )

        # --dry-run --force: skips CI (Gate 3) + approval (Gate 4); no real gh call.
        # Should pass Gate 2 without "not presented for merge" error.
        args = argparse.Namespace(pr="PR-9999", dry_run=True, force=True)
        _baton.cmd_merge(args, owner_config, "borges", client)

        captured = capsys.readouterr()
        assert "gate 2 (turn=sentinel): PASS" in captured.out

    def test_merge_repo_qualified_ci_red_refuses(self, engram_home, monkeypatch, capsys):
        """#1715 round-2 (reviewer-fairy-caught blocker): cmd_merge is a
        sibling gh consumer of the same `github` anchor field, and its
        CI-green gate is checked against gh's ambient-cwd repo unless the
        anchor's own repo is threaded through -- for an irreversible
        `gh pr merge --squash`, that's the most dangerous instance of the
        whole #1715 bug class. Prove the gate actually fires against the
        ANCHOR's repo, not just that some CI check happened."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("red", "check 'pr-checks' conclusion=FAILURE")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("unknown", "not reached"))

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-9300", turn="alex", github="pr/engram-agents/engram-paper/9300",
            )},
        )
        args = argparse.Namespace(pr="PR-9300", dry_run=False, force=False)
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_merge(args, owner_config, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        assert ("ci", "9300", "engram-agents/engram-paper") in calls
        captured = capsys.readouterr()
        assert "CI is red" in captured.err

    def test_merge_pr_number_mismatch_refuses(self, engram_home, monkeypatch, capsys):
        """The anchor's own PR number disagreeing with the CLI-resolved
        number is a red flag worth refusing on, not silently picking one --
        this would otherwise be a way for a mismatched/stale anchor to merge
        the wrong PR entirely."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr, repo=None: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("unknown", "no gh"))

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-9301", turn="alex", github="pr/engram-agents/engram-paper/9999",
            )},
        )
        args = argparse.Namespace(pr="PR-9301", dry_run=False, force=False)
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_merge(args, owner_config, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()
        captured = capsys.readouterr()
        assert "mismatch" in captured.err

    def test_merge_calls_gh_with_repo_flag(self, engram_home, monkeypatch):
        """The actual gate-5 `gh pr merge` invocation must carry --repo --
        this is the single most consequential call in the whole #1715 bug
        class (an irreversible squash-merge)."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr, repo=None: ("green", "ok"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("fresh", "ok"))

        captured_cmds = []

        def _fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        monkeypatch.setattr(_baton.subprocess, "run", _fake_run)

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-9302", turn="alex", github="pr/engram-agents/engram-paper/9302",
            )},
        )
        args = argparse.Namespace(pr="PR-9302", dry_run=False, force=False)
        _baton.cmd_merge(args, owner_config, "borges", client)

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "--repo" in cmd
        assert cmd[cmd.index("--repo") + 1] == "engram-agents/engram-paper"
        assert "9302" in cmd

    def test_merge_bare_anchor_refuses(self, engram_home, monkeypatch, capsys):
        """#1918: a baton with no github anchor at all refuses to merge --
        there is no default repo to guess against, and gate 5 is an
        irreversible `gh pr merge --squash`. Supersedes the old
        test_merge_bare_anchor_resolves_default_repo, which asserted the
        exact behavior this issue fixes."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("green", "ok")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("fresh", "ok"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-9303", turn="alex")},
        )
        args = argparse.Namespace(pr="PR-9303", dry_run=True, force=False)
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_merge(args, owner_config, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        assert calls == []  # refused before any gh call was made
        client.post.assert_not_called()
        captured = capsys.readouterr()
        assert "no repo-qualified github anchor" in captured.err

    def test_merge_bare_anchor_refuses_even_with_force(self, engram_home, monkeypatch, capsys):
        """#1918: --force skips CI/approval quality gates (3-4), never the
        repo-identity gate -- it has never meant 'merge without knowing
        which repo'."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        calls = []
        monkeypatch.setattr(
            _baton, "_pr_ci_state",
            lambda pr, repo=None: calls.append(("ci", pr, repo)) or ("green", "ok"),
        )
        monkeypatch.setattr(
            _baton, "_pr_approval_state",
            lambda pr, repo=None: calls.append(("approval", pr, repo)) or ("fresh", "ok"),
        )

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-9304", turn="alex")},
        )
        args = argparse.Namespace(pr="PR-9304", dry_run=False, force=True)
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_merge(args, owner_config, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        assert calls == []
        client.post.assert_not_called()

    def test_missing_config_fails_loud(self, capsys):
        """cmd_claim with empty config (no mode) exits EXIT_STATE (single-agent mode).

        Replaces the old subprocess test that verified missing config.json +
        no BATON_POOL_SENTINEL caused a loud failure. With the config-based gate,
        missing/empty config → mode defaults to 'single' → EXIT_STATE immediately,
        before any sentinel resolution is attempted.
        """
        client = _make_client()
        args = argparse.Namespace(project_id="pool-no-config")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, {}, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()
        assert "single-agent" in captured.err


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
            state, detail = baton._pr_ci_state("123", repo="example-org/example-repo")
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
            state, detail = baton._pr_ci_state("123", repo="example-org/example-repo")
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
            state, detail = baton._pr_ci_state("123", repo="example-org/example-repo")
        assert state == "red"
        assert "trap-check" in detail


# ===========================================================================
# CATEGORY 14: add-participant subcommand (#1609)
# ===========================================================================

class TestAddParticipant:
    """baton add-participant: arg wiring, error mapping, and the flip-unblock
    end-to-end regression guard (the whole point of the verb)."""

    def test_add_participant_posts_agent_and_participant(self):
        """Happy path: POSTs to /participants with {agent, participant}."""
        client = _make_client(
            post_return={"seq": 5, "added": True},
            get_return={"raw": _make_raw_baton("PR-600")},
        )
        args = argparse.Namespace(project_id="PR-600", participant="alex")
        _baton.cmd_add_participant(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-600/participants"
        assert body["agent"] == "borges"
        assert body["participant"] == "alex"

    def test_add_participant_normalizes_participant_case(self):
        client = _make_client(
            post_return={"seq": 5, "added": True},
            get_return={"raw": _make_raw_baton("PR-601")},
        )
        args = argparse.Namespace(project_id="PR-601", participant="  ALEX ")
        _baton.cmd_add_participant(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["participant"] == "alex"

    def test_add_participant_missing_project_exits_nonzero(self, capsys):
        """add-participant on a non-existent project exits non-zero."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-NONE", participant="alex")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_add_participant(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_add_participant_bad_project_id_exits_nonzero(self, engram_home):
        """add-participant with a malformed --project-id exits non-zero before any API call."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, "add-participant", "not a valid id!", "alex",
        )
        assert rc != 0

    def test_add_participant_server_403_maps_to_exit_validation(self, capsys):
        """Server-side NotAParticipant (agent not a current participant) → EXIT_VALIDATION."""
        client = _make_client(get_return={"raw": _make_raw_baton("PR-602")})
        client.post.side_effect = ForumHttpError(403, '{"error": "agent not a participant"}')
        args = argparse.Namespace(project_id="PR-602", participant="alex")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_add_participant(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "participant" in captured.err.lower()

    def test_add_participant_server_confirms_already_present_prints_friendly_message(self, capsys):
        """Server signals added=False (idempotent no-op) → friendly already-present message."""
        client = _make_client(
            post_return={"seq": 3, "added": False},
            get_return={"raw": _make_raw_baton("PR-603")},
        )
        args = argparse.Namespace(project_id="PR-603", participant="ariadne")  # already a seeded participant
        _baton.cmd_add_participant(args, MULTI_CONFIG, "borges", client)

        captured = capsys.readouterr()
        assert "already a participant" in captured.out.lower()

    def test_add_participant_single_agent_mode_exits_state(self):
        """add-participant in single-agent mode refuses (write command)."""
        client = _make_client()
        args = argparse.Namespace(project_id="PR-604", participant="alex")
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_add_participant(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_STATE

    def test_add_participant_then_flip_to_new_participant_succeeds(self, monkeypatch):
        """End-to-end regression guard: the whole point of the verb (#1609).

        Before add-participant, cmd_flip refuses ('not a participant') because
        the client-side check in cmd_flip reads the participants list from the
        baton. After add-participant, a baton read reflecting the addition
        (simulating the real server write) lets cmd_flip succeed.
        """
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        seed_raw = _make_raw_baton("PR-605", participants="[borges, ariadne]")

        # Step 1: flip to a not-yet-participant fails client-side.
        client = _make_client(get_return={"raw": seed_raw})
        flip_args = argparse.Namespace(
            project_id="PR-605", to="dana", reason="please review", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(flip_args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()

        # Step 2: add-participant succeeds (server would persist this).
        client.post.return_value = {"seq": 1, "added": True}
        add_args = argparse.Namespace(project_id="PR-605", participant="dana")
        _baton.cmd_add_participant(add_args, MULTI_CONFIG, "borges", client)

        # Step 3: simulate the server's post-add state (dana now a participant),
        # then flip to dana succeeds.
        client.get.return_value = {
            "raw": _make_raw_baton("PR-605", participants="[borges, ariadne, dana]"),
        }
        client.post.reset_mock()
        client.post.return_value = {"seq": 2}
        _baton.cmd_flip(flip_args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-605/flip"
        assert body["to_agent"] == "dana"


# ---------------------------------------------------------------------------
# #1715: repo-qualified PR anchors (fixes PR-baton IDs colliding across repos)
# ---------------------------------------------------------------------------

class TestParseGithubAnchor:
    def test_bare_pr_number(self):
        assert _baton._parse_github_anchor("pr/22") == ("pr", None, "22")

    def test_repo_qualified_pr(self):
        assert _baton._parse_github_anchor("pr/engram-agents/engram-paper/22") == (
            "pr", "engram-agents/engram-paper", "22",
        )

    def test_project_form(self):
        assert _baton._parse_github_anchor("project/4") == ("project", None, "4")

    def test_empty_string_returns_none(self):
        assert _baton._parse_github_anchor("") is None

    def test_garbage_returns_none(self):
        assert _baton._parse_github_anchor("not-an-anchor") is None


class TestInitRepoQualification:
    def test_init_auto_derives_bare_anchor_from_pr_project_id(self, monkeypatch):
        """A PR-<N> project_id with no --github auto-derives pr/<N> (#1715) --
        every PR-baton should carry SOME anchor from creation, not rely on
        downstream code re-deriving the number from project_id ad hoc."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-9001", title="auto anchor", participants="borges,ariadne",
            status="planning", turn=None, github=None, repo=None, colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        _, body = client.post.call_args[0]
        assert body.get("github") == "pr/9001"

    def test_init_with_repo_qualifies_anchor(self, monkeypatch):
        """--repo combines with the (auto-derived or explicit) pr/<N> anchor
        into the repo-qualified form."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-22", title="cross-repo PR", participants="luria,borges",
            status="in-progress", turn=None, github=None,
            repo="engram-agents/engram-paper", colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        _, body = client.post.call_args[0]
        assert body.get("github") == "pr/engram-agents/engram-paper/22"

    def test_init_with_repo_but_no_pr_anchor_rejected(self, monkeypatch):
        """--repo without a PR-shaped anchor (e.g. a project_id that isn't
        PR-<N> and no explicit --github) is a validation error, not a silent
        no-op -- --repo only makes sense qualifying a PR anchor."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="pool-something", title="not a PR baton",
            participants="borges,ariadne", status="planning", turn=None,
            github=None, repo="engram-agents/engram-paper", colleague=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()

    def test_init_rejects_malformed_repo_value(self, monkeypatch):
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-22", title="bad repo", participants="borges,ariadne",
            status="planning", turn=None, github=None,
            repo="not-a-valid-repo-format", colleague=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()

    def test_github_anchor_regex_accepts_repo_qualified_form(self):
        assert _baton._GITHUB_ANCHOR_RE.match("pr/engram-agents/engram-paper/22")
        assert _baton._GITHUB_ANCHOR_RE.match("pr/22")
        assert _baton._GITHUB_ANCHOR_RE.match("project/4")
        assert not _baton._GITHUB_ANCHOR_RE.match("pr/only-one-segment/22")

    def test_init_conflicting_github_and_repo_rejected(self, monkeypatch):
        """Reviewer-fairy-flagged operator-error case: an already
        repo-qualified --github combined with a DIFFERENT --repo must not
        silently overwrite the anchor's real repo -- reject and ask the
        caller to reconcile, rather than guess which one is intended."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-22", title="conflicting repo",
            participants="borges,ariadne", status="planning", turn=None,
            github="pr/engram-agents/engram-paper/22",
            repo="engram-agents/some-other-repo", colleague=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()

    def test_init_agreeing_github_and_repo_accepted(self, monkeypatch):
        """The non-conflicting case (--repo restates the SAME repo the
        --github anchor already carries) must still succeed."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-22", title="agreeing repo",
            participants="borges,ariadne", status="planning", turn=None,
            github="pr/engram-agents/engram-paper/22",
            repo="engram-agents/engram-paper", colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        _, body = client.post.call_args[0]
        assert body.get("github") == "pr/engram-agents/engram-paper/22"


class TestGcRepoAware:
    """#1715: cmd_gc must never guess a repo -- it reads the stored `github`
    anchor per project (never re-derives the PR number from project_id via
    regex, which has zero repo awareness) and SKIPS any PR-baton lacking a
    parseable PR anchor rather than defaulting silently.

    Sibling coverage: src/forum/tests/test_baton_thin_client.py::TestCmdGc
    tests the same cmd_gc surface from the thin-client-migration angle
    (POST body shape, error-code mapping). #1918 review round caught these
    two files drifting independently on the same anchor-contract change --
    if you touch cmd_gc's anchor handling, check both."""

    def test_gc_uses_repo_from_github_field_not_ambient_cwd(self, monkeypatch):
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-22", "github": "pr/engram-agents/engram-paper/22"},
        ]})
        captured_cmds = []

        def _fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "OPEN"
            return result

        monkeypatch.setattr(_baton.subprocess, "run", _fake_run)
        args = argparse.Namespace(dry_run=True, limit=30)
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "--repo" in cmd
        assert cmd[cmd.index("--repo") + 1] == "engram-agents/engram-paper"
        assert "22" in cmd

    def test_gc_skips_unqualified_anchor_never_guesses(self, monkeypatch, capsys):
        """#1918: a bare pr/<N> anchor (no repo) is skipped-and-counted like
        a missing anchor -- there is no default repo to pass to --repo.
        Supersedes the old test_gc_defaults_unqualified_anchor_to_default_repo_explicitly,
        which asserted the exact silent-guess behavior this issue fixes."""
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-1711", "github": "pr/1711"},
        ]})
        captured_cmds = []

        def _fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "MERGED"
            return result

        monkeypatch.setattr(_baton.subprocess, "run", _fake_run)
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        args = argparse.Namespace(dry_run=True, limit=30)
        _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        assert captured_cmds == []  # no gh call at all -- never guessed
        captured = capsys.readouterr()
        assert "skipped 1 PR-baton" in captured.err
        assert "unqualified" in captured.err

    def test_gc_skips_pr_baton_with_no_github_anchor(self, monkeypatch, capsys):
        """A PR-<N> baton with no (or unparseable) github field is skipped,
        not guessed at -- the pre-#1715 behavior derived the PR number from
        project_id alone and queried gh with zero repo context."""
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-9999", "github": None},
        ]})
        called = []
        monkeypatch.setattr(_baton.subprocess, "run", lambda *a, **kw: called.append(a) or MagicMock(returncode=0, stdout="OPEN"))
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        args = argparse.Namespace(dry_run=True, limit=30)
        _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        assert called == []  # no gh call at all for the ungauged baton
        captured = capsys.readouterr()
        assert "skipped 1 PR-baton" in captured.err

    def test_gc_two_different_repos_same_number_both_handled_correctly(self, monkeypatch):
        """The exact #1715 collision shape: two PR-batons for the SAME
        number (22) in DIFFERENT (both repo-qualified, per #1918 -- a bare
        anchor is never queried at all now) repos must each query their OWN
        repo and never share a result (reviewer-fairy caught: the original
        version of this test constructed only one project, so it never
        actually exercised the collision -- it was equivalent to the
        single-project test just above it)."""
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-9200", "github": "pr/engram-agents/engram-paper/22"},
            {"project_id": "PR-9201", "github": "pr/example-org/example-repo/22"},
        ]})
        captured_cmds = []

        def _fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            repo = cmd[cmd.index("--repo") + 1]
            result = MagicMock()
            result.returncode = 0
            # Different verdicts per repo -- proves the two #22s are queried
            # (and therefore could resolve) independently, not conflated.
            result.stdout = "MERGED" if repo == "engram-agents/engram-paper" else "OPEN"
            return result

        monkeypatch.setattr(_baton.subprocess, "run", _fake_run)
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        args = argparse.Namespace(dry_run=True, limit=30)
        _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        assert len(captured_cmds) == 2
        by_repo = {cmd[cmd.index("--repo") + 1]: cmd for cmd in captured_cmds}
        assert "engram-agents/engram-paper" in by_repo
        assert "example-org/example-repo" in by_repo
        # Both invocations target PR number 22, and each carries its OWN repo
        # -- confirms they are not the same call, and neither's repo bleeds
        # into the other's.
        assert "22" in by_repo["engram-agents/engram-paper"]
        assert "22" in by_repo["example-org/example-repo"]


class TestGcFailedVsSkipped:
    """#1918 defect 2: a gh query failure must be counted as `failed`, not
    `skipped` -- the old code bucketed both together, so a run's own
    summary line could read `failed 0` immediately after printing dozens of
    'gh query failed' lines, making a completely inert sweep look clean."""

    def test_gh_query_failure_counted_as_failed_not_skipped(self, monkeypatch, capsys):
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-31", "github": "pr/example-org/example-repo/31"},
        ]})
        monkeypatch.setattr(
            _baton.subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=1, stdout="", stderr="not found"),
        )
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        args = argparse.Namespace(dry_run=True, limit=30)

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        assert exc_info.value.code == EXIT_IO
        captured = capsys.readouterr()
        assert "would close 0, skipped 0, failed 1" in captured.out

    def test_pr_still_open_counted_as_skipped_not_failed(self, monkeypatch, capsys):
        """The companion case: a SUCCESSFUL gh query whose PR is simply
        still OPEN (no action needed) stays a `skipped`, not a `failed` --
        this test guards against over-correcting #1918 into flagging every
        non-close as an error."""
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-32", "github": "pr/example-org/example-repo/32"},
        ]})
        monkeypatch.setattr(
            _baton.subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="OPEN", stderr=""),
        )
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        args = argparse.Namespace(dry_run=True, limit=30)

        # No SystemExit -- a clean sweep with nothing to close is not a failure.
        _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        captured = capsys.readouterr()
        assert "would close 0, skipped 1, failed 0" in captured.out

    def test_mixed_run_exits_nonzero_and_reports_accurate_counts(self, monkeypatch, capsys):
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-41", "github": "pr/example-org/example-repo/41"},  # fails
            {"project_id": "PR-42", "github": "pr/example-org/example-repo/42"},  # open, skipped
            {"project_id": "PR-43", "github": "pr/example-org/example-repo/43"},  # merged, closed
        ]})

        def _fake_run(cmd, **kwargs):
            pr_num = cmd[3]  # ["gh", "pr", "view", pr_num, "--repo", ...]
            if pr_num == "41":
                return MagicMock(returncode=1, stdout="", stderr="gh: not found")
            if pr_num == "42":
                return MagicMock(returncode=0, stdout="OPEN", stderr="")
            return MagicMock(returncode=0, stdout="MERGED", stderr="")

        monkeypatch.setattr(_baton.subprocess, "run", _fake_run)
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        args = argparse.Namespace(dry_run=True, limit=30)

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        assert exc_info.value.code == EXIT_IO
        captured = capsys.readouterr()
        assert "would close 1, skipped 1, failed 1" in captured.out


class TestFlipRepoQualifiedGuard:
    """#1715 reviewer-fairy-caught blocker: cmd_flip's CI-green/approval-
    freshness/colleague-approval guard used a bare `^pr/(\\d+)$` regex that
    silently failed to match a repo-qualified anchor -- meaning the ENTIRE
    guard block was skipped, with no warning, for exactly the cross-repo
    PR-batons this feature exists to support. These tests prove the guard
    fires (and the resolved repo is threaded to the gh-querying helpers)
    for a repo-qualified anchor, not just the bare form."""

    def test_flip_repo_qualified_ci_red_refuses(self, monkeypatch, capsys):
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("red", "check 'pr-checks' conclusion=FAILURE")

        def _fake_approval_state(pr_num, repo=None):
            calls.append(("approval", pr_num, repo))
            return ("unknown", "not reached")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-9100", github="pr/engram-agents/engram-paper/9100",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-9100", to="casey", reason="ready", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()
        # The guard actually ran, and ran against the QUALIFIED repo, not
        # DEFAULT_GITHUB_REPO or an unresolved/None value.
        assert ("ci", "9100", "engram-agents/engram-paper") in calls
        captured = capsys.readouterr()
        assert "CI is red" in captured.err

    def test_flip_repo_qualified_ci_green_proceeds(self, monkeypatch):
        """Sanity companion: a genuinely green, fresh, approved cross-repo
        PR-baton still flips successfully -- the fix doesn't just make the
        guard fire, it must resolve correctly on the happy path too."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("green", "mergeStateStatus=CLEAN")

        def _fake_approval_state(pr_num, repo=None):
            calls.append(("approval", pr_num, repo))
            return ("fresh", "approved at current tip")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-9101", github="pr/engram-agents/engram-paper/9101",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-9101", to="casey", reason="ready", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        assert ("ci", "9101", "engram-agents/engram-paper") in calls
        assert ("approval", "9101", "engram-agents/engram-paper") in calls

    def test_flip_bare_anchor_skips_guard_never_guesses(self, monkeypatch, capsys):
        """#1918: an unqualified bare pr/<N> anchor skips the CI/approval/
        colleague guard entirely (no gh call, no guessed repo) rather than
        resolving against a default -- the flip still succeeds, just
        unverified, with a warning. Supersedes the old
        test_flip_bare_anchor_still_resolves_default_repo, which asserted
        the exact silent-guess behavior this issue fixes."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("green", "mergeStateStatus=CLEAN")

        def _fake_approval_state(pr_num, repo=None):
            calls.append(("approval", pr_num, repo))
            return ("fresh", "approved at current tip")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-9102", github="pr/9102")},
        )
        args = argparse.Namespace(
            project_id="PR-9102", to="casey", reason="ready", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        assert calls == []  # no gh call at all -- never guessed a repo
        captured = capsys.readouterr()
        assert "not repo-qualified" in captured.err
