#!/usr/bin/env python3
"""tools/engramd_read_scan.py -- build-time read-template scanner (U4c, #3196,
part of #2652; design docs/2652-security-slice-design.md SS3.2-3.5).

Walks the call graph from declared hook roots and classifies every
``.execute*`` call it reaches. The output is the committed template list
``src/engram/engramd_read_templates.json`` that the owner's read-template
registry (src/engram/engramd_read_registry.py) loads at start.

THIS IS A BUILD-TIME TOOL. It reads Python source and reports; it never
modifies the tree it scans.

Reuse (R11)
===========
The call graph, the root resolution (``<file>:<function>`` and the
self-rooted ``<file>:<line>`` form) and the breadth-first reach all come
from ``tools/write_op_census.py`` (U2): ``_scan_package`` builds the graph,
``_resolve_root`` resolves a declared root, ``_bfs_reach`` walks it. U2's
own site detector only sees write verbs, so a READ-site classifier lives
here, alongside, and feeds ``_bfs_reach`` its own per-function site lists.
U2's behaviour is not touched.

Outcome per reached site (R10)
==============================
  literal / module_literal SELECT (also WITH, VALUES) -> a registered template
  PRAGMA <name>  (bare)                                -> authorizer_governed
  INSERT/UPDATE/DELETE/REPLACE/DDL/transaction verbs,
    and any PRAGMA with ``=`` or an argument list      -> write_sites
  a site named by an ``exclusions`` entry              -> declared_out_of_scope
  EVERYTHING ELSE -- an f-string, a variable, a concatenation, a call, a
  statement whose verb is not recognised, a PRAGMA in argument form -- and
  an exclusion that names no reached site             -> ``failures``

Any failure makes the CLI exit 1 and write NOTHING. There is no allow-list,
no "pending" bucket, no ignore default: an unknown kind is a failure.

Known holes, stated rather than hidden
======================================
* The root set is PRESCRIBED, not checked (same hole (b) as U2): a hook
  function or module-level site left out of the config is not scanned.
* U2's edges match calls by bare name across the package (a name collision
  over-reaches) and attribute calls (``obj.method()``) produce no edge (an
  unseen call under-reaches). Neither effect has been measured as dominant.
* A literal reached through a function-local variable is a ``variable``
  failure, not a template: only a call-site literal, an adjacent-literal
  concatenation, or a single module-level string constant is readable.
* The scanner classifies nothing against a schema; the registry does that at
  load time against the LIVE store (R12).

CLI
===
    python tools/engramd_read_scan.py --package DIR --config FILE --out FILE
    python tools/engramd_read_scan.py --package DIR --config FILE --check FILE

config: {"roots": ["<file>:<function>" | "<file>:<line>", ...],
         "exclusions": [{"site": "<file>:<line>", "reason": "..."}]}

Exit 0 ok; 1 failures (or ``--check`` drift/missing file); 2 bad input
(missing package dir, unreadable or malformed config, unresolvable root).
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

SCHEMA_VERSION = 1


def _load_u2():
    """Load tools/write_op_census.py (U2) from beside this file."""
    existing = sys.modules.get("write_op_census")
    if existing is not None and hasattr(existing, "_scan_package"):
        return existing
    path = Path(__file__).resolve().with_name("write_op_census.py")
    spec = importlib.util.spec_from_file_location("write_op_census", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["write_op_census"] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


@dataclasses.dataclass(frozen=True)
class ReadSite:
    """One ``.execute*`` call. ``file``/``line``/``col_offset`` are the three
    attributes U2's ``_bfs_reach`` keys on."""

    file: str
    line: int
    col_offset: int
    kind: str  # literal | module_literal | fstring | variable | concat | call | executescript
    sql: Optional[str]

    @property
    def id(self) -> str:
        return f"{self.file}:{self.line}"


# ---------------------------------------------------------------------------
# Site extraction
# ---------------------------------------------------------------------------


def _module_string_constants(tree: ast.Module) -> Dict[str, Optional[str]]:
    """Module-level ``NAME = "literal"`` assignments. A name assigned more
    than once at module level maps to None (not a single readable constant)."""
    consts: Dict[str, Optional[str]] = {}
    for node in tree.body:
        targets: List[ast.expr] = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for tgt in targets:
            if not isinstance(tgt, ast.Name):
                continue
            readable = isinstance(value, ast.Constant) and isinstance(value.value, str)
            if tgt.id in consts:
                consts[tgt.id] = None
            else:
                consts[tgt.id] = value.value if readable else None
    return consts


def _bound_names(func: ast.AST) -> Set[str]:
    """Every name bound anywhere in ``func`` (parameters, plain / augmented /
    annotated assignment, ``for`` and ``with ... as`` targets, walrus,
    comprehension targets -- all are ``Name`` nodes in Store context -- plus
    ``except ... as``, imports and nested def/class names). Deliberately
    over-inclusive (nested scopes are walked too): the only cost of a name
    wrongly counted as bound is a ``variable`` failure, which fails closed."""
    bound: Set[str] = set()
    args = getattr(func, "args", None)
    if args is not None:
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if a is not None:
                bound.add(a.arg)
    for node in ast.walk(func):
        if isinstance(node, ast.arg):  # params of ANY lambda / nested function
            bound.add(node.arg)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not func:
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
    return bound


