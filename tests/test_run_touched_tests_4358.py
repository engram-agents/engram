"""Contract tests for run_touched_tests selection (#4358).

Written BEFORE the implementation (contract-first). The contract:

  1. Layer 1 follows the import roots tests/conftest.py puts on sys.path
     (derived by parsing conftest, not a hand-written directory list).
  2. The layers are a UNION; layer 3 (basename-in-test-text) ALWAYS runs.
  3. Basename match, over-selection accepted.
  4. No full-suite fallback for an unresolved file: it selects nothing, is
     named as unreferenced, and the guard set runs. Always-full triggers and
     the convention map's own full-suite signal are unchanged.
  5. server.py has no special case.
  6. The guard set (PR-MERGE-POLICY "Owner merge" item 4 test files) always
     runs in a non-full, non-empty-change selection.
  7. Output names per-layer counts and the unreferenced files; the SUBSET
     green line points at the nightly and the release cut.

Synthetic repos (tmp_path) except where a cell says "real tree".
"""

from __future__ import annotations

import ast
import functools
import importlib.util
import json
import textwrap
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "src" / "engram" / "tools" / "run_touched_tests.py"

_spec = importlib.util.spec_from_file_location("run_touched_tests_4358", str(RUNNER_PATH))
_runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runner)

# conftest text of the same shape as the real one (roots: repo root + src/engram)
_CONFTEST_SRC_ENGRAM = textwrap.dedent(
    """\
    import os
    import sys
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    SRC_ENGRAM = os.path.join(REPO_ROOT, "src", "engram")
    if SRC_ENGRAM not in sys.path:
        sys.path.insert(0, SRC_ENGRAM)
    if REPO_ROOT in sys.path:
        sys.path.remove(REPO_ROOT)
    sys.path.insert(0, REPO_ROOT)
    """
)


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _repo(tmp_path: Path, files: dict[str, str]) -> str:
    base = {"pytest.ini": "[pytest]\n", "tests/__init__.py": ""}
    base.update(files)
    _write(tmp_path, base)
    return str(tmp_path)


def _select(changed, repo, guard=(), detail=None):
    with mock.patch.object(_runner, "GUARD_SET_TESTS", tuple(guard)):
        return _runner.select_tests(changed, repo, detail=detail)


# ---------------------------------------------------------------------------
# C1 / C2 / C3 / C4 — synthetic
# ---------------------------------------------------------------------------


def test_c1_changed_doc_named_by_exactly_one_test_selects_it_plus_guard(tmp_path):
    repo = _repo(tmp_path, {
        "tests/test_a.py": 'DOC = "notes.md"\n',
        "tests/test_b.py": "x = 1\n",
        "tests/test_guard.py": "y = 1\n",
    })
    detail: dict = {}
    selected, is_full, _r, _m = _select(
        ["docs/notes.md"], repo, guard=["tests/test_guard.py"], detail=detail
    )
    assert not is_full
    assert selected == ["tests/test_a.py", "tests/test_guard.py"]
    assert detail["unreferenced"] == []


def test_c2_unreferenced_file_selects_only_guard_and_is_named(tmp_path, capsys):
    repo = _repo(tmp_path, {
        "tests/test_a.py": "x = 1\n",
        "tests/test_guard.py": "y = 1\n",
    })
    detail: dict = {}
    selected, is_full, reasons, _m = _select(
        ["data/zzz_nowhere.cfg"], repo, guard=["tests/test_guard.py"], detail=detail
    )
    assert is_full is False and reasons == []
    assert selected == ["tests/test_guard.py"]
    assert detail["unreferenced"] == ["data/zzz_nowhere.cfg"]
    # and the human output names it
    with mock.patch.object(_runner, "GUARD_SET_TESTS", ("tests/test_guard.py",)), \
         mock.patch.object(_runner, "_changed_files", return_value=["data/zzz_nowhere.cfg"]):
        rc = _runner.main(["--dry-run", "--base", "HEAD", "--repo-root", repo])
    assert rc == 0
    out = capsys.readouterr().out
    assert "unreferenced" in out.lower()
    assert "data/zzz_nowhere.cfg" in out


