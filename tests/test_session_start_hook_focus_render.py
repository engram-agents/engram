"""Hook-integration tests: the focus list renders in SessionStart's
additionalContext for EVERY source, not just source=="compact" (issue #1732).

Complements tests/test_focus_block.py (unit tests on the focus_block() helper
itself) by exercising main() end-to-end with a real knowledge.db, mirroring
the pattern in tests/test_session_start_hook_calibration.py. The done-criterion
per #1732 (Kepler, ls_0058-class discipline) is a focus list actually rendering
in a REAL post-compact session start — these tests are the mechanical gate for
that; the live verification still has to happen against the real substrate.
"""

import importlib.util
import io
import json
import os
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"


def _load_hook_module(engram_home: Path):
    """Load the session-start hook with ENGRAM_HOME pointing at engram_home."""
    os.environ["ENGRAM_HOME"] = str(engram_home)
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct

    for mod_name in list(sys.modules.keys()):
        if mod_name in ("engram-session-start-hook", "engram_session_start_hook"):
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location("engram_session_start_hook", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_focused_db(engram_home: Path) -> None:
    db_path = engram_home / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            claim TEXT,
            focused_at TEXT,
            focus_reason TEXT,
            is_current INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO nodes (id, claim, focused_at, focus_reason, is_current) VALUES (?, ?, ?, ?, ?)",
        ("ob_9001", "Focus-render live-verification claim.", "2026-07-09T10:00:00Z", "test thread", 1),
    )
    conn.commit()
    conn.close()


def _run_hook_main(module, hook_input: dict) -> dict:
    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with redirect_stdout(buf):
            module.main()
    return json.loads(buf.getvalue().strip())


@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram"
    home.mkdir()
    (home / "sessions").mkdir()
    return home


def _base_hook_input(tmp_path: Path, session_id: str, source: str) -> dict:
    return {
        "session_id": session_id,
        "transcript_path": str(tmp_path / "transcript.jsonl"),
        "source": source,
    }


class TestFocusRendersOnEverySessionStart:
    """#1732: focus_block() output appears in additionalContext regardless
    of source — startup, compact, and resume all render it."""

    @pytest.mark.parametrize("source", ["startup", "compact", "resume"])
    def test_focus_block_present_for_source(self, tmp_path, engram_home, source):
        _make_focused_db(engram_home)
        mod = _load_hook_module(engram_home)

        with patch.dict("sys.modules", {"engram_client": None}):
            result = _run_hook_main(
                mod, _base_hook_input(tmp_path, f"test-session-{source}", source)
            )

        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Focused nodes" in ctx, (
            f"source={source}: expected focus block in additionalContext, got: {ctx!r}"
        )
        assert "ob_9001" in ctx
        assert "Focus-render live-verification claim." in ctx
        assert "(test thread)" in ctx

    def test_focus_block_absent_when_nothing_focused(self, tmp_path, engram_home):
        """No focused rows -> no focus block, but the hook still succeeds."""
        db_path = engram_home / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT, focused_at TEXT, "
            "focus_reason TEXT, is_current INTEGER)"
        )
        conn.commit()
        conn.close()

        mod = _load_hook_module(engram_home)
        with patch.dict("sys.modules", {"engram_client": None}):
            result = _run_hook_main(
                mod, _base_hook_input(tmp_path, "test-session-empty-focus", "startup")
            )

        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Focused nodes" not in ctx
        assert "hookSpecificOutput" in result

    def test_focus_block_absent_gracefully_when_db_missing(self, tmp_path, engram_home):
        """No knowledge.db at all -> hook still completes cleanly, no focus block."""
        mod = _load_hook_module(engram_home)
        with patch.dict("sys.modules", {"engram_client": None}):
            result = _run_hook_main(
                mod, _base_hook_input(tmp_path, "test-session-no-db", "compact")
            )

        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Focused nodes" not in ctx
        assert "hookSpecificOutput" in result
