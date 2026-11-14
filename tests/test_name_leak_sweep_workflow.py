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

Discrimination for the three mutations the spec names:

* removing `--redact` from the scan invocation reddens
  `test_scan_invocation_passes_roster_file_and_redact` and nothing else;
* letting `N == 0` pass reddens
  `test_sweep_fails_when_zero_files_were_scanned` and nothing else;
* making `--redact` print the label reddens the redact cell in
  `tests/test_scan_leaks.py`, not anything here.
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

# Scanner stubs committed as the BASE commit's copy. The harness materialises
# the scanner from BASE_SHA, so a stub is what actually runs.
_CLEAN_SCANNER = (
    "import sys\n"
    'print("scan-leaks: scanned 1, skipped-binary 0")\n'
    "sys.exit(0)\n"
)
_ZERO_SCANNER = (
    "import sys\n"
    'print("scan-leaks: scanned 0, skipped-binary 0")\n'
    "sys.exit(0)\n"
)
_NO_SUMMARY_SCANNER = (
    "import sys\n"
    'print("scan-leaks: clean")\n'
    "sys.exit(0)\n"
)
_HIT_SCANNER = (
    "import sys\n"
    'print("scan-leaks: scanned 1, skipped-binary 0")\n'
    'print("doc.md:1", file=sys.stderr)\n'
    "sys.exit(1)\n"
)
# If the checkout's copy were executed instead of the base commit's, this
# prints a marker and exits with a code no cell expects.
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

def test_triggers_are_exactly_the_release_pr_and_dispatch():
    """pull_request -> main (the release gate), plus workflow_dispatch; and NO
    schedule until engram#4734 closes."""
    on = _triggers(_workflow())
    assert set(on) == {"pull_request", "workflow_dispatch"}, sorted(on)
    assert on["pull_request"] == {"branches": ["main"]}, on["pull_request"]
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


def test_executable_code_is_read_from_the_base_commit():
    """Both the decision function and the scanner come from BASE_SHA, never
    from the checkout: the code being released must not influence the tool that
    scans it (the same split name-leak-gate.yml makes)."""
    step = _sweep_step(_workflow())
    assert "BASE_SHA" in step["env"]
    run = step["run"]
    assert '_blob_from_base("tools/name_leak_gate.py")' in run
    assert '_blob_from_base("src/engram/tools/scan-leaks.py")' in run
    assert 'f"{base_sha}:{path}"' in run
    assert 'f"{base_sha}^{{commit}}"' in run, (
        "an unresolvable base is a fact about the clone and must fail closed"
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
    """A work repo whose base commit carries the real decision contract and a
    clean scanner stub, plus a matching attestation."""
    if shutil.which("git") is None:
        pytest.skip("git not available")

    work = tmp_path / "work"
    work.mkdir()

    def g(*args):
        return subprocess.run(
            ("git",) + args, cwd=work, check=True, capture_output=True, text=True
        )

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
    return work, base, g


def _commit_scanner(work: Path, g, source: str) -> str:
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        source, encoding="utf-8"
    )
    g("add", "-A")
    g("commit", "-qm", "scanner")
    return g("rev-parse", "HEAD").stdout.strip()


def _run_sweep(
    work: Path,
    base: str,
    tmp_path: Path,
    *,
    event: str = "pull_request",
    is_fork: str = "false",
    roster_value: str | None = None,
    attestation: dict | None = None,
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
    cells below could all pass by rejecting every run."""
    work, base, _g = sweep_repo
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode == 0, out
    assert "scanned 1 file(s)" in out, out


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
