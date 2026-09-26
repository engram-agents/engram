"""Tests for engram_retract single-payload signature (wave 2b of ob_1499).

Coverage:
  1. Happy path — well-formed payload retracts the node.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing node_id / error_type / reason rejected.
  4. error_type validation — invalid error_type rejected.
  5. Internal helper — _retract_impl works with named kwargs.
  6. Retract deletes resolves edges (issue #211, Option A).
  7. Self-anchor retract warning (issue #1200).
  8. Initiation provenance fields (issue #1099 C1).
  9. replacement_id — purely relational (#2612): the replacement node is
     created by the caller via its own canonical creation tool BEFORE
     calling engram_retract, then named by id. Covers the four up-front
     validations (missing / non-current / self-reference / older-than-target
     — the last because the retracts edge is temporal_causal=True), the
     retracts edge landing in the retraction's own transaction, and the
     targeted migration error for a payload still carrying the retired
     replacement_json field.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_retract_")
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


def _seed_observation(server):
    payload = {
        "url": "https://example.com/a", "title": "A",
        "quoted_text": "Test quote.",
        "interpretation": "Test interp.",
        "claim": "Test claim.",
        "quote_type": "attributed_analysis",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    return r["observation_id"]


# ── 1. Happy path ──────────────────────────────────────────────────────────


def test_payload_json_retracts_node():
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "hallucinated_claim",
        "reason": "Claim isn't supported by the cited evidence.",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("retracted_id") == ob_id or r.get("node_id") == ob_id, r


def test_payload_json_with_replacement_id():
    """replacement_id names a node the caller already created (purely relational)."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    repl_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "wrong_citation",
        "reason": "Original cited the wrong passage.",
        "replacement_id": repl_id,
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("replacement_id") == repl_id, r


def test_payload_json_replacement_json_migration_error():
    """A payload still carrying the retired replacement_json gets a targeted
    migration error, not the generic unknown-field rejection."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "wrong_citation",
        "reason": "x",
        "replacement_json": json.dumps({"claim": "irrelevant"}),
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "replacement_json" in r["error"], r
    assert "replacement_id" in r["error"], r
    assert "Unknown fields" not in r["error"], r


def test_payload_json_replacement_json_migration_error_with_unknown_field():
    """Compound payload: retired replacement_json PLUS an unrelated bogus
    field. The targeted migration error must still win — it holds today
    because the migration check runs before the unknown-field check, but
    nothing pins that order; a reorder would silently start returning the
    generic unknown-field error instead."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "wrong_citation",
        "reason": "x",
        "replacement_json": json.dumps({"claim": "irrelevant"}),
        "bogus_field": "should not matter here",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "replacement_json" in r["error"], r
    assert "replacement_id" in r["error"], r
    assert "Unknown fields" not in r["error"], r


