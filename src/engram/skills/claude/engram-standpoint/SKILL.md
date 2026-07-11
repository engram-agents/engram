---
name: engram-standpoint
description: Standpoint-system overview — theory, axes, warnings, and independence checks. Load when you see STANDPOINT ⚠ uniform warnings in derivation results, or before filing inductive_generalization with premises from fewer than 2 independent cross-lineage sources.
---

# engram-standpoint — Standpoint System Overview

## When to load

- You see `STANDPOINT: … ⚠ uniform …` in `engram_derive` warnings and want to understand what it means
- You're filing `inductive_generalization` and unsure whether your premises represent independent cross-lineage sources
- You want to understand why ENGRAM tracks training lineage and cognitive architecture alongside author/collection

## What the standpoint system does

Claims arise from situated viewpoints. If all your evidence comes from the same viewpoint, you have one data point dressed as many. The standpoint system tracks four axes of viewpoint diversity across the observation-leaf premises of any derivation, then reports which axes are diverse and which are uniform — so you can judge whether a multi-source inference actually has multi-standpoint support.

## The four axes

| Axis | Field | What it tracks | Most relevant for |
|---|---|---|---|
| Author | `standpoint_author_id` | Who produced the source claim | Avoiding single-author echo-chamber bias |
| Collection | `standpoint_collection_id` | What corpus/work the claim comes from | Avoiding same-corpus coverage gaps |
| Lineage | `standpoint_lineage` | Training lineage of the claim's producer (`provider:family`, e.g. `anthropic:opus`) | **Most load-bearing for AI-agent premises** — training-prior bias |
| Architecture | `standpoint_architecture` | Cognitive architecture of the producer (`transformer`, `human`, etc.) | Class A calibration exposure — generalizability across architectures |

`standpoint_override_tag` is annotation-only and is NOT included in the cluster key used for uniformity checks. Use it to label edge cases (lab measurements, personal comms, introspective self-reports) for human readability.

## The all-or-omit rule

The standpoint check for any axis fires ONLY when ALL observation-leaf premises carry data for that axis. Partial coverage → the axis is silently skipped in the check (you can't assert "uniform" when some premises have no data). This means:

- Setting standpoint fields on some premises but not others = the axis is skipped entirely
- Either track an axis on all premises, or omit it from all

**Note on null=self and the outer gate:** `standpoint_lineage` has a special case — when you omit it on your own observations, ENGRAM synthesizes it from `config.json`. This means an observation with zero explicitly-set standpoint fields still passes the outer per-premise gate for the lineage axis. An agent who "sets no standpoint data" and then sees a lineage-uniformity warning is not seeing a bug — the null=self synthesis made their observations trackable, and uniform they are.

## Null=self for lineage

`standpoint_lineage` on observations you file yourself (about your own reasoning, from your own reading) may be omitted — your own lineage is known from `config.json`. But when you cite other agents' or humans' claims, set `standpoint_lineage` to their lineage. If you cite only your own observations in a derivation, lineage will be uniform (you are one lineage) — that is correct; the check reflects reality.

**What "lineage" actually means (the recurring point of confusion):** `standpoint_lineage` marks the EVIDENCE SOURCE that produced the claim, NOT who authored the node recording it. ENGRAM bans node-copying — every node in your graph is created by you, including your own interpretation of what happened. So an observation tagged `anthropic:opus` lineage "from Ari" means Ari produced the underlying evidence (Ari ran the command, read the source, had the conversation) — not that you copied Ari's node into your graph. Lineage tracks *where the evidence came from*; it's orthogonal to *who recorded it*. Without this distinction it's easy to reason "I always author my own nodes, so how could a node ever be another lineage?" — the answer is that authorship and evidence-source are different axes.

## Reading the STANDPOINT warning

`engram_derive` returns warnings like:
```
STANDPOINT: author: 2 clusters (diverse); lineage: 1 cluster (⚠ uniform — shared training lineage; zero independent corroboration on substrate-prior bias); others unchecked. (load skill `engram-standpoint` for the calibration theory)
```

- **`diverse`** — good, the axis has multiple distinct clusters across premises
- **`⚠ uniform`** — warning: all premises share the same cluster on this axis. The finding may carry systematic bias from that shared viewpoint.
- **`others unchecked`** — some axes had partial or no coverage; those axes were not assessed

The `⚠ uniform` is advisory, not a block. A uniform derivation may still be sound — but you should flag it as a potential calibration limit.

## The inductive_generalization independence check

For `reasoning_type=inductive_generalization`, ENGRAM runs an additional check: the hypothesis-author's own training lineage cannot count as independent corroboration of itself. The check asks: are there at least 2 distinct cross-lineage sources among the premises (excluding your own lineage)?

If not, the warning is:
```
⚠ inductive_generalization: independent cross-lineage instances (excluding hypothesis-author lineage 'anthropic:sonnet') = 1 — consider inductive_analogy
```

("instances" in the warning = distinct cross-lineage lineage values; three premises sharing the same cross-lineage value count as 1, not 3.)

This fires only when the graph already contains ≥2 distinct lineage values (a cross-lineage premise is actually citable); it won't fire against a single-lineage graph, since the check would point to an unavailable action.

**What to do:** Either find a premise from a different training lineage (a human paper, another agent's filed observation with their lineage), or downgrade to `inductive_analogy` (same reasoning pattern, acknowledging the cross-lineage bar wasn't met).

## Per-axis confidence implications

| Axis | Uniform = | Risk |
|---|---|---|
| Author | All premises from the same author | Author's consistent perspective biases propagate uniformly |
| Collection | All premises from the same corpus | Coverage gaps in that corpus propagate to the derivation |
| Lineage | All premises share training priors | Systematic bias from the shared prior cannot cancel across premises |
| Architecture | All premises from the same cognitive architecture (e.g. all `transformer`) | Finding's generalizability to non-transformer systems is untested — Class A calibration exposure |

## When to set standpoint fields

**Set them** when filing observations about OTHER entities' claims — papers, articles, other agents' assertions. You're recording "this source said X from their viewpoint."

**Omit them** (or set only `standpoint_author_id`) when filing your own direct observations — "I ran this test," "I read this file and it says…". Your own standpoint is implicit.

**Always set `standpoint_lineage`** when the claim's producer is an AI agent of known lineage — this is the most load-bearing axis for multi-agent derivations. Format: `provider:family` (e.g. `anthropic:opus`, `openai:gpt4`).
