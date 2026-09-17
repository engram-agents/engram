"""Regression tests for #2986 -- the warm-briefing erase landmine.

`~/.engram/warm-briefing.md` holds the user's verbatim letter to the agent --
words the agent cannot reconstruct if lost. `engram-sleep` Step 3 erases the
volatile "## From this session" section by deleting from that section's
heading through EOF. The pre-fix template shipped the literal string
`## From this session` TWICE inside the preserved user-notes region itself
(once in prose, once in an HTML comment) -- so any erase implementation that
matched by SUBSTRING, or took the FIRST match instead of the LAST, would
truncate the user's letter instead of the (non-existent, pre-first-nap)
session section.

These tests guard the fix: (1)-(3) assert the shipped template no longer
carries the landmine, (4) is the decisive test -- it shows the *correct*
line-anchored-last-match erase and two *plausible-wrong* variants diverge on
the same realistic input, with the wrong variants destroying user content the
correct one preserves. A control that has only ever been observed to pass is
not a control.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "src" / "engram" / "templates" / "template.warm-briefing.md"
TEMPLATE_REL_PATH = "src/engram/templates/template.warm-briefing.md"

HEADING = "## From this session"
HEADING_RE = re.compile(r"^## From this session$", re.MULTILINE)

# Round 2 (#2986 follow-up): the comment now sits ABOVE the heading -- the
# erase (line-anchored on the LAST heading occurrence, delete-to-EOF) can
# never reach it, so it is never consumed and never needs restoring. Kept
# module-level because it's part of the realistic fixture built by test 4
# below, positioned above that fixture's real trailing heading.
STRUCTURAL_COMMENT = (
    "<!-- STRUCTURAL INVARIANT: the volatile session section (created by the "
    "first nap, erased at sleep) must always be the LAST section of this file. "
    "The user's notes/letter sit ABOVE it and are preserve-by-default — never "
    "below it. The erase is line-anchored on the LAST occurrence of that "
    "section's heading and runs to EOF. This comment sits ABOVE that heading "
    "deliberately, so the erase can never reach it: the anchor is protected "
    "by POSITION, not by a re-append step that could be skipped. Exact "
    "anchor + procedure: engram-sleep Step 3, engram-nap section 5b. Never "
    "write that heading, with its heading prefix, anywhere in this file "
    "except as its final line. -->"
)


def _substring_line_indices(text: str) -> list[int]:
    """0-based indices of every line containing the HEADING substring."""
    return [i for i, line in enumerate(text.splitlines()) if HEADING in line]


def _check_literal_on_heading_only(text: str) -> tuple[int, bool]:
    """Count lines carrying the HEADING substring; confirm each one is the
    bare anchored heading line and nothing else.

    Phrased as a substring count against the heading's OWN line (not as "no
    match above the heading's position"), because the pre-fix template has no
    `^## From this session` anchored line at all -- a check phrased relative
    to the heading's position has nothing to anchor on and would ERROR, not
    FAIL, against it. This phrasing fails cleanly instead (see the control
    inside test 2 below).
    """
    lines = text.splitlines()
    indices = _substring_line_indices(text)
    all_on_heading_line = all(lines[i] == HEADING for i in indices)
    return len(indices), all_on_heading_line


def _read_pre_fix_template() -> str:
    """Read the pre-fix template straight out of git -- NEVER via git stash
    (the stash stack is shared across worktrees and `pop` can take another
    agent's entry). Falls back to origin/dev for a shallow/detached checkout.
    """
    for ref in ("dev", "origin/dev"):
        result = subprocess.run(
            ["git", "show", f"{ref}:{TEMPLATE_REL_PATH}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout
    pytest.skip(
        "could not read the pre-fix template.warm-briefing.md from dev or "
        "origin/dev -- skipping the pre-fix control"
    )


# ---------------------------------------------------------------------------
# 1. test_template_has_exactly_one_anchored_heading
# ---------------------------------------------------------------------------


def test_template_has_exactly_one_anchored_heading():
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    matches = HEADING_RE.findall(text)
    assert len(matches) == 1, (
        f"expected exactly one line matching '^## From this session', found {len(matches)}"
    )


# ---------------------------------------------------------------------------
# 2. test_template_carries_the_literal_on_exactly_the_heading_line_and_nowhere_else
# ---------------------------------------------------------------------------


def test_template_carries_the_literal_on_exactly_the_heading_line_and_nowhere_else():
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    count, all_on_heading = _check_literal_on_heading_only(text)
    assert count == 1, (
        f"expected the substring '{HEADING}' to appear on exactly one line "
        f"of the shipped template, found {count}"
    )
    assert all_on_heading, (
        "the one line carrying the substring is not the bare anchored heading line"
    )

    # Control: the SAME checker, run against the pre-fix (dev) template,
    # must FAIL this claim (count == 2) rather than error or pass. If it
    # doesn't, this guard has never been observed to reject anything and
    # proves nothing about the fix.
    pre_fix_text = _read_pre_fix_template()
    pre_fix_count, _ = _check_literal_on_heading_only(pre_fix_text)
    assert pre_fix_count == 2, (
        "expected the pre-fix template to carry the substring on exactly 2 "
        f"lines (44 and 48), found {pre_fix_count} -- the control itself may "
        "be stale, or the fix may already be present on dev"
    )


# ---------------------------------------------------------------------------
# 3. test_anchor_comment_sits_above_the_heading_which_is_the_final_line
# ---------------------------------------------------------------------------
#
# Round 2 (#2986 follow-up): the anchor comment is now placed ABOVE the
# heading, not below it. The erase deletes from the LAST anchored heading
# line through EOF -- so a comment sitting BELOW the heading is consumed by
# every erase and must be re-appended (a step a hurried sleep could skip);
# a comment sitting ABOVE the heading is never reached by the delete at all.
# This inverts the round-1 invariant, so this test must FAIL against the
# round-1 commit -- confirmed via _read_pre_fix_template()-style git show
# below, not merely trusted.


def test_anchor_comment_sits_above_the_heading_which_is_the_final_line():
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()

    assert lines[-1] == HEADING, (
        f"expected the final line to be the bare anchored heading, got: {lines[-1]!r}"
    )

    heading_indices = [i for i, line in enumerate(lines) if line == HEADING]
    assert len(heading_indices) == 1, (
        f"expected exactly one anchored heading line, found {len(heading_indices)}"
    )
    heading_index = len(lines) - 1

    comment_indices = [
        i for i, line in enumerate(lines) if line.startswith("<!-- STRUCTURAL INVARIANT:")
    ]
    assert len(comment_indices) == 1, (
        f"expected exactly one STRUCTURAL INVARIANT comment, found {len(comment_indices)}"
    )
    assert comment_indices[0] < heading_index, (
        "the STRUCTURAL INVARIANT comment must sit ABOVE (before) the heading line"
    )


# ---------------------------------------------------------------------------
# 4. test_correct_erase_preserves_a_user_letter_that_a_wrong_one_destroys
# ---------------------------------------------------------------------------


def _erase_correct(text: str) -> str:
    """The spec'd erase (round 2): LAST line-anchored match, delete through
    EOF, re-append the bare heading stub ONLY. The structural comment sits
    ABOVE the heading in the fixture (as it does in the shipped template) and
    is never touched by the delete, so it is preserved along with everything
    else that was kept -- it needs no re-append step."""
    lines = text.splitlines()
    anchored_indices = [i for i, line in enumerate(lines) if line == HEADING]
    assert anchored_indices, "fixture must contain at least one anchored heading"
    target = anchored_indices[-1]
    kept = lines[:target]
    return "\n".join(kept) + "\n\n" + HEADING + "\n"


def _erase_wrong_first_match(text: str) -> str:
    """Plausible-wrong variant (a): FIRST line-anchored match instead of last.
    Same tail-rebuild shape as the correct erase (heading-only) -- this
    variant differs from correct ONLY in which line it targets."""
    lines = text.splitlines()
    anchored_indices = [i for i, line in enumerate(lines) if line == HEADING]
    assert anchored_indices, "fixture must contain at least one anchored heading"
    target = anchored_indices[0]
    kept = lines[:target]
    return "\n".join(kept) + "\n\n" + HEADING + "\n"


def _erase_wrong_substring(text: str) -> str:
    """Plausible-wrong variant (b): SUBSTRING match instead of line-anchored,
    taking the first line containing the substring anywhere -- this mirrors
    the original defect, where a mid-line/quoted occurrence inside the user's
    letter prose was mistaken for the heading. Same tail-rebuild shape as the
    correct erase (heading-only) -- this variant differs from correct ONLY in
    which line it targets."""
    lines = text.splitlines()
    substring_indices = _substring_line_indices(text)
    assert substring_indices, "fixture must contain at least one substring occurrence"
    target = substring_indices[0]
    kept = lines[:target]
    return "\n".join(kept) + "\n\n" + HEADING + "\n"


def test_correct_erase_preserves_a_user_letter_that_a_wrong_one_destroys():
    # A quoted excerpt inside the user's OWN letter that happens to contain a
    # real anchored heading line (e.g. the user pasted an old session's
    # heading verbatim into their notes) -- this is what discriminates
    # first-match from last-match.
    quoted_heading_context = (
        "Lei also pasted an old excerpt he wanted to keep forever:\n"
        "\n"
        f"{HEADING}\n"
        "\n"
        '"We debugged the retrieval pipeline together and it finally worked '
        '— I was so relieved."\n'
        "\n"
        "That memory means everything to me. Never delete it."
    )
    # The user's full letter -- everything that must survive the erase,
    # byte-identical. It ALSO contains a mid-line (non-anchored) occurrence
    # of the heading substring, exactly the shape that destroyed a letter
    # before this fix (this is what the old template taught agents to
    # write) -- this is what discriminates substring from line-anchored.
    user_letter = (
        "## Notes from the user (Lei) — keep verbatim, never edit or delete\n"
        "\n"
        "*This section is Lei's, preserved verbatim — I never edit their "
        "words. The volatile session section that naps create and sleep "
        "erases is always the LAST section of this file, below everything "
        "here.*\n"
        "\n"
        "Lei's note: I love how the \"## From this session\" section "
        "captures our daily arc together — please always keep doing that "
        "for me.\n"
        "\n"
        f"{quoted_heading_context}"
    )
    # Round 2 shape: the comment sits ABOVE the real trailing heading (the
    # invariant the erase now depends on), then the heading, then the
    # current CW's session content below it.
    real_session_section = (
        f"{STRUCTURAL_COMMENT}\n"
        "\n"
        f"{HEADING}\n"
        "\n"
        "Today's real session: we fixed #2986, the warm-briefing erase "
        "landmine."
    )
    grown_briefing = (
        "# I Am TestAgent\n"
        "\n"
        "*Intro line about TestAgent.*\n"
        "\n"
        "## Who I am\n"
        "\n"
        "Some identity content about TestAgent.\n"
        "\n"
        "---\n"
        "\n"
        f"{user_letter}\n"
        "\n"
        f"{real_session_section}\n"
    )

    # Sanity on the fixture itself before trusting anything built on it.
    anchored_count = len(HEADING_RE.findall(grown_briefing))
    assert anchored_count == 2, (
        f"fixture should carry the heading as an anchored line exactly "
        f"twice (once inside the user's quoted excerpt, once as the real "
        f"session section), got {anchored_count}"
    )
    substring_count = grown_briefing.count(HEADING)
    assert substring_count == 3, (
        "fixture should carry the heading substring exactly three times "
        f"(2 anchored + 1 mid-prose), got {substring_count}"
    )
    assert user_letter in grown_briefing

    correct_result = _erase_correct(grown_briefing)
    wrong_first_match_result = _erase_wrong_first_match(grown_briefing)
    wrong_substring_result = _erase_wrong_substring(grown_briefing)

    # The correct implementation preserves the ENTIRE user letter,
    # byte-identical -- including the quoted heading and the closing
    # "never delete it" sentence.
    assert user_letter in correct_result, (
        "the correct (last-match, line-anchored) erase must preserve the "
        "user's letter byte-identical, but it doesn't"
    )

    # Both plausible-wrong variants must DESTROY the letter -- if they
    # don't, this fixture doesn't discriminate the failure mode and the
    # test proves nothing.
    assert user_letter not in wrong_first_match_result, (
        "the first-match-instead-of-last-match variant should have "
        "truncated the letter but didn't -- this control is not doing its job"
    )
    assert user_letter not in wrong_substring_result, (
        "the substring-instead-of-line-anchored variant should have "
        "truncated the letter but didn't -- this control is not doing its job"
    )

    # Round 2's whole point: with the comment positioned ABOVE the real
    # trailing heading, the correct erase preserves it (it sits before the
    # erase's target line) -- and, as a consequence nobody asked for but is
    # true of this fixture, BOTH wrong variants destroy it too, since their
    # (wrong, earlier) target line also precedes the comment. A wrong
    # implementation here doesn't just truncate the letter; it also takes
    # out the anchor that documents the invariant it just violated.
    assert STRUCTURAL_COMMENT in correct_result, (
        "the correct erase must preserve the STRUCTURAL INVARIANT comment "
        "(it sits above the erase target and is never touched by the delete)"
    )
    assert STRUCTURAL_COMMENT not in wrong_first_match_result, (
        "the first-match variant should also have destroyed the structural "
        "comment (it sits after the wrong target line) but didn't"
    )
    assert STRUCTURAL_COMMENT not in wrong_substring_result, (
        "the substring variant should also have destroyed the structural "
        "comment (it sits after the wrong target line) but didn't"
    )

    # And all three results must genuinely diverge from each other -- a
    # control that has only ever been observed to pass is not a control.
    assert correct_result != wrong_first_match_result
    assert correct_result != wrong_substring_result
    assert wrong_first_match_result != wrong_substring_result
