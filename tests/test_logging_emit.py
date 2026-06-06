"""Unit tests for the round-1 server.py logging helpers.

Covers:
  _sanitize_params   — private-key stripping
  _log_engram_tool_event — fire-and-forget under DB failure
  _extract_result_status — non-dict / non-JSON return handling

These helpers are defined in server.py around lines 3998–4119 and are the
emission path for engram.tool.engram_call events introduced in PR #238.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Project root is the parent of tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server

import engram_core
class TestSanitizeParamsStripsPrivateKeys(unittest.TestCase):
    """_sanitize_params must remove all _PAYLOAD_PRIVATE_KEYS and keep others."""

    def test_strips_all_private_keys_keeps_public(self):
        payload = {
            "quoted_text": "some secret quote",
            "interpretation": "my interpretation",
            "claim": "the main claim text",
            "logical_chain": "step 1 -> step 2",
            "top_k": 10,
        }
        result = server._sanitize_params(payload)
        # Private keys must be absent
        self.assertNotIn("quoted_text", result)
        self.assertNotIn("interpretation", result)
        self.assertNotIn("claim", result)
        self.assertNotIn("logical_chain", result)
        # Public key must survive
        self.assertIn("top_k", result)
        self.assertEqual(result["top_k"], 10)

    def test_accepts_json_string_input(self):
        payload_json = json.dumps({"claim": "secret", "node_id": "ob_0001"})
        result = server._sanitize_params(payload_json)
        self.assertNotIn("claim", result)
        self.assertIn("node_id", result)

    def test_returns_empty_dict_on_non_dict_non_string(self):
        self.assertEqual(server._sanitize_params(None), {})
        self.assertEqual(server._sanitize_params(42), {})
        self.assertEqual(server._sanitize_params([1, 2, 3]), {})

    def test_returns_empty_dict_on_malformed_json_string(self):
        self.assertEqual(server._sanitize_params("{not valid json"), {})

    def test_empty_payload_returns_empty_dict(self):
        self.assertEqual(server._sanitize_params({}), {})


class TestLogEngramToolEventFireAndForget(unittest.TestCase):
    """_log_engram_tool_event must swallow all exceptions — never propagate."""

    def test_no_exception_when_index_db_does_not_exist(self):
        """If index.db is missing, the function returns None silently."""
        with tempfile.TemporaryDirectory(prefix="engram_test_nodb_") as tmpdir:
            # Point DATA_DIR at a dir where logs/index.db does NOT exist
            original_data_dir = engram_core.DATA_DIR
            engram_core.DATA_DIR = Path(tmpdir)
            try:
                result = server._log_engram_tool_event(
                    tool_name="engram_add_observation",
                    duration_ms=42,
                    result_status="created",
                    params_dict={"node_type": "observation"},
                )
                # Fire-and-forget: must return None, no exception
                self.assertIsNone(result)
            finally:
                engram_core.DATA_DIR = original_data_dir

    def test_no_exception_when_sqlite_connect_raises(self):
        """If sqlite3.connect fails (e.g. permission denied), no exception propagates."""
        import sqlite3 as _sqlite3

        with tempfile.TemporaryDirectory(prefix="engram_test_dbfail_") as tmpdir:
            # Create the logs/index.db path so existence check passes
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            (logs_dir / "index.db").touch()

            original_data_dir = engram_core.DATA_DIR
            engram_core.DATA_DIR = Path(tmpdir)
            try:
                with patch.object(
                    _sqlite3, "connect", side_effect=OSError("permission denied")
                ):
                    result = server._log_engram_tool_event(
                        tool_name="engram_derive",
                        duration_ms=100,
                        result_status="created",
                        params_dict={"reasoning_type": "inductive"},
                    )
                    self.assertIsNone(result)
            finally:
                engram_core.DATA_DIR = original_data_dir


class TestExtractResultStatusHandlesNonDictReturns(unittest.TestCase):
    """_extract_result_status must never raise and handle all edge cases."""

    def test_returns_status_from_json_dict_with_status_key(self):
        return_value = json.dumps({"status": "created", "node_id": "ob_0001"})
        result = server._extract_result_status(return_value)
        self.assertEqual(result, "created")

    def test_returns_none_for_json_dict_without_status_key(self):
        return_value = json.dumps({"node_id": "ob_0001", "message": "ok"})
        result = server._extract_result_status(return_value)
        self.assertIsNone(result)

    def test_returns_none_for_non_json_string(self):
        result = server._extract_result_status("not valid json {{{")
        self.assertIsNone(result)

    def test_returns_none_for_none(self):
        result = server._extract_result_status(None)
        self.assertIsNone(result)

    def test_returns_none_for_empty_string(self):
        result = server._extract_result_status("")
        self.assertIsNone(result)

    def test_returns_none_for_json_non_dict(self):
        # A JSON array — not a dict, so no "status" key
        result = server._extract_result_status(json.dumps(["created"]))
        self.assertIsNone(result)

    def test_status_value_coerced_to_str(self):
        # Status should be returned as a string even if stored as int
        return_value = json.dumps({"status": 200})
        result = server._extract_result_status(return_value)
        self.assertEqual(result, "200")


if __name__ == "__main__":
    unittest.main()
