"""Cells for the service-manager guard's reach over ALL THREE test roots (#3519 items 2+3).

#3522 put a guard in tests/conftest.py, so only tests under tests/ got it. The
other two roots CI runs -- src/forum/tests/ and tools/test_*.py -- inherited the
real PATH (and on a Mac, the real `launchctl`). Contract (stated by the spec):

  G1  a test under any of tests/, src/forum/tests/, tools/test_*.py, invoked as
      CI does (from the repo root, `python3 -m pytest <path>`), has the shim dir
      first on PATH: a mutating call reaches no real tool and exits 99, a
      read-only call passes through.
  G3  a test that stubs launchctl itself (stub prepended after the guard) wins.
  G5d the real forum / tools files still pass or skip as before.

Each cell builds a throwaway repo layout in tmp_path (the real root conftest.py,
pytest.ini and tests/conftest.py copied in; package markers as in the real
tree), puts a recording SPY `launchctl` behind the shim on the outer PATH, runs
`python -m pytest` on a tiny test file in that root, and reads the spy log.
A broken guard therefore reaches the spy, never a real tool.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

ROOTS = {
    "tests": "tests/test_probe_3519.py",
    "forum": "src/forum/tests/test_probe_3519.py",
    "tools": "tools/test_probe_3519.py",
}

PROBE = '''\
import os, shutil, subprocess

def _shim_is_first(tool):
    found = shutil.which(tool)
    assert found and "guard #3519" in open(found).read(), found

def _run(*args):
    return subprocess.run(list(args), capture_output=True, text=True)

def test_mutating_refused():
    r = _run("launchctl", "bootout", "gui/501/com.example.x")
    assert r.returncode == 99, (r.returncode, r.stdout, r.stderr)
    assert "#3519" in r.stderr
    r = _run("systemctl", "--user", "restart", "x.service")
    assert r.returncode == 99, (r.returncode, r.stdout, r.stderr)

def test_readonly_passes_through():
    _shim_is_first("launchctl")
    _shim_is_first("systemctl")
    r = _run("launchctl", "list")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    r = _run("systemctl", "--user", "is-active", "x.service")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

def _make_stub(tmp_path):
    stub = tmp_path / "stubbin"
    stub.mkdir()
    for tool in ("launchctl", "systemctl"):
        p = stub / tool
        p.write_text("#!/bin/bash\\necho STUB-$0\\nexit 7\\n")
        p.chmod(0o755)
    return stub

def _assert_stub_wins():
    r = _run("launchctl", "bootout", "gui/501/com.example.x")
    assert r.returncode == 7 and "STUB-" in r.stdout, (r.returncode, r.stdout, r.stderr)
    r = _run("systemctl", "restart", "x.service")
    assert r.returncode == 7, (r.returncode, r.stdout, r.stderr)

def test_own_stub_wins_set_in_test_body(tmp_path, monkeypatch):
    _shim_is_first("launchctl")
    stub = _make_stub(tmp_path)
    monkeypatch.setenv("PATH", str(stub) + os.pathsep + os.environ["PATH"])
    _assert_stub_wins()

import pytest

@pytest.fixture
def _stub_on_path(tmp_path, monkeypatch):
    stub = _make_stub(tmp_path)
    monkeypatch.setenv("PATH", str(stub) + os.pathsep + os.environ["PATH"])

def test_own_stub_wins_set_in_fixture(_stub_on_path):
    _assert_stub_wins()
'''


def _spy(directory: Path, log: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for tool in ("launchctl", "systemctl"):
        p = directory / tool
        p.write_text(f'#!/bin/bash\necho "{tool} $*" >> "{log}"\nexit 0\n')
        p.chmod(p.stat().st_mode | stat.S_IXUSR)


def _layout(tmp_path: Path) -> Path:
    """A throwaway repo root carrying the real conftest files and package markers."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "src" / "forum" / "tests").mkdir(parents=True)
    (root / "tools").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "pytest.ini", root / "pytest.ini")
    shutil.copy(REPO_ROOT / "tests" / "conftest.py", root / "tests" / "conftest.py")
    if (REPO_ROOT / "conftest.py").exists():
        shutil.copy(REPO_ROOT / "conftest.py", root / "conftest.py")
    for init in ("src/forum/__init__.py", "src/forum/tests/__init__.py", "tools/__init__.py"):
        (root / init).write_text("")
    return root


def _outer_env(spy_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env["PATH"] = f"{spy_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_probe(tmp_path: Path, root_key: str, select: str):
    root = _layout(tmp_path)
    rel = ROOTS[root_key]
    (root / rel).write_text(PROBE)
    log = tmp_path / "spy.log"
    log.write_text("")
    spy_dir = tmp_path / "spybin"
    _spy(spy_dir, log)
    cmd = [sys.executable, "-m", "pytest", rel, "-q", "-p", "no:cacheprovider",
           "--color=no", "-k", select]
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, env=_outer_env(spy_dir))
    return r, log.read_text().splitlines()


@pytest.mark.parametrize("root_key", sorted(ROOTS))
def test_c_a_mutating_call_refused_spy_sees_nothing(tmp_path, root_key):
    r, spy = _run_probe(tmp_path, root_key, "mutating_refused")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 passed" in r.stdout, r.stdout
    assert spy == [], spy


@pytest.mark.parametrize("root_key", sorted(ROOTS))
def test_c_b_readonly_call_passes_through_to_spy(tmp_path, root_key):
    r, spy = _run_probe(tmp_path, root_key, "readonly_passes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 passed" in r.stdout, r.stdout
    assert spy == ["launchctl list", "systemctl --user is-active x.service"], spy


@pytest.mark.parametrize("root_key", sorted(ROOTS))
def test_c_c_tests_own_stub_wins(tmp_path, root_key):
    r, spy = _run_probe(tmp_path, root_key, "own_stub_wins")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 passed" in r.stdout, r.stdout
    assert spy == [], spy


def test_c_d_real_root_files_still_pass_with_empty_spy(tmp_path):
    """The real forum installer tests and the real tools alarm test, run from the
    repo root as CI does, behind a recording spy: nothing reaches the spy, and
    no new failure appears. Flask can be absent (a Mac): the 2 failures in
    test_installer_baton_gate.py are the pre-existing flask-import failures, so
    the assertion is 'no failure other than those two', not 'all green'."""
    log = tmp_path / "spy.log"
    log.write_text("")
    spy_dir = tmp_path / "spybin"
    _spy(spy_dir, log)
    env = _outer_env(spy_dir)
    env["PATH"] = f"{spy_dir}{os.pathsep}{os.environ['PATH']}"
    files = [
        "src/forum/tests/test_installer.py",
        "src/forum/tests/test_installer_baton_gate.py",
        "src/forum/tests/test_backup_alarm_units.py",
        "tools/test_forum_backup_alarm.py",
    ]
    r = subprocess.run([sys.executable, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider",
                        "-rf", "--color=no"],
                       cwd=REPO_ROOT, capture_output=True, text=True, env=env)
    failed = [ln for ln in r.stdout.splitlines() if ln.startswith("FAILED")]
    allowed = ("test_installed_layout_loads_the_gate_from_app_tools",
               "test_control_layout_without_the_installer_copy_cannot_load_the_gate")
    unexpected = [ln for ln in failed if not any(a in ln for a in allowed)]
    assert not unexpected, r.stdout[-3000:]
    assert log.read_text().splitlines() == []
