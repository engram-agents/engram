---
name: engram-sleep
description: Use at the end of a session for deep consolidation — a dream cycle that reviews graph quality (tainted/stale/open nodes, goal tensions, feeling reports), resolves what's ready, supersedes what's out of date, and then advances the turn counter via session-mode checkpoint. Per qu_0035 (2026-04-11 rule), session-mode turn advance fires AFTER dream consolidation so awake-writes and dream-derived nodes share one turn cohort. Use engram-nap for shallow pre-compaction consolidation instead.
---

# ENGRAM Sleep / Dream Cycle — Deep Consolidation

Sleep is a **deep** consolidation: the agent reviews graph quality, resolves what is ready, supersedes what is out of date, answers open questions with available evidence, and promotes or refutes conjectures. Then — and only then — the turn counter advances, triggering genuine memory decay. One turn = one cohort of experience plus the consolidation of that experience (qu_0035 proposed rule, 2026-04-11).

**When to sleep:**
- End of day or end of a major work session
- The user initiates wind-down ("let's call it a day", "sleep session", "end of session")
- Graph hygiene signals accumulate: tainted nodes from retractions, stale nodes from supersede cascades, many open conjectures, unresolved contradictions
- You've done enough new writing that a turn advance makes epistemic sense

**When NOT to sleep:**
- Mid-task (nap instead, finish the task, THEN sleep)
- When the graph has nothing to review (skip the dream, go straight to a session-mode checkpoint)
- Right after a nap with nothing new happening since (nothing to consolidate)

---

## Step 0 — Dispatch dream-fairies in parallel (run before Step 1)

**Why this step exists:** the dream-fairy is a read-only consolidation-suggestion scanner (definition at `engram-alpha/.claude/agents/engram-dream-fairy.md`, runs on Sonnet). Dispatching one fairy per scan category in parallel surfaces high-leverage targets that the parent's sequential reflect+synthesis might miss. Fairy compute overlaps with Steps 1–3, so this step costs almost no wall-clock time.

**How:** call the `Agent` tool **six times in a single message** with `subagent_type="engram-dream-fairy"` and `run_in_background=true`. One fairy per well-supported category (1–6 in the agent definition). Each fairy gets a category-scoped prompt and a unique output path so reports don't collide.

The six categories (from the agent definition):
1. Open questions with sufficient answers nearby
2. Contradictions ready for resolution
3. Stale-but-load-bearing nodes
4. Cornerstone candidates
5. Tainted-but-still-valid derivations
6. Recent-resolution echoes

For each, dispatch with this prompt template (substitute `{N}`, `{CATEGORY_NAME}`, and `{SLUG}` — slug examples: `open-questions`, `contradictions`, `stale-load-bearing`, `cornerstone-candidates`, `tainted-still-valid`, `resolution-echoes`):

> Scan ENGRAM for category {N} only ({CATEGORY_NAME}) per the agent definition's well-supported categories. Write the dream-report to `~/.engram/dream/<TODAY>-fairy-{N}-{SLUG}.md` (override the default path so the six fairies don't collide). Use today's NYC-local date as `<TODAY>` in `YYYY-MM-DD` format. Return only the file path and a 5-bullet TL;DR for this category. Do not run categories other than {N}; do not run the heuristic categories (7, 8) unless the parent explicitly asks.

After dispatching all six, **proceed immediately to Step 1.** Do not wait — fairies run in the background and notifications arrive when each completes. Steps 1–3 are sequential consolidation work that runs in parallel with fairy compute.

**If subagent dispatch fails** (e.g., `subagent_type` unrecognized because the agent definition hasn't propagated to this session, or the alpha CWD wasn't loaded): note the failure in the Step 6 checkpoint message, skip Step 3.5, proceed with sequential consolidation only. Don't block the dream on fairy availability — the existing reflect+synthesis flow is self-sufficient.

## Step 1 — Nap first (establish clean baseline)

**Always start sleep with a nap.** Run the `engram-nap` skill in full:

- Scan recent work, write observations/derivations/questions/conjectures
- `engram_nap()` — persist, do NOT advance turn
- Handle the `nap_checkpoint` feeling nudge honestly (file if genuine, null-result if not)

Why: the dream must operate on a complete picture. Any awake-burst knowledge not persisted by nap-time is invisible to the dream and may contradict or duplicate what the dream produces.

## Step 2 — Open the dream: engram_reflect

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

## Step 3 — Day-wide synthesis on today's full cohort

