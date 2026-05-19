"""Tests for the drowsiness-display-levels change (cj_0054 / 2026-05-12).

Per Lei's design directive: hide raw drowsiness percentages below the urgent
threshold; show level words only ("refreshed" / "energetic" / "a little
drowsy"). At or above urgent, render the raw percentage. Thresholds are
configurable per context-window mode (200K vs 1M defaults differ; 200K
needs more buffer for nap headroom).

These tests exercise the level-mapping function and the format_drowsiness
renderer at every threshold boundary, for both 1M and 200K mode defaults.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Make the hook module importable.
HOOKS_DIR = Path(__file__).parent.parent / "hooks" / "claude"
sys.path.insert(0, str(HOOKS_DIR))

import context_tracker as ct


class TestDrowsinessLevelMapping(unittest.TestCase):
    """Boundary tests for drowsiness_level()."""

    def setUp(self):
        self.th_1m = ct.DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE["1m"]
        self.th_200k = ct.DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE["200k"]

    def test_1m_refreshed_range(self):
        """1M: 0.00–0.4999 should map to 'refreshed'."""
        for pct in [0.0, 0.10, 0.30, 0.49, 0.4999]:
            self.assertEqual(ct.drowsiness_level(pct, self.th_1m), "refreshed",
                             msg=f"pct={pct} should be 'refreshed' in 1m mode")

    def test_1m_energetic_range(self):
        """1M: 0.50–0.7999 should map to 'energetic'."""
        for pct in [0.50, 0.65, 0.75, 0.79, 0.7999]:
            self.assertEqual(ct.drowsiness_level(pct, self.th_1m), "energetic",
                             msg=f"pct={pct} should be 'energetic' in 1m mode")

    def test_1m_a_little_drowsy_range(self):
        """1M: 0.80–0.8999 should map to 'a little drowsy'."""
        for pct in [0.80, 0.85, 0.89, 0.8999]:
            self.assertEqual(ct.drowsiness_level(pct, self.th_1m), "a little drowsy",
                             msg=f"pct={pct} should be 'a little drowsy' in 1m mode")

    def test_1m_needs_a_nap_range(self):
        """1M: 0.90+ should map to 'needs a nap'."""
        for pct in [0.90, 0.95, 0.99, 1.0, 1.5]:
            self.assertEqual(ct.drowsiness_level(pct, self.th_1m), "needs a nap",
                             msg=f"pct={pct} should be 'needs a nap' in 1m mode")

    def test_200k_thresholds_lower_than_1m(self):
        """200K mode urgent threshold is below 1M's, leaving more nap headroom."""
        self.assertLess(
            self.th_200k["urgent_at_or_above"],
            self.th_1m["urgent_at_or_above"],
            "200K should reach urgent earlier (lower pct) than 1M for nap-headroom",
        )

    def test_200k_boundary_85_is_needs_nap(self):
        """200K: 0.85+ should be 'needs a nap' (urgent threshold is 0.85, not 0.90)."""
        self.assertEqual(ct.drowsiness_level(0.85, self.th_200k), "needs a nap")
        # And 0.84 should still be 'a little drowsy'
        self.assertEqual(ct.drowsiness_level(0.84, self.th_200k), "a little drowsy")

    def test_custom_thresholds(self):
        """drowsiness_level honours custom threshold dicts."""
        custom = {
            "refreshed_below":       0.30,
            "energetic_below":       0.60,
            "a_little_drowsy_below": 0.80,
            "urgent_at_or_above":    0.80,
        }
        self.assertEqual(ct.drowsiness_level(0.29, custom), "refreshed")
        self.assertEqual(ct.drowsiness_level(0.30, custom), "energetic")
        self.assertEqual(ct.drowsiness_level(0.59, custom), "energetic")
        self.assertEqual(ct.drowsiness_level(0.60, custom), "a little drowsy")
        self.assertEqual(ct.drowsiness_level(0.79, custom), "a little drowsy")
        self.assertEqual(ct.drowsiness_level(0.80, custom), "needs a nap")


