"""engram-alpha#4021 -- the 2671 fallback test leaves a temporary `_hooklib` in
`sys.modules`, and the A6 parent-walk cell then fails (order-dependent).

The contract these cells state:

    C1. After the 2671 file runs in-process, `sys.modules.get("_hooklib")` is the
        object that was there before (absent if it was absent), and no
        `sys.modules` value has a `__file__` under that run's temporary tree.
    C2. The two-file repro (the 2671 file and the A6 parent-walk cell), in both
        orders, reports 0 failed, and the A6 cell really ran and passed.

Every cell runs a real pytest in a SUBPROCESS over the real modules; the probe is
a tiny plugin that records `sys.modules` once collection is done and again at
session end.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
POLLUTER = "tests/test_2671_hooks_survive_unreadable_plugin_root.py"
VICTIM = ("tests/test_engramd_a6_latency.py::"
          "test_the_parent_walk_probe_leaves_path_usable_for_class_level_calls")
TIMEOUT_S = 600

_PROBE = '''\
import json, os, sys

_KEEP = []  # keeps the start-of-run object alive so an identity compare is honest
_STATE = {}

def pytest_collection_finish(session):
    mod = sys.modules.get("_hooklib")
    _KEEP.append(mod)
    _STATE["start_id"] = None if mod is None else id(mod)

def pytest_sessionfinish(session, exitstatus):
    mod = sys.modules.get("_hooklib")
    _STATE["end_id"] = None if mod is None else id(mod)
    _STATE["end_file"] = getattr(mod, "__file__", None) if mod is not None else None
    base = os.path.realpath(os.environ["PROBE_4021_BASE"])
    under = []
    for name, m in list(sys.modules.items()):
        f = getattr(m, "__file__", None)
        if isinstance(f, str) and os.path.realpath(f).startswith(base + os.sep):
            under.append(name)
    _STATE["under_tmp"] = sorted(under)
    _STATE["exitstatus"] = int(exitstatus)
    with open(os.environ["PROBE_4021_OUT"], "w") as fh:
        json.dump(_STATE, fh)
'''


def _run_pytest(args, extra_env=None):
    env = dict(os.environ)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-q", "-p", "no:cacheprovider"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=TIMEOUT_S)


@pytest.mark.parametrize("preload", [True, False], ids=["hooklib-cached", "hooklib-absent"])
def test_the_2671_file_leaves_sys_modules_as_found(preload, tmp_path):
    """C1. Red at base: the temp-tree `_hooklib` (and the hook modules) stay cached.
    Mutation: drop the teardown restore -> red."""
    plugin_dir = tmp_path / "probe_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "probe4021.py").write_text(_PROBE)
    out = tmp_path / "probe-out.json"
    base = tmp_path / "basetemp"
    pypath = os.pathsep.join(
        p for p in (str(plugin_dir), os.environ.get("PYTHONPATH", "")) if p)
    args = [POLLUTER, "-p", "probe4021", f"--basetemp={base}"]
    if preload:
        # Cache a real `_hooklib` before the run, as an earlier test module would.
        (plugin_dir / "preload4021.py").write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(HOOKS_DIR)!r})\n"
            "import _hooklib  # noqa: F401\n"
            "sys.path.pop(0)\n")
        args += ["-p", "preload4021"]
    proc = _run_pytest(args, {"PYTHONPATH": pypath, "PROBE_4021_OUT": str(out),
                              "PROBE_4021_BASE": str(base)})
    assert out.is_file(), f"probe wrote nothing\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    state = json.loads(out.read_text())
    assert state["exitstatus"] == 0, proc.stdout[-2000:]
    assert (state["start_id"] is not None) == preload, "control: preload state"
    assert state["end_id"] == state["start_id"], (
        f"`_hooklib` was replaced or leaked: file {state['end_file']!r}")
    assert state["under_tmp"] == [], (
        f"modules left in sys.modules from the temporary tree: {state['under_tmp']}")


@pytest.mark.parametrize("first,second", [(POLLUTER, VICTIM), (VICTIM, POLLUTER)],
                         ids=["polluter-first", "victim-first"])
def test_the_pair_in_either_order_reports_no_failure(first, second, tmp_path):
    """C2. At base, polluter-first: 1 failed. The A6 cell must actually have run."""
    junit = tmp_path / "junit.xml"
    proc = _run_pytest([first, second, f"--junitxml={junit}"])
    out = proc.stdout + proc.stderr
    summary = [ln for ln in out.splitlines() if re.search(r"\d+ (passed|error|failed)", ln)]
    assert summary, out[-3000:]
    assert not re.search(r"\d+ (failed|errors?)\b", summary[-1]), summary[-1]
    assert proc.returncode == 0, out[-3000:]
    cases = list(ET.parse(junit).getroot().iter("testcase"))
    target = [c for c in cases
              if c.get("name") == "test_the_parent_walk_probe_leaves_path_usable_for_class_level_calls"]
    assert len(target) == 1, "control: the A6 target cell must have run exactly once"
    assert all(c.find(t) is None for c in target for t in ("failure", "error", "skipped"))
