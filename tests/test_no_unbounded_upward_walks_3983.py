"""engram-alpha#3983: no NEW unbounded upward walk can ship unclassified.

Round 1 of #3983 grepped one spelling (``for x in ...parents``) and missed a second
(``roots.extend(...parents)``); round 2 found a third in shell (``while ...; d=$(dirname $d)``).
This guard scans EVERY shipped ``src/engram/**/*.py`` by AST for

  * ANY use of the ``.parents`` attribute, whatever surrounds it, and
  * CONTRACT: any ``while``/``for`` loop that reassigns a name ``n`` from an expression that both
    references ``n`` and applies an upward-path operation to it.  The operations are ``.parent``,
    ``.parents``, ``os.path.dirname``, ``os.path.split(...)[0]``, ``.rsplit`` / ``.rpartition``
    with a path-separator argument (``"/"``, backslash, ``os.sep``, ``os.path.sep``, ``os.altsep``),
    ``.joinpath("..")``, ``os.path.join(x, "..")`` and ``/ ".."``; ``os.pardir`` and ``Path("..")``
    count wherever ``".."`` does.  The match is structural: wrappers around the reference do
    not hide it (``d.resolve().parent``, ``Path(os.path.dirname(d))``, ``Path(d).parent``,
    ``d.joinpath("..").resolve()``, ``os.path.normpath(os.path.join(d, ".."))``).  A name
    reassigned from an expression that does not reference
    it (``d = base.parent``), a ``.parent`` read that is not a reassignment, a string
    ``.split(...)[0]``, and ``.rsplit``/``.rpartition`` on a non-separator (``name.rsplit(".", 1)[0]``,
    an extension or dotted-module strip) are not walks,

and every ``src/engram/**/*.sh`` by text for the same shape (``d="$(dirname "$d")"``,
``d="${d%/*}"``).  A hit passes only if it is

  * for a LOOP hit: the loop itself iterates the bounded helper (``for d in bounded_parents(...)``),
    judged on the INNERMOST loop around the reassignment, or the code sits inside the helper's own
    definition.  A function that merely CALLS the helper elsewhere no longer exempts its other
    loops.  A bare ``.parents`` use (``for d in here.parents:``, ``list(here.parents)``,
    ``here.parents[2]``) is exempt only inside a helper definition: calling the helper elsewhere
    in the same function does not exempt it either.  Every exemption is per use, not per function.
  * listed below, keyed ``path:function`` (never by line number), with its class and reason:
        A  a sibling-runtime lookup (must go through the bounded walk, so never listed)
        B  a repo-root / source-checkout / plugin-tree finder: it looks for a repo or a data
           directory, not for a runtime file shipped beside the caller
        C  something else: a fixed window, a path canonicaliser, a constant
    A new, unclassified walk fails the test, naming its key.

Limits, stated.  NOT covered:
  * trees other than ``src/engram`` (``src/forum`` is not shipped in the plugin; nothing outside
    ``src/engram`` is scanned), and files that are neither ``.py`` nor ``.sh``;
  * a shell ``cd ..`` loop, and shell walks spelled other than the dirname / ``${d%/*}`` forms;
  * a separator held in a variable (``rsplit(SEP, 1)[0]``) is not recognised as a separator;
  * a walk through a helper this scan does not know: ``d = _up(d)`` where ``_up`` does the
    ``.parent`` inside another function is not seen (the helper's own body is, if it loops);
  * a walk that is not a plain-name reassignment: attribute/subscript/tuple targets
    (``self.d = self.d.parent``, ``a, b = b, b.parent``), ``d /= ".."``, comprehensions, and
    recursion (``f(d.parent)``);
  * an upward step the scan cannot see as path-like: ``up = operator.attrgetter("parent")``,
    string slicing (``d[:d.rfind("/")]``), ``re.sub``, or ``..`` joined as a string;
  * a walk split over statements (``t = d.parent`` then ``d = t``);
  * the innermost-loop rule: a reassignment sitting in a bounded ``for`` nested inside an unbounded
    ``while`` is judged by the ``for`` alone.
"""

from __future__ import annotations

