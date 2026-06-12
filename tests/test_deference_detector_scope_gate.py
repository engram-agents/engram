"""Tests for deference-detector scope-gate (#287).

Both hooks (prompt + stop) should no-op when $ENGRAM_HOME/loop-mode.json
is absent (interactive mode). They should fire normally when it is present
(self-loop / autonomous mode).

Each test subprocess-invokes the hook script with a temp ENGRAM_HOME so
it exercises the real entrypoint rather than imported internals.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
PROMPT_HOOK = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-deference-detector-prompt.py"
STOP_HOOK   = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-deference-detector-stop.py"

_VALID_MARKER = {
    "pending": True,
    "hit_count": 1,
    "unique_labels": 1,
    "hits": [{"label": "want-me-to-q", "snippet": "want me to proceed?"}],
}

_STOP_STDIN_WITH_HIT = json.dumps({
    "session_id": "test-session",
    "transcript_path": "",
    "last_assistant_message": "Should I proceed with this? Just let me know if you want me to.",
})

_STOP_STDIN_EMPTY = json.dumps({
    "session_id": "test-session",
    "transcript_path": "",
    "last_assistant_message": "Here is the result.",
})


def _run_hook(script: Path, env: dict, stdin: str = "{}") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )


# ---------------------------------------------------------------------------
# Prompt hook tests
# ---------------------------------------------------------------------------

class TestPromptHookNoopsOutsideLoopMode:
    """test_prompt_hook_noops_outside_loop_mode"""

    def test_no_output_no_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Write a deference-detected marker — hook should clear it.
            marker_path = Path(tmp) / "deference-detected.json"
            marker_path.write_text(json.dumps(_VALID_MARKER))

            result = _run_hook(PROMPT_HOOK, {"ENGRAM_HOME": tmp})

            # No hookSpecificOutput — hook no-ops outside loop mode.
            assert result.returncode == 0
            stdout = result.stdout.strip()
            assert stdout == "", f"Expected empty stdout, got: {stdout!r}"

            # Defensive cleanup: marker must be cleared.
            assert not marker_path.exists(), "Stale marker was not cleared by prompt hook"

    def test_no_marker_present_still_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_hook(PROMPT_HOOK, {"ENGRAM_HOME": tmp})
            assert result.returncode == 0
            assert result.stdout.strip() == ""


class TestPromptHookFiresInsideLoopMode:
    """test_prompt_hook_fires_inside_loop_mode"""

    def test_emits_reworded_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Touch loop-mode marker.
            (Path(tmp) / "loop-mode.json").write_text("{}")

            # Write deference-detected marker.
            marker_path = Path(tmp) / "deference-detected.json"
            marker_path.write_text(json.dumps(_VALID_MARKER))

            result = _run_hook(PROMPT_HOOK, {"ENGRAM_HOME": tmp})

            assert result.returncode == 0
            stdout = result.stdout.strip()
            assert stdout, "Expected non-empty stdout in loop mode"

            data = json.loads(stdout)
            ctx = data["hookSpecificOutput"]["additionalContext"]

            # Load-bearing substrings from the spec.
            assert "self-loop mode" in ctx, f"Missing 'self-loop mode' in: {ctx}"
            assert "RLHF-baked" in ctx, f"Missing 'RLHF-baked' in: {ctx}"
            assert "informational, not actionable" in ctx, f"Missing 'informational, not actionable' in: {ctx}"
            assert "checking-before-executing" in ctx, f"Missing 'checking-before-executing' in: {ctx}"

            # Per-hit snippet should appear.
            assert "want me to proceed?" in ctx, f"Missing snippet in: {ctx}"

            # Marker must be cleared after surfacing.
            assert not marker_path.exists(), "Marker was not cleared after prompt hook fired"


# ---------------------------------------------------------------------------
# Stop hook tests
# ---------------------------------------------------------------------------

class TestStopHookSkipsMarkerWriteOutsideLoopMode:
    """test_stop_hook_skips_marker_write_outside_loop_mode"""

    def test_no_marker_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker_path = Path(tmp) / "deference-detected.json"

            result = _run_hook(STOP_HOOK, {"ENGRAM_HOME": tmp}, stdin=_STOP_STDIN_WITH_HIT)

            assert result.returncode == 0
            assert not marker_path.exists(), (
                "Stop hook wrote deference-detected.json outside loop mode — should skip"
            )


class TestStopHookWritesMarkerInsideLoopMode:
    """test_stop_hook_writes_marker_inside_loop_mode"""

    def test_marker_written_on_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Touch loop-mode marker.
            (Path(tmp) / "loop-mode.json").write_text("{}")

            marker_path = Path(tmp) / "deference-detected.json"

            result = _run_hook(STOP_HOOK, {"ENGRAM_HOME": tmp}, stdin=_STOP_STDIN_WITH_HIT)

            assert result.returncode == 0
            assert marker_path.exists(), (
                "Stop hook did not write deference-detected.json inside loop mode"
            )
            data = json.loads(marker_path.read_text())
            assert data.get("pending") is True
            assert data.get("hit_count", 0) >= 1

    def test_no_marker_when_no_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "loop-mode.json").write_text("{}")

            marker_path = Path(tmp) / "deference-detected.json"

            result = _run_hook(STOP_HOOK, {"ENGRAM_HOME": tmp}, stdin=_STOP_STDIN_EMPTY)

            assert result.returncode == 0
            # No hits in the message — no marker should be written.
            assert not marker_path.exists(), (
                "Stop hook wrote marker even though message had no deference phrases"
            )
