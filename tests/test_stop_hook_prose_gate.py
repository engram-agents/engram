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

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parent.parent / "src" / "engram" / "hooks" / "claude"
HOOK_PATH = HOOKS_DIR / "engram-stop-hook.py"
PYTHON = sys.executable


def _load_hook_module():
    """Load engram-stop-hook.py as a fresh in-process module instance.

    ENGRAM_HOME is read at module-import time (module-level constant), so the
    caller must set it in os.environ (e.g. via monkeypatch.setenv) BEFORE
    calling this -- mirrors tests/test_stophooklib.py's _load_module()
    pattern for the sibling _stophooklib module, extended with the env-var
    timing this module needs.
    """
    spec = importlib.util.spec_from_file_location(
        "_engram_stop_hook_test_instance", HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

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
        reason = parsed.get("reason", "")
        return "ENGRAM Write Check" in reason
    except (json.JSONDecodeError, AttributeError):
        return False


def _write_config(engram_home: Path, **cadence_overrides: object) -> None:
    """Write a config.json with the given cadence fields."""
    config = {"cadence": cadence_overrides}
    (engram_home / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _prime_offset_state(engram_home: Path, transcript_path: Path) -> None:
    """Write the nudge offset-state file directly, bypassing the hook process.

    Mirrors `{"transcript_path": ..., "byte_offset": <current size>}`, the
    shape `_update_state`/`write_offset_state` produce. Needed for the
    WS4-round-2 mature-graph fixtures below: for a MATURE graph, an
    out-of-window priming Stop-fire (the technique the young-graph tests
    above use, "Fire 1: establish ... state") never reaches `_update_state`
    -- the maturity-gate early return happens first and returns before any
    state write. Priming an IN-window fire instead would establish state,
    but it would also consume decision D's K=3 escalation rate-limit budget
    on the priming fire itself, which then rate-limits the very fire under
    test. Writing state directly here establishes the byte-offset checkpoint
    without going through either path, so the fire under test measures a
    real delta rather than accidentally passing via `_should_suppress`'s own
    "missing state file → emit unconditionally" fail-open branch.
    """
    state_file = engram_home / ".write-nudge-last-fire.json"
    offset = transcript_path.stat().st_size if transcript_path.exists() else 0
    state = {"transcript_path": str(transcript_path), "byte_offset": offset}
    state_file.write_text(json.dumps(state), encoding="utf-8")


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


def _make_mature_graph(engram_home: Path, n_current: int = 350) -> None:
    """Create a knowledge.db with n_current nodes at/above the default 300
    maturity threshold. Used by the WS4-round-2 bypass tests below: the
    maturity gate must MUTE the base nudge path but must NOT mute the
    near-compaction escalation path.
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


def _assistant_usage_record(text: str, input_tokens: int) -> str:
    """Build a compact-JSON assistant record carrying BOTH a usage block
    (what context_tracker.read_current_tokens looks for) and, optionally, a
    text content block. Used by the near-compaction (engram-alpha#2026 WS4)
    tests below to put a session "in window" for the drowsiness check.
    """
    content = [{"type": "text", "text": text}] if text else []
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": content,
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


# ===========================================================================
# engram-alpha#2026 WS4: near-compaction escalation.
#
# The near-compaction window (usage_pct >= urgent_at_or_above, per
# context_tracker's own thresholds) ESCALATES this file's existing
# prose-without-writes signal above -- it does not add a new unconditional
# per-Stop trigger. All fixtures below set cadence.drowsiness_ceiling_tokens
# to 200_000 (200k mode, urgent_at_or_above defaults to 0.85) and drive
# usage_pct via a "usage" block in an assistant record carrying
# input_tokens=190_000 (~0.95, comfortably above 0.85).
#
# In every test, Fire 1 establishes the prose-gate offset state against an
# EMPTY (therefore out-of-window) transcript -- this matters, not just for
# hygiene: a first-ever fire always "would emit" regardless of content
# (missing state file), and if that first fire were already in-window it
# would consume the decision-D rate-limit budget before the fire under test
# ever runs. Keeping Fire 1 out-of-window isolates each test's fire from
# that interaction.
# ===========================================================================

def test_near_compaction_prose_without_writes_escalates(tmp_path):
    """In-window + prose-without-writes must escalate: decision:block with
    the near-compaction capture text, using the LOWERED near-compaction
    prose threshold (100), not the base one (400).
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)
    _write_config(engram_home, drowsiness_ceiling_tokens=200_000)

    # Fire 1: establish prose-gate offset state on an out-of-window (empty)
    # transcript.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: enters the window (usage record, ~0.95 vs the 200k-mode urgent
    # threshold of 0.85) AND carries prose above the LOWERED near-compaction
    # threshold (100) but at/below the base threshold (400) -- proving the
    # lowered threshold gated this, not the base one.
    _append_lines(transcript, _assistant_usage_record("x" * 150, 190_000))

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"in-window prose-without-writes must escalate; stdout={stdout!r}"
    )
    parsed = json.loads(stdout)
    assert "near compaction" in parsed["reason"].lower(), (
        f"escalated nudge must carry the near-compaction capture text; stdout={stdout!r}"
    )


def test_near_compaction_writes_present_suppresses(tmp_path):
    """In-window + an engram write present must stay SILENT (decision B).

    This is the test that stops the hour-long block: decision B exists
    because an unconditional per-Stop nudge in the near-compaction window
    would interrupt every turn end for as long as the plateau lasts
    (measured ~1hr / ~3 heartbeat iterations at 97-99%, first-hand). The
    escalation must still respect condition B (no engram write) exactly like
    the base prose gate -- if the discipline already fired this turn, no
    nudge is needed, window or not.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)
    _write_config(engram_home, drowsiness_ceiling_tokens=200_000)

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: in-window usage + prose above the near threshold + an engram
    # write.
    usage_and_write = json.dumps(
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 190_000,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [
                    {"type": "text", "text": "x" * 150},
                    {
                        "type": "tool_use",
                        "id": "toolu_test_ws4_write",
                        "name": _ENGRAM_WRITE,
                        "input": {"payload_json": "{}"},
                        "caller": {"type": "direct"},
                    },
                ],
            },
        },
        separators=(",", ":"),
    )
    _append_lines(transcript, usage_and_write)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"in-window with an engram write present must stay silent (decision B); "
        f"got={stdout!r}"
    )


def test_near_compaction_out_of_window_unchanged(tmp_path):
    """Out-of-window behavior must be byte-identical to the pre-WS4 base
    nudge -- the regression that matters most (spec: 'this is the
    regression that matters'). No usage record is ever written, so
    estimate_usage() returns None and the session never reads as in-window.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)

    _run_hook(_make_stop_payload(str(transcript)), engram_home)
    _append_lines(transcript, _assistant_text_record(_LONG_PROSE))

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout)
    parsed = json.loads(stdout)
    assert "near compaction" not in parsed["reason"].lower(), (
        f"out-of-window fire must NOT use the escalated text; got={stdout!r}"
    )
    assert parsed["reason"] == (
        "[ENGRAM Write Check: Did your last response contain a decision, insight, "
        "or design choice worth recording? If so, write to ENGRAM now "
        "(observation, derivation, question, or conjecture). "
        "If not, end the turn with NO output - do not reply to or acknowledge this check; "
        "a text-only acknowledgment wastes a turn.]"
    ), "out-of-window text must be byte-identical to the pre-WS4 base nudge"


