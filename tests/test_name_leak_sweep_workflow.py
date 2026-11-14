"""Tests for .github/workflows/name-leak-sweep.yml (#4780).

Two layers, the same shape `tests/test_name_leak_gate.py` uses:

1. WIRING — the YAML is parsed and its contract is asserted: exactly the two
   triggers, no `schedule`, no `pull_request_target` use, the fork status name
   carrying the literal token `NOT SCANNED`, no job-level skip, and a scan
   invocation that passes `--roster-file` and `--redact`.

2. BEHAVIOUR — the sweep step's Python is extracted from the workflow verbatim
   and RUN against a scratch git repository. The cells drive the one assertion
   this workflow exists for: a scan which did not run must never report
   success. A missing summary line, `scan-leaks: scanned 0`, a scanner that
   exited, a missing roster or attestation, and a fork pull request are each a
   failure.

Why execute the heredoc rather than re-implement its logic in the test: the
repo already does this in `test_name_leak_gate.py` ("the I/O harness, executed
(not read)"), and the alternative asserts a copy of the logic rather than the
logic that ships.

Discrimination for the mutations the specs name:

* removing `--redact` from the scan invocation reddens
  `test_scan_invocation_passes_roster_file_and_redact` and nothing else;
* letting `N == 0` pass reddens
  `test_sweep_fails_when_zero_files_were_scanned` and nothing else;
* making `--redact` print the label reddens the redact cell in
  `tests/test_scan_leaks.py`, not anything here;
* making the capability probe always return True reddens
  `test_the_capability_chain_refuses_a_copy_without_the_roster_file_flag`
  and nothing else (the file-absence chain cells do not consult the probe);
* removing the dispatch ref guard reddens
  `test_workflow_dispatch_on_a_non_default_ref_fails_closed` and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "name-leak-sweep.yml"
GATE_SOURCE = REPO / "tools" / "name_leak_gate.py"

# A synthetic roster line for the harness. It is a regex, not a real name, and
# it compiles. The attestation below pins the sha256 of its canonical text.
_ROSTER = r"\bQuillonfex\b" + "\n"

def _with_capability(body: str, *, capability: bool = True) -> str:
    """A scanner stub, optionally carrying the `--roster-file` capability.

    The workflow probes a copy by running `<copy> --help` and looking for
    `--roster-file` in the output, so a stub that should be accepted must
    print that flag. `capability=False` is the pre-#4780 shape: a scanner that
    runs fine but does not accept the flag.
    """
    usage = (
        '    print("usage: scan-leaks.py [--roster-file PATH] [--redact]")\n'
        if capability
        else '    print("usage: scan-leaks.py")\n'
    )
    return (
        "import sys\n"
        'if "--help" in sys.argv:\n'
        + usage
        + "    sys.exit(0)\n"
        + body
    )


# Scanner stubs materialised by the chain. A stub is what actually runs.
_CLEAN_SCANNER = _with_capability(
    'print("scan-leaks: scanned 1, skipped-binary 0")\n'
    "sys.exit(0)\n"
)
_ZERO_SCANNER = _with_capability(
    'print("scan-leaks: scanned 0, skipped-binary 0")\n'
    "sys.exit(0)\n"
)
_NO_SUMMARY_SCANNER = _with_capability(
    'print("scan-leaks: clean")\n'
    "sys.exit(0)\n"
)
_HIT_SCANNER = _with_capability(
    'print("scan-leaks: scanned 1, skipped-binary 0")\n'
    'print("doc.md:1", file=sys.stderr)\n'
    "sys.exit(1)\n"
)
# The pre-#4780 scanner shape: present, runnable, but without `--roster-file`.
_INCAPABLE_SCANNER = _with_capability(
    'print("scan-leaks: scanned 1, skipped-binary 0")\n'
    "sys.exit(0)\n",
    capability=False,
)
# If the checkout's copy were executed instead of a chain link, this prints a
# marker and exits with a code no cell expects.
_MARKER_SCANNER = (
    "import sys\n"
    'print("CHECKOUT_SCANNER_RAN")\n'
    "sys.exit(7)\n"
)


# ---------------------------------------------------------------------------
# YAML accessors
# ---------------------------------------------------------------------------

def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(wf: dict) -> dict:
    """The `on:` mapping.

    PyYAML implements YAML 1.1, where a bare `on` is the boolean `True`, so the
    key is `True` (or the string) depending on the loader. Accept both, and
    fail loudly rather than silently reading an empty dict.
    """
    for key in ("on", True):
        if key in wf:
            value = wf[key]
            assert isinstance(value, dict), (
                f"`on:` should be a mapping so the triggers can be asserted; "
                f"got {type(value).__name__}"
            )
            return value
    raise AssertionError("no `on:` key found in the workflow")


def _job(wf: dict) -> dict:
    return wf["jobs"]["name-leak-sweep"]


def _sweep_step(wf: dict) -> dict:
    return next(step for step in _job(wf)["steps"] if step.get("id") == "sweep")


def _sweep_script() -> str:
    """The sweep step's Python, extracted from the workflow verbatim."""
    step = _sweep_step(_workflow())
    lines = step["run"].splitlines()
    start = next(i for i, line in enumerate(lines) if "<<'PY'" in line)
    end = max(i for i, line in enumerate(lines) if line.strip() == "PY")
    return textwrap.dedent("\n".join(lines[start + 1:end]))


