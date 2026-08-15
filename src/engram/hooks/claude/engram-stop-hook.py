#!/usr/bin/env python3
"""Stop hook: remind agent to check if last response had recordable content.

Emits engram.hook.fire event for per-hook fire metadata (alpha #175, DESIGN.md §4.3).

Idle-suppression (issue #840)
-----------------------------
Problem: before #824, this hook emitted plain text on stdout, which Claude Code
discarded as invalid hook output — so it was silently mute. #824 wrapped the
nudge in the Stop-hook JSON envelope ({"decision":"block","reason":"..."}),
delivering it to the model for the first time. That exposed a second problem:
on contentless turns where the agent replies "nothing to record," the hook fires
again (Stop fires after every assistant turn), creating a self-sustaining
nudge→no-op→nudge loop with NO user input between turns (observed 5 consecutive
fires, 2026-06-05). Habituation risk: a tripwire firing on no-ops trains the
agent to dismiss it.

Fix — delta-scan suppression: before emitting the nudge, stat the transcript
and check whether the delta meets the emit predicate (prose-gate refinement
below). Update the stored offset on every fire (emit or suppress) so the next
fire only scans the new delta.

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

Maturity-gate suppression (issue #845)
---------------------------------------
The write-nudge is scaffolding for young graphs: it trains healthy write habits
before they are internalized. For an established agent with a mature graph the
nudge is noise — the discipline fires from identity, not from an external
tripwire. Empirical anchor: the hook was silently mute from creation until #824
(agents built healthy write habits without it); the nudge is a bootstrapping aid,
not a permanent fixture.

Fix — maturity gate: before the idle delta-scan, count current nodes in
knowledge.db. If the count meets or exceeds the threshold (default 300,
configurable via config.json cadence.write_nudge_node_threshold), suppress
silently without touching the idle-suppression state file (the gate is
stateless; crossing back under threshold — e.g. restore from backup — leaves
the idle machinery undisturbed).

Fail direction (opposite of #840): on any DB error — missing file, missing
table, query failure — return False (NOT muted → emit). Rationale: a missing or
unreadable DB most likely means a newborn install, exactly who the nudge is for.
The maturity gate fails toward nudging (young-agent assumption); the #840 idle
gate fails toward emitting (uncertainty assumption). Both fail directions resolve
to emission, by different reasoning.

Config: cadence.write_nudge_node_threshold in ~/.engram/config.json.
  - Absent or unreadable → use default (300).
  - Set to 0 or negative → gate disabled; nudge always emits regardless of
    graph size. This lets an agent deliberately opt back into nudges.

Prose-gate refinement (issue #845, step 1.5 — Lei's spec 2026-06-05)
----------------------------------------------------------------------
Problem: #844's idle suppression used a blunt predicate — emit iff the delta
contains ANY tool_use marker. This over-fires on tool-heavy turns (many tools
fired, terse prose — the agent was busy, not forgetting to write) and
under-fires on long discussion turns (no tools, extended prose — the exact
scenario where deferred-write incidents actually live).

Fix — prose-gate: replace the byte-scan `_TOOL_USE_MARKER not in delta` decision
with a two-condition predicate parsed from JSONL structure:

  EMIT iff:
    (A) assistant prose in the delta exceeds the prose threshold (default 400
        chars, configurable via cadence.write_nudge_prose_threshold), AND
    (B) the delta contains NO engram write-tool use.

Rationale for (A): a turn with substantial prose is where reasoning happened —
the nudge is most valuable here. Short prose turns (brief acknowledgements,
status updates) are unlikely to contain recordable content.

Rationale for (B): if the agent already wrote to ENGRAM this turn, suppress —
the discipline fired, no nudge needed. Non-engram tool use (Bash, Read, Write,
etc.) is irrelevant to the write decision; suppressing on any tool_use (the
#844 predicate) incorrectly rewarded tool-heavy coding turns.

Prose measurement: parse the delta as JSONL lines (json.loads per line, skip
unparseable lines silently); for each record with "type" == "assistant", sum
the lengths of content blocks with "type" == "text".

Record shape (verified against production transcripts 2026-06-05):
  {
    "type": "assistant",
    "message": {
      "content": [
        {"type": "text", "text": "..."},
        {"type": "tool_use", "id": "...", "name": "mcp__engram__engram_add_observation", ...}
      ]
    },
    ...
  }
Both install-form prefixes appear in production:
  mcp__engram__engram_*              (direct MCP install)
  mcp__plugin_engram_engram__engram_* (plugin-marketplace install)
Both are detected.

Flush-race note: Stop fires before the final text block of the current turn
flushes into the JSONL transcript. A long-prose turn may measure short on THIS
fire (flush not yet landed) and long on the NEXT fire (flush landed). The nudge
is one turn late in this edge case — a self-correcting one-turn delay, acceptable
given the improved precision.

Fail direction: any parse failure in the new logic → EMIT (fail-open, consistent
with #844/#840). A corrupted or partially-written delta never suppresses.

Config: cadence.write_nudge_prose_threshold in ~/.engram/config.json.
  - Absent or unreadable → use default (400).
  - Set to 0 or negative → prose gate disabled; treat all prose as exceeding
    (emit unless an engram write is present).

_stophooklib extraction (PR-A, 2026-07-31-loop-arm-nudge-SPEC.md)
-------------------------------------------------------------------
The byte-offset state read/write, the delta read, the per-line JSONL parse
(with its any_record_parsed sentinel), and the message.content block walk
below now delegate to hooks/claude/_stophooklib.py -- shared with
engram-deference-detector-stop.py. This is a PURE refactor: every early
return, every exception type caught, and every fail-open direction is
unchanged. The state-file PATH (_NUDGE_STATE_FILE) stays local to this hook,
passed into the shared functions rather than owned by them. This hook does
NOT use the lib's transcript-path session-marker fallback (resolve_
transcript_path) -- it has never had one (payload["transcript_path"] is used
directly, with no existence check), and this hook's behavior must stay
byte-identical, so that extraction is not wired in here. If _stophooklib
somehow fails to import (_STOPHOOKLIB_OK = False), this does NOT degrade to
a silent no-op -- unlike the UserPromptSubmit hooks that gate their whole
main() on their prologue-lib import, this hook only gates the suppression
CHECK on it. _should_suppress returns False (not suppressed) and
_update_state returns without writing, so the stored offset never advances.
The observable effect is the LOUDEST possible failure, and a self-
perpetuating one: the write-nudge fires on every single turn, indefinitely,
until the import is fixed. This is still fail-open in the direction this
file's own "Fail-open rule" (above) requires -- the suppression check is an
optimization, not a blocker, and uncertainty about its own dependencies
resolves to nudging, never to silent suppression -- it is simply NOT quiet
about it.

Near-compaction capture escalation (engram-alpha#2026, workstream 4)
---------------------------------------------------------------------
Problem: the compaction summary carries our custom instructions only ~12% of
the time (16 of 135 summaries since May, measured -- see the issue). Anything
not already plain text in a file is lost at the compaction boundary. The
Stop path had no emitter aware of this at all -- the "ride auto-compaction"
guidance lived only on engram-surface-hook.py (a UserPromptSubmit hook that
fires once per loop wake, not per turn during the burst it's meant to
govern) and on the dormant context_tracker_hook.py.

Fix -- this hook's existing prose-without-writes signal (the prose gate
above) ESCALATES when the session is near compaction. It does not become a
new unconditional per-Stop check: on a 1M-context session the drowsiness
reading has been measured sitting at 97-99% for roughly an hour (~3
heartbeat iterations) with no compaction firing at all (measured
first-hand). An unconditional block in that window would interrupt every
turn end for that whole hour. So:

  - the near-compaction window is detected via context_tracker.estimate_usage()
    + the same _load_drowsiness_thresholds() / urgent_at_or_above comparison
    format_drowsiness() uses -- not a re-implementation, not a hardcoded
    percentage;
  - inside the window, the prose gate's threshold is LOWERED (unwritten
    means lost, not merely deferred, so capture eagerly) and the nudge text
    changes to ask for the two things that actually survive: the burst's
    arc appended to today's history file, and the nodes for anything
    decided or found;
  - the escalation is rate-limited to at most one emission per
    _NEAR_COMPACTION_RATE_LIMIT_K eligible Stop fires, so the plateau above
    cannot turn into a per-turn block;
  - outside the window, behavior is byte-identical to the prose gate
    described above -- this is the regression that matters, and the reason
    the window check fails CLOSED (toward "no escalation") on any missing
    or corrupt drowsiness reading, unlike this file's other checks, which
    fail open toward emission. Escalating on bad data reproduces exactly the
    per-turn-block regression the rate limit exists to prevent; the base
    (unescalated) nudge machinery is untouched and still applies.

Maturity gate does NOT apply inside the window (round 2, 2026-08-15)
----------------------------------------------------------------------
The maturity gate (#845, above) is a PROXY: it suppresses on node count,
standing in for "this agent has internalized the write habit, stop nagging."
The near-compaction escalation fires on a directly OBSERVED signal instead --
prose written THIS burst with no accompanying ENGRAM writes, measured from
the actual transcript delta on this very fire. A proxy must not suppress a
stronger, more specific, directly-observed signal, so the maturity gate is
now consulted only for the BASE (unescalated) nudge path -- main() determines
the near-compaction window BEFORE the maturity check, and only returns early
on maturity when out of window.

This does not make the escalation noisier for a genuinely mature agent: if
they really are writing reliably, the prose-without-writes condition is
false and nothing fires, mature or not. The only agent this newly reaches is
one who is mature by node count AND has an unwritten burst right now -- and
near-compaction-unwritten means LOST, not deferred, so no amount of
accumulated write habit changes that deadline. Do not re-collapse this to a
single early-return gate; that reintroduces the bug this section documents.

Delivery is decision:block, and the reason for that needs to be precise,
because the coarse version is false. hookSpecificOutput.additionalContext
DOES reach the agent from a Stop hook -- verified first-hand: the field
arrives, but only on the NEXT turn, not the stopping one. This nudge is
about the boundary that is about to happen now, so decision:block is what
makes it land before that boundary rather than after it -- never write
"additionalContext never reaches the agent" here, that claim is false and
has already needed correcting once.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)

_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
from _hooklib import atomic_write_json
try:
    from _stophooklib import (
        read_offset_state as _read_offset_state,
        write_offset_state as _write_offset_state_impl,
        read_delta as _read_delta,
        parse_jsonl_records as _parse_jsonl_records,
        walk_content_blocks as _walk_content_blocks,
    )
    _STOPHOOKLIB_OK = True
except Exception:
    _STOPHOOKLIB_OK = False

# Near-compaction escalation (engram-alpha#2026 WS4): reuse context_tracker's
# own ceiling/threshold logic rather than re-implementing it. Threading
# transcript_path/session_id from THIS hook's stdin payload (below, in
# main()) matches engram-surface-hook.py:3247-3256 -- the per-session
# marker, not a global one (#140).
try:
    from context_tracker import (
        estimate_usage as _estimate_usage,
        _load_drowsiness_thresholds as _load_drowsiness_thresholds,
    )
    _CONTEXT_TRACKER_OK = True
except Exception:
    _CONTEXT_TRACKER_OK = False

# State file for idle suppression. Stores transcript_path + byte_offset from
# the previous fire so we can read only the delta on the next fire.
_NUDGE_STATE_FILE = os.path.join(ENGRAM_HOME, ".write-nudge-last-fire.json")

# State file for the near-compaction rate limiter (decision D). Stores
# transcript_path + a count of escalation-eligible fires since the last
# escalated emission. Separate from _NUDGE_STATE_FILE because it tracks a
# different thing (eligible-fire count, not byte offset) on a different
# cadence (reset on window entry/exit, not on every fire).
_NEAR_COMPACTION_RATE_STATE_FILE = os.path.join(
    ENGRAM_HOME, ".near-compaction-nudge-rate.json"
)

# Default threshold for the maturity gate: suppress nudge when the graph has
# at least this many current nodes. Overridable via config.json.
_DEFAULT_NODE_THRESHOLD = 300

# Default prose length threshold for the prose gate: emit only when the delta
# contains more than this many characters of assistant text. Overridable via
# cadence.write_nudge_prose_threshold in config.json.
# 0 or negative means "disabled" — all prose lengths treated as exceeding.
_DEFAULT_PROSE_THRESHOLD = 400

# Near-compaction escalation (engram-alpha#2026 WS4, decision C1): lower
# prose threshold used inside the near-compaction window, where unwritten
# work means LOST (not merely deferred), so the gate should catch eagerly.
# Overridable via cadence.write_nudge_prose_threshold_near_compaction in
# config.json. Same 0-or-negative-disables convention as the base threshold.
# The specific number is not spec-mandated -- chosen as a quarter of the
# base default so escalated capture triggers well below the point that
# would otherwise be judged "substantial enough to nudge."
_DEFAULT_PROSE_THRESHOLD_NEAR_COMPACTION = 100

# Near-compaction escalation (decision D): at most one escalated emission per
# this many escalation-ELIGIBLE Stop fires (fires where the lowered prose
# gate would otherwise emit). A module constant, not a magic number, per the
# spec. Without this, decision B's near-compaction plateau (measured ~97-99%
# for ~1hr / ~3 heartbeat iterations with no compaction firing, measured
# first-hand)
# turns into a blocking interrupt on every turn end for that hour.
_NEAR_COMPACTION_RATE_LIMIT_K = 3

# Verb suffixes that constitute an ENGRAM write. The engram tool name has the
# form: mcp__engram__engram_<verb> or mcp__plugin_engram_engram__engram_<verb>.
# Verified against production transcripts (2026-06-05).
_ENGRAM_WRITE_VERBS = frozenset({
    "add_observation",
    "add_observation_batch",
    "derive",
    "add_conjecture",
    "ask",
    "report_feeling",
    "add_lesson",
    "add_cornerstone",
    "add_axiom",
    "add_goal",
    "add_person",
    "add_definition",
    "add_edge",
    "remove_edge",
    "add_task",
    "update_task",
    "supersede",
    "retract",
    "contradict",
    "resolve",
    "register_exemplar",
    "link_about",
    "set_recall_summaries",
    "nap",
    "advance_turn",
    "outgrow_cornerstone",
    "add_trust_signal",
    "lesson_register_incident",
    "goal_tension",
    "focus",
    "unfocus",
    "focus_save",
    "focus_load",
    "focus_swap",
    "focus_delete_set",
    "set_trust_tier",
})

# Known engram tool name prefixes from production transcripts. Both forms are
# in use: mcp__engram__engram_* (direct install) and
# mcp__plugin_engram_engram__engram_* (plugin-marketplace install).
_ENGRAM_TOOL_PREFIXES = (
    "mcp__engram__engram_",
    "mcp__plugin_engram_engram__engram_",
)


def _read_node_threshold() -> int:
    """Read cadence.write_nudge_node_threshold from config.json.

    Returns the configured value, or _DEFAULT_NODE_THRESHOLD if absent or
    unreadable. A value of 0 or negative in config means the gate is disabled
    (caller interprets <= 0 as always-emit).
    """
    config_path = os.path.join(ENGRAM_HOME, "config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        cadence = config.get("cadence", {})
        val = cadence.get("write_nudge_node_threshold")
        if val is None:
            return _DEFAULT_NODE_THRESHOLD
        return int(val)
    except Exception:  # noqa: BLE001
        # Config missing, unreadable, or malformed — use the default.
        return _DEFAULT_NODE_THRESHOLD


def _read_prose_threshold() -> int:
    """Read cadence.write_nudge_prose_threshold from config.json.

    Returns the configured value, or _DEFAULT_PROSE_THRESHOLD if absent or
    unreadable. A value of 0 or negative means the prose gate is disabled
    (all prose lengths treated as exceeding the threshold).
    """
    config_path = os.path.join(ENGRAM_HOME, "config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        cadence = config.get("cadence", {})
        val = cadence.get("write_nudge_prose_threshold")
        if val is None:
            return _DEFAULT_PROSE_THRESHOLD
        return int(val)
    except Exception:  # noqa: BLE001
        return _DEFAULT_PROSE_THRESHOLD


def _read_prose_threshold_near_compaction() -> int:
    """Read cadence.write_nudge_prose_threshold_near_compaction from config.json.

    Returns the configured value, or _DEFAULT_PROSE_THRESHOLD_NEAR_COMPACTION
    if absent or unreadable. Same 0-or-negative-disables convention as
    _read_prose_threshold. Used only inside the near-compaction window
    (engram-alpha#2026 WS4 decision C1) — outside it, _read_prose_threshold
    is used unchanged.
    """
    config_path = os.path.join(ENGRAM_HOME, "config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        cadence = config.get("cadence", {})
        val = cadence.get("write_nudge_prose_threshold_near_compaction")
        if val is None:
            return _DEFAULT_PROSE_THRESHOLD_NEAR_COMPACTION
        return int(val)
    except Exception:  # noqa: BLE001
        return _DEFAULT_PROSE_THRESHOLD_NEAR_COMPACTION


def _maturity_muted() -> bool:
    """Return True iff the agent's graph is mature enough to suppress the nudge.

    Reads the current-node count from knowledge.db and compares against the
    configured threshold. The query counts only is_current=1 rows — retracted
    and superseded nodes (is_current=0) do not count toward maturity.

    Fail direction: on ANY failure — DB missing, table missing, query error —
    return False (NOT muted → emit). A missing or unreadable DB most likely
    indicates a newborn install, exactly who the nudge is for. The maturity gate
    fails toward nudging (young-agent assumption); the #840 idle gate fails
    toward emitting (uncertainty assumption). Both fail directions resolve to
    emission, by different reasoning.

    Gate-disabled path: if the configured threshold is <= 0, return False
    immediately (gate disabled; the caller always emits regardless of graph
    size). An agent can opt back into nudges by setting the threshold to 0 or
    negative in config.json.
    """
    try:
        threshold = _read_node_threshold()
        if threshold <= 0:
            # Gate explicitly disabled via config — never mute.
            return False

        db_path = os.path.join(ENGRAM_HOME, "knowledge.db")
        # Open read-only via WAL-safe URI path. Under WAL mode a mode=ro
        # connection reads the pre-transaction snapshot and does not raise
        # SQLITE_BUSY during concurrent writes. Any exception that does occur
        # (missing file, missing table, I/O error) lands on the except branch
        # below and fails toward nudging.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT count(*) FROM nodes WHERE is_current = 1"
            ).fetchone()
            current_count = row[0]
        finally:
            conn.close()

        return current_count >= threshold

    except Exception:  # noqa: BLE001
        # Any failure — missing DB, missing table, query error — fails toward
        # nudging (young-agent assumption). Do NOT mute.
        return False


def _is_engram_write_tool(name: str) -> bool:
    """Return True if `name` is an ENGRAM write-tool call.

    A name is an engram write tool iff it starts with one of the known engram
    prefixes AND its verb suffix (the part after the prefix) is in the write
    set. Both install-form prefixes are checked.
    """
    for prefix in _ENGRAM_TOOL_PREFIXES:
        if name.startswith(prefix):
            verb = name[len(prefix):]
            return verb in _ENGRAM_WRITE_VERBS
    return False


_PARSE_FORCE_EMIT: tuple[int, bool] = (0, False)
"""Sentinel returned by _parse_delta_prose_and_writes when a non-empty delta
yields zero parseable records (all lines garbage / format drift). The caller
checks delta_bytes + any_record_parsed via the _PARSE_ALL_GARBAGE flag instead
— see below for why (0, False) alone cannot encode this case unambiguously).
"""

_PARSE_ALL_GARBAGE = object()
"""Sentinel object placed at index 0 of the return tuple when a non-empty
delta was provided but every line failed json.loads. The caller detects this
via `result[0] is _PARSE_ALL_GARBAGE` and unconditionally takes the EMIT path.

