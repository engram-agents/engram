"""Product-side twin of the #3519 test guard: the setup script refuses to
register a service for an ephemeral daemon path (#3519 item 1).

Background: on 2026-10-03 a test run from a fairy worktree registered a REAL
launchd job under com.engram.surface-daemon whose ProgramArguments pointed at a
deleted worktree's engram-surface-daemon.py; it respawned 27,605 times. The
conftest shims stop a TEST doing that; these cells pin that the SCRIPT itself
will not register a service for a daemon script under a temp root or an
agent/fairy worktree.

Contract (stated by the spec, asserted here):
  P1  physical path of the daemon script inside a temp root (/tmp, /private/tmp,
      /var/tmp, /private/var/tmp, /var/folders, /private/var/folders, and
      $TMPDIR when set AND non-empty) -> exit 2, before any plist/unit write and
      before any launchctl/systemctl call; one stderr ERROR line naming the
      resolved path, the reason, and the override. Empty/unset TMPDIR adds
      nothing (an empty string must never match every path).
  P2  a `/.claude/worktrees/` component pair in the resolved path -> same
      refusal. `my.claude/worktrees` and a bare `worktrees` do not match.
  P3  a normal path behaves exactly as before (no WARN/ERROR, files written).
  P4  `--allow-path` or ENGRAM_SETUP_ALLOW_PATH=1 (exactly 1) lets a refused path
      proceed with one loud WARN line naming the path; 0 / empty / true refuse.
  P5  the refusal precedes the OS branch: identical under uname=Linux/Darwin.
  P6  physical resolution: a symlink in a good dir pointing into a temp root
      refuses; a temp-looking symlink resolving to a good dir is allowed.

Good (non-temp, non-worktree) paths are built under a scratch dir created in the
REAL home directory, because every other writable place on a test host is a temp
root and the cells must discriminate "refused because temp" from "refused
because worktree". The scratch dir is removed on teardown; the cells skip when
no such base exists. Cells never set ENGRAM_SETUP_ALLOW_PATH except the P4
override cells. A recording spy launchctl/systemctl is FIRST on PATH.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SCRIPT = REPO_ROOT / "src" / "engram" / "tools" / "setup-surface-daemon-service.sh"

_TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp",
               "/var/folders", "/private/var/folders")


def _under(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return bool(root) and (path == root or path.startswith(root + "/"))


def _is_ephemeral(path: Path) -> bool:
    p = str(path.resolve())
    roots = list(_TEMP_ROOTS)
    if os.environ.get("TMPDIR"):
        roots.append(str(Path(os.environ["TMPDIR"]).resolve()))
    return any(_under(p, r) for r in roots) or "/.claude/worktrees/" in p + "/"


def _exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def good_base():
    """A scratch dir whose resolved path is neither a temp root nor a worktree."""
    home = Path.home()
    if _is_ephemeral(home):
        pytest.skip("real HOME is itself ephemeral; no good base available")
    base = Path(tempfile.mkdtemp(prefix=".engram-3519-", dir=home)).resolve()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def rig(tmp_path):
    """Stub dir (spy launchctl/systemctl, uname, python3) + temp HOME."""
    stub = tmp_path / "stub"
    stub.mkdir()
    log = tmp_path / "spy.log"
    log.write_text("")
    spy = f'#!/usr/bin/env bash\necho "$(basename "$0") $*" >> "{log}"\nexit 0\n'
    _exe(stub / "launchctl", spy)
    _exe(stub / "systemctl", spy)
    _exe(stub / "python3", '#!/usr/bin/env bash\necho "/usr/bin/python3"\n')
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)

    class Rig:
        pass

    r = Rig()
    r.stub, r.log, r.home = stub, log, home

    def run(hooks_dir, *, os_name="Darwin", env_extra=None, unset=(), args=()):
        _exe(stub / "uname", f'#!/usr/bin/env bash\necho "{os_name}"\n')
        env = {
            **os.environ,
            "PATH": f"{stub}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(home),
            "ENGRAM_HOME": str(home / ".engram"),
            "ENGRAM_HOOKS_DIR": str(hooks_dir),
            # #3939: the spy answers every launchctl verb 0, so the post-bootout wait would run its
            # full window; keep it short
            "ENGRAM_SETUP_LAUNCHD_POLL_S": "0.01", "ENGRAM_SETUP_LAUNCHD_WAIT_S": "0.05",
        }
        for k in ("ENGRAM_SETUP_ALLOW_PATH", *unset):
            env.pop(k, None)
        env.update(env_extra or {})
        return subprocess.run(["bash", str(SETUP_SCRIPT), *args],
                              capture_output=True, text=True, env=env, timeout=30)

    r.run = run
    r.spy_calls = lambda: [ln for ln in log.read_text().splitlines() if ln.strip()]
    r.written = lambda: [str(p) for p in home.rglob("*")
                         if p.suffix in (".plist", ".service")]
    return r


def _hooks(parent: Path, *parts: str) -> Path:
    d = parent.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    (d / "engram-surface-daemon.py").write_text("# stub daemon\n")
    return d


def _assert_refused(r, res, resolved: Path):
    out = res.stdout + res.stderr
    assert res.returncode == 2, out
    assert r.spy_calls() == [], r.spy_calls()
    assert r.written() == [], r.written()
    errs = [ln for ln in res.stderr.splitlines() if ln.startswith("ERROR")]
    assert len(errs) == 1, res.stderr
    assert str(resolved) in errs[0], errs[0]
    assert "ephemeral" in errs[0], errs[0]
    assert "--allow-path" in errs[0] and "ENGRAM_SETUP_ALLOW_PATH=1" in errs[0], errs[0]


def _assert_ran_normally(r, res):
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "WARN" not in out and "ERROR" not in out, out
    assert res.stderr == "", res.stderr
    assert r.spy_calls() != [], "the service manager should have been called"
    assert r.written(), "a plist/unit should have been written"


# --------------------------------------------------------------------- P1


@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
def test_p1_tmp_path_refused_before_any_write_or_service_call(rig, tmp_path, os_name):
    hooks = _hooks(tmp_path, "h")
    res = rig.run(hooks, os_name=os_name)
    _assert_refused(rig, res, (hooks / "engram-surface-daemon.py").resolve())


def test_p1b_custom_tmpdir_root_refused(rig, good_base):
    custom = good_base / "customtmp"
    hooks = _hooks(custom, "h")
    res = rig.run(hooks, env_extra={"TMPDIR": str(custom)})
    _assert_refused(rig, res, hooks / "engram-surface-daemon.py")


def test_p1b_tmpdir_prefix_is_component_bounded(rig, good_base):
    hooks = _hooks(good_base, "customtmp2", "h")
    res = rig.run(hooks, env_extra={"TMPDIR": str(good_base / "customtmp")})
    _assert_ran_normally(rig, res)


@pytest.mark.parametrize("mode", ["empty", "unset"])
def test_p1b_empty_or_unset_tmpdir_adds_nothing(rig, good_base, mode):
    hooks = _hooks(good_base, "h")
    res = rig.run(hooks, env_extra={"TMPDIR": ""} if mode == "empty" else None,
                  unset=("TMPDIR",) if mode == "unset" else ())
    _assert_ran_normally(rig, res)


# --------------------------------------------------------------------- P2


@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
def test_p2_claude_worktrees_refused(rig, good_base, os_name):
    hooks = _hooks(good_base, ".claude", "worktrees", "agent-x", "hooks")
    res = rig.run(hooks, os_name=os_name)
    _assert_refused(rig, res, hooks / "engram-surface-daemon.py")


@pytest.mark.parametrize("parts", [("my.claude", "worktrees", "h"),
                                   ("worktrees", "h"),
                                   (".claude", "worktrees2", "h"),
                                   (".claude", "x", "worktrees", "h")])
def test_p2_lookalikes_allowed(rig, good_base, parts):
    hooks = _hooks(good_base, *parts)
    res = rig.run(hooks)
    _assert_ran_normally(rig, res)


# --------------------------------------------------------------------- P3


@pytest.mark.parametrize("os_name", ["Darwin", "Linux"])
def test_p3_normal_path_unchanged(rig, good_base, os_name):
    hooks = _hooks(good_base, ".engram", "marketplace", "plugins", "engram", "hooks")
    res = rig.run(hooks, os_name=os_name)
    _assert_ran_normally(rig, res)
    assert "=== Setup complete ===" in res.stdout
    assert f"hooks dir:     {hooks}" in res.stdout
    if os_name == "Darwin":
        assert any(c.startswith("launchctl bootstrap") for c in rig.spy_calls())
    else:
        assert any("enable --now" in c for c in rig.spy_calls())


# --------------------------------------------------------------------- P4


def _assert_allowed_with_warn(r, res, path: Path):
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    warns = [ln for ln in out.splitlines() if ln.startswith("WARN")]
    assert len(warns) == 1, out
    assert str(path) in warns[0], warns[0]
    assert r.spy_calls() != [] and r.written(), out


def test_p4_flag_allows_with_warn(rig, tmp_path):
    hooks = _hooks(tmp_path, "h")
    res = rig.run(hooks, args=["--allow-path"])
    _assert_allowed_with_warn(rig, res, (hooks / "engram-surface-daemon.py").resolve())


def test_p4_env_one_allows_with_warn(rig, tmp_path):
    hooks = _hooks(tmp_path, "h")
    res = rig.run(hooks, env_extra={"ENGRAM_SETUP_ALLOW_PATH": "1"})
    _assert_allowed_with_warn(rig, res, (hooks / "engram-surface-daemon.py").resolve())


def test_p4_flag_allows_worktree_path_too(rig, good_base):
    hooks = _hooks(good_base, ".claude", "worktrees", "a", "h")
    res = rig.run(hooks, args=["--allow-path"])
    _assert_allowed_with_warn(rig, res, hooks / "engram-surface-daemon.py")


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "11", " 1"])
def test_p4_env_other_values_still_refuse(rig, tmp_path, value):
    hooks = _hooks(tmp_path, "h")
    res = rig.run(hooks, env_extra={"ENGRAM_SETUP_ALLOW_PATH": value})
    _assert_refused(rig, res, (hooks / "engram-surface-daemon.py").resolve())


def test_p4_allow_flag_does_not_warn_on_a_good_path(rig, good_base):
    hooks = _hooks(good_base, "h")
    res = rig.run(hooks, args=["--allow-path"])
    _assert_ran_normally(rig, res)


# --------------------------------------------------------------------- P5


def test_p5_refusal_identical_across_os(rig, tmp_path):
    hooks = _hooks(tmp_path, "h")
    results = {}
    for os_name in ("Linux", "Darwin"):
        res = rig.run(hooks, os_name=os_name)
        _assert_refused(rig, res, (hooks / "engram-surface-daemon.py").resolve())
        results[os_name] = (res.returncode, res.stdout, res.stderr)
    assert results["Linux"] == results["Darwin"]
    # nothing OS-specific (the banner names the OS) was printed before refusing
    assert results["Linux"][1] == ""


# --------------------------------------------------------------------- P6


def test_p6_file_symlink_in_good_dir_into_temp_refuses(rig, good_base, tmp_path):
    real = tmp_path / "real-daemon.py"
    real.write_text("# stub\n")
    hooks = good_base / "h"
    hooks.mkdir()
    (hooks / "engram-surface-daemon.py").symlink_to(real)
    res = rig.run(hooks)
    _assert_refused(rig, res, real.resolve())


def test_p6_dir_symlink_in_good_dir_into_temp_refuses(rig, good_base, tmp_path):
    real_hooks = _hooks(tmp_path, "h")
    (good_base / "linkdir").symlink_to(real_hooks)
    res = rig.run(good_base / "linkdir")
    _assert_refused(rig, res, (real_hooks / "engram-surface-daemon.py").resolve())


def test_p6_relative_file_symlink_into_worktree_refuses(rig, good_base):
    # the link target is relative; resolution must anchor it at the link's dir
    hooks = good_base / "h"
    hooks.mkdir()
    (hooks / "engram-surface-daemon.py").symlink_to("../.claude/worktrees/w/d.py")
    (good_base / ".claude" / "worktrees" / "w").mkdir(parents=True)
    (good_base / ".claude" / "worktrees" / "w" / "d.py").write_text("# stub\n")
    res = rig.run(hooks)
    _assert_refused(rig, res, good_base / ".claude" / "worktrees" / "w" / "d.py")


def test_p6_temp_looking_symlink_to_good_dir_allowed(rig, good_base, tmp_path):
    hooks = _hooks(good_base, "h")
    link = tmp_path / "looks-temp"
    link.symlink_to(hooks)
    res = rig.run(link)
    _assert_ran_normally(rig, res)


def test_p6_worktree_looking_symlink_to_good_dir_allowed(rig, good_base):
    _hooks(good_base, "h")
    (good_base / ".claude").mkdir()
    (good_base / ".claude" / "worktrees").symlink_to(good_base)
    res = rig.run(good_base / ".claude" / "worktrees" / "h")
    _assert_ran_normally(rig, res)
