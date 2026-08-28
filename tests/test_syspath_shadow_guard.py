"""Durable guard for #2063: a top-level tools/*.py must never be able to
shadow a same-named src/ package via `sys.path.insert(0, ...)`.

## Background

`tools/forum.py` (a CLI script) and `src/forum/` (the real package with
`db.py` etc.) share the bare importable name `forum`. Several test files
used to do `sys.path.insert(0, <tools-ish dir>)` -- unconditionally, at
position 0 -- to reach their own tools/-only target (`forum_backup.py`,
`baton.py`, `config_schema.py`, ...). That made the tools/-ish directory a
sys.path *root* ranked ahead of `src/forum` for the rest of the pytest
process, so any test collected afterward that does `from forum import db`
silently resolved to the CLI script instead and failed with a confusing
`ImportError: cannot import name 'db' from 'forum'` -- or, worse, aborted
collection for the ENTIRE suite (#2037). #2063 named the general class:
tools/*.py can shadow ANY same-named src/ package this way, `forum` is
just the pair that collided first.

The concrete fix (this issue's own PR) changed every offending
`insert(0, ...)` to `append(...)` (or dropped it where it was already
redundant) so a tools/-ish directory never outranks a real src/ package.
This file is the "durable guard" the issue itself asks for: it does not
merely check today's known collision, it fails loudly the moment ANY test
file (present or future) reintroduces the anti-pattern, naming the file,
line, and the colliding names -- so file #8 (the issue's own phrase) gets
caught before it ever reaches the confusing collection-time failure.

## Bound (#2111)

The guard detects a specific, named set of mechanisms for putting a
tools-ish directory at `sys.path` position 0: `sys.path.insert(0, ...)`
(directly, or reached via a `p = sys.path` value-alias, an `import sys as s`
module-alias, `from sys import path`, or `getattr(sys, "path")`), the
in-place slice-assignment forms `sys.path[0:0] = [...]` / `sys.path[:0] =
[...]` (and the same through any of the same alias/import/getattr forms,
since slice-assignment mutates the shared list object), and direct
rebinding of the module attribute itself (`sys.path = [...] + sys.path`).
It is **not** a general points-to analysis of every conceivable way to
manipulate `sys.path` -- e.g. `sys.path.__setitem__`, augmented assignment
(`sys.path[0:0] += [...]`), `unittest.mock.patch("sys.path", ...)`, or
mutation through a name that only *copied* a value once and was never
tracked back to `sys.path` -- and does not attempt to be.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "src" / "engram" / "tools"  # canonical; top-level
                                                     # tools/ symlinks here
SRC_DIR = REPO_ROOT / "src"

# Every test file this repo's suite actually collects. Scoped to the two
# roots CI runs (`pytest tests/ src/forum/tests/`, per
# .github/workflows/tests.yml) rather than every *.py in the tree, so this
# stays a guard on the *suite*, not an unbounded repo-wide lint.
_TEST_ROOTS = [REPO_ROOT / "tests", REPO_ROOT / "src" / "forum" / "tests"]


_THIS_FILE = Path(__file__).resolve()


def _all_test_files() -> list[Path]:
    files: list[Path] = []
    for root in _TEST_ROOTS:
        if root.is_dir():
            files.extend(sorted(root.glob("*.py")))
    # Exclude this guard file itself -- its own docstrings/comments
    # necessarily discuss the `sys.path.insert(0, ...)` anti-pattern in
    # prose, which trips the same regex the guard uses to detect it.
    return [f for f in files if f.resolve() != _THIS_FILE]


# ---------------------------------------------------------------------------
# Part 1: static regression guard -- no test file may `insert(0, ...)` a
# tools-ish directory. Parametrized per file so a violation names exactly
# which file (and which line) reintroduced the anti-pattern.
#
# #2089: the original per-line regex scan (`_INSERT0_RE` + `_resolve_arg_text`)
# was demonstrated bypassable four ways in PR #2088's adversarial review --
# a multi-line call, `sys.path` aliasing, two-hop name indirection, and
# string-concat literals -- plus a side defect where an assignment itself
# split across lines was missed. This replaces the regex scan with an
# AST-based one: it parses each file, finds every `<expr>.insert(...)` Call
# whose target resolves to `sys.path` (directly, or through a *scoped* alias
# table covering both module-level and per-function `name = <expr>`
# assignments -- kills the aliasing bypass), confirms the first argument
# resolves to the literal `0` (directly or through the same alias table),
# and constant-folds the second argument (string literals, `str(x)`, `+`
# concatenation, `/` path-joins, `%`-formatting, `.format(...)`, and
# fragment-collection through list/tuple/dict/subscript containers,
# resolved through the alias table) to catch a `tools` path component
# wherever it appears -- even nested inside an otherwise-unfoldable
# expression (the two-hop and concat bypasses), and even when the folding
# assignment itself spans multiple lines (the side defect). Being AST-based
# also makes the multi-line-call bypass a non-issue: `ast.parse` sees the
# same Call node regardless of how it is broken across lines.
#
# Round 2 (#2089 adversarial re-review of the round-1 AST scan): the
# round-1 alias table was a single FLAT, module-wide dict built by one
# `ast.walk` pass over the entire module, so a `name = <expr>` assignment
# inside one function silently overwrote the same-named assignment inside
# an unrelated function elsewhere in the file (last-assignment-wins,
# independent of which function a use site is actually in). That both
# masked a real violation (an earlier function's tools-ish `insert(0, ...)`
# got resolved against a LATER, unrelated function's non-tools reassignment
# of the same name) and produced a false positive (an earlier function's
# legitimate, non-tools-ish `insert(0, ...)` got resolved against a LATER,
# unrelated function's same-named tools-ish assignment). Fixed by scoping
# alias resolution per enclosing function: a MODULE scope table is built
# from only the assignments that are directly at module level (not inside
# any function); each function additionally gets its OWN scope table built
# from `module scope` merged with `that function's own local assignments`
# (locals shadow same-named module names, matching normal Python scoping).
# A call site inside function F is resolved against F's own table, never
# against a sibling function's locals.
#
# Round 2 shipped nested functions `their own locals + the module scope
# only` -- NOT their enclosing function's locals -- as "the simpler of the
# two options ... sufficient to kill both demonstrated cases". Round 3's
# verification found that shortcut is itself a coverage regression vs
# round 1's flat table: an ordinary closure --
#
#     def outer():
#         D = Path('/x') / 'tools'
#         def inner():
#             sys.path.insert(0, str(D))   # closure over outer's local D
#
# -- was silently NOT flagged, because `inner`'s scope table never saw
# `outer`'s locals at all. Fixed here by proper lexical chaining: each
# function's effective resolution table is `module scope -> each enclosing
# function's own locals, innermost-enclosing last -> that function's own
# locals`, later entries shadowing earlier ones -- i.e. real Python LEGB
# scoping (minus globals/builtins, which this guard has no need of). This
# is built in two passes: first, every function's OWN locals are collected
# in isolation (unchanged from round 2); second, each function's ancestry
# chain (its enclosing FunctionDef/AsyncFunctionDef nodes, outermost first)
# is walked with an explicit parent-stack (not `ast.walk`, which loses
# nesting order) and the effective table is `merge(module, *[own-locals of
# each ancestor, outermost -> innermost], own-locals of the function
# itself)`. SIBLING isolation (the round-2 regression fixtures below) is
# preserved because two sibling functions never appear in each other's
# ancestry chain -- only an actual lexical enclosure contributes locals.
# ---------------------------------------------------------------------------

# A resolved path segment counts as "tools-ish" if `tools` appears as an
# exact `/`-separated path component -- this catches both the top-level
# `tools/` dir and its canonical `src/engram/tools/` target (the symlink
# farm), which is the actual defect surface (it's where `forum.py`
# physically lives), while not false-triggering on an unrelated substring
# match like `toolshed`.
def _is_tools_ish_text(text: str) -> bool:
    parts = re.split(r"[\\/]", text)
    return any(part == "tools" for part in parts)


_MAX_ALIAS_CHAIN_DEPTH = 8


def _iter_scope_nodes(root: ast.AST):
    """Yield every descendant of *root* (not including *root* itself) that
    belongs to *root*'s own scope -- i.e. does NOT recurse into a nested
    `FunctionDef`/`AsyncFunctionDef`'s body (the nested function is its own
    separate scope, handled as its own call to this same generator). The
    nested function node itself IS yielded (so callers scanning for
    `FunctionDef` nodes still see it); only its children are skipped."""
    stack = list(ast.iter_child_nodes(root))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _collect_assignments(nodes) -> "dict[str, ast.expr]":
    """Extract every simple `name = <expr>` / `name: T = <expr>` assignment
    from an already-scoped node sequence (as produced by
    `_iter_scope_nodes`) -- last assignment within THAT scope wins, which is
    correct scoping (not the round-1 whole-module flattening bug: the node
    sequence passed in is already scope-bounded)."""
    assignments: dict[str, ast.expr] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                assignments[node.target.id] = node.value
    return assignments


class _AliasTable:
    """Map of `name -> ast.expr` for one resolution scope (module scope, or
    one function's own scope merged with the module scope beneath it --
    see the callers in `_find_insert0_violations`). Deliberately not a full
    symbol table: #2089 only asks for enough to catch the aliasing bypass
    (`p = sys.path`) and chained-name indirection (`_D = ROOT / _SEG`)
    *within the scope a use site actually lives in*, not a flat whole-file
    table."""

    def __init__(self, assignments: "dict[str, ast.expr]") -> None:
        self._assignments = assignments

    def lookup(self, name: str) -> "ast.expr | None":
        return self._assignments.get(name)


def _collect_import_bindings(nodes) -> "tuple[set[str], set[str]]":
    """Scope-bounded scan (same *nodes* shape as `_collect_assignments`,
    i.e. already produced by `_iter_scope_nodes`) for two binding classes
    #2111 needs beyond `name = <expr>`: names bound to the `sys` MODULE
    object (`import sys as s` -- bare `import sys` also lands here via the
    caller unioning in the literal `"sys"`), and names bound directly to
    the `sys.path` ATTRIBUTE (`from sys import path` / `from sys import
    path as p`). Returns ``(sys_module_names, sys_path_names)``, each a
    plain (unscoped-merge) set -- the caller chains these across the same
    module/ancestor/own lexical scoping `_find_insert0_violations` already
    does for the assignment-alias table."""
    sys_module_names: set[str] = set()
    sys_path_names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias_node in node.names:
                if alias_node.name == "sys":
                    sys_module_names.add(alias_node.asname or "sys")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "sys":
                for alias_node in node.names:
                    if alias_node.name == "path":
                        sys_path_names.add(alias_node.asname or "path")
    return sys_module_names, sys_path_names


def _is_sys_path_attribute(node: ast.expr, sys_module_names: frozenset) -> bool:
    """STRICT form: True only if *node* is literally the `sys.path`
    ATTRIBUTE expression itself -- directly (`sys.path`), through a module
    alias (`import sys as s` -> `s.path`), or through a dynamic lookup
    (`getattr(sys, "path")` / `getattr(s, "path")`). Deliberately does NOT
    chase the value-alias chain (`p = sys.path`) or a direct-imported name
    (`from sys import path` -> bare `path`) the way `_is_sys_path_expr`
    does for a *read* site (an `.insert(...)`/slice-mutation receiver) --
    this form exists only for #2111's rebinding-target check
    (`sys.path = ...`), where reassigning a name that merely COPIED a
    reference (`p = sys.path; p = [...]`, or `from sys import path; path =
    [...]`) rebinds only the LOCAL name and never touches the real
    `sys.path` attribute on the module -- not a violation, and conflating
    it with one would be a false positive."""
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id in sys_module_names
    ):
        return True
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) == 2
        and not node.keywords
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in sys_module_names
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "path"
    ):
        return True
    return False


def _is_sys_path_expr(
    node: ast.expr,
    alias: _AliasTable,
    sys_module_names: frozenset,
    sys_path_names: frozenset,
    seen: frozenset = frozenset(),
) -> bool:
    """True if *node* resolves to `sys.path` -- directly or via a module
    alias or dynamic `getattr` (delegated to the strict
    `_is_sys_path_attribute` check, #2111), a name directly imported via
    `from sys import path` (#2111), or through a chain of alias-table
    lookups (bypass #2: `p = sys.path`). This is the permissive form used
    for anything that only READS/mutates-in-place through the reference
    (an `.insert(...)` receiver, or a slice-assignment target) -- all of
    these forms genuinely reach the same underlying list object, unlike
    the rebinding case `_is_sys_path_attribute` alone guards."""
    if _is_sys_path_attribute(node, sys_module_names):
        return True
    if isinstance(node, ast.Name):
        if node.id in sys_path_names:
            return True
        if node.id in seen or len(seen) >= _MAX_ALIAS_CHAIN_DEPTH:
            return False
        target = alias.lookup(node.id)
        if target is None:
            return False
        return _is_sys_path_expr(
            target, alias, sys_module_names, sys_path_names, seen | {node.id}
        )
    return False


def _resolve_text(
    node: ast.expr, alias: _AliasTable, seen: frozenset = frozenset()
) -> "tuple[str | None, list[str]]":
    """Best-effort constant-folder for the second `sys.path.insert(0, ...)`
    argument.

    Returns ``(full_value, fragments)``:
      - ``full_value`` is the exact folded string when *every* part of
        *node* resolves to a literal; ``None`` when any part does not.
      - ``fragments`` is every literal string segment reachable while
        trying, even where full resolution fails -- so a `tools` literal
        buried inside an otherwise-opaque expression (bypass #3's
        `ROOT / _SEG` where `ROOT` is a runtime `Path(__file__)...` chain,
        or bypass #4's `str(ROOT) + "/src/engram/tools"`) is still found
        even though the outer expression can't be fully folded.
    """
    if len(seen) > _MAX_ALIAS_CHAIN_DEPTH:
        return None, []

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, [node.value]

    if isinstance(node, ast.Name):
        if node.id in seen:
            return None, []
        target = alias.lookup(node.id)
        if target is None:
            return None, []
        return _resolve_text(target, alias, seen | {node.id})

    if isinstance(node, ast.Call):
        # str(x) -- stringification; fold through to the argument.
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "str"
            and len(node.args) == 1
            and not node.keywords
        ):
            return _resolve_text(node.args[0], alias, seen)
        # #2089 suggestion (c): '...'.format(...) -- fold the same way
        # str(x) is unwrapped above: walk the format-string base and every
        # positional arg for literal fragments.
        if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            base_val, base_frags = _resolve_text(node.func.value, alias, seen)
            fragments: list[str] = list(base_frags)
            for a in node.args:
                _, frags = _resolve_text(a, alias, seen)
                fragments.extend(frags)
            return None, fragments
        # Any other call (Path(...), os.path.dirname(...), a helper
        # function, ...) is opaque as a whole, but its arguments may still
        # carry a stray literal fragment worth checking.
        fragments: list[str] = []
        for a in node.args:
            _, frags = _resolve_text(a, alias, seen)
            fragments.extend(frags)
        return None, fragments

    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
        left_val, left_frags = _resolve_text(node.left, alias, seen)
        right_val, right_frags = _resolve_text(node.right, alias, seen)
        fragments = left_frags + right_frags
        if left_val is not None and right_val is not None:
            sep = "" if isinstance(node.op, ast.Add) else "/"
            return left_val + sep + right_val, fragments
        return None, fragments

    if isinstance(node, ast.JoinedStr):  # f-strings -- collect literal parts
        fragments = [
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return None, fragments

    # #2089 suggestion (b): containers don't fold to one string, but a
    # tools-ish literal buried inside one (`SEGS = ['tools']; ... / SEGS[0]`)
    # is still worth catching as a fragment, the same way an unfoldable
    # Call's args are scanned above.
    if isinstance(node, (ast.List, ast.Tuple)):
        fragments = []
        for elt in node.elts:
            _, frags = _resolve_text(elt, alias, seen)
            fragments.extend(frags)
        return None, fragments

    if isinstance(node, ast.Dict):
        fragments = []
        for v in node.values:
            if v is None:  # `**spread` entry -- no literal value to walk
                continue
            _, frags = _resolve_text(v, alias, seen)
            fragments.extend(frags)
        return None, fragments

    if isinstance(node, ast.Subscript):
        # `SEGS[0]` -- don't try to resolve the index, just walk the
        # container being subscripted for stray literal fragments.
        _, frags = _resolve_text(node.value, alias, seen)
        return None, frags

    # #2089 suggestion (c): %-formatting and .format(...) calls are folded
    # the same way str(x) is unwrapped above -- the format string itself
    # (and any interpolated args) are walked for literal fragments, which
    # is enough to catch `'%s/tools' % root` and `'{}/tools'.format(root)`
    # even without simulating the actual substitution.
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        left_val, left_frags = _resolve_text(node.left, alias, seen)
        right_frags: list[str] = []
        if isinstance(node.right, (ast.Tuple, ast.List)):
            for elt in node.right.elts:
                _, frags = _resolve_text(elt, alias, seen)
                right_frags.extend(frags)
        else:
            _, frags = _resolve_text(node.right, alias, seen)
            right_frags.extend(frags)
        return None, left_frags + right_frags

    return None, []


def _resolves_to_zero(
    node: ast.expr, alias: "_AliasTable", seen: frozenset = frozenset()
) -> bool:
    """True if *node* is the literal `0`, or resolves to it through a chain
    of alias-table lookups (#2089 suggestion (a): `Z = 0; insert(Z, ...)`
    should flag, symmetric with how the second argument is resolved)."""
    if (
        isinstance(node, ast.Constant)
        and not isinstance(node.value, bool)
        and node.value == 0
    ):
        return True
    if isinstance(node, ast.Name):
        if node.id in seen or len(seen) >= _MAX_ALIAS_CHAIN_DEPTH:
            return False
        target = alias.lookup(node.id)
        if target is None:
            return False
        return _resolves_to_zero(target, alias, seen | {node.id})
    return False


def _check_insert_call(call: ast.Call, alias: _AliasTable) -> "tuple[bool, str]":
    """*call* is already known to be `<expr>.insert(...)` on a `sys.path`-
    resolving target. Returns ``(is_violation, detail)``."""
    if len(call.args) < 2:
        return False, ""
    if not _resolves_to_zero(call.args[0], alias):
        return False, ""
    full_value, fragments = _resolve_text(call.args[1], alias)
    if full_value is not None:
        if _is_tools_ish_text(full_value):
            return True, f"resolved to {full_value!r}"
        return False, ""
    # Full resolution failed (the second argument isn't a pure constant
    # expression this folder can fully evaluate -- e.g. it bottoms out at
    # a runtime Path(__file__)... chain). Fail toward flagging: if ANY
    # literal fragment reachable along the way is itself tools-ish, treat
    # it as a violation rather than silently passing on the strength of
    # the rest being unresolvable (this is what catches bypass #3's
    # two-hop indirection and bypass #4's string-concat literal, neither
    # of which folds to one exact constant). An expression with NO literal
    # fragments at all (e.g. `os.path.dirname(os.path.dirname(os.path.
    # abspath(__file__)))`, the ordinary same-repo bootstrap idiom used
    # throughout this suite) carries no evidence either way and is not
    # flagged -- see the #2089 fairy handoff for why an unconditional
    # "unresolvable == violation" policy is not viable against this
    # suite's ~300 existing `sys.path.insert(0, ...)` bootstrap lines.
    if any(_is_tools_ish_text(f) for f in fragments):
        return True, f"unresolved expression contains literal fragment(s) {fragments!r}"
    return False, ""


def _scan_calls(
    nodes,
    alias: "_AliasTable",
    sys_module_names: frozenset,
    sys_path_names: frozenset,
) -> "list[tuple[int, str]]":
    """Check every `Call` node in *nodes* (a scope-bounded node sequence)
    against *alias*, the resolution table for THAT scope. Shared by the
    module-level scan and every per-function scan below."""
    offenders: list[tuple[int, str]] = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "insert"):
            continue
        if not _is_sys_path_expr(func.value, alias, sys_module_names, sys_path_names):
            continue
        is_violation, detail = _check_insert_call(node, alias)
        if is_violation:
            offenders.append((node.lineno, detail))
    return offenders


def _scan_assignments(
    nodes,
    alias: "_AliasTable",
    sys_module_names: frozenset,
    sys_path_names: frozenset,
) -> "list[tuple[int, str]]":
    """#2111 (b): the two mechanisms that reach `sys.path` position 0
    without any `.insert(...)` call for `_scan_calls` to see -- neither
    defeats the call-scanner, both defeat the underlying INTENT the guard
    exists to enforce.

    - **Slice assignment** (`sys.path[0:0] = [...]` / `sys.path[:0] =
      [...]`): mutates the list IN PLACE, so this is checked through the
      permissive `_is_sys_path_expr` (any alias/import/getattr form that
      reaches the same underlying list object is a real vector here).
    - **Direct rebinding** (`sys.path = [tools_dir] + sys.path`): replaces
      the module attribute itself, so this is checked through the STRICT
      `_is_sys_path_attribute` (a name that merely copied a reference does
      NOT rebind the real `sys.path` on reassignment -- see that
      function's docstring for why conflating the two would be a false
      positive)."""
    offenders: list[tuple[int, str]] = []
    for node in nodes:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Subscript) and isinstance(
                target.slice, ast.Slice
            ):
                if not _is_sys_path_expr(
                    target.value, alias, sys_module_names, sys_path_names
                ):
                    continue
                sl = target.slice
                if sl.step is not None:
                    continue
                lower_is_zero = sl.lower is None or _resolves_to_zero(sl.lower, alias)
                upper_is_zero = sl.upper is not None and _resolves_to_zero(
                    sl.upper, alias
                )
                if not (lower_is_zero and upper_is_zero):
                    continue
                _, fragments = _resolve_text(node.value, alias)
                if any(_is_tools_ish_text(f) for f in fragments):
                    offenders.append((
                        node.lineno,
                        f"slice-assignment to sys.path[...] contains tools-ish "
                        f"fragment(s) {fragments!r}",
                    ))
            elif _is_sys_path_attribute(target, sys_module_names):
                _, fragments = _resolve_text(node.value, alias)
                if any(_is_tools_ish_text(f) for f in fragments):
                    offenders.append((
                        node.lineno,
                        f"sys.path rebinding contains tools-ish fragment(s) "
                        f"{fragments!r}",
                    ))
    return offenders


def _build_func_ancestry(tree: ast.AST) -> "dict[ast.AST, list[ast.AST]]":
    """Map every `FunctionDef`/`AsyncFunctionDef` node to the list of its
    LEXICALLY ENCLOSING function nodes, outermost first, NOT including
    itself. Built with an explicit parent-stack walk (not `ast.walk`,
    which visits every node in some order but discards nesting structure)
    so a nested function's chain reflects real lexical enclosure -- the
    scoping round 1's flat table and round 2's per-scope-only table both
    got wrong in opposite ways (round 1: everything bled together
    module-wide; round 2: nothing above a function's own locals was
    visible at all, missing a closure over an enclosing local, #2089
    round 3)."""
    ancestry: "dict[ast.AST, list[ast.AST]]" = {}

    def walk(node: ast.AST, stack: "list[ast.AST]") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ancestry[child] = list(stack)
                walk(child, stack + [child])
            else:
                walk(child, stack)

    walk(tree, [])
    return ancestry


def _find_insert0_violations(text: str, filename: str = "<file>") -> "list[tuple[int, str]]":
    """AST-scan *text* for violations of the #2111 bound documented in this
    file's module docstring: `sys.path.insert(0, <tools-ish>)` (directly,
    via alias/indirection, or via a module-alias / `from sys import path` /
    `getattr(sys, "path")` receiver -- #2111 (a)), `sys.path[0:0] = [...]`
    / `sys.path[:0] = [...]` slice-assignment, and direct rebinding
    `sys.path = [...]` (#2111 (b)) -- with alias resolution built by
    proper LEXICAL CHAINING (#2089 round 3): a use site inside function
    `F` resolves through `F`'s own locals, then each enclosing function's
    own locals (innermost enclosing checked first, i.e. shadowing
    outward), then the module scope -- ordinary Python LEGB scoping (minus
    globals/builtins, not needed here). SIBLING functions never appear in
    each other's ancestry chain, so the round-2 cross-function isolation
    fix is preserved: only genuine lexical enclosure contributes locals to
    a nested function's table, never an unrelated function defined
    elsewhere in the file. The same lexical chaining is applied to the
    #2111 sys-module-alias / sys-path-import binding sets (unioned rather
    than last-wins, since imports are additive, not reassignment).
    Returns a list of ``(lineno, detail)`` pairs."""
    tree = ast.parse(text, filename=filename)

    module_nodes = list(_iter_scope_nodes(tree))
    module_assignments = _collect_assignments(module_nodes)
    module_alias = _AliasTable(module_assignments)
    # #2111: module aliases (`import sys as s`) and direct-imported names
    # (`from sys import path`) -- "sys" itself is always a valid module
    # name regardless of whether an explicit `import sys` line is present
    # in this scope (matches the pre-#2111 behavior, which hardcoded the
    # literal name "sys" unconditionally).
    module_sys_modules_raw, module_sys_paths_raw = _collect_import_bindings(
        module_nodes
    )
    module_sys_modules = frozenset(module_sys_modules_raw | {"sys"})
    module_sys_paths = frozenset(module_sys_paths_raw)

    # Module-level call/assignment sites (not inside any function) resolve
    # against the module scope only.
    offenders = _scan_calls(module_nodes, module_alias, module_sys_modules, module_sys_paths)
    offenders.extend(
        _scan_assignments(module_nodes, module_alias, module_sys_modules, module_sys_paths)
    )

    ancestry = _build_func_ancestry(tree)

    # Pass 1: collect every function's OWN locals in isolation (no scope
    # chaining yet -- this is scope-bounded via `_iter_scope_nodes`, which
    # does not recurse into a nested function's body). Also collect each
    # function's own sys-module/sys-path import bindings the same way
    # (#2111).
    own_assignments: "dict[ast.AST, dict[str, ast.expr]]" = {}
    own_sys_bindings: "dict[ast.AST, tuple[set[str], set[str]]]" = {}
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        func_scope_nodes = list(_iter_scope_nodes(func))
        own_assignments[func] = _collect_assignments(func_scope_nodes)
        own_sys_bindings[func] = _collect_import_bindings(func_scope_nodes)

    # Pass 2: chain each function's effective table = module scope, then
    # every enclosing function's own locals (outermost -> innermost), then
    # the function's own locals -- each later merge shadowing the earlier
    # ones, matching normal lexical shadowing. The sys-module/sys-path
    # binding sets are additive (imports don't shadow the way a `name =
    # expr` reassignment does), so they're unioned rather than merged with
    # last-wins semantics.
    for func, own in own_assignments.items():
        func_assignments: "dict[str, ast.expr]" = dict(module_assignments)
        func_sys_modules: "set[str]" = set(module_sys_modules)
        func_sys_paths: "set[str]" = set(module_sys_paths)
        for ancestor in ancestry.get(func, []):
            func_assignments.update(own_assignments.get(ancestor, {}))
            ancestor_mod, ancestor_path = own_sys_bindings.get(ancestor, (set(), set()))
            func_sys_modules |= ancestor_mod
            func_sys_paths |= ancestor_path
        func_assignments.update(own)
        own_mod, own_path = own_sys_bindings.get(func, (set(), set()))
        func_sys_modules |= own_mod
        func_sys_paths |= own_path

        func_alias = _AliasTable(func_assignments)
        func_sys_modules_f = frozenset(func_sys_modules)
        func_sys_paths_f = frozenset(func_sys_paths)
        func_nodes = list(_iter_scope_nodes(func))
        offenders.extend(
            _scan_calls(func_nodes, func_alias, func_sys_modules_f, func_sys_paths_f)
        )
        offenders.extend(
            _scan_assignments(
                func_nodes, func_alias, func_sys_modules_f, func_sys_paths_f
            )
        )

    offenders.sort(key=lambda pair: pair[0])
    return offenders


@pytest.mark.parametrize("test_file", _all_test_files(), ids=lambda p: p.name)
def test_no_tools_dir_insert_at_position_zero(test_file: Path) -> None:
    """Regression guard for #2037/#2063: no test file may put a tools-ish
    directory at sys.path position 0. `append(...)` (or a conditional
    insert further back) is fine -- only rank-0 insertion can outrank
    src/ packages for the rest of the pytest process."""
    text = test_file.read_text()
    offenders = _find_insert0_violations(text, filename=str(test_file))
    assert not offenders, (
        f"{test_file.relative_to(REPO_ROOT)} does sys.path.insert(0, ...) "
        f"with a tools-ish directory -- this outranks src/ packages "
        f"(e.g. src/forum/) for every test collected afterward in the "
        f"same pytest process (#2037/#2063). Use sys.path.append(...) "
        f"instead, or import the target module directly by file path via "
        f"importlib.util.spec_from_file_location. Offending line(s): "
        f"{offenders}"
    )


# ---------------------------------------------------------------------------
# Part 1b: adversarial-bypass regression tests (#2089). Each of the four
# demonstrated bypasses, plus the side-defect (an assignment split across
# lines), is injected as fixture *text* fed straight to
# `_find_insert0_violations` -- mirroring how the file-level test above
# runs the same function, but pointed at a synthetic file (written into
# `tmp_path`, never a real suite member) instead of a real one, so these
# fixtures are never collected as violations of the guard on themselves.
# ---------------------------------------------------------------------------

def _scan_fixture(tmp_path: Path, source: str) -> "list[tuple[int, str]]":
    fixture = tmp_path / "fixture_test_case.py"
    fixture.write_text(source)
    return _find_insert0_violations(fixture.read_text(), filename=str(fixture))


class TestAdversarialBypasses:
    """Each case is exactly the shape from issue #2089's bypass list,
    verified (pre-fix) to pass the old regex scan silently."""

    def test_multiline_call_is_caught(self, tmp_path: Path) -> None:
        """Bypass #1: the call itself is split across lines."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "TOOLS_DIR = Path('/repo') / 'tools'\n"
            "sys.path.insert(\n"
            "    0,\n"
            "    str(TOOLS_DIR),\n"
            ")\n",
        )
        assert offenders, "multi-line sys.path.insert(0, ...) call was not caught"

    def test_sys_path_aliasing_is_caught(self, tmp_path: Path) -> None:
        """Bypass #2: `p = sys.path; p.insert(0, ...)`."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "TOOLS_DIR = Path('/repo') / 'tools'\n"
            "p = sys.path\n"
            "p.insert(0, str(TOOLS_DIR))\n",
        )
        assert offenders, "sys.path aliasing bypass was not caught"

    def test_two_hop_indirection_is_caught(self, tmp_path: Path) -> None:
        """Bypass #3: `_SEG = 'tools'; _D = ROOT / _SEG; ...insert(0, str(_D))`."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "ROOT = Path(__file__).resolve().parent\n"
            "_SEG = 'tools'\n"
            "_D = ROOT / _SEG\n"
            "sys.path.insert(0, str(_D))\n",
        )
        assert offenders, "two-hop name indirection bypass was not caught"

    def test_string_concat_literal_is_caught(self, tmp_path: Path) -> None:
        """Bypass #4: `str(ROOT) + '/src/engram/tools'`."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "ROOT = Path(__file__).resolve().parent\n"
            "sys.path.insert(0, str(ROOT) + '/src/engram/tools')\n",
        )
        assert offenders, "string-concat literal bypass was not caught"

    def test_split_assignment_is_caught(self, tmp_path: Path) -> None:
        """Side defect: the *assignment* feeding the call (not the call
        itself) spans multiple lines -- `_resolve_arg_text`'s documented
        miss, subsumed by AST parsing since ast.parse doesn't care about
        line breaks within an expression."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "TOOLS_DIR = (\n"
            "    Path(__file__).resolve().parent\n"
            "    / 'tools'\n"
            ")\n"
            "sys.path.insert(0, str(TOOLS_DIR))\n",
        )
        assert offenders, "split multi-line assignment bypass was not caught"

    def test_direct_literal_still_caught(self, tmp_path: Path) -> None:
        """Sanity: the original, un-bypassed shape is still caught."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "sys.path.insert(0, '/repo/tools')\n",
        )
        assert offenders, "direct literal insert(0, '.../tools') regressed"

    def test_append_is_not_flagged(self, tmp_path: Path) -> None:
        """`append(...)` (any position other than rank 0) must stay legal."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "sys.path.append('/repo/tools')\n",
        )
        assert not offenders, "append(...) must not be flagged -- only insert(0, ...) is"

    def test_unrelated_dunder_file_bootstrap_not_flagged(self, tmp_path: Path) -> None:
        """The ordinary same-repo bootstrap idiom used throughout this
        suite (`os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`)
        carries no literal fragment at all and must not be flagged -- this
        is the case that rules out an unconditional
        "unresolvable == violation" policy (see `_check_insert_call`)."""
        offenders = _scan_fixture(
            tmp_path,
            "import os\n"
            "import sys\n"
            "sys.path.insert(0, os.path.dirname(os.path.dirname("
            "os.path.abspath(__file__))))\n",
        )
        assert not offenders, (
            "an opaque, non-tools-ish __file__-derived insert(0, ...) must "
            "not be flagged"
        )

    def test_unrelated_alias_not_flagged(self, tmp_path: Path) -> None:
        """Aliasing `sys.path` to insert something that is NOT tools-ish
        must not be flagged -- the guard is about the target directory,
        not the aliasing shape itself."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "SRC_DIR = Path('/repo') / 'src'\n"
            "p = sys.path\n"
            "p.insert(0, str(SRC_DIR))\n",
        )
        assert not offenders, "a non-tools-ish alias insert(0, ...) must not be flagged"

    # -- #2089 round 2: adversarial reviewer's BLOCKER (flat module-wide
    # alias table bleeding across functions) + the three suggestions. --

    def test_masked_violation_across_functions_is_caught(self, tmp_path: Path) -> None:
        """BLOCKER regression (order-dependent masking): a REAL violation
        inside `foo()` must still be caught even though an unrelated LATER
        function `bar()` reassigns the same name `D` to a non-tools value.
        Under the old flat, module-wide alias table (last-assignment-wins
        across the whole module), `bar`'s later `D` overwrote `foo`'s
        entry and masked the violation entirely."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "def foo():\n"
            "    D = Path('/x') / 'tools'\n"
            "    sys.path.insert(0, str(D))\n"
            "\n"
            "def bar():\n"
            "    D = 'not-tools-related'\n"
            "    return D\n",
        )
        assert offenders, (
            "foo()'s real sys.path.insert(0, .../tools) violation was masked "
            "by bar()'s unrelated later reassignment of the same name `D` -- "
            "alias resolution must be scoped per function, not flat module-wide"
        )

    def test_mirror_false_positive_across_functions_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """BLOCKER mirror case: a LEGITIMATE, non-tools-ish
        `sys.path.insert(0, ...)` inside `foo()` must NOT be flagged just
        because an unrelated LATER function `bar()` happens to reassign the
        same name `D` to a tools-ish value. Under the old flat table,
        bar()'s later `D` bled backward into foo()'s resolution and
        produced a false positive."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "def foo():\n"
            "    D = Path('/x') / 'other'\n"
            "    sys.path.insert(0, str(D))\n"
            "\n"
            "def bar():\n"
            "    D = Path('/x') / 'tools'\n"
            "    return D\n",
        )
        assert not offenders, (
            "foo()'s legitimate insert(0, .../other) was falsely flagged due "
            "to bar()'s unrelated same-named tools-ish `D` -- alias "
            "resolution must be scoped per function, not flat module-wide"
        )

    def test_first_arg_alias_zero_is_caught(self, tmp_path: Path) -> None:
        """Suggestion (a): the first argument (position 0) may itself be an
        alias resolving to the literal `0` -- symmetric with how the second
        argument is resolved through the alias table."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "Z = 0\n"
            "TOOLS_DIR = Path('/repo') / 'tools'\n"
            "sys.path.insert(Z, str(TOOLS_DIR))\n",
        )
        assert offenders, "insert(Z, ...) with Z aliasing 0 was not caught"

    def test_list_subscript_fragment_is_caught(self, tmp_path: Path) -> None:
        """Suggestion (b): a tools-ish literal reachable only through a
        list literal + subscript (`SEGS = ['tools']; ... / SEGS[0]`) must
        still surface as a fragment even though the expression as a whole
        doesn't fully constant-fold."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "SEGS = ['tools']\n"
            "sys.path.insert(0, str(Path('/repo') / SEGS[0]))\n",
        )
        assert offenders, "tools-ish fragment inside a list/subscript was not caught"

    def test_percent_formatting_is_caught(self, tmp_path: Path) -> None:
        """Suggestion (c): %-formatting with a tools-ish literal in the
        format string must be caught."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "root = '/repo'\n"
            "sys.path.insert(0, '%s/tools' % root)\n",
        )
        assert offenders, "'%s/tools' % root was not caught"

    def test_str_format_is_caught(self, tmp_path: Path) -> None:
        """Suggestion (c): `.format(...)` with a tools-ish literal in the
        format string must be caught, folded the same way `str(x)` is."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "root = '/repo'\n"
            "sys.path.insert(0, '{}/tools'.format(root))\n",
        )
        assert offenders, "'{}/tools'.format(root) was not caught"

    # -- #2089 round 3: lexical chaining through nested functions. --

    def test_closure_over_enclosing_local_is_caught(self, tmp_path: Path) -> None:
        """(a) Round-3 regression: an ordinary closure -- a nested function
        inserting `str(D)` where `D` is a local of its ENCLOSING function,
        not its own -- was silently missed by round 2's per-scope-only
        table (which gave a nested function only its own locals + the
        module scope, never its enclosing function's locals). Round 1's
        flat table caught this; proper lexical chaining must too."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "def outer():\n"
            "    D = Path('/x') / 'tools'\n"
            "    def inner():\n"
            "        sys.path.insert(0, str(D))\n"
            "    inner()\n",
        )
        assert offenders, (
            "inner()'s closure over outer()'s tools-ish local D was not "
            "caught -- lexical chaining must resolve names through each "
            "enclosing function's locals, not just the nested function's "
            "own locals + module scope"
        )

    def test_sibling_isolation_preserved_through_chain(self, tmp_path: Path) -> None:
        """(b) Inverse of (a), guarding against a chaining fix that
        over-widens back into round 2's flat-table bug: `outer()`'s local
        `D` is NON-tools, and `inner()` (nested inside `outer()`) inserts
        `str(D)` -- so far a true negative. A SIBLING function `sibling()`
        (not enclosing `inner()` at all) happens to define a tools-ish `D`
        of its own. `inner()` must resolve against its OWN lexical chain
        (`outer()` -> module), never against the unrelated sibling's
        locals -- sibling isolation must survive the move to chaining."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "def outer():\n"
            "    D = Path('/x') / 'other'\n"
            "    def inner():\n"
            "        sys.path.insert(0, str(D))\n"
            "    inner()\n"
            "\n"
            "def sibling():\n"
            "    D = Path('/x') / 'tools'\n"
            "    return D\n",
        )
        assert not offenders, (
            "inner()'s legitimate insert(0, .../other) was falsely flagged "
            "due to an unrelated sibling function's same-named tools-ish "
            "D -- lexical chaining must follow inner()'s actual enclosing "
            "chain (outer -> module), never a sibling's locals"
        )

    def test_inner_local_shadows_enclosing(self, tmp_path: Path) -> None:
        """(c) Shadowing: `outer()`'s local `D` IS tools-ish, but `inner()`
        reassigns its OWN local `D` to a non-tools value before the insert
        call. Per normal lexical shadowing, `inner()`'s own local must win
        over the enclosing one -- the call must NOT be flagged."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "def outer():\n"
            "    D = Path('/x') / 'tools'\n"
            "    def inner():\n"
            "        D = Path('/x') / 'other'\n"
            "        sys.path.insert(0, str(D))\n"
            "    inner()\n",
        )
        assert not offenders, (
            "inner()'s own local D (non-tools) must shadow outer()'s "
            "enclosing tools-ish D -- it was incorrectly resolved against "
            "the enclosing scope instead of inner()'s own reassignment"
        )


