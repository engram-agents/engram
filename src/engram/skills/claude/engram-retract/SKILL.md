---
name: engram-retract
description: Use when you discover an error in ENGRAM — wrong evidence URL, fabricated quote, wrong citation, hallucinated claim, or duplicate node. The retract itself is one MCP call; the downstream taint cascade is automatic. Clearing that taint is a SEPARATE canonical flow (see engram-resolve-cascade) — derive-new + supersede, NEVER surgical / raw-DB edits.
---

# ENGRAM Error Correction Workflow

You discovered an error in your ENGRAM memory. The retract itself is mechanical
(Steps 0–3). Clearing the taint it leaves on downstream nodes is a **separate**
workflow with one canonical path — see "Clearing the taint" at the end; do not
improvise it here.

## Step 0 — Verify the target node

If you haven't already inspected the node this turn, do so before retracting —
a typo'd node ID would silently retract the wrong node. Inspect it (pass the
node ID to `engram_inspect`), then confirm the claim matches the error you
intend to fix, the node is `is_current=1`, and its type is what you expected.

## Step 1 — Classify the error type

- `fabricated_quote` — quoted_text not found in evidence source
- `wrong_citation` — claim doesn't follow from the quote
- `wrong_evidence` — cited the wrong source (includes fake/fabricated URLs)
- `hallucinated_claim` — claim not supported by evidence
- `duplicate` — same claim already existed
- `other` — anything else

## Step 2 — Call engram_retract

```
engram_retract(payload_json=json.dumps({
    "node_id": "<node_id>",
    "error_type": "<from step 1>",
    "reason": "<honest description of the error>",
}))
```

The engine:
- Marks the node retracted (preserved, not deleted — honest audit trail).
- Propagates `tainted_by` to downstream derivations (cornerstones/lessons are
  skipped per the vote-accumulator semantics; they get `support_lost` instead).
- Detects zero-support cornerstones/lessons and sets `support_lost`.
- Returns the full cascade scope — the downstream nodes now flagged.

**If the underlying claim is still valid** (the node was wrong about its
evidence/citation, not about the fact), file the corrected replacement node and
use it when re-establishing any tainted downstream node (next section).

## Step 3 — Read the cascade scope

The engine's response tells you which downstream nodes were tainted. You usually
don't resolve them in the same breath — that's the separate flow below, most
often performed during the dream cycle when the dream-master surfaces the
flagged nodes. If the retraction is identity-layer (a cornerstone, axiom, or
foundational goal), surface to the user before acting.

## Clearing the taint (the one canonical path)

A retract taints downstream derivations. There is exactly ONE correct way to
clear that taint, and the full procedure — by node type, with the DAG rationale
and a worked example — is the **single source of truth in
[engram-resolve-cascade](../engram-resolve-cascade/SKILL.md)**. In one line:

> **Derive a NEW node from the corrected premises, then `supersede` the old
> tainted one.**

⛔ **Never clear taint by surgery.** Do not edit a tainted node in place, and do
not use `tools/surgical.py` or raw `knowledge.db` edits to delete a `tainted_by`
flag or re-point an edge. Both corrupt the graph silently — re-pointing an older
node at a newer replacement violates the time-ordered DAG, and deleting the flag
without re-establishing the conclusion leaves a confident-looking node resting
on disowned evidence (a loud failure turned silent). Route through
**engram-resolve-cascade**; don't improvise.

## Principles

- **Never modify the database directly.** ENGRAM tools maintain provenance and
  the time-ordered DAG; surgery bypasses both.
- **Retracted nodes are preserved, not deleted.** Honest audit trail.
- **Be honest about the error.** The retraction reason carries forward;
  future-you and the user will want to know what went wrong.
- **The cascade is automatic; resolving it is deliberate.** The engine flags the
  downstream nodes; you (or the dream-master) clear them via the canonical
  derive-new + supersede flow in engram-resolve-cascade — never by surgery.
