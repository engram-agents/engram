"""Tests for tools/agentctl-mac -- the lightweight macOS sibling of
tools/agentctl's `session` subcommand.

Runs against the REAL script via subprocess (--dry-run mode, so nothing
actually execs claude/codex). This deliberately does NOT mock or stub the
shared verbs it calls into (tools/agentctl's __expand-session-args /
__detect-platform-as-self) -- the whole point of this script is that it
reuses those verbs directly, so a real subprocess run is the only way to
catch an integration break between the two scripts (e.g. the exact bash-3.2
nameref/mapfile/empty-array incompatibilities found and fixed while building
this: see the header comment in tools/agentctl-mac).

House rules (same as test_agentctl_session_args.py): every test is a real
discriminator, no `or True`, no asserts over self-constructed values.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

AGENTCTL_MAC = Path(__file__).resolve().parent.parent / "tools" / "agentctl-mac"


def _run(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["bash", str(AGENTCTL_MAC), *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

def test_missing_subcommand_errors():
    rc, out, err = _run()
    assert rc == 2
    assert not out
    assert "session" in err.lower()


def test_unknown_subcommand_errors():
    rc, out, err = _run("health")
    assert rc == 2
    assert not out


def test_missing_agent_name_errors():
    rc, out, err = _run("session")
    assert rc == 2
    assert not out
    assert "agent name" in err.lower()


# ---------------------------------------------------------------------------
# Dry-run: base command + platform default
# ---------------------------------------------------------------------------

def test_dry_run_no_flags_defaults_to_claude():
    rc, out, _ = _run("session", "luria", "--dry-run")
    assert rc == 0
    assert out.split()[0] == "claude"


def test_dry_run_explicit_codex_platform():
    rc, out, _ = _run("session", "luria", "--platform", "codex", "--dry-run")
    assert rc == 0
    assert out.split()[0] == "codex"


def test_dry_run_platform_equals_form():
    rc, out, _ = _run("session", "luria", "--platform=codex", "--dry-run")
    assert rc == 0
    assert out.split()[0] == "codex"


def test_invalid_platform_override_errors():
    rc, out, err = _run("session", "luria", "--platform", "gemini", "--dry-run")
    assert rc != 0
    assert not out
    assert "claude or codex" in err.lower()


# ---------------------------------------------------------------------------
# Alias-bundle expansion (delegated to tools/agentctl __expand-session-args --
# these pin that the delegation actually happens end-to-end, not just that
# the standalone verb works in isolation)
# ---------------------------------------------------------------------------

def test_drc_bundle_expands_all_three():
    rc, out, _ = _run("session", "luria", "-drc", "--dry-run")
    assert rc == 0
    assert "--dangerously-skip-permissions" in out
    assert "--remote-control" in out
    assert "luria" in out
    assert "--continue" in out


def test_agent_name_flows_into_remote_control():
    rc, out, _ = _run("session", "clio", "-r", "--dry-run")
    assert rc == 0
    assert "--remote-control" in out
    words = out.split()
    idx = words.index("--remote-control")
    assert words[idx + 1] == "clio"


def test_codex_d_expands_to_bypass_approvals():
    rc, out, _ = _run("session", "luria", "-d", "--platform", "codex", "--dry-run")
    assert rc == 0
    assert "--dangerously-bypass-approvals-and-sandbox" in out
    assert "--dangerously-skip-permissions" not in out


def test_codex_r_errors_nonzero():
    rc, out, err = _run("session", "luria", "-r", "--platform", "codex", "--dry-run")
    assert rc != 0
    assert not out
    assert "remote control" in err.lower()


def test_unrecognized_alias_char_errors_loudly():
    rc, out, err = _run("session", "luria", "-x", "--dry-run")
    assert rc != 0
    assert not out
    assert "x" in err


# ---------------------------------------------------------------------------
# --charge-file: explicitly rejected (not silently ignored)
# ---------------------------------------------------------------------------

def test_charge_file_rejected_loudly():
    rc, out, err = _run("session", "luria", "--charge-file", "/tmp/x.md", "--dry-run")
    assert rc != 0
    assert not out
    assert "charge-file" in err.lower()
    assert "not supported" in err.lower()


def test_charge_file_equals_form_also_rejected():
    rc, out, err = _run("session", "luria", "--charge-file=/tmp/x.md", "--dry-run")
    assert rc != 0
    assert not out
    assert "charge-file" in err.lower()


# ---------------------------------------------------------------------------
# --cd
# ---------------------------------------------------------------------------

def test_cd_nonexistent_dir_errors():
    rc, out, err = _run("session", "luria", "--cd", "/definitely/not/a/real/dir", "--dry-run")
    assert rc != 0
    assert "does not exist" in err.lower() or "no such file" in err.lower()


def test_cd_real_dir_succeeds():
    rc, out, _ = _run("session", "luria", "--cd", "/tmp", "--dry-run")
    assert rc == 0
    assert out.split()[0] == "claude"


# ---------------------------------------------------------------------------
# -- passthrough boundary: agentctl-mac's own flags (--dry-run, --platform,
# --cd, --charge-file) stop being parsed once a literal `--` is seen; the one
# exception under test here is --dry-run itself, which necessarily must come
# BEFORE `--` (there is no way to request dry-run output for something that
# by definition never executes past the boundary you're testing).
# ---------------------------------------------------------------------------

def test_passthrough_args_after_dashdash_land_verbatim():
    rc, out, _ = _run("session", "luria", "-d", "--dry-run", "--", "--resume", "abc123")
    assert rc == 0
    words = out.split()
    assert "--dangerously-skip-permissions" in words
    assert "--resume" in words
    assert "abc123" in words


def test_dashdash_separator_itself_not_forwarded():
    """The literal `--` token must be consumed as a separator, never appear
    in the expanded output (matches tools/agentctl's own _parse_session_flags
    behavior: a leading -- is shifted off, not passed through)."""
    rc, out, _ = _run("session", "luria", "--dry-run", "--", "--resume", "abc123")
    assert rc == 0
    assert "--" not in out.split()


def test_dry_run_after_dashdash_is_passthrough_not_intercepted():
    """A --dry-run appearing AFTER the -- boundary is passthrough content for
    the platform binary, not agentctl-mac's own flag -- so it must actually
    execute (or at least not be silently swallowed as if it were the real
    --dry-run flag). We can't safely let it exec claude in a test, so this
    is asserted indirectly: real execution with a nonexistent PATH binary
    would fail differently than dry-run's clean zero-exit print path.  Here
    we just confirm the word survives into the pre-exec argument list by
    combining with a real --dry-run BEFORE the boundary and checking both
    --dry-run instances didn't collapse into one no-op parse (the earlier
    one still produced dry-run output rather than an exec attempt).
    """
    rc, out, _ = _run("session", "luria", "--dry-run", "--", "--dry-run", "--resume", "x")
    assert rc == 0
    # The first --dry-run (ours) fires -> clean printed line, exit 0.
    # The second --dry-run (passthrough) must appear verbatim in that output.
    words = out.split()
    assert "--dry-run" in words
    assert "--resume" in words
    assert "x" in words
