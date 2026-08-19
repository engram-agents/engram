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

import os
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
    """codex -c must transform the base command to 'codex resume --last'."""
    rc, lines, _ = _run("codex", "borges", "-c")
    assert rc == 0
    # First three words must be the subcommand transform.
    assert lines[0] == "codex"
    assert lines[1] == "resume"
    assert lines[2] == "--last"


def test_codex_c_d_combined_has_both_resume_and_bypass():
    """codex -c -d must produce resume --last AND the bypass flag."""
    rc, lines, _ = _run("codex", "borges", "-c", "-d")
    assert rc == 0
    assert "resume" in lines
    assert "--last" in lines
    assert "--dangerously-bypass-approvals-and-sandbox" in lines


def test_codex_r_is_silently_consumed_for_compatibility():
    """codex -r must not fail or reach Codex, because it has no equivalent remote-control flag."""
    rc, lines, stderr = _run("codex", "borges", "-r")
    assert rc == 0
    assert lines == ["codex"]
    assert "-r" not in lines
    assert "--remote-control" not in lines
    assert not stderr


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
    assert lines.count("--last") == 1


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
# Boot compatibility regression: historical callers pass `-dr` to every
# platform. Codex cannot use Claude's remote-control flag, so the expansion
# must retain -d while consuming -r without an error.
# ---------------------------------------------------------------------------

def test_codex_dr_preserves_bypass_while_dropping_remote_control():
    """The generic historic -dr boot alias must reach Codex as only its -d expansion."""
    rc, lines, stderr = _run("codex", "borges", "-dr")
    assert rc == 0, f"codex -dr must remain compatible (got rc={rc}, stderr={stderr!r})"
    assert lines == ["codex", "--dangerously-bypass-approvals-and-sandbox"]


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


def test_codex_rc_bundle_resumes_while_dropping_remote_control():
    """-rc keeps Codex's resume transform and silently consumes the Claude-only -r alias."""
    rc, lines, stderr = _run("codex", "borges", "-rc")
    assert rc == 0, f"expected compatible codex -rc, got rc={rc}, stderr={stderr!r}"
    assert lines == ["codex", "resume", "--last"]


def test_codex_rx_rejects_the_unrecognized_alias_after_consuming_r():
    """Codex -rx consumes -r but still rejects unrecognized -x with exit 6."""
    rc, lines, stderr = _run("codex", "borges", "-rx")
    assert rc == 6, f"expected exit 6 for unrecognized -x, got rc={rc}"
    assert not lines, f"expected no stdout, got: {lines}"
    assert "x" in stderr


def test_codex_xr_rejects_the_unrecognized_alias_after_consuming_r():
    """Codex -xr likewise rejects -x; bundle order does not hide the error."""
    rc, lines, stderr = _run("codex", "borges", "-xr")
    assert rc == 6, f"expected exit 6 for unrecognized -x, got rc={rc}"
    assert not lines, f"expected no stdout, got: {lines}"
    assert "x" in stderr


# ---------------------------------------------------------------------------
# --as-self: skip-sudo mode (systemd boot-time auto-start, no sudo available)
#
# cmd_session's --as-self flag skips the sudo/user-switch entirely, for use
# inside a systemd unit already running as the target agent's own Linux user
# (User=agent-<name>). resolve_user() requires a REAL Linux user to exist, so
# we cannot exercise cmd_session end-to-end without one; instead we test the
# extracted pure-function seams directly (same pattern as
# __expand-session-args and __validate-chosen-name above):
#   __as-self-check <target_user>   — the match/mismatch gate (_as_self_check)
#   __parse-session-flags [args...] — cmd_session's own flag parser
#                                      (_parse_session_flags), independent of
#                                      resolve_user and of the platform-alias
#                                      grammar tested above.
# ---------------------------------------------------------------------------

def _as_self_check(target: str) -> tuple[int, list[str], str]:
    proc = subprocess.run(
        ["bash", str(AGENTCTL), "__as-self-check", target],
        capture_output=True,
        text=True,
    )
    lines = [l for l in proc.stdout.splitlines() if l]
    return proc.returncode, lines, proc.stderr


def _parse_session_flags(*args: str) -> tuple[int, list[str], str]:
    proc = subprocess.run(
        ["bash", str(AGENTCTL), "__parse-session-flags", *args],
        capture_output=True,
        text=True,
    )
    lines = [l for l in proc.stdout.splitlines() if l]
    return proc.returncode, lines, proc.stderr


