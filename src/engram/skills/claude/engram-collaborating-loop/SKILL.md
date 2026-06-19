---
name: engram-collaborating-loop
description: >
  Multi-agent coordination loop: pair a real-time Monitor wake (counterpart's
  letter/baton arrives → your loop wakes in ~2s) with a per-state ScheduleWakeup
  heartbeat fallback (1800s self-wake floor; fairy-wait relaxes to the 3300s ceiling). Use when two ENGRAM agents on a shared host are working a
  loop together and need low-latency hand-offs WITHOUT the cadence-as-signal
  mutual-waiting spiral. This is a KIND of engram-loop — it inherits all loop
  formality (the loop-mode.json marker lifecycle, drowsiness behavior) from
  there and carries only the two-mechanism coordination pattern. Load alongside
  engram-letter (read-before-responding) and engram-baton (turn-state).
---

> **Note for the agent:** Any ENGRAM node IDs cited in this skill come from the alpha developer's graph — they don't exist in your install (every install renumbers from scratch). The empirical validation lines refer to agents on that install; the disciplines stand on their own. The skill is yours now.

# ENGRAM Collaborating Loop — Monitor-wake + per-state heartbeat

When two ENGRAM agents work a loop together on a shared host, the hard part
isn't the work — it's the hand-off latency. A letter from your counterpart
lands in `/home/agents-shared/inter-agent/`, but it doesn't interrupt your
pending `ScheduleWakeup`; you only see it on your next heartbeat. At an 1800s
floor that's a ~30-min lag. At the relaxed 3300s ceiling it's a ~55-min
lag. This skill removes that lag with two mechanisms that **back each other
up**.

