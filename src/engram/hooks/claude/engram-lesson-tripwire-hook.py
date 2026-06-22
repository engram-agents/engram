#!/usr/bin/env python3
"""
PreToolUse hook for Bash: surfaces lesson tripwires at action-moment.

PROBLEM SOLVED
==============
The prompt-time recall hook (engram-surface-hook.py) fires at UserPromptSubmit
and matches lessons against the user's prompt text. But action-moment lessons —
ones whose true retrieval cue is a specific CLI command about to be executed —
systematically fail to surface because the encoding cue (the command) is absent
from the prompt. This is encoding specificity (Tulving & Thomson 1973): retrieval
requires reinstatement of the encoding context.

HOW THIS HOOK WORKS
===================
Fires on every Bash PreToolUse event. Queries active lessons that have a
`situation_pattern` field in their metadata JSON. Matches the pending command
against each pattern (case-insensitive regex). For any match, injects the
lesson's `scaffolding_nudge` as additionalContext before the command runs.
Complements (does not replace) the prompt-time recall hook.

TIER: T2 (Convenience) — degrades gracefully on DB unavailability; never blocks.
Issue: #1203 — lesson-tripwire encoding-specificity gap.
"""
import os as _os, sys as _sys
# Guard against source: directory marketplace double-fire (#1066).
_plugin_root = _os.environ.get("CLAUDE_PLUGIN_ROOT", "")
_engram_home = _os.environ.get("ENGRAM_HOME") or _os.path.expanduser("~/.engram")
if _plugin_root.startswith(_os.path.join(_engram_home, "marketplace") + _os.sep):
    _sys.exit(0)

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ENGRAM_HOME = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
DB_PATH = Path(ENGRAM_HOME) / "knowledge.db"

_QUERY = """
SELECT
    json_extract(metadata, '$.scaffolding_nudge') AS nudge,
    json_extract(metadata, '$.situation_pattern')  AS pattern
FROM nodes
WHERE type = 'lesson'
  AND is_current = 1
  AND memory_status = 'active'
  AND json_extract(metadata, '$.situation_pattern') IS NOT NULL
  AND json_extract(metadata, '$.situation_pattern') != ''
"""


def load_tripwires(db_path: Path = DB_PATH):
    """Return list of (pattern_str, nudge_str) from active lessons with situation_pattern."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        rows = conn.execute(_QUERY).fetchall()
        conn.close()
        return [(row[1], row[0]) for row in rows if row[0] and row[1]]
    except Exception:
        return []


def check_command(command: str, tripwires) -> list:
    """Return list of scaffolding nudges whose situation_pattern matches the command."""
    hits = []
    for pattern, nudge in tripwires:
        try:
            if re.search(pattern, command, re.IGNORECASE):
                hits.append(nudge)
        except re.error:
            pass  # malformed pattern — skip silently
    return hits


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        sys.exit(0)

    try:
        if hook_input.get("tool_name") != "Bash":
            sys.exit(0)

        # Both "tool_input" and "input" are observed depending on hook version.
        ti = hook_input.get("tool_input") or hook_input.get("input") or {}
        command = ti.get("command", "")
        if not isinstance(command, str) or not command.strip():
            sys.exit(0)

        tripwires = load_tripwires()
        if not tripwires:
            sys.exit(0)

        hits = check_command(command, tripwires)
        if not hits:
            sys.exit(0)

        nudge_lines = "\n".join(f"  • {h}" for h in hits)
        context = (
            "[lesson-tripwire] Action-moment pattern matched — remember:\n"
            f"{nudge_lines}"
        )
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": context,
            }
        }
        print(json.dumps(response))
        sys.exit(0)

    except Exception:
        sys.exit(0)  # hook failures must never block a Bash call


if __name__ == "__main__":
    main()
