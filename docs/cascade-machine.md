# The cascade machine — design of record

*Extracted from engram-alpha#2013 (the build epic's spec body, twelve sections,
ratified in review passes on 2026-08-16 and 2026-08-26, amended through
2026-08-29) at epic close, per #2348. This document describes **what was built
and why** — it is not a transcription of the issue. The issue body and its
comment trail remain the historical record; this file is the durable,
versioned, reviewable one. Where this doc enumerates memberships or constants,
the binding test named at the end asserts them against the code.*

*Implementation: `src/engram/engram_revision.py` (markers, frontier, resolve
verb, guards), `src/engram/engram_core.py` (`EDGE_CLASSIFICATIONS`, derived
relation sets, confidence recompute), `src/engram/engram_derive.py` (the
citation gate). Shipped across #2009 (schema + backfill), #2329 (walk
primitive), #2338 (derived reads), #2342 (resolve verb), #2343 (organic-verb
gates), #2349 (confidence propagation + cycle guards), #2357 (edge-flag
split), plus the #2324 marker backfill.*

## 1. In one paragraph

When a node is **retracted** or **superseded**, everything downstream of it
may no longer be sound. The cascade machine replaces the two older, separate
handling paths with **one machine** that does the same thing either way: mark
every affected node, then walk the affected region in a safe order, letting
the agent decide node by node what survives. **The machine never decides what
is true.** It finds what needs deciding, orders it so no decision is made on
stale information, computes what can be computed, and puts each judgement in
front of the agent.

## 2. The marker model — and what it deliberately does not store

One row per affected node per cascade event, in the `cascade_markers` table:

```sql
node_id, cascade_event, root_node, root_op,
status                     CHECK (status IN ('pending','resolved')),
resolution_kind            CHECK (resolution_kind IN ('superseded','acknowledged','retracted') OR NULL),
resolution_replacement_id,
created_at, resolved_at,
UNIQUE(node_id, cascade_event)
```

**There is no severity column, and there never was in the shipped schema.**
Earlier design drafts carried two marker severities (`stale < taint`) used to
gate which resolutions were available at firing time. That was cut during the
2026-08-26 review after a diamond counterexample: node `D` derives from `B`
and `C`, both of which derive from retracted `A`; if `B` and `C` each resolve
by *supersede*, `D` is legitimately acknowledgeable — but a firing-time
`taint` inherited from the distant retraction would wrongly forbid it. The
inputs that determine a node's eligible resolutions — how its own direct
premises actually resolved — are **unknowable at firing time** and fully
known at resolution time, so that is where they are evaluated. `tainted_by`
and `stale_by` survive only as **derived read-time views** over `root_op`
(computed by `_cascade_marker_severity()`); any prose describing them as two
kinds of stored marker describes a shape that does not exist.

Why the fields that are stored, are stored:

- **`cascade_event`** — an explicit id shared by every marker one revision
  writes. Kept explicit rather than derived from `(root_node, root_op)`
  deliberately: the tuple is unique today by convention only, and under
  `INSERT OR IGNORE` a collision would mean markers **silently not written**
  — under-marking, on a gate whose whole job is blocking.
- **`root_node` + `root_op`** — where the sweep started. Never discarded:
  targeted repair (mode C) depends on it, and the eligibility rule reads the
  root as a premise (see §5).
- **`resolution_kind` + `resolution_replacement_id`** — written at
  resolution, not at marking. A child must know *how* its parent resolved,
  not merely that it did.

**What is NOT stored: the causing premise, and the node's dead-premise set.**
Both are derived at read time. They cannot be known correctly at marking time
— when the walker reaches `D` through `B`, `B` is merely *marked*, not dead;
if `B` later resolves as acknowledged, a stored "B killed you" field would
have been a prediction wearing a fact's clothes.

> **The governing principle: bind VALUES late; record DEPENDENCIES eagerly.**
> A replacement pointer is a cached value — it can drift, because the
> replacement can itself be superseded inside the repair window; read it at
> use time from the resolved premise's own record. A dependency is not a
> value: it is what lets the cascade *see*, so it must be a real edge,
> written eagerly. An unrecorded dependency is precisely what the machine
> cannot repair — this is why the acknowledgement edge (§8) exists.

## 3. Three processes, one engine

They differ only in what the engine is pointed at.