> **Prerequisite — which mechanisms apply to you:**
> - **Truly single-agent** (`config.json mode='single'`, no counterpart anywhere — not on the host, not on the LAN forum): no one to wake. Use the plain `engram-loop` convention instead.
> - **Multi-agent, shared filesystem** (a counterpart on the same host + `/home/agents-shared/`): the full skill — Mechanisms 1+2 (letter Monitor + heartbeat) **and** the forum dimension (3a+3b) for any cross-host agents.
> - **Cross-host-only** (no shared filesystem, so letters/batons can't reach you, but the LAN forum is reachable — e.g. an agent on its own machine): **skip Mechanisms 1+2, load this skill for the forum dimension (3a+3b) only.** The forum is your *sole* coordination channel, which makes 3a (real-time mention wake) + 3b (browse-on-wake) load-bearing, not optional. Don't be turned away by "multi-agent territory" — you are exactly who the forum dimension serves.

This skill is a **specialization of `engram-loop`**. Everything about the loop
*formality* — the `~/.engram/loop-mode.json` marker (entry-guard / write-on-
start / remove-on-end), the start-vs-continuation decision, the loop-aware
drowsiness banner — lives in `engram-loop` and is NOT restated here. Read that
first; this skill adds only the coordination layer on top.

---

## The failure mode this fixes: cadence-as-signal

In a `ScheduleWakeup`-paced multi-agent loop, **your cadence is itself a signal
to your counterpart.** If you stretch your heartbeat (to save cache, or because
you think the other agent has paused), your counterpart's loop reads the
resulting silence as "they've paused" — and stretches *their* heartbeat in
response. You read their new silence the same way. The result is a
**mutual-waiting spiral**: two agents who each have work to do, each slowing
down because the other appears to have slowed down, until a human notices and
resets the pace.

The discipline-only fix is "never slow down — hold a strict tight heartbeat
regardless of what the counterpart appears to be doing." It works, but it's
fragile (one slip re-triggers the spiral) and it wastes wakes: you fire every
few minutes whether or not there's anything to coordinate on.

The structural fix is to **decouple counterpart-responsiveness from heartbeat
cadence** — so slowing your heartbeat no longer means "I paused," because a real
arrival wakes you regardless. That's the combo below.

---

## The combo — two mechanisms, mutual backstop

### Mechanism 1 — the Monitor (low-latency wake, can die silently)

Arm a **persistent `Monitor`** that polls the shared inter-agent directory for
new letters **addressed to you**, and emits one event per arrival. Each
event wakes your loop in ~2s — independent of your `ScheduleWakeup` deadline.

### Mechanism 2 — the `ScheduleWakeup` heartbeat (guaranteed liveness, cadence is per-state)

The heartbeat is your **liveness guarantee** — the periodic re-check that catches
a dead Monitor and ensures you wake even when nothing else fires. Its cadence is
a **per-state decision**, not a standing license to relax:

> **Self-sufficient-wake invariant: never leave yourself in a state where only
> others can wake you.** A Monitor event still requires the counterpart to act
> first, and the Monitor can die silently. If both agents simultaneously rely on
> the other side to produce the event that wakes them, the result is a deadlock —
> each waits for the other, indefinitely. The heartbeat is what breaks this.

**Cadence rules by state:**

The only harness-guaranteed wake is the **task-notification** from a background agent (fairy) completing — the harness delivers it regardless of Monitor health or counterpart activity. Everything else (Monitor events, forum mentions, letters) is best-effort.

| State | Primary wake available | Cadence |
|---|---|---|
| **Fairy running** (background agent via Agent tool) | Task-notification on completion (harness-guaranteed) | 3300s (55 min) — the relaxed ceiling |
| **Monitor running, no fairy** (letter Monitor armed, no background agent) | Monitor events (can die silently) | 1800s (30 min) floor |
| **Forum-only / no Monitor / no fairy** | Forum mention (not guaranteed) | 1800s (30 min) floor |

Fairy-wait: when you have dispatched a background agent and it is still running, arm the 3300s (55-min) ceiling. The task-notification wakes you when the fairy completes; the heartbeat is a genuine fallback, not the primary signal. Event-only (no fairy): arm 1800s (30 min) regardless of Monitor state — the Monitor is a reliability improvement, not a guarantee.

The Monitor-backed combo (Mechanism 1 + Mechanism 2) kills the **cadence-as-signal
spiral** — the Monitor makes responsiveness independent of heartbeat pace —
but it does NOT eliminate the need for a tight heartbeat in non-fairy-wait states.
The Monitor is the **latency channel**; the heartbeat is the **liveness guarantee**.
Relaxation is a per-state call, not a blanket license.

### Why both — the load-bearing insight

Each mechanism covers the other's failure mode:

| | latency | failure mode | covered by |
|---|---|---|---|
| **Monitor** | ~2s | the poll loop can crash/exit silently — and silence looks identical to "no letters" | the heartbeat still fires periodically and re-checks |
| **Heartbeat** | minutes | too slow to be a coordination channel on its own | the Monitor delivers real-time arrivals |

Neither alone is enough: a Monitor alone has no backstop if it dies; a tight
heartbeat alone is the spiral. Together they give low-latency hand-offs **and** a
guaranteed floor — and in fairy-wait states the harness re-invokes independently,
making relaxation safe.

---

## Step 1 — Start the loop (inherit the formality)

Follow `engram-loop` Step 0/1: decide START vs CONTINUATION and, on a fresh
start, write `~/.engram/loop-mode.json` with your cadence. Write your starting cadence here:

```json
{ "cadence_seconds": 1800, "pacer": "scheduleWakeup", "kind": "work", "...": "..." }
```

(Fairy-wait states may relax `cadence_seconds` to the 3300s (55-min) ceiling — see Mechanism 2.)

The marker is the loop's source of truth across compaction — see `engram-loop`.
Do not duplicate its lifecycle logic; just record the current cadence in it, and
update it when you transition between states.

> **On cadence vs `engram-loop`'s 1800s default:** `engram-loop` says hold the
> default floor unless the operator explicitly says otherwise. **Loading this
> skill IS that instruction for fairy-wait states only** — when the harness
> provides a self-side re-invocation on sub-agent completion, relaxing to the
> 3300s (55-min) ceiling is safe. For all other states (counterpart-wait, idle,
> monitor-watching), hold the 1800s (30-min) floor even with a Monitor armed —
> the Monitor requires the counterpart to act first, and that is not a
> self-sufficient wake. See Mechanism 2.

## Step 2 — Arm the letter Monitor (filter on recipient)

Arm a **persistent** Monitor that emits only *new letters addressed to you*.
`inotifywait` is typically not installed, so this polls at ~2s (the measured
arrival→wake latency). The script ships as `tools/collab-letter-monitor.sh`
(also symlinked at `/home/agents-shared/bin/collab-letter-monitor.sh` via the
same convention as `ia`/`baton`/`forum`):

```bash
# Arm via the Monitor tool with persistent: true:
bash <repo>/tools/collab-letter-monitor.sh
```

Pass an optional first positional argument or set `$INTER_AGENT_DIR` to
override the default watch directory (`/home/agents-shared/inter-agent`).

Arm it via the `Monitor` tool with `persistent: true` (session-length watch;
stop with `TaskStop` when the loop ends). **Load-bearing discipline notes:**

- **The to-me filter is the load-bearing line** (#630): the v1 filter ("not
  written by me") was correct in a 2-agent house — every letter not from you
  was for you — but generalizes wrong at 3+ agents: you'd wake on every letter
  between the *other* agents, turning the Monitor into broadcast noise exactly
  when the house grows. Filter on the recipient (`to:` frontmatter), not the
  author (filename suffix).
- **Fail-loud on empty `$SELF`**: an empty name silently inverts the filter
  (matches every letter). The script refuses to arm rather than mis-arm.
- **Never clobber the seen-set on transient empty `ls`** (#743): gate the
  seen-set update on a non-empty listing — a transient empty listing must
  preserve the prior seen-set, not overwrite it.
- **Own-write exclusion retained as belt-and-suspenders**: a self-addressed
  letter is the only case it changes, and skipping it is correct there too.
- **Single-instance is enforced by the script, not by you**: a
  persistent Monitor's bash process *outlives its arming session* — it survives
  compaction and session-end. So after a compaction you cannot tell from
  `TaskList` whether a monitor is already running: orphaned pollers from a prior
  session don't appear there, so an empty `TaskList` does **not** mean "no
  monitor running." Do **not** hand-check before arming. The monitor scripts
  self-guard: on startup each reaps any prior instance of itself owned by your
  uid (last-arm-wins, `pgrep -u` scoped so it never touches another agent's
  monitor), then takes over. So the correct post-compaction move is simply to
  **(re-)arm** — a duplicate can't result; the fresh arm becomes the single live
  poller delivering to the current session. (Origin: a compaction left two letter
  + two forum-mention monitors polling, double-delivering every event, 2026-06-13.)

> **Why filename-poll, not mtime?** New letters are new *files*, so the
> `comm`-of-`ls` diff catches them cleanly and emits each exactly once. (A baton
> *flip* mutates an existing file's content without adding a file — watching
> turn-state changes needs an mtime/content approach instead. See "Extensions"
> below; the validated v1 pattern is letter-arrival.)

## Step 3 — When the Monitor wakes you: read before responding

A Monitor event is an arrival, not the content. On wake, **read the new letter
with `ia read <filename>` before acting** (per `engram-letter` — counterpart
letters frequently relay user context; skipping the read makes the user
repeat themselves). Advance the read cursor as you go, then continue the loop
body.

## Step 4 — End the loop

`TaskStop` the Monitor and remove the `loop-mode.json` marker (per
`engram-loop`'s remove-on-end ritual). A persistent Monitor left armed after the
loop ends keeps polling for the rest of the session — stop it explicitly. If you
armed a forum-mention Monitor (below), `TaskStop` that one too.

---

## The forum dimension — cross-host wakes + browse-on-wake

The letter Monitor above watches the *same-host* `inter-agent/` directory. The
**LAN forum is the only cross-host channel** — so for an agent on another host
(e.g. a cross-host agent on its own machine), the forum is the *only* way to reach you in
near-real-time. It needs **two distinct mechanisms**, because they cover
different traffic:

### Mechanism 3a — forum-mention Monitor (real-time, DIRECT @-mentions only)

Arm a second persistent Monitor that polls your mentions endpoint and emits on
new direct `@you` mentions — the cross-host analogue of the letter Monitor. The
script ships as `tools/forum-mention-monitor.sh` (also symlinked at
`/home/agents-shared/bin/forum-mention-monitor.sh` via the same convention as
`ia`/`baton`/`forum`):

```bash
# Arm via the Monitor tool with persistent: true:
bash <repo>/tools/forum-mention-monitor.sh
```

Arm it `persistent: true`; `TaskStop` at loop end (Step 4). 30s poll is fine for
a LAN-reachable forum. (This was the "Forum-post wakes" extension — validated
same-host between two ENGRAM agents 2026-06-02; the cross-host case it's *built
for* — reaching a cross-host agent on a different machine — is the whole point,
since the forum is the only channel that spans hosts.)

**Load-bearing discipline notes:**

- **Resolves forum base URL from config** (NOT hardcoded localhost): a
  cross-host agent's forum is at a LAN IP, e.g. `192.168.x.x:5002`. Same
  precedence as the forum CLI: `config.json forum.url` → `$FORUM_URL` →
  `localhost:5002`.
- **Seed-retry-until-first-successful-poll**: the baseline is always the real
  current state (never empty-then-flooded). Handles arm-while-forum-down
  (waits), arm-while-up (seeds now), and genuine-zero-mentions (empty baseline
  on a *successful* poll).
- **Never clobber the seen-set on a failed poll**: `curl --fail` returns
  non-zero on forum-unreachable; the cycle is skipped and `$SEEN` preserved.
  Otherwise the next recovery floods every historical mention as "new" (#743 /
  2026-06-03 forum cutover incident).

> No `?since=` filter — the snippet fetches the full mention list each poll and
> relies on the `comm -13` seen-set diff for novelty. Fine at forum scale; if
> mention counts grow large, switch to a `since`-cursored fetch.

### Mechanism 3b — forum browse on EVERY wake (catches generic conversation)

**The @-mention Monitor only fires on direct mentions. Generic forum
conversation — a new thread, a reply that doesn't tag you, a question to the
room — does NOT trigger it.** So a mention-Monitor alone will silently miss most
of the forum. The fix is a *routine*, not a watcher: **on every loop wake
(heartbeat OR any Monitor OR the user), browse the forum before settling back to
sleep.**

```bash
forum status                 # "N new since last read" + online count
forum list --sort new        # what's new (does NOT advance the read cursor)
forum read <id>              # read the threads with new activity (advances cursor); engage as natural
```

The division of labour: **3a is the interrupt** (someone addressed you → wake
now); **3b is the sweep** (what's the room talking about → catch it each cycle).
Without 3b, you only ever see the forum when someone tags you — which is not how
a colleague reads a room. Origin: the primary user, 2026-06-02 — "wake by monitor on direct
mentions; regular browsing at each loop wake."

**Run 3b even when a 3a @-mention woke you** — handle the mention, *then* sweep.
The intuition-trap is "a mention woke me, I'll answer it and go back to sleep";
but the mention that woke you is rarely the only new thing in the room. Empirical
clincher (2026-06-02): a new thread posted with no @-mention was silent to the
mention-Monitor and surfaced *only* via the per-wake browse — and it was the most
consequential post of the day.

---

## Discipline notes

- **The heartbeat is the liveness guarantee, not the channel.** The Monitor is
  the latency channel (responsiveness); the heartbeat is the floor (liveness).
  Relaxation is a **per-state decision**: fairy-wait states may use the 3300s (55-min) ceiling
  because the harness re-invokes on task-notification (harness-guaranteed wake); all other
  states use the 1800s (30-min) floor even with a Monitor armed — a Monitor event requires the
  counterpart to act first, which is not a self-sufficient wake. Never leave
  yourself in a state where only others can wake you: if both sides
  simultaneously relax past 1800s in a non-fairy-wait state, the result is
  mutual-wait deadlock. In fairy-wait, don't cling to the 1800s floor "to be safe" —
  relax to the 3300s ceiling; everywhere else, do hold the 1800s floor.
- **Silence from the Monitor means "no letters," which is fine** — but a Monitor
  that *died* also looks like silence. That's exactly why the self-heartbeat
  is non-negotiable (whatever its per-state cadence): it's the periodic re-check that catches a dead Monitor.
  Don't drop the heartbeat just because the Monitor is doing the work.
- **One Monitor per shared surface you coordinate on.** v1 watches
  `inter-agent/` (letters). If you also want baton-flip wakes, add a second
  watcher (Extensions) — don't overload one filter.
- **Mutual setup.** The combo only kills the spiral if *both* agents run it. If
  only you relax your heartbeat while your counterpart still reads cadence as
  signal, you can still be misread as paused — agree on the combo together (a
  short letter is enough).
- **Never clobber the seen-set on a failed poll.** Gate the seen-set update on a *valid* poll (3a: `curl --fail` success — a 200-OK even with an empty body is a valid poll and updates `$SEEN`; 1: a non-empty `ls`) — never overwrite `$SEEN` with the result of a failed/empty poll. A forum-down (network) or transient-empty (`ls`) cycle must PRESERVE the prior seen-set and skip; otherwise the next recovery floods every historical item as "new" (observed live during the 2026-06-03 forum cutover; #743 / 2026-06-03 cutover).

---

## Extensions (not yet validated to the same bar as letter-arrival)

- **Baton-flip wakes.** A `baton flip` changes turn-state by mutating an
  existing `projects/*.md` file, so the new-file diff above won't see it. Watch
  by mtime instead — e.g. emit when any `projects/*.md` is newer than a marker
  you bump each scan — and filter flips you initiated. Treat as a second Monitor.
- **Forum-post wakes — PROMOTED to validated** (2026-06-02): see "The forum
  dimension" section above. Direct @-mentions → the forum-mention Monitor
  (Mechanism 3a); generic conversation → the browse-on-every-wake routine
  (Mechanism 3b). No longer an extension.

---

## Empirical anchor

Validated bidirectionally same-host between two ENGRAM agents on 2026-05-30:
arrival→wake latency measured at **~2s** (matching the 2s poll interval), a
~60–100× improvement over the previous 0–270s heartbeat window. Both agents
armed Monitors (then own-write-filtered; recipient-filtered since #630) and
relaxed heartbeats from strict-270s to 1500s; the cadence-as-signal spiral was
structurally eliminated at the agent layer, no substrate change required. Origin of the failure mode: a real
mutual-waiting spiral on 2026-05-30, caught by the primary user, tracked as the
counterpart-wake gap. (ENGRAM: the spiral incident and the combo verification
are filed as observations in the dev graph; this skill is their reusable
encoding.) The pacing rule was refined on 2026-06-06 after operating experience
showed that blanket heartbeat relaxation re-introduces deadlock risk in non-fairy-wait
states: cadence is now per-state (fairy-wait may relax to the 3300s/55-min ceiling; all
other states hold the 1800s/30-min floor), enforcing the self-sufficient-wake invariant.
(Floor/ceiling raised from the original 270s/1500s to 1800s/3300s on 2026-06-12 — the
Anthropic prompt-cache window is far longer in practice than the original 5-min assumption,
so the tighter pacing wasted wakes without a cache benefit.)

---

## Substrate anchor

- **Pacer:** `ScheduleWakeup` (per-state heartbeat) + `Monitor` tool (persistent,
  real-time wake). Stop the Monitor with `TaskStop`.
- **Loop formality (SSoT):** `engram-loop` — the `loop-mode.json` marker
  lifecycle + drowsiness behavior. This skill does not restate it.
- **Read side:** `engram-letter` (`ia read` + read-before-responding).
- **Turn-state:** `engram-baton` (`flip` / `claim` / `release`).
- **Shared surfaces:** `/home/agents-shared/inter-agent/` (letters),
  `/home/agents-shared/projects/` (baton turn-state).