def test_as_self_check_matching_user_exits_zero_no_sudo_mentioned():
    """--as-self with the CURRENT OS user as target must exit 0 with no error output.

    This is the "matching user execs without sudo" case: _as_self_check finds
    id -un already equals the target, so cmd_session would proceed straight to
    `exec` — no `sudo -u ... ` invocation anywhere on this path. We assert
    that on the match path, no sudo-related text appears anywhere in stdout
    or stderr (there is nothing to report — a real discriminator, since a
    mutation that made the check always print something would be caught).
    """
    current_user = subprocess.run(
        ["id", "-un"], capture_output=True, text=True, check=True
    ).stdout.strip()
    rc, lines, stderr = _as_self_check(current_user)
    assert rc == 0, f"matching user must exit 0; got rc={rc}, stderr={stderr!r}"
    assert not lines, f"expected no stdout on the match path; got: {lines!r}"
    assert not stderr, f"expected no stderr on the match path; got: {stderr!r}"
    assert "sudo" not in (stderr + "".join(lines)).lower()


def test_as_self_check_mismatch_errors_loudly_naming_both_users():
    """--as-self with a mismatched target must error loudly, naming both users.

    Exit code must be nonzero and distinct from the alias-grammar codes (3-6)
    used by _expand_session_args — cmd_session maps this mismatch to exit 7.
    """
    current_user = subprocess.run(
        ["id", "-un"], capture_output=True, text=True, check=True
    ).stdout.strip()
    bogus_target = "definitely-not-a-real-agent-user-zzz"
    rc, lines, stderr = _as_self_check(bogus_target)
    assert rc == 1, f"_as_self_check must return 1 on mismatch; got rc={rc}"
    assert not lines, f"expected no stdout on the mismatch path; got: {lines!r}"
    assert bogus_target in stderr, (
        f"stderr must name the target user '{bogus_target}'; got: {stderr!r}"
    )
    assert current_user in stderr, (
        f"stderr must name the current user '{current_user}'; got: {stderr!r}"
    )


def test_as_self_flag_parses_true_and_defaults_no_share_false():
    """--as-self alone must set AS_SELF=1; _parse_session_flags itself does not
    force NO_SHARE (that forcing happens in cmd_session, one layer up) — this
    test pins the parser's own output so a mutation that silently drops or
    flips --as-self parsing is caught independent of that higher-level policy.
    """
    rc, lines, stderr = _parse_session_flags("--as-self")
    assert rc == 0, f"expected rc=0, got {rc}, stderr={stderr!r}"
    assert "AS_SELF=1" in lines, f"expected AS_SELF=1 in output; got: {lines!r}"
    assert "NO_SHARE=0" in lines, f"expected NO_SHARE=0 (parser-level default); got: {lines!r}"


def test_as_self_composes_with_platform_flag():
    """--as-self must compose with --platform without altering its parsed value."""
    rc, lines, _ = _parse_session_flags("--as-self", "--platform", "codex")
    assert rc == 0
    assert "AS_SELF=1" in lines
    assert "PLATFORM=codex" in lines


def test_as_self_composes_with_c_alias_passthrough():
    """--as-self must compose with the -c platform alias: -c is untouched by
    cmd_session's own flag parser and lands verbatim in the passthrough/extra
    array (to be expanded later by _expand_session_args), regardless of
    --as-self's presence or position.
    """
    rc, lines, _ = _parse_session_flags("--as-self", "-c")
    assert rc == 0
    assert "AS_SELF=1" in lines
    assert "EXTRA:-c" in lines, f"expected -c to pass through verbatim; got: {lines!r}"


def test_as_self_composes_with_cd_and_no_share_and_extra_args():
    """--as-self combined with --cd, --no-share, and a trailing passthrough arg
    must parse all four independently — no flag's value leaks into another's.
    """
    rc, lines, _ = _parse_session_flags(
        "--as-self", "--cd", "/tmp", "--no-share", "-d", "--resume", "abc123"
    )
    assert rc == 0
    assert "AS_SELF=1" in lines
    assert "CD=/tmp" in lines
    assert "NO_SHARE=1" in lines
    assert "EXTRA:-d" in lines
    assert "EXTRA:--resume" in lines
    assert "EXTRA:abc123" in lines


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


