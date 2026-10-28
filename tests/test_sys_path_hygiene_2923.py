"""engram-alpha#2923 -- two test modules together trip the #2565 sys.path guard.

The contract these cells state (C):

    A test module leaves the two things the #2565 guard reads as it found
    them: the cached `tools` package (the `sys.modules` entries `tools` and
    `tools.*`), and the order of the `sys.path` entries that contain a
    `tools/__init__.py`.

    C2. Run with no `tools` package cached, the state_writers module leaves no
    `tools` / `tools.*` module cached whose file is under `src/engram/tools`.

Neither the stop hook nor the guard is wrong. The hook's move of `src/engram`
to the front of `sys.path` (`_import_engramd_client`) is right in the hook's
own short-lived process; the guard in `tests/conftest.py`
(`_assert_tools_package_is_repo_root`) is doing its job. The leak is two test
modules running that code in a shared pytest process without restoring:

  * `tests/test_engramd_stage_profile_3678.py` runs the stop hook in-process
    (through `tools/engramd_stage_profile.py`'s timing wrappers) and leaves
    `src/engram` at `sys.path[0]`.
  * `tests/test_principle_trigger_state_writers_3521.py::_fresh_core` deleted
    every `tools` / `tools.*` entry from `sys.modules` in its `finally`, so the
    guard could no longer trust a cached repo-root `tools` and fell back to
    scanning `sys.path`.

Together: `src/engram` first AND no cached `tools` -> the guard's scan finds
`src/engram/tools` -> every later module errors. At base, the pair cells below
fail with 27 errors from the #2565 guard ("sys.path collision (#2565): ...
would resolve to '<repo>/src/engram/tools'"), all in
`tests/test_reconcile_owner_read_3824.py`.

Every cell here runs a real pytest in a SUBPROCESS over the real modules (the
probe is a tiny plugin that records the guard's two inputs at session start
and session end; it does not model the modules).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGE = "tests/test_engramd_stage_profile_3678.py"
WRITERS = "tests/test_principle_trigger_state_writers_3521.py"
VICTIM = "tests/test_reconcile_owner_read_3824.py"
TIMEOUT_S = 600

# A plugin loaded with `-p`. At session start (after conftest, before any test
# module) it imports the repo-root `tools` package -- the state a wide run is in
# by the time these modules run -- and records the guard's inputs; at session
# end it records them again. With PROBE_2923_PRELOAD=0 it skips the import (the
# state when the module is the first to touch `tools`). It also records which
# cached `tools` / `tools.*` modules live under src/engram/tools.
# It imports nothing from the modules under test.
_PROBE = '''\
import json, os, sys

def _snap():
    mod = sys.modules.get("tools")
    return {
        "tools_file": getattr(mod, "__file__", None) if mod is not None else None,
        "tools_keys": sorted(
            k for k in sys.modules if k == "tools" or k.startswith("tools.")),
        "src_tools_keys": sorted(
            k for k, m in list(sys.modules.items())
            if (k == "tools" or k.startswith("tools."))
            and os.sep + os.path.join("src", "engram", "tools")
            in (getattr(m, "__file__", "") or "")),
        "path_with_tools": [
            e for e in sys.path
            if os.path.isfile(os.path.join(e, "tools", "__init__.py"))],
    }

_STATE = {}

def pytest_sessionstart(session):
    if os.environ.get("PROBE_2923_PRELOAD", "1") == "1":
        import tools, tools._owner_read  # noqa: F401  (cache the repo-root package)
    _STATE["start"] = _snap()

def pytest_sessionfinish(session, exitstatus):
    _STATE["end"] = _snap()
    _STATE["exitstatus"] = int(exitstatus)
    with open(os.environ["PROBE_2923_OUT"], "w") as fh:
        json.dump(_STATE, fh)
'''


def _run_pytest(args, extra_env=None):
    env = dict(os.environ)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-q", "-p", "no:cacheprovider"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        timeout=TIMEOUT_S)


def _probe_module(module, tmp_path, preload=True):
    plugin_dir = tmp_path / "probe_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "probe2923.py").write_text(_PROBE)
    out = tmp_path / "probe-out.json"
    pypath = os.pathsep.join(
        p for p in (str(plugin_dir), os.environ.get("PYTHONPATH", "")) if p)
    proc = _run_pytest(
        [module, "-p", "probe2923"],
        {"PYTHONPATH": pypath, "PROBE_2923_OUT": str(out),
         "PROBE_2923_PRELOAD": "1" if preload else "0"})
    assert out.is_file(), (
        f"probe wrote nothing; pytest exit {proc.returncode}\n"
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    state = json.loads(out.read_text())
    assert state["exitstatus"] == 0, (
        f"{module} itself failed under the probe\n{proc.stdout[-2000:]}")
    return state


def _assert_contract_c(state, module):
    start, end = state["start"], state["end"]
    # control: the probe really started from a cached repo-root tools package
    assert start["tools_file"] == str(REPO_ROOT / "tools" / "__init__.py")
    assert len(start["path_with_tools"]) >= 2, start["path_with_tools"]
    assert end["tools_file"] == start["tools_file"], (
        f"{module} changed the cached `tools` package: "
        f"{start['tools_file']!r} -> {end['tools_file']!r}")
    missing = set(start["tools_keys"]) - set(end["tools_keys"])
    assert not missing, f"{module} evicted cached tools entries: {sorted(missing)}"
    assert end["path_with_tools"] == start["path_with_tools"], (
        f"{module} reordered the sys.path entries holding tools/__init__.py: "
        f"{start['path_with_tools']} -> {end['path_with_tools']}")


# --- per-module cells (contract C) ----------------------------------------------

def test_stage_profile_leaves_sys_path_order_as_found(tmp_path):
    """Red at base (half 2 unfixed): the module leaves `src/engram` ahead of the
    repo root among the sys.path entries holding a tools/__init__.py."""
    _assert_contract_c(_probe_module(STAGE, tmp_path), STAGE)


def test_state_writers_leaves_cached_tools_as_found(tmp_path):
    """Red at base (half 1 unfixed): `_fresh_core` deleted every `tools` /
    `tools.*` entry, evicting the cached repo-root package it found."""
    _assert_contract_c(_probe_module(WRITERS, tmp_path), WRITERS)


def test_state_writers_without_cached_tools_leaks_no_src_engram_tools(tmp_path):
    """C2. Run with no `tools` cached, `_fresh_core`'s restore must evict the
    `tools` / `tools.*` modules it imported from src/engram/tools. Not seen by
    the preloaded cell above: with `tools` cached, `sys.modules.update(snapshot)`
    alone hides a missing eviction step."""
    state = _probe_module(WRITERS, tmp_path, preload=False)
    leaked = state["end"]["src_tools_keys"]
    assert not leaked, (
        f"{WRITERS} left modules from src/engram/tools cached in sys.modules: "
        f"{leaked}")


# --- the pair (the #2923 symptom) -----------------------------------------------

@pytest.mark.parametrize("first,second", [(STAGE, WRITERS), (WRITERS, STAGE)],
                         ids=["stage-then-writers", "writers-then-stage"])
def test_the_pair_then_the_victim_has_no_sys_path_collision(first, second):
    """At base this reports 27 errors from the #2565 guard (every one in
    test_reconcile_owner_read_3824.py), in either order of the first two."""
    proc = _run_pytest([first, second, VICTIM])
    out = proc.stdout + proc.stderr
    assert "sys.path collision" not in out, out[-3000:]
    assert proc.returncode == 0, out[-3000:]
