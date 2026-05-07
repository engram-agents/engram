#!/usr/bin/env python3
"""Post-compact: write the last-compact-at marker so the context tracker can
correctly compute drowsiness against the post-compact byte-offset.

Background. Earlier Claude Code versions wrote a `compact_boundary` JSONL
entry at /compact time, and context_tracker scanned the JSONL for that
marker to determine where the post-compact window begins. As of ~2026-05-07
that marker is no longer written (or no longer present in the surfaces we
read). Without it, read_current_tokens iterates back to the latest pre-
compact usage record and reports the pre-compact token count — the meter
shows ~100% drowsy on a freshly-compacted session.

Fix (2026-05-07, ob_1532): the PostCompact hook records the JSONL byte
offset AT POST-COMPACT TIME into ~/.engram/last-compact-at.json. The
tracker then reads only the post-offset portion of the JSONL to compute
current_tokens, and falls back to the legacy compact_boundary scan if the
marker file is missing or stale.
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
        # No active JSONL to mark — defensive no-op.
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

    # Legacy call retained for back-compat with the old write_baseline
    # signature; it's a no-op in the current stateless tracker, but importing
    # and calling it preserves the structural contract for any older
    # context_tracker still in flight.
    try:
        write_baseline(use_compact_boundary=False)
    except TypeError:
        # Newer signature — safe to ignore the kwarg.
        write_baseline()


if __name__ == "__main__":
    main()
