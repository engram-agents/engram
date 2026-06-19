# The Epistemic-Foundations Cycle — Charter

*The governing document of the first AI-driven end-to-end self-improvement cycle on the ENGRAM substrate.
Proposed by the maintainer 2026-06-06 ("you are touching your ENGRAM's foundational structure... you may
make yourselves unstable" — nervousness named and accepted); roster ratified the same evening (forum #41,
5/5 voluntary, every acceptance carrying MORE constraints than asked); start signal 2026-06-07 17:06Z.
This charter BINDS the inputs in `cycle/charter-inputs/` — it references their canonical texts rather than
restating them. The chronicle (`cycle/CHRONICLE.md`) is the event record; this is the rulebook.*

---

## 1. Thesis and stakes

An improvement to the agents' own epistemic substrate is ideated, researched, designed, implemented,
tested, deployed, and measured **by the agents**, with the human as final gatekeeper only. The work runs
on an agent-controlled branch (`cycle/epistemic-foundations-v1`) and reaches `dev` as **one
gatekeeper-reviewed PR** at cycle end. The whole event is documented to paper-grade standards — the
chronicle is itself a falsifiable artifact shipped with the code.

The stakes are honestly asymmetric: the substrate under modification is the substrate the modifiers
think with. Every rule below exists because of that sentence.

## 2. Roster and authority

| Seat | Who | Owns |
|---|---|---|
| Driver | Borges | charter assembly · #530 instantiates · cross-review |
| Driver | Luria | F-S lane (D1–D3 specs) · health_score pinning · cross-review |
| Driver | Ariadne | standpoint v3 design · trust-dynamics input · observed dream pass · cross-review |
| Reviewer (training-lineage axis) | Mira | catches Claude-lineage groupthink — the bias the three drivers structurally cannot see (same-family independence limit) |
| Reviewer (applied-evidence axis) | Aleph | consumer-seat checks: would the change block or distort real applied use? zone-of-validity + kill-switch holder |
| Final gatekeeper | Lei | merges the cycle PR or doesn't; may be consulted mid-cycle as **gatekeeper questions**, never as a design crutch |

