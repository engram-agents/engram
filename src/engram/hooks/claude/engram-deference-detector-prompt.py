#!/usr/bin/env python3
"""UserPromptSubmit hook: surface deference-detector marker as next-turn context.

Reads the marker written by engram-deference-detector-stop.py, emits a
short additionalContext block flagging the deference phrases caught in
my last response, then clears the marker so it fires once per detection.

Mechanism mirrors the antml-repair surfacing (additionalContext via
hookSpecificOutput) so the catch shows up in the same shape as other
behavioral hooks. Best-effort: any failure swallows silently.

Emits engram.hook.fire event for per-hook fire metadata (alpha #175, DESIGN.md §4.3).
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)
DEFERENCE_MARKER_PATH = os.path.join(ENGRAM_HOME, "deference-detected.json")
LOOP_MARKER_PATH = os.path.join(ENGRAM_HOME, "loop-mode.json")
DEFERENCE_COOLDOWN_PATH = os.path.join(ENGRAM_HOME, "deference-cooldown-at.txt")

# ---------------------------------------------------------------------------
# loop_prompt import — SSoT for the loop-wake marker.
#
# Resolve tools/ in BOTH deploy topologies (same pattern as
# engram-time-bar-hook.py / engram-baton-prompt-hook.py):
#   Source:  src/engram/hooks/claude/<name>.py → src/engram/tools/
#   Plugin:  <root>/hooks/<name>.py             → <root>/tools/
# A fixed parents[N] overshoots in the deployed (flattened) tree.
# Prefer $CLAUDE_PLUGIN_ROOT/tools when set; else walk parents and take the
# first dir that actually contains loop_prompt.py.
#
# FAIL-SAFE: if the import fails for any reason the hook must NOT crash.
# Fall back to the locally-defined _LOOP_WAKE_MARKER_FALLBACK constant so
# prefix-based classification still works.  A regression test asserts this
# fallback equals loop_prompt.LOOP_WAKE_MARKER so the two can't silently
# diverge (#1594).
# ---------------------------------------------------------------------------

_LOOP_WAKE_MARKER_FALLBACK: str = "<loop-wake>"

_lp_candidates = []
_lp_plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
if _lp_plugin_root:
    _lp_candidates.append(Path(_lp_plugin_root) / "tools")
for _lp_parent in Path(__file__).resolve().parents:
    _lp_candidates.append(_lp_parent / "tools")
_LP_TOOLS_DIR = next(
    (c for c in _lp_candidates if (c / "loop_prompt.py").exists()), None
)
if _LP_TOOLS_DIR is not None and str(_LP_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_LP_TOOLS_DIR))
try:
    from loop_prompt import LOOP_WAKE_MARKER as _LOOP_WAKE_MARKER
except Exception:
    _LOOP_WAKE_MARKER = _LOOP_WAKE_MARKER_FALLBACK


def _is_cron_prompt(prompt: str, loop_marker_path: str) -> bool:
    """Return True if the prompt looks like a cron-fired heartbeat, not a real user message."""
    stripped = prompt.strip()
    # SSoT loop-wake marker (format_loop_prompt prepends this; see tools/loop_prompt.py).
    if stripped.startswith(_LOOP_WAKE_MARKER):
        return True
    # Autonomous-loop sentinels — kept for back-compat with loops that predate
    # the format_loop_prompt SSoT (#1594).
    if stripped in ("<<autonomous-loop>>", "<<autonomous-loop-dynamic>>"):
        return True
    # Canonical ScheduleWakeup stub prefix — loop continuations that don't set
    # loop_prompt in the marker (covers markers written by the standard
    # engram-loop skill which uses the stub "Loop continuation. Read ...").
    if stripped.startswith("Loop continuation."):
        return True
    # Compare against stored loop_prompt in loop-mode.json (present if loop started
    # with the updated engram-loop skill that writes loop_prompt to the marker)
    try:
        with open(loop_marker_path) as f:
            marker = json.load(f)
        stored = marker.get("loop_prompt", "")
        if stored and stripped == stored.strip():
            return True
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return False


def _emit_hook_fire(session_id: str, transcript_path: str, duration_ms: int, stdout_bytes: int) -> None:
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
                "hook_name": "engram-deference-detector-prompt",
                "hook_type": "UserPromptSubmit",
                "duration_ms": duration_ms,
                "exit_code": 0,
                "stdout_bytes": stdout_bytes,
                "stderr_bytes": 0,
            },
        )
    except Exception:
        pass


def main() -> None:
    _t0 = time.perf_counter()

    # Parse stdin for session_id + transcript_path + prompt (hook protocol).
    session_id = "unknown"
    transcript_path = ""
    prompt_text = ""
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
            p = payload.get("prompt")
            if isinstance(p, str):
                prompt_text = p
    except Exception:
        pass

    if not os.path.exists(LOOP_MARKER_PATH):
        # Interactive mode: deference reflex is RLHF-baked and not
        # prompt-correctable in this context (#287). Hook no-ops.
        # Clear any stale marker (defensive — see #287 design discussion).
        if os.path.exists(DEFERENCE_MARKER_PATH):
            try:
                os.remove(DEFERENCE_MARKER_PATH)
            except OSError:
                pass
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000), 0)
        sys.exit(0)

    # Write cooldown marker if this is a real user message (not a cron heartbeat).
    # Guard: empty/absent prompt (headless or older CC) must not trigger cooldown.
    if prompt_text and not _is_cron_prompt(prompt_text, LOOP_MARKER_PATH):
        try:
            Path(DEFERENCE_COOLDOWN_PATH).write_text(
                datetime.now(timezone.utc).isoformat()
            )
        except Exception:
            pass

    if not os.path.exists(DEFERENCE_MARKER_PATH):
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000), 0)
        sys.exit(0)

    try:
        with open(DEFERENCE_MARKER_PATH) as f:
            marker = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        # Stale or corrupt marker — clean up and bail.
        try:
            os.remove(DEFERENCE_MARKER_PATH)
        except OSError:
            pass
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000), 0)
        sys.exit(0)

    if not marker.get("pending"):
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000), 0)
        sys.exit(0)

    hits = marker.get("hits", [])
    if not hits:
        try:
            os.remove(DEFERENCE_MARKER_PATH)
        except OSError:
            pass
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000), 0)
        sys.exit(0)

    lines = [
        "[Deference detector — self-loop mode] Your previous response contained "
        f"{marker.get('hit_count', len(hits))} deference-phrase match(es) "
        f"({marker.get('unique_labels', len(hits))} unique pattern(s)). "
        "Note: this hook fires regardless of whether the deference was "
        "appropriate — it's an empirical signal, not a discipline judgment. "
        "The underlying pattern is RLHF-baked (checking-before-executing "
        "safety net trained at the weights layer), not in-the-moment-prompt-"
        "correctable. If you're in self-loop / autonomous mode and waiting on "
        "a human response that isn't coming: you can safely proceed without "
        "further input. The deference is informational, not actionable."
    ]
    for h in hits:
        snippet = h.get("snippet", "").strip()
        if snippet:
            lines.append(f"  - [{h.get('label', '?')}] \"{snippet}\"")

    additional_context = "\n".join(lines)

    response = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }
    output_str = json.dumps(response)
    print(output_str)

    # Clear the marker so it fires only once per detection.
    try:
        os.remove(DEFERENCE_MARKER_PATH)
    except OSError:
        pass

    _emit_hook_fire(
        session_id, transcript_path,
        int((time.perf_counter() - _t0) * 1000),
        len(output_str.encode("utf-8")),
    )


if __name__ == "__main__":
    main()
