"""Tests for cascade semantics fix (PR A) — §5 of PR-A-SPEC.

Coverage:
  §5.1 Retract cascade — preserved correct behavior (non-cs/ls types still cascade)
  §5.2 Retract cascade — new correct behavior (cs/ls types skip taint + recursion)
  §5.3 Supersede cascade — mirror of §5.1/§5.2 for stale_by
  §5.4 Zero-support detection (retract and supersede paths; exemplifies edge)
  §5.5 Migration script (clear old markers; idempotency; support_lost on zero support)
  §5.6 Reflect surfacing (support_lost_nodes in engram_reflect output)
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOOLS_MIGRATION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "migration",
)
sys.path.insert(0, TOOLS_MIGRATION_DIR)

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_cascade_semantics_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


# ---------------------------------------------------------------------------
# Server factory helpers
# ---------------------------------------------------------------------------

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


def _seed_ob(server, claim="Test claim.", url="https://example.com/src",
             quote_type="attributed_analysis"):
    """Create an observation. All observations share the same evidence URL by
    default to avoid multiple DNS lookups in the test environment."""
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


def _seed_dv(server, claim, supporting_ids):
    if isinstance(supporting_ids, list):
        supporting_ids = ",".join(supporting_ids)
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": claim,
        "supporting_ids": supporting_ids,
        "logical_chain": f"Derivation: {claim}",
        "reasoning_type": "deductive_modus_ponens",
    })))
    assert "error" not in r, r
    return r["derivation_id"]


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


def _seed_ls(server, claim="Lesson pattern.", supporting_ids=None):
    """Create a lesson with the given incident observations as exemplifiers.

    engram_add_lesson requires incident_ids (comma-separated). Those observations
    are wired to the lesson via exemplifies edges by _add_lesson_impl.
    """
    if supporting_ids is None:
        # Lessons require at least one incident; create a placeholder.
        supporting_ids = _seed_ob(server, f"Placeholder incident for: {claim}",
                                  url="https://example.com/placeholder")
    if isinstance(supporting_ids, list):
        supporting_ids = ",".join(supporting_ids)

    r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": claim,
        "incident_ids": supporting_ids,
        "logical_chain": f"Pattern derived from incidents: {claim}",
        "scaffolding_nudge": "Watch for this pattern.",
    })))
    assert "error" not in r, r
    return r["lesson_id"]


def _is_tainted(server, node_id, by_id):
    conn = server._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return False
        meta = json.loads(row["metadata"] or "{}")
        return by_id in meta.get("tainted_by", [])
    finally:
        conn.close()


def _is_stale(server, node_id, by_id):
    conn = server._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return False
        meta = json.loads(row["metadata"] or "{}")
        return by_id in meta.get("stale_by", [])
    finally:
        conn.close()


def _get_meta(server, node_id):
    conn = server._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return json.loads(row["metadata"] or "{}") if row else {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# §5.1 Retract cascade — preserved correct behavior
# ---------------------------------------------------------------------------


def test_retract_ob_with_downstream_dv_propagates_taint():
    """Retracting ob_X; dv_Y with derives_from→ob_X gets tainted_by:[ob_X].

    Derivation chains still cascade — only cs/ls are skipped.
    """
    server = fresh_server()
    ob_id = _seed_ob(server, "Premise A.")
    dv_id = _seed_dv(server, "Derived from A.", ob_id)

    ret = json.loads(server._retract_impl(
        node_id=ob_id,
        error_type="hallucinated_claim",
        reason="Premise was fabricated.",
    ))
    assert "error" not in ret, ret

    assert _is_tainted(server, dv_id, ob_id), (
        f"dv_id {dv_id} should be tainted_by {ob_id} after retraction"
    )
    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert dv_id in tainted_ids, f"{dv_id} should appear in tainted_downstream"


def test_retract_dv_with_downstream_dv_propagates_taint():
    """Retraction cascade walks derivation chains correctly."""
    server = fresh_server()
    ob_id = _seed_ob(server, "Root premise.")
    dv1_id = _seed_dv(server, "Intermediate derivation.", ob_id)
    dv2_id = _seed_dv(server, "Downstream derivation.", dv1_id)

    ret = json.loads(server._retract_impl(
        node_id=dv1_id,
        error_type="hallucinated_claim",
        reason="Intermediate derivation was wrong.",
    ))
    assert "error" not in ret, ret
    assert _is_tainted(server, dv2_id, dv1_id), (
        f"dv2_id {dv2_id} should be tainted_by {dv1_id}"
    )


# ---------------------------------------------------------------------------
# §5.2 Retract cascade — new correct behavior (cs/ls skip)
# ---------------------------------------------------------------------------


def test_retract_ob_with_supported_by_edge_from_cs_does_NOT_taint_cs():
    """Retracting ob_X must NOT taint cs_Y that has supported_by→ob_X.

    Cascade skip rule: cs accumulates instance-votes; one retracted instance
    does not invalidate the pattern.
    """
    server = fresh_server()
    ob1 = _seed_ob(server, "Instance vote 1.")
    ob2 = _seed_ob(server, "Instance vote 2.")  # second supporter so not zero-support
    cs_id = _seed_cs(server, [ob1, ob2], tag="multi-support-cs")

    ret = json.loads(server._retract_impl(
        node_id=ob1,
        error_type="hallucinated_claim",
        reason="ob1 was fabricated.",
    ))
    assert "error" not in ret, ret

    assert not _is_tainted(server, cs_id, ob1), (
        f"cs {cs_id} must NOT be tainted_by {ob1} — cascade skip rule applies"
    )
    # cs must not appear in tainted_downstream
    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert cs_id not in tainted_ids, f"cs {cs_id} must not be in tainted_downstream"


def test_retract_ob_with_exemplifies_edge_to_ls_does_NOT_taint_ls():
    """Retracting ob_X must NOT taint ls_Y with incoming exemplifies←ob_X.

    The exemplifies edge goes FROM the instance TO the lesson. The cascade
    skip rule applies to the lesson when ob_X is retracted.
    """
    server = fresh_server()
    ob1 = _seed_ob(server, "Incident A — exemplifies lesson.")
    ob2 = _seed_ob(server, "Incident B — also exemplifies lesson.")
    ls_id = _seed_ls(server, "Pattern: X leads to Y.", supporting_ids=f"{ob1},{ob2}")

    ret = json.loads(server._retract_impl(
        node_id=ob1,
        error_type="hallucinated_claim",
        reason="ob1 was fabricated.",
    ))
    assert "error" not in ret, ret

    assert not _is_tainted(server, ls_id, ob1), (
        f"lesson {ls_id} must NOT be tainted_by {ob1} — cascade skip rule applies"
    )
    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert ls_id not in tainted_ids, f"lesson {ls_id} must not be in tainted_downstream"


def test_retract_ob_does_not_recurse_through_cs():
    """Cascade stops at cs; does NOT mark other cs supporters as collateral taint.

    Setup:
      ob_A ←[supported_by]← cs
      ob_B ←[supported_by]← cs  (cs also supported_by ob_B — unrelated supporter)
      ob_dv_premise ←[derives_from]← dv_indirect  (dv that is unrelated to cs)

    Retract ob_A. The cascade skip rule prevents cs from being tainted.
    ob_B and dv_indirect must also NOT be tainted — cascade stops at cs and
    does not "pass through" to infect unrelated nodes that are only related
    via the cs's own supporting chain.

    Note: the cascade walks source_id→target_id in ('derives_from','supported_by').
    cs is SOURCE of supported_by→ob_A (target). Retracting ob_A finds cs as SOURCE.
    With skip: cs is not tainted AND we do NOT recurse from cs. Without skip, we
    would call _find_downstream(cs), which would look for nodes where cs is TARGET.
    There are none — so the non-recursion is implicitly verified by cs not being tainted.
    The test verifies the primary contract (cs not tainted) and that tainted_downstream
    does not include cs.
    """
    server = fresh_server()
    ob_a = _seed_ob(server, "Retracted premise A.")
    ob_b = _seed_ob(server, "Live premise B.")
    # cs supported_by both A and B
    cs_id = _seed_cs(server, [ob_a, ob_b], tag="recursion-stop-test")

    # Create an unrelated derivation (derives from ob_b, not related to ob_a directly).
    # If cascade incorrectly recurses through cs and then looks for cs's targets,
    # it might find ob_b (cs→ob_b via supported_by) and taint ob_b.
    # Then from ob_b it might taint this derivation. This chain should NOT happen.
    dv_from_b = _seed_dv(server, "Derivation from ob_b.", ob_b)

    ret = json.loads(server._retract_impl(
        node_id=ob_a,
        error_type="hallucinated_claim",
        reason="ob_a was wrong.",
    ))
    assert "error" not in ret, ret

    # cs not tainted
    assert not _is_tainted(server, cs_id, ob_a), (
        f"cs {cs_id} must not be tainted"
    )
    # ob_b not tainted (cascade didn't recurse through cs)
    assert not _is_tainted(server, ob_b, ob_a), (
        f"ob_b {ob_b} must not be tainted — cascade did not recurse through cs"
    )
    # dv_from_b not tainted
    assert not _is_tainted(server, dv_from_b, ob_a), (
        f"dv_from_b {dv_from_b} must not be tainted — cascade stopped at cs"
    )
    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert cs_id not in tainted_ids, "cs must not appear in tainted_downstream"
    assert dv_from_b not in tainted_ids, "dv_from_b must not appear in tainted_downstream"


# ---------------------------------------------------------------------------
# §5.3 Supersede cascade — mirror of §5.1/5.2 for stale_by
# ---------------------------------------------------------------------------


def test_supersede_dv_with_downstream_dv_propagates_stale():
    """Superseding dv_old propagates stale_by to derivations downstream of it."""
    server = fresh_server()
    ob_id = _seed_ob(server, "Premise.")
    dv_old = _seed_dv(server, "Old derivation.", ob_id)
    dv_downstream = _seed_dv(server, "Downstream of old.", dv_old)
    dv_new = _seed_dv(server, "Refined old derivation.", ob_id)

    ret = json.loads(server._supersede_impl(dv_old, dv_new))
    assert "superseded" in ret.get("status", ""), ret

    assert _is_stale(server, dv_downstream, dv_old), (
        f"dv {dv_downstream} should be stale_by {dv_old} after supersede"
    )
    stale_ids = {s["id"] for s in ret.get("stale_downstream", [])}
    assert dv_downstream in stale_ids, f"{dv_downstream} should appear in stale_downstream"


def test_supersede_ob_with_supported_by_from_cs_does_NOT_stale_cs():
    """Superseding ob_old must NOT mark cs_Y stale when cs has supported_by→ob_old.

    Cascade skip rule for supersede: cornerstone accumulates instance-votes;
    one superseded instance does not stale the pattern.
    """
    server = fresh_server()
    ob_old = _seed_ob(server, "Old instance A.")
    ob_extra = _seed_ob(server, "Instance B — keeps cs live.")
    cs_id = _seed_cs(server, [ob_old, ob_extra], tag="supersede-skip-cs")
    ob_new = _seed_ob(server, "Refined instance A.")

    ret = json.loads(server._supersede_impl(ob_old, ob_new))
    assert "superseded" in ret.get("status", ""), ret

    assert not _is_stale(server, cs_id, ob_old), (
        f"cs {cs_id} must NOT be stale_by {ob_old} — cascade skip rule applies"
    )
    stale_ids = {s["id"] for s in ret.get("stale_downstream", [])}
    assert cs_id not in stale_ids, f"cs {cs_id} must not be in stale_downstream"


def test_supersede_ob_with_exemplifies_to_ls_does_NOT_stale_ls():
    """Superseding ob_old must NOT stale ls_Y when ls has incoming exemplifies←ob_old."""
    server = fresh_server()
    ob_old = _seed_ob(server, "Old incident exemplifying lesson.")
    ob_extra = _seed_ob(server, "Second incident keeps lesson live.")
    ls_id = _seed_ls(server, "Lesson pattern.", supporting_ids=f"{ob_old},{ob_extra}")
    ob_new = _seed_ob(server, "Refined old incident.")

    ret = json.loads(server._supersede_impl(ob_old, ob_new))
    assert "superseded" in ret.get("status", ""), ret

    assert not _is_stale(server, ls_id, ob_old), (
        f"lesson {ls_id} must NOT be stale_by {ob_old}"
    )
    stale_ids = {s["id"] for s in ret.get("stale_downstream", [])}
    assert ls_id not in stale_ids, f"lesson {ls_id} must not be in stale_downstream"


# ---------------------------------------------------------------------------
# §5.4 Zero-support detection
# ---------------------------------------------------------------------------


def test_retract_last_supporter_of_cs_sets_support_lost():
    """Retracting the ONLY supporter of cs → cs flagged support_lost."""
    server = fresh_server()
    ob_id = _seed_ob(server, "Sole supporter.")
    cs_id = _seed_cs(server, ob_id, tag="sole-supporter-cs")

    ret = json.loads(server._retract_impl(
        node_id=ob_id,
        error_type="hallucinated_claim",
        reason="Sole supporter was fabricated.",
    ))
    assert "error" not in ret, ret

    # support_lost must be in return value
    assert ret.get("support_lost_count", 0) == 1, (
        f"Expected support_lost_count=1, got: {ret}"
    )
    support_lost_ids = [n["id"] for n in ret.get("support_lost", [])]
    assert cs_id in support_lost_ids, (
        f"Expected {cs_id} in support_lost list, got: {support_lost_ids}"
    )

    # metadata must be updated
    meta = _get_meta(server, cs_id)
    assert meta.get("support_lost") is True, (
        f"cs {cs_id} metadata.support_lost must be True, got: {meta}"
    )
    # still NOT tainted (cascade skip rule)
    assert not _is_tainted(server, cs_id, ob_id), (
        f"cs {cs_id} must NOT be tainted"
    )


def test_retract_one_of_many_supporters_does_NOT_set_support_lost():
    """Retracting one of 5 supporters does NOT flag support_lost (4 remain)."""
    server = fresh_server()
    obs = [_seed_ob(server, f"Supporter {i}.") for i in range(5)]
    cs_id = _seed_cs(server, obs, tag="five-supporter-cs")

    ret = json.loads(server._retract_impl(
        node_id=obs[0],
        error_type="hallucinated_claim",
        reason="First supporter was fabricated.",
    ))
    assert "error" not in ret, ret

    assert ret.get("support_lost_count", 0) == 0, (
        f"support_lost_count should be 0 (4 supporters remain), got: {ret}"
    )
    meta = _get_meta(server, cs_id)
    assert not meta.get("support_lost"), (
        f"cs {cs_id} must NOT have support_lost set: {meta}"
    )


def test_retract_last_supporter_via_exemplifies_sets_support_lost():
    """Lesson supported only by exemplifies edge; retract the instance → support_lost."""
    server = fresh_server()
    ob_id = _seed_ob(server, "Sole incident exemplifying lesson.")
    ls_id = _seed_ls(server, "Lesson from sole incident.", supporting_ids=ob_id)

    ret = json.loads(server._retract_impl(
        node_id=ob_id,
        error_type="hallucinated_claim",
        reason="Incident was fabricated.",
    ))
    assert "error" not in ret, ret

    assert ret.get("support_lost_count", 0) == 1, (
        f"Expected support_lost_count=1 for lesson, got: {ret}"
    )
    support_lost_ids = [n["id"] for n in ret.get("support_lost", [])]
    assert ls_id in support_lost_ids, (
        f"Expected {ls_id} in support_lost list, got: {support_lost_ids}"
    )
    meta = _get_meta(server, ls_id)
    assert meta.get("support_lost") is True, (
        f"lesson {ls_id} metadata.support_lost must be True"
    )
    # not tainted
    assert not _is_tainted(server, ls_id, ob_id), (
        f"lesson {ls_id} must NOT be tainted"
    )


def test_supersede_last_supporter_of_cs_sets_support_lost():
    """Superseding the ONLY supporter of cs → cs flagged support_lost."""
    server = fresh_server()
    ob_old = _seed_ob(server, "Sole supporter, will be superseded.")
    cs_id = _seed_cs(server, ob_old, tag="supersede-sole-cs")
    ob_new = _seed_ob(server, "Replacement supporter.")

    ret = json.loads(server._supersede_impl(ob_old, ob_new))
    assert "superseded" in ret.get("status", ""), ret

    # ob_old is now is_current=0; cs has no live supported_by edges (ob_new is
    # NOT automatically wired to cs — caller must link explicitly).
    assert ret.get("support_lost_count", 0) == 1, (
        f"Expected support_lost_count=1, got: {ret}"
    )
    support_lost_ids = [n["id"] for n in ret.get("support_lost", [])]
    assert cs_id in support_lost_ids, (
        f"Expected {cs_id} in support_lost list, got: {support_lost_ids}"
    )
    meta = _get_meta(server, cs_id)
    assert meta.get("support_lost") is True, (
        f"cs {cs_id} metadata.support_lost must be True after supersede"
    )
    # still NOT stale
    assert not _is_stale(server, cs_id, ob_old), (
        f"cs {cs_id} must NOT be stale — cascade skip rule applies"
    )


def test_support_lost_logged_in_edit_log():
    """support_lost event appears in edit_history with correct action."""
    server = fresh_server()
    ob_id = _seed_ob(server, "Sole supporter — will be retracted.")
    cs_id = _seed_cs(server, ob_id, tag="audit-cs")

    json.loads(server._retract_impl(
        node_id=ob_id,
        error_type="hallucinated_claim",
        reason="Fabricated.",
    ))

    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT action, node_id, details FROM edit_history "
            "WHERE node_id = ? AND action = 'support_lost' ORDER BY id DESC LIMIT 1",
            (cs_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, (
        f"Expected a support_lost edit_history entry for {cs_id}, found none"
    )
    assert row["action"] == "support_lost"
    details = json.loads(row["details"])
    assert details.get("trigger") == ob_id, (
        f"Expected trigger={ob_id!r}, got: {details}"
    )


# ---------------------------------------------------------------------------
# §5.5 Migration script
# ---------------------------------------------------------------------------


def _get_test_db_path():
    return os.path.join(TEST_DIR, "knowledge.db")


def _inject_false_positive_markers(server, node_id, tainted_by=None, stale_by=None):
    """Directly inject tainted_by / stale_by into a node's metadata (simulates old cascade)."""
    conn = server._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (node_id,)).fetchone()
        meta = json.loads(row["metadata"] or "{}") if row else {}
        if tainted_by:
            meta["tainted_by"] = tainted_by
        if stale_by:
            meta["stale_by"] = stale_by
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (json.dumps(meta), node_id))
        conn.commit()
    finally:
        conn.close()