import ast
import re
import shutil
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "engram"
BOUNDED = ("bounded_parents", "_bounded_parents", "_own_tree_window", "_bounded_dirs")

# key -> "<class>: <reason>".  Keyed path (relative to src/engram) : innermost function.
PY_ALLOW = {
    "engram_core.py:_git_sha_for_file":
        "B: finds the git repo root of a cited file (20 steps), not a runtime file",
    "engram_observation.py:_resolve_with_trace":
        "C: physical path resolution; `..` handling on a path component stack",
    "health/series.py:_default_repo_dir":
        "C: a fixed parents[3] constant (the code checkout of a source tree)",
    "health/series.py:_find_plugin_tree_data_dir":
        "B: finds the data dir above a deployed marketplace/plugins/engram tree",
    "hooks/claude/_engramd_owner_hook_io.py:_import_sibling":
        "C: a fixed two-level window (own dir, its parent), never an open-ended climb",
    "hooks/claude/_hooklib.py:resolve_runtime_dir":
        "B: source-checkout finder (<ancestor>/src/engram), reached only after the plugin root and home",
    "hooks/claude/engram-lesson-tripwire-hook.py:_resolve_runtime_dir":
        "B: source-checkout finder (<ancestor>/src/engram), reached only after the plugin root and home",
    "tools/release-cutover.py:main":
        "B: finds the VCS root of the source checkout (5 steps)",
    "tools/run_matrix.py:<module>":
        "C: a fixed parents[3] constant (repo root of the test matrix)",
    "tools/run_touched_tests.py:_find_repo_root":
        "B: finds the repo root (pytest.ini + tests/), a dev tool",
    "viz/version_info.py:find_git_root":
        "B: finds the git root of a checkout",
}

SH_ALLOW = {
    "tools/engramd-owner-home-key.sh:engramd_owner_physical_path":
        "C: nearest EXISTING ancestor of a not-yet-existing home path, not a runtime-file lookup",
    "tools/setup-surface-daemon-service.sh:_physical_path":
        "C: symlink resolution (readlink chain, capped at 40), not a runtime-file lookup",
}


# ------------------------------------------------------------------ python scan

def _functions(tree: ast.AST):
    """Map id(node) -> (innermost function node or None)."""
    owner = {}

    def visit(n, cur):
        for c in ast.iter_child_nodes(n):
            nc = c if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) else cur
            owner[id(c)] = nc
            visit(c, nc)

    visit(tree, None)
    return owner


_SPLITTERS = ("rsplit", "rpartition")
_SEP_NAMES = ("os.sep", "os.path.sep", "os.altsep")


def _dotted(n: ast.AST) -> str:
    """`os.path.sep` for an Attribute/Name chain, else ''."""
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        parts.append(n.id)
        return ".".join(reversed(parts))
    return ""


def _is_sep(a: ast.AST) -> bool:
    """A path separator: "/", "\\", os.sep, os.path.sep, os.altsep."""
    if isinstance(a, ast.Constant):
        return a.value in ("/", "\\")
    return _dotted(a) in _SEP_NAMES


def _is_dotdot(a: ast.AST) -> bool:
    """`".."`, `os.pardir`, or `Path("..")` / `Path(os.pardir)`."""
    if isinstance(a, ast.Constant):
        return a.value == ".."
    if isinstance(a, ast.Attribute):
        return _dotted(a) in ("os.pardir", "os.path.pardir")
    if isinstance(a, ast.Call) and len(a.args) == 1 and not a.keywords:
        f = a.func
        return (f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "") in (
            "Path", "PurePath", "PurePosixPath", "PureWindowsPath", "PosixPath", "WindowsPath"
        ) and _is_dotdot(a.args[0])
    return False


