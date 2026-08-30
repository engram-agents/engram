"""CD-10 — a plain `engram_retract` of a NON-CURRENT node is refused.

RULING (Lei, 2026-08-30 00:03Z, relayed by Kepler in `gh issue view 2396`):
BAN. A plain `engram_retract` of a node with `is_current = 0` is REFUSED
with a pointer. `_retract_impl` never checked `is_current`; `_supersede_impl`
did (`:2128`, refuses a non-current `old_node_id`). That asymmetry re-opened
#2343's double-adjudication through a second door and made plain retract
non-idempotent on an already-superseded/retracted node (retract twice ->
two markers on every descendant, unbounded). Real-graph incidence of "both
superseded and retracted": zero in 8454 nodes. #2396 bans the operation;
this subsumes #2395 (the `and node["is_current"] == 1` clause on the #2343
pending-marker guard becomes unnecessary rather than merely dead, since the
new refusal already excludes non-current input).

SETUP    chain (base -> d1 -> d2 -> d3).

CD-10a   SETUP    supersede(d2 -> d2b) — d3 carries one pending supersede
                  marker; d3 acknowledges via the resolve verb so d3's
                  marker is fully resolved.
         ACTION   plain retract(d2) — d2 is already non-current.
         EXPECT   refused; message names the successor / points at
                  engram_history; d3's marker stays resolved and NOT
                  pending; nothing pending anywhere in the box; node
                  count and edge set unchanged; expect_clean. This is
                  #2396's finding (1): a fully-adjudicated descendant is
                  not dragged back.

CD-10b   SETUP    chain, nothing marked.
         ACTIONS  retract(d2) -> OK (d3 gets one marker); retract(d2)
                  again.
         EXPECT   the second call is REFUSED; d3 still carries exactly
                  ONE pending marker, not two; no new nodes, no new
                  edges. This is #2396's finding (3): plain retract on a
                  non-current node was not idempotent.

CD-10c   SETUP    supersede(d2 -> d2b) — d3 carries one pending supersede
                  marker, deliberately NOT acknowledged.
         ACTION   plain retract(d2).
         EXPECT   refused; d3 carries exactly ONE pending event
                  (supersede), never two; expect_clean. This is #2396's
                  finding (2): the double-adjudication door stays shut.

Claims:  #2396 (the ban), #2395 (folded in), #2343 / #2013 invariant 7.
"""

from _dsl import (
    cascade_playbox, expect_ok, expect_refused, expect_pending, expect_resolved,
    expect_clean, node_count, expect_node_count,
)


def _supersede_d2(box):
    """Mint d2b and supersede d2 -> d2b. Returns the d2b handle."""
    d2b = box.node("d2b", kind="derivation", supports=[box["d1"]], claim="d2b: revised d2.")
    expect_ok(box.supersede(box["d2"], d2b, reason="CD-10 event 1"))
    expect_pending(box, "d3", events=1, stage="setup (after supersede)")
    return d2b


def test_cd10a_retract_of_superseded_node_does_not_reopen_resolved_descendant():
    with cascade_playbox("chain") as box:
        # ---- SETUP ---------------------------------------------------
        d2b = _supersede_d2(box)
        expect_ok(
            box.resolve(box["d3"], "acknowledged", note="CD-10a d3 acknowledges event 1"),
            "resolve(d3, acknowledged)",
        )
        expect_resolved(box, "d3", kind="acknowledged", stage="setup (d3 fully adjudicated)")
        edges_before = box.edges()
        nodes_before = node_count(box)

        # ---- ACTIONS ---------------------------------------------------
        r = box.retract(box["d2"], reason="CD-10a plain retract of non-current d2")

        # ---- EXPECT ------------------------------------------------------
        expect_refused(r, "plain retract of non-current d2", mentions="successor")
        assert "engram_history" in r.get("next_call", ""), (
            f"refusal should point at engram_history via next_call: {r}"
        )
        assert r.get("successor_id") == box.id_of(d2b), (
            f"refusal should name d2b as the successor: {r}"
        )
        expect_resolved(box, "d3", kind="acknowledged", stage="after refusal (still resolved)")
        assert not _has_pending(box, "d3"), "d3 must not be dragged back onto the frontier"
        assert box.edges() == edges_before, "a refused retract must write no edge"
        expect_node_count(box, nodes_before, stage="after refusal")
        expect_clean(box, stage="after refusal")


def test_cd10b_plain_retract_of_non_current_node_is_not_idempotent_bug_now_refused():
    with cascade_playbox("chain") as box:
        # ---- SETUP ---------------------------------------------------
        edges_after_first = None
        nodes_after_first = None

        # ---- ACTIONS -----------------------------------------------------
        r1 = expect_ok(box.retract(box["d2"], reason="CD-10b first retract of d2"), "first retract(d2)")
        expect_pending(box, "d3", events=1, stage="after first retract")
        edges_after_first = box.edges()
        nodes_after_first = node_count(box)

        r2 = box.retract(box["d2"], reason="CD-10b second retract of d2")

        # ---- EXPECT ------------------------------------------------------
        expect_refused(r2, "second plain retract of already-retracted d2", mentions="not current")
        assert "engram_history" in r2.get("next_call", ""), (
            f"refusal should point at engram_history via next_call: {r2}"
        )
        assert "successor_id" not in r2, f"a purely-retracted node has no successor: {r2}"
        expect_pending(box, "d3", events=1, stage="after second refused retract (still exactly one)")
        assert box.edges() == edges_after_first, "a refused retract must write no new edge"
        expect_node_count(box, nodes_after_first, stage="after second refused retract")


def test_cd10c_retract_of_superseded_node_does_not_open_a_second_pending_event():
    with cascade_playbox("chain") as box:
        # ---- SETUP ---------------------------------------------------
        _supersede_d2(box)  # d3 carries one pending supersede marker, unacknowledged

        # ---- ACTIONS -----------------------------------------------------
        r = box.retract(box["d2"], reason="CD-10c plain retract of superseded, unacknowledged d2")

        # ---- EXPECT ------------------------------------------------------
        expect_refused(r, "plain retract of superseded d2 (unacknowledged descendant)", mentions="successor")
        assert "engram_history" in r.get("next_call", ""), (
            f"refusal should point at engram_history via next_call: {r}"
        )
        expect_pending(box, "d3", events=1, stage="after refusal (still exactly one pending event)")
        expect_clean(box, stage="after refusal")


def _has_pending(box, name):
    return any(m["status"] == "pending" for m in box.markers(box[name]))
