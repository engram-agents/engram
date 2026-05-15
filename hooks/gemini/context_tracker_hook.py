#!/usr/bin/env python3
"""UserPromptSubmit hook wrapper for the drowsiness-framed context tracker.

Reads `transcript_path` from this hook's stdin payload (the harness emits
session_id + transcript_path on every hook event) and threads it through
to estimate_usage(). This is the Issue #140 fix: when multiple sessions
run concurrently, the legacy global ~/.engram/active-session.json marker
races between SessionStart writes, and consumers reading it see whichever
session started most recently. Per-event stdin makes each hook fire see
its OWN session's transcript, race-free.

Falls back to the marker file via estimate_usage(transcript_path=None) if
stdin is empty/malformed — preserves pre-#140 behavior in degraded cases.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from context_tracker import estimate_usage, format_drowsiness

transcript_path: str | None = None
try:
    raw = sys.stdin.read()
    if raw:
        payload = json.loads(raw)
        tp = payload.get("transcript_path")
        if isinstance(tp, str) and tp:
            transcript_path = tp
except (ValueError, json.JSONDecodeError, OSError):
    pass  # Fall through; estimate_usage will use the marker fallback.

usage = estimate_usage(transcript_path=transcript_path)
if usage:
    msg = format_drowsiness(usage)
    if msg:
        print(msg)
