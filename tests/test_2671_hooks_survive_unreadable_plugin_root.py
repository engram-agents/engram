"""engram-alpha#2671: three per-prompt hooks crash at import on an
unreadable CLAUDE_PLUGIN_ROOT.

engram-deference-detector-prompt.py, engram-time-bar-hook.py, and
engram-loop-arm-stop-hook.py each build a `_LP_TOOLS_DIR` candidate list at
bare module-import time, resolving via `(c / "loop_prompt.py").exists()`.
Path.exists() raises OSError (not False) when a candidate directory exists
but is unreadable -- an unhandled raise there crashes the hook process
outright, before main() ever runs, since the resolution happens at import
time.

Guard is PER CANDIDATE, not one try/except around the whole search. The
first fix here wrapped the entire `next(...)` search in a single
try/except OSError, matching _hooklib.resolve_runtime_dir's shape -- but
that shape is only safe there because its raising candidate is already the
LAST rung tried. In these three hooks, $CLAUDE_PLUGIN_ROOT is the FIRST
candidate and the __file__-relative walk-parents fallbacks come after; a
whole-search guard aborts on the first raise and never reaches those
fallbacks, discarding a candidate that would have resolved -- exactly the
issue's reported scenario (an unreadable plugin root, readable real files
underneath). A colleague review round on the first fix reproduced this:
the whole-search variant returns None for [bad, good] candidate order but
finds the good candidate for [good, bad]; the per-candidate variant finds
it both ways. Two module-level viz siblings (viz/data/config.py's
_find_tools_parent, viz/data/stats.py's _find_indexer_dir) carry the
identical shape and got the identical fix.

These tests run each hook as a REAL SUBPROCESS (not an in-process reload)
specifically to prove the actual shipped script survives, mirroring how
Claude Code itself invokes a hook -- an in-process import test could pass
while the on-disk script still crashes for an unrelated reason (stale
bytecode, a different Python, etc.).

Two-sided control: an unreadable root must not crash, AND a readable root
must still work -- proving the guard tolerates the bad case without
silently breaking the good one. A THIRD, mixed-order case (bad-then-good
and good-then-bad) is what actually discriminates the whole-search guard
from the per-candidate one -- the wholly-bad and wholly-good cases alone
don't (both variants agree on those), which is why they didn't catch the
first fix's bug. Checked to fail: reverting any one of the five guards
back to the whole-search shape reproduces a wrong (None-when-resolvable)
result or an uncaught traceback for that site specifically (verified
manually, see PR #2673 description -- not a permanent test, since it
requires temporarily un-fixing shipped code).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parent.parent / "src" / "engram" / "hooks" / "claude"
PYTHON = sys.executable

_HOOKS = [
    "engram-deference-detector-prompt.py",
    "engram-time-bar-hook.py",
    "engram-loop-arm-stop-hook.py",
]


def _run_hook(hook_name: str, plugin_root: Path, engram_home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    env["ENGRAM_HOME"] = str(engram_home)
    return subprocess.run(
        [PYTHON, str(HOOKS_DIR / hook_name)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


@pytest.mark.skipif(
    os.geteuid() == 0 if hasattr(os, "geteuid") else False,
    reason="root bypasses file permission bits -- chmod 0o000 would not "
           "actually block traversal, making this test vacuously true in a "
           "root-run CI container rather than a real check",
)
@pytest.mark.parametrize("hook_name", _HOOKS)
def test_hook_survives_unreadable_plugin_root(hook_name: str, tmp_path: Path) -> None:
    engram_home = tmp_path / "home"
    engram_home.mkdir()

    blocked_root = tmp_path / "blocked"
    blocked_root.mkdir()
    blocked_root.chmod(0o000)
    try:
        blocked = _run_hook(hook_name, blocked_root, engram_home)
    finally:
        blocked_root.chmod(0o755)  # restore so tmp_path cleanup can remove it

    assert blocked.returncode == 0, (
        f"{hook_name} crashed under an unreadable CLAUDE_PLUGIN_ROOT "
        f"(exit {blocked.returncode}):\n{blocked.stderr}"
    )
    assert "Traceback" not in blocked.stderr, (
        f"{hook_name} printed a traceback under an unreadable "
        f"CLAUDE_PLUGIN_ROOT even though it exited 0:\n{blocked.stderr}"
    )

    # Control: the SAME hook, a READABLE root, must also exit clean -- proves
    # the OSError guard doesn't silently swallow a real, findable candidate
    # too (the failure mode a bare `except OSError: return SOMETHING_WRONG`
    # could introduce without this half of the check).
    readable_root = tmp_path / "readable"
    readable_root.mkdir()
    readable = _run_hook(hook_name, readable_root, engram_home)
    assert readable.returncode == 0, (
        f"{hook_name} failed under a normal readable CLAUDE_PLUGIN_ROOT "
        f"(exit {readable.returncode}):\n{readable.stderr}"
    )
    assert "Traceback" not in readable.stderr


SRC_ENGRAM = Path(__file__).parent.parent / "src" / "engram"


def _build_real_deployed_layout(hook_name: str, root: Path) -> Path:
    """Populate <root>/hooks/<hook_name> and <root>/tools/loop_prompt.py --
    a real, readable, deployed (flattened) layout the walk-parents fallback
    should find. Also copies every sibling _*.py lib module (_hooklib.py
    etc.) alongside the hook, matching the real deployed layout where they
    are siblings in the same hooks/ dir -- engram-deference-detector-prompt.py
    imports _hooklib unguarded at module top level, so it must be present
    for the hook to load at all, not just for the code path under test.
    Returns the hook's copied path."""
    (root / "hooks").mkdir(parents=True)
    (root / "tools").mkdir(parents=True)
    hook_dest = root / "hooks" / hook_name
    shutil.copy(SRC_ENGRAM / "hooks" / "claude" / hook_name, hook_dest)
    for lib in (SRC_ENGRAM / "hooks" / "claude").glob("_*.py"):
        shutil.copy(lib, root / "hooks" / lib.name)
    shutil.copy(SRC_ENGRAM / "tools" / "loop_prompt.py", root / "tools" / "loop_prompt.py")
    return hook_dest


