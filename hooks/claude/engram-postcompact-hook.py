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

Fix (2026-05-07, ob_NNNN): the PostCompact hook records the JSONL byte
offset AT POST-COMPACT TIME into ~/.engram/last-compact-at.json. The
tracker then reads only the post-offset portion of the JSONL to compute
current_tokens, and falls back to the legacy compact_boundary scan if the
marker file is missing or stale.
"""
import json
import os
import sys
import time
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


def _emit_hook_fire(session_id: str, transcript_path: str, duration_ms: int) -> None:
    """Emit engram.hook.fire event. Failure must not break the hook."""
    try:
        sys.path.insert(0, ENGRAM_HOME)
        from engram_log_emitter import Emitter
        _emitter = Emitter.init(
            session_id=session_id or "unknown",
            transcript_path=transcript_path or "",
        )
        _emitter.emit(
            event_type="engram.hook.fire",
            level=1,
            data={
                "hook_name": "engram-postcompact-hook",
                "hook_type": "PostCompact",
                "duration_ms": duration_ms,
                "exit_code": 0,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "context_mode": os.environ.get("ENGRAM_CONTEXT_MODE", "") or None,
            },
        )
    except Exception:
        pass


def main() -> None:
    _t0 = time.perf_counter()

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
        # No active JSONL to mark — defensive no-op.
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
        return
    try:
        byte_offset = os.path.getsize(jsonl_path)
    except OSError:
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
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

    _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))


if __name__ == "__main__":
    main()
