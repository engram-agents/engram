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
        quoted_text=claim + " in the cited source",
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


def _cascade_cycle(core):
    """PROPERTY check (#2013 §12.6 / §12.7), not a mechanism check: reads the
    WHOLE edges table filtered to the DERIVED cascade relation set —
    `core.CASCADE_RELATIONS`, never a relation-name literal — builds the
    adjacency, and DFS's for a back edge (grey-node revisit). Returns the
    cycle as a list of node ids (first repeated at both ends) if one exists,
    else None. Deliberately relation-set-agnostic: it must catch a cycle
    over ANY combination of cascade relations, of ANY length, not just the
    two-relation three-node shape the guard was written against — a check
    that only recognises the shape of the known defect is the mechanism
    test this property test replaces.
    """
    conn = core._get_db()
    try:
        relations = sorted(core.CASCADE_RELATIONS)
        placeholders = ",".join("?" * len(relations))
        rows = conn.execute(
            f"SELECT source_id, target_id FROM edges "
            f"WHERE relation IN ({placeholders})",
            relations,
        ).fetchall()
    finally:
        conn.close()

    adjacency = {}
    for r in rows:
        adjacency.setdefault(r["source_id"], []).append(r["target_id"])

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}
    path = []

    def visit(node):
        color[node] = GRAY
        path.append(node)
        for nxt in adjacency.get(node, ()):
            state = color.get(nxt, WHITE)
            if state == WHITE:
                found = visit(nxt)
                if found is not None:
                    return found
            elif state == GRAY:
                idx = path.index(nxt)
                return path[idx:] + [nxt]
        color[node] = BLACK
        path.pop()
        return None

    for start in list(adjacency.keys()):
        if color.get(start, WHITE) == WHITE:
            found = visit(start)
            if found is not None:
                return found
    return None


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
        node_id=ob, error_type="hallucinated_claim", reason="never held", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
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


