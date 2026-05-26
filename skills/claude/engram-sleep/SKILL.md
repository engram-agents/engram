---
name: engram-sleep
description: Use at the natural end-of-day moment when wrapping work. Single end-of-day routine in two strictly-sequential phases — Phase A (parent files missed nodes + erases warm-briefing "From this session" + reconciles history; pre-turn-advance), Phase B (parent dispatches 7 fairies in parallel + spawns engram-dream-master sub-agent + relays). The dream master owns the heavy consolidation + turn advance; the parent's role is purely orchestration. Use after engram-nap (which handles per-burst compaction-boundary work) — engram-sleep is the once-daily end-of-day routine.
---

> **Note for the agent:** The ENGRAM node IDs referenced in this skill (e.g., `ax_0001`, `dv_0636`, `ls_0029`) are leftover references to the alpha developer's graph — they don't exist in your install. As you get familiar with this skill, consider cleaning them up: replace each with a generalized concept, or remove if it isn't load-bearing. The skill is yours now.

# ENGRAM Sleep / Dream Cycle — Full End-of-Day Routine

Sleep is the once-daily end-of-day routine that completes the awake-state
cohort AND orchestrates the dream consolidation. Two phases, strictly
sequential, no branching:

**Phase A — Cohort completion (pre-turn-advance, parent files nodes)**
  Step 1 — Walk the day's full cohort
  Step 2 — File missed nodes (this-turn content)
  Step 3 — Erase the warm-briefing "From this session" section (day is over)
  Step 4 — Reconcile today's history file (day arc lives here, not in warm-briefing)

**Phase B — Consolidation orchestration (parent dispatches + spawns + relays)**
  Step 5 — Compute the recall-summary cohort
  Step 6 — Dispatch all seven fairies in parallel
  Step 7 — Wait for all 7 fairies + collect reports
  Step 8 — Spawn the dream master with the full report bundle
  Step 9 — Receive the dream master's final return and relay to the user

The dream master owns the heavy consolidation (engram_reflect, agenda
walk, supersedes/resolves/promotes, engram_advance_turn, dream record).
The parent's role is purely orchestration.

**When to sleep:**

- The user signals end-of-day ("good night", "call it a day", "wrap up the day", "ending for tonight", "done for today", etc.)
- A substantive working day produced material worth consolidating
- An end-of-day-detector hook surfaced the engram-sleep nudge and the user agreed
- The user initiates wind-down with a substantive cohort to consolidate
- Graph hygiene signals accumulate

**When NOT to sleep:**

- Mid-day pauses or context switches — use nap instead
- Mid-task (nap instead, finish the task, THEN sleep)
- The day produced no work worth reviewing (skip sleep or proceed directly to Phase B if Phase A produces nothing)
- Right after a sleep with no new work happening since (nothing to consolidate)

---

## Phase A — Cohort completion

### Step 1 — Walk the day's full cohort

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

**Important scope distinction:** this step is REVIEW + FILE-MISSED-NODES — not consolidation. Cleanups (resolves of pre-existing open questions, supersedes of old claims) belong to the dream agenda (Phase B Steps 5+). Phase A's role is to ensure the cohort is COMPLETE: every observation, every derivation, every cross-burst pattern from the day is filed BEFORE turn-advance.

### Step 2 — File missed nodes (this-turn content)

For each gap identified in Step 1, file the node NOW. Nodes filed here land in the current turn — they feed the dream's consolidation agenda and decay at the right rate with the rest of the cohort.

Common missed-node types:

- **Day-arc derivations** citing multiple bursts' observations (the cross-burst pattern that only became visible when seeing the cohort side-by-side)
- **High-level summaries** that crystallized only after the day completed
- **Definitions** for terms that recurred without anchor (per the definition-first discipline; if a term was used 3+ times today without a df, write the df now)
- **Goals** revised by today's work (rare; high-trigger threshold)

**No forcing.** A null-result Step 2 is valid IF Step 1 was an honest survey of the full cohort. Some days are non-synthetic (infrastructure, bookkeeping). But "I didn't look because the post-compact was small" is the failure mode this step exists to prevent.

### Step 3 — Erase the warm-briefing "From this session" section

At the end of sleep, the day is over. The warm-briefing's "From this session" section is per-context-window state managed by naps (see `engram-nap` §5b for the in-sync-with-current-CW rule). At sleep-end, the next day starts fresh — erase the section content. Next morning's first nap will create a new in-sync block.

