"""The ENGRAM block in a user's CLAUDE.md: pure text functions, no I/O, no side effects at import.

(engram-alpha#4444.) ENGRAM's CLAUDE.md text lives between two marker lines so that `bootstrap.py --merge`
can keep the user's own text around it, and the uninstaller can take only ENGRAM's part out:

    <!-- engram:begin sha256=<64 lower-case hex digits> -->
    <notice line>
    <the rendered template>
    <!-- engram:end -->

The hash is the SHA-256 of the block BODY (everything between the end of the BEGIN line and the start of
the END line, the notice line included), with every CR+LF read as LF so an editor that converts line endings
does not look like an edit. THE HASH IS AN EDIT DETECTOR, NOT A SECURITY CONTROL: anyone who can edit the
file can edit the hash too.

RULE FOR LATER SECTIONS (F2): a section added later (for example the trust settings of #4442) goes INSIDE
the block, by editing the template or its components. ENGRAM writes nothing outside the markers except the
one blank line `merge` puts before an appended block. The marker constants below are defined ONCE, here:
bootstrap, the comparators and the uninstaller all import them.

Callers: bootstrap.py (wrap, merge, unwrap, block_hash), engram_file_guard.py (through a `new_text_fn` that
calls merge, and the MergeRefused it catches), check-claude-md-drift.py (unwrap), and
uninstall-local-marketplace.sh through the venv python (plan_removal).
"""

from __future__ import annotations

import difflib
import hashlib
import re
from typing import NamedTuple

BEGIN_PREFIX = "<!-- engram:begin sha256="
BEGIN_SUFFIX = " -->"
END_LINE = "<!-- engram:end -->"
NOTICE_LINE = "ENGRAM rewrites everything between these markers; put your own text outside them."
OVERWRITE_FLAG = "--overwrite-edited-block"
APPEND_LINE = (
    "a block was appended; if this file already held an older ENGRAM text outside a block, that text is "
    "still there and now appears twice; remove the older copy by hand"
)
DIFF_LINES_SHOWN = 40
_BOM = "\ufeff"
_HASH_RE = re.compile(r"[0-9a-f]{64}")


class MergeRefused(Exception):
    """A merge or a removal that must not touch the file. The message is ONE plain line, plus (state (b)
    and (c) of B9 only) the diff lines after it. It does not name the file: the caller knows the path."""


class MergeOutcome(NamedTuple):
    text: str
    kind: str          # created | appended | updated | unchanged
    changed_lines: int
    line: str          # the one report line: the B2b sentence (appended), the B9(a) line (updated), else ""


