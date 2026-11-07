"""Contract-first tests for tools/name_leak_gate.py (issue #4429).

The gate's decision is a pure function over five booleans/strings, so the
contract is stated here BEFORE the implementation exists. Every row of the
corrected contract table is asserted, every wildcard is expanded to all of its
combinations, and the named invariant is asserted exhaustively:

    conclusion == "success" if and only if the event is trusted AND the roster
    was present AND the attestation matched. There is no success case in which
    the gate did not run.

A fork pull request is the one row that reports a distinct status name,
containing the literal token ``NOT SCANNED``, with the message wording
``not scanned, roster unavailable``. It is a failure, and it must never be
listed as a required check.

Discrimination: inverting any single row's expected conclusion turns exactly
that row's parametrized case red (the invariant test stays green), because each
row is one independently identified case.
"""

import ast
import importlib.util
import itertools
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "name_leak_gate.py"
WORKFLOW = REPO / ".github" / "workflows" / "name-leak-gate.yml"

# The reason strings are pinned here as literals, NOT read from the module: a
# test that imported them from the implementation could not notice a change to
# one, and the reason is what an admin reads in the failure issue to know which
# remedy to apply.
REASON_FORK = "not scanned, roster unavailable"
REASON_ROSTER_ABSENT = "roster absent - the NAME_LEAK_ROSTER secret is unset"
REASON_ATTESTATION_ABSENT = (
    "attestation absent or invalid - .github/name-leak-roster.attestation.json"
)
REASON_ATTESTATION_MISMATCH = (
    "roster sha256 does not match the committed attestation"
)
REASON_SUCCESS = "roster present and the whole-file sha256 matches the attestation"


def _reason_for_unknown_event(event):
    return f"unknown event {event!r} - failing closed"


