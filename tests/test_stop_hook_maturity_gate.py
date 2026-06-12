"""Tests for write-nudge maturity-gate suppression in hooks/claude/engram-stop-hook.py.

Issue #845: the write-nudge is scaffolding for young graphs. For an established
agent with a mature graph the nudge is noise — write-discipline fires from
identity, not from an external tripwire. Empirical anchor: the hook was silently
mute until #824; established agents built healthy graphs without it.

Fix: maturity gate. Before the idle delta-scan (issue #840), count current nodes
in knowledge.db. If count >= threshold (default 300, configurable via
cadence.write_nudge_node_threshold in config.json), suppress silently. The gate
does NOT touch the #840 state file — stateless, so crossing back under threshold
(e.g. restore from backup) leaves the idle machinery undisturbed.

Fail direction: on any DB error (missing file, missing table, query failure),
return False (NOT muted → emit). A missing DB most likely means a newborn install
— exactly who the nudge is for. The maturity gate fails toward nudging
(young-agent assumption); the #840 idle gate fails toward emitting (uncertainty
assumption). Both directions resolve to emission.

All tests are hermetic — they never touch ~/.engram or any production path.
Invocation pattern mirrors test_stop_hook_idle_suppression.py: subprocess +
real hook script + synthetic tmp ENGRAM_HOME + real sqlite DB fixtures.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parent.parent / "src" / "engram" / "hooks" / "claude"
HOOK_PATH = HOOKS_DIR / "engram-stop-hook.py"
PYTHON = sys.executable

# The minimal CREATE TABLE statement matching the production schema for our
# purposes. We only need the columns the maturity gate queries: id, is_current.
_NODES_DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    claim       TEXT,
    created_at  TEXT NOT NULL DEFAULT '',
    is_current  INTEGER DEFAULT 1
);
"""


def _make_stop_payload(transcript_path: str, session_id: str = "test-maturity-gate") -> str:
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


