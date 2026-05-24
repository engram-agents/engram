#!/bin/bash
# ENGRAM sleep — MANUAL CATCH-UP only as of 2026-05-14 (alpha #133 PR C).
#
# Sleep is now interactive-first: the agent runs `engram-sleep` at
# end-of-day in the user's active session (typically surfaced by the
# end-of-day-detector hook on wrap-up phrases). The cron-
# scheduled invocation that used to drive this script every night has been
# removed (see templates/crontab.template and tools/render_cron.py).
#
# This script remains on disk for the catch-up case: if yesterday's sleep
# was missed (host off at midnight, disconnected day, etc.), run this
# manually to consolidate the accumulated cohort. It spawns a fresh
# `claude -p` session that drives engram-sleep (two-phase: Phase A cohort
# completion then Phase B consolidation) per the two-routine architecture
# (dv_0636).
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
# Why fresh session for catch-up: the dream cycle starts with a clean
# context window so the agent can survey today's full cohort without
# compaction pressure from accumulated heartbeat traffic.

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

PROMPT="Run engram-sleep to consolidate today's work across all roles (interactive, heartbeat, telegram). This is the SINGLE turn-advance for $TODAY (NYC local) per the coordinated-sleep design — all other roles today used nap-mode only.

NOTE: This script is for MANUAL catch-up invocation only. The cron-scheduled path was removed 2026-05-14 (alpha #133 PR C — interactive sleep is now canonical). You're reading this because someone (you, presumably) invoked sleep.sh by hand, typically to consolidate a cohort whose end-of-day sleep was missed.

PHASE A — engram-sleep Phase A (pre-turn-advance, this-turn content):
  - Walk the day's full cohort via engram_history(mode='edits', action='created', since=<prev-sleep-timestamp>)
  - File any missed nodes (cross-burst derivations, late-day insights) — these belong to this turn
  - Rotate warm-briefing 'From this session' block comprehensively (full day arc)
  - Reconcile today's history file at \$ENGRAM_HOME/history/$TODAY.md + commit
  - Strategically reconcile your blocked-on-user queue file (canonical filename \$ENGRAM_HOME/ask-<user>.md per the engram-sleep skill's Phase A Step 4) — git-log walk + state verify; deferred items → ask-<user>-backlog.md; commit alongside history (no-op if the file doesn't exist on this install)
  - Hand off to Phase B

PHASE B — engram-sleep Phase B (consolidation + turn-advance):
  - Step 0: dispatch dream-fairies in parallel (six fairies via Agent tool with subagent_type=engram-dream-fairy and run_in_background=true, one per scan category 1–6, each writing to a unique \$ENGRAM_HOME/dream/$TODAY-fairy-{N}-{slug}.md path)
  - Step 1: engram_reflect — read the dream agenda
  - Step 2: integrate fairy reports as they land
  - Step 3: work the dream agenda (resolve / supersede / promote / refute — NO new awake-state synthesis here; that was bedtime's job)
  - Step 4: dream-review feeling nudge
  - Step 5: engram_advance_turn — single daily turn-advance
  - Step 5b: write sleep-success marker via tools/write_sleep_marker.py
  - Step 6: write dream-record at \$ENGRAM_HOME/history/dream/$TODAY.md + commit
  - Step 7: report

If subagent_type=engram-dream-fairy is unrecognized in this cron-spawned session, log the failure in the Step 7 report and proceed with the reflect-driven flow. Don't block sleep on fairy availability.

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
