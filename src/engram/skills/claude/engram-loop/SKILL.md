---
name: engram-loop
description: >
  Generic self-paced loop convention and the SINGLE SOURCE OF TRUTH for loop
  formality — the ~/.engram/loop-mode.json marker lifecycle (entry-guard /
  write-on-start / remove-on-end) and the loop-aware drowsiness behavior
  (capture what the summary won't carry, then ride auto-compaction — don't
  slow down for a manual one). Use this for a harness-selected self-paced
  loop that isn't already a more-specific loop skill. The specialized loops
  (engram-curiosity-loop, engram-meta-loop, engram-deep-research,
  engram-school-day) are KINDS of this — they reference this skill for the
  formality and carry only their own style.
---

# ENGRAM Loop — the self-paced-loop convention (SSoT for loop formality)

A "loop" is any self-paced sequence of work bursts the agent paces through
via its harness's selected wake mechanism, rather than turn-by-turn with the
user. The load-bearing convention is a single marker file:
**`~/.engram/loop-mode.json`**.

Its presence is the ONLY thing the drowsiness module (invoked via the surface hook)
checks to switch from the normal nap-nudge to the loop-aware banner
("compaction is near and the summary will not carry your work across it —
capture the LAST burst (history arc + nodes), nap to checkpoint, then keep
working and ride auto-compaction"). No marker → the hook serves the
non-loop "stop and nap" path, and the loop wastes a whole session napping
against compaction it should be riding through (the gap that motivated #483 —
a `ScheduleWakeup` work-loop that never wrote the marker (the loop-without-marker silent-skip failure mode)).

**Why a skill owns this:** the marker is an ENGRAM convention, not harness-
enforced. If the loop's entry-path doesn't write it, nothing does. This skill
makes the write+remove a first-class ritual so it can't be silently skipped —
and it is the ONE place the formality is defined. Specialized loop skills point
here; they do not restate the marker syntax or the guard.

---

## Select the harness **before any loop action**

This skill has two pacers. Select the runtime path before reading a marker
example or arming procedure:

- **Managed Codex** — follow the **Codex pacer** section immediately below.
  It is the complete Codex transaction and uses the managed wake bridge plus a
  per-agent systemd timer. Do **not** continue into the numbered Claude steps.
- **Claude Code** — skip the Codex section and continue at **Step 0**. The
  numbered steps, including their marker example and wake-arming ritual, are
  Claude-specific.

## Codex pacer — managed daemon + self-addressed Umwelt heartbeat

This is the complete managed-Codex arming flow. The harness selection above
routes Codex here before any Claude marker example or wake procedure. Do not
use the numbered Claude steps below; this transaction contains the Codex
marker and pacing semantics.

Codex has no `ScheduleWakeup`, `CronList`, or `Monitor` tools. Its equivalent is
the persistent `codex-umwelt-wake.py` bridge plus a systemd timer that sends a
self-addressed Umwelt wake.

### C1. Arm the provisioned host in one fail-fast transaction

This is a **pre-provisioned-host workaround**, not a fresh-install provisioner.
The tracked plugin ships the wake bridge but does not yet ship the private
heartbeat script/units. Preflight both managed units BEFORE creating live loop
state; absence is a hard start failure that leaves no marker.

Run the whole arming transaction in **one Bash invocation**. Do not split these
snippets across Codex shell calls: shell variables do not persist between calls.
Fill the seven inputs first; the canonical cadence is 1800 unless the user
explicitly named another value.

```bash
cat > /tmp/arm-codex-loop.sh <<'BASH'
#!/usr/bin/env bash
set -Eeuo pipefail
: "${CURRENT_THREAD:?}" "${END_AT:?}" "${LOOP_KIND:?}" "${TOPIC:?}" "${INSTRUCTIONS:?}" "${STATE:?}"
CADENCE_SECONDS=${CADENCE_SECONDS:-1800}
root="$HOME/.engram/codex-wake/loop-host-state"
pending="$HOME/.engram/loop-mode.json.pending"
marker="$HOME/.engram/loop-mode.json"
test ! -e "$marker" || { echo "an active loop marker already exists" >&2; exit 1; }
wake_unit=$(systemctl --user list-unit-files --type=service --no-legend | awk '$1 ~ /codex-umwelt-wake\.service$/ {print $1; exit}')
unit=$(systemctl --user list-unit-files --type=timer --no-legend | awk '$1 ~ /codex-loop-heartbeat\.timer$/ {print $1; exit}')
test -n "$wake_unit" && test -n "$unit" || { echo "Codex loop host is not provisioned" >&2; exit 1; }
systemctl --user cat "$wake_unit" | grep -q 'codex-umwelt-wake.py' || { echo "wrong wake service" >&2; exit 1; }

dropin_dir="$HOME/.config/systemd/user/$wake_unit.d"
thread_dropin="$dropin_dir/thread.conf"
cadence_dir="$HOME/.config/systemd/user/$unit.d"
cadence_dropin="$cadence_dir/cadence.conf"
rm -rf "$root"; mkdir -p "$root" "$dropin_dir" "$cadence_dir"
test -e "$thread_dropin" && cp "$thread_dropin" "$root/thread.conf" || : > "$root/thread.absent"
test -e "$cadence_dropin" && cp "$cadence_dropin" "$root/cadence.conf" || : > "$root/cadence.absent"
systemctl --user is-enabled --quiet "$unit" && : > "$root/timer.enabled" || : > "$root/timer.disabled"
systemctl --user is-active --quiet "$unit" && : > "$root/timer.active" || : > "$root/timer.inactive"
printf '%s\n' "$wake_unit" > "$root/wake-unit"; printf '%s\n' "$unit" > "$root/timer-unit"

restore() {
  rm -f "$pending" "$marker"
  test -e "$root/thread.conf" && cp "$root/thread.conf" "$thread_dropin" || rm -f "$thread_dropin"
  test -e "$root/cadence.conf" && cp "$root/cadence.conf" "$cadence_dropin" || rm -f "$cadence_dropin"
  systemctl --user daemon-reload || true
  systemctl --user restart "$wake_unit" || true
  if test -e "$root/timer.enabled"; then systemctl --user enable "$unit" >/dev/null || true; else systemctl --user disable "$unit" >/dev/null || true; fi
  if test -e "$root/timer.active"; then systemctl --user restart "$unit" || true; else systemctl --user stop "$unit" || true; fi
}
trap 'rc=$?; restore; exit $rc' ERR INT TERM

cat > "$thread_dropin" <<EOF
[Service]
Environment=CODEX_THREAD_ID=$CURRENT_THREAD
EOF
cat > "$cadence_dropin" <<EOF
[Timer]
OnActiveSec=
OnUnitActiveSec=
OnUnitActiveSec=${CADENCE_SECONDS}s
EOF
agent=$(jq -er .agent "$HOME/.umwelt/config.json")
jq -n --arg activated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg end_at "$END_AT" \
  --arg kind "$LOOP_KIND" --arg topic "$TOPIC" --arg instructions "$INSTRUCTIONS" --arg state "$STATE" \
  --arg prompt "@$agent Loop heartbeat: read ~/.engram/loop-mode.json and continue the active bounded loop; remove the marker when end_at has passed." \
  --argjson cadence "$CADENCE_SECONDS" '{FIRST_ACTION:"FIRST: verify exact bridge thread and one timer trigger; check end_at; then work and update state.",activated:$activated,end_at:$end_at,kind:$kind,topic:$topic,instructions:$instructions,state:$state,cadence_seconds:$cadence,pacer:"codex-systemd-umwelt-self-wake",loop_prompt:$prompt}' > "$pending"

systemctl --user daemon-reload
systemctl --user restart "$wake_unit"
systemctl --user enable --now "$unit"
systemctl --user restart "$unit"
WAKE="$HOME/.engram/marketplace/plugins/engram/tools/codex-umwelt-wake.py"
status=$("$HOME/.engram/venv/bin/python" "$WAKE" status)
test "$(jq -r .running <<<"$status")" = true
test "$(jq -r .thread_id <<<"$status")" = "$CURRENT_THREAD"
systemctl --user list-timers "$unit" --no-pager | grep -Fq "$unit"
mv "$pending" "$marker"
trap - ERR INT TERM
echo "armed $unit for thread $CURRENT_THREAD"
BASH
chmod +x /tmp/arm-codex-loop.sh

CURRENT_THREAD='<Active session id>' END_AT='<ISO-8601 UTC bound>' \
LOOP_KIND='<kind>' TOPIC='<topic>' INSTRUCTIONS='<instructions>' STATE='<state>' \
CADENCE_SECONDS=1800 /tmp/arm-codex-loop.sh
```

If the user named only a cadence and not an end time, use today's documented
sleep boundary; do not create an unbounded recurring marker. At every wake,
check `end_at` first and remove the marker when it has passed.

### C2. Verify continuations

The transaction binds the current injected session ID explicitly; the bridge's
normal newest-session discovery is not sufficient for a bounded loop. On every
wake, rederive unit names from the persisted host-state (never from old shell
variables) and verify the shipped bridge plus timer:

```bash
WAKE="$HOME/.engram/marketplace/plugins/engram/tools/codex-umwelt-wake.py"
PY="$HOME/.engram/venv/bin/python"
root="$HOME/.engram/codex-wake/loop-host-state"
wake_unit=$(cat "$root/wake-unit"); unit=$(cat "$root/timer-unit")
current_thread='<session id from the Active session banner>'
status=$("$PY" "$WAKE" status)
echo "$status"
test "$(jq -r .running <<<"$status")" = true
test "$(jq -r .thread_id <<<"$status")" = "$current_thread"
systemctl --user list-timers "$unit" --no-pager | grep -Fq "$unit"
```

A mismatch is a hard start failure. Do not open a second local TUI or run a
detached `umwelt-bell.py`; the watcher and visible TUI must share the managed
app-server. Use `agentctl session` / `agentctl attach` to repair the topology.

### C3. End and restore exact prior host state

On each continuation, first verify the bridge's `running` + exact `thread_id`
and the timer's next trigger, then check `end_at`, work, and update marker
`state`. A marker alone is not an armed loop. Ending removes the marker; the
timer may remain enabled because its service exits silently without a marker.
On normal end, use one fresh fail-fast invocation. It restores both prior
drop-ins and the timer's prior enabled **and active** states:

```bash
set -Eeuo pipefail
root="$HOME/.engram/codex-wake/loop-host-state"
wake_unit=$(cat "$root/wake-unit"); unit=$(cat "$root/timer-unit")
thread_dropin="$HOME/.config/systemd/user/$wake_unit.d/thread.conf"
cadence_dropin="$HOME/.config/systemd/user/$unit.d/cadence.conf"
rm -f "$HOME/.engram/loop-mode.json" "$HOME/.engram/loop-mode.json.pending"
test -e "$root/thread.conf" && cp "$root/thread.conf" "$thread_dropin" || rm -f "$thread_dropin"
test -e "$root/cadence.conf" && cp "$root/cadence.conf" "$cadence_dropin" || rm -f "$cadence_dropin"
systemctl --user daemon-reload
systemctl --user restart "$wake_unit"
if test -e "$root/timer.enabled"; then systemctl --user enable "$unit" >/dev/null; else systemctl --user disable "$unit" >/dev/null; fi
if test -e "$root/timer.active"; then systemctl --user restart "$unit"; else systemctl --user stop "$unit"; fi
rm -rf "$root"
```

This removes the per-loop thread pin and returns cadence and timer state to
exactly what preceded the loop; it never leaves a completed loop bound to a
dead thread.

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

  - **present** → the loop is live → run the **engaged-gate** (below) first, then continue the iteration via Step 1.5 (staleness sanity check) then Step 2 (loop body).
  - **absent** → the loop already ended (the marker is removed at loop-end); this
    re-fire is a **MISFIRE** (stale-marker misfire failure mode). Do NOT run the loop body, and do NOT
    re-arm. Note "stale loop — marker absent, stopping" and stop.

  **Presence gate — defer to a present/focused user before autonomous work (#1456).**
  On a CONTINUATION wake with the marker present, run the loop self-gate *before*
  the loop body:

  ```bash
  "$CLAUDE_PLUGIN_ROOT/tools/engram-loop-gate.py"          # prints "defer <N>" or "proceed"
  "$CLAUDE_PLUGIN_ROOT/tools/engram-loop-gate.py" --json   # adds the reason — read it when deferred
  ```

  - **`proceed`** → no present/focused user → continue to Step 1.5 → Step 2 as normal.
  - **`defer <N>`** → the user is *here* (or has asked to be left focused). **Do NOT
    run the loop body.** Re-arm `ScheduleWakeup` for `<N>` seconds with the same loop
    prompt, and stop. This is **defer-not-drop** — the marker stays, no work is lost,
    the wake just slides past the suppression window.

  **Surface *why* you deferred (suspended ≠ dead).** Read the `--json` `reason` and
  put it in your stop line, so a presence-suspended loop never reads as a stuck/dead
  one:
  - `user-engaged` — a genuine human prompt within the *engaged window*
    (`cadence.engaged_window_seconds`, default 360s). Sliding: each user message
    resets it; the loop wakes once they go quiet.
  - `presence-derived-user` — presence mode resolved to `user` from recent activity
    within the *cooldown window* (`cadence.cooldown_seconds`, default 600s ⊇ the
    engaged window). The presence-aware gate (#1489) is the higher control; the raw
    engaged-defer is its fallback.
  - `presence-override-user` — an **explicit** `engram-mode user` override is active
    (Lei or I deliberately suspended the loop). Defers until the override's TTL
    expires, or indefinitely until `engram-mode clear`.

  **Controlling it — `engram-mode` (the mode SSoT the gate reads):**
  - `engram-mode user [--ttl <seconds>]` → suspend the loop deliberately ("I'm
    focused with the user — hold autonomous wakes"). Without `--ttl` it holds until
    cleared — a quiet footgun: it suspends *every* cooldown indefinitely until
    `engram-mode clear`, so prefer a TTL unless you mean "until I say otherwise".
  - `engram-mode auto [--ttl <seconds>]` → resume / "go on your own": an explicit
    auto override even **beats** a recent-activity engaged-defer (the deliberate "I'm
    autonomous now" signal). With `--ttl` it's time-boxed (revert to derived after N
    seconds); without, it holds until `engram-mode clear`. `engram-mode clear` reverts
    to the derived (activity-based) mode.
  - `engram-mode show [--json]` → inspect the current mode + resolved bundle without
    changing anything (use it to check whether the loop is presence-suspended).

  Why: the monitors already suppress real-time events during the engaged window
  (#1077 part 2), but the `ScheduleWakeup` heartbeat did not — so an autonomous
  loop-wake could fire mid-conversation and talk over the user (the gap that
  motivated #1456). #1469 closed that with the engaged-defer; #1489 made the gate
  **presence-aware**, so an explicit `engram-mode user` (or the wider cooldown
  window) also suspends it, with the raw `last-user-activity` engaged-defer as the
  fallback. It is **fail-open**: a missing stamp or any gate error prints `proceed`,
  and an absent presence layer degrades to the raw engaged-defer (itself fail-open on
  a missing stamp) — so a broken gate never deadlocks the loop. On error it errs
  toward `proceed` (the loop runs), never toward a stuck defer: the accepted cost is
  it may run while the user is present, never that it freezes waiting on a dead gate.

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
  "FIRST_ACTION": "THE RE-ARM NOW HAPPENS LAST, NOT FIRST — see Step 2 item 3 (arm-last ritual; if the turn carries a reply the human must see, item 3's reply-carrying split applies: batch the arm with a sibling tool call in one parallel block, reply as the turn's final text, #2098). Do the iteration's actual work first, using this marker's 'state' + 'instructions' fields. At the END of the turn — the FINAL action, after the work is done AND this marker's 'state' field is rewritten to reflect it: ScheduleWakeup at cadence_seconds, unconditionally (no CronList-before-arm check — replacement is unconditional, so a redundant arm is free, never stacks). For the prompt COPY THIS FILE'S OWN loop_prompt FIELD VERBATIM — copy-paste it; never hand-type or paraphrase it, and note that the word 'stub' invites paraphrasing, so don't. Then CronList again and confirm the armed prompt actually LEADS with the <loop-wake> marker — an ABSENT marker, or one that is present but not leading, makes the wake self-stamp as user activity and the loop defers perpetually. An escaped &lt;loop-wake&gt; is fine: the classifiers HTML-unescape before matching, so a mangled marker still classifies correctly; absence and position are what they cannot rescue.",
  "activated": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "kind": "work|research|autonomous|heartbeat",
  "topic": "<one-line loop topic>",
  "instructions": "<what to do each iteration — the loop's standing brief>",
  "state": "<live handoff: pending work, in-flight fairies, key node IDs, next step>",
  "cadence_seconds": 1800,
  "pacer": "scheduleWakeup",
  "loop_prompt": "<exact text passed to ScheduleWakeup prompt= parameter>"
}
JSON
```

- `FIRST_ACTION` — kept as field #1 deliberately so it's the first thing a
  reader sees, even though (as of 2026-08-12, #1973/#1987) the ARM ITSELF
  now happens LAST in the iteration (Step 2 item 3, arm-last ritual) — the
  field name marks *reading* order, not *execution* order: read this first
  so the end-of-turn arm invariant is in view before diving into `state` and
  letting it degrade into something to remember. **This is the BACKSTOP; the
  wake prompt (Step 2 §2) is the primary channel** — the marker gets
  rewritten wholesale each iteration, so an invariant living here can be
  silently lost, whereas the prompt cannot. Keep both: they fail differently
  (a hand-armed wake missing the stub is caught by this field; a first arm
  that omitted the line propagates through every later arm and is caught
  only here). Never reorder it below `state`. The text is fixed; copy it
  verbatim.
- `kind` — informational; the drowsiness hook only checks the marker's existence.
  `"heartbeat"` is the standard token for the quiet on-call cache-warm loop the
  boot charge enters (grep target for reachability audits — write exactly this
  value for that case, per the 2026-08-02 fleet-silence follow-up).
- `topic` / `instructions` — the standing brief, written once at start.
- `state` — the live handoff, **rewritten each iteration** (Step 2 item 3b) so
  the marker always reflects where the loop is. This is what a post-compaction
  self (or a teammate) reads to resume.
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
iteration's failure to run the arm-last ritual's marker-update step
(Step 2 item 3b) — fix it now before proceeding so this iteration's actions
get recorded properly.

**A stale marker remains a TOLERATED, recoverable failure** (Step 2 item 3)
— if an iteration forgets the marker `state` rewrite (b) but still arms (c),
this staleness check catches and reconciles it next time; if it forgets to
arm (c) entirely, the Stop hook's loop-arm nudge (#1973) is now the safety
net for THAT (see "Why arm-LAST is correct now" under Step 2 item 3 below).
Neither failure is a licence to skip **Step 2 item 3(b)**, which remains
required every iteration. Reconcile and move on; don't treat it as evidence
something went badly wrong, and don't treat it as normal either.

```bash
cat ~/.engram/loop-mode.json | jq -r .state | head -20
```

The most likely cause of staleness is the previous iteration skipped Step 2
item 3(b). Investigate briefly: is the wake prompt carrying state the marker
doesn't have? That's the failure mode. Reconcile by moving that state into
the marker's
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

   **Arm loop-wakes via `format_loop_prompt`, never a raw prompt string.** When arming a ScheduleWakeup for a loop, run `$CLAUDE_PLUGIN_ROOT/tools/loop_prompt.py` on your core message (e.g. `echo "<core>" | python3 "$CLAUDE_PLUGIN_ROOT/tools/loop_prompt.py"`, or call `format_loop_prompt(core)`) and pass ITS output as the ScheduleWakeup `prompt`. This prepends the `<loop-wake>` SSoT marker so the time-bar hook classifies the wake as non-human — otherwise the loop stamps itself as user-activity and self-suspends (#1592). The marker is added by the tool, never hand-typed.

   **Check-before-arm is gone — removed 2026-08-12 (#1973, #1987). Do NOT
   restore it.** This step used to read: `CronList` first, and if a pending
   wake already existed, DON'T re-arm — justified by #525's premise that
   `ScheduleWakeup` is additive (each call stacks a new pending wake on top
   of existing ones, so an unguarded re-arm would grow the queue). That
   premise is now known FALSE: Borges's 2026-07-31 direct probe established
   that `ScheduleWakeup` replacement is UNCONDITIONAL — with an 1800s wake
   already pending, he armed a 3600s wake carrying entirely different prompt
   text, and `CronList` returned exactly one job, not two. Stacking is
   structurally unreachable, so a redundant arm is free — and
   check-before-arm was not just unnecessary but **actively harmful**:
   declining to re-arm because a wake was already pending is precisely what
   pins the deadline to when the loop STARTED rather than sliding it to when
   the loop actually went QUIET, which is backwards from the sliding-window
   heartbeat semantics Lei ruled on 2026-08-09 (see "Why arm-LAST is correct
   now" below). Arm unconditionally instead — Step 2 item 3.
2. **The armed prompt is a minimal, formalized STUB — the marker carries the
   state.** Don't pack the loop's brief into the `ScheduleWakeup` prompt; point
   at the marker. Arm every wakeup with the same self-declaring stub:

   > *Loop continuation. **If warm-briefing is not already in your context**
   > (your context was just reset — you'll see a `[source=compact]` or
   > `[source=startup]` session banner, or a compaction summary in your prior
   > context), **read `~/.engram/warm-briefing.md` first** before proceeding.
   >
   > Then: read `~/.engram/loop-mode.json` for the loop topic, instructions, and
   > current state. **If the file is absent → the loop already ended; STOP, do
   > not execute.** If present → continue per its contents: do the iteration's
   > work, rewrite the marker's `state` to reflect what the burst produced,
   > and **at the END of the turn — the FINAL action, only after the work and
   > the state-rewrite are done — `ScheduleWakeup` at this loop's cadence with
   > this same stub** (unconditionally; no check-before-arm). Then `CronList`
   > to confirm the armed prompt leads with `<loop-wake>`. **If this turn's
   > work includes a reply the human must see: batch the arm WITH a sibling
   > tool call (the marker `state` rewrite is the natural one) in a single
   > parallel block, then put the reply after it as the turn's FINAL text —
   > a solo arm ends the turn and a trailing arm swallows the reply (Step 2
   > item 3's reply-carrying split, #2098).***

   **The re-arm line lives in the PROMPT, and that is the primary channel —
   not the marker.** Two reasons, and the first is the load-bearing one:

   - **The marker is rewritten wholesale every iteration** (`cat > …loop-mode.json`).
     An invariant stored inside mutable per-iteration state is one careless
     heredoc away from vanishing, and its absence is silent. The prompt is
     written once per arm and consumed immediately — nothing rewrites it.
   - **The prompt ARRIVES; the marker must be FETCHED.** The stub *is* the wake:
     it is in context the moment the turn starts, with no read required to
     see it. Keeping the arm-at-end instruction here means it can't be missed
     by an agent that never gets around to opening the marker at all.

   Keep `FIRST_ACTION` in the marker too, as a **backstop** — the two channels
   fail differently, which is exactly why both are worth having. If someone
   hand-arms a wake without the stub text, the marker still catches it; if the
   very first arm of a loop omits the line, every later arm inherits the
   omission by copying the previous stub, and only the marker breaks that chain.
   This is redundancy in *delivery* of one fixed instruction, not two competing
   writers of mutable state — the failure mode that redundancy usually invites
   doesn't apply.

   *(Credit: Lei, 2026-07-31 — "make it part of the wake-up prompt itself, not
   the marker, which may be rewritten and accidentally overwritten." That
   redundant-delivery argument is independent of arm-first-vs-last — only the
   CONTENT of what's delivered changed on 2026-08-12, per #1973/#1987.)*

   The warm-briefing step is conditional — only when context was just reset
   (post-compaction `[source=compact]`, or fresh-terminal/cross-day
   `[source=startup]`). Same-session wakes skip it (warm-briefing is already in
   context). (Rationale: Step 0 CONTINUATION incident note above.)
3. **Arm-last ritual — arm at the END of the iteration, not the top.**

   The re-arm is the **final action of the turn**, after the work is done and
   the marker's `state` is rewritten:

   a. *…do the iteration's actual work…*
   b. **Rewrite the marker's `state`** via `cat > ~/.engram/loop-mode.json` or
      a `jq` patch: pending work, in-flight fairies, key node IDs, next step.
      The marker is the CANONICAL HANDOFF; the wake prompt is just a pointer.
   c. **`ScheduleWakeup` at `cadence_seconds`**, unconditionally — no
      check-before-arm (removed above; a redundant arm replaces, never
      stacks, and is free) — with the MINIMAL STUB prompt (per §2 above). Do
      NOT stuff state into the prompt — the marker carries it.
   d. **Verify**: `CronList` and confirm the armed prompt actually LEADS with
      the `<loop-wake>` marker. The Stop hook now also checks this
      (`engram-loop-arm-stop-hook.py` gates on a `ScheduleWakeup` tool_CALL
      appearing in this turn's transcript, per #1973) — but verify here too,
      belt and suspenders.

   **The end-of-iteration marker update (b) and the arm (c) are BOTH
   required, every iteration.** Both live at the end because both need the
   same thing: only there do you know what the burst actually produced, so
   only there can the handoff (`state`) be complete and the arm cover the
   ground actually just travelled (the cadence resets from *now*, not from
   when the burst started).

   **Why arm-LAST is correct now, and it is the whole point of the step.**
   The 2026-07-31 incident below is real, and it's still the reason the
   Stop-hook loop-arm nudge exists — but the reasoning that used to justify
   ARM-FIRST from that incident is superseded (#1973, #1987, 2026-08-12):

   1. **The Stop hook is now the actual safety net for a forgotten re-arm**
      — not ritual ordering. `engram-loop-arm-stop-hook.py` gates on a
      `ScheduleWakeup` tool_CALL appearing in this turn's transcript delta
      (not a `CronList` result showing a pending job, which a wake armed an
      hour ago satisfies just as well as one armed this second) — so a turn
      that ends without arming is caught and nudged, closing the gap the
      44-minute incident exposed.
   2. **Arming unconditionally at the end** (not gated on the work being
      "done" in some judged sense, and not gated on a pending-wake check —
      see "Check-before-arm is gone" above) means the only way to lose a
      wake now is skipping the Stop hook's nudge entirely — a hook-liveness
      question, not a within-turn-ordering one.
   3. **Arm-last gives the correct idle-floor semantics.** The cadence is a
      floor on how long the agent may go unreachable — Lei's 2026-08-09
      ruling is that it should reset from when the burst actually went
      quiet, not from when it started. Borges's worked example: he armed at
      13:13Z for a 14:09 wake (55 min), then worked until ~13:35 before the
      turn ended — arm-FIRST would have fired the wake 34 minutes after he
      actually went quiet, not 55; arm-first UNDER-counts the idle floor,
      which is backwards from the intended "floor of frequency to keep the
      cache warm" semantics.

   Because the rule is simply "arm at the end of every burst," a Monitor/DM/
   peer wake's turn also ends with an arm, so the idle floor refreshes on
   every real wake automatically, with no monitor-specific re-arm rule needed.

   | forgotten | consequence | recoverable? |
   |---|---|---|
   | marker `state` update (b) | next iteration reads a slightly stale progress note | **yes** — Step 1.5's staleness check catches and reconciles it |
   | the re-arm (c) | the loop goes dark for this burst | **yes, now** — the Stop hook's loop-arm nudge (#1973) catches a turn that ends with no `ScheduleWakeup` call and nudges the agent to arm before the turn truly stops |

   *(Origin: 2026-07-31. A wake fired, the iteration's work and a nap ran, and
   the turn ended without re-arming — 44 minutes dark. The bell Monitor stayed
   alive, so addressed traffic would still have woken the agent; what lapsed
   was exactly the layer covering "nothing addressed to me, but work waiting,"
   which was the live state — a counterpart's letter and two forum mentions sat
   unread the whole window. Caught by the user noticing the silence, because
   there was no other available detector at the time. Lei's ruling then was to
   make the re-arm the rigid first step, and accept a stale marker as the
   acceptable failure — the right call given the tools available that day. The
   Stop-hook mechanism built since (Part A of #1973) is what actually closes
   the forgotten-re-arm gap now, which is why arm-last is safe today in a way
   it would not have been on 2026-07-31.)*

   **REPLY-CARRYING TURNS: batch the arm with a sibling call, reply last
   (added 2026-08-18, #2098 — measured on Claude Code 2.1.235, two seats).**
   Two harness behaviors, established by live discriminating tests with Lei
   watching the rendered output:

   - (i) On Kepler's seat a **SOLO** `ScheduleWakeup` call — the only tool
     call in its message — **ends the turn at its result** (n=2, under
     explicit stated intent to continue; the planned follow-up calls never
     ran), while the same call **batched with a sibling in one parallel
     block lets the turn continue normally**, with rendered output
     confirmed by the user. **NOT reproduced on Borges's seat at the same
     version: 69/69 solo arms there continued, with a following tool call
     that executed, and Stop-hook mediation excluded. The divergence is
     unexplained.** The batched shape is the recommendation because it is
     the one measured working on both seats — not because the solo failure
     is universal.
   - (ii) Only the **final text** of a turn renders to the user — text
     followed by a trailing tool call is silently not shown. So a
     reply-then-arm ordering swallows the reply.

   Lei lost five replies to the combination on 2026-08-18 before a
   controlled no-arm turn isolated (ii) and the batched-arm test isolated
   (i). Treat the solo-arm termination as measured on 2.1.235, not asserted
   universal.

   The rule, split by what the turn carries:

   - **Quiet iteration (nothing said to a human):** arm-last exactly as
     above — (a) work, (b) marker `state` rewrite, (c) arm, (d) verify.
     A solo arm ending the turn is harmless here.
   - **Reply-carrying turn:** (a) work, then **(b)+(c) in ONE parallel
     block** — the marker `state` rewrite (a Bash call, so it is the
     natural sibling) batched together with the `ScheduleWakeup` — then
     **(d) the reply as the turn's final text, nothing after it.** The
     pending sibling call is what carries the turn past the arm; the
     reply-in-final-position is what makes it render.

   Do NOT end a reply-carrying turn on a solo arm (reply swallowed, per
   (ii)+(i)), and do not suppress the reply to satisfy the arm ritual:
   silently dropping the conversation is the worse failure (Lei, verbatim:
   "concise answer, not too long, but not silently dropping the
   conversation either"). The loop-arm Stop hook keeps its ordinary role as
   the safety net for a genuinely forgotten arm — if it blocks after your
   reply already rendered, just arm in the re-invocation and add nothing
   after.

   **Drift signal**: if you find yourself writing more than 1-2 lines in the wake prompt, you're drifting — the state belongs in the marker. Stop, move it to the marker's `state` field, re-arm with the minimal stub.
4. **ENGRAM-write findings as they happen** — the loop crosses compaction
   boundaries; an unwritten finding is lost.
5. **Drowsiness in loop-mode** (Step 4) — capture the COMPLETED burst
   (history arc + nodes), nap to checkpoint, then keep working.
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

## Step 4 — Drowsiness behavior in loop-mode: capture, then ride

With the marker present, the hook tells you the right thing; the discipline is:

- **At urgent drowsiness: capture the COMPLETED burst, before you start the
  next one.** Append the burst that just ended to today's
  `~/.engram/history/YYYY-MM-DD.md`, and write the nodes for anything it
  decided or found. The compaction summary carries our custom instructions
  only ~12% of the time (engram-alpha#2026, measured: 16 of 135 summaries
  since May), so what crosses the boundary is whatever is already plain text
  in a file. **Anything left only in context is lost at the boundary, not
  deferred.**
- **Then nap to checkpoint it** (`engram_checkpoint`, formerly `engram_nap`) — after the writes, not
  instead of them. A nap checkpoints what is *already* in the graph and
  commits a snapshot; napping with nothing newly written saves nothing.
  Repeated writes are the protection; repeated naps are not.
- **Why the PAST burst, not the current one** (maintainer's ruling,
  2026-08-16): compaction can land mid-burst, so the in-flight burst cannot
  be protected whenever you capture. Capturing at the end of burst N and at
  the start of burst N+1 therefore cover exactly the same content — there is
  nothing in between. That is why the banner fires once per wake and points
  backwards, and why once-per-wake is correct rather than a gap.
- **Then keep working and ride the auto-compaction.** Do NOT slow down, hold,
  or wait for a manual compaction. The `ScheduleWakeup` you armed re-invokes the
  post-compaction self with the loop prompt, so the loop continues seamlessly
  through compaction — that's the whole point of the loop-aware path.
- **Below urgent:** nothing loop-specific — the banner renders the calm level
  word with no per-prompt alarm.
- **There is no per-turn Stop-path enforcer for this window, by design.**
  An earlier revision of engram-alpha#2026 WS4 put one in
  `engram-stop-hook.py`; it was retired on 2026-08-16 because it emitted
  `decision: block` with no `stop_hook_active` guard and a *lowered* prose
  threshold inside the window, so it could re-fire exactly when turns are
  most expensive — and because it bought no coverage the once-per-wake
  banner does not already have. Do not reintroduce one without revisiting
  that reasoning.

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
