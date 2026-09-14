"""engram-alpha#2609 regression: pin the actual reported-broken pytest
collection ordering somewhere CI genuinely runs, not just as a hand-typed
reproduction command in a PR body.

Reviewer-fairy finding on PR #2619: none of this repo's real CI invocations
exercise the broken order today (tests.yml/nightly.yml pass a directory to
plain `pytest`, which collects alphabetically -- '.' < '_' puts
test_health_engine.py first; affected-tests.yml's run_touched_tests.py
explicitly iterates `sorted(selected)`). So a future regression of this
exact class (e.g. narrowing test_health_engine.py's
_resync_health_modules_before_reload back down to just health.engine +
health.signals) would ship green through every existing CI path. A
genuinely broken variant was hand-built and confirmed to reproduce #2609's
exact failure under this exact subprocess invocation before this test was
written against the fixed code.

DELIBERATELY A SEPARATE FILE from test_health_engine.py, not a class
inside it: subprocess-invoking `tests/test_health_engine.py` as one of the
target files means pytest re-collects and re-runs whatever test file it is
invoked FROM too, if that file is also named in the argument list. Putting
this test class inside test_health_engine.py itself and then running
`pytest tests/test_health_engine.py` as a subprocess from within one of
its own tests causes that subprocess to re-collect and re-run this same
regression test, which spawns three more subprocesses, each of which does
the same -- an exponential subprocess explosion, capped only by each
level's own 120s subprocess timeout (measured: 3 x 120s = 360s before the
outer test finally failed with TimeoutExpired, not a hang report of *why*).
Keeping this test in its own file, never itself a target of `_run()`,
avoids the self-recursion entirely.

Runs as a real subprocess (not an in-process import-and-call) because the
whole defect is about pytest's own module collection/import ordering
within ONE process -- an in-process call cannot reproduce that.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(*files: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *files, "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )


def test_reported_broken_order_passes():
    """The exact command #2609 reports as failing on unfixed dev."""
    proc = _run(
        "tests/test_health_engine_checkpoint_wiring.py",
        "tests/test_health_engine.py",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Bump this literal if test_health_engine.py's or
    # test_health_engine_checkpoint_wiring.py's own test count changes for
    # reasons unrelated to this ordering regression (an added/removed/
    # skipped test in either file) -- that's the fix, not a sign the
    # ordering regression itself came back.
    # engram-alpha#2720 added 4 new tests to test_health_engine.py's
    # TestWriteSnapshot (explicit umask-independent file/dir permissions):
    # 90 -> 94.
    assert "94 passed" in proc.stdout, proc.stdout


def test_normal_order_still_passes():
    """The order CI actually collects in -- must stay green too; this fix
    must not be a special-case that only helps the broken order."""
    proc = _run(
        "tests/test_health_engine.py",
        "tests/test_health_engine_checkpoint_wiring.py",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Bump this literal if test_health_engine.py's or
    # test_health_engine_checkpoint_wiring.py's own test count changes for
    # reasons unrelated to this ordering regression (an added/removed/
    # skipped test in either file) -- that's the fix, not a sign the
    # ordering regression itself came back.
    # engram-alpha#2720: 90 -> 94 (see identical note above).
    assert "94 passed" in proc.stdout, proc.stdout


def test_sibling_fresh_server_file_ordering_also_passes():
    """tests/test_health_series_hook.py's fresh_server() clears the
    identical health.* sys.modules set (independently confirmed by reading
    its source, not assumed from the PR body) -- the same fix must cover
    this second trigger of the same class, not just the one named in the
    issue."""
    proc = _run(
        "tests/test_health_series_hook.py",
        "tests/test_health_engine.py",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Bump this literal if test_health_engine.py's or
    # test_health_series_hook.py's own test count changes for reasons
    # unrelated to this ordering regression -- see the identical note on
    # test_reported_broken_order_passes above.
    # engram-alpha#2720: 83 -> 87 (see identical note above).
    assert "87 passed" in proc.stdout, proc.stdout
