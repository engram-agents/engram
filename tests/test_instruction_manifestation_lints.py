"""Lint tests for the instruction-manifestation engine (PR2, #2140).

One clean-fixture case AND one deliberately-planted-violation case per lint,
proving each lint actually FIRES rather than merely returning clean on an
already-clean fixture. Lint (a) additionally gets a substring-non-false-
positive case (a tool name appearing as a substring of a longer identifier
must NOT be flagged -- exactly what \\b word-boundary matching exists to
get right).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_BUILD = REPO_ROOT / "src" / "build"
if str(SRC_BUILD) not in sys.path:
    sys.path.insert(0, str(SRC_BUILD))

from instruction_manifestation import lints  # noqa: E402


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _dirs(tmp_path):
    components_dir = tmp_path / "components"
    harnesses_dir = tmp_path / "harnesses"
    components_dir.mkdir()
    harnesses_dir.mkdir()
    return components_dir, harnesses_dir


def _write_claude_harness(harnesses_dir: Path, tool_names="[Agent]", variables=None, components=None):
    variables = variables or {}
    components = components or []
    var_lines = "\n".join(f"  {k}: {v}" for k, v in variables.items()) or "  {}"
    comp_lines = "\n".join(
        f"  - id: {c['id']}\n    extension: {c.get('extension', 'auto')}" for c in components
    ) or "  []"
    text = (
        "name: claude\n"
        f"tool_names: {tool_names}\n"
        "variables:\n"
        f"{var_lines if variables else '  {}'}\n"
        "components:\n"
        f"{comp_lines if components else '  []'}\n"
    )
    _write(harnesses_dir / "claude.yaml", text)


# ---------------------------------------------------------------------------
# Lint (a) -- base-never-names-a-tool
# ---------------------------------------------------------------------------


def test_lint_a_clean_fixture_returns_no_violations(tmp_path):
    components_dir, harnesses_dir = _dirs(tmp_path)
    _write(
        components_dir / "safe.md",
        "---\n"
        "id: safe\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "---\n"
        "This body never names any harness tool.\n",
    )
    _write_claude_harness(harnesses_dir, tool_names="[Agent, Workflow]")
    assert lints.lint_base_never_names_tool(components_dir, harnesses_dir) == []


def test_lint_a_planted_violation_fires(tmp_path):
    components_dir, harnesses_dir = _dirs(tmp_path)
    _write(
        components_dir / "leaky.md",
        "---\n"
        "id: leaky\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "---\n"
        "Use the Agent tool to dispatch a fairy.\n",
    )
    _write_claude_harness(harnesses_dir, tool_names="[Agent, Workflow]")
    violations = lints.lint_base_never_names_tool(components_dir, harnesses_dir)
    assert violations
    assert any("leaky.md" in v and "'Agent'" in v for v in violations)


def test_lint_a_does_not_check_extension_bodies():
    """Extension bodies are explicitly allowed to name harness tools --
    lint (a) must never fire on kind=extension components."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        components_dir, harnesses_dir = _dirs(tmp_path)
        _write(
            components_dir / "base-a.md",
            "---\n"
            "id: base-a\n"
            "kind: base\n"
            "composition_mode: flat\n"
            "extension_required: false\n"
            "---\n"
            "Neutral base body.\n",
        )
        _write(
            components_dir / "ext-a.md",
            "---\n"
            "id: ext-a\n"
            "kind: extension\n"
            "extends: base-a\n"
            "harness: claude\n"
            "composition_mode: flat\n"
            "---\n"
            "Use the Agent tool directly -- this is fine in an extension.\n",
        )
        _write_claude_harness(harnesses_dir, tool_names="[Agent]")
        assert lints.lint_base_never_names_tool(components_dir, harnesses_dir) == []


