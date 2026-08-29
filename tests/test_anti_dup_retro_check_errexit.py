"""Tests for .github/workflows/anti-dup-retro-check.yml's errexit safety (#2366).

The workflow's own closing comment states a contract: "Always exit 0 --
this workflow is informational + retroactive. It should not fail the
triggering (already-merged) PR." But two `VAR=$(gh ...)` assignments used
to be unguarded, and this step runs under GitHub Actions' default `bash -e`
(errexit) -- a failing `gh api`/`gh pr comment` call would terminate the
whole step immediately, silently skipping every remaining orphan in the
loop and never reaching the `else` branch each one has specifically to
report the failure.

These tests extract the ACTUAL shipped bash from the workflow YAML (via
PyYAML, not a hand-copied duplicate that could drift from the real file)
and run each of the fixed fragments in isolation under `bash -e` with
a stub `gh` on PATH that fails every call -- the exact failure shape the
issue describes. A regression (removing any fix) makes the corresponding
test fail: the fragment either exits non-zero (proving errexit fired) or
never reaches its own trailing `echo DONE` marker.

A third fragment (`OPEN_PRS_JSON`, upstream of the whole orphan-scanning
loop) was added to the same fix and this test file after a reviewer-fairy
completeness sweep found it carries the identical unguarded-assignment
shape as the other two, sitting even earlier in the same step.

`OPEN_PRS_JSON`'s fix took a second pass after a colleague-review finding
(Borges, PR#2375): the first cut used `|| true`, matching CHECK_RESULT --
but combined with the existing `2>/dev/null`, that made a genuine `gh pr
list` failure silently indistinguishable from the correct "no open PRs"
case, converting the errexit fix's own failure mode from loud (crash) to
silent (looks like success). Fixed to match this file's own established
`if ! VAR=$(...); then warn; exit 0; fi` idiom (see `ISSUE_JSON` in the
real workflow) instead -- still exit-0, but now loud about which case
actually happened. Two tests below distinguish the two cases this fix
must tell apart: a genuine `gh` failure (warns) vs. a genuinely empty
open-PR list (silently, correctly, says so and exits 0).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "anti-dup-retro-check.yml"

OPEN_PRS_START = "# Fetch all open PRs"
OPEN_PRS_END = "PR_COUNT=$(printf"
CHECK_START = "# ── Post failing check-run"
CHECK_END = "# ── Post PR comment"
COMMENT_START = "# ── Post PR comment"
COMMENT_END = 'done <<< "$ORPHANS"'


def _extract_run_script() -> str:
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for step in data["jobs"]["anti-dup-retro"]["steps"]:
        if step.get("name") == "Identify orphaned PRs and post failing gates":
            return step["run"]
    raise AssertionError(
        "could not find the target step in anti-dup-retro-check.yml -- "
        "has the step name changed?"
    )


def _extract_fragment(script: str, start_anchor: str, end_anchor: str) -> str:
    start = script.index(start_anchor)
    end = script.index(end_anchor, start + 1)
    return script[start:end]


def _make_failing_gh(tmp_path: Path) -> None:
    """A stub `gh` on PATH that fails every call it's given, printing an
    error body to stdout (mirroring `gh`'s real behavior on an API error,
    per this file's own documented lesson about error bodies landing on
    stdout) and exiting 1. Deliberately undiscriminating between `gh api`
    and `gh pr comment` -- both fragments under test must survive an
    unconditional failure."""
    gh_path = tmp_path / "gh"
    gh_path.write_text(
        "#!/bin/sh\n"
        'echo \'{"message": "stub failure (test double, #2366)"}\'\n'
        "exit 1\n"
    )
    gh_path.chmod(gh_path.stat().st_mode | stat.S_IEXEC)


def _make_failing_gh_stderr_only(tmp_path: Path) -> None:
    """A stub `gh` that fails with its error on STDERR and empty stdout --
    matching `gh pr list`'s own real failure shape (unlike `gh api`, which
    prints structured error bodies to stdout on HTTP errors; `gh pr list`
    is a friendlier CLI wrapper whose errors go to stderr). The real
    `OPEN_PRS_JSON` line redirects stderr to /dev/null, so this is the
    accurate simulation: empty captured stdout, non-zero exit."""
    gh_path = tmp_path / "gh"
    gh_path.write_text(
        "#!/bin/sh\n"
        "echo 'stub failure (test double, #2366)' >&2\n"
        "exit 1\n"
    )
    gh_path.chmod(gh_path.stat().st_mode | stat.S_IEXEC)


def _make_empty_list_gh(tmp_path: Path) -> None:
    """A stub `gh` that SUCCEEDS with a genuinely empty PR list -- the
    correct "no open PRs" case, distinct from a `gh` failure. Used to
    confirm the fix's `if ! VAR=$(...)` restructure doesn't accidentally
    treat success-with-empty-result as a failure too."""
    gh_path = tmp_path / "gh"
    gh_path.write_text("#!/bin/sh\necho '[]'\nexit 0\n")
    gh_path.chmod(gh_path.stat().st_mode | stat.S_IEXEC)


def _run_fragment(
    fragment: str,
    tmp_path: Path,
    extra_vars: dict,
    stderr_only_failure: bool = False,
    empty_list_success: bool = False,
) -> subprocess.CompletedProcess:
    if empty_list_success:
        _make_empty_list_gh(tmp_path)
    elif stderr_only_failure:
        _make_failing_gh_stderr_only(tmp_path)
    else:
        _make_failing_gh(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    var_lines = "\n".join(f'{k}="{v}"' for k, v in extra_vars.items())
    # `set -e`, same as GitHub Actions' default shell for a `run:` step.
    # The trailing `echo DONE` is the marker proving execution reached the
    # fragment's own end rather than being cut short by errexit partway
    # through.
    script = f"set -e\n{var_lines}\n{fragment}\necho DONE\n"
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=10
    )


def test_open_prs_fetch_survives_gh_pr_list_failure_LOUDLY(tmp_path: Path) -> None:
    """#2366 completeness sweep + colleague-review follow-up (Borges,
    PR#2375): a failing `gh pr list` here must not terminate the step
    under errexit, AND must not be silently misreported as "no open PRs"
    -- the trap the first-cut `|| true` fix introduced (confirmed by
    Borges via a stubbed failing gh: indistinguishable from a clean empty
    scan). The fix matches this file's own established `if ! VAR=$(...)`
    idiom instead: a genuine gh failure must print the `::warning::`
    line and exit 0, distinct from the true-empty-list case (see the
    sibling test below)."""
    fragment = _extract_fragment(_extract_run_script(), OPEN_PRS_START, OPEN_PRS_END)
    result = _run_fragment(
        fragment, tmp_path, {"REPO": "test-owner/test-repo"}, stderr_only_failure=True
    )
    assert result.returncode == 0, (
        f"fragment terminated early under errexit (exit {result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "::warning::anti-dup-retro: could not fetch open PRs" in result.stdout, (
        f"a real gh pr list failure must warn loudly, not silently read as "
        f"'no open PRs' -- got: {result.stdout!r}"
    )
    assert "No open PRs to scan." not in result.stdout, (
        "a real gh failure must not be misreported as the true-empty-list case"
    )


def test_open_prs_fetch_reports_genuinely_empty_list_quietly(tmp_path: Path) -> None:
    """Sibling to the test above: when `gh pr list` genuinely SUCCEEDS
    with an empty array (the correct, ordinary "no open PRs" case), the
    fix's `if ! VAR=$(...)` restructure must not misclassify success as
    failure -- it should fall through to the existing `[]` check and
    print the quiet, expected message, not a warning."""
    fragment = _extract_fragment(_extract_run_script(), OPEN_PRS_START, OPEN_PRS_END)
    result = _run_fragment(
        fragment, tmp_path, {"REPO": "test-owner/test-repo"}, empty_list_success=True
    )
    assert result.returncode == 0, (
        f"fragment terminated early under errexit (exit {result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "No open PRs to scan." in result.stdout, result.stdout
    assert "::warning::" not in result.stdout, (
        "a genuinely empty PR list must not be reported as a gh failure"
    )


def test_check_run_post_survives_gh_api_failure(tmp_path: Path) -> None:
    """#2366: a failing `gh api ... check-runs` POST must not terminate the
    step under errexit -- the `else` branch (FAILED -- ...) must be
    reached and the fragment must still reach its own end."""
    fragment = _extract_fragment(_extract_run_script(), CHECK_START, CHECK_END)
    result = _run_fragment(
        fragment,
        tmp_path,
        {
            # A placeholder, deliberately -- the stub `gh` ignores every
            # argument it's given, so the real repo name adds nothing here.
            # Reviewer-fairy finding: the real name tripped scan-leaks.py's
            # private-dev-repo-URL pattern, which forbids this exact string
            # repo-wide with no test-fixture exemption.
            "REPO": "test-owner/test-repo",
            "ORPHAN_SHA": "deadbeef",
            "ORPHAN_NUM": "999",
            "N": "1",
            "MERGED_PR": "2",
        },
    )
    assert result.returncode == 0, (
        f"fragment terminated early under errexit (exit {result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "check-run on #999: FAILED" in result.stdout, result.stdout
    assert "DONE" in result.stdout, "fragment never reached its own end -- errexit fired"


def test_comment_post_survives_gh_pr_comment_failure(tmp_path: Path) -> None:
    """#2366: a failing `gh pr comment` must not terminate the step under
    errexit, AND must not be misreported as OK -- the trap a bare
    `|| true` would introduce here specifically, since this dispatch is
    exit-code-based (`if COMMENT_RESULT=$(...)`), not content-based."""
    fragment = _extract_fragment(_extract_run_script(), COMMENT_START, COMMENT_END)
    result = _run_fragment(
        fragment,
        tmp_path,
        {
            # A placeholder, deliberately -- the stub `gh` ignores every
            # argument it's given, so the real repo name adds nothing here.
            # Reviewer-fairy finding: the real name tripped scan-leaks.py's
            # private-dev-repo-URL pattern, which forbids this exact string
            # repo-wide with no test-fixture exemption.
            "REPO": "test-owner/test-repo",
            "ORPHAN_NUM": "999",
            "MERGED_PR": "2",
            "N": "1",
        },
    )
    assert result.returncode == 0, (
        f"fragment terminated early under errexit (exit {result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "comment on #999: FAILED" in result.stdout, result.stdout
    assert "comment posted on #999: OK" not in result.stdout, (
        "a real failure was misreported as OK -- the exit-code dispatch was defeated"
    )
    assert "DONE" in result.stdout, "fragment never reached its own end -- errexit fired"
