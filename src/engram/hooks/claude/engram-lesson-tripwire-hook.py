#!/usr/bin/env python3
"""
PreToolUse hook (Bash + MCP): surfaces lesson tripwires at action-moment.

PROBLEM SOLVED
==============
The prompt-time recall hook (engram-surface-hook.py) fires at UserPromptSubmit
and matches lessons against the user's prompt text. But action-moment lessons —
ones whose true retrieval cue is a specific CLI command or MCP tool call about
to be executed — systematically fail to surface because the encoding cue is
absent from the prompt. This is encoding specificity (Tulving & Thomson 1973):
retrieval requires reinstatement of the encoding context.

THREE-WAY TAXONOMY OF LESSON RETRIEVAL CUES (forum thread #135):
  Locus 1 — Bash-command-cued: cue is a shell command shape.
             Caught by this hook (matches against the Bash command string).
  Locus 2 — MCP-tool-cued: cue is an MCP tool invocation.
             Caught by this hook (matches against tool_name + serialized args).
  Locus 3 — Semantic-content-cued: cue is the content of an assertion, not any
             tool call. Not catchable at PreToolUse; stays with prompt-time hook.

HOW THIS HOOK WORKS
===================
Fires on every Bash or MCP-tool PreToolUse event. Queries active lessons that
have a `situation_pattern` field in their metadata JSON. For Bash calls, matches
the pending command against each pattern. For MCP calls, matches against
"{tool_name} {json(args)}" — so a pattern like `engram_add_observation` fires
before that specific MCP tool call. For any match, injects the lesson's
`scaffolding_nudge` as additionalContext before the tool call runs.
Complements (does not replace) the prompt-time recall hook.

TIER: T2 (Convenience) — degrades gracefully on DB unavailability; never blocks.
Issues: #1203 (Bash/locus-1) + #1297 (MCP/locus-2) — lesson-tripwire encoding-specificity gap.
"""
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


def check_command(match_target: str, tripwires) -> list:
    """Return list of scaffolding nudges whose situation_pattern matches match_target."""
    hits = []
    for pattern, nudge in tripwires:
        try:
            if re.search(pattern, match_target, re.IGNORECASE):
                hits.append(nudge)
        except re.error:
            pass  # malformed pattern — skip silently
    return hits


def build_match_target(hook_input: dict) -> str:
    """Build the string to match situation_pattern against, or "" to skip.

    Bash calls   → the shell command string.
    MCP calls    → "{tool_name} {compact_json_args}" so patterns like
                   `engram_add_observation` match the tool name substring.
    Other calls  → "" (caller exits early).
    """
    tool_name = hook_input.get("tool_name", "")
    ti = hook_input.get("tool_input") or hook_input.get("input") or {}

    if tool_name == "Bash":
        return ti.get("command", "") if isinstance(ti.get("command"), str) else ""

    if tool_name.startswith("mcp__"):
        try:
            args_str = json.dumps(ti, separators=(",", ":"), ensure_ascii=False)
        except Exception:
            args_str = ""
        return f"{tool_name} {args_str}"

    return ""


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        sys.exit(0)

    try:
        match_target = build_match_target(hook_input)
        if not match_target or not match_target.strip():
            sys.exit(0)

        tripwires = load_tripwires()
        if not tripwires:
            sys.exit(0)

        hits = check_command(match_target, tripwires)
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
        sys.exit(0)  # hook failures must never block a tool call


if __name__ == "__main__":
    main()
