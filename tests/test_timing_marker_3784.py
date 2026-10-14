"""Contract for the `timing` marker and the tracer guard (#3784 (a), Borges's ruling).

The nightly's coverage run (`--cov=src --cov-branch`) made the live stage-accounting
cell read ~0.118 unattributed against the 10% bound (0.072-0.076 without coverage):
the cell measured the tracer. The ruling:

(a) the cell skips LOUDLY, with the named reason, when a tracer is active;
(b) with no tracer the guard does not skip;
(c) the live cell carries `pytest.mark.timing`, and the marker is registered;
(d) nightly.yml: the coverage step deselects `timing`; a separate, distinct,
    no-coverage step selects it and still runs after a red coverage step;
(e) every file carrying the marker references the tracer guard;
(f) the ci-nightly notify steps read BOTH the suite and the timing outcome.

The guard is exercised as a unit (a fake tracer via `sys.settrace`), so these cells run
on every platform, with no owner process.
"""

from __future__ import annotations

import configparser
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

import test_engramd_stage_profile_3678 as sp
from test_nightly_workflow_3539 import _joined_command

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
WORKFLOW = REPO / ".github" / "workflows" / "nightly.yml"
REASON = "timing cell under an active tracer; judged in the no-coverage timing step"
LIVE_CELL = "test_stages_account_for_the_end_to_end_call"


