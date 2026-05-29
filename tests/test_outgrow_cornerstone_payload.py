"""Tests for engram_outgrow_cornerstone single-payload signature
(wave 3 of ob_1499 — issue #99).

The migration unifies the 5-arg surface (old_cornerstone_id, new_new_frame,
new_triggering_experience, new_supporting_ids, new_title) into one
payload_json object. Same pattern as PR #110 (derive), PR #114 (update_task).

Coverage:
  1. Happy path — well-formed payload outgrows a cornerstone.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing old_cornerstone_id / new_new_frame.
  4. Internal helper — _outgrow_cornerstone_impl callable with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_outgrow_")
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


def _seed_cornerstone(server, tag="test_axis"):
    """Create a cornerstone via the already-migrated engram_add_cornerstone."""
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": tag,
        "title": "Original frame",
        "new_frame": "I used to operate this way (the predecessor frame).",
        "triggering_experience": "Initial reframe baseline for outgrowth test.",
    })))
    assert "error" not in r, r
    assert r["cornerstone_id"].startswith("cs_"), r
    return r["cornerstone_id"]


def test_payload_json_outgrows_cornerstone():
    server = fresh_server()
    cs_id = _seed_cornerstone(server)
    payload = {
        "old_cornerstone_id": cs_id,
        "new_new_frame": "I now operate this updated way (the successor frame).",
        "new_triggering_experience": "Practice surfaced a refinement opportunity.",
        "new_title": "Refined frame",
    }
    r = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("status") == "outgrown"
    assert r.get("old_cornerstone_id") == cs_id
    assert r.get("new_cornerstone_id", "").startswith("cs_")
    # Growth-cycle roll: old.new_frame should now be new.prior_frame
    assert r.get("prior_frame") == "I used to operate this way (the predecessor frame)."
    assert r.get("new_frame") == "I now operate this updated way (the successor frame)."


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_outgrow_cornerstone(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_outgrow_cornerstone(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    cs_id = _seed_cornerstone(server)
    payload = {
        "old_cornerstone_id": cs_id,
        "new_new_frame": "Refined frame.",
        "bogus_field": "reject me",
    }
    r = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


def test_payload_json_missing_old_cornerstone_id_rejected():
    server = fresh_server()
    payload = {"new_new_frame": "Refined frame."}
    r = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "old_cornerstone_id" in r["error"], r


def test_payload_json_missing_new_new_frame_rejected():
    server = fresh_server()
    cs_id = _seed_cornerstone(server)
    payload = {"old_cornerstone_id": cs_id}
    r = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "new_new_frame" in r["error"], r


def test_payload_json_nonexistent_cornerstone_rejected():
    server = fresh_server()
    payload = {
        "old_cornerstone_id": "cs_99999",
        "new_new_frame": "Frame.",
    }
    r = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "not found" in r["error"], r


def test_impl_callable_with_kwargs():
    """In-server callers should be able to call _outgrow_cornerstone_impl directly."""
    server = fresh_server()
    cs_id = _seed_cornerstone(server)
    r = json.loads(server._outgrow_cornerstone_impl(
        old_cornerstone_id=cs_id,
        new_new_frame="Internal kwargs path test frame.",
        new_triggering_experience="Direct-impl smoke test.",
    ))
    assert "error" not in r, r
    assert r.get("status") == "outgrown"