def test_c3_conftest_change_is_still_full(tmp_path):
    repo = _repo(tmp_path, {"tests/test_a.py": "x = 1\n"})
    _s, is_full, reasons, _m = _select(["tests/conftest.py"], repo)
    assert is_full and any("conftest.py" in r for r in reasons)


def test_c4_changed_module_imported_by_test_selects_it(tmp_path):
    repo = _repo(tmp_path, {
        "module_a.py": "A = 1\n",
        "tests/test_b.py": "import module_a\n",
        "tests/test_c.py": "x = 1\n",
    })
    selected, is_full, _r, _m = _select(["module_a.py"], repo)
    assert not is_full
    assert selected == ["tests/test_b.py"]


# ---------------------------------------------------------------------------
# C7 — union: map-resolved file still reaches a text-only test
# ---------------------------------------------------------------------------


def test_c7_map_resolved_file_still_selects_text_only_test(tmp_path):
    test_map = {"mappings": [
        {"path_prefix_or_glob": "tools/install.sh", "test_globs": ["tests/test_map_x.py"]},
    ]}
    repo = _repo(tmp_path, {
        "src/engram/packaging/test-map.json": json.dumps(test_map),
        "tests/test_map_x.py": "x = 1\n",
        "tests/test_text_y.py": 'P = "tools/install.sh"\n',
        "tests/test_other.py": "z = 1\n",
    })
    detail: dict = {}
    selected, is_full, _r, map_sel = _select(["tools/install.sh"], repo, detail=detail)
    assert not is_full
    assert selected == ["tests/test_map_x.py", "tests/test_text_y.py"]
    assert map_sel == {"tests/test_map_x.py"}
    assert detail["unreferenced"] == []


# ---------------------------------------------------------------------------
# C10 — discovery: every root and the tools/ dir under each root
# ---------------------------------------------------------------------------


def _layout_repo(tmp_path: Path) -> str:
    return _repo(tmp_path, {
        "tests/conftest.py": _CONFTEST_SRC_ENGRAM,
        "rootmod.py": "R = 1\n",
        "tools/roottool.py": "T = 1\n",
        "src/engram/pkgmod.py": "import pkgdep\n",
        "src/engram/pkgdep.py": "D = 1\n",
        "src/engram/tools/toolmod.py": "import tooldep\n",
        "src/engram/tools/tooldep.py": "D = 1\n",
        "tests/test_imports_pkg.py": "import pkgmod\n",
        "tests/test_imports_tool.py": "import toolmod\n",
        "tests/test_imports_root.py": "import rootmod\n",
        "tests/test_imports_roottool.py": "import roottool\n",
        "tests/test_none.py": "x = 1\n",
    })


@pytest.mark.parametrize(
    "changed, expected_test",
    [
        ("src/engram/pkgmod.py", "tests/test_imports_pkg.py"),
        ("src/engram/pkgdep.py", "tests/test_imports_pkg.py"),  # transitive
        ("src/engram/tools/toolmod.py", "tests/test_imports_tool.py"),
        ("src/engram/tools/tooldep.py", "tests/test_imports_tool.py"),  # transitive
        ("rootmod.py", "tests/test_imports_root.py"),
        ("tools/roottool.py", "tests/test_imports_roottool.py"),
    ],
)
def test_c10_every_discovery_root_resolves_in_layer1(tmp_path, changed, expected_test):
    repo = _layout_repo(tmp_path)
    detail: dict = {}
    selected, is_full, _r, _m = _select([changed], repo, detail=detail)
    assert not is_full
    assert selected == [expected_test]
    assert detail["unreferenced"] == []
    # attributed to layer 1 (the import graph), not just a text hit
    assert detail["layers"]["l1_import_graph"] >= 1


