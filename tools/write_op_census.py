#!/usr/bin/env python3
"""tools/write_op_census.py — call-path write-operation census generator
(#3095, phase-1 build unit U2 of the #2652 EngramManager migration).

Design context: docs/2652-engrammanager-design.md §3.1 ("The phase-1
waypoint, and its asymmetry") and §7 ("Open, and whose call each one is").
§3.1 records that three honest hand-walks of a single call path
(`create_node(derivation)`) by two people produced three different answers —
not carelessness, a human tracing a call graph by eye misses transitive
calls. §7's first row says the fix is to GENERATE the write census by
walking call paths mechanically, never by hand-enumerating statements: an
enumeration over *statements* (or over `(verb, table)` PAIRS) can partition
one caller intent across many sites and still look complete when one
specific site is unreached, because a different site sharing the same pair
keeps the union looking covered. **The check this tool exists to make is at
the SITE level (one write statement's file:line), never the pair level.**

THIS IS A MEASUREMENT TOOL. It changes no runtime behavior: it reads Python
source under a declared package directory and reports findings; it never
modifies the codebase it scans.

Scope of THIS build (U2): correct against small, synthetic FIXTURE packages
(see tests/test_write_op_census.py) — NOT run against the real src/engram
tree, and NOT a real declaration of engram-alpha's production roots/tables.
That is a separate, later concern (§3.1 itself calls the table set "a
declared input, not a given"). This build proves the WALKER; it does not
enumerate reality.

Also out of scope (per §2.0's mechanism-vs-operation split): this tool stops
at ENTRY POINTS (signatures) and SITES. It does not attempt to further split
one entry point into the several caller intents ("operations") it might
carry — that is a later step, per §7's own scoping note ("a walk yields
entry points ... splitting an entry point into its invocations is a further
step the walk does not perform").

Input format
============
Two files:

  --package DIR    Root directory of the source package to scan. Every
                    ``*.py`` file under it (recursive) is parsed with
                    Python's `ast` module. **Scope is `.py` files only** —
                    see known-hole (c) below.

  --config FILE     A small JSON file declaring the walker's two inputs as a
                    flat object with two keys:

                        {
                          "roots":  ["<file>:<function_name>", "<file>:<line>", ...],
                          "tables": ["<table_name>", ...]
                        }

                    `roots` — a list of declared caller-facing write entry
                    points, each written as a path RELATIVE TO --package,
                    joined with a colon to either:

                      * a FUNCTION NAME (`"root.py:root_x"`) — the walker
                        starts a call-graph traversal at that function
                        definition and credits every write site reached
                        transitively (directly, or via any chain of
                        same-package function calls) to this root.

                      * a LINE NUMBER (`"orphan.py:5"`) — a SELF-ROOTED
                        DECLARED SITE (contract item 4 below). If that line
                        falls inside a function's span, the walker starts
                        the SAME traversal from that enclosing function
                        (so a self-rooted site's own further calls are
                        still traced) but files the result under the
                        LITERAL declared string as the per-root key — i.e.
                        the site is credited to ITSELF, not folded into
                        whatever function happens to enclose it. If the
                        line falls at module scope (no enclosing function —
                        the shape of the two real examples named in §7,
                        `engram-surface-hook.py:955` and
                        `engram-utility-credit-mention-stop.py:145`), the
                        walker requires that a recognized write site sits
                        at EXACTLY that line, and credits that one site to
                        itself with no further traversal (there is nothing
                        to traverse from at module scope). JUDGMENT CALL
                        (round-2 fix, undocumented until now, not exercised
                        by any fixture): a self-rooted declaration names
                        only a LINE, never a column, so if MORE THAN ONE
                        write site shares that line (see the same-line-
                        collision fix below), the walker deterministically
                        picks the LEFTMOST one (smallest `col_offset`)
                        rather than raising or depending on dict-insertion
                        order.

                      A root string that resolves to neither a known
                      function nor a recognized site raises
                      `CensusInputError` — refused loudly, never silently
                      dropped or silently treated as a no-op.

                    `tables` — a flat list of table-name strings: the
                    DECLARED scope of tables this run cares about (§3.1:
                    "the table set is a declared input, not a given"). A
                    write statement whose table is not in this list is
                    still recorded — as an explicit out-of-scope finding
                    (contract item 5) — never silently dropped.

Why this shape and not the alternative named in the dispatch spec (dotted
`module.path:function_name`): a dotted module path needs import machinery
(sys.path setup, package `__init__.py` discovery) to resolve back to a
source file, which this tool has no need of — it already reads every `.py`
file under --package directly. A file-relative path is simpler, avoids that
whole resolution step, and is symmetric with the file:line self-rooting
form, which has no dotted-module equivalent at all (a line number is not
importable). Documented here as the design choice it is — see the coder's
handoff for this being flagged explicitly as a judgment call against the
spec's own suggested alternative.

Write-statement / (verb, table) detection
==========================================
A write statement is any `ast.Call` whose `func` is an attribute access
ending in `.execute` (`cursor.execute(...)`, `conn.execute(...)`,
`self.db.cursor().execute(...)`, — any attribute chain, only the trailing
`.execute` matters) whose first positional argument is a plain string
literal (`ast.Constant` with a `str` value — this also covers Python's
automatic adjacent-string-literal concatenation, e.g. a multi-line SQL
string split across several literal lines, since the parser has already
joined those into one `ast.Constant` by the time `ast.parse` hands back the
tree). Calls built from an f-string, `.format()`, string concatenation via
`+`, or any other dynamic construction are NOT recognized writes — this
tool does not attempt to handle every SQL-construction idiom, only literal
strings, which is sufficient for the fixtures this build is proven against
(see the module docstring's "Scope of THIS build" note above).

The literal SQL text is classified with a small set of case-insensitive
regexes, checked in this order:

  1. `INSERT INTO <table>` — if the SAME statement ALSO contains
     `ON CONFLICT ... DO UPDATE` (contract item 6, see below), the verb is
     the composite `"upsert"`, not `"INSERT"`.
  2. `UPDATE <table>`
  3. `DELETE FROM <table>`

Any statement matching none of these three shapes is not a recognized write
and is silently skipped (this tool doesn't need to handle every SQL dialect
quirk, only the shapes exercised by its own fixtures).

The ON CONFLICT ... DO UPDATE rule (contract item 6)
=====================================================
**Chosen, documented rule: a single `INSERT ... ON CONFLICT ... DO UPDATE`
statement is recorded as ONE site carrying ONE named composite verb,
`"upsert"`, against the statement's target table — never as two separate
`(INSERT, table)` and `(UPDATE, table)` pairs, and never as two sites.**
This was a genuine, spec-left-open design choice (the alternative offered
was "record both verbs against the same site and table"); the composite-verb
form was chosen because it keeps the per-root `pairs` list a set of
distinct CALLER-VISIBLE write shapes rather than doubling every upsert site
into two entries that would need to be recognized-as-a-pair downstream by
whatever later step consumes this tool's output. See
`tests/test_write_op_census.py::test_item6_...` for the fixture that tests
this exact rule.

Call-graph construction
========================
For every `.py` file under --package: `ast.parse` it, then for every
`FunctionDef` / `AsyncFunctionDef` node found anywhere in the tree
(`ast.walk` — this reaches nested `def`s too, each recorded as its own
function with its own key), scan its DIRECT body only — recursing through
ordinary control-flow (`if`/`for`/`while`/`try`/`with`, etc.) but NOT
descending into any NESTED `FunctionDef`/`AsyncFunctionDef` found along the
way (that nested function's own body is scanned separately, as its own
entry, when `ast.walk` reaches it) — for two disjoint things:

  * a bare-name call expression, `some_name(...)` (`ast.Name` func) — a
    candidate CALL-GRAPH EDGE. Resolved purely BY NAME against every
    function defined anywhere in the package (same-file or cross-file,
    per the dispatch spec's "keep it simple" instruction) — if more than
    one function shares that name across the package, edges are added to
    EVERY match (an over-approximation, documented here rather than
    silently arbitrary; round-2 fix added
    `test_item9a_bare_name_collision_credits_every_matching_function`,
    which exercises this branch directly with two same-named `helper()`
    functions in different files, both reachable from one root).
    Attribute-form calls (`self.helper()`, `obj.method()`) are NOT resolved
    as edges — only bare `name(...)` calls are, a deliberate simplification
    named in the dispatch spec's own suggested design (round-2 fix added
    `test_item9b_attribute_form_call_is_not_resolved_as_an_edge`, which
    confirms a `self.helper()`-reached write site stays UNREACHED).

  * a `.execute(...)` call as described above — a candidate WRITE SITE.
    Internally keyed by `(file, line, col_offset)` — NOT `(file, line)`
    alone — a round-2 fix: two write sites can share one physical LINE
    (e.g. semicolon-chained `cursor.execute('INSERT ...'); cursor.execute(
    'DELETE ...')`, or any other shape producing two `ast.Call` write-sites
    at the same `lineno`), and `(file, line)`-only keying silently
    collapsed them to one site everywhere a site is deduplicated by
    location (`site_by_loc`, the BFS's `reached` map, and `run_census`'s
    per-root `combined` map) — the second site overwrote the first, which
    then vanished from `sites`, `pairs`, `unreached_sites`, AND
    `out_of_scope` alike, directly contradicting this tool's own
    "writes are never silently dropped" claim. `col_offset` (always present
    on a parsed `ast.Call`) disambiguates same-line sites in every one of
    those internal maps. The site's DISPLAYED id string stays `file:line`
    (no column) — see `Site.id`'s own docstring for why, and for what a
    same-line collision looks like in the output shape.

Module-level statements (top-level code, not inside any function) are
scanned the same way, for SITES only (module-level code has no "root
function" to record call-graph edges from) — this is what makes the
module-scope branch of the file:line self-rooted form (see Input format
above) meaningful: a write statement that sits directly in a hook's
top-level code, never wrapped in a function, is exactly the real shape §7
names for `engram-surface-hook.py:955` /
`engram-utility-credit-mention-stop.py:145`.

Once every file is scanned, roots are resolved (see Input format above) and
a breadth-first walk from each root's start-function(s) collects: every
function transitively reachable, and every write site owned by any of those
functions (plus, for self-rooted sites, the exact site named by the
declaration itself). THE CHECK IS SITE-LEVEL: a write site the walk never
reaches — whether or not another site sharing its (verb, table) pair was
reached by some root — is reported under `unreached_sites` (contract item
2), and the per-root sites/pairs are kept SEPARATE per root, never lumped
into one codebase-wide reachable-set (contract item 3).

Output format
=============
A single JSON object printed to stdout (this repo's other tool-output
convention — see tools/check_stubbed_signatures.py for CLI/argparse
*style*, though its own diff-scanning approach is a different mechanism
entirely and was not otherwise copied):

    {
      "known_limitations": [<4 strings — see below>],
      "per_root": {
        "<declared root string>": {
          "sites": ["<file>:<line>", ...],           // every site reached,
                                                       // in OR out of scope
          "pairs": [["<verb>", "<table>"], ...],      // IN-SCOPE pairs only
          "out_of_scope_sites": ["<file>:<line>", ...]
        },
        ...
      },
      "unreached_sites": ["<file>:<line>", ...],       // reached by NO root
      "out_of_scope": [
        {"site": "<file>:<line>", "verb": "<verb>", "table": "<table>"},
        ...
      ]
    }

`out_of_scope` is a GLOBAL list over every recognized write site in the
scanned package whose table is not in the declared table set, regardless of
whether any root reaches it (contract item 5 does not condition the
out-of-scope finding on reachability, and neither does this implementation
— an unreached out-of-scope site still appears here, exactly as an unreached
in-scope site still appears in `unreached_sites`). A site can legitimately
appear in BOTH `out_of_scope` and `unreached_sites` at once; the two facts
are orthogonal.

Known holes (contract item 7) — printed VERBATIM in every run's
`known_limitations`, so a reader of the output can never mistake a result
for complete. Quoting/paraphrasing §7's own holes (a)-(d):

  (a) the check is EXISTENTIAL-OVER-ROOTS while the deliverable is the
      per-root MAPPING.
  (b) the root set is PRESCRIBED, not CHECKED.
  (c) the site universe is scoped to `.py` files only.
  (d) the declared table set is DECLARED, not DERIVED.

None of these four holes is resolved by this build — they are §7's own open
questions, reported here rather than papered over, per the dispatch spec's
explicit scope bound ("do NOT attempt to resolve any of §7's open holes").

(Not a fifth hole, deliberately NOT added to `KNOWN_LIMITATIONS` above — a
round-2 fix CLOSED a gap that used to sit alongside these four: two write
sites sharing one physical line no longer silently collide down to one
site. See "Call-graph construction"'s note on `(file, line, col_offset)`
keying above for the mechanism, and `test_item8a_two_writes_on_one_physical_
line_both_survive` / `test_item8b_...` in the test suite for the fixtures.)

CLI
===
    python tools/write_op_census.py --package <dir> --config <config.json>

Exit 0 with the JSON result on stdout whenever every declared root resolves.
Exit 2, with a message on stderr and NOTHING on stdout, on any of: a missing
--package directory, an unreadable or malformed-JSON --config file, a
config missing the `roots`/`tables` list keys, or a root declaration that
resolves to neither a known function nor a recognized site
(`CensusInputError`).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Known holes (contract item 7) — printed verbatim in every run's output.
# ---------------------------------------------------------------------------

KNOWN_LIMITATIONS: Tuple[str, ...] = (
    "(a) EXISTENTIAL-OVER-ROOTS, not mapping-verified: this check confirms "
    "each site is reached by AT LEAST ONE root, but the deliverable is the "
    "PER-ROOT mapping -- a miss in one root's expected coverage can be "
    "masked by a DIFFERENT root's success reaching the same site. Testing "
    "the per-root mapping directly (this build's contract item 3) mitigates "
    "this by reporting reachability per root rather than as one "
    "codebase-wide union, but it does not eliminate the possibility in "
    "principle.",
    "(b) THE ROOT SET IS PRESCRIBED, NOT CHECKED: nothing in this tool "
    "detects an omitted root or surface. Garbage in, garbage out -- a "
    "caller-facing entry point left out of the declared root set silently "
    "vanishes from every finding this tool produces.",
    "(c) THE SITE UNIVERSE IS SCOPED TO .py FILES under the declared "
    "package directory. Any write surface expressed in a non-Python source "
    "(shell, SQL migration files, etc.) is invisible to this walker.",
    "(d) THE DECLARED TABLE SET IS DECLARED, NOT DERIVED: it is supplied by "
    "the caller (the --config file's `tables` key), not measured from the "
    "source. A table set that is wrong or incomplete silently reshapes "
    "every out-of-scope finding -- a missing table name makes a legitimate "
    "write look out-of-scope, and an extra table name hides a write that "
    "should have been flagged.",
)

COMPOSITE_VERB = "upsert"

_INSERT_RE = re.compile(r"\bINSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_UPDATE_RE = re.compile(r"\bUPDATE\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_DELETE_RE = re.compile(r"\bDELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_ON_CONFLICT_DO_UPDATE_RE = re.compile(
    r"\bON\s+CONFLICT\b.*?\bDO\s+UPDATE\b", re.IGNORECASE | re.DOTALL
)

_STOP_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


class CensusInputError(Exception):
    """A declared root (or a malformed config) could not be resolved."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    file: str
    line: int
    col_offset: int
    verb: str
    table: str
    in_scope: bool

    @property
    def id(self) -> str:
        # Deliberately `file:line` only, WITHOUT col_offset -- this is the
        # judgment call named in the round-2 fix's dispatch spec ("decide
        # whether Site.id's string format should change"). col_offset is
        # carried on the Site purely to disambiguate two write sites sharing
        # one physical line (see "Call-graph construction" in the module
        # docstring) in the internal dict keys (site_by_loc / reached /
        # combined) -- it is NOT surfaced in the id string, so the output
        # format and the 14 pre-existing tests' literal "file:line" string
        # assertions are unaffected by the fix. Two same-line sites can
        # therefore produce the SAME id string twice in one root's "sites"
        # list (their (verb, table) pairs still disambiguate them in
        # "pairs") -- documented, not silently arbitrary.
        return f"{self.file}:{self.line}"