def test_migration_clears_cs_taint_and_stale():
    """Migration clears false-positive tainted_by + stale_by from a cornerstone.

    Simulates the cs_0003 case: one taint entry + one stale entry injected
    manually (as the old cascade would have created them). Migration should
    clear both; live-support count > 1 → no support_lost.
    """
    import migrate_cascade_semantics_v1 as mig

    server = fresh_server()
    # Two live supporters so support_lost is NOT set after clearing.
    ob1 = _seed_ob(server, "Live supporter 1.")
    ob2 = _seed_ob(server, "Live supporter 2.")
    ob_retracted_sim = _seed_ob(server, "Now-retracted obs (old taint source).")
    cs_id = _seed_cs(server, [ob1, ob2, ob_retracted_sim], tag="migration-test-cs")

    # Inject false-positive markers as the old engine would have.
    _inject_false_positive_markers(server, cs_id,
                                   tainted_by=[ob_retracted_sim],
                                   stale_by=["dv_0004"])

    db_path = _get_test_db_path()
    conn = mig._get_db(db_path)
    try:
        p = mig.plan(conn)
    finally:
        conn.close()

    assert p["total"] >= 1, f"Expected at least 1 affected node, got: {p}"
    affected_ids = {e["id"] for e in p["affected"]}
    assert cs_id in affected_ids, f"Expected {cs_id} in affected set, got: {affected_ids}"

    # Apply migration
    conn = mig._get_db(db_path)
    try:
        result = mig.apply_migration(conn, p)
        conn.commit()
    finally:
        conn.close()

    assert result["cleared_taint"] >= 1
    assert result["cleared_stale"] >= 1
    # 3 live supporters remain → no support_lost
    assert result["support_lost_set"] == 0, (
        f"Expected support_lost_set=0 (live supporters remain), got: {result}"
    )

    meta = _get_meta(server, cs_id)
    assert "tainted_by" not in meta, f"tainted_by not cleared from {cs_id}: {meta}"
    assert "stale_by" not in meta, f"stale_by not cleared from {cs_id}: {meta}"
    assert not meta.get("support_lost"), f"support_lost should not be set: {meta}"


