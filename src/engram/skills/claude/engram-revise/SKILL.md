---
name: engram-revise
description: Use when you discover an error in ENGRAM (wrong evidence URL, fabricated quote, wrong citation, hallucinated claim, duplicate node — engram_retract) or when your understanding has genuinely evolved past a still-valid node (engram_supersede). Both are pure-wire revision verbs that open a cascade on downstream dependents; both are REFUSED outright if the target already carries a pending cascade marker of its own. Clearing that marker is a SEPARATE canonical flow — see engram-resolve-cascade.
---

# ENGRAM Revision Workflow — retract and supersede

Retract and supersede are the two verbs that revise a claim already in the
graph. They answer the same question from opposite sides — **"was I wrong, or
did I learn more?"** — and they share one mechanism: each opens a cascade on
the nodes that depend on the one being revised. This skill covers both. What
happens *downstream* of that cascade is a separate workflow — see "Clearing a
cascade marker" below; do not improvise it here.

| | `engram_retract` | `engram_supersede` |
|---|---|---|
| When | The node was **never valid** — wrong evidence, fabricated quote, hallucinated claim | The node **was valid at the time**; a newer node replaces it (new evidence, refined framing, scope-tightening) |
| Downstream marker | `root_op='retract'` (surfaced as `tainted_by`) | `root_op='supersede'` (surfaced as `stale_by`) |
| Creates the replacement? | No — retract only marks the error | No — pure-wire; create the replacement via its own canonical tool FIRST, then call this to link |

Retract vs supersede is decided by what was wrong with the old node, never by
preference — conflating the two corrupts downstream cascade adjudication.

## Step 0 — Verify the target node

If you haven't already inspected the node this turn, do so before revising —
a typo'd node ID would silently retract or supersede the wrong node. Inspect
it (`engram_inspect`), confirm the claim matches what you intend to fix, that
it's `is_current=1`, and that its type is what you expected.

## Step 1 — Check it isn't already mid-cascade

**Both verbs refuse outright** if the target already carries a pending cascade
marker of its own (cascade-machine invariant 7): a plain revision would let this
node's marker clear while everything else the same cascade touched stays
unadjudicated — a bypass wearing invariant 7's clothes. The refusal names the
pending events and hands you the two calls to make instead:

```
engram_cascade_frontier(payload_json='{"node_id": "<node_id>"}')   # mode C
engram_cascade_resolve(payload_json='{"node_id": "<node_id>",
    "resolution_kind": "retracted"|"superseded", ...}')
```

If `engram_inspect` already showed `warnings.tainted_by` or `warnings.stale_by`
on this node, expect the refusal — go straight to `engram-resolve-cascade`
instead of attempting Step 2.

## Step 2 — For a genuine error: classify and retract

- `fabricated_quote` — quoted_text not found in evidence source
- `wrong_citation` — claim doesn't follow from the quote
- `wrong_evidence` — cited the wrong source (includes fake/fabricated URLs)
- `hallucinated_claim` — claim not supported by evidence
- `duplicate` — same claim already existed
- `other` — anything else

```
engram_retract(payload_json=json.dumps({
    "node_id": "<node_id>",
    "error_type": "<from above>",
    "reason": "<honest description of the error>",
}))
```

**If the underlying claim is still valid** (the node was wrong about its
evidence/citation, not about the fact), file the corrected replacement node
and cite it when re-establishing any tainted downstream node — the same
derive-new-then-link pattern as the `superseded` shape in
`engram-resolve-cascade`'s worked examples (example A is the retracted-premise
case).

## Step 2′ — For genuine evolution: create the replacement, then supersede

```
engram_supersede(payload_json=json.dumps({
    "old_node_id": "<node_id>",
    "new_node_id": "<replacement, already created via its own creation tool>",
    "supersede_reason": "<what changed and why>",
}))
```

Pure-wire — this tool never creates the replacement. Create it first via its
type's canonical tool (`engram_add_observation`, `engram_derive`, etc.), then
call this to link. Retracted nodes are not supersede-able (use `engram_retract`
instead).

Two guardrails the tool enforces, both refused loudly rather than silently
allowed:

- **Type preservation** — `new_node_id` must be the same node type as
  `old_node_id`. Superseding a conjecture with a derivation (or any other
  cross-type revision) is not supersede; if the correction changes the
  epistemic category, retract the old node and cite the replacement
  organically in new derivations instead.
- **The supersede cycle guardrail** — the replacement must not be downstream of
  (transitively derive from) the node it replaces. Superseding a node with
  something that already depends on it would make the replacement stale by
  the very act that installs it.

## Step 3 — Read the cascade scope

Both calls return the full cascade scope — the downstream nodes now flagged.
You usually don't resolve them in the same breath; that's the separate flow
below, most often performed during the dream cycle when
`engram_cascade_frontier` surfaces eligible nodes. If the revision is
identity-layer (a cornerstone, axiom, or foundational goal), surface to the
user before acting.

## Clearing a cascade marker (the one canonical path)

A retract or supersede taints/stales downstream dependents. Clearing that is a
**separate** workflow with one entry point — the full procedure, the three
adjudication shapes, and worked examples are the single source of truth in
**[engram-resolve-cascade](../engram-resolve-cascade/SKILL.md)**. In one line:

> **Read the frontier (`engram_cascade_frontier`), then adjudicate each
> eligible node with `engram_cascade_resolve` — retracted, superseded, or
> acknowledged.**

⛔ **Never clear a marker by surgery.** Do not edit a marked node in place, and
do not use `tools/surgical.py` or raw `knowledge.db` edits to delete a
`cascade_markers` row or re-point an edge. Both corrupt the graph silently —
re-pointing an older node at a newer replacement violates the time-ordered
DAG, and deleting the marker without re-establishing the conclusion leaves a
confident-looking node resting on disowned evidence (a loud failure turned
silent). Route through **engram-resolve-cascade**; don't improvise.

## Principles

- **Never modify the database directly.** ENGRAM tools maintain provenance and
  the time-ordered DAG; surgery bypasses both.
- **Retracted and superseded nodes are preserved, not deleted.** Honest audit
  trail either way.
- **Be honest about the error, or about what changed.** The reason carries
  forward; future-you and the user will want to know.
- **The cascade is automatic; adjudicating it is deliberate**, and gated: a
  node already mid-cascade cannot be revised again by the plain verb until
  its own marker is adjudicated through `engram-resolve-cascade`.
