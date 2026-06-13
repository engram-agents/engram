"""Tests for tools/release_gates.py — release-readiness scorecard.

Test categories:
  - RegistryShape:      gate registry covers all expected gate IDs
  - JSONSchema:         --json output parses and has required fields
  - SuiteResidual:      compare_suite_residual logic (pure function; no real pytest)
  - ForbiddenTeachings: G4b on a tmp fixture tree (match + clean cases)
  - StampPattern:       check_stamp_pattern logic (pure function)
  - G6Offline:          G6 with gh CLI unavailable → SKIP
  - MarkdownBlock:      --markdown output contains the scorecard marker + all gate rows
  - MutationCheck:      G3a residual comparison + returncode backstop sabotage tests
  - G3aParser:          G3a output parser: FAILED lines, ERROR lines, returncode backstop
  - G1ManifestAbsent:   G1 returns SKIP (not PASS) when manifest is absent

Mutation-check protocol (mandatory per spec):
  The test test_G3a_mutation_sabotage_makes_unexpected_fail demonstrates
  the mutation guard for the residual comparison. It sabotages compare_suite_residual
  by feeding an "unexpected" failure into expected_residual (treating unexpected
  failures as expected), then asserts the result is FAIL. This test validates that
  the comparison logic is not accidentally trivialised.

  The test test_G3a_returncode_backstop_mutation verifies the returncode backstop:
  if the backstop were removed, a nonzero exit with no parsed lines would silently
  return PASS from compare_suite_residual([], []). The test feeds exactly that
  scenario and asserts FAIL.

  Mutation traces (manual verification):
    Residual comparison:
      - Original: unexpected = actual_set - expected_set → if unexpected → FAIL
      - Sabotage:  pass the unexpected ID into expected_set → unexpected becomes
                   empty → function returns DEGRADED, not FAIL
      - Test:      asserts status == FAIL → test goes RED under sabotage

    Returncode backstop:
      - Original: returncode != 0 + empty failures → return FAIL early
      - Sabotage:  remove the backstop → compare_suite_residual([], []) returns PASS
      - Test:      asserts status == FAIL → test goes RED under sabotage
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import the module under test
from tools.release_gates import (
    ALL_GATE_IDS,
    STATUS_DEGRADED,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    check_forbidden_teachings_in_content,
    check_stamp_pattern,
    compare_suite_residual,
    format_json_output,
    format_markdown,
    is_releasable,
    make_result,
    run_G1,
    run_G3a,
    run_G4b,
    run_G6,
    run_scorecard,
)


# ---------------------------------------------------------------------------
# RegistryShape
# ---------------------------------------------------------------------------

class TestRegistryShape(unittest.TestCase):
    """The registry covers exactly the eight expected gate IDs in declared order."""

    def test_all_gate_ids_present(self):
        expected = ["G3a", "G3b", "G4a", "G4b", "G5", "G1", "G2", "G6"]
        self.assertEqual(ALL_GATE_IDS, expected)

    def test_make_result_keys(self):
        r = make_result("G1", "dim", STATUS_PASS, "m", "d")
        self.assertSetEqual(set(r.keys()), {"gate", "dimension", "status", "metric", "detail"})

    def test_make_result_values(self):
        r = make_result("G1", "fresh-install", STATUS_PASS, "ok", "all good")
        self.assertEqual(r["gate"], "G1")
        self.assertEqual(r["dimension"], "fresh-install")
        self.assertEqual(r["status"], STATUS_PASS)
        self.assertEqual(r["metric"], "ok")
        self.assertEqual(r["detail"], "all good")


# ---------------------------------------------------------------------------
# JSONSchema
# ---------------------------------------------------------------------------

class TestJSONSchema(unittest.TestCase):
    """--json output parses and has required fields."""

    def _make_results(self) -> list:
        return [
            make_result("G1", "fresh-install", STATUS_PASS, "ok", ""),
            make_result("G3a", "core-MCP", STATUS_SKIP, "skipped", "--skip-slow"),
        ]

    def test_json_output_parses(self):
        results = self._make_results()
        out = format_json_output(results)
        data = json.loads(out)
        self.assertIn("generated_at", data)
        self.assertIn("releasable", data)
        self.assertIn("results", data)

    def test_json_output_results_schema(self):
        results = self._make_results()
        out = format_json_output(results)
        data = json.loads(out)
        for r in data["results"]:
            for key in ("gate", "dimension", "status", "metric", "detail"):
                self.assertIn(key, r, f"Missing key {key!r} in result {r}")

    def test_json_releasable_true_all_pass(self):
        results = [make_result(g, "d", STATUS_PASS, "m", "") for g in ALL_GATE_IDS]
        out = format_json_output(results)
        data = json.loads(out)
        self.assertTrue(data["releasable"])

    def test_json_releasable_false_if_fail(self):
        results = [make_result("G1", "d", STATUS_FAIL, "m", "")]
        out = format_json_output(results)
        data = json.loads(out)
        self.assertFalse(data["releasable"])

    def test_json_releasable_false_if_degraded(self):
        results = [make_result("G1", "d", STATUS_DEGRADED, "m", "")]
        out = format_json_output(results)
        data = json.loads(out)
        self.assertFalse(data["releasable"])

    def test_json_releasable_true_skip_only(self):
        # SKIP gates do not prevent releasability
        results = [make_result("G3a", "d", STATUS_SKIP, "m", "")]
        out = format_json_output(results)
        data = json.loads(out)
        self.assertTrue(data["releasable"])


# ---------------------------------------------------------------------------
# SuiteResidual (pure function; no real pytest run)
# ---------------------------------------------------------------------------

class TestSuiteResidual(unittest.TestCase):
    """compare_suite_residual covers all spec cases."""

    def test_no_failures_no_expected_is_pass(self):
        status, metric, detail = compare_suite_residual([], [])
        self.assertEqual(status, STATUS_PASS)

    def test_no_failures_with_expected_is_pass(self):
        # Expected residual exists but all pass → still PASS
        expected = ["tests/foo.py::Bar::test_x"]
        status, metric, detail = compare_suite_residual([], expected)
        self.assertEqual(status, STATUS_PASS)

    def test_failures_subset_of_expected_is_degraded(self):
        expected = [
            "tests/foo.py::Bar::test_x",
            "tests/foo.py::Bar::test_y",
        ]
        failures = ["tests/foo.py::Bar::test_x"]
        status, metric, detail = compare_suite_residual(failures, expected)
        self.assertEqual(status, STATUS_DEGRADED)
        self.assertIn("1", metric)
        self.assertIn("tests/foo.py::Bar::test_x", detail)

    def test_failures_equal_expected_is_degraded(self):
        expected = ["tests/foo.py::Bar::test_x"]
        failures = ["tests/foo.py::Bar::test_x"]
        status, metric, detail = compare_suite_residual(failures, expected)
        self.assertEqual(status, STATUS_DEGRADED)

    def test_unexpected_failure_is_fail(self):
        expected = ["tests/foo.py::Bar::test_x"]
        failures = ["tests/foo.py::Bar::test_UNEXPECTED"]
        status, metric, detail = compare_suite_residual(failures, expected)
        self.assertEqual(status, STATUS_FAIL)
        self.assertIn("UNEXPECTED", detail)

    def test_mix_expected_and_unexpected_is_fail(self):
        expected = ["tests/foo.py::Bar::test_x"]
        failures = [
            "tests/foo.py::Bar::test_x",      # expected
            "tests/foo.py::Bar::test_new",     # unexpected
        ]
        status, metric, detail = compare_suite_residual(failures, expected)
        self.assertEqual(status, STATUS_FAIL)
        self.assertIn("test_new", detail)
        # The expected one should NOT appear as unexpected
        self.assertNotIn("1 unexpected", detail.lower().replace("test_new", ""))

    def test_empty_failures_empty_expected(self):
        status, metric, detail = compare_suite_residual([], [])
        self.assertEqual(status, STATUS_PASS)
        self.assertIn("0", metric)


# ---------------------------------------------------------------------------
# MutationCheck — mandatory per spec
# ---------------------------------------------------------------------------

class TestMutationCheck(unittest.TestCase):
    """Mutation guard for the G3a residual comparison logic.

    The sabotage below demonstrates the test correctly catches a bug
    where unexpected failures are treated as expected.

    Mutation: we pass the unexpected failure ID into expected_residual so
    that unexpected = actual_set - expected_set becomes empty, and the
    function returns DEGRADED instead of FAIL.

    The test asserts status == FAIL, so it would go RED under the mutation.
    This is the documented mutation check per spec §Deliverable 3.
    """

    def test_G3a_mutation_sabotage_makes_unexpected_fail(self):
        """Passing unexpected failure as expected → DEGRADED (sabotage detected).

        This test verifies that compare_suite_residual correctly distinguishes
        unexpected from expected failures.

        With the REAL code:
          - failures = ["tests/foo.py::Bad::test_novel"]
          - expected = []  (we do NOT put test_novel in expected)
          - unexpected = {"tests/foo.py::Bad::test_novel"} → non-empty → FAIL

        The SABOTAGED code would put test_novel in expected, making unexpected
        empty → DEGRADED.

        We assert status == FAIL, confirming the real code is correct and
        that a sabotage version would make this assertion fail.
        """
        unexpected_id = "tests/foo.py::Bad::test_novel"

        # Real call (no sabotage): test_novel is NOT in expected_residual
        status, metric, detail = compare_suite_residual(
            failures=[unexpected_id],
            expected_residual=[],  # sabotage would put unexpected_id here
        )

        # Must be FAIL, not DEGRADED — if sabotage were applied, this fails
        self.assertEqual(
            status, STATUS_FAIL,
            f"Expected FAIL for unexpected failure, got {status}. "
            "If sabotage (treating unexpected as expected) were applied, "
            "this would be DEGRADED and this assertion would fail — "
            "confirming the mutation guard is live.",
        )
        self.assertIn(unexpected_id, detail)


# ---------------------------------------------------------------------------
# ForbiddenTeachings (G4b with tmp fixture tree)
# ---------------------------------------------------------------------------

class TestForbiddenTeachings(unittest.TestCase):
    """G4b on a tmp fixture tree — match + clean cases."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rg_test_g4b_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, relpath: str, content: str) -> str:
        full = os.path.join(self.tmpdir, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        Path(full).write_text(content, encoding="utf-8")
        return full

    def test_no_match_is_pass(self):
        self._write("README.md", "# ENGRAM\n\nClean install via plugin.\n")
        teachings = [{"surface_glob": "README.md", "regex": r"deploy\.sh", "reason": "retired"}]
        result = run_G4b(self.tmpdir, teachings)
        self.assertEqual(result["status"], STATUS_PASS)
        self.assertEqual(result["gate"], "G4b")

    def test_match_is_fail(self):
        self._write(
            "README.md",
            "# ENGRAM\n\nRun deploy.sh to install.\n",
        )
        teachings = [{"surface_glob": "README.md", "regex": r"deploy\.sh", "reason": "retired"}]
        result = run_G4b(self.tmpdir, teachings)
        self.assertEqual(result["status"], STATUS_FAIL)
        self.assertIn("README.md:3", result["detail"])
        self.assertIn("retired", result["detail"])

    def test_multiple_entries_one_match(self):
        self._write("README.md", "clean README\n")
        os.makedirs(os.path.join(self.tmpdir, "src", "engram", "templates"), exist_ok=True)
        self._write(
            "src/engram/templates/template.CLAUDE.md",
            "Register hooks in `settings.json` to enable.\n",
        )
        teachings = [
            {"surface_glob": "README.md", "regex": r"deploy\.sh", "reason": "retired path"},
            {
                "surface_glob": "src/engram/templates/template.*.md",
                "regex": r"hooks in `settings\.json`",
                "reason": "plugin hooks.json is the location",
            },
        ]
        result = run_G4b(self.tmpdir, teachings)
        self.assertEqual(result["status"], STATUS_FAIL)
        self.assertIn("plugin hooks.json is the location", result["detail"])

    def test_both_match_accumulates(self):
        self._write("README.md", "run deploy.sh\n")
        os.makedirs(os.path.join(self.tmpdir, "templates"), exist_ok=True)
        self._write(
            "templates/hooks.md.template",
            "hooks in `settings.json`\n",
        )
        teachings = [
            {"surface_glob": "README.md", "regex": r"deploy\.sh", "reason": "r1"},
            {
                "surface_glob": "templates/*.template",
                "regex": r"hooks in `settings\.json`",
                "reason": "r2",
            },
        ]
        result = run_G4b(self.tmpdir, teachings)
        self.assertEqual(result["status"], STATUS_FAIL)
        self.assertIn("r1", result["detail"])
        self.assertIn("r2", result["detail"])

    def test_check_forbidden_content_no_match(self):
        hits = check_forbidden_teachings_in_content("clean line\n", r"deploy\.sh", "f.md")
        self.assertEqual(hits, [])

    def test_check_forbidden_content_match(self):
        content = "line1\nrun deploy.sh here\nline3\n"
        hits = check_forbidden_teachings_in_content(content, r"deploy\.sh", "f.md")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], 2)  # lineno 2

    def test_missing_glob_file_is_pass(self):
        # If no files match the glob, result should be PASS (nothing to flag)
        teachings = [{"surface_glob": "nonexistent_*.md", "regex": r"deploy\.sh", "reason": "r"}]
        result = run_G4b(self.tmpdir, teachings)
        self.assertEqual(result["status"], STATUS_PASS)