class TestFormatDrowsinessOutput(unittest.TestCase):
    """End-to-end tests for format_drowsiness()."""

    def _usage(self, pct, mode="1m"):
        limit = 807_500 if mode == "1m" else 152_000
        return {
            "usage_pct": pct,
            "estimated_tokens": int(limit * pct),
            "context_limit": limit,
            "mode": mode,
        }

    @patch("context_tracker.os.path.exists")
    def test_refreshed_hides_percentage(self, exists_mock):
        """Below the refreshed_below threshold, only the level word should appear."""
        exists_mock.return_value = False  # no loop-mode marker
        out = ct.format_drowsiness(self._usage(0.30, mode="1m"))
        self.assertIn("refreshed", out)
        self.assertNotIn("30%", out)
        self.assertNotIn("0.30", out)

    @patch("context_tracker.os.path.exists")
    def test_energetic_hides_percentage(self, exists_mock):
        """Below energetic_below, only the level word should appear."""
        exists_mock.return_value = False
        out = ct.format_drowsiness(self._usage(0.65, mode="1m"))
        self.assertIn("energetic", out)
        self.assertNotIn("65%", out)

    @patch("context_tracker.os.path.exists")
    def test_a_little_drowsy_has_level_plus_warning(self, exists_mock):
        """At a_little_drowsy threshold, level word + warning text (no percentage)."""
        exists_mock.return_value = False
        defaults_1m = ct.DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE["1m"]
        with patch.object(ct, "_load_drowsiness_thresholds", return_value=defaults_1m):
            out = ct.format_drowsiness(self._usage(0.85, mode="1m"))
        self.assertIn("a little drowsy", out)
        self.assertIn("Plan a nap soon", out)
        self.assertNotIn("85%", out)

    @patch("context_tracker.os.path.exists")
    def test_needs_a_nap_shows_percentage(self, exists_mock):
        """At urgent threshold, percentage is shown explicitly (calibration signal)."""
        exists_mock.return_value = False
        defaults_1m = ct.DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE["1m"]
        with patch.object(ct, "_load_drowsiness_thresholds", return_value=defaults_1m):
            out = ct.format_drowsiness(self._usage(0.92, mode="1m"))
        self.assertIn("needs a nap", out)
        self.assertIn("92%", out)
        self.assertIn("Stop current work", out)

    @patch("context_tracker.os.path.exists")
    def test_200k_mode_urgent_at_85(self, exists_mock):
        """200K mode: urgent fires at 85% not 90% (more nap-headroom buffer)."""
        exists_mock.return_value = False
        out = ct.format_drowsiness(self._usage(0.85, mode="200k"))
        self.assertIn("needs a nap", out)
        self.assertIn("85%", out)

    @patch("context_tracker.os.path.exists")
    def test_loop_mode_below_urgent_hides_percentage(self, exists_mock):
        """Loop mode + below urgent: level word, no percentage."""
        exists_mock.return_value = True  # loop-mode marker exists
        out = ct.format_drowsiness(self._usage(0.65, mode="1m"))
        self.assertIn("energetic", out)
        self.assertIn("loop mode", out)
        self.assertNotIn("65%", out)

    @patch("context_tracker.os.path.exists")
    def test_loop_mode_at_urgent_shows_percentage(self, exists_mock):
        """Loop mode + urgent: percentage shown (so the agent sees calibration)."""
        exists_mock.return_value = True
        defaults_1m = ct.DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE["1m"]
        with patch.object(ct, "_load_drowsiness_thresholds", return_value=defaults_1m):
            out = ct.format_drowsiness(self._usage(0.93, mode="1m"))
        self.assertIn("needs a nap", out)
        self.assertIn("93%", out)
        self.assertIn("loop mode", out)

    @patch("context_tracker.os.path.exists")
    def test_200k_a_little_drowsy_band(self, exists_mock):
        """200K mode: 0.70–0.8499 should render as 'a little drowsy' + warning."""
        exists_mock.return_value = False
        for pct in [0.70, 0.75, 0.80, 0.84]:
            usage = self._usage(pct, mode="200k")
            out = ct.format_drowsiness(usage)
            self.assertIn("a little drowsy", out, msg=f"pct={pct} 200K should be 'a little drowsy'")
            self.assertIn("Plan a nap soon", out, msg=f"pct={pct} 200K should warn 'Plan a nap soon'")
            self.assertNotIn(f"{int(pct*100)}%", out, msg=f"pct={pct} 200K should hide raw percentage")

    @patch("context_tracker.os.path.exists")
    def test_partial_config_gap_band_still_warns(self, exists_mock):
        """Round-2 blocker fix: when urgent > a_little_drowsy_below (partial config),
        the gap band must still fire the drowsy warning, not silently fall through
        to a bare level-word line. Round-3 label-accuracy fix: the banner shows
        the computed level word ('needs a nap' in this scenario, because pct=0.92
        is at or above a_little_drowsy_below=0.90), not a hardcoded string."""
        exists_mock.return_value = False
        # Simulate config that pushed urgent up to 0.95 but left a_little_drowsy at default 0.90.
        # At pct=0.92: urgent=False (<0.95), level="needs a nap" (>=0.90), so the
        # banner reads "needs a nap" with the drowsy-tier warning (not the urgent
        # multi-line block).
        mismatched_thresholds = {
            "refreshed_below":       0.50,
            "energetic_below":       0.80,
            "a_little_drowsy_below": 0.90,
            "urgent_at_or_above":    0.95,  # pushed up via partial config
        }
        with patch.object(ct, "_load_drowsiness_thresholds", return_value=mismatched_thresholds):
            usage = self._usage(0.92, mode="1m")
            out = ct.format_drowsiness(usage)
        self.assertIn("needs a nap", out, msg="Banner should reflect the computed level (round-3 label-accuracy fix)")
        self.assertIn("Plan a nap soon", out, msg="Gap-band must still fire drowsy warning, not swallow it silently")
        self.assertNotIn("Stop current work", out, msg="Should not fire urgent warning yet — we're below urgent_at_or_above")
        self.assertNotIn("92%", out, msg="Raw percentage should still be hidden below urgent_at_or_above")


