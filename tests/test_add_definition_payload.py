"""Tests for engram_add_definition single-payload signature (wave 2c-i of ob_1499).

Coverage:
  1. Happy path — well-formed payload creates a definition.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing term / definition rejected.
  4. Internal helper — _add_definition_impl works with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_definition_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
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


def test_payload_json_creates_definition():
    server = fresh_server()
    payload = {
        "term": "recession",
        "definition": "Two consecutive quarters of negative GDP growth.",
    }
    r = json.loads(server.engram_add_definition(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "definition_id" in r and r["definition_id"].startswith("df_"), r


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_definition(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_definition(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {"term": "x", "definition": "y", "bogus": "reject"}
    r = json.loads(server.engram_add_definition(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus" in r["error"]


def test_payload_json_missing_term_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_definition(payload_json=json.dumps({"definition": "y"})))
    assert "error" in r and "term" in r["error"], r


def test_payload_json_missing_definition_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_definition(payload_json=json.dumps({"term": "x"})))
    assert "error" in r and "definition" in r["error"], r


def test_impl_callable_with_kwargs():
    server = fresh_server()
    r = json.loads(server._add_definition_impl(
        term="kwargs test",
        definition="testing _impl is callable.",
    ))
    assert "error" not in r, r
    assert "definition_id" in r
