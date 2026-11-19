"""template_render — canonical home for CLAUDE.md identity-surface render logic.

Shared by bootstrap.py (install-time rendering) and the drift-check tool
(tools/check-claude-md-drift.py), so the render-source list cannot drift
between the two call sites.

No side-effects at import time: no env reads, no file system writes, no
database access.  Safe to import from any context — including test helpers
and the drift tool — without triggering bootstrap.py's module-level env
requirements.

History
-------
Prior to engram-alpha#2067, the Claude ``~/.claude/CLAUDE.md`` render folded
two source files together: ``template.CLAUDE.md`` and
``compact-instructions.md`` (the latter inserted at a ``{{COMPACT_INSTRUCTIONS}}``
marker via ``fold_compact_instructions()``). #2067 removed the
``## Compact Instructions`` section from the Claude template entirely — see
``docs/compaction-continuity.md`` for why — so the Claude render is now a
single-file read. ``compact-instructions.md`` still ships; it is now the
source for the **Codex** compact-prompt path only (``bootstrap.py`` writes
it to ``~/.engram/codex-compact-prompt.md`` ($ENGRAM_HOME) and wires it into
``~/.codex/config.toml`` as ``experimental_compact_prompt_file``) and no
longer feeds the Claude render at all.

Exposed constants
-----------------
CLAUDE_RENDER_SOURCES : list[str]
    Canonical list of template file names (relative to the templates dir)
    that make up the installed CLAUDE.md.  The CI guard
    (tests/test_upgrade_drift_sources.py) cross-checks this list against the
    engram-upgrade SKILL.md so that a future extraction of another fragment
    cannot silently re-open the drift gap.

Exposed functions
-----------------
render_identity_surface(templates_dir) -> str
    Read template.CLAUDE.md from templates_dir and return its raw text.
    Other {{...}} placeholders (AGENT_NAME, seed-IDs, ENGRAM_HOME, etc.) are
    left untouched on purpose: the drift tool renders two refs with
    identical placeholder sets so the per-install IDs cancel out and only
    structural template changes surface in the diff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Files (relative to src/engram/templates/) that make up the installed
#: ~/.claude/CLAUDE.md.  The CI guard test_upgrade_drift_sources.py
#: cross-checks every entry here against the engram-upgrade SKILL.md's
#: drift-watch section so no future extraction can silently widen the gap.
CLAUDE_RENDER_SOURCES: list[str] = [
    "template.CLAUDE.md",
]

#: The template each recorded CLAUDE.md profile follows (config.json key
#: ``claude_md_profile``; no record = full). Mirrors the table in
#: src/build/engine/flows.py (the upgrade flow); tests/test_local_claude_md_profile.py
#: pins that both read the same answer for every config shape.
CLAUDE_MD_PROFILE_KEY = "claude_md_profile"
CLAUDE_MD_PROFILE_SOURCES: dict[str, list[str]] = {
    "full": CLAUDE_RENDER_SOURCES,
    "local": ["template.CLAUDE.local.md"],
}
#: The one template file each profile renders from (always a member of that
#: profile's CLAUDE_MD_PROFILE_SOURCES list; the list may widen, this name does not move).
CLAUDE_MD_PROFILE_TEMPLATE: dict[str, str] = {
    "full": "template.CLAUDE.md",
    "local": "template.CLAUDE.local.md",
}
PROFILE_UNDETERMINED = "undetermined"


def recorded_claude_md_profile(engram_home: str) -> str:
    """The profile a home recorded: "full", "local" or "undetermined".

    No home, no config.json, no key, null or "full" is full. An unreadable
    config.json, a non-object one, or any other value is undetermined, so no
    template is compared (a guess could compare the wrong one).
    """
    if not engram_home:
        return "full"
    try:
        with open(os.path.join(engram_home, "config.json"), encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        return "full"
    except (OSError, ValueError):
        return PROFILE_UNDETERMINED
    if not isinstance(config, dict):
        return PROFILE_UNDETERMINED
    value = config.get(CLAUDE_MD_PROFILE_KEY)
    if value is None or value == "full":
        return "full"
    if value == "local":
        return "local"
    return PROFILE_UNDETERMINED


# ---------------------------------------------------------------------------
# High-level render helper
# ---------------------------------------------------------------------------


def render_identity_surface(templates_dir: Path, profile: str = "full") -> str:
    """Render the CLAUDE.md identity surface from a templates directory.

    Reads ``template.CLAUDE.md`` from *templates_dir* and returns its raw
    text.  All ``{{...}}`` placeholders are intentionally left unresolved.
    When this function is called twice on two different git refs' template
    directories, the per-install substitution values (seed-IDs, ENGRAM_HOME,
    AGENT_NAME, …) are identical in both renders and cancel out in the diff —
    only structural template changes surface.

    Parameters
    ----------
    templates_dir:
        Path to a directory that contains the profile's template
        (``template.CLAUDE.md`` for full, ``template.CLAUDE.local.md`` for local).
    profile:
        "full" (default) or "local".

    Returns
    -------
    str
        The raw template text, placeholders intact.
    """
    template_path = templates_dir / CLAUDE_MD_PROFILE_TEMPLATE[profile]
    return template_path.read_text(encoding="utf-8")