# ── 2. Schema enforcement ──────────────────────────────────────────────────


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_retract(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_retract(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id, "error_type": "other", "reason": "x",
        "bogus_field": "should reject",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


# ── 3. Required-field validation ───────────────────────────────────────────


def test_payload_json_missing_node_id_rejected():
    server = fresh_server()
    payload = {"error_type": "other", "reason": "x"}
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r and "node_id" in r["error"], r


def test_payload_json_missing_error_type_rejected():
    server = fresh_server()
    payload = {"node_id": "ob_0001", "reason": "x"}
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r and "error_type" in r["error"], r


def test_payload_json_missing_reason_rejected():
    server = fresh_server()
    payload = {"node_id": "ob_0001", "error_type": "other"}
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r and "reason" in r["error"], r


# ── 4. error_type validation ───────────────────────────────────────────────


def test_payload_json_invalid_error_type_rejected():
    server = fresh_server()
    payload = {
        "node_id": "ob_0001",
        "error_type": "bogus_error_type",
        "reason": "x",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r and "Invalid error_type" in r["error"], r


# ── 5. Internal helper still callable with kwargs ──────────────────────────


def test_impl_callable_with_kwargs():
    server = fresh_server()
    ob_id = _seed_observation(server)
    r = json.loads(server._retract_impl(
        node_id=ob_id,
        error_type="other",
        reason="kwargs-path test",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement decision not under test",
    ))
    assert "error" not in r, r


# ── 6. Retract deletes resolves edges (issue #211, Option A) ───────────────


def _count_resolves_edges(server, source_id):
    """Return the number of resolves edges whose source is source_id."""
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id = ? AND relation = 'resolves'",
            (source_id,),
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def _seed_question_and_resolver(server, question_text="Is X the case?"):
    """Create an observation, a question, a resolving derivation, and wire the resolves edge.

    Returns (question_id, derivation_id).
    """
    ob = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/source",
        "title": "Source",
        "quoted_text": "X is known to be the case.",
        "interpretation": "Evidence that X holds.",
        "claim": "X is the case.",
        "quote_type": "hard_data",
    })))
    ob_id = ob["observation_id"]

    q = json.loads(server.engram_ask(payload_json=json.dumps({"question": question_text})))
    q_id = q["question_id"]

    dv = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "X is the case based on the source.",
        "supporting_ids": ob_id,
        "logical_chain": "Source attests X therefore X holds.",
        "reasoning_type": "authority_expert",
    })))
    dv_id = dv["derivation_id"]

    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": dv_id,
    })))
    assert r.get("status") == "resolution_created", f"Resolution failed: {r}"
    return q_id, dv_id


def test_retract_resolution_deletes_resolves_edges():
    """Retracting a resolver derivation must delete its outgoing resolves edge."""
    server = fresh_server()
    q_id, dv_id = _seed_question_and_resolver(server)

    # Verify edge exists before retraction
    assert _count_resolves_edges(server, dv_id) == 1, (
        f"Expected 1 resolves edge from {dv_id} before retraction"
    )

    ret = json.loads(server._retract_impl(
        node_id=dv_id,
        error_type="hallucinated_claim",
        reason="Resolver derivation was based on a hallucinated premise.",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement decision not under test",
    ))
    assert "error" not in ret, ret

    # Resolves edge must be gone after retraction
    edge_count = _count_resolves_edges(server, dv_id)
    assert edge_count == 0, f"Expected 0 edges after retract, got {edge_count}"


def test_retract_resolution_with_other_valid_resolution_preserves_others():
    """Retracting one resolver must NOT delete the other resolver's resolves edge."""
    server = fresh_server()
    q_id, dv_id_first = _seed_question_and_resolver(server, "Is Y the case?")

    # Wire a second independent resolver to the same question
    ob2 = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/source2",
        "title": "Source 2",
        "quoted_text": "Y is confirmed by independent measurement.",
        "interpretation": "Independent confirmation that Y holds.",
        "claim": "Y is the case (independent source).",
        "quote_type": "hard_data",
    })))
    dv2 = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Y is the case per independent measurement.",
        "supporting_ids": ob2["observation_id"],
        "logical_chain": "Independent measurement confirms Y.",
        "reasoning_type": "authority_expert",
    })))
    dv_id_second = dv2["derivation_id"]
    r2 = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": dv_id_second,
    })))
    assert r2.get("status") == "resolution_created", f"Second resolution failed: {r2}"

    # Both resolves edges exist before any retraction
    assert _count_resolves_edges(server, dv_id_first) == 1
    assert _count_resolves_edges(server, dv_id_second) == 1

    # Retract the first resolver
    ret = json.loads(server._retract_impl(
        node_id=dv_id_first,
        error_type="wrong_evidence",
        reason="First resolver cited the wrong source.",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement decision not under test",
    ))
    assert "error" not in ret, ret

    # First resolver's edge is gone; second resolver's edge survives
    assert _count_resolves_edges(server, dv_id_first) == 0, (
        f"First resolver's resolves edge should be deleted"
    )
    assert _count_resolves_edges(server, dv_id_second) == 1, (
        f"Second resolver's resolves edge must not be touched"
    )


