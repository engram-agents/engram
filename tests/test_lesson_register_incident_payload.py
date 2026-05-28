"""Tests for engram_lesson_register_incident single-payload signature
(wave 3 of ob_1499 — issue #99).

The migration unifies the 3-arg surface (lesson_id, incident_id, note) into one
payload_json object. Same pattern as PR #110 (derive), PR #114 (update_task).

Coverage:
  1. Happy path — well-formed payload attaches an incident to a lesson.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing lesson_id / incident_id.
  4. Business-logic errors — wrong-type lesson, non-claim-bearing incident.
  5. Idempotence — duplicate registration is a noop.
  6. Internal helper — _lesson_register_incident_impl callable with kwargs.

Axioms (claim-bearing, no DNS dependency) are used to seed incidents.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_payload_lesson_register")


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


def _seed_axiom(server, claim, basis="test fixture basis"):
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": claim,
        "basis": basis,
    })))
    assert "error" not in r, r
    return r["axiom_id"]


def _seed_lesson(server):
    """Seed an axiom-as-incident, then a lesson that references it."""
    inc1 = _seed_axiom(server, "First incident: I forgot to verify the source URL.")
    inc2 = _seed_axiom(server, "Second incident: I forgot to verify the citation date.")
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": "Always verify source provenance before citing.",
        "incident_ids": f"{inc1},{inc2}",
        "scaffolding_nudge": "Before citing: open the source and verify URL + date.",
        "logical_chain": "Two distinct citation-verification failures generalize "
                          "to a verification-before-citation rule.",
    })))
    assert "error" not in r, r
    return r["lesson_id"]


def test_payload_json_registers_incident():
    server = fresh_server()
    lesson_id = _seed_lesson(server)
    new_inc = _seed_axiom(server, "Third incident: I forgot to verify the author name.")
    payload = {
        "lesson_id": lesson_id,
        "incident_id": new_inc,
        "note": "Author-name verification miss — same pattern.",
    }
    r = json.loads(server.engram_lesson_register_incident(
        payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("status") == "created"
    assert r["edge"]["source"] == new_inc
    assert r["edge"]["target"] == lesson_id
    assert r["edge"]["relation"] == "exemplifies"


def test_payload_json_idempotent_duplicate_is_noop():
    server = fresh_server()
    lesson_id = _seed_lesson(server)
    new_inc = _seed_axiom(server, "Repeated incident for noop test.")
    payload = {"lesson_id": lesson_id, "incident_id": new_inc}
    r1 = json.loads(server.engram_lesson_register_incident(
        payload_json=json.dumps(payload)))
    assert r1.get("status") == "created", r1
    r2 = json.loads(server.engram_lesson_register_incident(
        payload_json=json.dumps(payload)))
    assert r2.get("status") == "noop", r2


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_lesson_register_incident(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_lesson_register_incident(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    lesson_id = _seed_lesson(server)
    new_inc = _seed_axiom(server, "Incident for unknown-field test.")
    payload = {
        "lesson_id": lesson_id,
        "incident_id": new_inc,
        "bogus_field": "reject me",
    }
    r = json.loads(server.engram_lesson_register_incident(
        payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


def test_payload_json_missing_lesson_id_rejected():
    server = fresh_server()
    new_inc = _seed_axiom(server, "Incident without lesson_id.")
    payload = {"incident_id": new_inc}
    r = json.loads(server.engram_lesson_register_incident(
        payload_json=json.dumps(payload)))
    assert "error" in r and "lesson_id" in r["error"], r


def test_payload_json_missing_incident_id_rejected():
    server = fresh_server()
    lesson_id = _seed_lesson(server)
    payload = {"lesson_id": lesson_id}
    r = json.loads(server.engram_lesson_register_incident(
        payload_json=json.dumps(payload)))
    assert "error" in r and "incident_id" in r["error"], r


def test_payload_json_nonexistent_lesson_rejected():
    server = fresh_server()
    new_inc = _seed_axiom(server, "Incident with nonexistent lesson.")
    payload = {"lesson_id": "ls_99999", "incident_id": new_inc}
    r = json.loads(server.engram_lesson_register_incident(
        payload_json=json.dumps(payload)))
    assert "error" in r and "not found" in r["error"], r


def test_impl_callable_with_kwargs():
    """In-server callers should be able to call the unified impl directly.

    PR #434 collapsed the lesson-specific impl into the unified
    _register_exemplar_impl. The kwargs path now uses target_id/exemplar_id
    (unified field names); the lesson-specific kwargs (lesson_id/incident_id)
    are translated by the engram_lesson_register_incident alias wrapper.
    """
    server = fresh_server()
    lesson_id = _seed_lesson(server)
    new_inc = _seed_axiom(server, "Direct-impl test incident.")
    r = json.loads(server._register_exemplar_impl(
        target_id=lesson_id,
        exemplar_id=new_inc,
        note="Direct-impl smoke test.",
    ))
    assert "error" not in r, r
    assert r.get("status") == "created"
