"""Baton/board markdown transforms — the pure (text → text) layer.

The single source of truth for parsing + editing a baton/board project's
frontmatter + turn-log. Relocated from ``tools/baton.py`` (whose ``cmd_*`` write
bodies move into :mod:`coordination.projects`) and de-duplicated with the parsing
``FileStore`` previously carried inline — one grammar, one definition, so the two
can't drift (the validator-drift class Borges flagged).

Everything here is pure: no I/O, no seq, no clock dependency except ``now_iso``.
The store does the atomic write + seq-embed; the writer-fns in
:mod:`coordination.projects` compose these transforms with the store + allocator.

Byte-fidelity with the original ``baton.py`` helpers is load-bearing: a relocated
write must produce the SAME on-disk text as the CLI did, or live batons drift.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Frontmatter grammar — identical to the original tools/baton.py + the inline
# copy FileStore carried, now unified here.
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):[ \t]*(.*)$", re.MULTILINE)

# ---------------------------------------------------------------------------
# Client-text hygiene (#3039 U2b round 4)
#
# Every write-body interpolates client strings into the frontmatter / turn log.
# ``parse_frontmatter`` lets the LAST duplicate key win and the store reads files
# in text mode (CR and CRLF become LF), so an interior line break in a client
# string could write a second ``phase:`` line. The set below is every line-break
# and control character we refuse to carry into a baton file: all C0 controls
# except TAB, DEL, the C1 controls (incl. NEL) and the two Unicode line /
# paragraph separators. (Measured: only LF and CR actually split frontmatter
# lines today; the rest are covered so a parser or reader change cannot widen the
# splitter silently.)
# ---------------------------------------------------------------------------
BREAK_OR_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f  ]+")


class UnsafeText(ValueError):
    """A client string carried a line-break / control character toward a baton file.

    The route layer refuses these before any lock / seq / write (422 naming the
    route and field); this exception is the LAST-RESORT backstop raised from the
    write layer itself (``update_frontmatter`` / ``append_turn_log`` / ``init``)
    so a future writer that forgets the route-layer sanitiser fails loudly
    instead of corrupting a baton.
    """


class DuplicateFrontmatterKeys(ValueError):
    """The file's frontmatter repeats a key the gates trust (see GUARDED_KEYS)."""

    def __init__(self, keys: list) -> None:
        self.keys = list(keys)
        super().__init__(
            f"baton file has duplicate frontmatter key(s) {', '.join(self.keys)}; repair it"
        )


def has_break_or_control(value: str) -> bool:
    """True if ``value`` contains any line-break / control character (TAB excepted)."""
    return bool(BREAK_OR_CONTROL_RE.search(value))


def normalize_free_text(value: str) -> str:
    """FREE-TEXT fields: every run of line-break / control characters becomes ONE
    space, then the result is stripped. Quotes and backslashes are untouched."""
    return BREAK_OR_CONTROL_RE.sub(" ", value).strip()


def assert_single_line(value, label: str) -> None:
    """Backstop: raise :class:`UnsafeText` if ``value`` would span (or look like it
    spans) more than one frontmatter line."""
    if has_break_or_control(str(value)):
        raise UnsafeText(
            f"{label} carries a line-break or control character; refusing to write it "
            "into a baton file (write-layer backstop)"
        )


# The frontmatter keys every authority decision reads. A repeat of any of them is
# refused by the gates ("repair it") -- ``parse_frontmatter`` keeps its own
# last-wins semantics untouched.
GUARDED_KEYS = ("phase", "turn", "github", "colleague", "author", "status", "participants")


def frontmatter_duplicate_keys(raw: str) -> list:
    """Keys (lower-cased, sorted) that appear MORE THAN ONCE in the frontmatter of ``raw``.

    Mirrors :func:`parse_frontmatter`'s grammar (same frontmatter match, same
    field regex, keys case-folded) but keeps what ``parse_frontmatter`` throws
    away: the repeats. ``[]`` for no/malformed frontmatter or no repeats.
    """
    m = FRONTMATTER_RE.match(raw)
    if not m:
        return []
    seen: dict = {}
    for field_m in FRONTMATTER_FIELD_RE.finditer(m.group(1)):
        key = field_m.group(1).strip().lower()
        seen[key] = seen.get(key, 0) + 1
    return sorted(k for k, n in seen.items() if n > 1)


