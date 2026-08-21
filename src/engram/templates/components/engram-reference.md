---
id: engram-reference
kind: reference
harnesses: all
surfaces: [CLAUDE.md]
anchor: "ENGRAM Reference"
order: 10
merge: replace
version: 1
---
## ENGRAM Reference

- **Data directory:** `{{ENGRAM_HOME}}/` (knowledge.db, config.json, session_log.md, warm-briefing.md)
- **Server source:** `{{ENGRAM_HOME}}/marketplace/plugins/engram/server.py`
- **Protocol / mechanics:** the MCP tool schemas (loaded once per session) + the `engram-*` skills are the live reference for node types, tool usage, workflows, focus mode, and the nap / sleep / temporal modes. *(The old combined `SKILL.md` doc was retired in #1149; the MCP tool schemas + engram-* skills are the replacement.)*
- **Viz server health dashboard:** `http://localhost:5001/health`

CLAUDE.md's job is identity and discipline; the MCP tool schemas + the `engram-*` skills are the mechanics and the live authority on protocol.

