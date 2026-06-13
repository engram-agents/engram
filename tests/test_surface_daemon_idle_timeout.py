"""Tests for the surface daemon's idle-timeout env-gate.

Covers (per spec):
  (a) ENGRAM_DAEMON_IDLE_TIMEOUT=0  → idle-shutdown disabled (idle_timeout <= 0).
  (b) ENGRAM_DAEMON_IDLE_TIMEOUT unset → default 28800 (8 h).
  (c) ENGRAM_DAEMON_IDLE_TIMEOUT set to garbage → falls back to 28800, no crash.
  (d) Positive integer override → used as-is.
  (e) Negative value → treated as "disabled" (idle_timeout <= 0).
  (f) idle_watchdog returns immediately when idle_timeout <= 0 (no blocking).
  (g) idle_watchdog triggers shutdown when idle_timeout > 0 and elapsed > timeout.

All tests use monkeypatching; no socket is opened and no daemon subprocess is
spawned.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path setup — mirror of test_surface_daemon_indexer.py
# ---------------------------------------------------------------------------
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

DAEMON_DIR = str(
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "hooks"
    / "claude"
)
if DAEMON_DIR not in sys.path:
    sys.path.insert(0, DAEMON_DIR)

_DAEMON_MODULE_PATH = str(
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "hooks"
    / "claude"
    / "engram-surface-daemon.py"
)


def _load_daemon_module():
    """Load engram-surface-daemon as a fresh module instance."""
    spec = importlib.util.spec_from_file_location(
        "engram_surface_daemon", _DAEMON_MODULE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# (a)–(e): _resolve_idle_timeout() parsing
# ---------------------------------------------------------------------------


class TestResolveIdleTimeout(unittest.TestCase):
    """_resolve_idle_timeout() reads ENGRAM_DAEMON_IDLE_TIMEOUT with fallback."""

    def _load(self):
        return _load_daemon_module()

    def test_unset_returns_default_28800(self):
        """Unset env var → default 28800 (8 hours, unchanged legacy behaviour)."""
        mod = self._load()
        env = {k: v for k, v in os.environ.items() if k != "ENGRAM_DAEMON_IDLE_TIMEOUT"}
        with patch.dict(os.environ, env, clear=True):
            result = mod._resolve_idle_timeout()
        self.assertEqual(result, 28800)

    def test_empty_string_returns_default_28800(self):
        """Empty-string env var is treated as unset → 28800."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_DAEMON_IDLE_TIMEOUT": ""}):
            result = mod._resolve_idle_timeout()
        self.assertEqual(result, 28800)

    def test_zero_returns_zero(self):
        """ENGRAM_DAEMON_IDLE_TIMEOUT=0 → returns 0 (disabled marker)."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_DAEMON_IDLE_TIMEOUT": "0"}):
            result = mod._resolve_idle_timeout()
        self.assertEqual(result, 0)

    def test_zero_is_not_positive_disabled(self):
        """0 is <= 0, so idle_watchdog should treat it as disabled."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_DAEMON_IDLE_TIMEOUT": "0"}):
            val = mod._resolve_idle_timeout()
        self.assertLessEqual(val, 0, "idle_timeout=0 must be treated as disabled")

    def test_negative_returns_negative(self):
        """Negative value → returned as-is (caller treats <= 0 as disabled)."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_DAEMON_IDLE_TIMEOUT": "-1"}):
            result = mod._resolve_idle_timeout()
        self.assertLessEqual(result, 0)

    def test_positive_integer_override(self):
        """A valid positive integer overrides the default."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_DAEMON_IDLE_TIMEOUT": "3600"}):
            result = mod._resolve_idle_timeout()
        self.assertEqual(result, 3600)

    def test_minimum_positive_value(self):
        """Value of 1 (positive) is accepted as-is."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_DAEMON_IDLE_TIMEOUT": "1"}):
            result = mod._resolve_idle_timeout()
        self.assertEqual(result, 1)

    def test_garbage_string_falls_back_to_28800(self):
        """Non-integer garbage → falls back to 28800; no crash."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_DAEMON_IDLE_TIMEOUT": "not-a-number"}):
            result = mod._resolve_idle_timeout()
        self.assertEqual(result, 28800)

    def test_float_string_falls_back_to_28800(self):
        """Float string (not int-parseable) → falls back to 28800."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_DAEMON_IDLE_TIMEOUT": "3600.5"}):
            result = mod._resolve_idle_timeout()
        self.assertEqual(result, 28800)

    def test_leading_whitespace_garbage_falls_back(self):
        """String with surrounding text → falls back to 28800."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_DAEMON_IDLE_TIMEOUT": "3600s"}):
            result = mod._resolve_idle_timeout()
        self.assertEqual(result, 28800)

    def test_resolve_does_not_crash_on_any_input(self):
        """A variety of bad inputs must not raise any exception."""
        mod = self._load()
        bad_values = ["", "abc", "1.5", "3600s", "--42", "None", "9" * 100]
        for val in bad_values:
            with self.subTest(val=val):
                with patch.dict(os.environ, {"ENGRAM_DAEMON_IDLE_TIMEOUT": val}):
                    try:
                        mod._resolve_idle_timeout()
                    except Exception as exc:
                        self.fail(
                            f"_resolve_idle_timeout() raised {exc!r} for input {val!r}"
                        )


