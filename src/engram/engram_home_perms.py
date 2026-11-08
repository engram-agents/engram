"""Owner-only permissions for the ENGRAM data home (engram-alpha#4445).

One function, `tighten_data_home(home)`, stdlib only, never raises into a caller.

The data home holds the knowledge graph, the diary, session and compaction records, the
session-transcript archives, database copies, evidence and logs:
private to the owner. A default umask (022) leaves them world-readable, so this makes
them owner-only, and runs again every session to catch children created loose later.

Owner-only; named ACL grants such as the operator's are preserved and inherited: on a path
carrying a named-user ACL (the cross-user viz operator account) the named entries and the mask
stay, and a directory's DEFAULT ACL entry stays too, so files created later under an inherited
operator grant remain operator-readable. That reaches memory text (the overflow files, logs,
transcripts), not only the viz health snapshots. Only the owning-group and other entries are
cleared. Details under "Owner-only EXCEPT" below.

The root `home` is resolved once with realpath (ENGRAM_HOME may be a symlink to a bigger
disk); a root that does not resolve to a directory is an `errors` entry. `home` must be an
absolute str or os.PathLike returning an absolute str: anything else (None, an int, "",
Path(""), ".", a relative path, bytes or a PathLike returning bytes) is ONE `errors` entry and
nothing is changed (a relative path would resolve against the current directory; bytes is
refused rather than decoded, the smaller and safer change). Below the root:

Rules, per path, never following a symlink (a symlink is skipped, its target never touched):
  * Directories never widen: they lose group and other bits like files do (a 0500 directory stays
    0500; a 0755 one becomes 0700). A regular file with more than one hard link is skipped
    (listed in `errors` as "hard-linked, skipped"), so nothing outside the tree changes mode.
  * `home` itself and the data directories in `DATA_DIRS` (`diary`, `sessions`, `compact`,
    `history`, `health-snapshots`, `session-logs-archive`, `claude-projects-archive`,
    `db-backup`, `evidence-archive`, `logs`, `telemetry`, `session-start-overflow`,
    `tripwire-overflow`; recursively): directories and regular files lose group and
    other bits (`mode & ~0o077`: 0644 becomes 0600, an owner-executable 0755 becomes 0700).
  * Regular files directly inside `home` (not recursive): the same file rule. This
    covers knowledge.db and its -wal/-shm/-journal, config.json, the ledgers and logs.
  * Never descended into or changed: `.git`, `marketplace`, `venv`, `tools`.
  * A path not owned by the current uid is skipped and listed in `errors`.
  * Owner-only EXCEPT on paths carrying a named-user ACL (Linux). There the group-class bits
    of the mode ARE the ACL mask, so `chmod 0700` would set `mask::---` and silently disable
    every named-user entry (the viz operator account reads this home through such entries);
    and clearing only the other bits would leave the owning group at the mask. So the ACL
    xattr is rewritten in place (`os.setxattr`, never setfacl, which recalculates the mask
    and can widen): the owning-group and other entries are cleared, the named entries and the
    mask are preserved, and the path is NOT chmod'ed. A directory's DEFAULT ACL gets the same
    rewrite (chmod never touches it, so files created later would otherwise inherit a group
    entry). Such paths are reported in `acl_preserved` as `rel (access)`, `rel (default)` or
    `rel (both)`. An ACL blob that does not parse is left alone and reported in `errors`.
    A NAMED default entry (the viz operator's grant) is deliberately NOT removed: files created
    later under it stay operator-readable by design, so "owner-only" covers what exists, not
    what an inherited operator grant will create (measured on Linux by the security seat).
  * Idempotent: a path is stat'ed first and chmod is called only when the mode differs.
"""

from __future__ import annotations

import errno
import os
import stat
import struct
from dataclasses import dataclass, field

# Every directory shipped code creates under the home that holds private data (session
# transcripts, database copies, evidence, logs). `config-backup` is not here: no shipped code
# creates it. Code and git objects (.git, marketplace, venv, tools) are NEVER_TOUCH.
DATA_DIRS = (
    "diary", "sessions", "compact", "history", "health-snapshots",
    "session-logs-archive", "claude-projects-archive", "db-backup", "evidence-archive",
    "logs", "telemetry", "session-start-overflow", "tripwire-overflow",
)
NEVER_TOUCH = (".git", "marketplace", "venv", "tools")
_ACL_ACCESS = "system.posix_acl_access"
_ACL_DEFAULT = "system.posix_acl_default"
_ACL_VERSION = 2
_USER_OBJ, _USER, _GROUP_OBJ, _GROUP, _MASK, _OTHER = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20
_SINGLETON_TAGS = (_USER_OBJ, _GROUP_OBJ, _MASK, _OTHER)


