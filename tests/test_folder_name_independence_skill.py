"""Folder-name independence, engram-sleep SKILL.md (S4, S5): a TEXT contract (pins wording, not agent behaviour).

REVISION 2: the default checkout name is `engram`; `engram-alpha` is the accepted old name.
  C4.1 the end-of-day `for repo in ...` loop lists both `~/engram` and `~/engram-alpha`, engram first.
  C5.1 the "Walk the day's commit log" command names the dev checkout as `~/engram`, with
       `~/engram-alpha` named as the accepted old name.
  C5.2 no `git -C ~/engram-alpha` occurrence without `~/engram` (the other name) on the same line.
  C5.3 the dream-master placeholder is name-neutral: no `<engram-alpha-repo-path>`; `<engram-repo-path>`.
"""
from __future__ import annotations

import re
from pathlib import Path

SKILL = (Path(__file__).resolve().parent.parent / "src" / "engram" / "skills" / "claude"
         / "engram-sleep" / "SKILL.md").read_text(encoding="utf-8")
NEW = r"~/engram(?![-\w])"


def test_C4_1_loop_lists_both_engram_first():
    loops = [ln for ln in SKILL.splitlines() if ln.startswith("for repo in ")]
    assert len(loops) == 1, loops
    ln = loops[0]
    assert re.search(NEW, ln) and "~/engram-alpha" in ln
    assert ln.index("~/engram ") < ln.index("~/engram-alpha")


def test_C5_1_commit_log_walk_names_both():
    lines = [ln for ln in SKILL.splitlines() if "Walk the day's commit log" in ln]
    assert len(lines) == 1
    assert "git -C ~/engram log --since=6am" in lines[0]
    assert "~/engram-alpha" in lines[0]


def test_C5_2_no_bare_alpha_git_c():
    for ln in SKILL.splitlines():
        if "git -C ~/engram-alpha" in ln:
            assert re.search(NEW, ln), ln


def test_C5_3_placeholder_name_neutral():
    assert "<engram-alpha-repo-path>" not in SKILL
    assert "<engram-repo-path>/tools/" in SKILL