# ---------------------------------------------------------------------------
# (f): idle_watchdog disabled when idle_timeout <= 0
# ---------------------------------------------------------------------------


class TestIdleWatchdogDisabled(unittest.TestCase):
    """idle_watchdog must return immediately when idle_timeout <= 0."""

    def _load(self):
        return _load_daemon_module()

    def _run_watchdog_with_timeout(self, idle_timeout: int, join_timeout: float = 1.0) -> bool:
        """Run idle_watchdog in a thread; return True if it exits within join_timeout."""
        mod = self._load()
        # Patch _shutdown so it doesn't interfere with the module-level singleton
        shutdown_event = threading.Event()
        # idle_watchdog reads the module's _shutdown; patch it
        mod._shutdown = shutdown_event

        done = threading.Event()

        def _run():
            mod.idle_watchdog(idle_timeout)
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=join_timeout)
        return done.is_set()

    def test_zero_timeout_returns_immediately(self):
        """idle_timeout=0 → watchdog returns without blocking."""
        exited = self._run_watchdog_with_timeout(idle_timeout=0, join_timeout=1.0)
        self.assertTrue(
            exited,
            "idle_watchdog(0) must return immediately (persistent-service mode); it blocked",
        )

    def test_negative_timeout_returns_immediately(self):
        """idle_timeout=-1 → watchdog returns without blocking."""
        exited = self._run_watchdog_with_timeout(idle_timeout=-1, join_timeout=1.0)
        self.assertTrue(
            exited,
            "idle_watchdog(-1) must return immediately (persistent-service mode); it blocked",
        )

    def test_disabled_watchdog_does_not_set_shutdown(self):
        """When disabled, idle_watchdog must NOT set _shutdown."""
        mod = self._load()
        shutdown_event = threading.Event()
        mod._shutdown = shutdown_event

        def _run():
            mod.idle_watchdog(0)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=1.0)

        self.assertFalse(
            shutdown_event.is_set(),
            "idle_watchdog(0) must not set _shutdown (persistent-service mode)",
        )


# ---------------------------------------------------------------------------
# (g): idle_watchdog triggers shutdown when timeout > 0 and elapsed exceeds it
# ---------------------------------------------------------------------------


class TestIdleWatchdogActive(unittest.TestCase):
    """idle_watchdog must set _shutdown when idle_timeout > 0 and idle elapsed.

    test_watchdog_fires_when_idle_exceeded is the load-bearing gate for the
    positive case; no separate negative-case test is retained (the prior
    test_watchdog_does_not_fire_when_not_idle ended with assertTrue(True) and
    provided no real coverage).
    """

    def _load(self):
        return _load_daemon_module()

    def test_watchdog_fires_when_idle_exceeded(self):
        """With idle_timeout=1 and _last_activity far in the past, shutdown fires.

        Strategy: set _last_activity to a timestamp 100 s in the past so
        the watchdog's elapsed check fires on the very first iteration.
        Then join with a reasonable timeout and confirm _shutdown was set.
        """
        mod = self._load()
        shutdown_event = threading.Event()
        mod._shutdown = shutdown_event
        # Simulate 100 s of idle time — far past a 1s timeout
        mod._last_activity = time.time() - 100

        def _run():
            mod.idle_watchdog(idle_timeout=1)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        # Allow up to 65 s (one watchdog check cycle + margin).
        # In practice the check fires instantly since elapsed > timeout already.
        # We use a short timeout here; the watchdog checks every 60s at most,
        # but since elapsed > idle_timeout on the first check, it fires immediately.
        t.join(timeout=5.0)

        self.assertTrue(
            shutdown_event.is_set(),
            "idle_watchdog must set _shutdown when idle > idle_timeout",
        )


if __name__ == "__main__":
    unittest.main()