def test_c10_roots_follow_conftest_not_a_hand_list(tmp_path):
    # conftest puts <repo>/lib on sys.path: discovery must follow it.
    conftest = textwrap.dedent(
        """\
        import os, sys
        REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        LIB = os.path.join(REPO_ROOT, "lib")
        sys.path.insert(0, LIB)
        """
    )
    repo = _repo(tmp_path, {
        "tests/conftest.py": conftest,
        "lib/libmod.py": "L = 1\n",
        "src/engram/ignored.py": "I = 1\n",
        "tests/test_lib.py": "import libmod\n",
        "tests/test_ignored.py": "import ignored\n",
    })
    selected, _f, _r, _m = _select(["lib/libmod.py"], repo)
    assert selected == ["tests/test_lib.py"]
    # src/engram is NOT a root of this conftest, so it is not discovered
    detail: dict = {}
    _select(["src/engram/ignored.py"], repo, detail=detail)
    assert detail["layers"]["l1_import_graph"] == 0


def test_relative_repo_root_resolves_roots(tmp_path, monkeypatch):
    # a relative --repo-root (".") must not break root derivation
    _layout_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    detail: dict = {}
    selected, _f, _r, _m = _select(["src/engram/pkgmod.py"], ".", detail=detail)
    assert selected == ["tests/test_imports_pkg.py"]
    assert detail["layers"]["l1_import_graph"] == 1


def test_import_roots_of_the_real_conftest():
    roots = {Path(p).resolve() for p in _runner._import_roots(str(REPO_ROOT))}
    assert roots == {REPO_ROOT, REPO_ROOT / "src" / "engram"}


# ---------------------------------------------------------------------------
# C11 — the map's own full-suite signal survives the unresolved-file rule
# ---------------------------------------------------------------------------


def test_c11_map_full_signal_still_full(tmp_path):
    test_map = {"mappings": [
        {"path_prefix_or_glob": "special/", "test_globs": [], "full_suite_fallback": True},
    ]}
    repo = _repo(tmp_path, {
        "src/engram/packaging/test-map.json": json.dumps(test_map),
        "tests/test_a.py": "x = 1\n",
    })
    _s, is_full, reasons, _m = _select(["special/thing.dat", "unknown.zz"], repo)
    assert is_full
    assert any("convention map" in r for r in reasons)


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


def test_subset_green_line_points_at_nightly_and_release(tmp_path, capsys):
    repo = _repo(tmp_path, {
        "module_a.py": "A = 1\n",
        "tests/test_b.py": "import module_a\n",
    })
    with mock.patch.object(_runner, "GUARD_SET_TESTS", ()), \
         mock.patch.object(_runner, "_changed_files", return_value=["module_a.py"]):
        _runner.main(["--dry-run", "--base", "HEAD", "--repo-root", repo])
    out = capsys.readouterr().out
    assert "SUBSET green" in out
    assert "merge-readiness" not in out
    assert "nightly" in out and "release" in out
    # per-layer counts are printed
    for tag in ("L1", "L2", "L3", "guard"):
        assert tag in out


def test_json_carries_new_fields_additively(tmp_path, capsys):
    repo = _repo(tmp_path, {
        "tests/test_a.py": 'D = "notes.md"\n',
        "tests/test_guard.py": "y = 1\n",
    })
    with mock.patch.object(_runner, "GUARD_SET_TESTS", ("tests/test_guard.py",)), \
         mock.patch.object(_runner, "_changed_files", return_value=["notes.md", "lost.zz"]):
        _runner.main(["--json", "--base", "HEAD", "--repo-root", repo])
    d = json.loads(capsys.readouterr().out)
    for key in ("base", "buckets", "changed_files", "full", "full_reasons",
                "selected", "total_test_files"):
        assert key in d  # existing keys not removed
    assert d["unreferenced"] == ["lost.zz"]
    assert d["layers"] == {
        "l1_import_graph": 0, "l2_map": 0, "l3_name_reference": 1, "guard_set": 1,
    }


def test_empty_change_list_selects_nothing_and_not_full(tmp_path):
    # Chosen behaviour: an empty change list keeps the existing "no changed
    # files" result; the guard set is not forced in.
    repo = _repo(tmp_path, {"tests/test_guard.py": "y = 1\n"})
    selected, is_full, _r, _m = _select([], repo, guard=["tests/test_guard.py"])
    assert selected == [] and is_full is False


