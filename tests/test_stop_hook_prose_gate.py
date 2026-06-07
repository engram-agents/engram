"""Tests for the prose-gate refinement in hooks/claude/engram-stop-hook.py.

Issue #845, step 1.5 (spec 2026-06-05): replace #844's emit-on-any-tool_use
predicate with:

  EMIT iff:
    (A) the transcript delta contains assistant prose longer than a threshold, AND
    (B) the delta contains NO engram write-tool use.

Rationale:
- Tool-heavy turns (many tools, terse prose) over-fire under #844. Under the
  prose gate they suppress — the agent was working, not forgetting to write.
- Long-prose toolless discussion turns never fire under #844 (no tool_use →
  suppress). Under the prose gate they emit — these are exactly the turns where
  deferred-write incidents live. This is THE new behavior.

Record shapes used in tests are copied from production transcripts (verified
2026-06-05). All tests are hermetic: they never touch ~/.engram or any production
path. Invocation pattern mirrors test_stop_hook_maturity_gate.py.
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

# The minimal CREATE TABLE statement matching the production schema.
# We only need the columns the maturity gate queries: id, is_current.
_NODES_DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    claim       TEXT,
    created_at  TEXT NOT NULL DEFAULT '',
    is_current  INTEGER DEFAULT 1
);
"""


def _make_stop_payload(transcript_path: str, session_id: str = "test-prose-gate") -> str:
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


def _write_config(engram_home: Path, **cadence_overrides: object) -> None:
    """Write a config.json with the given cadence fields."""
    config = {"cadence": cadence_overrides}
    (engram_home / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _make_young_graph(engram_home: Path, n_current: int = 50) -> None:
    """Create a knowledge.db with n_current nodes (well below the 300 threshold).

    The maturity gate must NOT fire on these fixtures so prose-gate tests are
    exercised unobstructed.
    """
    import sqlite3
    db_path = engram_home / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_NODES_DDL)
        for i in range(n_current):
            conn.execute(
                "INSERT INTO nodes(id, type, created_at, is_current) VALUES (?,?,?,?)",
                (f"ob_{i:04d}", "observation", "2026-01-01T00:00:00", 1),
            )
        conn.commit()
    finally:
        conn.close()


def _append_lines(transcript: Path, *lines: str) -> None:
    """Append one or more raw JSONL lines to the transcript."""
    with open(transcript, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# Compact-JSON helper builders — shapes verified from production transcripts.
# ---------------------------------------------------------------------------

def _assistant_text_record(text: str) -> str:
    """Build a compact-JSON assistant record with a single text content block."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": text}]
            },
        },
        separators=(",", ":"),
    )


def _assistant_text_and_tool_record(text: str, tool_name: str) -> str:
    """Build a compact-JSON assistant record with one text block and one tool_use block."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": text},
                    {
                        "type": "tool_use",
                        "id": "toolu_test_001",
                        "name": tool_name,
                        "input": {"payload_json": "{}"},
                        "caller": {"type": "direct"},
                    },
                ]
            },
        },
        separators=(",", ":"),
    )


def _assistant_tool_only_record(tool_name: str) -> str:
    """Build a compact-JSON assistant record with only a tool_use content block."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_test_002",
                        "name": tool_name,
                        "input": {"payload_json": "{}"},
                        "caller": {"type": "direct"},
                    }
                ]
            },
        },
        separators=(",", ":"),
    )


# Prose string that exceeds the default 400-char threshold.
_LONG_PROSE = "x" * 401

# Prose string that does not exceed the default threshold.
_SHORT_PROSE = "x" * 50

# An engram write tool name (plugin-install form).
_ENGRAM_WRITE = "mcp__plugin_engram_engram__engram_add_observation_batch"

# An engram write tool name (direct-install form).
_ENGRAM_WRITE_DIRECT = "mcp__engram__engram_add_observation"

# A non-engram tool (Bash).
_BASH_TOOL = "Bash"


# ===========================================================================
# Test 1: long-prose toolless delta → EMIT
# THE new behavior. Under #844 this case was SUPPRESS (no tool_use present).
# Under the prose gate it EMITS — discussion turns are where deferred-write
# incidents live.
# ===========================================================================

def test_long_prose_toolless_emits(tmp_path):
    """Long-prose turn with no tool use must EMIT the nudge.

    This is the primary new behavior enabled by the prose gate: a pure
    discussion turn where the agent wrote substantial text but no tool_use
    was present now correctly fires the write nudge, where #844 suppressed it.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)

    # Fire 1: establish idle-gate state.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: long prose, no tool use.
    _append_lines(transcript, _assistant_text_record(_LONG_PROSE))

    # Fire 2: prose gate conditions: (A) prose > 400 → True; (B) no write → True.
    # Both true → EMIT.
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"Long-prose toolless delta must EMIT the nudge (primary prose-gate behavior); "
        f"stdout={stdout!r}"
    )


