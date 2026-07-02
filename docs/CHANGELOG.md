# Changelog

All notable changes to ENGRAM Alpha are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with an `-alpha` suffix during the alpha phase.


## [Unreleased]

_Nothing yet — changes land here after the v0.2.0-rc2 cut._

## [v0.2.0-rc2] - 2026-07-02

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

