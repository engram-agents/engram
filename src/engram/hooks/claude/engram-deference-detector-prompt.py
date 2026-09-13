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

import html
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

# Module-scope emitter resolve+insert (gh#2570 §1/§3): this hook previously
# had no resolver at all -- its emit site did a bare
# sys.path.insert(0, ENGRAM_HOME), which shadows the plugin's live copy of
# engram_log_emitter.py whenever a stale scatter copy sits in ENGRAM_HOME.
# This file's own directory IS already sys.path[0] when invoked as a plain
# script (both source and flattened deployed layout) -- but this repo's own
# test suite also loads hooks directly via importlib.spec_from_file_location
# (e.g. tests/test_deference_detector_prompt.py, tests/test_loop_prompt.py),
# which does NOT implicitly add the hook's own directory to sys.path the way
# a subprocess invocation does. Insert explicitly so both invocation styles
# resolve _hooklib the same way (matches every sibling hook's existing
# precedent, e.g. engram-deference-detector-stop.py's _this_dir insert).
_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
from _hooklib import resolve_runtime_dir, emit_context
from _harness import detect_harness, get_harness_budgets

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
_MAIN_MANIFEST_ID = "hooks/claude/engram-deference-detector-prompt.py:main"

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
# (c / "loop_prompt.py").exists() raises OSError -- not False -- on an
# unreadable candidate directory, and this whole block runs at bare
# module-import time, so an unguarded raise here must never crash the
# hook process. PER-CANDIDATE guard, not a single try/except around the
# whole search: _lp_candidates puts $CLAUDE_PLUGIN_ROOT FIRST and the
# __file__-relative walk-parents fallbacks after, so if the plugin-root
# candidate is the unreadable one (exactly this issue's reported case),
# a whole-search try/except aborts on that first raise and never tries
# the later, normally-readable fallbacks -- discarding a candidate that
# would have resolved. Skipping just the failing candidate and
# continuing the search is what actually degrades gracefully
# (engram-alpha#2671 review round: reproduced the whole-search variant
# returning None on an unreadable-then-readable candidate order, where
# the per-candidate variant correctly finds the readable one).
_LP_TOOLS_DIR = None
for _lp_candidate in _lp_candidates:
    try:
        _lp_found = (_lp_candidate / "loop_prompt.py").exists()
    except OSError:
        continue
    if _lp_found:
        _LP_TOOLS_DIR = _lp_candidate
        break
if _LP_TOOLS_DIR is not None and str(_LP_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_LP_TOOLS_DIR))
try:
    from loop_prompt import LOOP_WAKE_MARKER as _LOOP_WAKE_MARKER
except Exception:
    _LOOP_WAKE_MARKER = _LOOP_WAKE_MARKER_FALLBACK


def _is_cron_prompt(prompt: str, loop_marker_path: str) -> bool:
    """Return True if the prompt looks like a cron-fired heartbeat, not a real user message."""
    # HTML-unescape before stripping so an escaped/mangled marker (e.g. an
    # agent hand-constructing &lt;loop-wake&gt; or &lt;&lt;autonomous-loop&gt;&gt;
    # instead of real angle brackets) still classifies identically to the
    # plain form. One normalize here covers every check below (gh#1649).
    stripped = html.unescape(prompt).strip()
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


_HOOK_FIRE_SITE = "deference-detector-prompt.hook_fire"


def _emit_hook_fire(session_id: str, transcript_path: str, duration_ms: int, stdout_bytes: int) -> None:
    """Emit engram.hook.fire event. Failure must not break the hook.

    alpha #2574 WS3: construction (import + Emitter.init) is narrowed to its
    own try/except so an INIT_FAILED outcome is recordable and distinct from
    an ordinary DROPPED emit (docs/2574-telemetry-design.md §1/§5) -- the
    previous single wide try made a failure after a successful emit
    indistinguishable from a construction failure.
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
            "hook_name": "engram-deference-detector-prompt",
            "hook_type": "UserPromptSubmit",
            "duration_ms": duration_ms,
            "exit_code": 0,
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": 0,
        },
        site="deference-detector-prompt.hook_fire",
    )
    write_telemetry_record(
        manifest_id=_HOOK_FIRE_SITE,
        event_type="engram.hook.fire",
        fields={"outcome": "delivered" if _uid is not None else "dropped"},
    )


def main() -> None:
    # alpha #2574 WS3: `reached` -- unconditional, before any guard/early
    # return in this function (docs/2574-telemetry-design.md §1 NEGATIVE
    # EVIDENCE / TRIGGER ENTRY).
    write_telemetry_record(manifest_id=_MAIN_MANIFEST_ID, event_type="engram.telemetry.reached", fields={})

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

    # engram-alpha#2863 WS2: `head` carries the single must-not-miss fact
    # (a deference reflex fired, and how many matches) -- `body_blocks`
    # carries the per-hit snippets, ordered as detected. Snippet content
    # comes from the agent's own prior response and is of genuinely
    # unpredictable size/count (unlike engram-toolcall-repair.py's bounded
    # sibling), so this is a real, not merely defensive, migration target.
    head = (
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
    )
    body_blocks = []
    for h in hits:
        snippet = h.get("snippet", "").strip()
        if snippet:
            body_blocks.append(f"  - [{h.get('label', '?')}] \"{snippet}\"")

    # No local economy target exists for this hook -- `target` uses the
    # same hard `budget` ceiling as `budget` itself (mirrors
    # engram-surface-hook.py's own default choice for a hook with no
    # pre-existing local cap).
    budgets = get_harness_budgets(detect_harness())
    additional_context = emit_context(
        head,
        body_blocks,
        budget=budgets["budget"],
        target=budgets["budget"],
    )

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
