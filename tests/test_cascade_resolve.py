"""Tests for engram_cascade_resolve (engram-alpha#2013 slice 4 — the
adjudication verb).

What each group pins, and why it is here rather than left to review:

  1. Eligibility (§4 / §11). Acknowledge is available iff NO affected direct
     premise resolved as retracted — INCLUDING the cascade root, which
     carries no marker of its own. The root branch is the modal case ("the
     premise I derived from is the thing that got retracted") and is exactly
     the branch a reader would most plausibly delete as redundant.
  2. Invariant 6 — a marker resolves only while its node is on the frontier.
     Enforced at the write, not trusted.
  3. No re-cascade on resolution (§6 / §10.2), for BOTH the retracted and the
     superseded shapes, with the counterpart guard that an ORDINARY retract /
     supersede still cascades — the seam must not disarm the normal path.
  4. Per-node resolution (§10.2): one judgement stamps every pending marker
     on the node, across all its events.
  5. Trivial auto-resolution (§4), positive AND negative — it must fire when
     nothing moved and must NOT fire when a premise actually died.
  6. Confidence recompute, transitive (§4) — the property the spec calls out
     as most likely to be implemented one hop deep.
  7. Pure-wire (§10.2): the verb never creates a node.
  8. Payload surface: replacement_id required iff superseded, and
     cascade_triggering NOT reachable from the MCP allowlist.

Import-pattern contract (#948 wave B — R1): imports engram_core,
engram_revision, engram_observation, engram_derive directly; contains no
``import server`` except in the one payload-allowlist test, which is ABOUT
the server surface and imports it locally.
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

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_derive
import engram_observation
import engram_revision


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_cascade_resolve_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _make_fresh_mods():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, derive, core


_URL_SEQ = [0]


def _seed_ob(obs, claim="Test claim.", quote_type="attributed_analysis"):
    _URL_SEQ[0] += 1
    r = json.loads(obs._add_observation_impl(
        url=f"https://example.com/src{_URL_SEQ[0]}",
        title=f"Source {_URL_SEQ[0]}",
        quoted_text=claim,
        interpretation=f"Interpretation: {claim}",
        claim=claim,
        quote_type=quote_type,
    ))
    assert "error" not in r, f"Failed to create observation: {r}"
    return r["observation_id"]


def _seed_dv(derive, claim, supporting_ids):
    if isinstance(supporting_ids, list):
        supporting_ids = ",".join(supporting_ids)
    r = json.loads(derive._derive_impl(
        claim=claim,
        supporting_ids=supporting_ids,
        logical_chain=f"Derivation: {claim}",
        reasoning_type="deductive_modus_ponens",
    ))
    assert "error" not in r, r
    return r["derivation_id"]


def _seed_dv_typed(derive, claim, supporting_ids, reasoning_type):
    """_seed_dv with an explicit reasoning_type — the DISCOUNT is what the
    fixpoint test needs, so it cannot use the default everywhere."""
    r = json.loads(derive._derive_impl(
        claim=claim,
        supporting_ids=",".join(supporting_ids),
        logical_chain=f"Derivation: {claim}",
        reasoning_type=reasoning_type,
    ))
    assert "error" not in r, r
    return r["derivation_id"]


def _markers(core, node_id=None, status=None):
    conn = core._get_db()
    try:
        sql = "SELECT * FROM cascade_markers WHERE 1=1"
        args = []
        if node_id:
            sql += " AND node_id = ?"
            args.append(node_id)
        if status:
            sql += " AND status = ?"
            args.append(status)
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _events(core):
    conn = core._get_db()
    try:
        return {r["cascade_event"] for r in conn.execute(
            "SELECT DISTINCT cascade_event FROM cascade_markers").fetchall()}
    finally:
        conn.close()


def _conf(core, node_id):
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT confidence FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return row["confidence"] if row else None
    finally:
        conn.close()


def _node_count(core):
    conn = core._get_db()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
    finally:
        conn.close()


def _premises(core, node_id):
    conn = core._get_db()
    try:
        return sorted(r["target_id"] for r in conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? "
            "AND relation = 'derives_from'", (node_id,)).fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1 — eligibility: the cascade ROOT counts as a premise resolving as root_op
# ---------------------------------------------------------------------------


def test_acknowledge_refused_when_the_root_premise_was_retracted():
    """The 2026-08-26 amendment, and the single most deletable branch in the
    implementation.

    The cascade root never carries a marker for its own event — the walker
    seeds it into `visited`. So a node whose ONLY affected premise is the
    root has an empty marker-derived affected set, and a reader who trusts
    _dead_premises alone would conclude "no premise died, acknowledge is
    fine". That is backwards for the MODAL cascade: the thing that got
    retracted is usually the premise directly below you.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Premise that turns out to be wrong.")
    dv1 = _seed_dv(derive, "Depends on the wrong premise.", [ob])

    ret = json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="never held"))
    assert "error" not in ret, ret

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged", note="still fine?"))
    assert "error" in resp, f"acknowledge must be refused: {resp}"
    assert resp["acknowledge_blocked_by"] == [ob], resp
    assert resp["eligible"] == ["retracted", "superseded"], resp

    # And the marker is untouched by the refusal.
    assert len(_markers(core, dv1, "pending")) == 1, _markers(core, dv1)


