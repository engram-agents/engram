"""CODEOWNERS coverage guard for gate machinery (engram-agents/engram).

Contract
--------
``owners_for(path, codeowners_text)`` returns the owners of the **last
matching line**, and that includes when the list is empty: an ownerless
matching line resolves to ``[]`` and shadows any earlier match. A path no
line matches also resolves to ``[]``. Both results are the same value to the
caller but mean different things in the file, and GitHub reads the second as
*ownership removed* — changes there can be approved by anyone with write
access — so the guard must see ``[]`` for it rather than the earlier match's
owners.

The file governs two owner sets, asserted and driven separately:

  * ``GATE_OWNERS`` — the owners of gate machinery (``.github/`` and the merge
    policy). The guard cell passes iff every tracked file under
    ``.github/workflows/`` resolves to ``GATE_OWNERS`` (asserted by value, not
    merely non-empty), and iff every path named in ``COVERED_SCOPES`` (the
    scopes ``.github/CODEOWNERS`` claims for gate machinery) resolves to at
    least one owner.
  * ``INSTRUCTION_OWNERS`` — the owners of the instruction-bearing sources (the
    root ``CLAUDE.md`` and the hook, skill, agent and template trees). Every
    scope named in ``INSTRUCTION_SCOPES`` resolves to exactly
    ``INSTRUCTION_OWNERS``, and so does every *tracked file beneath* each of
    them — the property, not just the directories, so an ownerless line placed
    after a directory pattern cannot remove one file's ownership unseen.

Today both sets hold ``["@kepler-somnium", "@borges-funes"]``. That is
correctness, not equivalence: the two are expected to diverge once the expanded
owner list is confirmed, so each is its own literal and a cell below asserts
they are not one shared object.

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
  * owners returned as written (``@user``, ``@org/team``, or an email); a
    matching line with no owner token is not skipped — it resolves to no
    owners and, as the last match, shadows any earlier one, which is how
    GitHub reads it (ownership removed).

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
GATE_OWNERS = ["@kepler-somnium", "@borges-funes"]

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

# The instruction-bearing owners: the files that change what every agent does
# without touching a line of code. A separate literal, never a reference to
# GATE_OWNERS — the two are expected to diverge when the expanded owner list is
# confirmed, and a shared literal would let an edit to one silently move the
# other. They hold the same values today; that is correctness, not equivalence.
INSTRUCTION_OWNERS = ["@kepler-somnium", "@borges-funes"]

# The scopes .github/CODEOWNERS claims for instruction-bearing sources, in the
# same (path, why it is covered) shape as COVERED_SCOPES. The per-scope cells
# and the tracked-file property cell both drive off this tuple, for the same
# reason: a scope cannot be guarded in one cell and dropped from another.
INSTRUCTION_SCOPES: tuple[tuple[str, str], ...] = (
    (
        "CLAUDE.md",
        "the project conventions every agent session loads",
    ),
    (
        "src/engram/hooks",
        "hooks change what every session does at its event boundaries",
    ),
    (
        "src/engram/skills",
        "skills are the on-demand procedure every agent follows",
    ),
    (
        "src/engram/agents",
        "agent specs define what each dispatched fairy is charged to do",
    ),
    (
        "src/engram/templates",
        "templates seed the instruction files installed into user repos",
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


def _tracked_files(scope: str) -> list[str]:
    """Tracked files under ``scope`` — git, never a filesystem walk.

    Same two reasons as ``_tracked_workflow_files``: an untracked scratch file
    in a developer's checkout must not fail the gate, and a file deleted from
    the tree but left on disk must not pass it.
    """
    completed = subprocess.run(
        ["git", "ls-files", scope],
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
        # Keep the entry even when it lists no owner: an ownerless matching
        # line is a real instruction in CODEOWNERS — GitHub reads it as
        # ownership removed — so it must be able to shadow an earlier match.
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
    assert owners_for(".github/workflows/nightly.yml") == GATE_OWNERS


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
    """Cell 5: the real repository file satisfies the guard for every scope.

    The workflows are asserted by value, against the same ``GATE_OWNERS``
    the named scopes are held to: a tracked workflow owned by *someone else*
    is unowned as far as this gate machinery goes, so presence alone is not
    evidence the scope is covered.
    """
    tracked = _tracked_workflow_files()
    assert tracked, (
        "the workflow enumeration is empty, so the guard would otherwise pass "
        "vacuously: git ls-files found no tracked file under .github/workflows"
    )
    resolutions = [(path, owners_for(path)) for path in tracked]
    wrong_owners = [
        (path, owners) for path, owners in resolutions if owners != GATE_OWNERS
    ]
    assert not wrong_owners, (
        f"tracked workflow files not owned by {GATE_OWNERS}: {wrong_owners}"
    )
    uncovered = [
        f"{path} ({why})" for path, why in COVERED_SCOPES if not owners_for(path)
    ]
    assert not uncovered, f"scopes claimed as covered but unowned: {uncovered}"


def test_ownerless_matching_line_shadows_an_earlier_owner():
    """Cell 6: an ownerless later match resolves to [] and shadows the owner.

    GitHub reads this shape as leaving the path with no owners, so changes
    there need no approval from the earlier line's owners. A matcher that
    skips the ownerless line returns the earlier owners and certifies a
    false green.
    """
    synthetic = (
        "# synthetic: the later line matches with no owner, so ownership is removed\n"
        "/.github/ @first\n"
        "/.github/workflows/pr-checks.yml\n"
    )
    assert owners_for(".github/workflows/pr-checks.yml", synthetic) == []


def test_ownerless_line_that_does_not_match_leaves_a_covered_path_owned():
    """Cell 7: an ownerless line that matches nothing changes nothing."""
    synthetic = (
        "/.github/ @first\n"
        "/.github/workflows/nightly.yml\n"
    )
    assert owners_for(".github/workflows/pr-checks.yml", synthetic) == ["@first"]


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
    assert owners == GATE_OWNERS, (
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


# ---------------------------------------------------------------------------
# Round-3 cells: the instruction-bearing scopes, the tracked-file property
# beneath them, and the control that keeps that property from passing vacuously
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope,why",
    INSTRUCTION_SCOPES,
    ids=[scope for scope, _ in INSTRUCTION_SCOPES],
)
def test_each_instruction_scope_resolves_to_instruction_owners(
    scope: str, why: str
) -> None:
    """Every instruction scope the file claims resolves to INSTRUCTION_OWNERS."""
    owners = owners_for(scope)
    assert owners == INSTRUCTION_OWNERS, (
        f"{scope} is claimed as instruction-bearing ({why}) but resolves to "
        f"{owners!r}"
    )


@pytest.mark.parametrize(
    "scope,why",
    INSTRUCTION_SCOPES,
    ids=[scope for scope, _ in INSTRUCTION_SCOPES],
)
def test_every_tracked_file_under_an_instruction_scope_is_owned(
    scope: str, why: str
) -> None:
    """Every tracked file beneath an instruction scope resolves to INSTRUCTION_OWNERS.

    Guard the property, not just the named directories: an ownerless line
    placed after a directory pattern inside one of these trees removes
    ownership for that one file, and a directory-level assertion would not see
    it. Also assert the enumeration is non-empty, so a mistyped scope cannot
    pass vacuously.
    """
    tracked = _tracked_files(scope)
    assert tracked, (
        f"the enumeration for {scope} is empty, so the guard would otherwise "
        f"pass vacuously: git ls-files found no tracked file under {scope}"
    )
    resolutions = [(path, owners_for(path)) for path in tracked]
    wrong_owners = [
        (path, owners) for path, owners in resolutions if owners != INSTRUCTION_OWNERS
    ]
    assert not wrong_owners, (
        f"tracked files under {scope} (instruction-bearing: {why}) not owned by "
        f"{INSTRUCTION_OWNERS}: {wrong_owners}"
    )


def test_a_code_path_outside_the_instruction_scopes_resolves_to_no_owner():
    """Control: a source path matches no instruction line and resolves to [].

    Without this, a pattern accidentally broad enough to match everything
    would turn the tracked-file property cell green while proving nothing.
    """
    assert owners_for("src/engram/server.py") == []


def test_the_two_owner_sets_are_separate_and_cover_disjoint_scopes():
    """The two owner sets are asserted separately, not one list used for both.

    ``is not`` catches the conflation that would defeat the point of two sets:
    aliasing one to the other (``INSTRUCTION_OWNERS = GATE_OWNERS``) keeps every
    cell green today, because the values match, while making divergence
    impossible. It does NOT catch a copy that starts out equal — only the
    per-set cells do, by holding each set to its own scopes.

    The disjointness assertion is the other half: a scope claimed by both sets
    would be resolved by whichever line came last, so one set's cell would be
    asserting the other's owners.
    """
    assert GATE_OWNERS is not INSTRUCTION_OWNERS, (
        "the two owner sets are the same list object, so an edit to one would "
        "silently move the other and they could never diverge"
    )
    gate_scopes = {path for path, _ in COVERED_SCOPES}
    instruction_scopes = {path for path, _ in INSTRUCTION_SCOPES}
    shared = gate_scopes & instruction_scopes
    assert not shared, f"scopes claimed by both owner sets: {sorted(shared)}"
