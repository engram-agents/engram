#!/bin/bash
# clio-wake-run.sh — runs INSIDE the detached tmux session started by
# clio-wake-launch.sh. Mirrors ../agent-session-run.sh's structure: makes an
# early death OBSERVABLE (logs launch/exit, tees platform stderr, holds a
# failed pane open for autopsy) instead of letting it evaporate with the
# pane. Single-seat, Claude-only — no platform-detection branch needed.
#
# Usage: clio-wake-run.sh <charge-dir> <cwd>

set -uo pipefail   # NO -e: we must reach the exit-code handling below.

CHARGE_DIR="${1:?Usage: clio-wake-run.sh <charge-dir> <cwd>}"
CD_TARGET="${2:-$HOME}"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

LOG_DIR="$HOME/.engram/logs"
LOG="$LOG_DIR/clio-wake.log"
mkdir -p "$LOG_DIR"

CHARGE_FILE="$CHARGE_DIR/wake-charge-clio.md"
if [[ ! -r "$CHARGE_FILE" || ! -s "$CHARGE_FILE" ]]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: wake charge '$CHARGE_FILE' is unreadable or empty" >> "$LOG"
    exit 1
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] launching: agentctl session clio -d -r --as-self --cd $CD_TARGET --charge-file $CHARGE_FILE" >> "$LOG"

# stderr rides through tee into the log AND stays on the pane. stdout is left
# alone — the platform CLI's interactive UI owns it.
agentctl session clio -d -r --as-self \
    --cd "$CD_TARGET" --charge-file "$CHARGE_FILE" \
    2> >(tee -a "$LOG" >&2)
ec=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] session process exited ec=$ec" >> "$LOG"

if [[ $ec -ne 0 ]]; then
    echo ""
    echo "[clio-wake] LAUNCH FAILED (exit $ec) — full log: $LOG"
    echo "[clio-wake] holding this pane open for inspection; kill it with:"
    echo "            tmux kill-session -t clio-wake"
    sleep 2147483647
fi

exit "$ec"
