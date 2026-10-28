"""Tests for saved_focus_sets_block() — #1975 layer 1: session-start listing
of saved focus_sets (excluding the active one), directly after the live
📌 focus block.

Coverage (per issue #1975's spec test list, Layer 1):
  1. Active set excluded from the listing.
  2. Empty focus_sets table renders nothing.
  3. Missing knowledge.db renders nothing (never-fail discipline).
  4. Compact single-line rendering when <= 3 non-active sets.
  5. One-line-per-set rendering (with description) when > 3 non-active sets.
"""

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SESSION_START_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_session_start_module(engram_home: Path):
    """Load the Claude session-start hook with ENGRAM_HOME set.

    Mirrors tests/test_focus_block.py's module-load helper.
    """
    os.environ["ENGRAM_HOME"] = str(engram_home)
    os.environ["INTER_AGENT_DIR"] = str(engram_home)
    os.environ["ENGRAM_SESSION_ROLE"] = "interactive"

    original_context_tracker = sys.modules.get("context_tracker")
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    stub_ct.find_active_jsonl = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct

    module_name = "engram_session_start_hook_for_saved_focus_sets_test"
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


def _make_db(engram_home: Path) -> Path:
    """Create a minimal knowledge.db with focus_sets + focus_state tables."""
    db_path = engram_home / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE focus_sets (
            name            TEXT PRIMARY KEY,
            node_ids        TEXT NOT NULL,
            description     TEXT,
            created_at      TEXT NOT NULL,
            last_loaded_at  TEXT,
            load_count      INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE focus_state (
            singleton_key    INTEGER PRIMARY KEY CHECK (singleton_key = 1),
            active_set_name  TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_set(db_path: Path, name: str, node_ids: list, description: str = "",
                 created_at: str = "2026-08-10T10:00:00+00:00") -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO focus_sets (name, node_ids, description, created_at, load_count) "
        "VALUES (?, ?, ?, ?, 0)",
        (name, json.dumps(node_ids), description, created_at),
    )
    conn.commit()
    conn.close()


def _set_active(db_path: Path, name) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM focus_state")
    conn.execute("INSERT INTO focus_state (singleton_key, active_set_name) VALUES (1, ?)", (name,))
    conn.commit()
    conn.close()


@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram"
    home.mkdir()
    return home


class TestSavedFocusSetsBlock:
    def test_nonexistent_db_returns_empty(self, engram_home):
        mod = _load_session_start_module(engram_home)
        result = mod.saved_focus_sets_block(str(engram_home))
        assert result == "", f"expected '' when db missing; got {result!r}"

    def test_empty_table_returns_empty(self, engram_home):
        _make_db(engram_home)
        mod = _load_session_start_module(engram_home)
        result = mod.saved_focus_sets_block(str(engram_home))
        assert result == "", f"expected '' for empty focus_sets table; got {result!r}"

    def test_active_set_excluded(self, engram_home):
        db_path = _make_db(engram_home)
        _insert_set(db_path, "arc-agi3-game-hall", ["ob_0001"])
        _insert_set(db_path, "baton-migration", ["ob_0002"])
        _set_active(db_path, "arc-agi3-game-hall")

        mod = _load_session_start_module(engram_home)
        result = mod.saved_focus_sets_block(str(engram_home))

        assert "baton-migration" in result
        assert "arc-agi3-game-hall" not in result

    def test_compact_single_line_for_le_3_sets(self, engram_home):
        db_path = _make_db(engram_home)
        _insert_set(db_path, "set-a", ["ob_0001"])
        _insert_set(db_path, "set-b", ["ob_0002", "ob_0003"])

        mod = _load_session_start_module(engram_home)
        result = mod.saved_focus_sets_block(str(engram_home))

        assert result.startswith("💼 Saved focus-sets: ")
        assert "\n" not in result
        assert "set-a (1 node, saved 2026-08-10)" in result
        assert "set-b (2 nodes, saved 2026-08-10)" in result
        assert " · " in result

    def test_one_line_per_set_when_gt_3_sets(self, engram_home):
        db_path = _make_db(engram_home)
        for i in range(4):
            _insert_set(db_path, f"set-{i}", ["ob_0001"], description=f"desc {i}")

        mod = _load_session_start_module(engram_home)
        result = mod.saved_focus_sets_block(str(engram_home))

        assert result.startswith("💼 Saved focus-sets:\n")
        for i in range(4):
            assert f"set-{i} (1 node, saved 2026-08-10) — desc {i}" in result

    def test_exception_renders_nothing(self, engram_home, monkeypatch):
        """Never-fail discipline: any read error -> ''."""
        db_path = _make_db(engram_home)
        _insert_set(db_path, "set-a", ["ob_0001"])
        mod = _load_session_start_module(engram_home)

        def _boom(*a, **kw):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(mod.sqlite3, "connect", _boom)
        result = mod.saved_focus_sets_block(str(engram_home))
        assert result == "", f"expected '' on exception; got {result!r}"
