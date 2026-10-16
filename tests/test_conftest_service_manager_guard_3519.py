"""Cells for the suite-wide service-manager guard in the repo-root conftest.py
(#3519; moved there from tests/conftest.py by #3540). It shims `launchctl`,
`systemctl`, `systemd-run` and `loginctl`; the K-cells at the end cover the two
added for the class (not the tool list) and the candidates scan.

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


_C4_MUTATING_SUB = {
    "launchctl": "bootstrap",
    "systemctl": "enable",
    "systemd-run": "--user",
    "loginctl": "enable-linger",
}


@pytest.mark.parametrize("tool", ["launchctl", "systemctl", "systemd-run", "loginctl"])
def test_c4_tests_own_stub_first_on_path_wins(tmp_path, monkeypatch, tool):
    """Do what the existing tests do: prepend a stub dir to the ambient PATH
    (through os.environ) and run with the inherited environment. A mutating call
    that the guard would refuse must reach the test's own stub instead."""
    stub_dir = tmp_path / "stub"
    log = _make_fake(stub_dir, tool, exit_code=0)
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ['PATH']}")
    sub = _C4_MUTATING_SUB[tool]
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

    env = {**os.environ, "PATH": path, "HOME": str(fake_home), "ENGRAM_HOME": str(engram_home),
           # #3519 item 1: the script's own ephemeral-path refusal would fire first
           # (temp/worktree daemon path); this cell tests the SHIM, so allow it.
           "ENGRAM_SETUP_ALLOW_PATH": "1"}
    r = subprocess.run(["bash", str(SETUP_SCRIPT)], capture_output=True, text=True, env=env, timeout=10)

    out = r.stdout + r.stderr
    assert r.returncode != 0, f"the script must fail at the guard; output:\n{out}"
    assert "#3519" in out, out
    assert not [c for c in _calls(spy_log) if c.startswith(("bootstrap", "bootout"))], _calls(spy_log)


# ============================================================================
# Class guard (#3519 follow-up): the invariant is "no test may mutate the real
# service manager", not a list of two tool names. Zephyr measured `systemd-run
# --user` from a test creating REAL transient units (12 leaked over four suite
# runs) with no refusal. Cells K1-K7 below.
# ============================================================================

import importlib.util
import re


