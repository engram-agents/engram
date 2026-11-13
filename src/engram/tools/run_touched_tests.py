#!/usr/bin/env python3
"""tools/run_touched_tests.py — import-graph test selection for fast PR iteration.

Three-layer union, deterministic, stateless (#4358):

  Layer 1 — Static import graph:
    AST-parse all tests/*.py and every source module under the import roots
    that tests/conftest.py puts on sys.path (derived by parsing conftest, never
    a hand-written directory list; see `_import_roots`) per run (no stored
    state — a stale selection DB would itself be a silent-green axis). Build
    a reverse map from source module → all test files that transitively import
    it. Changed module → selected test set.

  Layer 2 — Convention map (packaging/test-map.json):
    Non-imported surfaces (hooks, skills, templates, shell scripts) are mapped
    via a checked-in JSON file. Entries map path prefixes / globs to test globs.

  Layer 3 — Name reference (ALWAYS runs; not a fallback):
    Every changed file selects every tests/test_*.py whose text contains the
    changed file's BASENAME (substring, `grep -F` style). It runs even when
    layer 1 or 2 resolved the file: a test that only reads a file's text is
    invisible to both of them. Basename, not path: over-selection is accepted
    (it costs time, not coverage).

  Guard set (GUARD_SET_TESTS):
    The whole-tree guard tests of PR-MERGE-POLICY "Owner merge" item 4 run in
    every non-full selection that has at least one changed file.

  Full-suite triggers (unchanged):
    Changes to conftest.py, pytest.ini, requirements*, test-map.json itself,
    or this script → full suite. A convention-map entry marked
    full_suite_fallback → full suite.

  NO full-suite fallback for an unresolved file: a changed file that no layer
  resolves selects nothing from that file and is printed BY NAME as
  unreferenced; the guard set still runs.

Output honesty (the green-is-not-ground-truth discipline):
  SUBSET green: prints "SUBSET green (N/TOTAL selected for M changed files) —
    iteration signal, NOT a convergence claim. The full suite runs nightly on
    dev and at the release cut."
  Per-layer counts (L1 import graph, L2 map, L3 name reference, guard set) and
  the unreferenced file names are printed with every selection.
  Full suite trigger: names the triggering file(s).

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

# The guard set: whole-tree guard tests that no changed file names, so layers
# 1-3 never pick them (they enumerate or scan shipped files). Source of truth is
# docs/internal/PR-MERGE-POLICY.md, "Owner merge" item 4 — copy from there, do
# not invent members. tests/test_run_touched_tests_4358.py pins that every entry
# exists in the tree, so this list cannot rot silently.
#
# NOT listed on purpose: tools/scan-leaks.py and tools/test_id_set_check.py are
# named by that policy item but are scanners, not tests; they run outside this
# tool (the merge seat runs them by hand / CI runs them).
GUARD_SET_TESTS: tuple[str, ...] = (
    "tests/test_syspath_shadow_guard.py",
    "tests/test_tiers_manifest.py",
    "tests/test_run_touched_tests.py",
    "tests/test_ci_wiring_guard.py",
    "tests/test_engramd_read_scan.py",
    "tests/test_atomic_writer_modes_4013.py",
    "tests/test_truncating_writes_ratchet_4380.py",
    "tests/test_forum_process_spawn_ratchet_3518.py",
    "tests/test_no_db_creation_before_bootstrap_4107.py",
    "tests/test_no_unbounded_upward_walks_3983.py",
    "tests/test_prose_home_sweep_3863.py",
    "tests/test_prose_tail_6c_venv.py",
    "tests/test_raw_home_read_gate_3863.py",
    "tests/test_home_path_gate.py",
    "tests/test_lesson_pattern_audit_shipped_3941.py",
    "tests/test_docs_layout_guard.py",
    "tests/test_metadata_parse_drift_guard_3413.py",
    "tests/test_json_extract_metadata_guard_3485.py",
    "tests/test_no_retired_cascade_store_asserts.py",
    "tests/test_forum_redirect_origin_1459.py",
)


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


def _eval_path_expr(node: ast.AST, env: dict[str, str]) -> str | None:
    """Evaluate a conftest path expression without importing or executing it.

    Supports string constants, names bound earlier, ``__file__`` (seeded in
    *env*), and ``os.path.join/dirname/abspath/realpath/normpath`` calls.
    Returns None for anything else (an unevaluable expression is skipped, never
    guessed).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "os"
        and node.func.value.attr == "path"
    ):
        fn = node.func.attr
        args = [_eval_path_expr(a, env) for a in node.args]
        if any(a is None for a in args) or not args:
            return None
        if fn == "join":
            return os.path.join(*args)  # type: ignore[arg-type]
        if fn == "dirname" and len(args) == 1:
            return os.path.dirname(args[0])  # type: ignore[arg-type]
        if fn in ("abspath", "realpath", "normpath") and len(args) == 1:
            return os.path.normpath(args[0])  # type: ignore[arg-type]
    return None


