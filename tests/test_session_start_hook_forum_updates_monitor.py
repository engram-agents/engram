"""Tests for forum_updates_monitor_block() injected by the SessionStart hook.

Interim stopgap (dual-arm): mirrors test_session_start_hook_forum_monitor.py's
structure against forum-updates-monitor.sh (real-time DM + baton-turn-flip
events over the cursor-based /api/updates feed), the sibling script to
forum-mention-monitor.sh. See engram-alpha issue #1661 for the deeper
unification tracked separately (not attempted here).

Verifies:
  1. Returns "" when SESSION_ROLE != "interactive".
  2. Returns "" when config.json has no forum.url key.
  3. Returns "" when forum.url is empty string.
  4. Returns "" on config read/parse error (silent failure).
  5. Returns "" when the forum-updates-monitor.sh script is absent.
  6. Returns instruction string when forum is configured + script present.
  7. Instruction contains "Monitor" call with the resolved script path.
  8. Instruction contains "persistent=true".
  9. Hook integration: block appears in startup additionalContext when configured.
 10. Hook integration: block absent from compact/resume source.
 11. Non-dict config shapes return "" without raising.
 12. Hook integration: BOTH monitor blocks appear together when both configured.
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"


def _load_hook_module(
    engram_home: Path,
    session_role: str = "interactive",
    runtime_dir: Path | None = None,
) -> object:
    """Load the session-start hook with ENGRAM_HOME and SESSION_ROLE set.

    Forces a fresh import each call. Stubs context_tracker so module-level
    import doesn't fail when there is no real baseline DB.

    runtime_dir, if provided, is set as ENGRAM_RUNTIME_DIR so that
    _resolve_runtime_dir() returns it — letting tests place the monitor
    script at a predictable path.
    """
    os.environ["ENGRAM_HOME"] = str(engram_home)
    os.environ["ENGRAM_SESSION_ROLE"] = session_role
    if runtime_dir is not None:
        os.environ["ENGRAM_RUNTIME_DIR"] = str(runtime_dir)
    else:
        os.environ.pop("ENGRAM_RUNTIME_DIR", None)

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


def _make_engram_home(tmp_path: Path) -> Path:
    """Create a minimal engram home directory with sessions/ subdir."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()
    return engram_home


def _write_config_with_forum(engram_home: Path, forum_cfg: dict | None) -> None:
    """Write a minimal config.json; include forum key only when forum_cfg is not None."""
    config: dict = {"schema_version": 2}
    if forum_cfg is not None:
        config["forum"] = forum_cfg
    (engram_home / "config.json").write_text(json.dumps(config))


def _make_updates_monitor_script(runtime_dir: Path) -> Path:
    """Create a dummy forum-updates-monitor.sh at the expected location."""
    tools_dir = runtime_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    script = tools_dir / "forum-updates-monitor.sh"
    script.write_text("#!/bin/bash\necho 'stub'\n")
    return script


def _make_mention_monitor_script(runtime_dir: Path) -> Path:
    """Create a dummy forum-mention-monitor.sh at the expected location."""
    tools_dir = runtime_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    script = tools_dir / "forum-mention-monitor.sh"
    script.write_text("#!/bin/bash\necho 'stub'\n")
    return script


def _mock_no_engram_client():
    return patch.dict("sys.modules", {"engram_client": None})


# ---------------------------------------------------------------------------
# 1. Non-interactive role → returns ""
# ---------------------------------------------------------------------------

def test_non_interactive_role_returns_empty(tmp_path):
    """forum_updates_monitor_block() returns "" when SESSION_ROLE is not interactive."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, {"url": "http://localhost:5002"})
    _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="heartbeat", runtime_dir=runtime_dir)
    result = mod.forum_updates_monitor_block()
    assert result == "", f"Expected '' for non-interactive role, got: {result!r}"


def test_sleep_role_returns_empty(tmp_path):
    """forum_updates_monitor_block() returns "" when SESSION_ROLE is 'sleep'."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, {"url": "http://localhost:5002"})
    _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="sleep", runtime_dir=runtime_dir)
    result = mod.forum_updates_monitor_block()
    assert result == ""


# ---------------------------------------------------------------------------
# 2. No forum key → returns ""
# ---------------------------------------------------------------------------

def test_no_forum_key_returns_empty(tmp_path):
    """Returns "" when config.json has no forum key at all."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, None)  # no forum key
    _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)
    result = mod.forum_updates_monitor_block()
    assert result == "", f"Expected '' when forum key absent, got: {result!r}"


# ---------------------------------------------------------------------------
# 3. Empty forum.url → returns ""
# ---------------------------------------------------------------------------

def test_empty_forum_url_returns_empty(tmp_path):
    """Returns "" when forum.url is an empty string."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, {"url": ""})
    _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)
    result = mod.forum_updates_monitor_block()
    assert result == "", f"Expected '' for empty forum.url, got: {result!r}"


# ---------------------------------------------------------------------------
# 4. Config read/parse errors → returns ""
# ---------------------------------------------------------------------------

def test_missing_config_returns_empty(tmp_path):
    """Returns "" silently when config.json does not exist."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _make_updates_monitor_script(runtime_dir)
    # No config.json written.

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)
    result = mod.forum_updates_monitor_block()
    assert result == ""


def test_malformed_config_returns_empty(tmp_path):
    """Returns "" silently when config.json is not valid JSON."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _make_updates_monitor_script(runtime_dir)
    (engram_home / "config.json").write_text("{ bad json }")

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)
    result = mod.forum_updates_monitor_block()
    assert result == ""