def test_acknowledge_allowed_when_the_root_premise_was_only_superseded():
    """The mirror of the above — only RETRACT removes a leg from the
    argument. A superseded premise still supports the claim, differently, so
    acknowledge stays available and the agent judges the delta itself.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Original premise.", quote_type="official_statement")
    ob2 = _seed_ob(obs, "Refined premise.", quote_type="editorial")
    dv1 = _seed_dv(derive, "Depends on the premise.", [ob])

    sup = json.loads(rev._supersede_impl(
        old_node_id=ob, new_node_id=ob2, supersede_reason="refined"))
    assert "error" not in sup, sup

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged",
        note="the refinement does not touch my claim"))
    assert "error" not in resp, resp
    assert resp["resolution_kind"] == "acknowledged"
    assert len(_markers(core, dv1, "pending")) == 0, _markers(core, dv1)
    resolved = _markers(core, dv1)
    assert all(m["resolution_kind"] == "acknowledged" for m in resolved), resolved


def test_acknowledgement_defers_the_derives_from_edge_and_says_so():
    """§4 requires an acknowledgement to record a derives_from edge to the
    superseding node. THIS SLICE DOES NOT WRITE IT, and the response says
    so rather than staying silent.

    The reason is a collision, not a difficulty. The acknowledging node
    exists BEFORE its premise's replacement — that is the situation an
    acknowledgement arises in — so the edge is always backwards in time on
    a `temporal_causal: True` relation (#2357's rename of dag_check —
    creation-order, not the new `dag` edge-walk flag). Measured on this exact
    fixture before the
    deferral: source.created_at < target.created_at, every time.

    The test asserts the OLD premise edge is untouched (provenance is
    extended, never rewritten) and that the deferral is reported with the
    pair that would have been written, so the information is not lost —
    only the illegal write is.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Original premise.", quote_type="official_statement")
    dv1 = _seed_dv(derive, "Depends on the premise.", [ob])
    # Replacement created AFTER the dependent — the realistic ordering, and
    # the one that makes the edge backwards in time. A fixture that creates
    # it first cannot see the defect at all.
    ob2 = _seed_ob(obs, "Refined premise.", quote_type="editorial")

    assert _premises(core, dv1) == [ob]

    json.loads(rev._supersede_impl(
        old_node_id=ob, new_node_id=ob2, supersede_reason="refined"))
    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged"))
    assert "error" not in resp, resp

    assert resp["acknowledgement_edges_deferred"] == [
        {"premise_id": ob, "replacement_id": ob2}
    ], resp["acknowledgement_edges_deferred"]
    assert resp["acknowledgement_edge_note"], "the deferral must be stated"
    assert "NOT WRITE" in resp["acknowledgement_edge_note"]
    assert "HOLE IS REAL" in resp["acknowledgement_edge_note"], (
        "the note must name the blindness the deferral leaves, not just the "
        "reason for it")

    assert _premises(core, dv1) == [ob], (
        "no edge may be written, and the original premise edge is untouched")


