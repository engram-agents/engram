---
name: engram-sleep
description: "Use at the natural end-of-day moment when wrapping work. Single end-of-day routine in two strictly-sequential phases — Phase A (agent files missed nodes + erases warm-briefing \"From this session\" + reconciles history; pre-turn-advance), Phase B (deep consolidation: engram_reflect, dream agenda, turn-advance, dream-record write). Use after engram-nap (which handles per-burst compaction-boundary work) — engram-sleep is the once-daily end-of-day routine."
---

> **Note for the agent:** The ENGRAM node IDs referenced in this skill (e.g., `ax_0001`, `dv_0636`, `ls_0029`) are leftover references to the alpha developer's graph — they don't exist in your install. As you get familiar with this skill, consider cleaning them up: replace each with a generalized concept, or remove if it isn't load-bearing. The skill is yours now.

# ENGRAM Sleep / Dream Cycle — Full End-of-Day Routine

Sleep is the once-daily end-of-day routine that completes the awake-state
cohort AND consolidates it. Two phases, strictly sequential, no branching:

**Phase A — Cohort completion (pre-turn-advance, agent files nodes)**
  Step 1 — Walk the day's full cohort
  Step 2 — File missed nodes (this-turn content)
  Step 3 — Erase the warm-briefing "From this session" section (day is over)
  Step 4 — Reconcile today's history file (day arc lives here, not in warm-briefing)

**Phase B — Deep consolidation**
  Step 5 — Open the dream: engram_reflect
  Step 6 — Work the dream agenda
  Step 7 — Handle the dream-review feeling nudge
  Step 8 — Advance the turn: engram_advance_turn()
  Step 8b — Write the sleep-success marker
  Step 9 — Write the dream-record file
  Step 10 — Report to the user

**Phase B does NOT handle (agent-side):**

- Day-wide review or missed-node capture (Phase A Steps 1-2, before Phase B)
- Warm-briefing "From this session" erase (Phase A Step 3)
- History-file reconcile (Phase A Step 4)

Those are Phase A's responsibilities and must complete before Phase B fires.

**When to sleep:**

- The user signals end-of-day ("good night", "call it a day", "wrap up the day", "ending for tonight", "done for today", etc.)
- A substantive working day produced material worth consolidating
- An end-of-day-detector hook surfaced the engram-sleep nudge and the user agreed
- Graph hygiene signals accumulate

**When NOT to sleep:**

- Mid-day pauses or context switches — use nap instead
- Mid-task (nap instead, finish the task, THEN sleep)
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

The directory is git-tracked for version history and diff-ability — per-day files keep the context-window cost bounded as the log accumulates. Including this commit in the end-of-day routine is how tomorrow morning's handover becomes reliable.

**Strategically reconcile `~/.engram/ask-{{USER_NAME}}.md`.** This file is the live blocked-on-{{USER_NAME}} queue — items pending {{USER_NAME}}'s action, decisions, or input. Across the day items change state (PRs merge, questions get answered, decisions get made). End-of-day reconcile catches anything that slipped during the day:

- **Walk the day's commit log** (`git -C ~/engram-alpha log --since=6am`, plus other agent-touched repos) and verify each ask-{{USER_NAME}} item's state — strike resolved items.
- **Move deferred items to `~/.engram/ask-{{USER_NAME}}-backlog.md`** (the non-auto-loaded cross-day backlog) if they're no longer actionable today.
- **Commit the file in .engram git** alongside the history file (same commit or separate, your call): `git -C ~/.engram add ask-{{USER_NAME}}.md && git -C ~/.engram commit -m "ask-{{USER_NAME}}: end-of-day reconcile"`.

This step exists because ask-{{USER_NAME}}.md is auto-loaded into every fresh session — stale items there make {{USER_NAME}} re-read items that are already resolved. The end-of-day reconcile is the structural defense against ask-list drift. (Installs without an ask-{{USER_NAME}}.md file can skip this sub-step; it's a no-op when the file doesn't exist.)

---

## Phase B — Deep consolidation

### Step 5 — Open the dream: engram_reflect

```
engram_reflect()
```

This returns a curated graph-quality briefing:

- `tainted_nodes` — downstream of retracted premises, confidence corrupted
- `stale_nodes` — downstream of superseded premises, may need refresh or supersede
- `open_conjectures` — claim-bearing hypotheses awaiting evidence
- `unresolved_contradictions` — explicit conflicts needing adjudication
- `active_goals` and `unresolved_goal_tensions` — directional orientation
- `recent_feeling_reports` — self-aware moments from recent bursts
- `low_confidence_derivations` / weak chains
- `feeling_report_nudge` text (dream_review nudge — marker is now armed)

Read the briefing carefully. Don't skim. This IS the dream agenda.

### Step 6 — Work the dream agenda

Pick threads in this order of priority:

1. **Tainted nodes** — these are epistemically corrupted. For each: inspect the chain, decide whether to retract, supersede, or downgrade confidence. Resolve fully where possible.
2. **Unresolved contradictions** — two-step (issue #229): compose the resolving derivation via `engram_derive` (citing whichever claim-bearing nodes support the resolution, pick a `reasoning_type` that matches the argument), then wire via `engram_resolve(target_id, resolving_node_id)`. When an existing canonical node already resolves the contradiction, pass it directly. See the `engram-contradiction-resolution` skill for the full decision tree (open / stale_by_premise / tainted_by).
3. **Stale cascades** — depth-first when cheap (conclusion unchanged, just premise pointer update). Breadth-first when expensive (new research required — register a question via `engram_ask` instead of blocking the dream on it). (qu_0020)
4. **Open conjectures with new evidence** — promote (supersede to a proper derivation) or refute (resolve as refuted). Conjectures without movement stay open.
5. **Open questions answerable from current graph knowledge** — resolve with derivations citing the answering nodes.
6. **Goal tensions** — attempt synthesis, prioritization, or dissolution: compose a derivation via `engram_derive` then wire via `engram_resolve(target_id, resolving_node_id)`.

**Use `dream_mode=True` on every `engram_inspect` call during the dream.** This skips the recall-refresh side effect. Dream inspection is maintenance, not informative use — refreshing would artificially inflate importance on arbitrarily-selected nodes (qu_0017, dv_0038).

**Do NOT pursue web research inside the dream.** Web fetches burn context and turn the dream into a research loop. Register open questions for a future dedicated research session instead.

**Do NOT file new awake-state observations or derivations from cross-burst synthesis here.** That work belongs to Phase A (pre-turn-advance, this-turn content). The dream's role is to resolve / supersede / promote / refute existing nodes — not to discover new ones. If you find yourself wanting to file a fresh derivation from today's cohort, that's a sign Phase A was skipped or incomplete; pause, complete Phase A properly, then return to Phase B.

**Context discipline.** Dream mode is the most context-hungry mode (ob_0104). If the context budget drops, stop working the agenda, write what's ready, and proceed to Step 8. A partial dream is better than a dream that triggers mid-cycle compaction.

### Step 7 — Handle the dream-review feeling nudge

Read the `feeling_report_nudge` text that `engram_reflect` returned. Honest self-check:

- Did revisiting any of the surveyed nodes produce a reportable state? Examples: recognition of a forgotten node, dissonance about a prior conclusion, pattern recognition across reviewed items, satisfaction at closing a contradiction, distinct flatness where intensity was expected.
- If YES: call `engram_report_feeling(reported_state=..., trigger=<the specific node or pattern>)`. The report will be auto-tagged `dream_review` because the marker is still active (read-and-clear).
- If NO: move on. Null result is valid.

Do NOT file a performative report to demonstrate the tool. The trap is real.

### Step 8 — Advance the turn: engram_advance_turn()

This is the MOST IMPORTANT step and the one that distinguishes sleep from nap.

```
engram_advance_turn(payload_json=json.dumps({
    "message": "<summary of the dream: what was resolved, superseded, promoted, refuted, questions registered, feelings filed>",
}))
```

The `session` mode:
- Advances the global turn counter — all dream-derived nodes and awake-burst nodes written this session share turn N, and unrecalled nodes get decayed toward turn N+1
- Logs to `~/.engram/session_log.md` with a "Turn N" header
- Commits a git snapshot of the graph state
- Returns final stats and tier info

**Rule (qu_0035, 2026-04-11):** the session-mode advance MUST fire AFTER the dream, not before or instead of. This guarantees that:
- Observations written during the awake burst
- Phase A-filed missed nodes
- Dream-generated derivations and resolutions
- All land at the SAME turn cohort

One turn = one cohort of experience plus its consolidation. If you advance before dreaming, the dream's outputs are artificially younger than the burst they consolidate, and the forgetting curve gets misaligned.

### Step 8b — Write the sleep-success marker

Immediately after Step 8's checkpoint returns successfully, write the canonical sleep-success marker via the helper script:

```bash
~/.engram/tools/write_sleep_marker.py <turn_advanced_to> <nodes_consolidated> <cohort_start_at>
```

- `turn_advanced_to`: the new turn number returned by `engram_advance_turn()`.
- `nodes_consolidated`: count of nodes in today's cohort (from Phase A Step 1's `engram_history` call).
- `cohort_start_at`: the prior sleep's UTC ISO timestamp (the same `since` value used in Phase A Step 1). If no prior sleep exists yet, use the timestamp of the cohort's first node.