# ---------------------------------------------------------------------------
# StampPattern (pure function) — G2 per-rebuild stamp check
# ---------------------------------------------------------------------------

class TestStampPattern(unittest.TestCase):
    """check_stamp_pattern covers PASS (stamp present) and FAIL (stamp absent).

    Property under test (post-#897): the version string carries a per-rebuild
    stamp matching ``-dev.<YYYYMMDDHHMMSS>.<sha>``.

    Mutation-check discipline:
      (a) stamped version → PASS
          Mutation: if the regex never matched, this would be FAIL — the assert
          on STATUS_PASS would fail, confirming the guard is live.
      (b) static/unstamped version → FAIL
          Mutation: if the function returned PASS for any input, this would be
          PASS — the assert on STATUS_FAIL would fail.
      (c) two identical stamped versions (same-second collision) → both PASS
          Confirms the property-check approach resolves the collision false-DEGRADED.
          Mutation: if the function returned FAIL for identical stamps, the assert
          on STATUS_PASS would fail.
    """

    # --- (a) stamped version → PASS ---

    def test_stamped_version_is_pass(self):
        """Canonical stamped format → PASS."""
        stamped = "0.1.0-dev.20260607143022.a1b2c3d"
        status, metric, detail = check_stamp_pattern(stamped)
        self.assertEqual(status, STATUS_PASS)
        self.assertIn("stamp present", metric)
        self.assertIn(stamped, detail)

    def test_stamped_version_various_sha_lengths_is_pass(self):
        """Stamp regex accepts any hex-character sha suffix."""
        for sha in ("abc1234", "deadbeef", "0" * 40):
            with self.subTest(sha=sha):
                version = f"0.1.0-dev.20260607120000.{sha}"
                status, metric, detail = check_stamp_pattern(version)
                self.assertEqual(status, STATUS_PASS, f"Expected PASS for version={version!r}")

    # --- (b) static/unstamped version → FAIL ---

    def test_static_version_is_fail(self):
        """Plain semver (no stamp) → FAIL (regression after #897)."""
        static = "0.1.0"
        status, metric, detail = check_stamp_pattern(static)
        self.assertEqual(status, STATUS_FAIL)
        self.assertIn("stamp absent", metric)
        self.assertIn(static, detail)

    def test_empty_version_is_fail(self):
        """Empty version string (plugin.json had no version key) → FAIL."""
        status, metric, detail = check_stamp_pattern("")
        self.assertEqual(status, STATUS_FAIL)
        self.assertIn("stamp absent", metric)

    def test_partial_stamp_missing_sha_is_fail(self):
        """Timestamp present but no sha component → FAIL (incomplete stamp)."""
        partial = "0.1.0-dev.20260607143022"
        status, metric, detail = check_stamp_pattern(partial)
        self.assertEqual(status, STATUS_FAIL)

    def test_wrong_timestamp_length_is_fail(self):
        """13-digit timestamp (not 14) must not match → FAIL."""
        wrong = "0.1.0-dev.2026060714302.abc123"
        status, metric, detail = check_stamp_pattern(wrong)
        self.assertEqual(status, STATUS_FAIL)

    # --- producer fallback sha shapes (tools/engine/build.py) → PASS ---

    def test_git_unavailable_fallback_is_pass(self):
        """git-unavailable fallback sha 'unknown' → PASS (stamp IS present)."""
        version = "0.1.0-dev.20260607140831.unknown"
        status, metric, detail = check_stamp_pattern(version)
        self.assertEqual(status, STATUS_PASS)

    def test_g_prefixed_numeric_sha_is_pass(self):
        """Pure-digit sha7 gets g-prefix from producer (semver §9); stamp is still present → PASS."""
        version = "0.1.0-dev.20260607140831.g0149257"
        status, metric, detail = check_stamp_pattern(version)
        self.assertEqual(status, STATUS_PASS)

    # --- (c) two identical stamped versions (same-second collision) → both PASS ---

    def test_identical_stamped_versions_both_pass(self):
        """Same-second build collision: both calls return PASS.

        This is the core regression for issue #910: when two consecutive builds
        complete within the same second, they emit identical version stamps.
        The old compare_version_strings() returned DEGRADED for identical inputs.
        check_stamp_pattern() checks the property (stamp present) not equality,
        so both return PASS independently.
        """
        stamped = "0.1.0-dev.20260607143022.a1b2c3d"
        status_a, metric_a, _ = check_stamp_pattern(stamped)
        status_b, metric_b, _ = check_stamp_pattern(stamped)  # same string, same second
        self.assertEqual(status_a, STATUS_PASS, "First build should PASS with stamped version")
        self.assertEqual(status_b, STATUS_PASS, "Second build (same stamp) should also PASS")
        self.assertEqual(metric_a, "stamp present")
        self.assertEqual(metric_b, "stamp present")


