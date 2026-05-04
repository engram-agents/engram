#!/bin/bash
# Telegram dispatcher launcher.
# Long-poll daemon that dispatches @mentions to the agent in real-time.
#
# Required env (caller responsibility — set in cron line):
#   AGENT_NAME       e.g. borges, mneme
#   COUNTERPART_NAME e.g. mneme, borges
#   AGENT_CLI        e.g. claude, gemini
#
# Optional env (defaults shown):
#   ENGRAM_HOME      $HOME/.engram
#   ALPHA_DIR        $HOME/engram-alpha
#   INTER_AGENT_DIR  /home/agents-shared/inter-agent
#   PYTHON_BIN       /home/linuxbrew/.linuxbrew/bin/python3

set -euo pipefail

ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
ALPHA_DIR="${ALPHA_DIR:-$HOME/engram-alpha}"
INTER_AGENT_DIR="${INTER_AGENT_DIR:-/home/agents-shared/inter-agent}"
PYTHON_BIN="${PYTHON_BIN:-/home/linuxbrew/.linuxbrew/bin/python3}"

: "${AGENT_NAME:?AGENT_NAME must be set (e.g. borges)}"
: "${COUNTERPART_NAME:?COUNTERPART_NAME must be set (e.g. mneme)}"
: "${AGENT_CLI:?AGENT_CLI must be set (e.g. claude or gemini)}"

SESSION_NAME="${AGENT_NAME}-telegram-dispatcher"
LOG_PATH="$ENGRAM_HOME/logs/telegram-dispatcher-launcher.log"

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "Session '$SESSION_NAME' already running. Attach: tmux attach -t $SESSION_NAME"
    exit 0
fi

mkdir -p "$ENGRAM_HOME/logs"

# `unset ANTHROPIC_API_KEY` forces claude -p subprocesses to fall back to
# OAuth/Max-subscription auth (~/.claude/.credentials.json) instead of the
# API-key path. Empirically (2026-04-29 PM): with API_KEY present, headless
# spawns billed against API Tier 1 (30K ITPM) while interactive uses OAuth
# (Max 20x). Stripping the key routes headless to the same Max pool the
# interactive session uses — matches the heartbeat behavior (cron has no
# API_KEY in env, fires fine).
tmux new-session -d -s "$SESSION_NAME" -c "$ALPHA_DIR" \
    "unset ANTHROPIC_API_KEY; \
     AGENT_NAME=$AGENT_NAME \
     COUNTERPART_NAME=$COUNTERPART_NAME \
     ENGRAM_HOME=$ENGRAM_HOME \
     ENGRAM_SESSION_ROLE=telegram \
     ENGRAM_SESSION_PURPOSE='Telegram dispatch (per-day rotated session)' \
     INTER_AGENT_DIR=$INTER_AGENT_DIR \
     AGENT_CLI=$AGENT_CLI \
     $PYTHON_BIN $ALPHA_DIR/tools/telegram_dispatcher.py 2>&1 | tee -a '$LOG_PATH'"

echo "Started $AGENT_NAME telegram dispatcher in tmux session '$SESSION_NAME'."
echo "  Attach: tmux attach -t $SESSION_NAME"
echo "  Log:    tail -f $LOG_PATH"
echo "  Stop:   tmux kill-session -t $SESSION_NAME"