def _load():
    spec = importlib.util.spec_from_file_location("name_leak_gate", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()
decide = mod.decide


def _cases():
    """Every row of the corrected contract table, wildcards expanded.

    Each row pins its own ``reason``: the failure reason is the remedy an admin
    reads in the issue body, so a swap between two failure branches must go red
    here even when both conclusions are "failure".
    """
    cases = []

    def add(
        event,
        is_fork,
        roster,
        att_present,
        att_matches,
        conclusion,
        not_scanned,
        reason,
    ):
        cases.append(
            pytest.param(
                event,
                is_fork,
                roster,
                att_present,
                att_matches,
                conclusion,
                not_scanned,
                reason,
                id=(
                    f"{event or 'empty'}-fork={is_fork}-roster={roster}"
                    f"-attest={att_present}-match={att_matches}"
                ),
            )
        )

    # Rows 2 and 6: roster absent -> failure for every attestation combination.
    for att_present, att_matches in itertools.product([False, True], repeat=2):
        add(
            "push", False, False, att_present, att_matches, "failure", False,
            REASON_ROSTER_ABSENT,
        )
        add(
            "pull_request", False, False, att_present, att_matches, "failure", False,
            REASON_ROSTER_ABSENT,
        )

    # Rows 4 and 8: roster present, attestation absent -> failure regardless of
    # whether the (absent) attestation is reported as matching.
    for att_matches in (False, True):
        add(
            "push", False, True, False, att_matches, "failure", False,
            REASON_ATTESTATION_ABSENT,
        )
        add(
            "pull_request", False, True, False, att_matches, "failure", False,
            REASON_ATTESTATION_ABSENT,
        )

    # Rows 1 and 5: the only success rows for a trusted, non-fork event.
    add("push", False, True, True, True, "success", False, REASON_SUCCESS)
    add("pull_request", False, True, True, True, "success", False, REASON_SUCCESS)

    # `is_fork` is IGNORED for `push`: a push has no head repository, so the
    # flag cannot mean anything there and `decide()` never consults it. Pinned
    # because the doc's contract table spelled these rows `is_fork=False`, which
    # reads as a constraint the code does not impose — a reader implementing
    # from that spelling would write a check the gate does not have.
    add("push", True, True, True, True, "success", False, REASON_SUCCESS)
    add("push", True, False, False, False, "failure", False, REASON_ROSTER_ABSENT)

    # Rows 3 and 7: roster present, attestation present but the hash disagrees.
    add(
        "push", False, True, True, False, "failure", False,
        REASON_ATTESTATION_MISMATCH,
    )
    add(
        "pull_request", False, True, True, False, "failure", False,
        REASON_ATTESTATION_MISMATCH,
    )

    # Row 9: a fork PR is a failure for EVERY other input, and it is the only
    # case whose status name must carry the literal token NOT SCANNED.
    for roster, att_present, att_matches in itertools.product([False, True], repeat=3):
        add(
            "pull_request", True, roster, att_present, att_matches, "failure", True,
            REASON_FORK,
        )

    # Row 10: an unknown event fails closed, never success. merge_group is
    # deliberately included: this repo has no merge queue, so it must not be a
    # trusted event even though it once was in an earlier draft of the spec.
    for event in ("merge_group", "workflow_dispatch", "schedule", "", "Push"):
        add(
            event, False, True, True, True, "failure", False,
            _reason_for_unknown_event(event),
        )

    return cases


@pytest.mark.parametrize(
    "event,is_fork,roster,att_present,att_matches,expected_conclusion,"
    "not_scanned,expected_reason",
    _cases(),
)
def test_contract_row(
    event,
    is_fork,
    roster,
    att_present,
    att_matches,
    expected_conclusion,
    not_scanned,
    expected_reason,
):
    outcome = decide(event, is_fork, roster, att_present, att_matches)
    assert outcome.conclusion == expected_conclusion
    assert ("NOT SCANNED" in outcome.status_name) is not_scanned
    # The EXACT status name per row, not merely the NOT SCANNED token: the
    # workflow emits this string as the checks-list context, so the success
    # return in particular must be the constant and nothing else.
    assert outcome.status_name == (
        mod.STATUS_NOT_SCANNED if not_scanned else mod.STATUS_SCANNED
    )
    assert outcome.reason == expected_reason
    assert outcome.reason, "every outcome must carry a non-empty reason"


# Events the invariant loop ranges over: the two trusted ones plus shapes that
# must fail closed. Includes the corrected-out merge_group.
_INVARIANT_EVENTS = ["push", "pull_request", "merge_group", "workflow_dispatch", "schedule", ""]


def _trusted(event, is_fork):
    """A trusted event is one where the gate can actually run with the secret.

    A pull request from a fork is not trusted (no secret reaches it); a push to
    the default branch is. `is_fork` has no meaning outside pull_request.
    """
    return event == "push" or (event == "pull_request" and not is_fork)


def test_invariant_success_iff_trusted_roster_present_and_attestation_matched():
    for event, is_fork, roster, att_present, att_matches in itertools.product(
        _INVARIANT_EVENTS, [False, True], [False, True], [False, True], [False, True]
    ):
        outcome = decide(event, is_fork, roster, att_present, att_matches)
        expected_success = (
            _trusted(event, is_fork) and roster and att_present and att_matches
        )
        assert (outcome.conclusion == "success") is expected_success, (
            f"event={event!r} is_fork={is_fork} roster={roster} "
            f"attest_present={att_present} att_matches={att_matches}: "
            f"got {outcome.conclusion!r} ({outcome.reason})"
        )


def test_no_success_without_the_gate_running():
    """The strong half of the invariant, asserted by name.

    If the conclusion is success then the event was trusted, the roster was
    present, and the attestation matched. A fork PR is never a success.
    """
    for event, is_fork, roster, att_present, att_matches in itertools.product(
        _INVARIANT_EVENTS, [False, True], [False, True], [False, True], [False, True]
    ):
        outcome = decide(event, is_fork, roster, att_present, att_matches)
        if outcome.conclusion != "success":
            continue
        assert _trusted(event, is_fork), (event, is_fork)
        assert roster and att_present and att_matches, (event, is_fork)
        assert not (event == "pull_request" and is_fork)
        assert "NOT SCANNED" not in outcome.status_name


def test_fork_pull_request_reason_uses_the_required_wording():
    outcome = decide("pull_request", True, True, True, True)
    assert outcome.conclusion == "failure"
    assert outcome.reason == "not scanned, roster unavailable"
    assert "NOT SCANNED" in outcome.status_name


def test_status_names_are_exposed_as_constants():
    assert mod.STATUS_SCANNED == "name-leak-gate"
    assert "NOT SCANNED" in mod.STATUS_NOT_SCANNED


def _status_name_literals(expression):
    """The two status names an ``&& 'A' || 'B'`` ternary selects between.

    GitHub expressions spell strings with single quotes. Both the job's display
    name and the decide step's ``EXPECTED_STATUS_NAME`` end in the same ternary
    (fork => first literal, otherwise => second), so its final two quoted
    literals are exactly the strings the workflow emits as the status context.
    """
    literals = re.findall(r"'([^']*)'", expression)
    assert len(literals) >= 2, expression
    return literals[-2:]


def test_workflow_uses_the_same_status_names():
    """The pure function's status names and the workflow's job name must agree.

    GitHub reports the job's display name as the status context, so the string
    the contract promises has to be the string the workflow emits. This parses
    the workflow and compares the literals EXACTLY: a substring check passed
    even when both names were renamed, because `name-leak-gate` also appears in
    the filename, the workflow `name:` and the comments.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["name-leak-gate"]

    expected_literals = [mod.STATUS_NOT_SCANNED, mod.STATUS_SCANNED]

    # The status context in the checks list.
    assert _status_name_literals(job["name"]) == expected_literals

    # The decide step's runtime cross-check, which fails the step on drift.
    decide_step = next(step for step in job["steps"] if step.get("id") == "decide")
    assert (
        _status_name_literals(decide_step["env"]["EXPECTED_STATUS_NAME"])
        == expected_literals
    )


def test_module_is_pure_no_io_and_no_environment():
    """decide() may not read I/O or the environment: it takes all inputs as args."""
    source = TOOL.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_modules = {"os", "subprocess", "sys", "io", "pathlib", "shutil", "builtins"}
    forbidden_calls = {"open", "getenv", "environ", "system", "popen", "read_text"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden_modules, alias.name
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in forbidden_modules, node.module
        elif isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            assert name not in forbidden_calls, name
        elif isinstance(node, ast.Attribute):
            # A bare `os.environ` reference (not a call) is just as forbidden.
            assert node.attr != "environ", "decide() must not read the environment"

    # No environment read can hide behind a name the AST walk above misses.
    assert "os.environ" not in source
    assert "os.getenv" not in source


# ── the job's executable surface (review round 3) ────────────────────────────
#
# The job that holds NAME_LEAK_ROSTER executes code. On a same-repo pull
# request the branch is contributor-controlled, so every byte that runs there
# has to come from the base commit — otherwise the documented re-push path hands
# the roster to whoever wrote the branch. These three tests pin that property.
# The third is behavioural: it proves the shadowing hazard is real, so removing
# `-P` reddens a test rather than passing quietly.


def _run_blocks(workflow):
    """Every shell `run:` body in the gate job, in order."""
    job = workflow["jobs"]["name-leak-gate"]
    return [step["run"] for step in job["steps"] if isinstance(step.get("run"), str)]


def test_every_python_invocation_is_isolated_from_the_checkout():
    """Each `python3` call carries `-P`, so no committed module can be imported.

    Measured: without `-P`, `sys.path[0]` is `''` for a `-` script, so a
    `json.py` at the repository root is imported in place of the standard
    library one — inside the step that has the roster in its environment. With
    `-P` the import resolves to the standard library. A list of forbidden
    filenames would be a hole for every name nobody thought of; the flag is the
    property.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    invocations = [
        line.strip()
        for block in _run_blocks(workflow)
        for line in block.splitlines()
        if "python3" in line and not line.strip().startswith("#")
    ]
    assert len(invocations) == 5, invocations
    for line in invocations:
        assert re.search(r"python3\s+-P\b", line), line


def test_the_executed_code_is_read_from_a_protected_tree():
    """The executed `tools/` file comes from a tree no PR author can write.

    Base commit first, then the protected default branch, and the pull request's
    own copy only when neither has it. The branch contributes data (a diff, an
    attestation); if the code were read from the checkout, re-pushing an outside
    branch would run that author's code with the roster secret.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["name-leak-gate"]

    decide = next(step for step in job["steps"] if step.get("id") == "decide")
    assert "BASE_SHA" in decide["env"]
    run = decide["run"]

    # The chain, IN ORDER: base, then the protected default branch, then the
    # pull request's own copy. The second link is what makes the bootstrap claim
    # true — without it a PR into `main` (whose base predates the tool) sits on
    # the bootstrap path indefinitely, which is the review finding this pins.
    chain = "for ref in (base_sha, default_ref)"
    assert chain in run
    assert "DEFAULT_BRANCH" in decide["env"]
    assert '["git", "fetch", "--no-tags", "origin", default_branch]' in run
    assert run.index(chain) < run.index("pull request checkout (BOOTSTRAP)"), (
        "the checkout fallback must come AFTER the base and default-branch links"
    )

    # The bootstrap branch may be entered only on POSITIVE knowledge: both
    # protected refs must RESOLVE before either is read. An unresolvable ref is
    # a fact about the clone, not about the tree, and must fail closed rather
    # than fall through to the pull request's copy.
    assert '["git", "cat-file", "-e", f"{ref}^{{commit}}"]' in run
    assert "does not resolve in this" in run
    assert run.index("does not resolve in this") < run.index(
        "pull request checkout (BOOTSTRAP)"
    ), "the fail-closed check must run BEFORE the bootstrap fallback"

    # The seat's positive check has to be readable from the CLI, so the tree is
    # printed on stdout and not only to the step summary.
    assert "code read from {code_source}" in run

    # The old shape — exec the checkout's copy directly — must be gone.
    assert '"name_leak_gate", "tools/name_leak_gate.py"' not in run

    for step in job["steps"]:
        if isinstance(step.get("run"), str) and "name_leak_warn_base.py" in step["run"]:
            assert (
                'git show "${BASE_SHA}:tools/name_leak_warn.py"' in step["run"]
            ), step.get("name")


def test_isolated_mode_is_what_stops_a_shadowing_module(tmp_path):
    """Behavioural control for the flag the two tests above require.

    Builds the hazard and shows both directions: without `-P` a `json.py` beside
    the interpreter's cwd is imported (the shadow RAN), and with `-P` it is not.
    The negative arm is the point — it proves the positive arm's input would
    catch a regression, rather than passing for want of a hazard.
    """
    import subprocess
    import sys

    shadow = tmp_path / "json.py"
    marker = tmp_path / "shadow-ran"
    shadow.write_text(
        f'open({str(marker)!r}, "w").write("ran")\n'
        "raise SystemExit('shadow executed')\n",
        encoding="utf-8",
    )
    probe = "import json,sys;print(getattr(json,'__file__',''))"

    shadowed = subprocess.run(
        [sys.executable, "-c", probe], cwd=tmp_path, capture_output=True, text=True
    )
    assert marker.exists(), (
        "the negative control failed: without -P the shadow module was NOT "
        f"imported, so this test cannot detect a regression. stdout={shadowed.stdout!r} "
        f"stderr={shadowed.stderr!r}"
    )
    marker.unlink()

    isolated = subprocess.run(
        [sys.executable, "-P", "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert isolated.returncode == 0, isolated.stderr
    assert not marker.exists(), (
        "-P did not keep the checkout off sys.path: the shadow module ran"
    )
    assert str(tmp_path) not in isolated.stdout, isolated.stdout


# ── the I/O harness, executed (not read) ─────────────────────────────────────
#
# Every one of the four review blockers in this PR lived in the workflow's
# decide step — the heredoc — and none of them were reachable by a test of
# `decide()`. These cells extract that heredoc and RUN it against a scratch git
# repo with a real remote, one run per path of the code-source chain. They
# assert the line the operator reads, which is the thing that has to be right.


def _decide_script() -> str:
    """The decide step's Python, extracted from the workflow verbatim."""
    import textwrap

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        s for s in workflow["jobs"]["name-leak-gate"]["steps"] if s.get("id") == "decide"
    )
    lines = step["run"].splitlines()
    start = next(i for i, l in enumerate(lines) if "<<'PY'" in l)
    end = max(i for i, l in enumerate(lines) if l.strip() == "PY")
    return textwrap.dedent("\n".join(lines[start + 1 : end]))


