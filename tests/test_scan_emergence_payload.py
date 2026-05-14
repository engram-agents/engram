"""Tests for engram_scan_emergence single-payload signature
(wave 3 of ob_1499 — issue #99).

The migration unifies the 4-arg surface (min_cluster_size, similarity_threshold,
focus, node_type_filter) into one payload_json object. All fields are optional;
the default scan runs over the self-anchor scope.

Coverage:
  1. Happy path — well-formed payload runs the scan and returns clusters list.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Empty payload / default values work.
  4. Internal helper — _scan_emergence_impl callable with named kwargs.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_payload_scan_emergence")


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


def test_payload_json_scan_runs_focus_all():
    """With focus='all' the scan runs without requiring a self-anchor."""
    server = fresh_server()
    payload = {"focus": "all", "min_cluster_size": 3}
    r = json.loads(server.engram_scan_emergence(payload_json=json.dumps(payload)))
    # Either ok with empty clusters (no embeddings in fresh DB) or a
    # well-formed error. Critical: no payload-parsing error.
    assert "Invalid JSON" not in r.get("error", ""), r
    assert "must be a JSON object" not in r.get("error", ""), r
    assert "Unknown fields" not in r.get("error", ""), r
    assert "clusters" in r or "error" in r, r


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_scan_emergence(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_scan_emergence(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {"focus": "all", "bogus_field": "reject me"}
    r = json.loads(server.engram_scan_emergence(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


def test_payload_json_empty_payload_uses_defaults():
    """Calling with an empty payload should run the default scan
    (which complains about missing self-anchor unless one exists)."""
    server = fresh_server()
    r = json.loads(server.engram_scan_emergence(payload_json="{}"))
    # Default focus='self' requires self-anchor — should surface that as a
    # business-logic error, not a payload-parsing error.
    assert "Invalid JSON" not in r.get("error", ""), r
    assert "must be a JSON object" not in r.get("error", ""), r


def test_payload_json_invalid_focus_surfaced():
    server = fresh_server()
    payload = {"focus": "not_a_valid_scope"}
    r = json.loads(server.engram_scan_emergence(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown focus" in r["error"], r


def test_payload_json_no_arg_call_uses_empty_default():
    """engram_scan_emergence() with no argument should hit the
    `payload_json: str = ""` default and treat empty-string as "{}"
    (default scan). Documents the contract that the public surface
    is callable without args — a property the agent inherits and
    a future "remove default empty" refactor would silently break.
    Fairy round-1 suggestion on PR #115.
    """
    server = fresh_server()
    r = json.loads(server.engram_scan_emergence())  # no payload_json
    # Should NOT be a payload-parse error. Either succeeds (returns clusters)
    # or surfaces a business-logic error (e.g. missing self-anchor for
    # default focus='self'), both of which are valid responses.
    assert "Invalid JSON" not in r.get("error", ""), r
    assert "must be a JSON object" not in r.get("error", ""), r


def test_impl_callable_with_kwargs():
    """In-server callers should be able to call _scan_emergence_impl directly."""
    server = fresh_server()
    r = json.loads(server._scan_emergence_impl(focus="all", min_cluster_size=3))
    assert "Invalid JSON" not in r.get("error", ""), r
    assert "clusters" in r or "error" in r, r
