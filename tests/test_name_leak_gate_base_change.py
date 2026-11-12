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

These tests pin the WIRING. They cannot run a GitHub event, so they do NOT prove
the gate actually re-runs on a retarget: that rests on GitHub's documented
`edited` payload shape and was NOT observed in a live run. The pull request that
added this file states what was executed and what was not, and the first natural
retarget after it merges is the confirmation. What these tests do is fail the
moment someone tidies the `types:` list back to the default set, weakens or
inverts the guard, or drops the fork clause that keeps a fork pull request's
loud red from being replaced by a quiet `skipped`.
"""

from __future__ import annotations

import re
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


def _normalise_guard(expr) -> str:
    """Whitespace-collapsed guard text, with truthiness spellings made explicit.

    GitHub treats a bare context path as truthiness, so `|| github.event.changes
    .base` is the SAME predicate as `|| github.event.changes.base != null`. The
    guard is pinned by equality below, so without this normalisation a
    semantically correct respelling would be rejected — a false failure, which
    is the mirror image of the false pass that substring matching produced.
    """
    text = " ".join(str(expr).split())
    return re.sub(
        r"github\.event\.changes\.base(?!\s*(?:!=|==))",
        "github.event.changes.base != null",
        text,
    )


#: The shipped guard, as one canonical string. Compared by EQUALITY, not by
#: substring: a substring check accepts `... && ...` (the operator swapped,
#: which would skip every opened/synchronize/reopened run and disable the gate),
#: and it accepts `... || true` appended. Both were measured passing against a
#: contains-based version of this test.
EXPECTED_GUARD = (
    "github.event.action != 'edited' "
    "|| github.event.changes.base != null "
    "|| github.event.pull_request.head.repo.full_name != github.repository"
)


def test_the_guard_is_exactly_the_shipped_predicate():
    """Run unless: an edit that did not move the base, on a same-repo PR.

    Pinned by equality so the two most plausible regressions fail — swapping
    `||` for `&&`, and appending `|| true`. Both are silent gate disablers.
    """
    job = _workflow()["jobs"]["name-leak-gate"]
    guard = job.get("if")

    assert guard is not None, (
        "the job needs an `if:` guard, or every title and body edit starts a "
        "run (and replaces a good result with an identical one)"
    )
    assert _normalise_guard(guard) == EXPECTED_GUARD, (
        "the job guard is not the shipped predicate. Got "
        f"{_normalise_guard(guard)!r}, expected {EXPECTED_GUARD!r}. "
        "A `||`/`&&` swap here skips every opened/synchronize/reopened run and "
        "quietly disables the gate; `|| true` appended defeats the skip."
    )


def test_fork_pull_requests_always_run():
    """A fork PR's red must not be replaced by a quiet `skipped` on a title edit.

    Fork PRs report the loud `name-leak-gate (NOT SCANNED - fork PR)` failure and
    never receive the roster secret. Without the fork clause, a title or body
    edit fires `edited`, the guard skips the job, and GitHub replaces that red
    with `skipped` under the same name — quieter than the signal this workflow
    exists to make loud.
    """
    guard = _workflow()["jobs"]["name-leak-gate"].get("if")
    normalised = _normalise_guard(guard)

    assert "github.event.pull_request.head.repo.full_name != github.repository" in normalised, (
        "the guard must keep running for fork pull requests, or an `edited` "
        "event downgrades their NOT SCANNED failure to `skipped`"
    )
    # It has to be an OR arm. As an AND arm it would fire only on a
    # base-changing edit of a fork PR — exactly the case that is not the
    # problem — and a plain title edit would still downgrade the red.
    assert "|| github.event.pull_request.head.repo.full_name != github.repository" in normalised, (
        f"the fork clause must be joined with `||`; got {normalised!r}"
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