def test_acknowledgement_writes_no_backwards_in_time_edge():
    """The reason for the deferral, pinned directly rather than trusted.

    `derives_from` carries `temporal_causal: True` (#2357's rename of
    dag_check). If any acknowledgement ever
    writes an edge whose source predates its target, this fails — which is
    what the removed writer did on every single acknowledgement.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Original premise.", quote_type="official_statement")
    dv1 = _seed_dv(derive, "Depends on the premise.", [ob])
    ob2 = _seed_ob(obs, "Refined premise.", quote_type="editorial")
    json.loads(rev._supersede_impl(
        old_node_id=ob, new_node_id=ob2, supersede_reason="refined"))
    json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged"))

    conn = core._get_db()
    try:
        assert core.EDGE_CLASSIFICATIONS["derives_from"]["temporal_causal"] is True, (
            "if derives_from ever stops being creation-order-checked, revisit "
            "the deferral — the collision this pins would no longer exist. "
            "#2357 renamed this flag from dag_check; the creation-order "
            "semantics this assertion depends on are the renamed flag's, not "
            "the new `dag` edge-walk flag's.")
        bad = conn.execute(
            "SELECT e.source_id, e.target_id FROM edges e "
            "JOIN nodes s ON s.id = e.source_id "
            "JOIN nodes t ON t.id = e.target_id "
            "WHERE e.relation = 'derives_from' "
            "AND s.created_at < t.created_at"
        ).fetchall()
    finally:
        conn.close()
    assert bad == [], (
        f"an acknowledgement wrote a backwards-in-time derives_from edge: "
        f"{[(r['source_id'], r['target_id']) for r in bad]}")


def test_acknowledgement_cannot_close_a_cascade_cycle():
    """The worse half of the collision, constructed through PUBLIC VERBS
    only: base -> P, X derives from P, P-prime derives from X (legal, X is
    unmarked at that moment), supersede P -> P-prime, acknowledge X.

    The removed writer closed `X -> P' -> X`. Measured consequences at the
    time: the confidence recompute stopped being idempotent — 0.641, 0.567,
    0.503, 0.446 across four IDENTICAL runs, decaying each time — and once
    both members carried markers the frontier reported frontier_count 0
    against a non-empty pending set, so both refused resolution forever.

    The idempotence half matters beyond the cycle: this impl's phase split
    is not one transaction, and its entire safety argument is that the
    recompute can simply be re-run. That argument is only true while this
    holds.
    """
    rev, obs, derive, core = _make_fresh_mods()

    base = _seed_ob(obs, "Base observation.", quote_type="official_statement")
    p = _seed_dv(derive, "P, the premise that will be superseded.", [base])
    x = _seed_dv(derive, "X, depends on P.", [p])
    p2 = _seed_dv(derive, "P-prime, which happens to cite X.", [x])

    s = json.loads(rev._supersede_impl(
        old_node_id=p, new_node_id=p2, supersede_reason="r"))
    assert "error" not in s, s
    r = json.loads(rev._cascade_resolve_impl(
        node_id=x, resolution_kind="acknowledged"))
    assert "error" not in r, r

    conn = core._get_db()
    try:
        edges = {(e["source_id"], e["target_id"]) for e in conn.execute(
            "SELECT source_id, target_id FROM edges "
            "WHERE relation = 'derives_from'").fetchall()}
        assert not ((x, p2) in edges and (p2, x) in edges), (
            f"acknowledgement closed a cascade cycle: {sorted(edges)}")

        vals = []
        for _ in range(4):
            rev._cascade_recompute_confidence_transitive(conn, p)
            vals.append(conn.execute(
                "SELECT confidence FROM nodes WHERE id = ?", (x,)).fetchone()["confidence"])
        conn.commit()
    finally:
        conn.close()
    assert len(set(vals)) == 1, (
        f"the recompute must be idempotent — the phase-split safety argument "
        f"rests on it. Got {vals}")


# ---------------------------------------------------------------------------
# 2 — invariant 6: a marker resolves only from the frontier
# ---------------------------------------------------------------------------


def test_resolving_a_blocked_node_is_refused_and_names_the_blockers():
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Chain premise.")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])
    dv2 = _seed_dv(derive, "Transitive dependent.", [dv1])

    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="wrong"))

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv2, resolution_kind="retracted"))
    assert "error" in resp, resp
    assert resp["blocking_ancestors"] == [dv1], resp
    assert "engram_cascade_frontier" in resp["next_call"], resp
    assert len(_markers(core, dv2, "pending")) == 1


# ---------------------------------------------------------------------------
# 3 — no re-cascade on resolution, both shapes; normal path still cascades
# ---------------------------------------------------------------------------


def test_retract_resolution_opens_no_new_DERIVATION_cascade_event():
    """Renamed and re-scoped in round 3. The old name — "opens no new
    cascade event" — became FALSE once the contradiction fix landed: a
    resolution DOES mint a contradiction-only event when the node carries
    contradictions. The fixture here has none, so the assertion is
    unchanged; only the claim its name makes is narrowed to what holds
    universally. See test_resolution_marks_contradictions_on_a_dedicated_event
    for the case the old name would have mis-described.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Chain premise.")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])
    dv2 = _seed_dv(derive, "Transitive dependent.", [dv1])

    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="wrong"))
    events_before = _events(core)
    assert len(events_before) == 1, events_before
    assert len(_markers(core, dv2)) == 1

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="retracted",
        note="argument is broken", error_type="wrong_citation"))
    assert "error" not in resp, resp
    assert "SUPPRESSED" in resp["re_cascade"], resp["re_cascade"]

    assert _events(core) == events_before, (
        "resolution must not open a new cascade event")
    assert len(_markers(core, dv2)) == 1, (
        "dv2 must not acquire a second marker — it already carries the "
        "root event's, and a second would force it to be adjudicated twice")

    # Invariant 7's FIRST line, which pulls the OPPOSITE way from the
    # no-re-cascade half and is therefore the thing a cascade_triggering
    # guard could silently swallow: "Superseding a marked node must also
    # resolve its marker." The same holds for retracting one. If the
    # own-marker resolution were ever moved inside the cascade guard, a
    # `retracted` resolution would suppress the propagation correctly AND
    # leave dv1's own markers pending forever — the node would stay blocked
    # after being properly adjudicated, with nothing to signal it.
    #
    # This assertion exists because a mutation moving _resolve_own_markers
    # inside the guard SURVIVED the first version of this file on the
    # retract side while being caught on the supersede side (the supersede
    # test happened to read resolution_replacement_id). Predicted by
    # Ariadne's design read on #2342 before she had seen the diff.
    own = _markers(core, dv1)
    assert own, "dv1 must still carry its marker rows (never deleted)"
    assert all(m["status"] == "resolved" for m in own), own
    assert {m["resolution_kind"] for m in own} == {"retracted"}, own


def test_supersede_resolution_opens_no_new_DERIVATION_cascade_event():
    """Invariant 7 states this for the supersede side by name: such a
    supersede is "a PARTIAL RESOLUTION of the existing event, never a new
    one".

    Scoped to the DERIVATION cascade in round 3 — invariant 7's proof rests
    on "child", "transitivity" and "citing", all derivation-graph terms,
    none of which reach a `contradicts` edge. A contradiction-only event is
    outside what that proof covers; this fixture has no contradictions.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Chain premise.")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])
    dv2 = _seed_dv(derive, "Transitive dependent.", [dv1])
    dv1b = _seed_dv(derive, "Direct dependent, restated.", [ob])

    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="wrong"))
    events_before = _events(core)

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="superseded", replacement_id=dv1b,
        note="claim survives on the remaining premises"))
    assert "error" not in resp, resp
    assert _events(core) == events_before, resp
    assert len(_markers(core, dv2)) == 1, _markers(core, dv2)
    resolved = _markers(core, dv1)
    assert [m["resolution_kind"] for m in resolved] == ["superseded"], resolved
    assert resolved[0]["resolution_replacement_id"] == dv1b, resolved


def test_ordinary_retract_and_supersede_still_cascade():
    """The counterpart guard. cascade_triggering defaults to True, and the
    categorical line is marked-vs-unmarked rather than which verb was
    called — so the seam must be invisible on the normal path.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Premise A.")
    dv = _seed_dv(derive, "Dependent of A.", [ob])
    r = json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="wrong"))
    assert "error" not in r, r
    assert len(_markers(core, dv, "pending")) == 1, "normal retract must cascade"

    ob2 = _seed_ob(obs, "Premise B.")
    ob2b = _seed_ob(obs, "Premise B, refined.")
    dv2 = _seed_dv(derive, "Dependent of B.", [ob2])
    s = json.loads(rev._supersede_impl(
        old_node_id=ob2, new_node_id=ob2b, supersede_reason="refined"))
    assert "error" not in s, s
    assert len(_markers(core, dv2, "pending")) == 1, "normal supersede must cascade"


# ---------------------------------------------------------------------------
# 4 — per-node resolution across all of a node's events
# ---------------------------------------------------------------------------


