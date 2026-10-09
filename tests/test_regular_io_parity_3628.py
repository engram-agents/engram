"""#3628: the non-blocking regular-file helpers exist in TWO files on purpose.

`hooks/claude/_hooklib.py` must stay self-contained (tests copy it alone into
synthetic hooks/ layouts; every hook imports it), so it cannot import the core
module `engram_regular_io.py`. The four shared helpers are therefore
duplicated, and this file pins the two copies together two ways:

  (a) BEHAVIOURAL: one cell set, parametrised over BOTH implementations.
      Each cell runs in a child python with a hard timeout, so a regression
      that blocks on a FIFO fails (timeout) instead of hanging the suite.
  (b) TEXTUAL: each shared helper has an identical AST (docstring dropped) in
      both files, so the copies cannot drift even where no cell looks.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src" / "engram"
HOOK_DIR = SRC / "hooks" / "claude"
HOOKLIB = HOOK_DIR / "_hooklib.py"
CORE_IO = SRC / "engram_regular_io.py"

GUARD_S = int(os.environ.get("REGULAR_IO_PARITY_GUARD_S", "8"))
IMPLS = ["_hooklib", "engram_regular_io"]
SHARED = ["_not_regular_error", "open_regular_text", "read_regular_text",
          "open_regular_text_write"]

_PRELUDE = f"""
import sys, os, json, errno, stat
sys.path.insert(0, {str(HOOK_DIR)!r})
sys.path.insert(0, {str(SRC)!r})
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
    fn = funcs[0]
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
