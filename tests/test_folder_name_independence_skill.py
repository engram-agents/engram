"""engram-sleep SKILL.md: wording of the checkout folder in the commands an agent runs (a text check).

`engram` is the default checkout name; `engram-alpha` is the accepted old name.
  - the end-of-day `for repo in ...` loop lists both `~/engram` and `~/engram-alpha`, engram first;
  - the "Walk the day's commit log" command is a loop over both candidates, so a seat holding both walks both;
  - no `git -C ~/engram-alpha` occurrence without `~/engram` (the other name) on the same line;
  - the dream-master placeholder is name-neutral: no `<engram-alpha-repo-path>`; `<engram-repo-path>`.
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
    # a loop over every existing candidate, like the C4.1 loop (a missing folder is silent via 2>/dev/null)
    assert "for repo in ~/engram ~/engram-alpha;" in lines[0]
    assert 'git -C "$repo" log --since=6am 2>/dev/null' in lines[0]
    assert lines[0].index("~/engram ") < lines[0].index("~/engram-alpha")


def test_C5_2_no_bare_alpha_git_c():
    for ln in SKILL.splitlines():
        if "git -C ~/engram-alpha" in ln:
            assert re.search(NEW, ln), ln


def test_C5_3_placeholder_name_neutral():
    assert "<engram-alpha-repo-path>" not in SKILL
    assert "<engram-repo-path>/tools/" in SKILL
