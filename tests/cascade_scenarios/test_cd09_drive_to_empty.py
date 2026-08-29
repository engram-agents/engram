"""CD-09 — resolving frontier-by-frontier terminates with nothing pending.

SETUP    converging_diamonds; retract A and retract B (two events; every
         non-root node carries two markers).
ACTIONS  the §10.3 driver loop: read frontier; resolve every entry as
         `retracted` (the only shape eligible after a retracted root);
         repeat until the frontier is empty.
EXPECT   • Progression is exactly {C, D} → {E} → {} (E surfaces only once
           both parents are resolved; a multi-event node is adjudicated once).
         • Loop terminates in 2 rounds.
         • total_pending_nodes == 0 at the end; `invariant_1_violation_
           suspected` never appears (the empty-frontier-with-pending anomaly
           #2338 round 2 added a flag for).
         • Every node's markers resolved as `retracted`; conformance clean.
Claims:  #2338 §10.3 (driver termination, anomaly flag), #2342 (per-node).
"""

from _dsl import (
    cascade_playbox, expect_ok, expect_frontier, expect_resolved,
    expect_nothing_pending, expect_clean,
)


def test_cd09_drive_to_empty():
    with cascade_playbox("converging_diamonds") as box:
        # ---- SETUP -------------------------------------------------------
        expect_ok(box.retract(box["A"], reason="CD-09 A"))
        expect_ok(box.retract(box["B"], reason="CD-09 B"))

        # ---- ACTIONS + EXPECT, per round ---------------------------------
        progression = []
        for round_no in range(10):
            f = box.frontier()
            assert "invariant_1_violation_suspected" not in f, f
            names = sorted(box._handle_of(e["id"]) for e in f["frontier"])
            if not names:
                break
            progression.append(names)
            for n in names:
                expect_ok(box.resolve(box[n], "retracted", note=f"CD-09 round {round_no}"), f"resolve {n}")
        else:
            raise AssertionError("driver loop did not terminate in 10 rounds")

        assert progression == [["C", "D"], ["E"]], f"frontier progression was {progression}"
        expect_nothing_pending(box, stage="after drive")
        for n in ("C", "D", "E"):
            expect_resolved(box, n, kind="retracted", stage="after drive")
        expect_clean(box, stage="after drive")
