#!/usr/bin/env python3
"""Erase the warm-briefing's volatile "## From this session" section (#3141).

Called by the engram-sleep skill, Step 3.  The user's letter above the anchor
is irreplaceable, so the erase is specified by CHARACTER OFFSET, never by
line-join: `"\\n".join(lines[:anchor_index])` silently drops one byte (the
blank line before the heading).  Everything above the anchor must be
byte-identical afterwards; that is asserted as a post-condition BEFORE any
write.

Usage:
    erase_session_block.py [--file PATH] [--dry-run]

Default file: $ENGRAM_HOME/warm-briefing.md, else ~/.engram/warm-briefing.md.

Exit codes:
    0  erased, or already bare (no change)
    2  usage / IO error: missing or unreadable file, OR a failed write (the message
       says whether the original bytes were restored, or the file was untouched)
    3  drift-STOP: an ATX level-1/2 heading sits below the anchor; file untouched
       (BEST-EFFORT structural proxy: ATX headings only; setext headings and
       plain prose are not detected)
    4  post-condition failure: before any write (file untouched), or a read-back
       mismatch after the write (the original bytes are restored in place)

The write is IN PLACE (open the existing file r+, write, truncate, fsync, read
back): the inode, ACLs, xattrs, mode and hard links survive by construction, and a
`deny delete` ACL cannot block it (#3419).  If the write or the read-back fails
the original bytes are restored in place.  There is no temp file / os.replace.
The in-place write is NOT atomic: a concurrent reader can briefly see the old tail
after the new heading (never an altered letter, never an empty file).

Anchor: regex (?m)^## From this session  (line-anchored prefix match; the LAST
match is the target).  No line-ending translation is ever performed.
"""
import argparse
import os
import re
import sys
from pathlib import Path

HEADING = "## From this session"
ANCHOR_RE = re.compile(r"(?m)^## From this session")
# Best-effort structural proxy: an ATX level-1/2 heading per CommonMark (up to 3
# leading spaces, then `#` or `##`, then space/tab/CR/end of line).  Setext headings
# and plain prose are NOT detected; the prose clause stays the caller's judgement.
BELOW_RE = re.compile(r"^ {0,3}#{1,2}(?:[ \t\r]|$)")


class DriftStop(Exception):
    """A level-1/2 heading below the anchor: the section is no longer last."""


class PostconditionError(Exception):
    """An erase post-condition failed; nothing may be written."""


def _anchors(text):
    return list(ANCHOR_RE.finditer(text))


def _erase_core(text: str) -> str:
    matches = _anchors(text)
    if matches:
        return text[: matches[-1].start()] + HEADING + "\n"
    if text and not text.endswith("\n"):
        text += "\n"
    return text + HEADING + "\n"


def _check_drift(text: str, last) -> None:
    line_no = text.count("\n", 0, last.start()) + 1
    # Split on "\n" only: a CRLF line keeps its "\r", which BELOW_RE treats as a terminator.
    for offset, line in enumerate(text[last.start():].split("\n")[1:], start=1):
        if BELOW_RE.match(line):
            raise DriftStop(
                f"drift-STOP: line {line_no + offset} below the "
                f"'{HEADING}' anchor is a level-1/2 heading: {line!r}; "
                "the section is no longer last. File untouched."
            )