def _load_root_conftest():
    """The guard lives in the repo-root conftest.py (one hook for every test root).
    Loaded by path under a private name: `import conftest` is ambiguous with
    tests/conftest.py."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_root_conftest_3784", REPO / "conftest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tg = _load_root_conftest()


def _noop_tracer(frame, event, arg):  # pragma: no cover - never invoked as a trace
    return None


@pytest.fixture
def fake_tracer():
    prior = sys.gettrace()
    sys.settrace(_noop_tracer)
    try:
        yield
    finally:
        sys.settrace(prior)


@pytest.fixture
def no_tracer(monkeypatch):
    """Steer the guard to 'no tracer' even when this suite itself runs under coverage."""
    monkeypatch.setattr(tg, "_TRACER_PROBES", (lambda: False,))


# --- (a) a tracer makes the guard skip, with the named reason ---------------


def test_the_skip_reason_is_the_named_text():
    assert tg.TRACER_SKIP_REASON == REASON


def test_a_settrace_tracer_is_detected(fake_tracer):
    assert tg._tracer_active() is True


def test_the_guard_skips_with_the_named_reason_under_a_tracer(fake_tracer):
    with pytest.raises(pytest.skip.Exception) as ei:
        tg._skip_under_tracer()
    assert ei.value.msg == REASON


def test_the_sys_monitoring_coverage_tool_is_detected(monkeypatch):
    class FakeMonitoring:
        COVERAGE_ID = 7

        @staticmethod
        def get_tool(tool_id):
            return "coverage.py" if tool_id == 7 else None

    monkeypatch.setattr(sys, "monitoring", FakeMonitoring, raising=False)
    assert tg._probe_monitoring() is True


def test_an_unclaimed_monitoring_coverage_tool_is_not_a_tracer(monkeypatch):
    class FakeMonitoring:
        COVERAGE_ID = 7

        @staticmethod
        def get_tool(tool_id):
            return None

    monkeypatch.setattr(sys, "monitoring", FakeMonitoring, raising=False)
    assert tg._probe_monitoring() is False


def test_an_active_coverage_object_is_detected(monkeypatch):
    import types

    class FakeCoverage:
        @staticmethod
        def current():
            return object()

    fake = types.ModuleType("coverage")
    fake.Coverage = FakeCoverage
    monkeypatch.setitem(sys.modules, "coverage", fake)
    assert tg._probe_coverage() is True


def test_coverage_not_importable_is_not_a_tracer(monkeypatch):
    monkeypatch.setitem(sys.modules, "coverage", None)  # import raises ImportError
    assert tg._probe_coverage() is False


def test_a_probe_that_raises_is_not_a_tracer(monkeypatch):
    def boom():
        raise RuntimeError("probe broke")

    monkeypatch.setattr(tg, "_TRACER_PROBES", (boom,))
    assert tg._tracer_active() is False


def test_one_raising_probe_does_not_hide_a_later_positive_one(monkeypatch):
    def boom():
        raise RuntimeError("probe broke")

    monkeypatch.setattr(tg, "_TRACER_PROBES", (boom, lambda: True))
    assert tg._tracer_active() is True


# --- (b) no tracer: the guard returns without skipping ----------------------


def test_the_guard_does_not_skip_without_a_tracer(no_tracer):
    assert tg._tracer_active() is False
    assert tg._skip_under_tracer() is None


# --- (c) the live cell carries the marker; the marker is registered ---------


def test_the_live_cell_carries_the_timing_marker():
    marks = [m.name for m in getattr(getattr(sp, LIVE_CELL), "pytestmark", [])]
    assert "timing" in marks, marks


def test_timing_is_registered_in_pytest_ini():
    cp = configparser.ConfigParser()
    cp.read(REPO / "pytest.ini", encoding="utf-8")
    lines = [ln.strip() for ln in cp["pytest"]["markers"].splitlines() if ln.strip()]
    assert any(ln.split(":", 1)[0].strip() == "timing" for ln in lines), lines


def test_collection_selects_the_live_cell_by_the_marker_under_strict_markers():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--strict-markers",
         "-p", "no:cacheprovider", "-m", "timing", str(TESTS / "test_engramd_stage_profile_3678.py")],
        cwd=REPO, capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert LIVE_CELL in r.stdout, r.stdout


# --- (d) the workflow: deselect in the coverage step, a distinct select step -


@pytest.fixture(scope="module")
def steps():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["pytest"]["steps"]


def _by_id(steps, sid):
    hits = [s for s in steps if s.get("id") == sid]
    assert len(hits) == 1, f"expected exactly one step with id {sid!r}, got {len(hits)}"
    return hits[0]


def _pytest_argv_part(step):
    cmd = _joined_command(step["run"])
    part = re.split(r"\||2>&1", cmd, maxsplit=1)[0]
    assert "pytest " in part, part
    return part


def test_the_coverage_step_deselects_timing_and_keeps_coverage(steps):
    part = _pytest_argv_part(_by_id(steps, "suite"))
    assert '-m "not timing"' in part, part
    assert "--cov=src --cov-branch" in part, part


def test_a_separate_no_coverage_step_selects_timing(steps):
    suite, timing = _by_id(steps, "suite"), _by_id(steps, "timing")
    assert suite is not timing and suite["name"] != timing["name"]
    part = _pytest_argv_part(timing)
    assert "-m timing" in part, part
    assert "--cov" not in part, part
    assert "tests/ src/forum/tests/" in part, part
    assert "-rs" in part.split(), part
    assert "-o tmp_path_retention_policy=failed" in part, part


def test_the_timing_step_keeps_the_pipefail_and_tee_convention(steps):
    run = "\n".join(ln for ln in steps_run_lines(_by_id(steps, "timing")))
    assert "set -o pipefail" in run
    assert "tee " in run
    assert _by_id(steps, "timing").get("shell") == "bash"


def steps_run_lines(step):
    return [ln for ln in step["run"].splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_the_timing_step_runs_after_a_red_coverage_step(steps):
    ids = [s.get("id") for s in steps]
    assert ids.index("suite") < ids.index("timing")
    assert "!cancelled()" in str(_by_id(steps, "timing").get("if"))


def test_the_timing_step_sits_before_the_notify_steps(steps):
    names = [s.get("name") for s in steps]
    timing_at = [s.get("id") for s in steps].index("timing")
    for n in (
        "on failure — open or update the ci-nightly issue",
        "on success — close any open ci-nightly issue",
    ):
        assert timing_at < names.index(n)


# --- (f) the notify steps, evaluated over the truth table -------------------

FAIL_NAME = "on failure \u2014 open or update the ci-nightly issue"
OK_NAME = "on success \u2014 close any open ci-nightly issue"
OUTCOMES = ("success", "failure", "skipped", "cancelled")


def _eval_if(expr, cancelled, **outcomes):
    """Evaluate the `${{ ... }}` subset the notify steps use: `!`, `&&`, `||`,
    parentheses, cancelled(), steps.X.outcome ==/!= 'v'. Anything else raises."""
    m = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", str(expr), re.S)
    assert m, expr
    py = m.group(1)
    py = py.replace("cancelled()", "CANCELLED")
    py = re.sub(r"steps\.(\w+)\.outcome", r"OUT['\1']", py)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    leftover = re.sub(r"CANCELLED|OUT\['\w+'\]|'[a-z]+'|==|!=|\band\b|\bor\b|\bnot\b|[()\s]", "", py)
    assert leftover == "", (expr, leftover)
    return bool(eval(py, {"__builtins__": {}}, {"CANCELLED": cancelled, "OUT": outcomes}))  # noqa: S307


def _notify_if(steps, name):
    return [s for s in steps if s.get("name") == name][0]["if"]


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("suite", OUTCOMES)
@pytest.mark.parametrize("timing", OUTCOMES)
def test_the_notify_steps_fire_on_exactly_the_specified_truth_table(steps, suite, timing, cancelled):
    outs = {"suite": suite, "timing": timing}
    want_fail = (not cancelled) and (suite != "success" or timing != "success")
    want_ok = (not cancelled) and suite == "success" and timing == "success"
    assert _eval_if(_notify_if(steps, FAIL_NAME), cancelled, **outs) is want_fail
    assert _eval_if(_notify_if(steps, OK_NAME), cancelled, **outs) is want_ok


def test_the_evaluator_itself_discriminates():
    # Positive controls: the evaluator is not returning a constant.
    assert _eval_if("${{ !cancelled() && steps.a.outcome == 'success' }}", False, a="success") is True
    assert _eval_if("${{ !cancelled() && steps.a.outcome == 'success' }}", False, a="failure") is False
    assert _eval_if("${{ !cancelled() && steps.a.outcome == 'success' }}", True, a="success") is False
    assert _eval_if("${{ steps.a.outcome != 'x' || steps.b.outcome == 'y' }}", False, a="x", b="y") is True
    with pytest.raises(AssertionError):
        _eval_if("${{ __import__('os') }}", False)


# --- (e) class sweep: one hook, keyed on the marker, covers every timing cell -


def _marked_files():
    """Files marking a cell timing, in every form: a decorator, an indented
    decorator, `pytestmark = ...`, and `pytest.param(..., marks=...)`; across
    BOTH test roots."""
    out = []
    for root in (TESTS, REPO / "src" / "forum" / "tests"):
        for p in sorted(root.rglob("test_*.py")):
            if p.resolve() == Path(__file__).resolve():
                continue
            text = p.read_text(encoding="utf-8")
            if re.search(r"^[ \t]*(@pytest\.mark\.timing\b|pytestmark\b.*pytest\.mark\.timing\b)", text, re.M) \
                    or re.search(r"marks\s*=.*pytest\.mark\.timing\b", text):
                out.append(p)
    return out


def test_the_sweep_finds_the_stage_profile_file():
    assert any(p.name == "test_engramd_stage_profile_3678.py" for p in _marked_files())


def test_the_sweep_finds_every_marking_form(tmp_path, monkeypatch):
    root = tmp_path / "tests"
    root.mkdir()
    (root / "test_a.py").write_text("@pytest.mark.timing\ndef test_x(): pass\n")
    (root / "test_b.py").write_text("pytestmark = pytest.mark.timing\n")
    (root / "test_c.py").write_text("class T:\n    @pytest.mark.timing\n    def test_y(self): pass\n")
    (root / "test_d.py").write_text("P = pytest.param(1, marks=pytest.mark.timing)\n")
    (root / "test_e.py").write_text("# pytest.mark.timing only in prose\n")
    monkeypatch.setattr(sys.modules[__name__], "TESTS", root)
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    assert sorted(p.name for p in _marked_files()) == ["test_a.py", "test_b.py", "test_c.py", "test_d.py"]


def test_the_guard_is_one_setup_hook_keyed_on_the_timing_marker():
    hook = getattr(tg, "pytest_runtest_setup", None)
    assert callable(hook)
    import inspect

    src = inspect.getsource(hook)
    assert 'get_closest_marker("timing")' in src and "_skip_under_tracer()" in src, src


# --- the stage-rise attribution cells assert a measured latency bound -------
# (a rise < OTHER_STAGE_RISE_MAX_MS in every other stage). Measured on a Mac under
# --cov=src --cov-branch: 28 of them fail (e.g. client_import rose 0.53 ms), and all
# pass without coverage. They are timing cells: the marker alone guards them.


@pytest.mark.parametrize("name", [
    "test_an_injected_delay_lands_in_its_own_stage_and_no_other",
    "test_a_delay_in_the_real_function_is_reported_under_its_stage",
])
def test_the_stage_rise_attribution_cells_are_timing_cells(name):
    fn = getattr(sp, name)
    assert "timing" in [m.name for m in fn.pytestmark], name


# --- the retry spacing (#3787) is gone (Borges, #3784 ruling point 5) --------
# The 10 s spacing had no measured reason once the failure was shown to be a tracer
# confound. The best-of retry and the pairs line stay; the retry draws back-to-back.


def test_the_retry_spacing_constant_is_gone():
    assert not hasattr(sp, "RETRY_SPACING_S")


@pytest.mark.parametrize("fn", ["_best_of", "_live_accounting"])
def test_the_retry_helpers_take_no_pause_parameter(fn):
    import inspect

    assert "pause" not in inspect.signature(getattr(sp, fn)).parameters


def test_the_retry_draws_back_to_back_without_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    draws = iter([sp._synthetic(0.15, 1.0), sp._synthetic(0.07, 1.0)])
    profiles = sp._best_of(sp._synthetic(0.15, 1.0), lambda: next(draws))
    assert sp._stages_account(profiles)[0] and len(profiles) == 3
    assert slept == []


def test_the_live_retry_draws_back_to_back_without_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    class Tool:
        def profile(self, **kwargs):
            return sp._synthetic(0.07, 1.0)

    profiles = sp._live_accounting(Tool(), object(), sp._synthetic(0.15, 1.0))
    assert len(profiles) == 2
    assert slept == []


# --- end to end: the guard fires at SETUP, before any module-scoped fixture ----
# A stand-in test file carries the same marker; its module-scoped fixture records a
# side effect. The root conftest's hook is loaded into the stand-in's own conftest.

_STAND_IN = """
import pathlib
import pytest

