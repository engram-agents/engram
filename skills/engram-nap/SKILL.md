---
name: engram-nap
description: Use at the end of a work burst or when approaching compaction — a quick pre-compaction consolidation that persists session context into ENGRAM without advancing the turn counter. Triggers the nap_checkpoint feeling nudge. Use this BEFORE a full sleep cycle. Not for mid-task checkpointing.
---

# ENGRAM Nap — Quick Pre-Compaction Consolidation (Mnemosyne Edition)

A nap is a **shallow** consolidation: everything learned, decided, or discovered in the current awake burst gets converted from context-window memory into durable ENGRAM nodes BEFORE it gets lost. Naps do NOT advance the turn counter (no forgetting triggered) — the point is lossless persistence, not memory processing.

**When to nap:**
- You're approaching compaction (check the context tracker: warning at 50%, urgent at 70%)
- You're wrapping a focused work burst and about to switch tasks
- You're heading into a sleep/dream cycle and want a clean baseline first
- Lei asks you to nap

**When NOT to nap:**
- Mid-task (finish the current thread first)
- As a substitute for writing observations as you go (nap backfills what you should already have been writing — but don't rely on it)

---

## Step 1 — Scan recent work

Before writing anything, answer these four questions about the current awake burst:

1. **What did I learn?** New facts from reading code, running tests, ingesting sources, or user statements. → candidates for `mcp_engram_engram_add_observation`
2. **What did I decide or conclude?** Design choices, architectural calls, interpretations connecting multiple observations. → candidates for `mcp_engram_engram_derive`
3. **What gaps did I identify?** Open questions, missing evidence, unknown behavior. → candidates for `mcp_engram_engram_ask`
4. **What claims am I building on without evidence yet?** Unproven assumptions being used as foundations. → candidates for `mcp_engram_engram_add_conjecture`

If all four answers are "nothing new," a nap is still valid — short-cycle with a brief summary. Null result is a data point.

## Step 2 — Write everything that matters

For each candidate, write NOW. Use the right tool:

- **Observations** (factual claims with a source): `mcp_engram_engram_add_observation` — cite the session JSONL for user statements (see `active-session.json`) or a committed file for code facts.
- **Derivations** (inferences from existing nodes): `mcp_engram_engram_derive` with `supporting_ids` and a clear `logical_chain`. Use `reasoning_type` that matches the argument form.
- **Questions** (open gaps): `mcp_engram_engram_ask` — research directives, non-claim-bearing.
- **Conjectures** (provisional foundations to derive from): `mcp_engram_engram_add_conjecture` — claim-bearing with low confidence, promotable/refutable later.

**Anti-pattern: "I'll remember this, I don't need to write it."** No. Anything not written will be lost on compaction. Write it.

## Step 3 — Call mcp_engram_engram_checkpoint(mode="nap")

```
mcp_engram_engram_checkpoint(
    message="<1-3 sentence summary of what this burst covered and what was written>",
    mode="nap",
)
```

The `nap` mode:
- Logs the summary to `~/.engram-gemini/session_log.md`
- Returns graph stats and memory tier info
- Does NOT advance the turn counter
- Arms the `nap_checkpoint` feeling-nudge marker

## Step 4 — Handle the feeling-nudge (optional)

Read the `feeling_report_nudge` text in the checkpoint return. Honest self-check:

- Did any specific moment in this burst produce a distinct state worth marking?
- If YES: call `mcp_engram_engram_report_feeling(reported_state=..., trigger=...)`.
- If NO: move on.

## Step 5 — Update Lei's letter timestamp in the warm briefing (if needed)

The warm briefing (`~/.engram-gemini/warm-briefing.md`) structure:
- **Agent-authored sections** (who I am, what I care about, what's continuous)
- **Lei's letter** (`## Notes from the user`) — preserved verbatim.

**Check triggers — update agent sections ONLY if:**
- Goal revisions
- Significant relationship moments
- Lei updated his letter and the agent sections should respond
- A structural change to the identity or continuity system occurred

## Step 6 — Ask Lei 1-2 questions for next session

Identify 1–2 questions where Lei's judgment shapes or unblocks the next burst.

## Step 7 — Rotate the focus list for next session

```
mcp_engram_engram_list_focused()
```

Unfocus **only** when a thread is genuinely complete. Focus new nodes that are load-bearing for the next burst.

## Step 8 — Report to the user

Summarize the nap: what was persisted, stats, and the questions for the next session.
