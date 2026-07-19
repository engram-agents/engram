# Changelog

All notable changes to ENGRAM Alpha are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with an `-alpha` suffix during the alpha phase.


## [Unreleased]

_Nothing yet — changes land here after the v0.3.1-rc1 cut._

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

