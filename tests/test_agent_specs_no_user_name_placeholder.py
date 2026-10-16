"""Shipped agent specs must not carry unsubstituted {{TOKEN}} placeholders (#3121).

The build does not template plugin-shipped agents, so a token like
{{USER_NAME}} would reach the agent verbatim and make literal paths like
ask-{{USER_NAME}}.md unusable.
"""

from __future__ import annotations

import re
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1] / "src" / "engram" / "agents"
DREAM_MASTER = AGENTS_DIR / "claude" / "engram-dream-master.md"
PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")


def test_no_unsubstituted_placeholder_in_agent_specs():
    scanned = 0
    offenders = {}
    for path in sorted(AGENTS_DIR.rglob("*")):
        if path.is_file():
            scanned += 1
            found = PLACEHOLDER_RE.findall(path.read_text(encoding="utf-8"))
            if found:
                offenders[str(path.relative_to(AGENTS_DIR))] = sorted(set(found))
    assert scanned >= 1, f"no agent spec files found under {AGENTS_DIR}"
    assert not offenders, f"unsubstituted placeholders in agent specs: {offenders}"


def test_dream_master_defines_primary_user():
    text = DREAM_MASTER.read_text(encoding="utf-8")
    assert "<primary_user>" in text
    assert "`primary_user` key in `~/.engram/config.json`" in text