SIDE = pathlib.Path(__file__).with_name("side_effect.txt")


@pytest.fixture(scope="module")
def expensive():
    SIDE.write_text("ran")
    return 1


@pytest.mark.timing
def test_a_timing_cell(expensive):
    assert expensive == 1


def test_an_unmarked_cell(expensive):
    assert expensive == 1
"""

_STAND_IN_CONFTEST = """
import importlib.util
spec = importlib.util.spec_from_file_location("_real_root_conftest", {path!r})
_m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_m)
pytest_runtest_setup = _m.pytest_runtest_setup
"""


def _clean_env():
    import os

    return {
        k: v for k, v in os.environ.items()
        if not k.startswith(("COV_CORE", "COVERAGE", "PYTEST_ADDOPTS"))
    }


def _run_stand_in(tmp_path, only_marked, prelude="", extra=()):
    (tmp_path / "test_stand_in.py").write_text(_STAND_IN)
    (tmp_path / "conftest.py").write_text(_STAND_IN_CONFTEST.format(path=str(REPO / "conftest.py")))
    target = "test_stand_in.py::test_a_timing_cell" if only_marked else "test_stand_in.py"
    code = (
        "import sys\n" + prelude + "import pytest\n"
        "sys.exit(pytest.main(['-p', 'no:cacheprovider', '-q', '-rs', '-o', 'markers=timing: x', "
        + repr(target) + ", *" + repr(list(extra)) + "]))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=_clean_env(),
                       capture_output=True, text=True, timeout=180)
    return r, (tmp_path / "side_effect.txt").exists()


def test_end_to_end_a_traced_timing_cell_skips_before_the_module_fixture(tmp_path):
    r, side = _run_stand_in(tmp_path, True, prelude="sys.settrace(lambda *a: None)\n")
    out = r.stdout + r.stderr
    assert "1 skipped" in out and REASON in out, out
    assert not side, "the module-scoped fixture ran before the guard skipped"


def test_end_to_end_an_untraced_timing_cell_runs_and_passes(tmp_path):
    r, side = _run_stand_in(tmp_path, True)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and "1 passed" in out, out
    assert side


def test_end_to_end_a_traced_unmarked_cell_is_not_skipped(tmp_path):
    r, _ = _run_stand_in(tmp_path, False, prelude="sys.settrace(lambda *a: None)\n")
    out = r.stdout + r.stderr
    assert "1 skipped" in out and "1 passed" in out, out


def test_end_to_end_under_a_real_pytest_cov_run_the_timing_cell_skips(tmp_path):
    pytest.importorskip("pytest_cov")
    r, side = _run_stand_in(tmp_path, True, extra=["--cov=.", "--cov-report="])
    out = r.stdout + r.stderr
    assert "1 skipped" in out and REASON in out, out
    assert not side


# --- the timing step must not go green when every cell SKIPPED ----------------
# Under a tracer all timing cells skip and pytest exits 0: the bound would be
# unjudged and the ci-nightly issue could close. The step greps its own log for the
# skip reason and fails (keeping pytest's own exit code when it already failed).

import os
import shutil

_LOG = "/tmp/nightly-timing.log"


def _timing_script(steps):
    return _by_id(steps, "timing")["run"]


def _grepped_text(steps):
    m = re.search(r"grep\s+-q\s+-F\s+'([^']+)'", _timing_script(steps))
    assert m, "the timing step has no `grep -q -F '<skip reason>'` check"
    return m.group(1)


def test_the_timing_step_checks_its_log_for_the_skip_reason(steps):
    run = _timing_script(steps)
    assert _LOG in run and "::error::" in run
    assert _grepped_text(steps)


def test_the_text_the_step_greps_is_the_tail_of_the_guards_reason(steps):
    text = _grepped_text(steps)
    assert len(text) >= 25, text
    assert tg.TRACER_SKIP_REASON.endswith(text), (text, tg.TRACER_SKIP_REASON)
    assert text == "judged in the no-coverage timing step"


def _run_step(steps, tmp_path, fake_rc, lines):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "pytest"
    body = "#!/bin/sh\n" + "".join(f"echo {ln!r}\n" for ln in lines) + f"exit {fake_rc}\n"
    fake.write_text(body, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "timing.log"
    script = tmp_path / "step.sh"
    script.write_text(_timing_script(steps).replace(_LOG, str(log)), encoding="utf-8")
    env = {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}", "HOME": str(tmp_path)}
    return subprocess.run([bash, "--noprofile", "--norc", "-e", "-o", "pipefail", str(script)],
                          env=env, capture_output=True, text=True, timeout=60, cwd=tmp_path)


SKIP_LINE = "SKIPPED [32] conftest.py:232: " + tg.TRACER_SKIP_REASON


@pytest.mark.parametrize("fake_rc,lines,want", [
    (0, ["32 passed in 5s"], 0),
    (0, [SKIP_LINE, "32 skipped in 1s"], "nonzero"),
    (1, ["1 failed, 31 passed"], 1),
    (1, [SKIP_LINE, "1 failed"], "nonzero"),
])
def test_the_timing_step_exit_code_table(steps, tmp_path, fake_rc, lines, want):
    r = _run_step(steps, tmp_path, fake_rc, lines)
    if want == 0:
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "::error::" not in r.stdout
    elif want == 1:
        assert r.returncode == 1, (r.stdout, r.stderr)
    else:
        assert r.returncode != 0, (r.stdout, r.stderr)
        assert "::error::" in r.stdout and "NOT judged" in r.stdout, r.stdout


def test_a_failed_pytest_keeps_its_own_exit_code_when_the_skip_text_is_also_present(steps, tmp_path):
    r = _run_step(steps, tmp_path, 3, [SKIP_LINE])
    assert r.returncode == 3, (r.stdout, r.stderr)


def test_the_log_still_receives_the_pytest_output(steps, tmp_path):
    r = _run_step(steps, tmp_path, 0, ["32 passed in 5s"])
    assert r.returncode == 0
    assert "32 passed in 5s" in (tmp_path / "timing.log").read_text()
