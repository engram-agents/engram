"""Tests for engram_core._reflect_rs_or_claim's named display-cap constants
(#2773, the fallback-cap follow-up from PR #2721).

PR #2721 named `_list_impl`'s two previously-bare display literals
(`LIST_CLAIM_SUMMARY_DISPLAY_CAP = 200`, `LIST_CLAIM_FALLBACK_DISPLAY_CAP =
130`) but deliberately left `_reflect_rs_or_claim`'s own bare `200` and
`max_len: int = 160` untouched -- out of scope, per that PR's own review.
This file exercises the two constants this PR adds:

    REFLECT_SUMMARY_DISPLAY_CAP = 200          -- same VALUE as LIST_CLAIM_
                                                   SUMMARY_DISPLAY_CAP, by
                                                   convention, NOT by binding
    REFLECT_CLAIM_FALLBACK_DISPLAY_CAP = 160   -- deliberately DIFFERENT from
                                                   LIST_CLAIM_FALLBACK_
                                                   DISPLAY_CAP (130) -- these
                                                   are two different bounds,
                                                   not two names for one

Both the "may share a value without sharing a binding" half and the "must
NOT share a name" half of #2721's round-6 naming-split rule are tested
directly, per that review's own reasoning (quoted in full on issue #2773).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engram_core as core
import engram_query as query


# ---------------------------------------------------------------------------
# The constants exist, are named as specified, and hold their intended values.
# ---------------------------------------------------------------------------

def test_reflect_summary_display_cap_is_200():
    assert core.REFLECT_SUMMARY_DISPLAY_CAP == 200


def test_reflect_claim_fallback_display_cap_is_160():
    assert core.REFLECT_CLAIM_FALLBACK_DISPLAY_CAP == 160


def test_reflect_rs_or_claim_default_max_len_is_the_named_constant():
    """The function's own default parameter value IS the constant object's
    value, not a second bare 160 that happens to agree with it."""
    import inspect
    sig = inspect.signature(core._reflect_rs_or_claim)
    assert sig.parameters["max_len"].default == core.REFLECT_CLAIM_FALLBACK_DISPLAY_CAP


# ---------------------------------------------------------------------------
# The summary cap MAY share a VALUE with engram_query's constant (both 200,
# by convention) but must NOT share a BINDING -- mutating one module's
# constant must not move the other's behavior.
# ---------------------------------------------------------------------------

def test_reflect_summary_cap_equals_list_summary_cap_by_convention():
    """Same value today -- the numeric convention #2721/#2773 document."""
    assert core.REFLECT_SUMMARY_DISPLAY_CAP == query.LIST_CLAIM_SUMMARY_DISPLAY_CAP


