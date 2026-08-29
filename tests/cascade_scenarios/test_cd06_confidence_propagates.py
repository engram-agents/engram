"""CD-06 — acknowledging after A → A2 (weaker) lowers C, and E follows —
          EVEN THOUGH E IS STILL PENDING.

SETUP    converging_diamonds; snapshot confidence of C, D, E.
         A2 is deliberately a weaker source than A (quote_type editorial);
         B2 is the same strength as B.
ACTIONS  supersede A → A2 only; resolve C as `acknowledged`.
EXPECT   • C's confidence CHANGES and goes DOWN (its premise set now reads
           through the acknowledged_derives_from edge to the weaker A2 —
           #2349: the recompute reads the derived cascade set, so the edge
           is consulted).
         • E's confidence changes too, transitively, even though E was NOT
           adjudicated: E's marker is still pending, and E is not on the
           frontier (D is still unresolved). See the note below.
         • D's confidence is unchanged (D was not adjudicated; nothing
           upstream of D moved).
         • Numbers are printed for comparison with #2349's own fixture
           (0.833 → 0.343 there); the assertions are direction and reach.

WHY E'S CONFIDENCE MOVES WHILE E IS STILL PENDING — read this before asking
"shouldn't confidence only be recomputed when the node resolves?"

  The cascade machine carries TWO propagations with OPPOSITE rules
  (#2013 §4, its own boxed warning):

    MARKERS do NOT re-cascade on resolution.   They track ADJUDICATION —
        "has an agent (or the §4 trivial proof) confirmed this node's CLAIM
        still stands?" A pending marker is the caution flag on E.

    CONFIDENCE DOES propagate, transitively, to a fixpoint, on EVERY
        resolution of any kind (and on trivial auto-resolution). It tracks
        ARITHMETIC — "what do E's premises currently support?"

  E's stored number was COMPUTED FROM C's old value. Once C is recomputed,
  leaving E's old number in place would make the graph assert a value it no
  longer derives — §4 calls that "a provenance it did not compute from:
  structurally incoherent, and silent". The interim can be long (until
  someone adjudicates D), so the stale number would be read in the meantime.
  Recomputing early is cheap and harmless in both of E's end states: if E is
  later acknowledged it is recomputed again (idempotent); if E is superseded
  or retracted its number is moot (non-current).

  Ruled 2026-08-29 by Lei, on this exact case: keep the arithmetic correct
  DURING resolution; the marker, not the number, carries "under review".
  What must never happen is code or a reader treating "confidence changed"
  as "resolved" — the last three assertions below pin that distinction.

Claims:  #2013 §4 (confidence always recomputed, transitively), #2349
         (recompute reads the ack edge), #2338 (E stays off the frontier).
"""

from _dsl import (
    cascade_playbox, expect_ok, expect_frontier, expect_pending, expect_confidence,
    snapshot_confidence, expect_edges, expect_clean,
)


def test_cd06_confidence_propagates():
    with cascade_playbox("converging_diamonds") as box:
        # ---- SETUP -------------------------------------------------------
        before = snapshot_confidence(box, ["C", "D", "E"])

        # ---- ACTIONS -----------------------------------------------------
        expect_ok(box.supersede(box["A"], box["A2"], reason="CD-06 A weaker"))
        r = expect_ok(box.resolve(box["C"], "acknowledged", note="CD-06 C"))

        # ---- EXPECT: the arithmetic propagation --------------------------
        after = snapshot_confidence(box, ["C", "D", "E"])
        print(f"confidence C {before['C']} -> {after['C']}; "
              f"D {before['D']} -> {after['D']}; E {before['E']} -> {after['E']}")
        expect_edges(box, "acknowledged_derives_from", [("C", "A2")], stage="after ack C")
        expect_confidence(box, "C", changed_from=before["C"], stage="after ack C")
        assert after["C"] < before["C"], "a weaker premise must LOWER C, not raise it"
        expect_confidence(box, "E", changed_from=before["E"], stage="after ack C (transitive)")
        assert after["E"] < before["E"], "E is computed from C, so it must follow C down"
        expect_confidence(box, "D", unchanged_from=before["D"], stage="after ack C")
        changed = {box._handle_of(x["node_id"]) if isinstance(x, dict) else box._handle_of(x)
                   for x in r["confidence_recompute"]["changed"]}
        assert {"C", "E"} <= changed, r["confidence_recompute"]

        # ---- EXPECT: the two propagations stay DISTINCT ------------------
        # E's number moved, but E is NOT resolved and NOT adjudicable yet:
        expect_pending(box, "E", events=1, stage="E still pending after its confidence moved")
        expect_frontier(box, ["D"], stage="E not on the frontier: D unresolved")
        assert all(m["resolution_kind"] is None for m in box.markers(box["E"])), (
            "a confidence recompute must never write a resolution on E"
        )
        expect_clean(box, stage="after ack C")