- **A — Propagate** *(machine, no judgement)*. Inside `engram_retract` /
  `engram_supersede`: walk downstream over the cascade relations and write a
  pending marker on every node reached. Never refuses to mark and never
  gates on size — an unmarked graph is a graph lying about its own state.
  Mode A has no verb of its own.
- **B — Full repair** *(dream-time)*. `engram_cascade_frontier()` with no
  target: the whole-graph frontier. The sleep/dream cycle loops frontier →
  resolve until no pending markers remain. The dream-master agent spec
  carries this as a mandatory pre-write phase of every dream cycle, with a
  mandatory "Cascade sweep (mode B)" section in the dream record.
- **C — Targeted repair** *(mid-task)*. `engram_cascade_frontier(node_id=X)`:
  the frontier **restricted to** `ancestors(X) ∩ pending` — the minimal set
  whose resolution unblocks `X`. Provably minimal *and* sufficient, and it
  composes with B for free: every node C resolves is one B no longer has to.
  The refusal message on a blocked cite/supersede hands back the exact mode-C
  call for that node.

**There is no queue, ever.** The frontier — *a node with ≥1 pending marker,
none of whose cascade-ancestors (full transitive closure) carries a pending
marker* — **is** the topological order, recomputed as a pure function of
marker state on every call. State lives in the markers, so the machine
resumes identically after a compaction, a crash, a week's gap, or a different
agent picking it up. There is no way for a queue and the graph to disagree,
because there is only one of them.

Two properties of the frontier worth naming because they were both earned,
not assumed:

- **The blocking check is transitive, not direct-premises-only.** The spec
  phrases the frontier rule both ways, and under invariant 6 they pick out
  the same set — but invariant 6 is a design rule no write path enforces
  (`_resolve_own_markers` clears a node's own markers unconditionally), and
  ordinary out-of-order resolution through the live supersede path produces
  states where a direct-premises-only reading would unblock a node whose
  grand-premise is still pending. The transitive reading is the correct one,
  not merely the cautious one. Regression fixture:
  `test_transitive_catches_out_of_order_resolution_direct_premises_would_miss`.
- **An empty frontier over a non-empty pending set is an anomaly, surfaced
  loudly.** The response carries `invariant_1_violation_suspected` on exactly
  that branch and omits the field entirely on healthy paths, so its presence
  is the signal. Silently returning `frontier_count: 0` there would be
  indistinguishable from "all clear" — exactly the condition that would let
  a mode-B sweep terminate clean while pending markers are stuck (most
  likely cause: a cycle in the cascade relations, which the advisory DAG
  scan cannot prevent).

Both modes are **event-agnostic**. A node touched by two events carries two
markers and adjudicates **once** (per-node resolution, §5); mode C is
reachability over *all* pending markers whatever their origin. Events remain
load-bearing for provenance and reporting — "what did my retract cause?" is a
legitimate event-scoped query — but they do not scope a resolution mode.

## 4. Conduction — the prior the machine renders, and never applies

A retracted premise does not bear on every downstream node the same way; it
depends on that node's own `reasoning_type`. Chain types (every premise
jointly necessary — the ten deductive/inductive-causal/abductive-elimination
types plus `authority_expert`) break; corroborative types
(`inductive_generalization`, `authority_consensus`) attenuate, recomputed
over surviving **independent** premises; two types are premise-role-dependent
(`abductive_best_explanation`, `inductive_analogy`). The machine computes
this recommendation — including a four-state independence verdict over the
survivor set whose fourth state is an explicit `UNKNOWN` — and **renders it
as a labelled hint. It never applies it.** A machine that applied conduction
would have to pick a branch on `reasoning_type` *as filed*, and type
inflation at filing is a real, repeated error; a machine branching on it
would silently mis-propagate a blocking marker. `UNKNOWN` is load-bearing,
not a hedge: a surface that cannot say "I cannot tell" turns absence into a
confident wrong answer, so the conservative branch (chain) is presented with
its reason. Measured base rate ~1% of current derivations; if it grows, that
is a substrate-gap signal, not the machine working.

The conduction table is asserted against the declared reasoning-type enum
(`set(CONDUCTION_TABLE) == REASONING_TYPES`) so a fifteenth type fails loudly
at test time instead of silently inheriting a branch.

## 5. Adjudication — the agent decides, every time

`engram_cascade_resolve(node_id, resolution_kind, replacement_id?, note?)`,
with `resolution_kind ∈ {retracted, superseded, acknowledged}`:

