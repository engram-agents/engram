"""forum-drift-check.sh resolves --service-dir like the installer (engram-alpha#3568, F3).

Contract (written BEFORE the implementation): with no --service-dir the script
reuses the FORUM_HOME of the existing engram-forum.service in the user unit dir
(plain or quoted `Environment=` form); with no unit it refuses (exit 2) naming
the flag; with a unit that carries no parsable FORUM_HOME it refuses (exit 2)
naming the unit file, never guessing. An explicit --service-dir always wins.

HERMETIC: every subprocess runs under ``_hermetic_env`` (HOME and XDG_CONFIG_HOME
pinned to a temp dir, ENGRAM_HOME dropped): on the forum-admin seat the real unit
is right there (#3561).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "src" / "forum" / "deploy" / "forum-drift-check.sh"
_MAIN = "engram-forum.service"


def _hermetic_env(home: Path) -> dict:
    env = dict(os.environ)
    env.pop("ENGRAM_HOME", None)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _world(tmp_path: Path, unit_text: str | None) -> SimpleNamespace:
    """A throwaway source checkout, a CURRENT deployed tree, and a hermetic HOME
    (optionally carrying an engram-forum.service with `unit_text`)."""
    repo = tmp_path / "srcrepo"
    (repo / "src" / "forum").mkdir(parents=True)
    (repo / "src" / "forum" / "server.py").write_text("print('hello')\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    svc = tmp_path / "deployed"
    (svc / "app" / "forum").mkdir(parents=True)
    (svc / "app" / "forum" / "server.py").write_text("print('hello')\n")
    home = tmp_path / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    if unit_text is not None:
        (unit_dir / _MAIN).write_text(unit_text.replace("@SVC@", str(svc)))
    return SimpleNamespace(repo=repo, svc=svc, home=home, unit=unit_dir / _MAIN)


def _run(w: SimpleNamespace, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_SCRIPT), "--src", str(w.repo), "--ref", "HEAD", *flags],
        capture_output=True, text=True, env=_hermetic_env(w.home),
    )


class TestServiceDirResolution:

    @pytest.mark.parametrize("env_line", [
        "Environment=FORUM_HOME=@SVC@",
        'Environment="FORUM_HOME=@SVC@"',
    ], ids=["plain", "quoted"])
    def test_existing_units_forum_home_is_used(self, tmp_path, env_line):
        w = _world(tmp_path, f"[Service]\n{env_line}\nExecStart=/x\n")
        r = _run(w)
        assert r.returncode == 0, r.stdout + r.stderr
        assert str(w.svc / "app" / "forum") in r.stdout, r.stdout

    def test_explicit_flag_wins_over_the_unit(self, tmp_path):
        w = _world(tmp_path, "[Service]\nEnvironment=FORUM_HOME=/nonexistent-3568\nExecStart=/x\n")
        r = _run(w, "--service-dir", str(w.svc))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "/nonexistent-3568" not in r.stdout + r.stderr

    def test_no_flag_and_no_unit_refuses_naming_the_flag(self, tmp_path):
        w = _world(tmp_path, None)
        r = _run(w)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "--service-dir" in r.stderr, r.stderr
        assert "/home/agents-shared" not in r.stdout + r.stderr

    @pytest.mark.parametrize("body", [
        "[Service]\nExecStart=/x\n",
        "[Service]\nEnvironment=FORUM_HOME=\nExecStart=/x\n",
        "[Service]\n# Environment=FORUM_HOME=@SVC@\nExecStart=/x\n",
    ], ids=["absent", "empty", "commented"])
    def test_unparsable_unit_refuses_naming_the_unit_file(self, tmp_path, body):
        w = _world(tmp_path, body)
        r = _run(w)
        assert r.returncode == 2, r.stdout + r.stderr
        assert str(w.unit) in r.stderr, r.stderr
        assert "/home/agents-shared" not in r.stdout + r.stderr

    def test_default_no_longer_names_our_directory(self):
        text = _SCRIPT.read_text()
        assert 'SERVICE_DIR="/home/agents-shared/forum"' not in text
        head = text.split("set -euo pipefail", 1)[0]
        assert "Default: /home/agents-shared/forum" not in head


class TestMultiAssignmentUnitRefused:
    """D3 (#3568 follow-up): a FORUM_HOME line carrying a second assignment is not
    a service dir; refuse (exit 2, naming the unit), print nothing else."""

    @pytest.mark.parametrize("env_line", [
        "Environment=FORUM_HOME=@SVC@ FOO=1",
        'Environment="FORUM_HOME=@SVC@" "FOO=1"',
    ], ids=["plain", "quoted"])
    def test_multi_assignment_unit_refuses_naming_the_unit(self, tmp_path, env_line):
        w = _world(tmp_path, f"[Service]\n{env_line}\nExecStart=/x\n")
        r = _run(w)
        assert r.returncode == 2, r.stdout + r.stderr
        assert str(w.unit) in r.stderr, r.stderr
        assert r.stdout == "", r.stdout
        assert len(r.stderr.strip().splitlines()) == 1, r.stderr
