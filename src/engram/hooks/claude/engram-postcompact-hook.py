#!/usr/bin/env python3
"""Post-compact: write the last-compact-at marker so the context tracker can
correctly compute drowsiness against the post-compact byte-offset.

Background. Claude Code writes a `compact_boundary` JSONL entry (with full
compactMetadata: preTokens, postTokens, durationMs) at /compact time; the
context_tracker scans the JSONL for that marker to locate the post-compact
window. The marker was transiently absent ~2026-05-07 but is confirmed back
in live sessions (live-verified 2026-06-09). The PostCompact hook writes the
JSONL byte offset at compact time into ~/.engram/last-compact-at.json as a
correctness backstop — for sessions where compact_boundary is absent or
stale the tracker falls back to the marker; for normal sessions it is a
belt-and-suspenders efficiency hedge (avoids rescanning from the top).

This hook does NOT render additionalContext continuity surfaces (focus list,
starred letters) despite an earlier design (#1655/#1710) attempting to do so:
PostCompact is a side-effects-only Claude Code hook event and cannot inject
additionalContext at all (confirmed against CC docs, forum #241, 2026-07-09)
— the JSON this hook used to print with hookEventName=="PostCompact" was
silently discarded by the harness every time. The live focus list now
renders at SessionStart instead (source=="compact" fires there too — see
engram-session-start-hook.py, #1732); starred letters were already covered
there unconditionally and never needed to move.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from context_tracker import find_active_jsonl
from _hooklib import atomic_write_json, resolve_runtime_dir

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)
MARKER_PATH = os.path.join(ENGRAM_HOME, "last-compact-at.json")

# Module-scope emitter resolve+insert (gh#2570 §1/§3): this hook previously
# had no resolver at all -- its emit site did a bare
# sys.path.insert(0, ENGRAM_HOME), which shadows the plugin's live copy of
# engram_log_emitter.py whenever a stale scatter copy sits in ENGRAM_HOME.
_RUNTIME_DIR = resolve_runtime_dir(ENGRAM_HOME, "engram_log_emitter.py")
if _RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_DIR)

# alpha #2574 WS3: separate resolver for engram_telemetry_store.py -- per
# _hooklib.resolve_runtime_dir's own docstring, "one call per module you
# actually import; do not collapse two different imports onto one sentinel
# just because they happen to live in the same directory today" (gh#2570).
_TELEMETRY_RUNTIME_DIR = resolve_runtime_dir(ENGRAM_HOME, "engram_telemetry_store.py")
if _TELEMETRY_RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _TELEMETRY_RUNTIME_DIR)
try:
    from engram_telemetry_store import write_telemetry_record
except Exception:
    def write_telemetry_record(manifest_id, event_type, fields=None):  # pragma: no cover -- import-failure fallback
        return False

# alpha #2574 WS3: this hook's TRIGGER-ENTRY site literal (participation
# manifest_id) for engram.telemetry.reached -- see main()'s first line.
_MAIN_MANIFEST_ID = "hooks/claude/engram-postcompact-hook.py:main"

_HOOK_FIRE_SITE = "postcompact-hook.hook_fire"


def _emit_hook_fire(session_id: str, transcript_path: str, duration_ms: int) -> None:
    """Emit engram.hook.fire event. Failure must not break the hook.

    alpha #2574 WS3: construction (import + Emitter.init) is narrowed to its
    own try/except so an INIT_FAILED outcome is recordable and distinct from
    an ordinary DROPPED emit (docs/2574-telemetry-design.md §1/§5).
    """
    _emitter = None
    try:
        from engram_log_emitter import Emitter
        _emitter = Emitter.init(
            session_id=session_id or "unknown",
            transcript_path=transcript_path or "",
        )
    except Exception:
        write_telemetry_record(
            manifest_id=_HOOK_FIRE_SITE,
            event_type="engram.hook.fire",
            fields={"outcome": "init_failed"},
        )
        return

    _uid = _emitter.emit(
        event_type="engram.hook.fire",
        level=1,
        data={
            "hook_name": "engram-postcompact-hook",
            "hook_type": "PostCompact",
            "duration_ms": duration_ms,
            "exit_code": 0,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
        },
        site="postcompact-hook.hook_fire",
    )
    write_telemetry_record(
        manifest_id=_HOOK_FIRE_SITE,
        event_type="engram.hook.fire",
        fields={"outcome": "delivered" if _uid is not None else "dropped"},
    )


def main() -> None:
    # alpha #2574 WS3: `reached` -- unconditional, before any guard/early
    # return in this function.
    write_telemetry_record(manifest_id=_MAIN_MANIFEST_ID, event_type="engram.telemetry.reached", fields={})

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
        atomic_write_json(MARKER_PATH, marker)
    except OSError:
        pass

    # No additionalContext is emitted here — see module docstring: PostCompact
    # cannot inject additionalContext at all, so there is nothing useful to
    # print. The marker write above is this hook's entire job.
    _duration_ms = int((time.perf_counter() - _t0) * 1000)
    _emit_hook_fire(session_id, transcript_path, _duration_ms)


if __name__ == "__main__":
    main()