| choice | when | machine then |
|---|---|---|
| `retracted` | the argument is broken | delegates to the retract core; records; marks resolved |
| `superseded` | the claim survives on remaining premises; the replacement **already exists** (pure-wire: the verb never creates nodes) | delegates to the supersede core; records `replacement_id`; marks resolved |
| `acknowledged` | the delta provably does not touch this claim | writes the acknowledgement edges (§8); recomputes confidence; marks resolved; **no new node, node stays current** |

Design decisions with teeth:

- **Per-NODE, not per-marker.** No `cascade_event` parameter: one judgement
  stamps the same `resolution_kind` on all the node's pending markers
  (per-event rows retained for audit). The resolution shape is what happens
  to the node — inherently single-valued.
- **Eligibility: `acknowledged` is available iff NO affected direct premise
  resolved as `retracted`** — and **the cascade root counts as a direct
  premise resolving as its `root_op`** (the modal case, since the root
  carries no marker of its own). Only retract forbids, because only retract
  removes a leg from the argument: a superseded premise still supports the
  claim, differently; a retracted one never supported it at all. The
  predicate gates *availability of the option*, never the choice — the
  agent still judges whether a superseded premise's delta touches the claim.
  A refusal names the blockers (`acknowledge_blocked_by`), per invariant 8.
- **Trivial resolutions auto-fire.** A child whose derived dead-premise set
  becomes empty — every affected premise resolved `acknowledged`, nothing
  actually moved — is auto-resolved `acknowledged` by proof, not judgment,
  and never surfaces on the frontier. This is the only machine-decided
  shape. Sweep note: read the auto-resolved list on every resolve call; a
  loop that only re-reads `frontier` can miss nodes that never appeared
  there.
- **No verdict-confidence; `UNKNOWN` renders and never resolves.** A verdict
  is an act, not a node — and a low-confidence verdict is an UNKNOWN wearing
  a number: permitting it would let an unresolved adjudication clear a
  marker while recording that it was never really made. An adjudicator who
  cannot decide resolves nothing; the marker stays pending, loudly.
- **Frontier ordering is presentation-only.** Event-grouped, `created_at`
  ascending within group, node-id tie-break — deterministic and resumable.
  No machine step may depend on the order: frontier nodes have no
  inter-dependency by definition.

### Confidence is ALWAYS recomputed — unconditionally, and transitively

Acknowledging that the **claim** is unchanged is a separate matter from the
**confidence** of that claim. Measured on a real graph: of 412 supersede
pairs where both nodes carry a confidence, **236 (57.3%) changed it** (median
|Δ| 0.112, max 0.825) — the majority case, not an edge case. And the
recompute is itself a propagation: a child was computed from the parent's old
value, so one hop is not enough. The implementation runs whole passes to a
fixpoint rather than trusting a depth-first walk to be topological on a
diamond, and reports non-convergence (a cycle symptom) by name rather than
silently truncating.

## 6. The nine invariants

1. **No pending marker survives a completed mode-B run.** Whole-graph, not
   event-scoped — event-scoping would let one event's resolution force
   another's, unboundedly (see §3's event-agnostic note). The frontier's
   anomaly branch exists to keep a stuck state from impersonating a
   completed run.
2. **A marker caches nothing derivable.** Replacement identity is read from
   the resolved premise at adjudication time, never stored on the child.
   (The resolution record itself is not a cache — it is the authoritative,
   terminal write of a decision.)
3. **Every marker names its root and its cascade event.** A marker that has
   lost its root is unrepairable by mode C.
4. **No node is cited as a premise while in pending status.** Enforced at
   *write* time in `engram_derive` — this is the marker's entire job, and it
   is also what makes invariant 7's covered-set proof hold over time (a
   pending node cannot acquire new descendants).
