"""Ratchet for #4380: no NEW call under src/engram/ truncates or writes text
through a path name without being on the committed baseline.

SCOPE (stated once): every non-test `.py` under `src/engram/`, recursive,
de-duplicated by real path. A file is a test when any path part is `tests` or
its name is `conftest.py` or starts with `test_`. Stdlib + pytest only (the
pr-checks invariant job installs nothing else).

Why a ratchet. `open(p, "w")`, `Path.write_text` and `os.open(.., O_TRUNC)` all
truncate BEFORE anything can check what the path is, and all follow a symlink
(or truncate a hard-linked file). The strict family in
`engram_regular_io` (`_open_single_regular`, `write_regular_bytes_synced`,
`open_regular_text_write`) never passes O_TRUNC at open: it validates the
descriptor, then ftruncates. Fixing the sites one by one is how the class kept
producing members nobody noticed; this test turns the property into a check.

What counts as a site (a CALL expression; docstrings, comments and strings never
count). Routes:
  * `os.open:O_TRUNC`     `os.open(...)` whose flags argument contains O_TRUNC
                          ANYWHERE in the expression (the flags are a BinOp
                          `A | B | O_TRUNC`; a bare attribute check on the
                          argument node is inert on that, which is exactly the
                          bug a first draft of this property had);
  * `open:w`              BUILTIN `open(...)` with a constant mode containing `w`;
  * `Module.open:w`       `io|gzip|bz2|lzma|tarfile|zipfile|codecs .open(path, mode)`
                          (mode is the SECOND argument) with a constant mode
                          containing `w`: `gzip.open(p, "wb")` truncates and follows
                          a link exactly as `open(p, "wb")` does;
  * `Path.open:w`         `<x>.open(...)` with a constant mode containing `w`;
  * `write_text`, `write_bytes`  `<x>.write_text(` / `<x>.write_bytes(`;
  * `open:nonconstant-mode`, `Path.open:nonconstant-mode`,
    `Module.open:nonconstant-mode`
                          any open form whose mode is not a constant string: a
                          FINDING, never a skip ("uncountable" is not an answer
                          a property check may accept quietly).
  * `os.open:O_TRUNC` also covers flags HELD IN A NAME: a bare Name in the flags
    argument is resolved inside the enclosing function (any assignment or `|=` to that
    name, followed transitively through the names those values hold), and an O_TRUNC in
    any of them makes the call a site;
  * `os.open:nonconst-flags`  a Name in the flags that cannot be resolved inside the
                          function (a parameter, a module-level constant, an outer-scope
                          value), UNLESS the flags expression itself literally contains
                          O_NOFOLLOW (that exemption keeps the strict opener
                          `_open_single_regular`, `flags | O_NOFOLLOW | O_NONBLOCK`, off the
                          ledger; a `flags | O_NONBLOCK` opener is a finding);
  * `os.truncate`         `os.truncate(path, n)` follows a link;
  * `shutil.copy*`        `shutil.copyfile` / `copy` / `copy2`: the destination follows a
                          symlink;
  * `open:a`              builtin `open(p, "a*")` and `<x>.open(p, "a*")` (and the
                          `Module.open` forms): an append writes INTO a link's target.
Aliases are resolved within the module before classifying: `import os as _os`,
`from os import open [as X]`, `from shutil import copyfile [as X]`, and module-level
`X = open` / `X = os.open`. `builtins.open` is the builtin.

STATED BLIND SPOTS (the collector is a ratchet on the common shapes, not a proof):
  * flags built across functions (a helper returns them, a caller passes them in and the
    callee has no O_NOFOLLOW is flagged, but flags computed by a CALL expression,
    `os.open(p, make_flags())`, or read from a container/attribute, are not followed);
  * a name rebound by `global`/`nonlocal`, by tuple unpacking, `with ... as`, a `for`
    target or a walrus is not seen as an assignment;
  * `getattr(os, "open")(...)`, `__import__("os").open(...)`, `importlib` and any other
    dynamic lookup of the callee; `functools.partial(os.open, ...)`; a callable stored in
    a dict, attribute or argument and called later;
  * an alias made INSIDE a function (`o = os.open` in a body), or by assignment to an
    attribute, or a name rebound after the module-level alias;
  * `subprocess` / shell redirection (`>`, `tee`), `sqlite3`, `tempfile.mkstemp` and
    `os.replace`/`os.rename` onto a link, `Path.touch`, `os.utime`, `os.chmod`;
  * `mode="w"` given as an f-string/Name IS a finding (non-constant mode), but a mode
    passed positionally past a `*args` is not seen;
  * a non-allowlisted opener class (for example `tarfile.TarFile.open`, `zipfile.ZipFile(p,
    "w")` as a constructor, `gzip.GzipFile(p, "wb")`, `shelve.open`, `dbm.open`) is not a
    route;
  * `.write_text` / `.write_bytes` are matched by attribute name on ANY object, so a
    non-Path `.write_text` is a (harmless) finding and a differently named writer is not;
  * `os.open` with CONSTANT write flags and no `O_TRUNC` (for example
    `O_WRONLY|O_APPEND|O_CREAT` without `O_NOFOLLOW`, the append-through-a-link shape the
    old reply-gate log had) is not a route, and neither is builtin `open(p, "r+")`;
  * a literal `O_NOFOLLOW` in the flags exempts the call even when an `O_TRUNC` comes in
    through a module-level constant or a parameter default.
A site here being read-only is a false positive the baseline records with a comment; a
site missing from the list above is invisible.

The baseline is `tests/data/truncating_writes_baseline_4380.txt`: one line per
site, `relative/path.py::<qualified function>::<route>` (no line numbers, so an
edit elsewhere does not churn it; two identical sites in one function are two
identical lines). The test FAILS on a site not in the baseline, and FAILS on a
baseline line with no matching site, so the baseline only shrinks as sites are
fixed and can never grow silently. A site fixed by this change (the two helper
copies, the two survey writers, `_rewrite_private`, the backup-push status tmp)
is NOT in the baseline.

Planted controls (below) run the SAME collector on a snippet holding one of each
route plus negatives; a collector that reports nothing must be able to fail.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOT = PROJECT_ROOT / "src" / "engram"
BASELINE = PROJECT_ROOT / "tests" / "data" / "truncating_writes_baseline_4380.txt"

R_OS_TRUNC = "os.open:O_TRUNC"
R_OPEN_W = "open:w"
R_PATH_OPEN_W = "Path.open:w"
R_MOD_OPEN_W = "Module.open:w"
R_WRITE_TEXT = "write_text"
R_WRITE_BYTES = "write_bytes"
R_OS_NONCONST = "os.open:nonconst-flags"
R_OS_TRUNCATE = "os.truncate"
R_SHUTIL_COPY = "shutil.copy*"
R_OPEN_A = "open:a"
R_OPEN_VAR = "open:nonconstant-mode"
R_PATH_OPEN_VAR = "Path.open:nonconstant-mode"
R_MOD_OPEN_VAR = "Module.open:nonconstant-mode"
ROUTES = {R_OS_TRUNC, R_OS_NONCONST, R_OS_TRUNCATE, R_SHUTIL_COPY, R_OPEN_A, R_OPEN_W, R_PATH_OPEN_W, R_MOD_OPEN_W, R_WRITE_TEXT, R_WRITE_BYTES,
          R_OPEN_VAR, R_PATH_OPEN_VAR, R_MOD_OPEN_VAR}
# Stdlib modules whose `open(path, mode)` takes the mode SECOND (a leading underscore
# alias such as `import tarfile as _tarfile` is stripped).
_OPEN_MODULES = {"io", "gzip", "bz2", "lzma", "tarfile", "zipfile", "codecs"}


def _is_test_path(parts, name) -> bool:
    return "tests" in parts or name == "conftest.py" or name.startswith("test_")


def surface_files(root: Path = SCAN_ROOT):
    seen, out = set(), []
    for p in sorted(root.rglob("*.py")):
        rel_parts = p.relative_to(root).parts
        if _is_test_path(rel_parts[:-1], p.name):
            continue
        real = p.resolve()
        if real in seen:
            continue
        seen.add(real)
        out.append(p)
    return out


def _mentions(node, name) -> bool:
    """True when `name` appears ANYWHERE in the expression (attribute or bare name)."""
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == name:
            return True
        if isinstance(n, ast.Name) and n.id == name:
            return True
    return False


def _arg(call: ast.Call, pos: int, kw: str):
    if len(call.args) > pos and not isinstance(call.args[pos], ast.Starred):
        return call.args[pos]
    for k in call.keywords:
        if k.arg == kw:
            return k.value
    return None


def _mode_route(call: ast.Call, pos: int, w_route: str, var_route: str):
    mode = _arg(call, pos, "mode")
    if mode is None:
        return None  # no mode: a read
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        if "w" in mode.value:
            return w_route
        return R_OPEN_A if "a" in mode.value else None
    return var_route  # a variable / expression / f-string mode: a finding


class _Scope:
    """What one module tells us about names: import aliases and module-level
    `X = open` / `X = os.open` aliases. `callee(f)` canonicalises a call's func."""

    def __init__(self, tree: ast.AST):
        # local name -> real module (import os as _os); the plain names are seeded so a
        # bare `os.open(...)` classifies even where the import is not in this tree.
        self.modules: dict[str, str] = {m: m for m in ("os", "shutil", "builtins", *_OPEN_MODULES)}
        self.from_names: dict[str, str] = {}    # local name -> "module.attr" (from os import open as oo)
        self.aliases: dict[str, ast.AST] = {}   # module-level X = <Name|Attribute>
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    self.modules[a.asname or a.name.split(".")[0]] = a.name.split(".")[0]
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                for a in n.names:
                    self.from_names[a.asname or a.name] = f"{n.module.split('.')[0]}.{a.name}"
        for n in getattr(tree, "body", []):
            if (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                    and isinstance(n.value, (ast.Name, ast.Attribute))):
                self.aliases[n.targets[0].id] = n.value

    def callee(self, f, depth: int = 0):
        """The canonical dotted name a call's func resolves to ('os.open', 'open',
        'shutil.copyfile', ...), or '?' for an attribute on something unresolved
        (`p.open`, `p.write_text`), paired with that attribute's name."""
        if depth > 4:
            return ("?", None)
        if isinstance(f, ast.Name):
            if f.id in self.from_names:
                return (self.from_names[f.id], None)
            if f.id in self.aliases:
                return self.callee(self.aliases[f.id], depth + 1)
            return (f.id, None)
        if isinstance(f, ast.Attribute):
            if isinstance(f.value, ast.Name) and f.value.id in self.modules:
                real = self.modules[f.value.id]
                return ("open" if (real, f.attr) == ("builtins", "open") else f"{real}.{f.attr}", None)
            return ("?", f.attr)
        return ("?", None)


