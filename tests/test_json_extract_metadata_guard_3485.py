"""Drift guard: no SQL JSON function (or `->` / `->>` operator) may be applied to
an UNGUARDED `metadata` column reference, unless it is on a named allow-list
(#3485, ratchet for #3425 / #3484).

Why. `nodes.metadata` is TEXT holding JSON. A non-JSON blob makes SQLite's
`json_extract(metadata, ...)` raise `OperationalError: malformed JSON` for the
WHOLE query, so one bad row poisons every row the statement would have
returned. #3484 added a write guard (a trigger) and guarded six read sites;
#3485 guarded the rest (the lesson-tripwire hook's two loaders and the
reconcile tool) after measuring that ONE bad blob silenced the entire tripwire
layer. The guard is the inner form

    json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.key')

(a guarded read yields NULL for a bad blob instead of aborting). This test
makes the next unguarded site trip a test instead of waiting to be found by a
user with a bad row.

What the scan ranges over (and nothing else):
  * every `.py` under `src/engram/` (tests are not under it);
  * every extensionless python script there or under repo-root `tools/`
    (first line is a python shebang, the rule test_metadata_parse_drift_guard_3413
    uses -- e.g. `tools/engram-pkg/engram-pkg`);
  * every `.py` under repo-root `tools/` (a mix of real files and symlinks into
    `src/engram/tools/`; files are de-duplicated by REAL path so a symlinked
    file is scanned once, under its `src/engram/` label; the real files that
    exist only at repo root, e.g. `tools/engramd_read_scan.py`, are scanned
    under a `repo-tools/` label);
  * `src/engram/engramd_read_templates.json` (the registered read statements;
    GENERATED from the hook source, so it can only differ from the hook if
    someone hand-edits it -- scanned anyway, it is what the owner executes).
The repo root is resolved from THIS file's location, not a fixed `parents[N]`
into the source layout.

What counts as a site. A STRING (single-, double- or triple-quoted; a
docstring is not SQL and is excluded; comments are not strings) holding
  * a call to a JSON function that takes a JSON document first --
    `json_extract`, `json_each`, `json_tree`, `json_type`,
    `json_array_length`, `json_insert`, `json_replace`, `json_set`,
    `json_remove`, `json_patch`, and their `jsonb_` twins -- whose FIRST
    argument mentions the `metadata` column (bare, `"metadata"`, backticked,
    bracketed, or aliased `n.metadata`, with any whitespace/newlines between
    `(` and the argument) and is not exactly the guard
    `CASE WHEN json_valid(<col>) THEN <col> END` over the same column; or
  * the `->` / `->>` operator whose left operand is a bare `metadata` column
    reference followed by a path/key/placeholder.
  * a call whose FIRST ARGUMENT runs to the end of its string piece (the scan
    never reached a top-level `,` or `)` before the literal ended) -- e.g. SQL
    assembled by `+` concatenation or an f-string with the argument split from
    the function name across pieces. The scan cannot see that argument, so it
    refuses to certify it: such a call is a site, reported as "first argument
    not visible in the literal" (#3867). Rule for authors: a json-function's
    first argument must be visible in the literal.
The write-guard triggers (`json_valid(NEW.metadata)`) are not reads: `json_valid`
is not in the function set. A reference wrapped in the guard form is guarded.

What the guard CANNOT catch (so a green guard is not "the class is closed"):
  * SQL assembled from several pieces where the whole statement is built by
    interpolation (only each literal piece is scanned; the cut-off rule above
    covers the pieces whose call is split, not statements built whole);
  * a first argument that reaches the column through a view, a CTE alias, a
    subselect column renamed away from `metadata`, or a Python variable
    interpolated into an f-string;
  * a JSON function over a differently named column that holds metadata;
  * SQL in files that are neither `.py`, a python-shebang script, nor the
    templates JSON (a shell script's heredoc SQL, a `.sql` file);
  * a guard that is syntactically the exact form but sits in a position where
    the unguarded column is ALSO read elsewhere in the same statement (each
    JSON-function call is judged on its own first argument).

Ratchet. Each allow-list entry is `(relative path, enclosing function or
"<module>" / "<file>") -> (exact site count, issue ref, one-line reason)`:
  * a site not covered by an entry fails (new drift);
  * an entry whose count no longer matches the tree fails the stale-entry test
    (an extra site inside an allow-listed function fails too, so an entry
    cannot hide a new one);
  * the ALLOW_LIST starts EMPTY and `_MAX_ALLOWED_SITES` is 0: lower it, never
    raise it.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "engram"
TOOLS_ROOT = REPO_ROOT / "tools"
TEMPLATES_REL = "engramd_read_templates.json"

# (relative path, enclosing function / "<module>" / "<file>") ->
#   (exact site count, issue ref, one-line reason)
ALLOW_LIST: dict = {}

# The ratchet: total sites the allow-list may excuse. Lower, never raise.
_MAX_ALLOWED_SITES = 0


# --------------------------------------------------------------------------
# the scanner (pure; parametrised so it can be run on scratch trees)
# --------------------------------------------------------------------------

_FUNCS = r"jsonb?_(?:extract|each|tree|type|array_length|insert|replace|set|remove|patch)"
_CALL_RE = re.compile(r"(?<![\w.])" + _FUNCS + r"\s*\(", re.I)
_META_RE = re.compile(r"(?<!\w)metadata(?!\w)", re.I)
_COLREF_RE = re.compile(r'^(?:[A-Za-z_]\w*\s*\.\s*)?(?:"metadata"|`metadata`|\[metadata\]|metadata)$', re.I)
_GUARD_RE = re.compile(
    r"^CASE\s+WHEN\s+json_valid\s*\(\s*(?P<c1>[^()]+?)\s*\)\s+THEN\s+(?P<c2>[^()]+?)\s+END$",
    re.I | re.S,
)
_OP_RE = re.compile(
    r"""(?<!\w)(?:[A-Za-z_]\w*\s*\.\s*)?(?:"metadata"|`metadata`|\[metadata\]|metadata)\s*->>?\s*(?=['"?$:@\d])""",
    re.I,
)
_SQ_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def _first_arg(text, start):
    """The text of the first argument of the call whose `(` is at start-1."""
    depth, i, quote = 0, start, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 1  # doubled quote = escaped
                else:
                    quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    return text[start:i]


def _is_guard(arg):
    m = _GUARD_RE.match(" ".join(arg.split()))
    if not m:
        return False
    c1, c2 = m.group("c1").strip(), m.group("c2").strip()
    return c1.lower() == c2.lower() and bool(_COLREF_RE.match(c1))


def _first_arg_end(text, start):
    """(first-argument text, terminated). `terminated` is False when the scan ran
    off the end of `text` without reaching a top-level `,` or `)`: the first
    argument is cut off by the literal (#3867)."""
    arg = _first_arg(text, start)
    return arg, start + len(arg) < len(text)


def cut_off_calls(text):
    """Number of json-function calls in `text` whose first argument is not
    visible: it runs to the end of the string piece (#3867)."""
    return sum(1 for m in _CALL_RE.finditer(text) if not _first_arg_end(text, m.end())[1])


def sql_sites(text):
    """Number of unguarded metadata JSON reads, plus json-function calls whose
    first argument is cut off by the end of the literal, in one SQL string."""
    n = 0
    for m in _CALL_RE.finditer(text):
        arg, terminated = _first_arg_end(text, m.end())
        if not terminated:
            n += 1  # cannot see the first argument: refuse to certify it
            continue
        if not _META_RE.search(_SQ_LITERAL_RE.sub("''", arg)):
            continue  # first argument is not (or does not mention) the column
        if not _is_guard(arg):
            n += 1
    n += len(_OP_RE.findall(text))
    return n


def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def python_sites(source, label):
    """[(label, line, enclosing function)] for one python source text."""
    tree = ast.parse(source)
    skip = _docstring_ids(tree)
    out = []

    def visit(node, fname):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if (isinstance(child, ast.Constant) and isinstance(child.value, str)
                    and id(child) not in skip):
                for _ in range(sql_sites(child.value)):
                    out.append((label, child.lineno, fname))
            visit(child, fname)

    visit(tree, "<module>")
    return out


def json_sites(source, label):
    """Same, for a JSON file: every string VALUE is scanned as SQL."""
    out = []

    def walk(v):
        if isinstance(v, str):
            for _ in range(sql_sites(v)):
                out.append((label, 0, "<file>"))
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)

    walk(json.loads(source))
    return out


