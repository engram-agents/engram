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

import os
import re
import socket
import subprocess
from pathlib import Path

AGENTCTL_MAC = Path(__file__).resolve().parent.parent / "tools" / "agentctl-mac"
AGENTCTL_LINUX = Path(__file__).resolve().parent.parent / "tools" / "agentctl"


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

def test_dry_run_no_flags_uses_a_detected_supported_platform():
    rc, out, _ = _run("session", "luria", "--dry-run")
    assert rc == 0
    assert out.split()[0] in ("claude", "codex")


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
    rc, out, _ = _run("session", "luria", "-drc", "--platform", "claude", "--dry-run")
    assert rc == 0
    assert "--dangerously-skip-permissions" in out
    assert "--remote-control" in out
    assert "luria" in out
    assert "--continue" in out


def test_agent_name_flows_into_remote_control():
    rc, out, _ = _run("session", "clio", "-r", "--platform", "claude", "--dry-run")
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


def test_codex_r_is_silently_consumed_for_compatibility():
    rc, out, err = _run("session", "luria", "-r", "--platform", "codex", "--dry-run")
    assert rc == 0
    assert out.split() == ["codex"]
    assert "-r" not in out
    assert "--remote-control" not in out
    assert not err


# ---------------------------------------------------------------------------
# --as-self / --no-share: must be true no-ops, never forwarded to the
# platform binary (regression test for a real bug found by reviewer-fairy
# review on #1834 -- these previously fell through to the passthrough
# default and ended up verbatim in the expanded command, which the real
# claude/codex binary would then reject as an unknown flag).
# ---------------------------------------------------------------------------

def test_as_self_is_absorbed_not_forwarded():
    rc, out, _ = _run("session", "luria", "--as-self", "--platform", "claude", "--dry-run")
    assert rc == 0
    assert out.split() == ["claude"]


def test_no_share_is_absorbed_not_forwarded():
    rc, out, _ = _run("session", "luria", "--no-share", "--platform", "claude", "--dry-run")
    assert rc == 0
    assert out.split() == ["claude"]


def test_as_self_and_no_share_compose_with_real_flags():
    rc, out, _ = _run("session", "luria", "--as-self", "-d", "--no-share", "--platform", "claude", "--dry-run")
    assert rc == 0
    words = out.split()
    assert words == ["claude", "--dangerously-skip-permissions"]


# ---------------------------------------------------------------------------
# --local: third and last flag in tools/agentctl's _parse_session_flags set.
# Same bug class as --as-self/--no-share (found by a second reviewer-fairy
# pass, via this repo's sibling-completeness-check convention) -- but the
# correct behavior here is NOT silent absorption, it's the same deprecated-
# no-op stderr notice tools/agentctl itself prints (tools/agentctl:303-304).
# ---------------------------------------------------------------------------

def test_local_absorbed_with_deprecation_notice():
    rc, out, err = _run("session", "luria", "--local", "--platform", "claude", "--dry-run")
    assert rc == 0
    assert out.split() == ["claude"]
    assert "deprecated" in err.lower()


def test_local_composes_with_real_flags():
    rc, out, err = _run("session", "luria", "--local", "-d", "--platform", "claude", "--dry-run")
    assert rc == 0
    assert out.split() == ["claude", "--dangerously-skip-permissions"]
    assert "deprecated" in err.lower()


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
    rc, out, _ = _run("session", "luria", "--cd", "/tmp", "--platform", "claude", "--dry-run")
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
    rc, out, _ = _run("session", "luria", "-d", "--platform", "claude", "--dry-run", "--", "--resume", "abc123")
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


