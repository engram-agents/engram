"""Drift guard: no bare `json.loads` of a node-metadata value outside
`_metadata_dict` unless it is on a named allow-list (#3413, ratchet for #3425).

Why. `nodes.metadata` is TEXT holding JSON. A valid-JSON NON-object blob
(`[1,2]`, `"str"`, `5`, `null`) parses fine and then crashes every
`.get(...)` / `[k]` / `in` that follows. The root-cause fix is one helper,
`engram_core._metadata_dict(raw) -> dict`, that always returns a dict. This
test makes the bad state hard to reintroduce: any `json.loads` / `json.load`
over a metadata value under `src/engram/` must either be inside the helper or
be an ALLOW-LIST entry with a reason and an issue reference.

What the scan flags. An AST walk of every `.py` under `src/engram/` (tests are
not under it) AND every extensionless python script there: any file with no
`.py` suffix whose first line is a python shebang (`#!/usr/bin/env python3`,
`#!/usr/bin/python3`, ...), e.g. `tools/engram-pkg/engram-pkg` (the original
`*.py` glob missed it, reviewer round 1 of #3423). A `json.loads(X)` / `json.load(X)` call is a metadata parse when
X mentions a name, attribute or string containing `meta` (`meta_blob`,
`row["metadata"]`, `lmeta_json`, ...), or X is a bare Name assigned (one hop,
same function) from an expression that mentions `meta`, or X is a Name /
`Name[...]` assigned from an expression holding a string constant that
mentions `meta` (the `SELECT metadata ...` shape).

Ratchet. Each allow-list entry is `(relative path, enclosing function)` with
an exact site COUNT, an issue ref and a one-line reason:
  * a site not covered by the helper or an entry fails the guard (new drift);
  * an entry whose count no longer matches the tree fails the stale-entry test
    (a routed site must delete or lower its entry; an extra site inside an
    allow-listed function fails too, so an entry cannot hide a new one);
  * the number of `#3425` SITES may only shrink: `_MAX_3425_SITES` is asserted
    equal to the allow-list's count (and `<=` the ceiling), so it must be
    LOWERED whenever sites are routed. What is left under `#3425` is dev
    tooling and one fail-open hook write path; the runtime read AND write
    paths are routed (reads through `_metadata_dict`, writes through
    `_metadata_dict_for_write`, which logs and warns when it replaces a
    non-object blob).
  * a `LOCAL`-tagged entry is a site routed WITHOUT `engram_core`: standalone
    scripts, hooks and the viz layer duplicate the 2-line primitive by
    convention rather than import across the process boundary. It is valid
    only if, for every flagged parse `N = json.loads(...)`, the enclosing
    function's own scope (not a nested def / lambda / class) has an `if` AFTER
    the parse and before any later re-assignment of N, reachable (not
    `if False:`), whose test holds `isinstance(N, dict)` (directly or under
    `not`) and which re-assigns N in its body or orelse (AST check,
    `local_guard_problems`); an `except` handler of the try holding the parse is
    an alternative path, not a re-assignment. LOCAL sites are not counted in
    the `#3425` ceiling.

What the guard CANNOT catch (so a green guard is not "the class is closed"):
  * aliased parse: `from json import loads` / `import json as j` /
    `loads = json.loads`, or any dynamic json module;
  * metadata read through a helper other than `_metadata_dict` or a wrapper
    whose argument carries no `meta` name (e.g. `parse(row[0])` where `row`
    came from a query the scan cannot see, or a loop target over such rows);
  * a parse whose argument is built across more than one hop;
  * other serialisers (`ast.literal_eval`, `orjson`, `yaml`);
  * a call that routes through `_metadata_dict` but then assumes the dict has
    particular value TYPES (`meta["aliases"]` being a list, etc.): that is a
    different defect class.
"""

import ast
import pathlib

import pytest

SRC_ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "engram"

# The places a bare metadata parse is the point: the read helper and its
# write-side twin. (The write twin parses a variable named `raw` today, which
# the scan does not even flag; it is exempt so that renaming it to `meta_raw`
# cannot false-positive.)
HELPER = {
    ("engram_core.py", "_metadata_dict"),
    ("engram_core.py", "_metadata_dict_for_write"),
}