# ---------------------------------------------------------------------------
# Wiring cells
# ---------------------------------------------------------------------------

def test_triggers_are_the_release_pr_edited_and_dispatch():
    """pull_request -> main (the release gate) with the four activity types,
    plus workflow_dispatch; and NO schedule until engram#4734 closes.

    `edited` is load-bearing: a base retarget is an `edited` event, so a PR
    retargeted INTO `main` would otherwise never be swept at all.
    """
    on = _triggers(_workflow())
    assert set(on) == {"pull_request", "workflow_dispatch"}, sorted(on)
    assert on["pull_request"] == {
        "branches": ["main"],
        "types": ["opened", "synchronize", "reopened", "edited"],
    }, on["pull_request"]
    assert "schedule" not in on, (
        "no schedule until engram#4734 closes: zero runs with event `schedule` "
        "have ever fired on this repository, so a cron line would be a check "
        "that never runs"
    )


def test_no_pull_request_target_use():
    """`pull_request_target` is forbidden; the header must say so."""
    text = WORKFLOW.read_text(encoding="utf-8")
    # The token is REQUIRED in a comment (the gate's header states the ban), so
    # the check excludes full-line comments and asserts absence everywhere the
    # workflow could actually use it.
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "pull_request_target" not in body, (
        "`pull_request_target` would hand the roster secret to "
        "contributor-controlled code"
    )
    assert "pull_request_target" not in _triggers(_workflow())
    header = "\n".join(
        line for line in text.splitlines() if line.lstrip().startswith("#")
    )
    assert "pull_request_target" in header and "FORBIDDEN" in header, (
        "the header must state the ban, as name-leak-gate.yml does"
    )


def test_fork_status_name_contains_not_scanned():
    """The fork variant is the literal token a reviewer must be able to tell
    from 'scanned clean'; it must never be a required check."""
    expressions = [_job(_workflow())["name"]]
    literals = re.findall(r"'([^']*)'", expressions[0])
    assert literals[-2:] == [
        "name-leak-sweep (NOT SCANNED - fork PR)",
        "name-leak-sweep",
    ], literals
    assert "NOT SCANNED" in literals[-2]


def test_the_job_cannot_be_skipped():
    """No job-level `if:`: a skip reports `skipped` under the same name and
    supersedes the previous run on the same head."""
    assert "if" not in _job(_workflow()), (
        "a job-level `if:` would let a typo fix in the title turn a red sweep "
        "grey (measured on engram-agents/ci-probes#6)"
    )


def test_scan_invocation_passes_roster_file_and_redact():
    """The scanner invocation must pass --root, --roster-file and --redact.

    --redact is load-bearing in a public log: the unredacted finding form is
    `path:line [label] snippet`, i.e. a name twice.
    """
    run = _sweep_step(_workflow())["run"]
    start = run.index("scan = subprocess.run(")
    end = run.index("capture_output=True", start)
    invocation = run[start:end]
    assert '"--root"' in invocation, invocation
    assert '"--roster-file"' in invocation, invocation
    assert '"--redact"' in invocation, invocation
    assert "roster_path" in invocation, invocation


