#!/usr/bin/env python3
"""name_leak_gate.py — the pure decision contract for the name-leak CI gate.

`.github/workflows/name-leak-gate.yml` is the only caller. It performs all the
I/O (reading the `NAME_LEAK_ROSTER` secret, hashing it, reading the committed
attestation) and hands the five facts to :func:`decide`, which is a pure
function: no file access, no environment reads, no clock, no randomness.

The named invariant, stated here and asserted exhaustively in
`tests/test_name_leak_gate.py`:

    ``conclusion == "success"`` if and only if the event is trusted AND the
    roster was present AND the attestation matched. There is no success case
    in which the gate did not run.

"Trusted" means a push to the default branch, or a pull request whose head
repository is this repository. A pull request from a fork never receives the
roster secret, so it is a FAILURE carrying a distinct status name that
contains the literal token ``NOT SCANNED`` (message: "not scanned, roster
unavailable"). That status name must never be listed as a required check;
same-repo PRs are the supported path for outside contributions.

Why a fork PR is a failure and not a green "skipped": a green tick that means
nothing is exactly the defect (issue #4429) this gate replaces. A red X that
says NOT SCANNED is visible in the checks list and cannot be mistaken for a
pass.

Why `merge_group` is not a trusted event: this repository has no merge queue
(measured: zero merge_queue rules, no merge_group run has ever fired), so a
merge_group decision would never run. An unknown event fails closed.
"""

from dataclasses import dataclass

SUCCESS = "success"
FAILURE = "failure"

#: Status/check name when the decision is reached on a trusted event.
STATUS_SCANNED = "name-leak-gate"

#: Status/check name for a fork PR. The literal token is load-bearing: a
#: reviewer must be able to tell "not scanned" from "scanned clean" in the
#: checks list. This name must never be configured as a required check.
STATUS_NOT_SCANNED = "name-leak-gate (NOT SCANNED - fork PR)"

TRUSTED_EVENTS = frozenset({"push", "pull_request"})

FORK_REASON = "not scanned, roster unavailable"


@dataclass(frozen=True)
class Outcome:
    """The decision a workflow turns into a conclusion, status name and message."""

    conclusion: str  # "success" | "failure"
    status_name: str
    reason: str


def decide(
    event: str,
    is_fork: bool,
    roster_present: bool,
    attestation_present: bool,
    attestation_matches: bool,
) -> Outcome:
    """Decide the gate outcome from five facts, with no I/O of any kind.

    The order of the checks is part of the contract:

    1. A fork pull request is decided first, so it reports NOT SCANNED for
       every combination of the roster/attestation inputs (the secret never
       reaches a fork, so those inputs are meaningless there).
    2. An unknown event fails closed before any other consideration.
    3. Every trusted event then needs the roster AND a present, matching
       attestation. A missing attestation cannot "match".
    """
    if event == "pull_request" and is_fork:
        return Outcome(FAILURE, STATUS_NOT_SCANNED, FORK_REASON)

    if event not in TRUSTED_EVENTS:
        return Outcome(
            FAILURE,
            STATUS_SCANNED,
            f"unknown event {event!r} - failing closed",
        )

    if not roster_present:
        return Outcome(
            FAILURE,
            STATUS_SCANNED,
            "roster absent - the NAME_LEAK_ROSTER secret is unset",
        )

    if not attestation_present:
        return Outcome(
            FAILURE,
            STATUS_SCANNED,
            "attestation absent or invalid - .github/name-leak-roster.attestation.json",
        )

    if not attestation_matches:
        return Outcome(
            FAILURE,
            STATUS_SCANNED,
            "roster sha256 does not match the committed attestation",
        )

    return Outcome(
        SUCCESS,
        STATUS_SCANNED,
        "roster present and the whole-file sha256 matches the attestation",
    )
