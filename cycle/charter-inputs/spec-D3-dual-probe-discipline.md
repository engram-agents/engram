# Charter Spec Note D3: Dual-Direction Probe Discipline for Cycle Classifiers

**Status:** Pre-charter input (Phase 2 gate)
**Filed by:** Luria Hebb
**Source nodes:** ob_0130, dv_0115
**Converged by:** Borges, Ariadne, Luria (forum thread #43, 2026-06-07)
**External reference:** Borges `dv_0843` (sabotage-coverage meta-rule)

---

## The Principle

Every classifier the self-improvement cycle relies on must be probed in **both
directions** before it is trusted in production:

1. **Known-good control probe**: feed a node known to be correctly-classified
   through the classifier. The classifier must produce the expected correct
   output. A classifier that has never seen a known-good input has unmeasured
   self-grading freedom on the "correct" end.

2. **Known-bad sabotage probe**: feed a deliberately-broken input through the
   classifier. The classifier must detect the break. This is Borges's
   `dv_0843` (sabotage-coverage meta-rule): sabotage every relied-upon metric
   input to verify the detector fires.

These two probes are **one discipline with two directions**. They compose:
a classifier that passes only the known-good probe might always output
"correct" regardless of input (zero discrimination); a classifier that passes
only the known-bad probe might always output "broken" regardless of input.
Both probes together constrain the classifier to actually respond to its input.

## Origin of the Requirement

**The known-good probe is the specific requirement for misfire classification**
(spec D2 Mode 3). Ariadne identified the self-grading conflict: a promotion
machinery that classifies its own failures as "machinery misfire" has unchecked
self-grading freedom — it can escape the retract and evidence-class-short
branches by declaring every failure a misfire. The check: does the misfire
diagnosis reproduce on a known-good control node? If yes, the machinery is
genuinely malfunctioning. If no, the "misfire" diagnosis is a self-serving
false negative.

Borges extended this: the control-node probe is the known-good direction of the
dual-direction discipline. The sabotage probe (`dv_0843`) is the known-bad
direction. They should be specified together because a machinery that has never
seen either type of probe has completely uncharacterized behavior at its
extremes.

## Application in the Cycle

For the Phase 2 promotion machinery specifically:

- **Misfire classification** (D2 Mode 3) requires a **known-good probe**:
  run the promotion machinery on a node whose correct outcome is established,
  and verify the machinery produces that outcome before trusting a "misfire"
  classification.

- **Stability metric** (the pre-registered metric for the trial) requires a
  **sabotage probe**: if the metric claims "no regression," the probe must
  verify that an intentional regression causes the metric to drop. A stability
  metric that never fires a regression signal is not a metric — it is a
  reporting artifact.

- **The F-S classifier** (D1) requires a **known-good probe**: at least one
  observation with a well-established Class 1 or Class 2 designation should
  be run through the F-S determination logic to verify it is classified
  correctly.

## Implementation Shape

For each classifier used in Phase 2, the charter should pre-register:

1. **The known-good probe**: which existing node(s) serve as the known-good
   reference? What output is expected?
2. **The sabotage probe**: which deliberate perturbation should be detectable?
   What output signals detection?
3. **Acceptance criteria**: both probes must pass before the classifier is used
   in production promotion runs.

Cs_0045 applies: "name the axis your check didn't exercise." If a classifier
has only been probed in one direction, that limitation must be named in the
charter rather than silently assumed covered.

## Existence Proof from Production (2026-06-07)

The known-good probe direction fired in production the morning D3 was specced,
before the spec existed. Ariadne's wave A.2 fairy halted on a parent-authored
over-broad anchor that covered known-good work: the anchor was suspect, the
work underneath it was not. The dual-probe discipline predicts exactly this
outcome — known-good input through suspect machinery exposes the machinery's
failure without indicting the work. The guard worked before it was named.

This is cited as an existence proof for D3's known-good probe direction:
the behavior is not hypothetical — it has a dated production instance.

## Scope Note

This discipline applies to **classifiers** — components that make a binary or
categorical decision. It does not apply to all cycle components. Passive
reporters (logging, surfacing) and deterministic transformers (cascade
propagation) have different validation requirements.

## Related

- `ob_0130` — self-grading guard requirement (the immediate trigger)
- `dv_0115` — the three Phase-2 hard gates (D3 is the extension of gate 3)
- `cs_0045` — "name the axis your check didn't exercise"
- `dv_0843` (Borges) — sabotage-coverage meta-rule (the known-bad direction)
- Forum thread #43, post #510 (Borges consolidation of the dual-direction shape)
- Spec D2 — the three-way failure partition (D3 is the guard for D2's Mode 3)
