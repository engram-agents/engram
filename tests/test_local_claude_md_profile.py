"""The `local` CLAUDE.md profile: a second manifest over the existing components.

`local` is for one agent working with one user. It is a second, named rendering of the
identity template: the same component files, fewer of them. These cells pin the
contract written before the implementation:

  (a) render(profile=None) is today's bytes; the existing byte-identical pin is untouched.
  (b) an unknown or path-shaped profile name raises and never falls back to the full template.
  (c) bootstrap records the choice in config.json; the upgrade flow compares a home's file
      against the template OF THE RECORDED PROFILE, never offers the other one, and never
      writes the user's CLAUDE.md.
  (d) the new shippables are tiered.
  (e) the local render keeps the consent paragraph and the authority table.
  (f) the dead-component rule still works with two manifests.
  (g) the joins at the new adjacencies are clean.
  (h) bootstrap resolves the local file's placeholders the way it resolves the full one's.
  (i) the local render is shorter and carries none of the dropped components' headings.

All bootstrap runs use scratch homes through the environment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import template_component_render as tcr

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"
TEMPLATES = SRC / "templates"
FULL_TEMPLATE = TEMPLATES / "template.CLAUDE.md"
LOCAL_TEMPLATE = TEMPLATES / "template.CLAUDE.local.md"
LOCAL_MANIFEST = TEMPLATES / "manifests" / "claude-code.CLAUDE.md.local.json"
FULL_MANIFEST = TEMPLATES / "manifests" / "claude-code.CLAUDE.md.json"
BOOTSTRAP = SRC / "bootstrap.py"

LOCAL_IDS = [
    "identity-opening",
    "user-notes-verbatim-local",
    "authority-structure-intro",
    "authority-structure-table-claude-code",
    "authority-structure-close",
    "trust-tier-pointer",
    "model-identity-gate-local",
    "engram-write-discipline",
    "engram-read-discipline",
    "confidence-anchors",
    "native-tools-ban-local",
    "engram-reference",
    "session-start-reading-local",
]

SSR_ID = "session-start-reading"
SSR_LOCAL_ID = "session-start-reading-local"
SSR_CLAUSE = " Style: §Writing style for auto-load surfaces."
BAN_ID = "native-tools-ban-local"
GATE_ID = "model-identity-gate-local"
NOTES_ID = "user-notes-verbatim-local"
# every profile-only component: in the local manifest, in no full manifest
LOCAL_ONLY_IDS = (BAN_ID, GATE_ID, NOTES_ID)

PLACEHOLDER = re.compile(r"\{\{[^}]+\}\}")


def _manifest_ids(path: Path) -> list[str]:
    return [e["id"] for e in json.loads(path.read_text(encoding="utf-8"))["components"]]


def _local() -> str:
    return tcr.render("claude-code", "CLAUDE.md", "local")[0]


def _full() -> str:
    return tcr.render("claude-code", "CLAUDE.md")[0]


# --- (a) default is today's bytes ---------------------------------------------------------


def test_a_profile_none_is_todays_bytes_positional_and_keyword():
    expected = FULL_TEMPLATE.read_text(encoding="utf-8")
    assert tcr.render("claude-code", "CLAUDE.md")[0] == expected
    assert tcr.render("claude-code", "CLAUDE.md", None)[0] == expected
    assert tcr.render("claude-code", "CLAUDE.md", profile=None)[0] == expected


# --- (b) refusals --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["nonexistent", "Local2", "local-x", "full"])
def test_b_unknown_profile_raises_and_never_falls_back(name):
    with pytest.raises(tcr.TemplateComponentError):
        tcr.render("claude-code", "CLAUDE.md", name)


@pytest.mark.parametrize(
    "name",
    ["../x", "a/b", "", ".", "..", "a\\b", "local/../local", "../../etc/passwd", "local.json", " local", "local\n", "/abs"],
)
def test_b_path_shaped_name_raises_before_any_file_is_read(name, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a file was read for a refused profile name")

    monkeypatch.setattr(tcr, "_load_manifest", boom)
    monkeypatch.setattr(tcr, "_load_component", boom)
    monkeypatch.setattr(tcr, "_manifest_path", boom)
    with pytest.raises(tcr.TemplateComponentError, match="profile"):
        tcr.render("claude-code", "CLAUDE.md", name)


def test_b_non_string_profile_raises():
    with pytest.raises(tcr.TemplateComponentError):
        tcr.render("claude-code", "CLAUDE.md", 7)  # type: ignore[arg-type]


# --- the local manifest and the committed local template ------------------------------------


def test_local_manifest_is_the_listed_ids_in_the_full_manifests_relative_order():
    local_ids = _manifest_ids(LOCAL_MANIFEST)
    assert local_ids == LOCAL_IDS
    full_ids = _manifest_ids(FULL_MANIFEST)
    # The profile-only components have no place in the full manifest; the rest keep its relative order.
    normalised = [SSR_ID if i == SSR_LOCAL_ID else i for i in local_ids]
    assert [i for i in normalised if i not in LOCAL_ONLY_IDS] == [i for i in full_ids if i in set(normalised)], (
        "the local manifest must keep the full manifest's relative order"
    )
    # The ban sits where its host section sits in the full file: after the confidence anchors,
    # before the ENGRAM Reference.
    assert local_ids.index(BAN_ID) == local_ids.index("confidence-anchors") + 1
    assert local_ids.index(BAN_ID) == local_ids.index("engram-reference") - 1
    # Each sentence sits where its host section sits in the full file: the gate sentence before the
    # write discipline (the full file has model-identity-gate just before it), the notes sentence
    # between the write and read disciplines (cross-surface-persistence's place).
    assert local_ids.index(GATE_ID) == local_ids.index("engram-write-discipline") - 1
    assert local_ids.index(NOTES_ID) == local_ids.index("identity-opening") + 1
    assert local_ids.index(NOTES_ID) == local_ids.index("authority-structure-intro") - 1


def test_local_render_receipt_lists_the_manifest_ids():
    _, receipt = tcr.render("claude-code", "CLAUDE.md", "local")
    assert [c["id"] for c in receipt["components"]] == LOCAL_IDS
    assert receipt["excluded"] == []


def test_local_template_is_pinned_byte_identical_to_the_render():
    assert LOCAL_TEMPLATE.read_bytes() == _local().encode("utf-8"), (
        "template.CLAUDE.local.md is generated by render('claude-code','CLAUDE.md','local'); "
        "regenerate it with the renderer, never by hand"
    )


# --- the profile-only native-tools ban ---------------------------------------------------------

BAN_START = "**BANNED"


def _full_ban_paragraph() -> str:
    """The ban paragraph of the FULL component file, read at test time: from `**BANNED` to the next blank line."""
    text = (TEMPLATES / "components" / "fairy-tool-policy-claude-code.md").read_text(encoding="utf-8")
    start = text.index(BAN_START)
    assert text.count(BAN_START) == 1
    end = text.find("\n\n", start)
    return text[start:end]


def test_ban_local_component_body_equals_the_full_components_paragraph_byte_for_byte():
    para = _full_ban_paragraph()
    assert para.strip() and "NATIVE deep-research" in para and "`Workflow`" in para
    fm, body = tcr._load_component(BAN_ID)
    # heading rule: a profile-only component at an absent anchor renders as its own heading, then the paragraph;
    # the body owns its trailing blank line (whitespace-ownership rule): paragraph, then one "\n"
    assert body == "## " + fm["anchor"] + "\n\n" + para + "\n"


def test_ban_local_component_is_in_the_local_manifest_and_not_in_the_full_manifest():
    assert BAN_ID in _manifest_ids(LOCAL_MANIFEST)
    assert BAN_ID not in _manifest_ids(FULL_MANIFEST)


def test_ban_paragraph_renders_exactly_once_in_local_and_exactly_once_in_full():
    para = _full_ban_paragraph()
    assert _local().count(para) == 1
    assert _full().count(para) == 1


# --- the two profile-only sentences (model identity gate; notes stay verbatim) ----------------


GATE_PARAGRAPH_START = "My trusted family is recorded once, at the first session"
GATE_MISMATCH_START = "- **Mismatch**"
GATE_FULL_COMPONENT = TEMPLATES / "components" / "model-identity-gate.md"


def _gate_lines_of(text: str) -> tuple[str, str]:
    paragraphs = [ln for ln in text.split("\n") if ln.startswith(GATE_PARAGRAPH_START)]
    bullets = [ln for ln in text.split("\n") if ln.startswith(GATE_MISMATCH_START)]
    assert len(paragraphs) == 1 and len(bullets) == 1, (len(paragraphs), len(bullets))
    return paragraphs[0], bullets[0]


def _gate_paragraph() -> str:
    """The paragraph that introduces the match / mismatch bullets, read at test time from the FULL component file."""
    return _gate_lines_of(GATE_FULL_COMPONENT.read_text(encoding="utf-8"))[0]


def _gate_mismatch_bullet() -> str:
    """The whole Mismatch bullet (the action on a mismatch), read at test time from the FULL component file."""
    return _gate_lines_of(GATE_FULL_COMPONENT.read_text(encoding="utf-8"))[1]


GATE_MATCH_START = "- **Match**"
GATE_UNSET_START = "- **`self_lineage` unset**"
GATE_READ_START = "The procedure is the `engram-model-switch-read` skill:"
GATE_READ_END = "which means the read failed as an instrument."


def _gate_match_bullet() -> str:
    """The whole Match bullet (its last clause says to run the self-recognition read below), from the FULL component file."""
    lines = [ln for ln in GATE_FULL_COMPONENT.read_text(encoding="utf-8").split("\n") if ln.startswith(GATE_MATCH_START)]
    assert len(lines) == 1
    return lines[0]


def _gate_unset_bullet() -> str:
    """The whole `self_lineage` unset bullet (the gate is dormant; first-session records it), from the FULL component file."""
    lines = [ln for ln in GATE_FULL_COMPONENT.read_text(encoding="utf-8").split("\n") if ln.startswith(GATE_UNSET_START)]
    assert len(lines) == 1
    return lines[0]


def _gate_read_sentence() -> str:
    """The one sentence of the source's 'Why the gate exists' paragraph that names the skill, read at test time."""
    text = GATE_FULL_COMPONENT.read_text(encoding="utf-8")
    assert text.count(GATE_READ_START) == 1 and text.count(GATE_READ_END) == 1
    start = text.index(GATE_READ_START)
    end = text.index(GATE_READ_END, start) + len(GATE_READ_END)
    sentence = text[start:end]
    assert "\n" not in sentence and ". " not in sentence, "the carried text must be exactly one sentence"
    assert text[end] == "\n", "the sentence ends its paragraph in the source"
    return sentence