def test_lint_a_pure_substring_occurrence_is_clean(tmp_path):
    components_dir, harnesses_dir = _dirs(tmp_path)
    _write(
        components_dir / "substring-only.md",
        "---\n"
        "id: substring-only\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "---\n"
        "AgentSomethingElse is a made-up identifier, nothing more.\n",
    )
    _write_claude_harness(harnesses_dir, tool_names="[Agent]")
    violations = lints.lint_base_never_names_tool(components_dir, harnesses_dir)
    assert violations == [], (
        "a tool name occurring only as a SUBSTRING of a longer identifier "
        "(AgentSomethingElse) must not be flagged -- exactly what \\b "
        "word-boundary matching exists to get right"
    )


# ---------------------------------------------------------------------------
# Lint (b) -- unresolved-variable check
# ---------------------------------------------------------------------------


def test_lint_b_clean_fixture_returns_no_violations(tmp_path):
    components_dir, harnesses_dir = _dirs(tmp_path)
    _write(
        components_dir / "greet.md",
        "---\n"
        "id: greet\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "variables: [NAME]\n"
        "---\n"
        "Hello {{NAME}}.\n",
    )
    _write_claude_harness(harnesses_dir, tool_names="[]", variables={"NAME": "World"})
    assert lints.lint_unresolved_variables(components_dir, harnesses_dir) == []


def test_lint_b_undeclared_variable_in_body_fires(tmp_path):
    components_dir, harnesses_dir = _dirs(tmp_path)
    _write(
        components_dir / "greet.md",
        "---\n"
        "id: greet\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "---\n"
        "Hello {{NAME}}.\n",  # NAME not declared in variables: frontmatter
    )
    _write_claude_harness(harnesses_dir, tool_names="[]", variables={"NAME": "World"})
    violations = lints.lint_unresolved_variables(components_dir, harnesses_dir)
    assert any(
        "greet.md" in v and "not declared in this component's variables" in v
        for v in violations
    )


def test_lint_b_declared_but_never_defined_anywhere_fires(tmp_path):
    components_dir, harnesses_dir = _dirs(tmp_path)
    _write(
        components_dir / "greet.md",
        "---\n"
        "id: greet\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: false\n"
        "variables: [GHOST]\n"
        "---\n"
        "Hello {{GHOST}}.\n",
    )
    # No harness contract anywhere defines GHOST.
    _write_claude_harness(harnesses_dir, tool_names="[]", variables={"NAME": "World"})
    violations = lints.lint_unresolved_variables(components_dir, harnesses_dir)
    assert any(
        "greet.md" in v and "is not defined in any harness contract" in v
        for v in violations
    )
    # GHOST *was* declared, so it must not also trip the "undeclared" branch.
    assert not any("not declared in this component's variables" in v for v in violations)


# ---------------------------------------------------------------------------
# Lint (c) -- extension_required coverage
# ---------------------------------------------------------------------------


def test_lint_c_clean_fixture_returns_no_violations(tmp_path):
    components_dir, harnesses_dir = _dirs(tmp_path)
    _write(
        components_dir / "base-a.md",
        "---\n"
        "id: base-a\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: true\n"
        "---\n"
        "Base body.\n",
    )
    _write(
        components_dir / "ext-a.md",
        "---\n"
        "id: ext-a\n"
        "kind: extension\n"
        "extends: base-a\n"
        "harness: claude\n"
        "composition_mode: flat\n"
        "---\n"
        "Extension body.\n",
    )
    _write_claude_harness(
        harnesses_dir,
        tool_names="[]",
        components=[{"id": "base-a"}],
    )
    assert lints.lint_extension_required_coverage(components_dir, harnesses_dir) == []


def test_lint_c_missing_extension_fires_naming_harness_and_component(tmp_path):
    components_dir, harnesses_dir = _dirs(tmp_path)
    _write(
        components_dir / "base-a.md",
        "---\n"
        "id: base-a\n"
        "kind: base\n"
        "composition_mode: flat\n"
        "extension_required: true\n"
        "---\n"
        "Base body with no extension anywhere.\n",
    )
    _write_claude_harness(
        harnesses_dir,
        tool_names="[]",
        components=[{"id": "base-a"}],
    )
    violations = lints.lint_extension_required_coverage(components_dir, harnesses_dir)
    assert violations
    assert any("claude" in v and "base-a" in v for v in violations)
