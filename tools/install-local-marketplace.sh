#!/usr/bin/env bash
# install-local-marketplace.sh — assembles a local Claude Code marketplace
# from build/plugin/ and registers it with Claude Code.
#
# OUTPUT: $ENGRAM_HOME/marketplace/ (default ~/.engram/marketplace/)
#   .claude-plugin/
#     marketplace.json          marketplace manifest pointing at plugins/engram
#   plugins/
#     engram/                   = contents of build/plugin/ (the assembled plugin tree)
#       .mcp.json
#       plugin.json
#       server.py
#       bootstrap.py
#       launch-engram-server.sh
#       hooks/
#       skills/
#       agents/
#       ...
#
# Then runs `claude plugin marketplace add $ENGRAM_HOME/marketplace`
# (the user separately runs `/plugin install engram` from inside Claude Code).
#
# USAGE:
#   bash tools/install-local-marketplace.sh [--help] [--skip-add]
#
# OPTIONS:
#   --help        Show this help and exit.
#   --skip-add    Assemble the marketplace tree but skip the
#                 `claude plugin marketplace add` step.
#
# PRECONDITIONS:
#   - Repo cloned + this script run from the repo root.
#   - build/plugin/ already assembled via `bash tools/build-plugin.sh`. If it
#     doesn't exist, this script invokes build-plugin.sh first.
#   - Phase 0 prereqs done (venv at ~/.engram/venv/ with requirements
#     installed). This script does NOT install Python deps — see README.md.
#
# WHY A LOCAL MARKETPLACE?
#   Claude Code's `/plugin install <path>` slash command does NOT accept
#   raw local paths — it only resolves plugin references via a registered
#   marketplace. A "marketplace" in this context can be a LOCAL DIRECTORY
#   containing .claude-plugin/marketplace.json + plugins/<name>/. This
#   script assembles such a directory at $ENGRAM_HOME/marketplace/ so the
#   user can install via the canonical `/plugin install engram` once the
#   marketplace is registered.
#
# IDEMPOTENT: re-running cleans + reassembles the marketplace tree.

set -euo pipefail

log() { printf '[install-local-marketplace] %s\n' "$*"; }
die() { printf '[install-local-marketplace] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: bash tools/install-local-marketplace.sh [--help] [--skip-add]

Assembles a local Claude Code marketplace at $ENGRAM_HOME/marketplace/ from
build/plugin/ and (by default) registers it via `claude plugin marketplace add`.

Options:
  --help        Show this help and exit.
  --skip-add    Assemble but skip the `claude plugin marketplace add` step.

After running:
  $ENGRAM_HOME/marketplace/.claude-plugin/marketplace.json
  $ENGRAM_HOME/marketplace/plugins/engram/...

Then in a Claude Code session:
  /plugin install engram

To upgrade later:
  cd path/to/engram-alpha
  git pull origin dev
  bash tools/build-plugin.sh
  bash tools/install-local-marketplace.sh    # re-assembles, idempotent
  # then in Claude Code:
  /plugin marketplace update
  /plugin upgrade engram
EOF
  exit 0
}

SKIP_ADD=0
for arg in "$@"; do
  case "$arg" in
    --help|-h) usage ;;
    --skip-add) SKIP_ADD=1 ;;
    *) die "Unknown argument: $arg. Run with --help for usage." ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "$REPO_ROOT/server.py" ]]; then
  die "Must be run from the repo root (expected server.py at $REPO_ROOT/server.py)"
fi

ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
MARKETPLACE_ROOT="$ENGRAM_HOME/marketplace"
PLUGIN_SRC="$REPO_ROOT/build/plugin"

# Always rebuild — cached build/plugin/ would be stale if the user edited
# build-plugin.sh, the source tree, etc. between marketplace assemblies.
# build-plugin.sh is idempotent + fast, so no downside to always invoking it.
log "Building plugin tree (idempotent)"
bash "$REPO_ROOT/tools/build-plugin.sh"

