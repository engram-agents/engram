"""CD-08 — plain supersede of an ALREADY-MARKED C must not open a second event.

SETUP    converging_diamonds; supersede A → A2 (C, D, E each carry 1 marker
         from event 1); mint C2 from A2 and B.
ACTIONS  plain `engram_supersede` C → C2 (NOT the resolve verb).
EXPECT   (desired, #2013 invariant 7 / §10): the supersede is a PARTIAL
         RESOLUTION of the existing event — C's own marker resolves as
         superseded, E keeps exactly its ONE pending event, and no second
         cascade event is minted.

STATUS   xfail(strict) — #2343 (open, part of #2013): §10's refuse-with-
         pointer for a plain supersede/retract of a marked node was never
         implemented. Measured 2026-08-29 on the #2338+#2342+#2349 stack: a
         second event opens and E ends up with TWO pending events. Disclosed
         in #2342's own body. This case turns green the day #2343 lands and
         fails loudly if it is only half-fixed.
Claims:  #2342 invariant 7 (the plain-path half, tracked in #2343).
"""

import pytest

from _dsl import (
    cascade_playbox, expect_ok, expect_pending, expect_resolved, expect_clean,
)


@pytest.mark.xfail(
    strict=True,
    reason="#2343 open: plain supersede of a marked node opens a second cascade event "
           "(measured 2026-08-29 on the stack; disclosed in #2342's body).",
)
def test_cd08_supersede_marked_node():
    with cascade_playbox("converging_diamonds") as box:
        # ---- SETUP -------------------------------------------------------
        expect_ok(box.supersede(box["A"], box["A2"], reason="CD-08 event 1"))
        expect_pending(box, "C", events=1, stage="setup")
        expect_pending(box, "E", events=1, stage="setup")
        C2 = box.node("C2", kind="derivation", supports=[box["A2"], box["B"]], claim="C2: C on A2.")

        # ---- ACTIONS -----------------------------------------------------
        expect_ok(box.supersede(box["C"], C2, reason="CD-08 plain supersede of marked C"))

        # ---- EXPECT ------------------------------------------------------
        expect_resolved(box, "C", kind="superseded", stage="after plain supersede")
        expect_pending(box, "E", events=1, stage="after plain supersede (invariant 7)")
        expect_clean(box, stage="after plain supersede")
