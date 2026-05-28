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

# Background: how cascade markers fire (PR #421 semantics)

After a retract or supersede, the engine walks downstream and sets markers:

- **`tainted_by`** (list of retracted node IDs) — set on derivations that
  `derive_from` a retracted node. Means: one or more premises are now invalid.
- **`stale_by`** (list of superseded node IDs) — set on derivations that
  `derive_from` a superseded node. Means: a premise was updated; the derivation
  may or may not still hold.
- **`support_lost`** (boolean) — set on a cornerstone or lesson when ALL its
  live support edges (`supported_by` outgoing + `exemplifies` incoming) drop to
  zero. Cornerstones and lessons accumulate votes rather than derive_from
  premises, so they never receive `tainted_by` / `stale_by` from individual
  instance retractions.

Retracted nodes are preserved, not deleted — the engine checks `is_current`
status when walking edges. Removing `exemplifies` / `supported_by` edges is
unnecessary and discouraged; the retracted node carries its own status, and
traversal respects it.

# Decision tree by target type

For each node with a cascade marker, the resolution path depends on the
node's type:

## Pattern A — Derivation downstream of retracted/superseded upstream

When you see `dv_X.tainted_by = [ob_Y]` (ob_Y was retracted) or
`dv_X.stale_by = [dv_old]` (dv_old was superseded):

1. `engram_inspect(dv_X)` — read the derivation's claim and logical chain
2. `engram_inspect` each remaining live premise — does the claim still hold?
3. If YES (claim survives without the retracted/superseded premise):
   - File a new derivation via `engram_derive(...)` citing only the live
     premises (and any corrected replacements)
   - Wire the supersede: `engram_supersede(dv_X, new_dv_id, supersede_reason)`
   - The `tainted_by` / `stale_by` marker clears automatically when dv_X
     becomes `is_current=0`
4. If NO (claim depends on the invalid premise and cannot stand alone):
   - `engram_retract(dv_X, error_type="hallucinated_claim",
     reason="premise retracted; claim no longer supported")`
   - The marker clears automatically on retraction

The cascade auto-clears: once dv_X is either superseded or retracted, the
marker disappears and no further action is needed.

## Pattern B — Cornerstone or lesson with support_lost

When you see `cs_X.support_lost = true` or `ls_X.support_lost = true`:

1. `engram_inspect(cs_X / ls_X)` — read the cornerstone/lesson claim
2. `engram_query` for similar patterns in the graph — are there live
   observations or derivations that exemplify this cornerstone's claim?

   **If YES — trivial rewire:**

   3a. Register the exemplar against the pattern node:
       ```
       engram_register_exemplar(
           target_id=<cs_X or ls_X>,
           exemplar_id=<ob_or_dv_id>,
           note="<why this instance fits the pattern>"
       )
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

## Pattern C — Observation with tainted_by/stale_by (rare, audit upstream)

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

The gemini track runs dream-cycle consolidation inline (see engram-sleep
Phase B, Step 6 "Work the dream agenda"). During that phase, `engram_reflect`
surfaces flagged nodes — this skill is the playbook for what to do with them.
Don't resolve autonomously when the judgment call is non-trivial.
