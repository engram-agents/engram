"""#4093 precondition: five more test modules must open the #4029 window around their hook loads.

The #4029 guard (`_polluter_side_state_check` in tests/conftest.py) reads only
`sys.path[0]`, after the module's setup, so it misses a module whose in-process
hook loads leave entries deeper in `sys.path`, or whose leaked entry was already
in front. These cells measure the leak directly instead of through the guard:
each module is run ALONE in a child pytest with a tiny plugin that records
`sys.path` after collection and, at session finish, prints how many entries
ending in `src/engram/hooks/claude` were ADDED since (a multiset difference, so
15 identical entries count 15).

  * each of the five modules must show 0 added entries and 0 child errors;
  * a control module planted in tmp_path loads a hook in process with NO window
    and must show >= 1 (the instrument can say no);
  * the control with the window opened must show 0 (the control depends on the
    missing window, not on something else in the harness).

This file duplicates the child-run harness of tests/test_hook_load_optins_4091.py
(the plugin, `_run_child`, the two planted controls) ON PURPOSE, so the two PRs
stay independent of each other. A shared helper can follow once both are merged.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
HOOK_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-end-of-day-hook.py"

MODULES = [
    "tests/test_session_start_hook_forum_updates_monitor.py",
    "tests/test_resolve_runtime_dir_plugin.py",
    "tests/test_engram_surface_hook_idf_gate.py",
    "tests/test_resolve_engram_home_abspath.py",
    "tests/test_user_identity_owner_read_3197.py",
]

_MARKER = "HOOK_DIR_PATH_ENTRIES_ADDED="

_PLUGIN = '''\
"""Child-run plugin (#4093): count hook-dir entries added to sys.path since collection."""
import collections
import os
import sys

_SUFFIX = os.path.join("src", "engram", "hooks", "claude")
_baseline = collections.Counter()


def _hook_entries():
    return collections.Counter(
        p for p in sys.path
        if isinstance(p, str) and os.path.normpath(p).endswith(_SUFFIX))


def pytest_collection_finish(session):
    global _baseline
    _baseline = _hook_entries()


def pytest_sessionfinish(session, exitstatus):
    added = _hook_entries() - _baseline
    print("%s%d" % ("''' + _MARKER + '''", sum(added.values())))
'''

# Loads a hook in process the way the leaking modules do, with or without the window.
_PLANTED_NO_WINDOW = '''\
import importlib.util


def test_loads_a_hook_in_process():
    spec = importlib.util.spec_from_file_location("planted_eod_hook", {hook!r})
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "EOD_RE")
'''

_PLANTED_WITH_WINDOW = '''\
import importlib.util

from _hook_load_window import hook_load_window


def test_loads_a_hook_in_process():
    with hook_load_window():
        spec = importlib.util.spec_from_file_location("planted_eod_hook", {hook!r})
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert hasattr(module, "EOD_RE")
'''


def _run_child(target, cwd, work_dir):
    """Run `target` alone in a child pytest with the counting plugin.

    Returns (marker_count or None, error_count, completed process). The child gets
    a fresh ENGRAM_HOME under `work_dir` and writes no bytecode, so nothing lands in
    the repo."""
    plugin_dir = work_dir / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "leak_counter_4093.py").write_text(_PLUGIN)
    home = work_dir / "engram_home"
    home.mkdir()
    env = dict(os.environ)
    env["ENGRAM_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("R40_CHILD_PYTEST", None)
    extra = [str(plugin_dir), str(TESTS_DIR)]
    if env.get("PYTHONPATH"):
        extra.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(extra)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "leak_counter_4093", str(target)],
        capture_output=True, text=True, env=env, cwd=str(cwd), timeout=300,
    )
    out = proc.stdout
    found = re.findall(rf"{_MARKER}(\d+)", out)
    summary = [ln for ln in out.splitlines() if ln.strip()][-1] if out.strip() else ""
    err = re.search(r"(\d+) errors?\b", summary)
    return (int(found[-1]) if found else None, int(err.group(1)) if err else 0, proc)


def _assert_ran_and_counted(count, proc):
    # A child that never ran, or never reached sessionfinish, must not read as "0".
    assert count is not None, "no counter line from the child run:\n" + proc.stdout + proc.stderr
    assert re.search(r"\d+ passed", proc.stdout), proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "module", MODULES,
    ids=["session_start_forum_updates_monitor", "resolve_runtime_dir_plugin",
         "surface_hook_idf_gate", "resolve_engram_home_abspath",
         "user_identity_owner_read_3197"])
def test_module_leaves_no_hook_dir_on_sys_path(module, tmp_path):
    count, errors, proc = _run_child(module, REPO_ROOT, tmp_path)
    _assert_ran_and_counted(count, proc)
    assert count == 0, f"{module} left {count} hook-dir sys.path entries\n{proc.stdout}"
    assert errors == 0, f"{module} child run had {errors} error(s)\n{proc.stdout}"


def _plant(tmp_path, template):
    planted_dir = tmp_path / "planted"
    planted_dir.mkdir()
    path = planted_dir / "test_planted_loader_4093.py"
    path.write_text(template.format(hook=str(HOOK_PATH)))
    return path


def test_control_unwindowed_hook_load_is_counted(tmp_path):
    """A module that loads a hook in process with NO window: the instrument must say yes."""
    planted = _plant(tmp_path, _PLANTED_NO_WINDOW)
    count, _errors, proc = _run_child(planted, planted.parent, tmp_path)
    _assert_ran_and_counted(count, proc)
    assert count >= 1, f"the counter missed a known leak\n{proc.stdout}{proc.stderr}"


def test_control_windowed_hook_load_is_clean(tmp_path):
    """The same load inside the window: 0. Ties the control's count to the missing window."""
    planted = _plant(tmp_path, _PLANTED_WITH_WINDOW)
    count, _errors, proc = _run_child(planted, planted.parent, tmp_path)
    _assert_ran_and_counted(count, proc)
    assert count == 0, f"the window did not clean the planted load\n{proc.stdout}{proc.stderr}"
