"""Single-pick sites: prefer the checkout folder the process is working inside.

When both `~/engram` and `~/engram-alpha` exist and a site has no trust check, it picks the candidate the
process's own working folder is inside (real path equal or under it, so a linked worktree under
`~/engram-alpha/.claude/worktrees/<id>` is inside `~/engram-alpha`); otherwise the first existing of
`engram`, `engram-alpha`. Cases (test names keep the C-numbers):
  C13.1 cwd inside ~/engram-alpha, both exist -> ~/engram-alpha
  C13.2 cwd inside ~/engram, both exist       -> ~/engram
  C13.3 cwd elsewhere, both exist             -> ~/engram (the default)
  C13.4 only one exists                       -> that one, whatever the cwd
Also here: when no candidate holds the target file, fall back to the sole existing candidate directory
(cwd-first), so an old-name-only seat keeps its old value; and a candidate that is a symlink to a folder
elsewhere is matched by real path.
Sites: _hooklib.resolve_runtime_dir last rung, _hooklib.resolve_own_server_py_path, session-start hook
_resolve_mcp_server_path, lesson-tripwire hook _resolve_runtime_dir last rung,
supervision_probe.resolve_own_server_py_path, tools/rc-upgrade-verify.py REPO.
Not here: the launchers (they have a trust tie-break), the Piece C extras (list-all), agent-bootstrap.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_folder_name_independence_hooks import (  # noqa: E402
    HOOKS, TOOLS, _hooklib, _load, _session_hook,
)
from test_folder_name_independence_tools import _RC_DRIVER, RC, REPO  # noqa: E402

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

SITES = ["hooklib_runtime", "hooklib_server", "session_server", "tripwire", "supervision", "rc"]


def _layout(site, names):
    """(dirs to create, files to create) under the fake HOME for the existing candidate names."""
    if site in ("hooklib_runtime", "rc"):
        return [n for n in names], []
    if site == "tripwire":
        return [f"{n}/src/engram" for n in names], []
    return [], [f"{n}/server.py" for n in names]


def _expected(site, home, name):
    if site in ("hooklib_runtime", "rc"):
        return str(home / name)
    if site == "tripwire":
        return str(home / name / "src" / "engram")
    return str(home / name / "server.py")


def _run(site, tmp_path, monkeypatch, names, cwd, extra_dirs=(), alpha_symlink=False):
    """cwd: path relative to HOME (created if missing) the process sits in while the site resolves."""
    home = tmp_path / "home"
    home.mkdir()
    dirs, files = _layout(site, names)
    for d in dirs:
        (home / d).mkdir(parents=True)
    for f in files:
        (home / f).parent.mkdir(parents=True, exist_ok=True)
        (home / f).write_text("# fixture\n")
    for d in extra_dirs:
        (home / d).mkdir(parents=True, exist_ok=True)
    if alpha_symlink:
        # ~/engram-alpha becomes a symlink to a real folder elsewhere in the fake HOME
        (home / "elsewhere").mkdir()
        import shutil
        shutil.move(str(home / "engram-alpha"), str(home / "elsewhere" / "alpha-real"))
        os.symlink(home / "elsewhere" / "alpha-real", home / "engram-alpha")
    (home / cwd).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    eh = tmp_path / "eh"
    eh.mkdir()
    fake_hooks = tmp_path / "plugin" / "hooks"
    fake_hooks.mkdir(parents=True)

    if site == "rc":
        (eh / "knowledge.db").write_text("")
        env = {"HOME": str(home), "PATH": os.environ["PATH"], "ENGRAM_HOME": str(eh),
               "ENGRAM_PY": sys.executable}
        p = subprocess.run([sys.executable, "-c", _RC_DRIVER, str(RC), str(REPO)],
                           capture_output=True, text=True, env=env, timeout=60, cwd=str(home / cwd))
        line = [ln for ln in p.stdout.splitlines() if ln.startswith("REPO=")]
        assert line, p.stdout + p.stderr
        return home, line[0][5:]

    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    if site == "hooklib_runtime":
        mod = _hooklib()
        monkeypatch.chdir(home / cwd)
        return home, mod.resolve_runtime_dir(str(eh), "fni_no_such_sentinel.py")
    if site == "hooklib_server":
        mod = _hooklib()
        mod.__file__ = str(fake_hooks / "x.py")
        monkeypatch.chdir(home / cwd)
        return home, mod.resolve_own_server_py_path(str(eh))
    if site == "session_server":
        mod, _eh = _session_hook(tmp_path, monkeypatch)
        mod.__file__ = str(fake_hooks / "x.py")
        monkeypatch.chdir(home / cwd)
        return home, mod._resolve_mcp_server_path(str(eh))
    if site == "tripwire":
        stub = MagicMock()
        stub.write_baseline = MagicMock(return_value=None)
        monkeypatch.setitem(sys.modules, "context_tracker", stub)
        monkeypatch.setenv("ENGRAM_HOME", str(eh))
        mod = _load(HOOKS / "engram-lesson-tripwire-hook.py", "fni_tripwire_c13")
        mod.__file__ = str(fake_hooks / "x.py")
        mod.ENGRAM_HOME = str(eh)
        monkeypatch.chdir(home / cwd)
        return home, mod._resolve_runtime_dir()
    if site == "supervision":
        if str(TOOLS) not in sys.path:
            sys.path.append(str(TOOLS))
        import supervision_probe as sp
        tools_dir = tmp_path / "plugin" / "tools"
        tools_dir.mkdir(parents=True)
        monkeypatch.setattr(sp, "__file__", str(tools_dir / "supervision_probe.py"))
        monkeypatch.chdir(home / cwd)
        return home, sp.resolve_own_server_py_path(eh)
    raise AssertionError(site)


BOTH = ["engram", "engram-alpha"]


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("cwd", ["engram-alpha/sub", "engram-alpha/.claude/worktrees/abc123"])
def test_C13_1_cwd_inside_alpha_picks_alpha(site, cwd, tmp_path, monkeypatch):
    home, got = _run(site, tmp_path, monkeypatch, BOTH, cwd)
    assert got == _expected(site, home, "engram-alpha")


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("cwd", ["engram/sub", "engram/.claude/worktrees/abc123"])
def test_C13_2_cwd_inside_engram_picks_engram(site, cwd, tmp_path, monkeypatch):
    home, got = _run(site, tmp_path, monkeypatch, BOTH, cwd)
    assert got == _expected(site, home, "engram")


@pytest.mark.parametrize("site", SITES)
def test_C13_3_cwd_elsewhere_picks_engram(site, tmp_path, monkeypatch):
    home, got = _run(site, tmp_path, monkeypatch, BOTH, "umwelt")
    assert got == _expected(site, home, "engram")


@pytest.mark.parametrize("site", SITES)
def test_C13_3_cwd_is_the_checkout_root_itself(site, tmp_path, monkeypatch):
    home, got = _run(site, tmp_path, monkeypatch, BOTH, "engram-alpha")
    assert got == _expected(site, home, "engram-alpha")


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("only", BOTH)
def test_C13_4_only_one_exists_whatever_the_cwd(site, only, tmp_path, monkeypatch):
    home, got = _run(site, tmp_path, monkeypatch, [only], "umwelt")
    assert got == _expected(site, home, only)


# The separator property (`engram` must not prefix-match a cwd under `engram-alpha/...`) is pinned by
# test_C13_1_cwd_inside_alpha_picks_alpha and test_C13_3_cwd_is_the_checkout_root_itself: a prefix match
# without the separator would pick `engram` there and turn those red.


def test_C13_rc_prints_the_folder_it_chose():
    src = RC.read_text()
    assert 'print(f"USING: repo={REPO}' in src


# ---- fallback to the sole existing candidate directory ---------------------------------------------

SUFFIX_SITES = ["hooklib_server", "session_server", "tripwire", "supervision"]


@pytest.mark.parametrize("site", SUFFIX_SITES)
def test_fallback_a_only_alpha_dir_without_target_keeps_old_value(site, tmp_path, monkeypatch):
    home, got = _run(site, tmp_path, monkeypatch, [], "umwelt", extra_dirs=["engram-alpha"])
    assert got == _expected(site, home, "engram-alpha")


@pytest.mark.parametrize("site", SUFFIX_SITES)
def test_fallback_b_both_dirs_only_one_has_the_target(site, tmp_path, monkeypatch):
    home, got = _run(site, tmp_path, monkeypatch, ["engram-alpha"], "umwelt", extra_dirs=["engram"])
    assert got == _expected(site, home, "engram-alpha")


@pytest.mark.parametrize("site", SUFFIX_SITES)
def test_fallback_c_both_dirs_no_target_cwd_inside_alpha(site, tmp_path, monkeypatch):
    home, got = _run(site, tmp_path, monkeypatch, [], "engram-alpha/sub", extra_dirs=["engram", "engram-alpha"])
    assert got == _expected(site, home, "engram-alpha")


@pytest.mark.parametrize("site", SUFFIX_SITES)
def test_fallback_d_neither_dir_exists_is_the_engram_default(site, tmp_path, monkeypatch):
    home, got = _run(site, tmp_path, monkeypatch, [], "umwelt")
    assert got == _expected(site, home, "engram")


# ---- symlinked candidate: matched by real path ----------------------------------------------------

@pytest.mark.parametrize("site", ["hooklib_runtime", "hooklib_server", "supervision", "rc"])
def test_symlinked_alpha_cwd_inside_its_target_picks_alpha(site, tmp_path, monkeypatch):
    home, got = _run(site, tmp_path, monkeypatch, BOTH, "elsewhere/alpha-real/sub", alpha_symlink=True)
    assert got == _expected(site, home, "engram-alpha")
