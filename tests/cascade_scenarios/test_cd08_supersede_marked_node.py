"""CD-08 — a node with a PENDING marker cannot be superseded by the plain verb;
          the only route out is the resolve verb, which partially resolves the
          existing event.

RULING (Lei, 2026-08-29, from the record — #2013 invariant 7 first sentence
"the tool REFUSES a supersede that supplies no resolution", and his on-record
correction quoted in #2343: "REFUSE with a pointer"): a plain `engram_supersede`
or `engram_retract` on a node that already carries a pending cascade marker is
REFUSED, and the refusal hands back the exact `engram_cascade_resolve` call for
that node. The agent then adjudicates through the verb; a `superseded`
resolution there is a PARTIAL RESOLUTION of the existing event — invariant 7's
second sentence — never a new event.

An earlier version of this case asserted the plain supersede SUCCEEDS as a
partial resolution. That was wrong: it read invariant 7's "partial resolution"
phrase, which describes the resolution-supersede THROUGH the verb, as if it
described the plain verb. Two reviews checked the body against the docstring;
neither checked the docstring against the spec. Kept here so the next reader
does not repeat it.

SETUP    converging_diamonds; supersede A → A2 (C, D, E each carry ONE marker
         from event 1); mint C2 from A2 and B.

CD-08a   ACTIONS  plain `engram_supersede` C → C2.
         EXPECT   refused; the message names engram_cascade_resolve; C's
                  marker still pending; E still exactly one pending event;
                  no edge written; graph unchanged.
         STATUS   green since #2343 (the refusal). Before it, the plain
                  supersede succeeded AND opened a second event on E.

CD-08b   ACTIONS  the correct route: resolve(C, "superseded", replacement=C2).
         EXPECT   C's marker resolved as `superseded`; C non-current; E keeps
                  exactly ONE pending event (no second event); conformance
                  clean. Passes on dev today (measured 2026-08-29).

Claims:  #2013 invariant 7 (both sentences), §10.2 marked-vs-unmarked, #2343.
"""

from _dsl import (
    cascade_playbox, expect_ok, expect_refused, expect_pending, expect_resolved,
    expect_clean, node_count, expect_node_count,
)


def _setup(box):
    expect_ok(box.supersede(box["A"], box["A2"], reason="CD-08 event 1"))
    expect_pending(box, "C", events=1, stage="setup")
    expect_pending(box, "E", events=1, stage="setup")
    return box.node("C2", kind="derivation", supports=[box["A2"], box["B"]], claim="C2: C on A2.")


def test_cd08a_plain_supersede_of_marked_node_is_refused_with_pointer():
    with cascade_playbox("converging_diamonds") as box:
        # ---- SETUP -------------------------------------------------------
        C2 = _setup(box)
        edges_before = box.edges()
        nodes_before = node_count(box)

        # ---- ACTIONS -----------------------------------------------------
        r = box.supersede(box["C"], C2, reason="CD-08a plain supersede of marked C")

        # ---- EXPECT ------------------------------------------------------
        expect_refused(r, "plain supersede of marked C", mentions="cascade_resolve")
        expect_pending(box, "C", events=1, stage="after refusal (marker untouched)")
        expect_pending(box, "E", events=1, stage="after refusal (no second event)")
        assert box.edges() == edges_before, "a refused supersede must write no edge"
        expect_node_count(box, nodes_before, stage="after refusal")
        expect_clean(box, stage="after refusal")


def test_cd08b_resolve_verb_supersede_is_a_partial_resolution_of_the_existing_event():
    with cascade_playbox("converging_diamonds") as box:
        # ---- SETUP -------------------------------------------------------
        C2 = _setup(box)

        # ---- ACTIONS -----------------------------------------------------
        r = expect_ok(
            box.resolve(box["C"], "superseded", replacement=C2, note="CD-08b via the verb"),
            "resolve(C, superseded, C2)",
        )

        # ---- EXPECT ------------------------------------------------------
        assert r["resolved_marker_count"] == 1, r
        expect_resolved(box, "C", kind="superseded", stage="after resolve")
        conn = box._core._get_db()
        try:
            cur = conn.execute("SELECT is_current FROM nodes WHERE id = ?", (box.id_of(box["C"]),)).fetchone()[0]
        finally:
            conn.close()
        assert cur == 0, "a superseded resolution must retire C"
        expect_pending(box, "E", events=1, stage="after resolve (invariant 7: no second event)")
        expect_clean(box, stage="after resolve")