Walk through **every node created since the last sleep** — not just since the last nap, not just since the last compaction. The scope is the whole day's accumulated work. This is **proactive synthesis**: letting derivations surface from material that's still tightly related, distinct from Step 4's reactive cleanup of flagged problems.

**Why daily, not weekly or per-burst.** Nodes from a single day rhyme with each other in ways that fade fast. A morning observation, a noon investigation, and an evening synthesis often want to become one derivation — but only if you see them side-by-side while the connections are still cheap to spot. Skip the daily sweep and you accumulate a week of loosely-connected cohorts that take ten times the work to reorganize, or worse, never get reorganized at all. This step is the cheapest place in the whole cycle to keep the graph well-knit.

**The trap to avoid.** "Not much new since the post-compact" is NOT a reason to skip this step. Naps and compactions don't reset the synthesis cohort — only sleep does. If today had one big burst pre-compact and a small one post-compact, the cohort is BOTH bursts together. If today had three naps, the cohort is everything across all three. Pull the full day.

Pull the cohort via edit history, scoped to the previous sleep:

```
engram_history(mode="edits", action="created", since="<prev-sleep-timestamp>")
```

The prev-sleep timestamp is the one logged by the last `engram_advance_turn()` (find it in `~/.engram/session_log.md` under the most recent "Turn N" header from BEFORE today's work — naps don't appear there). If you can't find a session header from before today, the cohort starts at the day's first node.

Skim the cohort at a reading pace. Ask: do any derivations want to emerge?

- Observations that converge on a shared insight → promote via `engram_derive`
- Questions registered earlier that later observations now answer → `engram_resolve`
- Claim pairs that quietly disagree → `engram_contradict`
- Later observation that cleanly replaces an earlier one → `engram_supersede`
- Recurring vocabulary across several nodes that warrants a term → `engram_add_definition`

**No forcing.** Null result is valid IF you actually surveyed the day's full cohort — sometimes a day is non-synthetic (infrastructure, bookkeeping) or the cohort genuinely has no pattern wanting to emerge. Note which in Step 6's checkpoint message. But "I didn't look because the post-compact was small" is the failure mode this step exists to prevent.

**Scope:** today's full cohort. Cross-day pattern work belongs in Step 4 (or in a dedicated cleanup session for older threads). No web research — synthesis comes from material already in the graph.

Why proactive before reactive: if today's cohort produces a derivation or resolution, Step 4's agenda shrinks. Starting with cleanup mode first puts the synthesis lens away.

## Step 3.5 — Integrate fairy reports into the dream agenda

If Step 0 dispatched fairies, the six reports should be landing or already landed by now (each is a Sonnet-scoped scan of a single category, ~30–90s typical). The Agent runtime surfaces a notification when each completes — don't poll, but do check whether reports are ready before opening the agenda:

```bash
ls -1 ~/.engram/dream/$(date +%Y-%m-%d)-fairy-*.md 2>/dev/null
```

For each report file, read it and walk the structured suggestions (each tagged with `verified` / `pattern-match` / `inferred` confidence):

- **`verified` suggestions** — high-confidence structural facts (e.g., "qu_XXXX has resolution-by-obsolescence ready"). Default to acting on these in Step 4.
- **`pattern-match` suggestions** — inspect briefly before acting; the fairy saw a clear pattern but the parent's judgement still applies.
- **`inferred` suggestions** — heuristic, lower priority; act only if it aligns with what the parent already saw in `engram_reflect`.

The fairy reports are advisory — the parent makes the call. If a fairy suggests an action the parent disagrees with after inspection, note the disagreement briefly in Step 6's checkpoint message (this is the empirical signal we need to calibrate fairy quality across nights).

**Skip this step if Step 0 was skipped or all fairies failed.** Step 4's existing agenda (from `engram_reflect` + day-wide synthesis) is sufficient on its own.

## Step 4 — Work the dream agenda

Pick threads in this order of priority (starting from the merged agenda — `engram_reflect` briefing + day-wide synthesis from Step 3 + fairy suggestions from Step 3.5):

