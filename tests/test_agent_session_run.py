"""Integration regressions for the in-tmux autostart runner.

The runner invokes the installed ``agentctl`` command, so these tests replace
only that external executable and assert the argv the real runner supplies.
No tmux, systemd, daemon, or live platform process is started.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


RUNNER = Path(__file__).resolve().parent.parent / "deploy" / "autostart" / "agent-session-run.sh"


def _run_runner(tmp_path: Path, platform: str) -> tuple[subprocess.CompletedProcess[str], list[str], Path, Path]:
    """Run the real runner against a fake agentctl and return its session argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "session-argv.nul"
    home = tmp_path / "home"
    home.mkdir()
    charge = tmp_path / "charge.md"
    charge.write_text("boot charge\n", encoding="utf-8")
    cwd = tmp_path / "project"
    cwd.mkdir()

    agentctl = bin_dir / "agentctl"
    agentctl.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "if [[ \"${1:-}\" == __detect-platform-as-self ]]; then printf '%s\\n' \"$FAKE_PLATFORM\"; exit 0; fi\n"
        "if [[ \"${1:-}\" == session ]]; then printf '%s\\0' \"$@\" > \"$FAKE_CALLS\"; exit 0; fi\n"
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
    }
    proc = subprocess.run(
        ["bash", str(RUNNER), "mira", str(charge), str(cwd)],
        capture_output=True,
        text=True,
        env=env,
    )
    argv = calls.read_bytes().decode().split("\0")[:-1]
    return proc, argv, charge, cwd


def test_runner_preserves_claude_remote_control_alias(tmp_path):
    proc, argv, charge, cwd = _run_runner(tmp_path, "claude")
    assert proc.returncode == 0, proc.stderr
    assert argv == [
        "session", "mira", "-d", "-r", "--as-self", "--cd", str(cwd), "--charge-file", str(charge),
    ]


def test_runner_omits_incompatible_remote_control_alias_for_codex(tmp_path):
    proc, argv, charge, cwd = _run_runner(tmp_path, "codex")
    assert proc.returncode == 0, proc.stderr
    assert argv == [
        "session", "mira", "-d", "--as-self", "--cd", str(cwd), "--charge-file", str(charge),
    ]
    assert "-r" not in argv