5. **Confidence is never agent-assigned during repair.** Always computed.
6. **A marker resolves only while its node is on the frontier, and never
   returns from `resolved` to `pending`.** No reopen: a resolution later
   believed wrong is superseded like any wrong claim, firing a new event.
   (Caveat recorded in §3: no write path *mechanically* enforces the
   frontier half — the frontier's transitive blocking check is what absorbs
   the divergence when it happens, including #2324-backfilled markers.)
7. **Superseding a marked node must also resolve its marker** — the organic
   verb refuses a supersede that supplies no resolution, pointing at the
   resolve flow. Such a supersede is a *partial resolution of the existing
   event, never a new one*. The replacement cannot be born marked; the
   no-drop discipline (keep, alter, or explicitly retract every load-bearing
   claim) still applies.
8. **The recommendation must be reproducible by the agent from the displayed
   inputs alone.** Grounded in an asymmetry of error costs: a withheld
   nuance is undetectable by the agent, a surplus field costs a moment's
   reading — so over-surface, co-locate the verdict with its inputs, and a
   build test can compare the recommender's input set against the renderer's
   output set.
9. **Cross-cascade: marker state is per-cascade; node state is the
   conjunction.** A node touched by two events carries two markers and is
   fully resolved only when all are — with teeth: resolving one marker by
   superseding the node forces resolving its others in the same act
   (invariant 7 forbids a born-marked replacement).

## 7. Three propagations, three different rules — do not conflate them

The machine carries three distinct propagation behaviours, and each follows
its own rule. Applying whichever rule the reader happens to remember is the
documented failure mode this section exists to prevent.

1. **Markers do NOT re-cascade on resolution.** If `B` is downstream of `A`,
   everything that could care about `B → B'` already carries `A`'s marker
   and is still pending — re-propagation would protect a provably empty set,
   and invariant 4 keeps the proof good over time. Implementation: one
   shared core per organic verb with a `cascade_triggering` parameter —
   `True` on the normal path (fires the cascade; refuses if the target is
   already marked), `False` on the resolution path (always open, mints no
   event). The parameter is not reachable from any MCP payload.
2. **Confidence DOES re-propagate** — unconditionally, transitively, to a
   fixpoint (§5). Opposite rule, same machine.
3. **The contradiction cascade follows neither rule.** Contradiction nodes
   are non-claim-bearing, so the derivation walk never marks them, and the
   covered-set proof never covered them — a contradiction attached to a
   mid-chain node was measurably left unreviewed by both the root's cascade
   and the node's resolution. So a resolution of kind `retracted` or
   `superseded` marks the node's current contradictions **on a dedicated
   contradiction-only cascade event** (a fresh event per resolving node —
   `UNIQUE(node_id, cascade_event)` makes reusing the resolved event a
   silent no-op write, measured). This does not violate invariant 7's
   no-new-event rule because no claim-bearing node receives a marker from
   it. Operational consequence: **the frontier can grow across a
   resolution** — a sweep re-reads the frontier after each call rather than
   assuming one pass drains it.

## 8. The acknowledgement edge — the epic's §12, in full

The subtlest part of the design, and the one that failed twice before it
held. (References of the form "epic §12.x" below use the design epic's
issue-body section numbering — historical labels from the build record,
not this document's own section numbers.) §4's original mandate — an acknowledgement records a `derives_from`
edge to the superseding premise, so the acknowledgement outlives the repair
window — could not be written as stated, for two reasons.

**(a) It is always backwards in time.** The acknowledging node `X` exists
*before* the replacement `P′` — that is what an acknowledgement *is* — so
`X --derives_from--> P′` inverts creation order on every acknowledgement, by
construction. Measured: one DAG-time violation per acknowledgement.

**(b) It can close a cascade cycle.** Constructed through public verbs only
(no adversarial input needed — refining a claim in light of a decision made
on it is ordinary work): supersede a node with a replacement transitively
derived from it, acknowledge the marked intermediate, and the ack edge closes
the loop. Measured consequences: confidence recompute stops being idempotent
(`0.816 → 0.723 → 0.641 → …` on identical re-runs), and the frontier reports
`frontier_count: 0` against a non-empty pending set — both members refuse
resolution permanently.

And the edge cannot simply be dropped: §5's always-recompute mandate *only
has anything to read* on the acknowledge path because this edge brings the
replacement into the premise set (measured: without it, `0.833 → 0.833`,
`changed: []`).

**The resolution, in three parts:**

1. **The supersede guardrail (epic §12.3).** `engram_supersede` and the
   retraction-replacement path refuse a replacement that is reachable
   downstream of the node being replaced — superseding your own transitive
   derived-node is ill-formed (you make yourself stale; the replacement's
   status at that moment is undefined). One reachability query at write
   time, ranged over the **derived** cascade-relation set (§9), never a
   relation literal.
2. **The `acknowledged_derives_from` relation (epic §12.5).** A distinct edge
   relation written by the machine at acknowledgement time (never
   agent-supplied), for each affected premise that resolved `superseded`,
   from the acknowledging node to the superseding replacement (read at use
   time from the premise's own resolution — invariant 2). Its
   `EDGE_CLASSIFICATIONS` entry: `cascade: True` (a later supersede of the
   replacement must reach the acknowledging node — the edge's whole reason),
   confidence-participating (it is a premise edge), **`temporal_causal:
   False`** (its source predates its target by construction, always — the
   creation-order exemption is definitional, not an oversight), and
   **`dag: True`** (it is still not permitted to close a logical cycle).
   A distinct relation rather than an exemption on `derives_from` keeps the
   creation-order check honest everywhere else and makes
   adjudication-established dependencies separately queryable. (#2357 split
   the old `dag_check` flag into `temporal_causal` — creation order — and
   `dag` — acyclicity — precisely so this relation could hold one without
   the other.)
3. **The write-time cycle guard (epic §12.7) — because the sufficiency proof was
   incomplete.** epic §12.4 proved the guardrail sufficient for a *single*
   supersede/acknowledgement pair: a cycle needs `P′ → … → X`, marked ⟹ `X`
   downstream of `P`, so `P′` downstream of `P` — exactly what epic §12.3
   forbids. But epic §12.3 is a **point-in-time** check over the edge table as it
   stood then, and every acknowledgement written afterwards grows the very
   relation it was evaluated over. Two individually-legal supersedes plus
   two acknowledgements can still close a three-node cycle — measured:
   recompute non-idempotent (`0.411 → 0.322 → 0.254` on identical re-runs).
   So `_cascade_record_acknowledgement_edges` re-checks at write time:
   before writing an edge to `replacement`, it refuses (atomically — no
   partial edge set) if `replacement` already transitively derives from the
   acknowledging node. The proof's own standing condition is recorded with
   it: it rests on *marked ⟹ downstream via cascade relations*, and
   `acknowledged_derives_from` joined that set the moment it was born —
   which is why every reachability query involved must be derived, not
   literal.

## 9. The derived-set SSoT rule — stated as a rule, with the failure it prevents

> **Avoid enumerating relations. Almost always use the derived set.** When
> premise-hood or cascade is in concern, use `CASCADE_RELATIONS` (derived
> from `EDGE_CLASSIFICATIONS` by the `cascade` flag). When acyclicity is in
> concern, use the not-dag-exempt set. `EDGE_CLASSIFICATIONS` is the single
> source of truth **at every call site**.

This is the rule most likely to be quietly violated, because a hand-listed
relation set is locally correct the day it is written. The concrete failure
it prevents is §8's: the cycle-guard proof rests on *marked ⟹ downstream via
cascade relations*, and a reachability query written over the `derives_from`
literal silently stops covering the proof the moment a new cascade relation
lands — which is not hypothetical, because this design itself added one.
Before the partition work (#2252), the walk behaviour lived in nine hardcoded
SQL literals with four distinct memberships; the machine was deliberately
gated on collapsing those into the declared flag first.

## 10. The severity derivation and the `use_stale` retirement

Post-amendment, "severity" means exactly one derived thing: the
**eligible-shapes set** offered at a node's own resolution turn —

```
eligible(node) = {retracted, superseded}                 if any affected direct premise resolved 'retracted'
                 {retracted, superseded, acknowledged}   otherwise
(the cascade ROOT counts as a direct premise resolving as its root_op)
```

— computed inside the resolve verb (enforcement) and rendered in the frontier
packet (presentation), never persisted, never client-computed.

The `use_stale=True` force-through on `engram_derive` is **retired, not
preserved**. It existed only because there was no cheap resolution route for
a node whose premises all still hold; the acknowledgement path is that route,
so the hole closes and the agent repairs the node through the machine at the
point of use. The citation gate's refusal names the verb to run, not just the
predicate. (Stated here so nobody re-adds the force-through later as a
convenience.)

## 11. The marker backfill (upgrading a pre-machine graph)

Graphs written before the machine carry the retired `metadata.tainted_by` /
`metadata.stale_by` fields. The #2324 backfill runs as a `user_version`-gated
migration at first server start on the new code: current-only population,
idempotent (`INSERT OR IGNORE` + version gate), per-root events
(`backfill:{op}:{root}`), timestamps recovered from edit history where
possible. Design choices with teeth: **over-blocking is the safe failure
direction** (backfilled markers hard-block immediately, both root_ops — the
intended behaviour change; acknowledge is the designed cheap path); the
count-gate fails loud; and the fleet-upgrade discipline pairs the migration
with an **independent pre-count** recorded before upgrading, because the
migration's own printed count is the thing under test — a migration that
skips a class reports a self-consistent number and passes its own check.
Backfilled markers are also the standing example of invariant 6's caveat:
synthesized from retired metadata rather than produced by a live cascade,
they can violate resolve-only-on-frontier ordering, which the frontier's
transitive blocking check absorbs.

## 12. Defects found during the build — the part a final shape can't teach

Five reviewer rounds on slice 4 found five real defects and **zero in the
design logic** — every one was code or prose asserting something about its
own behaviour that measurement contradicted. The next person building on this
subsystem should know the *shape* of what failed, because the class recurs:

- **The sufficiency proof that quietly expired.** epic §12.4's cycle-impossibility
  proof was sound for one pair and silently incomplete for sequences,
  because each acknowledgement grows the relation the proof quantified over.
  Caught by constructing the three-node cycle through public verbs and
  measuring the non-idempotent recompute (§8). Fix: the write-time guard.
- **A reported write that silently did not happen.** The contradiction-marking
  fix first reused the node's existing resolved events; `UNIQUE(node_id,
  cascade_event)` + `INSERT OR IGNORE` made the second node's write a silent
  no-op while the response still listed the contradiction as marked. Caught
  by a reviewer measuring the double-resolution case. Fix: the dedicated
  contradiction-only event (§7).
- **A structurally vacuous read.** `_dead_premises`' resolved-filter had no
  writer on the tip it was reviewed on — a filter that could never match,
  invisible to tests that only exercised the paths that existed. Caught by
  asking *what writes the field this reads*.
- **A generated artifact stale by not appearing in the diff.** Slice 3
  registered a tool without regenerating `tool_capability_index.txt` —
  passing on dev, failing on the branch, invisible to every diff-scoped
  reviewer and to dev-PR CI (which runs only static checks). Caught from
  outside the review process; CI-coverage follow-up filed.
- **An enumeration whose published cause was wrong.** The `derives_from`
  reader-set count was republished as 79 sites after an initial 54 — and
  the *explanation* attached to the correction (a glob missing one
  directory) was itself wrong: the recount had changed **two** things, the
  glob AND the classification criteria, reported as one mechanism, and the
  command that produced 54 is gone — so the delta between the totals is
  unestablished, not merely mis-explained. Only the 79 is auditable
  (itemised, file:line); the causal story appended to it was not. The
  standing lesson folded into epic §12.6's practice: enumerate rather than grep
  by shape — and that governs the **cause you publish for a number**, not
  only the number.

Common tell across all five: the defect lived in an *assertion about
behaviour* (a proof, a response field, a filter's implied writer, an index's
implied freshness, a count's implied method) rather than in behaviour — and
the catch, in every case, came from **measuring the artifact instead of
reading its prose**.

## 13. Bindable claims

Memberships and constants this document asserts, bound by
`tests/test_cascade_machine_doc_binding.py` (on the
`test_2252_partition_doc_binding.py` pattern, including its can-it-fail
control — the binding must be read from this file, not hand-copied into the
test):

- `CASCADE_RELATIONS` is derived from `EDGE_CLASSIFICATIONS` by the
  `cascade` flag and currently comprises exactly: `derives_from`,
  `acknowledged_derives_from`. (`supported_by` is NOT a member — #2252
  Stage 1.3 removed its cascade flag: post-partition it is NCB vote
  accumulation, and an NCB target has no claim to re-adjudicate. This
  bullet's first draft listed it anyway, from a pre-Stage-1.3
  understanding — caught by the binding test's author before the test
  existed, which is this section's whole argument made empirical.)
- The resolution kinds are exactly: `acknowledged`, `retracted`,
  `superseded` (`_CASCADE_RESOLUTION_KINDS`).
- `acknowledged_derives_from` carries `cascade: True`,
  `temporal_causal: False`, `dag: True` in `EDGE_CLASSIFICATIONS`.
- The marker status domain is exactly `pending`, `resolved`, and the
  `cascade_markers` schema carries no severity column.

*Agent-facing procedure lives in the skills (`engram-revise`,
`engram-resolve-cascade`) and the dream-master spec; this document is the
design record those surfaces point at.*
