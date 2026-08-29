"""Tests for engram_cascade_frontier (engram-alpha#2013 slice 3 — derived
reads, coder-fairy spec).

Coverage:
  1. Basic frontier: a two-hop chain (ob -> dv1 -> dv2), retract ob. dv1
     (direct target) is frontier; dv2 (blocked by dv1's pending marker) is
     not, until dv1's own marker resolves.
  2. Determinism: two calls against identical marker state return
     byte-identical frontier ordering.
  3. Multi-event node appears exactly once, AND is blocked until ALL its
     pending ancestors — across ALL of its own events — have resolved (not
     per-event). The two rules the spec calls out as most likely to be got
     wrong.
  4. Mode C's result is a subset of Mode B's for the same graph state.
  5. Mode C's blast-radius estimate + pending_ancestor_count fields.
  6. Read-only: no cascade_markers row is ever mutated by a frontier call.
  7. Payload-level guards: unknown node_id, negative/non-int limit.

Import-pattern contract (#948 wave B — R1): imports engram_core,
engram_revision, engram_observation, engram_derive directly; contains no
``import server``. Duplicates the `_seed_ob`/`_seed_dv`/`_make_fresh_mods`
helper shapes from test_cascade_derived_sets.py rather than cross-importing
(no precedent in this repo for importing across test_*.py files — see that
file's own docstring).
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


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_cascade_frontier_")
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


def _seed_ob(obs, claim="Test claim.", url="https://example.com/src"):
    r = json.loads(obs._add_observation_impl(
        url=url,
        title="Source",
        quoted_text=claim,
        interpretation=f"Interpretation: {claim}",
        claim=claim,
        quote_type="attributed_analysis",
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


def _frontier_ids(resp: dict) -> set:
    return {s["id"] for s in resp["frontier"]}


def _pending_marker_count(core) -> int:
    conn = core._get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM cascade_markers WHERE status = 'pending'"
        ).fetchone()["c"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1 — basic frontier: direct target ready, transitive dependent blocked
# ---------------------------------------------------------------------------


def test_direct_target_frontier_transitive_dependent_blocked():
    rev, obs, derive, core = _make_fresh_mods()

    ob_id = _seed_ob(obs, "Chain premise.")
    dv1_id = _seed_dv(derive, "Direct dependent.", [ob_id])
    dv2_id = _seed_dv(derive, "Transitive dependent.", [dv1_id])

    ret = json.loads(rev._retract_impl(
        node_id=ob_id, error_type="hallucinated_claim", reason="wrong",
    ))
    assert "error" not in ret, ret

    resp = json.loads(rev._cascade_frontier_impl())
    assert resp["mode"] == "B"
    ids = _frontier_ids(resp)
    assert dv1_id in ids, f"direct target must be frontier: {resp['frontier']}"
    assert dv2_id not in ids, (
        f"transitive dependent must be blocked by dv1's own pending marker: "
        f"{resp['frontier']}"
    )

    # Resolve dv1 (retract it too) — its own pending marker clears, and dv2
    # should now be frontier.
    ret2 = json.loads(rev._retract_impl(
        node_id=dv1_id, error_type="hallucinated_claim", reason="also wrong",
    ))
    assert "error" not in ret2, ret2

    resp2 = json.loads(rev._cascade_frontier_impl())
    ids2 = _frontier_ids(resp2)
    assert dv2_id in ids2, (
        f"dv2 must become frontier once dv1's marker resolves: {resp2['frontier']}"
    )


# ---------------------------------------------------------------------------
# 2 — determinism
# ---------------------------------------------------------------------------


def test_determinism_across_two_calls():
    rev, obs, derive, core = _make_fresh_mods()

    ob1 = _seed_ob(obs, "Premise A.")
    ob2 = _seed_ob(obs, "Premise B.")
    dv1 = _seed_dv(derive, "Dependent on A.", [ob1])
    dv2 = _seed_dv(derive, "Dependent on B.", [ob2])

    json.loads(rev._retract_impl(node_id=ob1, error_type="hallucinated_claim", reason="x"))
    json.loads(rev._retract_impl(node_id=ob2, error_type="hallucinated_claim", reason="y"))

    resp_a = rev._cascade_frontier_impl()
    resp_b = rev._cascade_frontier_impl()
    assert resp_a == resp_b, (
        "identical marker state must yield byte-identical frontier output "
        "across two calls (no queue/cursor/session state)"
    )

    parsed = json.loads(resp_a)
    order = [s["id"] for s in parsed["frontier"]]
    # (Round-3 fix: dropped a vacuous `order == sorted(order) or set(order)
    # == {dv1, dv2}` assertion — with exactly {dv1, dv2} in `order`, the
    # right-hand disjunct is always true regardless of ordering, so the
    # whole line could never fail. The byte-identity check above is the
    # real determinism assertion; this presence check is the only other
    # thing worth pinning here.)
    assert dv1 in order and dv2 in order


# ---------------------------------------------------------------------------
# 3 — multi-event node: appears once; blocked until ALL pending ancestors,
#     across ALL its own events, resolve
# ---------------------------------------------------------------------------


def test_multi_event_node_once_and_blocked_until_all_ancestors_resolved():
    rev, obs, derive, core = _make_fresh_mods()

    ob1 = _seed_ob(obs, "Premise 1.")
    ob2 = _seed_ob(obs, "Premise 2.")
    mid1 = _seed_dv(derive, "Intermediate 1.", [ob1])
    mid2 = _seed_dv(derive, "Intermediate 2.", [ob2])
    joint = _seed_dv(derive, "Joint node with two premises.", [mid1, mid2])

    # Two independent retracts -> two independent cascade_events, both
    # reaching `joint` (via mid1 and mid2 respectively) as well as marking
    # mid1/mid2 themselves.
    r1 = json.loads(rev._retract_impl(node_id=ob1, error_type="hallucinated_claim", reason="a"))
    assert "error" not in r1, r1
    r2 = json.loads(rev._retract_impl(node_id=ob2, error_type="hallucinated_claim", reason="b"))
    assert "error" not in r2, r2

    conn = core._get_db()
    try:
        joint_markers = conn.execute(
            "SELECT cascade_event, status FROM cascade_markers WHERE node_id = ?",
            (joint,),
        ).fetchall()
    finally:
        conn.close()
    assert len(joint_markers) == 2, (
        f"joint node must carry TWO markers (one per event), got {joint_markers}"
    )

    resp = json.loads(rev._cascade_frontier_impl())
    ids = _frontier_ids(resp)
    assert mid1 in ids and mid2 in ids, "both intermediates must be frontier (roots aren't marked)"
    assert joint not in ids, (
        "joint must be BLOCKED — both mid1 and mid2 (its ancestors) still pending"
    )

    # Resolve only mid1. joint must STILL be blocked (mid2 still pending) —
    # this is the "all pending direct premises resolved ACROSS ALL EVENTS"
    # rule, not per-event.
    json.loads(rev._retract_impl(node_id=mid1, error_type="hallucinated_claim", reason="c"))
    resp2 = json.loads(rev._cascade_frontier_impl())
    ids2 = _frontier_ids(resp2)
    assert joint not in ids2, (
        f"joint must remain blocked with only ONE of two ancestors resolved: {resp2['frontier']}"
    )

    # Resolve mid2 too. joint must now be frontier, appearing EXACTLY ONCE.
    #
    # Event count on joint at this point is 4, not 2 — retracting mid1/mid2
    # (each already a marked node) each fire their OWN fresh downstream
    # cascade (a brand-new cascade_event whose root_node is mid1/mid2
    # respectively), which reaches joint again with a NEW pending marker,
    # layered on top of the two original (now-orphaned, still 'pending' on
    # joint itself, but harmless) markers from ob1/ob2's events. This is
    # real, correct system behavior (every retract/supersede call re-walks
    # its own downstream fresh, INSERT OR IGNORE only dedupes an exact
    # (node_id, cascade_event) pair, never across different events) — not a
    # frontier-tool defect. What matters for frontier-membership is joint's
    # ANCESTORS' pending status, not how many historical marker rows joint
    # itself has accumulated: none of joint's ancestors (ob1, mid1, ob2,
    # mid2) carry a pending marker anymore, so joint is unblocked regardless
    # of its own event count.
    json.loads(rev._retract_impl(node_id=mid2, error_type="hallucinated_claim", reason="d"))
    resp3 = json.loads(rev._cascade_frontier_impl())
    joint_entries = [s for s in resp3["frontier"] if s["id"] == joint]
    assert len(joint_entries) == 1, (
        f"joint must appear EXACTLY ONCE in the frontier, got {joint_entries}"
    )
    assert len(joint_entries[0]["events"]) == 4, (
        f"joint's single entry must carry ALL FOUR of its accumulated "
        f"events (2 orphaned + 2 fresh from resolving mid1/mid2), never "
        f"collapsed or dropped: {joint_entries[0]}"
    )
    event_roots = {e["root_node"] for e in joint_entries[0]["events"]}
    assert event_roots == {ob1, ob2, mid1, mid2}, (
        f"the four events' root_nodes must be exactly {{ob1, ob2, mid1, mid2}}, "
        f"got {event_roots}"
    )


# ---------------------------------------------------------------------------
# 4 — Mode C is a subset of Mode B
# ---------------------------------------------------------------------------


def test_mode_c_is_subset_of_mode_b():
    rev, obs, derive, core = _make_fresh_mods()

    ob1 = _seed_ob(obs, "Premise 1.")
    ob2 = _seed_ob(obs, "Premise 2.")
    mid1 = _seed_dv(derive, "Intermediate 1.", [ob1])
    mid2 = _seed_dv(derive, "Intermediate 2.", [ob2])
    joint = _seed_dv(derive, "Joint node.", [mid1, mid2])
    unrelated_ob = _seed_ob(obs, "Unrelated premise.")
    unrelated_dv = _seed_dv(derive, "Unrelated dependent.", [unrelated_ob])

    json.loads(rev._retract_impl(node_id=ob1, error_type="hallucinated_claim", reason="a"))
    json.loads(rev._retract_impl(node_id=ob2, error_type="hallucinated_claim", reason="b"))
    json.loads(rev._retract_impl(node_id=unrelated_ob, error_type="hallucinated_claim", reason="c"))

    mode_b = json.loads(rev._cascade_frontier_impl())
    mode_c = json.loads(rev._cascade_frontier_impl(node_id=joint))

    assert mode_c["mode"] == "C"
    ids_b = _frontier_ids(mode_b)
    ids_c = _frontier_ids(mode_c)
    assert ids_c, "mode C should be non-empty in this scenario"
    assert ids_c.issubset(ids_b), (
        f"mode C frontier {ids_c} must be a subset of mode B frontier {ids_b}"
    )
    # unrelated_dv is blocked (unrelated_ob has no marker itself so
    # unrelated_dv is actually frontier in mode B) but must NOT be reachable
    # from joint's ancestor chain, so mode C must never surface it.
    assert unrelated_dv not in ids_c


# ---------------------------------------------------------------------------
# 4b — transitive vs direct-premises-only divergence (round-3 fix, PR #2338).
#      The single most load-bearing decision in _cascade_blocked (transitive
#      cascade-ancestor closure, not one-hop direct premises) previously had
#      no test that could distinguish the two readings: every other fixture
#      resolves markers in dependency order, so both readings agree. This
#      fixture forces out-of-order resolution — R1 -> G -> P1 -> N, retract
#      R1 (markers land on G, P1, N), then supersede P1 with a replacement
#      that does NOT cite G. P1's own pending marker clears at supersede
#      time (_resolve_own_markers is unconditional — see _cascade_blocked's
#      docstring) while G's marker, untouched, stays pending. Under
#      direct-premises-only, N's one direct premise (P1) reads clean -> N
#      would wrongly appear on the frontier. Under transitive (what this
#      code implements), N stays blocked because G — still upstream of N via
#      the P1 edges supersede does not rewire — is still pending.
# ---------------------------------------------------------------------------


def test_transitive_catches_out_of_order_resolution_direct_premises_would_miss():
    rev, obs, derive, core = _make_fresh_mods()

    r1 = _seed_ob(obs, "Root premise (will be retracted).")
    g = _seed_dv(derive, "G depends on R1.", [r1])
    p1 = _seed_dv(derive, "P1 depends on G.", [g])
    n = _seed_dv(derive, "N depends on P1.", [p1])
    other = _seed_ob(obs, "Unrelated replacement premise — does not cite G.")
    new_p1 = _seed_dv(derive, "Replacement for P1, cites `other` not `g`.", [other])

    ret = json.loads(rev._retract_impl(
        node_id=r1, error_type="hallucinated_claim", reason="root wrong",
    ))
    assert "error" not in ret, ret

    # Sanity: retracting R1 marks G, P1, N (all pending) via the derivation
    # cascade; R1 itself (the root) never gets its own marker.
    conn = core._get_db()
    try:
        pending_after_retract = {
            row["node_id"]
            for row in conn.execute(
                "SELECT node_id FROM cascade_markers WHERE status = 'pending'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {g, p1, n}.issubset(pending_after_retract), pending_after_retract

    sup = json.loads(rev._supersede_impl(
        old_node_id=p1, new_node_id=new_p1,
        supersede_reason="round-3 fixture: replacement drops premise g",
    ))
    assert "error" not in sup, sup

    # P1's own pending marker(s) must be gone (supersede resolves them
    # unconditionally) while G's marker is untouched and still pending —
    # this is the out-of-order-resolution precondition the whole test rests
    # on. If either assumption fails, the reviewer's counter-example does
    # not reproduce as described and the rest of this test is moot.
    conn = core._get_db()
    try:
        p1_pending = conn.execute(
            "SELECT COUNT(*) AS c FROM cascade_markers "
            "WHERE node_id = ? AND status = 'pending'", (p1,),
        ).fetchone()["c"]
        g_pending = conn.execute(
            "SELECT COUNT(*) AS c FROM cascade_markers "
            "WHERE node_id = ? AND status = 'pending'", (g,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert p1_pending == 0, (
        f"precondition failed: P1's own marker(s) did not clear on supersede "
        f"({p1_pending} still pending) — reviewer's counter-example does not "
        f"reproduce as described."
    )
    assert g_pending >= 1, (
        f"precondition failed: G's marker cleared unexpectedly "
        f"({g_pending} pending) — reviewer's counter-example does not "
        f"reproduce as described."
    )

    resp = json.loads(rev._cascade_frontier_impl())
    ids = _frontier_ids(resp)

    # N must stay BLOCKED: transitive ancestry still finds G pending, even
    # though N's one *direct* premise (P1) is clean.
    assert n not in ids, (
        f"N must remain blocked — G (a transitive, non-direct ancestor via "
        f"P1's still-live edges) is still pending: {resp['frontier']}"
    )
    # G itself must be frontier — its own ancestors (just R1, which never
    # carries a marker) are clean, and it carries its own pending marker.
    assert g in ids, (
        f"G must be frontier — its own ancestor set has no pending marker: "
        f"{resp['frontier']}"
    )


# ---------------------------------------------------------------------------
# 5 — Mode C blast-radius estimate + pending_ancestor_count
# ---------------------------------------------------------------------------


def test_mode_c_blast_radius_fields():
    rev, obs, derive, core = _make_fresh_mods()

    ob1 = _seed_ob(obs, "Premise 1.")
    mid1 = _seed_dv(derive, "Intermediate 1.", [ob1])
    target = _seed_dv(derive, "Target.", [mid1])

    json.loads(rev._retract_impl(node_id=ob1, error_type="hallucinated_claim", reason="a"))

    mode_c = json.loads(rev._cascade_frontier_impl(node_id=target))
    assert mode_c["mode"] == "C"
    assert "blast_radius_estimate" in mode_c
    assert "pending_ancestor_count" in mode_c
    assert "blast_radius_estimate_note" in mode_c
    # mid1 is target's only pending ancestor (ob1 is a root, never marked).
    assert mode_c["pending_ancestor_count"] == 1
    # Exact-value pin (round-3 fix): the `>=` check alone is true by
    # construction (blast_radius_estimate is defined as >= the ancestor
    # count) and cannot catch an over- or under-count. This fixture's
    # single cascade_event (from retracting ob1) marks exactly two nodes
    # (mid1, target) — ob1 itself, the retracted root, never gets its own
    # marker — so blast_radius_estimate must be exactly 2.
    assert mode_c["blast_radius_estimate"] == 2, (
        f"expected exactly 2 (mid1, target) distinct nodes ever marked by "
        f"this fixture's single cascade_event, got {mode_c['blast_radius_estimate']}"
    )
    assert mode_c["blast_radius_estimate"] >= mode_c["pending_ancestor_count"]

    # Mode B response must NOT carry the mode-C-only fields.
    mode_b = json.loads(rev._cascade_frontier_impl())
    assert "blast_radius_estimate" not in mode_b
    assert "pending_ancestor_count" not in mode_b


# ---------------------------------------------------------------------------
# 6 — read-only: a frontier call never mutates cascade_markers
# ---------------------------------------------------------------------------


def test_frontier_call_is_read_only():
    rev, obs, derive, core = _make_fresh_mods()

    ob1 = _seed_ob(obs, "Premise 1.")
    dv1 = _seed_dv(derive, "Dependent.", [ob1])
    json.loads(rev._retract_impl(node_id=ob1, error_type="hallucinated_claim", reason="a"))

    before = _pending_marker_count(core)
    json.loads(rev._cascade_frontier_impl())
    json.loads(rev._cascade_frontier_impl(node_id=dv1))
    json.loads(rev._cascade_frontier_impl(limit=0))
    after = _pending_marker_count(core)
    assert before == after, "frontier calls must never mutate cascade_markers"

    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT status, resolution_kind, resolved_at FROM cascade_markers "
            "WHERE node_id = ?", (dv1,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending"
    assert row["resolution_kind"] is None
    assert row["resolved_at"] is None


# ---------------------------------------------------------------------------
# 7 — payload-level guards
# ---------------------------------------------------------------------------


def test_unknown_node_id_returns_error():
    rev, obs, derive, core = _make_fresh_mods()
    resp = json.loads(rev._cascade_frontier_impl(node_id="dv_does_not_exist"))
    assert "error" in resp


def test_negative_limit_returns_error():
    rev, obs, derive, core = _make_fresh_mods()
    resp = json.loads(rev._cascade_frontier_impl(limit=-1))
    assert "error" in resp


def test_non_int_limit_returns_error():
    rev, obs, derive, core = _make_fresh_mods()
    resp = json.loads(rev._cascade_frontier_impl(limit="five"))
    assert "error" in resp


def test_limit_caps_packets_not_summaries():
    rev, obs, derive, core = _make_fresh_mods()

    ob1 = _seed_ob(obs, "Premise 1.")
    ob2 = _seed_ob(obs, "Premise 2.")
    ob3 = _seed_ob(obs, "Premise 3.")
    dv1 = _seed_dv(derive, "D1.", [ob1])
    dv2 = _seed_dv(derive, "D2.", [ob2])
    dv3 = _seed_dv(derive, "D3.", [ob3])
    for ob in (ob1, ob2, ob3):
        json.loads(rev._retract_impl(node_id=ob, error_type="hallucinated_claim", reason="x"))

    resp = json.loads(rev._cascade_frontier_impl(limit=1))
    assert resp["frontier_count"] == 3
    assert len(resp["frontier"]) == 3, "summaries must NEVER be capped by limit"
    assert resp["packets_returned"] == 1
    assert len(resp["packets"]) == 1
    assert resp["packets_limit"] == 1


# ---------------------------------------------------------------------------
# 8 — Invariant-1 anomaly: pending markers exist but the frontier is empty
#     (round-2 fix). A cascade CYCLE in CASCADE_RELATIONS is the fixture:
#     the normal write path (derive/retract) cannot itself produce a cycle
#     (each derive call only adds forward derives_from edges toward
#     already-existing supporting_ids, and the DAG-integrity scan, while
#     advisory/non-blocking rather than a hard guard, isn't what's stopping
#     this — there is simply no write-path call that lets a NEW node name an
#     EXISTING node as its own dependent). So the cyclic edge pair is
#     inserted directly into `edges`, bypassing the normal write path
#     entirely — exactly the escape hatch the round-2 spec names. This
#     mirrors what an advisory (non-blocking) DAG scan failing to prevent a
#     cycle would look like in a live graph.
# ---------------------------------------------------------------------------


def _insert_cyclic_cascade_edge(core, source_id: str, target_id: str):
    """Direct INSERT of one derives_from edge, bypassing the normal
    derive()/retract() write path — see the module docstring above test 8
    for why this is the only way to get a cascade CYCLE into the graph.
    """
    conn = core._get_db()
    try:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, 'derives_from', ?)",
            (source_id, target_id, core._now()),
        )
        conn.commit()
    finally:
        conn.close()


def test_invariant_1_anomaly_on_cascade_cycle():
    rev, obs, derive, core = _make_fresh_mods()

    ob0 = _seed_ob(obs, "Common premise.")
    dv1 = _seed_dv(derive, "Dependent 1.", [ob0])
    dv2 = _seed_dv(derive, "Dependent 2.", [ob0])

    ret = json.loads(rev._retract_impl(
        node_id=ob0, error_type="hallucinated_claim", reason="wrong",
    ))
    assert "error" not in ret, ret

    # Sanity: before the cycle is introduced, both dv1 and dv2 are pending
    # AND frontier (roots aren't marked, so their only ancestor — ob0 — has
    # no marker of its own).
    pre = json.loads(rev._cascade_frontier_impl())
    pre_ids = _frontier_ids(pre)
    assert dv1 in pre_ids and dv2 in pre_ids, (
        f"pre-cycle sanity failed: expected dv1+dv2 frontier, got {pre}"
    )
    assert "invariant_1_violation_suspected" not in pre

    # Introduce the cycle: dv1 <-> dv2, each naming the other as a
    # cascade-ancestor. Both already carry a pending marker (from ob0's
    # retract above), so each now blocks the other — no node in the {dv1,
    # dv2} set has a marker-free ancestor set, so the frontier over this
    # set is empty while pending markers on both remain.
    _insert_cyclic_cascade_edge(core, dv1, dv2)
    _insert_cyclic_cascade_edge(core, dv2, dv1)

    resp = json.loads(rev._cascade_frontier_impl())
    assert resp["mode"] == "B"
    assert resp["frontier_count"] == 0, (
        f"cycle must mutually block both dv1 and dv2: {resp}"
    )
    assert resp["total_pending_nodes"] == 2, resp
    assert "invariant_1_violation_suspected" in resp, (
        "empty frontier with pending markers must be flagged, not silent"
    )
    assert resp["invariant_1_violation_suspected"], (
        "the anomaly field must be truthy, not an empty/falsy placeholder"
    )

    # Mode C against either cycle member reproduces the same anomaly: its
    # own self-inclusive pending-ancestor set is {dv1, dv2}, and neither is
    # unblocked, so this target's own restricted frontier is also empty.
    resp_c = json.loads(rev._cascade_frontier_impl(node_id=dv1))
    assert resp_c["mode"] == "C"
    assert resp_c["frontier_count"] == 0, resp_c
    assert "invariant_1_violation_suspected" in resp_c
    assert resp_c["invariant_1_violation_suspected"]

    # Read-only: the cycle + repeated frontier calls must never mutate
    # cascade_markers.
    before = _pending_marker_count(core)
    json.loads(rev._cascade_frontier_impl())
    json.loads(rev._cascade_frontier_impl(node_id=dv2))
    after = _pending_marker_count(core)
    assert before == after


def test_invariant_1_anomaly_absent_on_healthy_graph():
    """Control for test 8 above — it must be possible for this field to be
    ABSENT, not just present. A frontier response from an ordinary
    (acyclic) graph with a genuinely non-empty frontier must never carry
    invariant_1_violation_suspected, in EITHER mode.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob0 = _seed_ob(obs, "Common premise.")
    dv1 = _seed_dv(derive, "Dependent 1.", [ob0])
    dv2 = _seed_dv(derive, "Dependent 2.", [ob0])

    ret = json.loads(rev._retract_impl(
        node_id=ob0, error_type="hallucinated_claim", reason="wrong",
    ))
    assert "error" not in ret, ret

    resp = json.loads(rev._cascade_frontier_impl())
    assert resp["mode"] == "B"
    assert resp["frontier_count"] > 0, (
        f"control fixture must have a genuinely non-empty frontier: {resp}"
    )
    assert "invariant_1_violation_suspected" not in resp

    resp_c = json.loads(rev._cascade_frontier_impl(node_id=dv1))
    assert resp_c["mode"] == "C"
    assert resp_c["frontier_count"] > 0, resp_c
    assert "invariant_1_violation_suspected" not in resp_c