def test_the_decision_function_is_read_from_the_base_commit():
    """The decision contract comes from BASE_SHA, never from the checkout: on
    a same-repo pull request the branch is contributor-controlled and this
    step holds the roster secret."""
    step = _sweep_step(_workflow())
    assert "BASE_SHA" in step["env"]
    run = step["run"]
    assert '_blob_from_base("tools/name_leak_gate.py")' in run
    assert 'f"{base_sha}:{path}"' in run
    assert 'f"{ref}^{{commit}}"' in run, (
        "an unresolvable base is a fact about the clone and must fail closed"
    )


def test_the_scanner_comes_from_the_capability_chain():
    """The scanner is read through the gate's three-link chain, with the
    discriminator changed from file existence to CAPABILITY: base, then the
    protected default branch, then the pull request's own copy, which is loud.

    The second link is what makes the bootstrap claim reachable: on the first
    `dev -> main` release PR both protected refs predate `--roster-file`, so a
    base-only read could never pass the release that introduces the flag.
    """
    step = _sweep_step(_workflow())
    run = step["run"]
    assert "BASE_SHA" in step["env"]
    assert "DEFAULT_BRANCH" in step["env"]
    assert '["git", "fetch", "--no-tags", "origin", default_branch]' in run
    assert "for ref in (base_sha, default_ref)" in run
    assert run.index("for ref in (base_sha, default_ref)") < run.index(
        "pull request checkout (BOOTSTRAP)"
    ), "the checkout fallback must come AFTER the base and default-branch links"

    # The capability test is executing the copy, not reading its file name.
    assert '"--help"' in run
    assert '"--roster-file" in proc.stdout' in run

    # POSITIVE KNOWLEDGE ONLY: both protected refs must RESOLVE before either
    # is judged; an unresolvable ref is a fact about the clone, never about the
    # tree, and fails closed.
    assert '["git", "cat-file", "-e", f"{ref}^{{commit}}"]' in run
    assert "does not resolve in this" in run
    assert run.index("does not resolve in this") < run.index(
        "pull request checkout (BOOTSTRAP)"
    ), "the fail-closed check must run BEFORE the bootstrap fallback"

    # The supplying link is printed on EVERY run, not only on the fallback.
    assert "scanner read from {scanner_source}" in run


def test_a_dispatch_must_run_from_the_default_branch():
    """A dispatch can be started on any branch by a writer, so mapping it onto
    the contract's "push" shape is honest only with the ref checked. A hard
    failure inside the step, never a job-level `if:` — a skip reports
    `skipped` under this job's name and supersedes the previous run on the
    same head, which is the false tick this file exists to prevent."""
    step = _sweep_step(_workflow())
    run = step["run"]
    assert "DEFAULT_BRANCH" in step["env"]
    assert "GITHUB_REF" in step["env"]
    assert 'event == "workflow_dispatch"' in run
    assert 'f"refs/heads/{default_branch}"' in run
    assert "if" not in _job(_workflow()), (
        "the dispatch guard must not be a job-level `if:`"
    )


# ---------------------------------------------------------------------------
# The executed harness
# ---------------------------------------------------------------------------

def _gate_source() -> str:
    return GATE_SOURCE.read_text(encoding="utf-8")


def _canonical_sha(roster: str) -> str:
    return hashlib.sha256((roster.rstrip("\n") + "\n").encode("utf-8")).hexdigest()


