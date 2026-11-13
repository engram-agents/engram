"""Atomic write and retrying read for ``config.json`` (#3607).

``config.json`` is read on every node creation (the current turn is stamped
from it) and rewritten on every turn advance.  A truncate-then-write in place
exposes an empty or half-written file to any other process that reads inside
that window.  This module is the one place same-UID code reads and writes it:

* ``write_config_json`` -- temp file in the same directory, fsync, copy the
  existing file's mode onto the temp, ``os.replace``.  A reader sees the old
  file or the new one, never a partial one.
* ``read_config_json`` -- absent file -> ``{}``; a parse failure is retried a
  few times (it absorbs the one remaining non-atomic writer, the cross-UID
  in-place copy in ``viz/data/config.py``), then raises
  ``ConfigUnreadableError`` naming the file and the parse position.  It never
  returns a sentinel: a caller that stamps turn 0 from a torn file sinks the
  node it is creating.

Why the temp is created with ``os.open(..., 0o666)`` and not ``mkstemp``:
``mkstemp`` creates the file 0600, and under a default ACL on the directory
(the install's data dir carries one) that mode sets the ACL mask to ``---``,
silently revoking the named entries' effective access once the temp replaces
the real file.  Creating with 0o666 lets the kernel apply the process umask or
the directory's default ACL exactly as ``Path.write_text`` did for a new file.
When the target already exists its mode bits are copied onto the temp with
``fchmod`` (which also resets the mask), so an existing file keeps its mode.
It also keeps its group, where permitted: ``os.replace`` would otherwise give
the new inode the process's egid (or the directory's group), dropping access
for another UID that reads through group permissions.  The group is restored
with ``fchown`` (before the ``fchmod``); a group the caller is not a member of
cannot be set without privilege and is left as the new inode's.  The owner is
not preserved (needs privilege; same-UID callers own the file anyway).

A symlinked ``config.json`` is written through (the link stays, its target is
updated), as ``write_text`` did.  The temp-and-rename needs write access to
the containing DIRECTORY, so ``write_config_json`` is for same-UID callers;
the cross-UID viz path keeps its in-place copy.
"""

import errno
import json
import os
import secrets
import stat
import time
from pathlib import Path

from engram_regular_io import read_regular_text

# os.getxattr / os.setxattr / os.listxattr / os.removexattr exist on Linux and
# FreeBSD only: macOS does not expose POSIX ACLs (or any file xattrs) through
# them, and calling a missing attribute raises AttributeError, not OSError.
# Decided once at import so the copy and the inherited-ACL removal below are a
# no-op, never an error, where the API is absent.
_XATTR_API = (
    hasattr(os, "getxattr")
    and hasattr(os, "setxattr")
    and hasattr(os, "listxattr")
    and hasattr(os, "removexattr")
)