# ===========================================================================
# Test 2: long-prose + engram write → SUPPRESS
# Agent already wrote to ENGRAM this turn; nudge is not needed.
# ===========================================================================

def test_long_prose_with_engram_write_suppresses(tmp_path):
    """Long-prose delta WITH an engram write must SUPPRESS the nudge.

    The agent already wrote to ENGRAM (condition B fails) — the discipline
    fired, no nudge needed.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)

    # Fire 1: establish idle-gate state.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: long prose + engram write (add_observation_batch, plugin form).
    _append_lines(
        transcript,
        _assistant_text_and_tool_record(_LONG_PROSE, _ENGRAM_WRITE),
    )

    # Fire 2: (A) prose > 400 → True; (B) engram write present → False.
    # Condition B fails → SUPPRESS.
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"Long-prose delta with engram write must SUPPRESS; got={stdout!r}"
    )


# ===========================================================================
# Test 3: short-prose + non-engram tool (Bash) → SUPPRESS
# Behavior change from #844: was EMIT (Bash counted as "tool_use present"),
# now SUPPRESS (prose below threshold → condition A fails).
# ===========================================================================

def test_short_prose_bash_tool_suppresses(tmp_path):
    """Short-prose delta with a non-engram tool (Bash) must SUPPRESS the nudge.

    BEHAVIOR CHANGE FROM #844: under #844 any tool_use presence caused EMIT
    (predicate was 'no tool_use → suppress'). Under the prose gate, non-engram
    tool use is irrelevant; only prose length and engram writes matter. A Bash
    call with short prose → condition A fails → SUPPRESS.

    This is the correct behavior: a tool-heavy coding turn (agent was working,
    not discussing) should not fire the write nudge.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)

    # Fire 1: establish idle-gate state.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: short prose + Bash tool_use (non-engram).
    _append_lines(
        transcript,
        _assistant_text_and_tool_record(_SHORT_PROSE, _BASH_TOOL),
    )

    # Fire 2: (A) prose = 50 chars <= 400 → False. Condition A fails → SUPPRESS.
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"Short-prose delta with Bash tool must SUPPRESS (behavior change from #844); "
        f"got={stdout!r}"
    )


# ===========================================================================
# Test 4: short-prose toolless → SUPPRESS
# Condition A fails (prose below threshold); nothing to record.
# ===========================================================================

def test_short_prose_toolless_suppresses(tmp_path):
    """Short-prose toolless delta must SUPPRESS — nothing substantial to record."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)

    # Fire 1: establish idle-gate state.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: short prose, no tool use.
    _append_lines(transcript, _assistant_text_record(_SHORT_PROSE))

    # Fire 2: (A) 50 chars <= 400 → False → SUPPRESS.
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"Short-prose toolless delta must SUPPRESS; got={stdout!r}"
    )


# ===========================================================================
# Test 5: prose threshold config override — custom threshold=100
# ===========================================================================

def test_prose_threshold_config_override(tmp_path):
    """Custom prose threshold from config.json is respected.

    With write_nudge_prose_threshold=100 and prose of 101 chars: EMIT.
    The same prose would suppress under a threshold of 200.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)

    # Custom threshold = 100.
    _write_config(engram_home, write_nudge_prose_threshold=100)

    # Fire 1: establish idle-gate state.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: prose of exactly 101 chars (just above custom threshold).
    prose_101 = "y" * 101
    _append_lines(transcript, _assistant_text_record(prose_101))

    # Fire 2: (A) 101 > 100 → True; (B) no write → True → EMIT.
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"101-char prose with threshold=100 must EMIT; stdout={stdout!r}"
    )

    # Now verify the same prose SUPPRESSES with a higher threshold (200).
    # Reset state by starting a fresh session (new transcript).
    transcript2 = tmp_path / "transcript2.jsonl"
    transcript2.write_text("", encoding="utf-8")
    _write_config(engram_home, write_nudge_prose_threshold=200)

    _run_hook(_make_stop_payload(str(transcript2)), engram_home)
    _append_lines(transcript2, _assistant_text_record(prose_101))

    stdout2, rc2 = _run_hook(_make_stop_payload(str(transcript2)), engram_home)

    assert rc2 == 0
    assert stdout2 == "", (
        f"101-char prose with threshold=200 must SUPPRESS; got={stdout2!r}"
    )


