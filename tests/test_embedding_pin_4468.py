"""#4468 (part of #4447, contract 2): the forum keeps its OWN copy of the
embedding pin table, because it is deployed from src/forum/ alone and does not
import engram_core. This file is the one binding between the two copies.

Cells:
  (a) drift: the forum table equals the engram_core table, field by field.
  (b) safetensors only: neither table pins a pickle-based weights file, so a
      refresh cannot point at weights whose constructor unpickles before any
      hash is compared (Zephyr's #4467 note 1).
  (c) shape: 40-hex revision, exactly ten files, each hash 64 lowercase hex.

The forum module is loaded by file path (it is not on the root suite's path).
"""

from __future__ import annotations

import ast
import builtins
import copy
import importlib.util
import re
from pathlib import Path

import pytest

import engram_core

FORUM_EMBEDDINGS = (
    Path(__file__).resolve().parent.parent / "src" / "forum" / "embeddings.py"
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
PICKLE_SUFFIXES = (".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle")


@pytest.fixture(scope="module")
def forum_pin():
    spec = importlib.util.spec_from_file_location(
        "forum_embeddings_pin_4468", str(FORUM_EMBEDDINGS)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._MINILM_PIN


@pytest.fixture(params=["core", "forum"])
def table(request, forum_pin):
    return engram_core._MINILM_PIN if request.param == "core" else forum_pin


def test_a_forum_pin_equals_engram_core_pin(forum_pin):
    core = engram_core._MINILM_PIN
    assert forum_pin["repo_id"] == core["repo_id"]
    assert forum_pin["revision"] == core["revision"]
    assert forum_pin["sha256"] == core["sha256"]
    assert forum_pin == core


def test_b_only_safetensors_weights_are_pinned(table):
    names = table["sha256"]
    assert "model.safetensors" in names
    pickled = [n for n in names if n.lower().endswith(PICKLE_SUFFIXES)]
    assert pickled == [], f"pickle-based weights pinned: {pickled}"


def test_c_pin_shape(table):
    assert HEX40.match(table["revision"])
    assert len(table["sha256"]) == 10
    for name, digest in table["sha256"].items():
        assert HEX64.match(digest), f"{name}: {digest!r}"


# --- #4495: the snapshot entry-set walker `_unexpected_entry` also exists
# twice (engram_core.py and src/forum/embeddings.py), for the same reason as
# the table. Cell (d) binds the two copies; (e) are its self-tests.
#
# Strictness: the comparison is over the AST with ONLY the return annotation
# cleared. Comments and line numbers are ignored; the docstring and every
# statement are compared, so a docstring-only difference IS drift (the two
# docstrings are identical today, so keeping it strict costs nothing).
# Module-level names the body loads are bound too: an assigned name (a
# constant, a frozenset) must have an equal assigned expression in both
# modules, an imported name an equal import, and a name defined in only one
# module is itself drift. Both copies are read as SOURCE by path (the forum
# one through the same FORUM_EMBEDDINGS path the table loader uses;
# engram_core by its __file__), not executed a second time.
#
# Known residual (documented, deliberately not pinned by a cell): `_read_names`
# adds EVERY Store found by `ast.walk` to the walker's locals, so a name
# STORED inside a nested function masks a same-named module-level READ in the
# outer function (the nested store is not the outer's local). A scoped walk
# would close it; the walker has no nested functions today.


WALKER = "_unexpected_entry"
WALKER_MIN_STATEMENTS = 15  # floor N; the real body has 18 statements (docstring included)


def _find_walker(src, label):
    tree = ast.parse(src)
    hits = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == WALKER]
    assert len(hits) == 1, (
        f"{label}: expected exactly one module-level {WALKER}, found {len(hits)}")
    return tree, hits[0]


def _normalised(fn):
    clone = copy.deepcopy(fn)
    clone.returns = None  # the one thing cleared
    return ast.dump(clone, annotate_fields=True, include_attributes=False)


def _shallow(node):
    """The node's own fields, without its nested statement lists."""
    out = [type(node).__name__]
    for f, v in ast.iter_fields(node):
        if isinstance(v, list) and v and all(isinstance(x, ast.stmt) for x in v):
            continue
        out.append((f, ast.dump(v) if isinstance(v, ast.AST) else
                    [ast.dump(x) if isinstance(x, ast.AST) else x for x in v]
                    if isinstance(v, list) else v))
    return out


def _dumps(stmts):
    return [ast.dump(s) for s in stmts]


def _first_diff(a_stmts, b_stmts):
    """(stmt_a, stmt_b) for the innermost first differing statement pair;
    one side is None when a block is longer on the other side."""
    for sa, sb in zip(a_stmts, b_stmts):
        if ast.dump(sa) == ast.dump(sb):
            continue
        if _shallow(sa) == _shallow(sb):
            for f in ("body", "orelse", "finalbody"):
                ca, cb = getattr(sa, f, None), getattr(sb, f, None)
                if ca and cb and _dumps(ca) != _dumps(cb):
                    return _first_diff(ca, cb)
        return sa, sb
    n = min(len(a_stmts), len(b_stmts))
    if len(a_stmts) > n:
        return a_stmts[n], None
    if len(b_stmts) > n:
        return None, b_stmts[n]
    return None


_OTHER_KINDS = ("function", "class", "conditional")


def _collect_other(node, found):
    """Names bound by statements nested under a module-level compound
    statement, not descending into function/class bodies."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        found.add(node.name)
        return
    if isinstance(node, ast.Lambda):
        return
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        found.add(node.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        found.update((al.asname or al.name).split(".")[0] for al in node.names)
    elif isinstance(node, ast.ExceptHandler) and node.name:
        found.add(node.name)
    for child in ast.iter_child_nodes(node):
        _collect_other(child, found)


def _bind_target(target, value, entries, path=""):
    """Record assignment bindings for `target`. A Name target binds the
    assigned expression. Tuple/list targets bind pairwise when the value is a
    literal tuple/list of the same length (no starred parts); otherwise each
    name binds the WHOLE assigned expression plus its position, so a changed
    right-hand side or a swapped order is drift either way."""
    if isinstance(target, ast.Name):
        entries.setdefault(target.id, []).append(("assign", ast.dump(value)))
    elif isinstance(target, (ast.Tuple, ast.List)):
        pairwise = (isinstance(value, (ast.Tuple, ast.List))
                    and len(value.elts) == len(target.elts)
                    and not any(isinstance(x, ast.Starred)
                                for x in value.elts + target.elts))
        for i, t in enumerate(target.elts):
            if pairwise:
                _bind_target(t, value.elts[i], entries, f"{path}[{i}]")
            else:
                names = set()
                _collect_other(t, names)
                for nm in sorted(names):
                    entries.setdefault(nm, []).append(
                        ("assign", f"unpack{path}[{i}] of {ast.dump(value)}"))
    elif isinstance(target, ast.Starred):
        _bind_target(target.value, value, entries, path)


def _module_bindings(tree):
    """name -> ('assign'|'import'|'seq', canonical) for plain bindings, or
    ('other', kind) for a name defined by a function, class or compound
    statement (a conditional), which this cell does not bind.
    Plain rebinding: the last binding wins. A name with any `+=` style
    augmentation compares its whole ordered binding list instead."""
    entries = {}
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                _bind_target(t, n.value, entries)
        elif isinstance(n, ast.AnnAssign) and n.value is not None:
            _bind_target(n.target, n.value, entries)
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
            entries.setdefault(n.target.id, []).append(
                ("aug", ast.dump(n.op) + ast.dump(n.value)))
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                bound = (al.asname or al.name).split(".")[0]
                mod = f"{'.' * getattr(n, 'level', 0)}{getattr(n, 'module', None)}"
                entries.setdefault(bound, []).append(
                    ("import", f"{mod}:{al.name}:{al.asname}"))
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entries.setdefault(n.name, []).append(("other", "function"))
        elif isinstance(n, ast.ClassDef):
            entries.setdefault(n.name, []).append(("other", "class"))
        elif isinstance(n, (ast.If, ast.Try, ast.TryStar, ast.With, ast.AsyncWith,
                            ast.For, ast.AsyncFor, ast.While, ast.Match)):
            found = set()
            _collect_other(n, found)
            for nm in found:
                entries.setdefault(nm, []).append(("other", "conditional"))
    out = {}
    for name, lst in entries.items():
        others = [e for e in lst if e[0] == "other"]
        if others:
            out[name] = others[0]
        elif any(e[0] == "aug" for e in lst):
            out[name] = ("seq", tuple(lst))
        else:
            out[name] = lst[-1]
    return out


def _read_names(fn):
    """Names the body loads that are not bound inside the function: not
    parameters (of the walker or of anything nested, every kind), nor store
    targets (assignment, loop, comprehension, walrus), except-as names,
    nested def/class names, local imports, or match captures; and not
    builtins."""
    local = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.arg):
            local.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            local.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            local.add(n.name)
        elif (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
              and n is not fn):
            local.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            local.update((al.asname or al.name).split(".")[0] for al in n.names)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            local.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            local.add(n.rest)
    loads = {n.id for n in ast.walk(fn)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return {n for n in loads if n not in local and not hasattr(builtins, n)}


def _walker_drift(core_src, forum_src):
    """Human-readable drift findings (empty list = the copies agree)."""
    ct, cf = _find_walker(core_src, "engram_core")
    ft, ff = _find_walker(forum_src, "forum embeddings")
    problems = []
    if _normalised(cf) != _normalised(ff):
        d = _first_diff(cf.body, ff.body)
        if d is None:
            problems.append(f"{WALKER}: signature or decorators differ "
                            f"(engram_core line {cf.lineno}, "
                            f"forum embeddings line {ff.lineno})")
        else:
            sa, sb = d

            def show(s):
                return (f"line {s.lineno} ({ast.unparse(s)[:80]!r})"
                        if s else "absent")
            problems.append(
                f"{WALKER} differs: first differing statement at engram_core "
                f"{show(sa)} vs forum embeddings {show(sb)}")
    cb, fb = _module_bindings(ct), _module_bindings(ft)
    reads_c, reads_f = _read_names(cf), _read_names(ff)
    for name in sorted(reads_c | reads_f):
        in_c, in_f = name in cb, name in fb
        kinds = {lab: tab[name][1] for lab, tab in
                 (("engram_core", cb), ("forum embeddings", fb))
                 if name in tab and tab[name][0] == "other"}
        if kinds:
            where = " and ".join(f"{lab} ({k})" for lab, k in kinds.items())
            problems.append(
                f"module-level name {name} is defined by a {'/'.join(sorted(set(kinds.values())))} "
                f"in {where}, not a plain assignment or import; such helpers "
                f"are not bound by this cell, so the walker's dependence on "
                f"{name} is unchecked")
        elif in_c and in_f:
            if cb[name] != fb[name]:
                problems.append(f"module-level name {name} differs between "
                                f"engram_core and forum embeddings")
        elif in_c or in_f:
            problems.append(f"module-level name {name} is defined only in "
                            f"{'engram_core' if in_c else 'forum embeddings'}")
        else:
            problems.append(f"{name} is read by the walker but defined at "
                            f"module level in neither module")
    return problems


def test_d_walker_copies_do_not_drift():
    core_src = Path(engram_core.__file__).read_text(encoding="utf-8")
    forum_src = FORUM_EMBEDDINGS.read_text(encoding="utf-8")
    for label, src in (("engram_core", core_src), ("forum embeddings", forum_src)):
        _, fn = _find_walker(src, label)
        count = sum(isinstance(n, ast.stmt) for n in ast.walk(fn)) - 1
        assert count > WALKER_MIN_STATEMENTS, (
            f"{label}: {WALKER} has only {count} statements; vacuous floor")
    # the walker really reads this module-level name (bound by the drift check)
    _, fn = _find_walker(core_src, "engram_core")
    assert "_ALLOWED_EXTRA_FILES" in _read_names(fn)
    problems = _walker_drift(core_src, forum_src)
    assert not problems, "; ".join(problems)


_SYN = '''
import os
_ALLOWED_EXTRA_FILES = frozenset({{"README.md"}})
def _unexpected_entry(snap, pinned_names){ann}:
    """doc"""
    allowed = set(pinned_names) | _ALLOWED_EXTRA_FILES
    for dirpath, dirs, files in os.walk(snap, followlinks={fl}):
        for name in files:
            if name not in allowed:
                return "bad"
    return None
'''


def _syn(ann=" -> 'Optional[str]'", fl="True"):
    return _SYN.format(ann=ann, fl=fl)


def test_e1_annotation_only_difference_is_equal():
    assert _walker_drift(_syn(), _syn(ann=" -> 'str | None'")) == []
    assert _walker_drift(_syn(), _syn(ann="")) == []


def test_e2_one_keyword_difference_is_named():
    problems = _walker_drift(_syn(), _syn(fl="False"))
    assert len(problems) == 1
    assert "followlinks=False" in problems[0]
    assert "engram_core line" in problems[0]
    assert "forum embeddings line" in problems[0]


def test_e3_missing_function_fails_loudly_naming_the_module():
    renamed = _syn().replace("def _unexpected_entry", "def _renamed")
    with pytest.raises(AssertionError, match="forum embeddings"):
        _walker_drift(_syn(), renamed)
    with pytest.raises(AssertionError, match="engram_core"):
        _walker_drift(renamed, _syn())


def test_e4_module_name_value_drift_and_one_sided_name():
    other = _syn().replace('{"README.md"}', '{"README.md", "x"}')
    assert any("_ALLOWED_EXTRA_FILES differs" in p
               for p in _walker_drift(_syn(), other))
    gone = _syn().replace('_ALLOWED_EXTRA_FILES = frozenset({"README.md"})\n', "")
    assert 'frozenset({"README.md"})' not in gone
    assert any("_ALLOWED_EXTRA_FILES is defined only in engram_core" in p
               for p in _walker_drift(_syn(), gone))


def _mut(prelude="", body=""):
    """A synthetic module: `prelude` at module level, `body` inside the
    walker just before its final return (body is dedented source lines)."""
    src = _syn()
    if prelude:
        src = src.replace("import os\n", "import os\n" + prelude + "\n", 1)
    if body:
        ind = "".join("    " + ln + "\n" for ln in body.splitlines())
        src = src.replace("    return None\n", ind + "    return None\n")
    return src


# (f) names that are NOT module-level reads must not be reported: two
# IDENTICAL sources, using such a name, must show no drift.
_LOCAL_KINDS = {
    "except_as": "try:\n    pass\nexcept OSError as exc:\n    print(exc)",
    "nested_def_name": "def helper(q):\n    return q\nhelper(1)",
    "nested_class_name": "class Box:\n    pass\nBox()",
    "nested_def_params": (
        "def helper(a, /, b, *va, kw, **kws):\n    return a, b, va, kw, kws\n"
        "helper(1, 2, kw=3)"),
    "lambda_params": "f = lambda x, *y, z=1, **w: (x, y, z, w)\nf(1)",
    "local_import": "import json\nfrom collections import OrderedDict as OD\n"
                    "json.dumps(OD())",
    "match_captures": (
        "match snap:\n    case [first, *rest]:\n        print(first, rest)\n"
        "    case {'k': v, **kw}:\n        print(v, kw)\n"
        "    case str() as s:\n        print(s)"),
    "walrus": "if (m := len(snap)):\n    print(m)",
    "comprehension": "print([c for c in snap if c])",
}


@pytest.mark.parametrize("kind", sorted(_LOCAL_KINDS))
def test_f_local_names_are_not_module_level_reads(kind):
    src = _mut(body=_LOCAL_KINDS[kind])
    assert _walker_drift(src, src) == []


# (g) module-level bindings the comparison must not miss.
def test_g1_augassign_difference_is_reported():
    a = _mut("_N = 1\n_N += 1", "print(_N)")
    b = _mut("_N = 1\n_N += 2", "print(_N)")
    assert any("_N differs" in p for p in _walker_drift(a, b))
    assert _walker_drift(a, a) == []


def test_g2_relative_import_level_is_reported():
    a = _mut("from . import X", "X()")
    b = _mut("from .. import X", "X()")
    assert any("X differs" in p for p in _walker_drift(a, b))
    assert _walker_drift(a, a) == []


def test_g3_tuple_target_constants():
    a = _mut("_A, _B = 1, 2", "print(_A, _B)")
    assert _walker_drift(a, _mut("_A, _B = 1, 2", "print(_A, _B)")) == []
    for other in ("_A, _B = 1, 3", "_A, _B = 2, 1", "_A, _B = foo()"):
        probs = _walker_drift(a, _mut(other, "print(_A, _B)"))
        assert probs, other
        assert any("_A differs" in p or "_B differs" in p for p in probs), other


def test_g1b_only_the_pre_augmentation_value_differs():
    a = _mut("_N = 1\n_N += 1", "print(_N)")
    b = _mut("_N = 2\n_N += 1", "print(_N)")
    assert any("_N differs" in p for p in _walker_drift(a, b))


def test_g3b_non_literal_tuple_targets():
    body = "print(_A, _B)"
    base = _mut("_A, _B = foo()", body)
    assert _walker_drift(base, base) == []
    # a different right-hand side
    assert any("differs" in p for p in
               _walker_drift(base, _mut("_A, _B = bar()", body)))
    # swapped targets over the same right-hand side
    assert any("differs" in p for p in
               _walker_drift(base, _mut("_B, _A = foo()", body)))


def test_g3c_pairwise_equals_separate_assignments():
    body = "print(_A, _B)"
    assert _walker_drift(_mut("_A, _B = 1, 2", body),
                         _mut("_A = 1\n_B = 2", body)) == []


def test_g3d_starred_targets():
    body = "print(_A, _B)"
    star = _mut("_A, *_B = 1, 2, 3", body)
    assert _walker_drift(star, _mut("_A, *_B = 1, 2, 3", body)) == []
    assert any("differs" in p for p in
               _walker_drift(star, _mut("_A, *_B = 1, 2, 4", body)))
    # same length as the targets, but _B is a list when starred: not pairwise
    assert any("differs" in p for p in
               _walker_drift(_mut("_A, *_B = 1, 2", body),
                             _mut("_A, _B = 1, 2", body)))


def test_g4_plain_rebinding_last_binding_wins():
    a = _mut("_N = 1\n_N = 2", "print(_N)")
    assert _walker_drift(a, _mut("_N = 2", "print(_N)")) == []


# (h) a helper defined by something other than a plain assignment/import is
# a finding that says so (and does not claim it is defined in neither).
@pytest.mark.parametrize("kind,prelude", [
    ("function", "def _helper():\n    return 1"),
    ("class", "class _helper:\n    pass"),
    ("conditional", "if os.name:\n    _helper = 1"),
])
def test_h_helper_defined_otherwise_is_a_finding_naming_its_kind(kind, prelude):
    src = _mut(prelude, "_helper()")
    probs = _walker_drift(src, src)
    assert len(probs) == 1
    assert "_helper" in probs[0] and kind in probs[0]
    assert "not bound" in probs[0]
    assert "neither" not in probs[0]
