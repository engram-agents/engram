"""CD-02 — one judgement on C resolves BOTH of its events; no node is minted.

SETUP    converging_diamonds; supersede A → A2 and B → B2 (C carries 2 markers).
ACTIONS  resolve C as `acknowledged` — ONE call, no cascade_event argument.
EXPECT   • The call reports both events resolved (resolved_marker_count == 2).
         • C has no pending markers; both resolved as `acknowledged`.
         • acknowledged_derives_from edges written C→A2 AND C→B2 — one per
           superseding replacement (#2349 §4/§12.5).
         • The node table did not grow (resolve is pure-wire, #2342 §10.2).
         • D is untouched (still 2 pending); E still 2 pending, still not on
           the frontier; frontier is now exactly {D}.
Claims:  #2342 (per-node, pure-wire), #2349 (ack edge).
"""

from _dsl import (
    cascade_playbox, expect_ok, expect_frontier, expect_pending, expect_resolved,
    expect_edges, expect_node_count, node_count, expect_clean,
)


def test_cd02_one_judgement_clears_both_events():
    with cascade_playbox("converging_diamonds") as box:
        # ---- SETUP -------------------------------------------------------
        expect_ok(box.supersede(box["A"], box["A2"], reason="CD-02 event 1"))
        expect_ok(box.supersede(box["B"], box["B2"], reason="CD-02 event 2"))
        expect_pending(box, "C", events=2, stage="setup")
        nodes_before = node_count(box)

        # ---- ACTIONS -----------------------------------------------------
        r = expect_ok(box.resolve(box["C"], "acknowledged", note="CD-02"), "resolve C")

        # ---- EXPECT ------------------------------------------------------
        assert r["resolved_marker_count"] == 2, r
        assert len(r["resolved_events"]) == 2, r
        expect_resolved(box, "C", kind="acknowledged", stage="after ack C")
        expect_edges(box, "acknowledged_derives_from", [("C", "A2"), ("C", "B2")], stage="after ack C")
        expect_node_count(box, nodes_before, stage="after ack C (pure-wire)")

        expect_pending(box, "D", events=2, stage="after ack C")
        expect_pending(box, "E", events=2, stage="after ack C")
        expect_frontier(box, ["D"], stage="after ack C")
        expect_clean(box, stage="after ack C")
