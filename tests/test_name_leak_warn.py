"""Tests for tools/name_leak_warn.py — name-leak warn-gate contract.

Coverage:
  1. Added-lines-only: removed and context lines are ignored; only '+' lines fire.
  2. Word-boundary case: a fake short name inside a longer word does NOT match
     when the pattern carries \\b anchors.
  3. JSON format shape: --format json produces {"matches": [...]} with required keys.
  4. Empty roster: empty patterns list produces zero matches (skip-when-absent).
  5. Multi-pattern: multiple patterns in roster, correct pattern_index reported.
  6. No-real-names hygiene: tools/name_leak_warn.py itself contains no real
     personal/agent names (mirrors test_scan_leaks.py's example-file hygiene test).
  7. Workflow hygiene: name-leak-warn.yml contains no real personal/agent names.
  8. load_roster: absent file is an IO error — exits 1 (SystemExit(1)).
  9. load_roster: invalid regex in roster triggers exit 1 (usage error).
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — import the tool module directly for unit tests
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "name_leak_warn.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "name-leak-warn.yml"

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
# Test 6: no-real-names hygiene — tool source file
# ---------------------------------------------------------------------------

def test_tool_file_no_real_names():
    """tools/name_leak_warn.py must contain no real personal/agent names.

    Mirrors the example-file hygiene test from test_scan_leaks.py.
    The real name set is the grep pattern used in the spec's zero-names
    verification: borges|ariadne|luria|aleph|mira|funes|calvino|hebb
    plus lei|the child (case-insensitive).

    This test is intentionally conservative — it checks the literal committed
    file rather than running the scanner so that no local roster is required.
    """
    import re

    content = TOOL_PATH.read_text(encoding="utf-8")
    # Split so this test file itself does not contain the literal pattern string
    # that would cause a false positive if the scanner is ever run on tests/.
    real_name_pattern = re.compile(
        r"\b(lei|the child|lin(?!e|ux|k)|" +
        "borges|ariadne|luria|aleph|mira|funes|calvino|hebb)\b",
        re.IGNORECASE,
    )
    violations = []
    for i, line in enumerate(content.splitlines(), 1):
        if real_name_pattern.search(line):
            violations.append(f"  line {i}: {line.strip()[:100]}")

    assert not violations, (
        f"tools/name_leak_warn.py contains real personal/agent names:\n"
        + "\n".join(violations)
        + "\nReal names must not appear in any committed file."
    )


# ---------------------------------------------------------------------------
# Test 7: workflow hygiene — name-leak-warn.yml
# ---------------------------------------------------------------------------

def test_workflow_file_no_real_names():
    """name-leak-warn.yml must contain no real personal/agent names."""
    import re

    assert WORKFLOW_PATH.exists(), f"workflow file not found: {WORKFLOW_PATH}"
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    real_name_pattern = re.compile(
        r"\b(lei|the child|lin(?!e|ux|k)|" +
        "borges|ariadne|luria|aleph|mira|funes|calvino|hebb)\b",
        re.IGNORECASE,
    )
    violations = []
    for i, line in enumerate(content.splitlines(), 1):
        if real_name_pattern.search(line):
            violations.append(f"  line {i}: {line.strip()[:100]}")

    assert not violations, (
        f".github/workflows/name-leak-warn.yml contains real personal/agent names:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Test 8: load_roster with absent file → empty list, no error
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
# Test 9: invalid regex in roster → exit 1
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
