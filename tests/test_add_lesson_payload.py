"""Tests for engram_add_lesson single-payload signature (wave 2c-ii of ob_1499).

Coverage:
  1. Happy path — well-formed payload creates a lesson from an incident observation.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing claim / incident_ids / scaffolding_nudge / logical_chain rejected.
  4. Empty-string guards on required string fields.
  5. Internal helper — _add_lesson_impl works with named kwargs.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_payload_lesson")


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


def _seed_incident(server) -> str:
    """Create a claim-bearing observation node usable as an incident."""
    payload = {
        "quoted_text": "I shipped without reading the error.",
        "interpretation": "Missed contextual cue from stderr.",
        "claim": "Skipped reading error output before hypothesizing root cause.",
        "quote_type": "personal_communication",
        "url": "https://example.com/incident",
        "title": "Test incident",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


def test_payload_json_creates_lesson():
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = {
        "claim": "When diagnosing errors, read the output before hypothesizing.",
        "incident_ids": inc_id,
        "scaffolding_nudge": "Stop. Read the error output verbatim before forming a hypothesis.",
        "logical_chain": "Single incident → action-focused tripwire pattern.",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "lesson_id" in r and r["lesson_id"].startswith("ls_"), r
    assert r["incident_nodes"] == [inc_id]


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_lesson(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_lesson(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {
        "claim": "x", "incident_ids": "ob_0001",
        "scaffolding_nudge": "y", "logical_chain": "z",
        "bogus": "reject",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus" in r["error"]


def test_payload_json_missing_claim_rejected():
    server = fresh_server()
    payload = {
        "incident_ids": "ob_0001",
        "scaffolding_nudge": "y",
        "logical_chain": "z",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "claim" in r["error"], r


def test_payload_json_missing_scaffolding_nudge_rejected():
    server = fresh_server()
    payload = {
        "claim": "x",
        "incident_ids": "ob_0001",
        "logical_chain": "z",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "scaffolding_nudge" in r["error"], r


def test_payload_json_missing_logical_chain_rejected():
    server = fresh_server()
    payload = {
        "claim": "x",
        "incident_ids": "ob_0001",
        "scaffolding_nudge": "y",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "logical_chain" in r["error"], r


def test_payload_json_missing_incident_ids_rejected():
    server = fresh_server()
    payload = {
        "claim": "x",
        "scaffolding_nudge": "y",
        "logical_chain": "z",
    }
    # Per PR #68 fairy round-1: explicit empty-string guard on incident_ids
    # parallel to the other required-field guards.
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "incident_ids" in r["error"], r


def test_payload_json_whitespace_incident_ids_rejected():
    server = fresh_server()
    payload = {
        "claim": "x",
        "incident_ids": "   ",
        "scaffolding_nudge": "y",
        "logical_chain": "z",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "incident_ids" in r["error"], r


def test_payload_json_empty_claim_rejected():
    server = fresh_server()
    payload = {
        "claim": "  ",
        "incident_ids": "ob_0001",
        "scaffolding_nudge": "y",
        "logical_chain": "z",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "claim" in r["error"], r


def test_impl_callable_with_kwargs():
    server = fresh_server()
    inc_id = _seed_incident(server)
    r = json.loads(server._add_lesson_impl(
        claim="Internal kwargs lesson.",
        incident_ids=inc_id,
        scaffolding_nudge="testing _impl is callable.",
        logical_chain="single incident → tripwire.",
    ))
    assert "error" not in r, r
    assert "lesson_id" in r
