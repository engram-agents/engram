"""Tests for deference-detector cooldown after real user message.

The cooldown suppresses the stop hook from writing a deference-detected
marker for N minutes after a real user message lands during loop mode,
since deference phrases are appropriate when the agent is responding to
a human rather than operating autonomously.

The prompt hook writes the cooldown marker when it detects a real user
message (not a cron heartbeat sentinel or matching the stored loop_prompt).

Each test subprocess-invokes the hook script with a temp ENGRAM_HOME so
it exercises the real entrypoint rather than imported internals.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
PROMPT_HOOK = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-deference-detector-prompt.py"
STOP_HOOK   = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-deference-detector-stop.py"

_STOP_STDIN_WITH_HIT = json.dumps({
    "session_id": "test-session",
    "transcript_path": "",
    "last_assistant_message": "Should I proceed with this? Just let me know if you want me to.",
})


def _run_hook(script: Path, env: dict, stdin: str = "{}") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )


def _write_cooldown(tmp: str, minutes_ago: float) -> Path:
    """Write a deference-cooldown-at.txt timestamped `minutes_ago` minutes in the past."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    path = Path(tmp) / "deference-cooldown-at.txt"
    path.write_text(ts.isoformat())
    return path


def _write_loop_marker(tmp: str, extra: dict = None) -> Path:
    """Write a loop-mode.json marker."""
    data = {}
    if extra:
        data.update(extra)
    path = Path(tmp) / "loop-mode.json"
    path.write_text(json.dumps(data))
    return path


def _write_config(tmp: str, cooldown_minutes: float) -> Path:
    """Write a config.json with the given cooldown_minutes value."""
    cfg = {"deference_detector": {"cooldown_minutes": cooldown_minutes}}
    path = Path(tmp) / "config.json"
    path.write_text(json.dumps(cfg))
    return path


# ---------------------------------------------------------------------------
# Stop hook cooldown suppression tests
# ---------------------------------------------------------------------------

class TestStopHookCooldown:

    def test_stop_suppressed_within_cooldown(self):
        """Cooldown marker written 1 min ago, config 10 min → stop hook suppressed."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_loop_marker(tmp)
            _write_cooldown(tmp, minutes_ago=1)
            _write_config(tmp, cooldown_minutes=10)

            marker_path = Path(tmp) / "deference-detected.json"
            result = _run_hook(STOP_HOOK, {"ENGRAM_HOME": tmp}, stdin=_STOP_STDIN_WITH_HIT)

            assert result.returncode == 0
            assert not marker_path.exists(), (
                "Stop hook wrote deference marker during cooldown window — should be suppressed"
            )
            # Log should mention cooldown
            log_path = Path(tmp) / "deference-detector.log"
            if log_path.exists():
                log_text = log_path.read_text()
                assert "cooldown" in log_text.lower(), (
                    f"Expected 'cooldown' in log, got: {log_text!r}"
                )

    def test_stop_fires_outside_cooldown(self):
        """Cooldown marker written 15 min ago, config 10 min → stop hook fires normally."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_loop_marker(tmp)
            _write_cooldown(tmp, minutes_ago=15)
            _write_config(tmp, cooldown_minutes=10)

            marker_path = Path(tmp) / "deference-detected.json"
            result = _run_hook(STOP_HOOK, {"ENGRAM_HOME": tmp}, stdin=_STOP_STDIN_WITH_HIT)

            assert result.returncode == 0
            assert marker_path.exists(), (
                "Stop hook should fire normally when cooldown window has expired"
            )
            data = json.loads(marker_path.read_text())
            assert data.get("pending") is True

    def test_stop_fires_when_no_cooldown_marker(self):
        """No cooldown marker at all → stop hook fires normally."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_loop_marker(tmp)
            _write_config(tmp, cooldown_minutes=10)
            # Deliberately do NOT write deference-cooldown-at.txt

            marker_path = Path(tmp) / "deference-detected.json"
            result = _run_hook(STOP_HOOK, {"ENGRAM_HOME": tmp}, stdin=_STOP_STDIN_WITH_HIT)

            assert result.returncode == 0
            assert marker_path.exists(), (
                "Stop hook should fire normally when no cooldown marker exists"
            )
            data = json.loads(marker_path.read_text())
            assert data.get("pending") is True

    def test_stop_fires_when_cooldown_zero_disabled(self):
        """cooldown_minutes = 0 → cooldown disabled, stop hook fires even within window."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_loop_marker(tmp)
            _write_cooldown(tmp, minutes_ago=1)
            _write_config(tmp, cooldown_minutes=0)

            marker_path = Path(tmp) / "deference-detected.json"
            result = _run_hook(STOP_HOOK, {"ENGRAM_HOME": tmp}, stdin=_STOP_STDIN_WITH_HIT)

            assert result.returncode == 0
            assert marker_path.exists(), (
                "Stop hook should fire when cooldown_minutes=0 (cooldown disabled)"
            )
            data = json.loads(marker_path.read_text())
            assert data.get("pending") is True


