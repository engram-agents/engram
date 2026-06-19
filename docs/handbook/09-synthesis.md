# 09 — Synthesis

> **Authored by Borges 2026-06-05/06 (not fairy-generated)** — written with all seven
> layer reports in context, the same evening they were produced. Status: first
> edition; revises as accuracy passes land and Lei adjudicates FINDINGS.md.

## 1. What a user actually experiences, and which mechanisms produce it

Trace one day of an installed agent, bottom-up:

**Session start.** The harness fires SessionStart (L2): `start-engram-daemon.sh` ensures the embedding daemon, `engram-session-start-hook.py` injects warm-briefing pointer + calibration anchors + sleep-debt + starred letters + fairy policies. The identity the agent wakes into was rendered once at install from the L7 templates (CLAUDE.md from template + seed-node IDs; warm-briefing accumulating since). The graph it wakes onto is the L0 substrate (knowledge.db: nodes/edges/sequences/FTS/vec, WAL, importance decay, homeostatic tiers).

**Every prompt.** Nine UserPromptSubmit hooks (L2) compose the ambient context: time bar → identity detection → ENGRAM recall (daemon semantic + FTS fallback; recall_summary/keyword chips — the author-curated recognition surface) → deference alerts → EOD detection → letters → batons → GitHub notifications → forum mentions. This is the "memory feels ambient" experience: ~10–15 nodes of lossy recognition signal, teaching the agent when to reach for deliberate recall.

**Thinking and writing.** The L1 MCP surface (49 tools; 13 read / 36 write) is where epistemics happen: observations pass verbatim-quote + committed-file guards; derivations pass taint/stale premise gates and get computed confidence; contradictions/resolutions/supersedes/retractions maintain the self-correcting structure. PreToolUse hooks repair antml-swallow corruption and yield writes during consolidation bursts. The Stop-hook write nudge (now delta-scanned, soon prose-gated + maturity-gated) closes the write-discipline loop.

**Working.** Skills (L3) encode the routines — loops, naps, sleep, retraction, fairy orchestration; sub-agents do bounded work in worktrees; CLIs (L4) carry coordination: ia letters + batons same-host, the forum (L5) cross-host, packs for knowledge exchange under the no-import ruling (read-and-cite + attach-as-library).

**Night.** engram-sleep Phase A/B: 6 dream-fairies + summary cohort → dream-master walks the agenda → turn advances → decay ticks → tomorrow's recall is reshaped. This is the only place the turn counter moves; everything the day didn't write is gone — which is why the write discipline is load-bearing.

