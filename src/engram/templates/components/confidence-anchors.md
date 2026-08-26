---
id: confidence-anchors
kind: reference
harnesses: all
surfaces: [CLAUDE.md]
anchor: "Confidence calibration anchors"
order: 10
merge: replace
version: 1
---
## Confidence calibration anchors

Confidence is structurally determined, not free-form. Labels are type-relative because each type derives from a different determinant — observations from `quote_type` + `source_class`, derivations from `reasoning_type` + premise propagation, conjectures by-definition speculative, axioms terminal. SessionStart hook injects the current graph's actual distribution; these labels are the semantic anchor for what those numbers MEAN.

**Observations** (right-skewed because most filing is source-attested):
- `0.95` — hard_data: measured / observed event / verbatim verified quote
- `0.85` — official_statement: docs / policy / schema (also: typical ob)
- `0.70` — attributed_analysis: academic / expert claim, source-attested
- `0.40` — personal_communication: conversation / inferred meaning
- `0.35` — editorial: opinion / argument
- `× 0.95` discount for `source_class=introspective` (my own prior output); `→ 0.85` override for `source_class=user_stated` ({{USER_NAME}} as conduit, NOT as primary measurer)

**Derivations** (flatter; propagation from premises):
- `0.92+` inductive_generalization with broad case support
- `0.75` deductive_modus_ponens (≤ min premise confidence)
- `0.55` abductive_best_explanation — tentative best fit
- `0.45` inductive_analogy — structural similarity only
- `0.25` speculative / open-question recommendation

**Other types**: conjectures cap [0.10, 0.60], default 0.40, hard-clamped at creation → promote to derivation when stronger; axioms always 1.0 by type. Lessons/cornerstones carry no confidence (non-claim-bearing types, per engram-alpha#2252).

These numbers are **computed, not assigned** — an observation maps from its `quote_type`, a derivation propagates from its `reasoning_type` + premises. So when a *returned* confidence feels wrong against these anchors (too high for a speculative claim, too low for a strongly-supported one), read it as a signal to re-check the **inputs I chose**, not to override the output: almost always it's the wrong `reasoning_type` or a weak / mis-cited premise (derivations especially — observations are a near-mechanical `quote_type` map). The friction is the calibration mechanism working; the fix is upstream, in the inputs, not the number.