def test_one_judgement_resolves_every_pending_event_on_the_node():
    rev, obs, derive, core = _make_fresh_mods()

    ob_a = _seed_ob(obs, "Premise A.", quote_type="official_statement")
    ob_a2 = _seed_ob(obs, "Premise A refined.", quote_type="official_statement")
    ob_b = _seed_ob(obs, "Premise B.", quote_type="official_statement")
    ob_b2 = _seed_ob(obs, "Premise B refined.", quote_type="official_statement")
    dv = _seed_dv(derive, "Depends on both.", [ob_a, ob_b])

    json.loads(rev._supersede_impl(old_node_id=ob_a, new_node_id=ob_a2,
                                   supersede_reason="a"))
    json.loads(rev._supersede_impl(old_node_id=ob_b, new_node_id=ob_b2,
                                   supersede_reason="b"))
    assert len(_markers(core, dv, "pending")) == 2, _markers(core, dv)

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv, resolution_kind="acknowledged"))
    assert "error" not in resp, resp
    assert resp["resolved_marker_count"] == 2, resp
    assert len(_markers(core, dv, "pending")) == 0, _markers(core, dv)
    assert {m["resolution_kind"] for m in _markers(core, dv)} == {"acknowledged"}


# ---------------------------------------------------------------------------
# 5 — trivial auto-resolution, positive and negative
# ---------------------------------------------------------------------------


def test_trivial_child_auto_resolves_when_nothing_actually_moved():
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Premise.", quote_type="official_statement")
    ob2 = _seed_ob(obs, "Premise refined.", quote_type="official_statement")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])
    dv2 = _seed_dv(derive, "Transitive dependent.", [dv1])

    json.loads(rev._supersede_impl(old_node_id=ob, new_node_id=ob2,
                                   supersede_reason="refined"))
    assert len(_markers(core, dv2, "pending")) == 1

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged"))
    assert "error" not in resp, resp

    assert [a["node_id"] for a in resp["auto_resolved_trivial"]] == [dv2], resp
    assert len(_markers(core, dv2, "pending")) == 0, _markers(core, dv2)
    assert {m["resolution_kind"] for m in _markers(core, dv2)} == {"acknowledged"}


def test_trivial_does_not_fire_when_a_premise_actually_died():
    """The negative half, and the one that matters: an auto-resolution that
    fires when a premise DID move would silently retire an adjudication the
    agent never made.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Premise.")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])
    dv2 = _seed_dv(derive, "Transitive dependent.", [dv1])

    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="wrong"))
    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="retracted", error_type="wrong_citation"))
    assert "error" not in resp, resp

    assert resp["auto_resolved_trivial"] == [], resp
    assert len(_markers(core, dv2, "pending")) == 1, (
        "dv2's premise genuinely died — it must reach the agent, not "
        "auto-resolve")


# ---------------------------------------------------------------------------
# 6 — confidence recompute, and it must be transitive
# ---------------------------------------------------------------------------


def test_acknowledge_recompute_is_inert_while_the_ack_edge_is_deferred():
    """§4 calls this out explicitly: recomputing X changes X, and X's
    children were computed from X's OLD value, so one hop is not enough.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Strong premise.", quote_type="official_statement")
    ob2 = _seed_ob(obs, "Weak replacement.", quote_type="editorial")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])
    dv2 = _seed_dv(derive, "Transitive dependent.", [dv1])

    dv1_before, dv2_before = _conf(core, dv1), _conf(core, dv2)
    assert dv1_before == round(0.85 * 0.98, 3), dv1_before

    json.loads(rev._supersede_impl(old_node_id=ob, new_node_id=ob2,
                                   supersede_reason="weaker"))
    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged"))
    assert "error" not in resp, resp

    dv1_after, dv2_after = _conf(core, dv1), _conf(core, dv2)

    # NOTHING CHANGED, and that is the currently-correct answer — it is also
    # a cost of the deferred acknowledgement edge, recorded here rather than
    # buried. §4 couples its two requirements more tightly than it says: the
    # recompute only has anything to read on the acknowledge path BECAUSE
    # the ack edge brings the replacement into the premise set. Without it,
    # dv1's premises are unchanged and the superseded premise's own
    # confidence is untouched by being superseded, so "confidence is ALWAYS
    # recomputed" runs and finds nothing.
    #
    # This test therefore no longer demonstrates transitivity. The diamond
    # test below does, driven by a path that does not depend on the edge.
    assert dv1_after == dv1_before == round(0.85 * 0.98, 3), (dv1_before, dv1_after)
    assert dv2_after == dv2_before, (dv2_before, dv2_after)
    assert resp["confidence_recompute"]["changed"] == [], (
        "with the ack edge deferred there is nothing for an acknowledgement "
        "to recompute — see acknowledgement_edge_note")
    assert resp["acknowledgement_edges_deferred"], (
        "and the deferral that causes it must be reported")


def test_recompute_skips_a_conjecture_and_leaves_its_confidence_untouched():
    """A skip is REPORTED and the stored value is LEFT ALONE — never
    overwritten with a fallback, which afterwards is indistinguishable from a
    computed number. A conjecture's confidence is clamped at creation and is a
    judgement, not a formula output, so there is no input set to recompute
    from; the honest answer is to decline, loudly.

    Asserting the disposition alone would not catch a fallback write — this
    reads the stored confidence back and pins it.
    """
    _rev, _obs, _derive, core = _make_fresh_mods()
    import engram_epistemic
    epi = importlib.reload(engram_epistemic)
    r = json.loads(epi._add_conjecture_impl(
        claim="A speculative claim.", basis="Hunch, no evidence yet.",
        initial_confidence=0.42))
    assert "error" not in r, r
    cj = r.get("conjecture_id") or r.get("node_id")
    before = _conf(core, cj)
    assert before is not None

    conn = core._get_db()
    try:
        d = core._recompute_node_confidence(conn, cj, write=True)
        conn.commit()
    finally:
        conn.close()

    assert d["disposition"] == "skipped", d
    assert "no runtime creation-time confidence formula" in d["reason"], d
    assert _conf(core, cj) == before, (
        "a skipped type's stored confidence must be left exactly as it was")

    conn = core._get_db()
    try:
        missing = core._recompute_node_confidence(conn, "dv_9999", write=False)
        assert missing["disposition"] == "missing", missing
    finally:
        conn.close()

    assert "conjecture" not in core.CONFIDENCE_RECOMPUTABLE_TYPES
    assert "theory" not in core.CONFIDENCE_RECOMPUTABLE_TYPES
    assert core.CONFIDENCE_RECOMPUTABLE_TYPES <= core.CLAIM_BEARING_TYPES