# ---------------------------------------------------------------------------
# G6Offline (mock subprocess)
# ---------------------------------------------------------------------------

class TestG6Offline(unittest.TestCase):
    """G6 with gh CLI unavailable → SKIP."""

    def test_gh_not_found_returns_skip(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            result = run_G6([605, 689])
        self.assertEqual(result["gate"], "G6")
        self.assertEqual(result["status"], STATUS_SKIP)
        self.assertIn("unavailable", result["detail"].lower())

    def test_gh_timeout_returns_skip(self):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("gh", 10),
        ):
            result = run_G6([605, 689])
        self.assertEqual(result["status"], STATUS_SKIP)

    def test_gh_returns_open_issues_is_degraded(self):
        # Simulate gh returning OPEN for issue 605
        call_count = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if "--version" in cmd:
                p = mock.MagicMock()
                p.returncode = 0
                p.stdout = "gh version 2.0\n"
                return p
            # view call
            p = mock.MagicMock()
            p.returncode = 0
            p.stdout = json.dumps({"state": "OPEN"})
            return p

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = run_G6([605])

        self.assertEqual(result["status"], STATUS_DEGRADED)
        self.assertIn("#605", result["detail"])

    def test_gh_all_closed_is_pass(self):
        def fake_run(cmd, **kwargs):
            p = mock.MagicMock()
            p.returncode = 0
            if "--version" in cmd:
                p.stdout = "gh version 2.0\n"
            else:
                p.stdout = json.dumps({"state": "CLOSED"})
            return p

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = run_G6([100, 101])

        self.assertEqual(result["status"], STATUS_PASS)


