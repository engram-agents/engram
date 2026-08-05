#!/usr/bin/env python3
"""Stop hook: nudge to re-arm the loop-wake cron when a loop marker is live
and this turn shows no verified re-arm.

Origin (2026-07-31-loop-arm-nudge-SPEC.md): Ariadne went 44 minutes dark on
2026-07-31 by ending a turn without re-arming ScheduleWakeup. A heartbeat's
absence produces no event, so nothing inside the process can notice on its
own; Lei noticed. Everything shipped since had been *better discipline*, not
*mechanism*. This hook is the mechanism: replace "remember to re-arm" with
"the Stop hook checks for evidence of re-arming and nudges if it's missing."

Design facts this hook rests on (verified live, per the spec -- do not
re-derive):

1. ScheduleWakeup replacement is UNCONDITIONAL -- arming again always
   replaces the pending wake, never stacks one on top of another, so no
   "is one already pending" detector is needed or attempted. Replacement
   also RESETS the timer: a redundant arm this hook provokes slides the
   next wake later by one full cadence. That is not a cost to shrug off --
   it is the correct idle-timer semantics the heartbeat should have had:
   the heartbeat exists for the IDLE case, so "fire N seconds after the
   agent actually goes quiet" is the right behavior, and a fixed wall-clock
   interval was only ever an approximation of it. A redundant arm makes the
   mechanism behave MORE like that, not less.
2. Pending wakes have NO on-disk representation -- a hook cannot query
   "is a wake currently armed" directly. The only observable evidence a
   hook CAN check is a `CronList` tool_result actually present in the
   transcript.
3. `CronList` results land in the transcript as `tool_result` blocks on
   `type: "user"` records, correlated back to their originating `tool_use`
   block (on an earlier `type: "assistant"` record in the SAME delta) via
   `tool_use_id`, rendering the cron listing text (including the
   `<loop-wake>` marker if the armed prompt carried it) intact.
4. Injection is sufficient; blocking is not needed/confirmed. This hook
   uses ONLY the `hookSpecificOutput.additionalContext` channel
   (_stophooklib.emit_context, or the inline fallback below if the lib
   itself is unavailable) -- never a `{"decision": "block", ...}` envelope.
   Per Lei: the write-nudge's injected context "effectively wakes the
   agent" already; a blocking `decision` output's availability here is
   unconfirmed and this design does not need it. (engram-stop-hook.py DOES
   use a blocking decision for its own, unrelated write-nudge -- that fact
   is what makes the `stop_hook_active` re-fire guard below NECESSARY, not
   optional: that hook's block can force this one to re-fire mid-turn.)

Fire condition -- emit the nudge iff ALL of:
  (a) `~/.engram/loop-mode.json` exists (never nag a non-loop session), AND
  (b) its `pacer` field identifies a `scheduleWakeup`-paced loop (see
      2026-07-31 round-6 note below), AND
  (c) this turn's transcript delta contains NO `tool_result` block that
      BOTH (i) correlates via `tool_use_id` to a `tool_use` block, in the
      SAME delta, whose tool name identifies the wake-listing tool, AND
      (ii) carries the `<loop-wake>` marker.

2026-07-31 round-6 review finding (colleague review, asking what happens
on a non-Claude install): the marker's `pacer` field distinguishes a
ONE-SHOT `scheduleWakeup`-paced loop (must re-arm every turn -- what this
hook is built for and correctly polices) from a STANDING cron-paced loop
(must NOT re-arm; re-arming a standing cron is the double-fire bug this
codebase has already hit historically) or other pacers (e.g. `"monitor"`,
per `_status_derive.py`). Before this fix the hook read only marker
EXISTENCE, never `pacer` -- so on a cron-paced loop, which has no reason to
run `CronList` on any given turn, the gate could never be satisfied and the
nudge fired every single turn, telling the agent to `ScheduleWakeup` --
actively the wrong action, and a pacer-mixing bug in its own right. The
same gate also makes the codex-target question harmless by construction:
the plugin bundle ships `hooks.json` AND `skills/` to codex, so a codex
install gets this hook too, while `CronList` is a Claude-harness tool
name -- gating on `pacer` means a non-`scheduleWakeup`-paced (or
unreadable/foreign) install silently does nothing, with no need to assert
anything about what tools a codex install does or doesn't have.

`_pacer_is_schedule_wakeup` compares case-insensitively and
whitespace-tolerantly (`scheduleWakeup` is camelCase in the skills; a
stray `"ScheduleWakeup"` must not silently disable the hook). Missing
`pacer`, an unreadable/corrupt marker, a non-string value, or any
recognised-but-different pacer ALL resolve to "not scheduleWakeup" ->
silent no-op, no nudge. This is the ONE place in this hook where the safe
direction is SILENCE rather than emission (a deliberate, narrow exception
to the "Best-effort" policy at the end of this docstring, not an
inconsistency): the nudge's own INSTRUCTION TEXT is scheduleWakeup-specific
("Re-arm now — ScheduleWakeup..."), so firing it at a loop this hook
can't positively identify as scheduleWakeup-paced risks telling the agent
to take an actively wrong action. Emitting a WRONG instruction is worse
than emitting none -- the opposite trade-off from every other gate in this
hook, where under-detecting evidence just costs a harmless redundant
re-arm (fact 1 above).

2026-07-31 round-3 review finding (a real hole, reproduced independently
before this fix): the PRIOR version of this gate matched on TEXT CONTENT
alone (a pending-job-label alternation plus the marker), with no check on
which tool produced the `tool_result`. That is exactly the "a phrase the
agent could type without running anything" failure this hook exists to
rule out -- just wearing a `tool_result` wrapper nobody inspected. It
false-suppressed on: a `Bash` `tool_result` whose echoed text happened to
contain both a pending-job word and the marker, AND on a `Read` of THIS
HOOK'S OWN SOURCE FILE (whose docstring, unavoidably, contains `<loop-wake>`
and words like "pending" throughout) -- meaning an agent doing ordinary
maintenance reading on this file would have silently disabled the safety
net for that turn, no adversarial intent required.

The fix: `_delta_matches_cron_confirmation` now builds a
`tool_use_id -> tool name` map from every `tool_use` block in the SAME
delta, and a `tool_result` only counts as confirmation if its
`tool_use_id` resolves in that map to a name in `_WAKE_LISTING_TOOL_NAMES`.
Invariants, in priority order:
  1. Tool identity is REQUIRED -- a `tool_result` whose originating
     `tool_use` can't be found, or whose name isn't a known wake-listing
     tool, does not count as confirmation.
  2. Every correlation failure fails toward EMITTING -- an unresolvable
     `tool_use_id`, a `tool_use` in an EARLIER delta (already consumed by
     a prior fire, so invisible to this delta's map), or an unrecognized
     tool name, all leave that block uncounted; if NO block in the delta
     confirms, the nudge fires. Over-firing costs a redundant (harmless,
     see fact 1) re-arm; under-firing is the 44-minute silence this hook
     exists to prevent.
  3. The `<loop-wake>` marker requirement is UNCHANGED and non-negotiable
     -- a correctly-correlated CronList result showing only an unrelated
     job must still not satisfy the gate.
  4. The prior text-based `_PENDING_JOB_HINT` alternation is REMOVED
     (2026-07-31 round-3 supersedes the round-2 instruction to widen it --
     that was treating the symptom). Its whole job was distinguishing "a
     cron listing" from "arbitrary text"; tool identity does that
     properly, and dropping the alternation removes drift surface instead
     of adding to it.
  5. A rename of the wake-listing tool now causes OVER-firing (the tool
     name no longer resolves, so no `tool_result` can confirm), never
     silent suppression -- the correct direction, and the honest way to
     survive a rename: `_WAKE_LISTING_TOOL_NAMES` is a small literal set
     to update, not something to be clever about.

"This turn" = the byte range of transcript_path written since THIS hook's
own last fire (its own state file, `.loop-arm-nudge-last-fire.json` --
distinct from engram-stop-hook.py's `.write-nudge-last-fire.json` and from
engram-deference-detector-stop.py's marker file; no state is shared across
the three Stop hooks). This is evidence, not testimony: a phrase like "loop
cron confirmed" typed in prose does not satisfy the gate -- only a real,
correctly-correlated `CronList` tool_result does.

On a fresh transcript (no prior state for this hook, or the stored state's
transcript_path doesn't match, or the stored offset exceeds the current
file size -- session rotation/compaction), the delta is read from byte 0
(the whole transcript captured so far) rather than treated as "unknown ->
always emit" -- there is no prior fire boundary to violate by doing so, and
reading from 0 lets a same-turn CronList already in a brand-new transcript
still satisfy the gate.

HTML-unescape before matching, exactly as tools/loop_prompt.py's
is_loop_wake() does (#1649): an agent that hand-constructs
`&lt;loop-wake&gt;` instead of real angle brackets when composing a
ScheduleWakeup prompt must still satisfy this gate identically to the
literal marker form.

Known residual (stated here per the spec, not papered over): whether the
transcript delta reliably contains the `tool_result` when CronList is the
agent's LAST action before stopping, or whether that record flushes to the
JSONL only after this Stop hook has already read it (transcript-flush lag
is a known prior in this codebase's Stop hooks). The over-fire this can
cause is NOT bounded to
one self-correcting turn: `_stophooklib.write_offset_state` re-stats the
transcript independently of the stat this hook's own scan used, and if the
file grows in between -- exactly the flush-lag window above -- the
recorded offset advances past bytes this scan never read. Those bytes are
then permanently invisible to every future delta, so the nudge can recur
on turns where the agent did re-arm and verify correctly (see #1855). This
cannot cause a false SUPPRESSION, though: the offset only ever advances,
so the failure strictly under-counts evidence -- it can miss a real
re-arm, never fabricate one. The failure direction stays safe; only the
"one turn" bound was wrong.

Best-effort: any failure fails toward EMITTING the nudge (evidence, not
testimony -- the absence of provable evidence is itself the trigger to
nudge), except the top-level stdin-read/loop-marker-absent checks, which
fail toward silence (never nag a non-loop or unreadable session). This
includes the case where `_stophooklib` itself fails to import: unlike
engram-stop-hook.py (which has an emit path -- `{"decision":"block",...}`
-- that doesn't depend on the lib at all), this hook's only emit channel
IS imported from the lib. `_emit_context_fallback` below is a minimal
inline copy of the same envelope, used ONLY when the lib is unavailable,
so that failure mode -- the one thing this hook must never do, since its
entire purpose is making silence loud -- still nudges instead of going
silent. Behavioral hooks must never crash the session.
"""

