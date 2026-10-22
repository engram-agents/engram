"""#3863 step 3a: the hooks, the hook-imported libraries, the client and bootstrap refuse a relative ENGRAM_HOME.

Ruling: Borges's rulings on #3863, 2026-10-21 (hooks refuse-then-exit-0; hook-imported modules degrade;
entry points raise; see src/engram/engram_home_resolver.py for the step-2 text).

Contract, one parametrised cell per module so a path that accepts the relative value reddens only its cell:
  h*  each of the four hooks, run as a script with ENGRAM_HOME=rel/x: exit 0, exactly ONE stderr line that
      contains the value, nothing written under the cwd. Unset / absolute / `~/x` behave as before
      (`~/x` is normalised into os.environ["ENGRAM_HOME"]).
  l*  _stophooklib.resolve_transcript_path -> None, _stale_tree_hooklib.load_watch_list -> [] on a refused
      value (a marker/config planted under cwd/rel/x must NOT be read); absolute still works.
  c*  engram_client (config loader and EngramClient(db_dir=...)) raises ValueError naming ENGRAM_HOME.
  b*  bootstrap.py raises ValueError naming the value BEFORE importing server (a stub server beside a copy of
      bootstrap.py in a flat dir proves the order and the flattened layout).
  f*  step-2 follow-ups: closed fd 2, docstrings, one citation of the step-2 rulings.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
HOOKS = SRC / "hooks" / "claude"
REL = "rel/x"

# hook file -> the module global holding the resolved home
HOOK_VARS = {
    "engram-lesson-tripwire-hook.py": "ENGRAM_HOME",
    "engram-loop-arm-stop-hook.py": "ENGRAM_HOME",
    "engram-stale-tree-warn-hook.py": "ENGRAM_HOME",
    "engram-time-bar-hook.py": "ENGRAM_DIR",
}


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("ENGRAM_HOME", "CLAUDE_PLUGIN_ROOT")}
    env.update({"HOME": str(tmp_path / "_home"), "PYTHONDONTWRITEBYTECODE": "1"})
    env.update(extra)
    (tmp_path / "_home").mkdir(exist_ok=True)
    return env


def _tree(cwd):
    out = []
    for p, dirs, files in os.walk(cwd):
        if Path(p) == Path(cwd) and "_home" in dirs:
            dirs.remove("_home")
        out += [str(Path(p) / f) for f in files] + [str(Path(p) / d) for d in dirs]
    return sorted(out)


def _lines(text):
    return [l for l in text.splitlines() if l.strip()]


# ------------------------------------------------------------------------------------------------ h: the hooks

@pytest.mark.parametrize("hook", list(HOOK_VARS))
def test_h1_a_relative_home_exits_zero_one_stderr_line_writes_nothing(hook, tmp_path):
    r = subprocess.run([sys.executable, str(HOOKS / hook)], cwd=tmp_path, env=_env(tmp_path, ENGRAM_HOME=REL),
                       input="{}", capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", r.stdout
    assert len(_lines(r.stderr)) == 1 and REL in r.stderr, r.stderr
    assert "ENGRAM_HOME must be an absolute path, got 'rel/x'" in r.stderr
    assert _tree(tmp_path) == [], _tree(tmp_path)


_PROBE = """
import os, runpy, sys
g = runpy.run_path(sys.argv[1], run_name="_probe_3863")
print(os.environ.get("ENGRAM_HOME", "<unset>"))
print(g[sys.argv[2]])
"""


def _probe(hook, tmp_path, **env):
    r = subprocess.run([sys.executable, "-c", _PROBE, str(HOOKS / hook), HOOK_VARS[hook]], cwd=tmp_path,
                       env=_env(tmp_path, **env), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return _lines(r.stdout)[-2:]


@pytest.mark.parametrize("hook", list(HOOK_VARS))
def test_h2_unset_stays_unset_and_resolves_the_default(hook, tmp_path):
    env_after, var = _probe(hook, tmp_path)
    assert env_after == "<unset>"
    assert var == str(tmp_path / "_home" / ".engram")


@pytest.mark.parametrize("hook", list(HOOK_VARS))
def test_h3_an_absolute_value_is_normalised_into_the_env_and_the_global(hook, tmp_path):
    raw = str(tmp_path / "a" / ".." / "b")
    env_after, var = _probe(hook, tmp_path, ENGRAM_HOME=raw)
    assert env_after == var == str(tmp_path / "b")


@pytest.mark.parametrize("hook", list(HOOK_VARS))
def test_h4_a_tilde_value_is_expanded_into_the_env_and_the_global(hook, tmp_path):
    env_after, var = _probe(hook, tmp_path, ENGRAM_HOME="~/elsewhere")
    assert env_after == var == str(tmp_path / "_home" / "elsewhere")


@pytest.mark.parametrize("hook", list(HOOK_VARS))
@pytest.mark.parametrize("how", ["tilde", "dotdot"])
def test_h5_the_hook_normalises_the_env_itself_when_the_sibling_lib_cannot_run(hook, how, tmp_path):
    """The inline block is there so the hook works WITHOUT _hooklib (which also normalises the env on import).
    Copy the hook beside a STUB _hooklib that prints the env it sees at import and exits: the value must already
    be absolute. Mutation: drop the hook's own `os.environ[...] = str(...)` -> only this cell is red (the probe
    cells above pass because the real _hooklib repairs the env afterwards)."""
    d = tmp_path / "hookdir"
    d.mkdir()
    shutil.copy(HOOKS / hook, d / hook)
    (d / "_hooklib.py").write_text(
        "import os, sys\nprint('SEEN=' + os.environ.get('ENGRAM_HOME', '<unset>'))\nsys.exit(0)\n")
    raw, want = (("~/elsewhere", tmp_path / "_home" / "elsewhere") if how == "tilde"
                 else (str(tmp_path / "a" / ".." / "b"), tmp_path / "b"))
    r = subprocess.run([sys.executable, str(d / hook)], cwd=tmp_path, env=_env(tmp_path, ENGRAM_HOME=raw),
                       input="{}", capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert f"SEEN={want}" in r.stdout, (r.stdout, r.stderr)


# ------------------------------------------------------------------------------------------- l: hook libraries

def _load_by_path(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    return mod


@pytest.fixture
def hooklibs(monkeypatch, tmp_path):
    """Load the two libraries with a clean env (their sibling _hooklib resolves the home at import)."""
    monkeypatch.setattr(sys, "path", list(sys.path))  # the libraries insert their own dir at sys.path[0]; undo it
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "_home"))
    (tmp_path / "_home").mkdir(exist_ok=True)
    return (_load_by_path(HOOKS / "_stophooklib.py", "_stophooklib_3a"),
            _load_by_path(HOOKS / "_stale_tree_hooklib.py", "_stale_tree_hooklib_3a"))


def _plant_marker(root, transcript):
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / "s1.json").write_text(json.dumps({"transcript_path": str(transcript)}))


def test_l1_stophooklib_resolve_transcript_path_returns_none_on_a_refused_home(hooklibs, tmp_path, monkeypatch):
    stop, _ = hooklibs
    t = tmp_path / "t.jsonl"
    t.write_text("{}\n")
    _plant_marker(tmp_path / "rel" / "x", t)  # what a cwd-anchored read would find
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGRAM_HOME", REL)
    assert stop.resolve_transcript_path(None, "s1") is None


def test_l2_stophooklib_still_resolves_under_an_absolute_and_the_default_home(hooklibs, tmp_path, monkeypatch):
    stop, _ = hooklibs
    t = tmp_path / "t.jsonl"
    t.write_text("{}\n")
    _plant_marker(tmp_path / "abs", t)
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "abs"))
    assert stop.resolve_transcript_path(None, "s1") == str(t)
    monkeypatch.delenv("ENGRAM_HOME")
    _plant_marker(tmp_path / "_home" / ".engram", t)
    assert stop.resolve_transcript_path(None, "s1") == str(t)


def _plant_config(root):
    root.mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"stale_tree_watch": [{"path": "/r", "branch": "main"}]}))


def test_l3_stale_tree_load_watch_list_returns_empty_on_a_refused_home(hooklibs, tmp_path, monkeypatch):
    _, stale = hooklibs
    _plant_config(tmp_path / "rel" / "x")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGRAM_HOME", REL)
    assert stale.load_watch_list() == []


def test_l4_stale_tree_load_watch_list_still_reads_an_absolute_and_the_default_home(hooklibs, tmp_path, monkeypatch):
    _, stale = hooklibs
    _plant_config(tmp_path / "abs")
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "abs"))
    assert stale.load_watch_list() == [{"path": "/r", "branch": "main"}]
    monkeypatch.delenv("ENGRAM_HOME")
    _plant_config(tmp_path / "_home" / ".engram")
    assert stale.load_watch_list() == [{"path": "/r", "branch": "main"}]


# ------------------------------------------------------------------------------------------------- c: the client

@pytest.fixture
def client_mod(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SRC))
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    import engram_client
    return engram_client


def test_c1_the_config_loader_refuses_a_relative_home(client_mod, tmp_path, monkeypatch):
    (tmp_path / "rel" / "x").mkdir(parents=True)
    (tmp_path / "rel" / "x" / "config.json").write_text('{"mode": "multi"}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGRAM_HOME", REL)
    with pytest.raises(ValueError) as e:
        client_mod._load_engram_config()
    assert "ENGRAM_HOME" in str(e.value) and REL in str(e.value)


def test_c2_the_config_loader_still_reads_an_absolute_and_the_default_home(client_mod, tmp_path, monkeypatch):
    (tmp_path / "abs").mkdir()
    (tmp_path / "abs" / "config.json").write_text('{"mode": "multi"}')
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "abs"))
    assert client_mod._load_engram_config() == {"mode": "multi"}
    monkeypatch.delenv("ENGRAM_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert client_mod._load_engram_config() == {}


def test_c3_a_relative_db_dir_is_refused_and_the_env_is_untouched(tmp_path):
    """Runs in a SUBPROCESS: a constructor that wrongly accepts the value purges sys.modules and rebinds
    ENGRAM_HOME, which would poison every later test in this process."""
    code = ("import os, sys\nsys.path.insert(0, %r)\nimport engram_client\n"
            "try:\n    engram_client.EngramClient(db_dir='rel/db')\n"
            "except Exception as e:\n    print(type(e).__name__, e)\n"
            "print('ENV=' + os.environ.get('ENGRAM_HOME', '<unset>'))\n") % str(SRC)
    abs_home = str(tmp_path / "abs")
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=_env(tmp_path, ENGRAM_HOME=abs_home),
                       capture_output=True, text=True, timeout=120)
    first = r.stdout.splitlines()[0] if r.stdout else ""
    assert first.startswith("ValueError") and "ENGRAM_HOME" in first and "rel/db" in first, (r.stdout, r.stderr)
    assert f"ENV={abs_home}" in r.stdout, r.stdout  # nothing was written before the refusal
    assert _tree(tmp_path) == [], _tree(tmp_path)


# -------------------------------------------------------------------------------------------- b: bootstrap.py

def _flat_bootstrap(tmp_path):
    """bootstrap.py + the resolver in a bare dir (the deployed layout), with a stub server that records the
    import. A copy of bootstrap.py that imported server BEFORE refusing would hit the stub."""
    flat = tmp_path / "plugin_root"
    flat.mkdir()
    shutil.copy(SRC / "bootstrap.py", flat / "bootstrap.py")
    shutil.copy(SRC / "engram_home_resolver.py", flat / "engram_home_resolver.py")
    (flat / "server.py").write_text('raise SystemExit("STUB-SERVER-IMPORTED")\n')
    return flat


def _run_bootstrap(tmp_path, **env):
    flat = _flat_bootstrap(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    base = _env(tmp_path, ALPHA_DIR=str(tmp_path), ALPHA_TEMPLATES_DIR=str(tmp_path), CLAUDE_HOME=str(tmp_path / "ch"))
    base.update(env)
    return subprocess.run([sys.executable, str(flat / "bootstrap.py")], cwd=work, env=base, capture_output=True,
                          text=True, timeout=120)


def test_b1_a_relative_home_raises_valueerror_naming_the_value_before_server_is_imported(tmp_path):
    r = _run_bootstrap(tmp_path, ENGRAM_HOME=REL)
    assert r.returncode != 0
    assert "ValueError" in r.stderr and "ENGRAM_HOME must be an absolute path, got 'rel/x'" in r.stderr, r.stderr
    assert "STUB-SERVER-IMPORTED" not in r.stderr, r.stderr


def test_b2_an_absolute_home_reaches_the_server_import(tmp_path):
    r = _run_bootstrap(tmp_path, ENGRAM_HOME=str(tmp_path / "abs"))
    assert "STUB-SERVER-IMPORTED" in r.stderr, r.stderr


def test_b3_an_unset_home_is_still_a_keyerror(tmp_path):
    r = _run_bootstrap(tmp_path)
    assert r.returncode != 0 and "KeyError" in r.stderr and "ENGRAM_HOME" in r.stderr, r.stderr


# --------------------------------------------------------------------------------------- f: step-2 follow-ups

def test_f1_a_closed_stderr_does_not_make_engram_home_or_none_raise(tmp_path):
    """(a) The refusal line is printed to a stderr that may be closed (a hook with fd 2 shut). Expected GREEN on
    dev already (the print is guarded): this cell pins it. Mutation: drop the guard -> red. `os._exit` skips the
    interpreter's own stderr flush at shutdown (exit 120 on a closed fd 2), which is outside the function."""
    code = ("import os, sys\nsys.path.insert(0, %r)\nimport engram_home_resolver as r\nos.close(2)\n"
            "assert r.engram_home_or_none() is None\nos.write(1, b'ok')\nos._exit(0)\n") % str(SRC)
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=_env(tmp_path, ENGRAM_HOME=REL),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout == "ok", (r.returncode, r.stdout, r.stderr)


