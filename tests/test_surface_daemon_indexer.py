"""Tests for the surface daemon's periodic JSONL→index.db indexer tick.

Covers (per spec):
  (a) Gates on interval — Indexer.run_once NOT called before interval elapses.
  (b) Invokes run_once when interval has elapsed.
  (c) Swallows exceptions from run_once and still returns / advances the
      timestamp — the daemon survives indexer failures.
  (d) ENGRAM_INDEXER_INTERVAL_S env override parsing, including bad-value fallback.
  (e) _indexer_loop survives a failing tick and continues to the next.

19 cases total. All tests use monkeypatching and a fake monotonic clock; no
real ENGRAM DB is needed and no socket is opened.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so we can import the daemon helpers.
# The daemon lives at hooks/claude/engram-surface-daemon.py; project root is
# two dirname() levels up from that, same as what _resolve_runtime_dir does.
# ---------------------------------------------------------------------------
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# The daemon module is not on sys.path by default (it lives under hooks/claude/).
# Add it so we can import the helper functions directly without running main().
DAEMON_DIR = str(Path(__file__).resolve().parent.parent / "hooks" / "claude")
if DAEMON_DIR not in sys.path:
    sys.path.insert(0, DAEMON_DIR)

# Import the module under test.  We use importlib so we can re-import it
# cleanly between env-override tests without contaminating the module cache.
import importlib.util

_DAEMON_MODULE_PATH = str(
    Path(__file__).resolve().parent.parent / "hooks" / "claude" / "engram-surface-daemon.py"
)


def _load_daemon_module():
    """Load engram-surface-daemon as a module (hyphen in name requires importlib)."""
    spec = importlib.util.spec_from_file_location("engram_surface_daemon", _DAEMON_MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# (a) + (b): interval gating and invocation
# ---------------------------------------------------------------------------

class TestRunIndexerTickIntervalGating(unittest.TestCase):
    """run_indexer_tick respects the interval gate."""

    def _load(self):
        return _load_daemon_module()

    def test_returns_early_before_interval_elapses(self):
        """If interval has not elapsed, run_once must NOT be called."""
        mod = self._load()

        # Fake monotonic: current "now" is 50s after last_run_ts.
        # Interval is 60s, so 50 < 60 → gate should block.
        fake_now = 1000.0
        last_run_ts = fake_now - 50  # 50s ago — under the 60s threshold

        mock_run_once = MagicMock(return_value={"files_processed": 0, "rows_inserted": 0})

        with patch("time.monotonic", return_value=fake_now):
            with patch.dict("sys.modules", {"engram_log_indexer": MagicMock(Indexer=MagicMock(run_once=mock_run_once))}):
                result_ts = mod.run_indexer_tick(last_run_ts, interval_s=60)

        mock_run_once.assert_not_called()
        # Timestamp must be unchanged (early return)
        self.assertEqual(result_ts, last_run_ts)

    def test_invokes_run_once_when_interval_elapsed(self):
        """If interval HAS elapsed, run_once must be called exactly once."""
        mod = self._load()

        fake_now = 1000.0
        last_run_ts = fake_now - 61  # 61s ago — over the 60s threshold

        mock_run_once = MagicMock(return_value={"files_processed": 3, "rows_inserted": 42})

        with patch("time.monotonic", return_value=fake_now):
            with patch.dict("sys.modules", {
                "engram_log_indexer": MagicMock(Indexer=MagicMock(run_once=mock_run_once))
            }):
                result_ts = mod.run_indexer_tick(last_run_ts, interval_s=60)

        mock_run_once.assert_called_once_with()
        # Returned timestamp must be the new monotonic value
        self.assertEqual(result_ts, fake_now)

    def test_exact_boundary_triggers(self):
        """At exactly interval elapsed, the indexer fires (condition is <, not <=)."""
        mod = self._load()

        fake_now = 1000.0
        last_run_ts = fake_now - 60  # exactly 60s elapsed — condition `< 60` is False, so fires

        mock_run_once = MagicMock(return_value={"files_processed": 0, "rows_inserted": 0})

        with patch("time.monotonic", return_value=fake_now):
            with patch.dict("sys.modules", {
                "engram_log_indexer": MagicMock(Indexer=MagicMock(run_once=mock_run_once))
            }):
                result_ts = mod.run_indexer_tick(last_run_ts, interval_s=60)

        mock_run_once.assert_called_once_with()
        self.assertEqual(result_ts, fake_now)

    def test_neg_inf_last_run_ts_triggers_immediately(self):
        """last_run_ts=float('-inf') (initial sentinel) triggers a pass regardless of elapsed time."""
        mod = self._load()

        fake_now = 30.0  # only 30s process uptime — but float('-inf') forces immediate fire
        last_run_ts = float("-inf")

        mock_run_once = MagicMock(return_value={"files_processed": 1, "rows_inserted": 5})

        with patch("time.monotonic", return_value=fake_now):
            with patch.dict("sys.modules", {
                "engram_log_indexer": MagicMock(Indexer=MagicMock(run_once=mock_run_once))
            }):
                result_ts = mod.run_indexer_tick(last_run_ts, interval_s=60)

        mock_run_once.assert_called_once_with()
        self.assertEqual(result_ts, fake_now)


# ---------------------------------------------------------------------------
# (c): exception swallowing — daemon survives indexer failures
# ---------------------------------------------------------------------------

class TestRunIndexerTickExceptionSwallowing(unittest.TestCase):
    """run_indexer_tick must NOT propagate exceptions from Indexer.run_once()."""

    def _load(self):
        return _load_daemon_module()

    def test_run_once_exception_is_swallowed(self):
        """An exception from run_once must not propagate out of run_indexer_tick."""
        mod = self._load()

        fake_now = 1000.0
        last_run_ts = fake_now - 65  # overdue

        mock_run_once = MagicMock(side_effect=RuntimeError("disk full"))

        with patch("time.monotonic", return_value=fake_now):
            with patch.dict("sys.modules", {
                "engram_log_indexer": MagicMock(Indexer=MagicMock(run_once=mock_run_once))
            }):
                try:
                    result_ts = mod.run_indexer_tick(last_run_ts, interval_s=60)
                except Exception as exc:
                    self.fail(
                        f"run_indexer_tick must swallow run_once exceptions; raised: {exc!r}"
                    )

        # Even after an exception, the returned timestamp should advance so we
        # don't spam retries on a broken indexer every second.
        self.assertEqual(result_ts, fake_now)

    def test_import_error_is_swallowed(self):
        """If engram_log_indexer cannot be imported, the exception is swallowed."""
        mod = self._load()

        fake_now = 1000.0
        last_run_ts = fake_now - 120  # long overdue

        # Simulate ImportError by removing the module from sys.modules and
        # setting the import to fail.
        import builtins
        real_import = builtins.__import__

        def _broken_import(name, *args, **kwargs):
            if name == "engram_log_indexer":
                raise ImportError("engram_log_indexer not found")
            return real_import(name, *args, **kwargs)

        with patch("time.monotonic", return_value=fake_now):
            # Remove any cached engram_log_indexer so the deferred import fires
            sys.modules.pop("engram_log_indexer", None)
            with patch("builtins.__import__", side_effect=_broken_import):
                try:
                    result_ts = mod.run_indexer_tick(last_run_ts, interval_s=60)
                except Exception as exc:
                    self.fail(
                        f"run_indexer_tick must swallow import exceptions; raised: {exc!r}"
                    )

        # Timestamp still advances even on ImportError
        self.assertEqual(result_ts, fake_now)

    def test_exception_logged_to_stderr(self):
        """An exception from run_once must emit exactly one stderr line."""
        import io
        from contextlib import redirect_stderr

        mod = self._load()

        fake_now = 500.0
        last_run_ts = fake_now - 90  # overdue

        mock_run_once = MagicMock(side_effect=OSError("no space left on device"))

        buf = io.StringIO()
        with patch("time.monotonic", return_value=fake_now):
            with patch.dict("sys.modules", {
                "engram_log_indexer": MagicMock(Indexer=MagicMock(run_once=mock_run_once))
            }):
                with redirect_stderr(buf):
                    mod.run_indexer_tick(last_run_ts, interval_s=60)

        stderr_output = buf.getvalue()
        self.assertIn("indexer tick failed", stderr_output)
        self.assertIn("no space left on device", stderr_output)
        # Must be exactly one line (the failure contract: "one stderr line per failure")
        lines = [ln for ln in stderr_output.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, f"Expected 1 stderr line, got {len(lines)}: {stderr_output!r}")

    def test_successful_run_with_rows_logs_to_stderr(self):
        """A successful pass that inserts rows emits one informational stderr line."""
        import io
        from contextlib import redirect_stderr

        mod = self._load()

        fake_now = 200.0
        last_run_ts = fake_now - 70

        mock_run_once = MagicMock(return_value={"files_processed": 5, "rows_inserted": 100})

        buf = io.StringIO()
        with patch("time.monotonic", return_value=fake_now):
            with patch.dict("sys.modules", {
                "engram_log_indexer": MagicMock(Indexer=MagicMock(run_once=mock_run_once))
            }):
                with redirect_stderr(buf):
                    mod.run_indexer_tick(last_run_ts, interval_s=60)

        stderr_output = buf.getvalue()
        self.assertIn("indexer tick", stderr_output)
        self.assertIn("100", stderr_output)

    def test_successful_run_no_rows_silent(self):
        """A successful pass that inserts 0 rows must NOT log anything to stderr."""
        import io
        from contextlib import redirect_stderr

        mod = self._load()

        fake_now = 200.0
        last_run_ts = fake_now - 70

        mock_run_once = MagicMock(return_value={"files_processed": 3, "rows_inserted": 0})

        buf = io.StringIO()
        with patch("time.monotonic", return_value=fake_now):
            with patch.dict("sys.modules", {
                "engram_log_indexer": MagicMock(Indexer=MagicMock(run_once=mock_run_once))
            }):
                with redirect_stderr(buf):
                    mod.run_indexer_tick(last_run_ts, interval_s=60)

        stderr_output = buf.getvalue()
        self.assertEqual(stderr_output, "", f"Expected silent stderr for 0-row pass, got: {stderr_output!r}")


# ---------------------------------------------------------------------------
# (d): env override parsing
# ---------------------------------------------------------------------------

class TestParseIndexerInterval(unittest.TestCase):
    """_parse_indexer_interval() reads ENGRAM_INDEXER_INTERVAL_S with fallback."""

    def _load(self):
        return _load_daemon_module()

    def test_default_is_60_when_env_unset(self):
        """With no env var, interval defaults to 60."""
        mod = self._load()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENGRAM_INDEXER_INTERVAL_S", None)
            result = mod._parse_indexer_interval()
        self.assertEqual(result, 60)

    def test_valid_integer_override(self):
        """A valid positive integer in the env var overrides the default."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_INDEXER_INTERVAL_S": "120"}):
            result = mod._parse_indexer_interval()
        self.assertEqual(result, 120)

    def test_minimum_valid_value_is_1(self):
        """Value of 1 (positive integer) is accepted."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_INDEXER_INTERVAL_S": "1"}):
            result = mod._parse_indexer_interval()
        self.assertEqual(result, 1)

    def test_bad_value_non_integer_falls_back_to_60(self):
        """A non-integer string falls back to 60."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_INDEXER_INTERVAL_S": "abc"}):
            result = mod._parse_indexer_interval()
        self.assertEqual(result, 60)

    def test_bad_value_float_string_falls_back_to_60(self):
        """A float string (not int-parseable) falls back to 60."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_INDEXER_INTERVAL_S": "30.5"}):
            result = mod._parse_indexer_interval()
        self.assertEqual(result, 60)

    def test_bad_value_zero_falls_back_to_60(self):
        """Zero is not a valid interval (would spin tight loop); falls back to 60."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_INDEXER_INTERVAL_S": "0"}):
            result = mod._parse_indexer_interval()
        self.assertEqual(result, 60)

    def test_bad_value_negative_falls_back_to_60(self):
        """A negative value falls back to 60."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_INDEXER_INTERVAL_S": "-10"}):
            result = mod._parse_indexer_interval()
        self.assertEqual(result, 60)

    def test_empty_string_falls_back_to_60(self):
        """An empty string env var is treated as unset → defaults to 60."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_INDEXER_INTERVAL_S": ""}):
            result = mod._parse_indexer_interval()
        self.assertEqual(result, 60)

    def test_large_valid_value(self):
        """A large valid value (e.g. 3600 for hourly) is accepted as-is."""
        mod = self._load()
        with patch.dict(os.environ, {"ENGRAM_INDEXER_INTERVAL_S": "3600"}):
            result = mod._parse_indexer_interval()
        self.assertEqual(result, 3600)