def body_sha256(body: str) -> str:
    """Hex SHA-256 of `body` as UTF-8, every CR+LF read as LF (a lone CR is content)."""
    return hashlib.sha256(body.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def wrap(render: str) -> str:
    """The block: BEGIN line (with the hash), the notice line, the render, END line, one final newline.
    Raises ValueError when the render itself contains the marker text anywhere (A3): a template that
    documents the markers must fail at render time, never ship a file the next merge would refuse."""
    if BEGIN_PREFIX in render or END_LINE in render:
        raise ValueError(
            "the rendered CLAUDE.md text contains the ENGRAM marker text "
            f"({BEGIN_PREFIX!r} or {END_LINE!r}); remove it from the template"
        )
    if not render.endswith("\n"):
        render += "\n"
    body = NOTICE_LINE + "\n" + render
    return f"{BEGIN_PREFIX}{body_sha256(body)}{BEGIN_SUFFIX}\n{body}{END_LINE}\n"


def _is_begin(s: str) -> bool:
    return s.startswith(BEGIN_PREFIX) and s.endswith(BEGIN_SUFFIX) and len(s) >= len(BEGIN_PREFIX) + len(BEGIN_SUFFIX)


class _Line(NamedTuple):
    start: int   # offset of the line (after a leading BOM, for a BEGIN line on line 1)
    end: int     # offset just past the line's own terminator
    core: str    # the line without terminator and without trailing spaces, tabs and CR
    kind: "str | None"  # begin | end | near | None


def _scan(text: str) -> "list[_Line]":
    out = []
    for n, m in enumerate(re.finditer(r"[^\n]*\n|[^\n]+", text)):
        raw = m.group(0)
        content = raw[:-1] if raw.endswith("\n") else raw
        bom = n == 0 and content.startswith(_BOM)
        core = (content[1:] if bom else content).rstrip(" \t\r")
        kind = None
        if _is_begin(core):
            kind = "begin"
        elif core == END_LINE:
            kind = "end"
        else:
            lead = core.lstrip(" \t")
            if lead != core and (_is_begin(lead) or lead == END_LINE):
                kind = "near"  # an indented marker: refused, never guessed at
        out.append(_Line(m.start() + (1 if bom and kind == "begin" else 0), m.end(), core, kind))
    return out


def _locate(text: str) -> "tuple[list[_Line], tuple[int, int] | None]":
    """(lines, (begin index, end index)) for a text with exactly one clean block, (lines, None) for no marker
    at all; MergeRefused for every B4 shape."""
    lines = _scan(text)
    begins = [i for i, ln in enumerate(lines) if ln.kind == "begin"]
    ends = [i for i, ln in enumerate(lines) if ln.kind == "end"]
    near = [i for i, ln in enumerate(lines) if ln.kind == "near"]
    ambiguous = (
        near or len(begins) > 1 or len(ends) > 1 or bool(begins) != bool(ends)
        or (begins and ends and ends[0] < begins[0])
    )
    if ambiguous:
        found = []
        if begins:
            found.append("BEGIN line " + ", ".join(str(i + 1) for i in begins))
        if ends:
            found.append("END line " + ", ".join(str(i + 1) for i in ends))
        if near:
            found.append("indented marker line " + ", ".join(str(i + 1) for i in near))
        raise MergeRefused(
            "the ENGRAM markers are ambiguous (found " + "; ".join(found) + "); ENGRAM cannot tell which block "
            "is its own, so it changed nothing. Edit the file by hand so it holds at most one block "
            "(one BEGIN line then one END line, each alone on its line), then re-run"
        )
    return lines, ((begins[0], ends[0]) if begins else None)


def block_hash(text: str) -> "str | None":
    """The hash in the BEGIN line of the one block in `text`; None when there is no block or the hash is not
    64 lower-case hex digits. Raises MergeRefused on a B4 shape."""
    lines, loc = _locate(text)
    if loc is None:
        return None
    h = lines[loc[0]].core[len(BEGIN_PREFIX):-len(BEGIN_SUFFIX)]
    return h if _HASH_RE.fullmatch(h) else None


def _outside(text: str, lines: "list[_Line]", loc: "tuple[int, int] | None") -> str:
    if loc is None:
        return text
    return text[: lines[loc[0]].start] + text[lines[loc[1]].end:]


def _changed(current: str, new: str) -> "tuple[int, list[str]]":
    """(changed_lines, unified diff lines). B9a: over every non-equal block of a line-by-line comparison,
    the larger of the lines removed and the lines added, summed."""
    a = current.replace("\r\n", "\n").splitlines()
    b = new.replace("\r\n", "\n").splitlines()
    n = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
            if tag != "equal")
    diff = list(difflib.unified_diff(a, b, "the block in your file", "the block this install writes", lineterm="", n=1))
    return n, diff


def _updated_line(n: int) -> str:
    return f"ENGRAM block updated ({n} lines changed)"


def merge(existing: "str | None", render: str, *, overwrite_edited_block: bool = False) -> MergeOutcome:
    """The CLAUDE.md text after a run: `existing` (None = no file) with ENGRAM's block for `render`.

    Pure. Raises MergeRefused for every refusal (B4, B9(b)/(c), B11). B4 outranks `overwrite_edited_block`."""
    new_block = wrap(render)
    if existing is None:
        return MergeOutcome(new_block, "created", 0, "")
    lines, loc = _locate(existing)  # B4 first: no flag answers an ambiguity
    render_lf = render.replace("\r\n", "\n")
    if render_lf in _outside(existing, lines, loc).replace("\r\n", "\n"):
        raise MergeRefused(
            "this file already holds this install's ENGRAM text outside a block; appending would write it twice; "
            "remove one copy by hand"
        )
    if loc is None:  # B2: the user's bytes, one blank line, the block
        if existing == "":
            return MergeOutcome(new_block, "appended", 0, APPEND_LINE)
        sep = "" if existing.endswith("\n") else "\n"
        return MergeOutcome(existing + sep + "\n" + new_block, "appended", 0, APPEND_LINE)
    b, e = loc
    body = existing[lines[b].end:lines[e].start]
    new_body = new_block[new_block.index("\n") + 1:-len(END_LINE) - 1]
    text = existing[: lines[b].start] + new_block + existing[lines[e].end:]
    recorded = block_hash(existing)
    n, diff = _changed(body, new_body)
    if recorded is not None and body_sha256(body) == recorded:  # (a): nobody edited inside
        if text == existing:
            return MergeOutcome(existing, "unchanged", 0, "")
        return MergeOutcome(text, "updated", n, _updated_line(n))
    if overwrite_edited_block:
        return MergeOutcome(text, "updated", n, _updated_line(n))
    cause = (
        "someone edited inside the block" if recorded is not None
        else "this is a block this install did not write, or a damaged marker"
    )
    first = (
        f"this prints lines of your own file: {cause} ({n} lines differ from the text this install would write); "
        f"move your text outside the markers, or re-run with --merge {OVERWRITE_FLAG} to overwrite the block"
    )
    raise MergeRefused("\n".join([first] + diff[:DIFF_LINES_SHOWN]))