def test_recompute_needs_a_fixpoint_not_one_ordered_pass_on_a_diamond():
    """The mutation that survived the first version of this file, and the
    reason it survived is the point: on a straight chain the collection
    order happens to BE topological, so a single pass is indistinguishable
    from a fixpoint. It stops being so on a diamond.

    Shape: dv1 -> {A, B}, and C derives from BOTH A and B. The collection
    walk is depth-first from dv1's dependents in edge order, so it reaches
    C THROUGH A before it has ever visited B: the order is [dv1, A, C, B].
    C is therefore computed from B's OLD confidence on pass 1.

    B carries a much steeper discount (inductive_analogy, 0.70) than A
    (deductive_modus_ponens, 0.98), so B is the weakest link into C both
    before and after — which is what makes C's stale-B value differ from its
    settled one, rather than being masked by min() picking A either way.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Strong premise.", quote_type="official_statement")
    dv1 = _seed_dv(derive, "Root of the diamond.", [ob])
    a = _seed_dv_typed(derive, "Left arm.", [dv1], "deductive_modus_ponens")
    b = _seed_dv_typed(derive, "Right arm, steep discount.", [dv1],
                       "inductive_analogy")
    c = _seed_dv_typed(derive, "Apex, depends on both arms.", [a, b],
                       "deductive_modus_ponens")

    # Driven by changing the ROOT premise's confidence and running the
    # helper directly, rather than through a resolution. While the §4
    # acknowledgement edge is deferred, no resolution shape can produce a
    # confidence change at all (see
    # test_acknowledge_recompute_is_inert_while_the_ack_edge_is_deferred),
    # so routing this through the verb would test nothing. The fixpoint is
    # a property of the helper and is worth pinning independently of what
    # currently drives it.
    # The recompute is a pure function of STORED INPUTS, so editing a
    # confidence directly is undone by the first pass. The input for an
    # observation is its quote_type — change that and the whole diamond
    # follows. (Learned by writing the naive version first and watching it
    # restore 0.833.)
    conn = core._get_db()
    try:
        conn.execute("UPDATE nodes SET quote_type = 'editorial' WHERE id = ?",
                     (ob,))
        conn.commit()
        resp = {"confidence_recompute":
                rev._cascade_recompute_confidence_transitive(conn, ob)}
        conn.commit()
    finally:
        conn.close()

    dv1_after = _conf(core, dv1)
    a_after, b_after, c_after = _conf(core, a), _conf(core, b), _conf(core, c)

    assert dv1_after == round(0.35 * 0.98, 3), dv1_after
    assert a_after == round(dv1_after * 0.98, 3), a_after
    assert b_after == round(dv1_after * 0.70, 3), b_after
    assert b_after < a_after, (a_after, b_after)
    # The settled value: C follows its WEAKEST arm, B.
    assert c_after == round(b_after * 0.98, 3), (
        f"C must settle on B's NEW confidence. Got {c_after}; a single "
        f"ordered pass would leave it computed from B's stale value.")
    assert resp["confidence_recompute"]["passes"] >= 2, (
        "the diamond must actually require more than one pass — if this "
        "drops to 1 the fixture has stopped exercising the fixpoint")


# ---------------------------------------------------------------------------
# 7 — pure-wire
# ---------------------------------------------------------------------------


def test_resolution_never_creates_a_node():
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Premise.", quote_type="official_statement")
    ob2 = _seed_ob(obs, "Premise refined.", quote_type="official_statement")
    dv1 = _seed_dv(derive, "Dependent.", [ob])
    json.loads(rev._supersede_impl(old_node_id=ob, new_node_id=ob2,
                                   supersede_reason="refined"))

    before = _node_count(core)
    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged"))
    assert "error" not in resp, resp
    assert _node_count(core) == before, "pure-wire: no node may be created"


# ---------------------------------------------------------------------------
# 8 — payload surface
# ---------------------------------------------------------------------------


def test_replacement_id_required_iff_superseded():
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Premise.", quote_type="official_statement")
    ob2 = _seed_ob(obs, "Premise refined.", quote_type="official_statement")
    dv1 = _seed_dv(derive, "Dependent.", [ob])
    json.loads(rev._supersede_impl(old_node_id=ob, new_node_id=ob2,
                                   supersede_reason="refined"))

    missing = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="superseded"))
    assert "error" in missing and "replacement_id is required" in missing["error"]
    assert "PURE-WIRE" in missing["error"], missing

    surplus = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged", replacement_id=ob2))
    assert "error" in surplus, surplus
    assert "only meaningful" in surplus["error"], surplus

    unknown = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="superseded", replacement_id="dv_9999"))
    assert "error" in unknown and "not found" in unknown["error"], unknown

    bad_kind = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledge"))
    assert "error" in bad_kind and "Invalid resolution_kind" in bad_kind["error"]

    # every refusal above must have left the marker alone
    assert len(_markers(core, dv1, "pending")) == 1, _markers(core, dv1)


def test_unmarked_and_non_current_nodes_are_refused():
    rev, obs, derive, core = _make_fresh_mods()
    ob = _seed_ob(obs, "Premise.")
    dv = _seed_dv(derive, "Dependent.", [ob])

    clean = json.loads(rev._cascade_resolve_impl(
        node_id=dv, resolution_kind="acknowledged"))
    assert "error" in clean and "no pending cascade markers" in clean["error"]

    gone = json.loads(rev._cascade_resolve_impl(
        node_id="ob_9999", resolution_kind="acknowledged"))
    assert "error" in gone and "not found" in gone["error"], gone


def test_cascade_triggering_is_not_reachable_from_the_mcp_payload():
    """The seam must stay internal: an agent that could pass
    cascade_triggering=False to engram_retract could silently disarm the
    cascade on an ordinary revision, which is the one thing the propagation
    process is never allowed to skip (§3A: "never refuses to mark").
    """
    _make_fresh_mods()  # never let a payload test reach a non-test DB
    import server  # local — this test is ABOUT the server surface

    assert "cascade_triggering" not in server._CASCADE_RESOLVE_FIELDS
    assert "cascade_triggering" not in server._RETRACT_FIELDS
    assert "cascade_triggering" not in server._SUPERSEDE_FIELDS

    # The allowlist rejects before any DB access, so this never retracts.
    err = json.loads(server.engram_retract(json.dumps({
        "node_id": "ob_0001", "error_type": "other", "reason": "x",
        "cascade_triggering": False,
    })))
    assert "error" in err and "Unknown fields" in err["error"], err


# ---------------------------------------------------------------------------
# 9 — round-1 reviewer blockers, pinned
# ---------------------------------------------------------------------------


def test_no_convergence_warning_on_the_ordinary_paths():
    """Blocker 1. The warning claims the recompute is "PARTIAL, not final"
    and blames a CASCADE_RELATIONS cycle. Inferring it from
    ``passes >= max_passes`` fires it on the MODAL path, where the result is
    in fact complete and correct — a false claim about a mechanism, emitted
    by the machine itself, which is the worst place for one because a reader
    has no way to check it.

    Two measured false positives, both ordinary:
      * leaf resolved retracted/superseded -> `order` empty, max_passes == 1,
        the single pass trips `1 >= 1`;
      * leaf acknowledged with a real confidence change -> one node,
        max_passes == 2, pass 1 changes it, pass 2 confirms, `2 >= 2`.

    Convergence is now tracked explicitly (a pass that changed nothing),
    never inferred from the bound.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Premise A.")
    dv_leaf = _seed_dv(derive, "Leaf.", [ob])
    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="w"))
    r = json.loads(rev._cascade_resolve_impl(
        node_id=dv_leaf, resolution_kind="retracted", error_type="other"))
    assert "error" not in r, r
    cr = r["confidence_recompute"]
    assert cr["nodes_considered"] == 0, cr
    assert "convergence_warning" not in cr, cr

    # Case B — a recompute that DOES change something. Driven by editing the
    # premise directly and running the helper, because while the §4
    # acknowledgement edge is deferred no resolution shape can produce a
    # confidence change (see the inertness test). The warning must stay
    # absent on a run that converges after genuinely changing a value, which
    # is the case `passes >= max_passes` got wrong (2 >= 2).
    ob2 = _seed_ob(obs, "Premise B.", quote_type="official_statement")
    dv_leaf2 = _seed_dv(derive, "Leaf two.", [ob2])
    conn = core._get_db()
    try:
        conn.execute("UPDATE nodes SET confidence = 0.35 WHERE id = ?", (ob2,))
        conn.commit()
        cr2 = rev._cascade_recompute_confidence_transitive(conn, ob2)
        conn.commit()
    finally:
        conn.close()
    assert cr2["changed"], "this fixture must actually change a confidence"
    assert cr2["passes"] >= 2, cr2
    assert "convergence_warning" not in cr2, cr2


