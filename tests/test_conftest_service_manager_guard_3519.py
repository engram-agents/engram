"""Cells for the suite-wide service-manager guard in tests/conftest.py (#3519).

Background: before #3340, `TestMissingToolErrors::test_darwin_no_launchctl_exits_nonzero`
ran the real setup script with PATH = <stub dir>:<inherited PATH>. On a Mac the
inherited PATH holds the real `launchctl`, so the script ran `launchctl bootout`
and `launchctl bootstrap` for real and registered a launchd job under the
production label `com.engram.surface-daemon` (it ran for three days). #3340
fixed that one cell; the guard under test here prevents the next one: every test
runs with a shim directory FIRST on PATH whose `launchctl` / `systemctl` refuse
every non-read-only subcommand (exit 99) and pass read-only ones to the real
tool, found by searching PATH with the shim directory removed.

Contract (stated by the spec, asserted here):
  C1  launchctl mutating / unknown / missing subcommand -> exit 99, stderr names
      #3519 and the running test, the "real" launchctl behind the shim is NOT run.
  C2  launchctl list / print are passed through (or exit 127 with no real tool).
  C3  the same for systemctl, including option-prefixed forms.
  C4  a test's own stub first on PATH wins; the shim is never consulted.
  C5  the guard is active in a test that did nothing special.
  C6  PATH is restored after each test; the shim dir is not inside the repo.
  C7  the historical regression: the real setup script on a faked Darwin with no
      launchctl stub fails at the shim, and nothing reaches a launchctl behind it.

Safety: every cell that runs a shim builds an explicit PATH of
<shim dir>:<recording fake>, so a broken guard reaches a fake, never the real
tool. C5 and C7 use the ambient (guarded) PATH by design; they are only unsafe
on a host where the guard does not exist, so run them with a recording spy
`launchctl` first on PATH until the guard is in place.
"""

from __future__ import annotations

import os
import stat
import subprocess
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SCRIPT = REPO_ROOT / "src" / "engram" / "tools" / "setup-surface-daemon-service.sh"
DAEMON_PY = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py"
TEMPLATE_DIR = REPO_ROOT / "src" / "engram" / "templates"

LAUNCHCTL_MUTATING = [
    "bootstrap", "bootout", "load", "unload", "submit", "remove", "enable",
    "disable", "kickstart", "kill", "start", "stop", "frobnicate",
]


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_fake(directory: Path, name: str, exit_code: int = 0) -> Path:
    """A recording fake standing in for the 'real' tool behind the shim."""
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / f"{name}.calls"
    _write_exec(
        directory / name,
        f'#!/bin/bash\necho "$*" >> "{log}"\nexit {exit_code}\n',
    )
    return log


def _calls(log: Path) -> list:
    return log.read_text().splitlines() if log.exists() else []


def _run_shim(shim_dir, tool, args, behind=None):
    """Run <shim dir>/<tool> with PATH = <shim dir>[:<behind>] only."""
    path = str(shim_dir) + (":" + str(behind) if behind else "")
    env = {**os.environ, "PATH": path}
    return subprocess.run(
        [str(Path(shim_dir) / tool), *args], capture_output=True, text=True, env=env,
        timeout=10,  # a shim that re-execs itself must go red, not hang
    )


def _running_test_id() -> str:
    return os.environ["PYTEST_CURRENT_TEST"].split(" (")[0]


def _assert_refused(r, tool):
    assert r.returncode == 99, (r.returncode, r.stdout, r.stderr)
    assert "#3519" in r.stderr
    assert f"refused {tool}" in r.stderr
    assert _running_test_id() in r.stderr


# ---------------------------------------------------------------- C1 launchctl


@pytest.mark.parametrize("sub", LAUNCHCTL_MUTATING)
def test_c1_launchctl_mutating_subcommand_refused(service_manager_shim_dir, tmp_path, sub):
    log = _make_fake(tmp_path / "fake", "launchctl")
    r = _run_shim(service_manager_shim_dir, "launchctl",
                  [sub, "gui/501/com.engram.surface-daemon"], behind=tmp_path / "fake")
    _assert_refused(r, "launchctl")
    assert _calls(log) == [], "the tool behind the shim must not be run"


