---
name: engram-nap
description: Use at the end of a work burst or when approaching compaction — a quick pre-compaction consolidation that persists session context into ENGRAM without advancing the turn counter. Triggers the nap_checkpoint feeling nudge. Use this BEFORE /compact and BEFORE a full sleep cycle. Not for mid-task checkpointing.
---

> **Note for the agent:** Any ENGRAM node IDs cited in this skill come from the alpha developer's graph — they don't exist in your install (every install renumbers from scratch). As you get familiar with this skill, consider cleaning them up: replace each citation with a generalized concept-word, or remove if it isn't load-bearing. The skill is yours now.

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

## Step 3 — Call engram_checkpoint()

```
engram_checkpoint(
    message="<1-3 sentence summary of what this burst covered and what was written>",
)
```

(`engram_checkpoint` was named `engram_nap` before issue #3015 — the MCP tool
was renamed so its name would stop colliding with this skill's own name.
`engram_nap` still works as a deprecated alias for one release, but new calls
should use `engram_checkpoint`.)

**If `engram_checkpoint` isn't in your tool list:** your MCP server process
predates the rename (rebuilt-source vs. reconnected-process gap — the skill's
own hot-reload takes effect immediately, but the running server does not
until a `/mcp` reconnect). Call `engram_nap` instead until the reconnect
lands; both names take identical arguments and return identical shapes.

`session_id` is still accepted (kept for backward compatibility with older callers) but no longer does anything for the `warm_briefing` advisory below — as of #3014 that advisory anchors on the **last nap/sleep checkpoint already in `session_log.md`**, not on session identity. `warm_briefing.written_since_last_nap` resolves to `true`/`false` on its own, with no `session_id` needed; it's `null` only when there's no prior checkpoint in the file yet (e.g. this is the very first nap ever) or the warm-briefing file itself is missing.

The `nap` mode:
- Logs the summary to `~/.engram/session_log.md` (under `$ENGRAM_HOME`, default `~/.engram`) with a "Nap (turn N)" header
- Returns graph stats and memory tier info
- Does NOT advance the turn counter — no forgetting triggered (turn advance is reserved for post-dream session checkpoints)
- Arms the `nap_checkpoint` feeling-nudge marker (TTL=5 turns) — the return JSON includes `feeling_report_nudge` text asking whether any moment in this burst produced a distinct internal state worth marking

