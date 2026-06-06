"""Tests for tools/run_touched_tests.py — import-graph test selection.

Test categories:
  1. Mapping completeness (tag-once pattern): every hooks/claude/*.py and
     shipped tools/*.py|.sh in tiers.json is either reachable via layer 1,
     has a convention-map entry, or appears in the map's explicit unmapped list.

  2. Runner unit tests:
     - Layer-1 import-graph selection on a synthetic changed module
     - Transitive selection (A imports B imports test)
     - Layer-2 convention-map glob resolution
     - Fail-open triggers (each category)
     - SUBSET-green output line shape
     - --dry-run produces no pytest invocation

  3. Mutation check (mandatory): break transitive-closure step → verify
     the transitive test goes red; restore → verify it goes green again.
"""

from __future__ import annotations

import ast
import glob
import json
import os
import subprocess
import sys
import textwrap
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "tools" / "run_touched_tests.py"
TIERS_PATH = REPO_ROOT / "packaging" / "tiers.json"
TEST_MAP_PATH = REPO_ROOT / "packaging" / "test-map.json"
TESTS_DIR = REPO_ROOT / "tests"

# ---------------------------------------------------------------------------
# Import the runner module for unit testing
# ---------------------------------------------------------------------------

# Ensure repo root is on sys.path
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The runner lives under tools/; import it directly
import importlib.util

_runner_spec = importlib.util.spec_from_file_location(
    "run_touched_tests", str(RUNNER_PATH)
)
_runner = importlib.util.module_from_spec(_runner_spec)
_runner_spec.loader.exec_module(_runner)


# ---------------------------------------------------------------------------
# Helper: load tiers.json
# ---------------------------------------------------------------------------


