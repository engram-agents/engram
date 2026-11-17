"""The Read Discipline section's factual claims, bound to the code they describe.

The section (templates/components/engram-read-discipline.md) is identity prose
an agent reads every session. Version 3 of it said deliberate recall "returns
full content" and that the auto-surface returns "node IDs and themes"; neither
was true of the code any more (Lei, 2026-11-17). Prose does not execute, so
each claim of version 4 is asserted here against its source: if the code moves,
this file goes red and the section is re-read, instead of drifting again.

Static reads only (ast + the stdlib-only engram_restamp), so nothing here opens
a store or loads the embedding model.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
COMPONENT = SRC / "templates" / "components" / "engram-read-discipline.md"
QUERY_PY = SRC / "engram_query.py"


def _section() -> str:
    return COMPONENT.read_text(encoding="utf-8")


def _func(name: str) -> ast.FunctionDef:
    tree = ast.parse(QUERY_PY.read_text(encoding="utf-8"))
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, f"expected exactly one top-level def {name}, found {len(found)}"
    return found[0]


def _default(fn: ast.FunctionDef, arg: str):
    args = fn.args.args
    defaults = fn.args.defaults
    offset = len(args) - len(defaults)
    for i, a in enumerate(args):
        if a.arg == arg:
            assert i >= offset, f"{fn.name}({arg}) has no default"
            return ast.literal_eval(defaults[i - offset])
    raise AssertionError(f"{fn.name} has no parameter {arg}")


def _called_names(fn: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def test_the_two_retired_sentences_stay_retired():
    text = _section()
    assert "returns full content" not in text
    assert "node IDs and themes" not in text


def test_surface_and_query_are_one_search():
    """'the same search ... the same ranking': both call _search_nodes."""
    assert "the same ranking" in _section()
    assert "_search_nodes" in _called_names(_func("_surface_impl"))
    assert "_search_nodes" in _called_names(_func("_query_impl"))


def test_about_ten_nodes_is_the_surface_default():
    """'shows about ten nodes': the surface's own default top_k."""
    assert _section().count("about ten nodes") == 2
    assert _default(_func("_surface_impl"), "top_k") == 10


def test_query_returns_hints_not_the_record():
    """'summaries for the top results and keywords below': the tiered builder, with a small summary tier."""
    assert "summaries for the top results and keywords below" in _section()
    assert "_build_tiered_results" in _called_names(_func("_query_impl"))
    assert 0 < _default(_func("_query_impl"), "summary_top_k") < _default(_func("_query_impl"), "top_k")


def test_surface_refreshes_nothing_and_query_restamps():
    """'It refreshes nothing' (surface) / 'resets the forgetting clock' (query)."""
    text = _section()
    assert "It refreshes nothing." in text
    assert "resets the forgetting clock of each node it returns" in text
    surface_calls = _called_names(_func("_surface_impl"))
    assert "_refresh_recall" not in surface_calls
    assert "_utility_reward" not in surface_calls
    query_calls = _called_names(_func("_query_impl"))
    assert "_refresh_recall" in query_calls
    # A query restamps; it does not award utility. Utility comes from use.
    assert "_utility_reward" not in query_calls


def test_inspect_is_use():
    """'Use means inspecting it ...': inspect restamps and awards utility."""
    calls = _called_names(_func("_inspect_impl"))
    assert "_refresh_recall" in calls and "_utility_reward" in calls


def test_naming_an_id_or_citing_a_premise_is_use():
    """'... citing it as a premise, or naming its id in my reply': both are
    use actions that raise utility AND restamp."""
    assert "naming its id in my reply" in _section()
    sys.path.insert(0, str(SRC))
    try:
        import engram_restamp
    finally:
        sys.path.remove(str(SRC))
    for action in ("mention", "derive", "citation"):
        assert engram_restamp.USE_ALPHA[action] > 0, action
        assert action in engram_restamp.RESTAMP_ACTIONS, action
    hook = SRC / "hooks" / "claude" / "engram-utility-credit-mention-stop.py"
    assert 'action="mention"' in hook.read_text(encoding="utf-8") or "\"mention\"" in hook.read_text(encoding="utf-8")