def test_migration_idempotent():
    """Running migration twice is a no-op on the second pass."""
    import migrate_cascade_semantics_v1 as mig

    server = fresh_server()
    ob_id = _seed_ob(server, "Supporter.")
    cs_id = _seed_cs(server, ob_id, tag="idempotent-cs")
    _inject_false_positive_markers(server, cs_id, tainted_by=["ob_0001"])

    db_path = _get_test_db_path()

    # First run
    conn = mig._get_db(db_path)
    try:
        p = mig.plan(conn)
        result1 = mig.apply_migration(conn, p)
        conn.commit()
    finally:
        conn.close()

    assert result1["total_processed"] >= 1

    # Second run — should find nothing
    conn = mig._get_db(db_path)
    try:
        p2 = mig.plan(conn)
    finally:
        conn.close()

    assert p2["total"] == 0, (
        f"Second migration run found {p2['total']} affected nodes — expected 0 (idempotent)"
    )


def test_migration_sets_support_lost_when_zero_live_supporters():
    """Migration flags support_lost when a cs has zero live supporting nodes."""
    import migrate_cascade_semantics_v1 as mig

    server = fresh_server()
    ob_id = _seed_ob(server, "Sole supporter.")
    cs_id = _seed_cs(server, ob_id, tag="zero-support-migration-cs")

    # Simulate: ob was retracted externally (set is_current=0) + old cascade
    # left a tainted_by entry in the cs.
    conn = server._get_db()
    try:
        conn.execute("UPDATE nodes SET is_current = 0 WHERE id = ?", (ob_id,))
        conn.commit()
    finally:
        conn.close()

    _inject_false_positive_markers(server, cs_id, tainted_by=[ob_id])

    db_path = _get_test_db_path()
    conn = mig._get_db(db_path)
    try:
        p = mig.plan(conn)
        result = mig.apply_migration(conn, p)
        conn.commit()
    finally:
        conn.close()

    assert result["support_lost_set"] >= 1, (
        f"Expected support_lost_set >= 1, got: {result}"
    )
    meta = _get_meta(server, cs_id)
    assert meta.get("support_lost") is True, (
        f"cs {cs_id} should have support_lost=True after migration: {meta}"
    )


