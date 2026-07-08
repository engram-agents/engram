"""Tests for the focus_block() surface helper wired into the PostCompact hook.

Verifies (issue #1655): the live focus list renders at PostCompact via
additionalContext, verbatim, so the "deterministic focus channel" is an
actual mechanism rather than a prose convention the compacting model may skip.

Covers:
  1. focus_block() returns "" when knowledge.db doesn't exist.
  2. focus_block() returns "" when there are no focused-current rows.
  3. focus_block() renders focused+current nodes verbatim (id + claim).
  4. focus_block() renders focus_reason in parens when present.
  5. focus_block() renders "- [id] claim" (no parens) when focus_reason is NULL.
  6. focus_block() excludes unfocused nodes (focused_at IS NULL).
  7. focus_block() excludes focused-but-not-current nodes (is_current = 0).
"""

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_POSTCOMPACT_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-postcompact-hook.py"


# ---------------------------------------------------------------------------
# Module-load helper (mirrors tests/test_starred_block.py's pattern)
# ---------------------------------------------------------------------------

def _load_postcompact_module(engram_home: Path):
    """Load the Claude postcompact hook with ENGRAM_HOME set.

    Stubs context_tracker so module-level imports don't fail. Forces a fresh
    import each call so env changes take effect.
    """
    os.environ["ENGRAM_HOME"] = str(engram_home)
    os.environ["INTER_AGENT_DIR"] = str(engram_home)  # unused by focus_block; kept for parity
    os.environ["ENGRAM_SESSION_ROLE"] = "interactive"

    original_context_tracker = sys.modules.get("context_tracker")
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    stub_ct.find_active_jsonl = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct

    module_name = "engram_postcompact_hook"
    for mod_name in list(sys.modules.keys()):
        if mod_name == module_name:
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(module_name, _POSTCOMPACT_HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        if original_context_tracker is None:
            sys.modules.pop("context_tracker", None)
        else:
            sys.modules["context_tracker"] = original_context_tracker
    return module


# ---------------------------------------------------------------------------
# DB fixture helpers
# ---------------------------------------------------------------------------

def _make_db(engram_home: Path) -> Path:
    """Create a minimal knowledge.db with a `nodes` table."""
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
    conn.commit()
    conn.close()
    return db_path


def _insert_node(db_path: Path, node_id: str, claim: str, focused_at, focus_reason, is_current: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO nodes (id, claim, focused_at, focus_reason, is_current) VALUES (?, ?, ?, ?, ?)",
        (node_id, claim, focused_at, focus_reason, is_current),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram"
    home.mkdir()
    return home


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFocusBlockHelper:
    """focus_block() renders the live focus list verbatim for PostCompact."""

    def test_nonexistent_db_returns_empty(self, engram_home):
        """Returns '' when knowledge.db doesn't exist (no raise)."""
        mod = _load_postcompact_module(engram_home)
        result = mod.focus_block(str(engram_home))
        assert result == "", f"expected '' when db missing; got {result!r}"

    def test_empty_db_returns_empty(self, engram_home):
        """Returns '' when there are no focused-current rows."""
        _make_db(engram_home)
        mod = _load_postcompact_module(engram_home)
        result = mod.focus_block(str(engram_home))
        assert result == "", f"expected '' for empty focus list; got {result!r}"

    def test_focused_current_nodes_render_verbatim(self, engram_home):
        """Both focused+current nodes appear verbatim (id + claim); reason
        rendered in parens when present, omitted when NULL."""
        db_path = _make_db(engram_home)
        _insert_node(
            db_path, "ob_0001", "The sky is blue on a clear day.",
            "2026-07-08T10:00:00Z", "continuity thread", 1,
        )
        _insert_node(
            db_path, "dv_0002", "Water boils at 100C at sea level.",
            "2026-07-08T10:01:00Z", None, 1,
        )
        # Unfocused current node — must NOT appear.
        _insert_node(
            db_path, "ob_0003", "Unfocused claim, should not appear.",
            None, None, 1,
        )
        # Focused but not-current node — must NOT appear.
        _insert_node(
            db_path, "ob_0004", "Stale focused claim, should not appear.",
            "2026-07-08T09:00:00Z", "old thread", 0,
        )

        mod = _load_postcompact_module(engram_home)
        result = mod.focus_block(str(engram_home))

        assert result != "", "expected non-empty output"

        # Focused-current nodes appear verbatim.
        assert "ob_0001" in result
        assert "The sky is blue on a clear day." in result
        assert "dv_0002" in result
        assert "Water boils at 100C at sea level." in result

        # Reason rendered in parens for the node that has one.
        assert "(continuity thread) The sky is blue on a clear day." in result
        # NULL-reason node renders with no parens.
        assert "- [dv_0002] Water boils at 100C at sea level." in result

        # Unfocused / not-current nodes excluded.
        assert "ob_0003" not in result
        assert "ob_0004" not in result
        assert "old thread" not in result

    def test_cap_enforced_at_15(self, engram_home):
        """More than FOCUS_LIST_CAP (15) rows renders a '+N more' tail."""
        db_path = _make_db(engram_home)
        for i in range(18):
            _insert_node(
                db_path, f"ob_{i:04d}", f"Claim number {i}.",
                f"2026-07-08T10:{i:02d}:00Z", None, 1,
            )
        mod = _load_postcompact_module(engram_home)
        result = mod.focus_block(str(engram_home))
        assert "+3 more" in result, f"expected '+3 more' tail; got {result!r}"
