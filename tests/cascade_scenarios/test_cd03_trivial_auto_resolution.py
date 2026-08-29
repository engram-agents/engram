"""CD-03 — ack C and ack D → E auto-resolves as acknowledged, never surfaces.

SETUP    converging_diamonds; supersede A → A2 and B → B2.
ACTIONS  resolve C as `acknowledged`; then resolve D as `acknowledged`.
EXPECT   • After ack C: E still pending (2 events), frontier == {D}.
         • The ack of D reports E in `auto_resolved_trivial`.
         • After ack D: E has NO pending markers and its markers read
           `acknowledged` — the machine decided it, by proof: every affected
           premise of E resolved as acknowledged, so there is no delta for an
           agent to weigh (#2013 §4 trivial-resolution rule; §10.2 "the agent
           never sees them on the frontier").
         • Frontier empty, total_pending_nodes == 0, no anomaly flag.
         • E gets NO acknowledged_derives_from edge (its premises C, D were
           not superseded — only A, B were). Edges: C→A2, C→B2, D→A2, D→B2.
Claims:  #2342 (trivial auto-resolution), #2338 (frontier after).
"""

from _dsl import (
    cascade_playbox, expect_ok, expect_frontier, expect_pending, expect_resolved,
    expect_nothing_pending, expect_edges, expect_clean,
)


def test_cd03_trivial_auto_resolution():
    with cascade_playbox("converging_diamonds") as box:
        # ---- SETUP -------------------------------------------------------
        expect_ok(box.supersede(box["A"], box["A2"], reason="CD-03 event 1"))
        expect_ok(box.supersede(box["B"], box["B2"], reason="CD-03 event 2"))

        # ---- ACTIONS + EXPECT, stage 1 -----------------------------------
        expect_ok(box.resolve(box["C"], "acknowledged", note="CD-03 C"))
        expect_pending(box, "E", events=2, stage="after ack C")
        expect_frontier(box, ["D"], stage="after ack C")

        # ---- ACTIONS + EXPECT, stage 2 -----------------------------------
        r = expect_ok(box.resolve(box["D"], "acknowledged", note="CD-03 D"))
        auto = [box._handle_of(x if isinstance(x, str) else x.get("node_id")) for x in r["auto_resolved_trivial"]]
        assert "E" in auto, f"ack D should report E as auto-resolved; got {r['auto_resolved_trivial']}"

        expect_resolved(box, "E", kind="acknowledged", stage="after ack D")
        expect_nothing_pending(box, stage="after ack D")
        expect_edges(
            box, "acknowledged_derives_from",
            [("C", "A2"), ("C", "B2"), ("D", "A2"), ("D", "B2")],
            stage="after ack D",
        )
        expect_clean(box, stage="after ack D")
