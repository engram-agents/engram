#!/bin/bash
# agent-session-run.sh — runs INSIDE the detached tmux session started by
# agent-session-launch.sh. Launches the agent's platform session via
# agentctl and — the reason this script exists — makes an early death
# OBSERVABLE instead of evaporating with the pane.
#
# Background (2026-07-25 08:15 rehearsal): agentctl rejected the multi-line
# boot charge and exited 2 within milliseconds; the tmux session closed,
# taking the only copy of the error with it, while every systemd unit
# reported green. Three rehearsals in a row died in this launcher→claude gap
# (07-22 timer Unit=, 07-24 PATH, 07-25 newline guard), each invisible.
# This runner closes the gap from the inside:
#   - every launch + exit is appended to ~/.engram/logs/autostart-launch.log
#   - agentctl/claude stderr is tee'd into the same log
#   - on a NONZERO exit the pane is held open (sleep infinity) so the
#     operator can `tmux attach -t agent-<name>` and read the corpse.
#     The next morning's launcher refuses to double-launch over a held
#     pane — deliberately: a crashed session must be looked at, not
#     silently replaced (same posture as the unit's Restart=no).
#
# Usage: agent-session-run.sh <agent-name> <charge-file> <cwd>

set -uo pipefail   # NO -e: we must reach the exit-code handling below.

AGENT_NAME="${1:?Usage: agent-session-run.sh <agent-name> <charge-file> <cwd>}"
CHARGE_FILE="${2:?missing <charge-file>}"
CD_TARGET="${3:-$HOME}"

# Defensive PATH prepend for standalone runs; under the normal chain the
# launcher already exported this before starting the tmux server.
export PATH="$HOME/.local/bin:$PATH"

LOG_DIR="$HOME/.engram/logs"
LOG="$LOG_DIR/autostart-launch.log"
mkdir -p "$LOG_DIR"

# gh-stack ensure (#2391): the stacked-PR merge verb must be discoverable via
# `gh --help` on every seat, and the extension is per-user — a seat without it
# cannot merge a stack member at all. Idempotent (list-then-install), quiet,
# and NON-FATAL by design: a seat with no network or no gh still boots, and
# the CLAUDE.md rule section remains the knowledge carrier either way.
# `timeout` is GNU coreutils (absent on stock macOS); the command -v guard
# makes its absence skip the ensure rather than error — acceptable because
# this script is reached only via agentctl, which today runs on Linux seats.
if command -v gh >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1 \
        && ! timeout 10 gh extension list 2>/dev/null | grep -q 'github/gh-stack'; then
    echo "[$(date -Is)] [$AGENT_NAME] gh-stack extension absent — installing (30s cap)" >> "$LOG"
    timeout 30 gh extension install github/gh-stack >>"$LOG" 2>&1 \
        || echo "[$(date -Is)] [$AGENT_NAME] WARN: gh-stack extension install failed or timed out (non-fatal; see CLAUDE.md 'Stacked PRs')" >> "$LOG"
fi

platform="$(agentctl __detect-platform-as-self)" || {
    echo "[$(date -Is)] [$AGENT_NAME] ERROR: could not detect platform for boot session" >> "$LOG"
    echo "[autostart] could not detect the agent platform; refusing to guess boot arguments" >&2
    exit 1
}

# -r asks Claude to register remote control. Codex has one daemon-backed TUI
# in this pane instead; agentctl accepts and silently consumes -r there. Keep
# the Claude boot contract while omitting the unnecessary Codex alias.
case "$platform" in
    claude) session_aliases=(-d -r) ;;
    codex) session_aliases=(-d) ;;
    *)
        echo "[$(date -Is)] [$AGENT_NAME] ERROR: unsupported boot platform '$platform'" >> "$LOG"
        echo "[autostart] unsupported agent platform '$platform'; refusing to guess boot arguments" >&2
        exit 1
        ;;
esac

echo "[$(date -Is)] [$AGENT_NAME] launching: agentctl session $AGENT_NAME ${session_aliases[*]} --as-self --cd $CD_TARGET --charge-file $CHARGE_FILE" >> "$LOG"

# stderr rides through tee into the log AND stays on the pane. stdout is left
# alone — the platform CLI's interactive UI owns it.
agentctl session "$AGENT_NAME" "${session_aliases[@]}" --as-self \
    --cd "$CD_TARGET" --charge-file "$CHARGE_FILE" \
    2> >(tee -a "$LOG" >&2)
ec=$?

echo "[$(date -Is)] [$AGENT_NAME] session process exited ec=$ec" >> "$LOG"

if [[ $ec -ne 0 ]]; then
    echo ""
    echo "[autostart] LAUNCH FAILED (exit $ec) — full log: $LOG"
    echo "[autostart] holding this pane open for inspection; kill it with:"
    echo "            tmux kill-session -t agent-$AGENT_NAME"
    sleep infinity
fi

exit "$ec"