# (relative path under src/engram, enclosing function) ->
#   (exact site count, issue/class tag, one-line reason)
# Tag meaning: "#3425" = a parse site still to be routed (write paths are
# parse-mutate-re-dump, so what to STORE BACK for a non-object blob is a
# decision of its own); "S" = already guarded / caught, deliberately left;
# "RAW" = the raw parsed value is the product, never dict-accessed;
# "LOCAL" = routed locally with an `if ...isinstance(<parsed>, dict)...:` guard
# that re-assigns the parsed name, after the parse in the same function scope
# (verified by the AST check), for code that does not import engram_core by
# convention.
ALLOW_LIST = {
    # (The runtime write paths are all routed through
    # `_metadata_dict_for_write` / `_metadata_dict`: no entry left for them.)
    # ---- still to be routed (#3425) ----------------------------------------
    ("hooks/claude/engram-surface-hook.py", "_advance_person_moment_cursor"):
        (1, "#3425", "hook write path (cursor re-dump), wrapped in `except Exception` so it FAILS OPEN (returns False); engram_core is importable here (the hook imports it lazily elsewhere), the local parse follows the convention of duplicating small primitives across the hook/server process boundary"),
    ("tools/inspect_raw.py", "_get_field_value"):
        (2, "#3425", "dev tooling: one `.get` read plus one deliberate raw `parsed_metadata` return"),
    ("tools/migrate_person_nodes.py", "run_report"):
        (1, "#3425", "dev tooling: one-time migration report"),
    ("tools/surgical.py", "_compute_confidence_for_node"):
        (2, "#3425", "dev tooling: DB-surgery confidence recompute (source_class / reasoning_type reads)"),
    ("tools/surgical.py", "metadata_patch"):
        (1, "#3425", "dev tooling: DB-surgery metadata patch (parse, merge, re-dump)"),
    # ---- routed locally (no engram_core import by convention) --------------
    ("hooks/claude/engram-user-identity-hook.py", "detect_speaker"):
        (1, "LOCAL", "routed locally: isinstance guard (hook/viz cannot assume engram_core on sys.path by convention)"),
    ("viz/data/calibration.py", "_conf_dist_for_window"):
        (2, "LOCAL", "routed locally: isinstance guard (hook/viz cannot assume engram_core on sys.path by convention)"),
    ("tools/engram-pkg/engram-pkg", "_render_origin"):
        (1, "LOCAL", "routed locally: isinstance guard (standalone script, no engram_core import by convention)"),
    # ---- deliberate, not to be routed --------------------------------------
    ("engram_core.py", "_legacy_roots"):
        (1, "S", "guarded: `isinstance(m, dict)` check and a ValueError/TypeError catch right after the parse"),
    ("engram_query.py", "_inspect_impl"):
        (1, "RAW", "`parsed_metadata` is stored raw, never dict-accessed, and stripped from the output"),
    ("viz/data/graph.py", "_index_node"):
        (1, "S", "guarded: `isinstance(meta_obj, dict)` check right after the parse"),
}

# The ratchet. Count of SITES carrying the "#3425" tag. LOWER this number
# (never raise it) when a PR routes sites and deletes/lowers their entries.
_MAX_3425_SITES = 7


# --------------------------------------------------------------------------
# the scanner (pure; parametrised on a root so it can be run on scratch trees)
# --------------------------------------------------------------------------

def _mentions_meta(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and "meta" in n.id.lower():
            return True
        if isinstance(n, ast.Attribute) and "meta" in n.attr.lower():
            return True
        if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "meta" in n.value.lower()):
            return True
    return False


def _mentions_meta_string(node):
    return any(
        isinstance(n, ast.Constant) and isinstance(n.value, str)
        and "meta" in n.value.lower()
        for n in ast.walk(node)
    )


def _is_json_load(call):
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr in ("loads", "load")
            and isinstance(f.value, ast.Name) and f.value.id == "json")


