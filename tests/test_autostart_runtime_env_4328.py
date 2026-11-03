"""Autostart launcher fills XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS (#4328).

The systemd unit starts the launcher with a clean environment, and tmux inherits
the launcher's environment, so every tool shell lacked both variables and
`systemctl --user` failed with "Failed to connect to bus". Contract of the block
in `deploy/autostart/agent-session-launch.sh` (after both boot-env files are
sourced), with uid = `id -u` and base = ${AGENT_RUN_USER_BASE:-/run/user}:

1. XDG_RUNTIME_DIR unset OR EMPTY and $base/$uid is a directory owned by uid
   -> export XDG_RUNTIME_DIR=$base/$uid.
2. DBUS_SESSION_BUS_ADDRESS unset OR EMPTY and $XDG_RUNTIME_DIR/bus is a socket
   -> export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus (reads the
   LIVE XDG_RUNTIME_DIR, including one the boot-env set).
3. A set, non-empty value is never overridden; both set -> silent no-op.
4. Anything missing -> variable stays unset, exactly ONE stderr line starting
   `WARNING: autostart session env:` naming the variable, and the launch goes on.

Harness: same idea as test_boot_env_common.py (real script copied with ONLY the
RUNNER line rewritten; stub tmux/claude/agentctl first on PATH; the tmux stub
dumps its inherited environment). The fake runtime dir lives under /tmp, not
pytest's tmp_path: a unix socket path is limited to ~107 bytes.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# The launcher is the LINUX autostart unit's, and it reads the owner with GNU `stat -c %u`;
# on macOS `stat` has no -c, so these cells would fail there for a reason that has nothing
# to do with the contract (colleague review, alpha 4329). Skip them off Linux.
pytestmark = pytest.mark.skipif(sys.platform != "linux",
                                reason="Linux-only launcher (GNU stat -c); the macOS wrappers have their own tests")

SCRIPT =Path(__file__).resolve().parent.parent / "deploy" / "autostart" / "agent-session-launch.sh"
RUNNER_LINE = 'RUNNER="/usr/local/lib/agentctl/autostart/agent-session-run.sh"'
WARN = "WARNING: autostart session env:"
UID = os.getuid()


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


class Rt:
    """Scratch runtime tree under /tmp; sockets are kept alive until close()."""

    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="xdg", dir="/tmp"))
        self._socks = []
        self._extra = []

    def uid_dir(self, bus: str | None = "socket", name: int = UID) -> Path:
        d = self.base / str(name)
        d.mkdir(mode=0o700)
        self.make_bus(d, bus)
        return d

    def other_dir(self, bus: str | None = "socket") -> Path:
        d = Path(tempfile.mkdtemp(prefix="xdgB", dir="/tmp"))
        self._extra.append(d)
        self.make_bus(d, bus)
        return d

    def make_bus(self, d: Path, bus: str | None) -> None:
        p = d / "bus"
        if bus == "socket":
            s = socket.socket(socket.AF_UNIX)
            s.bind(str(p))
            self._socks.append(s)
        elif bus == "file":
            p.write_text("not a socket")
        elif bus == "dir":
            p.mkdir()
        else:
            assert bus is None

    def close(self):
        for s in self._socks:
            s.close()
        shutil.rmtree(self.base, ignore_errors=True)
        for d in self._extra:
            shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def rt():
    r = Rt()
    try:
        yield r
    finally:
        r.close()


def _launch(tmp_path: Path, *, run_user: Path | None, boot_env: str | None = None,
            fake_uid: int | None = None, id_fails: bool = False):
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
    if id_fails:
        _stub(bin_dir / "id", 'if [[ "${1:-}" == -u ]]; then exit 1; fi\nexit 0')
    elif fake_uid is not None:
        real_id = shutil.which("id")
        _stub(bin_dir / "id",
              f'if [[ "${{1:-}}" == -u ]]; then echo {fake_uid}; else exec {real_id} "$@"; fi')
    if boot_env is not None:
        (home / ".engram" / "boot-env").write_text(boot_env, encoding="utf-8")
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "AGENT_BOOT_ENV_COMMON": str(tmp_path / "none"),
    }
    if run_user is not None:
        env["AGENT_RUN_USER_BASE"] = str(run_user)
    proc = subprocess.run(["/bin/bash", str(script), "zed"], env=env,
                          capture_output=True, text=True, timeout=30)
    child = None
    if env_dump.exists():
        child = dict(ln.split("=", 1) for ln in env_dump.read_text(encoding="utf-8").splitlines()
                     if "=" in ln)
    return proc, child


def _warnings(proc):
    return [ln for ln in proc.stderr.splitlines() if ln.startswith(WARN)]


def _reached(proc, child):
    assert proc.returncode == 0, proc.stderr
    assert child is not None, "tmux stub never reached"


XDG, DBUS = "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"


def test_cell1_unset_dir_and_socket_present_exports_both(tmp_path, rt):
    d = rt.uid_dir("socket")
    proc, child = _launch(tmp_path, run_user=rt.base)
    _reached(proc, child)
    assert child[XDG] == str(d)
    assert child[DBUS] == f"unix:path={d}/bus"
    assert _warnings(proc) == []


def test_cell2_already_set_by_boot_env_is_kept(tmp_path, rt):
    # default dir is absent: an implementation that checks anyway would warn
    proc, child = _launch(
        tmp_path, run_user=rt.base,
        boot_env="XDG_RUNTIME_DIR=/custom/run\nDBUS_SESSION_BUS_ADDRESS=unix:path=/custom/bus\n")
    _reached(proc, child)
    assert child[XDG] == "/custom/run"
    assert child[DBUS] == "unix:path=/custom/bus"
    assert _warnings(proc) == []


def test_cell2b_already_set_even_when_default_dir_is_fine(tmp_path, rt):
    rt.uid_dir("socket")
    proc, child = _launch(
        tmp_path, run_user=rt.base,
        boot_env="XDG_RUNTIME_DIR=/custom/run\nDBUS_SESSION_BUS_ADDRESS=unix:path=/custom/bus\n")
    _reached(proc, child)
    assert child[XDG] == "/custom/run"
    assert child[DBUS] == "unix:path=/custom/bus"
    assert _warnings(proc) == []


def test_cell3_runtime_dir_absent_warns_once_and_launch_continues(tmp_path, rt):
    proc, child = _launch(tmp_path, run_user=rt.base)
    _reached(proc, child)
    assert XDG not in child and DBUS not in child
    w = _warnings(proc)
    assert len(w) == 1 and XDG in w[0] and "not a directory" in w[0], w


def test_cell3b_runtime_path_is_a_regular_file_not_a_directory(tmp_path, rt):
    (rt.base / str(UID)).write_text("not a directory")  # owned by us, but a file
    proc, child = _launch(tmp_path, run_user=rt.base)
    _reached(proc, child)
    assert XDG not in child and DBUS not in child
    w = _warnings(proc)
    assert len(w) == 1 and XDG in w[0] and "not a directory" in w[0], w


def test_cell4_runtime_dir_owned_by_another_uid(tmp_path, rt):
    # The stub `id` claims uid UID+1, so the script looks at <base>/<UID+1>; that
    # directory is really owned by UID (we cannot chown), i.e. by "another uid".
    rt.uid_dir("socket", name=UID + 1)
    proc, child = _launch(tmp_path, run_user=rt.base, fake_uid=UID + 1)
    _reached(proc, child)
    assert XDG not in child and DBUS not in child
    w = _warnings(proc)
    assert len(w) == 1 and XDG in w[0] and f"owned by uid '{UID}'" in w[0], w
    assert "not a directory" not in w[0]


@pytest.mark.parametrize("bus", [None, "file", "dir"])
def test_cell5_dir_present_but_no_bus_socket(tmp_path, rt, bus):
    d = rt.uid_dir(bus)
    proc, child = _launch(tmp_path, run_user=rt.base)
    _reached(proc, child)
    assert child[XDG] == str(d)
    assert DBUS not in child
    w = _warnings(proc)
    assert len(w) == 1 and DBUS in w[0] and f"{d}/bus" in w[0], w


def test_cell6_empty_xdg_in_boot_env_is_treated_as_unset(tmp_path, rt):
    d = rt.uid_dir("socket")
    proc, child = _launch(tmp_path, run_user=rt.base, boot_env="XDG_RUNTIME_DIR=\n")
    _reached(proc, child)
    assert child[XDG] == str(d)
    assert child[DBUS] == f"unix:path={d}/bus"
    assert _warnings(proc) == []


def test_cell6b_empty_dbus_in_boot_env_is_treated_as_unset(tmp_path, rt):
    d = rt.uid_dir("socket")
    proc, child = _launch(tmp_path, run_user=rt.base, boot_env="DBUS_SESSION_BUS_ADDRESS=\n")
    _reached(proc, child)
    assert child[XDG] == str(d)
    assert child[DBUS] == f"unix:path={d}/bus"
    assert _warnings(proc) == []


def test_cell7_bus_derived_from_live_xdg_not_default_dir(tmp_path, rt):
    rt.uid_dir(None)  # default dir A has no bus
    b = rt.other_dir("socket")
    proc, child = _launch(tmp_path, run_user=rt.base, boot_env=f"XDG_RUNTIME_DIR={b}\n")
    _reached(proc, child)
    assert child[XDG] == str(b)
    assert child[DBUS] == f"unix:path={b}/bus"
    assert _warnings(proc) == []


def test_cell8_only_dbus_set_xdg_filled_dbus_kept(tmp_path, rt):
    d = rt.uid_dir("socket")
    proc, child = _launch(tmp_path, run_user=rt.base,
                          boot_env="DBUS_SESSION_BUS_ADDRESS=unix:path=/custom/bus\n")
    _reached(proc, child)
    assert child[XDG] == str(d)
    assert child[DBUS] == "unix:path=/custom/bus"
    assert _warnings(proc) == []


def test_cell9_boot_env_xdg_points_nowhere_dbus_unset_warns_once_about_the_bus(tmp_path, rt):
    # XDG was set by the boot-env to a path that does not exist: never overridden, so the
    # only thing left to say is that its bus is not a socket (a guard that also required
    # the dir to exist would stay silent here: reviewer round 1, mutant X5).
    proc, child = _launch(tmp_path, run_user=rt.base, boot_env="XDG_RUNTIME_DIR=/no/such/dir\n")
    _reached(proc, child)
    assert child[XDG] == "/no/such/dir"
    assert DBUS not in child
    w = _warnings(proc)
    assert len(w) == 1 and DBUS in w[0] and "/no/such/dir/bus" in w[0], w


def test_cell10_id_u_fails_warns_once_naming_the_uid_not_a_missing_directory(tmp_path, rt):
    # `id -u` itself failing is a different cause from a missing runtime dir: the warning must
    # say the uid could not be determined, and the launch still goes on (reviewer round 1).
    proc, child = _launch(tmp_path, run_user=rt.base, id_fails=True)
    _reached(proc, child)
    assert XDG not in child and DBUS not in child
    w = _warnings(proc)
    assert len(w) == 1 and XDG in w[0] and "could not determine" in w[0], w
    assert "not a directory" not in w[0]
