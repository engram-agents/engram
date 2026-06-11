"""Tests for the auto-publish additions to hooks/claude/engram-forum-prompt-hook.py.

Covers spec #1077 part 1 requirements:
  1. tools/_status_derive.py unit tests — derive_own_status signal combinations.
     (Port of spec requirements; the module-level tests live in test_status_derive.py;
     this file tests the HOOK's usage of the module.)
  2. Throttle logic — _should_publish pure helper.
  3. Q4 sleep guard — _apply_sleep_guard pure helper.
  4. Hook resilience — import-failure path and POST-raises path both leave
     existing thread/mention output intact and exit 0.

The helpers _should_publish and _apply_sleep_guard are pure (no server, no fs),
tested directly. The hook resilience tests use importlib + monkeypatching, the
same pattern as test_forum_hook.py.
"""

import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

HOOK_PATH = (
    Path(__file__).parent.parent
    / "hooks" / "claude" / "engram-forum-prompt-hook.py"
)


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_hook():
    """Load the forum prompt hook as a Python module (fresh import each call)."""
    spec = importlib.util.spec_from_file_location(
        "engram_forum_prompt_hook", HOOK_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Helpers (shared with test_forum_hook.py pattern)
# ---------------------------------------------------------------------------

def _write_cursor_file(path: Path, ts: datetime) -> None:
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    path.write_text(ts_str + "\n")


def _write_fresh_cache(cache_path: Path, threads: list, mentions: list = None) -> None:
    payload = {"fetched_at": time.time(), "threads": threads}
    if mentions is not None:
        payload["mentions"] = mentions
    cache_path.write_text(json.dumps(payload))


def _run_main(hook, capture_output: bool = False):
    if capture_output:
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                hook.main()
        except SystemExit:
            pass
        return buf.getvalue()
    else:
        try:
            hook.main()
        except SystemExit:
            pass
        return None


def _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_publish):
    monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", str(tmp_cursor))
    monkeypatch.setattr(hook, "HOOK_CACHE_PATH", str(tmp_cache))
    monkeypatch.setattr(hook, "STATUS_PUBLISH_PATH", str(tmp_publish))


def _stub_config(hook, monkeypatch, agent_name: str = "ariadne"):
    monkeypatch.setattr(hook, "_load_config", lambda: {
        "agent_name": agent_name,
        "forum": {"url": "http://localhost:5002"},
    })


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_cursor(tmp_path):
    return tmp_path / "forum-surfaced-cursor.txt"


@pytest.fixture
def tmp_cache(tmp_path):
    return tmp_path / "forum-hook-cache.json"


@pytest.fixture
def tmp_publish(tmp_path):
    return tmp_path / "forum-status-publish.json"


# ===========================================================================
# 2. Throttle logic — _should_publish
# ===========================================================================

