"""Tests for .github/workflows/name-leak-sweep.yml (#4780).

Two layers, the same shape `tests/test_name_leak_gate.py` uses:

1. WIRING — the YAML is parsed and its contract is asserted: exactly the two
   triggers, no `schedule`, no `pull_request_target` use, the fork status name
   carrying the literal token `NOT SCANNED`, no job-level skip, the roster
   secret reachable from the SWEEP step ONLY (by any route — step env, job or
   workflow env, or a `GITHUB_ENV` write), and a scan invocation that passes
   `--roster-file` and `--redact`.

2. BEHAVIOUR — BOTH steps' Python is extracted from the workflow verbatim and
   RUN in order against a scratch git repository, sharing a `RUNNER_TEMP` and
   a `GITHUB_OUTPUT` file, so the STEP BOUNDARY is exercised rather than
   assumed: `select` (no roster secret in its environment, every capability
   probe, selects the scanner) then `sweep` (the secret, the roster file, the
   decision, the scan). The cells drive the assertion this workflow exists
   for: a scan which did not run must never report success — and no probe of
   any copy may run while the roster exists anywhere on the runner
   (#4782 r7). A missing summary line, `scan-leaks: scanned 0`, a scanner that
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
* making the capability probe always return True reddens TWO cells:
  `test_the_scanner_comes_from_the_capability_chain` (wiring, pins the probe's
  source text) and `test_the_capability_chain_refuses_a_copy_without_the_roster_file_flag`
  (behavioural); the file-absence chain cells do not consult the probe;
* removing the dispatch ref guard reddens TWO cells:
  `test_a_dispatch_must_run_from_the_default_branch` (wiring) and
  `test_workflow_dispatch_on_a_non_default_ref_fails_closed` (behavioural);
* R6: making the probe's NONZERO-EXIT branch fall back to "lacks the flag"
  (the pre-round-6 semantics) reddens TWO cells,
  `test_a_crashing_probe_blocks_the_checkout_link` and
  `test_a_crashing_checkout_probe_is_not_reported_as_lacking` — every other
  cell's stub exits 0, and the timeout cell reaches the could-not-examine
  bucket through the separate `TimeoutExpired` arm.
* R7: making the SELECTION step's fail-closed chain exit return 0 reddens the
  three cells that read that step's OWN exit code:
  `test_step_a_fails_closed_when_a_protected_ref_cannot_be_examined` (the
  `unexamined` gate), `test_a_non_capable_bootstrap_verdict_never_starts_the_sweep`
  and `test_step_a_fails_closed_when_the_checkout_probe_crashes` (the
  no-scanner gate). The cells that read the combined run stay green: the sweep
  step then starts on empty outputs and dies, so the pair is still non-zero.
  That asymmetry is why the exit invariant needs its own cells.
* R7: putting `NAME_LEAK_ROSTER` into the selection step's `env:` reddens
  `test_the_roster_secret_is_unreachable_from_the_selection_step` and no other
  cell (the harness never passes the variable to that step regardless).
* R7: dropping the `finally` around the scan reddens
  `test_the_roster_file_is_removed_when_the_scan_raises` (the success path
  removes the file too, so the normal-run cell cannot see it).
* #4752: making the sweep's `scan.returncode == 3` arm unreachable (e.g.
  `== 99`) reddens `test_sweep_reports_exit_3_as_coverage_incomplete`; the
  exit-1 hit cell and its positive control stay green, because they are
  separate arms and the generic "did not run" message is a third.
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

# A synthetic roster line for the harness, in the format-2 grammar: a class
# token, a tab, then a regex. It is a regex, not a real name, and it compiles.
# The attestation below pins the sha256 of its canonical text.
_ROSTER = "A\t" + r"\bQuillonfex\b" + "\n"
_ROSTER_COUNTS_LINE = "scan-leaks: roster patterns A=1, B=0\n"

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
# A clean stub reports the per-class counts the scanner's parse produced, which
# the sweep compares against the attestation (§2.5); a stub that exits 0
# without them is a scan whose roster was never proven to parse, and the sweep
# must fail closed on it.
_CLEAN_SCANNER = _with_capability(
    'print("scan-leaks: scanned 1, skipped-binary 0")\n'
    + 'print("' + _ROSTER_COUNTS_LINE.strip() + '")\n'
    + "sys.exit(0)\n"
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
# A scanner that refuses the roster it was handed (an uncompilable regex is the
# SCANNER'S call — the workflow compiles no roster line) exits 2 with a reason
# that names a line and an offset, never the roster text. The sweep must fail
# closed, and must not read exit 2 as a hit or as a pass.
_ROSTER_REFUSED_SCANNER = _with_capability(
    'print("scan-leaks: roster line 2 is not a valid regular expression: '
    'error at offset 4", file=sys.stderr)\n'
    "sys.exit(2)\n"
)
# #4752: no findings, but the scan could not read every candidate (or had
# none). Exit 3 is "coverage incomplete" — distinct from a hit (exit 1) and
# from a scanner that did not run (no summary line). The sweep must fail and
# say coverage was incomplete, never call it a leak and never tick.
_COVERAGE_INCOMPLETE_SCANNER = _with_capability(
    'print("scan-leaks: scanned 1, skipped-binary 0")\n'
    'print("scan-leaks: unreadable 1 (coverage incomplete): locked.txt")\n'
    "sys.exit(3)\n"
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
# R3-3: a REAL-SHAPED scanner. It resolves a sibling module at import time,
# mirroring the real scanner's `_load_home_path_class()`
# (`here.parent.parent / "build" / "engine" / ...`). Self-contained stubs
# cannot exercise that dependency, which is why the single-file
# materialisation passed every chain cell and failed in production.
_REAL_SHAPED_SCANNER = (
    "import sys\n"
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def _load_sweep_fixture_helper():\n"
    "    here = Path(__file__).resolve().parent\n"
    "    candidate = (\n"
    '        here.parent.parent / "build" / "engine"\n'
    '        / "sweep_fixture_helper.py"\n'
    "    )\n"
    "    if not candidate.is_file():\n"
    "        raise ImportError(\n"
    '            "sweep_fixture_helper.py not found beside scan-leaks.py"\n'
    "        )\n"
    '    return candidate.read_text(encoding="utf-8")\n'
    "\n"
    "\n"
    "_HELPER = _load_sweep_fixture_helper()\n"
    'assert _HELPER.strip() == "FIXTURE_HELPER", _HELPER\n'
    "\n"
    'if "--help" in sys.argv:\n'
    '    print("usage: scan-leaks.py [--roster-file PATH] [--redact]")\n'
    "    sys.exit(0)\n"
    "\n"
    'print("scan-leaks: scanned 1, skipped-binary 0")\n'
    'print("' + _ROSTER_COUNTS_LINE.strip() + '")\n'
    "sys.exit(0)\n"
)
# R3-2 / R5-2: a scanner whose USAGE depends on what it can see. The workflow
# probes a copy by running `<copy> --help` and accepts it only when the output
# names `--roster-file`, so a copy that can see NAME_LEAK_ROSTER advertises a
# usage WITHOUT the flag and is judged INCAPABLE. That makes the capability
# check itself the observable for the probe call site.
#
# R5-2: the previous stub printed the normal usage unconditionally and sent its
# marker to a stdout the probe discards, so dropping `env=_scanner_env()` from
# the probe reddened nothing. A marker is not observable here; the verdict is.
_ENV_PROBE_SCANNER = (
    "import os\n"
    "import sys\n"
    '_leaked = "NAME_LEAK_ROSTER" in os.environ\n'
    'if "--help" in sys.argv:\n'
    "    if _leaked:\n"
    '        print("usage: scan-leaks.py")\n'
    "    else:\n"
    '        print("usage: scan-leaks.py [--roster-file PATH] [--redact]")\n'
    "    sys.exit(0)\n"
    "if _leaked:\n"
    '    print("SCAN_SAW_ROSTER")\n'
    "    sys.exit(3)\n"
    'print("scan-leaks: scanned 1, skipped-binary 0")\n'
    'print("' + _ROSTER_COUNTS_LINE.strip() + '")\n'
    "sys.exit(0)\n"
)
# R5-1: a CAPABLE scanner — the probe accepts it — which prints a marker and
# then reports a clean scan, so a run that reaches it via BOOTSTRAP looks GREEN.
# It stands in for the contributor-controlled checkout copy the roster must
# never be handed to.
_CAPABLE_MARKER_SCANNER = _with_capability(
    'print("CHECKOUT_SCANNER_RAN")\n'
    'print("scan-leaks: scanned 1, skipped-binary 0")\n'
    + 'print("' + _ROSTER_COUNTS_LINE.strip() + '")\n'
    + "sys.exit(0)\n"
)
# R6: a scanner that CRASHES at import, so `<copy> --help` exits nonzero and
# prints no usage at all. A probe that dies is NOT a copy that lacks
# `--roster-file`; it is a copy this run could not EXAMINE, and the chain must
# fail closed rather than fall through to the contributor-controlled checkout.
_CRASHING_SCANNER = (
    "raise ImportError('scanner import failure (round-6 fixture)')\n"
)
# R6: a scanner that HANGS at import, so `<copy> --help` never answers. The
# sleep is far longer than the bound the timeout cell substitutes, and it
# happens at import so the probe never reaches a usage to read.
_HANGING_SCANNER = (
    "import time\n"
    "time.sleep(120)\n"
    'print("usage: scan-leaks.py [--roster-file PATH] [--redact]")\n'
)
# R7: an observer scanner. From INSIDE the scan it reports what the sweep step
# handed it: the `--root` path and whether a readable roster file exists at the
# `--roster-file` path. Those are the observables for "the scanned tree is
# still the checkout" and "the roster file existed during the scan and is gone
# afterwards" — neither is visible from the step's own output.
_OBSERVER_SCANNER = _with_capability(
    "import os\n"
    'args = sys.argv[1:]\n'
    'root = args[args.index("--root") + 1] if "--root" in args else "<absent>"\n'
    "roster = (\n"
    '    args[args.index("--roster-file") + 1]\n'
    '    if "--roster-file" in args\n'
    '    else "<absent>"\n'
    ")\n"
    'print(f"ROOT_AT_SCAN={root}")\n'
    'print(f"ROSTER_FILE_IS_FILE_AT_SCAN={os.path.isfile(roster)}")\n'
    "if os.path.isfile(roster):\n"
    '    with open(roster, encoding="utf-8") as fh:\n'
    '        print("ROSTER_TEXT_AT_SCAN=" + fh.read().strip())\n'
    'print("scan-leaks: scanned 1, skipped-binary 0")\n'
    'print("' + _ROSTER_COUNTS_LINE.strip() + '")\n'
    "sys.exit(0)\n"
)
# R7: a scanner that emits invalid UTF-8 DURING THE SCAN. The sweep step reads
# the child with `text=True`, so `subprocess.run` raises UnicodeDecodeError
# after the roster file exists — the exception path the `finally` must clean up.
# Its `--help` output stays valid, or the probe (which also decodes) would fail
# and the chain would never select this copy.
_INVALID_UTF8_SCANNER = _with_capability(
    'sys.stdout.buffer.write(b"\\xff\\xfe not utf-8")\n'
    "sys.stdout.buffer.flush()\n"
    "sys.exit(0)\n"
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


def _step(wf: dict, step_id: str) -> dict:
    return next(step for step in _job(wf)["steps"] if step.get("id") == step_id)


def _sweep_step(wf: dict) -> dict:
    """Step B: the step that holds the roster secret."""
    return _step(wf, "sweep")


def _select_step(wf: dict) -> dict:
    """Step A: the secret-free selection step (#4782 r7)."""
    return _step(wf, "select")


def _step_script(step_id: str) -> str:
    """One step's Python, extracted from the workflow verbatim."""
    step = _step(_workflow(), step_id)
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
    a same-repo pull request the branch is contributor-controlled and the sweep
    step holds the roster secret."""
    sweep = _sweep_step(_workflow())
    assert "BASE_SHA" in sweep["env"]
    run = sweep["run"]
    assert '_blob_from_base("tools/name_leak_gate.py")' in run
    assert 'f"{base_sha}:{path}"' in run
    assert 'f"{ref}^{{commit}}"' in _select_step(_workflow())["run"], (
        "an unresolvable base is a fact about the clone and must fail closed; "
        "the selection step checks it before any probe runs"
    )


def test_the_scanner_comes_from_the_capability_chain():
    """The scanner is read through the gate's three-link chain, with the
    discriminator changed from file existence to CAPABILITY: base, then the
    protected default branch, then the pull request's own copy, which is loud.

    The second link is what makes the bootstrap claim reachable: on the first
    `dev -> main` release PR both protected refs predate `--roster-file`, so a
    base-only read could never pass the release that introduces the flag.

    All of it lives in the SELECTION step (#4782 r7): the chain executes a
    candidate copy for `--help`, so it may not run where a secret lives.
    """
    step = _select_step(_workflow())
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

    # The supplying link is printed on EVERY run, not only on the fallback —
    # in the SWEEP step, next to the scan it describes.
    assert "scanner read from {scanner_source}" in _sweep_step(_workflow())["run"]


def test_the_capability_probe_is_bounded():
    """R6-1: the `--help` probe must carry a timeout.

    Without one, a copy that hangs on `--help` hangs the job until the
    job-level ceiling instead of being diagnosed. The bound is a module-level
    constant so the timeout cell can shrink it for one run without editing the
    call, and the call site passes it.
    """
    run = _select_step(_workflow())["run"]
    assert re.search(r"^PROBE_TIMEOUT_SECONDS = \d+$", run, re.MULTILINE), (
        "the probe bound must be a module-level PROBE_TIMEOUT_SECONDS constant"
    )
    assert "timeout=PROBE_TIMEOUT_SECONDS," in run, (
        "the `--help` probe call must pass the bound"
    )


def test_a_dispatch_must_run_from_the_default_branch():
    """A dispatch can be started on any branch by a writer, so mapping it onto
    the contract's "push" shape is honest only with the ref checked. A hard
    failure inside the step, never a job-level `if:` — a skip reports
    `skipped` under this job's name and supersedes the previous run on the
    same head, which is the false tick this file exists to prevent."""
    step = _select_step(_workflow())
    run = step["run"]
    assert "DEFAULT_BRANCH" in step["env"]
    assert "GITHUB_REF" in step["env"]
    assert 'event == "workflow_dispatch"' in run
    assert 'f"refs/heads/{default_branch}"' in run
    assert "if" not in _job(_workflow()), (
        "the dispatch guard must not be a job-level `if:`"
    )


# ---------------------------------------------------------------------------
# R7 — the roster secret is unreachable from the selection step
# ---------------------------------------------------------------------------

def test_the_roster_secret_is_unreachable_from_the_selection_step():
    """#4782 r7: the selection step must not carry `NAME_LEAK_ROSTER` BY ANY
    ROUTE. A step-level check alone is the one that misses, so every route is
    asserted on the PARSED YAML (a comment mentioning the name can neither
    satisfy nor defeat a parsed check):

    1. the step's own `env:` block;
    2. the JOB-level `env:` block, which would put the secret into every step;
    3. a `GITHUB_ENV` write in any step, which silently re-populates every
       LATER step — including one whose own `env:` block is clean.

    The workflow-level `env:` block is checked too: it is route 2 one level
    up, and closing it costs nothing.
    """
    wf = _workflow()
    job = _job(wf)
    secret = "secrets.NAME_LEAK_ROSTER"

    # Route 1 — the step's own env. The secret expression may be mapped in
    # EXACTLY ONE step's env, and that step must be the sweep (Step B). This
    # also catches a mapping under a DIFFERENT key name.
    holders = {}
    for step in job["steps"]:
        keys = [
            key
            for key, value in (step.get("env") or {}).items()
            if secret in str(value)
        ]
        if keys:
            holders[step.get("id")] = keys
    assert holders == {"sweep": ["NAME_LEAK_ROSTER"]}, (
        "the roster secret must be mapped into the sweep step's environment "
        f"and nowhere else; found {holders}"
    )
    assert "NAME_LEAK_ROSTER" not in (_select_step(wf).get("env") or {}), (
        "the selection step's env must not name the roster secret at all"
    )

    # Route 2 — a job-level (or workflow-level) env would reach every step,
    # the selection one included.
    for scope, block in (("workflow", wf), ("job", job)):
        block_env = block.get("env") or {}
        assert "NAME_LEAK_ROSTER" not in block_env, (
            f"{scope}-level env names NAME_LEAK_ROSTER, so every step receives it"
        )
        for key, value in block_env.items():
            assert secret not in str(value), (
                f"{scope}-level env {key!r} maps the roster secret, so every "
                "step — including the selection step — receives it"
            )

    # Route 3 — a write to $GITHUB_ENV in ANY step reaches every LATER step.
    # Only the WRITE FORMS are matched, so prose that merely names the variable
    # (a comment saying it must not be used, say) can neither satisfy nor
    # defeat the check. This job has no such write at all, which is the
    # strongest form: a future one reddens this cell and has to be reasoned
    # about.
    shell_write = re.compile(r">>?\s*[\"']?\$\{?GITHUB_ENV\}?[\"']?")
    python_write = re.compile(
        r"os\.environ\[\s*[\"']GITHUB_ENV[\"']\s*\]|open\([^)]*GITHUB_ENV"
    )
    for step in job["steps"]:
        label = step.get("id") or step.get("name") or "<unnamed step>"
        run = step.get("run") or ""
        assert not shell_write.search(run), (
            f"step {label!r} redirects into $GITHUB_ENV; an export there "
            "re-populates every later step, including the selection step"
        )
        assert not python_write.search(run), (
            f"step {label!r} writes the GITHUB_ENV file; an export there "
            "re-populates every later step, including the selection step"
        )


def test_the_selection_step_owns_every_capability_probe():
    """#4782 r7: every probe runs in the secret-free step, and a failed
    selection cannot reach the secret-bearing step.

    Step B must contain NO `--help` probe: after the split, no probe of any
    copy may run while the roster exists anywhere on the runner. The exit
    invariant is the boundary that keeps it that way — Step B carries GitHub's
    default `success()` condition (no `if:`, no `continue-on-error` on Step A),
    so a non-zero Step A means Step B never starts.
    """
    select = _select_step(_workflow())
    sweep = _sweep_step(_workflow())
    select_run = select["run"]
    sweep_run = sweep["run"]

    # Both protected links AND the checkout link are probed in Step A.
    assert "_probe_capability(candidate)" in select_run, (
        "the protected-ref capability probe must run in the selection step"
    )
    assert "_probe_capability(" in select_run
    assert "checkout_scanner" in select_run, (
        "the checkout copy's probe must run in the selection step"
    )
    assert "pull request checkout (BOOTSTRAP)" in select_run

    # ... and Step B probes nothing at all.
    assert "--help" not in sweep_run, (
        "no `--help` probe may run in the step that holds the roster secret"
    )
    assert "_probe_capability" not in sweep_run

    # The invariant's mechanism: a failed selection stops the sweep.
    assert "continue-on-error" not in select, (
        "continue-on-error on the selection step would let a run that selected "
        "no scanner reach the secret-bearing step"
    )
    assert "if" not in sweep, (
        "the sweep step must keep GitHub's default success() condition"
    )

    # Declaration order: selection first, sweep second.
    ids = [step.get("id") for step in _job(_workflow())["steps"]]
    assert ids.index("select") < ids.index("sweep")


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
                "roster_format": 2,
                "pattern_count_a": 1,
                "pattern_count_b": 0,
                "roster_sha256": _canonical_sha(_ROSTER),
                "updated_at": "2026-11-15T00:00:00Z",
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


def _failing_tar_dir(tmp_path: Path) -> Path:
    """A PATH entry whose `tar` always fails, so a ref that RESOLVES reaches
    the extraction-failure branch of `_materialise_from` while `git` stays
    reachable through the rest of PATH."""
    shim_dir = tmp_path / "failing-tar"
    shim_dir.mkdir()
    tar_shim = shim_dir / "tar"
    tar_shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    tar_shim.chmod(0o755)
    return shim_dir


def _failing_archive_git_dir(tmp_path: Path) -> Path:
    """A PATH entry whose `git` fails `archive` and delegates EVERY other
    subcommand to the real git.

    Mirrors `_failing_tar_dir`, but for the OTHER arm of `_materialise_from`:
    that one leaves `git archive` succeeding and breaks only `tar`, this one
    breaks the archive itself. Delegation is load-bearing — `rev-parse`,
    `cat-file`, `show` and `fetch` must keep working or the run would fail at
    the resolution guard instead of at the arm under test.
    """
    real_git = shutil.which("git")
    assert real_git is not None, "git is required for this fixture"
    shim_dir = tmp_path / "failing-git-archive"
    shim_dir.mkdir()
    git_shim = shim_dir / "git"
    git_shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "archive" ]; then\n'
        "    exit 1\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    git_shim.chmod(0o755)
    return shim_dir


def _parse_github_output(path: Path) -> dict[str, str]:
    """Parse a `GITHUB_OUTPUT` file written as `key=value` lines."""
    outputs = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        assert sep, f"GITHUB_OUTPUT line without '=': {line!r}"
        outputs[key] = value
    return outputs


def _roster_files(root: Path) -> list[Path]:
    """Every file named `roster.txt` under `root`, wherever it was created."""
    return sorted(p for p in Path(root).rglob("roster.txt") if p.is_file())


def _run_steps(
    work: Path,
    base: str,
    tmp_path: Path,
    *,
    stop_after_select: bool = False,
    event: str = "pull_request",
    is_fork: str = "false",
    roster_value: str | None = None,
    attestation: dict | None = None,
    default_branch: str = "dev",
    git_ref: str = "refs/heads/dev",
    extra_path: Path | None = None,
    probe_timeout_seconds: int | None = None,
):
    """Run the workflow's TWO steps in order; return (proc, output, sweep_ran).

    The step boundary IS the security property (#4782 r7), so the harness
    models it rather than concatenating the two scripts into one run:

    * `select` runs with NO `NAME_LEAK_ROSTER` in its environment. The
      workflow's own step env does not carry it, and this harness pops it from
      the inherited environment too, so an ambient value in the test process
      cannot make the cells lie;
    * the three values it writes to `GITHUB_OUTPUT` feed `sweep`, which runs
      with the secret — the wiring the YAML declares;
    * the sweep step is started only when the selection step exited 0, which
      is GitHub's default `success()` condition;
    * `sweep_ran` reports whether that second process was started at all, and
      a `step-b-invoked` sentinel is left in `tmp_path` when it was. A cell
      asserting "Step B never ran" needs this: on a wrongly-successful
      selection the sweep step would start, find no outputs, and die — leaving
      the combined run non-zero and the violation invisible.

    `probe_timeout_seconds` substitutes the SELECTION step's
    `PROBE_TIMEOUT_SECONDS` for one run. The shipped bound is sized for a real
    runner; a cell that provokes a real timeout needs it small. Substituting
    the constant costs ~2s (two links x 1s) and still exercises the SAME
    `TimeoutExpired` arm, message and fail-closed routing that the shipped
    value reaches.
    """
    if roster_value is None:
        roster_value = _ROSTER
    if attestation is not None:
        (work / ".github" / "name-leak-roster.attestation.json").write_text(
            json.dumps(attestation), encoding="utf-8"
        )

    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir(exist_ok=True)
    github_output = tmp_path / "github-output"
    github_output.write_text("", encoding="utf-8")

    select_text = _step_script("select")
    if probe_timeout_seconds is not None:
        select_text, substituted = re.subn(
            r"^PROBE_TIMEOUT_SECONDS = \d+$",
            f"PROBE_TIMEOUT_SECONDS = {probe_timeout_seconds}",
            select_text,
            flags=re.MULTILINE,
        )
        assert substituted == 1, (
            "the probe bound must be a single module-level "
            "'PROBE_TIMEOUT_SECONDS = <int>' line in the SELECTION step"
        )
    select_py = tmp_path / "select.py"
    select_py.write_text(select_text, encoding="utf-8")

    common = dict(
        EVENT_NAME=event,
        BASE_SHA=base,
        GITHUB_WORKSPACE=str(work),
        RUNNER_TEMP=str(runner_temp),
        GITHUB_OUTPUT=str(github_output),
    )

    # Each step gets the variables ITS OWN `env:` block declares (plus the
    # runner-provided ones), so the harness cannot hand a step a key the
    # workflow does not actually provide.
    select_env = dict(os.environ)
    select_env.update(common)
    select_env.update(DEFAULT_BRANCH=default_branch, GITHUB_REF=git_ref)
    # THE PROPERTY UNDER TEST: the selection step is given no secret. GitHub
    # populates a step's environment from its own `env:` block, but
    # `subprocess` inherits this test process's environment, so an ambient
    # value is popped explicitly rather than left to chance.
    select_env.pop("NAME_LEAK_ROSTER", None)
    if extra_path is not None:
        select_env["PATH"] = f"{extra_path}{os.pathsep}{select_env.get('PATH', '')}"

    proc_select = subprocess.run(
        [sys.executable, "-P", str(select_py)],
        cwd=work,
        env=select_env,
        capture_output=True,
        text=True,
    )
    out = proc_select.stdout + proc_select.stderr
    if proc_select.returncode != 0:
        return proc_select, out, False
    if stop_after_select:
        return proc_select, out, False

    outputs = _parse_github_output(github_output)
    sweep_py = tmp_path / "sweep.py"
    sweep_py.write_text(_step_script("sweep"), encoding="utf-8")

    sweep_env = dict(os.environ)
    sweep_env.update(common)
    sweep_env.update(
        PR_IS_FORK=is_fork,
        ATTESTATION_PATH=".github/name-leak-roster.attestation.json",
        NAME_LEAK_ROSTER=roster_value,
        SELECTED_RUN_DIR=outputs.get("run_dir", ""),
        SELECTED_SCANNER=outputs.get("scanner_path", ""),
        SCANNER_SOURCE=outputs.get("scanner_source", ""),
    )
    if extra_path is not None:
        sweep_env["PATH"] = f"{extra_path}{os.pathsep}{sweep_env.get('PATH', '')}"

    (tmp_path / "step-b-invoked").write_text("", encoding="utf-8")
    proc_sweep = subprocess.run(
        [sys.executable, "-P", str(sweep_py)],
        cwd=work,
        env=sweep_env,
        capture_output=True,
        text=True,
    )
    out += proc_sweep.stdout + proc_sweep.stderr
    return proc_sweep, out, True


def _run_sweep(work: Path, base: str, tmp_path: Path, **kwargs):
    """Run both steps; return (proc, combined output) like a single step."""
    proc, out, _sweep_ran = _run_steps(work, base, tmp_path, **kwargs)
    return proc, out


def _run_select(work: Path, base: str, tmp_path: Path, **kwargs):
    """Run ONLY the selection step; return (proc, output).

    The exit invariant — "the selection step exits non-zero on every path that
    does not select a scanner" — is a fact about THIS step's own exit code.
    Reading the combined run cannot test it: a wrongly-successful selection
    starts the sweep step, which then dies on the missing outputs and leaves
    the pair non-zero.
    """
    proc, out, _sweep_ran = _run_steps(
        work, base, tmp_path, stop_after_select=True, **kwargs
    )
    return proc, out


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


def test_a_real_scanner_with_import_time_dependencies_passes_the_base_link(
    sweep_repo, tmp_path
):
    """R3-3: the cell that would have caught the single-file materialisation.

    The base commit carries a REAL-SHAPED scanner — a loader at module import
    resolving `here.parent.parent/"build"/"engine"/...`, mirroring
    `_load_home_path_class()` — with the sibling committed at the path the
    loader expects. The same tree is on `origin/dev`, so under a single-file
    materialisation BOTH protected links fail the probe and the chain falls
    through to the checkout copy (loudly, as BOOTSTRAP).

    RED against 9e432d9b's `git show <ref>:<scanner>`; GREEN only when the
    ref's WHOLE TREE is materialised, so its own relative paths resolve.
    """
    work, _base, g = sweep_repo
    (work / "src" / "build" / "engine").mkdir(parents=True)
    (work / "src" / "build" / "engine" / "sweep_fixture_helper.py").write_text(
        "FIXTURE_HELPER\n", encoding="utf-8"
    )
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        _REAL_SHAPED_SCANNER, encoding="utf-8"
    )
    g("add", "-A")
    g("commit", "-qm", "real-shaped scanner")
    base = g("rev-parse", "HEAD").stdout.strip()
    g("push", "-q", "origin", "dev")
    g("fetch", "-q", "origin", "dev")

    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode == 0, out
    assert f"scanner read from {base}" in out, out
    assert "BOOTSTRAP" not in out, out


def test_an_extraction_failure_is_not_reported_as_a_tree_without_a_scanner(
    sweep_repo, tmp_path
):
    """R4-1: `_materialise_from` returning None means one of TWO different
    things, and they must not share a message.

    Case 1 (this cell): the extraction FAILED — `tar` exited nonzero — on a
    ref that RESOLVES (the resolution guard ran first, and `git archive`
    succeeds here; only the PATH `tar` is made to exit 1). That is a defect in
    this clone or the runner, never evidence about the tree.

    Case 2 (the chain cells above): the extraction succeeded and the tree
    genuinely has no scanner — the benign "this ref has no scanner" diagnosis.

    R5-1 moved where this lands: an extraction failure now fails closed BEFORE
    the checkout/BOOTSTRAP link, so the run never reaches the final
    "no copy ... with --roster-file" message. The distinction asserted here is
    that the extraction message is its own, never the benign one, and that the
    outcome is the fail-closed exit.
    """
    work, base, _g = sweep_repo
    failing_tar = _failing_tar_dir(tmp_path)
    proc, out = _run_sweep(work, base, tmp_path, extra_path=failing_tar)

    # The failure is not the benign diagnosis, for either protected ref.
    assert f"{base} has no src/engram/tools/scan-leaks.py" not in out, out
    assert "origin/dev has no src/engram/tools/scan-leaks.py" not in out, out
    # It says what actually happened, and names the ref.
    assert f"extracting the tree at {base} failed" in out, out
    assert (
        "extracting the tree at origin/dev failed (tar extraction of "
        "origin/dev exited 1)" in out
    ), out
    assert "this is an extraction/runner failure" in out, out
    # R5-1: fail closed before the checkout/BOOTSTRAP link, never through it.
    assert proc.returncode != 0, out
    assert "Failing closed before any scan" in out, out
    assert "BOOTSTRAP" not in out, out


def test_extraction_failure_on_a_protected_ref_fails_closed_before_bootstrap(
    sweep_repo, tmp_path
):
    """R5-1 (the blocker): when BOTH protected links fail to EXTRACT, the run
    must fail closed BEFORE the checkout/BOOTSTRAP link.

    `git archive` is made to fail for every ref, and the checkout carries a
    CAPABLE scanner that prints a marker and then reports a clean scan — the
    contributor-controlled copy, which the old chain ran with
    `--roster-file <path>` and a GREEN job. Here the run must exit nonzero,
    never print BOOTSTRAP, and never run the marker.
    """
    work, base, _g = sweep_repo
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        _CAPABLE_MARKER_SCANNER, encoding="utf-8"
    )
    failing_git = _failing_archive_git_dir(tmp_path)
    proc, out = _run_sweep(work, base, tmp_path, extra_path=failing_git)

    # The loud diagnosis names each ref and the extraction failure it hit.
    assert f"extracting the tree at {base} failed" in out, out
    assert "extracting the tree at origin/dev failed" in out, out
    assert "Failing closed before any scan" in out, out
    # The security property: the checkout link is never taken, so the
    # contributor's scanner never sees `--roster-file`.
    assert proc.returncode != 0, out
    assert "BOOTSTRAP" not in out, out
    assert "CHECKOUT_SCANNER_RAN" not in out, out


def test_the_git_archive_failure_arm_is_exercised_and_fails_closed(
    sweep_repo, tmp_path
):
    """R5-3: the `archive.returncode != 0` arm of `_materialise_from` had no
    cell — making it dead left every test green (open finding 1).

    The `git` shim fails `archive` and delegates every other subcommand, so the
    resolution guard, the base-commit read of `tools/name_leak_gate.py` and the
    fetch all still work; only the extraction fails, and it fails at the
    `git archive` step rather than at `tar` (which `_failing_tar_dir` covers).
    With R5-1 the run then fails closed instead of reaching the checkout's
    capable scanner.
    """
    work, base, _g = sweep_repo
    failing_git = _failing_archive_git_dir(tmp_path)
    proc, out = _run_sweep(work, base, tmp_path, extra_path=failing_git)

    # Named arm, not the tar arm: the message is `git archive <ref> exited 1`.
    assert f"git archive {base} exited 1" in out, out
    assert "git archive origin/dev exited 1" in out, out
    assert "Failing closed before any scan" in out, out
    assert proc.returncode != 0, out
    assert "BOOTSTRAP" not in out, out


def test_a_crashing_probe_blocks_the_checkout_link(sweep_repo, tmp_path):
    """R6-2 (the blocker): a probe that DIES is not a copy that gave a
    NEGATIVE verdict.

    Clio's reproduction one step after round 5: a scanner that raises at
    import at `base_sha` AND at `origin/dev`, with a capable copy in the
    checkout. `returncode == 0 and ...` read both crashes as "lacks
    --roster-file", took the checkout link, and ran the contributor-controlled
    copy with `--roster-file <path>` — exit 0, BOOTSTRAP, green.

    RED against 2da33562. The three verdicts must stay three: could-not-
    examine is recorded against the ref and fails closed before any scan.
    """
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _CRASHING_SCANNER, push=True)
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        _CAPABLE_MARKER_SCANNER, encoding="utf-8"
    )
    proc, out = _run_sweep(work, base, tmp_path)

    assert proc.returncode != 0, out
    assert "BOOTSTRAP" not in out, out
    assert "CHECKOUT_SCANNER_RAN" not in out, out
    # Diagnosed as a could-not-examine condition, naming both refs and the
    # failure that happened ...
    assert "could not examine" in out, out
    assert "its --help probe exited 1" in out, out
    assert base in out, out
    assert "origin/dev" in out, out
    # ... never as the benign "<ref>'s scanner lacks --roster-file" verdict.
    assert "'s src/engram/tools/scan-leaks.py lacks --roster-file" not in out, out


def test_a_crashing_checkout_probe_is_not_reported_as_lacking(
    sweep_repo, tmp_path
):
    """R6-3: the checkout link's own crash must not be described as a copy
    that LACKS the flag.

    The outcome here was already safe — the checkout is the LAST link, so a
    crashed probe and a well-formed "lacks" both end at the same fail-closed
    exit. The DIAGNOSIS was not: throwing the
    verdict away (`...[0] != PROBE_CAPABLE`) appended "lacks --roster-file"
    for a copy the run could not examine, which is the no-verdict-read-as-
    negative sentence this round exists to remove.

    Setup: both protected refs carry a WELL-FORMED incapable scanner (exit 0,
    no flag), so the chain legitimately reaches the checkout link; the
    checkout copy then crashes at import.

    RED against fa1ae6d2 on the checkout assertions — the run already failed
    closed there, but it said the checkout copy LACKED the flag.
    """
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _INCAPABLE_SCANNER, push=True)
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        _CRASHING_SCANNER, encoding="utf-8"
    )
    proc, out = _run_sweep(work, base, tmp_path)

    # Outcome unchanged: fail closed before any scan.
    assert proc.returncode != 0, out
    assert "scan-leaks: scanned" not in out, out
    assert "scanner read from" not in out, out
    # The protected refs genuinely lack the flag, and still say so.
    assert f"{base}'s src/engram/tools/scan-leaks.py lacks --roster-file" in out, out
    # The CHECKOUT's crash is a could-not-examine, in its own sentence ...
    assert (
        "could not examine the pull request checkout's copy of "
        "src/engram/tools/scan-leaks.py" in out
    ), out
    assert "its --help probe exited 1" in out, out
    # ... never the false "the checkout's copy lacks the flag" sentence.
    assert (
        "the pull request checkout's src/engram/tools/scan-leaks.py "
        "lacks --roster-file" not in out
    ), out


def test_a_hanging_probe_times_out_as_could_not_examine(sweep_repo, tmp_path):
    """R6-1 (the bound): a copy that hangs on `--help` must be DIAGNOSED, not
    left to hang the job until the 10-minute job ceiling.

    The harness substitutes a 1s bound so the cell costs ~2s (two links); the
    `TimeoutExpired` arm, its own message and the fail-closed routing are the
    ones the shipped bound reaches. The message echoes the bound, which is
    what proves the substituted value reached the probe rather than some
    other timeout.
    """
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _HANGING_SCANNER, push=True)
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        _CAPABLE_MARKER_SCANNER, encoding="utf-8"
    )
    proc, out = _run_sweep(work, base, tmp_path, probe_timeout_seconds=1)

    assert proc.returncode != 0, out
    assert "BOOTSTRAP" not in out, out
    assert "CHECKOUT_SCANNER_RAN" not in out, out
    assert "could not examine" in out, out
    assert "did not finish within 1s" in out, out
    assert "'s src/engram/tools/scan-leaks.py lacks --roster-file" not in out, out


# ── R7: the roster never exists while a probe runs ─────────────────────────

def test_a_non_capable_bootstrap_verdict_never_starts_the_sweep(
    sweep_repo, tmp_path
):
    """#4782 r7 (the exit invariant): when the chain reaches the BOOTSTRAP link
    and its copy is NOT capable, the selection step must exit non-zero — so the
    secret-bearing step never starts.

    The code that ran in the BOOTSTRAP branch is contributor-controlled, and it
    can leave state for a later step to inherit: a `GITHUB_ENV` line (an
    `LD_PRELOAD` or `PYTHONSTARTUP` applies to the next step's interpreter
    before it runs a line), a `GITHUB_PATH` entry, a `GITHUB_OUTPUT` line, or
    files under the run's temp dir. If the sweep step started after a
    non-capable verdict it would begin with the roster in its environment AND
    that state: finding 1, one step later.

    Asserted on the SELECTION STEP'S OWN EXIT CODE as well as on the boundary,
    because the combined run cannot tell the two apart (a wrongly-successful
    selection makes the sweep step die on empty outputs).
    """
    work, _base, g = sweep_repo
    # Both protected refs carry a scanner that lacks `--roster-file`, and so
    # does the checkout: the chain reaches the BOOTSTRAP link and gets a
    # non-capable verdict there.
    base = _commit_scanner(work, g, _INCAPABLE_SCANNER)
    g("push", "-q", "origin", "dev")
    g("fetch", "-q", "origin", "dev")
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        _INCAPABLE_SCANNER, encoding="utf-8"
    )

    select_proc, select_out = _run_select(work, base, tmp_path)
    assert select_proc.returncode != 0, select_out
    assert (
        "no copy of src/engram/tools/scan-leaks.py with --roster-file"
        in select_out
    ), select_out
    assert _roster_files(tmp_path) == [], (
        "the selection step must never create a roster file"
    )

    proc, out, sweep_ran = _run_steps(work, base, tmp_path)
    assert proc.returncode != 0, out
    assert sweep_ran is False, (
        "the sweep step started after a selection that selected no scanner"
    )
    assert "scan-leaks: scanned" not in out, out
    assert _roster_files(tmp_path) == []


def test_step_a_fails_closed_when_the_checkout_probe_crashes(sweep_repo, tmp_path):
    """#4782 r7 (the exit invariant, second shape): a CRASHING checkout probe is
    still "no scanner selected", so the selection step must exit non-zero and
    the sweep step must never start.

    The crash is the no-verdict condition the chain records against the link;
    the run must not fall through with a scanner it did not select.
    """
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _INCAPABLE_SCANNER, push=True)
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        _CRASHING_SCANNER, encoding="utf-8"
    )

    select_proc, select_out = _run_select(work, base, tmp_path)
    assert select_proc.returncode != 0, select_out
    assert (
        "could not examine the pull request checkout's copy of "
        "src/engram/tools/scan-leaks.py" in select_out
    ), select_out
    assert _roster_files(tmp_path) == []

    proc, out, sweep_ran = _run_steps(work, base, tmp_path)
    assert proc.returncode != 0, out
    assert sweep_ran is False, (
        "the sweep step started after a selection that selected no scanner"
    )
    assert "scan-leaks: scanned" not in out, out
    assert _roster_files(tmp_path) == []


def test_step_a_fails_closed_when_a_protected_ref_cannot_be_examined(
    sweep_repo, tmp_path
):
    """#4782 r7 (the exit invariant, `unexamined` gate): a protected ref whose
    capability probe produces NO verdict must exit the selection step
    non-zero, so the secret-bearing step never starts.

    This is the gate's OWN cell, not a pair-read: if it wrongly returned 0, the
    sweep step would start on empty outputs and die, leaving the combined run
    non-zero and the violation invisible. The invariant lives in this step's
    exit code, so the assertion is on the exit code.

    Both protected refs carry a scanner that crashes at import; the checkout
    carries a CAPABLE copy, which the chain must never reach on the strength of
    a ref this run could not examine.
    """
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _CRASHING_SCANNER, push=True)
    (work / "src" / "engram" / "tools" / "scan-leaks.py").write_text(
        _CAPABLE_MARKER_SCANNER, encoding="utf-8"
    )

    select_proc, select_out = _run_select(work, base, tmp_path)
    assert select_proc.returncode != 0, select_out
    assert "Failing closed before any scan" in select_out, select_out
    assert _roster_files(tmp_path) == []

    proc, out, sweep_ran = _run_steps(work, base, tmp_path)
    assert proc.returncode != 0, out
    assert sweep_ran is False, (
        "the sweep step started after a selection that selected no scanner"
    )
    assert "CHECKOUT_SCANNER_RAN" not in out, out
    assert "scan-leaks: scanned" not in out, out
    assert _roster_files(tmp_path) == []


def test_the_sweep_step_writes_the_roster_file_and_removes_it(
    sweep_repo, tmp_path
):
    """#4782 r7: the roster file is created in the SWEEP step, the scanner sees
    it at `--roster-file` during the scan, and it is gone afterwards.

    The observer scanner reports what it was handed from INSIDE the scan, so
    "the file existed" is proven rather than assumed: a step that never wrote
    it would still scan clean, which is not the same run.
    """
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _OBSERVER_SCANNER)
    proc, out = _run_sweep(work, base, tmp_path)

    assert proc.returncode == 0, out
    assert "ROSTER_FILE_IS_FILE_AT_SCAN=True" in out, out
    assert f"ROSTER_TEXT_AT_SCAN={_ROSTER.strip()}" in out, out
    assert _roster_files(tmp_path) == [], "the roster file outlived the sweep step"


def test_the_roster_file_is_removed_when_the_scan_raises(sweep_repo, tmp_path):
    """#4782 r7: the removal is in a `finally`, so it happens on the exception
    path too.

    The scanner emits invalid UTF-8 during the scan; the step reads the child's
    output with `text=True`, so `subprocess.run` raises UnicodeDecodeError
    AFTER the roster file exists. (The `--help` output stays valid, or the probe
    — which also decodes — would fail and the chain would never select it.)
    """
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _INVALID_UTF8_SCANNER)
    proc, out = _run_sweep(work, base, tmp_path)

    assert proc.returncode != 0, out
    assert "UnicodeDecodeError" in out, out
    assert _roster_files(tmp_path) == [], (
        "the roster file survived an exception raised by the scan"
    )


def test_the_scanned_tree_is_still_the_checkout(sweep_repo, tmp_path):
    """#4782 r7: the subject of the scan does not change in the split — it is
    the CHECKOUT (on a pull request, GitHub's merge ref), never the head commit
    and never one of the extracted protected trees.

    Asserted twice: the observer scanner reports the `--root` it was handed,
    and the checkout step must not pin a `ref:` that would make the workspace
    something other than the merge ref.
    """
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _OBSERVER_SCANNER)
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode == 0, out
    assert f"ROOT_AT_SCAN={work}" in out, out

    # The scan invocation takes the root from GITHUB_WORKSPACE ...
    sweep_run = _sweep_step(_workflow())["run"]
    start = sweep_run.index("scan = subprocess.run(")
    invocation = sweep_run[start:sweep_run.index("capture_output=True", start)]
    assert '"--root"' in invocation
    assert 'os.environ["GITHUB_WORKSPACE"]' in invocation

    # ... and the checkout that sets GITHUB_WORKSPACE is not pinned to the
    # pull request's head commit (the merge ref is what checkout defaults to).
    checkout = next(
        step
        for step in _job(_workflow())["steps"]
        if "checkout" in (step.get("uses") or "")
    )
    assert "ref" not in (checkout.get("with") or {}), (
        "pinning the checkout to a ref would change the scan's subject away "
        "from GitHub's merge ref"
    )


def test_the_probe_and_scan_do_not_inherit_the_roster_secret(sweep_repo, tmp_path):
    """R3-2 / R5-2 / R7: the roster reaches the scanner through --roster-file.
    The environment copy is a second path to the secret — the one a
    contributor-controlled copy would read — so both call sites strip it.

    After the split this is DEFENCE-IN-DEPTH, not the boundary (#4782 r7): the
    probe runs in the selection step, which holds no secret to strip, so its
    stripping is no longer observable here. The SCAN call site is, and its stub
    prints SCAN_SAW_ROSTER and exits 3 on a leak. The probe call site is pinned
    at the SOURCE instead of claimed as a behaviour.
    """
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _ENV_PROBE_SCANNER)
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode == 0, out
    # The base copy qualified and supplied the scanner.
    assert f"scanner read from {base}" in out, out
    assert "SCAN_SAW_ROSTER" not in out, out

    # Both call sites keep the stripping (defence-in-depth at the probe site,
    # observable behaviour at the scan site).
    assert "env=_scanner_env()" in _step_script("select"), (
        "the probe's environment must keep stripping the roster name"
    )
    assert "env=_scanner_env()" in _step_script("sweep"), (
        "the scan's environment must keep stripping the roster name"
    )


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


def test_a_hit_does_not_tell_a_home_path_hit_to_edit_the_roster(
    sweep_repo, tmp_path
):
    """R3-5: the roster instruction is irrelevant to an absolute-home-path hit,
    and this branch cannot tell the two families apart (both are exit 1), so
    the type-specific duty belongs to the scanner, which knows the type. The
    workflow's own message must not carry the roster instruction at all."""
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _HIT_SCANNER)
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode != 0, out
    assert "a hit means" in out, out
    assert "do not widen or edit the roster" not in out, out


# ── exit 3 is coverage incomplete: not a hit, and never a green tick ────────

def test_sweep_reports_exit_3_as_coverage_incomplete(sweep_repo, tmp_path):
    """No findings + exit 3 → the sweep fails and names coverage, not a leak.

    The scanner read a valid, non-zero summary and found nothing, but it could
    not read every candidate. A tick here would claim coverage the run did not
    have, and the hit message would claim a leak it did not find.
    """
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _COVERAGE_INCOMPLETE_SCANNER)
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode != 0, out
    assert "did not cover the whole tree (coverage incomplete)" in out, out
    assert "a hit means" not in out, out


def test_a_hit_is_not_reported_as_coverage_incomplete(sweep_repo, tmp_path):
    """Positive control for the exit-3 arm: exit 1 still takes the hit message,
    so the new branch did not swallow the neighbouring one."""
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _HIT_SCANNER)
    proc, out = _run_sweep(work, base, tmp_path)
    assert proc.returncode != 0, out
    assert "a hit means" in out, out
    assert "did not cover the whole tree (coverage incomplete)" not in out, out


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
            "roster_format": 2,
            "pattern_count_a": 1,
            "pattern_count_b": 0,
            "roster_sha256": "0" * 64,
            "updated_at": "2026-11-15T00:00:00Z",
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

def test_a_scanner_that_refuses_the_roster_fails_closed(sweep_repo, tmp_path):
    """An UNCOMPILABLE regex is the scanner's call: the workflow's validation
    block checks only the textual class prefix and compiles no roster line
    (cell 11 pins that). A scanner that refuses the roster exits 2, and the
    sweep must fail closed on it — never read it as a hit or a pass — with the
    roster text absent from the log."""
    token = "qvollmex3311"
    work, _base, g = sweep_repo
    base = _commit_scanner(work, g, _ROSTER_REFUSED_SCANNER)
    roster = "A\t\\bQuillonfex\\b\nA\t(?P=" + token + ")\n"
    proc, out = _run_sweep(
        work,
        base,
        tmp_path,
        roster_value=roster,
        # A matching attestation, so the sweep reaches the scanner: the point
        # of this cell is the exit-2 arm, not the decision.
        attestation=_format2_attestation(
            pattern_count_a=2,
            roster_sha256=_canonical_sha(roster),
        ),
    )
    assert proc.returncode != 0, out
    # It exits 2 without printing the frozen summary line, so the "did not run"
    # arm fails the sweep closed; it never reads as a hit or a pass.
    assert "did not run" in out, out
    assert "a hit means" not in out, out
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


# ---------------------------------------------------------------------------
# Format 2 — the attestation schema, the grammar check, one parser
# (spec roster-format2-loader §1, §2.5, §4 cells 10-12)
# ---------------------------------------------------------------------------
#
# Contract (written before the implementation):
#   - the attestation is `roster_format: 2` + `pattern_count_a` +
#     `pattern_count_b` + the whole-file sha; both counts must be integers >= 0,
#     the sha must be 64 hex, and the two counts are compared with what the
#     SCANNER reports after parsing. A mismatch of either count fails closed.
#   - the validation block compiles NO roster regex and invokes no scanner: it
#     asserts over the raw roster text that every non-blank non-comment line
#     starts with `A<TAB>` or `B<TAB>`, and fails inside its own step before
#     any scan.
#   - `load_roster_file` is the only parser of the format-2 grammar.

_EXPECTED_UPDATED_AT = "2026-11-15T00:00:00Z"


def _format2_attestation(**overrides) -> dict:
    """The fixture's attestation, matching `_ROSTER`, with overrides applied."""
    attestation = {
        "roster_format": 2,
        "pattern_count_a": 1,
        "pattern_count_b": 0,
        "roster_sha256": _canonical_sha(_ROSTER),
        "updated_at": _EXPECTED_UPDATED_AT,
    }
    attestation.update(overrides)
    return attestation


def test_cell10_matching_format2_attestation_passes(sweep_repo, tmp_path):
    """Cell 10a: schema 2 + both counts matching + the sha matching -> success,
    and the run reports the parsed class split."""
    work, base, _g = sweep_repo
    proc, out = _run_sweep(work, base, tmp_path,
                           attestation=_format2_attestation())
    assert proc.returncode == 0, out
    assert "against 1 class-A and 0 class-B roster pattern(s)" in out, out


@pytest.mark.parametrize(
    "field, value",
    [
        ("pattern_count_a", 2),
        ("pattern_count_b", 1),
    ],
)
def test_cell10_a_count_off_by_one_fails_closed(
    sweep_repo, tmp_path, field, value
):
    """Cell 10b: an attested per-class count that the parsed roster does not
    reproduce fails closed — the counts are compared inside the scan step,
    where the scanner has already parsed the roster."""
    work, base, _g = sweep_repo
    proc, out = _run_sweep(
        work, base, tmp_path,
        attestation=_format2_attestation(**{field: value}),
    )
    assert proc.returncode != 0, out
    assert "do not match the parsed roster" in out, out


def test_cell10_roster_format_1_fails_closed(sweep_repo, tmp_path):
    """Cell 10c: a format-1 attestation is not a schema-2 attestation. It is
    treated as absent and fails closed, never as 'close enough'."""
    work, base, _g = sweep_repo
    proc, out = _run_sweep(
        work,
        base,
        tmp_path,
        attestation={
            "roster_format": 1,
            "pattern_count": 1,
            "roster_sha256": _canonical_sha(_ROSTER),
        },
    )
    assert proc.returncode != 0, out
    assert "attestation absent or invalid" in out, out


def test_cell11_the_validation_block_is_a_text_check_before_any_scan():
    """Cell 11 (text/ordering): the roster validation the workflow performs is
    a pure text predicate over `A<TAB>`/`B<TAB>` — it compiles no roster regex
    and invokes no scanner — and it sits BEFORE the scan invocation, so a bad
    class letter fails there and no scan runs."""
    run = _step_script("sweep")

    assert 'startswith("A\\t")' in run, run
    assert 'startswith("B\\t")' in run, run
    assert "re.compile" not in run, (
        "the workflow must not compile roster regexes: that is the scanner's "
        "loader's job, and a compile over roster lines is contributor-shaped "
        "code near the secret"
    )
    assert 'split("\\t"' not in run, (
        "the workflow must not PARSE the class token; it only asserts the "
        "prefix, so the grammar has exactly one definition"
    )

    validation = min(
        run.index('startswith("A\\t")'), run.index('startswith("B\\t")')
    )
    scan_call = run.index("scan = subprocess.run(")
    assert validation < scan_call, (
        "the grammar check must run before the scan invocation"
    )


def test_cell11_a_bad_class_letter_fails_before_any_scan(sweep_repo, tmp_path):
    """Cell 11 (behavioural companion): a roster whose line 3 is `Z<TAB>x`
    fails with the line number and the prefix instruction, and the clean
    scanner stub's summary never appears — no scan ran."""
    work, base, _g = sweep_repo
    roster = "A\t\\bQuillonfex\\b\nB\tok\nZ\tx\n"
    proc, out = _run_sweep(work, base, tmp_path, roster_value=roster)
    assert proc.returncode != 0, out
    assert "line 3" in out, out
    assert "A<TAB>" in out and "B<TAB>" in out, out
    assert "scan-leaks: scanned" not in out, out


def test_cell12_exactly_one_roster_grammar_parser():
    """Cell 12: the format-2 grammar has exactly one definition in the repo.
    The class token is split only in the scanner's loader; the workflow holds a
    prefix assertion (no split, no compile), so a second parser reddens this."""
    scanner = (REPO / "src" / "engram" / "tools" / "scan-leaks.py").read_text(
        encoding="utf-8"
    )
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert scanner.count("def load_roster_file(") == 1, (
        "the loader is the single parser"
    )
    assert scanner.count('.split("\\t", 1)') == 1, (
        "the class token must be split in exactly one place"
    )
    assert '.split("\\t"' not in workflow, (
        "a split in the workflow would be a second parser"
    )
    assert "re.compile" not in workflow, (
        "a regex compile over roster lines in the workflow would be a second "
        "implementation of the grammar"
    )


@pytest.mark.parametrize("field", ["pattern_count_a", "pattern_count_b"])
def test_cell17_a_boolean_attestation_count_fails_closed(
    sweep_repo, tmp_path, field
):
    """Cell 17: `pattern_count_a: true` (a JSON bool) is not an integer count.

    `isinstance(True, int)` is True in Python, so the schema check alone lets a
    bool through — and `True == 1`, so a one-pattern class even passes the
    count comparison. The explicit bool guard is what fails it closed. The
    second parameter covers the sibling field, which needs the same guard.
    """
    work, base, _g = sweep_repo
    proc, out = _run_sweep(
        work, base, tmp_path,
        attestation=_format2_attestation(**{field: True}),
    )
    assert proc.returncode != 0, out
    assert "attestation absent or invalid" in out, out


# ---------------------------------------------------------------------------
# T13 (engram#4817): the sweep did NOT opt into format inference
# ---------------------------------------------------------------------------

def test_t13_the_sweep_still_refuses_a_format_1_roster(sweep_repo, tmp_path):
    """The whole-tree sweep's roster must still be format 2.

    The sweep's own grammar check is a pure text predicate over the raw
    roster; it fires before the scanner is invoked. The scanner's default
    (`require_classes=True`) is the same rule one level down, so a loosened
    default must never become reachable from this workflow — a format-1
    roster here is a failure, and no scan runs.
    """
    work, base, _g = sweep_repo
    roster = "\\bQuillonfex\\b\n"
    proc, out = _run_sweep(work, base, tmp_path, roster_value=roster)
    assert proc.returncode != 0, out
    assert "does not start with A<TAB> or B<TAB>" in out, out
    assert "scan-leaks: scanned" not in out, out
