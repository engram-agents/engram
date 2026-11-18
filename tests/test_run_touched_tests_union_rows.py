"""The test-selection runner takes the UNION of every matching test-map row.

Layer 2 of tools/run_touched_tests.py used to stop at the FIRST matching row of
src/engram/packaging/test-map.json, so a hook with a row of its own never reached
the catch-all row's test (that is how a hook left dev red once). The runner now
returns the union of every matching row, and the map carries one directory row
per narrow-directory enumerator test from the 2026-11-18 census, so a new or
edited file in such a directory selects the tests that scan it.

Cells:
  (a) union: two matching rows both contribute, in every row order.
  (b) full-suite request is unchanged and does not depend on row position.
  (c) table-driven from the census fixture, on the REAL map: a new file and an
      existing file in each census directory select the enumerator that scans it.
  (d) corpus: over the 40 merged PRs of the census, the union differs from the
      old first-match selection only where predicted, and the directory rows add
      exactly what the census says they add.

Stdlib + pytest only; the runner is loaded by file path.
"""

from __future__ import annotations

import collections
import importlib.util
import itertools
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUNNER_PATH = REPO / "src" / "engram" / "tools" / "run_touched_tests.py"
MAP_PATH = REPO / "src" / "engram" / "packaging" / "test-map.json"
NARROW_PATH = REPO / "tests" / "fixtures" / "narrow_enumerators_census_2026-11-18.json"
CORPUS_PATH = REPO / "tests" / "fixtures" / "pr_corpus_2026-11-18.json"

CENSUS_NOTE_PREFIX = "census 2026-11-18:"
BOUNDS = "tests/test_hook_injection_bounds.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("rtt_for_union_rows", str(RUNNER_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rtt = _load_runner()
REAL_MAP = json.loads(MAP_PATH.read_text(encoding="utf-8"))
NARROW = json.loads(NARROW_PATH.read_text(encoding="utf-8"))["tests"]
CORPUS = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["prs"]


def _pairs() -> list[tuple[str, str]]:
    return sorted((t, d) for t, info in NARROW.items() for d in info["dirs"])