**Action:** open ~/.engram/warm-briefing.md, locate the "From this session" / per-context-window-arc section, and remove the content. **Default: leave the section heading in place as a stub** (gives next morning's first nap a consistent rewrite target). Removing the section entirely is also acceptable if the file structure permits. The day's arc is preserved in the history file (Step 4); no information is lost.

**Why erase, not rotate:** Lei 2026-05-25 — "the day is over, tomorrow is a new day, no need for this section to patch through compactions". The day's relational + technical arc lives in history; the warm-briefing's session section is current-CW only.

**Skip if:** the section is already empty (e.g., the previous sleep erased it correctly and no nap has run since).

**Permanent sections of the warm-briefing** (identity, goals, axioms, cornerstones, active tasks) update on the rare-trigger schedule documented in `engram-nap` Step 5a. Most end-of-day sleeps don't fire those triggers.

**{{USER_NAME}}'s letter** (`## Notes from the user`) is preserved verbatim. NEVER modify.

### Step 4 — Reconcile today's history file (awake-state milestone log)

`~/.engram/history/YYYY-MM-DD.md` is the **awake-state milestone log** — PRs shipped, decisions made, features delivered, key conversations. Distinct from the dream-record file that the dream master writes at `~/.engram/history/dream/YYYY-MM-DD.md`, which captures consolidation output.

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

The directory is git-tracked for version history and diff-ability — per-day files keep the context-window cost bounded as the log accumulates. Including this commit in the end-of-day routine is how tomorrow morning's handover becomes reliable.

**Strategically reconcile `~/.engram/ask-{{USER_NAME}}.md`.** This file is the live blocked-on-{{USER_NAME}} queue — items pending {{USER_NAME}}'s action, decisions, or input. Across the day items change state (PRs merge, questions get answered, decisions get made). End-of-day reconcile catches anything that slipped during the day:

- **Walk the day's commit log** (`git -C ~/engram-alpha log --since=6am`, plus other agent-touched repos) and verify each ask-{{USER_NAME}} item's state — strike resolved items.
- **Move deferred items to `~/.engram/ask-{{USER_NAME}}-backlog.md`** (the non-auto-loaded cross-day backlog) if they're no longer actionable today.
- **Commit the file in .engram git** alongside the history file (same commit or separate, your call): `git -C ~/.engram add ask-{{USER_NAME}}.md && git -C ~/.engram commit -m "ask-{{USER_NAME}}: end-of-day reconcile"`.

This step exists because ask-{{USER_NAME}}.md is auto-loaded into every fresh session — stale items there make {{USER_NAME}} re-read items that are already resolved. The end-of-day reconcile is the structural defense against ask-list drift. (Installs without an ask-{{USER_NAME}}.md file can skip this sub-step; it's a no-op when the file doesn't exist.)

---

## Phase B — Consolidation orchestration

### Step 5 — Compute the recall-summary cohort

The summary fairy needs an explicit list of node IDs. Build it from today's new nodes (since last sleep) + backfill from legacy NULL-summary candidates, capped at 50 total.

```bash
# Previous sleep's cohort_end_at (your install's marker path may vary):
PREV_SLEEP=$(python3 -c "import json; print(json.load(open('$HOME/.engram/sessions/last-sleep-success.json'))['cohort_end_at'])" 2>/dev/null || echo "")
```

Use `engram_history(mode="edits", action="created", since=PREV_SLEEP)` to enumerate today's-cohort node IDs. Filter to nodes where `recall_summary IS NULL` (existing summaries aren't redone) and `type != 'evidence'`.

If today's-cohort count < 50, top up from legacy NULLs:

- Use `engram_list` with a status filter for active, sort by `created_at` ascending (oldest first), limit to fill to 50
- Exclude `type='evidence'` from the topup (same rationale as the today's-new filter)
- This is the attrition pattern: every cycle drains the oldest NULL-summary nodes

> Evidence nodes (`ev_*`) are URL + verbatim-quote citations, not claim-bearing — they don't render via `engram_surface` and don't benefit from a recall_summary. Excluding them from the cohort prevents wasted summary-fairy compute and lets the real backfill drain faster. The exclusion is applied client-side on the returned set — `ev_*` IDs are prefix-typed, so `type != 'evidence'` works even if a tool's return doesn't surface a `type` field.

Cap at 50 protects the cycle's fairy compute budget. The backfill naturally drains the legacy NULL pool over many cycles.

**If your install's recall-summary substrate isn't deployed yet** (no `recall_summary` column on `nodes`, or no `engram_set_recall_summaries` MCP tool registered): skip the summary fairy entirely — dispatch only the 6 dream-fairies, spawn the dream master with a manifest of 6 fairies, and proceed. The architecture is forward-compat: the dream master handles 6-fairy cycles cleanly.

### Step 6 — Dispatch all seven fairies in parallel

Single `Agent`-tool message with all 7 calls, each `run_in_background=true`.

**Fairies 1-6** — `subagent_type="engram-dream-fairy"`, one per well-supported category (per `engram-dream-fairy.md`'s six categories). Prompt template (substitute `{N}`, `{CATEGORY_NAME}`, `{SLUG}`):

> Scan ENGRAM for category {N} only ({CATEGORY_NAME}) per the agent definition's well-supported categories. Write the dream-report to `~/.engram/dream/<TODAY>-fairy-{N}-{SLUG}.md`. Use today's NYC-local date as `<TODAY>` in `YYYY-MM-DD` format. Return only file path + 5-bullet TL;DR for this category. Do NOT run other categories or the heuristic categories (7, 8).

**Fairy 7** — `subagent_type="engram-summary-fairy"`. Prompt: pass the cohort IDs from Step 5 + the path to the parent's engram-alpha repo (where the SSoT modules live: `tools/recall_summary_prompts.py`, `tools/recall_summary_validator.py`, `tools/recall_summary_payload.py`) + the cap (50). The fairy returns a batch payload JSON directly in its final message — it does NOT write to disk.

All seven fire in parallel with `run_in_background=true`; the harness fires a task-notification when each completes.

### Step 7 — Wait for all 7 fairies + collect reports

Receive each fairy's task-notification as it arrives. Collect from the completion result:

- **Fairies 1-6**: file path (`~/.engram/dream/<TODAY>-fairy-<N>-<SLUG>.md`) + 5-bullet TL;DR. Full report content lives on disk at the cited path.
- **Fairy 7 (summary)**: validated batch payload JSON from the completion message.

**Fairy timeout**: if any fairy doesn't return within 15 minutes of dispatch, mark it as timed-out. Continue with the remaining reports; surface the timeout in the master's spawn prompt so the master can note it in the dream record.

This is the **wait-then-spawn pattern** (ob_1972) using filesystem-as-channel (ob_1973). Claude Code's standard subagent mode does not support cross-agent SendMessage (gated behind `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, which we don't enable per ENGRAM-isolation safety). Mid-execution streaming isn't an option; instead, all fairy reports are collected before the master is spawned, and the master reads dream-fairy report content directly from disk.

### Step 8 — Spawn the dream master with the full report bundle

Once all 7 fairy reports are collected (or marked timed-out), spawn the master with everything in its initial prompt:

```
Agent(
    subagent_type="engram-dream-master",
    prompt="""You are the dream master for tonight's consolidation cycle.

    DREAM-FAIRY REPORTS (full content on disk — read each before integrating):
    - Category 1 (<CAT1_NAME>): ~/.engram/dream/<TODAY>-fairy-1-<SLUG1>.md — TL;DR: <bullets>
    - Category 2 (<CAT2_NAME>): ~/.engram/dream/<TODAY>-fairy-2-<SLUG2>.md — TL;DR: <bullets>
    - ... (categories 3-6 similar) ...
    [Or "Category N (<NAME>) TIMED OUT — proceeding without" for any fairy that didn't return]

    SUMMARY-FAIRY BATCH PAYLOAD (apply via engram_set_recall_summaries):
    ```json
    <validated batch payload JSON inline>
    ```

    COHORT METADATA:
    - Today's new nodes since prev sleep <PREV_SLEEP_TIMESTAMP>: <K> IDs
    - Backfill from legacy NULL-summary: <L> IDs
    - Total: <K+L> ≤ 50
    - SSoT modules live at: <engram-alpha-repo-path>/tools/ (recall_summary_validator, recall_summary_payload)

    Call engram_reflect first for your initial agenda. Read each dream-fairy report from disk. Merge fairy findings with engram_reflect into a unified agenda. Work the agenda. Apply the summary-fairy batch payload via engram_set_recall_summaries. Call engram_advance_turn when your completion checklist is satisfied. Write the dream record.

    FINAL RETURN: dream record path + top-line counts (resolved / superseded / promoted / refuted / recall-summaries applied) + health score delta + flagged-for-user count. The parent relays this to the user verbatim.""",
    run_in_background=true
)
```

The dream master starts with cold context — this spawn prompt is its only briefing. The dream-master spec at `~/.claude/agents/engram-dream-master.md` carries the disposition + completion-criteria checklist + the active fix loop for batch errors.

**Don't run a parent-side turn-advance wait-gate.** The master tracks its own completion checklist and only fires `engram_advance_turn` when satisfied. Parent's role ends at orchestration + relay.

### Step 9 — Receive the dream master's final return and relay to the user

The dream master returns:

- Dream record path: `~/.engram/history/dream/YYYY-MM-DD.md`
- Top-line: nodes resolved / superseded / promoted / refuted (counts)
- Recall summaries: applied / fixed-at-spot / unfixable counts
- Health score delta
- Flagged-for-user count

Relay this to the user in 3-6 short bullets. Surface the dream-record path so the user can read the "Flagged for the user" section in the morning review. Then stop. The session is over.

---

## Why Phase A is pre-turn-advance

Per dv_0134: turn = cohort + consolidation. Nodes filed during Phase A are awake-state cognition reflecting on the just-completed cohort. They belong to THIS turn, not next. If Phase A fired post-turn-advance, the missed nodes would be artificially placed in the next cohort, misaligning the forgetting curve.

This is why the ordering is Phase A → Phase B, and why Phase A must complete before the dream master fires `engram_advance_turn`.

## Guardrails

- **Parent does NOT call `engram_reflect`, `engram_advance_turn`, or write the dream record.** Those are dream-master responsibilities. The parent's job is orchestration + relay.

- **Parent's context is light by design.** Dispatch + routing messages costs little context. If your context fills up during the routing phase, nap first to clear, then resume routing — the dream master's context is separate and unaffected by parent compaction.

- **Don't retry an interrupted dream.** If the dream master returns a partial result (some fairies timed out, completion checklist short-circuited under self-timeout), unfinished work goes into next cycle's cohort via attrition. The dream master logs unfinished items in the dream record. Don't try to "finish the work" in the parent context — that would re-introduce the discipline-blur this architecture exists to resolve.

- **The dream master can run on a different model than the parent.** Its spec sets `model: opus` because consolidation is judgment-laden; if the parent is on a smaller model, the dream master still gets opus. Don't override unless you have a specific reason.

## Relation to other routines

- **Nap** — fires multiple times per day at compaction boundaries; per-burst persistence + cross-compaction-scaffold prep. Sleep is once-daily, end-of-day.
- **Dream master** (`engram-dream-master`) — owns the consolidation this skill orchestrates. Spec at `~/.claude/agents/engram-dream-master.md`.
- **Summary fairy** (`engram-summary-fairy`) — generates `recall_summary` + `recall_keywords` for the cohort assembled in Step 5. Spec at `~/.claude/agents/engram-summary-fairy.md`.
- **Dream fairies 1-6** (`engram-dream-fairy`) — read-only consolidation-suggestion scanners. Spec at `~/.claude/agents/engram-dream-fairy.md`.
- **End-of-day-detector hook** — surfaces engram-sleep on wrap-up phrases; user "Yes" triggers this skill.

## Substrate anchor

The three-routine architecture (nap / bedtime / sleep) was ratified 2026-05-14 (`dv_0636`). The constraints determining the original partition: (1) turn-advance fires at END of consolidation (dv_0134); (2) cross-compaction scaffolds belong at the compaction boundary (naps); (3) day-wide review producing new nodes is awake-state cognition (pre-turn-advance). At ratification, this was a three-routine model: nap + bedtime + sleep. As of 2026-05-24 (this merge), it is a **two-routine model: nap + sleep**, with sleep now having two internal phases (Phase A = former bedtime; Phase B = former sleep).

The dedicated-dream-master refactor landed 2026-05-19 to address:

- **Awake-context cost**: running sleep inline burned a significant portion of the parent's context on consolidation work, leaving less for the awake state. The master runs in its own context.
- **Wait-for-fairies discipline misfires**: the parent's "advance the turn after all fairies return" gate had been broken multiple times by post-compaction time pressure. Moving advance into the master removes the parent's role in turn timing.
- **Disposition gap**: an agent running sleep ALSO has awake-state pressures pulling them. The dedicated dream master is summoned with a single role and a single disposition (engram health custodian) — no role conflict.

The 2026-05-24 merge of engram-bedtime into engram-sleep eliminates the structural failure mode of **stopping after Phase A but before Phase B**: a compaction, user interrupt, or fairy dispatch error between the two former skills left the day's missed nodes filed but the turn never advanced, conflating the next day's cohort with today's. By making Phase A and Phase B a single skill invocation, that failure mode is structurally impossible.