class TestIssue2111UnflaggedConstructions:
    """The six constructions issue #2111 demonstrated as unflagged against
    the merged #2089/#2102 guard. Each `test_*_is_caught` is a can-it-fail
    control: if the corresponding detection added in `_is_sys_path_expr` /
    `_is_sys_path_attribute` / `_scan_assignments` is removed, that specific
    fixture (and only that one) stops being flagged -- it is not a
    tautology, it depends on the new code. Two negative controls close the
    obvious false-positive risk the two NEW mechanisms (slice-assignment,
    rebinding) introduce: reassigning a name that only ever *copied* a
    reference to `sys.path` must not be treated as touching the real
    attribute.

    #2111 (a) -- three are `_is_sys_path_expr` receiver-resolution gaps:
    """

    def test_module_alias_import_is_caught(self, tmp_path: Path) -> None:
        """`import sys as s; s.path.insert(0, ...)` -- a module alias, not
        a value alias of `sys.path` itself (which #2089 already caught)."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys as s\n"
            "from pathlib import Path\n"
            "TOOLS_DIR = Path('/repo') / 'tools'\n"
            "s.path.insert(0, str(TOOLS_DIR))\n",
        )
        assert offenders, "import sys as s; s.path.insert(0, ...) was not caught"

    def test_from_sys_import_path_is_caught(self, tmp_path: Path) -> None:
        """`from sys import path; path.insert(0, ...)` -- the attribute
        imported directly, bypassing any `sys.` prefix entirely."""
        offenders = _scan_fixture(
            tmp_path,
            "from sys import path\n"
            "from pathlib import Path\n"
            "TOOLS_DIR = Path('/repo') / 'tools'\n"
            "path.insert(0, str(TOOLS_DIR))\n",
        )
        assert offenders, "from sys import path; path.insert(0, ...) was not caught"

    def test_getattr_dynamic_attribute_is_caught(self, tmp_path: Path) -> None:
        """`getattr(sys, "path").insert(0, ...)` -- a dynamic attribute
        fetch instead of a literal `.path` access."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "TOOLS_DIR = Path('/repo') / 'tools'\n"
            "getattr(sys, 'path').insert(0, str(TOOLS_DIR))\n",
        )
        assert offenders, "getattr(sys, 'path').insert(0, ...) was not caught"

    # -- #2111 (b): the two unmodelled-mechanism constructions, no
    # `.insert(...)` call for the call-scanner to see at all. --

    def test_slice_assign_0_0_is_caught(self, tmp_path: Path) -> None:
        """`sys.path[0:0] = [tools_dir]` -- explicit `0:0` slice, the form
        the issue singles out as the one it "would not wave off"."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "TOOLS_DIR = Path('/repo') / 'tools'\n"
            "sys.path[0:0] = [str(TOOLS_DIR)]\n",
        )
        assert offenders, "sys.path[0:0] = [...] was not caught"

    def test_slice_assign_colon_0_is_caught(self, tmp_path: Path) -> None:
        """`sys.path[:0] = [tools_dir]` -- the omitted-lower-bound sibling
        of the `0:0` form; same front-insertion effect."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "TOOLS_DIR = Path('/repo') / 'tools'\n"
            "sys.path[:0] = [str(TOOLS_DIR)]\n",
        )
        assert offenders, "sys.path[:0] = [...] was not caught"

    def test_direct_rebinding_is_caught(self, tmp_path: Path) -> None:
        """`sys.path = [tools_dir] + sys.path` -- rebinding the module
        attribute itself, no subscript or call in sight."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "TOOLS_DIR = Path('/repo') / 'tools'\n"
            "sys.path = [str(TOOLS_DIR)] + sys.path\n",
        )
        assert offenders, "sys.path = [tools_dir] + sys.path was not caught"

    # -- False-positive controls for the two NEW (non-call) mechanisms. --

    def test_slice_assign_non_toolsish_not_flagged(self, tmp_path: Path) -> None:
        """A legitimate `sys.path[0:0] = [...]` inserting something that is
        NOT tools-ish must not be flagged -- mirrors
        `test_unrelated_alias_not_flagged` for the call-based scanner."""
        offenders = _scan_fixture(
            tmp_path,
            "import sys\n"
            "from pathlib import Path\n"
            "SRC_DIR = Path('/repo') / 'src'\n"
            "sys.path[0:0] = [str(SRC_DIR)]\n",
        )
        assert not offenders, "a non-tools-ish sys.path[0:0] = [...] must not be flagged"

    def test_reassigning_copied_reference_not_flagged(self, tmp_path: Path) -> None:
        """Reassigning a name that only ever COPIED a reference to
        `sys.path` (`from sys import path; path = [...]`) rebinds the
        LOCAL name only -- it never touches the real `sys.path` attribute,
        so it is not a violation. This is the false positive
        `_is_sys_path_attribute`'s strict (non-alias-chasing) design
        exists to avoid, per that function's own docstring."""
        offenders = _scan_fixture(
            tmp_path,
            "from sys import path\n"
            "from pathlib import Path\n"
            "TOOLS_DIR = Path('/repo') / 'tools'\n"
            "path = [str(TOOLS_DIR)] + path\n",
        )
        assert not offenders, (
            "reassigning a name that only copied a reference to sys.path "
            "was incorrectly treated as rebinding sys.path itself"
        )