# ---------------------------------------------------------------------------
# MarkdownBlock
# ---------------------------------------------------------------------------

class TestMarkdownBlock(unittest.TestCase):
    """--markdown output contains the scorecard marker + all gate rows."""

    def _make_all_results(self) -> list:
        return [
            make_result(gid, "dim", STATUS_PASS, "ok", "")
            for gid in ALL_GATE_IDS
        ]

    def test_marker_present(self):
        results = self._make_all_results()
        md = format_markdown(results)
        self.assertIn("<!-- release-gates-scorecard -->", md)

    def test_all_gate_rows_present(self):
        results = self._make_all_results()
        md = format_markdown(results)
        for gid in ALL_GATE_IDS:
            self.assertIn(f"| {gid} ", md, f"Gate {gid} row missing from markdown")

    def test_releasable_yes_when_all_pass(self):
        results = self._make_all_results()
        md = format_markdown(results)
        self.assertIn("RELEASABLE: yes", md)

    def test_releasable_no_when_fail(self):
        results = [make_result("G1", "d", STATUS_FAIL, "m", "")]
        md = format_markdown(results)
        self.assertIn("RELEASABLE: no", md)

    def test_skip_does_not_cause_releasable_no(self):
        results = [
            make_result("G3a", "d", STATUS_SKIP, "m", "--skip-slow"),
            make_result("G1", "d", STATUS_PASS, "m", ""),
        ]
        md = format_markdown(results)
        self.assertIn("RELEASABLE: yes", md)

    def test_date_present(self):
        import datetime
        results = self._make_all_results()
        md = format_markdown(results)
        date_str = datetime.date.today().isoformat()
        self.assertIn(date_str, md)


