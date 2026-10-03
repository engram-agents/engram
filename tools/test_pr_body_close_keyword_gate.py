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
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "pr-checks.yml"
_STEP_NAME_PREFIX = "pr-body-close-keyword"

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
    """Extract the ``run: |`` block of the close-keyword step via PyYAML.

    The gate lives as one step inside the combined ``pr-checks.yml`` job (it
    was a standalone workflow when this test was written; #3344 repointed it).
    The step is located by name prefix, never by index, so reordering steps
    does not break the test. Returns the raw bash string as stored in the YAML.

    Raises FileNotFoundError if the workflow file is missing.
    Raises KeyError / AssertionError with a descriptive message if the YAML
    structure doesn't match expectations (step name guard).
    """
    with open(_WORKFLOW_PATH, encoding="utf-8") as fh:
        wf = _yaml.safe_load(fh)

    job = next(iter(wf["jobs"].values()))
    matches = [
        st for st in job["steps"]
        if str(st.get("name", "")).startswith(_STEP_NAME_PREFIX)
    ]
    assert len(matches) == 1, (
        f"Expected exactly one step named {_STEP_NAME_PREFIX!r}...; "
        f"found {len(matches)}. The workflow structure may have changed — "
        "update this test accordingly."
    )
    step = matches[0]
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


def _run_output(run_block: str, pr_title: str, pr_body: str) -> str:
    """Return combined stdout+stderr of the gate for the given title/body."""
    result = _run_raw(run_block, pr_title, pr_body)
    return result.stdout + result.stderr


def _run_gate(run_block: str, pr_title: str, pr_body: str) -> Verdict:
    return Verdict.from_result(_run_raw(run_block, pr_title, pr_body))