@pytest.fixture
def sweep_repo(tmp_path):
    """A work repo on `dev` with a bare remote, whose base commit carries the
    real decision contract and a clean, capable scanner stub, plus a matching
    attestation.

    The remote matters: the scanner chain's second link is
    `origin/<default_branch>`, and link 3 may be entered only when both
    protected refs RESOLVE. A fixture without a remote could not tell "the
    protected ref has no capability" from "this clone cannot see the ref" —
    the distinction the positive-knowledge rule exists to preserve.
    """
    if shutil.which("git") is None:
        pytest.skip("git not available")

    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    work.mkdir()

    def g(*args, cwd=None):
        return subprocess.run(
            ("git",) + args,
            cwd=cwd or work,
            check=True,
            capture_output=True,
            text=True,
        )

    g("init", "-q", "--bare", str(remote), cwd=tmp_path)
    g("init", "-q")
    g("config", "user.email", "t@example.invalid")
    g("config", "user.name", "t")

    (work / "tools").mkdir()
    (work / "tools" / "name_leak_gate.py").write_text(_gate_source(), encoding="utf-8")
    (work / ".github").mkdir()
    (work / ".github" / "name-leak-roster.attestation.json").write_text(
        json.dumps(
            {
                "roster_format": 1,
                "pattern_count": 1,
                "roster_sha256": _canonical_sha(_ROSTER),
            }
        ),
        encoding="utf-8",
    )
    (work / "src" / "engram" / "tools").mkdir(parents=True)
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        _CLEAN_SCANNER, encoding="utf-8"
    )

    g("add", "-A")
    g("commit", "-qm", "base")
    base = g("rev-parse", "HEAD").stdout.strip()
    g("branch", "-M", "dev")
    g("remote", "add", "origin", str(remote))
    g("push", "-q", "origin", "dev")
    g("fetch", "-q", "origin", "dev")
    return work, base, g


def _commit_scanner(work: Path, g, source: str, *, push: bool = False) -> str:
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        source, encoding="utf-8"
    )
    g("add", "-A")
    g("commit", "-qm", "scanner")
    sha = g("rev-parse", "HEAD").stdout.strip()
    if push:
        g("push", "-q", "origin", "dev")
        g("fetch", "-q", "origin", "dev")
    return sha


def _remove_scanner(work: Path, g, *, push: bool = False) -> str:
    """Commit a tree with NO scanner, so the chain's file-absence path is what
    is exercised (`git show` fails, independent of the capability probe)."""
    g("rm", "-q", "src/engram/tools/scan-leaks.py")
    g("commit", "-qm", "scanner removed")
    sha = g("rev-parse", "HEAD").stdout.strip()
    if push:
        g("push", "-q", "origin", "dev")
        g("fetch", "-q", "origin", "dev")
    return sha


def _run_sweep(
    work: Path,
    base: str,
    tmp_path: Path,
    *,
    event: str = "pull_request",
    is_fork: str = "false",
    roster_value: str | None = None,
    attestation: dict | None = None,
    default_branch: str = "dev",
    git_ref: str = "refs/heads/dev",
):
    """Run the extracted sweep step; return (CompletedProcess, combined output)."""
    if roster_value is None:
        roster_value = _ROSTER
    if attestation is not None:
        (work / ".github" / "name-leak-roster.attestation.json").write_text(
            json.dumps(attestation), encoding="utf-8"
        )

    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir(exist_ok=True)
    script = tmp_path / "sweep.py"
    script.write_text(_sweep_script(), encoding="utf-8")

    env = dict(os.environ)
    env.update(
        EVENT_NAME=event,
        PR_IS_FORK=is_fork,
        NAME_LEAK_ROSTER=roster_value,
        ATTESTATION_PATH=".github/name-leak-roster.attestation.json",
        BASE_SHA=base,
        DEFAULT_BRANCH=default_branch,
        GITHUB_REF=git_ref,
        GITHUB_WORKSPACE=str(work),
        RUNNER_TEMP=str(runner_temp),
    )
    proc = subprocess.run(
        [sys.executable, "-P", str(script)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )
    return proc, proc.stdout + proc.stderr


# ── positive control ────────────────────────────────────────────────────────

def test_sweep_succeeds_when_the_scan_ran_clean(sweep_repo, tmp_path):
    """Positive control: a trusted event, matching roster/attestation and a
    scan whose summary reports N > 0 is a success. Without this the failure
    cells below could all pass by rejecting every run.

    Also link 1 of the scanner chain: the base commit carries a capable copy,
    so the base is what runs, and the run says so."""
    work, base, _g = sweep_repo
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode == 0, out
    assert "scanned 1 file(s)" in out, out
    assert f"scanner read from {base}" in out, out


# ── the scanner chain: base -> default branch -> the PR's own copy ──────────

def test_scanner_comes_from_the_default_branch_when_the_base_has_none(
    sweep_repo, tmp_path
):
    """Link 2: the base commit has no scanner at all, so the protected default
    branch supplies it (the mirror of the gate's own link-2 cell)."""
    work, _base, g = sweep_repo
    base = _remove_scanner(work, g)  # not pushed: origin/dev still carries it
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode == 0, out
    assert "scanner read from origin/dev" in out, out
    assert "BOOTSTRAP" not in out, out


def test_bootstrap_is_entered_only_when_no_protected_ref_has_the_capability(
    sweep_repo, tmp_path
):
    """Link 3, and it is loud: neither protected ref carries a copy with the
    capability, so the pull request's own copy runs. This is the genuine
    pre-merge shape (#4780's own introduction)."""
    work, _base, g = sweep_repo
    base = _remove_scanner(work, g, push=True)  # base AND origin/dev lack it
    (work / "src" / "engram" / "tools").mkdir(parents=True, exist_ok=True)
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        _CLEAN_SCANNER, encoding="utf-8"
    )
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode == 0, out
    assert "BOOTSTRAP" in out, out
    assert "scanner read from pull request checkout (BOOTSTRAP)" in out, out