@dataclass(frozen=True)
class FuncDef:
    file: str
    name: str
    lineno: int
    end_lineno: int


FuncKey = Tuple[str, str, int]  # (file, name, lineno) -- unique per definition


SiteKey = Tuple[str, int, int]  # (file, line, col_offset) -- unique per write site,
# disambiguating two write sites that share one physical line (e.g.
# semicolon-chained `cursor.execute(...); cursor.execute(...)`) -- see the
# module docstring's "Call-graph construction" section and Site.id's own
# docstring for why the DISPLAYED site id stays "file:line" while the
# internal dict key gains col_offset.


@dataclass
class ScanResult:
    functions: Dict[FuncKey, FuncDef]
    functions_by_name: Dict[str, List[FuncKey]]
    edges: Dict[FuncKey, Set[FuncKey]]
    sites_by_func: Dict[FuncKey, List[Site]]
    site_by_loc: Dict[SiteKey, Site]


# ---------------------------------------------------------------------------
# Statement classification
# ---------------------------------------------------------------------------


def _classify_statement(sql: str) -> Optional[Tuple[str, str]]:
    """Return (verb, table) for a recognized statement text, else None. See
    module docstring's "Write-statement / (verb, table) detection" section
    for the exact rule and ordering."""
    m_insert = _INSERT_RE.search(sql)
    if m_insert:
        table = m_insert.group(1)
        if _ON_CONFLICT_DO_UPDATE_RE.search(sql):
            return (COMPOSITE_VERB, table)
        return ("INSERT", table)
    m_update = _UPDATE_RE.search(sql)
    if m_update:
        return ("UPDATE", m_update.group(1))
    m_delete = _DELETE_RE.search(sql)
    if m_delete:
        return ("DELETE", m_delete.group(1))
    return None


