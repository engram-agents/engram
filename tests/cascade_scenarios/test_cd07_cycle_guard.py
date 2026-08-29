"""CD-07 — superseding C with E (its own descendant) is refused; graph unchanged.

SETUP    converging_diamonds; snapshot edges and node count.
ACTIONS  supersede C → E.
EXPECT   • Refused at supersede time: E transitively derives from C, so
           installing it as C's replacement would make the replacement stale
           by the act that installs it (#2349 §12.3 guardrail).
         • Edges and node count are byte-identical to before; no cascade
           event opened (D and E carry no markers); the graph is acyclic.
         • A legitimate supersede (C → C2 minted from A and B) is accepted.
Claims:  #2349 (reachability guard ranges over the derived cascade set).
"""

from _dsl import (
    cascade_playbox, expect_ok, expect_refused, expect_pending, expect_node_count,
    node_count, expect_clean,
)


def test_cd07_cycle_guard():
    with cascade_playbox("converging_diamonds") as box:
        # ---- SETUP -------------------------------------------------------
        edges_before = box.edges()
        nodes_before = node_count(box)

        # ---- ACTIONS -----------------------------------------------------
        r = box.supersede(box["C"], box["E"], reason="CD-07 cycle attempt")

        # ---- EXPECT ------------------------------------------------------
        expect_refused(r, "supersede C -> E (own descendant)")
        assert box.edges() == edges_before, "a refused supersede must write no edge"
        expect_node_count(box, nodes_before, stage="after refusal")
        for n in ("D", "E"):
            assert box.markers(box[n]) == [], f"{n} must carry no marker after a refusal"
        expect_clean(box, stage="after refusal")

        C2 = box.node("C2", kind="derivation", supports=[box["A"], box["B"]], claim="C2: C redone.")
        expect_ok(box.supersede(box["C"], C2, reason="CD-07 legitimate"), "supersede C -> C2")
        expect_pending(box, "E", events=1, stage="after legitimate supersede")
        expect_clean(box, stage="after legitimate supersede")