def test_charge_file_flag_parses_and_composes():
    """--charge-file must parse into CHARGE_FILE and compose with --as-self and
    alias passthrough. This is the 2026-07-25 boot-charge fix: the charge rides
    a dedicated flag (content appended to argv AFTER expansion) because the
    one-word-per-line expansion transport cannot carry multi-line text.
    """
    rc, lines, stderr = _parse_session_flags(
        "--as-self", "--charge-file", "/tmp/charge.md", "-dr"
    )
    assert rc == 0, f"expected rc=0, got {rc}, stderr={stderr!r}"
    assert "CHARGE_FILE=/tmp/charge.md" in lines, f"got: {lines!r}"
    assert "AS_SELF=1" in lines
    assert "EXTRA:-dr" in lines, f"-dr must still pass through to expansion; got: {lines!r}"


def test_charge_file_equals_form_parses():
    rc, lines, _ = _parse_session_flags("--charge-file=/x/y.md")
    assert rc == 0
    assert "CHARGE_FILE=/x/y.md" in lines, f"got: {lines!r}"


def test_charge_file_missing_argument_errors_loudly():
    rc, _, stderr = _parse_session_flags("--charge-file")
    assert rc == 2, f"missing arg must exit 2; got {rc}"
    assert "--charge-file" in stderr, f"error must name the flag; got: {stderr!r}"


def test_charge_file_default_is_empty():
    rc, lines, _ = _parse_session_flags("--as-self")
    assert rc == 0
    assert "CHARGE_FILE=" in lines, f"expected empty CHARGE_FILE default; got: {lines!r}"


# ---------------------------------------------------------------------------
# __detect-platform-as-self: no-sudo platform-detection seam
#
# tools/agentctl-mac (the macOS sibling) reuses this verb instead of
# duplicating the baked-profile/binary-heuristic ladder. It exercises
# _session_platform's as_self=1 branch directly -- no sudo, no real agent
# user (the `user` param is unused on that branch).
# ---------------------------------------------------------------------------

def _detect_platform_as_self(*args: str) -> tuple[int, list[str], str]:
    proc = subprocess.run(
        ["bash", str(AGENTCTL), "__detect-platform-as-self", *args],
        capture_output=True,
        text=True,
    )
    lines = [l for l in proc.stdout.splitlines() if l]
    return proc.returncode, lines, proc.stderr


def test_detect_platform_explicit_override_wins():
    rc, lines, _ = _detect_platform_as_self("codex")
    assert rc == 0
    assert lines == ["codex"]


def test_detect_platform_invalid_override_errors():
    rc, lines, stderr = _detect_platform_as_self("gemini")
    assert rc != 0, f"expected nonzero exit for invalid override, got rc={rc}"
    assert not lines, f"expected no stdout on error path, got: {lines}"
    assert "claude or codex" in stderr.lower(), f"got stderr: {stderr!r}"


def test_detect_platform_no_override_falls_back_to_default_or_heuristic():
    """With no override and (in this test environment) no baked profile at a
    bogus HOME, the ladder falls through to the binary heuristic / default.
    We don't assert a specific value (depends on what's on PATH in CI vs a
    real agent host) -- only that it's one of the two valid platforms and
    the call succeeds without sudo.
    """
    rc, lines, _ = _detect_platform_as_self()
    assert rc == 0
    assert lines and lines[0] in ("claude", "codex"), f"got: {lines!r}"

# ---------------------------------------------------------------------------
# Codex daemon-managed tmux topology
#
# The Codex path has a no-sudo internal seam. These tests substitute only the
# external codex/tmux binaries, then exercise the real shell helper end to end:
# it must ensure and verify the managed daemon before creating the sole tmux
# TUI, and it must never fall back to a direct local `codex` invocation.
# ---------------------------------------------------------------------------


