"""Test SKILL.md ↔ server.py drift on MCP tool surface.

The canonical SKILL.md §4 Tool Index documents every MCP tool the agent
is expected to know about. server.py defines those tools via `@mcp.tool()`
decorators. When server.py adds, removes, or renames a tool, SKILL.md
should be updated in the same PR — otherwise post-compaction agents
discover the tool only by accident (e.g., via ToolSearch surfacing it),
not by reading the protocol.

Prior incidents this test exists to prevent:
- ob_0966 (foundation-tier SKILL.md staleness — tool count 13 vs 24 vs actual ~30)
- ob_0967 (four-surface foundation rot — each surface drifts independently)
- 2026-05-11: engram_query_pattern shipped in PR #29 (2026-05-07) but SKILL.md
  had ZERO mention for 4 days until Lei caught it; same incident motivated
  this test.

Scope: function-name matching only (MVP). Signature matching is a future
extension when names alone produce too many false-positives or too few
real catches.

Allowlist semantics: a tool may be intentionally undocumented in SKILL.md
(internal helpers, deprecated-but-still-callable shims). Add to
SERVER_ONLY_ALLOWLIST below with a one-line reason. A tool documented in
SKILL.md that's not in server.py is ALWAYS a bug (you cannot use a tool
that doesn't exist) — there is no symmetric allowlist.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PY = REPO_ROOT / "server.py"
SKILL_MD = REPO_ROOT / "SKILL.md"

# Tools defined in server.py but intentionally not documented in SKILL.md.
# Add entries here ONLY with a documented reason; the test enforces this
# allowlist remains short.
SERVER_ONLY_ALLOWLIST: dict[str, str] = {
    # No entries currently — every shipped tool should be in SKILL.md.
}


def extract_server_tools(server_path: Path) -> set[str]:
    """Parse server.py via AST for every @mcp.tool()-decorated function.

    The tool's name is either the `name=` kwarg on the decorator OR the
    function's def name as default. Both shapes are handled.
    """
    tree = ast.parse(server_path.read_text())
    tools: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            # @mcp.tool(...) — decorator is an ast.Call with func.attr == "tool".
            # Intentionally checks only `.attr == "tool"`, not `.value.id == "mcp"` —
            # server.py has exactly one mcp instance, so the namespace ambiguity is
            # not a real risk. Loosening would let a future @other.tool() escape.
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not (isinstance(func, ast.Attribute) and func.attr == "tool"):
                continue
            # Look for name= kwarg
            tool_name = node.name  # default: function name
            for kw in decorator.keywords:
                if kw.arg == "name":
                    # Hard-assert string-literal contract: name= must be a
                    # constant. Falling back silently to function-name on
                    # non-Constant (e.g. name=SOME_VAR or f-string) would be
                    # a silent-wrong-answer path — fairy round-1 round-2
                    # suggestion 1.
                    assert isinstance(kw.value, ast.Constant) and isinstance(
                        kw.value.value, str
                    ), (
                        f"@mcp.tool(name=...) on `def {node.name}` must be a "
                        f"string literal; got {ast.dump(kw.value)}. This test's "
                        f"AST extraction contract requires string-literal names "
                        f"so the tool-name set is deterministic from source."
                    )
                    tool_name = kw.value.value
                    break
            tools.add(tool_name)
    return tools


def extract_skill_md_tools(skill_path: Path) -> set[str]:
    """Parse SKILL.md for tool names referenced in the §4 Tool Index.

    Convention: tool entries appear as `**engram_NAME** — description.`
    in bold-bullet form within the §4 Tool Index. Only the §4 section is
    scanned to avoid false-positive matches from prose elsewhere (e.g.,
    workflow examples that reference tools but don't define entries).
    """
    text = skill_path.read_text()
    # Locate §4 Tool Index — start at "## §4" heading
    section_start = text.find("## §4 Tool Index")
    if section_start < 0:
        # Fallback: try variant spelling
        section_start = text.find("## §4 ")
    if section_start < 0:
        raise AssertionError("Could not locate §4 Tool Index in SKILL.md")
    # End at next ## heading (start of §5 or later)
    section_end = text.find("\n## ", section_start + 1)
    if section_end < 0:
        section_end = len(text)
    tool_index_section = text[section_start:section_end]
    # Match bullet entries: "- **engram_NAME** —" or "- **engram_NAME**".
    # [a-z0-9_]+ admits future tool names containing digits (e.g.
    # `engram_v2_derive`) per fairy round-1 suggestion 2; today no shipped
    # tool name has a digit so this is currently a no-op widening.
    pattern = re.compile(r"\*\*engram_[a-z0-9_]+\*\*")
    tools: set[str] = set()
    for match in pattern.finditer(tool_index_section):
        name = match.group(0).strip("*")
        tools.add(name)
    return tools


def test_skill_md_documents_every_server_tool() -> None:
    """Every @mcp.tool()-decorated function in server.py appears in SKILL.md §4.

    Failure mode: a tool was added to server.py without a corresponding
    entry in SKILL.md, so post-compaction agents won't discover it from
    the canonical protocol doc. This is the case that triggered this
    test's existence (engram_query_pattern, 2026-05-11).
    """
    server_tools = extract_server_tools(SERVER_PY)
    skill_tools = extract_skill_md_tools(SKILL_MD)
    undocumented = server_tools - skill_tools - set(SERVER_ONLY_ALLOWLIST.keys())
    assert not undocumented, (
        f"Tools defined in server.py but not documented in SKILL.md §4: "
        f"{sorted(undocumented)}. Either add them to SKILL.md §4 Tool Index "
        f"(canonical fix) or add to SERVER_ONLY_ALLOWLIST in this test file "
        f"with a one-line reason (only if the tool is intentionally undocumented)."
    )


def test_no_phantom_tools_in_skill_md() -> None:
    """Every tool documented in SKILL.md §4 actually exists in server.py.

    Failure mode: a tool was removed/renamed in server.py but SKILL.md
    still documents the old name. Agents reading SKILL.md would try to
    call a tool that no longer exists.
    """
    server_tools = extract_server_tools(SERVER_PY)
    skill_tools = extract_skill_md_tools(SKILL_MD)
    phantom = skill_tools - server_tools
    assert not phantom, (
        f"Tools documented in SKILL.md §4 but not defined in server.py: "
        f"{sorted(phantom)}. Remove the SKILL.md entries (the tools no "
        f"longer exist) or restore the @mcp.tool() decorator (the tools "
        f"should still exist)."
    )


def test_allowlist_entries_have_reasons() -> None:
    """Every SERVER_ONLY_ALLOWLIST entry must have a non-empty reason.

    Prevents the allowlist from becoming a silent suppression channel.
    """
    for tool_name, reason in SERVER_ONLY_ALLOWLIST.items():
        assert reason.strip(), (
            f"Allowlist entry for {tool_name!r} has empty reason. "
            f"Either document why this tool is intentionally undocumented "
            f"in SKILL.md, or remove from the allowlist."
        )
