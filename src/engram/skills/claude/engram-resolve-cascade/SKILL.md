---
name: engram-resolve-cascade
description: Resolve cascade markers (tainted_by, stale_by, support_lost) on downstream nodes after a retract or supersede. Use during dream cycle or whenever investigating "what changed in my graph after that retract last night." Three by-type patterns — derivations get derive-new+supersede; cornerstones/lessons get exemplar-rewire or pattern-retract; observations rarely need action (cascade markers on observations are unusual and usually indicate the upstream retract was wrong).
---

# When to use this skill

Most commonly: during dream-cycle consolidation, when engram_reflect surfaces
nodes with `tainted_by`, `stale_by`, or `support_lost` markers. Also when
manually auditing after a retract or supersede.

NOT used during the retract action itself (that's the engram-retract path —
classify error, call engram_retract, engine handles cascade automatically).

# Background: how cascade markers fire (PR #421 semantics; storage updated
# 2026-08-16, engram-alpha#2013/#2009 — the cascade machine)

After a retract or supersede, the engine walks downstream and marks:

> **The marker row carries NO severity — it never did.** A `cascade_markers`
> row records `root_op` (`'retract'` or `'supersede'`) and a `status` of
> `pending` or `resolved`; there is no severity column. `tainted_by` and
> `stale_by` below are **derived read-time views** over `root_op`, computed by
> `_cascade_marker_severity()`, not two kinds of stored marker. Any prose
> calling these "taint-severity" or "supersede-severity" *rows* is describing
> a shape that does not exist in the table.
>
> **And at the DESIGN level the severity itself is deprecated** (#2013,
> settled 2026-08-26): one `pending` marker, blocking either way, with the
> retract-vs-supersede difference living in *which resolution shapes are
> offered at the node's own turn* — not in the marker. The names below
> describe **current code**, which the design explicitly permits ("prose that
> describes current code legitimately still uses them"); they are not the
> direction of travel. Expect them to go when the resolution path lands.

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
  zero. **Untouched by the cascade-markers migration** — this is a genuinely
  separate mechanism (still a plain `metadata.support_lost` flag written by
  `_detect_zero_support`), not part of the retract/supersede cascade marker
  system at all. Cornerstones and lessons accumulate votes rather than
  derive_from premises, so they never receive `tainted_by` / `stale_by`
  markers from individual instance retractions.

Retracted nodes are preserved, not deleted — the engine checks `is_current`
status when walking edges. Removing `exemplifies` / `supported_by` edges is
unnecessary and discouraged; the retracted node carries its own status, and
traversal respects it.

**Where the marker actually clears (nuance worth having before you read
"clears automatically" below):** in this slice, a `cascade_markers` row is
never written `status='resolved'` — nothing yet implements that write path.
"Clears" here means the marked node itself goes `is_current=0` (superseded
or retracted), at which point every reader that surfaces `tainted_by`/
`stale_by` (`engram_reflect`, `engram_inspect`, the health score) filters on
`is_current=1` and the node simply stops appearing — not that the marker row
changes state. The end-to-end effect described below is accurate; the
mechanism underneath it is "the node dropped off is_current", not "the
marker resolved".

# Why derive-new + supersede — and never surgery

Every resolution pattern below clears a marker by **derive-new + supersede** (or
by retracting the dependent node), never by an in-place edit or a surgical /
raw-DB fix. The reason is structural, and it's what makes the rule stick:

ENGRAM's graph is a **time-ordered causal DAG** — a node may only depend on
nodes created *before* it. When you retract premise `P` and replace it with `P'`
(created **now**), the replacement `P'` is *later* than the downstream
derivation `D` you're repairing. So you cannot simply re-point `D` at `P'`: that
makes an earlier node (`D`) depend on a later one (`P'`) — a causal edge
pointing backwards in time, a DAG violation. The guarantee that "every belief
rests only on evidence that already existed when the belief was formed" would
break silently. Deriving a **new** node `D'` *now* (so `D'` is later than `P'`),
citing the live/corrected premises, and superseding `D` with `D'` keeps the
ordering valid and preserves `D` as an honest audit trail.

⛔ **Do NOT use `tools/surgical.py` or raw `knowledge.db` edits to clear a
marker.** Two traps:

- **Re-pointing an edge** to a newer replacement creates the backwards-in-time
  DAG violation above — the very thing the native flow exists to prevent.
- **Just deleting a `tainted_by` marker row** without re-establishing the conclusion
  hides the problem instead of fixing it: the node still rests on evidence
  you've disowned. That converts a *loud* failure (a visible taint marker) into
  a *silent* one (a confident-looking node built on sand) — exactly the
  corruption the honesty axiom warns against.

Surgery bypasses every guardrail the MCP layer enforces (DAG validity, taint
cascade, supersede semantics, type preservation, confidence propagation). For
taint-clearing it is never the right tool.

**Worked example:**

```
t0  ob_X    "X measured at 5.0"         (premise)
t1  dv_Y    derives_from ob_X → "Y"     (conclusion)
t2  ob_X retracted (wrong_evidence); replacement ob_X_new "X = 7.0" filed at t2
    → dv_Y's engram_reflect/engram_inspect tainted_by now shows: [ob_X]
```

WRONG (surgery): re-point `dv_Y → ob_X_new`. But `dv_Y` (t1) is *earlier*
than `ob_X_new` (t2) → backwards causal edge → DAG violation.

RIGHT (native): at t3, file `dv_Y_new` deriving from `ob_X_new` → "Y′", then
`engram_supersede(dv_Y, dv_Y_new, reason)`. `dv_Y_new` (t3) is later than
`ob_X_new` (t2) → valid. `dv_Y` drops out of the tainted-nodes surface when
it goes non-current; both old nodes are preserved as the audit trail.

# Decision tree by target type

For each node with a cascade marker, the resolution path depends on the
node's type:

## Pattern A — Derivation downstream of retracted/superseded upstream

When `engram_reflect`'s `tainted_nodes` shows `{"id": "dv_X", "tainted_by": ["ob_Y"], ...}` (ob_Y was retracted) or its `stale_nodes` shows `{"id": "dv_X", "stale_by": ["dv_old"], ...}` (dv_old was superseded) — or equivalently `engram_inspect(dv_X)`'s `warnings.tainted_by` / `warnings.stale_by`:

1. `engram_inspect(dv_X)` — read the derivation's claim and logical chain
2. `engram_inspect` each remaining live premise — does the claim still hold?
3. If YES (claim survives without the retracted/superseded premise):
   - File a new derivation via `engram_derive(...)` citing only the live
     premises (and any corrected replacements)
   - Wire the supersede: `engram_supersede(dv_X, new_dv_id, supersede_reason)`
   - dv_X drops out of the tainted/stale surfaces automatically once it
     becomes `is_current=0`
4. If NO (claim depends on the invalid premise and cannot stand alone):
   - ```
     engram_retract(payload_json=json.dumps({
         "node_id": "dv_X",
         "error_type": "hallucinated_claim",
         "reason": "premise retracted; claim no longer supported",
     }))
     ```
   - dv_X drops out on retraction the same way

The cascade auto-clears: once dv_X is either superseded or retracted, it no
longer shows up as tainted/stale anywhere and no further action is needed.

## Pattern B — Cornerstone or lesson with support_lost

When you see `cs_X.support_lost = true` or `ls_X.support_lost = true`:

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
       superseded or retracted): retract the cornerstone/lesson with a clear
       retraction reason.

## Pattern C — Observation with a tainted/stale marker (rare, audit upstream)

If an observation has a cascade marker, this is unusual — observations
`derive_from` nothing (they are filed from primary sources, not inferred from
other claim-bearing nodes). A cascade marker on an observation usually means
the upstream retract was wrong or the observation was mistakenly modeled as
deriving from another node.

1. `engram_inspect` the marked observation
2. `engram_inspect` the upstream node that caused the marker
3. If the upstream retract was correct AND the observation's claim is
   independently invalidated: `engram_retract(ob_X, ...)`
4. If the upstream retract was correct but the observation stands on its
   own independent evidence: this is unusual. Surface to the user — the
   marker may have been set by a modeling error (the observation should not
   have been a downstream dependent of that node).

## When in doubt

Don't act. Surface the cascade marker to the user. Pattern resolution is a
human-judgment moment whenever the cleanup involves identity-layer
cornerstones, high-confidence load-bearing derivations, or any situation
where the right rewire is genuinely unclear.

The dream-master fairy's role is to surface flagged nodes via `engram_reflect`
and route them to this playbook — not to resolve them autonomously when the
judgment call is non-trivial.