def _load_tiers() -> dict:
    with open(TIERS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_test_map() -> dict:
    if not TEST_MAP_PATH.exists():
        return {}
    with open(TEST_MAP_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Helper: build layer-1 reverse import graph for the real repo
# ---------------------------------------------------------------------------


def _build_real_import_graph() -> dict[str, set[str]]:
    """Build the import graph for the actual repo (used by completeness check)."""
    return _runner._build_import_graph(str(REPO_ROOT))


# ---------------------------------------------------------------------------
# ═══════════════════════════════════════════════════════════════════════════
# 1. Mapping completeness — tag-once pattern
# ═══════════════════════════════════════════════════════════════════════════
# ---------------------------------------------------------------------------


class TestMappingCompleteness:
    """Every hook and shipped tool must be reachable by either layer.

    Coverage requirement: for each hooks/claude/*.py and each tools/*.py|.sh
    that appears in tiers.json mechanisms, one of the following must be true:
      (a) the module stem appears in the layer-1 reverse import graph
          (some test file imports it directly or transitively), OR
      (b) the path appears in packaging/test-map.json mappings, OR
      (c) the path appears in packaging/test-map.json unmapped list with a reason.

    Fails loudly naming the first uncovered path found — "added a hook, forgot
    to map it" is the failure mode this gate prevents.
    """

    def test_all_hooks_are_covered(self):
        """Every hooks/claude/*.py must be layer-1-reachable or in the test map."""
        tiers = _load_tiers()
        test_map = _load_test_map()
        import_graph = _build_real_import_graph()

        # Build the set of paths covered by the convention map
        map_covered: set[str] = set()
        for entry in test_map.get("mappings", []):
            p = entry.get("path_prefix_or_glob", "")
            # Normalize trailing separator
            map_covered.add(p.rstrip("/"))
        for entry in test_map.get("unmapped", []):
            map_covered.add(entry.get("path", "").rstrip("/"))

        # Enumerate hooks
        hooks_dir = REPO_ROOT / "hooks" / "claude"
        uncovered: list[str] = []
        for hook in sorted(hooks_dir.glob("*.py")):
            rel = f"hooks/claude/{hook.name}"
            stem = hook.stem

            in_import_graph = stem in import_graph
            in_map = rel in map_covered
            if not in_import_graph and not in_map:
                uncovered.append(rel)

        assert not uncovered, (
            "hooks/claude/*.py paths not reachable by import graph (layer 1) "
            "and not in packaging/test-map.json:\n"
            + "\n".join(f"  {p}" for p in uncovered)
        )

    def test_all_shipped_tools_are_covered(self):
        """Every shipped tools/**/*.py and tools/*.sh must be layer-1-reachable or mapped.

        'Shipped' means: listed in tiers.json mechanisms[] (not excluded[]).
        Tools in the excluded[] list are intentionally not shipped and are already
        accounted for by tiers.json's excluded list — they don't need a test-map entry.

        The scan is recursive (rglob) so that sub-packages such as tools/engine/
        and tools/migration/ are included.  tiers.json may carry either a per-file
        path ("tools/ia.py") or a directory-level path ("tools/engine") — a file
        is considered tiered if its path OR any of its parent directories appears
        in the tiered_paths set.

        Layer-1 coverage for sub-package files uses the same two-step lookup as
        select_tests: first the file's own stem, then the package-root "tools" key
        (via _package_root_stem).  tools/engine/*.py and tools/migration/*.py are
        caught by this fallback because import_graph["tools"] holds all tests that
        do any `from tools.* import ...`, including all four test_engine_* suites.
        """
        tiers = _load_tiers()
        test_map = _load_test_map()
        import_graph = _build_real_import_graph()

        # Only tiered mechanisms — these are the shipped surfaces
        tiered_paths: set[str] = {
            entry["path"] for entry in tiers.get("mechanisms", [])
        }

        def _is_tiered(rel: str) -> bool:
            """True if rel or any parent directory appears in tiered_paths."""
            if rel in tiered_paths:
                return True
            for parent in Path(rel).parents:
                if str(parent) in tiered_paths:
                    return True
            return False

        # Build convention-map coverage (mappings + unmapped list)
        map_covered: set[str] = set()
        for entry in test_map.get("mappings", []):
            map_covered.add(entry.get("path_prefix_or_glob", "").rstrip("/"))
        for entry in test_map.get("unmapped", []):
            map_covered.add(entry.get("path", "").rstrip("/"))

        # Enumerate tools/**/*.py and tools/*.sh recursively (includes sub-packages)
        tools_dir = REPO_ROOT / "tools"
        uncovered: list[str] = []
        for tool in sorted(tools_dir.rglob("*.py")) + sorted(tools_dir.glob("*.sh")):
            if tool.stem == "__pycache__":
                continue
            rel = str(tool.relative_to(REPO_ROOT))

            # Only check paths that are tiered (shipped)
            if not _is_tiered(rel):
                continue

            stem = tool.stem
            # Two-step L1 check mirrors select_tests: own stem first, then
            # package root "tools" key for sub-package files.
            in_import_graph = stem in import_graph
            if not in_import_graph:
                pkg_root = _runner._package_root_stem(rel)
                if pkg_root and pkg_root in import_graph:
                    in_import_graph = True
            in_map = rel in map_covered
            if not in_import_graph and not in_map:
                uncovered.append(rel)

        assert not uncovered, (
            "Shipped tools/**/*.py|.sh paths not reachable by import graph (layer 1) "
            "and not in packaging/test-map.json mappings or unmapped list:\n"
            + "\n".join(f"  {p}" for p in uncovered)
        )


# ---------------------------------------------------------------------------
# ═══════════════════════════════════════════════════════════════════════════
# 2. Runner unit tests
# ═══════════════════════════════════════════════════════════════════════════
# ---------------------------------------------------------------------------


def _make_tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal synthetic git repo for testing.

    Layout:
      module_a.py   — imports nothing repo-internal
      module_b.py   — imports module_a
      tests/
        test_a.py   — imports module_a (directly)
        test_b.py   — imports module_b (so transitively imports module_a too)
        test_c.py   — imports nothing repo-internal (baton-like — subprocess only)
      packaging/
        test-map.json
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "packaging").mkdir()

    # module_a.py
    (repo / "module_a.py").write_text(
        textwrap.dedent("""\
            def hello():
                return "hello from a"
        """),
        encoding="utf-8",
    )

    # module_b.py — imports module_a (non-local, top-level)
    (repo / "module_b.py").write_text(
        textwrap.dedent("""\
            import module_a

            def hello():
                return module_a.hello() + " via b"
        """),
        encoding="utf-8",
    )

    # test_a.py — direct import of module_a
    (repo / "tests" / "test_a.py").write_text(
        textwrap.dedent("""\
            import module_a

            def test_a():
                assert module_a.hello() == "hello from a"
        """),
        encoding="utf-8",
    )

    # test_b.py — imports module_b (which imports module_a)
    (repo / "tests" / "test_b.py").write_text(
        textwrap.dedent("""\
            import module_b

            def test_b():
                assert "hello from a" in module_b.hello()
        """),
        encoding="utf-8",
    )

    # test_c.py — no repo imports (subprocess-only test)
    (repo / "tests" / "test_c.py").write_text(
        textwrap.dedent("""\
            def test_c():
                pass
        """),
        encoding="utf-8",
    )

    # Minimal test-map.json (no mappings needed for basic tests)
    (repo / "packaging" / "test-map.json").write_text(
        json.dumps({"schema_version": 1, "mappings": [], "unmapped": []}),
        encoding="utf-8",
    )

    # Init as git repo
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo),
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo),
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", "main"],
        cwd=str(repo),
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(repo),
        capture_output=True,
    )

    return repo