Using a distinct sentinel (rather than a magic integer) makes the contract
explicit and avoids aliasing with real prose_length=0 from a valid empty turn.
"""


def _parse_delta_prose_and_writes(
    delta_bytes: bytes,
) -> tuple[object, bool]:
    """Parse the transcript delta and return (prose_length_or_sentinel, has_engram_write).

    Normal return: (prose_length: int, has_engram_write: bool)
    - prose_length: total character length of all "type":"text" content blocks
      inside "type":"assistant" records in the delta.
    - has_engram_write: True if any tool_use content block in the delta has a
      name that is an ENGRAM write tool.

    Parsing strategy:
    - Decode delta as UTF-8 (errors='replace' so corrupt bytes don't abort).
    - Split into lines; json.loads each line; skip unparseable lines silently.
    - Track any_record_parsed: True once any line survives json.loads.
    - For records with top-level "type" == "assistant", walk the
      message.content array for blocks.
    - "type":"text" blocks contribute their text length to prose_length.
    - "type":"tool_use" blocks with an engram-write name set has_engram_write.

    Fail directions (all force emission):
    1. Any exception in the entire function → return (0, False). Caller
       checks `result[0] is not _PARSE_ALL_GARBAGE` and applies the normal
       two-condition gate; (0, False) with a positive threshold → condition A
       fails → EMIT.
    2. Non-empty delta with zero parseable records (all lines failed
       json.loads — format drift or pure garbage) → return
       (_PARSE_ALL_GARBAGE, False). Caller detects the sentinel and takes the
       EMIT path unconditionally, bypassing the threshold comparison. This
       prevents format drift from silently killing the nudge forever: an
       unrecognised delta emits rather than suppresses.

    Both fail paths are fail-open for emission: uncertainty about what
    happened resolves to nudging, never to silent suppression.

    Record shape (verified against production transcripts 2026-06-05):
    {
      "type": "assistant",
      "message": {
        "content": [
          {"type": "text", "text": "..."},
          {"type": "tool_use", "id": "...", "name": "mcp__engram__engram_add_observation", "input": {...}}
        ]
      },
      ...
    }
    """
    try:
        if not _STOPHOOKLIB_OK:
            # Lib unavailable — fail toward emission, same direction as every
            # other failure path in this function.
            raise RuntimeError("_stophooklib unavailable")

        records, any_record_parsed = _parse_jsonl_records(delta_bytes)
        prose_length = 0
        has_engram_write = False

        for rec in records:
            if rec.get("type") != "assistant":
                continue

            content = rec.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue

            for block in _walk_content_blocks(content):
                block_type = block.get("type")
                if block_type == "text":
                    text_val = block.get("text", "")
                    if isinstance(text_val, str):
                        prose_length += len(text_val)
                elif block_type == "tool_use":
                    name = block.get("name", "")
                    if isinstance(name, str) and _is_engram_write_tool(name):
                        has_engram_write = True

        # Non-empty delta with zero parseable records: format drift or fully
        # garbage content. Return the all-garbage sentinel so the caller takes
        # the unconditional EMIT path — the nudge must never be silently killed
        # by an unrecognised record shape.
        if delta_bytes and not any_record_parsed:
            return _PARSE_ALL_GARBAGE, False

        return prose_length, has_engram_write

    except Exception:  # noqa: BLE001
        # Any parse failure → fail-open: values that cause emission.
        return 0, False


def _should_suppress(
    transcript_path: str, prose_threshold_override: int | None = None
) -> bool:
    """Return True iff the nudge should be suppressed for this fire.

    Prose-gate predicate (issue #845 step 1.5):
      EMIT iff:
        (A) assistant prose in the delta exceeds the prose threshold, AND
        (B) the delta contains NO engram write-tool use.

    Equivalently, SUPPRESS if:
        prose <= threshold  (condition A fails), OR
        an engram write was present  (condition B fails).

    Behavior change from #844: short-prose toolless turns now SUPPRESS (they
    were EMIT under #844). Long-prose toolless turns now EMIT (they were
    SUPPRESS under #844, because no tool_use was present). This is the intended
    refinement — the discussion-turn case is the one that actually needs nudging.

    prose_threshold_override (engram-alpha#2026 WS4, decision C1): when given,
    used in place of _read_prose_threshold() for condition A. The
    near-compaction escalation path passes the lowered near-compaction
    threshold here so it reuses this function's delta-read/parse machinery
    unchanged rather than duplicating it. None (the default) preserves the
    out-of-window call exactly as before — byte-identical behavior.

    Fail-open: any exception returns False (emit the nudge).
    """
    try:
        if not transcript_path:
            return False  # no transcript → unknown → emit

        if not _STOPHOOKLIB_OK:
            # Lib unavailable — fail toward emission, same direction as
            # every other failure path in this function.
            return False

        # Read stored state.
        stored_path: str | None = None
        stored_offset: int = 0
        try:
            stored_path, stored_offset = _read_offset_state(_NUDGE_STATE_FILE)
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
            delta = _read_delta(transcript_path, stored_offset, current_size)
        except OSError:
            return False  # can't read → emit

        # Prose-gate predicate (replaces #844's _TOOL_USE_MARKER byte scan).
        #
        # Parse the delta for prose length and engram write presence, then
        # apply the two-condition gate:
        #   EMIT iff (A) prose > threshold AND (B) no engram write.
        #   SUPPRESS otherwise.
        #
        # Special case: if the delta is non-empty but all lines were
        # unparseable (format drift / pure garbage), _parse_delta_prose_and_writes
        # returns (_PARSE_ALL_GARBAGE, False). Force EMIT unconditionally so
        # format drift never silently kills the nudge forever.
        parse_result, has_engram_write = _parse_delta_prose_and_writes(delta)
        if parse_result is _PARSE_ALL_GARBAGE:
            return False  # force EMIT

        prose_len = parse_result  # int in the normal path
        prose_threshold = (
            prose_threshold_override
            if prose_threshold_override is not None
            else _read_prose_threshold()
        )

        # Condition A: prose exceeds threshold.
        # When threshold <= 0, the gate is disabled: all prose lengths are
        # treated as exceeding (prose_gate_a = True regardless of prose_len).
        prose_gate_a = (prose_threshold <= 0) or (prose_len > prose_threshold)

        # Condition B: no engram write present.
        no_write_b = not has_engram_write

        # Emit iff both conditions hold; suppress otherwise.
        should_emit = prose_gate_a and no_write_b
        return not should_emit

    except Exception:  # noqa: BLE001
        # Fail-open: any unexpected error → emit the nudge.
        return False


def _in_near_compaction_window(transcript_path: str, session_id: str) -> bool:
    """Return True iff this session's drowsiness reading is at/above urgent.

    engram-alpha#2026 WS4 decision B: this determines whether the EXISTING
    prose-without-writes signal (_should_suppress above) escalates — it does
    not create a new unconditional trigger of its own.

    Reuses context_tracker.estimate_usage() threaded with THIS hook's own
    stdin-payload transcript_path/session_id (matches
    engram-surface-hook.py:3247-3256, the per-session marker, #140 — no
    global-marker fallback here) and _load_drowsiness_thresholds()'s
    urgent_at_or_above comparison — the same ceiling/threshold logic
    format_drowsiness() uses, not a re-implementation or a hardcoded
    percentage.

    Fail CLOSED (returns False = out-of-window) on any missing/corrupt
    reading, missing usage data, or exception — including when
    context_tracker itself failed to import. This is the opposite fail
    direction from the rest of this file (which fails toward emission):
    escalating on a bad or unavailable reading would reproduce exactly the
    per-turn-block regression the rate limit (decision D) exists to
    prevent, while failing closed here only falls back to the base
    (unescalated) prose-gate path below, which is unaffected and still
    applies.
    """
    if not _CONTEXT_TRACKER_OK:
        return False
    try:
        usage = _estimate_usage(transcript_path=transcript_path, session_id=session_id)
        if not usage:
            return False
        mode = usage.get("mode", "1m")
        thresholds = _load_drowsiness_thresholds(mode)
        return usage["usage_pct"] >= thresholds["urgent_at_or_above"]
    except Exception:  # noqa: BLE001
        return False


def _rate_limit_escalation(transcript_path: str) -> bool:
    """Return True iff an escalation-eligible fire may emit NOW.

    engram-alpha#2026 WS4 decision D: at most one escalated emission per
    _NEAR_COMPACTION_RATE_LIMIT_K escalation-eligible fires (fires where the
    lowered near-compaction prose gate would otherwise emit). Only called
    when the caller has already determined this fire is eligible to emit —
    this function decides whether THIS particular eligible fire is the one
    that gets to.

    Tracks a per-transcript_path count of eligible fires since the last
    escalated emission, in _NEAR_COMPACTION_RATE_STATE_FILE. A missing state
    file or a transcript_path change (new/rotated session) resets to fully
    eligible — the count starts at _NEAR_COMPACTION_RATE_LIMIT_K itself, so
    the very first eligible fire after any reset always emits immediately
    (an agent freshly entering the danger zone should be warned right away,
    not after two more silent fires).

    Fail-open: any read/write/parse failure returns True (allow the emit).
    Losing the rate limit occasionally is far cheaper than silently
    swallowing the capture-before-loss warning — consistent with this
    file's fail-open posture everywhere else; only the WINDOW DETECTION
    itself (_in_near_compaction_window, above) fails in the opposite
    direction, and for a different reason (see its own docstring).
    """
    try:
        state: dict = {}
        if os.path.exists(_NEAR_COMPACTION_RATE_STATE_FILE):
            with open(_NEAR_COMPACTION_RATE_STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)

        stored_path = state.get("transcript_path")
        count = state.get("count", _NEAR_COMPACTION_RATE_LIMIT_K)
        if stored_path != transcript_path or not isinstance(count, int):
            count = _NEAR_COMPACTION_RATE_LIMIT_K

        if count >= _NEAR_COMPACTION_RATE_LIMIT_K:
            allowed = True
            # Reset to 1, not 0: this call itself is one eligible fire (the
            # one that just emitted), so the next K-1 eligible fires after it
            # are the ones that must be suppressed for the period to be
            # exactly K. Resetting to 0 undercounts by one and stretches the
            # observed period to K+1 (measured: 3 consecutive Falses after
            # every True, not 2).
            new_count = 1
        else:
            allowed = False
            new_count = count + 1

        atomic_write_json(
            _NEAR_COMPACTION_RATE_STATE_FILE,
            {"transcript_path": transcript_path, "count": new_count},
            encoding="utf-8",
        )

        return allowed
    except Exception:  # noqa: BLE001
        return True  # fail-open: allow the emit


def _reset_near_compaction_rate_state() -> None:
    """Clear the rate-limit state (called when out-of-window).

    So the NEXT window entry starts fully eligible rather than inheriting a
    stale count from a previous window earlier in the same session. Best-
    effort hygiene, not a gate — failure is silenced.
    """
    try:
        if os.path.exists(_NEAR_COMPACTION_RATE_STATE_FILE):
            os.remove(_NEAR_COMPACTION_RATE_STATE_FILE)
    except Exception:  # noqa: BLE001
        pass


_NEAR_COMPACTION_NUDGE_TEXT = (
    "[ENGRAM Write Check — near compaction: Compaction is near and the "
    "summary will not carry this. Before you stop: (1) append this burst's "
    "arc to today's ~/.engram/history/YYYY-MM-DD.md, (2) write the nodes for "
    "anything you decided or found, (3) nap once if you haven't this burst. "
    "Anything left only in context is lost at the boundary, not deferred.]"
)


def _update_state(transcript_path: str) -> None:
    """Write the current EOF offset to the state file. Failure is silenced."""
    if not _STOPHOOKLIB_OK:
        return
    _write_offset_state_impl(_NUDGE_STATE_FILE, transcript_path)


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

    # Near-compaction window check (engram-alpha#2026 WS4 decision B), moved
    # ahead of the maturity gate (round 2, 2026-08-15 -- see "Maturity gate
    # does NOT apply inside the window" in the module docstring). Determined
    # first so the maturity-gate decision below can tell whether it is
    # allowed to short-circuit. This only decides whether the prose-gate
    # check further down escalates — it is not itself a trigger. Wrapped so
    # any failure here falls back to the unescalated (out-of-window) path,
    # never to a crash.
    in_window = False
    try:
        in_window = _in_near_compaction_window(transcript_path, session_id)
    except Exception:  # noqa: BLE001
        in_window = False  # fail closed toward "no escalation"

    # Maturity-gate check (issue #845) applies ONLY to the base (unescalated)
    # nudge path (round 2, 2026-08-15). A mature graph means the base
    # write-discipline fires from identity, not from this tripwire — but the
    # near-compaction escalation below is a directly-observed signal, not the
    # maturity proxy, so it must not be muted by node count. _maturity_muted()
    # itself never raises (its own fail direction is baked in: any DB error
    # returns False = not muted), so this comparison cannot throw and there is
    # nothing further to guard here. Do NOT touch the #840 state file on the
    # early-return path; the gate is stateless so crossing back under
    # threshold (e.g. restore from backup) leaves the idle machinery
    # undisturbed.
    if not in_window and _maturity_muted():
        # Empty stdout is the canonical no-op per #824/#832 contract.
        return

    # Idle-suppression check (issue #840, prose-gate refinement #845 step 1.5).
    # Wrap entirely in try/except so any failure here cannot prevent the nudge
    # from emitting.
    suppress = False
    escalate = False
    try:
        if in_window:
            # Decision C1: lowered prose threshold inside the window — reuses
            # _should_suppress's own delta-read/parse machinery unchanged.
            suppress = _should_suppress(
                transcript_path,
                prose_threshold_override=_read_prose_threshold_near_compaction(),
            )
            if not suppress:
                # Decision D: this fire would escalate-emit; the rate limiter
                # decides whether THIS fire is the one that actually gets to.
                if _rate_limit_escalation(transcript_path):
                    escalate = True
                else:
                    suppress = True  # rate-limited this fire
        else:
            suppress = _should_suppress(transcript_path)
            # Out of the window: keep the rate-limit state from going stale,
            # so the next window entry starts fully eligible.
            _reset_near_compaction_rate_state()
    except Exception:  # noqa: BLE001
        suppress = False  # fail-open
        escalate = False

    if suppress:
        # Silent no-op: update state so next fire scans the correct delta.
        _update_state(transcript_path)
        # Empty stdout is the canonical no-op per #824/#832 contract.
        # The emitter call is skipped intentionally on the suppressed path —
        # no meaningful hook event to record.
        return

    if escalate:
        # Decision C2 text + decision E delivery: additionalContext/reason
        # DOES reach the agent from a Stop hook, but only on the NEXT turn
        # (verified first-hand) — decision:block is what lands this one
        # before the compaction boundary it warns about, not after it.
        output = _NEAR_COMPACTION_NUDGE_TEXT
    else:
        output = (
            "[ENGRAM Write Check: Did your last response contain a decision, insight, "
            "or design choice worth recording? If so, write to ENGRAM now "
            "(observation, derivation, question, or conjecture). "
            "If not, end the turn with NO output - do not reply to or acknowledge this check; "
            "a text-only acknowledgment wastes a turn.]"
        )
    print(json.dumps({"decision": "block", "reason": output}))

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
