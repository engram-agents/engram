"""Run an A6 fork-path test in a FRESH interpreter, as production does (#4163 round 6).

`engramd_a6_latency._contended_fork_guard` refuses to fork a multi-threaded process.  In
production the tool forks only from `_layout_child`, a process it spawned itself, so no foreign
thread can be alive.  A test that calls the fork path inside the shared pytest process breaks
that precondition: any thread an EARLIER test leaked turns it red, in a whole-suite run only.

`@isolated` (put it OUTERMOST, above any `parametrize` / `skipif` marks) fixes the precondition:

  * outside the child it replaces the test with a wrapper that asks for no fixtures (so a
    module-scoped measuring fixture is never built in the shared process), re-runs the test's own
    node id in `python -m pytest`, and fails with the child's output tail unless exactly that
    test passed;
  * inside the child (marker env var set) it returns the test unchanged, so the body runs once and
    nothing spawns again.

Default-deny (round 7): the decorator is hand-applied, so `a6_fork_default_deny` backs it.  Import
it into every module that holds fork-path tests (`from _a6_isolated import a6_fork_default_deny`);
it is autouse, so in the shared pytest process `os.fork` raises, naming the running test and
`@isolated`.  A fork-path test added without the decorator therefore fails on its FIRST run, not
only in a whole-suite run.  Inside the child it does nothing.
"""
from __future__ import annotations

import inspect
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

CHILD_ENV = "A6_ISOLATED_CHILD"
REPO_ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_S = 900
_TAIL_CHARS = 6000


def in_child() -> bool:
    return os.environ.get(CHILD_ENV) == "1"


def run_in_fresh_interpreter(nodeid: str, *, timeout: int = TIMEOUT_S) -> None:
    """Run `nodeid` alone in a new pytest process; `pytest.fail` unless it ran and passed.

    Fails (never passes) on: a nonzero rc, rc 4/5 (usage error / nothing collected), and an rc 0
    whose summary does not say the one test passed (e.g. it was skipped).
    """
    env = dict(os.environ)
    env[CHILD_ENV] = "1"
    cmd = [sys.executable, "-m", "pytest", nodeid, "-q", "-p", "no:cacheprovider"]
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        pytest.fail(f"isolated child for {nodeid} timed out after {timeout}s\n{out[-_TAIL_CHARS:]}",
                    pytrace=False)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode in (4, 5):
        pytest.fail(f"isolated child for {nodeid} collected nothing (no tests ran; rc={proc.returncode})\n"
                    f"{out[-_TAIL_CHARS:]}", pytrace=False)
    if proc.returncode != 0:
        pytest.fail(f"isolated child for {nodeid} failed (rc={proc.returncode}); child output tail:\n"
                    f"{out[-_TAIL_CHARS:]}", pytrace=False)
    if not re.search(r"\b1 passed\b", out):
        pytest.fail(f"isolated child for {nodeid} exited 0 but did not report exactly one pass "
                    f"(skipped or deselected?)\n{out[-_TAIL_CHARS:]}", pytrace=False)


def _parametrize_names(fn) -> list[str]:
    names: list[str] = []
    for mark in getattr(fn, "pytestmark", []):
        if mark.name == "parametrize":
            argnames = mark.args[0] if mark.args else mark.kwargs["argnames"]
            if isinstance(argnames, str):
                argnames = [a.strip() for a in argnames.split(",") if a.strip()]
            names.extend(argnames)
    return names


def isolated(fn):
    """See the module docstring.  Must be the outermost decorator on the test."""
    if in_child():
        return fn

    names = _parametrize_names(fn)
    params = [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    params += [inspect.Parameter(n, inspect.Parameter.POSITIONAL_OR_KEYWORD) for n in names]

    def outer(request, **_params):
        path, _, rest = request.node.nodeid.partition("::")
        run_in_fresh_interpreter(f"{request.node.path}::{rest}")

    outer.__signature__ = inspect.Signature(params)
    outer.__name__ = fn.__name__
    outer.__qualname__ = fn.__qualname__
    outer.__module__ = fn.__module__
    outer.__doc__ = fn.__doc__
    outer.__dict__.update(fn.__dict__)  # carries the marks already applied (skipif, ...)
    return outer


@pytest.fixture(autouse=True, scope="module")
def a6_fork_default_deny():
    """Outside the child, make `os.fork` raise for the whole module (#4163 round 7).

    Module-scoped on purpose: a module-scoped measuring fixture built for a non-isolated test
    would otherwise run BEFORE a function-scoped patch.  The message names the running test via
    PYTEST_CURRENT_TEST, which pytest sets for setup, call and teardown alike.
    """
    if in_child():
        yield
        return

    def _denied_fork(*_a, **_k):
        current = os.environ.get("PYTEST_CURRENT_TEST", "<unknown test>").rsplit(" (", 1)[0]
        raise RuntimeError(f"{current} reaches os.fork in the shared pytest process; "
                           "decorate it @isolated (tests/_a6_isolated.py, #4163)")

    mp = pytest.MonkeyPatch()
    mp.setattr(os, "fork", _denied_fork)
    try:
        yield
    finally:
        mp.undo()