# ---------------------------------------------------------------------------
# Real tree: C5, C6, C8, C9, tools cell
# ---------------------------------------------------------------------------


def _test_files() -> list[Path]:
    return sorted((REPO_ROOT / "tests").glob("test_*.py"))


@functools.lru_cache(maxsize=None)
def _top_imports(path: Path) -> frozenset[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return frozenset()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return frozenset(out)


def _importers_of(stem: str) -> set[str]:
    return {
        f"tests/{t.name}" for t in _test_files() if stem in _top_imports(t)
    }


def _transitive_importers(stem: str) -> set[str]:
    """Independent re-implementation: reverse closure over source modules,
    then every test importing any module in the closure.

    The four source dirs below are deliberately fixed (the oracle must not
    derive roots the way the tool does). If conftest gains a root, only
    `test_import_roots_of_the_real_conftest` notices; update both together."""
    src_dirs = [REPO_ROOT, REPO_ROOT / "tools",
                REPO_ROOT / "src" / "engram", REPO_ROOT / "src" / "engram" / "tools"]
    imports: dict[str, set[str]] = {}
    for d in src_dirs:
        for p in d.glob("*.py"):
            if p.stem == "__init__":
                continue
            imports.setdefault(p.stem, set()).update(_top_imports(p))
    closure = {stem}
    changed = True
    while changed:
        changed = False
        for mod, deps in imports.items():
            if mod not in closure and deps & closure:
                closure.add(mod)
                changed = True
    out: set[str] = set()
    for m in closure:
        out |= _importers_of(m)
    return out


def test_c5_server_selects_superset_of_its_static_importers():
    expected = _importers_of("server")
    assert len(expected) > 20, "independent scan found too few importers; scan is broken"
    selected, is_full, _r, _m = _runner.select_tests(["src/engram/server.py"], str(REPO_ROOT))
    assert not is_full
    missing = expected - set(selected)
    assert not missing, f"{len(missing)} server importers not selected: {sorted(missing)[:5]}"


def test_c6_engram_core_selects_its_transitive_importers():
    expected = _transitive_importers("engram_core")
    assert len(expected) > 20
    selected, is_full, _r, _m = _runner.select_tests(["src/engram/engram_core.py"], str(REPO_ROOT))
    assert not is_full
    missing = expected - set(selected)
    assert not missing, f"{len(missing)} transitive importers not selected: {sorted(missing)[:5]}"


def test_c10_real_tools_module_selects_its_importers():
    # src/engram/tools/baton.py: tests do `import baton` (tools/ on sys.path).
    expected = _importers_of("baton")
    assert expected, "independent scan found no importers of baton"
    selected, is_full, _r, _m = _runner.select_tests(["src/engram/tools/baton.py"], str(REPO_ROOT))
    assert not is_full
    missing = expected - set(selected)
    assert not missing, f"tools-module importers not selected: {sorted(missing)[:5]}"


def test_c8_server_py_is_not_full_suite():
    _s, is_full, reasons, _m = _runner.select_tests(["src/engram/server.py"], str(REPO_ROOT))
    assert is_full is False
    assert reasons == []


def test_c9_every_guard_set_path_exists():
    assert _runner.GUARD_SET_TESTS, "guard set must not be empty"
    assert len(set(_runner.GUARD_SET_TESTS)) == len(_runner.GUARD_SET_TESTS)
    missing = [p for p in _runner.GUARD_SET_TESTS if not (REPO_ROOT / p).is_file()]
    assert not missing, f"guard-set entries missing from the tree: {missing}"
    assert all(p.startswith("tests/test_") and p.endswith(".py")
               for p in _runner.GUARD_SET_TESTS)


def test_guard_set_runs_in_real_tree_selection():
    selected, _f, _r, _m = _runner.select_tests(["docs/compaction-continuity.md"], str(REPO_ROOT))
    assert set(_runner.GUARD_SET_TESTS) <= set(selected)