import html
import json
import os
import sys
import time
from pathlib import Path

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)
LOOP_MARKER_PATH = os.path.join(ENGRAM_HOME, "loop-mode.json")

# This hook's OWN byte-offset state file -- never shared with
# engram-stop-hook.py's `.write-nudge-last-fire.json` or
# engram-deference-detector-stop.py's `deference-detected.json`.
_LOOP_ARM_STATE_FILE = os.path.join(ENGRAM_HOME, ".loop-arm-nudge-last-fire.json")

_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
try:
    from _stophooklib import (
        read_offset_state as _read_offset_state,
        write_offset_state as _write_offset_state,
        read_delta as _read_delta,
        parse_jsonl_records as _parse_jsonl_records,
        walk_content_blocks as _walk_content_blocks,
        emit_context as _emit_context,
    )
    _STOPHOOKLIB_OK = True
except Exception:
    _STOPHOOKLIB_OK = False

# ---------------------------------------------------------------------------
# loop_prompt import — SSoT for the loop-wake marker (same pattern as
# engram-time-bar-hook.py / engram-deference-detector-prompt.py). Resolve
# tools/ in BOTH deploy topologies:
#   Source:  src/engram/hooks/claude/<name>.py -> src/engram/tools/
#   Plugin:  <root>/hooks/<name>.py             -> <root>/tools/
# FAIL-SAFE: if the import fails for any reason, fall back to the
# locally-defined constant so the marker check below still works. A
# regression test asserts the fallback equals loop_prompt.LOOP_WAKE_MARKER
# (mirrors #1592's guard for engram-time-bar-hook.py).
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

