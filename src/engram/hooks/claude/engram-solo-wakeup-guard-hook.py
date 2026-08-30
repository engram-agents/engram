#!/usr/bin/env python3
"""PreToolUse hook: deny a SOLO ScheduleWakeup call when the turn it belongs
to is answering a genuine human message — the swallowed-reply bug (#2427).

Origin: Lei, 2026-08-30 16:46Z, after the third swallowed reply in one
session: "this has been a very serious issue, we need to come up with a
solution, otherwise the product will feel like broken on normal users if
they don't know the reason and just see no answer from agents."

Mechanism (#2098, Claude Code 2.1.235) — CONTESTED, not settled, stated
honestly rather than as fact: on Kepler's seat, a solo ScheduleWakeup call
(the only tool call in its message) was measured ending the turn at the
tool's result, DISCARDING any assistant text written after it (n=2, plus a
prior-session instance) — the user sees no answer at all. Borges's own
seat, same version, directly contradicts this: 69/69 solo calls measured
continuing normally with hook-mediation excluded, and a further 52/52 in
this very session (0 batched, 52/52 followed by rendered text) — two
seats, identical version, opposite results, cause never established. This
hook does not resolve which seat is representative; it ships anyway
because the asymmetry favors it regardless of which side is right: a
spurious sibling call on a seat where solo never fails costs nothing, a
swallowed reply on a seat where it does is the exact harm Lei escalated
today. The documented workaround
(engram-loop skill Step 2 item 3) is: batch the arm with a sibling tool
call, and place any reply as the turn's FINAL text (before or after the
calls — only text with nothing rendered after it survives). That
discipline was violated twice in one session by the seat that wrote it,
under reply momentum — vigilance failed the live test; this hook is the
gate that replaces it (the same vigilance-to-mechanism migration pattern
already used for engram-loop-arm-stop-hook.py's own origin).

Deliberately UNCONDITIONAL once the two conditions hold, no third
"is there really more text coming" check attempted: a PreToolUse hook
fires BEFORE the tool executes and before any further model completion,
so it cannot know whether the agent intends to add text after this call
resolves — trying to guess would either under-protect (guess "no more
text", the exact case that bit today) or make the gate unfalsifiable
(guess "yes" always, which is just this hook without the guessing step).
The chosen rule is blunter but decidable: never let a human-triggered
turn's ONLY tool call be ScheduleWakeup, full stop. The escape valve is
not a bypass condition on this hook — it is the fix itself: add ANY
sibling tool call (a CronList verify is the natural one; engram-loop's
own recommended shape) and the call is no longer solo, so the very next
attempt allows through unconditionally. No stuck-deny state is possible
by construction: the fix always removes exactly the condition being
denied.

Two independent conditions gate the deny, both re-derived from evidence
already reconstructable in the transcript alone — no new session state,
no coupling to loop-mode.json (unlike engram-loop-arm-stop-hook.py, this
guard is NOT loop-scoped: the underlying harness bug fires on ANY
ScheduleWakeup call, loop or not, so gating on a loop marker would leave
a non-loop human-triggered solo arm unprotected for no reason):

1. SOLO — no sibling tool_use in the same parallel dispatch block.
   Measured on this seat/session only (this session's own transcript, 16
   real occurrences grepped), NOT cross-verified against another seat or
   Claude Code version: Claude Code's JSONL format did NOT put multiple
   tool_use blocks in one "assistant" record's content array the way the
   raw Anthropic API shape would suggest — a "batch" of N parallel tool
   calls instead landed as N CONSECUTIVE single-tool_use assistant
   records, chained by `parentUuid` (record k+1's parentUuid == record
   k's uuid), with NO intervening "user" tool_result record between them.
   Held to the same honesty standard as the #2098 solo-swallow mechanism
   itself, which this hook's own next paragraph inherits verbatim from
   engram-loop-arm-stop-hook.py's docstring: that bug was measured on one
   seat and explicitly NOT reproduced on another at the same version, so
   this JSONL-shape claim gets the identical caveat rather than a false
   confidence the sibling finding already warns against. Mitigating factor
   if this claim is ever wrong: `_has_sibling_tool_use` below only
   inspects OTHER records via `parentUuid`, never blocks living in the
   SAME record as the call under test — so a wrong assumption fails
   toward reading every batched call as solo (OVER-deny, loud-not-fatal),
   never toward missing a real sibling and under-protecting.
   So "solo" = the call's own record has no sibling tool_use record
   immediately BEFORE it (via parentUuid) and none chained immediately
   AFTER it (a record whose parentUuid points back at this one). Checked
   in both directions because either position in a 2-call batch must
   correctly read as "not solo."
   RESIDUAL, not resolved here (same shape as the sibling Stop hook's
   own "Known residual" section): whether ALL of a batch's chained
   tool_use records are guaranteed already flushed to the transcript
   file by the time the FIRST one's PreToolUse fires, or whether they
   land incrementally as each call is dispatched. This hook's own
   correctness for the FIRST call in a batch depends on the former.
   Evidence found (this session's transcript) is consistent with "the
   whole assistant turn is persisted before any of its tool_use blocks
   begin executing" — which is what makes the identification step below
   ("this call = the LAST tool_use record in the file") work at all —
   but this is inferred from a post-hoc read, not confirmed by live
   instrumented timing. If ever falsified, the fail direction this
   causes is UNDER-protection (missing a real sibling that hadn't been
   written yet, reading a batched call as falsely solo → an unwanted
   deny, loud-not-fatal), never a false ALLOW of a truly-solo call.
2. HUMAN-TRIGGERED — walk the parentUuid chain backward from this
   call's own record, through assistant records and pure-tool_result
   "user" records, to the nearest "user" record that is NOT solely a
   tool_result wrapper — that record is the prompt that started this
   whole turn. Classify its text with the SAME prefix discriminator
   engram-time-bar-hook.py's `_is_human_prompt` already uses for this
   exact purpose (mirrored here, not imported — a small literal tuple,
   same reasoning as that hook's own `_NON_HUMAN_PREFIXES` staying
   local rather than factored into a shared module), PLUS one entry
   that hook's own tuple lacks: a body that (after HTML-unescape, per
   #1649) starts with `<task-notification`, `<command-message`,
   `<command-name`, `<local-command`, `Stop hook feedback` (a colleague
   review found -- see `_NON_HUMAN_PREFIXES`'s own comment -- that a
   blocking Stop-hook re-invocation classified as human under the base
   tuple, denying the exact solo arm that Stop hook was demanding), or
   the loop-wake marker is NOT human. Anything else non-empty IS human.
   `promptSource` (the richer, payload-level discriminator that hook
   also uses) is not available here — PreToolUse's own stdin payload
   carries none of the ORIGINATING prompt's metadata, only this specific
   tool call's — so this hook is necessarily on the fallback path that
   hook's own docstring already documents as slightly weaker (text-
   prefix only). Accepted: the same fallback already ships in production
   for the time-bar banner.

Fail direction is ASYMMETRIC by design, matching this codebase's existing
discipline for exactly this shape of guard (engram-loop-arm-stop-hook.py's
own "Best-effort" section) rather than a single blanket fail-open/fail-
closed rule:
  - Any failure BEFORE establishing "this turn is human-triggered" (stdin
    unreadable, transcript unreadable, "this call"'s own record not
    found, origin-chain broken) fails toward ALLOW. Most turns in a
    fleet of largely-autonomous agents are loop-wake or task-notification
    turns, where a solo ScheduleWakeup is completely normal and expected
    (this very session armed one solo-with-verify-call every ~10 minutes
    for hours without incident) — guessing "human" on missing evidence
    would make the gate fire on the common case, not the rare one, and
    would actively break the idle heartbeat loop this fleet runs on.
  - Once human-triggered is established, failure to positively confirm a
    sibling (can't read the transcript for the solo check, malformed
    records, etc.) fails toward DENY (treat as solo). Reasoning matches
    engram-loop-arm-stop-hook.py's own asymmetry: the thing being
    protected against (a real human message losing its reply) is worse
    than the cost of an occasional unnecessary re-issue.

Emission: `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
"permissionDecision": "deny", "permissionDecisionReason": ...}}` — the
documented Claude Code PreToolUse contract (matches
engram-toolcall-repair.py's own emission shape for the same event, using
"deny" instead of that hook's "allow"). Never raises, never crashes the
session: every stage is wrapped so a hook bug degrades to "allow,"
consistent with "Behavioral hooks must never crash the session"
(engram-loop-arm-stop-hook.py's own closing line).
"""

