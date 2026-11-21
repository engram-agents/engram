"""The hashed ENGRAM block in CLAUDE.md: the pure text functions (engram-alpha#4444, stage 1).

Contract-first: these cells were written BEFORE `src/engram/engram_claude_block.py` exists, one cell
(or one small parametrized table) per contract line, named with the contract id (A2..A6, B1..B11,
D2/D2b, E7, F1, F2). Each docstring says WHAT OBSERVABLE WOULD DIFFER if the claim were false.

The module is reached only through `_blk()`, at CELL time. While the module is missing every cell
that needs it fails with a plain AssertionError ("engram_claude_block is missing: stage 2"); there is
no importorskip and no module-level import, so a cell is red, never skipped and never an error at
collection.

Hash cells compute the expected SHA-256 with `hashlib` in the test itself, never by calling
`body_sha256` (a function wrong in the same way as its test would pass silently).

API assumed (decided by the spec author, not invented here):
  BEGIN_PREFIX, BEGIN_SUFFIX, END_LINE, NOTICE_LINE, body_sha256(body), wrap(render),
  merge(existing, render, *, overwrite_edited_block=False) -> MergeOutcome(text, kind, changed_lines, line),
  MergeRefused(Exception), unwrap(text), remove_block(text).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"
TIERS = REPO_ROOT / "src" / "build" / "packaging" / "tiers.json"

# Literals: the contract pins them as literals (F1), so the test spells them out.
BEGIN_LIT = "<!-- engram:begin sha256="
SUFFIX_LIT = " -->"
END_LIT = "<!-- engram:end -->"
SHA0 = "0" * 64

R_OLD = "# Rules\n\nFirst rule.\n\n## Section\n\n- item one\n- item two\n"
# R_NEW is R_OLD plus exactly four appended lines ("", "## Added", "", "Three more lines here."):
# a pure insertion, so "N lines changed" is 4 under any sensible way of counting.
R_NEW = R_OLD + "\n## Added\n\nThree more lines here.\n"
ADDED_LINES = 4

# The B9(b) first line says it prints lines of the user's own file. The contract gives the idea,
# not the words, so this one regex is the single place a wording choice is encoded.
OWN_TEXT_RE = re.compile(r"(your|user'?s?) own", re.I)
FLAG = "--overwrite-edited-block"


def _blk():
    """Import the stage-2 module AT CELL TIME; a missing module is a red cell, never a skip."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    try:
        import engram_claude_block as mod
    except Exception as e:  # ImportError, or anything the half-built module raises at import
        raise AssertionError("engram_claude_block is missing: stage 2") from e
    return mod


def _begin(h: str = SHA0) -> str:
    return BEGIN_LIT + h + SUFFIX_LIT


