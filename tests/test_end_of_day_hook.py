"""Tests for engram-end-of-day-hook.py — surfaces engram-sleep nudge.

Covers phrase-matching positives, phrase-matching negatives, command-skip
(prompts starting with `/` or `!`), and the sleep-marker freshness gate.

Each test invokes the hook as a subprocess with a fake ENGRAM_HOME so
the marker-check + stdin-read paths run end-to-end.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).parent.parent / "src" / "engram" / "hooks" / "claude" / "engram-end-of-day-hook.py"
)


def _run_hook(prompt: str, engram_home: Path) -> tuple[int, str]:
    """Invoke the hook with the given prompt + ENGRAM_HOME; return (rc, stdout)."""
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps({"prompt": prompt}),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.returncode, proc.stdout


@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    """Fake ENGRAM_HOME with no sleep marker present."""
    home = tmp_path / "engram_home"
    home.mkdir()
    (home / "sessions").mkdir()
    return home


def _write_sleep_marker(engram_home: Path, hours_ago: float) -> None:
    """Write a sleep-success marker dated `hours_ago` ago."""
    completed_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    marker = {
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "turn_advanced_to": 99,
        "nodes_consolidated": 42,
    }
    marker_path = engram_home / "sessions" / "last-sleep-success.json"
    marker_path.write_text(json.dumps(marker))


# ============================================================================
# Positive matches — should fire the nudge
# ============================================================================

@pytest.mark.parametrize("phrase", [
    "good night",
    "Good Night Alex",
    "let's call it a day",
    "Time to call it a night",
    "let's call it",
    "wrapping up the day now",
    "Let me wrap up the session",
    "winding down for the night",
    "wind down for the day soon",
    "we're at the end of the day",
    "end of the session, finally",
    "I'm done for today",
    "done for tonight",
    "signing off, see you tomorrow",
    "turning in for the night",
    "ending for tonight, lei",
])
def test_positive_phrase_match_fires_nudge(phrase: str, engram_home: Path) -> None:
    rc, stdout = _run_hook(phrase, engram_home)
    assert rc == 0
    assert stdout.strip(), f"expected non-empty stdout for phrase: {phrase!r}"
    response = json.loads(stdout)
    assert "hookSpecificOutput" in response
    output = response["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    context = output["additionalContext"]
    assert "End-of-day detected" in context
    assert "engram-sleep" in context
    assert "Phase A" in context


# ============================================================================
# Negative matches — should NOT fire the nudge
# ============================================================================

@pytest.mark.parametrize("prompt", [
    "let's review the PR",
    "what's the test coverage?",
    "I need help with engram-bedtime later",
    "How does the dream cycle work?",
    "is this code path safe?",
    "we shipped 3 PRs today",
    "explain the cron removal",
])
def test_negative_no_match_no_nudge(prompt: str, engram_home: Path) -> None:
    rc, stdout = _run_hook(prompt, engram_home)
    assert rc == 0
    # Empty stdout (sys.exit(0) with no print) is the no-fire signal.
    assert stdout.strip() == "", f"expected empty stdout for non-EOD prompt: {prompt!r}"


# ============================================================================
# Documented false-positive shape — these phrases DO fire the hook even though
# they are not literal end-of-day signals. The hook deliberately delegates the
# judgment to the agent (via the injected context's "if the phrase is
# incidental, ignore this nudge" instruction). These tests document the
# intended false-positive behavior so future regex tightening doesn't silently
# break the agent-judgment contract.
# ============================================================================

@pytest.mark.parametrize("prompt,phrase_word", [
    ("let's call it a feature, not a bug", "let's call it"),
    ("signing off on this design", "signing off"),
])
def test_documented_false_positives_still_fire(
    prompt: str, phrase_word: str, engram_home: Path
) -> None:
    """Non-EOD phrases containing matched substrings still fire the hook.

    The injected context tells the agent these may be incidental — agent
    judgment handles surfacing-vs-ignoring. This is the documented contract.
    """
    rc, stdout = _run_hook(prompt, engram_home)
    assert rc == 0
    assert stdout.strip(), f"documented false-positive should still fire: {prompt!r}"
    response = json.loads(stdout)
    context = response["hookSpecificOutput"]["additionalContext"]
    # The context must explicitly tell the agent to use judgment.
    assert "ignore" in context.lower() or "judgment" in context.lower() or "incidental" in context.lower()


# ============================================================================
# Matched-phrase surfacing — the actual matched substring must appear in the
# injected context so the agent can read what triggered the nudge and judge
# whether it's a real signal. This is load-bearing for the false-positive
# contract above.
# ============================================================================

@pytest.mark.parametrize("prompt,expected_phrase", [
    ("good night", "good night"),
    ("Good Night Alex", "Good Night"),
    # Re-alternation note: "let's call it" appears earlier in the pattern
    # list than "call it a day"; re.search returns the leftmost-pattern
    # match at the leftmost-position, so "let's call it" wins here.
    ("let's call it a day now", "let's call it"),
    ("done for today", "done for today"),
    ("signing off, see you tomorrow", "signing off"),
])
def test_matched_phrase_surfaces_in_context(
    prompt: str, expected_phrase: str, engram_home: Path
) -> None:
    """Verify the matched phrase appears verbatim in the injected context.

    The agent uses the surfaced phrase to judge whether the nudge applies
    (e.g., 'signing off' could be 'signing off for the day' or 'signing off
    on this PR' — only the second is a false positive, but the agent needs
    to see the actual phrase to decide).
    """
    rc, stdout = _run_hook(prompt, engram_home)
    assert rc == 0
    assert stdout.strip()
    response = json.loads(stdout)
    context = response["hookSpecificOutput"]["additionalContext"]
    # The matched phrase appears verbatim (case-preserved) in the context.
    assert f'"{expected_phrase}"' in context, (
        f"expected matched phrase {expected_phrase!r} to appear in context "
        f"for prompt {prompt!r}; got: {context}"
    )


# ============================================================================
# Command-prompt skip — `/` or `!` prefixed prompts are commands, skip hook
# ============================================================================

@pytest.mark.parametrize("prompt", [
    "/engram-bedtime then /engram-sleep good night",
    "!echo good night",
    "/help good night",
])
def test_command_prompt_skipped(prompt: str, engram_home: Path) -> None:
    rc, stdout = _run_hook(prompt, engram_home)
    assert rc == 0
    assert stdout.strip() == "", f"command prompt should skip: {prompt!r}"


# ============================================================================
# Empty / whitespace-only prompt — skip
# ============================================================================

def test_empty_prompt_skipped(engram_home: Path) -> None:
    rc, stdout = _run_hook("", engram_home)
    assert rc == 0
    assert stdout.strip() == ""


def test_whitespace_only_prompt_skipped(engram_home: Path) -> None:
    rc, stdout = _run_hook("   \n\t  ", engram_home)
    assert rc == 0
    assert stdout.strip() == ""


# ============================================================================
# Sleep-marker freshness gate
# ============================================================================

def test_recent_sleep_suppresses_nudge(engram_home: Path) -> None:
    """If sleep completed within the last 4h, the nudge should be suppressed."""
    _write_sleep_marker(engram_home, hours_ago=1.0)
    rc, stdout = _run_hook("good night", engram_home)
    assert rc == 0
    assert stdout.strip() == "", "recent sleep should suppress nudge"


def test_old_sleep_does_not_suppress_nudge(engram_home: Path) -> None:
    """If sleep completed >4h ago, the nudge should fire (next-day case)."""
    _write_sleep_marker(engram_home, hours_ago=24.0)
    rc, stdout = _run_hook("call it a day", engram_home)
    assert rc == 0
    assert stdout.strip(), "old sleep should not suppress nudge"
    response = json.loads(stdout)
    assert "End-of-day detected" in response["hookSpecificOutput"]["additionalContext"]


def test_boundary_4h_suppresses(engram_home: Path) -> None:
    """4h is the suppression threshold (strict <); slightly under suppresses."""
    _write_sleep_marker(engram_home, hours_ago=3.99)
    rc, stdout = _run_hook("good night", engram_home)
    assert rc == 0
    assert stdout.strip() == ""


def test_boundary_just_over_4h_does_not_suppress(engram_home: Path) -> None:
    """Slightly over 4h does not suppress."""
    _write_sleep_marker(engram_home, hours_ago=4.01)
    rc, stdout = _run_hook("good night", engram_home)
    assert rc == 0
    assert stdout.strip()


def test_missing_marker_does_not_suppress(engram_home: Path) -> None:
    """No marker file (fresh install / never slept) → don't suppress."""
    # engram_home fixture creates no marker; verify nudge still fires.
    rc, stdout = _run_hook("good night", engram_home)
    assert rc == 0
    assert stdout.strip(), "missing marker should not suppress nudge"


