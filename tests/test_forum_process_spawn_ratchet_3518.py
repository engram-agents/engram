"""Drift guard: every process spawn on the forum service's executable surface is
named on an allow-list, and each allowed one runs under the gate's identity
(ratchet for #3518).

Which incident this ratchets: #3283. A spec listed three ambient `gh` calls and
missed a fourth (`board_projects.py`, found in a colleague review of #3507)
because nothing enumerated them; #3512 then hand-pinned the gate's call count
against its `**gh_kw` count. This guard replaces the hand-pin with an
enumeration.

The surface (item 1). Every non-test `.py` under `src/forum/` (recursive,
de-duplicated by real path) plus `src/engram/tools/baton_gate.py`, the module
`server._load_baton_gate` loads by file path and executes. A file is a test when
any path part is `tests` or its name is `conftest.py` or starts with `test_`.
Roots are never a fixed `parents[N]` guess: the repo root is the nearest
ancestor of THIS file that holds both `src/forum/` and
`src/engram/tools/baton_gate.py` (a walk-parents search for the target, the
CLAUDE.md plugin-layout rule), so the test depends on the source layout only
through those two paths. A non-vacuity cell asserts the four known sites and a
sane file count, so wrong roots cannot read as green.

Detection (item 2). An AST walk through each file's own imports. A site is a
call OR a bare reference (`executor.submit(subprocess.run, ...)`,
`r = subprocess.run`) that resolves to a spawn API, through aliases
(`import subprocess as sp`, `from subprocess import run as r`,
`from os import system`). Spawn APIs: `subprocess.{run,Popen,call,check_call,
check_output,getoutput,getstatusoutput}`, `os.{system,popen,fork,forkpty,
startfile,exec*,spawn*,posix_spawn*}`, `pty.{spawn,fork}`,
`asyncio.{create_subprocess_exec,create_subprocess_shell}` (also under
`asyncio.subprocess`), `multiprocessing.{Process,Pool,get_context}` and
`multiprocessing.pool.Pool`, `concurrent.futures.ProcessPoolExecutor` (also
under `.process`). Dynamic forms are flagged too, because they hide the name:
`__import__(...)` and `importlib.import_module(...)` of a spawn-capable module
(or of a non-constant name), `getattr(<spawn module>, <spawn name or
non-constant>)`, and `from <spawn module> import *`. NOT sites: non-spawning
attributes (`subprocess.TimeoutExpired`, `subprocess.PIPE`), docstrings,
comments, strings.

Allow-list and ratchet (item 3, the #3413 shape). `(relative path, enclosing
function) -> (exact count, issue ref, reason)`. A site not covered fails; an
entry whose count no longer matches fails (stale; an extra site inside an
allow-listed function fails too, so an entry cannot hide a new one); the total
may only shrink (`_MAX_ALLOWED_SPAWN_SITES` must equal the allow-list's total and
may never exceed the number it had at adoption). The ceiling is process-bound,
not mechanically enforced: raising it takes the constant, an allow-list entry
and a reviewer, three edits that are all visible in a diff.

Allow-listed is not ambient (item 4). Each allow-listed site must be a direct
call carrying a `**N` keyword unpacking where `N` is a plain name assigned, in
the enclosing function, as one element of a tuple-unpack from a call
(`gh_kw, refusal = _gh_identity()`). That is the whole definition of "the
identity helper's kwargs": the NAME is not pinned (a rename stays green), the
SHAPE is. An allow-listed bare reference (no call to inspect) fails.

Surface honesty (item 5). The candidate file names `_load_baton_gate` searches
are derived by reading `server.py` (the tuple of `*baton_gate.py` string
literals inside that function), not pinned here; the file the loader's own
search (first of the six nearest ancestors holding any candidate) picks must be
on the surface. The surface must also contain exactly the file-path module
loaders (`spec_from_file_location`, `SourceFileLoader`, `runpy.run_path`, the
three in `_LOADER_NAMES`) named in `KNOWN_PATH_LOADERS`, so a new runtime-loaded
module that uses one of those three APIs cannot appear unnoticed. One known loader is deliberately OFF the surface: `packs.py` executes
`tools/engram-pkg/engram-pkg` in the service process (see the limits below).

Documented limits (item 6) -- a green guard is NOT "the service cannot spawn":
  * shell units and `ExecStart` lines, `.sh` launchers and any other non-Python
    launcher are not covered;
  * Python modules loaded from outside the declared surface (a plugin
    directory, a `sys.path` entry) are not covered. Known instance:
    `tools/engram-pkg/engram-pkg`, loaded by `packs.py`, holds four
    `subprocess.call(["bash", ...])` sites that are NOT scanned; the guard only
    pins that none sits at module level of that script (syntax, not
    reachability) and that every call to `_load_engram_pkg_cli` under non-test
    `src/forum` touches only `_find_dangling_edges` on the returned module and
    never lets that module escape;
  * a third module loader that is not one of the three in `_LOADER_NAMES` is NOT
    caught: `exec(compile(open(...).read(), ...))`, `runpy.run_module`,
    `imp.load_source`, `importlib.machinery.SourcelessFileLoader`, or a
    `sys.path.insert` followed by a static import;
  * a spawn API missing from the list above (event-loop `subprocess_exec`
    methods, `ctypes`/`cffi` calls into libc, `webbrowser`, `socketserver`
    forking mixins) is not seen, nor is a spawn reached through an object the
    scan cannot resolve (`self.runner.run(...)`, a callable fetched from a
    dict);
  * aliases are resolved per FILE, not per scope, so a local variable that
    rebinds an imported name is read as the import (a name that was never
    imported, such as a parameter called `subprocess`, is not);
  * module-object aliasing by assignment is not flagged (`s = sp; s.run(...)`,
    `mod = subprocess`), nor are `multiprocessing.context.Process`,
    `sys.modules['subprocess']`, `globals()` or `builtins.__import__`;
  * an alias created by assignment (`r = subprocess.run`) is flagged at that
    reference, but later uses of `r` are not separately counted;
  * identity (item 4) is checked by SHAPE: a `**N` where `N` was tuple-unpacked
    from a call. The check is FLOW-INSENSITIVE: the tuple-unpack may come after
    the spawn, sit inside `if False:`, or be reassigned, and it still passes. It
    does not verify that the callee is the identity helper or that the returned
    refusal is honoured.
"""