def _assigned_values(scope_node, name):
    """Every value assigned to `name` inside the enclosing function (or module)."""
    out = []
    for n in ast.walk(scope_node):
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in n.targets):
            out.append(n.value)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)) and isinstance(n.target, ast.Name) \
                and n.target.id == name and n.value is not None:
            out.append(n.value)
    return out


def _bare_names(expr):
    """Name nodes in a flags expression that stand for a VALUE held elsewhere: not an
    attribute base (`os` in `os.O_X`) and not an `O_*` constant imported by name."""
    bases = {id(n.value) for n in ast.walk(expr) if isinstance(n, ast.Attribute)}
    return [n.id for n in ast.walk(expr)
            if isinstance(n, ast.Name) and id(n) not in bases and not n.id.startswith("O_")]


def _param_names(fn):
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    a = fn.args
    names = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
    names |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
    return names


def _flags_route(flags, scope_node):
    """The route of an `os.open` flags expression, or None. O_TRUNC anywhere in it, or in
    the value of a Name it holds (resolved inside the enclosing function, transitively),
    is `os.open:O_TRUNC`. A Name that does not resolve inside the function (a parameter, a
    module-level constant, an outer-scope value) is `os.open:nonconst-flags`, unless the
    flags expression itself literally contains O_NOFOLLOW (the strict opener)."""
    if _mentions(flags, "O_TRUNC"):
        return R_OS_TRUNC
    params = _param_names(scope_node)
    unresolved, seen, todo = False, set(), list(_bare_names(flags))
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        values = _assigned_values(scope_node, name)
        if not values or name in params:
            unresolved = True
        for v in values:
            if _mentions(v, "O_TRUNC"):
                return R_OS_TRUNC
            todo.extend(_bare_names(v))
    if unresolved and not _mentions(flags, "O_NOFOLLOW"):
        return R_OS_NONCONST
    return None


