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

    # Inspect ct to confirm metadata (view="deep" for forensic field access)
    ct_inspect = json.loads(qry._inspect_impl(node_id=ct_id, view="deep", dream_mode=True))
    ct_meta = json.loads(ct_inspect["node"]["metadata"])
    assert "stale_by" in ct_meta
    assert ob_a in ct_meta["stale_by"]
    stale_repl = ct_meta.get("stale_replacement")
    assert isinstance(stale_repl, dict), f"stale_replacement should be dict, got {type(stale_repl)}"
    assert stale_repl.get(ob_a) == ob_a_refined, f"expected {{{ob_a}: {ob_a_refined}}}, got {stale_repl}"


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

    # ct should still be clean (view="deep" for forensic field access)
    ct_inspect = json.loads(qry._inspect_impl(node_id=ct_id, view="deep", dream_mode=True))
    ct_meta = json.loads(ct_inspect["node"]["metadata"]) if ct_inspect["node"].get("metadata") else {}
    assert ct_meta.get("stale_by", []) == []


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
    ))
    assert r.get("status") == "retracted", r
    assert r["tainted_contradiction_count"] == 1, r
    assert r["tainted_contradictions"][0]["id"] == ct_id

    ct_inspect = json.loads(qry._inspect_impl(node_id=ct_id, view="deep", dream_mode=True))
    ct_meta = json.loads(ct_inspect["node"]["metadata"])
    assert "tainted_by" in ct_meta
    assert ob_a in ct_meta["tainted_by"]


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
    ))
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
    ))
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
    """When both contradicting sides of a ct are superseded in sequence,
    metadata.stale_replacement must be dict-keyed so both replacements survive.
    metadata.stale_by correctly appends both old IDs (was already correct
    pre-fix). This test documents the fixed behavior from issue #231.

    Previously named test_both_sides_superseded_clobbers_stale_replacement;
    updated when issue #231 landed (PR: fix/stale-replacement-dict-keyed).
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

    ct_inspect = json.loads(qry._inspect_impl(node_id=ct_id, view="deep", dream_mode=True))
    ct_meta = json.loads(ct_inspect["node"]["metadata"])

    # stale_by correctly appends both old IDs (unchanged from pre-fix)
    assert set(ct_meta["stale_by"]) == {ob_a, ob_b}, ct_meta["stale_by"]

    # Fixed dict-keyed behavior: both replacements survive as separate entries.
    stale_repl = ct_meta.get("stale_replacement")
    assert isinstance(stale_repl, dict), f"stale_replacement should be dict, got {type(stale_repl)}"
    assert stale_repl.get(ob_a) == ob_a_refined, f"entry for {ob_a} wrong: {stale_repl}"
    assert stale_repl.get(ob_b) == ob_b_refined, f"entry for {ob_b} wrong: {stale_repl}"


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

    for nid in (ob_a, ob_b):
        node = json.loads(qry._inspect_impl(node_id=nid, view="deep", dream_mode=True))
        meta = json.loads(node["node"]["metadata"]) if node["node"].get("metadata") else {}
        assert "tainted_by" not in meta, f"{nid} should not be tainted by mere contradiction: {meta}"
        assert "stale_by" not in meta, f"{nid} should not be staled by mere contradiction: {meta}"


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
    ))
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
    workflow). Both guards must be cleared independently; clearing only
    use_contested must NOT silently bypass BLOCKED_STALE (or vice versa).

    Regression test for a reviewer-fairy-caught blocker on PR #1711: the
    original implementation used an elif chain that let 'contested' win
    over 'stale' for a single premise, discarding the stale classification
    (and the built_on_stale audit stamp) entirely once a premise was also
    contested.
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

    # use_contested alone must NOT be enough — ob_a is still stale too.
    r1 = json.loads(derive._derive_impl(
        claim="Building on ob_a with only the contested override.",
        supporting_ids=f"{ob_a},{ob_other}",
        logical_chain="Only opting into the contradiction override.",
        reasoning_type="deductive_modus_ponens",
        use_contested=True,
        use_stale=False,
    ))
    assert r1.get("status") == "blocked_stale", r1
    assert {p["id"] for p in r1["stale_premises"]} == {ob_a}, r1

    # use_stale alone must NOT be enough — ob_a is still contested too.
    r2 = json.loads(derive._derive_impl(
        claim="Building on ob_a with only the stale override.",
        supporting_ids=f"{ob_a},{ob_other}",
        logical_chain="Only opting into the stale override.",
        reasoning_type="deductive_modus_ponens",
        use_contested=False,
        use_stale=True,
    ))
    assert r2.get("status") == "blocked_contradicted", r2
    assert {p["id"] for p in r2["contested_premises"]} == {ob_a}, r2

    # Both overrides together clear it, and both audit stamps are written.
    r3 = json.loads(derive._derive_impl(
        claim="Building on ob_a with both overrides.",
        supporting_ids=f"{ob_a},{ob_other}",
        logical_chain="Opting into both.",
        reasoning_type="deductive_modus_ponens",
        use_contested=True,
        use_stale=True,
    ))
    assert "derivation_id" in r3, r3
    assert r3["built_on_contested"] == [ob_a], r3
    assert r3["built_on_stale"] == [ob_a], r3


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
    passed (mirrors use_stale's existing no-op-when-clean behavior)."""
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