def _import_roots(repo_root: str) -> list[str]:
    """Return the import roots the tests actually use (#4358).

    Derived by parsing ``tests/conftest.py`` (ast only — no import, no exec):
    every ``sys.path.insert(i, X)`` / ``sys.path.append(X)`` whose argument
    evaluates to an existing directory inside the repo is a root, in the order
    conftest adds them.  If conftest is absent, unparseable or yields nothing,
    the repo root alone is used (the pre-#4358 behaviour for repos with no
    conftest).  If conftest's roots change, discovery follows — there is no
    hand-written directory list here.
    """
    repo = os.path.abspath(repo_root)
    conftest = os.path.join(repo, TESTS_DIR, "conftest.py")
    try:
        with open(conftest, encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read(), filename=conftest)
    except (SyntaxError, OSError):
        return [repo]

    # Resolve simple `NAME = <path expr>` bindings; repeat so use-before-def
    # order inside the walk (BFS) cannot hide a binding.
    env: dict[str, str] = {"__file__": conftest}
    assigns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
    ]
    for _ in range(len(assigns) + 1):
        progressed = False
        for n in assigns:
            name = n.targets[0].id  # type: ignore[union-attr]
            if name in env:
                continue
            val = _eval_path_expr(n.value, env)
            if val is not None:
                env[name] = val
                progressed = True
        if not progressed:
            break

    roots: list[str] = []
    for n in ast.walk(tree):
        if not (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("insert", "append")
            and isinstance(n.func.value, ast.Attribute)
            and n.func.value.attr == "path"
            and isinstance(n.func.value.value, ast.Name)
            and n.func.value.value.id == "sys"
        ):
            continue
        arg = n.args[-1] if n.args else None
        if n.func.attr == "insert" and len(n.args) != 2:
            continue
        val = _eval_path_expr(arg, env) if arg is not None else None
        if val is None:
            continue
        val = os.path.normpath(val)
        inside = val == repo or val.startswith(repo + os.sep)
        if inside and os.path.isdir(val) and val not in roots:
            roots.append(val)
    return roots or [repo]


def _module_dirs(repo_root: str) -> list[str]:
    """Directories whose ``*.py`` files are importable by stem in the tests.

    Each import root, plus its ``tools/`` sub-directory (the ``tools`` package
    is also put on the path by the tests that ``import baton`` directly).
    """
    dirs: list[str] = []
    for root in _import_roots(repo_root):
        for d in (root, os.path.join(root, "tools")):
            if os.path.isdir(d) and d not in dirs:
                dirs.append(d)
    return dirs


