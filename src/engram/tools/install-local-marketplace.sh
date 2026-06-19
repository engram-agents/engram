#!/usr/bin/env bash
# install-local-marketplace.sh — assembles a local ENGRAM plugin marketplace
# from build/plugin/ and optionally registers it with the host CLI.
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
# Then runs `claude plugin marketplace add $ENGRAM_HOME/marketplace` for Claude
# Code targets. Codex targets are installed with `codex plugin add engram@engram-local`.
#
# USAGE:
#   bash tools/install-local-marketplace.sh [--help] [--skip-add] [--skip-build] [--target <claude-code|codex>]
#
# OPTIONS:
#   --help        Show this help and exit.
#   --skip-add    Assemble the marketplace tree but skip the
#                 `claude plugin marketplace add` step.
#   --skip-build  Skip the internal build-plugin.sh invocation entirely.
#                 Use this when the caller has already built build/plugin/ with
#                 the correct flags (e.g., migrate-to-plugin.sh Step 2) and you
#                 do not want this script to clobber that build with a flagless
#                 rebuild.  build/plugin/plugin.json must already exist.
#   --target T    Build target: claude-code (default) or codex.
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
Usage: bash tools/install-local-marketplace.sh [--help] [--skip-add] [--skip-build] [--target <claude-code|codex>] [--allow-branch]

Assembles a local ENGRAM marketplace at $ENGRAM_HOME/marketplace/ from
build/plugin/. With --target claude-code it registers via `claude plugin marketplace add`;
with --target codex it installs via `codex plugin add engram@engram-local`.

Options:
  --help          Show this help and exit.
  --skip-add      Assemble but skip the `claude plugin marketplace add` step.
  --skip-build    Skip the internal build-plugin.sh invocation. Use when the
                  caller has already built build/plugin/ with the correct flags.
                  build/plugin/plugin.json must already exist.
  --target T      Build target to pass through when not using --skip-build:
                  claude-code (default) or codex.
  --allow-branch  Allow building from a PR/feature branch (overrides #794's guard; 'dev' and 'main' always allowed).
                  Use only for deliberate branch builds (e.g. metric-eval checkouts).

After running:
  $ENGRAM_HOME/marketplace/.claude-plugin/marketplace.json
  $ENGRAM_HOME/marketplace/plugins/engram/...

Then in a Claude Code session:
  /plugin install engram

Or for Codex:
  bash tools/install-local-marketplace.sh --target codex

To upgrade later:
  cd path/to/engram
  git pull origin main
  bash tools/install-local-marketplace.sh    # builds + re-assembles, idempotent
  # then in Claude Code:
  /plugin marketplace update engram-local
  /plugin -> Installed -> engram plugin (not the MCP under it) -> Update now
  /mcp -> engram -> Reconnect
  # or in Codex:
  bash tools/install-local-marketplace.sh --target codex
EOF
  exit 0
}

SKIP_ADD=0
SKIP_BUILD=0
TARGET="claude-code"
ALLOW_BRANCH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage ;;
    --skip-add) SKIP_ADD=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --allow-branch) ALLOW_BRANCH=1; shift ;;
    --target)
      [[ $# -ge 2 ]] || die "--target requires a value: claude-code or codex"
      TARGET="$2"
      shift 2
      ;;
    --target=*) TARGET="${1#--target=}"; shift ;;
    *) die "Unknown argument: $1. Run with --help for usage." ;;
  esac
done
case "$TARGET" in
  claude-code|codex) ;;
  *) die "Invalid --target: $TARGET (expected claude-code or codex)" ;;
esac

_self="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
REPO_ROOT="$(git -C "$(dirname "$_self")" rev-parse --show-toplevel 2>/dev/null)"
[ -n "$REPO_ROOT" ] || { echo "install-local-marketplace: ERROR — could not resolve repo root via git" >&2; exit 1; }
if [[ ! -f "$REPO_ROOT/src/engram/server.py" ]]; then
  die "Must be run from the repo root (expected src/engram/server.py at $REPO_ROOT/src/engram/server.py)"
fi

