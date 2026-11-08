"""Contract cells for the nightly workflow's N-process split (#4420).

Contract (stated before the workflow changed):

C1  the suite is N SEQUENTIAL pytest processes: no ``-n``, no ``--dist``, no xdist.
C3  exactly one combine step runs before the coverage upload; the upload and the
    coverage-summary steps are unchanged; ``--cov-branch`` survives the combine.
C4  the sampler is part of the suite step's script, and a summary step (never able
    to flip the night) prints one row per shard.
C5  ``id: suite`` is kept (the notify steps and ``suite_green`` read its outcome);
    the step limit is the sum of the per-shard limits plus headroom, the job limit
    sits 15 minutes above it, and the lost-runner release stays under the
    hosted-runner ceiling of 360 minutes.
C6  the no-coverage timing step is byte-identical to the pre-split workflow.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "nightly.yml"
CI_DIR = REPO / ".github" / "ci"

# C6: sha256 of the timing step, as parsed YAML dumped with sorted keys, taken from
# the workflow before the split. Pinned instead of compared against another ref so
# the cell needs no git history; any edit to that step changes the hash.
TIMING_STEP_SHA256 = "155e0732cac09a4e327b9001f46ce76d7e2fa274b529b2220e192a49edebed7b"
# The coverage-summary step reads coverage.xml from the workspace root; it must keep working untouched.
COVERAGE_SUMMARY_STEP_SHA256 = "70e12c7fa99926b0f1a2f012421db28c5212399056f19d8bc4086971ff2dbb44"

UPLOAD_STEP = {
    "name": "Upload coverage artifacts",
    "if": "always()",
    "uses": "actions/upload-artifact@v4",
    "with": {
        "name": "coverage-report",
        "path": "coverage.xml\nhtmlcov/\n",
        "if-no-files-found": "warn",
        "retention-days": 14,
    },
}

HOSTED_JOB_CEILING_MIN = 360
LOST_RUNNER_RELEASE_MIN = 5


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def job(wf):
    return wf["jobs"]["pytest"]


@pytest.fixture(scope="module")
def steps(job):
    return job["steps"]


def _sha(step):
    return hashlib.sha256(json.dumps(step, sort_keys=True).encode()).hexdigest()


def _by_id(steps, sid):
    hits = [s for s in steps if s.get("id") == sid]
    assert len(hits) == 1, (sid, len(hits))
    return hits[0]


def _idx(steps, step):
    return next(i for i, s in enumerate(steps) if s is step)


def _combine_steps(steps):
    return [s for s in steps if "coverage combine" in str(s.get("run", ""))]


def _code(text):
    return "\n".join(ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#"))


# --- C1 ------------------------------------------------------------------------------


def test_c1_suite_step_runs_the_shard_script_not_one_big_pytest(steps):
    suite = _by_id(steps, "suite")
    assert suite["name"] == "Run full pytest suite"
    code = _code(suite["run"])
    assert ".github/ci/nightly_run_shards.sh" in code
    assert not re.search(r"(^|\s)pytest\s", code), "the suite step must not call pytest itself"


def test_c1_no_xdist_anywhere_in_the_workflow_or_the_shard_scripts(wf):
    texts = [_code(WORKFLOW.read_text(encoding="utf-8"))]
    for name in ("nightly_run_shards.sh", "nightly_shards.py", "nightly_shard_summary.py"):
        texts.append(_code((CI_DIR / name).read_text(encoding="utf-8")))
    seen_cmds = []
    for t in texts:
        # Only logical pytest command lines: `tail -n 40` elsewhere is not xdist.
        logical = t.replace("\\\n", " ").splitlines()
        cmds = [x for x in logical if re.search(r"(^|\s)pytest\s+(\"\$|-|tests/|src/)", x)]
        seen_cmds.extend(cmds)
        for ln in cmds:
            assert not re.search(r"(^|\s)-n\s*(\d+|auto)\b", ln), ln
        assert "--dist" not in t and "xdist" not in t and "--numprocesses" not in t
    assert seen_cmds, "the scan found no pytest command line, so it proved nothing"


def test_c1_shard_count_is_one_named_value_with_a_comment(job):
    env = job.get("env") or {}
    assert int(env["NIGHTLY_SHARD_COUNT"]) >= 2
    assert int(env["NIGHTLY_SHARD_TIMEOUT_MIN"]) > 0
    raw = WORKFLOW.read_text(encoding="utf-8")
    i = raw.index("NIGHTLY_SHARD_COUNT:")
    assert "#" in raw[max(0, i - 900):i], "the shard count needs its basis written next to it"


# --- C3 ------------------------------------------------------------------------------


def test_c3_exactly_one_combine_step_and_it_precedes_the_upload(steps):
    combos = _combine_steps(steps)
    assert len(combos) == 1
    upload = next(s for s in steps if s.get("name") == "Upload coverage artifacts")
    assert _idx(steps, combos[0]) < _idx(steps, upload)
    assert _idx(steps, combos[0]) > _idx(steps, _by_id(steps, "suite"))
    run = combos[0]["run"]
    for needle in ("coverage xml -o coverage.xml", "coverage html -d htmlcov", "coverage report"):
        assert needle in run
    assert combos[0].get("if") == "always()"


def test_c3_upload_and_coverage_summary_steps_are_unchanged(steps):
    upload = next(s for s in steps if s.get("name") == "Upload coverage artifacts")
    assert upload == UPLOAD_STEP
    summary = next(s for s in steps if s.get("name") == "Write coverage summary")
    assert _sha(summary) == COVERAGE_SUMMARY_STEP_SHA256


def _wrapper(path, target):
    path.write_text(f'#!/bin/sh\nexec "{target}" "$@"\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _make_project(root):
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "pkg" / "a.py").write_text(
        "def f(x):\n    if x > 0:\n        return 1\n    return 2\n", encoding="utf-8")
    (root / "src" / "pkg" / "b.py").write_text(
        "def g(x):\n    if x:\n        return 1\n    return 2\n", encoding="utf-8")
    (root / "src" / "pkg" / "unused.py").write_text("def never():\n    return 3\n", encoding="utf-8")
    (root / "tests" / "test_one.py").write_text(
        "import sys\nsys.path.insert(0, 'src')\nfrom pkg import a\n\n\ndef test_a():\n    assert a.f(1) == 1\n",
        encoding="utf-8")
    (root / "tests" / "test_two.py").write_text(
        "import sys\nsys.path.insert(0, 'src')\nfrom pkg import b\n\n\ndef test_b():\n    assert b.g(0) == 2\n",
        encoding="utf-8")
    (root / ".coveragerc").write_text((REPO / ".coveragerc").read_text(encoding="utf-8"), encoding="utf-8")


def _pytest_cov(root, files, cov_file=None, extra=()):
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if cov_file:
        env["COVERAGE_FILE"] = cov_file
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--cov=src", "--cov-branch",
         *extra, *files],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr


def _header(xml_path):
    m = re.search(r"<coverage [^>]*>", xml_path.read_text(encoding="utf-8"))
    return dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(0)))


def test_c3_two_shards_combine_to_the_single_process_artifact_shape(steps, tmp_path):
    # The cell runs real `pytest --cov` processes: without pytest-cov in the venv it is
    # not a failure of this PR, so it skips (CI installs pytest-cov via requirements).
    pytest.importorskip("pytest_cov")
    combine = _combine_steps(steps)[0]
    shard = tmp_path / "shardrun"
    shard.mkdir()
    _make_project(shard)
    _pytest_cov(shard, ["tests/test_one.py"], ".coverage.1")
    _pytest_cov(shard, ["tests/test_two.py"], ".coverage.2")
    assert (shard / ".coverage.1").exists() and (shard / ".coverage.2").exists()

    binp = tmp_path / "bin"
    binp.mkdir()
    _wrapper(binp / "python3", sys.executable)
    _wrapper(binp / "python", sys.executable)
    script = tmp_path / "combine.sh"
    script.write_text(combine["run"], encoding="utf-8")
    env = dict(os.environ, PATH=f"{binp}{os.pathsep}{os.environ['PATH']}", PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run(["bash", "-e", "-o", "pipefail", str(script)], cwd=str(shard), env=env,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (shard / "coverage.xml").exists()
    assert (shard / "htmlcov" / "index.html").exists()
    assert "TOTAL" in r.stdout  # the term report

    single = tmp_path / "single"
    single.mkdir()
    _make_project(single)
    _pytest_cov(single, ["tests"], None, extra=["--cov-report=xml:coverage.xml"])

    hs, h1 = _header(shard / "coverage.xml"), _header(single / "coverage.xml")
    assert "branch-rate" in hs and "branches-covered" in hs and int(hs["branches-valid"]) > 0
    names_s = sorted(re.findall(r'<class [^>]*filename="([^"]+)"', (shard / "coverage.xml").read_text()))
    names_1 = sorted(re.findall(r'<class [^>]*filename="([^"]+)"', (single / "coverage.xml").read_text()))
    # Same path root as the --cov=src single-process run (pkg/a.py, not src/pkg/a.py),
    # and the never-imported module is still reported as a gap.
    assert names_s == ["pkg/a.py", "pkg/b.py", "pkg/unused.py"]
    assert names_1 == names_s
    for key in ("lines-valid", "lines-covered", "branches-valid", "branches-covered"):
        assert hs[key] == h1[key], (key, hs, h1)


# --- C4 ------------------------------------------------------------------------------


def test_c4_summary_step_cannot_flip_the_night_and_follows_the_combine(steps):
    hits = [s for s in steps if "nightly_shard_summary.py" in str(s.get("run", ""))]
    assert len(hits) == 1
    s = hits[0]
    assert s.get("continue-on-error") is True
    assert str(s.get("if")) == "always()"
    assert _idx(steps, s) > _idx(steps, _combine_steps(steps)[0])
    assert "--n" in s["run"] and "NIGHTLY_SHARD_COUNT" in s["run"]


def test_c4_the_sampler_is_permanent_and_cleans_up_after_itself():
    code = _code((CI_DIR / "nightly_run_shards.sh").read_text(encoding="utf-8"))
    assert "[sampler]" in code
    assert "tail -n +1 -F" in code
    assert re.search(r"trap .* EXIT", code)
    assert "/tmp/sampler.log" in code


def test_no_diagnostic_leftovers_in_the_workflow(wf):
    raw = WORKFLOW.read_text(encoding="utf-8")
    for needle in ("pytest-timeout", "gctime4420", ".github/diag", "3.12.15",
                   "github.event_name == 'schedule'", "--timeout="):
        assert needle not in raw, needle


# --- C5 ------------------------------------------------------------------------------


def test_c5_notify_steps_still_read_the_suite_outcome(steps, job):
    for name in ("on failure — open or update the ci-nightly issue",
                 "on success — close any open ci-nightly issue"):
        s = next(x for x in steps if x.get("name") == name)
        assert "steps.suite.outcome" in s["if"]
        assert "steps.timing.outcome" in s["if"]
    assert "steps.notify_success.outputs.suite_green" in job["outputs"]["suite_green"]


def test_c5_timeout_arithmetic_step_is_sum_of_shards_plus_headroom_job_is_step_plus_15(job, steps):
    suite = _by_id(steps, "suite")
    n = int(job["env"]["NIGHTLY_SHARD_COUNT"])
    per = int(job["env"]["NIGHTLY_SHARD_TIMEOUT_MIN"])
    step_limit = suite["timeout-minutes"]
    assert step_limit == n * per + 15, (n, per, step_limit)
    assert job["timeout-minutes"] == step_limit + 15
    assert job["timeout-minutes"] + LOST_RUNNER_RELEASE_MIN <= HOSTED_JOB_CEILING_MIN


def test_c5_the_4389_comment_states_the_current_numbers():
    raw = WORKFLOW.read_text(encoding="utf-8")
    block = raw[raw.index("#4389: the job limit"):raw.index("timeout-minutes:")]
    wfd = yaml.safe_load(raw)
    job = wfd["jobs"]["pytest"]
    suite = next(s for s in job["steps"] if s.get("id") == "suite")
    assert str(suite["timeout-minutes"]) in block
    assert str(job["timeout-minutes"]) in block
    assert str(job["timeout-minutes"] + 5) in block
    assert "195" not in block and "180" not in block


# --- C6 ------------------------------------------------------------------------------


def test_c6_timing_step_is_byte_identical_to_the_pre_split_workflow(steps):
    timing = _by_id(steps, "timing")
    assert _sha(timing) == TIMING_STEP_SHA256
    assert "-m timing" in timing["run"]