def test_resolution_marks_contradictions_on_a_dedicated_event():
    """Blocker 2 (round 1) and its own fix's blocker (round 2), pinned
    together because the second only exists because of the first.

    Contradictions are non-claim-bearing, so _walk_cascade_downstream never
    reaches one — they are marked ONLY by a query against the operated
    node's own contradicts edges. A contradiction on a MID-CHAIN node is
    therefore covered by NOTHING: the root's cascade ran that query against
    its own edges. Suppressing the resolution path's contradiction marking
    meant such a contradiction would never be reviewed at all.

    The first fix reused the node's pre-existing event ids, to honour
    invariant 7's "never a new one". That collides: a marker row records ONE
    (root_node, root_op) pair, and `UNIQUE(node_id, cascade_event)` plus
    `INSERT OR IGNORE` make a second write on the same event a SILENT no-op
    — while the response still reported the contradiction as marked.

    A dedicated contradiction-only event fixes it without touching what
    invariant 7 protects: no claim-bearing node is marked by it, so no
    claim-bearing node's adjudication count changes.
    """
    rev, obs, derive, core = _make_fresh_mods()
    import engram_derive as _ed

    ob = _seed_ob(obs, "Root premise.")
    d1 = _seed_dv(derive, "First dependent.", [ob])
    d2 = _seed_dv(derive, "Second dependent.", [ob])
    c = json.loads(_ed._contradict_impl(
        node_id_a=d1, node_id_b=d2, description="conflict"))
    assert "error" not in c, c
    ct = c.get("contradiction_id") or c.get("node_id")

    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="w"))
    assert _markers(core, ct) == [], (
        "the root's cascade cannot reach a mid-chain node's contradiction — "
        "this is the structural fact the whole fix rests on, so pin it")

    clean = _seed_ob(obs, "Clean basis.")
    d1b = _seed_dv(derive, "First dependent, restated.", [clean])
    r1 = json.loads(rev._cascade_resolve_impl(
        node_id=d1, resolution_kind="superseded", replacement_id=d1b))
    assert "error" not in r1, r1
    assert r1["marked_contradictions"] == [ct], r1

    r2 = json.loads(rev._cascade_resolve_impl(
        node_id=d2, resolution_kind="retracted", error_type="other"))
    assert "error" not in r2, r2

    # THE COLLISION ASSERTION: d2's resolution must be RECORDED, not merely
    # reported. Reusing the pre-existing event made the second write a
    # silent no-op while still listing the ct in marked_contradictions.
    ms = _markers(core, ct)
    roots = {(m["root_node"], m["root_op"]) for m in ms}
    assert roots == {(d1, "supersede"), (d2, "retract")}, (
        f"both resolutions must be recorded on the contradiction, not just "
        f"reported: {ms}")
    assert r2["marked_contradictions"] == [ct], r2
    assert len({m["cascade_event"] for m in ms}) == 2, ms
    assert r1["contradiction_cascade_event"] != r2["contradiction_cascade_event"]

    # ...and no claim-bearing node picked up a marker from either
    # contradiction-only event — which is what invariant 7 actually protects.
    for ev in (r1["contradiction_cascade_event"],
               r2["contradiction_cascade_event"]):
        conn = core._get_db()
        try:
            rows = conn.execute(
                "SELECT cm.node_id, n.type FROM cascade_markers cm "
                "JOIN nodes n ON n.id = cm.node_id WHERE cm.cascade_event = ?",
                (ev,),
            ).fetchall()
        finally:
            conn.close()
        assert rows, ev
        assert all(r["type"] == "contradiction" for r in rows), rows


