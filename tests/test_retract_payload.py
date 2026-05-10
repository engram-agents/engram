"""Tests for engram_retract single-payload signature (wave 2b of ob_1499).

Coverage:
  1. Happy path — well-formed payload retracts the node.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing node_id / error_type / reason rejected.
  4. error_type validation — invalid error_type rejected.
  5. Internal helper — _retract_impl works with named kwargs.

Note on terminology: the OUTER param is `payload_json` (wave 2 wrapper);
there is also an INNER `replacement_json` field inside the payload (the
optional replacement-observation JSON for retract semantics). They are
two distinct JSON-string fields.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_payload_retract")


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


def _seed_observation(server):
    payload = {
        "url": "https://example.com/a", "title": "A",
        "quoted_text": "Test quote.",
        "interpretation": "Test interp.",
        "claim": "Test claim.",
        "quote_type": "attributed_analysis",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    return r["observation_id"]


# ── 1. Happy path ──────────────────────────────────────────────────────────


def test_payload_json_retracts_node():
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "hallucinated_claim",
        "reason": "Claim isn't supported by the cited evidence.",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("retracted_id") == ob_id or r.get("node_id") == ob_id, r


def test_payload_json_with_inner_replacement_json():
    """Outer payload_json contains an inner replacement_json string field."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    inner_replacement = json.dumps({
        "quoted_text": "Corrected quote.",
        "interpretation": "Corrected interp.",
        "claim": "Corrected claim.",
        "quote_type": "attributed_analysis",
    })
    payload = {
        "node_id": ob_id,
        "error_type": "wrong_citation",
        "reason": "Original cited the wrong passage.",
        "replacement_json": inner_replacement,
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" not in r, r


# ── 2. Schema enforcement ──────────────────────────────────────────────────


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_retract(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_retract(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id, "error_type": "other", "reason": "x",
        "bogus_field": "should reject",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


# ── 3. Required-field validation ───────────────────────────────────────────


def test_payload_json_missing_node_id_rejected():
    server = fresh_server()
    payload = {"error_type": "other", "reason": "x"}
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r and "node_id" in r["error"], r


def test_payload_json_missing_error_type_rejected():
    server = fresh_server()
    payload = {"node_id": "ob_0001", "reason": "x"}
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r and "error_type" in r["error"], r


def test_payload_json_missing_reason_rejected():
    server = fresh_server()
    payload = {"node_id": "ob_0001", "error_type": "other"}
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r and "reason" in r["error"], r


# ── 4. error_type validation ───────────────────────────────────────────────


def test_payload_json_invalid_error_type_rejected():
    server = fresh_server()
    payload = {
        "node_id": "ob_0001",
        "error_type": "bogus_error_type",
        "reason": "x",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r and "Invalid error_type" in r["error"], r


# ── 5. Internal helper still callable with kwargs ──────────────────────────


def test_impl_callable_with_kwargs():
    server = fresh_server()
    ob_id = _seed_observation(server)
    r = json.loads(server._retract_impl(
        node_id=ob_id,
        error_type="other",
        reason="kwargs-path test",
    ))
    assert "error" not in r, r
