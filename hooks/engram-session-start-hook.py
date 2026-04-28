#!/usr/bin/env python3
"""Session start: record active-session marker, reset context-tracker baseline,
clear sticky user identity, and inject session info into the conversation.

The active-session marker ($ENGRAM_HOME/active-session.json) is the canonical
pointer to the current JSONL. Fixes the turn-1 drowsiness bug where the
tracker picked newest-by-mtime globally and grabbed yesterday's file before
the new conversation's JSONL had flushed. Also gives observation tooling a
reliable transcript_path for citing the current conversation.
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from context_tracker import write_baseline

ENGRAM_HOME = os.environ.get("ENGRAM_HOME", os.path.expanduser("~/.engram"))
ACTIVE_SESSION_PATH = os.path.join(ENGRAM_HOME, "active-session.json")
CURRENT_USER_PATH = os.path.join(ENGRAM_HOME, "current_user.json")
WARM_BRIEFING_PATH = os.path.join(ENGRAM_HOME, "warm-briefing.md")
DAILY_HIGHLIGHTS_PATH = os.path.join(ENGRAM_HOME, "daily-highlights.md")


def write_marker(hook_input: dict) -> dict | None:
    session_id = hook_input.get("session_id")
    transcript_path = hook_input.get("transcript_path")
    if not session_id or not transcript_path:
        return None
    marker = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "source": hook_input.get("source"),
        "cwd": hook_input.get("cwd"),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        os.makedirs(os.path.dirname(ACTIVE_SESSION_PATH), exist_ok=True)
        with open(ACTIVE_SESSION_PATH, "w") as f:
            json.dump(marker, f)
    except OSError:
        return None
    return marker


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    marker = write_marker(hook_input)
    write_baseline(use_compact_boundary=True)

    if os.path.exists(CURRENT_USER_PATH):
        try:
            os.remove(CURRENT_USER_PATH)
        except OSError:
            pass

    lines = []
    if marker:
        lines.append(
            f"[Active session: {marker['session_id']} "
            f"(source={marker.get('source') or 'unknown'}) — "
            f"transcript at {marker['transcript_path']}]"
        )
    lines.append(
        f"[Session start reading — before responding to the first task, read "
        f"{WARM_BRIEFING_PATH} (relational context) and "
        f"{DAILY_HIGHLIGHTS_PATH} (yesterday's milestones + current focus).]"
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }))


if __name__ == "__main__":
    main()
