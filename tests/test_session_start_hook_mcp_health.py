"""SessionStart MCP-health probe platform behavior.

Issue #827: Codex can run hooks from a plugin cache while the MCP server runs
from a marketplace plugin path, and may lazy-start MCP on first tool call. A
startup pgrep miss is therefore ambiguous and must not produce the scary
"MCP server: OFFLINE" warning. Claude's reliable same-runtime process check is
preserved.
"""

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"


def _load_hook_module(monkeypatch, engram_home: Path):
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))

    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub_ct)

    sys.modules.pop("engram_session_start_hook", None)
    spec = importlib.util.spec_from_file_location(
        "engram_session_start_hook", _HOOK_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_runtime_platform(monkeypatch, mod, tmp_path: Path, platform: str | None):
    runtime_root = tmp_path / "plugin-runtime"
    hooks_dir = runtime_root / "hooks"
    hooks_dir.mkdir(parents=True)
    hook_path = hooks_dir / "engram-session-start-hook.py"
    hook_path.write_text("# test hook path\n")
    if platform is not None:
        (runtime_root / "platform.json").write_text(
            json.dumps({"platform": platform})
        )
    monkeypatch.setattr(mod, "__file__", str(hook_path))
    return runtime_root


def _run_hook_main(module, hook_input: dict) -> dict:
    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with patch("sys.stdout", buf):
            module.main()
    return json.loads(buf.getvalue().strip())


class _Completed:
    def __init__(self, returncode: int, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_codex_platform_defers_startup_mcp_probe(tmp_path, monkeypatch):
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)
    _set_runtime_platform(monkeypatch, mod, tmp_path, "codex")

    run = MagicMock(return_value=_Completed(1))
    monkeypatch.setattr(mod.subprocess, "run", run)

    assert mod._check_mcp_health(str(engram_home)) == (True, None)
    run.assert_not_called()


def test_codex_plugin_root_env_defers_when_platform_json_absent(tmp_path, monkeypatch):
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)
    _set_runtime_platform(monkeypatch, mod, tmp_path, None)
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(tmp_path / "codex-cache"))

    run = MagicMock(return_value=_Completed(1))
    monkeypatch.setattr(mod.subprocess, "run", run)

    assert mod._check_mcp_health(str(engram_home)) == (True, None)
    run.assert_not_called()


def test_claude_platform_preserves_offline_process_warning_probe(tmp_path, monkeypatch):
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)
    runtime_root = _set_runtime_platform(monkeypatch, mod, tmp_path, "claude-code")
    (runtime_root / "server.py").write_text("# test server\n")
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)

    run = MagicMock(return_value=_Completed(1))
    monkeypatch.setattr(mod.subprocess, "run", run)

    assert mod._check_mcp_health(str(engram_home)) == (
        False,
        "no engram server process found",
    )
    run.assert_called_once()
    assert str(runtime_root / "server.py") in run.call_args.args[0]


def test_claude_platform_wins_over_ambient_codex_plugin_root_env(
    tmp_path, monkeypatch
):
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)
    runtime_root = _set_runtime_platform(monkeypatch, mod, tmp_path, "claude-code")
    (runtime_root / "server.py").write_text("# test server\n")
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(tmp_path / "ambient-codex-root"))

    run = MagicMock(return_value=_Completed(1))
    monkeypatch.setattr(mod.subprocess, "run", run)

    assert mod._check_mcp_health(str(engram_home)) == (
        False,
        "no engram server process found",
    )
    run.assert_called_once()


def test_session_start_codex_does_not_emit_mcp_offline_false_alarm(
    tmp_path, monkeypatch
):
    engram_home = tmp_path / "engram"
    (engram_home / "sessions").mkdir(parents=True)
    mod = _load_hook_module(monkeypatch, engram_home)
    _set_runtime_platform(monkeypatch, mod, tmp_path, "codex")

    # If the old pgrep path were used, this would look offline. Codex should
    # skip the startup probe entirely and emit no substrate-health block.
    monkeypatch.setattr(mod.subprocess, "run", MagicMock(return_value=_Completed(1)))

    result = _run_hook_main(
        mod,
        {
            "session_id": "codex-session",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        },
    )

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "MCP server: OFFLINE" not in ctx
    assert "ENGRAM substrate health" not in ctx