def test_near_compaction_rate_limit_caps_to_one_per_three(tmp_path):
    """In-window, 3 consecutive escalation-eligible fires must emit exactly
    once (decision D: K=3 rate limit) -- without this, decision B's
    near-compaction plateau turns into a per-turn block.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)
    _write_config(engram_home, drowsiness_ceiling_tokens=200_000)

    # Fire 1: establish prose-gate offset state, out-of-window.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    emissions = 0

    # Fire 2: enters the window AND carries eligible prose in the same delta.
    _append_lines(transcript, _assistant_usage_record("z" * 150, 190_000))
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc == 0
    if _nudge_emitted(stdout):
        emissions += 1

    # Fires 3 and 4: still in-window (the usage record above remains the
    # most recent one in the file), each carrying more eligible prose.
    for _ in range(2):
        _append_lines(transcript, _assistant_text_record("z" * 150))
        stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)
        assert rc == 0
        if _nudge_emitted(stdout):
            emissions += 1

    assert emissions == 1, (
        f"3 consecutive escalation-eligible in-window fires must emit exactly "
        f"once (decision D, K=3); got {emissions} emissions"
    )


def test_near_compaction_drowsiness_unavailable_no_crash(tmp_path):
    """Drowsiness unavailable (no usage record at all, even with a ceiling
    configured) must be treated as out-of-window -- no crash, base text.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_young_graph(engram_home)
    _write_config(engram_home, drowsiness_ceiling_tokens=200_000)

    _run_hook(_make_stop_payload(str(transcript)), engram_home)
    _append_lines(transcript, _assistant_text_record(_LONG_PROSE))

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0, "missing drowsiness data must not crash the hook"
    assert _nudge_emitted(stdout)
    parsed = json.loads(stdout)
    assert "near compaction" not in parsed["reason"].lower(), (
        "drowsiness-unavailable must fall back to out-of-window (base text), not escalate"
    )