def _run_raw(run_block: str, pr_title: str, pr_body: str) -> subprocess.CompletedProcess:
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
        return result
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

    # ------------------------------------------------------------------
    # #3344: [part of #N] title + body that would auto-close #N
    # ------------------------------------------------------------------

    def test_part_of_negated_close_fails(self, run_block: str) -> None:
        """The real case (#3341): partial PR whose body says 'does not close #N'."""
        v = _run_gate(
            run_block, "fix: slice [part of #3197]", "This does not close #3197"
        )
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_part_of_fail_message_names_safe_paraphrase(self, run_block: str) -> None:
        """The failure must teach the fix: safe paraphrase + negation ignored."""
        out = _run_output(
            run_block, "fix: slice [part of #3197]", "This does not close #3197"
        )
        assert "stays open" in out and "part of #3197" in out
        assert "negation" in out

    def test_part_of_case_insensitive_title_fails(self, run_block: str) -> None:
        v = _run_gate(run_block, "fix: slice [Part Of #3197]", "Fixes #3197")
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_part_of_backticked_keyword_passes(self, run_block: str) -> None:
        """Inline code span: the real parser ignores it (#1191), so must we."""
        v = _run_gate(run_block, "fix: slice [part of #3197]", "`closes #3197`")
        assert v.label != "FAIL", f"expected non-FAIL; got {v}"

    def test_part_of_backtick_fence_keyword_passes(self, run_block: str) -> None:
        body = "Example:\n```\ncloses #3197\n```\nDone."
        v = _run_gate(run_block, "fix: slice [part of #3197]", body)
        assert v.label != "FAIL", f"expected non-FAIL; got {v}"

    def test_part_of_tilde_fence_keyword_passes(self, run_block: str) -> None:
        body = "Example:\n~~~\ncloses #3197\n~~~\nDone."
        v = _run_gate(run_block, "fix: slice [part of #3197]", body)
        assert v.label != "FAIL", f"expected non-FAIL; got {v}"

    def test_part_of_keyword_after_fence_still_fails(self, run_block: str) -> None:
        """Stripping is scoped to the fence: a keyword AFTER it still counts."""
        body = "```\nx\n```\nThis does not close #3197"
        v = _run_gate(run_block, "fix: slice [part of #3197]", body)
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_part_of_safe_paraphrase_passes(self, run_block: str) -> None:
        v = _run_gate(run_block, "fix: slice [part of #3197]", "#3197 stays open")
        assert v.label == "PASS", f"expected PASS; got {v}"

    def test_part_of_off_target_keyword_is_existing_warn(self, run_block: str) -> None:
        """Keyword for a DIFFERENT issue: not this rule — existing WARN, exit 0."""
        v = _run_gate(run_block, "fix: slice [part of #3197]", "closes #9999")
        assert v.label == "WARN", f"expected WARN; got {v}"
        assert v.exit_code == 0

    def test_closes_title_legit_close_unchanged(self, run_block: str) -> None:
        v = _run_gate(run_block, "fix: thing [closes #3197]", "Closes #3197.")
        assert v.label == "PASS", f"expected PASS; got {v}"

    def test_part_of_multiple_numbers_keyword_on_second_fails(
        self, run_block: str
    ) -> None:
        v = _run_gate(
            run_block,
            "fix: slice [part of #3197] [part of #2574]",
            "does not close #2574",
        )
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    # ------------------------------------------------------------------
    # #3344 round 1: stripper edge cases (CommonMark fence / span rules)
    # ------------------------------------------------------------------

    # "Passes" below means: this rule does not fire. The pre-existing
    # no-title-target WARN may still show (raw grep), so assert != FAIL.
    _PART_OF = "fix: slice [part of #5]"

    def test_part_of_long_fence_with_inner_short_fence_passes(
        self, run_block: str
    ) -> None:
        """4-backtick block holding a 3-backtick line: the inner line does NOT
        close it, so the keyword after it is still inside the block."""
        body = "````\n```\ncloses #5\n````\nDone."
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label != "FAIL", f"expected non-FAIL; got {v}"

    def test_part_of_long_fence_keyword_after_close_fails(
        self, run_block: str
    ) -> None:
        """The 4-backtick block still closes at a 4-backtick line."""
        body = "````\n```\nx\n````\nThis does not close #5"
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_part_of_open3_close4_fence_closes(self, run_block: str) -> None:
        """A closing run LONGER than the opener closes it; keyword after FAILs."""
        body = "```\nx\n````\nThis does not close #5"
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_part_of_tilde_not_closed_by_backticks(self, run_block: str) -> None:
        """Closing fence must be the same character as the opener."""
        body = "~~~\n```\ncloses #5\n~~~\nDone."
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label != "FAIL", f"expected non-FAIL; got {v}"

    def test_part_of_double_backtick_span_passes(self, run_block: str) -> None:
        v = _run_gate(run_block, self._PART_OF, "use ``closes #5`` ok")
        assert v.label != "FAIL", f"expected non-FAIL; got {v}"

    def test_part_of_double_backtick_span_holding_single_tick_passes(
        self, run_block: str
    ) -> None:
        """A k=2 span is not closed by a run of 1."""
        v = _run_gate(run_block, self._PART_OF, "use ``a ` closes #5`` ok")
        assert v.label != "FAIL", f"expected non-FAIL; got {v}"

    def test_part_of_unmatched_backtick_run_keeps_keyword_scanned(
        self, run_block: str
    ) -> None:
        """No closing run of the same length: the run is literal, not a span."""
        v = _run_gate(run_block, self._PART_OF, "``closes #5`")
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_part_of_crlf_fenced_keyword_passes(self, run_block: str) -> None:
        body = "Example:\r\n```\r\ncloses #5\r\n```\r\nDone."
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label != "FAIL", f"expected non-FAIL; got {v}"

    def test_part_of_crlf_bare_keyword_fails(self, run_block: str) -> None:
        body = "Intro\r\nThis does not close #5\r\n"
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    @pytest.mark.parametrize("indent", [1, 2, 3])
    def test_part_of_fence_indented_up_to_3_is_a_fence(
        self, run_block: str, indent: int
    ) -> None:
        """CommonMark: a fence may be indented AT MOST 3 spaces. Deliberately
        changed from the earlier pin (any indent): 2 spaces is still a fence."""
        pad = " " * indent
        body = f"{pad}```\n{pad}closes #5\n{pad}```\nDone."
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label != "FAIL", f"expected non-FAIL; got {v}"

    @pytest.mark.parametrize("indent", [4, 5])
    def test_part_of_fence_indented_4plus_is_not_a_fence(
        self, run_block: str, indent: int
    ) -> None:
        """CommonMark: 4+ spaces is an indented code block, not a fence.
        Deliberately changed from the earlier pin (5 spaces was a fence): the
        keyword is now scanned (fail-closed). Accepted cost: a fence nested in
        a list item at 4+ spaces is scanned too."""
        pad = " " * indent
        body = f"{pad}```\n{pad}closes #5\n{pad}```\nDone."
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_part_of_4plus_indented_fence_marker_does_not_swallow_rest(
        self, run_block: str
    ) -> None:
        body = "    ```\nThis does not close #5"
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_part_of_line_starting_with_inline_triple_span_is_not_a_fence(
        self, run_block: str
    ) -> None:
        """A backtick fence's info string cannot contain a backtick, so a line
        that BEGINS with an inline triple-backtick span is not a fence opener."""
        body = "```x``` is the form.\nThis does not close #5."
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_part_of_info_string_fence_keyword_passes(self, run_block: str) -> None:
        body = "```python\ncloses #5\n```\nDone."
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label != "FAIL", f"expected non-FAIL; got {v}"

    def test_part_of_info_string_fence_trap_after_close_fails(
        self, run_block: str
    ) -> None:
        body = "```python\nx = 1\n```\nThis does not close #5."
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    @pytest.mark.parametrize("body", ["prefix #5", "disclosed #5", "unfixed #5"])
    def test_part_of_keyword_inside_a_longer_word_is_not_flagged(
        self, run_block: str, body: str
    ) -> None:
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label != "FAIL", f"expected non-FAIL; got {v}"

    @pytest.mark.parametrize(
        "body", ["Fixes #5", "(closes #5)", "x\nResolved #5", "-fixed #5"]
    )
    def test_part_of_keyword_at_a_word_start_still_fails(
        self, run_block: str, body: str
    ) -> None:
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label == "FAIL", f"expected FAIL; got {v}"

    def test_part_of_unterminated_fence_swallows_rest(self, run_block: str) -> None:
        """CommonMark: an unclosed fence runs to the end of the document, so a
        keyword after it is code, not text. Pinned."""
        body = "```\nstill code\nThis does not close #5"
        v = _run_gate(run_block, self._PART_OF, body)
        assert v.label != "FAIL", f"expected non-FAIL; got {v}"

    def test_part_of_fail_message_does_not_overclaim(self, run_block: str) -> None:
        out = _run_output(run_block, self._PART_OF, "This does not close #5")
        assert "multi-line spans and indented code blocks are not recognised" in out


@SKIP_NO_YAML
def test_pr_checks_invokes_this_test_file() -> None:
    """Wiring guard (#3344): tools/test_*.py is outside tests.yml's pytest roots,
    so pr-checks.yml must run this file itself or it silently runs nowhere."""
    with open(_WORKFLOW_PATH, encoding="utf-8") as fh:
        wf = _yaml.safe_load(fh)
    job = next(iter(wf["jobs"].values()))
    needle = "pytest tools/test_pr_body_close_keyword_gate.py"
    wired = [st for st in job["steps"] if needle in str(st.get("run", ""))]
    assert wired, (
        f"No pr-checks.yml step runs {needle!r}; this test file would run "
        "nowhere in CI and rot."
    )