# ---------------------------------------------------------------------------
# Part 2: forward-looking collision detector. Enumerates every top-level
# tools/*.py script name alongside every top-level importable package
# under src/ (a directory with __init__.py directly under src/), and for
# any name present in BOTH -- `forum` today, whatever collides next
# tomorrow -- asserts that importing the bare name in the worst-case
# ordering (tools-ish dir ranked ahead of src/) still resolves to the
# real package once part 1's discipline is followed for every remaining
# insert(0) site, and independently reports which physical files are in
# collision so the failure message names both paths per #2063's ask.
# ---------------------------------------------------------------------------

def _tools_script_names() -> set[str]:
    top_level_tools = REPO_ROOT / "tools"
    if not top_level_tools.is_dir():
        return set()
    return {
        p.stem
        for p in top_level_tools.glob("*.py")
        if p.stem != "__init__"
    }


def _src_top_level_package_names() -> set[str]:
    if not SRC_DIR.is_dir():
        return set()
    return {
        p.name
        for p in SRC_DIR.iterdir()
        if p.is_dir() and (p / "__init__.py").is_file()
    }


def _collision_names() -> list[str]:
    return sorted(_tools_script_names() & _src_top_level_package_names())


@pytest.mark.parametrize("name", _collision_names() or pytest.param("forum", marks=pytest.mark.xfail(
    reason="no live tools/*.py vs src/<pkg> collision detected -- see test body",
    strict=False,
)))
def test_colliding_name_resolves_to_src_package(name: str) -> None:
    """For every name that exists as BOTH tools/<name>.py and a top-level
    src/<name>/ package, assert the "last mutation wins" pattern this
    repo's real fix relies on (tests/test_forum_db_hostname.py, #2064:
    insert src/ unconditionally, always at position 0, as the LAST
    sys.path mutation before the import) correctly resolves `import
    <name>` to the src/ package even in the worst-case ordering -- some
    earlier-collected test file has already put tools/ at position 0.
    Names both physical files in the failure message so a future
    collision (the issue's "file #8") fails loudly and legibly rather
    than as a confusing downstream ImportError."""
    import importlib
    import sys as _sys

    tools_file = REPO_ROOT / "tools" / f"{name}.py"
    src_pkg = SRC_DIR / name
    assert tools_file.is_file(), f"expected collision file missing: {tools_file}"
    assert (src_pkg / "__init__.py").is_file(), f"expected collision package missing: {src_pkg}"

    saved_path = list(_sys.path)
    # Save (don't just delete) any already-cached module(s) under this
    # name -- other test files collected earlier or later may hold direct
    # references to objects living inside them (e.g.
    # src/forum/tests/test_coordination_names.py asserts `forum.db.X is
    # <the X it imported at its own module top>`). Deleting-and-not-
    # restoring would force everyone downstream to re-import fresh,
    # silently breaking those `is` identity checks -- a self-inflicted
    # repeat of the exact defect class this file exists to guard against.
    saved_modules = {
        mod_name: _sys.modules[mod_name]
        for mod_name in list(_sys.modules)
        if mod_name == name or mod_name.startswith(f"{name}.")
    }
    try:
        # Worst case: some earlier-collected file already polluted tools/
        # to the front.
        _sys.path.insert(0, str(REPO_ROOT / "tools"))
        # The production invariant (test_forum_db_hostname.py, #2064):
        # whoever actually needs the src/ package inserts it
        # unconditionally, always at position 0, as the LAST mutation
        # before the import -- so it always wins regardless of what an
        # earlier file already put at the front.
        _sys.path.insert(0, str(SRC_DIR))

        for mod_name in saved_modules:
            del _sys.modules[mod_name]

        mod = importlib.import_module(name)
        resolved_file = Path(getattr(mod, "__file__", "") or "")
    finally:
        _sys.path[:] = saved_path
        for mod_name in list(_sys.modules):
            if mod_name == name or mod_name.startswith(f"{name}."):
                del _sys.modules[mod_name]
        _sys.modules.update(saved_modules)

    assert str(src_pkg) in str(resolved_file.parent), (
        f"import {name!r} resolved to {resolved_file} instead of the real "
        f"package at {src_pkg} -- tools/{name}.py at {tools_file} is "
        f"shadowing it even under the last-mutation-wins pattern. This is "
        f"the #2037/#2063 defect: re-check part 1's static guard above and "
        f"any file it may have missed, and confirm whichever file needs "
        f"{name!r} still inserts src/ unconditionally, at position 0, as "
        f"the LAST sys.path mutation before its own import."
    )
