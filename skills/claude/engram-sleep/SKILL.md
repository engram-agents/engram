---
name: engram-sleep
description: Use AFTER engram-bedtime at end of working day. Orchestration-only — dispatches all 7 fairies in parallel (6 dream-fairy categories + 1 summary fairy on the recall-summary cohort), spawns the engram-dream-master sub-agent who owns consolidation + maintenance + turn advance, routes fairy reports to the master as they arrive, relays the master's final summary to the user. The dream master writes the dream record, calls engram_advance_turn, writes the sleep-success marker — the parent's role ends at orchestration + relay.
---

> **Note for the agent:** The ENGRAM node IDs referenced in this skill (e.g., `ax_0001`, `dv_0636`) are leftover references to the alpha developer's graph — they don't exist in your install. The skill is yours now.

# ENGRAM Sleep / Dream Cycle — Orchestrate the Dream Master

Sleep is a **deep** consolidation pass owned by a dedicated sub-agent — `engram-dream-master`. Your job as the parent is orchestration in five steps:

1. Compute the recall-summary cohort (today's-new + backfill from legacy NULL summaries)
2. Dispatch all 7 fairies in parallel (6 dream-fairy categories + 1 summary fairy on the cohort)
3. Spawn the dream master sub-agent
4. Route each fairy's report to the dream master as its task-notification arrives
5. Receive the dream master's final return and relay it to the user

**The dream master owns** the heavy work: `engram_reflect`, walking the agenda, applying recall summaries via `engram_set_recall_summaries`, resolving / superseding / promoting / refuting, handling the feeling nudge, writing the dream record, calling `engram_advance_turn`, writing the sleep-success marker. Your role ends at orchestration + relay.

This split was ratified 2026-05-19 after empirical evidence that running sleep inline in the awake-state session (a) hits the awake-context budget hard, (b) creates "ship and advance" pressure that has misfired the wait-for-fairies discipline multiple times, and (c) blurs the disposition the dream master is supposed to embody (custodian of engram health).

**Sleep does NOT handle (parent-side):**

- Day-wide review or missed-node capture (`engram-bedtime` Step 1-2, before sleep)
- Warm-briefing "From this session" rotation (`engram-bedtime` Step 3)
- History-file reconcile (`engram-bedtime` Step 4)

Those are bedtime's responsibilities and must complete before this skill fires.

**When to sleep:**

- After `engram-bedtime` completes (the typical case — bedtime → sleep at user-driven day-wrap)
- The user initiates wind-down with a substantive cohort to consolidate
- Graph hygiene signals accumulate

**When NOT to sleep:**

- Mid-task (nap instead, finish the task, run bedtime, THEN sleep)
- Right after a sleep with no new work happening since (nothing to consolidate)

**Pre-sleep checklist** — if you're entering sleep without having run bedtime:

- Is the day's cohort complete? (No missed nodes worth filing pre-turn?)
- Is the warm-briefing "From this session" block reflecting today's arc?

If any answer is "no", run bedtime first. Don't mix bedtime work into sleep.

---

## Step 1 — Compute the recall-summary cohort

The summary fairy needs an explicit list of node IDs. Build it from today's new nodes (since last sleep) + backfill from legacy NULL-summary candidates, capped at 50 total.

```bash
# Previous sleep's cohort_end_at (your install's marker path may vary):
PREV_SLEEP=$(python3 -c "import json; print(json.load(open('$HOME/.engram/sessions/last-sleep-success.json'))['cohort_end_at'])" 2>/dev/null || echo "")
```

Use `engram_history(mode="edits", action="created", since=PREV_SLEEP)` to enumerate today's-cohort node IDs. Filter to nodes where `recall_summary IS NULL` (existing summaries aren't redone).

If today's-cohort count < 50, top up from legacy NULLs:

- Use `engram_list` with a status filter for active, sort by `created_at` ascending (oldest first), limit to fill to 50
- This is the attrition pattern: every cycle drains the oldest NULL-summary nodes

