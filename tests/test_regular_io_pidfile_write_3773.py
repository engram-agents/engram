"""The shared pidfile-write primitive in engram_regular_io -- issue #3773.

Contract (state of the world -> result), asserted here BEFORE the primitive
existed:

  describe_file_kind(mode)  one word per kind: FIFO, symlink, directory, socket,
                            character device, block device, else "non-regular file".
  NotRegularFile            an OSError; str(exc) is "<path> is a <kind>, not a
                            regular file" or "<path> has <n> hard links, not a
                            single regular file"; carries .path .st_mode .nlink .kind.
  write_pidfile(path, data) opens O_WRONLY|O_CREAT|O_NOFOLLOW|O_NONBLOCK (no
                            O_TRUNC), maps ENXIO/ELOOP/EISDIR/EOPNOTSUPP/ENODEV to
                            NotRegularFile, lets every other OSError through
                            unchanged, fstat-refuses a non-regular or multi-link
                            target, and ONLY THEN ftruncates and writes. A refused
                            target is never truncated or written; the fd is closed.
  kind_of_path(path)        os.stat (follows links, never opens): the kind word if
                            the path resolves to a non-regular file, else None.

Anything that can block runs in a CHILD python under a kill timeout, so a
regression is a red cell and not a hung run. Each cell's docstring names the
mutation that must redden it.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if SRC_ENGRAM not in sys.path:
    sys.path.insert(0, SRC_ENGRAM)

import engram_regular_io as rio  # noqa: E402

BOUND_S = 5.0

_PRELUDE = r"""
import json, os, socket, stat, sys
sys.path.append(sys.argv[1])
import engram_regular_io as rio
tmp = sys.argv[2]
def plant(kind, path, sentinel=None):
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        os.symlink(sentinel, path)
    elif kind == "hardlink":
        os.link(sentinel, path)
    elif kind == "dir":
        os.mkdir(path)
    elif kind == "sock":
        sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sk.bind(path)
        sk.close()
"""


def _child(tmp_path, body):
    code = _PRELUDE + body + "\nprint('RESULT:' + json.dumps(result))\n"
    try:
        done = subprocess.run([sys.executable, "-I", "-c", code, SRC_ENGRAM, str(tmp_path)],
                              capture_output=True, text=True, timeout=BOUND_S)
    except subprocess.TimeoutExpired:
        pytest.fail(f"HUNG: no return within {BOUND_S}s")
    assert done.returncode == 0, done.stderr
    line = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT:")][-1]
    return json.loads(line[len("RESULT:"):])


_ATTEMPT = r"""
path = os.path.join(tmp, "p.pid")
sentinel = os.path.join(tmp, "sentinel.txt")
open(sentinel, "wb").write(b"x" * 135)
plant(KIND, path, sentinel)
result = {"exc": None}
try:
    rio.write_pidfile(path, b"4242")
except BaseException as exc:
    result = {"exc": type(exc).__name__, "is_nrf": isinstance(exc, rio.NotRegularFile),
              "is_oserror": isinstance(exc, OSError), "text": str(exc), "kind": getattr(exc, "kind", None),
              "nlink": getattr(exc, "nlink", None)}
result["sentinel"] = open(sentinel, "rb").read().decode()
result["lexists"] = os.path.lexists(path)
"""


def _attempt(tmp_path, kind):
    return _child(tmp_path, f"KIND = {kind!r}\n" + _ATTEMPT)


# ------------------------------------------------------------ describe_file_kind

def test_describe_file_kind_names_every_kind(tmp_path):
    """Mutation: any branch returning the generic word, or two kinds swapped."""
    fifo = tmp_path / "f"
    os.mkfifo(fifo)
    link = tmp_path / "l"
    os.symlink("nowhere", link)
    d = tmp_path / "d"
    d.mkdir()
    sock = tmp_path / "s"
    sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sk.bind(str(sock))
    sk.close()
    assert rio.describe_file_kind(os.stat(fifo).st_mode) == "FIFO"
    assert rio.describe_file_kind(os.lstat(link).st_mode) == "symlink"
    assert rio.describe_file_kind(os.stat(d).st_mode) == "directory"
    assert rio.describe_file_kind(os.stat(sock).st_mode) == "socket"
    assert rio.describe_file_kind(os.stat("/dev/null").st_mode) == "character device"
    assert rio.describe_file_kind(stat.S_IFBLK | 0o660) == "block device"
    assert rio.describe_file_kind(0) == "non-regular file"


def test_not_regular_file_is_an_oserror_with_the_documented_text_and_fields():
    """Mutation: not an OSError subclass; wrong text; a missing field."""
    exc = rio.NotRegularFile("/x/p.pid", stat.S_IFIFO | 0o600, 1)
    assert isinstance(exc, OSError)
    assert str(exc) == "/x/p.pid is a FIFO, not a regular file"
    assert (exc.path, exc.st_mode, exc.nlink, exc.kind) == ("/x/p.pid", stat.S_IFIFO | 0o600, 1, "FIFO")
    multi = rio.NotRegularFile("/x/p.pid", stat.S_IFREG | 0o600, 3)
    assert str(multi) == "/x/p.pid has 3 hard links, not a single regular file"
    assert multi.kind == "hard-linked file" and multi.nlink == 3
    unknown = rio.NotRegularFile("/x/p.pid", None, None)
    assert unknown.st_mode is None and unknown.kind == "non-regular file"
    assert str(unknown) == "/x/p.pid is a non-regular file, not a regular file"


# ------------------------------------------------------------ the refusal rows

def test_fifo_without_reader_is_refused_not_hung(tmp_path):
    """Mutation: open without O_NONBLOCK (the cell is killed at the bound)."""
    r = _attempt(tmp_path, "fifo")
    assert r["is_nrf"] and r["kind"] == "FIFO" and "FIFO" in r["text"]
    assert r["sentinel"] == "x" * 135 and r["lexists"]


def test_fifo_with_a_reader_is_refused_by_fstat_and_the_reader_gets_nothing(tmp_path):
    """The open SUCCEEDS here (a reader exists); only the fstat refuses.
    Mutation: drop the S_ISREG check, or write before it."""
    body = r"""
