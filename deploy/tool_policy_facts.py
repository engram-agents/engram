"""Canonical fact table for the hand-synced TOOL POLICY block.

Three surfaces in this repo carry a hand-maintained copy of the tool policy
(banned harness tools, allowed engram-* fairies):

  - src/engram/templates/template.CLAUDE.md
  - deploy/autostart/boot-charge-default.md
  - deploy/autostart/boot-charge-quiet.md

They are NOT text-identical — the wording legitimately differs by surface
(template.CLAUDE.md writes in first person with informal fairy names like
"coder-fairies"; the boot charges write in second person with the full
hyphenated fairy names like "engram-coder-fairy"). So drift detection here is
a FACT-TABLE check, not an exact-text-match check: each surface must mention
every banned tool and every allowed fairy, using whatever alias the surface
actually uses for it. This module is the single source of truth for those
facts; tests/test_tool_policy_sync.py reads it and asserts presence per
surface. The surfaces govern this table's contents — if a surface's wording
changes, update the aliases here to match, don't invent new tokens that
don't actually appear anywhere.

Variant shipped: PRESENCE-ONLY, not proximity/polarity-checked.

A stricter "polarity" check was considered — verifying a banned token isn't
sitting next to an allowing word (allowed/permitted/encouraged) without an
intervening "not". In practice all three surfaces separate the BANNED and
ALLOWED paragraphs by clear boundary language ("a different/separate
mechanism, not another instance of the ban above"), so a naive proximity
window is more likely to misfire on that transition language than to catch
a real drift — a gate that cries wolf gets deleted. Presence-only is what's
shipped: every surface must contain the literal "BANNED" marker and every
banned/allowed token (by alias). This catches the real failure mode this
guard exists for (someone silently drops a fairy or a ban from one surface
while editing the others) without the false-positive risk of a proximity
heuristic tuned against only three known-good snapshots.
"""

from __future__ import annotations

# Tokens that must appear verbatim (case-sensitive) in every surface's tool
# policy block. Extracted directly from the surfaces — not invented.
BANNED_TOOLS: tuple[str, ...] = (
    "deep-research",
    "Workflow",
)

# The word every surface uses to flag the banned-tools paragraph. Checked
# independently of the BANNED_TOOLS tokens themselves, so a surface that
# still names "Workflow" but drops the "BANNED" framing is still caught.
BANNED_MARKER = "BANNED"

# Canonical fairy id -> aliases actually used across the three surfaces.
# The boot charges use the full hyphenated engram-* name; template.CLAUDE.md
# uses the shorter informal name. Every surface must contain at least one
# alias per fairy.
ALLOWED_FAIRIES: dict[str, tuple[str, ...]] = {
    "reviewer": ("engram-pr-reviewer", "reviewer-fairies"),
    "coder": ("engram-coder-fairy", "coder-fairies"),
    "batch-summary": ("engram-batch-summary-fairy", "summary-fairies"),
    "dream": ("engram-dream-fairy", "dream-fairies"),
    "dream-master": ("engram-dream-master",),
    "person-node": ("engram-person-node-fairy",),
}

# The three surfaces this fact table governs, relative to repo root.
SURFACES: tuple[str, ...] = (
    "src/engram/templates/template.CLAUDE.md",
    "deploy/autostart/boot-charge-default.md",
    "deploy/autostart/boot-charge-quiet.md",
)
