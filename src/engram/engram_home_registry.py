"""Per-seat ENGRAM home registry (#3863 PR 5, ruling R3).

A seat whose ENGRAM home is NOT ``<login home>/.engram`` (the default ``$ENGRAM_HOME``) records where it is,
so a tool that scans login homes (viz discovery, the viz-operator ACL tool)
finds it without a ``.engram*`` name guess (of ``$ENGRAM_HOME``).  Seats on the default home need no
entry: a missing entry means "the default".

Entry: ``<login home>/.engram-home.json`` = ``{"schema": 1, "engram_home": "<abs>"}``.
It is written by the seat's OWN SessionStart hook (which knows its resolved
ENGRAM_HOME) and read by other tools, so it is world-readable (0644).

Trust rule: a reader uses an entry ONLY if the entry file is a regular,
non-symlink, single-link file owned by the uid that owns the login home it sits
in AND by the uid that owns the directory it names.  A different user cannot
plant a pointer at a home they do not own, and (macOS allows hard-linking
another user's file) cannot hard-link someone else's entry into their own login
home: the link count must be 1 and the entry's owner must own the login home.

One entry per login home, last writer wins: a seat shares its login home with no
other seat, and a default-home session removes a stale entry of its own.  Anything else
is IGNORED (the caller falls back to its own glob), never an error.

Stdlib-only core module (the hook, viz and the ACL tool all import it).  Never
raises into a caller: ``write_entry`` returns a status word, the readers
return ``None`` / ``[]``.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile

ENTRY_NAME = ".engram-home.json"
SCHEMA = 1
MAX_ENTRY_BYTES = 4096


def _is_clean_abs(path) -> bool:
    return (
        isinstance(path, str)
        and path != ""
        and os.path.isabs(path)
        and ".." not in path.split(os.sep)
        and "\x00" not in path
    )


def read_entry(login_home, *, stat_fn=os.stat, lstat_fn=os.lstat):
    """The trusted ENGRAM home named by ``login_home``'s entry, or None.

    None covers: no entry, symlink / non-regular / oversize entry, bad JSON,
    wrong schema, relative or ``..`` path, a named path that is not a
    directory, and an entry whose uid differs from the named directory's uid.
    ``stat_fn`` / ``lstat_fn`` are injectable so the uid rule is testable
    without a second user.
    """
    entry = os.path.join(str(login_home), ENTRY_NAME)
    try:
        st = lstat_fn(entry)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_ENTRY_BYTES:
            return None
        fd = os.open(entry, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode) or fst.st_size > MAX_ENTRY_BYTES:
                return None
            if fst.st_nlink != 1 or (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev):
                return None
            data = os.read(fd, MAX_ENTRY_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_ENTRY_BYTES:
            return None
        doc = json.loads(data.decode("utf-8"))
        if not isinstance(doc, dict):
            return None
        schema = doc.get("schema")
        if type(schema) is not int or schema != SCHEMA:
            return None
        home = doc.get("engram_home")
        if not _is_clean_abs(home):
            return None
        hst = stat_fn(home)
        if not stat.S_ISDIR(hst.st_mode):
            return None
        if hst.st_uid != fst.st_uid:
            return None
        if stat_fn(str(login_home)).st_uid != fst.st_uid:
            return None
        return home
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def homes_for(login_home, **kw) -> list:
    """The registered ENGRAM home for ``login_home`` as a 0- or 1-item list."""
    home = read_entry(login_home, **kw)
    return [home] if home else []


def write_entry(engram_home, login_home=None) -> str:
    """Record ``engram_home`` for this seat.  Returns a status word, never raises.

    ``"written"``  entry created / replaced (non-default home).
    ``"removed"``  default home and a stale entry this user owns was removed.
    ``"default"``  default home, nothing to do.
    ``"error"``    the write was refused or failed (hook stays silent).
    """
    try:
        login = str(login_home) if login_home else os.path.expanduser("~")
        entry = os.path.join(login, ENTRY_NAME)
        home = os.path.abspath(str(engram_home))
        default = os.path.join(os.path.abspath(login), ".engram")  # ENGRAM_HOME's default under a login home
        if os.path.realpath(home) == os.path.realpath(default):
            try:
                st = os.lstat(entry)
            except FileNotFoundError:
                return "default"
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
                os.unlink(entry)
                return "removed"
            return "default"
        if not _is_clean_abs(home):
            return "error"
        try:
            st = os.lstat(entry)
            if not stat.S_ISREG(st.st_mode):
                return "error"
        except FileNotFoundError:
            pass
        # mkstemp: a unique name, so a stale temp file from an earlier crash can
        # never lock the writer out; fchmod makes the mode independent of umask.
        fd, tmp = tempfile.mkstemp(prefix=ENTRY_NAME + ".", suffix=".tmp", dir=login)
        try:
            try:
                os.write(fd, json.dumps({"schema": SCHEMA, "engram_home": home}).encode("utf-8"))
                os.fchmod(fd, 0o644)
            finally:
                os.close(fd)
            os.replace(tmp, entry)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return "error"
        return "written"
    except Exception:
        return "error"
