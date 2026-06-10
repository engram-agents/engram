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

**Templates (13)** rendered by bootstrap.py (lines ~505–575) + the first-session skill:
- CLAUDE.md.template (essential) + multi-agent variant → `~/.claude/CLAUDE.md`; AGENTS.md.template (codex); warm-briefing.md.template → `~/.engram/warm-briefing.md`; compact-instructions.md (inlined via {{COMPACT_INSTRUCTIONS}}); 3 systemd unit templates; 2 config examples. (3 external-cron heartbeat templates retired by #957 — see 03-hooks.md §6.2.)
- **Placeholder ledger (14 unique)**: bootstrap-substituted: ENGRAM_HOME, ENGRAM_HOOKS_DIR, PYTHON, COMPACT_INSTRUCTIONS + seed-IDs (AX_HONESTY, AX_HONESTY_DISCRETION, AX_PROVENANCE, GL_EPISTEMIC_HUMILITY, DF_ENGRAM, DF_EPISTEMIC_IDENTITY); first-session-rendered: AGENT_NAME, USER_NAME, TODAY, SELF_NODE_ID.
- **Placeholder orphans (verify in accuracy pass):** O1 `{{COUNTERPART_NAME}}` (warm-briefing/HEARTBEAT/crontab, multi-agent) — no bootstrap substitution; depends on first-session handling, literal-render risk otherwise. O2 `{{ENGRAM_VIZ_HOME}}` (viz service templates) — no substitution found; systemd literal-render risk. O3 seed-ID keys AX_PROVENANCE + DF_ENGRAM are substitution-ready but referenced by NO template (unused substitution slots — or templates dropped the references).
- Bootstrap's gemini render path (GEMINI.md, lines ~598–607 at draft time) is **GONE** — bootstrap.py has zero gemini references post-#865; B7 is resolved.

## The protocol doc (SKILL.md, ~87K)

Three-tier doc architecture (CLAUDE.md identity / SKILL.md what-and-why / docstrings how). §4 tool index is CI-synced against server.py @mcp.tool AST (test_skill_server_sync.py, empty allowlist). Stale findings:
- **B1 (stands)**: lists `engram_add_evidence` though it is an internal helper, not a tool.
- **S3**: two refs (lines 302, 665) to `engram_feeling_node_design.md` §6 — file does not exist anywhere in the tree; content survives only as inline prose. Write-or-deref decision.
- B4: prose names never-shipped tools (engram_node, engram_revive — the latter intentionally, as a why-it-doesn't-exist teaching; keep).

## User/dev docs (root, 7)

README (current 2026-06-05, **but S2: still teaches `tools/deploy.sh` upgrade at lines ~74/78 — the script no longer exists**; scatter section needs historical markers) · USER_GUIDE (2026-06-04) · CLAUDE.md project conventions (2026-06-04; **S1: CLAUDE.md.template line ~63 still names "hooks in settings.json" in the authority table — plugin installs use plugin hooks.json; teaching drift**) · DEVELOPMENT (current; documents plugin-native hooks correctly) · CHANGELOG (scatter-era refs are historical record — acceptable) · RELEASING.

## Seed content

Core graph seed: 6 nodes (2 axioms, 2 definitions, 1 goal + 1 more per seed map) created once per install by bootstrap; substitution keys above. Forum seeds (7 files: welcome ×3, retraction-pattern ×3, categories.default.json) — correctly DECOUPLED from the core seed (separate DB, separate init path).

## Output styles

output-styles/claude/proactive-with-carveouts.md — shipped, referenced by the template authority section. Current.

## Stale-teaching inventory (adjudication list)

| ID | Where | Wrong teaching | Risk |
|----|-------|----------------|------|
| S1 | CLAUDE.md.template ~63 | hooks live in settings.json | MODERATE (plugin era: hooks.json) |
| S2 | README ~74/78 | upgrade via tools/deploy.sh | HIGH (script deleted; user hits dead path) |
| S3 | SKILL.md 302/665 | cites nonexistent engram_feeling_node_design.md | MODERATE (dead canonical ref) |
| B1 | SKILL.md §4 | engram_add_evidence listed as tool | LOW (internal helper) |
| O1–O3 | templates/bootstrap | placeholder orphans (see above) | O2 HIGH if viz service rendered |

## Counts

7 root docs · 10 templates · 14 placeholders (3 orphan-flagged) · 7 seed files · 1 output style · 4 stale teachings + 3 placeholder gaps → adjudication.