def test_retract_resolution_multiple_targets_deletes_all_own_edges():
    """Retracting a resolver that resolves multiple targets deletes ALL its resolves edges.

    Setup: one derivation wired to TWO questions via two separate resolves edges.
    This exercises the multi-row DELETE path (source_id = dv_id, relation = 'resolves'
    matches two rows), which is what the test name claims to cover.
    """
    server = fresh_server()

    # One supporting observation
    ob = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/source",
        "title": "Source",
        "quoted_text": "Both A and B are settled by the same evidence.",
        "interpretation": "Evidence settles both open questions.",
        "claim": "The evidence settles both questions.",
        "quote_type": "hard_data",
    })))
    ob_id = ob["observation_id"]

    # Two questions
    q1 = json.loads(server.engram_ask(payload_json=json.dumps({"question": "Is question-A answered?"})))
    q2 = json.loads(server.engram_ask(payload_json=json.dumps({"question": "Is question-B answered?"})))
    q1_id = q1["question_id"]
    q2_id = q2["question_id"]

    # ONE derivation that answers BOTH questions
    dv = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Both question-A and question-B are answered by the evidence.",
        "supporting_ids": ob_id,
        "logical_chain": "Evidence directly addresses both questions simultaneously.",
        "reasoning_type": "authority_expert",
    })))
    dv_id = dv["derivation_id"]

    # Wire the SAME derivation to both targets — produces two separate resolves edges
    r1 = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q1_id, "resolving_node_id": dv_id,
    })))
    assert r1.get("status") == "resolution_created", f"First resolution failed: {r1}"

    r2 = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q2_id, "resolving_node_id": dv_id,
    })))
    assert r2.get("status") == "resolution_created", f"Second resolution failed: {r2}"

    # dv now has TWO outgoing resolves edges (the multi-row DELETE path)
    assert _count_resolves_edges(server, dv_id) == 2, (
        f"Expected 2 resolves edges from {dv_id} before retraction, "
        f"got {_count_resolves_edges(server, dv_id)}"
    )

    # Retract the single derivation
    ret = json.loads(server._retract_impl(
        node_id=dv_id,
        error_type="hallucinated_claim",
        reason="Derivation was hallucinated.",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement decision not under test",
    ))
    assert "error" not in ret, ret

    # Both edges are gone — DELETE matched both rows
    assert _count_resolves_edges(server, dv_id) == 0, (
        f"Expected 0 resolves edges from {dv_id} after retraction, "
        f"got {_count_resolves_edges(server, dv_id)}"
    )

    # Both targets are reopened (neither has another valid resolver)
    conn = server._get_db()
    try:
        q1_status = conn.execute("SELECT status FROM nodes WHERE id = ?", (q1_id,)).fetchone()["status"]
        q2_status = conn.execute("SELECT status FROM nodes WHERE id = ?", (q2_id,)).fetchone()["status"]
    finally:
        conn.close()
    assert q1_status == "open", f"q1 should be reopened, got status={q1_status!r}"
    assert q2_status == "open", f"q2 should be reopened, got status={q2_status!r}"


def test_retract_non_resolution_node_no_resolves_deletion():
    """Retracting a plain observation (no resolves edges) must not error and must not disturb unrelated edges."""
    server = fresh_server()

    # Create a plain observation (no resolves edge) and a second observation
    ob1 = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/plain",
        "title": "Plain",
        "quoted_text": "Plain fact.",
        "interpretation": "A plain observation.",
        "claim": "Plain fact is true.",
        "quote_type": "hard_data",
    })))
    ob1_id = ob1["observation_id"]

    # Also set up an unrelated resolver so we can verify it's untouched
    q_id, dv_id = _seed_question_and_resolver(server, "Unrelated question?")
    assert _count_resolves_edges(server, dv_id) == 1

    # Retract the plain observation (has no resolves edges)
    ret = json.loads(server._retract_impl(
        node_id=ob1_id,
        error_type="other",
        reason="Sanity check retraction of non-resolver node.",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement decision not under test",
    ))
    assert "error" not in ret, ret

    # The DELETE is scoped to ob1_id — unrelated resolver edge must survive
    assert _count_resolves_edges(server, dv_id) == 1, (
        f"Unrelated resolver's resolves edge must not be touched by retracting {ob1_id}"
    )


