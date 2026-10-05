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
    only if the enclosing function holds an `isinstance(<parsed name>, dict)`
    check for every flagged parse (AST check, `local_guard_problems`); LOCAL
    sites are not counted in the `#3425` ceiling.

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
# "LOCAL" = routed locally with an `isinstance(<parsed>, dict)` guard in the
# same function (verified by the AST check), for code that does not import
# engram_core by convention.
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


def _isinstance_dict_names(fn_node):
    """Names `n` for which `isinstance(n, dict)` appears inside fn_node."""
    names = set()
    for n in ast.walk(fn_node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "isinstance" and len(n.args) == 2
                and isinstance(n.args[0], ast.Name)
                and isinstance(n.args[1], ast.Name) and n.args[1].id == "dict"):
            names.add(n.args[0].id)
    return names


def local_guard_problems(sites, root=SRC_ROOT, allow_list=ALLOW_LIST):
    """For every LOCAL entry: each flagged parse must be assigned to a plain
    name AND that name must be `isinstance(<name>, dict)`-checked somewhere in
    the enclosing function. Returns [(rel, function, line, why)]."""
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
            ok = False
            for f in fns:
                checked = _isinstance_dict_names(f)
                for a in ast.walk(f):
                    if (isinstance(a, ast.Assign) and len(a.targets) == 1
                            and isinstance(a.targets[0], ast.Name)
                            and any(isinstance(c, ast.Call) and _is_json_load(c)
                                    and c.lineno == line for c in ast.walk(a.value))
                            and a.targets[0].id in checked):
                        ok = True
            if not ok:
                problems.append((rel, fn, line,
                                 "no `isinstance(<parsed name>, dict)` check for this parse"))
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
        "LOCAL allow-list entries need an `isinstance(<parsed>, dict)` check in "
        "the enclosing function:\n"
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