def test_session_start_claude_still_emits_mcp_offline_when_probe_reliable(
    tmp_path, monkeypatch
):
    engram_home = tmp_path / "engram"
    (engram_home / "sessions").mkdir(parents=True)
    mod = _load_hook_module(monkeypatch, engram_home)
    runtime_root = _set_runtime_platform(monkeypatch, mod, tmp_path, "claude-code")
    (runtime_root / "server.py").write_text("# test server\n")
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(mod.subprocess, "run", MagicMock(return_value=_Completed(1)))

    result = _run_hook_main(
        mod,
        {
            "session_id": "claude-session",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        },
    )

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "ENGRAM substrate health" in ctx
    assert "MCP server: OFFLINE (no engram server process found)" in ctx


def test_marker_absent_returns_false(tmp_path, monkeypatch):
    """When server running but marker absent → write-tool liveness is False."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)
    _set_runtime_platform(monkeypatch, mod, tmp_path, "claude-code")
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)

    write_ok, write_reason = mod._check_mcp_write_tool_marker(str(engram_home))
    assert write_ok is False
    assert "absent" in write_reason


def test_marker_with_running_pid_returns_ok(tmp_path, monkeypatch):
    """When marker exists with a live PID → write-tool liveness is True."""
    import os as _os
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)
    _set_runtime_platform(monkeypatch, mod, tmp_path, "claude-code")
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
    # Write marker with the current process PID — guaranteed running.
    marker = {"pid": _os.getpid(), "registered_at": "2026-06-13T00:00:00Z"}
    (engram_home / "mcp-tools-ready.json").write_text(json.dumps(marker))

    write_ok, write_reason = mod._check_mcp_write_tool_marker(str(engram_home))
    assert write_ok is True
    assert write_reason is None


def test_marker_with_dead_pid_and_no_server_returns_false(tmp_path, monkeypatch):
    """When marker PID is dead AND no server.py is running → liveness is False."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)
    _set_runtime_platform(monkeypatch, mod, tmp_path, "claude-code")
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
    # Suppress the secondary pgrep check — no server running in test env.
    monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: False)
    # PID 9999999 almost certainly does not exist on any Linux system.
    marker = {"pid": 9999999, "registered_at": "2026-06-13T00:00:00Z"}
    (engram_home / "mcp-tools-ready.json").write_text(json.dumps(marker))

    write_ok, write_reason = mod._check_mcp_write_tool_marker(str(engram_home))
    assert write_ok is False
    assert "stale" in write_reason


def test_marker_with_dead_pid_but_server_alive_returns_true(tmp_path, monkeypatch):
    """Stale marker PID + live server.py (post-restart race) → liveness is True."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)
    _set_runtime_platform(monkeypatch, mod, tmp_path, "claude-code")
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
    # Simulate: dead marker PID, but a live server.py is running.
    monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: True)
    marker = {"pid": 9999999, "registered_at": "2026-06-13T00:00:00Z"}
    (engram_home / "mcp-tools-ready.json").write_text(json.dumps(marker))

    write_ok, write_reason = mod._check_mcp_write_tool_marker(str(engram_home))
    assert write_ok is True  # server alive, marker just stale from restart race
    assert write_reason is None


def test_codex_platform_defers_write_tool_marker_check(tmp_path, monkeypatch):
    """Codex platform defers the marker check (no probe on Codex)."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    mod = _load_hook_module(monkeypatch, engram_home)
    _set_runtime_platform(monkeypatch, mod, tmp_path, "codex")
    # No marker file — but Codex defers, so should return True.
    write_ok, write_reason = mod._check_mcp_write_tool_marker(str(engram_home))
    assert write_ok is True
    assert write_reason is None


def test_session_start_does_not_emit_write_tool_warning(
    tmp_path, monkeypatch
):
    """Anti-regression: write-tool liveness check is deferred to UserPromptSubmit.

    Even when the server is running but the marker is absent, the SessionStart
    hook must NOT emit the write-tool warning — the timing race (marker still
    has old PID when session fires) would produce a false alarm every restart.
    The check moved to the surface hook (UserPromptSubmit) where the race
    window is long past.
    """
    engram_home = tmp_path / "engram"
    (engram_home / "sessions").mkdir(parents=True)
    mod = _load_hook_module(monkeypatch, engram_home)
    runtime_root = _set_runtime_platform(monkeypatch, mod, tmp_path, "claude-code")
    (runtime_root / "server.py").write_text("# test server\n")
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
    # pgrep succeeds (server running) but no mcp-tools-ready.json written
    monkeypatch.setattr(mod.subprocess, "run", MagicMock(return_value=_Completed(0)))

    result = _run_hook_main(
        mod,
        {
            "session_id": "test-session",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        },
    )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    # Neither write-tool warning nor offline should appear when server is running
    assert "MCP write-tool liveness: UNCONFIRMED" not in ctx
    assert "MCP server: OFFLINE" not in ctx
