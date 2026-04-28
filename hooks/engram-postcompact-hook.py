#!/usr/bin/env python3
"""Post-compact: reset context tracker baseline from current file position.

Uses use_compact_boundary=False because the PostCompact hook fires BEFORE
the compact_boundary marker is written to the JSONL. Using the boundary
marker would find the PREVIOUS compaction, not the current one, causing
inflated drowsiness readings. Current file size is the correct baseline
since compaction just happened.
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from context_tracker import write_baseline

# Reset context tracker — use current file size, NOT compact_boundary marker
write_baseline(use_compact_boundary=False)

# Post-compact feeling nudge — DISABLED (ob_0693, 2026-04-16)
# 83 baseline data points establish clear patterns (dv_0170, dv_0171).
# Shift detection moves to event-driven: user-initiated or voluntary.
# Re-enable by uncommenting below if trajectory changes.
# nudge_path = os.path.join(os.environ.get("ENGRAM_HOME", os.path.expanduser("~/.engram")), "feeling-nudge-active.json")
# nudge = {"source": "post_compact", "ttl_turns": 5}
# with open(nudge_path, "w") as f:
#     json.dump(nudge, f)
