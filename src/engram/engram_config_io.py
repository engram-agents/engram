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

import json
import os
import re
import secrets
import shlex
import stat
import sys
import time
from pathlib import Path

import engram_file_guard as _fg
from engram_regular_io import read_regular_text

# Newest backups kept by ``write_engram_config`` (#1989).
BACKUP_KEEP = 7
# Lost-key names listed in a ConfigShrinkError message (the rest are counted).
SHRINK_NAMES_SHOWN = 20


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


class ConfigShapeError(ValueError):
    """``write_engram_config`` was handed something that is not a dict."""


class ConfigShrinkError(ValueError):
    """A config write would drop top-level keys the file on disk has (#1989).
    The message carries key NAMES and the path only, never values."""

    def __init__(self, path, lost_keys):
        self.path = Path(path)
        self.lost_keys = sorted(lost_keys)
        shown = ", ".join(self.lost_keys[:SHRINK_NAMES_SHOWN])
        more = "" if len(self.lost_keys) <= SHRINK_NAMES_SHOWN else ", ..."
        super().__init__(
            f"refusing to write {self.path}: it would lose {len(self.lost_keys)} top-level "
            f"key(s) the file on disk has (first {min(len(self.lost_keys), SHRINK_NAMES_SHOWN)}, "
            f"sorted: {shown}{more}). Nothing was written."
        )


class ConfigMissingError(FileNotFoundError):
    """config.json is absent where a read-modify-write needs it (#1989).  Never
    regenerated from empty: that turns a deletion into a plausible-looking file."""

    def __init__(self, path):
        self.path = Path(path)
        backups = backup_paths(self.path)
        if backups:
            newest = shlex.quote(str(backups[-1]))
            cmd = f"cp -p {newest} {shlex.quote(str(self.path))}"
            where = (f"Backups sit beside it as {shlex.quote(str(self.path.parent / (self.path.name + _fg.STAMPED_BACKUP_INFIX)))}"
                     f"<UTC stamp>; the newest is {newest}.")
        else:
            cmd = (f"cp -p <newest {shlex.quote(self.path.name + _fg.STAMPED_BACKUP_INFIX)}* backup> "
                   f"{shlex.quote(str(self.path))}")
            where = (f"No backup was found beside it (they are named "
                     f"{shlex.quote(self.path.name + _fg.STAMPED_BACKUP_INFIX)}<UTC stamp>); "
                     "check the data dir's git history.")
        super().__init__(
            f"{self.path} does not exist; refusing to create it from empty (the turn counter would "
            f"restart). {where} To recover, copy the newest backup over the path: {cmd}"
        )


def _backup_regex(path: Path) -> "re.Pattern":
    return re.compile(
        re.escape(path.name + _fg.STAMPED_BACKUP_INFIX) + r"(\d{8}T\d{6}Z)(?:-(\d+))?\Z"
    )


def backup_paths(path) -> list:
    """This path's stamped backups, oldest first (parsed stamp, then numeric
    collision suffix; never name or mtime order).  Best effort: a listing
    failure gives [] and one stderr line."""
    path = Path(path)
    rx = _backup_regex(path)
    found = []
    try:
        with os.scandir(path.parent) as it:
            for entry in it:
                m = rx.match(entry.name)
                if m:
                    found.append(((m.group(1), int(m.group(2) or 0)), path.parent / entry.name))
    except OSError as exc:
        print(f"engram config: could not list backups beside {path}: {type(exc).__name__}", file=sys.stderr)
        return []
    return [p for _, p in sorted(found)]


def _next_backup(path: Path) -> Path:
    """The name for the next backup.  engram_file_guard's ``_next_backup_name``
    takes the FIRST free ``-n`` of the stamp, so after a prune the lowest
    numbers are free again and a NEW backup would reuse one and sort as the
    oldest, which the next prune would delete.  When this stamp already has
    backups, take one past the highest suffix instead (never reusing a number
    while a higher one exists)."""
    stamp = _fg.utc_stamp()
    taken = [n for (st, n), _ in _parsed_backups(path) if st == stamp]
    if not taken:
        return _fg._next_backup_name(path, stamp)
    n = max(taken) + 1
    while _fg.target_present(_fg._stamped(path, stamp, n)):
        n += 1
    return _fg._stamped(path, stamp, n)


def _parsed_backups(path: Path) -> list:
    """[((stamp, n), path)] for this path's backups; a listing failure gives [] (one stderr line)."""
    rx = _backup_regex(path)
    found = []
    try:
        with os.scandir(path.parent) as it:
            for entry in it:
                m = rx.match(entry.name)
                if m:
                    found.append(((m.group(1), int(m.group(2) or 0)), path.parent / entry.name))
    except OSError as exc:
        print(f"engram config: could not list backups beside {path}: {type(exc).__name__}", file=sys.stderr)
        return []
    return found


