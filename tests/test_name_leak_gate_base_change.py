"""The name-leak gate must re-run when a pull request's BASE BRANCH changes.

What the workflow scans is the pull request's own diff:

    git diff "${BASE_SHA}"...HEAD > "${RUN_DIR}/pr.diff"

so retargeting a pull request (dev -> main, say) replaces the entire subject of
the scan. Before this test existed the trigger was a bare `pull_request:`, which
GitHub expands to its default activity types — `opened`, `synchronize`,
`reopened`. `edited` is not among them, and a retarget fires only `edited`. The
gate therefore did not re-run, and because check runs attach to the head commit
rather than to the base, the previous success kept standing over a diff that had
completely changed. `engram-agents/engram#4619` sat in exactly that state on
2026-11-11.

Two independent things make the old result invalid after a retarget:

1. the diff changed, and
2. the gate reads its own tool code from `BASE_SHA`, so a new base is a
   different scanner version as well.

There is a second half, and it is why the job carries NO skip. A job-level `if:`
that skips the job on a non-base edit still reports under this job's `name`,
with conclusion `skipped`, and GitHub counts `skipped` as satisfied. Check runs
key on the head commit, so the skip SUPERSEDES the previous run on that head
instead of sitting beside it: a red gate becomes a grey one the moment someone
fixes a typo in the title. Measured on `engram-agents/ci-probes#6` at head
`1f7aacfe` — two failing runs, then a title edit, after which `gh pr checks`
showed a single `name-leak-gate skipping` line and both reds were gone from the
pull request's check list. Running on every `edited` re-scans and reproduces the
true verdict, at the cost of one ~11 s runner start per edit.

These tests pin the WIRING. They cannot run a GitHub event, so they do NOT prove
the gate actually re-runs on a retarget: that was measured by a colleague in
`engram-agents/ci-probes` and the run ids are recorded in the pull request that
added this file. What these tests do is fail the moment someone tidies the
`types:` list back to the default set, or re-adds a job-level skip that would let
a quiet `skipped` supersede a red.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "name-leak-gate.yml"
)

# GitHub's default pull_request activity types, i.e. what a bare
# `pull_request:` subscribes to. Pinned here so the test states what it is
# adding to rather than comparing against an unstated baseline.
GITHUB_DEFAULT_TYPES = {"opened", "synchronize", "reopened"}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(wf: dict) -> dict:
    """The `on:` mapping.

    PyYAML implements YAML 1.1, where a bare `on` is the boolean `True`, so the
    key is `True` (or the string) depending on the loader. Accept both, and fail
    loudly rather than silently reading an empty dict.
    """
    for key in ("on", True):
        if key in wf:
            value = wf[key]
            assert isinstance(value, dict), (
                f"`on:` should be a mapping so the triggers can be asserted; got {type(value).__name__}"
            )
            return value
    raise AssertionError("no `on:` key found in the workflow")


def test_pull_request_subscribes_to_edited():
    """A base-branch retarget fires only `edited`; without it the gate is silent."""
    pr = _triggers(_workflow())["pull_request"]

    assert isinstance(pr, dict), (
        "`pull_request:` must be a mapping carrying `types:` — a bare "
        "`pull_request:` gets GitHub's default activity types, which do not "
        "include `edited`, so a base-branch retarget never re-runs the gate."
    )
    types = pr.get("types")
    assert isinstance(types, list) and types, (
        f"`pull_request.types` must be an explicit list; got {types!r}"
    )

    assert "edited" in types, (
        "`edited` is missing from pull_request.types. A retarget changes the "
        'diff this workflow scans (`git diff "$BASE_SHA"...HEAD`), and with no '
        "re-run the previous success stays attached to the head commit — a "
        "green tick over a diff nobody scanned (engram-agents/engram#4619)."
    )

    # The default set must still be present: dropping one of these would lose
    # coverage that works today, which is the opposite failure.
    missing = GITHUB_DEFAULT_TYPES - set(types)
    assert not missing, (
        f"pull_request.types dropped default activity type(s) {sorted(missing)}; "
        "listing `types:` replaces the default set rather than extending it, so "
        "every type that must still trigger has to be named."
    )


def test_the_job_cannot_be_skipped():
    """No job-level `if:`: a skip supersedes a red with a quieter `skipped`.

    Check runs key on the head commit, so a skipped job does not sit beside the
    previous failure — it replaces it as the newest run under the same name, and
    GitHub counts `skipped` as satisfied. A red gate would then disappear from
    the pull request's check list behind a title edit. This cell is the
    regression guard for that: if someone re-adds the skip to save an ~11 s
    runner start, this fails.
    """
    job = _workflow()["jobs"]["name-leak-gate"]

    assert "if" not in job, (
        "jobs.name-leak-gate carries an `if:` guard again. A skipped job "
        "reports `skipped` under the same check name on the same head commit "
        "and SUPERSEDES the previous run, so a red gate becomes grey after any "
        "title or body edit (measured on engram-agents/ci-probes#6). The gate "
        "must run on every subscription it declares."
    )
