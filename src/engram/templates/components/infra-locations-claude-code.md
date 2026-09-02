---
id: infra-locations-claude-code
kind: reference
harnesses: [claude-code]
surfaces: [CLAUDE.md]
anchor: "Infrastructure Locations"
order: 10
merge: replace
version: 1
---
## Infrastructure Locations

**Topology: PLUGIN install** (the install route for all new installs). ENGRAM *data* lives in `{{ENGRAM_HOME}}` (default `~/.engram/`); the *runtime* (server, hooks, skills, agents, tools) ships in the Claude Code plugin bundle. `CLAUDE_PLUGIN_ROOT` = `{{ENGRAM_HOME}}/marketplace/plugins/engram/`. (Developer clones running the source tree directly are the only non-plugin topology; end-user installs are plugin-native.)

| Component | Path |
|-----------|------|
| ENGRAM data (graph, history, diary, sessions, config, warm-briefing) | `{{ENGRAM_HOME}}/` |
| Plugin runtime (`server.py`, runtime modules, `hooks/`, `skills/`, `agents/`, `tools/`, `templates/`) | `{{ENGRAM_HOME}}/marketplace/plugins/engram/` (`CLAUDE_PLUGIN_ROOT`) |
| Plugin venv (canonical python for the MCP server + daemon) | `{{ENGRAM_HOME}}/venv/` |
| MCP server config | plugin `.mcp.json` at `CLAUDE_PLUGIN_ROOT` (launches via `launch-engram-server.sh`) — registered through the plugin marketplace, not `~/.claude.json` |
| Hook config | plugin `hooks/hooks.json` at `CLAUDE_PLUGIN_ROOT` (loaded by the plugin system, not `~/.claude/settings.json`) |
| Plugin marketplace + enable state | marketplace `engram-local` registered in `~/.claude/settings.json`; Claude-managed plugin data/cache in `~/.claude/plugins/` |
| Warm briefing | `{{ENGRAM_HOME}}/warm-briefing.md` |
| Private diary | `{{ENGRAM_HOME}}/diary/` |
| Per-session marker | `{{ENGRAM_HOME}}/sessions/<session_id>.json` — `{session_id, transcript_path, source, cwd, started_at, role, purpose}`, written by SessionStart hook. One file per session, no clobber across concurrent runs. Most hooks pull `session_id` + `transcript_path` directly from their stdin payload (Claude Code emits both on every hook event), so the file is the fallback path. Agents citing current-session observations should read the `transcript_path` already injected into their context by SessionStart's additionalContext. |
