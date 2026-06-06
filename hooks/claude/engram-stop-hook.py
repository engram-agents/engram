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

# Substring present in every tool_use JSONL entry. A post BODY mentioning this
# string would be inside an assistant text block (a different JSONL key), so
# spurious matches in the safe direction: a false positive EMITS the nudge,
# which is correct (over-nudging beats under-nudging). False negatives for
# suppression are fine — we never suppress when uncertain.
_TOOL_USE_MARKER = b'"type":"tool_use"'

# Default threshold for the maturity gate: suppress nudge when the graph has
# at least this many current nodes. Overridable via config.json.
_DEFAULT_NODE_THRESHOLD = 300


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

    # Maturity-gate check (issue #845): cheaper than the delta-scan and checked
    # first. A mature graph means the write-discipline fires from identity, not
    # from this tripwire — suppress silently. Do NOT touch the state file on
    # this path; the gate is stateless so crossing back under threshold (e.g.
    # restore from backup) leaves the idle machinery undisturbed.
    if _maturity_muted():
        # Empty stdout is the canonical no-op per #824/#832 contract.
        return

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
