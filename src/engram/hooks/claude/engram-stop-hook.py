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
"""
import json
import os
import sqlite3
import sys
import time

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)

# State file for idle suppression. Stores transcript_path + byte_offset from
# the previous fire so we can read only the delta on the next fire.
_NUDGE_STATE_FILE = os.path.join(ENGRAM_HOME, ".write-nudge-last-fire.json")

# Default threshold for the maturity gate: suppress nudge when the graph has
# at least this many current nodes. Overridable via config.json.
_DEFAULT_NODE_THRESHOLD = 300

# Default prose length threshold for the prose gate: emit only when the delta
# contains more than this many characters of assistant text. Overridable via
# cadence.write_nudge_prose_threshold in config.json.
# 0 or negative means "disabled" — all prose lengths treated as exceeding.
_DEFAULT_PROSE_THRESHOLD = 400

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
        text = delta_bytes.decode("utf-8", errors="replace")
        prose_length = 0
        has_engram_write = False
        any_record_parsed = False

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # Skip unparseable lines silently — fail-open.
                continue

            any_record_parsed = True

            if not isinstance(rec, dict):
                continue
            if rec.get("type") != "assistant":
                continue

            content = rec.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
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


def _should_suppress(transcript_path: str) -> bool:
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
        prose_threshold = _read_prose_threshold()

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

    # Maturity-gate check (issue #845): cheaper than the delta-scan and checked
    # first. A mature graph means the write-discipline fires from identity, not
    # from this tripwire — suppress silently. Do NOT touch the state file on
    # this path; the gate is stateless so crossing back under threshold (e.g.
    # restore from backup) leaves the idle machinery undisturbed.
    if _maturity_muted():
        # Empty stdout is the canonical no-op per #824/#832 contract.
        return

    # Idle-suppression check (issue #840, prose-gate refinement #845 step 1.5).
    # Wrap entirely in try/except so any failure here cannot prevent the nudge
    # from emitting.
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
        "[ENGRAM Write Check: Did your last response contain a decision, insight, "
        "or design choice worth recording? If so, write to ENGRAM now "
        "(observation, derivation, question, or conjecture). "
        "If not, end the turn with NO output - do not reply to or acknowledge this check; "
        "a text-only acknowledgment wastes a turn.]"
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