def _is_upward_op(n: ast.AST) -> bool:
    """One upward-path operation, whatever it wraps: `.parent`, `.parents`, `dirname(..)`,
    `split(..)[0]`, `.rsplit`/`.rpartition` on a path separator, `.joinpath("..")`,
    `os.path.join(x, "..")`, `x / ".."` (`os.pardir` counts like `".."`)."""
    if isinstance(n, ast.Attribute):
        return n.attr in ("parent", "parents")
    if isinstance(n, ast.Call):
        f = n.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        if name == "dirname":
            return True
        if name in _SPLITTERS:  # only a PATH split: the separator must be a path separator
            return bool(n.args) and _is_sep(n.args[0])
        if name == "joinpath":
            return any(_is_dotdot(a) for a in n.args)
        return name == "join" and _dotted(f) in ("os.path.join", "path.join", "posixpath.join",
                                                 "ntpath.join") and any(_is_dotdot(a) for a in n.args)
    if isinstance(n, ast.Subscript):  # os.path.split(d)[0]: the HEAD of a PATH split only
        v, i = n.value, n.slice
        if not (isinstance(v, ast.Call) and isinstance(i, ast.Constant) and i.value == 0):
            return False
        f = v.func
        if isinstance(f, ast.Name):
            return f.id == "split"
        return isinstance(f, ast.Attribute) and f.attr == "split" and (
            (isinstance(f.value, ast.Attribute) and f.value.attr == "path")
            or (isinstance(f.value, ast.Name) and f.value.id in ("path", "posixpath", "ntpath")))
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
        return _is_dotdot(n.left) or _is_dotdot(n.right)
    return False


def _references(tree: ast.AST, name: str) -> bool:
    return any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(tree))


def _is_upward(value: ast.AST, target: str) -> bool:
    """True when `value` applies an upward-path operation to an expression that references `target`."""
    return any(_is_upward_op(n) and _references(n, target) for n in ast.walk(value))


