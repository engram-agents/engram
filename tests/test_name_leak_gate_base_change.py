"""The name-leak gate must re-run when a pull request's BASE BRANCH changes.

What the workflow scans is the pull request's own diff:

    git diff "${BASE_SHA}"...HEAD > "${RUN_DIR}/pr.diff"

so retargeting a pull request (dev -> main, say) replaces the entire subject of
the scan. Before this test existed the trigger was a bare `pull_request:`, which
GitHub expands to its default activity types — `opened`, `synchronize`,
`reopened`. `edited` is not among them, and a retarget fires only `edited`. The
gate therefore did not re-run, and because check runs attach to the head commit
rather than to the base, the previous success kept standing over a diff that had
completely changed. `engram#4619` sat in exactly that state on 2026-11-11.

Two independent things make the old result invalid after a retarget:

1. the diff changed, and
2. the gate reads its own tool code from `BASE_SHA`, so a new base is a
   different scanner version as well.

These tests pin the WIRING. They cannot run a GitHub event, so they do not prove
the gate actually re-runs on a retarget; that was measured on the live
repository and is reported in the pull request that added this file. What they
do is fail the moment someone tidies the `types:` list back to the default set,
or drops the guard that keeps a title/body edit from buying a runner start.
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
        "diff this workflow scans (`git diff \"$BASE_SHA\"...HEAD`), and with no "
        "re-run the previous success stays attached to the head commit — a "
        "green tick over a diff nobody scanned (engram#4619)."
    )

    # The default set must still be present: dropping one of these would lose
    # coverage that works today, which is the opposite failure.
    missing = GITHUB_DEFAULT_TYPES - set(types)
    assert not missing, (
        f"pull_request.types dropped default activity type(s) {sorted(missing)}; "
        "listing `types:` replaces the default set rather than extending it, so "
        "every type that must still trigger has to be named."
    )


def test_non_base_edits_do_not_start_a_run():
    """`edited` also fires for title/body changes; only a base move should run."""
    job = _workflow()["jobs"]["name-leak-gate"]
    guard = job.get("if")

    assert guard is not None, (
        "the job needs an `if:` guard, or every title and body edit starts a "
        "run (and replaces a good result with an identical one)"
    )
    guard_text = " ".join(str(guard).split())

    assert "github.event.action != 'edited'" in guard_text, (
        f"the guard must exempt non-`edited` events explicitly; got {guard_text!r}"
    )
    assert "github.event.changes.base" in guard_text, (
        "the guard must test `github.event.changes.base`, which GitHub populates "
        f"only when the base ref actually moved; got {guard_text!r}"
    )
    assert "!= null" in guard_text, (
        f"the guard must require `changes.base` to be present; got {guard_text!r}"
    )


def test_the_guard_is_not_vacuously_true():
    """A guard that accepts every edit would defeat its own purpose.

    Checked as a property rather than by matching one string: the expression must
    CONTAIN a restrictive clause, so `if: true`, `if: always()`, and a bare
    `if: github.event_name == 'pull_request'` all fail here.
    """
    guard = _workflow()["jobs"]["name-leak-gate"].get("if")

    # YAML 1.1 parses a bare `if: true` as the BOOLEAN True, not the string
    # "true". A string-only comparison silently accepts it — measured: the
    # `if: true` mutation passed this test until the boolean was handled.
    assert not isinstance(guard, bool), (
        f"the job-level guard is a bare boolean ({guard!r}); it restricts nothing"
    )

    guard_text = " ".join(str(guard).split()).lower()
    always_true = {
        "true",
        "always()",
        "!cancelled()",
        "github.event_name == 'pull_request'",
        "github.event_name == 'pull_request' || true",
    }
    assert guard_text not in always_true, (
        f"the job-level guard {guard_text!r} does not restrict anything"
    )