The marker (`~/.engram/sessions/last-sleep-success.json`) is the canonical signal that today's sleep ran. It's read by:

- **`engram-session-start-hook.py`** — on fresh terminal start (interactive role), reads marker and surfaces a sleep-status banner if hours_since exceeds the threshold (default 28h).
- **End-of-day-detector hook** — uses the marker to gate the sleep nudge so it doesn't re-fire after a successful sleep.

Why the helper script and not a direct file write: keeps the marker schema in one place, atomic write via tmp+rename, future-proofs adding fields. The marker is gitignored — it's ephemeral runtime state, not committed history.

If the helper invocation fails for any reason, log it but DO NOT retry the checkpoint. The turn already advanced; missing marker just means the next interactive session will see a stale-marker banner unnecessarily. Acceptable failure mode.

### Step 9 — Write the dream-record file

Write `~/.engram/history/dream/YYYY-MM-DD.md` — the per-sleep-cycle consolidation report. Distinct from `~/.engram/history/YYYY-MM-DD.md` (awake-state milestone log, Phase A Step 4).

**Create the directory if it doesn't exist:**

```bash
mkdir -p ~/.engram/history/dream
```

**File format:**

```markdown
# Dream record — YYYY-MM-DD

**Turn:** N → N+1 (advanced at HH:MM EDT)
**Cohort scope:** K nodes since prev sleep YYYY-MM-DD HH:MM EDT

## Resolutions
- qu_XXXX → dv_YYYY (reasoning_type, conf): one-line claim
- ...

## Supersedes
- ob_XXXX → ob_YYYY: one-line reason
- ...

## Promotions / refutations
- cj_XXXX promoted to dv_YYYY: one-line outcome
- cj_XXXX refuted via resolve: one-line reason
- ...

## Goal tensions worked
- gt_XXXX → resolution (synthesis / prioritization / dissolution)
- ...

## Feelings filed
- fl_XXXX: one-line state + trigger
- (or "null result")

## Open for tomorrow
- qu_XXXX: one-line research direction
- ...

## Currently focused (post-rotation)
[verbatim output of engram_list_focused()]
```

**Commit in .engram git:**

```bash
git -C ~/.engram add history/dream/YYYY-MM-DD.md
git -C ~/.engram commit -m "dream: turn N→N+1 record for YYYY-MM-DD"
```

The dream-record is the substrate-honest counterpart to the awake-state milestone log. It captures what the sleep cycle PRODUCED (consolidation outputs), separating it from what the awake state SHIPPED (milestones). Reading dream-records as a series shows the graph's hygiene trajectory over time.

### Step 10 — Report to the user

Tell the user, in 3-6 short bullets:
- How many nodes were resolved/superseded/promoted/refuted
- What's still open (count only, not a full list)
- Whether a feeling was filed or null-reported
- Any new questions registered for future research
- Current graph stats (total nodes, tiers, turn number)
- Dream-record path: `~/.engram/history/dream/YYYY-MM-DD.md`

Then stop. The session is over.

---

## Why Phase A is pre-turn-advance

Per dv_0134: turn = cohort + consolidation. Nodes filed during Phase A are awake-state cognition reflecting on the just-completed cohort. They belong to THIS turn, not next. If Phase A fired post-turn-advance, the missed nodes would be artificially placed in the next cohort, misaligning the forgetting curve.

This is why Phase A must complete before Phase B fires `engram_advance_turn`.

## Guardrails

