#!/bin/bash
# ENGRAM coordinated daily sleep — fires once per day per the coordinated-
# sleep design. The SINGLE turn-advance per day across all of an agent's
# roles (interactive, heartbeat, telegram, sleep). Other roles use nap-mode
# only. The dream cohort spans the full day across all roles via
# engram_history(since=<prev-sleep-timestamp>).
#
# Required env (caller responsibility):
#   AGENT_NAME       e.g. borges, mneme, aleph
#
# Optional env (defaults shown):
#   ENGRAM_HOME      $HOME/.engram
#   ALPHA_DIR        $HOME/engram-alpha
#   INTER_AGENT_DIR  /home/agents-shared/inter-agent
#   PYTHON_BIN       /home/linuxbrew/.linuxbrew/bin/python3
#
# Why fresh session per night (no resume): the dream cycle starts with a
# clean context window so the agent can survey today's full cohort
# without compaction pressure from accumulated heartbeat traffic.

set -euo pipefail

ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
ALPHA_DIR="${ALPHA_DIR:-$HOME/engram-alpha}"
INTER_AGENT_DIR="${INTER_AGENT_DIR:-/home/agents-shared/inter-agent}"
PYTHON_BIN="${PYTHON_BIN:-/home/linuxbrew/.linuxbrew/bin/python3}"

: "${AGENT_NAME:?AGENT_NAME must be set (e.g. borges)}"

# Run from alpha so CLAUDE.md context loads correctly.
cd "$ALPHA_DIR" || exit 1

# `unset ANTHROPIC_API_KEY` forces claude -p subprocesses to fall back to
# OAuth/Max-subscription auth (~/.claude/.credentials.json) instead of the
# API-key path. Without this, headless spawns bill against API Tier 1
# (30K ITPM) instead of the Max 20x pool the interactive session uses.
# Permanent lesson: ls_0018 — every headless claude -p launcher MUST strip
# this key. (Burned 2026-04-29 PM by exactly this trap.)
unset ANTHROPIC_API_KEY

# Self-check: skip if a recent sleep already consolidated everything.
# Handles the "manual /engram-sleep just before cron → cron fires" case
# (or two manual sleeps close together): if marker is fresh AND there's
# barely any new material since, this run is redundant — exit cleanly.
SLEEP_MARKER="$ENGRAM_HOME/sessions/last-sleep-success.json"
ENGRAM_DB="$ENGRAM_HOME/knowledge.db"

SKIP_REASON=$("$PYTHON_BIN" - "$SLEEP_MARKER" "$ENGRAM_DB" <<'PYEOF'
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

marker_path, db_path = sys.argv[1], sys.argv[2]

# No marker yet (first ever run) → run normally
if not os.path.exists(marker_path):
    sys.exit(0)

try:
    with open(marker_path) as f:
        marker = json.load(f)
    completed_at_str = marker["completed_at"]
    completed_at = datetime.fromisoformat(completed_at_str.replace("Z", "+00:00"))
except (OSError, KeyError, ValueError, json.JSONDecodeError):
    sys.exit(0)  # malformed marker → run defensively

hours_since = (datetime.now(timezone.utc) - completed_at).total_seconds() / 3600
if hours_since >= 4:
    sys.exit(0)  # ≥4h since last sleep, run normally

try:
    conn = sqlite3.connect(db_path, timeout=2.0)
    cur = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE created_at > ?",
        (completed_at_str,),
    )
    nodes_since = cur.fetchone()[0]
    conn.close()
except Exception:
    sys.exit(0)  # DB unreachable → run defensively

if nodes_since < 5:
    print(f"last sleep {hours_since:.1f}h ago, only {nodes_since} new nodes since")
PYEOF
)

if [ -n "$SKIP_REASON" ]; then
    echo "$(date +%Y-%m-%dT%H:%M:%S%z) sleep.sh: skip ($SKIP_REASON)"
    exit 0
fi

TODAY=$(date +%Y-%m-%d)
SESSION_UUID=$(cat /proc/sys/kernel/random/uuid)
SESSIONS_DIR="$ENGRAM_HOME/sessions"
mkdir -p "$SESSIONS_DIR"
echo "$SESSION_UUID" > "$SESSIONS_DIR/sleep-$TODAY.id"

PROMPT="Run the engram-sleep skill to consolidate today's work across all roles (interactive, heartbeat, telegram).

This is the SINGLE turn-advance for $TODAY (NYC local) per the coordinated-sleep design — all other roles today used nap-mode only. The dream cohort spans the full day across all roles.

Follow the skill in order: nap → engram_reflect → day-wide synthesis (engram_history since the previous sleep) → work the dream agenda → handle the dream-review feeling nudge → engram_advance_turn → update warm-briefing session note → reconcile today's history file at \$ENGRAM_HOME/history/$TODAY.md → report.

When done, exit cleanly."

echo "$PROMPT" | \
  env ENGRAM_SESSION_ROLE=sleep \
      ENGRAM_SESSION_PURPOSE="Coordinated daily sleep $TODAY (NYC local)" \
      claude -p \
        --session-id "$SESSION_UUID" \
        --permission-mode bypassPermissions \
        --setting-sources user,project,local \
        --add-dir "$ENGRAM_HOME" \
        --add-dir "$INTER_AGENT_DIR"
