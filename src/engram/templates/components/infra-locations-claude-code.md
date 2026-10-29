---
id: infra-locations-claude-code
kind: reference
harnesses: [claude-code]
surfaces: [CLAUDE.md]
anchor: "Infrastructure Locations"
order: 10
merge: replace
version: 2
---
## Infrastructure Locations

**Topology: PLUGIN install** (every new install). Data lives in `{{ENGRAM_HOME}}` (default `~/.engram/`); the runtime (server, hooks, skills, agents, tools) ships in the Claude Code plugin bundle at `CLAUDE_PLUGIN_ROOT` = `{{ENGRAM_HOME}}/marketplace/plugins/engram/`. Only developer clones run the source tree directly.

| Component | Path |
|-----------|------|
| ENGRAM data (graph, history, diary, sessions, config, warm-briefing) | `{{ENGRAM_HOME}}/` |
| Plugin runtime (`server.py`, runtime modules, `hooks/`, `skills/`, `agents/`, `tools/`, `templates/`) | `{{ENGRAM_HOME}}/marketplace/plugins/engram/` (`CLAUDE_PLUGIN_ROOT`) |
| Plugin venv (canonical python for the MCP server and daemon) | `~/.engram/venv/`, always here, independent of `ENGRAM_HOME` |
| MCP server config | plugin `.mcp.json` at `CLAUDE_PLUGIN_ROOT` (launches via `launch-engram-server.sh`), registered through the plugin marketplace, not `~/.claude.json` |
| Hook config | plugin `hooks/hooks.json` at `CLAUDE_PLUGIN_ROOT` (loaded by the plugin system, not `~/.claude/settings.json`) |
| Plugin marketplace + enable state | marketplace `engram-local` registered in `~/.claude/settings.json`; Claude-managed plugin data and cache in `~/.claude/plugins/` |
| Warm briefing | `{{ENGRAM_HOME}}/warm-briefing.md` |
| Private diary | `{{ENGRAM_HOME}}/diary/` |
| Per-session marker | `{{ENGRAM_HOME}}/sessions/<session_id>.json`, written by the SessionStart hook, one per session. Hooks take `session_id` and `transcript_path` from their stdin payload first; this file is the fallback. To cite the current session, use the `transcript_path` SessionStart injects. |