def erase_session_block(text: str):
    """Erase and assert post-conditions. Returns (new_text, info)."""
    old_matches = _anchors(text)
    if old_matches:
        _check_drift(text, old_matches[-1])
        prefix = text[: old_matches[-1].start()]
        expected_count = len(old_matches)
    else:
        prefix = text if (not text or text.endswith("\n")) else text + "\n"
        expected_count = 1
    new = _erase_core(text)

    if new[: len(prefix)] != prefix:
        raise PostconditionError(
            "P1 byte identity failed: the region above the anchor changed "
            f"(expected {len(prefix)} chars preserved)"
        )
    if not new.endswith("\n") or new[:-1].rsplit("\n", 1)[-1] != HEADING:
        raise PostconditionError(f"P2 failed: final line is not exactly {HEADING!r}")
    new_matches = _anchors(new)
    if (
        not new_matches
        or new_matches[-1].start() != len(prefix)
        or len(new_matches) != expected_count
    ):
        raise PostconditionError(
            "P3 failed: last anchor not at the preserved-prefix boundary or "
            "anchor count changed unexpectedly"
        )
    if _erase_core(new) != new:
        raise PostconditionError("P4 failed: erase is not idempotent on its own output")

    removed_chars = removed_lines = 0
    if old_matches:
        # Everything after the (possibly suffixed) anchor line is removed; the
        # anchor line itself is rewritten to the bare heading.
        section = text[len(prefix):]
        removed_chars = max(0, len(section) - len(HEADING + "\n"))
        nl = section.count("\n")
        removed_lines = max(0, nl - 1 if section.endswith("\n") else nl)
    info = {"kept": len(prefix), "removed_chars": removed_chars, "removed_lines": removed_lines}
    return new, info


class WriteFailure(OSError):
    """The in-place write failed; the message says whether the original was restored."""


def _restore_in_place(path: Path, original: str) -> bool:
    """Best-effort: put `original` back into the same inode. True if it was re-written."""
    try:
        with open(path, "r+", encoding="utf-8", newline="") as f:
            f.seek(0)
            f.write(original)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception:  # not BaseException: a second Ctrl-C must propagate
        return False


def _write_in_place(path: Path, data: str, original: str) -> None:
    """Overwrite the EXISTING file (same inode), so ACLs, xattrs, mode and hard
    links survive by construction (#3419).  No temp file, no os.replace, no chmod.
    On any failure the original bytes are restored in place and WriteFailure is raised.
    """
    began = False  # becomes True just before the first write: until then the file is untouched
    try:
        with open(path, "r+", encoding="utf-8", newline="") as f:
            began = True
            f.seek(0)
            f.write(data)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
    except BaseException as e:
        restored = _restore_in_place(path, original) if began else None
        if not isinstance(e, Exception):
            raise
        if restored is None:
            outcome = "file untouched"
        elif restored:
            outcome = "original bytes restored"
        else:
            outcome = "original bytes NOT restored: file may be damaged"
        raise WriteFailure(f"{e} ({outcome})") from e


def _read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", help="warm-briefing path (default: $ENGRAM_HOME/warm-briefing.md)")
    ap.add_argument("--dry-run", action="store_true", help="compute and check, write nothing")
    args = ap.parse_args(argv)

    if args.file:
        path = Path(args.file)
    else:
        home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
        path = Path(home) / "warm-briefing.md"

    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            old = f.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        return 2

    try:
        new, info = erase_session_block(old)
    except DriftStop as e:
        print(str(e), file=sys.stderr)
        return 3
    except PostconditionError as e:
        print(f"post-condition failure: {e}. File untouched.", file=sys.stderr)
        return 4

    if new == old:
        print("already bare: no change")
        return 0

    summary = (
        f"kept {info['kept']} chars above the anchor (byte-identical), "
        f"removed {info['removed_chars']} chars / {info['removed_lines']} lines"
    )
    if args.dry_run:
        print(f"dry-run: would erase: {summary}")
        return 0
    try:
        _write_in_place(path, new, old)
    except OSError as e:
        print(f"error: cannot write {path}: {e}", file=sys.stderr)
        return 2
    # Read-back: the bytes on disk must be exactly the text we meant to write.
    try:
        written = _read_text(path)
    except (OSError, UnicodeDecodeError) as e:
        written = f"<unreadable: {e}>"
    if written != new:
        restored = _restore_in_place(path, old)
        print(
            f"post-condition failure: read-back of {path} differs from the intended text; "
            f"original bytes {'restored' if restored else 'NOT restored: file may be damaged'}.",
            file=sys.stderr,
        )
        return 4
    print(f"erased: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