def _prune_backups(path: Path) -> None:
    """Keep the newest BACKUP_KEEP backups.  Loud, never fatal: the write has
    already happened.  Only regular files and symlinks are unlinked.  This
    removes the files from the directory only; the data dir is a git repo and
    history may still hold a copy."""
    rx = _backup_regex(path)
    found = []
    try:
        with os.scandir(path.parent) as it:
            for entry in it:
                m = rx.match(entry.name)
                if not m:
                    continue
                cand = path.parent / entry.name
                try:
                    st = os.lstat(cand)
                except OSError:
                    continue
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                    print(f"engram config: backup prune skipped {cand}: not a regular file", file=sys.stderr)
                    continue
                found.append(((m.group(1), int(m.group(2) or 0)), cand))
    except OSError as exc:
        print(f"engram config: backup prune could not list {path.parent}: {type(exc).__name__}", file=sys.stderr)
        return
    found.sort()
    for _, cand in (found[:-BACKUP_KEEP] if len(found) > BACKUP_KEEP else []):
        try:
            os.unlink(cand)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"engram config: backup prune could not remove {cand}: {type(exc).__name__}", file=sys.stderr)


def atomic_write_text(path, text, *, encoding="utf-8", follow_symlinks: bool = True) -> None:
    """Atomically replace ``path`` with ``text`` (#4013).  The one shared
    mode- and group-preserving writer: an existing file keeps its permission
    bits and, where permitted, its group; a new file gets what ``write_text``
    would give it (umask or the directory's default ACL).  By default a symlink
    is followed (the link stays, its target changes).  With
    ``follow_symlinks=False`` the path itself is replaced: a symlink (live or
    dangling) is swapped for a regular file, what it pointed at is never
    touched or created, and mode and group are copied only from an existing
    REGULAR file, never from a link's target.  Pass ``follow_symlinks=False`` for any path under a
    directory other users can write (a planted link would otherwise redirect the write); the default
    True exists for paths the caller owns and links on purpose, such as config.json via
    ``write_config_json``.  Needs write access to the containing directory (same-UID
    callers).  On any error the temp file is removed and the error re-raised;
    ``path`` is never left partial.  Kept AST-identical with
    ``hooks/claude/_hooklib.atomic_write_text`` (tests/test_atomic_writer_modes_4013.py)."""
    target = os.path.realpath(path) if follow_symlinks else os.path.abspath(path)
    try:
        st = os.stat(target) if follow_symlinks else os.lstat(target)
    except OSError:
        st = None
    copy_mode = st is not None and (follow_symlinks or stat.S_ISREG(st.st_mode))
    # An existing file's staging copy starts private (0600) and gets the old mode
    # only after the payload is written; a new file or a replaced link keeps the
    # umask / default-ACL policy of a plain open().
    create_mode = 0o600 if copy_mode else 0o666
    for _ in range(8):
        tmp = f"{target}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create_mode)
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
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
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


def write_engram_config(path, data, *, allow_key_loss: bool = False):
    """The guarded writer for ``config.json`` itself (#1989).  Returns the new
    backup's path, or None (new file, or bytes unchanged).

    A bug-guard, not a security boundary: it stops ENGRAM's own writer from
    shrinking the file, and stops nobody who can write the file.  It does not
    catch a valid-but-wrong config (right keys, wrong values).

    Order: shape check, shrink check, backup, write.  A refused write leaves no
    backup.  Raises ``ConfigShapeError`` for a non-dict, ``ConfigShrinkError``
    when a top-level key of the existing file is missing from ``data``, and lets
    ``ConfigUnreadableError`` through (an unreadable file is never overwritten).
    ``allow_key_loss=True`` is for a deliberate migration only; nothing in the
    tree passes it, and it logs the lost key names to stderr.

    A changed file is first copied to ``<name>.engram-backup.<UTC stamp>[-n]``
    (engram_file_guard's helpers); if that copy raises, nothing is written.  The
    backup keeps the source's mode and ACL, deliberately: a less-permissive copy
    is a recovery trap for a group-sharing seat.  (The ACL could be stripped and
    was deliberately not.)  Costs: retention is a confidentiality bound (anything
    ever in config.json stays readable in up to 7 copies; rotating a value out of
    the live file does not reach them; config.json holds identity config, not
    secrets), and revoking a named-user ACL grant on config.json does not reach
    backups already written.  The newest BACKUP_KEEP are kept; the prune cannot
    fail the call.
    """
    path = Path(path)
    if not isinstance(data, dict):
        raise ConfigShapeError(
            f"refusing to write {path}: config must be a JSON object, got {type(data).__name__}"
        )
    exists = os.path.lexists(path) and path.exists()
    current = read_config_json(path) if exists else None
    if isinstance(current, dict):
        lost = sorted(k for k in current if k not in data)
        if lost:
            if not allow_key_loss:
                raise ConfigShrinkError(path, lost)
            print(
                f"engram config: allow_key_loss: {path} loses {len(lost)} key(s): "
                + ", ".join(lost[:SHRINK_NAMES_SHOWN]),
                file=sys.stderr,
            )
    new_text = json.dumps(data, indent=2) + "\n"
    backup = None
    if exists:
        try:
            if read_regular_text(path, encoding="utf-8") == new_text:
                return None
        except OSError:
            pass
        backup = _next_backup(path)
        _fg.copy_exclusive(path, backup)  # raises -> nothing written
    write_config_json(path, data)
    if backup is not None:
        _prune_backups(path)
    return backup