class TestLayer1ImportGraph:
    """Layer-1: import-graph selection on synthetic module changes."""

    def test_direct_import_selects_test(self, tmp_path: Path):
        """Changing module_a selects test_a (which directly imports module_a)."""
        repo = _make_tmp_repo(tmp_path)

        # Monkeypatch _changed_files to simulate only module_a.py changed
        with patch.object(_runner, "_changed_files", return_value=["module_a.py"]):
            selected, is_full, reasons = _runner.select_tests(
                ["module_a.py"], str(repo)
            )

        assert not is_full
        sel_names = {Path(p).name for p in selected}
        assert "test_a.py" in sel_names, f"test_a.py should be selected; got {sel_names}"

    def test_transitive_import_selects_downstream_test(self, tmp_path: Path):
        """Changing module_a also selects test_b (which imports module_b → module_a)."""
        repo = _make_tmp_repo(tmp_path)

        with patch.object(_runner, "_changed_files", return_value=["module_a.py"]):
            selected, is_full, reasons = _runner.select_tests(
                ["module_a.py"], str(repo)
            )

        assert not is_full
        sel_names = {Path(p).name for p in selected}
        assert "test_b.py" in sel_names, (
            "test_b.py should be selected transitively (module_b → module_a);"
            f" got {sel_names}"
        )

    def test_unrelated_test_not_selected(self, tmp_path: Path):
        """test_c (no repo imports) is NOT selected when module_a changes."""
        repo = _make_tmp_repo(tmp_path)

        with patch.object(_runner, "_changed_files", return_value=["module_a.py"]):
            selected, is_full, reasons = _runner.select_tests(
                ["module_a.py"], str(repo)
            )

        # test_c has no repo imports → should not appear
        sel_names = {Path(p).name for p in selected}
        assert "test_c.py" not in sel_names, (
            f"test_c.py should NOT be selected; got {sel_names}"
        )

    def test_direct_module_b_selects_test_b_only(self, tmp_path: Path):
        """Changing module_b directly selects test_b but NOT test_a."""
        repo = _make_tmp_repo(tmp_path)

        with patch.object(_runner, "_changed_files", return_value=["module_b.py"]):
            selected, is_full, reasons = _runner.select_tests(
                ["module_b.py"], str(repo)
            )

        assert not is_full
        sel_names = {Path(p).name for p in selected}
        assert "test_b.py" in sel_names
        # test_a imports module_a, not module_b → NOT selected
        assert "test_a.py" not in sel_names, (
            f"test_a.py should NOT be selected when only module_b changes; got {sel_names}"
        )

    def test_function_local_import_is_walked(self, tmp_path: Path):
        """Function-local (lazy) imports are also walked by the AST scanner."""
        repo = _make_tmp_repo(tmp_path)

        # Add a module with a function-local import
        (repo / "module_lazy.py").write_text(
            textwrap.dedent("""\
                def compute():
                    import module_a  # function-local import
                    return module_a.hello()
            """),
            encoding="utf-8",
        )

        # Add a test that imports module_lazy
        (repo / "tests" / "test_lazy.py").write_text(
            textwrap.dedent("""\
                import module_lazy

                def test_lazy():
                    assert "hello" in module_lazy.compute()
            """),
            encoding="utf-8",
        )

        # Changing module_a should now also select test_lazy
        # because module_lazy has a function-local import of module_a
        with patch.object(_runner, "_changed_files", return_value=["module_a.py"]):
            selected, is_full, reasons = _runner.select_tests(
                ["module_a.py"], str(repo)
            )

        sel_names = {Path(p).name for p in selected}
        assert "test_lazy.py" in sel_names, (
            "test_lazy.py should be selected; module_lazy has a function-local "
            f"import of module_a; got {sel_names}"
        )