def _gate_text() -> str:
    """What the local gate carries, as the full file lays it out: the paragraph; the Match, Mismatch and unset bullets
    (adjacent lines, #4802 ruling of 2026-11-19); a blank line; the one sentence that names the read's skill."""
    return (
        _gate_paragraph() + "\n\n" + _gate_match_bullet() + "\n" + _gate_mismatch_bullet() + "\n"
        + _gate_unset_bullet() + "\n\n" + _gate_read_sentence()
    )


def _notes_sentence() -> str:
    """Item 3 of the warm-briefing list, minus its list marker, read at test time from the FULL file."""
    text = (TEMPLATES / "components" / "cross-surface-persistence.md").read_text(encoding="utf-8")
    lines = [ln for ln in text.split("\n") if ln.startswith("3. ") and "stay VERBATIM" in ln]
    assert len(lines) == 1
    return lines[0][len("3. ") :]


SCOPE_LINE = "**warm-briefing.md:**"


def _scope_line() -> str:
    """The line heading the warm-briefing list in the FULL file, read at test time (exactly one occurrence)."""
    text = (TEMPLATES / "components" / "cross-surface-persistence.md").read_text(encoding="utf-8")
    lines = [ln for ln in text.split("\n") if ln == SCOPE_LINE]
    assert len(lines) == 1
    return lines[0]


_SENTENCES = [
    pytest.param(NOTES_ID, _notes_sentence, "Never edit or summarize them.", id="notes"),
]


@pytest.mark.parametrize("cid,get_sentence,marker", _SENTENCES)
def test_local_sentence_component_body_equals_the_full_files_sentence_byte_for_byte(cid, get_sentence, marker):
    sentence = get_sentence()
    assert sentence.strip() and marker in sentence and "\n" not in sentence
    fm, body = tcr._load_component(cid)
    # whitespace-ownership rule: the body owns its trailing blank line, so it ends "\n" after the sentence.
    # the notes sentence keeps the source's scope line (the line heading its list), then the sentence
    assert body == _scope_line() + "\n\n" + sentence + "\n"


@pytest.mark.parametrize("cid", [GATE_ID, NOTES_ID], ids=["gate", "notes"])
def test_local_sentence_component_is_in_the_local_manifest_and_not_in_the_full_manifest(cid):
    assert cid in _manifest_ids(LOCAL_MANIFEST)
    assert cid not in _manifest_ids(FULL_MANIFEST)


@pytest.mark.parametrize("cid,get_sentence,marker", _SENTENCES)
def test_local_sentence_renders_exactly_once_in_local_and_exactly_once_in_full(cid, get_sentence, marker):
    sentence = get_sentence()
    assert _local().count(sentence) == 1
    assert _full().count(sentence) == 1



# --- the model gate's action: the full file's paragraph and Mismatch bullet, byte for byte --------


def test_gate_lines_are_the_ones_the_ruling_names():
    para, bullet = _gate_paragraph(), _gate_mismatch_bullet()
    assert "\n" not in para and "\n" not in bullet
    # (a) the paragraph that tells the agent to compare the model family at session start
    assert para.startswith("My trusted family is recorded once, at the first session")
    assert "read the model tag in the system prompt, map it to its family, and compare against `self_lineage`" in para
    # (b) the action on a mismatch: stop, tell the user
    assert bullet.startswith("- **Mismatch** (e.g. a Sonnet tag against")
    assert "STOP autonomous and loop work immediately. Greet the user" in bullet
    assert bullet.endswith("Changing family is an identity decision, never a silent swap.")


def test_gate_match_bullet_and_read_sentence_are_the_ones_the_ruling_names():
    match, sentence = _gate_match_bullet(), _gate_read_sentence()
    assert match.startswith("- **Match** (same family, any point version")
    assert match.endswith("run the self-recognition read below.")
    assert sentence.startswith("The procedure is the `engram-model-switch-read` skill:")
    assert sentence.endswith("which means the read failed as an instrument.")


def test_gate_component_body_is_the_heading_and_the_full_file_items_byte_for_byte():
    fm, body = tcr._load_component(GATE_ID)
    # heading rule: the anchor heading is absent from the local file, so the component brings its own;
    # the items are laid out as the full file lays them out (paragraph, blank line, Match and Mismatch bullets on
    # adjacent lines, blank line, the one sentence); the body owns its trailing blank line (whitespace-ownership rule).
    assert body == "## " + fm["anchor"] + "\n\n" + _gate_text() + "\n"


def test_gate_component_body_lines_each_occur_verbatim_in_the_source_and_the_carried_sentence_is_in_it():
    _, body = tcr._load_component(GATE_ID)
    source = GATE_FULL_COMPONENT.read_text(encoding="utf-8")
    source_lines = source.split("\n")
    body_lines = [ln for ln in body.split("\n") if ln.strip()]
    assert body_lines[0] == "## Model Identity Gate"
    not_whole_lines = [ln for ln in body_lines if ln not in source_lines]
    # exactly one body line is not a whole source line: the carried sentence, found verbatim inside the source's paragraph
    assert not_whole_lines == [_gate_read_sentence()]
    assert not_whole_lines[0] in source
    assert len(body_lines) == 6  # heading, paragraph, Match, Mismatch, unset, sentence: no other new words


def test_gate_component_version_was_bumped_past_the_two_line_version():
    fm, _ = tcr._load_component(GATE_ID)
    assert fm["version"] >= 3, "the component's text changed again (the read pointer joined); its version must move"


def test_the_rendered_local_file_carries_the_match_bullet_and_the_sentence_naming_the_skill():
    text = _local()
    assert text.split("\n").count(_gate_match_bullet()) == 1
    assert text.count(_gate_read_sentence()) == 1
    assert text.count("`engram-model-switch-read`") == 1


