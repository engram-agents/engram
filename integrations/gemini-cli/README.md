# ENGRAM — Gemini CLI Integration

This directory ships ENGRAM as a Gemini CLI extension. It's an **additive** second integration surface alongside the existing Claude Code support — and an **isolated** one: each agent gets its own data directory, knowledge graph, and identity.

## Status

- v0.1: source files + `install-gemini.sh` automation. Manual install path documented below for debugging.

## Multi-tenant by design

Each ENGRAM install is fully isolated:

| Surface       | Default ENGRAM_HOME | Identity doc         | Hook config                                  |
|---------------|---------------------|----------------------|----------------------------------------------|
| Claude Code   | `~/.engram/`         | `~/.claude/CLAUDE.md` | `~/.claude/settings.json` (hooks key)        |
| Gemini CLI    | `~/.engram-gemini/`  | `~/.gemini/extensions/engram/GEMINI.md` | `~/.gemini/extensions/engram/hooks/hooks.json` |

The Gemini agent picks its own name during the first-session dialogue (it does *not* inherit the Claude install's identity). Hook scripts read `$ENGRAM_HOME` so each install operates on its own knowledge.db, daemon socket, prompt counter, warm briefing, etc. No cross-contamination.

## Contents

| File | Purpose |
|------|---------|
| `gemini-extension.json.template` | Gemini extension manifest (registers the ENGRAM MCP server, names `GEMINI.md` as the context file, sets `ENGRAM_HOME` + `ENGRAM_DATA_DIR` env on the server) |
| `hooks/hooks.json.template` | Hook event wiring — SessionStart, BeforeAgent, AfterAgent → existing alpha hook scripts, with `ENGRAM_HOME` set per command |
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

1. Python 3 with `fastmcp` and `sentence-transformers` installed.
2. Install Gemini CLI (`npm install -g @google/gemini-cli` or follow `https://github.com/google-gemini/gemini-cli`).
3. Confirm `~/.gemini/` exists (Gemini CLI creates it on first run).

## Install

From the alpha repo root:

```bash
./install-gemini.sh
```

This runs the full bootstrap into `~/.engram-gemini/` (data dir) and `~/.gemini/extensions/engram/` (extension dir). To use a different data dir:

```bash
ENGRAM_HOME=~/.engram-other ./install-gemini.sh
```

After install, start a fresh Gemini CLI session in any directory:

```bash
gemini
```

The extension should:
- Register `engram` as an MCP server (visible via `gemini mcp list`)
- Inject a SessionStart pointer into the first turn
- Surface ENGRAM recall on each user prompt (via `BeforeAgent`)
- Flag missing writes at end-of-turn (via `AfterAgent`)
- Detect `~/.engram-gemini/first-run-pending` and run the cold-start dialogue (the agent picks its own name)

## Coexistence with Claude Code

Two separate installs, two separate knowledge graphs. Each has its own:
- `knowledge.db` (separate node IDs, separate seed graph instance)
- `recall-daemon.sock` (no port collision; both daemons run side-by-side)
- `warm-briefing.md`, `diary/`, `history/`
- Identity nodes, focus sets, edit history

Hook scripts are aware of `$ENGRAM_HOME` and operate only on the install whose env they were spawned in. Same hook source code, separate runtime state.

## Manual install (debugging only)

If `install-gemini.sh` fails or you need to inspect what gets rendered:

```bash
PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)')"
ENGRAM_HOME="$HOME/.engram-gemini"
ENGRAM_HOOKS_DIR="$ENGRAM_HOME/hooks"
EXT_DIR="$HOME/.gemini/extensions/engram"

mkdir -p "$EXT_DIR/hooks" "$ENGRAM_HOME/hooks"

# Render manifest
sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
    -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
    gemini-extension.json.template > "$EXT_DIR/gemini-extension.json"

# Render hooks config
sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
    -e "s|{{ENGRAM_HOOKS_DIR}}|$ENGRAM_HOOKS_DIR|g" \
    -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
    hooks/hooks.json.template > "$EXT_DIR/hooks/hooks.json"

# GEMINI.md still needs seed-graph IDs substituted — easiest is to run
# install-gemini.sh after the above, or invoke bootstrap.py directly with
# TARGET=gemini.
```

## Known gaps (v0.1)

1. **No post-compression wake hook.** Gemini's `PreCompress` is advisory and pre-compression. Claude Code's PostCompact fires *after* compaction so the new agent sees an injection; Gemini has no direct analog. Workaround: GEMINI.md instructs the agent to treat `SessionStart` with `source: 'resume'` as the post-compression boundary and re-read the warm-briefing.

2. **No `BeforeTool` repair.** Claude's antml-prefix bug doesn't apply under Gemini, so `engram-toolcall-repair.py` is not wired. If a Gemini-specific tool-call corruption mode appears, add a regex matcher for `mcp_engram_.*` (Gemini tool-name shape) and wire a Gemini-tailored repair script.

3. **`gemini mcp list` registration timing.** The MCP server is registered via the extension manifest, not via a CLI command — first session after install should pick it up automatically. If `gemini mcp list` doesn't show `engram`, restart the CLI.
