"""Keep the Agent-tool allowlist aligned with shipped ENGRAM fairy specs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from deploy.tool_policy_facts import ALLOWED_FAIRIES


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SPECS = REPO_ROOT / "src" / "engram" / "agents" / "claude"
# These three are the hand-synced surfaces governed by
# deploy.tool_policy_facts.SURFACES. Keep that contract at three; the
# component below is independently rendered from the same source policy.
HAND_SYNCED_ALLOWLIST_SURFACES = [
    REPO_ROOT / "src" / "engram" / "templates" / "template.CLAUDE.md",
    REPO_ROOT / "deploy" / "autostart" / "boot-charge-default.md",
    REPO_ROOT / "deploy" / "autostart" / "boot-charge-quiet.md",
]
EXPLICIT_ROSTER_SURFACES = [
    *HAND_SYNCED_ALLOWLIST_SURFACES,
    REPO_ROOT
    / "src"
    / "engram"
    / "templates"
    / "components"
    / "fairy-tool-policy-claude-code.md",
    REPO_ROOT
    / "src"
    / "build"
    / "instruction_manifestation"
    / "components"
    / "tool-policy-claude.md",
]


def _explicit_allowlist_roster(text: str) -> set[str]:
    """Extract the parenthesized roster from the Agent-tool grant itself."""
    grant = re.search(r"`?Agent`?-tool dispatch of (?:the )?`engram-\*` fairies", text)
    assert grant, "missing Agent-tool allowlist grant"

    roster_match = re.search(r"\(([^()]*)\)", text[grant.end() :])
    assert roster_match, "allowlist grant must include an explicit parenthesized roster"
    return set(re.findall(r"\bengram-[a-z0-9-]+\b", roster_match.group(1)))


def _shipped_roster() -> set[str]:
    return {path.stem for path in AGENT_SPECS.glob("engram-*.md")}


def _fact_table_roster() -> set[str]:
    """Return canonical engram-* ids recorded in the policy fact table."""
    return {
        alias
        for aliases in ALLOWED_FAIRIES.values()
        for alias in aliases
        if re.fullmatch(r"engram-[a-z0-9-]+", alias)
    }


def test_fact_table_allowed_fairies_matches_shipped_claude_agent_specs():
    shipped_roster = _shipped_roster()
    fact_table_roster = _fact_table_roster()

    assert fact_table_roster == shipped_roster, (
        "deploy/tool_policy_facts.py:ALLOWED_FAIRIES drifted from "
        "src/engram/agents/claude/engram-*.md: "
        f"facts={sorted(fact_table_roster)!r}, shipped={sorted(shipped_roster)!r}"
    )


@pytest.mark.parametrize("surface", EXPLICIT_ROSTER_SURFACES, ids=lambda path: path.name)
def test_explicit_agent_allowlist_matches_shipped_claude_agent_specs(surface: Path):
    shipped_roster = _shipped_roster()
    explicit_roster = _explicit_allowlist_roster(surface.read_text(encoding="utf-8"))

    assert explicit_roster == shipped_roster, (
        f"{surface.relative_to(REPO_ROOT)} allowlist drifted from "
        "src/engram/agents/claude/engram-*.md: "
        f"explicit={sorted(explicit_roster)!r}, shipped={sorted(shipped_roster)!r}"
    )
