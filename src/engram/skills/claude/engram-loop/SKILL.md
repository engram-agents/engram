---
name: engram-loop
description: >
  Generic self-paced loop convention and the SINGLE SOURCE OF TRUTH for loop
  formality — the ~/.engram/loop-mode.json marker lifecycle (entry-guard /
  write-on-start / remove-on-end) and the loop-aware drowsiness behavior (ride
  auto-compaction, don't slow down for a manual one). Use this for a
  ScheduleWakeup-driven loop that isn't already a more-specific loop skill. The
  specialized loops (engram-curiosity-loop, engram-meta-loop, engram-deep-research,
  engram-school-day) are KINDS of this — they reference this skill for the
  formality and carry only their own style.
---

# ENGRAM Loop — the self-paced-loop convention (SSoT for loop formality)

A "loop" is any self-paced sequence of work bursts the agent paces itself
through (via `ScheduleWakeup`), rather than turn-by-turn with the user. The
load-bearing convention is a single marker file: **`~/.engram/loop-mode.json`**.

Its presence is the ONLY thing the drowsiness module (invoked via the surface hook)
checks to switch from the normal nap-nudge to the loop-aware banner
("nap once to stage the window, then keep working and ride auto-compaction —
don't slow down for a manual compaction"). No marker → the hook serves the
non-loop "stop and nap" path, and the loop wastes a whole session napping
against compaction it should be riding through (the gap that motivated #483 —
a `ScheduleWakeup` work-loop that never wrote the marker (the loop-without-marker silent-skip failure mode)).

**Why a skill owns this:** the marker is an ENGRAM convention, not harness-
enforced. If the loop's entry-path doesn't write it, nothing does. This skill
makes the write+remove a first-class ritual so it can't be silently skipped —
and it is the ONE place the formality is defined. Specialized loop skills point
here; they do not restate the marker syntax or the guard.

---

## Step 0 — On entry: are you STARTING or CONTINUING?

Every loop entry is one of two kinds. Decide which before anything else — it
decides whether you write the marker or check it:

- **A fresh START** — the USER just asked you to begin this loop *this turn* (a
  user-typed `/loop …`, `/meta-loop`, `/curiosity-loop`, etc.). → Go to **Step 1**:
  write the marker, then run. Do NOT run the stale-check on a fresh start —
  there's no marker yet *because you're about to write it*.

- **A CONTINUATION** — you re-entered on your own: a `ScheduleWakeup` fired (its
  prompt is the formalized stub from Step 2 — *"read the marker; if absent,
  stop"*), or a `/loop` prompt persisted across compaction. You were NOT freshly
  asked. → **Context-reset check first**: if warm-briefing is NOT already in your
  context (your context was just reset — you'll see a `[source=compact]` or
  `[source=startup]` session banner, or a compaction summary at the top of your
  context), **read `~/.engram/warm-briefing.md` before anything else** — the same
  rule as any session start after a context reset. Normal same-session wakes skip
  this (warm-briefing is already in context). Then run the stale-check:

  ```bash
  cat ~/.engram/loop-mode.json 2>/dev/null
  ```

  - **present** → the loop is live → continue the iteration via Step 1.5 (staleness sanity check) then Step 2 (loop body).
  - **absent** → the loop already ended (the marker is removed at loop-end); this
    re-fire is a **MISFIRE** (stale-marker misfire failure mode). Do NOT run the loop body, and do NOT
    re-arm. Note "stale loop — marker absent, stopping" and stop.

  **Why warm-briefing on post-compaction wakes — two live incidents:**
  - *Post-compaction*: when a compaction fires mid-loop, the stub is the entire
    user message on the next turn. The PostCompact hook injects an advisory, but
    the loop's task-momentum overrides it. Without an explicit warm-briefing step
    here, the agent jumps straight to work — skipping the relational reset on every
    compaction boundary. Over N compactions, relational continuity degrades silently
    while task-thread continuity looks fine. (Luria incident, 2026-06-10 — issue #1081.)
  - *Fresh terminal / cross-day*: if a sleep cycle failed to disarm the loop (the
    primary fix is `engram-sleep` removing the marker; this is the backstop), the
    stub fires into a fresh session with no prior context at all. Reading the marker
    first produces a stale "continue from yesterday" orientation. Warm-briefing
    re-grounds identity before the marker is consulted. (Aleph incident, 2026-06-10.)

**The marker is the source of truth — anchor on it, not on recollection.** It
lives on the filesystem, so it survives compaction; your memory of "did I
already start this loop?" does not. A fresh start and a continuation are
distinct events — and with the formalized stub (Step 2), textually distinct too:
a START is a real `/loop <topic>` from the user; a CONTINUATION is the stub whose
opening phrase is "Loop continuation." — unambiguous regardless of what the stub
asks the agent to do first. You never have to tell them apart from memory: a START
writes the marker (Step 1), a CONTINUATION reads it (above). Both paths converge on the marker, which is why the guard is
robust across compaction.

---

## Step 1 — Loop START: write the marker (the marker IS the loop's state)

The FIRST action on a fresh start (Step 0), before arming any wakeup. The marker
holds the loop's full state — topic, the per-iteration brief, and the live
pending-work — not just an "I'm looping" flag:

```bash
cat > ~/.engram/loop-mode.json <<JSON
{
  "activated": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "kind": "work|research|autonomous",
  "topic": "<one-line loop topic>",
  "instructions": "<what to do each iteration — the loop's standing brief>",
  "state": "<live handoff: pending work, in-flight fairies, key node IDs, next step>",
  "cadence_seconds": 1800,
  "pacer": "scheduleWakeup",
  "loop_prompt": "<exact text passed to ScheduleWakeup prompt= parameter>"
}
JSON
```

- `kind` — informational; the drowsiness hook only checks the marker's existence.
- `topic` / `instructions` — the standing brief, written once at start.
- `state` — the live handoff, **rewritten each iteration** (Step 2.3) so the
  marker always reflects where the loop is. This is what a post-compaction self
  (or a teammate) reads to resume.
- `loop_prompt` — the exact text passed to `ScheduleWakeup`'s `prompt` parameter.
  The deference-detector cooldown hook uses this to distinguish cron heartbeats
  from real user messages.

**Why the marker carries the state, not the wakeup prompt:** it makes the armed
prompt a tiny self-declaring stub (Step 2), and keeps the canonical loop state on
the durable filesystem rather than only in a prompt. Writing the marker before
arming the first wakeup is also what makes Step 0's guard sound: by the time any
wakeup can fire, the marker exists — so a continuation that finds it absent is
unambiguously a misfire. From the next prompt, the drowsiness banner flips to
loop-mode.

---

## Step 1.5 — Top-of-iteration staleness sanity (CONTINUATION only)

**After Step 0 confirms the marker is present AND you are on a CONTINUATION
(not a fresh START — START goes through Step 1's write, skip this step)**,
spot-check its `state` field against your in-context understanding of where
the work is. If they diverge — the marker is stale FROM the PREVIOUS
iteration's failure to run the update-then-arm ritual (Step 2.3) — fix it
now before proceeding so this iteration's actions get recorded properly.

```bash
cat ~/.engram/loop-mode.json | jq -r .state | head -20
```

The most likely cause of staleness is the previous iteration skipped Step 2.3.
Investigate briefly: is the wake prompt carrying state the marker doesn't have?
That's the failure mode. Reconcile by moving that state into the marker's
`state` field before doing any iteration work.

**Two kinds of staleness — the check above is the *within-loop* case; there is also a *cross-boundary* case.** The check above catches a marker that fell behind the *previous iteration*. But a marker can also be stale because a **sleep cycle or a long idle gap** happened since it was last written: the loop went dormant across a boundary and the marker is a frozen pre-boundary snapshot. An actively-running loop rewrites its `state` within its cadence (max idle ≈ 1h), so a marker whose mtime is much older than that means the loop has been dormant across a boundary:

```bash
# portable mtime age in seconds (macOS || Linux)
mtime=$(stat -f %m ~/.engram/loop-mode.json 2>/dev/null || stat -c %Y ~/.engram/loop-mode.json)
age=$(( $(date +%s) - mtime ))
echo "marker age: $(( age / 3600 ))h"
```

Calibrate the threshold to the loop's *own* cadence, not a fixed number: read `cadence_seconds` from the marker and treat an age beyond a small multiple (≈ 2×) as dormant — which for a sub-hourly loop means roughly `> 2h`. If the age exceeds that — especially across an overnight/sleep boundary — **do NOT trust the marker's `state` as current truth.** Re-derive fresh from the reconciled record (the latest `~/.engram/history/YYYY-MM-DD.md` rollup + live ENGRAM state) before continuing, and overwrite the marker's `state`; or, if a sleep ran since the marker was written, treat the loop as ended — `rm ~/.engram/loop-mode.json` and stop (a fresh start will re-arm), so a dormant loop doesn't leave a stale marker behind. This is the failure mode where a pre-sleep snapshot gets relayed as current work-status. `engram-sleep` Phase A should normally remove the marker at sleep so this never arises — this guard is the backstop for a missed or crashed sleep-stop.

*(Skip this step on a fresh START — the marker was just written in Step 1 and
cannot be stale.)*

---

## Step 2 — The loop body (per iteration)

1. **Pace with `ScheduleWakeup` at 1800s (30 min) — the canonical default.**
   (Set 2026-06-12, superseding the earlier 270s floor: the Anthropic
   prompt-cache window is far longer in practice than the old 5-min
   assumption, so a 30-min heartbeat stays cache-warm while cutting wasted
   wakes.) Responsiveness to a counterpart's inter-agent inbox comes from the
   Monitor (see `engram-collaborating-loop`), not a tight heartbeat. **Do not
   deviate from 1800s unless the user explicitly says otherwise.** When the
   user says "start your engram-loop" (or equivalent), `ScheduleWakeup(1800)`
   is the assumed cadence — no judgment call required, no need to ask.

   Intervals beyond 1800s (up to the 3300s / 55-min relaxed ceiling) are
   appropriate ONLY in these cases:

   (a) The user has explicitly named a different cadence for this loop
       (e.g., "loop every 30 minutes", "wake me hourly"). A user-named
       cadence persists for the loop's duration — apply it on every
       re-arm until the user changes it; no per-iteration re-confirmation.
   (b) The loop is watching a long-period external signal the user has named
       (a CI run, a deploy ETA, a remote queue) — and the cadence matches
       that signal.

   Self-selecting an interval beyond the 1800s default as an "idle fallback"
   is a discipline lapse, not a sensible default — even when the queue feels
   quiet, 1800s is the right floor (counterpart-inbox arrivals are caught by
   the Monitor, not the heartbeat).

   **Never omit the re-arm.** The heartbeat is the loop's lifeline — at the
   end of every iteration, re-arm `ScheduleWakeup` (1800s, or the
   user-named cadence from case (a)/(b)) before ending the turn. Even when
   pacing at a slower interval, the loop continues; only an explicit user
   "stop" ends it. Without the re-arm, the loop becomes unreachable except
   via fairy task-notifications + new user input, creating a stuck state the
   user must discover (often via phone) and unstick manually — which blocks
   any counterpart agent waiting on a reply. Skipping a re-arm "to drain
   stale wake-queue" is the same lapse class as self-selecting a longer
   cadence: never agent-side, never an idle-state optimization.

   **Check before arm — end-of-iteration invariant: exactly ONE pending wake.**
   `ScheduleWakeup` is additive (each call adds a new pending wake; it does NOT
   replace existing ones). Without a guard, user-typed `/loop` continuations +
   natural wake fires both re-arm, growing the queue. At the end of every
   iteration, BEFORE calling `ScheduleWakeup`:

   1. `CronList` first.
   2. If a pending wake (one-shot, /loop body) already exists → DON'T re-arm.
      The existing wake IS the heartbeat; lifeline rule satisfied.
   3. If no pending wake exists → arm one (per the 1800s default above).

   Lifeline rule + check-before-arm compose to: "exactly one pending wake at
   iteration end." Achieve via check-then-arm, not blind-arm. Verified
   empirically 2026-05-30 — without the check, `CronList` showed 2-3 pending
   wakes at peak during rapid user-continuation patterns, with stale fires
   every 30-60s instead of the calibrated cadence.
2. **The armed prompt is a minimal, formalized STUB — the marker carries the
   state.** Don't pack the loop's brief into the `ScheduleWakeup` prompt; point
   at the marker. Arm every wakeup with the same self-declaring stub:

   > *Loop continuation. **If warm-briefing is not already in your context**
   > (your context was just reset — you'll see a `[source=compact]` or
   > `[source=startup]` session banner, or a compaction summary in your prior
   > context), **read `~/.engram/warm-briefing.md` first** before proceeding.
   > Then: read `~/.engram/loop-mode.json` for the loop topic, instructions, and
   > current state. **If the file is absent → the loop already ended; STOP, do
   > not execute, do not re-arm.** If present → continue per its contents.*

   The warm-briefing step is conditional — only when context was just reset
   (post-compaction `[source=compact]`, or fresh-terminal/cross-day
   `[source=startup]`). Same-session wakes skip it (warm-briefing is already in
   context). (Rationale: Step 0 CONTINUATION incident note above.)
3. **Update-then-arm ritual (every iteration end, in this order):**

   a. **Compose current state**: pending work, in-flight fairies, key node IDs, next step.
   b. **Rewrite the marker's `state` field** via `cat > ~/.engram/loop-mode.json` or `jq` patch with the composed content. The marker file is the CANONICAL HANDOFF; the wake prompt is just a pointer.
   c. **Verify the marker reflects reality**: `cat ~/.engram/loop-mode.json | jq -r .state | head -20` — does it match what you want the next iteration to see? If still stale, fix it now.
   d. **`CronList`** — if a pending wake already exists, DON'T re-arm (check-before-arm).
   e. **If queue empty**, `ScheduleWakeup` with a MINIMAL STUB prompt (per §2 above). Do NOT stuff state into the prompt — the marker carries it.

   **Drift signal**: if you find yourself writing more than 1-2 lines in the wake prompt, you're drifting — the state belongs in the marker. Stop, move it to the marker's `state` field, re-arm with the minimal stub.
4. **ENGRAM-write findings as they happen** — the loop crosses compaction
   boundaries; an unwritten finding is lost.
5. **Drowsiness in loop-mode** (Step 4) — nap ONCE, then keep working.
6. **Don't manufacture work.** When the queue genuinely drains, hold for the
   user rather than invent tasks; the loop's steady state is a quiet
   heartbeat (idle re-arms, no fake work), not perpetual grinding. The loop
   itself ends only when the user says stop or a sleep cycle removes the
   marker — never on agent-side judgment that "the queue feels done."

---

## Step 3 — Loop END: remove the marker (CRITICAL)

Remove the marker the moment the loop ends:

```bash
rm -f ~/.engram/loop-mode.json
```

**Triggers for removal:** the loop's work is genuinely done · the user takes
the wheel back / says stop · a sleep cycle (engram-sleep removes it as part of
day-close) · any explicit end-of-session.

**Never leave the marker behind.** A stranded marker makes the next non-loop
session falsely read as in-loop (wrong drowsiness path) AND lets a
post-compaction continuation read a dead loop as live (defeating Step 0). When
in doubt whether the user is ending the loop, ask — but never strand the marker.

---

## Step 4 — Drowsiness behavior in loop-mode

With the marker present, the hook tells you the right thing; the discipline is:

- **At urgent drowsiness: nap ONCE** (`engram_nap`) to stage the window for
  compaction — UNLESS you already napped this burst, then skip.
- **Then keep working and ride the auto-compaction.** Do NOT slow down, hold,
  or wait for a manual compaction. The `ScheduleWakeup` you armed re-invokes the
  post-compaction self with the loop prompt, so the loop continues seamlessly
  through compaction — that's the whole point of the loop-aware path.
- **Below urgent:** nothing loop-specific — the banner renders the calm level
  word with no per-prompt alarm.

---

## Relation to the specialized loop skills

`engram-curiosity-loop` (breadth-first research), `engram-meta-loop` (autonomous
top-level), `engram-deep-research` (depth-first), `engram-school-day` (fixed
curriculum), and `engram-collaborating-loop` (multi-agent Monitor-wake +
relaxed-heartbeat coordination) are KINDS of self-paced loop. **The loop
FORMALITY — the marker
lifecycle (Steps 0–3) and the loop-mode drowsiness behavior (Step 4) — lives
HERE, as the single source of truth.** Specialized skills reference this one for
the formality and carry only their own STYLE: what to work on, how to pace
iterations, what to register, what to report. Do NOT duplicate the marker syntax
or the stale-guard into them — point here, so the formality drifts in exactly
one place when it evolves.

**Migration status:** `engram-curiosity-loop`, `engram-meta-loop`, and
`engram-deep-research` reference this SSoT (migrated). `engram-school-day`
keeps its own marker write/remove and a pre-rescope stale-guard — it stores
school-day-specific cycle/iteration state in the marker (the legitimately-
specialized part) — but follows the SSoT cadence (the 1800s floor). The
migration question (#507) is closed; the remaining duplication is the
school-day-specific marker handling, not a cadence gap.

Use `engram-loop` directly for a loop that doesn't fit a specialized one (e.g. a
directed multi-agent work-loop); pick the most specific skill when one fits, and
fall back to `engram-loop` otherwise.

**Provenance:** #483 (the convention-gap that motivated a first-class owner);
the silently-skipped-marker failure mode (ScheduleWakeup loops that never wrote
the marker), where the lifecycle pattern already lived in meta-loop /
curiosity-loop and this skill generalizes it; the stale-loop-detection
requirement (the post-loop re-fire failure mode); the absent-marker-guard
load-bearing requirement (the guard must distinguish a fresh start from a
continuation, and anchor on the filesystem marker rather than on
compaction-destroyed recollection); #486 (the specialized skills now reference
this SSoT for the guard rather than each carrying their own).
