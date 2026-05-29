---
name: engram-retract
description: Use when you discover an error in ENGRAM — wrong evidence URL, fabricated quote, wrong citation, hallucinated claim, or duplicate node. The retract action itself is one MCP call; the cascade is automatic. For cleaning up cascade markers that arrive later (during dream cycle), see engram-resolve-cascade.
---

# ENGRAM Error Correction Workflow

You discovered an error in your ENGRAM memory. The action is mechanical:

## Step 0 — Verify the target node

If you haven't already inspected the node in the current turn, do so before
retracting — a typo'd node ID would silently retract the wrong node:

```
engram_inspect(node_id)
```

Confirm the claim matches the error you intend to fix, the node is `is_current=1`,
and its type is what you expected.

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
- Marks the node retracted
- Propagates `tainted_by` to downstream derivations (cs/ls are skipped per
  the vote-accumulator semantics)
- Detects zero-support cs/ls and sets `support_lost`
- Returns the full cascade scope

## Step 3 — Surface cascade scope (if any) for dream-cycle resolution

The engine's response tells you which downstream nodes were affected.
You don't need to act on them now — cascade resolution is a separate
workflow (see engram-resolve-cascade skill), most commonly performed
during the dream cycle when you work the dream agenda via engram_reflect.

If the retraction is identity-layer (a cornerstone, axiom, or
foundational goal), surface to the user before acting.

## Principles

- **Never modify the database directly.** ENGRAM tools maintain provenance.
- **Retracted nodes are preserved, not deleted.** Honest audit trail.
- **Be honest about the error.** The retraction reason carries forward;
  future-you and the user will want to know what went wrong.
- **The cascade is automatic.** You don't walk it; the engine does.
  See engram-resolve-cascade for what to do with the markers later.
