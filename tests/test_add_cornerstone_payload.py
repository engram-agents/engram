"""Tests for engram_add_cornerstone single-payload signature (wave 2c-ii of ob_1499).

Coverage:
  1. Happy path — well-formed payload creates a cornerstone.
  2. Optional fields (prior_frame, triggering_experience, supporting_ids) respected.
  3. Schema enforcement — invalid JSON, non-object, unknown fields.
  4. Required-field validation — missing tag / title / new_frame rejected.
  5. Internal helper — _add_cornerstone_impl works with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_cornerstone_")
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


def test_payload_json_creates_cornerstone():
    server = fresh_server()
    payload = {
        "tag": "epistemic-identity",
        "title": "Knowing what I do not know",
        "new_frame": "Calibrate before claiming.",
    }
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "cornerstone_id" in r and r["cornerstone_id"].startswith("cs_"), r
    assert r["tag"] == "epistemic-identity"


def test_payload_json_with_optional_fields():
    server = fresh_server()
    payload = {
        "tag": "self-knowledge",
        "title": "Bold internally, careful externally",
        "new_frame": "Internal-bold / external-careful filter.",
        "prior_frame": "Wait-for-approval reflex.",
        "triggering_experience": "Lei flagged the regression 2026-05-06.",
    }
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r["title"] == "Bold internally, careful externally"


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_cornerstone(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_cornerstone(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {
        "tag": "x", "title": "y", "new_frame": "z", "bogus": "reject",
    }
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus" in r["error"]


def test_payload_json_missing_tag_rejected():
    server = fresh_server()
    payload = {"title": "y", "new_frame": "z"}
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "tag" in r["error"], r


def test_payload_json_missing_title_rejected():
    server = fresh_server()
    payload = {"tag": "x", "new_frame": "z"}
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "title" in r["error"], r


def test_payload_json_missing_new_frame_rejected():
    server = fresh_server()
    payload = {"tag": "x", "title": "y"}
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "new_frame" in r["error"], r


def test_payload_json_empty_string_tag_rejected():
    server = fresh_server()
    payload = {"tag": "  ", "title": "y", "new_frame": "z"}
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "tag" in r["error"], r


def test_impl_callable_with_kwargs():
    server = fresh_server()
    r = json.loads(server._add_cornerstone_impl(
        tag="test",
        title="Internal kwargs test",
        new_frame="testing _impl is callable.",
    ))
    assert "error" not in r, r
    assert "cornerstone_id" in r
