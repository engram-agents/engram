#!/usr/bin/env python3
"""Verify last night's coordinated sleep ran cleanly.

Called by heartbeat.sh on the first tick of a new NYC-local day. Reads
the sleep marker ($ENGRAM_HOME/sessions/last-sleep-success.json), classifies
the state, and writes a one-line status to:
  - stdout (captured by heartbeat.log)
  - $ENGRAM_HOME/sleep-verify.log (dedicated audit trail)

If the state is RED (last sleep > 25h ago at first-tick-of-day, meaning
the sleep cron didn't fire), additionally writes an inter-agent
notice so the counterpart's heartbeat picks it up on its next tick and
can decide whether to escalate.

Classification:
  GREEN   — marker present, hours_since ≤ 25. Sleep ran cleanly.
  RED     — marker present, hours_since > 25. Cron likely missed; host
            may have been off, or sleep job errored.
  UNKNOWN — marker missing entirely. Either first-ever boot before any
            sleep ran, or marker was deleted. No alarm — silent record.

Required env (caller responsibility):
  AGENT_NAME       e.g. borges, mneme, aleph

Optional env (defaults shown):
  ENGRAM_HOME      derived from script location ($script/../) if unset
  INTER_AGENT_DIR  /home/agents-shared/inter-agent

Pure stdlib, no MCP / claude / network dependencies — this MUST be
robust to the very failure modes it's checking for.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Derive home from THIS script's location (script lives at <home>/tools/), so
# each agent's copy reads/writes its own data by construction. Env override
# remains for sandbox testing or when called from alpha clone.
ENGRAM_HOME = Path(os.environ.get("ENGRAM_HOME") or Path(__file__).resolve().parent.parent)
MARKER_PATH = ENGRAM_HOME / "sessions" / "last-sleep-success.json"
VERIFY_LOG = ENGRAM_HOME / "sleep-verify.log"
RED_THRESHOLD_HOURS = 25
AGENT_NAME = os.environ.get("AGENT_NAME", "agent")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_log(line: str) -> None:
    VERIFY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with VERIFY_LOG.open("a") as f:
        f.write(line + "\n")


def write_inter_agent_red(line: str, hours_since: float, marker: dict) -> None:
    """Write a structured notice to $INTER_AGENT_DIR so the counterpart sees it."""
    inter_agent_dir = Path(os.environ.get("INTER_AGENT_DIR", "/home/agents-shared/inter-agent"))
    if not inter_agent_dir.is_dir():
        return
    now = datetime.now(timezone.utc)
    fname = now.strftime(f"%Y-%m-%dT%H-%M-%SZ_{AGENT_NAME}_sleep-verify-red.md")
    body = f"""---
from: {AGENT_NAME}
timestamp: {now.strftime("%Y-%m-%dT%H:%M:%SZ")}
type: alert
topic: sleep-verification
urgency: warm
---

First-heartbeat-of-day sleep verification flagged RED:

- Last coordinated sleep: {marker.get('completed_at', '?')}
- Hours since: {hours_since:.1f} (threshold: {RED_THRESHOLD_HOURS}h)
- Last turn advance: {marker.get('turn_advanced_to', '?')}
- Last cohort size: {marker.get('nodes_consolidated', '?')} nodes

Likely cause: the coordinated sleep cron did not fire on {AGENT_NAME} side
(host was off, or sleep.sh errored). {AGENT_NAME}'s interactive session
will also see this via the SessionStart sleep-status banner on next fresh
terminal — this notice is the cross-agent signal in case the counterpart's
heartbeat picks it up first and wants to relay to the user.

— {AGENT_NAME} (autonomous heartbeat verifier, no LLM in this loop)
"""
    try:
        (inter_agent_dir / fname).write_text(body)
    except OSError:
        pass


def main() -> int:
    ts = utc_now_iso()

    if not MARKER_PATH.exists():
        line = f"{ts} UNKNOWN no marker at {MARKER_PATH} — first ever boot or marker deleted"
        print(line)
        append_log(line)
        return 0

    try:
        with MARKER_PATH.open() as f:
            marker = json.load(f)
        completed_at_str = marker["completed_at"]
        completed_at = datetime.fromisoformat(completed_at_str.replace("Z", "+00:00"))
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as e:
        line = f"{ts} UNKNOWN marker unreadable: {type(e).__name__}: {e}"
        print(line)
        append_log(line)
        return 0

    hours_since = (datetime.now(timezone.utc) - completed_at).total_seconds() / 3600
    turn = marker.get("turn_advanced_to", "?")
    nodes = marker.get("nodes_consolidated", "?")

    if hours_since <= RED_THRESHOLD_HOURS:
        line = (
            f"{ts} GREEN sleep verified — completed_at={completed_at_str} "
            f"hours_since={hours_since:.1f} turn={turn} nodes={nodes}"
        )
        print(line)
        append_log(line)
        return 0

    line = (
        f"{ts} RED sleep verification failed — completed_at={completed_at_str} "
        f"hours_since={hours_since:.1f} (>{RED_THRESHOLD_HOURS}h threshold) "
        f"turn={turn} nodes={nodes}"
    )
    print(line)
    append_log(line)
    write_inter_agent_red(line, hours_since, marker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
