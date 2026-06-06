#!/usr/bin/env bash
# tools/sync-cron.sh — splice the ENGRAM cron block into the user's crontab.
#
# Reads $ENGRAM_HOME/config.json (cadence block; install via
# tools/migration/migrate_config_v2.py) and the crontab template, renders the ENGRAM
# block via tools/render_cron.py, then splices it into the current
# crontab using `# BEGIN ENGRAM` / `# END ENGRAM` markers.
#
# Splice semantics (decided 2026-05-04 with Lei):
#   - If markers exist: replace ONLY the lines between them. Everything
#     outside the markers (other user cron entries, comments, env vars)
#     is preserved verbatim.
#   - If markers don't exist: append the rendered block to the end of
#     the existing crontab. Pre-existing user lines are kept.
#   - If the user has no crontab yet: install the rendered block as the
#     entire crontab.
#
# Idempotency: re-running with no config change is a no-op (the
# rendered block matches the existing block byte-for-byte; --diff exits 0
# with no output, --apply detects no diff and skips the install step).
#
# Usage:
#   tools/sync-cron.sh [--diff] [--dry-run] [--apply]
#                      [--config PATH] [--template PATH]
#                      [--agent-name NAME] [--counterpart-name NAME]
#                      [--engram-home PATH] [--agent-cli claude]
#
# Flags:
#   --diff      Print a unified diff of (current crontab) vs (new crontab)
#               and exit 0. Default mode.
#   --dry-run   Same as --diff (alias).
#   --apply     Install the new crontab via `crontab -`. Pre-checks via
#               diff and skips install if no changes are needed. Backs up
#               current crontab to $ENGRAM_HOME/cron-backups/ before
#               applying.
#
# Environment fallbacks (used when flags are not provided):
#   ENGRAM_HOME       (default: $HOME/.engram)
#   AGENT_NAME        (no default — required either via env or --agent-name)
#   COUNTERPART_NAME  (default: empty string)
#   AGENT_CLI         (default: claude)

set -euo pipefail

# ---------- defaults + flag parsing ----------

MODE="diff"
ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
AGENT_NAME="${AGENT_NAME:-}"
COUNTERPART_NAME="${COUNTERPART_NAME:-}"
AGENT_CLI="${AGENT_CLI:-claude}"
CONFIG_PATH=""
TEMPLATE_PATH=""

usage() {
    sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# //; s/^#$//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --diff|--dry-run)   MODE="diff"; shift ;;
        --apply)            MODE="apply"; shift ;;
        --config)           CONFIG_PATH="$2"; shift 2 ;;
        --template)         TEMPLATE_PATH="$2"; shift 2 ;;
        --agent-name)       AGENT_NAME="$2"; shift 2 ;;
        --counterpart-name) COUNTERPART_NAME="$2"; shift 2 ;;
        --engram-home)      ENGRAM_HOME="$2"; shift 2 ;;
        --agent-cli)        AGENT_CLI="$2"; shift 2 ;;
        -h|--help)          usage 0 ;;
        *) echo "ERROR: unknown flag '$1'" >&2; usage 2 ;;
    esac
done

if [[ -z "$AGENT_NAME" ]]; then
    echo "ERROR: AGENT_NAME must be set (env var or --agent-name)" >&2
    exit 2
fi

# Resolve paths. Prefer caller-provided. Fallback: $ENGRAM_HOME/config.json
# for config (the agent's live config), and the alpha source tree for the
# template (templates/ are not shipped to the runtime dir).
if [[ -z "$CONFIG_PATH" ]]; then
    CONFIG_PATH="$ENGRAM_HOME/config.json"
fi
if [[ -z "$TEMPLATE_PATH" ]]; then
    # Try $ENGRAM_HOME/crontab.template first (set up by install.sh), then
    # fall back to alpha source (useful in dev or when sync-cron.sh is run
    # from the alpha repo before install).
    if [[ -r "$ENGRAM_HOME/crontab.template" ]]; then
        TEMPLATE_PATH="$ENGRAM_HOME/crontab.template"
    else
        SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        TEMPLATE_PATH="$SCRIPT_DIR/../templates/crontab.template"
    fi
fi

if [[ ! -r "$CONFIG_PATH" ]]; then
    echo "ERROR: config not found at $CONFIG_PATH" >&2
    exit 1
fi
if [[ ! -r "$TEMPLATE_PATH" ]]; then
    echo "ERROR: template not found at $TEMPLATE_PATH" >&2
    exit 1
fi

# Locate render_cron.py (lives next to this script).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RENDERER="$SCRIPT_DIR/render_cron.py"
if [[ ! -x "$RENDERER" ]] && [[ ! -r "$RENDERER" ]]; then
    echo "ERROR: render_cron.py not found at $RENDERER" >&2
    exit 1
fi

PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "ERROR: python3 not found on PATH" >&2
    exit 1
fi

# ---------- render the new ENGRAM block ----------

NEW_BLOCK=$("$PYTHON_BIN" "$RENDERER" \
    --config "$CONFIG_PATH" \
    --template "$TEMPLATE_PATH" \
    --agent-name "$AGENT_NAME" \
    --counterpart-name "$COUNTERPART_NAME" \
    --engram-home "$ENGRAM_HOME" \
    --agent-cli "$AGENT_CLI")

# ---------- splice into existing crontab ----------

# Read current crontab (empty if user has none — `crontab -l` exits 1 in that case).
CURRENT="$(crontab -l 2>/dev/null || true)"