# #794 branch guard: refuse builds from PR/feature branches unless --allow-branch is set.
# Valid production branches: 'dev' (private dev source) and 'main' (public release branch).
# Every other branch — PR branches, feature branches, etc. — requires --allow-branch to override.
CURRENT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [[ "$CURRENT_BRANCH" != "dev" && "$CURRENT_BRANCH" != "main" && $ALLOW_BRANCH -eq 0 ]]; then
  die "Source repo is on branch '$CURRENT_BRANCH', not 'dev' (private dev) or 'main' (public release). Building from a PR/feature branch can deploy unmerged code. Pass --allow-branch to override (e.g. for metric-eval checkouts). Guard per #794."
fi

ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
MARKETPLACE_ROOT="$ENGRAM_HOME/marketplace"
PLUGIN_SRC="${ENGRAM_BUILD_DIR:-$REPO_ROOT/build/plugin}"

# Build the plugin tree unless --skip-build was passed.
#
# When called standalone (no --skip-build), read $ENGRAM_HOME/config.json for
# {multi_agent, install_tier} and pass the matching flags to build-plugin.sh.
# This mirrors the build_flags pattern in migrate-to-plugin.sh (step2_install_plugin)
# and makes this script self-sufficient: any caller gets a config-correct build
# rather than a silent single-agent/convenience default.
#
# When called with --skip-build (e.g., from migrate-to-plugin.sh after it has
# already run build-plugin.sh with the correct flags), the existing build/plugin/
# tree is used directly — no second flagless rebuild that would clobber the first.
if [[ $SKIP_BUILD -eq 1 ]]; then
  log "Skipping build-plugin.sh invocation (--skip-build)"
else
  # Assemble build flags from config.json (same pattern as migrate-to-plugin.sh).
  build_flags=()
  _cfg="$ENGRAM_HOME/config.json"
  if [[ -f "$_cfg" ]]; then
    # Hard-require jq when a config exists: without it the build silently
    # proceeds flagless and drops configured surfaces (multi-agent / dev tier)
    # — the #704 class. Same contract as migrate-to-plugin.sh's require_jq.
    if ! command -v jq &>/dev/null; then
      die "jq is required to read $_cfg for build flags (multi_agent/install_tier). Without jq the build would silently drop your configured surfaces (#704 class). Install jq and retry."
    fi
    _ma="$(jq -r '.multi_agent // false' "$_cfg")"
    _tier="$(jq -r '.install_tier // empty' "$_cfg")"
    [[ "$_ma" == "true" ]] && build_flags+=("--multi-agent")
    [[ -n "$_tier" ]] && build_flags+=("--tier" "$_tier")
  fi
  build_flags+=("--target" "$TARGET")
  [[ $ALLOW_BRANCH -eq 1 ]] && build_flags+=("--allow-branch")
  log "Building plugin tree (target: $TARGET, flags: ${build_flags[*]:-none})"
  bash "$REPO_ROOT/tools/build-plugin.sh" ${build_flags[@]+"${build_flags[@]}"}
fi

if [[ ! -f "$PLUGIN_SRC/plugin.json" ]]; then
  die "$PLUGIN_SRC/plugin.json missing — build-plugin.sh did not assemble correctly"
fi

if [[ $SKIP_BUILD -eq 1 ]]; then
  if [[ ! -f "$PLUGIN_SRC/platform.json" ]]; then
    die "--skip-build cannot verify target: $PLUGIN_SRC/platform.json missing. Rebuild with tools/install-local-marketplace.sh --target $TARGET or pass a build tree with platform.json."
  fi
  BUILT_TARGET="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('platform',''))" "$PLUGIN_SRC/platform.json")"
  if [[ "$BUILT_TARGET" != "$TARGET" ]]; then
    die "--skip-build target mismatch: $PLUGIN_SRC/platform.json is '$BUILT_TARGET' but --target is '$TARGET'. Rebuild with tools/install-local-marketplace.sh --target $TARGET or pass the matching --target."
  fi
fi

log "Cleaning $MARKETPLACE_ROOT"
rm -rf "$MARKETPLACE_ROOT"
mkdir -p "$MARKETPLACE_ROOT/.claude-plugin"
mkdir -p "$MARKETPLACE_ROOT/plugins/engram"

log "Copying $PLUGIN_SRC/ -> $MARKETPLACE_ROOT/plugins/engram/"
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
      "homepage": "https://github.com/engram-agents/engram",
      "tags": ["memory", "knowledge-graph", "mcp", "agent", "epistemics"]
    }
  ]
}
EOF

