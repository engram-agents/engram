"""Tests for agentctl __expand-session-args — alias expansion pure function.

Invokes `tools/agentctl __expand-session-args <platform> <agent> [args...]`
as a subprocess. No sudo, no agents, no filesystem side effects — the verb is
pure (no network, no disk, no real agent users required).

House rules (reviewer-enforced this week):
  - Every test is a real discriminator. No `or True`, no asserts over
    self-constructed values, no patching of the function under test.
  - Tests are designed so a mutation that breaks the actual expansion also
    breaks the assertion (i.e. a wrong expansion would produce wrong output
    that the test would catch).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

AGENTCTL = Path(__file__).resolve().parent.parent / "tools" / "agentctl"


def _run(platform: str, agent: str, *args: str) -> tuple[int, list[str], str]:
    """Run agentctl __expand-session-args; return (exit_code, output_lines, stderr)."""
    proc = subprocess.run(
        ["bash", str(AGENTCTL), "__expand-session-args", platform, agent, *args],
        capture_output=True,
        text=True,
    )
    lines = [l for l in proc.stdout.splitlines() if l]
    return proc.returncode, lines, proc.stderr


# ---------------------------------------------------------------------------
# claude alias expansions
# ---------------------------------------------------------------------------

def test_claude_d_expands_to_dangerously_skip_not_debug():
    """claude -d must become --dangerously-skip-permissions, NOT --debug, NOT bare -d."""
    rc, lines, _ = _run("claude", "borges", "-d")
    assert rc == 0
    assert "--dangerously-skip-permissions" in lines
    # Confirm the raw alias and the debug expansion are absent.
    assert "-d" not in lines
    assert "--debug" not in lines


def test_claude_r_expands_to_remote_control_with_agent_name():
    """claude -r must produce --remote-control followed immediately by the agent name."""
    rc, lines, _ = _run("claude", "borges", "-r")
    assert rc == 0
    assert "--remote-control" in lines
    rc_idx = lines.index("--remote-control")
    # Agent name must be the word immediately after --remote-control.
    assert lines[rc_idx + 1] == "borges"


def test_claude_c_expands_to_continue():
    """claude -c must expand to --continue."""
    rc, lines, _ = _run("claude", "borges", "-c")
    assert rc == 0
    assert "--continue" in lines
    assert "-c" not in lines


def test_claude_base_command_is_claude():
    """First output line for claude platform is 'claude'."""
    rc, lines, _ = _run("claude", "borges", "-c")
    assert rc == 0
    assert lines[0] == "claude"


# ---------------------------------------------------------------------------
# codex alias expansions
# ---------------------------------------------------------------------------

def test_codex_d_expands_to_bypass_approvals():
    """codex -d must become --dangerously-bypass-approvals-and-sandbox."""
    rc, lines, _ = _run("codex", "borges", "-d")
    assert rc == 0
    assert "--dangerously-bypass-approvals-and-sandbox" in lines
    assert "-d" not in lines
    # Must NOT produce the claude-specific flag.
    assert "--dangerously-skip-permissions" not in lines


def test_codex_c_subcommand_transform():
    """codex -c must transform the base command to 'codex resume --latest'."""
    rc, lines, _ = _run("codex", "borges", "-c")
    assert rc == 0
    # First three words must be the subcommand transform.
    assert lines[0] == "codex"
    assert lines[1] == "resume"
    assert lines[2] == "--latest"


def test_codex_c_d_combined_has_both_resume_and_bypass():
    """codex -c -d must produce resume --latest AND the bypass flag."""
    rc, lines, _ = _run("codex", "borges", "-c", "-d")
    assert rc == 0
    assert "resume" in lines
    assert "--latest" in lines
    assert "--dangerously-bypass-approvals-and-sandbox" in lines


def test_codex_r_returns_nonzero_and_stderr_mentions_remote():
    """codex -r must exit nonzero and mention remote control unavailability on stderr."""
    rc, lines, stderr = _run("codex", "borges", "-r")
    assert rc != 0
    assert "remote control" in stderr.lower()


# ---------------------------------------------------------------------------
# Pass-through ordering
# ---------------------------------------------------------------------------

def test_passthrough_order_preserved():
    """Non-alias args appear in their original relative order after expansion.

    Input: claude -d --resume abc123 -r
    Expected expansion (in order):
      claude, --dangerously-skip-permissions, --resume, abc123, --remote-control, borges
    The relative order of --resume/abc123 must be preserved between the alias
    expansions.
    """
    rc, lines, _ = _run("claude", "borges", "-d", "--resume", "abc123", "-r")
    assert rc == 0
    # Verify all expected words present.
    assert "--dangerously-skip-permissions" in lines
    assert "--resume" in lines
    assert "abc123" in lines
    assert "--remote-control" in lines
    assert "borges" in lines
    # Verify relative order of pass-through args is preserved.
    resume_idx = lines.index("--resume")
    abc_idx = lines.index("abc123")
    rc_idx = lines.index("--remote-control")
    assert resume_idx < abc_idx, "--resume must precede abc123"
    assert abc_idx < rc_idx, "abc123 must precede --remote-control"


# ---------------------------------------------------------------------------
# Alias dedupe
# ---------------------------------------------------------------------------

def test_claude_c_c_dedupe():
    """-c -c for claude must produce exactly one --continue, not two."""
    rc, lines, _ = _run("claude", "borges", "-c", "-c")
    assert rc == 0
    assert lines.count("--continue") == 1


def test_codex_c_c_dedupe_single_resume_transform():
    """-c -c for codex must produce exactly one resume subcommand transform."""
    rc, lines, _ = _run("codex", "borges", "-c", "-c")
    assert rc == 0
    assert lines.count("resume") == 1
    assert lines.count("--latest") == 1


# ---------------------------------------------------------------------------
# Unknown / invalid platform
# ---------------------------------------------------------------------------

def test_unknown_platform_exits_nonzero():
    """An unrecognized platform value must produce a nonzero exit code and NO stdout.

    _expand_session_args validates the platform; an unknown value exits 4.
    This guards against silent pass-through of an invalid platform string
    that would produce a nonsensical binary name at exec time.
    """
    rc, lines, stderr = _run("gemini", "borges", "-d")
    assert rc != 0, "An unknown platform must exit nonzero"
    # Stdout must be empty on the error path — no partial expansion emitted.
    assert not lines, f"Expected no stdout on error path, got: {lines}"


# ---------------------------------------------------------------------------
# -d dedup (suggestion 3)
# ---------------------------------------------------------------------------

def test_claude_d_d_dedup_emits_flag_once():
    """-d -d on claude must produce exactly one --dangerously-skip-permissions.

    Without the seen_d guard a second -d would emit the flag again, producing
    two occurrences that claude would misinterpret or reject.
    """
    rc, lines, _ = _run("claude", "borges", "-d", "-d")
    assert rc == 0
    assert lines.count("--dangerously-skip-permissions") == 1, (
        f"Expected exactly 1 occurrence, got: {lines.count('--dangerously-skip-permissions')}"
    )


def test_codex_d_d_dedup_emits_flag_once():
    """-d -d on codex must produce exactly one --dangerously-bypass-approvals-and-sandbox."""
    rc, lines, _ = _run("codex", "borges", "-d", "-d")
    assert rc == 0
    assert lines.count("--dangerously-bypass-approvals-and-sandbox") == 1, (
        f"Expected exactly 1 occurrence, got: {lines.count('--dangerously-bypass-approvals-and-sandbox')}"
    )


# ---------------------------------------------------------------------------
# Blocker regression: _expand_session_args exit-code propagation
#
# The blocker was that cmd_session used `mapfile < <(_expand_session_args ...)`
# which discards the sub-shell exit code — codex -r would print its error but
# execution would continue into a bare session.  The fix uses command
# substitution (`expanded_raw=$(...)`) which DOES propagate the exit code.
#
# We cannot end-to-end test cmd_session without sudo (resolve_user requires a
# real Linux user).  Instead we test the pure-function seam directly:
#   agentctl __expand-session-args codex <name> -r
# must exit 3 (the codex -r error path) with empty stdout.  This is the exact
# sub-call that cmd_session now captures via command substitution, so if the
# function exits nonzero the || { exit 2; } guard fires.
# ---------------------------------------------------------------------------

def test_codex_r_pure_function_exits_nonzero_empty_stdout():
    """codex -r via the pure __expand-session-args verb exits 3 with no stdout.

    This is the regression test for the process-substitution exit-swallow blocker.
    The plumbing guard (command-substitution + || exit 2) in cmd_session is
    covered by code inspection + the named-trap comment; this test validates the
    pure function's exit code so any mutation that makes it exit 0 is caught.
    """
    rc, lines, stderr = _run("codex", "borges", "-r")
    assert rc != 0, f"codex -r must exit nonzero (got rc={rc})"
    assert not lines, f"codex -r must produce no stdout (got: {lines})"
    assert "remote control" in stderr.lower(), (
        f"stderr must mention 'remote control' (got: {stderr!r})"
    )


# ---------------------------------------------------------------------------
# Blocker regression: echo flag-consumption (round-3)
#
# Original tests pinned that printf '%s\n' (not echo) was used for emission,
# because bash's echo consumes -n/-e/-E as its own flags and emits nothing.
# Under the bundled-flag grammar introduced in the bundled-flags PR (#835
# comment 2026-06-05), the single-dash namespace now belongs to agentctl
# aliases exclusively — no single-dash arg ever passes through raw, including
# -n and -e.  The old expectation (rc=0, -n in output) is OBSOLETE: these
# two tests are updated to assert loud failure (exit 6, unrecognized alias),
# which is the correct behaviour under the new grammar.
#
# The emission discipline (printf '%s\n' throughout) is still in effect and
# verified by the passthrough and bundle tests below; echo flag-consumption is
# simply no longer reachable via these args.
# ---------------------------------------------------------------------------

def test_claude_minus_n_fails_loudly():
    """-n for claude must fail loudly (exit 6) — n is not a recognized alias.

    Under the bundled-flag grammar, the single-dash namespace belongs to agentctl
    aliases exclusively.  -n was previously a pass-through (round-3); that
    expectation is now obsolete.  The correct behaviour is loud rejection.
    """
    rc, lines, stderr = _run("claude", "borges", "-n")
    assert rc == 6, f"expected exit 6 for unrecognized alias -n, got rc={rc}"
    assert not lines, f"expected no stdout on rejection path, got: {lines}"
    assert "n" in stderr, f"stderr must name the offending char (got: {stderr!r})"


def test_claude_minus_e_fails_loudly():
    """-e for claude must fail loudly (exit 6) — e is not a recognized alias.

    Under the bundled-flag grammar, the single-dash namespace belongs to agentctl
    aliases exclusively.  -e was previously a pass-through (round-3); that
    expectation is now obsolete.  The correct behaviour is loud rejection.
    """
    rc, lines, stderr = _run("claude", "borges", "-e")
    assert rc == 6, f"expected exit 6 for unrecognized alias -e, got rc={rc}"
    assert not lines, f"expected no stdout on rejection path, got: {lines}"
    assert "e" in stderr, f"stderr must name the offending char (got: {stderr!r})"


# ---------------------------------------------------------------------------
# Suggestion: loud rejection of empty and newline-containing args (round-3)
#
# The one-word-per-line protocol cannot safely carry empty args (command
# substitution strips trailing newlines, making --resume "" silently become
# --resume) or newline-containing args (they split into multiple words on the
# mapfile side).  _expand_session_args now rejects both loudly: exit 5 +
# stderr message naming the constraint.
# ---------------------------------------------------------------------------

def test_empty_arg_rejected_with_nonzero_exit_and_stderr():
    """An empty string arg must produce exit 5 and a stderr message mentioning 'empty'."""
    rc, lines, stderr = _run("claude", "borges", "")
    assert rc == 5, f"expected exit 5 for empty arg, got rc={rc}"
    assert not lines, f"expected no stdout on rejection path, got: {lines}"
    assert "empty" in stderr.lower(), (
        f"stderr must mention 'empty' (got: {stderr!r})"
    )


def test_newline_containing_arg_rejected_with_nonzero_exit_and_stderr():
    """An arg containing a newline must produce exit 5 and a stderr message mentioning 'newline'."""
    rc, lines, stderr = _run("claude", "borges", "foo\nbar")
    assert rc == 5, f"expected exit 5 for newline-containing arg, got rc={rc}"
    assert not lines, f"expected no stdout on rejection path, got: {lines}"
    assert "newline" in stderr.lower(), (
        f"stderr must mention 'newline' (got: {stderr!r})"
    )


# ---------------------------------------------------------------------------
# Bundled-flag grammar (PR #835 comment 2026-06-05)
#
# -xyz is a BUNDLE of single-char alias flags, expanded in char order.
# Any unrecognized char causes loud failure (exit 6) naming the char and arg.
# No single-dash arg ever passes through raw.
# Bare - (stdin) passes through as a non-flag arg.
# Double-dash flags pass through verbatim.
# ---------------------------------------------------------------------------

def test_claude_drc_bundle_expands_correctly():
    """-drc for claude expands to: --dangerously-skip-permissions --remote-control borges --continue.

    Chars are expanded in the order d → r → c.
    """
    rc, lines, _ = _run("claude", "borges", "-drc")
    assert rc == 0, f"expected rc=0, got {rc}"
    assert "--dangerously-skip-permissions" in lines
    assert "--remote-control" in lines
    assert "borges" in lines
    assert "--continue" in lines
    # Order: d-expansion before r-expansion before c-expansion.
    d_idx = lines.index("--dangerously-skip-permissions")
    r_idx = lines.index("--remote-control")
    c_idx = lines.index("--continue")
    assert d_idx < r_idx, "--dangerously-skip-permissions must precede --remote-control"
    assert r_idx < c_idx, "--remote-control must precede --continue"


def test_claude_rdc_bundle_expands_correctly():
    """-rdc for claude expands to: --remote-control borges --dangerously-skip-permissions --continue.

    Chars are expanded in the order r → d → c.
    """
    rc, lines, _ = _run("claude", "borges", "-rdc")
    assert rc == 0, f"expected rc=0, got {rc}"
    assert "--remote-control" in lines
    assert "borges" in lines
    assert "--dangerously-skip-permissions" in lines
    assert "--continue" in lines
    r_idx = lines.index("--remote-control")
    d_idx = lines.index("--dangerously-skip-permissions")
    c_idx = lines.index("--continue")
    assert r_idx < d_idx, "--remote-control must precede --dangerously-skip-permissions"
    assert d_idx < c_idx, "--dangerously-skip-permissions must precede --continue"


def test_claude_cd_bundle_expands_correctly():
    """-cd for claude expands to: --continue --dangerously-skip-permissions (c then d)."""
    rc, lines, _ = _run("claude", "borges", "-cd")
    assert rc == 0, f"expected rc=0, got {rc}"
    assert "--continue" in lines
    assert "--dangerously-skip-permissions" in lines
    c_idx = lines.index("--continue")
    d_idx = lines.index("--dangerously-skip-permissions")
    assert c_idx < d_idx, "--continue must precede --dangerously-skip-permissions"


def test_claude_dx_fails_loudly_naming_char():
    """-dx for claude must fail loudly (exit 6) naming the unrecognized char 'x' and the arg '-dx'."""
    rc, lines, stderr = _run("claude", "borges", "-dx")
    assert rc == 6, f"expected exit 6 for unrecognized alias in -dx, got rc={rc}"
    assert not lines, f"expected no stdout on rejection path, got: {lines}"
    assert "x" in stderr, f"stderr must name the offending char 'x' (got: {stderr!r})"
    assert "-dx" in stderr, f"stderr must name the original arg '-dx' (got: {stderr!r})"


def test_bare_dash_passes_through():
    """Bare - (stdin convention) must pass through verbatim as a non-flag arg."""
    rc, lines, _ = _run("claude", "borges", "-")
    assert rc == 0, f"expected rc=0 for bare -, got rc={rc}"
    assert "-" in lines, f"bare - must appear in output, got: {lines}"


def test_double_dash_flag_passes_through():
    """--debug (double-dash) must pass through verbatim for claude."""
    rc, lines, _ = _run("claude", "borges", "--debug")
    assert rc == 0, f"expected rc=0 for --debug, got rc={rc}"
    assert "--debug" in lines, f"--debug must appear in output, got: {lines}"


def test_codex_rc_bundle_exits_3_remote_control_error():
    """-rc for codex must exit 3 (remote control unavailable) and mention 'remote control' on stderr.

    -r in a bundle on codex is the same as standalone -r on codex: the platform
    does not support remote control.  Exit 3 takes precedence over exit 6 when
    both error conditions occur in a bundle.
    """
    rc, lines, stderr = _run("codex", "borges", "-rc")
    assert rc == 3, f"expected exit 3 for codex -rc, got rc={rc}"
    assert not lines, f"expected no stdout on codex -r error path, got: {lines}"
    assert "remote control" in stderr.lower(), (
        f"stderr must mention 'remote control' (got: {stderr!r})"
    )


def test_exit_3_takes_precedence_over_exit_6():
    """Codex -rx: codex -r (exit 3) takes precedence over unrecognized alias -x (exit 6)."""
    rc, lines, stderr = _run("codex", "borges", "-rx")
    assert rc == 3, f"expected exit 3 (codex -r precedence), got rc={rc}"
    assert not lines, f"expected no stdout, got: {lines}"
    assert "remote control" in stderr.lower(), (
        f"stderr must mention 'remote control' (got: {stderr!r})"
    )


def test_exit_3_takes_precedence_over_exit_6_reversed():
    """Codex -xr: codex -r (exit 3) takes precedence over unrecognized alias -x (exit 6)."""
    rc, lines, stderr = _run("codex", "borges", "-xr")
    assert rc == 3, f"expected exit 3 (codex -r precedence), got rc={rc}"
    assert not lines, f"expected no stdout, got: {lines}"
    assert "remote control" in stderr.lower(), (
        f"stderr must mention 'remote control' (got: {stderr!r})"
    )


def test_claude_r_r_dedup_emits_remote_control_once():
    """-r -r on claude must produce exactly one --remote-control <agent> pair.

    Without the seen_r guard a second -r would emit --remote-control borges again,
    producing two occurrences that claude would misinterpret.
    """
    rc, lines, _ = _run("claude", "borges", "-r", "-r")
    assert rc == 0
    assert lines.count("--remote-control") == 1, (
        f"Expected exactly 1 --remote-control, got: {lines.count('--remote-control')}"
    )
    assert lines.count("borges") == 1, (
        f"Expected exactly 1 agent name 'borges', got: {lines.count('borges')}"
    )
