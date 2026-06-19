---
name: engram-contradiction-resolution
description: "Drive an ENGRAM contradiction (ct_XXXX) to a clean substrate state - the tool-call sequence for investigating, resolving, and verifying a contradiction node. Load when a contradiction needs resolution."
---

# ENGRAM — Contradiction resolution

How to drive a contradiction (ct_XXXX) to a clean substrate state. Focused on the **ENGRAM operation sequence**, not the philosophical logic of the resolution itself — the latter is yours to author; this skill describes which tools to call in what order.

The skill exists because the chain-dilution contradiction-resolution saga (2026-05-09 to 2026-05-20, seven resolution attempts) revealed two failure patterns:
1. **Chain dilution**: each resolution attempt cited the prior resolution chain, compounding confidence drop until threshold-based status couldn't fire.
2. **Substrate blindness to supersede**: a contradiction whose contradicting node had already been superseded continued to read as `open` / `partially_resolved` because the substrate didn't cascade supersede to the contradiction node.

The 2026-05-20 substrate change (issue #229) closes both: supersede/retract now cascade to contradictions (`stale_by_premise` / `tainted_by`), and `engram_resolve` is now pure-wire (callers compose the resolving derivation via `engram_derive` first, then wire it).

---

## When to use

- A contradiction (ct_XXXX) is `open` or `partially_resolved` and needs to be driven to resolution.
- A contradiction surfaced as `stale_by_premise` (one of its contradicting sides was superseded — supersede cascade fired).
- A contradiction surfaced as `tainted_by` (one of its contradicting sides was retracted — retract cascade fired).
- Dream-fairy-2 (contradictions-ripe-for-resolution) flagged a contradiction for action.

## Standard resolution path — open contradiction

When a contradiction is genuinely open and you want to resolve it.

### Step 1 — Inspect both sides + existing resolution attempts

```
engram_inspect(payload_json=json.dumps({"node_id": ct_id, "dream_mode": True}))
```

Read:
- The ct's `claim` (description of the conflict) and `status`.
- Both contradicting neighbors (the two claim-bearing nodes). Note their current `is_current` status and confidence.
- Any existing `resolves` edges incoming. If they exist, the contradiction may already be substantively resolved at the resolution-edge layer even if `status` doesn't show it (status reflects max-of-resolves confidence vs threshold).

Use `dream_mode=True` if this is part of a dream / maintenance pass — prevents importance inflation on the inspected nodes.

### Step 2 — Identify what's actually in conflict

The contradicting nodes carry the disagreement. Some patterns:
- **Empirical disagreement** — same observation, two different verdicts. Either both are right under different conditions (boundary clarification), or one is wrong (retract).
- **Interpretive disagreement** — same text/data, two different readings. Need a derivation that adjudicates which reading wins under what criterion.
- **Value-laden disagreement** — different value commitments lead to different verdicts. Often requires a synthesis derivation or explicit boundary statement.

### Step 3 — Compose the resolving derivation

```
engram_derive(payload_json=json.dumps({
    "claim": "<the resolution claim>",
    "supporting_ids": "<root-anchor IDs, comma-separated>",
    "logical_chain": "<explicit reasoning>",
    "reasoning_type": "<deductive_modus_ponens / abductive_best_explanation / etc.>",
}))
```

**Critical**: cite **root-anchor nodes** (the original contradicting observations or canonical derivations), NOT prior resolution attempts. Citing prior resolution attempts is the chain-dilution mechanism that broke the canonical saga — each step diluted confidence further from threshold.

If a high-confidence canonical node already exists that resolves the contradiction (e.g., the canonical resolving derivation that closed the chain-dilution saga), **skip step 3** and use that node directly as the resolving_node_id in step 4.

### Step 4 — Wire the resolves edge

```
engram_resolve(payload_json=json.dumps({
    "target_id": ct_id,
    "resolving_node_id": "<dv_id from step 3 OR existing canonical node>",
}))
```

`engram_resolve` is now pure-wire (issue #229) — it only writes the resolves edge and flips status. No derivation creation, no confidence recomputation. Status flips to `resolved` if the resolving node's confidence is ≥ 0.7 (default threshold), else `partially_resolved`.

`engram_resolve` uses **max-of-resolves**: if other valid resolvers exist, the target's status reflects the highest-confidence one. A weak later resolver cannot downgrade a target previously resolved by a strong one.

### Step 5 — Verify

```
engram_inspect(payload_json=json.dumps({"node_id": ct_id, "dream_mode": True}))
```

Confirm `status == "resolved"`. If still `partially_resolved`, the resolving node's confidence is below threshold — strengthen by composing a derivation citing stronger evidence (step 3 again) and wiring it (step 4).

---

## Stale-contradiction path — post-supersede

A contradiction is `stale_by_premise` when one of its contradicting nodes was superseded. The supersede cascade (issue #229) marks the contradiction stale so dream-fairy-2 reviews whether the supersede already did the philosophical work.

### Step 1 — Inspect the contradiction and the supersede chain

```
engram_inspect(payload_json=json.dumps({"node_id": ct_id, "dream_mode": True}))
```

Read `metadata.stale_by` (list of superseded contradicting nodes) and `metadata.stale_replacement` (the new node that supersedes the old). Then:

```
engram_inspect(payload_json=json.dumps({"node_id": stale_replacement_id, "dream_mode": True}))
engram_inspect(payload_json=json.dumps({"node_id": other_side_id, "dream_mode": True}))
```

The "other side" is the contradicting node that WASN'T superseded.

### Step 2 — Decide: substantive-resolve vs preserve-and-rewire

Per the supersede no-drop discipline, the new node MUST have either **kept** or **altered** the conflicting claim from the old node — drop is forbidden. So exactly one of two cases applies:

**Case 1 (substantive resolve): the new node altered the conflicting claim.**

The new node no longer contradicts the other side — the supersede already did the philosophical work. Action: wire `engram_resolve` from the new node to the contradiction.

```
engram_resolve(payload_json=json.dumps({
    "target_id": ct_id,
    "resolving_node_id": stale_replacement_id,
}))
```

This is the canonical chain-dilution case: an earlier derivation was superseded by a later one that altered the bifurcation framing from "strict Kahl satisfaction" to "our owned extension"; the superseding derivation no longer contradicted the strict-text-reading observation.

**Case 2 (preserve and rewire): the new node kept the conflicting claim.**

The contradiction persists, just with the new node as the contradicting side. Action: create a new contradiction between the new node and the other side, then supersede the old contradiction → new contradiction.

```
new_ct = engram_contradict(payload_json=json.dumps({
    "node_id_a": stale_replacement_id,
    "node_id_b": other_side_id,
    "description": "<updated description reflecting new framing>",
}))

engram_supersede(payload_json=json.dumps({
    "old_node_id": ct_id,
    "new_node_id": new_ct["contradiction_id"],
    "supersede_reason": "Re-wired after <old_side> was superseded by <new_side>; conflict preserved with updated framing.",
}))
```

The old contradiction is now `is_current=0`; the new contradiction carries the live conflict forward. Continue with the standard resolution path on `new_ct` if you want to drive it to closure.

### Step 3 — Clear the stale flag (case 1 only)

When case 1 fires and `engram_resolve` flips status to `resolved`, the `stale_by_premise` flag is incidental — the contradiction is now closed. No explicit clear needed; the resolved status overrides the operational meaning of stale.

For case 2, the old contradiction is superseded — its metadata is preserved for audit but it's no longer is_current.

---

## Tainted-contradiction path — post-retract

A contradiction is `tainted_by` when one of its contradicting nodes was retracted. The retract was an error correction — that side was never valid.

### Step 1 — Inspect the contradiction and the retracted node

```
engram_inspect(payload_json=json.dumps({"node_id": ct_id, "dream_mode": True}))
engram_inspect(payload_json=json.dumps({"node_id": retracted_node_id, "dream_mode": True}))
```

Read `metadata.tainted_by` to confirm which node was retracted. Read the retracted node's `metadata.error_type` and `metadata.retraction_reason` to understand WHY it was invalidated.

### Step 2 — Decide: does the contradiction still hold?

The retracted side was never valid. The other side may or may not still be in genuine tension with reality. Three sub-cases:

**Sub-case A: the retracted node was the only side claiming conflict; the other side stands alone.**

There's no longer a contradiction — the conflict was illusory. Action: explicitly close the contradiction by writing a derivation that acknowledges this and wiring it via `engram_resolve`. The claim should be something like "Contradiction dissolved: the contradicting node (ID) was retracted as (error_type); the other side stands without challenge."

```
dv = engram_derive(payload_json=json.dumps({
    "claim": "ct_X dissolved: contradicting side was retracted as <error_type>; <other_side> stands.",
    "supporting_ids": "<other_side_id>",
    "logical_chain": "Retracted node was never valid evidence; the other side has no remaining challenge.",
    "reasoning_type": "deductive_modus_tollens",
}))

engram_resolve(payload_json=json.dumps({
    "target_id": ct_id,
    "resolving_node_id": dv["derivation_id"],
}))
```

**Sub-case B: a replacement observation was created (engram_retract with replacement_json).**

The retract may have produced a corrected replacement. If the replacement STILL contradicts the other side, follow the post-supersede case 2 pattern: new-contradict + supersede-old-contradiction.

**Sub-case C: the contradiction is genuinely moot now (the topic no longer matters, or both sides have been retracted).**

This is rare and usually means the area shifted significantly. Consider whether to file a retract on the contradiction itself, citing the moot status.

---

## Tools used by this skill

| Tool | When |
|------|------|
| `engram_inspect` | Always start with this to read state |
| `engram_derive` | Compose the resolving derivation when no existing canonical node fits |
| `engram_resolve` | Pure-wire the resolves edge (was renamed from old combo, see issue #229) |
| `engram_contradict` | Create a new contradiction when re-wiring (case 2 of stale path) |
| `engram_supersede` | Replace an old contradiction with a re-wired new one |
| `engram_retract` | Only for retracting the contradiction itself if it's invalidated |

---

## Anti-patterns the chain-dilution saga taught

1. **Citing the prior resolution chain in your new resolving derivation.** Chain dilution. Always cite root nodes.
2. **Wrapping an existing canonical node in another derivation to "make the resolve work."** Unnecessary. With pure-wire `engram_resolve`, you can pass the canonical node directly as `resolving_node_id`.
3. **Treating `partially_resolved` as automatically wrong / needing fix.** It might be the correct substrate state for the current resolution chain. Only act if the chain's confidence genuinely doesn't reflect the philosophical certainty — strengthen the chain by adding a derivation citing higher-confidence roots.
4. **Multiple "admin closes" without checking whether supersede already did the work.** With the cascade now in place, the substrate will mark the contradiction `stale_by_premise` after a relevant supersede; check for this state before composing a new resolution.

---

## For dream-fairy-2

When you scan for contradictions ripe for resolution, your decision tree:

1. `metadata.tainted_by` present? → tainted-contradiction path.
2. `metadata.stale_by` present? → stale-contradiction path. Decide case 1 (substantive resolve) vs case 2 (preserve and rewire).
3. `status == "open"` or `"partially_resolved"` with no cascade flags? → standard resolution path. But first check: has either contradicting node been superseded since the contradiction was created? If yes, the supersede may have happened BEFORE the cascade landed — treat as stale-contradiction path anyway.

Always cite root nodes in any new resolving derivation. Never chain off a prior weak resolution.
