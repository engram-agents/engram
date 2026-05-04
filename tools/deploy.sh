#!/bin/bash
# tools/deploy.sh — Deploy alpha code into the running ENGRAM home.
#
# This is the deliberate version-upgrade action. Copies code files from
# the alpha working tree into $ENGRAM_HOME, leaving data files
# (knowledge.db, history/, diary/, logs/, etc.) untouched.
#
# The runtime tree ($ENGRAM_HOME) holds running code as a *managed copy*
# of alpha — it is NOT a development surface. Edits to running code
# happen in alpha and propagate via this script. (Lei + Borges, 2026-05-04.)
#
# Usage:
#   tools/deploy.sh [--yes|-y] [--dry-run|-n] [--delete] [--help|-h]
#
# Environment:
#   ENGRAM_HOME — where to deploy to (default: $HOME/.engram)
#
# Flags:
#   --dry-run   Show planned changes; don't apply.
#   --yes       Skip the confirm prompt.
#   --delete    For directories in the manifest, remove files in target
#               that don't exist in source. Off by default — safer, but
#               leaves stale code behind when alpha removes a file.
#
# What gets deployed (manifest at the bottom of this script):
#   - Runtime Python: server.py, engram_client.py, engram_confidence.py,
#     viz_server.py
#   - Hooks (hooks/), tools (tools/)
#   - Protocol doc (SKILL.md)
#
# What does NOT get deployed:
#   - Data files: knowledge.db, graph_snapshot.md, session_log.md,
#     warm-briefing.md, config.json, recall_window.json
#   - Per-agent state: diary/, sessions/, history/, logs/,
#     active-session.json, feeling-nudge-active.json, etc.
#   - Install-time entry points: install.sh, install-gemini.sh,
#     bootstrap.py, settings.hooks.json.template, README.md, LICENSE,
#     USER_GUIDE.md (these belong to fresh installs, not running upgrades)
#   - Install-time scaffolding: templates/, integrations/, inter-agent/
#     (deployed at install time only, not part of the running tree)
#   - Skills: skills/ in alpha is the source-of-truth for fresh installs;
#     they live at ~/.claude/skills/ (claude) or ~/.gemini/skills/ (gemini)
#     once installed and are not part of $ENGRAM_HOME.
#
# After deploy: restart your MCP client to reload server.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALPHA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
YES=0
DRY_RUN=0
DELETE=0

usage() {
    sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# //; s/^#$//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)     YES=1; shift ;;
        --dry-run|-n) DRY_RUN=1; shift ;;
        --delete)     DELETE=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

# Sanity checks
if [[ ! -d "$ALPHA_DIR" ]]; then
    echo "ALPHA_DIR not found: $ALPHA_DIR" >&2
    exit 1
fi
if [[ ! -d "$ENGRAM_HOME" ]]; then
    echo "ENGRAM_HOME not found: $ENGRAM_HOME" >&2
    echo "(set ENGRAM_HOME=/path/to/runtime if it lives elsewhere)" >&2
    exit 1
fi
if [[ "$ALPHA_DIR" -ef "$ENGRAM_HOME" ]]; then
    echo "ALPHA_DIR and ENGRAM_HOME are the same directory — refusing to deploy" >&2
    echo "(this would defeat the dev/runtime separation; see header)" >&2
    exit 1
fi
if ! command -v rsync >/dev/null 2>&1; then
    echo "rsync not found — required for deploy" >&2
    exit 1
fi

# Resolve alpha version metadata
if git -C "$ALPHA_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    ALPHA_SHA="$(git -C "$ALPHA_DIR" rev-parse --short HEAD)"
    ALPHA_BRANCH="$(git -C "$ALPHA_DIR" rev-parse --abbrev-ref HEAD)"
    if [[ -n "$(git -C "$ALPHA_DIR" status --porcelain)" ]]; then
        ALPHA_DIRTY="dirty"
    else
        ALPHA_DIRTY="clean"
    fi
else
    ALPHA_SHA="unknown"
    ALPHA_BRANCH="unknown"
    ALPHA_DIRTY="not-a-git-repo"
fi

# Manifest — entries are paths relative to $ALPHA_DIR.
# Directories deploy recursively (with trailing slash semantics).
# See header for what's intentionally excluded (install-time scaffolding,
# data files, claude/gemini-specific skill directories).
MANIFEST=(
    "server.py"
    "engram_client.py"
    "engram_confidence.py"
    "viz_server.py"
    "hooks"
    "tools"
    "SKILL.md"
)

