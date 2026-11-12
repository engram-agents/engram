"""Tests for the plugin-root probe in the runtime-dir resolver.

Issue #642: clean plugin install degrades to FTS-only because ENGRAM_HOME
is data-only (no engram_client.py) and ~/engram-alpha doesn't exist on a
public user's machine. The fix adds a plugin-root probe (priority 3, before
the ~/engram-alpha fallback) that walks two dirname() levels up from __file__
to locate <plugin_root>/engram_client.py.

Retargeted for gh#2570 (2026-09-05, maintainer's ruling on the coder-fairy's
flagged gap): the resolver this file tests used to be an independent
`_resolve_runtime_dir(engram_home)` copy inside each of
engram-surface-hook.py / engram-session-start-hook.py /
engram-surface-daemon.py / engram-utility-credit-mention-stop.py (with
engram-utility-credit-mention-stop.py taking a different sentinel,
engram_ids.py). gh#2570 hoisted that logic into ONE shared function,
`_hooklib.resolve_runtime_dir(engram_home, sentinel)`, and deleted the four
local copies. Retiring this file instead of retargeting it was considered
and rejected: TestResolveRuntimeDirWalkParents holds the regression coverage
for #1712 (worktree-nesting) and #1727 (permission-error-mid-walk) -- i.e.
the tests for the two properties gh#2570's entire point was to PROPAGATE.
Deleting the guard on the thing being spread would be exactly the
coverage-erosion shape gh#2570 exists to fix. So: every scenario class below
is retargeted at `_hooklib.resolve_runtime_dir`, not dropped.

What changed, precisely:
  - Four hook-specific loaders (one per file, extracting `_resolve_runtime_dir`
    by name) collapse to two hooklib-loaders (real location / fake location),
    parameterized by SENTINEL instead of by hook file -- because the four
    hooks migrated by gh#2570 now share the identical implementation, so what
    varies observably between them is the sentinel argument, not the code
    path. `test_plugin_layout_*` / `test_scatter_no_regression_*` /
    `test_shadow_regression_*` / `test_scatter_only_*` /
    `test_explicit_override_*` keep one test method per FORMER subject
    (surface-hook, session-start-hook, surface-daemon, utility-credit) so the
    method names and their git-blame history stay traceable, even though
    three of the four now exercise byte-identical code with the same
    sentinel.
  - TestResolveRuntimeDirWalkParents collapses its per-hook-file list to two
    SUBJECTS: the untouched engram-lesson-tripwire-hook.py (still its own,
    no-argument `_resolve_runtime_dir` -- named here explicitly, per the
    maintainer's instruction, as "one copy remains, deliberately, pending a
    look at whether it is the same thing": its different signature means it
    may resolve something else entirely, and it was NOT folded on the
    strength of a matching function name), and the shared
    `_hooklib.resolve_runtime_dir`, exercised once per sentinel
    (engram_client.py, covering the three merged-into-one-implementation
    hooks; engram_ids.py, covering utility-credit-mention-stop.py) rather
    than three redundant runs of identical code under different hook-name
    labels.
  - Where a subject is the shared resolver, the WORKTREE-NESTED file that
    gets copied and loaded is `_hooklib.py` itself, not any hook file --
    `_hooklib.py` lives in the exact same directory as the hooks that call
    it in both topologies (its own docstring establishes this), so copying
    IT to the fake nested location and loading it there exercises the
    genuine, unmodified walk-parents/permission-error code at the path
    depth being simulated, matching this file's own pre-existing principle
    (stated in TestResolveRuntimeDirWalkParents below) of never
    reimplementing the resolver under test.
  - The old `patched_fn` hand-reimplementation inside
    TestResolveRuntimeDirPluginProbe / TestResolveRuntimeDirShadowRegression
    (a second, parallel copy of the first 3 resolution rungs, used only to
    control the `__file__` math without physically copying a file) is
    removed as part of the retarget: since the subject is now ONE shared
    file (`_hooklib.py`) instead of eight-hooks-worth of copy-pasted
    functions, physically writing that one file to the fake location and
    loading it for real is no more expensive than maintaining a
    hand-written stand-in, and it means every test in this file now
    exercises the actual, unmodified resolver -- not a parallel
    reimplementation of it.

Two scenarios validated (unchanged from the original doc, now against the
shared resolver):

  1. Plugin layout — hook at <tmp>/<plugin>/hooks/hook.py (flat layout,
     no platform subdir — mirrors tools/build-plugin.sh line ~199 which
     copies hooks flat into <plugin_root>/hooks/),
     engram_client.py at <tmp>/<plugin>/, ENGRAM_HOME data-only,
     ENGRAM_RUNTIME_DIR unset, no ~/engram-alpha sentinel.
     Expected: resolve_runtime_dir returns <tmp>/<plugin>/.

  2. Scatter layout (regression) — engram_client.py already present under
     ENGRAM_HOME but no plugin bundle present. ENGRAM_RUNTIME_DIR unset.
     Expected: resolve_runtime_dir returns ENGRAM_HOME (priority 3, scatter
     fallback when no plugin bundle exists), preserving existing scatter-install
     behaviour.

No live ENGRAM install is touched; every test builds its own temp-dir tree.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch

import pytest

# #4093: each hook load runs the hook's module-level sys.path.insert; the shared window
# (#4029) puts sys.path and the src/engram modules back after each test.
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOKLIB_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "_hooklib.py"
_HOOKLIB_SRC = _HOOKLIB_PATH.read_text()
# engram-alpha#2863 WS2: engram-lesson-tripwire-hook.py now ALSO carries a
# module-scope `from _harness import detect_harness, get_harness_budgets`
# (folded onto the shared emit_context() renderer) -- a second real
# dependency the fake worktree tree below must satisfy, same reasoning as
# the pre-existing _hooklib.py sibling-write this mirrors (see
# TestResolveRuntimeDirWalkParents._build_worktree_tree's own docstring).
_HARNESS_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "_harness.py"
_HARNESS_SRC = _HARNESS_PATH.read_text()


def _last_ditch_checkout(suffix: str = "") -> str:
    """The last-ditch fallback: first existing of ~/engram, ~/engram-alpha (the default
    name first, the old name accepted); neither exists -> the ~/engram path."""
    for name in ("~/engram", "~/engram-alpha"):
        if os.path.isdir(os.path.expanduser(name + suffix)):
            return os.path.expanduser(name + suffix)
    return os.path.expanduser("~/engram" + suffix)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_resolve_runtime_dir_module(hooklib_file_path: Path) -> types.FunctionType:
    """Load `resolve_runtime_dir` from an ALREADY-PRESENT _hooklib.py file at
    hooklib_file_path. The caller is responsible for getting the file onto
    disk there (shutil.copy2, write_text, ...) -- this function only loads
    and extracts, so its own behavior doesn't depend on ENGRAM_HOME/
    ENGRAM_RUNTIME_DIR at import time (_hooklib.py, unlike some hooks, has
    no module-level env-dependent side effects to guard against)."""
    mod_name = f"_hooklib_instance_{id(hooklib_file_path)}"
    spec = importlib.util.spec_from_file_location(mod_name, hooklib_file_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    fn = getattr(mod, "resolve_runtime_dir", None)
    if fn is None:
        raise AttributeError(f"resolve_runtime_dir not found in {hooklib_file_path}")
    return fn


def _load_resolve_runtime_dir_real() -> types.FunctionType:
    """Load resolve_runtime_dir from its REAL repo location, unmodified --
    for tests that don't need to fake __file__ (scatter, explicit-override:
    neither rung depends on where the plugin root's dirname() math lands).
    Mirrors the original test's use of the real hook file's own location for
    the same purpose. Never writes anything -- read-only load of the real
    file."""
    return _load_resolve_runtime_dir_module(_HOOKLIB_PATH)


def _load_resolve_runtime_dir_at_fake_path(hooklib_file_path: Path) -> types.FunctionType:
    """Copy _hooklib.py's real source to hooklib_file_path (creating parent
    dirs as needed) and load resolve_runtime_dir from THAT location, so its
    internal os.path.abspath(__file__) plugin-root math resolves relative to
    the FAKE path being simulated, not this repo's real checkout. Never
    touches the real _hooklib.py on disk -- writes only to the caller-given
    (temp-dir) path."""
    hooklib_file_path.parent.mkdir(parents=True, exist_ok=True)
    hooklib_file_path.write_text(_HOOKLIB_SRC)
    return _load_resolve_runtime_dir_module(hooklib_file_path)


def _load_function_clean(hook_path: Path) -> types.FunctionType:
    """Import a hook module (with env guards) and return its OWN,
    self-contained `_resolve_runtime_dir` -- the untouched, no-argument
    contract (engram-lesson-tripwire-hook.py only, post-gh#2570; every other
    former user of this loader was migrated to
    `_load_resolve_runtime_dir_real` / `_load_resolve_runtime_dir_at_fake_path`
    above).

    The hook has module-level side-effects (importing context_tracker,
    reading env vars at import time). We extract only the function by
    compiling the source and extracting it from a throw-away namespace,
    avoiding the module-level code's need for a live ENGRAM install.
    """
    # Stub context_tracker so the session-start hook's top-level import
    # would work too, in case a future subject needs it. Harmless no-op for
    # engram-lesson-tripwire-hook.py, which doesn't import context_tracker.
    stub_ct = types.ModuleType("context_tracker")
    stub_ct.write_baseline = lambda *a, **kw: None  # type: ignore[attr-defined]
    old_ct = sys.modules.get("context_tracker")
    sys.modules["context_tracker"] = stub_ct  # type: ignore[assignment]

    # Point ENGRAM_HOME at a temp dir so module-level resolution doesn't
    # accidentally succeed during import.
    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get("ENGRAM_HOME")
        os.environ["ENGRAM_HOME"] = tmp
        # Ensure ENGRAM_RUNTIME_DIR is unset for a clean import.
        old_rtd = os.environ.pop("ENGRAM_RUNTIME_DIR", None)

        mod_name = f"_hook_{hook_path.stem}_{id(hook_path)}"
        spec = importlib.util.spec_from_file_location(mod_name, hook_path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        except Exception:
            # Some hooks raise on missing resources; we only need the function.
            pass
        fn = getattr(mod, "_resolve_runtime_dir", None)

        # Restore env.
        if old_home is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old_home
        if old_rtd is not None:
            os.environ["ENGRAM_RUNTIME_DIR"] = old_rtd

    # Restore context_tracker stub.
    if old_ct is None:
        sys.modules.pop("context_tracker", None)
    else:
        sys.modules["context_tracker"] = old_ct

    if fn is None:
        raise AttributeError(
            f"_resolve_runtime_dir not found in {hook_path}"
        )
    return fn


# ---------------------------------------------------------------------------
# The four former subjects, now identified by (label, sentinel) rather than
# by hook file -- surface-hook / session-start-hook / surface-daemon share
# ONE implementation post-gh#2570, so what varies between them is only the
# sentinel a given caller happens to pass, not the code exercised.
# ---------------------------------------------------------------------------

_SUBJECTS = [
    ("engram-surface-hook.py", "engram_client.py"),
    ("engram-session-start-hook.py", "engram_client.py"),
    ("engram-surface-daemon.py", "engram_client.py"),
    ("engram-utility-credit-mention-stop.py", "engram_ids.py"),
]


class TestResolveRuntimeDirPluginProbe(unittest.TestCase):
    """Plugin-root probe fires correctly for all four former subjects."""

    def _run_plugin_layout_test(self, label: str, sentinel: str) -> None:
        """Assert plugin-root probe resolves to plugin root.

        Layout mirrors the REAL flat plugin install (tools/build-plugin.sh
        line ~199: hooks copied flat into <plugin_root>/hooks/, no platform
        subdir), with `_hooklib.py` itself as the file whose __file__ math
        is exercised (it lives alongside the hooks in this exact layout):
          <tmp>/<plugin>/hooks/_hooklib.py — the resolver lives here (FLAT)
          <tmp>/<plugin>/<sentinel>        — runtime module here
          ENGRAM_HOME = <tmp>/data-only-home/  — no <sentinel>
          ENGRAM_RUNTIME_DIR = unset
          ~/engram-alpha = non-existent (patched)

        `sentinel` is the module filename the ORIGINAL subject checks for
        (engram_client.py for surface-hook/session-start/surface-daemon;
        engram_ids.py for utility-credit-mention-stop).

        With this flat layout, dirname(dirname(__file__)) correctly resolves
        to <plugin_root> (dirname of hooks/ dir). Three dirname calls would
        resolve to the PARENT of plugin_root — the bug this test catches.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Build the FLAT plugin tree (mirrors build-plugin.sh).
            # _hooklib.py lands at <plugin_root>/hooks/_hooklib.py — no
            # platform subdir, matching the deployed layout.
            plugin_root = tmp_path / "plugin"
            hook_dir = plugin_root / "hooks"
            hook_dir.mkdir(parents=True)
            # Place the sentinel module at the plugin root.
            (plugin_root / sentinel).write_text("# stub\n")

            # Build a data-only ENGRAM_HOME (no sentinel).
            data_home = tmp_path / "data-only-home"
            data_home.mkdir()

            hooklib_file = hook_dir / "_hooklib.py"
            fn = _load_resolve_runtime_dir_at_fake_path(hooklib_file)

            with patch.dict(
                os.environ,
                {"ENGRAM_HOME": str(data_home)},
                clear=False,
            ):
                os.environ.pop("ENGRAM_RUNTIME_DIR", None)
                # Patch expanduser so ~/engram-alpha resolves to a non-existent path.
                with patch(
                    "os.path.expanduser",
                    side_effect=lambda p: str(tmp_path / "no-such-engram-alpha")
                    if "engram-alpha" in p
                    else os.path.expanduser.__wrapped__(p)  # type: ignore[attr-defined]
                    if hasattr(os.path.expanduser, "__wrapped__")
                    else p,
                ):
                    result = fn(str(data_home), sentinel)

            self.assertEqual(
                result,
                str(plugin_root),
                f"[{label}] Expected plugin root {plugin_root!r}, "
                f"got {result!r}",
            )

    def _run_scatter_layout_test(self, label: str, sentinel: str) -> None:
        """Assert ENGRAM_HOME probe (priority 3) still wins when no plugin bundle present.

        Layout:
          ENGRAM_HOME = <tmp>/scatter-home/ with <sentinel> present
          plugin root has no <sentinel> (no bundle) -- loaded from
          _hooklib.py's REAL repo location, whose own dirname(dirname())
          lands one level short of a real plugin root by construction of
          the source tree (src/engram/hooks/claude/_hooklib.py -> two
          dirnames -> src/engram/hooks, which never contains engram_client.py
          / engram_ids.py) -- so rung 2 misses naturally here, same as the
          original per-hook test relied on.
          ENGRAM_RUNTIME_DIR = unset
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scatter_home = tmp_path / "scatter-home"
            scatter_home.mkdir()
            (scatter_home / sentinel).write_text("# stub\n")

            fn = _load_resolve_runtime_dir_real()

            with patch.dict(
                os.environ,
                {"ENGRAM_HOME": str(scatter_home)},
                clear=False,
            ):
                os.environ.pop("ENGRAM_RUNTIME_DIR", None)
                result = fn(str(scatter_home), sentinel)

            self.assertEqual(
                result,
                str(scatter_home),
                f"[{label}] Scatter layout: expected ENGRAM_HOME "
                f"{scatter_home!r} to win, got {result!r}",
            )

    # -- Plugin layout tests (one per former subject) --

    def test_plugin_layout_claude_surface_hook(self):
        self._run_plugin_layout_test("engram-surface-hook.py", "engram_client.py")

    def test_plugin_layout_claude_session_start_hook(self):
        self._run_plugin_layout_test("engram-session-start-hook.py", "engram_client.py")

    def test_plugin_layout_claude_surface_daemon(self):
        self._run_plugin_layout_test("engram-surface-daemon.py", "engram_client.py")

    def test_plugin_layout_claude_utility_credit_mention_stop(self):
        self._run_plugin_layout_test(
            "engram-utility-credit-mention-stop.py", "engram_ids.py"
        )

    # -- Scatter no-regression tests (one per former subject) --

    def test_scatter_no_regression_claude_surface_hook(self):
        self._run_scatter_layout_test("engram-surface-hook.py", "engram_client.py")

    def test_scatter_no_regression_claude_session_start_hook(self):
        self._run_scatter_layout_test("engram-session-start-hook.py", "engram_client.py")

    def test_scatter_no_regression_claude_surface_daemon(self):
        self._run_scatter_layout_test("engram-surface-daemon.py", "engram_client.py")

    def test_scatter_no_regression_claude_utility_credit_mention_stop(self):
        self._run_scatter_layout_test(
            "engram-utility-credit-mention-stop.py", "engram_ids.py"
        )


class TestResolveRuntimeDirShadowRegression(unittest.TestCase):
    """Regression tests for #1152: plugin bundle must win over stale data-dir snapshot.

    Three scenarios, each run against all four former subjects:

      (a) Shadow case (#1152): plugin bundle present AND stale engram_home snapshot
          present -> resolver must return plugin_root, NOT engram_home.
      (b) Scatter-only case: no plugin bundle, only engram_home snapshot ->
          resolver returns engram_home.
      (c) Explicit override: ENGRAM_RUNTIME_DIR set -> wins over both.

    Each scenario physically copies _hooklib.py's real source to the fake
    plugin-root location and loads it from there (see
    _load_resolve_runtime_dir_at_fake_path), so the __file__ math is
    exercised on the actual resolver, not a hand-written stand-in.

    `sentinel` is the module filename the former subject checks for:
    engram_client.py for surface-hook/session-start-hook/surface-daemon;
    engram_ids.py for utility-credit-mention-stop.
    """

    def _run_shadow_test(self, label: str, sentinel: str) -> None:
        """(a) Plugin bundle AND stale engram_home snapshot both present.

        Expected: plugin_root wins (bundle canonical; stale snapshot must NOT shadow).
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Build flat plugin tree with sentinel at plugin root.
            plugin_root = tmp_path / "plugin"
            hook_dir = plugin_root / "hooks"
            hook_dir.mkdir(parents=True)
            (plugin_root / sentinel).write_text("# bundle stub\n")

            # Also put a stale sentinel in engram_home (the #1152 scenario).
            engram_home = tmp_path / "dot-engram"
            engram_home.mkdir()
            (engram_home / sentinel).write_text("# stale scatter snapshot\n")

            hooklib_file = hook_dir / "_hooklib.py"
            fn = _load_resolve_runtime_dir_at_fake_path(hooklib_file)

            with patch.dict(os.environ, {"ENGRAM_HOME": str(engram_home)}, clear=False):
                os.environ.pop("ENGRAM_RUNTIME_DIR", None)
                result = fn(str(engram_home), sentinel)

            self.assertEqual(
                result,
                str(plugin_root),
                f"[{label}] #1152 shadow: expected plugin_root "
                f"{plugin_root!r} to win over stale engram_home, got {result!r}",
            )

    def _run_scatter_only_test(self, label: str, sentinel: str) -> None:
        """(b) No plugin bundle; only engram_home snapshot present.

        Expected: engram_home wins (scatter-install fallback path).
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Plugin tree exists but has NO sentinel.
            plugin_root = tmp_path / "plugin"
            hook_dir = plugin_root / "hooks"
            hook_dir.mkdir(parents=True)
            # (no sentinel at plugin_root)

            engram_home = tmp_path / "scatter-home"
            engram_home.mkdir()
            (engram_home / sentinel).write_text("# scatter copy\n")

            hooklib_file = hook_dir / "_hooklib.py"
            fn = _load_resolve_runtime_dir_at_fake_path(hooklib_file)

            with patch.dict(os.environ, {"ENGRAM_HOME": str(engram_home)}, clear=False):
                os.environ.pop("ENGRAM_RUNTIME_DIR", None)
                result = fn(str(engram_home), sentinel)

            self.assertEqual(
                result,
                str(engram_home),
                f"[{label}] Scatter-only: expected engram_home "
                f"{engram_home!r}, got {result!r}",
            )

    def _run_explicit_override_test(self, label: str, sentinel: str) -> None:
        """(c) ENGRAM_RUNTIME_DIR set -> wins over both plugin bundle and engram_home.

        Expected: the explicit env-var value is returned unchanged.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Both plugin and engram_home are present (same as shadow case).
            plugin_root = tmp_path / "plugin"
            hook_dir = plugin_root / "hooks"
            hook_dir.mkdir(parents=True)
            (plugin_root / sentinel).write_text("# bundle stub\n")

            engram_home = tmp_path / "dot-engram"
            engram_home.mkdir()
            (engram_home / sentinel).write_text("# stale scatter snapshot\n")

            explicit_dir = str(tmp_path / "explicit-runtime")

            hooklib_file = hook_dir / "_hooklib.py"
            fn = _load_resolve_runtime_dir_at_fake_path(hooklib_file)

            with patch.dict(
                os.environ,
                {
                    "ENGRAM_HOME": str(engram_home),
                    "ENGRAM_RUNTIME_DIR": explicit_dir,
                },
                clear=False,
            ):
                result = fn(str(engram_home), sentinel)

            self.assertEqual(
                result,
                explicit_dir,
                f"[{label}] Explicit override: expected {explicit_dir!r}, "
                f"got {result!r}",
            )

    # -- Shadow tests (a): plugin wins over stale engram_home --

    def test_shadow_regression_surface_hook(self):
        self._run_shadow_test("engram-surface-hook.py", "engram_client.py")

    def test_shadow_regression_session_start_hook(self):
        self._run_shadow_test("engram-session-start-hook.py", "engram_client.py")

    def test_shadow_regression_surface_daemon(self):
        self._run_shadow_test("engram-surface-daemon.py", "engram_client.py")

    def test_shadow_regression_utility_credit_mention_stop(self):
        self._run_shadow_test("engram-utility-credit-mention-stop.py", "engram_ids.py")

    # -- Scatter-only tests (b): engram_home wins when no plugin bundle --

    def test_scatter_only_surface_hook(self):
        self._run_scatter_only_test("engram-surface-hook.py", "engram_client.py")

    def test_scatter_only_session_start_hook(self):
        self._run_scatter_only_test("engram-session-start-hook.py", "engram_client.py")

    def test_scatter_only_surface_daemon(self):
        self._run_scatter_only_test("engram-surface-daemon.py", "engram_client.py")

    def test_scatter_only_utility_credit_mention_stop(self):
        self._run_scatter_only_test("engram-utility-credit-mention-stop.py", "engram_ids.py")

    # -- Explicit override tests (c): ENGRAM_RUNTIME_DIR wins over both --

    def test_explicit_override_surface_hook(self):
        self._run_explicit_override_test("engram-surface-hook.py", "engram_client.py")

    def test_explicit_override_session_start_hook(self):
        self._run_explicit_override_test("engram-session-start-hook.py", "engram_client.py")

    def test_explicit_override_surface_daemon(self):
        self._run_explicit_override_test("engram-surface-daemon.py", "engram_client.py")

    def test_explicit_override_utility_credit_mention_stop(self):
        self._run_explicit_override_test(
            "engram-utility-credit-mention-stop.py", "engram_ids.py"
        )


class _WalkParentsSubject(NamedTuple):
    """One subject for the #1712 / #1727 regression coverage below.

    `source_kind` is "hook" for the untouched, self-contained,
    no-argument engram-lesson-tripwire-hook.py (loaded and called via its
    own `_resolve_runtime_dir`), or "hooklib" for the shared
    `_hooklib.resolve_runtime_dir(engram_home, sentinel)` that now serves
    every migrated hook.
    """

    label: str
    source_kind: str  # "hook" | "hooklib"
    real_source_path: Path
    sentinel: str
    takes_engram_home_arg: bool
    expected_last_ditch: str  # the ORIGINAL hardcoded fallback string, unchanged by #1712


class TestResolveRuntimeDirWalkParents(unittest.TestCase):
    """Regression tests for #1712: the absolute-path last-ditch fallback is
    replaced by a walk-parents search. Two SUBJECTS (retargeted for gh#2570,
    see module docstring):

      1. engram-lesson-tripwire-hook.py -- UNTOUCHED by gh#2570, deliberately.
         Its own `_resolve_runtime_dir` takes NO ARGUMENTS -- a different
         contract from the shared resolver, so it may resolve something
         else entirely and was not folded on the strength of a matching
         function name. One copy remains here, deliberately, pending a
         look at whether it is the same thing.
      2. `_hooklib.resolve_runtime_dir` -- the shared resolver that now
         serves engram-surface-hook.py, engram-session-start-hook.py, and
         engram-surface-daemon.py (sentinel=engram_client.py, tested once
         since the three now execute byte-identical code) and
         engram-utility-credit-mention-stop.py (sentinel=engram_ids.py,
         tested separately since it's a different sentinel value, even
         though the code path is the same).

    Recreates the worktree-nesting hazard directly:
    `<tmp>/main-repo/.claude/worktrees/fake-agent/` nested under
    `<tmp>/main-repo/`, matching this repo's own dispatch pattern and real
    relative hook depth (src/engram/hooks/claude/<file>.py).

    The real subject file is COPIED to the worktree-nested location (not
    just referenced) so that `Path(__file__).resolve()` inside the loaded
    module genuinely resolves under the fake worktree -- exercising the
    actual, unmodified walk-parents code added by this fix, not a
    reimplementation. For the "hooklib" subjects this means copying
    `_hooklib.py` itself (not any hook file) to the nested location:
    `_hooklib.py` lives in the exact same directory as the hooks that call
    it in both topologies, so this is the genuine deployed-adjacent shape,
    not a stand-in for it.
    """

    _SUBJECTS = [
        _WalkParentsSubject(
            label="engram-lesson-tripwire-hook.py (untouched by gh#2570 -- "
            "own no-argument _resolve_runtime_dir, a different contract, "
            "not folded on the strength of a matching name)",
            source_kind="hook",
            real_source_path=(
                _REPO_ROOT / "src" / "engram" / "hooks" / "claude"
                / "engram-lesson-tripwire-hook.py"
            ),
            sentinel="engram_idf.py",
            takes_engram_home_arg=False,
            expected_last_ditch=_last_ditch_checkout("/src/engram"),
        ),
        _WalkParentsSubject(
            label="_hooklib.resolve_runtime_dir, sentinel=engram_client.py "
            "(covers the migrated engram-surface-hook.py / "
            "engram-session-start-hook.py / engram-surface-daemon.py, which "
            "now share this one implementation -- tested once, not three "
            "times, since the code path is identical)",
            source_kind="hooklib",
            real_source_path=_HOOKLIB_PATH,
            sentinel="engram_client.py",
            takes_engram_home_arg=True,
            expected_last_ditch=_last_ditch_checkout(),
        ),
        _WalkParentsSubject(
            label="_hooklib.resolve_runtime_dir, sentinel=engram_ids.py "
            "(covers the migrated engram-utility-credit-mention-stop.py)",
            source_kind="hooklib",
            real_source_path=_HOOKLIB_PATH,
            sentinel="engram_ids.py",
            takes_engram_home_arg=True,
            expected_last_ditch=_last_ditch_checkout(),
        ),
    ]

    @contextlib.contextmanager
    def _tmpdir(self):
        """The tmp root every test below builds its fake tree under. A hook
        for the symlinked-tmp variant (TestResolveRuntimeDirWalkParentsSymlinkedTmp)."""
        with tempfile.TemporaryDirectory() as tmp:
            yield tmp

    @staticmethod
    def _build_worktree_tree(tmp_path: Path, subject: _WalkParentsSubject):
        """<tmp>/main-repo/.claude/worktrees/fake-agent/src/engram/hooks/claude/<file>.py
        -- a copy of the real subject file (the hook itself for the untouched
        no-arg case, or _hooklib.py for the shared-resolver case), nested at
        this repo's real relative depth under a fake worktree, itself nested
        under a fake main-repo.

        ALSO writes a sibling _hooklib.py into the same nested directory,
        unconditionally (a genuine bug found while retargeting this file for
        gh#2570, pre-dating this change and not introduced by it): the
        lesson-tripwire subject's own `_resolve_runtime_dir` sits behind a
        module-scope `from _hooklib import atomic_write_json` -- a real
        dependency at the top of that file, unrelated to gh#2570 -- which
        this fake worktree tree never satisfied. In a FULL test-suite run
        this went unnoticed because some earlier test in the same process
        had already populated sys.modules["_hooklib"] with the REAL module,
        which Python's import system then reused for the fake worktree copy
        too, silently -- an order-dependent pass. Verified in isolation
        (`pytest tests/test_resolve_runtime_dir_plugin.py::TestResolveRuntimeDirWalkParents::test_worktree_own_copy_wins_over_ancestor`
        alone, fresh process, no prior _hooklib import) that the ORIGINAL,
        pre-retarget test file failed this exact way for the
        engram-lesson-tripwire-hook.py subject too -- this is not new
        breakage from the retarget, it is a pre-existing hermeticity gap the
        retarget's own isolation testing surfaced. Writing this sibling
        unconditionally (redundant-but-harmless for "hooklib" subjects,
        whose own file IS _hooklib.py) makes every subject's worktree tree
        self-contained regardless of what else ran earlier in the process.

        Also writes a sibling _harness.py, same reasoning (engram-alpha#2863
        WS2): folding engram-lesson-tripwire-hook.py onto the shared
        emit_context() renderer added a second module-scope dependency,
        `from _harness import detect_harness, get_harness_budgets`, which
        this fake worktree tree must satisfy for the same
        order-dependent-pass reason the _hooklib.py sibling exists for.
        """
        main_repo = tmp_path / "main-repo"
        worktree = main_repo / ".claude" / "worktrees" / "fake-agent"
        worktree_hook_dir = worktree / "src" / "engram" / "hooks" / "claude"
        worktree_hook_dir.mkdir(parents=True)
        worktree_file = worktree_hook_dir / subject.real_source_path.name
        shutil.copy2(subject.real_source_path, worktree_file)
        hooklib_sibling = worktree_hook_dir / "_hooklib.py"
        if hooklib_sibling != worktree_file:
            hooklib_sibling.write_text(_HOOKLIB_SRC)
        harness_sibling = worktree_hook_dir / "_harness.py"
        if harness_sibling != worktree_file:
            harness_sibling.write_text(_HARNESS_SRC)
        return main_repo, worktree, worktree_file

    @staticmethod
    def _load_subject_fn(worktree_file: Path, subject: _WalkParentsSubject) -> types.FunctionType:
        """Load the callable to exercise for `subject`, from the ALREADY
        worktree-nested copy at worktree_file (see _build_worktree_tree)."""
        if subject.source_kind == "hook":
            return _load_function_clean(worktree_file)
        fn = _load_resolve_runtime_dir_module(worktree_file)
        return functools.partial(fn, sentinel=subject.sentinel)

    @staticmethod
    def _call_resolver(loaded_fn, takes_arg: bool, engram_home: str):
        return loaded_fn(engram_home) if takes_arg else loaded_fn()

    def _resolve_with_clean_env(self, worktree_file: Path, subject: _WalkParentsSubject,
                                 data_home: Path) -> str:
        fn = self._load_subject_fn(worktree_file, subject)
        with patch.dict(os.environ, {"ENGRAM_HOME": str(data_home)}, clear=False):
            os.environ.pop("ENGRAM_RUNTIME_DIR", None)
            return self._call_resolver(fn, subject.takes_engram_home_arg, str(data_home))

    def test_worktree_own_copy_wins_over_ancestor(self):
        """Worktree has its OWN src/engram/<marker> -- the walk-parents
        search must return the WORKTREE's own copy (nearer ancestor), not
        the main-repo's (spec Tests item 2). Main-repo ALSO gets a copy so
        the assertion actually distinguishes "own copy wins" from "walked
        past it and found the ancestor's instead"."""
        for subject in self._SUBJECTS:
            with self.subTest(subject=subject.label):
                with self._tmpdir() as tmp:
                    tmp_path = Path(tmp)
                    main_repo, worktree, worktree_file = self._build_worktree_tree(
                        tmp_path, subject
                    )
                    main_repo_engram_dir = main_repo / "src" / "engram"
                    main_repo_engram_dir.mkdir(parents=True)
                    (main_repo_engram_dir / subject.sentinel).write_text("# main-repo stub\n")

                    own_engram_dir = worktree / "src" / "engram"
                    (own_engram_dir / subject.sentinel).write_text("# worktree's own stub\n")

                    data_home = tmp_path / "data-only-home"
                    data_home.mkdir()

                    result = self._resolve_with_clean_env(
                        worktree_file, subject, data_home
                    )

                    # Compare REAL paths: the resolver returns the
                    # `Path(__file__).resolve()` spelling, so a symlinked tmp
                    # root (macOS /var -> /private/var, gh#3292) names the same
                    # directory differently. Full-path realpath equality still
                    # proves WHICH directory was chosen.
                    self.assertEqual(
                        os.path.realpath(result),
                        os.path.realpath(own_engram_dir),
                        f"[{subject.label}] expected worktree's own "
                        f"{own_engram_dir!r}, got {result!r}",
                    )

    def test_worktree_walks_up_to_ancestor_copy(self):
        """Worktree has NO <marker> anywhere in its own tree; only the
        main-repo (an ancestor beyond the worktree) does -- the walk-parents
        search must find the ancestor's copy (spec Tests item 3, distinct
        from item 2's "worktree has its own copy" case)."""
        for subject in self._SUBJECTS:
            with self.subTest(subject=subject.label):
                with self._tmpdir() as tmp:
                    tmp_path = Path(tmp)
                    main_repo, worktree, worktree_file = self._build_worktree_tree(
                        tmp_path, subject
                    )
                    # Worktree's own src/engram/ deliberately has NO marker.
                    main_repo_engram_dir = main_repo / "src" / "engram"
                    main_repo_engram_dir.mkdir(parents=True)
                    (main_repo_engram_dir / subject.sentinel).write_text("# main-repo stub\n")

                    data_home = tmp_path / "data-only-home"
                    data_home.mkdir()

                    result = self._resolve_with_clean_env(
                        worktree_file, subject, data_home
                    )

                    # Compare REAL paths (see the sibling test): same
                    # directory, different spelling under a symlinked tmp root.
                    self.assertEqual(
                        os.path.realpath(result),
                        os.path.realpath(main_repo_engram_dir),
                        f"[{subject.label}] expected ancestor's "
                        f"{main_repo_engram_dir!r}, got {result!r}",
                    )

    def test_last_ditch_fallback_preserved_when_nothing_found(self):
        """Nothing found anywhere in the walked parents -- must still return
        the ORIGINAL hardcoded fallback string, proving the old fallback is
        preserved (additive hardening), not removed (spec Tests item 4)."""
        for subject in self._SUBJECTS:
            with self.subTest(subject=subject.label):
                with self._tmpdir() as tmp:
                    tmp_path = Path(tmp)
                    main_repo, worktree, worktree_file = self._build_worktree_tree(
                        tmp_path, subject
                    )
                    # No marker anywhere in the tree -- every walk-parents
                    # candidate misses.
                    data_home = tmp_path / "data-only-home"
                    data_home.mkdir()

                    result = self._resolve_with_clean_env(
                        worktree_file, subject, data_home
                    )

                    self.assertEqual(
                        result,
                        subject.expected_last_ditch,
                        f"[{subject.label}] expected last-ditch "
                        f"{subject.expected_last_ditch!r}, got {result!r}",
                    )

    def test_permission_error_mid_walk_degrades_to_last_ditch(self):
        """Reviewer-fairy blocker (PR #1727): an unreadable ancestor
        directory mid-walk must degrade to the last-ditch fallback, not
        crash. pathlib.Path.exists() -- unlike os.path.exists() used at the
        rungs above -- raises PermissionError instead of returning False on
        a permission-denied path component. Since the resolver is called at
        bare MODULE-IMPORT time (unguarded) by most of its callers, an
        unhandled exception here would crash the whole hook process."""
        for subject in self._SUBJECTS:
            with self.subTest(subject=subject.label):
                with self._tmpdir() as tmp:
                    tmp_path = Path(tmp)
                    main_repo, worktree, worktree_file = self._build_worktree_tree(
                        tmp_path, subject
                    )
                    # No marker anywhere findable -- same empty tree as the
                    # last-ditch test, but additionally lock down the
                    # worktree's own src/engram/ dir so a mid-walk .exists()
                    # check on a path under it raises PermissionError rather
                    # than returning False.
                    own_engram_dir = worktree / "src" / "engram"
                    data_home = tmp_path / "data-only-home"
                    data_home.mkdir()

                    # Load the function BEFORE locking down the directory --
                    # loading the subject module needs to traverse
                    # worktree/src/engram/hooks/claude/<file>, which lives
                    # UNDER the same directory we're about to chmod. Only the
                    # subsequent CALL (which does the walk-parents .exists()
                    # checks) should hit the permission wall.
                    fn = self._load_subject_fn(worktree_file, subject)
                    os.chmod(own_engram_dir, 0o000)
                    try:
                        with patch.dict(os.environ, {"ENGRAM_HOME": str(data_home)}, clear=False):
                            os.environ.pop("ENGRAM_RUNTIME_DIR", None)
                            result = self._call_resolver(
                                fn, subject.takes_engram_home_arg, str(data_home)
                            )
                    finally:
                        # Restore permissions so TemporaryDirectory cleanup
                        # (which must delete this dir) doesn't itself fail.
                        os.chmod(own_engram_dir, 0o755)

                    self.assertEqual(
                        result,
                        subject.expected_last_ditch,
                        f"[{subject.label}] expected degrade-to-last-ditch "
                        f"{subject.expected_last_ditch!r} on a permission error "
                        f"mid-walk, got {result!r}",
                    )


class TestResolveRuntimeDirWalkParentsSymlinkedTmp(TestResolveRuntimeDirWalkParents):
    """engram-alpha#3292: the same WalkParents tests with the tmp root reached
    through a SYMLINK, reproducing macOS's TMPDIR shape (/var -> /private/var)
    on Linux. The resolver returns `Path(__file__).resolve()`-derived paths
    (the real directory), so an expectation built from the unresolved tmp path
    names the same directory spelled differently and must still pass.
    """

    @contextlib.contextmanager
    def _tmpdir(self):
        with tempfile.TemporaryDirectory() as base:
            real_parent = Path(base) / "private" / "var"
            real_dir = real_parent / "tmpX"
            real_dir.mkdir(parents=True)
            link = Path(base) / "var"
            link.symlink_to(real_parent, target_is_directory=True)
            reached = link / "tmpX"
            # Guard against a vacuous variant: the spelling really differs.
            self.assertNotEqual(str(reached), os.path.realpath(reached))
            yield str(reached)


if __name__ == "__main__":
    unittest.main()