# The wake-listing tool's name(s), as they appear in a tool_use block's
# "name" field. A small literal/alias set, deliberately -- see invariant 5
# in the module docstring: a rename should cause over-firing (this set no
# longer matches, so nothing can confirm), not silent suppression via
# cleverness. Update this set, not the matching logic, if the tool is ever
# renamed or gains an alias.
_WAKE_LISTING_TOOL_NAMES = frozenset({"CronList"})

# The loop-mode.json "pacer" value that identifies a one-shot,
# ScheduleWakeup-paced loop -- the ONLY pacer this hook's nudge applies to
# (round-6 fix). Compared case-insensitively and whitespace-tolerantly by
# _pacer_is_schedule_wakeup below; this constant holds the canonical
# lowercase form.
_SCHEDULE_WAKEUP_PACER = "schedulewakeup"

_NUDGE_TEXT = (
    "[Loop Arm Check: a loop marker is active and this turn shows no verified "
    "CronList. Re-arm now — ScheduleWakeup with this file's loop_prompt copied "
    "verbatim — then run CronList and confirm one job carrying "
    f"{_LOOP_WAKE_MARKER}. Arming is idempotent: a redundant arm replaces, "
    "never stacks.]"
)


def _emit_context_fallback(text: str) -> None:
    """Minimal inline copy of _stophooklib.emit_context's envelope, used
    ONLY when `_stophooklib` failed to import (`_STOPHOOKLIB_OK` is False).

    This hook has no OTHER emit path (unlike engram-stop-hook.py, whose
    `{"decision":"block",...}` emission is independent of the lib) -- so if
    the reason `_delta_has_cron_confirmation` can't confirm anything is
    that the lib itself is broken, the nudge must still reach stdout
    through some channel. Duplicating this ~6-line envelope locally is
    cheaper and more honest than making the hook's own failure-recovery
    path depend on the exact thing that just failed.
    """
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": text,
        }
    }))


