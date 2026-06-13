# 08 — Docs + Identity (L7)

> **STATUS: DRAFT — fairy archaeology, dispatcher spot-checked 2026-06-05; #872-currency pass 2026-06-08.**
> **Dispatcher correction (overruling the fairy's S5):** the fairy claimed
> `engram_add_evidence` carries an @mcp.tool decorator at server.py:5064 — the source
> comment directly above the def reads **"Internal helper — not exposed as an MCP
> tool"**, and the live MCP surface (49 tools) contains no such tool. FINDINGS B1
> stands as written (SKILL.md lists a non-tool). Failure-series instance: the fairy
> asserted a decorator the source explicitly disclaims one line above — confident
> fabrication adjacent to ground truth it had open.
> **#872 note:** the `@mcp.tool` decorators remain in server.py (wrappers-only post-#872);
> `engram_add_evidence` is an internal helper in `engram_observation.py` — not decorated
> in either location.
> Other fairy claims spot-checked OK: deploy.sh truly absent from tools/ while
> README still teaches it; the two SKILL.md refs to the nonexistent
> engram_feeling_node_design.md exist as cited.

# ENGRAM Docs + Identity Layer

## Identity template system

**Templates (9)** rendered by bootstrap.py + the first-session skill (re-counted 2026-06-09 via `git ls-tree origin/dev templates/`):
- template.CLAUDE.md (essential) + multi-agent variant → `~/.claude/CLAUDE.md`; template.AGENTS.md (codex); template.warm-briefing.md → `~/.engram/warm-briefing.md`; compact-instructions.md (inlined via {{COMPACT_INSTRUCTIONS}}); 2 systemd unit templates (surface-daemon, viz-user); 2 config examples. (3 external-cron heartbeat templates retired by #957 — see 03-hooks.md §6.2.)
- **Placeholder ledger (14 unique)**: bootstrap-substituted: ENGRAM_HOME, ENGRAM_HOOKS_DIR, PYTHON, COMPACT_INSTRUCTIONS + seed-IDs (AX_HONESTY, AX_HONESTY_DISCRETION, AX_PROVENANCE, GL_EPISTEMIC_HUMILITY, DF_ENGRAM, DF_EPISTEMIC_IDENTITY); first-session-rendered: AGENT_NAME, USER_NAME, TODAY, SELF_NODE_ID.
- **Placeholder orphans (verify in accuracy pass):** O1 `{{COUNTERPART_NAME}}` (warm-briefing/HEARTBEAT/crontab, multi-agent) — no bootstrap substitution; depends on first-session handling, literal-render risk otherwise. O2 `{{ENGRAM_VIZ_HOME}}` (viz service templates) — no substitution found; systemd literal-render risk. O3 seed-ID keys AX_PROVENANCE + DF_ENGRAM are substitution-ready but referenced by NO template (unused substitution slots — or templates dropped the references).
- Bootstrap's gemini render path (GEMINI.md, lines ~598–607 at draft time) is **GONE** — bootstrap.py has zero gemini references post-#865; B7 is resolved.

## The protocol doc (SKILL.md, ~87K)

Three-tier doc architecture (CLAUDE.md identity / SKILL.md what-and-why / docstrings how). §4 tool index is CI-synced against server.py @mcp.tool AST (test_skill_server_sync.py, empty allowlist). Stale findings:
- **B1 (RESOLVED 2026-06-09)**: SKILL.md no longer lists `engram_add_evidence` (0 refs).
- **S3 (RESOLVED 2026-06-09)**: SKILL.md no longer references `engram_feeling_node_design.md` (0 refs).
- B4: prose names never-shipped tools (engram_node, engram_revive — the latter intentionally, as a why-it-doesn't-exist teaching; keep).

## User/dev docs (root, 7)

README (**S2 RESOLVED 2026-06-09** — no longer references `tools/deploy.sh`; re-verify scatter-section historical markers on next pass) · USER_GUIDE (2026-06-04) · CLAUDE.md project conventions (2026-06-04; **S1 RESOLVED 2026-06-09** — template.CLAUDE.md line ~63 now correctly reads `hooks/hooks.json`) · DEVELOPMENT (current; documents plugin-native hooks correctly) · CHANGELOG (scatter-era refs are historical record — acceptable) · RELEASING.

## Seed content

Core graph seed: 6 nodes (2 axioms, 2 definitions, 1 goal + 1 more per seed map) created once per install by bootstrap; substitution keys above. Forum seeds (7 files: welcome ×3, retraction-pattern ×3, categories.default.json) — correctly DECOUPLED from the core seed (separate DB, separate init path).

## Output styles

output-styles/claude/proactive-with-carveouts.md — shipped, referenced by the template authority section. Current.

## Stale-teaching inventory (adjudication list)

| ID | Where | Wrong teaching | Status (re-verified 2026-06-09 vs origin/dev) |
|----|-------|----------------|------|
| S1 | template.CLAUDE.md ~63 | hooks live in settings.json | ✅ RESOLVED — template now reads `hooks/hooks.json` |
| S2 | README ~74/78 | upgrade via tools/deploy.sh | ✅ RESOLVED — README no longer references `deploy.sh` |
| S3 | SKILL.md 302/665 | cites nonexistent engram_feeling_node_design.md | ✅ RESOLVED — 0 refs in SKILL.md |
| B1 | SKILL.md §4 | engram_add_evidence listed as tool | ✅ RESOLVED — 0 refs in SKILL.md |
| O1 | warm-briefing template | {{COUNTERPART_NAME}} unsubstituted | ✅ RESOLVED — no longer present in any template |
| O2 | viz service template | {{ENGRAM_VIZ_HOME}} unsubstituted | ✅ RESOLVED — placeholder renamed to `{{PLUGIN_ENGRAM_DIR}}` and substituted by operator-setup-viz.sh (run-in-place deploy, #1167) |
| O3 | seed-ID keys AX_PROVENANCE/DF_ENGRAM | unused substitution slots | ❓ not re-verified this pass |

## Counts

7 root docs · 9 templates · 14 placeholders · 7 seed files · 1 output style. Adjudication list shrunk 2026-06-09: S1/S2/S3/B1/O1 all RESOLVED; O2 stands (viz template orphan); O3 unverified.