def test_c1_launchctl_no_subcommand_refused(service_manager_shim_dir, tmp_path):
    log = _make_fake(tmp_path / "fake", "launchctl")
    r = _run_shim(service_manager_shim_dir, "launchctl", [], behind=tmp_path / "fake")
    _assert_refused(r, "launchctl")
    assert _calls(log) == []


# ---------------------------------------------------------------- C2 launchctl


@pytest.mark.parametrize("args", [["list"], ["print", "gui/501/x"], ["print-disabled", "gui/501"]])
def test_c2_launchctl_readonly_passed_through(service_manager_shim_dir, tmp_path, args):
    log = _make_fake(tmp_path / "fake", "launchctl", exit_code=7)
    r = _run_shim(service_manager_shim_dir, "launchctl", args, behind=tmp_path / "fake")
    assert r.returncode == 7, "the exit status of the real tool is the shim's"
    assert _calls(log) == [" ".join(args)]
    assert "#3519" not in r.stderr


def test_c2_launchctl_readonly_without_real_tool_exits_127(service_manager_shim_dir):
    r = _run_shim(service_manager_shim_dir, "launchctl", ["list"])
    assert r.returncode == 127, (r.returncode, r.stderr)
    assert "#3519" not in r.stderr


# ----------------------------------------------------------------- C3 systemctl

SYSTEMCTL_REFUSED = [
    ["--user", "daemon-reload"],
    ["start", "x"],
    ["--user", "restart", "x"],
    ["enable", "x"],
    ["disable", "x"],
    ["stop", "x"],
    ["kill", "x"],
    ["mask", "x"],
    ["import-environment"],
    ["reset-failed"],
    ["--user", "--no-pager", "start", "x"],
    ["-p", "ActiveState", "start", "x"],
    ["--property=ActiveState", "stop", "x"],
    ["--now", "enable", "x"],
    ["frobnicate"],
    [],
    ["--user"],
]

SYSTEMCTL_ALLOWED = [
    ["--user", "show", "-p", "ActiveState", "x"],
    ["status", "x"],
    ["is-active", "x"],
    ["list-units"],
    ["-p", "ActiveState", "show", "x"],
    ["--user", "--no-pager", "--quiet", "is-enabled", "x"],
    ["-n", "5", "status", "x"],
    ["--version"],
]


@pytest.mark.parametrize("args", SYSTEMCTL_REFUSED, ids=lambda a: " ".join(a) or "<bare>")
def test_c3_systemctl_mutating_refused(service_manager_shim_dir, tmp_path, args):
    log = _make_fake(tmp_path / "fake", "systemctl")
    r = _run_shim(service_manager_shim_dir, "systemctl", args, behind=tmp_path / "fake")
    _assert_refused(r, "systemctl")
    assert _calls(log) == []


@pytest.mark.parametrize("args", SYSTEMCTL_ALLOWED, ids=lambda a: " ".join(a))
def test_c3_systemctl_readonly_passed_through(service_manager_shim_dir, tmp_path, args):
    log = _make_fake(tmp_path / "fake", "systemctl", exit_code=3)
    r = _run_shim(service_manager_shim_dir, "systemctl", args, behind=tmp_path / "fake")
    assert r.returncode == 3
    assert _calls(log) == [" ".join(args)]
    assert "#3519" not in r.stderr


def test_c3_systemctl_readonly_without_real_tool_exits_127(service_manager_shim_dir):
    r = _run_shim(service_manager_shim_dir, "systemctl", ["status", "x"])
    assert r.returncode == 127, (r.returncode, r.stderr)


# ------------------------------------------- near-miss words of allowed ones (F3)

LAUNCHCTL_NEAR_MISS = ["listx", "lis", "list-all", "printx", "Print", "examine"]
SYSTEMCTL_NEAR_MISS = ["showx", "show-foo", "statusx", "cat2"]


