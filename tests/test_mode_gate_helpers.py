"""Tests for engram_client.is_multi_agent_mode() and get_counterparts().

PR 1 of the inter-agent-comms-v1 cohort adds two module-level helpers to
engram_client.py that read `mode` and `counterparts` from config.json.

These tests verify:
  - mode=single → is_multi_agent_mode() returns False
  - mode=multi  → is_multi_agent_mode() returns True
  - mode field absent → is_multi_agent_mode() returns False (safe default)
  - mode=single → get_counterparts() returns []
  - mode=multi with counterparts → get_counterparts() returns the list
  - counterparts field absent → get_counterparts() returns []
  - config.json absent → both helpers return their defaults (False / [])
  - config.json malformed JSON → both helpers return their defaults

Import-pattern contract (#948 wave C — R1):
    This file imports engram_core and engram_client directly and contains no
    ``import server``. _configure_paths and _ensure_data_dir are defined in
    engram_core; server.py wraps them via thin forwarding functions.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import engram_client
import engram_core


class TestIsMultiAgentMode(unittest.TestCase):
    """Tests for engram_client.is_multi_agent_mode()."""

    def _write_config(self, tmp_dir: Path, data: dict) -> Path:
        """Write data to config.json in tmp_dir and return the dir path."""
        (tmp_dir / "config.json").write_text(json.dumps(data))
        return tmp_dir

    def test_single_mode_returns_false(self):
        """mode='single' → False."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(Path(tmp), {"mode": "single"})
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertFalse(engram_client.is_multi_agent_mode())

    def test_multi_mode_returns_true(self):
        """mode='multi' → True."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(Path(tmp), {"mode": "multi", "counterparts": ["ariadne"]})
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertTrue(engram_client.is_multi_agent_mode())

    def test_missing_mode_field_returns_false(self):
        """config.json present but no `mode` key → default is False (single)."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(Path(tmp), {"schema_version": 2, "trust_pool": []})
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertFalse(engram_client.is_multi_agent_mode())

    def test_absent_config_returns_false(self):
        """config.json does not exist → False (safe default)."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertFalse(engram_client.is_multi_agent_mode())

    def test_malformed_config_returns_false(self):
        """config.json contains invalid JSON → False (safe default, no crash)."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.json").write_text("{ not valid json [")
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertFalse(engram_client.is_multi_agent_mode())

    def test_unknown_mode_value_returns_false(self):
        """mode='unknown-value' → False (only 'multi' is True)."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(Path(tmp), {"mode": "cluster"})
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertFalse(engram_client.is_multi_agent_mode())

    def test_mode_capital_M_returns_false(self):
        """mode='Multi' (wrong case) → False (comparison is case-sensitive)."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(Path(tmp), {"mode": "Multi"})
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertFalse(engram_client.is_multi_agent_mode())

    def test_mode_boolean_true_returns_false(self):
        """mode=True (boolean, not string) → False (type mismatch, not 'multi')."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(Path(tmp), {"mode": True})
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertFalse(engram_client.is_multi_agent_mode())


class TestGetCounterparts(unittest.TestCase):
    """Tests for engram_client.get_counterparts()."""

    def _write_config(self, tmp_dir: Path, data: dict) -> Path:
        (tmp_dir / "config.json").write_text(json.dumps(data))
        return tmp_dir

    def test_single_mode_returns_empty_list(self):
        """mode='single', no counterparts → []."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(Path(tmp), {"mode": "single", "counterparts": []})
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertEqual(engram_client.get_counterparts(), [])

    def test_multi_mode_returns_counterparts_list(self):
        """mode='multi' with counterparts=['ariadne'] → ['ariadne']."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(Path(tmp), {"mode": "multi", "counterparts": ["ariadne"]})
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertEqual(engram_client.get_counterparts(), ["ariadne"])

    def test_multi_mode_multiple_counterparts(self):
        """mode='multi' with multiple counterparts → full list."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(Path(tmp), {
                "mode": "multi",
                "counterparts": ["ariadne", "mneme"],
            })
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                result = engram_client.get_counterparts()
                self.assertEqual(result, ["ariadne", "mneme"])

    def test_missing_counterparts_field_returns_empty_list(self):
        """config.json present but no `counterparts` key → []."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(Path(tmp), {"mode": "multi"})
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertEqual(engram_client.get_counterparts(), [])

    def test_absent_config_returns_empty_list(self):
        """config.json does not exist → [] (safe default)."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertEqual(engram_client.get_counterparts(), [])

    def test_malformed_config_returns_empty_list(self):
        """config.json invalid JSON → [] (safe default, no crash)."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.json").write_text("{ not valid json [")
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertEqual(engram_client.get_counterparts(), [])


class TestDefaultConfigHasNewFields(unittest.TestCase):
    """Verify engram_core's _ensure_data_dir() writes mode + counterparts defaults."""

    def test_fresh_config_has_mode_single(self):
        """A freshly created config.json contains mode='single'."""
        with tempfile.TemporaryDirectory() as tmp:
            engram_core._configure_paths(tmp)
            # _ensure_data_dir creates config.json when it doesn't exist.
            engram_core._ensure_data_dir()
            config_path = Path(tmp) / "config.json"
            self.assertTrue(config_path.exists())
            data = json.loads(config_path.read_text())
            self.assertEqual(data.get("mode"), "single")

    def test_fresh_config_has_empty_counterparts(self):
        """A freshly created config.json contains counterparts=[]."""
        with tempfile.TemporaryDirectory() as tmp:
            engram_core._configure_paths(tmp)
            engram_core._ensure_data_dir()
            config_path = Path(tmp) / "config.json"
            data = json.loads(config_path.read_text())
            self.assertEqual(data.get("counterparts"), [])


if __name__ == "__main__":
    unittest.main()
