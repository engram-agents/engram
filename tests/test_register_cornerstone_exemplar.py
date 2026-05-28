"""Tests for engram_register_cornerstone_exemplar (PR-B).

Coverage:
  §4.1 Happy path — edge creation, idempotency, note stored on edge.
  §4.2 Validation failures — wrong target type, non-claim-bearing exemplar,
       missing nodes, retracted cornerstone, empty fields, unknown fields.
  §4.3 Integration with PR #421 zero-support detection — exemplifies edges
       to cornerstones count as live support in _detect_zero_support.

Axioms are used to seed claim-bearing exemplars (no DNS dependency).
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_register_cornerstone_exemplar")


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


def _seed_cs(server, supporting_ids, tag="cs-tag"):
    if isinstance(supporting_ids, list):
        supporting_ids = ",".join(supporting_ids)
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": tag,
        "title": f"Cornerstone: {tag}",
        "new_frame": f"Principle for {tag}.",
        "supporting_ids": supporting_ids,
    })))
    assert "error" not in r, r
    return r["cornerstone_id"]


def _seed_ob(server, claim="Test claim.", url="https://example.com/src",
             quote_type="attributed_analysis"):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Source",
        "quoted_text": claim,
        "interpretation": f"Interpretation: {claim}",
        "claim": claim,
        "quote_type": quote_type,
    })))
    assert "error" not in r, f"Failed to create observation: {r}"
    return r["observation_id"]


def _seed_ls(server, claim="Lesson pattern.", incident_id=None):
    if incident_id is None:
        incident_id = _seed_ob(server, f"Placeholder incident for: {claim}",
                               url="https://example.com/placeholder")
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": claim,
        "incident_ids": incident_id,
        "logical_chain": f"Pattern derived from incidents: {claim}",
        "scaffolding_nudge": "Watch for this pattern.",
    })))
    assert "error" not in r, r
    return r["lesson_id"]


def _get_meta(server, node_id):
    conn = server._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return json.loads(row["metadata"] or "{}") if row else {}
    finally:
        conn.close()


def _is_tainted(server, node_id, by_id):
    meta = _get_meta(server, node_id)
    return by_id in meta.get("tainted_by", [])


# ---------------------------------------------------------------------------
# §4.1 Happy path
# ---------------------------------------------------------------------------


def test_register_exemplar_creates_edge():
    """Happy path: valid cornerstone + claim-bearing exemplar creates exemplifies edge."""
    server = fresh_server()
    sup = _seed_axiom(server, "Supporting instance for cornerstone.")
    cs_id = _seed_cs(server, sup, tag="exemplar-happy-cs")
    new_exemplar = _seed_axiom(server, "New exemplar for the cornerstone pattern.")

    r = json.loads(server.engram_register_cornerstone_exemplar(
        payload_json=json.dumps({
            "cornerstone_id": cs_id,
            "exemplar_id": new_exemplar,
        })
    ))
    assert "error" not in r, r
    assert r.get("status") == "created", r
    assert r["edge"]["source"] == new_exemplar
    assert r["edge"]["target"] == cs_id
    assert r["edge"]["relation"] == "exemplifies"

    # Verify edge exists in DB.
    conn = server._get_db()
    try:
        edge = conn.execute(
            """SELECT id FROM edges
               WHERE source_id = ? AND target_id = ? AND relation = 'exemplifies'""",
            (new_exemplar, cs_id),
        ).fetchone()
    finally:
        conn.close()
    assert edge is not None, f"Expected exemplifies edge in DB from {new_exemplar} to {cs_id}"

    # Verify exemplar count and claim in return value.
    assert r.get("cornerstone_id") == cs_id
    assert r.get("cornerstone_claim") is not None
    assert isinstance(r.get("cornerstone_exemplar_count"), int)
    assert r["cornerstone_exemplar_count"] >= 1


def test_register_exemplar_idempotent():
    """Calling twice with same args: second call returns already_exists; only one edge in DB."""
    server = fresh_server()
    sup = _seed_axiom(server, "Supporting instance.")
    cs_id = _seed_cs(server, sup, tag="idempotent-cs")
    exemplar = _seed_axiom(server, "Idempotency test exemplar.")

    payload = json.dumps({"cornerstone_id": cs_id, "exemplar_id": exemplar})
    r1 = json.loads(server.engram_register_cornerstone_exemplar(payload_json=payload))
    assert r1.get("status") == "created", r1

    r2 = json.loads(server.engram_register_cornerstone_exemplar(payload_json=payload))
    assert r2.get("status") == "already_exists", r2

    # Only one edge in DB.
    conn = server._get_db()
    try:
        count = conn.execute(
            """SELECT COUNT(*) AS c FROM edges
               WHERE source_id = ? AND target_id = ? AND relation = 'exemplifies'""",
            (exemplar, cs_id),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1, f"Expected exactly 1 exemplifies edge, got {count}"


def test_register_exemplar_with_note():
    """Note field is stored on the edge's metadata."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for note-cs.")
    cs_id = _seed_cs(server, sup, tag="note-cs")
    exemplar = _seed_axiom(server, "Exemplar for note test.")
    note_text = "This exemplar matches because of feature X."

    r = json.loads(server.engram_register_cornerstone_exemplar(
        payload_json=json.dumps({
            "cornerstone_id": cs_id,
            "exemplar_id": exemplar,
            "note": note_text,
        })
    ))
    assert r.get("status") == "created", r
    assert r["edge"].get("note") == note_text

    # Verify note stored in DB edge metadata.
    conn = server._get_db()
    try:
        row = conn.execute(
            """SELECT metadata FROM edges
               WHERE source_id = ? AND target_id = ? AND relation = 'exemplifies'""",
            (exemplar, cs_id),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    edge_meta = json.loads(row["metadata"]) if row["metadata"] else {}
    assert edge_meta.get("note") == note_text, f"Expected note in edge metadata: {edge_meta}"


# ---------------------------------------------------------------------------
# §4.2 Validation failures
# ---------------------------------------------------------------------------


def test_register_exemplar_rejects_non_cornerstone_target_lesson():
    """Passing a lesson_id as cornerstone_id is rejected with type mismatch error."""
    server = fresh_server()
    # Use an axiom as the incident so we don't need DNS resolution.
    inc = _seed_axiom(server, "Incident (axiom) for lesson seed.")
    ls_id = _seed_ls(server, "A lesson.", incident_id=inc)
    exemplar = _seed_axiom(server, "Exemplar for wrong-target test.")

    r = json.loads(server.engram_register_cornerstone_exemplar(
        payload_json=json.dumps({"cornerstone_id": ls_id, "exemplar_id": exemplar})
    ))
    assert "error" in r, r
    # Error should name the type mismatch.
    assert "cornerstone" in r["error"].lower() or "lesson" in r["error"].lower(), r


def test_register_exemplar_rejects_non_cornerstone_target_axiom_used_as_cs():
    """Passing a non-cornerstone node (axiom) as cornerstone_id is rejected."""
    server = fresh_server()
    # Use an axiom as the "wrong target" — it has type='axiom', not 'cornerstone'.
    ax_id = _seed_axiom(server, "Axiom used as wrong cornerstone target.")
    exemplar = _seed_axiom(server, "Exemplar for wrong-target-axiom test.")

    r = json.loads(server.engram_register_cornerstone_exemplar(
        payload_json=json.dumps({"cornerstone_id": ax_id, "exemplar_id": exemplar})
    ))
    assert "error" in r, r
    assert "cornerstone" in r["error"].lower() or ax_id in r["error"], r


def test_register_exemplar_rejects_non_claim_bearing_exemplar_feeling():
    """Passing a feeling_report node as exemplar_id is rejected."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for rejection test cs.")
    cs_id = _seed_cs(server, sup, tag="feeling-reject-cs")

    fl_r = json.loads(server.engram_report_feeling(payload_json=json.dumps({
        "reported_state": "curious",
        "trigger": "Testing non-claim-bearing rejection.",
    })))
    assert "error" not in fl_r, fl_r
    fl_id = fl_r["feeling_id"]

    r = json.loads(server.engram_register_cornerstone_exemplar(
        payload_json=json.dumps({"cornerstone_id": cs_id, "exemplar_id": fl_id})
    ))
    assert "error" in r, r
    assert "claim-bearing" in r["error"].lower() or "feeling" in r["error"].lower(), r


def test_register_exemplar_rejects_missing_cornerstone():
    """Nonexistent cornerstone_id is rejected with clear error."""
    server = fresh_server()
    exemplar = _seed_axiom(server, "Exemplar for missing-cs test.")

    r = json.loads(server.engram_register_cornerstone_exemplar(
        payload_json=json.dumps({"cornerstone_id": "cs_99999", "exemplar_id": exemplar})
    ))
    assert "error" in r, r
    assert "not found" in r["error"].lower() or "cs_99999" in r["error"], r


def test_register_exemplar_rejects_retracted_cornerstone():
    """Retracted cornerstone (is_current=0) is rejected."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for retracted-cs test.")
    cs_id = _seed_cs(server, sup, tag="retracted-cs")

    # Retract the cornerstone via outgrow (creates new replacement, old becomes is_current=0).
    # Use direct DB update to simulate retraction without needing a replacement cornerstone.
    conn = server._get_db()
    try:
        conn.execute("UPDATE nodes SET is_current = 0 WHERE id = ?", (cs_id,))
        conn.commit()
    finally:
        conn.close()

    exemplar = _seed_axiom(server, "Exemplar for retracted-cs test.")
    r = json.loads(server.engram_register_cornerstone_exemplar(
        payload_json=json.dumps({"cornerstone_id": cs_id, "exemplar_id": exemplar})
    ))
    assert "error" in r, r
    assert "superseded" in r["error"].lower() or "is_current" in r["error"].lower(), r


