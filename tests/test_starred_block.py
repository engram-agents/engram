"""Tests for the starred_block() surface helper wired into the Claude hooks.

Verifies:
  1. starred_block() returns "" when no starred JSON file exists.
  2. starred_block() returns "" when the starred list is empty.
  3. starred_block() renders one-line pointer with expected format:
       ⭐ [<from>] "<title>" — ia read <filename>
  4. starred_block() includes note when present.
  5. starred_block() silently skips entries whose letter file no longer exists.
  6. starred_block() caps at 10 entries and appends "+N more" tail.
  7. starred_block() returns "" on corrupt starred JSON (graceful).

Tests cover the Claude session-start hook and postcompact hook implementations
(both contain the same starred_block function with the same contract).
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SESSION_START_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
_POSTCOMPACT_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-postcompact-hook.py"


# ---------------------------------------------------------------------------
# Module-load helpers
# ---------------------------------------------------------------------------

def _load_hook_module(hook_path: Path, module_name: str, engram_home: Path, inter_agent_dir: Path):
    """Load a hook module with ENGRAM_HOME and INTER_AGENT_DIR set.

    Stubs context_tracker so module-level imports don't fail.
    Forces a fresh import each call so env changes take effect.
    """
    os.environ["ENGRAM_HOME"] = str(engram_home)
    os.environ["INTER_AGENT_DIR"] = str(inter_agent_dir)
    os.environ["ENGRAM_SESSION_ROLE"] = "interactive"

    # Stub context_tracker so module-level import doesn't fail. Restore the
    # caller's sys.modules entry after import so this test helper doesn't leak a
    # MagicMock into later tests that import context_tracker at runtime.
    original_context_tracker = sys.modules.get("context_tracker")
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    stub_ct.find_active_jsonl = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct

    # Force fresh import
    for mod_name in list(sys.modules.keys()):
        if mod_name == module_name:
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(module_name, hook_path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        if original_context_tracker is None:
            sys.modules.pop("context_tracker", None)
        else:
            sys.modules["context_tracker"] = original_context_tracker
    return module


def _load_session_start_module(engram_home: Path, inter_agent_dir: Path):
    """Load the Claude session-start hook."""
    return _load_hook_module(
        _SESSION_START_HOOK_PATH, "engram_session_start_hook", engram_home, inter_agent_dir
    )


def _load_postcompact_module(engram_home: Path, inter_agent_dir: Path):
    """Load the Claude postcompact hook."""
    return _load_hook_module(
        _POSTCOMPACT_HOOK_PATH, "engram_postcompact_hook", engram_home, inter_agent_dir
    )


# ---------------------------------------------------------------------------
# Letter writer helper
# ---------------------------------------------------------------------------

def _ts(offset_minutes: int = -5) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)


def _write_letter(
    inter_agent_dir: Path,
    filename: str,
    from_agent: str,
    to_agent: str,
    timestamp: datetime,
    heading: str = "Test letter",
    body: str = "Letter body.",
) -> None:
    ts_str = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    content = (
        f"---\nfrom: {from_agent}\nto: {to_agent}\ntimestamp: {ts_str}\n---\n\n"
        f"# {heading}\n\n{body}\n"
    )
    (inter_agent_dir / filename).write_text(content)


def _write_starred(engram_home: Path, entries: list) -> None:
    (engram_home / "inter-agent-starred.json").write_text(json.dumps(entries))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram"
    home.mkdir()
    return home


@pytest.fixture
def inter_agent_dir(tmp_path: Path) -> Path:
    d = tmp_path / "inter-agent"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Parametrize over both hook implementations
# ---------------------------------------------------------------------------

def _get_starred_block_fns(engram_home, inter_agent_dir):
    """Return the starred_block functions from the Claude hook implementations."""
    ss_mod = _load_session_start_module(engram_home, inter_agent_dir)
    pc_mod = _load_postcompact_module(engram_home, inter_agent_dir)
    return [
        ("claude-session-start", ss_mod.starred_block),
        ("claude-postcompact", pc_mod.starred_block),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStarredBlockHelper:
    """starred_block() in both hooks renders the same contract."""

    def test_no_starred_file_returns_empty(self, engram_home, inter_agent_dir):
        """Returns '' when no inter-agent-starred.json file exists."""
        for hook_name, fn in _get_starred_block_fns(engram_home, inter_agent_dir):
            result = fn(str(engram_home))
            assert result == "", f"{hook_name}: expected '' when no starred file; got {result!r}"

    def test_empty_starred_list_returns_empty(self, engram_home, inter_agent_dir):
        """Returns '' when the starred list is an empty array."""
        _write_starred(engram_home, [])
        for hook_name, fn in _get_starred_block_fns(engram_home, inter_agent_dir):
            result = fn(str(engram_home))
            assert result == "", f"{hook_name}: expected '' for empty list; got {result!r}"

    def test_pointer_format(self, engram_home, inter_agent_dir):
        """Renders one-line pointer using snapshot from/title (no re-parse of letter)."""
        filename = "2026-05-30T10-00-00Z_borges.md"
        _write_letter(inter_agent_dir, filename, "borges", "ariadne", _ts(-60), "Key Agreement")
        # Snapshot fields stored at star-time
        _write_starred(engram_home, [{
            "filename": filename,
            "from": "borges",
            "title": "Key Agreement",
            "starred_at": "2026-05-30T10:00:00Z",
            "note": "",
        }])

        for hook_name, fn in _get_starred_block_fns(engram_home, inter_agent_dir):
            result = fn(str(engram_home))
            assert "1 starred letter" in result, f"{hook_name}: expected count in header"
            assert "borges" in result, f"{hook_name}: expected from-agent in output"
            assert "Key Agreement" in result, f"{hook_name}: expected title in output"
            assert f"ia read {filename}" in result, f"{hook_name}: expected ia read command"

    def test_note_appears_in_output(self, engram_home, inter_agent_dir):
        """Renders the note when present."""
        filename = "2026-05-30T10-00-00Z_borges.md"
        _write_letter(inter_agent_dir, filename, "borges", "ariadne", _ts(-60), "Agreement")
        _write_starred(engram_home, [{"filename": filename, "starred_at": "2026-05-30T10:00:00Z", "note": "work split"}])

        for hook_name, fn in _get_starred_block_fns(engram_home, inter_agent_dir):
            result = fn(str(engram_home))
            assert "work split" in result, f"{hook_name}: expected note in output"

    def test_missing_letter_renders_from_snapshot(self, engram_home, inter_agent_dir):
        """Renders from snapshot fields even when the source letter was deleted."""
        _write_starred(engram_home, [{
            "filename": "missing.md",
            "from": "borges",
            "title": "A deleted letter",
            "starred_at": "2026-05-01T10:00:00Z",
            "note": "",
        }])

        for hook_name, fn in _get_starred_block_fns(engram_home, inter_agent_dir):
            result = fn(str(engram_home))
            assert "borges" in result, f"{hook_name}: expected snapshot from in output"
            assert "A deleted letter" in result, f"{hook_name}: expected snapshot title in output"
            assert "ia read missing.md" in result, f"{hook_name}: expected filename in output"

    def test_cap_and_tail(self, engram_home, inter_agent_dir):
        """Caps at 10 entries and adds '+N more' tail when >10 entries exist."""
        entries = []
        for i in range(12):
            filename = f"2026-05-30T{i:02d}-00-00Z_borges.md"
            _write_letter(inter_agent_dir, filename, "borges", "ariadne", _ts(-(60 + i)), f"Letter {i}")
            entries.append({"filename": filename, "starred_at": "2026-05-30T10:00:00Z", "note": ""})
        _write_starred(engram_home, entries)

        for hook_name, fn in _get_starred_block_fns(engram_home, inter_agent_dir):
            result = fn(str(engram_home))
            assert "12 starred" in result, f"{hook_name}: expected total count in header"
            assert "+2 more" in result, f"{hook_name}: expected +2 more tail"

    def test_corrupt_starred_json_returns_empty(self, engram_home, inter_agent_dir):
        """Returns '' on corrupt JSON in the starred file (graceful)."""
        (engram_home / "inter-agent-starred.json").write_text("{not valid json!!!")

        for hook_name, fn in _get_starred_block_fns(engram_home, inter_agent_dir):
            result = fn(str(engram_home))
            assert result == "", f"{hook_name}: expected '' on corrupt JSON; got {result!r}"

    def test_stale_nudge_appears_for_old_star(self, engram_home, inter_agent_dir):
        """Stars older than STARRED_STALE_DAYS render a trailing stale nudge."""
        filename = "2026-05-30T10-00-00Z_borges.md"
        _write_letter(inter_agent_dir, filename, "borges", "ariadne", _ts(-60), "Old work")
        # Use a date well in the past to guarantee stale (> 7 days ago)
        stale_ts = "2026-01-01T00:00:00Z"
        _write_starred(engram_home, [{
            "filename": filename,
            "from": "borges",
            "title": "Old work",
            "starred_at": stale_ts,
            "note": "",
        }])

        for hook_name, fn in _get_starred_block_fns(engram_home, inter_agent_dir):
            result = fn(str(engram_home))
            assert "stale" in result, f"{hook_name}: expected stale nudge for old star; got {result!r}"
            assert "unstar if resolved" in result, f"{hook_name}: expected unstar nudge; got {result!r}"

    def test_fresh_star_no_stale_nudge(self, engram_home, inter_agent_dir):
        """Stars within STARRED_STALE_DAYS do NOT get the stale nudge."""
        filename = "2026-05-30T10-00-00Z_borges.md"
        _write_letter(inter_agent_dir, filename, "borges", "ariadne", _ts(-60), "Fresh work")
        # Use a timestamp from one day ago (within the 7-day window)
        fresh_ts = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_starred(engram_home, [{
            "filename": filename,
            "from": "borges",
            "title": "Fresh work",
            "starred_at": fresh_ts,
            "note": "",
        }])

        for hook_name, fn in _get_starred_block_fns(engram_home, inter_agent_dir):
            result = fn(str(engram_home))
            assert "stale" not in result, f"{hook_name}: expected no stale nudge for fresh star; got {result!r}"
            assert "unstar if resolved" not in result, f"{hook_name}: expected no unstar nudge; got {result!r}"
