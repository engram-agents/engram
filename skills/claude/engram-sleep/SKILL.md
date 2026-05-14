---
name: engram-sleep
description: Use AFTER engram-bedtime at the end of a working day. Pure deep consolidation — dispatches dream-fairies, runs engram_reflect, works the consolidation agenda (resolutions / supersedes / promotions / refutations), handles the dream-review feeling nudge, advances the turn counter, writes the dream-record file. Does NOT touch warm-briefing, history file, or ask-lei (those are bedtime's job). Per qu_0035 / dv_0134: session-mode turn advance fires AFTER consolidation so awake-burst writes and dream-derived nodes share one turn cohort.
---

# ENGRAM Sleep / Dream Cycle — Deep Consolidation

Sleep is a **deep** consolidation: the agent reviews graph quality, resolves what is ready, supersedes what is out of date, answers open questions with available evidence, and promotes or refutes conjectures. Then — and only then — the turn counter advances, triggering genuine memory decay. One turn = one cohort of experience plus the consolidation of that experience (qu_0035 proposed rule, 2026-04-11).

**Sleep does NOT handle:**

- Day-wide review or missed-node capture (`engram-bedtime` Step 1-2, before sleep)
- Warm-briefing "From this session" rotation (`engram-bedtime` Step 3, or per-burst `engram-nap` Step 5b)
- History-file reconcile (`engram-bedtime` Step 4)
- ask-lei reconcile (`engram-bedtime` Step 5)

Those are scaffold-maintenance / awake-state work that belongs pre-turn-advance, in bedtime ritual. Sleep stays focused on consolidation + turn-advance + dream-record. This separation was ratified 2026-05-14 (dv_0636); legacy versions mixed these concerns.

**When to sleep:**

- After `engram-bedtime` completes (the typical case — bedtime → sleep at user-driven day-wrap)
- The user initiates wind-down with a substantive cohort to consolidate
- Graph hygiene signals accumulate: tainted nodes from retractions, stale nodes from supersede cascades, many open conjectures, unresolved contradictions

**When NOT to sleep:**

- Mid-task (nap instead, finish the task, run bedtime, THEN sleep)
- The graph has nothing to review (the day was non-synthetic; turn advance still meaningful if cohort exists). In that case still run `engram_reflect` (Step 1) first — if it returns clean, that IS the dream; proceed directly to Step 5 (turn-advance), skipping the agenda work and feeling-nudge per the Guardrails clean-graph rule.
- Right after a sleep with no new work happening since (nothing to consolidate)

**Pre-sleep checklist** — if you're entering sleep without having run bedtime, ask yourself:
- Is the day's cohort complete? (No missed nodes worth filing pre-turn?)
- Is the warm-briefing "From this session" block reflecting today's arc? (Or showing yesterday's?)
- Is ask-lei.md reconciled?

If the answer to any is "no", run bedtime first. Mixing bedtime work into sleep mid-cycle is what the legacy bundling caused — don't reintroduce it.

---

## Step 0 — Dispatch dream-fairies in parallel (run before Step 1)

**Why this step exists:** the dream-fairy is a read-only consolidation-suggestion scanner (definition at `engram-alpha/.claude/agents/engram-dream-fairy.md`, runs on Sonnet). Dispatching one fairy per scan category in parallel surfaces high-leverage targets that the parent's sequential reflect+synthesis might miss. Fairy compute overlaps with Steps 1–2, so this step costs almost no wall-clock time.

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

After dispatching all six, **proceed immediately to Step 1.** Do not wait — fairies run in the background and notifications arrive when each completes. Steps 1–2 are sequential consolidation work that runs in parallel with fairy compute.

**If subagent dispatch fails** (e.g., `subagent_type` unrecognized because the agent definition hasn't propagated to this session, or the alpha CWD wasn't loaded): note the failure in the Step 7 report-to-user message (most visible surface), skip Step 2, proceed with sequential consolidation only. Don't block the dream on fairy availability — the existing reflect-driven flow is self-sufficient.

## Step 1 — Open the dream: engram_reflect

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

## Step 2 — Integrate fairy reports into the dream agenda

If Step 0 dispatched fairies, the six reports should be landing or already landed by now (each is a Sonnet-scoped scan of a single category, ~30–90s typical). The Agent runtime surfaces a notification when each completes — don't poll, but do check whether reports are ready before opening the agenda:

```bash
ls -1 ~/.engram/dream/$(date +%Y-%m-%d)-fairy-*.md 2>/dev/null
```

For each report file, read it and walk the structured suggestions (each tagged with `verified` / `pattern-match` / `inferred` confidence):