def _sha(body: str) -> str:
    """The contract's hash, computed here: CRLF read as LF, then SHA-256 of the UTF-8 bytes."""
    return hashlib.sha256(body.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _block_parts(wrapped: str):
    """(begin_line, body, end_line) of a text that is exactly one LF block, read without the module."""
    lines = wrapped.split("\n")
    assert lines[-1] == "", "a wrapped render ends with one final newline"
    lines = lines[:-1]
    return lines[0], "\n".join(lines[1:-1]) + "\n", lines[-1]


def _assert_hash_verifies(wrapped: str) -> None:
    begin, body, end = _block_parts(wrapped)
    m = re.fullmatch(re.escape(BEGIN_LIT) + r"([0-9a-f]{64})" + re.escape(SUFFIX_LIT), begin)
    assert m, f"first line is not a BEGIN line with a 64-hex hash: {begin!r}"
    assert m.group(1) == _sha(body), "the hash in the BEGIN line is not the SHA-256 of the block body"
    assert end == END_LIT


def _refusal(existing, render=R_NEW, **kw) -> str:
    """The message of the MergeRefused that merge() must raise; any other outcome is a red cell."""
    blk = _blk()  # outside the try: a missing module must not be swallowed as a "refusal"
    try:
        out = blk.merge(existing, render, **kw)
    except blk.MergeRefused as e:
        return str(e)
    raise AssertionError(f"merge() did not refuse (kind={out.kind!r}); a refusal was required")


# --------------------------------------------------------------------------- constants (F1, A4)

def test_F1_the_marker_constants_are_pinned_as_literals():
    """F1: BEGIN prefix, BEGIN suffix and END line are exactly these literals. If a constant drifted
    (a different spelling, a trailing space) every file already written would stop matching."""
    blk = _blk()
    assert blk.BEGIN_PREFIX == BEGIN_LIT
    assert blk.BEGIN_SUFFIX == SUFFIX_LIT
    assert blk.END_LINE == END_LIT


def test_F1_A2_the_marker_text_is_defined_in_exactly_one_python_file():
    """Definitions / A2: the marker strings are defined ONCE. Scanning the shipped Python for the
    BEGIN literal finds exactly engram_claude_block.py; a second copy (say in bootstrap.py) would
    let the writer and the comparators drift apart."""
    hits = sorted(
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if "engram:begin sha256=" in p.read_text(encoding="utf-8", errors="replace")
    )
    assert hits == ["engram_claude_block.py"], hits


def test_A4_the_notice_is_one_fixed_line_that_tells_the_user_where_to_write():
    """A4: the notice line is a single line and says to put your own text outside the markers.
    A multi-line or missing notice would leave a user with no hint that edits inside are lost."""
    blk = _blk()
    assert "\n" not in blk.NOTICE_LINE and blk.NOTICE_LINE.strip()
    assert "put your own text outside" in blk.NOTICE_LINE


def test_A4_wrap_layout_is_begin_notice_body_end_and_one_final_newline():
    """A4 + B1 shape: wrap(render) is BEGIN line, the ONE notice line, the render body, END line, one
    final newline, and the hash is SHA-256 of notice + body. A wrap that put the notice outside the
    hashed body, or added a trailing blank line, would differ here."""
    blk = _blk()
    w = blk.wrap(R_OLD)
    expected_hash = _sha(blk.NOTICE_LINE + "\n" + R_OLD)
    assert w == f"{_begin(expected_hash)}\n{blk.NOTICE_LINE}\n{R_OLD}{END_LIT}\n"
    assert not w.endswith("\n\n")


def test_A2_wrap_then_unwrap_gives_back_the_render():
    """A2: ONE function writes the block and the comparators strip back to the same render. If wrap
    and unwrap were written separately, a render could survive wrap but not come back out."""
    blk = _blk()
    for render in (R_OLD, R_NEW, "one line\n"):
        assert blk.unwrap(blk.wrap(render)) == render


# ---------------------------------------------------------------------------- hashing

def test_body_sha256_is_the_stdlib_sha256_of_the_body_with_crlf_read_as_lf():
    """Definitions: the hash is SHA-256 of the body, CR+LF read as LF. A body_sha256 that hashed raw
    CRLF bytes would call every editor-converted file edited (M18)."""
    blk = _blk()
    for body in ("a\nb\n", "a\r\nb\r\n", "café 中\n", ""):
        assert blk.body_sha256(body) == hashlib.sha256(body.replace("\r\n", "\n").encode("utf-8")).hexdigest()
    h = blk.body_sha256("x\n")
    assert re.fullmatch(r"[0-9a-f]{64}", h)


def test_body_sha256_reads_only_crlf_pairs_not_a_lone_carriage_return():
    """Definitions: 'every carriage return + line feed read as a line feed'. A lone CR is content;
    a function that dropped every CR would hash `a\\rb` as `ab`."""
    blk = _blk()
    assert blk.body_sha256("a\rb\n") == hashlib.sha256(b"a\rb\n").hexdigest()
    assert blk.body_sha256("a\rb\n") != blk.body_sha256("ab\n")


# ------------------------------------------------------------------------------ A3

@pytest.mark.parametrize("render", [
    pytest.param("# T\n\nsee below\n" + END_LIT + "\nmore\n", id="end_line_in_prose"),
    pytest.param("# T\n\n```\n" + _begin("abc") + "\n```\n", id="begin_inside_a_code_fence"),
    pytest.param("# T\n\nwrite `" + END_LIT + "` to end it\n", id="end_marker_mid_sentence"),
    pytest.param("# T\n\nthe prefix " + BEGIN_LIT + " starts it\n", id="begin_prefix_mid_sentence"),
])
def test_A3_render_raises_when_the_body_contains_the_marker_text_anywhere(render):
    """A3: a body that holds the marker text (even in a code fence or mid-sentence) fails LOUDLY at
    render time. If wrap() returned a block for it, the next merge would refuse the user's own file."""
    blk = _blk()
    with pytest.raises(Exception) as ei:
        blk.wrap(render)
    assert not isinstance(ei.value, AssertionError), "wrap() must raise a real error, not fail an assert"
    assert str(ei.value).strip(), "the failure must say something"


def test_A3_control_a_body_that_only_mentions_the_word_engram_is_wrapped():
    """A3 control: the check is for the marker text, not for the word 'engram'; if it refused every
    render that mentions engram, no template could ship."""
    blk = _blk()
    ok = "# T\n\nengram block and engram:begin and <!-- another comment -->\n"
    assert blk.wrap(ok).startswith(BEGIN_LIT)


# ------------------------------------------------------------------------------ B1

def test_B1_absent_file_gets_the_pure_wrapped_render():
    """B1: no file -> the result is the wrapped render, kind 'created', no report line. A merge that
    prepended a blank line, or reported 'appended', would differ."""
    blk = _blk()
    out = blk.merge(None, R_OLD)
    assert out.text == blk.wrap(R_OLD)
    assert out.kind == "created"
    assert out.line == ""
    _assert_hash_verifies(out.text)


# ------------------------------------------------------------------------------ B2 / B2b

B2_CASES = {
    "empty": b"",
    "ends_in_newline": b"my own rules\n",
    "no_final_newline": b"my own rules",
    "crlf": b"line one\r\nline two\r\n",
    "bom": b"\xef\xbb\xbfmy own rules\n",
    "tabs_and_trailing_spaces": b"a\tb  \n\tindented \n",
    "non_ascii": "café 中文 \U0001F600\n".encode("utf-8"),
    "whitespace_only": b"  \n\t\n \n",
}
B2_IDS = list(B2_CASES)


@pytest.mark.parametrize("name", B2_IDS)
def test_B2_markerless_file_keeps_its_bytes_then_one_blank_line_then_the_block(name):
    """B2: result.startswith(existing); then ONE blank line; then the block. A merge that dropped,
    re-encoded or re-ended the user's text (M1), or put the block first, would differ."""
    blk = _blk()
    existing = B2_CASES[name].decode("utf-8")
    out = blk.merge(existing, R_NEW)
    assert out.text.startswith(existing)
    rest = out.text[len(existing):]
    block = blk.wrap(R_NEW)
    assert rest.endswith(block)
    gap = rest[: -len(block)]
    if name == "empty":
        # absent and empty both reach new_text_fn as "", so the pure render is acceptable here
        assert gap in ("", "\n", "\r\n")
        assert out.kind in ("created", "appended")
        return
    assert out.kind == "appended"
    if existing.endswith("\n"):
        assert gap in ("\n", "\r\n"), f"exactly one blank line expected, got {gap!r}"
    else:
        # the last line is ended first, then the blank line: two line feeds in total
        assert gap in ("\n\n", "\r\n\r\n"), f"unterminated last line: got gap {gap!r}"


def test_B2_a_file_with_no_final_newline_gets_its_last_line_ended_then_a_blank_line():
    """B2 / D2 pin: 'my own rules' + '\\n\\n' + block. If the blank line were missing the block would be
    glued to the user's last line; D2's 'gains exactly one newline' depends on this shape."""
    blk = _blk()
    out = blk.merge("my own rules", R_NEW)
    assert out.text == "my own rules\n\n" + blk.wrap(R_NEW)


def test_B2b_an_appended_block_says_in_plain_words_that_an_older_copy_now_appears_twice():
    """B2b: the report line for an APPEND is the one sentence of the contract, verbatim; any other kind
    has no such line. If the line were missing (M23), a user with an older ENGRAM text would not learn
    it now appears twice."""
    blk = _blk()
    sentence = ("a block was appended; if this file already held an older ENGRAM text outside a block, "
                "that text is still there and now appears twice; remove the older copy by hand")
    out = blk.merge("my own rules\n", R_NEW)
    assert out.kind == "appended"
    assert sentence.lower() in out.line.lower(), out.line
    assert blk.merge(None, R_NEW).line == ""


# ------------------------------------------------------------------------------ B3

B3_CASES = {
    "nothing_around": ("", ""),
    "text_both_sides": ("my rules\n\n", "\nafter\n"),
    "after_has_no_final_newline": ("my rules\n\n", "tail without newline"),
    "crlf_outside": ("a\r\nb\r\n\r\n", "\r\nc\r\nd\r\n"),
    "bom_on_the_first_line": ("﻿my rules\n\n", "\n"),
    "bom_directly_before_begin": ("﻿", "after\n"),
    "tabs_and_trailing_spaces": ("a\tb  \n\t\n", " tail \t\n"),
    "non_ascii": ("café 中文\n\n", "☃ end\n"),
}


@pytest.mark.parametrize("name", list(B3_CASES))
def test_B3_only_the_block_changes_the_bytes_around_it_stay(name):
    """B3: with exactly one block (BEGIN first) only the lines BEGIN..END change; the text before
    BEGIN and after END is identical. A merge that normalised line endings, dropped the BOM, or
    trimmed the tail would differ (M1, M6)."""
    blk = _blk()
    before, after = B3_CASES[name]
    existing = before + blk.wrap(R_OLD) + after
    out = blk.merge(existing, R_NEW)
    assert out.kind == "updated"
    assert out.text.startswith(before) and out.text.endswith(after)
    assert out.text == before + blk.wrap(R_NEW) + after


# ------------------------------------------------------------------------------ B4

def _b4_shapes():
    B, E = _begin(), END_LIT
    return {
        # name: (text, line numbers the message must name)
        "two_blocks": (f"{B}\nbody\n{E}\n{B}\nbody2\n{E}\n", {1, 4}),
        "two_end_lines": (f"{B}\nbody\n{E}\n{E}\n", {3, 4}),
        "begin_without_end": (f"top\n{B}\nbody\n", {2}),
        "end_without_begin": (f"top\nbody\n{E}\n", {3}),
        "end_before_begin": (f"{E}\nbody\n{B}\n", {1, 3}),
        "interleaved": (f"{B}\na\n{E}\nmid\n{B}\nb\n{E}\n", {1, 5}),
        "near_miss_indented_begin": (f"top\n  {B}\nbody\n{E}\n", {2}),
        "near_miss_indented_end": (f"{B}\nbody\n\t{E}\n", {3}),
    }


B4_SHAPES = _b4_shapes()


@pytest.mark.parametrize("shape", list(B4_SHAPES))
def test_B4_every_ambiguous_marker_shape_is_refused_in_one_line_naming_the_lines(shape):
    """B4: two or more BEGIN, two or more END, BEGIN without END, END without BEGIN, END before BEGIN,
    interleaved, or an indented marker: MergeRefused, ONE plain line, naming the line numbers found.
    A finder that took the last END (M2) or ignored an indented marker would not refuse."""
    text, numbers = B4_SHAPES[shape]
    msg = _refusal(text)
    assert "\n" not in msg.strip(), "ONE message line"
    named = {int(n) for n in re.findall(r"\d+", msg)}
    assert numbers <= named, f"message must name line(s) {sorted(numbers)}; names {sorted(named)}: {msg!r}"


@pytest.mark.parametrize("shape", list(B4_SHAPES))
def test_B4_outranks_the_overwrite_flag_at_the_function_level(shape):
    """B4 outranks B10 (Zephyr blocker): overwrite_edited_block=True does not turn ANY B4 shape into a
    write. If the flag skipped the marker scan (M21), an ambiguous file would be rewritten."""
    text, _ = B4_SHAPES[shape]
    msg = _refusal(text, overwrite_edited_block=True)
    assert "\n" not in msg.strip()


def _clean_variants():
    blk = _blk()
    w = blk.wrap(R_OLD)
    lines = w.split("\n")
    def with_(i, suffix):
        out = list(lines)
        out[i] = out[i] + suffix
        return "\n".join(out)
    last = len(lines) - 2  # the END line (the list ends with the empty string after the final newline)
    return {
        "begin_trailing_spaces": with_(0, "  "),
        "begin_trailing_tab": with_(0, "\t"),
        "begin_trailing_cr": with_(0, "\r"),
        "end_trailing_spaces_and_tab": with_(last, " \t"),
        "end_trailing_cr": with_(last, "\r"),
        "bom_before_begin_on_line_1": "﻿" + w,
    }


@pytest.mark.parametrize("variant", ["begin_trailing_spaces", "begin_trailing_tab", "begin_trailing_cr",
                                     "end_trailing_spaces_and_tab", "end_trailing_cr", "bom_before_begin_on_line_1"])
def test_B4_control_a_marker_with_trailing_whitespace_or_a_leading_bom_is_a_clean_marker(variant):
    """B4 control / M9 / M15: trailing spaces, tabs or CR on a marker line, or a BOM on line 1, are
    editor noise, not ambiguity. If those were treated as text, a clean one-block file would be
    appended to (two blocks) or refused; the merge must update the one block it can find."""
    blk = _blk()
    existing = _clean_variants()[variant]
    out = blk.merge(existing, R_NEW)  # must not raise
    assert out.kind == "updated"
    assert out.text.count(BEGIN_LIT) == 1 and out.text.count(END_LIT) == 1
    if variant.startswith("bom"):
        assert out.text.startswith("﻿"), "the BOM is kept in the output"
    assert blk.wrap(R_NEW) in out.text


def test_B4_control_marker_text_inside_a_longer_line_is_ordinary_text():
    """Definitions: a sentence that merely mentions a marker is ordinary text (Zephyr's narrower
    line). If it were a marker, a user's note about the markers would be refused or read as a block."""
    blk = _blk()
    existing = (f"notes: the line {END_LIT} closes it,\n"
                f"and {BEGIN_LIT}<hash> -->\nstarts it, per the docs\n")
    out = blk.merge(existing, R_NEW)
    assert out.kind == "appended"
    assert out.text.startswith(existing)
    assert out.text.endswith(blk.wrap(R_NEW))


# ------------------------------------------------------------------------------ B5 note
# B5 (a file that is not UTF-8 text is refused) is a byte-level rule on the file read; the function
# takes str, so its cell is at the command-line level (test_bootstrap_merge_block_4444.py).


# ------------------------------------------------------------------------------ B6 / B7

def _b6_inputs():
    blk = _blk()
    out = [("absent", None)]
    out += [(f"b2_{n}", b.decode("utf-8")) for n, b in B2_CASES.items() if n != "empty"]
    out += [(f"b3_{n}", before + blk.wrap(R_OLD) + after) for n, (before, after) in B3_CASES.items()]
    return out


@pytest.mark.parametrize("name", ["absent"] + [f"b2_{n}" for n in B2_IDS if n != "empty"] + [f"b3_{n}" for n in B3_CASES])
def test_B6_running_the_merge_on_its_own_output_changes_nothing(name):
    """B6: for each input of B1 to B3, merge(merge(x)) is the same text and reports 'unchanged'. If the
    first output were not a fixed point (a growing blank line, a re-hashed notice), a second install
    would rewrite the file every time."""
    blk = _blk()
    existing = dict(_b6_inputs())[name]
    first = blk.merge(existing, R_NEW)
    second = blk.merge(first.text, R_NEW)
    assert second.text == first.text
    assert second.kind == "unchanged"


def test_B7_a_file_that_already_equals_the_new_text_is_unchanged_with_no_report():
    """B7: the file equals the text this run would write -> kind 'unchanged', text identical, no
    changed lines, no report line (M4: a merge that 'writes' an identical file would back it up)."""
    blk = _blk()
    w = blk.wrap(R_NEW)
    out = blk.merge(w, R_NEW)
    assert (out.kind, out.text, out.changed_lines, out.line) == ("unchanged", w, 0, "")


# ------------------------------------------------------------------------------ B9

UPDATED_RE = re.compile(r"^ENGRAM block updated \((\d+) lines? changed\)")


def test_B9a_equal_hash_means_nobody_edited_so_the_block_is_replaced_and_reported():
    """B9(a): hash equals the hash of the current body -> the block is replaced (new body, new hash),
    kind 'updated', and the report line starts 'ENGRAM block updated (N lines changed)'. A merge that
    refused here (every upgrade would need the flag) or wrote with no line (M12) would differ."""
    blk = _blk()
    out = blk.merge("top\n\n" + blk.wrap(R_OLD) + "\nbottom\n", R_NEW)
    assert out.kind == "updated"
    assert out.text == "top\n\n" + blk.wrap(R_NEW) + "\nbottom\n"
    m = UPDATED_RE.match(out.line)
    assert m, f"report line is not the B9(a) line: {out.line!r}"
    assert int(m.group(1)) == out.changed_lines == ADDED_LINES


def test_B9a_the_count_is_the_same_for_a_pure_deletion():
    """B9(a): N counts changed lines. Going from R_NEW back to R_OLD removes exactly four lines; a
    count that only looked at additions would report 0."""
    blk = _blk()
    out = blk.merge(blk.wrap(R_NEW), R_OLD)
    assert out.kind == "updated" and out.changed_lines == ADDED_LINES


def _render_with(items: str) -> str:
    return f"# Rules\n\nFirst rule.\n\n## Section\n\n{items}\n## Tail\n\nlast line\n"


def test_B9a_definition_replacing_two_lines_by_three_counts_three():
    """B9a: changed_lines is, per non-equal block of a line-by-line comparison, the LARGER of the lines
    removed and the lines added. Two lines replaced by three is 3 (a +/- line count would say 5, an
    'added only' count 3 but 0 for a deletion)."""
    blk = _blk()
    old = _render_with("- item one\n- item two\n")
    new = _render_with("- new a\n- new b\n- new c\n")
    out = blk.merge(blk.wrap(old), new)
    assert out.kind == "updated" and out.changed_lines == 3
    assert UPDATED_RE.match(out.line).group(1) == "3"


def test_B9a_definition_an_equal_count_change_counts_that_count_not_double():
    """B9a: two lines replaced by two is 2, not 4. A count of removed plus added lines would say 4."""
    blk = _blk()
    old = _render_with("- item one\n- item two\n")
    new = _render_with("- other a\n- other b\n")
    assert blk.merge(blk.wrap(old), new).changed_lines == 2


def test_B9a_definition_separate_blocks_add_up():
    """B9a: the sum runs over EVERY non-equal block. One line changed near the top (1) and one line replaced
    by three further down (3) is 4; counting only the largest block would say 3."""
    blk = _blk()
    old = "# Rules\n\nFirst rule.\n\n## Section\n\n- keep one\n- keep two\n- keep three\n\n## Tail\n\nlast line\n"
    new = "# Rules\n\nFirst rule, reworded.\n\n## Section\n\n- keep one\n- keep two\n- keep three\n\n## Tail\n\nx 1\nx 2\nx 3\n"
    assert blk.merge(blk.wrap(old), new).changed_lines == 4


def test_B9a_control_a_crlf_converted_block_is_still_state_a_not_an_edit():
    """B9 control / E7 / M18: an editor that converted the whole file to CRLF did not edit the body.
    CRLF read as LF gives the same hash, so the block is replaced (and the text outside keeps its
    CRLF bytes). A hash over raw CR bytes would refuse every CRLF file."""
    blk = _blk()
    before, after = "a\r\nb\r\n\r\n", "\r\nz\r\n"
    crlf_block = blk.wrap(R_OLD).replace("\n", "\r\n")
    out = blk.merge(before + crlf_block + after, R_NEW)
    assert out.kind == "updated"
    assert out.text.startswith(before) and out.text.endswith(after)
    assert blk.wrap(R_NEW) in out.text


def _edited(wrapped: str, extra: str) -> str:
    """The same block with `extra` inserted right after the notice line and the hash left stale."""
    lines = wrapped.split("\n")
    assert extra.endswith("\n")
    return "\n".join(lines[:2] + extra.split("\n")[:-1] + lines[2:])


def test_B9b_a_readable_hash_that_differs_is_refused_with_the_count_the_diff_and_the_flag():
    """B9(b): an edit inside the block -> MergeRefused. The message is one plain line (it says it
    prints lines of the user's own file, gives the count of changed lines and names the flag) followed
    by diff lines carrying the user's text. A merge that replaced the edited block (M16) would not raise."""
    blk = _blk()
    existing = _edited(blk.wrap(R_OLD), "USER EDIT 1\nUSER EDIT 2\n")
    msg = _refusal(existing, R_OLD)
    lines = msg.split("\n")
    assert OWN_TEXT_RE.search(lines[0]), f"first line must say it prints the user's own lines: {lines[0]!r}"
    assert FLAG in lines[0]
    assert "2" in re.findall(r"\d+", lines[0]), "the count of changed lines (2) is in the first line"
    diff = lines[1:]
    assert diff, "the diff lines follow the first line"
    assert all(ln[:1] in ("-", "+", " ", "@") for ln in diff if ln), diff
    assert any("USER EDIT 1" in ln for ln in diff) and any("USER EDIT 2" in ln for ln in diff)


def test_B9b_the_diff_is_cut_at_forty_lines():
    """B9(b): 'the first 40 lines of that unified diff'. With a hundred user lines the message has one
    plain line plus exactly forty diff lines; an uncut diff would put the user's whole edit in a log."""
    blk = _blk()
    many = "".join(f"user line {i}\n" for i in range(100))
    msg = _refusal(_edited(blk.wrap(R_OLD), many), R_OLD)
    lines = msg.split("\n")
    assert len(lines) - 1 == 40, f"{len(lines) - 1} diff lines after the first"
    assert "100" in re.findall(r"\d+", lines[0])


def test_B9b_an_edit_of_one_character_in_the_notice_line_is_state_b():
    """B9: 'an edit of one character in the notice line is (b)'. The notice is inside the hashed body;
    a hash that skipped it would let a user's change there be overwritten silently."""
    blk = _blk()
    lines = blk.wrap(R_OLD).split("\n")
    lines[1] = lines[1][:-1] + "X"
    msg = _refusal("\n".join(lines), R_OLD)
    assert OWN_TEXT_RE.search(msg.split("\n")[0])


def test_B9c_a_begin_line_without_a_readable_hash_is_treated_exactly_like_b():
    """B9(c): hash missing, not 64 hex, or not LOWER-case hex is a BEGIN with no readable hash: refused
    like (b), with the cause in words ('a block this install did not write, or a damaged marker') and
    the flag. Treating it as (a) (M17) would overwrite a hand-written block."""
    blk = _blk()
    good = blk.wrap(R_OLD)
    body = "\n".join(good.split("\n")[1:])
    good_hash = good.split("\n")[0][len(BEGIN_LIT):-len(SUFFIX_LIT)]
    for bad in ("", "zzz", good_hash.upper(), good_hash[:-1], good_hash + "0"):
        msg = _refusal(_begin(bad) + "\n" + body, R_OLD)
        assert "did not write" in msg and "damaged marker" in msg, (bad, msg)
        assert FLAG in msg, (bad, msg)


def test_B10_the_overwrite_flag_writes_states_b_and_c_and_reports_like_a():
    """B10: with overwrite_edited_block=True an edited block (b) and a damaged marker (c) are written
    (the new block, new hash) and the same 'ENGRAM block updated' line as (a) is produced. A flag that
    did nothing would leave the user with no way past (b)."""
    blk = _blk()
    edited = "top\n\n" + _edited(blk.wrap(R_OLD), "USER EDIT\n") + "bottom\n"
    out_b = blk.merge(edited, R_OLD, overwrite_edited_block=True)
    assert out_b.kind == "updated" and out_b.text == "top\n\n" + blk.wrap(R_OLD) + "bottom\n"
    assert UPDATED_RE.match(out_b.line)
    damaged = _begin("zzz") + "\n" + "\n".join(blk.wrap(R_OLD).split("\n")[1:])
    out_c = blk.merge(damaged, R_OLD, overwrite_edited_block=True)
    assert out_c.kind == "updated" and out_c.text == blk.wrap(R_OLD)
    assert UPDATED_RE.match(out_c.line)


def test_B10_control_the_flag_changes_nothing_for_an_untouched_block_or_a_markerless_file():
    """B10 control: the flag answers 'the block was edited' only. On a clean block it is state (a); on a
    markerless file it is still an append; on an absent file still a create."""
    blk = _blk()
    assert blk.merge(blk.wrap(R_OLD), R_NEW, overwrite_edited_block=True) == blk.merge(blk.wrap(R_OLD), R_NEW)
    assert blk.merge("mine\n", R_NEW, overwrite_edited_block=True) == blk.merge("mine\n", R_NEW)
    assert blk.merge(None, R_NEW, overwrite_edited_block=True) == blk.merge(None, R_NEW)


# ------------------------------------------------------------------------------ B11

B11_MESSAGE = "this file already holds this install's ENGRAM text outside a block"


def test_B11_the_render_verbatim_in_a_markerless_file_is_refused_in_one_line():
    """B11: when this install's render (before the wrap) already appears verbatim OUTSIDE any block, the
    merge refuses in one line with the contract's sentence. Without it (M22) a re-run would write the
    ENGRAM text twice."""
    msg = _refusal("my notes\n\n" + R_NEW + "\nmore of mine\n", R_NEW)
    assert "\n" not in msg.strip()
    assert B11_MESSAGE in msg


def test_B11_the_render_verbatim_outside_a_one_block_file_is_refused_before_and_after_the_block():
    """B11: 'the text verbatim outside a one-block file' also refuses, whether the stray copy sits
    before or after the block. A check that only scanned markerless files would miss both."""
    blk = _blk()
    block = blk.wrap(R_OLD)
    for existing in (R_NEW + "\n" + block, block + "\n" + R_NEW):
        assert B11_MESSAGE in _refusal(existing, R_NEW)


def test_B11_the_render_with_crlf_line_ends_is_still_the_same_text():
    """B11: 'line ends read as LF'. The same render saved with CRLF is a duplicate too; an exact byte
    comparison would miss it."""
    msg = _refusal("mine\r\n" + R_NEW.replace("\n", "\r\n"), R_NEW)
    assert B11_MESSAGE in msg


def test_B11_no_flag_overrides_it():
    """B11: 'no flag overrides it'. overwrite_edited_block answers a different question."""
    assert B11_MESSAGE in _refusal("x\n\n" + R_NEW, R_NEW, overwrite_edited_block=True)


@pytest.mark.parametrize("older", [
    pytest.param(R_NEW.replace("First rule.", "First rule, an older wording."), id="one_line_older"),
    pytest.param(R_NEW[: -len("Three more lines here.\n")], id="the_text_minus_its_last_line"),
    pytest.param("# Rules\n\nsomething else entirely\n", id="different_text"),
])
def test_B11_control_an_older_or_partial_text_is_not_refused(older):
    """B11 control (M22b): only the EXACT current text is refused; an older render or a prefix of it is
    appended (the B2b line warns). A check that refused everything would pass the refusal cells."""
    blk = _blk()
    out = blk.merge("mine\n\n" + older, R_NEW)
    assert out.kind == "appended"


def test_B11_control_the_render_inside_the_block_is_not_outside_text():
    """B11 control: the render inside the block is the block, not a stray copy. A file that is exactly
    the wrapped render must be 'unchanged', not refused as a duplicate of itself."""
    blk = _blk()
    assert blk.merge(blk.wrap(R_NEW), R_NEW).kind == "unchanged"


# ------------------------------------------------------------------------------ A5 / A6 / unwrap

def test_A5_a_file_with_no_markers_comes_back_from_unwrap_unchanged():
    """A5: a markerless file (every seat today) is compared as a whole file, unchanged. An unwrap that
    stripped or normalised it would make an old unwrapped CLAUDE.md read as drift."""
    blk = _blk()
    for text in (R_OLD, "no trailing newline", "crlf\r\nfile\r\n", ""):
        assert blk.unwrap(text) == text


def test_A6_a_crlf_converted_hashed_wrapped_file_unwraps_to_the_lf_render():
    """A6: 'a CRLF-converted hashed wrapped file reads no drift': unwrap reads CRLF as LF, so the render
    comes back equal. A comparator on raw CR bytes (M25) would see drift in every converted file."""
    blk = _blk()
    assert blk.unwrap(blk.wrap(R_NEW).replace("\n", "\r\n")) == R_NEW


def test_A1_unwrap_returns_the_blocks_inside_without_the_notice_and_ignores_text_outside():
    """A1: comparators compare the block's INSIDE. With text on both sides, unwrap gives exactly the
    render (no markers, no notice, none of the user's text), so a user's own lines never read as drift."""
    blk = _blk()
    assert blk.unwrap("top\n\n" + blk.wrap(R_NEW) + "\nbottom\n") == R_NEW


def test_A1_unwrap_strips_the_begin_line_by_prefix_and_suffix_not_by_one_exact_string():
    """A1 / M10: the BEGIN line is recognised by its constant prefix and suffix. A block whose hash is
    any 64 hex digits (here another one) must still unwrap; a comparator keyed on one exact string
    would not match a hashed BEGIN."""
    blk = _blk()
    w = blk.wrap(R_NEW)
    other = _begin("a" * 64) + "\n" + "\n".join(w.split("\n")[1:])
    assert blk.unwrap(other) == R_NEW


# ------------------------------------------------------------------------------ D2 / D2b (remove_block)

D2_ORIGINALS = {
    "lf": "my rules\nsecond line\n",
    "crlf": "a\r\nb\r\n",
    "bom": "﻿my rules\n",
    "tabs_trailing": "a\tb  \n\tindented \n",
    "non_ascii": "café 中文\n",
}


@pytest.mark.parametrize("name", list(D2_ORIGINALS))
def test_D2_remove_block_after_merge_returns_an_original_that_ends_in_a_newline_byte_for_byte(name):
    """D2: for every original that ends in a newline, removing the block from the merged file returns
    the original exactly. If remove_block left the blank line merge added (or took a line of the user's)
    the round trip would differ by one line."""
    blk = _blk()
    original = D2_ORIGINALS[name]
    merged = blk.merge(original, R_NEW).text
    assert blk.remove_block(merged) == original


def test_D2_an_original_with_no_final_newline_gains_exactly_one_newline():
    """D2 [Q2]: 'my rules' merged then removed is 'my rules\\n'. The cell pins exactly that, so the one
    newline the merge had to add is the only difference and nothing more is lost or added."""
    blk = _blk()
    assert blk.remove_block(blk.merge("my rules", R_NEW).text) == "my rules\n"


def test_D2b_text_after_the_end_line_is_joined_cleanly_to_the_text_before():
    """D2b: with text on BOTH sides of the block, removing the block and the one blank line before it
    leaves before + after, nothing else. A remove that cut from the first BEGIN to the end of the file
    (M6) would lose the tail."""
    blk = _blk()
    before, after = "top one\ntop two\n", "tail one\ntail two\n"
    merged = before + "\n" + blk.wrap(R_OLD) + after
    assert blk.remove_block(merged) == before + after


def test_D2b_crlf_outside_lines_keep_their_endings_the_block_has_lf_inside_a_crlf_file():
    """D2b: merge writes the block with LF endings inside a CRLF file (stated), and removal leaves every
    outside line ending as it was. A remove that normalised the file to LF would change the user's bytes."""
    blk = _blk()
    original = "a\r\nb\r\n"
    merged = blk.merge(original, R_NEW).text
    assert merged.startswith(original)
    block = blk.wrap(R_NEW)
    assert merged.endswith(block) and "\r" not in block, "the block itself is written with LF only"
    gap = merged[len(original):-len(block)]
    assert gap.count("\n") == 1 and gap.strip("\r\n") == ""
    after = "tail\r\nmore\r\n"
    assert blk.remove_block(merged + after) == original + after


@pytest.mark.parametrize("shape", ["two_blocks", "begin_without_end", "end_without_begin", "near_miss_indented_begin"])
def test_D3_remove_block_refuses_a_b4_shape(shape):
    """D3 / pure half: remove_block raises MergeRefused on a B4 shape, so the uninstall can leave the
    file untouched with one line. A remove that guessed which block was ENGRAM's would cut user text."""
    blk = _blk()
    text, _ = B4_SHAPES[shape]
    with pytest.raises(blk.MergeRefused):
        blk.remove_block(text)


# ------------------------------------------------------------------------------ F1 / F1b / F2

@pytest.mark.parametrize("template", ["template.CLAUDE.md", "template.CLAUDE.local.md"])
def test_F1_every_profiles_wrapped_render_has_one_begin_first_one_end_last_and_a_true_hash(template):
    """F1 / F1b / E3: for both profiles the wrapped render has exactly one BEGIN as its first line with
    a hash equal to SHA-256 of its body, and exactly one END as its last non-empty line. A profile left
    unwrapped (M8) or a hash that does not verify would put every user of an older version in state (c)."""
    blk = _blk()
    text = (SRC / "templates" / template).read_text(encoding="utf-8")
    w = blk.wrap(text)
    assert w.count(BEGIN_LIT) == 1 and w.count(END_LIT) == 1
    assert w.split("\n")[0].startswith(BEGIN_LIT)
    assert [ln for ln in w.split("\n") if ln.strip()][-1] == END_LIT
    begin = w.split("\n")[0]
    body = "\n".join(w.split("\n")[1:-2]) + "\n"
    assert begin == _begin(_sha(body))


def test_F2_a_later_section_added_to_the_template_lands_inside_the_markers():
    """F2: a template with one extra `##` section still has the markers as first and last lines, and
    the section is inside. A wrap that put new sections after END would leave them outside ENGRAM's
    block, where a merge never updates them."""
    blk = _blk()
    text = (SRC / "templates" / "template.CLAUDE.md").read_text(encoding="utf-8") + "\n## A later section\n\nbody\n"
    w = blk.wrap(text)
    lines = [ln for ln in w.split("\n") if ln.strip()]
    assert lines[0].startswith(BEGIN_LIT) and lines[-1] == END_LIT
    assert w.index("## A later section") < w.index(END_LIT)


# ------------------------------------------------------------------------------ tier

def test_tier_the_new_module_has_a_tiers_json_entry_in_the_same_tier_as_the_file_guard():
    """Contract text (tier): engram_claude_block.py is tiered like engram_file_guard.py (bootstrap.py
    imports it at module level, so both are hard dependencies). CI's manifest-gate fails a shippable
    with no entry; this pins the TIER as well, which the gate does not."""
    data = json.loads(TIERS.read_text(encoding="utf-8"))
    entries = [e for e in _walk(data) if isinstance(e, dict) and "path" in e and "tier" in e]
    tiers = {e["path"]: e["tier"] for e in entries}
    assert "engram_file_guard.py" in tiers, "reference entry vanished from tiers.json"
    assert "engram_claude_block.py" in tiers, "no tiers.json entry for engram_claude_block.py"
    assert tiers["engram_claude_block.py"] == tiers["engram_file_guard.py"]


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)
