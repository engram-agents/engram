"""CI guard: template_render.CLAUDE_RENDER_SOURCES stays in sync with
engram-upgrade SKILL.md and bootstrap.py stays breadcrumb-constant-free.

Three assertions
----------------

1. **SKILL.md cross-check**: every filename in
   ``template_render.CLAUDE_RENDER_SOURCES`` is referenced (as a literal
   substring) in the engram-upgrade SKILL.md.  This links the code's
   render-source set to the skill's drift-watch documentation — a future
   extraction can add a new file to CLAUDE_RENDER_SOURCES but, until the
   SKILL.md also references it, this test fails loudly.

2. **No second breadcrumb literal in bootstrap.py**: after the SSoT
   refactor, ``COMPACT_BREADCRUMB_LINE`` lives ONLY in template_render.py.
   bootstrap.py must NOT contain a separate assignment of the literal
   breadcrumb string.  This prevents the exact drift class we're closing: two
   copies of the same string that can silently diverge.

3. **Functional fold test**: build a tiny temp "base" dir (template with
   ``{{COMPACT_INSTRUCTIONS}}`` + a compact file saying "OLD") and a "head"
   dir (same template + compact file saying "NEW"); assert that
   render_identity_surface() produces *different* outputs for them and that
   the breadcrumb is absent from BOTH renders.  This proves the drift tool
   would catch a folded-source-only change — exactly the PR #1145 case that
   exposed the gap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
BOOTSTRAP_PY_PATH = REPO_ROOT / "src" / "engram" / "bootstrap.py"

# ---------------------------------------------------------------------------
# Test 1: every CLAUDE_RENDER_SOURCE is mentioned in the upgrade SKILL.md
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


# ---------------------------------------------------------------------------
# Test 2: bootstrap.py imports COMPACT_BREADCRUMB_LINE — no second literal
# ---------------------------------------------------------------------------


def test_bootstrap_no_second_breadcrumb_literal():
    """bootstrap.py must NOT contain a second literal copy of the breadcrumb string.

    After the SSoT refactor, COMPACT_BREADCRUMB_LINE lives in template_render.py
    and is imported by bootstrap.py.  A second literal assignment in bootstrap.py
    would re-create the exact drift class we're closing: two copies that can
    diverge silently.

    The sentinel for a "second literal" is the opening of the breadcrumb HTML
    comment being assigned directly in bootstrap.py (rather than imported).
    We check that the string ``"<!-- Source of truth: compact-instructions.md``
    does NOT appear as a separate string literal in bootstrap.py source.
    """
    bootstrap_src = BOOTSTRAP_PY_PATH.read_text(encoding="utf-8")

    # The literal opening of the breadcrumb comment is the fingerprint.
    # It must appear in template_render.py (as the constant definition) but
    # NOT as a standalone assignment in bootstrap.py.
    breadcrumb_opening = "<!-- Source of truth: compact-instructions.md"

    # Check it is in template_render.py (SSoT).
    tr_src = (SRC_ENGRAM / "template_render.py").read_text(encoding="utf-8")
    assert breadcrumb_opening in tr_src, (
        "COMPACT_BREADCRUMB_LINE is not defined in template_render.py — "
        "the SSoT constant is missing."
    )

    # Check it does NOT appear as a second separate assignment in bootstrap.py.
    # An import reference is fine; a second literal string definition is not.
    # We detect a "second copy" by looking for the literal being assigned to
    # a variable (= "<!-- Source...") rather than just being imported.
    # The import line in bootstrap.py reads: from template_render import COMPACT_BREADCRUMB_LINE
    # A second copy would look like: foo = "<!-- Source of truth: compact-instructions.md..."
    #
    # Strategy: count occurrences.  After the refactor, the breadcrumb opening
    # must appear ZERO times as a string literal in bootstrap.py (the import
    # line only references the name COMPACT_BREADCRUMB_LINE, not the string itself).
    assert breadcrumb_opening not in bootstrap_src, (
        f"bootstrap.py contains a second literal copy of the breadcrumb "
        f"string ({breadcrumb_opening!r}). "
        "It should import COMPACT_BREADCRUMB_LINE from template_render instead "
        "of redefining the string — a second literal copy can silently diverge."
    )


# ---------------------------------------------------------------------------
# Test 3: functional fold test — compact-only change surfaces as drift
# ---------------------------------------------------------------------------


def test_render_catches_compact_only_change(tmp_path: Path):
    """render_identity_surface() produces different output when compact-instructions
    changes but the main template does not — the PR #1145 case.

    Also asserts the breadcrumb is absent from BOTH renders (the strip works).
    """
    breadcrumb = template_render.COMPACT_BREADCRUMB_LINE

    # Minimal template that exercises the fold: breadcrumb + marker.
    minimal_template = (
        "# Identity\n"
        "Some identity content.\n"
        "\n"
        "## Compact Instructions\n"
        + breadcrumb
        + "{{COMPACT_INSTRUCTIONS}}\n"
        "## After\n"
        "Content after the compact section.\n"
    )

    # Base dir: compact says "OLD"
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    (base_dir / "template.CLAUDE.md").write_text(minimal_template, encoding="utf-8")
    (base_dir / "compact-instructions.md").write_text(
        "OLD compact content.\n", encoding="utf-8"
    )

    # Head dir: compact says "NEW" (same template — compact-only change)
    head_dir = tmp_path / "head"
    head_dir.mkdir()
    (head_dir / "template.CLAUDE.md").write_text(minimal_template, encoding="utf-8")
    (head_dir / "compact-instructions.md").write_text(
        "NEW compact content.\n", encoding="utf-8"
    )

    base_render = template_render.render_identity_surface(base_dir)
    head_render = template_render.render_identity_surface(head_dir)

    # 1. The two renders DIFFER (compact-only change is detected).
    assert base_render != head_render, (
        "render_identity_surface() returned identical output for base and head "
        "even though compact-instructions.md changed. The drift tool would have "
        "missed the PR #1145 class of change."
    )

    # 2. "OLD" is in base but NOT in head (compact content actually folded in).
    assert "OLD compact content." in base_render, (
        "Base render does not contain the expected compact content ('OLD'). "
        "fold_compact_instructions may be broken."
    )
    assert "NEW compact content." in head_render, (
        "Head render does not contain the expected compact content ('NEW'). "
        "fold_compact_instructions may be broken."
    )

    # 3. Breadcrumb is ABSENT from both renders (strip works correctly).
    # The breadcrumb contains the HTML comment text; check for the opening tag.
    breadcrumb_marker = "<!-- Source of truth: compact-instructions.md"
    assert breadcrumb_marker not in base_render, (
        "Breadcrumb leaked into the BASE render — "
        "fold_compact_instructions is not stripping COMPACT_BREADCRUMB_LINE."
    )
    assert breadcrumb_marker not in head_render, (
        "Breadcrumb leaked into the HEAD render — "
        "fold_compact_instructions is not stripping COMPACT_BREADCRUMB_LINE."
    )
