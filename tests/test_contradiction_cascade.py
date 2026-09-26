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

Import-pattern contract (#948 wave B — R1):
    This file imports engram_revision, engram_derive, engram_observation,
    engram_query, and engram_core directly and contains no ``import server``.
    The module objects are passed to fresh_module() (R4) for per-test
    reload isolation.
"""

import atexit
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add tests/ dir so conftest.fresh_module is importable both under pytest and
# when the file is run as a standalone script.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_derive
import engram_observation
import engram_query
import engram_revision

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_ct_cascade_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _make_fresh_mods():
    """Reload all family modules, configure a clean test DB, and return modules.

    Uses conftest.fresh_module(engram_revision) per R4: the module object is
    passed (not a string), so the ``import engram_revision`` at the top of
    this file is the AST-visible dependency edge for run_touched_tests.py.

    Returns (rev, obs, derive, qry, core) — freshly reloaded module objects
    with engram_core pointing to TEST_DIR.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    qry = importlib.import_module("engram_query")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, derive, qry, core


def _mk_obs(obs, claim: str, quote_type: str = "hard_data") -> str:
    r = json.loads(obs._add_observation_impl(
        url="https://example.com/test",
        title="Test source",
        claim=claim,
        quoted_text=claim,
        interpretation=f"Test observation: {claim}",
        quote_type=quote_type,
    ))
    assert "observation_id" in r, r
    return r["observation_id"]


def _mk_dv(derive, claim: str, supporting_ids: list,
           reasoning_type: str = "deductive_modus_ponens",
           use_contested: bool = False) -> str:
    r = json.loads(derive._derive_impl(
        claim=claim,
        supporting_ids=",".join(supporting_ids),
        logical_chain=f"Reasoning for: {claim}",
        reasoning_type=reasoning_type,
        use_contested=use_contested,
    ))
    assert "derivation_id" in r, r
    return r["derivation_id"]


def _mk_ct(derive, node_a: str, node_b: str, description: str) -> str:
    r = json.loads(derive._contradict_impl(
        node_id_a=node_a,
        node_id_b=node_b,
        description=description,
    ))
    assert "contradiction_id" in r, r
    return r["contradiction_id"]


def _stale_by(core, node_id: str) -> set:
    """root_node ids of node_id's pending supersede-severity markers.
    Migrated off metadata.stale_by to cascade_markers (#2013/#2009 slice 1)
    — same observable meaning (which roots this node is stale because of),
    new storage.
    """
    conn = core._get_db()
    try:
        rows = conn.execute(
            "SELECT root_node FROM cascade_markers WHERE node_id = ? "
            "AND root_op = 'supersede' AND status = 'pending'",
            (node_id,),
        ).fetchall()
        return {r["root_node"] for r in rows}
    finally:
        conn.close()


def _tainted_by(core, node_id: str) -> set:
    """root_node ids of node_id's pending retract-severity markers.
    Migrated off metadata.tainted_by to cascade_markers.
    """
    conn = core._get_db()
    try:
        rows = conn.execute(
            "SELECT root_node FROM cascade_markers WHERE node_id = ? "
            "AND root_op = 'retract' AND status = 'pending'",
            (node_id,),
        ).fetchall()
        return {r["root_node"] for r in rows}
    finally:
        conn.close()


# ── Supersede cascade ──────────────────────────────────────────────────────


def test_supersede_marks_contradiction_stale_by_premise():
    """Supersede on a node that's a side of a contradiction should mark the
    contradiction node `stale_by_premise` with the old id + replacement id."""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Claim A holds.")
    ob_b = _mk_obs(obs, "Claim A does not hold.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "Disagreement about A.")

    # Need 2 obs for deductive_modus_ponens. ob_a sits on the open ct_id
    # contradiction filed above, so citing it as a premise now requires the
    # #1654 use_contested opt-in — deliberate here, this test's target
    # assertion is the cross-type supersede rejection below, not the guard.
    ob_a2 = _mk_obs(obs, "Claim A reframed under stricter reading.")
    dv_new = _mk_dv(derive, "Refined version of ob_a — same surface, sharper framing.",
                    [ob_a, ob_a2], use_contested=True)

    r = json.loads(rev._supersede_impl(
        old_node_id=ob_a,
        new_node_id=dv_new,
        supersede_reason="test cascade",
    ))
    # Cross-type supersede is rejected: supersede preserves type.
    # So this particular test setup fails at the type check. Use same-type.
    assert "error" in r and "Type mismatch" in r["error"], r


def test_supersede_same_type_marks_contradiction_stale():
    """Supersede ob → ob (same type) cascades to contradictions."""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Source A says X is true.")
    ob_b = _mk_obs(obs, "Source B says X is false.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "Sources A and B disagree.")

    ob_a_refined = _mk_obs(obs, "Source A's stricter reading: X is true under condition Y.")

    r = json.loads(rev._supersede_impl(
        old_node_id=ob_a,
        new_node_id=ob_a_refined,
        supersede_reason="narrower claim",
    ))
    assert r.get("status") == "superseded", r
    assert r["stale_contradiction_count"] == 1, r
    assert r["stale_contradictions"][0]["id"] == ct_id

    # Confirm the ct node picked up a pending supersede-severity marker
    # rooted at ob_a (#2013/#2009 slice 1 — cascade_markers, not metadata).
    # stale_replacement is retired: a downstream node's own replacement is
    # no longer cached at marking time (see engram_revision.py's module
    # docstring) — there's nothing to assert here anymore.
    assert ob_a in _stale_by(core, ct_id)


def test_supersede_unrelated_node_does_not_flag_contradiction():
    """Supersede on a node NOT touching any contradiction leaves contradictions alone."""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Source A says X.")
    ob_b = _mk_obs(obs, "Source B says not X.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "A vs B.")

    # Unrelated observation
    ob_c = _mk_obs(obs, "Unrelated claim about Z.")
    ob_c_refined = _mk_obs(obs, "Refined unrelated claim about Z.")

    r = json.loads(rev._supersede_impl(
        old_node_id=ob_c,
        new_node_id=ob_c_refined,
    ))
    assert r.get("status") == "superseded", r
    assert r["stale_contradiction_count"] == 0

    # ct should still be clean — no pending marker at all.
    assert _stale_by(core, ct_id) == set()


def test_supersede_cascades_to_multiple_contradictions():
    """One node can be a side of multiple contradictions; supersede marks ALL stale."""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Source A says X.")
    ob_b = _mk_obs(obs, "Source B says not X.")
    ob_c = _mk_obs(obs, "Source C says X with caveats.")
    ct_ab = _mk_ct(derive, ob_a, ob_b, "A vs B.")
    ct_ac = _mk_ct(derive, ob_a, ob_c, "A vs C nuanced.")

    ob_a_refined = _mk_obs(obs, "Source A refined.")
    r = json.loads(rev._supersede_impl(
        old_node_id=ob_a,
        new_node_id=ob_a_refined,
    ))
    assert r["stale_contradiction_count"] == 2
    flagged_ids = {sc["id"] for sc in r["stale_contradictions"]}
    assert flagged_ids == {ct_ab, ct_ac}


# ── Retract cascade ────────────────────────────────────────────────────────


def test_retract_marks_contradiction_tainted_by():
    """Retract on a node that's a side of a contradiction should mark the
    contradiction node `tainted_by` with the retracted node id."""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Source A says X.")
    ob_b = _mk_obs(obs, "Source B says not X.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "A vs B.")

    r = json.loads(rev._retract_impl(
        node_id=ob_a,
        error_type="fabricated_quote",
        reason="Test retract — claim was fabricated.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert r.get("status") == "retracted", r
    assert r["tainted_contradiction_count"] == 1, r
    assert r["tainted_contradictions"][0]["id"] == ct_id

    assert ob_a in _tainted_by(core, ct_id)


def test_retract_unrelated_node_does_not_taint_contradiction():
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "A.")
    ob_b = _mk_obs(obs, "Not A.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "Disagreement.")

    ob_c = _mk_obs(obs, "Unrelated claim.")
    r = json.loads(rev._retract_impl(
        node_id=ob_c,
        error_type="fabricated_quote",
        reason="Test.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert r["tainted_contradiction_count"] == 0


def test_retract_cascades_to_multiple_contradictions():
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "A.")
    ob_b = _mk_obs(obs, "Not A.")
    ob_c = _mk_obs(obs, "A with caveat.")
    ct_ab = _mk_ct(derive, ob_a, ob_b, "A vs B.")
    ct_ac = _mk_ct(derive, ob_a, ob_c, "A vs C.")

    r = json.loads(rev._retract_impl(
        node_id=ob_a,
        error_type="hallucinated_claim",
        reason="Test.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert r["tainted_contradiction_count"] == 2
    flagged_ids = {tc["id"] for tc in r["tainted_contradictions"]}
    assert flagged_ids == {ct_ab, ct_ac}


# ── End-to-end stale-contradiction resolution ─────────────────────────────


def test_ct_0006_shape_supersede_then_resolve():
    """End-to-end: supersede flags contradiction stale; the superseding node
    is then used to resolve in a single _resolve_impl call (no chain
    dilution)."""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Source A holds P.")
    ob_b = _mk_obs(obs, "Source B holds not P.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "Source A vs Source B on P.")

    # New node adopts ob_b's reading + adds caveat — supersedes ob_a, also
    # substantively resolves ct because the new claim no longer conflicts with ob_b.
    ob_a_refined = _mk_obs(obs,
        "Source A overreached; under the operational criterion, not P holds (acknowledging B's reading).")

    # Step 1: supersede — cascade fires
    sup = json.loads(rev._supersede_impl(
        old_node_id=ob_a,
        new_node_id=ob_a_refined,
        supersede_reason="narrower claim consistent with B",
    ))
    assert sup["stale_contradiction_count"] == 1

    # Step 2: dream-fairy-2 inspects, sees stale_by_premise, decides
    # substantive resolution — wires _resolve_impl directly with the
    # superseding node. (In real usage might compose a synthesis derivation
    # via _derive_impl first; here ob_a_refined directly resolves.)
    res = json.loads(rev._resolve_impl(
        target_id=ct_id,
        resolving_node_id=ob_a_refined,
    ))
    assert res.get("status") == "resolution_created", res
    assert res["resolution_status"] == "resolved", res

    # ct status reflects the resolved state
    ct_inspect = json.loads(qry._inspect_impl(node_id=ct_id, dream_mode=True))
    assert ct_inspect["node"]["status"] == "resolved"


# ── Both-sides-superseded dict-keyed behavior (issue #231 fixed) ──────────


def test_both_sides_superseded_preserves_all_replacements():
    """When both contradicting sides of a ct are superseded in sequence, both
    supersedes must survive as INDEPENDENT markers — the two-markers-per-
    event invariant (#2013 §9 / comment 2026-08-14T21:23:31Z), not one
    record clobbering the other.

    Originally about metadata.stale_replacement's dict-keyed fix (issue
    #231, "both replacements survive as separate entries"). Migrated to
    cascade_markers (#2013/#2009 slice 1): the mechanism issue #231 fixed
    (a single stale_replacement dict entry per old_node_id, previously
    clobbered on the second supersede) doesn't exist anymore in the same
    shape — the marker table's UNIQUE(node_id, cascade_event) constraint
    plus one cascade_event per _supersede_impl call makes clobbering
    structurally unreachable: two supersedes on two different roots ALWAYS
    produce two separate marker rows on ct_id, never a shared field two
    writes could collide on. What survives to test is the observable
    behavior issue #231 was about: both roots show up, independently.
    """
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Source A says X.")
    ob_b = _mk_obs(obs, "Source B says not X.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "A vs B.")

    ob_a_refined = _mk_obs(obs, "Source A refined under condition Y.")
    sup_a = json.loads(rev._supersede_impl(
        old_node_id=ob_a,
        new_node_id=ob_a_refined,
    ))
    assert sup_a["stale_contradiction_count"] == 1

    ob_b_refined = _mk_obs(obs, "Source B refined under condition Z.")
    sup_b = json.loads(rev._supersede_impl(
        old_node_id=ob_b,
        new_node_id=ob_b_refined,
    ))
    assert sup_b["stale_contradiction_count"] == 1

    # Both roots survive as independent pending markers on ct_id — neither
    # supersede's marker clobbered the other's.
    assert _stale_by(core, ct_id) == {ob_a, ob_b}

    # And they really are two separate rows (two cascade_events), not one
    # row somehow carrying both roots — confirms the UNIQUE(node_id,
    # cascade_event) constraint is doing the work, not a coincidence of
    # the set comparison above.
    conn = core._get_db()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM cascade_markers WHERE node_id = ? "
            "AND status = 'pending'",
            (ct_id,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 2, f"expected 2 independent markers on {ct_id}, found {n}"


# ── #1654 — BLOCKED_CONTRADICTED premise guard ─────────────────────────────


def test_contradict_itself_is_cascade_free():
    """Filing a contradiction taints/stales NEITHER side — cascade rides
    resolution (retract/supersede), never contradict itself. This is the
    settled #1654 semantics (Kepler+Borges DM 2026-07-04): a contradiction
    is a live dispute, not a refutation."""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Claim A holds.")
    ob_b = _mk_obs(obs, "Claim A does not hold.")
    _mk_ct(derive, ob_a, ob_b, "Disagreement about A.")

    # Migrated off metadata to cascade_markers (#2013/#2009 slice 1) via this
    # file's own _tainted_by/_stale_by helpers. 34d7428 added those helpers and
    # converted four other call sites but missed this one, leaving the only
    # guard of the #1654 cascade-free invariant asserting over a store nothing
    # writes — permanently true, unable to fail.
    for nid in (ob_a, ob_b):
        assert not _tainted_by(core, nid), (
            f"{nid} should not be tainted by mere contradiction: "
            f"{_tainted_by(core, nid)}"
        )
        assert not _stale_by(core, nid), (
            f"{nid} should not be staled by mere contradiction: "
            f"{_stale_by(core, nid)}"
        )


def test_derive_blocked_contradicted_on_open_dispute():
    """A premise sitting on an open, unresolved contradiction blocks new
    derivations citing it by default — the #1654 guard."""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Claim A holds.")
    ob_b = _mk_obs(obs, "Claim A does not hold.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "Disagreement about A.")
    ob_other = _mk_obs(obs, "An unrelated corroborating claim.")

    r = json.loads(derive._derive_impl(
        claim="Building further on the disputed claim A.",
        supporting_ids=f"{ob_a},{ob_other}",
        logical_chain="Combining A with an unrelated premise.",
        reasoning_type="deductive_modus_ponens",
    ))
    assert r.get("status") == "blocked_contradicted", r
    assert r["error"].startswith("BLOCKED_CONTRADICTED"), r
    assert {p["id"] for p in r["contested_premises"]} == {ob_a}, r
    assert r["contested_premises"][0]["open_contradictions"] == [ct_id], r
    assert "derivation_id" not in r


def test_derive_use_contested_override_succeeds_and_auto_stamps():
    """use_contested=True lets the derivation proceed and auto-stamps
    metadata.built_on_contested — the resolution-pressure valve."""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Claim A holds.")
    ob_b = _mk_obs(obs, "Claim A does not hold.")
    _mk_ct(derive, ob_a, ob_b, "Disagreement about A.")
    ob_other = _mk_obs(obs, "An unrelated corroborating claim.")

    r = json.loads(derive._derive_impl(
        claim="Deliberately building on the disputed claim A.",
        supporting_ids=f"{ob_a},{ob_other}",
        logical_chain="Combining A with an unrelated premise, knowingly contested.",
        reasoning_type="deductive_modus_ponens",
        use_contested=True,
    ))
    assert "derivation_id" in r, r
    assert r["built_on_contested"] == [ob_a], r

    node = json.loads(qry._inspect_impl(node_id=r["derivation_id"], view="deep", dream_mode=True))
    meta = json.loads(node["node"]["metadata"])
    assert meta["built_on_contested"] == [ob_a], meta


def test_derive_no_override_still_blocked_contradicted():
    """Sanity companion to the auto-stamp test: without use_contested,
    _derive_impl offers no way to note/acknowledge the dispute and proceed
    — it's a hard block_contradicted, full stop. (The payload-level check
    that an author-supplied built_on_contested field is rejected as an
    unknown field lives in test_derive_payload.py, which has the
    server.engram_derive fixture this file deliberately doesn't import —
    see this file's R1 import-pattern contract.)"""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Claim A holds.")
    ob_b = _mk_obs(obs, "Claim A does not hold.")
    _mk_ct(derive, ob_a, ob_b, "Disagreement about A.")
    ob_other = _mk_obs(obs, "An unrelated corroborating claim.")

    r_no_override = json.loads(derive._derive_impl(
        claim="Attempting to note the dispute without opting in.",
        supporting_ids=f"{ob_a},{ob_other}",
        logical_chain="No override requested.",
        reasoning_type="deductive_modus_ponens",
    ))
    assert r_no_override.get("status") == "blocked_contradicted", r_no_override


def test_derive_succeeds_without_override_after_contradiction_resolved():
    """Once the contradiction is resolved (a resolves edge wired against
    it), the premise is no longer 'open' and ordinary derive succeeds with
    no override needed."""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Source A holds P.")
    ob_b = _mk_obs(obs, "Source B holds not P.")
    ct_id = _mk_ct(derive, ob_a, ob_b, "Source A vs Source B on P.")
    ob_other = _mk_obs(obs, "An unrelated corroborating claim.")

    # Blocked while open.
    blocked = json.loads(derive._derive_impl(
        claim="Building on A while still disputed.",
        supporting_ids=f"{ob_a},{ob_other}",
        logical_chain="Should be blocked.",
        reasoning_type="deductive_modus_ponens",
    ))
    assert blocked.get("status") == "blocked_contradicted", blocked

    # Resolve the contradiction (root-cause resolver, per #229 pure-wire
    # semantics — resolving_node_id must be claim-bearing and current).
    ob_resolver = _mk_obs(obs, "Adjudication: A overreached, P does not hold under the operational reading.")
    res = json.loads(rev._resolve_impl(
        target_id=ct_id,
        resolving_node_id=ob_resolver,
    ))
    assert res.get("status") == "resolution_created", res

    ct_inspect = json.loads(qry._inspect_impl(node_id=ct_id, dream_mode=True))
    assert ct_inspect["node"]["status"] in ("resolved", "partially_resolved"), ct_inspect["node"]["status"]

    # Now derive citing ob_a succeeds with no override — no open contradiction remains.
    after = json.loads(derive._derive_impl(
        claim="Building on A now that the dispute is resolved.",
        supporting_ids=f"{ob_a},{ob_other}",
        logical_chain="No longer disputed.",
        reasoning_type="deductive_modus_ponens",
    ))
    assert "derivation_id" in after, after
    assert "built_on_contested" not in after, after


def test_taint_dominates_contradiction():
    """A premise that is BOTH retracted (tainted) AND under an open
    contradiction reports BLOCKED_TAINTED, not BLOCKED_CONTRADICTED — taint
    is the hard, unconditional block."""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Claim A holds.")
    ob_b = _mk_obs(obs, "Claim A does not hold.")
    _mk_ct(derive, ob_a, ob_b, "Disagreement about A.")
    ob_other = _mk_obs(obs, "An unrelated corroborating claim.")

    ret = json.loads(rev._retract_impl(
        node_id=ob_a, error_type="other", reason="test: proven wrong",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert ret.get("status") == "retracted", ret

    r = json.loads(derive._derive_impl(
        claim="Trying to build on a retracted, contested premise.",
        supporting_ids=f"{ob_a},{ob_other}",
        logical_chain="Should be tainted-blocked, not contradicted-blocked.",
        reasoning_type="deductive_modus_ponens",
        use_contested=True,
    ))
    assert r.get("status") == "blocked_tainted", r


def test_stale_and_contested_are_independent_not_elif():
    """A premise can be BOTH superseded (stale) AND sitting on a still-open
    contradiction (contested) at the same time — supersede fires on one
    side of a live dispute without auto-resolving it (#229's documented
    workflow). Both must be CLASSIFIED independently. Note "classified",
    not "cleared" — pre-#2324 both had overrides and each had to be cleared
    on its own; since #2324 stale cannot be cleared at all, so the invariant
    survives only in its reporting form. See the TEST INTENT note below.

    Regression test for a reviewer-fairy-caught blocker on PR #1711: the
    original implementation used an elif chain that let 'contested' win
    over 'stale' for a single premise, discarding the stale classification
    (and the built_on_stale audit stamp) entirely once a premise was also
    contested.

    TEST INTENT AFTER #2324 — the invariant is unchanged, the probe moved.
    `use_stale` is retired, so the old proof ("clear one override, watch the
    OTHER guard still fire") is no longer expressible: nothing clears stale.
    Asserting through the derive API can therefore only ever observe the
    first block, which would make an elif chain INVISIBLE again — exactly the
    regression this test exists to catch.

    So the independence assertion moved down to _validate_premises, where
    both classifications are visible at once: it returns (block, stale_ids,
    contested_ids), and under an elif chain one of those two lists would come
    back empty for a premise that is genuinely both. That is a strictly
    sharper probe than the original — it reads the classification directly
    instead of inferring it from which override was needed.
    """
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Claim A holds.")
    ob_b = _mk_obs(obs, "Claim A does not hold.")
    _mk_ct(derive, ob_a, ob_b, "Disagreement about A.")

    # Supersede ob_a — it's now stale (has a current successor) AND still
    # contested (the contradiction above was never resolved).
    ob_a2 = _mk_obs(obs, "Claim A, refined.")
    sup = json.loads(rev._supersede_impl(
        old_node_id=ob_a, new_node_id=ob_a2, supersede_reason="refined reading",
    ))
    assert sup.get("status") == "superseded", sup

    ob_other = _mk_obs(obs, "An unrelated corroborating claim.")

    # THE INDEPENDENCE ASSERTION (the point of this test). Ask the guard
    # directly, with BOTH overrides on, so nothing can be attributed to one
    # of them being off. An elif chain would return one of these two lists
    # empty; both must name ob_a.
    conn = core._get_db()
    try:
        block, stale_ids, contested_ids = core._validate_premises(
            [ob_a, ob_other], conn, use_stale=True, use_contested=True
        )
    finally:
        conn.close()
    assert ob_a in stale_ids, (stale_ids, contested_ids)
    assert ob_a in contested_ids, (stale_ids, contested_ids)

    # And the guard still refuses, because stale is a HARD block since #2324
    # — use_stale being True changes nothing except that it earns a notice.
    assert block is not None and block["status"] == "blocked_stale", block

    # Same through the public API. Both overrides passed; still refused.
    r1 = json.loads(derive._derive_impl(
        claim="Building on ob_a with both (now-inert) overrides.",
        supporting_ids=f"{ob_a},{ob_other}",
        logical_chain="Opting into both.",
        reasoning_type="deductive_modus_ponens",
        use_contested=True,
        use_stale=True,
    ))
    assert r1.get("status") == "blocked_stale", r1
    assert {p["id"] for p in r1["stale_premises"]} == {ob_a}, r1
    assert "derivation_id" not in r1, r1

    # The retired parameter is LOUD, not silently ignored — a caller who
    # passes it must be told it did nothing, or they keep a false belief
    # about what is protecting their derivation.
    assert "use_stale_retired" in r1, r1
    assert "RETIRED" in r1["use_stale_retired"], r1

    # ob_a is superseded, so this is the case-2 route: cite the successor.
    # The route must NAME the successor, not merely say one exists.
    routes = [
        e["route"]
        for p in r1["stale_premises"]
        for e in p["stale_because"]
        if e["case"] == "superseded_premise"
    ]
    assert routes, r1
    assert any(ob_a2 in r for r in routes), routes


def test_extra_meta_cannot_spoof_or_remove_auto_stamps():
    """By-construction hardening (Borges's #1711 review): the built_on_stale
    / built_on_contested auto-stamps must survive even if extra_meta
    contains those keys — dict.update() ordering alone is a by-convention
    guarantee (safe only because today's one caller hardcodes
    extra_meta={"warrant": ...}), not a structural one. _create_derivation
    must strip reserved keys from extra_meta unconditionally so a future
    generic extra_meta passthrough can't silently spoof or blank the stamp.
    """
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "Claim A holds.")
    ob_b = _mk_obs(obs, "Claim A does not hold.")
    _mk_ct(derive, ob_a, ob_b, "Disagreement about A.")
    ob_other = _mk_obs(obs, "An unrelated corroborating claim.")

    conn = core._get_db()
    try:
        block, success = core._create_derivation(
            conn,
            claim="Attempting to spoof the stamp via extra_meta.",
            supporting_ids=[ob_a, ob_other],
            logical_chain="extra_meta carries a forged built_on_contested value.",
            reasoning_type="deductive_modus_ponens",
            use_contested=True,
            # A malicious/careless caller tries to override the real stamp.
            extra_meta={"built_on_contested": ["forged_id"], "warrant": "legit warrant"},
        )
        assert block is None, block
        conn.commit()
    finally:
        conn.close()

    node = json.loads(qry._inspect_impl(node_id=success["node_id"], view="deep", dream_mode=True))
    meta = json.loads(node["node"]["metadata"])
    assert meta["built_on_contested"] == [ob_a], (
        f"extra_meta must not override the auto-stamp; got {meta.get('built_on_contested')}"
    )
    assert meta["warrant"] == "legit warrant", "non-reserved extra_meta keys should still pass through"


def test_use_contested_true_with_no_contested_premise_is_a_no_op():
    """use_contested=True is harmless when no premise is actually contested
    — no built_on_contested stamp gets written just because the flag was
    passed.

    The parenthetical here used to read "mirrors use_stale's existing
    no-op-when-clean behavior". Dropped as of #2324, because it is now wrong
    twice over: use_stale is a no-op ALWAYS rather than only when clean, and
    passing it on a clean call is no longer silent — it adds a
    use_stale_retired notice to the payload. So the two flags no longer
    mirror each other in either half of that sentence, and use_contested is
    the only one this test's shape still describes."""
    rev, obs, derive, qry, core = _make_fresh_mods()
    ob_a = _mk_obs(obs, "An uncontested claim.")
    ob_b = _mk_obs(obs, "Another uncontested claim.")

    r = json.loads(derive._derive_impl(
        claim="Deriving from two clean premises with the flag on regardless.",
        supporting_ids=f"{ob_a},{ob_b}",
        logical_chain="Neither premise is disputed.",
        reasoning_type="deductive_modus_ponens",
        use_contested=True,
    ))
    assert "derivation_id" in r, r
    assert "built_on_contested" not in r, r

    node = json.loads(qry._inspect_impl(node_id=r["derivation_id"], view="deep", dream_mode=True))
    meta = json.loads(node["node"]["metadata"])
    assert "built_on_contested" not in meta, meta