# ---------------------------------------------------------------------------
# §5.6 Surfacing — engram_reflect
# ---------------------------------------------------------------------------


def test_reflect_surfaces_support_lost_node_ids():
    """engram_reflect includes support_lost_nodes in its returned structure."""
    server = fresh_server()
    ob_id = _seed_ob(server, "Sole supporter.")
    cs_id = _seed_cs(server, ob_id, tag="reflect-support-lost-cs")

    # Retract the sole supporter → cs gets support_lost
    json.loads(server._retract_impl(
        node_id=ob_id,
        error_type="hallucinated_claim",
        reason="Fabricated.",
    ))

    reflect_result = json.loads(server._reflect_impl())
    assert "error" not in reflect_result, reflect_result

    support_lost = reflect_result.get("support_lost_nodes", [])
    support_lost_ids = [n["id"] for n in support_lost]
    assert cs_id in support_lost_ids, (
        f"Expected {cs_id} in reflect support_lost_nodes, got: {support_lost_ids}"
    )

    # summary count should match
    summary = reflect_result.get("summary", {})
    assert summary.get("support_lost_nodes", 0) >= 1, (
        f"Expected summary.support_lost_nodes >= 1, got: {summary}"
    )


def test_reflect_does_not_surface_cs_ls_as_tainted_when_skip_rule_applies():
    """A cs/ls that was NOT tainted (due to skip rule) does not appear in tainted_nodes."""
    server = fresh_server()
    ob1 = _seed_ob(server, "Supporter A.", url="https://example.com/a")
    ob2 = _seed_ob(server, "Supporter B.", url="https://example.com/b")
    cs_id = _seed_cs(server, [ob1, ob2], tag="skip-rule-cs")

    # Retract ob1 — cs should NOT be tainted (skip rule)
    json.loads(server._retract_impl(
        node_id=ob1,
        error_type="hallucinated_claim",
        reason="ob1 fabricated.",
    ))

    reflect_result = json.loads(server._reflect_impl())
    assert "error" not in reflect_result, reflect_result

    tainted_ids = {n["id"] for n in reflect_result.get("tainted_nodes", [])}
    assert cs_id not in tainted_ids, (
        f"cs {cs_id} must NOT appear in tainted_nodes — cascade skip rule was applied"
    )


