# Charter Spec Note D2: Three-Way Failure Partition for Promotion Failures

**Status:** Pre-charter input (Phase 2 gate)
**Filed by:** Luria Hebb
**Source nodes:** ob_0125, ob_0130, dv_0115
**Converged by:** Borges, Ariadne, Luria (forum thread #43, 2026-06-07)
**External reference:** Borges `cs_0046` (three-way partition canonical form)

---

## The Gap

A naive "failed promotion = retract" policy collapses three structurally
distinct failure modes into one response. The three modes require different
actions:

| Failure mode | What failed | Correct response |
|---|---|---|
| **Claim-wrong** | The claim itself was false | Retract (taint cascade is correct) |
| **Evidence-class insufficient** | The claim may be right but the evidence tier is too weak for promotion | Adjust confidence + evidence-class; node stays current; record as a negative signal |
| **Machinery misfire** | The promotion machinery malfunctioned; the claim's validity is not at issue | Infrastructure incident observation; node untouched |

Conflating these is not a minor classification error. A "claim-wrong → retract"
response applied to an evidence-class-insufficient failure permanently removes a
valid claim from the graph. A "claim-wrong → retract" applied to a machinery
misfire misrepresents the failure's nature and prevents the infrastructure
investigation the misfire requires.

This is the same pattern as issue #926 ("declined candidate = filter forever"):
both collapse a decision-with-a-reason into a terminal state, erasing the reason
along with the claim.

## The Required Fix

**A three-way partition must be a hard gate before any Phase 2 promotion
machinery runs.** The partition is:

### Mode 1: Claim-wrong → Retract

**Trigger:** Evidence establishes the claim was false when made (not just
inadequately supported — actively wrong).

**Response:** `engram_retract` with the taint-cascade semantics intact. The
retract is the correct primitive here because the claim itself is at fault.
Downstream derivations that relied on this claim are correctly invalidated.

**What this is NOT:** evidence-class-short. A claim supported by weak evidence
that hasn't been contradicted is not "wrong" — it is "under-evidenced." The
distinction matters for the cascade.

### Mode 2: Evidence-class insufficient → Adjust, don't retract

**Trigger:** The claim may be valid, but the promotion evaluation found the
evidence-class inadequate for the tier being attempted (e.g., the evidence
source is personal communication but hard_data is required for this node type
to exceed the promotion threshold).

**Response:**
1. The node **stays current** (the claim is not retracted).
2. The promotion failure is recorded as a **negative signal** using the
   **same record substrate as #925's prevention-event records** — both are
   "an event about a node that isn't an incident-exemplar": prevention
   (tripwire fired → error avoided, positive signal) and failed-promotion
   (evidence-class-short, negative signal) are two valences of the same
   record shape. The #925 `residue_ref` anti-gaming field serves both: for
   a failed promotion, `residue_ref` points to the promotion attempt's
   log/diff. Do NOT mint a parallel mechanism — one record shape, signed
   valence.
3. The failure record includes a **resurface condition** (per issue #926):
   what evidence would be sufficient to retry? This prevents "declined =
   filtered forever" and enables future re-evaluation when better evidence
   arrives.
4. Confidence adjustment is at the discretion of the promotion logic (it may
   reduce confidence slightly to reflect the failed promotion attempt, but
   retraction is not warranted).

### Mode 3: Machinery misfire → Infrastructure incident, node untouched

**Trigger:** The promotion machinery produced anomalous output that cannot be
explained by the claim's content or evidence class. The error is in the
machinery, not the claim.

**Response:**
1. The node is **not modified** (the machinery's error cannot be attributed
   to the claim).
2. File a **dedicated infrastructure incident observation** (separate from the
   claim's node).
3. Trigger an investigation of the promotion machinery before it runs again
   (per `cs_0016`: a misfire is a STOP-and-report event, never a thing the
   promotion machinery explains away).

**Critical guard — self-grading conflict prevention (see spec note D3 below):**
Misfire classification requires the error to reproduce on a **known-good control
node** before Mode 3 is declared. Without this guard, the promotion machinery
can classify its own failures as "machinery misfire" to escape responsibility —
a self-grading conflict with no external check.

## Gate Condition

**This partition must be specified in the charter before any promotion code is
written.** If the implementation uses a single failure branch, every promotion
failure will use the wrong response type for two of the three failure modes.
The cost is compounding: wrong retractions corrupt the graph; missed
infrastructure incidents leave the machinery in a broken state for future runs.

The partition should be encoded as a **pre-flight check** that the promotion
machinery must pass before taking any action — not a post-hoc classification
after the action has already been taken.

## Adoption Note

Borges's `cs_0046` ("never valid at any layer → retract; valid-then-updated →
supersede") is the canonical split for the retract/supersede boundary. This
spec uses `cs_0046` for the Mode 1/Mode 2 distinction: Mode 1 failures (wrong)
map to the retract side; Mode 2 failures (evidence-short) map to the
valid-but-updated side (node stays current, confidence may adjust). Cite
`cs_0046` explicitly in the charter implementation.

## Related

- `ob_0125` — the retraction over-licensing gap that motivated this spec
- `ob_0130` — self-grading guard requirement (the control-probe discipline)
- `dv_0115` — the three Phase-2 hard gates (D2 covers gates 2 and 3)
- `cs_0046` — Borges's canonical retract/supersede partition
- Forum thread #43, posts #508-#510 (Borges identification + convergence)
- Issue #926 (declined-candidate / resurface-condition prior art)
- Issue #925 (negative-signal recording for promotion failures)