def test_reflect_summary_cap_is_not_bound_to_list_summary_cap_via_import():
    """Genuine independence, checked at the only level that can actually
    show it.

    A monkeypatch-based runtime test CANNOT distinguish "independently
    defined, coincidentally equal" from "computed once from the other
    module's constant at import time" -- both `int`s are immutable, so
    patching engram_query.LIST_CLAIM_SUMMARY_DISPLAY_CAP after both modules
    are already loaded can never move an already-bound engram_core module
    attribute either way, regardless of how it was originally assigned.
    (An earlier version of this test used exactly that monkeypatch and
    passed even when engram_core.py's source was mutated to read
    `REFLECT_SUMMARY_DISPLAY_CAP = engram_query.LIST_CLAIM_SUMMARY_DISPLAY_CAP`
    -- caught only by directly re-reading the source, not by running the
    vacuous runtime check.)

    The property that actually matters -- and the one Borges's round-6
    review on #2721 named ("Binding them makes an engram_list triage-display
    change silently alter engram_reflect output") -- is a SOURCE-LEVEL one:
    engram_core.py must never import engram_query at all. engram_query.py
    already imports engram_core (`import engram_core as core`); the reverse
    import would both create the exact accidental-coupling hazard the review
    warned about AND introduce a circular import between the two modules.

    Checked via the AST, not a substring match. Round-1 reviewer-fairy on
    PR #2775 found that a plain `"import engram_query" not in source` check
    is itself evadable -- `from engram_query import LIST_CLAIM_SUMMARY_
    DISPLAY_CAP as REFLECT_SUMMARY_DISPLAY_CAP` achieves the exact
    accidental-coupling hazard this test exists to catch while containing
    the substring "engram_query import", not "import engram_query" (the
    words are in the opposite order in a `from ... import ...` statement).
    Reproduced live: the substring version of this test passed unchanged
    under that exact mutation. An AST walk over `ast.Import`/`ast.ImportFrom`
    nodes has no such blind spot -- it inspects the parsed statement
    structure, not its literal token order.

    Round-1's own delta-confirmation fairy round on PR #2775 found a further
    evasion the AST-Import/ImportFrom walk alone misses: a dynamic
    `importlib.import_module("engram_query")` (or bare `__import__(...)`)
    call produces neither an `ast.Import` nor an `ast.ImportFrom` node at
    all -- it's an ordinary function call. Proved live that the coupling is
    real under that form (mutating engram_query's constant propagated to
    engram_core's). Extended the walk to also flag `ast.Call` nodes whose
    callee is `importlib.import_module` / a bare `import_module` (if
    imported directly from `importlib`) / `__import__`, with a string
    literal first argument naming engram_query.

    Residual, explicitly acknowledged rather than silently left implied
    fine: a sufficiently obfuscated dynamic import (e.g. a name built via
    string concatenation, or resolved through `getattr`/`globals()`) can
    still evade a purely static check. This is the same class of limit
    fairy_report.py's boundary-search fix documented earlier this session --
    no purely structural signal rules out an arbitrarily elaborate evasion
    of a static analysis. Closing the two well-known dynamic-import
    spellings is the proportionate stop here; chasing every further
    obfuscation has diminishing returns for code nobody has a reason to
    write this deviously by accident.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(core))
    offending = []

    def _names_engram_query(value: str | None) -> bool:
        return value == "engram_query" or (value or "").startswith("engram_query.")

    def _call_targets_dynamic_import(node: ast.Call) -> bool:
        func = node.func
        if isinstance(func, ast.Attribute):
            # importlib.import_module(...), or the attribute-form calls to
            # the __import__ builtin (importlib.__import__(...),
            # builtins.__import__(...)) round-3's own reviewer-fairy found
            # evade an import_module-only check -- same callable, reached
            # through a module attribute instead of a bare name.
            return func.attr in ("import_module", "__import__")
        if isinstance(func, ast.Name):
            # import_module(...) (from importlib import import_module) or __import__(...)
            return func.id in ("import_module", "__import__")
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _names_engram_query(alias.name):
                    offending.append(f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
        elif isinstance(node, ast.ImportFrom):
            if _names_engram_query(node.module):
                names = ", ".join(
                    a.name + (f" as {a.asname}" if a.asname else "") for a in node.names
                )
                offending.append(f"from {node.module} import {names}")
        elif isinstance(node, ast.Call) and _call_targets_dynamic_import(node):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and _names_engram_query(arg.value):
                    offending.append(f"dynamic import of {arg.value!r}")

    assert not offending, (
        "engram_core.py must not import engram_query in any form -- found: "
        f"{offending}. Doing so would let an engram_query-scoped edit "
        "silently change engram_reflect's output (the exact hazard #2721's "
        "round-6 review named), and would invert the module's existing "
        "one-directional dependency (query imports core, never core "
        "imports query)"
    )


# ---------------------------------------------------------------------------
# The fallback cap must NOT share a name (or, per #2721's own measurement,
# behavior) with engram_query's fallback cap -- these are two genuinely
# different bounds, not one bound with two names.
# ---------------------------------------------------------------------------

def test_reflect_fallback_cap_differs_from_list_fallback_cap():
    """Positive control: this divergence (160 vs 130) is INTENTIONAL, per
    #2721's round-6 measurement that all 13 of _reflect_rs_or_claim's call
    sites rely on the 160 default with none overriding it. A future edit
    that "fixes" these into alignment would be undoing a deliberate design
    decision, not closing a bug -- this test exists so that edit fails loud
    rather than silently landing."""
    assert core.REFLECT_CLAIM_FALLBACK_DISPLAY_CAP != query.LIST_CLAIM_FALLBACK_DISPLAY_CAP
    assert core.REFLECT_CLAIM_FALLBACK_DISPLAY_CAP == 160
    assert query.LIST_CLAIM_FALLBACK_DISPLAY_CAP == 130


# ---------------------------------------------------------------------------
# Boundary tests, edges derived from the constants themselves (not
# re-hardcoded numbers) -- same discipline #2721's own boundary tests used.
# ---------------------------------------------------------------------------

def test_recall_summary_exactly_at_cap_is_not_truncated():
    cap = core.REFLECT_SUMMARY_DISPLAY_CAP
    row = {"recall_summary": "S" * cap, "claim": "unused"}
    result = core._reflect_rs_or_claim(row)
    assert result == "S" * cap
    assert not result.endswith("...")


def test_recall_summary_one_past_cap_is_truncated_with_ellipsis():
    cap = core.REFLECT_SUMMARY_DISPLAY_CAP
    row = {"recall_summary": "S" * (cap + 1), "claim": "unused"}
    result = core._reflect_rs_or_claim(row)
    assert result == "S" * cap + "..."
    assert len(result) == cap + 3


def test_claim_fallback_exactly_at_default_max_len_is_not_truncated():
    max_len = core.REFLECT_CLAIM_FALLBACK_DISPLAY_CAP
    row = {"recall_summary": "", "claim": "C" * max_len}
    result = core._reflect_rs_or_claim(row)
    assert result == "C" * max_len
    assert not result.endswith("...")


def test_claim_fallback_one_past_default_max_len_is_truncated_with_ellipsis():
    max_len = core.REFLECT_CLAIM_FALLBACK_DISPLAY_CAP
    row = {"recall_summary": "", "claim": "C" * (max_len + 1)}
    result = core._reflect_rs_or_claim(row)
    assert result == "C" * max_len + "..."


def test_claim_fallback_respects_an_explicit_max_len_override():
    """max_len remains a genuine parameter, not hardcoded to the constant --
    the constant is only the DEFAULT."""
    row = {"recall_summary": "", "claim": "C" * 50}
    result = core._reflect_rs_or_claim(row, max_len=10)
    assert result == "C" * 10 + "..."