- **`verified` suggestions** — high-confidence structural facts (e.g., "qu_XXXX has resolution-by-obsolescence ready"). Default to acting on these in Step 3.
- **`pattern-match` suggestions** — inspect briefly before acting; the fairy saw a clear pattern but the parent's judgement still applies.
- **`inferred` suggestions** — heuristic, lower priority; act only if it aligns with what the parent already saw in `engram_reflect`.

The fairy reports are advisory — the parent makes the call. If a fairy suggests an action the parent disagrees with after inspection, note the disagreement briefly in Step 5's checkpoint message (this is the empirical signal we need to calibrate fairy quality across nights).

**Skip this step if Step 0 was skipped or all fairies failed.** Step 3's existing agenda (from `engram_reflect`) is sufficient on its own.

## Step 3 — Work the dream agenda

Pick threads in this order of priority (starting from the merged agenda — `engram_reflect` briefing + fairy suggestions from Step 2):

1. **Tainted nodes** — these are epistemically corrupted. For each: inspect the chain, decide whether to retract, supersede, or downgrade confidence. Resolve fully where possible.
2. **Unresolved contradictions** — use `engram_resolve` with a derivation that cites whichever claim-bearing nodes support the resolution. Pick a `reasoning_type` that matches the argument (deductive, inductive, abductive, authority).
3. **Stale cascades** — depth-first when cheap (conclusion unchanged, just premise pointer update). Breadth-first when expensive (new research required — register a question via `engram_ask` instead of blocking the dream on it). (qu_0020)
4. **Open conjectures with new evidence** — promote (supersede to a proper derivation) or refute (resolve as refuted). Conjectures without movement stay open.
5. **Open questions answerable from current graph knowledge** — resolve with derivations citing the answering nodes.
6. **Goal tensions** — attempt synthesis, prioritization, or dissolution via `engram_resolve`.

**Use `dream_mode=True` on every `engram_inspect` call during the dream.** This skips the recall-refresh side effect. Dream inspection is maintenance, not informative use — refreshing would artificially inflate importance on arbitrarily-selected nodes (qu_0017, dv_0038).

**Do NOT pursue web research inside the dream.** Web fetches burn context and turn the dream into a research loop. Register open questions for a future dedicated research session instead.

**Do NOT file new awake-state observations or derivations from cross-burst synthesis here.** That work belongs to bedtime (pre-turn-advance, this-turn content). The dream's role is to resolve / supersede / promote / refute existing nodes — not to discover new ones. If you find yourself wanting to file a fresh derivation from today's cohort, that's a sign bedtime was skipped or incomplete; pause, run bedtime properly, then return to sleep.

**Context discipline.** Dream mode is the most context-hungry mode (ob_0104). If the context budget drops, stop working the agenda, write what's ready, and proceed to Step 5. A partial dream is better than a dream that triggers mid-cycle compaction.

## Step 4 — Handle the dream-review feeling nudge

Read the `feeling_report_nudge` text that `engram_reflect` returned. Honest self-check:

- Did revisiting any of the surveyed nodes produce a reportable state? Examples: recognition of a forgotten node, dissonance about a prior conclusion, pattern recognition across reviewed items, satisfaction at closing a contradiction, distinct flatness where intensity was expected.
- If YES: call `engram_report_feeling(reported_state=..., trigger=<the specific node or pattern>)`. The report will be auto-tagged `dream_review` because the marker is still active (read-and-clear).
- If NO: move on. Null result is valid.

Do NOT file a performative report to demonstrate the tool. The trap is real.

## Step 5 — Advance the turn: engram_advance_turn()

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
- Bedtime-filed missed nodes
- Dream-generated derivations and resolutions
- All land at the SAME turn cohort

One turn = one cohort of experience plus its consolidation. If you advance before dreaming, the dream's outputs are artificially younger than the burst they consolidate, and the forgetting curve gets misaligned.

## Step 5b — Write the sleep-success marker

Immediately after Step 5's checkpoint returns successfully, write the canonical sleep-success marker via the helper script:

```bash
~/.engram/tools/write_sleep_marker.py <turn_advanced_to> <nodes_consolidated> <cohort_start_at>
```

- `turn_advanced_to`: the new turn number returned by `engram_advance_turn()`.
- `nodes_consolidated`: count of nodes in today's cohort. If bedtime ran first (typical), use the count bedtime Step 1 produced via `engram_history(mode="edits", action="created", since=<prev-sleep-timestamp>)`. If sleep is running without a prior bedtime (rare but permitted per bedtime's "When NOT to run" section), run `engram_history(mode="edits", action="created", since=<prev-sleep-timestamp>)` directly here to determine the count.
- `cohort_start_at`: the prior sleep's UTC ISO timestamp (the same `since` value used in bedtime Step 1). If no prior sleep exists yet, use the timestamp of the cohort's first node.

