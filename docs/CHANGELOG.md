# Changelog

All notable changes to ENGRAM Alpha are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with an `-alpha` suffix during the alpha phase.


## [Unreleased]

_Nothing yet — changes land here after the v0.3.2-rc1 cut._

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

