"""The READMEs' "What else is in this repository" tables must match the tree.

Why: this repository publishes ENGRAM's whole development history, so a
casual visitor sees folders that are not ENGRAM (deploy/, docs/internal/,
.claude/ ...). The project owner's condition for publishing was that those
folders are explained up front, and that the explanation cannot go stale
when a new top-level folder lands.

Contract, checked for README.md (section "## What else is in this
repository") and README.zh.md (section "## 这个仓库里还有什么"):

1. Every top-level directory tracked by git (dot-directories included) has
   exactly one table row in that section; the first column is the folder in
   backticks with a trailing slash. A missing row fails and names the folder.
2. No row names a folder that is not a tracked top-level directory.
3. Rows count only between the section heading and the next "## " line. A
   folder mentioned elsewhere does not count. A missing heading fails.
4. Both READMEs list the same folders.
"""

import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HEADINGS = {
    "README.md": "## What else is in this repository",
    "README.zh.md": "## 这个仓库里还有什么",
}
ROW = re.compile(r"^\|\s*`([^`|]+/)`\s*\|")
FIX = (
    'Add a row for it to both READMEs\' "What else is in this repository" '
    "tables (README.md and README.zh.md)."
)


def tracked_top_dirs():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode("utf8")
    return {p.split("/", 1)[0] for p in out.split("\0") if "/" in p}


def table_rows(name):
    """Folder names (no trailing slash) of the rows inside the section."""
    lines = (ROOT / name).read_text(encoding="utf8").splitlines()
    heading = HEADINGS[name]
    start = next((i for i, ln in enumerate(lines) if ln.strip() == heading), None)
    assert start is not None, f'{name}: section heading "{heading}" is missing'
    rows = []
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        m = ROW.match(ln)
        if m:
            rows.append(m.group(1)[:-1])
    return rows


@pytest.mark.parametrize("name", ["README.md", "README.zh.md"])
def test_every_tracked_top_dir_has_exactly_one_row(name):  # C1 / C2
    counts = Counter(table_rows(name))
    for d in sorted(tracked_top_dirs()):
        assert counts[d] != 0, f"{name}: no row for top-level directory {d}/. {FIX}"
        assert counts[d] == 1, f"{name}: {d}/ has {counts[d]} rows, expected 1"


@pytest.mark.parametrize("name", ["README.md", "README.zh.md"])
def test_no_row_for_a_folder_that_is_not_tracked(name):  # C3
    stale = sorted(set(table_rows(name)) - tracked_top_dirs())
    assert not stale, (
        f"{name}: rows name folders that are not tracked top-level "
        f"directories: {[s + '/' for s in stale]}. Remove them."
    )


def test_both_readmes_list_the_same_folders():  # C5
    en, zh = set(table_rows("README.md")), set(table_rows("README.zh.md"))
    assert en == zh, (
        f"only in README.md: {sorted(en - zh)}; only in README.zh.md: {sorted(zh - en)}"
    )
