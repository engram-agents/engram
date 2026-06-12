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
RUNNER_PATH = REPO_ROOT / "src" / "engram" / "tools" / "run_touched_tests.py"
TIERS_PATH = REPO_ROOT / "src" / "build" / "packaging" / "tiers.json"
TEST_MAP_PATH = REPO_ROOT / "src" / "engram" / "packaging" / "test-map.json"
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
# 0. Repo-root resolution — regression guard for issue #1122
# ═══════════════════════════════════════════════════════════════════════════
#
# Mutation-checkable guard: if someone reverts to parent.parent.parent, the
# test goes red (that call returns .../src, not the repo root).
# ---------------------------------------------------------------------------


class TestRepoRootResolution:
    """_find_repo_root must resolve to the actual repo root, not src/."""

    def test_find_repo_root_from_module_file(self):
        """Starting from the runner module's directory, the walk reaches repo root."""
        runner_dir = Path(_runner.__file__).resolve().parent
        result = _runner._find_repo_root(runner_dir)

        # Must contain the two marker files that define the repo root
        assert (result / "pytest.ini").exists(), (
            f"_find_repo_root returned {result!r} which has no pytest.ini"
        )
        assert (result / "tests").is_dir(), (
            f"_find_repo_root returned {result!r} which has no tests/ directory"
        )

    def test_result_is_not_src_dir(self):
        """The resolved repo root must not be the src/ directory (the off-by-one target)."""
        runner_dir = Path(_runner.__file__).resolve().parent
        result = _runner._find_repo_root(runner_dir)

        assert result.name != "src", (
            f"_find_repo_root returned {result!r} — this is the src/ directory, "
            "not the repo root.  The off-by-one bug (parent.parent.parent) is back."
        )

    def test_result_matches_known_repo_root(self):
        """The resolved root equals the repo root as computed by tests/ conftest path."""
        runner_dir = Path(_runner.__file__).resolve().parent
        result = _runner._find_repo_root(runner_dir)

        # REPO_ROOT in this file is computed from Path(__file__).resolve().parents[1]
        # (tests/conftest.py is at tests/, so parents[1] is the repo root).
        assert result == REPO_ROOT, (
            f"_find_repo_root({runner_dir!r}) returned {result!r}, "
            f"expected {REPO_ROOT!r}"
        )

    def test_raises_on_no_markers(self, tmp_path: Path):
        """RuntimeError is raised when no ancestor has the marker files."""
        # tmp_path has no pytest.ini or tests/ → walk exhausts without finding root
        with pytest.raises(RuntimeError, match="repo root.*not found"):
            _runner._find_repo_root(tmp_path)


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
        hooks_dir = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
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
    (repo / "src" / "engram" / "packaging").mkdir(parents=True, exist_ok=True)

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
    (repo / "src" / "engram" / "packaging" / "test-map.json").write_text(
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
            selected, is_full, reasons, _map_sel = _runner.select_tests(
                ["module_a.py"], str(repo)
            )

        assert not is_full
        sel_names = {Path(p).name for p in selected}
        assert "test_a.py" in sel_names, f"test_a.py should be selected; got {sel_names}"

    def test_transitive_import_selects_downstream_test(self, tmp_path: Path):
        """Changing module_a also selects test_b (which imports module_b → module_a)."""
        repo = _make_tmp_repo(tmp_path)

        with patch.object(_runner, "_changed_files", return_value=["module_a.py"]):
            selected, is_full, reasons, _map_sel = _runner.select_tests(
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
            selected, is_full, reasons, _map_sel = _runner.select_tests(
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
            selected, is_full, reasons, _map_sel = _runner.select_tests(
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
            selected, is_full, reasons, _map_sel = _runner.select_tests(
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
        (repo / "src" / "engram" / "packaging" / "test-map.json").write_text(
            json.dumps(test_map), encoding="utf-8"
        )

        selected, is_full, reasons, _map_sel = _runner.select_tests(
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
        (repo / "src" / "engram" / "packaging" / "test-map.json").write_text(
            json.dumps(test_map), encoding="utf-8"
        )

        selected, is_full, reasons, _map_sel = _runner.select_tests(
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
        (repo / "src" / "engram" / "packaging" / "test-map.json").write_text(
            json.dumps(test_map), encoding="utf-8"
        )

        selected, is_full, reasons, _map_sel = _runner.select_tests(
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

        selected, is_full, reasons, _map_sel = _runner.select_tests(
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
        selected, is_full, reasons, _map_sel = _runner.select_tests(
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
        selected, is_full, reasons, _map_sel = _runner.select_tests(
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

        selected, is_full, reasons, _map_sel = _runner.select_tests(
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
        real_selected, is_full, _, _ms = _runner.select_tests(["module_a.py"], str(repo))
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
            broken_selected, is_full_b, _, _ms2 = _runner.select_tests(
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
        restored_selected, _, _, _ms3 = _runner.select_tests(["module_a.py"], str(repo))
        restored_names = {Path(p).name for p in restored_selected}
        assert "test_b.py" in restored_names, (
            "After restoring real implementation, test_b.py must be selected again; "
            f"got {restored_names}"
        )


# ---------------------------------------------------------------------------
# ═══════════════════════════════════════════════════════════════════════════
# 4. #948 residue — selection banner split + audit gates
# ═══════════════════════════════════════════════════════════════════════════
# ---------------------------------------------------------------------------


def _make_tmp_repo_with_contract(tmp_path: Path) -> Path:
    """Extend _make_tmp_repo with contract-tier and map-selected test files.

    Adds to the base repo:
      module_c.py            — a simple module
      tests/test_c_payload.py — contract-tier file (basename matches test_*_payload.py)
      tests/test_map.py       — map-only file (will be mapped via convention map)
      tests/test_server_import.py — internals file that imports 'server'
      packaging/test-map.json — updated with a map entry for module_c.py
    """
    repo = _make_tmp_repo(tmp_path)

    # module_c.py — a simple module with no imports
    (repo / "module_c.py").write_text(
        textwrap.dedent("""\
            def greet():
                return "hello from c"
        """),
        encoding="utf-8",
    )

    # test_c_payload.py — contract-tier (basename pattern test_*_payload.py)
    (repo / "tests" / "test_c_payload.py").write_text(
        textwrap.dedent("""\
            # contract-tier: tests the MCP-payload contract for module_c
            import module_c
            import server  # allowed: contract-tier files may import server

            def test_c_payload():
                assert module_c.greet() == "hello from c"
        """),
        encoding="utf-8",
    )

    # test_map.py — will be reached via convention map only (not import graph)
    (repo / "tests" / "test_map.py").write_text(
        textwrap.dedent("""\
            def test_via_map():
                pass
        """),
        encoding="utf-8",
    )

    # test_server_import.py — internals file (L1-selected) that imports server
    # This is a "violating" file for --audit-imports purposes
    (repo / "tests" / "test_server_import.py").write_text(
        textwrap.dedent("""\
            import module_a
            import server  # should be contract-tier; violation for audit

            def test_server():
                pass
        """),
        encoding="utf-8",
    )

    # server.py — a fake "server" module so _discover_repo_modules finds it
    (repo / "server.py").write_text(
        textwrap.dedent("""\
            # fake server module for testing
        """),
        encoding="utf-8",
    )

    # Update test-map.json: map module_c.py → test_map.py via convention map,
    # and seed server_import_allowlist with test_c_payload.py (the allowlisted file)
    test_map = {
        "schema_version": 1,
        "mappings": [
            {
                "path_prefix_or_glob": "module_c.py",
                "test_globs": ["tests/test_map.py"],
            }
        ],
        "unmapped": [],
        "server_import_allowlist": ["test_c_payload.py"],
    }
    (repo / "src" / "engram" / "packaging" / "test-map.json").write_text(
        json.dumps(test_map), encoding="utf-8"
    )

    # Re-commit so git state is clean
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add contract fixtures"],
        cwd=str(repo),
        capture_output=True,
    )

    return repo


class TestBannerSplit:
    """Feature 1: selection banner split into contract / internals / map-residue."""

    def test_contract_file_counted_in_contract_bucket(self, tmp_path: Path):
        """A test_*_payload.py file is attributed to the contract bucket."""
        repo = _make_tmp_repo_with_contract(tmp_path)

        # Changing module_c.py selects test_c_payload.py (via import graph)
        # and test_map.py (via convention map)
        selected, is_full, _, map_sel = _runner.select_tests(
            ["module_c.py"], str(repo)
        )
        assert not is_full

        test_map = _runner._load_test_map(str(repo))
        allowlist = _runner._get_server_import_allowlist(test_map)
        n_internals, n_contract, n_map_residue = _runner._classify_selection(
            selected, map_sel, allowlist
        )

        sel_names = {Path(p).name for p in selected}
        assert "test_c_payload.py" in sel_names, (
            f"test_c_payload.py should be selected; got {sel_names}"
        )
        assert n_contract >= 1, (
            f"contract bucket should be >= 1; got n_contract={n_contract}"
        )

    def test_internals_file_counted_in_internals_bucket(self, tmp_path: Path):
        """A non-payload, non-map test file is attributed to internals."""
        repo = _make_tmp_repo_with_contract(tmp_path)

        # Changing module_a.py selects test_a.py and test_b.py via import graph
        selected, is_full, _, map_sel = _runner.select_tests(
            ["module_a.py"], str(repo)
        )
        assert not is_full

        test_map = _runner._load_test_map(str(repo))
        allowlist = _runner._get_server_import_allowlist(test_map)
        n_internals, n_contract, n_map_residue = _runner._classify_selection(
            selected, map_sel, allowlist
        )

        sel_names = {Path(p).name for p in selected}
        # test_a.py and test_b.py are internals (not payload, not map-only)
        assert "test_a.py" in sel_names or "test_b.py" in sel_names
        assert n_internals >= 1, (
            f"internals bucket should be >= 1; got n_internals={n_internals}"
        )

    def test_map_selected_file_counted_in_map_residue_bucket(self, tmp_path: Path):
        """A file reached ONLY via convention map is attributed to map-residue."""
        repo = _make_tmp_repo_with_contract(tmp_path)

        # module_c.py is in the convention map → test_map.py is map-only selected
        # (test_map.py doesn't import module_c, so L1 won't reach it)
        selected, is_full, _, map_sel = _runner.select_tests(
            ["module_c.py"], str(repo)
        )
        assert not is_full

        test_map = _runner._load_test_map(str(repo))
        allowlist = _runner._get_server_import_allowlist(test_map)
        n_internals, n_contract, n_map_residue = _runner._classify_selection(
            selected, map_sel, allowlist
        )

        sel_names = {Path(p).name for p in selected}
        assert "test_map.py" in sel_names, (
            f"test_map.py should be selected via convention map; got {sel_names}"
        )
        assert n_map_residue >= 1, (
            f"map-residue bucket should be >= 1; got n_map_residue={n_map_residue}"
        )

    def test_bucket_counts_sum_to_total(self, tmp_path: Path):
        """internals + contract + map-residue must equal the total selected count."""
        repo = _make_tmp_repo_with_contract(tmp_path)

        selected, is_full, _, map_sel = _runner.select_tests(
            ["module_c.py"], str(repo)
        )
        assert not is_full

        test_map = _runner._load_test_map(str(repo))
        allowlist = _runner._get_server_import_allowlist(test_map)
        n_internals, n_contract, n_map_residue = _runner._classify_selection(
            selected, map_sel, allowlist
        )

        assert n_internals + n_contract + n_map_residue == len(selected), (
            f"bucket counts must sum to total selected: "
            f"{n_internals}+{n_contract}+{n_map_residue} != {len(selected)}"
        )

    def test_banner_line_present_in_dry_run_output(self, tmp_path: Path, capsys, monkeypatch):
        """--dry-run output includes the [selection] bucket line."""
        repo = _make_tmp_repo_with_contract(tmp_path)

        monkeypatch.setattr(
            _runner,
            "_changed_files",
            lambda base, repo_root: ["module_a.py"],
        )

        rc = _runner.main(["--dry-run", "--base", "HEAD", "--repo-root", str(repo)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[selection]" in out, f"Expected [selection] bucket line; got:\n{out}"
        assert "internals" in out
        assert "contract" in out
        assert "map-residue" in out


class TestAuditImports:
    """Feature 2: --audit-imports flag (D3 dual-direction shape)."""

    def test_audit_imports_catches_violating_file(self, tmp_path: Path):
        """A file that imports server but is not contract-tier is flagged."""
        repo = _make_tmp_repo_with_contract(tmp_path)
        test_map = _runner._load_test_map(str(repo))

        violations = _runner._audit_imports(str(repo), test_map)

        # test_server_import.py imports server and is not test_*_payload.py
        viol_names = {Path(v).name for v in violations}
        assert "test_server_import.py" in viol_names, (
            f"test_server_import.py should be a violation; got {viol_names}"
        )

    def test_audit_imports_respects_payload_pattern(self, tmp_path: Path):
        """A test_*_payload.py file is NOT flagged even if it imports server."""
        repo = _make_tmp_repo_with_contract(tmp_path)
        test_map = _runner._load_test_map(str(repo))

        violations = _runner._audit_imports(str(repo), test_map)

        viol_names = {Path(v).name for v in violations}
        assert "test_c_payload.py" not in viol_names, (
            "test_c_payload.py is contract-tier and must not be flagged as a violation"
        )

    def test_audit_imports_respects_allowlist(self, tmp_path: Path):
        """A file in server_import_allowlist is NOT flagged."""
        repo = _make_tmp_repo_with_contract(tmp_path)

        # Add an allowlisted file that imports server
        (repo / "tests" / "test_allowlisted.py").write_text(
            textwrap.dedent("""\
                import server  # allowed via allowlist
                def test_allowed(): pass
            """),
            encoding="utf-8",
        )

        # Update test-map to allowlist this file
        test_map_data = {
            "schema_version": 1,
            "mappings": [],
            "unmapped": [],
            "server_import_allowlist": ["test_allowlisted.py"],
        }
        (repo / "src" / "engram" / "packaging" / "test-map.json").write_text(
            json.dumps(test_map_data), encoding="utf-8"
        )
        test_map = _runner._load_test_map(str(repo))

        violations = _runner._audit_imports(str(repo), test_map)

        viol_names = {Path(v).name for v in violations}
        assert "test_allowlisted.py" not in viol_names, (
            "test_allowlisted.py is in the allowlist and must not be flagged"
        )

    def test_audit_imports_absent_allowlist_key_tolerated(self, tmp_path: Path):
        """Absent server_import_allowlist key doesn't crash — returns empty set."""
        test_map_without_key = {"schema_version": 1, "mappings": [], "unmapped": []}
        allowlist = _runner._get_server_import_allowlist(test_map_without_key)
        assert allowlist == set(), (
            "Missing server_import_allowlist key must yield empty set, not an error"
        )

    def test_audit_imports_strict_exit1_on_violations(self, tmp_path: Path, monkeypatch):
        """--audit-imports --strict exits 1 when violations exist."""
        repo = _make_tmp_repo_with_contract(tmp_path)

        rc = _runner.main(
            ["--audit-imports", "--strict", "--repo-root", str(repo)]
        )
        assert rc == 1, (
            f"Expected exit code 1 with --strict and violations present; got {rc}"
        )

    def test_audit_imports_strict_exit0_when_clean(self, tmp_path: Path):
        """--audit-imports --strict exits 0 when no violations exist."""
        repo = _make_tmp_repo(tmp_path)
        # Base repo has no server imports at all
        rc = _runner.main(
            ["--audit-imports", "--strict", "--repo-root", str(repo)]
        )
        assert rc == 0, (
            f"Expected exit code 0 with --strict and no violations; got {rc}"
        )

    def test_audit_imports_default_exit0_even_with_violations(
        self, tmp_path: Path, capsys
    ):
        """--audit-imports without --strict exits 0 regardless of violations."""
        repo = _make_tmp_repo_with_contract(tmp_path)

        rc = _runner.main(["--audit-imports", "--repo-root", str(repo)])
        assert rc == 0, (
            "Without --strict, --audit-imports must exit 0 even with violations"
        )
        out = capsys.readouterr().out
        # Should still report
        assert "audit-imports" in out


class TestAuditPatches:
    """Feature 3: --audit-patches flag (D3 dual-direction shape)."""

    def _make_repo_with_patch_fixtures(self, tmp_path: Path) -> Path:
        """Create a repo with monkeypatch fixture files for testing audit-patches."""
        repo = _make_tmp_repo(tmp_path)

        # server.py — a repo module
        (repo / "server.py").write_text("# fake server", encoding="utf-8")

        # test_patches_no_import.py — uses string monkeypatch on 'server' but
        # does NOT import server → should be a violation
        (repo / "tests" / "test_patches_no_import.py").write_text(
            textwrap.dedent("""\
                def test_patched(monkeypatch):
                    monkeypatch.setattr("server.some_func", lambda: "mocked")
            """),
            encoding="utf-8",
        )

        # test_patches_with_import.py — uses string monkeypatch on 'server'
        # AND imports server → should NOT be a violation
        (repo / "tests" / "test_patches_with_import.py").write_text(
            textwrap.dedent("""\
                import server

                def test_patched_ok(monkeypatch):
                    monkeypatch.setattr("server.some_func", lambda: "mocked")
            """),
            encoding="utf-8",
        )

        # test_setitem_no_import.py — setitem string target without the import
        # → should be a violation (dedicated D3 fire-direction for setitem)
        (repo / "tests" / "test_setitem_no_import.py").write_text(
            textwrap.dedent("""\
                def test_item_patched(monkeypatch):
                    monkeypatch.setitem("server.registry", "key", "val")
            """),
            encoding="utf-8",
        )

        # test_setitem_with_import.py — setitem string target WITH the import
        # → should NOT be a violation (dedicated D3 no-fire direction)
        (repo / "tests" / "test_setitem_with_import.py").write_text(
            textwrap.dedent("""\
                import server

                def test_item_patched_ok(monkeypatch):
                    monkeypatch.setitem("server.registry", "key", "val")
            """),
            encoding="utf-8",
        )

        # Re-commit
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add patch fixtures"],
            cwd=str(repo),
            capture_output=True,
        )

        return repo

    def test_audit_patches_catches_string_patch_without_import(self, tmp_path: Path):
        """A file with monkeypatch string target but missing import is flagged."""
        repo = self._make_repo_with_patch_fixtures(tmp_path)

        violations = _runner._audit_patches(str(repo))

        viol_names = {Path(v).name for v in violations}
        assert "test_patches_no_import.py" in viol_names, (
            "test_patches_no_import.py uses monkeypatch.setattr('server.X') "
            f"without importing server; should be flagged; got {viol_names}"
        )

    def test_audit_patches_passes_string_patch_with_import(self, tmp_path: Path):
        """A file with monkeypatch string target that also imports the module is NOT flagged."""
        repo = self._make_repo_with_patch_fixtures(tmp_path)

        violations = _runner._audit_patches(str(repo))

        viol_names = {Path(v).name for v in violations}
        assert "test_patches_with_import.py" not in viol_names, (
            "test_patches_with_import.py imports server AND uses string patch; "
            "must not be flagged as a violation"
        )

    def test_audit_patches_setitem_fires_without_import(self, tmp_path: Path):
        """setitem string target without the import is flagged (dedicated pair)."""
        repo = self._make_repo_with_patch_fixtures(tmp_path)

        violations = _runner._audit_patches(str(repo))

        viol_names = {Path(v).name for v in violations}
        assert "test_setitem_no_import.py" in viol_names, (
            f"setitem string patch without import must be flagged; got {viol_names}"
        )

    def test_audit_patches_setitem_clean_with_import(self, tmp_path: Path):
        """setitem string target with the import present is NOT flagged."""
        repo = self._make_repo_with_patch_fixtures(tmp_path)

        violations = _runner._audit_patches(str(repo))

        viol_names = {Path(v).name for v in violations}
        assert "test_setitem_with_import.py" not in viol_names, (
            "setitem with import present must not be flagged; "
            f"got {viol_names}"
        )

    def test_audit_patches_strict_exit1_on_violations(self, tmp_path: Path):
        """--audit-patches --strict exits 1 when violations exist."""
        repo = self._make_repo_with_patch_fixtures(tmp_path)

        rc = _runner.main(
            ["--audit-patches", "--strict", "--repo-root", str(repo)]
        )
        assert rc == 1, (
            f"Expected exit code 1 with --strict and patch violations; got {rc}"
        )

    def test_audit_patches_strict_exit0_when_clean(self, tmp_path: Path):
        """--audit-patches --strict exits 0 when no violations."""
        repo = _make_tmp_repo(tmp_path)
        # Base repo has no monkeypatch string calls at all
        rc = _runner.main(
            ["--audit-patches", "--strict", "--repo-root", str(repo)]
        )
        assert rc == 0, (
            f"Expected exit code 0 with --strict and no patch violations; got {rc}"
        )

    def test_audit_patches_default_exit0_even_with_violations(
        self, tmp_path: Path, capsys
    ):
        """--audit-patches without --strict exits 0 regardless of violations."""
        repo = self._make_repo_with_patch_fixtures(tmp_path)

        rc = _runner.main(["--audit-patches", "--repo-root", str(repo)])
        assert rc == 0, (
            "Without --strict, --audit-patches must exit 0 even with violations"
        )
        out = capsys.readouterr().out
        assert "audit-patches" in out

    def test_parse_monkeypatch_targets_extracts_string_args(self, tmp_path: Path):
        """_parse_monkeypatch_string_targets returns string-literal first args."""
        test_file = tmp_path / "test_example.py"
        test_file.write_text(
            textwrap.dedent("""\
                def test_x(monkeypatch):
                    monkeypatch.setattr("server.fn", lambda: None)
                    monkeypatch.delattr("server.other")
                    monkeypatch.setitem("server.registry", "key", "val")
                    monkeypatch.setattr(some_obj, "method", lambda: None)  # non-string: skip
            """),
            encoding="utf-8",
        )
        targets = _runner._parse_monkeypatch_string_targets(str(test_file))
        assert "server.fn" in targets
        assert "server.other" in targets
        assert "server.registry" in targets
        # Non-string first arg (some_obj) must not appear
        assert not any("some_obj" in t for t in targets)


# ---------------------------------------------------------------------------
# --json emitter (#864 P0 §3): machine-readable selection for run_matrix.py.
# Structured stdout instead of scraped banner prose. Selection internals are
# patched so each path is exercised deterministically (no reliance on git
# state); json.loads on captured stdout is itself the "stdout is pure JSON"
# guard (a banner-prose leak would make it fail).
# ---------------------------------------------------------------------------

_JSON_KEYS = [
    "base", "buckets", "changed_files", "full",
    "full_reasons", "selected", "total_test_files",
]


def _run_json(monkeypatch, argv, *, changed=None, select_ret=None,
              classify=None, total=172):
    import io
    import contextlib
    if changed is not None:
        monkeypatch.setattr(_runner, "_changed_files", lambda base, root: changed)
    if select_ret is not None:
        monkeypatch.setattr(_runner, "select_tests",
                            lambda c, r, verbose=False: select_ret)
    if classify is not None:
        monkeypatch.setattr(_runner, "_classify_selection", lambda *a, **k: classify)
    monkeypatch.setattr(_runner, "_count_total_test_files", lambda root: total)
    monkeypatch.setattr(_runner, "_load_test_map", lambda root: {})
    monkeypatch.setattr(_runner, "_get_server_import_allowlist", lambda m: set())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _runner.main(argv)
    assert rc == 0, f"--json must exit 0, got {rc}"
    return json.loads(buf.getvalue())  # also asserts stdout is pure, valid JSON


def test_json_full_flag(monkeypatch):
    d = _run_json(monkeypatch, ["--json", "--full"])
    assert d["full"] is True
    assert d["full_reasons"] == ["--full flag"]
    assert d["selected"] == []
    assert d["buckets"] == {"internals": 0, "contract": 0, "map_residue": 0}
    assert sorted(d) == _JSON_KEYS


def test_json_trigger_full(monkeypatch):
    # select_tests reports is_full=True (e.g. a full-trigger file changed).
    d = _run_json(
        monkeypatch, ["--json"],
        changed=["packaging/test-map.json"],
        select_ret=([], True, ["packaging/test-map.json changed → full"], {}),
    )
    assert d["full"] is True
    assert "test-map.json" in d["full_reasons"][0]
    assert d["selected"] == []
    assert sorted(d) == _JSON_KEYS


def test_json_subset_with_buckets(monkeypatch):
    d = _run_json(
        monkeypatch, ["--json"],
        changed=["engram_core.py"],
        select_ret=(["tests/test_core_x.py", "tests/test_core_y.py"], False, [],
                    {"tests/test_core_y.py"}),
        classify=(1, 1, 0),
    )
    assert d["full"] is False
    assert d["selected"] == ["tests/test_core_x.py", "tests/test_core_y.py"]
    assert d["changed_files"] == ["engram_core.py"]
    assert d["buckets"] == {"internals": 1, "contract": 1, "map_residue": 0}
    assert d["total_test_files"] == 172
    assert sorted(d) == _JSON_KEYS  # schema-key regression guard (subset path)


def test_json_empty_selection(monkeypatch):
    # Changed files resolve to no tests → full=false, selected empty, zero buckets.
    d = _run_json(
        monkeypatch, ["--json"],
        changed=["README.md"],
        select_ret=([], False, [], {}),
    )
    assert d["full"] is False
    assert d["selected"] == []
    assert d["buckets"] == {"internals": 0, "contract": 0, "map_residue": 0}
    assert sorted(d) == _JSON_KEYS  # schema-key regression guard (empty path)


def test_json_stdout_is_single_line_pure_json(monkeypatch):
    # Hard guard for the run_matrix consumer: exactly one line, no banner prose
    # (select_tests must run verbose=False so its [L1]/[direct] lines don't leak).
    import io
    import contextlib
    monkeypatch.setattr(_runner, "_changed_files", lambda b, r: ["engram_core.py"])
    monkeypatch.setattr(_runner, "select_tests",
                        lambda c, r, verbose=False: (["tests/test_a.py"], False, [], set()))
    monkeypatch.setattr(_runner, "_classify_selection", lambda *a, **k: (1, 0, 0))
    monkeypatch.setattr(_runner, "_count_total_test_files", lambda r: 5)
    monkeypatch.setattr(_runner, "_load_test_map", lambda r: {})
    monkeypatch.setattr(_runner, "_get_server_import_allowlist", lambda m: set())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _runner.main(["--json"])
    out = buf.getvalue().strip()
    assert out.count("\n") == 0, f"--json stdout must be a single line, got: {out!r}"
    json.loads(out)
