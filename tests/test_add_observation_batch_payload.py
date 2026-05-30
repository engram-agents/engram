"""Tests for engram_add_observation_batch single-payload signature (wave 3 of ob_1499 — issue #99).

The migration unifies the 8-arg surface (observations_json + url + title + domain
+ source_date + evidence_id + content_hash + git_sha) into one payload_json
object. The inner `observations_json` field is itself a stringified JSON array
(nested encoding — matches engram_retract's `replacement_json` pattern).

Coverage:
  1. Happy path — well-formed payload creates observations.
  2. Schema enforcement — invalid outer JSON, non-object, unknown fields.
  3. Inner observations_json — invalid JSON, non-array.
  4. Source identification — neither url nor evidence_id rejected.
  5. Internal helper — _add_observation_batch_impl callable with kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_obs_batch_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))
TEST_FILE = os.path.join(TEST_DIR, "obs_batch_source.md")


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
    # Local file source — file:// URLs skip DNS validation. Content must
    # include the quoted_text values used in _two_obs() because server-side
    # verify_quote runs against the file content for file:// evidence.
    with open(TEST_FILE, "w") as f:
        f.write(
            "First quoted snippet from the test source.\n"
            "Second quoted snippet from the test source.\n"
        )
    return server


def _two_obs():
    return [
        {
            "quoted_text": "First quoted snippet from the test source.",
            "interpretation": "First interpretation",
            "claim": "First atomic claim from batch test.",
            "quote_type": "attributed_analysis",
        },
        {
            "quoted_text": "Second quoted snippet from the test source.",
            "interpretation": "Second interpretation",
            "claim": "Second atomic claim from batch test.",
            "quote_type": "attributed_analysis",
        },
    ]


def test_payload_json_creates_batch():
    server = fresh_server()
    payload = {
        "observations_json": json.dumps(_two_obs()),
        "url": f"file://{TEST_FILE}",
        "title": "Test source for batch payload test",
    }
    r = json.loads(server.engram_add_observation_batch(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("status") == "batch_complete"
    assert r.get("total") == 2
    assert r.get("created") == 2
    assert r.get("evidence_id", "").startswith("ev_")


def test_payload_json_invalid_outer_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_observation_batch(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON in payload_json" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_observation_batch(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {
        "observations_json": json.dumps(_two_obs()),
        "url": f"file://{TEST_FILE}",
        "title": "Test source",
        "bogus_field": "reject me",
    }
    r = json.loads(server.engram_add_observation_batch(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


def test_payload_json_invalid_inner_observations_rejected():
    server = fresh_server()
    payload = {
        "observations_json": "{not-an-array}",
        "url": f"file://{TEST_FILE}",
        "title": "Test source",
    }
    r = json.loads(server.engram_add_observation_batch(payload_json=json.dumps(payload)))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_inner_observations_non_array_rejected():
    server = fresh_server()
    payload = {
        "observations_json": json.dumps({"not": "an array"}),
        "url": f"file://{TEST_FILE}",
        "title": "Test source",
    }
    r = json.loads(server.engram_add_observation_batch(payload_json=json.dumps(payload)))
    assert "error" in r and "must be a JSON array" in r["error"], r


def test_payload_json_missing_source_identification_rejected():
    server = fresh_server()
    payload = {
        "observations_json": json.dumps(_two_obs()),
        # No url, no evidence_id — should error.
    }
    r = json.loads(server.engram_add_observation_batch(payload_json=json.dumps(payload)))
    assert "error" in r and ("evidence_id" in r["error"] or "url" in r["error"]), r


def test_payload_json_url_without_title_rejected():
    server = fresh_server()
    payload = {
        "observations_json": json.dumps(_two_obs()),
        "url": f"file://{TEST_FILE}",
        # No title — should error.
    }
    r = json.loads(server.engram_add_observation_batch(payload_json=json.dumps(payload)))
    assert "error" in r and "title" in r["error"], r


def test_impl_callable_with_kwargs():
    """In-server callers should be able to call _add_observation_batch_impl directly."""
    server = fresh_server()
    r = json.loads(server._add_observation_batch_impl(
        observations_json=json.dumps(_two_obs()),
        url=f"file://{TEST_FILE}",
        title="Test source for _impl kwargs path",
    ))
    assert "error" not in r, r
    assert r.get("total") == 2
    assert r.get("created") == 2
