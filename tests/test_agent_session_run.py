"""Integration regressions for the in-tmux autostart runner.

The runner invokes the installed ``agentctl`` command, so these tests replace
only that external executable and assert the argv and selected charge the real
runner supplies. No tmux, systemd, daemon, or live platform process is started.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "deploy" / "autostart" / "agent-session-run.sh"
CHARGE_SOURCE = REPO_ROOT / "deploy" / "autostart"


def _run_runner(
    tmp_path: Path,
    platform: str,
    *,
    legacy_override: str | None = None,
    explicit_override: str | None = None,
    profile: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str], str, Path, Path]:
    """Run the real runner and return its argv plus the consumed charge text."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "session-argv.nul"
    charge_content = tmp_path / "selected-charge.md"
    home = tmp_path / "home"
    home.mkdir()
    engram_home = home / ".engram"
    engram_home.mkdir()
    if legacy_override is not None:
        (engram_home / "boot-charge.md").write_text(legacy_override, encoding="utf-8")
    if explicit_override is not None:
        (engram_home / f"boot-charge-{platform}.md").write_text(
            explicit_override, encoding="utf-8"
        )
    if profile is not None:
        (engram_home / "boot-charge-profile").write_text(profile, encoding="utf-8")
    cwd = tmp_path / "project"
    cwd.mkdir()

    agentctl = bin_dir / "agentctl"
    agentctl.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "if [[ \"${1:-}\" == __detect-platform-as-self ]]; then printf '%s\\n' \"$FAKE_PLATFORM\"; exit 0; fi\n"
        "if [[ \"${1:-}\" == session ]]; then\n"
        "  printf '%s\\0' \"$@\" > \"$FAKE_CALLS\"\n"
        "  charge=\"${@: -1}\"\n"
        "  cp \"$charge\" \"$FAKE_CHARGE_CONTENT\"\n"
        "  exit 0\n"
        "fi\n"
        "printf 'unexpected agentctl invocation: %s\\n' \"$*\" >&2\n"
        "exit 99\n",
        encoding="utf-8",
    )
    agentctl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_PLATFORM": platform,
        "FAKE_CALLS": str(calls),
        "FAKE_CHARGE_CONTENT": str(charge_content),
    }
    proc = subprocess.run(
        ["bash", str(RUNNER), "mira", str(CHARGE_SOURCE), str(cwd)],
        capture_output=True,
        text=True,
        env=env,
    )
    argv = calls.read_bytes().decode().split("\0")[:-1]
    return proc, argv, charge_content.read_text(encoding="utf-8"), home, cwd


def test_runner_preserves_claude_remote_control_alias_and_selects_claude_charge(tmp_path):
    proc, argv, charge, _home, cwd = _run_runner(tmp_path, "claude")
    assert proc.returncode == 0, proc.stderr
    assert argv[:6] == ["session", "mira", "-d", "-r", "--as-self", "--cd"]
    assert argv[6] == str(cwd)
    assert argv[7] == "--charge-file"
    assert "ScheduleWakeup" in charge
    assert "codex-umwelt-wake.py" not in charge


def test_runner_omits_incompatible_remote_control_alias_for_codex_and_selects_codex_charge(tmp_path):
    proc, argv, charge, _home, cwd = _run_runner(tmp_path, "codex")
    assert proc.returncode == 0, proc.stderr
    assert argv[:5] == ["session", "mira", "-d", "--as-self", "--cd"]
    assert argv[5] == str(cwd)
    assert argv[6] == "--charge-file"
    assert "-r" not in argv
    assert "codex-umwelt-wake.py" in charge
    for forbidden in ("Monitor", "ScheduleWakeup", "CronList"):
        assert forbidden not in charge


def test_codex_ignores_legacy_generic_override_loudly_and_uses_safe_charge(tmp_path):
    legacy = "Call Monitor, ScheduleWakeup, and CronList immediately.\n"
    proc, _argv, charge, _home, _cwd = _run_runner(
        tmp_path, "codex", legacy_override=legacy
    )
    assert proc.returncode == 0, proc.stderr
    assert "ignoring legacy generic boot charge" in proc.stderr
    assert charge != legacy
    assert "codex-umwelt-wake.py" in charge
    assert "ScheduleWakeup" not in charge


def test_explicit_codex_override_wins_without_rewriting_user_file(tmp_path):
    explicit = "My explicit Codex charge.\n"
    proc, _argv, charge, home, _cwd = _run_runner(
        tmp_path, "codex", explicit_override=explicit
    )
    assert proc.returncode == 0, proc.stderr
    assert charge == explicit
    assert (home / ".engram" / "boot-charge-codex.md").read_text(encoding="utf-8") == explicit


def test_claude_retains_legacy_generic_override_compatibility(tmp_path):
    legacy = "My existing Claude boot charge.\n"
    proc, _argv, charge, _home, _cwd = _run_runner(
        tmp_path, "claude", legacy_override=legacy
    )
    assert proc.returncode == 0, proc.stderr
    assert charge == legacy


def test_quiet_profile_composes_claude_safe_harness_instructions(tmp_path):
    proc, _argv, charge, _home, _cwd = _run_runner(
        tmp_path, "claude", profile="quiet\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert "minimize token spend" in charge
    for required in ("Monitor tool", "ScheduleWakeup", 'pacer: "scheduleWakeup"'):
        assert required in charge
    for forbidden in ("codex-umwelt-wake.py", 'pacer: "systemd"', "CODEX_THREAD_ID"):
        assert forbidden not in charge


def test_quiet_profile_composes_codex_safe_harness_instructions(tmp_path):
    proc, _argv, charge, _home, _cwd = _run_runner(
        tmp_path, "codex", profile="quiet\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert "minimize token spend" in charge
    for required in ("codex-umwelt-wake.py", 'pacer: "systemd"', "systemd heartbeat timer"):
        assert required in charge
    for forbidden in ("Monitor tool", "ScheduleWakeup", "CronList", 'pacer: "scheduleWakeup"'):
        assert forbidden not in charge