class TestShouldPublish:
    """Pure helper — no filesystem, no server."""

    def _get_helper(self):
        hook = _load_hook()
        return hook._should_publish

    def test_no_last_file_publish(self):
        fn = self._get_helper()
        assert fn(None, ("working", None, (), 270), time.time(), 60) is True

    def test_state_changed_publish(self):
        fn = self._get_helper()
        last = {
            "state": "idle", "activity": None, "queue": [], "cadence": 270,
            "published_at": time.time() - 10,
        }
        current = ("working", None, (), 270)
        assert fn(last, current, time.time(), 270) is True

    def test_activity_changed_publish(self):
        fn = self._get_helper()
        last = {
            "state": "working", "activity": "old topic", "queue": [], "cadence": 270,
            "published_at": time.time() - 10,
        }
        current = ("working", "new topic", (), 270)
        assert fn(last, current, time.time(), 270) is True

    def test_unchanged_within_throttle_skip(self):
        fn = self._get_helper()
        now = time.time()
        last = {
            "state": "working", "activity": "topic", "queue": [], "cadence": 270,
            "published_at": now - 30,  # 30s ago — within 270s throttle
        }
        current = ("working", "topic", (), 270)
        assert fn(last, current, now, 270) is False

    def test_unchanged_past_throttle_publish(self):
        fn = self._get_helper()
        now = time.time()
        last = {
            "state": "working", "activity": "topic", "queue": [], "cadence": 270,
            "published_at": now - 400,  # 400s ago — past 270s throttle
        }
        current = ("working", "topic", (), 270)
        assert fn(last, current, now, 270) is True

    def test_queue_changed_publish(self):
        fn = self._get_helper()
        last = {
            "state": "working", "activity": None, "queue": ["PR-1"], "cadence": 270,
            "published_at": time.time() - 10,
        }
        current = ("working", None, ("PR-1", "PR-2"), 270)
        assert fn(last, current, time.time(), 270) is True

    def test_cadence_none_floor_used(self):
        """cadence None → throttle=max(0,60)=60; within 60s → skip."""
        fn = self._get_helper()
        now = time.time()
        last = {
            "state": "idle", "activity": None, "queue": [], "cadence": None,
            "published_at": now - 30,  # 30s ago — within 60s floor
        }
        current = ("idle", None, (), None)
        assert fn(last, current, now, 60) is False

    def test_cadence_sentinel_floor_used(self):
        """cadence 0 (on-call) → throttle=max(0,60)=60."""
        fn = self._get_helper()
        now = time.time()
        last = {
            "state": "working", "activity": None, "queue": [], "cadence": 0,
            "published_at": now - 30,
        }
        current = ("working", None, (), 0)
        assert fn(last, current, now, 60) is False


# ===========================================================================
# 3. Q4 Sleep guard — _apply_sleep_guard
# ===========================================================================

class TestApplySleepGuard:
    """Pure helper — monkeypatches _read_loop_mode via the hook module."""

    def _get_helper_and_hook(self):
        hook = _load_hook()
        return hook._apply_sleep_guard, hook

    def test_no_loop_last_sleeping_within_window_overrides(self):
        fn, hook = self._get_helper_and_hook()
        # Patch _read_loop_mode to return None (no loop marker)
        hook._read_loop_mode = lambda: None

        now = time.time()
        last = {
            "state": "sleeping", "activity": "bedtime routine",
            "queue": [], "cadence": 270, "published_at": now - 30,
        }
        state, activity, queue, cadence = fn("idle", None, [], None, last, now)
        assert state == "sleeping"
        assert activity == "bedtime routine"
        # Cadence must be restored from the last sleeping publish (not left at
        # the derived None) — otherwise _should_publish sees a changed tuple and
        # fires a spurious cadence-dropping POST. (Reviewer catch, #1077 r2.)
        assert cadence == 270

    def test_no_loop_last_sleeping_past_window_not_overridden(self):
        fn, hook = self._get_helper_and_hook()
        hook._read_loop_mode = lambda: None

        now = time.time()
        last = {
            "state": "sleeping", "activity": "bedtime routine",
            "queue": [], "cadence": 270, "published_at": now - 400,
        }
        # Past the window (400 > max(270,60)=270)
        state, activity, queue, cadence = fn("idle", None, [], None, last, now)
        assert state == "idle"
        # Past window → derived cadence (None) flows through unchanged.
        assert cadence is None

    def test_loop_marker_present_no_override(self):
        fn, hook = self._get_helper_and_hook()
        hook._read_loop_mode = lambda: {"topic": "active loop"}

        now = time.time()
        last = {
            "state": "sleeping", "activity": None,
            "queue": [], "cadence": 270, "published_at": now - 30,
        }
        state, activity, queue, cadence = fn("working", "loop topic", [], 60, last, now)
        # Loop present → no override (cadence flows through unchanged)
        assert state == "working"
        assert cadence == 60

    def test_no_last_file_no_override(self):
        fn, hook = self._get_helper_and_hook()
        hook._read_loop_mode = lambda: None

        now = time.time()
        state, activity, queue, cadence = fn("idle", None, [], None, None, now)
        assert state == "idle"
        assert cadence is None

    def test_last_not_sleeping_no_override(self):
        fn, hook = self._get_helper_and_hook()
        hook._read_loop_mode = lambda: None

        now = time.time()
        last = {
            "state": "idle", "activity": None,
            "queue": [], "cadence": 270, "published_at": now - 30,
        }
        state, activity, queue, cadence = fn("working", None, [], 120, last, now)
        # Last was not sleeping → no override (cadence flows through unchanged)
        assert state == "working"
        assert cadence == 120

    def test_sleep_guard_into_should_publish_no_spurious_publish(self):
        """Integration: a false-wake mid-sleep must NOT trigger a publish.

        After sleep, derive_own_status returns (idle/working, ..., cadence=None).
        The sleep guard restores the full last-sleeping tuple INCLUDING cadence,
        so _should_publish sees an unchanged tuple within the throttle window and
        skips — no spurious cadence-dropping POST. (Reviewer catch, #1077 r2.)
        """
        guard, hook = self._get_helper_and_hook()
        should_publish = hook._should_publish
        hook._read_loop_mode = lambda: None

        now = time.time()
        last = {
            "state": "sleeping", "activity": "bedtime routine",
            "queue": [], "cadence": 270, "published_at": now - 30,
        }
        # Derived (false wake): no loop marker → idle, cadence None.
        state, activity, queue, cadence = guard("idle", None, [], None, last, now)
        throttle = max(cadence or 0, 60)
        current_tuple = (state, activity, tuple(queue), cadence)
        # Tuple is identical to last → no publish.
        assert should_publish(last, current_tuple, now, throttle) is False


