#!/usr/bin/env python3
"""Write the sleep-success marker after a coordinated sleep cycle completes.

Called by the engram-sleep skill at the post-turn-advance step
(claude Step 5b) immediately after engram_advance_turn
succeeds. The marker is the canonical
signal to:
  - engram-session-start-hook.py (surfaces sleep-status banner if
    marker is older than the threshold)

Usage:
    write_sleep_marker.py <turn_advanced_to> <nodes_consolidated> <cohort_start_at>

cohort_start_at: UTC ISO timestamp of the prior sleep (or first node in cohort
if no prior sleep). turn_advanced_to: integer turn the checkpoint advanced to.
nodes_consolidated: integer count of nodes in the consolidated cohort.

The marker is meant to be ephemeral runtime state — gitignored so it doesn't
clutter the .engram git working tree.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <turn_advanced_to> <nodes_consolidated> <cohort_start_at>",
              file=sys.stderr)
        return 2
    try:
        turn = int(sys.argv[1])
        nodes = int(sys.argv[2])
    except ValueError:
        print(f"turn and nodes must be integers, got {sys.argv[1]!r}, {sys.argv[2]!r}",
              file=sys.stderr)
        return 2
    cohort_start = sys.argv[3]

    # Default to ~/.engram regardless of where this script is installed.
    # Path(__file__).resolve() would follow symlinks (tools/ is a symlink in
    # plugin topology) and land in the plugin root, not the data home. ENGRAM_HOME
    # env override remains for sandbox / multi-agent testing.
    engram_home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
    sessions_dir = os.path.join(engram_home, "sessions")
    marker_path = os.path.join(sessions_dir, "last-sleep-success.json")

    marker = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "turn_advanced_to": turn,
        "nodes_consolidated": nodes,
        "cohort_start_at": cohort_start,
    }
    os.makedirs(sessions_dir, exist_ok=True)
    tmp_path = marker_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(marker, f, indent=2)
    os.replace(tmp_path, marker_path)
    print(f"wrote {marker_path}: turn {turn}, {nodes} nodes consolidated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
