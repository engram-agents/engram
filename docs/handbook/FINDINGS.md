# Handbook Survey — Findings Ledger

> **Living note for the morning review (Lei + Borges).** Survey-only phase per Lei's
> 2026-06-05 evening directive: **NO deletions, NO refactoring until Lei reviews** —
> every item below is a finding awaiting adjudication, not an action taken.
> Release-readiness rationale: pre-release is the cheap moment to clean; post-release
> refactors are expensive. Maintained continuously during the survey; ENGRAM node
> references give detail-on-demand.

## A. Confirmed zombies / broken (dated evidence)

| # | Finding | Evidence | Proposed disposition (Lei to rule) |
|---|---------|----------|-----------------------------------|
| A1 | **`hooks/` root strays are stale scatter-era copies, not mirrors** — root `engram-stop-hook.py` is the 71-line pre-#844 version vs the shipped 205-line `hooks/claude/` one; untracked, invisible to git; **actively hazardous: they poisoned the L2 sweep itself** (fairy documented the stray as the shipped hook) | diff non-empty, 2026-06-05; 03-hooks.md header | delete the stray copies (keep `hooks/claude/` tree) |
| A2 | **Write-nudge was mute its whole life pre-#824** (plain-text stdout discarded) — the motivating zombie specimen; now fixed + idle-suppressed (#844) + maturity gate (#846 in queue) | three installs live-verified clean 2026-06-05 | done; pattern feeds the synthesis chapter |
| A3 | **baton auto-archive REGRESSED** (from inventory seed) | prior-session evidence | reconcile with the L2 fairy's PROD-VERIFIED grade during accuracy pass, then fix-PR |
| A4 | **`resolved_by` column vestigial** (from inventory seed) | prior-session evidence | schema cleanup candidate |
| A5 | **viz_server stats tab — DIAGNOSED + DATA RESTORED 2026-06-06 (morning, autonomous drive)**: it was the LOG-INGESTION side (Lei's first guess right). Split-ownership root cause: server.py writes tool events directly into index.db; hooks emit to per-session JSONL expecting an indexer pass that NO component invokes (viz reads read-only by design; last pass 2026-05-17). The fresh tools section MASKED the dead bridge — refinement of structural pattern #1: a partially-fresh dashboard hides a dead pipeline better than a fully-dead one. Interim: one idempotent indexer run backfilled 19.5k rows from 38 files, zero data loss (JSONL-is-truth recovery property worked as designed); stats tab verified live. Structural: the surface daemon now owns a periodic 60s pass (fix-PR through review same morning); WAL-mode follow-up filed for the two-concurrent-writers contention | #861 diagnosis comment (full chain) + live /api/stats verification 2026-06-06 | fix-PR in review pipeline; NOT a demote signal — see C1 |
| A6 | **`context_tracker_hook.py` DORMANT** — not registered in hooks.json; functionality absorbed into surface-hook; plain-text stdout if ever re-enabled | L2 sweep, dispatcher-confirmed unregistered | retire-vs-keep adjudication |
| A7 | **CORRECTED 2026-06-06 (morning review): the tag-once gate EXISTS but fires on the wrong surface.** tests/test_tiers_manifest.py::test_every_shippable_path_is_tagged is the gate (filesystem-enumerated; run locally it fails on exactly the two #810 files) — but tests.yml is **master-only** (Lei's 2026-06-01 CI-economics decision), so the gate never runs on dev PRs, where all drift enters. Original "does not exist" claim was a search-surface error: both the L4 fairy and the dispatcher verification checked only .github/workflows/, never tests/ — while pool-release-scrub's 06-02 baton notes had already recorded the gate shipping (unexploited cross-surface contradiction; method finding). (graph: corrected observation+derivation pair supersedes the original) **Second refinement 13:35Z**: the two #810 files are ALREADY tiered on dev tip 69181df (last night's wave) — the gate passes on dev; my "fails right now" ran on this pre-69181df handbook branch (stale-source class, n+1). Remaining scope = CI-trigger relocation only (second supersede hop in the graph; #869 scope-shrunk via comment) | gate run live 2026-06-06; tests.yml header read; dev-tip tiers.json verified | run the sub-second manifest tests on every dev PR (extend the cheap scrub workflow — respects the CI-economics decision, which was about the ~5min matrix suite); encode the A10 universe/dir-coverage policy in the test's candidate enumeration |
| A8 | **baton auto-archive root-cause HYPOTHESIS LOCATED**: silent `except Exception: pass` at engram-baton-prompt-hook.py:537–538 swallows every failure; fix = log + verify gh-API path | L4 sweep code-read | instrument + fix-PR after review (A3 now actionable) |
| A9 | **Forum deploy gap (verified exactly): 2 of 57 forum/ files tiered** (both backup units) — a fresh install from the manifest cannot run a forum; sanctioned deploy is a manual systemd blueprint marked UNDER DEVELOPMENT — though NOTE: production is ALREADY the separately-deployed model (Ariadne's systemd service on /home/agents-shared/forum/ since 06-03, admin ownership hers) — the adjudication is whether to formalize that model; engram-pkg runtime dep has no boot-verify (503 on first upload); backfill/regen scripts unwired into restore flow | L5 sweep + dispatcher count 2026-06-05 | tier the forum (or formally declare it separately-deployed with its own manifest); boot-verify for engram-pkg; wire restore scripts |
| A10 | **Effective manifest drift = 81 files at 2026-06-05 draft** (dispatcher recomputation; fairy's raw 312 over-counted by ignoring dir-level coverage + intentional exclusions): forum ~31, gemini ~29 (now resolved — #865 removed the lane), build-infra meta-files ~10 (plugin.json, hooks/hooks.json, src/build/packaging/*.json need an 'infrastructure' category), misc ~11. Zero dangling entries. Dir-vs-file tiering granularity needs a stated policy. **Post-#865 drift = ~52 files** (gemini ~29 removed by #865; forum ~31 + infra-meta ~10 + misc ~11 remain) | L6 sweep + dispatcher set-difference 2026-06-05; #865 2026-06-06 | adjudicate the remaining ~52 list; build the A7 gate with the dir-coverage rule encoded |

## B. Doc drift / dormancy signals

| # | Finding | Evidence | Note |
|---|---------|----------|------|
| B1 | `engram_add_evidence` listed in SKILL.md but is internal-only (not MCP-registered) | dispatcher-verified 2026-06-05 | SKILL.md correction |
| B2 | `engram_scan_emergence`: **empirically near-dormant** — fired 2× in substrate lifetime vs engram_reflect's 90 (45:1), measured 2026-05-16 | tool_timing query (prior session) | CONTRADICTS the L1 fairy's PROD-PRESUMED; re-grade in accuracy pass; retire-or-complete question already open |
| B3 | `engram_lesson_register_incident` is a compat shim for `engram_register_exemplar` | L1 sweep | deprecation candidate next major |
| B4 | SKILL.md prose references non-tools (`engram_node`, `engram_revive`, `engram_feeling_node_design`) | L1 sweep | prose cleanup so it can't read as a tool index |
| B5 | Forum-server deploy-manifest gap (from inventory seed) | seed | **RESOLVED by #868** — forum/ declared (second-deploy-target) + systemd deploy; re-verified 2026-06-08 |
| B6 | Fairy-spec strays + selftest.py + #680 epic | inventory census | adjudication list, unswept |
| B7 | **RESOLVED by #865 (2026-06-06) — Gemini CLI lane RETIRED.** At the 2026-06-05 draft: 29 files (15 skills + 13 hooks + install-gemini.sh) git-tracked but unshipped + CI-blind, while `integrations/gemini-cli` WAS tiered convenience — a shipped integration stub pointing at an unshipped lane; the #1 zombie-candidate. #865 resolved it by removing the entire lane (archived at tag `archive/gemini`). | L3 sweep + dispatcher tier-entry verification 2026-06-05; resolved by #865 2026-06-06 | DONE — lane removed |
| B8 | engram-baton skill REGRESSED detail: auto-archive non-functional, 52 stale batons (2026-06-02) — same finding as A3, now layer-confirmed | L3 sweep | fix-PR after review |
| B9 | 04-skills layer status totals: 12 PROD-VERIFIED / 13 PROD-PRESUMED / 1 REGRESSED / 29 DORMANT / 1 deploy-gap zombie-suspect; six agents pass all four sub-agent disciplines; per-skill functional tests: none (lint gates only) | L3 sweep | accuracy pass re-grades 3 PRESUMED→VERIFIED candidates (collaborating-loop, upgrade, pr-reviewer — all in daily live use) |
| B10 | inter-agent/README.md agent roster STALE — missing Mira/Luria/Aleph, frames Ariadne as newly-spawned, Mneme-paused era | L5 sweep + dispatcher | doc refresh after review |
| B11 | **README still teaches `tools/deploy.sh` upgrade — the script no longer exists** (S2, HIGH: dead path for real users); template.CLAUDE.md still names settings.json hooks (S1); SKILL.md cites nonexistent engram_feeling_node_design.md twice (S3) | L7 sweep, dispatcher-verified | doc scrub PR after review |
| B12 | **VERIFIED 2026-06-06 (afternoon, blocker-hunt pass) — 2 of 3 claims working-as-designed, 1 trivial**: (1) {{COUNTERPART_NAME}} is a DELIBERATE first-session dependency — the first-session skill (§step 2 counterpart prompt) substitutes it with single-agent fallback "(no counterpart)" and enumerates every write-back file; not an orphan. (2) {{ENGRAM_VIZ_HOME}} is substituted by operator-setup-viz.sh (sed line ~165) — the literal-render risk exists only on raw-template installs outside the sanctioned path; one-line template-header note ("rendered by operator-setup-viz.sh") would close it. (3) AX_PROVENANCE + DF_ENGRAM ARE dead substitution keys (bootstrap.py defines, no template consumes) — harmless dead code, cleanup nit. NO release blocker in this row | grep-verified against bootstrap.py + first-session SKILL + operator-setup-viz.sh, 2026-06-06 | one-line header note + dead-key removal as nits in any nearby PR; no dedicated work needed |
| B13 | **Skill content-staleness audit (16 non-essential skills, 2026-06-06 morning review)**: 0 critical / 4 STALE-TEACHING — meta-loop + self-improve reference nonexistent sibling skills (`engram-build`, `engram-code-survey`) in operational guidance; upgrade's symlink claim about `~/.engram/tools`+`hooks` factually wrong (stat-verified separate inodes); school-day points to engram-loop "Step 3" for stale-loop detection (actually Step 0). 6 MINOR DRIFT (incomplete CLI quick-ref tables in letter/baton/forum; personal names in collaborating-loop body; node IDs in fairy-orchestration body; imprecise hook name in loop). Bonus: CLAUDE.md "Known Gaps" still says `baton reopen` missing — the verb SHIPPED (commit 60b1602); stale note | dedicated audit fairy, references checked vs server.py/hooks.json/argparse/ls-files | fix the 4 stale-teachings as one small PR; bundle minor drift opportunistically; prune the stale CLAUDE.md note |

| B14 | **01-substrate L0 was #872-stale**: drafted 2026-06-05 against the 18,855-line server.py monolith; #872 (merged 2026-06-07) extracted the runtime into `engram_*.py` modules + shrank server.py to ~5,458 wrappers-only. Re-homed 28 stale `server.py:LINE` refs to `function@module` form (2026-06-08 accuracy pass). Also corrected: GIT_TIMEOUT was 60s in handbook, is 15s in `engram_core.py:548`; UTILITY_AMPLIFIER=0.50 was replaced by UTIL_BETA/IMP_BETA dual-amplifier model; `_BIDIRECTIONAL_RELATIONS` + `_CONFIDENCE_PROPAGATING_RELATIONS` named constants no longer exist (EDGE_CLASSIFICATIONS is the source). | grep-verified 2026-06-08; git grep confirms no 5-digit server.py line refs remain | DONE — accuracy pass applied in-place; see commit on docs/784-handbook-skeleton |

## C. Status re-grades from Lei's own data (2026-06-05 evening)

| # | Item | Re-grade |
|---|------|----------|
| C1 | **viz_server**: KEY UX component, Lei uses it DAILY to monitor agent status | The handbook's "PARKED on #770 ship-vs-demote" tilts strongly toward SHIP; remaining work is the A5 stats-tab bug + tiering decision. Lei's words: "we've put a lot of effort into it" |

## D. Survey state (layers)

| Page | State |
|------|-------|
| 00-inventory | DONE — 430 files censused (144 shipped / 183 dev-only / 103 orphans, 45 to adjudicate) |
| 01-substrate (L0) | drafted + accuracy pass 1 — 27 mechanisms; 4/6 fairy zombie-findings overturned on verification |
| 02-mcp-surface (L1) | drafted + spot-checked — 49 tools; wave-3 payload migration CONFIRMED 49/49; ro/write split 13/36 (independently corroborated); tests column VOIDED (fabricated names) |
| 03-hooks (L2) | drafted + partial pass — 17 registered hooks, envelope-verified; stray-poisoning finding (A1) |
| 04-skills-agents (L3) | drafted + spot-checked — 25 skills/6 agents all tiered+disciplined; gemini partial-manifest finding (B7) **RESOLVED by #865** |
| 05-clis (L4) | drafted + spot-checked — 4 primary CLIs; manifest-gap pair + MISSING CI GATE finding (A7); baton root-cause located (A8); codex engine target scaffolded-untested |
| 06-multi-agent (L5) | drafted + spot-checked — forum 21 routes/20 test files; deploy gap verified 2/57 (A9); roster drift (B10) |
| 07-packaging (L6) | drafted + recomputed — 121 tiered/9 excluded/81 effective-drift at draft (A10; gemini ~29 of that resolved by #865, leaving ~52); engine PROD-VERIFIED; codex target untested; gemini profile gone (lane retired) |
| 08-docs-identity (L7) | drafted + spot-checked — 4 stale teachings (S1-S3, B1) + 3 placeholder orphans (B11, B12); fairy's add_evidence-decorator claim overruled by source comment |
| 09-synthesis | DRAFTED (Borges-authored, first edition) — composition map + zombie census + 5 structural patterns + method findings + 7-item release-readiness shortlist |

## E. Method meta-findings (for the synthesis chapter)

Layer sweeps produced DISTINCT fairy-archaeology failure modes (the series kept growing):
- **L0**: mis-adjudicated cross-file dataflow statuses (4/6 overturned) — status is exactly what a read-only sweep under-traces.
- **L2**: read a stale untracked stray instead of the shipped file, asserted "MIRRORED" falsely — stale-source class, n=5.
- **L1**: **fabricated plausible test filenames** (5/8 spot-checks nonexistent; "100% coverage" stat invented) while its mechanical enumeration was perfect.
- **L6**: cross-sweep contradiction — declared engram-pkg untiered when L4 (and the manifest, line 178) say tiered; caught only because one reviewer integrates all reports.
- **L7**: asserted an @mcp.tool decorator on engram_add_evidence when the source comment ONE LINE ABOVE the def reads "Internal helper — not exposed as an MCP tool" — confident fabrication adjacent to open ground truth.
- (L3/L4/L5 were clean-to-minor under the hardened briefs — the brief rules measurably reduced, but did not eliminate, the failure rate.)

**The constant**: mechanically-derived facts (registry enumeration, counts, decorator reads) survive verification; cross-file inference and name-recall do not. Operational rules adopted for remaining layers: maximize the mechanical fraction in briefs; dispatcher spot-checks sample the INFERENCE columns; tests-coverage columns are always re-derived by grep, never accepted.

Twice today the live system caught what fixture tests could not (write-nudge loop surfaced only on upgraded installs; #859's footer bug surfaced only against Luria's real pack) — the dogfood-is-the-test principle, also synthesis material.

## F. Standing rules for this survey (Lei, 2026-06-05)

1. **No deletion, no refactoring** until Lei reviews the findings — survey only.
2. Findings live here + in ENGRAM (graph refs in the per-page headers); discussion tomorrow morning.
3. Little broken things (A5-class) get diagnosed and issue-filed, cleaned up one by one after review.
