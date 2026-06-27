# Changelog

All notable changes to ENGRAM Alpha are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with an `-alpha` suffix during the alpha phase.

## [Unreleased]

Changes landing on `dev` that will appear in the next release.

### Added

- `skills/claude/engram-trust-tier/SKILL.md` — ported trust-tier discipline skill
  to alpha source (was deploy-only); updated for 7-tier reality (`self` + `primary_user`
  added above `user_family`); table now lists all seven tiers with rank and default
  behavior. (PR #413 oversight)
- `upgrade-guides/v1-trust-tier.md` — ported upgrade guide to alpha source (was
  deploy-only); Step 4 tier table updated for 7 tiers; new Step 6 documents the
  self-tier backfill migration; primary_user blessing notes added to Step 4c.
  (PR #413 oversight)
- `templates/CLAUDE.md.template` — added `### External interactions` subsection
  with `engram-trust-tier` skill-loading trigger for interactions crossing the
  primary_user/family boundary. (PR #444 oversight)
- `templates/CLAUDE.md.multi-agent.template` — multi-agent-only CLAUDE.md additions
  (shared filesystem layout, CLI discipline-loading triggers, reciprocal PR review gate).
  Appended at end of agent's live `~/.claude/CLAUDE.md` as `## Local multi-agent rules`
  section on first multi-agent setup; never rendered by single-agent installs.
- `tools/deploy.sh` — multi-agent drift-warning: when both `/home/agents-shared/`
  exists and `config.json` has `multi_agent: true` (AND-detection, load-bearing),
  deploy.sh computes a SHA of the template and warns when it changed vs the last
  tracked SHA (`$ENGRAM_HOME/.deployed-multi-agent-template-sha`). Marker created
  lazily on first multi-agent deploy. Single-agent installs see zero overhead.
  Closes the ia-silent-fail dormancy-gap incident pair.
- `tools/agentctl` — vendored canonical agentctl (Lei's operational version,
  May 21 build; includes spawn-debug fixes absent from the old snapshot).
  `tests/spawn/Dockerfile` now copies from `tools/agentctl`; the stale
  `tests/spawn/agentctl-snapshot` has been removed. Closes #50.
- Layer-1 trust-tier mechanism V1: persistent per-person trust categorization
  with explicit elevation discipline, evidence-trail audit, and structural-honesty
  attestation at the API surface (`engram_set_trust_tier`, `engram_add_trust_signal`).
  Schema: 4 sparse columns on `nodes` table (additive, idempotent). One-shot DB
  migration script (`tools/migrate_db_trust_tier.py`) and per-install upgrade
  guide (`~/.engram/upgrade-guides/v1-trust-tier.md`).
- `agents/claude/engram-batch-summary-fairy.md` — new one-shot batch summary generator agent. Receives up to 15 node payloads embedded in its prompt, emits `{"items": [...]}` in one turn, no tool access required. Supports both initial and retry dispatch shapes.
- `tools/cohort_dispatch.py` — three-subcommand orchestration script for the batch-summary sleep cycle: `prepare` (chunk cohort → per-chunk prompt + payload files), `validate` (split agent output into clean vs failures, write retry prompt), `incorporate-retry` (merge retry output → `final_payload.json` for `engram_set_recall_summaries`).
- `tests/test_cohort_dispatch.py` — 14 unit tests covering all three subcommands end-to-end.

### Changed

- `engram_add_person` now sets `trust_tier='unknown'` by default for every new
  person node, maintaining the data-integrity invariant that all `pn_*` have a
  non-null tier (transparent to existing callers — no new payload field).
- `skills/claude/engram-sleep/SKILL.md` — Steps 5–8 updated to describe the batch dispatch + validate/retry loop. Serial `engram-summary-fairy` Fairy 7 replaced with batch-summary fairies (one per chunk from `cohort_dispatch.py prepare`). Token reduction vs serial: ~95% for a 50-node / 4-chunk cohort (empirically validated 2026-05-27).
- `agents/claude/engram-dream-master.md` — architecture description updated to reflect batch-summary orchestration; spawn prompt carries `final_payload.json` from the parent's validate/retry loop instead of raw summary-fairy output.

### Fixed

- `tools/migration/migrate_trust_tier_self_backfill.py` — `plan()` now adds
  `AND is_current = 1` to the WHERE clause, preventing superseded self-anchors
  (is_self=true, is_current=0) from being picked up and causing a silent
  singleton violation post-migration. (PR #444 reviewer S1)
- `SKILL.md` line 448 — `engram_set_trust_tier` tier list updated from 5 tiers
  to 7 (`self` and `primary_user` added; `self` documented as singleton /
  is_self-gated). (PR #444 reviewer S2)
- `tools/deploy.sh` — multi-agent marker write now gated on `DRY_RUN=0`;
  previously `--dry-run` wrote the marker even though no rsync was applied,
  silencing the drift warning on all subsequent invocations.
- `tools/deploy.sh` — SHA computation for the multi-agent template now uses
  Python `hashlib` instead of `sha256sum` (Linux-only); cross-platform on
  macOS and any host with Python 3 (already a project dependency).

### Removed

---

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