# ── 7. Self-anchor retract warning (issue #1200) ───────────────────────────


def test_retract_self_anchor_warns():
    server = fresh_server()
    import json
    # Create the self-anchor person node
    person = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "Test Agent", "role": "agent", "is_self": True
    })))
    person_id = person["person_id"]
    # Retract it
    result = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": person_id,
        "error_type": "other",
        "reason": "Filed in error — test",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    })))
    assert result.get("status") == "retracted", f"expected retracted, got {result}"
    assert "warning" in result, f"expected warning key in result, got {result}"
    assert "is_self" in result["warning"]
    assert "zero current self-anchors" in result["warning"]


def test_retract_non_self_person_no_warning():
    server = fresh_server()
    import json
    # Create a regular (non-self) person node
    person = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "Collaborator", "role": "collaborator"
    })))
    person_id = person["person_id"]
    result = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": person_id,
        "error_type": "other",
        "reason": "Filed in error — test",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    })))
    assert result.get("status") == "retracted"
    assert "warning" not in result, f"unexpected warning in result: {result.get('warning')}"


def test_retract_self_anchor_still_retracts():
    server = fresh_server()
    import json
    import sqlite3
    person = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "Test Agent 2", "role": "agent", "is_self": True
    })))
    person_id = person["person_id"]
    result = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": person_id,
        "error_type": "other",
        "reason": "Filed in error — test",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    })))
    assert result.get("status") == "retracted"
    # Verify the DB row is actually retracted
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status, is_current FROM nodes WHERE id = ?", (person_id,)).fetchone()
    assert row["status"] == "retracted"
    assert row["is_current"] == 0


# ── 8. Initiation provenance fields ───────────────────────────────────────


def test_initiation_user_prompted_stored():
    """initiation='user_prompted' is stored in metadata and echoed in result."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "other",
        "reason": "Test initiation field.",
        "initiation": "user_prompted",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("initiation") == "user_prompted", r
    # Verify it's in DB metadata
    import sqlite3
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (ob_id,)).fetchone()
    meta = json.loads(row["metadata"])
    assert meta.get("initiation") == "user_prompted", meta


def test_initiation_gate_prompted_with_gate():
    """initiation='gate_prompted' + initiation_gate stored and echoed."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "hallucinated_claim",
        "reason": "Gate flagged this as unsupported.",
        "initiation": "gate_prompted",
        "initiation_gate": "honesty_axiom",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("initiation") == "gate_prompted", r
    assert r.get("initiation_gate") == "honesty_axiom", r
    import sqlite3
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (ob_id,)).fetchone()
    meta = json.loads(row["metadata"])
    assert meta.get("initiation") == "gate_prompted", meta
    assert meta.get("initiation_gate") == "honesty_axiom", meta


def test_initiation_unprompted_stored():
    """initiation='unprompted' is stored and echoed."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "duplicate",
        "reason": "Self-identified as duplicate during routine scan.",
        "initiation": "unprompted",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("initiation") == "unprompted", r


def test_initiation_omitted_leaves_no_key():
    """When initiation is omitted, result has no 'initiation' key."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "other",
        "reason": "No initiation supplied.",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "initiation" not in r, f"expected no initiation key, got: {r}"


