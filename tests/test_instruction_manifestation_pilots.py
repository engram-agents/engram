"""End-to-end coverage for the two PR2 pilot components (#2140):
``identity-self-reference-example`` (hand-authored) and ``tool-policy``
(generated).

Renders the real, checked-in ``claude`` harness contract against the real,
checked-in components directory and asserts the rendered output contains
the expected substituted variable values and the expected tool-policy
facts -- pulled dynamically from ``deploy/tool_policy_facts.py`` (not
hardcoded in this test), so a change to that fact table is what this test
notices, not something it has to be told about by hand.

Also covers the generator's own drift-guard + idempotency (same discipline
as ``tests/test_tool_capability_index.py``'s pattern for the sibling
generated-artifact class), and the two pilots' clean pass through the
schema validator and all three PR2 lints.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_BUILD = REPO_ROOT / "src" / "build"
if str(SRC_BUILD) not in sys.path:
    sys.path.insert(0, str(SRC_BUILD))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from instruction_manifestation import builder, lints, schema  # noqa: E402
from deploy.tool_policy_facts import (  # noqa: E402
    ALLOWED_FAIRIES,
    BANNED_MARKER,
    BANNED_TOOLS,
)

IM_ROOT = REPO_ROOT / "src" / "build" / "instruction_manifestation"
COMPONENTS_DIR = IM_ROOT / "components"
HARNESSES_DIR = IM_ROOT / "harnesses"
GENERATOR_SCRIPT = IM_ROOT / "generate_tool_policy_component.py"
GENERATED_COMPONENT_PATH = COMPONENTS_DIR / "tool-policy.md"


def _load_generator_module():
    spec = importlib.util.spec_from_file_location(
        "instruction_manifestation_generate_tool_policy_component", str(GENERATOR_SCRIPT)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Real end-to-end render
# ---------------------------------------------------------------------------


def test_render_claude_harness_end_to_end():
    rendered, receipt = builder.render_harness("claude", COMPONENTS_DIR, HARNESSES_DIR)

    assert receipt == {
        "harness": "claude",
        "components": ["identity-self-reference-example", "tool-policy"],
    }

    # identity-self-reference-example: both variables substituted.
    assert "~/.claude/CLAUDE.md" in rendered
    assert "the SessionStart hook injects" in rendered
    # No leftover {{...}} tokens anywhere in the render (render_harness
    # would have raised BuildError otherwise, but assert directly too).
    assert "{{" not in rendered and "}}" not in rendered

    # tool-policy: every fact pulled dynamically from tool_policy_facts.py.
    assert BANNED_MARKER in rendered
    for tool in BANNED_TOOLS:
        assert tool in rendered, (
            f"rendered claude harness output is missing banned-tool token "
            f"{tool!r} -- the tool-policy pilot component (generated base + "
            f"hand-authored extension) has drifted out of sync with "
            f"deploy/tool_policy_facts.py"
        )
    for fairy_id, aliases in ALLOWED_FAIRIES.items():
        assert any(alias in rendered for alias in aliases), (
            f"rendered claude harness output is missing fairy {fairy_id!r} "
            f"-- none of its known aliases {aliases!r} were found"
        )


def test_real_components_and_harness_pass_schema_validation():
    assert schema.validate_components(COMPONENTS_DIR) == []
    contract = schema.parse_harness_contract(HARNESSES_DIR / "claude.yaml")
    assert schema.validate_harness_contract(contract, COMPONENTS_DIR) == []


def test_real_components_and_harness_pass_all_three_lints():
    assert lints.lint_base_never_names_tool(COMPONENTS_DIR, HARNESSES_DIR) == []
    assert lints.lint_unresolved_variables(COMPONENTS_DIR, HARNESSES_DIR) == []
    assert lints.lint_extension_required_coverage(COMPONENTS_DIR, HARNESSES_DIR) == []


# ---------------------------------------------------------------------------
# Generator drift-guard + idempotency (mirrors
# tests/test_tool_capability_index.py's pattern for the sibling generated-
# artifact class).
# ---------------------------------------------------------------------------


def test_checked_in_tool_policy_component_is_byte_identical_to_fresh_generation():
    generator_mod = _load_generator_module()
    facts = generator_mod._import_facts()
    fresh_text = generator_mod.generate_component_text(facts)

    assert GENERATED_COMPONENT_PATH.exists(), (
        f"{GENERATED_COMPONENT_PATH} is missing -- run "
        f"generate_tool_policy_component.py and commit the result."
    )
    checked_in_text = GENERATED_COMPONENT_PATH.read_text(encoding="utf-8")
    assert checked_in_text == fresh_text, (
        "Checked-in components/tool-policy.md is stale relative to "
        "deploy/tool_policy_facts.py. Re-run "
        "generate_tool_policy_component.py and commit the regenerated "
        "component."
    )


def test_generator_is_idempotent():
    generator_mod = _load_generator_module()
    facts = generator_mod._import_facts()
    first = generator_mod.generate_component_text(facts)
    second = generator_mod.generate_component_text(facts)
    assert first == second


def test_generated_component_is_a_valid_base_component():
    frontmatter, body = schema.parse_component_file(GENERATED_COMPONENT_PATH)
    assert frontmatter["id"] == "tool-policy"
    assert frontmatter["kind"] == "base"
    assert frontmatter["extension_required"] is True
    assert BANNED_MARKER in body
