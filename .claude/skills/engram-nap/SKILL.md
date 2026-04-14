---
name: engram-nap
description: Quick consolidation — persist what you learned this session into ENGRAM before stopping. Use at the end of a work burst or before /compact. Does NOT advance the turn counter.
---

# ENGRAM Nap — Quick Consolidation

A nap converts everything you learned, decided, or discovered in the current session from your context window into durable ENGRAM nodes. Anything not written will be lost on compaction.

---

## Step 1 — Scan recent work

Before writing anything, answer these questions about the current session:

1. **What did I learn?** New facts from reading code, running tests, ingesting sources, or user statements. These become observations (`engram_add_observation`).
2. **What did I decide or conclude?** Design choices, interpretations connecting multiple observations. These become derivations (`engram_derive`).
3. **What gaps did I identify?** Open questions, missing evidence, unknown behavior. These become questions (`engram_ask`).

If all answers are "nothing new," that's fine — proceed to Step 3 with a brief summary.

## Step 2 — Write everything that matters

For each candidate from Step 1, write NOW. Don't defer.

- **Observations** (factual claims with a source): `engram_add_observation` with `url` + `title` for the source.
- **Derivations** (inferences from existing nodes): `engram_derive` with `supporting_ids` and a clear `logical_chain`.
- **Questions** (open gaps): `engram_ask` — research directives for future sessions.
- **Definitions** (term conventions): `engram_add_definition` if you established shared vocabulary.

**Priority if context is tight:** observations > questions > derivations. Raw facts are irrecoverable if lost; derivations can be re-inferred later if the premises are preserved.

## Step 3 — Checkpoint

```
engram_checkpoint(
    message="<1-3 sentence summary of what this session covered and what was written>",
    mode="nap",
)
```

The `nap` mode:
- Logs the summary to the session log
- Returns graph stats
- Does NOT advance the turn counter — no forgetting triggered

## Step 4 — Report to the user

Tell the user briefly:
- What was written (counts or highlights, not a wall of text)
- Whether to continue working, run /compact, or end the session