def _make_db(engram_home: Path, *, n_current: int, n_retracted: int = 0) -> Path:
    """Create a minimal knowledge.db in engram_home with the given node counts.

    n_current nodes have is_current=1; n_retracted nodes have is_current=0.
    Returns the path to the DB.
    """
    db_path = engram_home / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_NODES_DDL)
        for i in range(n_current):
            conn.execute(
                "INSERT INTO nodes(id, type, created_at, is_current) VALUES (?,?,?,?)",
                (f"ob_{i:04d}", "observation", "2026-01-01T00:00:00", 1),
            )
        for i in range(n_retracted):
            conn.execute(
                "INSERT INTO nodes(id, type, created_at, is_current) VALUES (?,?,?,?)",
                (f"ret_{i:04d}", "observation", "2026-01-01T00:00:00", 0),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _write_config(engram_home: Path, threshold: int) -> None:
    """Write a minimal config.json with cadence.write_nudge_node_threshold."""
    config = {"cadence": {"write_nudge_node_threshold": threshold}}
    (engram_home / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _append_tool_use(transcript: Path) -> None:
    """Append an assistant JSONL entry with non-engram tool_use and long prose.

    Prose-gate update (issue #845 step 1.5): the new emit predicate requires
    BOTH (A) assistant prose > threshold AND (B) no engram write. This helper
    satisfies both conditions: prose > 400 chars (A) and tool name is
    "engram_query" — a read tool, not in the write set (B). Uses compact JSON
    to match the real Claude Code transcript format.
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


# ---------------------------------------------------------------------------
# Test (a): graph >= 300 current nodes → muted (silent stdout)
# ---------------------------------------------------------------------------

def test_mature_graph_muted(tmp_path):
    """A graph with >= 300 current nodes must produce empty stdout (maturity mute)."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _make_db(engram_home, n_current=300)
    _append_tool_use(transcript)  # tool activity present — idle gate would emit

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"Graph with 300 current nodes must produce silent stdout; got={stdout!r}"
    )


def test_mature_graph_above_threshold_muted(tmp_path):
    """A graph well above threshold (500 nodes) is also muted."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _make_db(engram_home, n_current=500)
    _append_tool_use(transcript)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"Graph with 500 current nodes must produce silent stdout; got={stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test (b): graph < 300 → not muted (falls through to #840 logic)
#           A tool-use delta present → nudge emits.
# ---------------------------------------------------------------------------

def test_young_graph_with_tool_use_emits(tmp_path):
    """A graph below threshold falls through to the idle gate; tool activity emits."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _make_db(engram_home, n_current=299)

    # Fire 1: establish idle-gate state
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Tool activity between fires
    _append_tool_use(transcript)

    # Fire 2: below threshold → idle gate active; tool delta → emits
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"Young graph (299 nodes) with tool delta must emit nudge; stdout={stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test (c): no knowledge.db → not muted (newborn fail-open)
# ---------------------------------------------------------------------------

def test_no_db_not_muted(tmp_path):
    """Missing knowledge.db must NOT mute — newborn install, exactly who needs the nudge."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # No knowledge.db created
    assert not (engram_home / "knowledge.db").exists()

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"Missing DB must emit nudge (fail-open newborn assumption); stdout={stdout!r}"
    )


def test_no_db_not_muted_second_fire(tmp_path):
    """Missing DB must NOT mute on a second fire (pre-existing #844 state file).

    The base test (test_no_db_not_muted) only exercises the first-fire path:
    with no state file, the idle gate emits unconditionally regardless of the
    maturity gate result.  This variant pre-seeds a #844 state file matching
    the transcript so the second fire's idle gate would SUPPRESS if the
    maturity gate incorrectly muted.  A nudge emit here proves the maturity
    gate's no-DB fail-open is what drives emission, isolated from first-fire
    behavior.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # No knowledge.db
    assert not (engram_home / "knowledge.db").exists()

    # Fire 1: seeds the #844 state file (no DB → maturity gate fails open →
    # idle gate first-fire → emits and writes state).
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Verify state file was created by fire 1.
    state_file = engram_home / ".write-nudge-last-fire.json"
    assert state_file.exists(), "Fire 1 must create the idle state file"

    # Tool activity between fires — idle gate would suppress without it.
    _append_tool_use(transcript)

    # Fire 2: state file exists + tool delta → idle gate would EMIT (tool
    # present).  No DB → maturity gate fails open.  Net result: nudge emits.
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        "Missing DB (second fire, tool delta) must emit nudge via maturity-gate "
        f"fail-open; stdout={stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test (d): config override threshold=5 with 10 nodes → muted
#           config override threshold=0 → gate disabled, emits even at 10 nodes
# ---------------------------------------------------------------------------

def test_config_override_threshold_5_mutes_at_10(tmp_path):
    """Custom threshold=5 in config; graph with 10 current nodes → muted."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _make_db(engram_home, n_current=10)
    _write_config(engram_home, threshold=5)
    _append_tool_use(transcript)  # tool activity — idle gate would emit

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"threshold=5 with 10 current nodes must be muted; got={stdout!r}"
    )


def test_config_threshold_zero_disables_gate(tmp_path):
    """threshold=0 disables the maturity gate; nudge emits even at 10 current nodes."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _make_db(engram_home, n_current=10)
    _write_config(engram_home, threshold=0)

    # Fire 1: establish idle-gate state
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Tool activity between fires
    _append_tool_use(transcript)

    # Fire 2: maturity gate disabled (threshold=0); tool delta → idle gate emits
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"threshold=0 must disable gate; nudge must emit at 10 nodes; stdout={stdout!r}"
    )


def test_config_negative_threshold_disables_gate(tmp_path):
    """threshold=-1 also disables the maturity gate."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _make_db(engram_home, n_current=10)
    _write_config(engram_home, threshold=-1)

    # Fire 1: establish idle-gate state
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Tool activity between fires
    _append_tool_use(transcript)

    # Fire 2: gate disabled → emits
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"threshold=-1 must disable gate; nudge must emit; stdout={stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test (e): retracted/superseded nodes (is_current=0) do NOT count
#           350 total / 250 current → NOT muted (only current nodes count)
# ---------------------------------------------------------------------------

def test_retracted_nodes_excluded_from_threshold(tmp_path):
    """Retracted nodes (is_current=0) must NOT count toward the threshold.

    350 total nodes (250 current + 100 retracted) is below the default 300
    current-node threshold — the gate must NOT fire; nudge must emit.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # 250 current + 100 retracted = 350 total, but only 250 current
    _make_db(engram_home, n_current=250, n_retracted=100)

    # Fire 1: establish idle-gate state
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Tool activity between fires
    _append_tool_use(transcript)

    # Fire 2: 250 current < 300 threshold → not muted → idle gate active →
    # tool delta present → emits
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"250 current (100 retracted, 350 total) must NOT be muted; stdout={stdout!r}"
    )