import html
import json
import os
import sys
from pathlib import Path

_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
try:
    from _stophooklib import (
        parse_jsonl_records as _parse_jsonl_records,
        walk_content_blocks as _walk_content_blocks,
    )
    _STOPHOOKLIB_OK = True
except Exception:
    _STOPHOOKLIB_OK = False

# ---------------------------------------------------------------------------
# loop_prompt import — SSoT for the loop-wake marker. Same dual-topology
# resolution as engram-loop-arm-stop-hook.py / engram-time-bar-hook.py:
#   Source:  src/engram/hooks/claude/<name>.py -> src/engram/tools/
#   Plugin:  <root>/hooks/<name>.py             -> <root>/tools/
# FAIL-SAFE: an import failure falls back to the literal constant so the
# marker check below still works.
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

# The wake-arming tool's name, as it appears in a tool_use block's "name"
# field. This hook's own hooks.json matcher already scopes invocation to
# this tool; the check here is defense-in-depth, matching
# engram-loop-arm-stop-hook.py's own belt-and-suspenders identity check.
_WAKE_ARMING_TOOL_NAME = "ScheduleWakeup"

# Mirrors engram-time-bar-hook.py's `_NON_HUMAN_PREFIXES` (kept local
# rather than imported, matching that hook's own precedent of a small
# literal tuple duplicated rather than factored out — see this hook's
# module docstring, point 2), PLUS one entry that hook doesn't carry:
# "Stop hook feedback:" -- a colleague's review (Borges, PR#2433) found
# that a blocking Stop-hook re-invocation (e.g. engram-loop-arm-stop-
# hook.py's own re-fire, promptSource=None, no other recognized prefix)
# classified as HUMAN under the base tuple, which would have this guard
# deny the exact solo ScheduleWakeup arm that Stop hook is DEMANDING --
# two of this codebase's own hooks in direct opposition on one turn.
# Confirmed the exact leading text against a real occurrence in this
# session's own transcript ("Stop hook feedback:\n[Loop Arm Check: ...",
# promptSource: None). engram-time-bar-hook.py's own tuple lacks this
# entry too (a pre-existing gap this PR did not introduce, not fixed
# here -- filed as issue #2438, kept out of this PR's own scope).
#
# This is an INCOMPLETE ENUMERATION of a growing set, not a closed
# definition. Borges's colleague review found the "Stop hook feedback"
# gap above; in his own words, a further miss is "a matter of time."
# (This module's own claim, not verified against Borges: the base 4
# entries this tuple was seeded with were themselves already an
# enumeration, not a closed set, when engram-time-bar-hook.py first
# wrote them -- stated here as this file's own inference, since that
# earlier history is unaudited.) Any future non-human turn shape (a new
# blocking hook's own wrapper text, a new harness event type) needs its
# own prefix added here; there is no mechanism
# that derives this list automatically.
_NON_HUMAN_PREFIXES = (
    "<task-notification",
    "<command-message",
    "<command-name",
    "<local-command",
    "Stop hook feedback",
    _LOOP_WAKE_MARKER,
)