path = os.path.join(tmp, "p.pid")
os.mkfifo(path)
rd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
calls = []
real = os.ftruncate
os.ftruncate = lambda *a: (calls.append(a), real(*a))[1]
result = {}
try:
    rio.write_pidfile(path, b"4242")
except BaseException as exc:
    result = {"is_nrf": isinstance(exc, rio.NotRegularFile), "kind": getattr(exc, "kind", None)}
os.ftruncate = real
try:
    got = os.read(rd, 64).decode("latin-1")
except BlockingIOError:
    got = "EAGAIN"
result["got"] = got
result["ftruncate_calls"] = len(calls)
"""
    r = _child(tmp_path, body)
    assert r["is_nrf"] and r["kind"] == "FIFO"
    assert r["got"] in ("", "EAGAIN")  # nothing received: EOF (writer closed) or no data yet
    assert r["ftruncate_calls"] == 0


def test_symlink_to_sentinel_is_refused_and_the_sentinel_is_untouched(tmp_path):
    """Mutation: drop O_NOFOLLOW (the write goes through the link)."""
    r = _attempt(tmp_path, "symlink")
    assert r["is_nrf"] and r["kind"] == "symlink"
    assert r["sentinel"] == "x" * 135


def test_hard_link_to_sentinel_is_refused_and_the_other_name_is_untouched(tmp_path):
    """The cell that goes red if O_TRUNC is added to the open flags: the open
    would truncate the shared inode before the nlink check can refuse.
    Mutation: O_TRUNC in the flags; the st_nlink != 1 check removed."""
    r = _attempt(tmp_path, "hardlink")
    assert r["is_nrf"] and "2 hard links" in r["text"] and r["kind"] == "hard-linked file"
    assert r["nlink"] == 2
    assert r["sentinel"] == "x" * 135


def test_directory_is_refused(tmp_path):
    """Mutation: EISDIR dropped from the errno mapping."""
    r = _attempt(tmp_path, "dir")
    assert r["is_nrf"] and r["kind"] == "directory"


def test_real_socket_file_is_refused(tmp_path):
    """Linux gives ENXIO for a socket-type file; macOS gives EOPNOTSUPP (see the
    injected row below and the darwin-only cell). Mutation: ENXIO dropped."""
    r = _attempt(tmp_path, "sock")
    assert r["is_nrf"] and r["kind"] == "socket"


@pytest.mark.skipif(sys.platform != "darwin", reason="needs a Mac seat; no macOS runners in CI")
def test_darwin_socket_file_open_gives_eopnotsupp_and_is_refused(tmp_path):
    """macOS only: the real kernel errno for a socket-type file."""
    r = _attempt(tmp_path, "sock")
    assert r["is_nrf"] and r["kind"] == "socket"


_NOT_REGULAR_ERRNOS = ["ENXIO", "ELOOP", "EISDIR", "EOPNOTSUPP", "ENODEV"]


@pytest.mark.parametrize("name", _NOT_REGULAR_ERRNOS)
def test_each_not_regular_errno_at_the_open_boundary_becomes_not_regular_file(tmp_path, monkeypatch, name):
    """Injected at os.open so the macOS EOPNOTSUPP row also runs on Linux.
    Mutation: that one errno removed from the mapping -> only its row is red."""
    code = getattr(errno, name, None)
    if code is None:
        pytest.skip(f"errno.{name} does not exist on this platform")
    target = tmp_path / "p.pid"
    target.write_text("old")
    real_open = os.open

    def fake_open(path, flags, *a, **k):
        if str(path) == str(target):
            raise OSError(code, os.strerror(code), str(path))
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(rio.os, "open", fake_open)
    with pytest.raises(rio.NotRegularFile) as ei:
        rio.write_pidfile(target, b"1")
    assert ei.value.__cause__.errno == code
    assert target.read_text() == "old"


def test_eacces_propagates_as_a_plain_oserror(tmp_path, monkeypatch):
    """Mutation: every OSError mapped to NotRegularFile."""
    target = tmp_path / "p.pid"
    real_open = os.open

    def fake_open(path, flags, *a, **k):
        if str(path) == str(target):
            raise PermissionError(errno.EACCES, "denied", str(path))
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(rio.os, "open", fake_open)
    with pytest.raises(OSError) as ei:
        rio.write_pidfile(target, b"1")
    assert not isinstance(ei.value, rio.NotRegularFile)
    assert ei.value.errno == errno.EACCES


def test_enoent_propagates_unchanged(tmp_path):
    """A missing parent directory is not a not-regular-file refusal."""
    with pytest.raises(FileNotFoundError):
        rio.write_pidfile(tmp_path / "nodir" / "p.pid", b"1")


# ------------------------------------------------------------ the success rows

def test_longer_stale_content_leaves_no_tail(tmp_path):
    """Mutation: ftruncate removed (a 100-byte stale file keeps its tail)."""
    p = tmp_path / "p.pid"
    p.write_bytes(b"9" * 100)
    rio.write_pidfile(p, b"123")
    assert p.read_bytes() == b"123"


def test_absent_path_is_created_regular_with_the_umask_mode(tmp_path):
    """Mutation: O_CREAT dropped; mode other than 0o666 & ~umask."""
    p = tmp_path / "p.pid"
    old = os.umask(0o022)
    try:
        rio.write_pidfile(p, b"77")
    finally:
        os.umask(old)
    st = os.stat(p)
    assert stat.S_ISREG(st.st_mode) and p.read_bytes() == b"77"
    assert stat.S_IMODE(st.st_mode) == 0o644


# ------------------------------------------------------------ no write, no leak

@pytest.mark.parametrize("kind", ["symlink", "hardlink", "dir"])
def test_ftruncate_is_never_called_on_a_refusal_path(tmp_path, monkeypatch, kind):
    """Spy. Mutation: ftruncate placed before the checks."""
    path = tmp_path / "p.pid"
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_bytes(b"x" * 135)
    if kind == "symlink":
        os.symlink(sentinel, path)
    elif kind == "hardlink":
        os.link(sentinel, path)
    else:
        path.mkdir()
    calls = []
    real = os.ftruncate
    monkeypatch.setattr(rio.os, "ftruncate", lambda *a: (calls.append(a), real(*a))[1])
    with pytest.raises(rio.NotRegularFile):
        rio.write_pidfile(path, b"1")
    assert calls == []
    assert sentinel.read_bytes() == b"x" * 135


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="fd count needs /proc/self/fd (Linux)")
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo_reader"])
def test_a_refusal_closes_its_fd(tmp_path, kind):
    """Mutation: the fd left open on the fstat-refusal path."""
    path = tmp_path / "p.pid"
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_bytes(b"x")
    held = None
    if kind == "symlink":
        os.symlink(sentinel, path)
    elif kind == "hardlink":
        os.link(sentinel, path)
    else:
        os.mkfifo(path)
        held = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            with pytest.raises(rio.NotRegularFile):
                rio.write_pidfile(path, b"1")
        after = len(os.listdir("/proc/self/fd"))
    finally:
        if held is not None:
            os.close(held)
    assert after == before


# ------------------------------------------------------------ kind_of_path

def test_kind_of_path_rows(tmp_path):
    """Mutation: lstat instead of stat (symlink-to-FIFO would read "symlink");
    a regular file or an absent path returning a word."""
    fifo = tmp_path / "f"
    os.mkfifo(fifo)
    link = tmp_path / "l"
    os.symlink(fifo, link)
    d = tmp_path / "d"
    d.mkdir()
    reg = tmp_path / "r"
    reg.write_text("1")
    reg_link = tmp_path / "rl"
    os.symlink(reg, reg_link)
    assert rio.kind_of_path(fifo) == "FIFO"
    assert rio.kind_of_path(link) == "FIFO"
    assert rio.kind_of_path(d) == "directory"
    assert rio.kind_of_path(reg) is None
    assert rio.kind_of_path(reg_link) is None
    assert rio.kind_of_path(tmp_path / "absent") is None
    assert rio.kind_of_path(tmp_path / "dangling-parent" / "x") is None