def _is_python_script(path):
    """An extensionless file whose first line is a python shebang."""
    if path.suffix or not path.is_file() or path.is_symlink():
        return False
    try:
        with open(path, "rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def _scan_files(root):
    root = pathlib.Path(root)
    py = [p for p in root.rglob("*.py")]
    scripts = [p for p in root.rglob("*") if _is_python_script(p)]
    return sorted(py + scripts)


def find_metadata_parse_sites(root):
    """Return [(relative posix path, line, enclosing function name)]."""
    root = pathlib.Path(root)
    out = []
    for path in _scan_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(root).as_posix()

        def walk_scope(scope, fname):
            nodes = []

            def collect(n):
                for child in ast.iter_child_nodes(n):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        walk_scope(child, child.name)
                        continue
                    nodes.append(child)
                    collect(child)

            collect(scope)
            tainted_name, tainted_sql = set(), set()
            for n in nodes:
                if isinstance(n, ast.Assign):
                    names = {x.id for t in n.targets for x in ast.walk(t)
                             if isinstance(x, ast.Name)}
                    if _mentions_meta(n.value):
                        tainted_name |= names
                    if _mentions_meta_string(n.value):
                        tainted_sql |= names
            for n in nodes:
                if not (isinstance(n, ast.Call) and _is_json_load(n) and n.args):
                    continue
                arg = n.args[0]
                root_name = arg.value if isinstance(arg, ast.Subscript) else arg
                hit = (
                    _mentions_meta(arg)
                    or (isinstance(arg, ast.Name) and arg.id in tainted_name)
                    or (isinstance(root_name, ast.Name) and root_name.id in tainted_sql)
                )
                if hit:
                    out.append((rel, n.lineno, fname))

        walk_scope(tree, "<module>")
    return out


def unaccounted_sites(sites, allow_list=ALLOW_LIST, helper=HELPER):
    """Sites that are neither inside the helper nor covered by an entry."""
    return [s for s in sites
            if (s[0], s[2]) not in helper and (s[0], s[2]) not in allow_list]


def entry_mismatches(sites, allow_list=ALLOW_LIST):
    """Entries whose exact count does not match the tree: [(key, expected, found)]."""
    found = {}
    for rel, _line, fn in sites:
        found[(rel, fn)] = found.get((rel, fn), 0) + 1
    return [(key, spec[0], found.get(key, 0))
            for key, spec in allow_list.items() if found.get(key, 0) != spec[0]]


_SCOPE_BREAKS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _scope_stmts(body):
    """Statements of ONE function scope, in source order, descending into
    compound statements but never into a nested def / lambda / class, and never
    into the body of an `if` whose test is the constant False / 0 / None."""
    for st in body:
        if isinstance(st, _SCOPE_BREAKS):
            continue
        yield st
        if isinstance(st, ast.If):
            dead = isinstance(st.test, ast.Constant) and not st.test.value
            if not dead:
                yield from _scope_stmts(st.body)
            yield from _scope_stmts(st.orelse)
            continue
        for field in ("body", "orelse", "finalbody"):
            yield from _scope_stmts(getattr(st, field, None) or [])
        for h in getattr(st, "handlers", None) or []:
            yield from _scope_stmts(h.body)
        for c in getattr(st, "cases", None) or []:
            yield from _scope_stmts(c.body)


def _tests_isinstance_dict(test, name):
    """`isinstance(name, dict)` appears in an `if` test (directly or under `not`)."""
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "isinstance" and len(n.args) == 2
        and isinstance(n.args[0], ast.Name) and n.args[0].id == name
        and isinstance(n.args[1], ast.Name) and n.args[1].id == "dict"
        for n in ast.walk(test))


def _assigns_name(stmts, name):
    return any(isinstance(s, ast.Assign) and len(s.targets) == 1
               and isinstance(s.targets[0], ast.Name) and s.targets[0].id == name
               for s in _scope_stmts(stmts))


def _isinstance_dict_sense(test, name):
    """`"neg"` for exactly `not isinstance(name, dict)`, `"pos"` for exactly
    `isinstance(name, dict)`, else None."""
    neg = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
    inner = test.operand if neg else test
    if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
            and inner.func.id == "isinstance" and len(inner.args) == 2
            and not inner.keywords
            and isinstance(inner.args[0], ast.Name) and inner.args[0].id == name
            and isinstance(inner.args[1], ast.Name) and inner.args[1].id == "dict"):
        return "neg" if neg else "pos"
    return None


def _assigns_dict_literal(stmts, name):
    """`name = <dict literal>` (e.g. `{}`) somewhere in `stmts` (same scope)."""
    return any(isinstance(s, ast.Assign) and len(s.targets) == 1
               and isinstance(s.targets[0], ast.Name) and s.targets[0].id == name
               and isinstance(s.value, ast.Dict)
               for s in _scope_stmts(stmts))


def _header_exprs(st):
    """The expressions a compound statement evaluates BEFORE its body runs."""
    if isinstance(st, ast.If) or isinstance(st, ast.While):
        return [st.test]
    if isinstance(st, (ast.For, ast.AsyncFor)):
        return [st.target, st.iter]
    if isinstance(st, (ast.With, ast.AsyncWith)):
        return list(st.items)
    if isinstance(st, ast.Match):
        return [st.subject]
    return []