def test_corrupt_marker_does_not_suppress(engram_home: Path) -> None:
    """Malformed marker JSON → suppression check fails safe (no suppression)."""
    marker_path = engram_home / "sessions" / "last-sleep-success.json"
    marker_path.write_text("not valid json {{{")
    rc, stdout = _run_hook("good night", engram_home)
    assert rc == 0
    assert stdout.strip(), "corrupt marker should not suppress nudge"


# ============================================================================
# stdin protocol — malformed input
# ============================================================================

def test_malformed_stdin_exits_nonzero(engram_home: Path) -> None:
    """Non-JSON stdin → exit 1 (non-blocking error)."""
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="not json at all",
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 1
    assert proc.stdout.strip() == ""


# ============================================================================
# Loop-wake guard (#2204): the agent's own loop-wake prompt is not the user
# ============================================================================

_WAKE_MARKER = "<loop-wake>"
# Text that would nudge if the guard were absent.
_EOD_TEXT = "good night, let's call it a day"


def test_loop_wake_prompt_is_silent(engram_home: Path) -> None:
    rc, stdout = _run_hook(f"{_WAKE_MARKER} Loop continuation. {_EOD_TEXT}", engram_home)
    assert rc == 0
    assert stdout.strip() == ""


def test_loop_wake_prompt_after_leading_whitespace_is_silent_pins_strip(engram_home: Path) -> None:
    rc, stdout = _run_hook(f"  \n\t{_WAKE_MARKER} {_EOD_TEXT}", engram_home)
    assert rc == 0
    assert stdout.strip() == ""