if ! python3 -c "import json; json.load(open('$MARKETPLACE_ROOT/.claude-plugin/marketplace.json'))" 2>/dev/null; then
  die "marketplace.json failed JSON parse"
fi

log "Marketplace assembled at $MARKETPLACE_ROOT"

# ── Version marker ─────────────────────────────────────────────────────────────
# Refresh ~/.engram/.deployed-version so the plugin path maintains the same
# forensics anchor the retired scatter upgrader (deploy.sh) kept (which commit
# is my running code from?). Without this the marker goes stale after every
# plugin upgrade — the engram-upgrade skill's change-set review
# (deployed..target) and post-upgrade verification both anchor on it. Same
# format the scatter-era writer used.
# Capture the OLD sha BEFORE overwriting the marker — used below to detect
# whether hooks/hooks.json changed in this upgrade (requires a full restart).
OLD_ALPHA_SHA="$(grep '^alpha_sha=' "$ENGRAM_HOME/.deployed-version" 2>/dev/null | cut -d= -f2- || true)"
ALPHA_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
ALPHA_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
  ALPHA_DIRTY="dirty"
else
  ALPHA_DIRTY="clean"
fi
cat > "$ENGRAM_HOME/.deployed-version" <<MARKER
alpha_sha=$ALPHA_SHA
alpha_branch=$ALPHA_BRANCH
alpha_dirty=$ALPHA_DIRTY
deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
deployed_from=$REPO_ROOT
MARKER
log "Version marker refreshed: $ENGRAM_HOME/.deployed-version (alpha_sha=$ALPHA_SHA)"

# ── Hook-registration-change detection ────────────────────────────────────────
# Claude Code only reloads the hook set on a FULL restart — /mcp reconnect
# reloads the MCP server but NOT the hook registration.  If hooks/hooks.json
# changed in this upgrade, a removed hook will throw a dangling-file error and
# an added hook won't fire until Claude Code is restarted.
if [[ -n "$OLD_ALPHA_SHA" ]] && git -C "$REPO_ROOT" cat-file -e "${OLD_ALPHA_SHA}^{commit}" 2>/dev/null && ! git -C "$REPO_ROOT" diff --quiet "$OLD_ALPHA_SHA" HEAD -- hooks/hooks.json 2>/dev/null; then
  echo ""
  echo "  ⚠️  HOOK REGISTRATION CHANGED in this upgrade."
  echo "      You MUST fully RESTART Claude Code (not just /mcp) to reload the hook set —"
  echo "      otherwise removed hooks will error and added hooks won't fire."
  echo "      (/mcp reconnect reloads the MCP server, not the hook registration.)"
  echo ""
fi

if [[ $SKIP_ADD -eq 1 ]]; then
  log "Skipping host CLI marketplace registration (--skip-add)"
  if [[ "$TARGET" == "codex" ]]; then
    log "To install in Codex: codex plugin add engram@engram-local"
  else
    log "To register manually: claude plugin marketplace add $MARKETPLACE_ROOT"
    log "Then in a Claude Code session: /plugin install engram"
  fi
  exit 0
fi

if [[ "$TARGET" == "codex" ]]; then
  if ! command -v codex >/dev/null 2>&1; then
    log "WARN: 'codex' not on PATH - cannot install the plugin automatically"
    log "      Run manually in Codex: codex plugin add engram@engram-local"
    exit 0
  fi
  log "Installing/updating plugin with Codex"
  codex plugin add engram@engram-local

  CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

  # Codex 0.137.0 does not expand ${CLAUDE_PLUGIN_ROOT} in plugin .mcp.json
  # command strings at MCP startup.  Patch the installed cache entry to the
  # absolute cache path so the next session can spawn ENGRAM reliably.
  CODEX_PLUGIN_CACHE_DIR=""
  if [[ -d "$CODEX_HOME/plugins/cache/engram-local/engram" ]]; then
    CODEX_PLUGIN_CACHE_DIR="$(python3 - "$CODEX_HOME/plugins/cache/engram-local/engram" <<'PY'
import os
import re
import sys

root = sys.argv[1]

