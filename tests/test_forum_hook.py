"""Tests for hooks/claude/engram-forum-prompt-hook.py — surfaced-cursor advance semantics.

Covers three scenarios for the cursor-advance gate (fix for the twin-bug
where the cursor was advanced on a cache-hit, allowing posts that arrived
after the cache was populated but before it expired to be dropped):

  1. Cache hit  → cursor UNCHANGED.
  2. Live fetch success → cursor ADVANCED to ~now.
  3. Unreachable, no cache → cursor UNCHANGED.

Each test loads the hook as a module via importlib, monkeypatches
SURFACED_CURSOR_PATH and HOOK_CACHE_PATH to tmp paths, and stubs
_load_config / _fetch_threads as needed. main() is called directly
(catching SystemExit); then the cursor file is inspected.
"""

import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).parent.parent
    / "hooks" / "claude" / "engram-forum-prompt-hook.py"
)


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_hook():
    """Load the forum prompt hook as a Python module."""
    spec = importlib.util.spec_from_file_location(
        "engram_forum_prompt_hook", HOOK_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_cursor_file(path: Path, ts: datetime) -> None:
    """Write a UTC datetime to a cursor file in the hook's expected format."""
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(ts_str + "\n")


def _read_cursor_bytes(path: Path) -> bytes:
    """Read a cursor file as raw bytes for byte-for-byte comparison."""
    return path.read_bytes()


def _write_fresh_cache(cache_path: Path, threads: list) -> None:
    """Write a fresh (not expired) cache file at cache_path."""
    payload = {"fetched_at": time.time(), "threads": threads}
    cache_path.write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_cursor(tmp_path: Path) -> Path:
    return tmp_path / "forum-surfaced-cursor.txt"


@pytest.fixture
def tmp_cache(tmp_path: Path) -> Path:
    return tmp_path / "forum-hook-cache.json"


# ---------------------------------------------------------------------------
# Scenario 1: Cache hit → cursor UNCHANGED
# ---------------------------------------------------------------------------

def test_cache_hit_cursor_unchanged(tmp_cursor, tmp_cache, monkeypatch):
    """On a cache hit, the surfaced cursor must not advance.

    The live fetch that populated the cache already advanced the cursor to
    its fetch time. Advancing again to `now` here would step past posts that
    arrived after the cache was populated but before it expired.
    """
    hook = _load_hook()

    # Point the module at our tmp paths
    monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", str(tmp_cursor))
    monkeypatch.setattr(hook, "HOOK_CACHE_PATH", str(tmp_cache))

    # Stub _load_config to return a configured dict (avoids touching disk)
    monkeypatch.setattr(hook, "_load_config", lambda: {
        "agent_name": "testbot",
        "forum": {"url": "http://localhost:5002"},
    })

    # Write a known cursor value
    known_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _write_cursor_file(tmp_cursor, known_ts)
    cursor_before = _read_cursor_bytes(tmp_cursor)

    # Seed a fresh cache (age 0 — well within 30s TTL)
    _write_fresh_cache(tmp_cache, [])

    # Run main(); it will hit the cache branch and call sys.exit(0) because
    # threads is empty — catch that.
    try:
        hook.main()
    except SystemExit:
        pass

    # Cursor must be byte-for-byte unchanged
    cursor_after = _read_cursor_bytes(tmp_cursor)
    assert cursor_after == cursor_before, (
        f"Cursor must be unchanged on a cache hit; "
        f"before={cursor_before!r}, after={cursor_after!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 2: Live fetch success → cursor ADVANCED
# ---------------------------------------------------------------------------

def test_live_fetch_success_cursor_advanced(tmp_cursor, tmp_cache, monkeypatch):
    """On a successful live fetch, the surfaced cursor must advance to ~now."""
    hook = _load_hook()

    monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", str(tmp_cursor))
    monkeypatch.setattr(hook, "HOOK_CACHE_PATH", str(tmp_cache))

    monkeypatch.setattr(hook, "_load_config", lambda: {
        "agent_name": "testbot",
        "forum": {"url": "http://localhost:5002"},
    })

    # Write a known cursor value in the past
    old_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _write_cursor_file(tmp_cursor, old_ts)

    # No cache file → forces the live-fetch branch
    assert not tmp_cache.exists()

    # Stub _fetch_threads to return a non-None empty list (success, no threads)
    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])

    before_call = datetime.now(timezone.utc)

    # main() will call sys.exit(0) because threads list is empty
    try:
        hook.main()
    except SystemExit:
        pass

    after_call = datetime.now(timezone.utc)

    # Cursor must exist and have advanced past old_ts
    assert tmp_cursor.exists(), "Cursor file must exist after live fetch"
    raw = tmp_cursor.read_text().strip()
    new_ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    assert new_ts > old_ts, (
        f"Cursor must advance on live fetch success; old={old_ts}, new={new_ts}"
    )
    # Sanity: the new cursor is within the window of the call
    assert new_ts >= before_call.replace(microsecond=0) or new_ts >= before_call, (
        f"Cursor must be ~now after live fetch; before_call={before_call}, new={new_ts}"
    )
    _ = after_call  # referenced for documentation, not strictly needed in assertion


# ---------------------------------------------------------------------------
# Scenario 3: Unreachable, no cache → cursor UNCHANGED
# ---------------------------------------------------------------------------

def test_unreachable_no_cache_cursor_unchanged(tmp_cursor, tmp_cache, monkeypatch):
    """When server is unreachable and no cache exists, cursor must not advance.

    Leaving the cursor in place ensures the next successful query re-checks
    the same window and doesn't miss posts made during the outage.
    """
    hook = _load_hook()

    monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", str(tmp_cursor))
    monkeypatch.setattr(hook, "HOOK_CACHE_PATH", str(tmp_cache))

    monkeypatch.setattr(hook, "_load_config", lambda: {
        "agent_name": "testbot",
        "forum": {"url": "http://localhost:5002"},
    })

    # Write a known cursor value
    known_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _write_cursor_file(tmp_cursor, known_ts)
    cursor_before = _read_cursor_bytes(tmp_cursor)

    # No cache file, and _fetch_threads returns None (server unreachable)
    assert not tmp_cache.exists()
    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: None)

    # main() exits silently (threads=None → no-op)
    try:
        hook.main()
    except SystemExit:
        pass

    # Cursor must be byte-for-byte unchanged
    cursor_after = _read_cursor_bytes(tmp_cursor)
    assert cursor_after == cursor_before, (
        f"Cursor must be unchanged when server is unreachable and no cache exists; "
        f"before={cursor_before!r}, after={cursor_after!r}"
    )