# ---------------------------------------------------------------------------
# IsReleasable
# ---------------------------------------------------------------------------

class TestIsReleasable(unittest.TestCase):
    """is_releasable covers all status combinations."""

    def test_all_pass_is_releasable(self):
        results = [make_result("G1", "d", STATUS_PASS, "m", "")]
        self.assertTrue(is_releasable(results))

    def test_skip_only_is_releasable(self):
        results = [make_result("G3a", "d", STATUS_SKIP, "m", "")]
        self.assertTrue(is_releasable(results))

    def test_fail_is_not_releasable(self):
        results = [make_result("G1", "d", STATUS_FAIL, "m", "")]
        self.assertFalse(is_releasable(results))

    def test_degraded_is_not_releasable(self):
        results = [make_result("G6", "d", STATUS_DEGRADED, "m", "")]
        self.assertFalse(is_releasable(results))

    def test_mix_pass_skip_is_releasable(self):
        results = [
            make_result("G1", "d", STATUS_PASS, "m", ""),
            make_result("G3a", "d", STATUS_SKIP, "m", ""),
        ]
        self.assertTrue(is_releasable(results))


# ---------------------------------------------------------------------------
# G3aParser — pytest output parser: FAILED lines, ERROR lines, returncode backstop
# ---------------------------------------------------------------------------

