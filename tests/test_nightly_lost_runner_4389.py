"""Contract cells for the nightly's lost-runner second job (#4389).

Problem: nightly.yml had ONE job, `pytest`, and both ci-nightly notify steps lived
inside it. When a hosted runner is lost they die with it and no issue is filed.
The fix: (a) a job-level and a suite-step time limit, (b) a SECOND job that runs
after the first, on its own runner, and files the issue when the first did not
succeed, (c) no duplicate update when the in-job step already filed.

Measured inputs this contract rests on (probes on a scratch repo, 2026-11-06,
simulated runner loss, limits of 3 and 15 minutes). They are inputs, not
something these cells prove:

1. A job-level `timeout-minutes` releases a lost runner at limit + 5 min; the
   job's result in `needs` is `cancelled`.
2. A needs job gated `!cancelled() && needs.X.result != 'success'` RUNS after a
   failed step, a step timeout, a live-runner job-limit cancel and a lost-runner
   job-limit cancel, and does NOT run after a HUMAN `gh run cancel`.
3. A job output set in a step is readable by the needs job after a failed step,
   a step timeout, a job-limit cancel; it is EMPTY after a lost runner.
4. `gh issue list --label` and the REST list miss a write about a second old, so
   the SAME-NIGHT skip decision must NOT read the issue list.
5. A missing label makes `gh issue create` fail; the second job creates it.

The cells (C1..C6):

C1  limits: job 195, `id: suite` step 180, step lower than job by exactly 15.
C2  second job structure: exactly one other job, needs == pytest, ubuntu-latest,
    its own timeout <= 15, no checkout, `issues: write` not narrowed.
C3  the gate: the REAL `if:` string is evaluated by a small evaluator for the
    GitHub expression subset (unknown token -> assert, never a silent pass) over
    the truth table (cancelled_run, result, notified) -> runs.
C4  the in-job output: step id, `jobs.pytest.outputs.notified` wiring, and the
    REAL `run:` block executed under `bash -e -o pipefail` against a stub `gh`:
    `notified=true` is written IFF the create/comment call returned success.
C5  the second job's script: its REAL `run:` steps executed in order, from an
    EMPTY cwd (the job has no checkout), against the same stub `gh`.
C6  PROSE PINS (deliberately loose regexes, they pin wording that is part of the
    contract, not behaviour): the accepted-extra-update comment and the
    output-set-only-after-success comment.

Pure-evaluator self-tests (`test_evaluator_selftest_*`) check the test's own
instrument and pass regardless of nightly.yml.

The workflow path is read from the env var NIGHTLY_WORKFLOW_PATH (default: the
real file) so a mutated copy can be pointed at without touching the repo.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

DEFAULT_WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "nightly.yml"
)


def _workflow_path() -> Path:
    override = os.environ.get("NIGHTLY_WORKFLOW_PATH")
    return Path(override) if override else DEFAULT_WORKFLOW


ON_FAILURE_NAME = "on failure — open or update the ci-nightly issue"
ON_FAILURE_IF = (
    "${{ !cancelled() && (steps.suite.outcome != 'success' || steps.timing.outcome != 'success') }}"
)

SERVER_URL = "https://github.example"
REPOSITORY = "acme/widgets"
RUN_ID = "424242"
RUN_URL = f"{SERVER_URL}/{REPOSITORY}/actions/runs/{RUN_ID}"
TITLE_RE = re.compile(r"^nightly CI failure — \d{4}-\d{2}-\d{2}$")


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="module")
def raw():
    return _workflow_path().read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def wf(raw):
    return yaml.safe_load(raw)


@pytest.fixture(scope="module")
def pytest_job(wf):
    assert "pytest" in wf["jobs"], "nightly.yml has no `pytest` job"
    return wf["jobs"]["pytest"]


def _second_job(wf):
    others = [k for k in wf["jobs"] if k != "pytest"]
    assert len(others) == 1, (
        f"expected exactly one job other than `pytest` (the lost-runner notify job), "
        f"found {len(others)}: {others}"
    )
    return wf["jobs"][others[0]]


def _step_by_name(job, name):
    hits = [s for s in job["steps"] if s.get("name") == name]
    assert len(hits) == 1, f"expected exactly one step named {name!r}, got {len(hits)}"
    return hits[0]


def _step_by_id(job, sid):
    hits = [s for s in job["steps"] if s.get("id") == sid]
    assert len(hits) == 1, f"expected exactly one step with id {sid!r}, got {len(hits)}"
    return hits[0]


# --- the GitHub expression evaluator (the test's instrument) -----------------

_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<str>'(?:[^']|'')*')
      | (?P<op>&&|\|\||==|!=|!|\(|\))
      | (?P<ident>[A-Za-z_][A-Za-z0-9_.\-]*)
    )""",
    re.VERBOSE,
)
_STATUS_FUNCS = {"success", "failure", "always", "cancelled"}
_VARIABLES = {
    "needs.pytest.result",
    "needs.pytest.outputs.notified",
    "needs.pytest.outputs.suite_green",
}


def _tokenize(expr: str):
    pos, out = 0, []
    expr = expr.rstrip()
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        assert m is not None and m.end() > pos, (
            f"expression evaluator: unknown token at {expr[pos:pos + 20]!r} in {expr!r}"
        )
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