def test_register_exemplar_rejects_missing_exemplar():
    """Nonexistent exemplar_id is rejected with clear error."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for missing-exemplar test.")
    cs_id = _seed_cs(server, sup, tag="missing-exemplar-cs")

    r = json.loads(server.engram_register_cornerstone_exemplar(
        payload_json=json.dumps({"cornerstone_id": cs_id, "exemplar_id": "ax_99999"})
    ))
    assert "error" in r, r
    assert "not found" in r["error"].lower() or "ax_99999" in r["error"], r


def test_register_exemplar_rejects_empty_cornerstone_id():
    """Empty cornerstone_id is rejected with clear error."""
    server = fresh_server()
    exemplar = _seed_axiom(server, "Exemplar for empty-cornerstone-id test.")

    r = json.loads(server.engram_register_cornerstone_exemplar(
        payload_json=json.dumps({"cornerstone_id": "", "exemplar_id": exemplar})
    ))
    assert "error" in r, r
    assert "cornerstone_id" in r["error"], r


def test_register_exemplar_rejects_empty_exemplar_id():
    """Empty exemplar_id is rejected with clear error."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for empty-exemplar-id test.")
    cs_id = _seed_cs(server, sup, tag="empty-exemplar-id-cs")

    r = json.loads(server.engram_register_cornerstone_exemplar(
        payload_json=json.dumps({"cornerstone_id": cs_id, "exemplar_id": ""})
    ))
    assert "error" in r, r
    assert "exemplar_id" in r["error"], r


