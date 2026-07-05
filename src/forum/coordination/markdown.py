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
FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):\s*(.*)$", re.MULTILINE)

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


def append_turn_log(body: str, entry: str) -> str:
    """Append a turn-log ``entry`` to the body's ``## Turn log`` section.

    Creates the section if absent. Mirrors ``baton._append_turn_log`` exactly.
    ``entry`` is a full line, e.g. ``- 2026-06-26T15:05:26Z ariadne → borges: reason``.
    """
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
