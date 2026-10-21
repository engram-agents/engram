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

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
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


REPO_ROOT = Path(__file__).resolve().parent.parent
# Names the temp-tree hook import can leave behind with no `__file__`.
_TEMP_TREE_NAMES = frozenset({"_hooklib", "_harness", "_stophooklib"})


def _pop_roots(tmp_path_factory) -> tuple:
    """The only directories an added module may come from and still be popped at
    teardown: pytest's basetemp (the temp deployed tree) and `<repo>/src/engram`."""
    return tuple(os.path.realpath(str(p)) + os.sep for p in (
        tmp_path_factory.getbasetemp(), REPO_ROOT / "src" / "engram"))


@pytest.fixture(autouse=True)
def _restore_sys_modules_and_path(tmp_path_factory):
    """#4021: the fallback test below imports a hook and its sibling `_hooklib`
    from a temporary deployed tree. Left cached, that temp `_hooklib` makes the
    A6 parent-walk cell fail when it runs later in the same process. At
    teardown, put `sys.modules` and `sys.path` back as found: restore the exact
    object for every key the test replaced or removed, and pop an ADDED key
    only if it came from pytest's basetemp or `<repo>/src/engram` (or is one of
    the `__file__`-less names the temp-tree import uses). The roots are
    deliberately not the whole repo or the system temp dir: a `.venv` inside
    the repo, or a checkout under the system temp dir, would sweep in
    third-party modules. Any other added key (a
    stdlib or third-party module first imported during the test) stays: a later
    re-import would build a second object while earlier importers keep the
    first."""
    modules_before = dict(sys.modules)
    path_before = sys.path[:]
    roots = _pop_roots(tmp_path_factory)
    try:
        yield
    finally:
        for key in [k for k in sys.modules if k not in modules_before]:
            f = getattr(sys.modules[key], "__file__", None)
            if isinstance(f, str):
                ours = os.path.realpath(f).startswith(roots)
            else:
                ours = key in _TEMP_TREE_NAMES or key.startswith("_test_2671_")
            if ours:
                del sys.modules[key]
        for key, mod in modules_before.items():
            if sys.modules.get(key) is not mod:
                sys.modules[key] = mod
        sys.path[:] = path_before


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


# engram-alpha#2677: the two tests above only cover the wholly-unreadable
# case, which the old whole-walk-wrapped try/except and the current
# per-candidate one satisfy identically (both correctly return None when
# EVERY candidate raises). Neither test can fail against the round-1 shape
# fixed in #2671/#2673 -- the same class of gap the hooks' own
# test_hook_resolves_via_fallback_when_plugin_root_unreadable exists to
# close. These two are the discriminating case for the viz resolvers:
# a NEARER ancestor is unreadable, a FARTHER one is readable and carries
# the target file. A whole-walk guard aborts on the first raise (the
# nearer, unreadable candidates) and never reaches the farther, resolvable
# one; the per-candidate guard skips past them and finds it.


@pytest.mark.skipif(
    os.geteuid() == 0 if hasattr(os, "geteuid") else False,
    reason="root bypasses file permission bits",
)
def test_find_tools_parent_resolves_past_nearer_unreadable_ancestor(tmp_path):
    outer = tmp_path / "outer"
    (outer / "tools").mkdir(parents=True)
    (outer / "tools" / "config_schema.py").write_text("# stub\n")
    # #3983: `outer` is the tree root (the walk is bounded at the nearest engram_core.py).
    (outer / "engram_core.py").write_text("")

    blocked = outer / "blocked"
    blocked.mkdir()
    start = blocked / "nested" / "start"
    blocked.chmod(0o000)
    try:
        result = _viz_config._find_tools_parent(start)
    finally:
        blocked.chmod(0o755)  # restore so tmp_path cleanup can remove it

    assert result == outer, (
        f"expected fallback resolution to {outer}, got {result!r} -- the "
        "unreadable nearer candidate aborted the whole walk instead of "
        "being skipped"
    )


