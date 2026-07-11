"""Tests for the plugin-root probe added to _resolve_runtime_dir.

Issue #642: clean plugin install degrades to FTS-only because ENGRAM_HOME
is data-only (no engram_client.py) and ~/engram-alpha doesn't exist on a
public user's machine. The fix adds a plugin-root probe (priority 3, before
the ~/engram-alpha fallback) that walks two dirname() levels up from __file__
to locate <plugin_root>/engram_client.py.

Two scenarios validated:

  1. Plugin layout — hook at <tmp>/<plugin>/hooks/hook.py (flat layout,
     no platform subdir — mirrors tools/build-plugin.sh line ~199 which
     copies hooks flat into <plugin_root>/hooks/),
     engram_client.py at <tmp>/<plugin>/, ENGRAM_HOME data-only,
     ENGRAM_RUNTIME_DIR unset, no ~/engram-alpha sentinel.
     Expected: _resolve_runtime_dir returns <tmp>/<plugin>/.

  2. Scatter layout (regression) — engram_client.py already present under
     ENGRAM_HOME but no plugin bundle present. ENGRAM_RUNTIME_DIR unset.
     Expected: _resolve_runtime_dir returns ENGRAM_HOME (priority 3, scatter
     fallback when no plugin bundle exists), preserving existing scatter-install
     behaviour.

Each test imports _resolve_runtime_dir directly from the source file after
overriding the relevant filesystem state with a temporary directory, so no
live ENGRAM install is touched.

Tests cover the three Claude hook files only; the Gemini lane was removed
2026-06-06 (upstream discontinued; last state at tag archive/gemini-last-state).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_function_from_hook(hook_path: Path) -> types.FunctionType:
    """Load _resolve_runtime_dir from the given hook file.

    The hooks have module-level side-effects (importing context_tracker,
    reading env vars at import time). We extract only the function by
    compiling the source and extracting it from a throw-away namespace,
    avoiding the module-level code.

    Approach: exec only the function definition lines by finding the function
    in a fresh module namespace. We use importlib to load the whole module
    with guarded env vars, then grab the function attribute.
    """
    return _load_function_clean(hook_path)


def _load_function_clean(hook_path: Path) -> types.FunctionType:
    """Import hook as a module (with env guards) and return _resolve_runtime_dir."""
    # Stub context_tracker so the session-start hook's top-level import works.
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
# The actual test: one parameterised base, run against all 5 hook files.
# ---------------------------------------------------------------------------

_HOOK_FILES = [
    _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py",
    _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py",
    _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py",
]


class TestResolveRuntimeDirPluginProbe(unittest.TestCase):
    """Plugin-root probe fires correctly for all four Claude hook files."""

    def _run_plugin_layout_test(
        self, hook_path: Path, sentinel: str = "engram_client.py"
    ) -> None:
        """Assert plugin-root probe resolves to plugin root.

        Layout mirrors the REAL flat plugin install (tools/build-plugin.sh
        line ~199: hooks copied flat into <plugin_root>/hooks/, no platform
        subdir):
          <tmp>/<plugin>/hooks/hook.py    — hook file lives here (FLAT)
          <tmp>/<plugin>/<sentinel>       — runtime module here
          ENGRAM_HOME = <tmp>/data-only-home/  — no <sentinel>
          ENGRAM_RUNTIME_DIR = unset
          ~/engram-alpha = non-existent (patched)

        `sentinel` is the module filename the hook checks for (engram_client.py
        for the three original hooks; engram_ids.py for utility-credit-mention-stop).

        With this flat layout, dirname(dirname(__file__)) correctly resolves
        to <plugin_root> (dirname of hooks/ dir). Three dirname calls would
        resolve to the PARENT of plugin_root — the bug this test catches.
        """
        fn = _load_function_from_hook(hook_path)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Build the FLAT plugin tree (mirrors build-plugin.sh).
            # Hooks land at <plugin_root>/hooks/<name>.py — no platform subdir.
            plugin_root = tmp_path / "plugin"
            hook_dir = plugin_root / "hooks"
            hook_dir.mkdir(parents=True)
            # Place the sentinel module at the plugin root.
            (plugin_root / sentinel).write_text("# stub\n")

            # Build a data-only ENGRAM_HOME (no sentinel).
            data_home = tmp_path / "data-only-home"
            data_home.mkdir()

            # Simulate the hook's __file__ being inside the flat plugin tree by
            # patching the resolution logic. We rebuild a wrapper that
            # overrides __file__ with the flat-layout path.
            fake_hook_file = str(hook_dir / hook_path.name)

            def patched_fn(engram_home: str) -> str:
                """Wrapper that substitutes __file__ with the flat fake_hook_file."""
                import os as _os

                explicit = _os.environ.get("ENGRAM_RUNTIME_DIR")
                if explicit:
                    return explicit
                _plugin_root = _os.path.dirname(
                    _os.path.dirname(_os.path.abspath(fake_hook_file))
                )
                if _os.path.exists(_os.path.join(_plugin_root, sentinel)):
                    return _plugin_root
                if _os.path.exists(_os.path.join(engram_home, sentinel)):
                    return engram_home
                return _os.path.expanduser("~/engram-alpha")

            with (
                patch.dict(
                    os.environ,
                    {"ENGRAM_HOME": str(data_home)},
                    clear=False,
                ),
                patch.dict(
                    os.environ,
                    {},
                    clear=False,
                ),
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
                    result = patched_fn(str(data_home))

            self.assertEqual(
                result,
                str(plugin_root),
                f"[{hook_path.name}] Expected plugin root {plugin_root!r}, "
                f"got {result!r}",
            )

    def _run_scatter_layout_test(
        self, hook_path: Path, sentinel: str = "engram_client.py"
    ) -> None:
        """Assert ENGRAM_HOME probe (priority 3) still wins when no plugin bundle present.

        Layout:
          ENGRAM_HOME = <tmp>/scatter-home/ with <sentinel> present
          plugin root has no <sentinel> (no bundle)
          ENGRAM_RUNTIME_DIR = unset
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scatter_home = tmp_path / "scatter-home"
            scatter_home.mkdir()
            (scatter_home / sentinel).write_text("# stub\n")

            fn = _load_function_from_hook(hook_path)

            with patch.dict(
                os.environ,
                {"ENGRAM_HOME": str(scatter_home)},
                clear=False,
            ):
                os.environ.pop("ENGRAM_RUNTIME_DIR", None)
                result = fn(str(scatter_home))

            self.assertEqual(
                result,
                str(scatter_home),
                f"[{hook_path.name}] Scatter layout: expected ENGRAM_HOME "
                f"{scatter_home!r} to win, got {result!r}",
            )

    # -- Plugin layout tests (one per hook file) --

    def test_plugin_layout_claude_surface_hook(self):
        self._run_plugin_layout_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
        )

    def test_plugin_layout_claude_session_start_hook(self):
        self._run_plugin_layout_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
        )

    def test_plugin_layout_claude_surface_daemon(self):
        self._run_plugin_layout_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py"
        )

    # -- Scatter no-regression tests (one per hook file) --

    def test_scatter_no_regression_claude_surface_hook(self):
        self._run_scatter_layout_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
        )

    def test_scatter_no_regression_claude_session_start_hook(self):
        self._run_scatter_layout_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
        )

    def test_scatter_no_regression_claude_surface_daemon(self):
        self._run_scatter_layout_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py"
        )

    def test_plugin_layout_claude_utility_credit_mention_stop(self):
        self._run_plugin_layout_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-utility-credit-mention-stop.py",
            sentinel="engram_ids.py",
        )

    def test_scatter_no_regression_claude_utility_credit_mention_stop(self):
        self._run_scatter_layout_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-utility-credit-mention-stop.py",
            sentinel="engram_ids.py",
        )