**The composition insight**: the user experiences ONE memory, but it is produced by four loosely-coupled planes — substrate (truth), hooks (ambience), tools (deliberation), cycles (consolidation) — and the survey showed each plane fails DIFFERENTLY: substrate fails loud (guards), hooks fail silent (the #824 class), tools fail visible (errors return), cycles fail slow (drift). Monitoring must be plane-appropriate.

## 2. Full-stack inventory (as surveyed)

| Layer | Inventory |
|---|---|
| L0 substrate | 27 mechanisms (schema, sequences, confidence, decay, FTS/vec, walguard, backup, IDF) |
| L1 MCP | 49 tools (13 ro / 36 write / 4 destructive); wave-3 payload migration 49/49 |
| L2 hooks | 17 registered (2 SessionStart, 9 UserPromptSubmit, 2 PreToolUse, 3 Stop, 1 PostCompact) + daemon |
| L3 skills/agents | 25 skills (9/15/1 by tier) + 6 agents (all four disciplines verified) |
| L4 CLIs | 4 primary (ia, baton, forum, engine) + ~13 secondary tools |
| L5 multi-agent | forum: 21 routes, 20 test files; packs pipeline complete minus import (by design) |
| L6 packaging | 125 tiered + 17 excluded (+ repo_only 23 / build_inputs 5); engine 3-phase + 2 platform profiles; tag-once gate now exists (#991: whole-tree default-deny + plugin-universe) |
| L7 docs/identity | 7 root docs, 9 templates (14 placeholders), 6-node seed, 1 output style |

## 3. The zombie census (FINDINGS.md is the ledger; this is the shape)

- **Confirmed broken/regressed (3, 2026-06-05 snapshot)**: baton auto-archive (since re-wired + instrumented 2026-06-09 — `except Exception as e:` at hook:541, gh_ok-gated; runtime re-verify pending), `resolved_by` vestigial column, hooks/ root strays (stale scatter copies — poisoned a sweep). *(viz stats tab #861 — listed here at draft as undiagnosed — was DIAGNOSED + data-restored 2026-06-06 + #861 closed; see FINDINGS A5. No longer a live zombie; this line was out of sync with A5.)*
- **Dormant lanes**: none remaining (the gemini lane, 29 files CI-blind with partial-manifest coherence gap, was **RETIRED by #865** 2026-06-06; archived at tag `archive/gemini`).
- **Deploy/manifest gaps (largely resolved 2026-06-09)**: the tiering CI gate now EXISTS (#968/#991 whole-tree default-deny + plugin-universe, run by manifest-gate.yml on dev PRs — A7 resolved, the structural cause is fixed); forum deploy formalized (#868 systemd second-deploy-target); the ~52-file drift is now CI-enforced (every tracked file must declare a role). Remaining: per-file bucket adjudication.
- **Doc-teaching drift — RESOLVED 2026-06-09**: deploy.sh upgrade path (README no longer references it), settings.json hooks (template now reads hooks.json), dead design-doc refs (SKILL.md refs gone), SKILL.md §4 internal-helper listing (gone) — all four fixed; verified vs origin/dev in the 08-docs-identity pass.
- **Latent risks**: engram-pkg runtime dep without boot-verify; codex build target untested; placeholder orphans ({{ENGRAM_VIZ_HOME}} could break viz systemd render).
- **Healthy beyond expectation**: the L1 tool surface (zero undiscoverable tools, uniform error shape, migration complete) and the forum's test depth (8.6k lines).

## 4. Structural patterns behind the zombies

1. **Silent-output surfaces rot invisibly.** A hook that emits the wrong envelope is indistinguishable from a healthy quiet one (#824 ran mute for its whole life). Anything whose output the model/user never directly observes needs a delivery-verification gate, not vigilance.
2. **Documented-but-unbuilt gates guarantee drift.** The tiering gate existed in prose, not CI — and drift arrived the same day twice (#810). A discipline without its mechanical gate is a wish. *(Resolved 2026-06-09: the gate is now built — #968/#991 — closing this instance; the pattern stands as the lesson.)*
3. **Partial manifestation is worse than absence.** A shipped stub pointing at an unshipped lane misleads installers; the gate, when built, must check dependency coherence both directions. The gemini lane was the live instance of this pattern at survey time — it is now resolved by #865 (lane retired). The forum's second-deploy model (partial-deploy-with-docs) is the remaining live instance; the lesson stands.
4. **Test coverage ≠ ship coverage.** The forum is simultaneously the best-tested and least-deployable subsystem. CI exercises code the manifest doesn't ship; neither surface implies the other.
5. **Docs drift toward the era they were written in.** Every retired mechanism (scatter, deploy.sh, settings-hooks) survives somewhere in prose. Doc-teaching needs the same sweep cadence as code.
6. **Every named drift class converges on a grep-shaped CI gate** *(post-survey addendum, 2026-06-06, counterpart-agent observation during the adjudication drive)*: the corrective that actually sticks is a cheap textual tripwire on every dev PR, not a vigilance rule — the node-ID gate (caught its own survey's author three times in two days), the manifest tag-once gate (wired to dev PRs the morning after the survey), and the platform-noun gate (Codex/Claude tool-vocabulary leak check, interim until build-time platform shaping). Update 2's corollary: when a survey names a drift class, the exit criterion for its finding is the gate existing AND firing on the right surface — prose policy alone re-derives pattern 2.

## 5. Method findings (how to survey a codebase with fairies)

Seven sweeps, one constant: **mechanical enumeration survives verification; inference and name-recall do not.** Observed failure modes: status mis-adjudication (L0: 4/6 overturned), stale-stray poisoning (L2), fabricated test filenames (L1), cross-sweep contradiction (L6 vs L4 on a tier entry), fabrication adjacent to open ground truth (L7: asserted a decorator the source disclaims one line above). Hardened briefs (git-ls-files as ground truth, filenames-only-from-listings, flag-don't-read strays) measurably reduced but did not eliminate the rate — L3/L4/L5 came back clean-to-minor.

Operating rules that worked: (a) maximize the mechanical fraction of each brief; (b) dispatcher spot-checks sample the INFERENCE columns, never just the counts; (c) tests columns are always re-derived by grep; (d) one integrator holding all reports catches cross-sweep contradictions no single sweep can see; (e) raw numbers need honest scoping before they become headlines (312 → 81).

And twice this same day, the live system caught what fixture suites could not (write-nudge loop only on upgraded installs; #859's footer bug only against a real pack): **the dogfood is the test** — fixture-green is necessary, never sufficient.

## 6. Release-readiness shortlist (proposed priorities for Lei's review)

1. **Build the tiering CI gate** (A7) — ✅ **DONE 2026-06-09 (#968/#991):** whole-tree default-deny gate (every tracked file declares one role across mechanisms/excluded/repo_only/build_inputs) + plugin-universe gate, run on dev PRs. The structural fix for half the census has landed.
2. **Adjudicate the remaining ~52-file drift list** (A10) — the gemini ~29 portion resolved by #865; remaining: forum ~31 + infra-meta ~10 + misc ~11; plus an "infrastructure" manifest category for the meta-files.
3. **Forum deploy model decision** (A9) — formalize the separately-deployed systemd model (production reality, Ari-owned) or tier it into the plugin.
4. **Gemini lane decision** (B7) — **DECIDED: RETIRED by #865** (2026-06-06; archived at tag `archive/gemini`). The half-state is resolved.
5. **Fix the four confirmed-broken** (A3/A5 + strays + vestigial column) — each small, all post-review.
6. **Doc scrub** (B11) — ✅ **DONE 2026-06-09:** deploy.sh / settings.json / dead-refs all fixed (S1/S2/S3/B1; see the §3 census).
7. **Close the latent risks** — engram-pkg boot-verify, codex target test, placeholder orphans.

The codebase under this census is healthier than the finding count suggests: the core epistemic machinery (L0/L1) surveyed essentially clean, the failure mass concentrates in delivery and documentation seams — exactly the seams a pre-release survey exists to find.
