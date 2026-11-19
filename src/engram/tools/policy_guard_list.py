"""Parse the guard list out of PR-MERGE-POLICY "Owner merge" item 4 (#4487, #4358).

The merge policy's Owner-merge item 4 names the guard tests and scanners the
merge seat runs on every PR.  Two consumers read it: the existence check in
``tests/test_policy_guard_list_4487.py`` and the selection tool
``run_touched_tests.py`` (its guard set).  The parser lives here so there is one
copy.

Only the backticked ``tests/*.py`` and ``tools/*.py`` paths inside item 4 count,
and only item 4 of the "Owner merge" section.  A reformatted document never
passes by finding zero paths: a missing anchor raises ``PolicyAnchorError``
naming it.  ``PolicyAnchorError`` subclasses ``AssertionError`` so callers that
caught the old ``assert`` keep working, but it is raised explicitly, so it
survives ``python -O``.
"""

from __future__ import annotations

import re

HEADING = "## Owner merge"
ITEM4_START = re.compile(r"^4\. \*\*The seat's own run")
ITEM5_START = re.compile(r"^5\. \*\*The PR's changed")
PATH = re.compile(r"`((?:tests|tools)/[A-Za-z0-9_.\-]+\.py)`")


class PolicyAnchorError(AssertionError):
    """The policy document lacks an anchor the parser needs (named in the text)."""


def item4_paths(text: str) -> list[str]:
    """Return the unique tests/ and tools/ paths named in Owner-merge item 4."""
    lines = text.splitlines()
    head = next((i for i, ln in enumerate(lines) if ln.startswith(HEADING)), None)
    if head is None:
        raise PolicyAnchorError(f"anchor missing: no line starting {HEADING!r}")
    end = len(lines)
    for i in range(head + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    section = lines[head + 1 : end]
    start = next((i for i, ln in enumerate(section) if ITEM4_START.match(ln)), None)
    if start is None:
        raise PolicyAnchorError(
            "anchor missing: no item-4 start line matching '4. **The seat's own run' "
            f"inside the section {HEADING!r}"
        )
    stop = next(
        (i for i in range(start + 1, len(section)) if ITEM5_START.match(section[i])),
        None,
    )
    if stop is None:
        raise PolicyAnchorError(
            "anchor missing: no item-5 line matching '5. **The PR's changed' "
            "after item 4"
        )
    span = "\n".join(section[start:stop])
    paths = list(dict.fromkeys(PATH.findall(span)))
    if not paths:
        raise PolicyAnchorError(
            "the item-4 span held no paths (no backticked tests/ or tools/ .py names)"
        )
    return paths