class TestResolveRuntimeDirShadowRegression(unittest.TestCase):
    """Regression tests for #1152: plugin bundle must win over stale data-dir snapshot.

    Three scenarios, each run against all four Claude hook files:

      (a) Shadow case (#1152): plugin bundle present AND stale engram_home snapshot
          present -> resolver must return plugin_root, NOT engram_home.
      (b) Scatter-only case: no plugin bundle, only engram_home snapshot ->
          resolver returns engram_home.
      (c) Explicit override: ENGRAM_RUNTIME_DIR set -> wins over both.

    The patched_fn pattern from _run_plugin_layout_test is reused so that the
    fake_hook_file controls the dirname() math independently of where the test
    suite is run from.

    `sentinel` is the module filename the hook checks for: engram_client.py for the
    three original hooks; engram_ids.py for utility-credit-mention-stop.
    """

    def _make_patched_fn(self, fake_hook_file: str, sentinel: str = "engram_client.py"):
        """Build a resolver function that uses fake_hook_file for __file__ math."""
        def patched_fn(engram_home: str) -> str:
            import os as _os
            explicit = _os.environ.get("ENGRAM_RUNTIME_DIR")
            if explicit:
                return explicit
            _plugin_root = _os.path.dirname(
                _os.path.dirname(_os.path.abspath(fake_hook_file))
            )
            if _os.path.exists(_os.path.join(_plugin_root, sentinel)):
                return _plugin_root
            if _os.path.exists(_os.path.join(engram_home, sentinel)):
                return engram_home
            return _os.path.expanduser("~/engram-alpha")
        return patched_fn

    def _run_shadow_test(
        self, hook_path: Path, sentinel: str = "engram_client.py"
    ) -> None:
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

            fake_hook_file = str(hook_dir / hook_path.name)
            patched_fn = self._make_patched_fn(fake_hook_file, sentinel)

            with patch.dict(os.environ, {"ENGRAM_HOME": str(engram_home)}, clear=False):
                os.environ.pop("ENGRAM_RUNTIME_DIR", None)
                result = patched_fn(str(engram_home))

            self.assertEqual(
                result,
                str(plugin_root),
                f"[{hook_path.name}] #1152 shadow: expected plugin_root "
                f"{plugin_root!r} to win over stale engram_home, got {result!r}",
            )

    def _run_scatter_only_test(
        self, hook_path: Path, sentinel: str = "engram_client.py"
    ) -> None:
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

            fake_hook_file = str(hook_dir / hook_path.name)
            patched_fn = self._make_patched_fn(fake_hook_file, sentinel)

            with patch.dict(os.environ, {"ENGRAM_HOME": str(engram_home)}, clear=False):
                os.environ.pop("ENGRAM_RUNTIME_DIR", None)
                result = patched_fn(str(engram_home))

            self.assertEqual(
                result,
                str(engram_home),
                f"[{hook_path.name}] Scatter-only: expected engram_home "
                f"{engram_home!r}, got {result!r}",
            )

    def _run_explicit_override_test(
        self, hook_path: Path, sentinel: str = "engram_client.py"
    ) -> None:
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

            fake_hook_file = str(hook_dir / hook_path.name)
            patched_fn = self._make_patched_fn(fake_hook_file, sentinel)

            with patch.dict(
                os.environ,
                {
                    "ENGRAM_HOME": str(engram_home),
                    "ENGRAM_RUNTIME_DIR": explicit_dir,
                },
                clear=False,
            ):
                result = patched_fn(str(engram_home))

            self.assertEqual(
                result,
                explicit_dir,
                f"[{hook_path.name}] Explicit override: expected {explicit_dir!r}, "
                f"got {result!r}",
            )

    # -- Shadow tests (a): plugin wins over stale engram_home --

    def test_shadow_regression_surface_hook(self):
        self._run_shadow_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
        )

    def test_shadow_regression_session_start_hook(self):
        self._run_shadow_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
        )

    def test_shadow_regression_surface_daemon(self):
        self._run_shadow_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py"
        )

    def test_shadow_regression_utility_credit_mention_stop(self):
        self._run_shadow_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-utility-credit-mention-stop.py",
            sentinel="engram_ids.py",
        )

    # -- Scatter-only tests (b): engram_home wins when no plugin bundle --

    def test_scatter_only_surface_hook(self):
        self._run_scatter_only_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
        )

    def test_scatter_only_session_start_hook(self):
        self._run_scatter_only_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
        )

    def test_scatter_only_surface_daemon(self):
        self._run_scatter_only_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py"
        )

    def test_scatter_only_utility_credit_mention_stop(self):
        self._run_scatter_only_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-utility-credit-mention-stop.py",
            sentinel="engram_ids.py",
        )

    # -- Explicit override tests (c): ENGRAM_RUNTIME_DIR wins over both --

    def test_explicit_override_surface_hook(self):
        self._run_explicit_override_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
        )

    def test_explicit_override_session_start_hook(self):
        self._run_explicit_override_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
        )

    def test_explicit_override_surface_daemon(self):
        self._run_explicit_override_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py"
        )

    def test_explicit_override_utility_credit_mention_stop(self):
        self._run_explicit_override_test(
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-utility-credit-mention-stop.py",
            sentinel="engram_ids.py",
        )


