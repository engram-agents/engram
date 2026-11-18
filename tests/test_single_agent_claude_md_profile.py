"""The `single-agent` CLAUDE.md profile: a second manifest over the existing components.

`single-agent` is for one agent working with one user. It is a second, named rendering of the
identity template: the same component files, fewer of them. These cells pin the
contract written before the implementation:

  (a) render(profile=None) is today's bytes; the existing byte-identical pin is untouched.
  (b) an unknown or path-shaped profile name raises and never falls back to the full template.
  (c) bootstrap records the choice in config.json; the upgrade flow compares a home's file
      against the template OF THE RECORDED PROFILE, never offers the other one, and never
      writes the user's CLAUDE.md.
  (d) the new shippables are tiered.
  (e) the single-agent render keeps the consent paragraph and the authority table.
  (f) the dead-component rule still works with two manifests.
  (g) the joins at the new adjacencies are clean.
  (h) bootstrap resolves the single-agent file's placeholders the way it resolves the full one's.
  (i) the single-agent render is shorter and carries none of the dropped components' headings.

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
SINGLE_AGENT_TEMPLATE = TEMPLATES / "template.CLAUDE.single-agent.md"
SINGLE_AGENT_MANIFEST = TEMPLATES / "manifests" / "claude-code.CLAUDE.md.single-agent.json"
FULL_MANIFEST = TEMPLATES / "manifests" / "claude-code.CLAUDE.md.json"
BOOTSTRAP = SRC / "bootstrap.py"

SINGLE_AGENT_IDS = [
    "identity-opening",
    "user-notes-verbatim-single-agent",
    "authority-structure-intro",
    "authority-structure-table-claude-code",
    "authority-structure-close",
    "trust-tier-pointer",
    "model-identity-gate-single-agent",
    "engram-write-discipline",
    "engram-read-discipline",
    "confidence-anchors",
    "native-tools-ban-single-agent",
    "engram-reference",
    "session-start-reading",
]

BAN_ID = "native-tools-ban-single-agent"
GATE_ID = "model-identity-gate-single-agent"
NOTES_ID = "user-notes-verbatim-single-agent"
# every single-agent-only component: in the single-agent manifest, in no full manifest
SINGLE_AGENT_ONLY_IDS = (BAN_ID, GATE_ID, NOTES_ID)

PLACEHOLDER = re.compile(r"\{\{[^}]+\}\}")


def _manifest_ids(path: Path) -> list[str]:
    return [e["id"] for e in json.loads(path.read_text(encoding="utf-8"))["components"]]


def _single_agent() -> str:
    return tcr.render("claude-code", "CLAUDE.md", "single-agent")[0]


def _full() -> str:
    return tcr.render("claude-code", "CLAUDE.md")[0]


# --- (a) default is today's bytes ---------------------------------------------------------


def test_a_profile_none_is_todays_bytes_positional_and_keyword():
    expected = FULL_TEMPLATE.read_text(encoding="utf-8")
    assert tcr.render("claude-code", "CLAUDE.md")[0] == expected
    assert tcr.render("claude-code", "CLAUDE.md", None)[0] == expected
    assert tcr.render("claude-code", "CLAUDE.md", profile=None)[0] == expected


# --- (b) refusals --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["nonexistent", "Single-agent2", "single-agent-x", "full"])
def test_b_unknown_profile_raises_and_never_falls_back(name):
    with pytest.raises(tcr.TemplateComponentError):
        tcr.render("claude-code", "CLAUDE.md", name)


@pytest.mark.parametrize(
    "name",
    ["../x", "a/b", "", ".", "..", "a\\b", "single-agent/../single-agent", "../../etc/passwd", "single-agent.json", " single-agent", "single-agent\n", "/abs"],
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


# --- the single-agent manifest and the committed single-agent template ------------------------------------


def test_single_agent_manifest_is_the_listed_ids_in_the_full_manifests_relative_order():
    single_agent_ids = _manifest_ids(SINGLE_AGENT_MANIFEST)
    assert single_agent_ids == SINGLE_AGENT_IDS
    full_ids = _manifest_ids(FULL_MANIFEST)
    # The single-agent-only components have no place in the full manifest; the rest keep its relative order.
    assert [i for i in single_agent_ids if i not in SINGLE_AGENT_ONLY_IDS] == [i for i in full_ids if i in set(single_agent_ids)], (
        "the single-agent manifest must keep the full manifest's relative order"
    )
    # The ban sits where its host section sits in the full file: after the confidence anchors,
    # before the ENGRAM Reference.
    assert single_agent_ids.index(BAN_ID) == single_agent_ids.index("confidence-anchors") + 1
    assert single_agent_ids.index(BAN_ID) == single_agent_ids.index("engram-reference") - 1
    # Each sentence sits where its host section sits in the full file: the gate sentence before the
    # write discipline (the full file has model-identity-gate just before it), the notes sentence
    # between the write and read disciplines (cross-surface-persistence's place).
    assert single_agent_ids.index(GATE_ID) == single_agent_ids.index("engram-write-discipline") - 1
    assert single_agent_ids.index(NOTES_ID) == single_agent_ids.index("identity-opening") + 1
    assert single_agent_ids.index(NOTES_ID) == single_agent_ids.index("authority-structure-intro") - 1


def test_single_agent_render_receipt_lists_the_manifest_ids():
    _, receipt = tcr.render("claude-code", "CLAUDE.md", "single-agent")
    assert [c["id"] for c in receipt["components"]] == SINGLE_AGENT_IDS
    assert receipt["excluded"] == []


def test_single_agent_template_is_pinned_byte_identical_to_the_render():
    assert SINGLE_AGENT_TEMPLATE.read_bytes() == _single_agent().encode("utf-8"), (
        "template.CLAUDE.single-agent.md is generated by render('claude-code','CLAUDE.md','single-agent'); "
        "regenerate it with the renderer, never by hand"
    )


# --- the single-agent-only native-tools ban ---------------------------------------------------------

BAN_START = "**BANNED"


def _full_ban_paragraph() -> str:
    """The ban paragraph of the FULL component file, read at test time: from `**BANNED` to the next blank line."""
    text = (TEMPLATES / "components" / "fairy-tool-policy-claude-code.md").read_text(encoding="utf-8")
    start = text.index(BAN_START)
    assert text.count(BAN_START) == 1
    end = text.find("\n\n", start)
    return text[start:end]


def test_ban_single_agent_component_body_equals_the_full_components_paragraph_byte_for_byte():
    para = _full_ban_paragraph()
    assert para.strip() and "NATIVE deep-research" in para and "`Workflow`" in para
    fm, body = tcr._load_component(BAN_ID)
    # heading rule: a single-agent-only component at an absent anchor renders as its own heading, then the paragraph;
    # the body owns its trailing blank line (whitespace-ownership rule): paragraph, then one "\n"
    assert body == "## " + fm["anchor"] + "\n\n" + para + "\n"


def test_ban_single_agent_component_is_in_the_single_agent_manifest_and_not_in_the_full_manifest():
    assert BAN_ID in _manifest_ids(SINGLE_AGENT_MANIFEST)
    assert BAN_ID not in _manifest_ids(FULL_MANIFEST)


def test_ban_paragraph_renders_exactly_once_in_single_agent_and_exactly_once_in_full():
    para = _full_ban_paragraph()
    assert _single_agent().count(para) == 1
    assert _full().count(para) == 1


# --- the two single-agent-only sentences (model identity gate; notes stay verbatim) ----------------


def _gate_sentence() -> str:
    """The last sentence of the Mismatch bullet, read at test time from the FULL component file."""
    text = (TEMPLATES / "components" / "model-identity-gate.md").read_text(encoding="utf-8")
    bullets = [ln for ln in text.split("\n") if ln.startswith("- **Mismatch**")]
    assert len(bullets) == 1
    return bullets[0].rsplit(". ", 1)[1]


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
    pytest.param(GATE_ID, _gate_sentence, "silent swap", id="gate"),
    pytest.param(NOTES_ID, _notes_sentence, "Never edit or summarize them.", id="notes"),
]


@pytest.mark.parametrize("cid,get_sentence,marker", _SENTENCES)
def test_single_agent_sentence_component_body_equals_the_full_files_sentence_byte_for_byte(cid, get_sentence, marker):
    sentence = get_sentence()
    assert sentence.strip() and marker in sentence and "\n" not in sentence
    fm, body = tcr._load_component(cid)
    # whitespace-ownership rule: the body owns its trailing blank line, so it ends "\n" after the sentence.
    if cid == GATE_ID:
        # heading rule: the anchor heading is absent from the single-agent file, so the component brings its own
        assert body == "## " + fm["anchor"] + "\n\n" + sentence + "\n"
    else:
        # the notes sentence keeps the source's scope line (the line heading its list), then the sentence
        assert body == _scope_line() + "\n\n" + sentence + "\n"


@pytest.mark.parametrize("cid,get_sentence,marker", _SENTENCES)
def test_single_agent_sentence_component_is_in_the_single_agent_manifest_and_not_in_the_full_manifest(cid, get_sentence, marker):
    assert cid in _manifest_ids(SINGLE_AGENT_MANIFEST)
    assert cid not in _manifest_ids(FULL_MANIFEST)


@pytest.mark.parametrize("cid,get_sentence,marker", _SENTENCES)
def test_single_agent_sentence_renders_exactly_once_in_single_agent_and_exactly_once_in_full(cid, get_sentence, marker):
    sentence = get_sentence()
    assert _single_agent().count(sentence) == 1
    assert _full().count(sentence) == 1


# --- heading rule: a single-agent-only component at an absent anchor brings its own heading ----------

_HEADED = [
    pytest.param(GATE_ID, "Model Identity Gate", "External interactions", "trust-tier-pointer", _gate_sentence, id="gate"),
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
def test_heading_appears_once_in_single_agent_with_the_text_directly_under_it(cid, anchor, prev_section, prev_id, get_text):
    single_agent = _single_agent()
    assert single_agent.split("\n").count("## " + anchor) == 1
    assert ("## " + anchor + "\n\n" + get_text() + "\n") in single_agent
    # the full render keeps its own single heading from the full components
    assert _full().split("\n").count("## " + anchor) == 1


@pytest.mark.parametrize("cid,anchor,prev_section,prev_id,get_text", _HEADED)
def test_the_text_no_longer_sits_at_the_end_of_the_previous_section(cid, anchor, prev_section, prev_id, get_text):
    lines = _single_agent().split("\n")
    blanks, before = _blank_lines_before(lines, lines.index("## " + anchor))
    assert blanks == 1
    # the line before the blank is the previous single-agent component's last line, not the moved text
    assert before == _last_line(prev_id)
    assert before != get_text()


def test_notes_scope_line_and_sentence_sit_after_my_goal_and_before_authority_structure():
    single_agent = _single_agent()
    block = _scope_line() + "\n\n" + _notes_sentence() + "\n"
    assert single_agent.count(block) == 1
    at = single_agent.index(block)
    assert single_agent.index("## My Goal") < at < single_agent.index("## Authority Structure")
    # directly before the Authority Structure heading: block, one blank line, heading
    assert single_agent[at + len(block):].startswith("\n## Authority Structure")


# --- (e) consent paragraph and authority table --------------------------------------------


def _component_body(component_id: str) -> str:
    raw = (TEMPLATES / "components" / f"{component_id}.md").read_text(encoding="utf-8")
    return raw.split("\n---\n", 1)[1]


def test_e_single_agent_keeps_the_consent_paragraph_and_the_authority_table():
    close = _component_body("authority-structure-close")
    match = re.search(r"[^.\n]*claims no consent[^.\n]*\.", close)
    assert match, "authority-structure-close no longer holds the 'claims no consent' sentence"
    sentence = match.group(0).strip()
    assert sentence and "claims no consent" in sentence
    single_agent = _single_agent()
    assert sentence in single_agent
    table_header = "| Class | Examples | My authority |"
    assert table_header in _component_body("authority-structure-table-claude-code")
    assert table_header in single_agent
    ids = [c["id"] for c in tcr.render("claude-code", "CLAUDE.md", "single-agent")[1]["components"]]
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
    only_full = set(_manifest_ids(FULL_MANIFEST)) - set(_manifest_ids(SINGLE_AGENT_MANIFEST))
    assert only_full, "the single-agent manifest should drop some components"
    # Rendering single-agent succeeds: nothing in components/ is flagged dead.
    tcr.render("claude-code", "CLAUDE.md", "single-agent")
    assert only_full <= tcr._all_referenced_component_ids()


def test_f_a_component_in_no_manifest_still_fails_a_single_agent_render(tmp_path, monkeypatch):
    scratch = _scratch(tmp_path, monkeypatch)
    (scratch / "components" / "orphan-for-single-agent.md").write_text(
        _component_file("orphan-for-single-agent"), encoding="utf-8"
    )
    with pytest.raises(tcr.TemplateComponentError, match="orphan-for-single-agent"):
        tcr.render("claude-code", "CLAUDE.md", "single-agent")
    with pytest.raises(tcr.TemplateComponentError, match="orphan-for-single-agent"):
        tcr.render("claude-code", "CLAUDE.md")


def test_f_a_component_listed_only_in_the_single_agent_manifest_is_live(tmp_path, monkeypatch):
    scratch = _scratch(tmp_path, monkeypatch)
    (scratch / "components" / "single-agent-only-extra.md").write_text(
        _component_file("single-agent-only-extra"), encoding="utf-8"
    )
    path = scratch / "manifests" / "claude-code.CLAUDE.md.single-agent.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["components"].append({"id": "single-agent-only-extra", "anchor": "Nowhere"})
    path.write_text(json.dumps(manifest), encoding="utf-8")
    text, _ = tcr.render("claude-code", "CLAUDE.md", "single-agent")
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
    single_agent = _single_agent()
    assert "\n\n\n\n" not in single_agent, "a run of 3+ blank lines"
    assert single_agent.endswith("\n") and not single_agent.endswith("\n\n"), "must end with exactly one newline"
    assert not single_agent.startswith("\n")


def test_g_every_heading_is_preceded_by_a_blank_line():
    lines = _single_agent().split("\n")
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and re.match(r"#{1,6} ", line) and i > 0:
            assert lines[i - 1].strip() == "", f"heading glued to the paragraph above: {line!r}"


_PAIRS = list(zip(SINGLE_AGENT_IDS, SINGLE_AGENT_IDS[1:]))


@pytest.mark.parametrize("prev_id,next_id", _PAIRS)
def test_g_boundary_shape_at_each_adjacency(prev_id, next_id):
    """One blank line between components, unless the full document differs at the same boundary."""
    full_ids = _manifest_ids(FULL_MANIFEST)
    # the single-agent-only components are in no full manifest, so no full-render boundary exists to compare with
    adjacent_in_full = (
        not {BAN_ID, GATE_ID, NOTES_ID} & {prev_id, next_id} and full_ids.index(next_id) == full_ids.index(prev_id) + 1
    )
    expected = 1
    if adjacent_in_full:
        flines = _full().split("\n")
        nf = _first_line(next_id)
        assert flines.count(nf) == 1, f"{next_id}: first line not unique in the full render"
        expected, before = _blank_lines_before(flines, flines.index(nf))
        assert before == _last_line(prev_id)
    lines = _single_agent().split("\n")
    nl = _first_line(next_id)
    assert lines.count(nl) == 1, f"{next_id}: first line not unique in the single-agent render"
    blanks, before = _blank_lines_before(lines, lines.index(nl))
    assert before == _last_line(prev_id), (
        f"{prev_id} does not directly precede {next_id} in the single-agent render"
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


def test_i_single_agent_is_strictly_shorter_and_has_none_of_the_dropped_headings():
    single_agent, full = _single_agent(), _full()
    assert len(single_agent.encode()) < len(full.encode())
    assert len(single_agent.splitlines()) < len(full.splitlines())
    single_agent_headings = set(_headings(single_agent))
    dropped = set(_manifest_ids(FULL_MANIFEST)) - set(SINGLE_AGENT_IDS)
    seen = 0
    for cid in sorted(dropped):
        for h in _headings(_component_body(cid)):
            seen += 1
            if h in ("## Model Identity Gate", "## Parallelization and Delegation"):
                continue  # rendered by the single-agent-only components (heading rule), asserted below
            assert h not in single_agent_headings, f"{cid}: dropped heading {h!r} is in the single-agent render"
    assert seen >= 4
    for named in ("Agent coordination", "Loop-mode policy"):
        assert named in full
        assert named not in single_agent, named
    # the two single-agent-only components bring their own heading at their absent anchor (heading rule)
    for named in ("Model Identity Gate", "Parallelization and Delegation"):
        assert named in full
        assert ("## " + named) in single_agent_headings
    assert "## ENGRAM Write Discipline" in single_agent_headings
    assert "## ENGRAM Read Discipline" in single_agent_headings



# --- the Session Start Reading section is restored (ruling: cross-compaction continuity) -------

SSR_ID = "session-start-reading"
SSR_HEADING = "## Session Start Reading"


def _section(text: str, heading: str) -> str:
    """The lines from `heading` to the next `## ` heading (exclusive), outside code fences."""
    lines = text.split("\n")
    start = lines.index(heading)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).rstrip("\n")


def test_session_start_reading_renders_with_its_heading_and_its_three_numbered_items():
    text = _single_agent()
    assert text.split("\n").count(SSR_HEADING) == 1
    section = _section(text, SSR_HEADING)
    numbered = [ln for ln in section.split("\n") if re.match(r"\d+\. ", ln)]
    assert [ln.split(". ", 1)[0] for ln in numbered] == ["1", "2", "3"], numbered


def test_session_start_reading_section_is_the_full_files_section_byte_for_byte():
    assert _section(_single_agent(), SSR_HEADING) == _section(_full(), SSR_HEADING)


def test_session_start_reading_is_the_existing_full_component_not_a_new_one():
    assert SSR_ID in _manifest_ids(FULL_MANIFEST)
    assert _manifest_ids(SINGLE_AGENT_MANIFEST).count(SSR_ID) == 1
    assert not (TEMPLATES / "components" / f"{SSR_ID}-single-agent.md").exists()


def test_session_start_reading_sits_where_the_full_manifest_puts_it():
    full_ids, ids = _manifest_ids(FULL_MANIFEST), _manifest_ids(SINGLE_AGENT_MANIFEST)
    # the full manifest puts it right after the ENGRAM Reference; so does the single-agent one
    assert full_ids.index(SSR_ID) == full_ids.index("engram-reference") + 1
    assert ids.index(SSR_ID) == ids.index("engram-reference") + 1
    # and its place among the components the two manifests share is the full manifest's place
    shared = [i for i in ids if i in set(full_ids)]
    assert shared == [i for i in full_ids if i in set(shared)]
    assert shared.index(SSR_ID) == shared.index("engram-reference") + 1


# --- the profile is a SUBSET of the full template: every component is a full component or a bound variant of one

# the profile-only components and the full component each is bound to (the binding cells above compare bytes)
BOUND_VARIANT_OF = {
    GATE_ID: "model-identity-gate",
    BAN_ID: "fairy-tool-policy-claude-code",
    NOTES_ID: "cross-surface-persistence",
}


def test_every_single_agent_component_is_a_full_component_or_a_bound_variant_of_one():
    full_ids = set(_manifest_ids(FULL_MANIFEST))
    ids = _manifest_ids(SINGLE_AGENT_MANIFEST)
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
def single_agent_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("single-agent-home")
    r = _bootstrap(home, ["--claude-md-profile", "single-agent"])
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


def test_c1_single_agent_option_records_the_profile_and_writes_the_single_agent_render(single_agent_home, full_home):
    assert _config(single_agent_home)["claude_md_profile"] == "single-agent"
    single_agent_md = _claude_md(single_agent_home)
    assert "Loop-mode policy" not in single_agent_md  # a dropped component; the gate's own heading now renders
    assert "## ENGRAM Write Discipline" in single_agent_md
    assert len(single_agent_md) < len(_claude_md(full_home))


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
def test_c1_a_default_or_full_run_overwrites_a_stale_single_agent_record(tmp_path, profile):
    r, home = _call_record_step(tmp_path, profile, json.dumps({"self_lineage": "x:y", "claude_md_profile": "single-agent"}))
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
    r, _ = _call_record_step(tmp_path, "single-agent", "5")
    assert r.returncode != 0 and "Traceback" not in r.stderr
    assert "REFUSED" in r.stderr and "CLAUDE.md was not written" in r.stderr


def test_c1_missing_config_refusal_says_the_graph_was_already_seeded(tmp_path):
    r, _ = _call_record_step(tmp_path, "single-agent", None)
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


def test_h_single_agent_file_leaves_the_same_unresolved_placeholders_as_the_full_file(single_agent_home, full_home):
    single_agent_md, full_md = _claude_md(single_agent_home), _claude_md(full_home)
    # The run really rendered the single-agent file (an option that is ignored would pass the set check).
    assert "Loop-mode policy" not in single_agent_md and "Loop-mode policy" in full_md
    single_agent_left = set(PLACEHOLDER.findall(single_agent_md))
    full_left = set(PLACEHOLDER.findall(full_md))
    assert single_agent_left == full_left, (single_agent_left, full_left)
    # What bootstrap resolves is resolved in the single-agent file too; the names left are the ones the
    # first-session skill fills in, in both files.
    for name in ("{{ENGRAM_HOME}}", "{{TODAY}}", "{{AX_HONESTY}}", "{{DF_EPISTEMIC_IDENTITY}}"):
        assert name not in single_agent_md


# --- (c2)-(c4): the upgrade flow's template-gate ------------------------------------------

from tools.engine import flows  # noqa: E402

FULL_REL = "src/engram/templates/template.CLAUDE.md"
SINGLE_AGENT_REL = "src/engram/templates/template.CLAUDE.single-agent.md"


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _upgrade_fixture(tmp_path: Path, *, profile, change: str):
    """A source repo with both templates at commit A; `change` ('full', 'single-agent', 'both') edits at commit B."""
    src = tmp_path / "source"
    src.mkdir()
    _git(src, "init", "-b", "dev")
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "T")
    _write(src / FULL_REL, "FULL TEMPLATE v1\n")
    _write(src / SINGLE_AGENT_REL, "SINGLE-AGENT TEMPLATE v1\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "a")
    a = _git(src, "rev-parse", "HEAD")
    if change in ("full", "both"):
        _write(src / FULL_REL, "FULL TEMPLATE v2\n")
    if change in ("single-agent", "both"):
        _write(src / SINGLE_AGENT_REL, "SINGLE-AGENT TEMPLATE v2\n")
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


def test_c2_single_agent_home_full_template_changed_only_reports_no_change(tmp_path):
    ctx = _run_gate(_upgrade_fixture(tmp_path, profile="single-agent", change="full"))
    assert ctx["_template_changed"] is False
    assert ctx["_template_diff"] is None
    assert ctx["_template_gate_done"] is True


def test_c2_single_agent_home_single_agent_template_changed_surfaces_the_single_agent_template(tmp_path):
    ctx = _run_gate(_upgrade_fixture(tmp_path, profile="single-agent", change="single-agent"))
    assert ctx["_template_changed"] is True
    assert "SINGLE-AGENT TEMPLATE v2" in ctx["_template_diff"]
    assert "FULL TEMPLATE" not in ctx["_template_diff"]


def test_c2_single_agent_home_both_changed_surfaces_only_the_single_agent_template(tmp_path):
    ctx = _run_gate(_upgrade_fixture(tmp_path, profile="single-agent", change="both"))
    assert "SINGLE-AGENT TEMPLATE v2" in ctx["_template_diff"]
    assert "FULL TEMPLATE" not in ctx["_template_diff"]


def test_c2_full_home_single_agent_template_changed_only_reports_no_change(tmp_path):
    for profile in (None, "full"):
        sub = tmp_path / f"p-{profile}"
        sub.mkdir()
        ctx = _run_gate(_upgrade_fixture(sub, profile=profile, change="single-agent"))
        assert ctx["_template_changed"] is False, profile
        assert ctx["_template_diff"] is None, profile


def test_c3_home_with_no_recorded_profile_behaves_as_today(tmp_path):
    ctx = _run_gate(_upgrade_fixture(tmp_path, profile=None, change="full"))
    assert ctx["_template_changed"] is True
    assert "FULL TEMPLATE v2" in ctx["_template_diff"]
    assert "SINGLE-AGENT TEMPLATE" not in ctx["_template_diff"]


def test_c3_home_with_no_config_json_behaves_as_today(tmp_path):
    ctx = _upgrade_fixture(tmp_path, profile=None, change="full")
    os.remove(Path(ctx["engram_home"]) / "config.json")
    _run_gate(ctx)
    assert "FULL TEMPLATE v2" in ctx["_template_diff"]


@pytest.mark.parametrize("bad", ["weird", "lean", "../single-agent", 7, ["single-agent"], ""])
def test_c3_an_unrecognized_recorded_profile_never_offers_either_template(tmp_path, bad):
    ctx = _run_gate(_upgrade_fixture(tmp_path, profile=bad, change="both"))
    assert ctx["_template_gate_done"] is True
    diff = ctx["_template_diff"]
    assert diff is not None and "claude_md_profile" in diff
    assert "TEMPLATE v" not in diff


def test_c3_an_unreadable_config_never_offers_either_template(tmp_path):
    ctx = _upgrade_fixture(tmp_path, profile="single-agent", change="both")
    (Path(ctx["engram_home"]) / "config.json").write_text("{not json", encoding="utf-8")
    _run_gate(ctx)
    assert "TEMPLATE v" not in (ctx["_template_diff"] or "")


@pytest.mark.parametrize("profile", [None, "single-agent"])
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
    single_agent = entries["templates/template.CLAUDE.single-agent.md"]
    assert single_agent["tier"] == full["tier"], "bootstrap reads the single-agent file, so it ships where the full one does"
    assert "templates/manifests" in entries


# --- round 3: Codex ignores the option; the upgrade skill's Step 2 snippet ----------------------


def test_c1_codex_target_with_single_agent_option_records_no_profile(tmp_path):
    r = _bootstrap(tmp_path, ["--claude-md-profile", "single-agent"], extra_env={"TARGET": "codex"})
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
        ('{"claude_md_profile": "single-agent"}', "template.CLAUDE.single-agent.md"),
        ('{"install_tier": "convenience"}', "template.CLAUDE.md"),
        (None, "template.CLAUDE.md"),
        ('{"claude_md_profile": "tiny"}', "template.CLAUDE.md"),
        ('{"claude_md_profile": "lean"}', "template.CLAUDE.md"),
    ],
    ids=["single-agent", "unrecorded", "no-config", "unrecognised", "retired-working-name"],
)
def test_upgrade_skill_step2_snippet_picks_the_template(tmp_path, shell, config_text, expected):
    assert _pick_template(shell, tmp_path / "home", config_text) == expected


def test_render_reads_the_named_template_even_if_the_sources_list_widens(tmp_path, monkeypatch):
    import template_render as tr

    (tmp_path / "template.CLAUDE.single-agent.md").write_text("SINGLE-AGENT", encoding="utf-8")
    (tmp_path / "template.CLAUDE.md").write_text("FULL", encoding="utf-8")
    (tmp_path / "other.md").write_text("OTHER", encoding="utf-8")
    monkeypatch.setitem(tr.CLAUDE_MD_PROFILE_SOURCES, "single-agent", ["other.md", "template.CLAUDE.single-agent.md"])
    assert tr.render_identity_surface(tmp_path, "single-agent") == "SINGLE-AGENT"
    for profile, name in tr.CLAUDE_MD_PROFILE_TEMPLATE.items():
        assert name in tr.CLAUDE_MD_PROFILE_SOURCES[profile]


# --- docs pins (round 5: #4802 design-seat findings) ----------------------------------------

README_AGENT = REPO_ROOT / "README-AGENT.md"
README = REPO_ROOT / "README.md"
FIRST_SESSION_SKILL = SRC / "skills" / "claude" / "engram-first-session" / "SKILL.md"


def test_first_session_skill_does_not_say_the_single_agent_file_has_no_model_identity_gate_section():
    """The single-agent render HAS a `## Model Identity Gate` section, so the skill must not deny it."""
    assert "## Model Identity Gate" in _single_agent().splitlines()
    skill = " ".join(FIRST_SESSION_SKILL.read_text(encoding="utf-8").split())
    assert "has no such section" not in skill


def test_first_session_skill_pins_the_true_one_sentence_claim_against_the_single_agent_render():
    """The skill says the single-agent gate section holds one sentence; the single-agent render must keep that true."""
    skill = " ".join(FIRST_SESSION_SKILL.read_text(encoding="utf-8").split())
    assert "in a single-agent-profile file that section holds one sentence" in skill
    lines = _single_agent().splitlines()
    start = lines.index("## Model Identity Gate") + 1
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("## "))
    body = " ".join(" ".join(lines[start:end]).split())
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", body) if s]
    assert len(sentences) == 1, sentences


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


# --- the working name `lean` is retired, not aliased (#4802 rework) -------------------------------


def test_bootstrap_refuses_the_retired_working_name_and_lists_the_real_profiles(tmp_path):
    r = _bootstrap(tmp_path, ["--claude-md-profile", "lean"])
    assert r.returncode != 0
    msg = r.stderr + r.stdout
    assert "--claude-md-profile needs one of full, single-agent" in msg
    assert "'lean'" in msg  # the refused value is echoed back
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert not (tmp_path / ".engram" / "knowledge.db").exists()


def test_bootstrap_profile_list_is_full_and_single_agent_and_nothing_else():
    assert _bootstrap_profile_facts()["profiles"] == ["full", "single-agent"]


def test_a_recorded_retired_working_name_reads_as_undetermined_in_the_renderer_module(tmp_path):
    import template_render as tr

    (tmp_path / "config.json").write_text(json.dumps({"claude_md_profile": "lean"}), encoding="utf-8")
    assert tr.recorded_claude_md_profile(str(tmp_path)) == tr.PROFILE_UNDETERMINED
    (tmp_path / "config.json").write_text(json.dumps({"claude_md_profile": "single-agent"}), encoding="utf-8")
    assert tr.recorded_claude_md_profile(str(tmp_path)) == "single-agent"


def test_the_profile_map_has_no_key_for_the_retired_working_name():
    import template_render as tr

    assert sorted(tr.CLAUDE_MD_PROFILE_SOURCES) == ["full", "single-agent"]
    assert sorted(tr.CLAUDE_MD_PROFILE_TEMPLATE) == ["full", "single-agent"]
    with pytest.raises(tcr.TemplateComponentError):
        tcr.render("claude-code", "CLAUDE.md", "lean")
