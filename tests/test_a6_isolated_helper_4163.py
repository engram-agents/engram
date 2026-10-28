"""#4163 round 6: the A6 fork-path tests run in a FRESH interpreter, as production does.

`_contended_fork_guard` refuses to fork a multi-threaded process, and it is right to.  In
production the tool forks only from `_layout_child`, a process it spawned itself.  The tests used
to call the fork path inside the shared pytest process, so ANY thread an earlier test leaked made
them red in a whole-suite run (and only there).  `_a6_isolated.isolated` re-runs such a test in a
child interpreter; these cells pin the helper's contract.

Cells:
  H1  a stranger thread in the OUTER pytest process does not trip an isolated fork test.
  H2  a failing isolated body fails the outer test, with the child's traceback tail in the message.
  H3  a node id that collects nothing fails, never passes.
  H4  inside the child the body runs exactly once and never spawns again.
  H5  the guard still refuses in-process (the stranger-thread cells are not weakened).
  H6  (round 7) a child that passes TWO tests is not "exactly one pass".
  D1  (round 7) a NON-decorated fork-path test fails on a plain run, naming the test and @isolated.
  D2  (round 7) the same scratch test passes when decorated (the denial is not a blanket failure).
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import _a6_isolated

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
C2 = "tests/test_engramd_a6_contended_processes_3740.py::test_c2_returns_exactly_clients_times_n_floats"
C5 = "tests/test_engramd_a6_contended_processes_3740.py::test_c5_a_multithreaded_parent_is_refused_before_anything_forks"
R1 = "tests/test_engramd_a6_contended_processes_3740.py::test_r1_a_non_exempted_live_thread_still_trips_the_guard_and_is_named"


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k != _a6_isolated.CHILD_ENV}
    env.update(extra)
    return env


def _pytest(args, env):
    return subprocess.run([sys.executable, "-m", "pytest", *args, "-q", "-p", "no:cacheprovider"],
                          cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=600)


def _scratch(tmp_path, body, name="test_scratch_4163.py"):
    """A scratch test module whose one test is `@isolated`; `body` is the test's source."""
    path = tmp_path / name
    head = ("import os, sys\n"
            f"sys.path.insert(0, {str(TESTS_DIR)!r})\n"
            "from _a6_isolated import isolated\n\n\n"
            "@isolated\n"
            "def test_scratch():\n")
    path.write_text(head + textwrap.indent(textwrap.dedent(body), "    "))
    return path


# -- H1 -----------------------------------------------------------------------------------------------

def test_h1_an_isolated_fork_test_passes_with_a_stranger_thread_alive_in_the_outer_process(tmp_path):
    """The outer pytest process carries a parked non-daemon thread (the plugin plants it at session
    start).  C2 forks for real; isolated, its body runs in a fresh child and passes.
    Mutation: make `isolated` run the body in-process -> the guard refuses -> red."""
    proc = _pytest([C2, "-p", "_a6_plant_thread_plugin"],
                   _clean_env(PYTHONPATH=str(TESTS_DIR)))
    assert proc.returncode == 0 and "1 passed" in proc.stdout, proc.stdout[-3000:] + proc.stderr[-1000:]


# -- H2 -----------------------------------------------------------------------------------------------

def test_h2_a_failing_isolated_body_fails_the_outer_test_with_the_childs_traceback(tmp_path):
    """Mutation: swallow the child's output -> the sentinel is absent from the message -> red."""
    path = _scratch(tmp_path, 'assert False, "A6-SENTINEL-BOOM"\n')
    with pytest.raises(pytest.fail.Exception) as exc:
        _a6_isolated.run_in_fresh_interpreter(f"{path}::test_scratch")
    msg = str(exc.value)
    assert "A6-SENTINEL-BOOM" in msg and "AssertionError" in msg, msg
    assert "rc=1" in msg, msg


def test_h2_a_failing_body_through_the_decorator_fails_the_outer_run(tmp_path):
    path = _scratch(tmp_path, 'assert False, "A6-SENTINEL-DECORATED"\n')
    proc = _pytest([str(path)], _clean_env())
    assert proc.returncode != 0
    assert "A6-SENTINEL-DECORATED" in proc.stdout, proc.stdout[-3000:]


# -- H3 -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("nodeid_tail", ["::test_does_not_exist", "::test_scratch[no-such-param]"])
def test_h3_a_node_id_that_collects_nothing_fails_never_passes(tmp_path, nodeid_tail):
    """Mutation: treat rc 4/5 as success -> no raise -> red."""
    path = _scratch(tmp_path, "pass\n")
    with pytest.raises(pytest.fail.Exception) as exc:
        _a6_isolated.run_in_fresh_interpreter(f"{path}{nodeid_tail}")
    assert "collected nothing" in str(exc.value) or "no tests" in str(exc.value).lower(), str(exc.value)


def test_h3_a_child_that_skips_is_not_a_pass(tmp_path):
    path = tmp_path / "test_skip_4163.py"
    path.write_text("import pytest\n\ndef test_scratch():\n    pytest.skip('nope')\n")
    with pytest.raises(pytest.fail.Exception):
        _a6_isolated.run_in_fresh_interpreter(f"{path}::test_scratch")