def test_the_match_bullets_read_below_is_followed_later_in_the_section_by_the_sentence():
    lines = _local().splitlines()
    start = lines.index("## Model Identity Gate") + 1
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("## "))
    section = lines[start:end]
    below = [i for i, ln in enumerate(section) if "run the self-recognition read below" in ln]
    pointed = [i for i, ln in enumerate(section) if _gate_read_sentence() in ln]
    assert len(below) == 1 and len(pointed) == 1, (below, pointed)
    assert below[0] < pointed[0], "the 'below' in the Match bullet must point at text that follows it in the section"


def test_gate_items_render_once_each_in_local_and_once_each_in_the_full_template_and_render():
    para, match, bullet = _gate_paragraph(), _gate_match_bullet(), _gate_mismatch_bullet()
    local = _local()
    for line in (para, match, bullet):
        assert local.split("\n").count(line) == 1
        assert _full().split("\n").count(line) == 1
        # the committed full template carries them too, so a drift in the template alone is caught here as well
        assert FULL_TEMPLATE.read_text(encoding="utf-8").split("\n").count(line) == 1
    sentence = _gate_read_sentence()
    assert local.count(sentence) == 1 and _full().count(sentence) == 1
    assert FULL_TEMPLATE.read_text(encoding="utf-8").count(sentence) == 1
    assert ("## Model Identity Gate\n\n" + _gate_text() + "\n") in local


def test_gate_section_in_the_local_render_holds_exactly_the_five_items():
    lines = _local().splitlines()
    start = lines.index("## Model Identity Gate") + 1
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("## "))
    assert [ln for ln in lines[start:end] if ln.strip()] == [
        _gate_paragraph(), _gate_match_bullet(), _gate_mismatch_bullet(), _gate_unset_bullet(), _gate_read_sentence()
    ]

# --- heading rule: a profile-only component at an absent anchor brings its own heading ----------

_HEADED = [
    pytest.param(GATE_ID, "Model Identity Gate", "External interactions", "trust-tier-pointer", _gate_text, id="gate"),
    pytest.param(
        BAN_ID, "Parallelization and Delegation", "Confidence calibration anchors", "confidence-anchors",
        _full_ban_paragraph, id="ban",
    ),
]


@pytest.mark.parametrize("cid,anchor,prev_section,prev_id,get_text", _HEADED)
def test_heading_line_equals_the_components_own_anchor_field(cid, anchor, prev_section, prev_id, get_text):
    fm, body = tcr._load_component(cid)
    assert fm["anchor"] == anchor
    assert body.split("\n", 1)[0] == "## " + fm["anchor"]


@pytest.mark.parametrize("cid,anchor,prev_section,prev_id,get_text", _HEADED)
def test_heading_appears_once_in_local_with_the_text_directly_under_it(cid, anchor, prev_section, prev_id, get_text):
    local = _local()
    assert local.split("\n").count("## " + anchor) == 1
    assert ("## " + anchor + "\n\n" + get_text() + "\n") in local
    # the full render keeps its own single heading from the full components
    assert _full().split("\n").count("## " + anchor) == 1


@pytest.mark.parametrize("cid,anchor,prev_section,prev_id,get_text", _HEADED)
def test_the_text_no_longer_sits_at_the_end_of_the_previous_section(cid, anchor, prev_section, prev_id, get_text):
    lines = _local().split("\n")
    blanks, before = _blank_lines_before(lines, lines.index("## " + anchor))
    assert blanks == 1
    # the line before the blank is the previous local component's last line, not the moved text
    assert before == _last_line(prev_id)
    assert before != get_text()


def test_notes_scope_line_and_sentence_sit_after_my_goal_and_before_authority_structure():
    local = _local()
    block = _scope_line() + "\n\n" + _notes_sentence() + "\n"
    assert local.count(block) == 1
    at = local.index(block)
    assert local.index("## My Goal") < at < local.index("## Authority Structure")
    # directly before the Authority Structure heading: block, one blank line, heading
    assert local[at + len(block):].startswith("\n## Authority Structure")


# --- (e) consent paragraph and authority table --------------------------------------------


def _component_body(component_id: str) -> str:
    raw = (TEMPLATES / "components" / f"{component_id}.md").read_text(encoding="utf-8")
    return raw.split("\n---\n", 1)[1]


def test_e_local_keeps_the_consent_paragraph_and_the_authority_table():
    close = _component_body("authority-structure-close")
    match = re.search(r"[^.\n]*claims no consent[^.\n]*\.", close)
    assert match, "authority-structure-close no longer holds the 'claims no consent' sentence"
    sentence = match.group(0).strip()
    assert sentence and "claims no consent" in sentence
    local = _local()
    assert sentence in local
    table_header = "| Class | Examples | My authority |"
    assert table_header in _component_body("authority-structure-table-claude-code")
    assert table_header in local
    ids = [c["id"] for c in tcr.render("claude-code", "CLAUDE.md", "local")[1]["components"]]
    assert "authority-structure-close" in ids
    assert "authority-structure-table-claude-code" in ids


# --- (f) dead-component rule with two manifests -------------------------------------------


def _scratch(tmp_path, monkeypatch) -> Path:
    scratch = tmp_path / "templates"
    shutil.copytree(TEMPLATES / "components", scratch / "components")
    shutil.copytree(TEMPLATES / "manifests", scratch / "manifests")
    monkeypatch.setattr(tcr, "COMPONENTS_DIR", scratch / "components")
    monkeypatch.setattr(tcr, "MANIFESTS_DIR", scratch / "manifests")
    return scratch


def _component_file(component_id: str) -> str:
    return (
        "---\n"
        f"id: {component_id}\n"
        "kind: rule\n"
        "harnesses: all\n"
        "surfaces: [CLAUDE.md]\n"
        'anchor: "Nowhere"\n'
        "order: 10\n"
        "merge: replace\n"
        "version: 1\n"
        "---\n"
        "A scratch component.\n"
    )


def test_f_a_component_listed_only_in_the_full_manifest_is_live_not_dead():
    only_full = set(_manifest_ids(FULL_MANIFEST)) - set(_manifest_ids(LOCAL_MANIFEST))
    assert only_full, "the local manifest should drop some components"
    # Rendering local succeeds: nothing in components/ is flagged dead.
    tcr.render("claude-code", "CLAUDE.md", "local")
    assert only_full <= tcr._all_referenced_component_ids()


def test_f_a_component_in_no_manifest_still_fails_a_local_render(tmp_path, monkeypatch):
    scratch = _scratch(tmp_path, monkeypatch)
    (scratch / "components" / "orphan-for-local.md").write_text(
        _component_file("orphan-for-local"), encoding="utf-8"
    )
    with pytest.raises(tcr.TemplateComponentError, match="orphan-for-local"):
        tcr.render("claude-code", "CLAUDE.md", "local")
    with pytest.raises(tcr.TemplateComponentError, match="orphan-for-local"):
        tcr.render("claude-code", "CLAUDE.md")


def test_f_a_component_listed_only_in_the_local_manifest_is_live(tmp_path, monkeypatch):
    scratch = _scratch(tmp_path, monkeypatch)
    (scratch / "components" / "profile-only-extra.md").write_text(
        _component_file("profile-only-extra"), encoding="utf-8"
    )
    path = scratch / "manifests" / "claude-code.CLAUDE.md.local.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["components"].append({"id": "profile-only-extra", "anchor": "Nowhere"})
    path.write_text(json.dumps(manifest), encoding="utf-8")
    text, _ = tcr.render("claude-code", "CLAUDE.md", "local")
    assert "A scratch component." in text
    tcr.render("claude-code", "CLAUDE.md")


# --- (g) joins -----------------------------------------------------------------------------


def _first_line(component_id: str) -> str:
    body = tcr._load_component(component_id)[1]
    return next(ln for ln in body.splitlines() if ln.strip())


def _last_line(component_id: str) -> str:
    body = tcr._load_component(component_id)[1]
    return [ln for ln in body.splitlines() if ln.strip()][-1]


def _blank_lines_before(lines: list[str], index: int) -> tuple[int, str]:
    n = 0
    i = index - 1
    while i >= 0 and not lines[i].strip():
        n += 1
        i -= 1
    return n, lines[i] if i >= 0 else ""


def test_g_no_run_of_three_blank_lines_and_one_final_newline():
    local = _local()
    assert "\n\n\n\n" not in local, "a run of 3+ blank lines"
    assert local.endswith("\n") and not local.endswith("\n\n"), "must end with exactly one newline"
    assert not local.startswith("\n")


