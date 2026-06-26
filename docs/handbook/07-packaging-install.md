# 07 — Packaging + Install (L6)

> **STATUS: DRAFT — fairy archaeology + dispatcher recomputation 2026-06-05.**
> **UPDATE 2026-06-09 — whole-tree default-deny tag-once gate MERGED (#968/#991):** `test_whole_tree_every_tracked_file_has_a_role` (tests/test_tiers_manifest.py) now enumerates ALL git-tracked files (`git ls-files`) and requires each to have EXACTLY ONE declared role across four manifest buckets — `mechanisms[]` (tiered shippable), `excluded[]` (in a candidate dir but intentionally not shipped), `repo_only[]` (tracked but not a shippable artifact — tests/CI/docs/root dev files), `build_inputs[]` (consumed by build/install — .mcp.json, platform.json, manifests); none → FAIL (unaccounted), more-than-one → FAIL (ambiguous). The narrower `test_every_shippable_path_is_tagged` (plugin-universe gate — build-plugin.sh source dirs, mechanisms-or-excluded) is kept alongside it as the authoritative shippable-universe check. This is default-deny, mechanically resolving the dir-level-vs-file-level policy question below. Counts refreshed below.
> **Dispatcher corrections (two):**
> 1. The fairy's "tools/engram-pkg is NOT tiered" is WRONG — `tools/engram-pkg` is
>    tiered at tiers.json line 178 (the L4 sweep had it right; cross-sweep
>    contradiction resolved by direct grep). Fairy-error series instance: another
>    inference-column miss inside otherwise-solid mechanical work.
> 2. The raw "312 uncovered files" headline over-counts. Dispatcher recomputation
>    (excluding tests/, docs/, .github/, .claude/, root meta-docs, and crediting
>    directory-level tier entries for their children): **81 effective uncovered
>    files**. Composition: forum app ~31 (modules, templates, seeds, spec,
>    requirements, deploy scripts beyond the 2 tiered backup units) · gemini lane
>    ~29 (**RETIRED by #865** — resolved) · build-infrastructure meta-files
>    (~10: plugin.json, hooks/hooks.json, src/build/packaging/*.json incl. tiers.json itself,
>    platform profiles — consumed BY the engine; need an explicit "infrastructure"
>    manifest category or exclusion-with-reason) · misc (~11: fairy-spec strays,
>    selftest.py, forum util scripts, inter-agent design docs, repo meta).
> The dir-level-tiering granularity question (are children of a tiered directory
> canonically covered?) needs a stated policy — dispatcher recomputation assumed YES;
> the fairy's 312 assumed NO.

# ENGRAM Packaging + Install Layer

## Manifest (src/build/packaging/tiers.json, schema v1)

125 tiered mechanisms (62 essential / 50 convenience / 13 dev; cumulative ⊂-model) + 12 multi_agent-flagged + 4 identity-coupled (dropped for `--identity foreign`) + 17 excluded-with-reason (persona tools, operator tools, superseded hook, CI test files). Plus the two whole-tree buckets #991 added: `repo_only[]` (23 — tracked non-shippables: tests/CI/docs/root) + `build_inputs[]` (5 — build/install inputs). **Zero dangling entries** (all tiered paths exist). Coverage: now whole-tree default-deny — every git-tracked file must carry exactly one declared role (see §CI gates).

## Build engine (tools/engine/ + build-plugin.sh wrapper)

Phase 1 (manifest load → tier+flag filter → copy to build/plugin/) · Phase 2 (hooks.json generation, reference resolution) · Phase 3 (platform profiles, --target claude-code|codex, --identity self|foreign with leak-scan). Files: manifest.py (load/validate/resolve: ships(m) := rank(tier) ≤ rank(chosen) AND (¬multi_agent OR chosen)) · build.py (transforms + codex emitters) · steps.py (convergent step-DAG for flows) · cli.py (build/plan/run/doctor). PROD-VERIFIED: graduated via golden-equivalence (#793), legacy bash retired. Codex target: implemented, **untested** (no codex-target test — L4 finding).

## Platform profiles (src/build/packaging/platforms/)

claude-code.json (identity_doc CLAUDE.md, .mcp.json, hook envelope text-ok) · codex.json (AGENTS.md, config-toml-block, strict-json envelopes, min 0.136.0). Loaded at Phase 3, baked into bundle as platform.json. No gemini profile (the lane was RETIRED by #865; Claude/Codex are the two active targets).

## Installers

- **install-local-marketplace.sh** — builds (if needed) + assembles `$ENGRAM_HOME/marketplace/` + registers; reads config.json {install_tier, multi_agent} (the #704/#707 silent-misbuild fixes). PROD-VERIFIED (used for every upgrade incl. today's).
- **migrate-to-plugin.sh** — one-time scatter→plugin: preflight → backup-first → plugin surfaces (+PAUSE /plugin install) → scatter removal → knowledge.db integrity assert → (+PAUSE MCP restart) → --verify. Data allowlist NEVER deleted. #713 idempotency fixes in. PROD-VERIFIED (4 installs migrated).
- **install-gemini.sh** — RETIRED by #865 (removed from tree 2026-06-06).
- tests/install/run_install_test.sh — de-personalization grep gate on shipped agent specs.

## Templates (templates/)

10 shipped+tiered: template.CLAUDE.md (essential; placeholder-rendered identity floor) + multi-agent variant · template.warm-briefing.md · config.json.example (schema v2) · template.AGENTS.md (codex) · compact-instructions · 3 systemd units (surface-daemon, viz-user, viz) · agents.json.example. (3 templates for the external-cron heartbeat system were retired by #957 — see §6.2 of 03-hooks.md.) Templates verified current — no retired-scatter references found (the #783/#787 settings.hooks.json.template defect class is resolved; that file is gone). The 3 gemini-cli templates (integrations/gemini-cli/) were RETIRED by #865 alongside the lane.

## CI gates (.github/workflows/)

- **tests.yml** — pytest matrix 3.11/3.12 over tests/ + forum/tests/ (ENGRAM_NO_EMBEDDINGS=1). Runs on master push/PR + dispatch. *(Dispatcher note: forum's tests ARE in CI even though forum isn't in the manifest — test coverage and ship coverage diverge, the L5 paradox.)*
- **check-no-new-shipped-node-ids.yml** — blocks concrete node-ID shapes in shipped paths (tests/ etc. excluded). Live (fired twice today).
- ~~MISSING: the tiers gate (A7)~~ **CORRECTED 2026-06-06 (see FINDINGS A7):** the tag-once + dangling gates EXIST (tests/test_tiers_manifest.py) — this page's "missing" claim repeated the same search-surface error (looked only in workflows/). The actual defect was trigger placement (master-only suite); **resolved same day**: manifest-gate.yml now runs the sub-second manifest tests on every dev PR (the A7 follow-through PR, merged). **Extended 2026-06-09 (#968/#991, merged):** a second, broader gate landed — `test_whole_tree_every_tracked_file_has_a_role` enumerates ALL git-tracked files and fails unless each has exactly one role across four buckets (`mechanisms`/`excluded`/`repo_only`/`build_inputs`); default-deny. The original `test_every_shippable_path_is_tagged` (plugin-universe — build-plugin.sh source dirs, mechanisms-or-excluded) is kept as the authoritative shippable-universe check. Two complementary scopes: plugin-universe catches untiered shippables; whole-tree additionally enforces a declared role for every tracked file (removed lanes are deleted, not bucketed — #865 precedent). Still genuinely absent: platform-profile build validation + a foreign-leak-scan CI gate — candidates for the platform-noun gate family (see the build-time platform-shaping thread).

## Zombie ranking (layer)

| Rank | Suspect | Status |
|---|---|---|
| 1 | Effective drift backlog (forum ~31 + infra-meta ~10 + misc ~11; gemini RETIRED #865) | now CI-enforced — whole-tree gate (#991) fails on any tracked file with no declared role; remaining = adjudicate the right bucket (tier/exclude/repo_only/build_input) per file |
| 2 | Tiers CI gate (the structural cause) | **RESOLVED** — whole-tree tag-once gate merged 2026-06-09 (#968/#991); was master-only (A7), now dev-PR + whole-tree |
| 3 | gemini lane | **RETIRED by #865** (2026-06-06; archived at tag `archive/gemini`) |
| 4 | forum/fairy-spec-*.md (5) + inter-agent design docs (2) | PARKED — archive candidates |
| 5 | Dir-level vs file-level tiering | **RESOLVED** by the whole-tree gate — "every tracked file → exactly one of four declared roles" is the mechanical policy (#991) |
