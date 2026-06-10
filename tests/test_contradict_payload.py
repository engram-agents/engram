"""Tests for engram_contradict single-payload signature (wave 2b of ob_1499).

Coverage:
  1. Happy path — well-formed payload creates a contradiction node.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing node_id_a / node_id_b / description.
  4. Internal helper — _contradict_impl works with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_contradict_")
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


# ── 5. #1010: is_current guard ─────────────────────────────────────────────


def _seed_conjecture(server, claim):
    """Add a conjecture and return its ID."""
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps({
        "claim": claim,
        "basis": "Seeded for #1010 guard test.",
    })))
    return r["conjecture_id"]


def _supersede(server, old_id, new_id):
    """Supersede old_id with new_id; assert success."""
    r = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": old_id,
        "new_node_id": new_id,
        "supersede_reason": "Guard test supersede.",
    })))
    assert r.get("status") == "superseded", r
    return r


def _retract(server, node_id):
    """Retract node_id; assert success."""
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": node_id,
        "error_type": "other",
        "reason": "Guard test retract.",
    })))
    assert r.get("status") in ("retracted", "retraction_recorded"), r
    return r


def test_contradict_superseded_node_a_rejected_names_successor():
    """#1010: contradicting a superseded node_id_a is rejected; error names
    the current successor."""
    server = fresh_server()
    _, live_b = _seed_two_observations(server)
    old_a = _seed_conjecture(server, "Old claim A — will be superseded.")
    new_a = _seed_conjecture(server, "Refined claim A — the live successor.")
    _supersede(server, old_a, new_a)

    r = json.loads(server.engram_contradict(payload_json=json.dumps({
        "node_id_a": old_a,
        "node_id_b": live_b,
        "description": "old_a versus live_b — should be rejected.",
    })))
    assert "error" in r, f"contradicting a superseded node must be rejected, got: {r}"
    assert "not current" in r["error"], r["error"]
    assert new_a in r["error"], (
        f"error must name the current successor {new_a}, got: {r['error']}"
    )


def test_contradict_retracted_node_b_rejected_no_successor():
    """#1010: contradicting a retracted node_b is rejected with the
    no-current-successor hint."""
    server = fresh_server()
    live_a, _ = _seed_two_observations(server)
    retracted_b = _seed_conjecture(server, "Retracted claim B — will be retracted.")
    _retract(server, retracted_b)

    r = json.loads(server.engram_contradict(payload_json=json.dumps({
        "node_id_a": live_a,
        "node_id_b": retracted_b,
        "description": "live_a versus retracted_b — should be rejected.",
    })))
    assert "error" in r, f"contradicting a retracted node must be rejected, got: {r}"
    assert "not current" in r["error"], r["error"]
    assert "no current successor" in r["error"], (
        f"error must carry the no-successor hint, got: {r['error']}"
    )
    assert "epistemic effect" in r["error"], (
        f"error must mention 'epistemic effect', got: {r['error']}"
    )


def test_contradict_both_current_succeeds():
    """#1010 regression: contradicting two live nodes still succeeds."""
    server = fresh_server()
    a_id, b_id = _seed_two_observations(server)
    r = json.loads(server.engram_contradict(payload_json=json.dumps({
        "node_id_a": a_id,
        "node_id_b": b_id,
        "description": "Both current — contradiction is valid.",
    })))
    assert "error" not in r, r
    assert "contradiction_id" in r, r


def test_contradict_superseded_node_b_rejected_names_successor():
    """#1010: node_b non-current is caught as well as node_a."""
    server = fresh_server()
    live_a, _ = _seed_two_observations(server)
    old_b = _seed_conjecture(server, "Old claim B — will be superseded.")
    new_b = _seed_conjecture(server, "Refined claim B — the live successor.")
    _supersede(server, old_b, new_b)

    r = json.loads(server.engram_contradict(payload_json=json.dumps({
        "node_id_a": live_a,
        "node_id_b": old_b,
        "description": "live_a versus old_b — should be rejected.",
    })))
    assert "error" in r, f"contradicting a superseded node_b must be rejected, got: {r}"
    assert "not current" in r["error"], r["error"]
    assert new_b in r["error"], (
        f"error must name the current successor {new_b}, got: {r['error']}"
    )