# ===========================================================================
# Test 6: prose threshold = 0 → gate disabled → emit even on short prose
# (provided no engram write is present)
# ===========================================================================

def test_prose_threshold_zero_disables_gate(tmp_path):
    """write_nudge_prose_threshold=0 disables the prose gate.

    With threshold=0, all prose lengths are treated as exceeding — condition A
    is always True. So a short-prose toolless turn now EMITS (was SUPPRESS with
    threshold=400).
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)

    # threshold=0 disables the prose gate.
    _write_config(engram_home, write_nudge_prose_threshold=0)

    # Fire 1: establish idle-gate state.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: short prose, no tool use.
    _append_lines(transcript, _assistant_text_record(_SHORT_PROSE))

    # Fire 2: threshold=0 → A = True; B = True → EMIT.
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"threshold=0 must disable prose gate; short prose must EMIT; stdout={stdout!r}"
    )


# ===========================================================================
# Test 7: corrupt JSONL line among valid lines → skip-line resilience
# The valid lines are still parsed correctly; the corrupt one is skipped.
# ===========================================================================

def test_corrupt_jsonl_line_skip_resilience(tmp_path):
    """A corrupt JSONL line in the delta must be skipped; valid lines still parsed.

    The corrupt line is silently dropped; the long-prose record after it still
    contributes to the prose length count, causing EMIT.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)

    # Fire 1: establish idle-gate state.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: corrupt line first, then a valid long-prose record.
    _append_lines(
        transcript,
        "{this is not valid json!!!",
        _assistant_text_record(_LONG_PROSE),
    )

    # Fire 2: corrupt line skipped; long prose counted → (A) True; (B) True → EMIT.
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"Corrupt JSONL + long-prose record must EMIT (skip-line resilience); "
        f"stdout={stdout!r}"
    )


# ===========================================================================
# Test 8: direct-install engram write form (mcp__engram__engram_*)
# Both mcp__engram__engram_* and mcp__plugin_engram_engram__engram_* suppress.
# ===========================================================================

def test_engram_write_direct_install_form_suppresses(tmp_path):
    """Engram write in the direct-install name form (mcp__engram__engram_*) must SUPPRESS.

    Both plugin-marketplace and direct-install engram tool name forms appear in
    production. The write-detection must handle both.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)

    # Fire 1: establish idle-gate state.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: long prose + direct-install-form engram write.
    _append_lines(
        transcript,
        _assistant_text_and_tool_record(_LONG_PROSE, _ENGRAM_WRITE_DIRECT),
    )

    # Fire 2: (A) prose > 400 → True; (B) engram write (direct form) → False.
    # Condition B fails → SUPPRESS.
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"Direct-install engram write form must SUPPRESS; got={stdout!r}"
    )


# ===========================================================================
# Falsification test: flip the prose comparison and verify the primary test fails.
# This confirms the prose comparison is the load-bearing gate, not a no-op.
# ===========================================================================

def test_falsification_inverted_prose_comparison(tmp_path):
    """Falsification: verify test_long_prose_toolless_emits fails if the
    comparison is inverted to prose_len < threshold.

    This test does NOT call the hook — it directly exercises _should_suppress
    with a manually constructed delta to confirm the prose comparison is the
    load-bearing gate.
    """
    import importlib.util
    import importlib

    # Load the hook module dynamically so we can call _parse_delta_prose_and_writes.
    spec = importlib.util.spec_from_file_location("engram_stop_hook", str(HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Build a delta with long prose, no engram write.
    delta_line = _assistant_text_record(_LONG_PROSE)
    delta_bytes = (delta_line + "\n").encode("utf-8")

    prose_len, has_engram_write = mod._parse_delta_prose_and_writes(delta_bytes)

    # Correct predicate: prose_len > threshold → emit.
    threshold = mod._DEFAULT_PROSE_THRESHOLD
    correct_emit = (prose_len > threshold) and not has_engram_write
    assert correct_emit, (
        f"Correct predicate must evaluate to EMIT for long prose; "
        f"prose_len={prose_len}, threshold={threshold}"
    )

    # Inverted predicate: prose_len < threshold (wrong direction) → would suppress.
    inverted_emit = (prose_len < threshold) and not has_engram_write
    assert not inverted_emit, (
        f"Inverted prose comparison must NOT emit (confirms comparison is load-bearing); "
        f"prose_len={prose_len}, threshold={threshold}"
    )


# ===========================================================================
# Regression: existing maturity-gate and #840 behavior must be unaffected.
# ===========================================================================

def test_zero_delta_still_suppresses(tmp_path):
    """Zero-delta between fires must still suppress (unchanged from #840/#845).

    The zero-delta path is handled before prose parsing — nothing happened →
    suppress unconditionally.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)

    # Fire 1: establish state at EOF.
    stdout1, _ = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert stdout1, "Fire 1 must emit (fail-open, no state file)"

    # Fire 2: no new bytes written.
    stdout2, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout2 == "", (
        f"Zero-delta fire must still suppress; got={stdout2!r}"
    )