def _stores_name(st, name):
    """`st` (not descending into nested scopes) stores to `name`."""
    stack = [st]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store):
            return True
        stack.extend(c for c in ast.iter_child_nodes(n)
                     if not isinstance(c, _SCOPE_BREAKS))
    return False


def _guard_problem(fn_node, assign, name):
    """None if the parse `assign` (to plain `name`) is followed, in the same
    function scope and before any re-assignment of `name`, by an `if` whose test
    holds `isinstance(name, dict)` and which re-assigns `name` in its body or
    orelse; else a `why` string."""
    after = assign.end_lineno
    # The `except` handlers of a `try` that holds the parse are ALTERNATIVE
    # paths (`try: meta = json.loads(..) / except ValueError: meta = {}`), not
    # re-assignments that follow the parse: they do not end the guard window.
    alt = set()
    for t in _scope_stmts(fn_node.body):
        if isinstance(t, ast.Try) and any(
                n is assign for b in t.body for n in ast.walk(b)):
            for h in t.handlers:
                alt.update(id(x) for x in _scope_stmts(h.body))
    stmts = sorted((s for s in _scope_stmts(fn_node.body)
                    if s.lineno > after and id(s) not in alt),
                   key=lambda s: s.lineno)
    saw_check = False
    wrong = None
    for st in stmts:
        if (isinstance(st, ast.If) and _tests_isinstance_dict(st.test, name)):
            saw_check = True
            sense = _isinstance_dict_sense(st.test, name)
            if sense is None:
                wrong = wrong or ("the guard test must be exactly `isinstance(%s, dict)` "
                                  "or `not isinstance(%s, dict)`" % (name, name))
            else:
                arm, arm_name = ((st.body, "body") if sense == "neg"
                                 else (st.orelse, "else"))
                if _assigns_dict_literal(arm, name):
                    return None
                wrong = wrong or (
                    "`%s` must be assigned a dict literal (`%s = {}`) in the %s of "
                    "`%sisinstance(%s, dict)` (line %d)"
                    % (name, name, arm_name, "not " if sense == "neg" else "", name, st.lineno))
        # a re-assignment of `name` that is not the guard ends the window
        # (statement-level only: stores nested in a compound stmt are visited
        # as their own statements, so check simple statements here; a compound
        # statement's own HEADER, e.g. a walrus in an `if` test, is checked too)
        if isinstance(st, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With,
                           ast.AsyncWith, ast.Try, ast.Match)):
            stored = any(_stores_name(h, name) for h in _header_exprs(st))
        else:
            stored = _stores_name(st, name)
        if stored:
            return ("`%s` is re-assigned (line %d) before any guard" % (name, st.lineno)
                    if not saw_check else
                    wrong or "the `isinstance(%s, dict)` test does not re-assign `%s`" % (name, name))
    if saw_check:
        return wrong or "the `isinstance(%s, dict)` test never re-assigns `%s` (needs `%s = {}`)" % (name, name, name)
    return ("no reachable `if` after this parse, in the same function scope, tests "
            "`isinstance(%s, dict)` and re-assigns `%s`" % (name, name))


def local_guard_problems(sites, root=SRC_ROOT, allow_list=ALLOW_LIST):
    """For every LOCAL entry: each flagged parse must be assigned to a plain
    name N AND be followed, in the SAME function scope (not a nested def /
    lambda / class) and before any later re-assignment of N (a walrus in an
    `if` header counts), by a reachable `if` (test not the constant False / 0 /
    None) that matches its arm: `if not isinstance(N, dict): N = {}` (dict
    literal in the BODY) or `if isinstance(N, dict): ... else: N = {}` (dict
    literal in the ORELSE). Returns [(rel, function, line, why)].

    NOT modelled: reachability beyond a constant-falsy `if` body (`while False`,
    `for _ in []`, the else-arm of `if True`, code after `return`, a guard under
    a conditional that only runs on one branch), and the ternary shape
    `N = N if isinstance(N, dict) else {}` (rejected: it has no `if`
    statement; pinned by a test)."""
    root = pathlib.Path(root)
    problems = []
    for (rel, fn), spec in allow_list.items():
        if spec[1] != "LOCAL":
            continue
        lines = {line for r, line, f in sites if (r, f) == (rel, fn)}
        path = root / rel
        if not path.is_file():
            problems.append((rel, fn, 0, "file missing"))
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        fns = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn]
        if not fns:
            problems.append((rel, fn, 0, "function missing"))
            continue
        for line in sorted(lines):
            whys = []
            ok = False
            for f in fns:
                for a in _scope_stmts(f.body):
                    if not (isinstance(a, ast.Assign) and len(a.targets) == 1
                            and isinstance(a.targets[0], ast.Name)
                            and any(isinstance(c, ast.Call) and _is_json_load(c)
                                    and c.lineno == line for c in ast.walk(a.value))):
                        continue
                    why = _guard_problem(f, a, a.targets[0].id)
                    if why is None:
                        ok = True
                    else:
                        whys.append(why)
            if not ok:
                problems.append((rel, fn, line, whys[0] if whys else
                                 "this parse is not assigned to a plain name in the "
                                 "function's own scope, so no guard can be tied to it"))
    return problems


