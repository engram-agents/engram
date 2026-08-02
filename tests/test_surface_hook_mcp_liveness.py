"""Tests for the MCP write-tool liveness check in engram-surface-hook.py.

The check moved from SessionStart to UserPromptSubmit to eliminate the startup
timing race: the old server PID stays in mcp-tools-ready.json until the new
server finishes initializing (~8s), so checking at SessionStart produces a
false alarm on every restart. By first user prompt the race window is past.
"""

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"


def _load_hook_module(monkeypatch=None, engram_home: Path | None = None):
    """Import the surface hook as a module despite the hyphenated filename."""
    if engram_home and monkeypatch:
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    sys.modules.pop("engram_surface_hook", None)
    spec = importlib.util.spec_from_file_location("engram_surface_hook", str(_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCheckMcpWriteToolMarker:
    """Unit tests for _check_mcp_write_tool_marker in the surface hook."""

    def test_marker_absent_returns_false(self, tmp_path):
        """No marker file → False with 'absent' reason."""
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_hook_module(engram_home=engram_home)
        ok, reason = mod._check_mcp_write_tool_marker(str(engram_home))
        assert ok is False
        assert "absent" in reason

    def test_marker_with_running_pid_returns_ok(self, tmp_path):
        """Marker with a live PID → True."""
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_hook_module(engram_home=engram_home)
        marker = {"pid": os.getpid(), "registered_at": "2026-06-14T00:00:00Z"}
        (engram_home / "mcp-tools-ready.json").write_text(json.dumps(marker))
        ok, reason = mod._check_mcp_write_tool_marker(str(engram_home))
        assert ok is True
        assert reason is None

    def test_marker_with_no_pid_field_returns_false(self, tmp_path):
        """Marker with no/invalid pid field → False."""
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_hook_module(engram_home=engram_home)
        marker = {"registered_at": "2026-06-14T00:00:00Z"}
        (engram_home / "mcp-tools-ready.json").write_text(json.dumps(marker))
        ok, reason = mod._check_mcp_write_tool_marker(str(engram_home))
        assert ok is False
        assert "pid" in reason

    def test_exception_returns_ok(self, tmp_path):
        """Any unexpected error → True (advisory, never block)."""
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_hook_module(engram_home=engram_home)
        # Point at a path that will error on open (directory, not file)
        (engram_home / "mcp-tools-ready.json").mkdir()
        ok, reason = mod._check_mcp_write_tool_marker(str(engram_home))
        assert ok is True

    def test_marker_with_dead_pid_and_no_server_returns_false(self, tmp_path, monkeypatch):
        """When marker PID is dead AND no server.py is running → liveness is False."""
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home=engram_home)
        # Suppress the secondary pgrep check — no server running in test env.
        monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: False)
        # PID 9999999 almost certainly does not exist on any system.
        marker = {"pid": 9999999, "registered_at": "2026-06-14T00:00:00Z"}
        (engram_home / "mcp-tools-ready.json").write_text(json.dumps(marker))

        ok, reason = mod._check_mcp_write_tool_marker(str(engram_home))
        assert ok is False
        assert "stale" in reason

    def test_marker_with_dead_pid_but_server_alive_returns_true(self, tmp_path, monkeypatch):
        """Stale marker PID + live server.py (post-restart race) → liveness is True."""
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home=engram_home)
        # Simulate: dead marker PID, but a live server.py is running.
        monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: True)
        marker = {"pid": 9999999, "registered_at": "2026-06-14T00:00:00Z"}
        (engram_home / "mcp-tools-ready.json").write_text(json.dumps(marker))

        ok, reason = mod._check_mcp_write_tool_marker(str(engram_home))
        assert ok is True  # server alive, marker just stale from restart race
        # Not silent: informational note, per PR #1839's colleague
        # review -- a suppressed condition must still surface somewhere.
        assert reason is not None
        assert "stale" in reason


