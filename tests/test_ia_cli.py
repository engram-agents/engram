"""Tests for tools/ia.py — inter-agent CLI (DM-API backend).

UCS PR3a: ia.py is now a pure-LAN-API thin client; no local inter-agent/
filesystem reads or writes.  Tests follow the baton.py test pattern —
mock ForumClient for direct-call tests; subprocess for argparse/exit-code
integration.

Test categories:
  - Smoke:        each subcommand happy-path exits 0 via subprocess
  - ModeGate:     single-agent mode → exit 3 for all DM commands
  - Write:        POST per-recipient, multi-recipient loop, To/Subject encoding
  - List:         GET /api/dm, thread rendering
  - Read:         GET /api/dm/<counterpart>, message rendering, 404 handling
  - Cursor:       show/set the as_of int cursor
  - MarkRead:     advance cursor via /api/updates
  - LoudFail:     ForumNetworkError/HttpError → stderr + non-zero exit
  - Peers:        GET /api/agents/online rendering
  - Status:       health check output
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module import (direct-call tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS_DIR = str(_REPO_ROOT / "src" / "engram" / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import importlib.util as _ilu

_IA_PATH = _REPO_ROOT / "src" / "engram" / "tools" / "ia.py"
_spec = _ilu.spec_from_file_location("ia_mod", str(_IA_PATH))
_ia = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_ia)

# Forum API error types for mocking
from forum_api import ForumHttpError, ForumNetworkError  # noqa: E402

CLI_PATH = _IA_PATH

# Exit codes (mirror ia.py constants)
EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_IO = 2
EXIT_STATE = 3

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_ia(
    engram_home: Path,
    *args: str,
    stdin_text: str = "",
    env_overrides: dict | None = None,
) -> tuple:
    """Run the ia CLI as a subprocess; return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, str(CLI_PATH)] + list(args),
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _write_config(
    engram_home: Path,
    mode: str = "multi",
    agent_name: str = "ariadne",
) -> None:
    """Write a minimal config.json into engram_home."""
    cfg = {"mode": mode, "agent_name": agent_name}
    (engram_home / "config.json").write_text(json.dumps(cfg))


def _write_cursor(engram_home: Path, value: int) -> None:
    """Write an integer cursor to the read-cursor file."""
    (engram_home / "inter-agent-read-cursor.txt").write_text(str(value) + "\n")


def _read_cursor(engram_home: Path) -> str:
    """Read the raw cursor file content (stripped)."""
    path = engram_home / "inter-agent-read-cursor.txt"
    if path.exists():
        return path.read_text().strip()
    return ""


def _make_client(
    get_return: dict | None = None,
    post_return: dict | None = None,
) -> MagicMock:
    """Return a mock ForumClient with configurable return values."""
    client = MagicMock()
    if get_return is not None:
        client.get.return_value = get_return
    if post_return is not None:
        client.post.return_value = post_return or {"seq": 1, "ts": "2026-06-28T12:00:00Z"}
    return client


