# ENGRAM Performance Diagnostic — Telemetry Analysis (Agent: borges)

Data sources: `/home/agent-borges/.engram/knowledge.db` (`tool_timing`, 28,675 rows,
2026-04-16 → 2026-07-06) and `/home/agent-borges/.engram/logs/index.db` (`events`
table, 37,040 rows, 2026-05-17 → 2026-07-06). `index.db` had to be copied to scratch
before opening (the live handle refused read-only open — likely an in-progress
writer holding the file open; no `-wal`/`-shm` sidecars existed at copy time).
`tool_timing` schema and `events` schema both matched the columns named in the task
brief, with one addition: `events` also carries `engram.tool.engram_call` rows (a
third event type alongside `engram.hook.fire` / `engram.surface.fire`) — not used
here since `tool_timing` is the authoritative source for tool latency.

**Headline finding, upfront:** the "engram_inspect avg 6967ms" figure is real but
badly misleading as a *current* number. It is an all-time average dominated by a
10-day incident window (2026-05-17 → 2026-05-27, agent turns 50–60) during which a
handful of burst events drove tens of thousands of calls into 15–25+ second
latencies. **Since 2026-05-27, engram_inspect averages 285ms** (p50 48ms), a 24×
improvement over the all-time figure. See §G below — this reframes nearly
everything else in this report.

---

## A. Per-tool latency distribution (all-time, all 45 tools)

Sorted by total wall-clock consumed. Full table (45 rows) in
`f1-telemetry-data.json` → `A_per_tool_latency`. Top 12 by total time:

| tool | n | p50 ms | p90 ms | p99 ms | max ms | total s | error rate |
|---|---|---|---|---|---|---|---|
| engram_inspect | 9,672 | 86 | 18,965 | 19,340 | 23,623 | 67,393.5 | 0.0% |
| engram_query | 1,757 | 677 | 21,958 | 24,909 | 26,379 | 15,875.2 | 0.68% |
| engram_surface | 8,763 | 267 | 3,265 | 5,285 | 84,720 | 6,949.4 | 3.65% |
| engram_list | 558 | 82 | 20,604 | 38,108 | 38,527 | 2,539.4 | 3.41% |
| engram_checkpoint | 302 | 6,816 | 8,048 | 16,536 | 16,834 | 2,087.3 | 0.0% |
| engram_nap | 180 | 8,644 | 11,114 | 18,264 | 310,712 | 1,802.3 | 0.0% |
| engram_add_observation | 2,435 | 142 | 885 | 3,669 | 370,051 | 1,676.8 | 0.08% |
| engram_advance_turn | 61 | 6,298 | 11,496 | 18,542 | 18,595 | 493.8 | 0.0% |
| engram_diagnose | 597 | 347 | 1,459 | 2,267 | 88,629 | 489.3 | 0.0% |
| engram_query_pattern | 188 | 108 | 3,682 | 10,871 | 18,529 | 205.3 | 1.06% |
| engram_get_subgraph | 38 | 28 | 19,387 | 24,039 | 25,654 | 157.2 | 0.0% |
| engram_stats | 501 | 72 | 819 | 5,320 | 10,863 | 151.6 | 1.20% |

Everything past these is <120s total over the whole 81-day window (see JSON).
Note the p50/p90 gap for `engram_inspect`, `engram_query`, `engram_list`,
`engram_get_subgraph`: p50 is fast (tens–hundreds of ms) but p90 jumps to
15,000–20,000+ ms — a strong **bimodal** signal, investigated in §G.

---

## B. Trend vs graph growth (top 5 by total time, monthly buckets)