class TestMcpLivenessIntegration:
    """Integration test: main() wires the write-tool warning into hook output."""

    def test_main_emits_write_tool_warning_on_stale_marker(self, tmp_path, monkeypatch):
        """main() must surface UNCONFIRMED in additionalContext when marker PID is dead.

        This is the wiring test Ariadne flagged: unit tests cover the helper
        directly, but the end-to-end path through main() was only verified by hand.
        """
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home=engram_home)

        # Stale marker — PID 9999999 is almost certainly not running.
        marker = {"pid": 9999999, "registered_at": "2026-06-14T00:00:00Z"}
        (engram_home / "mcp-tools-ready.json").write_text(json.dumps(marker))
        # Suppress the secondary pgrep check — this test asserts the pure
        # stale-marker-and-no-server path, independent of whether a real
        # engram server.py happens to be running on the machine under test.
        monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: False)

        # Stub DB/network/filesystem dependencies to keep the test hermetic.
        # query_daemon returns a valid (empty) result so used_daemon=True and
        # the daemon-offline warning is suppressed, leaving only the MCP warning.
        monkeypatch.setattr(
            mod, "query_daemon",
            lambda *a, **kw: {"matched_ids": [], "match_count": 0, "nodes": []},
        )
        monkeypatch.setattr(mod, "_get_auto_surface_config",
                            lambda: {"idf_gate_enabled": False,
                                     "short_prompt_threshold_chars": 0,
                                     "prev_response_tail_chars": 0})
        # #1698 slice 3: main() now calls check_principle_triggers_full
        # (returns (text, rendered)) rather than check_principle_triggers
        # directly — stub the function main() actually invokes.
        monkeypatch.setattr(mod, "check_principle_triggers_full", lambda *a, **kw: ("", []))
        monkeypatch.setattr(mod, "check_error_patterns", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "_query_attached_packs", lambda *a, **kw: [])
        monkeypatch.setattr(mod, "format_nudge", lambda *a, **kw: "")
        monkeypatch.setattr(mod, "check_write_reminder", lambda: "")
        monkeypatch.setattr(mod, "check_repair_marker", lambda: "")
        monkeypatch.setattr(mod, "check_feeling_nudge_marker", lambda: "")
        monkeypatch.setattr(mod, "check_warm_briefing", lambda count: "")
        monkeypatch.setattr(mod, "format_nap_warning", lambda count: "")

        stdin_data = json.dumps({"prompt": "test mcp liveness wiring"})
        buf = io.StringIO()
        with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
            with patch("sys.stdout", buf):
                try:
                    mod.main()
                except SystemExit:
                    pass

        output = json.loads(buf.getvalue().strip())
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "MCP write-tool liveness: UNCONFIRMED" in ctx
        assert "stale" in ctx

    def test_main_emits_informational_note_on_stale_marker_offset_by_live_server(
        self, tmp_path, monkeypatch
    ):
        """main() surfaces an INFORMATIONAL (not alarming) note when a dead
        marker PID is offset by a live server.py -- per PR #1839's
        colleague review, this healthy-but-suppressed condition must not go
        completely silent, or a genuine duplicate-launch incident goes
        unnoticed the way #1772 almost did.
        """
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home=engram_home)

        marker = {"pid": 9999999, "registered_at": "2026-06-14T00:00:00Z"}
        (engram_home / "mcp-tools-ready.json").write_text(json.dumps(marker))
        # A live server.py IS found this time -- the offset path.
        monkeypatch.setattr(mod, "_is_any_server_py_running", lambda: True)

        monkeypatch.setattr(
            mod, "query_daemon",
            lambda *a, **kw: {"matched_ids": [], "match_count": 0, "nodes": []},
        )
        monkeypatch.setattr(mod, "_get_auto_surface_config",
                            lambda: {"idf_gate_enabled": False,
                                     "short_prompt_threshold_chars": 0,
                                     "prev_response_tail_chars": 0})
        monkeypatch.setattr(mod, "check_principle_triggers_full", lambda *a, **kw: ("", []))
        monkeypatch.setattr(mod, "check_error_patterns", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "_query_attached_packs", lambda *a, **kw: [])
        monkeypatch.setattr(mod, "format_nudge", lambda *a, **kw: "")
        monkeypatch.setattr(mod, "check_write_reminder", lambda: "")
        monkeypatch.setattr(mod, "check_repair_marker", lambda: "")
        monkeypatch.setattr(mod, "check_feeling_nudge_marker", lambda: "")
        monkeypatch.setattr(mod, "check_warm_briefing", lambda count: "")
        monkeypatch.setattr(mod, "format_nap_warning", lambda count: "")

        stdin_data = json.dumps({"prompt": "test mcp liveness informational wiring"})
        buf = io.StringIO()
        with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
            with patch("sys.stdout", buf):
                try:
                    mod.main()
                except SystemExit:
                    pass

        output = json.loads(buf.getvalue().strip())
        ctx = output["hookSpecificOutput"]["additionalContext"]
        # Healthy, not the alarming UNCONFIRMED path...
        assert "MCP write-tool liveness: UNCONFIRMED" not in ctx
        # ...but not silent either.
        assert "MCP write-tool liveness OK" in ctx
        assert "stale" in ctx
