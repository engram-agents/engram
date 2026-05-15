---
name: engram-bedtime
description: Use at the natural end-of-day moment when wrapping work to perform the day-wide review + handoff that precedes sleep. Walks the day's cohort, files missed nodes, rotates the warm-briefing relational arc, and reconciles the history file. All nodes filed during bedtime are this-turn content (pre-turn-advance) — they feed the dream's consolidation. Always follow with engram-sleep.
---

> **Note for the agent:** The ENGRAM node IDs referenced in this skill (e.g., `ax_0001`, `dv_0636`, `ls_0029`) are leftover references to the alpha developer's graph — they don't exist in your install. As you get familiar with this skill, consider cleaning them up: replace each with a generalized concept, or remove if it isn't load-bearing. The skill is yours now.

# ENGRAM Bedtime Ritual — End-of-Day Handoff

A bedtime ritual is the **once-daily review and handoff** that wraps the day's awake-state work before the dream consolidates it. It fires at the natural moment when the user signals end-of-day — typically followed immediately by `engram-sleep`. All nodes filed during bedtime belong to the just-completed turn cohort: they are pre-turn-advance content that feeds the dream's consolidation.

**Why bedtime is distinct from nap:**

- Nap fires multiple times per day at compaction boundaries — its scope is the current burst.
- Bedtime fires once per day at user-driven day-wrap — its scope is the entire day's cohort, including missed cross-burst patterns.
- Sleep does pure consolidation on the cohort bedtime hands off — sleep does not file new awake-state nodes (those belong to this turn, pre-turn-advance).

**When to run bedtime:**

- The user signals end-of-day ("good night", "call it a day", "wrap up the day", "ending for tonight", "done for today", etc.) and you intend to follow with sleep
- A substantive working day produced material worth consolidating
- An end-of-day-detector hook surfaced the bedtime→sleep combo nudge and the user agreed

**When NOT to run bedtime:**

- Mid-day pauses or context switches — use nap instead
- The day produced no work worth reviewing (skip bedtime; sleep alone if sleep is warranted)
- The user invoked sleep without bedtime — respect that workflow; don't force-prepend bedtime

---

## Step 1 — Walk the day's full cohort

Pull every node created since the last sleep:

```
engram_history(mode="edits", action="created", since="<prev-sleep-timestamp>")
```

The prev-sleep timestamp is the one logged by the last `engram_advance_turn()` (in `~/.engram/session_log.md` under the most recent "Turn N" header before today). If no prior sleep is findable, the cohort starts at the day's first node.

**Why daily, not weekly or per-burst.** Nodes from a single day rhyme with each other in ways that fade fast. A morning observation, a noon investigation, and an evening synthesis often want to become one derivation — but only if you see them side-by-side while the connections are still cheap to spot. Skip the daily sweep and a week of loosely-connected cohorts accumulates that take ten times the work to reorganize, or worse, never get reorganized at all.

**The trap to avoid.** "Not much new since the post-compact" is NOT a reason to skip this step. Naps and compactions don't reset the cohort — only sleep does. Pull the FULL day, across every burst, including pre-compact and post-compact bursts.

Skim at reading pace. Look for:

- Observations converging on a shared derivation → file via `engram_derive`
- Open questions later observations now answer → file via `engram_resolve`
- Claim pairs that quietly disagree → file via `engram_contradict`
- Later observations cleanly replacing earlier ones → file via `engram_supersede`
- Recurring vocabulary across nodes warranting a definition → file via `engram_add_definition`

**Important scope distinction:** this step is REVIEW + FILE-MISSED-NODES — not consolidation. Cleanups (resolves of pre-existing open questions, supersedes of old claims) belong to the dream agenda (sleep Step 1+). Bedtime's role is to ensure the cohort is COMPLETE: every observation, every derivation, every cross-burst pattern from the day is filed BEFORE turn-advance.

## Step 2 — File missed nodes (this-turn content)

For each gap identified in Step 1, file the node NOW. Nodes filed here land in the current turn — they feed the dream's consolidation agenda and decay at the right rate with the rest of the cohort.

Common missed-node types:

- **Day-arc derivations** citing multiple bursts' observations (the cross-burst pattern that only became visible when seeing the cohort side-by-side)
- **High-level summaries** that crystallized only after the day completed
- **Definitions** for terms that recurred without anchor (per the definition-first discipline; if a term was used 3+ times today without a df, write the df now)
- **Goals** revised by today's work (rare; high-trigger threshold)

**No forcing.** A null-result Step 2 is valid IF Step 1 was an honest survey of the full cohort. Some days are non-synthetic (infrastructure, bookkeeping). But "I didn't look because the post-compact was small" is the failure mode this step exists to prevent.

