#!/usr/bin/env python3
"""UserPromptSubmit hook wrapper for the drowsiness-framed context tracker.

Reads `session_id` and `transcript_path` from this hook's stdin payload
(Claude Code emits both on every hook event) and threads them through to
estimate_usage(). This is the Issue #140 fix: when multiple Claude
sessions run concurrently, each hook fire reads its OWN session's
transcript and per-session marker (~/.engram/sessions/<session_id>.json),
not a single shared global marker. No clobber possible.

Falls back gracefully (estimate_usage handles None for either arg) if
stdin is empty/malformed.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from context_tracker import estimate_usage, format_drowsiness

transcript_path: str | None = None
session_id: str | None = None
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
    pass  # Fall through; estimate_usage handles missing args.

usage = estimate_usage(transcript_path=transcript_path, session_id=session_id)
if usage:
    msg = format_drowsiness(usage)
    if msg:
        print(msg)
