#!/usr/bin/env python3
"""Compute walltime for a task span from a Claude Code JSONL transcript.

Model: a task span is one or more [user-message → assistant-segment] pairs.
Per-segment walltime = last_assistant_timestamp − user_message_timestamp.
Sum across segments. This includes Claude's generation latency (agent work
time) and excludes the gap between an assistant-segment's end and the next
user message (Lei's typing-back time).

Usage:
    compute_task_time.py --start-marker "let's do Piece C" \\
                         --end-marker "Now one meta data point"
    compute_task_time.py --session <id> --start-msg-index 42 --end-msg-index 58

Markers are verbatim substrings of user-message text. First matching user
message is the segment's start; the segment BEFORE the end-marker's user
message closes the task. If --end-marker is omitted, the task ends at the
last user message in the transcript.

Output (JSON, stdout):
    {
      "actual_minutes": float,
      "segment_count": int,
      "segments": [{"user_ts": "...", "last_assistant_ts": "...", "minutes": float}],
      "gaps_over_5min": [{"between_segment_indexes": [i, i+1], "minutes": float}],
      "warnings": [str],
      "start_user_ts": str,
      "end_user_ts": str (or null if open-ended)
    }
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime
from typing import Any

SESSIONS_DIR = os.path.expanduser("~/.engram/sessions")
PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/*/")


def resolve_transcript(session_id: str | None, explicit_path: str | None) -> str:
    if explicit_path:
        return os.path.abspath(os.path.expanduser(explicit_path))
    if session_id:
        marker_path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
        if os.path.exists(marker_path):
            with open(marker_path) as f:
                marker = json.load(f)
            return marker["transcript_path"]
        for project_dir in glob.glob(PROJECTS_GLOB):
            candidate = os.path.join(project_dir, f"{session_id}.jsonl")
            if os.path.exists(candidate):
                return candidate
        raise SystemExit(f"No transcript found for session {session_id}")
    raise SystemExit("Pass --session or --transcript; issue #140 retired the global active-session marker.")


def load_entries(transcript_path: str) -> list[dict[str, Any]]:
    entries = []
    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def entry_role(entry: dict[str, Any]) -> str | None:
    """Return 'user', 'assistant', or None for meta entries."""
    t = entry.get("type")
    if t in ("user", "assistant"):
        return t
    return None


def entry_text(entry: dict[str, Any]) -> str:
    """Extract searchable text from a user message entry."""
    msg = entry.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("type") == "tool_result":
                    tr = c.get("content", "")
                    if isinstance(tr, str):
                        parts.append(tr)
        return "\n".join(parts)
    return ""


def entry_is_genuine_user_prompt(entry: dict[str, Any]) -> bool:
    """True iff this entry is a real user prompt (not a tool_result passthrough)."""
    if entry_role(entry) != "user":
        return False
    msg = entry.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                return False
        return True
    return False


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def find_marker_index(
    entries: list[dict[str, Any]],
    marker: str,
    start_from: int = 0,
) -> int | None:
    for i in range(start_from, len(entries)):
        e = entries[i]
        if not entry_is_genuine_user_prompt(e):
            continue
        if marker in entry_text(e):
            return i
    return None


def compute(
    entries: list[dict[str, Any]],
    start_index: int,
    end_index: int | None,
) -> dict[str, Any]:
    """Compute task walltime as sum of per-segment walltimes.

    A segment starts at a genuine user prompt and ends at the last assistant
    entry before the next genuine user prompt. The task span runs from
    `start_index` to (exclusive) `end_index`; if `end_index` is None, runs
    to end of transcript.
    """
    if end_index is None:
        end_index = len(entries)
    segments = []
    warnings = []
    i = start_index
    while i < end_index:
        e = entries[i]
        if entry_is_genuine_user_prompt(e):
            user_ts_raw = e.get("timestamp")
            if not user_ts_raw:
                i += 1
                continue
            user_ts = parse_ts(user_ts_raw)
            # Walk forward to last assistant entry before the NEXT genuine user prompt
            last_assistant_ts = None
            j = i + 1
            while j < end_index:
                e2 = entries[j]
                if entry_is_genuine_user_prompt(e2):
                    break
                if entry_role(e2) == "assistant" and e2.get("timestamp"):
                    last_assistant_ts = parse_ts(e2["timestamp"])
                j += 1
            if last_assistant_ts is None:
                warnings.append(f"segment at index {i} has no assistant response; skipped")
                i = j
                continue
            minutes = (last_assistant_ts - user_ts).total_seconds() / 60.0
            if minutes < 0:
                warnings.append(f"segment at index {i} has negative walltime ({minutes:.2f} min); skipped")
            else:
                segments.append({
                    "user_ts": user_ts.isoformat(),
                    "last_assistant_ts": last_assistant_ts.isoformat(),
                    "minutes": round(minutes, 2),
                    "user_index": i,
                })
            i = j
        else:
            i += 1

    actual_minutes = round(sum(s["minutes"] for s in segments), 2)

    # Gaps: between (last_assistant_ts of segment N) and (user_ts of segment N+1)
    gaps = []
    for k in range(len(segments) - 1):
        end_ts = parse_ts(segments[k]["last_assistant_ts"])
        next_start_ts = parse_ts(segments[k + 1]["user_ts"])
        gap_min = (next_start_ts - end_ts).total_seconds() / 60.0
        if gap_min > 5:
            gaps.append({
                "between_segment_indexes": [k, k + 1],
                "minutes": round(gap_min, 2),
            })

    return {
        "actual_minutes": actual_minutes,
        "segment_count": len(segments),
        "segments": segments,
        "gaps_over_5min": gaps,
        "warnings": warnings,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", help="Session ID; resolved via ~/.claude/projects/*/<id>.jsonl")
    ap.add_argument("--transcript", help="Explicit path to a JSONL transcript (overrides --session)")
    ap.add_argument("--start-marker", help="Verbatim substring of the user message that STARTS the task")
    ap.add_argument("--end-marker", help="Verbatim substring of the user message that ENDS the task (exclusive). Omit to run to end of transcript.")
    ap.add_argument("--start-msg-index", type=int, help="Explicit entry index for task start (overrides --start-marker)")
    ap.add_argument("--end-msg-index", type=int, help="Explicit entry index for task end (exclusive, overrides --end-marker)")
    args = ap.parse_args()

    transcript_path = resolve_transcript(args.session, args.transcript)
    entries = load_entries(transcript_path)

    if args.start_msg_index is not None:
        start_index = args.start_msg_index
    elif args.start_marker:
        resolved = find_marker_index(entries, args.start_marker)
        if resolved is None:
            raise SystemExit(f"start-marker not found: {args.start_marker!r}")
        start_index = resolved
    else:
        raise SystemExit("Provide --start-marker or --start-msg-index")

    if args.end_msg_index is not None:
        end_index = args.end_msg_index
    elif args.end_marker:
        resolved_end = find_marker_index(entries, args.end_marker, start_from=start_index + 1)
        if resolved_end is None:
            raise SystemExit(f"end-marker not found after start: {args.end_marker!r}")
        end_index = resolved_end
    else:
        end_index = None

    result = compute(entries, start_index, end_index)
    result["transcript"] = transcript_path
    result["start_user_ts"] = entries[start_index].get("timestamp")
    result["end_user_ts"] = entries[end_index].get("timestamp") if end_index is not None and end_index < len(entries) else None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
