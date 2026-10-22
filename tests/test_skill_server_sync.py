"""RETIRED: SKILL.md ↔ server.py drift gate (retired in #1149).

The root src/engram/SKILL.md was the canonical tool index for MCP tools. It
has been retired (#1149) in favor of the MCP tool docstrings, the engram-*
skills, and the CLAUDE.md disciplines. The file was moved to docs/internal/archive/SKILL.md
for historical reference and is no longer shipped.

Prior incidents this test existed to prevent:
- ob_0966 (foundation-tier SKILL.md staleness — tool count 13 vs 24 vs actual ~30)
- ob_0967 (four-surface foundation rot — each surface drifts independently)
- 2026-05-11: engram_query_pattern shipped in PR #29 (2026-05-07) but SKILL.md
  had ZERO mention for 4 days until caught; same incident motivated
  this test.
- 2026-06-06: engram_add_evidence appeared in §5 prose (internal helper, not
  an MCP tool); the §4-only test was green throughout, surfaced by PR #892's
  scrub. test_no_phantom_tools_in_full_prose closes the gap (issue #893).

With the retirement of SKILL.md, the tool-discovery surface shifts to the MCP
tool docstrings (discoverable via ToolSearch and the MCP schema introspection
endpoint). This file is kept as a tombstone so the git history is legible.
All tests are unconditionally skipped.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.skip(
    reason=(
        "SKILL.md retired in #1149 — moved to docs/internal/archive/SKILL.md, "
        "no longer shipped. Tool documentation now lives in MCP tool "
        "docstrings and the engram-* skills."
    )
)


def test_skill_md_documents_every_server_tool() -> None:
    pass  # noqa: E701  (unconditionally skipped by pytestmark)


def test_no_phantom_tools_in_skill_md() -> None:
    pass


def test_allowlist_entries_have_reasons() -> None:
    pass


def test_no_phantom_tools_in_full_prose() -> None:
    pass