def _is_python_script(path):
    """An extensionless file whose first line is a python shebang."""
    if path.suffix or not path.is_file():
        return False
    try:
        with open(path, "rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def collect_files(src_root=SRC_ROOT, tools_root=TOOLS_ROOT):
    """{label: real Path}. A file under src_root is labelled by its path
    relative to src_root; a file that exists only under the repo-root tools dir
    is labelled `repo-tools/<rel>` (a distinct prefix, so it can never collide
    with a `tools/...` label under src_root). De-duplicated by REAL path: the
    repo-root symlinks into src/engram/tools/ are scanned once, under their
    src_root label (src_root is walked first)."""
    src_root = Path(src_root).resolve()
    seen, out = set(), {}

    def add(path, label):
        real = path.resolve()
        if real in seen:
            return
        seen.add(real)
        out[label] = real

    def candidates(root):
        return sorted(p for p in root.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts
                      and (p.suffix == ".py" or _is_python_script(p)))

    for p in candidates(src_root):
        add(p, p.relative_to(src_root).as_posix())
    tools_root = Path(tools_root)
    if tools_root.exists():
        for p in candidates(tools_root):
            add(p, "repo-tools/" + p.relative_to(tools_root).as_posix())
    tpl = src_root / TEMPLATES_REL
    if tpl.exists():
        add(tpl, TEMPLATES_REL)
    return out


def find_sites(files):
    """[(label, line, enclosing function)] over {label: Path}."""
    sites = []
    for label, path in sorted(files.items()):
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            sites.extend(json_sites(text, label))
        else:
            sites.extend(python_sites(text, label))
    return sites


def unaccounted_sites(sites, allow_list=None):
    allow_list = ALLOW_LIST if allow_list is None else allow_list
    return [s for s in sites if (s[0], s[2]) not in allow_list]


def entry_mismatches(sites, allow_list=None):
    allow_list = ALLOW_LIST if allow_list is None else allow_list
    found = {}
    for rel, _line, fn in sites:
        found[(rel, fn)] = found.get((rel, fn), 0) + 1
    return [(key, spec[0], found.get(key, 0))
            for key, spec in allow_list.items() if found.get(key, 0) != spec[0]]


# --------------------------------------------------------------------------
# the guard on the real tree
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tree_sites():
    files = collect_files()
    return files, find_sites(files)


def test_no_unguarded_metadata_json_read_outside_the_allow_list(tree_sites):
    """Mutation: remove the guard from any `json_extract(metadata, ...)` in the
    scanned tree (or add one) -> this fails naming file, line, function."""
    _files, sites = tree_sites
    bad = unaccounted_sites(sites)
    assert not bad, (
        "unguarded `json_*(metadata, ...)` read (a non-JSON blob would abort the whole "
        "query). Wrap it: json_extract(CASE WHEN json_valid(metadata) THEN metadata END, ...). "
        "Or: first argument not visible in the literal -- a json-function's first argument must be visible in the literal "
        "(spell the CASE WHEN json_valid(...) wrap out in the same string piece as the call; do not concatenate or interpolate it). "
        "Sites (path, line, function): %r" % bad
    )


def test_every_allow_list_entry_matches_the_tree_exactly(tree_sites):
    """Stale or too-generous entries fail; an extra site inside an allow-listed
    function fails (the exact count is the ratchet)."""
    _files, sites = tree_sites
    assert entry_mismatches(sites) == []


def test_allow_list_is_within_its_ratchet():
    assert sum(spec[0] for spec in ALLOW_LIST.values()) <= _MAX_ALLOWED_SITES


def test_scan_covers_the_known_read_sites_and_dedupes_symlinks(tree_sites):
    """Non-vacuity: the scan reached the files this guard exists for, and a
    symlinked tools/ file appears once, under its src/engram label."""
    files, _sites = tree_sites
    labels = set(files)
    assert "hooks/claude/engram-lesson-tripwire-hook.py" in labels
    assert "tools/reconcile_lesson_demotions.py" in labels
    assert TEMPLATES_REL in labels
    assert "engram_filter.py" in labels  # a guarded site (control: scanned, 0 hits)
    # an extensionless shebang script is covered (the .py glob alone misses it)
    assert "tools/engram-pkg/engram-pkg" in labels
    # a file that exists ONLY at repo-root tools/ is covered under a repo-tools/ label
    assert "repo-tools/engramd_read_scan.py" in labels
    # de-dup: no two labels share a real path
    reals = [p.resolve() for p in files.values()]
    assert len(reals) == len(set(reals))
    # the repo-root symlink of a src/engram/tools file did not add a second label
    assert "repo-tools/reconcile_lesson_demotions.py" not in labels


# --------------------------------------------------------------------------
# the scanner's own contract, on synthetic text (so a green guard means
# something: each of these is a shape the scan must flag or must NOT flag)
# --------------------------------------------------------------------------

FLAGGED = {
    "bare": "SELECT json_extract(metadata, '$.x') FROM nodes",
    "multiline": "SELECT json_extract(\n    metadata, '$.x') FROM nodes",
    "newline-before-paren-close-arg": "SELECT json_extract  (\n\t metadata\n , '$.x') FROM nodes",
    "aliased": "SELECT json_extract(n.metadata, '$.x') FROM nodes n",
    "aliased-m": "SELECT json_extract(m.metadata, '$.x') FROM nodes m",
    "double-quoted-col": 'SELECT json_extract("metadata", \'$.x\') FROM nodes',
    "backtick-col": "SELECT json_extract(`metadata`, '$.x') FROM nodes",
    "bracket-col": "SELECT json_extract([metadata], '$.x') FROM nodes",
    "upper": "SELECT JSON_EXTRACT(METADATA, '$.x') FROM nodes",
    "json_each": "SELECT value FROM nodes, json_each(nodes.metadata, '$.aliases')",
    "json_each-bare": "SELECT value FROM json_each(metadata)",
    "json_type": "SELECT json_type(metadata, '$.x') FROM nodes",
    "json_tree": "SELECT key FROM nodes, json_tree(metadata)",
    "json_array_length": "SELECT json_array_length(metadata, '$.l') FROM nodes",
    "jsonb": "SELECT jsonb_extract(metadata, '$.x') FROM nodes",
    "arrow": "SELECT metadata -> '$.x' FROM nodes",
    "arrow-text": "SELECT n.metadata ->> '$.x' FROM nodes n",
    "arrow-placeholder": "SELECT metadata->>? FROM nodes",
    "coalesce-wrapper-is-not-a-guard": "SELECT json_extract(COALESCE(metadata, '{}'), '$.x') FROM nodes",
    "wrong-guard-column": "SELECT json_extract(CASE WHEN json_valid(claim) THEN metadata END, '$.x') FROM nodes",
    "guard-with-else": "SELECT json_extract(CASE WHEN json_valid(metadata) THEN metadata ELSE metadata END, '$.x') FROM nodes",
    "guarded-one-unguarded-other": (
        "SELECT json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.a'), "
        "json_extract(metadata, '$.b') FROM nodes"
    ),
    # #3867: a first argument cut off by the end of its literal is a site (not visible).
    "cut-off-empty-argument": "json_extract(",
    "cut-off-partly-cut-argument": "SELECT json_extract(n.",
    "cut-off-after-select-list": "SELECT json_type(",
}

NOT_FLAGGED = {
    "guarded": "SELECT json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.x') FROM nodes",
    "guarded-multiline": "SELECT json_extract(\n  CASE WHEN json_valid(metadata)\n       THEN metadata END,\n  '$.x') FROM nodes",
    "guarded-quoted": 'SELECT json_extract(CASE WHEN json_valid("metadata") THEN "metadata" END, \'$.x\') FROM nodes',
    "guarded-aliased": "SELECT json_extract(CASE WHEN json_valid(n.metadata) THEN n.metadata END, '$.x') FROM nodes n",
    "guarded-json_each": "SELECT value FROM nodes, json_each(CASE WHEN json_valid(metadata) THEN metadata END, '$.a')",
    "write-guard-trigger": "WHEN NEW.metadata IS NOT NULL AND NOT json_valid(NEW.metadata)",
    "other-column": "SELECT json_extract(payload, '$.x') FROM edges",
    "metadata-only-in-path-literal": "SELECT json_extract(payload, '$.metadata') FROM edges",
    "metadata-as-substring-of-column": "SELECT json_extract(parsed_metadata, '$.x') FROM t",
    "metadata-as-value": "SELECT id FROM nodes WHERE metadata IS NOT NULL AND json_valid(metadata)",
    "arrow-on-other-column": "SELECT payload -> '$.x' FROM edges",
    "guarded-operand-arrow": "SELECT (CASE WHEN json_valid(metadata) THEN metadata END) -> '$.x' FROM nodes",
}


@pytest.mark.parametrize("name", sorted(FLAGGED))
def test_scanner_flags(name):
    """Mutation: loosen the first-argument / alias / quote / whitespace handling
    (or drop the operator regex) -> the corresponding shape goes red."""
    assert sql_sites(FLAGGED[name]) >= 1, FLAGGED[name]


@pytest.mark.parametrize("name", sorted(NOT_FLAGGED))
def test_scanner_does_not_flag(name):
    """Mutation: treat any `json_valid` mention (or the bare word `metadata`)
    as a hit -> the guarded / unrelated shapes go red."""
    assert sql_sites(NOT_FLAGGED[name]) == 0, NOT_FLAGGED[name]


CUT_OFF = ["json_extract(", "SELECT json_extract(n.", "json_type(", "json_set(CASE WHEN json_valid(metadata)"]
NOT_CUT_OFF = [
    "json_extract(metadata, '$.a')",  # unguarded (a site for another reason) but fully visible
    "SELECT json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.a') FROM nodes",
    "json_each(x) j",
    "json_extract(payload, '$.a')",
]


@pytest.mark.parametrize("text", CUT_OFF)
def test_cut_off_predicate_true(text):
    """#3867: the first argument ran to the end of the literal (never reached a
    top-level `,` or `)`). `SELECT json_extract(n.` is partly cut, so an
    empty-argument rule would miss it."""
    assert cut_off_calls(text) == 1, text
    assert sql_sites(text) == 1, text


@pytest.mark.parametrize("text", NOT_CUT_OFF)
def test_cut_off_predicate_false(text):
    assert cut_off_calls(text) == 0, text


def test_a_cut_off_call_that_also_mentions_the_column_is_one_site():
    assert sql_sites("SELECT json_extract(n.metadata") == 1


def test_python_scan_flags_a_first_argument_split_across_pieces():
    """(a)/(b) #3867: the concatenated and f-string forms cut the first argument
    off at the piece boundary, so the piece holding the call is a site."""
    concat = 'X = "metadata"\nq = "json_type(" + X + ", \'$.k\')"\n'
    fstr = 'X = "metadata"\nq = f"json_type({X}, \'$.k\')"\n'
    partly = 'q = "json_extract(n." + "metadata, \'$.a\')"\n'
    for src in (concat, fstr, partly):
        got = python_sites(src, "scratch.py")
        assert len(got) == 1 and got[0][0] == "scratch.py", (src, got)


def test_python_scan_does_not_flag_a_fully_literal_guard_in_one_piece():
    src = ('q = ("SELECT json_extract(CASE WHEN json_valid(metadata) THEN metadata END, \'$.a\')"\n'
           '     " FROM nodes")\n')
    assert python_sites(src, "scratch.py") == []


def test_failure_message_tells_authors_the_first_argument_must_be_visible():
    import inspect
    src = inspect.getsource(test_no_unguarded_metadata_json_read_outside_the_allow_list)
    assert "first argument must be visible in the literal" in src


def test_scanner_counts_each_call_separately():
    assert sql_sites(FLAGGED["guarded-one-unguarded-other"]) == 1
    two = "SELECT json_extract(metadata, '$.a'), json_extract(metadata, '$.b') FROM nodes"
    assert sql_sites(two) == 2


@pytest.mark.parametrize("quote", ["'", '"', '"""', "'''"])
def test_python_scan_flags_every_string_form(quote):
    """Single-, double- and triple-quoted python strings are all scanned."""
    inner = "json_extract(\n    metadata, '$.x')" if len(quote) == 3 else "json_extract(metadata, $.x)"
    if quote == '"':
        inner = inner.replace("'", "")
    src = "def f():\n    q = %s%s%s\n" % (quote, inner, quote)
    assert python_sites(src, "scratch.py") == [("scratch.py", 2, "f")]


def test_python_scan_ignores_comments_and_docstrings():
    src = (
        '"""module doc: json_extract(metadata, \'$.x\') is the bare form."""\n'
        "# json_extract(metadata, '$.x') in a comment\n"
        "def f():\n"
        '    """doc: json_extract(metadata, \'$.x\')"""\n'
        "    return 1\n"
    )
    assert python_sites(src, "scratch.py") == []


def test_python_scan_attributes_the_site_to_the_enclosing_function():
    src = (
        "Q = \"SELECT json_extract(metadata, '$.a')\"\n"
        "def outer():\n"
        "    def inner():\n"
        "        return \"SELECT json_extract(metadata, '$.b')\"\n"
        "    return inner\n"
    )
    assert python_sites(src, "s.py") == [("s.py", 1, "<module>"), ("s.py", 4, "inner")]


def test_json_scan_flags_a_template_string_value_and_ignores_dict_keys():
    doc = json.dumps({"json_extract(metadata, '$.k')": 1, "templates": [
        {"key": "k1", "sql": "SELECT json_extract(metadata, '$.x') FROM nodes", "sites": []},
        {"key": "k2", "sql": "SELECT json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.x') FROM nodes", "sites": []},
    ]})
    assert json_sites(doc, "t.json") == [("t.json", 0, "<file>")]


def test_ratchet_mechanics_on_a_synthetic_allow_list():
    sites = [("a.py", 3, "f"), ("a.py", 9, "f"), ("b.py", 1, "g")]
    allow = {("a.py", "f"): (2, "#0", "reason")}
    assert unaccounted_sites(sites, allow) == [("b.py", 1, "g")]
    assert entry_mismatches(sites, allow) == []
    assert entry_mismatches(sites[:1] + sites[2:], allow) == [(("a.py", "f"), 2, 1)]  # stale / extra
    assert entry_mismatches(sites, {("c.py", "h"): (1, "#0", "r")}) == [(("c.py", "h"), 1, 0)]


def test_collect_files_on_a_scratch_tree(tmp_path):
    """A scratch .py, an extensionless python script and a non-python script
    under a scratch src root: the first two are scanned, the third is not; and
    a repo-root tools symlink to a src file is de-duplicated."""
    src = tmp_path / "src"
    (src / "tools").mkdir(parents=True)
    (src / "a.py").write_text("x = 1\n")
    (src / "tools" / "t.py").write_text("y = 1\n")
    (src / "script").write_text("#!/usr/bin/env python3\nz = 1\n")
    (src / "shellscript").write_text("#!/bin/sh\necho hi\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "t.py").symlink_to(src / "tools" / "t.py")
    (tools / "only_here.py").write_text("w = 1\n")
    files = collect_files(src, tools)
    assert set(files) == {"a.py", "tools/t.py", "script", "repo-tools/only_here.py"}