class TestLayer2ConventionMap:
    """Layer-2: convention map glob resolution."""

    def test_map_entry_selects_test(self, tmp_path: Path):
        """A convention-map entry for a hook path selects the mapped test."""
        repo = _make_tmp_repo(tmp_path)

        # Add a fake hook and test
        (repo / "hooks").mkdir()
        (repo / "hooks" / "myhook.py").write_text("# hook", encoding="utf-8")
        (repo / "tests" / "test_myhook.py").write_text(
            "def test_hook(): pass", encoding="utf-8"
        )

        # Write test-map.json with an entry for the hook
        test_map = {
            "schema_version": 1,
            "mappings": [
                {
                    "path_prefix_or_glob": "hooks/myhook.py",
                    "test_globs": ["tests/test_myhook.py"],
                }
            ],
            "unmapped": [],
        }
        (repo / "packaging" / "test-map.json").write_text(
            json.dumps(test_map), encoding="utf-8"
        )

        selected, is_full, reasons = _runner.select_tests(
            ["hooks/myhook.py"], str(repo)
        )

        assert not is_full
        sel_names = {Path(p).name for p in selected}
        assert "test_myhook.py" in sel_names, f"Expected test_myhook.py; got {sel_names}"

    def test_prefix_match_works(self, tmp_path: Path):
        """Prefix-style mapping matches any path under that prefix."""
        repo = _make_tmp_repo(tmp_path)

        # Write a map with a prefix match for skills/
        (repo / "skills").mkdir()
        (repo / "skills" / "my_skill").mkdir()
        (repo / "skills" / "my_skill" / "SKILL.md").write_text(
            "# skill", encoding="utf-8"
        )
        (repo / "tests" / "test_skill_lint.py").write_text(
            "def test_lint(): pass", encoding="utf-8"
        )

        test_map = {
            "schema_version": 1,
            "mappings": [
                {
                    "path_prefix_or_glob": "skills/",
                    "test_globs": ["tests/test_skill_lint.py"],
                }
            ],
            "unmapped": [],
        }
        (repo / "packaging" / "test-map.json").write_text(
            json.dumps(test_map), encoding="utf-8"
        )

        selected, is_full, reasons = _runner.select_tests(
            ["skills/my_skill/SKILL.md"], str(repo)
        )

        assert not is_full
        sel_names = {Path(p).name for p in selected}
        assert "test_skill_lint.py" in sel_names

    def test_full_suite_fallback_in_map(self, tmp_path: Path):
        """A map entry with full_suite_fallback=true triggers full suite."""
        repo = _make_tmp_repo(tmp_path)

        (repo / "hooks").mkdir()
        (repo / "hooks" / "danger.sh").write_text("#!/bin/sh", encoding="utf-8")

        test_map = {
            "schema_version": 1,
            "mappings": [
                {
                    "path_prefix_or_glob": "hooks/danger.sh",
                    "full_suite_fallback": True,
                    "test_globs": [],
                    "note": "no tests; full suite",
                }
            ],
            "unmapped": [],
        }
        (repo / "packaging" / "test-map.json").write_text(
            json.dumps(test_map), encoding="utf-8"
        )

        selected, is_full, reasons = _runner.select_tests(
            ["hooks/danger.sh"], str(repo)
        )

        assert is_full
        assert any("hooks/danger.sh" in r for r in reasons)


