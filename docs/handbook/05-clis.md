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
**state surfaces** — R: packaging/tiers.json, packaging/platforms/*.json; W: build/plugin/.
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