_DENY_REASON = (
    "[Solo-wakeup guard, #2427: this ScheduleWakeup call is answering a "
    "human message and is the ONLY tool call this turn. A solo "
    "ScheduleWakeup ends the turn at its own result and DISCARDS any "
    "assistant text written after it -- the human sees no reply at all. "
    "Re-issue this call BATCHED with a sibling tool call in the same "
    "parallel block (a CronList verify is the natural one) so the turn "
    "continues normally. If your reply text is already fully written "
    "BEFORE this call with nothing meant to follow it, batching is still "
    "the safe move -- nothing is lost either way, and this hook cannot "
    "tell the two cases apart from here. (engram-loop Step 2 item 3, "
    "#2098.)]"
)


def _read_text(path: str) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def _tool_use_blocks(record: dict) -> list:
    """Return every tool_use block in an assistant record's content, [] for
    anything else (non-assistant record, malformed content)."""
    if record.get("type") != "assistant":
        return []
    content = record.get("message", {}).get("content", [])
    return [
        b for b in _walk_content_blocks(content)
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]


def _find_this_call(records: list) -> dict | None:
    """Return the record representing THIS ScheduleWakeup call: the LAST
    ScheduleWakeup-named tool_use record in the whole transcript.

    Identification rests on "this call, having just been generated, is
    the most recent ScheduleWakeup call anywhere in the file" -- see the
    module docstring's residual note on flush timing. Deliberately SKIPS
    PAST other-named tool_use records while searching backward rather than
    giving up at the first one found: a sibling call that comes AFTER
    ScheduleWakeup in the same batch (e.g. [ScheduleWakeup, CronList]) may
    already be flushed to the transcript by the time this hook runs, and
    would otherwise be misread as "the most recent tool call isn't ours."
    Returns None (fail toward ALLOW at the caller) only if NO ScheduleWakeup
    tool_use record exists anywhere in the file.
    """
    for rec in reversed(records):
        blocks = _tool_use_blocks(rec)
        if any(b.get("name") == _WAKE_ARMING_TOOL_NAME for b in blocks):
            return rec
    return None