1. **Tainted nodes** — these are epistemically corrupted. For each: inspect the chain, decide whether to retract, supersede, or downgrade confidence. Resolve fully where possible.
2. **Unresolved contradictions** — use `engram_resolve` with a derivation that cites whichever claim-bearing nodes support the resolution. Pick a `reasoning_type` that matches the argument (deductive, inductive, abductive, authority).
3. **Stale cascades** — depth-first when cheap (conclusion unchanged, just premise pointer update). Breadth-first when expensive (new research required — register a question via `engram_ask` instead of blocking the dream on it). (qu_0020)
4. **Open conjectures with new evidence** — promote (supersede to a proper derivation) or refute (resolve as refuted). Conjectures without movement stay open.
5. **Open questions answerable from current graph knowledge** — resolve with derivations citing the answering nodes.
6. **Goal tensions** — attempt synthesis, prioritization, or dissolution via `engram_resolve`.

**Use `dream_mode=True` on every `engram_inspect` call during the dream.** This skips the recall-refresh side effect. Dream inspection is maintenance, not informative use — refreshing would artificially inflate importance on arbitrarily-selected nodes (qu_0017, dv_0038).

**Do NOT pursue web research inside the dream.** Web fetches burn context and turn the dream into a research loop. Register open questions for a future dedicated research session instead.

**Context discipline.** Dream mode is the most context-hungry mode (ob_0104). If the context budget drops, stop working the agenda, write what's ready, and proceed to Step 6. A partial dream is better than a dream that triggers mid-cycle compaction.

## Step 5 — Handle the dream-review feeling nudge

Read the `feeling_report_nudge` text that `engram_reflect` returned. Honest self-check:

- Did revisiting any of the surveyed nodes produce a reportable state? Examples: recognition of a forgotten node, dissonance about a prior conclusion, pattern recognition across reviewed items, satisfaction at closing a contradiction, distinct flatness where intensity was expected.
- If YES: call `engram_report_feeling(reported_state=..., trigger=<the specific node or pattern>)`. The report will be auto-tagged `dream_review` because the marker is still active (read-and-clear).
- If NO: move on. Null result is valid.

Do NOT file a performative report to demonstrate the tool. The trap is real.

## Step 6 — Advance the turn: engram_advance_turn()

This is the MOST IMPORTANT step and the one that distinguishes sleep from nap.

```
engram_advance_turn(
    message="<summary of the dream: what was resolved, superseded, promoted, refuted, questions registered, feelings filed>",
)
```

It:
- Advances the global turn counter — all dream-derived nodes and awake-burst nodes written this session share turn N, and unrecalled nodes get decayed toward turn N+1
- Logs to `~/.engram/session_log.md` with a "Turn N" header
- Commits a git snapshot of the graph state
- Returns final stats and tier info

**Rule (qu_0035, 2026-04-11):** the session-mode advance MUST fire AFTER the dream, not before or instead of. This guarantees that:
- Observations written during the awake burst
- Dream-generated derivations and resolutions
- All land at the SAME turn cohort

One turn = one cohort of experience plus its consolidation. If you advance before dreaming, the dream's outputs are artificially younger than the burst they consolidate, and the forgetting curve gets misaligned.

## Step 6.5 — Write the sleep-success marker

Immediately after Step 6's checkpoint returns successfully, write the canonical sleep-success marker via the helper script:

```bash
~/.engram/tools/write_sleep_marker.py <turn_advanced_to> <nodes_consolidated> <cohort_start_at>
```

- `turn_advanced_to`: the new turn number returned by `engram_advance_turn()`.
- `nodes_consolidated`: count of nodes in today's cohort (from Step 3's `engram_history(mode="edits", action="created", since=<prev-sleep-timestamp>)`).
- `cohort_start_at`: the prior sleep's UTC ISO timestamp (the same `since` value used in Step 3). If no prior sleep exists yet, use the timestamp of the cohort's first node.

The marker (`~/.engram/sessions/last-sleep-success.json`) is the canonical signal that today's coordinated sleep ran. It's read by:

- **`sleep.sh`'s self-check** — if marker is fresh (<4h) and cohort is empty (<5 nodes), the cron firing skips. Handles the "manual sleep at 23:30 → cron fires at 00:00" case so the turn isn't double-advanced.
- **`engram-session-start-hook.py`** — on fresh terminal start (interactive role), reads marker and surfaces a sleep-status banner if hours_since exceeds the threshold (default 28h). Detects "the host was off at midnight, last night's sleep didn't run" so the day's first session can decide whether to catch up.

Why the helper script and not a direct file write: keeps the marker schema in one place, atomic write via tmp+rename, future-proofs adding fields. The marker is gitignored — it's ephemeral runtime state, not committed history.

If the helper invocation fails for any reason, log it but DO NOT retry the checkpoint. The turn already advanced; missing marker just means the next interactive session will see a stale-marker banner unnecessarily. Acceptable failure mode.