# ---------------------------------------------------------------------------
# (e): _indexer_loop loop-level survival — loop continues after a failed tick
# ---------------------------------------------------------------------------

class TestIndexerLoopSurvival(unittest.TestCase):
    """_indexer_loop must reach a second tick after the first tick's run_once fails."""

    def _load(self):
        return _load_daemon_module()

    def test_loop_survives_failing_tick_and_calls_again(self):
        """First tick's run_once raises; second tick's run_once succeeds; loop survives.

        Strategy:
          - Inject a mock engram_log_indexer whose Indexer.run_once has a
            side_effect sequence: [raise RuntimeError, return success dict].
          - Run _indexer_loop in a separate thread with a tiny interval (0.01s)
            and a real shutdown Event.  The second run_once call sets the Event
            so the loop exits cleanly.
          - Assert run_once was called at least twice, proving the loop continued
            past the failed tick.
          - Hard 3s join-timeout so a regression hangs the test visibly rather
            than passing vacuously.
        """
        mod = self._load()
        shutdown = threading.Event()
        run_once_calls = []

        def _run_once_side_effect():
            run_once_calls.append(1)
            if len(run_once_calls) == 1:
                raise RuntimeError("simulated indexer failure on tick 1")
            # Second call succeeds; trigger shutdown so the loop exits.
            shutdown.set()
            return {"files_processed": 1, "rows_inserted": 0}

        mock_indexer_cls = MagicMock()
        mock_indexer_cls.run_once = MagicMock(side_effect=_run_once_side_effect)
        mock_indexer_mod = MagicMock()
        mock_indexer_mod.Indexer = mock_indexer_cls

        with patch.dict("sys.modules", {"engram_log_indexer": mock_indexer_mod}):
            t = threading.Thread(
                target=mod._indexer_loop,
                args=(0.01, shutdown),
                daemon=True,
            )
            t.start()
            t.join(timeout=3.0)

        # If the thread is still alive, _indexer_loop hung — hard failure.
        self.assertFalse(
            t.is_alive(),
            "_indexer_loop thread did not exit within 3s — possible hang after tick failure",
        )
        # run_once must have been called at least twice: once failing, once succeeding.
        self.assertGreaterEqual(
            len(run_once_calls),
            2,
            f"Expected >= 2 run_once calls (survive + retry); got {len(run_once_calls)}",
        )


if __name__ == "__main__":
    unittest.main()