def _has_sibling_tool_use(record: dict, uuid_index: dict) -> bool:
    """True iff `record` is preceded OR followed by another tool_use
    record in the same parallel-dispatch chain (see module docstring
    point 1 for the chained-single-block transcript shape)."""
    parent_uuid = record.get("parentUuid")
    if parent_uuid:
        parent = uuid_index.get(parent_uuid)
        if parent is not None and _tool_use_blocks(parent):
            return True

    own_uuid = record.get("uuid")
    if own_uuid:
        for rec in uuid_index.values():
            if rec is record:
                continue
            if rec.get("parentUuid") == own_uuid and _tool_use_blocks(rec):
                return True
    return False


def _is_pure_tool_result(record: dict) -> bool:
    """True iff a "user" record's content is ENTIRELY tool_result block(s)
    -- i.e. it is not a genuine prompt, just the harness handing a tool's
    output back to the model mid-turn."""
    if record.get("type") != "user":
        return False
    content = record.get("message", {}).get("content", [])
    blocks = _walk_content_blocks(content)
    if not blocks:
        return False  # empty content is not "purely a tool_result" -- ambiguous, treat as a real record
    return all(b.get("type") == "tool_result" for b in blocks if isinstance(b, dict))


def _record_text(record: dict) -> str:
    """Best-effort plain text of a record's content -- the first text-type
    block, or "" if none."""
    content = record.get("message", {}).get("content", [])
    for block in _walk_content_blocks(content):
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                return text
    return ""


def _find_turn_origin(record: dict, uuid_index: dict) -> dict | None:
    """Walk parentUuid backward from `record` to the nearest "user" record
    that is NOT purely a tool_result wrapper -- the prompt that started
    this whole turn. Returns None if the chain breaks or loops before
    finding one (fail toward ALLOW at the caller)."""
    seen = set()
    current = record
    for _ in range(len(uuid_index) + 1):  # bounded: cannot exceed record count
        parent_uuid = current.get("parentUuid")
        if not parent_uuid or parent_uuid in seen:
            return None
        seen.add(parent_uuid)
        parent = uuid_index.get(parent_uuid)
        if parent is None:
            return None
        if parent.get("type") == "user" and not _is_pure_tool_result(parent):
            return parent
        current = parent
    return None


def _is_human_text(text: str) -> bool:
    """Mirrors engram-time-bar-hook.py's `_is_human_prompt` fallback path
    exactly (prefix-exclusion after HTML-unescape) -- see module docstring
    point 2 for why the richer promptSource-based check isn't available
    here."""
    stripped = html.unescape(text).lstrip()
    for prefix in _NON_HUMAN_PREFIXES:
        if stripped.startswith(prefix):
            return False
    return bool(stripped)


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw else {}
    except Exception:
        return  # unreadable stdin -> allow

    if not isinstance(payload, dict):
        return

    tool_name = payload.get("tool_name")
    if tool_name != _WAKE_ARMING_TOOL_NAME:
        return  # defensive; hooks.json matcher already scopes this

    if not _STOPHOOKLIB_OK:
        return  # can't parse the transcript at all -> allow (evidence absent)

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return

    try:
        text = _read_text(transcript_path)
    except OSError:
        return

    records, _any = _parse_jsonl_records(text.encode("utf-8"))
    if not records:
        return

    this_call = _find_this_call(records)
    if this_call is None:
        return  # can't identify our own call -> allow

    uuid_index = {
        rec["uuid"]: rec for rec in records
        if isinstance(rec.get("uuid"), str) and rec["uuid"]
    }

    origin = _find_turn_origin(this_call, uuid_index)
    if origin is None:
        return  # can't establish human-triggered -> allow (rare-case default)

    if not _is_human_text(_record_text(origin)):
        return  # loop-wake / task-notification / empty -> allow

    # Human-triggered established. From here, failure to POSITIVELY confirm
    # a sibling fails toward deny (treat as solo) -- see module docstring's
    # asymmetric fail-direction section.
    is_solo = not _has_sibling_tool_use(this_call, uuid_index)
    if not is_solo:
        return  # batched -> allow

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _DENY_REASON,
        }
    }))


if __name__ == "__main__":
    main()