def unwrap(text: str) -> str:
    """For comparators: the render inside the one block (CRLF read as LF, notice line removed). A text with no
    markers is returned UNCHANGED (A5). Raises MergeRefused on a B4 shape."""
    lines, loc = _locate(text)
    if loc is None:
        return text
    inside = text[lines[loc[0]].end:lines[loc[1]].start].replace("\r\n", "\n")
    first, sep, rest = inside.partition("\n")
    # Only the notice line is dropped: an edited or deleted notice line must stay visible to the comparators.
    return rest if sep and first == NOTICE_LINE else inside


def _body_matches_hash(text: str) -> bool:
    lines, loc = _locate(text)
    if loc is None:
        return False
    recorded = block_hash(text)
    return recorded is not None and body_sha256(text[lines[loc[0]].end:lines[loc[1]].start]) == recorded


def plan_removal(text: str, real_path: str, record: "dict | None") -> "tuple[str, str]":
    """What the uninstaller does with a CLAUDE.md (D1, D3, D6). Returns (action, detail):

      ("none", "")        no marker at all: today's restore rule applies (the caller decides, D3b)
      ("leave", reason)   ambiguous markers (D3), or no matching record (D6): the file stays untouched;
                          `reason` is ONE plain line without the file name
      ("pure", "")        nothing but whitespace outside the block and the file is what this install wrote
                          (its record matches and the body still hashes to the BEGIN hash): today's rule (D1)
      ("pure_other", "")  nothing but whitespace outside the block, but not provably what this install wrote:
                          today's rule too, and the caller says the replaced text did not match (D3c)
      ("remove", text)    text outside the block and an UNEDITED body: `text` is the file without the block (D2);
                          an edited body (or an END line moved over the user's text) is a "leave" (D6b)

    D6: the block is removed only when THIS ENGRAM home's record (B12) names this file (compared as real
    paths) and the hash in the BEGIN line equals the recorded hash. `record` is the parsed record or None."""
    try:
        lines, loc = _locate(text)
    except MergeRefused as e:
        return "leave", str(e).split("\n", 1)[0]
    if loc is None:
        return "none", ""
    recorded_ok = (
        isinstance(record, dict) and record.get("path") == real_path
        and isinstance(record.get("sha256"), str) and record["sha256"] == block_hash(text)
    )
    if not _outside(text, lines, loc).strip():
        return ("pure" if recorded_ok and _body_matches_hash(text) else "pure_other"), ""
    if not recorded_ok:
        return "leave", "no record in this ENGRAM home names this file's block, so ENGRAM does not remove it"
    if not _body_matches_hash(text):  # D6b: edited inside, or the END line moved down over the user's text
        return "leave", (
            "the block was changed after ENGRAM wrote it, so ENGRAM does not remove it; delete the lines "
            "between the two markers by hand if you want it gone"
        )
    return "remove", remove_block(text)


def remove_block(text: str) -> str:
    """The text without ENGRAM's block and without the one blank line `merge` put before it; every other byte
    (and every outside line ending) is kept. Raises MergeRefused on a B4 shape. No block: unchanged."""
    lines, loc = _locate(text)
    if loc is None:
        return text
    before = text[: lines[loc[0]].start]
    after = text[lines[loc[1]].end:]
    if before.endswith("\n\n"):
        before = before[:-1]
    elif before.endswith("\n\r\n"):
        before = before[:-2]
    return before + after
