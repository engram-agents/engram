# packaging/

Build-time source materials that `tools/build-plugin.sh` assembles into the
packaged Claude Code plugin tree (`build/plugin/`). The repo is never
*recognized* as a plugin — it is only *reconstructed* into the plugin format by
the build script — so these sources can live anywhere; this directory keeps them
out of paths that Claude Code auto-scans.

## `mcp.json`

The MCP server registration for the **packaged plugin**. `build-plugin.sh`
copies it to `build/plugin/.mcp.json` (the dotfile name the plugin format
expects at the plugin root).

It deliberately does **not** live at the repo root as `.mcp.json`: Claude Code
auto-loads any repo-root `.mcp.json` as a *project-scope* MCP server for every
dev-mode clone, where `${CLAUDE_PLUGIN_ROOT}` is unset → a dead-path `engram`
registration that conflicts with the working user-scope server and knocks engram
**offline on restart**. Keeping the source here (no leading dot, not at root)
removes that auto-load surface entirely. See issue #618.

**Do not move this back to the repo root.**
