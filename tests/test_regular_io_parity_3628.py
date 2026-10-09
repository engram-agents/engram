"""#3628: the non-blocking regular-file helpers exist in TWO files on purpose.

`hooks/claude/_hooklib.py` must stay self-contained (tests copy it alone into
synthetic hooks/ layouts; every hook imports it), so it cannot import the core
module `engram_regular_io.py`. The five shared helpers (the four of #3591 plus
`open_regular_text_append`, #3628) are therefore duplicated, and this file pins
the two copies together two ways:

  (a) BEHAVIOURAL: one cell set, parametrised over BOTH implementations.
      Each cell runs in a child python with a hard timeout, so a regression
      that blocks on a FIFO fails (timeout) instead of hanging the suite.
  (b) TEXTUAL: each shared helper has an identical AST (docstring dropped) in
      both files, so the copies cannot drift even where no cell looks.

The AST cell drops docstrings BY DESIGN (wording may differ, code may not), so a
parity mutation must change the function BODY -- e.g. the 0o666 mode constant
in code, or an os.O_* flag -- not its prose: a docstring-only edit is a null
mutation, not a failed one.

One further copy is pinned here: `hooks/claude/engram-loop-arm-stop-hook.py`
must keep working when it runs with NO sibling lib at all (its own tested
contract: a missed ScheduleWakeup arm must stay loud), so it cannot hard-import
`_hooklib`. It imports the helpers when it can and otherwise defines
`_not_regular_error` + `open_regular_text` itself; those two fallback defs are
pinned identical to `_hooklib`'s (AST) and behave identically (cells below).
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src" / "engram"
HOOK_DIR = SRC / "hooks" / "claude"
HOOKLIB = HOOK_DIR / "_hooklib.py"
CORE_IO = SRC / "engram_regular_io.py"
LOOP_ARM = HOOK_DIR / "engram-loop-arm-stop-hook.py"

GUARD_S = int(os.environ.get("REGULAR_IO_PARITY_GUARD_S", "8"))
IMPLS = ["_hooklib", "engram_regular_io"]
SHARED = ["_not_regular_error", "open_regular_text", "read_regular_text",
          "open_regular_text_write", "open_regular_text_append"]
# The loop-arm hook's import-failure fallback copies (see module docstring).
LOOP_ARM_FALLBACK = ["_not_regular_error", "open_regular_text"]
# A generated module holding ONLY the fallback defs, importable like an impl.
_GEN_DIR = Path(tempfile.mkdtemp(prefix="regular_io_parity_"))
FALLBACK_MODULE = "loop_arm_fallback_copy"

_PRELUDE = f"""
import sys, os, json, errno, stat
sys.path.insert(0, {str(HOOK_DIR)!r})
sys.path.insert(0, {str(SRC)!r})
sys.path.insert(0, {str(_GEN_DIR)!r})
import importlib
impl = importlib.import_module(sys.argv[1])
ROOT = sys.argv[2]
"""


def _child(impl: str, tmp: Path, body: str, timeout=GUARD_S):
    """Run `body` against `impl` in a child python; it sets `result` (JSON)."""
    code = _PRELUDE + body + "\nprint('RESULT:' + json.dumps(result))\n"
    try:
        done = subprocess.run([sys.executable, "-c", code, impl, str(tmp)],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{impl}: blocked for more than {timeout}s on a non-regular file")
    assert done.returncode == 0, done.stderr
    line = [l for l in done.stdout.splitlines() if l.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):])


_NEEDS_FIFO = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo is not available on this platform")


# ------------------------------------------------------------------ (a) behaviour

@_NEEDS_FIFO
@pytest.mark.parametrize("impl", IMPLS)
def test_read_fifo_without_writer_fails_fast(tmp_path, impl):
    os.mkfifo(tmp_path / "f")
    r = _child(impl, tmp_path, r"""
p = os.path.join(ROOT, "f")
try:
    impl.read_regular_text(p); result = "no-error"
except OSError as e:
    result = [e.errno, errno.EINVAL]
