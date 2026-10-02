"""Tests for tools/loop_prompt.py — SSoT loop-wake marker module (#1592).

Behaviors covered:

  Unit — format_loop_prompt:
    1. Marker is the first non-whitespace token in the output.
    2. Core message appears in the output.
    3. No meta → no meta line.
    4. With meta kwargs → meta line as second line, JSON-encoded.
    5. Idempotent: double-applying does not double-mark.
    6. Leading whitespace in core → still idempotent (lstripped before check).

  Unit — format_loop_prompt, already-marked input + meta kwargs (#3309):
   19. Already-marked, no existing meta line, + kind kwarg → meta line
       inserted as line 2; marker appears once; body preserved.
   20. Re-wrapping with a different kind → exactly one meta line, reflecting
       the new kind (replace, not merge).
   20b. Re-wrapping with different meta KEYS (not just a changed value for
       the same key) → old keys are dropped, not merged in.
   21. Already-marked + meta, body ending in "\n" → output ends in "\n".
   22. CLI path (subprocess): already-marked stdin + --kind → stdout
       carries the meta line (the issue's reproduction case).
   23. (ROUND 2) Re-wrapping an already-marked+meta'd prompt with a new
       kind, body ending in "\n" → full-string equality with the exact
       expected output (byte-for-byte ``rest`` preservation on the
       REPLACE-with-body return).
   24. (ROUND 3) Bare marker, no newline at all, + meta kwargs →
       full-string equality with the exact expected output (the
       "nothing else to preserve" return).
   25. (ROUND 3) Already-marked input whose only content after the marker
       is an existing meta line (no body at all) → re-wrapping replaces
       it; full-string equality with the exact expected output (the
       REPLACE-with-no-rest return).
   26. (ROUND 3, reviewer finding) Already-marked input with NO existing
       meta line + meta kwargs → full-string equality with the exact
       expected output (the INSERT return; items 19/21 above only check
       substrings/counts, not full equality, so this return was
       otherwise unpinned too).
   27. (#1847) Already-marked input WITH leading whitespace before the
       marker → the marker is still the first character of the result
       (leading whitespace is stripped before the idempotency check).
   28. (#1847) format_loop_prompt is idempotent even when the original
       core had leading whitespace before the marker.
   29. (#1847) Already-marked input with NO leading whitespace, body
       ending in "\n" → byte-identical output (also pins trailing
       whitespace preservation per the #3301 review, #3309).

  Unit — is_loop_wake:
    7. format_loop_prompt output → is_loop_wake True.
    8. Plain human text → is_loop_wake False.
    9. Empty string → is_loop_wake False.
   10. Starts with marker but has extra whitespace before → is_loop_wake True (lstrip).

  Unit — is_loop_wake HTML-entity normalization (gh#1646, Layer 2):
   13. HTML-escaped marker (&lt;loop-wake&gt;) classifies as loop-wake (the
       incident case — an agent hand-constructed the escaped literal).
   14. Plain marker still classifies (unchanged behavior).
   15. Leading whitespace + escaped marker still classifies.
   16. Plain human text is not misclassified as loop-wake.
   17. Empty string is not loop-wake.

  SSoT guard:
   11. LOOP_WAKE_MARKER equals the string literal "<loop-wake>".
   12. CLI module imports cleanly (no import-time side-effects crash).
   18. The deference-detector hook's fallback constant
       (_LOOP_WAKE_MARKER_FALLBACK) equals loop_prompt.LOOP_WAKE_MARKER, so
       the two can't silently diverge.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

LP_PATH = Path(__file__).parent.parent / "src" / "engram" / "tools" / "loop_prompt.py"
_spec = importlib.util.spec_from_file_location("loop_prompt_test", LP_PATH)
lp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lp)

DEFERENCE_HOOK_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "engram"
    / "hooks"
    / "claude"
    / "engram-deference-detector-prompt.py"
)
_dd_spec = importlib.util.spec_from_file_location(
    "deference_detector_prompt_test", DEFERENCE_HOOK_PATH
)
dd = importlib.util.module_from_spec(_dd_spec)
_dd_spec.loader.exec_module(dd)


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
        core = "  \n" + lp.format_loop_prompt("msg")
        result = lp.format_loop_prompt(core)
        assert result == core.lstrip()
        assert result.count(lp.LOOP_WAKE_MARKER) == 1

    def test_leading_whitespace_before_marker_is_stripped(self):
        """An already-marked core with leading whitespace returns the marker
        as the first character — leading whitespace does not stay in front
        of the marker (#1847)."""
        core = "  \n<loop-wake>\nbody"
        result = lp.format_loop_prompt(core)
        assert result[0] == "<"
        assert result.count(lp.LOOP_WAKE_MARKER) == 1
        assert result == "<loop-wake>\nbody"

    def test_idempotent_on_the_stripped_result(self):
        """format_loop_prompt is idempotent even when the input core had
        leading whitespace before the marker (#1847)."""
        once = lp.format_loop_prompt("  \n<loop-wake>\nbody")
        twice = lp.format_loop_prompt(once)
        assert once == twice == "<loop-wake>\nbody"

    def test_already_marked_no_leading_whitespace_is_byte_identical(self):
        """An already-marked core with NO leading whitespace returns
        byte-identical (unchanged behavior; #1847). Body ends in "\\n" to pin
        trailing whitespace (#3309, #3301 review) — a future
        ``return core.strip()`` in the idempotent no-meta branch would strip
        this trailing newline and must fail this test."""
        core = lp.LOOP_WAKE_MARKER + "\nbody\n"
        assert lp.format_loop_prompt(core) == core

    def test_already_marked_with_meta_kwargs_inserts_meta_line(self):
        """(a) Already-marked input with no existing meta line + a meta
        kwarg → line 2 of the result is the new meta line; the marker
        appears exactly once; the body is preserved (#3309)."""
        core = lp.LOOP_WAKE_MARKER + "\nbody"
        result = lp.format_loop_prompt(core, kind="working")
        lines = result.split("\n")
        assert lines[0] == lp.LOOP_WAKE_MARKER
        assert lines[1] == '<loop-meta {"kind":"working"}>'
        assert result.count(lp.LOOP_WAKE_MARKER) == 1
        assert "body" in result

    def test_rewrap_with_different_kind_replaces_meta_line(self):
        """(b) Re-wrapping an already-marked+meta'd prompt with a new
        ``kind`` produces exactly one meta line, reflecting the NEW kind
        (not the old one) — the old meta line is replaced, not kept
        alongside the new one (#3309)."""
        once = lp.format_loop_prompt("core", kind="working")
        twice = lp.format_loop_prompt(once, kind="sleeping")
        meta_lines = [line for line in twice.split("\n") if line.startswith(lp._META_PREFIX)]
        assert len(meta_lines) == 1
        parsed = json.loads(meta_lines[0][len(lp._META_PREFIX):-len(lp._META_SUFFIX)])
        assert parsed.get("kind") == "sleeping"

    def test_rewrap_with_different_meta_keys_drops_old_keys(self):
        """(b)-extra: a merge-not-replace bug (M3) would keep the old key
        alongside the new one. Use fixtures with DIFFERENT keys so a merge
        is distinguishable from a same-key overwrite: old meta has "seq",
        new meta has "kind" — the result must carry only "kind" (#3309)."""
        once = lp.format_loop_prompt("core", seq=3)
        twice = lp.format_loop_prompt(once, kind="x")
        meta_lines = [line for line in twice.split("\n") if line.startswith(lp._META_PREFIX)]
        assert len(meta_lines) == 1
        parsed = json.loads(meta_lines[0][len(lp._META_PREFIX):-len(lp._META_SUFFIX)])
        assert parsed == {"kind": "x"}
        assert "seq" not in parsed

    def test_already_marked_with_meta_preserves_trailing_newline(self):
        """(c) Already-marked input + meta kwargs, body ending in "\\n" →
        the meta line is present (today it is silently dropped — this is
        what makes the test red on dev) AND the output still ends in "\\n"
        (trailing whitespace after the body is preserved byte-for-byte,
        same as branch 2) (#3309)."""
        core = lp.LOOP_WAKE_MARKER + "\nbody\n"
        result = lp.format_loop_prompt(core, kind="working")
        assert "<loop-meta " in result
        assert result.endswith("\n")

    def test_cli_already_marked_input_with_kind_emits_meta_line(self):
        """(d) CLI path: piping an already-marked prompt back through
        ``--kind`` must not silently drop the meta line (#3309, the issue's
        reproduction case). Exercises the real subprocess entrypoint, not
        just the importlib-loaded module object, so the CLI's stdin→stdout
        wiring is covered too."""
        result = subprocess.run(
            [sys.executable, str(LP_PATH), "--kind", "working"],
            input="<loop-wake>\nbody\n",
            capture_output=True,
            text=True,
            check=True,
        )
        assert "<loop-meta " in result.stdout
        assert '"kind":"working"' in result.stdout

    def test_rewrap_replace_meta_preserves_trailing_newline_exact(self):
        """(ROUND 2) Already-marked input that HAS an existing meta line,
        body ending in "\\n", re-wrapped with a new ``kind`` → the result
        equals the expected string EXACTLY (full-string equality, not just
        ``endswith``). Pins the REPLACE path's byte-for-byte preservation of
        ``rest`` — a ``rest.rstrip()`` mutation on that path would drop the
        trailing newline and fail this test (#3309 round 2)."""
        once = lp.format_loop_prompt("body\n", kind="working")
        twice = lp.format_loop_prompt(once, kind="sleeping")
        meta_line = (
            f"{lp._META_PREFIX}"
            f"{json.dumps({'kind': 'sleeping'}, separators=(',', ':'))}"
            f"{lp._META_SUFFIX}"
        )
        expected = lp.LOOP_WAKE_MARKER + "\n" + meta_line + "\nbody\n"
        assert twice == expected

    def test_bare_marker_no_newline_with_meta_full_equality(self):
        """(ROUND 3) A bare marker with no newline at all (nothing after it
        to preserve), + meta kwargs → the result equals the expected string
        EXACTLY. Pins the "nothing else to preserve" return
        (``return stripped_core + "\\n" + meta_line``) — a mutation that
        appends an extra trailing "\\n" to that return leaves every other
        test in this file green, because no existing test exercises the
        bare-marker-no-newline input at all (#3309 round 3)."""
        meta_line = (
            f"{lp._META_PREFIX}"
            f"{json.dumps({'kind': 'working'}, separators=(',', ':'))}"
            f"{lp._META_SUFFIX}"
        )
        result = lp.format_loop_prompt(lp.LOOP_WAKE_MARKER, kind="working")
        expected = lp.LOOP_WAKE_MARKER + "\n" + meta_line
        assert result == expected

    def test_rewrap_replace_meta_no_body_full_equality(self):
        """(ROUND 3) Already-marked input whose ONLY content after the
        marker is an existing meta line — no body at all. Re-wrapping with
        a new ``kind`` replaces the meta line; the result equals the
        expected string EXACTLY. Pins the REPLACE-with-no-``rest`` return
        (``return marker_line + "\\n" + meta_line``) — a mutation that
        appends an extra trailing "\\n" to that return leaves every other
        test in this file green, because no existing test constructs an
        already-marked+meta input with nothing after the meta line
        (#3309 round 3)."""
        full = lp.format_loop_prompt("x", kind="a")
        marker_line, meta_line_a, _rest = full.split("\n", 2)
        truncated = marker_line + "\n" + meta_line_a  # marker + meta line, no trailing text
        result = lp.format_loop_prompt(truncated, kind="b")
        meta_line_b = (
            f"{lp._META_PREFIX}"
            f"{json.dumps({'kind': 'b'}, separators=(',', ':'))}"
            f"{lp._META_SUFFIX}"
        )
        expected = lp.LOOP_WAKE_MARKER + "\n" + meta_line_b
        assert result == expected

    def test_already_marked_insert_meta_full_equality(self):
        """(ROUND 3, reviewer finding) Already-marked input with NO
        existing meta line + meta kwargs → the result equals the expected
        string EXACTLY. Pins the INSERT return
        (``return marker_line + "\\n" + meta_line + "\\n" + after_marker``)
        — tests (a)/(c) above (``test_already_marked_with_meta_kwargs_inserts_meta_line``,
        ``test_already_marked_with_meta_preserves_trailing_newline``) only
        check substrings/line-indices/counts, not full-string equality, so
        a mutation that appends an extra trailing "\\n" to this return
        leaves all 30 pre-round-3 tests green (#3309 round 3)."""
        meta_line = (
            f"{lp._META_PREFIX}"
            f"{json.dumps({'kind': 'working'}, separators=(',', ':'))}"
            f"{lp._META_SUFFIX}"
        )
        core = lp.LOOP_WAKE_MARKER + "\nbody"
        result = lp.format_loop_prompt(core, kind="working")
        expected = lp.LOOP_WAKE_MARKER + "\n" + meta_line + "\nbody"
        assert result == expected


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
# is_loop_wake — HTML-entity normalization (gh#1646, Layer 2)
# ---------------------------------------------------------------------------

class TestIsLoopWakeHtmlEntityNormalization:
    """is_loop_wake must classify identically whether the marker arrives
    plain or HTML-escaped, so an agent-construction mistake (writing
    &lt;loop-wake&gt; instead of real angle brackets into a ScheduleWakeup
    prompt) can't silently defeat detection."""

    def test_escaped_marker_is_loop_wake(self):
        """The incident case: &lt;loop-wake&gt; classifies as loop-wake."""
        assert lp.is_loop_wake("&lt;loop-wake&gt;\n...core...")

    def test_plain_marker_is_loop_wake(self):
        """Plain marker (unchanged behavior) still classifies as loop-wake."""
        assert lp.is_loop_wake("<loop-wake>\n...core...")

    def test_leading_whitespace_plus_escaped_marker_is_loop_wake(self):
        """Leading whitespace + escaped marker still classifies."""
        assert lp.is_loop_wake("  &lt;loop-wake&gt; x")

    def test_normal_human_prompt_is_not_loop_wake(self):
        """A normal human prompt is not misclassified as loop-wake."""
        assert not lp.is_loop_wake("a normal human prompt")

    def test_empty_string_is_not_loop_wake(self):
        """Empty string returns False."""
        assert not lp.is_loop_wake("")


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

    def test_deference_hook_fallback_matches_ssot(self):
        """The deference-detector hook's fallback constant
        (_LOOP_WAKE_MARKER_FALLBACK) must equal loop_prompt.LOOP_WAKE_MARKER
        so the two can't silently diverge (#1594)."""
        assert dd._LOOP_WAKE_MARKER_FALLBACK == lp.LOOP_WAKE_MARKER