def test_the_capability_chain_refuses_a_copy_without_the_roster_file_flag(
    sweep_repo, tmp_path
):
    """The discriminator is CAPABILITY, not the file's existence.

    Arm 1 (fall-through): the base carries a scanner that predates
    `--roster-file` — exactly the shape of `origin/main` on the release PR
    that introduces the flag. It must NOT be accepted; the protected default
    branch supplies the scanner instead.

    Arm 2 (refusal): no link carries the capability. Scanning anyway would run
    a scanner against no roster and could print a green tick, so the step
    fails closed BEFORE scanning and names each link and what it lacked.
    """
    work, _base, g = sweep_repo

    # Arm 1: base present-but-incapable; origin/dev still carries the capable
    # copy from the fixture.
    base = _commit_scanner(work, g, _INCAPABLE_SCANNER)
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode == 0, out
    assert "scanner read from origin/dev" in out, out

    # Arm 2: the protected branch gets the incapable copy too, and so does the
    # checkout.
    g("push", "-q", "origin", "dev")
    g("fetch", "-q", "origin", "dev")
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        _INCAPABLE_SCANNER, encoding="utf-8"
    )
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode != 0, out
    assert "no copy of src/engram/tools/scan-leaks.py with --roster-file" in out, out
    assert "lacks --roster-file" in out, out
    assert "Never run a scanner against a roster without --roster-file" in out, out


def test_fails_closed_when_the_base_does_not_resolve(sweep_repo, tmp_path):
    """An unreadable ref is a fact about the clone, never about the tree."""
    work, _base, _g = sweep_repo
    proc, out = _run_sweep(work, "0" * 40, tmp_path)
    assert proc.returncode != 0, out
    assert "does not resolve in this clone" in out, out
    assert "BOOTSTRAP" not in out, out


def test_fails_closed_when_the_default_ref_does_not_resolve(sweep_repo, tmp_path):
    """The fetch is `check=False` because the ref's RESOLUTION, not the fetch
    exit code, is the fact. A ref this clone cannot see fails closed rather
    than reading as "that tree has no scanner"."""
    work, base, _g = sweep_repo
    proc, out = _run_sweep(work, base, tmp_path, default_branch="no-such-branch")
    assert proc.returncode != 0, out
    assert "does not resolve in this clone" in out, out
    assert "BOOTSTRAP" not in out, out


# ── the scan-ran assertion (the invariant's load-bearing half) ──────────────

def test_sweep_fails_when_the_summary_line_is_missing(sweep_repo, tmp_path):
    """No `scan-leaks: scanned N` line means the sweep did not run: failure,
    never a green tick."""
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _NO_SUMMARY_SCANNER)
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode != 0, out
    assert "did not run" in out, out


def test_sweep_fails_when_zero_files_were_scanned(sweep_repo, tmp_path):
    """`scanned 0` is a scan of nothing, not a clean scan: failure."""
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _ZERO_SCANNER)
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode != 0, out
    assert "0 files scanned" in out, out


