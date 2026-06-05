#!/usr/bin/env python3
"""Stop hook: remind agent to check if last response had recordable content.

Emits engram.hook.fire event for per-hook fire metadata (alpha #175, DESIGN.md §4.3).
"""
import json
import os
import sys
import time


ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)


def main() -> None:
    _t0 = time.perf_counter()

    # Read stdin for session_id and transcript_path (Stop hook protocol).
    session_id = "unknown"
    transcript_path = ""
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
    except Exception:
        pass

    output = (
        "[ENGRAM Write Check: Did your last response contain a decision, insight,"
        "or design choice worth recording? If so, write to ENGRAM now "
        "(observation, derivation, question, or conjecture). If not, carry on.]"
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": output}}))

    _duration_ms = int((time.perf_counter() - _t0) * 1000)

    # Emit engram.hook.fire event. Failure must not break the hook.
    try:
        sys.path.insert(0, ENGRAM_HOME)
        from engram_log_emitter import Emitter
        _emitter = Emitter.init(
            session_id=session_id,
            transcript_path=transcript_path,
        )
        _emitter.emit(
            event_type="engram.hook.fire",
            level=1,
            data={
                "hook_name": "engram-stop-hook",
                "hook_type": "Stop",
                "duration_ms": _duration_ms,
                "exit_code": 0,
                "stdout_bytes": len(output.encode("utf-8")),
                "stderr_bytes": 0,
            },
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