def _pacer_is_schedule_wakeup(loop_marker_path: str) -> bool:
    """Return True iff the loop marker's `pacer` field identifies a
    `scheduleWakeup`-paced loop -- the only pacer this hook's nudge
    applies to (round-6 fix, module docstring has the full rationale).

    Compared case-insensitively and whitespace-tolerantly against
    `_SCHEDULE_WAKEUP_PACER` so a stray `"ScheduleWakeup"` (or trailing
    whitespace) doesn't silently disable the hook.

    Returns False for anything else: a missing `pacer` key, an
    unreadable/corrupt marker file, a non-string `pacer` value, or any
    recognised-but-different pacer (e.g. `"monitor"`, or a cron-driven
    pacer). This is deliberate -- see the module docstring's round-6 note
    for why "can't positively identify as scheduleWakeup" fails toward
    SILENCE here, the one exception to this file's otherwise
    fail-toward-emitting policy.
    """
    try:
        with open(loop_marker_path, encoding="utf-8") as f:
            marker = json.load(f)
    except Exception:
        return False
    pacer = marker.get("pacer")
    if not isinstance(pacer, str):
        return False
    return pacer.strip().lower() == _SCHEDULE_WAKEUP_PACER


def _tool_result_text(block: dict) -> str:
    """Extract flat text from a tool_result block's own `content` field.

    A tool_result block's `content` is itself either a bare string or a
    list of typed content blocks (e.g. `[{"type": "text", "text": "..."}]`)
    -- the same bare-string-vs-list shape `_stophooklib.walk_content_blocks`
    already normalizes, so it's reused here for the INNER field too.
    """
    content = block.get("content")
    parts = []
    for inner in _walk_content_blocks(content):
        if inner.get("type") == "text":
            t = inner.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts)


def _build_tool_use_name_map(records: list) -> dict:
    """Map tool_use_id -> tool name, scanning every `tool_use` block on
    every `type: "assistant"` record in `records` (i.e. THIS delta only).

    A tool_use_id that only appears in an EARLIER delta (already consumed
    by a prior fire of this hook) is invisible here by construction --
    that's invariant 2 in the module docstring: an uncorrelatable
    tool_result must fail toward emitting, not toward silently trusting a
    stale correlation from a previous turn.
    """
    id_to_name: dict = {}
    for rec in records:
        if rec.get("type") != "assistant":
            continue
        content = rec.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in _walk_content_blocks(content):
            if block.get("type") != "tool_use":
                continue
            tool_id = block.get("id")
            name = block.get("name")
            if isinstance(tool_id, str) and tool_id and isinstance(name, str):
                id_to_name[tool_id] = name
    return id_to_name


def _delta_matches_cron_confirmation(delta_bytes: bytes) -> bool:
    """Scan the delta's tool_result blocks for a verified, tool-identity-
    correlated CronList result carrying the loop-wake marker.

    A `tool_result` block counts as confirmation iff ALL of:
      1. It has a `tool_use_id` that resolves (within THIS delta's own
         tool_use_id -> name map) to a name in `_WAKE_LISTING_TOOL_NAMES`.
      2. Its (HTML-unescaped, per #1649) text contains the literal
         `<loop-wake>` marker.
    Any correlation failure (missing/unresolvable tool_use_id, unknown
    tool name) makes that block NOT count -- it does not abort the scan,
    it just can't be the thing that suppresses. If no block in the delta
    satisfies both conditions, the nudge fires.
    """
    if not delta_bytes:
        return False
    records, _any_record_parsed = _parse_jsonl_records(delta_bytes)
    tool_use_names = _build_tool_use_name_map(records)
    for rec in records:
        if rec.get("type") != "user":
            continue
        content = rec.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in _walk_content_blocks(content):
            if block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                continue  # unresolvable correlation -> doesn't confirm
            tool_name = tool_use_names.get(tool_use_id)
            if tool_name not in _WAKE_LISTING_TOOL_NAMES:
                continue  # unknown/uncorrelated tool -> doesn't confirm
            text = _tool_result_text(block)
            if not text:
                continue
            unescaped = html.unescape(text)
            if _LOOP_WAKE_MARKER in unescaped:
                return True
    return False