def classify_call(call: ast.Call, scope: "_Scope | None" = None, scope_node=None):
    """The route a call is on, or None."""
    scope = scope or _Scope(ast.Module(body=[], type_ignores=[]))
    if scope_node is None:
        scope_node = ast.Module(body=[], type_ignores=[])
    callee, attr = scope.callee(call.func)
    if callee == "open":
        return _mode_route(call, 1, R_OPEN_W, R_OPEN_VAR)
    if callee == "os.open":
        flags = _arg(call, 1, "flags")
        return _flags_route(flags, scope_node) if flags is not None else None
    if callee == "os.truncate":
        return R_OS_TRUNCATE
    if callee in ("shutil.copyfile", "shutil.copy", "shutil.copy2"):
        return R_SHUTIL_COPY
    if callee.endswith(".open") and callee.split(".")[0].lstrip("_") in _OPEN_MODULES:
        return _mode_route(call, 1, R_MOD_OPEN_W, R_MOD_OPEN_VAR)
    if callee == "?":
        if attr == "open":
            return _mode_route(call, 0, R_PATH_OPEN_W, R_PATH_OPEN_VAR)
        if attr == "write_text":
            return R_WRITE_TEXT
        if attr == "write_bytes":
            return R_WRITE_BYTES
    return None


class _Collector(ast.NodeVisitor):
    def __init__(self, tree: ast.AST):
        self.stack: list[str] = []
        self.nodes: list[ast.AST] = [tree]   # innermost function (or the module)
        self.scope = _Scope(tree)
        self.found: list[tuple[str, str]] = []

    def _scoped(self, node):
        self.stack.append(node.name)
        is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if is_fn:
            self.nodes.append(node)
        self.generic_visit(node)
        if is_fn:
            self.nodes.pop()
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Call(self, node):
        route = classify_call(node, self.scope, self.nodes[-1])
        if route is not None:
            self.found.append((".".join(self.stack) or "<module>", route))
        self.generic_visit(node)