_STUB_TOOL = (
    "SUCCESS='success'\nFAILURE='failure'\n"
    "STATUS_SCANNED='name-leak-gate'\n"
    "STATUS_NOT_SCANNED='name-leak-gate (NOT SCANNED - fork PR)'\n"
    "TRUSTED_EVENTS=frozenset({'push','pull_request'})\nFORK_REASON='not scan'\n"
    "from dataclasses import dataclass\n"
    "@dataclass(frozen=True)\nclass Outcome:\n"
    "    conclusion: str\n    status_name: str\n    reason: str\n"
    "def decide(e,f,r,a,am):\n"
    "    if e=='pull_request' and f: return Outcome(FAILURE,STATUS_NOT_SCANNED,FORK_REASON)\n"
    "    if e not in TRUSTED_EVENTS: return Outcome(FAILURE,STATUS_SCANNED,'x')\n"
    "    return Outcome(SUCCESS,STATUS_SCANNED,'ok')\n"
)


@pytest.fixture
def gate_repo(tmp_path):
    """A work repo on `dev` with a bare remote, base commit lacking the tool."""
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")

    def g(*args, cwd, check=True):
        return subprocess.run(
            ("git",) + args, cwd=cwd, check=check, capture_output=True, text=True
        )

    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    g("init", "-q", "--bare", str(remote), cwd=tmp_path)
    g("init", "-q", str(work), cwd=tmp_path)
    g("config", "user.email", "t@example.invalid", cwd=work)
    g("config", "user.name", "t", cwd=work)
    g("remote", "add", "origin", str(remote), cwd=work)
    (work / "tools").mkdir()
    (work / "tools" / "name_leak_warn.py").write_text("# warn tool\n")
    (work / ".github").mkdir()
    (work / ".github" / "name-leak-roster.attestation.json").write_text(
        '{"roster_format": 1, "pattern_count": 1, "roster_sha256": "%s"}' % ("0" * 64)
    )
    g("add", "-A", cwd=work)
    g("commit", "-qm", "base without the gate tool", cwd=work)
    base_sha = g("rev-parse", "HEAD", cwd=work).stdout.strip()
    g("branch", "-M", "dev", cwd=work)
    g("push", "-q", "origin", "dev", cwd=work)
    g("fetch", "-q", "origin", "dev", cwd=work)
    return work, base_sha, g