def count_3425_sites(allow_list=ALLOW_LIST):
    return sum(spec[0] for spec in allow_list.values() if spec[1] == "#3425")


# --------------------------------------------------------------------------
# the real tree
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_sites():
    sites = find_metadata_parse_sites(SRC_ROOT)
    assert sites, "scanner found no sites at all: the scan itself is broken"
    return sites


def test_no_unrouted_metadata_parse(real_sites):
    bad = unaccounted_sites(real_sites)
    assert not bad, (
        "bare json.loads of a node-metadata value outside _metadata_dict:\n"
        + "\n".join(
            f"  {rel}:{line} in {fn}()  ->  use engram_core._metadata_dict(raw), "
            "or add an ALLOW_LIST entry in tests/test_metadata_parse_drift_guard_3413.py "
            "with a one-line reason and an issue ref"
            for rel, line, fn in bad
        )
    )


def test_allow_list_has_no_stale_or_hiding_entries(real_sites):
    bad = entry_mismatches(real_sites)
    assert not bad, (
        "allow-list entries whose site count no longer matches the tree "
        "(routed a site? delete or lower the entry; added one inside an "
        "allow-listed function? route it through _metadata_dict instead):\n"
        + "\n".join(f"  {k}: entry says {exp}, tree has {got}" for k, exp, got in bad)
    )


def test_helper_exemption_targets_exist():
    """Every HELPER exemption must point at a function that still exists (their
    own parses name no `meta` variable, so the scan does not even see them
    today; the exemption is for the day an argument is renamed)."""
    for rel, name in sorted(HELPER):
        tree = ast.parse((SRC_ROOT / rel).read_text(encoding="utf-8"))
        assert any(isinstance(n, ast.FunctionDef) and n.name == name
                   for n in ast.walk(tree)), (rel, name)


def test_local_entries_carry_an_isinstance_dict_guard(real_sites):
    bad = local_guard_problems(real_sites)
    assert not bad, (
        "LOCAL allow-list entries need an `if` testing `isinstance(<parsed>, dict)` "
        "that re-assigns the name, after the parse in the same function scope:\n"
        + "\n".join(f"  {r}:{line} in {f}(): {why}" for r, f, line, why in bad)
    )


def test_3425_ratchet_only_shrinks():
    n = count_3425_sites()
    assert n <= _MAX_3425_SITES, (
        f"{n} '#3425' sites on the allow-list, ceiling is {_MAX_3425_SITES}: "
        "the list may only shrink; route the site through _metadata_dict instead"
    )
    assert n == _MAX_3425_SITES, (
        f"{n} '#3425' sites but the ceiling is still {_MAX_3425_SITES}: sites "
        "were routed, so LOWER _MAX_3425_SITES to the new count"
    )


def test_every_entry_has_reason_and_issue_tag():
    for key, (count, tag, reason) in ALLOW_LIST.items():
        assert count >= 1, key
        assert tag in ("#3425", "S", "RAW", "LOCAL"), (key, tag)
        assert reason.strip(), key


# --------------------------------------------------------------------------
# the scanner itself, on scratch trees (so the guard is known to bite)
# --------------------------------------------------------------------------

def _scratch(tmp_path, src, name="mod.py"):
    (tmp_path / name).write_text(src, encoding="utf-8")
    return find_metadata_parse_sites(tmp_path)


