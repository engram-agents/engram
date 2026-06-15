"""template_render — canonical home for CLAUDE.md identity-surface fold logic.

Shared by bootstrap.py (install-time rendering) and the drift-check tool
(tools/check-claude-md-drift.py).  bootstrap.py imports the canonical
constant (COMPACT_BREADCRUMB_LINE) and source-list (CLAUDE_RENDER_SOURCES)
from here so those cannot drift, and the drift tool renders through this
module's fold_compact_instructions().  NOTE: bootstrap.py still performs an
*equivalent* inline fold via its own ``substitutions`` loop rather than
calling fold_compact_instructions() directly — equivalent today, but
converging them (or asserting their equivalence in a test) is tracked as a
follow-up (#1193).  Duplicating the constant/source-list, by contrast, would
re-create the drift class that tools/check-claude-md-drift.py is designed
to catch.

No side-effects at import time: no env reads, no file system writes, no
database access.  Safe to import from any context — including test helpers
and the drift tool — without triggering bootstrap.py's module-level env
requirements.

Exposed constants
-----------------
COMPACT_BREADCRUMB_LINE : str
    The exact breadcrumb comment line that sits above {{COMPACT_INSTRUCTIONS}}
    in template.CLAUDE.md.  It is a guide for template editors (the real
    content lives in compact-instructions.md, not the template) and must be
    stripped at render time so it never leaks into the installed
    ~/.claude/CLAUDE.md or the Codex prompt.

    Both render paths in bootstrap.py already iterate ``substitutions`` and
    call str.replace(), so the strip is achieved by mapping this constant to
    the empty string in the substitutions dict.  The string MUST match the
    template line exactly (kept single-line for that reason);
    test_bootstrap_codex.py asserts that the stripped render no longer
    contains it.

CLAUDE_RENDER_SOURCES : list[str]
    Canonical list of template file names (relative to the templates dir)
    that are folded together to produce the installed CLAUDE.md.  The CI
    guard (tests/test_upgrade_drift_sources.py) cross-checks this list
    against the engram-upgrade SKILL.md so that a future extraction of
    another fragment cannot silently re-open the drift gap.

Exposed functions
-----------------
fold_compact_instructions(template_text, compact_text) -> str
    Core fold: replace {{COMPACT_INSTRUCTIONS}} with compact_text and remove
    COMPACT_BREADCRUMB_LINE.  Pure string operation — no file I/O.

render_identity_surface(templates_dir) -> str
    Read template.CLAUDE.md + compact-instructions.md from templates_dir and
    return fold_compact_instructions(...).  Other {{...}} placeholders
    (AGENT_NAME, seed-IDs, ENGRAM_HOME, etc.) are left untouched on purpose:
    the drift tool renders two refs with identical placeholder sets so the
    per-install IDs cancel out and only structural / folded-content changes
    surface in the diff.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The source-of-truth breadcrumb comment that lives above the
#: {{COMPACT_INSTRUCTIONS}} marker in template.CLAUDE.md.  Template editors
#: see it as a pointer to compact-instructions.md; it must NOT appear in any
#: rendered output.  Declared here (not in bootstrap.py) so the drift tool
#: can share the exact same string without duplicating it.
#:
#: Kept single-line so test_bootstrap_codex.py can find it as a literal
#: substring of this file's source text (the test reads the raw .py file).
COMPACT_BREADCRUMB_LINE: str = "<!-- Source of truth: compact-instructions.md — edit there, not in this template. This comment and the marker below are both replaced at install-time. -->\n"  # noqa: E501

#: Files (relative to src/engram/templates/) that are folded together by
#: bootstrap.py to produce the installed ~/.claude/CLAUDE.md.  The CI guard
#: test_upgrade_drift_sources.py cross-checks every entry here against the
#: engram-upgrade SKILL.md's drift-watch section so no future extraction can
#: silently widen the gap.
CLAUDE_RENDER_SOURCES: list[str] = [
    "template.CLAUDE.md",
    "compact-instructions.md",
]


# ---------------------------------------------------------------------------
# Core fold logic
# ---------------------------------------------------------------------------


def fold_compact_instructions(template_text: str, compact_text: str) -> str:
    """Fold compact-instructions.md content into the template.

    Replaces ``{{COMPACT_INSTRUCTIONS}}`` with *compact_text* and removes
    ``COMPACT_BREADCRUMB_LINE`` (the source-of-truth comment above the
    marker in the template).  All other ``{{...}}`` placeholders are left
    untouched — only the two folded-source substitutions are applied here.

    Parameters
    ----------
    template_text:
        Full text of templates/template.CLAUDE.md (raw, unprocessed).
    compact_text:
        Full text of templates/compact-instructions.md.

    Returns
    -------
    str
        The rendered text: breadcrumb stripped, compact body folded in.
        Other placeholders remain as-is.
    """
    result = template_text
    # Strip the source-of-truth breadcrumb so it does not appear in the
    # installed CLAUDE.md.  The breadcrumb sits on its own line immediately
    # above the {{COMPACT_INSTRUCTIONS}} marker; replacing the full string
    # (including the trailing newline captured in COMPACT_BREADCRUMB_LINE)
    # removes the line cleanly.
    result = result.replace(COMPACT_BREADCRUMB_LINE, "")
    # Fold the compact content in place of the marker.
    result = result.replace("{{COMPACT_INSTRUCTIONS}}", compact_text)
    return result


# ---------------------------------------------------------------------------
# High-level render helper
# ---------------------------------------------------------------------------


def render_identity_surface(templates_dir: Path) -> str:
    """Render the CLAUDE.md identity surface from a templates directory.

    Reads ``template.CLAUDE.md`` and ``compact-instructions.md`` from
    *templates_dir*, folds them via :func:`fold_compact_instructions`, and
    returns the result.

    All ``{{...}}`` placeholders other than ``{{COMPACT_INSTRUCTIONS}}`` and
    the breadcrumb are intentionally left unresolved.  When this function is
    called twice on two different git refs' template directories, the
    per-install substitution values (seed-IDs, ENGRAM_HOME, AGENT_NAME, …)
    are identical in both renders and cancel out in the diff — only
    structural template changes and compact-content changes surface.

    Parameters
    ----------
    templates_dir:
        Path to a directory that contains ``template.CLAUDE.md`` and
        (optionally) ``compact-instructions.md``.  If
        ``compact-instructions.md`` is absent, an empty string is used in its
        place (so an extraction from a pre-extraction ref correctly shows the
        folded content as drift).

    Returns
    -------
    str
        The folded render with ``{{COMPACT_INSTRUCTIONS}}`` resolved and the
        breadcrumb stripped; other placeholders intact.
    """
    template_path = templates_dir / "template.CLAUDE.md"
    compact_path = templates_dir / "compact-instructions.md"

    template_text = template_path.read_text(encoding="utf-8")
    compact_text = compact_path.read_text(encoding="utf-8") if compact_path.exists() else ""

    return fold_compact_instructions(template_text, compact_text)