@pytest.mark.parametrize("sub", LAUNCHCTL_NEAR_MISS)
def test_near_miss_launchctl_refused(service_manager_shim_dir, tmp_path, sub):
    # `examine` runs a tool against launchd (man launchctl): not allowlisted.
    log = _make_fake(tmp_path / "fake", "launchctl")
    r = _run_shim(service_manager_shim_dir, "launchctl", [sub, "x"], behind=tmp_path / "fake")
    _assert_refused(r, "launchctl")
    assert _calls(log) == []


@pytest.mark.parametrize("sub", SYSTEMCTL_NEAR_MISS)
def test_near_miss_systemctl_refused(service_manager_shim_dir, tmp_path, sub):
    log = _make_fake(tmp_path / "fake", "systemctl")
    r = _run_shim(service_manager_shim_dir, "systemctl", [sub, "x"], behind=tmp_path / "fake")
    _assert_refused(r, "systemctl")
    assert _calls(log) == []


# ----------------------- value-taking options the subcommand finder does not
# know (F1): an allowlisted word in value position must not hide a mutating verb.

SYSTEMCTL_OPTION_HOLE = [
    ["--kill-whom", "show", "kill", "x"],
    ["--kill-who", "status", "kill", "x"],
    ["--job-mode", "status", "start", "x"],
    ["--state", "cat", "start", "x"],
    ["--root", "show", "start", "x"],
    ["-E", "show", "start", "x"],
    ["--what", "show", "clean", "x"],
    ["--preset-mode", "show", "preset", "x"],
    ["--timestamp", "show", "start", "x"],
    ["-ap", "show", "start", "x"],
]


@pytest.mark.parametrize("args", SYSTEMCTL_OPTION_HOLE, ids=lambda a: " ".join(a))
def test_systemctl_option_value_hole_refused(service_manager_shim_dir, tmp_path, args):
    log = _make_fake(tmp_path / "fake", "systemctl")
    r = _run_shim(service_manager_shim_dir, "systemctl", args, behind=tmp_path / "fake")
    _assert_refused(r, "systemctl")
    assert _calls(log) == []


# Sol's round: real systemctl lets an UNLISTED value-taking option swallow the next
# word (`--root is-enabled foo.service` -> the verb is foo.service), so the finder
# must fail closed on every dash-token it cannot place.

SYSTEMCTL_FAIL_CLOSED_REFUSED = [
    # forms that reached the spy on cd616a7e
    ["--job-mode", "show", "reenable", "x"],
    ["--state", "show", "reenable", "x"],
    ["--job-mode", "show", "try-reload-or-restart", "x"],
    ["--job-mode", "show", "set-default", "x"],
    ["--job-mode", "show", "soft-reboot"],
    ["--job-mode", "show", "force-reload", "x"],
    ["--root", "show", "enqueue-marked"],
    # more unplaceable options
    ["--boot-loader-entry", "show", "start", "x"],
    ["--drop-in", "show", "start", "x"],
    ["--image", "show", "start", "x"],
    ["--kill-value", "show", "kill", "x"],
    ["--legend", "show", "start", "x"],
    ["--when", "show", "start", "x"],
    ["--what", "show", "clean", "x"],
    ["-ap", "show", "start", "x"],
    ["--", "show", "x"],
    ["--user", "--", "status", "x"],
    # a verb on NO list at all (a future systemd verb): only the finder can stop
    # these, so they discriminate the fail-closed rule from the any-token backstop
    ["--job-mode", "show", "frobnicate", "x"],
    ["--state", "status", "some-future-verb", "x"],
    ["--root", "is-enabled", "newverb"],
    # a mutating verb that is on neither original list, bare and as an argument
    ["reenable", "x"],
    ["set-default", "x"],
    ["soft-reboot"],
    ["suspend-then-hibernate"],
    ["try-reload-or-restart", "x"],
    ["show", "reenable", "x"],
    ["status", "try-reload-or-restart", "x"],
    ["show", "enqueue-marked"],
    ["is-active", "condrestart", "x"],
]