def test_scanner_flags_a_bare_row_metadata_parse(tmp_path):
    sites = _scratch(tmp_path, (
        "import json\n"
        "def f(row):\n"
        "    return json.loads(row['metadata']).get('x')\n"
    ))
    assert sites == [("mod.py", 3, "f")]
    assert unaccounted_sites(sites) == sites


def test_scanner_flags_one_hop_taint_and_sql_shape(tmp_path):
    sites = _scratch(tmp_path, (
        "import json\n"
        "def f(n):\n"
        "    blob = n.metadata_raw\n"
        "    return json.loads(blob)\n"
        "def g(conn):\n"
        "    row = conn.execute('SELECT metadata FROM nodes').fetchone()\n"
        "    return json.loads(row[0])\n"
    ))
    assert [(s[1], s[2]) for s in sites] == [(4, "f"), (7, "g")]


def test_scanner_ignores_unrelated_json_and_names_the_innermost_function(tmp_path):
    sites = _scratch(tmp_path, (
        "import json\n"
        "def f(row):\n"
        "    json.loads(row['embedding'])\n"
        "    def inner(r):\n"
        "        return json.loads(r['metadata'])\n"
    ))
    assert sites == [("mod.py", 5, "inner")]


def test_a_stale_entry_and_a_hidden_extra_site_are_both_reported(tmp_path):
    sites = _scratch(tmp_path, (
        "import json\n"
        "def f(row):\n"
        "    a = json.loads(row['metadata'])\n"
        "    b = json.loads(row['metadata'])\n"
    ))
    allow = {("mod.py", "f"): (1, "#3425", "r"), ("mod.py", "gone"): (1, "S", "r")}
    got = {k: (e, g) for k, e, g in entry_mismatches(sites, allow)}
    assert got == {("mod.py", "f"): (1, 2), ("mod.py", "gone"): (1, 0)}


def test_scanner_scans_extensionless_python_scripts(tmp_path):
    (tmp_path / "tool").write_text(
        "#!/usr/bin/env python3\nimport json\n"
        "def f(row):\n    return json.loads(row['metadata']).get('x')\n",
        encoding="utf-8")
    (tmp_path / "other").write_text(  # not python: not scanned
        "#!/bin/sh\necho json.loads(row['metadata'])\n", encoding="utf-8")
    (tmp_path / "plain").write_text(  # no shebang: not scanned
        "import json\njson.loads(row['metadata'])\n", encoding="utf-8")
    assert find_metadata_parse_sites(tmp_path) == [("tool", 4, "f")]
    assert unaccounted_sites(find_metadata_parse_sites(tmp_path))


def test_real_engram_pkg_script_is_scanned(real_sites):
    assert any(r == "tools/engram-pkg/engram-pkg" for r, _l, _f in real_sites)


def test_local_entry_requires_isinstance_dict_on_the_parsed_name(tmp_path):
    good = (
        "import json\n"
        "def f(row):\n"
        "    meta = json.loads(row['metadata'])\n"
        "    if not isinstance(meta, dict):\n"
        "        meta = {}\n"
        "    return meta.get('x')\n"
    )
    bad = good.replace("    if not isinstance(meta, dict):\n        meta = {}\n", "")
    wrong_name = good.replace("isinstance(meta, dict)", "isinstance(other, dict)")
    allow = {("mod.py", "f"): (1, "LOCAL", "r")}
    for src, expect_ok in ((good, True), (bad, False), (wrong_name, False)):
        sites = _scratch(tmp_path, src)
        got = local_guard_problems(sites, root=tmp_path, allow_list=allow)
        assert (got == []) is expect_ok, (src, got)


def test_bare_parse_added_to_a_copy_of_engram_pkg_is_flagged(tmp_path):
    """Mutation as a standing test: a bare metadata parse appended to a scratch
    copy of the real extensionless script is reported, by name."""
    real = SRC_ROOT / "tools" / "engram-pkg" / "engram-pkg"
    dest = tmp_path / "tools" / "engram-pkg"
    dest.mkdir(parents=True)
    (dest / "engram-pkg").write_text(
        real.read_text(encoding="utf-8")
        + "\n\ndef _scratch_bare(row):\n    return json.loads(row['metadata']).get('k')\n",
        encoding="utf-8")
    bad = unaccounted_sites(find_metadata_parse_sites(tmp_path))
    assert [(r, f) for r, _l, f in bad] == [("tools/engram-pkg/engram-pkg", "_scratch_bare")]


