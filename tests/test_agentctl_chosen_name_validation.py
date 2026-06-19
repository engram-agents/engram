"""Tests for agentctl __validate-chosen-name — chosen-name normalization + validation.

Invokes `tools/agentctl __validate-chosen-name <chosen>` as a subprocess.
No sudo, no real users, no filesystem side effects — the verb exercises only
the lowercase-normalization and regex-validation logic extracted from
cmd_finalize_name.

Bug context: a public install submitted "Sol" (capitalized) and hit a hard
"not a valid Linux username" error. The fix auto-lowercases for the Linux
username while leaving the identity name (in ENGRAM / CLAUDE.md) untouched.

House rules (matching test_agentctl_session_args.py):
  - Every test is a real discriminator.
  - No patching of the function under test.
  - A mutation that breaks the normalization or validation also breaks the assert.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

AGENTCTL = Path(__file__).resolve().parent.parent / "tools" / "agentctl"


def _validate(chosen: str) -> tuple[int, list[str], str]:
    """Run agentctl __validate-chosen-name; return (exit_code, output_lines, stderr)."""
    proc = subprocess.run(
        ["bash", str(AGENTCTL), "__validate-chosen-name", chosen],
        capture_output=True,
        text=True,
    )
    lines = [l for l in proc.stdout.splitlines() if l]
    return proc.returncode, lines, proc.stderr


# ---------------------------------------------------------------------------
# Case-normalization: capitalized identity names become lowercase usernames
# ---------------------------------------------------------------------------

def test_capitalized_name_is_accepted_and_normalized():
    """'Sol' must be accepted (exit 0) and normalized to 'sol' on stdout.

    This is the exact failure case from the bug report: a public install
    submitted 'Sol' and hit a hard rejection.  After the fix, Sol is
    auto-lowercased to sol for the Linux username.
    """
    rc, lines, stderr = _validate("Sol")
    assert rc == 0, f"'Sol' must be accepted after normalization; got rc={rc}, stderr={stderr!r}"
    assert "sol" in lines, f"normalized name 'sol' must appear in stdout; got lines={lines!r}"


def test_capitalized_name_emits_informative_note():
    """When input is normalized, an informative Note line must appear before the name.

    The note explains that the POSIX username is lowercase while the identity
    name in ENGRAM is unaffected — this is important UX for the agent being named.
    """
    rc, lines, _ = _validate("Sol")
    assert rc == 0
    # The Note line must mention the normalized username and the identity name.
    note_lines = [l for l in lines if l.startswith("Note:")]
    assert note_lines, f"expected a 'Note:' line in stdout; got lines={lines!r}"
    note = note_lines[0]
    assert "agent-sol" in note, f"note must mention 'agent-sol'; got: {note!r}"
    assert "Sol" in note, f"note must preserve original identity name 'Sol'; got: {note!r}"
    assert "ENGRAM" in note, f"note must mention ENGRAM; got: {note!r}"


def test_capitalized_name_note_precedes_normalized_name():
    """The Note line must appear before the normalized name on stdout.

    The caller (or a human reading output) uses the last non-Note line as the
    normalized name; the note comes first so it can be visually distinguished.
    """
    rc, lines, _ = _validate("Sol")
    assert rc == 0
    note_idx = next((i for i, l in enumerate(lines) if l.startswith("Note:")), None)
    name_idx = next((i for i, l in enumerate(lines) if l == "sol"), None)
    assert note_idx is not None, "expected a Note line"
    assert name_idx is not None, "expected 'sol' in output"
    assert note_idx < name_idx, "Note must precede the normalized name"


# ---------------------------------------------------------------------------
# Already-lowercase names: pass through unchanged, no note
# ---------------------------------------------------------------------------

def test_lowercase_name_accepted_unchanged():
    """'ada' (already lowercase) must be accepted (exit 0) with 'ada' on stdout."""
    rc, lines, stderr = _validate("ada")
    assert rc == 0, f"'ada' must be accepted; got rc={rc}, stderr={stderr!r}"
    assert "ada" in lines, f"'ada' must appear in stdout; got lines={lines!r}"


def test_lowercase_name_emits_no_note():
    """'ada' must produce no Note line — normalization did not change the input."""
    rc, lines, _ = _validate("ada")
    assert rc == 0
    note_lines = [l for l in lines if l.startswith("Note:")]
    assert not note_lines, f"no Note expected for already-lowercase input; got: {note_lines!r}"


# ---------------------------------------------------------------------------
# Names that are un-fixable by lowercasing: must be rejected
# ---------------------------------------------------------------------------

def test_digit_start_rejected():
    """'3bot' starts with a digit; lowercasing cannot fix that — must exit 1.

    The regex ^[a-z][a-z0-9_-]*$ requires a letter-start.  '3bot' lowercases
    to '3bot' (unchanged) and still fails.
    """
    rc, lines, stderr = _validate("3bot")
    assert rc == 1, f"'3bot' must be rejected (exit 1); got rc={rc}"
    assert not lines, f"no stdout expected on rejection path; got: {lines!r}"
    assert "3bot" in stderr, f"stderr must name the offending input; got: {stderr!r}"


def test_digit_start_error_mentions_identity_separation():
    """Error for '3bot' must explain that the Linux-username constraint is separate from the identity name."""
    rc, _, stderr = _validate("3bot")
    assert rc == 1
    # The message should distinguish Linux username from identity name.
    assert "identity" in stderr.lower() or "separate" in stderr.lower(), (
        f"error must mention identity/separation; got: {stderr!r}"
    )


def test_space_in_name_rejected():
    """'a b' contains a space; must be rejected (exit 1) after lowercasing.

    Lowercasing 'a b' yields 'a b' — still fails ^[a-z][a-z0-9_-]*$.
    """
    rc, lines, stderr = _validate("a b")
    assert rc == 1, f"'a b' must be rejected (exit 1); got rc={rc}"
    assert not lines, f"no stdout expected on rejection path; got: {lines!r}"
    assert "a b" in stderr, f"stderr must name the offending input; got: {stderr!r}"


# ---------------------------------------------------------------------------
# Mixed-case names that normalize cleanly
# ---------------------------------------------------------------------------

def test_allcaps_name_normalized():
    """'ADA' normalizes to 'ada' — must be accepted with a note."""
    rc, lines, stderr = _validate("ADA")
    assert rc == 0, f"'ADA' must be accepted after normalization; got rc={rc}, stderr={stderr!r}"
    assert "ada" in lines, f"normalized 'ada' must appear in stdout; got: {lines!r}"
    note_lines = [l for l in lines if l.startswith("Note:")]
    assert note_lines, f"expected a Note line for 'ADA' normalization; got lines={lines!r}"


def test_mixed_case_name_normalized():
    """'ArIa' normalizes to 'aria' — must be accepted with a note."""
    rc, lines, _ = _validate("ArIa")
    assert rc == 0, f"'ArIa' must be accepted after normalization; got rc={rc}"
    assert "aria" in lines, f"normalized 'aria' must appear in stdout; got: {lines!r}"
