"""Tests for the substrate-health probe injected by the SessionStart hook.

PR: feat(hooks): SessionStart substrate-health probe (#198 + #248 Item 2)

Closes #198 (MCP server health warning), mitigates #248 Item 2 (surface
daemon health warning). Both probes are advisory — any unexpected probe
exception is treated as ok=True to ensure session start is never blocked.

Seven test cases:
  1. test_mcp_ok_daemon_ok_no_warning         — both healthy, no extra lines
  2. test_mcp_offline_warning_includes_reason — MCP down, reason in output
  3. test_daemon_offline_warning_includes_reason — daemon down, reason in output
  4. test_both_offline_emits_both_warnings    — both down, both lines present
  5. test_pgrep_timeout_treated_as_ok         — probe timeout doesn't block
  6. test_socket_missing_treated_as_daemon_down — no socket file
  7. test_socket_connect_refused_treated_as_daemon_down — listening socket gone
"""

import importlib.util
import io
import json
import os
import socket as socket_module
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers shared with test_session_start_hook_calibration.py
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "hooks" / "claude" / "engram-session-start-hook.py"


def _load_hook_module(engram_home: Path):
    """Load the session-start hook with ENGRAM_HOME pointing at engram_home.

    Forces a fresh import each call. Stubs context_tracker so the module-level
    import doesn't fail when there is no real baseline DB.
    """
    os.environ["ENGRAM_HOME"] = str(engram_home)

    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct

    for mod_name in list(sys.modules.keys()):
        if mod_name in (
            "engram-session-start-hook",
            "engram_session_start_hook",
        ):
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location("engram_session_start_hook", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_hook_main(module, hook_input: dict) -> dict:
    """Run module.main() with hook_input on stdin; return parsed output JSON."""
    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with redirect_stdout(buf):
            module.main()
    return json.loads(buf.getvalue().strip())


def _make_hook_input(tmp_path: Path, source: str = "startup") -> dict:
    return {
        "session_id": "test-session-health-probe",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
        "source": source,
    }


def _make_engram_home(tmp_path: Path) -> Path:
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()
    return engram_home


def _mock_no_engram_client():
    """Patch sys.modules so engram_client import raises (calibration silently skipped)."""
    return patch.dict("sys.modules", {"engram_client": None})


# ---------------------------------------------------------------------------
# 1. Both healthy — no warning
# ---------------------------------------------------------------------------

def test_mcp_ok_daemon_ok_no_warning(tmp_path):
    """When both MCP server and daemon are healthy, no substrate-health warning emitted."""
    engram_home = _make_engram_home(tmp_path)
    mod = _load_hook_module(engram_home)

    # Create a real (empty) server.py so pgrep has a path to search for.
    server_py = engram_home / "server.py"
    server_py.write_text("# stub\n")

    # pgrep returns 0 (found), daemon socket connect succeeds
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            with patch.object(mod, "_check_surface_daemon_health", return_value=(True, None)):
                result = _run_hook_main(mod, _make_hook_input(tmp_path))

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "⚠️" not in ctx, f"Expected no health warning but got: {ctx!r}"
    assert "ENGRAM substrate health" not in ctx


# ---------------------------------------------------------------------------
# 2. MCP offline
# ---------------------------------------------------------------------------

def test_mcp_offline_warning_includes_reason(tmp_path):
    """When MCP server is offline, warning block appears with the reason string."""
    engram_home = _make_engram_home(tmp_path)
    mod = _load_hook_module(engram_home)

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(False, "no engram server process found")):
            with patch.object(mod, "_check_surface_daemon_health", return_value=(True, None)):
                result = _run_hook_main(mod, _make_hook_input(tmp_path))

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "ENGRAM substrate health" in ctx
    assert "MCP server: OFFLINE" in ctx
    assert "no engram server process found" in ctx
    assert "Tell the user immediately" in ctx
    # Daemon should NOT appear
    assert "Surface daemon: OFFLINE" not in ctx


# ---------------------------------------------------------------------------
# 3. Daemon offline
# ---------------------------------------------------------------------------

def test_daemon_offline_warning_includes_reason(tmp_path):
    """When surface daemon is offline, warning block appears with the reason string."""
    engram_home = _make_engram_home(tmp_path)
    mod = _load_hook_module(engram_home)

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            with patch.object(mod, "_check_surface_daemon_health", return_value=(False, "socket file not found")):
                result = _run_hook_main(mod, _make_hook_input(tmp_path))

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "ENGRAM substrate health" in ctx
    assert "Surface daemon: OFFLINE" in ctx
    assert "socket file not found" in ctx
    assert "Semantic recall degrades" in ctx
    # MCP should NOT appear
    assert "MCP server: OFFLINE" not in ctx


# ---------------------------------------------------------------------------
# 4. Both offline
# ---------------------------------------------------------------------------

def test_both_offline_emits_both_warnings(tmp_path):
    """When both are offline, both warning lines appear in the output."""
    engram_home = _make_engram_home(tmp_path)
    mod = _load_hook_module(engram_home)

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(False, "no engram server process found")):
            with patch.object(mod, "_check_surface_daemon_health", return_value=(False, "connect refused")):
                result = _run_hook_main(mod, _make_hook_input(tmp_path))

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "ENGRAM substrate health" in ctx
    assert "MCP server: OFFLINE" in ctx
    assert "Surface daemon: OFFLINE" in ctx
    assert "no engram server process found" in ctx
    assert "connect refused" in ctx


