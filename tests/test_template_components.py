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


# --- #3238: write-discipline says "err on the side of writing more" ---------

TEMPLATES_DIR = REPO_ROOT / "src" / "engram" / "templates"
WRITE_MORE_HANDLE = "**Err on the side of writing more than I think I will need.**"
WRITE_NOW_HANDLE = "**Write now, don't defer.**"
FULL_AGENCY_HANDLE = "**Full agency, no permission-asking.**"
WRITE_DISCIPLINE_HEADING = "## ENGRAM Write Discipline"


def _write_discipline_sources():
    """Every source template file that carries the Write Discipline section."""
    paths = [TEMPLATES_DIR / "components" / "engram-write-discipline.md"]
    paths += sorted(TEMPLATES_DIR.glob("template.*"))
    found = [
        p for p in paths
        if WRITE_DISCIPLINE_HEADING in p.read_text(encoding="utf-8")
    ]
    assert found, "no template carries the Write Discipline section"
    return found


def test_write_discipline_carries_write_more_bullet_in_position():
    sources = _write_discipline_sources()
    # Guard against the enumeration silently shrinking: the component and the
    # whole-file CLAUDE.md template must both be covered.
    names = {p.name for p in sources}
    assert {"engram-write-discipline.md", "template.CLAUDE.md"} <= names
    for p in sources:
        text = p.read_text(encoding="utf-8")
        assert text.count(WRITE_MORE_HANDLE) == 1, p.name
        i_now = text.index(WRITE_NOW_HANDLE)
        i_more = text.index(WRITE_MORE_HANDLE)
        i_agency = text.index(FULL_AGENCY_HANDLE)
        assert i_now < i_more < i_agency, p.name


def test_write_discipline_never_restores_hard_trigger_sentence():
    for p in _write_discipline_sources():
        assert "write nudge is a hard trigger" not in p.read_text(
            encoding="utf-8"
        ).lower(), p.name


def test_write_more_bullet_line_identical_across_sources():
    lines = set()
    for p in _write_discipline_sources():
        matches = [
            ln for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.startswith("- " + WRITE_MORE_HANDLE)
        ]
        assert len(matches) == 1, p.name
        lines.add(matches[0])
    assert len(lines) == 1, "write-more bullet differs between sources"


# --- External-communication kinds: component and D6 skill section must agree ---
#
# The "sign every external communication" rule lists four kinds of message in
# two shipped places (the trust-tier-pointer component and the D6 section of
# the engram-trust-tier skill). Nothing else binds the two lists, so this test
# does: each kind's identifying phrase must appear in BOTH.

EXTERNAL_COMMS_PHRASES = [
    "an issue or comment in a repository that neither your primary user nor an "
    "`our_side` collaborator works in",
    "an email to an external person",
    "a letter to a non-`our_side` agent",
    "any public post",
]
TRUST_TIER_POINTER_PATH = (
    REPO_ROOT / "src" / "engram" / "templates" / "components" / "trust-tier-pointer.md"
)
TRUST_TIER_SKILL_PATH = (
    REPO_ROOT / "src" / "engram" / "skills" / "claude" / "engram-trust-tier" / "SKILL.md"
)


def _d6_section(text):
    """Text from the line starting '### D6' up to the next '## ' or '### ' line."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("### D6")), None)
    assert start is not None, (
        "the '### D6' heading is missing from engram-trust-tier/SKILL.md; "
        "cannot check the external-communication list against it"
    )
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## ") or lines[i].startswith("### "):
            end = i
            break
    return "\n".join(lines[start:end])


@pytest.mark.parametrize("phrase", EXTERNAL_COMMS_PHRASES)
def test_external_comms_phrase_in_component_and_d6_section(phrase):
    pointer = TRUST_TIER_POINTER_PATH.read_text(encoding="utf-8")
    d6 = _d6_section(TRUST_TIER_SKILL_PATH.read_text(encoding="utf-8"))
    missing = []
    if phrase not in pointer:
        missing.append("components/trust-tier-pointer.md")
    if phrase not in d6:
        missing.append("engram-trust-tier/SKILL.md (D6 section)")
    assert not missing, (
        f"external-communication phrase {phrase!r} is missing from: "
        f"{', '.join(missing)} -- the two lists have drifted apart"
    )
