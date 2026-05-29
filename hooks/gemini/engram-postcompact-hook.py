#!/usr/bin/env python3
"""Post-compact: write the last-compact-at marker so the context tracker can
correctly compute drowsiness against the post-compact byte-offset.

See hooks/claude/engram-postcompact-hook.py for the full rationale (ob_1532).
Mirrored here for the Gemini sidecar.
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from context_tracker import find_active_jsonl, write_baseline

# Multi-install precedence: prefer ENGRAM_DATA_DIR (server's canonical), fall back
# to ENGRAM_HOME (the conventional name), then default. Resolves the multi-install
# split-brain where operator sets one var but not the other (#354).
ENGRAM_HOME = (
    os.environ.get("ENGRAM_DATA_DIR")
    or os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)
MARKER_PATH = os.path.join(ENGRAM_HOME, "last-compact-at.json")


def main() -> None:
    # Issue #140: read session_id + transcript_path from this hook's stdin
    # payload to resolve THIS session's JSONL race-free, instead of reading
    # a single shared global marker.
    session_id: str | None = None
    transcript_path: str | None = None
    try:
        raw = sys.stdin.read()
        if raw:
            payload = json.loads(raw)
            sid = payload.get("session_id")
            if isinstance(sid, str) and sid:
                session_id = sid
            tp = payload.get("transcript_path")
            if isinstance(tp, str) and tp:
                transcript_path = tp
    except (ValueError, json.JSONDecodeError, OSError):
        pass

    if transcript_path and os.path.exists(transcript_path):
        jsonl_path = transcript_path
    else:
        jsonl_path = find_active_jsonl(session_id)
    if not jsonl_path or not os.path.exists(jsonl_path):
        return
    try:
        byte_offset = os.path.getsize(jsonl_path)
    except OSError:
        return
    marker = {
        "jsonl_path": jsonl_path,
        "byte_offset": byte_offset,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(MARKER_PATH, "w") as f:
            json.dump(marker, f)
    except OSError:
        pass

    try:
        write_baseline(use_compact_boundary=False)
    except TypeError:
        write_baseline()


if __name__ == "__main__":
    main()
