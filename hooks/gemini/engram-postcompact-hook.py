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

ENGRAM_HOME = os.environ.get("ENGRAM_HOME", os.path.expanduser("~/.engram"))
MARKER_PATH = os.path.join(ENGRAM_HOME, "last-compact-at.json")


def main() -> None:
    jsonl_path = find_active_jsonl()
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