def _ls_files(directory: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", directory], cwd=str(REPO), capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    return [f for f in out.stdout.split("\n") if f]


def _common_ext(directory: str) -> str:
    files = _ls_files(directory)
    direct = [f for f in files if "/" not in f[len(directory) + 1 :]]
    # A directory that holds only subdirectories (e.g. the skills tree) has no
    # direct files; fall back to everything tracked beneath it.
    pool = direct or files
    counts = collections.Counter(Path(f).suffix.lstrip(".") for f in pool)
    return counts.most_common(1)[0][0]


def _without_census_rows(tmap: dict) -> dict:
    kept = [r for r in tmap["mappings"] if not r.get("note", "").startswith(CENSUS_NOTE_PREFIX)]
    return {**tmap, "mappings": kept}


def _selection(files: list[str], tmap: dict) -> set[str]:
    out: set[str] = set()
    for f in files:
        sel, _full = rtt._resolve_convention_map(f, tmap, str(REPO))
        out |= sel
    return out


def _old_first_match(changed: str, tmap: dict, root: str) -> tuple[set[str], bool]:
    """Local copy of the old matching loop: stop at the first matching row."""
    import fnmatch
    import glob
    import os

    candidates = rtt._shipped_path_candidates(changed)
    for entry in tmap.get("mappings", []):
        pattern = entry.get("path_prefix_or_glob", "")
        if not any(c.startswith(pattern) or fnmatch.fnmatch(c, pattern) for c in candidates):
            continue
        if entry.get("full_suite_fallback", False):
            return set(), True
        sel = set()
        for tglob in entry.get("test_globs", []):
            for m in glob.glob(os.path.join(root, tglob)):
                sel.add(str(Path(m).relative_to(root)))
        return sel, False
    return set(), False


# ---------------------------------------------------------------------------
# (a) union
# ---------------------------------------------------------------------------


def _synthetic_root(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    for name in ("t1", "t2", "t3"):
        (tmp_path / "tests" / f"{name}.py").write_text("", encoding="utf-8")
    return tmp_path


R1 = {"path_prefix_or_glob": "pkg/", "test_globs": ["tests/t1.py"], "note": "r1"}
R2 = {"path_prefix_or_glob": "pkg/*.py", "test_globs": ["tests/t2.py"], "note": "r2"}
R3 = {"path_prefix_or_glob": "other/", "test_globs": ["tests/t3.py"], "note": "r3"}


@pytest.mark.parametrize("order", list(itertools.permutations([0, 1, 2])), ids=str)
def test_a_union_of_every_matching_row_in_every_row_order(tmp_path, order):
    root = _synthetic_root(tmp_path)
    rows = [(R1, R2, R3)[i] for i in order]
    got = rtt._resolve_convention_map("pkg/a.py", {"mappings": rows}, str(root))
    assert got == ({"tests/t1.py", "tests/t2.py"}, False)


def test_a_real_map_hook_selects_its_own_row_and_the_catch_all():
    sel, full = rtt._resolve_convention_map(
        "src/engram/hooks/claude/engram-stop-hook.py", REAL_MAP, str(REPO)
    )
    assert not full
    own = [
        r
        for r in REAL_MAP["mappings"]
        if r.get("path_prefix_or_glob") == "hooks/claude/engram-stop-hook.py"
    ]
    assert own, "engram-stop-hook.py has no row of its own in the real map"
    assert own[0]["test_globs"], "its own row names no test"
    own_tests = _selection(["src/engram/hooks/claude/engram-stop-hook.py"], {"mappings": own})
    assert own_tests and own_tests <= sel
    assert BOUNDS in sel


# ---------------------------------------------------------------------------
# (b) full-suite request unchanged
# ---------------------------------------------------------------------------

FULL = {"path_prefix_or_glob": "pkg/", "test_globs": [], "full_suite_fallback": True, "note": "f"}


@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_b_a_matching_full_suite_row_wins_wherever_it_sits(tmp_path, position):
    root = _synthetic_root(tmp_path)
    rows = {
        "first": [FULL, R1, R2],
        "middle": [R1, FULL, R2],
        "last": [R1, R2, FULL],
    }[position]
    got = rtt._resolve_convention_map("pkg/a.py", {"mappings": rows}, str(root))
    assert got == (set(), True)


def test_b_select_tests_full_suite_for_conftest_is_unchanged():
    # Recorded on the tree before this change (first-match runner):
    #   select_tests(["tests/conftest.py"], root) == ([], True, ["tests/conftest.py"], set())
    selected, is_full, reasons, _unresolved = rtt.select_tests(["tests/conftest.py"], str(REPO))
    assert is_full is True
    assert selected == []
    assert reasons == ["tests/conftest.py"]


# ---------------------------------------------------------------------------
# (c) table-driven from the census fixture, on the real map
# ---------------------------------------------------------------------------


def _pair_id(pair: tuple[str, str]) -> str:
    return f"{Path(pair[0]).name}@{pair[1]}"


def test_c_fixture_shape():
    assert len(NARROW) == 23
    assert len({d for t in NARROW.values() for d in t["dirs"]}) == 18


@pytest.mark.parametrize("pair", _pairs(), ids=_pair_id)
def test_c_a_new_file_in_the_directory_selects_the_enumerator(pair):
    test, directory = pair
    new_path = f"{directory}/zz_census_new_file.{_common_ext(directory)}"
    sel, full = rtt._resolve_convention_map(new_path, REAL_MAP, str(REPO))
    assert not full
    assert test in sel, (new_path, sorted(sel))


@pytest.mark.parametrize("pair", _pairs(), ids=_pair_id)
def test_c_an_existing_file_in_the_directory_selects_the_enumerator(pair):
    test, directory = pair
    existing = _ls_files(directory)[0]
    sel, full = rtt._resolve_convention_map(existing, REAL_MAP, str(REPO))
    assert not full
    assert test in sel, (existing, sorted(sel))


# ---------------------------------------------------------------------------
# (d) corpus
# ---------------------------------------------------------------------------

# Measured when the census was taken: the only PRs of the corpus whose old
# first-match selection differs from the union, each by exactly the bounds test
# (they change a hook that has a row of its own).
UNION_ADDS_BOUNDS = {4869, 4863}


def test_d1_union_equals_first_match_on_the_corpus_except_the_two_hook_prs():
    base = _without_census_rows(REAL_MAP)
    assert len(base["mappings"]) < len(REAL_MAP["mappings"]) or not any(
        r.get("note", "").startswith(CENSUS_NOTE_PREFIX) for r in REAL_MAP["mappings"]
    )
    differing: dict[int, set[str]] = {}
    for pr in CORPUS:
        new = _selection(pr["files"], base)
        old: set[str] = set()
        for f in pr["files"]:
            old |= _old_first_match(f, base, str(REPO))[0]
        if new != old:
            differing[pr["pr"]] = new - old
            assert old <= new, (pr["pr"], sorted(old - new))
    assert set(differing) == UNION_ADDS_BOUNDS, sorted(differing)
    for pr, added in differing.items():
        assert added == {BOUNDS}, (pr, sorted(added))


def test_d2_directory_rows_add_exactly_what_the_census_says():
    base = _without_census_rows(REAL_MAP)
    touched_any = False
    for pr in CORPUS:
        files = pr["files"]
        before = _selection(files, base)
        after = _selection(files, REAL_MAP)
        expected = {
            test
            for test, info in NARROW.items()
            if any(f == d or f.startswith(d + "/") for d in info["dirs"] for f in files)
        } - before
        touched_any = touched_any or bool(expected)
        assert after - before == expected, (pr["pr"], sorted(after - before), sorted(expected))
    assert touched_any, "no PR of the corpus touches a census directory; the cell would be vacuous"