""")
    assert r == [r[1], r[1]], r  # EINVAL from the fstat, not a hang


@_NEEDS_FIFO
@pytest.mark.parametrize("impl", IMPLS)
def test_open_read_fifo_with_writer_attached_is_rejected(tmp_path, impl):
    os.mkfifo(tmp_path / "f")
    r = _child(impl, tmp_path, r"""
p = os.path.join(ROOT, "f")
wfd = os.open(p, os.O_RDWR | os.O_NONBLOCK)
try:
    os.write(wfd, b"payload")
    try:
        impl.open_regular_text(p).close(); result = "no-error"
    except OSError as e:
        result = [e.errno, errno.EINVAL]
finally:
    os.close(wfd)
""")
    assert r == [r[1], r[1]], r


@_NEEDS_FIFO
@pytest.mark.parametrize("impl", IMPLS)
def test_write_fifo_without_reader_fails_fast(tmp_path, impl):
    os.mkfifo(tmp_path / "f")
    r = _child(impl, tmp_path, r"""
p = os.path.join(ROOT, "f")
try:
    impl.open_regular_text_write(p).close(); result = "no-error"
except OSError as e:
    result = [e.errno, errno.ENXIO]
""")
    assert r == [r[1], r[1]], r  # ENXIO from os.open(O_NONBLOCK), not a hang


@_NEEDS_FIFO
@pytest.mark.parametrize("impl", IMPLS)
def test_write_fifo_with_reader_is_einval_and_reader_gets_nothing(tmp_path, impl):
    os.mkfifo(tmp_path / "f")
    r = _child(impl, tmp_path, r"""
p = os.path.join(ROOT, "f")
rfd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
try:
    try:
        f = impl.open_regular_text_write(p)
        f.write("leak"); f.close(); err = "no-error"
    except OSError as e:
        err = e.errno
    try:
        got = os.read(rfd, 4096).decode()
    except BlockingIOError:
        got = ""
finally:
    os.close(rfd)
result = {"err": err, "einval": errno.EINVAL, "got": got}
""")
    assert r["err"] == r["einval"], r
    assert r["got"] == "", r  # zero bytes reached the reader


@pytest.mark.parametrize("impl", IMPLS)
def test_directory_raises_isadirectory_on_read_and_write(tmp_path, impl):
    (tmp_path / "d").mkdir()
    r = _child(impl, tmp_path, r"""
p = os.path.join(ROOT, "d")
out = {"eisdir": errno.EISDIR}
for name, fn in (("read", impl.read_regular_text), ("write", impl.open_regular_text_write)):
    try:
        fn(p); out[name] = "no-error"
    except OSError as e:
        out[name] = e.errno
result = out
""")
    assert r["read"] == r["eisdir"], r
    assert r["write"] == r["eisdir"], r


@pytest.mark.parametrize("impl", IMPLS)
def test_dev_null_is_rejected_on_read_and_write(tmp_path, impl):
    if not os.path.exists("/dev/null"):
        pytest.skip("/dev/null is not available")
    r = _child(impl, tmp_path, r"""
out = {"einval": errno.EINVAL}
for name, fn in (("read", impl.read_regular_text), ("write", impl.open_regular_text_write)):
    try:
        fn("/dev/null"); out[name] = "no-error"
    except OSError as e:
        out[name] = e.errno
result = out
""")
    assert r["read"] == r["einval"], r
    assert r["write"] == r["einval"], r


@pytest.mark.parametrize("impl", IMPLS)
def test_dev_zero_read_is_rejected_not_slurped(tmp_path, impl):
    if not os.path.exists("/dev/zero"):
        pytest.skip("/dev/zero is not available")
    r = _child(impl, tmp_path, r"""
try:
    impl.read_regular_text("/dev/zero"); result = "no-error"
except OSError as e:
    result = [e.errno, errno.EINVAL]
""")
    assert r == [r[1], r[1]], r


@pytest.mark.parametrize("impl", IMPLS)
def test_regular_file_content_and_encoding(tmp_path, impl):
    (tmp_path / "r.txt").write_bytes("héllo 世界\n".encode("utf-8"))
    r = _child(impl, tmp_path, r"""
p = os.path.join(ROOT, "r.txt")
with impl.open_regular_text(p, encoding="utf-8") as f:
    a = f.read()