import ast
import collections
import pathlib
import re
import textwrap

import pytest

# --------------------------------------------------------------------------
# the scanner (pure; parametrised on a root so it can be run on scratch trees)
# --------------------------------------------------------------------------

Site = collections.namedtuple("Site", "rel line func kind what call identity_why")

_SPAWN_EXACT = frozenset({
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput",
    "pty.spawn", "pty.fork",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    "asyncio.subprocess.create_subprocess_exec", "asyncio.subprocess.create_subprocess_shell",
    "multiprocessing.Process", "multiprocessing.Pool", "multiprocessing.get_context",
    "multiprocessing.pool.Pool",
    "concurrent.futures.ProcessPoolExecutor", "concurrent.futures.process.ProcessPoolExecutor",
})
_SPAWN_OS = re.compile(r"^os\.(system|popen|fork|forkpty|startfile|exec\w*|spawn\w*|posix_spawn\w*)$")
# Modules whose dynamic import / `import *` / getattr is flagged.
_SPAWN_MODULES = frozenset({
    "subprocess", "os", "pty", "asyncio", "multiprocessing", "concurrent",
})
_LOADER_NAMES = frozenset({
    "importlib.util.spec_from_file_location", "importlib.machinery.SourceFileLoader",
    "runpy.run_path",
})


def _is_spawn_name(dotted):
    return dotted in _SPAWN_EXACT or bool(_SPAWN_OS.match(dotted))


def _is_test_path(parts):
    name = parts[-1]
    return "tests" in parts[:-1] or name == "conftest.py" or name.startswith("test_")


def find_repo_root(start):
    """Nearest ancestor of `start` holding src/forum/ and the gate module."""
    start = pathlib.Path(start).resolve()
    for d in [start] + list(start.parents):
        if (d / "src" / "forum").is_dir() and (d / "src" / "engram" / "tools" / "baton_gate.py").is_file():
            return d
    raise AssertionError(f"no ancestor of {start} holds src/forum and src/engram/tools/baton_gate.py")


def surface_files(repo_root):
    """[(repo-relative posix path of the REAL file, real path)], sorted, de-duped."""
    repo_root = pathlib.Path(repo_root).resolve()
    found = {}
    forum = repo_root / "src" / "forum"
    cands = []
    if forum.is_dir():
        cands += [p for p in forum.rglob("*.py")
                  if not _is_test_path(p.relative_to(forum).parts)]
    gate = repo_root / "src" / "engram" / "tools" / "baton_gate.py"
    if gate.is_file():
        cands.append(gate)
    for p in cands:
        real = p.resolve()
        try:
            rel = real.relative_to(repo_root).as_posix()
        except ValueError:
            rel = p.relative_to(repo_root).as_posix()
        found.setdefault(real, rel)
    return sorted(((rel, real) for real, rel in found.items()))