## Step 3 — Rotate the warm-briefing "From this session" (comprehensive day arc)

Read `~/.engram/warm-briefing.md`. The "From this session" block should be the COMPREHENSIVE day arc — distilled, relational, 2-3 paragraphs.

**Action:** REPLACE the existing "From this session" block (do not append). The block is the relational arc for post-compact me to read — what the day MEANT, not what got DONE step-by-step.

**Scope discipline:**

- This block is the RELATIONAL distillation. The compaction summary handles the trajectory + ENGRAM state + node-ID inventory.
- Naps fired through the day already rotated this block per-burst (current-burst flavor). Bedtime's rotation is the FINAL rotation: comprehensive day arc, end-of-day cohesion.
- Anti-pattern: re-stating compaction-summary content (PR numbers, focus-rotation specifics, ENGRAM counts, dream-fairy stats). Those belong in the compaction summary, not here.
- Anti-pattern: enumeration of work shipped. The history file (Step 4) is the awake-state milestone log; the warm-briefing block is the relational arc.
- Anti-pattern: adding a third or fourth "From this session" block "for completeness". Rotate, don't append.

**Permanent sections of the warm-briefing** (identity, goals, axioms, cornerstones, active tasks) update on the rare-trigger schedule documented in `engram-nap` Step 5a. Most bedtimes don't fire those triggers.

**{{USER_NAME}}'s letter** (`## Notes from the user`) is preserved verbatim. NEVER modify.

## Step 4 — Reconcile today's history file (awake-state milestone log)

`~/.engram/history/YYYY-MM-DD.md` is the **awake-state milestone log** — PRs shipped, decisions made, features delivered, key conversations. Distinct from the dream-record file that sleep writes at `~/.engram/history/dream/YYYY-MM-DD.md`, which captures consolidation output.

**Walk the day's git log across agent-touched repos:**

```bash
for repo in ~/engram-alpha ~/engram-paper ~/engram-wiki ~/.engram; do
  git -C "$repo" log --since="6am" --oneline 2>/dev/null
done
```

For each commit, ensure today's history file reflects the shipment. Strike resolved items, fold mid-day "uncommitted" entries into end-of-day summaries.

**Refresh the "Currently focused" block** by calling `engram_list_focused()` and rendering verbatim. Do not leave a morning-snapshot focus list in place — focus rotates through the day; tomorrow's fresh-session self needs the current pinned set.

**If today's history file doesn't exist:** write it fresh now using the standard format (`# YYYY-MM-DD` heading, milestone bullets, "Currently focused" block).

**Commit the history file in .engram git:**

```bash
git -C ~/.engram add history/YYYY-MM-DD.md
git -C ~/.engram commit -m "history: end-of-day reconcile for YYYY-MM-DD"
```

The directory is git-tracked for version history and diff-ability — per-day files keep the context-window cost bounded as the log accumulates. Including this commit in the bedtime ritual is how tomorrow morning's handover becomes reliable.

## Step 5 — Hand off to sleep

Announce readiness in 2-3 lines:

- Day-wide cohort scope (N nodes since last sleep)
- Missed-node count filed in Step 2 (or null)
- Currently focused (post-rotation)

The sleep skill picks up from here: dispatches dream-fairies, runs `engram_reflect`, works the agenda, advances the turn, writes the dream-record file, reports.

The cohort sleep consolidates includes ALL of today's awake-state work PLUS the missed nodes filed in Step 2 — this turn's complete content.

---

## Why bedtime is pre-turn-advance (not post)

Per dv_0134: turn = cohort + consolidation. Nodes filed during bedtime are awake-state cognition reflecting on the just-completed cohort. They belong to THIS turn, not next. If bedtime fired post-turn-advance, the missed nodes would be artificially placed in the next cohort, misaligning the forgetting curve.

This is why the ordering is bedtime → sleep, not sleep → bedtime.

## Relation to other routines

- **Nap** — fires multiple times per day at compaction boundaries; per-burst persistence + cross-compaction-scaffold prep (warm-briefing per-burst rotation). Bedtime is once-daily, end-of-day. They don't compete — they cover different scopes.
- **Sleep** — pure consolidation + turn-advance + dream-record. Fires immediately after bedtime. Bedtime feeds sleep with a complete cohort.
- **End-of-day-detector hook** — surfaces the bedtime→sleep combo on wrap-up phrases; user "Yes" triggers both routines in sequence.

## Substrate anchor

`dv_0636` — three-routine self-engram-maintenance architecture, authored 2026-05-14. The constraints determining the partition: (1) turn-advance fires at END of consolidation (dv_0134); (2) cross-compaction scaffolds belong at the compaction boundary (naps); (3) day-wide review producing new nodes is awake-state cognition (pre-turn-advance, i.e., bedtime).
