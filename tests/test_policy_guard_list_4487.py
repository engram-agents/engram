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
- C5. The message the existence check prints names a missing path, only that
  one, and the document; it is None when nothing is missing.
- C6. A later section's item 4 is not read when Owner merge has none.
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


def _missing_message(paths: list[str], root: Path) -> str | None:
    """The exact failure message for missing paths, or None when all exist."""
    missing = _missing(paths, root)
    if not missing:
        return None
    return (
        "Owner-merge item 4 in docs/internal/PR-MERGE-POLICY.md names files that "
        "are not in the tree: " + ", ".join(missing)
    )


def _real_paths() -> list[str]:
    return _item4_paths(POLICY.read_text(encoding="utf-8"))


def test_every_item4_path_exists():
    msg = _missing_message(_real_paths(), REPO_ROOT)
    assert msg is None, msg


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
    message = _missing_message(paths, tmp_path)
    assert message is not None
    assert "tests/test_bogus_one.py" in message
    assert "tests/test_real_one.py" not in message
    assert "PR-MERGE-POLICY.md" in message
    (tmp_path / "tests" / "test_bogus_one.py").write_text("")
    assert _missing_message(paths, tmp_path) is None


_SYNTH_NO_ITEM4_HERE = """\
## Owner merge
3. **Third item** `tests/test_not_a_real_file_3.py`
5. **The PR's changed or added tests** `tests/test_not_a_real_file_5.py`

## Later section
4. **The seat's own run, in a later section** `tests/test_not_a_real_file_9.py`
5. **The PR's changed, in a later section**
"""


def test_a_later_sections_item4_is_not_read_when_owner_merge_has_none():
    with pytest.raises(AssertionError) as exc:
        _item4_paths(_SYNTH_NO_ITEM4_HERE)
    assert "item-4 start" in str(exc.value)


def test_real_item4_names_the_tools_scanners_too():
    paths = _real_paths()
    for expected in ("tools/scan-leaks.py", "tools/test_id_set_check.py"):
        assert expected in paths, f"item 4 no longer yields {expected}"


def test_a_directory_with_a_listed_name_does_not_count_as_a_file(tmp_path):
    (tmp_path / "tests" / "test_dirlike.py").mkdir(parents=True)
    message = _missing_message(["tests/test_dirlike.py"], tmp_path)
    assert message is not None
    assert "tests/test_dirlike.py" in message


_SYNTH_TWICE = """\
## Owner merge
4. **The seat's own run** `tests/test_twice.py`
   and again here `tests/test_twice.py`
5. **The PR's changed or added tests**
"""


def test_a_path_named_twice_is_returned_once():
    assert _item4_paths(_SYNTH_TWICE) == ["tests/test_twice.py"]