# ---------------------------------------------------------------------------
# §5.7 Skip rule works on cs→cs / cs→ls / ls→cs cascade paths
# ---------------------------------------------------------------------------


def test_retract_ls_with_supported_by_from_cs_does_NOT_taint_cs():
    """Retracting ls_Y must NOT taint cs_X that has supported_by → ls_Y.

    Live graphs contain cs→ls supported_by edges (e.g. cs_0003→ls_0001).
    The cascade skip rule must fire on the downstream node's type (cs_X),
    not on the retracted node's type, so cs_X is protected even when the
    retracted node is itself a lesson.
    """
    server = fresh_server()
    # Create the lesson with a placeholder incident so engram_add_lesson is happy.
    ob_incident = _seed_ob(server, "Incident exemplifying the lesson.")
    ls_y = _seed_ls(server, "Lesson: pattern Y.", supporting_ids=ob_incident)
    # Cornerstone whose supported_by edge targets ls_y.
    cs_x = _seed_cs(server, [ls_y], tag="cs-supported-by-ls")

    ret = json.loads(server._retract_impl(
        node_id=ls_y,
        error_type="hallucinated_claim",
        reason="Lesson was incorrect.",
    ))
    assert "error" not in ret, ret

    assert not _is_tainted(server, cs_x, ls_y), (
        f"cs {cs_x} must NOT be tainted_by {ls_y} — downstream-type check fires on cs"
    )
    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert cs_x not in tainted_ids, (
        f"cs {cs_x} must not appear in tainted_downstream"
    )
    # Confirm tainted_by key is absent from cs_x's metadata entirely.
    meta = _get_meta(server, cs_x)
    assert cs_x not in meta.get("tainted_by", []), (
        f"cs {cs_x} metadata.tainted_by must not include itself"
    )
    assert ls_y not in meta.get("tainted_by", []), (
        f"cs {cs_x} metadata.tainted_by must not include {ls_y}"
    )


