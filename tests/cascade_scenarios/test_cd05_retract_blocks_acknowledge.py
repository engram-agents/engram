"""CD-05 — RETRACT A (not supersede) → C and D may only be retracted or superseded.

SETUP    converging_diamonds.
ACTIONS  retract A; then attempt resolve C as `acknowledged`.
EXPECT   • The acknowledge is REFUSED: a retract removes a leg from the
           argument, so "nothing changed" cannot be true (#2342 eligibility:
           acknowledge iff NO affected direct premise resolved as retracted;
           the cascade root counts as a premise resolving as root_op).
         • The refusal names the eligible shapes {retracted, superseded} and
           names A as the blocker.
         • The refused call leaves C's marker pending (nothing half-applied).
         • Retracting C then succeeds; E stays pending until D resolves too.
Claims:  #2342 (eligibility), #2338 (frontier unchanged by a refusal).
"""

from _dsl import (
    cascade_playbox, expect_ok, expect_refused, expect_frontier, expect_pending,
    expect_resolved, expect_clean,
)


def test_cd05_retract_blocks_acknowledge():
    with cascade_playbox("converging_diamonds") as box:
        # ---- SETUP / ACTIONS ---------------------------------------------
        expect_ok(box.retract(box["A"], reason="CD-05 A was wrong"), "retract A")
        r = box.resolve(box["C"], "acknowledged", note="CD-05 must refuse")

        # ---- EXPECT ------------------------------------------------------
        expect_refused(r, "acknowledge C", mentions="retracted")
        assert sorted(r["eligible"]) == ["retracted", "superseded"], r
        assert box._handle_of(r["acknowledge_blocked_by"][0]) == "A", r
        expect_pending(box, "C", events=1, stage="after refused ack")
        expect_frontier(box, ["C", "D"], stage="after refused ack")

        expect_ok(box.resolve(box["C"], "retracted", note="CD-05 C follows A"), "retract C")
        expect_resolved(box, "C", kind="retracted", stage="after retract C")
        expect_pending(box, "E", events=1, stage="after retract C")
        expect_frontier(box, ["D"], stage="after retract C")
        expect_clean(box, stage="after retract C")
