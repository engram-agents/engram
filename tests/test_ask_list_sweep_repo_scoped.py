"""engram-sleep Step 4's ask-list sweep must resolve PR/issue references
against the repo they actually belong to, never the cwd checkout (alpha#3188).

A bare `#N` has no repo to check against: `gh pr view "$n"` / `gh issue view
"$n"` silently resolve it against whichever repo happens to be the current
checkout. On a multi-repo ask-file, two repos can share a number -- the cwd
repo's own same-numbered PR/issue then answers in place of the real one, with
no error. Measured four separate times on the same four PR numbers
(engram-alpha#3188): a bare `#N` swept from engram-alpha's checkout reported
four live ai-portfolio PRs as MERGED.

The fix is mechanical: references are written `owner/repo#N`, the sweep's
extraction pattern only matches that shape, and `gh` is called with
`--repo` taken from the reference itself. A bare `#N` is therefore never
even a sweep candidate -- not "usually skipped", structurally unmatched.

These tests read the sweep's own documented regex out of SKILL.md (the
single source of truth an agent actually runs) and exercise it directly,
rather than asserting on prose alone -- a doc-only check that only verifies
*words* changed, not that the extraction *behavior* changed.
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
SLEEP_MD = REPO / "src/engram/skills/claude/engram-sleep/SKILL.md"


def _ask_list_sweep_section() -> str:
    text = SLEEP_MD.read_text(encoding="utf-8")
    marker = "**Deterministic external-state sweep (the prune gate).**"
    assert marker in text, "ask-list sweep section heading moved; this gate needs updating"
    end_marker = "**Walk the day's commit log**"
    assert end_marker in text, "ask-list sweep section's next bullet moved; this gate needs updating"
    start = text.index(marker)
    end = text.index(end_marker, start)
    return text[start:end]


def _extraction_pattern() -> re.Pattern[str]:
    """Pull the `grep -oE '...'` pattern out of the sweep's own bash block."""
    section = _ask_list_sweep_section()
    m = re.search(r"grep -oE '([^']+)'", section)
    assert m, "sweep's grep -oE pattern not found -- bash block reshaped?"
    return re.compile(m.group(1))


def test_extraction_pattern_matches_owner_repo_hash_n():
    pattern = _extraction_pattern()
    assert pattern.search("engram-agents/engram#3188")
    assert pattern.search("LeiShi/ai-portfolio#83")
    # bare "alpha#2400" (no owner/) must NOT match -- "owner/repo#N" is required
    assert not pattern.fullmatch("alpha#2400")


def test_extraction_pattern_does_not_match_bare_hash_n():
    """The bug: a bare #N used to be swept and resolved against the cwd repo."""
    pattern = _extraction_pattern()
    for bare in ("#83", "#84", "#1650", "#3188"):
        assert not pattern.search(bare), (
            f"extraction pattern matched bare {bare!r} -- a bare #N must never "
            "be a sweep candidate (alpha#3188)"
        )
    # a real mixed entry: one owner/repo#N reference alongside a bare #N in the
    # same string -- the pattern must extract ONLY the qualified one.
    prose = (
        "LeiShi/ai-portfolio#83 is still OPEN; #1650 is the sibling "
        "bug in baton gc (alpha#2400 is unrelated)."
    )
    matches = pattern.findall(prose)
    assert matches == ["LeiShi/ai-portfolio#83"], (
        f"expected only the qualified reference to be extracted from {prose!r}, "
        f"got {matches}"
    )


def test_gh_calls_are_repo_scoped_from_the_reference():
    section = _ask_list_sweep_section()
    assert "--repo \"$repo\"" in section, (
        "gh pr view / gh issue view calls must pass --repo \"$repo\" taken "
        "from the matched reference, not rely on the cwd checkout"
    )
    assert section.count("--repo \"$repo\"") >= 2, (
        "both the gh pr view and gh issue view fallback calls must be --repo-scoped"
    )


def test_bare_reference_contract_is_stated_explicitly():
    section = _ask_list_sweep_section()
    assert "never a bare `#N`" in section
    assert "never prune-eligible, unconditionally" in section
    assert "UNKNOWN (no repo)" in section


def test_old_unscoped_pattern_is_gone():
    """Regression guard: the old bug's exact pattern must not reappear."""
    section = _ask_list_sweep_section()
    assert "grep -oE '#[0-9]+'" not in section
    assert "tr -d '#'" not in section
