---
name: engram-sleep
description: Use at the end of a session for deep consolidation — a dream cycle that reviews graph quality, resolves what's ready, supersedes what's out of date, and then advances the turn counter via session-mode checkpoint. Use engram-nap for shallow pre-compaction consolidation instead.
---

# ENGRAM Sleep / Dream Cycle — Deep Consolidation (Mnemosyne Edition)

Sleep is a **deep** consolidation: the agent reviews graph quality, resolves what is ready, supersedes what is out of date, answers open questions with available evidence, and promotes or refutes conjectures. Then — and only then — the turn counter advances, triggering genuine memory decay.

**When to sleep:**
- End of day or end of a major work session
- Lei initiates wind-down ("let's call it a day", "sleep session", "end of session")
- Graph hygiene signals accumulate (tainted nodes, stale nodes, unresolved contradictions)

---

## Step 1 — Nap first (establish clean baseline)

**Always start sleep with a nap.** Run the `engram-nap` skill in full.

## Step 2 — Open the dream: engram_reflect

```
mcp_engram_engram_reflect()
```

Read the briefing carefully. This IS the dream agenda.

## Step 3 — Day-wide synthesis on today's full cohort

Walk through **every node created since the last sleep**.

```
mcp_engram_engram_history(mode="edits", action="created", since="<prev-sleep-timestamp>")
```

The prev-sleep timestamp is in `~/.engram-gemini/session_log.md`.

Skim the cohort. Ask: do any derivations want to emerge?
- Observations converging on insight → `mcp_engram_engram_derive`
- Questions answered by observations → `mcp_engram_engram_resolve`
- Claim pairs disagreeing → `mcp_engram_engram_contradict`
- Later observation replacing earlier → `mcp_engram_engram_supersede`
- Recurring vocabulary → `mcp_engram_engram_add_definition`

## Step 4 — Work the dream agenda

Priority:
1. **Tainted nodes**
2. **Unresolved contradictions**
3. **Stale cascades**
4. **Open conjectures with new evidence**
5. **Open questions answerable from graph**
6. **Goal tensions**

**Use `dream_mode=True` on every `mcp_engram_engram_inspect` call during the dream.**

## Step 5 — Handle the dream-review feeling nudge

Read `feeling_report_nudge` from `engram_reflect`. File if genuine.

## Step 6 — Call mcp_engram_engram_checkpoint(mode="session")

```
mcp_engram_engram_checkpoint(
    message="<summary of the day's milestones and dream outcomes>",
    mode="session",
)
```

Advances the turn counter.

## Step 7 — Update the daily highlights

Append to `~/.engram-gemini/daily-highlights.md`:
- `## YYYY-MM-DD`
- Milestone bullets
- `### Currently focused (end of day)` mirroring `mcp_engram_engram_list_focused()`

## Step 8 — Reconcile the day's git log (if applicable)

Check `git -C ~/.engram-gemini log --since="6am" --oneline`.

## Step 9 — Report to the user

Summarize:
- Nodes resolved/superseded/promoted/refuted
- Open items count
- Feeling report status
- New questions
- Current graph stats
