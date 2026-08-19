"""CI guard: template_render.CLAUDE_RENDER_SOURCES stays in sync with
engram-upgrade SKILL.md.

One assertion
-------------

1. **SKILL.md cross-check**: every filename in
   ``template_render.CLAUDE_RENDER_SOURCES`` is referenced (as a literal
   substring) in the engram-upgrade SKILL.md.  This links the code's
   render-source set to the skill's drift-watch documentation — a future
   extraction can add a new file to CLAUDE_RENDER_SOURCES but, until the
   SKILL.md also references it, this test fails loudly.

History
-------
This module used to carry two additional assertions: a check that
bootstrap.py held no second literal copy of ``COMPACT_BREADCRUMB_LINE``, and
a functional fold test proving a compact-instructions.md-only change
surfaced as drift (the PR #1145 case). engram-alpha#2067 deleted both
``COMPACT_BREADCRUMB_LINE`` and ``fold_compact_instructions()`` from
template_render.py entirely — the Claude template no longer folds
compact-instructions.md in at all (see docs/compaction-continuity.md) — so
both assertions tested machinery that no longer exists and were deleted
rather than fixture-patched. The PR #1145 class of gap they guarded
(a folded-source-only change invisible to a template-only diff) no longer
applies to the Claude surface: compact-instructions.md is Codex-only now,
outside CLAUDE_RENDER_SOURCES, so it correctly produces no Claude drift —
see test_check_claude_md_drift.py's test_compact_instructions_change_exits_0
for the positive regression guard on that new invariant.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"

# Insert src/engram so we can import template_render (side-effect-free).
if str(SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(SRC_ENGRAM))

# Import after path manipulation.
import template_render  # noqa: E402

SKILL_MD_PATH = (
    REPO_ROOT
    / "src" / "engram" / "skills" / "claude" / "engram-upgrade" / "SKILL.md"
)

# ---------------------------------------------------------------------------
# Every CLAUDE_RENDER_SOURCE is mentioned in the upgrade SKILL.md
# ---------------------------------------------------------------------------


def test_render_sources_referenced_in_upgrade_skill():
    """Every file in CLAUDE_RENDER_SOURCES must appear in engram-upgrade SKILL.md.

    This is the mechanical link between the code's render-source declaration
    and the upgrade skill's drift-watch documentation.  Adding a new source
    file to CLAUDE_RENDER_SOURCES without also mentioning it in the skill
    means the upgrading agent would not know to watch it for drift.
    """
    assert SKILL_MD_PATH.exists(), (
        f"engram-upgrade SKILL.md not found at {SKILL_MD_PATH}"
    )
    skill_text = SKILL_MD_PATH.read_text(encoding="utf-8")

    missing: list[str] = []
    for source_file in template_render.CLAUDE_RENDER_SOURCES:
        if source_file not in skill_text:
            missing.append(source_file)

    assert not missing, (
        "The following files are in template_render.CLAUDE_RENDER_SOURCES but "
        "NOT referenced in the engram-upgrade SKILL.md. Add them to the skill's "
        "drift-watch documentation (Step 2 / Step 6) so upgrades catch all "
        "rendered-CLAUDE.md sources:\n"
        + "\n".join(f"  {f}" for f in missing)
    )
