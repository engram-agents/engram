#!/usr/bin/env python3
"""Stop hook: nudge to re-arm the loop-wake cron when a loop marker is live
and this turn shows no verified re-arm.

Origin (2026-07-31-loop-arm-nudge-SPEC.md): Ariadne went 44 minutes dark on
2026-07-31 by ending a turn without re-arming ScheduleWakeup. A heartbeat's
absence produces no event, so nothing inside the process can notice on its
own; Lei noticed. Everything shipped since had been *better discipline*, not
*mechanism*. This hook is the mechanism: replace "remember to re-arm" with
"the Stop hook checks for evidence of re-arming and nudges if it's missing."

2026-08-12 retarget (#1973, #1987): this hook originally gated on a
`CronList` **tool_result** showing a pending `<loop-wake>` job. That proves
*a wake is pending*, not *a wake was armed from now* -- Lei ruled the
heartbeat is a SLIDING WINDOW (re-arm from now at the end of every turn,
because any real wake already refreshes the cache and the previously-armed
wake is redundant). A job armed an hour ago and one armed this second are
byte-identical in `CronList` output, so the old gate could not discriminate
compliance from violation even in principle -- and worse, it was satisfiable
by the exact behaviour it was meant to police: Ariadne declined to re-arm
four times in one session, each time satisfying the hook with a bare
`CronList`, and the pending job stayed pinned at its original (stale) fire
time for ~90 minutes (#1973). The fix: gate on a `ScheduleWakeup`
**tool_call** in this turn's transcript delta instead -- a tool_call's mere
presence is unambiguous evidence of arming-from-now; a `CronList`
tool_result never is. `CronList`-after-arm stays valuable as
*verification* (it is what catches a marker whose armed prompt failed to
lead with `<loop-wake>`), and the nudge text below says so explicitly:
verification-after-arm, never a substitute for arming.

Design facts this hook rests on (verified live, per the spec -- do not
re-derive):

1. ScheduleWakeup replacement is UNCONDITIONAL -- arming again always
   replaces the pending wake, never stacks one on top of another, so no
   "is one already pending" detector is needed or attempted (this is also
   why the `engram-loop` skill's own end-of-iteration arm no longer checks
   `CronList` before arming -- see that skill's 2026-08-12 revision).
   Replacement also RESETS the timer: a redundant arm this hook provokes
   slides the next wake later by one full cadence. That is not a cost to
   shrug off -- it is the correct idle-timer semantics the heartbeat should
   have had: the heartbeat exists for the IDLE case, so "fire N seconds
   after the agent actually goes quiet" is the right behavior, and a fixed
   wall-clock interval was only ever an approximation of it. A redundant arm
   makes the mechanism behave MORE like that, not less.
2. Pending wakes have NO on-disk representation -- a hook cannot query
   "is a wake currently armed" directly. What a hook CAN observe is the
   transcript: either a `CronList` result (evidence a wake IS pending, but
   not evidence of WHEN it was armed) or a `ScheduleWakeup` tool_use block
   (evidence a wake WAS ARMED THIS TURN -- the fact this hook now checks
   for).
3. `tool_use` blocks land in the transcript on `type: "assistant"` records,
   each carrying its own `name` and `input` fields directly -- unlike a
   `tool_result`, a `tool_use` block needs no correlation back to another
   record to know what tool produced it and what arguments it was called
   with. `ScheduleWakeup`'s `input.prompt` carries the exact text passed to
   the `prompt=` parameter, including the `<loop-wake>` marker if the armed
   prompt carried it (per `tools/loop_prompt.py`'s `format_loop_prompt`).
4. Blocking IS needed -- this hook emits `{"decision": "block", "reason":
   ...}`, matching engram-stop-hook.py's own write-nudge exactly, never
   `hookSpecificOutput.additionalContext`. Evidence, grade first, then the
   conclusion it supports (2026-08-12 correction, same day as the retarget
   above -- restructured per PR #2004's colleague review so a future
   caveat can't fall off a trailing clause the way this one did the first
   time):
     - PER THE DOCS (documentation-derived, not measured): Claude Code's
       Stop-hook docs (`hooks.md`, "Stop decision control") do not list
       `additionalContext` as a supported `hookSpecificOutput` field for
       the Stop event at all -- only `permissionDecision`/
       `permissionDecisionReason` are documented as interrupting a
       stopping turn.
     - MEASURED, live, 2026-08-12: this codebase's legacy `decision`/
       `reason` pair (not the docs' newer field names) demonstrably
       interrupts a turn -- engram-stop-hook.py's write-nudge has used it
       for its own effect all along. Separately, on two independent seats,
       a nudge carrying only `additionalContext` (this hook's
       pre-correction version) WAS observed landing in the agent's
       context -- but not until a subsequent turn, never the one that was
       ending. **Open, not resolved:** whether `additionalContext` alone
       causes that delivery, or whether it rode on a DIFFERENT Stop hook
       (e.g. the write-nudge) blocking in the SAME multi-hook firing event
       and merely carrying this hook's text along -- "borrowed liveness,"
       which would mean this hook had never worked on its own merit. A
       discriminating test was proposed (a Stop event where this hook's
       old gate fires but no other hook blocks) and attempted twice; both
       attempts were retracted by their own author as invalid
       instrumentation, not as a negative result. Deliberately unresolved
       here rather than adjudicated by argument.
     - CONCLUSION: this open question does not change the fix. Whichever
       hypothesis is true, `decision:block` makes this hook self-
       sufficient -- it no longer needs a same-turn assist from a sibling
       hook's block, and it no longer depends on a next-turn delivery that
       cannot exist when the turn it needed to interrupt was the last one
       (a forgotten re-arm leaves no next turn for a delayed nudge to land
       in). `decision:block` converts "delivered eventually, maybe on its
       own merit" into "acted on now, unconditionally" -- the actual
       defect this fixes either way.
   The original version of this design fact stated its conclusion before
   any evidence grading, and claimed the nudge "had never actually
   delivered... on any host" -- a false universal (an unmeasured claim
   asserted as measured, corrected in this revision). The real defect was
   always delivery TIMING, not delivery ABSENCE. The `stop_hook_active` re-fire guard
   below (already required because engram-stop-hook.py's OWN block could
   force this hook to re-fire mid-turn) is what makes it SAFE for this
   hook to also block: on a re-fire caused by its own block,
   `stop_hook_active` is true and the guard returns early, so blocking
   cannot recurse.

2026-08-18 note (#2098, measured on Claude Code 2.1.235): on Kepler's
seat a SOLO `ScheduleWakeup` call -- the only tool call in its message --
ends the turn at its result (n=2, under explicit intent to continue),
while the SAME call batched with any sibling tool call in one parallel
block lets the turn continue normally (measured live, rendered output
confirmed by the user). NOT reproduced on Borges's seat at the same
version: 69/69 solo arms there continued, with a following tool call
that executed and Stop-hook mediation excluded -- the divergence is
unexplained. The batched shape is the recommendation because it is the
one measured working on both seats, not because the solo failure is
universal. Separately, only
a turn's FINAL text renders to the user. Consequence for reply-carrying
loop turns (engram-loop Step 2 item 3): batch the arm with a sibling
call (the marker-state rewrite is the natural one), then the reply as
the turn's final text. This hook keeps its ordinary safety-net role
unchanged; `_NUDGE_TEXT` carries a pointer to the batched shape so an
agent nudged mid-conversation doesn't swallow its next reply. Measured
on 2.1.235, not asserted universal.

Fire condition -- emit the nudge iff ALL of:
  (a) `~/.engram/loop-mode.json` exists (never nag a non-loop session), AND
  (b) its `pacer` field identifies a `scheduleWakeup`-paced loop (see
      2026-07-31 round-6 note below), AND
  (c) this turn's transcript delta contains NO `tool_use` block that BOTH
      (i) names the wake-arming tool (`ScheduleWakeup`), AND (ii) carries
      the `<loop-wake>` marker in its `input.prompt`.

2026-07-31 round-6 review finding (colleague review, asking what happens
on a non-Claude install): the marker's `pacer` field distinguishes a
ONE-SHOT `scheduleWakeup`-paced loop (must re-arm every turn -- what this
hook is built for and correctly polices) from a STANDING cron-paced loop
(must NOT re-arm; re-arming a standing cron is the double-fire bug this
codebase has already hit historically) or other pacers (e.g. `"monitor"`,
per `_status_derive.py`). Before this fix the hook read only marker
EXISTENCE, never `pacer` -- so on a cron-paced loop, which has no reason to
call `ScheduleWakeup` on any given turn, the gate could never be satisfied
and the nudge fired every single turn, telling the agent to `ScheduleWakeup`
-- actively the wrong action, and a pacer-mixing bug in its own right. The
same gate also makes the codex-target question harmless by construction:
the plugin bundle ships `hooks.json` AND `skills/` to codex, so a codex
install gets this hook too, while `ScheduleWakeup` is a Claude-harness tool
name -- gating on `pacer` means a non-`scheduleWakeup`-paced (or
unreadable/foreign) install silently does nothing, with no need to assert
anything about what tools a codex install does or doesn't have. This
reasoning is unchanged by the 2026-08-12 retarget -- `pacer` gating is
orthogonal to which tool-shape the confirmation check inspects.

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
before this fix, and re-derived below for the 2026-08-12 tool_call
retarget): the PRIOR-PRIOR version of this gate matched on TEXT CONTENT
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

The round-3 fix required tool-IDENTITY, not just tool-result-wrapper
presence: the CronList-result-era `_delta_matches_cron_confirmation` built
a `tool_use_id -> tool name` map from every `tool_use` block in the SAME
delta, and a `tool_result` only counted as confirmation if its
`tool_use_id` resolved in that map to a name in `_WAKE_LISTING_TOOL_NAMES`.
The 2026-08-12 retarget (function now `_delta_matches_arm_confirmation`,
tool set now `_WAKE_ARMING_TOOL_NAMES`) makes that whole correlation step
UNNECESSARY, not merely simpler: a `tool_use` block already carries its own
`name` field directly, so there is no separate `tool_result` to correlate
BACK to a producer -- the call IS the producer. The round-3 hole
(identity-free text matching) therefore cannot recur in the new shape by
construction: a `Bash` call, or a `Read` of this hook's own source file, is
a DIFFERENT tool_use block with a DIFFERENT `name`, so it is never mistaken
for a `ScheduleWakeup` call no matter what text its `input` or a companion
`tool_result` happens to contain. The regression tests that exercised the
old round-3 hole are ported to their tool_use-shaped equivalents (a decoy
`Bash` call whose `input` happens to contain the marker; a `Read` of this
hook's own source file as a decoy tool_use/tool_result pair) rather than
dropped, so the underlying invariant -- tool identity is required, text
alone is not enough -- keeps regression coverage under the new shape.

Invariants, in priority order (re-derived for tool_use detection; several
simplify because a tool_use needs no correlation, noted below):
  1. Tool identity is REQUIRED -- a `tool_use` block whose `name` isn't
     `ScheduleWakeup` (`_WAKE_ARMING_TOOL_NAMES`) does not count as
     confirmation, no matter what its `input.prompt` (or any sibling
     `tool_result`) contains.
  2. Every non-match fails toward EMITTING -- a tool_use with the wrong
     name, a missing/non-dict `input`, a missing/non-string `prompt`, or a
     `prompt` that doesn't (after HTML-unescape) contain the marker, all
     leave that block uncounted; if NO block in the delta confirms, the
     nudge fires. Over-firing costs a redundant (harmless, see fact 1)
     re-arm; under-firing is the 44-minute silence this hook exists to
     prevent.
  3. The `<loop-wake>` marker requirement is UNCHANGED and non-negotiable
     -- a `ScheduleWakeup` call with an unrelated/marker-less prompt must
     still not satisfy the gate.
  4. tool_use_id CORRELATION IS NO LONGER NEEDED (simplification, not a
     dropped guard): the old gate needed it because a `tool_result` carries
     no `name` of its own and had to be traced back to the `tool_use` that
     produced it via `tool_use_id`. A `tool_use` block already IS the
     record of "this tool was called with these arguments" -- its presence
     in the delta, with the right `name` and the right `input.prompt`, is
     the complete fact this hook needs, nothing to correlate. This also
     means the "tool_use in an earlier delta" edge case (the old gate's
     invariant-2 sub-case) no longer applies the same way: a `tool_use`
     block is either IN this delta (this turn called ScheduleWakeup) or it
     isn't (this turn didn't) -- there is no separate later-arriving result
     to worry about splitting across deltas.
  5. A rename of the wake-arming tool now causes OVER-firing (the tool
     name no longer resolves, so no `tool_use` can confirm), never silent
     suppression -- the correct direction, and the honest way to survive a
     rename: `_WAKE_ARMING_TOOL_NAMES` is a small literal set to update, not
     something to be clever about.

"This turn" = the byte range of transcript_path written since THIS hook's
own last fire (its own state file, `.loop-arm-nudge-last-fire.json` --
distinct from engram-stop-hook.py's `.write-nudge-last-fire.json` and from
engram-deference-detector-stop.py's marker file; no state is shared across
the three Stop hooks). This is evidence, not testimony: a phrase like "I
armed the wakeup" typed in prose does not satisfy the gate -- only a real
`ScheduleWakeup` tool_use block does.

On a fresh transcript (no prior state for this hook, or the stored state's
transcript_path doesn't match, or the stored offset exceeds the current
file size -- session rotation/compaction), the delta is read from byte 0
(the whole transcript captured so far) rather than treated as "unknown ->
always emit" -- there is no prior fire boundary to violate by doing so, and
reading from 0 lets a same-turn ScheduleWakeup already in a brand-new
transcript still satisfy the gate.

HTML-unescape before matching, exactly as tools/loop_prompt.py's
is_loop_wake() does (#1649): an agent that hand-constructs
`&lt;loop-wake&gt;` instead of real angle brackets when composing a
ScheduleWakeup prompt must still satisfy this gate identically to the
literal marker form.

Known residual (stated here per the spec, not papered over): whether the
transcript delta reliably contains the `tool_use` block when ScheduleWakeup
is the agent's LAST action before stopping, or whether that record flushes
to the JSONL only after this Stop hook has already read it (transcript-
flush lag is a known prior in this codebase's Stop hooks -- previously
documented against the `tool_result` shape, and the same lag risk applies
identically to a `tool_use` block, since both land in the same JSONL
stream). The over-fire this can cause is NOT bounded to one self-correcting
turn: `_stophooklib.write_offset_state` re-stats the transcript
independently of the stat this hook's own scan used, and if the file grows
in between -- exactly the flush-lag window above -- the recorded offset
advances past bytes this scan never read. Those bytes are then permanently
invisible to every future delta, so the nudge can recur on turns where the
agent did re-arm and verify correctly (see #1855). This cannot cause a
false SUPPRESSION, though: the offset only ever advances, so the failure
strictly under-counts evidence -- it can miss a real re-arm, never fabricate
one. The failure direction stays safe; only the "one turn" bound was wrong.

Best-effort: any failure fails toward EMITTING the nudge (evidence, not
testimony -- the absence of provable evidence is itself the trigger to
nudge), except the top-level stdin-read/loop-marker-absent checks, which
fail toward silence (never nag a non-loop or unreadable session). This
includes the case where `_stophooklib` itself fails to import: as of the
2026-08-12 blocking-decision correction (design fact 4), emission is a
single `print(json.dumps({"decision": "block", "reason": ...}))` call that
does NOT depend on the lib at all -- matching engram-stop-hook.py's own
emit path exactly, and removing the whole primary/fallback emission split
this paragraph used to describe. `_stophooklib` is still needed for the
OTHER functions imported from it (offset-state read/write, delta parsing,
content-block walking), so `_STOPHOOKLIB_OK` still gates whether the
transcript-delta scan runs at all -- but it no longer gates emission
itself: an unimportable lib now degrades this hook to "always nudge, never
scan" (the delta check is skipped, so `confirmed` stays `False`), which is
the correct fail-toward-emitting direction, not a separate fallback code
path to keep in sync with the primary one. Behavioral hooks must never
crash the session.
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

# The wake-arming tool's name(s), as they appear in a tool_use block's
# "name" field. A small literal/alias set, deliberately -- see invariant 5
# in the module docstring: a rename should cause over-firing (this set no
# longer matches, so nothing can confirm), not silent suppression via
# cleverness. Update this set, not the matching logic, if the tool is ever
# renamed or gains an alias.
_WAKE_ARMING_TOOL_NAMES = frozenset({"ScheduleWakeup"})

# The loop-mode.json "pacer" value that identifies a one-shot,
# ScheduleWakeup-paced loop -- the ONLY pacer this hook's nudge applies to
# (round-6 fix). Compared case-insensitively and whitespace-tolerantly by
# _pacer_is_schedule_wakeup below; this constant holds the canonical
# lowercase form.
_SCHEDULE_WAKEUP_PACER = "schedulewakeup"

_NUDGE_TEXT = (
    "[Loop Arm Check: a loop marker is active and this turn shows no "
    "ScheduleWakeup call. Arm now — ScheduleWakeup with this file's "
    "loop_prompt copied verbatim — then run CronList to verify the armed "
    "prompt leads with "
    f"{_LOOP_WAKE_MARKER}. CronList alone never satisfies this check, only "
    "verifies after the real arming action. Arming is idempotent: a "
    "redundant arm replaces, never stacks. If your NEXT turn will carry a "
    "user-facing reply: batch the arm with a sibling tool call in one "
    "parallel block and put the reply after it as the turn's final text — "
    "a solo arm ends the turn, a trailing arm swallows the reply (engram-"
    "loop Step 2 item 3, #2098). If a reply already rendered this turn, "
    "arm now and add nothing after.]"
)


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


def _tool_use_prompt_text(block: dict) -> str:
    """Extract the `prompt` input parameter's text from a tool_use block.

    `ScheduleWakeup`'s call shape is `{"type": "tool_use", "name":
    "ScheduleWakeup", "input": {"prompt": "...", ...}}` -- the marker this
    hook checks for lives in the CALL's own input, not in any result. A
    missing/non-dict `input`, or a missing/non-string `prompt`, yields ""
    (treated as no evidence, the same way an empty tool_result text did
    before the 2026-08-12 retarget).
    """
    input_ = block.get("input")
    if not isinstance(input_, dict):
        return ""
    prompt = input_.get("prompt")
    return prompt if isinstance(prompt, str) else ""


def _delta_matches_arm_confirmation(delta_bytes: bytes) -> bool:
    """Scan the delta's tool_use blocks for a ScheduleWakeup call whose
    `input.prompt` carries the loop-wake marker.

    A `tool_use` block counts as confirmation iff ALL of:
      1. Its `name` is in `_WAKE_ARMING_TOOL_NAMES` (i.e. "ScheduleWakeup").
      2. Its (HTML-unescaped, per #1649) `input.prompt` text contains the
         literal `<loop-wake>` marker.
    Unlike the old CronList-result gate, a tool_use block carries its own
    `name` directly -- no tool_use_id correlation to a separate result is
    needed, because the call itself IS the evidence (its mere presence in
    this turn's delta proves ScheduleWakeup was invoked THIS turn, which is
    exactly the "armed from now" fact #1973 needs and a CronList result can
    never prove: a job armed an hour ago and one armed this second are
    byte-identical in CronList output). If no tool_use block in the delta
    satisfies both conditions, the nudge fires.
    """
    if not delta_bytes:
        return False
    records, _any_record_parsed = _parse_jsonl_records(delta_bytes)
    for rec in records:
        if rec.get("type") != "assistant":
            continue
        content = rec.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in _walk_content_blocks(content):
            if block.get("type") != "tool_use":
                continue
            if block.get("name") not in _WAKE_ARMING_TOOL_NAMES:
                continue  # not a ScheduleWakeup call -> doesn't confirm
            text = _tool_use_prompt_text(block)
            if not text:
                continue
            unescaped = html.unescape(text)
            if _LOOP_WAKE_MARKER in unescaped:
                return True
    return False


def _delta_has_arm_confirmation(transcript_path: str) -> bool:
    """Return True iff this turn's transcript delta contains a
    ScheduleWakeup tool_use call carrying the loop-wake marker in its
    `input.prompt`.

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

    return _delta_matches_arm_confirmation(delta)


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
        confirmed = _delta_has_arm_confirmation(transcript_path)
    except Exception:  # noqa: BLE001
        confirmed = False  # fail-open: nudge fires

    if confirmed:
        if _STOPHOOKLIB_OK:
            _write_offset_state(_LOOP_ARM_STATE_FILE, transcript_path)
        return

    # Single, lib-independent emission path (2026-08-12 blocking-decision
    # correction) -- matches engram-stop-hook.py's own write-nudge exactly.
    # A blocking decision is what actually interrupts a stopping turn (see
    # design fact 4); additionalContext alone is not even a documented
    # Stop-hook field. No fallback/primary split to keep in sync: this
    # print statement is the only emit channel, lib available or not.
    print(json.dumps({"decision": "block", "reason": _NUDGE_TEXT}))
    if _STOPHOOKLIB_OK:
        _write_offset_state(_LOOP_ARM_STATE_FILE, transcript_path)
    # Lib unavailable -- no offset state to write without it, so skip that
    # bookkeeping (nothing else to do with it); the nudge above still fired.

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
