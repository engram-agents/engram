"""#3222 fold 2 -- ``sys`` must be bound wherever engram_core error paths use it.

engram_core.py had no module-level ``import sys``; two functions used
``sys.stderr`` in an ``except`` handler without a local import, so the handler
itself raised NameError:
  1. ``_commit_snapshot_async``: worker-unavailable fallback must return None
     (that None is what makes the caller fall back to the sync path).
  2. ``_NLIClassifier._ensure_loaded``: a model-load failure must log, set
     ``_load_failed`` and return False.
Test 3 is a pure-``ast`` class guard over every src/engram/*.py. Temp data dir
only; never touches ~/.engram.
"""

from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
import textwrap
import types
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
SRC = REPO_ROOT / "src" / "engram"


@pytest.fixture
def core_env():
    tmp = Path(tempfile.mkdtemp(prefix="engram_test_sysbound_3222_"))
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    import server
    server._configure_paths(tmp)
    server._ensure_data_dir()
    server.core._git_available = True
    conn = server._get_db()
    yield server, conn
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def test_async_worker_unavailable_returns_none(core_env, capsys):
    server, conn = core_env
    import engram_snapshot_worker as sw
    with mock.patch.object(sw, "start_worker", side_effect=RuntimeError("boom")):
        result = server.core._commit_snapshot_async(conn, "t", "test", True)
    assert result is None
    assert "snapshot worker unavailable" in capsys.readouterr().err


def test_nli_load_failure_reported_not_raised(core_env, capsys):
    server, _conn = core_env
    clf = server.core._NLIClassifier()

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("synthetic load failure")

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.CrossEncoder = _Boom
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    with mock.patch.dict(sys.modules, {"sentence_transformers": fake_st, "torch": fake_torch}):
        assert clf._ensure_loaded("fake/model") is False
    assert clf._load_failed is True
    assert "Failed to load NLI model" in capsys.readouterr().err


def _bound_names_at_module_level(tree: ast.Module) -> set[str]:
    """Names bound by imports at module level, incl. inside top-level try/if/with."""
    bound: set[str] = set()

    def walk(stmts):
        for s in stmts:
            if isinstance(s, ast.Import):
                bound.update((a.asname or a.name.split(".")[0]) for a in s.names)
            elif isinstance(s, ast.ImportFrom):
                bound.update((a.asname or a.name) for a in s.names)
            elif isinstance(s, (ast.Try, ast.If, ast.With)):
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(s, field, []))
                for h in getattr(s, "handlers", []):
                    walk(h.body)

    walk(tree.body)
    return bound


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = _DEFS + (ast.Lambda, ast.ClassDef)


def _own_scope_nodes(fn, stop):
    """Yield nodes of ``fn``'s body without descending into nested ``stop`` nodes."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, stop):
            stack.extend(ast.iter_child_nodes(n))


def _fn_binds_sys(fn) -> bool:
    """True if ``sys`` is a parameter or imported in ``fn``'s OWN scope."""
    a = fn.args
    params = a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]
    if any(p.arg == "sys" for p in params):
        return True
    for n in _own_scope_nodes(fn, _SCOPES):
        if isinstance(n, ast.Import) and any((x.asname or x.name.split(".")[0]) == "sys" for x in n.names):
            return True
        if isinstance(n, ast.ImportFrom) and any((x.asname or x.name) == "sys" for x in n.names):
            return True
    return False


def _unbound_sys_in_tree(tree: ast.Module, filename: str = "<src>") -> list[str]:
    """Functions loading ``sys`` in their own scope with no binding in it, in an
    enclosing function, or at module level (caller checks module level)."""
    offenders = []

    def visit(node, enclosing_bound):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _DEFS):
                bound = enclosing_bound or _fn_binds_sys(child)
                if not bound:
                    for n in _own_scope_nodes(child, _DEFS):
                        if isinstance(n, ast.Name) and n.id == "sys" and isinstance(n.ctx, ast.Load):
                            offenders.append(f"{filename}:{child.name}:{n.lineno}")
                            break
                visit(child, bound)
            else:
                visit(child, enclosing_bound)

    visit(tree, False)
    return offenders


def _unbound_sys_sites() -> list[str]:
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if "sys" in _bound_names_at_module_level(tree):
            continue
        offenders.extend(_unbound_sys_in_tree(tree, path.name))
    return offenders


def _offenders(source: str) -> list[str]:
    tree = ast.parse(textwrap.dedent(source))
    if "sys" in _bound_names_at_module_level(tree):
        return []
    return _unbound_sys_in_tree(tree)


def test_guard_flags_import_only_in_inner_def():
    # (a) the inner def's import does not bind sys for the outer body.
    src = """
    def outer():
        def inner():
            import sys
        sys.stderr.write("x")
    """
    hits = _offenders(src)
    assert len(hits) == 1 and ":outer:" in hits[0], hits


def test_guard_accepts_outer_import_used_in_inner_def():
    # (b) closure: an enclosing function's import binds sys for nested ones.
    src = """
    def outer():
        import sys
        def inner():
            sys.stderr.write("x")
        return inner
    """
    assert _offenders(src) == []


def test_guard_flags_import_sys_as_other():
    # (c) `import sys as other` does not bind the name `sys`.
    src = """
    def f():
        import sys as other
        sys.stderr.write("x")
    """
    assert len(_offenders(src)) == 1


def test_guard_accepts_sys_parameter():
    # (d)
    src = """
    def f(sys):
        sys.stderr.write("x")
    """
    assert _offenders(src) == []


def test_guard_accepts_module_level_import():
    # (e)
    src = """
    import sys
    def f():
        sys.stderr.write("x")
    """
    assert _offenders(src) == []


def test_no_function_uses_unbound_sys():
    """Guard: a function that loads ``sys`` must have it bound (module level or a
    local import somewhere in that function)."""
    offenders = _unbound_sys_sites()
    assert offenders == [], "unbound `sys` use:\n" + "\n".join(offenders)
