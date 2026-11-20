"""Contract tests for tools/coverage-diff.sh, the advisory coverage report (#4358 ask 3).

Written before the script change. The contract: nonzero ONLY for a usage error
(exit 2); everything else exits 0 with one plain line saying what happened; the
caveat is printed with every report and names the subprocess blind spot.

Hermetic: the script is copied into a temp git repo, with a stub selector, a
python wrapper that fakes the coverage / pytest_cov imports, and a stub
diff-cover that records its argv. Nothing here depends on what the host has
installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT_SRC = REPO / "tools" / "coverage-diff.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="needs bash and git",
)

# Two modes, two exit codes: `--json` is the classify phase (STUB_JSON_RC, default 0;
# its message goes to stderr like the real selector's refusals); anything else is the
# run phase, whose exit code is pytest's (STUB_RC, default 0).
SELECTOR_STUB = '''\
import os, sys
if "--json" in sys.argv:
    open(os.environ["RUN_MARK"], "a").write("json " + " ".join(sys.argv[1:]) + "\\n")
    print(os.environ.get("STUB_JSON_OUT", ""), file=sys.stderr)
    print("{}")
    sys.exit(int(os.environ.get("STUB_JSON_RC", "0")))
open(os.environ["RUN_MARK"], "a").write("run " + " ".join(sys.argv[1:]) + "\\n")
print(os.environ.get("STUB_OUT", ""))
if os.environ.get("STUB_WRITE_XML") == "1":
    for a in sys.argv:
        if a.startswith("--cov-report=xml:"):
            open(a.split(":", 1)[1], "w").write("<coverage/>")
sys.exit(int(os.environ.get("STUB_RC", "0")))
'''

PY_WRAPPER = f'''#!/bin/bash
# Fakes the import probes; everything else runs under the real interpreter.
if [ "$1" = "-c" ]; then
  case "$2" in
    *import*coverage*|*import*pytest_cov*)
      for m in $STUB_MISSING; do
        case "$2" in *"$m"*) exit 1;; esac
      done
      exit 0;;
  esac
fi
exec "{sys.executable}" "$@"
'''

DIFFCOVER_STUB = '''#!/bin/bash
echo "$@" >> "$DC_LOG"
echo "DIFFCOVER-REPORT"
exit "${DC_RC:-0}"
'''


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    shutil.copy(SCRIPT_SRC, repo / "tools" / "coverage-diff.sh")
    (repo / "tools" / "run_touched_tests.py").write_text(SELECTOR_STUB)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    py = bindir / "python"
    py.write_text(PY_WRAPPER)
    py.chmod(0o755)
    dc = bindir / "diff-cover"
    dc.write_text(DIFFCOVER_STUB)
    dc.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return {
        "repo": repo, "bin": bindir, "py": py, "dc": dc, "home": home,
        "run_mark": tmp_path / "run_mark", "dc_log": tmp_path / "dc_log",
    }


def run(env, *args, extra=None, engram_py=True):
    e = {
        "PATH": os.environ["PATH"],
        "HOME": str(env["home"]),
        "RUN_MARK": str(env["run_mark"]),
        "DC_LOG": str(env["dc_log"]),
    }
    if engram_py:
        e["ENGRAM_PY"] = str(env["py"])
    e.update(extra or {})
    p = subprocess.run(
        ["bash", str(env["repo"] / "tools" / "coverage-diff.sh"), *args],
        cwd=env["repo"], env=e, capture_output=True, text=True, timeout=60,
    )
    return p.returncode, p.stdout + p.stderr


def selector_ran(env):
    """Any selector invocation at all (classify or run)."""
    return env["run_mark"].exists()


def run_mode_ran(env):
    return bool(calls(env, "run"))


def calls(env, mode):
    """The selector's argv lines for one mode ('json' or 'run'), after the mode word."""
    if not env["run_mark"].exists():
        return []
    return [ln.split(" ", 1)[1] for ln in env["run_mark"].read_text().splitlines()
            if ln.split(" ", 1)[0] == mode]


def dc_called(env):
    return env["dc_log"].exists()


# --- usage errors: the only nonzero exit, and it is 2 -----------------------

def test_unknown_option_exits_2(env):
    rc, out = run(env, "--bogus")
    assert rc == 2
    assert "unknown option" in out and "--bogus" in out
    assert not selector_ran(env)


def test_base_without_value_exits_2(env):
    rc, out = run(env, "--base")
    assert rc == 2
    assert "--base needs a value" in out
    assert not selector_ran(env)