# Read errors that mean "this attribute is absent or this filesystem cannot
# carry it": a tolerant read, used by the explicit ACL getxattr and by
# listxattr.  Here "nothing to copy" is not a failure -- a filesystem that
# cannot carry xattrs at all (ENOTSUP/EOPNOTSUPP/ENOSYS) and an attribute that
# is simply missing (ENODATA/ENOATTR) are both benign.
_NO_XATTR_ERRNOS = tuple(
    e
    for e in (
        getattr(errno, "ENODATA", None),
        getattr(errno, "ENOATTR", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOSYS", None),
    )
    if e is not None
)

# Read errors that mean only "the attribute vanished between listxattr and
# getxattr", used by the per-name read: it is genuinely absent, so nothing is
# lost.  Everything else -- notably EACCES and EPERM on a file the caller may
# write but not read -- raises, because metadata that cannot be read must not
# be dropped silently.
_VANISHED_XATTR_ERRNOS = tuple(
    e
    for e in (
        getattr(errno, "ENODATA", None),
        getattr(errno, "ENOATTR", None),
    )
    if e is not None
)


class ConfigUnreadableError(ValueError):
    """``config.json`` exists but does not parse after the retry window."""

    def __init__(self, path, original: json.JSONDecodeError):
        self.path = Path(path)
        self.original = original
        super().__init__(
            f"{self.path} is unreadable: invalid JSON at line {original.lineno} "
            f"column {original.colno} (char {original.pos}): {original.msg}. "
            "Refusing to continue on a guessed config; repair or restore the file."
        )


def atomic_write_text(path, text, *, encoding="utf-8", follow_symlinks: bool = True, mode=None, dir_fd=None) -> None:
    """Atomically replace ``path`` with ``text`` (#4013).  The one shared
    mode-, group-, ACL- and user-xattr-preserving writer: an existing file keeps
    its permission bits and, where permitted, its group; a new file gets what ``write_text``
    would give it (umask or the directory's default ACL).  By default a symlink
    is followed (the link stays, its target changes).  With
    ``follow_symlinks=False`` the path itself is replaced: a symlink (live or
    dangling) is swapped for a regular file, what it pointed at is never
    touched or created, and mode and group are copied only from an existing
    REGULAR file, never from a link's target.  Pass ``follow_symlinks=False`` for any path under a
    directory other users can write (a planted link would otherwise redirect the write); the default
    True exists for paths the caller owns and links on purpose, such as config.json via
    ``write_config_json``.  ``mode``, when given, is applied with fchmod to the
    TEMP file before the atomic rename (#4682) — so a caller placing a file in a
    directory other users can write never needs a post-replace chmod that an
    attacker could redirect through a swapped link.

    ``dir_fd`` binds EVERY step — the existing-target probe, the temp creation,
    the atomic replace and the failure cleanup — to an open directory
    descriptor instead of re-resolving ``path`` (#4682 TOCTOU): a co-tenant who
    swaps a path component for a symlink AFTER the caller opened and checked
    the directory cannot redirect any step, because no step looks at the path
    again.  ``path`` must then be a bare basename, and anything else is refused
    with ValueError: an ABSOLUTE path makes os.open/os.replace ignore ``dir_fd``
    entirely (no error, no pinning), and a relative path carrying a component
    resolves into a SUBDIRECTORY that was never the directory the caller opened
    and checked.  Either form would quietly reintroduce the hole.  (Measured,
    not assumed: a relative ``sub/f`` under ``dir_fd`` really does land in the
    pinned directory, so the refusal is about the directory that was verified,
    not about ``dir_fd`` being ignored wholesale.)  The temp is staged 0o600
    (private) in that case, even for a new target; pass ``mode`` for the final
    bits.

    Needs write access to the
    containing directory (same-UID
    callers).  On any error the temp file is removed and the error re-raised;
    ``path`` is never left partial.  Named POSIX ACL entries
    (``system.posix_acl_access``) and ``user.*`` extended attributes are copied
    from the existing file onto the temp before the publish; when the existing
    file has NO access ACL, the temp's inherited access ACL is REMOVED instead,
    so the write cannot grant access the replaced file never had.  A new target
    keeps the directory's default ACL by design: there is nothing to replace,
    and a plain ``open`` would inherit it too.  That copy (or removal) is the
    LAST metadata step before the publish, and must stay last: the mode and
    group are restored first, and ``fchmod`` rewrites the ACL mask (which is the
    final word on the group bits), so a mode change after the ACL is applied
    would silently alter a preserved ACL.  Only the ``user.*`` namespace is
    copied: ``security.*`` and ``trusted.*`` are frequently unreadable or
    unsettable by an unprivileged writer, so attempting them would turn a normal
    write into a failure on SELinux hosts.  The access ACL is read explicitly
    rather than inferred from ``listxattr``: whether that call reports
    ``system.posix_acl_access`` is filesystem-dependent (some list it, some do
    not), and the copy must not depend on which; ``system.posix_acl_default`` is
    a directory attribute and does not apply to a regular file.  Where the
    platform exposes no xattr API (macOS), the copy is a no-op, never an error.
    A per-name read that returns ENODATA/ENOATTR (the attribute vanished between
    ``listxattr`` and ``getxattr``) is skipped: it is genuinely absent, so
    nothing is lost.  Any other error on a per-name read -- not the explicit ACL
    ``getxattr`` or ``listxattr`` above, which tolerate the whole
    absent-or-unsupported set -- notably EACCES/EPERM on a file the caller may
    write but not read, fails the write -- a bounded behaviour change from the
    writer before #4641, which wrote successfully and dropped that attribute,
    because metadata that cannot be read must not be lost silently.
    If the source carries one of these and it cannot be applied to the temp, the
    write raises, the temp is removed, and the target is left untouched.  Note
    the ownership assumption: in a directory other users can write, the source's
    ACL and ``user.*`` values are attacker-controlled and are propagated to the
    new inode -- the same class of trust as the pre-existing mode copy, stated
    here rather than left implied.
    Under ``dir_fd`` that copy is SKIPPED: ``os.getxattr``/``os.setxattr``
    accept no ``dir_fd`` and ``target`` is then a bare name resolved against the
    process CWD.  A pre-existing target's named ACL entries and ``user.*``
    xattrs are therefore NOT carried over in that mode -- its mode and group
    still are -- so a caller that needs both must use a path-based write.
    Kept AST-identical with
    ``hooks/claude/_hooklib.atomic_write_text`` (tests/test_atomic_write_parity_4013.py)."""
    if dir_fd is None:
        target = os.path.realpath(path) if follow_symlinks else os.path.abspath(path)
        try:
            st = os.stat(target) if follow_symlinks else os.lstat(target)
        except OSError:
            st = None
        copy_mode = st is not None and (follow_symlinks or stat.S_ISREG(st.st_mode))
    else:
        # The caller pinned the containing directory with an fd.  A bare name
        # is the only form that resolves INSIDE that directory: an absolute
        # path makes os.open/os.replace ignore dir_fd (no error, no pinning),
        # and a relative path carrying a component resolves into a
        # subdirectory nobody checked.  Both are refused rather than silently
        # reintroducing the symlink-swap hole.
        if os.path.dirname(path):
            raise ValueError(
                "atomic_write_text(dir_fd=...) needs a bare basename, got "
                f"{path!r}: an absolute path makes os.open/os.replace ignore "
                "dir_fd, and a relative subpath resolves below the verified "
                "directory, so the write would not be pinned"
            )
        target = path
        try:
            st = os.stat(target, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            st = None
        copy_mode = st is not None and stat.S_ISREG(st.st_mode)
    # An existing file's staging copy starts private (0600) and gets the old mode
    # only after the payload is written; a new file or a replaced link keeps the
    # umask / default-ACL policy of a plain open().  Through dir_fd the staging
    # copy is ALWAYS private: it lands inside a directory other users can write,
    # so pass `mode` for the final bits.
    create_mode = 0o600 if copy_mode or dir_fd is not None else 0o666
    for _ in range(8):
        tmp = f"{target}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create_mode, dir_fd=dir_fd)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"could not create a unique temp file beside {target}")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
            if copy_mode:
                try:
                    # Before fchmod: chown can clear mode bits on some systems.
                    os.fchown(fh.fileno(), -1, st.st_gid)
                except PermissionError:
                    pass  # not a member of that group: the new inode keeps its own
                os.fchmod(fh.fileno(), stat.S_IMODE(st.st_mode))
            if mode is not None:
                os.fchmod(fh.fileno(), mode)
            # ACL entries and user.* xattrs are inode metadata too, and the
            # rename installs a new inode: read them from the file being
            # replaced and apply them to the temp.  This is the LAST metadata
            # step before the publish, deliberately: fchmod rewrites the ACL
            # mask (the mask is the final word on the group bits), so a mode
            # change after setxattr would silently alter the preserved ACL.
            # It must stay after every fchmod, INCLUDING the mode= above.
            # Under dir_fd the copy is SKIPPED (see the closure below): target
            # is then a bare name resolved against the process CWD, and
            # os.getxattr/os.setxattr take no dir_fd parameter.  Without the
            # guard the read raises ENOENT -- which is NOT in the tolerance
            # set -- on every write that REPLACES an existing file, i.e. from
            # the second session on, while the first one still worked.
            if copy_mode and _XATTR_API and dir_fd is None:
                try:
                    acl = os.getxattr(target, "system.posix_acl_access", follow_symlinks=False)
                except OSError as exc:
                    if exc.errno not in _NO_XATTR_ERRNOS:
                        raise
                    # No access ACL on the file being replaced.  The temp was
                    # created in this same directory, so it may have INHERITED
                    # the directory's default ACL, which the target never had;
                    # publishing it would grant access the target's metadata
                    # never allowed.  Remove it here, at the same position as
                    # the copy and for the same reason: every fchmod has
                    # already run (a mode change after the ACL work would alter
                    # the mask), and os.replace has not run yet.
                    try:
                        os.removexattr(tmp, "system.posix_acl_access", follow_symlinks=False)
                    except OSError as remove_exc:
                        if remove_exc.errno not in _NO_XATTR_ERRNOS:
                            raise
                else:
                    os.setxattr(tmp, "system.posix_acl_access", acl, follow_symlinks=False)
                try:
                    names = os.listxattr(target, follow_symlinks=False)
                except OSError as exc:
                    if exc.errno not in _NO_XATTR_ERRNOS:
                        raise
                    names = ()
                for name in names:
                    if name.startswith("user."):
                        try:
                            value = os.getxattr(target, name, follow_symlinks=False)
                        except OSError as exc:
                            if exc.errno in _VANISHED_XATTR_ERRNOS:
                                continue
                            raise
                        os.setxattr(tmp, name, value, follow_symlinks=False)
        os.replace(tmp, target, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def write_config_json(path, data) -> None:
    """Atomically replace ``path`` with ``data`` as JSON (indent=2, trailing
    newline) through ``atomic_write_text``: mode and group of an existing file
    are kept, a symlink is followed, the temp is removed on error."""
    atomic_write_text(path, json.dumps(data, indent=2) + "\n", encoding="utf-8")


def read_config_json(path, *, attempts: int = 3, delay: float = 0.05) -> dict:
    """The parsed ``config.json``; ``{}`` when the file is absent (callers
    keep their own defaults).  A parse failure is retried up to ``attempts``
    reads, ``delay`` seconds apart, then raises ``ConfigUnreadableError``."""
    path = Path(path)
    last = None
    for i in range(max(1, attempts)):
        if i:
            time.sleep(delay)
        try:
            # Non-blocking and regular-file-only (#3628): a FIFO at config.json
            # raises OSError (as a directory does) instead of hanging the caller.
            text = read_regular_text(path, encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            last = e
    raise ConfigUnreadableError(path, last) from last
