"""Tests for engram_add_conjecture single-payload signature (wave 2c-i of ob_1499).

Coverage:
  1. Happy path — well-formed payload creates a conjecture.
  2. Optional initial_confidence respected.
  3. Schema enforcement — invalid JSON, non-object, unknown fields.
  4. Required-field validation — missing claim / basis rejected.
  5. Internal helper — _add_conjecture_impl works with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_conjecture_")
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


def test_payload_json_creates_conjecture():
    server = fresh_server()
    payload = {
        "claim": "Structured memory substitutes for model scale.",
        "basis": "Working hypothesis from ob_0001 series.",
    }
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "conjecture_id" in r and r["conjecture_id"].startswith("cj_"), r


def test_payload_json_with_initial_confidence():
    server = fresh_server()
    payload = {
        "claim": "x",
        "basis": "y",
        "initial_confidence": 0.55,
    }
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("confidence") == 0.55, r


def test_payload_json_string_initial_confidence_coerced():
    """initial_confidence="0.55" (string) is coerced to float, not TypeError.

    Per PR #67 fairy round-1 blocker: payload_json may deliver initial_confidence
    as a string (json.loads preserves "0.55" as str), and **params splats it
    into _add_conjecture_impl unchanged. Without coercion the max/min compare
    raises TypeError. The fix coerces explicitly + returns clean error on bad input.
    """
    server = fresh_server()
    payload = {
        "claim": "x",
        "basis": "y",
        "initial_confidence": "0.55",  # STRING, not number
    }
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps(payload)))
    assert "error" not in r, f"Expected coercion to succeed, got: {r}"
    assert r.get("confidence") == 0.55, r


def test_payload_json_non_numeric_initial_confidence_rejected():
    """initial_confidence="abc" returns clean error, not TypeError."""
    server = fresh_server()
    payload = {
        "claim": "x",
        "basis": "y",
        "initial_confidence": "abc",  # not a number
    }
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps(payload)))
    assert "error" in r and "initial_confidence" in r["error"], r


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_conjecture(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_conjecture(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {"claim": "x", "basis": "y", "bogus": "reject"}
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r


def test_payload_json_missing_claim_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps({"basis": "y"})))
    assert "error" in r and "claim" in r["error"], r


def test_payload_json_missing_basis_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps({"claim": "x"})))
    assert "error" in r and "basis" in r["error"], r


def test_impl_callable_with_kwargs():
    server = fresh_server()
    r = json.loads(server._add_conjecture_impl(
        claim="Internal kwargs test.",
        basis="testing _impl is callable.",
    ))
    assert "error" not in r, r
    assert "conjecture_id" in r
