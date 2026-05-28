# Changelog

All notable changes to ENGRAM Alpha are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with an `-alpha` suffix during the alpha phase (see `RELEASING.md` for the version
scheme).

## [Unreleased]

Changes landing on `dev` that will appear in the next release. Cut and rename
to a versioned section when releasing per `RELEASING.md`.

### Added

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

### Removed

---

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
