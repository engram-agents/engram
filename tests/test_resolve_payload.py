"""Tests for engram_resolve single-payload signature (wave 2a of ob_1499).

Mirrors tests/test_add_observation_payload.py for the resolve tool.
Wave-2 motivation: 1x antml-prefix swallow fire on engram_resolve the night
of 2026-05-08 (claim split at bare `</claim>`).

Coverage:
  1. Happy path — well-formed payload creates a resolution derivation.
  2. Schema enforcement — invalid JSON, non-object payload, unknown fields.
  3. Required-field validation — missing target_id / claim / etc.
  4. Internal helper — _resolve_impl works with named kwargs.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_payload_resolve")


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


def _seed_question_with_evidence(server):
    """Create a question + supporting observation; return their IDs."""
    obs_payload = {
        "url": "https://example.com/a",
        "title": "A",
        "quoted_text": "X says Y is the case.",
        "interpretation": "X claims Y holds.",
        "claim": "Y is the case according to X.",
        "quote_type": "attributed_analysis",
    }
    ob = json.loads(server.engram_add_observation(payload_json=json.dumps(obs_payload)))
    ob_id = ob["observation_id"]

    q = json.loads(server.engram_ask(payload_json=json.dumps({"question": "Is Y actually the case?"})))
    q_id = q["question_id"]
    return q_id, ob_id


# ── 1. Happy path ──────────────────────────────────────────────────────────


def test_payload_json_creates_resolution():
    server = fresh_server()
    q_id, ob_id = _seed_question_with_evidence(server)
    payload = {
        "target_id": q_id,
        "claim": "Yes, Y is the case based on X's account.",
        "supporting_ids": ob_id,
        "logical_chain": "X attests Y → therefore Y holds (attributed).",
        "reasoning_type": "authority_expert",
    }
    r = json.loads(server.engram_resolve(payload_json=json.dumps(payload)))
    dv_id = r.get("derivation_id") or r.get("node_id")
    assert dv_id and dv_id.startswith("dv_"), r


def test_payload_json_resolution_with_optional_fields():
    server = fresh_server()
    q_id, ob_id = _seed_question_with_evidence(server)
    payload = {
        "target_id": q_id,
        "claim": "Y is the case via authority chain.",
        "supporting_ids": ob_id,
        "logical_chain": "X attests Y.",
        "reasoning_type": "authority_expert",
        "context_ids": "",
        "use_stale": False,
    }
    r = json.loads(server.engram_resolve(payload_json=json.dumps(payload)))
    assert "derivation_id" in r or "node_id" in r, r


# ── 2. Schema enforcement ──────────────────────────────────────────────────


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_resolve(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_resolve(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    q_id, ob_id = _seed_question_with_evidence(server)
    payload = {
        "target_id": q_id,
        "claim": "x",
        "supporting_ids": ob_id,
        "logical_chain": "y",
        "bogus_field": "should reject",
    }
    r = json.loads(server.engram_resolve(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


# ── 3. Required-field validation (defers to impl) ─────────────────────────


def test_payload_json_missing_supporting_ids_rejected():
    server = fresh_server()
    q_id, _ = _seed_question_with_evidence(server)
    payload = {
        "target_id": q_id,
        "claim": "x",
        "supporting_ids": "",
        "logical_chain": "y",
    }
    r = json.loads(server.engram_resolve(payload_json=json.dumps(payload)))
    assert "error" in r, r


def test_payload_json_missing_target_id_rejected():
    server = fresh_server()
    _, ob_id = _seed_question_with_evidence(server)
    payload = {
        "target_id": "",
        "claim": "x",
        "supporting_ids": ob_id,
        "logical_chain": "y",
    }
    r = json.loads(server.engram_resolve(payload_json=json.dumps(payload)))
    assert "error" in r and "target_id" in r["error"], r


def test_payload_json_missing_claim_rejected():
    server = fresh_server()
    q_id, ob_id = _seed_question_with_evidence(server)
    payload = {
        "target_id": q_id,
        "claim": "",
        "supporting_ids": ob_id,
        "logical_chain": "y",
    }
    r = json.loads(server.engram_resolve(payload_json=json.dumps(payload)))
    assert "error" in r and "claim" in r["error"], r


def test_payload_json_missing_logical_chain_rejected():
    server = fresh_server()
    q_id, ob_id = _seed_question_with_evidence(server)
    payload = {
        "target_id": q_id,
        "claim": "x",
        "supporting_ids": ob_id,
        "logical_chain": "",
    }
    r = json.loads(server.engram_resolve(payload_json=json.dumps(payload)))
    assert "error" in r and "logical_chain" in r["error"], r


# ── 4. Internal helper still callable with kwargs ─────────────────────────


def test_impl_callable_with_kwargs():
    server = fresh_server()
    q_id, ob_id = _seed_question_with_evidence(server)
    r = json.loads(server._resolve_impl(
        target_id=q_id,
        claim="Internal-helper resolve via kwargs.",
        supporting_ids=ob_id,
        logical_chain="kwargs-path test",
        reasoning_type="authority_expert",
    ))
    dv_id = r.get("derivation_id") or r.get("node_id")
    assert dv_id, r
