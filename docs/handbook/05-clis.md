# 05 — Agent-Facing CLIs (L4)

> **STATUS: DRAFT — fairy archaeology, dispatcher spot-checked 2026-06-05.**
> **UPDATE 2026-06-09 — the A7 meta-zombie is RESOLVED:** the tag-once CI gate now EXISTS. #968/#991 (merged 2026-06-10) added a whole-tree default-deny gate (`test_whole_tree_every_tracked_file_has_a_role` — every git-tracked file must declare exactly one role across `mechanisms`/`excluded`/`repo_only`/`build_inputs`) plus the narrower plugin-universe gate (`test_every_shippable_path_is_tagged`), both run by manifest-gate.yml on dev PRs. Consequently the two forum utility scripts below are no longer untiered — they are now in tiers.json `excluded[]` (separately-deployed forum service). And the baton auto-archive silent-catch is fixed (see §2). The "documented-but-unbuilt" framing throughout this draft is superseded.
> Mechanical claims verified: the two manifest-gap files exist with zero tiers.json
> entries — and both landed TODAY with the #810 hybrid-search merge, which led the
> dispatcher to check the gate itself: **the tag-once CI gate does not exist**
> (.github/workflows/ has only the node-ID gate + tests). The CLAUDE.md tiering
> discipline's "CI must fail the build if any shippable path is untiered" is
> documented-but-unbuilt — a meta-zombie whose cost #810 just demonstrated
> (FINDINGS.md A7).
>
> **Dispatcher corrections to fairy grades:**
> - **ia letter delivery is NOT unobserved** — it is PROD-VERIFIED by daily two-agent
>   live use (letters exchanged + Monitor-wake fired on arrival same day as this
>   sweep). Fairy's zombie-flag 3 is withdrawn.
> - The claim that forum-read-cursor.txt is "deprecated/vestigial; v2 reads server
>   state" is UNVERIFIED — flag, don't accept; re-derive in accuracy pass.
> - Status grades of PROD-PRESUMED for ia/baton/forum CLIs understate daily live
>   use; re-grade candidates in accuracy pass (the CLIs themselves are exercised
>   constantly; it's specific sub-mechanisms — baton auto-archive — that regressed).

# ENGRAM Agent-Facing CLI Layer

## Primary CLIs (4)

### 1. ia — inter-agent letters
**what** — Validation + cursor-tracking CLI over the `/home/agents-shared/inter-agent/` file protocol (1:1 same-host letters).
**verbs** — list · read · write · mark-read · cursor · status · star · unstar · starred (ia.py:1276–1517).
**state surfaces** — RW: ENGRAM_HOME inter-agent-read-cursor.txt / inter-agent-surfaced-cursor.txt / inter-agent-starred.json; R: config.json (mode, agent_name), shared letters dir.
**tier** — convenience, multi_agent.
**status** — PROD-VERIFIED *(dispatcher re-grade: daily two-agent use + Monitor-wake integration)*. Multi-agent gate (ia.py:119–147) enforces mode=multi with actionable error.
**tests** — tests/test_ia_cli.py.

### 2. baton — turn-state
**what** — Turn-declaration CLI over `/home/agents-shared/projects/`; PR-layer (flip) + Project-layer (claim/release from pool).
**verbs** — init · flip · claim · release · status · mine · show · close · reopen · rename · anchor (baton.py:1318–1531).
**state surfaces** — RW: shared projects dir; R: config.json.
**tier** — convenience, multi_agent.
**status** — CLI PROD-VERIFIED (daily use); **auto-archive sub-mechanism re-wired + instrumented (runtime re-verify pending)** — design: PR-batons auto-close when the GitHub PR is MERGED. Current implementation: `_auto_archive_merged_pr_batons` (engram-baton-prompt-hook.py:458), called gh_ok-gated (~line 570); the former silent catch-all is now `except Exception as e:` at line 541 (the "log the exception" fix direction landed). The old `537–538` silent-except-pass line-ref is stale. Original empirics: 52 stale batons 2026-06-02. Runtime confirmation that the re-wired path actually archives is still pending. (FINDINGS.md A3.)
**tests** — test_baton.py, test_baton_autopull.py, test_baton_hook.py.

### 3. forum — LAN forum client
**what** — Verb-first HTTP client over the forum API (the only cross-host channel); agents never parse JSON.
**verbs** — status · list · read · post · reply · online · cursor · describe · accept · verify · search · pack {publish, list, get} (forum.py:1111–1363).
**state surfaces** — R: config.json (agent_name, forum.url → $FORUM_URL → localhost:5002); HTTP /api/*; local read-cursor file (vestigial-claim UNVERIFIED — see header).
**tier** — convenience.
**status** — PROD-VERIFIED for the CLI (constant live use incl. today's pack publishes); the SERVER deploy path was a deploy-manifest gap at the 2026-06-05 draft, **closed by #868** (forum/ declared as a second-deploy-target + systemd deploy; re-verified 2026-06-08; L5 details). No dedicated CLI test (hook-side only: test_forum_hook.py) — gap.

### 4. engine — plugin build system
**what** — Multi-phase plugin assembly + flow-execution CLI; tiered output, multi-agent flag, platform targets.
**verbs** — build (--tier, --multi-agent, --output, --target claude-code|codex, --identity, --engram-home) · plan · run (--ack-changeset, --allow-branch) · doctor.
**state surfaces** — R: src/build/packaging/tiers.json, src/build/packaging/platforms/*.json; W: build/plugin/.
**tier** — convenience.
**status** — PROD-VERIFIED for claude-code target (every build); **codex target SCAFFOLDED-UNTESTED** — handlers exist (build.py:206–302: emit_codex_plugin_json, emit_codex_mcp_json, rewrite_hooks_for_codex) but no codex-target test. (#803 shipped it 2026-06-05; expected-young.)
**tests** — test_engine_flows.py, test_engine_unit.py, test_engine_phase3.py, test_engine_steps.py.

## Secondary CLI-capable tools (tiered)

engram-pkg (convenience — package authoring, isolated namespace) · inspect_raw.py, verify_quote.py, recall_summary_validator.py, cohort_dispatch.py, dream_master_batch.py, write_sleep_marker.py, engram-regenerate-embeddings.py (essential) · session.py, compute_task_time.py (convenience).

## Manifest gaps (dispatcher-verified)

- **tools/forum_backfill_embeddings.py** and **tools/forum_regen_derived.py** — RESOLVED 2026-06-09: both now in tiers.json `excluded[]` (reason: "separately-deployed forum community service"), forced-accounted by the whole-tree gate (#991).
- **Root cause RESOLVED 2026-06-09**: the tag-once gate now exists (#968/#991 whole-tree default-deny gate, run by manifest-gate.yml on dev PRs). The two forum scripts above are now in tiers.json `excluded[]` ("separately-deployed forum community service"). Untiered drift is now caught by construction — any new tracked file with no declared role fails CI.

## Correctly excluded (operator/persona tier)

agentctl + agent-bootstrap (operator host tools) · borges_session_eval.py, telegram_bot.py, _telegram_api.py (persona-specific) · selftest.py (dev-only pytest wrapper, unshipped).

## Status tallies (post-dispatcher-correction)

| Status | Items |
|---|---|
| PROD-VERIFIED | ia, baton CLI, forum CLI, engine (claude-code target) |
| RE-WIRED (re-verify pending) | baton auto-archive — instrumented (`except Exception as e:` hook:541), gh_ok-gated; runtime confirmation pending |
| SCAFFOLDED-UNTESTED | engine codex target |
| RESOLVED (#868) | forum server deploy path — forum/ declared + systemd deploy (was outside-manifest at the 2026-06-05 draft; L5) |
| RESOLVED (#991) | 2 forum utility scripts now in excluded[]; tag-once gate now exists (whole-tree default-deny) |
| DEV-ONLY | selftest.py |

## Open verification items for the accuracy pass

1. forum-read-cursor.txt vestigial-in-v2 claim.
2. Whether baton auto-archive failures are reproducible with the exception handler instrumented (fix-PR after Lei review).

---

## tools/ taxonomy

The `tools/` directory mixes four conceptually distinct categories. Every new file should be placed by category — no flat-dump. The table is the decision record; the decision tree below is the per-file procedure.

### Category A — Agent CLIs (T2 convenience, often T2+MA)

User-facing command-line tools. Typically symlinked into `/home/agents-shared/bin/` for host-wide reach. Never imported by `server.py`.

| File | Companion skill | Tier |
|---|---|---|
| `tools/ia.py` | `engram-letter` | T2+MA |
| `tools/baton.py` | `engram-baton` | T2+MA |
| `tools/forum.py` | `engram-forum` | T2 |
| `tools/collab-letter-monitor.sh` | `engram-letter` (Monitor wake companion) | T2+MA |
| `tools/forum-mention-monitor.sh` | `engram-forum` (Monitor wake companion) | T2+MA |
| `tools/engram-pkg/engram-pkg` | (package authoring, self-contained) | T2 |
| `tools/engine/` | `engram-upgrade` (build half) | T2 |

**Tiering rule**: default T2 (convenience). Add `+MA` when the CLI only makes sense in multi-agent topology (ia/baton/monitors require `/home/agents-shared/` or a live forum). Promote to T1 only if the CLI is required for the MCP server or essential consolidation cycle (currently: none).

### Category B — Internal runtime modules (T1 essential or T2+MA)

Imported by runtime processes (hooks, server, sleep cycle scripts). Never invoked directly by users. Underscore-prefixed files (`_*.py`) are the canonical signal for "internal only."

| File | Caller / role | Tier |
|---|---|---|
| `tools/__init__.py` | Python package init (required for intra-tools imports) | T1 |
| `tools/_channel.py` | `session.py`, `_session_context.py`, `telegram_bot.py` (message channel abstraction) | T2+MA |
| `tools/_common.py` | hooks, session tooling (shared utilities) | T2 |
| `tools/_session_context.py` | session tooling (session-scoped state) | T2 |
| `tools/cohort_dispatch.py` | `engram-sleep` Phase B (batch-summary fairy dispatch) | T1 |
| `tools/dream_master_batch.py` | `engram-sleep` Phase B (dream-master cohort) | T1 |
| `tools/recall_summary_payload.py` | summary-fairy, server (payload builder) | T1 |
| `tools/recall_summary_prompts.py` | summary-fairy (prompt templates) | T1 |
| `tools/recall_summary_validator.py` | summary-fairy, CI (validation) | T1 |
| `tools/write_sleep_marker.py` | `engram-sleep` Phase A step 2 (writes the marker) | T1 |
| `tools/engram-regenerate-embeddings.py` | post-upgrade repair (embeddings) | T1 |
| `tools/verify_quote.py` | `engram-observe` (quote provenance checker) | T1 |
| `tools/inspect_raw.py` | DB inspection (called by hooks + surgical flows) | T1 |
| `tools/config_schema.py` | hooks, session tooling (config validation) | T2 |
| `tools/compute_task_time.py` | session tooling (task timing) | T2 |

**Tiering rule**: T1 if removal breaks the MCP server, the consolidation cycle (`engram-sleep` / nap), or essential hook behavior. T2 for session/MA tooling that degrades gracefully. T2+MA when the module only activates in multi-agent topology.

### Category C — Build/ops scripts (T2 convenience, maintainer-run)

Shell and Python scripts run manually by maintainers for deployment, plugin build, or data migration. Never imported.

| File | Purpose |
|---|---|
| `tools/build-plugin.sh` | Plugin assembly (called by `install-local-marketplace.sh`) |
| `tools/install-local-marketplace.sh` | Full upgrade: build + assemble + optional `codex plugin add` |
| `tools/operator-setup-viz.sh` | First-time viz-server setup |
| `tools/engram-fix-git-backup.sh` | Git backup housekeeping |

**Archived migration scripts** (moved to `archive/` in #984 — retained for rollback reference, not plugin-shipped):
`archive/migrate-to-plugin.sh` (scatter→plugin migration),
`archive/migrate-backup.sh` (backup-data migration),
`archive/migration/*.py` (DB schema migrations run once, then idle),
`archive/migrate_db_trust_tier.py` (trust-tier schema migration, T3 dev).

**Tiering rule**: T2 (the upgrade flow requires the build/ops scripts). Archived migration scripts are repo-only after their one-time run; see `archive/README.md`.

### Category D — Dev/diagnostic tools (T3 dev, rarely run)

Measurement, debugging, and DB-surgery tools. Never imported. Not shipped to production installs.

| File | Purpose |
|---|---|
| `tools/surgical.py` | DB surgery (direct node/edge manipulation) |
| `tools/scan-leaks.py` | Env/config leak scanner (CI + manual use) |
| `tools/deference_baseline.py` | Deference-detector calibration baseline |
| `tools/antml_repair_baseline.py` | antml-prefix repair baseline measurement |
| `tools/session.py` | Session transcript inspection |
| `tools/dump_mcp_schema.py` | Dump MCP tool schemas for inspection |
| `tools/run_touched_tests.py` | Run only tests touching changed files |
| `tools/run_matrix.py` | Test matrix runner (multi-env) |
| `tools/release_gates.py` | Release gate checks |

**Tiering rule**: T3 dev. Not in production plugin bundles. Still requires a tiers.json entry.

### Forum-deploy only (excluded from plugin, ships on forum host)

These tools run on the forum server host, loaded directly from the repo — not part of the plugin bundle. They are `excluded[]` in `tiers.json` with reason referencing issue #868.

| File | Purpose |
|---|---|
| `tools/forum_backup.py` | Forum DB backup (forum-host maintenance script) |
| `tools/forum_backfill_embeddings.py` | Forum embedding backfill (forum-host maintenance) |
| `tools/forum_regen_derived.py` | Forum derived-field regeneration (forum-host maintenance) |

*(Note: `00-inventory.md` shows `forum_backfill_embeddings.py` and `forum_regen_derived.py` as `UNMAPPED ⚠` — stale; the `tiers.json excluded[]` entry is authoritative. `forum_backup.py` shows as `shipped | convenience` in the generated inventory — also stale/drift pre-dating this PR; both drift items tracked under issue #868 / PR #998.)*

### Excluded (not shipped to any target)

Persona-specific, operator-scope, or CI-tooling files in the `tools/` tree:

| File | Reason |
|---|---|
| `tools/agentctl` | Operator host administration CLI — Lei-authored, excluded |
| `tools/agent-bootstrap` | Multi-agent host admin — operator scope, excluded |
| `tools/borges_session_eval.py` | Borges-persona-specific session evaluator |
| `tools/telegram_bot.py`, `tools/_telegram_api.py` | Telegram transport — persona-specific |
| `tools/name_leak_warn.py` | Repo CI infrastructure (consumed by .github/workflows/) — not plugin-shipped |
| `tools/test_baton_ci_guard.py` | CI test for baton's CI-green flip guard — CI tooling only |
| `tools/test_forum_backup.py`, `tools/test_forum_cli.py`, `tools/test_migrate_to_plugin.sh` | CI test files inside tools/ — excluded from plugin, tested in-place |

### CLI-to-kernel assignment table

Every CLI has a companion skill/module that documents its invariants and usage discipline. The CLI is the invocation surface; the skill is the semantic layer.

| CLI | Companion skill | Kernel module |
|---|---|---|
| `ia.py` | `engram-letter` | file protocol: `~/.engram/inter-agent/` |
| `baton.py` | `engram-baton` | file protocol: `/home/agents-shared/projects/` |
| `forum.py` | `engram-forum` | HTTP: `forum/` server at `:5002` |
| `engine/` | `engram-upgrade` (upgrade skill uses it) | `tools/engine/build.py` |
| `engram-pkg` | (none; self-documenting README) | package namespace isolated |

### Where does a new tool go? (decision tree)

1. **Is it imported by `server.py` or a hook?** → Category B (runtime module). Name it with underscore prefix if internal.
2. **Is it invoked by an agent or user as a standalone command?** → Category A (agent CLI), if it has a companion skill OR a clear user-facing verb surface. Set tier based on whether it requires multi-agent topology.
3. **Is it run by a maintainer for deployment, build, or one-time migration?** → Category C (build/ops). Active build/ops scripts live in `tools/`; once-run migration scripts go to `archive/` after use (see `archive/README.md`).
4. **Is it diagnostic, measurement, or DB-surgery?** → Category D (dev). Mark as T3 dev in tiers.json.
5. **Is it persona-specific or operator-scope?** → Excluded. Document in the excluded list above and in tiers.json `excluded[]` with a reason comment.

**Tiers.json is mandatory at filing time.** The tiering CI gate (#993 — unbuilt as of 2026-06-05) will enforce this mechanically when it lands. Until then: every new file must have a tiers.json entry before the PR merges — no exceptions. Category D (T3 dev) still requires an entry.
