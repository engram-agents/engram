# ENGRAM Technical Handbook

*The mechanism registry: what exists, how it works, whether it actually runs. One page per **mechanism** (mechanisms cross files), bottom-up from the complete file inventory, finishing in a top-level synthesis of how the layers compose into the product. EPIC #784.*

## Why this exists

Three needs, one artifact (Lei, 2026-06-04 + 2026-06-05):

1. **Implemented ≠ shipped ≠ alive.** The motivating specimens are real: the utility-credit hook was implemented, merged, and silently dropped from shipping (#783); the Stop-hook write nudge **shipped and ran for its entire life without ever being delivered** — plain-text stdout the harness discarded — until #824's envelope fix unmuted it (2026-06-05) and a missing idle-suppression surfaced within hours (#840/#844). Neither maintainer nor agent could answer "is this in prod?" from memory, and in both cases the truth was a state nobody guessed. **The handbook's job is to make that question mechanically answerable for every mechanism.**
2. **Secondary source of truth** for the paper's technical chapter.
3. **Platform-treatment map** — grounded per-mechanism shipping decisions for the cross-host release model (#779).

## Method (bottom-up, per Lei 2026-06-05)

1. **Census** (`00-inventory.md`, generated): every file in the repo → ship status per `src/build/packaging/tiers.json` → mechanism assignment. Nothing escapes the table.
2. **Sweep**: file by file, layer by layer. Each file gets assigned to a mechanism; each mechanism gets a page with **verified** status — run it or trace its delivery path to ground truth, never status-from-memory. First-draft archaeology is fairy-friendly; accuracy review is Borges + Lei.
3. **Zombie hunt**: for every mechanism, answer the #824 question explicitly — *"if this were silently broken, what would we observe, and have we observed its absence?"* A mechanism whose output is never independently observed is `zombie-suspect` by default until verified.
4. **Synthesis** (`09-synthesis.md`, written last): how the layers compose into the current user experience.

## Status taxonomy

| status | meaning |
|---|---|
| `prod-verified` | ships + behavior verified against ground truth, **with date and evidence** |
| `prod-presumed` | ships, believed live, not yet verified this audit |
| `dormant` | ships, inert by design or config (not a defect) |
| `zombie-suspect` | ships, *believed* live, but delivery/output is unobserved — the #824 class |
| `regressed` | was live, demonstrably broken now |
| `parked` | implemented, deliberately not shipped |
| `retired` | removed or superseded; kept only as history |
| `dev-only` | repo infrastructure; never ships |

Verification recency matters — operationalized: a `prod-verified` decays to `prod-presumed` when any file listed on its mechanism page has commits newer than the verification date (mechanical check: `git log --since=<verify-date> -- <files>` non-empty → decay). Re-verify to restore.

## Layer map (page files)

| layer | page | contents |
|---|---|---|
| L0 | `01-substrate.md` | knowledge.db schema, server core, runtime modules (backup, walguard, filters, IDF), git layer |
| L1 | `02-mcp-surface.md` | every MCP tool: contract, guards, write-path discipline |
| L2 | `03-hooks.md` | all hooks, per platform: delivery path **verified**, envelope contract, suppression logic |
| L3 | `04-skills-agents.md` | skills + sub-agent specs: activation surfaces, platform variants |
| L4 | `05-clis.md` | agent-facing CLIs: ia, baton, forum, agentctl, engram-pkg, engine |
| L5 | `06-multi-agent.md` | forum server (out-of-manifest deploy!), inter-agent dir, packs, monitors |
| L6 | `07-packaging-install.md` | build engine, tiers manifest, installers, templates, platform profiles, CI gates |
| L7 | `08-docs-identity.md` | identity templates, docs surfaces, seed content |
| — | `09-synthesis.md` | the top-level composition: what a user actually experiences and which mechanisms produce it |

## Known zombie-suspects at skeleton time (seeded from today's audit)

| suspect | evidence | adjudication |
|---|---|---|
| `hooks/gemini/*` + `skills/gemini/*` + `install-gemini.sh` + `integrations/gemini-cli/*` (34 files) | Gemini lane dormant since the Mnemosyne era; #788 lint found defect classes there that CI is blind to | **RETIRED by #865** (2026-06-06; archived at tag `archive/gemini`) |
| baton auto-archive (#500) | closed as done; empirically non-functional (52 stale batons found 2026-06-02) | **regressed — confirmed** |
| `resolved_by` node column | 0/112 resolved questions populated it (point-in-time SQL census of the author's live graph, 2026-06-03 dream cycle; re-derive: `SELECT count(*) FROM nodes WHERE type='question' AND status='resolved' AND resolved_by IS NOT NULL`); resolution lives on `resolves` edges | **dormant-vestigial — confirmed** |
| forum server deploy path | was outside tiers.json at the 2026-06-05 draft (blind spot bit live: validator-dep → 500s) | **RESOLVED by #868** — forum/ declared (second-deploy-target) + systemd deploy added; re-verified 2026-06-08 |
| `forum/fairy-spec-*.md` | build-era scaffolding left in tree | pending — archive candidates |
| `selftest.py` | root-level, unmapped, unknown last run | pending |
| #680 codex-adapter epic | superseded by the engine `--target codex` path (#791/#803)? | pending — close-or-rescope |

## Conventions per page

Each mechanism page section: **what** (plain language, one paragraph, for humans) · **how** (terse spec, for agents) · **status** (from taxonomy, dated, with the verifying evidence) · **files** · **key constants** · **tests** · **tier / platform strategy** · **ENGRAM provenance** (design-decision node IDs, Borges-graph) · **paper anchor**.