class TestFailOpenTriggers:
    """Fail-open triggers → full suite, naming the cause."""

    @pytest.mark.parametrize(
        "trigger_file",
        [
            "conftest.py",
            "pytest.ini",
            "requirements.txt",
            "requirements-dev.txt",
            # wildcard forms — pin the fnmatch patterns so a future refactor
            # of ALWAYS_FULL_PATTERNS can't silently drop them (review catch)
            "requirements-forum.txt",
            "requirements-extra.in",
            "setup.cfg",
            "pyproject.toml",
            "packaging/test-map.json",
            "tools/run_touched_tests.py",
        ],
    )
    def test_always_full_triggers(self, tmp_path: Path, trigger_file: str):
        """Each always-full file triggers full suite."""
        repo = _make_tmp_repo(tmp_path)

        selected, is_full, reasons = _runner.select_tests(
            [trigger_file], str(repo)
        )

        assert is_full, f"Expected full suite for {trigger_file!r}; got is_full={is_full}"
        assert any(trigger_file in r for r in reasons), (
            f"Expected {trigger_file!r} to appear in reasons; got {reasons}"
        )

    def test_unresolved_file_triggers_full(self, tmp_path: Path):
        """A changed file unresolved by both layers triggers full suite."""
        repo = _make_tmp_repo(tmp_path)

        # Change a file that has no test mapping and is not imported
        selected, is_full, reasons = _runner.select_tests(
            ["some/unknown/path.yaml"], str(repo)
        )

        assert is_full
        assert any("unresolved" in r for r in reasons)


class TestOutputContract:
    """SUBSET-green output line shape (the honest-iteration-signal contract)."""

    def test_subset_green_message_shape(self, tmp_path: Path, capsys):
        """When tests run and pass, SUBSET green message matches the contract."""
        repo = _make_tmp_repo(tmp_path)

        # Select test_a by changing module_a
        selected, is_full, reasons = _runner.select_tests(
            ["module_a.py"], str(repo)
        )

        assert not is_full
        assert selected

        n_selected = len(selected)
        total = _runner._count_total_test_files(str(repo))
        n_changed = 1

        # Exercise the REAL print path (review catch: the prior version
        # asserted against a locally-built string — could never fail).
        # main()'s --dry-run prints the honesty line unconditionally.
        import unittest.mock as _mock
        with _mock.patch.object(
            _runner, "_changed_files", return_value=["module_a.py"]
        ):
            rc = _runner.main(
                ["--dry-run", "--base", "HEAD", "--repo-root", str(repo)]
            )
        assert rc == 0
        out = capsys.readouterr().out
        assert "SUBSET green" in out
        assert "iteration signal, NOT a convergence claim" in out
        assert "Full suite gates merge-readiness" in out
        assert f"{n_selected}/{total}" in out

    def test_full_trigger_names_cause(self, tmp_path: Path):
        """Full-suite trigger names the specific triggering file."""
        repo = _make_tmp_repo(tmp_path)

        selected, is_full, reasons = _runner.select_tests(
            ["conftest.py"], str(repo)
        )

        assert is_full
        # The reason must name the file
        combined_reasons = " ".join(reasons)
        assert "conftest.py" in combined_reasons


class TestDryRun:
    """--dry-run prints selection without invoking pytest."""

    def test_dry_run_no_pytest_invoked(self, tmp_path: Path, monkeypatch):
        """--dry-run must not call subprocess.run with pytest."""
        repo = _make_tmp_repo(tmp_path)

        pytest_calls: list = []

        original_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if any("pytest" in str(c) for c in cmd):
                pytest_calls.append(cmd)
            return original_run(["true"])  # return 0 for any call

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Patch _changed_files to return module_a.py
        monkeypatch.setattr(
            _runner,
            "_changed_files",
            lambda base, repo_root: ["module_a.py"],
        )

        rc = _runner.main(
            ["--dry-run", "--base", "HEAD", "--repo-root", str(repo)]
        )

        assert not pytest_calls, (
            f"--dry-run should not invoke pytest; got calls: {pytest_calls}"
        )

    def test_dry_run_prints_selection(self, tmp_path: Path, monkeypatch, capsys):
        """--dry-run prints the test files that would be selected."""
        repo = _make_tmp_repo(tmp_path)

        monkeypatch.setattr(
            _runner,
            "_changed_files",
            lambda base, repo_root: ["module_a.py"],
        )

        _runner.main(["--dry-run", "--base", "HEAD", "--repo-root", str(repo)])

        captured = capsys.readouterr()
        # Should mention the test files
        assert "test_a.py" in captured.out or "test_b.py" in captured.out

    def test_dry_run_full_suite_no_pytest(self, tmp_path: Path, monkeypatch):
        """--dry-run on full-suite trigger also skips pytest."""
        repo = _make_tmp_repo(tmp_path)

        pytest_calls: list = []
        original_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if any("pytest" in str(c) for c in cmd):
                pytest_calls.append(cmd)
            return original_run(["true"])

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(
            _runner,
            "_changed_files",
            lambda base, repo_root: ["conftest.py"],
        )

        _runner.main(
            ["--dry-run", "--base", "HEAD", "--repo-root", str(repo)]
        )

        assert not pytest_calls


