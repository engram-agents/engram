"""Tests for tools/name_leak_warn.py — name-leak warn-gate contract.

Coverage:
  1. Added-lines-only: removed and context lines are ignored; only '+' lines fire.
  2. Word-boundary case: a fake short name inside a longer word does NOT match
     when the pattern carries \\b anchors.
  3. JSON format shape: --format json produces {"matches": [...]} with required keys.
  4. Empty roster: empty patterns list produces zero matches (skip-when-absent).
  5. Multi-pattern: multiple patterns in roster, correct pattern_index reported.
  6. Roster-fed pattern self-test (alpha#3303): roster_text() and
     strict_pattern() must be live — the patterns match an injected roster
     name as a whole word, reject look-alikes, and reject a name absent from
     the injected roster. The regression guard for the non-raw-string bug
     (trailing \\b compiling to a literal backspace) is preserved, and the
     cell needs no real value. strict_pattern() compiles one pattern per
     roster line, exactly as tools/name_leak_warn.py's load_roster does — never
     joined into one alternation, which is a different language (a line
     carrying inline flags, e.g. ``(?i)name``, is valid alone but raises
     "global flags not at the start of the expression" when joined).
  7. No-real-names hygiene (STRICT): tools/name_leak_warn.py itself contains
     no real personal/agent names at all (mirrors test_scan_leaks.py's
     example-file hygiene test). Skips when NAME_LEAK_ROSTER is unset or
     compiles to no pattern (comment-only / whitespace-only).
  8. Workflow-hygiene rule fixtures (alpha#3303, Kepler's ruling, CLAUDE.md
     "Attribution convention"): cases (a) other-human-anywhere, (b)
     marker-on-comment-line-only, (c) @handle-mention-anywhere — planted
     synthetic text, driven through the same helper as test 9.
  9. Workflow hygiene: pr-checks.yml (which now hosts the name-leak-warn job,
     post-#1598 consolidation) satisfies the (a)/(b)/(c) rule above.
  10. load_roster: absent file is an IO error — exits 1 (SystemExit(1)).
  11. load_roster: invalid regex in roster triggers exit 1 (usage error).
  12. Roster secret scope: no workflow binds NAME_LEAK_ROSTER outside a step's
     ``env`` (workflow-level env, job-level env, job ``container.env``,
     ``services.*.env``, and step ``with`` are flagged; a step ``env`` is the
     one legal location), the step-level bindings are non-vacuous and include
     tests.yml and pr-checks.yml, and a synthetic table pins each flagged
     path. Only the allowlisted step-env sites may carry the secret, compared
     as a multiset so a duplicate identity is still a surplus binding; a
     surplus binding and a stale allowlist entry both fail. alpha#4568.

Principal marker: the principal set is declared in a separate
``NAME_LEAK_PRINCIPAL`` variable (same regex-line convention as the roster:
one bare regex per line, ``#`` comments skipped, multiple lines unioned) by
repeating the principal's ``NAME_LEAK_ROSTER`` line. The roster keeps exactly
one meaning — the patterns the scanner applies as-written — so the marker
never enters it (a ``principal: ...`` line there would be compiled as one
literal regex by ``tools/name_leak_warn.py``). With the principal declared,
the principal plus the agent handles are comment-line attribution markers
(case b) and every other roster name is forbidden anywhere (case a). Without
the declaration the rule degrades: case (a) becomes the empty set and every
roster name is treated as case (b), logged once. The degraded mode does NOT
check a roster name on a comment line — attribution comments are
policy-allowed — but it still flags a roster name on a ``run:`` line, a step
``name:``, an env value, or in an ``@``-mention anywhere.
"""

from collections import Counter
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

import pytest
import yaml

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
# Real-name roster — the real values live only in the NAME_LEAK_ROSTER
# secret. This module reads them through roster_text() and never carries a
# literal of its own, so importing the module cannot fail when the secret is
# absent and the file can be published without leaking a name.
#
# Agent handles stay as literals: they already ship in three READMEs, so they
# are not part of the secret.
# ---------------------------------------------------------------------------

_SKIP_REASON = "NAME_LEAK_ROSTER not set — real-name hygiene not checked"

_AGENT_HANDLES = r"borges|ariadne|luria|aleph|mira|funes|calvino|hebb|kepler|clio|sol|zephyr"


def roster_text() -> str | None:
    """Return the roster secret's text, or None when it is unset or empty.

    This is the only place the roster enters this module. A missing roster is
    not an error — cells that need it skip — but it is never silently green.
    """
    value = os.environ.get("NAME_LEAK_ROSTER")
    return value if value else None


def _require_roster() -> str:
    """Return the roster text, or skip the calling cell when it has no pattern.

    A skip is the honest outcome: a green pass with no roster would assert
    nothing about real names. A comment-only or whitespace-only
    ``NAME_LEAK_ROSTER`` is truthy but compiles to zero patterns, so the
    tool's own ``load_roster()`` decides — a non-empty string alone is not a
    roster.
    """
    roster = roster_text()
    if roster is None or not load_roster(roster_source=roster, roster_file=None):
        pytest.skip(_SKIP_REASON)
    return roster


def principal_text() -> str | None:
    """Return the principal variable's text, or None when unset or empty.

    ``NAME_LEAK_PRINCIPAL`` is deliberately separate from ``NAME_LEAK_ROSTER``:
    ``tools/name_leak_warn.py`` compiles every non-comment roster line as one
    regex, so a ``principal: <regex>`` line inside the roster secret would
    compile as that whole string and silently stop scanning the principal.
    """
    value = os.environ.get("NAME_LEAK_PRINCIPAL")
    return value if value else None