def test_affected_contradiction_ids_does_not_return_the_node_itself():
    """A contradiction node OWNS its contradicts edges, so querying with a
    ct_ id matched its own edges and returned itself — which is not "a
    contradiction that has node_id as a side" and contradicted the helper's
    own docstring. Harmless at today's call sites only because each
    re-fetches with is_current=1, i.e. defused by an unrelated guard.
    """
    rev, obs, derive, core = _make_fresh_mods()
    import engram_derive as _ed

    a = _seed_ob(obs, "Side A.")
    b = _seed_ob(obs, "Side B.")
    c = json.loads(_ed._contradict_impl(
        node_id_a=a, node_id_b=b, description="conflict"))
    ct = c.get("contradiction_id") or c.get("node_id")

    conn = core._get_db()
    try:
        assert rev._affected_contradiction_ids(conn, a) == [ct]
        assert rev._affected_contradiction_ids(conn, ct) == [], (
            "a contradiction is not one of its own affected contradictions")
    finally:
        conn.close()


def test_affected_contradiction_ids_ignores_a_malformed_contradicts_edge():
    """The `ct_` prefix filter is DEFENSIVE — `engram_contradict` always
    writes ct -> side, so no live write path can produce a contradicts edge
    with a non-contradiction on both ends. A mutation dropping the filter
    therefore survived every behavioural test.

    Rather than assert the guard is exercised (the mistake this file has
    already made twice, on the auto-resolver's frontier check), this test
    MANUFACTURES the malformed state the guard exists for — a contradicts
    edge between two observations — and pins that the helper still returns
    nothing. Direct SQL is the point here: the state is unreachable through
    the write path, which is exactly why the guard needs a test that does
    not go through it.
    """
    rev, obs, _derive, core = _make_fresh_mods()

    a = _seed_ob(obs, "Side A.")
    b = _seed_ob(obs, "Side B.")

    conn = core._get_db()
    try:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, 'contradicts', ?)",
            (a, b, core._now()),
        )
        conn.commit()
        assert rev._affected_contradiction_ids(conn, a) == [], (
            "a contradicts edge with no contradiction on either end must "
            "yield nothing — never the other claim-bearing node")
        assert rev._affected_contradiction_ids(conn, b) == []
    finally:
        conn.close()