def _run_gate(work, base_sha, tmp_path, default_branch="dev"):
    """Run the extracted decide step; return (returncode, combined output)."""
    import subprocess

    out_file = tmp_path / "gh_output.txt"
    summary = tmp_path / "gh_summary.md"
    out_file.write_text("")
    summary.write_text("")
    env = dict(os.environ)
    env.update(
        EVENT_NAME="pull_request",
        PR_IS_FORK="false",
        EXPECTED_STATUS_NAME="name-leak-gate",
        NAME_LEAK_ROSTER="somepattern\n",
        ATTESTATION_PATH=".github/name-leak-roster.attestation.json",
        BASE_SHA=base_sha,
        DEFAULT_BRANCH=default_branch,
        GITHUB_OUTPUT=str(out_file),
        GITHUB_STEP_SUMMARY=str(summary),
        RUNNER_TEMP=str(tmp_path),
    )
    p = subprocess.run(
        [sys.executable, "-P", str(tmp_path / "decide.py")],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )
    return p.returncode, p.stdout + p.stderr


def _code_line(output):
    for line in output.splitlines():
        if "code read from" in line:
            return line
    return ""


def _commit_tool_and_push(work, g):
    """Put the tool on `dev`, push, fetch. Returns the commit that NOW has it."""
    (work / "tools" / "name_leak_gate.py").write_text(_STUB_TOOL)
    g("add", "-A", cwd=work)
    g("commit", "-qm", "add the gate tool", cwd=work)
    g("push", "-q", "origin", "dev", cwd=work)
    g("fetch", "-q", "origin", "dev", cwd=work)
    return g("rev-parse", "HEAD", cwd=work).stdout.strip()