def _write_daemon_stubs(tmp_path: Path, socket_path: Path) -> tuple[Path, Path, Path]:
    """Create fake codex/tmux binaries and return (bin_dir, log, tmux_state)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    state = tmp_path / "tmux-running"
    (bin_dir / "codex").write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "{ printf 'codex'; printf ' %q' \"$@\"; printf '\\n'; } >> \"$FAKE_LOG\"\n"
        "if [[ \"${1:-}\" == app-server && \"${2:-}\" == daemon && \"${3:-}\" == start ]]; then exit \"${FAKE_START_RC:-0}\"; fi\n"
        "if [[ \"${1:-}\" == app-server && \"${2:-}\" == daemon && \"${3:-}\" == version ]]; then\n"
        "  if [[ \"${FAKE_VERSION_RC:-0}\" != 0 ]]; then printf '%s\\n' \"${FAKE_VERSION_ERR:-Connection refused}\" >&2; exit \"$FAKE_VERSION_RC\"; fi\n"
        "  if [[ -n \"${FAKE_VERSION_OUTPUT+x}\" ]]; then printf '%s\\n' \"$FAKE_VERSION_OUTPUT\"; else printf '{\\\"socketPath\\\": \\\"%s\\\"}\\n' \"$FAKE_SOCKET\"; fi; exit 0\n"
        "fi\n"
        "if [[ \"${1:-}\" == --remote ]]; then printf '%s\\n' \"$PWD\" > \"$FAKE_CODEX_CWD\"; exit 0; fi\n"
        "printf 'unexpected fake codex invocation\\n' >&2; exit 99\n",
        encoding="utf-8",
    )
    (bin_dir / "tmux").write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "{ printf 'tmux'; printf ' %q' \"$@\"; printf '\\n'; } >> \"$FAKE_LOG\"\n"
        "case \"${1:-}\" in\n"
        "  has-session) test -f \"$FAKE_TMUX_STATE\" ;;\n"
        "  new-session) touch \"$FAKE_TMUX_STATE\" ;;\n"
        "  set-option) exit 0 ;;\n"
        "  attach) exit 0 ;;\n"
        "  display-message) printf '%s\\n' \"${FAKE_TMUX_NAME:-other-session}\" ;;\n"
        "  *) printf 'unexpected fake tmux invocation\\n' >&2; exit 98 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    return bin_dir, log, state


def _make_unix_socket(path: Path):
    """Bind a real UDS so the shell helper's -S validation is meaningful."""
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    return sock


def _run_managed_codex(
    tmp_path: Path,
    *args: str,
    session_exists: bool = False,
    start_rc: int = 0,
    version_rc: int = 0,
    version_output: str | None = None,
    socket_is_real: bool = True,
    inside_managed_tmux: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str]:
    socket_path = tmp_path / "app-server-control.sock"
    sock = _make_unix_socket(socket_path) if socket_is_real else None
    if not socket_is_real:
        socket_path.write_text("not a socket", encoding="utf-8")
    try:
        bin_dir, log, state = _write_daemon_stubs(tmp_path, socket_path)
        if session_exists:
            state.touch()
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_LOG": str(log),
            "FAKE_SOCKET": str(socket_path),
            "FAKE_TMUX_STATE": str(state),
            "FAKE_START_RC": str(start_rc),
            "FAKE_VERSION_RC": str(version_rc),
            "FAKE_CODEX_CWD": str(tmp_path / "codex-process-cwd"),
            **({"TMUX": "/tmp/fake", "FAKE_TMUX_NAME": "agent-mira"} if inside_managed_tmux else {}),
        }
        if version_output is not None:
            env["FAKE_VERSION_OUTPUT"] = version_output
        proc = subprocess.run(
            ["bash", str(AGENTCTL), "__codex-managed-session", "mira", "--cwd", str(tmp_path), "--", *args],
            capture_output=True,
            text=True,
            env=env,
        )
        return proc, log.read_text(encoding="utf-8") if log.exists() else ""
    finally:
        if sock is not None:
            sock.close()


def test_codex_managed_session_starts_daemon_then_one_remote_tmux_tui(tmp_path):
    proc, calls = _run_managed_codex(tmp_path, "--dangerously-bypass-approvals-and-sandbox")
    assert proc.returncode == 0, proc.stderr
    lines = calls.splitlines()
    assert lines[0] == "codex app-server daemon start"
    assert lines[1] == "codex app-server daemon version"
    new_session = next(line for line in lines if line.startswith("tmux new-session"))
    assert "codex\\ --remote\\ unix://" in new_session
    assert "--dangerously-bypass-approvals-and-sandbox" in new_session
    assert "tmux set-option -t agent-mira mouse on" in lines
    assert "tmux attach -t agent-mira" in lines
    assert not any(line.startswith("codex --remote") for line in lines), calls


