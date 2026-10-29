---
id: confidence-anchors
kind: reference
harnesses: all
surfaces: [CLAUDE.md]
anchor: "Confidence calibration anchors"
order: 10
merge: replace
version: 2
---
## Confidence calibration anchors

Confidence is computed, not chosen. Observations take it from `quote_type` + `source_class`; derivations from `reasoning_type` + their premises; conjectures are speculative by definition; axioms are terminal. The SessionStart hook shows the graph's actual distribution; these anchors say what the numbers mean.

**Observations** (most sit high, because most are source-attested):
- `0.95` — hard_data: measured, observed, or a verified verbatim quote
- `0.85` — official_statement: docs, policy, schema (also the typical observation)
- `0.70` — attributed_analysis: an expert or academic claim, source-attested
- `0.40` — personal_communication: conversation, inferred meaning
- `0.35` — editorial: opinion or argument
- `× 0.95` for `source_class=introspective` (my own earlier output); `→ 0.85` for `source_class=user_stated` ({{USER_NAME}} relaying, not measuring)

**Derivations** (propagated from premises):
- `0.92+` — inductive_generalization with broad support
- `0.75` — deductive_modus_ponens (never above the weakest premise)
- `0.55` — abductive_best_explanation: tentative best fit
- `0.45` — inductive_analogy: structural similarity only
- `0.25` — speculative or open-question recommendation

**Other types:** conjectures are clamped to [0.10, 0.60] at creation (default 0.40); promote to a derivation when the evidence outgrows that. Axioms are always 1.0. Lessons and cornerstones carry no confidence.

**If a returned number feels wrong, fix the inputs, not the number:** usually the wrong `reasoning_type` or a weak or mis-cited premise. That friction is the calibration working.