# Splice: replace lines between BEGIN/END markers with the new block. If
# markers absent, append. The shell-side splice avoids Python dependencies
# on crontab content; the heavy lifting was already done by render_cron.py.
splice() {
    local current="$1"
    local new_block="$2"

    if echo "$current" | grep -qF "# BEGIN ENGRAM"; then
        # Markers exist — replace block in-place using awk for clean
        # before/after preservation.
        echo "$current" | awk -v block="$new_block" '
            /^# BEGIN ENGRAM/ { in_block = 1; print block; next }
            /^# END ENGRAM/   { in_block = 0; next }
            !in_block         { print }
        '
    elif [[ -z "$current" ]]; then
        # No existing crontab — install just the block.
        echo "$new_block"
    else
        # Existing crontab without markers — append block at end.
        printf '%s\n%s\n' "$current" "$new_block"
    fi
}

# Defensive check #1: BEGIN/END markers must be balanced. The awk splice
# toggles `in_block` on each marker; an unmatched BEGIN (e.g., from a
# hand-edited crontab) silently drops every line from BEGIN to EOF. Refuse
# to proceed in that case so the user notices.
N_BEGIN=$(echo "$CURRENT" | grep -cE "^# BEGIN ENGRAM" || true)
N_END=$(echo "$CURRENT" | grep -cE "^# END ENGRAM" || true)
if [[ "$N_BEGIN" != "$N_END" ]]; then
    echo "ERROR: crontab has unmatched ENGRAM markers (BEGIN=$N_BEGIN, END=$N_END)." >&2
    echo "       The splice would silently drop lines after an unmatched BEGIN." >&2
    echo "       Edit your crontab (\`crontab -e\`) to balance the markers, then re-run." >&2
    exit 1
fi

NEW_CRONTAB="$(splice "$CURRENT" "$NEW_BLOCK")"

# Defensive check #2: if the user's existing crontab contains ENGRAM-looking
# lines OUTSIDE the BEGIN/END markers (i.e., a hand-rolled prod crontab
# from before sync-cron existed), --apply would silently duplicate them
# alongside the new managed block. Warn loudly. The user needs to either
# remove the un-bracketed lines or manually wrap them in markers before
# applying.
HAS_MARKERS=0
echo "$CURRENT" | grep -qF "# BEGIN ENGRAM" && HAS_MARKERS=1

if [[ $HAS_MARKERS -eq 0 ]]; then
    # Anchor on ENGRAM-pathed scripts only. Matching just `heartbeat.sh` /
    # `sleep.sh` would false-positive on a user's unrelated /home/them/
    # scripts/sleep.sh, blocking --apply for no good reason.
    UNBRACKETED_LINES=$(echo "$CURRENT" | grep -E "/\.engram(-[a-z]+)?/tools/(heartbeat|sleep|start-channel-dispatcher|start-telegram-dispatcher)\.sh" || true)
    if [[ -n "$UNBRACKETED_LINES" ]]; then
        echo "WARNING: Your existing crontab has ENGRAM-looking cron lines but no" >&2
        echo "         # BEGIN ENGRAM / # END ENGRAM markers. Running --apply will" >&2
        echo "         APPEND the rendered block alongside them, creating duplicates." >&2
        echo "" >&2
        echo "Existing un-bracketed ENGRAM lines:" >&2
        echo "$UNBRACKETED_LINES" | sed 's/^/  /' >&2
        echo "" >&2
        echo "Recommended: edit your crontab (\`crontab -e\`) to delete these lines" >&2
        echo "or wrap them in '# BEGIN ENGRAM' / '# END ENGRAM' markers, then re-run." >&2
        echo "" >&2
        if [[ "$MODE" = "apply" ]]; then
            echo "Refusing to apply. Re-run --apply once the unbracketed lines are gone." >&2
            exit 1
        fi
    fi
fi

# ---------- diff or apply ----------

if [[ "$MODE" = "diff" ]]; then
    if [[ "$CURRENT" = "$NEW_CRONTAB" ]]; then
        echo "✓ Crontab is already in sync with $CONFIG_PATH — no changes."
        exit 0
    fi
    echo "Planned crontab changes (current → rendered):"
    echo
    # diff -u returns 1 when files differ; that's not an error here, so
    # absorb it. set -euo pipefail would otherwise abort the script.
    diff -u <(echo "$CURRENT") <(echo "$NEW_CRONTAB") | sed -E 's/^/  /' || true
    echo
    echo "Re-run with --apply to install."
    exit 0
fi

# --apply path: skip if no change, otherwise back up + install.
if [[ "$CURRENT" = "$NEW_CRONTAB" ]]; then
    echo "✓ Crontab is already in sync — no changes applied."
    exit 0
fi

# Backup current crontab before mutation.
BACKUP_DIR="$ENGRAM_HOME/cron-backups"
mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUP_DIR/crontab-${AGENT_NAME}-${STAMP}.bak"
echo "$CURRENT" > "$BACKUP"
chmod 0640 "$BACKUP" 2>/dev/null || true
echo "  ✓ Backup: $BACKUP"

# Install via crontab -.
echo "$NEW_CRONTAB" | crontab -
echo "  ✓ New crontab installed for $AGENT_NAME"
echo
echo "Verification:"
crontab -l | grep -E "BEGIN ENGRAM|END ENGRAM|heartbeat\.sh|sleep\.sh" | sed 's/^/  /' || echo "  (unexpected — no ENGRAM lines found in installed crontab)"
