---
name: engram-sleep
description: Deep end-of-session consolidation — review graph quality, resolve what's ready, supersede what's outdated, then advance the turn counter. Always starts with a nap. Use at the end of a full session.
---

# ENGRAM Sleep — Deep Consolidation

Sleep is a deep consolidation: review graph quality, resolve what's ready, supersede what's outdated, and then advance the turn counter. One turn = one session of experience plus the consolidation of that experience.

**When to sleep:**
- End of a session or major work block
- The user says "let's wrap up", "end of session", or similar
- Graph hygiene signals accumulate: open contradictions, stale nodes

**When NOT to sleep:**
- Mid-task (use /nap instead, finish the task, then sleep)
- Right after a nap with nothing new happening since

---

## Step 1 — Nap first

**Always start sleep with a nap.** Run the `/nap` skill in full — scan recent work, write observations/derivations/questions, and checkpoint with `mode="nap"`.

The dream must operate on a complete picture. Any knowledge not persisted by nap-time is invisible to the dream.

## Step 2 — Open the dream: engram_reflect

```
engram_reflect()
```

This returns a graph-quality briefing:

- `tainted_nodes` — downstream of retracted premises, confidence corrupted
- `stale_nodes` — downstream of superseded premises, may need refresh
- `unresolved_contradictions` — explicit conflicts needing adjudication
- `low_confidence_derivations` — weak reasoning chains
- `open_questions` — registered gaps in knowledge

Read the briefing carefully. This IS the dream agenda.

## Step 3 — Work the dream agenda

Pick threads in priority order:

1. **Tainted nodes** — epistemically corrupted. Inspect the chain, decide whether to retract, supersede, or downgrade confidence.
2. **Unresolved contradictions** — use `engram_resolve` with a derivation citing the supporting evidence.
3. **Stale cascades** — update derivations whose premises were superseded.
4. **Open questions answerable from current graph** — resolve with derivations citing the answering nodes.

**Do NOT pursue web research inside the dream.** Register open questions for a future research session instead.

**Context discipline.** Dream mode burns context. If budget drops, stop working the agenda, write what's ready, and proceed to Step 4.

## Step 4 — Advance the turn

```
engram_checkpoint(
    message="<summary: what was resolved, superseded, questions registered>",
    mode="session",
)
```

The `session` mode:
- Advances the global turn counter
- Commits a git snapshot of the graph state
- Returns final stats

**The turn advance MUST fire AFTER the dream, not before.** This guarantees awake-burst writes and dream-derived nodes share one turn cohort.

## Step 5 — Report to the user

Tell the user in 3-6 short bullets:
- How many nodes were resolved/superseded
- What's still open (count only)
- Any new questions registered for future research
- Current graph stats (total nodes, turn number)

Then stop. The session is over.