def _load_root_conftest():
    """The repo-root conftest.py, loaded by path: `import conftest` is ambiguous
    here (tests/conftest.py also exists)."""
    spec = importlib.util.spec_from_file_location(
        "_root_conftest_for_3519", REPO_ROOT / "conftest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------- K1 systemd-run, the required control


def test_k1_systemd_run_guard_active_without_any_setup(monkeypatch, tmp_path):
    """Zephyr's case. Nothing stubbed by the test, only the autouse guard: a
    transient-unit call is refused (exit 99) and a spy `systemd-run` behind the
    shim is never called."""
    spy_log = _spy_after_shim(monkeypatch, tmp_path, "systemd-run")
    r = subprocess.run(
        ["systemd-run", "--user", "--unit", "x", "/bin/true"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 99, (r.returncode, r.stdout, r.stderr)
    assert "#3519" in r.stderr
    assert "refused systemd-run" in r.stderr
    assert _calls(spy_log) == [], "the spy behind the shim was reached"


# ------------------------------------------------- K2 / K3 systemd-run allowlist
# Decision: systemd-run has no read-only verbs. The ONLY passed-through calls are
# EXACTLY one argument, one of --version / --help / -h. Anything else (including
# a help flag mixed with other tokens) is refused.

SYSTEMD_RUN_ALLOWED = [["--version"], ["--help"], ["-h"]]

SYSTEMD_RUN_REFUSED = [
    [],
    ["--user"],
    ["--scope", "/bin/true"],
    ["--user", "--version"],
    ["--version", "--user"],
    ["--help", "x"],
    ["-h", "--user"],
    ["--versions"],
    ["--versio"],
    ["--Version"],
    ["--user", "--unit", "x", "/bin/true"],
    ["/bin/true"],
    ["version"],
    ["--"],
]


@pytest.mark.parametrize("args", SYSTEMD_RUN_ALLOWED, ids=lambda a: " ".join(a))
def test_k2_systemd_run_help_version_passed_through(service_manager_shim_dir, tmp_path, args):
    log = _make_fake(tmp_path / "fake", "systemd-run", exit_code=3)
    r = _run_shim(service_manager_shim_dir, "systemd-run", args, behind=tmp_path / "fake")
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert _calls(log) == [" ".join(args)]
    assert "#3519" not in r.stderr


@pytest.mark.parametrize("args", SYSTEMD_RUN_REFUSED, ids=lambda a: " ".join(a) or "<bare>")
def test_k3_systemd_run_refused(service_manager_shim_dir, tmp_path, args):
    log = _make_fake(tmp_path / "fake", "systemd-run")
    r = _run_shim(service_manager_shim_dir, "systemd-run", args, behind=tmp_path / "fake")
    _assert_refused(r, "systemd-run")
    assert _calls(log) == []


# ------------------------------------------------------------------ K4 loginctl

LOGINCTL_ALLOWED = [
    ["show-user", "x"],
    ["show-session", "1"],
    ["list-sessions"],
    ["list-users"],
    ["user-status", "x"],
    ["session-status", "1"],
    ["--version"],
    ["help"],
    # option forms the finder must place
    ["--no-pager", "list-sessions"],
    ["--no-legend", "--no-pager", "list-users"],
    ["--value", "-a", "--all", "-l", "--full", "show-user", "x"],
    ["--no-ask-password", "-q", "--quiet", "list-sessions"],
    ["-p", "Linger", "show-user", "x"],
    ["--property", "Linger", "show-user", "x"],
    ["--property=Linger", "show-user", "x"],
    ["-H", "host", "list-sessions"],
    ["--host=host", "list-sessions"],
    ["-M", "m", "show-session", "1"],
    ["--machine=m", "show-session", "1"],
    ["-n", "5", "session-status", "1"],
    ["--lines=5", "session-status", "1"],
    ["-o", "json", "user-status", "x"],
    ["--output=json", "user-status", "x"],
    ["-s", "SIGTERM", "show-user", "x"],
    ["--signal=SIGTERM", "show-user", "x"],
    ["--kill-whom", "all", "show-user", "x"],
    ["--kill-whom=all", "show-user", "x"],
]

LOGINCTL_MUTATING = [
    "enable-linger", "disable-linger", "terminate-user", "terminate-session",
    "kill-user", "kill-session", "lock-session", "unlock-session", "activate",
    "attach", "flush-devices",
]

LOGINCTL_REFUSED = (
    [[verb, "x"] for verb in LOGINCTL_MUTATING]
    + [
        ["--no-pager", "enable-linger", "x"],
        ["-p", "Linger", "enable-linger", "x"],
        ["--property=Linger", "disable-linger", "x"],
        # an unknown verb, and no verb
        ["frobnicate"],
        ["show-users"],
        ["list-sessionsx"],
        [],
        ["--no-pager"],
        # unplaceable dash tokens fail closed
        ["--bogus", "show-user", "x"],
        ["-ap", "show-user", "x"],
        ["--", "show-user", "x"],
        ["--root", "show-user", "enable-linger", "x"],
        ["-Z", "show-user", "x"],
        # a value-taking option eats the next word; the verb is then `x`
        ["--signal", "show-user", "x"],
        ["-p", "show-user", "x"],
        # belt: a known value-taking option cannot hide a mutating verb
        ["--kill-whom", "show-user", "kill-user", "x"],
        ["show-user", "enable-linger"],
        ["list-sessions", "terminate-session", "1"],
    ]
)


@pytest.mark.parametrize("args", LOGINCTL_ALLOWED, ids=lambda a: " ".join(a))
def test_k4_loginctl_readonly_passed_through(service_manager_shim_dir, tmp_path, args):
    log = _make_fake(tmp_path / "fake", "loginctl", exit_code=3)
    r = _run_shim(service_manager_shim_dir, "loginctl", args, behind=tmp_path / "fake")
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert _calls(log) == [" ".join(args)]
    assert "#3519" not in r.stderr


@pytest.mark.parametrize("args", LOGINCTL_REFUSED, ids=lambda a: " ".join(a) or "<bare>")
def test_k4_loginctl_refused(service_manager_shim_dir, tmp_path, args):
    log = _make_fake(tmp_path / "fake", "loginctl")
    r = _run_shim(service_manager_shim_dir, "loginctl", args, behind=tmp_path / "fake")
    _assert_refused(r, "loginctl")
    assert _calls(log) == []


def test_k4_loginctl_guard_active_without_any_setup(monkeypatch, tmp_path):
    """The real call a shipped installer makes (`loginctl enable-linger`)."""
    spy_log = _spy_after_shim(monkeypatch, tmp_path, "loginctl")
    r = subprocess.run(["loginctl", "enable-linger", "nobody"], capture_output=True,
                       text=True, timeout=10)
    assert r.returncode == 99, (r.returncode, r.stdout, r.stderr)
    assert _calls(spy_log) == []


# --------------------------------------------------------------------- K5 pin


def test_k5_service_shims_are_exactly_the_four_tools():
    conf = _load_root_conftest()
    assert set(conf._SERVICE_SHIMS) == {"launchctl", "systemctl", "systemd-run", "loginctl"}


def test_k5_shim_dir_holds_all_four_shims(service_manager_shim_dir):
    for tool in ("launchctl", "systemctl", "systemd-run", "loginctl"):
        shim = Path(service_manager_shim_dir) / tool
        assert shim.exists() and os.access(shim, os.X_OK), tool


# ------------------------------------------- K6 the class cell + scanner controls

SCAN_EXTENSIONS = {".py", ".sh", ".bash", ".template", ".service", ".plist"}
SCAN_TOP_DIRS = ("src", "tests", "tools", "deploy")
SCAN_SHEBANG_DIRS = ("src", "tools", "deploy")  # extensionless `#!` files only here
SCAN_SKIP_DIRNAMES = {"__pycache__", ".git", "node_modules", ".venv", "venv"}
# the two files that legitimately name every candidate
SCAN_EXCLUDED_RELPATHS = {
    "conftest.py",
    "tests/test_conftest_service_manager_guard_3519.py",
}
INITIAL_CANDIDATES = (
    "launchctl", "systemctl", "systemd-run", "loginctl", "busctl", "dbus-send",
    "gdbus", "systemd-mount", "machinectl",
)


def _has_shebang(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _scan_candidate_files(root: Path):
    """Yield (relpath, path) for every code file the K6 scan covers under <root>."""
    root = Path(root)
    top = root / "conftest.py"
    if top.is_file():
        yield "conftest.py", top
    for top_dir in SCAN_TOP_DIRS:
        base = root / top_dir
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SCAN_SKIP_DIRNAMES)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                ext = path.suffix
                if ext in SCAN_EXTENSIONS or (
                    ext == "" and top_dir in SCAN_SHEBANG_DIRS and _has_shebang(path)
                ):
                    yield path.relative_to(root).as_posix(), path


def scan_candidate_use(root, candidates):
    """{candidate: [relpath, ...]} for every candidate INVOKED (word-boundary
    match: the token is not preceded or followed by [A-Za-z0-9_-]) in a code file
    under <root>, excluding the guard's own two files. Takes a root so controls
    run on tmp trees, never the repo."""
    patterns = {
        c: re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(c) + r"(?![A-Za-z0-9_-])")
        for c in candidates
    }
    found: dict = {}
    for rel, path in _scan_candidate_files(root):
        if rel in SCAN_EXCLUDED_RELPATHS:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for cand, pat in patterns.items():
            if pat.search(text):
                found.setdefault(cand, []).append(rel)
    return found


def candidates_missing_a_shim(root, candidates, shims):
    """The K6 rule: {candidate: [files]} for candidates invoked under <root> that
    have no entry in <shims>. Empty means the rule holds."""
    used = scan_candidate_use(root, candidates)
    return {c: files for c, files in used.items() if c not in shims}


def test_k6_every_invoked_service_manager_tool_is_shimmed():
    conf = _load_root_conftest()
    missing = candidates_missing_a_shim(
        REPO_ROOT, conf.SERVICE_MANAGER_TOOL_CANDIDATES, conf._SERVICE_SHIMS
    )
    assert not missing, (
        "service-manager tool(s) invoked in repo code but not guarded: "
        + "; ".join(f"{c} in {files}" for c, files in sorted(missing.items()))
        + ". Add a shim to _SERVICE_SHIMS in the root conftest.py (or, if it truly "
        "cannot mutate, remove it from SERVICE_MANAGER_TOOL_CANDIDATES with a "
        "one-line reason in the comment)."
    )


def test_k6_repo_scan_is_not_vacuous():
    """The K6 rule above passes on an empty scan, so pin a floor: launchctl and
    systemctl are invoked in shipped code today, and the real-repo scan must find
    both. A wrong REPO_ROOT or moved directories turn this red instead of letting
    the class cell pass on nothing."""
    used = scan_candidate_use(REPO_ROOT, ("launchctl", "systemctl"))
    missing = {"launchctl", "systemctl"} - set(used)
    assert not missing, (
        f"the K6 scanner found no use of {sorted(missing)} in {REPO_ROOT}, which it is "
        "known to find; the scan is mis-wired, so the K6 rule cannot be trusted"
    )


def test_k6_initial_candidates_are_all_present():
    conf = _load_root_conftest()
    cands = set(conf.SERVICE_MANAGER_TOOL_CANDIDATES)
    assert set(INITIAL_CANDIDATES) <= cands, (
        "dropped from SERVICE_MANAGER_TOOL_CANDIDATES: "
        f"{sorted(set(INITIAL_CANDIDATES) - cands)}; if a tool truly cannot mutate, "
        "record the reason in the conftest.py comment and update INITIAL_CANDIDATES here"
    )
    # every shimmed tool is itself a candidate, so the scan covers it
    assert set(conf._SERVICE_SHIMS) <= cands


def _plant(root: Path, rel: str, body: str, mode: int = 0o644) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)
    return path


@pytest.mark.parametrize(
    "rel, body, mode",
    [
        ("deploy/autostart/x.sh", "busctl call a\n", 0o644),
        ("deploy/autostart/launch-x", "#!/bin/bash\nbusctl call a\n", 0o755),
    ],
    ids=["deploy-sh", "deploy-shebang"],
)
def test_k6_control_scanner_covers_deploy_dir(tmp_path, rel, body, mode):
    _plant(tmp_path, rel, body, mode)
    assert scan_candidate_use(tmp_path, INITIAL_CANDIDATES) == {"busctl": [rel]}


def test_k6_control_positive_scanner_reports_planted_use(tmp_path):
    _plant(tmp_path, "src/x/mod.py", 'import subprocess\nsubprocess.run(["busctl", "call", "a"])\n')
    assert scan_candidate_use(tmp_path, INITIAL_CANDIDATES) == {"busctl": ["src/x/mod.py"]}


@pytest.mark.parametrize(
    "decoy", ["mybusctl", "busctl_helper", "busctl-ish", "x-busctl", "busctl2", "_busctl"]
)
def test_k6_control_negative_decoys_not_reported(tmp_path, decoy):
    _plant(tmp_path, "src/x/mod.py", f'run("{decoy} call")\n')
    assert scan_candidate_use(tmp_path, INITIAL_CANDIDATES) == {}


@pytest.mark.parametrize(
    "rel, body, mode",
    [
        ("src/a/run.sh", "#!/bin/bash\nbusctl call x\n", 0o755),
        ("tools/a/run.bash", "busctl call x\n", 0o644),
        ("tests/a/t.py", "x = 'busctl'\n", 0o644),
        ("src/a/u.service", "ExecStart=/usr/bin/busctl call x\n", 0o644),
        ("src/a/u.template", "busctl\n", 0o644),
        ("src/a/u.plist", "<string>busctl</string>\n", 0o644),
        ("src/a/noext", "#!/bin/sh\nbusctl call x\n", 0o755),
        ("tools/a/noext", "#!/usr/bin/env bash\nbusctl call x\n", 0o755),
        ("conftest.py", "busctl\n", 0o644),
    ],
)
def test_k6_control_scanner_covers_each_file_kind(tmp_path, rel, body, mode):
    _plant(tmp_path, rel, body, mode)
    got = scan_candidate_use(tmp_path, INITIAL_CANDIDATES)
    if rel == "conftest.py":
        assert got == {}, "the root conftest.py is excluded"
    else:
        assert got == {"busctl": [rel]}, got


def test_k6_control_scanner_ignores_what_it_should(tmp_path):
    _plant(tmp_path, "src/a/notes.txt", "busctl\n")  # wrong extension
    _plant(tmp_path, "src/a/noshebang", "busctl\n")  # extensionless, no shebang
    _plant(tmp_path, "tests/noshebang", "#!/bin/sh\nbusctl\n", 0o755)  # shebang only src/tools
    _plant(tmp_path, "docs/a.py", "busctl\n")  # directory not scanned
    _plant(tmp_path, "tests/test_conftest_service_manager_guard_3519.py", "busctl\n")  # excluded
    _plant(tmp_path, "src/__pycache__/a.py", "busctl\n")
    assert scan_candidate_use(tmp_path, INITIAL_CANDIDATES) == {}


def test_k6_control_rule_reports_unshimmed_candidate_only_in_planted_file(tmp_path):
    """The rule (not just the scanner): a candidate absent from the shims but
    present in a planted file is returned as missing; shimmed ones are not."""
    conf = _load_root_conftest()
    _plant(tmp_path, "src/a/m.py", 'run(["busctl", "x"])\nrun(["loginctl", "show-user"])\n')
    missing = candidates_missing_a_shim(
        tmp_path, conf.SERVICE_MANAGER_TOOL_CANDIDATES, conf._SERVICE_SHIMS
    )
    assert missing == {"busctl": ["src/a/m.py"]}, missing
    # with a shim for it, the rule is satisfied
    assert candidates_missing_a_shim(
        tmp_path, conf.SERVICE_MANAGER_TOOL_CANDIDATES, {**conf._SERVICE_SHIMS, "busctl": ()}
    ) == {}


def test_k6_control_rule_covers_every_initial_candidate_with_the_real_candidates(tmp_path):
    """Plant one use of each initial candidate in its own file; with no shims at
    all the rule, fed the REAL candidate tuple, must report every one."""
    conf = _load_root_conftest()
    for cand in INITIAL_CANDIDATES:
        _plant(tmp_path, f"src/a/{cand}_user.py", f'run(["{cand}", "x"])\n')
    missing = candidates_missing_a_shim(tmp_path, conf.SERVICE_MANAGER_TOOL_CANDIDATES, {})
    assert set(missing) == set(INITIAL_CANDIDATES), sorted(set(INITIAL_CANDIDATES) ^ set(missing))


# ------------------------------------------------------- K7 unchanged behaviour


def test_k7_refusal_message_text_is_unchanged(service_manager_shim_dir, tmp_path):
    log = _make_fake(tmp_path / "fake", "launchctl")
    r = _run_shim(
        service_manager_shim_dir, "launchctl", ["bootstrap", "x"], behind=tmp_path / "fake"
    )
    assert r.stderr.startswith(
        "[conftest.py guard #3519] refused launchctl bootstrap x: tests must not mutate "
        "the real service manager; stub launchctl first on PATH in the test (running test: "
    ), r.stderr
    assert _calls(log) == []
