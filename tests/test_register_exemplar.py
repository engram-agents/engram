"""Tests for engram_register_exemplar unified MCP tool.

Coverage:
  §1 Happy path — edge creation for cornerstones and lessons, idempotency,
     note stored on edge.
  §2 Validation failures — wrong target type, non-claim-bearing exemplar,
     missing nodes, retracted target, empty fields, unknown fields.
  §3 Integration with PR #421 zero-support detection — exemplifies edges
     to cornerstones count as live support in _detect_zero_support.
  §4 Lesson alias backward-compat — lesson_id/incident_id field names route
     correctly via engram_lesson_register_incident.
  §5 Live-count SSoT — exemplar_count is live-computed via _count_live_exemplars,
     no longer cached in node metadata (closes #442).

Axioms are used to seed claim-bearing exemplars (no DNS dependency).
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_register_exemplar_")
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


def _seed_ls(server, claim="Lesson pattern.", incident_id=None):
    if incident_id is None:
        incident_id = _seed_axiom(server, f"Placeholder incident for: {claim}")
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
# §1 Happy path
# ---------------------------------------------------------------------------


def test_register_exemplar_cornerstone_creates_edge():
    """Happy path: unified tool with cornerstone target creates exemplifies edge."""
    server = fresh_server()
    sup = _seed_axiom(server, "Supporting instance for cornerstone.")
    cs_id = _seed_cs(server, sup, tag="unified-happy-cs")
    new_exemplar = _seed_axiom(server, "New exemplar for the cornerstone pattern.")

    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({
            "target_id": cs_id,
            "exemplar_id": new_exemplar,
        })
    ))
    assert "error" not in r, r
    assert r.get("status") == "created", r
    assert r["edge"]["source"] == new_exemplar
    assert r["edge"]["target"] == cs_id
    assert r["edge"]["relation"] == "exemplifies"
    assert r.get("target_id") == cs_id
    assert r.get("target_claim") is not None
    assert isinstance(r.get("exemplar_count"), int)
    assert r["exemplar_count"] >= 1
    # Cornerstone targets rebuild the anchor cache (#1691) — the slot
    # reserved as "future cornerstone-tripwire mechanism" is now filled.
    assert r.get("cache_rebuild", {}).get("status") == "rebuilt", r

    # Verify edge in DB.
    conn = server._get_db()
    try:
        edge = conn.execute(
            """SELECT id FROM edges
               WHERE source_id = ? AND target_id = ? AND relation = 'exemplifies'""",
            (new_exemplar, cs_id),
        ).fetchone()
    finally:
        conn.close()
    assert edge is not None


def test_register_exemplar_lesson_creates_edge():
    """Happy path: unified tool with lesson target creates exemplifies edge + cache_rebuild."""
    server = fresh_server()
    inc = _seed_axiom(server, "Incident for lesson seed.")
    ls_id = _seed_ls(server, "A testable lesson.", incident_id=inc)
    new_exemplar = _seed_axiom(server, "New exemplar for the lesson.")

    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({
            "target_id": ls_id,
            "exemplar_id": new_exemplar,
        })
    ))
    assert "error" not in r, r
    assert r.get("status") == "created", r
    assert r["edge"]["source"] == new_exemplar
    assert r["edge"]["target"] == ls_id
    assert r.get("target_id") == ls_id
    assert r.get("target_claim") is not None
    assert isinstance(r.get("exemplar_count"), int)
    # Lesson targets include cache_rebuild.
    assert "cache_rebuild" in r, r

    # Verify edge in DB.
    conn = server._get_db()
    try:
        edge = conn.execute(
            """SELECT id FROM edges
               WHERE source_id = ? AND target_id = ? AND relation = 'exemplifies'""",
            (new_exemplar, ls_id),
        ).fetchone()
    finally:
        conn.close()
    assert edge is not None


def test_register_exemplar_cornerstone_idempotent():
    """Cornerstone: second call with same args returns already_exists; only one edge in DB."""
    server = fresh_server()
    sup = _seed_axiom(server, "Supporting instance.")
    cs_id = _seed_cs(server, sup, tag="idempotent-unified-cs")
    exemplar = _seed_axiom(server, "Idempotency test exemplar.")

    payload = json.dumps({"target_id": cs_id, "exemplar_id": exemplar})
    r1 = json.loads(server.engram_register_exemplar(payload_json=payload))
    assert r1.get("status") == "created", r1

    r2 = json.loads(server.engram_register_exemplar(payload_json=payload))
    assert r2.get("status") == "already_exists", r2
    # Pin new SSoT behavior: exemplar_count = 1 exemplifies (exemplar→cs) +
    # 1 supported_by (cs→sup, live). Pre-PR this was exemplifies-only (=1).
    assert r2.get("exemplar_count") == 2, (
        f"Expected exemplar_count=2 (1 exemplifies + 1 supported_by), got {r2.get('exemplar_count')!r}"
    )

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


def test_register_exemplar_lesson_idempotent():
    """Lesson: second call with same args returns already_exists; only one edge in DB."""
    server = fresh_server()
    inc = _seed_axiom(server, "Incident for lesson idempotency test.")
    ls_id = _seed_ls(server, "Idempotency lesson.", incident_id=inc)
    exemplar = _seed_axiom(server, "Idempotency test exemplar for lesson.")

    payload = json.dumps({"target_id": ls_id, "exemplar_id": exemplar})
    r1 = json.loads(server.engram_register_exemplar(payload_json=payload))
    assert r1.get("status") == "created", r1

    r2 = json.loads(server.engram_register_exemplar(payload_json=payload))
    assert r2.get("status") == "already_exists", r2

    conn = server._get_db()
    try:
        count = conn.execute(
            """SELECT COUNT(*) AS c FROM edges
               WHERE source_id = ? AND target_id = ? AND relation = 'exemplifies'""",
            (exemplar, ls_id),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1, f"Expected exactly 1 exemplifies edge, got {count}"


def test_register_exemplar_with_note():
    """Note field is stored on the edge's metadata (cornerstone target)."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for note-cs.")
    cs_id = _seed_cs(server, sup, tag="note-unified-cs")
    exemplar = _seed_axiom(server, "Exemplar for note test.")
    note_text = "This exemplar matches because of feature X."

    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({
            "target_id": cs_id,
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
    assert edge_meta.get("note") == note_text


# ---------------------------------------------------------------------------
# §2 Validation failures
# ---------------------------------------------------------------------------


def test_register_exemplar_rejects_observation_target():
    """Passing an observation as target_id is rejected — must be lesson or cornerstone."""
    server = fresh_server()
    ob_id = _seed_axiom(server, "Axiom used as wrong target.")
    exemplar = _seed_axiom(server, "Exemplar for wrong-target test.")

    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({"target_id": ob_id, "exemplar_id": exemplar})
    ))
    assert "error" in r, r
    # Error should name the type constraint.
    err = r["error"].lower()
    assert "lesson" in err and "cornerstone" in err, r