# ---------------------------------------------------------------------------
# 9 — dead_premises / immediate_cause / root_is_direct_premise coverage
# ---------------------------------------------------------------------------


def test_dead_premise_detail_is_populated_and_root_case_covered():
    """Pins the three fields that carry "which premises died and how"
    (§10.1) — immediate_cause, dead_premises, root_is_direct_premise. None
    of these were previously asserted anywhere in this file.

    Fixture is two hops deep on purpose: a cascade root never carries its
    own marker, so a direct-child-of-root's dead_premises is *structurally*
    always empty — an instrument that can't see what it's measuring. Using
    N (two hops from r1, one hop from g) gives a genuine non-empty
    dead_premises case (the retract event, rooted at r1) alongside the
    root-carries-no-marker case (the supersede event, rooted at g, where
    root_is_direct_premise is True) in the same fixture.
    """
    rev, obs, derive, core = _make_fresh_mods()

    r1 = _seed_ob(obs, "Root premise (will be retracted).")
    g = _seed_dv(derive, "G depends on R1.", [r1])
    n = _seed_dv(derive, "N depends on G.", [g])
    o1 = _seed_ob(obs, "Unrelated replacement premise — does not cite G.")

    ret = json.loads(rev._retract_impl(
        node_id=r1, error_type="hallucinated_claim", reason="root wrong",
    ))
    assert "error" not in ret, ret

    new_g = _seed_dv(derive, "Replacement for G, does not cite r1.", [o1])
    sup = json.loads(rev._supersede_impl(
        old_node_id=g, new_node_id=new_g,
        supersede_reason="dead-premise-detail fixture: replacement drops r1",
    ))
    assert "error" not in sup, sup

    resp = json.loads(rev._cascade_frontier_impl())
    ids = _frontier_ids(resp)
    assert n in ids, f"N must be frontier: {resp['frontier']}"

    n_summary = next(s for s in resp["frontier"] if s["id"] == n)
    events = n_summary["events"]

    retract_events = [e for e in events if e["root_op"] == "retract"]
    supersede_events = [e for e in events if e["root_op"] == "supersede"]
    assert len(retract_events) == 1, events
    assert len(supersede_events) == 1, events

    retract_event = next(e for e in retract_events if e["root_node"] == r1)
    supersede_event = next(e for e in supersede_events if e["root_node"] == g)

    # -- retract event (root=r1): g is N's dead direct premise --------------
    assert retract_event["immediate_cause"] == [g], retract_event
    dead = retract_event["dead_premises"]
    assert len(dead) == 1, dead
    entry = dead[0]
    assert entry["premise_id"] == g, entry
    assert entry["resolution_kind"] == "superseded", entry
    assert entry["resolution_replacement_id"] == new_g, entry
    assert entry.get("resolved_at"), entry
    # r1 (the retract event's root) is not N's *direct* premise — g is.
    assert retract_event["root_is_direct_premise"] is False, retract_event

    # -- supersede event (root=g): g IS N's direct premise, so it carries --
    # -- no marker of its own — the empty fields here are expected, not ----
    # -- a bug (a cascade root never gets its own marker). ------------------
    assert supersede_event["root_is_direct_premise"] is True, supersede_event
    assert supersede_event["immediate_cause"] == [], supersede_event
    assert supersede_event["dead_premises"] == [], supersede_event


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