def test_convergence_warning_DOES_fire_when_the_recompute_cannot_settle():
    """The positive direction, which the round-1 blocker fix left untested:
    a mutation flipping `if not converged:` to `if False:` survived every
    cascade test, because all of them assert the warning is ABSENT.

    A real non-convergence needs a CASCADE_RELATIONS cycle, which the
    write paths refuse to create. So this drives the helper directly with a
    recompute that never settles — the same state a cycle would produce —
    and asserts the warning fires and says the result is partial.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Premise.")
    dv1 = _seed_dv(derive, "Dependent.", [ob])

    real = core._recompute_node_confidence
    calls = {"n": 0}

    def _never_settles(conn, node_id, *, write=True):
        calls["n"] += 1
        d = real(conn, node_id, write=False)
        return {**d, "disposition": "recomputed", "changed": True,
                "old": 0.5, "new": 0.4}

    core._recompute_node_confidence = _never_settles
    try:
        conn = core._get_db()
        try:
            res = rev._cascade_recompute_confidence_transitive(conn, ob)
        finally:
            conn.close()
    finally:
        core._recompute_node_confidence = real

    assert calls["n"] > 0, "the fixture must actually drive the helper"
    assert "convergence_warning" in res, res
    assert "PARTIAL" in res["convergence_warning"], res["convergence_warning"]
    assert res["passes"] == res["nodes_considered"] + 1, res


def test_acknowledgement_edge_via_marker_branch_is_exercised():
    """The `via="marker"` branch of _cascade_acknowledgement_edges_deferred —
    reached when the superseded premise carries a MARKER of its own, rather
    than being the cascade root (the `via="root"` branch, which
    test_acknowledgement_records_a_derives_from_edge_to_the_superseder
    already covers).

    Named by the round-1 reviewer as a surviving mutation: hardcoding
    `replacement = None` in that branch changed nothing any test could see,
    because every existing edge fixture went through `via="root"`.

    Shape: `R -> P -> N`. Retract R (marks P and N). Resolve P by
    SUPERSEDING it — P's own marker then carries resolution_kind
    'superseded' plus the replacement id. N's affected-premise read now
    reaches P through the marker, not through the root.
    """
    rev, obs, derive, core = _make_fresh_mods()

    r_ob = _seed_ob(obs, "Root.", quote_type="official_statement")
    p = _seed_dv(derive, "Premise node.", [r_ob])
    n = _seed_dv(derive, "Depends on the premise node.", [p])

    json.loads(rev._retract_impl(
        node_id=r_ob, error_type="hallucinated_claim", reason="w"))

    clean = _seed_ob(obs, "Clean basis.", quote_type="official_statement")
    p2 = _seed_dv(derive, "Premise node, restated.", [clean])
    rp = json.loads(rev._cascade_resolve_impl(
        node_id=p, resolution_kind="superseded", replacement_id=p2))
    assert "error" not in rp, rp

    affected = rev._cascade_affected_premises(core._get_db(), n)
    assert any(a["via"] == "marker" and a["premise_id"] == p for a in affected), affected

    rn = json.loads(rev._cascade_resolve_impl(
        node_id=n, resolution_kind="acknowledged"))
    assert "error" not in rn, rn
    assert rn["acknowledgement_edges_deferred"] == [
        {"premise_id": p, "replacement_id": p2}
    ], rn["acknowledgement_edges_deferred"]
    assert p2 not in _premises(core, n), (
        "the edge is DEFERRED, not written — see "
        "test_acknowledgement_writes_no_backwards_in_time_edge")


def test_contradiction_marking_writes_the_same_audit_trail_as_the_ordinary_path():
    """Round-3 reviewer: deleting or INVERTING the resolution path's
    `_log_edit` left all 94 cascade tests passing, because nothing in the
    suite read `edit_history` at all. Measured divergence at the time: the
    resolution path left the ct at ['created'] where an ordinary supersede
    leaves ['created', 'stale_flagged'].

    The action name is not cosmetic — it is how the audit trail records
    whether the contradiction went stale (a side was superseded) or was
    tainted (a side was retracted), which is the same distinction
    `root_op` carries on the marker.
    """
    rev, obs, derive, core = _make_fresh_mods()
    import engram_derive as _ed

    def _edit_actions(ct_id):
        conn = core._get_db()
        try:
            return [r["action"] for r in conn.execute(
                "SELECT action FROM edit_history WHERE node_id = ? "
                "ORDER BY rowid", (ct_id,)).fetchall()]
        finally:
            conn.close()

    ob = _seed_ob(obs, "Root premise.")
    d1 = _seed_dv(derive, "First dependent.", [ob])
    d2 = _seed_dv(derive, "Second dependent.", [ob])
    c = json.loads(_ed._contradict_impl(
        node_id_a=d1, node_id_b=d2, description="conflict"))
    ct = c.get("contradiction_id") or c.get("node_id")

    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="w"))

    clean = _seed_ob(obs, "Clean basis.")
    d1b = _seed_dv(derive, "First dependent, restated.", [clean])
    json.loads(rev._cascade_resolve_impl(
        node_id=d1, resolution_kind="superseded", replacement_id=d1b))
    assert "stale_flagged" in _edit_actions(ct), (
        f"a supersede resolution must record stale_flagged: {_edit_actions(ct)}")

    json.loads(rev._cascade_resolve_impl(
        node_id=d2, resolution_kind="retracted", error_type="other"))
    acts = _edit_actions(ct)
    assert "tainted" in acts, (
        f"a retract resolution must record tainted, not stale_flagged: {acts}")


def test_no_phantom_contradiction_event_when_the_node_has_none():
    """Round-3 reviewer: `if _cts:` -> `if True:` survived — it mints a
    cascade_event id that is reported in the response while marking
    nothing, so a caller inspecting contradiction_cascade_event sees an
    event that does not exist anywhere in cascade_markers.

    The modal resolution has no contradictions, so this pins the common
    case rather than an exotic one.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Premise.")
    dv1 = _seed_dv(derive, "Dependent, no contradictions.", [ob])
    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="w"))
    events_before = _events(core)

    r = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="retracted", error_type="other"))
    assert "error" not in r, r
    assert r["marked_contradictions"] == [], r
    assert r["contradiction_cascade_event"] is None, (
        "no contradictions means no event id may be reported")
    assert _events(core) == events_before, (
        "and no event id may exist in the marker table either")


def test_a_non_current_contradiction_is_not_marked():
    """The `is_current = 1` filter is live and MCP-reachable — round 3's
    self-match fix left it as that guard's only remaining job, and the
    reviewer found it unpinned. A contradiction that has itself been
    retracted has no claim left to re-review, so marking it would put a
    dead node on the frontier.
    """
    rev, obs, derive, core = _make_fresh_mods()
    import engram_derive as _ed

    ob = _seed_ob(obs, "Root premise.")
    d1 = _seed_dv(derive, "Dependent.", [ob])
    other = _seed_ob(obs, "Other side.")
    c = json.loads(_ed._contradict_impl(
        node_id_a=d1, node_id_b=other, description="conflict"))
    ct = c.get("contradiction_id") or c.get("node_id")

    json.loads(rev._retract_impl(
        node_id=ct, error_type="other", reason="the contradiction was mis-filed"))
    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="w"))

    clean = _seed_ob(obs, "Clean basis.")
    d1b = _seed_dv(derive, "Dependent, restated.", [clean])
    r = json.loads(rev._cascade_resolve_impl(
        node_id=d1, resolution_kind="superseded", replacement_id=d1b))
    assert "error" not in r, r
    assert r["marked_contradictions"] == [], (
        f"a retracted contradiction must not be marked: {r}")
    assert r["contradiction_cascade_event"] is None, r