# ---------------------------------------------------------------------------
# AST body scanning (shared by module-level and function-level scans)
# ---------------------------------------------------------------------------


def _direct_body_nodes(node: ast.AST):
    """Yield every descendant of `node` that is NOT inside a nested function
    definition. A child that IS a FunctionDef/AsyncFunctionDef is skipped
    entirely (never yielded, never descended into) -- that nested function's
    own body is scanned separately, as its own entry, when `ast.walk` in
    `_scan_package` reaches it directly. This is what keeps a function's
    direct-call/write-site scan from double-counting (or misattributing) a
    nested function's own calls/sites."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _STOP_TYPES):
            continue
        yield child
        yield from _direct_body_nodes(child)


def _scan_node_body(
    node: ast.AST, relfile: str, declared_tables: Set[str]
) -> Tuple[Set[str], List[Site]]:
    """Scan `node`'s direct body (module or function) for bare-name call
    edges and `.execute(...)` write sites. See module docstring's
    "Call-graph construction" section."""
    calls: Set[str] = set()
    sites: List[Site] = []
    for child in _direct_body_nodes(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            calls.add(func.id)
        elif isinstance(func, ast.Attribute) and func.attr == "execute":
            if (
                child.args
                and isinstance(child.args[0], ast.Constant)
                and isinstance(child.args[0].value, str)
            ):
                classified = _classify_statement(child.args[0].value)
                if classified is not None:
                    verb, table = classified
                    sites.append(
                        Site(
                            file=relfile,
                            line=child.lineno,
                            col_offset=child.col_offset,
                            verb=verb,
                            table=table,
                            in_scope=table in declared_tables,
                        )
                    )
    return calls, sites


# ---------------------------------------------------------------------------
# Package scan
# ---------------------------------------------------------------------------


def _relpath(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _scan_package(package_dir: Path, declared_tables: Set[str]) -> ScanResult:
    functions: Dict[FuncKey, FuncDef] = {}
    functions_by_name: Dict[str, List[FuncKey]] = defaultdict(list)
    direct_calls: Dict[FuncKey, Set[str]] = defaultdict(set)
    sites_by_func: Dict[FuncKey, List[Site]] = defaultdict(list)
    site_by_loc: Dict[SiteKey, Site] = {}

    for path in sorted(package_dir.rglob("*.py")):
        relfile = _relpath(path, package_dir)
        source = path.read_text()
        tree = ast.parse(source, filename=relfile)

        # Module-level (top-level, not-inside-any-function) write sites --
        # sites only; module-level calls form no function-keyed edge.
        _module_calls, module_sites = _scan_node_body(tree, relfile, declared_tables)
        for site in module_sites:
            site_by_loc[(site.file, site.line, site.col_offset)] = site

        for node in ast.walk(tree):
            if not isinstance(node, _STOP_TYPES):
                continue
            fkey: FuncKey = (relfile, node.name, node.lineno)
            end_lineno = getattr(node, "end_lineno", None) or node.lineno
            functions[fkey] = FuncDef(relfile, node.name, node.lineno, end_lineno)
            functions_by_name[node.name].append(fkey)
            calls, sites = _scan_node_body(node, relfile, declared_tables)
            direct_calls[fkey] = calls
            sites_by_func[fkey] = sites
            for site in sites:
                site_by_loc[(site.file, site.line, site.col_offset)] = site

    edges: Dict[FuncKey, Set[FuncKey]] = defaultdict(set)
    for fkey, called_names in direct_calls.items():
        for name in called_names:
            for target_key in functions_by_name.get(name, ()):
                edges[fkey].add(target_key)

    return ScanResult(
        functions=functions,
        functions_by_name=dict(functions_by_name),
        edges=dict(edges),
        sites_by_func=dict(sites_by_func),
        site_by_loc=site_by_loc,
    )


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------


def _find_enclosing_function(
    file_part: str, line: int, functions: Dict[FuncKey, FuncDef]
) -> Optional[FuncKey]:
    candidates = [
        key
        for key, fdef in functions.items()
        if fdef.file == file_part and fdef.lineno <= line <= fdef.end_lineno
    ]
    if not candidates:
        return None
    # innermost = smallest span
    candidates.sort(key=lambda key: functions[key].end_lineno - functions[key].lineno)
    return candidates[0]


def _find_site_by_line(
    file_part: str, line: int, site_by_loc: Dict[SiteKey, Site]
) -> Optional[Site]:
    """Resolve a self-rooted `file:line` declaration (Input format's
    "LINE NUMBER" form) against `site_by_loc`, whose keys carry col_offset
    (see SiteKey) for the same-line-collision fix -- but a self-rooted
    declaration string names only a LINE, never a column, so this looks up
    by (file, line) alone across every col_offset at that line.

    JUDGMENT CALL (round-2 fix, not exercised by any fixture -- no test
    combines a self-rooted declaration with a same-line collision): if more
    than one write site shares the declared line (e.g. two semicolon-chained
    `.execute(...)` calls self-rooted by their shared line number), this
    deterministically returns the LEFTMOST one (smallest col_offset) rather
    than raising or silently picking whichever happened to be inserted last
    into a dict (the pre-fix bug this whole round-2 fix removes). The
    module docstring's Input format section documents this tie-break."""
    candidates = [
        site
        for (f, l, _c), site in site_by_loc.items()
        if f == file_part and l == line
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda s: s.col_offset)
    return candidates[0]