def guarded_duplicate_keys(raw: str) -> list:
    """The subset of :func:`frontmatter_duplicate_keys` the gates trust (GUARDED_KEYS)."""
    return [k for k in frontmatter_duplicate_keys(raw) if k in GUARDED_KEYS]


# The turn-log section header + line shape the baton-flip monitor parses
# (``- <iso> <from> → <to>: <reason>``).
TURN_LOG_HEADER = "## Turn log"


def now_iso() -> str:
    """Current UTC time as ISO-8601 with a ``Z`` suffix (baton timestamp format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return ``(fields, body)`` from a frontmatter markdown file.

    Fields are lowercased keys → stripped string values. On no/malformed
    frontmatter returns ``({}, text)``. (Was ``baton._parse_frontmatter`` /
    ``store_file._parse_frontmatter`` — one definition now.)
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fields: dict = {}
    for field_m in FRONTMATTER_FIELD_RE.finditer(m.group(1)):
        fields[field_m.group(1).strip().lower()] = field_m.group(2).strip()
    return fields, text[m.end():]


def update_frontmatter(text: str, updates: dict) -> str:
    """Apply ``updates`` to frontmatter fields, returning the full updated text.

    Replaces values in place (preserving field order + the body). A key not
    already present is appended before the closing ``---``. On no frontmatter the
    text is returned unchanged (the caller handles that case). Mirrors
    ``baton._update_frontmatter`` exactly.
    """
    # Last-resort backstop (round 4): the route layer sanitises client text before
    # it gets here; a writer that forgot must fail loudly, not corrupt the file.
    for key, val in updates.items():
        assert_single_line(key, "frontmatter key")
        assert_single_line(val, f"frontmatter value for {key!r}")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return text
    body = text[m.end():]
    found_keys = set()
    new_lines = []
    for line in m.group(1).split("\n"):
        fm = FRONTMATTER_FIELD_RE.match(line)
        if fm:
            key = fm.group(1).strip().lower()
            if key in updates:
                new_lines.append(f"{key}: {updates[key]}")
                found_keys.add(key)
                continue
        new_lines.append(line)
    for key, val in updates.items():
        if key not in found_keys:
            new_lines.append(f"{key}: {val}")
    new_block = "\n".join(new_lines)
    return f"---\n{new_block}\n---\n{body}"


def check_log_entry(entry: str) -> None:
    """Last-resort backstop (round 4) for a turn-log entry. An entry is one line
    plus, at most, further whole log lines (``\\n- <iso> ...`` -- the phase-stamp
    suffixes); anything else -- a stray control character, or a continuation line
    that is not itself a log line -- raises :class:`UnsafeText`."""
    for i, line in enumerate(entry.split("\n")):
        assert_single_line(line, "turn-log entry")
        if i and not line.startswith("- "):
            raise UnsafeText("turn-log entry continuation line is not a log line; refusing")


def append_turn_log(body: str, entry: str) -> str:
    """Append a turn-log ``entry`` to the body's ``## Turn log`` section.

    Creates the section if absent. Mirrors ``baton._append_turn_log`` exactly.
    ``entry`` is a full line, e.g. ``- 2026-06-26T15:05:26Z ariadne → borges: reason``.
    """
    check_log_entry(entry)
    if TURN_LOG_HEADER in body:
        return body.rstrip() + "\n" + entry + "\n"
    separator = "\n\n" if body.strip() else ""
    return body.rstrip() + separator + f"\n{TURN_LOG_HEADER}\n\n" + entry + "\n"


def reattach_frontmatter(updated_text: str, new_body: str) -> str:
    """Reattach the frontmatter block of ``updated_text`` to ``new_body``.

    The flip/close write path edits frontmatter (via :func:`update_frontmatter`,
    which returns full text) and SEPARATELY edits the body (via
    :func:`append_turn_log`); this stitches the updated frontmatter back onto the
    new body. Mirrors the reconstruction in ``baton.cmd_flip`` / ``_close_baton``:
    take everything up to the end of the frontmatter match, strip trailing
    newlines, then ``\\n`` + the new body. On no frontmatter returns
    ``updated_text`` unchanged (matching the CLI's fallback).
    """
    m = FRONTMATTER_RE.match(updated_text)
    if not m:
        return updated_text
    return updated_text[:m.end()].rstrip("\n") + "\n" + new_body
