"""Tests for engram_add_person single-payload signature (wave 2c-ii of ob_1499).

Updated in feat/person-node-typed-schema to reflect the new typed schema:
  - `role` is now a deprecated alias for `relation` (still accepted, flagged in response)
  - `description` / `logical_chain` are no longer accepted (no prose bin)
  - `relation` is optional (was required as `role` before)
  - Response uses `relation` key, not `role`

Coverage:
  1. Happy path — well-formed payload creates a person.
  2. Optional fields (aliases, is_self) respected; description rejected.
  3. Schema enforcement — invalid JSON, non-object, unknown fields.
  4. Required-field validation — missing name rejected; role/relation optional.
  5. Internal helper — _add_person_impl works with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_person_")
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


def test_payload_json_creates_person():
    """Happy path with role as deprecated alias — accepted, stored as relation."""
    server = fresh_server()
    payload = {
        "name": "Alex",
        "role": "primary collaborator",
    }
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "person_id" in r and r["person_id"].startswith("pn_"), r
    assert r["name"] == "Alex"
    # Response uses 'relation' key (role is now a deprecated alias)
    assert r["relation"] == "primary collaborator"
    # Deprecation warning is present when role is used without relation
    assert "deprecation_warning" in r


def test_payload_json_with_optional_fields():
    """Optional fields (aliases) are respected; description is no longer accepted."""
    server = fresh_server()
    # description removed from payload — it's no longer in the schema
    payload = {
        "name": "Alex",
        "role": "colleague",
        "aliases": "A,Alexander",
    }
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r["aliases"] == ["A", "Alexander"], r


def test_payload_json_description_rejected():
    """description is not in the new typed schema — rejected as unknown field."""
    server = fresh_server()
    payload = {
        "name": "Alex",
        "role": "colleague",
        "description": "Backend engineer.",
    }
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "Unknown fields" in r["error"]
    assert "description" in r["error"]


def test_payload_json_is_self_flag():
    """is_self bool is honored; second self-anchor attempt is rejected."""
    server = fresh_server()
    p1 = {"name": "TestSelf", "role": "self", "is_self": True}
    r1 = json.loads(server.engram_add_person(payload_json=json.dumps(p1)))
    assert "error" not in r1, r1
    p2 = {"name": "OtherSelf", "role": "self", "is_self": True}
    r2 = json.loads(server.engram_add_person(payload_json=json.dumps(p2)))
    assert "error" in r2 and "Self-anchor" in r2["error"], r2


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {"name": "x", "role": "y", "bogus": "reject"}
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus" in r["error"]


def test_payload_json_missing_name_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({"role": "y"})))
    assert "error" in r and "name" in r["error"], r


def test_payload_json_role_optional():
    """role/relation is now OPTIONAL (was required as `role` in old schema).
    name alone is sufficient to create a person node.
    """
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({"name": "x"})))
    assert "error" not in r, r
    assert r["person_id"].startswith("pn_"), r
    assert r["relation"] == ""


def test_payload_json_empty_string_name_rejected():
    server = fresh_server()
    payload = {"name": "  ", "role": "y"}
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert "error" in r and "name" in r["error"], r


def test_payload_json_is_self_string_rejected():
    """is_self must be a JSON boolean. A string 'false' is truthy in Python and
    would silently create a spurious self-anchor (PR #68 fairy round-1 blocker).
    """
    server = fresh_server()
    payload = {"name": "x", "role": "y", "is_self": "false"}
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert "error" in r and "is_self" in r["error"], r
    # And the truthier-looking "true" string is also rejected
    payload2 = {"name": "z", "role": "y", "is_self": "true"}
    r2 = json.loads(server.engram_add_person(payload_json=json.dumps(payload2)))
    assert "error" in r2 and "is_self" in r2["error"], r2
    # And confirm a real bool still works
    payload3 = {"name": "RealSelf", "role": "self", "is_self": True}
    r3 = json.loads(server.engram_add_person(payload_json=json.dumps(payload3)))
    assert "error" not in r3, r3


def test_impl_callable_with_kwargs():
    server = fresh_server()
    r = json.loads(server._add_person_impl(
        name="Internal",
        role="kwargs test",
    ))
    assert "error" not in r, r
    assert "person_id" in r
