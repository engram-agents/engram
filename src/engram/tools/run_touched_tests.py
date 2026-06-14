#!/usr/bin/env python3
"""tools/run_touched_tests.py — import-graph test selection for fast PR iteration.

Two-layer deterministic, stateless, fail-open selection:

  Layer 1 — Static import graph:
    AST-parse all tests/*.py and repo-root source modules per run (no stored
    state — a stale selection DB would itself be a silent-green axis). Build
    a reverse map from source module → all test files that transitively import
    it. Changed module → selected test set.

  Layer 2 — Convention map (packaging/test-map.json):
    Non-imported surfaces (hooks, skills, templates, shell scripts) are mapped
    via a checked-in JSON file. Entries map path prefixes / globs to test globs.

  Fail-open triggers:
    Any changed file unresolved by both layers → full suite.
    Changes to conftest.py, pytest.ini, requirements*, test-map.json itself,
    or this script → full suite.

Output honesty (the green-is-not-ground-truth discipline):
  SUBSET green: prints "SUBSET green (N/TOTAL selected for M changed files) —
    iteration signal, NOT a convergence claim. Full suite gates merge-readiness."
  Full suite fallback: names the triggering file(s).

Usage:
  python3 tools/run_touched_tests.py [--base REF] [--full] [--dry-run]
                                     [--repo-root PATH] [-- pytest args...]
  python3 tools/run_touched_tests.py --audit-imports [--strict]
  python3 tools/run_touched_tests.py --audit-patches [--strict]
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import glob
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Generator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TESTS_DIR = "tests"
TEST_MAP_PATH = "src/engram/packaging/test-map.json"

# Files / globs that always trigger full suite (config/infra changes)
ALWAYS_FULL_PATTERNS: list[str] = [
    "conftest.py",
    "pytest.ini",
    "setup.cfg",
    "pyproject.toml",
    "requirements*.txt",
    "requirements*.in",
    "src/engram/packaging/test-map.json",
    "src/engram/tools/run_touched_tests.py",
]


# ---------------------------------------------------------------------------
# Repo-root resolution
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path:
    """Walk upward from *start* to find the repo root.

    The repo root is the lowest ancestor directory that contains BOTH
    ``pytest.ini`` and a ``tests/`` sub-directory.  This marker-walk is
    robust to file moves: adding one more ``parent`` call in the caller
    re-breaks after the next restructure, but the markers travel with the
    project.

    Raises RuntimeError if no qualifying ancestor is found.
    """
    for p in [start, *start.parents]:
        if (p / "pytest.ini").exists() and (p / "tests").is_dir():
            return p
    raise RuntimeError(
        f"repo root (pytest.ini + tests/) not found above {start}"
    )


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _changed_files(base: str, repo_root: str) -> list[str]:
    """Return list of changed files (relative to repo root) vs base ref.

    Combines:
      - git diff --name-only <base>...HEAD  (committed changes on branch)
      - git status --porcelain              (working-tree untracked/modified)
    """
    changed: set[str] = set()

    # Committed diff vs base
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    changed.add(line)
    except FileNotFoundError:
        pass  # git not available

    # Working-tree status: untracked + modified (fairies run pre-commit)
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if len(line) >= 3:
                    # Format: XY filename  (or XY old -> new for renames)
                    fname = line[3:].strip()
                    if " -> " in fname:
                        fname = fname.split(" -> ")[-1]
                    changed.add(fname)
    except FileNotFoundError:
        pass

    return sorted(changed)


# ---------------------------------------------------------------------------
# AST import walking (layer 1)
# ---------------------------------------------------------------------------


def _iter_imports(tree: ast.AST) -> Generator[str, None, None]:
    """Yield all imported module names (top-level AND function-local)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            # Absolute imports only; relative imports are test-internal
            if node.module and node.level == 0:
                yield node.module


