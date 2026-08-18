"""Drift guard for the hand-synced TOOL POLICY block (3 surfaces).

Three surfaces in this repo carry a hand-maintained copy of the tool policy
(see deploy/tool_policy_facts.py's docstring for the full rationale):

  - src/engram/templates/template.CLAUDE.md
  - deploy/autostart/boot-charge-default.md
  - deploy/autostart/boot-charge-quiet.md

They legitimately differ in wording, so this is a FACT-TABLE check against
deploy/tool_policy_facts.py, not an exact-text-match check: every surface
must (a) contain the "BANNED" marker, (b) mention every banned tool token,
and (c) mention at least one alias per allowed fairy. deploy/tool_policy_facts.py
is the single source of truth for the tokens/aliases; this file only asserts
presence per surface. Variant shipped: PRESENCE-ONLY (see the fact module's
docstring for why the stricter proximity/polarity check was not shipped).


SCOPE BOUND (spec §1): this guard covers exactly the three surfaces in THIS
repo that hand-sync the TOOL POLICY block — src/engram/templates/
template.CLAUDE.md, deploy/autostart/boot-charge-default.md, and
deploy/autostart/boot-charge-quiet.md. Per-agent identity files
(~/.claude/CLAUDE.md) are deliberately OUT of scope: per-seat state, outside
every repo, wrong to read from a test on privacy as well as portability
grounds. A fourth in-repo copy appearing is a scope change, not a silent
addition — route it back to the spec author.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deploy.tool_policy_facts import (
    ALLOWED_FAIRIES,
    BANNED_MARKER,
    BANNED_TOOLS,
    SURFACES,
)

REPO_ROOT = Path(__file__).parent.parent


def _surface_text(rel_path: str) -> str:
    full = REPO_ROOT / rel_path
    assert full.exists(), f"tool-policy surface missing: {rel_path}"
    return full.read_text(encoding="utf-8")


@pytest.mark.parametrize("surface", SURFACES)
def test_surface_contains_banned_marker(surface: str) -> None:
    text = _surface_text(surface)
    assert BANNED_MARKER in text, (
        f"{surface} is missing the '{BANNED_MARKER}' marker — the tool "
        "policy block may have been edited out or reworded past recognition."
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_surface_contains_every_banned_tool(surface: str) -> None:
    text = _surface_text(surface)
    for token in BANNED_TOOLS:
        assert token in text, (
            f"{surface} is missing banned-tool token '{token}' — the tool "
            "policy surfaces have drifted out of sync. Compare against "
            "deploy/tool_policy_facts.py:BANNED_TOOLS and the other two "
            "surfaces, then re-sync by hand."
        )


@pytest.mark.parametrize("surface", SURFACES)
def test_surface_contains_every_allowed_fairy(surface: str) -> None:
    text = _surface_text(surface)
    for fairy_id, aliases in ALLOWED_FAIRIES.items():
        assert any(alias in text for alias in aliases), (
            f"{surface} is missing fairy '{fairy_id}' — none of its known "
            f"aliases {aliases!r} were found. The tool policy surfaces have "
            "drifted out of sync; compare against deploy/tool_policy_facts.py"
            ":ALLOWED_FAIRIES and the other two surfaces, then re-sync by hand."
        )


def test_no_fourth_surface_silently_exists() -> None:
    """Guard the SCOPE claim: exactly these three files carry the block.

    Not exhaustive (a truly new copy elsewhere in the repo wouldn't be
    caught by this), but pins down the three known surfaces so a future
    edit that renames/moves one of them fails loudly here rather than
    silently narrowing coverage to two.
    """
    for surface in SURFACES:
        assert (REPO_ROOT / surface).exists(), f"expected surface missing: {surface}"
    assert len(SURFACES) == 3
