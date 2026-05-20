# CLAUDE.md — engram-alpha project conventions

Developer-facing conventions for contributing to engram-alpha. Identity-level
instructions live in the user-level CLAUDE.md.

## MCP tool parameter style

All ENGRAM MCP tools (`@mcp.tool()`-decorated functions in `server.py`) take a
**single JSON-string parameter** — `payload_json`. The tool body json.loads it,
validates, dispatches.

Why: multi-arg signatures hit the antml-prefix swallow risk (ob_1499) — Claude
Code's prompt construction can drop arguments under certain whitespace
conditions, causing silent corruption. Single-payload pattern eliminates the
swallow surface.

Applies to: every new MCP tool, every existing-tool migration. Check existing
tools (`engram_add_observation`, `engram_derive`, etc.) for the canonical shape.

Lapses to fix on sight: any tool with multiple positional/keyword params
should be migrated. Wave-3 (2026-05-14) tightened most; a handful of legacy
read-side tools (`engram_surface`, `engram_query`, `engram_inspect`,
`engram_get_subgraph`, `engram_list`, `engram_history`, `engram_focus*`,
`engram_query_pattern`) still carry multi-param signatures as of 2026-05-19
and remain on the migration backlog.
