"""Tests for _EFFECTIVE_SESSION_ROLE plumbing in the SessionStart hook.

Verifies issue #1330: when SESSION_ROLE=="interactive" (env-var not set by
spawner) but write_marker() detects a fairy CWD and sets role="fairy" in the
marker, main() must update _EFFECTIVE_SESSION_ROLE to "fairy" so that banner
guards respect the CWD-derived role.

Verifies:
  1. _EFFECTIVE_SESSION_ROLE starts equal to SESSION_ROLE (env-var) at module load.
  2. After main() processes a marker with role="fairy", _EFFECTIVE_SESSION_ROLE
     is "fairy" even though SESSION_ROLE (env-var) was "interactive".
  3. When write_marker() returns None (missing session_id/transcript_path),
     _EFFECTIVE_SESSION_ROLE falls back to SESSION_ROLE.
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

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook_module(engram_home: Path, session_role: str = "interactive") -> object:
    """Load the session-start hook with ENGRAM_HOME and SESSION_ROLE set.

    Forces a fresh import each call. Stubs context_tracker so module-level
    import doesn't fail when there is no real baseline DB.
    """
    os.environ["ENGRAM_HOME"] = str(engram_home)
    os.environ["ENGRAM_SESSION_ROLE"] = session_role
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


def _mock_no_engram_client():
    """Patch sys.modules so engram_client import raises (calibration silently skipped)."""
    return patch.dict("sys.modules", {"engram_client": None})


# ---------------------------------------------------------------------------
# 1. Module-level initialization: _EFFECTIVE_SESSION_ROLE == SESSION_ROLE
# ---------------------------------------------------------------------------

def test_effective_role_initializes_to_session_role_interactive(tmp_path):
    """At module load, _EFFECTIVE_SESSION_ROLE equals SESSION_ROLE (interactive)."""
    engram_home = _make_engram_home(tmp_path)
    mod = _load_hook_module(engram_home, session_role="interactive")
    assert mod._EFFECTIVE_SESSION_ROLE == "interactive"
    assert mod._EFFECTIVE_SESSION_ROLE == mod.SESSION_ROLE


def test_effective_role_initializes_to_session_role_fairy(tmp_path):
    """At module load with ENGRAM_SESSION_ROLE=fairy, _EFFECTIVE_SESSION_ROLE is fairy."""
    engram_home = _make_engram_home(tmp_path)
    mod = _load_hook_module(engram_home, session_role="fairy")
    assert mod._EFFECTIVE_SESSION_ROLE == "fairy"
    assert mod._EFFECTIVE_SESSION_ROLE == mod.SESSION_ROLE


# ---------------------------------------------------------------------------
# 2. After main(): marker role="fairy" promotes _EFFECTIVE_SESSION_ROLE
# ---------------------------------------------------------------------------

def test_main_promotes_effective_role_when_marker_reports_fairy(tmp_path):
    """When SESSION_ROLE=='interactive' but write_marker() returns role='fairy',
    _EFFECTIVE_SESSION_ROLE is updated to 'fairy' after main() runs.

    This is the core fix for issue #1330: a fairy session launched via worktree
    CWD (without ENGRAM_SESSION_ROLE set) gets role='fairy' in its marker but
    previously still rendered all interactive-only banners.
    """
    engram_home = _make_engram_home(tmp_path)
    # Load module with SESSION_ROLE="interactive" (simulating no env var from spawner).
    mod = _load_hook_module(engram_home, session_role="interactive")
    assert mod.SESSION_ROLE == "interactive", "Pre-condition: env var not set by spawner"
    assert mod._EFFECTIVE_SESSION_ROLE == "interactive", "Pre-condition: starts as interactive"

    # A fairy marker — simulates what write_marker() produces when CWD detection
    # fires (FAIRY_CWD_RE matches worktree path even though SESSION_ROLE=="interactive").
    fairy_marker = {
        "session_id": "test-fairy-cwd-session",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
        "source": "startup",
        "role": "fairy",
        "purpose": "fairy",
    }

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            with patch.object(mod, "write_marker", return_value=fairy_marker):
                hook_input = {
                    "session_id": "test-fairy-cwd-session",
                    "transcript_path": str(tmp_path / "transcript.jsonl"),
                    "source": "startup",
                    # CWD matches worktree pattern — write_marker is mocked so
                    # the real CWD detection isn't exercised here.
                    "cwd": "/home/user/repo/.claude/worktrees/agent-abc123",
                }
                _run_hook_main(mod, hook_input)

    assert mod._EFFECTIVE_SESSION_ROLE == "fairy", (
        f"Expected _EFFECTIVE_SESSION_ROLE='fairy' after main() processes fairy marker, "
        f"got {mod._EFFECTIVE_SESSION_ROLE!r}. "
        f"SESSION_ROLE is still {mod.SESSION_ROLE!r} (unchanged, as designed)."
    )
    # SESSION_ROLE itself is NOT changed — only _EFFECTIVE_SESSION_ROLE is updated.
    assert mod.SESSION_ROLE == "interactive", (
        "SESSION_ROLE must remain 'interactive' — only _EFFECTIVE_SESSION_ROLE is updated."
    )


# ---------------------------------------------------------------------------
# 3. None marker → _EFFECTIVE_SESSION_ROLE falls back to SESSION_ROLE
# ---------------------------------------------------------------------------

def test_main_falls_back_to_session_role_when_marker_is_none(tmp_path):
    """When write_marker() returns None (missing session_id/transcript_path),
    _EFFECTIVE_SESSION_ROLE stays equal to SESSION_ROLE.
    """
    engram_home = _make_engram_home(tmp_path)
    mod = _load_hook_module(engram_home, session_role="interactive")

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            with patch.object(mod, "write_marker", return_value=None):
                # Missing session_id → write_marker returns None in real code too.
                hook_input = {
                    "source": "startup",
                }
                _run_hook_main(mod, hook_input)

    assert mod._EFFECTIVE_SESSION_ROLE == "interactive", (
        f"Expected _EFFECTIVE_SESSION_ROLE='interactive' (fallback to SESSION_ROLE) "
        f"when marker is None, got {mod._EFFECTIVE_SESSION_ROLE!r}."
    )