def test_acknowledgement_records_the_edge_on_the_new_relation():
    """§4's requirement, finally satisfied — as `acknowledged_derives_from`,
    never as `derives_from` (#2013 §12.5).

    The reason for a distinct relation is a collision, not a difficulty: the
    acknowledging node exists BEFORE its premise's replacement — that is the
    situation an acknowledgement arises in — so the edge is always backwards
    in time, and `derives_from` carries `temporal_causal: True` (#2357's
    rename of dag_check — creation-order, not the new `dag` edge-walk flag).
    Measured on this exact fixture before the relation existed:
    source.created_at < target.created_at, every time.

    The fixture ordering is load-bearing: the replacement is created AFTER
    the dependent, which is the only ordering that can occur in life and the
    one that makes the edge invert creation order. A fixture that creates the
    replacement first cannot see the property this relation exists for — and
    an earlier version of my own probe did exactly that and reported the
    defect absent.

    The OLD premise edge is retained: provenance is extended, never rewritten.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Original premise.", quote_type="official_statement")
    dv1 = _seed_dv(derive, "Depends on the premise.", [ob])
    ob2 = _seed_ob(obs, "Refined premise.", quote_type="editorial")

    assert _premises(core, dv1) == [ob]

    json.loads(rev._supersede_impl(
        old_node_id=ob, new_node_id=ob2, supersede_reason="refined"))
    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged"))
    assert "error" not in resp, resp
    assert resp["acknowledgement_edges"] == [
        {"premise_id": ob, "replacement_id": ob2}
    ], resp["acknowledgement_edges"]

    conn = core._get_db()
    try:
        rels = sorted(
            (e["relation"], e["target_id"]) for e in conn.execute(
                "SELECT relation, target_id FROM edges WHERE source_id = ?",
                (dv1,)).fetchall()
            if e["relation"].endswith("derives_from")
        )
    finally:
        conn.close()
    assert rels == [("acknowledged_derives_from", ob2), ("derives_from", ob)], rels


def test_acknowledgement_survives_a_second_generation():
    """#2349 blocker 2 — an acknowledgement must survive MORE THAN ONE
    supersede of the node it now rests on, not just the first.

    Measured symptom before the fix: an acknowledgement survived EXACTLY
    ONE supersede. Generation 2 adjudicated with an EMPTY affected-premise
    list (the acknowledged premise was invisible to
    `core._direct_premise_ids`, which read a hand-enumerated
    `('derives_from', 'supported_by')` literal that `acknowledged_derives_from`
    was never added to) — so no new acknowledgement edge got written, even
    though `_resolve_own_markers` still stamped the marker 'acknowledged'
    regardless. Generation 3 was then not marked at all: the node had no
    live cascade-bearing edge to generation 2's replacement for a THIRD
    supersede's downstream walk to find.

    The fix ranges `_direct_premise_ids` over `core.CITATION_SUPPORT_RELATIONS`
    (now including `acknowledged_derives_from`) instead of the old literal —
    see that constant's own comment in engram_core.py for why the relation
    belongs there. NOT the same fix as #2013 §12.6 (ranging a walk over
    CASCADE_RELATIONS): CASCADE_RELATIONS already included
    `acknowledged_derives_from` before this PR (#2013 §12.5), which is why
    generation 2's marker gets written correctly in the first place — the
    bug was downstream of that, in the read that decides what to do with
    the marker once it exists.

    Public verbs only (#2356): three generations of supersede on a single
    premise chain, with an acknowledgement between each pair.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob1 = _seed_ob(obs, "Generation 1 premise.", quote_type="official_statement")
    dv = _seed_dv(derive, "Depends on the premise across generations.", [ob1])

    # --- Generation 1: supersede ob1 -> ob2, dv acknowledges. ---
    s1 = json.loads(rev._supersede_impl(
        old_node_id=ob1, new_node_id=(ob2 := _seed_ob(
            obs, "Generation 2 premise.", quote_type="official_statement")),
        supersede_reason="gen1->gen2"))
    assert "error" not in s1, s1
    assert len(_markers(core, dv, "pending")) == 1, (
        "dv must be marked once ob1 is superseded")

    ack1 = json.loads(rev._cascade_resolve_impl(
        node_id=dv, resolution_kind="acknowledged"))
    assert "error" not in ack1, f"generation 1 acknowledgement: {ack1}"
    assert ack1["acknowledgement_edges"] == [
        {"premise_id": ob1, "replacement_id": ob2}
    ], ack1["acknowledgement_edges"]

    # --- Generation 2: supersede ob2 -> ob3. The marking walk already
    # ranges over CASCADE_RELATIONS (includes acknowledged_derives_from
    # since #2013 §12.5, landed before this PR) — dv gets marked here
    # regardless of blocker 2. The bug is in what dv's OWN adjudication
    # sees, checked next. ---
    s2 = json.loads(rev._supersede_impl(
        old_node_id=ob2, new_node_id=(ob3 := _seed_ob(
            obs, "Generation 3 premise.", quote_type="official_statement")),
        supersede_reason="gen2->gen3"))
    assert "error" not in s2, s2
    assert len(_markers(core, dv, "pending")) == 1, (
        "dv must be marked again once ob2 is superseded — this part of "
        "the marking walk was never the bug (CASCADE_RELATIONS already "
        "carried acknowledged_derives_from before this PR)")

    # THE ASSERTION Part B calls for: generation 2's affected-premise read
    # must be NON-EMPTY — before the fix this was [], because ob2 (reached
    # from dv only via acknowledged_derives_from) was invisible to
    # _direct_premise_ids.
    affected_gen2 = rev._cascade_affected_premises(core._get_db(), dv)
    assert affected_gen2 != [], (
        "generation 2's affected-premise list must be non-empty — an "
        "empty list here is exactly the measured pre-fix symptom")
    assert any(a["premise_id"] == ob2 for a in affected_gen2), affected_gen2

    ack2 = json.loads(rev._cascade_resolve_impl(
        node_id=dv, resolution_kind="acknowledged"))
    assert "error" not in ack2, f"generation 2 acknowledgement: {ack2}"
    assert ack2["acknowledgement_edges"] == [
        {"premise_id": ob2, "replacement_id": ob3}
    ], ack2["acknowledgement_edges"]

    # --- Generation 3: supersede ob3 -> ob4. dv must STILL be marked —
    # before the fix, dv had no live edge to ob3 at all (generation 2's
    # acknowledgement never actually wrote one), so this walk would have
    # found nothing downstream of ob3 to mark. ---
    ob4 = _seed_ob(obs, "Generation 4 premise.", quote_type="official_statement")
    s3 = json.loads(rev._supersede_impl(
        old_node_id=ob3, new_node_id=ob4, supersede_reason="gen3->gen4"))
    assert "error" not in s3, s3
    assert len(_markers(core, dv, "pending")) == 1, (
        "generation 3 must still be marked — this is the assertion that "
        "actually distinguishes the fix from the bug: before it, dv had "
        "no acknowledged_derives_from edge to ob3 at all, so this supersede's "
        "downstream walk would find nothing to mark")

    ack3 = json.loads(rev._cascade_resolve_impl(
        node_id=dv, resolution_kind="acknowledged"))
    assert "error" not in ack3, f"generation 3 acknowledgement: {ack3}"
    assert ack3["acknowledgement_edges"] == [
        {"premise_id": ob3, "replacement_id": ob4}
    ], ack3["acknowledgement_edges"]

    assert _cascade_cycle(core) is None, "the generational chain must stay acyclic"


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


def test_supersede_guard_refuses_a_downstream_replacement():
    """§12.3, and the reason the acknowledgement edge is safe at all.

    Construction, public verbs only: base -> P, X derives from P, P-prime
    derives from X. Superseding P with P-prime is now REFUSED — P-prime is
    downstream of the node it would replace, so installing it would make it
    stale by the very act, and it is the one shape that lets an
    acknowledgement close a cycle (§12.4).

    Before the guard this supersede was ACCEPTED and the cycle followed:
    the confidence recompute decayed a stored value on every identical run
    (0.641, 0.567, 0.503, 0.446) and both members sat off the frontier
    permanently.
    """
    rev, obs, derive, core = _make_fresh_mods()

    base = _seed_ob(obs, "Base observation.", quote_type="official_statement")
    p = _seed_dv(derive, "P, the premise that will be superseded.", [base])
    x = _seed_dv(derive, "X, depends on P.", [p])
    p2 = _seed_dv(derive, "P-prime, which happens to cite X.", [x])

    r = json.loads(rev._supersede_impl(
        old_node_id=p, new_node_id=p2, supersede_reason="r"))
    assert "error" in r, f"the guard must refuse this: {r}"
    assert r["guard"] == "supersede_replacement_is_downstream", r
    assert r["old_node_id"] == p and r["new_node_id"] == p2, r

    # ...and an ORDINARY supersede, whose replacement is independent, is
    # untouched. Without this the guard could be refusing everything.
    clean = _seed_ob(obs, "Independent replacement basis.",
                     quote_type="official_statement")
    p_ok = _seed_dv(derive, "A legitimate replacement.", [clean])
    ok = json.loads(rev._supersede_impl(
        old_node_id=x, new_node_id=p_ok, supersede_reason="ordinary"))
    assert "error" not in ok, ok


def test_acknowledgement_cannot_close_a_cascade_cycle():
    """§12.7's write-time guard, restored as a PROPERTY test rather than the
    narrow mechanism test it replaces.

    The replaced version asserted the ONE cycle shape we had thought of —
    a single relation, a single node pair, checked directly. Its intent was
    right and its reach was not; this one derives the relation set from
    CASCADE_RELATIONS and walks the whole edge table, so a cycle of any
    length over any cascade relation fails it. Do not narrow it back.

    (An earlier revision of this docstring pointed at `git show 3af90ab:...`
    for the old body. That commit was never pushed and two rebases have made
    it unreachable from origin — a pointer into a reader's `fatal: Not a
    valid object name`. The intent is stated above instead, which is what
    the pointer was carrying anyway.)

    §12.3's supersede-time guardrail refuses a replacement that is already
    downstream of what it replaces — but it is a POINT-IN-TIME check,
    evaluated once, at supersede time, over the edge table as it stood THEN.
    Every acknowledgement WRITTEN AFTER adds an edge to the very relation
    (`acknowledged_derives_from` JOINS CASCADE_RELATIONS) the check was
    evaluated over — so two individually-legal supersedes plus two
    acknowledgements can still close a cycle across THREE nodes and TWO
    relations, a shape the old test's single-relation, single-pair
    assertion could never have caught (that is the point of this being a
    property test: it must detect ANY cycle, ANY length, over the whole
    derived cascade set — not just this one).

    Fixture, public verbs only (`_derive_impl` / `_supersede_impl` /
    `_cascade_resolve_impl` — no hand-inserted edges, no direct SQL writes;
    a hand-built unreachable state proves nothing, #2356):

        dv1 --------> ob   (base)
        dv2 --------> ob   (base)
        dv3 --------> dv2  (dv3 derives from dv2)
        dv4 --------> dv3  (dv4 derives from dv3)
        dv5 --------> dv1  (dv5 derives from dv1)

    Then: supersede(dv1 -> dv4), supersede(dv2 -> dv5) — each individually
    legal under §12.3 (neither replacement is yet downstream of what it
    replaces). Acknowledge dv3 (writes dv3 -> dv5). Before this guard, the
    FOURTH call — acknowledge dv5 (which would write dv5 -> dv4) — closes
    `dv3 -> dv5 -> dv4 -> dv3` (the last leg is dv4's pre-existing
    `derives_from` edge to dv3). Measured before the fix: confidence
    recompute stopped being idempotent (0.411, 0.322, 0.254 on identical
    re-runs) and conformance reported `flare: false`.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Base observation.", quote_type="official_statement")
    dv1 = _seed_dv(derive, "dv1, a base derivation.", [ob])
    dv2 = _seed_dv(derive, "dv2, a second base derivation.", [ob])
    dv3 = _seed_dv(derive, "dv3, derives from dv2.", [dv2])
    dv4 = _seed_dv(derive, "dv4, derives from dv3.", [dv3])
    dv5 = _seed_dv(derive, "dv5, derives from dv1.", [dv1])

    assert _cascade_cycle(core) is None, (
        "the fixture itself must start acyclic, or this test proves nothing")

    s1 = json.loads(rev._supersede_impl(
        old_node_id=dv1, new_node_id=dv4, supersede_reason="r1"))
    assert "error" not in s1, f"s1 must be individually legal under §12.3: {s1}"

    s2 = json.loads(rev._supersede_impl(
        old_node_id=dv2, new_node_id=dv5, supersede_reason="r2"))
    assert "error" not in s2, f"s2 must be individually legal under §12.3: {s2}"

    a1 = json.loads(rev._cascade_resolve_impl(
        node_id=dv3, resolution_kind="acknowledged"))
    assert "error" not in a1, f"acknowledging dv3 must be legal on its own: {a1}"

    # THE FOURTH CALL — this is the one the guard must refuse.
    a2 = json.loads(rev._cascade_resolve_impl(
        node_id=dv5, resolution_kind="acknowledged"))
    assert "error" in a2, (
        f"the guard must refuse the cycle-closing acknowledgement, got: {a2}")
    assert a2["guard"] == "acknowledgement_would_close_cascade_cycle", a2
    assert a2["relation"] == "acknowledged_derives_from", a2
    assert a2["node_id"] == dv5, a2
    # #2405: the remedy must NAME the resolution route (engram_cascade_resolve
    # with resolution_kind='superseded'/'retracted'), not the bare organic
    # verbs — "supersede or retract" alone is exactly the wording that got
    # misread as those organic verbs. Pin both the error string AND the
    # remedy key (the trap this replaces: a fix that rewords only one of the
    # two passes a remedy-only assertion while the error string — the one an
    # agent reads first — still ships the stale sentence).
    # What these two checks actually deliver: the exact phrase "supersede or
    # retract", case-insensitively, absent from both fields. They do NOT
    # catch whitespace variants (a doubled space, a line break between the
    # words) or paraphrases ("supersede/retract", "supersede or to retract")
    # — production text already uses "supersede/retract" elsewhere and
    # correctly does not trip this check.
    assert "engram_cascade_resolve" in a2["error"], a2
    assert "engram_cascade_resolve" in a2["remedy"], a2
    assert "supersede or retract" not in a2["error"].lower(), a2
    assert "supersede or retract" not in a2["remedy"].lower(), a2
    assert "next_call" in a2 and "engram_cascade_frontier" in a2["next_call"], a2
    assert "then" in a2 and "engram_cascade_resolve" in a2["then"], a2

    # THE PROPERTY: acyclic over the WHOLE derived cascade set, not merely
    # "the two edges the old test happened to check for".
    cycle = _cascade_cycle(core)
    assert cycle is None, f"cascade relation set has a cycle: {cycle}"


def test_acknowledgement_rollback_undoes_a_genuinely_written_prior_edge():
    """#2349 blocker 2 / round-2 reviewer suggestion — the ONE path the test
    above cannot exercise, by construction.

    §12.7's write-time guard sits inside a loop over `affected` in
    `_cascade_record_acknowledgement_edges`. If a LATER iteration raises
    after an EARLIER one already INSERTed an ack edge, correctness depends
    on `conn.rollback()` undoing that earlier insert (single connection, no
    intermediate commit — confirmed by reading the code). The test above
    gives its acknowledging node (dv5) exactly ONE affected premise, so the
    raise always fires on the first and only iteration — the
    rollback-of-a-prior-successful-insert path has never actually run.

    This fixture gives the acknowledging node (N) TWO affected premises —
    P2 (safe) and B (cycle-closing) — sorted so P2 processes FIRST (its id
    sorts before B's; both markers are on the SAME cascade event, so
    premise_id is the tiebreaker). Public verbs only, no hand-inserted
    edges (#2356):

        R_ob --> P2 --> N          R_ob --> B --> N     (N's two premises)
        X_ob --> Mp --> M          M --> RB              (the "bridge" arm)

    Retract R_ob (marks P2, B, N on ONE event). Resolve P2 -> RA (a
    genuinely unrelated replacement) — legal under §12.3, no relation to N.
    Build RB deriving from M, BEFORE M has anything to do with N, then
    resolve B -> RB — also legal under §12.3 at THAT moment (RB's only
    ancestor is M -> Mp -> X_ob; nothing connects it to B yet).

    THEN supersede Mp -> N directly (Mp's own supersede, not via B or P2 at
    all) and have M acknowledge it, writing M -> N. This is the same
    §12.3-is-point-in-time move the test above uses, except the bridge
    lands AFTER B's supersede was already validated, not via a second
    acknowledge on the SAME node the property test above exercises — so by
    the time N itself tries to acknowledge, RB (B's replacement, built on M)
    has become transitively downstream of N through the new M -> N edge,
    even though nothing about B or RB changed.

    N's own acknowledge call must therefore: write N -> RA cleanly (P2
    first, real INSERT, real committed... no, NOT committed — same
    connection, no intermediate commit), then raise on N -> RB (B second).
    Assert, after the refusal:
      - the call returns the SAME guard key as the property test above;
      - N -> RA is NOT in the edges table — the whole point: the rollback
        must have removed a row that was genuinely written, not merely
        skipped one that was never attempted;
      - N's own markers are unchanged (still pending, not resolved);
      - the graph is still acyclic by `_cascade_cycle`.

    Can-it-fail control (performed by hand during authoring, per this
    file's convention — see the sibling test's own docstring pointer). A
    plain no-op in place of `conn.rollback()` does NOT reproduce the bug —
    measured, not assumed: `core._get_db()` opens a fresh, non-pooled
    connection per call, and closing an sqlite3 connection with an
    uncommitted transaction pending discards it, so the surrounding
    `finally: conn.close()` already undoes an un-rolled-back INSERT as a
    side effect of connection teardown. The control that actually exercises
    what `conn.rollback()` is FOR is replacing it with `conn.commit()` —
    forcing the partial write to survive past close(). That mutation left
    N -> RA surviving in the edges table after the (still-returned)
    refusal, turning this test's `got is None` assertion red, while the
    property test above continued to PASS unchanged (it has only one
    affected premise, so there is nothing for a broken rollback to leave
    behind) — which is the actual demonstration that this test covers
    ground the older one structurally cannot.
    """
    rev, obs, derive, core = _make_fresh_mods()

    r_ob = _seed_ob(obs, "Root to retract.", quote_type="official_statement")
    x_ob = _seed_ob(obs, "Bridge base.", quote_type="official_statement")
    mp = _seed_dv(derive, "Mp, derives from x_ob.", [x_ob])

    p2 = _seed_dv(derive, "P2, derives from r_ob.", [r_ob])
    b = _seed_dv(derive, "B, derives from r_ob.", [r_ob])
    assert p2 < b, (
        "fixture requires p2's id to sort before b's, so P2 is the FIRST "
        "affected-premise entry processed within N's single acknowledge "
        "call — creation order controls this since ids are sequential")

    n = _seed_dv(derive, "N, derives from p2 and b.", [p2, b])
    m = _seed_dv(derive, "M, derives from mp.", [mp])

    assert _cascade_cycle(core) is None, (
        "the fixture itself must start acyclic, or this test proves nothing")

    ret = json.loads(rev._retract_impl(
        node_id=r_ob, error_type="hallucinated_claim", reason="w", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
    assert "error" not in ret, ret

    clean_ob = _seed_ob(obs, "Clean basis for P2's replacement.",
                         quote_type="official_statement")
    ra = _seed_dv(derive, "RA, safe replacement for P2.", [clean_ob])
    rp2 = json.loads(rev._cascade_resolve_impl(
        node_id=p2, resolution_kind="superseded", replacement_id=ra))
    assert "error" not in rp2, f"resolving P2 must be legal on its own: {rp2}"

    # RB is built on M BEFORE M has any relationship to N — so superseding
    # B -> RB is legal under §12.3 at this moment (checked below).
    rb = _seed_dv(derive, "RB, replacement for B, derives from M.", [m])
    rb_supersede = json.loads(rev._cascade_resolve_impl(
        node_id=b, resolution_kind="superseded", replacement_id=rb))
    assert "error" not in rb_supersede, (
        f"resolving B -> RB must be legal under §12.3 at THIS moment, "
        f"before the M -> N bridge exists: {rb_supersede}")

    # The bridge: Mp is superseded directly by N (unrelated to N's own
    # premises p2/b), then M acknowledges — writing M -> N. AFTER this, RB
    # (which derives from M) is transitively downstream of N, even though
    # RB's supersede-of-B was validated before this bridge existed.
    s_mp = json.loads(rev._supersede_impl(
        old_node_id=mp, new_node_id=n, supersede_reason="bridge"))
    assert "error" not in s_mp, f"Mp -> N must be legal under §12.3: {s_mp}"
    ack_m = json.loads(rev._cascade_resolve_impl(
        node_id=m, resolution_kind="acknowledged"))
    assert "error" not in ack_m, f"M acknowledging must be legal: {ack_m}"

    conn = core._get_db()
    try:
        reaches_before = conn.execute(
            "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? "
            "AND relation = 'acknowledged_derives_from'", (m, n)).fetchone()
    finally:
        conn.close()
    assert reaches_before, "the M -> N bridge edge must exist before N acts"

    n_markers_before = sorted(
        r["status"] for r in _markers(core, node_id=n))

    # THE CALL UNDER TEST: N acknowledges both P2 and B in ONE call. P2
    # (sorts first) writes N -> RA cleanly; B (sorts second) raises because
    # RB is now downstream of N via the bridge.
    ack_n = json.loads(rev._cascade_resolve_impl(
        node_id=n, resolution_kind="acknowledged"))
    assert "error" in ack_n, (
        f"the guard must refuse N's acknowledgement once B's replacement "
        f"RB is reachable from N: {ack_n}")
    assert ack_n["guard"] == "acknowledgement_would_close_cascade_cycle", ack_n
    assert ack_n["relation"] == "acknowledged_derives_from", ack_n
    assert ack_n["node_id"] == n, ack_n
    assert ack_n["replacement_id"] == rb, ack_n

    conn = core._get_db()
    try:
        got = conn.execute(
            "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? "
            "AND relation = 'acknowledged_derives_from'", (n, ra)).fetchone()
    finally:
        conn.close()
    assert got is None, (
        "THE WHOLE POINT: N -> RA was genuinely INSERTed by the first "
        "iteration before the second raised — conn.rollback() must have "
        "removed it. A surviving row here means the rollback path is "
        "broken (or never ran).")

    n_markers_after = sorted(
        r["status"] for r in _markers(core, node_id=n))
    assert n_markers_after == n_markers_before, (
        f"N's own markers must be unchanged by a refused acknowledgement: "
        f"before={n_markers_before}, after={n_markers_after}")

    cycle = _cascade_cycle(core)
    assert cycle is None, f"cascade relation set has a cycle: {cycle}"


# ---------------------------------------------------------------------------
# 2 — invariant 6: a marker resolves only from the frontier
# ---------------------------------------------------------------------------


def test_resolving_a_blocked_node_is_refused_and_names_the_blockers():
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Chain premise.")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])
    dv2 = _seed_dv(derive, "Transitive dependent.", [dv1])

    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="wrong", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv2, resolution_kind="retracted", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
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
        node_id=ob, error_type="hallucinated_claim", reason="wrong", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
    events_before = _events(core)
    assert len(events_before) == 1, events_before
    assert len(_markers(core, dv2)) == 1

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="retracted",
        note="argument is broken", error_type="wrong_citation", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
    assert "error" not in resp, resp
    assert "SUPPRESSED" in resp["re_cascade"], resp["re_cascade"]

    assert _events(core) == events_before, (
        "resolution must not open a new cascade event")
    assert len(_markers(core, dv2)) == 1, (
        "dv2 must not acquire a second marker — it already carries the "
        "root event's, and a second would force it to be adjudicated twice")

    # Invariant 7's FIRST line, which pulls the OPPOSITE way from the
    # no-re-cascade half and is therefore the thing a cascade_triggering
    # guard could silently swallow — quoted IN FULL (#2343: an earlier copy
    # here stopped at the em-dash, and the dropped clause is the refusal Lei
    # ruled on 2026-08-29; the truncation is how two seats read the design
    # backwards): "Superseding a marked node must also resolve its marker —
    # the tool refuses a supersede that supplies no resolution." The
    # resolution route (this verb) resolves it; the PLAIN verb refuses it.
    # The same holds for retracting one. If the
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
        node_id=ob, error_type="hallucinated_claim", reason="wrong", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
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
        node_id=ob, error_type="hallucinated_claim", reason="wrong", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
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
        node_id=ob, error_type="hallucinated_claim", reason="wrong", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="retracted", error_type="wrong_citation", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
    assert "error" not in resp, resp

    assert resp["auto_resolved_trivial"] == [], resp
    assert len(_markers(core, dv2, "pending")) == 1, (
        "dv2's premise genuinely died — it must reach the agent, not "
        "auto-resolve")


def test_trivial_auto_resolve_never_fires_on_an_axiom_even_when_all_premises_acknowledged():
    """#2406 — an identity-layer node must surface to the user, never be
    cleared by the machine's own trivial-resolution proof, no matter how
    cleanly the proof holds.

    MANUFACTURED STATE, same discipline as
    test_affected_contradiction_ids_ignores_a_malformed_contradicts_edge:
    no live write path can give an axiom a `derives_from` edge.
    `engram_add_axiom` only ever writes `cites` edges (context_ids), and
    `engram_add_edge` refuses `derives_from` outright (it is in
    CASCADE_RELATIONS and excluded from `_ADDABLE_AFTER_CREATION_RELATIONS`
    — created only at derivation-creation time, where the source is always
    the new derivation, never an axiom). So an axiom becoming a cascade
    candidate at all is unreachable today through any public write path —
    this test does not claim otherwise. The fix is defense-in-depth for a
    write path that does not yet exist, on the same "kept anyway as one
    cheap query" ground this function already applies to its frontier
    check — and it is cheap to get wrong silently, because #2401's
    dream-master text already promises an axiom-on-the-frontier surfaces to
    the user; without this gate the axiom would never REACH the frontier to
    make that promise apply.
    """
    rev, obs, derive, core = _make_fresh_mods()
    epi = importlib.import_module("engram_epistemic")

    ob = _seed_ob(obs, "Premise.", quote_type="official_statement")
    ob2 = _seed_ob(obs, "Premise refined.", quote_type="official_statement")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])

    ax = json.loads(epi._add_axiom_impl(
        claim="Identity-layer claim.",
        basis="Test fixture.",
    ))["axiom_id"]

    conn = core._get_db()
    try:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, 'derives_from', ?)",
            (ax, dv1, core._now()),
        )
        conn.commit()
    finally:
        conn.close()

    json.loads(rev._supersede_impl(old_node_id=ob, new_node_id=ob2,
                                   supersede_reason="refined"))
    assert len(_markers(core, dv1, "pending")) == 1

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged"))
    assert "error" not in resp, resp

    # Without the #2406 gate, ax's only affected premise (dv1) resolved
    # 'acknowledged' with nothing pending — exactly the trivial-resolve
    # predicate — and it would have auto-resolved right here, silently.
    assert ax not in [a["node_id"] for a in resp["auto_resolved_trivial"]], resp
    assert len(_markers(core, ax, "pending")) == 1, (
        "an axiom's cascade marker must stay pending — it belongs on the "
        "frontier for a human to see, not cleared by proof-of-no-delta")

    frontier = json.loads(rev._cascade_frontier_impl(node_id=ax))
    assert ax in [f["id"] for f in frontier["frontier"]], frontier


# ---------------------------------------------------------------------------
# 6 — confidence recompute, and it must be transitive
# ---------------------------------------------------------------------------


def test_acknowledge_recompute_follows_the_replacement():
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

    # §4's two requirements are COUPLED, and this is where that shows: the
    # recompute only has anything to read on the acknowledge path BECAUSE the
    # acknowledgement edge brings the replacement into the premise set. While
    # the edge was deferred this measured 0.833 -> 0.833, changed: [].
    #
    # dv1 now rests on ob (0.85) AND ob2 (0.35); weakest link wins. And it
    # propagates: dv2 was computed from dv1's OLD value, so one hop is not
    # enough.
    assert dv1_after == round(0.35 * 0.98, 3), (dv1_before, dv1_after)
    assert dv2_after < dv2_before, (dv2_before, dv2_after)
    assert dv2_after == round(dv1_after * 0.98, 3), dv2_after
    changed = {c["node_id"] for c in resp["confidence_recompute"]["changed"]}
    assert {dv1, dv2} <= changed, resp["confidence_recompute"]


def test_recompute_is_idempotent_across_the_acknowledgement_edge():
    """The phase split is NOT one transaction, and its whole safety argument
    is that the recompute can simply be re-run. That argument is only true
    while re-running is a no-op.

    This assertion used to live inside the cycle test, whose fixture the
    §12.3 guardrail now refuses to build — so it was deleted along with it.
    It is restored here on a LEGAL fixture, and it matters more after §12.5
    than before: `acknowledged_derives_from` joins the premise set, so the
    acknowledged node's own recompute now reads an edge that did not exist
    when the value was first computed. When the cycle version regressed it
    read 0.641, 0.567, 0.503, 0.446 across four IDENTICAL runs — decaying,
    never settling. A single run cannot see that; only repetition can.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Strong premise.", quote_type="official_statement")
    ob2 = _seed_ob(obs, "Weak replacement.", quote_type="editorial")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])
    dv2 = _seed_dv(derive, "Transitive dependent.", [dv1])

    json.loads(rev._supersede_impl(old_node_id=ob, new_node_id=ob2,
                                   supersede_reason="weaker"))
    ack = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged"))
    assert "error" not in ack, ack

    conn = core._get_db()
    try:
        # The edge must actually be there, or this test is idempotent about
        # nothing — the deferral measured a clean no-op for exactly that
        # reason.
        assert conn.execute(
            "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? "
            "AND relation = 'acknowledged_derives_from'",
            (dv1, ob2)).fetchone() is not None, "no acknowledgement edge to be idempotent across"

        # CAN-IT-FAIL. Without this, a recompute that did NOTHING would pass
        # the loop below — resolve already wrote the correct values, so four
        # no-ops read back four identical correct numbers. Poisoning the
        # stored values first means run 1 has to actually repair them.
        conn.execute("UPDATE nodes SET confidence = 0.111 WHERE id IN (?, ?)",
                     (dv1, dv2))

        runs = []
        for _ in range(4):
            rev._cascade_recompute_confidence_transitive(conn, ob)
            runs.append((
                conn.execute("SELECT confidence FROM nodes WHERE id = ?",
                             (dv1,)).fetchone()["confidence"],
                conn.execute("SELECT confidence FROM nodes WHERE id = ?",
                             (dv2,)).fetchone()["confidence"],
            ))
        conn.commit()
    finally:
        conn.close()

    assert len(set(runs)) == 1, (
        f"the recompute must be idempotent — the phase-split safety argument "
        f"rests on it. Got {runs}")
    # And it settled on the value the single-run test asserts, not merely on
    # SOME fixed point: a recompute that stuck at the pre-supersede number
    # would also be idempotent, and would be wrong.
    assert runs[0] == (round(0.35 * 0.98, 3), round(0.35 * 0.98 * 0.98, 3)), runs[0]



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


# ---------------------------------------------------------------------------
# #3125 — replacement_absent_because / replacement_absent_note are
# retracted-only. The prior guard only refused them for "acknowledged", so
# passing them alongside a "superseded" resolution was silently accepted and
# dropped on the floor, despite the docstring's own "REQUIRED iff
# resolution_kind is 'retracted'" contract. C1/C2 below are the gap and its
# discriminating control; C3/C4 pin the unchanged acknowledged/retracted
# behaviour so a later change to this guard cannot regress either without
# being caught here.
# ---------------------------------------------------------------------------


def test_superseded_refuses_spurious_replacement_absent_because():
    """C1a (the gap, now fixed). A legitimate 'superseded' resolution with a
    valid replacement_id must still be refused if it also carries a
    replacement_absent_because — that field is retracted-only, and silently
    accepting it here means it is validated nowhere and simply dropped.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Chain premise.")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])
    dv1b = _seed_dv(derive, "Direct dependent, restated.", [ob])
    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="wrong", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="superseded", replacement_id=dv1b,
        replacement_absent_because="NONE_BECAUSE_NO_SUCCESSOR"))
    assert "error" in resp, resp
    assert "only meaningful" in resp["error"], resp
    assert len(_markers(core, dv1, "pending")) == 1, _markers(core, dv1)


def test_superseded_refuses_spurious_replacement_absent_note():
    """C1b — the replacement_absent_note twin of the test above."""
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Chain premise.")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])
    dv1b = _seed_dv(derive, "Direct dependent, restated.", [ob])
    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="wrong", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="superseded", replacement_id=dv1b,
        replacement_absent_note="not applicable"))
    assert "error" in resp, resp
    assert "only meaningful" in resp["error"], resp
    assert len(_markers(core, dv1, "pending")) == 1, _markers(core, dv1)


def test_superseded_with_only_replacement_id_still_accepted():
    """C2 — the discriminating control. A legitimate 'superseded' resolution
    that supplies ONLY replacement_id (no replacement_absent_* fields) must
    stay accepted. This is the test that goes RED under the naive/wrong fix
    shape (widening the single existing acknowledged-only check to
    `resolution_kind != "retracted"` across all three fields) — that
    widening would also catch replacement_id itself and refuse every
    legitimate superseded resolve, since superseded REQUIRES replacement_id.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Chain premise.")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])
    dv1b = _seed_dv(derive, "Direct dependent, restated.", [ob])
    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="wrong", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="superseded", replacement_id=dv1b))
    assert "error" not in resp, resp
    assert resp["resolution_kind"] == "superseded", resp


