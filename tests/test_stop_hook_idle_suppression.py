"""Tests for write-nudge idle suppression in hooks/claude/engram-stop-hook.py.

Issue #840: after #824 wrapped the write-nudge in the strict-JSON envelope
(delivering it to the model for the first time), the hook had no idle
suppression. On contentless turns (agent replies "nothing to record"), Stop
fires again immediately, creating a self-sustaining nudge→no-op→nudge loop
with no user input between turns (5 consecutive fires observed 2026-06-05).

Fix: delta-scan suppression. Before emitting, check whether any tool_use
events appeared in the transcript delta since the last fire. No tool activity
→ suppress (silent stdout). Fail-open on any uncertainty: missing state file,
corrupt JSON, I/O error, path change → always emit.

Invocation pattern mirrors test_codex_hook_envelopes.py (subprocess, real hook
script, synthetic tmp ENGRAM_HOME + transcript). All tests are hermetic: they
never touch ~/.engram or any production path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parent.parent / "hooks" / "claude"
HOOK_PATH = HOOKS_DIR / "engram-stop-hook.py"
PYTHON = sys.executable


def _make_stop_payload(transcript_path: str, session_id: str = "test-idle-suppress") -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": transcript_path,
        "last_assistant_message": "Nothing to record.",
        "stop_hook_active": False,
    })


def _run_hook(
    stdin_payload: str,
    engram_home: Path,
) -> tuple[str, int]:
    """Run the stop hook, return (stdout_stripped, returncode)."""
    env = dict(os.environ)
    env["ENGRAM_HOME"] = str(engram_home)
    result = subprocess.run(
        [PYTHON, str(HOOK_PATH)],
        input=stdin_payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return result.stdout.strip(), result.returncode


def _nudge_emitted(stdout: str) -> bool:
    """Return True if the hook emitted a write-nudge on this invocation."""
    if not stdout:
        return False
    try:
        parsed = json.loads(stdout)
        ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
        return "ENGRAM Write Check" in ctx
    except (json.JSONDecodeError, AttributeError):
        return False


def _append_tool_use(transcript: Path) -> None:
    """Append an assistant JSONL entry with a non-engram tool_use and long prose.

    Prose-gate update (issue #845 step 1.5): the new emit predicate requires
    BOTH (A) assistant prose > threshold AND (B) no engram write. This helper
    satisfies both conditions: prose > 400 chars (condition A) and tool name
    is "engram_query" — a read tool, not in the write set (condition B). The
    hook correctly emits on this delta.

    Uses separators=(',', ':') to produce compact JSON without spaces after
    colons, matching the real Claude Code transcript format — verified against
    live ~/.claude/projects/*.jsonl.
    """
    # Prose length must exceed the default threshold (400 chars). 401 chars of
    # 'x' is the minimal value that satisfies condition A.
    long_prose = "x" * 401
    entry = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": long_prose},
                    {"type": "tool_use", "id": "tu_test", "name": "engram_query", "input": {}},
                ]
            },
        },
        separators=(",", ":"),
    )
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def _append_text_only(transcript: Path) -> None:
    """Append a text-only assistant JSONL entry (no tool_use).

    Uses compact JSON to match real transcript format.
    """
    entry = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Nothing to record."}
                ]
            },
        },
        separators=(",", ":"),
    )
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


# ---------------------------------------------------------------------------
# Test 1: first fire (no state file) → nudge emitted, state file created
# ---------------------------------------------------------------------------

def test_first_fire_emits_nudge_and_creates_state_file(tmp_path):
    """First fire ever (no state file) must emit the nudge (fail-open) and
    create the state file with the transcript's current EOF offset."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    state_file = engram_home / ".write-nudge-last-fire.json"
    assert not state_file.exists(), "Precondition: state file must not exist"

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"First fire (no state file) must emit nudge; stdout={stdout!r}"
    )
    assert state_file.exists(), "State file must be created after first fire"
    state = json.loads(state_file.read_text())
    assert state.get("transcript_path") == str(transcript)
    assert isinstance(state.get("byte_offset"), int)


# ---------------------------------------------------------------------------
# Test 2: second fire, delta contains long prose (prose-gate driven emit)
# ---------------------------------------------------------------------------

def test_second_fire_with_long_prose_delta_emits_nudge(tmp_path):
    """After a long-prose turn between fires, the nudge must emit.

    This tests the prose-gate predicate (#845 step 1.5): prose > threshold
    AND no engram write → EMIT. The _append_tool_use helper produces a delta
    with long prose (>400 chars) and a non-engram tool, satisfying both
    conditions A and B of the gate.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # Fire 1: establishes state (empty transcript)
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Simulate long-prose turn: append a record with >400 chars prose and a
    # non-engram tool (both conditions of the prose gate are satisfied).
    _append_tool_use(transcript)

    # Fire 2: delta has long prose + no engram write → prose gate emits
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"Long-prose delta (prose gate) must emit nudge; stdout={stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: second fire, delta has text only → suppressed
# ---------------------------------------------------------------------------

def test_second_fire_text_only_delta_suppressed(tmp_path):
    """After a text-only turn (no tool_use), the nudge must be suppressed
    and stdout must be empty."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # Fire 1: establishes state (empty transcript)
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Simulate text-only assistant turn
    _append_text_only(transcript)

    # Fire 2: delta has no tool_use → suppress
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"Text-only delta must produce empty stdout (suppressed); got={stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: third fire after suppression, another toolless delta → still suppressed
# (regression test: the loop must actually break)
# ---------------------------------------------------------------------------

def test_loop_broken_consecutive_toolless_turns_suppressed(tmp_path):
    """THE regression test for #840: two consecutive toolless turns after the
    first fire must both be suppressed — the loop is broken, not infinite."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # Fire 1: establishes state
    stdout1, _ = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert _nudge_emitted(stdout1), "Fire 1 (no state file) must emit"

    # Toolless turn 1
    _append_text_only(transcript)

    # Fire 2: first suppression
    stdout2, rc2 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc2 == 0
    assert stdout2 == "", f"Fire 2 (toolless) must be suppressed; got={stdout2!r}"

    # Toolless turn 2 (loop scenario: agent again says "nothing to record")
    _append_text_only(transcript)

    # Fire 3: must also be suppressed — offset advanced by fire 2
    stdout3, rc3 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc3 == 0
    assert stdout3 == "", (
        f"Fire 3 (second consecutive toolless) must still be suppressed (loop broken); "
        f"got={stdout3!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: transcript_path changed between fires → nudge emitted (new session)
# ---------------------------------------------------------------------------

def test_path_change_between_fires_emits_nudge(tmp_path):
    """When transcript_path changes (new session), treat as unknown → emit."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript_a = tmp_path / "transcript_a.jsonl"
    transcript_a.write_text("", encoding="utf-8")
    transcript_b = tmp_path / "transcript_b.jsonl"
    transcript_b.write_text("", encoding="utf-8")

    # Fire 1 on transcript A
    _run_hook(_make_stop_payload(str(transcript_a)), engram_home)

    # Grow transcript A so it's not at the same offset
    _append_text_only(transcript_a)

    # Fire 2 on transcript B (different session)
    stdout, rc = _run_hook(_make_stop_payload(str(transcript_b)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"New transcript_path must emit nudge (fail-open); stdout={stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: stored offset > file size → nudge emitted (rotation / compaction)
# ---------------------------------------------------------------------------

def test_stored_offset_beyond_file_size_emits_nudge(tmp_path):
    """If the stored offset is larger than the current file size (rotation or
    compaction truncated the transcript), treat as unknown → emit."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"

    # Write some content, fire once to establish state with a real offset.
    _append_text_only(transcript)
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Simulate rotation: truncate the transcript to 0 bytes.
    transcript.write_text("", encoding="utf-8")

    # State file now has offset > 0 but file is 0 bytes.
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"Offset > file size must emit nudge (fail-open); stdout={stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: state file is corrupt JSON → nudge emitted (fail-open)
# ---------------------------------------------------------------------------

def test_corrupt_state_file_emits_nudge(tmp_path):
    """A corrupt state file must not suppress — fail-open and emit the nudge."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # Write a corrupt state file.
    state_file = engram_home / ".write-nudge-last-fire.json"
    state_file.write_text("{not valid json!!!", encoding="utf-8")

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"Corrupt state file must emit nudge (fail-open); stdout={stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test 8: envelope regression — #824 contract must be preserved on emission
# ---------------------------------------------------------------------------

def test_emitted_nudge_preserves_824_envelope(tmp_path):
    """When the nudge emits (tool_use delta), the output must be byte-compatible
    with the #824 envelope: JSON object with hookSpecificOutput.hookEventName=Stop
    and a non-empty additionalContext string.

    This is the regression guard so idle-suppression cannot break #824's fix.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # Establish state then add tool_use so fire 2 must emit.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)
    _append_tool_use(transcript)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout, "Emitting path must produce non-empty stdout"

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"Emitted stdout is not valid JSON: {exc}\nstdout={stdout!r}")

    assert "hookSpecificOutput" in parsed, (
        "Missing hookSpecificOutput — #824 envelope broken"
    )
    hso = parsed["hookSpecificOutput"]
    assert hso.get("hookEventName") == "Stop", (
        f"hookEventName={hso.get('hookEventName')!r}, expected 'Stop'"
    )
    ctx = hso.get("additionalContext")
    assert isinstance(ctx, str) and ctx, "additionalContext must be a non-empty string"
    assert "ENGRAM Write Check" in ctx, "Write-check text missing from additionalContext"
    assert "systemMessage" not in parsed, (
        "systemMessage must NOT be present (user-facing banner, not model-reaching)"
    )
    assert "additionalContext" not in parsed, (
        "Bare additionalContext at top level — still using old form Codex rejects"
    )


def test_zero_delta_between_fires_suppressed(tmp_path):
    """Stored offset == current size (NOTHING appended between fires) → suppress.

    Dedicated pin for the zero-delta branch: fire 1 establishes state on a
    transcript that then receives no further writes; fire 2 must produce empty
    stdout (the delta is empty, hence provably toolless). Reviewer round-1
    flagged this branch as untested.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    _append_text_only(transcript)

    # Fire 1: fresh state → emits (fail-open) and records EOF offset.
    stdout1, rc1 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc1 == 0
    assert stdout1, "First fire must emit (fail-open on fresh state)"

    # Fire 2: transcript untouched → zero-byte delta → suppress.
    stdout2, rc2 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc2 == 0
    assert stdout2 == "", (
        f"Zero-delta fire must be silent, got: {stdout2!r}"
    )