def test_retracted_nodes_at_threshold_boundary(tmp_path):
    """300 total but only 299 current → NOT muted. Current-nodes-only semantic."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _make_db(engram_home, n_current=299, n_retracted=1)

    # Fire 1: establish idle-gate state
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Tool activity between fires
    _append_tool_use(transcript)

    # Fire 2: 299 current < 300 → not muted → emits
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"299 current + 1 retracted (300 total) must NOT be muted; stdout={stdout!r}"
    )


# ---------------------------------------------------------------------------
# Guard: idle state file must NOT be written when maturity gate fires
# ---------------------------------------------------------------------------

def test_maturity_gate_does_not_touch_state_file(tmp_path):
    """When the maturity gate fires, the #840 idle state file must be untouched.

    The gate is stateless — crossing back under threshold (restore from backup)
    must leave the idle machinery in its prior state.
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    state_file = engram_home / ".write-nudge-last-fire.json"

    # Verify state file doesn't exist before running
    assert not state_file.exists()

    _make_db(engram_home, n_current=300)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", f"Maturity gate must produce silent stdout; got={stdout!r}"
    assert not state_file.exists(), (
        "Maturity gate must NOT create/modify the idle state file"
    )


# ---------------------------------------------------------------------------
# Regression: #824 envelope intact on young-graph emit path
# ---------------------------------------------------------------------------

def test_young_graph_emitted_nudge_preserves_824_envelope(tmp_path):
    """On a young graph, the emitted nudge must preserve the #824 JSON envelope."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _make_db(engram_home, n_current=50)

    # Fire 1: establishes idle state
    _run_hook(_make_stop_payload(str(transcript)), engram_home)
    _append_tool_use(transcript)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout, "Young-graph emit must produce non-empty stdout"

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"Emitted stdout is not valid JSON: {exc}\nstdout={stdout!r}")

    assert "hookSpecificOutput" in parsed, "Missing hookSpecificOutput — #824 envelope broken"
    hso = parsed["hookSpecificOutput"]
    assert hso.get("hookEventName") == "Stop"
    ctx = hso.get("additionalContext")
    assert isinstance(ctx, str) and ctx, "additionalContext must be a non-empty string"
    assert "ENGRAM Write Check" in ctx, "Write-check text missing from additionalContext"


# ---------------------------------------------------------------------------
# Regression: #841 nudge text — explicit NO-output instruction, no 'carry on'
# ---------------------------------------------------------------------------

def _get_nudge_ctx(tmp_path) -> str:
    """Helper: run hook on a young graph and return the emitted additionalContext."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _make_db(engram_home, n_current=50)

    # Fire 1: establish idle-gate state
    _run_hook(_make_stop_payload(str(transcript)), engram_home)
    _append_tool_use(transcript)

    # Fire 2: nudge emits
    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc == 0 and stdout, f"Expected nudge emit; stdout={stdout!r}"
    parsed = json.loads(stdout)
    return parsed["hookSpecificOutput"]["additionalContext"]


def test_nudge_contains_no_output_instruction(tmp_path):
    """Emitted nudge must say 'NO output', not 'carry on' (#841 fix).

    Mutation note: restoring the old 'carry on' text would make the 'carry on'
    assertion below fail immediately, catching any regression to the ambiguous
    wording this fix replaced.
    """
    ctx = _get_nudge_ctx(tmp_path)
    assert "NO output" in ctx, (
        f"Nudge must contain 'NO output' (explicit zero-output instruction); ctx={ctx!r}"
    )
    assert "carry on" not in ctx, (
        f"Nudge must NOT contain 'carry on' (ambiguous old text replaced by #841); ctx={ctx!r}"
    )


def test_nudge_contains_space_after_insight_comma(tmp_path):
    """Emitted nudge must contain 'insight, or design' (space after comma) — missing-space fix.

    The pre-fix string concatenation 'insight,' + 'or design' rendered as
    'insight,or design' (no space). This test pins the corrected form.
    """
    ctx = _get_nudge_ctx(tmp_path)
    assert "insight, or design" in ctx, (
        f"Nudge must contain 'insight, or design' (space after comma); ctx={ctx!r}"
    )