def test_register_exemplar_rejects_lesson_target_if_exemplar_is_feeling():
    """Non-claim-bearing feeling_report as exemplar_id is rejected (lesson target)."""
    server = fresh_server()
    inc = _seed_axiom(server, "Incident for lesson seed.")
    ls_id = _seed_ls(server, "Lesson for feeling-reject test.", incident_id=inc)

    fl_r = json.loads(server.engram_report_feeling(payload_json=json.dumps({
        "reported_state": "curious",
        "trigger": "Testing non-claim-bearing rejection.",
    })))
    assert "error" not in fl_r, fl_r
    fl_id = fl_r["feeling_id"]

    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({"target_id": ls_id, "exemplar_id": fl_id})
    ))
    assert "error" in r, r
    assert "claim-bearing" in r["error"].lower() or "feeling" in r["error"].lower(), r


def test_register_exemplar_rejects_cornerstone_target_if_exemplar_is_feeling():
    """Non-claim-bearing feeling_report as exemplar_id is rejected (cornerstone target)."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for feeling-reject-cs.")
    cs_id = _seed_cs(server, sup, tag="feeling-reject-unified-cs")

    fl_r = json.loads(server.engram_report_feeling(payload_json=json.dumps({
        "reported_state": "curious",
        "trigger": "Testing non-claim-bearing rejection (cs).",
    })))
    assert "error" not in fl_r, fl_r
    fl_id = fl_r["feeling_id"]

    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({"target_id": cs_id, "exemplar_id": fl_id})
    ))
    assert "error" in r, r
    assert "claim-bearing" in r["error"].lower() or "feeling" in r["error"].lower(), r


def test_register_exemplar_rejects_missing_target():
    """Nonexistent target_id is rejected with clear error."""
    server = fresh_server()
    exemplar = _seed_axiom(server, "Exemplar for missing-target test.")

    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({"target_id": "cs_99999", "exemplar_id": exemplar})
    ))
    assert "error" in r, r
    assert "not found" in r["error"].lower() or "cs_99999" in r["error"], r


def test_register_exemplar_rejects_retracted_cornerstone():
    """Retracted cornerstone (is_current=0) is rejected."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for retracted-cs test.")
    cs_id = _seed_cs(server, sup, tag="retracted-unified-cs")

    conn = server._get_db()
    try:
        conn.execute("UPDATE nodes SET is_current = 0 WHERE id = ?", (cs_id,))
        conn.commit()
    finally:
        conn.close()

    exemplar = _seed_axiom(server, "Exemplar for retracted-cs test.")
    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({"target_id": cs_id, "exemplar_id": exemplar})
    ))
    assert "error" in r, r
    assert "superseded" in r["error"].lower() or "is_current" in r["error"].lower(), r


