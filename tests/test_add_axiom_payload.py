"""Tests for engram_add_axiom single-payload signature (wave 2c-i of ob_1499).

Coverage:
  1. Happy path — well-formed payload creates an axiom.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing claim / basis rejected.
  4. Internal helper — _add_axiom_impl works with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_axiom_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def test_payload_json_creates_axiom():
    server = fresh_server()
    payload = {
        "claim": "Honesty is non-negotiable in graph construction.",
        "basis": "ax_0001 founding principle.",
    }
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "axiom_id" in r and r["axiom_id"].startswith("ax_"), r
    assert r.get("confidence") == 1.0


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_axiom(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_axiom(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {"claim": "x", "basis": "y", "bogus": "reject"}
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus" in r["error"]


def test_payload_json_missing_claim_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({"basis": "y"})))
    assert "error" in r and "claim" in r["error"], r


def test_payload_json_missing_basis_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({"claim": "x"})))
    assert "error" in r and "basis" in r["error"], r


def test_impl_callable_with_kwargs():
    server = fresh_server()
    r = json.loads(server._add_axiom_impl(
        claim="Internal kwargs path test.",
        basis="testing _impl is callable.",
    ))
    assert "error" not in r, r
    assert "axiom_id" in r
