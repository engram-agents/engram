"""CD-04 — ack C but SUPERSEDE D → E is NOT trivial; it must surface.

SETUP    converging_diamonds; supersede A → A2 and B → B2; mint D2 (a
         derivation from A2 and B2) as D's replacement.
ACTIONS  resolve C as `acknowledged`; resolve D as `superseded` → D2.
EXPECT   • After both: E still carries pending markers — the trivial rule
           does NOT fire, because one affected premise (D) substantively
           changed. E must be adjudicated by an agent.
         • Frontier == {E}.
         • E's frontier entry names D (via the `superseded` resolution) as
           an affected premise / immediate cause, so the agent sees WHY.
         • Acknowledging E is still eligible (nothing was retracted) and,
           once done, writes acknowledged_derives_from E→D2 and leaves
           nothing pending.
Claims:  #2342 (trivial rule is by proof, not by default), #2349 (ack edge
         to the superseding replacement), #2338 (adjudication packet).
"""

from _dsl import (
    cascade_playbox, expect_ok, expect_frontier, expect_pending, expect_resolved,
    expect_nothing_pending, expect_edges, expect_clean,
)


def test_cd04_non_trivial_surfaces_e():
    with cascade_playbox("converging_diamonds") as box:
        # ---- SETUP -------------------------------------------------------
        expect_ok(box.supersede(box["A"], box["A2"], reason="CD-04 event 1"))
        expect_ok(box.supersede(box["B"], box["B2"], reason="CD-04 event 2"))
        D2 = box.node("D2", kind="derivation", supports=[box["A2"], box["B2"]],
                      claim="D2: D rebuilt on A2 and B2.")

        # ---- ACTIONS -----------------------------------------------------
        expect_ok(box.resolve(box["C"], "acknowledged", note="CD-04 C"))
        expect_ok(box.resolve(box["D"], "superseded", replacement=D2, note="CD-04 D"))

        # ---- EXPECT ------------------------------------------------------
        expect_pending(box, "E", events=2, stage="after ack C + supersede D")
        f = expect_frontier(box, ["E"], stage="after ack C + supersede D")
        entry = f["frontier"][0]
        def _pid(p):  # packet premise entries are dicts {premise_id, resolution_kind, ...}
            return p["premise_id"] if isinstance(p, dict) else p
        dead = [p for ev in entry["events"] for p in ev.get("dead_premises", [])]
        named = {box._handle_of(_pid(p)) for p in dead}
        assert "D" in named, f"E's packet should name D as the changed premise; got {entry['events']}"
        assert any(isinstance(p, dict) and p.get("resolution_kind") == "superseded"
                   and box._handle_of(p["premise_id"]) == "D" for p in dead), dead

        r = expect_ok(box.resolve(box["E"], "acknowledged", note="CD-04 E"), "resolve E")
        assert "acknowledged" in r["eligible_shapes"], r
        expect_resolved(box, "E", kind="acknowledged", stage="after ack E")
        expect_edges(
            box, "acknowledged_derives_from",
            [("C", "A2"), ("C", "B2"), ("E", "D2")],
            stage="after ack E",
        )
        expect_nothing_pending(box, stage="after ack E")
        expect_clean(box, stage="after ack E")
