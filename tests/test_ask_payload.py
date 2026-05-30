"""Tests for engram_ask single-payload signature
(wave 3 of ob_1499 — issue #99).

The migration unifies the 4-arg surface (question, context_ids, category,
lacks) into one payload_json object. Same pattern as PR #110 (derive),
PR #114 (update_task).

Coverage:
  1. Happy path — well-formed payload registers a question.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing question rejected.
  4. Category / lacks validation.
  5. Internal helper — _ask_impl callable with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_ask_")
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


def test_payload_json_creates_question():
    server = fresh_server()
    payload = {
        "question": "Why does the bootstrap path skip embedding generation?",
        "category": "implementation",
        "lacks": "implementation",
    }
    r = json.loads(server.engram_ask(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("status") == "created"
    assert r.get("question_id", "").startswith("qu_"), r
    assert r.get("category") == "implementation"
    assert r.get("lacks") == "implementation"


def test_payload_json_question_only_works():
    server = fresh_server()
    payload = {"question": "What's the minimal question payload?"}
    r = json.loads(server.engram_ask(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("question_id", "").startswith("qu_"), r


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_ask(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_ask(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {"question": "x", "bogus_field": "reject me"}
    r = json.loads(server.engram_ask(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


def test_payload_json_missing_question_rejected():
    server = fresh_server()
    r = json.loads(server.engram_ask(payload_json=json.dumps({"category": "research"})))
    assert "error" in r and "question" in r["error"], r


def test_payload_json_invalid_category_rejected():
    server = fresh_server()
    payload = {"question": "x", "category": "not_a_category"}
    r = json.loads(server.engram_ask(payload_json=json.dumps(payload)))
    assert "error" in r and "Invalid category" in r["error"], r


def test_payload_json_invalid_lacks_rejected():
    server = fresh_server()
    payload = {"question": "x", "lacks": "not_a_lack"}
    r = json.loads(server.engram_ask(payload_json=json.dumps(payload)))
    assert "error" in r and "Invalid lacks" in r["error"], r


def test_impl_callable_with_kwargs():
    """In-server callers should be able to call _ask_impl directly."""
    server = fresh_server()
    r = json.loads(server._ask_impl(
        question="Internal kwargs path test.",
        category="meta",
    ))
    assert "error" not in r, r
    assert r.get("question_id", "").startswith("qu_"), r
    assert r.get("category") == "meta"