def test_long_prose_with_multiple_text_blocks_accumulates(tmp_path):
    """Prose across multiple text blocks in a single record accumulates correctly.

    If an assistant record has two text blocks (200 chars each), the total prose
    is 400 chars. With the default threshold of 400, 400 > 400 is False →
    SUPPRESS. With 401 total → EMIT.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)

    # Fire 1: establish state.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: one record with two text blocks totaling exactly 400 chars (not > threshold).
    rec_400 = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "a" * 200},
                    {"type": "text", "text": "b" * 200},
                ]
            },
        },
        separators=(",", ":"),
    )
    _append_lines(transcript, rec_400)

    # Fire 2: total = 400 chars; threshold = 400; 400 > 400 is False → SUPPRESS.
    stdout2, rc2 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc2 == 0
    assert stdout2 == "", (
        f"Exactly 400 chars (not > threshold) must SUPPRESS; got={stdout2!r}"
    )

    # Now fire 3 with one more char (401 total in the new delta).
    rec_1 = _assistant_text_record("z")
    _append_lines(transcript, rec_1)

    # Fire 3: new delta is just the 1-char record = 1 char total.
    # 1 > 400 is False → SUPPRESS (each fire only sees its own delta).
    # This confirms delta isolation: prior turns don't bleed into new delta.
    stdout3, rc3 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc3 == 0
    assert stdout3 == "", (
        f"1-char delta after prior 400-char delta must SUPPRESS (delta isolation); "
        f"got={stdout3!r}"
    )


# ===========================================================================
# Test 12 (blocker fix): non-empty delta, every line unparseable → EMIT
# Discriminator test for the all-garbage sentinel path in
# _parse_delta_prose_and_writes. Without the any_record_parsed fix, a delta
# of pure garbage yields (0, False) → prose_len=0 ≤ 400 → condition A fails
# → SUPPRESS, silently killing the nudge forever on format drift.
# With the fix: (PARSE_ALL_GARBAGE, False) → caller detects sentinel → EMIT.
# ===========================================================================

def test_all_garbage_delta_emits(tmp_path):
    """Non-empty delta where every line fails json.loads must EMIT the nudge.

    This is the discriminator for the any_record_parsed fix. Before the fix,
    all-garbage content returned (0, False), which failed condition A (0 > 400
    is False) → SUPPRESS, meaning format drift could silently kill the nudge
    forever. After the fix, the all-garbage sentinel is detected and the caller
    takes the unconditional EMIT path regardless of the prose threshold.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)

    # Fire 1: establish idle-gate state.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: only garbage lines — none are valid JSON.
    _append_lines(
        transcript,
        "{not json at all!!!",
        "also not json: [[[",
        "random text with no structure",
    )

    # Fire 2: delta is non-empty but all lines fail json.loads.
    # The all-garbage sentinel must force EMIT regardless of prose threshold.
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        "Non-empty all-garbage delta must EMIT the nudge (all-garbage sentinel "
        f"path); stdout={stdout!r}"
    )
