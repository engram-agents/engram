"""Tests for engram_contradict single-payload signature (wave 2b of ob_1499).

Coverage:
  1. Happy path — well-formed payload creates a contradiction node.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing node_id_a / node_id_b / description.
  4. Internal helper — _contradict_impl works with named kwargs.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_payload_contradict")


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


def _seed_two_observations(server):
    """Create two claim-bearing observations for contradict tests."""
    p1 = {
        "url": "https://example.com/a", "title": "A",
        "quoted_text": "Source A says X is true.",
        "interpretation": "A claims X.",
        "claim": "X is true.",
        "quote_type": "attributed_analysis",
    }
    p2 = {
        "url": "https://example.com/b", "title": "B",
        "quoted_text": "Source B says X is false.",
        "interpretation": "B claims not X.",
        "claim": "X is false.",
        "quote_type": "attributed_analysis",
    }
    a = json.loads(server.engram_add_observation(payload_json=json.dumps(p1)))
    b = json.loads(server.engram_add_observation(payload_json=json.dumps(p2)))
    return a["observation_id"], b["observation_id"]


# ── 1. Happy path ──────────────────────────────────────────────────────────


def test_payload_json_creates_contradiction():
    server = fresh_server()
    a_id, b_id = _seed_two_observations(server)
    payload = {
        "node_id_a": a_id,
        "node_id_b": b_id,
        "description": "A says X is true; B says X is false.",
        "root_cause": "Different sources interpret evidence differently.",
    }
    r = json.loads(server.engram_contradict(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    # Server canonical key is contradiction_id (server.py:8727); assert
    # explicitly to avoid the silent-pass shape the fairy flagged.
    assert "contradiction_id" in r, f"Expected contradiction_id in response, got: {r}"
    assert r["contradiction_id"].startswith("ct_"), f"Expected ct_ prefix, got: {r['contradiction_id']}"


# ── 2. Schema enforcement ──────────────────────────────────────────────────


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_contradict(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_contradict(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    a_id, b_id = _seed_two_observations(server)
    payload = {
        "node_id_a": a_id, "node_id_b": b_id,
        "description": "x", "bogus_field": "should reject",
    }
    r = json.loads(server.engram_contradict(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


# ── 3. Required-field validation ───────────────────────────────────────────


def test_payload_json_missing_node_id_a_rejected():
    server = fresh_server()
    payload = {"node_id_b": "ob_0001", "description": "x"}
    r = json.loads(server.engram_contradict(payload_json=json.dumps(payload)))
    assert "error" in r and "node_id_a" in r["error"], r


def test_payload_json_missing_node_id_b_rejected():
    server = fresh_server()
    payload = {"node_id_a": "ob_0001", "description": "x"}
    r = json.loads(server.engram_contradict(payload_json=json.dumps(payload)))
    assert "error" in r and "node_id_b" in r["error"], r


def test_payload_json_missing_description_rejected():
    server = fresh_server()
    payload = {"node_id_a": "ob_0001", "node_id_b": "ob_0002"}
    r = json.loads(server.engram_contradict(payload_json=json.dumps(payload)))
    assert "error" in r and "description" in r["error"], r


# ── 4. Internal helper still callable with kwargs ──────────────────────────


def test_impl_callable_with_kwargs():
    server = fresh_server()
    a_id, b_id = _seed_two_observations(server)
    r = json.loads(server._contradict_impl(
        node_id_a=a_id,
        node_id_b=b_id,
        description="kwargs-path test",
    ))
    assert "error" not in r, r