def _docstring(path, func):
    """A function's docstring read from the SOURCE (no import: importing the daemon modules leaks sys.path)."""
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func)
    return ast.get_docstring(fn) or ""


def test_f2_engramd_client_docstrings_and_the_hook_io_comment_name_the_relative_home_raise():
    for func in ("read", "write"):
        doc = _docstring(SRC / "engramd_client.py", func)
        assert "ValueError" in doc and "ENGRAM_HOME" in doc and "socket_path=None" in doc, func
    io = (HOOKS / "_engramd_owner_hook_io.py").read_text()
    line = next(l for l in io.splitlines() if "raise-free" in l)
    assert "ValueError" in line or "relative ENGRAM_HOME" in line, line


def test_f3_engramd_owner_resolve_engram_home_docstring_no_longer_says_the_env_branch_anchors():
    doc = _docstring(SRC / "engramd_owner.py", "_resolve_engram_home")
    assert "ValueError" in doc and "refuse" in doc.lower(), doc
    assert "Always returns an ABSOLUTE path" not in doc, doc


def test_f4_the_step2_rulings_are_cited_once_in_the_resolver_docstring_not_repeated():
    hits = sorted(str(p.relative_to(REPO)) for p in (REPO / "src").rglob("*.py")
                  if "step-2 rulings" in p.read_text(errors="replace"))
    assert hits == ["src/engram/engram_home_resolver.py"], hits
