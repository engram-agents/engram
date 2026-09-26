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
  5. is_current guards (PR #1023): non-current about node skipped + warned;
     current about node inherited normally.
  6. missing_about_ids (issue #1024): absent (DB-deleted) person node in about
     edges reported in missing_about_ids, not silently dropped.
  7. is_current guard — retracted new_supporting_id skipped, non_current_supporting_ids populated (#1019).
  8. is_current guard — superseded new_supporting_id skipped, non_current_supporting_ids populated (#1019).
  9. importance_base anchor (#2250) — the outgrow path's interim
     confidence=0.5/surprise=0.0 write is gone; the new (successor)
     cornerstone's final importance_base/importance_score are unchanged
     from the pre-fix behavior.
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_outgrow_")
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


def test_outgrow_skips_retracted_about_person_node():
    """Non-current (retracted) person node in about edges is skipped with warning."""
    server = fresh_server()
    cs_id = _seed_cornerstone(server)

    # Add a person node and link it to the cornerstone
    pr = json.loads(server._add_person_impl(name="Ghost Person", role="test subject"))
    assert "error" not in pr, pr
    pn_id = pr["person_id"]

    lr = json.loads(server._link_about_impl(node_id=cs_id, person_id=pn_id))
    assert "error" not in lr, lr

    # Retract the person node so it becomes non-current
    rr = json.loads(server._retract_impl(
        node_id=pn_id,
        error_type="other",
        reason="test: verify non-current about-edge guard",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" not in rr, rr

    # Outgrow the cornerstone — should succeed; non-current person skipped
    r = json.loads(server._outgrow_cornerstone_impl(
        old_cornerstone_id=cs_id,
        new_new_frame="Successor frame after person-node retraction.",
    ))
    assert "error" not in r, r
    assert r.get("status") == "outgrown"
    assert pn_id in r.get("non_current_about_ids", []), r
    assert pn_id not in r.get("preserved_about_edges", []), r


def test_outgrow_preserves_current_about_person_node():
    """Current person node in about edges is inherited normally."""
    server = fresh_server()
    cs_id = _seed_cornerstone(server)

    # Add a current person node and link it to the cornerstone
    pr = json.loads(server._add_person_impl(name="Active Person", role="test subject"))
    assert "error" not in pr, pr
    pn_id = pr["person_id"]

    lr = json.loads(server._link_about_impl(node_id=cs_id, person_id=pn_id))
    assert "error" not in lr, lr

    # Outgrow — current person node should be inherited
    r = json.loads(server._outgrow_cornerstone_impl(
        old_cornerstone_id=cs_id,
        new_new_frame="Successor frame with active person.",
    ))
    assert "error" not in r, r
    assert r.get("status") == "outgrown"
    assert pn_id in r.get("preserved_about_edges", []), r
    assert "non_current_about_ids" not in r, r


def test_outgrow_reports_absent_about_person_node():
    """An about-edge pointing to a DB-deleted person node is reported in missing_about_ids."""
    server = fresh_server()
    cs_id = _seed_cornerstone(server)

    # Create a person node, link it, then hard-delete the node row so the edge is orphaned
    pr = json.loads(server._add_person_impl(name="Deleted Person", role="test subject"))
    pn_id = pr["person_id"]
    json.loads(server._link_about_impl(node_id=cs_id, person_id=pn_id))

    # Hard-delete the person node directly — the about edge remains but the node is absent
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM nodes WHERE id = ?", (pn_id,))

    r = json.loads(server._outgrow_cornerstone_impl(
        old_cornerstone_id=cs_id,
        new_new_frame="Successor frame after person-node hard deletion.",
    ))
    assert "error" not in r, r
    assert r.get("status") == "outgrown"
    assert pn_id in r.get("missing_about_ids", []), r
    assert pn_id not in r.get("preserved_about_edges", []), r
    assert pn_id not in r.get("non_current_about_ids", []), r


def test_outgrow_missing_and_noncurrent_about_nodes_reported_separately():
    """Both absent and non-current about nodes are tracked in separate fields."""
    server = fresh_server()
    cs_id = _seed_cornerstone(server)

    # Person node 1: link then retract (non-current)
    pr1 = json.loads(server._add_person_impl(name="Retracted Person", role="test"))
    pn_retracted = pr1["person_id"]
    json.loads(server._link_about_impl(node_id=cs_id, person_id=pn_retracted))
    json.loads(server._retract_impl(node_id=pn_retracted, error_type="other", reason="test", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))

    # Person node 2: link then hard-delete (absent)
    pr2 = json.loads(server._add_person_impl(name="Deleted Person", role="test"))
    pn_deleted = pr2["person_id"]
    json.loads(server._link_about_impl(node_id=cs_id, person_id=pn_deleted))
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM nodes WHERE id = ?", (pn_deleted,))

    # Person node 3: active — should be preserved
    pr3 = json.loads(server._add_person_impl(name="Active Person", role="test"))
    pn_active = pr3["person_id"]
    json.loads(server._link_about_impl(node_id=cs_id, person_id=pn_active))

    r = json.loads(server._outgrow_cornerstone_impl(
        old_cornerstone_id=cs_id,
        new_new_frame="Successor frame with mixed about-edge states.",
    ))
    assert "error" not in r, r
    assert r.get("status") == "outgrown"
    assert pn_retracted in r.get("non_current_about_ids", []), r
    assert pn_deleted in r.get("missing_about_ids", []), r
    assert pn_active in r.get("preserved_about_edges", []), r
    # Ensure correct separation: no cross-contamination
    assert pn_retracted not in r.get("missing_about_ids", []), r
    assert pn_deleted not in r.get("non_current_about_ids", []), r


# ── 7-8. is_current guard — new_supporting_ids with non-current nodes (#1019) ──


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


def test_retracted_new_supporting_id_skipped_with_warning():
    """Retracted new_supporting_id is skipped, non_current_supporting_ids populated (#1019)."""
    server = fresh_server()
    cs_id = _seed_cornerstone(server)
    obs_id = _seed_obs(server, "retract-support")
    _retract_node(server, obs_id)
    r = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps({
        "old_cornerstone_id": cs_id,
        "new_new_frame": "Updated frame.",
        "new_supporting_ids": obs_id,
    })))
    assert "error" not in r, r
    assert r.get("status") == "outgrown", r
    assert obs_id not in r.get("supporting_nodes_linked", []), r
    assert obs_id in r.get("non_current_supporting_ids", []), r


def test_superseded_new_supporting_id_skipped_with_warning():
    """Superseded new_supporting_id is skipped, non_current_supporting_ids populated (#1019)."""
    server = fresh_server()
    cs_id = _seed_cornerstone(server)
    obs_id = _seed_obs(server, "supersede-old")
    obs2_id = _seed_obs(server, "supersede-new")
    _supersede_node(server, obs_id, obs2_id)
    r = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps({
        "old_cornerstone_id": cs_id,
        "new_new_frame": "Updated frame.",
        "new_supporting_ids": obs_id,
    })))
    assert "error" not in r, r
    assert r.get("status") == "outgrown", r
    assert obs_id not in r.get("supporting_nodes_linked", []), r
    assert obs_id in r.get("non_current_supporting_ids", []), r


# ── 9. importance_base anchor (#2250 dead-write cleanup) ────────────────────


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


def test_outgrow_importance_base_anchored_at_2_0():
    """Outgrowth's new (successor) cornerstone still ends at
    importance_base=2.0 with the matching decay-anchored importance_score,
    even though the interim confidence=0.5/surprise=0.0 write (#2250) is no
    longer performed.

    This is the correctness invariant that actually matters — the final
    stored values are unchanged from before the cleanup, not an assertion
    on the removed dead write itself.
    """
    server = fresh_server()
    import engram_core as core

    cs_id = _seed_cornerstone(server)
    r = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps({
        "old_cornerstone_id": cs_id,
        "new_new_frame": "Successor frame for the anchor invariant test.",
    })))
    assert "error" not in r, r
    new_id = r["new_cornerstone_id"]

    importance_base, importance_score = _read_importance(new_id)
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