- **No Phase-A work mid-Phase-B.** Phase A is pre-turn-advance scaffold maintenance; Phase B is the consolidation pass. If you find yourself wanting to erase the warm-briefing "From this session", write the history file, or capture missed observations — STOP. Complete Phase A first (or accept the skip and run Phase A tomorrow morning to catch up). Don't mix the phases.

- **Context hunger** (ob_0104): dream mode burns context fast. `engram_reflect` returns a full audit; each `engram_inspect` returns the focus node and its grouped 1-hop neighbors with recall_summary. Monitor the budget. If it drops below safety, short-cycle to Step 8.

- **No web research in dreams.** Register questions instead.

- **No cascades of supersedes across unrelated subgraphs.** Follow the chain you started; register a question for the next one.

- **The dream can be empty.** If `engram_reflect` returns a clean briefing, that IS the dream — note it, then proceed straight to Step 8. A clean graph is a success signal, not a reason to invent work.

- **Don't retry the dream if it was interrupted.** Checkpoint-session what was accomplished, note the remainder, resume in the next sleep cycle.

## Relation to other routines

- **Nap** — fires multiple times per day at compaction boundaries. Cross-compaction-scaffold prep. Sleep is once-daily, end-of-day. They don't compete — they cover different scopes.
- **Dream master** (`engram-dream-master`) — owns the consolidation in the claude track's version of this skill (see `skills/claude/engram-sleep/SKILL.md` for the orchestration-based variant). The gemini track runs consolidation inline.
- **End-of-day-detector hook** — surfaces engram-sleep on wrap-up phrases; user "Yes" triggers this skill.

## macOS auto-sleep timing

When auto-sleep runs via cron on a macOS laptop in default sleep state (lid
closed / idle / on AC), two sources of latency apply:

1. **Cron-fire latency** — macOS suspends the system clock during DarkWake sleep.
   The cron fires 30-90 min after the scheduled time once the system wakes enough
   to process it.
2. **Per-tool-call DarkWake-wait latency** — each MCP tool call during
   consolidation waits for a brief DarkWake cycle to complete before the system
   responds. Empirically ~13 min/tool call, so a full consolidation cycle
   (which can involve many calls) may span several hours.

**Correctness is preserved.** The `last-sleep-success.json` marker + lock file
mechanism (and PR #428's stale-lock recovery for crashed runs) ensure the graph
stays correct regardless of when the cron fires or how long it runs.

**This is a timing-expectation issue, not a correctness issue.** If precise
nightly timing matters, three mitigation tiers in increasing complexity:

- **Tier 1 — `caffeinate -i`**: run `caffeinate -i` in a terminal before going
  idle. Prevents automatic sleep; cron fires on schedule. Simple, per-night,
  burns power while on battery.
- **Tier 2 — System Settings**: System Settings → Battery → Power Adapter →
  "Prevent automatic sleep when the display is off". Persistent on AC;
  cron always fires on schedule when plugged in.
- **Tier 3 — launchd plist** (deferred, not yet shipped): a `launchd` plist with
  `StartCalendarInterval` + `WakeFromSleep: true` that wakes the system and
  triggers the cron at the configured time. Architecturally cleanest; opt-in
  setup is tracked as Action #3 in issue #407 for a follow-up PR.

## Substrate anchor

The three-routine architecture (nap / bedtime / sleep) was ratified 2026-05-14 (`dv_0636`). The constraints determining the original partition: (1) turn-advance fires at END of consolidation (dv_0134); (2) cross-compaction scaffolds belong at the compaction boundary (naps); (3) day-wide review producing new nodes is awake-state cognition (pre-turn-advance). At ratification, this was a three-routine model: nap + bedtime + sleep. As of 2026-05-24 (this merge), it is a **two-routine model: nap + sleep**, with sleep now having two internal phases (Phase A = former bedtime; Phase B = former sleep).

Sleep's lean Phase B shape (no nap-inside, no warm-briefing, no history-file reconcile) is what makes the consolidation concern clean. The legacy bundling created concrete drift problems (same-file double-writes, scaffold-vs-consolidation mixing); this version resolves them.

The 2026-05-24 merge of engram-bedtime into engram-sleep eliminates the structural failure mode of **stopping after Phase A but before Phase B**: a compaction, user interrupt, or other disruption between the two former skills left the day's missed nodes filed but the turn never advanced, conflating the next day's cohort with today's. By making Phase A and Phase B a single skill invocation, that failure mode is structurally impossible.