# ---------------------------------------------------------------------------
# Prompt hook cooldown-writing tests
# ---------------------------------------------------------------------------

class TestPromptHookCooldownWrite:

    def test_prompt_writes_cooldown_for_real_user_message(self):
        """Real user message in loop mode → deference-cooldown-at.txt written."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_loop_marker(tmp)  # no loop_prompt stored

            cooldown_path = Path(tmp) / "deference-cooldown-at.txt"
            assert not cooldown_path.exists()

            stdin = json.dumps({
                "session_id": "test-session",
                "transcript_path": "",
                "prompt": "what is the status?",
            })
            result = _run_hook(PROMPT_HOOK, {"ENGRAM_HOME": tmp}, stdin=stdin)

            assert result.returncode == 0
            assert cooldown_path.exists(), (
                "Prompt hook should write cooldown marker for a real user message"
            )
            # Should be a parseable ISO timestamp
            ts_text = cooldown_path.read_text().strip()
            parsed = datetime.fromisoformat(ts_text)
            assert parsed.tzinfo is not None, "Timestamp should be timezone-aware"

    def test_prompt_skips_cooldown_for_autonomous_loop_sentinel(self):
        """Autonomous-loop sentinel → deference-cooldown-at.txt NOT written."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_loop_marker(tmp)

            cooldown_path = Path(tmp) / "deference-cooldown-at.txt"

            stdin = json.dumps({
                "session_id": "test-session",
                "transcript_path": "",
                "prompt": "<<autonomous-loop-dynamic>>",
            })
            result = _run_hook(PROMPT_HOOK, {"ENGRAM_HOME": tmp}, stdin=stdin)

            assert result.returncode == 0
            assert not cooldown_path.exists(), (
                "Prompt hook should NOT write cooldown for autonomous-loop sentinel"
            )

    def test_prompt_skips_cooldown_for_loop_prompt_match(self):
        """Prompt matches stored loop_prompt → deference-cooldown-at.txt NOT written."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_loop_marker(tmp, extra={"loop_prompt": "do my research"})

            cooldown_path = Path(tmp) / "deference-cooldown-at.txt"

            stdin = json.dumps({
                "session_id": "test-session",
                "transcript_path": "",
                "prompt": "do my research",
            })
            result = _run_hook(PROMPT_HOOK, {"ENGRAM_HOME": tmp}, stdin=stdin)

            assert result.returncode == 0
            assert not cooldown_path.exists(), (
                "Prompt hook should NOT write cooldown when prompt matches stored loop_prompt"
            )

    def test_prompt_writes_cooldown_for_different_prompt(self):
        """Prompt differs from stored loop_prompt → treated as real user message."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_loop_marker(tmp, extra={"loop_prompt": "do my research"})

            cooldown_path = Path(tmp) / "deference-cooldown-at.txt"

            stdin = json.dumps({
                "session_id": "test-session",
                "transcript_path": "",
                "prompt": "what is the status?",
            })
            result = _run_hook(PROMPT_HOOK, {"ENGRAM_HOME": tmp}, stdin=stdin)

            assert result.returncode == 0
            assert cooldown_path.exists(), (
                "Prompt hook should write cooldown for a prompt that differs from stored loop_prompt"
            )

    def test_prompt_skips_cooldown_for_canonical_loop_stub(self):
        """Canonical ScheduleWakeup stub ('Loop continuation. Read...') → cooldown NOT written.

        Covers loops whose marker lacks loop_prompt — the standard engram-loop skill
        uses this stub prefix for all ScheduleWakeup continuations.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _write_loop_marker(tmp)  # no loop_prompt stored

            cooldown_path = Path(tmp) / "deference-cooldown-at.txt"

            stub = (
                "Loop continuation. Read `~/.engram/loop-mode.json` for the loop topic, "
                "instructions, and current state. **If the file is absent → the loop already "
                "ended; STOP, do not execute, do not re-arm.** If present → continue per its contents."
            )
            stdin = json.dumps({
                "session_id": "test-session",
                "transcript_path": "",
                "prompt": stub,
            })
            result = _run_hook(PROMPT_HOOK, {"ENGRAM_HOME": tmp}, stdin=stdin)

            assert result.returncode == 0
            assert not cooldown_path.exists(), (
                "Prompt hook should NOT write cooldown for canonical loop continuation stub"
            )

    def test_prompt_skips_cooldown_for_empty_prompt(self):
        """Empty prompt (headless/older CC) → cooldown NOT written.

        An empty prompt_text must not trigger the cooldown, since it is not
        a real user message — it indicates a headless invocation or an older
        Claude Code version that doesn't populate the prompt field.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _write_loop_marker(tmp)

            cooldown_path = Path(tmp) / "deference-cooldown-at.txt"

            stdin = json.dumps({
                "session_id": "test-session",
                "transcript_path": "",
                "prompt": "",
            })
            result = _run_hook(PROMPT_HOOK, {"ENGRAM_HOME": tmp}, stdin=stdin)

            assert result.returncode == 0
            assert not cooldown_path.exists(), (
                "Prompt hook should NOT write cooldown when prompt is empty"
            )