def _parse_imports(path: str) -> set[str]:
    """Return the set of top-level module name stems imported in a Python file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            src = f.read()
        tree = ast.parse(src, filename=path)
    except (SyntaxError, OSError):
        return set()

    imports: set[str] = set()
    for mod in _iter_imports(tree):
        # Only the top-level package name matters for graph lookup
        imports.add(mod.split(".")[0])
    return imports


def _discover_repo_modules(repo_root: str) -> set[str]:
    """Return the set of Python module stems defined in the repo.

    Scans:
      - Root-level *.py files  (stem = module name)
      - tools/*.py files       (stem also importable after conftest sys.path setup)

    NOTE — L1 is effectively inert for `from tools.X import ...` style imports.
    When a test writes `from tools.ia import ...`, AST parsing yields top-level
    stem "tools" (the package name), not "ia".  So `import_graph["ia"]` stays
    empty; the tests land under `import_graph["tools"]`.  The convention map
    (Layer 2) correctly fills this gap for tools/*.py surfaces.  The "tools" key
    IS populated (tests that do any `from tools.*` import accumulate there), but
    it is only L1-reachable when a changed file maps to stem "tools" — see
    `_package_root_stem` for the explicit sub-package fallback that makes this
    key reachable for tools/engine/*.py and tools/migration/*.py changes.
    """
    modules: set[str] = set()

    # Root-level .py modules
    for p in Path(repo_root).glob("*.py"):
        modules.add(p.stem)

    # tools/*.py — conftest adds repo_root to sys.path; tools is a package too.
    # Filter "__init__" — it is never a useful lookup key (no test imports __init__)
    # and adding it to repo_module_stems pollutes module_deps with a dead entry.
    tools_dir = Path(repo_root) / "tools"
    if tools_dir.is_dir():
        for p in tools_dir.glob("*.py"):
            if p.stem != "__init__":
                modules.add(p.stem)
        if (tools_dir / "__init__.py").exists():
            modules.add("tools")

    return modules


def _path_to_module_stem(path: str) -> str | None:
    """Convert a repo-relative file path to a Python module stem.

    Returns None if the path is not a .py file.
    """
    if path.endswith(".py"):
        return Path(path).stem
    return None


def _package_root_stem(path: str) -> str | None:
    """Return the top-level package stem for files in a tools sub-package.

    tools/engine/build.py    → "tools"
    tools/migration/foo.py   → "tools"
    tools/ia.py              → None  (direct tools/ file; stem "ia" is the key)
    hooks/claude/foo.py      → None  (not a tools sub-package)

    Used as a fallback L1 lookup when the changed file's own stem is absent from
    import_graph.  import_graph["tools"] accumulates every test that does a
    `from tools.X import ...` — including all four test_engine_* suites.  A
    tools sub-package file change (tools/engine/build.py → stem "build", which
    is absent from import_graph) therefore finds its tests via this fallback.
    """
    parts = Path(path).parts
    # Sub-package files have at least 3 parts: ("tools", "<subdir>", "<file>.py")
    if len(parts) >= 3 and parts[0] == "tools" and path.endswith(".py"):
        return "tools"
    return None


def _build_import_graph(repo_root: str) -> dict[str, set[str]]:
    """Build the reverse import graph: module_stem → set of test files.

    Parses all tests/test_*.py files and all repo source modules.  Follows
    transitive deps so that if test_foo imports module_a and module_a imports
    module_b, changing module_b also selects test_foo.

    Returns {module_stem: {relative_test_file_path, ...}}.
    """
    tests_dir = Path(repo_root) / TESTS_DIR
    if not tests_dir.is_dir():
        return {}

    repo_module_stems = _discover_repo_modules(repo_root)

    # Map: test_file → directly-imported repo module stems
    test_direct: dict[str, set[str]] = {}
    for tf in sorted(tests_dir.glob("test_*.py")):
        rel = str(tf.relative_to(repo_root))
        raw = _parse_imports(str(tf))
        test_direct[rel] = raw & repo_module_stems

    # Map: module_stem → directly-imported repo module stems (for transitive walk)
    module_deps: dict[str, set[str]] = {}

    for p in Path(repo_root).glob("*.py"):
        stem = p.stem
        raw = _parse_imports(str(p))
        module_deps[stem] = raw & repo_module_stems

    tools_dir = Path(repo_root) / "tools"
    if tools_dir.is_dir():
        for p in tools_dir.glob("*.py"):
            stem = p.stem
            raw = _parse_imports(str(p))
            module_deps[stem] = raw & repo_module_stems

    # Compute transitive closure for a module stem
    def _transitive_deps(stem: str, visited: set[str] | None = None) -> set[str]:
        if visited is None:
            visited = set()
        if stem in visited:
            return visited
        visited.add(stem)
        for dep in module_deps.get(stem, set()):
            _transitive_deps(dep, visited)
        return visited

    # Reverse map: module_stem → test files that import it (transitively)
    reverse: dict[str, set[str]] = {}

    for test_file, direct_imports in test_direct.items():
        # Expand each direct import to its transitive closure
        all_needed: set[str] = set()
        for mod in direct_imports:
            all_needed |= _transitive_deps(mod)

        for mod in all_needed:
            reverse.setdefault(mod, set()).add(test_file)

    return reverse


# ---------------------------------------------------------------------------
# Convention map — layer 2
# ---------------------------------------------------------------------------


def _load_test_map(repo_root: str) -> dict:
    """Load packaging/test-map.json.  Returns empty dict if absent.

    Tolerates absence of the ``server_import_allowlist`` key — existing maps
    without it keep working; the key defaults to an empty list.
    """
    path = Path(repo_root) / TEST_MAP_PATH
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _get_server_import_allowlist(test_map: dict) -> set[str]:
    """Return the set of basenames explicitly allowed to import 'server'.

    Reads ``server_import_allowlist`` from the test-map dict.  Returns an
    empty set when the key is absent (backwards-compatible with old maps).
    """
    return set(test_map.get("server_import_allowlist", []))


def _is_contract_test(basename: str, allowlist: set[str]) -> bool:
    """Return True if *basename* classifies as a contract test.

    Contract tests are:
      - Files whose basename matches the ``test_*_payload.py`` glob, OR
      - Files explicitly named in the server_import_allowlist.
    """
    return fnmatch.fnmatch(basename, "test_*_payload.py") or basename in allowlist


def _resolve_convention_map(
    changed_file: str,
    test_map: dict,
    repo_root: str,
) -> tuple[set[str], bool]:
    """Look up changed_file in the convention map.

    Returns (selected_test_files, full_suite_requested).
    full_suite_requested=True when the map entry specifies a full-suite fallback.
    """
    mappings = test_map.get("mappings", [])
    selected: set[str] = set()

    for entry in mappings:
        pattern = entry.get("path_prefix_or_glob", "")
        test_globs = entry.get("test_globs", [])
        full_fallback = entry.get("full_suite_fallback", False)

        # Match: prefix match OR fnmatch glob
        matched = changed_file.startswith(pattern) or fnmatch.fnmatch(
            changed_file, pattern
        )
        if not matched:
            continue

        if full_fallback:
            return set(), True

        # Expand test globs relative to repo_root
        for tglob in test_globs:
            for m in glob.glob(os.path.join(repo_root, tglob)):
                rel = str(Path(m).relative_to(repo_root))
                selected.add(rel)

        # First matching entry wins
        break

    return selected, False


def _matches_full_trigger(changed_file: str) -> bool:
    """Return True if changed_file matches an always-full-suite pattern."""
    name = os.path.basename(changed_file)
    for pattern in ALWAYS_FULL_PATTERNS:
        if fnmatch.fnmatch(changed_file, pattern):
            return True
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# Selection pipeline
# ---------------------------------------------------------------------------


def select_tests(
    changed_files: list[str],
    repo_root: str,
    verbose: bool = False,
) -> tuple[list[str], bool, list[str], set[str]]:
    """Two-layer selection pipeline.

    Returns:
      (selected_test_paths, is_full_suite, full_suite_reasons, map_selected)

    selected_test_paths: repo-relative paths to selected test files
    is_full_suite: True → caller should run everything
    full_suite_reasons: human-readable trigger descriptions (when is_full_suite)
    map_selected: subset of selected_test_paths that were reached via layer-2
      convention map.  Used by the banner split to attribute the ``map-residue``
      bucket without re-deriving selection attribution.
    """
    full_suite_reasons: list[str] = []

    # Phase 0: always-full triggers (conftest, pytest.ini, requirements, self, map)
    for cf in changed_files:
        if _matches_full_trigger(cf):
            full_suite_reasons.append(cf)

    if full_suite_reasons:
        return [], True, full_suite_reasons, set()

    if not changed_files:
        return [], False, [], set()

    # Build layer-1 reverse import graph (stateless per run)
    import_graph = _build_import_graph(repo_root)

    # Load layer-2 convention map
    test_map = _load_test_map(repo_root)

    selected: set[str] = set()
    # Track files reached ONLY via layer-2 convention map.  A file that is
    # reached by BOTH layers is attributed to layer-1 (import graph), which
    # is the more specific attribution for banner-split purposes.
    map_only_selected: set[str] = set()
    unresolved: list[str] = []

    for cf in changed_files:
        resolved = False
        l1_hits: set[str] = set()

        # — Layer 1: import graph —
        stem = _path_to_module_stem(cf)
        if stem and stem in import_graph:
            l1_hits = import_graph[stem]
            selected |= l1_hits
            resolved = True
            if verbose:
                print(f"  [L1] {cf} → {len(l1_hits)} test(s) via import graph")
        elif stem:
            # Fallback: for tools sub-package files (tools/engine/build.py → stem
            # "build", absent from graph), check the package root key "tools".
            # import_graph["tools"] holds all tests that import any tools.* module.
            pkg_root = _package_root_stem(cf)
            if pkg_root and pkg_root in import_graph:
                l1_hits = import_graph[pkg_root]
                selected |= l1_hits
                resolved = True
                if verbose:
                    print(
                        f"  [L1-pkg] {cf} → {len(l1_hits)} test(s) via package root "
                        f'"{pkg_root}" key'
                    )

        # — Layer 2: convention map —
        # Applied even when L1 found something (hook file may BOTH be imported
        # AND have explicit map entries for non-imported test surfaces)
        map_tests, map_full = _resolve_convention_map(cf, test_map, repo_root)
        if map_full:
            full_suite_reasons.append(f"convention map: {cf} → full suite")
            return [], True, full_suite_reasons, set()
        if map_tests:
            # Only attribute to map-only when L1 did not already reach the file.
            map_only_selected |= map_tests - l1_hits
            selected |= map_tests
            resolved = True
            if verbose:
                print(
                    f"  [L2] {cf} → {len(map_tests)} test(s) from convention map"
                )

        # — Direct: changed file IS a test file —
        if (
            cf.startswith(f"{TESTS_DIR}/")
            and cf.endswith(".py")
            and os.path.exists(os.path.join(repo_root, cf))
        ):
            selected.add(cf)
            # Attribution: a directly-selected test file is not map-residue,
            # even if a convention-map entry also reaches it — direct/L1 wins
            # (reviewer catch: banner accuracy; selection itself unaffected).
            map_only_selected.discard(cf)
            resolved = True
            if verbose:
                print(f"  [direct] {cf} is a test file → selected directly")

        if not resolved:
            unresolved.append(cf)

    if unresolved:
        full_suite_reasons.extend(
            f"unresolved by both layers: {cf}" for cf in unresolved
        )
        return [], True, full_suite_reasons, set()

    # Filter to only existing files
    existing = [
        tf
        for tf in sorted(selected)
        if os.path.exists(os.path.join(repo_root, tf))
    ]

    # map_only_selected constrained to files that actually exist
    existing_set = set(existing)
    map_selected_existing = map_only_selected & existing_set

    return existing, False, [], map_selected_existing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_total_test_files(repo_root: str) -> int:
    """Count total test_*.py files in tests/."""
    tests_dir = Path(repo_root) / TESTS_DIR
    if not tests_dir.is_dir():
        return 0
    return len(list(tests_dir.glob("test_*.py")))


def _classify_selection(
    selected: list[str],
    map_selected: set[str],
    allowlist: set[str],
) -> tuple[int, int, int]:
    """Split selected test files into banner bucket counts.

    Buckets (mutually exclusive, all selected files accounted for):
      contract    — basename matches test_*_payload.py OR in the allowlist
      map-residue — reached via layer-2 convention map only (and not contract)
      internals   — everything else (reached via layer-1 import graph / direct)

    Returns (n_internals, n_contract, n_map_residue).
    """
    n_contract = 0
    n_map_residue = 0
    n_internals = 0
    for tf in selected:
        base = os.path.basename(tf)
        if _is_contract_test(base, allowlist):
            n_contract += 1
        elif tf in map_selected:
            n_map_residue += 1
        else:
            n_internals += 1
    return n_internals, n_contract, n_map_residue


# ---------------------------------------------------------------------------
# Audit helpers — #948 contract
# ---------------------------------------------------------------------------


def _audit_imports(
    repo_root: str,
    test_map: dict,
) -> list[str]:
    """Return sorted list of test files that violate the server-import rule.

    A file violates when it:
      - is in tests/test_*.py,
      - imports the 'server' module (via _parse_imports / _iter_imports), AND
      - does NOT match the ``test_*_payload.py`` basename glob, AND
      - is NOT named in the ``server_import_allowlist`` key of test-map.json.

    Returns a sorted list of repo-relative paths of violating files.
    """
    allowlist = _get_server_import_allowlist(test_map)
    tests_dir = Path(repo_root) / TESTS_DIR
    if not tests_dir.is_dir():
        return []

    violations: list[str] = []
    for tf in sorted(tests_dir.glob("test_*.py")):
        base = tf.name
        # Skip contract-tier and explicitly allowed files
        if _is_contract_test(base, allowlist):
            continue
        # Check if this file imports server
        imports = _parse_imports(str(tf))
        if "server" in imports:
            rel = str(tf.relative_to(repo_root))
            violations.append(rel)

    return sorted(violations)


def _parse_monkeypatch_string_targets(path: str) -> list[str]:
    """Return list of string-literal first args to monkeypatch.setattr/delattr/setitem.

    Detects AST patterns of the form:
      monkeypatch.setattr("module.attr", ...)
      monkeypatch.delattr("module.attr", ...)
      monkeypatch.setitem("module.attr", ...)

    where the first argument is an ``ast.Constant`` string containing a dot.
    Returns the list of such string values (e.g. ["server.some_func"]).
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            src = f.read()
        tree = ast.parse(src, filename=path)
    except (SyntaxError, OSError):
        return []

    targets: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match: monkeypatch.<setattr|delattr|setitem>(...)
        if not (
            isinstance(func, ast.Attribute)
            and func.attr in ("setattr", "delattr", "setitem")
            and isinstance(func.value, ast.Name)
            and func.value.id == "monkeypatch"
        ):
            continue
        # First positional argument must be a string constant containing a dot
        if not node.args:
            continue
        first_arg = node.args[0]
        if (
            isinstance(first_arg, ast.Constant)
            and isinstance(first_arg.value, str)
            and "." in first_arg.value
        ):
            targets.append(first_arg.value)

    return targets


def _audit_patches(
    repo_root: str,
) -> list[str]:
    """Known false-negative class: a renamed monkeypatch fixture
    (e.g. 'def test_x(mp):' then 'mp.setattr(...)') is not detected --
    the AST match keys on the literal name 'monkeypatch'.

    Return sorted list of test files that violate the string-patch rule.

    A file violates when it uses ``monkeypatch.setattr/delattr/setitem`` with a
    string literal ``"module.attr..."`` whose module stem (text before the first
    dot) is a repo module that is NOT present in the file's own import set.

    ``_discover_repo_modules`` is used to determine the set of repo module stems.
    Returns a sorted list of repo-relative paths of violating files.
    """
    repo_modules = _discover_repo_modules(repo_root)
    tests_dir = Path(repo_root) / TESTS_DIR
    if not tests_dir.is_dir():
        return []

    violations: list[str] = []
    for tf in sorted(tests_dir.glob("test_*.py")):
        string_targets = _parse_monkeypatch_string_targets(str(tf))
        if not string_targets:
            continue
        file_imports = _parse_imports(str(tf))
        for target in string_targets:
            module_stem = target.split(".")[0]
            if module_stem in repo_modules and module_stem not in file_imports:
                rel = str(tf.relative_to(repo_root))
                violations.append(rel)
                break  # one violation per file is enough

    return sorted(violations)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _emit_json_selection(args, repo_root: str) -> int:
    """Emit the touched-test selection as a single JSON object on stdout.

    Machine-readable interface for run_matrix.py / CI (#864 §3): structured
    output instead of scraping the human banner (the parse-the-printed-text
    construction-vacuity surface the green-is-not-ground-truth lesson warns
    against). No pytest run; selection is computed once on the host. Schema:

        {
          "base":             <git ref the diff was taken against>,
          "changed_files":    [<repo-relative paths>],
          "full":             <bool — whole suite selected>,
          "full_reasons":     [<trigger descriptions when full>],
          "selected":         [<test files>; [] when full or none>],
          "total_test_files": <int>,
          "buckets":          {"internals": N, "contract": N, "map_residue": N}
        }

    full=true means the convergence-gate whole suite is selected (selected is []
    then — the consumer runs `pytest tests/`, not the listed subset). Buckets are
    zero when full or when nothing is selected. select_tests runs with
    verbose=False so no banner prose leaks onto stdout and corrupts the JSON.
    """
    empty_buckets = {"internals": 0, "contract": 0, "map_residue": 0}
    total_test_files = _count_total_test_files(repo_root)

    if args.full:
        out = {
            "base": args.base, "changed_files": [], "full": True,
            "full_reasons": ["--full flag"], "selected": [],
            "total_test_files": total_test_files, "buckets": empty_buckets,
        }
        print(json.dumps(out))
        return 0

    changed = _changed_files(args.base, repo_root)
    selected, is_full, reasons, map_selected = select_tests(
        changed, repo_root, verbose=False
    )

    if is_full:
        out = {
            "base": args.base, "changed_files": changed, "full": True,
            "full_reasons": reasons, "selected": [],
            "total_test_files": total_test_files, "buckets": empty_buckets,
        }
        print(json.dumps(out))
        return 0

    if selected:
        test_map = _load_test_map(repo_root)
        allowlist = _get_server_import_allowlist(test_map)
        n_internals, n_contract, n_map_residue = _classify_selection(
            selected, map_selected, allowlist
        )
        buckets = {
            "internals": n_internals,
            "contract": n_contract,
            "map_residue": n_map_residue,
        }
    else:
        buckets = dict(empty_buckets)

    out = {
        "base": args.base, "changed_files": changed, "full": False,
        "full_reasons": [], "selected": selected,
        "total_test_files": total_test_files, "buckets": buckets,
    }
    print(json.dumps(out))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Import-graph test selection for fast PR iteration. "
            "Full suite remains the convergence gate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 tools/run_touched_tests.py --base origin/dev\n"
            "  python3 tools/run_touched_tests.py --dry-run\n"
            "  python3 tools/run_touched_tests.py -- -x -v\n"
            "  python3 tools/run_touched_tests.py --audit-imports\n"
            "  python3 tools/run_touched_tests.py --audit-patches --strict\n"
        ),
    )
    parser.add_argument(
        "--base",
        default="origin/dev",
        metavar="REF",
        help="Base git ref for diff (default: origin/dev)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Skip selection; run full test suite unconditionally",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print selection without running pytest",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        metavar="PATH",
        help="Repository root (default: parent dir of this script)",
    )
    parser.add_argument(
        "--audit-imports",
        action="store_true",
        help=(
            "Scan tests/test_*.py and report files that import 'server' "
            "but are not contract-tier (test_*_payload.py or allowlist). "
            "Exit 0 by default; exit 1 with --strict when violations exist."
        ),
    )
    parser.add_argument(
        "--audit-patches",
        action="store_true",
        help=(
            "Scan tests/test_*.py and report files that use "
            "monkeypatch.setattr/delattr/setitem with a string-literal "
            "\"module.attr\" first arg whose module stem is a repo module not "
            "present in the file's own imports. "
            "Exit 0 by default; exit 1 with --strict when violations exist."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "With --audit-imports or --audit-patches: exit 1 when violations "
            "are found (future CI gate). Has no effect without an audit flag."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit the selection as a single JSON object on stdout and exit 0 "
            "(machine-readable interface for run_matrix.py / CI — no pytest run, "
            "no human banner). Composes with --base; ignores --dry-run/passthrough. "
            "With --full the diff is skipped, so changed_files is emitted as []."
        ),
    )

    # Split argv on '--' to pass remaining args through to pytest
    if argv is None:
        argv = sys.argv[1:]

    passthrough: list[str] = []
    if "--" in argv:
        idx = argv.index("--")
        passthrough = argv[idx + 1 :]
        argv = argv[:idx]

    args = parser.parse_args(argv)

    # Resolve repo root
    if args.repo_root:
        repo_root = os.path.abspath(args.repo_root)
    else:
        # Walk upward from this file's directory until we find the repo root
        # (the directory containing both pytest.ini and tests/).  A fixed
        # parent-count such as .parent.parent.parent was off-by-one after the
        # Phase-1 restructure moved this file from tools/ to src/engram/tools/,
        # and would break again on any future move.  The marker-walk is stable.
        repo_root = str(_find_repo_root(Path(__file__).resolve().parent))

    # --audit-imports: report server-import violations
    if args.audit_imports:
        test_map = _load_test_map(repo_root)
        violations = _audit_imports(repo_root, test_map)
        if violations:
            print(
                f"[audit-imports] {len(violations)} file(s) import 'server' "
                f"but are not contract-tier (test_*_payload.py or allowlist):"
            )
            for v in violations:
                print(f"  {v}")
        else:
            print("[audit-imports] no violations found")
        if args.strict and violations:
            return 1
        return 0

    # --audit-patches: report string-patch-without-import violations
    if args.audit_patches:
        violations = _audit_patches(repo_root)
        if violations:
            print(
                f"[audit-patches] {len(violations)} file(s) use monkeypatch "
                f"string targets for repo modules not in their import set:"
            )
            for v in violations:
                print(f"  {v}")
        else:
            print("[audit-patches] no violations found")
        if args.strict and violations:
            return 1
        return 0

    # --json: machine-readable selection for run_matrix.py / CI (no pytest run,
    # no human banner — structured output, never scraped prose).
    if args.json:
        return _emit_json_selection(args, repo_root)

    total_test_files = _count_total_test_files(repo_root)

    # --full: bypass selection entirely
    if args.full:
        print("[run_touched_tests] --full: running complete test suite")
        if args.dry_run:
            print("[dry-run] would run: pytest tests/")
            return 0
        cmd = [sys.executable, "-m", "pytest", "tests/"] + passthrough
        return subprocess.run(cmd, cwd=repo_root).returncode

    # Collect changed files
    changed = _changed_files(args.base, repo_root)

    if args.dry_run:
        print(f"[run_touched_tests] changed files vs {args.base} ({len(changed)} total):")
        for cf in changed:
            print(f"  {cf}")

    # Run selection pipeline
    selected, is_full, reasons, map_selected = select_tests(
        changed, repo_root, verbose=args.dry_run
    )

    if is_full:
        trigger_desc = "; ".join(reasons)
        print(f"[run_touched_tests] FULL MAIN SUITE triggered by: {trigger_desc}")
        # Honesty note (colleague-review catch): "full" here means tests/ —
        # forum/tests/ deliberately NOT included: those run against the forum
        # venv (bs4 et al. are version-pinned there; the plugin venv produces
        # dozens of env-skew false failures). Until the forum-deploy epic's
        # per-target test runner lands, forum changes need a manual
        # forum-venv pytest run — say so loudly instead of implying coverage.
        print(
            "[run_touched_tests] NOTE: forum/tests/ NOT included — run them "
            "against the forum venv separately (see the forum-deploy epic)."
        )
        if args.dry_run:
            print("[dry-run] would run: pytest tests/")
            return 0
        cmd = [sys.executable, "-m", "pytest", "tests/"] + passthrough
        return subprocess.run(cmd, cwd=repo_root).returncode

    # Load test-map and allowlist for banner classification
    test_map = _load_test_map(repo_root)
    allowlist = _get_server_import_allowlist(test_map)

    if not selected:
        msg = (
            f"SUBSET green (0/{total_test_files} selected for "
            f"{len(changed)} changed files) — iteration signal, NOT a "
            "convergence claim. Full suite gates merge-readiness."
        )
        print("[run_touched_tests] no tests selected (no changed files resolve to tests)")
        print(msg)
        return 0

    n_selected = len(selected)
    n_changed = len(changed)
    subset_msg = (
        f"SUBSET green ({n_selected}/{total_test_files} selected for "
        f"{n_changed} changed files) — iteration signal, NOT a convergence claim. "
        "Full suite gates merge-readiness."
    )
    n_internals, n_contract, n_map_residue = _classify_selection(
        selected, map_selected, allowlist
    )
    bucket_line = (
        f"[selection] {n_selected} selected: "
        f"{n_internals} internals + {n_contract} contract + {n_map_residue} map-residue"
    )

    if args.dry_run:
        print(f"\n[run_touched_tests] would run {n_selected}/{total_test_files} test files:")
        for tf in selected:
            print(f"  {tf}")
        print(f"\n{subset_msg}")
        print(bucket_line)
        return 0

    cmd = [sys.executable, "-m", "pytest"] + selected + passthrough
    rc = subprocess.run(cmd, cwd=repo_root).returncode

    if rc == 0:
        print(f"\n{subset_msg}")
        print(bucket_line)

    return rc


if __name__ == "__main__":
    sys.exit(main())
