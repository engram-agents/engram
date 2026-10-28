"""engram-alpha#4023 -- two order-dependent test polluters, plus the check that
names a polluter at its own module.

The contract these cells state:

    C1. After the a6 stop-measurement test runs, `sys.path` is exactly what it
        was before (the whole list, not just [0]).
    C2. After the concurrent hook-and-reset test runs, `sys.modules["tools"]`
        is the same object as before (identity); if absent before, absent after.
    C3. The three-part repro (A, E, then a victim module that imports
        `tools.*`) reports no errors, and the victim's tests actually ran.
    C4. A module-scope autouse fixture in `tests/conftest.py`: (hard) raises an
        error NAMING THE MODULE when a module evicts / replaces the `tools` that
        was cached at its setup; (hard since #4029) the same for a module that
        leaves `sys.path[0]` inside `src/engram`, with `sys.path` restored
        exactly first; it also still prints ONE session-end line
        `#4023 sys.path[0] leaks: N module(s): a, b` (sorted, only when N > 0).
        A clean module gets neither.

Mechanism (measured): (A) runs the stop hook in-process, whose module-level
`sys.path.insert(0, ...)` calls stay in the pytest process. (E) goes through
`_fresh_core_with_data_dir`, which deleted every cached `tools` / `tools.*`
entry. The #2565 guard trusts a cached `tools` and scans `sys.path` only when
none is cached, so (A) is silent until (E) -- then every later module that
imports `tools.*` errors.

Every cell runs a real pytest in a SUBPROCESS over the real modules (the probe
is a tiny plugin that records `sys.path` and the `tools` identity once
collection is done and again at session end; it does not model the modules).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
POLLUTER_A = ("tests/test_engramd_a6_latency.py::"
              "test_stop_measurement_compares_the_raw_count_and_names_the_build_on_any_host")
POLLUTER_E = ("tests/test_principle_triggers_registry.py::"
              "test_concurrent_hook_and_reset_writes_lose_no_update")
VICTIM = "tests/test_propose_generalizes_migration.py"
TIMEOUT_S = 600

_PROBE = '''\
import json, os, sys

_KEEP = []  # keeps the start-of-run `tools` alive so an identity compare is honest

def _snap(keep):
    mod = sys.modules.get("tools")
    if keep:
        _KEEP.append(mod)
    return {"path": list(sys.path),
            "tools_id": None if mod is None else id(mod),
            "tools_file": getattr(mod, "__file__", None) if mod is not None else None}

_STATE = {}

def pytest_collection_finish(session):
    # After conftest and every collected import, before any test or fixture runs.
    if os.environ.get("PROBE_4023_PRELOAD", "1") == "1":
        import tools  # noqa: F401  (cache the repo-root package)
    _STATE["start"] = _snap(True)

def pytest_sessionfinish(session, exitstatus):
    _STATE["end"] = _snap(False)
    _STATE["exitstatus"] = int(exitstatus)
    with open(os.environ["PROBE_4023_OUT"], "w") as fh:
        json.dump(_STATE, fh)
'''


def _env(extra=None):
    env = dict(os.environ)
    # #4163 round 6: the A6 polluters are `@isolated` (tests/_a6_isolated.py) -- unmarked, they
    # re-run in a grandchild and the probed process is never touched, so C1 / C3 go vacuous.
    # The marker makes the body run in THIS subprocess, which is the one the cells watch.
    env["A6_ISOLATED_CHILD"] = "1"
    env.update(extra or {})
    return env


def _run_pytest(args, extra_env=None, cwd=REPO_ROOT):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-q", "-p", "no:cacheprovider"],
        cwd=str(cwd), env=_env(extra_env), capture_output=True, text=True,
        timeout=TIMEOUT_S)


def _probe(selection, tmp_path, preload=True):
    plugin_dir = tmp_path / "probe_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "probe4023.py").write_text(_PROBE)
    out = tmp_path / "probe-out.json"
    pypath = os.pathsep.join(
        p for p in (str(plugin_dir), os.environ.get("PYTHONPATH", "")) if p)
    proc = _run_pytest(
        [selection, "-p", "probe4023"],
        {"PYTHONPATH": pypath, "PROBE_4023_OUT": str(out),
         "PROBE_4023_PRELOAD": "1" if preload else "0"})
    assert out.is_file(), (
        f"probe wrote nothing; pytest exit {proc.returncode}\n"
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    state = json.loads(out.read_text())
    assert state["exitstatus"] == 0, (
        f"{selection} itself failed under the probe\n{proc.stdout[-2000:]}")
    return state


# --- C1 / C2: each polluter leaves the process as it found it -------------------

def test_a6_stop_measurement_leaves_sys_path_exactly_as_found(tmp_path):
    """C1. Red at base: the in-process stop hook leaves `src/engram` at the front.
    Mutation: drop the module-scope sys.path restore -> red."""
    state = _probe(POLLUTER_A, tmp_path)
    assert not state["start"]["path"][0].startswith(str(REPO_ROOT / "src" / "engram")), (
        "control: probe started with src/engram already in front")
    assert state["end"]["path"] == state["start"]["path"], (
        f"{POLLUTER_A} changed sys.path:\n  before {state['start']['path']}\n"
        f"  after  {state['end']['path']}")


def test_concurrent_hook_and_reset_leaves_cached_tools_as_found(tmp_path):
    """C2 (cached). Red at base: `_fresh_core_with_data_dir` evicted the cached
    repo-root `tools`. Identity, not equality. Mutation: delete the restore
    after the purge -> red."""
    state = _probe(POLLUTER_E, tmp_path, preload=True)
    start, end = state["start"], state["end"]
    assert start["tools_file"] == str(REPO_ROOT / "tools" / "__init__.py"), "control"
    assert end["tools_id"] == start["tools_id"], (
        f"{POLLUTER_E} replaced or evicted the cached `tools`: "
        f"{start['tools_file']!r} -> {end['tools_file']!r}")


def test_concurrent_hook_and_reset_leaves_absent_tools_absent(tmp_path):
    """C2 (absent). With no `tools` cached, the `tools` the helper imported from
    src/engram during its polluted window must not survive. Mutation: restore
    the snapshot but skip the purge of entries added in the window -> red."""
    state = _probe(POLLUTER_E, tmp_path, preload=False)
    assert state["start"]["tools_id"] is None, "control: nothing cached at start"
    assert state["end"]["tools_id"] is None, (
        f"{POLLUTER_E} left a `tools` cached: {state['end']['tools_file']!r}")


# --- C3: the three-part repro ----------------------------------------------------

@pytest.mark.parametrize("first,second", [(POLLUTER_A, POLLUTER_E), (POLLUTER_E, POLLUTER_A)],
                         ids=["a-then-e", "e-then-a"])
def test_the_pair_then_the_victim_reports_no_errors(first, second, tmp_path):
    """C3. At base: 2 passed, 24 errors, in either order of the first two."""
    junit = tmp_path / "junit.xml"
    proc = _run_pytest([first, second, VICTIM, f"--junitxml={junit}"])
    out = proc.stdout + proc.stderr
    summary = [ln for ln in out.splitlines() if re.search(r"\d+ (passed|error|failed)", ln)]
    assert summary, out[-3000:]
    assert not re.search(r"\d+ errors?\b", summary[-1]), summary[-1]
    assert proc.returncode == 0, out[-3000:]
    cases = ET.parse(junit).getroot().iter("testcase")
    victim = [c for c in cases if "test_propose_generalizes_migration" in c.get("classname", "")]
    assert victim, "control: the victim module's tests must actually have run"
    assert all(c.find("error") is None and c.find("failure") is None for c in victim)


# --- C4: the polluter-side check -------------------------------------------------

_CLEAN = "def test_clean():\n    assert True\n"

_PLANT_PATH = textwrap.dedent('''\
    import os, sys
    import pytest
    import tools  # noqa: F401  (cached, as in a wide run: the #2565 guard trusts it)

    @pytest.fixture(scope="module", autouse=True)
    def _pollute():
        yield
        sys.path.insert(0, os.path.join(os.environ["REPO_ROOT_4023"], "src", "engram", "SUBDIR_4023"))

    def test_pollutes_path():
        assert True
    ''')

_PLANT_TOOLS = textwrap.dedent('''\
    import sys
    import pytest
    import tools  # noqa: F401  (cached at import, so there is something to evict)

    @pytest.fixture(scope="module", autouse=True)
    def _evict():
        yield
        sys.modules.pop("tools", None)

    def test_evicts_tools():
        assert True
    ''')


def _run_modules(tmp_path, modules):
    d = tmp_path / "planted"
    d.mkdir()
    for name, body in modules.items():
        (d / name).write_text(body)
    pypath = os.pathsep.join(
        p for p in (str(TESTS_DIR), os.environ.get("PYTHONPATH", "")) if p)
    # `-p conftest` loads the REAL tests/conftest.py as a plugin for modules that
    # live outside tests/, so the check under test is the shipped one.
    return _run_pytest(
        [str(d), "-p", "conftest", "--rootdir", str(d)],
        {"PYTHONPATH": pypath, "REPO_ROOT_4023": str(REPO_ROOT)}, cwd=d)


def test_a_path_leaking_module_errors_at_its_teardown_and_is_named_in_one_summary_line(tmp_path):
    """C4, path half: hard since #4029 (it was non-failing in #4023), and it still
    leaves the suite-produced trace. Two planted leakers plus a clean module: each
    leaker errors at its own teardown, the summary line appears once with N=2,
    names both, sorted, and not the clean one. Mutation: make the half
    non-failing again -> the returncode / error asserts are red; suppress the
    summary -> the line assert is red."""
    proc = _run_modules(tmp_path, {
        # distinct entries: a module that re-inserts the entry already in front is no delta
        "test_plant_path_b.py": _PLANT_PATH.replace("SUBDIR_4023", "tools"),
        "test_plant_path_a.py": _PLANT_PATH.replace("SUBDIR_4023", "hooks"),
        "test_ok.py": _CLEAN})
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out[-3000:]
    assert re.search(r"\b2 errors\b", out), out[-3000:]
    assert out.count("ERROR at teardown of test_pollutes_path") == 2, out[-3000:]
    assert "ERROR at teardown of test_clean" not in out, out[-3000:]
    lines = [ln for ln in out.splitlines() if ln.startswith("#4023 sys.path[0] leaks:")]
    assert lines == [
        "#4023 sys.path[0] leaks: 2 module(s): test_plant_path_a, test_plant_path_b"], out[-3000:]


def test_polluter_check_names_a_module_that_evicts_tools(tmp_path):
    """C4. Mutation: drop the `tools` half of the check -> red."""
    proc = _run_modules(tmp_path, {"test_plant_tools.py": _PLANT_TOOLS, "test_ok.py": _CLEAN})
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out[-3000:]
    assert "test_plant_tools" in out and "tools" in out, out[-3000:]
    assert "ERROR at teardown of test_evicts_tools" in out, out[-3000:]
    assert "ERROR at teardown of test_clean" not in out, out[-3000:]
    assert "#4023 sys.path[0] leaks" not in out, out[-3000:]


_PLANT_EVICT_AND_LEAK = textwrap.dedent('''\
    import os, sys, types
    import pytest
    import tools  # noqa: F401  (cached at import, so there is something to evict)

    @pytest.fixture(scope="module", autouse=True)
    def _evict_and_leak():
        yield
        sys.modules.pop("tools", None)
        # a submodule that was NOT present at setup (a stale copy from the polluted window)
        sys.modules["tools.planted_4023"] = types.ModuleType("tools.planted_4023")
        sys.path.insert(0, os.path.join(os.environ["REPO_ROOT_4023"], "src", "engram"))

    def test_evicts_and_leaks():
        assert True
    ''')

_FOLLOWER = textwrap.dedent('''\
    import os

    def test_follower_has_no_stale_tools_submodule():
        import sys
        assert "tools.planted_4023" not in sys.modules

    def test_follower_resolves_repo_root_tools():
        import tools
        assert os.path.abspath(tools.__file__) == os.path.join(
            os.environ["REPO_ROOT_4023"], "tools", "__init__.py"), tools.__file__
    ''')


def test_an_evictor_is_named_once_and_the_next_module_is_not_hit(tmp_path):
    """C4 tools half, no cascade: the check restores the `tools` captured at
    setup BEFORE it raises. The evictor also leaves src/engram in front, so
    without the restore the follower's #2565 guard scans sys.path and errors.
    Expect exactly one error (the evictor, named) and the follower passes.
    The evictor also leaves a `tools.planted_4023` key that was absent at its
    setup; the follower asserts it is gone (the restore must be exact, not
    overwrite-only).
    Mutation: drop the restore-before-raise -> the follower errors -> red.
    Mutation: overwrite-only (drop the pop of added keys) -> only the
    stale-submodule check goes red."""
    proc = _run_modules(tmp_path, {
        "test_plant_evict.py": _PLANT_EVICT_AND_LEAK, "test_zz_follower.py": _FOLLOWER})
    out = proc.stdout + proc.stderr
    assert "3 passed" in out, out[-3000:]
    assert re.search(r"\b1 error\b", out), out[-3000:]
    assert "ERROR at teardown of test_evicts_and_leaks" in out, out[-3000:]
    assert "test_plant_evict" in out, out[-3000:]
    assert "sys.path collision" not in out, out[-3000:]
    assert "test_follower_resolves_repo_root_tools" not in "".join(
        ln for ln in out.splitlines() if ln.startswith(("ERROR", "FAILED"))), out[-3000:]


def test_polluter_check_is_quiet_on_a_clean_module(tmp_path):
    """C4 negative control: a clean module under the same conftest passes, so
    the cells above fail because of the planted polluter and nothing else."""
    proc = _run_modules(tmp_path, {"test_ok.py": _CLEAN})
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
    assert "1 passed" in proc.stdout
    assert "#4023" not in proc.stdout + proc.stderr
