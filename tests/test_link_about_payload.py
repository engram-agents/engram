"""Tests for engram_link_about single-payload signature (wave 2b of ob_1499).

Mirrors tests/test_report_feeling_payload.py for the link_about tool.
Wave 2b motivation per issue #55: extend the payload-signature pattern to
high-blast-radius mutators + edge-creators.

Coverage:
  1. Happy path — well-formed payload creates an `about` edge.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing node_id rejected.
  4. Internal helper — _link_about_impl works with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_link_about_")
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


def _seed_self_anchor_and_obs(server):
    """Create a self-anchor person + an observation; return their IDs.

    NB: engram_add_person was migrated to single-payload signature in PR #68
    (wave 2c-ii, merged 2026-05-10). This helper now wraps the person kwargs
    in payload_json. Issue #75 documents the post-migration test-helper-gap
    that this fix closes.
    """
    p = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "Test Self",
        "role": "agent",
        "description": "self-anchor for tests",
        "is_self": True,
    })))
    person_id = p.get("person_id") or p.get("node_id")

    obs_payload = {
        "url": "https://example.com/a",
        "title": "A",
        "quoted_text": "X says Y is the case.",
        "interpretation": "X claims Y holds.",
        "claim": "Y is the case according to X.",
        "quote_type": "attributed_analysis",
    }
    o = json.loads(server.engram_add_observation(payload_json=json.dumps(obs_payload)))
    obs_id = o["observation_id"]
    return person_id, obs_id


# ── 1. Happy path ──────────────────────────────────────────────────────────


def test_payload_json_creates_about_edge():
    server = fresh_server()
    _, obs_id = _seed_self_anchor_and_obs(server)
    payload = {"node_id": obs_id}  # default to self-anchor
    r = json.loads(server.engram_link_about(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("status") in ("created", "exists", "linked", "already_linked"), r


def test_payload_json_with_explicit_person_id():
    server = fresh_server()
    person_id, obs_id = _seed_self_anchor_and_obs(server)
    payload = {"node_id": obs_id, "person_id": person_id}
    r = json.loads(server.engram_link_about(payload_json=json.dumps(payload)))
    assert "error" not in r, r


# ── 2. Schema enforcement ──────────────────────────────────────────────────


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_link_about(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_link_about(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {"node_id": "ob_0001", "bogus_field": "should be rejected"}
    r = json.loads(server.engram_link_about(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


# ── 3. Required-field validation ───────────────────────────────────────────


def test_payload_json_missing_node_id_rejected():
    server = fresh_server()
    r = json.loads(server.engram_link_about(payload_json=json.dumps({})))
    assert "error" in r, r
    assert "node_id" in r["error"]


def test_payload_json_empty_node_id_rejected():
    server = fresh_server()
    r = json.loads(server.engram_link_about(payload_json=json.dumps({"node_id": ""})))
    assert "error" in r and "node_id" in r["error"], r


# ── 4. Internal helper still callable with kwargs ──────────────────────────


def test_impl_callable_with_kwargs():
    server = fresh_server()
    _, obs_id = _seed_self_anchor_and_obs(server)
    r = json.loads(server._link_about_impl(node_id=obs_id))
    assert "error" not in r, r