def _aliases(tree):
    """Local name -> dotted module/object path, from every import in the file."""
    out, stars = {}, []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    out[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for a in n.names:
                if a.name == "*":
                    stars.append((n, n.module))
                else:
                    out[a.asname or a.name] = n.module + "." + a.name
    return out, stars


def _resolve(node, aliases):
    """Dotted name `node` (a Name / Attribute chain) resolves to through the
    file's imports, else None. A name that was never imported does not resolve."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in aliases:
        return None
    return ".".join([aliases[node.id]] + parts[::-1])


def _spawn_capable_module(dotted):
    return any(dotted == m or dotted.startswith(m + ".") for m in _SPAWN_MODULES)


def _identity_why(call, fnode):
    """None if `call` carries `**N`, N tuple-unpacked from a call in `fnode`."""
    names = [k.value.id for k in call.keywords
             if k.arg is None and isinstance(k.value, ast.Name)]
    if not names:
        return "no `**<name>` keyword unpacking of identity kwargs"
    for n in ast.walk(fnode):
        if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and len(n.targets) == 1 and isinstance(n.targets[0], (ast.Tuple, ast.List))):
            targets = {e.id for e in n.targets[0].elts if isinstance(e, ast.Name)}
            if targets & set(names):
                return None
    return ("`**%s` is not a name tuple-unpacked from a call in the enclosing function"
            % names[0])


def _walk_file(rel, tree):
    aliases, stars = _aliases(tree)
    call_of = {id(c.func): c for c in ast.walk(tree) if isinstance(c, ast.Call)}
    sites = []

    def add(node, fname, fnode, kind, what, call=None):
        why = _identity_why(call, fnode) if call is not None else "bare reference, not a call"
        sites.append(Site(rel, node.lineno, fname, kind, what, call, why))

    for n, mod in stars:
        if _spawn_capable_module(mod):
            add(n, "<module>", tree, "star-import", "from %s import *" % mod)

    def visit(node, fname, fnode):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name, child)
                continue
            if (isinstance(child, (ast.Attribute, ast.Name))
                    and isinstance(child.ctx, ast.Load)):
                dotted = _resolve(child, aliases)
                if dotted and _is_spawn_name(dotted):
                    add(child, fname, fnode, "spawn", dotted, call_of.get(id(child)))
            if isinstance(child, ast.Call):
                f = child.func
                dyn = None
                if isinstance(f, ast.Name) and f.id == "__import__" and f.id not in aliases:
                    dyn = "__import__"
                elif _resolve(f, aliases) == "importlib.import_module":
                    dyn = "importlib.import_module"
                if dyn:
                    a0 = child.args[0] if child.args else None
                    if not (isinstance(a0, ast.Constant) and isinstance(a0.value, str)):
                        add(child, fname, fnode, "dynamic-import", dyn + "(<non-constant>)")
                    elif _spawn_capable_module(a0.value):
                        add(child, fname, fnode, "dynamic-import", "%s(%r)" % (dyn, a0.value))
                if (isinstance(f, ast.Name) and f.id == "getattr" and f.id not in aliases
                        and len(child.args) >= 2):
                    mod = _resolve(child.args[0], aliases)
                    if mod and _spawn_capable_module(mod):
                        a1 = child.args[1]
                        if not (isinstance(a1, ast.Constant) and isinstance(a1.value, str)):
                            add(child, fname, fnode, "dynamic-getattr", "getattr(%s, <non-constant>)" % mod)
                        elif _is_spawn_name(mod + "." + a1.value):
                            add(child, fname, fnode, "dynamic-getattr", "getattr(%s, %r)" % (mod, a1.value))
            visit(child, fname, fnode)

    visit(tree, "<module>", tree)
    return sites


def scan_surface(repo_root):
    """Every spawn site on the surface: [Site]."""
    out = []
    for rel, real in surface_files(repo_root):
        out.extend(_walk_file(rel, ast.parse(real.read_text(encoding="utf-8"))))
    return out


def unaccounted_sites(sites, allow_list):
    """Sites no allow-list entry covers."""
    return [s for s in sites if (s.rel, s.func) not in allow_list]


def entry_mismatches(sites, allow_list):
    """Entries whose exact count does not match the tree: [(key, expected, found)]."""
    found = collections.Counter((s.rel, s.func) for s in sites)
    return [(key, spec[0], found.get(key, 0))
            for key, spec in allow_list.items() if found.get(key, 0) != spec[0]]


def identity_problems(sites, allow_list):
    """Allow-listed sites that are not a call under the identity kwargs:
    [(rel, func, line, why)]."""
    return [(s.rel, s.func, s.line, s.identity_why) for s in sites
            if (s.rel, s.func) in allow_list and s.identity_why is not None]


def server_gate_candidates(server_path):
    """The `*baton_gate.py` string literals in the tuple inside `_load_baton_gate`."""
    tree = ast.parse(pathlib.Path(server_path).read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "_load_baton_gate":
            for t in ast.walk(fn):
                if (isinstance(t, ast.Tuple) and t.elts
                        and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                                and e.value.endswith("baton_gate.py") for e in t.elts)):
                    return [e.value for e in t.elts]
    return []


def find_path_loaders(repo_root):
    """File-path module loaders on the surface: [(rel, line, function)]."""
    out = []
    for rel, real in surface_files(repo_root):
        tree = ast.parse(real.read_text(encoding="utf-8"))
        aliases, _stars = _aliases(tree)

        def visit(node, fname):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name)
                    continue
                if isinstance(child, (ast.Attribute, ast.Name)) and isinstance(child.ctx, ast.Load):
                    if _resolve(child, aliases) in _LOADER_NAMES:
                        out.append((rel, child.lineno, fname))
                visit(child, fname)

        visit(tree, "<module>")
    return out


_LOADER_FN = "_load_engram_pkg_cli"
_PURE_CHECKER = frozenset({"_find_dangling_edges"})


def _forum_nontest_files(repo_root):
    forum = pathlib.Path(repo_root).resolve() / "src" / "forum"
    return sorted(p for p in forum.rglob("*.py")
                  if not _is_test_path(p.relative_to(forum).parts))


def _loader_call_reports(repo_root):
    """For every call to the engram-pkg loader under non-test src/forum:
    (rel, line, attributes accessed on the returned module, escape description
    or None). A bare reference to the loader (not called) is an escape."""
    root = pathlib.Path(repo_root).resolve()
    reports = []
    for path in _forum_nontest_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(root).as_posix()
        parent = {}
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                parent[c] = n

        def enclosing(node):
            while node in parent:
                node = parent[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return node
            return tree

        for n in ast.walk(tree):
            if not (isinstance(n, ast.Name) and n.id == _LOADER_FN and isinstance(n.ctx, ast.Load)):
                continue
            call = parent.get(n)
            if not (isinstance(call, ast.Call) and call.func is n):
                reports.append((rel, n.lineno, set(), "the loader is referenced without being called"))
                continue
            up = parent.get(call)
            if isinstance(up, ast.Attribute) and up.value is call:
                reports.append((rel, n.lineno, {up.attr}, None))  # _load_...().attr
                continue
            if not (isinstance(up, ast.Assign) and up.value is call and len(up.targets) == 1
                    and isinstance(up.targets[0], ast.Name)):
                reports.append((rel, n.lineno, set(), "the returned module escapes (it is not bound to a plain name)"))
                continue
            name = up.targets[0].id
            attrs, escape = set(), None
            for u in ast.walk(enclosing(call)):
                if isinstance(u, ast.Name) and u.id == name and isinstance(u.ctx, ast.Load):
                    holder = parent.get(u)
                    if isinstance(holder, ast.Attribute) and holder.value is u:
                        attrs.add(holder.attr)
                    else:
                        escape = "the returned module escapes through `%s` (line %d)" % (name, u.lineno)
            reports.append((rel, n.lineno, attrs, escape))
    return reports


def loader_call_sites(repo_root):
    """Number of calls/references to the loader under non-test src/forum."""
    return len(_loader_call_reports(repo_root))


def loader_call_problems(repo_root):
    """Every loader call site must touch exactly the pure checker on the returned
    module and must not let the module escape (an escaped module has an
    unknowable attribute set). Each call site is pinned on its own."""
    out = []
    for rel, line, attrs, escape in _loader_call_reports(repo_root):
        if escape:
            out.append("%s:%d: %s, so its attribute set is unknowable" % (rel, line, escape))
        elif attrs != _PURE_CHECKER:
            out.append("%s:%d: engram-pkg attributes used %s, expected %s"
                       % (rel, line, sorted(attrs), sorted(_PURE_CHECKER)))
    return out


# --------------------------------------------------------------------------
# the allow-list
# --------------------------------------------------------------------------

_GATE = "src/engram/tools/baton_gate.py"
_BOARD = "src/forum/board_projects.py"

ALLOW_LIST = {
    (_BOARD, "_gh_pr_state"):
        (1, "#3509", "the board's PR-state read; env comes from the injected identity provider, no identity -> no call"),
    (_GATE, "_pr_ci_state"):
        (3, "#3533", "CI-state read for the flip gate, under the service identity: the check-run rollup (ambient login only) plus, for the fine-grained service token, the merge-state read and the workflow-runs read"),
    (_GATE, "_gh_read_json"):
        (1, "#3541", "the one private reader the two approval reads share (gh pr view --json headRefOid,author, and gh api pulls/<N>/reviews); it takes the identity itself, so every approval-state call runs under the service identity"),
}

# File-path module loaders on the surface (item 5), by (path, function). The
# gate loader's target is ON the surface. The packs loader executes
# tools/engram-pkg/engram-pkg in the service process and is deliberately OFF it
# (a documented limit: that script has four `subprocess.call(["bash", ...])`
# sites, all inside functions the service never calls; the loader-call cell pins that
# the forum uses only its pure `_find_dangling_edges`). Extending the surface to
# it is a design choice for the issue, not something this guard decides.
KNOWN_PATH_LOADERS = {
    ("src/forum/server.py", "_load_baton_gate"): "on the surface (item 5 cell)",
    ("src/forum/packs.py", "_load_engram_pkg_cli"): "OFF the surface: tools/engram-pkg/engram-pkg",
}

# The ratchet. LOWER this (never raise it) when a PR routes a spawn away and
# deletes/lowers its entry. Adoption value: 4; raised to 6 once, deliberately, by
# #3533 (the fine-grained service token cannot read the check-run rollup, so
# `_pr_ci_state` gained two gh calls, both under the identity kwargs); lowered
# to 5 by #3541 (the two approval reads each lost their own spawn site and now
# share ONE private REST reader: 6 - 2 + 1).
_MAX_ALLOWED_SPAWN_SITES = 5
_ADOPTION_CEILING = 5


# --------------------------------------------------------------------------
# helpers for scratch trees
# --------------------------------------------------------------------------

def _tree(tmp_path, files):
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src), encoding="utf-8")
    return tmp_path


def _scan_one(tmp_path, src, rel="src/forum/mod.py"):
    return scan_surface(_tree(tmp_path, {rel: src}))


@pytest.fixture(scope="module")
def repo_root():
    return find_repo_root(pathlib.Path(__file__).resolve())


@pytest.fixture(scope="module")
def real_sites(repo_root):
    return scan_surface(repo_root)


# --------------------------------------------------------------------------
# non-vacuity
# --------------------------------------------------------------------------

def test_repo_root_is_found_by_walking_up_for_the_target_files(repo_root):
    assert (repo_root / "src" / "forum").is_dir()
    assert (repo_root / _GATE).is_file()
    assert pathlib.Path(__file__).resolve().is_relative_to(repo_root)


def test_real_surface_is_not_vacuous(repo_root):
    files = surface_files(repo_root)
    rels = {rel for rel, _p in files}
    assert len(files) >= 15, f"suspiciously small surface: {sorted(rels)}"
    assert _GATE in rels
    assert _BOARD in rels
    assert "src/forum/server.py" in rels
    assert not [r for r in rels if "/tests/" in r or r.rsplit("/", 1)[-1].startswith("test_")]


def test_real_tree_sees_exactly_the_four_known_sites(real_sites):
    found = collections.Counter((s.rel, s.func) for s in real_sites)
    assert found == collections.Counter({
        (_BOARD, "_gh_pr_state"): 1,
        (_GATE, "_pr_ci_state"): 3,
        (_GATE, "_gh_read_json"): 1,
    })


def test_surface_dedupes_by_real_path(tmp_path):
    root = _tree(tmp_path, {
        "src/forum/a.py": "import subprocess\ndef f():\n    subprocess.run(['x'])\n",
        "src/engram/tools/baton_gate.py": "X = 1\n",
    })
    (root / "src" / "forum" / "alias.py").symlink_to(root / "src" / "forum" / "a.py")
    sites = scan_surface(root)
    assert len(sites) == 1, sites


def test_scratch_tests_dirs_and_test_files_are_not_on_the_surface(tmp_path):
    root = _tree(tmp_path, {
        "src/forum/tests/test_x.py": "import subprocess\nsubprocess.run(['x'])\n",
        "src/forum/test_y.py": "import subprocess\nsubprocess.run(['x'])\n",
        "src/forum/conftest.py": "import subprocess\nsubprocess.run(['x'])\n",
        "src/forum/sub/real.py": "import subprocess\nsubprocess.run(['x'])\n",
    })
    assert [s.rel for s in scan_surface(root)] == ["src/forum/sub/real.py"]


# --------------------------------------------------------------------------
# detection (item 2)
# --------------------------------------------------------------------------

_POSITIVE = {
    "run": "import subprocess\ndef f():\n    subprocess.run(['x'])\n",
    "Popen": "import subprocess\ndef f():\n    subprocess.Popen(['x'])\n",
    "call": "import subprocess\ndef f():\n    subprocess.call(['x'])\n",
    "check_call": "import subprocess\ndef f():\n    subprocess.check_call(['x'])\n",
    "check_output": "import subprocess\ndef f():\n    subprocess.check_output(['x'])\n",
    "getoutput": "import subprocess\ndef f():\n    subprocess.getoutput('x')\n",
    "getstatusoutput": "import subprocess\ndef f():\n    subprocess.getstatusoutput('x')\n",
    "alias-import-as": "import subprocess as sp\ndef f():\n    sp.run(['x'])\n",
    "alias-from-as": "from subprocess import run as r\ndef f():\n    r(['x'])\n",
    "alias-from-plain": "from subprocess import Popen\ndef f():\n    Popen(['x'])\n",
    "alias-in-function-import": "def f():\n    import subprocess as q\n    q.run(['x'])\n",
    "os.system": "import os\ndef f():\n    os.system('x')\n",
    "os.system-from": "from os import system\ndef f():\n    system('x')\n",
    "os.popen": "import os\ndef f():\n    os.popen('x')\n",
    "os.popen-alias": "import os as o\ndef f():\n    o.popen('x')\n",
    "os.execv": "import os\ndef f():\n    os.execv('/bin/x', ['x'])\n",
    "os.execvp": "import os\ndef f():\n    os.execvp('x', ['x'])\n",
    "os.spawnl": "import os\ndef f():\n    os.spawnl(os.P_WAIT, '/bin/x', 'x')\n",
    "os.posix_spawn": "import os\ndef f():\n    os.posix_spawn('/bin/x', ['x'], {})\n",
    "os.fork": "import os\ndef f():\n    os.fork()\n",
    "os.forkpty": "import os\ndef f():\n    os.forkpty()\n",
    "pty.spawn": "import pty\ndef f():\n    pty.spawn(['x'])\n",
    "pty.fork": "import pty\ndef f():\n    pty.fork()\n",
    "asyncio.exec": "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('x')\n",
    "asyncio.shell": "import asyncio\nasync def f():\n    await asyncio.create_subprocess_shell('x')\n",
    "multiprocessing.Process": "import multiprocessing\ndef f():\n    multiprocessing.Process(target=f)\n",
    "multiprocessing.Pool": "import multiprocessing\ndef f():\n    multiprocessing.Pool(2)\n",
    "multiprocessing.Pool-from": "from multiprocessing import Pool\ndef f():\n    Pool(2)\n",
    "futures.ProcessPoolExecutor-dotted": "import concurrent.futures\ndef f():\n    concurrent.futures.ProcessPoolExecutor()\n",
    "futures.ProcessPoolExecutor-from-as": "from concurrent.futures import ProcessPoolExecutor as P\ndef f():\n    P()\n",
    "futures-module-from": "from concurrent import futures\ndef f():\n    futures.ProcessPoolExecutor()\n",
    "ref-as-argument": "import subprocess\ndef f(ex):\n    ex.submit(subprocess.run, ['x'])\n",
    "ref-as-argument-from-alias": "from subprocess import run\ndef f(ex):\n    ex.submit(run, ['x'])\n",
    "ref-assigned": "import subprocess\nr = subprocess.run\n",
    "ref-partial": "import functools, subprocess\ndef f():\n    return functools.partial(subprocess.run, ['x'])\n",
    "dynamic-__import__": "def f():\n    m = __import__('subprocess')\n    return m\n",
    "dynamic-import_module": "import importlib\ndef f():\n    return importlib.import_module('subprocess')\n",
    "dynamic-import_module-from": "from importlib import import_module\ndef f():\n    return import_module('os')\n",
    "dynamic-import-nonconstant": "def f(name):\n    return __import__(name)\n",
    "dynamic-getattr": "import subprocess\ndef f():\n    return getattr(subprocess, 'run')\n",
    "dynamic-getattr-nonconstant": "import os\ndef f(n):\n    return getattr(os, n)\n",
    "star-import": "from subprocess import *\n",
}

_NEGATIVE = {
    "TimeoutExpired": "import subprocess\ndef f():\n    try:\n        pass\n    except subprocess.TimeoutExpired:\n        pass\n",
    "PIPE": "import subprocess\nX = subprocess.PIPE\n",
    "CalledProcessError": "import subprocess\nclass E(subprocess.CalledProcessError):\n    pass\n",
    "docstring": 'import subprocess\ndef f():\n    """Calls subprocess.run( and os.system(."""\n',
    "comment": "import subprocess\n# subprocess.run(['x']) os.system('x')\nX = 1\n",
    "string": "X = 'subprocess.run(os.system)'\nY = [\"gh\", \"pr\"]\n",
    "os.path": "import os\ndef f():\n    return os.path.join('a', 'b') + os.getcwd()\n",
    "from-os-path": "from os import path\nX = path.join('a')\n",
    "self-attribute": "def f(self):\n    self.subprocess.run(['x'])\n",
    "unimported-name": "def f(run):\n    run(['x'])\n",
    "parameter-named-subprocess": "def f(subprocess):\n    subprocess.run(['x'])\n",
    "local-named-subprocess": "def f():\n    subprocess = something()\n    subprocess.run(['x'])\n",
    "json": "import json\ndef f():\n    return json.loads('1')\n",
    "getattr-non-spawn": "import subprocess\nX = getattr(subprocess, 'PIPE')\n",
    "import-json-dynamic": "X = __import__('json')\n",
    "import-os-only": "import os\nimport subprocess\n",
    "from-import-only-nonspawn": "from subprocess import PIPE, TimeoutExpired\nX = PIPE\n",
    "futures-thread-pool": "import concurrent.futures\ndef f():\n    concurrent.futures.ThreadPoolExecutor()\n",
}


@pytest.mark.parametrize("name", sorted(_POSITIVE))
def test_detects_spawn_form(tmp_path, name):
    sites = _scan_one(tmp_path, _POSITIVE[name])
    assert len(sites) == 1, (name, sites)


@pytest.mark.parametrize("name", sorted(_NEGATIVE))
def test_does_not_flag_non_spawn_form(tmp_path, name):
    assert _scan_one(tmp_path, _NEGATIVE[name]) == []


def test_site_names_the_innermost_enclosing_function_and_call_vs_reference(tmp_path):
    sites = _scan_one(tmp_path, """
        import subprocess
        def outer():
            def inner():
                subprocess.run(['x'])
            return inner
        def ref(ex):
            ex.submit(subprocess.run)
        subprocess.call(['y'])
    """)
    by = {(s.func, s.call is not None) for s in sites}
    assert by == {("inner", True), ("ref", False), ("<module>", True)}


# --------------------------------------------------------------------------
# allow-list accounting (item 3)
# --------------------------------------------------------------------------

_ONE = {("src/forum/mod.py", "f"): (1, "#1", "r")}

_F = "import subprocess\ndef f():\n    subprocess.run(['x'])\n"
_F2 = "import subprocess\ndef f():\n    subprocess.run(['x'])\n    subprocess.run(['y'])\n"
_FG = "import subprocess\ndef f():\n    subprocess.run(['x'])\ndef g():\n    subprocess.run(['y'])\n"


def test_real_tree_has_no_unaccounted_site(real_sites):
    bad = unaccounted_sites(real_sites, ALLOW_LIST)
    assert not bad, f"unaccounted process-spawn site(s): {[(s.rel, s.line, s.func, s.what) for s in bad]}"


def test_real_tree_allow_list_has_no_stale_or_hiding_entry(real_sites):
    assert entry_mismatches(real_sites, ALLOW_LIST) == []


def test_an_uncovered_site_is_reported(tmp_path):
    sites = _scan_one(tmp_path, _FG)
    bad = unaccounted_sites(sites, _ONE)
    assert [(s.func) for s in bad] == ["g"]


def test_a_stale_entry_is_reported(tmp_path):
    sites = _scan_one(tmp_path, "X = 1\n")
    assert entry_mismatches(sites, _ONE) == [(("src/forum/mod.py", "f"), 1, 0)]


def test_a_hidden_extra_site_inside_an_allow_listed_function_is_reported(tmp_path):
    sites = _scan_one(tmp_path, _F2)
    assert unaccounted_sites(sites, _ONE) == []
    assert entry_mismatches(sites, _ONE) == [(("src/forum/mod.py", "f"), 1, 2)]


def test_ceiling_may_only_shrink():
    total = sum(spec[0] for spec in ALLOW_LIST.values())
    assert total == _MAX_ALLOWED_SPAWN_SITES, (
        "the ceiling must equal the allow-list's total: lower it when a spawn is "
        "routed away, never raise it to admit a new one")
    assert _MAX_ALLOWED_SPAWN_SITES <= _ADOPTION_CEILING


def test_every_entry_has_an_issue_ref_and_a_reason():
    for key, (count, ref, reason) in ALLOW_LIST.items():
        assert isinstance(count, int) and count >= 1, key
        assert re.fullmatch(r"#\d+", ref), (key, ref)
        assert len(reason.strip()) >= 20, key


# --------------------------------------------------------------------------
# allow-listed is not ambient (item 4)
# --------------------------------------------------------------------------

_ID = "src/forum/mod.py"


def _identity(tmp_path, body):
    sites = _scan_one(tmp_path, "import subprocess\n" + textwrap.dedent(body))
    return identity_problems(sites, _ONE)


def test_real_tree_allow_listed_spawns_all_carry_the_identity_kwargs(real_sites):
    assert identity_problems(real_sites, ALLOW_LIST) == []


def test_identity_tuple_unpacked_from_a_call_passes(tmp_path):
    assert _identity(tmp_path, """
        def f():
            gh_kw, refusal = ident()
            subprocess.run(['x'], **gh_kw)
    """) == []


def test_identity_name_is_not_pinned_only_the_shape(tmp_path):
    assert _identity(tmp_path, """
        def f():
            env_kw, _r = ident()
            subprocess.run(['x'], **env_kw)
    """) == []


def test_identity_unpack_inside_an_if_passes(tmp_path):
    assert _identity(tmp_path, """
        def f(provider):
            gh_kw: dict = {}
            if provider is not None:
                gh_kw, refusal = provider()
            subprocess.run(['x'], **gh_kw)
    """) == []


def test_allow_listed_call_without_kwargs_unpacking_fails(tmp_path):
    bad = _identity(tmp_path, """
        def f():
            gh_kw, refusal = ident()
            subprocess.run(['x'])
    """)
    assert len(bad) == 1 and bad[0][:2] == (_ID, "f")


def test_unpacking_a_name_not_tuple_unpacked_from_a_call_fails(tmp_path):
    assert len(_identity(tmp_path, """
        def f(other_name):
            subprocess.run(['x'], **other_name)
    """)) == 1


def test_unpacking_a_name_assigned_plainly_fails(tmp_path):
    assert len(_identity(tmp_path, """
        def f():
            kw = {}
            subprocess.run(['x'], **kw)
    """)) == 1


def test_unpacking_a_literal_dict_fails(tmp_path):
    assert len(_identity(tmp_path, """
        def f():
            gh_kw, refusal = ident()
            subprocess.run(['x'], **{"env": {}})
    """)) == 1


def test_tuple_unpack_in_another_function_does_not_count(tmp_path):
    assert len(_identity(tmp_path, """
        def other():
            gh_kw, refusal = ident()
        def f():
            subprocess.run(['x'], **gh_kw)
    """)) == 1


def test_allow_listed_bare_reference_fails(tmp_path):
    assert len(_identity(tmp_path, """
        def f(ex):
            gh_kw, refusal = ident()
            ex.submit(subprocess.run, ['x'])
    """)) == 1


def test_non_allow_listed_sites_are_not_judged_by_item_4(tmp_path):
    sites = _scan_one(tmp_path, _FG)
    assert identity_problems(sites, _ONE) != []  # f lacks it ...
    assert all(p[1] == "f" for p in identity_problems(sites, _ONE))  # ... g is item 3's concern


def test_a_bare_run_planted_in_a_copy_of_board_projects_is_unaccounted(repo_root, tmp_path):
    src = (repo_root / _BOARD).read_text(encoding="utf-8")
    src += "\n\ndef _planted():\n    return subprocess.run([\"gh\", \"pr\", \"view\"])\n"
    root = _tree(tmp_path, {_BOARD: src})
    bad = unaccounted_sites(scan_surface(root), ALLOW_LIST)
    assert [(s.rel, s.func) for s in bad] == [(_BOARD, "_planted")]


def test_removing_kwargs_from_a_copy_of_each_real_allow_listed_call_fails(repo_root, tmp_path):
    board = (repo_root / _BOARD).read_text(encoding="utf-8")
    gate = (repo_root / _GATE).read_text(encoding="utf-8")
    assert board.count("            **gh_kw,\n") == 1
    assert gate.count("**gh_kw") == 4
    mutated_board = board.replace("            **gh_kw,\n", "")
    mutated_gate = gate.replace("timeout=15, **gh_kw)", "timeout=15)")
    assert mutated_board != board and mutated_gate != gate, "mutation did not apply (vacuous control)"
    root = _tree(tmp_path, {_BOARD: mutated_board, _GATE: mutated_gate})
    bad = {(p[0], p[1]) for p in identity_problems(scan_surface(root), ALLOW_LIST)}
    assert bad == {(_BOARD, "_gh_pr_state"), (_GATE, "_gh_read_json")}


# --------------------------------------------------------------------------
# surface honesty (item 5)
# --------------------------------------------------------------------------

def test_the_gate_file_server_loads_is_on_the_scan_surface(repo_root):
    server = repo_root / "src" / "forum" / "server.py"
    candidates = server_gate_candidates(server)
    assert candidates, "could not derive the gate's candidate names from server.py"
    surface = {p.resolve() for _rel, p in surface_files(repo_root)}
    # Mirror the loader's own search: the first of server.py's six nearest
    # ancestors holding any candidate wins, candidates tried in literal order.
    loaded = None
    for base in list(server.resolve().parents)[:6]:
        hit = next((base / rel for rel in candidates if (base / rel).is_file()), None)
        if hit is not None:
            loaded = hit.resolve()
            break
    assert loaded is not None, "none of the gate's candidate paths exists above server.py"
    assert loaded in surface, f"runtime-loaded but off the surface: {loaded}"
    assert loaded == (repo_root / _GATE).resolve()


def test_candidate_derivation_reads_the_tuple_literal_in_load_baton_gate(tmp_path):
    p = tmp_path / "server.py"
    p.write_text(textwrap.dedent("""
        def other():
            for rel in ("a/not_it.py", "b/not_it.py"):
                pass
        def _load_baton_gate():
            for rel in ("tools/baton_gate.py", "src/engram/tools/baton_gate.py"):
                pass
    """), encoding="utf-8")
    assert server_gate_candidates(p) == ["tools/baton_gate.py", "src/engram/tools/baton_gate.py"]


def test_the_surface_has_exactly_the_known_file_path_module_loaders(repo_root):
    loaders = find_path_loaders(repo_root)
    assert sorted({(rel, fn) for rel, _l, fn in loaders}) == sorted(KNOWN_PATH_LOADERS), (
        "a file-path module loader appeared (or went away): decide whether the "
        "module it loads belongs on the scan surface, then update KNOWN_PATH_LOADERS")


def test_known_off_surface_loader_target_has_no_spawn_syntax_at_module_level(repo_root):
    """Checks SYNTAX only: packs.py executes tools/engram-pkg/engram-pkg in the
    service process, and none of that script's spawn sites sits at module level
    (nothing spawns by merely loading it). It is not a reachability check: a
    module-level call to a function that spawns would pass."""
    script = repo_root / "tools" / "engram-pkg" / "engram-pkg"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    module_level = [s for s in _walk_file("engram-pkg", tree) if s.func == "<module>"]
    assert module_level == []


def test_real_forum_calls_only_the_pure_checker_through_the_loader(repo_root):
    problems = loader_call_problems(repo_root)
    assert problems == [], problems
    assert loader_call_sites(repo_root) >= 1, "no call to the loader found: vacuous pin"


_PACKS = "src/forum/packs.py"


def _planted_packs(repo_root, tmp_path, extra):
    src = (repo_root / _PACKS).read_text(encoding="utf-8") + "\n\n" + textwrap.dedent(extra)
    return _tree(tmp_path, {_PACKS: src})


def test_loader_pin_follows_call_sites_not_the_name_cli(repo_root, tmp_path):
    root = _planted_packs(repo_root, tmp_path, """
        def _planted():
            _m = _load_engram_pkg_cli()
            _m.cmd_build(None)
    """)
    problems = loader_call_problems(root)
    assert len(problems) == 1 and "cmd_build" in problems[0], problems


def test_loader_pin_fails_when_the_returned_module_escapes_by_return(repo_root, tmp_path):
    root = _planted_packs(repo_root, tmp_path, """
        def _planted():
            return _load_engram_pkg_cli()
    """)
    problems = loader_call_problems(root)
    assert len(problems) == 1 and "escapes" in problems[0], problems


@pytest.mark.parametrize("body", [
    "m = _load_engram_pkg_cli()\n    use(m)",
    "m = _load_engram_pkg_cli()\n    holder.mod = m",
    "m = _load_engram_pkg_cli()\n    box = [m]",
    "use(_load_engram_pkg_cli())",
    "a, b = _load_engram_pkg_cli()",
    "f = _load_engram_pkg_cli",
])
def test_loader_pin_fails_for_other_escape_shapes(repo_root, tmp_path, body):
    root = _planted_packs(repo_root, tmp_path, "def _planted():\n    " + body + "\n")
    assert loader_call_problems(root) != []


def test_loader_pin_second_call_site_is_pinned_per_site(repo_root, tmp_path):
    """A second call site is allowed only if it too touches exactly the pure
    checker (each site is pinned on its own); any other attribute fails."""
    ok = _planted_packs(repo_root, tmp_path / "ok", """
        def _planted():
            c = _load_engram_pkg_cli()
            return c._find_dangling_edges([], [])
    """)
    assert loader_call_problems(ok) == []
    bad = _planted_packs(repo_root, tmp_path / "bad", """
        def _planted():
            c = _load_engram_pkg_cli()
            return c._find_dangling_edges([], []), c.cmd_build
    """)
    assert len(loader_call_problems(bad)) == 1


def test_loader_pin_ignores_test_files(repo_root, tmp_path):
    root = _tree(tmp_path, {"src/forum/tests/test_x.py": "m = _load_engram_pkg_cli()\nm.cmd_build()\n"})
    assert loader_call_problems(root) == []


def test_a_second_file_path_loader_is_detected(tmp_path):
    root = _tree(tmp_path, {
        "src/forum/a.py": "import importlib.util\ndef g(p):\n    return importlib.util.spec_from_file_location('m', p)\n",
        "src/forum/b.py": "from importlib.util import spec_from_file_location as s\ndef h(p):\n    return s('m', p)\n",
        "src/forum/c.py": "import runpy\ndef k(p):\n    return runpy.run_path(p)\n",
    })
    assert sorted(find_path_loaders(root)) == [
        ("src/forum/a.py", 3, "g"), ("src/forum/b.py", 3, "h"), ("src/forum/c.py", 3, "k")]


def test_dropping_the_gate_from_the_surface_would_be_seen(repo_root, tmp_path):
    """The honesty and non-vacuity cells key off the gate: a scratch surface with
    only src/forum has none of the three gate sites, and the four-site check
    above would differ."""
    root = _tree(tmp_path, {_BOARD: (repo_root / _BOARD).read_text(encoding="utf-8")})
    found = {(s.rel, s.func) for s in scan_surface(root)}
    assert (_GATE, "_pr_ci_state") not in found and (_BOARD, "_gh_pr_state") in found


# --------------------------------------------------------------------------
# more detection controls named by the issue
# --------------------------------------------------------------------------

def test_a_bare_run_planted_in_a_scratch_module_is_unaccounted(tmp_path):
    sites = _scan_one(tmp_path, "import subprocess\ndef g():\n    subprocess.run(['gh', 'pr', 'view'])\n",
                      rel="src/forum/newmod.py")
    assert [(s.rel, s.func) for s in unaccounted_sites(sites, ALLOW_LIST)] == [("src/forum/newmod.py", "g")]


def test_an_aliased_import_added_to_a_new_forum_module_is_unaccounted(tmp_path):
    sites = _scan_one(tmp_path, "import subprocess as sp\ndef g():\n    sp.run(['x'])\n",
                      rel="src/forum/sub/newmod.py")
    assert len(unaccounted_sites(sites, ALLOW_LIST)) == 1
