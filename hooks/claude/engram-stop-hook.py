#!/usr/bin/env python3
"""Stop hook: remind agent to check if last response had recordable content.

Emits engram.hook.fire event for per-hook fire metadata (alpha #175, DESIGN.md §4.3).

Idle-suppression (issue #840)
-----------------------------
Problem: before #824, this hook emitted plain text on stdout, which Claude Code
discarded as invalid hook output — so it was silently mute. #824 wrapped the
nudge in the strict-JSON envelope (hookSpecificOutput.additionalContext),
delivering it to the model for the first time. That exposed a second problem:
on contentless turns where the agent replies "nothing to record," the hook fires
again (Stop fires after every assistant turn), creating a self-sustaining
nudge→no-op→nudge loop with NO user input between turns (observed 5 consecutive
fires, 2026-06-05). Habituation risk: a tripwire firing on no-ops trains the
agent to dismiss it.

Fix — delta-scan suppression: before emitting the nudge, stat the transcript
and check whether any tool_use events appeared since the last fire. If none
did, the turn was purely textual (the agent was speaking, not acting) — suppress
the nudge. Update the stored offset on every fire (emit or suppress) so the
next fire only scans the new delta.

Delta-scan beats last-message inspection for a timing reason: Stop fires before
the final assistant message text block flushes into the JSONL transcript (a
known flush race — hooks needing latest in-process state must use the stdin
payload or their own marker file, never JSONL tail-scans). But tool_use records
flush at execution time, during the turn, so the delta reliably reflects tool
activity even when the final text hasn't landed yet.

Fail-open rule: the suppression check is an optimization, not a blocker. Any
exception in the check path — missing file, corrupt state, I/O error — causes
the nudge to emit. Suppression must never become the default under uncertainty.
The advisory-probe pattern in engram-session-start-hook.py `_check_mcp_health`
is the in-repo precedent for this discipline.
"""
import json
import os
import sys
import time

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)

# State file for idle suppression. Stores transcript_path + byte_offset from
# the previous fire so we can read only the delta on the next fire.
_NUDGE_STATE_FILE = os.path.join(ENGRAM_HOME, ".write-nudge-last-fire.json")

# Substring present in every tool_use JSONL entry. A post BODY mentioning this
# string would be inside an assistant text block (a different JSONL key), so
# spurious matches in the safe direction: a false positive EMITS the nudge,
# which is correct (over-nudging beats under-nudging). False negatives for
# suppression are fine — we never suppress when uncertain.
_TOOL_USE_MARKER = b'"type":"tool_use"'


def _should_suppress(transcript_path: str) -> bool:
    """Return True iff the transcript delta since last fire contains NO tool_use.

    Fail-open: any exception returns False (emit the nudge).
    """
    try:
        if not transcript_path:
            return False  # no transcript → unknown → emit

        # Read stored state.
        stored_path: str | None = None
        stored_offset: int = 0
        try:
            with open(_NUDGE_STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
            stored_path = state.get("transcript_path")
            stored_offset = int(state.get("byte_offset", 0))
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            # Missing or corrupt state file — first fire ever or after session
            # rotation. Treat as unknown → emit.
            return False

        # New session or path changed → emit.
        if stored_path != transcript_path:
            return False

        # Stat the transcript.
        try:
            current_size = os.path.getsize(transcript_path)
        except OSError:
            return False  # can't stat → emit

        # Stored offset beyond current size → rotation/compaction → emit.
        if stored_offset > current_size:
            return False

        # No new bytes since last fire → no tool activity, no text — idle turn.
        if stored_offset == current_size:
            return True  # suppress: nothing happened at all

        # Read only the delta.
        try:
            with open(transcript_path, "rb") as f:
                f.seek(stored_offset)
                delta = f.read(current_size - stored_offset)
        except OSError:
            return False  # can't read → emit

        # Suppress only when delta is a known-clean window with no tool_use.
        return _TOOL_USE_MARKER not in delta

    except Exception:  # noqa: BLE001
        # Fail-open: any unexpected error → emit the nudge.
        return False


def _update_state(transcript_path: str) -> None:
    """Write the current EOF offset to the state file. Failure is silenced."""
    try:
        offset = 0
        if transcript_path:
            try:
                offset = os.path.getsize(transcript_path)
            except OSError:
                pass
        state = {"transcript_path": transcript_path, "byte_offset": offset}
        tmp = _NUDGE_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _NUDGE_STATE_FILE)
    except Exception:  # noqa: BLE001
        pass


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

    # Idle-suppression check (issue #840). Wrap entirely in try/except so any
    # failure here cannot prevent the nudge from emitting.
    suppress = False
    try:
        suppress = _should_suppress(transcript_path)
    except Exception:  # noqa: BLE001
        suppress = False  # fail-open

    if suppress:
        # Silent no-op: update state so next fire scans the correct delta.
        _update_state(transcript_path)
        # Empty stdout is the canonical no-op per #824/#832 contract.
        # The emitter call is skipped intentionally on the suppressed path —
        # no meaningful hook event to record.
        return

    output = (
        "[ENGRAM Write Check: Did your last response contain a decision, insight,"
        "or design choice worth recording? If so, write to ENGRAM now "
        "(observation, derivation, question, or conjecture). If not, carry on.]"
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": output}}))

    # Update state after a real emit so the next fire scans only new bytes.
    _update_state(transcript_path)

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