def test_codex_managed_session_already_running_attaches_without_second_tui(tmp_path):
    proc, calls = _run_managed_codex(tmp_path, session_exists=True)
    assert proc.returncode == 0, proc.stderr
    assert "codex app-server daemon start" in calls
    assert "codex app-server daemon version" in calls
    assert "tmux new-session" not in calls
    assert "tmux set-option -t agent-mira mouse on" in calls
    assert "tmux attach -t agent-mira" in calls


def test_codex_managed_session_stale_or_unreachable_socket_fails_without_local_fallback(tmp_path):
    proc, calls = _run_managed_codex(tmp_path, version_rc=1)
    assert proc.returncode != 0
    assert "managed Codex app-server is unreachable" in proc.stderr
    assert "do not launch a local TUI" in proc.stderr
    assert "codex app-server daemon start" in calls
    assert "codex app-server daemon version" in calls
    assert "tmux" not in calls


def test_codex_managed_session_daemon_start_failure_has_no_local_fallback(tmp_path):
    proc, calls = _run_managed_codex(tmp_path, start_rc=1)
    assert proc.returncode != 0
    assert "could not ensure the managed Codex app-server" in proc.stderr
    assert "do not start a local Codex fallback" in proc.stderr
    assert calls.splitlines() == ["codex app-server daemon start"]
    assert "tmux" not in calls
    assert "codex --remote" not in calls


def test_codex_managed_session_malformed_or_missing_socket_path_has_no_local_fallback(tmp_path):
    for index, version_output in enumerate(("not JSON", "{}")):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        proc, calls = _run_managed_codex(case_dir, version_output=version_output)
        assert proc.returncode != 0
        assert "reported no usable socketPath" in proc.stderr
        assert "codex app-server daemon start" in calls
        assert "codex app-server daemon version" in calls
        assert "tmux" not in calls
        assert "codex --remote" not in calls


def test_codex_managed_session_non_socket_path_has_no_local_fallback(tmp_path):
    proc, calls = _run_managed_codex(tmp_path, socket_is_real=False)
    assert proc.returncode != 0
    assert "but it is not a Unix socket" in proc.stderr
    assert "Refusing to launch a separate local Codex TUI" in proc.stderr
    assert "codex app-server daemon start" in calls
    assert "codex app-server daemon version" in calls
    assert "tmux" not in calls
    assert "codex --remote" not in calls


def test_codex_managed_session_forwards_resume_arguments_into_remote_tui(tmp_path):
    proc, calls = _run_managed_codex(
        tmp_path,
        "resume",
        "--last",
        "--dangerously-bypass-approvals-and-sandbox",
    )
    assert proc.returncode == 0, proc.stderr
    new_session = next(line for line in calls.splitlines() if line.startswith("tmux new-session"))
    assert "codex\\ --remote\\ unix://" in new_session
    assert "resume\\ --last\\ --dangerously-bypass-approvals-and-sandbox" in new_session


def test_codex_managed_session_rejects_an_explicit_remote_override(tmp_path):
    proc, calls = _run_managed_codex(tmp_path, "--remote", "ws://127.0.0.1:9999")
    assert proc.returncode == 2
    assert "explicit --remote is not allowed" in proc.stderr
    assert "codex app-server" not in calls
    assert "tmux" not in calls


def test_codex_autostart_pane_reuses_its_managed_tmux_without_nesting(tmp_path):
    proc, calls = _run_managed_codex(
        tmp_path,
        "resume",
        "--last",
        inside_managed_tmux=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "tmux new-session" not in calls
    assert "tmux attach" not in calls
    assert "tmux display-message -p" in calls
    assert "tmux set-option -t agent-mira mouse on" in calls
    assert any(line.startswith("codex --remote unix://") and "resume --last" in line for line in calls.splitlines())
    assert (tmp_path / "codex-process-cwd").read_text(encoding="utf-8").strip() == str(tmp_path)


def test_codex_managed_session_rejects_new_resume_args_against_live_tui(tmp_path):
    proc, calls = _run_managed_codex(tmp_path, "resume", "--last", session_exists=True)
    assert proc.returncode != 0
    assert "already running; cannot apply new session/resume arguments" in proc.stderr
    assert "tmux new-session" not in calls
    assert "tmux attach" not in calls
