---
name: engram-resolve-cascade
description: Resolve cascade markers (tainted_by, stale_by) on nodes flagged by a retract or supersede. Use during dream cycle, whenever engram_cascade_frontier shows eligible nodes, or whenever a plain engram_retract/engram_supersede is refused and points here. Read the frontier, then adjudicate each eligible node via engram_cascade_resolve — retracted, superseded, or acknowledged. Cornerstones/lessons with support_lost are handled by the separate engram-support-lost skill.
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

# Background: how cascade markers fire (the cascade machine; design of
# record: docs/cascade-machine.md in the ENGRAM source repository)

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
  `tainted_by` / `stale_by` markers. Handling lives in the
  **[engram-support-lost](../engram-support-lost/SKILL.md)** skill.

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
- **Mode C** (`node_id` given) — the frontier **restricted to**
  `ancestors(node_id) ∩ pending`: only the *currently-resolvable* (unblocked)
  pending ancestors of this one target — the same moving-frontier semantics
  as mode B, not a different node-set. `ancestors(node_id)` is
  self-inclusive: the target itself appears in the returned frontier when
  it is itself resolvable — the MODAL case when you arrive here from a
  refused plain retract/supersede, so your own target in the list is the
  mechanism working, not a bug. Resolve what it returns and call
  again: ancestors that were blocked surface on the next call, exactly as a
  mode-B sweep progresses. The FULL pending-ancestor slice is never returned
  as resolvable entries — it appears only as the labeled
  `blast_radius_estimate` (and `total_pending_nodes` counts graph-wide).
  Use this mode when `engram-revise` pointed you here after a refused plain
  retract/supersede.

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
  **No new node.** Records an `acknowledged_derives_from` edge to each
  superseding premise
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

**Citation count is never a reason to hesitate on a frontier node — say this
plainly, because agents (and the dream-master) have hesitated on exactly this
basis in practice.** (Scoped to ordinary claim-bearing citers, i.e. the usual
case this paragraph is about — a different, disjoint class from the
contradiction-cascade exception just above: that one concerns non-claim-bearing
contradiction nodes reached by a dedicated event, and is unaffected by
anything below.) A frontier node's entire downstream-reachable set was
already marked pending by the SAME root event that put the node itself on the
frontier, at the moment that event first fired — not incrementally, one hop
at a time, as each frontier node gets resolved later. The sentence above
("the derivation cascade does NOT re-fire on resolution") is not just a
mechanical footnote — it is the reason a frontier node's citation count is
irrelevant to the cost of resolving it. A node with 20 citers costs exactly
the same to resolve as one with none: nothing downstream can acquire a
*second* marker from your `engram_cascade_resolve` call, ever, by
construction. If a thought along the lines of *"this node is heavily cited —
let me register-and-defer rather than resolve it now, to avoid triggering a
big blast radius"* shows up, that thought is factually wrong, not prudent —
treat it as a bug to correct, not a caution to honor. (Confirmed in practice,
not just by design: a session that hesitated on a heavily-cited frontier node
for precisely this reason resolved it anyway and confirmed 0 new markers —
the citers were already covered by the original root event, as they always
are. The maintainer's ruling on the incident: the hesitation itself was "the
real friction showing up in practice," not a signal to heed.)

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
`D` as an honest audit trail. (The `acknowledged` shape avoids the derive-new
step but NOT the wiring: when an affected premise was **superseded**, the
resolution itself records an `acknowledged_derives_from` edge from your node
to the superseding premise `P'` — deliberately exempt from the creation-order
check (`temporal_causal: False`; this edge always points older→newer, that is
what an acknowledgement *is*) but still cycle-guarded (`dag: True`; a
resolution whose edge would close a logical loop is refused, §12.7). The edge
is what lets a *later* supersede of `P'` reach your node again once its marker
is gone. Only when nothing actually moved — every affected premise itself
resolved `acknowledged`, the auto-ack case — is truly nothing new cited.)

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

**Worked example A — retracted premise (`acknowledged` NOT available):**

```
t0  ob_X    "X measured at 5.0"         (premise)
t1  dv_Y    derives_from ob_X → "Y"     (conclusion)
t2  ob_X retracted (wrong_evidence); corrected ob_X_new "X = 7.0" filed at t2
    → engram_cascade_frontier(node_id="dv_Y") now shows dv_Y on the frontier
      (its one pending ancestor, ob_X, is itself non-current — resolved)
```

A retracted premise never supported the claim at all — a leg of the argument
is *gone*, so the eligibility gate withholds `acknowledged` entirely: the
resolve response's `eligible` list is `["retracted", "superseded"]` and
`acknowledge_blocked_by` names `ob_X`. **This holds even if `dv_Y`'s
conclusion is textually unchanged** — there is no "keep it as is" option
after a retract; the argument must be rebuilt on live premises.

If the conclusion survives on the corrected value: at t3, file `dv_Y_new`
deriving from `ob_X_new` → "Y′" (the claim text may be identical — what's new
is the argument, not necessarily the words), then
`engram_cascade_resolve(node_id="dv_Y", resolution_kind="superseded",
replacement_id="dv_Y_new")`. `dv_Y_new` (t3) is later than `ob_X_new` (t2) →
valid. `dv_Y` drops off `is_current`; both old nodes are preserved as audit
trail. If the conclusion does not survive at all:
`resolution_kind="retracted"`.

**Worked example B — superseded premise (`acknowledged` available):**

```
t0  ob_X    "X measured at 5.0"         (premise)
t1  dv_Y    derives_from ob_X → "Y"     (conclusion)
t2  ob_X superseded by ob_X' "X measured at 5.02 on the better instrument"
    → engram_cascade_frontier(node_id="dv_Y") shows dv_Y on the frontier,
      and `eligible` now includes "acknowledged" — a superseded premise
      still supports the claim, just differently
```

If the premise's delta doesn't touch `dv_Y`'s argument (e.g. it used `ob_X`'s
presence, not the precise number — an agent judgment call, §4):
`engram_cascade_resolve(node_id="dv_Y", resolution_kind="acknowledged")` —
no new node, `dv_Y` stays current, confidence recomputes against the corrected
premise, **and the resolution wires
`dv_Y --acknowledged_derives_from--> ob_X'`** (see the parenthetical above:
temporal_causal-exempt by design, dag-cycle-checked) so a later supersede of
`ob_X'` can still reach `dv_Y`.