# ===========================================================================
# 4. Hook resilience
# ===========================================================================

class TestHookResilience:
    """Verify the existing thread/mention output is unaffected by publish failures."""

    def test_import_failure_path_output_intact(
        self, tmp_cursor, tmp_cache, tmp_publish, monkeypatch
    ):
        """When derive_own_status import fails (set to None), thread output is unaffected."""
        hook = _load_hook()
        _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_publish)
        _stub_config(hook, monkeypatch)

        # Simulate import failure
        monkeypatch.setattr(hook, "derive_own_status", None)

        _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        _write_fresh_cache(tmp_cache, threads=[{"id": "1", "title": "test thread"}])

        output = _run_main(hook, capture_output=True)
        assert output.strip(), "Expected output even when derive_own_status is None"
        data = json.loads(output.strip())
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "\U0001f4e2" in ctx, "Generic threads line must appear when publish import failed"

    def test_post_raises_output_intact(
        self, tmp_cursor, tmp_cache, tmp_publish, monkeypatch
    ):
        """When the POST raises, the thread/mention output is unaffected and exits 0."""
        hook = _load_hook()
        _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_publish)
        _stub_config(hook, monkeypatch)

        # Stub derive_own_status to return a valid tuple
        monkeypatch.setattr(
            hook, "derive_own_status",
            lambda agent: ("working", "testing", [], 270)
        )
        # Stub _read_loop_mode for sleep guard (no loop marker)
        monkeypatch.setattr(hook, "_read_loop_mode", lambda: None)

        # Make urlopen raise for the POST (but not for threads/inbox fetches)
        _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        _write_fresh_cache(tmp_cache, threads=[{"id": "2", "title": "a thread"}])

        original_urlopen = hook.urllib.request.urlopen
        call_count = [0]

        def patched_urlopen(req, timeout=None):
            call_count[0] += 1
            # Raise on the POST (status endpoint)
            if hasattr(req, "get_method") and req.get_method() == "POST":
                raise ConnectionRefusedError("no server")
            return original_urlopen(req, timeout=timeout)

        monkeypatch.setattr(hook.urllib.request, "urlopen", patched_urlopen)

        output = _run_main(hook, capture_output=True)
        assert output.strip(), "Expected threads output even when POST raises"
        data = json.loads(output.strip())
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "\U0001f4e2" in ctx, "Generic threads line must be present when POST raises"
        # Publish state file must NOT have been written (POST failed)
        assert not tmp_publish.exists(), (
            "Publish state file must NOT be written when POST fails"
        )

    def test_post_success_writes_publish_state(
        self, tmp_cursor, tmp_cache, tmp_publish, monkeypatch
    ):
        """When POST succeeds, the publish state file is written."""
        hook = _load_hook()
        _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_publish)
        _stub_config(hook, monkeypatch)

        monkeypatch.setattr(
            hook, "derive_own_status",
            lambda agent: ("working", "testing", [], 270)
        )
        monkeypatch.setattr(hook, "_read_loop_mode", lambda: None)

        _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        _write_fresh_cache(tmp_cache, threads=[{"id": "3", "title": "new thread"}])

        class _FakeResp:
            def read(self):
                return b'{"ok": true}'
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        def patched_urlopen(req, timeout=None):
            return _FakeResp()

        monkeypatch.setattr(hook.urllib.request, "urlopen", patched_urlopen)

        _run_main(hook, capture_output=True)

        assert tmp_publish.exists(), "Publish state file must be written after successful POST"
        state_data = json.loads(tmp_publish.read_text())
        assert state_data["state"] == "working"
        assert state_data["cadence"] == 270
        assert "published_at" in state_data

    def test_throttle_skips_unchanged_within_window(
        self, tmp_cursor, tmp_cache, tmp_publish, monkeypatch
    ):
        """When status is unchanged and within throttle, no POST is made."""
        hook = _load_hook()
        _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_publish)
        _stub_config(hook, monkeypatch)

        monkeypatch.setattr(
            hook, "derive_own_status",
            lambda agent: ("working", "testing", [], 270)
        )
        monkeypatch.setattr(hook, "_read_loop_mode", lambda: None)

        # Pre-populate publish state with same values, recently published
        now = time.time()
        tmp_publish.write_text(json.dumps({
            "state": "working", "activity": "testing",
            "queue": [], "cadence": 270,
            "published_at": now - 30,  # 30s ago — within 270s throttle
        }))

        _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        _write_fresh_cache(tmp_cache, threads=[])

        post_calls = []

        def patched_urlopen(req, timeout=None):
            if hasattr(req, "get_method") and req.get_method() == "POST":
                post_calls.append(req)
            raise ConnectionRefusedError("should not be called")

        monkeypatch.setattr(hook.urllib.request, "urlopen", patched_urlopen)

        _run_main(hook, capture_output=True)

        assert post_calls == [], (
            "No POST should be made when status unchanged within throttle window"
        )

    def test_empty_agent_name_skips_publish(
        self, tmp_cursor, tmp_cache, tmp_publish, monkeypatch
    ):
        """When agent_name is empty, publish is skipped entirely."""
        hook = _load_hook()
        _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_publish)

        # Config with no agent_name; forum URL set so it doesn't no-op at the gate
        monkeypatch.setattr(hook, "_load_config", lambda: {
            "forum": {"url": "http://localhost:5002"},
        })
        monkeypatch.setattr(hook, "_get_agent_name", lambda config: "")

        _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        _write_fresh_cache(tmp_cache, threads=[{"id": "10", "title": "thread"}])

        post_calls = []

        def patched_urlopen(req, timeout=None):
            if hasattr(req, "get_method") and req.get_method() == "POST":
                post_calls.append(req)
            raise ConnectionRefusedError("should not be called for status")

        monkeypatch.setattr(hook.urllib.request, "urlopen", patched_urlopen)

        _run_main(hook, capture_output=True)

        assert post_calls == [], "No POST when agent_name is empty"
