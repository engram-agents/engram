"""Structural harness-order guard for the shared ENGRAM loop skill (#2508)."""
from __future__ import annotations

from pathlib import Path


SKILL = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "skills"
    / "claude"
    / "engram-loop"
    / "SKILL.md"
)


def test_harness_selection_precedes_every_claude_marker_example():
    """A Codex reader must route to its transaction before the Claude
    scheduleWakeup marker is presented as the numbered Step 1 default."""
    text = SKILL.read_text(encoding="utf-8")
    select = text.index("## Select the harness **before any loop action**")
    codex = text.index("## Codex pacer")
    step_zero = text.index("## Step 0")
    step_one = text.index("## Step 1 —")
    claude_marker = text.index('"pacer": "scheduleWakeup"')

    assert select < codex < step_zero < step_one < claude_marker
    assert "Do **not** continue into the numbered Claude steps." in text[select:step_zero]