def _resolve_root(
    root_decl: str, scan: ScanResult
) -> Tuple[Set[FuncKey], List[Site]]:
    """Returns (start_funcs, extra_sites) for one declared root string.
    `extra_sites` are sites credited to this root with no further call-graph
    traversal (the module-scope, no-enclosing-function branch of a
    self-rooted file:line declaration). Raises CensusInputError if the
    declaration resolves to neither a function nor a recognized site."""
    if ":" not in root_decl:
        raise CensusInputError(
            f"malformed root declaration (expected 'file:name' or "
            f"'file:line'): {root_decl!r}"
        )
    file_part, _, rest = root_decl.rpartition(":")
    if rest.isdigit():
        line = int(rest)
        enclosing = _find_enclosing_function(file_part, line, scan.functions)
        if enclosing is not None:
            return {enclosing}, []
        site = _find_site_by_line(file_part, line, scan.site_by_loc)
        if site is not None:
            return set(), [site]
        raise CensusInputError(
            f"declared root {root_decl!r} does not resolve to a known "
            "function span or a recognized write site at that exact line"
        )
    name = rest
    matches = [
        key for key in scan.functions if key[0] == file_part and key[1] == name
    ]
    if not matches:
        raise CensusInputError(
            f"declared root {root_decl!r} does not resolve to any function "
            f"named {name!r} in {file_part!r}"
        )
    return set(matches), []


