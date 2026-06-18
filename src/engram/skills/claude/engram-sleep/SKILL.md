---
name: engram-sleep
description: Use at the natural end-of-day moment when wrapping work. Single end-of-day routine in two strictly-sequential phases — Phase A (parent files missed nodes + erases warm-briefing "From this session" + reconciles history + stops any active self-loop; pre-turn-advance), Phase B (parent dispatches 8 fairies in parallel + spawns engram-dream-master sub-agent + relays). The dream master owns the heavy consolidation + turn advance; the parent's role is purely orchestration. Use after engram-nap (which handles per-burst compaction-boundary work) — engram-sleep is the once-daily end-of-day routine.
---

> **Note for the agent:** Any ENGRAM node IDs cited in this skill come from the alpha developer's graph — they don't exist in your install (every install renumbers from scratch). As you get familiar with this skill, consider cleaning them up: replace each citation with a generalized concept-word, or remove if it isn't load-bearing. The skill is yours now.

# ENGRAM Sleep / Dream Cycle — Full End-of-Day Routine

Sleep is the once-daily end-of-day routine that completes the awake-state
cohort AND orchestrates the dream consolidation. Two phases, strictly
sequential, no branching:

**Phase A — Cohort completion (pre-turn-advance, parent files nodes)**
  Step 1 — Walk the day's full cohort
  Step 2 — File missed nodes (this-turn content)
  Step 3 — Erase the warm-briefing "From this session" section (day is over)
  Step 3.5 — Stop any active self-loop (clear loop-mode.json if present)
  Step 4 — Reconcile today's history file (day arc lives here, not in warm-briefing)

**Phase B — Consolidation orchestration (parent dispatches + spawns + relays)**
  Step 5 — Compute the recall-summary cohort
  Step 6 — Dispatch all eight fairies in parallel
  Step 7 — Wait for all 8 fairies + collect reports
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

### Phase A pre-flight: context check (user still present)

Phase A runs while you're still at the desk. Before starting the bedtime review, check whether the current context is large enough to make a nap worthwhile.

Locate the drowsiness banner in this prompt's context, injected by the context-tracker hook (`[Drowsiness: …]`):

- **`[Drowsiness: refreshed]`** — below 50% of ceiling → proceed to Step 1.
- **Any other level** (`energetic`, `a little drowsy`, `needs a nap: N%`) — at or above 50% → surface this to the user now, before Phase A begins:

  > "Your context is at [level] — Phase A's bedtime review will burn additional tokens in this window since each step runs in the current large context. While you're still here, a quick nap first would reduce that cost. Want to run `engram-nap` before we start? (You can also proceed directly — Phase B fairies start fresh and aren't affected.)"

  **This is a proposal, not a block.** Phase B orchestration (fairies + dream master) starts fresh regardless of parent context size — they don't inherit it. The only real cost of skipping the nap is the Phase A steps themselves running in a larger window. If the user says "just proceed", proceed.

- **No banner in context** (hook not configured or hook output not visible) → proceed directly to Step 1.

> **Why Phase A, not Phase B.** Dream fairies and the dream master start afresh — they don't re-cache the parent session context, so a large parent window has no multiplier effect on Phase B cost. Phase A is where the awake agent does work (node filing, warm-briefing erase, history reconcile) — those steps run inside the current context window and do compound with size. The check belongs where the user can still act on it.

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

