"""Tests for the quiet-day guard on sleep_status_block() (#1995).

sleep_status_block() has a fourth silent path: when the marker is stale
(hours_since > SLEEP_DEBT_HOURS_THRESHOLD) but nodes_since is below
SLEEP_DEBT_MIN_NODES_THRESHOLD, the nudge is suppressed — a quiet day
(host was off, or genuinely idle) doesn't need a catch-up cycle. If the
nodes_since query itself fails (nodes_since stays None), the guard must
NOT suppress — an unknown count is not evidence of a quiet day, so the
existing "node count unavailable" banner still fires.
"""

import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Test helper (copied from tests/test_session_start_hook_calibration.py —
# a private underscore helper scoped to that file, not importable cross-file)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook_module(engram_home: Path):
    """Load the session-start hook with ENGRAM_HOME pointing at engram_home.

    Returns the module. Forces a fresh import each call to pick up env overrides.
    Forces a stub for context_tracker.write_baseline so the module-level import
    doesn't fail when there is no real baseline DB.
    """
    os.environ["ENGRAM_HOME"] = str(engram_home)
    # Stub context_tracker so the import at the top of the hook doesn't fail.
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


def _write_stale_marker(engram_home: Path, hours_ago: int = 40, turn_advanced_to: int = 100):
    """Write a last-sleep-success.json marker whose completed_at is
    `hours_ago` hours in the past (computed at call time, not hardcoded).

    Returns the completed_at ISO string so callers can insert node rows
    with created_at timestamps after it.
    """
    sessions_dir = engram_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    completed_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    completed_at_str = completed_at.isoformat()
    marker = {
        "completed_at": completed_at_str,
        "turn_advanced_to": turn_advanced_to,
    }
    with open(sessions_dir / "last-sleep-success.json", "w") as f:
        json.dump(marker, f)
    return completed_at_str


def _make_nodes_db(engram_home: Path, completed_at_str: str, n_rows_after: int):
    """Create knowledge.db with a `nodes` table and n_rows_after rows whose
    created_at is strictly after completed_at_str.

    Uses the real schema's NOT NULL columns (id, type, created_at) per
    src/engram/engram_core.py's CREATE TABLE nodes statement — other
    columns are nullable/defaulted so are omitted here.
    """
    db_path = engram_home / "knowledge.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    completed_at = datetime.fromisoformat(completed_at_str)
    for i in range(n_rows_after):
        created_at = (completed_at + timedelta(minutes=i + 1)).isoformat()
        conn.execute(
            "INSERT INTO nodes (id, type, created_at) VALUES (?, ?, ?)",
            (f"ob_{i:04d}", "observation", created_at),
        )
    conn.commit()
    conn.close()


def _clear_session_role_env():
    os.environ.pop("ENGRAM_SESSION_ROLE", None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSleepStatusBlockQuietDayGuard:
    def test_below_threshold_is_silent(self, tmp_path):
        """Stale marker (40h ago) + 3 new nodes (< 10) → suppressed entirely."""
        _clear_session_role_env()
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        completed_at_str = _write_stale_marker(engram_home, hours_ago=40)
        _make_nodes_db(engram_home, completed_at_str, n_rows_after=3)

        mod = _load_hook_module(engram_home)
        assert mod.sleep_status_block() == ""

    def test_at_or_above_threshold_nudges(self, tmp_path):
        """Stale marker (40h ago) + 15 new nodes (>= 10) → banner fires."""
        _clear_session_role_env()
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        completed_at_str = _write_stale_marker(engram_home, hours_ago=40)
        _make_nodes_db(engram_home, completed_at_str, n_rows_after=15)

        mod = _load_hook_module(engram_home)
        result = mod.sleep_status_block()
        assert result != ""
        assert "SLEEP STATUS" in result
        assert "15 nodes added since" in result

    def test_boundary_exactly_at_threshold_nudges(self, tmp_path):
        """Exactly SLEEP_DEBT_MIN_NODES_THRESHOLD (10) new nodes → still
        nudges, since the guard is strictly `< threshold`, not `<=`.
        Locks in the "less than 10" semantics, not "10 or fewer"."""
        _clear_session_role_env()
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        completed_at_str = _write_stale_marker(engram_home, hours_ago=40)
        _make_nodes_db(engram_home, completed_at_str, n_rows_after=10)

        mod = _load_hook_module(engram_home)
        assert mod.SLEEP_DEBT_MIN_NODES_THRESHOLD == 10
        result = mod.sleep_status_block()
        assert result != ""
        assert "10 nodes added since" in result

    def test_fail_open_on_query_error(self, tmp_path):
        """nodes_since query fails (no nodes table / no db) → nodes_since
        stays None → the quiet-day guard must NOT suppress; the existing
        "node count unavailable" banner still fires."""
        _clear_session_role_env()
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        (engram_home / "sessions").mkdir(parents=True, exist_ok=True)
        _write_stale_marker(engram_home, hours_ago=40)
        # No knowledge.db at all → sqlite3.connect succeeds (creates an
        # empty file) but the SELECT against a nonexistent `nodes` table
        # raises sqlite3.OperationalError (a subclass of sqlite3.Error),
        # which the hook's try/except swallows, leaving nodes_since=None.

        mod = _load_hook_module(engram_home)
        result = mod.sleep_status_block()
        assert result != ""
        assert "SLEEP STATUS" in result
        assert "node count unavailable" in result
