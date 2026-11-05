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

alpha #4193 -- the reply gate (#4104) also consults the Stop payload's
`last_assistant_message`. The hook can fire before the final assistant record is
flushed, so a turn that really ended on rendered text can show a delta ending on an
earlier tool_use, and the transcript-only gate false-blocks (forcing a duplicate reply).
FIELD SEMANTICS ARE UNVERIFIED: no payload was measured for a swallowed reply (text then
a trailing tool_use in one message). It may be the concatenation of all the final
message's text blocks (R-flat) or only its final text block (R-final); either way it can
be non-empty on a swallowed turn, so "non-empty means rendered" is NOT the rule. The
contract holds under both readings. Let P = the payload field when it is a str with
non-whitespace content, else None; compare whitespace-normalised:
  1. P present and NOT found in the delta's assistant text -> the text is not flushed
     yet -> treat the reply as rendered (no reply-gate block).
  2. P present and found there (normalised P is a substring of the joined text blocks of
     the final message, OR that message's last text block is a suffix of P, OR P is a
     substring of the joined assistant text of the whole turn since the human prompt) while the transcript still says the turn does not end on text -> STILL
     block (swallowed reply).
  3. P absent / non-str / empty / whitespace -> exactly the transcript-only path.
The override is consulted only when the transcript path already says `has_human and not
reply_rendered`, so it can only turn a block into no-block. "Found" looks only at the FINAL
MESSAGE: the assistant records after the last human prompt whose message.id equals the
last assistant record's id. Identity, not adjacency: with parallel tool calls one API
message is written one block per record with tool_results between them (measured in a
real transcript), so a swallowed reply's text can sit before several tool_results. Text
of an earlier message (a different id) does not count. A last record with no usable id
falls back to its adjacent run of assistant records. The reverse clause is a suffix test, not a substring test, so a short earlier
preamble ("Ok") cannot make a longer payload ("Ok, here is the answer") look found.
Text equal to the payload (e.g. "Done." then a tool call, payload "Done.") is
indistinguishable from a swallowed reply and keeps the block; so is a P equal to text
already flushed earlier in the turn. That covers a SECOND unverified reading of the field:
the last text produced in the turn (not the final message's text), which on a swallowed
reply followed by a tool-only message would otherwise look like flush lag. A real flush-lag
reply is unflushed, so it appears nowhere in the delta and still gets the override. Each such gate decision appends one line to
`$ENGRAM_HOME/loop-arm-reply-gate.log` (no payload text, bounded, fail-silent) so the
real field semantics can be read off live traffic.

Fire condition -- for Claude, emit the normal nudge iff ALL of:
  (a) `~/.engram/loop-mode.json` (under $ENGRAM_HOME) exists (never nag a non-loop session), AND
  (b) its `pacer` field identifies a `scheduleWakeup`-paced loop (see
      2026-07-31 round-6 note below), AND
  (c) this turn's transcript delta contains NO `tool_use` block that BOTH
      (i) names the wake-arming tool (`ScheduleWakeup`), AND (ii) carries
      the `<loop-wake>` marker in its `input.prompt`.

For managed Codex, a `scheduleWakeup` marker emits only the Codex repair
nudge; the normal Claude confirmation scan never runs.

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
Codex needs one extra guard beyond pacer gating: a stale or misfiled
`pacer: "scheduleWakeup"` marker is exactly the cross-harness state that
would otherwise make this hook demand a Claude-only action. Runtime platform
evidence therefore wins before the normal confirmation scan. On managed Codex,
that erroneous marker gets a Codex-specific bridge/timer repair nudge; all
other Codex markers stay silent. This preserves the original pacer gate for
Claude while refusing to reinforce a wrong Codex pacer.

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
import re
import shlex
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A RELATIVE ENGRAM_HOME is refused (alpha #3863 6b, step 3a): it would resolve against this process's cwd, a
# different home than the daemon's or the server's. Fail open: one stderr line, nothing written, exit 0.
# (Inline, not from _hooklib: this hook must still run when the sibling lib cannot import. engram_home() is a
# VERBATIM copy of tools/_owner_read.engram_home(), pinned by tests/test_engram_home_parity_3863.py.)
def engram_home() -> Path:
    """The ENGRAM home: $ENGRAM_HOME (default ~/.engram) when set and non-empty, expanded and absolute;
    a RELATIVE value is refused (ValueError naming it). Verbatim pinned copy (see the comment above)."""
    raw = os.environ.get("ENGRAM_HOME") or "~/.engram"
    home = os.path.expanduser(raw)
    if not os.path.isabs(home):
        raise ValueError("ENGRAM_HOME must be an absolute path, got %r" % (raw,))
    return Path(os.path.abspath(home))


try:
    _ENGRAM_HOME_PATH = engram_home()
except ValueError as _exc:
    sys.stderr.write("engram hook: %s; doing nothing\n" % (_exc,))
    sys.exit(0)
if os.environ.get("ENGRAM_HOME"):
    os.environ["ENGRAM_HOME"] = str(_ENGRAM_HOME_PATH)  # `~/x` -> absolute for every raw reader

ENGRAM_HOME = str(_ENGRAM_HOME_PATH)
LOOP_MARKER_PATH = os.path.join(ENGRAM_HOME, "loop-mode.json")

# This hook's OWN byte-offset state file -- never shared with
# engram-stop-hook.py's `.write-nudge-last-fire.json` or
# engram-deference-detector-stop.py's `deference-detected.json`.
_LOOP_ARM_STATE_FILE = os.path.join(ENGRAM_HOME, ".loop-arm-nudge-last-fire.json")

# alpha #3919 M2: the quiet-wake counter's OWN state file. Deliberately NOT the
# loop marker: `loop_prompt.py --rearm-delay` rewrites the marker, and a second
# writer there would race it.
# Known limits (PR 3947 review): (1) two sessions sharing one ENGRAM_HOME overwrite each other's
# counter (last writer wins, keyed by transcript path, like the offset file beside it): the
# failure is a missed nudge, never a spurious one. (2) If this hook is skipped for a turn
# (marker absent, stop_hook_active) the next delta spans several turns and counts as ONE tick.
_QUIET_WAKE_STATE_FILE = os.path.join(ENGRAM_HOME, ".quiet-wake-counter.json")

_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

# Module-scope emitter resolve+insert (gh#2570 §1/§3): this hook previously
# had no resolver at all -- its emit site did a bare
# sys.path.insert(0, ENGRAM_HOME), which shadows the plugin's live copy of
# engram_log_emitter.py whenever a stale scatter copy sits in ENGRAM_HOME.
# Guarded (unlike the sibling hooks' unguarded equivalent): this hook's own
# design contract, defended by test_emits_via_fallback_when_stophooklib_import_fails
# and the two _copy_hook_into_baked_bundle regression tests, is that it must
# NEVER crash even when a sibling lib module is absent from an isolated
# bundle -- its whole purpose is making a missed ScheduleWakeup arm loud,
# so an unhandled ImportError here (which would abort the primary nudge
# below, not just telemetry) is strictly worse than the emitter silently
# not firing. Same fail-toward-emitting posture as the rest of this file.
try:
    from _hooklib import resolve_runtime_dir
    _RUNTIME_DIR = resolve_runtime_dir(ENGRAM_HOME, "engram_log_emitter.py")
    if _RUNTIME_DIR not in sys.path:
        sys.path.insert(0, _RUNTIME_DIR)
except Exception:
    pass

# alpha #2574 WS3: separate resolver for engram_telemetry_store.py -- per
# _hooklib.resolve_runtime_dir's own docstring, "one call per module you
# actually import; do not collapse two different imports onto one sentinel
# just because they happen to live in the same directory today" (gh#2570).
# Guarded the same way as the emitter resolver above -- same fail-toward-
# emitting posture this whole file uses (this new addition does not touch
# the existing bootstrap above it).
try:
    from _hooklib import resolve_runtime_dir as _resolve_runtime_dir_for_telemetry
    _TELEMETRY_RUNTIME_DIR = _resolve_runtime_dir_for_telemetry(ENGRAM_HOME, "engram_telemetry_store.py")
    if _TELEMETRY_RUNTIME_DIR not in sys.path:
        sys.path.insert(0, _TELEMETRY_RUNTIME_DIR)
except Exception:
    pass
try:
    from engram_telemetry_store import write_telemetry_record
except Exception:
    def write_telemetry_record(manifest_id, event_type, fields=None):  # pragma: no cover -- import-failure fallback
        return False

# alpha #2574 WS3: this hook's TRIGGER-ENTRY site literal (participation
# manifest_id) for engram.telemetry.reached -- see main()'s first line.
_MAIN_MANIFEST_ID = "hooks/claude/engram-loop-arm-stop-hook.py:main"
_HOOK_FIRE_SITE = "loop-arm-stop-hook.hook_fire"

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

# #3628: the two marker reads below (platform.json, loop-mode.json) must be
# non-blocking and regular-file-only. They use _hooklib's helper when it can be
# imported. This hook's contract (see the emitter resolver note above, and
# test_emits_via_fallback_when_stophooklib_import_fails) is that it must still
# work, and still emit its nudge, when it runs with NO sibling lib at all, so it
# cannot hard-import _hooklib: on ImportError it defines the same two helpers
# itself. The fallback bodies are pinned identical to _hooklib's by
# tests/test_regular_io_parity_3628.py (AST + behavioural) -- never edit one
# without the other.
try:
    from _hooklib import atomic_write_json as _atomic_write_json
except Exception:
    _atomic_write_json = None

# #4380: the reply-gate log is WRITTEN (appended, and trimmed by rotation), so it must go
# through the strict open (no O_TRUNC at open, O_NOFOLLOW, single regular file). The helper is
# NOT copied here: this hook's log is diagnostic and fail-silent, so with no _hooklib it simply
# does not log (the gate decision never depends on it).
try:
    from _hooklib import _open_single_regular as _strict_open
    from _hooklib import report_refused_write as _report_refused_write
except Exception:
    _strict_open = None
    _report_refused_write = None

try:
    from _hooklib import open_regular_text
except Exception:
    def _not_regular_error(fd, path):
        """The OSError for a path that opened but is not a regular file. A
        directory keeps the IsADirectoryError a plain open() raises for it; any
        other kind (FIFO, socket, device) is an EINVAL OSError. Does not close fd:
        the caller's except clause does, once."""
        import errno
        try:
            is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        except OSError:
            is_dir = False
        code = errno.EISDIR if is_dir else errno.EINVAL
        return OSError(code, os.strerror(code) + " (not a regular file)", str(path))

    def open_regular_text(path, encoding=None):
        """`open(path, encoding=encoding)` for a read that must never block (#3591).

        A plain open() of a FIFO waits for a writer; a hook under a 5 s timeout
        hangs and is killed. This opens O_NONBLOCK (returns at once on a FIFO),
        fstat-checks that the target is a regular file, and returns a text file
        object over the fd. Anything else raises OSError (IsADirectoryError for a
        directory, EINVAL otherwise), so a call site that already treats an
        unreadable file as absent -- `except OSError` or broader -- needs only
        `open(p)` -> `open_regular_text(p)`. FileNotFoundError / PermissionError
        come from os.open exactly as from open(). For a regular file the result is
        the same text wrapper open() returns."""
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _not_regular_error(fd, path)
            return os.fdopen(fd, "r", encoding=encoding)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

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

# Inline copy, not _hooklib's: every _hooklib import in this hook is guarded, so it cannot be assumed here.
# Pinned identical to every other copy by tests/test_bounded_sibling_lookups_3983.py.
def _bounded_parents(here):
    """Directories to search for a sibling runtime file: `here`'s own directory
    up to and INCLUDING its own tree root, nearest first, never above it (#3983).
    The tree root is the nearest ancestor holding engram_core.py (the plugin root
    installed, src/engram/ in a source checkout; an essential-tier file, so every
    build ships it).  server.py is no marker: viz/server.py exists.  With no
    engram_core.py above, only the own directory and its parent are searched
    (the conservative case).  An unreadable directory is skipped
    (never taken for the root), so the walk still ends at the first readable root."""
    chain = []
    for d in here.parents:
        chain.append(d)
        try:
            if (d / "engram_core.py").exists():
                return chain
        except OSError:
            continue  # unreadable: not a root we can see; keep climbing
    return chain[:2]


_lp_candidates = []
_lp_plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
if _lp_plugin_root:
    _lp_candidates.append(Path(_lp_plugin_root) / "tools")
# Own tree only: up to and including the tree root, never above it (#3983).
for _lp_parent in _bounded_parents(Path(__file__).resolve()):
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

# ---------------------------------------------------------------------------
# The helper command the nudge PRINTS must RUN AS PRINTED.  The resolution
# above already found the real tools/ dir (`_LP_TOOLS_DIR`), so print THAT:
# a static guess is copied from one deploy layout and broken in the other —
# the bare `tools/loop_prompt.py` this replaced resolved in NO cwd, not even
# the repo root, because the source tree keeps the file at `src/engram/tools/`.
# Only when the import resolution FAILED does the printed command fall back
# to a sh form whose `:-` default covers a plugin-topology seat with the
# variable set AND one that has the plugin installed but
# `$CLAUDE_PLUGIN_ROOT` unset in its shell (measured: unset in an agent's
# Bash env, login shell included).
# ---------------------------------------------------------------------------

_LOOP_PROMPT_FALLBACK_PATH = (
    '"${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/marketplace/plugins/engram}'
    '/tools/loop_prompt.py"'
)


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


def _runtime_platform() -> str | None:
    """Return a baked runtime platform when a plugin bundle provides one.

    A baked profile is stronger evidence than an ambient environment variable:
    a user shell can retain ``CODEX_PLUGIN_ROOT`` while a Claude bundle is the
    hook's actual runtime. Source/dev hooks normally have no profile, so the
    caller may use the Codex root variable as the documented fallback.
    """
    # The bundle containing this hook is the runtime actually executing this
    # code. Its baked platform profile must win over every ambient root: a
    # shell can retain either plugin-root variable after a harness switch.
    # Environment roots are source/dev fallbacks only, when this colocated
    # bundle has no profile.
    candidates: list[Path] = [Path(__file__).resolve().parent.parent]
    for env_name in ("ENGRAM_RUNTIME_DIR", "CODEX_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        value = os.environ.get(env_name, "").strip()
        if value:
            candidates.append(Path(value))
    for runtime_dir in candidates:
        try:
            with open_regular_text(runtime_dir / "platform.json", encoding="utf-8") as f:
                data = json.load(f)
            platform = data.get("platform")
            if isinstance(platform, str):
                return platform.strip().lower()
        except Exception:
            continue
    return None


def _is_managed_codex_runtime() -> bool:
    """Detect Codex from reliable bundle evidence, then its runtime root."""
    platform = _runtime_platform()
    if platform is not None:
        return platform == "codex"
    return bool(os.environ.get("CODEX_PLUGIN_ROOT", "").strip())


_NUDGE_HEAD = (
    "[Loop Arm Check: a loop marker is active and this turn shows no "
    "verified re-arm (ScheduleWakeup + CronList in the same parallel block). "
    "Arm now — read `cadence_seconds` from the marker (it is already in your "
    "context) and batch `ScheduleWakeup(delay=cadence_seconds)` and `CronList` "
    "in ONE parallel block — the ONLY documented arm shape; a solo "
    "`ScheduleWakeup` without a `CronList` sibling in the same parallel block "
    "IS the defect (#4007). "
)
# INVARIANT (#4139): the nudge never names a marker field that the marker
# doesn't have. This sentence is only emitted when the marker carries a
# non-empty-str `loop_prompt`; otherwise _nudge_text_for_marker swaps in the
# helper-pointing sentence instead.
_NUDGE_LOOP_PROMPT_FIELD_SENTENCE = (
    "Use this file's own `loop_prompt` field verbatim "
    "for the ScheduleWakeup prompt — never paraphrase. "
)
_NUDGE_TAIL = (
    "The CronList result is "
    "your verify step: confirm the prompt leads with "
    f"{_LOOP_WAKE_MARKER}. "
    "Arming is idempotent: a redundant arm replaces, never stacks. If your "
    "NEXT turn will carry a user-facing reply: batch the arm with a sibling "
    "tool call in one parallel block and put the reply after it as the turn's "
    "final text — a solo arm ends the turn, a trailing arm swallows the reply "
    "(engram-loop Step 2 item 3, #2098). If a reply already rendered this "
    "turn, arm now and add nothing after.]"
)
_NUDGE_TEXT = _NUDGE_HEAD + _NUDGE_LOOP_PROMPT_FIELD_SENTENCE + _NUDGE_TAIL


def _loop_prompt_helper_path() -> str:
    """The printed path of tools/loop_prompt.py: the directory the import
    resolution above actually found (`_LP_TOOLS_DIR`), else the sh-form
    fallback (already quoted) -- same policy as the comment above
    `_LOOP_PROMPT_FALLBACK_PATH`. No new path logic."""
    if _LP_TOOLS_DIR is not None:
        return '"' + str(Path(_LP_TOOLS_DIR) / "loop_prompt.py") + '"'
    return _LOOP_PROMPT_FALLBACK_PATH


def _marker_has_loop_prompt(loop_marker_path: str) -> bool:
    """True iff the marker parses and carries a non-empty str `loop_prompt`.
    Unreadable / unparseable / non-dict marker -> False (never raises)."""
    try:
        with open_regular_text(loop_marker_path, encoding="utf-8") as f:
            marker = json.load(f)
        lp = marker.get("loop_prompt")
        return isinstance(lp, str) and bool(lp)
    except Exception:  # noqa: BLE001
        return False


def _nudge_text_for_marker(loop_marker_path: str) -> str:
    """Choose the arm-nudge text so it never names a marker field the marker
    lacks (#4139). C1: non-empty str `loop_prompt` -> `_NUDGE_TEXT`, byte
    for byte. C2/C3: anything else (absent, null, "", non-string, unreadable
    or unparseable marker) -> the helper-pointing sentence."""
    if _marker_has_loop_prompt(loop_marker_path):
        return _NUDGE_TEXT
    return (
        _NUDGE_HEAD
        + "This marker has no `loop_prompt` field: build the prompt with "
        + f"`python3 {_loop_prompt_helper_path()} --default-stub` for a heartbeat loop "
        + "(for any other loop kind, build it from one line saying what that wake does, "
        + f"e.g. `echo '<one line saying what this wake does>' | python3 {_loop_prompt_helper_path()}`) "
        + "(never hand-write it). "
        + _NUDGE_TAIL
    )


_CODEX_PACER_NUDGE_TEXT = (
    "[Loop Arm Check: managed Codex is active but this loop marker says "
    "pacer scheduleWakeup. Do not use the Claude wake path. Verify the "
    "managed codex-umwelt-wake.py bridge and this agent's systemd heartbeat "
    "timer, then correct the marker to pacer systemd before continuing.]"
)


# alpha #4104: a human's reply is swallowed when the turn ends on a tool call
# (only a turn's FINAL assistant text renders). Deliberately says nothing about
# arming: this block must never push the agent back into a trailing arm.
_REPLY_NOT_RENDERED_TEXT = (
    "[Reply Delivery Check: your reply to the human was not rendered, because "
    "this turn ends on a tool call and only a turn's final text renders. Write "
    "the reply now as the turn's final text, with no tool call after it.]"
)

# Wake-pending: a wake due further out than this is "pending" (#4104).
_WAKE_PENDING_MIN_SECONDS = 90
# Wake-pending: tail-scan bound for the transcript file.
_WAKE_PENDING_TAIL_BYTES = 4 * 1024 * 1024


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
        with open_regular_text(loop_marker_path, encoding="utf-8") as f:
            marker = json.load(f)
    except Exception:
        return False
    pacer = marker.get("pacer")
    if not isinstance(pacer, str):
        return False
    return pacer.strip().lower() == _SCHEDULE_WAKEUP_PACER


def _read_waiting_on(loop_marker_path: str) -> tuple[str, str] | None:
    """Return (description, return_by_str) if the loop marker carries an
    active (not yet expired) waiting_on declaration; None otherwise.

    Active = present, description is a non-empty string, return_by parses
    as a future ISO-8601 timestamp. Any exception → None (never raises).
    """
    try:
        with open_regular_text(loop_marker_path, encoding="utf-8") as f:
            marker = json.load(f)
        wo = marker.get("waiting_on")
        if not isinstance(wo, dict):
            return None
        desc = wo.get("description")
        rb_str = wo.get("return_by")
        if not isinstance(desc, str) or not desc.strip():
            return None
        if not isinstance(rb_str, str) or not rb_str.strip():
            return None
        rb = datetime.fromisoformat(rb_str.replace("Z", "+00:00"))
        if rb.tzinfo is None:
            rb = rb.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if rb <= now:
            return None
        return desc.strip(), rb_str.strip()
    except Exception:  # noqa: BLE001
        return None


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
    """Scan the delta for a batched arm: ScheduleWakeup + CronList in ONE
    parallel block (same message.id group of consecutive assistant records).

    Real Claude Code parallel blocks: each tool_use is written as its own
    assistant record sharing the same `message.id`. Confirmed iff some
    message.id group contains BOTH a loop-wake ScheduleWakeup AND a CronList.

    Records without a `message.id` are treated as isolated singleton groups
    (backward compat with synthetic fixtures that write multiple tool_use
    blocks in one record's content array — the single-record shape with both
    SW and CronList in the same content still confirms correctly).

    A solo ScheduleWakeup (no CronList in its group) is the defect (#4007).
    """
    if not delta_bytes:
        return False
    records, _any_record_parsed = _parse_jsonl_records(delta_bytes)

    # groups: group_key -> {"has_sw": bool, "has_cronlist": bool}
    groups: dict[object, dict[str, bool]] = {}
    _unique_counter = 0

    for rec in records:
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        msg_id = msg.get("id")
        if msg_id is None:
            _unique_counter += 1
            group_key: object = f"_solo_{_unique_counter}"
        else:
            group_key = msg_id

        if group_key not in groups:
            groups[group_key] = {"has_sw": False, "has_cronlist": False}

        for block in _walk_content_blocks(content):
            if block.get("type") != "tool_use":
                continue
            if block.get("name") in _WAKE_ARMING_TOOL_NAMES:
                text = _tool_use_prompt_text(block)
                if text and _LOOP_WAKE_MARKER in html.unescape(text):
                    groups[group_key]["has_sw"] = True
            elif block.get("name") == "CronList":
                groups[group_key]["has_cronlist"] = True

    return any(g["has_sw"] and g["has_cronlist"] for g in groups.values())


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


# ---------------------------------------------------------------------------
# alpha #3919 M2 -- quiet-wake counter.
#
# A QUIET WAKE is one turn (the transcript delta this hook already computes)
# such that ALL hold:
#   1. it was STARTED by a wake-class trigger: the delta's FIRST user-role text
#      record carries `<loop-wake>` (after HTML-unescape) or `<task-notification>`;
#   2. every tool_use is in the observe-or-arm ALLOWLIST below, and every Bash
#      tool_use passes `_bash_is_read_only`;
#   3. the delta parses (unparseable / empty -> NOT quiet; fail toward not nudging).
# Transcript-only: no regex over assistant prose, no network.
# Codex managed path and non-scheduleWakeup pacers are out of scope here.
# ---------------------------------------------------------------------------

# Consecutive quiet wakes before the nudge fires.
QUIET_TICKS_THRESHOLD = 2

# Within N: quiet, no block (systemMessage advisory fine).
# Beyond N, first trigger: one study-unit block, then quiet until return_by.
_WAITING_ON_NEAR_ZONE_MINUTES = 10

# A REVIEWED constant: adding a verb is a deliberate change (a contract cell
# pins the exact membership). Plain harness tools, plus umwelt's MCP READ verbs
# (matched by the part after the last `__`, on an umwelt server only -- see
# `_tool_name_is_observe_or_arm`). `checklist` (can mutate) and `open` (not
# verified read-only) are deliberately NOT here. `Bash` is handled separately,
# under the read-only command check.
_UMWELT_READ_VERBS = frozenset({
    "whats_new", "hear", "look_around", "look_at", "look_in",
    "crossings_here", "crossings_for_agent", "search_crossings",
    "thread", "map", "where_can_i_go", "inventory",
})
QUIET_WAKE_OBSERVE_OR_ARM_TOOLS = frozenset({
    "ScheduleWakeup", "CronList", "Monitor", "TaskStop", "ToolSearch",
    "Read", "Glob", "Grep",
}) | _UMWELT_READ_VERBS

_QUIET_NUDGE_TEXT = (
    "[Quiet Wake Check: {n} quiet wakes in a row (no work done, only looking "
    "and re-arming). Take an OWNER step from your board, or run ONE study unit: "
    "one target, one method, one written ENGRAM result. Next act: run "
    "`python3 tools/study_queue.py` once it exists (alpha #3919 M3); until "
    "then read the latest dream report's `For the awake seat` section "
    "(`~/.engram/history/dream/`, most recent file).]"
)

_QUIET_ADVISORY_TEXT = (
    "[Tick Rule — known-return wait: {n} quiet wake(s) while waiting for "
    "\"{desc}\" (return by {rb_str}). Inside a declared bounded wait, quiet "
    "wakes are expected — this is advisory, not a demand. Counter reset. "
    "Full tick rule: engram-loop skill.]"
)

_QUIET_STUDY_UNIT_TEXT = (
    "[Tick Rule — study unit: waiting for \"{desc}\" (return by {rb_str}). "
    "This wait is longer than one study unit. Take one bounded study unit "
    "— one target, one method, one written ENGRAM result — before the "
    "quiet hold begins. After this unit the hook is quiet until return_by. "
    "Full tick rule: engram-loop skill.]"
)

_BASH_PIPE_SINKS = frozenset({"head", "tail", "wc", "grep", "sort", "uniq", "cut"})
_BASH_PLAIN_READERS = frozenset({"pgrep", "ps", "ls", "cat", "head", "tail", "wc", "grep", "pwd"})
_BASH_GH_READ_VERBS = frozenset({"view", "list", "checks", "status"})
_BASH_GH_API_WRITE_SHORT = ("-X", "-f", "-F")  # also matched attached, e.g. -XPOST
_BASH_GH_API_WRITE_LONG = ("--method", "--field", "--raw-field", "--input")
_BASH_GIT_READ_VERBS = frozenset({"status", "log", "diff", "show", "rev-parse"})  # `fetch` has its own rule
_BASH_GIT_BRANCH_FLAGS = frozenset({"-a", "-r", "-v", "-vv", "--all", "--remotes", "--verbose", "--list", "-l", "--show-current"})
# The re-arm helper (--rearm-delay flag): prints cadence_seconds and is an ARM action, so a
# heartbeat turn that calls it is quiet. Whole-command match only.
# Nudge no longer prescribes the helper (#4007 — arm reads cadence_seconds directly).
_LOOP_PROMPT_HELPER_RE = re.compile(
    r"(?:(?P<w>[A-Za-z_]\w*=\$\()|)"
    r"python3?\s+[\"']?(?:[\w./~$-]*/)?loop_prompt\.py[\"']?\s+--rearm-delay"
    r"(?:\s+--wake-kind\s+(?:heartbeat|other))?"
    r"(?:\s+--marker\s+[\w./~-]+)?"
    r"(?(w)\))"
)
# The standard supervisor/bell arm commands (a Monitor whose command is anything else is not an arm).
_MONITOR_ARM_COMMAND_RE = re.compile(
    r"(?:UMWELT_BELL_QUIET_ARM=1\s+)?"
    r"(?:python3?\s+[\w./~$-]*(?:monitor-supervisor|umwelt-bell)\.py"
    r"|bash\s+[\w./~$-]*(?:forum-updates-monitor|forum-mention-monitor)\.sh)"
    r"(?:\s+[\w./~:@=,$-]+)*"
)
_BASH_CD_PREFIX = re.compile(r"^\s*cd\s+[^\s;&|<>$`()]+\s*&&\s*")


def _short_cluster(a: str) -> str | None:
    """The letters of a single-dash short-flag cluster (`-iXPOST` -> `iXPOST`), else None."""
    return a[1:] if len(a) > 1 and a[0] == "-" and a[1] != "-" else None


def _gh_api_args_have_write_flag(args: list) -> bool:
    for a in args:
        cluster = _short_cluster(a)
        # gh/pflag accept clustered shorthand and an attached value: -XPOST, -iXPOST, -ifa=b
        if cluster is not None and any(c in cluster for c in "XfF"):
            return True
        if any(a == f or a.startswith(f + "=") for f in _BASH_GH_API_WRITE_LONG):
            return True
    return False


def _simple_command_is_read_only(tokens: list, *, piped_sink: bool) -> bool:
    """One already-tokenised simple command against the read-only allowlist."""
    if not tokens:
        return False
    first, rest = tokens[0], tokens[1:]
    if piped_sink:
        if first not in _BASH_PIPE_SINKS:
            return False
        if first == "sort":
            # `-o FILE` writes a file (also clustered: -uo, -ro); --compress-program runs a program
            return not any(
                (_short_cluster(a) is not None and "o" in _short_cluster(a))
                or a.startswith("--output") or a.startswith("--compress-program")
                for a in rest
            )
        if first == "uniq":
            return all(a.startswith("-") for a in rest)  # a 2nd positional is an OUTPUT file
        return True
    if first == "baton":
        return bool(rest) and rest[0] in ("mine", "status", "show")
    if first == "gh":
        # `gh -R owner/repo pr list`: the repo flag may come before the noun
        while len(rest) >= 2 and rest[0] in ("-R", "--repo"):
            rest = rest[2:]
        if any(a in ("--web", "-w") for a in rest):
            return False  # opens a browser
        if rest and rest[0] == "api":
            return not _gh_api_args_have_write_flag(rest[1:])
        return len(rest) >= 2 and rest[1] in _BASH_GH_READ_VERBS
    if first == "git":
        if not rest:
            return False
        while len(rest) >= 2 and rest[0] == "-C":  # `git -C <path> log ...`
            rest = rest[2:]
        if not rest:
            return False
        verb, args = rest[0], rest[1:]
        if any(a.startswith("--output") or a in ("--ext-diff", "--textconv", "--open-files-in-pager") or a == "-O"
               for a in args):
            return False  # `git diff --output=FILE` writes a file; --ext-diff runs a program
        if verb == "fetch":
            # bare `git fetch` / `git fetch <remote>` only: a refspec (`a:b`) or --prune writes or deletes refs
            return len(args) <= 1 and all(not a.startswith("-") and ":" not in a for a in args)
        if verb in _BASH_GIT_READ_VERBS:
            return True
        if verb == "branch":  # bare listing only; any other arg could create/delete/rename
            return all(a in _BASH_GIT_BRANCH_FLAGS for a in args)
        if verb == "worktree":
            return bool(args) and args[0] == "list"
        if verb == "tag":
            return bool(args) and args[0] == "--points-at"
        return False
    if first == "date":
        # `date -s` sets the clock (also clustered: -us)
        # (and a bare positional like `date 101914302026` sets it on BSD: only +FORMAT is allowed)
        return not any(
            (_short_cluster(a) is not None and "s" in _short_cluster(a)) or a.startswith("--set")
            or (not a.startswith("-") and not a.startswith("+"))
            for a in rest
        )
    return first in _BASH_PLAIN_READERS


def _split_pipes(cmd: str) -> list:
    """Split on `|` outside single/double quotes (`grep -E 'a|b' f` is one stage)."""
    parts, cur, quote = [], [], None
    for ch in cmd:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == "|":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _bash_is_read_only(command: str) -> bool:
    """ALLOWLIST, whole-command: True only for a single simple read-only command
    (optionally after one leading `cd <path> &&`, optionally one pipe into
    head/tail/wc/grep/sort/uniq/cut). Anything else, or anything unparseable,
    is False. Conservative on purpose: a quoted `;` or `>` also refuses."""
    try:
        if not isinstance(command, str):
            return False
        cmd = _BASH_CD_PREFIX.sub("", command, count=1).strip()
        if not cmd:
            return False
        if _LOOP_PROMPT_HELPER_RE.fullmatch(cmd):
            return True
        if any(ch in cmd for ch in (";", "`", ">", "<", "&", "\n", "\r")) or "$(" in cmd or "||" in cmd:
            return False
        parts = _split_pipes(cmd)
        if len(parts) > 3:
            return False
        for i, part in enumerate(parts):
            if not _simple_command_is_read_only(shlex.split(part), piped_sink=(i >= 1)):
                return False
        return True
    except Exception:  # noqa: BLE001 -- anything we cannot parse is not provably read-only
        return False


def _tool_name_is_observe_or_arm(name: object) -> bool:
    if not isinstance(name, str) or not name:
        return False
    if "__" in name:
        # mcp__<server>__<verb>: an umwelt server's read verb only. A same-named
        # verb on some other server is not assumed read-only.
        parts = name.split("__")
        return (
            parts[0] == "mcp"
            and len(parts) >= 3
            and "umwelt" in parts[1]
            and parts[-1] in _UMWELT_READ_VERBS
            and parts[-1] in QUIET_WAKE_OBSERVE_OR_ARM_TOOLS
        )
    return name in QUIET_WAKE_OBSERVE_OR_ARM_TOOLS and name not in _UMWELT_READ_VERBS


_STOP_HOOK_FEEDBACK_PREFIX = "Stop hook feedback"
# The compaction summary is a user-role record too, and it quotes the marker in its prose.
_COMPACTION_SUMMARY_PREFIX = "This session is being continued from a previous conversation"
_NOT_A_TRIGGER_PREFIXES = (_STOP_HOOK_FEEDBACK_PREFIX, _COMPACTION_SUMMARY_PREFIX)


def _record_user_text(rec: dict) -> str | None:
    """Text of a user-role record, or None when it carries no text (e.g. a
    tool_result-only record)."""
    if rec.get("type") != "user":
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            b.get("text") for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        return "\n".join(texts) if texts else None
    return None


def _delta_is_quiet_wake(delta_bytes: bytes) -> bool:
    """True iff this turn's delta is a QUIET WAKE (definition above)."""
    if not delta_bytes or not delta_bytes.strip():
        return False
    records, any_parsed = _parse_jsonl_records(delta_bytes)
    if not any_parsed or not records:
        return False
    # Strict: a delta with ANY unparseable non-blank line is "unparseable".
    nonblank = [ln for ln in delta_bytes.decode("utf-8", errors="replace").split("\n") if ln.strip()]
    if len(records) != len(nonblank):
        return False

    first_text = None
    for rec in records:
        text = _record_user_text(rec)
        if text is None:
            continue
        # (Also the compaction summary, see _COMPACTION_SUMMARY_PREFIX.)
        # A Stop-hook block arrives as a user-role record ("Stop hook feedback: ...") and, for
        # this very hook, quotes the loop-wake marker. It is a continuation, not a trigger: the
        # hook's offset is written at the block, so the NEXT fire's delta still carries it (and
        # the continuation turn) ahead of the real trigger. Skip it.
        if text.lstrip().startswith(_NOT_A_TRIGGER_PREFIXES):
            continue
        first_text = text
        break
    if first_text is None:
        return False
    unescaped = html.unescape(first_text)
    if _LOOP_WAKE_MARKER not in unescaped and "<task-notification>" not in unescaped:
        return False

    for rec in records:
        if rec.get("type") != "assistant":
            continue
        message = rec.get("message")
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            return False
        for block in _walk_content_blocks(content):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if name == "Bash":
                input_ = block.get("input")
                command = input_.get("command") if isinstance(input_, dict) else None
                if not _bash_is_read_only(command):
                    return False
            elif name == "Monitor":
                input_ = block.get("input")
                mcmd = input_.get("command") if isinstance(input_, dict) else None
                # an arm is the standard supervisor/bell command (or a websocket watch, no command);
                # any other Monitor command could do anything, so it is not quiet
                if mcmd is not None and not (
                    isinstance(mcmd, str)
                    and (_MONITOR_ARM_COMMAND_RE.fullmatch(mcmd.strip()) or _bash_is_read_only(mcmd))
                ):
                    return False
            elif not _tool_name_is_observe_or_arm(name):
                return False
    return True


def _read_turn_delta(transcript_path: str) -> bytes | None:
    """The transcript bytes since this hook's own last fire (same boundary as
    `_delta_has_arm_confirmation`), or None when unreadable."""
    if not transcript_path or not _STOPHOOKLIB_OK:
        return None
    try:
        current_size = os.path.getsize(transcript_path)
    except OSError:
        return None
    stored_offset = 0
    try:
        stored_path, offset = _read_offset_state(_LOOP_ARM_STATE_FILE)
        if stored_path == transcript_path and 0 <= offset <= current_size:
            stored_offset = offset
    except Exception:  # noqa: BLE001
        pass
    if stored_offset >= current_size:
        return b""
    try:
        return _read_delta(transcript_path, stored_offset, current_size)
    except OSError:
        return None


def _strict_records(delta_bytes: bytes) -> list | None:
    """Records of a delta, or None unless EVERY non-blank line parses (the
    strictness `_delta_is_quiet_wake` applies; the #4104 reply gate fails
    toward today's behaviour on anything less)."""
    if not delta_bytes or not delta_bytes.strip():
        return None
    records, any_parsed = _parse_jsonl_records(delta_bytes)
    if not any_parsed or not records:
        return None
    nonblank = [ln for ln in delta_bytes.decode("utf-8", errors="replace").split("\n") if ln.strip()]
    if len(records) != len(nonblank):
        return None
    return records


# Local to _human_prompt_index (deliberately NOT in _NOT_A_TRIGGER_PREFIXES:
# other consumers keep today's behaviour).
# Injected user text with no provenance fields: a skill expansion, and the
# compact-request post-compaction context (tools/compact-request.py).
_INJECTED_TEXT_PREFIXES = (
    "Base directory for this skill:",
    "# Post-compaction context",
)


def _record_fields_say_not_human(rec: dict) -> bool:
    """True when a user record's own provenance fields mark it as injected.

    Real Claude Code user records carry `origin` / `promptSource` /
    `turnOrigin` / `isMeta` (a typed prompt: origin.kind "human", promptSource
    "typed", turnOrigin "human"; skill expansions and Stop-hook feedback are
    isMeta). These are EXCLUSIONS only, applied when a field is present; old
    transcripts and synthetic fixtures without them keep the text rules."""
    if rec.get("isMeta") is True:
        return True
    origin = rec.get("origin")
    if isinstance(origin, dict):
        kind = origin.get("kind")
        if isinstance(kind, str) and kind != "human":
            return True
    turn_origin = rec.get("turnOrigin")
    if isinstance(turn_origin, str) and turn_origin != "human":
        return True
    return rec.get("promptSource") == "system"


def _human_prompt_index(records: list) -> int | None:
    """Index of the LAST user record that is a genuine human prompt: has text
    that is not Stop-hook feedback / a compaction summary, carries no
    `<loop-wake>` marker and no `<task-notification>` (after HTML-unescape).
    A later wake / task-notification record clears an earlier human prompt;
    so does a record that merely QUOTES the marker inside a human prompt (fails
    toward today's behaviour; rare, accepted). Stop-hook feedback, compaction
    summaries and skill expansions never clear."""
    found = None
    for i, rec in enumerate(records):
        text = _record_user_text(rec)
        if text is None or not text.strip():
            continue
        if text.lstrip().startswith(_NOT_A_TRIGGER_PREFIXES):
            continue
        # Skill expansions / post-compaction context are injected text that may
        # quote the marker (the engram-loop skill does): not human, and must not
        # clear a human prompt either.
        if text.lstrip().startswith(_INJECTED_TEXT_PREFIXES) or rec.get("turnCompanion") is True:
            continue
        unescaped = html.unescape(text)
        if _LOOP_WAKE_MARKER in unescaped or "<task-notification>" in unescaped:
            # A wake / task notification after the human prompt starts a new
            # turn: the turn being ended is not the human's. Checked BEFORE the
            # field exclusions because real wake records are isMeta too.
            found = None
            continue
        if _record_fields_say_not_human(rec):
            continue
        found = i
    return found


def _reply_rendered(records: list, after: int) -> bool:
    """True iff, after record index `after`, the LAST assistant record carries
    non-empty text and no tool_use follows that text (an earlier tool_use is
    fine: the text must be last)."""
    last = None
    for rec in records[after + 1:]:
        if rec.get("type") == "assistant":
            last = rec
    if last is None:
        return False
    message = last.get("message")
    content = message.get("content", []) if isinstance(message, dict) else []
    if not isinstance(content, list):
        return False
    text_seen = False
    for block in _walk_content_blocks(content):
        btype = block.get("type")
        if btype == "text":
            body = block.get("text")
            if isinstance(body, str) and body.strip():
                text_seen = True
        elif btype == "tool_use":
            text_seen = False  # a tool call after the text: the text is not final
    return text_seen


def _delta_reply_state(delta_bytes: bytes) -> tuple[bool, bool]:
    """(has_human_prompt, reply_rendered) for a turn delta. (False, False) when
    the delta cannot be parsed strictly or holds no human prompt."""
    records = _strict_records(delta_bytes)
    if records is None:
        return (False, False)
    idx = _human_prompt_index(records)
    if idx is None:
        return (False, False)
    return (True, _reply_rendered(records, idx))


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


def _payload_gate_facts(delta_bytes: bytes, payload_norm: str | None) -> tuple[bool, bool] | None:
    """(payload_found_in_delta, delta_ends_on_tool_use) for the #4193 override, or
    None when the delta cannot be read (the caller then keeps the block). Only the
    FINAL MESSAGE counts (identified by message.id, see below); text of an earlier
    message, before an intervening tool_result, is not the final message."""
    records = _strict_records(delta_bytes)
    if records is None:
        return None
    idx = _human_prompt_index(records)
    if idx is None:
        return None
    # The final message = the assistant records after the prompt that share the LAST
    # assistant record's message.id, wherever non-assistant records (tool_results of
    # parallel calls) sit between them. No usable id: that record's adjacent run.
    last = None
    for i in range(len(records) - 1, idx, -1):
        if records[i].get("type") == "assistant":
            last = i
            break
    chosen = []
    if last is not None:
        msg = records[last].get("message")
        mid = msg.get("id") if isinstance(msg, dict) else None
        if isinstance(mid, str) and mid:
            for rec in records[idx + 1:last + 1]:
                m = rec.get("message")
                if rec.get("type") == "assistant" and isinstance(m, dict) and m.get("id") == mid:
                    chosen.append(rec)
        else:
            start = last
            while start > idx + 1 and records[start - 1].get("type") == "assistant":
                start -= 1
            chosen = records[start:last + 1]
    texts = []
    last_type = None
    for rec in chosen:
        message = rec.get("message")
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            continue
        for block in _walk_content_blocks(content):
            last_type = block.get("type")
            body = block.get("text")
            if last_type == "text" and isinstance(body, str) and body.strip():
                texts.append(_norm_ws(body))
    # Feeds only the log line, never the decision.
    ends_on_tool = last_type == "tool_use"
    found = bool(payload_norm and texts
                 and (payload_norm in _norm_ws(" ".join(texts)) or payload_norm.endswith(texts[-1])))
    # Also found when P is text ALREADY flushed anywhere in this turn: an unflushed final
    # reply appears nowhere in the delta, so such a P cannot be it (a "last text produced in
    # the turn" reading of the field must not silence a swallowed reply).
    if payload_norm and not found:
        turn_texts = []
        for rec in records[idx + 1:]:
            m = rec.get("message")
            c = m.get("content", []) if rec.get("type") == "assistant" and isinstance(m, dict) else []
            if isinstance(c, list):
                turn_texts.extend(_norm_ws(b["text"]) for b in _walk_content_blocks(c)
                                  if b.get("type") == "text" and isinstance(b.get("text"), str)
                                  and b["text"].strip())
        found = payload_norm in _norm_ws(" ".join(turn_texts))
    return (found, ends_on_tool)


_REPLY_GATE_LOG = os.path.join(ENGRAM_HOME, "loop-arm-reply-gate.log")
_REPLY_GATE_LOG_MAX_BYTES = 64 * 1024
_REPLY_GATE_LOG_KEEP_LINES = 200


def _reply_gate_fd() -> int:
    """A non-blocking READ-ONLY fd on the log that must be a regular file (never hangs on a FIFO)."""
    fd = os.open(_REPLY_GATE_LOG, os.O_RDONLY | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("not a regular file: %s" % (_REPLY_GATE_LOG,))
    return fd


def _log_reply_gate(session_id: str, payload_len: int | None, facts: tuple[bool, bool] | None,
                    decision: str) -> None:
    """alpha #4193: one diagnostic line per reply-gate decision. Never logs payload
    text; fail-silent (a logging error must never change the decision)."""
    try:
        found, ends_on_tool = facts if facts is not None else (False, False)
        line = "%s session=%s payload_len=%s found=%s ends_tool_use=%s decision=%s\n" % (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            re.sub(r"\s+", "_", session_id)[:80],
            "absent" if payload_len is None else payload_len,
            "y" if found else "n", "y" if ends_on_tool else "n", decision)
        data = line.encode("utf-8")
        rotate = False
        try:
            if os.path.getsize(_REPLY_GATE_LOG) > _REPLY_GATE_LOG_MAX_BYTES:
                fd = _reply_gate_fd()
                with os.fdopen(fd, "rb") as f:
                    kept = f.read().splitlines(keepends=True)[-_REPLY_GATE_LOG_KEEP_LINES:]
                data = b"".join(kept) + data
                rotate = True
        except OSError:
            pass
        # #4380: WRITE through the strict open (O_NOFOLLOW, single regular file, never O_TRUNC
        # at open). A symlink, hard link or non-regular file is refused (nothing touched; one
        # rate-limited stderr line names it) rather than appended to or truncated; the rotation
        # truncates only on the validated fd. With no _hooklib there is no log (diagnostic only).
        if _strict_open is None:
            return
        try:
            fd = _strict_open(_REPLY_GATE_LOG, os.O_WRONLY | os.O_CREAT | (0 if rotate else os.O_APPEND))
        except OSError as exc:
            if type(exc).__name__ == "NotRegularFile" and _report_refused_write is not None:
                _report_refused_write(exc)
            return
        if rotate:
            try:
                os.ftruncate(fd, 0)
            except BaseException:
                os.close(fd)
                raise
        with os.fdopen(fd, "wb" if rotate else "ab") as f:
            f.write(data)
    except Exception:  # noqa: BLE001
        pass


def _parse_iso_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith(("Z", "z")):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now_utc() -> datetime:
    """Current UTC time. ENGRAM_LOOP_ARM_NOW_ISO (ISO-8601) overrides it when
    set: a TEST SEAM for the wake-pending cells, nothing else reads it."""
    raw = os.environ.get("ENGRAM_LOOP_ARM_NOW_ISO")
    if raw:
        parsed = _parse_iso_utc(raw)
        if parsed is not None:
            return parsed
    return datetime.now(timezone.utc)


def _read_transcript_tail(transcript_path: str) -> bytes | None:
    """At most the last 4 MiB of the file; when truncated, the first (possibly
    partial) line is dropped. None when unreadable."""
    try:
        size = os.path.getsize(transcript_path)
        # non-blocking, regular-file-only open (#3628); bytes via its .buffer
        with open_regular_text(transcript_path) as tf:
            f = tf.buffer
            if size > _WAKE_PENDING_TAIL_BYTES:
                f.seek(size - _WAKE_PENDING_TAIL_BYTES)
                data = f.read(_WAKE_PENDING_TAIL_BYTES)
                nl = data.find(b"\n")
                return b"" if nl < 0 else data[nl + 1:]
            return f.read()
    except OSError:
        return None


def _loop_wake_arm_blocks(rec: dict) -> list:
    """The loop-wake ScheduleWakeup tool_use blocks of one assistant record."""
    if rec.get("type") != "assistant":
        return []
    message = rec.get("message")
    content = message.get("content", []) if isinstance(message, dict) else []
    if not isinstance(content, list):
        return []
    out = []
    for block in _walk_content_blocks(content):
        if block.get("type") == "tool_use" and block.get("name") in _WAKE_ARMING_TOOL_NAMES:
            prompt = _tool_use_prompt_text(block)
            if prompt and _LOOP_WAKE_MARKER in html.unescape(prompt):
                out.append(block)
    return out


def _wake_pending(transcript_path: str, now: datetime) -> bool:
    """True iff the LATEST assistant loop-wake ScheduleWakeup in the transcript
    (tail-bounded) is due more than 90 s after `now` and no later user text
    record carries the marker (a fired wake consumes it). A missing/invalid
    timestamp or delaySeconds, an unreadable file, or no such arm -> False
    (fail toward today's forced-arm behaviour)."""
    if not transcript_path:
        return False
    data = _read_transcript_tail(transcript_path)
    if not data:
        return False
    records, _parsed = _parse_jsonl_records(data)
    arm_idx = None
    for i, rec in enumerate(records):
        if _loop_wake_arm_blocks(rec):
            arm_idx = i
    if arm_idx is None:
        return False
    arm = records[arm_idx]
    armed_at = _parse_iso_utc(arm.get("timestamp"))
    if armed_at is None:
        return False
    delay = None
    for block in _loop_wake_arm_blocks(arm):
        d = block["input"].get("delaySeconds")
        if isinstance(d, (int, float)) and not isinstance(d, bool):
            delay = d
    if delay is None:
        return False
    for rec in records[arm_idx + 1:]:
        text = _record_user_text(rec)
        # Stop-hook feedback / compaction summaries quote the marker; they are
        # not a fired wake (same exclusion as the human-prompt definition).
        if text is None or text.lstrip().startswith(_NOT_A_TRIGGER_PREFIXES):
            continue
        if _LOOP_WAKE_MARKER in html.unescape(text):
            return False
    due = armed_at + timedelta(seconds=delay)
    return (due - now).total_seconds() > _WAKE_PENDING_MIN_SECONDS


def _read_quiet_ticks(transcript_path: str) -> int:
    """Stored count for THIS transcript; missing/corrupt/other-transcript -> 0."""
    try:
        with open_regular_text(_QUIET_WAKE_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        ticks = data.get("quiet_ticks")
        if (
            data.get("transcript_path") == transcript_path
            and isinstance(ticks, int)
            and not isinstance(ticks, bool)
            and ticks >= 0
        ):
            return ticks
    except Exception:  # noqa: BLE001
        pass
    return 0


def _read_waiting_on_block_rb(transcript_path: str) -> str | None:
    """Return the return_by string for which the once-only study-unit block has
    already fired, or None if it hasn't fired yet for this transcript."""
    try:
        with open_regular_text(_QUIET_WAKE_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("transcript_path") != transcript_path:
            return None
        rb = data.get("waiting_on_block_rb")
        return rb if isinstance(rb, str) and not isinstance(rb, bool) else None
    except Exception:  # noqa: BLE001
        return None


def _write_quiet_ticks(
    transcript_path: str, ticks: int, waiting_on_block_rb: str | None = None
) -> None:
    data: dict = {"transcript_path": transcript_path, "quiet_ticks": ticks}
    if waiting_on_block_rb is not None:
        data["waiting_on_block_rb"] = waiting_on_block_rb
    if _atomic_write_json is not None:
        _atomic_write_json(_QUIET_WAKE_STATE_FILE, data, encoding="utf-8")
        return
    import tempfile  # fallback when _hooklib cannot import: same-dir temp + rename
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_QUIET_WAKE_STATE_FILE) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _QUIET_WAKE_STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _quiet_wake_step(
    transcript_path: str, can_nudge: bool
) -> tuple[str, bool] | None:
    """Classify this turn, update + persist the counter.

    Returns (reason, is_block) when a nudge is due AND can_nudge; else None.
    is_block=True → emit decision:block; False → emit systemMessage only.

    Waiting-on zones (threshold N = _WAITING_ON_NEAR_ZONE_MINUTES):
    - No active waiting_on → demand (block=True).
    - return_by within N → advisory systemMessage (block=False).
    - return_by beyond N, once-only not yet fired → study-unit block (block=True),
      records the fired flag alongside quiet_ticks.
    - return_by beyond N, flag set → advisory systemMessage (block=False).
    Counter increments and resets identically in all cases. Additive: never raises.
    """
    try:
        delta = _read_turn_delta(transcript_path)
        if delta is None:
            return None  # unreadable: leave the counter alone
        ticks = _read_quiet_ticks(transcript_path) + 1 if _delta_is_quiet_wake(delta) else 0
        due = ticks >= QUIET_TICKS_THRESHOLD and can_nudge
        block_rb = _read_waiting_on_block_rb(transcript_path)
        if not due:
            _write_quiet_ticks(transcript_path, ticks, block_rb)
            return None
        wo = _read_waiting_on(LOOP_MARKER_PATH)
        if wo is not None:
            desc, rb_str = wo
            rb = datetime.fromisoformat(rb_str.replace("Z", "+00:00"))
            if rb.tzinfo is None:
                rb = rb.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if rb - now <= timedelta(minutes=_WAITING_ON_NEAR_ZONE_MINUTES):
                # Within N: quiet, advisory as systemMessage only
                _write_quiet_ticks(transcript_path, 0, block_rb)
                return _QUIET_ADVISORY_TEXT.format(n=ticks, desc=desc, rb_str=rb_str), False
            # Beyond N: check once-only flag
            if block_rb == rb_str:
                # Already fired: advisory as systemMessage, preserve flag
                _write_quiet_ticks(transcript_path, 0, rb_str)
                return _QUIET_ADVISORY_TEXT.format(n=ticks, desc=desc, rb_str=rb_str), False
            # First time beyond N: study-unit block, set flag
            _write_quiet_ticks(transcript_path, 0, rb_str)
            return _QUIET_STUDY_UNIT_TEXT.format(desc=desc, rb_str=rb_str), True
        # No active waiting_on: normal demand, clear any stale flag
        _write_quiet_ticks(transcript_path, 0)
        return _QUIET_NUDGE_TEXT.format(n=ticks), True
    except Exception:  # noqa: BLE001
        return None


def _emit_hook_fire(session_id: str, transcript_path: str, _t0: float) -> None:
    """Emit the engram.hook.fire event (shared by the nudge and reply-gate paths)."""
    _duration_ms = int((time.perf_counter() - _t0) * 1000)

    # Emit engram.hook.fire event, matching the sibling Stop hooks'
    # observability convention. Failure must not break the hook.
    #
    # alpha #2574 WS3: construction (import + Emitter.init) is narrowed to
    # its own try/except so an INIT_FAILED outcome is recordable and
    # distinct from an ordinary DROPPED emit (docs/internal/2574-telemetry-design.md
    # §1/§5).
    _emitter = None
    try:
        from engram_log_emitter import Emitter
        _emitter = Emitter.init(
            session_id=session_id,
            transcript_path=transcript_path,
        )
    except Exception:
        write_telemetry_record(
            manifest_id=_HOOK_FIRE_SITE,
            event_type="engram.hook.fire",
            fields={"outcome": "init_failed"},
        )

    if _emitter is not None:
        _uid = _emitter.emit(
            event_type="engram.hook.fire",
            level=1,
            data={
                "hook_name": "engram-loop-arm-stop-hook",
                "hook_type": "Stop",
                "duration_ms": _duration_ms,
                "exit_code": 0,
            },
            site="loop-arm-stop-hook.hook_fire",
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

    session_id = "unknown"
    transcript_path = ""
    payload_text = None
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
            # alpha #4193: usable only when a str with non-whitespace content.
            lam = payload.get("last_assistant_message")
            if isinstance(lam, str) and lam.strip():
                payload_text = lam
    except Exception:
        return

    # Never nag a non-loop session. No state-file touch on this path — the
    # gate is stateless, matching engram-stop-hook.py's maturity-gate
    # discipline (crossing back into loop mode later leaves this hook's
    # own bookkeeping undisturbed).
    if not os.path.exists(LOOP_MARKER_PATH):
        return

    # A stale scheduleWakeup marker is harmful on Codex: the normal nudge
    # would demand a Claude-only tool. Detect the runtime before the regular
    # pacer gate and provide only a concrete managed-bridge/systemd repair.
    pacer_is_schedule_wakeup = _pacer_is_schedule_wakeup(LOOP_MARKER_PATH)
    if _is_managed_codex_runtime():
        if not pacer_is_schedule_wakeup:
            return
        print(json.dumps({"decision": "block", "reason": _CODEX_PACER_NUDGE_TEXT}))
        if _STOPHOOKLIB_OK:
            _write_offset_state(_LOOP_ARM_STATE_FILE, transcript_path)
        return

    # Round-6 fix: never nag a loop this hook can't positively identify as
    # scheduleWakeup-paced -- see _pacer_is_schedule_wakeup's docstring and
    # the module docstring's round-6 note for why silence, not emission,
    # is correct here. Also stateless, same reasoning as the marker-absent
    # check above.
    if not pacer_is_schedule_wakeup:
        return

    confirmed = False
    try:
        confirmed = _delta_has_arm_confirmation(transcript_path)
    except Exception:  # noqa: BLE001
        confirmed = False  # fail-open: nudge fires

    # alpha #4104: reply-delivery gate. Decided from the transcript delta only.
    # A human prompt whose turn does not END on assistant text means the reply
    # was swallowed (e.g. a trailing arm): block, ahead of the arm check.
    has_human = reply_rendered = False
    _delta = None
    try:
        _delta = _read_turn_delta(transcript_path)
        if _delta is not None:
            has_human, reply_rendered = _delta_reply_state(_delta)
    except Exception:  # noqa: BLE001
        has_human = reply_rendered = False

    # alpha #4193: the transcript can lag the final record (flush lag). A payload
    # `last_assistant_message` that is NOT in the delta means the text is not flushed
    # yet: treat the reply as rendered. Found in the delta (swallowed) keeps the block.
    if has_human and not reply_rendered:
        _facts = None
        _norm = _norm_ws(payload_text) if payload_text is not None else None
        # _facts None is believed unreachable while has_human is true (_delta_reply_state parses
        # the same bytes); kept as fail-keep-the-block.
        try:
            _facts = _payload_gate_facts(_delta, _norm) if _delta is not None else None
        except Exception:  # noqa: BLE001
            _facts = None
        _override = _norm is not None and _facts is not None and not _facts[0]
        _log_reply_gate(session_id, None if payload_text is None else len(payload_text), _facts,
                        "override-rendered" if _override else "block")
        if _override:
            reply_rendered = True

    if has_human and not reply_rendered:
        _quiet_wake_step(transcript_path, can_nudge=False)  # keep the counter honest
        print(json.dumps({"decision": "block", "reason": _REPLY_NOT_RENDERED_TEXT}))
        if _STOPHOOKLIB_OK:
            _write_offset_state(_LOOP_ARM_STATE_FILE, transcript_path)
        _emit_hook_fire(session_id, transcript_path, _t0)
        return

    # A human-answering turn whose reply rendered, with a wake still pending:
    # the heartbeat is already covered, so no forced arm.
    if has_human and not confirmed:
        pending = False
        try:
            pending = _wake_pending(transcript_path, _now_utc())
        except Exception:  # noqa: BLE001
            pending = False
        if pending:
            _quiet_wake_step(transcript_path, can_nudge=False)
            if _STOPHOOKLIB_OK:
                _write_offset_state(_LOOP_ARM_STATE_FILE, transcript_path)
            return

    # alpha #3919 M2: runs BEFORE the confirmed-early-return (a quiet turn
    # normally DOES contain the re-arm) and BEFORE the offset write (it reads
    # the same delta). It never emits when the arm-check nudge is firing.
    quiet_result = _quiet_wake_step(transcript_path, can_nudge=confirmed)

    if confirmed:
        if _STOPHOOKLIB_OK:
            _write_offset_state(_LOOP_ARM_STATE_FILE, transcript_path)
        if quiet_result is not None:
            reason, is_block = quiet_result
            if is_block:
                print(json.dumps({"decision": "block", "reason": reason}))
            else:
                print(json.dumps({"systemMessage": reason}))
        return

    # Single, lib-independent emission path (2026-08-12 blocking-decision
    # correction) -- matches engram-stop-hook.py's own write-nudge exactly.
    # A blocking decision is what actually interrupts a stopping turn (see
    # design fact 4); additionalContext alone is not even a documented
    # Stop-hook field. No fallback/primary split to keep in sync: this
    # print statement is the only emit channel, lib available or not.
    print(json.dumps({"decision": "block", "reason": _nudge_text_for_marker(LOOP_MARKER_PATH)}))
    if _STOPHOOKLIB_OK:
        _write_offset_state(_LOOP_ARM_STATE_FILE, transcript_path)
    # Lib unavailable -- no offset state to write without it, so skip that
    # bookkeeping (nothing else to do with it); the nudge above still fired.

    _emit_hook_fire(session_id, transcript_path, _t0)

if __name__ == "__main__":
    main()
