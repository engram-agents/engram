#!/usr/bin/env bash
# build-plugin.sh — assembles the ENGRAM Claude Code plugin tree from canonical sources.
#
# OUTPUT: build/plugin/
#   <plugin>/                   plugin root (contents below)
#     server.py                 MCP server + runtime modules
#     engram_*.py               runtime helper modules
#     SKILL.md                  ENGRAM protocol reference
#     bootstrap.py              bootstrap helper (imported by first-session)
#     plugin.json               plugin manifest
#     .mcp.json                 MCP server registration
#     hooks/
#       hooks.json              plugin hook config
#       *.py *.sh               hook scripts
#     skills/                   skill tree (namespaced /engram:<name> by Claude Code)
#     agents/                   agent specs
#     output-styles/            output style markdown files
#
# USAGE:
#   bash tools/build-plugin.sh [--help]
#
# The script is idempotent: re-running cleans and re-assembles build/plugin/.
# Run from the repo root (engram-alpha/).
#
# NOTE: This script does NOT copy knowledge.db or any ~/.engram data. The
# plugin reads/writes ENGRAM_HOME (defaults to ~/.engram) at runtime — that
# data is user-scoped and never part of the plugin tree. See plugin.json.
#
# PYTHON SUBSTITUTION: start-engram-daemon.sh contains {{PYTHON}} template
# placeholders (shared with the legacy install.sh path). The build script
# substitutes {{PYTHON}} → python3 in the copied file so it is ready to use
# without further templating.

set -euo pipefail

# ── helpers ────────────────────────────────────────────────────────────────────

usage() {
  cat <<'EOF'
Usage: bash tools/build-plugin.sh [--help]

Assembles the ENGRAM Claude Code plugin tree into build/plugin/.

Options:
  --help    Show this help message and exit.

The script must be run from the repo root (the directory containing server.py).
Output is written to build/plugin/ — this directory is gitignored.
Re-running is safe: the output tree is cleaned and rebuilt each time.
EOF
  exit 0
}

log() { printf '[build-plugin] %s\n' "$*"; }
die() { printf '[build-plugin] ERROR: %s\n' "$*" >&2; exit 1; }

# ── args ───────────────────────────────────────────────────────────────────────

for arg in "$@"; do
  case "$arg" in
    --help|-h) usage ;;
    *) die "Unknown argument: $arg. Run with --help for usage." ;;
  esac
done

# ── repo-root guard ────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "$REPO_ROOT/server.py" ]]; then
  die "Must be run from the repo root (expected server.py at $REPO_ROOT/server.py)"
fi

# ── build target ──────────────────────────────────────────────────────────────

PLUGIN_ROOT="$REPO_ROOT/build/plugin"

log "Cleaning $PLUGIN_ROOT"
rm -rf "$PLUGIN_ROOT"
mkdir -p "$PLUGIN_ROOT"

# ── 1. MCP server + runtime modules ───────────────────────────────────────────

log "Copying runtime modules → plugin root"
cp "$REPO_ROOT/server.py"              "$PLUGIN_ROOT/server.py"
cp "$REPO_ROOT/bootstrap.py"           "$PLUGIN_ROOT/bootstrap.py"
cp "$REPO_ROOT/SKILL.md"               "$PLUGIN_ROOT/SKILL.md"
cp -p "$REPO_ROOT/launch-engram-server.sh" "$PLUGIN_ROOT/launch-engram-server.sh"
chmod +x "$PLUGIN_ROOT/launch-engram-server.sh"

for f in "$REPO_ROOT"/engram_*.py; do
  [[ -f "$f" ]] || die "No engram_*.py files found at $REPO_ROOT"
  cp "$f" "$PLUGIN_ROOT/$(basename "$f")"
done

# ── 2. Manifests ──────────────────────────────────────────────────────────────

log "Copying manifests"
cp "$REPO_ROOT/plugin.json"  "$PLUGIN_ROOT/plugin.json"
cp "$REPO_ROOT/.mcp.json"    "$PLUGIN_ROOT/.mcp.json"

# ── 3. Hooks ──────────────────────────────────────────────────────────────────

log "Copying hooks → plugin hooks/"
mkdir -p "$PLUGIN_ROOT/hooks"