| tool | month | n | p50 ms | p90 ms | cumulative nodes at month-end |
|---|---|---|---|---|---|
| engram_inspect | 2026-04 | 354 | 18 | 52 | 2,925 |
| engram_inspect | 2026-05 | 7,580 | 6,045 | 19,013 | 4,309 |
| engram_inspect | 2026-06 | 1,317 | 46 | 94 | 5,629 |
| engram_inspect | 2026-07 | 421 | 96 | 438 | 5,797 |
| engram_query | 2026-04 | 52 | 34 | 292 | 2,925 |
| engram_query | 2026-05 | 1,338 | 16,129 | 22,356 | 4,309 |
| engram_query | 2026-06 | 262 | 141 | 3,572 | 5,629 |
| engram_query | 2026-07 | 105 | 223 | 449 | 5,797 |
| engram_surface | 2026-04 | 1,811 | 179 | 4,213 | 2,925 |
| engram_surface | 2026-05 | 4,410 | 246 | 3,266 | 4,309 |
| engram_surface | 2026-06 | 2,148 | 288 | 1,052 | 5,629 |
| engram_surface | 2026-07 | 394 | 327 | 1,175 | 5,797 |
| engram_list | 2026-04 | 14 | 17 | 20 | 2,925 |
| engram_list | 2026-05 | 373 | 63 | 36,846 | 4,309 |
| engram_list | 2026-06 | 87 | 97 | 182 | 5,629 |
| engram_list | 2026-07 | 84 | 217 | 639 | 5,797 |
| engram_checkpoint | 2026-04 | 268 | 6,668 | 7,978 | 2,925 |
| engram_checkpoint | 2026-05 | 34 | 7,363 | 8,383 | 4,309 |

**Answer to "are reads getting slower as the graph grows, and at what slope?": no
measurable growth-driven slope.** April → July, node count nearly doubled
(2,925 → 5,797) while p50/p90 for `engram_inspect`, `engram_query`, and
`engram_list` all *dropped* from June onward relative to May. May is the outlier
month entirely because of the incident window in §G, not because of graph size —
April (smaller graph) and June/July (larger graph) both show low, comparable
latency (p50 tens–hundreds of ms). `engram_surface` shows a mild, plausible
growth-correlated trend (p50 179→327ms April→July) but it's a ~1.8× drift over
2,872 added nodes, not the dominant story.

`engram_checkpoint` has no June/July rows at all — it stops appearing after May.
Either the tool was retired/renamed or its call path changed; worth a follow-up
question to the maintainer rather than treating it as "fixed," since we can't
distinguish "no longer used" from "renamed and now uncounted" from the timing
table alone.

---

## C. Tail forensics

**25 slowest calls overall** (full list in JSON → `C_slowest_25_calls`):

| tool | timestamp | duration_ms | turn |
|---|---|---|---|
| engram_add_observation | 2026-05-23T17:40:45Z | 370,051 | 57 |
| engram_nap | 2026-05-20T02:23:58Z | 310,712 | 53 |
| engram_add_observation | 2026-05-07T13:06:36Z | 151,939 | 41 |
| engram_diagnose | 2026-05-20T02:23:20Z | 88,629 | 53 |
| engram_add_observation | 2026-05-14T22:27:29Z | 86,980 | 48 |
| engram_surface | 2026-05-14T22:21:40Z | 84,720 | 48 |
| engram_surface | 2026-05-14T22:26:06Z | 84,508 | 48 |
| engram_surface | 2026-05-14T22:31:44Z | 84,179 | 48 |
| engram_surface | 2026-06-13T13:52:29Z | 83,102 | 78 |
| engram_add_observation | 2026-04-18T12:17:22Z | 48,988 | 20 |
| engram_add_observation | 2026-05-20T12:26:12Z | 46,112 | 54 |
| engram_surface | 2026-05-01T21:53:00Z | 38,701 | 35 |
| engram_list ×13 | 2026-05-18 14:20–14:37 | 37,757–38,527 | 52 |

**Clustering**: Two distinct patterns.
1. **Same-tool, same-turn micro-bursts**: the 13 slowest `engram_list` calls all
   fall within an 18-minute window on 2026-05-18 (turn 52), part of the same
   incident described in §G — a shared external/systemic cause, not per-call
   variance.