def collect_source(source: str):
    tree = ast.parse(source)
    c = _Collector(tree)
    c.visit(tree)
    return c.found


def collect_tree(root: Path = SCAN_ROOT):
    """Counter of 'relative/path.py::<qualname>::<route>' over the whole surface."""
    out = Counter()
    for p in surface_files(root):
        rel = p.relative_to(root).as_posix()
        rel = ("src/engram/" + rel) if root == SCAN_ROOT else rel
        for qual, route in collect_source(p.read_text(encoding="utf-8")):
            out[f"{rel}::{qual}::{route}"] += 1
    return out


def read_baseline(path: Path = BASELINE) -> Counter:
    out = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out[line] += 1
    return out


def diff(current: Counter, baseline: Counter):
    new = current - baseline        # sites the baseline does not cover
    gone = baseline - current       # baseline lines with no matching site
    return new, gone


# --------------------------------------------------------------------------
# the real tree
# --------------------------------------------------------------------------

def test_no_site_outside_the_baseline():
    new, _gone = diff(collect_tree(), read_baseline())
    assert not new, (
        "truncating write route(s) not on the #4380 baseline. Route the write through "
        "engram_regular_io (open_regular_text_write / write_regular_bytes_synced) or, for "
        "a path that is never a state path, argue for it in review and add the line to "
        "tests/data/truncating_writes_baseline_4380.txt:\n  "
        + "\n  ".join(f"{k} (x{v})" for k, v in sorted(new.items())))


def test_every_baseline_entry_still_exists():
    _new, gone = diff(collect_tree(), read_baseline())
    assert not gone, (
        "baseline line(s) with no matching site: the site was fixed or moved. DELETE the "
        "line (the baseline only shrinks):\n  "
        + "\n  ".join(f"{k} (x{v})" for k, v in sorted(gone.items())))


def test_surface_is_not_vacuous():
    """Wrong roots must not read as green: the surface is the whole package and the
    baseline is the real set, not an empty one."""
    files = surface_files()
    assert len(files) > 100, len(files)
    names = {p.name for p in files}
    assert {"engram_regular_io.py", "engram_core.py", "_hooklib.py"} <= names
    baseline = read_baseline()
    assert sum(baseline.values()) > 10, sum(baseline.values())
    routes_seen = {k.rsplit("::", 1)[1] for k in baseline}
    assert routes_seen <= ROUTES, routes_seen - ROUTES


def test_the_sites_this_change_fixed_are_not_baselined():
    base = read_baseline()
    for key in base:
        path, qual, _route = key.split("::")
        assert not path.endswith(("engram_regular_io.py", "_hooklib.py")) or (
            qual not in {"open_regular_text_write", "open_regular_text_append"}), key
        assert not (path.endswith("tools/quote_quality_survey.py")
                    and qual in {"_write_sheet", "_write_key"}), key
        assert not (path.endswith("tools/s2_clause_sheet.py")
                    and qual == "_rewrite_private"), key
        assert not (path.endswith("engram_core.py")
                    and qual == "_write_backup_push_status"
                    and key.endswith(R_OS_TRUNC)), key
        # round 2: the loop-arm Stop hook's reply-gate log (flags held in a name) and
        # backup_remote_repair's status tmp are fixed, not baselined.
        assert not path.endswith("engram-loop-arm-stop-hook.py") or not qual.startswith(
            ("_reply_gate", "_log_reply_gate")), key
        assert not (path.endswith("backup_remote_repair.py") and qual == "_write_status"), key