def test_register_exemplar_rejects_retracted_lesson():
    """Retracted lesson (is_current=0) is rejected."""
    server = fresh_server()
    inc = _seed_axiom(server, "Incident for retracted-lesson test.")
    ls_id = _seed_ls(server, "Lesson to retract.", incident_id=inc)

    conn = server._get_db()
    try:
        conn.execute("UPDATE nodes SET is_current = 0 WHERE id = ?", (ls_id,))
        conn.commit()
    finally:
        conn.close()

    exemplar = _seed_axiom(server, "Exemplar for retracted-lesson test.")
    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({"target_id": ls_id, "exemplar_id": exemplar})
    ))
    assert "error" in r, r
    assert "superseded" in r["error"].lower() or "is_current" in r["error"].lower(), r


def test_register_exemplar_rejects_missing_exemplar():
    """Nonexistent exemplar_id is rejected with clear error."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for missing-exemplar test.")
    cs_id = _seed_cs(server, sup, tag="missing-exemplar-unified-cs")

    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({"target_id": cs_id, "exemplar_id": "ax_99999"})
    ))
    assert "error" in r, r
    assert "not found" in r["error"].lower() or "ax_99999" in r["error"], r


def test_register_exemplar_rejects_empty_target_id():
    """Empty target_id is rejected with clear error."""
    server = fresh_server()
    exemplar = _seed_axiom(server, "Exemplar for empty-target-id test.")

    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({"target_id": "", "exemplar_id": exemplar})
    ))
    assert "error" in r, r
    assert "target_id" in r["error"], r


def test_register_exemplar_rejects_empty_exemplar_id():
    """Empty exemplar_id is rejected with clear error."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for empty-exemplar-id test.")
    cs_id = _seed_cs(server, sup, tag="empty-exemplar-unified-cs")

    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({"target_id": cs_id, "exemplar_id": ""})
    ))
    assert "error" in r, r
    assert "exemplar_id" in r["error"], r


