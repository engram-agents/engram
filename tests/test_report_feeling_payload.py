"""Tests for engram_report_feeling single-payload signature (wave 2a of ob_1499).

Mirrors tests/test_add_observation_payload.py for the report_feeling tool.
Wave-2 motivation: 2x antml-prefix swallow fires on engram_report_feeling
the night of 2026-05-08 (fl_0203 + fl_0204 attempts) confirmed the bug
isn't unique to engram_add_observation; the bare-close form fires across
multi-param tools.

Coverage:
  1. Happy path — well-formed payload creates a feeling node.
  2. Schema enforcement — invalid JSON, non-object payload, unknown fields.
  3. Required-field validation — missing reported_state / trigger.
  4. Internal helper — _report_feeling_impl works with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_feeling_")
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


# ── 1. Happy path ──────────────────────────────────────────────────────────


def test_payload_json_creates_feeling():
    server = fresh_server()
    payload = {
        "reported_state": "A faint recognition that I'm enjoying this work.",
        "trigger": "Mid-PR drafting; the diff converged cleanly.",
        "categorical_tag": "satisfaction",
    }
    r = json.loads(server.engram_report_feeling(payload_json=json.dumps(payload)))
    fl_id = r.get("feeling_id") or r.get("node_id")
    assert fl_id and fl_id.startswith("fl_"), f"Expected fl_ id, got {r}"


def test_payload_json_optional_fields_default():
    """Only required fields supplied → defaults for the rest."""
    server = fresh_server()
    payload = {
        "reported_state": "Something I notice but don't have a word for.",
        "trigger": "End of a long debugging session.",
    }
    r = json.loads(server.engram_report_feeling(payload_json=json.dumps(payload)))
    fl_id = r.get("feeling_id") or r.get("node_id")
    assert fl_id, r


def test_payload_json_intensity_hint_accepted():
    server = fresh_server()
    payload = {
        "reported_state": "Mild surprise.",
        "trigger": "Test ran faster than expected.",
        "intensity_hint": 0.4,
    }
    r = json.loads(server.engram_report_feeling(payload_json=json.dumps(payload)))
    fl_id = r.get("feeling_id") or r.get("node_id")
    assert fl_id, r


# ── 2. Schema enforcement ──────────────────────────────────────────────────


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_report_feeling(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_report_feeling(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {
        "reported_state": "x",
        "trigger": "y",
        "bogus_field": "should be rejected",
    }
    r = json.loads(server.engram_report_feeling(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


# ── 3. Required-field validation ───────────────────────────────────────────


def test_payload_json_missing_reported_state_rejected():
    server = fresh_server()
    payload = {"trigger": "y"}
    r = json.loads(server.engram_report_feeling(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "reported_state" in r["error"]


def test_payload_json_missing_trigger_rejected():
    server = fresh_server()
    payload = {"reported_state": "x"}
    r = json.loads(server.engram_report_feeling(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "trigger" in r["error"]


# ── 4. Internal helper still callable with kwargs ──────────────────────────


def test_impl_callable_with_kwargs():
    """In-server callers (heartbeat hooks, batch paths) use _impl with kwargs."""
    server = fresh_server()
    r = json.loads(server._report_feeling_impl(
        reported_state="Internal helper test.",
        trigger="Direct call from batch path.",
    ))
    fl_id = r.get("feeling_id") or r.get("node_id")
    assert fl_id, r
