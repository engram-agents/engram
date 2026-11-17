"""The Read Discipline section's factual claims, tied to the code they describe.

The section (templates/components/engram-read-discipline.md) is identity prose
an agent reads every session. Version 3 of it said deliberate recall "returns
full content" and that the auto-surface returns "node IDs and themes"; neither
was true of the code any more (Lei, 2026-11-17). Prose does not execute, so
each claim of version 4 is tied here to the entry point, default or action
table it describes: if one of those moves, this file goes red and the section
is re-read, instead of drifting again.

WHAT THIS FILE IS AND IS NOT. These are static reads (ast over the source, and
the stdlib-only engram_restamp): they check which functions an entry point
calls, what its defaults are, and what the action table holds. They do not run
a query. The BEHAVIOUR itself is tested where it always was:
tests/test_query_refresh_recall.py (a query restamps; refresh_recall=False does
not), tests/test_use_sites_restamp_contract.py (every use site restamps and
rewards) and tests/test_mention_restamp_contract.py (a prose mention does
both). A call that exists but is switched off by default would defeat a bare
call-name check, so each cell below also pins the default that keeps the call
live (reviewer round 1, mutants M1, M2 and M4).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
COMPONENT = SRC / "templates" / "components" / "engram-read-discipline.md"
QUERY_PY = SRC / "engram_query.py"
HOOKS = SRC / "hooks" / "claude"
SURFACE_HOOK = HOOKS / "engram-surface-hook.py"
SURFACE_DAEMON = HOOKS / "engram-surface-daemon.py"
MENTION_HOOK = HOOKS / "engram-utility-credit-mention-stop.py"

# Every name through which a node's recall clock or utility is written.
REFRESH_WRITERS = frozenset(
    {"_refresh_recall", "_utility_reward", "restamp", "use_reward", "bump_utility"}
)
_REFRESH_WRITER_RE = re.compile(r"\b(" + "|".join(sorted(REFRESH_WRITERS)) + r")\b")


def _section() -> str:
    return COMPONENT.read_text(encoding="utf-8")


def _func(name: str, path: Path = QUERY_PY) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, f"expected exactly one top-level def {name} in {path.name}, found {len(found)}"
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


def test_about_ten_nodes_is_what_the_prompt_hook_asks_for():
    """'shows about ten nodes': the count is decided by what the prompt hook
    asks for, so every default on that path is pinned, not only the server's."""
    assert _section().count("about ten nodes") == 2
    assert _default(_func("_surface_impl"), "top_k") == 10
    assert _default(_func("query_daemon", SURFACE_HOOK), "top_k") == 10
    assert _default(_func("query_fts_fallback", SURFACE_HOOK), "top_k") == 10
    assert SURFACE_DAEMON.read_text(encoding="utf-8").count('request.get("top_k", 10)') == 1


def test_query_returns_hints_not_the_record():
    """'summaries for the top results and keywords below': the tiered shape is
    the DEFAULT reply (return_debug off), with a summary tier smaller than the
    result count."""
    assert "summaries for the top results and keywords below" in _section()
    q = _func("_query_impl")
    assert "_build_tiered_results" in _called_names(q)
    assert _default(q, "return_debug") is False
    assert 0 < _default(q, "summary_top_k") < _default(q, "top_k")


def test_surface_refreshes_nothing():
    """'It refreshes nothing': no writer of the recall clock or of utility is
    called by the surface entry point, nor named anywhere in the hook and the
    daemon that render it. Positive control: the same search finds the writers
    in the mention hook, which does refresh."""
    assert "It refreshes nothing." in _section()
    assert not (_called_names(_func("_surface_impl")) & REFRESH_WRITERS)
    for path in (SURFACE_HOOK, SURFACE_DAEMON):
        hits = _REFRESH_WRITER_RE.findall(path.read_text(encoding="utf-8"))
        assert not hits, f"{path.name} names a refresh writer: {sorted(set(hits))}"
    assert _REFRESH_WRITER_RE.findall(MENTION_HOOK.read_text(encoding="utf-8"))


def test_query_restamps_by_default_and_awards_no_utility():
    """'resets the forgetting clock of each node it returns': the restamp call
    is there AND on by default; a query awards no utility."""
    assert "resets the forgetting clock of each node it returns" in _section()
    q = _func("_query_impl")
    calls = _called_names(q)
    assert "_refresh_recall" in calls
    assert _default(q, "refresh_recall") is True
    assert "_utility_reward" not in calls


def test_inspect_is_use_and_subgraph_refreshes_neighbours():
    """'Use means inspecting it ...' / 'refreshes the neighbours it shows'."""
    assert "refreshes the neighbours it shows" in _section()
    inspect_calls = _called_names(_func("_inspect_impl"))
    assert "_refresh_recall" in inspect_calls and "_utility_reward" in inspect_calls
    assert "_refresh_recall" in _called_names(_func("_subgraph_impl"))


def test_naming_an_id_or_citing_a_premise_is_use():
    """'... citing it as a premise, or naming its id in my reply': both are
    use actions that raise utility AND restamp, and the mention hook is wired
    to the shared table under its own key."""
    assert "naming its id in my reply" in _section()
    sys.path.insert(0, str(SRC))
    try:
        import engram_restamp
    finally:
        sys.path.remove(str(SRC))
    for action in ("mention", "derive", "citation"):
        assert engram_restamp.USE_ALPHA[action] > 0, action
        assert action in engram_restamp.RESTAMP_ACTIONS, action
    tree = ast.parse(MENTION_HOOK.read_text(encoding="utf-8"))
    actions = [
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_ACTION" for t in n.targets)
    ]
    assert actions == ["mention"]
    called = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "use_reward" in called
