"""Pins for the nightly workflow's disk-read + no-pip-cache contract (#3539).

The nightly's `Post Set up Python` step (setup-python's pip-cache save) hit
`No space left on device` and failed an otherwise green 17547-test run.
The fix drops the cache and adds two read-only disk-measurement steps so the
real disk writer can be identified. The contract pinned here:

(a) a disk-read step can NEVER change the job's conclusion: `continue-on-error:
    true`, every command guarded with `|| true` (GitHub runs `shell: bash` as
    `bash -e -o pipefail`), proven by running the real script under that shell;
(b) the `id: suite` step and the two ci-nightly notify steps' `if:` strings are
    unchanged — `steps.suite.outcome` stays the only thing they key on;
(c) no `actions/setup-python` step in nightly.yml uses `cache:`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "nightly.yml"

BEFORE = "disk usage — before suite"
AFTER = "disk usage — after suite"

ON_FAILURE_NAME = "on failure — open or update the ci-nightly issue"
ON_FAILURE_IF = "${{ !cancelled() && steps.suite.outcome != 'success' }}"
ON_SUCCESS_NAME = "on success — close any open ci-nightly issue"
ON_SUCCESS_IF = "${{ !cancelled() && steps.suite.outcome == 'success' }}"


@pytest.fixture(scope="module")
def steps():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return wf["jobs"]["pytest"]["steps"]


def _index(steps, name):
    hits = [i for i, s in enumerate(steps) if s.get("name") == name]
    assert len(hits) == 1, f"expected exactly one step named {name!r}, got {len(hits)}"
    return hits[0]


def _suite_index(steps):
    hits = [i for i, s in enumerate(steps) if s.get("id") == "suite"]
    assert len(hits) == 1, f"expected exactly one id: suite step, got {len(hits)}"
    return hits[0]


def _install_index(steps):
    return _index(steps, "Install dependencies")


# --- (c) no pip cache -------------------------------------------------------


def test_no_setup_python_step_uses_cache(steps):
    setups = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python")]
    assert setups, "expected at least one actions/setup-python step"
    for s in setups:
        with_ = s.get("with") or {}
        assert "cache" not in with_, f"setup-python step still sets cache: {with_!r}"
        assert "cache-dependency-path" not in with_, (
            f"setup-python step still sets cache-dependency-path: {with_!r}"
        )


# --- disk steps: existence + ordering --------------------------------------


def test_before_step_sits_between_install_and_suite(steps):
    assert _install_index(steps) < _index(steps, BEFORE) < _suite_index(steps)


def test_before_step_is_adjacent_to_suite(steps):
    # "after Install dependencies, before Run full pytest suite" — nothing
    # else may creep between the read and the suite it measures.
    assert _index(steps, BEFORE) + 1 == _suite_index(steps)


def test_after_step_immediately_follows_suite(steps):
    assert _index(steps, AFTER) == _suite_index(steps) + 1


# --- (a) can never change the job's conclusion -----------------------------


@pytest.mark.parametrize("name", [BEFORE, AFTER])
def test_disk_steps_are_bash_and_continue_on_error(steps, name):
    step = steps[_index(steps, name)]
    assert step.get("shell") == "bash"
    assert step.get("continue-on-error") is True


@pytest.mark.parametrize("name", [BEFORE, AFTER])
def test_disk_steps_have_a_timeout(steps, name):
    # A timeout under continue-on-error cannot change the job conclusion, so a
    # hung `du` costs at most this long between the suite and the artifact upload.
    t = steps[_index(steps, name)].get("timeout-minutes")
    assert isinstance(t, int) and not isinstance(t, bool) and 0 < t <= 15, t


def _normalised_run(steps, name):
    run = steps[_index(steps, name)]["run"]
    return run.replace("before suite", "<LABEL>").replace("after suite", "<LABEL>")


def test_disk_step_scripts_do_not_drift(steps):
    # Two copies of ~28 lines: an edit to one must land in the other. Only the
    # step-name text in the summary heading may differ.
    assert _normalised_run(steps, BEFORE) == _normalised_run(steps, AFTER)


# GitHub runs an explicit `shell: bash` step as exactly this command line, so
# errexit AND pipefail are ON. errexit is not inherited into `$(...)` but
# pipefail is, so the substitution's status can be non-zero and the assignment
# would then trip errexit. The `|| true` inside the substitution and the
# assignment's `)" || true` are two layers, each sufficient alone (removing only
# one stays green by design; removing both goes red). The summary-append guard
# is separately load-bearing. The contract is enforced by running the real
# script under that shell and asserting it exits 0 however the reads fail.
GH_BASH_ARGS = ["--noprofile", "--norc", "-e", "-o", "pipefail"]


def _run_script(run, tmp_path, summary, stubs=False, timeout=60):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    script = tmp_path / "step.sh"
    script.write_text(run, encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    rt = tmp_path / "rt"
    rt.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    path = os.environ.get("PATH", "")
    if stubs:
        sd = tmp_path / "stubs"
        sd.mkdir(exist_ok=True)
        for cmd in ("df", "du", "sort", "tail"):
            f = sd / cmd
            f.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            f.chmod(0o755)
        path = f"{sd}{os.pathsep}{path}"
    env = {"PATH": path, "HOME": str(home), "RUNNER_TEMP": str(rt), "GITHUB_WORKSPACE": str(ws)}
    if summary is not None:
        env["GITHUB_STEP_SUMMARY"] = str(summary)
    return subprocess.run(
        [bash, *GH_BASH_ARGS, str(script)],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


@pytest.mark.parametrize("name", [BEFORE, AFTER])
def test_disk_steps_exit_zero_when_writes_fail(steps, tmp_path, name):
    run = steps[_index(steps, name)]["run"]
    if not Path("/dev/full").exists():
        pytest.skip("/dev/full not available")
    r = _run_script(run, tmp_path, "/dev/full")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("name", [BEFORE, AFTER])
def test_disk_steps_exit_zero_when_summary_unwritable(steps, tmp_path, name):
    run = steps[_index(steps, name)]["run"]
    r = _run_script(run, tmp_path, tmp_path / "no-such-dir" / "summary.md")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("name", [BEFORE, AFTER])
def test_disk_steps_exit_zero_when_summary_unset(steps, tmp_path, name):
    run = steps[_index(steps, name)]["run"]
    r = _run_script(run, tmp_path, None)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("name", [BEFORE, AFTER])
def test_disk_steps_exit_zero_when_disk_tools_fail(steps, tmp_path, name):
    run = steps[_index(steps, name)]["run"]
    r = _run_script(run, tmp_path, tmp_path / "summary.md", stubs=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("name", [BEFORE, AFTER])
def test_disk_steps_normal_run_writes_summary(steps, tmp_path, name):
    # Positive control: proves the summary write is actually observable, so the
    # failure-case tests above are not passing on a script that never writes.
    run = steps[_index(steps, name)]["run"]
    summary = tmp_path / "summary.md"
    r = _run_script(run, tmp_path, summary)
    assert r.returncode == 0, r.stderr
    assert "### disk usage —" in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", [BEFORE, AFTER])
def test_disk_steps_read_the_specified_paths(steps, name):
    run = steps[_index(steps, name)]["run"]
    for needle in ("df -h /", "df -h /tmp", "du -xsh", "RUNNER_TEMP", "GITHUB_WORKSPACE",
                   ".cache", "du -x -d 3", "sort -h", "tail -20"):
        assert needle in run, f"{needle!r} missing from {name!r}"


def test_after_step_runs_when_suite_is_red(steps):
    cond = steps[_index(steps, AFTER)].get("if")
    assert cond is not None and "!cancelled()" in str(cond)


# --- (b) notify contract untouched -----------------------------------------


def test_suite_step_keeps_its_id(steps):
    assert steps[_suite_index(steps)]["name"] == "Run full pytest suite"


def test_ci_nightly_if_expressions_unchanged(steps):
    assert steps[_index(steps, ON_FAILURE_NAME)]["if"] == ON_FAILURE_IF
    assert steps[_index(steps, ON_SUCCESS_NAME)]["if"] == ON_SUCCESS_IF