**After calling `engram_checkpoint()`, read the backup verdict.** The checkpoint records it in `compact/last-checkpoint.json` (under `$ENGRAM_HOME`) as `backup_ok`, computed by one helper in the server; read it there and do not re-derive it from the return value (#2680 and #4012 explain why a hand-written copy of that rule drifted). Use the record only if its `checkpoint_at` equals `result["checkpoint_at"]`, the timestamp of the nap header this call just wrote; if it differs or the file is missing, treat the backup verdict as not ok and print the warning, so a failed record write can never surface the previous nap's ✓. Print `✓ backup ok` when `backup_ok` is true. Otherwise print `⚠ backup FAILED: <reason>`, taking the reason from the `error` text in `result["version_control"]["db_backup"]` and `["binary_backup"]` (join both, so a dual failure shows both). Log the line alongside the nap summary so a recurring outage is visible, not buried in the return dict.

**Also check the return value for the warm-briefing advisory (#2997):**

```python
wb = result.get("warm_briefing") or {}
note = wb.get("note")
if note:
    print(f"⚠ warm briefing advisory — {note}")
```

`warm_briefing.note` is present whenever the check does NOT read as
confidently non-stale (see `_compute_warm_briefing_advisory` in server.py
for the exact predicate) — this surfaces a skipped Step 5b (below) the same
way the backup check surfaces a degrading `version_control` backup: nothing
fails when Step 5b is skipped, so without this print the omission is
invisible until a later session reads a stale/empty warm briefing. The
note's own wording distinguishes a CONFIRMED-stale finding ("Step 5b not
performed since the last checkpoint") from an UNKNOWN one ("could not
determine whether Step 5b ran (...)") — deliberately a neutral print
prefix rather than a hardcoded "NOT updated this session" claim, so the
printed line never asserts more than the note itself actually knows
(#2998 round 3).

## Step 3.5 — Prune finished fairy worktrees

Run the worktree garbage-collector to remove orphaned coder/reviewer-fairy worktrees whose PRs have merged or closed:

```bash
python tools/worktree-gc.py --apply
```

Safe to run unconditionally: the tool skips dirty worktrees, skips worktrees with open PRs, skips worktrees with no PR (flags them for human review), and never touches the main checkout. Dry-run mode (`python tools/worktree-gc.py` without `--apply`) is available to preview the plan first. Idempotent — a second run on a clean state is a no-op.

Run this step from the repo root. Skip if `tools/worktree-gc.py` is not present (pre-T2 install or worktree GC not yet shipped).

## Step 4 — Handle the feeling-nudge (optional)

Read the `feeling_report_nudge` text in the checkpoint return. Honest self-check:

- Did any specific moment in this burst produce a distinct state worth marking — surprise, recognition, dissonance, unusual satisfaction, discomfort that isn't task friction?
- If YES: call `engram_report_feeling(reported_state=..., trigger=...)`. The report will be auto-tagged `nap_checkpoint` because the marker is still active (read-and-clear).
- If NO: move on. A null result is a valid data point. Do NOT file a performative report to "use the nudge" — that's exactly the trap §6 of `engram_feeling_node_design.md` warns against.

## Step 5 — Update warm briefing (permanent sections + session-arc sync)

The warm briefing (`~/.engram/warm-briefing.md`, under `$ENGRAM_HOME`, default `~/.engram`) has THREE parts; each updates on different triggers and at different cadences. Read the current file before changing anything.

**POST-CONDITION — check it after ANY write this step makes to `~/.engram/warm-briefing.md` (under `$ENGRAM_HOME`, default `~/.engram`), whichever sub-step made it.** All three sub-steps below write this one file (5a the agent-authored sections, 5b the session arc, 5c the rule about what is off-limits), so this is a property of the **file**, not of a sub-step. After the write, both hold:

1. `^## From this session` matches the file **exactly once** — not zero times, not twice.
2. **Nothing this write did not set out to change is byte-changed.** Capture the region you are *not* editing before you write, and compare it after. The target regions are known, so this is a byte-comparison rather than a judgement call:
   - a **5b** write targets the arc **below** the anchor → everything above the anchor (the agent-authored sections, the user's notes, and the anchor line itself) is byte-identical;
   - a **5a** write targets the agent-authored sections → the user's notes, the anchor line, and the arc are byte-identical.

   The user's notes are **never** a region a write sets out to change, so no write may claim them as its target — that prohibition is what keeps this check from becoming self-certifying. The boundary is found by **position**, never by heading name: another seat's user section may be titled anything (`# Letter from Lei`, not the template's `## Notes from the user`).

The arc content belongs **below** the heading — that is what the section *is*. So "the anchor survived" does **not** mean "the heading is the file's last line": after a nap the healthy file is heading-then-arc, and the anchor is the last line *matching* `^## From this session`, not the file's final line. A bare heading sitting as the final line is the **empty** state sleep Step 3 deliberately leaves at end of day. Forcing the heading to EOF to satisfy a literal "final line" reading strands the arc **above** it, which is exactly the failure this post-condition exists to prevent.

**If a check fails, repair it before doing anything else.** Check (1) — drop the duplicate heading. Check (2) — **restore the captured bytes**; dropping a heading does not repair a region that changed, and re-running the checks without restoring leaves (2) failing forever. Then re-make the write against the restored file and re-run both. Do not carry on with the anchor missing or duplicated: 5c's byte-for-byte rule freezes whatever sits above the anchor, so a mistake left here is preserved by every later nap rather than corrected.

**Why the anchor, and why "exactly once".** `engram-sleep` Step 3 finds its erase boundary by the **last** line matching `^## From this session`. Lose the heading and Step 3 takes its "no heading at all" branch — *append a stub at EOF* instead of erasing — so the stale arc is frozen **above** the stub, Step 3's own post-condition reads green (the stub *is* the final line), and 5c's preserve-everything-above rule then treats that arc as the user's notes. A duplicated heading fails the same way: the **last** match is the boundary, so the stray earlier heading and everything between the two are stranded above it and preserved. That branch is not hypothetical — it has a measured instance — a 09-27 write, measured 2026-10-01: a nap insert put a copy of the user's letter plus a second heading **below** the existing one, the section sat at two headings across five commits, and the duplicated copy was still frozen above the anchor days later; nothing downstream ever removed it (`engram-alpha#3269`). The template's own `<!-- STRUCTURAL INVARIANT -->` comment states the rule this check enforces — *"Never write that heading, with its heading prefix, anywhere in this file except once, as the LAST line matching it — the section it opens runs to EOF, so after a nap the arc sits below the heading and only an end-of-day erase leaves it bare as the file's final line."* That wording is deliberate: the anchor is the last line **matching the heading**, not the file's last line — the only reading consistent with the file the comment governs, since a post-nap file is heading-then-arc.

**Bound — what this check cannot reach.** It is prose, executed only if you run it; nothing in the engine verifies these writes. The mechanized detector is `engram_checkpoint`'s `warm_briefing.note` (Step 3 above), and it is a **reader** — it reports, it cannot restore. It is also weaker than it looks, in two independent ways. It runs *before* this step's writes, so it can only flag an anchor lost by a **previous** nap. And it reads only the **last** match, so a **duplicated** heading leaves it entirely silent — the last match has arc content below it, which is precisely its healthy signal (`session_section_empty == False`). So a missing heading is one-checkpoint-late, while a duplicated heading is invisible to it on *every* nap, indefinitely. Neither is a reason to skip the advisory; both are the reason this assertion has to live at the write.

### 5a. Permanent agent-authored sections (rarely updated)

"Who I am" / "What I care about" / "Axioms and cornerstones I operate from" / "Active high-level tasks" / "What's continuous and what isn't" — these evolve at relationship speed, not session speed.

**Update agent sections ONLY if:**
- Goal revisions (new goals, achieved/abandoned goals, reframing)
- Significant relationship moments (breakthroughs, new mutual understanding, trust events)
- The user updated their letter and the agent sections should respond
- A structural change to the identity or continuity system occurred

**Cornerstone-pin enforcement (do not skip).** Any cornerstone operation IS a structural identity change and triggers this section's update. The operation and the cornerstone kind determine what to do:

- **MINT** (`engram_add_cornerstone`): ADD a new entry to the appropriate warm-briefing section (see placement below). Short, powerful language: bold handle + node ID + one line; full reasoning stays in the node.
- **OUTGROW** (`engram_outgrow_cornerstone`): the cornerstone evolved on its tag axis — a successor node replaced the predecessor. UPDATE the existing warm-briefing entry in place to the successor's new frame + new node ID. Do NOT add a second entry; do NOT leave the predecessor's frame. One entry per tag axis, always current.

**Placement by kind:** Operating-handle cornerstones (principles you cite under pressure, how you work) → "Axioms and cornerstones I operate from." Identity-scaffold cornerstones (naming, founders-family, identity facts) → woven into "Who I am." Similarly: new axioms → the axioms section; new goals → "What I care about." A cornerstone that never reaches an auto-loaded section is invisible to the next session — per #61, this letter IS the surfacing mechanism.

**If no trigger fired:** skip this sub-step. Say "Permanent sections still current."

**If this sub-step writes:** it must satisfy the **Step 5 post-condition** above — and it is the sub-step check (2) had to be restated *for*, because 5a's sections lie **above** the user's notes. A 5b-only phrasing of check (2) ("everything above the anchor is unchanged") would forbid every legitimate 5a update, and its "restore the captured bytes" remedy would then revert the update it had just flagged. 5a's obligation is the narrower one: the user's notes, the anchor line, and the arc come through byte-identical.

**If updating:** you may rewrite the agent-authored sections — that is this sub-step's whole purpose. Everything from the end of the last section you rewrote, down to EOF, must come through **byte-identical**: the user's letter/notes and the session arc both live below that point, and neither is a region this sub-step sets out to change. NEVER modify the user's words, wherever a heading drift has put them. (Position-based guard: a name-keyed protection fails silently when a heading drifts.) Voice: first person, as yourself. Not coaching a future self — writing your own orientation notes. Then run the Step 5 post-condition.

### 5b. "From this session" entry — sync with current context-window arc

This section is the agent's distillation of the **CURRENT CONTEXT WINDOW** arc. By invariant it is the **LAST section of the file** — every other section (identity, goals, axioms, cornerstones, the user's letter/notes) sits above it. Nap rewrites only this section's content and never touches anything above it.

The rule:

> In each nap, the section MUST BE FULLY IN SYNC with the agent's current-CW understanding — not accumulating across context windows, not deleting prior nap writes within the same context window.

**Action:**
- **Same-CW second/third nap**: REWRITE the section to cover everything from CW-start to NOW. Include content from prior same-CW naps (the agent still remembers it). This is "update" — produce one coherent current-CW arc.
- **First nap post-compaction**: prior CW's content is OBSOLETE (different CW). REPLACE the section entirely; prior content can be fully discarded.
- The block should be DENSE relationally and SPARSE technically. 2-3 paragraphs typical. Name arcs by what-they-meant, not by what-was-done.
- Node IDs are fine in narrative context but should support the arc, not enumerate it.
- If a nap fires before any session content has happened yet (very early nap), skip this sub-step.

**Write-time post-condition:** stated **once**, at the top of this step — it covers this sub-step's write. Read it there and check it here; it is deliberately not repeated, so that 5a, 5b and 5c cannot drift into three versions of one assertion.

**Cross-skill note**: at the end of sleep, this section is ERASED — day is over; clean slate for tomorrow. Next morning's first nap creates the section fresh. The day's overall arc lives in `~/.engram/history/YYYY-MM-DD.md` (under `$ENGRAM_HOME`, default `~/.engram`), not here.

**Anti-patterns specific to 5b:**
- Appending new sessions next to old ones (changelog-style accumulation).
- Preserving prior-CW content "for record" — the compaction summary already covers prior-CW trajectory.
- Re-stating compaction-summary content (PR numbers, focus-rotation specifics, ENGRAM counts, dream-fairy stats) — those belong in the compaction summary.
- Bullet-by-bullet enumeration of work shipped — same.

### 5c. The user's letter/notes — the region between the agent-authored sections and the anchor

The user's letter/notes are immutable. They sit ABOVE the anchor and BELOW the agent-authored sections — and **"everything above the anchor" is not the user's region**, because the agent-authored sections are above them too and 5a rewrites those freely. Find the anchor the same mechanical way the erase finds it: the LAST line matching the anchored regex `^## From this session`, never a substring match, and never by heading name — another seat titles its user section differently (`# Letter from Lei`). Everything between the last agent-authored section and the anchor is the user's and is preserved byte-for-byte. NEVER modify the user's words.

**This is what makes the Step 5 post-condition load-bearing.** The byte-for-byte guarantee here is exactly what its check (2) protects: anything that ends up above the anchor — a duplicated letter, a stray heading, a half-replaced block — is preserved by this rule on every subsequent nap, so it has to be caught at the write that put it there.

**Common-case anti-patterns:**
- Rewriting every nap (purpose drift — the briefing becomes a second changelog)
- Technical changelogs in the session block (that's what `engram_checkpoint` (formerly `engram_nap`) / `engram_advance_turn`'s session_log entries and the compaction summary are for)
- Second-person framing ("you will feel..." — retired with identity v1)
- False warmth (evaluate by reader-effect, not truth-value)

**Note for end-of-day (sleep):** at the end of sleep, the `engram-sleep` skill ERASES the "From this session" section (day is over). The day's overall arc lives in the history file (`~/.engram/history/YYYY-MM-DD.md`, under `$ENGRAM_HOME`, default `~/.engram`), not in warm-briefing. Naps through the day keep the section in-sync-with-current-CW; sleep wipes it for tomorrow.

## Step 6 — Ask {{USER_NAME}} 1-2 questions for next session

Before calling the nap done, identify 1–2 questions where {{USER_NAME}}'s judgment shapes or unblocks the next burst. The point is to give them something to think about *while compaction runs* — so by the time I wake, they have answers ready and the post-compact session opens with direction, not re-derivation.

Good targets:
- A scope or design decision I was working around (not blocked on, but their call outweighs mine)
- A direction I need them to confirm before committing to an approach
- A tradeoff where their preferences/values matter (aesthetics, priorities, relationship choices)

Keep it to 1–2. The budget is {{USER_NAME}}'s thinking time during a short compaction window, not a to-do list.

Include enough context in each question that {{USER_NAME}} can answer without rereading the session — one line of background per question. If the context is long, cite the relevant ENGRAM node IDs (use the IDs as they exist in your install) instead of restating.

**If nothing warrants a question:** say "No decisions pending for next session" rather than inventing one. Manufactured questions waste their attention and train me to perform rather than surface real needs.

Write these questions in the user-facing message alongside the nap summary (Step 8).

## Step 7 — Rotate the focus list for next session

The focus list is the deterministic channel from pre- to post-compact self — the pre-compaction summary renders it verbatim, so post-compact me wakes up pointed at whatever is pinned. The nap is the natural rotation point: I just wrote the new cohort (Step 2) and just named what next session needs (Step 6).

```
engram_list_focused()
```

**Default is to keep pins.** A focused node represents a thread still in flight. Short detours — a quick pivot to fix a bug, a sub-question that opens a fresh line for a few turns, a pause to answer the user — are NOT grounds for unfocus; those threads will want their pins back when I return. Unfocus **only** when a thread is genuinely complete: the question resolved, the derivation integrated, the cornerstone stable under recall, the conjecture promoted or refuted. When in doubt, keep the pin.

**Focus new cornerstones** from this burst: `engram_focus(node_ids=[...], reason="...")` — the key claims, questions, or derivations the next session must pick up intact. Include the thread name in `reason` so future-me can see why each pin exists.

**Redundant-with-auto-load check (#1249).** Before cap-based eviction, scan the focused list for nodes that are already guaranteed to load via CLAUDE.md or warm-briefing regardless of the focus set: goals (`gl_*`), axioms (`ax_*`), cornerstones (`cs_*`), founding identity observations (day-1 cluster, etc.). These nodes occupy a slot without adding cross-compaction value — the auto-load surface carries them whether or not they're pinned. **Unfocus them immediately** (call `engram_unfocus`) unless they are the *direct subject of active work in this burst* (e.g., you are currently revising a goal node's claim or deriving from a specific axiom — not just using one as background context). Fill the freed slots with current-work nodes from Step 2's cohort — the live plans, open questions, and design derivations that exist nowhere in the auto-load surfaces. The failure mode this check prevents: identity nodes silently consuming the entire 15-slot cap while the live working context (today's plan, an open question, a design decision) goes unfocused and is lost at compaction. The focus channel's value is carrying *non-auto-loaded* working context. A goal in focus gains ~nothing; a current-task derivation that exists only in ENGRAM loses everything without it.

**Cap handling (15 max).** If adding new pins would exceed the cap after the redundant-with-auto-load sweep above, evict the **earliest-focused node that is not currently being actively worked on** — oldest-inactive-first, not oldest-overall. This preserves long-running pins that are still load-bearing while shedding truly dormant ones. If every slot is on an active thread, that's a signal to consolidate or split threads rather than force-evict.

**Null-result is valid:** if the list is already aligned with next session's direction, say so and move on. Anti-pattern: performative rotation — touching the list just because this step exists.

**History file refresh.** If this burst shipped a milestone — completed + committed work, not in-progress — append to today's file at `~/.engram/history/YYYY-MM-DD.md` (under `$ENGRAM_HOME`, default `~/.engram`): if the file doesn't exist, create it with `# YYYY-MM-DD` as the heading, then add a bullet (what shipped, why it mattered, commit refs). If the file already exists for today, add a new bullet to it. Don't touch prior-day files. If nothing shipped this burst, skip — don't invent entries. This keeps the directory fresh so fresh-session me walks in with accurate "what I shipped last" context (reads the newest file by filename sort).

## Step 8 — Tell the user what's next

Report briefly to the user:
- What was written (counts or highlights, not a wall of text)
- Whether a feeling was filed or null-reported
- Whether the warm briefing was updated (and why, if so)
- Whether the warm briefing was flagged as not-yet-updated this session (the `warm_briefing.note` check above)
- Whether today's history file was appended (and for what milestone, if so)
- The 1–2 questions for next session (from Step 6), or "No decisions pending"
- What should happen next: continue working, run /compact, run a sleep cycle, or stop for the session

If the nap fired because a compaction warning is active, explicitly say: "Ready to compact — the context burst is preserved in ENGRAM."

## Step 9 — Request compaction

Last, after Step 5b and the focus rotation (Step 7): the context the tool assembles carries the focus list and the warm-briefing arc, so both must be final. This step only leaves a request; the `engram-compaction` mod, if the person has installed it (it is on by default once installed; `enabled: false` turns it off), acts on it at the end of the answered turn that runs this step, so end the turn normally and do not tell the person it will compact "next turn". With the mod off the request is never read (the files stay on disk; an unconsumed request ages out after 30 minutes).

- **A loop marker exists** (`~/.engram/loop-mode.json`, under `$ENGRAM_HOME`, default `~/.engram`): run the tool, no question.
- **No loop marker:** ask first, once — "Compact now? The nap just persisted everything." — and run the tool only on yes. On a no, run `python tools/compact-request.py --clear`, and check that it exits 0. Exit 4 means a file could not be removed, and stderr names it; if it names the old request, that request can still compact, so say so once. Never retry in a loop.

```bash
python tools/compact-request.py --session <your session id> --backup-ok <true|false>
```

`--backup-ok` is the Step 3 verdict (`true` only on `✓ backup ok`); it is optional, and when you omit it the tool reads the verdict the checkpoint recorded. Under a loop marker you may add `--wake-armed-at HH:MMZ --wake-for HH:MMZ` (the armed ScheduleWakeup's times) so the post-compaction context names them. The tool refuses (exit 3) when the backup failed or the warm briefing is not in order (one anchor, a non-empty arc, written since the checkpoint), and says which check on stderr; say so once and move on. Exit 4 means nothing was written; say that too. Never retry in a loop.

---

## Why naps don't advance the turn counter

Turn advance drives exponential importance inflation and therefore forgetting. If naps advanced the turn, a long day with 10 nap checkpoints would burn 10 turns of decay on nodes that haven't even been reviewed yet — punishing nodes the agent hasn't had time to revisit. Naps are persistence-only; genuine memory processing happens in sleep/dream cycles, and THAT is when the turn advances (the turn-advance-after-dream rule, 2026-04-11).

## Relation to other routines

A nap is NOT the first step of a sleep cycle (legacy nap-bundles-into-sleep model retired 2026-05-14 — see `engram-sleep` skill for context). The two-routine model:

- **Nap** — fires multiple times per day at compaction boundaries; per-burst persistence + cross-compaction-scaffold prep
- **Sleep** (`engram-sleep`) — fires once/day at user-driven day-wrap; full end-of-day cycle (cohort review + missed-node capture + warm-briefing erase + consolidation orchestration); pre-turn-advance through Phase A, then turn-advances via dream master in Phase B

The pre-consolidation complete-cohort baseline is established by engram-sleep Phase A (day-wide review of everything since the last sleep), not by a nap immediately before sleep. If you need to nap before starting sleep (e.g., context fills up during day-wrap), that's fine — naps can fire any time at compaction boundaries. Just don't conflate the nap with Phase A; they have different scopes.
