"""Builder (render/composition logic) tests for the instruction-
manifestation engine (PR2, #2140).

Covers the flat-composition render (base + extension concatenation,
{{VARIABLE}} substitution), the extension_required build-failure case
(asserting the error names both harness and component), and the
unresolved-variable build-failure case. Fixtures are synthetic (tmp_path),
isolated from the real, checked-in pilot components.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_BUILD = REPO_ROOT / "src" / "build"
if str(SRC_BUILD) not in sys.path:
    sys.path.insert(0, str(SRC_BUILD))

from instruction_manifestation import builder, schema  # noqa: E402


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def dirs(tmp_path):
    components_dir = tmp_path / "components"
    harnesses_dir = tmp_path / "harnesses"
    components_dir.mkdir()
    harnesses_dir.mkdir()
    return components_dir, harnesses_dir


# ---------------------------------------------------------------------------
# Flat-composition render: base + extension concatenation, variable subst.
# ---------------------------------------------------------------------------


def test_flat_composition_concatenates_base_then_extension_and_substitutes(dirs):
    components_dir, harnesses_dir = dirs

    _write(
        components_dir / "greeting.md",
        "---\n"
        "id: greeting\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: true\n"
        "variables: [NAME]\n"
        "---\n"
        "Hello, {{NAME}}.\n",
    )
    _write(
        components_dir / "greeting-claude.md",
        "---\n"
        "id: greeting-claude\n"
        "kind: extension\n"
        "extends: greeting\n"
        "harness: claude\n"
        "composition_mode: flat\n"
        "---\n"
        "Claude-specific addendum.\n",
    )
    _write(
        harnesses_dir / "claude.yaml",
        "name: claude\n"
        "tool_names: [Agent]\n"
        "variables:\n"
        "  NAME: World\n"
        "components:\n"
        "  - id: greeting\n"
        "    extension: auto\n",
    )

    rendered, receipt = builder.render_harness("claude", components_dir, harnesses_dir)

    assert "Hello, World." in rendered
    assert "Claude-specific addendum." in rendered
    # Base body must precede the extension body (flat composition order).
    assert rendered.index("Hello, World.") < rendered.index("Claude-specific addendum.")
    assert receipt == {"harness": "claude", "components": ["greeting"]}


def test_flat_composition_with_explicit_extension_override(dirs):
    components_dir, harnesses_dir = dirs

    _write(
        components_dir / "greeting.md",
        "---\n"
        "id: greeting\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "---\n"
        "Hello.\n",
    )
    _write(
        components_dir / "greeting-alt.md",
        "---\n"
        "id: greeting-alt\n"
        "kind: extension\n"
        "extends: greeting\n"
        "harness: claude\n"
        "composition_mode: flat\n"
        "---\n"
        "Alt addendum.\n",
    )
    _write(
        harnesses_dir / "claude.yaml",
        "name: claude\n"
        "tool_names: []\n"
        "variables: {}\n"
        "components:\n"
        "  - id: greeting\n"
        "    extension: greeting-alt\n",
    )

    rendered, _receipt = builder.render_harness("claude", components_dir, harnesses_dir)
    assert "Hello." in rendered
    assert "Alt addendum." in rendered


def test_no_extension_when_not_required_renders_base_only(dirs):
    components_dir, harnesses_dir = dirs
    _write(
        components_dir / "solo.md",
        "---\n"
        "id: solo\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "---\n"
        "Solo content.\n",
    )
    _write(
        harnesses_dir / "claude.yaml",
        "name: claude\n"
        "tool_names: []\n"
        "variables: {}\n"
        "components:\n"
        "  - id: solo\n"
        "    extension: auto\n",
    )
    rendered, receipt = builder.render_harness("claude", components_dir, harnesses_dir)
    assert rendered.strip() == "Solo content."
    assert receipt["components"] == ["solo"]


# ---------------------------------------------------------------------------
# extension_required build failure -- must name BOTH harness and component.
# ---------------------------------------------------------------------------


def test_extension_required_with_no_match_raises_naming_harness_and_component(dirs):
    components_dir, harnesses_dir = dirs
    _write(
        components_dir / "needs-ext.md",
        "---\n"
        "id: needs-ext\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: true\n"
        "---\n"
        "Body needing an extension.\n",
    )
    _write(
        harnesses_dir / "claude.yaml",
        "name: claude\n"
        "tool_names: []\n"
        "variables: {}\n"
        "components:\n"
        "  - id: needs-ext\n"
        "    extension: auto\n",
    )

    with pytest.raises(builder.BuildError) as excinfo:
        builder.render_harness("claude", components_dir, harnesses_dir)

    message = str(excinfo.value)
    assert "claude" in message
    assert "needs-ext" in message


# ---------------------------------------------------------------------------
# Unresolved-variable build failure -- must name component and token.
# ---------------------------------------------------------------------------


def test_unresolved_variable_raises_naming_component_and_token(dirs):
    components_dir, harnesses_dir = dirs
    _write(
        components_dir / "broken.md",
        "---\n"
        "id: broken\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "variables: [UNDEFINED_TOKEN]\n"
        "---\n"
        "This references {{UNDEFINED_TOKEN}} which no harness defines.\n",
    )
    _write(
        harnesses_dir / "claude.yaml",
        "name: claude\n"
        "tool_names: []\n"
        "variables: {}\n"
        "components:\n"
        "  - id: broken\n"
        "    extension: auto\n",
    )

    with pytest.raises(builder.BuildError) as excinfo:
        builder.render_harness("claude", components_dir, harnesses_dir)

    message = str(excinfo.value)
    assert "broken" in message
    assert "UNDEFINED_TOKEN" in message


# ---------------------------------------------------------------------------
# find_extension: ambiguous auto-resolution is a build failure, not a
# silent first-match pick.
# ---------------------------------------------------------------------------


def test_ambiguous_auto_extension_raises(dirs):
    components_dir, harnesses_dir = dirs
    _write(
        components_dir / "base-a.md",
        "---\n"
        "id: base-a\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "---\n"
        "Base.\n",
    )
    for suffix in ("1", "2"):
        _write(
            components_dir / f"ext-{suffix}.md",
            "---\n"
            f"id: ext-{suffix}\n"
            "kind: extension\n"
            "extends: base-a\n"
            "harness: claude\n"
            "composition_mode: flat\n"
            "---\n"
            f"Ext {suffix}.\n",
        )

    components = schema.load_all_components(components_dir)

    with pytest.raises(builder.BuildError, match="ambiguous"):
        builder.find_extension(components, "base-a", "claude", "auto")