class TestConfigOverride(unittest.TestCase):
    """Tests that config.json thresholds override defaults."""

    def test_config_override_replaces_defaults(self):
        """Custom thresholds in config.json should be used; defaults filled in for missing keys."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w") as f:
                json.dump(
                    {
                        "drowsiness_display": {
                            "1m": {
                                "refreshed_below": 0.40,
                                # energetic_below + a_little_drowsy_below + urgent_at_or_above
                                # left out — should fall back to defaults
                            }
                        }
                    },
                    f,
                )
            with patch.object(ct, "ENGRAM_HOME", tmp):
                th = ct._load_drowsiness_thresholds("1m")
            self.assertEqual(th["refreshed_below"], 0.40)
            self.assertEqual(th["energetic_below"], 0.80)  # default
            self.assertEqual(th["a_little_drowsy_below"], 0.90)  # default
            self.assertEqual(th["urgent_at_or_above"], 0.90)  # default

    def test_missing_config_returns_defaults(self):
        """No config.json → defaults."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ct, "ENGRAM_HOME", tmp):
                th = ct._load_drowsiness_thresholds("1m")
            self.assertEqual(th, ct.DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE["1m"])

    def test_malformed_config_returns_defaults(self):
        """Malformed config.json → defaults (no exception)."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w") as f:
                f.write("{ this is not json")
            with patch.object(ct, "ENGRAM_HOME", tmp):
                th = ct._load_drowsiness_thresholds("1m")
            self.assertEqual(th, ct.DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE["1m"])


if __name__ == "__main__":
    unittest.main()