def _delta_has_cron_confirmation(transcript_path: str) -> bool:
    """Return True iff this turn's transcript delta contains a verified,
    tool-identity-correlated CronList tool_result carrying the loop-wake
    marker.

    "This turn" = bytes written to transcript_path since this hook's own
    last fire (own state file). On first fire for a given transcript (no
    stored state, a mismatched path, or rotation/compaction) the delta
    starts from byte 0 -- there is no prior fire boundary to violate by
    doing so.

    Fail-open toward NOT-confirmed (i.e. the nudge fires) on any read
    error, and unconditionally if `_stophooklib` is unavailable (nothing
    below this point works without it): evidence, not testimony -- the
    absence of provable evidence is itself the trigger.
    """
    if not transcript_path or not _STOPHOOKLIB_OK:
        return False

    try:
        current_size = os.path.getsize(transcript_path)
    except OSError:
        return False

    stored_offset = 0
    try:
        stored_path, offset = _read_offset_state(_LOOP_ARM_STATE_FILE)
        if stored_path == transcript_path and 0 <= offset <= current_size:
            stored_offset = offset
    except Exception:
        pass  # missing/corrupt state (or rotation) -> scan from byte 0

    if stored_offset >= current_size:
        delta = b""
    else:
        try:
            delta = _read_delta(transcript_path, stored_offset, current_size)
        except OSError:
            return False

    return _delta_matches_cron_confirmation(delta)


def main() -> None:
    _t0 = time.perf_counter()

    session_id = "unknown"
    transcript_path = ""
    try:
        raw = sys.stdin.read()
        if raw:
            payload = json.loads(raw)
            # Stop hooks can re-fire when another Stop hook in the same
            # array returns a blocking decision. engram-stop-hook.py's
            # write-nudge does exactly this ({"decision":"block",...}),
            # which is what makes this guard NECESSARY (not merely
            # defensive) -- without it, that hook's block would force this
            # one to re-fire and re-nag mid-turn. Matches
            # engram-utility-credit-mention-stop.py's existing precedent.
            if payload.get("stop_hook_active", False):
                return
            sid = payload.get("session_id")
            if isinstance(sid, str) and sid:
                session_id = sid
            tp = payload.get("transcript_path")
            if isinstance(tp, str) and tp:
                transcript_path = tp
    except Exception:
        return

    # Never nag a non-loop session. No state-file touch on this path — the
    # gate is stateless, matching engram-stop-hook.py's maturity-gate
    # discipline (crossing back into loop mode later leaves this hook's
    # own bookkeeping undisturbed).
    if not os.path.exists(LOOP_MARKER_PATH):
        return

    # Round-6 fix: never nag a loop this hook can't positively identify as
    # scheduleWakeup-paced -- see _pacer_is_schedule_wakeup's docstring and
    # the module docstring's round-6 note for why silence, not emission,
    # is correct here. Also stateless, same reasoning as the marker-absent
    # check above.
    if not _pacer_is_schedule_wakeup(LOOP_MARKER_PATH):
        return

    confirmed = False
    try:
        confirmed = _delta_has_cron_confirmation(transcript_path)
    except Exception:  # noqa: BLE001
        confirmed = False  # fail-open: nudge fires

    if confirmed:
        if _STOPHOOKLIB_OK:
            _write_offset_state(_LOOP_ARM_STATE_FILE, transcript_path)
        return

    if _STOPHOOKLIB_OK:
        _emit_context(_NUDGE_TEXT)
        _write_offset_state(_LOOP_ARM_STATE_FILE, transcript_path)
    else:
        # Lib unavailable -- this is exactly the failure this hook must
        # never go silent on (its entire purpose is making silence loud).
        # Use the inline fallback envelope instead of the lib's
        # emit_context; there is no offset state to write without the lib,
        # so skip that bookkeeping (nothing else to do with it).
        _emit_context_fallback(_NUDGE_TEXT)

    _duration_ms = int((time.perf_counter() - _t0) * 1000)

    # Emit engram.hook.fire event, matching the sibling Stop hooks'
    # observability convention. Failure must not break the hook.
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
                "hook_name": "engram-loop-arm-stop-hook",
                "hook_type": "Stop",
                "duration_ms": _duration_ms,
                "exit_code": 0,
            },
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
