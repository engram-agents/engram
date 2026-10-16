"""Contract tests for engram-alpha#3333: macOS test gating.

Five test files failed on a stock Mac because they need primitives it lacks
(GNU `timeout`, /proc/net/unix, /proc/<pid>/fd, GNU `stat -c`, a BlockingIOError
from a full AF_UNIX backlog). Every skip condition added for #3333 must be
"the primitive is missing", never the platform name, so that a host WITH the
primitive (Linux) never newly skips a test. We cannot run Linux from a Mac,
so each condition is unit-tested here with the primitive faked present and
faked absent, and with `sys.platform` faked to both values to prove it is
not what the condition reads.

Also pinned: the hermetic `timeout` stub runs `timeout 3 cmd args` as
`cmd args`, and is NOT installed when a real `timeout` is on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentctl_forum_hint_3563 as hint_mod  # noqa: E402
import test_resolve_engram_home_abspath as resolve_mod  # noqa: E402
import test_socket_ownership_guard as guard_mod  # noqa: E402
import test_start_daemon_probe_branch_logging as probe_mod  # noqa: E402

PLATFORMS = ["darwin", "linux"]


# ---------------------------------------------------------------------------
# timeout condition + hermetic stub (C2, C3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
def test_timeout_missing_condition_tracks_which_not_platform(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/timeout")
    assert probe_mod._real_timeout_missing() is False
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert probe_mod._real_timeout_missing() is True


def test_stub_runs_timeout_3_cmd_args_as_cmd_args(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert probe_mod._make_timeout_stub(tmp_path) is True
    stub = tmp_path / "timeout"
    assert stub.exists() and os.access(stub, os.X_OK)
    result = subprocess.run(
        [str(stub), "3", "printf", "%s|%s", "a", "b"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == "a|b"
    failing = subprocess.run([str(stub), "3", "sh", "-c", "exit 7"], timeout=10)
    assert failing.returncode == 7, "the stub must pass the command's exit status through"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_stub_not_installed_when_real_timeout_present(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/timeout")
    assert probe_mod._make_timeout_stub(tmp_path) is False
    assert not (tmp_path / "timeout").exists()


@pytest.mark.parametrize("real_timeout", [True, False])
def test_fake_systemctl_installs_stub_only_when_needed(monkeypatch, tmp_path, real_timeout):
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: "/usr/bin/timeout" if real_timeout else None
    )
    probe_mod._make_fake_systemctl(
        tmp_path, mode="show", load_state="loaded", active_state="active", sub_state="running",
    )
    assert (tmp_path / "systemctl").exists()
    assert (tmp_path / "timeout").exists() is (not real_timeout)


def test_wedged_systemctl_fake_never_gets_a_stub(monkeypatch, tmp_path):
    """The wedged-bus cell needs a REAL timeout to bound it; a pass-through
    stub would let the fake sleep out its full duration."""
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    probe_mod._make_fake_systemctl(tmp_path, mode="sleep")
    assert (tmp_path / "systemctl").exists()
    assert not (tmp_path / "timeout").exists()


# ---------------------------------------------------------------------------
# /proc/net/unix (resolve_engram_home_abspath)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("present", [True, False])
def test_proc_net_unix_condition(monkeypatch, platform, present):
    monkeypatch.setattr(sys, "platform", platform)

    class FakePath:
        def __init__(self, p):
            self.p = p

        def exists(self):
            return present

    monkeypatch.setattr(resolve_mod, "Path", FakePath)
    assert resolve_mod._has_proc_net_unix() is present


# ---------------------------------------------------------------------------
# guard primitives (socket_ownership_guard)
# ---------------------------------------------------------------------------


def _fake_guard_host(monkeypatch, *, net_unix, proc_fd, gnu_stat):
    class FakePath:
        def __init__(self, p):
            self.p = p

        def exists(self):
            return net_unix

    monkeypatch.setattr(guard_mod, "Path", FakePath)
    monkeypatch.setattr(os.path, "isdir", lambda p: proc_fd)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0 if gnu_stat else 1),
    )


@pytest.mark.parametrize("platform", PLATFORMS)
def test_guard_primitives_all_present_means_nothing_missing(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    _fake_guard_host(monkeypatch, net_unix=True, proc_fd=True, gnu_stat=True)
    assert guard_mod._missing_guard_primitives() == []


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize(
    "absent,expected",
    [
        ("net_unix", "/proc/net/unix"),
        ("proc_fd", "/proc/<pid>/fd"),
        ("gnu_stat", "GNU `stat -c`"),
    ],
)
def test_guard_primitives_each_absence_is_named(monkeypatch, platform, absent, expected):
    monkeypatch.setattr(sys, "platform", platform)
    flags = {"net_unix": True, "proc_fd": True, "gnu_stat": True}
    flags[absent] = False
    _fake_guard_host(monkeypatch, **flags)
    assert guard_mod._missing_guard_primitives() == [expected]


def test_guard_primitives_stat_that_cannot_run_counts_as_missing(monkeypatch):
    _fake_guard_host(monkeypatch, net_unix=True, proc_fd=True, gnu_stat=True)

    def boom(*a, **k):
        raise FileNotFoundError("stat")

    monkeypatch.setattr(subprocess, "run", boom)
    assert guard_mod._missing_guard_primitives() == ["GNU `stat -c`"]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_stat_only_gate_ignores_missing_procfs(monkeypatch, platform):
    """The stat-only cell must still run on a host with GNU stat but no procfs."""
    monkeypatch.setattr(sys, "platform", platform)
    _fake_guard_host(monkeypatch, net_unix=False, proc_fd=False, gnu_stat=True)
    assert guard_mod._missing_stat_primitive() == []


@pytest.mark.parametrize("platform", PLATFORMS)
def test_stat_only_gate_names_a_missing_stat(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    _fake_guard_host(monkeypatch, net_unix=True, proc_fd=True, gnu_stat=False)
    assert guard_mod._missing_stat_primitive() == ["GNU `stat -c`"]


# ---------------------------------------------------------------------------
# GNU `stat -c` for tools/agentctl (#3614): hint writer + viz-operator config read
# ---------------------------------------------------------------------------


def _fake_stat(monkeypatch, *, returncode=0, stdout="1000 755 user\n", exc=None):
    seen = []

    def fake_run(cmd, *a, **k):
        seen.append(cmd)
        if exc is not None:
            raise exc
        return types.SimpleNamespace(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


@pytest.mark.parametrize("platform", PLATFORMS)
def test_agentctl_gnu_stat_present_means_not_missing(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    seen = _fake_stat(monkeypatch)
    assert hint_mod._gnu_stat_missing() is False
    assert seen[0][:2] == ["stat", "-c"] and "%U" in seen[0][2] and "%a" in seen[0][2]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_agentctl_gnu_stat_rejecting_dash_c_means_missing(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    _fake_stat(monkeypatch, returncode=1, stdout="")
    assert hint_mod._gnu_stat_missing() is True


@pytest.mark.parametrize("platform", PLATFORMS)
def test_agentctl_gnu_stat_empty_output_means_missing(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    _fake_stat(monkeypatch, returncode=0, stdout="\n")
    assert hint_mod._gnu_stat_missing() is True


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("exc", [FileNotFoundError("stat"), PermissionError("stat"),
                                 subprocess.TimeoutExpired("stat", 5)])
def test_agentctl_gnu_stat_that_cannot_run_counts_as_missing(monkeypatch, platform, exc):
    monkeypatch.setattr(sys, "platform", platform)
    _fake_stat(monkeypatch, exc=exc)
    assert hint_mod._gnu_stat_missing() is True


def test_agentctl_gnu_stat_skip_reason_names_the_primitive():
    assert "GNU `stat -c`" in hint_mod._NEEDS_GNU_STAT_REASON
    assert "Linux-only by design" in hint_mod._NEEDS_GNU_STAT_REASON


def test_blocking_io_probe_returns_false_when_the_socket_cannot_bind(monkeypatch):
    """A TMPDIR path over the AF_UNIX length limit must make the probe return
    False (skip the cell), not raise at import and error the whole file."""
    import tempfile

    long_dir = "/tmp/" + "t" * 150
    os.makedirs(long_dir, exist_ok=True)
    try:
        monkeypatch.setattr(tempfile, "tempdir", long_dir)
        assert probe_mod._full_backlog_connect_raises_blocking_io() is False
    finally:
        shutil.rmtree(long_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# full-backlog BlockingIOError probe (probe_branch_logging TestBranch3BlockingIO)
# ---------------------------------------------------------------------------


def _fake_socket_module(connect_exc):
    class FakeSock:
        def bind(self, p):
            pass

        def listen(self, n):
            pass

        def setblocking(self, flag):
            pass

        def close(self):
            pass

        def connect(self, p):
            if connect_exc is not None:
                raise connect_exc

    return types.SimpleNamespace(
        AF_UNIX=1, SOCK_STREAM=1, socket=lambda *a, **k: FakeSock(),
    )


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize(
    "exc,expected",
    [
        (BlockingIOError(), True),            # Linux: full backlog -> EAGAIN
        (ConnectionRefusedError(), False),    # macOS: full backlog -> ECONNREFUSED
        (None, False),                        # backlog never fills
    ],
)
def test_blocking_io_probe_reads_the_connect_behaviour_not_platform(
    monkeypatch, platform, exc, expected
):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(probe_mod, "socket_mod", _fake_socket_module(exc))
    assert probe_mod._full_backlog_connect_raises_blocking_io() is expected