# Per-deploy report — check what would change before applying.
echo "=== ENGRAM deploy ==="
echo "  source:  $ALPHA_DIR ($ALPHA_BRANCH @ $ALPHA_SHA, $ALPHA_DIRTY)"
echo "  target:  $ENGRAM_HOME"
echo "  delete:  $([[ $DELETE = 1 ]] && echo yes || echo "no (use --delete to remove stale files)")"
echo "  mode:    $([[ $DRY_RUN = 1 ]] && echo "dry-run (no changes)" || echo "live (will apply)")"
if [[ "$ALPHA_DIRTY" = "dirty" ]]; then
    echo "  warning: alpha working tree has uncommitted changes — deploying them anyway"
fi
echo

CHANGES=0
PLAN_DETAILS=""
for entry in "${MANIFEST[@]}"; do
    src="$ALPHA_DIR/$entry"
    dst="$ENGRAM_HOME/$entry"
    if [[ ! -e "$src" ]]; then
        printf '  skip:      %s (not in alpha)\n' "$entry"
        continue
    fi
    if [[ ! -e "$dst" ]]; then
        printf '  NEW:       %s\n' "$entry"
        CHANGES=1
        continue
    fi
    # Compute diff via rsync --dry-run --itemize-changes
    rsync_args=(-ac --dry-run --itemize-changes)
    if [[ -d "$src" ]]; then
        [[ $DELETE = 1 ]] && rsync_args+=(--delete)
        diff_output="$(rsync "${rsync_args[@]}" "$src/" "$dst/" 2>&1 | grep -v '^$' | grep -vE '^sending incremental|^sent |^total size|^$' || true)"
    else
        diff_output="$(rsync "${rsync_args[@]}" "$src" "$dst" 2>&1 | grep -v '^$' | grep -vE '^sending incremental|^sent |^total size|^$' || true)"
    fi
    if [[ -z "$diff_output" ]]; then
        printf '  unchanged: %s\n' "$entry"
    else
        printf '  CHANGED:   %s\n' "$entry"
        echo "$diff_output" | sed 's/^/      /' | head -8
        CHANGES=1
    fi
done
echo

if [[ $CHANGES -eq 0 ]]; then
    echo "No changes needed. ENGRAM_HOME is up to date with alpha @ $ALPHA_SHA."
    exit 0
fi

if [[ $DRY_RUN = 1 ]]; then
    echo "Dry-run complete. Re-run without --dry-run to apply."
    exit 0
fi

if [[ $YES = 0 ]]; then
    read -r -p "Apply these changes? [y/N] " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
fi

# Apply
for entry in "${MANIFEST[@]}"; do
    src="$ALPHA_DIR/$entry"
    dst="$ENGRAM_HOME/$entry"
    [[ ! -e "$src" ]] && continue
    if [[ -d "$src" ]]; then
        mkdir -p "$dst"
        rsync_args=(-ac)
        [[ $DELETE = 1 ]] && rsync_args+=(--delete)
        rsync "${rsync_args[@]}" "$src/" "$dst/"
    else
        rsync -ac "$src" "$dst"
    fi
done

# Apply install-time template substitutions that install.sh would have done.
# These need to run after every deploy because rsync overwrites the substituted
# version with the literal-template version from alpha. Without this step, the
# daemon launcher silently fails (literal `{{PYTHON}}` is not a valid command).
# Source of truth: install.sh's sed lines.
PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null || echo "")"
if [[ -n "$PYTHON_BIN" && -f "$ENGRAM_HOME/hooks/start-engram-daemon.sh" ]]; then
    sed -i "s|{{PYTHON}}|$PYTHON_BIN|g" "$ENGRAM_HOME/hooks/start-engram-daemon.sh"
    echo "  ✓ Substituted {{PYTHON}} → $PYTHON_BIN in hooks/start-engram-daemon.sh"
fi

# Stamp version marker for forensics + future "what version is running?" queries
TS="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
cat > "$ENGRAM_HOME/.deployed-version" <<EOF
alpha_sha=$ALPHA_SHA
alpha_branch=$ALPHA_BRANCH
alpha_dirty=$ALPHA_DIRTY
deployed_at=$TS
deployed_from=$ALPHA_DIR
EOF

echo
echo "✓ Deployed alpha @ $ALPHA_SHA → $ENGRAM_HOME"
echo "  Version marker: $ENGRAM_HOME/.deployed-version"
echo
echo "Next step: restart the MCP server so it loads the new server.py."
echo "  - In Claude Code: /mcp restart engram (or kill the engram MCP process)."
echo "  - Hooks (context_tracker.py, etc.) reload per-fire automatically."