def _classify_arg(
    call: ast.Call, consts: Dict[str, Optional[str]], shadowed: Set[str] = frozenset()
) -> Tuple[str, Optional[str]]:
    if not call.args or isinstance(call.args[0], ast.Starred):
        return "variable", None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return "literal", arg.value
    if isinstance(arg, ast.Name):
        if arg.id in shadowed:
            # Bound in the enclosing function (or one lexically around it):
            # the module constant of the same name is NOT what runs.
            return "variable", None
        value = consts.get(arg.id)
        if value is not None:
            return "module_literal", value
        return "variable", None
    if isinstance(arg, ast.JoinedStr):
        return "fstring", None
    if isinstance(arg, ast.BinOp):
        return "concat", None
    if isinstance(arg, ast.Call):
        return "call", None
    return "variable", None


def _execute_sites(node: ast.AST, relfile: str, consts, u2, shadowed: Set[str] = frozenset()) -> List[ReadSite]:
    """Every ``.execute*`` call in ``node``'s direct body (U2's own
    ``_direct_body_nodes``: nested defs are scanned as their own entries)."""
    sites: List[ReadSite] = []
    for child in u2._direct_body_nodes(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr.startswith("execute")
        ):
            if child.func.attr == "executescript":
                # Runs many statements: never classifiable by a first verb.
                sites.append(ReadSite(relfile, child.lineno, child.col_offset, "executescript", None))
                continue
            kind, sql = _classify_arg(child, consts, shadowed)
            sites.append(ReadSite(relfile, child.lineno, child.col_offset, kind, sql))
    return sites


def _scan_read_sites(package_dir: Path, scan, u2):
    """-> (sites_by_func, site_by_loc) in U2's FuncKey/SiteKey shapes."""
    sites_by_func: Dict[tuple, List[ReadSite]] = {}
    site_by_loc: Dict[tuple, ReadSite] = {}
    for path in sorted(package_dir.rglob("*.py")):
        relfile = u2._relpath(path, package_dir)
        tree = ast.parse(path.read_text(), filename=relfile)
        consts = _module_string_constants(tree)
        for site in _execute_sites(tree, relfile, consts, u2):
            site_by_loc[(site.file, site.line, site.col_offset)] = site
        parent = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        for node in ast.walk(tree):
            if not isinstance(node, u2._STOP_TYPES):
                continue
            fkey = (relfile, node.name, node.lineno)
            shadowed: Set[str] = set()
            scope = node
            while scope is not None:
                if isinstance(scope, u2._STOP_TYPES):
                    shadowed |= _bound_names(scope)
                scope = parent.get(scope)
            sites = _execute_sites(node, relfile, consts, u2, shadowed)
            sites_by_func[fkey] = sites
            for site in sites:
                site_by_loc[(site.file, site.line, site.col_offset)] = site
    return sites_by_func, site_by_loc


# ---------------------------------------------------------------------------
# Statement classification (literal kinds only)
# ---------------------------------------------------------------------------

_LEADING_NOISE = re.compile(r"\s+|--[^\n]*(?:\n|$)|/\*.*?\*/", re.DOTALL)
_READ_VERBS = frozenset({"SELECT", "WITH", "VALUES"})
_WRITE_VERBS = frozenset({
    "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "VACUUM",
    "ATTACH", "DETACH", "REINDEX", "ANALYZE", "BEGIN", "COMMIT", "END", "ROLLBACK",
    "SAVEPOINT", "RELEASE",
})
_WRITE_WORD_IN_CTE = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
_BARE_PRAGMA = re.compile(r"PRAGMA\s+(?:[A-Za-z_]\w*\s*\.\s*)?[A-Za-z_]\w*\s*;?\s*$", re.IGNORECASE)


def _strip_leading_noise(sql: str) -> str:
    pos = 0
    while True:
        m = _LEADING_NOISE.match(sql, pos)
        if not m or m.end() == pos:
            return sql[pos:]
        pos = m.end()


def _classify_literal(sql: str) -> str:
    """-> 'read' | 'pragma_governed' | 'write' | 'pragma_form' | 'unclassified'."""
    body = _strip_leading_noise(sql)
    m = re.match(r"[A-Za-z]+", body)
    verb = m.group(0).upper() if m else ""
    if verb in _READ_VERBS:
        if verb == "WITH" and _WRITE_WORD_IN_CTE.search(body):
            return "write"
        return "read"
    if verb in _WRITE_VERBS:
        return "write"
    if verb == "PRAGMA":
        return "pragma_governed" if _BARE_PRAGMA.match(body) else (
            "write" if "=" in body else "pragma_form")
    return "unclassified"


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def _site_sort_key(site_id: str) -> Tuple[str, int]:
    file_part, _, line = site_id.rpartition(":")
    return (file_part, int(line))


