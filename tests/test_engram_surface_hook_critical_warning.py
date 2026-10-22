"""Tests for the daemon-offline CRITICAL warning path in engram-surface-hook.py.

Issue #356 follow-up: PR #347 added the CRITICAL warning when daemon is
unreachable, but no test asserted the path fires + templates correctly.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


# ---------------------------------------------------------------------------
# Helper: load the hook module (hyphenated filename)
# ---------------------------------------------------------------------------


def _load_hook_module(engram_home: str | None = None):
    """Import the surface hook as a module despite the hyphenated filename.

    #2086 (sweep of the #2015/#2081/#2084 ENGRAM_HOME-after-import isolation
    hazard): engram-surface-hook.py bakes derived path constants (COUNTER_PATH,
    WRITE_REMINDER_PATH, REPAIR_MARKER_PATH, WARM_BRIEFING_PATH,
    ERROR_PATTERNS_PATH, ERROR_INCIDENTS_PATH, PRINCIPLE_TRIGGERS_PATH,
    PRINCIPLE_TRIGGER_STATE_PATH, SOCKET_PATH, KNOWLEDGE_DB_PATH, ...) from
    ENGRAM_HOME ONCE at import time. This file's tests drive hook.main(),
    which reads/writes most of those constants directly (e.g. via
    check_principle_triggers_full, check_write_reminder, check_repair_marker,
    check_warm_briefing) — so patching only `hook.ENGRAM_HOME` (or a couple
    of the derived constants) AFTER import, as this file previously did,
    leaves the rest resolving against the REAL `$ENGRAM_HOME` (default
    `~/.engram`), reading and writing a live agent's actual state while
    green in CI. Setting ENGRAM_HOME in the environment BEFORE exec_module
    (matching test_surface_ledger_suppression.py's LedgerTestBase convention)
    makes every import-time-computed constant resolve against `engram_home`.
    """
    hook_path = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
    spec = importlib.util.spec_from_file_location("engram_surface_hook", hook_path)
    mod = importlib.util.module_from_spec(spec)
    if engram_home is not None:
        with mock.patch.dict(os.environ, {"ENGRAM_HOME": engram_home}):
            spec.loader.exec_module(mod)
    else:
        spec.loader.exec_module(mod)
    return mod


def _make_hook_input(prompt: str = "hello, what's next?") -> str:
    """Build a minimal JSON hook-input payload for sys.stdin."""
    return json.dumps({"prompt": prompt})


def _run_main_capture(hook, hook_input_json: str) -> str:
    """Run hook.main() with mocked stdin; return the captured stdout text.

    Patches query_daemon → None (daemon offline) and query_fts_fallback → {}
    so the CRITICAL warning fires and the rest of main() degrades gracefully.
    Catches SystemExit(0) which main() always raises on success.
    Returns the raw text printed to stdout.
    """
    output_buf = io.StringIO()

    with patch.object(hook, "query_daemon", return_value=None), \
         patch.object(hook, "query_fts_fallback", return_value={}), \
         patch("sys.stdin", io.StringIO(hook_input_json)), \
         patch("sys.stdout", output_buf):
        try:
            hook.main()
        except SystemExit:
            pass

    return output_buf.getvalue()


# ---------------------------------------------------------------------------
# Tests for get_user_name()
# ---------------------------------------------------------------------------


class TestGetUserName(unittest.TestCase):
    """Unit tests for get_user_name() — the helper that supplies {user_name}
    in the CRITICAL warning template."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_critical_test_")
        self.hook = _load_hook_module(self.tmp)
        # #2086 self-guard: if the env-before-import fix above is ever
        # reverted, this fails loudly instead of silently reading real state.
        self.assertEqual(
            self.hook.ENGRAM_HOME, self.tmp,
            "hook.ENGRAM_HOME did not resolve against the test's isolated "
            "ENGRAM_HOME -- the env-patch-before-import fix for #2086 may "
            "have regressed.",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config(self, data: dict) -> None:
        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_user_name_renders_from_primary_user(self):
        """config.json = {"primary_user": "Alex"} → get_user_name() returns "Alex"."""
        self._write_config({"primary_user": "Alex"})
        result = self.hook.get_user_name()
        self.assertEqual(result, "Alex")

    def test_fallback_when_primary_user_unset(self):
        """config.json with no primary_user key → get_user_name() returns "the human"."""
        self._write_config({})
        result = self.hook.get_user_name()
        self.assertEqual(result, "the human")

    def test_fallback_when_config_absent(self):
        """No config.json file in ENGRAM_HOME → get_user_name() returns "the human"."""
        # setUp creates a clean temp dir with no config.json
        result = self.hook.get_user_name()
        self.assertEqual(result, "the human")

    def test_fallback_when_primary_user_non_string(self):
        """config.json = {"primary_user": 42} → str() coercion → returns "42".

        Item 2 of issue #356: the str() wrap in get_user_name() ensures that
        a truthy non-string value (e.g., integer 42) is returned as a string,
        not the raw int. Without str(), the f-string would still render "42"
        but the return type would be int — type-impure.
        """
        self._write_config({"primary_user": 42})
        result = self.hook.get_user_name()
        self.assertIsInstance(result, str, "get_user_name() must return str, not int")
        self.assertEqual(result, "42")


# ---------------------------------------------------------------------------
# Integration test: CRITICAL warning fires when used_daemon=False
# ---------------------------------------------------------------------------


class TestCriticalWarningIntegration(unittest.TestCase):
    """The ⚠️ ENGRAM CRITICAL warning fires in main() when used_daemon=False."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_critical_integration_test_")
        # Write a config.json with primary_user set so the full template fires.
        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"primary_user": "Alex"}, f)
        # Load the hook and redirect ENGRAM_HOME to our temp dir.
        self.hook = _load_hook_module(self.tmp)
        # #2086 self-guard: hook.main() reads/writes many ENGRAM_HOME-derived
        # constants (COUNTER_PATH, PRINCIPLE_TRIGGER_STATE_PATH, ...) that a
        # post-import `hook.ENGRAM_HOME = self.tmp` reassignment does NOT
        # redirect (they're frozen strings computed once at import). Assert
        # one of them here so a regression to post-import patching fails
        # loudly instead of silently reading/writing real ~/.engram state.
        self.assertEqual(
            self.hook.PRINCIPLE_TRIGGER_STATE_PATH,
            os.path.join(self.tmp, "principle-trigger-state.json"),
            "PRINCIPLE_TRIGGER_STATE_PATH did not resolve against the "
            "test's isolated ENGRAM_HOME -- the env-patch-before-import "
            "fix for #2086 may have regressed.",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_critical_text_fires_when_daemon_unreachable(self):
        """CRITICAL warning fires and templates user_name when daemon returns None.

        query_daemon is mocked to return None (simulates daemon offline).
        The output must contain the CRITICAL warning header and "Notify Alex".
        """
        raw_output = _run_main_capture(self.hook, _make_hook_input())
        self.assertTrue(
            raw_output.strip(),
            "main() produced no stdout — expected a JSON response with CRITICAL warning",
        )
        # Parse the JSON response
        response = json.loads(raw_output.strip())
        additional_context = (
            response
            .get("hookSpecificOutput", {})
            .get("additionalContext", "")
        )
        self.assertIn(
            "⚠️ ENGRAM CRITICAL",
            additional_context,
            "CRITICAL warning text must appear in additionalContext when daemon is offline",
        )
        self.assertIn(
            "Notify Alex",
            additional_context,
            "CRITICAL warning must include 'Notify Alex' when primary_user is 'Alex'",
        )
        self.assertIn(
            "persists after re-launching",
            additional_context,
            "recurrence-hint must appear in the CRITICAL warning",
        )




class TestWarmingUpSoft(unittest.TestCase):
    """SOFT warming-up message fires when launch attempt is recent."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_warmup_soft_test_")
        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"primary_user": "Alex"}, f)
        self.hook = _load_hook_module(self.tmp)
        # #2086 self-guard: hook.main() reads/writes many ENGRAM_HOME-derived
        # constants (COUNTER_PATH, PRINCIPLE_TRIGGER_STATE_PATH, ...) that a
        # post-import `hook.ENGRAM_HOME = self.tmp` reassignment does NOT
        # redirect (they're frozen strings computed once at import). Assert
        # one of them here so a regression to post-import patching fails
        # loudly instead of silently reading/writing real ~/.engram state.
        self.assertEqual(
            self.hook.PRINCIPLE_TRIGGER_STATE_PATH,
            os.path.join(self.tmp, "principle-trigger-state.json"),
            "PRINCIPLE_TRIGGER_STATE_PATH did not resolve against the "
            "test's isolated ENGRAM_HOME -- the env-patch-before-import "
            "fix for #2086 may have regressed.",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_warming_up_soft_when_launch_attempt_recent(self):
        """SOFT 'warming up' fires (not CRITICAL) when daemon-launch-attempt is recent."""
        import time
        # Write a current-epoch launch-attempt marker
        marker_path = os.path.join(self.tmp, "daemon-launch-attempt")
        with open(marker_path, "w") as f:
            f.write(str(int(time.time())))

        raw_output = _run_main_capture(self.hook, _make_hook_input())
        self.assertTrue(
            raw_output.strip(),
            "main() produced no stdout — expected a JSON response",
        )
        response = json.loads(raw_output.strip())
        additional_context = (
            response
            .get("hookSpecificOutput", {})
            .get("additionalContext", "")
        )
        self.assertIn(
            "warming up",
            additional_context,
            "SOFT 'warming up' text must appear in additionalContext during cold-start window",
        )
        self.assertNotIn(
            "⚠️ ENGRAM CRITICAL",
            additional_context,
            "CRITICAL warning must NOT fire during the cold-start warmup window",
        )


class TestCriticalWhenLaunchAttemptStale(unittest.TestCase):
    """CRITICAL fires when daemon-launch-attempt stamp is stale (outside warmup window)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_stale_stamp_test_")
        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"primary_user": "Alex"}, f)
        self.hook = _load_hook_module(self.tmp)
        # #2086 self-guard: hook.main() reads/writes many ENGRAM_HOME-derived
        # constants (COUNTER_PATH, PRINCIPLE_TRIGGER_STATE_PATH, ...) that a
        # post-import `hook.ENGRAM_HOME = self.tmp` reassignment does NOT
        # redirect (they're frozen strings computed once at import). Assert
        # one of them here so a regression to post-import patching fails
        # loudly instead of silently reading/writing real ~/.engram state.
        self.assertEqual(
            self.hook.PRINCIPLE_TRIGGER_STATE_PATH,
            os.path.join(self.tmp, "principle-trigger-state.json"),
            "PRINCIPLE_TRIGGER_STATE_PATH did not resolve against the "
            "test's isolated ENGRAM_HOME -- the env-patch-before-import "
            "fix for #2086 may have regressed.",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_critical_when_launch_attempt_stale(self):
        """CRITICAL fires (not SOFT) when daemon-launch-attempt is older than the warmup window."""
        import time
        # Write a stale launch-attempt marker (200 seconds in the past, well outside the 120s window)
        marker_path = os.path.join(self.tmp, "daemon-launch-attempt")
        with open(marker_path, "w") as f:
            f.write(str(int(time.time()) - 200))

        raw_output = _run_main_capture(self.hook, _make_hook_input())
        self.assertTrue(
            raw_output.strip(),
            "main() produced no stdout — expected a JSON response with CRITICAL warning",
        )
        response = json.loads(raw_output.strip())
        additional_context = (
            response
            .get("hookSpecificOutput", {})
            .get("additionalContext", "")
        )
        self.assertIn(
            "⚠️ ENGRAM CRITICAL",
            additional_context,
            "CRITICAL warning must fire when launch-attempt stamp is stale",
        )
        self.assertNotIn(
            "warming up",
            additional_context,
            "SOFT 'warming up' text must NOT appear when launch-attempt is stale",
        )


class TestIdleShutdownTombstone(unittest.TestCase):
    """Tombstone present + recent → SOFT idle-shutdown message (#1260)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_idle_tombstone_test_")
        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"primary_user": "Alex"}, f)
        self.hook = _load_hook_module(self.tmp)
        # #2086 self-guard: hook.main() reads/writes many ENGRAM_HOME-derived
        # constants (COUNTER_PATH, PRINCIPLE_TRIGGER_STATE_PATH, ...) that a
        # post-import `hook.ENGRAM_HOME = self.tmp` reassignment does NOT
        # redirect (they're frozen strings computed once at import). Assert
        # one of them here so a regression to post-import patching fails
        # loudly instead of silently reading/writing real ~/.engram state.
        self.assertEqual(
            self.hook.PRINCIPLE_TRIGGER_STATE_PATH,
            os.path.join(self.tmp, "principle-trigger-state.json"),
            "PRINCIPLE_TRIGGER_STATE_PATH did not resolve against the "
            "test's isolated ENGRAM_HOME -- the env-patch-before-import "
            "fix for #2086 may have regressed.",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_idle_shutdown_tombstone_gives_soft_not_critical(self):
        """Offline + recent tombstone → SOFT idle-shutdown message, NOT CRITICAL."""
        import time
        # Write a fresh idle-shutdown tombstone
        tombstone_path = os.path.join(self.tmp, "daemon-idle-shutdown")
        with open(tombstone_path, "w") as f:
            f.write(str(int(time.time())))

        raw_output = _run_main_capture(self.hook, _make_hook_input())
        self.assertTrue(raw_output.strip(), "main() produced no stdout")
        response = json.loads(raw_output.strip())
        additional_context = (
            response
            .get("hookSpecificOutput", {})
            .get("additionalContext", "")
        )
        self.assertIn(
            "idle-shutdown",
            additional_context,
            "SOFT idle-shutdown message must appear when tombstone is present and recent",
        )
        self.assertNotIn(
            "⚠️ ENGRAM CRITICAL",
            additional_context,
            "CRITICAL warning must NOT fire when idle-shutdown tombstone is present and recent",
        )


class TestCriticalWhenTombstoneAbsent(unittest.TestCase):
    """Tombstone absent → CRITICAL fires (#1260)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_no_tombstone_test_")
        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"primary_user": "Alex"}, f)
        self.hook = _load_hook_module(self.tmp)
        # #2086 self-guard: hook.main() reads/writes many ENGRAM_HOME-derived
        # constants (COUNTER_PATH, PRINCIPLE_TRIGGER_STATE_PATH, ...) that a
        # post-import `hook.ENGRAM_HOME = self.tmp` reassignment does NOT
        # redirect (they're frozen strings computed once at import). Assert
        # one of them here so a regression to post-import patching fails
        # loudly instead of silently reading/writing real ~/.engram state.
        self.assertEqual(
            self.hook.PRINCIPLE_TRIGGER_STATE_PATH,
            os.path.join(self.tmp, "principle-trigger-state.json"),
            "PRINCIPLE_TRIGGER_STATE_PATH did not resolve against the "
            "test's isolated ENGRAM_HOME -- the env-patch-before-import "
            "fix for #2086 may have regressed.",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_critical_fires_when_tombstone_absent(self):
        """Offline + no tombstone file → CRITICAL warning fires."""
        # No tombstone written — clean temp dir
        raw_output = _run_main_capture(self.hook, _make_hook_input())
        self.assertTrue(raw_output.strip(), "main() produced no stdout")
        response = json.loads(raw_output.strip())
        additional_context = (
            response
            .get("hookSpecificOutput", {})
            .get("additionalContext", "")
        )
        self.assertIn(
            "⚠️ ENGRAM CRITICAL",
            additional_context,
            "CRITICAL warning must fire when no tombstone is present",
        )
        self.assertNotIn(
            "idle-shutdown",
            additional_context,
            "SOFT idle-shutdown message must NOT appear when no tombstone is present",
        )


class TestStaleTombstone(unittest.TestCase):
    """Tombstone present but older than 48h → CRITICAL fires (#1260)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_stale_tombstone_test_")
        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"primary_user": "Alex"}, f)
        self.hook = _load_hook_module(self.tmp)
        # #2086 self-guard: hook.main() reads/writes many ENGRAM_HOME-derived
        # constants (COUNTER_PATH, PRINCIPLE_TRIGGER_STATE_PATH, ...) that a
        # post-import `hook.ENGRAM_HOME = self.tmp` reassignment does NOT
        # redirect (they're frozen strings computed once at import). Assert
        # one of them here so a regression to post-import patching fails
        # loudly instead of silently reading/writing real ~/.engram state.
        self.assertEqual(
            self.hook.PRINCIPLE_TRIGGER_STATE_PATH,
            os.path.join(self.tmp, "principle-trigger-state.json"),
            "PRINCIPLE_TRIGGER_STATE_PATH did not resolve against the "
            "test's isolated ENGRAM_HOME -- the env-patch-before-import "
            "fix for #2086 may have regressed.",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_critical_fires_when_tombstone_stale(self):
        """Offline + tombstone older than 48h → CRITICAL (stale tombstone ignored)."""
        import time
        tombstone_path = os.path.join(self.tmp, "daemon-idle-shutdown")
        # 48h + 1s past the validity window
        with open(tombstone_path, "w") as f:
            f.write(str(int(time.time()) - 172801))

        raw_output = _run_main_capture(self.hook, _make_hook_input())
        self.assertTrue(raw_output.strip(), "main() produced no stdout")
        response = json.loads(raw_output.strip())
        additional_context = (
            response
            .get("hookSpecificOutput", {})
            .get("additionalContext", "")
        )
        self.assertIn(
            "⚠️ ENGRAM CRITICAL",
            additional_context,
            "CRITICAL must fire when tombstone is stale (> 48h)",
        )
        self.assertNotIn(
            "idle-shutdown",
            additional_context,
            "SOFT idle-shutdown message must NOT appear for a stale tombstone",
        )


class TestCorruptTombstone(unittest.TestCase):
    """Tombstone present but with non-integer content → CRITICAL fires (#1260)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_corrupt_tombstone_test_")
        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"primary_user": "Alex"}, f)
        self.hook = _load_hook_module(self.tmp)
        # #2086 self-guard: hook.main() reads/writes many ENGRAM_HOME-derived
        # constants (COUNTER_PATH, PRINCIPLE_TRIGGER_STATE_PATH, ...) that a
        # post-import `hook.ENGRAM_HOME = self.tmp` reassignment does NOT
        # redirect (they're frozen strings computed once at import). Assert
        # one of them here so a regression to post-import patching fails
        # loudly instead of silently reading/writing real ~/.engram state.
        self.assertEqual(
            self.hook.PRINCIPLE_TRIGGER_STATE_PATH,
            os.path.join(self.tmp, "principle-trigger-state.json"),
            "PRINCIPLE_TRIGGER_STATE_PATH did not resolve against the "
            "test's isolated ENGRAM_HOME -- the env-patch-before-import "
            "fix for #2086 may have regressed.",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_critical_fires_when_tombstone_corrupt(self):
        """Offline + tombstone with non-integer content → CRITICAL (int() raises, falls through)."""
        tombstone_path = os.path.join(self.tmp, "daemon-idle-shutdown")
        with open(tombstone_path, "w") as f:
            f.write("not-an-int")

        raw_output = _run_main_capture(self.hook, _make_hook_input())
        self.assertTrue(raw_output.strip(), "main() produced no stdout")
        response = json.loads(raw_output.strip())
        additional_context = (
            response
            .get("hookSpecificOutput", {})
            .get("additionalContext", "")
        )
        self.assertIn(
            "⚠️ ENGRAM CRITICAL",
            additional_context,
            "CRITICAL must fire when tombstone content is corrupt (non-integer)",
        )
        self.assertNotIn(
            "idle-shutdown",
            additional_context,
            "SOFT idle-shutdown message must NOT appear for a corrupt tombstone",
        )


if __name__ == "__main__":
    unittest.main()