# -- H4 -----------------------------------------------------------------------------------------------

def test_h4_the_body_runs_exactly_once_and_the_child_never_spawns_again(tmp_path):
    """Outer mode spawns one child; inside it the body runs once and `isolated` is a no-op.
    Mutation: run the body in the outer AND the child -> two lines; or spawn from the child -> a
    third line / recursion -> red."""
    log = tmp_path / "runs.log"
    path = _scratch(tmp_path, f'''\
        with open({str(log)!r}, "a") as f:
            f.write("child=%s ppid=%d\\n" % (os.environ.get("A6_ISOLATED_CHILD"), os.getppid()))
        ''')
    proc = _pytest([str(path)], _clean_env())
    assert proc.returncode == 0 and "1 passed" in proc.stdout, proc.stdout[-3000:]
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith("child=1 "), lines


def test_h4_with_the_marker_set_the_decorator_returns_the_function_unchanged():
    marked = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); from _a6_isolated import isolated\n"
         "def f(): pass\n"
         "print(isolated(f) is f)" % str(TESTS_DIR)],
        env=_clean_env(**{_a6_isolated.CHILD_ENV: "1"}), capture_output=True, text=True, timeout=60)
    assert marked.stdout.strip() == "True", marked.stdout + marked.stderr


# -- H5 -----------------------------------------------------------------------------------------------

def test_h5_the_guard_still_refuses_in_process_with_a_stranger_thread():
    """Not weakened: with a stranger thread alive the guard raises, naming it.  (The C5/R1 cells
    keep asserting this themselves; this one pins it from the helper's side.)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("engramd_a6_latency_h5", REPO_ROOT / "tools" / "engramd_a6_latency.py")
    tool = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = tool
    try:
        spec.loader.exec_module(tool)
        stop = threading.Event()
        t = threading.Thread(target=stop.wait, name="h5-stranger")
        t.start()
        try:
            with pytest.raises(RuntimeError, match=r"h5-stranger"):
                tool._contended_fork_guard()
        finally:
            stop.set()
            t.join()
    finally:
        sys.modules.pop(spec.name, None)


@pytest.mark.parametrize("nodeid", [C5, R1], ids=["c5", "r1"])
def test_h5_the_stranger_thread_refusal_cells_are_not_isolated_away(nodeid):
    """C5 and R1 plant their own thread and assert the refusal; they stay plain tests (run
    in-process, with the pytest process's own threads out of their way), never wrappers around
    a child that could pass without asserting."""
    import re
    src = (REPO_ROOT / nodeid.split("::")[0]).read_text()
    name = nodeid.split("::")[1]
    m = re.search(r"(?m)^((?:@[^\n]*\n)*)def %s\(" % re.escape(name), src)
    assert m, name
    assert "@isolated" not in m.group(1), m.group(1)


# -- H6 (round 7) -------------------------------------------------------------------------------------

def test_h6_a_child_that_passes_two_tests_is_not_exactly_one_pass(tmp_path):
    """The node id resolves to a module holding TWO passing tests, so the child exits 0 with
    "2 passed".  The helper must fail and say the child did not report exactly one pass.
    Mutation: loosen the check in `run_in_fresh_interpreter` to `\\d+ passed` -> no raise -> red."""
    path = tmp_path / "test_two_4163.py"
    path.write_text("def test_one():\n    pass\n\n\ndef test_two():\n    pass\n")
    with pytest.raises(pytest.fail.Exception) as exc:
        _a6_isolated.run_in_fresh_interpreter(str(path))
    msg = str(exc.value)
    assert "did not report exactly one pass" in msg and "2 passed" in msg, msg


# -- D1 / D2 (round 7): default-deny at the fork -----------------------------------------------------

def _fork_scratch(tmp_path, decorated):
    """A scratch module that uses the default-deny fixture; its one test reaches os.fork."""
    path = tmp_path / ("test_fork_deco_4163.py" if decorated else "test_fork_plain_4163.py")
    path.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(TESTS_DIR)!r})\n"
        "from _a6_isolated import isolated, a6_fork_default_deny  # noqa: F401\n\n\n"
        + ("@isolated\n" if decorated else "")
        + "def test_forks_for_real():\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        os._exit(0)\n"
        "    os.waitpid(pid, 0)\n")
    return path


def test_d1_a_non_decorated_fork_path_test_fails_a_plain_run_naming_the_test_and_isolated(tmp_path):
    """Mutation: make the fixture a no-op -> the scratch test really forks and passes -> red."""
    path = _fork_scratch(tmp_path, decorated=False)
    proc = _pytest([str(path)], _clean_env())
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out[-3000:]
    assert "test_forks_for_real" in out and "decorate it @isolated" in out, out[-3000:]
    assert "reaches os.fork in the shared pytest process" in out, out[-3000:]
    assert "1 failed" in out, out[-3000:]


def test_d2_the_same_test_decorated_passes_and_really_forks(tmp_path):
    path = _fork_scratch(tmp_path, decorated=True)
    proc = _pytest([str(path)], _clean_env())
    assert proc.returncode == 0 and "1 passed" in proc.stdout, proc.stdout[-3000:] + proc.stderr[-1000:]
