"""Tests for tools/name_leak_warn.py — name-leak warn-gate contract.

Coverage:
  1. Added-lines-only: removed and context lines are ignored; only '+' lines fire.
  2. Word-boundary case: a fake short name inside a longer word does NOT match
     when the pattern carries \\b anchors.
  3. JSON format shape: --format json produces {"matches": [...]} with required keys.
  4. Empty roster: empty patterns list produces zero matches (skip-when-absent).
  5. Multi-pattern: multiple patterns in roster, correct pattern_index reported.
  6. Real-name pattern self-test (alpha#3303): REAL_NAME_PATTERN must
     actually be live — regression guard for the non-raw-string bug that
     made the shipped pattern's trailing \\b compile to a literal backspace.
  7. No-real-names hygiene (STRICT): tools/name_leak_warn.py itself contains
     no real personal/agent names at all (mirrors test_scan_leaks.py's
     example-file hygiene test).
  8. Workflow-hygiene rule fixtures (alpha#3303, Kepler's ruling, CLAUDE.md
     "Attribution convention"): cases (a) other-human-anywhere, (b)
     marker-on-comment-line-only, (c) @handle-mention-anywhere — planted
     synthetic text, driven through the same helper as test 9.
  9. Workflow hygiene: pr-checks.yml (which now hosts the name-leak-warn job,
     post-#1598 consolidation) satisfies the (a)/(b)/(c) rule above.
  10. load_roster: absent file is an IO error — exits 1 (SystemExit(1)).
  11. load_roster: invalid regex in roster triggers exit 1 (usage error).
"""

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — import the tool module directly for unit tests
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "name_leak_warn.py"
# #1598 (2026-07-01) folded the standalone name-leak-warn.yml workflow into
# pr-checks.yml as a job/steps ("was name-leak-warn.yml") — the standalone
# file no longer exists, so the hygiene check now targets pr-checks.yml.
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "pr-checks.yml"