# ---------------------------------------------------------------------------
# Completeness derivation: agentctl-mac must recognize every flag
# tools/agentctl's _parse_session_flags does, not just the ones a fairy
# happened to test. Three one-flag fixes in a row (#1834 -> #1836 --as-self/
# --no-share -> #1837 --local) is a pattern, not a coincidence -- each was
# found by a SEPARATE ad-hoc sweep. This test derives the required set from
# tools/agentctl's own source instead of hardcoding it, so a 7th upstream
# flag added later fails CI here rather than silently leaking to the
# platform binary the way the first three did.
#
# Deliberately NOT "reject unknown flags" -- the permissive passthrough is
# correct and load-bearing (claude/codex have their own flags this script
# has never heard of, e.g. --resume). The guard is narrower: every flag
# tools/agentctl's OWN flag namespace recognizes must also be recognized
# here (in a case-label sense -- absorbed, rejected, or otherwise handled --
# not necessarily identically), so that namespace can never silently drift
# out of sync again.
# ---------------------------------------------------------------------------

_CASE_LABEL_RE = re.compile(r'^\s*((?:--[a-z][a-z-]*(?:=\*)?\|?)+)\)', re.MULTILINE)


def _flags_from_case_labels(text: str) -> set[str]:
    """Extract the base --flag names (no `=*` suffix, `|`-bundles split) from
    every case-label line in `text`. Excludes the bare `--` separator and the
    `*)` catch-all (neither is a real flag name)."""
    flags: set[str] = set()
    for match in _CASE_LABEL_RE.finditer(text):
        for alt in match.group(1).split("|"):
            alt = alt.rstrip("*").rstrip("=")
            if alt and alt != "--":
                flags.add(alt)
    return flags


def _parse_session_flags_body() -> str:
    """Isolate just the _parse_session_flags function body from
    tools/agentctl -- the ONLY function whose flag namespace agentctl-mac
    needs to mirror (not the whole file's every case statement, which
    includes unrelated subcommands like health/share/spawn)."""
    text = AGENTCTL_LINUX.read_text()
    start = text.index("_parse_session_flags() {")
    end = text.index("\n}\n", start)
    return text[start:end]


def test_agentctl_mac_covers_every_parse_session_flags_flag():
    upstream_flags = _flags_from_case_labels(_parse_session_flags_body())
    # Sanity: fail loudly if the extraction itself is broken (e.g. tools/
    # agentctl's function signature changed shape) rather than silently
    # passing on an empty set.
    assert upstream_flags == {
        "--platform", "--local", "--no-share", "--as-self", "--cd", "--charge-file",
    }, f"extraction found an unexpected flag set: {upstream_flags!r}"

    mac_text = AGENTCTL_MAC.read_text()
    mac_flags = _flags_from_case_labels(mac_text)
    missing = upstream_flags - mac_flags
    assert not missing, (
        f"tools/agentctl-mac does not recognize {missing!r} from tools/agentctl's "
        "_parse_session_flags -- it will fall through to the passthrough default "
        "and leak verbatim to the platform binary, the exact bug class fixed in "
        "#1836/#1837. Add a case for it in tools/agentctl-mac (absorb, reject, or "
        "otherwise handle -- doesn't have to match tools/agentctl's own behavior "
        "exactly, just must not silently fall through)."
    )


# ---------------------------------------------------------------------------
# Codex managed daemon + one-TUI topology
#
# The Mac wrapper is single-user but must not make a separate local Codex
# app-server. These tests stub only Codex and tmux and exercise the wrapper's
# real Codex hand-off to agentctl's portable managed-session core.
# ---------------------------------------------------------------------------