@pytest.mark.skipif(
    os.geteuid() == 0 if hasattr(os, "geteuid") else False,
    reason="root bypasses file permission bits",
)
def test_find_indexer_dir_resolves_past_nearer_unreadable_ancestor(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "engram_log_indexer.py").write_text("# stub\n")
    # #3983: `outer` is the tree root (the walk is bounded at the nearest engram_core.py).
    (outer / "engram_core.py").write_text("")

    blocked = outer / "blocked"
    blocked.mkdir()
    start = blocked / "nested" / "start"
    blocked.chmod(0o000)
    try:
        result = _viz_stats._find_indexer_dir(start)
    finally:
        blocked.chmod(0o755)  # restore so tmp_path cleanup can remove it

    assert result == outer, (
        f"expected fallback resolution to {outer}, got {result!r} -- the "
        "unreadable nearer candidate aborted the whole walk instead of "
        "being skipped"
    )

# ---------------------------------------------------------------------------
# #4021 round 2/3: the teardown restore must not pop a module that merely got
# imported during a test and lives outside the roots it owns (pytest's basetemp
# and <repo>/src/engram). Re-importing it later would build a second object
# while earlier importers still hold the first. A pair, because a test cannot
# observe its own teardown: the first plants two tiny modules the test creates
# itself (so they can never be pre-imported and the cell can never skip) --
# one under the system temp dir but outside basetemp, one under the repo but
# outside src/engram -- and imports them, plus the temp-tree `_hooklib`; the
# second (it runs right after, in definition order) checks what survived.
# ---------------------------------------------------------------------------

_TEARDOWN_PROBE: dict = {}


def test_4021_teardown_probe_imports_outside_modules_and_temp_hooklib(tmp_path, monkeypatch):
    import uuid

    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    # With a `_hooklib` already cached the hook reuses it and never loads the temp
    # copy; drop it so the import really comes from the temp tree (the autouse
    # fixture puts the original back at teardown).
    sys.modules.pop("_hooklib", None)
    hook_path = _build_real_deployed_layout("engram-deference-detector-prompt.py", tmp_path / "deployed")
    _load_hook_fresh(hook_path, "_test_4021_probe_hook")
    temp_lib = sys.modules.get("_hooklib")
    assert temp_lib is not None and str(tmp_path) in str(getattr(temp_lib, "__file__", "")), (
        "control: the hook import must have cached a temp-tree _hooklib")

    planted: dict = {}
    made = []
    try:
        # Outside basetemp, under the system temp dir. Nothing is planted inside the
        # checkout (#2126: the suite never writes into the repo it runs from); the
        # repo-root arm is pinned by the roots cell below instead.
        d = tempfile.mkdtemp(prefix="tmp_4021_")
        made.append(d)
        name = f"_probe4021_systemtmp_{uuid.uuid4().hex}"
        (Path(d) / f"{name}.py").write_text("VALUE = 1\n")
        sys.path.insert(0, d)  # restored by the autouse fixture
        assert name not in sys.modules, "control: a fresh name cannot be pre-imported"
        planted[name] = importlib.import_module(name)
    finally:
        for d in made:
            shutil.rmtree(d, ignore_errors=True)
    assert len(planted) == 1
    _TEARDOWN_PROBE.update(planted=planted, temp_lib=temp_lib)


def test_4021_teardown_keeps_outside_modules_and_drops_the_temp_hooklib():
    if not _TEARDOWN_PROBE:
        pytest.skip("the probe test did not run (deselected or skipped)")
    for name, mod in _TEARDOWN_PROBE["planted"].items():
        assert sys.modules.get(name) is mod, (
            f"{name!r} (outside basetemp and src/engram) was popped at teardown "
            "(identity split for any earlier importer)")
    assert sys.modules.get("_hooklib") is not _TEARDOWN_PROBE["temp_lib"], (
        "the temp-tree _hooklib survived teardown")


def test_4021_pop_roots_are_exactly_basetemp_and_src_engram(tmp_path_factory):
    """#4021: the teardown may pop only modules from these two roots. Adding the
    whole repo (a `.venv` inside it) or the system temp dir (a checkout under it)
    would sweep in third-party modules and split their identity."""
    expected = (
        os.path.realpath(str(tmp_path_factory.getbasetemp())) + os.sep,
        os.path.realpath(str(REPO_ROOT / "src" / "engram")) + os.sep,
    )
    assert _pop_roots(tmp_path_factory) == expected
