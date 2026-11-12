"""Hook and tool fallbacks: `engram` is tried first, then `engram-alpha`; neither -> the `engram` default.

  Piece C default extras of the session-start hook (ENGRAM_PIECE_C_REPOS unset); test names keep C3.n:
    C3.1 env override set and non-empty: unchanged (wins)
    C3.2 only ~/engram a directory      -> [~/engram, ENGRAM_HOME]
    C3.3 only ~/engram-alpha            -> [~/engram-alpha, ENGRAM_HOME]
    C3.4 both directories               -> engram first, alpha second, ENGRAM_HOME last;
                                           same real path (symlink) -> listed once (engram name)
    C3.5 neither                        -> [ENGRAM_HOME]
  Last-rung fallbacks: _hooklib.resolve_runtime_dir and resolve_own_server_py_path, the session-start hook's
  _resolve_mcp_server_path, the lesson-tripwire hook's _resolve_runtime_dir, supervision_probe's
  resolve_own_server_py_path: first existing of [engram, engram-alpha] candidate; neither -> `engram`.
  (The extra rule for a working folder inside one candidate lives in test_folder_name_independence_cwd_c13.py.)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "src" / "engram" / "hooks" / "claude"
TOOLS = REPO / "src" / "engram" / "tools"

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _home(tmp_path, monkeypatch, dirs=(), files=()):
    home = tmp_path / "home"
    home.mkdir()
    for d in dirs:
        (home / d).mkdir(parents=True)
    for f in files:
        (home / f).parent.mkdir(parents=True, exist_ok=True)
        (home / f).write_text("# fixture\n")
    monkeypatch.setenv("HOME", str(home))
    return home


def _session_hook(tmp_path, monkeypatch, piece_c=None):
    eh = tmp_path / "eh"
    eh.mkdir(exist_ok=True)
    monkeypatch.setenv("ENGRAM_HOME", str(eh))
    if piece_c is None:
        monkeypatch.delenv("ENGRAM_PIECE_C_REPOS", raising=False)
    else:
        monkeypatch.setenv("ENGRAM_PIECE_C_REPOS", piece_c)
    stub = MagicMock()
    stub.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub)
    return _load(HOOKS / "engram-session-start-hook.py", "fni_session_start_hook"), eh


# ---- S3 ---------------------------------------------------------------------

def test_C3_1_override_wins(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, dirs=["engram", "engram-alpha"])
    mod, eh = _session_hook(tmp_path, monkeypatch, piece_c=str(tmp_path / "x"))
    assert mod.PIECE_C_REPOS == [str(tmp_path / "x"), mod.ENGRAM_HOME]


def test_C3_2_only_engram(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, dirs=["engram"])
    mod, eh = _session_hook(tmp_path, monkeypatch)
    assert mod.PIECE_C_REPOS == [str(home / "engram"), mod.ENGRAM_HOME]


def test_C3_3_only_alpha(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, dirs=["engram-alpha"])
    mod, eh = _session_hook(tmp_path, monkeypatch)
    assert mod.PIECE_C_REPOS == [str(home / "engram-alpha"), mod.ENGRAM_HOME]


def test_C3_4_both_engram_first_alpha_second(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, dirs=["engram", "engram-alpha"])
    mod, eh = _session_hook(tmp_path, monkeypatch)
    assert mod.PIECE_C_REPOS == [str(home / "engram"), str(home / "engram-alpha"), mod.ENGRAM_HOME]


def test_C3_4_symlink_listed_once(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, dirs=["engram"])
    os.symlink(home / "engram", home / "engram-alpha")
    mod, eh = _session_hook(tmp_path, monkeypatch)
    assert mod.PIECE_C_REPOS == [str(home / "engram"), mod.ENGRAM_HOME]


def test_C3_4_symlink_other_direction_listed_once(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, dirs=["engram-alpha"])
    os.symlink(home / "engram-alpha", home / "engram")
    mod, eh = _session_hook(tmp_path, monkeypatch)
    assert mod.PIECE_C_REPOS == [str(home / "engram"), mod.ENGRAM_HOME]


def test_C3_5_neither(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    mod, eh = _session_hook(tmp_path, monkeypatch)
    assert mod.PIECE_C_REPOS == [mod.ENGRAM_HOME]


# ---- S9: _hooklib ------------------------------------------------------------

def _hooklib():
    return _load(HOOKS / "_hooklib.py", "fni_hooklib")


def _runtime_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    eh = tmp_path / "eh"
    eh.mkdir(exist_ok=True)
    # a sentinel that exists nowhere, so every real rung misses and the last-ditch is returned
    return _hooklib().resolve_runtime_dir(str(eh), "fni_no_such_sentinel.py")


def test_S9_runtime_only_engram(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, dirs=["engram"])
    assert _runtime_dir(tmp_path, monkeypatch) == str(home / "engram")


def test_S9_runtime_only_alpha(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, dirs=["engram-alpha"])
    assert _runtime_dir(tmp_path, monkeypatch) == str(home / "engram-alpha")


def test_S9_runtime_both_engram_first(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, dirs=["engram", "engram-alpha"])
    assert _runtime_dir(tmp_path, monkeypatch) == str(home / "engram")


def test_S9_runtime_neither_default_is_engram(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    assert _runtime_dir(tmp_path, monkeypatch) == str(home / "engram")


def _server_path(modfile_dir: Path, mod, tmp_path):
    mod.__file__ = str(modfile_dir / "x.py")
    eh = tmp_path / "eh_no_server"
    eh.mkdir(exist_ok=True)
    return mod.resolve_own_server_py_path(str(eh)) if mod.__name__ == "fni_hooklib" \
        else mod._resolve_mcp_server_path(str(eh))


def _fake_hooks_dir(tmp_path):
    d = tmp_path / "plugin" / "hooks"
    d.mkdir(parents=True)
    return d


@pytest.mark.parametrize("which", ["hooklib", "session"])
def test_S9_S10_server_py_cases(tmp_path, monkeypatch, which):
    def run(dirs_files, sub):
        t = tmp_path / sub
        t.mkdir()
        home = _home(t, monkeypatch, files=dirs_files)
        if which == "hooklib":
            mod = _hooklib()
        else:
            mod, _eh = _session_hook(t, monkeypatch)
        return home, _server_path(_fake_hooks_dir(t), mod, t)

    home, got = run(["engram/server.py"], "a")
    assert got == str(home / "engram" / "server.py")
    home, got = run(["engram-alpha/server.py"], "b")
    assert got == str(home / "engram-alpha" / "server.py")
    home, got = run(["engram/server.py", "engram-alpha/server.py"], "c")
    assert got == str(home / "engram" / "server.py")
    home, got = run([], "d")
    assert got == str(home / "engram" / "server.py")


# ---- S11: lesson-tripwire ----------------------------------------------------

def _tripwire_last_ditch(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    stub = MagicMock()
    stub.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub)
    eh = tmp_path / "eh"
    eh.mkdir(exist_ok=True)
    monkeypatch.setenv("ENGRAM_HOME", str(eh))
    mod = _load(HOOKS / "engram-lesson-tripwire-hook.py", "fni_tripwire")
    mod.__file__ = str(tmp_path / "plugin" / "hooks" / "x.py")
    mod.ENGRAM_HOME = str(eh)
    return mod._resolve_runtime_dir()


def test_S11_only_engram(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, dirs=["engram/src/engram"])
    assert _tripwire_last_ditch(tmp_path, monkeypatch) == str(home / "engram" / "src" / "engram")


def test_S11_only_alpha(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, dirs=["engram-alpha/src/engram"])
    assert _tripwire_last_ditch(tmp_path, monkeypatch) == str(home / "engram-alpha" / "src" / "engram")


def test_S11_both_engram_first(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, dirs=["engram/src/engram", "engram-alpha/src/engram"])
    assert _tripwire_last_ditch(tmp_path, monkeypatch) == str(home / "engram" / "src" / "engram")


def test_S11_neither_default_is_engram(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    assert _tripwire_last_ditch(tmp_path, monkeypatch) == str(home / "engram" / "src" / "engram")


# ---- S12: supervision_probe --------------------------------------------------

def _probe(tmp_path, monkeypatch):
    sys.path.append(str(TOOLS))
    import supervision_probe as sp
    tools_dir = tmp_path / "plugin" / "tools"
    tools_dir.mkdir(parents=True)
    monkeypatch.setattr(sp, "__file__", str(tools_dir / "supervision_probe.py"))
    eh = tmp_path / "eh_no_server"
    eh.mkdir()
    return sp.resolve_own_server_py_path(eh)


def test_S12_cases(tmp_path, monkeypatch):
    for sub, files, want in [
        ("a", ["engram/server.py"], "engram/server.py"),
        ("b", ["engram-alpha/server.py"], "engram-alpha/server.py"),
        ("c", ["engram/server.py", "engram-alpha/server.py"], "engram/server.py"),
        ("d", [], "engram/server.py"),
    ]:
        t = tmp_path / sub
        t.mkdir()
        home = _home(t, monkeypatch, files=files)
        assert _probe(t, monkeypatch) == str(home / want)
