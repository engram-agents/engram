#!/usr/bin/env python3
"""telemetry_manifest_gate.py — §4a manifest gate for the telemetry redesign
(issue #2574, workstream WS2).

Design: docs/2574-telemetry-design.md — read §1 (PARTICIPATION DEFINITION)
and §4a (the registry) before touching this file. This module is WS2's
deliverable; it does NOT wire any real `site=` kwargs into emit call sites
(that is WS3's job) and does NOT touch the telemetry store (WS1) or consumer
projections (WS4).

WHAT THIS GATE COMPUTES
========================
Two static verdicts, over site-id literal sets, per §1's DECLARED block:

    L := every `site="..."` literal argument at an `emit`/`emit_if_initialized`
         call anywhere in the source tree (src/engram/**.py).
    M := every site-id literal recorded in the manifest's per-mechanism
         `telemetry` records (packaging/tiers.json).

    NOT WIRED  := M - L   -- manifest declares a site the source never emits
                             from.
    UNDECLARED := L - M   -- an emit call in the DECLARABLE POPULATION with no
                             manifest record. The declarable population is
                             every call to `emit`/`emit_if_initialized` in the
                             source tree EXCEPT engram_log_emitter.py's own
                             internal implementation of `emit_if_initialized`
                             (the transport, not a consumer of it — see
                             TRANSPORT_EXCLUSION below).

Both sets MUST be empty for `--check` to pass.

TODAY'S STATE (WS2, before WS3 adds any `site=` kwargs): L = M = ∅ across the
whole real tree, so both verdicts are trivially empty. That is EXPECTED, not a
loophole — `--self-test` proves the extraction logic is live (it plants a
positive fixture and asserts the gate actually catches it), rather than green
by vacuous truth. See the module docstring's own warning in
docs/2574-telemetry-design.md's Validation section: "a scanner that reports
clean is indistinguishable from one whose patterns never matched anything."

BACKWARD CALL-GRAPH WALK (§1, "The fix is to DERIVE it")
==========================================================
For each real emit call site (keyed today by file:lineno, since no `site=`
kwarg exists yet to key by), a backward walk from the call's nearest
enclosing top-level function to the set of DISPATCH TARGETS that can reach
it — a `@mcp.tool`-decorated function, or a `main()` invoked under
`if __name__ == "__main__":`.

Caller resolution (the reverse call graph):
  - bare name call `foo(...)`      -> the SAME-FILE top-level def named `foo`
    (Python's own module-namespace lookup: an unqualified call resolves to
    whatever `foo` is bound to in the CALLING module's own namespace, which
    shadows any import of the same name — this is what makes
    `server.py`'s `_add_observation_impl(**params)` resolve to server.py's
    own compat-forwarder def, NOT engram_observation.py's real impl).
  - attribute call `alias.foo(...)` -> resolved via the calling file's own
    `import X as alias` (or `import X`) statement, to `X.py`'s top-level def
    named `foo`.

Keying callers on (file, name) pairs — never bare name alone — is what keeps
a same-named helper defined separately in multiple files (e.g.
`_emit_hook_fire`, three hook files, §1's WRAPPER paragraph) from collapsing
into one cross-file key, which is the exact defect §12.4 records ("the wrong
six").

A thin wrapper is not itself a dispatch target; walking past it to ITS own
callers is just the ordinary backward-walk step — no special-casing needed,
because caller resolution already dedupes by (file, enclosing-function), so
three call sites inside one wrapper called from three branches of the SAME
outer function collapse to that one outer function, not three.

DYNAMIC-DISPATCH STATIC SCAN (§1 "STATIC" / "RUNTIME")
=========================================================
Scans ALL of the source tree (not just the emitting modules — a backward walk
is blind to a dynamic CALLER, which by definition is not the emitting module)
for three dynamically-dispatched call shapes:
  (a) an ast.Call whose own `func` is itself a call to `getattr()`
  (b) a callee that is any subscript expression (dispatch-table lookup)
  (c) a two-step getattr() result bound to a local, called on a later line
Every hit must be resolved before `--check` can pass: either shown by the
backward walk not to reach any declared site, or recorded as a declared
dynamic-dispatch exclusion in the manifest's `telemetry.dynamic_dispatch_exclusions`
list, with a reason — default-deny with a declared exclusion set, the same
discipline already used for `telemetry: []`.

REACHED-LITERAL JOIN-KEY CHECK (issue #3136, WS7)
===================================================
A second, STATIC join-key check, added by WS7 (the §1 VERDICTS runtime
report, `engram_telemetry_verdicts.py`): every `reached` literal hand-written
in source (each hook's own `_MAIN_MANIFEST_ID`, server.py's inline
`"server.py:engram_query"`, etc. — every
`write_telemetry_record(manifest_id=..., event_type="engram.telemetry.reached")`
call) must equal, as a set, the union over every DECLARED site of that
site's own derived dispatch-target set (formatted via `dispatch_target_id`)
— a set difference in both directions, exactly like NOT_WIRED/UNDECLARED
above. See `find_reached_literals`, `derived_target_ids_over_declared_sites`,
and `reached_literal_mismatches`. A typo on either side of this join makes
the mistyped target's `reached` count silently 0 forever, which the VERDICTS
report reads as `NOT REACHED` — "NOT a fault" — so this check is wired into
`check_ok()`, not left advisory.

DISPATCH MAP (issue #3136, WS7 REVISION 2026-09-27)
=====================================================
`engram_telemetry_verdicts.py` originally imported this module's own
`build_call_graph`/`derive_dispatch_targets`/`enclosing_key_for_site`/
`dispatch_target_id` to walk the backward call-graph AT RUNTIME. That
cannot work on an installed plugin: no `tiers.json` ships, this module
resolves paths from the `src/` layout (REPO_ROOT is computed 4 levels above
this file, which overshoots the plugin root once the build flattens
`hooks/claude/*` to `hooks/*`), and the `reached` literals in a deployed
hook still carry the SOURCE path (`hooks/claude/....py:main`) that a walk
over the DEPLOYED (flattened) tree would never derive. See issue #3136's
own "REVISION 2026-09-27" block.

The fix: walk once, HERE, and let the result ship as committed DATA instead
of a runtime import. `build_dispatch_map()` performs that walk and returns
the JSON-serializable capture; `emit_dispatch_map()` (and this module's own
`--emit-map PATH` CLI flag) writes it to `src/engram/telemetry_targets.json`
— a plain, convenience-tier, git-committed file (NOT build-generated,
NOT baked at build time) that `engram_telemetry_verdicts.py` reads directly
off its own directory at runtime, with zero import of this (dev-tier)
module. `tests/test_tiers_manifest.py`'s own drift-gate test re-derives the
map fresh on every `pr-checks` run and fails, naming the differing sites, if
the committed file has gone stale — the same default-deny discipline as
§4a's NOT_WIRED/UNDECLARED check.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import tempfile
from collections import deque
from dataclasses import dataclass

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
MANIFEST_PATH = os.path.join(REPO_ROOT, "src", "build", "packaging", "tiers.json")

EMIT_FUNC_NAMES = {"emit", "emit_if_initialized"}

# engram_log_emitter.py's own implementation of emit_if_initialized
# (`return emitter.emit(...)`) — the transport, not a consumer of it. Per
# §1's DECLARED block, this call is excluded from the declarable population.
# In practice it never carries a `site=` kwarg (nothing calls it with one),
# so it is excluded from L "for free" — this constant exists so a future
# reader (or a --report consumer) can name the exclusion explicitly rather
# than rediscover it.
TRANSPORT_EXCLUSION_FILE = "engram_log_emitter.py"
# alpha #2574 WS3/WS5 (line-number drift, not a design change): WS3's `site`
# param addition to Emitter.emit()/_emit_inner() shifted this file's own
# source lines, moving `emit_if_initialized`'s `return emitter.emit(...)`
# from :260 to :281; WS5's own `site` param addition to
# `emit_if_initialized` itself (plus its accompanying docstring note, so the
# AST scan can find a `site="..."` literal directly at THAT call, not only
# at Emitter.emit()) shifted it again, from :281 to :287. Re-verify with
# `--report` after any future engram_log_emitter.py edit -- this is exactly
# the stale-line-number failure mode docs/2574-telemetry-design.md §1 warns
# against, one level down in this gate's own exclusion constant rather than
# in a manifest_id.
TRANSPORT_EXCLUSION_LINENO = 287

FuncKey = tuple  # (file: str, name: str) — kept untyped for py3.8 dataclass friendliness


# ---------------------------------------------------------------------------
# manifest / filesystem helpers
# ---------------------------------------------------------------------------


def load_manifest(path: str = MANIFEST_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def source_root(manifest: dict | None = None, repo_root: str = REPO_ROOT) -> str:
    manifest = manifest if manifest is not None else load_manifest()
    return os.path.join(repo_root, manifest.get("source_root", "."))


def iter_py_files(root: str):
    """Yield every .py file under root, deterministically ordered."""
    for dirpath, dirnames, filenames in sorted(os.walk(root)):
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def relpath(full: str, root: str) -> str:
    return os.path.relpath(full, root).replace(os.sep, "/")


def _parse(full: str):
    try:
        with open(full, encoding="utf-8") as f:
            src = f.read()
        return ast.parse(src, filename=full)
    except (SyntaxError, OSError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Emit-call-site extraction (set L, and the census the backward walk starts
# from)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmitCallSite:
    file: str  # relative to src_root, e.g. "engram_observation.py"
    lineno: int
    site_literal: str | None  # the site="..." kwarg literal, if present
    enclosing: str | None  # nearest enclosing top-level function name, or
    # None for module-scope calls (no top-level def wraps it)

    @property
    def key(self):
        return (self.file, self.lineno)

    def is_transport_exclusion(self) -> bool:
        return self.file == TRANSPORT_EXCLUSION_FILE and self.lineno == TRANSPORT_EXCLUSION_LINENO


def _call_func_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _extract_site_kwarg(call: ast.Call) -> str | None:
    for kw in call.keywords:
        if kw.arg == "site" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _scan_calls_for_emits(node: ast.AST, file_rel: str, enclosing: str | None, sites: list):
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _call_func_name(child.func)
            if name in EMIT_FUNC_NAMES:
                sites.append(
                    EmitCallSite(
                        file=file_rel,
                        lineno=child.lineno,
                        site_literal=_extract_site_kwarg(child),
                        enclosing=enclosing,
                    )
                )


def find_emit_call_sites(src_root: str) -> list:
    """Return every emit()/emit_if_initialized() call site under src_root.

    Enclosing-function attribution is at TOP-LEVEL def granularity: a call
    inside a nested closure is attributed to the nearest enclosing TOP-LEVEL
    function (this codebase's dispatch chains are all top-level defs — see
    the module docstring's caller-resolution note).
    """
    sites: list = []
    for full in iter_py_files(src_root):
        tree = _parse(full)
        if tree is None:
            continue
        rel = relpath(full, src_root)
        for top in tree.body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _scan_calls_for_emits(top, rel, top.name, sites)
            else:
                _scan_calls_for_emits(top, rel, None, sites)
    return sites


def declarable_sites(sites: list) -> list:
    """The declarable population: every site EXCEPT the transport exclusion."""
    return [s for s in sites if not s.is_transport_exclusion()]


def compute_L(sites: list) -> set:
    """Set of site-id literals actually present in source (declarable pop only)."""
    return {s.site_literal for s in declarable_sites(sites) if s.site_literal is not None}


# ---------------------------------------------------------------------------
# Manifest telemetry records (set M)
# ---------------------------------------------------------------------------


def manifest_telemetry_records(manifest: dict) -> list:
    """Return every {"site", "trigger", ...} record across all mechanisms'
    `telemetry` lists. A `telemetry: []` entry contributes zero records,
    deliberately (§4a: "An empty list is legitimate and silent")."""
    records: list = []
    for entry in manifest.get("mechanisms", []):
        tel = entry.get("telemetry")
        if isinstance(tel, list):
            for rec in tel:
                if isinstance(rec, dict) and "site" in rec:
                    records.append(rec)
    return records


def compute_M(manifest: dict) -> set:
    return {rec["site"] for rec in manifest_telemetry_records(manifest)}


def not_wired(L: set, M: set) -> set:
    """M - L: manifest declares a site the source never emits from."""
    return M - L


def undeclared(L: set, M: set) -> set:
    """L - M: an emit call in the declarable population with no manifest record."""
    return L - M


# ---------------------------------------------------------------------------
# telemetry-key-required population (mechanical, filesystem-derived — no
# hand-maintained allowlist)
# ---------------------------------------------------------------------------


def _mechanism_file_candidates(entry: dict, src_root_path: str) -> list:
    """Return the absolute path(s) this manifest entry corresponds to.

    Mirrors test_tiers_manifest.py's own resolution: a "source" field is a
    repo-root-relative override; otherwise the path is joined under
    src_root_path. Directory-shaped entries (skills, tools sub-packages) are
    walked for their .py members.
    """
    if "source" in entry:
        full = os.path.join(os.path.dirname(os.path.dirname(src_root_path)), entry["source"])
    else:
        full = os.path.join(src_root_path, entry["path"])
    if os.path.isdir(full):
        return list(iter_py_files(full))
    return [full]


def emitting_mechanism_paths(manifest: dict, src_root_path: str, sites: list | None = None) -> set:
    """Return the set of manifest mechanism `path` values whose corresponding
    file(s) contain at least one emit()/emit_if_initialized() call.

    Mechanical — derived from the AST scan, not a hand-maintained list. This
    is §4a's "any manifest entry whose path is a hook or tool that can emit"
    population, computed rather than enumerated, so a newly-added emit call
    automatically enters the required-declaration population.
    """
    if sites is None:
        sites = find_emit_call_sites(src_root_path)
    files_with_emits = {s.file for s in sites}
    result = set()
    for entry in manifest.get("mechanisms", []):
        for full in _mechanism_file_candidates(entry, src_root_path):
            if not os.path.isfile(full):
                continue
            rel = relpath(full, src_root_path)
            if rel in files_with_emits:
                result.add(entry["path"])
                break
    return result


# ---------------------------------------------------------------------------
# Call graph construction (for the backward walk)
# ---------------------------------------------------------------------------


@dataclass
class CallGraph:
    registry: dict  # (file, name) -> ast.FunctionDef  (top-level only)
    mcp_tool_targets: set  # (file, name) decorated with @mcp.tool(...)
    main_dispatch_targets: set  # (file, name) invoked under `if __name__ == "__main__":`
    callers_of: dict  # (file, name) -> set of (file, name) that call it


def _is_mcp_tool_decorator(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "tool"
        and isinstance(target.value, ast.Name)
        and target.value.id == "mcp"
    )


def _module_import_aliases(tree: ast.Module) -> dict:
    """Map each name bound by a top-level import to the module-relative .py
    file it (probably) refers to, resolved within src_root's flat namespace
    (this repo's own top-level modules import each other by bare name, e.g.
    `import engram_revision as _revision_mod`). Only absolute, single-segment
    module imports are resolved — sufficient for this repo's actual
    dispatch-chain imports (verified against the real tree; see module
    docstring). Anything else (relative imports, dotted packages) is left
    unresolved and simply never matches a registry key, which is safe (the
    same "wrong guess costs nothing" reasoning tests/test_tiers_manifest.py
    already uses for its own import resolver).
    """
    aliases: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name
                if "." in mod:
                    continue
                asname = alias.asname or mod
                aliases[asname] = f"{mod}.py"
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or not node.module or "." in node.module:
                continue
            # `from X import Y [as Z]` — Y may be an attribute of X.py, not a
            # separate file; record the possibility so a later `Z.something()`
            # is NOT mis-resolved (we simply don't add an alias for the name
            # itself unless it also looks like a submodule import, which our
            # dispatch chains never use). No-op here deliberately.
            continue
    return aliases


def build_call_graph(src_root_path: str) -> CallGraph:
    registry: dict = {}
    mcp_tool_targets: set = set()
    file_trees: dict = {}

    for full in iter_py_files(src_root_path):
        tree = _parse(full)
        if tree is None:
            continue
        rel = relpath(full, src_root_path)
        file_trees[rel] = tree
        for top in tree.body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = (rel, top.name)
                registry[key] = top
                if any(_is_mcp_tool_decorator(d) for d in top.decorator_list):
                    mcp_tool_targets.add(key)

    callers_of: dict = {}

    def _add_edge(caller_key, callee_key):
        callers_of.setdefault(callee_key, set()).add(caller_key)

    main_dispatch_targets: set = set()

    for rel, tree in file_trees.items():
        aliases = _module_import_aliases(tree)

        def resolve_target(call: ast.Call):
            func = call.func
            if isinstance(func, ast.Name):
                key = (rel, func.id)
                return key if key in registry else None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                target_file = aliases.get(func.value.id)
                if target_file:
                    key = (target_file, func.attr)
                    return key if key in registry else None
            return None

        # call edges, keyed by enclosing top-level function (or module scope)
        for top in tree.body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                caller_key = (rel, top.name)
                scan_node = top
            else:
                caller_key = (rel, None)
                scan_node = top
            for child in ast.walk(scan_node):
                if isinstance(child, ast.Call):
                    target = resolve_target(child)
                    if target is not None and caller_key[1] is not None:
                        _add_edge(caller_key, target)

        # main-dispatch detection: `if __name__ == "__main__": ... main() ...`
        for top in tree.body:
            if _is_main_guard(top):
                for child in ast.walk(top):
                    if isinstance(child, ast.Call):
                        target = resolve_target(child)
                        if target is not None:
                            main_dispatch_targets.add(target)

    return CallGraph(
        registry=registry,
        mcp_tool_targets=mcp_tool_targets,
        main_dispatch_targets=main_dispatch_targets,
        callers_of=callers_of,
    )


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]

    def _is_name_dunder(n):
        return isinstance(n, ast.Name) and n.id == "__name__"

    def _is_main_str(n):
        return isinstance(n, ast.Constant) and n.value == "__main__"

    return (_is_name_dunder(left) and _is_main_str(right)) or (_is_name_dunder(right) and _is_main_str(left))


def derive_dispatch_targets(graph: CallGraph, start: tuple) -> set:
    """Backward BFS from `start` (a (file, enclosing-function) key) to the
    set of dispatch targets (mcp.tool functions / main-guard-invoked mains)
    that can reach it via direct calls.

    Returns an EMPTY set if no dispatch target is reachable — this is the
    "under-count reads as NOT REACHED" bound §1 states as the walk's known
    danger direction, not a bug in the walk itself.

    `start` itself is checked first: an emit call sitting DIRECTLY inside a
    `@mcp.tool` function or inside `main()` (several of the 11 hook-side
    sites do this — no `_emit_hook_fire`-style wrapper in between) is its own
    dispatch target, with no caller edge to traverse at all. Missing this
    case was a real defect caught by running this walker against the actual
    tree (engram-surface-hook.py's three sites, engram-session-start-hook.py,
    engram-stop-hook.py, engram-loop-arm-stop-hook.py, and one of
    engram-utility-credit-mention-stop.py's two sites all sit directly inside
    `main()`, with no intervening wrapper) rather than reasoning about it.
    """
    found: set = set()
    if start in graph.mcp_tool_targets or start in graph.main_dispatch_targets:
        found.add(start)
    visited = {start}
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        for caller in graph.callers_of.get(cur, ()):
            if caller in graph.mcp_tool_targets or caller in graph.main_dispatch_targets:
                found.add(caller)
            if caller not in visited:
                visited.add(caller)
                queue.append(caller)
    return found


def enclosing_key_for_site(site: EmitCallSite) -> tuple | None:
    if site.enclosing is None:
        return None
    return (site.file, site.enclosing)


def dispatch_target_id(key) -> str:
    """Format a (file, func) dispatch-target key as the manifest_id literal
    that key's `reached` write carries in source -- e.g.
    ``("hooks/claude/engram-surface-hook.py", "main")`` -> the literal
    ``"hooks/claude/engram-surface-hook.py:main"`` that file's own
    ``_MAIN_MANIFEST_ID`` constant holds. §1's join key ("The fix is to
    DERIVE it") and its own R definition ("R := max, over the site's derived
    dispatch-target set, of that target's reached count") both need this
    SAME string shape -- one function, so the two callers (the reached
    join-key check below, and WS7's `engram_telemetry_verdicts.compute_verdicts`,
    issue #3136) cannot drift apart on the format.

    Renamed from this module's own private `_format_key` (issue #3136 WS7,
    which is this function's second caller) -- previously used only by
    `main()`'s `--report` printing.
    """
    if key is None:
        return "<unresolved>"
    return f"{key[0]}:{key[1]}"


# ---------------------------------------------------------------------------
# Dynamic-dispatch static scan (§1 STATIC block)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DynamicDispatchHit:
    file: str
    lineno: int
    shape: str  # "a" | "b" | "c"
    snippet: str


def _is_getattr_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"


def _collect_function_scopes(tree: ast.Module) -> list:
    """Partition a module's AST into (scope_node_or_None, [Call nodes],
    [Assign nodes]) units, one per function definition PLUS one for
    module-level code — each unit holding only the statements that belong to
    it, not to any function nested inside it (those get their own unit).

    This is what makes the scan correct for a CLASS METHOD (`engram_client.py`'s
    `ClientHarness.call`, the doc's own worked dynamic-dispatch example, lives
    inside a class body, not at module top-level) as well as for a nested
    closure — restricting the scan to `tree.body`'s top-level FunctionDefs
    (an earlier version of this scanner did exactly that) silently missed
    every method, which is precisely where the worked example lives.
    """
    units: list = []
    module_calls: list = []
    module_assigns: list = []
    units.append((None, module_calls, module_assigns))

    def visit(node: ast.AST, calls: list, assigns: list) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                own_calls: list = []
                own_assigns: list = []
                units.append((child, own_calls, own_assigns))
                visit(child, own_calls, own_assigns)
                continue
            if isinstance(child, ast.Call):
                calls.append(child)
            elif isinstance(child, ast.Assign):
                assigns.append(child)
            visit(child, calls, assigns)

    visit(tree, module_calls, module_assigns)
    return units


def find_dynamic_dispatch_hits(src_root_path: str) -> list:
    """Scan every .py file under src_root_path for three call shapes:

      (a) ast.Call whose OWN func is itself a call to getattr()
          e.g. getattr(obj, "method")(args)
      (b) a callee that is ANY subscript expression (dispatch-table lookup)
          e.g. table[key](args)
      (c) a two-step getattr(): a getattr() result bound to a local NAME,
          called on a LATER line within the same scope.
          e.g. fn = getattr(obj, "name", None); ...; fn()

    Scoped per FUNCTION (module-level def, class method, or nested closure —
    see `_collect_function_scopes`), not per top-level statement: a class
    method's own calls belong to that method's scope, never to its class's or
    module's.
    """
    hits: list = []
    for full in iter_py_files(src_root_path):
        tree = _parse(full)
        if tree is None:
            continue
        rel = relpath(full, src_root_path)
        with open(full, encoding="utf-8") as f:
            lines = f.readlines()

        def snippet(lineno: int) -> str:
            if 1 <= lineno <= len(lines):
                return lines[lineno - 1].strip()[:120]
            return ""

        for _scope_node, calls, assigns in _collect_function_scopes(tree):
            getattr_bound_locals: dict = {}  # local name -> lineno of getattr() assignment
            for a in assigns:
                if _is_getattr_call(a.value):
                    for target in a.targets:
                        if isinstance(target, ast.Name):
                            getattr_bound_locals[target.id] = a.lineno

            for call in calls:
                if _is_getattr_call(call.func):
                    hits.append(DynamicDispatchHit(rel, call.lineno, "a", snippet(call.lineno)))
                elif isinstance(call.func, ast.Subscript):
                    hits.append(DynamicDispatchHit(rel, call.lineno, "b", snippet(call.lineno)))
                elif isinstance(call.func, ast.Name) and call.func.id in getattr_bound_locals:
                    assign_line = getattr_bound_locals[call.func.id]
                    if call.lineno > assign_line:
                        hits.append(DynamicDispatchHit(rel, call.lineno, "c", snippet(call.lineno)))
    return hits


def dynamic_dispatch_exclusions(manifest: dict) -> set:
    """Declared exclusion set: {(file, lineno)} pairs the manifest's
    telemetry block records as known/accepted dynamic-dispatch hits, each
    carrying a reason. Default-deny — an undeclared hit fails --check."""
    tel = manifest.get("telemetry", {})
    excl = tel.get("dynamic_dispatch_exclusions", [])
    out = set()
    for entry in excl:
        if "file" in entry and "line" in entry:
            out.add((entry["file"], entry["line"]))
    return out


def zero_emit_predicate_violations(manifest: dict, sites: list) -> list[str]:
    """Hardening (Borges's colleague-read finding on #2846): an exclusion
    whose declared reason asserts a mechanically-checkable predicate --
    ``zero_emit_file: true``, meaning "this file contains zero
    emit()/emit_if_initialized() calls" -- must have that predicate
    RE-CHECKED against the live emit-site census every run, not asserted
    once by hand and trusted forever. Without this, an exclusion is a
    suppressor that can silently outlive its own justification: someone adds
    an emit call to an excluded file next month, the file's zero-emit claim
    becomes false, and nothing notices, because the exclusion set is a
    default-deny allowlist checked only against (file, lineno), never against
    the reason it was granted for.

    Returns one message per violation (empty list = clean). A violation means
    the manifest claims a file has zero emit calls but the live census finds
    at least one -- the exclusion's justification no longer holds and the
    hit must be re-reviewed, not silently kept excluded.
    """
    tel = manifest.get("telemetry", {})
    excl = tel.get("dynamic_dispatch_exclusions", [])
    emitting_files = {s.file for s in sites}
    violations = []
    for entry in excl:
        if entry.get("zero_emit_file") and entry.get("file") in emitting_files:
            violations.append(
                f"{entry['file']}:{entry.get('line')} is excluded on the claim "
                f"'zero_emit_file: true', but the live emit-site census now finds "
                f"an emit call in {entry['file']} -- the exclusion's stated "
                f"justification no longer holds; re-review it rather than leaving "
                f"it excluded."
            )
    return violations


def unresolved_dynamic_dispatch_hits(
    hits: list, exclusions: set, graph: CallGraph | None = None, declared_starts: list | None = None
) -> list:
    """Filter dynamic-dispatch hits down to the ones that are neither
    declared-excluded nor shown-unreachable by the backward walk.

    `declared_starts`, if given, is the list of (file, enclosing) keys for
    every DECLARED site (from the manifest's telemetry records). A hit is
    "shown unreachable" only in the narrow sense used by §1's residual
    argument: this gate does not attempt to prove a dynamic call site can
    never reach a declared site (that is undecidable by construction, which
    is why the scan exists) — it reports every un-excluded hit and leaves the
    reachability judgment to the reviewer who writes the exclusion reason.
    Declared_starts is accepted for API symmetry with a future stricter mode
    but is not currently used to auto-clear hits.
    """
    return [h for h in hits if (h.file, h.lineno) not in exclusions]


# ---------------------------------------------------------------------------
# Reached-literal <-> derived-dispatch-target join-key check (issue #3136,
# WS7). §1: "The fix is to DERIVE it" gives R its join key (a site's own
# derived dispatch-target set, formatted via `dispatch_target_id` above);
# this section is the STATIC half of that join -- it checks that every
# `reached` literal hand-written at a dispatch target's own first statement
# (`write_telemetry_record(manifest_id=<literal>,
# event_type="engram.telemetry.reached", ...)`, e.g. each hook's
# `_MAIN_MANIFEST_ID` or server.py's inline `"server.py:engram_query"`)
# actually names a target the gate's OWN backward walk derives for some
# declared site -- and vice versa. Same shape as NOT_WIRED/UNDECLARED (a set
# difference in both directions), one level down: NOT_WIRED/UNDECLARED
# checks site declarations against site emit calls; this checks a site's
# DERIVED reached-target set against the reached literals actually written
# in source. A typo'd literal makes its target's reached count silently 0
# forever, which §1's VERDICTS table reads as `NOT REACHED` -- "NOT a
# fault" -- so an undetected typo here is the design's own dangerous
# direction realized silently.
# ---------------------------------------------------------------------------

REACHED_EVENT_TYPE = "engram.telemetry.reached"


def _module_level_string_consts(tree: ast.Module) -> dict:
    """Map every module-TOP-LEVEL `NAME = "literal"` assignment to its string
    value (single-target, single string-constant assignments only) -- e.g.
    each hook's own `_MAIN_MANIFEST_ID = "hooks/claude/....py:main"`. Used by
    `find_reached_literals` to resolve a
    `write_telemetry_record(manifest_id=<NAME>, ...)` call whose manifest_id
    argument is a bare Name rather than a literal directly at the call (the
    hook-side convention; server.py's own reached calls pass the literal
    directly). Deliberately does not recurse into function bodies -- a
    same-named LOCAL variable shadowing a module constant is out of scope for
    this repo's actual convention (verified against the real tree, same
    reasoning `_module_import_aliases` above already uses for its own
    narrower-than-general resolution)."""
    consts: dict = {}
    for top in tree.body:
        if (
            isinstance(top, ast.Assign)
            and len(top.targets) == 1
            and isinstance(top.targets[0], ast.Name)
            and isinstance(top.value, ast.Constant)
            and isinstance(top.value.value, str)
        ):
            consts[top.targets[0].id] = top.value.value
    return consts


def find_reached_literals(src_root_path: str) -> set:
    """Scan src_root_path for every
    ``write_telemetry_record(manifest_id=..., event_type="engram.telemetry.reached", ...)``
    call and return the SET of resolved manifest_id literal strings (this is
    RL in the join-key check below).

    `manifest_id` is resolved two ways, matching the two conventions actually
    present in this tree: a literal string constant directly at the call
    (server.py's own reached writes), or a bare Name resolved against that
    file's own module-top-level string constants (every hook's
    `_MAIN_MANIFEST_ID`). A manifest_id expression that resolves to neither
    shape is skipped -- silently under-counting RL, which is the SAME "safe
    to under-report, dangerous to over-report" direction §1's own dynamic-
    dispatch scan already accepts (an unresolved reached call would surface
    instead as a DERIVED_NOT_REACHED mismatch below, once a human names the
    convention it actually uses)."""
    out: set = set()
    for full in iter_py_files(src_root_path):
        tree = _parse(full)
        if tree is None:
            continue
        consts = _module_level_string_consts(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_func_name(node.func) != "write_telemetry_record":
                continue
            event_type_val = None
            manifest_id_val = None
            for kw in node.keywords:
                if kw.arg == "event_type" and isinstance(kw.value, ast.Constant):
                    event_type_val = kw.value.value
                elif kw.arg == "manifest_id":
                    if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        manifest_id_val = kw.value.value
                    elif isinstance(kw.value, ast.Name):
                        manifest_id_val = consts.get(kw.value.id)
            if event_type_val == REACHED_EVENT_TYPE and manifest_id_val:
                out.add(manifest_id_val)
    return out


def derived_target_ids_over_declared_sites(sites: list, dispatch_targets: dict, M: set) -> set:
    """Union, over every DECLARED site (site_literal in M), of that site's
    own derived dispatch-target set (already computed by `run_gate` into
    `dispatch_targets`), each formatted via `dispatch_target_id`. This is DT
    in the join-key check below -- the exact target-id population §1's `R`
    is computed against, for every declared site at once."""
    out: set = set()
    for site in declarable_sites(sites):
        if site.site_literal in M:
            for target in dispatch_targets.get(site.key, ()):
                out.add(dispatch_target_id(target))
    return out


def reached_literal_mismatches(reached_literals: set, derived_target_ids: set) -> tuple:
    """§1's reached join-key check, both directions -- same shape as
    NOT_WIRED (M - L) / UNDECLARED (L - M) above:

      REACHED_NOT_DERIVED := RL - DT  -- a `reached` literal written in
          source names a dispatch target that no declared site's backward
          walk actually derives (a typo'd literal, or an orphaned reached
          write). Naming the bad literal IS the point: a human reads it off
          this set directly.
      DERIVED_NOT_REACHED := DT - RL  -- a declared site's own derived
          dispatch target has NO matching `reached` literal anywhere in
          source (the target's own reached-write is missing, or misspelled
          on the OTHER side of the same typo). THIS is the dangerous
          direction: an under-counted RL makes that target's `reached` count
          silently 0 forever, which §1's VERDICTS table reads as
          `NOT REACHED` -- filed "NOT a fault".

    Returns (reached_not_derived, derived_not_reached).
    """
    return (reached_literals - derived_target_ids, derived_target_ids - reached_literals)


# ---------------------------------------------------------------------------
# High-level report / check
# ---------------------------------------------------------------------------


@dataclass
class GateReport:
    L: set
    M: set
    not_wired: set
    undeclared: set
    dispatch_targets: dict  # site.key -> set of (file, name) dispatch targets
    dynamic_hits: list
    unresolved_dynamic_hits: list
    zero_emit_violations: list
    reached_literals: set  # RL -- issue #3136 WS7's join-key check
    derived_target_ids: set  # DT -- issue #3136 WS7's join-key check
    reached_not_derived: set  # RL - DT
    derived_not_reached: set  # DT - RL


def run_gate(src_root_path: str, manifest: dict) -> GateReport:
    sites = find_emit_call_sites(src_root_path)
    L = compute_L(sites)
    M = compute_M(manifest)
    graph = build_call_graph(src_root_path)

    dispatch_targets: dict = {}
    for site in declarable_sites(sites):
        start = enclosing_key_for_site(site)
        if start is None:
            dispatch_targets[site.key] = set()
            continue
        dispatch_targets[site.key] = derive_dispatch_targets(graph, start)

    dyn_hits = find_dynamic_dispatch_hits(src_root_path)
    exclusions = dynamic_dispatch_exclusions(manifest)
    unresolved = unresolved_dynamic_dispatch_hits(dyn_hits, exclusions)
    zero_emit_violations = zero_emit_predicate_violations(manifest, sites)

    reached_literals = find_reached_literals(src_root_path)
    derived_target_ids = derived_target_ids_over_declared_sites(sites, dispatch_targets, M)
    reached_not_derived, derived_not_reached = reached_literal_mismatches(reached_literals, derived_target_ids)

    return GateReport(
        L=L,
        M=M,
        not_wired=not_wired(L, M),
        undeclared=undeclared(L, M),
        dispatch_targets=dispatch_targets,
        dynamic_hits=dyn_hits,
        unresolved_dynamic_hits=unresolved,
        zero_emit_violations=zero_emit_violations,
        reached_literals=reached_literals,
        derived_target_ids=derived_target_ids,
        reached_not_derived=reached_not_derived,
        derived_not_reached=derived_not_reached,
    )


def check_ok(report: GateReport) -> bool:
    """§4a's pass condition: NOT_WIRED and UNDECLARED empty, every
    dynamic-dispatch hit either declared-excluded or (in practice, since this
    gate does not attempt automatic unreachability proofs) at least reported
    for review, no declared exclusion's zero-emit-file predicate has been
    falsified by the live census, and (issue #3136 WS7) the reached-literal
    join-key check clean in both directions. Per round 11's ruling, an
    undeclared, unresolved dynamic hit is a CI failure — never a silent
    pass; the join-key check is held to the same discipline, since a
    mismatch there silently corrupts §1's `R` computation rather than
    merely being reported wrong."""
    return (
        not report.not_wired
        and not report.undeclared
        and not report.unresolved_dynamic_hits
        and not report.zero_emit_violations
        and not report.reached_not_derived
        and not report.derived_not_reached
    )


# ---------------------------------------------------------------------------
# Dispatch map builder + emitter (issue #3136 D1 fix, REVISION 2026-09-27) —
# see the module docstring's own "DISPATCH MAP" section for why this exists.
# ---------------------------------------------------------------------------


def build_dispatch_map(src_root_path: str, manifest: dict) -> dict:
    """Walk the backward call-graph ONCE and return the JSON-serializable
    capture of every declared site's own derived dispatch-target set —
    reusing the SAME internals `run_gate` already uses
    (`find_emit_call_sites`, `declarable_sites`, `compute_M`,
    `build_call_graph`, `enclosing_key_for_site`, `derive_dispatch_targets`,
    `dispatch_target_id`), never re-implementing the walk a third time.

    Returns ``{"schema": 1, "degraded_threshold": <float>, "sites":
    {<site_literal>: [<target_id string>, ...], ...}}`` — one key per
    DECLARED site (every site literal in M, the manifest's own compute_M()
    set), even when that site's own derived-target list is empty (the
    NOT_WIRED / zero-target case) — a caller reading this map never needs
    the manifest itself to know which sites are declared.

    Raises KeyError if the manifest is missing `telemetry.degraded_threshold`
    — a map-generation-time failure, deliberately loud rather than a silent
    runtime default (issue #3136 D5: `engram_telemetry_verdicts.py` reads
    `thr` from this map, not from the manifest, so a missing key must be
    caught HERE, at generation time, not discovered as a silent fallback
    three layers downstream at verdict-compute time).
    """
    telemetry_cfg = manifest.get("telemetry")
    if not isinstance(telemetry_cfg, dict) or "degraded_threshold" not in telemetry_cfg:
        raise KeyError(
            "manifest is missing telemetry.degraded_threshold -- required to "
            "build the telemetry dispatch map (issue #3136 D5: this must be a "
            "loud failure at map-generation time, never a silent runtime "
            "default in engram_telemetry_verdicts.py)."
        )
    thr = telemetry_cfg["degraded_threshold"]

    sites = find_emit_call_sites(src_root_path)
    declarable = declarable_sites(sites)
    M = compute_M(manifest)
    graph = build_call_graph(src_root_path)

    site_map: dict = {}
    for site_literal in sorted(M):
        targets: set = set()
        for site in declarable:
            if site.site_literal != site_literal:
                continue
            start = enclosing_key_for_site(site)
            if start is None:
                continue
            targets |= derive_dispatch_targets(graph, start)
        site_map[site_literal] = sorted(dispatch_target_id(t) for t in targets)

    return {"schema": 1, "degraded_threshold": thr, "sites": site_map}


def emit_dispatch_map(path: str, src_root_path: str, manifest: dict) -> dict:
    """Build the dispatch map (`build_dispatch_map`) and write it to `path`
    as pretty-printed, sorted-keys JSON with a trailing newline. Returns the
    built map (so a caller, e.g. a test, need not re-read it off disk).
    Factored out of the `--emit-map` CLI flag so it is directly callable
    without shelling out."""
    dispatch_map = build_dispatch_map(src_root_path, manifest)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dispatch_map, f, indent=2, sort_keys=True)
        f.write("\n")
    return dispatch_map


# ---------------------------------------------------------------------------
# self-test — synthetic positive/negative fixtures
# ---------------------------------------------------------------------------


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _self_test_emit_extraction(tmp: str) -> list:
    failures = []
    _write(
        os.path.join(tmp, "mod_a.py"),
        (
            "def emit_if_initialized(event_type, level, data, site=None):\n"
            "    pass\n\n"
            "def helper():\n"
            '    emit_if_initialized("engram.x.y", 1, {}, site="mod_a.helper")\n\n'
            "def helper_no_site():\n"
            '    emit_if_initialized("engram.x.y", 1, {})\n'
        ),
    )
    sites = find_emit_call_sites(tmp)
    literals = {s.site_literal for s in sites if s.site_literal}
    if literals != {"mod_a.helper"}:
        failures.append(f"emit-extraction self-test: expected {{'mod_a.helper'}}, got {literals}")
    if len(sites) != 2:
        failures.append(f"emit-extraction self-test: expected 2 call sites total, got {len(sites)}")
    return failures


def _self_test_not_wired_undeclared() -> list:
    failures = []
    L = {"real.site.a"}
    M = {"real.site.a", "phantom.site.b"}
    nw = not_wired(L, M)
    if nw != {"phantom.site.b"}:
        failures.append(f"NOT_WIRED self-test: expected {{'phantom.site.b'}}, got {nw}")
    L2 = {"real.site.a", "orphan.site.c"}
    M2 = {"real.site.a"}
    und = undeclared(L2, M2)
    if und != {"orphan.site.c"}:
        failures.append(f"UNDECLARED self-test: expected {{'orphan.site.c'}}, got {und}")
    return failures


def _self_test_call_graph_and_walk(tmp: str) -> list:
    failures = []
    # server.py: one real mcp.tool dispatch target, calling into a helper
    # module via an attribute import, plus a same-module second caller of the
    # impl (mirrors the _add_observation_impl / batch-path shape §1 verifies).
    _write(
        os.path.join(tmp, "wserver.py"),
        (
            "import wimpl as _impl_mod\n\n"
            "class mcp:\n"
            "    @staticmethod\n"
            "    def tool():\n"
            "        def deco(f):\n"
            "            return f\n"
            "        return deco\n\n"
            "@mcp.tool()\n"
            "def wtool_a(payload_json):\n"
            "    return _impl_mod._impl(payload_json)\n"
        ),
    )
    _write(
        os.path.join(tmp, "wimpl.py"),
        (
            "def emit_if_initialized(event_type, level, data, site=None):\n"
            "    pass\n\n"
            "def _impl(payload_json):\n"
            '    emit_if_initialized("engram.w.fired", 1, {})\n'
            "    return _impl_batch(payload_json)\n\n"
            "def _impl_batch(payload_json):\n"
            "    return payload_json\n"
        ),
    )
    graph = build_call_graph(tmp)
    sites = find_emit_call_sites(tmp)
    target_site = next(s for s in sites if s.file == "wimpl.py")
    start = enclosing_key_for_site(target_site)
    found = derive_dispatch_targets(graph, start)
    expected = {("wserver.py", "wtool_a")}
    if found != expected:
        failures.append(f"call-graph self-test: expected {expected}, got {found}")
    return failures


def _self_test_wrapper_no_inflation(tmp: str) -> list:
    """Regression guard for §12.4's 'wrong six': a same-named helper defined
    separately in TWO files, each called from its own file's main(), must
    resolve to TWO distinct single-target sets — never inflate by cross-file
    name collision."""
    failures = []
    for n in ("1", "2"):
        _write(
            os.path.join(tmp, f"hook{n}.py"),
            (
                "def emit(event_type, level, data, site=None):\n"
                "    pass\n\n"
                "def _emit_hook_fire():\n"
                '    emit("engram.hook.fire", 1, {})\n\n'
                "def main():\n"
                "    _emit_hook_fire()\n"
                "    _emit_hook_fire()\n\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            ),
        )
    graph = build_call_graph(tmp)
    sites = find_emit_call_sites(tmp)
    for n in ("1", "2"):
        site = next(s for s in sites if s.file == f"hook{n}.py")
        found = derive_dispatch_targets(graph, enclosing_key_for_site(site))
        expected = {(f"hook{n}.py", "main")}
        if found != expected:
            failures.append(f"wrapper self-test (hook{n}): expected {expected}, got {found}")
    return failures


def _self_test_dynamic_dispatch_shapes(tmp: str) -> list:
    failures = []
    _write(
        os.path.join(tmp, "dyn_pos.py"),
        (
            "def f():\n"
            '    getattr(obj, "x")()\n\n'
            "def g():\n"
            "    table = {}\n"
            "    table[key]()\n\n"
            "def h():\n"
            '    fn = getattr(obj, "y", None)\n'
            "    other_stmt = 1\n"
            "    fn()\n"
        ),
    )
    _write(
        os.path.join(tmp, "dyn_neg.py"),
        (
            "def f():\n"
            '    return getattr(socket, "EAI_NODATA", -5)\n\n'
            "def g():\n"
            "    return plain_call(1, 2)\n"
        ),
    )
    hits = find_dynamic_dispatch_hits(tmp)
    pos_shapes = {h.shape for h in hits if h.file == "dyn_pos.py"}
    if pos_shapes != {"a", "b", "c"}:
        failures.append(f"dynamic-dispatch self-test: expected shapes {{'a','b','c'}} in dyn_pos.py, got {pos_shapes}")
    neg_hits = [h for h in hits if h.file == "dyn_neg.py"]
    if neg_hits:
        failures.append(f"dynamic-dispatch self-test: expected ZERO hits in dyn_neg.py, got {neg_hits}")
    return failures


def _self_test_discriminating_control(tmp: str) -> list:
    """§9 test 3b's second control: a site with ONE DIRECT caller (a real
    dispatch target) PLUS one dynamic caller elsewhere in the tree. The
    walk's target set must be NON-EMPTY (so the zero-target disjunct stays
    silent) while the dynamic-dispatch scan still finds — and, absent a
    declared exclusion, leaves UNRESOLVED — the separate dynamic hit. Only
    the static-scan clause can fail this case; that is what makes it
    discriminating (round 10's version failed to discriminate because it used
    a zero-caller site, which the empty-target-set check alone already
    catches)."""
    failures = []
    _write(
        os.path.join(tmp, "disc_impl.py"),
        (
            "def emit_if_initialized(event_type, level, data, site=None):\n"
            "    pass\n\n"
            "def _disc_impl():\n"
            '    emit_if_initialized("engram.disc.fired", 1, {}, site="engram.disc.fired")\n'
        ),
    )
    _write(
        os.path.join(tmp, "disc_server.py"),
        (
            "import disc_impl as _impl_mod\n\n"
            "class mcp:\n"
            "    @staticmethod\n"
            "    def tool():\n"
            "        def deco(f):\n"
            "            return f\n"
            "        return deco\n\n"
            "@mcp.tool()\n"
            "def disc_tool():\n"
            "    return _impl_mod._disc_impl()\n"
        ),
    )
    _write(
        os.path.join(tmp, "disc_dynamic_caller.py"),
        (
            "def maybe_call_it():\n"
            "    table = {'x': None}\n"
            "    table['x']()\n"
        ),
    )
    graph = build_call_graph(tmp)
    sites = find_emit_call_sites(tmp)
    site = next(s for s in sites if s.file == "disc_impl.py")
    found = derive_dispatch_targets(graph, enclosing_key_for_site(site))
    expected = {("disc_server.py", "disc_tool")}
    if found != expected:
        failures.append(f"discriminating-control self-test: expected NON-EMPTY {expected}, got {found}")
    dyn_hits = find_dynamic_dispatch_hits(tmp)
    unresolved = unresolved_dynamic_dispatch_hits(dyn_hits, exclusions=set())
    if not unresolved:
        failures.append(
            "discriminating-control self-test: expected an UNRESOLVED dynamic-dispatch "
            "hit in disc_dynamic_caller.py, found none — the static-scan clause "
            "would have been silently non-discriminating (round-10 defect)."
        )
    # And --check must FAIL on this fixture (manifest declares the site,
    # dispatch target set is non-empty, but the dynamic hit is unresolved).
    manifest = {
        "source_root": ".",
        "mechanisms": [
            {"path": "disc_impl.py", "telemetry": [{"site": "engram.disc.fired", "trigger": "MCP tool body"}]},
        ],
    }
    report = run_gate(tmp, manifest)
    if report.not_wired or report.undeclared:
        failures.append(
            "discriminating-control self-test: expected NOT_WIRED/UNDECLARED "
            f"clean (the site's own site= literal matches the manifest "
            f"record), got not_wired={report.not_wired} undeclared={report.undeclared}"
        )
    if check_ok(report):
        failures.append(
            "discriminating-control self-test: expected check_ok()=False "
            "(unresolved dynamic-dispatch hit, with NOT_WIRED/UNDECLARED "
            "otherwise clean and a NON-EMPTY target set), got True"
        )
    return failures


def _self_test_reached_join_key_typo(tmp: str) -> list:
    """Issue #3136 WS7's join-key check, first control: a `reached` literal
    written in source that does NOT match its own site's derived dispatch
    target (a typo). `REACHED_NOT_DERIVED` (RL - DT) must name the bad
    literal; `DERIVED_NOT_REACHED` (DT - RL) is non-empty too in this
    single-target fixture (nothing else could match the correct id either) —
    both directions failing on the SAME typo is the expected shape here, not
    a discrimination failure (see the missing-literal fixture below for the
    control that isolates the other direction alone)."""
    failures = []
    _write(
        os.path.join(tmp, "join1_impl.py"),
        (
            "def emit_if_initialized(event_type, level, data, site=None):\n"
            "    pass\n\n"
            "def _join1_impl():\n"
            '    emit_if_initialized("engram.join1.fired", 1, {}, site="join1.fired")\n'
        ),
    )
    _write(
        os.path.join(tmp, "join1_server.py"),
        (
            "import join1_impl as _impl_mod\n\n"
            "def write_telemetry_record(manifest_id, event_type, fields=None):\n"
            "    pass\n\n"
            "class mcp:\n"
            "    @staticmethod\n"
            "    def tool():\n"
            "        def deco(f):\n"
            "            return f\n"
            "        return deco\n\n"
            "@mcp.tool()\n"
            "def join1_tool():\n"
            '    write_telemetry_record(manifest_id="join1_server.py:join1_toolTYPO", '
            'event_type="engram.telemetry.reached", fields={})\n'
            "    return _impl_mod._join1_impl()\n"
        ),
    )
    sites = find_emit_call_sites(tmp)
    graph = build_call_graph(tmp)
    manifest = {"mechanisms": [{"path": "join1_impl.py", "telemetry": [{"site": "join1.fired", "trigger": "MCP tool body"}]}]}
    M = compute_M(manifest)
    dispatch_targets = {s.key: derive_dispatch_targets(graph, enclosing_key_for_site(s)) for s in declarable_sites(sites)}
    RL = find_reached_literals(tmp)
    DT = derived_target_ids_over_declared_sites(sites, dispatch_targets, M)
    reached_not_derived, derived_not_reached = reached_literal_mismatches(RL, DT)
    if "join1_server.py:join1_toolTYPO" not in reached_not_derived:
        failures.append(
            "reached-join-key typo self-test: expected the typo'd literal "
            f"'join1_server.py:join1_toolTYPO' in REACHED_NOT_DERIVED, got {reached_not_derived}"
        )
    if not derived_not_reached:
        failures.append(
            "reached-join-key typo self-test: expected the correct target id "
            "('join1_server.py:join1_tool') in DERIVED_NOT_REACHED (its own "
            "reached literal is the typo'd string, not itself), got empty"
        )
    return failures


def _self_test_reached_join_key_missing(tmp: str) -> list:
    """Issue #3136 WS7's join-key check, second control: a declared site
    whose derived dispatch target has NO `reached` literal at all anywhere
    in source (the write was simply never added). This isolates
    `DERIVED_NOT_REACHED` (DT - RL) as the ONLY non-empty direction —
    `REACHED_NOT_DERIVED` must stay empty, which is what makes this the
    control for "the other" direction rather than a duplicate of the typo
    fixture above."""
    failures = []
    _write(
        os.path.join(tmp, "join2_impl.py"),
        (
            "def emit_if_initialized(event_type, level, data, site=None):\n"
            "    pass\n\n"
            "def _join2_impl():\n"
            '    emit_if_initialized("engram.join2.fired", 1, {}, site="join2.fired")\n'
        ),
    )
    _write(
        os.path.join(tmp, "join2_server.py"),
        (
            "import join2_impl as _impl_mod\n\n"
            "class mcp:\n"
            "    @staticmethod\n"
            "    def tool():\n"
            "        def deco(f):\n"
            "            return f\n"
            "        return deco\n\n"
            "@mcp.tool()\n"
            "def join2_tool():\n"
            "    return _impl_mod._join2_impl()\n"
        ),
    )
    sites = find_emit_call_sites(tmp)
    graph = build_call_graph(tmp)
    manifest = {"mechanisms": [{"path": "join2_impl.py", "telemetry": [{"site": "join2.fired", "trigger": "MCP tool body"}]}]}
    M = compute_M(manifest)
    dispatch_targets = {s.key: derive_dispatch_targets(graph, enclosing_key_for_site(s)) for s in declarable_sites(sites)}
    RL = find_reached_literals(tmp)
    DT = derived_target_ids_over_declared_sites(sites, dispatch_targets, M)
    reached_not_derived, derived_not_reached = reached_literal_mismatches(RL, DT)
    if reached_not_derived:
        failures.append(
            f"reached-join-key missing-literal self-test: expected REACHED_NOT_DERIVED "
            f"empty (no reached literal exists at all in this fixture), got {reached_not_derived}"
        )
    if "join2_server.py:join2_tool" not in derived_not_reached:
        failures.append(
            "reached-join-key missing-literal self-test: expected "
            f"'join2_server.py:join2_tool' in DERIVED_NOT_REACHED, got {derived_not_reached}"
        )
    return failures


def self_test() -> list:
    """Run every synthetic positive/negative fixture. Returns a list of
    failure strings (empty = all passed)."""
    failures: list = []
    with tempfile.TemporaryDirectory(prefix="telemetry-gate-selftest-") as tmp:
        failures += _self_test_emit_extraction(tmp)
    failures += _self_test_not_wired_undeclared()
    with tempfile.TemporaryDirectory(prefix="telemetry-gate-selftest-") as tmp:
        failures += _self_test_call_graph_and_walk(tmp)
    with tempfile.TemporaryDirectory(prefix="telemetry-gate-selftest-") as tmp:
        failures += _self_test_wrapper_no_inflation(tmp)
    with tempfile.TemporaryDirectory(prefix="telemetry-gate-selftest-") as tmp:
        failures += _self_test_dynamic_dispatch_shapes(tmp)
    with tempfile.TemporaryDirectory(prefix="telemetry-gate-selftest-") as tmp:
        failures += _self_test_discriminating_control(tmp)
    with tempfile.TemporaryDirectory(prefix="telemetry-gate-selftest-") as tmp:
        failures += _self_test_reached_join_key_typo(tmp)
    with tempfile.TemporaryDirectory(prefix="telemetry-gate-selftest-") as tmp:
        failures += _self_test_reached_join_key_missing(tmp)
    return failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true", help="run only the detector self-test and exit")
    ap.add_argument("--check", action="store_true", help="run the §4a CI gate (exit 1 on failure)")
    ap.add_argument("--report", "--dump", dest="report", action="store_true",
                     help="print every site's derived dispatch-target set and the dynamic-dispatch scan result")
    ap.add_argument("--manifest", default=MANIFEST_PATH, help="path to packaging/tiers.json")
    ap.add_argument("--src-root", default=None, help="override source root (default: manifest's source_root)")
    ap.add_argument("--emit-map", default=None, metavar="PATH",
                     help="write the built telemetry dispatch map (issue #3136 D1) to PATH and exit "
                          "(the committed src/engram/telemetry_targets.json is regenerated this way)")
    args = ap.parse_args()

    failures = self_test()
    if failures:
        print("telemetry-manifest-gate: SELF-TEST FAILED — refusing to report a result.", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 2
    if args.self_test:
        print("telemetry-manifest-gate: self-test OK (emit extraction, NOT_WIRED/UNDECLARED, "
              "call-graph walk, wrapper non-inflation, dynamic-dispatch shapes, discriminating "
              "control, reached-join-key typo/missing controls).")
        return 0

    manifest = load_manifest(args.manifest)
    # repo_root for source_root resolution: the manifest's own grandparent's
    # grandparent (src/build/packaging/tiers.json -> repo root), same
    # relationship MANIFEST_PATH has to REPO_ROOT above.
    manifest_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(args.manifest)))))
    src_root_path = args.src_root or source_root(manifest, manifest_repo_root)

    if args.emit_map:
        dispatch_map = emit_dispatch_map(args.emit_map, src_root_path, manifest)
        print(
            f"telemetry-manifest-gate: wrote dispatch map "
            f"({len(dispatch_map['sites'])} declared site(s)) -> {args.emit_map}"
        )
        return 0

    report = run_gate(src_root_path, manifest)

    if args.report:
        print(f"L (site literals in source): {sorted(report.L)}")
        print(f"M (site literals in manifest): {sorted(report.M)}")
        print(f"NOT WIRED (M - L): {sorted(report.not_wired)}")
        print(f"UNDECLARED (L - M): {sorted(report.undeclared)}")
        print()
        print(f"Emit call sites and their derived dispatch-target sets ({len(report.dispatch_targets)} sites):")
        for key in sorted(report.dispatch_targets):
            targets = report.dispatch_targets[key]
            targets_str = ", ".join(sorted(dispatch_target_id(t) for t in targets)) or "<EMPTY — no dispatch target reached>"
            print(f"  {key[0]}:{key[1]} -> {{{targets_str}}}")
        print()
        print(f"Dynamic-dispatch scan: {len(report.dynamic_hits)} hit(s), "
              f"{len(report.unresolved_dynamic_hits)} unresolved:")
        for h in report.dynamic_hits:
            marker = "UNRESOLVED" if h in report.unresolved_dynamic_hits else "excluded"
            print(f"  [{marker}] {h.file}:{h.lineno} (shape {h.shape}) — {h.snippet}")
        print()
        print(f"Reached literals in source (RL): {sorted(report.reached_literals)}")
        print(f"Derived target ids over declared sites (DT): {sorted(report.derived_target_ids)}")
        print(f"REACHED_NOT_DERIVED (RL - DT): {sorted(report.reached_not_derived)}")
        print(f"DERIVED_NOT_REACHED (DT - RL): {sorted(report.derived_not_reached)}")

    if args.check:
        ok = check_ok(report)
        if ok:
            print("telemetry-manifest-gate: CLEAN — NOT_WIRED/UNDECLARED empty, "
                  "no unresolved dynamic-dispatch hits, reached join-key check clean.")
            return 0
        print("telemetry-manifest-gate: FAILED", file=sys.stderr)
        if report.not_wired:
            print(f"  NOT WIRED (manifest declares, source never emits): {sorted(report.not_wired)}", file=sys.stderr)
        if report.undeclared:
            print(f"  UNDECLARED (source emits, manifest has no record): {sorted(report.undeclared)}", file=sys.stderr)
        if report.unresolved_dynamic_hits:
            print("  Unresolved dynamic-dispatch hits (declare an exclusion with a reason, "
                  "or show the walk cannot reach a declared site):", file=sys.stderr)
            for h in report.unresolved_dynamic_hits:
                print(f"    {h.file}:{h.lineno} (shape {h.shape}) — {h.snippet}", file=sys.stderr)
        if report.zero_emit_violations:
            print("  Exclusions whose zero-emit-file justification no longer holds:", file=sys.stderr)
            for v in report.zero_emit_violations:
                print(f"    {v}", file=sys.stderr)
        if report.reached_not_derived:
            print(f"  REACHED_NOT_DERIVED (a `reached` literal in source names no derived "
                  f"dispatch target -- typo'd or orphaned): {sorted(report.reached_not_derived)}", file=sys.stderr)
        if report.derived_not_reached:
            print(f"  DERIVED_NOT_REACHED (a declared site's derived dispatch target has no "
                  f"matching `reached` literal in source -- its reached count is silently 0 "
                  f"forever): {sorted(report.derived_not_reached)}", file=sys.stderr)
        return 1

    if not args.report:
        print("telemetry-manifest-gate: nothing to do — pass --self-test, --check, --report, or --emit-map PATH.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