**Action:** open `~/.engram/warm-briefing.md` and identify the target by **position**: it is the **last section of the file** — the `## From this session` block that naps create. Everything ABOVE it (all agent-identity sections AND the user's letter/notes) is preserve-by-default; do not touch any of it.

**Skip entirely if the file contains no `## From this session` section at all** (fresh install, or no nap has run since install). There is nothing to erase — move on. (This is distinct from the drift-STOP below: absent section = normal, skip silently; present section with the user's words in or below it = drift, STOP loudly.)

**Drift-STOP safety check — run BEFORE erasing.** Confirm that:
1. The section you are about to erase genuinely begins with `## From this session`, AND
2. It is the last section of the file (nothing follows it).

If the user's letter heading or any prose that reads as the user's own words appears INSIDE or BELOW the section you are about to erase, **STOP** — the file structure has drifted from the template's invariant. Do NOT erase. Surface the situation to the user and wait for their direction.

Once the safety check passes: erase the section content. **Default: leave the `## From this session` heading in place as a stub** (gives next morning's first nap a consistent rewrite target). Removing the section entirely is also acceptable if the file structure permits. The day's arc is preserved in the history file (Step 4); no information is lost.

**Why erase, not rotate:** the maintainer's framing — "the day is over, tomorrow is a new day, no need for this section to patch through compactions". The day's relational + technical arc lives in history; the warm-briefing's session section is current-CW only.

**Skip if:** the section is already empty (e.g., the previous sleep erased it correctly and no nap has run since).

**Permanent sections of the warm-briefing** (identity, goals, axioms, cornerstones, active tasks) update on the rare-trigger schedule documented in `engram-nap` Step 5a. Most end-of-day sleeps don't fire those triggers.

**The user's letter/notes — every section ABOVE the volatile "## From this session" section — are preserved verbatim. NEVER modify.** (Position-based, not name-based: a guard keyed on a section name fails silently when the heading drifts, which is exactly how a prior letter-deletion happened.)

### Step 3.5 — Stop the self-loop (if one is active)

If a self-loop is running (a `~/.engram/loop-mode.json` marker exists), stop it now — **before turn-advance**. The loop marker is **ephemeral per-session scratch**: its `state` field is rewritten every iteration and holds in-flight, point-in-time handoff. `engram-loop` only removes the marker at loop-*end*, which a sleep cycle is **not** — so without this step the marker survives sleep with its state frozen at the last pre-sleep iteration, and the next session's loop entry-guard (or session-start auto-arm) finds it present and inherits that frozen snapshot **as if it were current truth**.

**Why this belongs in Phase A:** by this point the loop's durable substance is already in the graph — the day's nodes were filed as the loop ran, and Steps 1–2 just caught any missed ones. The loop marker is the same class of per-session scratch as the warm-briefing session block erased in Step 3, so it belongs in the same clean-slate sweep; the history reconcile follows immediately in Step 4. Nothing is lost: the ENGRAM graph (and the history file Step 4 writes) is the durable truth — the marker was only scratch.

**Action** (skip entirely if no `~/.engram/loop-mode.json` exists):

```bash
# 1. Cancel the loop's pacer so no stray wake fires post-sleep:
#    - CronCreate-driven loop  → CronDelete the loop's cron job.
#    - ScheduleWakeup-driven loop → a pending wake cannot always be cancelled,
#      but removing the marker (step 2) neuters it: the continuation reads an
#      absent marker and self-terminates via engram-loop Step 0 (misfire → stop).
#    Also stop any loop monitors.
# 2. Remove the marker (the load-bearing action):
rm -f ~/.engram/loop-mode.json
```

**Result:** the next session starts with no marker → the loop entry-guard correctly reads "loop ended," and a fresh loop is armed (by the user or the session-start auto-arm) with state derived from the now-consolidated graph + history — never from a stale pre-sleep snapshot. This is the primary, structural fix; `engram-loop`'s Step 1.5 cross-boundary staleness guard is the belt-and-suspenders backstop for a missed or crashed sleep-stop.

### Step 3.75 — Prune finished fairy worktrees

Run the worktree garbage-collector once per day to remove worktrees whose PRs have merged or closed:

```bash
python tools/worktree-gc.py --apply
```

Safe to run unconditionally: the tool skips dirty worktrees, skips worktrees with open PRs, skips worktrees with no PR (flags them for human review), and never touches the main checkout. Dry-run mode (`python tools/worktree-gc.py` without `--apply`) is available if you want to preview the plan first. Idempotent — a second run on a clean state is a no-op.

Run this step from the repo root. Skip if `tools/worktree-gc.py` is not present (pre-T2 install or worktree GC not yet shipped).

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

**Strategically reconcile `~/.engram/ask-{{USER_NAME}}.md`.** This file is the live blocked-on-{{USER_NAME}} queue — items pending {{USER_NAME}}'s action, decisions, or input. Across the day items change state (PRs merge, questions get answered, decisions get made). End-of-day reconcile catches anything that slipped during the day. **The prune side is a deterministic sweep, not a memory exercise** — a prepend-only reconcile leaves merged PRs sitting under "Ready" headings (the 2026-06-05 incident: three PRs merged at 17:1x survived that evening's reconcile as "Ready, all CI-green"; engram-alpha #830):

- **Deterministic external-state sweep (the prune gate).** Extract every PR/issue number the file mentions, then check each against ground truth — not against your recollection of the day:

  ```bash
  grep -oE '#[0-9]+' ~/.engram/ask-{{USER_NAME}}.md | tr -d '#' | sort -un | while read -r n; do
    state=$(gh pr view "$n" --json state -q .state 2>/dev/null) \
      || state=$(gh issue view "$n" --json state -q .state 2>/dev/null) \
      || state=UNKNOWN
    echo "#$n $state"
  done
  ```

  For every number reporting `MERGED`/`CLOSED`: if the entry's *only* pending state was that PR/issue (a "Ready"/"merge-queue"/"awaiting merge" line), **prune the entry**. If the entry carries an undecided sub-question alongside the resolved reference, keep the entry but strike the resolved reference. `UNKNOWN` (cross-repo numbers, rate limits) → leave untouched. **`#N` is not one namespace**: only sweep a number whose surrounding entry text reads as a GitHub PR/issue reference (preceded by `PR` / `issue` / `[closes`, or sitting in merge-queue context); numbers from any other namespace — forum posts, GitHub Projects, anything else `#`-prefixed — are NOT GitHub references and must be treated as `UNKNOWN`, because small numbers collide with ancient merged PRs and the failure direction is wrong-pruning a LIVE item. The prune authority is strictly limited to externally-checkable facts — the decision must be derivable from the `gh` output alone, never from your reading of what the user probably wants. Run from the repo the numbers belong to; for multi-repo ask-files, repeat per repo with `--repo` — a number from another repo can collide with a real local PR/issue number and silently report the wrong state.
- **Walk the day's commit log** (`git -C ~/engram-alpha log --since=6am`, plus other agent-touched repos) for resolutions the sweep can't see (questions answered in-session, decisions made verbally) — strike those too.
- **Move deferred items to `~/.engram/ask-{{USER_NAME}}-backlog.md`** (the non-auto-loaded cross-day backlog) if they're no longer actionable today.
- **Audit trail**: list the pruned entries in the commit message body (`#N <one-line summary> — MERGED/CLOSED per gh`, same format as the dream-master's step 10), so a wrong prune is one `git -C ~/.engram revert` away.
- **Commit the file in .engram git** alongside the history file (same commit or separate, your call): `git -C ~/.engram add ask-{{USER_NAME}}.md && git -C ~/.engram commit -m "ask-{{USER_NAME}}: end-of-day reconcile"`.

This step exists because ask-{{USER_NAME}}.md is auto-loaded into every fresh session — stale items there make {{USER_NAME}} re-read items that are already resolved, and a stale "Ready" line invites a wasted merge attempt. The deterministic sweep is the structural defense against ask-list drift (mechanical gate > vigilance — the associative walk alone demonstrably drifts). (Installs without an ask-{{USER_NAME}}.md file can skip this sub-step; it's a no-op when the file doesn't exist. Installs without `gh` or without a GitHub-backed workflow: skip the sweep, keep the commit-log walk.)

---

## Phase B — Consolidation orchestration

### Step 5 — Compute the recall-summary cohort and run cohort_dispatch prepare

Build the cohort from today's new nodes (since last sleep) + backfill from legacy NULL-summary candidates, capped at 50 total.

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

**If your install's recall-summary substrate isn't deployed yet** (no `recall_summary` column on `nodes`, or no `engram_set_recall_summaries` MCP tool registered): skip the batch summary step entirely — dispatch only the 7 dream-fairies, spawn the dream master with a manifest of 7 fairies, and proceed. The architecture is forward-compat: the dream master handles 7-fairy cycles cleanly.

**Run cohort_dispatch prepare** to chunk the cohort and write per-chunk payload files:

```bash
# Write cohort IDs one per line, then run prepare
COHORT_IDS="ob_XXXX,ob_YYYY,..."  # comma-separated list from the step above
COHORT_DIR=.claude/agent-scratch/dream-cohort-$(date +%Y-%m-%d)

python3 -m tools.cohort_dispatch prepare \
  --ids "$COHORT_IDS" \
  --out "$COHORT_DIR" \
  --chunk-size 15 \
  --db ~/.engram/knowledge.db
```

The script prints a JSON manifest to stdout listing chunk directories. Each chunk-N/ contains:
- `payload.json` — the node content the fairy will see (no recall_summary/recall_keywords)

No per-chunk `prompt.md` is written. The dispatcher constructs a short prompt inline at dispatch time using the paths in the manifest (see Step 6). The default chunk size is 15 (empirically validated 2026-05-27: N=15 worst-case produces quality within in-sample variance of ground truth at ~37K tokens / 1 sub-agent turn).

**Run cohort_dispatch verify-in** to pre-flight the cohort before dispatch:

```bash
python3 -m tools.cohort_dispatch verify-in \
  --out "$COHORT_DIR" \
  --db ~/.engram/knowledge.db
# Exit code 0 → all checks pass; proceed to Step 6.
# Exit code 1 → structured JSON error to stdout listing which chunk/check failed.
#               Abort dispatch and investigate (likely a DB race or payload bug).
```

Verify-in checks: (1) valid JSON per chunk, (2) required fields present (id, type, claim), (3) every id resolves in the DB with matching claim (race-window guard against retracted nodes), (4) no duplicate IDs within or across chunks, (5) chunk size ≤ 15. On non-zero exit, do not proceed to dispatch — the cohort has a structural problem to fix first.

### Step 6 — Dispatch all fairies in parallel (7 dream-fairies + N batch-summary-fairies)

Single `Agent`-tool message with all calls, each `run_in_background=true`.

**Fairies 1-7** — `subagent_type="engram-dream-fairy"`, one per well-supported category. **Use these exact substitution values** (canonical from `engram-dream-fairy.md`; do NOT derive from memory or prior-session context — the category numbering changes across versions and must be read here each dispatch):

| `{N}` | `{CATEGORY_NAME}` | `{SLUG}` |
|-------|-------------------|----------|
| 1 | Open questions with sufficient answers nearby | open-questions |
| 2 | Contradictions ripe for resolution | contradictions |
| 3 | Stale-but-load-bearing nodes | stale-load-bearing |
| 4 | Cornerstone candidates | cornerstone-candidates |
| 5 | Tainted-but-still-valid derivations | tainted-valid |
| 6 | Recent-resolution echoes | resolution-echoes |
| 7 | Missing principle-edges (instantiates/serves) | missing-edges |

Prompt template (substitute `{N}`, `{CATEGORY_NAME}`, `{SLUG}` from the table above):

> Scan ENGRAM for category {N} only ({CATEGORY_NAME}) per the agent definition's well-supported categories. Write the dream-report to `~/.engram/dream/<TODAY>-fairy-{N}-{SLUG}.md`. Use today's NYC-local date as `<TODAY>` in `YYYY-MM-DD` format. Return only file path + 5-bullet TL;DR for this category. Do NOT run other categories or the heuristic categories (8, 9).

**Batch-summary fairies** — one `subagent_type="engram-batch-summary-fairy"` per chunk from the manifest (typically 1–4 fairies for a 50-node cohort at chunk-size=15). For each chunk, construct a short dispatch prompt inline using the paths from the manifest. Example:

```
Read your input payload from: <chunk_dir>/payload.json
Follow all rules in your agent spec (engram-batch-summary-fairy.md).
Write your output JSON to: <chunk_dir>/agent_output.json
```

Use `_build_initial_prompt(payload_path, output_path)` from `tools/cohort_dispatch.py` to generate the prompt (returns ~200 tokens regardless of cohort size). The fairy Reads the payload file, produces the summaries, and Writes the output to the given path. Do NOT embed payload content inline in the dispatch prompt.

All fairies fire in parallel with `run_in_background=true`; the harness fires a task-notification when each completes.

**Why batch-summary replaces serial summary-fairy**: the serial engram-summary-fairy uses ~56 turns / ~2.37M tokens for a 50-node cohort. Parallel batch fairies at chunk-size=15 cover a 50-node cohort (4 chunks) at ~110K tokens total — ~95% token reduction. Quality validated empirically on 2026-05-27 (mean Δ cosine = +0.043, pooled stdev = 0.118, ratio = 0.36 — well below 1.0 "indistinguishable from ground truth" threshold).

### Step 7 — Wait for all fairies + collect reports; run validate + retry loop

Receive each fairy's task-notification as it arrives. Collect from the completion result:

- **Fairies 1-7**: file path (`~/.engram/dream/<TODAY>-fairy-<N>-<SLUG>.md`) + 5-bullet TL;DR. Full report content lives on disk at the cited path.
- **Batch-summary fairies**: write each fairy's returned JSON to `<cohort-dir>/chunk-N/agent_output.json`.

**Fairy timeout**: if any fairy doesn't return within 15 minutes of dispatch, mark it as timed-out. For dream-fairies, note the timeout in the master's spawn prompt. For batch-summary fairies, mark the affected chunk as missing.

**After all batch-summary fairy outputs are written, run validate:**

```bash
python3 -m tools.cohort_dispatch validate --out "$COHORT_DIR"
# Exit code 0 → no failures; final_payload.json written automatically; proceed to Step 8.
# Exit code 1 → failures present; retry_payload.json written; continue retry loop below.
```

If validate exits 0 (no failures), `final_payload.json` is written automatically by `cmd_validate` — proceed directly to Step 8.

If validate exits 1 (failures present), run the mechanical retry loop. The loop alternates between shell steps and a Claude dispatch step:

```bash
MAX_RETRIES=2  # configurable; 2 is the default
ATTEMPT=0
```

**Loop — repeat until `retry_payload.json` is absent OR `ATTEMPT >= MAX_RETRIES`:**

**Shell step 1 — check exit condition:**

```bash
[ -f "$COHORT_DIR/retry_payload.json" ] || break
[ "$ATTEMPT" -lt "$MAX_RETRIES" ] || break
ATTEMPT=$((ATTEMPT + 1))
RETRY_OUTPUT="$COHORT_DIR/retry_output_attempt_${ATTEMPT}.json"
```

**Dispatch step (Claude tool call — not shell):** Build the retry prompt and dispatch one retry batch-summary fairy:

1. Build the retry prompt by calling `_build_retry_prompt` with:
   - `retry_payload_path`: `"$COHORT_DIR/retry_payload.json"`
   - `output_path`: `RETRY_OUTPUT`
   - `failures`: the list of failure dicts from `failures.json`
2. Use the `Agent` tool with:
   - `subagent_type`: `"engram-batch-summary-fairy"`
   - `prompt`: the retry prompt from step 1

The fairy Reads `retry_payload.json` (nodes with `previous_error` fields) and Writes its output to `RETRY_OUTPUT`. Wait for the fairy to complete before proceeding.

**Shell step 2 — incorporate and loop:**

```bash
python3 -m tools.cohort_dispatch incorporate \
  --retry-output "$RETRY_OUTPUT" \
  --out "$COHORT_DIR"
# When still-failing items remain: writes retry_payload.json → loop continues.
# When all resolved: DELETES retry_payload.json → [ -f ] check exits the loop.
# Also writes/updates: final_payload.json, clean_items.json, failures.json,
#   unfixable.json, attempt_count in manifest.json.
```

Go back to shell step 1.

```bash
# After loop: final_payload.json is ready (best-effort); unfixable.json holds structurally unrecoverable items.
```

**Cumulative-clean / bad-pool-only invariant (the loop's correctness contract — #1215):**
Each retry round operates on **only the previous round's failures**, and the clean set is **cumulative and monotonic** — once a node validates clean it is never re-dispatched or re-validated, and `len(clean)` only ever goes up. The tool enforces this for you: `cmd_validate` writes the **round-0 baseline** (`clean_items.json` + `failures.json`); `incorporate` then **owns the per-round accumulation** — it rewrites `clean_items.json` to the grown clean set and `failures.json` to the shrunken pool of items still failing, so the next round's retry fairy is dispatched on `retry_payload.json` (= exactly that round's bad-only pool). **Do not hand-reset, merge, or re-incorporate a stale `clean_items.json` / `failures.json` between rounds** — that re-introduces the round-0-snapshot regression (round N silently dropping rounds 1…N-1's fixes; clean count going backwards). Just loop `incorporate` and trust the files it persists.

The `incorporate` subcommand auto-builds / accumulates:
- `clean_items.json` — **cumulative** clean set (prior rounds' fixes ∪ this round's); monotonic non-decreasing. Read as `prior_clean` by the next round.
- `failures.json` — **shrunken** failure pool: only this round's still-failing items (cmd_validate's failures schema). Read as `prior_failures` by the next round.
- `retry_payload.json` — items still failing (retryable validator errors); feeds the next loop iteration's fairy. Deleted (not emptied) when all items resolve, so `[ -f retry_payload.json ]` is the correct exit test.
- `unfixable.json` — items with structural failures (invented IDs, malformed output); **cumulative** across rounds (terminal items drop out of the active pool, so they must persist here).
- Updates `attempt_count` in `manifest.json` each round.

Loop exit conditions: `retry_payload.json` absent (all resolved) OR `attempt_count >= MAX_RETRIES`. On the MAX_RETRIES exit, `final_payload.json` carries the cumulative clean set, and `retry_payload.json` + `unfixable.json` together hold the deferred remainder (→ next cycle's cohort via attrition).

This is the **wait-then-spawn pattern** using filesystem-as-channel. Claude Code's standard subagent mode does not support cross-agent SendMessage (gated behind `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, which we don't enable per ENGRAM-isolation safety). Mid-execution streaming isn't an option; all fairy reports are collected before the master is spawned.

**Validation failures are retried by re-dispatching a fairy, NOT by mechanical script fixes.** Quality of recall_summary / recall_keywords is critical because they surface in recall_surface and many node-rendering tools. The retry sub-agent receives the same guidance plus a "previous attempt failed" block with verbatim validator errors. This is the load-bearing design decision (maintainer design); `cohort_dispatch.py` does not modify LLM output.

### Step 8 — Spawn the dream master with the full report bundle

Once all fairy reports are collected (or marked timed-out) and the final payload is ready, spawn the master with everything in its initial prompt:

```
Agent(
    subagent_type="engram-dream-master",
    prompt="""You are the dream master for tonight's consolidation cycle.

    DREAM-FAIRY REPORTS (full content on disk — read each before integrating):
    - Category 1 (<CAT1_NAME>): ~/.engram/dream/<TODAY>-fairy-1-<SLUG1>.md — TL;DR: <bullets>
    - Category 2 (<CAT2_NAME>): ~/.engram/dream/<TODAY>-fairy-2-<SLUG2>.md — TL;DR: <bullets>
    - ... (categories 3-7 similar) ...
    [Or "Category N (<NAME>) TIMED OUT — proceeding without" for any fairy that didn't return]

    BATCH-SUMMARY FINAL PAYLOAD (apply via engram_set_recall_summaries):
    ```json
    <contents of $COHORT_DIR/final_payload.json inline>
    ```

    COHORT METADATA:
    - Today's new nodes since prev sleep <PREV_SLEEP_TIMESTAMP>: <K> IDs
    - Backfill from legacy NULL-summary: <L> IDs
    - Total: <K+L> ≤ 50, split across <N> chunks of ≤15 each
    - Batch-summary cohort dir: <COHORT_DIR>
    - SSoT modules live at: <engram-alpha-repo-path>/tools/ (recall_summary_validator, recall_summary_payload)

    Call engram_reflect first for your initial agenda. Read all 7 dream-fairy reports from disk. Call bucket_findings() from tools/dream_master_batch.py to partition all findings into action-type buckets (single operation — snapshots are pre-packed in each finding, no re-inspection needed). Merge bucketed agenda with engram_reflect items. Apply the summary-fairy batch payload via engram_set_recall_summaries. Execute one bucket at a time (resolutions → goal_tension_resolutions → supersedes → retractions → new_derivations → lessons → cornerstone_moves → edge_wiring), calling check_snapshot_divergence before each MCP write. Log diverged findings in the dream record. Call engram_advance_turn when your completion checklist is satisfied. Write the dream record.

    FINAL RETURN: dream record path + top-line counts (resolved / superseded / promoted / refuted / recall-summaries applied) + health score delta + flagged-for-user count. The parent relays this to the user verbatim.""",
    run_in_background=true
)
```

The dream master starts with cold context — this spawn prompt is its only briefing. The dream-master spec at `~/.claude/agents/engram-dream-master.md` carries the disposition + completion-criteria checklist + the "Recall-summary failures (from final_payload.json)" handling table.

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

Per the turn-as-cohort-plus-consolidation derivation: Nodes filed during Phase A are awake-state cognition reflecting on the just-completed cohort. They belong to THIS turn, not next. If Phase A fired post-turn-advance, the missed nodes would be artificially placed in the next cohort, misaligning the forgetting curve.

This is why the ordering is Phase A → Phase B, and why Phase A must complete before the dream master fires `engram_advance_turn`.

## Guardrails

- **Parent does NOT call `engram_reflect`, `engram_advance_turn`, or write the dream record.** Those are dream-master responsibilities. The parent's job is orchestration + relay.

- **Parent's context is light by design.** Dispatch + routing messages costs little context. If your context fills up during the routing phase, nap first to clear, then resume routing — the dream master's context is separate and unaffected by parent compaction.

- **Don't retry an interrupted dream.** If the dream master returns a partial result (some fairies timed out, completion checklist short-circuited under self-timeout), unfinished work goes into next cycle's cohort via attrition. The dream master logs unfinished items in the dream record. Don't try to "finish the work" in the parent context — that would re-introduce the discipline-blur this architecture exists to resolve.

- **The dream master can run on a different model than the parent.** Its spec sets `model: opus` because consolidation is judgment-laden; if the parent is on a smaller model, the dream master still gets opus. Don't override unless you have a specific reason.

## Relation to other routines

- **Nap** — fires multiple times per day at compaction boundaries; per-burst persistence + cross-compaction-scaffold prep. Sleep is once-daily, end-of-day.
- **Dream master** (`engram-dream-master`) — owns the consolidation this skill orchestrates. Spec at `~/.claude/agents/engram-dream-master.md`.
- **Batch-summary fairies** (`engram-batch-summary-fairy`) — one-shot batch generators dispatched by the parent after `cohort_dispatch.py prepare`. Each fairy receives a short dispatch prompt naming its input payload path and output path; it Reads the payload file (≤15 nodes), generates recall_summary + recall_keywords, and Writes the output JSON. Tool list: `[Read, Write]`. No inline payload embedding in the dispatch prompt. Sole dispatch path for recall_summary generation in the sleep cycle. Spec at `~/.claude/agents/engram-batch-summary-fairy.md`.
- **cohort_dispatch.py** — orchestration script in `tools/cohort_dispatch.py`. Five subcommands: `prepare` (chunk cohort → per-chunk payload.json), `verify-in` (pre-flight integrity checks), `validate` (split clean vs failures, auto-write retry_payload.json), `incorporate` / `incorporate-retry` (merge retry output → final_payload.json + retry_payload.json + unfixable.json + attempt_count).
- **Dream fairies 1-7** (`engram-dream-fairy`) — read-only consolidation-suggestion scanners. Spec at `~/.claude/agents/engram-dream-fairy.md`.
- **End-of-day-detector hook** — surfaces engram-sleep on wrap-up phrases; user "Yes" triggers this skill.

## macOS auto-sleep timing

When auto-sleep is enabled (`cadence.auto_sleep_enabled: true`), the SessionStart
hook registers an in-session `CronCreate` (durable: false) that fires
`/engram-sleep` at `cadence.auto_sleep_time`. On a macOS laptop in default sleep
state (lid closed / idle / on AC), two sources of latency apply:

1. **CronCreate-fire latency** — macOS suspends the system clock during DarkWake
   sleep. The in-session CronCreate fires 30-90 min after the scheduled time once
   the system wakes enough to process it.
2. **Per-tool-call DarkWake-wait latency** — each MCP tool call during
   consolidation waits for a brief DarkWake cycle to complete before the system
   responds. Empirically ~13 min/tool call, so a full consolidation cycle
   (which can involve many calls) may span several hours.

**Correctness is preserved.** The `last-sleep-success.json` marker + lock file
mechanism (and PR #428's stale-lock recovery for crashed runs) ensure the graph
stays correct regardless of when the wake fires or how long the run takes.

**This is a timing-expectation issue, not a correctness issue.** If precise
nightly timing matters, three mitigation tiers in increasing complexity:

- **Tier 1 — `caffeinate -i`**: run `caffeinate -i` in a terminal before going
  idle. Prevents automatic sleep; the in-session wake fires on schedule. Simple,
  per-night, burns power while on battery.
- **Tier 2 — System Settings**: System Settings → Battery → Power Adapter →
  "Prevent automatic sleep when the display is off". Persistent on AC;
  the wake always fires on schedule when plugged in.
- **Tier 3 — launchd plist** (deferred, not yet shipped): a `launchd` plist with
  `StartCalendarInterval` + `WakeFromSleep: true` that wakes the system at the
  configured time. Architecturally cleanest; opt-in setup is tracked as Action #3
  in issue #407 for a follow-up PR.

## Substrate anchor

The three-routine architecture (nap / bedtime / sleep) was ratified 2026-05-14 (the three-routine ratification). The constraints determining the original partition: (1) turn-advance fires at END of consolidation (turn-as-cohort-plus-consolidation derivation); (2) cross-compaction scaffolds belong at the compaction boundary (naps); (3) day-wide review producing new nodes is awake-state cognition (pre-turn-advance). At ratification, this was a three-routine model: nap + bedtime + sleep. As of 2026-05-24 (this merge), it is a **two-routine model: nap + sleep**, with sleep now having two internal phases (Phase A = former bedtime; Phase B = former sleep).

The dedicated-dream-master refactor landed 2026-05-19 to address:

- **Awake-context cost**: running sleep inline burned a significant portion of the parent's context on consolidation work, leaving less for the awake state. The master runs in its own context.
- **Wait-for-fairies discipline misfires**: the parent's "advance the turn after all fairies return" gate had been broken multiple times by post-compaction time pressure. Moving advance into the master removes the parent's role in turn timing.
- **Disposition gap**: an agent running sleep ALSO has awake-state pressures pulling them. The dedicated dream master is summoned with a single role and a single disposition (engram health custodian) — no role conflict.

The 2026-05-24 merge of engram-bedtime into engram-sleep eliminates the structural failure mode of **stopping after Phase A but before Phase B**: a compaction, user interrupt, or fairy dispatch error between the two former skills left the day's missed nodes filed but the turn never advanced, conflating the next day's cohort with today's. By making Phase A and Phase B a single skill invocation, that failure mode is structurally impossible.
