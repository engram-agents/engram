"""Acceptance gate for the componentized-template decomposition (PR1, #2140).

The whole point of PR1 is that it changes no prose by construction (#2140
sec5): `render("claude-code", "CLAUDE.md")` must reproduce
`template.CLAUDE.md` byte-for-byte. If this test doesn't pass byte-identical,
the fix is in the component split / manifest, never in this test.

Also covers the two build-failure cases from #2140 sec4: a manifest id with
no matching component file (the #2131 inverse-existence lesson), and a
component file that exists but is referenced by no manifest (a dead
component).
"""

import json
import shutil
from pathlib import Path

import pytest

import template_component_render as tcr

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "src" / "engram" / "templates" / "template.CLAUDE.md"


def test_render_claude_code_claude_md_is_byte_identical_to_template():
    rendered, receipt = tcr.render("claude-code", "CLAUDE.md")

    with TEMPLATE_PATH.open("r", encoding="utf-8") as f:
        expected = f.read()

    assert rendered == expected, (
        "render('claude-code', 'CLAUDE.md') did not reproduce "
        "template.CLAUDE.md byte-for-byte -- fix the component split, not "
        "this test (#2140 sec5)."
    )

    # Sanity on the receipt shape while we're here.
    assert receipt["excluded"] == []
    assert receipt["components"], "receipt should list the included components"
    included_ids = [c["id"] for c in receipt["components"]]
    assert len(included_ids) == len(set(included_ids)), "duplicate component id in receipt"
    for c in receipt["components"]:
        # Every component carries an integer version >= 1; a component's
        # version increments on content revision (first real bump: the
        # model-identity-gate self-recognition paragraph, version 2).
        assert isinstance(c["version"], int) and c["version"] >= 1


def test_render_unknown_manifest_raises():
    with pytest.raises(tcr.TemplateComponentError):
        tcr.render("nonexistent-harness", "CLAUDE.md")


def _copytree_worktree(tmp_path):
    """Copy templates/{components,manifests} into an isolated scratch tree."""
    scratch = tmp_path / "templates"
    shutil.copytree(REPO_ROOT / "src" / "engram" / "templates" / "components", scratch / "components")
    shutil.copytree(REPO_ROOT / "src" / "engram" / "templates" / "manifests", scratch / "manifests")
    return scratch


def test_manifest_entry_with_no_matching_component_file_raises(tmp_path, monkeypatch):
    scratch = _copytree_worktree(tmp_path)

    manifest_path = scratch / "manifests" / "claude-code.CLAUDE.md.json"
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest = json.load(f)
    manifest["components"].append({"id": "does-not-exist-anywhere", "anchor": "Nowhere"})
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f)

    monkeypatch.setattr(tcr, "COMPONENTS_DIR", scratch / "components")
    monkeypatch.setattr(tcr, "MANIFESTS_DIR", scratch / "manifests")

    with pytest.raises(tcr.TemplateComponentError, match="does-not-exist-anywhere"):
        tcr.render("claude-code", "CLAUDE.md")


def test_component_file_absent_from_every_manifest_raises_as_dead():
    """A component file present on disk but referenced by no manifest is a
    build FAILURE (#2140 sec4), not a silently-skipped file -- render()
    raises, and the raised message reports the excluded id(s).
    """
    orphan_id = "orphan-component-not-in-any-manifest"
    orphan_path = tcr.COMPONENTS_DIR / f"{orphan_id}.md"
    assert not orphan_path.exists(), "test fixture id collided with a real component"

    orphan_path.write_text(
        "---\n"
        f"id: {orphan_id}\n"
        "kind: rule\n"
        "harnesses: all\n"
        "surfaces: [CLAUDE.md]\n"
        'anchor: "Nowhere"\n'
        "order: 10\n"
        "merge: replace\n"
        "version: 1\n"
        "---\n"
        "This component is deliberately not listed in any manifest.\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(tcr.TemplateComponentError, match=orphan_id):
            tcr.render("claude-code", "CLAUDE.md")
    finally:
        orphan_path.unlink()