@pytest.mark.parametrize("args", SYSTEMCTL_FAIL_CLOSED_REFUSED, ids=lambda a: " ".join(a))
def test_systemctl_fail_closed_refused(service_manager_shim_dir, tmp_path, args):
    log = _make_fake(tmp_path / "fake", "systemctl")
    r = _run_shim(service_manager_shim_dir, "systemctl", args, behind=tmp_path / "fake")
    _assert_refused(r, "systemctl")
    assert _calls(log) == []


SYSTEMCTL_FAIL_CLOSED_CONTROLS = [
    ["--user", "show", "-p", "ActiveState", "engram-surface-daemon.service"],
    ["--user", "--no-pager", "status", "x"],
    ["--property=ActiveState", "show", "x"],
    ["is-active", "x"],
    ["--user", "list-units"],
    ["-n", "20", "status", "x"],
    ["--user", "is-enabled", "x"],
    ["--version"],
]


@pytest.mark.parametrize("args", SYSTEMCTL_FAIL_CLOSED_CONTROLS, ids=lambda a: " ".join(a))
def test_systemctl_fail_closed_controls_pass_through(service_manager_shim_dir, tmp_path, args):
    log = _make_fake(tmp_path / "fake", "systemctl")
    r = _run_shim(service_manager_shim_dir, "systemctl", args, behind=tmp_path / "fake")
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert _calls(log) == [" ".join(args)]


def test_option_hole_controls_still_pass_through(service_manager_shim_dir, tmp_path):
    slog = _make_fake(tmp_path / "sfake", "systemctl")
    sargs = ["--user", "show", "-p", "ActiveState", "engram-surface-daemon.service"]
    r = _run_shim(service_manager_shim_dir, "systemctl", sargs, behind=tmp_path / "sfake")
    assert r.returncode == 0 and _calls(slog) == [" ".join(sargs)]
    llog = _make_fake(tmp_path / "lfake", "launchctl")
    r = _run_shim(service_manager_shim_dir, "launchctl", ["print", "gui/501/x"],
                  behind=tmp_path / "lfake")
    assert r.returncode == 0 and _calls(llog) == ["print gui/501/x"]


# --------------------------------------------------------- C4 test's own stub


@pytest.mark.parametrize("tool", ["launchctl", "systemctl"])
def test_c4_tests_own_stub_first_on_path_wins(tmp_path, monkeypatch, tool):
    """Do what the existing tests do: prepend a stub dir to the ambient PATH
    (through os.environ) and run with the inherited environment."""
    stub_dir = tmp_path / "stub"
    log = _make_fake(stub_dir, tool, exit_code=0)
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ['PATH']}")
    sub = "bootstrap" if tool == "launchctl" else "enable"
    r = subprocess.run([tool, sub, "x"], capture_output=True, text=True)
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert "#3519" not in r.stderr
    assert _calls(log) == [f"{sub} x"]


# ------------------------------------------------------------- C5 autouse guard


def _spy_after_shim(monkeypatch, tmp_path, tool):
    """Put a recording spy right after the shim on the ambient PATH (as C7
    does), so a broken guard reaches the spy, never a real launchd/systemd."""
    spy_dir = tmp_path / "spy"
    log = _make_fake(spy_dir, tool)
    ambient = os.environ["PATH"].split(":")
    monkeypatch.setenv("PATH", ":".join([ambient[0], str(spy_dir), *ambient[1:]]))
    return log