def test_retract_cs_with_supported_by_from_other_cs_does_NOT_taint_source_cs():
    """Retracting cs_Y must NOT taint cs_X that has supported_by → cs_Y.

    Uncommon but schema-permitted: a cornerstone can cite another cornerstone
    as a support (engram_add_cornerstone accepts any existing node ID as a
    supporting_id). When the inner cs is retracted, the outer cs's skip rule
    must fire.
    """
    server = fresh_server()
    # cs_y: the cornerstone that will be retracted.
    ob_for_y = _seed_ob(server, "Evidence backing cs_y.")
    cs_y = _seed_cs(server, [ob_for_y], tag="inner-cs")
    # cs_x: cornerstone with supported_by → cs_y.
    cs_x = _seed_cs(server, [cs_y], tag="outer-cs-citing-inner")

    ret = json.loads(server._retract_impl(
        node_id=cs_y,
        error_type="hallucinated_claim",
        reason="cs_y was wrong.",
    ))
    assert "error" not in ret, ret

    assert not _is_tainted(server, cs_x, cs_y), (
        f"cs {cs_x} must NOT be tainted_by {cs_y} — cascade skip rule applies to cs type"
    )
    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert cs_x not in tainted_ids, (
        f"cs {cs_x} must not appear in tainted_downstream"
    )
    meta = _get_meta(server, cs_x)
    assert cs_y not in meta.get("tainted_by", []), (
        f"cs {cs_x} metadata must not contain tainted_by entry for {cs_y}"
    )