def test_non_dict_config_shapes_return_empty(tmp_path):
    """Valid-JSON-but-non-dict shapes must return "" without raising."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)
    for raw in ["null", "[1, 2, 3]", '"a string"', "42", '{"forum": null}']:
        (engram_home / "config.json").write_text(raw)
        result = mod.forum_updates_monitor_block()
        assert result == "", f"non-dict config {raw!r} should return '' (got {result!r})"


# ---------------------------------------------------------------------------
# 5. Script absent → returns ""
# ---------------------------------------------------------------------------

def test_script_absent_returns_empty(tmp_path):
    """Returns "" when forum-updates-monitor.sh does not exist at the resolved path."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, {"url": "http://localhost:5002"})
    # Do NOT create the script file.

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)
    result = mod.forum_updates_monitor_block()
    assert result == "", f"Expected '' when script absent, got: {result!r}"


# ---------------------------------------------------------------------------
# 6. Configured + script present → returns instruction string
# ---------------------------------------------------------------------------

def test_configured_returns_nonempty(tmp_path):
    """Returns a non-empty string when forum is configured and script is present."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, {"url": "http://localhost:5002"})
    _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)
    result = mod.forum_updates_monitor_block()
    assert result != "", "Expected non-empty string when forum configured + script present"


# ---------------------------------------------------------------------------
# 7. Instruction contains "Monitor" call with the resolved script path
# ---------------------------------------------------------------------------

def test_instruction_contains_monitor_call(tmp_path):
    """Instruction string mentions Monitor and the script path."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, {"url": "http://localhost:5002"})
    script = _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)
    result = mod.forum_updates_monitor_block()

    assert "Monitor" in result, f"Expected 'Monitor' in instruction: {result!r}"
    assert str(script) in result, f"Expected script path '{script}' in instruction: {result!r}"


# ---------------------------------------------------------------------------
# 8. Instruction contains "persistent=true"
# ---------------------------------------------------------------------------

def test_instruction_contains_persistent_true(tmp_path):
    """Instruction includes persistent=true for the Monitor call."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, {"url": "http://localhost:5002"})
    _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)
    result = mod.forum_updates_monitor_block()
    assert "persistent=true" in result, f"Expected 'persistent=true' in instruction: {result!r}"


# ---------------------------------------------------------------------------
# 9. Hook integration: block appears in startup additionalContext
# ---------------------------------------------------------------------------

def test_integration_updates_monitor_block_in_startup_context(tmp_path):
    """When configured+interactive+startup, updates monitor block appears in additionalContext."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, {"url": "http://localhost:5002"})
    _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            hook_input = {
                "session_id": "test-forum-updates-monitor-startup",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            }
            result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Forum updates monitor" in ctx, (
        f"Expected 'Forum updates monitor' in additionalContext: {ctx!r}"
    )
    assert "Monitor" in ctx


# ---------------------------------------------------------------------------
# 10. Hook integration: block absent from compact/resume source
# ---------------------------------------------------------------------------

def test_integration_updates_monitor_block_absent_on_compact(tmp_path):
    """Updates monitor block is NOT injected for source=compact (only source=startup)."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, {"url": "http://localhost:5002"})
    _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            hook_input = {
                "session_id": "test-forum-updates-monitor-compact",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "compact",
            }
            result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Forum updates monitor" not in ctx, (
        f"Updates monitor block should NOT appear for source=compact, got: {ctx!r}"
    )


def test_integration_updates_monitor_block_absent_on_resume(tmp_path):
    """Updates monitor block is NOT injected for source=resume (only source=startup)."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, {"url": "http://localhost:5002"})
    _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            hook_input = {
                "session_id": "test-forum-updates-monitor-resume",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "resume",
            }
            result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Forum updates monitor" not in ctx, (
        f"Updates monitor block should NOT appear for source=resume, got: {ctx!r}"
    )


def test_integration_updates_monitor_block_absent_when_unconfigured(tmp_path):
    """Updates monitor block absent from startup context when forum is not configured."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, None)  # no forum key
    _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            hook_input = {
                "session_id": "test-forum-updates-monitor-no-config",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            }
            result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Forum updates monitor" not in ctx


# ---------------------------------------------------------------------------
# 12. Hook integration: BOTH monitor blocks appear together (dual-arm)
# ---------------------------------------------------------------------------

def test_integration_both_monitor_blocks_appear_together(tmp_path):
    """When both scripts are configured+present, both blocks appear in the SAME
    additionalContext (the actual dual-arm behavior requested)."""
    engram_home = _make_engram_home(tmp_path)
    runtime_dir = tmp_path / "runtime"
    _write_config_with_forum(engram_home, {"url": "http://localhost:5002"})
    _make_mention_monitor_script(runtime_dir)
    _make_updates_monitor_script(runtime_dir)

    mod = _load_hook_module(engram_home, session_role="interactive", runtime_dir=runtime_dir)

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            hook_input = {
                "session_id": "test-dual-arm-both-monitors",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            }
            result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Forum mention monitor" in ctx, (
        f"Expected 'Forum mention monitor' in additionalContext: {ctx!r}"
    )
    assert "Forum updates monitor" in ctx, (
        f"Expected 'Forum updates monitor' in additionalContext: {ctx!r}"
    )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
