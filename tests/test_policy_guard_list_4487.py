"""Every file named in the merge policy's Owner-merge item 4 exists (#4487).

``docs/internal/PR-MERGE-POLICY.md``, section "Owner merge", item 4, lists the
guard tests and scanners the merge seat runs on every PR, and closes with
"a guard that moved is not a guard that ran".  Nothing checked that.  This
file reads the document, takes the ``tests/*.py`` and ``tools/*.py`` paths
named inside item 4 and only item 4, and asserts each one is on disk.

Contract (one cell each; stdlib only; it reads this document and names no module):

- C1. On the real document every path item 4 names exists; a failure names
  each missing path.
- C2. The real document yields at least 20 paths, and this file is one of them.
- C3. Paths named outside item 4 (other items, or a later section with its own
  "4." line) are not returned.
- C4. A document missing the section heading, the item-4 start or the item-5
  line fails loudly with the missing anchor named; an item 4 holding no paths
  fails too.  A reformatted document never passes by finding zero paths.
- C5. The existence check reports a missing path by name, and only that one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY = REPO_ROOT / "docs" / "internal" / "PR-MERGE-POLICY.md"

_HEADING = "## Owner merge"
_ITEM4_START = re.compile(r"^4\. \*\*The seat's own run")
_ITEM5_START = re.compile(r"^5\. \*\*The PR's changed")
_PATH = re.compile(r"`((?:tests|tools)/[A-Za-z0-9_.\-]+\.py)`")


def _item4_paths(text: str) -> list[str]:
    """Return the unique tests/ and tools/ paths named in Owner-merge item 4."""
    lines = text.splitlines()
    head = next((i for i, ln in enumerate(lines) if ln.startswith(_HEADING)), None)
    assert head is not None, f"anchor missing: no line starting {_HEADING!r}"
    end = len(lines)
    for i in range(head + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    section = lines[head + 1 : end]
    start = next((i for i, ln in enumerate(section) if _ITEM4_START.match(ln)), None)
    assert start is not None, (
        "anchor missing: no item-4 start line matching '4. **The seat's own run' "
        f"inside the section {_HEADING!r}"
    )
    stop = next(
        (i for i in range(start + 1, len(section)) if _ITEM5_START.match(section[i])),
        None,
    )
    assert stop is not None, (
        "anchor missing: no item-5 line matching '5. **The PR's changed' "
        "after item 4"
    )
    span = "\n".join(section[start:stop])
    paths = list(dict.fromkeys(_PATH.findall(span)))
    assert paths, "the item-4 span held no paths (no backticked tests/ or tools/ .py names)"
    return paths


def _missing(paths: list[str], root: Path) -> list[str]:
    return [p for p in paths if not (root / p).is_file()]


def _real_paths() -> list[str]:
    return _item4_paths(POLICY.read_text(encoding="utf-8"))


def test_every_item4_path_exists():
    paths = _real_paths()
    missing = _missing(paths, REPO_ROOT)
    assert not missing, (
        "Owner-merge item 4 in docs/internal/PR-MERGE-POLICY.md names files that "
        "are not in the tree: " + ", ".join(missing)
    )


def test_item4_extraction_is_not_empty_and_includes_this_file():
    paths = _real_paths()
    assert len(paths) >= 20, f"only {len(paths)} paths extracted from item 4"
    assert "tests/test_policy_guard_list_4487.py" in paths, (
        "item 4 does not name tests/test_policy_guard_list_4487.py"
    )


_SYNTH_OK = """\
## Intro
4. **The seat's own run, decoy before the section** `tests/test_not_a_real_file_0.py`
5. **The PR's changed decoy**

## Owner merge (title)
3. **Third item** `tests/test_not_a_real_file_3.py`
4. **The seat's own run, on a scratch merge**
   `tests/test_policy_guard_list_4487.py`
5. **The PR's changed or added tests** `tests/test_not_a_real_file_5.py`

## Later section
4. **The seat's own run, again** `tests/test_not_a_real_file_9.py`
5. **The PR's changed again**
"""


def test_paths_outside_item4_do_not_bind():
    assert _item4_paths(_SYNTH_OK) == ["tests/test_policy_guard_list_4487.py"]


@pytest.mark.parametrize(
    "text, anchor",
    [
        (
            "## Other\n4. **The seat's own run** `tests/a.py`\n5. **The PR's changed\n",
            "## Owner merge",
        ),
        (
            "## Owner merge\n3. **Third** `tests/a.py`\n5. **The PR's changed\n",
            "item-4 start",
        ),
        (
            "## Owner merge\n4. **The seat's own run** `tests/a.py`\n",
            "item-5",
        ),
        (
            "## Owner merge\n4. **The seat's own run** no paths here\n5. **The PR's changed\n",
            "held no paths",
        ),
    ],
    ids=["no-heading", "no-item4", "no-item5", "no-paths"],
)
def test_missing_anchors_fail_loudly(text, anchor):
    with pytest.raises(AssertionError) as exc:
        _item4_paths(text)
    assert anchor in str(exc.value)


def test_a_missing_path_is_reported_by_name(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_real_one.py").write_text("")
    paths = ["tests/test_real_one.py", "tests/test_bogus_one.py"]
    missing = _missing(paths, tmp_path)
    assert missing == ["tests/test_bogus_one.py"]
    message = ", ".join(missing)
    assert "tests/test_bogus_one.py" in message
    assert "tests/test_real_one.py" not in message