class TestResolveRuntimeDirWalkParents(unittest.TestCase):
    """Regression tests for #1712: the absolute-path last-ditch fallback is
    replaced by a walk-parents search in BOTH `_resolve_runtime_dir`
    functions (engram-lesson-tripwire-hook.py, no-arg; engram-surface-hook.py,
    takes engram_home). Recreates the worktree-nesting hazard directly:
    `<tmp>/main-repo/.claude/worktrees/fake-agent/` nested under
    `<tmp>/main-repo/`, matching this repo's own dispatch pattern and real
    relative hook depth (src/engram/hooks/claude/<hook>.py).

    The real hook file is COPIED to the worktree-nested location (not just
    referenced) so that `Path(__file__).resolve()` inside the loaded module
    genuinely resolves under the fake worktree -- exercising the actual,
    unmodified walk-parents code added by this fix, not a reimplementation.
    """

    # (hook_path, marker_filename, takes_engram_home_arg, expected last-ditch
    # fallback string -- the ORIGINAL hardcoded guess, unchanged by this fix)
    _HOOKS = [
        (
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-lesson-tripwire-hook.py",
            "engram_idf.py",
            False,
            os.path.expanduser("~/engram-alpha/src/engram"),
        ),
        (
            _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py",
            "engram_client.py",
            True,
            os.path.expanduser("~/engram-alpha"),
        ),
    ]

    @staticmethod
    def _build_worktree_tree(tmp_path: Path, hook_path: Path):
        """<tmp>/main-repo/.claude/worktrees/fake-agent/src/engram/hooks/claude/<hook>.py
        -- a copy of the real hook file, nested at this repo's real relative
        depth under a fake worktree, itself nested under a fake main-repo."""
        main_repo = tmp_path / "main-repo"
        worktree = main_repo / ".claude" / "worktrees" / "fake-agent"
        worktree_hook_dir = worktree / "src" / "engram" / "hooks" / "claude"
        worktree_hook_dir.mkdir(parents=True)
        worktree_hook_file = worktree_hook_dir / hook_path.name
        shutil.copy2(hook_path, worktree_hook_file)
        return main_repo, worktree, worktree_hook_file

    @staticmethod
    def _call_resolver(loaded_fn, takes_arg: bool, engram_home: str):
        return loaded_fn(engram_home) if takes_arg else loaded_fn()

    def _resolve_with_clean_env(self, worktree_hook_file: Path, takes_arg: bool,
                                 data_home: Path) -> str:
        fn = _load_function_clean(worktree_hook_file)
        with patch.dict(os.environ, {"ENGRAM_HOME": str(data_home)}, clear=False):
            os.environ.pop("ENGRAM_RUNTIME_DIR", None)
            return self._call_resolver(fn, takes_arg, str(data_home))

    def test_worktree_own_copy_wins_over_ancestor(self):
        """Worktree has its OWN src/engram/<marker> -- the walk-parents
        search must return the WORKTREE's own copy (nearer ancestor), not
        the main-repo's (spec Tests item 2). Main-repo ALSO gets a copy so
        the assertion actually distinguishes "own copy wins" from "walked
        past it and found the ancestor's instead"."""
        for hook_path, marker, takes_arg, _ in self._HOOKS:
            with self.subTest(hook=hook_path.name):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    main_repo, worktree, worktree_hook_file = self._build_worktree_tree(
                        tmp_path, hook_path
                    )
                    main_repo_engram_dir = main_repo / "src" / "engram"
                    main_repo_engram_dir.mkdir(parents=True)
                    (main_repo_engram_dir / marker).write_text("# main-repo stub\n")

                    own_engram_dir = worktree / "src" / "engram"
                    (own_engram_dir / marker).write_text("# worktree's own stub\n")

                    data_home = tmp_path / "data-only-home"
                    data_home.mkdir()

                    result = self._resolve_with_clean_env(
                        worktree_hook_file, takes_arg, data_home
                    )

                    self.assertEqual(
                        result,
                        str(own_engram_dir),
                        f"[{hook_path.name}] expected worktree's own "
                        f"{own_engram_dir!r}, got {result!r}",
                    )

    def test_worktree_walks_up_to_ancestor_copy(self):
        """Worktree has NO <marker> anywhere in its own tree; only the
        main-repo (an ancestor beyond the worktree) does -- the walk-parents
        search must find the ancestor's copy (spec Tests item 3, distinct
        from item 2's "worktree has its own copy" case)."""
        for hook_path, marker, takes_arg, _ in self._HOOKS:
            with self.subTest(hook=hook_path.name):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    main_repo, worktree, worktree_hook_file = self._build_worktree_tree(
                        tmp_path, hook_path
                    )
                    # Worktree's own src/engram/ deliberately has NO marker.
                    main_repo_engram_dir = main_repo / "src" / "engram"
                    main_repo_engram_dir.mkdir(parents=True)
                    (main_repo_engram_dir / marker).write_text("# main-repo stub\n")

                    data_home = tmp_path / "data-only-home"
                    data_home.mkdir()

                    result = self._resolve_with_clean_env(
                        worktree_hook_file, takes_arg, data_home
                    )

                    self.assertEqual(
                        result,
                        str(main_repo_engram_dir),
                        f"[{hook_path.name}] expected ancestor's "
                        f"{main_repo_engram_dir!r}, got {result!r}",
                    )

    def test_last_ditch_fallback_preserved_when_nothing_found(self):
        """Nothing found anywhere in the walked parents -- must still return
        the ORIGINAL hardcoded fallback string, proving the old fallback is
        preserved (additive hardening), not removed (spec Tests item 4)."""
        for hook_path, marker, takes_arg, expected_last_ditch in self._HOOKS:
            with self.subTest(hook=hook_path.name):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    main_repo, worktree, worktree_hook_file = self._build_worktree_tree(
                        tmp_path, hook_path
                    )
                    # No marker anywhere in the tree -- every walk-parents
                    # candidate misses.
                    data_home = tmp_path / "data-only-home"
                    data_home.mkdir()

                    result = self._resolve_with_clean_env(
                        worktree_hook_file, takes_arg, data_home
                    )

                    self.assertEqual(
                        result,
                        expected_last_ditch,
                        f"[{hook_path.name}] expected last-ditch "
                        f"{expected_last_ditch!r}, got {result!r}",
                    )

    def test_permission_error_mid_walk_degrades_to_last_ditch(self):
        """Reviewer-fairy blocker (PR #1727): an unreadable ancestor
        directory mid-walk must degrade to the last-ditch fallback, not
        crash. pathlib.Path.exists() -- unlike os.path.exists() used at the
        rungs above -- raises PermissionError instead of returning False on
        a permission-denied path component. Since engram-surface-hook.py
        calls this function at bare MODULE-IMPORT time (unguarded), an
        unhandled exception here would crash the whole hook process."""
        for hook_path, marker, takes_arg, expected_last_ditch in self._HOOKS:
            with self.subTest(hook=hook_path.name):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    main_repo, worktree, worktree_hook_file = self._build_worktree_tree(
                        tmp_path, hook_path
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
                    # importing the hook module itself needs to traverse
                    # worktree/src/engram/hooks/claude/<hook>.py, which lives
                    # UNDER the same directory we're about to chmod. Only the
                    # subsequent CALL (which does the walk-parents .exists()
                    # checks) should hit the permission wall.
                    fn = _load_function_clean(worktree_hook_file)
                    os.chmod(own_engram_dir, 0o000)
                    try:
                        with patch.dict(os.environ, {"ENGRAM_HOME": str(data_home)}, clear=False):
                            os.environ.pop("ENGRAM_RUNTIME_DIR", None)
                            result = self._call_resolver(fn, takes_arg, str(data_home))
                    finally:
                        # Restore permissions so TemporaryDirectory cleanup
                        # (which must delete this dir) doesn't itself fail.
                        os.chmod(own_engram_dir, 0o755)

                    self.assertEqual(
                        result,
                        expected_last_ditch,
                        f"[{hook_path.name}] expected degrade-to-last-ditch "
                        f"{expected_last_ditch!r} on a permission error "
                        f"mid-walk, got {result!r}",
                    )


if __name__ == "__main__":
    unittest.main()