2. **Isolated single-call spikes independent of the May incident**: three
   `engram_surface` calls cluster within 10 minutes on 2026-05-14 (turn 48,
   84.2–84.7s each), and one more recurs in isolation on 2026-06-13 (83.1s, turn
   78) — well outside the incident window, meaning `engram_surface` has an
   ongoing (not fully resolved) tail-latency risk, plausibly related to a daemon
   fallback stall (see `fallback_to_fts` in §D).

**engram_add_observation and engram_nap, calls >30s:**
- `engram_add_observation`: **7 calls** — 2026-04-18 (48,988ms), 2026-05-07
  ×2 (151,939ms, 35,678ms), 2026-05-14 (86,980ms), 2026-05-20 ×2 (37,399ms,
  46,112ms), 2026-05-23 (370,051ms, the single worst call in the whole dataset).
  All 7 predate 2026-05-27; none recur after the incident window closed.
- `engram_nap`: **1 call** — 2026-05-20T02:23:58Z, 310,712ms (~5.2 minutes),
  inside the incident burst window (turn 53).

---

## D. Per-hook profile (`index.db`)

| hook_name | fires (all-time) | fires (7d) | avg ms (all-time) | p95 ms (all-time) | errors |
|---|---|---|---|---|---|
| engram-write-yield-hook | 9,683 | 0 | 0.0 | 0.0 | 0 |
| engram-deference-detector-stop | 3,162 | 0 | 0.2 | 0.0 | 0 |
| engram-deference-detector-prompt | 3,161 | 0 | 0.0 | 0.0 | 0 |
| engram-stop-hook | 2,381 | 0 | 0.0 | 0.0 | 0 |
| engram-utility-credit-mention-stop | 2,105 | 369 | 10.1 | 19.0 | 0 |
| engram-session-start-hook | 220 | 15 | 891.7 | 2,306.6 | 0 |
| engram-postcompact-hook | 44 | 0 | 0.0 | 0.0 | 0 |

**Anomaly**: "fires (7d)" = 0 for 5 of 7 hooks. Checked `max(ts)` per hook
directly — `engram-write-yield-hook` last fired 2026-06-02, `-stop-hook`
2026-06-06, `-postcompact-hook` 2026-06-13, `-deference-detector-*` 2026-06-13.
**These hooks have not logged an event in 3–5 weeks**, well before the 2026-07-06
"now." This is either (a) genuine disuse (the behaviors they guard haven't
triggered recently), or (b) the hooks were renamed/replaced and new events are
landing under different `hook_name` values not captured here, or (c) telemetry
logging broke for them silently. Cannot distinguish from this data alone —
flagging, not concluding.

`engram-utility-credit-mention-stop` and `engram-session-start-hook` are the only
two with recent (7d) activity, so they're the only ones with a live current-cost
estimate: **~10.1ms + ~891.7ms ≈ 902ms** combined avg when both fire, though
`session-start-hook` only fires once per session (220 times all-time vs. tens of
thousands of prompts), not per-prompt.

**Per-prompt hook cost**: of the named hooks, only `engram-deference-detector-prompt`
clearly matches a UserPromptSubmit-class trigger by name, and its logged duration
is ~0ms (all-time). Combined with `engram-write-yield-hook` (also apparently
~0ms, though possibly Stop-class not prompt-class — name is ambiguous from data
alone), the **measured per-prompt hook overhead is negligible (<1ms)** — but given
the staleness flag above, treat this as "negligible when last measured (early
June)," not as a live guarantee.

