"""Hermetic regression test for the PR-body close-keyword trap gate.

Fixture-test follow-up to PR #959 (merged) — Ariadne's review suggestion.

Strategy
--------
Extract the ``run: |`` block from the shipped workflow at test time so that
this test exercises the EXACT bytes in the workflow, not a copy.  Any bash
regression in the workflow will surface immediately here.

The gate logic is pure bash (grep/sort/set-difference) — no ``gh``, no
network — so each fixture can be run fully hermetically via subprocess with
``PR_TITLE`` and ``PR_BODY`` injected as environment variables.

Verdict semantics
-----------------
  FAIL   exit_code != 0  OR  "::error::" in output  (off-target close-keyword)
  WARN   exit_code == 0  AND "::warning::" in output (body keyword, no title target)
  PASS   exit_code == 0  AND no error/warning         (no body keywords, or all on-target)

Extraction method: PyYAML (``import yaml``) — navigates
``jobs.<first-job>.steps[0].run`` directly.  More robust than text-based
indentation scanning because it is format-agnostic w.r.t. YAML whitespace.
If PyYAML is absent the test suite is skipped with a clear message.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

import pytest

# ---------------------------------------------------------------------------
# Locate the workflow file relative to this test file
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "check-pr-body-close-keyword.yml"

# ---------------------------------------------------------------------------
# Attempt to import PyYAML; skip entire module if unavailable
# ---------------------------------------------------------------------------
try:
    import yaml as _yaml  # type: ignore[import]
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

SKIP_NO_YAML = pytest.mark.skipif(
    not _YAML_AVAILABLE,
    reason="PyYAML not importable; install pyyaml to run these tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_run_block() -> str:
    """Extract the ``run: |`` block from the gate workflow via PyYAML.

    Navigates ``jobs.<first-job>.steps[0].run`` and returns the raw bash
    string exactly as stored in the YAML.

    Raises FileNotFoundError if the workflow file is missing.
    Raises KeyError / AssertionError with a descriptive message if the YAML
    structure doesn't match expectations (step name guard).
    """
    with open(_WORKFLOW_PATH, encoding="utf-8") as fh:
        wf = _yaml.safe_load(fh)

    job = next(iter(wf["jobs"].values()))
    step = job["steps"][0]

    expected_name = "Fail on off-target close-keyword in the PR body"
    assert step.get("name") == expected_name, (
        f"Expected step[0].name == {expected_name!r}; got {step.get('name')!r}. "
        "The workflow structure may have changed — update this test accordingly."
    )
    run_block = step["run"]
    # Guard against a vacuous suite: if the run-block is ever emptied by a
    # refactor, an empty bash script exits 0 and every FAIL/WARN-expected
    # fixture would pass silently. Fail loudly instead.
    assert run_block and run_block.strip(), (
        "Extracted run-block is empty — the gate workflow's run: step has no body. "
        "The test would pass vacuously; fix the extraction or the workflow."
    )
    return run_block


class Verdict(NamedTuple):
    exit_code: int
    has_error: bool
    has_warning: bool

    @classmethod
    def from_result(cls, result: subprocess.CompletedProcess) -> "Verdict":
        combined = result.stdout + result.stderr
        return cls(
            exit_code=result.returncode,
            has_error="::error::" in combined,
            has_warning="::warning::" in combined,
        )

    @property
    def label(self) -> str:
        if self.exit_code != 0 or self.has_error:
            return "FAIL"
        if self.has_warning:
            return "WARN"
        return "PASS"


def _run_gate(run_block: str, pr_title: str, pr_body: str) -> Verdict:
    """Write *run_block* to a temp .sh and execute it with the given env vars."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(run_block)
        tmp_path = tmp.name

    try:
        # Minimal env for airtight hermeticity: the gate bash reads only
        # PR_TITLE/PR_BODY; PATH is kept so bash can resolve grep/sort/printf/tr.
        result = subprocess.run(
            ["bash", tmp_path],
            env={
                "PATH": os.environ.get("PATH", ""),
                "PR_TITLE": pr_title,
                "PR_BODY": pr_body,
            },
            capture_output=True,
            text=True,
        )
        return Verdict.from_result(result)
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Fixture-driven tests
# ---------------------------------------------------------------------------

