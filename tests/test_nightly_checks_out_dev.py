"""The nightly tests dev, not the default branch.

The repository's default branch is main (the release branch). A scheduled
workflow runs from the default branch's copy of the file, and a bare
`actions/checkout` checks out that same commit, so the nightly would test the
last release. So every `actions/checkout` step in nightly.yml, in every job,
must carry `ref: dev`; the test below asserts exactly that and makes no
distinction between checkouts that test code and any other.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "nightly.yml"


def _checkout_steps():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job_name, job in wf["jobs"].items():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                yield job_name, step


def test_every_testing_checkout_pins_dev():
    found = list(_checkout_steps())
    assert found, "nightly.yml has no checkout step; the suite has no source to test"
    for job_name, step in found:
        ref = (step.get("with") or {}).get("ref")
        assert ref == "dev", f"job {job_name!r}: checkout must carry `ref: dev`, got {ref!r}"
