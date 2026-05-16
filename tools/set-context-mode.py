#!/usr/bin/env python3
"""Set the per-session marker's `context_mode` field from the agent itself.

WHY THIS EXISTS
---------------
The context-window mode (200K vs 1M) is critical for accurate drowsiness
reporting (~5x ceiling difference). The SessionStart hook can detect mode
from these signals:

  1. ENGRAM_CONTEXT_MODE env var (explicit user override)
  2. ENGRAM_CONTEXT_MODE-populated per-session marker context_mode field
  3. Mid-session magnitude heuristics (current_tokens > MODE_BOUNDARY)
  4. Prior auto-compaction preTokens

For a FRESH session in 1M mode below MODE_BOUNDARY, the first three signals
are absent and the hook falls through to the "200k" default — misreporting
drowsiness against a 152K ceiling when actual ceiling is ~807K. Issue #72.

The Claude Code JSONL records the BASE model id (e.g. "claude-opus-4-7")
WITHOUT the context-mode suffix (the `[1m]` tag lives in the system-prompt
text only, not in JSONL records or env vars). So the SessionStart hook
literally cannot detect the variant from observable infrastructure.

But the AGENT can see its own system prompt at session start — the harness
tells the agent "You are powered by the model named Opus 4.7 (1M context).
The exact model ID is claude-opus-4-7[1m]." This script is the bridge:
the agent reads its own variant, calls this script with the mode, and the
per-session marker gets populated. The context_tracker's existing
precedence-step-2 (marker context_mode field) then picks it up on every
subsequent invocation.

USAGE
-----
  set-context-mode.py 1m
  set-context-mode.py 200k

CALLED BY
---------
The session-start discipline in CLAUDE.md.template: on first prompt of any
session, if the per-session marker's context_mode field is empty, the agent
reads its system prompt for the [1m] / [200k] suffix and invokes this
script with the matching argument. One-shot per session; no-op on re-invocation.

NO-OP CASES
-----------
- CLAUDE_CODE_SESSION_ID env var unset → exit 0 (silent — script ran in a
  non-Claude-Code context, e.g. test harness)
- Per-session marker doesn't exist → exit 0 (silent — SessionStart hook
  hasn't fired yet; very early invocation)
- Marker already has context_mode populated → exit 0 (silent — idempotent)

ERROR CASES
-----------
- Mode argument missing or not in {"200k", "1m"} → stderr + exit 2
- Marker exists but malformed JSON → stderr + exit 1
"""

import json
import os
import sys
from pathlib import Path

VALID_MODES = {"200k", "1m"}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: set-context-mode.py <200k|1m>", file=sys.stderr)
        return 2

    mode = sys.argv[1].strip().lower()
    if mode not in VALID_MODES:
        print(f"error: mode must be one of {sorted(VALID_MODES)}; got '{mode}'", file=sys.stderr)
        return 2

    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not session_id:
        # Non-Claude-Code invocation (test harness, gemini session, etc.). Silent no-op.
        return 0

    engram_home = Path(os.environ.get("ENGRAM_HOME", str(Path.home() / ".engram")))
    marker_path = engram_home / "sessions" / f"{session_id}.json"
    if not marker_path.exists():
        # SessionStart hook hasn't written the marker yet. Silent no-op.
        return 0

    try:
        with marker_path.open() as f:
            marker = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: could not read marker at {marker_path}: {exc}", file=sys.stderr)
        return 1

    if marker.get("context_mode"):
        # Already populated. Idempotent no-op.
        return 0

    marker["context_mode"] = mode
    try:
        with marker_path.open("w") as f:
            json.dump(marker, f, indent=2)
    except OSError as exc:
        print(f"error: could not write marker at {marker_path}: {exc}", file=sys.stderr)
        return 1

    print(f"context_mode set to '{mode}' for session {session_id[:8]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