def test_sweep_fails_when_the_scanner_exits_nonzero(sweep_repo, tmp_path):
    """A hit (exit 1) fails the release gate and says what a hit means."""
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _HIT_SCANNER)
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode != 0, out
    assert "a hit means" in out, out


# ── the decision's failure rows, executed ───────────────────────────────────

def test_sweep_fails_when_the_roster_is_absent(sweep_repo, tmp_path):
    """A missing secret is a failure, never a green skip."""
    work, base, _g = sweep_repo
    proc, out = _run_sweep(work, base, tmp_path, roster_value="")
    assert proc.returncode != 0, out
    assert "roster absent" in out, out


def test_sweep_fails_when_the_attestation_does_not_match(sweep_repo, tmp_path):
    work, base, _g = sweep_repo
    proc, out = _run_sweep(
        work,
        base,
        tmp_path,
        attestation={
            "roster_format": 1,
            "pattern_count": 1,
            "roster_sha256": "0" * 64,
        },
    )
    assert proc.returncode != 0, out
    assert "does not match" in out, out


def test_sweep_fails_on_a_fork_with_not_scanned(sweep_repo, tmp_path):
    """A fork PR never receives the secret, so it is a loud failure that says
    it did NOT scan — never a green skip."""
    work, base, _g = sweep_repo
    proc, out = _run_sweep(work, base, tmp_path, is_fork="true")
    assert proc.returncode != 0, out
    assert "did NOT scan" in out, out


# ── the call-site adaptation for workflow_dispatch ──────────────────────────

def test_workflow_dispatch_is_a_trusted_event_for_the_sweep(sweep_repo, tmp_path):
    """workflow_dispatch is one of this workflow's two triggers, so it must be
    successfully scannable by hand.

    `decide()`'s TRUSTED_EVENTS is {push, pull_request}; the sweep presents a
    dispatch under the trusted non-fork shape ("push") at the call site. This
    cell is what stops that adaptation from silently disappearing.
    """
    work, base, _g = sweep_repo
    proc, out = _run_sweep(work, base, tmp_path, event="workflow_dispatch")
    assert proc.returncode == 0, out
    assert "scanned 1 file(s)" in out, out


def test_workflow_dispatch_on_a_non_default_ref_fails_closed(sweep_repo, tmp_path):
    """GitHub allows a writer to dispatch a workflow on a BRANCH. The contract's
    "push" shape means "an event only a writer could have caused on a protected
    ref", so a dispatch is trusted only from `refs/heads/<default_branch>`.
    A hard failure inside the step — not a job-level `if:` — so the run cannot
    turn into a `skipped` false tick."""
    work, base, _g = sweep_repo
    proc, out = _run_sweep(
        work,
        base,
        tmp_path,
        event="workflow_dispatch",
        git_ref="refs/heads/feature-x",
    )
    assert proc.returncode != 0, out
    assert "must run from the default branch" in out, out
    assert "refs/heads/dev" in out, out


# ── roster validation and the base-commit split, executed ───────────────────

def test_invalid_roster_pattern_fails_without_printing_it(sweep_repo, tmp_path):
    """An invalid roster line fails the step by LINE NUMBER; the pattern text
    is never printed (this is a public log and the roster is a name list)."""
    token = "qvollmex3311"
    work, base, _g = sweep_repo
    proc, out = _run_sweep(
        work,
        base,
        tmp_path,
        roster_value=rf"\bQuillonfex\b" + "\n" + f"(?P={token})" + "\n",
    )
    assert proc.returncode != 0, out
    assert "line 2 is not a valid regular expression" in out, out
    assert token not in out, out


def test_scanner_is_read_from_the_base_commit_not_the_checkout(sweep_repo, tmp_path):
    """A scanner stubbed into the checkout but never committed must not run:
    the scanner is read from BASE_SHA. (The code being released must not
    influence the tool that scans it.)"""
    work, base, _g = sweep_repo
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        _MARKER_SCANNER, encoding="utf-8"
    )
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode == 0, out
    assert "CHECKOUT_SCANNER_RAN" not in out, out
    assert "scanned 1 file(s)" in out, out