def test_acknowledged_still_refuses_all_three_replacement_fields():
    """C3 — unchanged behaviour. This should already pass without any code
    change to the guard under test: it is a regression guard confirming the
    acknowledged-side check was not accidentally loosened while fixing the
    gap above.
    """
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Original premise.", quote_type="official_statement")
    ob2 = _seed_ob(obs, "Refined premise.", quote_type="editorial")
    dv1 = _seed_dv(derive, "Depends on the premise.", [ob])
    json.loads(rev._supersede_impl(
        old_node_id=ob, new_node_id=ob2, supersede_reason="refined"))

    with_id = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged", replacement_id=ob2))
    assert "error" in with_id, with_id
    assert "only meaningful" in with_id["error"], with_id

    with_because = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged",
        replacement_absent_because="NONE_BECAUSE_NO_SUCCESSOR"))
    assert "error" in with_because, with_because
    assert "only meaningful" in with_because["error"], with_because

    with_note = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="acknowledged",
        replacement_absent_note="not applicable"))
    assert "error" in with_note, with_note
    assert "only meaningful" in with_note["error"], with_note

    assert len(_markers(core, dv1, "pending")) == 1, _markers(core, dv1)


def test_retracted_with_replacement_absent_because_still_accepted():
    """C4 — unchanged behaviour on the retracted path, following the same
    fixture/acceptance shape as
    test_retract_resolution_opens_no_new_DERIVATION_cascade_event above."""
    rev, obs, derive, core = _make_fresh_mods()

    ob = _seed_ob(obs, "Chain premise.")
    dv1 = _seed_dv(derive, "Direct dependent.", [ob])
    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="wrong", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))

    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="retracted",
        replacement_absent_because="NONE_BECAUSE_NO_SUCCESSOR"))
    assert "error" not in resp, resp
    assert resp["resolution_kind"] == "retracted", resp


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
        node_id=ob, error_type="hallucinated_claim", reason="w", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
    r = json.loads(rev._cascade_resolve_impl(
        node_id=dv_leaf, resolution_kind="retracted", error_type="other", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
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
        node_id=ob, error_type="hallucinated_claim", reason="w", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
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
        node_id=d2, resolution_kind="retracted", error_type="other", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
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
    """The `via="marker"` branch of _cascade_record_acknowledgement_edges —
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
        node_id=r_ob, error_type="hallucinated_claim", reason="w", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))

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
    assert rn["acknowledgement_edges"] == [
        {"premise_id": p, "replacement_id": p2}
    ], rn["acknowledgement_edges"]
    conn = core._get_db()
    try:
        got = conn.execute(
            "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? "
            "AND relation = 'acknowledged_derives_from'", (n, p2)).fetchone()
    finally:
        conn.close()
    assert got, "the marker-branch replacement must be recorded on the new relation"


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
        node_id=ob, error_type="hallucinated_claim", reason="w", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))

    clean = _seed_ob(obs, "Clean basis.")
    d1b = _seed_dv(derive, "First dependent, restated.", [clean])
    json.loads(rev._cascade_resolve_impl(
        node_id=d1, resolution_kind="superseded", replacement_id=d1b))
    assert "stale_flagged" in _edit_actions(ct), (
        f"a supersede resolution must record stale_flagged: {_edit_actions(ct)}")

    json.loads(rev._cascade_resolve_impl(
        node_id=d2, resolution_kind="retracted", error_type="other", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
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
        node_id=ob, error_type="hallucinated_claim", reason="w", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
    events_before = _events(core)

    r = json.loads(rev._cascade_resolve_impl(
        node_id=dv1, resolution_kind="retracted", error_type="other", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
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
        node_id=ct, error_type="other", reason="the contradiction was mis-filed", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))
    json.loads(rev._retract_impl(
        node_id=ob, error_type="hallucinated_claim", reason="w", replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test"))

    clean = _seed_ob(obs, "Clean basis.")
    d1b = _seed_dv(derive, "Dependent, restated.", [clean])
    r = json.loads(rev._cascade_resolve_impl(
        node_id=d1, resolution_kind="superseded", replacement_id=d1b))
    assert "error" not in r, r
    assert r["marked_contradictions"] == [], (
        f"a retracted contradiction must not be marked: {r}")
    assert r["contradiction_cascade_event"] is None, r
