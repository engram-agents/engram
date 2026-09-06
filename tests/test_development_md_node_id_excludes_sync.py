"""Drift guard for docs/DEVELOPMENT.md's hand-copied node-ID EXCLUDES snippet.

engram-alpha#2626: the local pre-push check snippet in DEVELOPMENT.md's
"Enforcement" section is a hand-maintained copy of the EXCLUDES array in
.github/workflows/pr-checks.yml's no-new-shipped-node-ids step. It had
drifted -- 5 missing entries and one wrong path -- with nobody catching it
because nothing compared the two. This test is that comparison: it parses
both EXCLUDES lists (as sets of `:(exclude)PATH` pathspecs) and asserts they
match exactly, so a future edit to either side that doesn't update the other
fails CI immediately instead of silently drifting again.

Deliberately NOT a bigger refactor (single-source-of-truth generation, per
Ariadne's design question on #2626) -- syncing by hand plus a guard that
actually fires is the cheaper fix, and the guard is what was missing, not
the sync mechanism.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "pr-checks.yml"
DEV_MD_PATH = REPO_ROOT / "docs" / "DEVELOPMENT.md"

_EXCLUDE_RE = re.compile(r":\(exclude\)([^\s'\"]+)")


def _workflow_excludes() -> set[str]:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    # Isolate the no-new-shipped-node-ids step's EXCLUDES=( ... ) block --
    # scoping to it (rather than scanning the whole workflow file) means a
    # *different* step later adding an unrelated :(exclude) pathspec can't
    # silently fail this test for a reason that has nothing to do with it.
    match = re.search(r"EXCLUDES=\((.*?)\n\s*\)", text, re.DOTALL)
    assert match, "EXCLUDES=( ... ) block not found in pr-checks.yml -- step renamed/restructured?"
    # Drop commented-out lines (e.g. the dead engram_ids.py root-symlink
    # entry, kept as an explanatory comment after Phase 4) -- a bare regex
    # over the block's raw text can't distinguish "in the array" from
    # "mentioned in a comment about the array", and this block has both.
    active_lines = "\n".join(
        line for line in match.group(1).splitlines() if not line.strip().startswith("#")
    )
    return set(_EXCLUDE_RE.findall(active_lines))


def _development_md_excludes() -> set[str]:
    text = DEV_MD_PATH.read_text(encoding="utf-8")
    # Isolate the local-check bash snippet (the fenced block right after
    # "To check your own PR locally before pushing") -- scoping to it, not
    # the whole file, for the same reason as above.
    match = re.search(
        r"To check your own PR locally before pushing.*?```bash(.*?)```", text, re.DOTALL
    )
    assert match, "local pre-push check snippet not found under its expected heading in DEVELOPMENT.md"
    return set(_EXCLUDE_RE.findall(match.group(1)))


def test_development_md_excludes_match_live_workflow():
    workflow_excludes = _workflow_excludes()
    dev_md_excludes = _development_md_excludes()

    missing_from_docs = workflow_excludes - dev_md_excludes
    extra_in_docs = dev_md_excludes - workflow_excludes

    assert not missing_from_docs and not extra_in_docs, (
        "docs/DEVELOPMENT.md's local pre-push check snippet has drifted from "
        "the live .github/workflows/pr-checks.yml EXCLUDES array.\n"
        f"  Missing from DEVELOPMENT.md (present in the live gate): {sorted(missing_from_docs) or None}\n"
        f"  Extra in DEVELOPMENT.md (not in the live gate): {sorted(extra_in_docs) or None}\n"
        "Sync both the bash snippet and the 'Allowlisted paths' prose list."
    )


def test_development_md_excludes_nonempty_sanity():
    """Checked to fail: if the regex/heading anchors above ever stop
    matching anything (e.g. the heading text changes), both sets would be
    empty and the equality check above would pass vacuously -- catch that
    directly rather than trusting an empty-set match."""
    assert len(_workflow_excludes()) >= 5
    assert len(_development_md_excludes()) >= 5