def test_initiation_invalid_value_rejected():
    """An invalid initiation value is rejected before the retraction executes."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "other",
        "reason": "x",
        "initiation": "self_initiated",  # invalid — not in VALID_INITIATION_TYPES
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "Invalid initiation" in r["error"], r


def test_initiation_ref_auto_captured_in_metadata():
    """initiation_ref is auto-captured by the server wrapper and stored in metadata."""
    import sqlite3
    server = fresh_server()
    ob_id = _seed_observation(server)
    # Set an env var so we can verify the capture path
    os.environ["CLAUDE_SESSION_ID"] = "test-session-abc123"
    try:
        payload = {
            "node_id": ob_id,
            "error_type": "other",
            "reason": "Test session capture.",
            "initiation": "unprompted",
            "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
            "replacement_absent_note": "test fixture -- replacement decision not under test",
        }
        r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
        assert "error" not in r, r
        # initiation_ref echoed in result
        assert r.get("initiation_ref") == "test-session-abc123", r
        # and stored in DB metadata
        db_path = os.path.join(TEST_DIR, "knowledge.db")
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (ob_id,)).fetchone()
        meta = json.loads(row["metadata"])
        assert meta.get("initiation_ref") == "test-session-abc123", meta
    finally:
        os.environ.pop("CLAUDE_SESSION_ID", None)


def test_initiation_ref_not_user_supplied():
    """initiation_ref is NOT accepted as a user-supplied field (unknown-field rejection)."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "other",
        "reason": "x",
        "initiation_ref": "user-tried-to-supply-this",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "initiation_ref" in r["error"], r


def test_initiation_gate_without_gate_prompted_rejected():
    """initiation_gate supplied with initiation != 'gate_prompted' is rejected."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    # gate with user_prompted — contradictory combination
    payload = {
        "node_id": ob_id,
        "error_type": "other",
        "reason": "x",
        "initiation": "user_prompted",
        "initiation_gate": "honesty_axiom",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "initiation_gate" in r["error"] and "gate_prompted" in r["error"], r


def test_initiation_gate_without_initiation_rejected():
    """initiation_gate with no initiation field is also rejected (not gate_prompted)."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "other",
        "reason": "x",
        "initiation_gate": "honesty_axiom",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "initiation_gate" in r["error"] and "gate_prompted" in r["error"], r


def test_initiation_ref_captured_without_initiation_field():
    """initiation_ref is captured from env even when no initiation label is supplied."""
    import sqlite3
    server = fresh_server()
    ob_id = _seed_observation(server)
    os.environ["CLAUDE_SESSION_ID"] = "session-no-initiation"
    try:
        payload = {
            "node_id": ob_id,
            "error_type": "other",
            "reason": "No initiation label, but session should still be captured.",
            "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
            "replacement_absent_note": "test fixture -- replacement decision not under test",
        }
        r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
        assert "error" not in r, r
        assert r.get("initiation_ref") == "session-no-initiation", r
        db_path = os.path.join(TEST_DIR, "knowledge.db")
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (ob_id,)).fetchone()
        meta = json.loads(row["metadata"])
        assert meta.get("initiation_ref") == "session-no-initiation", meta
    finally:
        os.environ.pop("CLAUDE_SESSION_ID", None)


def test_initiation_ref_anthropic_session_id_fallback():
    """ANTHROPIC_SESSION_ID is used as fallback when CLAUDE_SESSION_ID is absent."""
    import sqlite3
    server = fresh_server()
    ob_id = _seed_observation(server)
    os.environ.pop("CLAUDE_SESSION_ID", None)
    os.environ["ANTHROPIC_SESSION_ID"] = "anthropic-fallback-xyz"
    try:
        payload = {
            "node_id": ob_id,
            "error_type": "other",
            "reason": "Test ANTHROPIC_SESSION_ID fallback path.",
            "initiation": "unprompted",
            "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
            "replacement_absent_note": "test fixture -- replacement decision not under test",
        }
        r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
        assert "error" not in r, r
        assert r.get("initiation_ref") == "anthropic-fallback-xyz", r
        db_path = os.path.join(TEST_DIR, "knowledge.db")
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (ob_id,)).fetchone()
        meta = json.loads(row["metadata"])
        assert meta.get("initiation_ref") == "anthropic-fallback-xyz", meta
    finally:
        os.environ.pop("ANTHROPIC_SESSION_ID", None)


