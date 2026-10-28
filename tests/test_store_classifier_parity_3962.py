"""#3962: the store-path classifier exists in TWO files on purpose, pinned together.

`tools/_owner_read.check_store_path` (stdlib-only, for the tools) and
`engram_regular_io.check_store_path` (core, which cannot import a tool module)
are ONE classification semantics in two pinned copies; there is no third. Same
approach as tests/test_regular_io_parity_3628.py:

  (a) TEXTUAL: every pinned function and exception class has an identical AST
      (docstrings dropped: wording may differ, code may not).
  (b) BEHAVIOURAL: ONE cell set run over BOTH implementations, each in a child
      python under a hard timeout, so a regression that blocks on a FIFO fails
      (timeout) instead of hanging the suite.

A parity mutation must change a BODY, not prose: a docstring-only edit is a null
mutation. (c) is the mutant check, run by hand and reported in the PR: e.g. make
one copy treat ENOTDIR as not found; (a) and (b) both go red.
"""
from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src" / "engram"
OWNER_READ = SRC / "tools" / "_owner_read.py"
CORE_IO = SRC / "engram_regular_io.py"

GUARD_S = int(os.environ.get("STORE_CLASSIFIER_PARITY_GUARD_S", "10"))
IMPLS = ["tools._owner_read", "engram_regular_io"]

# The pinned set, named explicitly: a new member is added here AND in both files.
PINNED_FUNCTIONS = ["_errno_symbol", "_uncheckable", "_file_type_words", "check_store_path"]
PINNED_CLASSES = ["StoreUnusable", "StoreUncheckable", "StoreNotAStore"]

_PRELUDE = f"""
import sys, os, json, stat, importlib
sys.path.insert(0, {str(SRC)!r})
impl = importlib.import_module(sys.argv[1])
P = sys.argv[2]
"""

_CHECK = r"""
try:
    impl.check_store_path(P)
    result = {"cls": None}
except BaseException as e:
    result = {"cls": type(e).__name__, "bases": [c.__name__ for c in type(e).__mro__],
              "msg": str(e), "st_mode": getattr(e, "st_mode", None)}
"""


def _classify(impl: str, path: str) -> dict:
    code = _PRELUDE + _CHECK + "\nprint('RESULT:' + json.dumps(result))\n"
    try:
        done = subprocess.run([sys.executable, "-c", code, impl, path], capture_output=True,
                              text=True, timeout=GUARD_S)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{impl}: blocked for more than {GUARD_S}s classifying {path}")
    assert done.returncode == 0, done.stderr
    line = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):])


# ------------------------------------------------------------------ (a) textual

def _strip_doc(body):
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        return body[1:]
    return body


def _top_level(path: Path, name: str, kind):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = [n for n in tree.body if isinstance(n, kind) and n.name == name]
    assert len(found) == 1, f"{path.name}: expected exactly one top-level {kind.__name__} {name}"
    return found[0]


def _dump(node) -> str:
    node.body = _strip_doc(list(node.body))
    return ast.dump(node)


@pytest.mark.parametrize("name", PINNED_FUNCTIONS)
def test_pinned_function_has_identical_ast_in_both_files(name):
    a = _dump(_top_level(OWNER_READ, name, ast.FunctionDef))
    b = _dump(_top_level(CORE_IO, name, ast.FunctionDef))
    assert a == b, (f"{name} differs between tools/_owner_read.py and engram_regular_io.py: the two "
                    f"copies of the store-path classifier must stay identical (#3962)")


@pytest.mark.parametrize("name", PINNED_CLASSES)
def test_pinned_exception_class_has_identical_ast_in_both_files(name):
    a = _dump(_top_level(OWNER_READ, name, ast.ClassDef))
    b = _dump(_top_level(CORE_IO, name, ast.ClassDef))
    assert a == b, (f"{name} differs between tools/_owner_read.py and engram_regular_io.py: the two "
                    f"copies of the store-path classifier must stay identical (#3962)")


def test_the_pinned_set_is_every_classifier_definition_in_core():
    """Core holds no classifier definition the pin does not name (a new helper cannot slip in unpinned)."""
    tree = ast.parse(CORE_IO.read_text(encoding="utf-8"))
    core_names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    classifier_like = {n for n in core_names if n.startswith("Store") or n in PINNED_FUNCTIONS}
    assert classifier_like == set(PINNED_FUNCTIONS) | set(PINNED_CLASSES), classifier_like


# --------------------------------------------------------------- (b) behaviour

KINDS = ["enoent", "chmod000", "enotdir", "loop", "dangling", "fifo", "dir", "link_to_file", "regular"]

# (class name, message prefix | None) per kind; spelled out literally, not computed.
def _expected(kind: str, p: str):
    if kind == "enoent":
        return "FileNotFoundError", f"ENGRAM database not found: {p}"
    if kind == "fifo":
        return "StoreNotAStore", f"the store path {p} is a FIFO, not a regular file"
    if kind == "dir":
        return "StoreNotAStore", f"the store path {p} is a directory, not a regular file"
    if kind in ("link_to_file", "regular"):
        return None, None
    symbol = {"chmod000": "EACCES", "enotdir": "ENOTDIR", "loop": "ELOOP", "dangling": "ENOENT"}[kind]
    return "StoreUncheckable", f"cannot check the store at {p} ({symbol}:"


@pytest.fixture
def plant():
    made: list[Path] = []

    def _plant(kind: str) -> str:
        if kind == "chmod000" and hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        if kind == "fifo" and not hasattr(os, "mkfifo"):
            pytest.skip("os.mkfifo is not available on this platform")
        base = Path(tempfile.mkdtemp(prefix="sc3962-"))
        made.append(base)
        s = base / "knowledge.db"
        if kind == "enoent":
            return str(s)
        if kind == "chmod000":
            d = base / "locked"
            d.mkdir()
            (d / "knowledge.db").write_bytes(b"")
            d.chmod(0)
            return str(d / "knowledge.db")
        if kind == "enotdir":
            (base / "afile").write_bytes(b"")
            return str(base / "afile" / "knowledge.db")
        if kind == "loop":
            os.symlink("b", base / "a")
            os.symlink("a", base / "b")
            os.symlink("a", s)
        elif kind == "dangling":
            os.symlink(str(base / "nowhere"), s)
        elif kind == "fifo":
            os.mkfifo(s)
        elif kind == "dir":
            s.mkdir()
        elif kind == "link_to_file":
            (base / "real.db").write_bytes(b"x")
            os.symlink(str(base / "real.db"), s)
        elif kind == "regular":
            s.write_bytes(b"x")
        return str(s)

    yield _plant
    for base in made:
        for d in base.rglob("*"):
            try:
                if d.is_dir() and not d.is_symlink():
                    d.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.parametrize("impl", IMPLS)
@pytest.mark.parametrize("kind", KINDS)
def test_one_cell_set_over_both_implementations(kind, impl, plant):
    p = plant(kind)
    cls, prefix = _expected(kind, p)
    r = _classify(impl, p)
    assert r["cls"] == cls, r
    if cls is None:
        return
    assert r["msg"].startswith(prefix), r
    if cls == "FileNotFoundError":
        assert r["msg"] == prefix, r  # exactly the text callers raise for a missing store
        assert "StoreUnusable" not in r["bases"], r
    else:
        assert "StoreUnusable" in r["bases"], r
    if cls == "StoreNotAStore":
        assert r["st_mode"] == os.stat(p).st_mode, r
        assert isinstance(r["st_mode"], int) and not stat.S_ISREG(r["st_mode"]), r
    else:
        assert r["st_mode"] is None, r