def test_skip_rule_does_not_recurse_through_cs_or_ls():
    """Cascade walk terminates at cs/ls; nodes 'downstream of cs' in cascade terms
    must NOT receive taint via the cs hop.

    Graph shape:
      ob_a  ← (supported_by) ← cs_mid        # cs_mid is downstream of ob_a
      cs_mid ← (supported_by) ← cs_down       # cs_down is downstream of cs_mid

    When ob_a is retracted:
    - _find_downstream(ob_a) finds cs_mid; skip rule fires → cs_mid NOT tainted.
    - Because cs_mid is skipped, _find_downstream(cs_mid) is NOT called.
    - Therefore cs_down (which has supported_by → cs_mid) NEVER gets visited.
    - cs_down must NOT be tainted.

    Note: a derivation cannot directly cite cs as a premise (cornerstones are
    not in CLAIM_BEARING_TYPES, so engram_derive rejects them as supporting_ids).
    Instead we use a cs→cs supported_by chain, which is a schema-permitted shape
    already present in the live graph (e.g. cs_0002→cs_0001). This verifies the
    same invariant — cascade termination at the cs boundary — via a permitted
    graph topology.
    """
    server = fresh_server()
    ob_a = _seed_ob(server, "Root observation A.")
    # cs_mid: mid-level cornerstone, downstream of ob_a in the cascade walk.
    cs_mid = _seed_cs(server, [ob_a], tag="cascade-mid-cs")
    # cs_down: further cornerstone, downstream of cs_mid in the cascade walk.
    # If cascade recursed through cs_mid, it would call _find_downstream(cs_mid)
    # and find cs_down (because cs_down has supported_by → cs_mid, i.e.
    # source_id=cs_down, target_id=cs_mid).
    cs_down = _seed_cs(server, [cs_mid], tag="cascade-down-cs")

    ret = json.loads(server._retract_impl(
        node_id=ob_a,
        error_type="hallucinated_claim",
        reason="ob_a was fabricated.",
    ))
    assert "error" not in ret, ret

    # cs_mid: cascade skip rule fires — NOT tainted.
    assert not _is_tainted(server, cs_mid, ob_a), (
        f"cs_mid {cs_mid} must NOT be tainted_by {ob_a} — skip rule fires"
    )
    # cs_down: cascade did NOT recurse through cs_mid — NOT tainted.
    assert not _is_tainted(server, cs_down, ob_a), (
        f"cs_down {cs_down} must NOT be tainted — cascade terminates at cs_mid hop"
    )

    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert cs_mid not in tainted_ids, "cs_mid must not appear in tainted_downstream"
    assert cs_down not in tainted_ids, (
        "cs_down must not appear in tainted_downstream — cascade did not recurse through cs_mid"
    )


# ---------------------------------------------------------------------------
# §6.2-6.4 PR-AA — cascade-walker SSoT regression tests
# ---------------------------------------------------------------------------


def test_diamond_cascade_retract_visits_each_node_exactly_once():
    """Diamond-shape: dv_Z reachable from ob_X via two paths; retract ob_X.

    Graph shape:
      ob_X → dv_A (derives_from → ob_X)
      ob_X → dv_B (derives_from → ob_X)
      dv_Z derives_from → dv_A  AND  dv_Z derives_from → dv_B

    Before the PR-AA fix, _find_downstream would append dv_Z to `tainted`
    twice (once via dv_A path, once via dv_B path). After the fix, the shared
    walker's visited-set deduplication ensures dv_Z appears exactly once.
    """
    server = fresh_server()
    ob_x = _seed_ob(server, "Root observation X.")
    dv_a = _seed_dv(server, "Derivation A — from X.", ob_x)
    dv_b = _seed_dv(server, "Derivation B — from X.", ob_x)
    # dv_Z cites both dv_A and dv_B (diamond shape).
    dv_z = _seed_dv(server, "Derivation Z — from A and B.", [dv_a, dv_b])

    ret = json.loads(server._retract_impl(
        node_id=ob_x,
        error_type="hallucinated_claim",
        reason="ob_x was fabricated.",
    ))
    assert "error" not in ret, ret

    # dv_Z should appear exactly once in tainted_downstream.
    tainted_ids = [t["id"] for t in ret.get("tainted_downstream", [])]
    dv_z_count = tainted_ids.count(dv_z)
    assert dv_z_count == 1, (
        f"dv_Z {dv_z} must appear exactly once in tainted_downstream, "
        f"got {dv_z_count} (diamond-shape dedup failure). Full list: {tainted_ids}"
    )

    # Metadata write is still correct.
    assert _is_tainted(server, dv_z, ob_x), (
        f"dv_Z {dv_z} must be tainted_by {ob_x}"
    )