# --------------------------------------------------------------------------
# planted controls: the collector must be able to fail
# --------------------------------------------------------------------------

_PLANTED = '''
import os, io, gzip, tarfile, shutil, builtins
import os as _os
from os import open as oo, truncate as tr
from shutil import copyfile as cf
from pathlib import Path

opn = open
osopen = os.open

def p_os_open_bare(p):
    return os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

def p_os_open_kw(p):
    return os.open(p, flags=os.O_TRUNC)

def p_open_w(p):
    return open(p, "w")

def p_open_wb(p):
    return open(p, "wb")

def p_open_mode_kw(p):
    return open(p, mode="w+")

def p_io_open_w(p):
    return io.open(p, "w")

def p_gzip_open_wb(p):
    return gzip.open(p, "wb")

def p_tarfile_open_kw(p):
    return tarfile.open(p, mode="w:gz")

def p_module_open_var(p, m):
    return gzip.open(p, m)

def p_path_open_w(p):
    return Path(p).open("w")

def p_path_open_kw(p):
    return p.open(mode="wb")

def p_write_text(p):
    p.write_text("x")

def p_write_bytes(p):
    p.write_bytes(b"x")

def p_open_var_mode(p, m):
    return open(p, m)

def p_path_open_var_mode(p, m):
    return p.open(m)

# ---- round 2 (review of b2f78458): flags held in a name ----
def p_flags_name_trunc(p):
    fl = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    return os.open(p, fl, 0o600)

def p_flags_name_augassign(p):
    fl = os.O_WRONLY
    fl |= os.O_TRUNC
    return os.open(p, fl)

def p_flags_param_no_nofollow(p, flags):
    return os.open(p, flags | os.O_NONBLOCK, 0o666)

def p_flags_via_second_name(p, flags):
    fl = flags | os.O_NONBLOCK
    return os.open(p, fl)

def n_flags_param_with_nofollow(p, flags):
    return os.open(p, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o666)

def n_flags_name_without_trunc(p):
    fl = os.O_WRONLY | os.O_CREAT
    return os.open(p, fl)

# ---- round 2: more routes ----
def p_os_truncate(p):
    os.truncate(p, 0)

def p_shutil_copyfile(a, b):
    shutil.copyfile(a, b)

def p_shutil_copy(a, b):
    shutil.copy(a, b)

def p_shutil_copy2(a, b):
    shutil.copy2(a, b)

def p_open_append(p):
    return open(p, "a")

def p_open_append_plus(p):
    return open(p, mode="ab+")

def p_path_open_append(p):
    return p.open("a")

# ---- round 2: aliases ----
def p_from_os_import_open_as(p):
    return oo(p, os.O_WRONLY | os.O_TRUNC)

def p_module_level_open_alias(p):
    return opn(p, "w")

def p_module_level_os_open_alias(p):
    return osopen(p, os.O_TRUNC)

def p_import_os_as(p):
    return _os.open(p, _os.O_TRUNC)

def p_from_os_import_truncate(p):
    tr(p, 0)

def p_from_shutil_import_copyfile_as(a, b):
    cf(a, b)

def p_builtins_open(p):
    return builtins.open(p, "w")

class K:
    def method(self, p):
        return open(p, "w")

    def nested(self, p):
        def inner():
            return p.write_text("x")
        return inner

# ---- negatives: none of these may be reported ----
def n_open_r(p):
    return open(p, "r")

def n_open_default(p):
    return open(p)

def n_gzip_open_read(p):
    return gzip.open(p, "rb")

def n_os_open_no_trunc(p):
    return os.open(p, os.O_WRONLY)

def n_os_open_create(p):
    return os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

def n_read_text(p):
    return p.read_text()

def n_path_open_r(p):
    return p.open("r")

def n_path_open_noarg(p):
    return p.open()

def n_docstring():
    """open(p, "w") and p.write_text("x") and os.O_TRUNC in prose"""
    s = "os.open(p, os.O_TRUNC)"  # a string, not a call
    return s  # open(p, "w") in a comment
'''