_spec = importlib.util.spec_from_file_location("name_leak_warn", TOOL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

scan_diff = _mod.scan_diff
load_roster = _mod.load_roster
format_json = _mod.format_json

# ---------------------------------------------------------------------------
# Fake names used in tests — obviously-fake placeholders only, no real names.
# ---------------------------------------------------------------------------

FAKE_NAME_A = "Alice Example"
FAKE_NAME_B = "agentname-nick"

# ---------------------------------------------------------------------------
# Real-name roster — single source of raw-string pieces, reused by every
# hygiene check below (alpha#3303: the shipped pattern's second piece was
# NOT a raw string, so its closing \b compiled to a literal backspace
# \x08, and the pattern could never match real text — both pieces are raw
# here, and test_real_name_pattern_is_live below guards against regression).
# ---------------------------------------------------------------------------

_LEI = r"lei"
_OTHER_HUMAN_NAMES = r"the child|lin(?!e|ux|k)"
_AGENT_HANDLES = r"borges|ariadne|luria|aleph|mira|funes|calvino|hebb|kepler|clio|sol|zephyr"

# Strict: every roster name (Lei, other humans, agent handles) — the rule
# used where NO roster name may appear at all (tools/name_leak_warn.py), and
# the pattern exercised directly by test_real_name_pattern_is_live.
REAL_NAME_PATTERN = re.compile(
    r"\b(" + _LEI + "|" + _OTHER_HUMAN_NAMES + "|" + _AGENT_HANDLES + r")\b",
    re.IGNORECASE,
)

# Workflow-file rule — Lei's functional-vs-marker test (CLAUDE.md
# "Attribution convention", alpha#2270), mechanised per Kepler's #3303
# ruling on issue #3303:
#   (a) any human name other than Lei — must not appear ANYWHERE (case 2 of
#       the convention is held open; the safe side is none).
#   (b) Lei + bare agent handles — attribution markers, allowed ONLY on a
#       YAML comment line (the line's first non-space char is '#'); on any
#       other line (step `name:`, `run:`, env, etc.) that position is
#       functional, so it fails.
#   (c) an `@handle` mention of ANY roster name — functional (renders as a
#       live GitHub notification the moment it's quoted anywhere on the
#       platform) — forbidden anywhere, comment or not.
_CASE_A_RE = re.compile(r"\b(" + _OTHER_HUMAN_NAMES + r")\b", re.IGNORECASE)
_CASE_B_RE = re.compile(r"\b(" + _LEI + "|" + _AGENT_HANDLES + r")\b", re.IGNORECASE)
_CASE_C_RE = re.compile(
    r"@(" + _LEI + "|" + _OTHER_HUMAN_NAMES + "|" + _AGENT_HANDLES + r")\b",
    re.IGNORECASE,
)


def _check_workflow_name_hygiene(content: str) -> list[tuple[int, str, str]]:
    """Apply the workflow-file name-hygiene rule (cases a/b/c above) to
    ``content``. Returns a list of (line_number, case, line_text) violations;
    empty means clean. Shared by the real-file test and its fixtures so both
    exercise the identical rule."""
    violations: list[tuple[int, str, str]] = []
    for i, line in enumerate(content.splitlines(), 1):
        is_comment = line.lstrip().startswith("#")
        if _CASE_A_RE.search(line):
            violations.append((i, "a", line.strip()[:100]))
        if _CASE_B_RE.search(line) and not is_comment:
            violations.append((i, "b", line.strip()[:100]))
        if _CASE_C_RE.search(line):
            violations.append((i, "c", line.strip()[:100]))
    return violations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_diff(added_lines: list[str], removed_lines: list[str] | None = None,
               context_lines: list[str] | None = None,
               filename: str = "path/to/file.py") -> str:
    """Construct a minimal unified diff string for testing."""
    total_context = len(context_lines or [])
    total_added = len(added_lines)
    total_removed = len(removed_lines or [])
    old_count = total_context + total_removed
    new_count = total_context + total_added
    lines = [
        f"diff --git a/{filename} b/{filename}",
        f"--- a/{filename}",
        f"+++ b/{filename}",
        f"@@ -1,{old_count} +1,{new_count} @@",
    ]
    for line in (context_lines or []):
        lines.append(f" {line}")
    for line in (removed_lines or []):
        lines.append(f"-{line}")
    for line in added_lines:
        lines.append(f"+{line}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Test 1: added-lines-only
# ---------------------------------------------------------------------------

def test_removed_lines_not_scanned():
    """Removed lines (starting with '-') must not trigger matches."""
    patterns = load_roster(roster_source=f"\\b{FAKE_NAME_A}\\b", roster_file=None)
    diff = _fake_diff(
        added_lines=["# nothing here"],
        removed_lines=[f"# old comment mentioning {FAKE_NAME_A}"],
    )
    matches = scan_diff(diff, patterns)
    assert matches == [], f"removed lines must not fire; got {matches}"


def test_context_lines_not_scanned():
    """Context lines (starting with ' ') must not trigger matches."""
    patterns = load_roster(roster_source=f"\\b{FAKE_NAME_A}\\b", roster_file=None)
    diff = _fake_diff(
        added_lines=["# new clean line"],
        context_lines=[f"# context mentioning {FAKE_NAME_A}"],
    )
    matches = scan_diff(diff, patterns)
    assert matches == [], f"context lines must not fire; got {matches}"


def test_added_line_fires():
    """Added lines that match the pattern ARE reported."""
    patterns = load_roster(roster_source=f"\\b{FAKE_NAME_A}\\b", roster_file=None)
    diff = _fake_diff(added_lines=[f"# Author: {FAKE_NAME_A}"])
    matches = scan_diff(diff, patterns)
    assert len(matches) == 1, f"expected 1 match; got {matches}"
    assert matches[0]["pattern_index"] == 0
    assert FAKE_NAME_A in matches[0]["line"]


# ---------------------------------------------------------------------------
# Test 2: word-boundary — fake short name inside longer word
# ---------------------------------------------------------------------------

def test_word_boundary_no_false_positive():
    """Pattern \\bAlice\\b must NOT match 'Malice' or 'aliceblue'."""
    # Use just the first word of FAKE_NAME_A to test boundary sensitivity
    patterns = load_roster(roster_source=r"\bAlice\b", roster_file=None)
    diff = _fake_diff(added_lines=["color = 'aliceblue'  # or Malice"])
    matches = scan_diff(diff, patterns)
    assert matches == [], f"\\b anchors should prevent false positive; got {matches}"


def test_word_boundary_does_match_standalone():
    """Pattern \\bAlice\\b DOES match 'Alice' standing alone."""
    patterns = load_roster(roster_source=r"\bAlice\b", roster_file=None)
    diff = _fake_diff(added_lines=["# Alice wrote this"])
    matches = scan_diff(diff, patterns)
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# Test 3: JSON format shape
# ---------------------------------------------------------------------------

def test_json_format_shape():
    """--format json output has required top-level key and match fields."""
    patterns = load_roster(roster_source=f"\\b{FAKE_NAME_A}\\b", roster_file=None)
    diff = _fake_diff(added_lines=[f"author = '{FAKE_NAME_A}'"])
    matches = scan_diff(diff, patterns)
    output = format_json(matches)
    data = json.loads(output)
    assert "matches" in data, f"top-level 'matches' key missing; got {data.keys()}"
    assert len(data["matches"]) == 1
    m = data["matches"][0]
    for key in ("file", "line_number", "pattern_index", "line"):
        assert key in m, f"required key {key!r} missing from match dict"
    assert isinstance(m["line_number"], int)
    assert isinstance(m["pattern_index"], int)


def test_json_format_empty_matches():
    """--format json with no matches produces {"matches": []}."""
    output = format_json([])
    data = json.loads(output)
    assert data == {"matches": []}, f"got {data}"


# ---------------------------------------------------------------------------
# Test 4: empty roster → zero matches
# ---------------------------------------------------------------------------

def test_empty_roster_no_matches():
    """Empty patterns list produces zero matches regardless of diff content."""
    patterns = load_roster(roster_source="", roster_file=None)
    diff = _fake_diff(added_lines=["any content here including anything"])
    matches = scan_diff(diff, patterns)
    assert matches == [], f"empty roster should produce no matches; got {matches}"


def test_none_roster_no_matches():
    """None roster source with no file also produces zero matches."""
    patterns = load_roster(roster_source=None, roster_file=None)
    diff = _fake_diff(added_lines=["some added line"])
    matches = scan_diff(diff, patterns)
    assert matches == []


# ---------------------------------------------------------------------------
# Test 5: multi-pattern — correct pattern_index
# ---------------------------------------------------------------------------

def test_multi_pattern_correct_index():
    """Second pattern in roster reports pattern_index=1."""
    roster_text = f"\\b{FAKE_NAME_A}\\b\n\\b{FAKE_NAME_B}\\b"
    patterns = load_roster(roster_source=roster_text, roster_file=None)
    assert len(patterns) == 2
    diff = _fake_diff(added_lines=[f"# config for {FAKE_NAME_B}"])
    matches = scan_diff(diff, patterns)
    assert len(matches) == 1
    assert matches[0]["pattern_index"] == 1, f"expected index 1; got {matches[0]['pattern_index']}"


def test_multi_pattern_first_pattern_wins():
    """When a line matches both patterns, pattern_index=0 (first match wins, one per line)."""
    roster_text = f"\\b{FAKE_NAME_A}\\b\n\\b{FAKE_NAME_B}\\b"
    patterns = load_roster(roster_source=roster_text, roster_file=None)
    diff = _fake_diff(added_lines=[f"# {FAKE_NAME_A} and {FAKE_NAME_B}"])
    matches = scan_diff(diff, patterns)
    assert len(matches) == 1, "one match per line (break after first hit)"
    assert matches[0]["pattern_index"] == 0


# ---------------------------------------------------------------------------
# Test 6 (self-test): the real-name pattern must actually be live.
# alpha#3303 — the shipped pattern's second alternation piece was a plain
# (non-raw) Python string, so the trailing \b compiled to a literal
# backspace character and the pattern could never match real text.
# ---------------------------------------------------------------------------

def test_real_name_pattern_is_live():
    """REAL_NAME_PATTERN must match each roster name as a whole word, and
    must not match look-alike substrings (line/linux/link, Malice)."""
    must_match = [
        "approved by Lei",
        "Borges ran it",
        "mira-verala's note",
        "the child",
        "Lin said",
        "Kepler merged it",
        "per Clio's review",
        "Sol re-measured",
        "zephyr is idle",
    ]
    for text in must_match:
        assert REAL_NAME_PATTERN.search(text), f"pattern must match {text!r} but did not"

    must_not_match = [
        "keep this on one line",
        "runs on linux",
        "click the link",
        "Malice in wonderland",
        "solution",
        "console",
        "isolate",
    ]
    for text in must_not_match:
        assert not REAL_NAME_PATTERN.search(text), f"pattern incorrectly matched {text!r}"


# ---------------------------------------------------------------------------
# Test 7: no-real-names hygiene — tool source file
# ---------------------------------------------------------------------------

def test_tool_file_no_real_names():
    """tools/name_leak_warn.py must contain no real personal/agent names at
    all — the STRICT rule (no roster name anywhere), using REAL_NAME_PATTERN
    (module-level, built from raw-string pieces; see #3303).

    Mirrors the example-file hygiene test from test_scan_leaks.py.

    This test is intentionally conservative — it checks the literal committed
    file rather than running the scanner so that no local roster is required.
    """
    content = TOOL_PATH.read_text(encoding="utf-8")
    violations = []
    for i, line in enumerate(content.splitlines(), 1):
        if REAL_NAME_PATTERN.search(line):
            violations.append(f"  line {i}: {line.strip()[:100]}")

    assert not violations, (
        f"tools/name_leak_warn.py contains real personal/agent names:\n"
        + "\n".join(violations)
        + "\nReal names must not appear in any committed file."
    )


# ---------------------------------------------------------------------------
# Test 8: workflow-hygiene rule fixtures — cases (a)/(b)/(c), planted text
# (red on a synthetic violation, exercised through the shared helper that
# the real-file test below also uses).
# ---------------------------------------------------------------------------

def test_workflow_rule_case_a_blocks_other_human_names_anywhere():
    """(a) a human name other than Lei must not appear ANYWHERE — comment
    or not — and look-alike substrings (line/linux/link) must not false-fire."""
    on_comment_line = "# a note mentioning the child's review\n"
    violations = _check_workflow_name_hygiene(on_comment_line)
    assert any(v[1] == "a" for v in violations), f"case (a) should flag the child; got {violations}"

    on_run_line = "      run: echo 'as Lin said, this works'\n"
    violations = _check_workflow_name_hygiene(on_run_line)
    assert any(v[1] == "a" for v in violations), f"case (a) should flag bare Lin; got {violations}"

    substrings_only = "      # keep this on one line, not linux, no link here\n"
    violations = _check_workflow_name_hygiene(substrings_only)
    assert not any(v[1] == "a" for v in violations), (
        f"case (a) false-positived on line/linux/link substrings; got {violations}"
    )


def test_workflow_rule_case_b_marker_allowed_only_on_comment_lines():
    """(b) Lei + bare agent handles are attribution markers — allowed ONLY
    on a YAML comment line; the same name on a non-comment line (step
    `name:`, `run:`, env, etc.) is functional, so it fails."""
    marker_on_comment = "      # approved by Lei after Borges's review\n"
    violations = _check_workflow_name_hygiene(marker_on_comment)
    assert not any(v[1] == "b" for v in violations), (
        f"marker on a comment line should be clean; got {violations}"
    )

    marker_in_run = '      run: echo "Borges did this"\n'
    violations = _check_workflow_name_hygiene(marker_in_run)
    assert any(v[1] == "b" for v in violations), (
        f"marker on a non-comment (run:) line should be flagged; got {violations}"
    )

    marker_in_step_name = "      - name: Lei's gate\n"
    violations = _check_workflow_name_hygiene(marker_in_step_name)
    assert any(v[1] == "b" for v in violations), (
        f"marker in a step name: line should be flagged; got {violations}"
    )

    # New-roster-handle fixture (alpha#3307): Kepler marker on a comment line
    # is clean; the same handle on a run: line is functional and flagged.
    new_handle_marker_on_comment = "      # per Kepler's review, this is fine\n"
    violations = _check_workflow_name_hygiene(new_handle_marker_on_comment)
    assert not any(v[1] == "b" for v in violations), (
        f"new-handle marker on a comment line should be clean; got {violations}"
    )

    new_handle_marker_in_run = '      run: echo "Kepler did this"\n'
    violations = _check_workflow_name_hygiene(new_handle_marker_in_run)
    assert any(v[1] == "b" for v in violations), (
        f"new-handle marker on a non-comment (run:) line should be flagged; got {violations}"
    )


def test_workflow_rule_case_c_blocks_at_handle_mentions_anywhere():
    """(c) an @handle mention of any roster name is functional (renders as a
    live GitHub notification once quoted on the platform) — forbidden
    anywhere, including inside a comment line; a bare handle with no @ is
    not an (c)-violation (that's case (b)'s territory)."""
    at_mention_in_comment = "      # cc @Borges for review\n"
    violations = _check_workflow_name_hygiene(at_mention_in_comment)
    assert any(v[1] == "c" for v in violations), (
        f"@handle mention inside a comment should still be flagged; got {violations}"
    )

    bare_handle_in_comment = "      # Borges reviewed this\n"
    violations = _check_workflow_name_hygiene(bare_handle_in_comment)
    assert not any(v[1] == "c" for v in violations), (
        f"bare handle (no @) must not trip case (c); got {violations}"
    )

    # New-roster-handle fixture (alpha#3307): @clio mention is functional
    # (a live GitHub notification once quoted) and forbidden even on a
    # comment line.
    new_handle_at_mention_in_comment = "      # cc @clio\n"
    violations = _check_workflow_name_hygiene(new_handle_at_mention_in_comment)
    assert any(v[1] == "c" for v in violations), (
        f"@handle mention of a new roster name should still be flagged; got {violations}"
    )


# ---------------------------------------------------------------------------
# Test 9: workflow hygiene — pr-checks.yml (hosts the name-leak-warn job)
# ---------------------------------------------------------------------------

def test_workflow_file_no_real_names():
    """pr-checks.yml (the name-leak-warn job's home post-#1598) must satisfy
    Lei's functional-vs-marker rule (CLAUDE.md "Attribution convention",
    alpha#2270; mechanised per Kepler's #3303 ruling) — cases (a)/(b)/(c),
    via the shared _check_workflow_name_hygiene helper, exercised green here
    on the real file and red on synthetic fixtures above."""
    assert WORKFLOW_PATH.exists(), f"workflow file not found: {WORKFLOW_PATH}"
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    violations = _check_workflow_name_hygiene(content)

    assert not violations, (
        ".github/workflows/pr-checks.yml fails the name-hygiene rule "
        "(case a/b/c — see CLAUDE.md 'Attribution convention'):\n"
        + "\n".join(f"  line {i} [case {c}]: {t}" for i, c, t in violations)
    )


# ---------------------------------------------------------------------------
# Test 10: load_roster with absent file → empty list, no error
# ---------------------------------------------------------------------------

def test_load_roster_absent_file_exits_1(tmp_path):
    """Absent --roster-file is an IO error: exit 1."""
    absent = tmp_path / "no-roster.txt"
    # File does not exist — should not raise, should return []
    # But per spec: absent-file is an IO error (exits 1).
    # Check: the function itself raises SystemExit when the file doesn't exist.
    import pytest
    with pytest.raises(SystemExit) as exc_info:
        load_roster(roster_source=None, roster_file=str(absent))
    assert exc_info.value.code == 1, (
        f"absent roster file should exit 1 (IO error); got {exc_info.value.code}"
    )


def test_load_roster_empty_string_returns_empty():
    """Empty string roster source returns empty list without error."""
    patterns = load_roster(roster_source="", roster_file=None)
    assert patterns == []


def test_load_roster_comment_and_blank_lines_skipped():
    """Blank lines and #-comment lines are skipped in roster parsing."""
    roster = "\n# this is a comment\n\n\\bAlice\\b\n"
    patterns = load_roster(roster_source=roster, roster_file=None)
    assert len(patterns) == 1


# ---------------------------------------------------------------------------
# Test 11: invalid regex in roster → exit 1
# ---------------------------------------------------------------------------

def test_load_roster_invalid_regex_exits_1(tmp_path):
    """Invalid regex pattern in roster file triggers SystemExit(1)."""
    import pytest
    roster_file = tmp_path / "bad.txt"
    roster_file.write_text("[invalid regex(\n")
    with pytest.raises(SystemExit) as exc_info:
        load_roster(roster_source=None, roster_file=str(roster_file))
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Integration test: subprocess round-trip
# ---------------------------------------------------------------------------

def test_subprocess_exit_0_on_match(tmp_path):
    """Script exits 0 even when matches are found (warn-only)."""
    roster_file = tmp_path / "roster.txt"
    roster_file.write_text(f"\\b{FAKE_NAME_A}\\b\n")
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(_fake_diff(added_lines=[f"# {FAKE_NAME_A} was here"]))

    result = subprocess.run(
        [sys.executable, str(TOOL_PATH),
         "--roster-file", str(roster_file),
         "--diff-file", str(diff_file)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"expected exit 0 (warn-only); got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert FAKE_NAME_A in result.stdout


def test_subprocess_exit_0_on_no_match(tmp_path):
    """Script exits 0 when no matches found."""
    roster_file = tmp_path / "roster.txt"
    roster_file.write_text(f"\\b{FAKE_NAME_A}\\b\n")
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(_fake_diff(added_lines=["# nothing interesting here"]))

    result = subprocess.run(
        [sys.executable, str(TOOL_PATH),
         "--roster-file", str(roster_file),
         "--diff-file", str(diff_file)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_subprocess_json_output(tmp_path):
    """--format json produces parseable JSON with correct structure."""
    roster_file = tmp_path / "roster.txt"
    roster_file.write_text(f"\\b{FAKE_NAME_A}\\b\n")
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(_fake_diff(added_lines=[f"x = '{FAKE_NAME_A}'"]))

    result = subprocess.run(
        [sys.executable, str(TOOL_PATH),
         "--roster-file", str(roster_file),
         "--diff-file", str(diff_file),
         "--format", "json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert "matches" in data
    assert len(data["matches"]) == 1
    m = data["matches"][0]
    assert m["pattern_index"] == 0
    assert FAKE_NAME_A in m["line"]
