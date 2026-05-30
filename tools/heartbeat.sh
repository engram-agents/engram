#!/bin/bash
# ENGRAM heartbeat trigger — runs Claude Code in headless mode for proactive
# maintenance. Single-source-of-truth in alpha; per-agent identity carried
# via env vars set in the agent's crontab line.
#
# Required env (caller responsibility):
#   AGENT_NAME       e.g. borges, mneme, aleph
#   COUNTERPART_NAME e.g. mneme (Borges's counterpart), borges (Mneme's)
#
# Optional env (defaults shown):
#   ENGRAM_HOME      $HOME/.engram
#   ALPHA_DIR        $HOME/engram-alpha
#   INTER_AGENT_DIR  /home/agents-shared/inter-agent
#   PYTHON_BIN       /home/linuxbrew/.linuxbrew/bin/python3

ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
ALPHA_DIR="${ALPHA_DIR:-$HOME/engram-alpha}"
INTER_AGENT_DIR="${INTER_AGENT_DIR:-/home/agents-shared/inter-agent}"
PYTHON_BIN="${PYTHON_BIN:-/home/linuxbrew/.linuxbrew/bin/python3}"

: "${AGENT_NAME:?AGENT_NAME must be set (e.g. borges)}"
: "${COUNTERPART_NAME:?COUNTERPART_NAME must be set (e.g. mneme)}"

# Run from alpha so CLAUDE.md context loads correctly.
cd "$ALPHA_DIR" || exit 1

# Strip ANTHROPIC_API_KEY before any claude -p invocation (per the ANTHROPIC_API_KEY-stripping discipline).
# Cron's bare env doesn't have it, but ad-hoc manual runs from a shell that
# does — `unset` makes the symmetry uniform across all four launchers.
unset ANTHROPIC_API_KEY

# Poll Telegram for new messages from counterpart (mirrors to $INTER_AGENT_DIR)
"$PYTHON_BIN" "$ALPHA_DIR/tools/telegram_bot.py" poll

# Per-day-persistent heartbeat session: all heartbeat ticks within one
# NYC-local day share one --session-id, accumulating coordination memory
# across the day. Nightly sleep cron closes out the day; tomorrow's first
# tick spawns a fresh session.
TODAY=$(date +%Y-%m-%d)
SESSIONS_DIR="$ENGRAM_HOME/sessions"
DAY_ID_FILE="$SESSIONS_DIR/heartbeat-$TODAY.id"
mkdir -p "$SESSIONS_DIR"

if [ -f "$DAY_ID_FILE" ]; then
    SESSION_UUID=$(cat "$DAY_ID_FILE")
    FIRST_TICK_OF_DAY=0
else
    SESSION_UUID=$(cat /proc/sys/kernel/random/uuid)
    echo "$SESSION_UUID" > "$DAY_ID_FILE"
    FIRST_TICK_OF_DAY=1
fi

# First-tick-of-day verification: did last night's coordinated sleep
# run cleanly? Pure bash + python helper, no LLM tokens — must be
# robust to the very failure modes it's checking for. Output goes to
# stdout (heartbeat.log) and $ENGRAM_HOME/sleep-verify.log; if RED, also
# drops a notice in $INTER_AGENT_DIR/ so counterpart's heartbeat sees it.
if [ "$FIRST_TICK_OF_DAY" = "1" ]; then
    INTER_AGENT_DIR="$INTER_AGENT_DIR" \
    AGENT_NAME="$AGENT_NAME" \
        "$PYTHON_BIN" "$ALPHA_DIR/tools/verify_sleep.py" || true
fi

# Resume only if a JSONL exists for this UUID — first tick of the day
# creates via --session-id, subsequent ticks resume.
PROJECTS_ROOT="$HOME/.claude/projects"
if compgen -G "$PROJECTS_ROOT/*/$SESSION_UUID.jsonl" > /dev/null; then
    SESSION_FLAG="--resume"
else
    SESSION_FLAG="--session-id"
fi

echo "Perform your heartbeat routine. Read \$ENGRAM_HOME/HEARTBEAT.md and act on its instructions. Respond with HEARTBEAT_OK if no action was taken." | \
  env ENGRAM_SESSION_ROLE=heartbeat \
      ENGRAM_SESSION_PURPOSE="Coordination heartbeat for $TODAY (NYC local)" \
      claude -p \
        "$SESSION_FLAG" "$SESSION_UUID" \
        --permission-mode bypassPermissions \
        --setting-sources user,project,local \
        --add-dir "$ENGRAM_HOME" \
        --add-dir "$INTER_AGENT_DIR"

# Liveness pulse — single one-shot per fire, post inbox processing.
# Resolves the silent-ack-cumulative-drift incident. Threshold-based: at most
# one pulse per silent stretch, decoupling liveness from substance.
AGENT_NAME="$AGENT_NAME" \
COUNTERPART_NAME="$COUNTERPART_NAME" \
ENGRAM_HOME="$ENGRAM_HOME" \
INTER_AGENT_DIR="$INTER_AGENT_DIR" \
  "$PYTHON_BIN" "$ALPHA_DIR/tools/inter_agent_pulse.py" tick \
    >> "$ENGRAM_HOME/logs/inter-agent-pulse.log" 2>&1 || true