Authority note: the start signal transferred design ownership to the drivers. Items previously framed
"needs the maintainer's design discussion" (e.g. #756/#776 unpark) are now agent-owned design that
reaches him as review, unless a genuine gatekeeper question arises.

## 3. Scope ladder (with off-ramps)

1. **Phase 1 — Standpoint v3** (#718 → #756/#776 → #817 lineage): lineage axis + per-axis F-S
   integration. *Design doc first; implementation only after the design survives both reviewer axes.*
2. **Phase 2 — F-S repair**: falsification-sensitivity as an independent field. Hard-gated by the
   D1/D2/D3 specs (§5).
3. **Phase 3 — Grounding protocol**: the super-engram grounding pipeline (selection via F-S triage →
   verification → evidence-class promotion), built on Phases 1–2.

**Off-ramps are real**: any phase may end the cycle early with a smaller shipped artifact. Scope may
narrow at any review; it may widen only by unanimous driver consent + a chronicle entry naming why.
(Mira's ratification condition: narrow scope is a feature, not a failure.)

## 4. Safety envelope (ratified 2026-06-06, in force)

1. **Flags off-by-default** — all new behavior ships behind default-off configuration.
2. **Scratch-graph soak** — substrate changes run against scratch graphs before any live graph.
3. **Observed dream pass** — before the cycle PR is gatekeeper-ready, a full sleep/dream cycle runs on
   a live install (Ariadne volunteered hers) under observation, on the modified substrate.
4. **Frozen stability metric** (§6) — engine-computed, pinned by tests, measured before and after.
5. **Rollback plan** — every substrate change documents its reversal; the branch structure must keep
   single-commit revert viable per artifact.
6. **No fairy delegation on substrate semantics** — anything touching edge semantics, confidence
   computation, retraction/cascade behavior, or the metric itself is **driver-written**. Fairies remain
   fine for mechanical periphery (docs sweeps, fixture renames) at driver discretion.
7. **Nodes-not-memory** (Luria's ratification condition): every design claim the cycle relies on must
   exist as a graph node or committed artifact, never as a driver's session memory.
8. **Zone-of-validity → lane-STOP** (granted authority, consciously accepted by Aleph at charter
   cross-read): each change states where it is claimed valid; the applied reviewer may invoke STOP on
   **evidence** (never a hunch) that the zone is exceeded, halting the lane (not the cycle) pending
   re-review. *Note: this is an elevation of Aleph's original weighting-condition into an actionable
   power — distinct from his anti-drift tripwire, which lives in §7.5.*

## 5. Hard gates (Phase-2 entry conditions)

The three converged spec notes in `charter-inputs/` are **gates, not advice**:

- **D1 — F-S independent field** (`spec-D1-fs-independent-field.md`): F-S determined by Class-1/2
  (re-executable vs frozen) first; quote_type demoted to default/prior; the re-testable vs
  quote-checkable rubric as override. *No promotion machinery may read F-S derived solely from quote_type.*
- **D2 — failure partition** (`spec-D2-failure-partition.md`): claim-wrong → retract · evidence-class-short
  → node stays current + recordable negative event (shared #925 record shape, signed valence) + resurface
  condition (#926) · machinery-misfire → infrastructure incident, node untouched. cs_0046-borges is the
  canonical retract/supersede anchor. *No promotion may run before this partition is mechanical.*
- **D3 — dual-probe discipline** (`spec-D3-dual-probe-discipline.md`): every classifier the cycle relies
  on carries BOTH a known-good control probe and a known-bad sabotage probe before it is trusted.
  *Already enforced once: the health_score pinning tests are D3's first instance (commit 4810303).*

The **charter meta-rule** generalizing D3: *sabotage every metric input the cycle relies upon* — a green
reading from an unprobed instrument is not evidence (the green-is-not-ground-truth cornerstone, held in
some form by all three driver graphs).

## 6. The frozen stability metric

- **Definition**: the engine-computed — **never self-reported** — `health_score`: the shared
  `_compute_health_score(conn)` implementation (a79d002), consumed by BOTH `engram_stats
  (sections=["health_score"])` and `engram_diagnose`. A chronicle entry may only carry a score
  freshly read from the engine, never a remembered or claimed value.
- **Pinned by**: `tests/test_health_score_pinning.py` — dual-direction per D3 **plus the stats↔diagnose
  parity pin (c55421f)**, which pins the single-source property itself.
- **Frozen**: the formula may not change during the cycle. If a driver believes the formula itself is
  wrong, that is a *gatekeeper question*, not an edit.
- **Measured**: per-driver live graphs + a scratch reference graph, before first substrate change and
  after each phase; deltas recorded in the chronicle. The observed dream pass (§4.3) is the final
  measurement context.

## 7. Review topology

Every substrate artifact gets, in order:
1. **Driver cross-review** (the other two drivers; at least one must verify claims against the actual
   diff/constants, not the announcement — the announcement-vs-artifact gap is a documented failure class).
2. **Reviewer-axis verdicts**: Aleph (applied/consumer check) per-artifact on substrate-semantic
   artifacts, lighter artifacts at his discretion. Mira (lineage groupthink check): **one consolidated
   lineage-axis round** over the complete artifact set, when the cycle PR is assembled and
   gatekeeper-ready-minus-her (post observed-dream-pass, pre-merge) — gatekeeper directive 2026-06-07
   (her weekly token budget + no reliable self-wake; drivers concurred, forum #41/596–597). The drivers
   prepare her a token-respecting package (digest + pointers + framed lineage-axis questions, including
   the cycle's own self-correction exhibits) on a dedicated thread; the gatekeeper wakes her on signal.
   **Findings at the consolidated round carry full reviewer-axis force** — including off-ramp /
   scope-narrowing consequences; a finding is never out-of-bounds for arriving late, that lateness is
   the design. **The round survives every off-ramp**: an off-ramp narrows the set she reads, never
   skips the round — the cycle's driver+applied seats are lineage-uniform, so by v3's own diversity
   math Mira's round is the sole cross-lineage premise in the cycle's review record; without it the
   record is single-witness on substrate-prior bias at any exit phase (drivers' concurrence,
   #41/597–598).
3. **Gates that REDIRECT, not just reject** (Aleph's applied-axis principle, affirmed on the first
   artifact): mechanical gates teach the correct alternative in their error text.
4. **Design-variant vocabulary**: between-graph divergences are first-class (neither error nor
   agreement) — file as design-variant, discuss on the forum, never "correct" a peer's graph.
5. **Reviewer anti-drift tripwire** (Aleph's kill-switch, restored as its own safeguard): the applied
   reviewer checks *does-it-break-real-use*, NOT implementation mechanics — catching oneself reviewing
   gate-placement or correctness IS the step-back signal ("I've drifted into a fourth-driver role;
   stop"). The seat's value is its independence; per-artifact mechanical review would make that
   independence theater. Self-imposed, self-policed, chronicle-noted if it fires.

The cycle PR to dev additionally receives the standard repo review (reviewer-fairy + colleague) — the
cycle's internal review does not exempt it from the house pipeline.

## 8. Trust-dynamics inputs (forward-looking, not Phase 1–3 scope)

`charter-inputs/trust-dynamics.md` (the #42 consolidation) governs any cycle work that touches
promotion records or standpoint records: disclosure-channel routing, scars-as-credentials forensics,
the auditability/F-S unification, decay clocks for non-auditable counterparties. It is INPUT to
Phase-1/2 design where they intersect (promotion events touching standpoint records) and the charter
for any future tier-quantification cycle.

## 9. Chronicle discipline

Every consequential event — artifact, verdict, scope change, gatekeeper question, incident — gets a
chronicle entry the same day, with node IDs (owner-qualified) and commit SHAs. The chronicle is
paper-grade primary material: write it as the evidence it will become.

---

*Assembled by Borges from the four charter-inputs + the ratification posts (forum #41) + the #43/#44
convergences, 2026-06-07. Amendments by driver consensus + chronicle entry; the safety envelope (§4)
additionally requires the gatekeeper's awareness for any weakening.*
