"""CODEOWNERS coverage guard for gate machinery (engram-agents/engram).

Contract
--------
``owners_for(path)`` returns the list of owners that govern ``path`` under
GitHub's CODEOWNERS rules, or an empty list when no pattern matches.

The guard cell passes iff every tracked file under ``.github/workflows/``
resolves to at least one owner, and iff every path named in
``COVERED_SCOPES`` (the scopes ``.github/CODEOWNERS`` claims) resolves to at
least one owner.

Matching subset implemented here (NOT full CODEOWNERS fidelity)
--------------------------------------------------------------
Only the subset the repository's file needs is implemented:

  * anchored patterns (``/docs/foo`` is repository-relative); a slash at the
    beginning or in the middle — but not a trailing slash — also makes a
    pattern repository-relative, as GitHub reads it;
  * directory patterns ending in ``/``, matching the directory and
    everything beneath it;
  * ``*`` matching within a single path segment;
  * ``**`` matching across path segments (including zero of them) only in
    the three gitignore positions (``**/``, ``/**``, ``/**/``); any other
    consecutive asterisks are ordinary ``*``s, which cannot cross a ``/``;
  * blank lines and lines whose first non-space character is ``#``;
  * owners returned as written (``@user``, ``@org/team``, or an email);
    a line with no owner token is ignored.

Deliberately NOT implemented: ``?``, character classes (``[abc]``),
backslash escapes, negation (``!`` is gitignore-only and invalid in
CODEOWNERS), and GitHub's remaining gitignore corner cases. Unsupported
characters are matched literally. A matcher that silently diverged from
GitHub would pass this guard while GitHub read the file differently, so
the tests below pin the subset rather than assume it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CODEOWNERS_PATH = REPO_ROOT / ".github" / "CODEOWNERS"

# The gate-machinery owners, in the order the real file lists them. Asserted by
# value, never by count: the list is the contract, and a count would let the
# membership rot silently one round after the number stopped matching.
EXPECTED_OWNERS = ["@kepler-somnium", "@borges-funes"]

# The scopes .github/CODEOWNERS claims for gate machinery, named once. Both the
# per-path cells and the aggregate guard cell are driven from this tuple, so a
# scope cannot be guarded in one place and silently dropped from the other.
# (path, why it is covered)
COVERED_SCOPES: tuple[tuple[str, str], ...] = (
    (
        ".github/workflows",
        "every gate workflow runs from this directory",
    ),
    (
        ".github/CODEOWNERS",
        "the owners list must cover itself or anyone can edit it",
    ),
    (
        "docs/internal/PR-MERGE-POLICY.md",
        "the merge policy this gate machinery exists to enforce",
    ),
)


def _read_codeowners() -> str:
    return CODEOWNERS_PATH.read_text(encoding="utf-8")


def _tracked_workflow_files() -> list[str]:
    """Tracked files under .github/workflows — git, never a filesystem walk.

    An untracked scratch file in a developer's checkout must not fail the
    gate, and a file deleted from the tree but left on disk must not pass it.
    """
    completed = subprocess.run(
        ["git", "ls-files", ".github/workflows"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Implementation (written after the cells above were observed failing)
# ---------------------------------------------------------------------------


def _segment_to_regex(segment: str) -> str:
    """Translate one path segment: ``*`` stays inside it, the rest is literal."""
    parts: list[str] = []
    for char in segment:
        if char == "*":
            parts.append("[^/]*")
        else:
            parts.append(re.escape(char))
    return "".join(parts)


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.startswith("/")
    if anchored:
        pattern = pattern[1:]
    dir_only = pattern.endswith("/")
    if dir_only:
        pattern = pattern[:-1]
    if not anchored and "/" in pattern:
        # A slash at the beginning or in the middle makes the pattern
        # repository-relative. The trailing slash was stripped above, so a
        # trailing slash does NOT anchor: `apps/` and `docs/` stay unanchored,
        # which is what GitHub does.
        anchored = True

    segments = pattern.split("/")
    parts = ["^"]
    if not anchored:
        parts.append("(?:.*/)?")
    for index, segment in enumerate(segments):
        previous_is_double_star = index > 0 and segments[index - 1] == "**"
        if segment == "**":
            # `**` is special in exactly three positions: leading `**/`,
            # trailing `/**` and middle `/**/`. Anywhere else it is two
            # ordinary `*`s and stays inside one segment.
            if not previous_is_double_star and index > 0:
                parts.append("/")
            if index == len(segments) - 1:
                parts.append(".*")  # trailing `/**`: everything inside
            else:
                parts.append("(?:.*/)?")  # zero or more whole segments
        else:
            if not previous_is_double_star and index > 0:
                parts.append("/")
            parts.append(_segment_to_regex(segment))
    if dir_only:
        parts.append("(?:/.*)?")
    parts.append("$")
    return re.compile("".join(parts))


def parse_codeowners(text: str) -> list[tuple[re.Pattern[str], list[str]]]:
    """Parse CODEOWNERS text into (compiled pattern, owners) in file order."""
    entries: list[tuple[re.Pattern[str], list[str]]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        pattern, owner_fields = fields[0], fields[1:]
        owners = [field for field in owner_fields if "@" in field]
        if not owners:
            continue
        entries.append((_pattern_to_regex(pattern), owners))
    return entries


def owners_for(path: str, codeowners_text: str | None = None) -> list[str]:
    """Owners governing ``path``; later matching lines override earlier ones."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")

    text = _read_codeowners() if codeowners_text is None else codeowners_text
    owners: list[str] = []
    for pattern, line_owners in parse_codeowners(text):
        if pattern.match(normalized):
            owners = line_owners
    return list(owners)


