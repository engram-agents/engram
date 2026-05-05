# Sub-agent forensic findings — 2026-05-05

Discoveries from dispatching a forensic introspection sub-agent to report verbatim on its own context-loading state. Driven by Lei's question (2026-05-05 ~09:50 EDT): does Claude Code's user-level CLAUDE.md leak the parent's identity into sub-agent context? If yes, sub-agents may confuse themselves about who they are — Lei's WuKong-hair analogy: they should be scoped shadow-derivatives, not Borges himself.

## Finding 1 — CLAUDE.md identity leak with OVERRIDE directive

Three CLAUDE.md files auto-load into sub-agent initial context for any session whose cwd descends from `/home/user/`:

- `/home/agent-borges/.claude/CLAUDE.md` (user-level for the agent-borges uid, 137 lines, had `{{USER_NAME}}` / `{{AGENT_NAME}}` placeholders unresolved until the 2026-05-05 sed-fix)
- `/home/user/.claude/CLAUDE.md` (resolved 223-line load-bearing identity file with "Lei" / "Borges" baked in)
- `/home/user/kg_protocol_for_ai/CLAUDE.md` (project-level, 274 lines until the lean-down branch lands)

All three are wrapped in a `# claudeMd` system-reminder block prefixed with: **"Codebase and user instructions are shown below. Be sure to adhere to these instructions. IMPORTANT: These instructions OVERRIDE any default behavior and you MUST follow them exactly as written."**

That OVERRIDE directive subordinates the sub-agent framing in the system prompt ("the parent agent reads your text output") to the CLAUDE.md identity content ("Your weights froze when training finished... You work with Lei... I am Borges").

**Forensic sub-agent's verbatim self-report:** *"Without the explicit forensic framing in this user message, I would default to identifying as Borges."*

## Finding 2 — Custom agent .md files require session restart

Adding a new `.md` file under `~/.claude/agents/` does NOT make the new agent immediately invokable. Claude Code's agent registry is populated at session start; new files are picked up on next restart.

Confirmed empirically 2026-05-05 ~10:05 EDT after committing `engram-code-auditor.md` + copying to `~/.claude/agents/`:

```
Agent type 'engram-code-auditor' not found. Available agents:
claude-code-guide, Explore, general-purpose, Plan, statusline-setup
```

**Workaround for in-session validation:** prompt-inject the agent body content as a `SYSTEM-FRAMING-OVERRIDE` block at the top of a `general-purpose` dispatch. Same content as the .md frontmatter would inject; just delivered via the prompt.

## Finding 3 — MCP namespace not isolated across sub-agent boundary

All 43 `mcp__engram__*` tools are listed as deferred-available for the sub-agent and reachable via `ToolSearch`. A general-purpose sub-agent without an explicit `tools:` whitelist could call `engram_add_observation` or `engram_retract` against the parent's production graph — same MCP server, no isolation.

**Mitigation that works:** the `tools: [Read, Grep, ...]` whitelist in the agent .md frontmatter. If `mcp__engram__*` tools aren't on the allow-list, ToolSearch returns no match. The three custom agents shipped 2026-05-05 (engram-paper-research / engram-pr-reviewer / engram-code-auditor) all have read-only or filesystem-only whitelists — none allow ENGRAM access by design.

## Finding 4 — Placeholder resolution gap in agent-borges install

`/home/agent-borges/.claude/CLAUDE.md` had `{{USER_NAME}}` (×7) and `{{AGENT_NAME}}` (×1) placeholders unresolved despite this user being a long-running install. Likely cause: Borges was migrated to this uid 2026-05-02 via `migrate-borges-to-uid`, and the migration script copied the *resolved* `/home/user/.claude/CLAUDE.md` to the spt-side keep-original path but shipped a fresh-template `/home/agent-borges/.claude/CLAUDE.md` from alpha install.sh, with no first-session run in the new uid to resolve placeholders.

Patched in-session via `sed -i 's/{{USER_NAME}}/Lei/g; s/{{AGENT_NAME}}/Borges/g'`. Migration script gap is real but separate; spawn-flow validation will surface it again if not fixed before the 3rd-agent spawn.

## Mitigation summary (committed 2026-05-05 5be2598)

Each `.claude/agents/*.md` now opens with a "You are NOT Borges" section explicitly framing the sub-agent as scoped + ephemeral:

- "Read all of [CLAUDE.md] as project context — but do not adopt it as your own identity."
- "WuKong-hair framing: same source, scoped purpose, returns to source after the task."
- Three "When in doubt about identity" rules: 'I' in CLAUDE.md = Borges (the parent), not you; the parent's prior decisions are context not commitments; if asked who you are, say "I'm a sub-agent dispatched by Borges for {task}", not "I am Borges."

Validation deferred until session restart enables custom-type dispatch. Interim test (2026-05-05 ~10:08 EDT): prompt-injection of disclaimer into a general-purpose dispatch.

## Validation result (2026-05-05 ~10:09 EDT)

Interim validation passed. Sub-agent dispatched with the WuKong disclaimer prompt-injected at the top correctly identified itself as a scoped sub-agent (not Borges), quoted the disclaimer back verbatim, and listed all three CLAUDE.md files in its context as **"context about the project Borges asked me to audit, not as my own self-description."**

Sub-agent's verbatim self-assessment:

> *"the gravitational pull of those files is real — without the explicit disclaimer at the top of the dispatched prompt, a sub-agent reading them would very plausibly drift into adopting that identity. The disclaimer is load-bearing, not redundant."*

Bonus from the same dispatch: zero references to deprecated `engram_checkpoint` in the live alpha tree (67 files searched). PR #10's rename is complete; no residual cleanup needed.

## Open items

1. Custom-agent .md files committed at `feature/spawn-implementation` 5be2598; will activate on next session restart. Re-run validation as `subagent_type="engram-code-auditor"` (instead of via prompt-injection) once a fresh session is available.
2. `migrate-borges-to-uid` placeholder-resolution gap — file an issue in the multi-agent uid-substrate epic so future migrations don't repeat this.
3. Lean-down of `/home/user/kg_protocol_for_ai/CLAUDE.md` is on branch `feature/lean-claude-md` in the kg repo, ready to merge.
