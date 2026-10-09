"""Non-blocking, regular-file-only text opens (#3591, #3628).

A plain ``open()`` of a path where a FIFO sits waits for a writer (read) or a
reader (write).  A process under a hard timeout -- a SessionStart hook has 5 s
-- hangs and is killed silently, and a ``try/except`` is fail-open for an
exception, not for a hang.  These helpers open with ``O_NONBLOCK`` (returns at
once on a FIFO), ``fstat`` the descriptor, and refuse anything that is not a
regular file with an ``OSError``, so a call site that already treats an
unreadable file as absent needs only ``open(p)`` -> ``open_regular_text(p)``.

Layering rule: hooks import core, never the reverse; core code must not import
from ``hooks/``.  This module is core (stdlib-only) so that both core readers
(``engram_config_io``, ``engram_log_emitter``) and the hooks' shared
``_hooklib`` use the ONE implementation; ``_hooklib`` keeps its own
byte-identical copy of the four shared helpers (it must stay importable alone),
pinned identical by tests/test_regular_io_parity_3628.py.
"""

from __future__ import annotations

import os
import stat


def _not_regular_error(fd, path):
    """The OSError for a path that opened but is not a regular file. A
    directory keeps the IsADirectoryError a plain open() raises for it; any
    other kind (FIFO, socket, device) is an EINVAL OSError. Does not close fd:
    the caller's except clause does, once."""
    import errno
    try:
        is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
    except OSError:
        is_dir = False
    code = errno.EISDIR if is_dir else errno.EINVAL
    return OSError(code, os.strerror(code) + " (not a regular file)", str(path))


def open_regular_text(path, encoding=None):
    """`open(path, encoding=encoding)` for a read that must never block (#3591).

    A plain open() of a FIFO waits for a writer; a hook under a 5 s timeout
    hangs and is killed. This opens O_NONBLOCK (returns at once on a FIFO),
    fstat-checks that the target is a regular file, and returns a text file
    object over the fd. Anything else raises OSError (IsADirectoryError for a
    directory, EINVAL otherwise), so a call site that already treats an
    unreadable file as absent -- `except OSError` or broader -- needs only
    `open(p)` -> `open_regular_text(p)`. FileNotFoundError / PermissionError
    come from os.open exactly as from open(). For a regular file the result is
    the same text wrapper open() returns."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _not_regular_error(fd, path)
        return os.fdopen(fd, "r", encoding=encoding)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def read_regular_text(path, encoding=None):
    """`Path(path).read_text(encoding=encoding)` that must never block on a
    FIFO (#3591): the whole text of a regular file, OSError otherwise (see
    open_regular_text)."""
    with open_regular_text(path, encoding=encoding) as f:
        return f.read()


def open_regular_text_write(path, encoding=None):
    """`open(path, "w", encoding=encoding)` that must never block (#3591).

    A write-open of a FIFO waits for a reader. O_WRONLY|O_NONBLOCK makes that
    fail at once with ENXIO when no reader exists (an OSError); and when a
    reader does exist the fstat still rejects the FIFO before anything is
    written. O_CREAT|O_TRUNC with mode 0o666 (umask applies) matches what
    open(path, "w") does, so a regular file is created / truncated and written
    exactly as before. Raises OSError for a non-regular target."""
    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _not_regular_error(fd, path)
        return os.fdopen(fd, "w", encoding=encoding)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def open_regular_text_append(path, encoding=None):
    """`open(path, "a", encoding=encoding)` that must never block (#3628).

    Same shape as open_regular_text_write, with O_APPEND|O_CREAT (no O_TRUNC)
    and mode 0o666 (umask applies), matching open(path, "a"). A reader-less
    FIFO fails at os.open with ENXIO; a FIFO that has a reader is rejected by
    the fstat before any byte is written. Raises OSError for a non-regular
    target."""
    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_APPEND | os.O_CREAT, 0o666)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _not_regular_error(fd, path)
        return os.fdopen(fd, "a", encoding=encoding)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