def test_harness_reads_the_tool_from_the_base_when_it_is_there(gate_repo, tmp_path):
    """Link 1: the base commit carries the tool, so the base is what runs."""
    work, _base, g = gate_repo
    (tmp_path / "decide.py").write_text(_decide_script())
    base_with_tool = _commit_tool_and_push(work, g)
    rc, out = _run_gate(work, base_with_tool, tmp_path)
    assert rc == 0, out
    assert f"code read from {base_with_tool}" in _code_line(out), out


def test_harness_falls_back_to_the_default_branch(gate_repo, tmp_path):
    """Link 2: the base predates the tool, so the protected branch supplies it.

    This is the case a pull request into `main` takes, and it is the one whose
    absence let the bootstrap path stay open forever.
    """
    work, base_without_tool, g = gate_repo
    (tmp_path / "decide.py").write_text(_decide_script())
    _commit_tool_and_push(work, g)
    rc, out = _run_gate(work, base_without_tool, tmp_path)
    assert rc == 0, out
    assert "code read from origin/dev" in _code_line(out), out
    assert "BOOTSTRAP" not in out


def test_harness_bootstraps_only_when_both_protected_refs_lack_the_tool(
    gate_repo, tmp_path
):
    """Link 3, and it is entered only on positive knowledge.

    The tool exists nowhere on a protected ref (it is in the working tree only),
    which is the genuine pre-merge shape. The run must say BOOTSTRAP and name
    the checkout, so an operator can see it took the unprotected path.
    """
    work, base_without_tool, _g = gate_repo
    (tmp_path / "decide.py").write_text(_decide_script())
    (work / "tools" / "name_leak_gate.py").write_text(_STUB_TOOL)  # uncommitted
    rc, out = _run_gate(work, base_without_tool, tmp_path)
    assert rc == 0, out
    assert "BOOTSTRAP" in out
    assert "code read from pull request checkout (BOOTSTRAP)" in _code_line(out), out