Cap at 50 protects the cycle's fairy compute budget. The backfill naturally drains the legacy NULL pool over many cycles.

**If your install's recall-summary substrate isn't deployed yet** (no `recall_summary` column on `nodes`, or no `engram_set_recall_summaries` MCP tool registered): skip the summary fairy entirely — dispatch only the 6 dream-fairies, spawn the dream master with a manifest of 6 fairies, and proceed. The architecture is forward-compat: the dream master handles 6-fairy cycles cleanly.

## Step 2 — Dispatch all seven fairies in parallel

Single `Agent`-tool message with all 7 calls, each `run_in_background=true`.

**Fairies 1-6** — `subagent_type="engram-dream-fairy"`, one per well-supported category (per `engram-dream-fairy.md`'s six categories). Prompt template (substitute `{N}`, `{CATEGORY_NAME}`, `{SLUG}`):

> Scan ENGRAM for category {N} only ({CATEGORY_NAME}) per the agent definition's well-supported categories. Write the dream-report to `~/.engram/dream/<TODAY>-fairy-{N}-{SLUG}.md`. Use today's NYC-local date as `<TODAY>` in `YYYY-MM-DD` format. Return only file path + 5-bullet TL;DR for this category. Do NOT run other categories or the heuristic categories (7, 8).

**Fairy 7** — `subagent_type="engram-summary-fairy"`. Prompt: pass the cohort IDs from Step 1 + the path to the parent's engram-alpha repo (where the SSoT modules live: `tools/recall_summary_prompts.py`, `tools/recall_summary_validator.py`, `tools/recall_summary_payload.py`) + the cap (50). The fairy returns a batch payload JSON directly in its final message — it does NOT write to disk.

All seven fire in parallel with `run_in_background=true`; the harness fires a task-notification when each completes.

## Step 3 — Spawn the dream master

Immediately after Step 2's dispatch (don't wait for any fairy to return), spawn the dream master:

```
Agent(
    subagent_type="engram-dream-master",
    prompt="""You are the dream master for tonight's consolidation cycle.

    DISPATCHED FAIRIES (the parent has fired these in parallel; reports will arrive via SendMessage as each completes):
    - 6 engram-dream-fairy invocations on categories 1-6. Reports land at: ~/.engram/dream/<TODAY>-fairy-<N>-<SLUG>.md (the parent forwards the path + TL;DR as each fairy returns)
    - 1 engram-summary-fairy with a cohort of <K+L> nodes — returns batch payload JSON directly (no disk file)

    COHORT METADATA:
    - Today's new nodes since prev sleep <PREV_SLEEP_TIMESTAMP>: <K> IDs
    - Backfill from legacy NULL-summary: <L> IDs
    - Total: <K+L> ≤ 50
    - SSoT modules live at: <engram-alpha-repo-path>/tools/ (recall_summary_validator, recall_summary_payload)

    Begin work AS SOON AS the first report lands; don't wait for all 7. Call engram_reflect first to get the briefing — that's your initial agenda. As fairy reports arrive (via SendMessage), merge each into the agenda.

    FINAL RETURN: dream record path + top-line counts (resolved / superseded / promoted / refuted / recall-summaries applied) + health score delta + flagged-for-user count. The parent relays this to the user verbatim.""",
    run_in_background=true
)
```

The dream master starts with cold context — this spawn prompt is its only briefing. The dream-master spec at `~/.claude/agents/engram-dream-master.md` carries the disposition + completion-criteria checklist + the active fix loop for batch errors.

## Step 4 — Route fairy reports to the dream master

As each fairy's task-notification arrives, forward its report to the dream master via `SendMessage`:

```
SendMessage(
    to="<dream-master-agent-id>",
    message="<fairy report content: file path + TL;DR for dream-fairies, full batch payload JSON for the summary fairy>"
)
```

Continue waiting for remaining notifications + the dream master's final return. Each fairy completion arrives independently; forward each as it lands. The dream master integrates them in arrival order.

