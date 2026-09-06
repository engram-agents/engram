"""#2565 -- can-it-fail control for the sys.path-collision guard added to
tests/conftest.py (`_sys_path_collision_guard` / `_assert_tools_package_is_repo_root`).

The guard itself cannot be exercised as a normal in-process unit test: it
runs as an autouse, module-scoped fixture inside THIS repo's own
conftest.py, checking the real `tools` package that this very test session
has already imported correctly. Testing "does it fire on a genuine
collision" requires a SEPARATE pytest invocation, in a scratch directory
shaped like this repo (a `tests/` subdirectory holding the test files and
conftest.py, separate from the scratch repo root holding `tools/`) --
reproducing the actual bug shape end-to-end (#2063 / PR#2564: one test
file's own sys.path.insert(0, ...) silently breaks a LATER, unrelated file
in the same pytest invocation), not just unit-testing the check function.

The scratch/repo-root split matters mechanically, not just cosmetically:
pytest's own "prepend" import mode auto-manages each test file's own
rootpath (here, the scratch `tests/` dir, since it has no __init__.py) by
re-inserting it at sys.path[0] before importing every module. If the
scratch repo root and the test files lived in the SAME directory (an
earlier draft of this fixture did this), pytest's own per-file
reinsertion would keep re-prepending that shared directory ahead of
whatever a "poisoning" module inserted, silently defeating the repro --
caught by this exact scratch layout returning "2 passed" on the first
draft, which meant the fixture was not actually reproducing anything.
Splitting `tests/` (pytest-managed) from the scratch repo root (managed
only by the scratch conftest.py's own explicit reinsertion, mirroring
THIS repo's own tests/conftest.py) makes decoy-vs-repo-root ordering
entirely independent of pytest's own per-file rootpath dance, matching
the real repo's actual mechanics.

Coverage:
  1. Two well-behaved scratch modules (neither touches sys.path) both pass
     -- the guard's normal, quiet path.
  2. A first scratch module that inserts a decoy directory ahead of the
     scratch repo root, poisoning `sys.modules['tools']`, causes the
     SECOND module's guard fixture to fail loudly with a message naming
     the actual collision -- the can-it-fail control, reproducing the
     real bug shape and confirming the guard actually catches it rather
     than passing trivially regardless of input.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

_GUARD_CONFTEST = textwrap.dedent(
    """
    import os
    import sys

    import pytest

    # Mirrors this repo's own tests/conftest.py: REPO_ROOT is the scratch
    # repo root (one level up from this conftest.py, which lives in the
    # scratch tests/ dir), explicitly reinserted at sys.path[0] once at
    # session start -- independent of pytest's own per-file rootpath
    # management, which only ever concerns the scratch tests/ dir itself.
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if REPO_ROOT in sys.path:
        sys.path.remove(REPO_ROOT)
    sys.path.insert(0, REPO_ROOT)

    _EXPECTED_TOOLS_DIR = os.path.join(REPO_ROOT, "tools")


    def _assert_tools_package_is_repo_root():
        # Proactive, filesystem-based check -- does NOT require `tools` to
        # already be imported. If `tools` is already cached, trust that
        # (an import already committed to a resolution; re-deriving from
        # sys.path here could disagree with what Python actually did).
        # Otherwise, scan sys.path in order for the first entry that
        # contains a same-named tools/__init__.py -- that is what the NEXT
        # `import tools` would resolve to, whether or not it has happened
        # yet, which is what lets this fire BEFORE a later file's own
        # `import tools...` line raises a raw, less-attributable error.
        mod = sys.modules.get("tools")
        if mod is not None and getattr(mod, "__file__", None) is not None:
            actual_dir = os.path.dirname(os.path.abspath(mod.__file__))
        else:
            actual_dir = None
            for entry in sys.path:
                candidate = os.path.join(entry, "tools", "__init__.py")
                if os.path.isfile(candidate):
                    actual_dir = os.path.dirname(os.path.abspath(candidate))
                    break
            if actual_dir is None:
                return  # no `tools` package resolvable at all -- not this guard's concern
        if actual_dir != _EXPECTED_TOOLS_DIR:
            raise RuntimeError(
                "sys.path collision (#2565): `import tools` would resolve "
                f"to {actual_dir!r}, not the repo-root package at "
                f"{_EXPECTED_TOOLS_DIR!r}."
            )


    @pytest.fixture(autouse=True, scope="module")
    def _sys_path_collision_guard():
        _assert_tools_package_is_repo_root()
        yield


    def pytest_collectstart(collector):
        # Runs BEFORE a module is imported/collected -- catches a collision
        # left over from an earlier file's own top-level sys.path mistake
        # before THIS file's own `import tools...` line gets a chance to
        # raise a raw, less-attributable ModuleNotFoundError instead.
        #
        # pytest.exit(), never a bare raise: pytest_collectstart is called
        # with no surrounding try/except in _pytest.runner.collect_one_node,
        # so any other exception here becomes an INTERNALERROR crash trace
        # instead of the intended attributable message (confirmed live,
        # not assumed -- see tests/conftest.py's own copy of this hook for
        # the incident that caught it).
        try:
            _assert_tools_package_is_repo_root()
        except RuntimeError as exc:
            pytest.exit(str(exc), returncode=1)
    """
)


def _build_base_layout(root: Path) -> Path:
    """Shared scaffolding both scratch-repo builders below need, mirroring
    this repo's own shape:

        root/tools/__init__.py, root/tools/marker.py   -- the "real" package
        root/decoy/tools/__init__.py                    -- same-named decoy,
                                                             no marker.py
        root/tests/conftest.py                          -- carries the guard

    Returns the created `tests/` dir; each caller writes its own
    test_a.py/test_b.py content into it (deliberately not built here --
    that content is what differs between the import-time and run-phase
    repros, and is each builder's own reason to exist). Factored out per
    reviewer-fairy round 1 on PR #2668: this scaffolding was duplicated
    verbatim between the two builders, a maintenance hazard for any future
    third scratch-repo shape."""
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "__init__.py").write_text("")
    (root / "tools" / "marker.py").write_text("VALUE = 'real'\n")

    (root / "decoy" / "tools").mkdir(parents=True)
    (root / "decoy" / "tools" / "__init__.py").write_text("")
    # deliberately no marker.py here -- importing tools.marker via the
    # decoy fails, exactly like src/engram/tools/ lacking tools/engine.

    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "conftest.py").write_text(_GUARD_CONFTEST)
    return tests_dir


def _build_scratch_repo(root: Path, *, poison_first_module: bool) -> None:
    """test_a.py optionally pollutes sys.path with `root/decoy` ahead of
    everything AT MODULE TOP LEVEL (mimicking a test file that inserts the
    wrong directory, per #2063/#2564's actual shape); test_b.py always
    needs the real `tools.marker` to pass, mirroring test_engine_phase3.py's
    role as the LATER, unrelated victim of an earlier file's mistake."""
    tests_dir = _build_base_layout(root)

    poison_line = (
        "sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname("
        "os.path.abspath(__file__))), 'decoy'))\n"
        if poison_first_module
        else ""
    )
    (tests_dir / "test_a.py").write_text(
        "import os\nimport sys\n" + poison_line + "\n\ndef test_a():\n    assert True\n"
    )
    (tests_dir / "test_b.py").write_text(
        "import tools.marker\n\n\ndef test_b():\n    assert tools.marker.VALUE == 'real'\n"
    )


def _build_scratch_repo_run_phase_poison(root: Path) -> None:
    """Same base layout as _build_scratch_repo, but the poison happens
    INSIDE test_a's own test FUNCTION body -- during test EXECUTION, after
    collection (and every module's own pytest_collectstart call) has
    already finished clean -- rather than at test_a's module top level.
    test_b.py is deliberately bare (`assert True`, no `import tools...` of
    its own) so the guard's PROACTIVE sys.path-scan branch is what fires
    (see _assert_tools_package_is_repo_root's docstring: it does not
    require `tools` to already be imported), matching Borges's own
    colleague-review repro on PR #2665 exactly -- his victim was a bare
    `assert True`, not a tools.marker importer. A victim that itself
    imports tools.marker at module level would import it correctly during
    COLLECTION (before the run-phase poison fires) and get cached, which
    would defeat this repro by construction: the fixture's `already
    cached, trust that` branch would short-circuit before ever reaching
    the sys.path scan the poison is meant to trip."""
    tests_dir = _build_base_layout(root)

    (tests_dir / "test_a.py").write_text(
        "import os\nimport sys\n\n\n"
        "def test_a_poisons_at_run_time():\n"
        "    sys.path.insert(0, os.path.join(os.path.dirname(os.path."
        "dirname(os.path.abspath(__file__))), 'decoy'))\n"
        "    assert True\n"
    )
    (tests_dir / "test_b.py").write_text("def test_b():\n    assert True\n")


def _run_pytest(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-v", str(root / "tests")],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_guard_is_quiet_when_no_collision_occurs(tmp_path: Path):
    """Baseline: two well-behaved modules, no sys.path pollution -- both
    tests pass, and the guard fires (harmlessly) on both."""
    _build_scratch_repo(tmp_path, poison_first_module=False)
    proc = _run_pytest(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed" in proc.stdout, proc.stdout


def test_guard_fires_loudly_on_a_genuine_collision(tmp_path: Path):
    """The can-it-fail control: test_a.py pollutes sys.path (mimicking a
    real test file's own mistaken sys.path.insert), which does NOT itself
    break test_a but leaves the decoy directory ahead of the scratch repo
    root for the rest of the process. test_b.py's guard fixture (running
    right after test_b's own module finishes collecting, per module-scope
    timing) then detects `sys.modules['tools']` resolved to the decoy and
    fails loudly, naming the actual collision -- BEFORE test_b's own test
    function ever runs. Must NOT read as '2 passed'; if it does, this
    fixture is not actually reproducing the collision shape."""
    _build_scratch_repo(tmp_path, poison_first_module=True)
    proc = _run_pytest(tmp_path)
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        f"expected the collision to produce a failure, got returncode 0:\n{combined}"
    )
    assert "2 passed" not in combined, (
        f"the poisoned sys.path did not break anything -- the scratch "
        f"fixture may not actually reproduce the collision shape:\n{combined}"
    )
    # Checked explicitly, not just "the message text appears somewhere":
    # an earlier draft of this hook raised a bare RuntimeError from
    # pytest_collectstart, which pytest has NO try/except around --
    # crashing the whole run with an INTERNALERROR trace that ALSO
    # contains the exception's message text. A naive
    # `"sys.path collision (#2565)" in combined` assertion passes on
    # either outcome, so it would have shipped that crash as "the guard
    # works." This assertion rules that class of false-positive out.
    assert "INTERNALERROR" not in combined, (
        f"the guard crashed pytest's own collection machinery instead of "
        f"producing a clean, attributable failure:\n{combined}"
    )
    assert "sys.path collision (#2565)" in combined, (
        f"expected the guard's own attributable message naming the "
        f"collision, got:\n{combined}"
    )


def test_run_phase_collision_is_caught_by_the_fixture_not_collectstart(tmp_path: Path):
    """Colleague-review finding on PR #2665 (Borges): the fixture's own
    docstring asserts a falsifiable property -- that IT, not
    pytest_collectstart, is what catches a run-phase collision (one a
    test body introduces during its own execution, after collection has
    already finished clean). Borges confirmed this live with an ad-hoc,
    not-checked-in poison before approving, and flagged the gap himself:
    the docstring states the property as fact with nothing in the suite
    that verifies it. This test is that check, checked in.

    Discriminator vs. the import-time control above
    (test_guard_fires_loudly_on_a_genuine_collision): an import-time
    collision is caught by pytest_collectstart's pytest.exit(), which
    aborts the ENTIRE SESSION during collection, before any test body
    ever runs -- that control asserts '2 passed' NOT in the output for
    exactly this reason. A run-phase collision cannot be caught that
    early (collection already finished clean before the poison fires),
    so test_a's own test function genuinely PASSES, and only test_b's
    autouse fixture -- a per-module setup ERROR, not a session-wide
    exit -- catches it. '1 passed, 1 error' (empirically confirmed, not
    assumed) is the positive signature that this exercised the
    fixture's path, not collectstart's."""
    _build_scratch_repo_run_phase_poison(tmp_path)
    proc = _run_pytest(tmp_path)
    combined = proc.stdout + proc.stderr
    assert "INTERNALERROR" not in combined, (
        f"the guard crashed pytest's own machinery instead of producing "
        f"a clean, attributable failure:\n{combined}"
    )
    assert "sys.path collision (#2565)" in combined, (
        f"expected the guard's own attributable message naming the "
        f"collision, got:\n{combined}"
    )
    # Positive control: test_a's own test function must have genuinely
    # run and passed BEFORE its run-phase poison could take effect. If
    # this is missing, the session likely aborted during collection (the
    # import-time / collectstart path, already covered above) instead of
    # reaching the run-phase / fixture path this test targets.
    assert "1 passed" in combined, (
        f"expected test_a's own test function to have genuinely run and "
        f"passed before its run-phase poison took effect -- got:\n{combined}"
    )
    # Negative control matching the discriminator above: pytest.exit()
    # produces a distinct "Interrupted"/session-abort shape, never
    # "1 passed, 1 error". Seeing it here would mean this scratch repo
    # accidentally reproduced the OTHER, already-covered code path
    # instead of the run-phase one this test exists to check.
    assert "Interrupted" not in combined, (
        f"expected a per-module fixture ERROR, not pytest.exit()'s "
        f"whole-session abort -- got what looks like a session-wide "
        f"interrupt instead:\n{combined}"
    )


# ---------------------------------------------------------------------------
# #2664 -- two fixture/function-scoped sites the completeness sweep above
# CANNOT see by construction (it only runs each candidate file STANDALONE,
# which proves nothing about a fixture-scoped insert whose hazard is
# specifically cross-file). Checked in as real regression tests against
# THIS REPO's own files, not a synthetic scratch repo, since the whole
# point is to guard the two actual sites that were found broken.
# ---------------------------------------------------------------------------


def test_surgical_relation_sets_no_longer_collides_with_test_engine_phase3():
    """#2664 site 1. Before the fix, tests/test_surgical_relation_sets.py's
    `_load_surgical()` did `sys.path.insert(0, str(SRC_ENGRAM))` (removed
    entirely -- redundant, since tests/conftest.py already puts SRC_ENGRAM
    on sys.path unconditionally) AND src/engram/tools/surgical.py's own
    module top level did the identical `sys.path.insert(0, str(PROJECT_DIR))`
    (changed to .append() -- this one is the actual root cause: fixing
    only the test file's line does NOT close this reproduction, since
    `_load_surgical()`'s exec_module() call re-runs surgical.py's own
    top-level code fresh every time it's invoked). Measured before this
    fix: 5 passed, 53 errors. After: 58 passed, 0 errors -- this test
    checks in that measurement so a regression at either site fails loud."""
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
            "tests/test_surgical_relation_sets.py", "tests/test_engine_phase3.py",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = proc.stdout + proc.stderr
    assert "sys.path collision (#2565)" not in combined, (
        f"the #2664 fix regressed -- test_engine_phase3.py collided again "
        f"when run alongside test_surgical_relation_sets.py:\n{combined}"
    )
    assert "58 passed" in combined, (
        f"expected all 58 tests (5 + 53) to pass cleanly, got:\n{combined}"
    )


def test_principle_triggers_registry_no_longer_collides_with_test_engine_phase3():
    """#2664 site 2. Before the fix,
    tests/test_principle_triggers_registry.py's `_fresh_core_with_data_dir`
    (called fresh per-test at 3 call sites) did `sys.path.insert(0,
    src_engram)` with no restore, permanently reordering src_engram ahead
    of the repo root for the rest of the session. Fixed via snapshot/
    restore (this site's own position-0 need, documented in its docstring,
    is real -- unlike site 1's -- so the fix restores sys.path afterward
    rather than removing the insert or switching to append).

    Selects ONLY the 3 tests that call the helper, not the whole file:
    running the full file masks this collision by accident (an earlier,
    unrelated test in the same file happens to `import server`, caching
    the CORRECT `tools` package before these 3 tests get a chance to
    pollute sys.path -- the #2565 guard's own 'already cached, trust
    that' branch then protects every later file regardless). That
    masking is why the full-file combination stays green before AND
    after this fix (checked, not assumed) and is not itself evidence of
    anything -- the narrower selection below is. Measured before this
    fix: 3 passed, 53 errors. After: 56 passed, 0 errors."""
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
            "tests/test_principle_triggers_registry.py::test_concurrent_hook_and_reset_writes_lose_no_update",
            "tests/test_principle_triggers_registry.py::test_with_state_lock_degrades_when_lockfile_open_fails",
            "tests/test_principle_triggers_registry.py::test_with_state_lock_releases_lock_when_closure_raises",
            "tests/test_engine_phase3.py",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = proc.stdout + proc.stderr
    assert "sys.path collision (#2565)" not in combined, (
        f"the #2664 fix regressed -- test_engine_phase3.py collided again "
        f"when run alongside these 3 test_principle_triggers_registry.py "
        f"tests:\n{combined}"
    )
    assert "56 passed" in combined, (
        f"expected all 56 tests (3 + 53) to pass cleanly, got:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Completeness sweep -- reviewer-fairy round-1 catch on PR #2661: a clean
# FULL-suite collection (`pytest tests/ --co`) only proves the DEFAULT,
# complete, alphabetically-ordered run is safe (some earlier file's own
# correct `import tools`-family usage caches the right package first for
# everyone). It says nothing about running a single file or small subset
# STANDALONE -- exactly this repo's own documented dev-PR workflow ("run
# affected-tests locally") and exactly what several `pr-checks.yml` steps
# already do. Three sibling files with the identical `insert(0, SRC_ENGRAM)`
# bug shape (test_cornerstone_anchor.py, test_in_turn_recall.py,
# test_lesson_tripwire.py) passed the full-suite --co check cleanly while
# regressing to errors (or, for the unittest.TestCase-based
# test_lesson_tripwire.py, a full session abort via pytest.exit -- a class
# collector re-triggers pytest_collectstart, unlike a flat function-only
# module) when run standalone or in a small subset. This sweep runs EVERY
# test file matching the same `sys.path.insert(0, ...src...engram...)`
# shape in ISOLATION, so a future sibling instance is caught here rather
# than slipping past a full-suite-only check the way these three did.
# ---------------------------------------------------------------------------

_INSERT0_SRC_ENGRAM_RE = re.compile(
    r"sys\.path\.insert\(\s*0\s*,[^)]*"
    r"(?:\bSRC_ENGRAM\b|\bsrc_engram\b|\bsrc\b[^)]*\bengram\b)"
    r"[^)]*\)",
    re.IGNORECASE | re.DOTALL,
)


def _files_with_insert0_src_engram_pattern() -> list[Path]:
    """Every tests/*.py file whose source text contains an `insert(0, ...)`
    call mentioning `SRC_ENGRAM` (this repo's established variable-name
    convention for the computed .../src/engram path -- most sites hold
    it, then pass just the variable at the call site) OR the literal `src`
    and `engram` string components directly -- the same shape as all 15
    sites this issue's PRs have fixed to date (12 in the original PR, 3
    more from this fairy round).

    Bound, stated because it is a real limit and this sweep must not be
    read as more than it is: a regex over raw source text with two known
    shapes, not an AST walk with alias resolution like
    tests/test_syspath_shadow_guard.py's own (much more thorough) scanner.
    A first draft of this sweep checked only the literal-string shape and
    passed even after DELIBERATELY REVERTING one of the 3 fairy-found
    fixes back to its buggy form -- caught by that exact mutation test,
    not by inspection: the reverted line reads
    `sys.path.insert(0, str(SRC_ENGRAM))`, and `\bsrc\b` does not match
    inside `SRC_ENGRAM` (`_` is a word character in regex `\b` semantics,
    so there is no boundary between `SRC` and `_ENGRAM`). A THIRD naming
    convention this sweep still cannot see (e.g. a locally-renamed
    `_MY_OWN_SRC_ENGRAM_DIR` variable) would recur the same way -- this
    sweep narrows the blind spot to two known shapes, it does not close
    it, mirroring the same caveat test_syspath_shadow_guard.py's own
    docstring states about its own, more thorough bound.

    A SECOND, independent bound (found by PR #2661's own round-2 reviewer-
    fairy, via its own fresh sibling search rather than this sweep, still
    live even after both known instances were fixed under #2664): this
    sweep's test runs each candidate file STANDALONE, which can only
    prove a module-top-level insert is safe in isolation. It cannot see a
    FIXTURE- or FUNCTION-SCOPED insert whose hazard is specifically
    CROSS-FILE -- the file passes fine alone and only pollutes a
    DIFFERENT, later-run file in the same process. See
    test_surgical_relation_sets_no_longer_collides_with_test_engine_phase3
    and test_principle_triggers_registry_no_longer_collides_with_test_engine_phase3
    below for the two instances #2664 found and fixed this way -- both
    checked in as their own dedicated regression tests rather than folded
    into this sweep, because that is the whole point of this bound: this
    sweep's standalone-execution model cannot express them. The runtime
    guard's own safety property held throughout regardless -- it caught
    both collisions loudly and attributably the moment they fired; the
    gap was only ever in what THIS SWEEP's coverage claim could see."""
    tests_dir = Path(__file__).resolve().parent
    matches = []
    for path in sorted(tests_dir.glob("*.py")):
        if path.name in (Path(__file__).name, "conftest.py"):
            continue  # this file's own docstring/patterns, and the guard itself
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _INSERT0_SRC_ENGRAM_RE.search(text):
            matches.append(path)
    return matches


def test_no_sibling_file_trips_the_guard_when_run_standalone():
    """Run every candidate file (see _files_with_insert0_src_engram_pattern)
    standalone and confirm none of them produce the guard's own "sys.path
    collision (#2565)" message or an INTERNALERROR -- the completeness
    check a full-suite-only --co run cannot provide (see module docstring
    above this test for why)."""
    candidates = _files_with_insert0_src_engram_pattern()
    assert candidates, (
        "expected at least one file matching the insert(0, src, engram) "
        "pattern (this test file's own scratch conftest templates are "
        "excluded, but real tests/*.py sources should still match) -- if "
        "this is genuinely zero, the regex itself may have drifted from "
        "the actual pattern in use"
    )
    failures = []
    for path in candidates:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(path)],
            cwd=str(path.parent.parent),
            capture_output=True,
            text=True,
            timeout=120,
        )
        combined = proc.stdout + proc.stderr
        if "sys.path collision (#2565)" in combined or "INTERNALERROR" in combined:
            failures.append((path.name, combined[-2000:]))
    assert not failures, "sibling files still trip the #2565 guard when run standalone:\n\n" + "\n\n".join(
        f"=== {name} ===\n{tail}" for name, tail in failures
    )
