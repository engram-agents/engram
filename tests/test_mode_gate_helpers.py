"""Tests for engram_client.is_forum_configured().

#4255 slice 2 retired is_multi_agent_mode() / get_counterparts() and the `mode`
and `counterparts` default-config fields; their tests are gone with them (the
absence is asserted in tests/test_retire_runtime_keys_4255s2.py). What remains is
the forum-configured gate, which never depended on `mode`.

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


class TestIsForumConfigured(unittest.TestCase):
    """Tests for engram_client.is_forum_configured() (#2857 step 1).

    Parallel to TestIsMultiAgentMode above, but the predicate is entirely
    different: forum.url set (agent_name alone does not count, #3552),
    independent of the mode field.
    """

    def _write_config(self, tmp_dir: Path, data: dict) -> Path:
        (tmp_dir / "config.json").write_text(json.dumps(data))
        return tmp_dir

    def test_forum_url_set_returns_true(self):
        """forum.url set, mode='single' → True (mode is irrelevant)."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(
                Path(tmp), {"mode": "single", "forum": {"url": "http://x:5002"}}
            )
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertTrue(engram_client.is_forum_configured())

    def test_agent_name_set_returns_true(self):
        """agent_name AND forum.url set, mode='single' → True (mode is irrelevant)."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(
                Path(tmp),
                {"mode": "single", "agent_name": "ariadne",
                 "forum": {"url": "http://x:5002"}},
            )
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertTrue(engram_client.is_forum_configured())

    def test_multi_mode_without_forum_or_agent_name_returns_false(self):
        """mode='multi' alone, no forum.url → False."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(Path(tmp), {"mode": "multi"})
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertFalse(engram_client.is_forum_configured())

    def test_absent_config_returns_false(self):
        """config.json does not exist → False (safe default)."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertFalse(engram_client.is_forum_configured())

    def test_malformed_config_returns_false(self):
        """config.json contains invalid JSON → False (safe default, no crash)."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.json").write_text("{ not valid json [")
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertFalse(engram_client.is_forum_configured())

    def test_empty_forum_url_string_returns_false(self):
        """forum.url present but blank/whitespace → False."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(Path(tmp), {"forum": {"url": "   "}})
            with patch.dict(os.environ, {"ENGRAM_HOME": tmp}):
                self.assertFalse(engram_client.is_forum_configured())


if __name__ == "__main__":
    unittest.main()