# ── 9. replacement_id validation (#2612) ────────────────────────────────────


def test_replacement_id_missing_node_rejected():
    """replacement_id naming a nonexistent node is refused, no mutation performed."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "wrong_citation",
        "reason": "x",
        "replacement_id": "ob_9999999",
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "replacement_id" in r["error"] and "not found" in r["error"], r
    # No mutation performed — node still current.
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT status, is_current FROM nodes WHERE id = ?", (ob_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["is_current"] == 1, "retract must not have mutated the node"
    assert row["status"] != "retracted"


def test_replacement_id_non_current_node_rejected():
    """replacement_id naming an already-superseded/retracted node is refused."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    stale_repl_id = _seed_observation(server)
    # Retract the would-be replacement first, so it's no longer current.
    stale_retract = json.loads(server._retract_impl(
        node_id=stale_repl_id,
        error_type="other",
        reason="Make this node non-current for the test.",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement decision not under test",
    ))
    assert "error" not in stale_retract, stale_retract

    payload = {
        "node_id": ob_id,
        "error_type": "wrong_citation",
        "reason": "x",
        "replacement_id": stale_repl_id,
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "replacement_id" in r["error"] and "not current" in r["error"], r
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT status, is_current FROM nodes WHERE id = ?", (ob_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["is_current"] == 1, "retract must not have mutated the target node"


def test_replacement_id_self_reference_rejected():
    """replacement_id equal to node_id is refused."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "wrong_citation",
        "reason": "x",
        "replacement_id": ob_id,
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "must differ" in r["error"], r
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT status, is_current FROM nodes WHERE id = ?", (ob_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["is_current"] == 1, "retract must not have mutated the node"


def test_replacement_id_older_than_target_rejected():
    """replacement_id naming a node OLDER than node_id is refused — the
    retracts edge (replacement -> retracted) is temporal_causal=True, so a
    replacement that predates the node it corrects would violate that
    relation's own declared invariant."""
    server = fresh_server()
    # Create the would-be replacement FIRST, so it is strictly older than
    # the node created next.
    repl_id = _seed_observation(server)
    ob_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "wrong_citation",
        "reason": "x",
        "replacement_id": repl_id,
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "temporal_causal" in r["error"], r
    assert "retracts" in r["error"], r
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT status, is_current FROM nodes WHERE id = ?", (ob_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["is_current"] == 1, "retract must not have mutated the node"


def test_replacement_id_edge_written_in_same_transaction():
    """The retracts edge lands alongside the retraction itself — both visible
    immediately after the call, on one connection's worth of writes."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    repl_id = _seed_observation(server)
    payload = {
        "node_id": ob_id,
        "error_type": "wrong_citation",
        "reason": "Original cited the wrong passage.",
        "replacement_id": repl_id,
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("replacement_id") == repl_id, r

    conn = server._get_db()
    try:
        node_row = conn.execute(
            "SELECT status, is_current FROM nodes WHERE id = ?", (ob_id,)
        ).fetchone()
        edge_row = conn.execute(
            "SELECT COUNT(*) AS c FROM edges WHERE source_id = ? AND target_id = ? "
            "AND relation = 'retracts'",
            (repl_id, ob_id),
        ).fetchone()
    finally:
        conn.close()
    assert node_row["status"] == "retracted"
    assert node_row["is_current"] == 0
    assert edge_row["c"] == 1, "retracts edge must exist alongside the retraction"


def _seed_dv(server, claim, supporting_ids):
    """Create a derivation via the engram_derive facade — supporting_ids may
    be a single node id or a list of them."""
    if isinstance(supporting_ids, list):
        supporting_ids = ",".join(supporting_ids)
    payload = {
        "claim": claim,
        "supporting_ids": supporting_ids,
        "logical_chain": f"Derivation: {claim}",
        "reasoning_type": "deductive_modus_ponens",
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["derivation_id"]


# ── 10. retract_replacement_is_downstream guard (round-3 review of #2612) ───
#
# Seeded graph, per the reviewing colleague's reproduction:
#   O (observation) <- P (derivation) <- X (derivation)
#   edges: P --derives_from--> O ,  X --derives_from--> P
#
# CONTROL: engram_supersede(old=P, new=X) must be REFUSED by the pre-existing
# §12.3 guard ("supersede_replacement_is_downstream") — this is the positive
# control proving the fixture itself is capable of tripping the guard.
# SUBJECT: engram_retract(node_id=P, replacement_id=X) must ALSO be refused,
# by the new guard ("retract_replacement_is_downstream") ported in this PR.


def _seed_downstream_chain(server):
    o_id = _seed_observation(server)
    p_id = _seed_dv(server, "P derives from O.", o_id)
    x_id = _seed_dv(server, "X derives from P.", p_id)
    return o_id, p_id, x_id


def test_supersede_replacement_is_downstream_control():
    """Positive control: the pre-existing supersede guard refuses replacing
    P with its own descendant X. Without this passing, a refusal on the
    retract path below cannot be distinguished from a broken fixture."""
    server = fresh_server()
    _o_id, p_id, x_id = _seed_downstream_chain(server)

    payload = {"old_node_id": p_id, "new_node_id": x_id}
    r = json.loads(server.engram_supersede(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert r.get("guard") == "supersede_replacement_is_downstream", r


def test_retract_replacement_is_downstream_rejected():
    """Subject: retract(node_id=P, replacement_id=X) must be refused by the
    new guard — naming a pre-existing downstream node as replacement_id is
    the shape that makes the replacement stale by the act that installs it,
    and installs a cascade marker on the very node the response also reports
    as the replacement."""
    server = fresh_server()
    _o_id, p_id, x_id = _seed_downstream_chain(server)

    payload = {
        "node_id": p_id,
        "error_type": "hallucinated_claim",
        "reason": "x",
        "replacement_id": x_id,
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert r.get("guard") == "retract_replacement_is_downstream", r

    # No mutation performed — P must still be current, no retracts edge, no
    # pending cascade marker installed on X.
    conn = server._get_db()
    try:
        p_row = conn.execute(
            "SELECT status, is_current FROM nodes WHERE id = ?", (p_id,)
        ).fetchone()
        edge_row = conn.execute(
            "SELECT COUNT(*) AS c FROM edges WHERE source_id = ? AND target_id = ? "
            "AND relation = 'retracts'",
            (x_id, p_id),
        ).fetchone()
        marker_row = conn.execute(
            "SELECT COUNT(*) AS c FROM cascade_markers WHERE node_id = ?",
            (x_id,),
        ).fetchone()
    finally:
        conn.close()
    assert p_row["is_current"] == 1, "retract must not have mutated the node"
    assert p_row["status"] != "retracted"
    assert edge_row["c"] == 0, "no retracts edge should have been written"
    assert marker_row["c"] == 0, "no cascade marker should have landed on X"


def test_retract_replacement_id_non_downstream_still_succeeds():
    """A legitimate, NON-downstream replacement_id still succeeds — proves
    the new guard isn't vacuously refusing every replacement_id retract."""
    server = fresh_server()
    _o_id, p_id, _x_id = _seed_downstream_chain(server)
    # An unrelated, freshly-created observation — not on P's cascade at all.
    repl_id = _seed_observation(server)

    payload = {
        "node_id": p_id,
        "error_type": "hallucinated_claim",
        "reason": "Corrected by an unrelated, non-downstream replacement.",
        "replacement_id": repl_id,
    }
    r = json.loads(server.engram_retract(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("replacement_id") == repl_id, r

    conn = server._get_db()
    try:
        p_row = conn.execute(
            "SELECT status, is_current FROM nodes WHERE id = ?", (p_id,)
        ).fetchone()
    finally:
        conn.close()
    assert p_row["status"] == "retracted"
    assert p_row["is_current"] == 0