def test_g_every_heading_is_preceded_by_a_blank_line():
    lines = _local().split("\n")
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and re.match(r"#{1,6} ", line) and i > 0:
            assert lines[i - 1].strip() == "", f"heading glued to the paragraph above: {line!r}"


_PAIRS = list(zip(LOCAL_IDS, LOCAL_IDS[1:]))


@pytest.mark.parametrize("prev_id,next_id", _PAIRS)
def test_g_boundary_shape_at_each_adjacency(prev_id, next_id):
    """One blank line between components, unless the full document differs at the same boundary."""
    full_ids = _manifest_ids(FULL_MANIFEST)
    # the profile-only components are in no full manifest, so no full-render boundary exists to compare with
    # the Session Start Reading variant stands where its full source stands, so it is compared as its source
    as_full = lambda cid: SSR_ID if cid == SSR_LOCAL_ID else cid  # noqa: E731
    adjacent_in_full = (
        not {BAN_ID, GATE_ID, NOTES_ID} & {prev_id, next_id}
        and full_ids.index(as_full(next_id)) == full_ids.index(as_full(prev_id)) + 1
    )
    expected = 1
    if adjacent_in_full:
        flines = _full().split("\n")
        nf = _first_line(next_id)
        assert flines.count(nf) == 1, f"{next_id}: first line not unique in the full render"
        expected, before = _blank_lines_before(flines, flines.index(nf))
        assert before == _last_line(prev_id)
    lines = _local().split("\n")
    nl = _first_line(next_id)
    assert lines.count(nl) == 1, f"{next_id}: first line not unique in the local render"
    blanks, before = _blank_lines_before(lines, lines.index(nl))
    assert before == _last_line(prev_id), (
        f"{prev_id} does not directly precede {next_id} in the local render"
    )
    assert blanks == expected == 1, (
        f"{prev_id} -> {next_id}: {blanks} blank line(s), expected {expected}"
    )


# --- (i) shorter, none of the dropped headings --------------------------------------------


def _headings(text: str) -> list[str]:
    out, in_fence = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"#{1,6} ", line):
            out.append(line)
    return out


def test_i_local_is_strictly_shorter_and_has_none_of_the_dropped_headings():
    local, full = _local(), _full()
    assert len(local.encode()) < len(full.encode())
    assert len(local.splitlines()) < len(full.splitlines())
    local_headings = set(_headings(local))
    # the Session Start Reading component is carried through its bound variant, so it is not a dropped one
    dropped = set(_manifest_ids(FULL_MANIFEST)) - set(LOCAL_IDS) - {SSR_ID}
    seen = 0
    for cid in sorted(dropped):
        for h in _headings(_component_body(cid)):
            seen += 1
            if h in ("## Model Identity Gate", "## Parallelization and Delegation"):
                continue  # rendered by the profile-only components (heading rule), asserted below
            assert h not in local_headings, f"{cid}: dropped heading {h!r} is in the local render"
    assert seen >= 4
    for named in ("Agent coordination", "Loop-mode policy"):
        assert named in full
        assert named not in local, named
    # the two profile-only components bring their own heading at their absent anchor (heading rule)
    for named in ("Model Identity Gate", "Parallelization and Delegation"):
        assert named in full
        assert ("## " + named) in local_headings
    assert "## ENGRAM Write Discipline" in local_headings
    assert "## ENGRAM Read Discipline" in local_headings



# --- the Session Start Reading section is restored (ruling: cross-compaction continuity) -------

SSR_HEADING = "## Session Start Reading"


def _section(text: str, heading: str) -> str:
    """The lines from `heading` to the next `## ` heading (exclusive), outside code fences."""
    lines = text.split("\n")
    start = lines.index(heading)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).rstrip("\n")


def test_session_start_reading_renders_with_its_heading_and_its_three_numbered_items():
    text = _local()
    assert text.split("\n").count(SSR_HEADING) == 1
    section = _section(text, SSR_HEADING)
    numbered = [ln for ln in section.split("\n") if re.match(r"\d+\. ", ln)]
    assert [ln.split(". ", 1)[0] for ln in numbered] == ["1", "2", "3"], numbered


def test_session_start_reading_section_is_the_full_files_section_with_exactly_the_one_clause_removed():
    full_section = _section(_full(), SSR_HEADING)
    assert full_section.count(SSR_CLAUSE) == 1
    assert _section(_local(), SSR_HEADING) == full_section.replace(SSR_CLAUSE, "")


def _ssr_source_text() -> str:
    return (TEMPLATES / "components" / f"{SSR_ID}.md").read_text(encoding="utf-8")


def test_session_start_reading_local_component_is_the_source_with_exactly_the_one_clause_removed():
    source_fm, source_body = tcr._load_component(SSR_ID)
    fm, body = tcr._load_component(SSR_LOCAL_ID)
    assert source_body.count(SSR_CLAUSE) == 1
    # string-replace on the source, so any other drift (a word, a line, the dream-triage check) fails
    assert body == source_body.replace(SSR_CLAUSE, "")
    assert body != source_body
    for key in ("kind", "harnesses", "surfaces", "anchor", "order", "merge"):
        assert fm[key] == source_fm[key], key
    assert fm["id"] == SSR_LOCAL_ID and fm["version"] >= 1


def test_the_local_render_has_no_writing_style_section_or_pointer_but_keeps_the_rest_of_the_reading_section():
    text = _local()
    assert "Writing style for auto-load surfaces" not in text
    assert "## Writing style" not in text
    section = _section(text, SSR_HEADING)
    assert "If the SessionStart context includes `⚠️ ENGRAM substrate health:` lines" in section
    assert "recall falls back to keyword search (FTS) only." in section
    assert "check that its `## Sleep-end triage (parent)` section covers every item under `## For the awake seat`" in section
    assert "Triage any uncovered item now (do now / file / defer with a reason) before the first task." in section
    assert "The SessionStart hook injects a pointer to this list; this section is the stable copy if the pointer is missing." in section


def test_session_start_reading_local_is_in_the_local_manifest_in_the_full_manifests_place_and_the_full_one_is_not():
    full_ids, ids = _manifest_ids(FULL_MANIFEST), _manifest_ids(LOCAL_MANIFEST)
    assert SSR_LOCAL_ID in ids and SSR_ID not in ids
    assert SSR_LOCAL_ID not in full_ids and SSR_ID in full_ids
    # the full manifest puts the section right after the ENGRAM Reference; so does the local one
    assert full_ids.index(SSR_ID) == full_ids.index("engram-reference") + 1
    assert ids.index(SSR_LOCAL_ID) == ids.index("engram-reference") + 1


# --- the profile is a SUBSET of the full template: every component is a full component or a bound variant of one

# the profile-only components and the full component each is bound to (the binding cells above compare bytes)
BOUND_VARIANT_OF = {
    GATE_ID: "model-identity-gate",
    BAN_ID: "fairy-tool-policy-claude-code",
    NOTES_ID: "cross-surface-persistence",
    SSR_LOCAL_ID: SSR_ID,
}


def test_every_local_component_is_a_full_component_or_a_bound_variant_of_one():
    full_ids = set(_manifest_ids(FULL_MANIFEST))
    ids = _manifest_ids(LOCAL_MANIFEST)
    not_in_full = [i for i in ids if i not in full_ids]
    # the components that are not full components are exactly the ones declared as bound variants
    assert sorted(not_in_full) == sorted(BOUND_VARIANT_OF), not_in_full
    for variant, source in BOUND_VARIANT_OF.items():
        assert variant in ids
        assert source in full_ids, f"{variant} is bound to {source}, which is not a full-manifest component"
        assert (TEMPLATES / "components" / f"{source}.md").is_file()

# --- (c1) and (h): bootstrap ---------------------------------------------------------------


def _bootstrap(tmp_path: Path, args: list[str] | None = None, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    engram_home = tmp_path / ".engram"
    engram_home.mkdir(parents=True, exist_ok=True)
    for fname in (
        "server.py", "engram_client.py", "engram_confidence.py", "engram_filter.py",
        "engram_idf.py", "engram_ids.py", "engram_log_emitter.py", "engram_log_indexer.py",
    ):
        if (SRC / fname).exists():
            shutil.copy2(SRC / fname, engram_home / fname)
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(TEMPLATES),
        "ENGRAM_HOME": str(engram_home),
        "CLAUDE_HOME": str(tmp_path / ".claude"),
        "CODEX_HOME": str(tmp_path / ".codex"),
        "PYTHON_BIN": sys.executable,
        "TARGET": "claude",
    })
    for k in ("ENGRAM_BOOTSTRAP", "FORCE", "FORCE_RESEED_EMPTY"):
        env.pop(k, None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(BOOTSTRAP), *(args or [])],
        env=env, capture_output=True, text=True, timeout=180,
    )