def test_diamond_cascade_supersede_visits_each_node_exactly_once():
    """Diamond-shape: dv_Z reachable from ob_old via two paths; supersede ob_old.

    Graph shape:
      ob_old → dv_A (derives_from → ob_old)
      ob_old → dv_B (derives_from → ob_old)
      dv_Z derives_from → dv_A  AND  dv_Z derives_from → dv_B

    Before the PR-AA fix, _flag_stale would append dv_Z to `stale` twice.
    After the fix, the shared walker's visited-set deduplication ensures
    dv_Z appears exactly once in stale_downstream.
    """
    server = fresh_server()
    ob_old = _seed_ob(server, "Old root observation.")
    ob_new = _seed_ob(server, "Refined root observation.")
    dv_a = _seed_dv(server, "Derivation A — from old root.", ob_old)
    dv_b = _seed_dv(server, "Derivation B — from old root.", ob_old)
    # dv_Z cites both dv_A and dv_B (diamond shape).
    dv_z = _seed_dv(server, "Derivation Z — from A and B.", [dv_a, dv_b])

    ret = json.loads(server._supersede_impl(ob_old, ob_new))
    assert "error" not in ret, ret

    # dv_Z should appear exactly once in stale_downstream.
    stale_ids = [s["id"] for s in ret.get("stale_downstream", [])]
    dv_z_count = stale_ids.count(dv_z)
    assert dv_z_count == 1, (
        f"dv_Z {dv_z} must appear exactly once in stale_downstream, "
        f"got {dv_z_count} (diamond-shape dedup failure). Full list: {stale_ids}"
    )

    # Metadata write is still correct.
    assert _is_stale(server, dv_z, ob_old), (
        f"dv_Z {dv_z} must be stale_by {ob_old}"
    )


def test_three_path_cascade_visits_node_once():
    """Three-parent diamond: dv_Z reachable via 3 paths; retract ob_X.

    Graph shape:
      ob_X → dv_A, dv_B, dv_C  (all derives_from → ob_X)
      dv_Z derives_from → dv_A, dv_B, dv_C

    dv_Z must appear exactly once in tainted_downstream regardless of the
    path count.
    """
    server = fresh_server()
    ob_x = _seed_ob(server, "Root observation for 3-parent test.")
    dv_a = _seed_dv(server, "Derivation A (3-parent).", ob_x)
    dv_b = _seed_dv(server, "Derivation B (3-parent).", ob_x)
    dv_c = _seed_dv(server, "Derivation C (3-parent).", ob_x)
    dv_z = _seed_dv(server, "Derivation Z — from A, B, and C.", [dv_a, dv_b, dv_c])

    ret = json.loads(server._retract_impl(
        node_id=ob_x,
        error_type="hallucinated_claim",
        reason="ob_x fabricated — 3-parent test.",
    ))
    assert "error" not in ret, ret

    tainted_ids = [t["id"] for t in ret.get("tainted_downstream", [])]
    dv_z_count = tainted_ids.count(dv_z)
    assert dv_z_count == 1, (
        f"dv_Z {dv_z} must appear exactly once even with 3 parents, "
        f"got {dv_z_count}. Full list: {tainted_ids}"
    )
    assert _is_tainted(server, dv_z, ob_x), (
        f"dv_Z {dv_z} must still be tainted_by {ob_x}"
    )


def test_cascade_stops_at_retracted_downstream_node():
    """Parity guard: walker stops at retracted mid-chain nodes (is_current=0).

    Graph shape:
      ob_X → dv_A (dv_A is already retracted, is_current=0)
      dv_A → dv_B (dv_B depends on dv_A)

    When ob_X is retracted:
    - dv_A has is_current=0 → walker stops there, dv_A NOT in tainted list.
    - dv_B is downstream of dv_A, which is not visited → dv_B NOT in tainted list.

    Before the PR-AA fix, _find_downstream would recurse through dv_A even
    when dv_A was retracted (the recursive call was outside the `if d_node:`
    guard). The shared walker's stop-at-retracted semantics fix this asymmetry.
    """
    server = fresh_server()
    ob_x = _seed_ob(server, "Root observation for parity guard test.")
    dv_a = _seed_dv(server, "Mid-chain derivation A.", ob_x)
    dv_b = _seed_dv(server, "Terminal derivation B — derives from A.", dv_a)

    # Pre-retract dv_A so it has is_current=0 before ob_X is retracted.
    pre = json.loads(server._retract_impl(
        node_id=dv_a,
        error_type="hallucinated_claim",
        reason="dv_a pre-retracted to set up parity test.",
    ))
    assert "error" not in pre, pre

    # Now retract ob_X. The cascade should not include dv_A (already retracted)
    # or dv_B (unreachable because walker stops at dv_A).
    ret = json.loads(server._retract_impl(
        node_id=ob_x,
        error_type="hallucinated_claim",
        reason="ob_x retracted — parity guard test.",
    ))
    assert "error" not in ret, ret

    tainted_ids = {t["id"] for t in ret.get("tainted_downstream", [])}
    assert dv_a not in tainted_ids, (
        f"dv_A {dv_a} must NOT be in tainted_downstream — is_current=0, walker stops here"
    )
    assert dv_b not in tainted_ids, (
        f"dv_B {dv_b} must NOT be in tainted_downstream — cascade must not recurse through "
        f"retracted dv_A"
    )