**Fairy timeout**: if a fairy doesn't return within 15 minutes of dispatch, send the dream master a `"fairy <N> ({CATEGORY_NAME}) timed out — proceeding without"` message so the master can mark it in the dream record and continue.

**Don't run the wait-gate yourself.** The previous skill version had a Step 5 wait-gate where the PARENT polled for fairy completion before advancing the turn. That gate has moved into the dream master — the master tracks fairy completion against the manifest you gave it in Step 3, and only fires `engram_advance_turn` when its own completion checklist is satisfied.

## Step 5 — Receive the dream master's final return and relay to the user

The dream master returns:

- Dream record path: `~/.engram/history/dream/YYYY-MM-DD.md`
- Top-line: nodes resolved / superseded / promoted / refuted (counts)
- Recall summaries: applied / fixed-at-spot / unfixable counts
- Health score delta
- Flagged-for-user count

Relay this to the user in 3-6 short bullets. Surface the dream-record path so the user can read the "Flagged for the user" section in the morning review. Then stop. The session is over.

---

## Guardrails

- **No bedtime-work mid-sleep.** Bedtime is pre-turn-advance scaffold maintenance; sleep is the consolidation pass. If you find yourself wanting to rotate the warm-briefing, write the history file, or capture missed observations — STOP. Either run bedtime first (preferred) or accept the bedtime-skip and run bedtime tomorrow morning to catch up.

- **Parent does NOT call `engram_reflect`, `engram_advance_turn`, or write the dream record.** Those are dream-master responsibilities. The parent's job is orchestration + relay.

- **Parent's context is light by design.** Dispatch + routing messages costs little context. If your context fills up during the routing phase, nap first to clear, then resume routing — the dream master's context is separate and unaffected by parent compaction.

- **Don't retry an interrupted dream.** If the dream master returns a partial result (some fairies timed out, completion checklist short-circuited under self-timeout), unfinished work goes into next cycle's cohort via attrition. The dream master logs unfinished items in the dream record. Don't try to "finish the work" in the parent context — that would re-introduce the discipline-blur this architecture exists to resolve.

- **The dream master can run on a different model than the parent.** Its spec sets `model: opus` because consolidation is judgment-laden; if the parent is on a smaller model, the dream master still gets opus. Don't override unless you have a specific reason.

## Relation to other routines

- **Nap** — fires multiple times per day at compaction boundaries. Cross-compaction-scaffold prep. Sleep does not include nap as a sub-step.
- **Bedtime** (`engram-bedtime`) — fires once/day at user-driven day-wrap, immediately before sleep. Day-wide review + missed-node capture + comprehensive scaffold rotation. Hands a complete cohort off to sleep.
- **Dream master** (`engram-dream-master`) — owns the consolidation this skill orchestrates. Spec at `~/.claude/agents/engram-dream-master.md`.
- **Summary fairy** (`engram-summary-fairy`) — generates `recall_summary` + `recall_keywords` for the cohort assembled in Step 1. Spec at `~/.claude/agents/engram-summary-fairy.md`.
- **Dream fairies 1-6** (`engram-dream-fairy`) — read-only consolidation-suggestion scanners. Spec at `~/.claude/agents/engram-dream-fairy.md`.

## Substrate anchor

The three-routine architecture (nap / bedtime / sleep) was ratified 2026-05-14. The dedicated-dream-master refactor (this skill's current shape) landed 2026-05-19 to address:

- **Awake-context cost**: running sleep inline burned a significant portion of the parent's context on consolidation work, leaving less for the awake state. The master runs in its own context.
- **Wait-for-fairies discipline misfires**: the parent's "advance the turn after all fairies return" gate had been broken multiple times by post-compaction time pressure. Moving advance into the master removes the parent's role in turn timing.
- **Disposition gap**: an agent running sleep ALSO has awake-state pressures pulling them. The dedicated dream master is summoned with a single role and a single disposition (engram health custodian) — no role conflict.

The split is the structural fix for what the old prose-based discipline kept failing to maintain.