def _discover_repo_modules(repo_root: str) -> set[str]:
    """Return the set of Python module stems defined in the repo.

    Scans ``*.py`` in every directory of `_module_dirs` (each import root that
    tests/conftest.py puts on sys.path, plus that root's ``tools/``).  Before
    #4358 this scanned only ``<repo>/*.py`` and ``<repo>/tools/*.py`` — a layout
    from before the sources moved under ``src/`` — which left layer 1 blind to
    every ``src/engram`` module.

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
    for d in _module_dirs(repo_root):
        for p in Path(d).glob("*.py"):
            # "__init__" is never a useful lookup key (no test imports it) and
            # would pollute module_deps with a dead entry.
            if p.stem != "__init__":
                modules.add(p.stem)
        if os.path.basename(d) == "tools" and (Path(d) / "__init__.py").exists():
            modules.add("tools")
    return modules


def _path_to_module_stem(path: str) -> str | None:
    """Convert a repo-relative file path to a Python module stem.

    Returns None if the path is not a .py file.
    """
    if path.endswith(".py"):
        return Path(path).stem
    return None


def _package_root_stem(path: str, roots_rel: tuple[str, ...] = ("",)) -> str | None:
    """Return the top-level package stem for files in a tools sub-package.

    tools/engine/build.py    → "tools"
    tools/migration/foo.py   → "tools"
    tools/ia.py              → None  (direct tools/ file; stem "ia" is the key)
    hooks/claude/foo.py      → None  (not a tools sub-package)

    *roots_rel* are the import roots relative to the repo root ("" = the repo
    root itself; "src/engram" under today's conftest), so
    src/engram/tools/engine/build.py → "tools" as well (#4358).  The default
    keeps the single-root behaviour for callers that pass only a path.

    Used as a fallback L1 lookup when the changed file's own stem is absent from
    import_graph.  import_graph["tools"] accumulates every test that does a
    `from tools.X import ...` — including all four test_engine_* suites.  A
    tools sub-package file change (tools/engine/build.py → stem "build", which
    is absent from import_graph) therefore finds its tests via this fallback.
    """
    if not path.endswith(".py"):
        return None
    for rel in roots_rel:
        rel = rel.strip("/")
        if rel:
            if not path.startswith(rel + "/"):
                continue
            sub = path[len(rel) + 1 :]
        else:
            sub = path
        parts = Path(sub).parts
        # Sub-package files have at least 3 parts: ("tools", "<subdir>", "<file>.py")
        if len(parts) >= 3 and parts[0] == "tools":
            return "tools"
    return None


def _build_import_graph(repo_root: str) -> dict[str, set[str]]:
    """Build the reverse import graph: module_stem → set of test files.

    Parses all tests/test_*.py files and all source modules under the import roots
    (`_module_dirs`).  Follows
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

    # Map: module_stem → directly-imported repo module stems (for transitive walk).
    # Same stem in two directories (e.g. a repo-root tools/ symlink and its
    # src/engram/tools target): union the deps rather than let one overwrite.
    module_deps: dict[str, set[str]] = {}
    for d in _module_dirs(repo_root):
        for p in Path(d).glob("*.py"):
            raw = _parse_imports(str(p))
            module_deps.setdefault(p.stem, set()).update(raw & repo_module_stems)

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


def _shipped_path_candidates(changed_file: str) -> tuple[str, ...]:
    """Return changed_file plus its de-prefixed logical/shipped-path form.

    test-map.json's ``path_prefix_or_glob`` entries are written in the
    manifest's logical (shipped) path convention -- bare ``hooks/claude/...``,
    ``skills/claude/...``, ``agents/...``, ``packaging/...``, ``templates/...``,
    ``tools/...`` -- the same convention tiers.json uses and the one
    TestMappingCompleteness's coverage check compares against directly.
    ``hooks/``, ``skills/``, ``agents/``, ``packaging/`` and ``templates/``
    have NO repo-root existence at all (confirmed: none of these directories
    exist at repo root); the real file for MOST of these lives one level
    deeper under ``src/engram/<same-path>`` -- but not all: ``packaging/`` in
    particular splits across TWO source trees by filename
    (``packaging/release-gates.json`` -> ``src/engram/packaging/...``,
    ``packaging/tiers.json`` -> ``src/build/packaging/...`` -- caught in
    review, #2439). ``tools/`` is its own mixed case: most entries ARE
    repo-root symlinks (``tools/baton.py -> ../src/engram/tools/baton.py``,
    ``tools/build-plugin.sh -> ../src/build/build-plugin.sh``), and the
    symlink's own unchanged presence in git means an edit to the real content
    is reported at the symlink's TARGET path, not the bare shim path itself
    -- ``src/engram/tools/...`` for most, ``src/build/...`` (mapping back to
    ``tools/...``) for the handful of build-tooling entries (``build-plugin.sh``,
    ``engine/*``).

    Rather than hardcode which specific sub-path lives under which source
    tree (a mapping that can grow a new split at any time, as ``packaging/``
    already did once), every ``src/engram/`` OR ``src/build/`` prefixed path
    gets BOTH a stripped-to-bare candidate and (for ``src/build/`` only,
    since ``src/engram/`` never remaps into ``tools/``) a ``tools/``-prepended
    candidate. Whichever candidate matches an actual manifest entry wins; an
    unrelated real entry accidentally colliding with a wrong candidate is not
    a concern in practice, since manifest entries are 1:1 with specific
    filenames.

    Without this normalization, every mapping entry under one of the five
    shim-less prefixes (no import-graph fallback either, for anything that
    isn't a directly-imported .py module -- hooks are invoked as subprocesses,
    never imported; skills/agents/templates are data, not code) and every
    non-import-graph-reachable symlinked tools/* entry (esp. *.sh scripts,
    which layer 1's Python-AST walk cannot see at all) can never match a real
    changed-file path -- silently falling back to the FULL MAIN SUITE on every
    touch (#2439). Matching against both the literal path and its de-prefixed
    form closes this for the whole class in one place, rather than rewriting
    every affected manifest entry (which would itself break
    TestMappingCompleteness's bare-path coverage comparison).
    """
    candidates = [changed_file]
    for prefix in ("src/engram/", "src/build/"):
        if changed_file.startswith(prefix):
            candidates.append(changed_file[len(prefix) :])
    if changed_file.startswith("src/build/"):
        candidates.append("tools/" + changed_file[len("src/build/") :])
    return tuple(candidates)


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
    candidates = _shipped_path_candidates(changed_file)

    for entry in mappings:
        pattern = entry.get("path_prefix_or_glob", "")
        test_globs = entry.get("test_globs", [])
        full_fallback = entry.get("full_suite_fallback", False)

        # Match: prefix match OR fnmatch glob, against the literal changed
        # path or its de-prefixed shipped-path form (see
        # _shipped_path_candidates).
        matched = any(
            c.startswith(pattern) or fnmatch.fnmatch(c, pattern)
            for c in candidates
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


def _name_reference_hits(
    changed_files: list[str], repo_root: str
) -> dict[str, set[str]]:
    """Layer 3: changed file → tests/test_*.py whose text contains its basename.

    Substring match on the BASENAME (``grep -F`` style), deliberately not the
    repo-relative path: tests build paths (no test spells out
    ``engram-nap/SKILL.md``), so a path match under-selects.  Generic basenames
    over-select; that costs time, not coverage.  Each test file is read once.
    """
    tests_dir = Path(repo_root) / TESTS_DIR
    hits: dict[str, set[str]] = {cf: set() for cf in changed_files}
    if not tests_dir.is_dir() or not changed_files:
        return hits
    names = {cf: os.path.basename(cf) for cf in changed_files}
    for tf in sorted(tests_dir.glob("test_*.py")):
        try:
            text = tf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(tf.relative_to(repo_root))
        for cf, name in names.items():
            if name and name in text:
                hits[cf].add(rel)
    return hits


def _empty_detail(detail: dict | None) -> None:
    if detail is not None:
        detail["layers"] = {
            "l1_import_graph": 0, "l2_map": 0, "l3_name_reference": 0, "guard_set": 0,
        }
        detail["unreferenced"] = []


def select_tests(
    changed_files: list[str],
    repo_root: str,
    verbose: bool = False,
    detail: dict | None = None,
) -> tuple[list[str], bool, list[str], set[str]]:
    """Three-layer union selection pipeline (+ the guard set).

    Returns:
      (selected_test_paths, is_full_suite, full_suite_reasons, map_selected)

    selected_test_paths: repo-relative paths to selected test files
    is_full_suite: True → caller should run everything
    full_suite_reasons: human-readable trigger descriptions (when is_full_suite)
    map_selected: subset of selected_test_paths that were reached via layer-2
      convention map.  Used by the banner split to attribute the ``map-residue``
      bucket without re-deriving selection attribution.

    If *detail* is a dict it is filled in place with additive diagnostics (the
    return shape stays the 4-tuple):
      detail["layers"]       {"l1_import_graph", "l2_map", "l3_name_reference",
                              "guard_set"} — per-layer selected-test counts
                              (layers overlap; they are not a partition)
      detail["unreferenced"] changed files no layer resolved, by name

    An unresolved file no longer triggers the full suite (#4358): it selects
    nothing from that file, is reported as unreferenced, and the guard set
    still runs.  An empty change list returns ([], False, [], set()) with no
    guard set — "nothing changed" keeps its pre-#4358 meaning.
    """
    full_suite_reasons: list[str] = []
    _empty_detail(detail)

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
    repo_norm = os.path.abspath(repo_root)
    roots_rel = tuple(
        "" if r == repo_norm else os.path.relpath(r, repo_norm).replace(os.sep, "/")
        for r in _import_roots(repo_root)
    )

    # Load layer-2 convention map
    test_map = _load_test_map(repo_root)

    # Layer 3: name reference — always runs, for every changed file
    name_hits = _name_reference_hits(changed_files, repo_root)

    selected: set[str] = set()
    l1_all: set[str] = set()
    l2_all: set[str] = set()
    l3_all: set[str] = set()
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
            l1_all |= l1_hits
            resolved = True
            if verbose:
                print(f"  [L1] {cf} → {len(l1_hits)} test(s) via import graph")
        elif stem:
            # Fallback: for tools sub-package files (tools/engine/build.py → stem
            # "build", absent from graph), check the package root key "tools".
            # import_graph["tools"] holds all tests that import any tools.* module.
            pkg_root = _package_root_stem(cf, roots_rel)
            if pkg_root and pkg_root in import_graph:
                l1_hits = import_graph[pkg_root]
                selected |= l1_hits
                l1_all |= l1_hits
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
            l2_all |= map_tests
            resolved = True
            if verbose:
                print(
                    f"  [L2] {cf} → {len(map_tests)} test(s) from convention map"
                )

        # — Layer 3: name reference (always runs; union with L1/L2) —
        l3_hits = name_hits.get(cf, set())
        if l3_hits:
            selected |= l3_hits
            l3_all |= l3_hits
            resolved = True
            if verbose:
                print(
                    f"  [L3] {cf} → {len(l3_hits)} test(s) naming "
                    f'"{os.path.basename(cf)}"'
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
            if verbose:
                print(f"  [unreferenced] {cf} → no layer resolves it")

    # Guard set: always runs (skipped quietly when an entry is absent from the
    # tree; tests/test_run_touched_tests_4358.py fails if the constant rots).
    guard = {
        g for g in GUARD_SET_TESTS if os.path.exists(os.path.join(repo_root, g))
    }
    selected |= guard

    # Filter to only existing files
    existing = [
        tf
        for tf in sorted(selected)
        if os.path.exists(os.path.join(repo_root, tf))
    ]

    # map_only_selected constrained to files that actually exist
    existing_set = set(existing)
    map_selected_existing = map_only_selected & existing_set

    if detail is not None:
        detail["layers"] = {
            "l1_import_graph": len(l1_all & existing_set),
            "l2_map": len(l2_all & existing_set),
            "l3_name_reference": len(l3_all & existing_set),
            "guard_set": len(guard),
        }
        detail["unreferenced"] = list(unresolved)

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


_FULL_SUITE_NOTE = (
    "The full suite runs nightly on dev and at the release cut."
)


def _layer_lines(detail: dict) -> list[str]:
    """Human lines for the per-layer counts and the unreferenced files."""
    lay = detail.get("layers") or {}
    lines = [
        "[layers] "
        f"L1 import graph: {lay.get('l1_import_graph', 0)} | "
        f"L2 map: {lay.get('l2_map', 0)} | "
        f"L3 name reference: {lay.get('l3_name_reference', 0)} | "
        f"guard set: {lay.get('guard_set', 0)}"
    ]
    unref = detail.get("unreferenced") or []
    if unref:
        lines.append(
            f"[unreferenced] {len(unref)} changed file(s) no layer resolves "
            "(no test imports, maps, or names them): " + ", ".join(unref)
        )
    return lines


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


_EMPTY_LAYERS = {
    "l1_import_graph": 0, "l2_map": 0, "l3_name_reference": 0, "guard_set": 0,
}


def _empty_extra() -> dict:
    return {"layers": dict(_EMPTY_LAYERS), "unreferenced": []}


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
          "buckets":          {"internals": N, "contract": N, "map_residue": N},
          "layers":           {"l1_import_graph": N, "l2_map": N,
                               "l3_name_reference": N, "guard_set": N},
          "unreferenced":     [<changed files no layer resolved>]
        }

    "layers" and "unreferenced" were added in #4358 (additive; no key removed).
    Layer counts overlap (they are not a partition of "selected").

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
            **_empty_extra(),
        }
        print(json.dumps(out))
        return 0

    changed = _changed_files(args.base, repo_root)
    detail: dict = {}
    selected, is_full, reasons, map_selected = select_tests(
        changed, repo_root, verbose=False, detail=detail
    )
    extra = {
        "layers": detail.get("layers") or dict(_EMPTY_LAYERS),
        "unreferenced": detail.get("unreferenced") or [],
    }

    if is_full:
        out = {
            "base": args.base, "changed_files": changed, "full": True,
            "full_reasons": reasons, "selected": [],
            "total_test_files": total_test_files, "buckets": empty_buckets,
            **extra,
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
        **extra,
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
    detail: dict = {}
    selected, is_full, reasons, map_selected = select_tests(
        changed, repo_root, verbose=args.dry_run, detail=detail
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
            f"convergence claim. {_FULL_SUITE_NOTE}"
        )
        print("[run_touched_tests] no tests selected (no changed files resolve to tests)")
        for line in _layer_lines(detail):
            print(line)
        print(msg)
        return 0

    n_selected = len(selected)
    n_changed = len(changed)
    subset_msg = (
        f"SUBSET green ({n_selected}/{total_test_files} selected for "
        f"{n_changed} changed files) — iteration signal, NOT a convergence claim. "
        f"{_FULL_SUITE_NOTE}"
    )
    n_internals, n_contract, n_map_residue = _classify_selection(
        selected, map_selected, allowlist
    )
    bucket_line = (
        f"[selection] {n_selected} selected: "
        f"{n_internals} internals + {n_contract} contract + {n_map_residue} map-residue"
    )

    layer_lines = _layer_lines(detail)

    if args.dry_run:
        print(f"\n[run_touched_tests] would run {n_selected}/{total_test_files} test files:")
        for tf in selected:
            print(f"  {tf}")
        print(f"\n{subset_msg}")
        print(bucket_line)
        for line in layer_lines:
            print(line)
        return 0

    for line in layer_lines:
        print(line)
    cmd = [sys.executable, "-m", "pytest"] + selected + passthrough
    rc = subprocess.run(cmd, cwd=repo_root).returncode

    if rc == 0:
        print(f"\n{subset_msg}")
        print(bucket_line)

    return rc


if __name__ == "__main__":
    sys.exit(main())
