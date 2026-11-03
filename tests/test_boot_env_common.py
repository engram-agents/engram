"""Host-common boot environment file for the Linux autostart launcher.

`deploy/autostart/agent-session-launch.sh` sources a HOST-COMMON file
(`/home/agents-shared/autostart/boot-env.common`, override `AGENT_BOOT_ENV_COMMON`)
immediately BEFORE the per-agent `~/.engram/boot-env`, so a per-agent value wins.

Harness: the real script is copied to tmp with ONLY its hard-coded RUNNER path
rewritten (the installed runner lives under /usr/local/lib, which CI lacks) and
run with stub `tmux` / `claude` / `agentctl` first on PATH. The tmux stub dumps
its inherited environment, which is exactly what the launched chain would see.
The macOS wrapper and agentctl prologue are covered in their own test files.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "autostart" / "agent-session-launch.sh"
RUNNER_LINE = 'RUNNER="/usr/local/lib/agentctl/autostart/agent-session-run.sh"'


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _launch(tmp_path: Path, *, common: str | None = None, per_agent: str | None = None,
            common_mode: int = 0o644):
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env_dump = tmp_path / "child-env.txt"
    runner = tmp_path / "runner.sh"
    _stub(runner, "exit 0")
    script = tmp_path / "agent-session-launch.sh"
    text = SCRIPT.read_text(encoding="utf-8")
    assert RUNNER_LINE in text
    script.write_text(text.replace(RUNNER_LINE, f'RUNNER="{runner}"'), encoding="utf-8")
    _stub(bin_dir / "claude", "exit 0")
    _stub(bin_dir / "agentctl", 'echo "Usage: agentctl session <a> [--charge-file <f>]"; exit 2')
    _stub(bin_dir / "tmux",
          f'if [[ "${{1:-}}" == has-session ]]; then exit 1; fi\nenv > "{env_dump}"\nexit 0')
    if per_agent is not None:
        (home / ".engram" / "boot-env").write_text(per_agent, encoding="utf-8")
    common_file = tmp_path / "shared" / "boot-env.common"
    if common is not None:
        common_file.parent.mkdir()
        common_file.write_text(common, encoding="utf-8")
        common_file.chmod(common_mode)
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "AGENT_BOOT_ENV_COMMON": str(common_file),
        # Pinned so the launcher's #4328 runtime-env block is a no-op (a set,
        # non-empty value is never touched) and never reads the host's /run/user.
        "XDG_RUNTIME_DIR": "/nonexistent-xdg",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent-xdg/bus",
    }
    proc = subprocess.run(["/bin/bash", str(script), "zed"], env=env,
                          capture_output=True, text=True, timeout=30)
    child = env_dump.read_text(encoding="utf-8").splitlines() if env_dump.exists() else None
    return proc, child, common_file


def _env_stderr(proc):
    """Stderr lines about boot-env, ignoring the launcher's always-on cwd line
    and trust preflight (#4170; covered by test_launch_cwd_trust_4170.py)."""
    return [ln for ln in proc.stderr.splitlines()
            if ln and not ln.startswith("boot cwd:")
            and not ln.startswith("ERROR: Claude Code has not trusted")]


def test_per_agent_value_wins_over_common(tmp_path):
    proc, child, _ = _launch(tmp_path, common="SHARED_VAR=common\n", per_agent="SHARED_VAR=agent\n")
    assert proc.returncode == 0, proc.stderr
    assert "SHARED_VAR=agent" in child
    assert "SHARED_VAR=common" not in child


def test_common_only_value_is_exported_to_child(tmp_path):
    proc, child, _ = _launch(tmp_path, common="COMMON_ONLY=yes\n", per_agent="OTHER=1\n")
    assert proc.returncode == 0, proc.stderr
    assert "COMMON_ONLY=yes" in child
    assert "OTHER=1" in child


def test_absent_common_file_is_silent(tmp_path):
    proc, child, _ = _launch(tmp_path, common=None, per_agent="OTHER=1\n")
    assert proc.returncode == 0, proc.stderr
    assert _env_stderr(proc) == []
    assert "OTHER=1" in child


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_unreadable_common_file_warns_once_and_per_agent_still_sourced(tmp_path):
    proc, child, common_file = _launch(tmp_path, common="COMMON_ONLY=yes\n",
                                       per_agent="OTHER=1\n", common_mode=0o000)
    common_file.chmod(0o644)
    assert proc.returncode == 0, proc.stderr
    err = _env_stderr(proc)
    assert len(err) == 1 and "WARNING" in err[0] and str(common_file) in err[0]
    assert "OTHER=1" in child
    assert "COMMON_ONLY=yes" not in child


def test_broken_common_file_aborts_loudly(tmp_path):
    proc, child, _ = _launch(tmp_path, common="false\n", per_agent="OTHER=1\n")
    assert proc.returncode != 0
    assert child is None  # never reached the tmux launch


def test_syntax_error_in_common_file_aborts_loudly(tmp_path):
    proc, child, _ = _launch(tmp_path, common="if then fi (\n", per_agent="OTHER=1\n")
    assert proc.returncode != 0
    assert child is None