def test_renaming_the_write_helpers_raw_variable_does_not_false_positive(tmp_path):
    """Friction fix: `_metadata_dict_for_write`'s own parse is exempt, so
    renaming its `raw` argument to `meta_raw` (which the scan would flag as a
    metadata parse) keeps the guard green. A scratch copy of engram_core.py."""
    src = (SRC_ROOT / "engram_core.py").read_text(encoding="utf-8")
    start = src.index("def _metadata_dict_for_write(")
    end = src.index("\ndef ", start + 10)
    body = src[start:end]
    assert "raw" in body
    renamed = src[:start] + body.replace("raw", "meta_raw") + src[end:]
    (tmp_path / "engram_core.py").write_text(renamed, encoding="utf-8")
    sites = find_metadata_parse_sites(tmp_path)
    assert ("engram_core.py", "_metadata_dict_for_write") in {(r, f) for r, _l, f in sites}, \
        "the rename should make the scan SEE the parse (else this test proves nothing)"
    assert [s for s in unaccounted_sites(sites)
            if s[2] == "_metadata_dict_for_write"] == []
    # control: the same renamed parse in a non-exempt function IS flagged
    (tmp_path / "engram_core.py").write_text(
        renamed.replace("def _metadata_dict_for_write(", "def _metadata_dict_for_write_x("),
        encoding="utf-8")
    flagged = [s for s in unaccounted_sites(find_metadata_parse_sites(tmp_path))
               if s[2] == "_metadata_dict_for_write_x"]
    assert flagged


# --------------------------------------------------------------------------
# the LOCAL guard check, tightened (#3425 follow-up 1): the guard must be an
# `if` AFTER the parse, before any re-assignment, whose test holds
# `isinstance(N, dict)` and which RE-ASSIGNS N, in the same function scope.
# --------------------------------------------------------------------------

_LOCAL_ALLOW = {("mod.py", "f"): (1, "LOCAL", "r")}


def _local_problems(tmp_path, src):
    sites = _scratch(tmp_path, src)
    return sites, local_guard_problems(sites, root=tmp_path, allow_list=_LOCAL_ALLOW)


def test_local_guard_rejects_isinstance_only_in_a_nested_function(tmp_path):
    _sites, got = _local_problems(tmp_path, (
        "import json\n"
        "def f(row):\n"
        "    meta = json.loads(row['metadata'])\n"
        "    def g():\n"
        "        if not isinstance(meta, dict):\n"
        "            meta = {}\n"
        "        return meta\n"
        "    return meta.get('x')\n"
    ))
    assert [p[2] for p in got] == [3], got
    assert got[0][3]  # carries a human-readable `why`


def test_local_guard_rejects_a_guard_inside_if_false(tmp_path):
    _sites, got = _local_problems(tmp_path, (
        "import json\n"
        "def f(row):\n"
        "    meta = json.loads(row['metadata'])\n"
        "    if False:\n"
        "        if not isinstance(meta, dict):\n"
        "            meta = {}\n"
        "    return meta.get('x')\n"
    ))
    assert [p[2] for p in got] == [3], got


def test_local_guard_rejects_a_bare_isinstance_without_reassignment(tmp_path):
    for body in (
        "    isinstance(meta, dict)\n",                          # bare expression
        "    assert isinstance(meta, dict)\n",                   # assert
        "    ok = isinstance(meta, dict)\n",                     # used as a value
        "    if not isinstance(meta, dict):\n        return {}\n",  # test but no re-assign of N
    ):
        _sites, got = _local_problems(tmp_path, (
            "import json\n"
            "def f(row):\n"
            "    meta = json.loads(row['metadata'])\n"
            + body +
            "    return meta.get('x')\n"
        ))
        assert [p[2] for p in got] == [3], (body, got)


def test_local_guard_second_parse_of_the_same_name_is_reported_alone(tmp_path):
    _sites, got = _local_problems(tmp_path, (
        "import json\n"                                   # 1
        "def f(row):\n"                                   # 2
        "    meta = json.loads(row['metadata'])\n"        # 3  guarded below
        "    if not isinstance(meta, dict):\n"            # 4
        "        meta = {}\n"                             # 5
        "    meta = json.loads(row['metadata'])\n"        # 6  never guarded
        "    return meta.get('x')\n"                      # 7
    ))
    assert [p[2] for p in got] == [6], got
    assert got[0][3]  # carries a human-readable `why`