def evaluate_if(
    if_value, *, cancelled_run: bool, result: str, notified: str, suite_green: str = ""
) -> bool:
    """Evaluate a job-level `if:` for the GitHub expression subset these workflows use.

    Models: `always()` True even in a cancelled run; `cancelled()` = the run was
    cancelled by a person; success()/failure() over needs.pytest.result; the
    implicit `success() &&` when no status function appears. Unknown token or
    variable -> AssertionError.
    """
    assert isinstance(if_value, str), f"`if:` must be a string, got {if_value!r}"
    expr = if_value.strip()
    m = re.fullmatch(r"\$\{\{(.*)\}\}", expr, re.DOTALL)
    if m:
        expr = m.group(1)
    assert "${{" not in expr and "}}" not in expr, f"malformed expression wrapper: {if_value!r}"
    toks = _tokenize(expr)
    assert toks, f"empty expression: {if_value!r}"

    def is_func_at(i):
        return (
            toks[i][0] == "ident"
            and i + 2 < len(toks) + 0
            and toks[i + 1] == ("op", "(")
            and toks[i + 2] == ("op", ")")
        )

    has_status = any(
        toks[i][0] == "ident" and toks[i][1] in _STATUS_FUNCS and is_func_at(i)
        for i in range(len(toks))
    )

    env = {
        "needs.pytest.result": result,
        "needs.pytest.outputs.notified": notified,
        "needs.pytest.outputs.suite_green": suite_green,
    }

    def fn(name):
        if name == "always":
            return True
        if name == "cancelled":
            return cancelled_run
        if name == "success":
            return result == "success" and not cancelled_run
        if name == "failure":
            return result == "failure" and not cancelled_run
        raise AssertionError(f"expression evaluator: unknown function {name}()")

    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else (None, None)

    def take():
        t = peek()
        pos[0] += 1
        return t

    def truthy(v):
        return bool(v)

    def eq(a, b):
        if isinstance(a, str) and isinstance(b, str):
            return a.lower() == b.lower()
        return a == b

    def parse_or():
        v = parse_and()
        while peek() == ("op", "||"):
            take()
            r = parse_and()
            v = truthy(v) or truthy(r)
        return v

    def parse_and():
        v = parse_cmp()
        while peek() == ("op", "&&"):
            take()
            r = parse_cmp()
            v = truthy(v) and truthy(r)
        return v

    def parse_cmp():
        v = parse_unary()
        while peek() in (("op", "=="), ("op", "!=")):
            op = take()[1]
            r = parse_unary()
            v = eq(v, r) if op == "==" else (not eq(v, r))
        return v

    def parse_unary():
        if peek() == ("op", "!"):
            take()
            return not truthy(parse_unary())
        return parse_primary()

    def parse_primary():
        kind, text = take()
        assert kind is not None, f"expression evaluator: unexpected end in {if_value!r}"
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "op" and text == "(":
            v = parse_or()
            assert take() == ("op", ")"), f"expression evaluator: missing ) in {if_value!r}"
            return v
        if kind == "ident":
            if peek() == ("op", "("):
                assert text in _STATUS_FUNCS, f"expression evaluator: unknown function {text}()"
                take()
                assert take() == ("op", ")"), f"expression evaluator: {text}( must close at once"
                return fn(text)
            assert text in _VARIABLES, f"expression evaluator: unknown variable {text!r}"
            return env[text]
        raise AssertionError(f"expression evaluator: unexpected token {text!r} in {if_value!r}")

    value = parse_or()
    assert pos[0] == len(toks), f"expression evaluator: trailing tokens in {if_value!r}"
    if not has_status:
        return truthy(fn("success")) and truthy(value)
    return truthy(value)


# --- the script harness (stub gh, bash -e -o pipefail) ------------------------

_STUB_GH = """#!{python}
import json, os, sys
cfg = json.load(open(os.environ["STUB_GH_CONFIG"]))
argv = sys.argv[1:]
with open(os.environ["STUB_GH_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\\n")
spec = cfg.get(" ".join(argv[:2]), {{}})
out = spec.get("out", "")
if out:
    sys.stdout.write(out + "\\n")
sys.exit(spec.get("rc", 0))
"""

_EXPR_RE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def substitute(script: str, mapping: dict) -> str:
    def repl(m):
        key = m.group(1)
        assert key in mapping, f"harness: unknown ${{{{ {key} }}}} expression in a run block"
        return mapping[key]

    return _EXPR_RE.sub(repl, script)


def step_script(job, step, mapping):
    """The (script, env) pair GitHub would run for `step` of `job`.

    The env is the job's `env:` overlaid by the step's `env:` (step wins), every
    value passed through the same `${{ }}` substitution as the run text. A script
    that reads a variable the workflow sets through `env:` therefore sees it here
    exactly as it would on the runner (the safer pattern: `RESULT: ${{ ... }}`).
    """
    env = {}
    for source in (job.get("env") or {}, step.get("env") or {}):
        for key, value in source.items():
            env[str(key)] = substitute(str(value), mapping)
    return substitute(step["run"], mapping), env