def test_register_exemplar_rejects_unknown_field():
    """Payload with extra unknown field returns 'Unknown fields' error."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for unknown-field test.")
    cs_id = _seed_cs(server, sup, tag="unknown-field-cs")
    exemplar = _seed_axiom(server, "Exemplar for unknown-field test.")

    r = json.loads(server.engram_register_cornerstone_exemplar(
        payload_json=json.dumps({
            "cornerstone_id": cs_id,
            "exemplar_id": exemplar,
            "bogus_field": "reject me",
        })
    ))
    assert "error" in r, r
    assert "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"], r


def test_register_exemplar_invalid_json_rejected():
    """Malformed JSON in payload_json returns clear error."""
    server = fresh_server()
    r = json.loads(server.engram_register_cornerstone_exemplar(payload_json="{not-json}"))
    assert "error" in r, r
    assert "Invalid JSON" in r["error"], r


def test_register_exemplar_non_object_payload_rejected():
    """Non-object JSON (e.g., array) in payload_json returns clear error."""
    server = fresh_server()
    r = json.loads(server.engram_register_cornerstone_exemplar(payload_json="[1, 2, 3]"))
    assert "error" in r, r
    assert "must be a JSON object" in r["error"], r


# ---------------------------------------------------------------------------
# §4.3 Integration with PR #421's zero-support detection
# ---------------------------------------------------------------------------


def test_cornerstone_exemplifies_edge_counts_as_live_support():
    """Exemplifies edge from a live exemplar counts as live support for a cornerstone.

    Setup:
    - cs_X has all supported_by edges pointing to retracted nodes (would normally
      trigger support_lost).
    - Add an exemplifies edge from a LIVE exemplar to cs_X.
    - Retract a downstream node that touches cs_X.

    Expected: cs_X is NOT marked support_lost because the live exemplifies
    edge counts as live support in _detect_zero_support.
    """
    server = fresh_server()
    # Create the sole supported_by premise then immediately retract it.
    sole_premise = _seed_axiom(server, "Sole supported_by premise for cs_X.")
    cs_x = _seed_cs(server, sole_premise, tag="exemplifies-live-support-cs")

    # Retract the sole premise — this normally triggers support_lost.
    # But we'll add a live exemplifies edge FIRST to prevent that.
    live_exemplar = _seed_axiom(server, "Live exemplar that will hold cs_X support.")
    ex_r = json.loads(server.engram_register_cornerstone_exemplar(
        payload_json=json.dumps({
            "cornerstone_id": cs_x,
            "exemplar_id": live_exemplar,
        })
    ))
    assert ex_r.get("status") == "created", ex_r

    # Now retract the sole_premise (this triggers _detect_zero_support).
    ret = json.loads(server._retract_impl(
        node_id=sole_premise,
        error_type="hallucinated_claim",
        reason="Sole premise retracted — exemplifies edge should prevent support_lost.",
    ))
    assert "error" not in ret, ret

    # cs_X must NOT be in support_lost list — live exemplifies edge protects it.
    support_lost_ids = [n["id"] for n in ret.get("support_lost", [])]
    assert cs_x not in support_lost_ids, (
        f"cs_x {cs_x} must NOT be in support_lost — live exemplifies edge from "
        f"{live_exemplar} counts as live support. support_lost={support_lost_ids}"
    )

    meta = _get_meta(server, cs_x)
    assert not meta.get("support_lost"), (
        f"cs_x {cs_x} metadata.support_lost must NOT be True: {meta}"
    )
    # cs_x must also not be tainted (cascade skip rule).
    assert not _is_tainted(server, cs_x, sole_premise), (
        f"cs_x {cs_x} must NOT be tainted_by {sole_premise} — cascade skip rule applies"
    )


def test_cornerstone_support_lost_when_no_exemplifies_either():
    """Support_lost fires when no live supported_by AND no live exemplifies edges.

    Same setup as above but WITHOUT the exemplifies edge — verifies that
    exemplifies counting actually fires (regression guard).
    """
    server = fresh_server()
    sole_premise = _seed_axiom(server, "Sole supported_by premise — no exemplifies.")
    cs_x = _seed_cs(server, sole_premise, tag="no-exemplifies-support-lost-cs")

    # No exemplifies edge added.

    ret = json.loads(server._retract_impl(
        node_id=sole_premise,
        error_type="hallucinated_claim",
        reason="Sole premise retracted — no exemplifies edge, should trigger support_lost.",
    ))
    assert "error" not in ret, ret

    support_lost_ids = [n["id"] for n in ret.get("support_lost", [])]
    assert cs_x in support_lost_ids, (
        f"cs_x {cs_x} MUST be in support_lost — no live supported_by and no live exemplifies. "
        f"support_lost={support_lost_ids}"
    )

    meta = _get_meta(server, cs_x)
    assert meta.get("support_lost") is True, (
        f"cs_x {cs_x} metadata.support_lost must be True: {meta}"
    )


def test_impl_callable_with_kwargs():
    """In-server callers should be able to call _register_cornerstone_exemplar_impl directly."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for direct-impl test.")
    cs_id = _seed_cs(server, sup, tag="direct-impl-cs")
    exemplar = _seed_axiom(server, "Exemplar for direct-impl test.")

    r = json.loads(server._register_cornerstone_exemplar_impl(
        cornerstone_id=cs_id,
        exemplar_id=exemplar,
        note="Direct-impl smoke test.",
    ))
    assert "error" not in r, r
    assert r.get("status") == "created"
