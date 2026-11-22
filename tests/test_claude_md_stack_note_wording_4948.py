"""Contract for the Stacked PRs note wording change (engram#4948).

The note that closes the "Stacked PRs (`gh-stack`)" block of CLAUDE.md gains one
sentence: wording-only tightening in one copy need not be propagated, while a
change to what a seat must do is made in all three copies in the same pass. To
make room under the CLAUDE.md byte bound, the stale 2026-09-15 transitional
paragraph (its three pull requests have merged) moves to docs/internal/GH-STACK.md.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
DOC = (ROOT / "docs/internal/GH-STACK.md").read_text(encoding="utf-8")

SENTENCE = (
    "Wording-only tightening in one copy need not be propagated to the others; "
    "a change to what a seat must do is made in all three in the same pass."
)
HEADING = "## Transitional note retired from CLAUDE.md (2026-11-22)"
RULING = "Lei's 2026-09-15 01:01Z ruling"

# Opening lines of the other three "Duplicated by hand" notes, pinned by text
# (line numbers move). The first two lines of two notes are identical, so the
# third line is what tells them apart.
OTHER_NOTE_OPENINGS = [
    (
        "> **Duplicated by hand in engram, umwelt, and soma `CLAUDE.md`**, with no\n"
        "> generator or test binding the copies. Change one, change all three in the same\n"
        "> pass. This copy is the condensed form (#3323); the umwelt and soma copies still\n"
    ),
    (
        "> **Duplicated by hand in umwelt's `CLAUDE.md`**, with no generator or test binding\n"
        "> them — each file loads only with its own project. Change one, change the other in the\n"
        "> same pass; the failure mode is a rule current on the copy you are reading and stale\n"
    ),
    (
        "> **Duplicated by hand in engram, umwelt, and soma `CLAUDE.md`**, with no\n"
        "> generator or test binding the copies. Change one, change all three in the same\n"
        "> pass. The gates themselves are CI steps in engram's `pr-checks.yml` (umwelt has\n"
    ),
]

# Last lines of the same three notes, pinned as whole lines (reviewer round 1: the
# opening pins cannot see a deletion at the end of a note).
OTHER_NOTE_LAST_LINES = [
    "> these three copies document one mechanism, never three copies of it.",
    "> on the one governing the repo you are in.",
    "> the sibling copies point.",
]


def _stack_block():
    start = CLAUDE.index("### Stacked PRs (")
    end = CLAUDE.index("\n### ", start + 1)
    return CLAUDE[start:end]


def test_c1_sentence_occurs_once():
    flat = " ".join(CLAUDE.replace("\n> ", " ").split())
    assert flat.count(SENTENCE) == 1


def test_c2_sentence_sits_in_the_stack_note():
    flat = " ".join(
        (line[2:] if line.startswith("> ") else line) for line in _stack_block().splitlines()
    )
    flat = " ".join(flat.split())
    note = flat.index("**Duplicated by hand")
    anchor = flat.index("sync the rules, not the local facts.", note)
    assert flat.index(SENTENCE) > anchor
    assert flat.index(SENTENCE) > note
    # the sentence is in the same note: nothing but note text lies between
    assert "\n\n" not in _stack_block()[
        _stack_block().index("> **Duplicated by hand"):
    ]


def test_c3_stale_paragraph_moved_to_the_doc():
    for gone in ("soma#60", "umwelt#816", "THIS PR", RULING):
        assert gone not in CLAUDE, gone
    assert DOC.count(HEADING + "\n") == 1
    assert DOC.count(RULING) == 1
    assert DOC.count("umwelt#816") >= 1


def test_c4_moves_entry_for_the_stack_note():
    moves = json.loads((ROOT / "tools/claude_md_moves.json").read_text(encoding="utf-8"))["moves"]
    hits = [
        m
        for m in moves
        if m["to_doc"] == "docs/internal/GH-STACK.md" and "Stacked PRs note" in m["from_section"]
    ]
    assert hits


def test_c5_other_duplicated_by_hand_notes_untouched():
    for opening in OTHER_NOTE_OPENINGS:
        assert CLAUDE.count(opening) == 1, opening
    lines = CLAUDE.split("\n")
    for last in OTHER_NOTE_LAST_LINES:
        assert lines.count(last) == 1, last