@dataclass
class TightenResult:
    changed: list[str] = field(default_factory=list)
    acl_preserved: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _parse_acl(blob) -> "list[tuple[int, int, int]] | None":
    """Entries `(tag, perm, id)` of a Linux POSIX ACL xattr, or None if it does not parse:
    little-endian u32 version 2, then 8-byte entries `<u16 tag, u16 perm, u32 id>`; length
    4 + 8n; one each of USER_OBJ, GROUP_OBJ, OTHER (and at most one MASK); named USER/GROUP
    entries unique per id; no unknown tag."""
    try:
        if len(blob) < 4 or (len(blob) - 4) % 8:
            return None
        if struct.unpack_from("<I", blob, 0)[0] != _ACL_VERSION:
            return None
        entries = [struct.unpack_from("<HHI", blob, 4 + 8 * i) for i in range((len(blob) - 4) // 8)]
    except Exception:
        return None
    seen = set()
    for tag, _perm, eid in entries:
        if tag not in (_USER_OBJ, _USER, _GROUP_OBJ, _GROUP, _MASK, _OTHER):
            return None
        key = (tag, eid if tag in (_USER, _GROUP) else None)
        if key in seen:
            return None
        seen.add(key)
    tags = {t for t, _p, _i in entries}
    if _GROUP_OBJ not in tags or _OTHER not in tags or _USER_OBJ not in tags:
        return None
    return entries


def _tighten_acl_blob(blob: bytes) -> "bytes | None":
    """A copy of the ACL xattr `blob` with ONLY the GROUP_OBJ and OTHER perms set to 0 and
    every other byte identical (USER_OBJ, named entries and the MASK untouched, order kept);
    None if it is already tight or does not parse (the caller tells them apart with
    _parse_acl). Pure: nothing is written and setfacl is never involved."""
    entries = _parse_acl(blob)
    if entries is None:
        return None
    out = bytearray(blob)
    for i, (tag, perm, _eid) in enumerate(entries):
        if tag in (_GROUP_OBJ, _OTHER) and perm != 0:
            out[4 + 8 * i + 2:4 + 8 * i + 4] = b"\x00\x00"
    return bytes(out) if bytes(out) != bytes(blob) else None


_NO_ACL_ERRNOS = frozenset(
    e for e in (getattr(errno, n, None) for n in ("ENODATA", "ENOTSUP", "EOPNOTSUPP", "ENOSYS")) if e is not None
)


def _read_acl(path: str, name: str):
    """The raw ACL xattr, or None when there is none: ENODATA, ENOTSUP, EOPNOTSUPP or ENOSYS (a
    filesystem without xattr support cannot carry an ACL; ERANGE is
    retried inside os.getxattr), or the platform has no getxattr (macOS: AttributeError, not
    OSError). Any OTHER OSError (EIO, ENOMEM, ESTALE, EACCES...) is raised: a path whose ACL
    could not be read must not be chmod'ed (that can set mask::--- and disable named entries)."""
    if not hasattr(os, "getxattr"):
        return None
    try:
        return os.getxattr(path, name, follow_symlinks=False)
    except OSError as e:
        if e.errno in _NO_ACL_ERRNOS:
            return None
        raise


def _tighten_acl_xattr(path: str, rel: str, name: str, blob: bytes, res: TightenResult) -> bool:
    """Rewrite one ACL xattr. True when it is now tight (rewritten or already so); False, with
    an `errors` entry, when it did not parse or the rewrite failed."""
    new = _tighten_acl_blob(blob)
    if new is None:
        if _parse_acl(blob) is None:
            res.errors.append(f"{rel}: {name} does not parse, left alone")
            return False
        return True
    try:
        os.setxattr(path, name, new, follow_symlinks=False)
    except OSError as e:
        res.errors.append(f"{rel}: {name} not rewritten ({e.strerror or e})")
        return False
    if rel not in res.changed:
        res.changed.append(rel)
    return True


def _chmod(path: str, mode: int) -> None:
    # The caller has already lstat'ed `path` as a non-symlink; where the platform lets
    # chmod refuse to follow, say so as well.
    if os.chmod in os.supports_follow_symlinks:
        os.chmod(path, mode, follow_symlinks=False)
    else:
        os.chmod(path, mode)


def _tighten_one(path: str, rel: str, want_dir: bool, res: TightenResult) -> bool:
    """Tighten one path. Returns True when `path` is a real directory we may list."""
    try:
        st = os.lstat(path)
    except OSError as e:
        res.errors.append(f"{rel}: {e.strerror or e}")
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    is_dir = stat.S_ISDIR(st.st_mode)
    if is_dir != want_dir or not (is_dir or stat.S_ISREG(st.st_mode)):
        return False
    if st.st_uid != os.getuid():
        res.errors.append(f"{rel}: not owned by the current user, skipped")
        return False
    if not is_dir and st.st_nlink > 1:
        # Another name for this inode may live outside the tree; chmod would change it too.
        res.errors.append(f"{rel}: hard-linked, skipped")
        return False
    cur = stat.S_IMODE(st.st_mode)
    try:
        access = _read_acl(path, _ACL_ACCESS)
        default = _read_acl(path, _ACL_DEFAULT) if is_dir else None
    except OSError as e:
        res.errors.append(f"{rel}: could not read its ACL ({e.strerror or e}), left alone")
        return is_dir
    if access is not None or default is not None:
        done = []
        for kind, name, blob in (("access", _ACL_ACCESS, access), ("default", _ACL_DEFAULT, default)):
            if blob is not None and _tighten_acl_xattr(path, rel, name, blob, res):
                done.append(kind)
        if done:
            res.acl_preserved.append(f"{rel} ({'both' if len(done) == 2 else done[0]})")
    if access is not None:
        return is_dir  # never chmod: the group bits of the mode ARE the mask
    new = cur & ~0o077  # never widen: a 0500 directory stays 0500
    if new != cur:
        try:
            _chmod(path, new)
            if rel not in res.changed:
                res.changed.append(rel)
        except OSError as e:
            res.errors.append(f"{rel}: {e.strerror or e}")
            return False
    return is_dir


def _walk(path: str, rel: str, res: TightenResult) -> None:
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError as e:
        res.errors.append(f"{rel}: {e.strerror or e}")
        return
    for entry in entries:
        child_rel = f"{rel}/{entry.name}"
        if entry.is_symlink():
            continue
        if entry.is_dir(follow_symlinks=False):
            if _tighten_one(entry.path, child_rel, True, res):
                _walk(entry.path, child_rel, res)
        else:
            _tighten_one(entry.path, child_rel, False, res)


def tighten_data_home(home) -> TightenResult:
    """Make the ENGRAM data home owner-only (rules in the module docstring)."""
    res = TightenResult()
    try:
        # The ROOT is resolved once: ENGRAM_HOME may legitimately be a symlink (a bigger disk).
        # Symlinks INSIDE the tree are still never followed.
        # Not an ABSOLUTE str path (None, an int, "", Path(""), ".", a relative path, bytes) is an
        # error entry: realpath of a relative path resolves against the CURRENT DIRECTORY, which
        # must never be tightened by accident. bytes (also a PathLike returning bytes) is refused
        # rather than fsdecode'd: the smaller, safer change; it used to be half-handled (root
        # tightened, the str-only walk below left the children loose).
        raw = os.fspath(home) if isinstance(home, (str, bytes, os.PathLike)) else None
        if not isinstance(raw, str) or not os.path.isabs(raw):
            res.errors.append(f".: {home!r} is not a usable path, nothing was changed")
            return res
        root = os.path.realpath(raw)
        try:
            root_mode = os.lstat(root).st_mode
        except OSError as e:
            res.errors.append(f".: {os.fspath(home)} does not resolve to anything ({e.strerror or e})")
            return res
        if not stat.S_ISDIR(root_mode):
            res.errors.append(f".: {os.fspath(home)} resolves to something that is not a directory ({root})")
            return res
        if not _tighten_one(root, ".", True, res):
            return res
        for entry in sorted(os.scandir(root), key=lambda e: e.name):
            if entry.name in NEVER_TOUCH or entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name in DATA_DIRS and _tighten_one(entry.path, entry.name, True, res):
                    _walk(entry.path, entry.name, res)
            else:
                _tighten_one(entry.path, entry.name, False, res)
    except Exception as e:  # never raise into a caller
        res.errors.append(f"unexpected: {type(e).__name__}: {e}")
    return res