def test_c5_guard_active_without_any_setup(monkeypatch, tmp_path):
    """Nothing stubbed by the test: a mutating call is refused on every host
    (the shim is found even where no real launchctl exists)."""
    spy_log = _spy_after_shim(monkeypatch, tmp_path, "launchctl")
    r = subprocess.run(
        ["launchctl", "bootstrap", "gui/1", "/nonexistent.plist"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 99, (r.returncode, r.stdout, r.stderr)
    assert "#3519" in r.stderr
    assert _calls(spy_log) == []


def test_c5_systemctl_guard_active_without_any_setup(monkeypatch, tmp_path):
    spy_log = _spy_after_shim(monkeypatch, tmp_path, "systemctl")
    r = subprocess.run(["systemctl", "--user", "start", "x"], capture_output=True,
                       text=True, timeout=10)
    assert r.returncode == 99, (r.returncode, r.stdout, r.stderr)
    assert _calls(spy_log) == []


# ------------------------------------------------------------ C6 PATH restoration
# Two ordered cells, no pytester: each cell asserts the invariant a leaked
# prepend would break (the shim dir appears exactly once, and only first). If
# the first cell's guard leaked its prepend past teardown, the second cell would
# see the shim dir twice.


def test_c6_a_path_has_shim_dir_exactly_once_first(service_manager_shim_dir):
    entries = os.environ["PATH"].split(":")
    assert entries[0] == str(service_manager_shim_dir)
    assert entries.count(str(service_manager_shim_dir)) == 1


def test_c6_b_following_test_does_not_see_a_leaked_prepend(service_manager_shim_dir):
    entries = os.environ["PATH"].split(":")
    assert entries[0] == str(service_manager_shim_dir)
    assert entries.count(str(service_manager_shim_dir)) == 1, (
        "the previous test's PATH prepend leaked past its teardown"
    )


def test_c6_shim_dir_is_not_inside_the_repo(service_manager_shim_dir):
    shim = Path(service_manager_shim_dir).resolve()
    assert REPO_ROOT.resolve() not in [shim, *shim.parents]
    assert (shim / "launchctl").exists() and (shim / "systemctl").exists()
    assert os.access(shim / "launchctl", os.X_OK) and os.access(shim / "systemctl", os.X_OK)


# ------------------------------------------------- C7 the historical regression


def test_c7_old_darwin_no_launchctl_cell_is_stopped_by_the_guard(
    service_manager_shim_dir, tmp_path
):
    """The cell that registered a zombie job on a Mac, as it was before #3340:
    HOME = a temp dir, `uname` stubbed to Darwin, python3 stubbed, NO launchctl
    stub, the inherited (guarded) PATH. The script must fail at the shim, and a
    launchctl behind the shim (here a recording spy) must never see a
    bootstrap or bootout."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _write_exec(stub_dir / "uname", '#!/usr/bin/env bash\necho "Darwin"\n')
    _write_exec(stub_dir / "python3", '#!/usr/bin/env bash\necho "/usr/bin/python3"\n')

    plugin_root = stub_dir / "plugin"
    hooks_dir = plugin_root / "hooks" / "claude"
    template_dir = plugin_root / "templates"
    hooks_dir.mkdir(parents=True)
    template_dir.mkdir(parents=True)
    (hooks_dir / "engram-surface-daemon.py").symlink_to(DAEMON_PY)
    for f in TEMPLATE_DIR.iterdir():
        if f.is_file():
            shutil.copy(f, template_dir / f.name)
    engram_home = fake_home / ".engram"
    engram_home.mkdir()

    # Inherited (guarded) PATH, with the spy directly after the shim so that a
    # guard that passed the call through would reach the spy, never a real tool.
    spy_dir = tmp_path / "spy"
    spy_log = _make_fake(spy_dir, "launchctl")
    ambient = os.environ["PATH"].split(":")
    assert ambient[0] == str(service_manager_shim_dir)
    path = ":".join([str(stub_dir), ambient[0], str(spy_dir), *ambient[1:]])

    env = {**os.environ, "PATH": path, "HOME": str(fake_home), "ENGRAM_HOME": str(engram_home)}
    r = subprocess.run(["bash", str(SETUP_SCRIPT)], capture_output=True, text=True, env=env, timeout=10)

    out = r.stdout + r.stderr
    assert r.returncode != 0, f"the script must fail at the guard; output:\n{out}"
    assert "#3519" in out, out
    assert not [c for c in _calls(spy_log) if c.startswith(("bootstrap", "bootout"))], _calls(spy_log)