# ---------------------------------------------------------------------------
# BFS
# ---------------------------------------------------------------------------


def _bfs_reach(
    start_funcs: Set[FuncKey],
    edges: Dict[FuncKey, Set[FuncKey]],
    sites_by_func: Dict[FuncKey, List[Site]],
) -> List[Site]:
    visited: Set[FuncKey] = set()
    queue = deque(start_funcs)
    reached: Dict[SiteKey, Site] = {}
    while queue:
        f = queue.popleft()
        if f in visited:
            continue
        visited.add(f)
        for site in sites_by_func.get(f, ()):
            reached[(site.file, site.line, site.col_offset)] = site
        for nxt in edges.get(f, ()):
            if nxt not in visited:
                queue.append(nxt)
    return list(reached.values())


# ---------------------------------------------------------------------------
# Top-level census
# ---------------------------------------------------------------------------


def run_census(package_dir: Path, roots: List[str], tables: List[str]) -> dict:
    """Run the full call-path write-operation census. See module docstring
    for the input/output contract. Raises CensusInputError if any declared
    root fails to resolve."""
    declared_tables = set(tables)
    scan = _scan_package(package_dir, declared_tables)

    per_root: Dict[str, dict] = {}
    reached_locs: Set[SiteKey] = set()

    for root_decl in roots:
        start_funcs, extra_sites = _resolve_root(root_decl, scan)
        reached_sites = _bfs_reach(start_funcs, scan.edges, scan.sites_by_func)
        combined: Dict[SiteKey, Site] = {
            (s.file, s.line, s.col_offset): s for s in reached_sites
        }
        for s in extra_sites:
            combined[(s.file, s.line, s.col_offset)] = s
        ordered = sorted(combined.values(), key=lambda s: (s.file, s.line, s.col_offset))
        in_scope_pairs = sorted({(s.verb, s.table) for s in ordered if s.in_scope})
        out_of_scope_sites = [s for s in ordered if not s.in_scope]
        per_root[root_decl] = {
            "sites": [s.id for s in ordered],
            "pairs": [list(p) for p in in_scope_pairs],
            "out_of_scope_sites": [s.id for s in out_of_scope_sites],
        }
        reached_locs.update(combined.keys())

    all_sites = list(scan.site_by_loc.values())
    unreached_sites = sorted(
        s.id for s in all_sites if (s.file, s.line, s.col_offset) not in reached_locs
    )
    out_of_scope_global = sorted(
        (
            {"site": s.id, "verb": s.verb, "table": s.table}
            for s in all_sites
            if not s.in_scope
        ),
        key=lambda d: d["site"],
    )

    return {
        "known_limitations": list(KNOWN_LIMITATIONS),
        "per_root": per_root,
        "unreached_sites": unreached_sites,
        "out_of_scope": out_of_scope_global,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Walk call paths from declared write-entry-point roots and "
            "report every database write site (file:line) each root "
            "reaches, per-root, at the site level (#3095)."
        )
    )
    parser.add_argument(
        "--package",
        required=True,
        metavar="DIR",
        help="Root directory of the source package to scan (recursive *.py glob).",
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="FILE",
        help='JSON file declaring {"roots": [...], "tables": [...]}. See module docstring.',
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    package_dir = Path(args.package)
    if not package_dir.is_dir():
        print(
            f"write_op_census: error -- package directory not found: {package_dir}",
            file=sys.stderr,
        )
        return 2

    config_path = Path(args.config)
    try:
        config_text = config_path.read_text()
    except OSError as exc:
        print(
            f"write_op_census: error -- cannot read config {config_path}: {exc}",
            file=sys.stderr,
        )
        return 2

    try:
        config = json.loads(config_text)
    except json.JSONDecodeError as exc:
        print(
            f"write_op_census: error -- malformed JSON config {config_path}: {exc}",
            file=sys.stderr,
        )
        return 2

    roots = config.get("roots", [])
    tables = config.get("tables", [])
    if not isinstance(roots, list) or not isinstance(tables, list):
        print(
            "write_op_census: error -- config must declare 'roots' and "
            "'tables' as JSON lists",
            file=sys.stderr,
        )
        return 2

    try:
        result = run_census(package_dir, roots, tables)
    except CensusInputError as exc:
        print(f"write_op_census: error -- {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
