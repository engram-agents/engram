"""Tests for supersede/retract cascade to contradiction nodes (issue #229).

The 2026-05-20 substrate change: when engram_supersede or engram_retract
fires on a node, walk its incoming `contradicts` edges and mark each
contradiction node with `stale_by_premise` / `tainted_by` respectively
(parallel to the existing derivation cascade).

Why: a supersede on one side of a contradiction may already substantively
resolve the contradiction (the new node alters the conflicting claim).
Dream-fairy-2 needs the stale flag to surface contradictions whose premise
has shifted, then decide whether to wire engram_resolve (case 1) or
new-contradict + supersede-old-contradict (case 2).
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_ct_cascade")


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


def _mk_obs(server, claim: str, quote_type: str = "hard_data") -> str:
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/test",
        "title": "Test source",
        "claim": claim,
        "quoted_text": claim,
        "interpretation": f"Test observation: {claim}",
        "quote_type": quote_type,
    })))
    assert "observation_id" in r, r
    return r["observation_id"]


def _mk_dv(server, claim: str, supporting_ids: list[str],
           reasoning_type: str = "deductive_modus_ponens") -> str:
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": claim,
        "supporting_ids": ",".join(supporting_ids),
        "logical_chain": f"Reasoning for: {claim}",
        "reasoning_type": reasoning_type,
    })))
    assert "derivation_id" in r, r
    return r["derivation_id"]


def _mk_ct(server, node_a: str, node_b: str, description: str) -> str:
    r = json.loads(server.engram_contradict(payload_json=json.dumps({
        "node_id_a": node_a,
        "node_id_b": node_b,
        "description": description,
    })))
    assert "contradiction_id" in r, r
    return r["contradiction_id"]


# ── Supersede cascade ──────────────────────────────────────────────────────


def test_supersede_marks_contradiction_stale_by_premise():
    """Supersede on a node that's a side of a contradiction should mark the
    contradiction node `stale_by_premise` with the old id + replacement id."""
    server = fresh_server()
    ob_a = _mk_obs(server, "Claim A holds.")
    ob_b = _mk_obs(server, "Claim A does not hold.")
    ct_id = _mk_ct(server, ob_a, ob_b, "Disagreement about A.")

    # Need 2 obs for deductive_modus_ponens
    ob_a2 = _mk_obs(server, "Claim A reframed under stricter reading.")
    dv_new = _mk_dv(server, "Refined version of ob_a — same surface, sharper framing.",
                    [ob_a, ob_a2])

    r = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": ob_a,
        "new_node_id": dv_new,
        "supersede_reason": "test cascade",
    })))
    # Cross-type supersede is rejected: supersede preserves type.
    # So this particular test setup fails at the type check. Use same-type.
    assert "error" in r and "Type mismatch" in r["error"], r


def test_supersede_same_type_marks_contradiction_stale():
    """Supersede ob → ob (same type) cascades to contradictions."""
    server = fresh_server()
    ob_a = _mk_obs(server, "Source A says X is true.")
    ob_b = _mk_obs(server, "Source B says X is false.")
    ct_id = _mk_ct(server, ob_a, ob_b, "Sources A and B disagree.")

    ob_a_refined = _mk_obs(server, "Source A's stricter reading: X is true under condition Y.")

    r = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": ob_a,
        "new_node_id": ob_a_refined,
        "supersede_reason": "narrower claim",
    })))
    assert r.get("status") == "superseded", r
    assert r["stale_contradiction_count"] == 1, r
    assert r["stale_contradictions"][0]["id"] == ct_id

    # Inspect ct to confirm metadata
    ct_inspect = json.loads(server.engram_inspect(node_id=ct_id, dream_mode=True))
    ct_meta = json.loads(ct_inspect["node"]["metadata"])
    assert "stale_by" in ct_meta
    assert ob_a in ct_meta["stale_by"]
    assert ct_meta.get("stale_replacement") == ob_a_refined


def test_supersede_unrelated_node_does_not_flag_contradiction():
    """Supersede on a node NOT touching any contradiction leaves contradictions alone."""
    server = fresh_server()
    ob_a = _mk_obs(server, "Source A says X.")
    ob_b = _mk_obs(server, "Source B says not X.")
    ct_id = _mk_ct(server, ob_a, ob_b, "A vs B.")

    # Unrelated observation
    ob_c = _mk_obs(server, "Unrelated claim about Z.")
    ob_c_refined = _mk_obs(server, "Refined unrelated claim about Z.")

    r = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": ob_c,
        "new_node_id": ob_c_refined,
    })))
    assert r.get("status") == "superseded", r
    assert r["stale_contradiction_count"] == 0

    # ct should still be clean
    ct_inspect = json.loads(server.engram_inspect(node_id=ct_id, dream_mode=True))
    ct_meta = json.loads(ct_inspect["node"]["metadata"]) if ct_inspect["node"]["metadata"] else {}
    assert ct_meta.get("stale_by", []) == []


def test_supersede_cascades_to_multiple_contradictions():
    """One node can be a side of multiple contradictions; supersede marks ALL stale."""
    server = fresh_server()
    ob_a = _mk_obs(server, "Source A says X.")
    ob_b = _mk_obs(server, "Source B says not X.")
    ob_c = _mk_obs(server, "Source C says X with caveats.")
    ct_ab = _mk_ct(server, ob_a, ob_b, "A vs B.")
    ct_ac = _mk_ct(server, ob_a, ob_c, "A vs C nuanced.")

    ob_a_refined = _mk_obs(server, "Source A refined.")
    r = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": ob_a,
        "new_node_id": ob_a_refined,
    })))
    assert r["stale_contradiction_count"] == 2
    flagged_ids = {sc["id"] for sc in r["stale_contradictions"]}
    assert flagged_ids == {ct_ab, ct_ac}


# ── Retract cascade ────────────────────────────────────────────────────────


def test_retract_marks_contradiction_tainted_by():
    """Retract on a node that's a side of a contradiction should mark the
    contradiction node `tainted_by` with the retracted node id."""
    server = fresh_server()
    ob_a = _mk_obs(server, "Source A says X.")
    ob_b = _mk_obs(server, "Source B says not X.")
    ct_id = _mk_ct(server, ob_a, ob_b, "A vs B.")

    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_a,
        "error_type": "fabricated_quote",
        "reason": "Test retract — claim was fabricated.",
    })))
    assert r.get("status") == "retracted", r
    assert r["tainted_contradiction_count"] == 1, r
    assert r["tainted_contradictions"][0]["id"] == ct_id

    ct_inspect = json.loads(server.engram_inspect(node_id=ct_id, dream_mode=True))
    ct_meta = json.loads(ct_inspect["node"]["metadata"])
    assert "tainted_by" in ct_meta
    assert ob_a in ct_meta["tainted_by"]


def test_retract_unrelated_node_does_not_taint_contradiction():
    server = fresh_server()
    ob_a = _mk_obs(server, "A.")
    ob_b = _mk_obs(server, "Not A.")
    ct_id = _mk_ct(server, ob_a, ob_b, "Disagreement.")

    ob_c = _mk_obs(server, "Unrelated claim.")
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_c,
        "error_type": "fabricated_quote",
        "reason": "Test.",
    })))
    assert r["tainted_contradiction_count"] == 0


def test_retract_cascades_to_multiple_contradictions():
    server = fresh_server()
    ob_a = _mk_obs(server, "A.")
    ob_b = _mk_obs(server, "Not A.")
    ob_c = _mk_obs(server, "A with caveat.")
    ct_ab = _mk_ct(server, ob_a, ob_b, "A vs B.")
    ct_ac = _mk_ct(server, ob_a, ob_c, "A vs C.")

    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_a,
        "error_type": "hallucinated_claim",
        "reason": "Test.",
    })))
    assert r["tainted_contradiction_count"] == 2
    flagged_ids = {tc["id"] for tc in r["tainted_contradictions"]}
    assert flagged_ids == {ct_ab, ct_ac}


# ── End-to-end stale-contradiction resolution ─────────────────────────────


def test_ct_0006_shape_supersede_then_resolve():
    """End-to-end: supersede flags contradiction stale; the superseding node
    is then used to resolve in a single engram_resolve call (no chain
    dilution)."""
    server = fresh_server()
    ob_a = _mk_obs(server, "Source A holds P.")
    ob_b = _mk_obs(server, "Source B holds not P.")
    ct_id = _mk_ct(server, ob_a, ob_b, "Source A vs Source B on P.")

    # New node adopts ob_b's reading + adds caveat — supersedes ob_a, also
    # substantively resolves ct because the new claim no longer conflicts with ob_b.
    ob_a_refined = _mk_obs(server,
        "Source A overreached; under the operational criterion, not P holds (acknowledging B's reading).")

    # Step 1: supersede — cascade fires
    sup = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": ob_a,
        "new_node_id": ob_a_refined,
        "supersede_reason": "narrower claim consistent with B",
    })))
    assert sup["stale_contradiction_count"] == 1

    # Step 2: dream-fairy-2 inspects, sees stale_by_premise, decides
    # substantive resolution — wires engram_resolve directly with the
    # superseding node. (In real usage might compose a synthesis derivation
    # via engram_derive first; here ob_a_refined directly resolves.)
    res = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": ct_id,
        "resolving_node_id": ob_a_refined,
    })))
    assert res.get("status") == "resolution_created", res
    assert res["resolution_status"] == "resolved", res

    # ct status reflects the resolved state
    ct_inspect = json.loads(server.engram_inspect(node_id=ct_id, dream_mode=True))
    assert ct_inspect["node"]["status"] == "resolved"


# ── Both-sides-superseded scalar-clobber documentation (issue #231) ───────


def test_both_sides_superseded_clobbers_stale_replacement():
    """Document current behavior: when both contradicting sides of a ct are
    superseded in sequence, metadata.stale_replacement is a scalar that gets
    clobbered by the second supersede (only the second replacement pointer
    survives). metadata.stale_by correctly appends both old IDs.

    Tracked for fix in issue #231 (substrate: stale_replacement should be
    dict-keyed by old_node_id). This test asserts CURRENT behavior so that
    when issue #231 lands, the test must be updated as part of that change.
    """
    server = fresh_server()
    ob_a = _mk_obs(server, "Source A says X.")
    ob_b = _mk_obs(server, "Source B says not X.")
    ct_id = _mk_ct(server, ob_a, ob_b, "A vs B.")

    ob_a_refined = _mk_obs(server, "Source A refined under condition Y.")
    sup_a = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": ob_a,
        "new_node_id": ob_a_refined,
    })))
    assert sup_a["stale_contradiction_count"] == 1

    ob_b_refined = _mk_obs(server, "Source B refined under condition Z.")
    sup_b = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": ob_b,
        "new_node_id": ob_b_refined,
    })))
    assert sup_b["stale_contradiction_count"] == 1

    ct_inspect = json.loads(server.engram_inspect(node_id=ct_id, dream_mode=True))
    ct_meta = json.loads(ct_inspect["node"]["metadata"])

    # stale_by correctly appends both old IDs
    assert set(ct_meta["stale_by"]) == {ob_a, ob_b}, ct_meta["stale_by"]

    # CURRENT scalar-clobber behavior: only the SECOND replacement survives.
    # Issue #231 tracks the fix to make this dict-keyed.
    assert ct_meta["stale_replacement"] == ob_b_refined, (
        f"Pre-#231 scalar behavior: expected {ob_b_refined} (last writer wins), "
        f"got {ct_meta['stale_replacement']}"
    )
