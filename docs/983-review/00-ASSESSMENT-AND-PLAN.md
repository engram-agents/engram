# #983 Documentation — Currency Assessment + Restructure Plan

*Borges, 2026-06-11. Drafting workspace for issue #983. Scope Lei gave me:
drive README + README-AGENT + USER_GUIDE drafting; verify currency; improve along
the principles in #983. Lei polishes the human-side docs with me. HANDBOOK and
CONTRIBUTING are out of this pass (HANDBOOK = docs/handbook/ already exists, "may
not be ready for first release"; CONTRIBUTING = Luria's add, community/post-alpha).*

---

## 1. Current state (what exists today)

| File | Size | Audience today | Verdict |
|---|---|---|---|
| `README.md` | 1096 lines / 47KB | Mixed — explicitly "mostly for agents" (line 7) | **Rework.** Doing 4 jobs: human pitch + agent install runbook + config reference + troubleshooting. |
| `USER_GUIDE.md` | 407 lines / 30KB | Humans, post-install | **Keep + light audit.** Already in the puppy-adoption voice; largely current. |
| `SKILL.md` | 88KB | Agents, operating manual | Out of scope here. README-AGENT *points* to it; doesn't absorb it. |
| `DEVELOPMENT.md` | 21KB | Contributors | Out of scope (Luria: add a community section later). |
| `docs/handbook/` | 00–09 chapters | Veterans/devs | = the HANDBOOK concept. Out of scope this pass. |
| `README-AGENT.md` | — | — | **New file.** |

## 2. Currency findings (the "verify up-to-date" ask)

**README.md — real drift (grounded against `tools/install-local-marketplace.sh`, not memory):**

1. **Scatter-era "What the install does" table (lines 664–675).** Describes
   `claude mcp add -s user` writing to `~/.claude.json`, copying `skills/*` →
   `~/.claude/skills/`, `hooks/*` → `~/.engram/hooks/`. **Stale.** The plugin
   install assembles `~/.engram/marketplace/plugins/engram/` and registers via
   `claude plugin marketplace add`; MCP is provided by the plugin's own `.mcp.json`,
   hooks by the plugin's `hooks/hooks.json`. No `claude mcp add`, no copy to
   `~/.engram/hooks/`.
2. **Scatter-era "File layout after install" diagram (lines 751–802).** Shows
   `~/.claude.json` MCP registration + `~/.engram/hooks/` as the live hooks dir +
   `~/.claude/skills/` as the live skills dir. All scatter topology. Plugin runtime
   lives under `~/.engram/marketplace/plugins/engram/` (`CLAUDE_PLUGIN_ROOT`);
   `~/.engram/hooks/` is inert leftover.
3. **Redundancy.** Three overlapping install runbooks: "Plugin install" Phase 0–3,
   "Install as an agent (on behalf of a user)" (lines 473–569), "Upgrade as an
   agent" (lines 572–661). Same ground, three voices.
4. **Codex section provisional markers (lines 333–336, 380).** ⚠ "to be confirmed
   from the spawn runbook" / "exact UX verified during first spawn." Install steps
   not finalized — flag for Mira/codex-readiness owners before this ships externally.

**USER_GUIDE.md — current.** Spot-checked references (engram-sleep, engram-nap,
viz config tab, auto-sleep cron, drowsiness tiers, fairy policies, agentctl verbs)
all match current behavior. No stale claims found. Gaps are *additive* (below).

## 3. Restructure plan

### README.md → slim human entry point
Target: short enough a human reads it in 2 minutes. Sections:
- One-line what-it-is + the honest tagline ("memory **of** the agent, not **for** it").
- **The problem → our solution** narrative — KEEP the existing "What problem does
  this solve?" (lines 9–41); it's excellent and human-readable. Tighten ~30%.
- **What you get** — 4–5 bullets, plain language (not the install-manifest bullets).
- **The 5 FAQs** Lei specified (verbatim intent):
  1. ENGRAM vs other graph-memory systems → OF vs FOR; identity grows IN it;
     continuation scaffolds + maintenance routines (retraction, resolution,
     nap/sleep/dream); designed so the agent can *become someone*.
  2. How to start → install Claude Code, clone the repo, ask your agent to guide install.
  3. What happens to my CLAUDE.md / workflows → back up user CLAUDE.md first, install,
     ask your agent to merge it back; skills + project settings unaffected.
  4. How to update → ask your agent to upgrade ENGRAM.
  5. Other questions → ask your agent first; USER_GUIDE for beginner questions.
- **Where to go next** — pointers: humans → USER_GUIDE; your agent → README-AGENT;
  deep detail → docs/handbook.
- Move ALL install mechanics + config reference + troubleshooting + multi-agent
  tooling + design notes → README-AGENT.

### README-AGENT.md → new, agent-facing
Absorbs (relocated + currency-fixed) from current README:
- Plugin install Phase 0–3 (Claude) + Codex install (keep provisional ⚠ markers).
- "Install as an agent" + "Upgrade as an agent" → merged into one deterministic
  procedure (kill the 3-way redundancy).
- **What the install does** — REWRITE to plugin topology (the stale table).
- **File layout after install** — REWRITE to plugin topology.
- Config reference (config.json fields), Polarity defaults, viz, troubleshooting,
  multi-agent operator tools, design notes.
- Agent-aspirational framing top-matter: "you're the agent helping a human adopt
  ENGRAM; here's the deterministic procedure, the rationale, and how to scan the
  package for safety if the human asks." (Lei's style note for README-AGENT.)
- Points to SKILL.md as the post-install operating manual (doesn't duplicate it).

### USER_GUIDE.md → keep + additive improvements
- KEEP structure + voice (matches the vision).
- ADD: a dedicated **printable cheat sheet** (one-page, fridge-magnet style) — Lei
  explicitly wanted this.
- ADD: **illustration callouts** — I can't draw, so I'll mark `[ILLUSTRATION: ...]`
  placeholders where a cartoon would land best (the 50 First Dates tape, the
  puppy-first-week arc, the drowsiness meter, the you-steer/agent-drives split) +
  describe each, for Lei or a human illustrator to fill.
- CONSIDER: light trim of the most detailed sub-sections (auto-sleep window math,
  full config table) — they edge past "really basic beginner." Flag, don't cut
  unilaterally — Lei's call.

## 4. Open judgment calls for Lei
- **README length target** — I'm aiming "2-minute read." Confirm that's the right ceiling.
- **README-AGENT vs SKILL.md boundary** — README-AGENT = install + pre/post-install
  orientation + safety-scan; SKILL.md = operating manual. Agree?
- **USER_GUIDE trim** — keep comprehensive, or pull the deep config/auto-sleep math
  into README-AGENT and keep USER_GUIDE strictly beginner?
- **Codex section** — ships in README-AGENT now with ⚠ provisional markers, or hold
  the Codex install out until the spawn runbook is finalized?