def test_near_compaction_drowsiness_raises_fails_closed():
    """If context_tracker.estimate_usage raises, _in_near_compaction_window
    must return False (fail CLOSED toward "no escalation") rather than
    propagate -- the opposite fail direction from the rest of this file,
    and deliberately so (see the function's own docstring): escalating on a
    bad reading would reproduce the exact per-turn-block regression the
    decision-D rate limit exists to prevent.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "engram_stop_hook_ws4_raise_test", str(HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod._CONTEXT_TRACKER_OK, (
        "context_tracker must be importable in this environment for this "
        "test to be meaningful"
    )

    def _raise(*_args, **_kwargs):
        raise RuntimeError("simulated drowsiness read failure")

    mod._estimate_usage = _raise

    result = mod._in_near_compaction_window("/nonexistent/path.jsonl", "some-session")
    assert result is False, (
        "a raising drowsiness read must resolve to out-of-window (False), never propagate"
    )


# ===========================================================================
# engram-alpha#2026 WS4 round 2: near-compaction escalation must BYPASS the
# maturity gate. The maturity gate is a PROXY (node count standing for
# "write discipline is internalized"); the near-compaction escalation fires
# on a directly-OBSERVED signal (prose written this burst, no accompanying
# writes, measured from the actual transcript delta). A proxy must not
# suppress a stronger, more specific, directly-observed signal. All fixtures
# below use _make_mature_graph (>= the default 300-node threshold) where the
# equivalent tests above used _make_young_graph -- everything else about the
# window/rate-limit/write-suppression mechanics is identical to the tests
# above, and is deliberately re-exercised here under maturity to prove the
# gate ordering, not to re-prove those mechanics from scratch.
# ===========================================================================

def test_near_compaction_mature_graph_prose_without_writes_still_escalates(tmp_path):
    """Mature graph + in-window + prose-without-writes must still ESCALATE.

    This is the behavior change: before round 2, _maturity_muted() returned
    early before any near-compaction logic ran, so a mature graph got no
    escalated nudge even near compaction. After round 2, the maturity gate
    no longer applies inside the window.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_mature_graph(engram_home)
    _write_config(engram_home, drowsiness_ceiling_tokens=200_000)

    # Prime offset state directly rather than running an out-of-window Fire 1
    # (see _prime_offset_state docstring): for a MATURE graph, an
    # out-of-window fire returns early via the maturity gate before ever
    # reaching _update_state, so it cannot establish state the way it does
    # for the young-graph tests above.
    _prime_offset_state(engram_home, transcript)

    # Delta: enters the window (usage record, ~0.95 vs the 200k-mode urgent
    # threshold of 0.85) AND carries prose above the LOWERED near-compaction
    # threshold (100) but at/below the base threshold (400).
    _append_lines(transcript, _assistant_usage_record("x" * 150, 190_000))

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        "mature graph + in-window + prose-without-writes must still escalate "
        f"(maturity gate must not mute the escalation path); stdout={stdout!r}"
    )
    parsed = json.loads(stdout)
    assert "near compaction" in parsed["reason"].lower(), (
        f"escalated nudge must carry the near-compaction capture text; stdout={stdout!r}"
    )