def test_harness_fails_closed_when_the_default_ref_does_not_resolve(
    gate_repo, tmp_path
):
    """An unreadable ref is a fact about the clone, never about the tree."""
    work, base_without_tool, _g = gate_repo
    (tmp_path / "decide.py").write_text(_decide_script())
    (work / "tools" / "name_leak_gate.py").write_text(_STUB_TOOL)
    rc, out = _run_gate(work, base_without_tool, tmp_path, default_branch="no-such-branch")
    assert rc != 0, out
    assert "does not resolve in this clone" in out
    assert "BOOTSTRAP" not in out


def test_harness_fails_closed_when_nothing_anywhere_has_the_tool(gate_repo, tmp_path):
    """No protected ref has it AND the checkout does not either: say so."""
    work, base_without_tool, _g = gate_repo
    (tmp_path / "decide.py").write_text(_decide_script())
    rc, out = _run_gate(work, base_without_tool, tmp_path)
    assert rc != 0, out
    assert "::error::" in out
    assert "Traceback" not in out


def _roster_path(tmp_path) -> str:
    outputs = (tmp_path / "gh_output.txt").read_text(encoding="utf-8")
    return next(
        line.split("=", 1)[1]
        for line in outputs.splitlines()
        if line.startswith("roster_path=")
    )


def test_harness_puts_the_roster_in_a_private_per_run_directory(gate_repo, tmp_path):
    """The roster is a secret on disk: private mode, private dir, no fixed name.

    A fixed path is wrong twice. On a shared host the name belongs to whoever
    wrote it first, so the next user's run cannot write it at all — that is how
    all five cells failed for a colleague on a shared /tmp. And a predictable
    world-readable path hands the roster to every account on the machine.
    """
    work, base_without_tool, _g = gate_repo
    (tmp_path / "decide.py").write_text(_decide_script())
    (work / "tools" / "name_leak_gate.py").write_text(_STUB_TOOL)

    first_rc, first_out = _run_gate(work, base_without_tool, tmp_path)
    assert first_rc == 0, first_out
    first = _roster_path(tmp_path)
    roster = Path(first)
    assert roster.exists()
    assert roster.stat().st_mode & 0o777 == 0o600, oct(roster.stat().st_mode)
    assert roster.parent.stat().st_mode & 0o777 == 0o700, oct(
        roster.parent.stat().st_mode
    )
    assert str(tmp_path) in first, "the roster must live under RUNNER_TEMP"

    # A second run must not reuse the name — that reuse is what collided.
    second_rc, second_out = _run_gate(work, base_without_tool, tmp_path)
    assert second_rc == 0, second_out
    assert _roster_path(tmp_path) != first