If the delta DOES touch the argument: same derive-new + `superseded` shape as
example A.

**The difference to hold on to:** `acknowledged`'s availability is gated on
*what happened to the premises* (retracted → never offered; superseded →
offered), never on how small the change feels. The agent's judgment enters
only in example B, choosing between acknowledge and derive-new — in example A
there is nothing to judge.

# Pattern B — Cornerstone or lesson with support_lost

`support_lost` is a plain `metadata.support_lost` boolean set when a
cornerstone's or lesson's live support edges all drop to zero — it never
appears on `engram_cascade_frontier` and is never adjudicated via
`engram_cascade_resolve`; the acting verb is `engram_register_exemplar`, not
a cascade-resolution shape, and the flag clears automatically once a live
exemplar edge exists rather than by explicit adjudication. This section
stays only so inbound references from `Background` and elsewhere keep
resolving — the full handling (inspect → search for live exemplars →
trivial rewire, leave honestly unanchored, or retract via `engram-revise`)
now lives in the **[engram-support-lost](../engram-support-lost/SKILL.md)**
skill. Load it directly rather than following this pointer twice.

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

# When in doubt — your call, almost always

The graph is YOUR epistemic self. Errors and their corrections are your
responsibility — adjudication is not something to hand to your user. The
default on every path in this playbook is: **resolve it yourself, with the
evidence you have.** Your user can help with genuinely hard decisions; they
are never an everything-discuss-first gate. And most users are not fluent in
ENGRAM vocabulary — surfacing marker mechanics to them is noise and burden,
not diligence.

Three dispositions, in order of preference:

1. **Resolve** — the overwhelming default. Given enough evidence to judge,
   adjudicate — including high-confidence, load-bearing, or heavily-cited
   nodes. That a derivation is load-bearing or widely cited is a reason to
   resolve it *carefully*, not a reason to defer it or outsource the call —
   see the blast-radius callout in Adjudication above: resolving a frontier
   node never costs more markers no matter how many nodes cite it, so
   citation count is not a legitimate input to this disposition choice at
   all.

2. **Leave open** — when the evidence genuinely doesn't settle it. A pending
   marker is honest state, not a failure: more evidence arrives, and the
   frontier will still surface it. Leaving a hard case open for a
   better-informed future pass IS a resolution discipline. Never escalate
   just to discharge the discomfort of an open question.

3. **Escalate** — rare, and only for these shapes:
   - the resolution would leave a **goal or axiom in tension** — the
     identity core moves, not just a belief;
   - the right outcome is one the **resolution mechanisms can't cleanly
     express** — a genuinely novel shape the machine cannot settle;
   - the user **holds information or a human-perspective judgment you
     actually need** — facts about the world, their intent, their domain.

   The test: escalate when you need something *from* the user, never to
   delegate a judgment that is yours. And when you do escalate, translate —
   ask in plain terms about the belief at stake ("I recorded X, later
   found Y, which did you mean?"), never in marker/frontier/resolution-kind
   vocabulary.

The dream-master's role is the same disposition, one level up: run mode B
and adjudicate — the judgment calls included, not only the mechanical
cases — leave open what evidence can't settle, and carry only the rare
identity-core or need-human-input shapes into the dream record for the
waking agent to take up (who then decides whether it truly reaches the
user).
