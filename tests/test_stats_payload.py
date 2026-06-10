"""MCP-contract tests for the engram_stats payload_json wrapper surface (#948 R2).

These exercise the server.py wrapper layer — payload_json parsing and argument
validation — which lives in the @mcp.tool() wrapper, NOT in engram_stats._stats_impl.
They are deliberately split out of tests/test_engram_stats_mode.py (which holds the
engram_stats family-internals as R1) because a file mixing internals + MCP-contract
tests is the signal to split (IMPORT_RUBRIC.md R2). This file keeps ``import server``
and the test_*_payload.py name so the audit gate classifies it as contract-tier.

Coverage (the wrapper-validation behaviors the family-internals layer cannot exercise):
  - malformed JSON payload → structured error
  - non-object (array) payload → structured error
  - unknown mode value → structured error
  - sections list with non-string elements → structured error
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_stats_payload_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Return a freshly initialised server module with an empty test DB."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def test_invalid_json_returns_error():
    """Malformed JSON payload returns a structured error."""
    server = fresh_server()
    r = json.loads(server.engram_stats("{not-json}"))
    assert "error" in r, r
    assert "Invalid JSON" in r["error"], r


def test_non_object_payload_returns_error():
    """Array payload returns a structured error."""
    server = fresh_server()
    r = json.loads(server.engram_stats("[1, 2, 3]"))
    assert "error" in r, r
    assert "JSON object" in r["error"], r


def test_invalid_mode_returns_error():
    """An unknown mode value returns a structured error."""
    server = fresh_server()
    r = json.loads(server.engram_stats('{"mode": "invalid"}'))
    assert "error" in r, r
    assert "mode" in r["error"].lower() or "Invalid" in r["error"], r


def test_sections_non_string_elements_rejected():
    """sections list with non-string elements must return an error."""
    server = fresh_server()
    r = json.loads(server.engram_stats('{"sections": [1, "structure"]}'))
    assert "error" in r, r
    assert "strings" in r["error"]
