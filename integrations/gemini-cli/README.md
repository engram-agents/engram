# ENGRAM — Gemini CLI Integration

This directory ships ENGRAM as a Gemini CLI extension. It's an **additive** second integration surface alongside the existing Claude Code support — both can coexist on the same SQLite database.

## Status

- v0.1: source files only. Manual install for the first smoke test.
- v0.2 (planned): `install.sh --target=gemini` automates the substitution + copy.

## Contents

| File | Purpose |
|------|---------|
| `gemini-extension.json.template` | Gemini extension manifest (registers the ENGRAM MCP server, names `GEMINI.md` as the context file) |
| `hooks/hooks.json.template` | Hook event wiring — SessionStart, BeforeAgent, AfterAgent → existing alpha hook scripts |
| `GEMINI.md.template` | Agent-facing context (identity scaffolding + ENGRAM write discipline) — auto-loaded by Gemini CLI when the extension is active |
| `README.md` | This file |

## Hook event coverage

| Gemini event | Hook script(s) | Notes |
|---|---|---|
| `SessionStart` | `start-engram-daemon.sh`, `engram-session-start-hook.py` | Same input shape as Claude Code's SessionStart; `source` differs (`'startup' \| 'resume' \| 'clear'` vs Claude's `'startup' \| 'resume' \| 'compact'`) but the hook does not filter on it |
| `BeforeAgent` | `engram-user-identity-hook.py`, `engram-surface-hook.py` | Equivalent to Claude Code's `UserPromptSubmit` |
| `AfterAgent` | `engram-stop-hook.py` | Equivalent to Claude Code's `Stop` (per-turn after final response) |
| _not wired_: `BeforeTool` | (Claude-specific `engram-toolcall-repair.py`) | The antml-prefix bug it fixes is Claude-specific; not needed under Gemini |
| _not wired_: `PreCompress` | (would be `engram-postcompact-hook.py`) | Timing differs from Claude's `PostCompact` (PreCompress fires *before* compression, same agent sees the marker, not a new one). Treat `SessionStart` with `source: 'resume'` as the post-compression wake instead. |

## Prerequisites

1. Run the main alpha install first (`./install.sh` from the alpha repo root). This populates `~/.engram/` with the runtime snapshot and the hook scripts.
2. Install Gemini CLI (`npm install -g @google/gemini-cli` or follow `https://github.com/google-gemini/gemini-cli`).
3. Confirm `~/.gemini/` exists (Gemini CLI creates it on first run).

## Manual install (v0.1)

Substitute the placeholders, then place the rendered files in `~/.gemini/extensions/engram/`. Choose absolute paths that match your environment:

```bash
# Resolve paths
PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)')"
ENGRAM_HOME="$HOME/.engram"
ENGRAM_HOOKS_DIR="$ENGRAM_HOME/hooks"
EXT_DIR="$HOME/.gemini/extensions/engram"

# Create extension directory
mkdir -p "$EXT_DIR/hooks"

# Render manifest
sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
    -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
    gemini-extension.json.template > "$EXT_DIR/gemini-extension.json"

# Render hooks config
sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
    -e "s|{{ENGRAM_HOOKS_DIR}}|$ENGRAM_HOOKS_DIR|g" \
    hooks/hooks.json.template > "$EXT_DIR/hooks/hooks.json"

# Render GEMINI.md (uses the seed-node IDs already substituted into the
# alpha install at ~/.claude/CLAUDE.md — copy from there if present, or
# substitute manually)
cp GEMINI.md.template "$EXT_DIR/GEMINI.md"
# TODO: post-install pass should substitute {{AX_HONESTY}}, {{GL_EPISTEMIC_HUMILITY}},
# {{DF_EPISTEMIC_IDENTITY}}, {{ENGRAM_HOME}} in $EXT_DIR/GEMINI.md
# (the same IDs used by the Claude CLAUDE.md install — readable from
# the seed-graph snapshot in ~/.engram/knowledge.db).
```

After install, start a fresh Gemini CLI session in any directory. The extension should:
- Register `engram` as an MCP server (visible via `gemini mcp list`)
- Inject a SessionStart pointer into the first turn
- Surface ENGRAM recall on each user prompt (via `BeforeAgent`)
- Flag missing writes at end-of-turn (via `AfterAgent`)

## Coexistence with Claude Code

The same SQLite database (`~/.engram/knowledge.db`) is used by both surfaces. Hooks are dispatched per-surface (Claude Code via `~/.claude/settings.json`; Gemini CLI via this extension's `hooks/hooks.json`). The MCP server runs once per CLI process — both CLIs can write concurrently because SQLite WAL mode handles multi-writer access.

The surface-daemon (`engram-surface-daemon.py`) is started by either CLI's SessionStart hook on a best-effort basis; subsequent starts are no-ops if the daemon is already running.

## Known gaps (v0.1)

1. **No post-compression wake hook.** Gemini's `PreCompress` is advisory and pre-compression. Claude Code's PostCompact fires *after* compaction so the new agent sees the injection; Gemini has no direct analog. Workaround: GEMINI.md instructs the agent to treat `SessionStart` with `source: 'resume'` as the post-compression boundary and re-read the warm-briefing.

2. **No `BeforeTool` repair.** Claude's antml-prefix bug doesn't apply under Gemini, so `engram-toolcall-repair.py` is not wired. If a Gemini-specific tool-call corruption mode appears, add a regex matcher for `mcp_engram_.*` (Gemini tool-name shape) and wire a Gemini-tailored repair script.

3. **`install.sh` does not yet automate this.** Manual install above is the v0.1 path. Once the smoke test confirms the manifest + hook wiring work, `install.sh --target=gemini` (or auto-detection) will land in v0.2.

4. **GEMINI.md template substitution is partly manual.** The seed-node placeholders (`{{AX_HONESTY}}` etc.) are substituted by `bootstrap.py` at alpha install time for `~/.claude/CLAUDE.md`. We need a parallel render step for `~/.gemini/extensions/engram/GEMINI.md`. For v0.1, copy the resolved IDs from the rendered Claude CLAUDE.md by hand, or read them from the seed-graph snapshot.