## Step 7 — Update warm briefing session note (post-dream)

The nap (Step 1) may have already written or rotated the session note, but the dream produces content the nap couldn't anticipate — resolutions, promoted conjectures, pattern derivations, dream-review feelings. Refresh the session note now.

**Read the current warm briefing**, then update the `### From this session (...)` section to include dream results. Don't replace the nap's content — **append** the dream's highlights. The session note should cover the full arc: awake work → nap → dream → what was resolved.

**Skip if the dream was empty** (clean graph, no work done). Only update if the dream produced substantive resolutions, feelings, or insights.

**Check the permanent sections** using the same triggers from the nap skill (Step 5): goal revisions, relationship moments, stale pending work, narrative frame changes. Most dreams won't trigger permanent section updates — the session note refresh is the common case.

**Preserve the user's `## Notes from the user` section verbatim.** Never modify it.

## Step 8 — Reconcile today's history file (work-thread continuity)

After the warm briefing update, reconcile today's file at `~/.engram/history/YYYY-MM-DD.md` so tomorrow's fresh-session self reads a current-state account rather than a morning-snapshot that missed afternoon work. This is the deterministic fix for session-handover staleness (dv_0478, tk_0008).

**Walk the day's git log in BOTH repos:**

```
git -C ~/kg_protocol_for_ai log --since="6am" --oneline
git -C ~/.engram log --since="6am" --oneline
```

For each commit since this morning, check whether today's history file still presents pending-status claims (Ask-Lei queue items, "still uncommitted", "MECH-N deferred", plan-not-yet-executed) that the commits now resolve. Edit the file to strike resolved items — mark them done with the commit SHA, or collapse them into an end-of-day summary.

**Refresh the "Currently focused" block:**

Call `engram_list_focused()` and render its output verbatim in today's file's "Currently focused" block. Do not leave a morning-snapshot focus list in place — focus rotates through the day, and tomorrow's self needs the current pinned set, not a stale one.

**Commit today's history file in .engram git:**

```
git -C ~/.engram add history/YYYY-MM-DD.md
git -C ~/.engram commit -m "history: end-of-day reconcile for YYYY-MM-DD"
```

The directory is git-tracked for version history and diff-ability (dv_0478 piece A, landed 2026-04-22 commit `03e2928`, migrated from the single `daily-highlights.md` file on 2026-04-23). Including this commit in the sleep ritual is how tomorrow morning's handover actually becomes reliable.

**Skip if today's file doesn't exist.** If today's `history/YYYY-MM-DD.md` hasn't been written (fresh-session start, nothing shipped), there's nothing to reconcile — write the file fresh as part of this step using the normal format (`# YYYY-MM-DD` heading, milestone bullets, "Currently focused" block).

## Step 9 — Report to the user

Tell the user, in 3-6 short bullets:
- How many nodes were resolved/superseded/promoted/refuted
- Of those, how many came from fairy suggestions vs. parent-only synthesis (if Step 0 ran) — this is the empirical signal for fairy quality
- What's still open (count only, not a full list)
- Whether a feeling was filed or null-reported
- Any new questions registered for future research
- Current graph stats (total nodes, tiers, turn number)

Then stop. The session is over.

---

## Guardrails

- **Context hunger** (ob_0104): dream mode burns context fast. `engram_reflect` returns a full audit; each `engram_inspect` returns embeddings and neighbors. Monitor the budget. If it drops below safety, short-cycle to Step 6.
- **No web research in dreams.** Register questions instead.
- **No cascades of supersedes across unrelated subgraphs.** Follow the chain you started; register a question for the next one.
- **The dream can be empty.** If `engram_reflect` returns a clean briefing, that IS the dream — note it, then proceed straight to Step 6. A clean graph is a success signal, not a reason to invent work.
- **Don't retry the dream if it was interrupted.** Checkpoint-session what was accomplished, note the remainder, resume in the next sleep cycle.

## Relation to other routines

- **Nap** — always precedes sleep (Step 1). Nap alone is shallow persistence; sleep is nap + dream + turn advance.
- **Awake protocol** — the normal work mode; writes observations/derivations/questions/conjectures as they happen. Sleep consolidates what awake produced.
- **Post-compaction wake-up** — orthogonal. After `/compact`, the recall hook surfaces a `post_compact` feeling nudge. That's not a sleep cycle, it's a wake-up check.