def _load_hook_fresh(hook_path: Path, module_name: str):
    """Load hook_path under a unique module_name, clearing any cached
    loop_prompt module first so the top-level resolution block re-runs
    against the current sys.path/env rather than reusing a prior load.
    Mirrors test_deference_detector_prompt.py's own _load_hook_fresh."""
    sys.modules.pop("loop_prompt", None)
    spec = importlib.util.spec_from_file_location(module_name, str(hook_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(
    os.geteuid() == 0 if hasattr(os, "geteuid") else False,
    reason="root bypasses file permission bits",
)
@pytest.mark.parametrize("hook_name", _HOOKS)
def test_hook_resolves_via_fallback_when_plugin_root_unreadable(hook_name, tmp_path, monkeypatch):
    """The discriminating case: CLAUDE_PLUGIN_ROOT (candidate 1, checked
    FIRST) points at an unreadable directory, but the hook's own real
    on-disk location (candidate 2+, via walk-parents) is perfectly
    readable and has loop_prompt.py right there. A whole-search
    try/except aborts on candidate 1's raise and never reaches candidate
    2, silently degrading to the local fallback constant -- which is
    indistinguishable from success by exit-code-and-traceback alone (both
    exit 0, no traceback). The per-candidate guard must resolve
    _LP_TOOLS_DIR to the real deployed tools/ dir here, not None."""
    blocked_plugin_root = tmp_path / "unreadable_plugin_root"
    blocked_plugin_root.mkdir()
    blocked_plugin_root.chmod(0o000)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(blocked_plugin_root))

    deployed_root = tmp_path / "real_deployed"
    hook_path = _build_real_deployed_layout(hook_name, deployed_root)

    saved_syspath = sys.path[:]
    try:
        hook = _load_hook_fresh(hook_path, f"_test_2671_{hook_name.replace('-', '_').replace('.', '_')}")
        assert hook._LP_TOOLS_DIR == deployed_root / "tools", (
            f"{hook_name}: expected fallback resolution to {deployed_root / 'tools'}, "
            f"got {hook._LP_TOOLS_DIR!r} -- the unreadable CLAUDE_PLUGIN_ROOT candidate "
            "aborted the whole search instead of being skipped"
        )
    finally:
        blocked_plugin_root.chmod(0o755)  # restore so tmp_path cleanup can remove it
        sys.path[:] = saved_syspath
        sys.modules.pop("loop_prompt", None)


# ---------------------------------------------------------------------------
# viz/data/config.py and viz/data/stats.py -- named in #2671 as "may ride
# along if the same one-line shape applies cleanly". Same OSError hazard
# (Path.is_file(), not Path.exists(), but empirically raises identically on
# an unreadable ancestor), same never-raises fix. Unit tests, not
# subprocess, since these are plain Optional[Path]-returning functions
# rather than full hook scripts with a stdin/stdout contract.
# ---------------------------------------------------------------------------


# Imported as proper package modules (not importlib.util.spec_from_file_location)
# because both use package-relative imports internally (stats.py's
# `from .. import agents`), which only resolve when the module is loaded
# through its real package -- conftest.py already puts src/engram on
# sys.path for exactly this (see test_viz_schema_ssot.py for the same
# convention).
from viz.data import config as _viz_config  # noqa: E402
from viz.data import stats as _viz_stats  # noqa: E402


@pytest.mark.skipif(
    os.geteuid() == 0 if hasattr(os, "geteuid") else False,
    reason="root bypasses file permission bits",
)
def test_find_tools_parent_survives_unreadable_ancestor(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o000)
    try:
        result = _viz_config._find_tools_parent(blocked / "some" / "nested" / "start")
    finally:
        blocked.chmod(0o755)
    assert result is None


@pytest.mark.skipif(
    os.geteuid() == 0 if hasattr(os, "geteuid") else False,
    reason="root bypasses file permission bits",
)
def test_find_indexer_dir_survives_unreadable_ancestor(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o000)
    try:
        result = _viz_stats._find_indexer_dir(blocked / "some" / "nested" / "start")
    finally:
        blocked.chmod(0o755)
    assert result is None