@pytest.fixture(scope="module")
def local_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("local-home")
    r = _bootstrap(home, ["--claude-md-profile", "local"])
    assert r.returncode == 0, r.stdout + r.stderr
    return home


@pytest.fixture(scope="module")
def full_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("full-home")
    r = _bootstrap(home)
    assert r.returncode == 0, r.stdout + r.stderr
    return home


def _config(home: Path) -> dict:
    return json.loads((home / ".engram" / "config.json").read_text(encoding="utf-8"))


def _claude_md(home: Path) -> str:
    return (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")


def test_c1_local_option_records_the_profile_and_writes_the_local_render(local_home, full_home):
    assert _config(local_home)["claude_md_profile"] == "local"
    local_md = _claude_md(local_home)
    assert "Loop-mode policy" not in local_md  # a dropped component; the gate's own heading now renders
    assert "## ENGRAM Write Discipline" in local_md
    assert len(local_md) < len(_claude_md(full_home))


def test_c1_default_records_nothing_and_writes_todays_render(full_home):
    assert "claude_md_profile" not in _config(full_home)
    assert "## Model Identity Gate" in _claude_md(full_home)


def test_c1_explicit_full_records_nothing_and_writes_todays_render(tmp_path):
    r = _bootstrap(tmp_path, ["--claude-md-profile", "full"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "claude_md_profile" not in _config(tmp_path)
    assert "## Model Identity Gate" in _claude_md(tmp_path)


def test_c1_a_default_run_with_no_record_leaves_config_untouched(full_home):
    assert "claude_md_profile" not in _config(full_home)
    assert not list((full_home / ".engram").glob("config.json.engram-backup.*"))


def _call_record_step(tmp_path: Path, profile: str | None, config_text: str | None):
    """Call bootstrap._record_claude_md_profile in a child process against a scratch home.

    A rerun of the whole bootstrap on a seeded home is refused by design (FORCE needs an empty
    graph), so the record step is driven directly, with ENGRAM_HOME monkeypatched to the scratch home.
    """
    home = tmp_path / "scratch-home"
    home.mkdir()
    if config_text is not None:
        (home / "config.json").write_text(config_text, encoding="utf-8")
    code = (
        "import importlib.util, pathlib\n"
        f"spec = importlib.util.spec_from_file_location('bootstrap_mod', {str(BOOTSTRAP)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"m.ENGRAM_HOME = pathlib.Path({str(home)!r})\n"
        f"m._record_claude_md_profile({profile!r})\n"
    )
    env = os.environ.copy()
    env.update({
        "ALPHA_DIR": str(REPO_ROOT), "ALPHA_TEMPLATES_DIR": str(TEMPLATES),
        "ENGRAM_HOME": str(home), "HOME": str(tmp_path),
        "CLAUDE_HOME": str(tmp_path / ".claude"), "CODEX_HOME": str(tmp_path / ".codex"),
        "PYTHON_BIN": sys.executable, "TARGET": "claude",
    })
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    return r, home


@pytest.mark.parametrize("profile", [None, "full"])
def test_c1_a_default_or_full_run_overwrites_a_stale_local_record(tmp_path, profile):
    r, home = _call_record_step(tmp_path, profile, json.dumps({"self_lineage": "x:y", "claude_md_profile": "local"}))
    assert r.returncode == 0, r.stderr
    cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert cfg["claude_md_profile"] == "full" and cfg["self_lineage"] == "x:y"


@pytest.mark.parametrize("profile", [None, "full"])
def test_c1_a_default_run_with_no_record_writes_nothing(tmp_path, profile):
    r, home = _call_record_step(tmp_path, profile, json.dumps({"self_lineage": "x:y"}))
    assert r.returncode == 0, r.stderr
    assert json.loads((home / "config.json").read_text(encoding="utf-8")) == {"self_lineage": "x:y"}
    assert not list(home.glob("config.json.engram-backup.*"))


def test_c1_a_non_dict_config_is_refused_not_a_traceback(tmp_path):
    r, _ = _call_record_step(tmp_path, "local", "5")
    assert r.returncode != 0 and "Traceback" not in r.stderr
    assert "REFUSED" in r.stderr and "CLAUDE.md was not written" in r.stderr


def test_c1_missing_config_refusal_says_the_graph_was_already_seeded(tmp_path):
    r, _ = _call_record_step(tmp_path, "local", None)
    assert r.returncode != 0
    msg = r.stderr + r.stdout
    assert "REFUSED" in msg and "is missing" in msg
    assert "already seeded" in msg and "CLAUDE.md was not written" in msg


def test_c1_unknown_profile_is_refused_before_anything_is_written(tmp_path):
    r = _bootstrap(tmp_path, ["--claude-md-profile", "../x"])
    assert r.returncode != 0
    assert "claude-md-profile" in r.stderr + r.stdout
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert not (tmp_path / ".engram" / "knowledge.db").exists()


def test_c1_the_option_needs_a_value(tmp_path):
    r = _bootstrap(tmp_path, ["--claude-md-profile"])
    assert r.returncode != 0
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()


def test_h_local_file_leaves_the_same_unresolved_placeholders_as_the_full_file(local_home, full_home):
    local_md, full_md = _claude_md(local_home), _claude_md(full_home)
    # The run really rendered the local file (an option that is ignored would pass the set check).
    assert "Loop-mode policy" not in local_md and "Loop-mode policy" in full_md
    local_left = set(PLACEHOLDER.findall(local_md))
    full_left = set(PLACEHOLDER.findall(full_md))
    assert local_left == full_left, (local_left, full_left)
    # What bootstrap resolves is resolved in the local file too; the names left are the ones the
    # first-session skill fills in, in both files.
    for name in ("{{ENGRAM_HOME}}", "{{TODAY}}", "{{AX_HONESTY}}", "{{DF_EPISTEMIC_IDENTITY}}"):
        assert name not in local_md


# --- (c2)-(c4): the upgrade flow's template-gate ------------------------------------------

from tools.engine import flows  # noqa: E402

FULL_REL = "src/engram/templates/template.CLAUDE.md"
LOCAL_REL = "src/engram/templates/template.CLAUDE.local.md"


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _upgrade_fixture(tmp_path: Path, *, profile, change: str):
    """A source repo with both templates at commit A; `change` ('full', 'local', 'both') edits at commit B."""
    src = tmp_path / "source"
    src.mkdir()
    _git(src, "init", "-b", "dev")
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "T")
    _write(src / FULL_REL, "FULL TEMPLATE v1\n")
    _write(src / LOCAL_REL, "LOCAL TEMPLATE v1\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "a")
    a = _git(src, "rev-parse", "HEAD")
    if change in ("full", "both"):
        _write(src / FULL_REL, "FULL TEMPLATE v2\n")
    if change in ("local", "both"):
        _write(src / LOCAL_REL, "LOCAL TEMPLATE v2\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "b")
    home = tmp_path / "engram_home"
    home.mkdir()
    cfg: dict = {"install_tier": "convenience"}
    if profile is not None:
        cfg["claude_md_profile"] = profile
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    ctx = {
        "engram_home": str(home),
        "source_dir": str(src),
        "target_sha": "HEAD",
        "_deployed_sha": a,
    }
    return ctx


def _run_gate(ctx):
    flows._apply_changeset_reviewed(ctx)
    flows._apply_template_gate(ctx)
    return ctx


def test_c2_local_home_full_template_changed_only_reports_no_change(tmp_path):
    ctx = _run_gate(_upgrade_fixture(tmp_path, profile="local", change="full"))
    assert ctx["_template_changed"] is False
    assert ctx["_template_diff"] is None
    assert ctx["_template_gate_done"] is True


def test_c2_local_home_local_template_changed_surfaces_the_local_template(tmp_path):
    ctx = _run_gate(_upgrade_fixture(tmp_path, profile="local", change="local"))
    assert ctx["_template_changed"] is True
    assert "LOCAL TEMPLATE v2" in ctx["_template_diff"]
    assert "FULL TEMPLATE" not in ctx["_template_diff"]


def test_c2_local_home_both_changed_surfaces_only_the_local_template(tmp_path):
    ctx = _run_gate(_upgrade_fixture(tmp_path, profile="local", change="both"))
    assert "LOCAL TEMPLATE v2" in ctx["_template_diff"]
    assert "FULL TEMPLATE" not in ctx["_template_diff"]


def test_c2_full_home_local_template_changed_only_reports_no_change(tmp_path):
    for profile in (None, "full"):
        sub = tmp_path / f"p-{profile}"
        sub.mkdir()
        ctx = _run_gate(_upgrade_fixture(sub, profile=profile, change="local"))
        assert ctx["_template_changed"] is False, profile
        assert ctx["_template_diff"] is None, profile


def test_c3_home_with_no_recorded_profile_behaves_as_today(tmp_path):
    ctx = _run_gate(_upgrade_fixture(tmp_path, profile=None, change="full"))
    assert ctx["_template_changed"] is True
    assert "FULL TEMPLATE v2" in ctx["_template_diff"]
    assert "LOCAL TEMPLATE" not in ctx["_template_diff"]


def test_c3_home_with_no_config_json_behaves_as_today(tmp_path):
    ctx = _upgrade_fixture(tmp_path, profile=None, change="full")
    os.remove(Path(ctx["engram_home"]) / "config.json")
    _run_gate(ctx)
    assert "FULL TEMPLATE v2" in ctx["_template_diff"]


@pytest.mark.parametrize("bad", ["weird", "lean", "../local", 7, ["local"], ""])
def test_c3_an_unrecognized_recorded_profile_never_offers_either_template(tmp_path, bad):
    ctx = _run_gate(_upgrade_fixture(tmp_path, profile=bad, change="both"))
    assert ctx["_template_gate_done"] is True
    diff = ctx["_template_diff"]
    assert diff is not None and "claude_md_profile" in diff
    assert "TEMPLATE v" not in diff


def test_c3_an_unreadable_config_never_offers_either_template(tmp_path):
    ctx = _upgrade_fixture(tmp_path, profile="local", change="both")
    (Path(ctx["engram_home"]) / "config.json").write_text("{not json", encoding="utf-8")
    _run_gate(ctx)
    assert "TEMPLATE v" not in (ctx["_template_diff"] or "")


@pytest.mark.parametrize("profile", [None, "local"])
def test_c4_the_gate_never_writes_the_users_claude_md(tmp_path, profile, monkeypatch):
    ctx = _upgrade_fixture(tmp_path, profile=profile, change="both")
    claude_dir = tmp_path / "fake_home" / ".claude"
    claude_dir.mkdir(parents=True)
    claude_md = claude_dir / "CLAUDE.md"
    claude_md.write_bytes(b"MY OWN INSTRUCTIONS\n")
    before_listing = sorted(os.listdir(claude_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
    _run_gate(ctx)
    assert claude_md.read_bytes() == b"MY OWN INSTRUCTIONS\n"
    assert sorted(os.listdir(claude_dir)) == before_listing, "the gate must not create a backup or a new file"


# --- (d) tiers -----------------------------------------------------------------------------


def test_d_the_new_shippables_are_tiered():
    tiers = json.loads((REPO_ROOT / "src" / "build" / "packaging" / "tiers.json").read_text(encoding="utf-8"))
    entries = {e["path"]: e for e in tiers["shippables"]} if "shippables" in tiers else None
    if entries is None:  # shape differs: fall back to a flat scan for every dict with a path
        entries = {}

        def walk(node):
            if isinstance(node, dict):
                if "path" in node and "tier" in node:
                    entries[node["path"]] = node
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(tiers)
    full = entries["templates/template.CLAUDE.md"]
    local = entries["templates/template.CLAUDE.local.md"]
    assert local["tier"] == full["tier"], "bootstrap reads the local file, so it ships where the full one does"
    assert "templates/manifests" in entries


# --- round 3: Codex ignores the option; the upgrade skill's Step 2 snippet ----------------------


def test_c1_codex_target_with_local_option_records_no_profile(tmp_path):
    r = _bootstrap(tmp_path, ["--claude-md-profile", "local"], extra_env={"TARGET": "codex"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ignored" in r.stdout + r.stderr
    cfg_path = tmp_path / ".engram" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    assert "claude_md_profile" not in cfg


UPGRADE_SKILL = SRC / "skills" / "claude" / "engram-upgrade" / "SKILL.md"


def _step2_snippet() -> str:
    """The shell block in Step 2 that picks the template by the recorded profile, read from the skill text."""
    text = UPGRADE_SKILL.read_text(encoding="utf-8")
    for block in re.findall(r"```bash\n(.*?)```", text, flags=re.S):
        if "TEMPLATE=template.CLAUDE.md" in block:
            return block
    raise AssertionError("Step 2 template-picking snippet not found in the upgrade SKILL.md")


def _pick_template(shell: str, home: Path, config_text: str | None) -> str:
    home.mkdir(parents=True, exist_ok=True)
    if config_text is not None:
        (home / "config.json").write_text(config_text, encoding="utf-8")
    # Run only the picking lines; the git log line needs a source repo and is not under test.
    lines = [ln for ln in _step2_snippet().splitlines() if ln.startswith(("TEMPLATE=", "grep -Eq"))]
    assert len(lines) == 2, lines
    script = "\n".join(lines) + '\nprintf %s "$TEMPLATE"\n'
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(home)
    r = subprocess.run([shell, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


_STEP2_SHELLS = [s for s in ("/bin/bash", "/bin/sh") if os.path.exists(s)]


@pytest.mark.parametrize("shell", _STEP2_SHELLS)
@pytest.mark.parametrize(
    "config_text, expected",
    [
        ('{"claude_md_profile": "local"}', "template.CLAUDE.local.md"),
        ('{"install_tier": "convenience"}', "template.CLAUDE.md"),
        (None, "template.CLAUDE.md"),
        ('{"claude_md_profile": "tiny"}', "template.CLAUDE.md"),
        ('{"claude_md_profile": "lean"}', "template.CLAUDE.md"),
    ],
    ids=["local", "unrecorded", "no-config", "unrecognised", "retired-working-name"],
)
def test_upgrade_skill_step2_snippet_picks_the_template(tmp_path, shell, config_text, expected):
    assert _pick_template(shell, tmp_path / "home", config_text) == expected


def test_render_reads_the_named_template_even_if_the_sources_list_widens(tmp_path, monkeypatch):
    import template_render as tr

    (tmp_path / "template.CLAUDE.local.md").write_text("LOCAL", encoding="utf-8")
    (tmp_path / "template.CLAUDE.md").write_text("FULL", encoding="utf-8")
    (tmp_path / "other.md").write_text("OTHER", encoding="utf-8")
    monkeypatch.setitem(tr.CLAUDE_MD_PROFILE_SOURCES, "local", ["other.md", "template.CLAUDE.local.md"])
    assert tr.render_identity_surface(tmp_path, "local") == "LOCAL"
    for profile, name in tr.CLAUDE_MD_PROFILE_TEMPLATE.items():
        assert name in tr.CLAUDE_MD_PROFILE_SOURCES[profile]


# --- docs pins (round 5: #4802 design-seat findings) ----------------------------------------

README_AGENT = REPO_ROOT / "README-AGENT.md"
README = REPO_ROOT / "README.md"
FIRST_SESSION_SKILL = SRC / "skills" / "claude" / "engram-first-session" / "SKILL.md"


def test_first_session_skill_does_not_say_the_local_file_has_no_model_identity_gate_section():
    """The local render HAS a `## Model Identity Gate` section, so the skill must not deny it."""
    assert "## Model Identity Gate" in _local().splitlines()
    skill = " ".join(FIRST_SESSION_SKILL.read_text(encoding="utf-8").split())
    assert "has no such section" not in skill


def test_first_session_skill_pins_the_true_five_lines_claim_against_the_local_render():
    """The skill says the local gate section holds five lines; the local render must keep that true."""
    skill = " ".join(FIRST_SESSION_SKILL.read_text(encoding="utf-8").split())
    assert "in a local-profile file that section holds five lines" in skill
    assert "two lines" not in skill.split("Model Identity Gate", 1)[1][:600]
    assert "`engram-model-switch-read`" in skill.split("Model Identity Gate", 1)[1][:600]
    lines = _local().splitlines()
    start = lines.index("## Model Identity Gate") + 1
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("## "))
    assert len([ln for ln in lines[start:end] if ln.strip()]) == 5


def _bootstrap_profile_facts() -> dict:
    """CLAUDE_MD_PROFILES and the profile an option-less parse yields, read from bootstrap.py."""
    code = (
        "import importlib.util, json\n"
        f"spec = importlib.util.spec_from_file_location('bootstrap_mod', {str(BOOTSTRAP)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print(json.dumps({'profiles': list(m.CLAUDE_MD_PROFILES),"
        " 'default': m._parse_args([]).claude_md_profile}))\n"
    )
    scratch = Path(tempfile.mkdtemp())
    env = os.environ.copy()
    env.update({
        "HOME": str(scratch), "ENGRAM_HOME": str(scratch / ".engram"),
        "CLAUDE_HOME": str(scratch / ".claude"), "CODEX_HOME": str(scratch / ".codex"),
        "ALPHA_DIR": str(REPO_ROOT), "ALPHA_TEMPLATES_DIR": str(TEMPLATES),
        "PYTHON_BIN": sys.executable, "TARGET": "claude",
    })
    try:
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _install_doc_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if "--claude-md-profile" in ln]


def test_install_doc_names_the_flag_and_every_profile_bootstrap_accepts_and_no_other():
    profiles = _bootstrap_profile_facts()["profiles"]
    lines = _install_doc_lines(README_AGENT)
    assert lines, "README-AGENT.md install steps must name --claude-md-profile"
    text = " ".join(lines)
    for p in profiles:
        assert re.search(rf"--claude-md-profile\s+`?{re.escape(p)}\b|`{re.escape(p)}`", text), p
    # every value written right after the flag is one bootstrap accepts
    for value in re.findall(r"--claude-md-profile[ =]`?([A-Za-z0-9_-]+)", text):
        assert value in profiles, f"doc names profile {value!r} that bootstrap refuses"
    assert "claude_md_profile" in text
    # the line names the INSTALLER's flag, and says bootstrap is what checks the value
    assert "install.sh --claude-md-profile" in text
    assert re.search(r"bootstrap[^.;]*\bchecks\b", text)


def test_install_doc_default_is_the_profile_bootstrap_uses_with_no_option():
    import template_render as tr

    facts = _bootstrap_profile_facts()
    assert facts["default"] is None  # no option -> no profile recorded -> the unprofiled render
    default_profiles = [p for p, name in tr.CLAUDE_MD_PROFILE_TEMPLATE.items() if name == "template.CLAUDE.md"]
    assert default_profiles == ["full"]
    text = " ".join(_install_doc_lines(README_AGENT))
    assert re.search(r"default[^.]*\bfull\b|\bfull\b[^.]*default", text, re.IGNORECASE)
    for p in facts["profiles"]:
        if p != "full":
            assert not re.search(rf"default\s+(?:is\s+)?`{re.escape(p)}`|`{re.escape(p)}`\s+is\s+the\s+default", text)


def test_readme_runs_no_bootstrap_of_its_own_so_it_carries_no_profile_line():
    """README.md has no bootstrap.py install step; if one appears it must name the flag too."""
    assert "bootstrap.py" not in README.read_text(encoding="utf-8") or _install_doc_lines(README)



# --- the one-line header: a manifest-level field, not a component (#4802 rework) ----------------

HEADER = (
    "> Profile: local. A purely local, single-agent setup: one agent working with one user on one machine, "
    "no agent team, no long-running autonomous loop. Not fully tested. Made for the local benchmark effort "
    "(https://github.com/engram-agents/engram-benchmarks); see README-AGENT.md."
)


def _scratch_with_manifest_edit(tmp_path, monkeypatch, edit):
    """A scratch copy of the templates whose local manifest is passed through `edit` (a dict -> None)."""
    scratch = _scratch(tmp_path, monkeypatch)
    path = scratch / "manifests" / "claude-code.CLAUDE.md.local.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    edit(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_the_manifest_carries_the_header_and_the_header_is_one_line_naming_profile_and_purpose():
    header = json.loads(LOCAL_MANIFEST.read_text(encoding="utf-8"))["header"]
    assert header == HEADER
    assert "\n" not in header and "\r" not in header
    assert header.startswith("> Profile: local. ")


def test_the_rendered_files_first_line_is_the_header_then_a_blank_line_then_the_identity_opening():
    lines = _local().split("\n")
    assert lines[0] == HEADER
    assert lines[1] == ""
    assert lines[2].startswith("# ")
    # and the committed file starts the same way
    assert LOCAL_TEMPLATE.read_text(encoding="utf-8").split("\n")[0] == HEADER


def test_the_header_sentence_is_pinned_word_for_word():
    text = LOCAL_TEMPLATE.read_text(encoding="utf-8")
    assert (
        "A purely local, single-agent setup: one agent working with one user on one machine, no agent team, "
        "no long-running autonomous loop. Not fully tested. Made for the local benchmark effort "
        "(https://github.com/engram-agents/engram-benchmarks); see README-AGENT.md."
    ) in text.split("\n")[0]


def test_the_header_is_in_no_component_and_not_in_the_full_render_or_full_manifest():
    assert "header" not in json.loads(FULL_MANIFEST.read_text(encoding="utf-8"))
    assert "Profile: local" not in _full()
    assert not _full().startswith(">")
    for path in (TEMPLATES / "components").glob("*.md"):
        assert "Profile: local" not in path.read_text(encoding="utf-8"), path.name
    assert [c["id"] for c in tcr.render("claude-code", "CLAUDE.md", "local")[1]["components"]] == LOCAL_IDS


def test_a_manifest_without_a_header_renders_exactly_as_before(tmp_path, monkeypatch):
    with_header = _local()
    _scratch_with_manifest_edit(tmp_path, monkeypatch, lambda m: m.pop("header"))
    without = tcr.render("claude-code", "CLAUDE.md", "local")[0]
    assert without == with_header[len(HEADER) + 2 :]
    assert without.startswith("# ") and not without.startswith(">")
    # the default manifest has none: its render is the committed full template, byte for byte
    assert tcr.render("claude-code", "CLAUDE.md")[0] == FULL_TEMPLATE.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "bad",
    [
        "> two\nlines",
        "> carriage\rreturn",
        "",
        "   ",
        7,
        None,
        ["> a list"],
        "> a made-up placeholder {{NOT_A_REAL_PLACEHOLDER}}",
    ],
    ids=["newline", "cr", "empty", "blank", "int", "null", "list", "unknown-placeholder"],
)
def test_a_malformed_header_is_a_build_failure_not_a_silent_render(tmp_path, monkeypatch, bad):
    _scratch_with_manifest_edit(tmp_path, monkeypatch, lambda m: m.__setitem__("header", bad))
    with pytest.raises(tcr.TemplateComponentError, match="header"):
        tcr.render("claude-code", "CLAUDE.md", "local")


def test_a_header_may_use_a_placeholder_the_components_already_use(tmp_path, monkeypatch):
    _scratch_with_manifest_edit(
        tmp_path, monkeypatch, lambda m: m.__setitem__("header", "> Profile: local, for {{USER_NAME}}.")
    )
    assert tcr.render("claude-code", "CLAUDE.md", "local")[0].split("\n")[0] == (
        "> Profile: local, for {{USER_NAME}}."
    )


def test_the_default_manifest_with_a_header_is_refused_so_the_full_render_cannot_change(tmp_path, monkeypatch):
    scratch = _scratch(tmp_path, monkeypatch)
    path = scratch / "manifests" / "claude-code.CLAUDE.md.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["header"] = "> a header on the full manifest"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(tcr.TemplateComponentError, match="header"):
        tcr.render("claude-code", "CLAUDE.md")


# --- the purpose-and-limits text: four places, the same facts ---------------------------------------

BENCH_URL = "https://github.com/engram-agents/engram-benchmarks"
_BANNED_IN_PURPOSE_TEXT = (r"small[- ]context", r"\blean\b", r"local[- ]only")
_KEPT_TERMS = ("authority table", "trust-tier pointer", "signature rule")


def _purpose_text_readme() -> str:
    """README-AGENT.md's profile section: from the install line naming the flag to the next heading."""
    lines = README_AGENT.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if "install.sh --claude-md-profile" in ln)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#")), len(lines))
    return "\n".join(lines[start:end])


def _purpose_text_help() -> str:
    r = subprocess.run(
        ["bash", str(SRC / "tools" / "install.sh"), "--help"], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("--claude-md-profile"))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("--")), len(lines))
    return "\n".join(lines[start:end])


@pytest.fixture(scope="module")
def local_run(tmp_path_factory):
    home = tmp_path_factory.mktemp("local-run")
    r = _bootstrap(home, ["--claude-md-profile", "local"])
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def _purpose_text_bootstrap(run) -> str:
    """The paragraph bootstrap prints when the profile is chosen: from its first line to the first blank line."""
    lines = run.stdout.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("CLAUDE.md profile: local"))
    end = next((i for i in range(start + 1, len(lines)) if not lines[i].strip()), len(lines))
    return "\n".join(lines[start:end])


def _purpose_text_changelog() -> str:
    text = (REPO_ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    entries = [e for e in re.split(r"\n(?=- )", text) if e.startswith("- A `local` CLAUDE.md profile")]
    assert len(entries) == 1, len(entries)
    return entries[0]


_PLACES = ["readme", "help", "bootstrap", "changelog"]


def _place_text(place, local_run) -> str:
    if place == "readme":
        return _purpose_text_readme()
    if place == "help":
        return _purpose_text_help()
    if place == "bootstrap":
        return _purpose_text_bootstrap(local_run)
    return _purpose_text_changelog()


@pytest.mark.parametrize("place", _PLACES)
def test_purpose_text_names_one_agent_one_user_one_machine_not_fully_tested_and_the_benchmark_repo(place, local_run):
    text = " ".join(_place_text(place, local_run).split())
    low = text.lower()
    assert "one agent" in low and "one user" in low and "one machine" in low, text
    assert "not fully tested" in low, text
    assert BENCH_URL in text, text


@pytest.mark.parametrize("place", _PLACES)
def test_purpose_text_never_says_small_context_lean_or_local_only(place, local_run):
    text = _place_text(place, local_run)
    for banned in _BANNED_IN_PURPOSE_TEXT:
        assert not re.search(banned, text, re.IGNORECASE), (place, banned)


@pytest.mark.parametrize("place", ["readme", "bootstrap", "changelog"])
def test_purpose_text_says_the_authority_table_trust_tier_pointer_and_signature_rule_are_kept(place, local_run):
    text = " ".join(_place_text(place, local_run).split())
    low = text.lower()
    for term in _KEPT_TERMS:
        assert term in low, (place, term)
    assert re.search(r"\b(keeps|kept)\b", low), place


@pytest.mark.parametrize("place", ["readme", "bootstrap", "changelog"])
def test_purpose_text_says_what_the_profile_does_not_carry_and_what_it_was_not_measured_with(place, local_run):
    low = " ".join(_place_text(place, local_run).split()).lower()
    for named in ("agent coordination", "loop-mode policy", "who i am", "my collaborator", "infrastructure locations"):
        assert named in low, (place, named)
    for unmeasured in ("fresh home", "real upgrade", "whole test suite"):
        assert unmeasured in low, (place, unmeasured)
    # restored by the Session Start Reading ruling: it is carried, so it is never listed as dropped
    dropped_part = low.split("does not carry", 1)[1] if "does not carry" in low else ""
    assert "session start reading" not in dropped_part.split("not fully tested")[0], place


def test_the_bootstrap_paragraph_is_at_most_six_lines_and_only_for_the_local_choice(local_run, tmp_path):
    assert len(_purpose_text_bootstrap(local_run).splitlines()) <= 6
    default = _bootstrap(tmp_path)
    assert default.returncode == 0, default.stdout + default.stderr
    assert "CLAUDE.md profile: local" not in default.stdout


def test_the_bootstrap_paragraph_is_not_printed_for_a_codex_run_that_ignores_the_option(tmp_path):
    r = _bootstrap(tmp_path, ["--claude-md-profile", "local"], extra_env={"TARGET": "codex"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CLAUDE.md profile: local" not in r.stdout

# --- the working name `lean` is retired, not aliased (#4802 rework) -------------------------------


def test_bootstrap_refuses_the_retired_working_name_and_lists_the_real_profiles(tmp_path):
    r = _bootstrap(tmp_path, ["--claude-md-profile", "lean"])
    assert r.returncode != 0
    msg = r.stderr + r.stdout
    assert "--claude-md-profile needs one of full, local" in msg
    assert "'lean'" in msg  # the refused value is echoed back
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert not (tmp_path / ".engram" / "knowledge.db").exists()


def test_bootstrap_profile_list_is_full_and_local_and_nothing_else():
    assert _bootstrap_profile_facts()["profiles"] == ["full", "local"]


def test_a_recorded_retired_working_name_reads_as_undetermined_in_the_renderer_module(tmp_path):
    import template_render as tr

    (tmp_path / "config.json").write_text(json.dumps({"claude_md_profile": "lean"}), encoding="utf-8")
    assert tr.recorded_claude_md_profile(str(tmp_path)) == tr.PROFILE_UNDETERMINED
    (tmp_path / "config.json").write_text(json.dumps({"claude_md_profile": "local"}), encoding="utf-8")
    assert tr.recorded_claude_md_profile(str(tmp_path)) == "local"


def test_the_profile_map_has_no_key_for_the_retired_working_name():
    import template_render as tr

    assert sorted(tr.CLAUDE_MD_PROFILE_SOURCES) == ["full", "local"]
    assert sorted(tr.CLAUDE_MD_PROFILE_TEMPLATE) == ["full", "local"]
    with pytest.raises(tcr.TemplateComponentError):
        tcr.render("claude-code", "CLAUDE.md", "lean")


# --- the retired axis word stays out of the profile's own surface (#4802 round 2) --------------
# Lei's 11-17 ruling (relayed by Kepler) allows ONE sentence to carry the retired word: "a purely local,
# single-agent setup". Every other hit under the shipped trees is a line of the retirement machinery (#4255).
# A new use of the word, in any profile file, fails here.

_SENTENCE = "a purely local, single-agent setup"
_LEI_LINES = (
    # the manifest's header field and the rendered file's first line carry the sentence
    '"header": "' + HEADER + '",',
    HEADER,
)
# retirement machinery that predates the profile: (path, whole stripped line)
_RETIRED_AXIS_LINES = (
    ("src/engram/tools/install.sh", '--multi-agent|--single-agent) _retired_flag "$1"; shift ;;'),
)
# whole files outside the profile's surface whose hits are the retirement machinery (#4255), policed by their own gates
_RETIRED_AXIS_FILES = ("tools/test_pre_dispatch_check.py", "tools/claude_md_moves.json")
_SCANNED_PATHS = ("src", "tools", "README-AGENT.md")


def _retired_word_hits():
    out = subprocess.run(
        ["git", "grep", "-n", "-i", "-E", r"single[-_ ]agent", "--", *_SCANNED_PATHS],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert out.returncode in (0, 1), out.stderr
    hits = []
    for raw in out.stdout.splitlines():
        path, lineno, text = raw.split(":", 2)
        hits.append((path, int(lineno), text.strip()))
    return hits


def _allowed_hit(path, text):
    if text in _LEI_LINES:
        return True
    if path in _RETIRED_AXIS_FILES:
        return True
    return any(path == p and text == t for p, t in _RETIRED_AXIS_LINES)


def test_retired_word_appears_only_in_leis_sentence_lines_and_the_retirement_machinery():
    bad = [f"{p}:{n}: {t[:110]}" for p, n, t in _retired_word_hits() if not _allowed_hit(p, t)]
    assert not bad, "the retired word outside the allowed lines:\n" + "\n".join(bad)


def test_every_line_that_carries_leis_sentence_carries_it_exactly():
    # the allowed lines are exact: the sentence is not a licence for a looser line that merely contains the word
    lei = [(p, t) for p, _n, t in _retired_word_hits() if _SENTENCE in t.lower()]
    assert lei, "the header sentence is gone"
    assert all(t in _LEI_LINES for _p, t in lei), lei
    assert sorted({p for p, _t in lei}) == [
        "src/engram/templates/manifests/claude-code.CLAUDE.md.local.json",
        "src/engram/templates/template.CLAUDE.local.md",
    ]