**`engram.surface.fire`** (n=4,374): p50 322ms, p90 1,010ms, p99 1,266ms, max
1,397ms. `fallback_to_fts` = true on 130/4,374 events (**2.97%**). This distribution
looks clean/unimodal (no long tail into seconds) — contrast with the `tool_timing`
table's `engram_surface` row, which has p99 5,285ms and a max of 84,720ms. The two
tables are measuring different things (the daemon-side surface event vs. the
MCP-tool-call wall-clock including the daemon call plus everything else in the
tool handler) — the tool-level number is the one that actually gates the agent, so
the fallback-rate figure here likely under-represents user-visible latency risk.

---

## E. Call-mix economics (top 5 by total time, 81-day observed period)

| tool | total hours observed | fraction of total tool wall-clock | if 10× faster |
|---|---|---|---|
| engram_inspect | 18.72 h | 67.1% | saves ~16.8 h / 81 days → **~68 h/quarter** |
| engram_query | 4.41 h | 15.8% | saves ~3.97 h / 81 days → **~16 h/quarter** |
| engram_surface | 1.93 h | 6.9% | saves ~1.74 h / 81 days → **~7 h/quarter** |
| engram_list | 0.71 h | 2.5% | saves ~0.64 h / 81 days → **~2.6 h/quarter** |
| engram_checkpoint | 0.58 h | 2.1% | saves ~0.52 h / 81 days → **~2.1 h/quarter** |

(Quarter ≈ 91 days; scaled linearly from the 81-day observed total, `hours ×
(0.9 × 91/81)`.) **Caveat, load-bearing**: these totals are dominated by the same
May incident window as everything else. `engram_inspect` alone accounts for
67% of *all* tool wall-clock time across 45 tools, entirely because of ~7,580 May
calls averaging 6+ seconds each; at the *post-incident* rate (285ms avg,
measured June–July), the same call volume would cost a small fraction of this.
**A "10× faster" framing is the wrong lens here** — the tool already got ~24×
faster after whatever changed around 2026-05-27; the more useful economics
question is "did that fix hold, and can it regress," not "optimize further."

---

## F. Anomalies

1. **Status errors**: 370/28,675 tool calls (1.29%) are `status=error`, concentrated
   in `engram_surface` (320 errors, 3.65% of its calls), `engram_list` (19,
   3.41%), `engram_query_pattern` (2, 1.06%), `engram_stats` (6, 1.20%),
   `engram_history` (2, 1.16%), `engram_focus_sets` (1, 2.13%),
   `engram_add_observation_batch` (1, 1.12%), `engram_query` (12, 0.68%). No
   error concentration in `engram_inspect`, `engram_add_observation`,
   `engram_checkpoint`, or `engram_nap` (0% each) despite those being the
   heaviest/slowest by duration — errors and latency are not the same failure
   axis here.
2. **Strong bimodality** in `engram_inspect`, `engram_query`, `engram_list`,
   `engram_get_subgraph` (see §A/§G): p50 in double/triple digits ms, p90 jumping
   straight to 15,000–38,000ms with almost nothing in between. This is not a
   long-tail/heavy-tail distribution shape — it is two separated clusters, which
   is the signature of a small number of discrete incident events, not
   gradually-worsening performance. Confirmed in §G: it traces to specific
   single-turn burst windows on 5 dates between 2026-05-17 and 2026-05-27,
   entirely absent afterward.
3. **`engram_checkpoint` and `engram_advance_turn` are consistently ~6–10s
   avg regardless of period** (no bimodality, no incident-window dependence) —
   these read as genuinely expensive operations by design (consolidation-class
   work), not a bug; flagging as "known-cost, not anomalous."
4. **Turn-vs-duration correlation for `engram_inspect`**: Pearson r = -0.237
   (n=9,672) — weak negative correlation, i.e. later turns trend slightly
   *faster*, consistent with the incident being concentrated in an early-turn
   window (turns 50–60) and turns since then being faster. Not a strong
   relationship on its own; the month/incident-window framing in §B/§G is more
   informative than turn number.
5. **Hook telemetry staleness** (§D): 5/7 hooks haven't logged in 3–5 weeks —
   flagged, not diagnosed.
