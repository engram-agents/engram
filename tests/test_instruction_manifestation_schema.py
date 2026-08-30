"""Schema-validator tests for the instruction-manifestation engine (PR2,
#2140).

One passing + one failing case per validation rule in the PR2 spec sec. 2
(``schema.validate_components``) and sec. 3 (``schema.validate_harness_
contract``). Each fixture is written directly to a ``tmp_path`` components
directory so these tests never touch the real, checked-in pilot components
under ``src/build/instruction_manifestation/components/`` (those get their
own end-to-end coverage in ``test_instruction_manifestation_pilots.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_BUILD = REPO_ROOT / "src" / "build"
if str(SRC_BUILD) not in sys.path:
    sys.path.insert(0, str(SRC_BUILD))

from instruction_manifestation import schema  # noqa: E402


def _write(components_dir: Path, filename: str, text: str) -> Path:
    path = components_dir / filename
    path.write_text(text, encoding="utf-8")
    return path


def _minimal_base(cid: str, **overrides: str) -> str:
    fm = {
        "id": cid,
        "kind": "base",
        "composition_mode": "flat",
        "extension_required": False,
    }
    fm.update(overrides)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append(f"Body for {cid}.")
    return "\n".join(lines) + "\n"


def _minimal_extension(cid: str, extends: str, harness: str = "claude", **overrides: str) -> str:
    fm = {
        "id": cid,
        "kind": "extension",
        "extends": extends,
        "harness": harness,
        "composition_mode": "flat",
    }
    fm.update(overrides)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append(f"Extension body for {cid}.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Rule 1 -- id matches filename stem
# ---------------------------------------------------------------------------


def test_id_matches_stem_passes(tmp_path):
    _write(tmp_path, "foo.md", _minimal_base("foo"))
    violations = schema.validate_components(tmp_path)
    assert violations == []


def test_id_mismatched_stem_fails(tmp_path):
    _write(tmp_path, "foo.md", _minimal_base("bar"))
    violations = schema.validate_components(tmp_path)
    assert any("does not match filename stem" in v for v in violations)


# ---------------------------------------------------------------------------
# Rule 2 -- id globally unique
# ---------------------------------------------------------------------------


def test_id_unique_passes(tmp_path):
    _write(tmp_path, "foo.md", _minimal_base("foo"))
    _write(tmp_path, "bar.md", _minimal_base("bar"))
    violations = schema.validate_components(tmp_path)
    assert violations == []


def test_duplicate_id_fails(tmp_path):
    _write(tmp_path, "dup-a.md", _minimal_base("dup-a"))
    # dup-b.md declares the SAME id as dup-a -- this file also trips rule 1
    # (id != its own stem "dup-b"), which is expected and fine; the
    # assertion below targets specifically the uniqueness violation.
    _write(tmp_path, "dup-b.md", _minimal_base("dup-a"))
    violations = schema.validate_components(tmp_path)
    assert any("is not unique" in v and "dup-a" in v for v in violations)


# ---------------------------------------------------------------------------
# Rule 3 -- kind is exactly base or extension
# ---------------------------------------------------------------------------


def test_valid_kind_passes(tmp_path):
    _write(tmp_path, "foo.md", _minimal_base("foo"))
    violations = schema.validate_components(tmp_path)
    assert violations == []


def test_invalid_kind_fails(tmp_path):
    _write(tmp_path, "foo.md", _minimal_base("foo", kind="rule"))
    violations = schema.validate_components(tmp_path)
    assert any("kind must be one of" in v for v in violations)


# ---------------------------------------------------------------------------
# Rule 4 -- kind=base forbids extends/harness; kind=extension requires both
# ---------------------------------------------------------------------------


def test_base_without_extends_or_harness_passes(tmp_path):
    _write(tmp_path, "foo.md", _minimal_base("foo"))
    violations = schema.validate_components(tmp_path)
    assert violations == []


def test_base_with_extends_present_fails(tmp_path):
    _write(tmp_path, "foo.md", _minimal_base("foo", extends="bar"))
    violations = schema.validate_components(tmp_path)
    assert any("'extends' is present" in v for v in violations)


def test_base_with_harness_present_fails(tmp_path):
    _write(tmp_path, "foo.md", _minimal_base("foo", harness="claude"))
    violations = schema.validate_components(tmp_path)
    assert any("'harness' is present" in v for v in violations)


def test_extension_with_extends_and_harness_passes(tmp_path):
    _write(tmp_path, "base-a.md", _minimal_base("base-a"))
    _write(tmp_path, "ext-a.md", _minimal_extension("ext-a", extends="base-a"))
    violations = schema.validate_components(tmp_path)
    assert violations == []


def test_extension_missing_extends_fails(tmp_path):
    text = (
        "---\n"
        "id: ext-a\n"
        "kind: extension\n"
        "harness: claude\n"
        "composition_mode: flat\n"
        "---\n"
        "Body.\n"
    )
    _write(tmp_path, "ext-a.md", text)
    violations = schema.validate_components(tmp_path)
    assert any("requires a non-empty 'extends'" in v for v in violations)


def test_extension_missing_harness_fails(tmp_path):
    text = (
        "---\n"
        "id: ext-a\n"
        "kind: extension\n"
        "extends: base-a\n"
        "composition_mode: flat\n"
        "---\n"
        "Body.\n"
    )
    _write(tmp_path, "ext-a.md", text)
    violations = schema.validate_components(tmp_path)
    assert any("requires a non-empty 'harness'" in v for v in violations)


# ---------------------------------------------------------------------------
# Rule 5 -- harness value must be claude|codex|dsh
# ---------------------------------------------------------------------------


def test_valid_harness_value_passes(tmp_path):
    _write(tmp_path, "base-a.md", _minimal_base("base-a"))
    _write(tmp_path, "ext-a.md", _minimal_extension("ext-a", extends="base-a", harness="codex"))
    violations = schema.validate_components(tmp_path)
    assert violations == []


def test_invalid_harness_value_fails(tmp_path):
    _write(tmp_path, "base-a.md", _minimal_base("base-a"))
    _write(
        tmp_path,
        "ext-a.md",
        _minimal_extension("ext-a", extends="base-a", harness="gemini"),
    )
    violations = schema.validate_components(tmp_path)
    assert any("harness 'gemini' is not one of" in v for v in violations)


# ---------------------------------------------------------------------------
# Rule 6 -- extends must reference an existing kind=base component
# ---------------------------------------------------------------------------


def test_extends_existing_base_passes(tmp_path):
    _write(tmp_path, "base-a.md", _minimal_base("base-a"))
    _write(tmp_path, "ext-a.md", _minimal_extension("ext-a", extends="base-a"))
    violations = schema.validate_components(tmp_path)
    assert violations == []


def test_extends_nonexistent_id_fails(tmp_path):
    _write(tmp_path, "ext-a.md", _minimal_extension("ext-a", extends="does-not-exist"))
    violations = schema.validate_components(tmp_path)
    assert any(
        "extends 'does-not-exist' but no component with that id exists" in v
        for v in violations
    )


def test_extends_another_extension_fails(tmp_path):
    _write(tmp_path, "base-a.md", _minimal_base("base-a"))
    _write(tmp_path, "ext-a.md", _minimal_extension("ext-a", extends="base-a"))
    # ext-b extends ext-a, which is kind=extension, not kind=base.
    _write(tmp_path, "ext-b.md", _minimal_extension("ext-b", extends="ext-a"))
    violations = schema.validate_components(tmp_path)
    assert any(
        "extends 'ext-a' but that id is not kind=base" in v for v in violations
    )


# ---------------------------------------------------------------------------
# Rule 7 -- composition_mode must be exactly "flat"
# ---------------------------------------------------------------------------


def test_composition_mode_flat_passes(tmp_path):
    _write(tmp_path, "foo.md", _minimal_base("foo"))
    violations = schema.validate_components(tmp_path)
    assert violations == []


def test_composition_mode_illegal_value_fails(tmp_path):
    _write(tmp_path, "foo.md", _minimal_base("foo", composition_mode="nested"))
    violations = schema.validate_components(tmp_path)
    matches = [v for v in violations if "composition_mode must be 'flat'" in v]
    assert matches
    # The clear-error requirement: the illegal value itself must be named.
    assert any("'nested'" in v for v in matches)


# ---------------------------------------------------------------------------
# Rule 8 -- extension_required must be a real boolean
# ---------------------------------------------------------------------------


def test_extension_required_real_boolean_passes(tmp_path):
    _write(tmp_path, "foo.md", _minimal_base("foo", extension_required="true"))
    # NOTE: YAML's bare `true` (no quotes) parses to a real Python bool via
    # yaml.safe_load -- this fixture intentionally emits the unquoted token.
    violations = schema.validate_components(tmp_path)
    assert violations == []


def test_extension_required_string_value_fails(tmp_path):
    text = (
        "---\n"
        "id: foo\n"
        "kind: base\n"
        "composition_mode: flat\n"
        'extension_required: "true"\n'
        "---\n"
        "Body.\n"
    )
    _write(tmp_path, "foo.md", text)
    violations = schema.validate_components(tmp_path)
    assert any("extension_required must be a real boolean" in v for v in violations)


# ---------------------------------------------------------------------------
# Rule 9 -- variables must be a list of strings
# ---------------------------------------------------------------------------


def test_variables_list_of_strings_passes(tmp_path):
    text = (
        "---\n"
        "id: foo\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "variables: [FOO, BAR]\n"
        "---\n"
        "{{FOO}} {{BAR}}\n"
    )
    _write(tmp_path, "foo.md", text)
    violations = schema.validate_components(tmp_path)
    assert violations == []


def test_variables_not_a_list_fails(tmp_path):
    text = (
        "---\n"
        "id: foo\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "variables: FOO\n"
        "---\n"
        "Body.\n"
    )
    _write(tmp_path, "foo.md", text)
    violations = schema.validate_components(tmp_path)
    assert any("variables must be a list of strings" in v for v in violations)


def test_variables_list_with_non_string_element_fails(tmp_path):
    text = (
        "---\n"
        "id: foo\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "variables: [FOO, 42]\n"
        "---\n"
        "Body.\n"
    )
    _write(tmp_path, "foo.md", text)
    violations = schema.validate_components(tmp_path)
    assert any("variables must be a list of strings" in v for v in violations)


# ---------------------------------------------------------------------------
# Harness contract validation (PR2 spec sec. 3)
# ---------------------------------------------------------------------------


def _write_harness(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_harness_contract_valid_passes(tmp_path):
    components_dir = tmp_path / "components"
    components_dir.mkdir()
    _write(components_dir, "foo.md", _minimal_base("foo"))

    harness_path = tmp_path / "claude.yaml"
    _write_harness(
        harness_path,
        "name: claude\n"
        "tool_names: [Agent]\n"
        "variables:\n"
        "  FOO: bar\n"
        "components:\n"
        "  - id: foo\n"
        "    extension: auto\n",
    )
    contract = schema.parse_harness_contract(harness_path)
    violations = schema.validate_harness_contract(contract, components_dir)
    assert violations == []


def test_harness_contract_missing_component_file_fails(tmp_path):
    components_dir = tmp_path / "components"
    components_dir.mkdir()

    harness_path = tmp_path / "claude.yaml"
    _write_harness(
        harness_path,
        "name: claude\n"
        "tool_names: [Agent]\n"
        "variables:\n"
        "  FOO: bar\n"
        "components:\n"
        "  - id: does-not-exist\n"
        "    extension: auto\n",
    )
    contract = schema.parse_harness_contract(harness_path)
    violations = schema.validate_harness_contract(contract, components_dir)
    assert any(
        "components[].id 'does-not-exist' has no matching component file" in v
        for v in violations
    )


def test_harness_contract_tool_names_malformed_fails(tmp_path):
    components_dir = tmp_path / "components"
    components_dir.mkdir()

    harness_path = tmp_path / "claude.yaml"
    _write_harness(
        harness_path,
        "name: claude\n"
        "tool_names: not-a-list\n"
        "variables:\n"
        "  FOO: bar\n"
        "components: []\n",
    )
    contract = schema.parse_harness_contract(harness_path)
    violations = schema.validate_harness_contract(contract, components_dir)
    assert any("tool_names must be a list of strings" in v for v in violations)


def test_harness_contract_variables_malformed_fails(tmp_path):
    components_dir = tmp_path / "components"
    components_dir.mkdir()

    harness_path = tmp_path / "claude.yaml"
    _write_harness(
        harness_path,
        "name: claude\n"
        "tool_names: [Agent]\n"
        "variables: [not, a, dict]\n"
        "components: []\n",
    )
    contract = schema.parse_harness_contract(harness_path)
    violations = schema.validate_harness_contract(contract, components_dir)
    assert any("variables must be a dict of string -> string" in v for v in violations)


# ---------------------------------------------------------------------------
# Sanity: the real, checked-in claude.yaml harness contract validates clean
# against the real, checked-in components directory.
# ---------------------------------------------------------------------------


def test_real_claude_harness_contract_validates_clean():
    real_components_dir = REPO_ROOT / "src" / "build" / "instruction_manifestation" / "components"
    real_harness_path = (
        REPO_ROOT / "src" / "build" / "instruction_manifestation" / "harnesses" / "claude.yaml"
    )
    contract = schema.parse_harness_contract(real_harness_path)
    violations = schema.validate_harness_contract(contract, real_components_dir)
    assert violations == []


def test_real_components_directory_validates_clean():
    real_components_dir = REPO_ROOT / "src" / "build" / "instruction_manifestation" / "components"
    violations = schema.validate_components(real_components_dir)
    assert violations == []


# ---------------------------------------------------------------------------
# Parse errors -- reviewer-fairy finding on this PR's round-1 review: neither
# ComponentParseError nor HarnessContractParseError, nor validate_components'
# per-file resilience (one malformed file doesn't stop validation of the
# others), had a direct test anywhere. Closing that gap here.
# ---------------------------------------------------------------------------


def test_parse_component_file_missing_opening_delimiter_raises(tmp_path):
    path = tmp_path / "broken.md"
    path.write_text("id: broken\nkind: base\n", encoding="utf-8")
    with pytest.raises(schema.ComponentParseError, match="missing opening"):
        schema.parse_component_file(path)


def test_parse_component_file_missing_closing_delimiter_raises(tmp_path):
    path = tmp_path / "broken.md"
    path.write_text("---\nid: broken\nkind: base\n", encoding="utf-8")
    with pytest.raises(schema.ComponentParseError, match="missing closing"):
        schema.parse_component_file(path)


def test_parse_component_file_non_dict_frontmatter_raises(tmp_path):
    path = tmp_path / "broken.md"
    # A YAML list, not a mapping, between the delimiters.
    path.write_text("---\n- one\n- two\n---\nBody.\n", encoding="utf-8")
    with pytest.raises(schema.ComponentParseError, match="did not parse to a YAML mapping"):
        schema.parse_component_file(path)


def test_validate_components_one_malformed_file_does_not_stop_the_others(tmp_path):
    """The resilience behavior validate_components' own docstring claims
    (a ComponentParseError on one file is collected as a violation and
    validation continues for the rest) -- asserted, not just documented."""
    components_dir = tmp_path / "components"
    components_dir.mkdir()
    _write(components_dir, "good.md", _minimal_base("good"))
    _write(components_dir, "broken.md", "id: broken\nkind: base\n")  # no delimiters at all

    violations = schema.validate_components(components_dir)

    assert any("missing opening" in v for v in violations)
    # The well-formed sibling file must still have been fully validated
    # (zero violations attributable to it) -- the malformed file's parse
    # failure didn't abort the pass.
    assert not any("good.md" in v for v in violations)


def test_parse_harness_contract_invalid_yaml_raises(tmp_path):
    path = tmp_path / "claude.yaml"
    path.write_text("name: claude\n  tool_names: [unterminated\n", encoding="utf-8")
    with pytest.raises(schema.HarnessContractParseError, match="invalid YAML"):
        schema.parse_harness_contract(path)


def test_parse_harness_contract_non_dict_raises(tmp_path):
    path = tmp_path / "claude.yaml"
    path.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(schema.HarnessContractParseError, match="did not parse to a mapping"):
        schema.parse_harness_contract(path)
