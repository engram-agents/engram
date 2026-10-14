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
copy of the shared helpers (it must stay importable alone), pinned identical by
tests/test_regular_io_parity_3628.py. ``read_state_json_classified`` (#3521 row
40) is one of them: the UNKNOWN-state read rule for principle-trigger-state.json
that core's ``_reset_principle_enactments`` and the two hooks all share.
"""

from __future__ import annotations

import errno
import json
import os
import stat


def describe_file_kind(mode: int) -> str:
    """The one word for what `mode` (an st_mode) says a non-regular path is
    (#3773). The ONE kind mapping: the engramd owner and the collab tools all
    name a refused path through it. Anything not listed (and a regular mode,
    which callers do not ask about) reads "non-regular file"."""
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character device"
    if stat.S_ISBLK(mode):
        return "block device"
    return "non-regular file"


class NotRegularFile(OSError):
    """A pidfile write target that is not a single regular file (#3773).

    str(exc) is "<path> is a <kind>, not a regular file", or, for a regular
    file with st_nlink != 1, "<path> has <n> hard links, not a single regular
    file". Fields: .path, .st_mode (int | None), .nlink (int | None), .kind
    (the describe_file_kind word, or "hard-linked file")."""

    def __init__(self, path, st_mode=None, nlink=None):
        self.path = str(path)
        self.st_mode = st_mode
        self.nlink = nlink
        if st_mode is not None and stat.S_ISREG(st_mode) and nlink is not None and nlink != 1:
            self.kind = "hard-linked file"
            text = f"{self.path} has {nlink} hard links, not a single regular file"
        else:
            self.kind = describe_file_kind(st_mode) if st_mode is not None else "non-regular file"
            text = f"{self.path} is a {self.kind}, not a regular file"
        super().__init__(text)

    def __str__(self) -> str:
        return self.args[0]


def write_pidfile(path, data: bytes) -> None:
    """Write `data` to a pidfile that must be ONE regular file (#3773; the
    sequence the engramd owner merged in #3768, now shared).

    Open with O_WRONLY|O_CREAT|O_NOFOLLOW|O_NONBLOCK and mode 0o666 (umask
    applies) and NO O_TRUNC: O_NOFOLLOW refuses a symlink (on a write the
    follow IS the truncation of its target), O_NONBLOCK makes a reader-less
    FIFO fail with ENXIO instead of waiting. ENXIO, ELOOP, EISDIR, EOPNOTSUPP
    and ENODEV become NotRegularFile; any other OSError propagates unchanged.
    Then fstat the open fd: not a regular file, or st_nlink != 1 (a hard link
    shares another name's inode) -> close it and raise NotRegularFile. Only
    then ftruncate and write, so a refused target is never touched.

    This write path REFUSES symlinks on purpose; the read side
    (open_regular_text) follows them. Do not "fix" one side to match the other.

    Two stated bounds of the hard-link guard:
      * st_nlink is not trustworthy on every filesystem. On an odd or
        network-mounted home a LEGITIMATE pidfile can report st_nlink != 1 and
        be refused, so its caller does not start. That is the failure
        direction of this guard, opposite to the one it was added for.
      * A hard link created between the fstat and the ftruncate still
        truncates the shared inode. The window is two syscalls deep, it needs
        write access to the directory that holds the pidfile (the same
        precondition the guard already assumes), and there is no portable
        remedy."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags, 0o666)
    except OSError as exc:
        # getattr so a platform lacking one of them still imports.
        not_regular = {
            getattr(errno, name)
            for name in ("ENXIO", "ELOOP", "EISDIR", "EOPNOTSUPP", "ENODEV")
            if hasattr(errno, name)
        }
        if exc.errno not in not_regular:
            raise
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            mode = None
        raise NotRegularFile(path, mode, None) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise NotRegularFile(path, st.st_mode, st.st_nlink)
        os.ftruncate(fd, 0)
        os.write(fd, data)
    finally:
        os.close(fd)


def kind_of_path(path):
    """What `path` resolves to, in one word, when it is not a regular file
    (#3773): the describe_file_kind word, or None for a regular file, an
    absent path, or one that cannot be stat'ed. os.stat follows symlinks and
    never opens, so it cannot block on a FIFO. It exists so a READ failure
    can say what the file is in one line."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if stat.S_ISREG(mode):
        return None
    return describe_file_kind(mode)


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


def read_state_json_classified(path):
    """Read a JSON-object state file and CLASSIFY the outcome (#3521 row 40).

    Returns `(state, unknown_reason, movable)`; never raises:

      * ABSENT (FileNotFoundError)              -> `({}, None, False)`
      * a SYMLINK (even to a valid file, even
        dangling)                               -> `({}, "SymlinkStatePath: ...", False)`
      * a JSON object (dict)                    -> `(state, None, False)`
      * bad JSON / undecodable bytes / absurd
        nesting (the bytes WERE read)           -> `({}, "<Class>: <msg>", True)`
      * valid JSON that is not an object        -> `({}, "NotADict: <type> ...", True)`
      * ANY OSError (PermissionError, EMFILE,
        EIO, a FIFO / directory ...)            -> `({}, "<Class>: <msg>", False)`

    `movable` is True ONLY when the file was actually READ and found unusable:
    the maintainer's ruling moves aside the UNPARSEABLE file, and an unreadable
    file has not been shown unparseable. A transient OSError (an fd limit, an
    I/O hiccup) on a perfectly valid file must never cost its history, so every
    OSError is UNKNOWN and never movable: the caller neither moves nor writes
    it, and the bytes stay where they are. The reason string is
    `"<ExceptionClass>: <message>"`, the shape `split_probe_reason` /
    `probe_unknown_line` already consume. The read never blocks (see
    open_regular_text).

    A symlinked path is UNKNOWN and never movable (maintainer ruling 5): the
    read follows a link and fstat-checks its TARGET, so a link to a regular
    file would read as regular, and the move-aside `os.replace` would rename
    the LINK and leave the corrupt target in place behind a `.corrupt` link.
    `os.lstat` runs first; a dangling link is still a link (UNKNOWN, not
    absent). Core's copy of `_hooklib.read_state_json_classified` (core may not
    import a hook module); kept AST-identical, pinned by
    tests/test_regular_io_parity_3628.py."""
    try:
        try:
            lst = os.lstat(path)
        except FileNotFoundError:
            return {}, None, False
        except OSError as exc:
            return {}, f"{type(exc).__name__}: {exc}", False
        if stat.S_ISLNK(lst.st_mode):
            return {}, (
                "SymlinkStatePath: state path is a symlink; replace the link "
                "with a regular file to restore cooldowns"
            ), False
        try:
            data = json.loads(read_regular_text(path))
        except FileNotFoundError:
            return {}, None, False
        except (ValueError, RecursionError) as exc:  # bad JSON, bad bytes, absurd nesting
            return {}, f"{type(exc).__name__}: {exc}", True
        except OSError as exc:
            return {}, f"{type(exc).__name__}: {exc}", False
        if not isinstance(data, dict):
            return {}, f"NotADict: state is a {type(data).__name__}, not an object", True
        return data, None, False
    except Exception as exc:  # defensive: the contract is "never raises"
        return {}, f"{type(exc).__name__}: {exc}", False
