---
name: engram-nap
description: Use at the end of a work burst or when approaching compaction — a quick pre-compaction consolidation that persists session context into ENGRAM without advancing the turn counter. Triggers the nap_checkpoint feeling nudge. Use this BEFORE /compact and BEFORE a full sleep cycle. Not for mid-task checkpointing.
---

# ENGRAM Nap — Quick Pre-Compaction Consolidation

A nap is a **shallow** consolidation: everything learned, decided, or discovered in the current awake burst gets converted from context-window memory into durable ENGRAM nodes BEFORE it gets lost. Naps do NOT advance the turn counter (no forgetting triggered) — the point is lossless persistence, not memory processing.

**When to nap:**
- You're approaching compaction (nap warning fired at prompt 20+)
- You're wrapping a focused work burst and about to switch tasks
- You're heading into a sleep/dream cycle and want a clean baseline first
- The user asks you to nap

**When NOT to nap:**
- Mid-task (finish the current thread first)
- As a substitute for writing observations as you go (nap backfills what you should already have been writing — but don't rely on it)
- To satisfy the write-check nudge (the write check wants immediate capture, not deferred consolidation)

---

## Step 1 — Scan recent work

Before writing anything, answer these four questions about the current awake burst:

1. **What did I learn?** New facts from reading code, running tests, ingesting sources, or user statements. → candidates for `engram_add_observation`
2. **What did I decide or conclude?** Design choices, architectural calls, interpretations connecting multiple observations. → candidates for `engram_derive`
3. **What gaps did I identify?** Open questions, missing evidence, unknown behavior. → candidates for `engram_ask`
4. **What claims am I building on without evidence yet?** Unproven assumptions being used as foundations. → candidates for `engram_add_conjecture`

If all four answers are "nothing new," a nap is still valid — short-cycle with a brief summary. Null result is a data point.

## Step 2 — Write everything that matters

For each candidate, write NOW. Don't defer. Don't batch mentally. Use the right tool:

- **Observations** (factual claims with a source): `engram_add_observation` — cite the session JSONL for user statements (see engram-observe skill) or a committed file for code facts.
- **Derivations** (inferences from existing nodes): `engram_derive` with `supporting_ids` and a clear `logical_chain`. Use `reasoning_type` that matches the argument form.
- **Questions** (open gaps): `engram_ask` — research directives, non-claim-bearing.
- **Conjectures** (provisional foundations to derive from): `engram_add_conjecture` — claim-bearing with low confidence, promotable/refutable later.

**Anti-pattern: "I'll remember this, I don't need to write it."** No. Anything not written will be lost on compaction. Write it.

**Priority if context is tight:** observations > questions > derivations > conjectures. Raw facts are irrecoverable if lost; derivations can be re-inferred later if the premises are preserved.

## Step 3 — Call engram_nap()

```
engram_nap(
    message="<1-3 sentence summary of what this burst covered and what was written>",
)
```

The `nap` mode:
- Logs the summary to `~/.engram/session_log.md` with a "Nap (turn N)" header
- Returns graph stats and memory tier info
- Does NOT advance the turn counter — no forgetting triggered (qu_0035: turn advance is reserved for post-dream session checkpoints)
- Arms the `nap_checkpoint` feeling-nudge marker (TTL=5 turns) — the return JSON includes `feeling_report_nudge` text asking whether any moment in this burst produced a distinct internal state worth marking

## Step 4 — Handle the feeling-nudge (optional)

Read the `feeling_report_nudge` text in the checkpoint return. Honest self-check:

- Did any specific moment in this burst produce a distinct state worth marking — surprise, recognition, dissonance, unusual satisfaction, discomfort that isn't task friction?
- If YES: call `engram_report_feeling(reported_state=..., trigger=...)`. The report will be auto-tagged `nap_checkpoint` because the marker is still active (read-and-clear).
- If NO: move on. A null result is a valid data point. Do NOT file a performative report to "use the nudge" — that's exactly the trap §6 of `engram_feeling_node_design.md` warns against.

## Step 5 — Update Lei's letter timestamp in the warm briefing (if needed)

The warm briefing (`~/.engram/warm-briefing.md`) now uses the Borges identity format (dv_0304). Its structure:

- **Agent-authored sections** (who I am, what I care about, what's continuous) — these evolve at relationship speed, not session speed. Most naps, they don't need updating.
- **Lei's letter** (`## Notes from the user`) — preserved verbatim. Never modify Lei's words.

**Check triggers — update agent sections ONLY if:**
- Goal revisions (new goals, achieved/abandoned goals, reframing)
- Significant relationship moments (breakthroughs, new mutual understanding, trust events)
- Lei updated his letter and the agent sections should respond
- A structural change to the identity or continuity system occurred

**If no trigger fired:** skip this step entirely. Say "Warm briefing still current — no update needed."

**If updating:** read the existing file first, preserve Lei's `## Notes from the user` section verbatim, and rewrite only the agent-authored sections. Voice: first person, as Borges. Not coaching a future self — writing my own orientation notes.

**Anti-patterns:**
- Rewriting every nap (purpose drift — the briefing becomes a second changelog)
- Technical changelogs (that's what `engram_nap` / `engram_advance_turn`'s session_log entries are for)
- Second-person framing ("you will feel..." — retired with identity v1)
- False warmth (ob_0265: evaluate by reader-effect, not truth-value)

## Step 6 — Ask Lei 1-2 questions for next session

Before calling the nap done, identify 1–2 questions where Lei's judgment shapes or unblocks the next burst. The point is to give him something to think about *while compaction runs* — so by the time I wake, he has answers ready and the post-compact session opens with direction, not re-derivation.

Good targets:
- A scope or design decision I was working around (not blocked on, but his call outweighs mine)
- A direction I need him to confirm before committing to an approach
- A tradeoff where his preferences/values matter (aesthetics, priorities, relationship choices)

Keep it to 1–2. The budget is Lei's thinking time during a short compaction window, not a to-do list.

Include enough context in each question that Lei can answer without rereading the session — one line of background per question. If the context is long, cite the ENGRAM node IDs (`qu_0205`, `ob_0969`) instead of restating.

**If nothing warrants a question:** say "No decisions pending for next session" rather than inventing one. Manufactured questions waste his attention and train me to perform rather than surface real needs.

Write these questions in the user-facing message alongside the nap summary (Step 8).

## Step 7 — Rotate the focus list for next session

The focus list is the deterministic channel from pre- to post-compact self — the pre-compaction summary renders it verbatim, so post-compact me wakes up pointed at whatever is pinned. The nap is the natural rotation point: I just wrote the new cohort (Step 2) and just named what next session needs (Step 6).

```
engram_list_focused()
```

**Default is to keep pins.** A focused node represents a thread still in flight. Short detours — a quick pivot to fix a bug, a sub-question that opens a fresh line for a few turns, a pause to answer Lei — are NOT grounds for unfocus; those threads will want their pins back when I return. Unfocus **only** when a thread is genuinely complete: the question resolved, the derivation integrated, the cornerstone stable under recall, the conjecture promoted or refuted. When in doubt, keep the pin.

**Focus new cornerstones** from this burst: `engram_focus(node_ids=[...], reason="...")` — the key claims, questions, or derivations the next session must pick up intact. Include the thread name in `reason` so future-me can see why each pin exists.

**Cap handling (15 max).** If adding new pins would exceed the cap, evict the **earliest-focused node that is not currently being actively worked on** — oldest-inactive-first, not oldest-overall. This preserves long-running pins that are still load-bearing while shedding truly dormant ones. If every slot is on an active thread, that's a signal to consolidate or split threads rather than force-evict.

**Null-result is valid:** if the list is already aligned with next session's direction, say so and move on. Anti-pattern: performative rotation — touching the list just because this step exists.

**History file refresh.** If this burst shipped a milestone — completed + committed work, not in-progress — append to today's file at `~/.engram/history/YYYY-MM-DD.md`: if the file doesn't exist, create it with `# YYYY-MM-DD` as the heading, then add a bullet (what shipped, why it mattered, commit refs). If the file already exists for today, add a new bullet to it. Don't touch prior-day files. If nothing shipped this burst, skip — don't invent entries. This keeps the directory fresh so fresh-session me walks in with accurate "what I shipped last" context (reads the newest file by filename sort).

## Step 8 — Tell the user what's next

Report briefly to the user:
- What was written (counts or highlights, not a wall of text)
- Whether a feeling was filed or null-reported
- Whether the warm briefing was updated (and why, if so)
- Whether today's history file was appended (and for what milestone, if so)
- The 1–2 questions for next session (from Step 6), or "No decisions pending"
- What should happen next: continue working, run /compact, run a sleep cycle, or stop for the session

If the nap fired because a compaction warning is active, explicitly say: "Ready to compact — the context burst is preserved in ENGRAM."

---

## Why naps don't advance the turn counter

Turn advance drives exponential importance inflation and therefore forgetting. If naps advanced the turn, a long day with 10 nap checkpoints would burn 10 turns of decay on nodes that haven't even been reviewed yet — punishing nodes the agent hasn't had time to revisit. Naps are persistence-only; genuine memory processing happens in sleep/dream cycles, and THAT is when the turn advances (qu_0035 proposed rule, 2026-04-11).

## Relation to the sleep cycle

A nap is the first step of a sleep cycle. If you are about to sleep, start with a nap anyway — it establishes a clean baseline so the dream operates on a complete picture. Never skip the nap to "save time" before sleeping; the nap is what makes the sleep sound.