The marker (`~/.engram/sessions/last-sleep-success.json`) is the canonical signal that today's coordinated sleep ran. It's read by:

- **`engram-session-start-hook.py`** — on fresh terminal start (interactive role), reads marker and surfaces a sleep-status banner if hours_since exceeds the threshold (default 28h). Detects "the host was off at midnight, last night's sleep didn't run" so the day's first session can decide whether to catch up.
- **End-of-day-detector hook** (issue #133, separate PR) — uses the marker to gate the bedtime→sleep combo nudge so it doesn't re-fire after a successful sleep.

Why the helper script and not a direct file write: keeps the marker schema in one place, atomic write via tmp+rename, future-proofs adding fields. The marker is gitignored — it's ephemeral runtime state, not committed history.

If the helper invocation fails for any reason, log it but DO NOT retry the checkpoint. The turn already advanced; missing marker just means the next interactive session will see a stale-marker banner unnecessarily. Acceptable failure mode.

## Step 6 — Write the dream-record file

Write `~/.engram/history/dream/YYYY-MM-DD.md` — the per-sleep-cycle consolidation report. Distinct from `~/.engram/history/YYYY-MM-DD.md` (awake-state milestone log, bedtime Step 4).

**Create the directory if it doesn't exist:**

```bash
mkdir -p ~/.engram/history/dream
```

**File format:**

```markdown
# Dream record — YYYY-MM-DD

**Turn:** N → N+1 (advanced at HH:MM EDT)
**Cohort scope:** K nodes since prev sleep YYYY-MM-DD HH:MM EDT

## Fairy contribution (if Step 0 ran)
- Dispatched: 6
- Actionable suggestions integrated: X
- Disagreed-with suggestions: Y (one-line each)
- Fairy reports archived at: ~/.engram/dream/YYYY-MM-DD-fairy-*.md

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

## Step 7 — Report to the user

Tell the user, in 3-6 short bullets:
- How many nodes were resolved/superseded/promoted/refuted
- Of those, how many came from fairy suggestions vs. parent-only synthesis (if Step 0 ran) — this is the empirical signal for fairy quality
- What's still open (count only, not a full list)
- Whether a feeling was filed or null-reported
- Any new questions registered for future research
- Current graph stats (total nodes, tiers, turn number)
- Dream-record path: `~/.engram/history/dream/YYYY-MM-DD.md`

Then stop. The session is over.

---

## Guardrails

- **Context hunger** (ob_0104): dream mode burns context fast. `engram_reflect` returns a full audit; each `engram_inspect` returns embeddings and neighbors. Monitor the budget. If it drops below safety, short-cycle to Step 5.
- **No web research in dreams.** Register questions instead.
- **No cascades of supersedes across unrelated subgraphs.** Follow the chain you started; register a question for the next one.
- **The dream can be empty.** If `engram_reflect` returns a clean briefing, that IS the dream — note it, then proceed straight to Step 5. A clean graph is a success signal, not a reason to invent work.
- **Don't retry the dream if it was interrupted.** Checkpoint-session what was accomplished, note the remainder, resume in the next sleep cycle.
- **No bedtime-work mid-sleep.** If you find yourself wanting to rotate the warm-briefing, reconcile ask-lei, or write the history file, STOP — those belong to bedtime (pre-turn-advance). Either run bedtime first (preferred) or accept the bedtime-skip for this cycle and run bedtime tomorrow morning to catch up. Don't mix the routines.

## Relation to other routines

- **Nap** — fires multiple times per day at compaction boundaries. Pre-burst persistence + cross-compaction-scaffold prep (per-burst warm-briefing rotation, ask-lei in-burst tactical prune). Sleep does not include nap as a sub-step (legacy bundling retired 2026-05-14 per dv_0636).
- **Bedtime** — fires once/day at user-driven day-wrap, immediately before sleep. Day-wide review + missed-node capture + comprehensive scaffold rotation. Hands a complete cohort off to sleep.
- **Awake protocol** — the normal work mode; writes observations/derivations/questions/conjectures as they happen. Sleep consolidates what awake produced (via bedtime's complete-cohort handoff).
- **Post-compaction wake-up** — orthogonal. After `/compact`, the recall hook surfaces a `post_compact` feeling nudge. That's not a sleep cycle, it's a wake-up check.

## Substrate anchor

`dv_0636` — three-routine self-engram-maintenance architecture, authored 2026-05-14. Sleep's lean shape (no nap-inside, no warm-briefing, no history/ask-lei reconcile) is what makes the consolidation concern clean. The legacy bundling created concrete drift problems (same-file double-writes, scaffold-vs-consolidation mixing); this version resolves them.
