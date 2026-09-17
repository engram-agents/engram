"""Regression guard: the MCP-schema load-time parenthetical must never return.

`(loaded once per session)` was a 4-week-old drift-prone regression (commit
`3eaf3718`, PR #2164, 2026-08-21) that replaced the correct-at-the-time
`(loaded at each tool-call)` wording under a title claiming to fix a
mis-statement. Lei's ruling (2026-09-17): neither wording belongs in
CLAUDE.md -- MCP-schema load-time mechanics are drift-prone detail, not a
rule, and should not be stated at all.

This asserts the literal string is gone from every file under
`src/engram/templates/` -- a directory-wide count, not a presence check
relative to any one line, so nothing here can go stale if a line moves.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "src" / "engram" / "templates"

BANNED_PHRASE = "loaded once per session"


def _count_occurrences(root: Path) -> int:
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        total += text.count(BANNED_PHRASE)
    return total


def test_no_mcp_schema_load_time_claim_anywhere_in_templates():
    count = _count_occurrences(TEMPLATES_DIR)
    assert count == 0, (
        f"found {count} occurrence(s) of {BANNED_PHRASE!r} under "
        f"{TEMPLATES_DIR} -- this drift-prone load-time claim was ruled "
        "out of scope for CLAUDE.md (Lei, 2026-09-17); delete it, don't "
        "reword it."
    )