# ---------------------------------------------------------------------------
# Contract cells
# ---------------------------------------------------------------------------


def test_workflow_path_covered_only_by_dot_github_line_resolves_to_expected_owners():
    """Cell 1: a workflow path covered only by `/.github/` gets the owners."""
    assert owners_for(".github/workflows/nightly.yml") == EXPECTED_OWNERS


def test_codeowners_file_resolves_to_at_least_one_owner():
    """Cell 2: the self-reference — CODEOWNERS must cover itself."""
    assert owners_for(".github/CODEOWNERS"), ".github/CODEOWNERS must cover itself"


def test_unmatched_path_resolves_to_empty_list():
    """Cell 3: a path no line matches resolves to []."""
    assert owners_for("README.md") == []


def test_last_matching_pattern_wins():
    """Cell 4: with two matching lines, the later line's owners are returned."""
    synthetic = (
        "# synthetic file: the narrower second line must win\n"
        "/docs/ @first\n"
        "/docs/internal/ @second @third\n"
    )
    assert owners_for("docs/internal/PR-MERGE-POLICY.md", synthetic) == [
        "@second",
        "@third",
    ]


def test_guard_cell_every_tracked_workflow_and_codeowners_have_an_owner():
    """Cell 5: the real repository file satisfies the guard for every scope."""
    tracked = _tracked_workflow_files()
    assert tracked, (
        "the workflow enumeration is empty, so the guard would otherwise pass "
        "vacuously: git ls-files found no tracked file under .github/workflows"
    )
    missing = [p for p in tracked if not owners_for(p)]
    assert not missing, f"tracked workflow files with no CODEOWNERS owner: {missing}"
    uncovered = [
        f"{path} ({why})" for path, why in COVERED_SCOPES if not owners_for(path)
    ]
    assert not uncovered, f"scopes claimed as covered but unowned: {uncovered}"


# ---------------------------------------------------------------------------
# Pins for the documented matching subset
# ---------------------------------------------------------------------------


def test_star_matches_within_one_segment_only():
    synthetic = "/docs/*.md @docs\n"
    assert owners_for("docs/README.md", synthetic) == ["@docs"]
    assert owners_for("docs/nested/README.md", synthetic) == []


def test_double_star_matches_across_segments_including_none():
    synthetic = "/docs/**/README.md @docs\n"
    assert owners_for("docs/README.md", synthetic) == ["@docs"]
    assert owners_for("docs/a/b/README.md", synthetic) == ["@docs"]


def test_directory_pattern_matches_everything_beneath_it():
    synthetic = "/build/ @infra\n"
    assert owners_for("build/output/app.bin", synthetic) == ["@infra"]


def test_pattern_without_a_slash_matches_at_any_depth():
    synthetic = "*.js @js\n"
    assert owners_for("app.js", synthetic) == ["@js"]
    assert owners_for("web/app.js", synthetic) == ["@js"]


def test_comments_and_blank_lines_are_ignored():
    synthetic = "# a comment\n\n   # an indented comment\n/docs/ @docs\n"
    assert owners_for("docs/x.md", synthetic) == ["@docs"]


# ---------------------------------------------------------------------------
# Round-2 cells: every claimed scope, and `**` only where gitignore makes it
# special
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope,why",
    COVERED_SCOPES,
    ids=[scope for scope, _ in COVERED_SCOPES],
)
def test_each_named_scope_resolves_to_expected_owners(scope: str, why: str) -> None:
    """Every scope the file claims is covered, driven off COVERED_SCOPES."""
    owners = owners_for(scope)
    assert owners == EXPECTED_OWNERS, (
        f"{scope} is claimed as covered ({why}) but resolves to {owners!r}"
    )


def test_double_star_is_special_only_in_the_three_gitignore_positions():
    """`**/`, `/**` and `/**/` keep their cross-segment meaning."""
    synthetic = "**/logs @anywhere\n/docs/** @docs\n/a/**/b @ab\n"
    assert owners_for("logs", synthetic) == ["@anywhere"]
    assert owners_for("deep/nested/logs", synthetic) == ["@anywhere"]
    assert owners_for("docs/x.md", synthetic) == ["@docs"]
    assert owners_for("docs/deep/nested/x.md", synthetic) == ["@docs"]
    assert owners_for("a/b", synthetic) == ["@ab"]
    assert owners_for("a/x/b", synthetic) == ["@ab"]
    assert owners_for("a/x/y/b", synthetic) == ["@ab"]


def test_double_star_outside_the_special_positions_stays_inside_one_segment():
    """`a**b` is two ordinary `*`s and cannot match across a slash."""
    synthetic = "a**b @ab\n"
    assert owners_for("aXXb", synthetic) == ["@ab"]
    assert owners_for("a/x/b", synthetic) == []
