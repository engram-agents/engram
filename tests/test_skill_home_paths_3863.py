"""No command-position hard-coded ENGRAM home path in four shipped files [part of #3863].

Lei's ruling (2026-10-17): the deployment uses ENGRAM_HOME wherever possible,
because the install allows a custom engram folder. `tools/home_path_gate.py`
is a FILE-level ratchet (a file that mentions ENGRAM_HOME once counts clean),
so it cannot see a skill that names `ENGRAM_HOME` in one place and pastes
`~/.engram/...` in another. This test is the line-level control for the three
skills PR 2 of #3863 fixes.

DEFINITION (precise). A *code line* is any line inside a fenced block (``` or
~~~ fences) plus every inline code span (`...`) on a non-fenced line. A code
line VIOLATES when it contains `~/.engram`, `$HOME/.engram`, `${HOME}/.engram`
or `"$HOME"/.engram` (with `.engram` as the whole folder name: `.engram-x` and
`.engram_x` do not count) that is NOT immediately preceded by the text
`${ENGRAM_HOME:-`. So `${ENGRAM_HOME:-$HOME/.engram}/x` passes and a bare
`~/.engram/x` is flagged.

KNOWN BOUND. Prose outside backticks is not scanned (it is not something a
reader pastes); the rule for it is in the PR's decision log, not enforced here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "src" / "engram" / "skills" / "claude"

TARGETS = [
    "engram-health-exam",
    "engram-upgrade",
    "engram-sleep",
]

_HOME_PATH = re.compile(r"(?:~|\$HOME|\$\{HOME\}|\"\$HOME\")/\.engram(?![\w-])")
_ALLOWED_PREFIX = "${ENGRAM_HOME:-"
_FENCE = re.compile(r"^\s*(```|~~~)")
_SPAN = re.compile(r"`([^`\n]+)`")


def _code_chunks(text):
    """Yield (lineno, chunk) for every fenced line and inline code span."""
    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            yield n, line
        else:
            for m in _SPAN.finditer(line):
                yield n, m.group(1)


def flagged_lines(text):
    """[(lineno, chunk)] of code chunks holding a hard-coded home path."""
    out = []
    for n, chunk in _code_chunks(text):
        for m in _HOME_PATH.finditer(chunk):
            if not chunk[: m.start()].endswith(_ALLOWED_PREFIX):
                out.append((n, chunk.strip()))
                break
    return out


@pytest.mark.parametrize("skill", TARGETS)
def test_skill_has_no_command_position_hardcoded_home(skill):
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    bad = flagged_lines(text)
    assert not bad, (
        f"{skill}/SKILL.md hard-codes the ENGRAM home in {len(bad)} code line(s); "
        f"use ${{ENGRAM_HOME:-$HOME/.engram}} (#3863):\n"
        + "\n".join(f"  L{n}: {c}" for n, c in bad)
    )


class TestPlantedDefinition:
    """The detector itself: bare spellings are flagged, the env form is not."""

    def test_bare_tilde_in_fence_is_flagged(self):
        text = "```bash\nls ~/.engram/x\n```\n"
        assert flagged_lines(text) == [(2, "ls ~/.engram/x")]

    @pytest.mark.parametrize(
        "spelling",
        ["$HOME/.engram/x", "${HOME}/.engram/x", '"$HOME"/.engram/x', "~/.engram"],
    )
    def test_each_spelling_flagged_in_fence_and_span(self, spelling):
        assert flagged_lines(f"```\ncat {spelling}\n```\n")
        assert flagged_lines(f"see `{spelling}` here\n")

    def test_env_form_is_not_flagged(self):
        text = '```bash\nls "${ENGRAM_HOME:-$HOME/.engram}/x"\n```\nuse `${ENGRAM_HOME:-$HOME/.engram}/y`\n'
        assert flagged_lines(text) == []

    def test_env_form_does_not_excuse_a_second_bare_path_on_the_line(self):
        text = '```\ncp "${ENGRAM_HOME:-$HOME/.engram}/a" ~/.engram/b\n```\n'
        assert len(flagged_lines(text)) == 1

    def test_prose_outside_backticks_is_not_scanned(self):
        assert flagged_lines("lives in ~/.engram by default\n") == []

    def test_sibling_folder_names_are_not_the_home(self):
        assert flagged_lines("```\nls ~/.engram-gemini/x ~/.engram_home\n```\n") == []
