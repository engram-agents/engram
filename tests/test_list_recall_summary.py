"""Tests for engram_list recall_summary integration (Wave D rollout).

Covers, by AREA rather than one entry per test — deliberately, because a
per-test roster here went stale the moment tests were added to the file and
nobody updated the list. Areas absorb a new test without going false; a
numbered inventory of individual tests does not. Per-test detail lives in each
test's own docstring, and the section banners below are the actual inventory.

  1. Default triage `claim` sourcing: the key carries recall_summary when one
     is populated (not the original long claim), and falls back to the
     truncated claim for legacy nodes that have none — including the short
     claim that needs no truncation at all.
  2. The display-truncation boundary itself: a summary exactly at
     LIST_CLAIM_SUMMARY_DISPLAY_CAP comes back untruncated, one char past it
     comes back truncated with an ellipsis, and a far-oversize value truncates
     the same way. All three seed the column by direct SQL — fixture
     convenience, uniform across the three, and not a path difference between
     them: none of these lengths needs a bypass to be written, since the
     ordinary engram_add_observation path accepts every one of them.
  3. The two display caps' VALUES, in two separate tests: a literal pin on
     the constants themselves, and a real binding that READS the three
     agent-facing prose surfaces hand-mirroring them (server.py's engram_list
     docstring, its DESIGN INTENT block, engram_client.py's tool description)
     and asserts each states the current constant. Every boundary test above
     derives its edges from the constants, so nothing else in this file would
     notice a value change, and none of them reads the prose at all.
  4. fields_json projection of recall_summary / recall_keywords (smoke tests
     for the column-whitelist additions).
  5. filters_json predicates over recall_summary / recall_keywords (contains,
     is_not_null) evaluating end-to-end.

Fixture pattern: same fresh_server() + _add_observation() shape as
test_engram_filter.py (TestEngramListStructured).
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_list_recall_summary_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Reset state and return the server module rebound to the test dir."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_observation(srv, claim: str, url: str = "https://example.com/test") -> str:
    r = json.loads(srv.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test source",
        "quoted_text": claim,
        "interpretation": f"Interpretation of: {claim}",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "error" not in r, r
    return r["observation_id"]


def _set_recall_fields(srv, node_id: str, summary: str, keywords: list[str]) -> None:
    """Directly write recall_summary + recall_keywords to the DB (backfill pattern)."""
    conn = srv._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET recall_summary = ?, recall_keywords = ? WHERE id = ?",
            (summary, json.dumps(keywords), node_id),
        )
        conn.commit()
    finally:
        conn.close()


def _caps() -> tuple[int, int]:
    """(summary display cap, claim-fallback display cap) from the live module.

    Read off engram_query rather than hardcoded here: hardcoding the numbers in
    the tests is the same drift the named constants exist to remove. Imported
    lazily — fresh_server() drops every `engram_*` module from sys.modules, so
    this must run after it to read the module instance under test.
    """
    import engram_query
    return (
        engram_query.LIST_CLAIM_SUMMARY_DISPLAY_CAP,
        engram_query.LIST_CLAIM_FALLBACK_DISPLAY_CAP,
    )


# ---------------------------------------------------------------------------
# Test 0: the caps' VALUES are part of the agent-facing contract
# ---------------------------------------------------------------------------

# Three agent-facing surfaces state the two cap numbers as plain prose an agent
# reads and plans against. The two patterns below are what BINDS that prose to
# the constants. They are deliberately phrase-shaped rather than a bare `\d+`
# scan: a matcher loose enough to accept any "200" occurring anywhere in
# server.py could not fail, and a test that cannot fail is worse than no test,
# because it reads as coverage.
#
# Consequence to know before editing any of the three surfaces: REWORDING one
# reddens the binding test even when its number is still right. That is
# intended. The fix is to update the pattern here in the same change — this is
# the single place the expected wording is written down.
_PROSE_SUMMARY_CAP_RE = re.compile(r"truncated to (\d+) chars with an ellipsis if longer")
_PROSE_FALLBACK_CAP_RE = re.compile(r"the claim truncated to (\d+) chars")

# Matches the banner line opening ANY tool's DESIGN INTENT block, capturing the
# tool name. Anchors by banner, never by line number: this test exists to stop
# hand-mirrored values going stale, so it must not itself carry a hardcoded
# location that goes stale the next time server.py grows a line.
#
# ONE pattern, deliberately, because it has TWO uses that must agree: finding
# this block's own opening line, and recognising the NEXT block's banner as a
# terminator in _comment_block_at. Written as two regexes they could drift —
# a banner form recognised as an anchor but not as a terminator would silently
# re-open the run-on this pattern exists to stop. Derived rather than
# duplicated, so that state is unrepresentable rather than merely discouraged.
#
# Measured against server.py at the time of writing: 48 DESIGN INTENT banners,
# all 48 matched by this pattern, all 48 immediately followed by a `# ----`
# rule. Those blocks are banner-delimited, which is what makes a sibling block
# separable from an interior blank line at all.
_DESIGN_INTENT_BANNER_RE = re.compile(
    r"^#\s*DESIGN INTENT\s*[-\u2013\u2014]+\s*(\S+)\s*\Z"
)

# The one block this file binds against. Compared whole, never by prefix, so
# `engram_list_focused` deliberately does not match.
_DESIGN_INTENT_ANCHOR_TOOL = "engram_list"


def _is_design_intent_anchor(line: str) -> bool:
    """True for the DESIGN INTENT banner of _DESIGN_INTENT_ANCHOR_TOOL only."""
    m = _DESIGN_INTENT_BANNER_RE.match(line)
    return m is not None and m.group(1) == _DESIGN_INTENT_ANCHOR_TOOL


def _collapse_ws(text: str) -> str:
    """Flatten a prose surface to one line so line-wrapping cannot hide a number.

    Measured, because it is easy to assume this is cosmetic: it is load-bearing
    for exactly one of the three surfaces today. server.py's docstring wraps
    BOTH matched phrases across a newline ("truncated to 200 chars\n    with an
    ellipsis if longer"), so neither pattern matches its raw text at all. The
    DESIGN INTENT block matches raw once its comment lines are joined, and
    engram_client.py's description is a single line where collapsing is a
    no-op. Collapsing all three anyway keeps the patterns independent of wrap
    position, so a pure reflow of any surface cannot redden the test.
    """
    return re.sub(r"\s+", " ", text).strip()


def _comment_block_at(lines: list[str], start: int) -> list[str]:
    """The comment block beginning at `lines[start]`, one collapsed entry per line.

    Pure: takes already-read lines, touches no file and no server. Extracted
    from _design_intent_block precisely so the scanning rule below can be
    tested against synthetic input, since that rule is what the binding test's
    coverage actually rests on.

    The rule, and why it is not the obvious one: scanning stops at the first
    line that is NEITHER blank NOR a column-0 comment. An earlier version
    stopped at the first line that did not start with "#", which made a
    genuinely EMPTY line inside a comment block a terminator. That was a
    silent-pass hole — every "#"-prefixed line after such a blank went unread,
    so a wrong restatement placed there would never be checked. In server.py
    the DESIGN INTENT block's paragraph breaks are bare "#" lines today, so
    the hole was latent rather than live; replacing one bare "#" with a truly
    empty line is an ordinary edit and would have opened it. A guard that a
    routine reflow can switch off is not a guard.

    "Blank" means `not ln.strip()` — no non-whitespace at all. So a line of
    CODE with leading whitespace is NOT blank and correctly terminates the
    block, as does an indented comment (the column-0 "#" test is deliberately
    unchanged from the original, so nothing but the blank-line handling moved).

    Blank lines contribute no text, which makes a trailing run of them
    structurally harmless: it cannot extend the returned prose.

    That leaves one case a blank-line rule alone cannot decide — a LATER
    comment block separated from this one by blank lines only, which is
    byte-identical to an interior paragraph break. It is separated here by
    STRUCTURE rather than by whitespace: scanning also stops at a
    _DESIGN_INTENT_BANNER_RE line, because these blocks are banner-delimited
    (48 of 48 in server.py, each banner followed by a `# ----` rule). So a
    sibling DESIGN INTENT block announces itself and is excluded, while an
    interior blank is still read past. Both, with no trade-off between them.

    Note what is NOT claimed: a blank-separated PLAIN comment block — one with
    no banner — is still absorbed. That residual is real, and pinned by
    test_comment_block_at_absorbs_a_blank_separated_plain_comment_block so it
    is known rather than discovered. It is tolerable only because the
    banner-led case is the one that actually occurs in server.py.

    Why over-reading is not simply the safe direction here, since the reverse
    is the usual rule: the binding assertion downstream is set-equality over
    the cap numbers found, not containment. Absorbing a neighbour that
    mentions any other number reddens the test loudly AND wrongly, and the
    obvious repair for a maintainer facing a spurious failure is to narrow the
    scan — which puts the silent hole straight back. "Fails loudly" and "fails
    usefully" come apart when a false positive's natural fix is to undo the
    guard.
    """
    block: list[str] = []
    for offset, ln in enumerate(lines[start:]):
        if not ln.strip():
            continue
        if not ln.startswith("#"):
            break
        # offset 0 is this block's OWN banner, which must not terminate it.
        if offset and _DESIGN_INTENT_BANNER_RE.match(ln):
            break
        block.append(ln.lstrip("#").strip())
    return block


def _design_intent_block_lines(srv) -> list[str]:
    """The `# DESIGN INTENT — engram_list` comment block, one entry per line.

    Read out of `srv.__file__` rather than a path built from this test's own
    location: the plugin build restructures source paths, so the module object
    under test is the only reliable pointer to the file that actually defines
    the constant we are comparing against.

    The file read and the anchor assertion live here; the block-scanning rule
    itself is _comment_block_at, which is pure and separately tested.

    This is the ONE place the anchor is located. Both consumers go through it —
    _design_intent_block below, which collapses the list to prose for the
    prose-binding test, and the end-reaching test, which needs the list itself.
    A consumer that re-found the anchor for itself would be a second copy of
    the one anchor rule, i.e. exactly the correlated-failure shape the
    end-reaching test exists to keep out of its own comparison.
    """
    with open(srv.__file__, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    anchors = [i for i, ln in enumerate(lines) if _is_design_intent_anchor(ln)]
    assert len(anchors) == 1, (
        f"Expected exactly one DESIGN INTENT banner for "
        f"{_DESIGN_INTENT_ANCHOR_TOOL} in {srv.__file__}; found {len(anchors)}. "
        f"If the block was renamed, duplicated, or removed, update "
        f"_DESIGN_INTENT_BANNER_RE / _DESIGN_INTENT_ANCHOR_TOOL."
    )
    return _comment_block_at(lines, anchors[0])


def _design_intent_block(srv) -> str:
    """The same block as collapsed prose, for the cap-pattern surfaces.

    Collapsing is what the PROSE-matching consumer needs (see _collapse_ws:
    the cap patterns must not depend on wrap position). It is not what a
    boundary check needs, and the end-reaching test deliberately does not use
    it — see _comment_tail_above_list_fields' docstring for why.
    """
    return _collapse_ws(" ".join(_design_intent_block_lines(srv)))


# The code line that opens immediately below the DESIGN INTENT block in
# server.py. Anchored at column 0 so the two INDENTED uses of the same name
# inside engram_list's body cannot be mistaken for the assignment, and located
# by name rather than by line number for the same reason the banner is: this
# file exists partly to stop hardcoded locations going stale, so it must not
# carry one itself.
_LIST_FIELDS_ASSIGN_RE = re.compile(r"^_LIST_FIELDS\s*=")


def _comment_tail_above_list_fields(lines: list[str]) -> list[str]:
    """The contiguous column-0 comment run directly above `_LIST_FIELDS = ...`.

    Computed BACKWARD from a CODE anchor, and the direction is the point. A
    forward walk from the banner would be _comment_block_at's own rule written
    a second time: it would agree with the scan under test whenever that scan
    is wrong for a shared reason, so it would verify nothing. This walks the
    other way, from the other end, off an anchor the scan never looks at — so
    the two can agree about where the block ENDS only by both being right.

    Its own rule is deliberately smaller than the scan's: stop at the first
    line that is not a column-0 comment, blank lines included, with no
    blank-line handling and no banner terminator. A truly empty line inside the
    block (there are none today) would therefore SHORTEN this tail rather than
    corrupt it, and a shorter tail is still a real suffix to assert against.

    Entries are normalised exactly as _comment_block_at normalises its own, so
    the comparison downstream is between block BOUNDARIES and nothing else.
    Sharing the normalisation is deliberate; sharing the boundary rule is what
    would have destroyed the independence.

    Which parts are actually shared, stated precisely because an earlier
    version of this claimed independence wholesale and that is only half true:

      - `lstrip("#").strip()` is DUPLICATED, as two separate literals, one
        here and one in _comment_block_at. Duplication is the right shape for
        it: a change to either literal makes the two sides normalise
        differently, so the drift DIVERGES and the comparison reddens.
      - `_collapse_ws` used to be genuinely SHARED by both sides of the
        comparison, and that was a correlated-failure channel, not an
        independent one: a `_collapse_ws -> ""` mutation collapsed both sides
        to the same empty string and left the end-reaching test green, caught
        only by test_display_caps_bound_to_agent_facing_prose — a different
        test, on a different surface. The comparison is now between LINE
        LISTS, so `_collapse_ws` is no longer on the end-reaching test's path
        at all and the channel is closed rather than merely documented.
    """
    anchors = [i for i, ln in enumerate(lines) if _LIST_FIELDS_ASSIGN_RE.match(ln)]
    assert len(anchors) == 1, (
        f"Expected exactly one column-0 `_LIST_FIELDS =` assignment; found "
        f"{len(anchors)}. If the constant was renamed or moved away from the "
        f"DESIGN INTENT block, update _LIST_FIELDS_ASSIGN_RE — and pick the new "
        f"anchor from the CODE below the block, never from the banner above it."
    )
    tail: list[str] = []
    i = anchors[0] - 1
    while i >= 0 and lines[i].startswith("#"):
        tail.append(lines[i].lstrip("#").strip())
        i -= 1
    tail.reverse()
    return tail


def _scan_reaches_tail(block_lines: list[str], tail_lines: list[str]) -> bool:
    """Does the scan's output END with `tail_lines`, compared as LINE LISTS?

    Pure, and extracted for exactly the reason _comment_block_at was extracted
    from the block reader one layer down: the comparison's BASIS is the thing
    that failed last time, and a rule that only ever runs against the single
    live server.py is a rule exercised on one input shape. Pulled out here it
    can be driven with synthetic input by
    test_scan_reaches_tail_compares_full_line_lists, which is what stops
    the basis being silently swapped back.

    The basis is line lists, NOT a collapsed string, and that is the whole
    content of this function. The predecessor compared
    `_collapse_ws(" ".join(block_lines)).endswith(_collapse_ws(" ".join(
    tail_lines)))` while its caller's vacuity guard measured `len(tail_lines)`
    — the PRE-collapse list. The two read different objects, and a bare "#"
    line normalises to "": it counted toward the guard and contributed nothing
    to the string, so a tail of three bare-"#" spacers collapsed to "" and
    `endswith("")` was trivially true. Comparing the lists makes both sides
    the same kind of object, so nothing can count on one side and vanish on
    the other.

    What this does NOT check, stated because the name could be read as
    promising it: this is a SUFFIX check, so it says nothing about the block's
    FRONT. See the caller's docstring for the measured bound.
    """
    return block_lines[-len(tail_lines):] == tail_lines


def _agent_facing_prose_surfaces(srv) -> dict:
    """The three surfaces that hand-mirror the caps, keyed by a human label.

    Two are read as LIVE objects rather than file text — the docstring off the
    function, the description off the schema dict — so they bind what actually
    ships to an agent, not merely what the file says. The DESIGN INTENT block
    has no runtime representation and is the one that must be read as source.

    engram_client is imported lazily: fresh_server() drops every `engram_*`
    module from sys.modules, so this must run after it.
    """
    import engram_client
    return {
        "server.py DESIGN INTENT block": _design_intent_block(srv),
        "server.py engram_list docstring": _collapse_ws(srv.engram_list.__doc__ or ""),
        "engram_client.py TOOL_SCHEMAS['engram_list']['description']": _collapse_ws(
            engram_client.TOOL_SCHEMAS["engram_list"]["description"]
        ),
    }


# ---------------------------------------------------------------------------
# _comment_block_at: the scanning rule the prose-binding test rests on
#
# Driven with synthetic lines only — no server, no file I/O. That is the whole
# reason the rule was extracted: the binding test below can only be as good as
# this scan, and a scan exercised solely through the real server.py is
# exercised on exactly one input shape.
# ---------------------------------------------------------------------------


def test_comment_block_at_reads_past_an_interior_blank_line():
    """A blank line inside a comment block must not end the block.

    This is the regression control for the silent-pass hole. The previous
    rule ("stop at the first line that does not start with #") treated a
    genuinely empty line as a terminator, so the DISTINCTIVE-TOKEN line below
    was never read — and a wrong cap restatement parked there would have sailed
    through test_display_caps_bound_to_agent_facing_prose with a green tick.
    Checked to fail: this assertion reddens under the old rule and passes
    under the current one.
    """
    lines = [
        "# DESIGN INTENT — engram_list",
        "# ---------------------------",
        "# first paragraph of the block",
        "",
        "# truncated to 12345 chars with an ellipsis if longer",
        "_LIST_FIELDS = frozenset({",
    ]
    text = " ".join(_comment_block_at(lines, 0))
    assert "12345" in text, (
        "Prose after an interior blank line was not read; the block scan is "
        "terminating on blank lines again, which re-opens the silent-pass hole."
    )


def test_comment_block_at_reads_past_a_multi_line_blank_run():
    """A run of SEVERAL blank lines inside the block is not a terminator either.

    Separate from the single-blank control above because it is the test that
    discriminates against the plausible-wrong rule, not merely against the old
    one: "consume a single blank, but treat two as a section break" (the PEP 8
    two-blank-lines intuition) passes every other test in this group. It also
    re-opens the hole for anyone who reflows a paragraph break into two empty
    lines instead of one, which is no less ordinary an edit than one.
    """
    lines = [
        "# DESIGN INTENT — engram_list",
        "# first paragraph of the block",
        "",
        "",
        "# truncated to 12345 chars with an ellipsis if longer",
        "_LIST_FIELDS = frozenset({",
    ]
    assert "12345" in " ".join(_comment_block_at(lines, 0)), (
        "A multi-line blank run terminated the block. Blank RUNS are not "
        "section breaks here: any blank-count threshold re-opens the hole for "
        "a reflow that happens to leave two empty lines rather than one."
    )


def test_comment_block_at_stops_at_indented_code():
    """An indented line of code terminates the block.

    Guards the definition of "blank": leading whitespace does not make a line
    blank, so `    return x` ends the block rather than being skipped over.
    """
    lines = [
        "# banner",
        "# body",
        "    return SOMETHING_AFTER_THE_BLOCK",
        "# a later comment that must stay out",
    ]
    assert _comment_block_at(lines, 0) == ["banner", "body"]


def test_comment_block_at_stops_at_code_even_across_blank_lines():
    """Blank lines around an intervening code line do not rescue a later block.

    The blanks are skipped, but the code line still terminates, so the second
    comment block stays out of the result.
    """
    lines = [
        "# banner",
        "# body",
        "",
        "SOME_CODE = 1",
        "",
        "# UNRELATED_LATER_BLOCK",
    ]
    text = " ".join(_comment_block_at(lines, 0))
    assert "UNRELATED_LATER_BLOCK" not in text
    assert text == "banner body"


def test_comment_block_at_trailing_blank_run_adds_nothing():
    """A run of blank lines at the end of the block contributes no text.

    Stated as a test rather than left to inspection because it is what makes
    the trailing-run case need no special-casing: blank lines yield no entries,
    so they cannot extend the returned prose whether they are interior,
    trailing, or the last thing in the file.
    """
    assert _comment_block_at(["# banner", "# body", "", "", ""], 0) == ["banner", "body"]


def test_comment_block_at_stops_at_a_blank_separated_sibling_banner():
    """A sibling DESIGN INTENT block is excluded even across blank lines only.

    This is the case a blank-line rule alone cannot decide: "blank inside a
    block" and "blank between two blocks" are byte-identical. The separation
    is structural rather than whitespace-based — these blocks are
    banner-delimited (48 of 48 in server.py), so the sibling announces itself
    and _DESIGN_INTENT_BANNER_RE terminates the scan on it.

    Checked to fail: an implementation without the banner terminator returns
    the sibling's prose here, and this assertion reddens against it.

    Why exclusion rather than the usual prefer-to-over-read direction: the
    binding assertion downstream is set-equality over the cap numbers found,
    so an absorbed neighbour mentioning any other number fails loudly AND
    wrongly — and the natural repair for a spurious failure is to narrow the
    scan, restoring the silent hole. See _comment_block_at's docstring.
    """
    lines = [
        "# DESIGN INTENT — engram_list",
        "# body of the block under test",
        "",
        "# DESIGN INTENT — engram_query",
        "# SIBLING_BLOCK_PROSE truncated to 999 chars",
        "SOME_CODE = 1",
    ]
    got = _comment_block_at(lines, 0)
    assert "SIBLING_BLOCK_PROSE" not in " ".join(got), (
        "A sibling DESIGN INTENT block was absorbed across a blank line; the "
        "banner terminator is missing or no longer matches the banner form."
    )
    assert got == ["DESIGN INTENT — engram_list", "body of the block under test"]


def test_comment_block_at_absorbs_a_blank_separated_plain_comment_block():
    """KNOWN RESIDUAL, pinned so it is known rather than discovered.

    The banner terminator separates SIBLING DESIGN INTENT blocks. It does
    nothing for a blank-separated PLAIN comment block, which is still absorbed
    — there is no structure left to distinguish it from an interior paragraph
    break, and no rule reading only the text could.

    Tolerable because it does not occur: in server.py the DESIGN INTENT block
    is followed immediately by `_LIST_FIELDS = frozenset({`, with no blank
    line and no intervening comment. If an unbannered comment block is ever
    inserted between the two, this test is where the consequence is already
    written down, and it will need a rule that this one cannot supply.
    """
    lines = [
        "# DESIGN INTENT — engram_list",
        "# body of the block under test",
        "",
        "# a plain unbannered comment",
        "SOME_CODE = 1",
    ]
    assert _comment_block_at(lines, 0) == [
        "DESIGN INTENT — engram_list",
        "body of the block under test",
        "a plain unbannered comment",
    ]


def test_design_intent_anchor_matches_the_tool_whole_not_by_prefix():
    """`engram_list_focused` must not be mistaken for `engram_list`.

    The anchor is derived from _DESIGN_INTENT_BANNER_RE plus a whole-name
    comparison rather than being its own regex, so the anchor form and the
    terminator form cannot drift apart. This pins the prefix property that
    derivation is responsible for.
    """
    assert _is_design_intent_anchor("# DESIGN INTENT — engram_list")
    assert not _is_design_intent_anchor("# DESIGN INTENT — engram_list_focused")
    assert not _is_design_intent_anchor("# DESIGN INTENT — engram_query")
    # Every banner the terminator recognises is a well-formed banner; the
    # anchor is the subset naming exactly this tool.
    for line in ("# DESIGN INTENT — engram_list",
                 "# DESIGN INTENT — engram_list_focused",
                 "# DESIGN INTENT — engram_query"):
        assert _DESIGN_INTENT_BANNER_RE.match(line), line


def test_design_intent_block_scan_reaches_the_end_of_the_real_block():
    """The real server.py block is read to its last line, not just its first.

    The binding test only proves SOMETHING was read, and would stay green if
    the scan stopped after the banner. This asserts the scan's output ENDS
    WITH the block's actual last lines, so a scan that truncates anywhere
    inside the block reddens here.

    Where the expected tail comes from, which is the load-bearing part: it is
    computed BACKWARD from the `_LIST_FIELDS` code anchor below the block
    (_comment_tail_above_list_fields), never forward from the banner. A
    forward computation would restate the rule under test and pass whenever
    that rule failed for a shared reason. Backward-from-code is independent in
    both direction and anchor.

    Coverage split, stated accurately because an earlier version of this
    docstring called this "a length-free version of the control above" and
    that teaches a reader something false: the SYNTHETIC controls carry the
    blank-line coverage entirely. Measured on server.py — the real block
    contains ZERO truly empty lines, all four of its paragraph breaks being
    bare "#" — so the blank-line bug cannot manifest in the live file at all.
    This test is a different check, on the single input shape that actually
    ships.

    What it replaced, and why: this assertion used to pin one literal sentence
    from the block's last line, with a repair note inviting a maintainer to
    "update this expectation" if the block was reworded. Nothing enforced that
    the pinned sentence was the block's LAST line, so that documented repair
    could re-point the expectation at a MID-block sentence — green test, no
    end-reaching coverage left, and a name still reading reaches_the_end. A
    false negative introduced by the repair, with no test left to notice it.
    A guard a routine reflow can switch off is not a guard.

    Why the comparison is between LINE LISTS and not between strings — the
    same lesson landing a second time, one layer in. The assertion used to be
    `collapsed_block.endswith(_collapse_ws(" ".join(tail_lines)))`, guarded by
    the `len(tail_lines) > 2` check below. Those two read DIFFERENT objects:
    the guard measured the PRE-collapse list while the assertion consumed the
    POST-collapse string, and a bare "#" line normalises to "" — so it counted
    toward the guard and contributed nothing to the string. Measured against
    the real module: a blank line plus three bare-"#" spacer lines above
    `_LIST_FIELDS` gives tail_lines == ["", "", ""], len 3, guard satisfied,
    collapsed tail "" — and `endswith("")` is trivially true, so the test
    stayed GREEN under the very truncating scan it exists to catch. Comparing
    the LISTS removes the pre/post-collapse mismatch at its root: both sides
    are the same kind of object, so nothing can count on one side and vanish
    on the other. It also takes `_collapse_ws` off this test's path entirely,
    which closes a correlated-failure channel rather than documenting it (see
    _comment_tail_above_list_fields' docstring).

    The `len(tail_lines) > 2` guard below STAYS, and is load-bearing for a
    reason the comparison does not supply. The case it alone catches, measured
    by neutering the guard in a copy of this file and re-running: shrink the
    block to its own header (banner + `# ----` rule) sitting directly above
    `_LIST_FIELDS`, and run a scan that stops after the rule. BOTH sides then
    read `[banner, rule]`, the comparison finds them equal, and the test is
    vacuously green — guard removed, it PASSES; guard present, it reddens.
    That is the shape the guard's own message names, and the comparison cannot
    see it, because agreeing about a two-line block is exactly what a scan
    truncating at the header does.

    What the guard does NOT do, stated because the plausible version of this
    is wrong: it is not protecting against a degenerate `[-0:]` slice. With an
    empty tail, `block_lines[-0:]` is the WHOLE list (27 entries against the
    live file), which compares UNEQUAL to `[]` — so that case reddens with or
    without the guard, and the guard only improves the message. The fix here
    is the comparison's BASIS, not the guard's existence.

    Two alternatives were rejected on measurement, not taste: making the guard
    `len(tail_lines) > 2 and bool(tail)`, or counting non-empty entries. Both
    go False on that same blank-plus-spacers file — so both would REFUSE, i.e.
    redden, on a source file whose scan is perfectly correct, converting a
    formatting edit into a false positive. The list comparison stays green
    there and reddens only when the scan actually truncates.

    KNOWN LIMIT, deliberately not closed: this reaches the END, and its
    coverage of the block's FRONT is incidental rather than designed. Today a
    front-truncating scan does redden here, but only because the comment run
    above `_LIST_FIELDS` happens to span the WHOLE block, so a dropped opening
    line shows up as a length mismatch. Measured: insert ONE truly empty line
    mid-block — an ordinary edit, and precisely the edit _comment_block_at is
    built to read past — and tail_lines falls from 27 entries to 14. A scan
    that reaches the end correctly while dropping the block's first 3 lines is
    then invisible: it reddens here on the live file and PASSES after that one
    blank line. Left as a stated bound rather than closed, because widening
    this to check both ends would make it two tests wearing one name; the
    front is _comment_block_at's own synthetic tests' territory.
    """
    srv = fresh_server()
    with open(srv.__file__, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    tail_lines = _comment_tail_above_list_fields(lines)
    assert len(tail_lines) > 2, (
        f"The comment run directly above `_LIST_FIELDS` is now {len(tail_lines)} "
        f"line(s) — no longer than the block's own header (banner + `# ----` "
        f"rule), which is where the truncating scans this test exists to catch "
        f"stop. The assertion below would be vacuous, so it is refused instead. "
        f"Either a non-comment line now sits inside the run, or an unbannered "
        f"comment block was inserted between the block and the code — the "
        f"residual pinned by "
        f"test_comment_block_at_absorbs_a_blank_separated_plain_comment_block, "
        f"whose docstring already says this is the point where a rule it cannot "
        f"supply is needed."
    )
    block_lines = _design_intent_block_lines(srv)
    assert _scan_reaches_tail(block_lines, tail_lines), (
        "The DESIGN INTENT block scan is no longer reaching the block's final "
        "lines. Expected the scan's last lines to be the comment run sitting "
        "directly above `_LIST_FIELDS` in server.py:\n"
        f"  expected (last {len(tail_lines)} of the comment run): {tail_lines!r}\n"
        f"  actual   (last {len(tail_lines)} of the scan):        "
        f"{block_lines[-len(tail_lines):]!r}\n"
        f"  scan returned {len(block_lines)} line(s) in total.\n"
        "Nothing here needs updating when the block is reworded — both sides "
        "are read from the same file — so this is a scan defect: check "
        "_comment_block_at for an early terminator."
    )


def test_scan_reaches_tail_compares_full_line_lists():
    """The end-reaching comparison must compare full LINE LISTS: LIST-based,
    not a collapsed string, and spanning the WHOLE of the tail, not a
    fixed-size slice of it.

    This pins the comparison's BASIS, which is the one thing the test above
    cannot pin about itself. Reverting `_scan_reaches_tail` to the collapsed-
    string form it replaced leaves every other test in this file green — that
    was measured, not supposed: the whole file passed on the reverted basis.
    So without this test the fix above can be undone silently, by exactly the
    documented-repair route that produced the previous false negative.

    Third instance of one class on this test, which is why it gets a test and
    not another paragraph:

        the prose pin could not verify its own premise
          -> its replacement's guard measured the PRE-collapse list while the
             assertion consumed the POST-collapse string
            -> nothing pinned the basis, so the fix reverts silently

    Each layer's repair reintroduced the class it had just closed, and each
    time the surviving remedy was a docstring. A docstring is preventive and
    walk-past-able; a test is not.

    The shape is the real file's own vacuity case, reproduced synthetically: a
    blank line plus three bare-"#" spacers between the block and
    `_LIST_FIELDS`. Both sides are computed by the real helpers rather than
    written out by hand, so this also pins the step that makes the bug
    possible — a bare "#" normalises to "", counting as a line while
    contributing no text.

    Why it discriminates, which is the property to preserve if it is ever
    edited: under the LIST basis the truncated scan is rejected, because its
    last three entries are prose rather than three empty strings. Under the
    COLLAPSED-STRING basis the expected tail collapses to "" and
    `endswith("")` holds for anything at all, so the truncated scan is
    ACCEPTED and this test reddens. Green here under a string basis is
    unreachable; that is what makes it a pin rather than a restatement.

    TWO properties are pinned here, not one: the comparison's TYPE (line
    lists rather than a collapsed string — the paragraph above) and its SPAN
    (the whole of `tail_lines` rather than a fixed-size slice of it — the
    `full[-2:]` assertion below). The type half alone was measured
    insufficient. It discriminates entirely on the tail's FINAL entry, so any
    basis that inspects one trailing element already satisfies it.

    What the span half catches, measured rather than reasoned so the list can
    be read without re-running it — every one of these is line-based, and
    every one left this whole file GREEN before the `full[-2:]` assertion
    existed: `block_lines[-1:] == tail_lines[-1:]`, the same with `[-2:]`,
    `set(...) == set(...)` over the suffix, and a drop-empties variant of it.
    The probe is a scan held to TWO lines against a three-line tail — one
    that reaches the block's end while dropping its front — and no comparison
    narrowed to a fixed-size slice can reject it. `[-2:]` is the costly one:
    nothing else in this file sees it, and it silently deletes the incidental
    front coverage the caller's KNOWN LIMIT records as holding today.

    What the span half does NOT separate, stated because "span" could be read
    as promising it: `sorted(...) == sorted(...)` over the same suffix
    survives the whole file. Sorting preserves the suffix's WIDTH, and this
    fixture's tail is three IDENTICAL empty entries, so no ordering is left to
    observe. The assertion pins the comparison's width; ordering stays
    unpinned, and reaching it would need a fixture whose tail entries differ.

    What it does not reach: a maintainer who bypasses `_scan_reaches_tail`
    entirely, by inlining a different comparison at the call site, is not
    caught here. That is a real gap, and it is the reason the comparison was
    given a name at all — naming it converts a silent one-line edit into one
    that must also strand or delete a helper carrying its own test.
    """
    lines = [
        "# DESIGN INTENT — engram_list",
        "# ---------------------------",
        "# real body line",
        "",
        "#",
        "#",
        "#",
        "_LIST_FIELDS = frozenset({",
    ]

    tail_lines = _comment_tail_above_list_fields(lines)
    assert tail_lines == ["", "", ""], (
        f"Fixture drift: the comment run above `_LIST_FIELDS` should be the "
        f"three bare-'#' spacers, each normalising to '', but is {tail_lines!r}."
    )
    # The vacuity PRECONDITION, pinned so the fixture cannot quietly stop
    # exercising it: a tail that SATISFIES the caller's `> 2` guard while
    # carrying no text at all. If this stops holding, the test below is still
    # green but no longer about anything.
    assert len(tail_lines) > 2 and not any(tail_lines), tail_lines

    full = _comment_block_at(lines, 0)
    assert full[-3:] == ["", "", ""], f"Fixture drift: scan returned {full!r}."

    # What a scan that stopped at the first bare "#" would have returned —
    # the truncation this whole test exists to catch.
    truncated = full[:3]

    assert not _scan_reaches_tail(truncated, tail_lines), (
        "A scan that stopped at the first bare '#' was ACCEPTED as reaching "
        "the block's end. That is the collapsed-string basis returning: the "
        "expected tail collapses to '' and `endswith('')` is trivially true, "
        "so the guard above it cannot fire. Compare LINE LISTS.\n"
        f"  tail_lines:     {tail_lines!r}\n"
        f"  truncated scan: {truncated!r}"
    )
    # SPAN, not only TYPE: a comparison that decides on the tail's last one
    # or two entries is still LINE-based, so every assertion above it stays
    # green. This is the one that separates it from a full-width compare.
    assert not _scan_reaches_tail(full[-2:], tail_lines), (
        "A scan holding FEWER lines than the tail it must end with was "
        "ACCEPTED as reaching the block's end. The comparison has stopped "
        "spanning the WHOLE of tail_lines and now decides on a fixed-size "
        "slice of it — a basis like `block_lines[-1:] == tail_lines[-1:]` "
        "is still line-based, so the truncation assertion above stays green "
        "and only this one can see the change.\n"
        "What that admits: a scan that reaches the block's END correctly "
        "while dropping lines off its FRONT is now indistinguishable from a "
        "complete one. That is the incidental front coverage the caller's "
        "KNOWN LIMIT records as holding today, deleted silently.\n"
        f"  tail_lines ({len(tail_lines)} lines): {tail_lines!r}\n"
        f"  scan       ({len(full[-2:])} lines): {full[-2:]!r}\n"
        "Compare across the full width of tail_lines, not a slice of it."
    )
    # Control: the CORRECT scan is still accepted, so the assertion above is
    # rejecting truncation rather than rejecting everything.
    assert _scan_reaches_tail(full, tail_lines), (
        f"The correct scan was rejected; the comparison is now too strict. "
        f"tail_lines={tail_lines!r} full={full!r}"
    )


def test_display_caps_match_documented_values():
    """Pin the two display caps to the values the docs are written around.

    Scope, stated precisely because an earlier version of this docstring
    claimed more than the test did: this is a VALUE pin and nothing else. It
    reddens when a constant moves — the "did anyone mean to change this
    number?" tripwire — and it reads no prose at all, so it cannot see
    doc/code drift. Making the prose direction fail is
    test_display_caps_bound_to_agent_facing_prose's job, immediately below.
    """
    fresh_server()
    summary_cap, fallback_cap = _caps()
    assert summary_cap == 200, (
        "LIST_CLAIM_SUMMARY_DISPLAY_CAP changed; update server.py's engram_list "
        "docstring, its DESIGN INTENT comment block, and engram_client.py's "
        "engram_list description in the same change."
    )
    assert fallback_cap == 130, (
        "LIST_CLAIM_FALLBACK_DISPLAY_CAP changed; update server.py's engram_list "
        "docstring, its DESIGN INTENT comment block, and engram_client.py's "
        "engram_list description in the same change."
    )


def test_display_caps_bound_to_agent_facing_prose():
    """Bind both constants to the three prose surfaces that hand-mirror them.

    server.py's DESIGN INTENT block, server.py's engram_list docstring, and
    engram_client.py's engram_list tool description each state both numbers as
    plain prose — the half an agent actually reads and plans against. This test
    is what makes those three agree with the code.

    Both directions redden, which is the whole point: change a constant and
    all three surfaces disagree with it; change one surface's number and that
    surface disagrees. A surface reworded past the patterns above also reddens,
    with the "no longer states" message — intended, see those patterns' comment.
    """
    srv = fresh_server()
    summary_cap, fallback_cap = _caps()

    checks = (
        ("LIST_CLAIM_SUMMARY_DISPLAY_CAP", _PROSE_SUMMARY_CAP_RE, summary_cap),
        ("LIST_CLAIM_FALLBACK_DISPLAY_CAP", _PROSE_FALLBACK_CAP_RE, fallback_cap),
    )
    for label, prose in _agent_facing_prose_surfaces(srv).items():
        for const_name, pattern, expected in checks:
            found = pattern.findall(prose)
            assert found, (
                f"{label} no longer states {const_name} in a form this test can "
                f"read.\n  Pattern: {pattern.pattern!r}\n"
                f"  Surface: {prose!r}\n"
                f"If the prose was reworded deliberately, update the pattern in "
                f"tests/test_list_recall_summary.py in the same change."
            )
            assert all(int(n) == expected for n in found), (
                f"{label} states {const_name} as {found}, but the constant is "
                f"{expected}. One side was changed without the other — update "
                f"engram_query.py's constant and ALL THREE surfaces listed in "
                f"_agent_facing_prose_surfaces() together."
            )


# ---------------------------------------------------------------------------
# Test 1: recall_summary present → triage `claim` field equals the summary
# ---------------------------------------------------------------------------


def test_triage_claim_sources_from_recall_summary_when_populated():
    """When a node has recall_summary, the default triage `claim` key equals
    the recall_summary text, not the (possibly long) original claim."""
    srv = fresh_server()
    _, fallback_cap = _caps()

    long_original_claim = (
        "A very long original claim that exceeds one hundred and thirty characters "
        "and would normally be truncated by the legacy fallback path in engram_list "
        "triage output so we can distinguish the two code paths clearly here"
    )
    assert len(long_original_claim) > fallback_cap, (
        f"Pre-condition: original claim must exceed {fallback_cap} chars"
    )

    ob_id = _add_observation(srv, long_original_claim)

    summary = "Curated dense summary for triage recognition purposes."
    _set_recall_fields(srv, ob_id, summary, ["curated", "dense", "summary"])

    result = json.loads(srv.engram_list(payload_json=json.dumps({"node_type": "observation_factual"})))
    assert result["status"] == "ok"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not found in engram_list output"

    assert match["claim"] == summary, (
        f"Expected triage `claim` to equal recall_summary.\n"
        f"  Got:      {match['claim']!r}\n"
        f"  Expected: {summary!r}"
    )
    # Confirm the original long claim text is NOT present in the claim field
    assert long_original_claim not in match["claim"], (
        "Original long claim leaked into triage output when recall_summary was present"
    )


# ---------------------------------------------------------------------------
# Test 2: recall_summary absent → falls back to truncated claim (regression)
# ---------------------------------------------------------------------------


def test_triage_claim_falls_back_to_truncated_claim_when_no_summary():
    """Legacy nodes without recall_summary: triage `claim` field is the
    original claim truncated to LIST_CLAIM_FALLBACK_DISPLAY_CAP chars with
    '...' suffix, unchanged."""
    srv = fresh_server()
    _, fallback_cap = _caps()

    long_claim = (
        "A long original claim that is definitely more than one hundred and thirty "
        "characters so that the truncation path fires and we can check the suffix "
        "appears correctly in the triage output for legacy nodes without a summary"
    )
    assert len(long_claim) > fallback_cap, (
        f"Pre-condition: claim must exceed {fallback_cap} chars"
    )

    ob_id = _add_observation(srv, long_claim)
    # No recall_summary written — this is the legacy/unfilled node path.

    result = json.loads(srv.engram_list(payload_json=json.dumps({"node_type": "observation_factual"})))
    assert result["status"] == "ok"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not found in engram_list output"

    expected = long_claim[:fallback_cap] + "..."
    assert match["claim"] == expected, (
        f"Expected legacy truncation fallback in triage `claim`.\n"
        f"  Got:      {match['claim']!r}\n"
        f"  Expected: {expected!r}"
    )


def test_triage_claim_short_claim_no_truncation_when_no_summary():
    """Short claim (at or below the fallback cap) without recall_summary:
    returned verbatim, no truncation suffix."""
    srv = fresh_server()
    _, fallback_cap = _caps()

    short_claim = "A short claim that fits within the fallback display cap."
    assert len(short_claim) <= fallback_cap, (
        f"Pre-condition: claim must fit within {fallback_cap} chars"
    )

    ob_id = _add_observation(srv, short_claim)

    result = json.loads(srv.engram_list(payload_json=json.dumps({"node_type": "observation_factual"})))
    assert result["status"] == "ok"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not found in engram_list output"
    assert match["claim"] == short_claim, (
        f"Short claim should be verbatim (no truncation): {match['claim']!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: fields_json=["id","recall_summary"] projection works
# ---------------------------------------------------------------------------


def test_fields_json_recall_summary_projection():
    """fields_json=['id','recall_summary'] returns rows with exactly those
    columns (whitelist addition smoke test)."""
    srv = fresh_server()
    ob_id = _add_observation(srv, "projection test claim for recall_summary field")
    summary = "Projection test summary."
    _set_recall_fields(srv, ob_id, summary, ["proj", "test", "field"])

    result = json.loads(srv.engram_list(payload_json=json.dumps({
        "filters_json": json.dumps({"field": "type", "op": "eq", "value": "observation_factual"}),
        "fields_json": json.dumps(["id", "recall_summary"]),
    })))
    assert result["status"] == "ok", f"Unexpected error: {result.get('error')}"
    assert result["nodes"], "Expected at least one node in projection result"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not in projection result"

    # Only id + recall_summary should be present (id is always prepended)
    assert set(match.keys()) == {"id", "recall_summary"}, (
        f"Unexpected keys in projection result: {set(match.keys())}"
    )
    assert match["recall_summary"] == summary, (
        f"recall_summary mismatch: {match['recall_summary']!r} vs {summary!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: fields_json=["id","recall_keywords"] projection works
# ---------------------------------------------------------------------------


def test_fields_json_recall_keywords_projection():
    """fields_json=['id','recall_keywords'] returns rows with exactly those
    columns (whitelist addition smoke test for recall_keywords)."""
    srv = fresh_server()
    ob_id = _add_observation(srv, "projection test claim for recall_keywords field")
    keywords = ["kw-alpha", "kw-beta", "kw-gamma"]
    _set_recall_fields(srv, ob_id, "Some summary.", keywords)

    result = json.loads(srv.engram_list(payload_json=json.dumps({
        "filters_json": json.dumps({"field": "type", "op": "eq", "value": "observation_factual"}),
        "fields_json": json.dumps(["id", "recall_keywords"]),
    })))
    assert result["status"] == "ok", f"Unexpected error: {result.get('error')}"
    assert result["nodes"], "Expected at least one node in projection result"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not in projection result"

    assert set(match.keys()) == {"id", "recall_keywords"}, (
        f"Unexpected keys in projection result: {set(match.keys())}"
    )
    # recall_keywords stored as JSON string in DB; custom projection returns raw value
    kw_raw = match["recall_keywords"]
    assert kw_raw is not None, "recall_keywords should not be None after _set_recall_fields"
    assert json.loads(kw_raw) == keywords, (
        f"recall_keywords content mismatch: {kw_raw!r} vs {keywords!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: filters_json using recall_summary as filter field works
# ---------------------------------------------------------------------------


def test_filters_json_recall_summary_contains():
    """filters_json with recall_summary contains filter returns matching nodes
    and rejects unknown-field error (smoke test that recall_summary is in
    ALLOWED_NODE_COLUMNS and filter evaluation works end-to-end)."""
    srv = fresh_server()

    ob_id = _add_observation(srv, "filter test claim for recall_summary predicate")
    summary = "Unique sentinel phrase for filter test xyzzy42."
    _set_recall_fields(srv, ob_id, summary, ["filter", "test", "sentinel"])

    # Also add a node without the sentinel phrase to confirm selectivity
    _add_observation(srv, "unrelated claim without the sentinel phrase",
                     url="https://example.com/other")

    result = json.loads(srv.engram_list(payload_json=json.dumps({
        "filters_json": json.dumps({
            "field": "recall_summary",
            "op": "contains",
            "value": "xyzzy42",
        }),
    })))
    assert result["status"] == "ok", (
        f"Unexpected error from recall_summary filter: {result.get('error')}"
    )
    assert result["total_matched"] >= 1, "Expected at least one match for recall_summary filter"
    returned_ids = {n["id"] for n in result["nodes"]}
    assert ob_id in returned_ids, (
        f"Node {ob_id} with matching recall_summary not in filter result"
    )


# ---------------------------------------------------------------------------
# Test 6: filters_json using recall_keywords as filter field works
# ---------------------------------------------------------------------------


def test_filters_json_recall_keywords_is_not_null():
    """filters_json with recall_keywords is_not_null filter returns only nodes
    that have recall_keywords populated (smoke test for filter evaluation)."""
    srv = fresh_server()

    ob_with_kw = _add_observation(srv, "node with keywords populated",
                                   url="https://example.com/with-kw")
    ob_without_kw = _add_observation(srv, "node without keywords unfilled",
                                      url="https://example.com/without-kw")
    _set_recall_fields(srv, ob_with_kw, "Summary for kw node.", ["alpha", "beta", "gamma"])
    # ob_without_kw intentionally left unfilled.

    result = json.loads(srv.engram_list(payload_json=json.dumps({
        "filters_json": json.dumps({
            "field": "recall_keywords",
            "op": "is_not_null",
        }),
    })))
    assert result["status"] == "ok", (
        f"Unexpected error from recall_keywords filter: {result.get('error')}"
    )
    returned_ids = {n["id"] for n in result["nodes"]}
    assert ob_with_kw in returned_ids, (
        f"Node {ob_with_kw} with recall_keywords should appear in is_not_null filter result"
    )
    assert ob_without_kw not in returned_ids, (
        f"Node {ob_without_kw} without recall_keywords should NOT appear in is_not_null filter"
    )


# ---------------------------------------------------------------------------
# Test 7: oversize recall_summary (well past the display cap)
#         → truncated to the cap + "..." in triage output
# ---------------------------------------------------------------------------


def test_triage_oversize_recall_summary_truncated_with_ellipsis():
    """A node whose recall_summary exceeds LIST_CLAIM_SUMMARY_DISPLAY_CAP
    should appear in triage with claim truncated to the cap + '...', matching
    the legacy claim-truncation convention.

    The summary is seeded by direct SQL for fixture convenience, NOT because
    the length requires a bypass — worth saying, because "written via direct
    SQL" reads as though a bypass were needed and re-seeds exactly the
    unreachability misconception the display-cap comment in engram_query.py
    exists to remove. This value is reachable through the ordinary
    engram_add_observation path, which soft-accepts it (flagging
    needs_tightening) and only hard-rejects at RECALL_SUMMARY_HARD_REJECT_AT
    = 280 chars. The direct write is simply the shorter fixture, and is the
    same path _set_recall_fields uses for every other node in this file."""
    srv = fresh_server()
    summary_cap, _ = _caps()

    ob_id = _add_observation(srv, "claim for oversize recall_summary test")

    # Build a summary well past the display cap (not at the edge — the
    # boundary itself is pinned by test 8 below).
    oversize_len = summary_cap + 50
    oversize_summary = "X" * oversize_len
    assert len(oversize_summary) == oversize_len, "Pre-condition: summary length"

    # Seed the field directly — fixture convenience, not a required bypass;
    # see this test's docstring. (The batch validator's own hard cap happens to
    # equal this display cap, which is a numeric coincidence rather than a
    # dependency — see LIST_CLAIM_SUMMARY_DISPLAY_CAP.)
    conn = srv._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET recall_summary = ? WHERE id = ?",
            (oversize_summary, ob_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = json.loads(srv.engram_list(payload_json=json.dumps({"node_type": "observation_factual"})))
    assert result["status"] == "ok"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not found in engram_list output"

    expected_claim = "X" * summary_cap + "..."
    assert match["claim"] == expected_claim, (
        f"Expected oversize recall_summary truncated to {summary_cap} chars + '...'.\n"
        f"  Got (len {len(match['claim'])}): {match['claim']!r}\n"
        f"  Expected (len {len(expected_claim)}): {expected_claim!r}"
    )
    assert len(match["claim"]) == summary_cap + 3, (
        f"Expected length {summary_cap + 3}, got {len(match['claim'])}"
    )


# ---------------------------------------------------------------------------
# Test 8: exactly-at-cap vs one-past-cap — the display-truncation boundary
#         itself. Round-1/round-2 review of this behavior turned entirely on
#         an off-by-one between "<= cap" (use as-is) and "> cap" (truncate),
#         and nothing previously pinned that boundary directly (test 7 only
#         exercises a far-oversize case, nowhere near the edge).
# ---------------------------------------------------------------------------


def test_triage_recall_summary_exactly_at_cap_untruncated():
    """A recall_summary of exactly LIST_CLAIM_SUMMARY_DISPLAY_CAP chars is the
    top of the "use as-is" branch (`len(rs) <= cap`) — must come back
    unchanged, with no ellipsis."""
    srv = fresh_server()
    summary_cap, _ = _caps()

    ob_id = _add_observation(srv, "claim for exactly-at-cap boundary test")

    summary_at_cap = "Y" * summary_cap
    assert len(summary_at_cap) == summary_cap, "Pre-condition: summary length == cap"
    _set_recall_fields(srv, ob_id, summary_at_cap, ["kw"])

    result = json.loads(srv.engram_list(payload_json=json.dumps({"node_type": "observation_factual"})))
    assert result["status"] == "ok"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not found in engram_list output"

    assert match["claim"] == summary_at_cap, (
        f"Expected exactly-{summary_cap}-char recall_summary returned UNTRUNCATED "
        f"(no ellipsis).\n"
        f"  Got (len {len(match['claim'])}): {match['claim']!r}\n"
        f"  Expected (len {summary_cap}): {summary_at_cap!r}"
    )
    assert len(match["claim"]) == summary_cap, (
        f"Expected length {summary_cap} (no ellipsis appended), got {len(match['claim'])}"
    )


def test_triage_recall_summary_one_past_cap_truncated_with_ellipsis():
    """One char past LIST_CLAIM_SUMMARY_DISPLAY_CAP must be truncated on
    display. This value is ordinarily reachable (the write path's own
    hard-reject threshold, RECALL_SUMMARY_HARD_REJECT_AT, is 280 — not a
    hypothetical direct-SQL-only case); round 1 of this PR's review caught the
    PR author's mistaken claim that this branch was unreachable."""
    srv = fresh_server()
    summary_cap, _ = _caps()

    ob_id = _add_observation(srv, "claim for one-past-cap boundary test")

    summary_over = "Z" * (summary_cap + 1)
    assert len(summary_over) == summary_cap + 1, "Pre-condition: summary length == cap + 1"
    _set_recall_fields(srv, ob_id, summary_over, ["kw"])

    result = json.loads(srv.engram_list(payload_json=json.dumps({"node_type": "observation_factual"})))
    assert result["status"] == "ok"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not found in engram_list output"

    expected_claim = "Z" * summary_cap + "..."
    assert match["claim"] == expected_claim, (
        f"Expected {summary_cap + 1}-char recall_summary truncated to "
        f"{summary_cap} chars + '...'.\n"
        f"  Got (len {len(match['claim'])}): {match['claim']!r}\n"
        f"  Expected (len {len(expected_claim)}): {expected_claim!r}"
    )
    assert len(match["claim"]) == summary_cap + 3, (
        f"Expected length {summary_cap + 3}, got {len(match['claim'])}"
    )