def _template_key(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def scan(package_dir: Path, roots: List[str], exclusions: List[dict]) -> dict:
    """See the module docstring. Raises ``write_op_census.CensusInputError``
    for a root that resolves to nothing."""
    u2 = _load_u2()
    package_dir = Path(package_dir)
    base = u2._scan_package(package_dir, set())  # U2's call graph, unchanged
    sites_by_func, site_by_loc = _scan_read_sites(package_dir, base, u2)
    view = dataclasses.replace(base, sites_by_func=sites_by_func, site_by_loc=site_by_loc)

    reached: Dict[tuple, ReadSite] = {}
    for root_decl in roots:
        start_funcs, extra_sites = u2._resolve_root(root_decl, view)
        for site in u2._bfs_reach(start_funcs, view.edges, view.sites_by_func):
            reached[(site.file, site.line, site.col_offset)] = site
        for site in extra_sites:
            reached[(site.file, site.line, site.col_offset)] = site

    excluded = {e["site"]: e for e in exclusions}
    reached_ids = {s.id for s in reached.values()}

    templates: Dict[str, Set[str]] = {}
    sql_by_key: Dict[str, str] = {}
    governed: Set[str] = set()
    writes: Set[str] = set()
    failures: List[dict] = []

    for site in sorted(reached.values(), key=lambda s: (s.file, s.line, s.col_offset)):
        if site.id in excluded:
            continue
        if site.kind in ("literal", "module_literal"):
            verdict = _classify_literal(site.sql)
            if verdict == "read":
                key = _template_key(site.sql)
                templates.setdefault(key, set()).add(site.id)
                sql_by_key[key] = site.sql
            elif verdict == "pragma_governed":
                governed.add(site.id)
            elif verdict == "write":
                writes.add(site.id)
            else:
                failures.append({"site": site.id, "kind": verdict})
        else:
            failures.append({"site": site.id, "kind": site.kind})

    for site_id, entry in excluded.items():
        if site_id not in reached_ids:
            failures.append({"site": site_id, "kind": "stale_exclusion"})

    failures.sort(key=lambda f: (_site_sort_key(f["site"]), f["kind"]))
    return {
        "schema": SCHEMA_VERSION,
        "templates": [
            {
                "key": key,
                "sql": sql_by_key[key],
                "sites": sorted(templates[key], key=_site_sort_key),
            }
            for key in sorted(templates)
        ],
        "authorizer_governed": sorted(governed, key=_site_sort_key),
        "write_sites": sorted(writes, key=_site_sort_key),
        "declared_out_of_scope": [
            {"site": e["site"], "reason": e["reason"]}
            for e in sorted(
                (e for sid, e in excluded.items() if sid in reached_ids),
                key=lambda e: _site_sort_key(e["site"]),
            )
        ],
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _committed_payload(result: dict) -> dict:
    """The committed file is the scan minus ``failures`` (always empty there)."""
    return {k: v for k, v in result.items() if k != "failures"}


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build-time read-template scanner (U4c, #3196).")
    p.add_argument("--package", required=True, metavar="DIR")
    p.add_argument("--config", required=True, metavar="FILE")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--out", metavar="FILE", help="write the template list (only when there are no failures)")
    g.add_argument("--check", metavar="FILE", help="exit 1 if FILE differs from a fresh scan")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    u2 = _load_u2()

    package_dir = Path(args.package)
    if not package_dir.is_dir():
        print(f"engramd_read_scan: error -- package directory not found: {package_dir}", file=sys.stderr)
        return 2
    try:
        config = json.loads(Path(args.config).read_text())
    except (OSError, ValueError) as exc:
        print(f"engramd_read_scan: error -- cannot use config {args.config}: {exc}", file=sys.stderr)
        return 2
    roots = config.get("roots") if isinstance(config, dict) else None
    exclusions = config.get("exclusions", []) if isinstance(config, dict) else None
    if not isinstance(roots, list) or not isinstance(exclusions, list) or not all(
        isinstance(e, dict) and isinstance(e.get("site"), str) and isinstance(e.get("reason"), str)
        for e in exclusions
    ):
        print("engramd_read_scan: error -- config needs 'roots' (list) and 'exclusions' "
              "(list of {site, reason})", file=sys.stderr)
        return 2

    try:
        result = scan(package_dir, roots, exclusions)
    except u2.CensusInputError as exc:
        print(f"engramd_read_scan: error -- {exc}", file=sys.stderr)
        return 2

    if result["failures"]:
        for f in result["failures"]:
            print(f"engramd_read_scan: FAILURE {f['site']}  kind={f['kind']}", file=sys.stderr)
        print(f"engramd_read_scan: {len(result['failures'])} failure(s); nothing written", file=sys.stderr)
        return 1

    payload = _committed_payload(result)
    if args.check is not None:
        try:
            committed = json.loads(Path(args.check).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"engramd_read_scan: DRIFT -- cannot read {args.check}: {exc}", file=sys.stderr)
            return 1
        if committed != payload:
            print(f"engramd_read_scan: DRIFT -- {args.check} differs from a fresh scan; "
                  "regenerate with --out", file=sys.stderr)
            return 1
        return 0

    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