def _assigned_names(node: ast.AST):
    """(name, value) for each plain-name (re)assignment spelled by `node`."""
    if isinstance(node, ast.Assign):
        return [(t.id, node.value) for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
        return [(node.target.id, node.value)]
    if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
        return [(node.target.id, node.value)]
    return []


def _iterates_bounded(loop: ast.AST) -> bool:
    """`for d in bounded_parents(...)`: the one loop shape the bounded helper exempts."""
    if not isinstance(loop, ast.For) or not isinstance(loop.iter, ast.Call):
        return False
    f = loop.iter.func
    return (f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "") in BOUNDED


def _loop_walk_sites(tree: ast.AST):
    """Yield (assign_node, function_name_or_None) for each upward self-reassignment whose
    INNERMOST enclosing loop is not a bounded-helper iteration."""
    def visit(n, loop, fn):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            loop, fn = None, n.name
        elif isinstance(n, (ast.While, ast.For)):
            loop = n
        elif loop is not None:
            if any(_is_upward(v, nm) for nm, v in _assigned_names(n)) and not _iterates_bounded(loop):
                yield n, fn
        for c in ast.iter_child_nodes(n):
            yield from visit(c, loop, fn)

    yield from visit(tree, None, None)


def scan_python(root: Path):
    """Return {key: [lineno,...]} of every upward-walk hit NOT covered by the bounded walk."""
    hits: dict[str, list[int]] = {}
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if "tests" in rel.parts:
            continue
        tree = ast.parse(p.read_text())
        owner = _functions(tree)

        def record(node, lineno):
            fn = owner.get(id(node))
            name = fn.name if fn is not None else "<module>"
            if fn is not None and name in BOUNDED:  # the helper's own implementation
                return
            hits.setdefault(f"{rel.as_posix()}:{name}", []).append(lineno)

        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr == "parents":
                record(n, n.lineno)
        for a, fname in _loop_walk_sites(tree):
            if fname in BOUNDED:  # the helper's own implementation
                continue
            hits.setdefault(f"{rel.as_posix()}:{fname or '<module>'}", []).append(a.lineno)
    return hits


# ------------------------------------------------------------------ shell scan

_SH_OWN = [
    re.compile(r'(\w+)=\$?\(?"?\$\(\s*(?:command )?dirname "?\$\{?\1\}?"?\s*\)'),   # d="$(dirname "$d")"
    re.compile(r'(\w+)=\$\(\s*(?:command )?dirname "?\$\{?\1\}?"?\s*\)'),
    re.compile(r'(\w+)="\$\{\1%/\*\}"'),                                           # d="${d%/*}"
]
_SH_FUNC = re.compile(r"^(?:function\s+)?(\w+)\s*\(\)\s*\{")


def scan_shell(root: Path):
    hits: dict[str, list[int]] = {}
    for p in sorted(root.rglob("*.sh")):
        rel = p.relative_to(root)
        if "tests" in rel.parts:
            continue
        fn = "<top>"
        for n, line in enumerate(p.read_text().splitlines(), 1):
            m = _SH_FUNC.match(line)
            if m:
                fn = m.group(1)
            elif line and not line[0].isspace() and line[0] not in "#}":
                fn = "<top>" if not m else fn
            if line.lstrip().startswith("#"):
                continue
            if any(rx.search(line) for rx in _SH_OWN):
                if fn == "_bounded_dirs":
                    continue
                hits.setdefault(f"{rel.as_posix()}:{fn}", []).append(n)
    return hits


# ------------------------------------------------------------------ the guard

def test_every_shipped_python_upward_walk_is_bounded_or_classified():
    hits = scan_python(SRC)
    unclassified = {k: v for k, v in hits.items() if k not in PY_ALLOW}
    assert not unclassified, (
        "an upward walk (.parents, or a `.parent`/dirname loop) that is neither bounded nor "
        f"classified (#3983): {unclassified}. A sibling-runtime lookup must call bounded_parents "
        "(or the cites_step3 fixed window); a repo-root finder or constant is added to PY_ALLOW "
        "with its class and reason.")


def test_every_shipped_shell_upward_walk_is_bounded_or_classified():
    hits = scan_shell(SRC)
    unclassified = {k: v for k, v in hits.items() if k not in SH_ALLOW}
    assert not unclassified, (
        f"a shell upward dirname walk that is neither bounded nor classified (#3983): {unclassified}. "
        "A sibling-runtime lookup must go through _bounded_dirs; anything else is added to SH_ALLOW.")


def test_the_allowlists_have_no_stale_entries():
    """An entry whose walk is gone would rot into a free pass for a future one."""
    py, sh = scan_python(SRC), scan_shell(SRC)
    assert sorted(set(PY_ALLOW) - set(py)) == []
    assert sorted(set(SH_ALLOW) - set(sh)) == []
    for reason in [*PY_ALLOW.values(), *SH_ALLOW.values()]:
        assert re.match(r"[BC]: \S", reason), reason  # class A is never allowlisted


# --------------------------------------------- the guard must be able to go red

UNBOUNDED = {
    "for_parents": "def _x():\n    for p in Path(__file__).resolve().parents:\n        pass\n",
    "extend_parents": "def _x(r):\n    r.extend(Path(__file__).resolve().parents)\n",
    "subscript_parents": "X = Path(__file__).parents[2]\n",
    "while_parent": "def _x(d):\n    while d != d.parent:\n        d = d.parent\n",
    "while_dirname": "import os\ndef _x(d):\n    while d != '/':\n        d = os.path.dirname(d)\n",
}


@pytest.mark.parametrize("spelling", sorted(UNBOUNDED))
def test_the_python_guard_goes_red_on_a_planted_unbounded_walk(spelling, tmp_path):
    root = tmp_path / "engram"
    (root / "tools").mkdir(parents=True)
    shutil.copy(SRC / "tools" / "check_stale_processes.py", root / "tools" / "check_stale_processes.py")
    assert scan_python(root) == {}  # the copy of a bounded shipped file is clean
    f = root / "tools" / "check_stale_processes.py"
    f.write_text(f.read_text() + "\n\n" + UNBOUNDED[spelling])
    hits = scan_python(root)
    assert any(k.startswith("tools/check_stale_processes.py:") for k in hits), (spelling, hits)


@pytest.mark.parametrize("use", [
    "    for d in here.parents:\n        pass\n",
    "    return list(here.parents)\n",
    "    return here.parents[2]\n",
])
def test_a_bare_parents_use_is_flagged_even_where_the_function_also_calls_the_helper(use, tmp_path):
    (tmp_path / "m.py").write_text(
        "def f(here):\n    for p in bounded_parents(here):\n        pass\n" + use)
    assert list(scan_python(tmp_path)) == ["m.py:f"]


def test_the_helper_definitions_are_exempt_for_a_bare_parents_use(tmp_path):
    (tmp_path / "m.py").write_text(
        "\n".join(f"def {n}(here):\n    return list(here.parents)\n" for n in BOUNDED))
    assert scan_python(tmp_path) == {}


def test_the_shell_guard_goes_red_on_a_planted_dirname_loop(tmp_path):
    for body in ('    while [ "$d" != "/" ]; do\n        d="$(dirname "$d")"\n    done\n',
                 '    while [ "$d" != "/" ]; do\n        d="${d%/*}"\n    done\n'):
        (tmp_path / "x.sh").write_text("_find_it() {\n    local d=\"$1\"\n" + body + "}\n")
        assert list(scan_shell(tmp_path)) == ["x.sh:_find_it"], body
    (tmp_path / "x.sh").write_text('_find_it() {\n    d="$(dirname "$other")"\n}\n')
    assert scan_shell(tmp_path) == {}  # not an own-name reassignment


# ---------------------------- the loop rule is structural, not a list of spellings

WRAPPED_WALKS = {
    "resolve_parent": "d = d.resolve().parent",
    "path_of_dirname": "d = Path(os.path.dirname(d))",
    "split_zero": "d = os.path.split(d)[0]",
    "joinpath_dotdot": 'd = d.joinpath("..").resolve()',
    "path_wrapped_parent": "d = Path(d).parent",
    "div_dotdot": 'd = (d / "..").resolve()',
    "rsplit_sep": 'd = d.rsplit("/", 1)[0]',
    "rpartition_sep": 'd = d.rpartition(os.sep)[0]',
    "join_dotdot_normpath": 'd = os.path.normpath(os.path.join(d, ".."))',
    "join_pardir": "d = os.path.join(d, os.pardir)",
    "div_pardir": "d = d / os.pardir",
    "joinpath_pardir": "d = d.joinpath(os.pardir)",
    "div_path_dotdot": 'd = d / Path("..")',
}


@pytest.mark.parametrize("spelling", sorted(WRAPPED_WALKS))
def test_a_wrapped_reassignment_in_a_loop_is_flagged(spelling, tmp_path):
    (tmp_path / "m.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def f(d):\n    while d != '/':\n        " + WRAPPED_WALKS[spelling] + "\n")
    assert list(scan_python(tmp_path)) == ["m.py:f"], spelling


def test_a_function_that_calls_the_helper_elsewhere_does_not_exempt_its_other_loops(tmp_path):
    (tmp_path / "m.py").write_text(
        "def f(here):\n    for p in bounded_parents(here):\n        pass\n"
        "    d = here\n    while d != d.parent:\n        d = d.parent\n")
    assert list(scan_python(tmp_path)) == ["m.py:f"]


def test_the_loop_that_iterates_the_helper_result_is_exempt(tmp_path):
    (tmp_path / "m.py").write_text(
        "def f(here):\n    for d in _bounded_parents(here):\n        d = d.parent\n")
    assert scan_python(tmp_path) == {}


@pytest.mark.parametrize("body", [
    "    for d in bounded_parents(here):\n        use(d)\n",
    "    while d:\n        d = base.parent\n",       # reassigned from an expression without d
    "    while d:\n        x = d.parent\n        d = None\n",  # .parent read, not a reassignment of d
    "    x = d.parent\n",                             # no loop at all
    "    return here.parent\n",                       # singular .parent, no walk
    "    while d:\n        d = d.split(' -> ')[0]\n",   # a string split, not os.path.split
    "    while d:\n        d = os.path.split(d)[1]\n",  # the TAIL of a split, not the parent
    '    while d:\n        name = name.rsplit(".", 1)[0]\n',    # an extension strip, not a path walk
    '    while d:\n        name = name.rpartition(".")[0]\n',
    '    while d:\n        mod = mod.rsplit(".", 1)[0]\n',      # a dotted-module strip
    '    for _ in range(3):\n        name = name.rsplit(".", 1)[0]\n',
    "    while d:\n        d = os.path.join(d, 'sub')\n",        # join without a parent step
])
def test_controls_that_must_stay_green(body, tmp_path):
    (tmp_path / "m.py").write_text("def f(here, d, base):\n" + body)
    assert scan_python(tmp_path) == {}