_EXPECTED_POSITIVES = {
    ("p_os_open_bare", R_OS_TRUNC),
    ("p_os_open_kw", R_OS_TRUNC),
    ("p_open_w", R_OPEN_W),
    ("p_open_wb", R_OPEN_W),
    ("p_open_mode_kw", R_OPEN_W),
    ("p_io_open_w", R_MOD_OPEN_W),
    ("p_gzip_open_wb", R_MOD_OPEN_W),
    ("p_tarfile_open_kw", R_MOD_OPEN_W),
    ("p_module_open_var", R_MOD_OPEN_VAR),
    ("p_path_open_w", R_PATH_OPEN_W),
    ("p_path_open_kw", R_PATH_OPEN_W),
    ("p_write_text", R_WRITE_TEXT),
    ("p_write_bytes", R_WRITE_BYTES),
    ("p_open_var_mode", R_OPEN_VAR),
    ("p_path_open_var_mode", R_PATH_OPEN_VAR),
    ("p_flags_name_trunc", R_OS_TRUNC),
    ("p_flags_name_augassign", R_OS_TRUNC),
    ("p_flags_param_no_nofollow", R_OS_NONCONST),
    ("p_flags_via_second_name", R_OS_NONCONST),
    ("p_os_truncate", R_OS_TRUNCATE),
    ("p_shutil_copyfile", R_SHUTIL_COPY),
    ("p_shutil_copy", R_SHUTIL_COPY),
    ("p_shutil_copy2", R_SHUTIL_COPY),
    ("p_open_append", R_OPEN_A),
    ("p_open_append_plus", R_OPEN_A),
    ("p_path_open_append", R_OPEN_A),
    ("p_from_os_import_open_as", R_OS_TRUNC),
    ("p_module_level_open_alias", R_OPEN_W),
    ("p_module_level_os_open_alias", R_OS_TRUNC),
    ("p_import_os_as", R_OS_TRUNC),
    ("p_from_os_import_truncate", R_OS_TRUNCATE),
    ("p_from_shutil_import_copyfile_as", R_SHUTIL_COPY),
    ("p_builtins_open", R_OPEN_W),
    ("K.method", R_OPEN_W),
    ("K.nested.inner", R_WRITE_TEXT),
}


def test_planted_controls_collector_finds_exactly_the_positives():
    found = collect_source(_PLANTED)
    assert set(found) == _EXPECTED_POSITIVES
    assert len(found) == len(_EXPECTED_POSITIVES)  # no duplicates, none dropped
    assert not [q for q, _r in found if q.startswith("n_")]


def test_bare_attribute_check_would_have_been_inert_on_the_flags_binop():
    """The mutant a first draft had: test `arg.attr == 'O_TRUNC'` on the flags node.
    On `A | B | O_TRUNC` the node is a BinOp, so it never fires. Prove the real
    collector does not share that blindness."""
    call = ast.parse("os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)").body[0].value
    flags = call.args[1]
    assert not (isinstance(flags, ast.Attribute) and flags.attr == "O_TRUNC")  # the inert check
    assert classify_call(call) == R_OS_TRUNC


def _make_tree(tmp_path, files):
    root = tmp_path / "src" / "engram"
    for rel, src in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(src, encoding="utf-8")
    return root


def test_collector_reports_a_new_site_in_a_scratch_tree_and_ignores_test_files(tmp_path):
    root = _make_tree(tmp_path, {
        "good.py": "def f(p):\n    return open(p)\n",
        "bad.py": "def g(p):\n    p.write_text('y')\n",
        "tests/test_x.py": "def h(p):\n    p.write_text('y')\n",
        "test_y.py": "def h(p):\n    p.write_text('y')\n",
        "conftest.py": "def h(p):\n    p.write_text('y')\n",
    })
    cur = Counter()
    for p in surface_files(root):
        for q, r in collect_source(p.read_text()):
            cur[f"{p.relative_to(root).as_posix()}::{q}::{r}"] += 1
    assert cur == Counter({"bad.py::g::write_text": 1})
    new, gone = diff(cur, Counter())
    assert new == cur and not gone


def test_diff_flags_a_vanished_entry_and_a_second_identical_site():
    base = Counter({"a.py::f::write_text": 1, "b.py::g::open:w": 1})
    cur = Counter({"a.py::f::write_text": 2})
    new, gone = diff(cur, base)
    assert new == Counter({"a.py::f::write_text": 1})   # a second identical site is new
    assert gone == Counter({"b.py::g::open:w": 1})       # a fixed site must leave the baseline