def test_same_text_without_marker_still_nudges(engram_home: Path) -> None:
    rc, stdout = _run_hook(f"Loop continuation. {_EOD_TEXT}", engram_home)
    assert rc == 0
    assert "End-of-day detected" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]


def test_marker_not_at_start_does_not_suppress(engram_home: Path) -> None:
    rc, stdout = _run_hook(f"please read this: {_WAKE_MARKER} {_EOD_TEXT}", engram_home)
    assert rc == 0
    assert "End-of-day detected" in json.loads(stdout)["hookSpecificOutput"]["additionalContext"]


def test_fallback_marker_equals_loop_prompt_marker(
    engram_home: Path, monkeypatch: pytest.MonkeyPatch, isolate_hook_loads
) -> None:
    import importlib.util

    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    spec = importlib.util.spec_from_file_location("eod_hook_under_test", HOOK_PATH)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    sys.path.insert(0, str(HOOK_PATH.parent.parent.parent / "tools"))  # restored by the window
    import loop_prompt

    assert hook._LOOP_WAKE_MARKER_FALLBACK == "<loop-wake>"
    assert hook._LOOP_WAKE_MARKER_FALLBACK == loop_prompt.LOOP_WAKE_MARKER
    assert hook._LOOP_WAKE_MARKER == loop_prompt.LOOP_WAKE_MARKER