def _regex_lines(text: str) -> list[str]:
    """The bare, non-comment lines of a regex-line variable, stripped."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _principal_patterns() -> list[str]:
    """The principal patterns declared in ``NAME_LEAK_PRINCIPAL``.

    A declaration names a principal by repeating that name's roster line; the
    roster therefore keeps exactly one meaning (the patterns the scanner
    applies) while this variable only classifies them.
    """
    principal = principal_text()
    return _regex_lines(principal) if principal is not None else []


def _parse_roster(roster: str) -> tuple[list[str], list[str], bool]:
    """Split the roster into (principal, other, declared) patterns.

    The roster is the scanner's pattern list, applied as-written. The separate
    ``NAME_LEAK_PRINCIPAL`` variable declares which roster lines are the
    principal by repeating them; those move from case (a) to case (b). A
    declaration that repeats no roster line is still a principal pattern (the
    strict rule scans it), but it cannot be case (a) because it is not in the
    roster.
    """
    declared = _principal_patterns()
    declared_set = set(declared)
    roster_patterns = _regex_lines(roster)
    others = [p for p in roster_patterns if p not in declared_set]
    principal = [p for p in roster_patterns if p in declared_set]
    principal += [p for p in declared if p not in set(roster_patterns)]
    return principal, others, bool(declared)


def _all_roster_patterns(roster: str) -> list[str]:
    """Every roster pattern (principal and other names)."""
    principal, others, _ = _parse_roster(roster)
    return principal + others


_DEGRADATION_NOTED = False


def _note_degradation_once() -> None:
    """Say, once per process, that no principal has been declared.

    The degraded rule is not silent: case (a) becomes the empty set and every
    roster name is checked as a comment-line marker (case b). Attribution
    comments are therefore not flagged, but a roster name on any other line is.
    """
    global _DEGRADATION_NOTED
    if _DEGRADATION_NOTED:
        return
    _DEGRADATION_NOTED = True
    warnings.warn(
        "NAME_LEAK_PRINCIPAL is not set: case (a) is empty and every roster "
        "name is treated as comment-line-only (case b). Roster names on "
        "non-comment lines and @mentions are still checked; attribution "
        "comments are not flagged.",
        UserWarning,
        stacklevel=2,
    )


# A leading GLOBAL inline-flag group, e.g. ``(?i)`` or ``(?im)``. Python accepts
# global flags only at the very start of an expression, so a roster line that
# carries them must keep them at position 0 when re-compiled (see
# _compile_bounded). Scoped groups such as ``(?i:...)`` do not match this and
# are left in place.
_GLOBAL_INLINE_FLAGS = re.compile(r"^\(\?[aiLmsux]+\)")


def _compile_bounded(
    fragment: str,
    prefix: str = r"\b(",
    suffix: str = r")\b",
    label: str = "the pattern",
) -> re.Pattern:
    """Compile ONE roster line the way the gate does, bounded as a whole word.

    The gate (``tools/name_leak_warn.py: load_roster``) compiles each non-comment
    roster line on its own; it never joins the lines into a single alternation.
    The two are different languages. A line carrying an inline flag
    (``(?i)quillonfex``) is a valid roster entry for the gate, but inside a
    larger alternation ``re`` raises "global flags not at the start of the
    expression". Compiling one line at a time keeps this self-test in the same
    language as the scanner.

    ``re.IGNORECASE`` is applied as before, *unless the line carries its own
    flags* — such a line keeps them, moved back to position 0 so ``re`` accepts
    them, and is not given IGNORECASE on top.

    A line that fails to compile is loud and never a silent skip: print to
    stderr, then exit 1. That follows the gate's precedent (``name-leak-gate.yml``
    validates the roster first and prints the LINE NUMBER only), but here the
    message names ``label`` — the line's 1-based index within the roster and the
    roster's line count, or ``the agent-handle pattern`` — together with the
    exception class and its ``.pos`` offset. It prints neither the line's text
    (not even truncated or ``repr``'d) nor ``str(exc)``: the roster is the
    ``NAME_LEAK_ROSTER`` secret, whose values are real human names, and
    ``re.error.__str__`` and some ``msg`` values embed part of the pattern.
    """
    at_start = _GLOBAL_INLINE_FLAGS.match(fragment)
    if at_start:
        flags_text = at_start.group(0)
        body = fragment[at_start.end():]
        source = flags_text + prefix + body + suffix
        flags = 0
    else:
        source = prefix + fragment + suffix
        flags = re.IGNORECASE
    try:
        return re.compile(source, flags)
    except re.error as exc:
        # Build the message from the class name and offset only: the fragment
        # and str(exc) can each embed part of the secret roster line.
        print(
            f"name_leak_warn: cannot compile {label}: "
            f"{type(exc).__name__} at offset {exc.pos}",
            file=sys.stderr,
        )
        sys.exit(1)


def _matches_any(patterns: list[re.Pattern], text: str) -> bool:
    """True when any compiled roster pattern matches ``text``."""
    return any(pattern.search(text) for pattern in patterns)


def strict_pattern(roster: str) -> list[re.Pattern]:
    """Build the 'no roster name may appear anywhere' patterns.

    One compiled pattern per roster line — and one for the agent-handle
    alternation — never a single joined alternation. Each line is compiled
    exactly the way ``tools/name_leak_warn.py`` compiles it (see
    ``_compile_bounded``), so a roster line the gate accepts cannot make this
    self-test raise. Every pattern is bounded as a whole word, so a name is
    matched as a whole word and a look-alike that merely contains it does not
    fire. Every roster line — and every principal line declared in
    ``NAME_LEAK_PRINCIPAL`` — is a regex inserted as written (never escaped), so
    each line keeps its own anchors and case handling.
    """
    roster_parts = _all_roster_patterns(roster)
    total = len(roster_parts)
    labelled = [
        (part, f"roster line {index} of {total}")
        for index, part in enumerate(roster_parts, 1)
    ]
    labelled.append((_AGENT_HANDLES, "the agent-handle pattern"))
    return [_compile_bounded(part, label=label) for part, label in labelled]


# Workflow-file rule — the principal's functional-vs-marker test (CLAUDE.md
# "Attribution convention", alpha#2270), mechanised per Kepler's #3303
# ruling on issue #3303:
#   (a) any human name the roster covers, other than the principal — must not
#       appear ANYWHERE (case 2 of the convention is held open; the safe side
#       is none).
#   (b) the principal and bare agent handles — attribution markers, allowed
#       ONLY on a YAML comment line (the line's first non-space char is '#');
#       on any other line (step `name:`, `run:`, env, etc.) that position is
#       functional, so it fails.
#   (c) an `@handle` mention of any roster name or agent handle — functional
#       (renders as a live GitHub notification the moment it's quoted
#       anywhere on the platform) — forbidden anywhere, comment or not.
#
# The principal set comes from NAME_LEAK_PRINCIPAL, which repeats the
# principal's roster line (see _parse_roster). Without a declaration the rule
# degrades: case (a) becomes empty and every roster name moves to case (b), so
# nothing false-positives on a policy-allowed attribution comment.
def _workflow_case_patterns(
    roster: str,
) -> tuple[list[re.Pattern], list[re.Pattern], list[re.Pattern]]:
    """Build the (a)/(b)/(c) pattern lists from the same roster the strict rule
    uses, plus the separately declared principal and the agent handles.

    Each roster line is compiled on its own, exactly as the strict rule (and the
    gate) does; the partitioning into cases is unchanged.
    """
    principal, others, declared = _parse_roster(roster)
    if declared:
        case_a_humans, case_b_humans = others, principal
    else:
        _note_degradation_once()
        case_a_humans, case_b_humans = [], principal + others

    roster_lines = _regex_lines(roster)
    total = len(roster_lines)

    def _label(pattern: str) -> str:
        # Name the roster line by its 1-based position. A declared principal
        # that repeats no roster line is not a roster line; say so instead.
        if pattern == _AGENT_HANDLES:
            return "the agent-handle pattern"
        try:
            index = roster_lines.index(pattern)
        except ValueError:
            return "a declared principal pattern"
        return f"roster line {index + 1} of {total}"

    case_a = [_compile_bounded(p, label=_label(p)) for p in case_a_humans]
    case_b = [
        _compile_bounded(p, label=_label(p))
        for p in case_b_humans + [_AGENT_HANDLES]
    ]
    case_c = [
        _compile_bounded(p, prefix="@(", suffix=r")\b", label=_label(p))
        for p in principal + others + [_AGENT_HANDLES]
    ]
    return case_a, case_b, case_c


# Synthetic rosters for the workflow-hygiene fixture cells. These names appear
# nowhere else in the repository; the cells inject them so the rule mechanism
# can be exercised without any real value. (The injected-roster cell above
# uses a different pair, deliberately kept unique to that cell.)
#
# _CASE_ROSTER carries the principal (Aurelio) as an ordinary roster line, so
# the scanner applies it as-written; _CASE_PRINCIPAL repeats that line to
# declare it as the principal, which leaves Ottoline and Maribel as "other"
# names for case (a). Without the declaration the same roster exercises the
# degraded rule.
_CASE_ROSTER = (
    r"\bAurelio\b" + "\n" + r"\bOttoline\b" + "\n" + r"\bMaribel\b"
)
_CASE_PRINCIPAL = r"\bAurelio\b"


def _check_workflow_name_hygiene(
    content: str, roster: str
) -> list[tuple[int, str, str]]:
    """Apply the workflow-file name-hygiene rule (cases a/b/c above) to
    ``content``. Returns a list of (line_number, case, line_text) violations;
    empty means clean. Shared by the real-file test and its fixtures so both
    exercise the identical rule."""
    case_a, case_b, case_c = _workflow_case_patterns(roster)
    violations: list[tuple[int, str, str]] = []
    for i, line in enumerate(content.splitlines(), 1):
        is_comment = line.lstrip().startswith("#")
        if _matches_any(case_a, line):
            violations.append((i, "a", line.strip()[:100]))
        if _matches_any(case_b, line) and not is_comment:
            violations.append((i, "b", line.strip()[:100]))
        if _matches_any(case_c, line):
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
# Test 6 (self-test): the roster-fed pattern must actually be live, proved
# without any real value. alpha#3303's regression guard (a non-raw \b became a
# literal backspace) is preserved here: the pattern must match a synthetic
# name as a whole word, must not match a look-alike that merely contains it,
# and — the discriminating arm — must not match a name absent from the
# injected roster (a match-everything pattern passes the first two).
# ---------------------------------------------------------------------------

def test_roster_text_absent_is_none(monkeypatch):
    """roster_text() is None when NAME_LEAK_ROSTER is unset."""
    monkeypatch.delenv("NAME_LEAK_ROSTER", raising=False)
    assert roster_text() is None


def test_roster_text_empty_is_none(monkeypatch):
    """roster_text() is None when NAME_LEAK_ROSTER is set but empty."""
    monkeypatch.setenv("NAME_LEAK_ROSTER", "")
    assert roster_text() is None


def test_roster_text_returns_value(monkeypatch):
    """roster_text() returns the environment value verbatim when non-empty."""
    monkeypatch.setenv("NAME_LEAK_ROSTER", r"\bXx\b")
    assert roster_text() == r"\bXx\b"


@pytest.mark.parametrize("roster", ["# only a comment", "   "])
def test_require_roster_skips_a_roster_with_no_pattern(monkeypatch, roster):
    """A truthy but patternless NAME_LEAK_ROSTER (comment-only or
    whitespace-only) must take the named skip path. Both inputs compile to
    zero patterns, so a green pass would assert nothing about real names —
    the bug this cell pins is a truthy-string check, which both satisfy."""
    monkeypatch.setenv("NAME_LEAK_ROSTER", roster)
    with pytest.raises(pytest.skip.Exception) as exc_info:
        _require_roster()
    assert _SKIP_REASON in str(exc_info.value), (
        f"the skip must carry the named reason; got {exc_info.value!r}"
    )


def test_strict_pattern_roster_path_is_live(monkeypatch):
    """The injected roster must drive the built pattern: both synthetic
    names match as whole words, look-alikes that merely contain them do not,
    and a name absent from the injected roster does not."""
    monkeypatch.setenv(
        "NAME_LEAK_ROSTER", r"\bQuillonfex\b" + "\n" + r"\bBrimwatley\b"
    )
    monkeypatch.delenv("NAME_LEAK_PRINCIPAL", raising=False)
    roster = roster_text()
    assert roster is not None
    patterns = strict_pattern(roster)

    for name in ("Quillonfex", "Brimwatley"):
        assert _matches_any(patterns, name), (
            f"strict_pattern must match the injected roster name {name!r}"
        )

    for lookalike in ("Quillonfexish", "Brimwatleyville"):
        assert not _matches_any(patterns, lookalike), (
            f"strict_pattern must not match look-alike {lookalike!r}"
        )

    assert not _matches_any(patterns, "Zorblaxim"), (
        "strict_pattern must not match a name absent from the injected roster"
    )


def test_strict_pattern_accepts_a_roster_line_with_inline_flags(monkeypatch):
    """A roster line carrying its own global inline flags is a valid entry for
    the gate — ``load_roster`` compiles each line on its own — so the strict
    matcher must accept it too.

    The discriminating input: joined into one alternation, ``(?i)Quillonfex``
    raises "global flags not at the start of the expression". A fix that only
    keeps plain patterns working does not pass this cell.
    """
    monkeypatch.setenv(
        "NAME_LEAK_ROSTER", "(?i)Quillonfex" + "\n" + r"\bBrimwatley\b"
    )
    monkeypatch.delenv("NAME_LEAK_PRINCIPAL", raising=False)
    roster = roster_text()
    assert roster is not None

    patterns = strict_pattern(roster)

    for name in ("Quillonfex", "quillonfex", "QUILLONFEX", "Brimwatley", "brimwatley"):
        assert _matches_any(patterns, name), (
            f"strict_pattern must match the injected roster name {name!r}"
        )
    assert not _matches_any(patterns, "Quillonfexish"), (
        "a name embedded in a longer word must not fire"
    )
    assert not _matches_any(patterns, "Zorblaxim"), (
        "a name absent from the injected roster must not fire"
    )


# Contract for an uncompilable roster line, stated here and tested by the cell
# below. On such a line the process must (a) exit non-zero, (b) write a
# message to stderr naming the line's 1-based index and total (or, for the
# agent-handle alternation, a label saying so), and (c) write nothing that
# contains the line's text — the roster is the NAME_LEAK_ROSTER secret, whose
# values are real human names, and CI logs can become public. `re.error.__str__`
# and some `msg` values embed part of the pattern, so the message is built from
# `type(exc).__name__` and `exc.pos` only.
def test_compile_error_names_the_line_not_its_text(monkeypatch, capsys):
    """An uncompilable roster line fails loud without echoing the line.

    The bad line carries a token invented for this cell (it appears in no
    roster and nowhere else in the repository), so the green case can only
    come from the token staying out of stderr — not from it being absent
    anyway.
    """
    monkeypatch.delenv("NAME_LEAK_PRINCIPAL", raising=False)
    token = "Qwemberlax"
    roster = r"\bQuillonfex\b" + "\n" + f"*{token}"

    with pytest.raises(SystemExit) as exc_info:
        strict_pattern(roster)

    assert exc_info.value.code == 1, (
        f"an uncompilable roster line must exit 1; got {exc_info.value.code!r}"
    )
    captured = capsys.readouterr()
    assert "roster line 2 of 2" in captured.err, (
        "the message must name the failing line by 1-based index and total; "
        f"got stderr {captured.err!r}"
    )
    assert token not in captured.err, (
        "the compile-error message echoed the roster line's text (the roster "
        f"is a secret); got stderr {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: no-real-names hygiene — tool source file
# ---------------------------------------------------------------------------

def test_tool_file_no_real_names():
    """tools/name_leak_warn.py must contain no real personal/agent names at
    all — the STRICT rule (no roster name anywhere), using the same roster
    source as the scanner (strict_pattern; see #3303).

    Mirrors the example-file hygiene test from test_scan_leaks.py. It checks
    the literal committed file rather than running the scanner; without the
    roster there is nothing to check, so the cell skips rather than passing
    green.
    """
    roster = _require_roster()
    patterns = strict_pattern(roster)
    content = TOOL_PATH.read_text(encoding="utf-8")
    violations = []
    for i, line in enumerate(content.splitlines(), 1):
        if _matches_any(patterns, line):
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

def test_workflow_rule_case_a_blocks_other_human_names_anywhere(monkeypatch):
    """(a) a human name the roster covers must not appear ANYWHERE — comment
    or not — and a name embedded in a longer word must not false-fire while
    the same name standing alone must.

    The roster is injected (synthetic names) so the cell never depends on a
    real value, and it does not read the secret at all — it runs whether or not
    ``NAME_LEAK_ROSTER`` is set.
    """
    monkeypatch.setenv("NAME_LEAK_ROSTER", _CASE_ROSTER)
    monkeypatch.setenv("NAME_LEAK_PRINCIPAL", _CASE_PRINCIPAL)
    roster = roster_text()
    assert roster is not None

    on_comment_line = "# a note mentioning Ottoline's review\n"
    violations = _check_workflow_name_hygiene(on_comment_line, roster)
    assert any(v[1] == "a" for v in violations), f"case (a) should flag Ottoline; got {violations}"

    on_run_line = "      run: echo 'as Maribel said, this works'\n"
    violations = _check_workflow_name_hygiene(on_run_line, roster)
    assert any(v[1] == "a" for v in violations), f"case (a) should flag bare Maribel; got {violations}"

    # Boundary control, both directions: the helper bounds each pattern as a
    # whole word, so an injected *bare* roster name must not fire when it is
    # embedded in a longer word, while the same name standing alone must. The
    # old line/linux/link decoy belonged to a short-name pattern this cell no
    # longer injects, so it could not fail; this pair can.
    boundary_roster = _CASE_ROSTER + "\n" + "Vexalor"
    embedded = "      # keep this on one line: Vexalorian is not the name\n"
    violations = _check_workflow_name_hygiene(embedded, boundary_roster)
    assert not any(v[1] == "a" for v in violations), (
        f"case (a) false-positived on a bare name embedded in a longer word; got {violations}"
    )

    standalone = "      # keep this on one line: Vexalor is the name\n"
    violations = _check_workflow_name_hygiene(standalone, boundary_roster)
    assert any(v[1] == "a" for v in violations), (
        f"case (a) must flag the bare name standing alone; got {violations}"
    )


def test_workflow_rule_case_b_marker_allowed_only_on_comment_lines(monkeypatch):
    """(b) bare agent handles are attribution markers — allowed ONLY on a
    YAML comment line; the same handle on a non-comment line (step `name:`,
    `run:`, env, etc.) is functional, so it fails.

    The roster is injected (synthetic names), so the cell does not read the
    secret and runs whether or not ``NAME_LEAK_ROSTER`` is set."""
    monkeypatch.setenv("NAME_LEAK_ROSTER", _CASE_ROSTER)
    monkeypatch.setenv("NAME_LEAK_PRINCIPAL", _CASE_PRINCIPAL)
    roster = roster_text()
    assert roster is not None

    marker_on_comment = "      # approved by Borges after Ariadne's review\n"
    violations = _check_workflow_name_hygiene(marker_on_comment, roster)
    assert not any(v[1] == "b" for v in violations), (
        f"marker on a comment line should be clean; got {violations}"
    )

    marker_in_run = '      run: echo "Borges did this"\n'
    violations = _check_workflow_name_hygiene(marker_in_run, roster)
    assert any(v[1] == "b" for v in violations), (
        f"marker on a non-comment (run:) line should be flagged; got {violations}"
    )

    marker_in_step_name = "      - name: Borges's gate\n"
    violations = _check_workflow_name_hygiene(marker_in_step_name, roster)
    assert any(v[1] == "b" for v in violations), (
        f"marker in a step name: line should be flagged; got {violations}"
    )

    # New-roster-handle fixture (alpha#3307): Kepler marker on a comment line
    # is clean; the same handle on a run: line is functional and flagged.
    new_handle_marker_on_comment = "      # per Kepler's review, this is fine\n"
    violations = _check_workflow_name_hygiene(new_handle_marker_on_comment, roster)
    assert not any(v[1] == "b" for v in violations), (
        f"new-handle marker on a comment line should be clean; got {violations}"
    )

    new_handle_marker_in_run = '      run: echo "Kepler did this"\n'
    violations = _check_workflow_name_hygiene(new_handle_marker_in_run, roster)
    assert any(v[1] == "b" for v in violations), (
        f"new-handle marker on a non-comment (run:) line should be flagged; got {violations}"
    )


def test_workflow_rule_case_c_blocks_at_handle_mentions_anywhere(monkeypatch):
    """(c) an @handle mention of any roster name is functional (renders as a
    live GitHub notification once quoted on the platform) — forbidden
    anywhere, including inside a comment line; a bare handle with no @ is
    not an (c)-violation (that's case (b)'s territory).

    The roster is injected (synthetic names), so the cell does not read the
    secret and runs whether or not ``NAME_LEAK_ROSTER`` is set."""
    monkeypatch.setenv("NAME_LEAK_ROSTER", _CASE_ROSTER)
    monkeypatch.setenv("NAME_LEAK_PRINCIPAL", _CASE_PRINCIPAL)
    roster = roster_text()
    assert roster is not None

    at_mention_in_comment = "      # cc @Borges for review\n"
    violations = _check_workflow_name_hygiene(at_mention_in_comment, roster)
    assert any(v[1] == "c" for v in violations), (
        f"@handle mention inside a comment should still be flagged; got {violations}"
    )

    bare_handle_in_comment = "      # Borges reviewed this\n"
    violations = _check_workflow_name_hygiene(bare_handle_in_comment, roster)
    assert not any(v[1] == "c" for v in violations), (
        f"bare handle (no @) must not trip case (c); got {violations}"
    )

    # New-roster-handle fixture (alpha#3307): @clio mention is functional
    # (a live GitHub notification once quoted) and forbidden even on a
    # comment line.
    new_handle_at_mention_in_comment = "      # cc @clio\n"
    violations = _check_workflow_name_hygiene(new_handle_at_mention_in_comment, roster)
    assert any(v[1] == "c" for v in violations), (
        f"@handle mention of a new roster name should still be flagged; got {violations}"
    )


def test_workflow_principal_marker_keeps_the_strict_split(monkeypatch):
    """With NAME_LEAK_PRINCIPAL set the principal is a comment-line marker
    (case b) — clean on a comment line, flagged on a run: line — while an
    other roster name is forbidden anywhere (case a)."""
    monkeypatch.setenv("NAME_LEAK_ROSTER", _CASE_ROSTER)
    monkeypatch.setenv("NAME_LEAK_PRINCIPAL", _CASE_PRINCIPAL)
    roster = roster_text()
    assert roster is not None

    principal_on_comment = "      # approved by Aurelio\n"
    violations = _check_workflow_name_hygiene(principal_on_comment, roster)
    assert not any(v[1] in ("a", "b") for v in violations), (
        f"principal on a comment line should be clean; got {violations}"
    )

    principal_in_run = '      run: echo "Aurelio did this"\n'
    violations = _check_workflow_name_hygiene(principal_in_run, roster)
    assert any(v[1] == "b" for v in violations), (
        f"principal on a non-comment (run:) line should be flagged; got {violations}"
    )

    other_on_comment = "      # a note mentioning Ottoline\n"
    violations = _check_workflow_name_hygiene(other_on_comment, roster)
    assert any(v[1] == "a" for v in violations), (
        f"an other roster name must be case (a) anywhere; got {violations}"
    )


def test_workflow_principal_comments_are_skipped(monkeypatch):
    """A `#` line inside NAME_LEAK_PRINCIPAL is a comment, not a declaration:
    only the remaining bare lines declare the principal. The discriminating
    assertion: a commented-out declaration leaves the rule degraded, so an
    other roster name on a comment line is NOT case (a) — if the comment line
    counted, that name would be case (a) there."""
    monkeypatch.setenv("NAME_LEAK_ROSTER", _CASE_ROSTER)
    monkeypatch.setenv(
        "NAME_LEAK_PRINCIPAL", "# " + _CASE_PRINCIPAL + "\n# a note, not a pattern"
    )
    roster = roster_text()
    assert roster is not None

    other_on_comment = "      # a note mentioning Ottoline\n"
    violations = _check_workflow_name_hygiene(other_on_comment, roster)
    assert not any(v[1] == "a" for v in violations), (
        f"a commented-out principal must leave case (a) empty; got {violations}"
    )

    other_in_run = '      run: echo "Ottoline did this"\n'
    violations = _check_workflow_name_hygiene(other_in_run, roster)
    assert any(v[1] == "b" for v in violations), (
        f"degraded mode must still flag the name on a run: line; got {violations}"
    )


def test_workflow_no_principal_marker_degrades_to_non_comment_only(monkeypatch):
    """Without a NAME_LEAK_PRINCIPAL declaration, case (a) is empty and every
    roster name is treated as case (b): clean on a comment line, STILL flagged
    on a run: line. The second pair is the discriminating one — a degradation that
    quietly checked nothing would pass the first pair only."""
    monkeypatch.setenv("NAME_LEAK_ROSTER", _CASE_ROSTER)
    monkeypatch.delenv("NAME_LEAK_PRINCIPAL", raising=False)
    roster = roster_text()
    assert roster is not None

    name_on_comment = "      # a note mentioning Ottoline\n"
    violations = _check_workflow_name_hygiene(name_on_comment, roster)
    assert not any(v[1] in ("a", "b") for v in violations), (
        f"degraded mode must allow the name on a comment line; got {violations}"
    )

    name_in_run = '      run: echo "Ottoline did this"\n'
    violations = _check_workflow_name_hygiene(name_in_run, roster)
    assert any(v[1] == "b" for v in violations), (
        f"degraded mode must STILL flag the name on a run: line; got {violations}"
    )


# ---------------------------------------------------------------------------
# Test 9: workflow hygiene — pr-checks.yml (hosts the name-leak-warn job)
# ---------------------------------------------------------------------------

def test_workflow_file_no_real_names():
    """pr-checks.yml (the name-leak-warn job's home post-#1598) must satisfy
    the principal's functional-vs-marker rule (CLAUDE.md "Attribution
    convention", alpha#2270; mechanised per Kepler's #3303 ruling) — cases
    (a)/(b)/(c), via the shared _check_workflow_name_hygiene helper, exercised
    green here on the real file and red on synthetic fixtures above. Without
    the roster there is no pattern to apply, so the cell skips."""
    roster = _require_roster()
    assert WORKFLOW_PATH.exists(), f"workflow file not found: {WORKFLOW_PATH}"
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    violations = _check_workflow_name_hygiene(content, roster)

    assert not violations, (
        ".github/workflows/pr-checks.yml fails the name-hygiene rule "
        "(case a/b/c — see CLAUDE.md 'Attribution convention'):\n"
        + "\n".join(f"  line {i} [case {c}]: {t}" for i, c, t in violations)
    )


# ---------------------------------------------------------------------------
# CI flags guard: the roster secret must never reach the log
# ---------------------------------------------------------------------------
#
# When a roster line is invalid, load_roster() raises SystemExit inside a
# frame of this module. pytest prints that frame's locals when the traceback
# style is the default (``--tb=auto``, which is what a bare ``-q`` gives), and
# it prints captured stderr separately. Either surface prints the roster's
# values, including the good lines, and these logs can become public. Nothing
# else in the suite sees the runner's flags, so this cell reads the three
# workflow files that invoke this module and asserts the flags that keep the
# secret out of the log.

CI_WORKFLOW_PATHS = (
    REPO_ROOT / ".github" / "workflows" / "pr-checks.yml",
    REPO_ROOT / ".github" / "workflows" / "tests.yml",
    REPO_ROOT / ".github" / "workflows" / "nightly.yml",
)

_THIS_FILE = "tests/test_name_leak_warn.py"


def test_ci_steps_run_this_file_without_captured_output():
    """Every CI step that runs this file must keep the roster out of the log.

    The sweep is a property check over the three workflow files rather than a
    list of command strings, so a moved, renamed or reworded step is still
    checked. For each file: exactly one step invokes this module (a second
    copy would be a second set of flags nobody reviewed), that step passes
    ``--show-capture=no``, and it names its traceback style explicitly —
    ``--tb=short`` or ``--tb=no`` — because pytest's default (``--tb=auto``)
    dumps frame locals, which is where the roster would appear.
    """
    for path in CI_WORKFLOW_PATHS:
        assert path.exists(), f"workflow not found: {path}"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        steps = [
            step
            for job in (doc.get("jobs") or {}).values()
            for step in (job.get("steps") or [])
            if "pytest" in (step.get("run") or "")
            and _THIS_FILE in (step.get("run") or "")
        ]
        assert len(steps) == 1, (
            f"{path.name}: expected exactly one step running {_THIS_FILE}, "
            f"found {len(steps)}; a second copy would be a second set of "
            "flags nobody reviewed"
        )
        command = steps[0]["run"]
        assert "--show-capture=no" in command, (
            f"{path.name}: the step running {_THIS_FILE} does not pass "
            "--show-capture=no; its captured stderr would print the roster"
        )
        assert "--tb=short" in command or "--tb=no" in command, (
            f"{path.name}: the step running {_THIS_FILE} leaves the traceback "
            "style at pytest's default (--tb=auto, which dumps frame locals); "
            "name --tb=short or --tb=no explicitly"
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


# Contract for ``load_roster`` on an uncompilable roster line (engram#4559): exit
# 1 before returning any pattern; write exactly ONE stderr line naming the
# failing line by its RAW 1-based number in the roster text (blank and comment
# lines count, the numbering ``name-leak-gate.yml`` prints) plus the exception
# class and ``.pos``; write NOTHING derived from the line's text (not the line,
# not a truncation, not ``repr``, not ``str(exc)``, not ``exc.msg``). The roster
# is the NAME_LEAK_ROSTER secret and ``pr-checks.yml`` leaves stderr unredirected.
@pytest.mark.parametrize("entry_point", ["roster_file", "roster_source"])
def test_load_roster_compile_error_names_the_line_not_its_text(
    tmp_path, capsys, entry_point
):
    """A bad roster line exits 1 naming raw line 4, echoing none of its text.

    The bad line is ``(?P=<token>)``: CPython raises "unknown group name
    '<token>'", so ``str(exc)`` and ``exc.msg`` both embed the invented token.
    That is asserted below, so a green cell proves the ``str(exc)`` leak is
    covered and not only the ``repr(line)`` leak. The token appears nowhere else
    in the repository.
    """
    token = "Zvimbrelq"
    bad_line = f"(?P={token})"
    with pytest.raises(re.error) as probe:
        re.compile(bad_line)
    assert token in str(probe.value) and token in probe.value.msg, (
        "precondition: this CPython must embed the token in str(exc) and exc.msg"
    )

    # Raw numbering: valid line, comment, blank, then the bad line = line 4.
    roster = "\\bQuillonfex\\b\n# a comment\n\n" + bad_line + "\n"
    if entry_point == "roster_file":
        roster_file = tmp_path / "roster.txt"
        roster_file.write_text(roster, encoding="utf-8")
        kwargs = {"roster_source": None, "roster_file": str(roster_file)}
    else:
        kwargs = {"roster_source": roster, "roster_file": None}

    with pytest.raises(SystemExit) as exc_info:
        load_roster(**kwargs)

    assert exc_info.value.code == 1, (
        f"an uncompilable roster line must exit 1; got {exc_info.value.code!r}"
    )
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1, f"exactly one stderr line; got {err!r}"
    assert "roster line 4" in err, (
        f"the message must name the raw 1-based line number 4; got {err!r}"
    )
    assert f"offset {probe.value.pos}" in err, (
        f"the message must carry exc.pos ({probe.value.pos}); got {err!r}"
    )
    assert type(probe.value).__name__ in err, (
        f"the message must carry the exception class name; got {err!r}"
    )
    assert token not in err, f"the roster line's text reached stderr: {err!r}"
    assert bad_line not in err, f"the roster line reached stderr: {err!r}"
    assert probe.value.msg not in err and "unknown group name" not in err, (
        f"str(exc) / exc.msg reached stderr: {err!r}"
    )


# The one-line contract also holds for compile failures that are not ``re.error``
# or whose ``.pos`` is None (#4711 review). ``RecursionError`` (deeply nested
# parentheses) is caught by the same clause but has no cell here: provoking it is
# slow and depends on the interpreter's recursion limit.
def _assert_one_line_unknown_offset(capsys, bad_line, token, class_name):
    with pytest.raises(SystemExit) as exc_info:
        load_roster(roster_source="\\bQuillonfex\\b\n" + bad_line + "\n", roster_file=None)
    assert exc_info.value.code == 1, f"must exit 1; got {exc_info.value.code!r}"
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1, f"exactly one stderr line; got {err!r}"
    assert "roster line 2" in err, f"must name raw line 2; got {err!r}"
    assert class_name in err, f"must carry {class_name}; got {err!r}"
    assert "offset unknown" in err, f"no int pos, so 'offset unknown'; got {err!r}"
    assert token not in err, f"the roster line's text reached stderr: {err!r}"


def test_load_roster_overflow_error_is_one_line_without_text(capsys):
    """``<Token>{99999999999}`` raises OverflowError (no ``.pos``), not re.error."""
    token = "Xelvandroq"
    bad_line = f"{token}{{99999999999}}"
    try:
        re.compile(bad_line)
    except OverflowError:
        pass
    except re.error:
        pytest.skip("this Python raises re.error, not OverflowError, for the line")
    else:
        pytest.skip("this Python compiles the line; no OverflowError to test")
    _assert_one_line_unknown_offset(capsys, bad_line, token, "OverflowError")


def test_load_roster_pos_none_error_reports_offset_unknown(capsys):
    """A re.error whose ``.pos`` is None prints ``offset unknown``, not ``None``."""
    token = "Ysparnovik"
    bad_line = f"(?<={token}+)x"
    try:
        re.compile(bad_line)
    except re.error as probe:
        if probe.pos is not None:
            pytest.skip(f"this Python sets pos={probe.pos!r} for the line")
        # The class name is "error" on 3.12 and "PatternError" on 3.13+.
        class_name = type(probe).__name__
    else:
        pytest.skip("this Python compiles the line; no re.error to test")
    _assert_one_line_unknown_offset(capsys, bad_line, token, class_name)


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


# ---------------------------------------------------------------------------
# Roster secret scope guard: NAME_LEAK_ROSTER binds only in a step's `env`
# ---------------------------------------------------------------------------
#
# The roster holds real personal names. A step-level `env:` hands it to exactly
# the step that needs it; a workflow- or job-level `env:` hands it to every step
# in that scope, including steps that dump their environment or run third-party
# actions. The cells elsewhere in this file read the variable's VALUE; none of
# them reads where the YAML binds it, so moving a binding up a level would leave
# every one of them green. These cells read the binding's location.
#
# The sweep is over a glob of .github/workflows/ rather than the three-file
# tuple used by the CI-flags guard, so a workflow added later is covered the
# day it lands. The check is deliberately key-based: it flags a literal
# `NAME_LEAK_ROSTER` key and never scans a value that embeds or aliases the
# name. The secret's value is never read here — only the key's location.

_ROSTER_VAR = "NAME_LEAK_ROSTER"
_WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
_WORKFLOW_FILES = tuple(
    sorted(
        path
        for path in _WORKFLOWS_DIR.glob("*")
        if path.suffix in (".yml", ".yaml")
    )
)


def _roster_bindings_outside_steps(doc: dict) -> list[str]:
    """Dotted paths where a workflow doc binds NAME_LEAK_ROSTER outside a step env.

    Returns [] for a clean doc. Each entry is a JSON-ish path a human can grep:
      "env.NAME_LEAK_ROSTER"                            (workflow-level env)
      "jobs.<job_id>.env.NAME_LEAK_ROSTER"              (job-level env)
      "jobs.<job_id>.container.env.NAME_LEAK_ROSTER"    (job container env)
      "jobs.<job_id>.services.<svc>.env.NAME_LEAK_ROSTER" (service env)
      "jobs.<job_id>.steps[3].with.NAME_LEAK_ROSTER"    (step `with`)
      "jobs.<job_id>.steps[3].env.NAME_LEAK_ROSTER"      -> NOT reported (legal)
    A step's `env` is the only legal location. A step's `with` is reported,
    because a `with:` value is action input, not an environment binding, and
    this check is about the environment. A job's ``container.env`` and each
    ``services.<id>.env`` apply the variable to the job's containers rather
    than to one step, so they are outside a step in the same sense — a service
    container is often a third-party image, so handing it the roster is an
    exposure surface. No workflow uses ``container:`` or ``services:`` today;
    those legs are latent coverage. The check is key-based: it flags a literal
    ``NAME_LEAK_ROSTER`` key and does not scan a value that embeds or aliases
    the name.
    """
    found: list[str] = []

    workflow_env = doc.get("env")
    if isinstance(workflow_env, dict) and _ROSTER_VAR in workflow_env:
        found.append(f"env.{_ROSTER_VAR}")

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return found

    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        job_env = job.get("env")
        if isinstance(job_env, dict) and _ROSTER_VAR in job_env:
            found.append(f"jobs.{job_id}.env.{_ROSTER_VAR}")
        container = job.get("container")
        if isinstance(container, dict):
            container_env = container.get("env")
            if isinstance(container_env, dict) and _ROSTER_VAR in container_env:
                found.append(f"jobs.{job_id}.container.env.{_ROSTER_VAR}")
        services = job.get("services")
        if isinstance(services, dict):
            for service_id, service in services.items():
                if not isinstance(service, dict):
                    continue
                service_env = service.get("env")
                if isinstance(service_env, dict) and _ROSTER_VAR in service_env:
                    found.append(
                        f"jobs.{job_id}.services.{service_id}.env.{_ROSTER_VAR}"
                    )
        for index, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            step_with = step.get("with")
            if isinstance(step_with, dict) and _ROSTER_VAR in step_with:
                found.append(f"jobs.{job_id}.steps[{index}].with.{_ROSTER_VAR}")

    return found


# Synthetic docs for the scope table. Each must-flag case names the exact dotted
# path it expects; the last case is the positive control — a step-level `env` is
# the one legal location and must return [] so the function cannot reject
# everything and still look green.
_ROSTER_BINDING_CASES = (
    pytest.param(
        {"env": {_ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"}},
        ["env.NAME_LEAK_ROSTER"],
        id="workflow-env",
    ),
    pytest.param(
        {
            "jobs": {
                "build": {
                    "env": {_ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"},
                }
            }
        },
        ["jobs.build.env.NAME_LEAK_ROSTER"],
        id="job-env",
    ),
    pytest.param(
        {
            "jobs": {
                "build": {
                    "container": {
                        "image": "example/image:1",
                        "env": {_ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"},
                    }
                }
            }
        },
        ["jobs.build.container.env.NAME_LEAK_ROSTER"],
        id="job-container-env",
    ),
    pytest.param(
        {
            "jobs": {
                "build": {
                    "services": {
                        "db": {
                            "image": "example/postgres:1",
                            "env": {
                                _ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"
                            },
                        }
                    }
                }
            }
        },
        ["jobs.build.services.db.env.NAME_LEAK_ROSTER"],
        id="job-service-env",
    ),
    pytest.param(
        {
            "jobs": {
                "build": {
                    "steps": [
                        {"run": "echo first"},
                        {
                            "uses": "example/action@v1",
                            "with": {
                                _ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"
                            },
                        },
                    ]
                }
            }
        },
        ["jobs.build.steps[1].with.NAME_LEAK_ROSTER"],
        id="step-with",
    ),
    pytest.param(
        {
            "jobs": {
                "build": {
                    "steps": [
                        {
                            "run": "echo legal",
                            "env": {
                                _ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"
                            },
                        }
                    ]
                }
            }
        },
        [],
        id="step-env-is-legal",
    ),
)


@pytest.mark.parametrize("path", _WORKFLOW_FILES, ids=lambda path: path.name)
def test_real_workflows_bind_roster_only_in_step_env(path):
    """No workflow may bind NAME_LEAK_ROSTER outside a step's `env`.

    Covers every *.yml / *.yaml in .github/workflows/ by glob, so a workflow
    added after this test was written is still checked.
    """
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), (
        f"{path.name}: yaml.safe_load did not return a mapping (got "
        f"{type(doc).__name__}); refusing to read that as a clean pass"
    )
    offenders = _roster_bindings_outside_steps(doc)
    assert offenders == [], (
        f"{path.name} binds {_ROSTER_VAR} outside a step env: "
        f"{', '.join(offenders)}. A workflow- or job-level env hands the "
        "secret to every step in that scope; bind it on the one step that "
        "needs it."
    )


def test_roster_step_env_bindings_are_non_vacuous():
    """The scope guard must be checking real bindings, not an empty set.

    Half (a): at least one step-level binding exists somewhere in the workflow
    set, so a wholesale rename cannot leave the sweep green over nothing.
    Half (b): tests.yml and pr-checks.yml each still bind it at step level —
    those are the two workflows that must keep running the real-roster
    self-test.
    """
    assert _WORKFLOW_FILES, (
        f"no *.yml/*.yaml found under {_WORKFLOWS_DIR}; the scope guard is "
        "vacuous"
    )

    step_env_files: dict[str, int] = {}
    for path in _WORKFLOW_FILES:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(doc, dict), (
            f"{path.name}: yaml.safe_load did not return a mapping (got "
            f"{type(doc).__name__}); refusing to read that as a clean pass"
        )
        count = len(_roster_step_env_keys(doc))
        if count:
            step_env_files[path.name] = count

    assert step_env_files, (
        f"no workflow binds {_ROSTER_VAR} at step level; the scope guard "
        "would pass over nothing and a wholesale rename would go unnoticed"
    )
    for required in ("tests.yml", "pr-checks.yml"):
        assert step_env_files.get(required, 0) >= 1, (
            f"{required} no longer carries a step-level {_ROSTER_VAR} "
            "binding; those are the two workflows that must keep the "
            "real-roster self-test running"
        )


@pytest.mark.parametrize("doc,expected", _ROSTER_BINDING_CASES)
def test_roster_bindings_outside_steps_scope_table(doc, expected):
    """Flag workflow env, job env, job container/service env and step `with`;
    allow a step `env`."""
    assert _roster_bindings_outside_steps(doc) == expected


# ---------------------------------------------------------------------------
# Roster step-env allowlist: only the named steps may carry the secret
# ---------------------------------------------------------------------------
#
# The scope sweeper above rejects every location but a step's `env`. Within a
# step's `env`, the issue's closing condition is narrower still: only the steps
# that actually need the roster may carry it. This allowlist names the six
# sites that existed when it was written, derived from the YAML itself.
#
# A step identity is the step's `name` when it has one, else its `id`, else its
# 0-based index — the same precedence applied by the scan below.
#
# The comparison is over MULTISETS, not sets: two steps in one job that resolve
# to the same identity are two bindings, so the second fails as a surplus
# instead of collapsing into the first. Equality is asserted in BOTH
# directions: a surplus step-level binding fails (the closing condition), and
# an allowlisted entry with no matching step also fails, so deleting a step
# cannot silently leave a stale permission behind.

_ROSTER_STEP_ALLOWLIST: dict[str, frozenset[tuple[str, str | int]]] = {
    "name-leak-gate.yml": frozenset(
        {
            ("name-leak-gate", "decide the gate verdict"),
        }
    ),
    "nightly.yml": frozenset(
        {
            ("pytest", "Name-leak-warn self-test (real roster)"),
        }
    ),
    "pr-checks.yml": frozenset(
        {
            ("pr-checks", "name-leak-warn — check for name-leak roster secret"),
            ("pr-checks", "name-leak-warn — compute diff and scan for name mentions"),
            (
                "pr-checks",
                "invariant-checks — name-leak-warn self-test with the real roster",
            ),
        }
    ),
    "tests.yml": frozenset(
        {
            ("pytest", "Run pytest — name-leak-warn self-test (real roster)"),
        }
    ),
}


def _roster_step_env_keys(doc: dict) -> dict[tuple[str, str | int], list[int]]:
    """Map each step-env binding's (job_id, step_identity) to its step indices.

    Step identity is the step's ``name`` when it has one, else its ``id``, else
    its 0-based index. The value is the list of 0-based indices that resolved to
    that key — a list rather than a count so a duplicate identity can be named
    by the index of the surplus occurrence. Only a step's own ``env`` counts
    here: workflow-level and job-level env, a job ``container.env`` and a
    ``services.*.env`` are the sweeper's concern.
    """
    occurrences: dict[tuple[str, str | int], list[int]] = {}
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return occurrences
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for index, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            env = step.get("env")
            if not (isinstance(env, dict) and _ROSTER_VAR in env):
                continue
            if step.get("name"):
                identity: str | int = step["name"]
            elif step.get("id"):
                identity = step["id"]
            else:
                identity = index
            occurrences.setdefault((job_id, identity), []).append(index)
    return occurrences


def _format_step(identity: str | int, index: int | None = None) -> str:
    """Render a step identity for a failure message.

    An index-identified step is printed as "step index N (0-based)" rather than
    a bare integer. When ``index`` is given, it is the step's 0-based position
    in the job — load-bearing for a duplicate identity, where the identity is
    shared and only the index tells the two steps apart.
    """
    if isinstance(identity, int):
        return f"step index {identity} (0-based)"
    if index is None:
        return f"step {identity!r}"
    return f"step {identity!r} at step index {index}"


def _roster_step_env_mismatches(
    doc: dict,
    allowed: frozenset[tuple[str, str | int]] | set[tuple[str, str | int]],
) -> tuple[list[tuple[str, str | int, int]], list[tuple[str, str | int]]]:
    """Return (surplus, missing) step-env keys relative to ``allowed``.

    The comparison is a MULTISET one (``collections.Counter`` on both sides):
    two steps that resolve to the same identity are two bindings. ``surplus``
    holds one ``(job_id, identity, step_index)`` per binding beyond the allowed
    count, so a second step reusing an allowlisted identity is named by its own
    index. ``missing`` holds one ``(job_id, identity)`` per allowlisted entry
    with no matching step.
    """
    occurrences = _roster_step_env_keys(doc)
    actual = Counter(
        {key: len(indices) for key, indices in occurrences.items()}
    )
    expected = Counter(allowed)

    surplus: list[tuple[str, str | int, int]] = []
    for key in actual - expected:
        job_id, identity = key
        allowed_count = expected.get(key, 0)
        for index in sorted(occurrences[key])[allowed_count:]:
            surplus.append((job_id, identity, index))

    missing: list[tuple[str, str | int]] = []
    for key, shortfall in (expected - actual).items():
        job_id, identity = key
        for _ in range(shortfall):
            missing.append((job_id, identity))

    surplus.sort(key=repr)
    missing.sort(key=repr)
    return surplus, missing


# Synthetic docs for the allowlist comparison. The first case is the positive
# control: a doc whose step-env binding IS allowlisted returns clean, so the
# check cannot pass by rejecting every doc. The remaining cases pin each
# mismatch direction and the name -> id -> index identity precedence.
_ROSTER_STEP_ENV_CASES = (
    pytest.param(
        {
            "jobs": {
                "build": {
                    "steps": [
                        {
                            "name": "allowed",
                            "env": {_ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"},
                        }
                    ]
                }
            }
        },
        frozenset({("build", "allowed")}),
        [],
        [],
        id="allowlisted-step-env-is-clean",
    ),
    pytest.param(
        {
            "jobs": {
                "build": {
                    "steps": [
                        {
                            "name": "unlisted",
                            "env": {_ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"},
                        }
                    ]
                }
            }
        },
        frozenset(),
        [("build", "unlisted", 0)],
        [],
        id="unlisted-step-env-flagged",
    ),
    pytest.param(
        {"jobs": {"build": {"steps": [{"run": "echo nothing"}]}}},
        frozenset({("build", "gone")}),
        [],
        [("build", "gone")],
        id="stale-allowlist-entry-flagged",
    ),
    pytest.param(
        {
            "jobs": {
                "build": {
                    "steps": [
                        {
                            "name": "the-name",
                            "id": "the-id",
                            "env": {_ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"},
                        }
                    ]
                }
            }
        },
        frozenset({("build", "the-name")}),
        [],
        [],
        id="identity-prefers-name",
    ),
    pytest.param(
        {
            "jobs": {
                "build": {
                    "steps": [
                        {
                            "id": "the-id",
                            "env": {_ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"},
                        }
                    ]
                }
            }
        },
        frozenset({("build", "the-id")}),
        [],
        [],
        id="identity-falls-back-to-id",
    ),
    pytest.param(
        {
            "jobs": {
                "build": {
                    "steps": [
                        {"run": "first"},
                        {
                            "run": "second",
                            "env": {_ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"},
                        },
                    ]
                }
            }
        },
        frozenset({("build", 1)}),
        [],
        [],
        id="identity-falls-back-to-index",
    ),
    # A duplicate identity is a second binding, not a set collision: the
    # allowlist permits one step named "dup", so the step at index 1 is surplus.
    pytest.param(
        {
            "jobs": {
                "build": {
                    "steps": [
                        {
                            "name": "dup",
                            "env": {_ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"},
                        },
                        {
                            "name": "dup",
                            "env": {_ROSTER_VAR: "${{ secrets.NAME_LEAK_ROSTER }}"},
                        },
                    ]
                }
            }
        },
        frozenset({("build", "dup")}),
        [("build", "dup", 1)],
        [],
        id="duplicate-identity-flagged",
    ),
)


def test_roster_step_env_bindings_match_allowlist():
    """Only allowlisted steps may bind NAME_LEAK_ROSTER in their own env.

    The comparison is a multiset, so a second step that reuses an allowlisted
    identity is a surplus binding and fails, named by its own step index. An
    allowlisted entry with no matching step (or a missing allowlisted workflow
    file) fails as a stale permission.
    """
    present = {path.name: path for path in _WORKFLOW_FILES}
    problems: list[str] = []
    for name in sorted(set(present) | set(_ROSTER_STEP_ALLOWLIST)):
        allowed = _ROSTER_STEP_ALLOWLIST.get(name, frozenset())
        if name not in present:
            for job_id, identity in sorted(allowed, key=repr):
                problems.append(
                    f"{name}: allowlisted workflow file is missing "
                    f"(expected job {job_id!r}, {_format_step(identity)})"
                )
            continue
        doc = yaml.safe_load(present[name].read_text(encoding="utf-8"))
        assert isinstance(doc, dict), (
            f"{name}: yaml.safe_load did not return a mapping (got "
            f"{type(doc).__name__}); refusing to read that as a clean pass"
        )
        surplus, missing = _roster_step_env_mismatches(doc, allowed)
        for job_id, identity, index in surplus:
            problems.append(
                f"{name}: {_format_step(identity, index)} in job {job_id!r} "
                f"binds {_ROSTER_VAR} beyond what the allowlist permits"
            )
        for job_id, identity in missing:
            problems.append(
                f"{name}: allowlisted {_format_step(identity)} in job "
                f"{job_id!r} no longer exists"
            )
    assert problems == [], (
        f"{_ROSTER_VAR} step-env allowlist mismatch:\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize(
    "doc,allowed,expected_surplus,expected_missing", _ROSTER_STEP_ENV_CASES
)
def test_roster_step_env_allowlist_table(
    doc, allowed, expected_surplus, expected_missing
):
    """The allowlist comparison flags both directions and accepts a legal doc."""
    assert _roster_step_env_mismatches(doc, allowed) == (
        expected_surplus,
        expected_missing,
    )