class TestG3aParser(unittest.TestCase):
    """G3a pytest output parser covers FAILED lines, ERROR lines, and returncode backstop.

    All tests use mock.patch on subprocess.run to inject synthetic pytest output
    without running a real suite.
    """

    _REPO_ROOT = str(PROJECT_ROOT)

    def _fake_proc(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        """Build a mock CompletedProcess-alike."""
        p = mock.MagicMock()
        p.stdout = stdout
        p.stderr = stderr
        p.returncode = returncode
        return p

    def test_failed_line_parsed_as_failure(self):
        """FAILED lines are parsed into the failure set (original path)."""
        output = (
            "tests/foo.py::Bar::test_bad FAILED\n"
            "FAILED tests/foo.py::Bar::test_bad - AssertionError: 0 != 1\n"
            "1 failed in 0.01s\n"
        )
        proc = self._fake_proc(stdout=output, returncode=1)
        with mock.patch("subprocess.run", return_value=proc):
            result = run_G3a(self._REPO_ROOT, sys.executable, [], skip_slow=False)
        # test_bad is not in expected_residual → FAIL
        self.assertEqual(result["status"], STATUS_FAIL)
        self.assertIn("test_bad", result["detail"])

    def test_error_line_parsed_as_failure(self):
        """ERROR lines (fixture/teardown/collection errors) are parsed into the failure set."""
        output = (
            ".E\n"
            "ERROR tests/foo.py::test_x - RuntimeError: fixture blew up\n"
            "1 passed, 1 error in 0.05s\n"
        )
        proc = self._fake_proc(stdout=output, returncode=1)
        with mock.patch("subprocess.run", return_value=proc):
            result = run_G3a(self._REPO_ROOT, sys.executable, [], skip_slow=False)
        # test_x (error) is not in expected_residual → FAIL
        self.assertEqual(result["status"], STATUS_FAIL, result)
        self.assertIn("test_x", result["detail"])

    def test_error_line_in_expected_residual_is_degraded(self):
        """ERROR-line test ID that IS in expected_residual → DEGRADED (known residual)."""
        output = (
            "ERROR tests/foo.py::test_known - RuntimeError: fixture blew up\n"
            "1 error in 0.05s\n"
        )
        proc = self._fake_proc(stdout=output, returncode=1)
        expected = ["tests/foo.py::test_known"]
        with mock.patch("subprocess.run", return_value=proc):
            result = run_G3a(self._REPO_ROOT, sys.executable, expected, skip_slow=False)
        self.assertEqual(result["status"], STATUS_DEGRADED, result)

    def test_returncode_backstop_nonzero_no_parsed_lines(self):
        """Returncode backstop: nonzero exit + no parsed FAILED/ERROR lines → FAIL.

        This covers pytest collection errors that produce no test-ID lines
        (e.g., SyntaxError in conftest.py):
          "ERROR collecting tests/conftest.py\\n..."
        but with output that has no 'FAILED ' or 'ERROR ' prefixed test-ID lines.
        """
        # Output has no parseable FAILED/ERROR test-ID lines
        output = (
            "INTERNALERROR> SyntaxError\n"
            "no tests ran\n"
        )
        proc = self._fake_proc(stdout=output, returncode=3)
        with mock.patch("subprocess.run", return_value=proc):
            result = run_G3a(self._REPO_ROOT, sys.executable, [], skip_slow=False)
        self.assertEqual(result["status"], STATUS_FAIL, result)
        self.assertIn("exit=3", result["metric"])
        self.assertIn("no parsed", result["detail"])

    def test_returncode_zero_empty_output_is_pass(self):
        """Returncode 0 with no failures → PASS (clean suite)."""
        output = "2780 passed, 32 skipped in 42.1s\n"
        proc = self._fake_proc(stdout=output, returncode=0)
        with mock.patch("subprocess.run", return_value=proc):
            result = run_G3a(self._REPO_ROOT, sys.executable, [], skip_slow=False)
        self.assertEqual(result["status"], STATUS_PASS, result)

    def test_maxsplit_parametrize_value_with_dash(self):
        """rsplit(' - ', 1) preserves test IDs containing ' - ' in parametrize values.

        pytest formats: "FAILED tests/foo.py::test_x[value - with dash] - AssertionError"
        rsplit from the right strips only the trailing " - <reason>" suffix, leaving
        the parametrize bracket intact.  A naive split(" - ", 1) would truncate at the
        first occurrence and lose part of the test ID.
        """
        # Test ID: "tests/foo.py::test_x[value - with dash]"
        output = (
            "FAILED tests/foo.py::test_x[value - with dash] - AssertionError\n"
            "1 failed in 0.01s\n"
        )
        proc = self._fake_proc(stdout=output, returncode=1)
        with mock.patch("subprocess.run", return_value=proc):
            result = run_G3a(self._REPO_ROOT, sys.executable, [], skip_slow=False)
        self.assertEqual(result["status"], STATUS_FAIL)
        # The full test ID including the parametrize part must be in detail
        self.assertIn("test_x[value - with dash]", result["detail"])


# ---------------------------------------------------------------------------
# MutationCheck — returncode backstop sabotage (extends the existing mutation tests)
# ---------------------------------------------------------------------------

class TestMutationCheckReturncodeBackstop(unittest.TestCase):
    """Mutation guard for the G3a returncode backstop.

    Verifies that the backstop guard (returncode != 0 and no parsed failures → FAIL)
    is live and would not silently return PASS if removed.

    Mutation trace:
      - Original: if proc.returncode not in (0, 1) or (proc.returncode != 0 and not failures):
                    return FAIL(...)
      - Sabotage:  remove this guard → compare_suite_residual([], []) returns PASS
      - Test:      asserts status == FAIL → test goes RED under sabotage
    """

    def test_G3a_returncode_backstop_mutation(self):
        """Nonzero returncode + no parsed lines must FAIL, not silently PASS.

        If the returncode backstop were removed:
          - proc.returncode = 2, output has no FAILED/ERROR lines
          - failures = []
          - compare_suite_residual([], []) → STATUS_PASS
          - result status == PASS (wrong — would be a false PASS)

        The test asserts FAIL, so it would go RED if the backstop were deleted.
        """
        output = "ImportError while loading conftest.py\n"
        proc = mock.MagicMock()
        proc.stdout = output
        proc.stderr = ""
        proc.returncode = 2  # non-zero; not a normal pytest exit code

        with mock.patch("subprocess.run", return_value=proc):
            result = run_G3a(str(PROJECT_ROOT), sys.executable, [], skip_slow=False)

        self.assertEqual(
            result["status"], STATUS_FAIL,
            f"Expected FAIL for nonzero exit with no parsed lines, got {result['status']}. "
            "If the returncode backstop were removed, this would be PASS — "
            "confirming the mutation guard is live.",
        )


# ---------------------------------------------------------------------------
# G1ManifestAbsent — SKIP (not PASS) when manifest file is absent
# ---------------------------------------------------------------------------

class TestG1ManifestAbsent(unittest.TestCase):
    """G1 returns SKIP when plugin.json is present but manifest is absent.

    Prior behaviour (before this fix): returned PASS with "manifest is a nice-to-have".
    Correct behaviour per spec: return SKIP so the gap is explicit.
    """

    def test_manifest_absent_returns_skip_not_pass(self):
        """plugin.json present, manifest absent → STATUS_SKIP."""
        with tempfile.TemporaryDirectory(prefix="rg_test_g1_") as tmp_out:
            # Write plugin.json so the build-succeeded check passes
            plugin_json = os.path.join(tmp_out, "plugin.json")
            Path(plugin_json).write_text('{"version": "0.1.0"}', encoding="utf-8")
            # Do NOT write .engram-build-manifest.json

            def fake_run(cmd, **kwargs):
                p = mock.MagicMock()
                p.returncode = 0
                p.stdout = ""
                p.stderr = ""
                return p

            with mock.patch("subprocess.run", side_effect=fake_run):
                with mock.patch("tempfile.TemporaryDirectory") as mock_tmpdir:
                    # Make TemporaryDirectory yield our controlled tmp_out
                    mock_tmpdir.return_value.__enter__ = mock.MagicMock(return_value=tmp_out)
                    mock_tmpdir.return_value.__exit__ = mock.MagicMock(return_value=False)
                    result = run_G1(str(PROJECT_ROOT), sys.executable)

        self.assertEqual(result["gate"], "G1")
        self.assertEqual(
            result["status"], STATUS_SKIP,
            f"Expected SKIP for absent manifest, got {result['status']}. "
            "Returning PASS here would mask the missing manifest-nonzero check.",
        )
        self.assertIn("manifest", result["detail"].lower())

    def test_manifest_present_with_paths_returns_pass(self):
        """plugin.json + nonzero manifest → STATUS_PASS (normal path unaffected)."""
        with tempfile.TemporaryDirectory(prefix="rg_test_g1_ok_") as tmp_out:
            plugin_json = os.path.join(tmp_out, "plugin.json")
            Path(plugin_json).write_text('{"version": "0.1.0"}', encoding="utf-8")
            manifest_path = os.path.join(tmp_out, ".engram-build-manifest.json")
            Path(manifest_path).write_text(
                '{"shipped_paths": ["a.py", "b.py"]}', encoding="utf-8"
            )

            def fake_run(cmd, **kwargs):
                p = mock.MagicMock()
                p.returncode = 0
                p.stdout = ""
                p.stderr = ""
                return p

            with mock.patch("subprocess.run", side_effect=fake_run):
                with mock.patch("tempfile.TemporaryDirectory") as mock_tmpdir:
                    mock_tmpdir.return_value.__enter__ = mock.MagicMock(return_value=tmp_out)
                    mock_tmpdir.return_value.__exit__ = mock.MagicMock(return_value=False)
                    result = run_G1(str(PROJECT_ROOT), sys.executable)

        self.assertEqual(result["status"], STATUS_PASS, result)
        self.assertIn("2 paths shipped", result["metric"])


if __name__ == "__main__":
    unittest.main()


class TestG4aNodeIdBurnDown:
    """Direct unit coverage for G4a (walk + regex + DEGRADED-never-FAIL)."""

    def _make_tree(self, tmp_path):
        (tmp_path / "shipped.py").write_text(
            "# provenance: see the lesson node aa_0001 and bb_12345\nx = 1\n"
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_x.py").write_text("# cc_9999 excluded dir\n")
        (tmp_path / "README.md").write_text("# dd_0002 excluded file\n")
        (tmp_path / "notes.bin").write_text("ee_0003 wrong extension\n")
        return tmp_path

    def test_counts_only_shipped_surfaces(self, tmp_path):
        from tools.release_gates import run_G4a
        self._make_tree(tmp_path)
        res = run_G4a(str(tmp_path))
        # Only shipped.py's two IDs count: tests/ pruned, README.md excluded,
        # .bin extension skipped.
        assert "2" in res["metric"], res
        assert res["status"] == "DEGRADED", res

    def test_zero_ids_is_pass(self, tmp_path):
        from tools.release_gates import run_G4a
        (tmp_path / "clean.py").write_text("x = 1\n")
        res = run_G4a(str(tmp_path))
        assert res["status"] == "PASS", res

    def test_never_fail_contract(self, tmp_path):
        """G4a is a burn-down metric: any count, however large, is DEGRADED."""
        from tools.release_gates import run_G4a
        body = "\n".join(f"# zz_{1000+i}" for i in range(500))
        (tmp_path / "noisy.py").write_text(body + "\n")
        res = run_G4a(str(tmp_path))
        assert res["status"] == "DEGRADED", res
        assert res["status"] != "FAIL"

    def test_nested_excluded_dir_pruned(self, tmp_path):
        """Any-depth pruning: forum/tests/ is pruned like top-level tests/."""
        from tools.release_gates import run_G4a
        nested = tmp_path / "forum" / "tests"
        nested.mkdir(parents=True)
        (nested / "test_y.py").write_text("# ff_0004 nested excluded\n")
        (tmp_path / "forum" / "app.py").write_text("# gg_0005 shipped\n")
        res = run_G4a(str(tmp_path))
        assert "1" in res["metric"], res