# ---------------------------------------------------------------------------
# 5a. Unexpected exception in pgrep treated as ok (advisory)
# ---------------------------------------------------------------------------

def test_pgrep_unexpected_exception_treated_as_ok(tmp_path):
    """When subprocess.run raises an unexpected Exception (not TimeoutExpired),
    _check_mcp_health returns ok=True and no warning is emitted.

    This is the generic-exception advisory path: the probe must never block
    session start, so any exception that isn't explicitly handled returns
    ok=True. Compare test_pgrep_timeout_treated_as_offline which covers the
    subprocess.TimeoutExpired branch (explicitly handled → ok=False).
    """
    engram_home = _make_engram_home(tmp_path)
    mod = _load_hook_module(engram_home)

    # Patch subprocess.run to raise an unexpected RuntimeError only for pgrep
    # calls — let other subprocess.run calls (e.g. git log in piece_c_git_log)
    # pass through normally.
    import subprocess as _subprocess

    original_run = _subprocess.run

    def _selective_raise(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and cmd and cmd[0] == "pgrep":
            raise RuntimeError("unexpected probe error")
        return original_run(*args, **kwargs)

    with _mock_no_engram_client():
        with patch("subprocess.run", side_effect=_selective_raise):
            with patch.object(mod, "_check_surface_daemon_health", return_value=(True, None)):
                result = _run_hook_main(mod, _make_hook_input(tmp_path))

    ctx = result["hookSpecificOutput"]["additionalContext"]
    # Unexpected exception → ok=True → no warning
    assert "MCP server: OFFLINE" not in ctx, (
        "Unexpected probe exception should be treated as ok=True (advisory)"
    )


# ---------------------------------------------------------------------------
# 5b. subprocess.TimeoutExpired treated as offline (explicitly handled)
# ---------------------------------------------------------------------------

def test_pgrep_timeout_treated_as_offline(tmp_path):
    """When subprocess.run raises subprocess.TimeoutExpired, _check_mcp_health
    returns ok=False with reason 'pgrep timed out' and a warning is emitted.

    subprocess.TimeoutExpired is caught explicitly in _check_mcp_health (not
    the generic except-Exception advisory branch), so it produces ok=False —
    a timed-out pgrep means we cannot confirm the server is running.
    """
    import subprocess as _subprocess

    engram_home = _make_engram_home(tmp_path)
    mod = _load_hook_module(engram_home)

    original_run = _subprocess.run

    def _selective_timeout(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and cmd and cmd[0] == "pgrep":
            raise _subprocess.TimeoutExpired(cmd=cmd, timeout=1)
        return original_run(*args, **kwargs)

    with _mock_no_engram_client():
        with patch("subprocess.run", side_effect=_selective_timeout):
            with patch.object(mod, "_check_surface_daemon_health", return_value=(True, None)):
                result = _run_hook_main(mod, _make_hook_input(tmp_path))

    ctx = result["hookSpecificOutput"]["additionalContext"]
    # TimeoutExpired → ok=False → warning present
    assert "MCP server: OFFLINE" in ctx, (
        "subprocess.TimeoutExpired should be treated as ok=False (pgrep timed out)"
    )
    assert "pgrep timed out" in ctx


# ---------------------------------------------------------------------------
# 6. Socket file missing → daemon down
# ---------------------------------------------------------------------------

def test_socket_missing_treated_as_daemon_down(tmp_path):
    """When the daemon socket file does not exist, daemon is reported as offline."""
    engram_home = _make_engram_home(tmp_path)
    mod = _load_hook_module(engram_home)

    # No recall-daemon.sock in engram_home → os.path.exists returns False
    # Ensure the socket does NOT exist.
    socket_path = engram_home / "recall-daemon.sock"
    assert not socket_path.exists(), "Socket should not exist for this test"

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            # Call the real _check_surface_daemon_health (not mocked)
            result = _run_hook_main(mod, _make_hook_input(tmp_path))

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Surface daemon: OFFLINE" in ctx
    assert "socket file not found" in ctx


# ---------------------------------------------------------------------------
# 7. Socket connect refused → daemon down
# ---------------------------------------------------------------------------

def test_socket_connect_refused_treated_as_daemon_down(tmp_path):
    """When the daemon socket exists but connect is refused, daemon is offline."""
    engram_home = _make_engram_home(tmp_path)
    mod = _load_hook_module(engram_home)

    # Create a socket file path (but no daemon listening on it).
    # We'll use a real AF_UNIX socket that is created + immediately closed
    # (no accept loop), so connect raises ConnectionRefusedError.
    socket_path = engram_home / "recall-daemon.sock"

    # Bind a socket and immediately close without accepting — this leaves a
    # socket file present but with no listener, so connect() raises
    # ConnectionRefusedError on Linux.
    srv = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    try:
        srv.bind(str(socket_path))
        # Do NOT call srv.listen() so connect raises ConnectionRefusedError.
        srv.close()

        with _mock_no_engram_client():
            with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
                result = _run_hook_main(mod, _make_hook_input(tmp_path))
    finally:
        srv.close()
        if socket_path.exists():
            socket_path.unlink()

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Surface daemon: OFFLINE" in ctx
    # The reason should be either "connect refused" or an OSError variant.
    assert "OFFLINE" in ctx
