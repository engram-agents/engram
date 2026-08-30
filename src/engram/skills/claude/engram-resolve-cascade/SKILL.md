---
name: engram-resolve-cascade
description: Resolve cascade markers (tainted_by, stale_by, support_lost) on nodes flagged by a retract or supersede. Use during dream cycle, whenever engram_cascade_frontier shows eligible nodes, or whenever a plain engram_retract/engram_supersede is refused and points here. Read the frontier, then adjudicate each eligible node via engram_cascade_resolve — retracted, superseded, or acknowledged. Cornerstones/lessons with support_lost use a separate exemplar-rewire/pattern-retract pattern.
---

# When to use this skill

Most commonly: during dream-cycle consolidation, when `engram_cascade_frontier`
shows nodes eligible to adjudicate right now. Also whenever a plain
`engram_retract` or `engram_supersede` call is **refused** because the target
already carries a pending cascade marker of its own — the refusal names the
exact `engram_cascade_frontier` / `engram_cascade_resolve` calls to make
instead (see `engram-revise`, Step 1). Also useful when manually auditing
"what changed in my graph after that retract/supersede last night."

NOT used during the retract/supersede action itself — that's the
**[engram-revise](../engram-revise/SKILL.md)** path (classify error or create
a replacement, call the plain verb, engine handles the cascade automatically
unless the target is itself already marked).

# Background: how cascade markers fire (PR #421 semantics; storage + verbs
# updated 2026-08 through engram-alpha#2013/#2009/#2338/#2342/#2349/#2343 —
# the cascade machine)

After a retract or supersede, the engine walks downstream and marks:

> **The marker row carries NO severity — it never did.** A `cascade_markers`
> row records `root_op` (`'retract'` or `'supersede'`) and a `status` of
> `pending` or `resolved`; there is no severity column. `tainted_by` and
> `stale_by` below are **derived read-time views** over `root_op`, computed by
> `_cascade_marker_severity()`, not two kinds of stored marker. Any prose
> calling these "taint-severity" or "supersede-severity" *rows* is describing
> a shape that does not exist in the table.

- **`tainted_by`** — a derivation that `derive_from`s a retracted node picks
  up a pending marker (`root_op='retract'`) in the `cascade_markers` table
  (NOT a `metadata.tainted_by` field — that field is retired). You still SEE
  it the same way: `engram_reflect`'s `tainted_nodes` report and
  `engram_inspect`'s `warnings.tainted_by` both surface it as a `tainted_by`
  list of root node IDs, unchanged in shape. Means: one or more premises are
  now invalid.
- **`stale_by`** — same migration, `root_op='supersede'`. `engram_reflect`'s
  `stale_nodes` report and `engram_inspect`'s `warnings.stale_by` surface it.
  Means: a premise was updated; the derivation may or may not still hold.
- **`support_lost`** (boolean) — set on a cornerstone or lesson when ALL its
  live support edges (`supported_by` outgoing + `exemplifies` incoming) drop to
  zero. **A genuinely separate mechanism** (a plain `metadata.support_lost`
  flag written by `_detect_zero_support`), not part of the cascade-marker
  system at all — cornerstones and lessons accumulate votes rather than
  derive_from premises, so individual instance retractions never give them
  `tainted_by` / `stale_by` markers. See Pattern B.

Retracted/superseded nodes are preserved, not deleted — the engine checks
`is_current` when walking edges.

**Where a marker actually clears:** a `cascade_markers` row is written
`status='resolved'` by `engram_cascade_resolve` (the adjudication verb below)
— this is a real write path now, not aspirational. The node itself also drops
off `is_current=1` if the resolution was `retracted` or `superseded`, at which
point every reader that surfaces `tainted_by`/`stale_by` stops showing it
regardless. `acknowledged` resolves the marker WITHOUT retiring the node — the
node stays current, the marker is stamped resolved, and confidence recomputes.

# Discovery: engram_cascade_frontier (the read half)

`engram_cascade_frontier` tells you which marked nodes are ready to
adjudicate right now — READ-ONLY, no marker mutation, recomputed fresh from
marker state on every call (no queue, no stored cursor).

A node is on the **frontier** iff it carries ≥1 pending marker AND none of its
cascade-ancestors (full transitive closure, not just direct premises) still
carries a pending marker. This is the topological gate: you resolve a cascade
bottom-up, oldest premise first, never skipping ahead to a node whose own
inputs are still unadjudicated.

Two modes:
- **Mode B** (`node_id` omitted) — every frontier node in the graph. Use this
  for a dream-cycle sweep.