def test_unresolvable_base_exits_2_and_selector_never_runs(env):
    rc, out = run(env, "--base", "no-such-ref")
    assert rc == 2
    assert "no-such-ref" in out and "does not resolve" in out
    assert not selector_ran(env)


def test_default_base_unresolvable_in_this_repo_exits_2(env):
    # the temp repo has no origin/dev, so the default base must be rejected up front
    rc, out = run(env)
    assert rc == 2
    assert "origin/dev" in out and "does not resolve" in out
    assert not selector_ran(env)


@pytest.mark.parametrize("sel_rc", [2, 3, 4, 5, 6])
def test_selector_refusal_exits_2_and_prints_its_output(env, sel_rc):
    rc, out = run(env, "--base", "HEAD",
                  extra={"STUB_JSON_RC": str(sel_rc), "STUB_JSON_OUT": "SELECTOR-SAYS-NO"})
    assert rc == 2
    assert "SELECTOR-SAYS-NO" in out
    assert "the selector refused" in out and f"exit {sel_rc}" in out
    assert not run_mode_ran(env)  # the refusal is read from --json; no test run
    assert not dc_called(env)


# --- item 1: full-suite trigger, detected by exit code ----------------------

def test_exit_7_is_a_full_suite_trigger_even_without_the_phrase(env):
    rc, out = run(env, "--base", "HEAD",
                  extra={"STUB_JSON_RC": "7", "STUB_JSON_OUT": "some unrelated words"})
    assert rc == 0
    assert "full-suite trigger" in out and "exit 7" in out
    assert "no tests ran" in out and "no coverage report" in out
    assert not run_mode_ran(env)
    assert not dc_called(env)
    assert not (env["repo"] / "coverage.xml").exists()


def test_trigger_phrase_in_output_is_not_a_trigger_when_exit_is_0(env):
    rc, out = run(env, "--base", "HEAD",
                  extra={"STUB_RC": "0", "STUB_WRITE_XML": "1",
                         "STUB_OUT": "FULL-SUITE TRIGGER: test name mentions it"})
    assert rc == 0
    assert dc_called(env)
    assert "no tests ran" not in out


# --- round 2: classify with --json, then run; the run phase's code is pytest's ---

def test_json_unknown_code_is_named_and_exits_0(env):
    rc, out = run(env, "--base", "HEAD", extra={"STUB_JSON_RC": "9"})
    assert rc == 0
    assert "exited 9" in out
    assert not run_mode_ran(env) and not dc_called(env)


def test_collection_error_in_run_phase_is_not_a_refusal(env):
    # --json classifies cleanly (0); pytest then exits 2 (collection error).
    rc, out = run(env, "--base", "HEAD",
                  extra={"STUB_JSON_RC": "0", "STUB_RC": "2", "STUB_WRITE_XML": "1"})
    assert rc == 0
    assert "the selector refused" not in out
    assert "exited 2" in out and "pytest" in out and "collection error" in out
    assert dc_called(env) and "DIFFCOVER-REPORT" in out
    assert "subprocess" in out  # caveat still printed


def test_collection_error_without_coverage_xml_exits_0(env):
    rc, out = run(env, "--base", "HEAD", extra={"STUB_RC": "2"})
    assert rc == 0
    assert "no coverage data" in out
    assert not dc_called(env)


# --- item 2: missing tools --------------------------------------------------

@pytest.mark.parametrize("missing", ["coverage", "pytest_cov"])
def test_missing_python_package_named_and_nothing_run(env, missing):
    rc, out = run(env, "--base", "HEAD", extra={"STUB_MISSING": missing})
    assert rc == 0
    assert missing in out
    assert str(env["py"]) in out
    assert "ENGRAM_PY" in out and "environment" in out
    assert "pip install" in out
    assert not selector_ran(env) and not dc_called(env)


def test_missing_diff_cover_named_and_nothing_run(env):
    env["dc"].unlink()
    rc, out = run(env, "--base", "HEAD")
    assert rc == 0
    assert "diff-cover" in out and str(env["bin"] / "diff-cover") in out
    assert str(env["py"]) in out
    assert "pip install" in out
    assert not selector_ran(env)


def test_missing_interpreter_default_choice_is_stated(env):
    rc, out = run(env, "--base", "HEAD", engram_py=False)
    default = str(env["home"] / ".engram" / "venv" / "bin" / "python")
    assert rc == 0
    assert default in out
    assert "default" in out and "ENGRAM_PY" in out
    assert not selector_ran(env)