b = impl.read_regular_text(p, encoding="utf-8")
c = impl.read_regular_text(p, encoding="latin-1")
result = {"a": a, "b": b, "c": c}
""")
    assert r["a"] == r["b"] == "héllo 世界\n"
    assert r["c"] == "héllo 世界\n".encode("utf-8").decode("latin-1")


@pytest.mark.parametrize("impl", IMPLS)
def test_missing_file_is_filenotfound(tmp_path, impl):
    r = _child(impl, tmp_path, r"""
try:
    impl.read_regular_text(os.path.join(ROOT, "nope")); result = "no-error"
except FileNotFoundError:
    result = "FileNotFoundError"
""")
    assert r == "FileNotFoundError"


@pytest.mark.parametrize("impl", IMPLS)
def test_write_matches_plain_open_w_umask_truncate_and_content(tmp_path, impl):
    r = _child(impl, tmp_path, r"""
os.umask(0o027)
a = os.path.join(ROOT, "a.txt")   # created by the helper
b = os.path.join(ROOT, "b.txt")   # created by plain open(, "w")
with impl.open_regular_text_write(a, encoding="utf-8") as f:
    f.write("café\n")
with open(b, "w", encoding="utf-8") as f:
    f.write("café\n")
mode_a = stat.S_IMODE(os.stat(a).st_mode); mode_b = stat.S_IMODE(os.stat(b).st_mode)
# truncate: existing longer content is cut, not overwritten in place
with open(a, "w") as f:
    f.write("x" * 100)
with impl.open_regular_text_write(a) as f:
    f.write("short")
result = {"mode_a": mode_a, "mode_b": mode_b,
          "text_a": open(os.path.join(ROOT, "b.txt"), encoding="utf-8").read(),
          "trunc": open(a).read()}
""")
    assert r["mode_a"] == r["mode_b"] == 0o640, r
    assert r["text_a"] == "café\n"
    assert r["trunc"] == "short", r


# ------------------------------------------------------------------ (b) textual

def _helper_dump(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(funcs) == 1, f"{path.name}: expected exactly one top-level def {name}"
    return _dump_fn(funcs[0])


def _dump_fn(fn: ast.FunctionDef) -> str:
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # drop the docstring: wording may differ, code may not
    fn = ast.FunctionDef(name=fn.name, args=fn.args, body=body,
                         decorator_list=fn.decorator_list, returns=fn.returns,
                         type_comment=None, **({"type_params": []} if hasattr(fn, "type_params") else {}))
    return ast.dump(fn)


@pytest.mark.parametrize("name", SHARED)
def test_shared_helper_has_identical_ast_in_both_files(name):
    assert _helper_dump(HOOKLIB, name) == _helper_dump(CORE_IO, name), (
        f"{name} differs between _hooklib.py and engram_regular_io.py: "
        "the duplicated non-blocking helpers must stay identical (#3628)")


# ------------------------------------------------- (a') behaviour: the append helper

@_NEEDS_FIFO
@pytest.mark.parametrize("impl", IMPLS)
def test_append_fifo_without_reader_fails_fast(tmp_path, impl):
    os.mkfifo(tmp_path / "f")
    r = _child(impl, tmp_path, r"""
p = os.path.join(ROOT, "f")
try:
    impl.open_regular_text_append(p).close(); result = "no-error"
except OSError as e:
    result = [e.errno, errno.ENXIO]
""")
    assert r == [r[1], r[1]], r  # ENXIO from os.open(O_NONBLOCK), not a hang


@_NEEDS_FIFO
@pytest.mark.parametrize("impl", IMPLS)
def test_append_fifo_with_reader_is_einval_and_reader_gets_nothing(tmp_path, impl):
    os.mkfifo(tmp_path / "f")
    r = _child(impl, tmp_path, r"""
p = os.path.join(ROOT, "f")
rfd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
try:
    try:
        f = impl.open_regular_text_append(p)
        f.write("leak"); f.close(); err = "no-error"
    except OSError as e:
        err = e.errno
    try:
        got = os.read(rfd, 4096).decode()
    except BlockingIOError:
        got = ""
