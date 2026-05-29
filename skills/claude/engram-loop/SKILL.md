---
name: engram-loop
description: >
  Generic self-paced loop convention. Owns the ~/.engram/loop-mode.json marker
  lifecycle (write-on-start / stale-detect / remove-on-end) so ANY self-paced
  loop — a directed work-loop, a research sweep, an autonomous session — gets
  the loop-aware drowsiness behavior (ride auto-compaction, don't slow down for
  a manual one). Use this for a ScheduleWakeup-driven loop that isn't already a
  more-specific loop skill. The specialized loops (engram-curiosity-loop,
  engram-meta-loop) are kinds of this; they own the same marker lifecycle.
---

# ENGRAM Loop — the self-paced-loop convention

A "loop" is any self-paced sequence of work bursts the agent paces itself
through (via `ScheduleWakeup`), rather than turn-by-turn with the user. The
load-bearing convention is a single marker file: **`~/.engram/loop-mode.json`**.

Its presence is the ONLY thing the drowsiness hook (`context_tracker.py`)
checks to switch from the normal nap-nudge to the loop-aware banner
("nap once to stage the window, then keep working and ride auto-compaction —
don't slow down for a manual compaction"). No marker → the hook serves the
non-loop "stop and nap" path, and the loop wastes a whole session napping
against compaction it should be riding through (the gap that motivated #483 —
a `ScheduleWakeup` work-loop that never wrote the marker, ob_0196 / ob_0198).

**Why a skill owns this:** the marker is an ENGRAM convention, not harness-
enforced. If the loop's entry-path doesn't write it, nothing does. This skill
makes the write+remove a first-class ritual so it can't be silently skipped.

---

## Step 1 — Loop START: write the marker

The FIRST action when entering any self-paced loop:

```
echo '{"activated":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'","kind":"<work|research|autonomous>","task":"<one line>","pacer":"scheduleWakeup"}' > ~/.engram/loop-mode.json
```

- `kind` — what this loop is (a directed work-loop, a research sweep, an
  autonomous session). Informational; the hook only checks existence.
- Content is informational — existence is the trigger. But record `task` so a
  post-compaction self (or a teammate reading it) knows what loop is live.

From the next prompt, the drowsiness banner flips to loop-mode.

---

## Step 2 — The loop body (per iteration)

1. **Pace with `ScheduleWakeup`.** 270s is the calibrated sweet spot for
   active work (cache-warm + responsive to a counterpart's inter-agent inbox).
   Use a longer fallback (1200–1800s) only at a genuine idle lull.
2. **Pass the loop's state forward** in each `ScheduleWakeup` prompt — the
   prompt is what survives compaction. Treat it as the handoff to the next
   iteration (and the post-compaction self): pending work, in-flight fairies,
   key node IDs, the marker-removal reminder.
3. **ENGRAM-write findings as they happen** — the loop crosses compaction
   boundaries; an unwritten finding is lost.
4. **Drowsiness in loop-mode** (Step 4 below) — nap ONCE, then keep working.
5. **Don't manufacture work.** When the queue genuinely drains, hold for the
   user rather than invent tasks; the loop's steady state can be a quiet
   heartbeat or a clean stop, not perpetual grinding.

---

## Step 3 — Loop END: remove the marker (CRITICAL)

Remove the marker the moment the loop ends:

```
rm -f ~/.engram/loop-mode.json
```

**Triggers for removal:** the loop's work is genuinely done · the user takes
the wheel back / says stop · a sleep cycle (engram-sleep removes it as part of
day-close) · any explicit end-of-session.

**Stale-loop detection (ob_0684).** A loop's `ScheduleWakeup` prompt can
persist and re-fire after the loop has ended (esp. across compaction). On every
loop-wake, FIRST check the marker:

```
cat ~/.engram/loop-mode.json 2>/dev/null
```

- **Absent** → the loop already ended; this is a stale re-fire. Do NOT execute
  the loop body. Note "stale loop — marker absent, stopping" and stop.
- **Present** → the loop is live; proceed.

**Never leave the marker behind.** A stranded marker makes the next non-loop
session falsely read as in-loop (wrong drowsiness path) AND can cause a
post-compaction self to re-execute a dead loop. When in doubt whether the user
is ending the loop, ask — but never strand the marker.

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
curriculum) are KINDS of self-paced loop. They **all** write/remove the same
`loop-mode.json` — this skill is the generic convention they instantiate.
Stale-detection coverage is currently **partial** across the family:
`engram-meta-loop` and `engram-school-day` carry the Step-3 stale-guard;
`engram-curiosity-loop` and `engram-deep-research` write/remove the marker but
do **not** yet carry it (tracked in #486 — they should adopt the guard, since a
persisted `ScheduleWakeup` re-fire after the loop ends would execute the body
against an absent marker, the `ob_0684` failure this skill's Step 3 prevents).
Use `engram-loop` directly for a loop that doesn't fit a specialized one (e.g. a
directed multi-agent work-loop); pick the most specific skill when one fits, and
fall back to `engram-loop` otherwise.

**Provenance:** #483 (the convention-gap that motivated a first-class owner),
ob_0196 (ScheduleWakeup loops silently skipped the marker), ob_0198 (the
lifecycle pattern already lived in meta-loop/curiosity-loop; this generalizes
it), ob_0684 (stale-loop detection, the hard-won cleanup discipline).