def test_local_guard_must_precede_the_next_reassignment(tmp_path):
    _sites, got = _local_problems(tmp_path, (
        "import json\n"
        "def f(row):\n"
        "    meta = json.loads(row['metadata'])\n"
        "    meta = dict(meta)\n"                         # re-assigned unguarded
        "    if not isinstance(meta, dict):\n"
        "        meta = {}\n"
        "    return meta.get('x')\n"
    ))
    assert [p[2] for p in got] == [3], got


def test_local_guard_accepts_not_isinstance_then_reassign(tmp_path):
    _sites, got = _local_problems(tmp_path, (
        "import json\n"
        "def f(row):\n"
        "    meta = json.loads(row['metadata'])\n"
        "    if not isinstance(meta, dict):\n"
        "        meta = {}\n"
        "    return meta.get('x')\n"
    ))
    assert got == []


def test_local_guard_accepts_isinstance_else_reassign(tmp_path):
    _sites, got = _local_problems(tmp_path, (
        "import json\n"
        "def f(row):\n"
        "    meta = json.loads(row['metadata'])\n"
        "    if isinstance(meta, dict):\n"
        "        x = meta.get('x')\n"
        "    else:\n"
        "        meta = {}\n"
        "        x = None\n"
        "    return x\n"
    ))
    assert got == []


def test_local_guard_try_except_alternative_is_not_a_reassignment_window(tmp_path):
    head = (
        "import json\n"
        "def f(row):\n"
        "    try:\n"
        "        meta = json.loads(row['metadata'])\n"
        "    except ValueError:\n"
        "        meta = {}\n"
    )
    guard = "    if not isinstance(meta, dict):\n        meta = {}\n"
    tail = "    return meta.get('x')\n"
    _s, got = _local_problems(tmp_path, head + guard + tail)
    assert got == []
    _s, got = _local_problems(tmp_path, head + tail)  # handler alone is no guard
    assert [p[2] for p in got] == [4], got


# --------------------------------------------------------------------------
# #3425 round 2: the guard must MATCH ITS ARM and assign a dict LITERAL.
#   `not isinstance(N, dict)` -> `N = {}` in the BODY;
#   `isinstance(N, dict)`     -> `N = {}` in the ORELSE.
# --------------------------------------------------------------------------

def _guard_src(guard):
    return ("import json\n"                                 # 1
            "def f(row):\n"                                 # 2
            "    meta = json.loads(row['metadata'])\n"      # 3
            + guard +
            "    return meta.get('x')\n")


@pytest.mark.parametrize("guard", [
    # wrong sense: assigns in the body of the POSITIVE test
    "    if isinstance(meta, dict):\n        meta = {}\n",
    # wrong sense, with an else that does not assign
    "    if isinstance(meta, dict):\n        meta = {}\n    else:\n        pass\n",
    # wrong sense the other way: assigns in the ORELSE of the NEGATED test
    "    if not isinstance(meta, dict):\n        pass\n    else:\n        meta = {}\n",
    # non-dict value
    "    if not isinstance(meta, dict):\n        meta = None\n",
    "    if isinstance(meta, dict):\n        pass\n    else:\n        meta = []\n",
    "    if not isinstance(meta, dict):\n        meta = dict(meta)\n",
])
def test_local_guard_rejects_wrong_arm_or_non_dict_value(tmp_path, guard):
    _sites, got = _local_problems(tmp_path, _guard_src(guard))
    assert [p[2] for p in got] == [3], (guard, got)
    assert got[0][3]  # carries a human-readable `why`


def test_local_guard_walrus_store_between_parse_and_guard_ends_the_window(tmp_path):
    _sites, got = _local_problems(tmp_path, (
        "import json\n"
        "def f(row):\n"
        "    meta = json.loads(row['metadata'])\n"
        "    if (meta := {}):\n"
        "        pass\n"
        "    if not isinstance(meta, dict):\n"
        "        meta = {}\n"
        "    return meta.get('x')\n"
    ))
    assert [p[2] for p in got] == [3], got
    assert got[0][3]


def test_local_guard_ternary_shape_is_rejected_by_decision(tmp_path):
    """PINNED DECISION, not an accident: `meta = meta if isinstance(meta, dict)
    else {}` is not modelled (it has no `if` statement) and is reported. A site
    that wants it must be rewritten as the statement form."""
    _sites, got = _local_problems(tmp_path, _guard_src(
        "    meta = meta if isinstance(meta, dict) else {}\n"))
    assert [p[2] for p in got] == [3], got
    assert got[0][3]
