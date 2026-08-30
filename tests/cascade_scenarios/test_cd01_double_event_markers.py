"""CD-01 — superseding BOTH roots marks C, D, E twice; only C and D surface.

SETUP    converging_diamonds: A, B roots; C, D derive from both; E from C, D.
ACTIONS  supersede A → A2, then supersede B → B2 (two independent events).
EXPECT   • C, D, E each carry exactly TWO pending markers (one per event).
         • Frontier (mode B) is exactly {C, D}: E is pending but NOT on the
           frontier because its own premises are unresolved (#2338: the
           predicate is transitive; a multi-event node appears once).
         • Mode C on E lists {C, D} as its pending ancestors.
         • Each frontier entry lists BOTH events, both with the root as a
           direct premise resolving as `supersede`.
         • Conformance clean, no cycle.
Claims:  #2338 (frontier read), #2342 (marker substrate behaviour).
"""

from _dsl import (
    cascade_playbox, expect_ok, expect_frontier, expect_pending, expect_clean,
)


def test_cd01_double_event_markers():
    with cascade_playbox("converging_diamonds") as box:
        # ---- SETUP -------------------------------------------------------
        for n in ("C", "D", "E"):
            assert box.markers(box[n]) == [], f"{n} must start with no markers"

        # ---- ACTIONS -----------------------------------------------------
        expect_ok(box.supersede(box["A"], box["A2"], reason="CD-01 event 1"), "supersede A")
        expect_ok(box.supersede(box["B"], box["B2"], reason="CD-01 event 2"), "supersede B")

        # ---- EXPECT ------------------------------------------------------
        for n in ("C", "D", "E"):
            expect_pending(box, n, events=2, stage="after both supersedes")

        f = expect_frontier(box, ["C", "D"], stage="after both supersedes")
        assert f["total_pending_nodes"] == 3, f
        expect_frontier(box, ["C", "D"], node="E", stage="mode C on E")

        for entry in f["frontier"]:
            ev = entry["events"]
            assert len(ev) == 2, f"{box._handle_of(entry['id'])}: expected 2 events, got {ev}"
            assert {e["root_op"] for e in ev} == {"supersede"}
            assert all(e["root_is_direct_premise"] for e in ev)
            assert sorted(box._handle_of(e["root_node"]) for e in ev) == ["A", "B"]

        expect_clean(box, stage="after both supersedes")