def test_missing_interpreter_from_environment_is_stated(env):
    gone = env["bin"] / "nope" / "python"
    rc, out = run(env, "--base", "HEAD", extra={"ENGRAM_PY": str(gone)},
                  engram_py=False)
    assert rc == 0
    assert str(gone) in out
    assert "ENGRAM_PY" in out and "environment" in out
    assert not selector_ran(env)


def test_two_missing_pieces_are_separated_and_both_named(env):
    env["dc"].unlink()
    rc, out = run(env, "--base", "HEAD", extra={"STUB_MISSING": "coverage"})
    assert rc == 0
    assert "the python package coverage; diff-cover at " + str(env["bin"] / "diff-cover") in out
    assert not selector_ran(env)


def test_three_missing_pieces_are_all_separated(env):
    env["dc"].unlink()
    rc, out = run(env, "--base", "HEAD", extra={"STUB_MISSING": "coverage pytest_cov"})
    assert rc == 0
    assert ("missing the python package coverage; the python package pytest_cov; "
            "diff-cover at ") in out


# --- the selector's argv, both phases ---------------------------------------

def test_selector_argv_classify_and_run_phases(env):
    rc, out = run(env, "--base", "HEAD", extra={"STUB_WRITE_XML": "1"})
    assert rc == 0
    (cl,) = calls(env, "json")
    (rn,) = calls(env, "run")
    assert "--base HEAD" in cl and "--json" in cl.split()
    assert "--base HEAD" in rn and "--json" not in rn.split()
    after = rn.split(" -- ", 1)[1].split()
    assert "--cov=src" in after and "--cov-branch" in after


# --- item 3: red run --------------------------------------------------------

def test_red_run_still_reports_and_exits_0(env):
    rc, out = run(env, "--base", "HEAD",
                  extra={"STUB_RC": "1", "STUB_WRITE_XML": "1"})
    assert rc == 0
    assert dc_called(env) and "DIFFCOVER-REPORT" in out
    assert "the test run exited 1" in out and "tests failed" in out
    assert "report reflects that run" in out
    assert "subprocess" in out  # caveat still printed


# --- item 4: no coverage.xml ------------------------------------------------

@pytest.mark.parametrize("sel_rc", [0, 1])
def test_no_coverage_xml_after_a_run_exits_0(env, sel_rc):
    (env["repo"] / "coverage.xml").write_text("<stale/>")  # must not be trusted
    rc, out = run(env, "--base", "HEAD", extra={"STUB_RC": str(sel_rc)})
    assert rc == 0
    assert "no coverage data" in out
    assert not dc_called(env)


# --- item 5: diff-cover fails -----------------------------------------------

def test_diff_cover_failure_reports_its_exit_code_and_exits_0(env):
    rc, out = run(env, "--base", "HEAD",
                  extra={"STUB_WRITE_XML": "1", "DC_RC": "5"})
    assert rc == 0
    assert "diff-cover failed" in out and "exit 5" in out


# --- item 6: the caveat -----------------------------------------------------

def test_caveat_is_todays_and_names_the_subprocess_blind_spot(env):
    rc, out = run(env, "--base", "HEAD",
                  extra={"STUB_WRITE_XML": "1",
                         "STUB_OUT": "SUBSET green (3/10 selected for 1 changed files) -- ok"})
    assert rc == 0
    assert "only the tests THIS run selected" in out
    assert "The full suite runs nightly on dev and at the release cut" in out
    assert "convergence gate" not in out
    assert "subprocess" in out and "not measured" in out
    assert "hooks" in out and "shell scripts" in out
    assert "subprocess coverage is not configured" in out
    assert "SUBSET green (3/10 selected for 1 changed files)" in out


def test_caveat_falls_back_when_no_subset_line(env):
    rc, out = run(env, "--base", "HEAD", extra={"STUB_WRITE_XML": "1"})
    assert rc == 0
    assert "see the selector's output above" in out


def test_diff_cover_gets_the_base_and_branch_flags(env):
    run(env, "--base", "HEAD", extra={"STUB_WRITE_XML": "1"})
    argv = env["dc_log"].read_text()
    assert "--compare-branch=HEAD" in argv and "--branch-coverage" in argv
    assert "--fail-under" not in argv


# --- item 7: the header states the contract ---------------------------------

def test_header_exit_codes_block_states_the_contract():
    head = SCRIPT_SRC.read_text().split("set -uo pipefail")[0]
    block = head.split("EXIT CODES", 1)[1]
    assert "usage error" in block and "2" in block
    assert "full-suite" in block.lower()
    assert "\n#   1 " not in block  # the old setup-error exit 1 is gone
