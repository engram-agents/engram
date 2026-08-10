"""Tests for the focus_block() surface helper wired into the SessionStart hook.

Verifies (issue #1732, moved from PostCompact per #1655/#1710): the live focus
list renders at every SessionStart via additionalContext, verbatim, so the
"deterministic focus channel" is an actual mechanism rather than a prose
convention the compacting model may skip. PostCompact was the original render
point (#1655/#1710) but PostCompact is a side-effects-only Claude Code hook
event that cannot inject additionalContext at all (confirmed against CC docs,
forum #241, 2026-07-09) — SessionStart fires for source=="compact" too, and is
the correct point; Lei's 2026-07-09 scope call additionally extended the
render to every session start, not just post-compact ones.

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
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SESSION_START_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"


# ---------------------------------------------------------------------------
# Module-load helper (mirrors tests/test_starred_block.py's pattern)
# ---------------------------------------------------------------------------

def _load_session_start_module(engram_home: Path):
    """Load the Claude session-start hook with ENGRAM_HOME set.

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

    module_name = "engram_session_start_hook_for_focus_block_test"
    for mod_name in list(sys.modules.keys()):
        if mod_name == module_name:
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(module_name, _SESSION_START_HOOK_PATH)
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
    """focus_block() renders the live focus list verbatim for SessionStart."""

    def test_nonexistent_db_returns_empty(self, engram_home):
        """Returns '' when knowledge.db doesn't exist (no raise)."""
        mod = _load_session_start_module(engram_home)
        result = mod.focus_block(str(engram_home))
        assert result == "", f"expected '' when db missing; got {result!r}"

    def test_empty_db_returns_empty(self, engram_home):
        """Returns '' when there are no focused-current rows."""
        _make_db(engram_home)
        mod = _load_session_start_module(engram_home)
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

        mod = _load_session_start_module(engram_home)
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
        mod = _load_session_start_module(engram_home)
        result = mod.focus_block(str(engram_home))
        assert "+3 more" in result, f"expected '+3 more' tail; got {result!r}"


# ---------------------------------------------------------------------------
# Layer 3 (#1975) — staleness gate on live pins
# ---------------------------------------------------------------------------

class TestFocusBlockStaleness:
    """Pins older than focus_staleness_days (config, default 7) get a
    trailing ⚠ marker + one summary line when >= 1 pin is stale."""

    def test_fresh_pin_unmarked(self, engram_home):
        db_path = _make_db(engram_home)
        now = datetime.now(timezone.utc)
        _insert_node(
            db_path, "ob_0001", "Fresh claim.",
            (now - timedelta(days=1)).isoformat(), None, 1,
        )
        mod = _load_session_start_module(engram_home)
        result = mod.focus_block(str(engram_home))
        assert "⚠" not in result, f"fresh pin must not be marked stale; got {result!r}"

    def test_pin_older_than_default_threshold_marked(self, engram_home):
        """Default threshold is 7 days; a 10-day-old pin is marked, with a
        one-pin summary line at the block's end."""
        db_path = _make_db(engram_home)
        now = datetime.now(timezone.utc)
        _insert_node(
            db_path, "ob_0001", "Stale claim.",
            (now - timedelta(days=10)).isoformat(), None, 1,
        )
        mod = _load_session_start_module(engram_home)
        result = mod.focus_block(str(engram_home))
        assert "⚠ pinned" in result, f"expected stale marker; got {result!r}"
        assert "rotate out or re-pin with a current reason" in result
        assert "⚠ 1 pin(s) stale" in result

    def test_pin_at_exactly_default_threshold_unmarked(self, engram_home):
        """Boundary: 'older than' N days excludes a pin exactly N days old."""
        db_path = _make_db(engram_home)
        now = datetime.now(timezone.utc)
        _insert_node(
            db_path, "ob_0001", "Exactly-at-threshold claim.",
            (now - timedelta(days=7)).isoformat(), None, 1,
        )
        mod = _load_session_start_module(engram_home)
        result = mod.focus_block(str(engram_home))
        assert "⚠" not in result, f"pin exactly at threshold must not be marked; got {result!r}"

    def test_threshold_configurable(self, engram_home):
        """config.json focus.focus_staleness_days overrides the default 7."""
        (engram_home / "config.json").write_text(
            json.dumps({"focus": {"focus_staleness_days": 2}})
        )
        db_path = _make_db(engram_home)
        now = datetime.now(timezone.utc)
        _insert_node(
            db_path, "ob_0001", "Aged 3 days.",
            (now - timedelta(days=3)).isoformat(), None, 1,
        )
        mod = _load_session_start_module(engram_home)
        result = mod.focus_block(str(engram_home))
        assert "⚠ pinned" in result, (
            f"3-day-old pin should be stale under a 2-day config threshold; got {result!r}"
        )

    def test_summary_line_counts_all_stale_pins(self, engram_home):
        db_path = _make_db(engram_home)
        now = datetime.now(timezone.utc)
        _insert_node(db_path, "ob_0001", "Stale one.", (now - timedelta(days=10)).isoformat(), None, 1)
        _insert_node(db_path, "ob_0002", "Stale two.", (now - timedelta(days=20)).isoformat(), None, 1)
        _insert_node(db_path, "ob_0003", "Fresh.", (now - timedelta(days=1)).isoformat(), None, 1)
        mod = _load_session_start_module(engram_home)
        result = mod.focus_block(str(engram_home))
        assert "⚠ 2 pin(s) stale" in result, f"expected 2-pin summary; got {result!r}"
