#!/usr/bin/env bash
# uninstall-local-marketplace.sh — removes the ENGRAM local plugin marketplace.
#
# Safe defaults — this script removes the plugin registration and assembled
# marketplace tree WITHOUT touching your ENGRAM data or agent identity:
#
#   REMOVED:
#     $ENGRAM_HOME/marketplace/       assembled plugin tree
#     $ENGRAM_HOME/.deployed-version  install version marker
#     host CLI registration           (claude plugin marketplace remove / codex plugin remove)
#
#   PRESERVED:
#     $ENGRAM_HOME/knowledge.db       your entire ENGRAM graph
#     $ENGRAM_HOME/history/           awake-state milestone log
#     $ENGRAM_HOME/diary/             private diary entries
#     $ENGRAM_HOME/warm-briefing.md   relational context letter
#     $ENGRAM_HOME/config.json        agent configuration
#     $ENGRAM_HOME/venv/              Python environment
#     ~/.claude/CLAUDE.md             agent identity configuration
#
# For a complete data reset, remove $ENGRAM_HOME/ manually AFTER running this
# script. See README.md § Uninstall for the full decision tree.
#
# USAGE:
#   bash tools/uninstall-local-marketplace.sh [--help] [--dry-run]
#       [--skip-cli] [--target <claude-code|codex>]
#
# OPTIONS:
#   --help        Show this help and exit.
#   --dry-run     Print what would be done without making any changes.
#   --skip-cli    Skip the host CLI unregistration step (marketplace tree
#                 removal still proceeds). Use when the CLI is unavailable
#                 or the marketplace was never registered.
#   --target T    Host target: claude-code (default) or codex.

set -euo pipefail

log()  { printf '[uninstall-local-marketplace] %s\n' "$*"; }
warn() { printf '[uninstall-local-marketplace] WARN: %s\n' "$*"; }
die()  { printf '[uninstall-local-marketplace] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: bash tools/uninstall-local-marketplace.sh [--help] [--dry-run]
    [--skip-cli] [--target <claude-code|codex>]

Removes the ENGRAM plugin registration and marketplace tree.
Preserves all ENGRAM data ($ENGRAM_HOME/knowledge.db, history, diary, etc.).

Options:
  --help        Show this help and exit.
  --dry-run     Print what would be done without making any changes.
  --skip-cli    Skip host CLI unregistration (remove marketplace tree only).
  --target T    claude-code (default) or codex.

To reinstall after uninstalling:
  bash tools/install-local-marketplace.sh

For a full data reset, remove $ENGRAM_HOME/ manually after running this script.
EOF
  exit 0
}

DRY_RUN=0
SKIP_CLI=0
TARGET="claude-code"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)    usage ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --skip-cli)   SKIP_CLI=1; shift ;;
    --target)
      [[ $# -ge 2 ]] || die "--target requires a value: claude-code or codex"
      TARGET="$2"; shift 2 ;;
    --target=*)   TARGET="${1#--target=}"; shift ;;
    *)            die "Unknown argument: $1. Run with --help for usage." ;;
  esac
done
case "$TARGET" in
  claude-code|codex) ;;
  *) die "Invalid --target: $TARGET (expected claude-code or codex)" ;;
esac

ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
MARKETPLACE_ROOT="$ENGRAM_HOME/marketplace"
VERSION_MARKER="$ENGRAM_HOME/.deployed-version"

[[ $DRY_RUN -eq 1 ]] && log "[dry-run — no changes will be made]"

# ── Nothing to do? ──────────────────────────────────────────────────────────
if [[ ! -d "$MARKETPLACE_ROOT" && ! -f "$VERSION_MARKER" ]]; then
  log "Nothing to uninstall — marketplace and version marker both absent."
  if [[ $SKIP_CLI -eq 0 ]]; then
    warn "If the plugin is still registered with Claude Code, run:"
    warn "  claude plugin marketplace remove engram-local"
    warn "  (or from inside Claude Code: /plugin disable engram)"
  fi
  exit 0
fi