- **Mode C** (`node_id` given) — `ancestors(node_id) ∩ pending`: the minimal
  pending-ancestor slice that blocks this one target, plus a labeled
  `blast_radius_estimate`. Use this when `engram-revise` pointed you here
  after a refused plain retract/supersede.

```
engram_cascade_frontier(payload_json='{"node_id": "<node_id>"}')   # mode C
engram_cascade_frontier(payload_json='{}')                          # mode B
```

Each frontier entry lists every pending event on that node (root, `root_op`,
which direct premises died and how) — enough to adjudicate from without a
separate `engram_inspect` call, though inspecting the claim itself is still
worth doing before you decide.

**`invariant_1_violation_suspected`** appears only when candidates exist but
the frontier is empty — should be unreachable in a well-formed graph. Don't
treat its presence as a normal empty result; it means something (most likely
a `CASCADE_RELATIONS` cycle) needs investigating, not resolving.

# Adjudication: engram_cascade_resolve (the write half)

For each frontier node, `engram_cascade_resolve` records **one of three
shapes**, per node (not per marker — one judgement stamps every pending
marker on that node at once, since the frontier packet already showed you all
of them):

- **`retracted`** — the argument is broken. Delegates to the retract core
  (`error_type` optional, defaults `other`).
- **`superseded`** — the claim survives on the remaining premises, and you
  have **already created the replacement node** via its own canonical tool
  (`engram_derive`, `engram_add_observation`, ...). Pass `replacement_id`.
  Delegates to the supersede core.
- **`acknowledged`** — the upstream delta provably does not touch this claim.
  **No new node.** Records a `derives_from` edge to each superseding premise
  (so a later supersede of *that* premise re-reads this node) and recomputes
  confidence. Available IFF no affected direct premise resolved as
  `retracted` — only retract removes a leg from the argument, so only retract
  forbids acknowledging.

```
engram_cascade_resolve(payload_json=json.dumps({
    "node_id": "<frontier node>",
    "resolution_kind": "retracted" | "superseded" | "acknowledged",
    "replacement_id": "<only for superseded>",
    "note": "<your reasoning>",
}))
```

