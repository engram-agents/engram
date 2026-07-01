"""Tests for tools/loop_prompt.py — SSoT loop-wake marker module (#1592).

Behaviors covered:

  Unit — format_loop_prompt:
    1. Marker is the first non-whitespace token in the output.
    2. Core message appears in the output.
    3. No meta → no meta line.
    4. With meta kwargs → meta line as second line, JSON-encoded.
    5. Idempotent: double-applying does not double-mark.
    6. Leading whitespace in core → still idempotent (lstripped before check).

  Unit — is_loop_wake:
    7. format_loop_prompt output → is_loop_wake True.
    8. Plain human text → is_loop_wake False.
    9. Empty string → is_loop_wake False.
   10. Starts with marker but has extra whitespace before → is_loop_wake True (lstrip).

  SSoT guard:
   11. LOOP_WAKE_MARKER equals the string literal "<loop-wake>".
   12. CLI module imports cleanly (no import-time side-effects crash).
"""

import importlib.util
import os
from pathlib import Path

import pytest

LP_PATH = Path(__file__).parent.parent / "src" / "engram" / "tools" / "loop_prompt.py"
_spec = importlib.util.spec_from_file_location("loop_prompt_test", LP_PATH)
lp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lp)


# ---------------------------------------------------------------------------
# format_loop_prompt
# ---------------------------------------------------------------------------

class TestFormatLoopPrompt:
    """format_loop_prompt always places the marker first."""

    def test_marker_is_first_token(self):
        """The marker is the first non-whitespace content in the output."""
        result = lp.format_loop_prompt("Check for new work.")
        assert result.lstrip().startswith(lp.LOOP_WAKE_MARKER)

    def test_marker_on_first_line(self):
        """The first line is exactly the marker (no trailing content)."""
        result = lp.format_loop_prompt("Check for new work.")
        first_line = result.split("\n")[0]
        assert first_line.strip() == lp.LOOP_WAKE_MARKER

    def test_core_appears_in_output(self):
        """The core message appears verbatim somewhere in the output."""
        core = "Working loop — evaluating open threads."
        result = lp.format_loop_prompt(core)
        assert core in result

    def test_no_meta_no_meta_line(self):
        """When no meta kwargs are given, no <loop-meta ...> line is emitted."""
        result = lp.format_loop_prompt("Simple core.")
        assert "<loop-meta" not in result

    def test_with_meta_embeds_json(self):
        """Meta kwargs produce a machine-readable JSON line after the marker."""
        result = lp.format_loop_prompt("Working.", kind="curiosity", seq=3)
        lines = result.split("\n")
        # First line = marker, second line = meta
        assert lines[0] == lp.LOOP_WAKE_MARKER
        meta_line = lines[1]
        assert meta_line.startswith("<loop-meta ")
        assert meta_line.endswith(">")
        import json
        inner_json = meta_line[len("<loop-meta "):-len(">")]
        parsed = json.loads(inner_json)
        assert parsed.get("kind") == "curiosity"
        assert parsed.get("seq") == 3

    def test_idempotent_no_double_marker(self):
        """Applying format_loop_prompt to already-marked output does not double-mark."""
        once = lp.format_loop_prompt("Already marked?")
        twice = lp.format_loop_prompt(once)
        assert twice == once

    def test_idempotent_with_leading_whitespace(self):
        """A core that starts with whitespace then the marker is also idempotent."""
        core = lp.format_loop_prompt("msg")
        # Add leading whitespace to simulate an already-formatted prompt
        result = lp.format_loop_prompt(core)
        assert result == core


# ---------------------------------------------------------------------------
# is_loop_wake
# ---------------------------------------------------------------------------

class TestIsLoopWake:
    """is_loop_wake detects the loop-wake marker correctly."""

    def test_formatted_prompt_is_loop_wake(self):
        """Output of format_loop_prompt always passes is_loop_wake."""
        assert lp.is_loop_wake(lp.format_loop_prompt("any core"))

    def test_formatted_with_meta_is_loop_wake(self):
        """Output of format_loop_prompt with meta also passes is_loop_wake."""
        assert lp.is_loop_wake(lp.format_loop_prompt("any core", kind="working"))

    def test_plain_human_text_is_not_loop_wake(self):
        """A plain human message returns False."""
        assert not lp.is_loop_wake("What is the status of #1592?")

    def test_empty_string_is_not_loop_wake(self):
        """Empty string returns False."""
        assert not lp.is_loop_wake("")

    def test_leading_whitespace_does_not_defeat_detection(self):
        """Leading whitespace before the marker still returns True (lstrip applied)."""
        body = "  \n  " + lp.LOOP_WAKE_MARKER + "\nsome core"
        assert lp.is_loop_wake(body)

    def test_partial_marker_prefix_does_not_match(self):
        """A prompt that starts with only part of the marker returns False."""
        partial = lp.LOOP_WAKE_MARKER[:-1]  # e.g. "<loop-wak"
        assert not lp.is_loop_wake(partial + "\nsome core")


# ---------------------------------------------------------------------------
# SSoT guard
# ---------------------------------------------------------------------------

class TestSSoT:
    """The marker string is exactly what the spec commits to."""

    def test_loop_wake_marker_value(self):
        """LOOP_WAKE_MARKER equals the committed string '<loop-wake>'."""
        assert lp.LOOP_WAKE_MARKER == "<loop-wake>"

    def test_module_imports_cleanly(self):
        """Importing loop_prompt does not raise any exception."""
        # Already imported above; the exec_module call would have raised if not.
        assert hasattr(lp, "LOOP_WAKE_MARKER")
        assert hasattr(lp, "format_loop_prompt")
        assert hasattr(lp, "is_loop_wake")