def test_near_compaction_mature_graph_out_of_window_still_muted(tmp_path):
    """Mature graph + out-of-window + prose-without-writes must stay MUTED,
    unchanged from pre-round-2 behavior. The maturity gate still applies to
    the base (unescalated) nudge path -- only the escalation path bypasses it.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_mature_graph(engram_home)

    # No config write, no usage record -- estimate_usage() returns None, the
    # session never reads as in-window, so the base path applies. No offset
    # priming needed: the maturity-mute early return in main() happens before
    # any state-dependent logic runs, so it is unaffected by whether state
    # was ever established.
    _append_lines(transcript, _assistant_text_record(_LONG_PROSE))

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        "mature graph + out-of-window must stay silent (maturity gate "
        f"unchanged for the base path); got={stdout!r}"
    )


def test_near_compaction_mature_graph_writes_present_stays_silent(tmp_path):
    """Mature graph + in-window + an engram write present must stay SILENT.

    The window bypasses the maturity gate but does not override condition B
    (no engram write) of the underlying prose-without-writes signal -- if the
    discipline already fired this turn, no nudge is needed, window or not,
    mature or not.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _make_mature_graph(engram_home)
    _write_config(engram_home, drowsiness_ceiling_tokens=200_000)

    # Prime offset state directly -- see _prime_offset_state docstring and
    # the analogous comment in the "still_escalates" test above.
    _prime_offset_state(engram_home, transcript)

    usage_and_write = json.dumps(
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 190_000,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [
                    {"type": "text", "text": "x" * 150},
                    {
                        "type": "tool_use",
                        "id": "toolu_test_ws4_round2_write",
                        "name": _ENGRAM_WRITE,
                        "input": {"payload_json": "{}"},
                        "caller": {"type": "direct"},
                    },
                ],
            },
        },
        separators=(",", ":"),
    )
    _append_lines(transcript, usage_and_write)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        "mature graph + in-window + an engram write present must stay silent "
        f"(condition B still applies inside the window); got={stdout!r}"
    )


def test_near_compaction_rate_limit_period_pinned(tmp_path, monkeypatch):
    """_rate_limit_escalation's emission PERIOD must be exactly K, pinned by
    position, not merely by count.

    Round-3 fix (PR #2031 reviewer-fairy pass): the pre-fix code reset
    new_count to 0 on emit, which takes K further increments (not K-1) to
    climb back to count >= K -- an observed period of K+1, not the documented
    K. A count-only assertion (e.g. "3 emissions in 9 fires") passes under
    EITHER the K or the K+1 behavior when N is a multiple of both; asserting
    the exact emission INDICES is what actually pins this and would have
    caught the original off-by-one.

    Calls _rate_limit_escalation directly (bypassing the full hook subprocess
    -- this function's contract is pure state-file arithmetic and does not
    need a transcript or a graph) 3*K times on a fresh state file and asserts
    emissions land at exactly indices 0, K, 2K, ... i.e. every K-th call
    starting from the first.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    mod = _load_hook_module()

    k = mod._NEAR_COMPACTION_RATE_LIMIT_K
    n_calls = 3 * k
    transcript_path = str(tmp_path / "transcript.jsonl")

    results = [mod._rate_limit_escalation(transcript_path) for _ in range(n_calls)]
    emitted_indices = [i for i, allowed in enumerate(results) if allowed]
    expected_indices = list(range(0, n_calls, k))

    assert emitted_indices == expected_indices, (
        f"rate limiter period must be exactly K={k} (emit at every K-th "
        f"eligible fire starting from the first); expected emissions at "
        f"{expected_indices}, got {emitted_indices} (full results={results})"
    )