finally:
    os.close(rfd)
result = {"err": err, "einval": errno.EINVAL, "got": got}
""")
    assert r["err"] == r["einval"], r
    assert r["got"] == "", r  # zero bytes reached the reader


@pytest.mark.parametrize("impl", IMPLS)
def test_append_directory_and_dev_null_are_rejected(tmp_path, impl):
    (tmp_path / "d").mkdir()
    r = _child(impl, tmp_path, r"""
out = {"eisdir": errno.EISDIR, "einval": errno.EINVAL}
for name, p in (("dir", os.path.join(ROOT, "d")), ("null", "/dev/null")):
    try:
        impl.open_regular_text_append(p).close(); out[name] = "no-error"
    except OSError as e:
        out[name] = e.errno
result = out
""")
    assert r["dir"] == r["eisdir"], r
    if os.path.exists("/dev/null"):
        assert r["null"] == r["einval"], r


@pytest.mark.parametrize("impl", IMPLS)
def test_append_keeps_prior_content_and_matches_plain_open_a_mode(tmp_path, impl):
    r = _child(impl, tmp_path, r"""
os.umask(0o027)
a = os.path.join(ROOT, "a.txt")   # created by the helper
b = os.path.join(ROOT, "b.txt")   # created by plain open(, "a")
with impl.open_regular_text_append(a, encoding="utf-8") as f:
    f.write("café\n")
with open(b, "a", encoding="utf-8") as f:
    f.write("café\n")
mode_a = stat.S_IMODE(os.stat(a).st_mode); mode_b = stat.S_IMODE(os.stat(b).st_mode)
# append: existing content is KEPT (no O_TRUNC), the new text lands after it
with impl.open_regular_text_append(a, encoding="utf-8") as f:
    f.write("second\n")
result = {"mode_a": mode_a, "mode_b": mode_b, "text": open(a, encoding="utf-8").read()}
""")
    assert r["mode_a"] == r["mode_b"] == 0o640, r
    assert r["text"] == "café\nsecond\n", r


# ------------------------------------------------- loop-arm fallback copy (pinned)

def _defs_anywhere(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]


def _materialize_fallback_module() -> None:
    """Write a module holding ONLY the loop-arm hook's fallback defs, verbatim."""
    lines = LOOP_ARM.read_text(encoding="utf-8").splitlines()
    chunks = []
    for name in LOOP_ARM_FALLBACK:
        found = _defs_anywhere(LOOP_ARM, name)
        assert len(found) == 1, f"{LOOP_ARM.name}: expected exactly one def {name}"
        chunks.append(textwrap.dedent("\n".join(lines[found[0].lineno - 1:found[0].end_lineno])))
    (_GEN_DIR / f"{FALLBACK_MODULE}.py").write_text(
        "import os\nimport stat\n\n\n" + "\n\n\n".join(chunks) + "\n", encoding="utf-8")


IMPLS_OPEN = IMPLS + [FALLBACK_MODULE]


@pytest.fixture
def fallback_module():
    _materialize_fallback_module()


@_NEEDS_FIFO
@pytest.mark.parametrize("impl", IMPLS_OPEN)
def test_open_regular_text_fifo_without_and_with_writer(tmp_path, impl, fallback_module):
    os.mkfifo(tmp_path / "f")
    r = _child(impl, tmp_path, r"""
p = os.path.join(ROOT, "f")
out = {"einval": errno.EINVAL}
try:
    impl.open_regular_text(p).close(); out["no_writer"] = "no-error"
except OSError as e:
    out["no_writer"] = e.errno
wfd = os.open(p, os.O_RDWR | os.O_NONBLOCK)
try:
    os.write(wfd, b"payload")
    try:
        impl.open_regular_text(p).close(); out["writer"] = "no-error"
    except OSError as e:
        out["writer"] = e.errno
finally:
    os.close(wfd)
result = out
""")
    assert r["no_writer"] == r["einval"], r
    assert r["writer"] == r["einval"], r