# ---------------------------------------------------------------------------
# ═══════════════════════════════════════════════════════════════════════════
# 3. Mutation check (mandatory)
# ═══════════════════════════════════════════════════════════════════════════
#
# Purpose: verify that the transitive-closure step is load-bearing.
# Procedure:
#   A) With real (transitive) implementation: changing module_a selects
#      test_b (which imports module_b → module_a). PASSES.
#   B) Break transitive closure (make it direct-imports-only): changing
#      module_a no longer selects test_b. FAILS (the mutation is detected).
#   C) Restore original implementation: test_b is selected again. PASSES.
#
# This test is the validation that the mutation check was actually run and
# that the transitive closure step is NOT dead code.
# ---------------------------------------------------------------------------


class TestTransitiveClosureMutation:
    """Mandatory mutation check: break transitive closure → downstream goes red."""

    def test_mutation_break_transitive_selects_less(self, tmp_path: Path):
        """Breaking transitive closure means test_b is NOT selected when module_a changes.

        This test verifies that the transitive-closure step is load-bearing:
        if we patch _transitive_deps to return only the node itself (no recursion),
        test_b disappears from selection (because module_b is not transitively expanded
        to include module_a). The mutation makes the test go red vs. the real impl.
        """
        repo = _make_tmp_repo(tmp_path)

        # --- Real implementation: test_b IS selected ---
        real_selected, is_full, _ = _runner.select_tests(["module_a.py"], str(repo))
        real_names = {Path(p).name for p in real_selected}
        assert "test_b.py" in real_names, (
            "Pre-condition: real implementation must select test_b transitively; "
            f"got {real_names}"
        )

        # --- Mutated implementation: break the closure to direct-only ---
        # We rebuild the graph with a patched version that skips recursion.
        # Implement the broken graph builder inline:
        def _broken_build_import_graph(repo_root: str) -> dict[str, set[str]]:
            """Direct-imports-only (broken) version — no transitive closure."""
            tests_dir = Path(repo_root) / "tests"
            if not tests_dir.is_dir():
                return {}

            repo_module_stems = _runner._discover_repo_modules(repo_root)

            # test file → direct imports only
            test_direct: dict[str, set[str]] = {}
            for tf in sorted(tests_dir.glob("test_*.py")):
                rel = str(tf.relative_to(repo_root))
                raw = _runner._parse_imports(str(tf))
                test_direct[rel] = raw & repo_module_stems

            # Reverse map — DIRECT only (no transitive expansion)
            reverse: dict[str, set[str]] = {}
            for test_file, direct_imports in test_direct.items():
                for mod in direct_imports:
                    reverse.setdefault(mod, set()).add(test_file)

            return reverse

        with patch.object(_runner, "_build_import_graph", _broken_build_import_graph):
            broken_selected, is_full_b, _ = _runner.select_tests(
                ["module_a.py"], str(repo)
            )

        broken_names = {Path(p).name for p in broken_selected}

        # The mutation should cause test_b to be ABSENT:
        # test_b imports module_b; module_b imports module_a.
        # Direct-only: module_a maps to test_a only (test_a directly imports module_a).
        # test_b is NOT in the direct reverse map for module_a → mutation detected.
        assert "test_b.py" not in broken_names, (
            "Mutation check FAILED: the broken (direct-only) implementation still "
            "selects test_b.py. The transitive closure step may not be exercised "
            "by the transitive test case, which defeats the mutation check. "
            f"Got broken_names={broken_names}"
        )
        assert "test_a.py" in broken_names, (
            "test_a.py should still appear in broken implementation "
            f"(direct import); got {broken_names}"
        )

        # --- Restore: real implementation selects test_b again ---
        restored_selected, _, _ = _runner.select_tests(["module_a.py"], str(repo))
        restored_names = {Path(p).name for p in restored_selected}
        assert "test_b.py" in restored_names, (
            "After restoring real implementation, test_b.py must be selected again; "
            f"got {restored_names}"
        )