# ── 1. Unregister from host CLI ─────────────────────────────────────────────
if [[ $SKIP_CLI -eq 1 ]]; then
  log "Skipping host CLI unregistration (--skip-cli)"
elif [[ "$TARGET" == "codex" ]]; then
  if command -v codex >/dev/null 2>&1; then
    log "Removing plugin from Codex..."
    if [[ $DRY_RUN -eq 0 ]]; then
      codex plugin remove engram@engram-local 2>/dev/null \
        && log "  Codex plugin removed." \
        || warn "  'codex plugin remove' failed or not supported. Remove manually: codex plugin remove engram@engram-local"
    else
      log "  (dry-run) Would run: codex plugin remove engram@engram-local"
    fi
  else
    warn "'codex' not on PATH — skipping CLI unregistration."
    warn "  Remove manually: codex plugin remove engram@engram-local"
  fi
else
  if command -v claude >/dev/null 2>&1; then
    log "Unregistering marketplace from Claude Code..."
    if [[ $DRY_RUN -eq 0 ]]; then
      claude plugin marketplace remove engram-local 2>/dev/null \
        && log "  Marketplace unregistered." \
        || {
          warn "  'claude plugin marketplace remove' returned non-zero."
          warn "  If the plugin is still visible in Claude Code, run inside a session:"
          warn "    /plugin disable engram"
          warn "    /plugin marketplace remove engram-local"
        }
    else
      log "  (dry-run) Would run: claude plugin marketplace remove engram-local"
    fi
  else
    warn "'claude' not on PATH — skipping CLI unregistration."
    warn "  Run inside a Claude Code session: /plugin disable engram"
  fi
fi

# ── 2. Remove marketplace tree ──────────────────────────────────────────────
REMOVED_MARKETPLACE=0
if [[ -d "$MARKETPLACE_ROOT" ]]; then
  if [[ $DRY_RUN -eq 0 ]]; then
    log "Removing $MARKETPLACE_ROOT"
    rm -rf "$MARKETPLACE_ROOT"
    REMOVED_MARKETPLACE=1
  else
    log "(dry-run) Would remove: $MARKETPLACE_ROOT"
  fi
fi

# ── 3. Remove version marker ─────────────────────────────────────────────────
REMOVED_MARKER=0
if [[ -f "$VERSION_MARKER" ]]; then
  if [[ $DRY_RUN -eq 0 ]]; then
    log "Removing $VERSION_MARKER"
    rm -f "$VERSION_MARKER"
    REMOVED_MARKER=1
  else
    log "(dry-run) Would remove: $VERSION_MARKER"
  fi
fi

# ── Done ─────────────────────────────────────────────────────────────────────
log ""
log "═══════════════════════════════════════════════════════════════════"
if [[ $DRY_RUN -eq 1 ]]; then
  log " Dry run complete. Run without --dry-run to apply the changes."
else
  log " Uninstall complete."
  log " "
  log " Removed:"
  [[ $REMOVED_MARKETPLACE -eq 1 ]] && log "   $MARKETPLACE_ROOT (plugin tree)"
  [[ $REMOVED_MARKER -eq 1 ]]      && log "   $VERSION_MARKER (version marker)"
  [[ $REMOVED_MARKETPLACE -eq 0 && $REMOVED_MARKER -eq 0 ]] && log "   (nothing — both were already absent)"
  log " "
  log " Preserved (ENGRAM data is intact):"
  log "   $ENGRAM_HOME/knowledge.db  — your ENGRAM graph"
  log "   $ENGRAM_HOME/history/      — session history"
  log "   $ENGRAM_HOME/diary/        — diary entries"
  log "   $ENGRAM_HOME/config.json   — configuration"
  log "   $ENGRAM_HOME/venv/         — Python environment"
  log "   ~/.claude/CLAUDE.md        — agent identity"
  log " "
  log " To reinstall: bash tools/install-local-marketplace.sh"
  log " For a full data reset: manually remove $ENGRAM_HOME/"
fi
log "═══════════════════════════════════════════════════════════════════"