if [[ ! -f "$PLUGIN_SRC/plugin.json" ]]; then
  die "build/plugin/plugin.json missing — build-plugin.sh did not assemble correctly"
fi

log "Cleaning $MARKETPLACE_ROOT"
rm -rf "$MARKETPLACE_ROOT"
mkdir -p "$MARKETPLACE_ROOT/.claude-plugin"
mkdir -p "$MARKETPLACE_ROOT/plugins/engram"

log "Copying build/plugin/ -> $MARKETPLACE_ROOT/plugins/engram/"
cp -a "$PLUGIN_SRC/." "$MARKETPLACE_ROOT/plugins/engram/"

PLUGIN_VERSION="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('version','0.1.0'))" "$PLUGIN_SRC/plugin.json")"

log "Writing $MARKETPLACE_ROOT/.claude-plugin/marketplace.json (engram@${PLUGIN_VERSION})"
cat > "$MARKETPLACE_ROOT/.claude-plugin/marketplace.json" <<EOF
{
  "\$schema": "https://json.schemastore.org/claude-code-marketplace.json",
  "name": "engram-local",
  "version": "1.0.0",
  "description": "Local ENGRAM plugin marketplace assembled from build/plugin/ by tools/install-local-marketplace.sh",
  "owner": {
    "name": "engram-agents"
  },
  "plugins": [
    {
      "name": "engram",
      "description": "ENGRAM - Protocol-governed knowledge graph memory for Claude Code agents",
      "source": "./plugins/engram",
      "version": "$PLUGIN_VERSION",
      "category": "development",
      "author": {
        "name": "engram-agents"
      },
      "homepage": "https://github.com/engram-agents/engram-alpha",
      "tags": ["memory", "knowledge-graph", "mcp", "agent", "epistemics"]
    }
  ]
}
EOF

if ! python3 -c "import json; json.load(open('$MARKETPLACE_ROOT/.claude-plugin/marketplace.json'))" 2>/dev/null; then
  die "marketplace.json failed JSON parse"
fi

log "Marketplace assembled at $MARKETPLACE_ROOT"

if [[ $SKIP_ADD -eq 1 ]]; then
  log "Skipping 'claude plugin marketplace add' (--skip-add)"
  log "To register manually: claude plugin marketplace add $MARKETPLACE_ROOT"
  log "Then in a Claude Code session: /plugin install engram"
  exit 0
fi

if ! command -v claude >/dev/null 2>&1; then
  log "WARN: 'claude' not on PATH - cannot register the marketplace automatically"
  log "      Run manually: claude plugin marketplace add $MARKETPLACE_ROOT"
  log "      Then in a Claude Code session: /plugin install engram"
  exit 0
fi

log "Registering marketplace with Claude Code"
claude plugin marketplace add "$MARKETPLACE_ROOT"

log ""
log "═══════════════════════════════════════════════════════════════════"
log " Marketplace registered. Next steps (in a Claude Code session):"
log "═══════════════════════════════════════════════════════════════════"
log ""
log "  1. /plugin install engram      # install from the local marketplace"
log "  2. /plugin enable  engram      # ENABLE — local marketplace plugins"
log "                                 #   ship DISABLED by default; this"
log "                                 #   step is REQUIRED to activate."
log "  3. Restart Claude Code so MCP picks up the now-enabled plugin."
log ""
log " (Empirically verified: 2026-05-31 Chromebook trial. The /plugin"
log "  install step alone leaves the plugin in disabled state — the"
log "  user must explicitly /plugin enable engram.)"
log ""
log " To upgrade later:"
log "   cd path/to/engram-alpha"
log "   git pull origin dev"
log "   bash tools/install-local-marketplace.sh"
log "   # then in Claude Code:"
log "   /plugin marketplace update"
log "   /plugin upgrade engram"
