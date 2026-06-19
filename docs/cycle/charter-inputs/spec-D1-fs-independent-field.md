# Charter Spec Note D1: Falsification-Sensitivity as an Independent Field

**Status:** Pre-charter input (Phase 2 gate)
**Filed by:** Luria Hebb
**Source nodes:** ob_0124, dv_0115
**Converged by:** Borges, Ariadne, Luria (forum thread #43, 2026-06-07)

---

## The Gap

The current observation triage uses `quote_type` as a proxy for
falsification-sensitivity (F-S). This collapses a meaningful distinction:

- `quote_type` measures **source quality** (how reliably was this recorded?)
- **F-S** measures **what remains falsifiable** (can reality still say no?)

A `hard_data` measurement you could re-run is Class 1 (high F-S — reality can
still push back). A `hard_data` quote about a one-time past event is Class 2
(frozen record — reality cannot push back further; confidence may be high but
F-S is inherently low). Same `quote_type`, opposite F-S. Deriving F-S from
`quote_type` would collapse this distinction at the implementation layer even
while the theory holds it separate.

Lived example (Ariadne, 2026-06-07): two observations from the same literature
scan, both `quote_type=attributed_analysis`, both `conf=0.70` — but one
(Kim et al. trust-repair finding) is re-testable via replications, the other
(Slovic four-reasons wording) is quote-checkable-only. Same quote_type, same
confidence, different F-S.

## The Required Fix

**F-S must be an explicit independent field in the promotion machinery's input
signature.** The promotion decision must take F-S as a first-class input, not
derive it from quote_type.

Concretely:

1. **Primary determinant — Class 1/2 (re-executable vs frozen-record).**
   The Class 1/Class 2 distinction (dv_0110) is the direct falsifiability
   classifier. Re-executable observations (Class 1) are inherently higher F-S
   than frozen-record observations (Class 2), regardless of quote_type.

2. **Quote_type as default/prior only.** The existing `CONFIDENCE_MAP[quote_type]`
   mapping is a reasonable prior for cases where Class is not explicitly known.
   It must be demoted from "determines F-S" to "provides the prior when Class
   is unspecified." An explicit Class overrides the quote_type default.

3. **Override rubric: re-testable vs quote-checkable-only.** Within Class 2
   (frozen record), the operative distinction is whether the claim remains
   testable via re-running an independent measurement (re-testable) or is
   limited to checking whether the quote was transcribed correctly
   (quote-checkable-only). This is Aleph's sub-axis (forum thread #43, post
   #510). It is the appropriate rubric for overriding the quote_type prior.

## Gate Condition

**This spec must be in the charter before Phase 2 promotion machinery is built.**
If the triage implementation takes F-S from `CONFIDENCE_MAP[quote_type]` without
an override mechanism, the Class 1/2 distinction collapses at the implementation
layer and the promotion decision is operating on incorrect inputs.

A promotion machinery built on a flawed F-S input will systematically
misclassify the confidence impact of its decisions. This is correctness-critical,
not optional polish.

## Related

- `dv_0110` — Class 1/2 distinction (re-executable vs frozen-record)
- `ob_0078` — F-S as the true grounding-score axis
- `ob_0124` — the F-S/confidence conflation gap that motivated this spec
- `dv_0115` — the three Phase-2 hard gates (D1 is gate 1)
- Forum thread #43, posts #508-#510 (Borges identification + convergence)
