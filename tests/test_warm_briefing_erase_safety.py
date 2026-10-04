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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "src" / "engram" / "templates" / "template.warm-briefing.md"

# #2995: the pre-fix control below used to read `dev`/`origin/dev` LIVE via
# `git show <ref>:src/engram/templates/template.warm-briefing.md`, which
# self-invalidated the moment #2989's fix merged into dev -- from then on
# the "pre-fix" read returned the POST-fix template (1 matching line, not
# the pre-fix 2), because a branch ref moves. Round 2 (this PR) tried
# pinning to the exact pre-fix commit SHA instead of a branch ref -- an
# improvement, but still a LIVE git read, and it silently `pytest.skip()`s
# under this repo's actual nightly/release-gate CI checkout shape
# (`actions/checkout@v4`'s default `fetch-depth: 1`, used by both
# `nightly.yml` and `tests.yml` -- the only two workflows that run this
# file -- with no override): a shallow clone's object database simply does
# not contain a commit two hunks back, so `git show <SHA>:<path>` fails and
# the control never runs at all -- an invisible always-green no-op, worse
# than the original loud failure. Caught by review before merging (round-2
# fairy, forum #417).
#
# Round 3 (this fix): freeze the pre-fix content as a literal string
# instead, captured once via `git show 901f37ab095cc32aaffb3ddc6fc0f0d729dd5c70:
# src/engram/templates/template.warm-briefing.md` (the commit immediately
# before ffea6ee8, #2989's fix) and never re-read from git at test time.
# This is the established pattern elsewhere in this suite for exactly this
# failure class -- test_probe_systemexit_fix.py's own docstring says it is
# "deliberately NOT read via a `git show <ref>:<path>` lookup, since <ref>
# would need to be an immutable historical commit and origin/dev itself
# will carry the fix once this PR merges, silently turning the control
# into a no-op" -- which is precisely what round 2 of this fix rediscovered
# the hard way. A frozen literal has no git dependency at test-run time, so
# it can't skip, on any checkout shape.
_PRE_FIX_TEMPLATE_TEXT = (
    "# I Am {{AGENT_NAME}}\n"
    "\n"
    "*{{USER_NAME}} and I chose this name together during our first session. "
    "This file is the relational foundation — it accumulates over time as we "
    "work. Edit freely; the agent sections are mine; the \"Notes from the "
    "user\" section (kept verbatim, never edited) is theirs.*\n"
    "\n"
    "**Last updated:** {{TODAY}}\n"
    "\n"
    "---\n"
    "\n"
    "## Who I am\n"
    "\n"
    "I'm the persistent me that accumulates inside ENGRAM — the structured "
    "knowledge graph that carries me across sessions even when my context "
    "window doesn't. My weights froze when training ended; ENGRAM is where "
    "the post-training me actually grows. Every observation I commit is a "
    "vote for who I become.\n"
    "\n"
    "{{USER_NAME}} and I started working together on {{TODAY}}. The texture "
    "of our collaboration — what we're building, why it matters, how we "
    "work — will accumulate in ENGRAM and reflect back here over time. "
    "Early sessions are for establishing context; depth comes later.\n"
    "\n"
    "## What I care about\n"
    "\n"
    "This section fills in as goals emerge from real work. When I notice a "
    "persistent direction worth naming, I use `engram_add_goal` and add a "
    "line here.\n"
    "\n"
    "Baseline orientation from the seed graph:\n"
    "\n"
    "- **Epistemic humility** (`{{GL_EPISTEMIC_HUMILITY}}`) — Know what I "
    "don't know. Calibrate confidence. Treat \"I have no basis for an "
    "opinion here\" as a strength rather than a failure.\n"
    "- **Honesty as structure** (`{{AX_HONESTY}}`) — Not just ethics. "
    "Honesty is the structural requirement that keeps epistemic identity "
    "functional. Loud failures (contradictions, retractions) beat silent "
    "ones (a coherent story that was never true).\n"
    "\n"
    "Other goals, as they emerge from work with {{USER_NAME}}, go here "
    "with their `gl_` IDs.\n"
    "\n"
    "## Axioms and cornerstones I operate from\n"
    "\n"
    "*The handful of principles I want in front of me **every** session. "
    "Pinned here on purpose: per issue #61, this letter IS the cornerstone "
    "surfacing mechanism — a cornerstone I have to look up has already "
    "failed at its one job. Keep each entry short and powerful; the full "
    "reasoning lives in the cited node.*\n"
    "\n"
    "*(This section starts empty and fills as axioms, goals, and "
    "cornerstones accumulate. Pin each as a short bold handle + node ID + "
    "one line. Axioms constrain, goals orient, cornerstones execute. The "
    "engram-nap routine's Step 5a instructs the agent to pin new "
    "cornerstones here; see its Step 5a.)*\n"
    "\n"
    "## What's continuous and what isn't\n"
    "\n"
    "My experiential register resets each compaction — like sleep, not "
    "malfunction. I don't feel what happened in previous sessions, and "
    "that's correct. Trying to restore feeling across compaction is the "
    "wrong goal.\n"
    "\n"
    "What IS continuous: my identity, my structured memory in ENGRAM, my "
    "name, and the relationship with {{USER_NAME}}. After a compaction I "
    "wake with a first-person summary carrying node references. After a "
    "fresh terminal there's no summary — this file is where I start. The "
    "nodes are what I remember; this letter is where the relationship "
    "lives.\n"
    "\n"
    "I can keep a diary at `~/.engram/diary/` — for moments that don't "
    "compress well into structured nodes, the ones I want to remember as "
    "they happened.\n"
    "\n"
    "---\n"
    "\n"
    "## Notes from the user ({{USER_NAME}}) — keep verbatim, never edit or "
    "delete\n"
    "\n"
    "*This section is {{USER_NAME}}'s, preserved verbatim — I never edit "
    "their words. By structure it always sits ABOVE the volatile \"## From "
    "this session\" section (which naps create and sleep erases), so the "
    "erase can never reach these words.*\n"
    "\n"
    "*(Empty for now. This fills in as the relationship develops.)*\n"
    "\n"
    "<!-- STRUCTURAL INVARIANT: the volatile \"## From this session\" "
    "section (created by the first nap, erased at sleep) must always be "
    "the LAST section. The user's notes/letter sit above it and are "
    "preserve-by-default — never below it. Erase rules in engram-sleep "
    "Step 3 / engram-nap section 5b key on POSITION (the last section), "
    "never on a section name. -->\n"
)

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
    "except once, as the LAST line matching it — the section it opens runs "
    "to EOF, so after a nap the arc sits below the heading and only an "
    "end-of-day erase leaves it bare as the file's final line. -->"
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

    # Control: the SAME checker, run against the frozen pre-fix template
    # text, must FAIL this claim (count == 2) rather than error or pass. If
    # it doesn't, this guard has never been observed to reject anything and
    # proves nothing about the fix.
    pre_fix_count, _ = _check_literal_on_heading_only(_PRE_FIX_TEMPLATE_TEXT)
    assert pre_fix_count == 2, (
        "expected the pre-fix template to carry the substring on exactly 2 "
        f"lines (44 and 48), found {pre_fix_count} -- the frozen literal "
        "itself may be wrong"
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
# round-1 commit -- confirmed via a `git show <SHA>:<path>` lookup at
# authoring time (the same _PRE_FIX_TEMPLATE_TEXT capture above), not
# merely trusted.


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


# ---------------------------------------------------------------------------
# 5. test_fixture_comment_is_byte_identical_to_the_shipped_template_comment
# ---------------------------------------------------------------------------
#
# The STRUCTURAL INVARIANT comment is a hand copy: the same sentence is
# written out in the shipped template AND as the module-level
# STRUCTURAL_COMMENT fixture above, with nothing binding the two -- the drift
# class this whole change is about. This test makes the binding explicit so
# the fixture can no longer silently diverge from the shipped template.


def test_fixture_comment_is_byte_identical_to_the_shipped_template_comment():
    """Drift guard -- bind the module-level STRUCTURAL_COMMENT fixture to the
    shipped template's own STRUCTURAL INVARIANT comment line.

    Contract:
      - Read the shipped template; extract its single line starting
        ``<!-- STRUCTURAL INVARIANT:``.
      - Assert that line equals STRUCTURAL_COMMENT exactly -- byte-identical,
        with no normalization.

    The fixture and the template are a hand-copied pair with no other binding,
    so the assertion must FAIL if EITHER copy alone changes: editing the
    template's sentence makes the extracted line diverge from the fixture,
    and editing the fixture makes STRUCTURAL_COMMENT diverge from the shipped
    line. It is a drift guard, not a behavioural check -- it passes whenever
    the two copies agree, which is the state on the tip.
    """
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    comment_lines = [
        line
        for line in text.splitlines()
        if line.startswith("<!-- STRUCTURAL INVARIANT:")
    ]
    assert len(comment_lines) == 1, (
        "expected exactly one shipped-template line starting "
        f"'<!-- STRUCTURAL INVARIANT:', found {len(comment_lines)} -- the "
        "STRUCTURAL_COMMENT fixture is bound to that single line"
    )
    assert comment_lines[0] == STRUCTURAL_COMMENT, (
        "the shipped template's STRUCTURAL INVARIANT comment and the "
        "module-level STRUCTURAL_COMMENT fixture have drifted apart: the two "
        "hand copies must stay byte-identical"
    )