@SKIP_NO_YAML
class TestCloseKeywordGate:
    """Drive the shipped bash against (title, body) pairs and assert the verdict.

    Expected verdicts were pre-verified by running the extracted block before
    writing this test — all 13 matched the spec's stated semantics.
    """

    @pytest.fixture(scope="class")
    def run_block(self) -> str:
        return _extract_run_block()

    # ------------------------------------------------------------------
    # PASS cases
    # ------------------------------------------------------------------

    def test_empty_body_passes(self, run_block: str) -> None:
        """No body at all — nothing to flag."""
        v = _run_gate(run_block, "fix: thing [closes #10]", "")
        assert v.label == "PASS", f"expected PASS; got {v}"

    def test_no_keywords_passes(self, run_block: str) -> None:
        """Body contains #10 as a plain reference, not preceded by a close-keyword."""
        v = _run_gate(run_block, "fix: thing [closes #10]", "Refactors the parser. See #10.")
        assert v.label == "PASS", f"expected PASS; got {v}"

    def test_on_target_single_passes(self, run_block: str) -> None:
        """Body keyword matches the title's only close target exactly."""
        v = _run_gate(run_block, "feat: thing [closes #10]", "Closes #10.")
        assert v.label == "PASS", f"expected PASS; got {v}"

    def test_multi_target_legit_passes(self, run_block: str) -> None:
        """Both body keywords match both title targets — legitimately closing two issues."""
        v = _run_gate(
            run_block,
            "feat: thing [closes #10] [closes #11]",
            "Closes #10. Closes #11.",
        )
        assert v.label == "PASS", f"expected PASS; got {v}"

    def test_fix_variant_on_target_passes(self, run_block: str) -> None:
        """'Fixed #10' — past-tense 'fix' variant still matches the on-target number."""
        v = _run_gate(run_block, "feat: thing [closes #10]", "Fixed #10.")
        assert v.label == "PASS", f"expected PASS; got {v}"

    # ------------------------------------------------------------------
    # WARN cases
    # ------------------------------------------------------------------

    def test_no_title_target_warns(self, run_block: str) -> None:
        """Body has a close-keyword but the title has no [closes #N] — WARN, not FAIL.

        Per Borges #756: may be a partial/EPIC slice referencing an umbrella.
        """
        v = _run_gate(run_block, "chore: cleanup", "Fixes #12.")
        assert v.label == "WARN", f"expected WARN; got {v}"

    # ------------------------------------------------------------------
    # FAIL cases
    # ------------------------------------------------------------------

    def test_off_target_single_fails(self, run_block: str) -> None:
        """Body closes #11 but title only targets #10 — classic off-target trap."""
        v = _run_gate(run_block, "feat: thing [closes #10]", "Closes #11.")
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_negated_still_fails(self, run_block: str) -> None:
        """'does NOT close #11' still triggers the gate — GitHub ignores negation."""
        v = _run_gate(
            run_block,
            "feat: thing [closes #10]",
            "This does NOT close #11.",
        )
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_bracketed_off_target_fails(self, run_block: str) -> None:
        """'[closes #11]' in the body still triggers — GitHub ignores brackets."""
        v = _run_gate(
            run_block,
            "feat: thing [closes #10]",
            "the scrub PR uses [closes #11]",
        )
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_backticked_off_target_fails(self, run_block: str) -> None:
        """'`closes #11`' in the body still triggers — raw grep catches it."""
        v = _run_gate(
            run_block,
            "feat: thing [closes #10]",
            "uses `closes #11` somewhere",
        )
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_multi_ref_one_off_fails(self, run_block: str) -> None:
        """Body closes on-target #10 AND off-target #11 — the off-target traps the gate."""
        v = _run_gate(
            run_block,
            "feat: thing [closes #10]",
            "Closes #10. Also fixes #11.",
        )
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_resolve_variant_off_target_fails(self, run_block: str) -> None:
        """'Resolves' is a tracked keyword — off-target resolves #11 must FAIL."""
        v = _run_gate(run_block, "feat: thing [closes #10]", "Resolves #11.")
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_case_insensitive_off_target_fails(self, run_block: str) -> None:
        """'CLOSES #11' (all-caps) — the gate grep is case-insensitive (-i flag)."""
        v = _run_gate(run_block, "feat: thing [closes #10]", "CLOSES #11.")
        assert v.label == "FAIL", f"expected FAIL; got {v}"