@pytest.mark.parametrize("impl", IMPLS_OPEN)
def test_open_regular_text_dir_null_missing_and_regular(tmp_path, impl, fallback_module):
    (tmp_path / "d").mkdir()
    (tmp_path / "r.txt").write_bytes("héllo 世界\n".encode("utf-8"))
    r = _child(impl, tmp_path, r"""
out = {"eisdir": errno.EISDIR, "einval": errno.EINVAL}
for name, p in (("dir", os.path.join(ROOT, "d")), ("null", "/dev/null")):
    try:
        impl.open_regular_text(p).close(); out[name] = "no-error"
    except OSError as e:
        out[name] = e.errno
try:
    impl.open_regular_text(os.path.join(ROOT, "nope")); out["missing"] = "no-error"
except FileNotFoundError:
    out["missing"] = "FileNotFoundError"
with impl.open_regular_text(os.path.join(ROOT, "r.txt"), encoding="utf-8") as f:
    out["text"] = f.read()
    out["bytes_ok"] = True
with impl.open_regular_text(os.path.join(ROOT, "r.txt")) as f:
    out["raw"] = f.buffer.read().decode("utf-8")
result = out
""")
    assert r["dir"] == r["eisdir"], r
    if os.path.exists("/dev/null"):
        assert r["null"] == r["einval"], r
    assert r["missing"] == "FileNotFoundError", r
    assert r["text"] == "héllo 世界\n", r
    assert r["raw"] == "héllo 世界\n", r  # callers that need bytes read through .buffer


@pytest.mark.parametrize("name", LOOP_ARM_FALLBACK)
def test_loop_arm_fallback_helper_has_identical_ast_to_hooklib(name):
    found = _defs_anywhere(LOOP_ARM, name)
    assert len(found) == 1, f"{LOOP_ARM.name}: expected exactly one def {name}"
    assert _dump_fn(found[0]) == _helper_dump(HOOKLIB, name), (
        f"{name} in {LOOP_ARM.name} differs from _hooklib.py: the import-failure "
        "fallback copy must stay identical to the shared helper (#3628)")


# ------------------------------------------- (c) connect_regular_ro: ONE copy (#3628 2d)
#
# The sqlite helper exists only in `_hooklib.py` (no core caller needs it), so
# there is no AST-equality cell; these behavioural cells pin the one copy. The
# full contract, per-site cells and the guards live in
# tests/test_hooks_sqlite_regular_3628s2d.py. Residual, not closed: a regular
# file swapped for a FIFO between the helper's stat and its connect still hangs
# (#3577); no cell here claims otherwise.

@_NEEDS_FIFO
def test_connect_regular_ro_refuses_a_fifo_promptly_with_the_dual_class(tmp_path):
    os.mkfifo(tmp_path / "db")
    r = _child("_hooklib", tmp_path, r"""
import sqlite3
try:
    impl.connect_regular_ro(os.path.join(ROOT, "db")); result = "no-error"
except Exception as e:
    result = [isinstance(e, sqlite3.OperationalError), isinstance(e, OSError),
              "not a regular file" in str(e)]
""")
    assert r == [True, True, True], r


@pytest.mark.parametrize("kind", ["missing", "dangling"])
def test_connect_regular_ro_missing_and_dangling_raise_what_a_raw_connect_raises(tmp_path, kind):
    if kind == "dangling":
        os.symlink(str(tmp_path / "nowhere"), tmp_path / "db")
    r = _child("_hooklib", tmp_path, r"""
import sqlite3
p = os.path.join(ROOT, "db")
def err(f):
    try:
        f(); return None
    except Exception as e:
        return [type(e).__name__, str(e)]
result = [err(lambda: sqlite3.connect("file:" + p + "?mode=ro", uri=True)),
          err(lambda: impl.connect_regular_ro(p))]
""")
    assert r[0] is not None and r[0] == r[1], r


def test_connect_regular_ro_regular_file_reads(tmp_path):
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "db"))
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (7)")
    conn.commit()
    conn.close()
    r = _child("_hooklib", tmp_path, r"""
c = impl.connect_regular_ro(os.path.join(ROOT, "db"), timeout=0.2)
result = c.execute("SELECT x FROM t").fetchone()[0]
""")
    assert r == 7
