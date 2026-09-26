"""Tests for engram_add_cornerstone single-payload signature (wave 2c-ii of ob_1499).

Coverage:
  1. Happy path — well-formed payload creates a cornerstone.
  2. Optional fields (prior_frame, triggering_experience, supporting_ids) respected.
  3. Schema enforcement — invalid JSON, non-object, unknown fields.
  4. Required-field validation — missing tag / title / new_frame rejected.
  5. Internal helper — _add_cornerstone_impl works with named kwargs.
  6. is_current guard — retracted supporting_id skipped, non_current_supporting_ids populated (#1019).
  7. is_current guard — superseded supporting_id skipped, non_current_supporting_ids populated (#1019).
  8. importance_base anchor (#2250) — the interim confidence=0.5/surprise=0.0
     write is gone; the final importance_base/importance_score are unchanged
     from the pre-fix behavior (the actual correctness invariant, not an
     implementation-detail assertion on the removed dead write itself).
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_cornerstone_")
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


def test_payload_json_creates_cornerstone():
    server = fresh_server()
    payload = {
        "tag": "epistemic-identity",
        "title": "Knowing what I do not know",
        "new_frame": "Calibrate before claiming.",
    }
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "cornerstone_id" in r and r["cornerstone_id"].startswith("cs_"), r
    assert r["tag"] == "epistemic-identity"


def test_payload_json_with_optional_fields():
    server = fresh_server()
    payload = {
        "tag": "self-knowledge",
        "title": "Bold internally, careful externally",
        "new_frame": "Internal-bold / external-careful filter.",
        "prior_frame": "Wait-for-approval reflex.",
        "triggering_experience": "Regression caught in review, 2026-05-06.",
    }
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r["title"] == "Bold internally, careful externally"


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_cornerstone(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_cornerstone(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {
        "tag": "x", "title": "y", "new_frame": "z", "bogus": "reject",
    }
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus" in r["error"]


def test_payload_json_missing_tag_rejected():
    server = fresh_server()
    payload = {"title": "y", "new_frame": "z"}
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "tag" in r["error"], r


def test_payload_json_missing_title_rejected():
    server = fresh_server()
    payload = {"tag": "x", "new_frame": "z"}
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "title" in r["error"], r


def test_payload_json_missing_new_frame_rejected():
    server = fresh_server()
    payload = {"tag": "x", "title": "y"}
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "new_frame" in r["error"], r


def test_payload_json_empty_string_tag_rejected():
    server = fresh_server()
    payload = {"tag": "  ", "title": "y", "new_frame": "z"}
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" in r and "tag" in r["error"], r


def test_impl_callable_with_kwargs():
    server = fresh_server()
    r = json.loads(server._add_cornerstone_impl(
        tag="test",
        title="Internal kwargs test",
        new_frame="testing _impl is callable.",
    ))
    assert "error" not in r, r
    assert "cornerstone_id" in r


# ── 6-7. is_current guard — supporting_ids with non-current nodes (#1019) ──


def _seed_obs(server, url_suffix="a"):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": f"https://example.com/{url_suffix}",
        "title": url_suffix.upper(),
        "quoted_text": "Test quote.", "interpretation": "Test interp.", "claim": "Test claim.",
        "quote_type": "attributed_analysis",
    })))
    return r["observation_id"]


def _retract_node(server, node_id):
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": node_id,
        "error_type": "hallucinated_claim",
        "reason": "Test retraction — is_current guard test.",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN", "replacement_absent_note": "test fixture -- replacement decision not under test",
    })))
    assert "error" not in r, f"retract failed: {r}"


def _supersede_node(server, old_id, new_id):
    r = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": old_id,
        "new_node_id": new_id,
        "supersede_reason": "Test supersede — is_current guard test.",
    })))
    assert "error" not in r, f"supersede failed: {r}"


def test_retracted_supporting_id_skipped_with_warning():
    """Retracted supporting_id is skipped and appears in non_current_supporting_ids (#1019)."""
    server = fresh_server()
    obs_id = _seed_obs(server, "retract-support")
    _retract_node(server, obs_id)
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": "test-axis",
        "title": "Cornerstone with retracted support",
        "new_frame": "New frame.",
        "supporting_ids": obs_id,
    })))
    assert "error" not in r, r
    assert r.get("status") == "created", r
    assert obs_id not in r.get("supporting_nodes", []), r
    assert obs_id in r.get("non_current_supporting_ids", []), r


def test_superseded_supporting_id_skipped_with_warning():
    """Superseded supporting_id is skipped and appears in non_current_supporting_ids (#1019)."""
    server = fresh_server()
    obs_id = _seed_obs(server, "supersede-old")
    obs2_id = _seed_obs(server, "supersede-new")
    _supersede_node(server, obs_id, obs2_id)
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": "test-axis",
        "title": "Cornerstone with superseded support",
        "new_frame": "New frame.",
        "supporting_ids": obs_id,
    })))
    assert "error" not in r, r
    assert r.get("status") == "created", r
    assert obs_id not in r.get("supporting_nodes", []), r
    assert obs_id in r.get("non_current_supporting_ids", []), r


# ── 8. importance_base anchor (#2250 dead-write cleanup) ────────────────────


def _read_importance(node_id):
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT importance_base, importance_score FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        return row
    finally:
        conn.close()


def test_create_importance_base_anchored_at_2_0():
    """Cornerstone creation still ends at importance_base=2.0 with the
    matching decay-anchored importance_score, even though the interim
    confidence=0.5/surprise=0.0 write (#2250) is no longer performed.

    This is the correctness invariant that actually matters — the final
    stored values are unchanged from before the cleanup, not an assertion
    on the removed dead write itself.
    """
    server = fresh_server()
    import engram_core as core

    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": "importance-anchor-test",
        "title": "Anchor invariant check",
        "new_frame": "New frame for the anchor invariant test.",
    })))
    assert "error" not in r, r
    cs_id = r["cornerstone_id"]

    importance_base, importance_score = _read_importance(cs_id)
    assert importance_base == 2.0, (
        f"Expected importance_base=2.0 (unchanged anchor value), got {importance_base}"
    )
    expected_score = core._compute_importance(2.0, core._get_current_turn())
    assert importance_score == expected_score, (
        f"Expected importance_score={expected_score} (2.0 decayed to current "
        f"turn), got {importance_score}"
    )
    # Never left at the interim confidence=0.5/surprise=0.0 value.
    assert importance_base != 0.5