Confidence is always recomputed (transitively — a child was computed from the
parent's OLD value), whichever shape you pick. **The derivation cascade does
NOT re-fire on resolution** — nothing downstream acquires a second marker to
adjudicate from this call. The one exception: if the node carries
**contradictions**, this call mints a dedicated contradiction-only cascade
event (`contradiction_cascade_event`) — contradictions are non-claim-bearing
and the derivation walk never reaches them any other way. **This means the
frontier can grow underneath you across a resolution** — re-read the frontier
after each call in a sweep rather than assuming one pass drains it.

A node not on the frontier is refused, naming its blocking ancestors — its
eligible shapes are a function of how ALL its premises resolve, so they
aren't knowable until the ancestors resolve first.

**Downstream children can auto-resolve without you seeing them on the
frontier at all.** After you resolve a node, the response's
`auto_resolved_trivial` list names any direct child that just became
eligible AND trivially resolvable — every one of that child's own affected
premises resolved as `acknowledged` (not merely non-retracted: a premise
that resolved `superseded` still blocks the auto-fire, same as `retracted`
does). Such a child is auto-resolved `acknowledged` on the spot, because
nothing about its argument actually moved. Read this list on every
call — a sweep that only reads `frontier` after each `engram_cascade_resolve`
can miss nodes that never needed to appear there.

# Why derive-new + supersede (for the `superseded` shape specifically)

ENGRAM's graph is a **time-ordered causal DAG** — a node may only depend on
nodes created *before* it. When you retract/supersede premise `P` and replace
it with `P'` (created **now**), the replacement `P'` is *later* than the
downstream derivation `D` you're repairing. So you cannot simply re-point `D`
at `P'`: that makes an earlier node (`D`) depend on a later one (`P'`) — a
causal edge pointing backwards in time. Deriving a **new** node `D'` *now*
(later than `P'`), citing the live/corrected premises, and resolving `D` as
`superseded` with `replacement_id=D'` keeps the ordering valid and preserves
`D` as an honest audit trail. (When the claim needs no new premises at all —
the `acknowledged` shape — this concern doesn't arise, because nothing new is
being cited.)

⛔ **Do NOT use `tools/surgical.py` or raw `knowledge.db` edits to clear a
marker.** Two traps:

- **Re-pointing an edge** to a newer replacement creates the backwards-in-time
  DAG violation above.
- **Deleting a `cascade_markers` row directly** without going through
  `engram_cascade_resolve` hides the problem instead of fixing it: the node
  still rests on evidence you've disowned, and no `resolved` status or
  confidence recompute ever happens. That converts a *loud* failure (a
  visible pending marker) into a *silent* one — exactly the corruption the
  honesty axiom warns against.

Surgery bypasses every guardrail the MCP layer enforces (DAG validity, taint
cascade, resolution eligibility, type preservation, confidence propagation).
For clearing a marker it is never the right tool.

**Worked example:**

```
t0  ob_X    "X measured at 5.0"         (premise)
t1  dv_Y    derives_from ob_X → "Y"     (conclusion)
t2  ob_X retracted (wrong_evidence); replacement ob_X_new "X = 7.0" filed at t2
    → engram_cascade_frontier(node_id="dv_Y") now shows dv_Y on the frontier
      (its one pending ancestor, ob_X, is itself non-current — resolved)
```

If `dv_Y`'s conclusion still holds on the corrected value: at t3, file
`dv_Y_new` deriving from `ob_X_new` → "Y′", then
`engram_cascade_resolve(node_id="dv_Y", resolution_kind="superseded",
replacement_id="dv_Y_new")`. `dv_Y_new` (t3) is later than `ob_X_new` (t2) →
valid. `dv_Y` drops off `is_current`; both old nodes are preserved as audit
trail.

If `dv_Y`'s conclusion doesn't depend on the specific value that changed (e.g.
it only used `ob_X`'s presence, not its number): `engram_cascade_resolve(
node_id="dv_Y", resolution_kind="acknowledged")` — no new node, `dv_Y` stays
current, confidence recomputes against the corrected premise.

# Pattern B — Cornerstone or lesson with support_lost

When you see `cs_X.support_lost = true` or `ls_X.support_lost = true` (this
path is unchanged by the cascade-frontier machine — support_lost is the
separate mechanism described above, not a cascade marker):

1. `engram_inspect(cs_X / ls_X)` — read the cornerstone/lesson claim
2. `engram_query` for similar patterns in the graph — are there live
   observations or derivations that exemplify this cornerstone's claim?

   **If YES — trivial rewire:**

   3a. Register the exemplar against the pattern node:
       ```
       engram_register_exemplar(payload_json=json.dumps({
           "target_id": "<cs_X or ls_X>",
           "exemplar_id": "<ob_or_dv_id>",
           "note": "<why this instance fits the pattern>",
       }))
       ```
       The tool handles both cornerstones and lessons; for lesson targets,
       the tripwire cache refresh fires automatically.

   3b. The `support_lost` flag clears automatically once at least one live
       exemplar edge exists.

   **If NO — the pattern has lost its empirical basis:**

   4a. Decide whether the cornerstone/lesson still merits existing.

   4b. If the pattern still holds in principle but is currently un-exemplified:
       leave the `support_lost` flag in place. The next exemplifying experience
       can link it via the trivial-rewire path above. `support_lost` is not an
       error — it is an honest signal that the pattern is currently unanchored.

   4c. If the pattern no longer applies (the underlying belief has been
       superseded or retracted): retract the cornerstone/lesson via
       `engram-revise`.

# Pattern C — Observation with a cascade marker (rare, audit upstream)

If an observation shows up on the frontier, this is unusual — observations
`derive_from` nothing (they are filed from primary sources, not inferred from
other claim-bearing nodes). A marker on an observation usually means the
upstream retract/supersede was wrong or the observation was mistakenly
modeled as deriving from another node.

1. `engram_inspect` the marked observation
2. `engram_inspect` the upstream root node that caused the marker
3. If the upstream revision was correct AND the observation's claim is
   independently invalidated: resolve it `retracted` via
   `engram_cascade_resolve`.
4. If the upstream revision was correct but the observation stands on its
   own independent evidence: resolve it `acknowledged`, or — if the
   `derive_from` edge itself was the modeling error — surface to the user;
   this may need the edge removed rather than the marker adjudicated.

# When in doubt

Don't act. Surface the cascade marker to the user. Pattern resolution is a
human-judgment moment whenever the cleanup involves identity-layer
cornerstones, high-confidence load-bearing derivations, or any situation
where the right resolution shape is genuinely unclear.

The dream-master fairy's role is to run `engram_cascade_frontier` (mode B),
route flagged nodes to this playbook, and adjudicate the mechanical cases —
not to resolve genuinely unclear judgment calls autonomously.