def version_key(name: str) -> tuple[int, ...]:
    parts = re.split(r"[^0-9]+", name)
    return tuple(int(part) for part in parts if part)

candidates = [
    os.path.join(root, name)
    for name in os.listdir(root)
    if os.path.isdir(os.path.join(root, name))
]
if candidates:
    candidates.sort(key=lambda path: (version_key(os.path.basename(path)), os.path.basename(path)))
    print(candidates[-1])
PY
)"
  fi
  if [[ -n "$CODEX_PLUGIN_CACHE_DIR" && -f "$CODEX_PLUGIN_CACHE_DIR/.mcp.json" ]]; then
    log "Patching Codex plugin MCP command -> $CODEX_PLUGIN_CACHE_DIR/launch-engram-server.sh"
    python3 - "$CODEX_PLUGIN_CACHE_DIR/.mcp.json" "$CODEX_PLUGIN_CACHE_DIR/launch-engram-server.sh" <<'PY'
import json
import sys
path, command = sys.argv[1:3]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
data.setdefault("mcpServers", {}).setdefault("engram", {})["command"] = command
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
  else
    log "WARN: could not locate installed Codex plugin cache .mcp.json for absolute command patch"
  fi

  CODEX_AGENTS_DIR="$CODEX_HOME/agents"
  if compgen -G "$MARKETPLACE_ROOT/plugins/engram/agents/*.toml" > /dev/null; then
    log "Installing Codex custom-agent TOML files -> $CODEX_AGENTS_DIR"
    mkdir -p "$CODEX_AGENTS_DIR"
    cp -a "$MARKETPLACE_ROOT/plugins/engram/agents/"*.toml "$CODEX_AGENTS_DIR/"

    # Codex custom agents are config layers.  Plugin MCP tool allow/deny policy
    # must target the plugin-provided server namespace, not top-level
    # [mcp_servers.*] transport tables.  Rewrite older built TOMLs defensively.
    python3 - "$CODEX_AGENTS_DIR" <<'PY'
import pathlib
import sys
agents_dir = pathlib.Path(sys.argv[1])
for path in agents_dir.glob("engram-*.toml"):
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "[mcp_servers.engram]",
        "[plugins.\"engram@engram-local\".mcp_servers.engram]",
    )
    path.write_text(text, encoding="utf-8")
PY
  else
    log "WARN: no Codex custom-agent TOML files found in $MARKETPLACE_ROOT/plugins/engram/agents"
  fi
else
  if ! command -v claude >/dev/null 2>&1; then
    log "WARN: 'claude' not on PATH - cannot register the marketplace automatically"
    log "      Run manually: claude plugin marketplace add $MARKETPLACE_ROOT"
    log "      Then in a Claude Code session: /plugin install engram"
    exit 0
  fi

  log "Registering marketplace with Claude Code"
  claude plugin marketplace add "$MARKETPLACE_ROOT"
fi

log ""
log "═══════════════════════════════════════════════════════════════════"
if [[ "$TARGET" == "codex" ]]; then
  log " Codex marketplace installed/updated. Next steps:"
  log "═══════════════════════════════════════════════════════════════════"
  log ""
  log "  Restart or resume Codex so hooks/MCP reload the upgraded plugin cache."
  log "  If hook trust changed, approve the refreshed hook definitions when prompted."
  log ""
  log " To upgrade later in Codex:"
  log "   cd path/to/engram"
  log "   git pull origin main"
  log "   bash tools/install-local-marketplace.sh --target codex"
else
  log " Marketplace registered. Next steps (in a Claude Code session):"
  log "═══════════════════════════════════════════════════════════════════"
  log ""
  log "  1. /plugin install engram      # install from the local marketplace"
  log "                                 #   If this reports 'Plugin not found',"
  log "                                 #   the plugin is already registered from"
  log "                                 #   a previous install — run step 2 with"
  log "                                 #   /plugin enable engram@engram-local"
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
  log "   cd path/to/engram"
  log "   git pull origin main"
  log "   bash tools/install-local-marketplace.sh"
  log "   # then in Claude Code:"
  log "   1. /plugin marketplace update engram-local"
  log "   2. /plugin -> Installed -> engram plugin (not the MCP entry under it) -> Update now"
  log "   3. /mcp -> engram -> Reconnect"
fi