class Outcome:
    def __init__(self, rc, calls, output_lines, stderr):
        self.rc = rc
        self.calls = calls
        self.output_lines = output_lines
        self.stderr = stderr


def run_scripts(scripts, tmp_path, scripted, *, gh_present=True, errexit=True):
    """Run bash scripts in order (stop at the first non-zero) from an empty cwd.

    Each item is a script string (fixed environment only) or a `(script, env)`
    pair from `step_script` (that env merged over the fixed one). The fixed
    GH_TOKEN/GH_REPO are DEFAULTS: a workflow that sets them wins, one that
    forgets them runs with the defaults and must be caught by the env cell.
    The harness's own keys (PATH, HOME, GITHUB_OUTPUT, STUB_*) always win.

    `errexit=True` (the default) runs `bash -e -o pipefail`, as GitHub's `shell: bash`
    does. `errexit=False` runs `bash -o pipefail`, so a script that relies on its OWN
    `set -e` (and not on the runner's flag) is the only thing that can stop it.
    """
    stubdir = tmp_path / "stubbin"
    stubdir.mkdir(exist_ok=True)
    cwd = tmp_path / "emptycwd"
    cwd.mkdir(exist_ok=True)
    assert not any(cwd.iterdir()), "harness: cwd must start empty"
    log = tmp_path / "gh-calls.jsonl"
    log.write_text("")
    cfg = tmp_path / "gh-config.json"
    cfg.write_text(json.dumps(scripted))
    out_file = tmp_path / "github-output"
    out_file.write_text("")
    if gh_present:
        gh = stubdir / "gh"
        gh.write_text(_STUB_GH.format(python=sys.executable))
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        path = f"{stubdir}:/usr/bin:/bin"
    else:
        # a farm of the plain tools the scripts may use, and no gh. /usr/bin
        # itself may hold a real gh, so it is NOT put on PATH here.
        for tool in ("bash", "sh", "date", "tail", "head", "cat", "tr", "sed", "grep", "env"):
            for d in ("/usr/bin", "/bin"):
                src = Path(d) / tool
                if src.exists() and not (stubdir / tool).exists():
                    (stubdir / tool).symlink_to(src)
        assert not (stubdir / "gh").exists()
        path = str(stubdir)
    defaults = {
        "LANG": "C",
        "GH_TOKEN": "dummy-token",
        "GH_REPO": REPOSITORY,
    }
    fixed = {
        "PATH": path,
        "HOME": str(tmp_path),
        "GITHUB_OUTPUT": str(out_file),
        "STUB_GH_CONFIG": str(cfg),
        "STUB_GH_LOG": str(log),
    }
    rc, stderr = 0, ""
    for item in scripts:
        script, step_env = (item, {}) if isinstance(item, str) else item
        env = {**defaults, **step_env, **fixed}
        proc = subprocess.run(
            ["/bin/bash", *(["-e"] if errexit else []), "-o", "pipefail", "-c", script],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        rc, stderr = proc.returncode, proc.stderr
        if rc != 0:
            break
    calls = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return Outcome(rc, calls, out_file.read_text().splitlines(), stderr)


def _sub_map(result="failure"):
    return {
        "github.server_url": SERVER_URL,
        "github.repository": REPOSITORY,
        "github.run_id": RUN_ID,
        "github.token": "dummy-token",
        "needs.pytest.result": result,
    }


def _kinds(calls):
    return [" ".join(c[:2]) for c in calls]


def _arg_after(argv, flag):
    assert flag in argv, f"{flag} not in {argv!r}"
    i = argv.index(flag)
    assert i + 1 < len(argv), f"{flag} has no value in {argv!r}"
    return argv[i + 1]


# --- C1 limits ----------------------------------------------------------------


def test_c1_job_limit_is_195(pytest_job):
    assert pytest_job.get("timeout-minutes") == 195, (
        f"jobs.pytest.timeout-minutes must be 195, got {pytest_job.get('timeout-minutes')!r}"
    )


def test_c1_suite_step_limit_is_180(pytest_job):
    suite = _step_by_id(pytest_job, "suite")
    assert suite.get("timeout-minutes") == 180, (
        f"the `id: suite` step's timeout-minutes must be 180, got {suite.get('timeout-minutes')!r}"
    )


def test_c1_step_limit_is_exactly_15_below_job_limit(pytest_job):
    job = pytest_job.get("timeout-minutes")
    step = _step_by_id(pytest_job, "suite").get("timeout-minutes")
    assert isinstance(job, int) and isinstance(step, int), (
        f"both limits must be set (job={job!r}, suite step={step!r})"
    )
    assert job - step == 15, f"job limit - suite step limit must be 15, got {job} - {step}"


def test_c1_timing_step_carries_no_such_limit(pytest_job):
    """The 180 belongs on the suite step; a limit moved onto `timing` is a different contract."""
    suite = _step_by_id(pytest_job, "suite")
    timing = _step_by_id(pytest_job, "timing")
    assert suite.get("timeout-minutes") == 180, "the suite step must carry the 180-minute limit"
    assert timing.get("timeout-minutes") != 180, "the timing step must not carry the suite limit"


# --- C2 second job structure ---------------------------------------------------


def test_c2_exactly_one_other_job(wf):
    second_job = _second_job(wf)
    assert isinstance(second_job, dict)


def test_c2_needs_is_exactly_pytest(wf):
    second_job = _second_job(wf)
    needs = second_job.get("needs")
    needs = [needs] if isinstance(needs, str) else needs
    assert needs == ["pytest"], f"second job `needs` must be exactly pytest, got {needs!r}"


def test_c2_runs_on_ubuntu_latest(wf):
    second_job = _second_job(wf)
    assert second_job.get("runs-on") == "ubuntu-latest", (
        f"second job runs-on must be ubuntu-latest, got {second_job.get('runs-on')!r}"
    )


def test_c2_has_its_own_short_timeout(wf):
    second_job = _second_job(wf)
    t = second_job.get("timeout-minutes")
    assert isinstance(t, int) and not isinstance(t, bool), (
        f"second job must set its own integer timeout-minutes, got {t!r}"
    )
    assert 0 < t <= 15, f"second job timeout-minutes must be <= 15, got {t}"


def test_c2_does_not_check_out_the_repo(wf):
    second_job = _second_job(wf)
    uses = [str(s.get("uses", "")) for s in second_job.get("steps", [])]
    bad = [u for u in uses if u.startswith("actions/checkout")]
    assert not bad, f"second job must not check out the repo, found {bad}"
    assert second_job.get("steps"), "second job has no steps"


def test_c2_issues_write_is_still_granted(wf):
    second_job = _second_job(wf)
    top = wf.get("permissions")
    assert isinstance(top, dict) and top.get("issues") == "write", (
        f"workflow-level permissions must grant issues: write, got {top!r}"
    )
    if "permissions" in second_job:
        jp = second_job["permissions"]
        assert isinstance(jp, dict) and jp.get("issues") == "write", (
            f"second job narrows permissions without issues: write: {jp!r}"
        )


# --- C3 the gate --------------------------------------------------------------

# (cancelled_run, result, notified, suite_green, runs). `suite_green` is the in-job
# success step's output: 'true' means the suite step was green AND the success step
# ran to its last line; a green suite followed by a failing non-suite step must NOT
# file (R1, #4389 round 4).
GATE_TABLE = [
    (False, "failure", "", "", True),
    (False, "cancelled", "", "", True),
    (False, "failure", "true", "", False),
    (False, "cancelled", "true", "", False),
    (False, "success", "", "", False),
    (True, "cancelled", "", "", False),
    (True, "failure", "", "", False),
    (True, "success", "", "", False),
    (False, "failure", "", "true", False),
    (False, "cancelled", "", "true", False),
    (False, "failure", "true", "true", False),
]


@pytest.mark.parametrize(
    "cancelled_run,result,notified,suite_green,runs",
    GATE_TABLE,
    ids=[
        f"cancelled_run={c}-result={r}-notified={n!r}-suite_green={g!r}-{'RUN' if x else 'SKIP'}"
        for c, r, n, g, x in GATE_TABLE
    ],
)
def test_c3_gate_truth_table(wf, cancelled_run, result, notified, suite_green, runs):
    second_job = _second_job(wf)
    assert "if" in second_job, "second job has no `if:` gate"
    got = evaluate_if(
        second_job["if"],
        cancelled_run=cancelled_run,
        result=result,
        notified=notified,
        suite_green=suite_green,
    )
    assert got is runs, (
        f"gate {second_job['if']!r} with cancelled_run={cancelled_run} result={result!r} "
        f"notified={notified!r} suite_green={suite_green!r}: runs={got}, contract says runs={runs}"
    )


# --- C4 the in-job output --------------------------------------------------------


ON_SUCCESS_NAME = "on success — close any open ci-nightly issue"
ON_SUCCESS_IF = (
    "${{ !cancelled() && steps.suite.outcome == 'success' && steps.timing.outcome == 'success' }}"
)
SUCCESS_ID = "notify_success"


def _notify_step(pytest_job):
    return _step_by_name(pytest_job, ON_FAILURE_NAME)


def _assert_wired(pytest_job):
    """The producer wiring every C4 exec cell relies on (else an `absent` is vacuous)."""
    step = _notify_step(pytest_job)
    assert step.get("if") == ON_FAILURE_IF, f"the notify step's `if:` changed: {step.get('if')!r}"
    sid = step.get("id")
    assert sid, f"the {ON_FAILURE_NAME!r} step has no `id`"
    want = "${{ steps." + sid + ".outputs.notified }}"
    got = (pytest_job.get("outputs") or {}).get("notified")
    assert got == want, f"jobs.pytest.outputs.notified must be {want!r}, got {got!r}"
    return step


def test_c4_notify_step_has_an_id(pytest_job):
    step = _notify_step(pytest_job)
    assert step.get("id"), f"the {ON_FAILURE_NAME!r} step has no `id`"


def test_c4_job_output_is_wired_to_the_step(pytest_job):
    _assert_wired(pytest_job)


def _run_notify(pytest_job, tmp_path, scripted):
    step = _assert_wired(pytest_job)
    return run_scripts([step_script(pytest_job, step, _sub_map())], tmp_path, scripted)


def test_c4_a_issue_created_sets_output(pytest_job, tmp_path):
    r = _run_notify(
        pytest_job,
        tmp_path,
        {"issue list": {"out": "", "rc": 0}, "issue create": {"rc": 0}},
    )
    assert r.rc == 0, f"script exit {r.rc}: {r.stderr}"
    assert "issue create" in _kinds(r.calls), f"no issue create call: {r.calls}"
    assert "notified=true" in r.output_lines, f"notified=true missing: {r.output_lines}"


def test_c4_b_issue_commented_sets_output(pytest_job, tmp_path):
    r = _run_notify(
        pytest_job,
        tmp_path,
        {"issue list": {"out": "7", "rc": 0}, "issue comment": {"rc": 0}},
    )
    assert r.rc == 0, f"script exit {r.rc}: {r.stderr}"
    assert "issue comment" in _kinds(r.calls), f"no issue comment call: {r.calls}"
    assert "notified=true" in r.output_lines, f"notified=true missing: {r.output_lines}"


def test_c4_c_create_fails_no_output(pytest_job, tmp_path):
    r = _run_notify(
        pytest_job,
        tmp_path,
        {"issue list": {"out": "", "rc": 0}, "issue create": {"rc": 1}},
    )
    assert "issue create" in _kinds(r.calls), f"create was never attempted: {r.calls}"
    assert r.rc != 0, "script must fail when gh issue create fails"
    assert "notified=true" not in r.output_lines, f"output written on a failed create: {r.output_lines}"


def test_c4_d_comment_fails_no_output(pytest_job, tmp_path):
    r = _run_notify(
        pytest_job,
        tmp_path,
        {"issue list": {"out": "7", "rc": 0}, "issue comment": {"rc": 1}},
    )
    assert "issue comment" in _kinds(r.calls), f"comment was never attempted: {r.calls}"
    assert r.rc != 0, "script must fail when gh issue comment fails"
    assert "notified=true" not in r.output_lines, f"output written on a failed comment: {r.output_lines}"


def test_c4_e_list_fails_no_output(pytest_job, tmp_path):
    r = _run_notify(pytest_job, tmp_path, {"issue list": {"out": "", "rc": 1}})
    assert "issue list" in _kinds(r.calls), f"list was never attempted: {r.calls}"
    assert r.rc != 0, "script must fail when gh issue list fails"
    assert "notified=true" not in r.output_lines, f"output written on a failed list: {r.output_lines}"


# --- C4b the in-job success step's `suite_green` output (R1) ----------------------


def _assert_success_wired(pytest_job):
    """The producer wiring the C4b exec cells rely on (else an `absent` is vacuous)."""
    step = _step_by_name(pytest_job, ON_SUCCESS_NAME)
    assert step.get("if") == ON_SUCCESS_IF, f"the success step's `if:` changed: {step.get('if')!r}"
    assert step.get("id") == SUCCESS_ID, (
        f"the {ON_SUCCESS_NAME!r} step id must be {SUCCESS_ID!r}, got {step.get('id')!r}"
    )
    want = "${{ steps." + SUCCESS_ID + ".outputs.suite_green }}"
    got = (pytest_job.get("outputs") or {}).get("suite_green")
    assert got == want, f"jobs.pytest.outputs.suite_green must be {want!r}, got {got!r}"
    return step


def test_c4b_success_step_has_the_id_and_the_output_is_wired(pytest_job):
    _assert_success_wired(pytest_job)


def _run_success(pytest_job, tmp_path, scripted):
    step = _assert_success_wired(pytest_job)
    return run_scripts([step_script(pytest_job, step, _sub_map())], tmp_path, scripted)


def test_c4b_a_nothing_to_close_sets_suite_green(pytest_job, tmp_path):
    r = _run_success(pytest_job, tmp_path, {"issue list": {"out": "", "rc": 0}})
    assert r.rc == 0, f"script exit {r.rc}: {r.stderr}"
    assert "suite_green=true" in r.output_lines, f"suite_green=true missing: {r.output_lines}"


def test_c4b_b_comment_and_close_set_suite_green(pytest_job, tmp_path):
    r = _run_success(
        pytest_job,
        tmp_path,
        {"issue list": {"out": "7", "rc": 0}, "issue comment": {"rc": 0}, "issue close": {"rc": 0}},
    )
    assert r.rc == 0, f"script exit {r.rc}: {r.stderr}"
    assert {"issue comment", "issue close"} <= set(_kinds(r.calls)), _kinds(r.calls)
    assert "suite_green=true" in r.output_lines, f"suite_green=true missing: {r.output_lines}"


def test_c4b_c_comment_fails_no_suite_green(pytest_job, tmp_path):
    r = _run_success(
        pytest_job,
        tmp_path,
        {"issue list": {"out": "7", "rc": 0}, "issue comment": {"rc": 1}},
    )
    assert "issue comment" in _kinds(r.calls), f"comment was never attempted: {r.calls}"
    assert r.rc != 0, "script must fail when gh issue comment fails"
    assert "suite_green=true" not in r.output_lines, f"output on a failed comment: {r.output_lines}"


def test_c4b_d_close_fails_no_suite_green(pytest_job, tmp_path):
    r = _run_success(
        pytest_job,
        tmp_path,
        {"issue list": {"out": "7", "rc": 0}, "issue comment": {"rc": 0}, "issue close": {"rc": 1}},
    )
    assert "issue close" in _kinds(r.calls), f"close was never attempted: {r.calls}"
    assert r.rc != 0, "script must fail when gh issue close fails"
    assert "suite_green=true" not in r.output_lines, f"output on a failed close: {r.output_lines}"


def test_c4b_e_list_fails_no_suite_green(pytest_job, tmp_path):
    r = _run_success(pytest_job, tmp_path, {"issue list": {"out": "", "rc": 1}})
    assert "issue list" in _kinds(r.calls), f"list was never attempted: {r.calls}"
    assert r.rc != 0, "script must fail when gh issue list fails"
    assert "suite_green=true" not in r.output_lines, f"output on a failed list: {r.output_lines}"


def test_c4b_prose_lost_runner_after_green_suite_files(raw):
    """PROSE PIN: a comment block explains that a lost runner between a green suite
    and the last line of the success step leaves the output empty, so the second job
    files, and that this is right because the night did not finish."""
    hits = [
        t
        for _, t in _comment_blocks(raw)
        if re.search(r"lost runner", t, re.I)
        and re.search(r"green suite", t, re.I)
        and re.search(r"did not finish", t, re.I)
        and re.search(r"\bfiles\b", t, re.I)
    ]
    assert hits, "no comment block says lost runner / green suite / did not finish / files"


# --- C5 the second job's script --------------------------------------------------


def _second_scripts(second_job, result):
    scripts = [
        step_script(second_job, s, _sub_map(result)) for s in second_job["steps"] if "run" in s
    ]
    assert scripts, "second job has no `run:` step"
    return scripts


def test_c5_gh_credentials_are_in_the_second_jobs_env(wf):
    second_job = _second_job(wf)
    keys = set((second_job.get("env") or {}).keys())
    for s in second_job["steps"]:
        if "run" in s:
            keys |= set((s.get("env") or {}).keys())
    assert {"GH_TOKEN", "GH_REPO"} <= keys, f"second job env lacks GH_TOKEN/GH_REPO: {sorted(keys)}"


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_c5_a_no_open_issue_creates_label_then_one_issue(wf, tmp_path, result):
    second_job = _second_job(wf)
    r = run_scripts(
        _second_scripts(second_job, result),
        tmp_path,
        {"issue list": {"out": "", "rc": 0}},
    )
    assert r.rc == 0, f"script exit {r.rc}: {r.stderr}"
    kinds = _kinds(r.calls)
    assert "label create" in kinds, f"no label create call: {kinds}"
    assert kinds.count("issue create") == 1, f"issue create must be called once: {kinds}"
    assert "issue comment" not in kinds, f"comment called with no open issue: {kinds}"
    assert kinds.index("label create") < kinds.index("issue create"), (
        f"label must be created BEFORE the issue: {kinds}"
    )
    label = r.calls[kinds.index("label create")]
    assert "ci-nightly" in label and "--force" in label, f"label create args: {label}"
    create = r.calls[kinds.index("issue create")]
    assert _arg_after(create, "--label") == "ci-nightly", f"issue create args: {create}"
    body = _arg_after(create, "--body")
    assert RUN_URL in body, f"body lacks the run URL {RUN_URL}: {body!r}"
    assert result in body, f"body lacks the result string {result!r}: {body!r}"
    assert "no log tail" in body.lower(), f"body lacks 'no log tail': {body!r}"
    title = _arg_after(create, "--title")
    assert TITLE_RE.match(title), f"title {title!r} does not match {TITLE_RE.pattern}"


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_c5_b_open_issue_gets_a_comment_not_a_new_issue(wf, tmp_path, result):
    second_job = _second_job(wf)
    r = run_scripts(
        _second_scripts(second_job, result),
        tmp_path,
        {"issue list": {"out": "7", "rc": 0}},
    )
    assert r.rc == 0, f"script exit {r.rc}: {r.stderr}"
    kinds = _kinds(r.calls)
    assert "issue comment" in kinds, f"no issue comment call: {kinds}"
    assert r.calls[kinds.index("issue comment")][:3] == ["issue", "comment", "7"], (
        f"comment must target issue 7: {r.calls}"
    )
    assert "issue create" not in kinds, f"issue create called with an open issue: {kinds}"
    assert "label create" in kinds, f"label must still be created: {kinds}"
    assert kinds.index("label create") < kinds.index("issue comment"), (
        f"label must be created before the comment: {kinds}"
    )
    comment = r.calls[kinds.index("issue comment")]
    body = _arg_after(comment, "--body")
    assert RUN_URL in body, f"comment body lacks the run URL {RUN_URL}: {body!r}"
    assert result in body, f"comment body lacks the result string {result!r}: {body!r}"
    assert "no log tail" in body.lower(), f"comment body lacks 'no log tail': {body!r}"


def test_c5_c_label_failure_stops_before_any_issue_call(wf, tmp_path):
    second_job = _second_job(wf)
    r = run_scripts(
        _second_scripts(second_job, "failure"),
        tmp_path,
        {"label create": {"rc": 1}},
    )
    kinds = _kinds(r.calls)
    assert "label create" in kinds, f"label create was never attempted: {kinds}"
    assert r.rc != 0, "script must exit non-zero when the label create fails"
    assert not [k for k in kinds if k.startswith("issue")], (
        f"an issue call ran after the label create failed: {kinds}"
    )


def test_c5_d_gh_absent_exits_non_zero(wf, tmp_path):
    second_job = _second_job(wf)
    r = run_scripts(
        _second_scripts(second_job, "failure"), tmp_path, {}, gh_present=False
    )
    assert r.rc != 0, "script must not exit 0 when gh is absent"
    assert r.calls == []


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_c5_e1_list_fails_files_a_new_issue_anyway(wf, tmp_path, result):
    """R2: a failed list read must not swallow the report. The script files a NEW issue
    (never a comment: there is no number) whose body says the list read failed and an
    older open issue may exist. Run WITHOUT the harness's `-e`: the script's own
    handling is what is under test."""
    second_job = _second_job(wf)
    r = run_scripts(
        _second_scripts(second_job, result),
        tmp_path,
        {"issue list": {"out": "", "rc": 1}, "issue create": {"rc": 0}},
        errexit=False,
    )
    kinds = _kinds(r.calls)
    assert "issue list" in kinds, f"list was never attempted: {kinds}"
    assert r.rc == 0, f"script exit {r.rc}: {r.stderr}"
    assert kinds.count("issue create") == 1, f"exactly one issue create expected: {kinds}"
    assert "issue comment" not in kinds, f"comment called after a failed list: {kinds}"
    body = _arg_after(r.calls[kinds.index("issue create")], "--body")
    low = body.lower()
    assert "list read failed" in low, f"body lacks 'list read failed': {body!r}"
    assert "older" in low, f"body lacks 'older': {body!r}"
    assert RUN_URL in body, f"body lacks the run URL {RUN_URL}: {body!r}"
    assert result in body, f"body lacks the result string {result!r}: {body!r}"
    assert "no log tail" in low, f"body lacks 'no log tail': {body!r}"


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_c5_e2_list_and_create_both_fail_exits_non_zero(wf, tmp_path, result):
    second_job = _second_job(wf)
    r = run_scripts(
        _second_scripts(second_job, result),
        tmp_path,
        {"issue list": {"out": "", "rc": 1}, "issue create": {"rc": 1}},
        errexit=False,
    )
    kinds = _kinds(r.calls)
    assert "issue create" in kinds, f"create was never attempted: {kinds}"
    assert r.rc != 0, "script must exit non-zero when the list AND the create fail"


def test_c5_f_create_fails_exits_non_zero(wf, tmp_path):
    second_job = _second_job(wf)
    r = run_scripts(
        _second_scripts(second_job, "failure"),
        tmp_path,
        {"issue list": {"out": "", "rc": 0}, "issue create": {"rc": 1}},
    )
    kinds = _kinds(r.calls)
    assert "issue create" in kinds, f"create was never attempted: {kinds}"
    assert r.rc != 0, "script must exit non-zero when gh issue create fails"


def test_c5_g_comment_fails_exits_non_zero(wf, tmp_path):
    second_job = _second_job(wf)
    r = run_scripts(
        _second_scripts(second_job, "failure"),
        tmp_path,
        {"issue list": {"out": "7", "rc": 0}, "issue comment": {"rc": 1}},
    )
    kinds = _kinds(r.calls)
    assert "issue comment" in kinds, f"comment was never attempted: {kinds}"
    assert r.rc != 0, "script must exit non-zero when gh issue comment fails"
    assert "issue create" not in kinds, f"issue create called with an open issue: {kinds}"


def test_s2_list_filter_keeps_the_empty_guard(wf):
    """Without `// empty` an empty list prints `null` and `gh issue comment null` fails."""
    second_job = _second_job(wf)
    runs = "\n".join(s["run"] for s in second_job["steps"] if "run" in s)
    assert ".[0].number // empty" in runs, "second job's list filter lost `// empty`"


def test_s3_second_job_is_not_continue_on_error(wf):
    second_job = _second_job(wf)
    assert second_job.get("continue-on-error") in (None, False), (
        f"second job must not be continue-on-error: {second_job.get('continue-on-error')!r}"
    )


# --- C6 prose pins (loose regexes on YAML comments) -------------------------------


def _comment_blocks(raw):
    """Contiguous runs of `#` comment lines, each joined into one whitespace-collapsed string.

    Returns (start_line_index, text) pairs. A block spans wrapped lines so a
    sentence broken across two comment lines still counts.
    """
    blocks, cur, start = [], [], None
    for i, line in enumerate(raw.splitlines()):
        s = line.strip()
        if s.startswith("#"):
            if not cur:
                start = i
            cur.append(s.lstrip("#").strip())
        elif cur:
            blocks.append((start, re.sub(r"\s+", " ", " ".join(cur))))
            cur = []
    if cur:
        blocks.append((start, re.sub(r"\s+", " ", " ".join(cur))))
    return blocks


def test_c6_prose_extra_update_is_accepted(raw):
    """PROSE PIN: a comment block says a lost runner between create and output gives an
    extra update and that this is accepted."""
    hits = [
        t
        for _, t in _comment_blocks(raw)
        if re.search(r"extra update", t, re.I) and re.search(r"accepted", t, re.I)
    ]
    assert hits, "no comment mentions both 'extra update' and 'accepted'"


def test_c6_prose_output_is_set_only_after_success(raw):
    """PROSE PIN: a comment adjacent to the output step (inside it, or directly above
    it) says the output is set only after the issue call returned success."""
    lines = raw.splitlines()
    starts = [i for i, ln in enumerate(lines) if ON_FAILURE_NAME in ln and "name:" in ln]
    assert len(starts) == 1, f"expected one {ON_FAILURE_NAME!r} step header, got {len(starts)}"
    begin = starts[0]
    end = next(
        (i for i in range(begin + 1, len(lines)) if re.match(r"\s{6}- name:", lines[i])),
        len(lines),
    )
    region_lines = lines[begin:end]
    region_text = re.sub(
        r"\s+",
        " ",
        " ".join(ln.strip().lstrip("#").strip() for ln in region_lines if ln.strip().startswith("#")),
    )
    above = [t for st, t in _comment_blocks(raw) if st < begin and _block_ends_at(lines, st, begin)]
    candidates = [region_text] + above
    assert any(re.search(r"only after", t, re.I) for t in candidates), (
        "no comment adjacent to the output step says 'only after'"
    )


def _block_ends_at(lines, start, step_line):
    """True when the comment block starting at `start` runs straight into `step_line`."""
    i = start
    while i < step_line and lines[i].strip().startswith("#"):
        i += 1
    return i == step_line


# --- evaluator self-tests (the instrument; independent of nightly.yml) -----------

_REAL_GATE = (
    "${{ !cancelled() && needs.pytest.result != 'success' && needs.pytest.outputs.notified != 'true'"
    " && needs.pytest.outputs.suite_green != 'true' }}"
)


def test_evaluator_selftest_real_gate_shape():
    for c, r, n, g, runs in GATE_TABLE:
        got = evaluate_if(_REAL_GATE, cancelled_run=c, result=r, notified=n, suite_green=g)
        assert got is runs


def test_evaluator_selftest_suite_green_is_a_known_variable_and_defaults_empty():
    gate = "${{ needs.pytest.outputs.suite_green != 'true' }}"
    assert evaluate_if(gate, cancelled_run=False, result="success", notified="") is True
    assert (
        evaluate_if(gate, cancelled_run=False, result="success", notified="", suite_green="true")
        is False
    )


def test_evaluator_selftest_implicit_success_and_always():
    plain = "${{ needs.pytest.result != 'success' }}"
    assert evaluate_if(plain, cancelled_run=False, result="failure", notified="") is False
    assert evaluate_if("always()", cancelled_run=True, result="cancelled", notified="") is True


def test_evaluator_selftest_unknown_tokens_fail_loudly():
    for bad in (
        "${{ !cancelled() && github.event_name == 'x' }}",
        "${{ !cancelled() && contains(needs.pytest.result, 'x') }}",
        "${{ !cancelled() & true }}",
        "${{ foo() }}",
    ):
        with pytest.raises(AssertionError):
            evaluate_if(bad, cancelled_run=False, result="failure", notified="")


# --- harness self-tests: job and step env (synthetic jobs, no nightly.yml) -------

_ENV_PROBE = 'echo "a=$A b=$B c=$C tok=$GH_TOKEN repo=$GH_REPO" >> "$GITHUB_OUTPUT"'


def _probe(job, tmp_path):
    tmp_path.mkdir(exist_ok=True)
    step = job["steps"][0]
    r = run_scripts([step_script(job, step, _sub_map("cancelled"))], tmp_path, {})
    assert r.rc == 0, r.stderr
    assert len(r.output_lines) == 1, r.output_lines
    return r.output_lines[0]


def test_harness_selftest_job_and_step_env_are_applied_and_substituted(tmp_path):
    job = {
        "env": {"A": "job-${{ github.repository }}", "C": "from-job"},
        "steps": [
            {
                "run": _ENV_PROBE,
                "env": {"B": "${{ needs.pytest.result }}", "C": "from-step"},
            }
        ],
    }
    line = _probe(job, tmp_path / "p")
    assert line.startswith(f"a=job-{REPOSITORY} b=cancelled c=from-step "), line


def test_harness_selftest_gh_credentials_are_defaults_only(tmp_path):
    bare = {"steps": [{"run": _ENV_PROBE}]}
    line = _probe(bare, tmp_path / "bare")
    assert line.endswith(f"tok=dummy-token repo={REPOSITORY}"), line
    own = {
        "env": {"GH_REPO": "other/repo"},
        "steps": [{"run": _ENV_PROBE, "env": {"GH_TOKEN": "${{ github.token }}-x"}}],
    }
    line = _probe(own, tmp_path / "own")
    assert line.endswith("tok=dummy-token-x repo=other/repo"), line


def test_harness_selftest_unknown_env_expression_fails_loudly():
    job = {"steps": [{"run": "true", "env": {"X": "${{ secrets.nope }}"}}]}
    with pytest.raises(AssertionError):
        step_script(job, job["steps"][0], _sub_map())