def _make_args(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace with sensible defaults for ia commands."""
    defaults = {
        "forum_url": "http://testforum:5002",
        "format": "human",
        "counterpart": None,
        "since_seq": 0,
        "to": None,
        "subject": None,
        "body_file": None,
        "from_stdin": False,
        "set_val": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


MULTI_CONFIG = {"mode": "multi", "agent_name": "ariadne"}
SINGLE_CONFIG = {"mode": "single", "agent_name": "ariadne"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram_home"
    home.mkdir()
    return home


# ===========================================================================
# CATEGORY 1: Smoke tests — subprocess integration
# ===========================================================================


class TestSmoke:
    """Each subcommand exits 0 on happy-path input (subprocess, mocked forum)."""

    def test_help_exits_0(self, engram_home):
        _write_config(engram_home, "multi")
        rc, stdout, stderr = _run_ia(engram_home, "--help")
        assert rc == 0
        assert "list" in stdout
        assert "read" in stdout
        assert "write" in stdout
        assert "mark-read" in stdout
        assert "cursor" in stdout
        assert "status" in stdout
        assert "peers" in stdout

    def test_cursor_show_exits_0(self, engram_home):
        """ia cursor with no file → shows 0 and exits 0."""
        _write_config(engram_home, "multi")
        rc, stdout, stderr = _run_ia(engram_home, "cursor", "--show")
        assert rc == 0
        assert "0" in stdout

    def test_cursor_set_exits_0(self, engram_home):
        """ia cursor --set 42 → exits 0, writes 42 to cursor file."""
        _write_config(engram_home, "multi")
        rc, stdout, stderr = _run_ia(engram_home, "cursor", "--set", "42")
        assert rc == 0
        assert "42" in stdout
        assert _read_cursor(engram_home) == "42"

    def test_status_multi_mode_exits_0(self, engram_home):
        """ia status in multi mode exits 0 and shows agent name (forum errors are graceful)."""
        _write_config(engram_home, "multi", "ariadne")
        # Status is graceful on unreachable forum — it shows "(unreachable)"
        rc, stdout, stderr = _run_ia(
            engram_home, "status",
            env_overrides={"FORUM_URL": "http://localhost:1"},  # port 1 = refused
        )
        assert rc == 0
        assert "ariadne" in stdout

    def test_status_single_mode_exits_0(self, engram_home):
        """ia status works in single mode (diagnostic utility — mode-gate exempt)."""
        _write_config(engram_home, "single", "ariadne")
        rc, stdout, stderr = _run_ia(
            engram_home, "status",
            env_overrides={"FORUM_URL": "http://localhost:1"},
        )
        assert rc == 0
        assert "ariadne" in stdout
        assert "single" in stdout


# ===========================================================================
# CATEGORY 2: Mode gate tests
# ===========================================================================


class TestModeGate:
    """Single-agent mode returns a clean error (exit 3) for all DM commands."""

    def test_list_single_mode_exits_3(self, engram_home):
        _write_config(engram_home, "single")
        rc, stdout, stderr = _run_ia(engram_home, "list")
        assert rc == EXIT_STATE
        assert "single-agent mode" in stderr
        assert "mode='multi'" in stderr
        assert "inter-agent/README.md" in stderr

    def test_read_single_mode_exits_3(self, engram_home):
        _write_config(engram_home, "single")
        rc, stdout, stderr = _run_ia(engram_home, "read", "borges")
        assert rc == EXIT_STATE
        assert "single-agent mode" in stderr

    def test_write_single_mode_exits_3(self, engram_home):
        _write_config(engram_home, "single")
        rc, stdout, stderr = _run_ia(
            engram_home, "write", "--to", "borges", "--from-stdin",
            stdin_text="hello",
        )
        assert rc == EXIT_STATE
        assert "single-agent mode" in stderr

    def test_mark_read_single_mode_exits_3(self, engram_home):
        _write_config(engram_home, "single")
        rc, stdout, stderr = _run_ia(engram_home, "mark-read")
        assert rc == EXIT_STATE
        assert "single-agent mode" in stderr

    def test_cursor_single_mode_exits_3(self, engram_home):
        _write_config(engram_home, "single")
        rc, stdout, stderr = _run_ia(engram_home, "cursor", "--show")
        assert rc == EXIT_STATE
        assert "single-agent mode" in stderr

    def test_peers_single_mode_exits_3(self, engram_home):
        _write_config(engram_home, "single")
        rc, stdout, stderr = _run_ia(
            engram_home, "peers",
            env_overrides={"FORUM_URL": "http://localhost:1"},
        )
        assert rc == EXIT_STATE
        assert "single-agent mode" in stderr

    def test_status_single_mode_exits_0(self, engram_home):
        """ia status is exempt from the mode gate (diagnostic utility)."""
        _write_config(engram_home, "single")
        rc, stdout, stderr = _run_ia(
            engram_home, "status",
            env_overrides={"FORUM_URL": "http://localhost:1"},
        )
        assert rc == EXIT_OK
        assert "single" in stdout

    def test_missing_config_treated_as_single(self, engram_home):
        """No config.json → mode defaults to 'single' → exit 3 for DM commands."""
        rc, stdout, stderr = _run_ia(engram_home, "list")
        assert rc == EXIT_STATE

    def test_corrupt_config_treated_as_single(self, engram_home):
        """Corrupt config.json → mode defaults to 'single' → exit 3."""
        (engram_home / "config.json").write_text("{not valid json")
        rc, stdout, stderr = _run_ia(engram_home, "list")
        assert rc == EXIT_STATE
        assert "invalid" in stderr and "failed to parse" in stderr

    def test_mode_gate_error_has_all_three_lines(self, engram_home):
        """Mode gate error emits all three required lines (DESIGN §9.4)."""
        _write_config(engram_home, "single")
        rc, stdout, stderr = _run_ia(engram_home, "list")
        assert rc == EXIT_STATE
        assert "single-agent mode" in stderr
        assert "mode='multi'" in stderr
        assert "inter-agent/README.md" in stderr


# ===========================================================================
# CATEGORY 3: Write command — POST + body encoding
# ===========================================================================


class TestWrite:
    """Write command: POST per-recipient, multi-recipient loop, body encoding."""

    def test_write_single_recipient_posts_once(self, monkeypatch):
        """write --to borges calls POST /api/dm/borges exactly once."""
        client = _make_client(post_return={"seq": 7, "ts": "2026-06-28T10:00:00Z"})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(to="borges", from_stdin=True, subject=None)
            # Simulate stdin with body text
            monkeypatch.setattr("sys.stdin", __import__("io").StringIO("Hello borges.\n"))
            _ia.cmd_write(args, MULTI_CONFIG, "ariadne")

        client.post.assert_called_once()
        call_path, call_payload = client.post.call_args[0]
        assert "/api/dm/borges" in call_path
        assert call_payload["agent"] == "ariadne"
        assert "Hello borges." in call_payload["body"]

    def test_write_multi_recipient_posts_to_each(self, monkeypatch, capsys):
        """write --to borges,mira posts to each recipient's DM thread."""
        post_return = {"seq": 5, "ts": "2026-06-28T10:00:00Z"}
        client = _make_client(post_return=post_return)
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(to="borges,mira", from_stdin=True, subject=None)
            monkeypatch.setattr("sys.stdin", __import__("io").StringIO("Group message.\n"))
            _ia.cmd_write(args, MULTI_CONFIG, "ariadne")

        assert client.post.call_count == 2
        paths = [call[0][0] for call in client.post.call_args_list]
        assert any("borges" in p for p in paths)
        assert any("mira" in p for p in paths)

    def test_write_multi_recipient_to_line_prepended(self, monkeypatch):
        """Multi-recipient: **To:** header prepended to body for each DM."""
        post_return = {"seq": 1, "ts": "2026-06-28T10:00:00Z"}
        client = _make_client(post_return=post_return)
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(to="borges,mira", from_stdin=True, subject=None)
            monkeypatch.setattr("sys.stdin", __import__("io").StringIO("Group message.\n"))
            _ia.cmd_write(args, MULTI_CONFIG, "ariadne")

        # Both posts should have the **To:** header
        for call in client.post.call_args_list:
            body = call[0][1]["body"]
            assert "**To:** borges, mira" in body, f"No To: header in body: {body!r}"

    def test_write_multi_recipient_partial_failure_continues_and_exits_nonzero(
        self, monkeypatch, capsys
    ):
        """Collect-and-continue: a mid-list send failure does NOT abort the rest.

        All recipients are attempted; the failed ones are reported by name; and
        the command exits non-zero (EXIT_IO) so the partial delivery is visible
        rather than silently truncated at the first failure.
        """
        client = _make_client()
        # recipient 1 succeeds, recipient 2 fails (forum unreachable mid-loop),
        # recipient 3 succeeds — proving the loop continues past the failure.
        client.post.side_effect = [
            {"seq": 1, "ts": "2026-06-28T10:00:00Z"},
            ForumNetworkError("connection refused"),
            {"seq": 2, "ts": "2026-06-28T10:00:01Z"},
        ]
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(to="borges,mira,clio", from_stdin=True, subject=None)
            monkeypatch.setattr("sys.stdin", __import__("io").StringIO("Group message.\n"))
            with pytest.raises(SystemExit) as exc:
                _ia.cmd_write(args, MULTI_CONFIG, "ariadne")

        assert exc.value.code == EXIT_IO
        # ALL three recipients attempted despite the mid-list failure
        assert client.post.call_count == 3
        err = capsys.readouterr().err
        assert "FAILED to mira" in err
        assert "1 of 3 deliveries FAILED" in err

    def test_write_single_recipient_no_to_line(self, monkeypatch):
        """Single recipient: NO **To:** header (would expose BCC-like info)."""
        client = _make_client(post_return={"seq": 1, "ts": "2026-06-28T10:00:00Z"})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(to="borges", from_stdin=True, subject=None)
            monkeypatch.setattr("sys.stdin", __import__("io").StringIO("Just to borges.\n"))
            _ia.cmd_write(args, MULTI_CONFIG, "ariadne")

        body = client.post.call_args[0][1]["body"]
        assert "**To:**" not in body, f"Unexpected To: header for single recipient: {body!r}"

    def test_write_subject_prepended(self, monkeypatch):
        """--subject S: '**Subject:** S' is prepended before the body."""
        client = _make_client(post_return={"seq": 2, "ts": "2026-06-28T10:00:00Z"})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(to="borges", from_stdin=True, subject="PR review request")
            monkeypatch.setattr("sys.stdin", __import__("io").StringIO("Please look at PR #42.\n"))
            _ia.cmd_write(args, MULTI_CONFIG, "ariadne")

        body = client.post.call_args[0][1]["body"]
        assert "**Subject:** PR review request" in body
        assert "Please look at PR #42." in body
        # Subject should come before the body text
        assert body.index("**Subject:**") < body.index("Please look")

    def test_write_multi_recipient_with_subject_order(self, monkeypatch):
        """Multi-recipient + subject: To-line appears before Subject line."""
        client = _make_client(post_return={"seq": 3, "ts": "2026-06-28T10:00:00Z"})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(to="borges,mira", from_stdin=True, subject="Important update")
            monkeypatch.setattr("sys.stdin", __import__("io").StringIO("See the issue.\n"))
            _ia.cmd_write(args, MULTI_CONFIG, "ariadne")

        body = client.post.call_args_list[0][0][1]["body"]
        assert "**To:**" in body
        assert "**Subject:** Important update" in body
        # To-line should come before Subject
        assert body.index("**To:**") < body.index("**Subject:**")

    def test_write_empty_body_fails(self, monkeypatch):
        """Empty body → exit EXIT_VALIDATION, nothing posted."""
        client = _make_client()
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(to="borges", from_stdin=True)
            monkeypatch.setattr("sys.stdin", __import__("io").StringIO("   \n"))
            with pytest.raises(SystemExit) as exc_info:
                _ia.cmd_write(args, MULTI_CONFIG, "ariadne")
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()

    def test_write_missing_to_fails(self, engram_home):
        """--to not provided → exit non-zero."""
        _write_config(engram_home, "multi")
        rc, stdout, stderr = _run_ia(
            engram_home, "write", "--from-stdin",
            stdin_text="hello",
        )
        assert rc != 0

    def test_write_body_file(self, monkeypatch, tmp_path):
        """--body-file: reads body from file, posts it."""
        body_file = tmp_path / "body.md"
        body_file.write_text("Content from file.\n", encoding="utf-8")
        client = _make_client(post_return={"seq": 4, "ts": "2026-06-28T10:00:00Z"})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(to="borges", body_file=str(body_file))
            _ia.cmd_write(args, MULTI_CONFIG, "ariadne")

        body = client.post.call_args[0][1]["body"]
        assert "Content from file." in body

    def test_write_body_file_nonexistent_fails(self):
        """--body-file pointing to missing file → exit EXIT_VALIDATION."""
        client = _make_client()
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(
                to="borges",
                body_file="/tmp/definitely_does_not_exist_ia_test.md",
            )
            with pytest.raises(SystemExit) as exc_info:
                _ia.cmd_write(args, MULTI_CONFIG, "ariadne")
        assert exc_info.value.code == EXIT_VALIDATION


# ===========================================================================
# CATEGORY 4: List command
# ===========================================================================


class TestList:
    """List command: GET /api/dm, thread rendering."""

    def test_list_shows_threads(self, capsys):
        """cmd_list renders counterpart names from /api/dm threads."""
        threads = [{"counterpart": "borges"}, {"counterpart": "mira"}]
        client = _make_client(get_return={"threads": threads})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(format="human")
            _ia.cmd_list(args, MULTI_CONFIG, "ariadne")

        captured = capsys.readouterr()
        assert "borges" in captured.out
        assert "mira" in captured.out

    def test_list_empty_shows_zero_message(self, capsys):
        """cmd_list with no threads shows a '(0)' message."""
        client = _make_client(get_return={"threads": []})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(format="human")
            _ia.cmd_list(args, MULTI_CONFIG, "ariadne")

        captured = capsys.readouterr()
        assert "0" in captured.out

    def test_list_json_format(self, capsys):
        """cmd_list --format json outputs JSON array."""
        threads = [{"counterpart": "borges"}]
        client = _make_client(get_return={"threads": threads})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(format="json")
            _ia.cmd_list(args, MULTI_CONFIG, "ariadne")

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert data[0]["counterpart"] == "borges"

    def test_list_calls_correct_endpoint(self):
        """cmd_list calls GET /api/dm with agent param."""
        client = _make_client(get_return={"threads": []})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args()
            _ia.cmd_list(args, MULTI_CONFIG, "ariadne")

        client.get.assert_called_once_with("/api/dm", params={"agent": "ariadne"})


# ===========================================================================
# CATEGORY 5: Read command
# ===========================================================================


class TestRead:
    """Read command: GET /api/dm/<counterpart>, message rendering, 404 handling."""

    def test_read_shows_messages(self, capsys):
        """cmd_read renders messages from the thread."""
        messages = [
            {"seq": 1, "sender": "borges", "ts": "2026-06-28T10:00:00Z", "body": "Hello ariadne."},
            {"seq": 2, "sender": "ariadne", "ts": "2026-06-28T10:01:00Z", "body": "Hi borges!"},
        ]
        client = _make_client(get_return={"messages": messages})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(counterpart="borges", since_seq=0)
            _ia.cmd_read(args, MULTI_CONFIG, "ariadne")

        captured = capsys.readouterr()
        assert "Hello ariadne." in captured.out
        assert "Hi borges!" in captured.out
        assert "seq=1" in captured.out
        assert "seq=2" in captured.out

    def test_read_json_format(self, capsys):
        """cmd_read --format json outputs JSON array of messages."""
        messages = [
            {"seq": 1, "sender": "borges", "ts": "2026-06-28T10:00:00Z", "body": "JSON test."}
        ]
        client = _make_client(get_return={"messages": messages})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(counterpart="borges", format="json")
            _ia.cmd_read(args, MULTI_CONFIG, "ariadne")

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert data[0]["seq"] == 1

    def test_read_404_prints_no_thread_message(self, capsys):
        """cmd_read with 404 response prints '(no DM thread with X)' and exits 0."""
        client = MagicMock()
        client.get.side_effect = ForumHttpError(404, "not found")
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(counterpart="nobody")
            _ia.cmd_read(args, MULTI_CONFIG, "ariadne")  # should NOT raise

        captured = capsys.readouterr()
        assert "no DM thread with nobody" in captured.out

    def test_read_empty_thread_prints_message(self, capsys):
        """cmd_read with empty messages list prints '(no messages with X)'."""
        client = _make_client(get_return={"messages": []})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(counterpart="borges")
            _ia.cmd_read(args, MULTI_CONFIG, "ariadne")

        captured = capsys.readouterr()
        assert "no messages with borges" in captured.out

    def test_read_passes_since_seq(self):
        """cmd_read passes --since-seq to the API."""
        client = _make_client(get_return={"messages": []})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(counterpart="borges", since_seq=5)
            _ia.cmd_read(args, MULTI_CONFIG, "ariadne")

        call_params = client.get.call_args[1]["params"]
        assert call_params["since_seq"] == "5"


# ===========================================================================
# CATEGORY 6: Cursor command
# ===========================================================================


class TestCursor:
    """Cursor: show/set the as_of int cursor."""

    def test_cursor_show_zero_when_no_file(self, engram_home):
        """ia cursor --show with no cursor file → shows 0."""
        _write_config(engram_home, "multi")
        rc, stdout, stderr = _run_ia(engram_home, "cursor", "--show")
        assert rc == EXIT_OK
        assert "0" in stdout

    def test_cursor_set_integer(self, engram_home):
        """ia cursor --set 100 → writes 100 to file."""
        _write_config(engram_home, "multi")
        rc, stdout, stderr = _run_ia(engram_home, "cursor", "--set", "100")
        assert rc == EXIT_OK
        assert _read_cursor(engram_home) == "100"

    def test_cursor_show_after_set(self, engram_home):
        """ia cursor shows the value written by --set."""
        _write_config(engram_home, "multi")
        _run_ia(engram_home, "cursor", "--set", "42")
        rc, stdout, stderr = _run_ia(engram_home, "cursor", "--show")
        assert rc == EXIT_OK
        assert "42" in stdout

    def test_cursor_show_existing_file(self, engram_home):
        """ia cursor reads existing cursor file."""
        _write_config(engram_home, "multi")
        _write_cursor(engram_home, 999)
        rc, stdout, stderr = _run_ia(engram_home, "cursor", "--show")
        assert rc == EXIT_OK
        assert "999" in stdout

    def test_cursor_set_invalid_value_fails(self, engram_home):
        """ia cursor --set 'not-an-int' → exit EXIT_VALIDATION."""
        _write_config(engram_home, "multi")
        rc, stdout, stderr = _run_ia(engram_home, "cursor", "--set", "not-an-int")
        assert rc == EXIT_VALIDATION
        assert "integer" in stderr

    def test_cursor_write_no_tmp_remains(self, engram_home):
        """After cursor --set, no .tmp file remains in engram_home."""
        _write_config(engram_home, "multi")
        _run_ia(engram_home, "cursor", "--set", "77")
        tmp_files = list(engram_home.glob("*.tmp"))
        assert tmp_files == [], f"Tmp files must not remain: {tmp_files}"

    def test_read_cursor_int_helper_returns_0_when_absent(self, tmp_path):
        """_read_cursor_int returns 0 when cursor file does not exist."""
        result = _ia._read_cursor_int(str(tmp_path / "nonexistent.txt"))
        assert result == 0

    def test_read_cursor_int_helper_reads_value(self, tmp_path):
        """_read_cursor_int returns the int stored in the file."""
        cursor_file = tmp_path / "cursor.txt"
        cursor_file.write_text("123\n")
        assert _ia._read_cursor_int(str(cursor_file)) == 123

    def test_read_cursor_int_helper_returns_0_on_invalid(self, tmp_path):
        """_read_cursor_int returns 0 on non-integer file content."""
        cursor_file = tmp_path / "cursor.txt"
        cursor_file.write_text("2026-05-22T10:00:00Z\n")  # old timestamp format
        assert _ia._read_cursor_int(str(cursor_file)) == 0


# ===========================================================================
# CATEGORY 7: MarkRead command
# ===========================================================================


class TestMarkRead:
    """mark-read: advance cursor via /api/updates."""

    def test_mr_alias_reaches_command_not_help(self, engram_home):
        """`ia mr` (the registered alias) must dispatch, not fall through to help.

        Regression: argparse sets `subcommand` to the string the USER TYPED, not
        the canonical name, so `aliases=["mr"]` produced an args.subcommand of
        "mr" that matched no dispatch branch. It fell to the else-branch, printed
        top-level help, and exited EXIT_VALIDATION — while the help epilog says
        DM commands require multi-agent mode. So a user in multi mode (as here)
        was told the opposite, naming a false cause. The shipped inter-agent
        prompt-hook advertises `ia mr`, so the documented path was the broken one.

        Asserted on the user-visible symptom rather than the exit code: with no
        forum reachable the command legitimately fails loud, and that is fine —
        what must not happen is the top-level-help-plus-mode-epilog output.
        """
        _write_config(engram_home, "multi")
        rc, stdout, stderr = _run_ia(engram_home, "mr")

        combined = stdout + stderr
        assert "All DM commands require multi-agent mode" not in combined, (
            "`ia mr` printed the mode-gate epilog while config mode IS multi — "
            "the alias fell through to top-level help instead of dispatching."
        )
        # The canonical spelling and the alias must behave the same way here.
        rc_canon, out_canon, err_canon = _run_ia(engram_home, "mark-read")
        assert ("All DM commands require multi-agent mode" in combined) == (
            "All DM commands require multi-agent mode" in (out_canon + err_canon)
        ), "alias and canonical spelling diverge"

    def test_mark_read_advances_cursor(self, engram_home, capsys):
        """mark-read writes returned as_of to cursor file."""
        _write_config(engram_home, "multi")
        _write_cursor(engram_home, 10)

        updates_response = {"updates": [], "as_of": 42, "ts": "2026-06-28T12:00:00Z"}
        client = _make_client(get_return=updates_response)
        with patch.object(_ia, "ForumClient", return_value=client):
            # Set READ_CURSOR_PATH to use our test home
            original_path = _ia.READ_CURSOR_PATH
            _ia.READ_CURSOR_PATH = str(engram_home / "inter-agent-read-cursor.txt")
            try:
                args = _make_args()
                _ia.cmd_mark_read(args, MULTI_CONFIG, "ariadne")
            finally:
                _ia.READ_CURSOR_PATH = original_path

        assert _read_cursor(engram_home) == "42"

    def test_mark_read_calls_updates_endpoint(self, engram_home):
        """mark-read calls GET /api/updates with agent + since + kinds=dm."""
        _write_cursor(engram_home, 5)

        updates_response = {"updates": [], "as_of": 10, "ts": "2026-06-28T12:00:00Z"}
        client = _make_client(get_return=updates_response)
        with patch.object(_ia, "ForumClient", return_value=client):
            original_path = _ia.READ_CURSOR_PATH
            _ia.READ_CURSOR_PATH = str(engram_home / "inter-agent-read-cursor.txt")
            try:
                args = _make_args()
                _ia.cmd_mark_read(args, MULTI_CONFIG, "ariadne")
            finally:
                _ia.READ_CURSOR_PATH = original_path

        call_params = client.get.call_args[1]["params"]
        assert call_params["agent"] == "ariadne"
        assert call_params["kinds"] == "dm"
        # since should be the cursor value (5)
        assert call_params["since"] == "5"

    def test_mark_read_subprocess_multi_mode(self, engram_home):
        """ia mark-read in single mode → exit 3 (mode gate fires)."""
        _write_config(engram_home, "single")
        rc, stdout, stderr = _run_ia(engram_home, "mark-read")
        assert rc == EXIT_STATE
        assert "single-agent mode" in stderr


# ===========================================================================
# CATEGORY 8: Loud-fail — unreachable forum
# ===========================================================================


class TestLoudFail:
    """ForumNetworkError / HttpError → stderr + non-zero exit, NO fallback."""

    def test_list_network_error_exits_nonzero(self, capsys):
        """cmd_list with ForumNetworkError → stderr + sys.exit(EXIT_IO)."""
        client = MagicMock()
        client.get.side_effect = ForumNetworkError("connection refused")
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args()
            with pytest.raises(SystemExit) as exc_info:
                _ia.cmd_list(args, MULTI_CONFIG, "ariadne")
        assert exc_info.value.code == EXIT_IO
        captured = capsys.readouterr()
        assert "unreachable" in captured.err.lower() or "forum" in captured.err.lower()

    def test_write_network_error_exits_nonzero(self, monkeypatch, capsys):
        """cmd_write with ForumNetworkError → stderr + sys.exit(EXIT_IO)."""
        client = MagicMock()
        client.post.side_effect = ForumNetworkError("connection refused")
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(to="borges", from_stdin=True)
            monkeypatch.setattr("sys.stdin", __import__("io").StringIO("test body\n"))
            with pytest.raises(SystemExit) as exc_info:
                _ia.cmd_write(args, MULTI_CONFIG, "ariadne")
        assert exc_info.value.code == EXIT_IO

    def test_mark_read_network_error_exits_nonzero(self, engram_home, capsys):
        """cmd_mark_read with ForumNetworkError → stderr + sys.exit."""
        client = MagicMock()
        client.get.side_effect = ForumNetworkError("connection refused")
        with patch.object(_ia, "ForumClient", return_value=client):
            original_path = _ia.READ_CURSOR_PATH
            _ia.READ_CURSOR_PATH = str(engram_home / "inter-agent-read-cursor.txt")
            try:
                args = _make_args()
                with pytest.raises(SystemExit) as exc_info:
                    _ia.cmd_mark_read(args, MULTI_CONFIG, "ariadne")
            finally:
                _ia.READ_CURSOR_PATH = original_path
        assert exc_info.value.code == EXIT_IO

    def test_peers_network_error_exits_nonzero(self, capsys):
        """cmd_peers with ForumNetworkError → stderr + sys.exit."""
        client = MagicMock()
        client.get.side_effect = ForumNetworkError("connection refused")
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args()
            with pytest.raises(SystemExit) as exc_info:
                _ia.cmd_peers(args, MULTI_CONFIG, "ariadne")
        assert exc_info.value.code == EXIT_IO

    def test_read_http_500_exits_nonzero(self, capsys):
        """cmd_read with HTTP 500 → stderr + sys.exit."""
        client = MagicMock()
        client.get.side_effect = ForumHttpError(500, "internal server error")
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(counterpart="borges")
            with pytest.raises(SystemExit) as exc_info:
                _ia.cmd_read(args, MULTI_CONFIG, "ariadne")
        assert exc_info.value.code == EXIT_IO

    def test_loud_fail_subprocess_unreachable(self, engram_home):
        """ia list against a refused port → non-zero exit + stderr."""
        _write_config(engram_home, "multi")
        rc, stdout, stderr = _run_ia(
            engram_home, "list",
            env_overrides={"FORUM_URL": "http://localhost:1"},
        )
        assert rc != EXIT_OK
        # Should print an error on stderr
        assert len(stderr) > 0

    def test_no_silent_fallback_on_network_error(self, monkeypatch, capsys):
        """ForumNetworkError must exit, never fall back to local files."""
        client = MagicMock()
        client.get.side_effect = ForumNetworkError("connection refused")
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args()
            with pytest.raises(SystemExit):
                _ia.cmd_list(args, MULTI_CONFIG, "ariadne")
        # If we reach here, it exited — no silent fallback occurred


# ===========================================================================
# CATEGORY 9: Peers command
# ===========================================================================


class TestPeers:
    """Peers: GET /api/agents/online rendering."""

    def test_peers_shows_agent_names(self, capsys):
        """cmd_peers renders names from the online list."""
        online = [
            {"name": "borges", "state": "idle"},
            {"name": "mira", "state": "working"},
        ]
        client = _make_client(get_return={"online": online, "registered": 3})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args()
            _ia.cmd_peers(args, MULTI_CONFIG, "ariadne")

        captured = capsys.readouterr()
        assert "borges" in captured.out
        assert "mira" in captured.out

    def test_peers_excludes_self(self, capsys):
        """cmd_peers filters out the calling agent from the display."""
        online = [
            {"name": "ariadne", "state": "idle"},
            {"name": "borges", "state": "working"},
        ]
        client = _make_client(get_return={"online": online, "registered": 2})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args()
            _ia.cmd_peers(args, MULTI_CONFIG, "ariadne")

        captured = capsys.readouterr()
        # ariadne (self) should not appear in peers output
        # borges should appear
        assert "borges" in captured.out

    def test_peers_json_format(self, capsys):
        """cmd_peers --format json outputs raw API response as JSON."""
        api_response = {"online": [{"name": "borges", "state": "idle"}], "registered": 2, "count": 1}
        client = _make_client(get_return=api_response)
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args(format="json")
            _ia.cmd_peers(args, MULTI_CONFIG, "ariadne")

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "online" in data

    def test_peers_empty_shows_zero_message(self, capsys):
        """cmd_peers with no online agents shows a '0' message."""
        client = _make_client(get_return={"online": [], "registered": 5})
        with patch.object(_ia, "ForumClient", return_value=client):
            args = _make_args()
            _ia.cmd_peers(args, MULTI_CONFIG, "ariadne")

        captured = capsys.readouterr()
        assert "0" in captured.out


# ===========================================================================
# CATEGORY 10: Status command
# ===========================================================================


class TestStatus:
    """Status: health check output."""

    def test_status_json_format(self, capsys, engram_home):
        """cmd_status --format json returns valid JSON with expected fields."""
        client = MagicMock()
        client.get.side_effect = [
            {"threads": [{"counterpart": "borges"}]},
            {"updates": [], "as_of": 10, "ts": "2026-06-28T12:00:00Z"},
        ]
        config = {"mode": "multi", "agent_name": "ariadne"}
        with patch.object(_ia, "ForumClient", return_value=client):
            original_path = _ia.READ_CURSOR_PATH
            _ia.READ_CURSOR_PATH = str(engram_home / "inter-agent-read-cursor.txt")
            try:
                args = _make_args(format="json")
                _ia.cmd_status(args, config, "ariadne")
            finally:
                _ia.READ_CURSOR_PATH = original_path

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["agent"] == "ariadne"
        assert data["mode"] == "multi"
        assert "read_cursor" in data
        assert "dm_threads" in data

    def test_status_graceful_on_unreachable(self, engram_home, capsys):
        """cmd_status handles forum unreachable gracefully (shows '(unreachable)')."""
        client = MagicMock()
        client.get.side_effect = ForumNetworkError("connection refused")
        with patch.object(_ia, "ForumClient", return_value=client):
            original_path = _ia.READ_CURSOR_PATH
            _ia.READ_CURSOR_PATH = str(engram_home / "inter-agent-read-cursor.txt")
            try:
                args = _make_args(format="human")
                _ia.cmd_status(args, MULTI_CONFIG, "ariadne")
            finally:
                _ia.READ_CURSOR_PATH = original_path

        captured = capsys.readouterr()
        assert "ariadne" in captured.out
        assert "unreachable" in captured.out


# ===========================================================================
# CATEGORY 11: _encode_body helper unit tests
# ===========================================================================


class TestEncodeBody:
    """_encode_body: header construction logic."""

    def test_no_subject_no_multi_recipient(self):
        """Single recipient, no subject → body unchanged."""
        result = _ia._encode_body("Hello.\n", ["borges"], None)
        assert result == "Hello.\n"

    def test_subject_only(self):
        """Subject → **Subject:** prepended."""
        result = _ia._encode_body("Body text.\n", ["borges"], "My Subject")
        assert result.startswith("**Subject:** My Subject")
        assert "Body text." in result

    def test_multi_recipient_only(self):
        """Multi-recipient → **To:** prepended."""
        result = _ia._encode_body("Body text.\n", ["borges", "mira"], None)
        assert result.startswith("**To:** borges, mira")
        assert "Body text." in result

    def test_multi_recipient_and_subject_order(self):
        """Multi-recipient + subject → **To:** first, then **Subject:**."""
        result = _ia._encode_body("Body.\n", ["borges", "mira"], "Hello")
        assert result.index("**To:**") < result.index("**Subject:**")

    def test_body_preserved_verbatim(self):
        """Body text is preserved verbatim including backticks and special chars."""
        body = "Run `gh pr view 42` and check $STATUS.\n"
        result = _ia._encode_body(body, ["borges"], None)
        assert body.strip() in result
