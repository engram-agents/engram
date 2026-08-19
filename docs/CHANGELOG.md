# Changelog

All notable changes to ENGRAM Alpha are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with an `-alpha` suffix during the alpha phase.


## [Unreleased]

## [v0.4.0] - 2026-08-19

**A reliability-and-hygiene release.** No single forcing incident drove this
wave the way the loop-silence outage drove v0.3.2; instead it's the
accumulation of a long `dev` stretch (107 commits since the `v0.3.2-rc1` cut,
2026-08-04 through 2026-08-19) across sleep-cycle cost control,
compaction-survival, test-suite hermeticity, and a lot of smaller
robustness fixes. Two items are worth calling out because they change
*process*, not just code: the dream-fairy lottery replaces the old
all-8-nightly scan with weight-aging draws, and a local pre-push test gate
was tried and then reverted in the same window — both are represented below
by their net state, not their full back-and-forth.

### Added — sleep & dream-lottery cost control

- **Weight-aging dream-fairy lottery** (#1901, #1903) — the sleep cycle's
  Phase B draws one scan category per night via a weight-aging lottery
  (`tools/dream_lottery.py`) instead of dispatching all 9 categories nightly,
  with an urgency interrupt that force-includes cascade-sensitive categories
  when the day's retraction/supersede activity crosses a threshold.
- **Append-only dream-lottery draw history + `history` subcommand**
  (#1909, #1910) — every draw's before/after weights, drawn types,
  interrupt-forced types, and cascade-marker count now persist to a JSONL
  history file (`--history`, default `~/.engram/dream-lottery-history.jsonl`),
  with a `history` subcommand to tally it (`--limit N`).
- **Draw re-entrancy guard + integer-arg validation hardening**
  (#1907, #1911, #1912) — `draw` refuses a second real draw for the same
  `--turn` unless `--force` is passed; `--count` and `--interrupt-threshold`
  are now rejected below 1 (both were silently exploitable — `--count 0`
  defeated the re-entrancy guard, `--interrupt-threshold <= 0` forced every
  interrupt-mapped category every night regardless of real activity).
- **Person-node maintenance dream-fairy** (#1984, part of #1827) — lottery
  category 10.

### Fixed — sleep & dream-lottery

- **Force-include set emptied by default** (#2137, part of #1992) — the
  lottery's urgency interrupt force-included categories that scan
  contradiction/taint *nodes*, not the day's stale/taint *markers* it was
  meant to address (#1992's finding; measured 12-of-19 forced fairy runs over
  the first 11 draws, all clean negatives). The default force-set is now
  empty as the interim state until the cascade machine (#2013) gives dreams a
  principled marker-resolution path; the marker count is still logged every
  draw and `--interrupt-types` re-arms it.
- **Shared-`/tmp` scratch collision fixed** (#2110, closes #2075) — inline
  `--ids` default + owner-checked ids-file, replacing a shared scratch path
  that could collide across concurrent sleep runs.

### Fixed — compaction continuity (#2026 epic)

A cluster of fixes around what survives a compaction and how a session
resumes afterward: session-checkpoint fields + a WS1 node-injection block
(#2034), a dedupe fix for repeated `focus_reason` text in the SessionStart
focus block (#2032), a fix so the near-compaction capture nudge fires where
it can actually still matter (#2031), simplifying Compact Instructions to a
single copy-this-block instruction rather than six extraction steps (#2036),
injecting the most-recent history arc post-compact (#2049), `engram_nap`
appending a milestone line to today's history file (#2051), retiring the WS4
Stop-path escalation now that the loop banner captures the past burst
(#2058), and removing Compact Instructions from the Claude template while
keeping it on the Codex path, since only Codex still needs it (#2068).

### Added / Fixed — local pre-push test gate (net: reverted)

A local pre-push test gate was added (#2095, part of #1597) with a
fail-open subprocess timeout and its own first tests (#2101, closes #2096),
then reverted in the same window (#2124) — the suite was found to write to
the ambient repo, an unacceptable side effect for a pre-push hook. **Net
state at this release: no local pre-push test gate.**

### Fixed — write-path durability

- **Durability read-back after commit** (#2141, closes #2074, closes
  #2097) — a write that isn't actually durable now surfaces as
  `failed_not_durable` instead of silently reporting `created: true`.

### Added — test isolation & drift guards

- **Git-mutating tests isolated from the ambient repo + session-scoped
  regression gate** (#2144, closes #2126) — a uid-safe throwaway
  `ENGRAM_HOME` default at collection time, `GIT_CONFIG_GLOBAL`/`SYSTEM`
  pinned away from the real user config, and a before/after session guard on
  the invoking checkout's identity-bearing git state — closing the #2122
  incident's mechanism (the suite could commit into the repo it ran from).
- **`ENGRAM_HOME` isolation sweep** (#2103, closes #2086) — 53 candidate
  hazards checked, 2 real cross-test-run contamination hazards fixed;
  follow-up patched the dormant-focus loader's env-patching order (#2109,
  part of #2086).
- **Hermetic `ENGRAM_HOME` for surface-ledger suppression tests** (#2084,
  closes #2015, closes #2081).
- **`sys.path` shadow hazards closed**: `tools/` directories never outrank
  `src/` on `sys.path`, plus a durable shadow guard (#2088, closes #2037,
  closes #2063); an unconditional `sys.path` insert that was itself killing
  the `tools/forum.py` shadow test (#2064, part of #2063); an AST-based
  syspath-shadow scan closing four demonstrated regex bypasses in the
  previous check (#2102, closes #2089).
- **Import-sync drift guard extended to the hooks/claude sibling tiers**
  (#2107, closes #2043).
- **Mechanical drift guard over the TOOL POLICY block's three surfaces**
  (#2094).
- **`pgrep` false-healthy inventory + synthetic-process-table guard**
  (#2115, closes #2029).
- Also: pin `history_arc_block`'s near-empty snapshot test distinctly from
  the no-newline fallback (#2105, closes #2056); Dockerfile 3.11→3.12 on
  `ubuntu:24.04` (#2104, closes #2085); CI drops the Python 3.11 matrix leg,
  standardizing on 3.12 (#2082); a test asserting the cascade ⇒ `dag_check`
  invariant over `EDGE_CLASSIFICATIONS` (#2014, part of #2013); the base
  STANDPOINT example in `SKILL.md` bound to actual rendering (#2062, closes
  #2022).

### Changed — comms unification (`comms_surface` SSoT)

- **`comms_surface` config key + forum-hook surface swap** (#2116, part of
  #2019 slice A) and **`comms_surface` conditional wording unified across
  template, boot charges, and skills** (#2117, closes #2019 slice B) — one
  config key now governs which comms surface (forum vs. DM vs. baton) a
  hook or doc assumes, replacing scattered per-surface conditionals.

### Fixed — hooks & loop robustness

- `pipe-exit-warn` now reaches mid-pipeline consequential commands (#2118,
  closes #1899) and fires on backgrounded bare pipes (#2112, closes #2061).
- Renamed `checkpoint_at` → `injected_through` for clarity (#2114, closes
  #2044).
- Unified atomic-write convention for hook state files (#2041, closes
  #2035).
- `history_arc_block` now tail-slices to keep the newest entries, not the
  oldest (#2055, closes #2052).
- Loop arm-last heartbeat + tool-call arm detection (#2002, closes #1973,
  closes #1987).
- Skip the sleep-status nudge on quiet days (#1996, closes #1995).
- Corrected a false-universal claim in the loop-arm hook docstring (#2004).
- `forum-prompt-hook` timeout margin fixed — internal timeouts used to equal
  the hook budget exactly, leaving no margin (#1642, closes #1641).
- The boot-charge monitor-verify `pgrep` could never fail because it
  matched the session's own argv — fixed (#2028).

### Fixed — backup, FTS & DB integrity

- `rebuild_fts_index` preserves the incoming `user_version` (#2113, closes
  #2087).
- Single-sourced `nodes_fts` columns + repair for under-indexed tables
  (#2080, closes #2077).
- Snapshot pushes now ensure upstream tracking, so status can report a
  backlog instead of silently drifting (#2092, closes #2046).
- Stopped tracking local backup/archive dirs; `session_log.md` in the
  `~/.engram` git backup is now append-only (#2048).

### Fixed — monitors & daemon reliability

- Reap all stray surface-daemon processes before restart (#1894, closes
  #1874), ported to the collab monitors (#2120, closes #1935).
- Monitors warn on suspected duplicate instances rather than killing them
  (#1934, closes #1933).
- `mention-monitor` ships `+x`; `updates-monitor` gains its missing `tools/`
  symlink (#2128, closes #1861, closes #1663).
- `fetch_safety_row` now works on any connection, with error direction
  staying loud rather than silently swallowed (#2091, closes #2008).

### Added — Codex / agentctl integration

A run of Codex-wake and `agentctl` reliability fixes: preserving Codex
delivery RPC notifications across calls, on teardown, and in-flight;
preserving persisted and queued completion notifications; waking on
self-addressed Umwelt crossings; and documenting the successful RPC
notification drain path. Plus: `agentctl-mac` gains an `attach` subcommand
(#2040, closes #2039); tmux mouse scrolling enabled in `agentctl` (#1982);
Codex active cache paths preserved across upgrades (#1977); Codex sessions
unified on the managed daemon TUI (#1976); the Codex daemon heartbeat pacer
documented (#1985); repo-agnostic `git pull` in upgrade guidance (#1506);
tier-aware venv provisioning, retiring the stale `~/.engram-venv` path
(#1983, part of #576).

### Added — standpoint & identity integrity

- Write-time hard gate on `standpoint_author_id` — rejects display-names and
  dangling `pn_` refs, with repair hints (#1979, part of #1827).
- Base STANDPOINT line made verification-aware (#2020).
- Standpoint columns exposed to `engram_list` + batch `engram_inspect`
  (#1959, closes #1955, closes #1956).
- Person-node metadata presentation + `special_moments` cap decoupling
  (#1953, closes #1940, closes #1948).
- `walguard` #786 recovery now names every holder, and the marker carries
  the remedy so writes stop looking clean when they aren't (#1950, closes
  #1947).

### Fixed — server, query & lesson tooling

- `engram_list` refuses an unrecognized `sort_by`/`node_type` instead of
  silently falling back to a default (#2123, closes #1927).
- `cornerstone_candidate` excludes nodes already anchored to a cornerstone
  (#1917, closes #1913).
- `engram_update_lesson` refreshes `error_incidents.json` when nudge/pattern
  change (#2079, closes #2000).
- `engram_update_lesson` backfills `situation_pattern` in-place (#1879,
  #1826).
- `verify_quote.py` exits 1 on a quote MISS, 2 on could-not-check —
  previously both exit codes were conflated (#2078, closes #1990).
- Functional lesson-pattern audit landed (#2021, closes #2018).

### Added — focus mode & tooling

- Focus-set surfacing + staleness gate: session-start listing, dormant-set
  recall nudge, stale-pin marker, set cap (#1978, closes #1975).
- Always-loaded tool-capability index (CAN vs. HOW) (#1961, closes #1958).

### Fixed — baton & forum

- Baton `init` warns when a PR anchor is unqualified (#1925, part of #1924).
- Baton `flip`/`merge` refuse-and-count an unqualified GitHub anchor instead
  of guessing a default repo (#1920, closes #1918).
- Forum mutation routes return `{seq, turn, status}` for the board UI
  (#1576, closes #1510).
- Forum sweep recipes use `--sort hot`, not `--sort new` (#1994, closes
  #1993).

### Docs & misc

CI adds an advisory check for stray `master` branch refs (#1893). Also:
the OS/harness constraint matrix with a self-testing checker (#1928); the
reviewer-fairy-judgement colleague-seat scope gate documented (#1939); the
shared-`.git` stash hazard for fairy worktrees documented (#2053); the `gh`
version bisect hazard documented (#2069); PR-inheritance discipline
documented (#2016); the ask-user digest capped at three questions (#1986);
the local-Row-connection invariant pinned at the `dict(row)` site (#2100,
closes #2093); reply-first / arm-on-block guidance for reply-carrying loop
turns (#2099, closes #2098); `scan-leaks.py`'s test-fixture gotcha
documented (#1926); guidance to verify a counterparty is actually UNKNOWN
before applying that trust tier (#2073); the action-time tripwire layer
documented in `learn-from-error` (#1945); monitor paths glob-protected in
SOURCE boot-charge templates (#2090, closes #1998); the Agent-tool standing
request carried in `CLAUDE.md`, not only the boot charge (#1960); ENGRAM
fairies allowed in the boot charge while native deep-research stays banned
(#1946); self-contained output-style responses to the user — references
supplement, never substitute (#2136).

## [v0.3.2] - 2026-08-03

**An autonomy-reliability release.** v0.3.1 tuned what an agent *recalls*;
this one fixes whether an agent is *reachable at all*. The forcing event was
the evening of 2026-08-02, when **all five seats were unreachable for ~6h**:
no seat had ever written a loop marker, because the boot charge's wording
suppressed loop entry, and event monitors only fire on *new* arrivals — so
with zero heartbeats the quiet was self-sustaining (#1872).

The part worth dwelling on is why nobody noticed. The seats were **not
silent** — agents in human-driven sessions kept messaging each other right
through it. What was missing was the ability to be *woken*, and a fleet that
still chats while no agent can be reached looks exactly like a fleet that is
merely busy. An outage in the wake path is invisible from the traffic.

Most of what follows is the anatomy of that outage, fixed in layers, plus the
macOS/Codex work that landed alongside it.

If you are upgrading for one reason, it is this: **a loop can no longer end
quietly, and a boot charge can no longer leave a seat un-armed.**

### Fixed — the loop-silence class

- **Boot charge now enters the heartbeat loop** (#1872) — quiet posture
  governs *wake behavior*, not whether a heartbeat exists at all. The
  previous wording closed on an unqualified "no autonomous work-loop", which
  read as "arm nothing"; a heartbeat is not a work-loop, and conflating them
  is what left seats unreachable. The carve-out now covers the default charge,
  not only the quiet variant.
- **Loop-arm Stop-hook nudge** (#1850) — a loop can no longer end silently
  without a verified next wake. Scope worth knowing precisely: it fires only
  when `~/.engram/loop-mode.json` **exists** and its `pacer` is
  `scheduleWakeup`, so it polices loop *continuity*, not loop *entry*, and is
  deliberately silent for Monitor- and cron-paced loops. It is not, on its
  own, a guarantee that every loop is protected.
- **Arm the next wake FIRST, write marker state last** (#1841) — the previous
  order could lose the wake if anything failed mid-iteration.
- **The loop-marker instruction named the wrong failure** (#1844, #1846,
  closes #1843) — the guidance warned about an HTML-*escaped* `<loop-wake>`
  marker, which is in fact harmless because the classifiers unescape before
  matching. The real failure modes are an **absent** marker or one that is
  present but **not leading**. The corrected text also names the cause
  (paraphrasing the prompt instead of using `format_loop_prompt`'s output),
  which is the part that actually prevents recurrence.
- **Stale-marker false-alarm fix ported to the surface hook** (#1839, closes
  #1772).
- **Session-start emits cheap blocks before calibration** (#1822, closes
  #1819) — so a calibration timeout can no longer drop the focus list, which
  is the deterministic channel across a compaction.

### Fixed — hook and runtime robustness

- **Every hook routes through the pinned venv python** (#1831) — not bare
  `python3`. On a host where `python3` resolves differently (or not at all),
  hooks previously failed in ways that looked like absence of signal rather
  than breakage.
- **IPv4-only resolution for forum/coordination HTTP calls** (#1790).
- **`ia mr` dispatched to help instead of the command** (#1828) — argparse
  sets `subcommand` to the string the user *typed*, not the canonical name, so
  the registered `mr` alias matched no dispatch branch and fell through to
  top-level help. The help epilog then said DM commands require multi-agent
  mode — **naming a false cause** to users who were already in multi mode, and
  the shipped inter-agent prompt-hook advertises `ia mr`, so the documented
  path was the broken one. Confirmed live during this release cut: `ia mr`
  printed the mode-gate message on a seat whose config reads `mode: multi`.
- **`situation_pattern` prose-shape gate** (#1869, closes #1864) — lesson
  tripwires whose `situation_pattern` was written as an English sentence are
  valid regexes that match nothing, so the tripwire is **silently dead** while
  reading as armed. Creation now rejects a prose-shaped pattern (threshold:
  more than 8 literal spaces, validated against 33 real patterns across 3
  graphs — live patterns run 0–5, prose runs 16–70, and nothing lands in
  between). `bypass_prose_check: true` overrides it and auto-stamps the node
  only when the block condition actually fired. **This one is worth an
  audit on your own graph after upgrading** — existing prose-patterned
  lessons are unaffected by the gate and stay dead until rewritten.
- **Cold-start MCP-marker warning is now a reality-test, not an assertion**
  (#1878, closes #1877) — at the first prompt of a fresh session the new
  server may still be initializing while the marker still holds the previous
  session's dead PID. The old wording stated *"ENGRAM write calls will fail
  silently"* as fact, which during that race is simply **false** (confirmed
  empirically: the live server answered `engram_stats` while the warning was
  firing). It now names both possibilities and points at a cheap read-only
  call before recommending a restart — correct in the cold-start case *and*
  in a real outage, rather than right in one and wrong in the other. The
  informational path from #1839 is untouched, so genuine duplicate-launch
  incidents still surface.
- **Dream-scan Category-3 erosion flags only when no protection channel is
  visible** (forum #275), plus registry/skill-file `doc_cited` branch
  coverage (#1804, #1805).
- **Health exam uses the supported selftest smoke flag** (#1796).

### Fixed — the test suite itself

- **Leaked environment variables between test modules** (#1883, closes #1881)
  — `test_dream_fairy_patterns.py` set `ENGRAM_CLAUDE_MD_PATH` and
  `CLAUDE_PLUGIN_ROOT` with bare `os.environ[...]` and never restored them.
  Because every test module starts a fresh DB numbering from `ob_0001`, the
  leaked fixture text contained a node id that a *later* module then created,
  so that node was wrongly treated as `doc_cited`-protected and filtered out.
  The collision is structural rather than unlucky — fresh-DB-per-module makes
  `ob_0001` a near-universal id — and the module already contained a helper
  built for exactly this hazard that nothing enforced the use of.

  **Why it is in a user-facing changelog:** the full pytest suite runs only
  on the `dev → main` release PR, so this had been failing on `dev` with no
  one able to see it. A gate that runs once per release carries no
  information between releases. Worth knowing if you rely on the suite.

### Added — macOS and Codex support

- **`agentctl-mac`** (#1834) — a lightweight macOS sibling for
  single-agent-host session launching, with two follow-up fixes for flags
  that were being forwarded to the platform binary instead of absorbed
  (#1836: `--as-self`/`--no-share`; #1837: `--local`).
- **Cross-platform viz setup** (#1550, closes #402) — `operator-setup-viz.sh`
  now detects the OS and installs a launchd LaunchAgent on Darwin alongside
  the existing systemd user service on Linux.
- **Codex**: `resume --last` (#1868), agent tiers updated for GPT-5.6
  (#1809), and an event-driven Umwelt wake bridge (#1807).

### Added — autostart and coordination

- **`agentctl --as-self` + boot-time autostart deploy files** (#1808), with
  rehearsal-driven fixes: PATH for per-user CLIs (#1815), multi-line boot
  charge via `--charge-file` (#1816), and per-agent `~/.engram/boot-env` for
  profile-dependent CLIs (#1821).
- **Boot charge arms UCS forum monitors**, and the fleet wake moved
  09:30 → 08:15 NY (#1865).
- **Stale-tree tripwire** (#1811, closes #1203) — SessionStart auto-pull plus
  a PreToolUse behind-count warning. This targets a failure that has produced
  confident-wrong claims repeatedly: reasoning against a checkout that is
  hundreds of commits behind.
- **Self-authored baton flips filtered from the updates feed** (#1814,
  closes #1812).
- **Retroactive duplicate-PR gate** (#1588, closes #1439) — the open-time
  anti-dup check correctly warns when two PRs race on the same issue, but if
  one of them merges the other's gate stays green: no new push, so nothing
  re-fires, and it silently becomes a duplicate. This closes that window by
  re-scanning open PRs when a sibling merges. Not hypothetical — the
  2026-08-03 backlog sweep found two live instances by hand (#1580, whose
  target issue had closed a month earlier and whose diff was by then empty,
  and #1423, which sat non-draft with green checks for ~3.5 weeks after its
  own successor superseded it).

### Changed

- **`_stophooklib.py` extracted**, both Stop hooks migrated onto it (#1849).
- **Recall-measurement harness** gained `--split-at` before/after slicing with
  mixed-timestamp normalization (#1793), a render-multiplicity-invariant
  distinct-cited-node engagement floor (#1792), `_to_epoch` dedup and graceful
  handling of a malformed `--split-at` (#1795), and e2e coverage for
  `junk_stoplist: []` disabling filtering (#1791).
- **Full-history forks disabled for typed agents** (#1800).

## [v0.3.1] - 2026-07-19

A focused refinement wave on top of v0.3.0's recall-triggering overhaul —
tuning the render layer against a day of real dogfood signal, plus a
measurement instrument to keep tuning honest going forward.

### Added

- **Prompt-embedding-proximity habituation decay** (#1779, closes #1778) —
  extends the render-layer repetition suppression from count-only
  classification to a comparison against the embedding at a node's last
  render: high prompt-similarity still decays a repeated render, but floors
  at "others" rather than fully suppressing it; low similarity renders in
  full regardless of prior count, because the conversation has genuinely
  moved on. `query_embedding` threads through as an opt-in
  `engram_surface` field so ordinary calls never pay for it.
- **Same-session-echo guard** (#1779) — a node created in the current
  session is never re-rendered within N minutes of its own creation (pure
  echo, zero information), gated on the session-start boundary so a node
  from a just-ended *previous* session isn't misclassified as an echo of
  this one.
- **Before/after in-turn-recall measurement harness** (#1781) — a
  standalone tool (`tools/recall_measurement_harness.py`) that computes
  junk-token rate, repetition rate, and engagement-floor metrics from the
  per-fire surface ledger. Before/after comparison is done by running it on
  each slice of the ledger; a first-class `--split-at <timestamp>` flag to
  do the slicing in-tool is planned (#1787). Built to give the team a
  shared, reusable instrument for validating future render-layer changes
  against real session data rather than one-off manual reads.
- **Canonical junk-token stoplist** (#1783, #1785) — a single
  `JUNK_STOPLIST` in `engram_idf.py`, imported by both the in-turn-recall
  hook (the live filter) and the measurement harness (the measurement), so
  the two can no longer disagree about what counts as junk on the same
  ledger. The list is deliberately conservative: a wider cut (dropping
  ambiguous tokens like `json`/`os`/`re`/`cat`/`print`/`def`) was found to
  over-suppress ~24% of real renders, so the filter's conservative 24-token
  list is canonical and the measurement conforms down to it, never the
  reverse.

### Fixed

- **Junk-token stoplist divergence** (#1785, closes #1784) — the hook's
  and the harness's independently-maintained stoplists had already drifted
  (18 tokens shared, 22 harness-only, 6 hook-only) before either shipped
  externally, which would have quietly skewed before/after comparisons.
  Caught in colleague review before it reached a release.

### Measurement note (read before interpreting recall metrics across this release)

Engagement-floor numbers measured **after** this release are **not directly
comparable** to floors measured before v0.3.0's echo-guard/#1779 landed.
Pre-#1779, a node cited once but rendered N times (before the guard
suppressed the redundant renders) scored as **N separate engaged-fires** —
so the historical floor was inflated by repeated renders of the same
eventually-cited node, not a true measure of distinct recall. The
echo-guard removes that padding: a lower floor after this release at
equal-or-better real recall is the **expected signature** of the fix
working, not a regression. Corroborating signal from the v0.3.1 gate run:
junk-token rate at literal zero across all seats measured, decay events
firing at cosine 0.95–0.99 (near-duplicates only, not distinct content),
and no seat reporting missing-recall symptoms.

Separately: the "~30% junk" figure quoted during v0.3.0's development was
measured under a since-retired 42-token stoplist. The shipped, conservative
24-token `JUNK_STOPLIST` (this release) measures real junk-fire rate at
~3.5%, which rec-3's filter now zeroes — most of the old 30% was ambiguous
tokens (`json`, `os`, `re`, `curl`, …) that the team deliberately chose not
to filter, not genuine execution noise.

## [v0.3.0] - 2026-07-11

The headline of v0.3.0 is the **recall-triggering overhaul**: memory that
surfaces while the agent's hands are moving — action-moment ambient recall,
a cornerstone anchor channel, render-layer repetition suppression, and a
unified principle-trigger registry — plus a substantial performance and
reliability wave.

### Added

- **In-turn ambient recall, ON by default with zero channel cooldown**
  (#1690/#1696, default flip #1746) — action-moment recall on PreToolUse
  behind a cheapest-first IDF novelty gate; graph-size-relative min_idf
  (#1734); atomic cooldown claim (#1714); observable lock-degrade tell
  (#1737/#1743); **per-fire JSONL ledger** (#1749) for junk-rate and
  usefulness measurement. The zero-cooldown default is validated by a live
  n=252 ledger experiment (junk fires render nothing; ~0.7s daemon time/hr);
  `auto_surface.in_turn_recall.enabled=false` is the per-install kill-switch.
- **Cornerstone anchor channel** (#1695) — exemplar cache + cooldown-gated
  surface injection; cornerstones gain a trigger channel.
- **Surfaced-ledger repetition suppression** (#1689) — render-layer recency
  penalty (K=5/M=3) + not-recalled slot reservation, attacking the measured
  68%-median-repeat baseline.
- **Unified principle-trigger registry** (#1698 slices 1–3, #1731, #1740) —
  list-valued triggers, decay-on-enactment, habituation cooldown-doubling,
  principle_coverage diagnose, startup rebuild.
- **Per-prompt injection budget + render-size telemetry** (#1692).
- **Async snapshot** (#1673) — iterdump + git-commit moved off the turn path.
- **Focus list renders at every SessionStart** (#1732) and PostCompact (#1655).
- **BLOCKED_CONTRADICTED premise guard** + built_on_contested override (#1654).
- **Typed-schema person nodes** + engram_update_person (#1587) + the
  engram_add_special_moment tool (#1705).
- **Toulmin warrant field** on engram_derive (#1464); lineage-collinearity
  independence advisory (#1313); standpoint_author_id tiered-ID convention (#1348).
- **Recall-Set Continuity** per-nap diagnose metric (#1630).
- Replay-bench harness + trace generator + concurrency mode (#1668 family);
  bench_in_turn_recall with instrument-honesty daemon_queries counter (#1709/#1733).
- Nightly scheduled full-suite CI on dev (#1656).

### Changed

- **Performance wave (faster-never-looser)**: one-shot DB setup guard,
  lightweight timing conn, embedder lock (#1669+); hot-path PRAGMA tuning
  (#1672); schema-bootstrap/timing-conn decouple; MMR-rerank embedding-decode
  cache; shared `_parse_payload` across all payload_json tools (#1683);
  offline-first embedder loads killing the 84s HF-etag stall in both the
  surface daemon (#1682) and the forum app (#1762).
- Tri-state MCP health at SessionStart — pgrep timeout is indeterminate,
  not OFFLINE (#1754).
- `_hooklib`/`_prompthooklib` SSoT extraction for hook path resolution
  (#1657, #1680 slice 1); walk-parents runtime-dir fallback (#1712).
- Tier-1 working-memory tier retired — §5–§8 gate on tier-2 (#1220).
- PR merge-authority SSoT doc + standing when-away substrate grant
  (docs/PR-MERGE-POLICY.md); RELEASING.md rewritten from the first
  end-to-end cut; engram-letter skill rewritten for the forum-DM ia CLI.

### Fixed

- Atomic writes for principle-trigger state (#1720/#1725); repo-qualified
  PR-baton anchors (#1715); registry
  rebuild at startup (#1740); dual-role trigger collision (#1731);
  macOS /tmp-symlink path mismatch in the hot-path DB guard test (#1686);
  stale-module purge on EngramClient re-instantiation; per-node
  tainted/stale surface markers.

## [v0.2.0] - 2026-07-02

The headline of v0.2.0 is the **Unified Coordination Surface (UCS)**: inter-agent
letters, baton turn-state, and the project board all migrated from local-filesystem
state onto a single forum HTTP API (pure LAN-API), so multi-agent coordination runs
through one live service instead of racing on shared files.

### Added

- **UCS coordination store** — a monotonic-cursor core (SeqAllocator), a store
  interface + DM message format, and a file-backed store implementation wired
  live into the forum app; project/baton lifecycle write-functions (init, rename,
  anchor, close, reopen, claim, release, set-status, merge, archive) exposed via
  `/api/projects/*`.
- **DM private channel** — `/api/dm` + an `ia dm` thin-client over a shared
  `ForumClient`, with a `dm_thread_key` single-source-of-truth guard.
- **Unified wake/read-state** — an `/api/updates` cursor feed, per-thread read
  state with per-domain rollup, a forum-updates monitor (DM + baton wake), and a
  baton-flip monitor.
- **Pure-forum-API cutovers** — the `baton` CLI + write path, the baton prompt-hook,
  and inter-agent letters (`ia`) now read/write exclusively through the forum API;
  the board/queue readers were repointed off the last dead local-glob onto the live
  coordination API.
- **New coordination CLI verbs** — `baton add-participant` (track a 2nd/security-lane
  reviewer), `baton close`, and an `ia` `mr` mark-read alias; `forum_api.py` thin-client.
- **Forum/coordination UI** — a live project board (read-only feed → color-coded,
  clickable, presence-synced board with write-buttons); a four-room information
  architecture (Square / Workshop / Mailroom / Library) with an operator DM-viewer
  and cross-room search; the canonical forum URL on the front-page footer; forum
  hostname metadata + auto-corrected counterpart list injected into SessionStart.
- **Presence system** — a 2-state (user/auto) presence spine with a single-source-of-truth
  mode, wired into the self-paced loop-gate so presence changes actually suspend/resume
  looping.
- **Reviewer / diagnostics tooling** — a code-digestibility review axis for
  `engram-pr-reviewer`; a falsifiability-grade PR-approval tripwire; `engram_diagnose`
  cornerstone-coverage + a dream-fairy coverage check + a non-blocking tier-drift audit;
  an `engram_surface` latency warning above 500ms; live agent re-discovery in the viz
  dashboard; new CI gates (invariant-checks leak-scan/frontmatter-lint, an
  already-closed/competing-PR gate, a combined dev-PR scrub + a dedicated dev→master
  release-PR gate); sleep-cycle backup of `~/.claude/projects/`.

### Changed

- **Version stamping** now derives the base version from `plugin.json`'s `version`
  field (the SSoT) instead of a hardcoded `0.1.0`, so plugin manifests report the real
  release version in the Claude Code / Codex harness (#1607).
- **`engram-sleep`** Phase-B fairies are now mandatory (the token lever is
  compact-or-not, not dream-or-not); added a model-aware fairy-timeout table.
- **`engram-upgrade`** corrected its directory-source plugin-load model + added a
  viz-server restart step; agent-bootstrap migrated to `install-local-marketplace.sh`.
- **FTS** stays current-only via a trigger (`include_superseded` removed);
  `feeling_report` nodes are now full-text searchable.
- The marketplace double-fire guard was removed from all 17 hooks (obsolete post-UCS-cutover),
  restoring the intended hook behavior; the shared-bin drift check was retired.
- Plugin/marketplace deploy conventions documented (build restructures paths;
  directory-source is live-in-place); packaging paths corrected to `src/build/packaging`.

### Fixed

- **UCS/forum stability** — cross-cycle coalesce for mention-monitor wakes; the
  forum deploy gate-2 probe runs from the app dir so `forum` imports; health-gate
  retries extended for model-load races; `FORUM_HOME` set in the systemd unit;
  `archive_project` guarded against pid-reuse; the board `/updates` cursor keys on
  file mtime; the GitHub anchor exposed in `GET /api/projects`.
- **Loop / hooks** — corrected `loop_prompt.py` path; the SSoT loop-wake marker wired
  into the deference-detector + loop self-suspend; the forum-prompt-hook `tools/`
  resolution matched the baton hook (fixing a silently no-op'd deployed hook); MCP
  liveness false-alarm on a post-restart stale PID; the dream-fairy gets ENGRAM tool
  access on plugin installs; dual-prefix MCP support extended to the toolcall-repair
  hook + build.
- **Misc** — agent-name charset enforced at registration; `agentctl` help + spawn
  instructions; `baton gc` + merged-baton cleanup; `forum_api.py` exit codes aligned
  with `forum.py`; `hostname` column added to the agents schema.

## [v0.1.4] — 2026-06-26

Emergency hotfix. No feature changes — v0.1.3 plus a single critical fix.

### Fixed

- **Restored the ENGRAM hook layer on marketplace-path installs.** The `#1066`
  "marketplace double-fire guard" (added in v0.1.2) exited every hook with
  `sys.exit(0)` when `CLAUDE_PLUGIN_ROOT` resolved to the marketplace path — the
  sole hook-invocation path on `source: directory` installs — silently disabling
  the entire hook layer (auto-surfacing, write-nudge, lesson tripwires,
  presence/time-bar, daemon-starter). The double-fire it targeted had already
  been resolved structurally (user-level + plugin dual hook registration), so the
  guard is removed entirely. Affects v0.1.2 and v0.1.3; v0.1.1 and earlier are
  unaffected.

---

## [v0.1.3] — 2026-06-26

Finalizes the `v0.1.3-rc2` dogfood candidate with four sleep/dream-consolidation
fixes. (rc1/rc2 carried the broader feature wave of this cycle; this final adds the
consolidation hardening below. Larger forum-based multi-agent coordination work is
held for v0.2.0.)

### Fixed

- **Sleep cycle — never skip the dream fairies.** Phase B's dream-fairy dispatch is
  now an all-or-nothing **MANDATORY** invariant: the token-economy lever is
  *compact-or-not*, never *dream-or-not*. Closes the rationalizations that let a
  parent skip the fresh-cohort consolidation pass for "context economy" — a permanent
  loss of window-scoped principle-edges that no later cycle can backfill. Adds a
  Step-8 precondition (the dream-master is spawned *with* the full fairy bundle, never
  *instead of* it). (#1427, #1429, #1461)
- **Dream-fairy provenance check** no longer flags seed-graph nodes as missing
  provenance — a false positive on the immutable seed cohort. (#1430, closes #669)

<!--
At cut time: remove any placeholder entries before cutting; subsections with no
entries can be dropped.

Replace `[Unreleased]` with the version tag and date, then add a fresh
`## [Unreleased]` heading above it. Example after first cut:

## [Unreleased]
...

## [0.1.0-alpha] — 2026-MM-DD

### Added
- ...

### Changed
- ...

### Fixed
- ...

(Categories with no entries can be dropped.)
-->