6. **`engram_surface` has an unresolved post-incident tail risk**: unlike
   `engram_inspect`/`engram_query`/`engram_list`, its slowest calls are NOT
   confined to the May incident window — one of its 84-second-class spikes
   recurred on 2026-06-13, a month after the incident closed (§C). Worth
   watching; the other three tools' tails fully cleared.

---

## G. The incident window (root context for A, B, C, E)

Isolating calls with `duration_ms >= 15000` and grouping by `(tool, date, turn)`
shows the "slow tail" is not diffuse — it is a handful of dense, single-turn
bursts, all between 2026-05-17 and 2026-05-27 (agent turns 50–60):

| date | tool | turn | burst call count | window |
|---|---|---|---|---|
| 2026-05-18 | engram_inspect | 52 | 795 | 14:14:43–14:40:27 |
| 2026-05-18 | engram_query | 52 | 673 | (same session) |
| 2026-05-18 | engram_list | 52 | 55 | (same session) |
| 2026-05-20 | engram_inspect | 53 | 2,362 | 01:37:23–02:24:51 |
| 2026-05-21 | engram_inspect | 54 | 24 | 01:03:50–01:04:04 |
| 2026-05-23 | engram_inspect | 56 | 27 | 00:50:00–00:50:07 |
| 2026-05-24 | engram_inspect | 57 | 33 | 02:19:00–02:19:14 |

Within the 2026-05-20 burst (largest one, 2,362 `engram_inspect` calls in 47
minutes, single turn=53), durations *escalate* call-over-call — first calls land
around 15.4s, later calls in the same burst climb past 18–24s — the signature of
queueing/contention (many calls serialized behind a shared resource) rather than
each call independently taking that long.

**Not explained by graph size**: only 285 of 5,797 total nodes (4.9%) were
created during the 2026-05-18→05-27 window, so this was not a mass-import event
inflating the graph mid-burst.

**Pre/post split, all affected tools** (full detail in JSON →
`G_incident_window_pre_post_split`):

| tool | pre-05-27 avg | pre-05-27 p50 | post-05-27 avg | post-05-27 p50 | improvement (avg) |
|---|---|---|---|---|---|
| engram_inspect | 9,104ms | 7,066ms | 285ms | 48ms | **32×** |
| engram_query | 11,597ms | 16,078ms | 831ms | 166ms | **14×** |
| engram_list | 7,207ms | 61ms | 314ms | 105ms | **23×** |
| engram_surface | 902ms | 237ms | 593ms | 288ms | 1.5× (mild, not incident-driven) |
| engram_add_observation | 909ms | 134ms | 324ms | 151ms | 2.8× |
| engram_nap | 12,146ms | 8,990ms | 6,237ms | 5,612ms | 1.9× |
| engram_advance_turn | 10,231ms | 9,477ms | 6,804ms | 5,768ms | 1.5× |

**Read on this**: whatever the "headline" avg-latency figures were built from,
they are dominated by an 8-10 day window roughly two months ago, not current
steady-state behavior. `engram_surface`, `engram_nap`, and `engram_advance_turn`
retain some elevated cost post-incident (consistent with §F.3 — nap/advance_turn
look like genuinely expensive consolidation ops, and surface has its own
unresolved tail per §F.6), but `engram_inspect`/`engram_query`/`engram_list` are
now fast tools whose historical averages are a poor guide to current cost.

**Open question for the maintainer, not answered by this data**: what changed
around 2026-05-27 that ended the burst pattern (a deploy, a lock/contention fix,
a resource change)? The data shows the *before/after* clearly but not the *cause*.

---

## Files

- `/home/agents-shared/kepler/perf-2026-07-06/f1-telemetry-analysis.md` (this file)
- `/home/agents-shared/kepler/perf-2026-07-06/f1-telemetry-data.json` (all backing
  aggregates: sections A–G, keyed to match this report's section letters)