# Copy the plugin hook config (already written with ${CLAUDE_PLUGIN_ROOT} paths)
cp "$REPO_ROOT/hooks/hooks.json" "$PLUGIN_ROOT/hooks/hooks.json"

# Copy hook scripts — excluding __pycache__ and persona-specific / dead scripts
# that are NOT referenced in hooks.json and should not ship in the generic plugin.
# Confirmed safe to exclude: none of these are imported by any referenced hook
# (grep-verified against all scripts named in hooks.json before adding this list).
HOOKS_EXCLUDE=(
  context_tracker_hook.py           # persona-specific; superseded by context_tracker.py
  engram-baton-prompt-hook.py       # persona-specific (multi-agent baton workflow)
  engram-time-bar-hook.py           # persona-specific UI; not in hooks.json
  engram-utility-credit-mention-stop.py  # dead / persona-specific; not in hooks.json
)

find "$REPO_ROOT/hooks/claude" -maxdepth 1 \( -name "*.py" -o -name "*.sh" \) | while read -r src; do
  fname="$(basename "$src")"
  for excl in "${HOOKS_EXCLUDE[@]}"; do
    if [[ "$fname" == "$excl" ]]; then
      log "  Skipping persona-specific/dead hook: $fname"
      continue 2
    fi
  done
  cp "$src" "$PLUGIN_ROOT/hooks/$fname"
done

# Substitute {{PYTHON}} → python3 in start-engram-daemon.sh
# (The shell script shares the template var with install.sh; we resolve it for plugin use.)
if [[ -f "$PLUGIN_ROOT/hooks/start-engram-daemon.sh" ]]; then
  sed -i 's/{{PYTHON}}/python3/g' "$PLUGIN_ROOT/hooks/start-engram-daemon.sh"
  log "  Substituted {{PYTHON}} → python3 in start-engram-daemon.sh"
fi

# ── 4. Skills ─────────────────────────────────────────────────────────────────

log "Copying skills → plugin skills/"
mkdir -p "$PLUGIN_ROOT/skills"
if [[ -d "$REPO_ROOT/skills/claude" ]]; then
  cp -r "$REPO_ROOT/skills/claude/." "$PLUGIN_ROOT/skills/"
else
  die "skills/claude/ not found at $REPO_ROOT/skills/claude"
fi

# ── 5. Agents ─────────────────────────────────────────────────────────────────

log "Copying agents → plugin agents/"
mkdir -p "$PLUGIN_ROOT/agents"
if [[ -d "$REPO_ROOT/agents/claude" ]]; then
  # Copy all agent .md files (skip README.md — that's repo-level, not a spec)
  find "$REPO_ROOT/agents/claude" -maxdepth 1 -name "*.md" ! -name "README.md" | while read -r src; do
    cp "$src" "$PLUGIN_ROOT/agents/$(basename "$src")"
  done
else
  die "agents/claude/ not found at $REPO_ROOT/agents/claude"
fi

# ── 6. Output styles ──────────────────────────────────────────────────────────

log "Copying output-styles → plugin output-styles/"
mkdir -p "$PLUGIN_ROOT/output-styles"
if [[ -d "$REPO_ROOT/output-styles/claude" ]]; then
  cp -r "$REPO_ROOT/output-styles/claude/." "$PLUGIN_ROOT/output-styles/"
else
  die "output-styles/claude/ not found at $REPO_ROOT/output-styles/claude"
fi

# ── done ──────────────────────────────────────────────────────────────────────

log ""
log "Plugin tree assembled at: $PLUGIN_ROOT"
log ""
log "Contents:"
find "$PLUGIN_ROOT" -not -path '*/__pycache__/*' -not -name '*.pyc' | sort | sed "s|$PLUGIN_ROOT/||" | sed "s|^|  |"
log ""
log "To verify manifest JSON:"
log "  python3 -c \"import json; json.load(open('$PLUGIN_ROOT/plugin.json'))\" && echo OK"
log "  python3 -c \"import json; json.load(open('$PLUGIN_ROOT/.mcp.json'))\" && echo OK"
log "  python3 -c \"import json; json.load(open('$PLUGIN_ROOT/hooks/hooks.json'))\" && echo OK"