def test_register_exemplar_rejects_unknown_field():
    """Payload with extra unknown field returns 'Unknown fields' error."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for unknown-field test.")
    cs_id = _seed_cs(server, sup, tag="unknown-field-unified-cs")
    exemplar = _seed_axiom(server, "Exemplar for unknown-field test.")

    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({
            "target_id": cs_id,
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
    r = json.loads(server.engram_register_exemplar(payload_json="{not-json}"))
    assert "error" in r, r
    assert "Invalid JSON" in r["error"], r


def test_register_exemplar_non_object_payload_rejected():
    """Non-object JSON (e.g., array) in payload_json returns clear error."""
    server = fresh_server()
    r = json.loads(server.engram_register_exemplar(payload_json="[1, 2, 3]"))
    assert "error" in r, r
    assert "must be a JSON object" in r["error"], r


# ---------------------------------------------------------------------------
# §3 Integration with PR #421's zero-support detection
# ---------------------------------------------------------------------------


def test_cornerstone_exemplifies_edge_counts_as_live_support():
    """Exemplifies edge from a live exemplar counts as live support for a cornerstone.

    Setup:
    - cs_X has all supported_by edges pointing to retracted nodes (would normally
      trigger support_lost).
    - Add an exemplifies edge from a LIVE exemplar to cs_X via the unified tool.
    - Retract a downstream node that touches cs_X.

    Expected: cs_X is NOT marked support_lost because the live exemplifies
    edge counts as live support in _detect_zero_support.
    """
    server = fresh_server()
    sole_premise = _seed_axiom(server, "Sole supported_by premise for cs_X.")
    cs_x = _seed_cs(server, sole_premise, tag="exemplifies-live-support-unified-cs")

    live_exemplar = _seed_axiom(server, "Live exemplar that will hold cs_X support.")
    ex_r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({
            "target_id": cs_x,
            "exemplar_id": live_exemplar,
        })
    ))
    assert ex_r.get("status") == "created", ex_r

    ret = json.loads(server._retract_impl(
        node_id=sole_premise,
        error_type="hallucinated_claim",
        reason="Sole premise retracted — exemplifies edge should prevent support_lost.",
    ))
    assert "error" not in ret, ret

    support_lost_ids = [n["id"] for n in ret.get("support_lost", [])]
    assert cs_x not in support_lost_ids, (
        f"cs_x {cs_x} must NOT be in support_lost — live exemplifies edge from "
        f"{live_exemplar} counts as live support. support_lost={support_lost_ids}"
    )

    meta = _get_meta(server, cs_x)
    assert not meta.get("support_lost"), (
        f"cs_x {cs_x} metadata.support_lost must NOT be True: {meta}"
    )
    assert not _is_tainted(server, cs_x, sole_premise), (
        f"cs_x {cs_x} must NOT be tainted_by {sole_premise} — cascade skip rule applies"
    )


def test_cornerstone_support_lost_when_no_exemplifies_either():
    """Support_lost fires when no live supported_by AND no live exemplifies edges.

    Same setup as above but WITHOUT the exemplifies edge — verifies that
    exemplifies counting actually fires (regression guard).
    """
    server = fresh_server()
    sole_premise = _seed_axiom(server, "Sole supported_by premise — no exemplifies (unified).")
    cs_x = _seed_cs(server, sole_premise, tag="no-exemplifies-support-lost-unified-cs")

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
    assert meta.get("support_lost") is True


def test_impl_callable_with_kwargs():
    """_register_exemplar_impl is callable with named kwargs for in-server callers."""
    server = fresh_server()
    sup = _seed_axiom(server, "Support for direct-impl test.")
    cs_id = _seed_cs(server, sup, tag="direct-impl-unified-cs")
    exemplar = _seed_axiom(server, "Exemplar for direct-impl test.")

    r = json.loads(server._register_exemplar_impl(
        target_id=cs_id,
        exemplar_id=exemplar,
        note="Direct-impl smoke test.",
    ))
    assert "error" not in r, r
    assert r.get("status") == "created"


# ---------------------------------------------------------------------------
# §4 Lesson alias backward-compat
# ---------------------------------------------------------------------------


def test_lesson_alias_creates_edge_via_unified_tool():
    """engram_lesson_register_incident delegates to unified tool and creates edge."""
    server = fresh_server()
    inc = _seed_axiom(server, "Incident for alias test.")
    ls_id = _seed_ls(server, "Alias lesson.", incident_id=inc)
    new_inc = _seed_axiom(server, "New incident for alias backward-compat.")

    r = json.loads(server.engram_lesson_register_incident(
        payload_json=json.dumps({
            "lesson_id": ls_id,
            "incident_id": new_inc,
            "note": "Alias backward-compat test.",
        })
    ))
    assert "error" not in r, r
    assert r.get("status") == "created", r
    assert r["edge"]["source"] == new_inc
    assert r["edge"]["target"] == ls_id

    conn = server._get_db()
    try:
        edge = conn.execute(
            """SELECT id FROM edges
               WHERE source_id = ? AND target_id = ? AND relation = 'exemplifies'""",
            (new_inc, ls_id),
        ).fetchone()
    finally:
        conn.close()
    assert edge is not None


def test_lesson_alias_idempotent_returns_noop():
    """Alias: duplicate registration returns 'noop' (legacy contract for lesson callers)."""
    server = fresh_server()
    inc = _seed_axiom(server, "Incident for alias idempotency.")
    ls_id = _seed_ls(server, "Alias idempotency lesson.", incident_id=inc)
    new_inc = _seed_axiom(server, "Incident to register twice via alias.")

    payload = json.dumps({"lesson_id": ls_id, "incident_id": new_inc})
    r1 = json.loads(server.engram_lesson_register_incident(payload_json=payload))
    assert r1.get("status") == "created", r1

    r2 = json.loads(server.engram_lesson_register_incident(payload_json=payload))
    assert r2.get("status") == "noop", r2


def test_lesson_alias_accepts_cornerstone_target_via_unified():
    """Alias: passing a cornerstone as lesson_id succeeds — alias delegates to unified
    tool which accepts both lesson and cornerstone targets. The alias is purely for
    field-name backward compat, not for type restriction.
    """
    server = fresh_server()
    sup = _seed_axiom(server, "Support for alias cornerstone-via-alias test.")
    cs_id = _seed_cs(server, sup, tag="alias-cs-target-cs")
    exemplar = _seed_axiom(server, "Exemplar for alias cornerstone-via-alias test.")

    r = json.loads(server.engram_lesson_register_incident(
        payload_json=json.dumps({"lesson_id": cs_id, "incident_id": exemplar})
    ))
    # Should succeed — unified tool handles cornerstones.
    assert "error" not in r, r
    assert r.get("status") == "created", r
    assert r["edge"]["target"] == cs_id


# ---------------------------------------------------------------------------
# §5 live-count SSoT — exemplar_count is no longer cached in metadata (closes #442)
# ---------------------------------------------------------------------------


def test_response_exemplar_count_matches_live_count():
    """The response exemplar_count matches the SSoT helper after registration."""
    server = fresh_server()
    inc = _seed_axiom(server, "Incident for live-count test.")
    ls_id = _seed_ls(server, "Live-count test lesson.", incident_id=inc)

    new_exemplar = _seed_axiom(server, "New exemplar for live-count verification.")
    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({"target_id": ls_id, "exemplar_id": new_exemplar})
    ))
    assert "error" not in r, r
    assert r.get("status") == "created", r

    conn = server._get_db()
    try:
        live_count = server._count_live_exemplars(conn, ls_id, "lesson")
    finally:
        conn.close()

    assert r["exemplar_count"] == live_count, (
        f"Response exemplar_count {r['exemplar_count']} != live helper {live_count}"
    )
    # No cached exemplar_count / incident_count field in metadata (retired).
    meta = _get_meta(server, ls_id)
    assert "exemplar_count" not in meta, f"exemplar_count should not be cached: {meta}"
    assert "incident_count" not in meta, f"incident_count should not be cached: {meta}"


def test_new_lesson_metadata_has_no_cached_count():
    """A freshly-created lesson has no exemplar_count / incident_count in metadata."""
    server = fresh_server()
    inc = _seed_axiom(server, "Incident for no-cache metadata test.")
    ls_id = _seed_ls(server, "No-cached-count lesson.", incident_id=inc)

    meta = _get_meta(server, ls_id)
    assert "exemplar_count" not in meta, f"Should not be cached on creation: {meta}"
    assert "incident_count" not in meta, f"Should not be cached on creation: {meta}"


def test_remove_exemplifies_edge_live_count_lesson():
    """Live count decreases after edge removal — SSoT helper verifies (closes #442)."""
    server = fresh_server()
    inc = _seed_axiom(server, "Edge-removal live-count incident.")
    ls_id = _seed_ls(server, "Edge-removal live-count lesson.", incident_id=inc)

    # Register an additional exemplar — now 2 live exemplars.
    new_exemplar = _seed_axiom(server, "Exemplar to be removed.")
    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({"target_id": ls_id, "exemplar_id": new_exemplar})
    ))
    assert r.get("status") == "created", r

    conn = server._get_db()
    try:
        pre_count = server._count_live_exemplars(conn, ls_id, "lesson")
    finally:
        conn.close()
    assert pre_count == 2, f"Expected 2 before removal, got {pre_count}"

    # Remove the edge.
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": new_exemplar, "target_id": ls_id, "relation": "exemplifies",
    })))
    assert "error" not in r, r

    conn = server._get_db()
    try:
        post_count = server._count_live_exemplars(conn, ls_id, "lesson")
    finally:
        conn.close()
    assert post_count == 1, f"Expected 1 after removal, got {post_count}"


def test_remove_exemplifies_edge_live_count_cornerstone():
    """Live count decreases after edge removal for cornerstone targets (closes #442)."""
    server = fresh_server()
    cs_id = _seed_cs(server, "Cornerstone for edge-removal live-count test.")
    ex = _seed_axiom(server, "Cornerstone exemplar to be removed.")
    r = json.loads(server.engram_register_exemplar(
        payload_json=json.dumps({"target_id": cs_id, "exemplar_id": ex})
    ))
    assert r.get("status") == "created", r

    conn = server._get_db()
    try:
        pre_count = server._count_live_exemplars(conn, cs_id, "cornerstone")
    finally:
        conn.close()
    assert pre_count == 1, f"Expected 1 before removal, got {pre_count}"

    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": ex, "target_id": cs_id, "relation": "exemplifies",
    })))
    assert "error" not in r, r

    conn = server._get_db()
    try:
        post_count = server._count_live_exemplars(conn, cs_id, "cornerstone")
    finally:
        conn.close()
    assert post_count == 0, f"Expected 0 after removal, got {post_count}"