def _make_unix_socket(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    return sock


def _write_mac_codex_stubs(tmp_path: Path) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    tmux_state = tmp_path / "tmux-running"
    (bin_dir / "codex").write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "{ printf 'codex'; printf ' %q' \"$@\"; printf '\\n'; } >> \"$FAKE_LOG\"\n"
        "if [[ \"${1:-}\" == app-server && \"${2:-}\" == daemon && \"${3:-}\" == start ]]; then exit \"${FAKE_START_RC:-0}\"; fi\n"
        "if [[ \"${1:-}\" == app-server && \"${2:-}\" == daemon && \"${3:-}\" == version ]]; then\n"
        "  if [[ \"${FAKE_VERSION_RC:-0}\" != 0 ]]; then exit \"$FAKE_VERSION_RC\"; fi\n"
        "  if [[ -n \"${FAKE_VERSION_OUTPUT+x}\" ]]; then printf '%s\\n' \"$FAKE_VERSION_OUTPUT\"; else printf '{\\\"socketPath\\\": \\\"%s\\\"}\\n' \"$FAKE_SOCKET\"; fi\n"
        "  exit 0\n"
        "fi\n"
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
        "  attach) exit 0 ;;\n"
        "  display-message) printf '%s\\n' \"${FAKE_TMUX_NAME:-other-session}\" ;;\n"
        "  *) printf 'unexpected fake tmux invocation\\n' >&2; exit 98 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    return bin_dir, log, tmux_state


def _run_managed_mac_codex(
    tmp_path: Path,
    *args: str,
    session_exists: bool = False,
    start_rc: int = 0,
    version_output: str | None = None,
    socket_is_real: bool = True,
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    socket_path = tmp_path / "app-server-control.sock"
    sock = _make_unix_socket(socket_path) if socket_is_real else None
    if not socket_is_real:
        socket_path.write_text("not a socket", encoding="utf-8")
    try:
        bin_dir, log, tmux_state = _write_mac_codex_stubs(tmp_path)
        if session_exists:
            tmux_state.touch()
        cwd = tmp_path / "project"
        cwd.mkdir()
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_LOG": str(log),
            "FAKE_SOCKET": str(socket_path),
            "FAKE_TMUX_STATE": str(tmux_state),
            "FAKE_START_RC": str(start_rc),
        }
        if version_output is not None:
            env["FAKE_VERSION_OUTPUT"] = version_output
        proc = subprocess.run(
            ["bash", str(AGENTCTL_MAC), "session", "luria", "--platform", "codex", "--cd", str(cwd), *args],
            capture_output=True,
            text=True,
            env=env,
        )
        return proc, log.read_text(encoding="utf-8") if log.exists() else "", cwd
    finally:
        if sock is not None:
            sock.close()


def test_mac_codex_ensures_verified_daemon_then_starts_remote_tui_in_cwd(tmp_path):
    proc, calls, cwd = _run_managed_mac_codex(tmp_path, "-c")
    assert proc.returncode == 0, proc.stderr
    lines = calls.splitlines()
    assert lines[:2] == ["codex app-server daemon start", "codex app-server daemon version"]
    new_session = next(line for line in lines if line.startswith("tmux new-session"))
    assert f"-c {cwd}" in new_session
    assert "codex\\ --remote\\ unix://" in new_session
    assert "resume\\ --last" in new_session
    assert "tmux attach -t agent-luria" in lines
    assert not any(line.startswith("codex --remote") for line in lines)


def test_mac_codex_reuses_existing_tui_by_attaching_without_second_session(tmp_path):
    proc, calls, _ = _run_managed_mac_codex(tmp_path, session_exists=True)
    assert proc.returncode == 0, proc.stderr
    assert "codex app-server daemon start" in calls
    assert "codex app-server daemon version" in calls
    assert "tmux new-session" not in calls
    assert "tmux attach -t agent-luria" in calls


def test_mac_codex_rejects_continue_against_existing_tui(tmp_path):
    proc, calls, _ = _run_managed_mac_codex(tmp_path, "-c", session_exists=True)
    assert proc.returncode != 0
    assert "already running; cannot apply new session/resume arguments" in proc.stderr
    assert "tmux new-session" not in calls
    assert "tmux attach" not in calls


def test_mac_codex_non_socket_daemon_report_fails_without_local_fallback(tmp_path):
    proc, calls, _ = _run_managed_mac_codex(tmp_path, socket_is_real=False)
    assert proc.returncode != 0
    assert "but it is not a Unix socket" in proc.stderr
    assert "codex app-server daemon start" in calls
    assert "codex app-server daemon version" in calls
    assert "tmux" not in calls
    assert "codex --remote" not in calls


def test_mac_codex_daemon_start_failure_never_launches_tui(tmp_path):
    proc, calls, _ = _run_managed_mac_codex(tmp_path, start_rc=1)
    assert proc.returncode != 0
    assert "could not ensure the managed Codex app-server" in proc.stderr
    assert calls.splitlines() == ["codex app-server daemon start"]
    assert "tmux" not in calls
