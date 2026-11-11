"""The one place that decides, backs up and writes a protected file.

Guarantee: setup never silently overwrites or deletes a file the user or their agent wrote.
A file that already exists and differs from what setup would write is backed up first and
replaced only on an explicit consent that the user gave (`--replace PATH`, or `--force`).

Shape:
  plan(items)           check-only: classify every path, pick its backup name. Writes nothing.
  refusal(plan, consent) ONE message naming every path that still needs consent, or None.
  apply(plan, consent)  re-check, then write: absent -> write; identical -> nothing;
                        differs and consented -> backup first, then atomic write.

States of a path: absent, identical (already holds the new text), differs, dangling (a symlink
to nothing), blocked (present but not a regular file, so it cannot be saved or replaced).
Only `differs` can be consented to. Stdlib only.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, NamedTuple

STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
FIRST_BACKUP_SUFFIX = ".pre-engram"
STAMPED_BACKUP_INFIX = ".engram-backup."


class Item(NamedTuple):
    """One path setup wants to write. `new_text=None` means setup wants it removed.

    `first_identity=True` marks an identity file (CLAUDE.md, AGENTS.md, config.toml). Every
    replace of it is saved to a stamped backup; in addition, while `<name>.pre-engram` is
    absent, the same bytes are kept there too, as the first-ever copy (the name the installer
    and the uninstaller use). An existing `<name>.pre-engram` is never modified.
    """
    path: Path
    new_text: "str | None"
    first_identity: bool = False


class Entry(NamedTuple):
    path: Path
    new_text: "str | None"
    state: str           # absent | identical | differs | dangling | blocked
    backup: "Path | None"  # the stamped backup the current bytes would be saved to (differs only)
    first_backup: "Path | None" = None  # `<name>.pre-engram`, when it would also be written
    first_identity: bool = False


class Plan(NamedTuple):
    entries: "tuple[Entry, ...]"
    stamp: str


class Consent(NamedTuple):
    """Yes to every listed path (`all_paths`), or to exactly the paths in `paths`."""
    all_paths: bool = False
    paths: "frozenset[str]" = frozenset()

    def allows(self, path: Path) -> bool:
        return self.all_paths or _key(path) in self.paths


NO_CONSENT = Consent()


class Refused(Exception):
    """Raised by apply() when it finds a path that needs consent it does not have."""


def _key(path: Path) -> str:
    return os.path.abspath(os.path.expanduser(str(path)))


def consent_for(all_paths: bool, replace: Iterable[str]) -> Consent:
    return Consent(all_paths, frozenset(_key(Path(p)) for p in replace))


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime(STAMP_FORMAT)


def target_present(path: Path) -> bool:
    """True if anything sits at `path`, a dangling symlink included (lexists, not exists)."""
    return os.path.lexists(path)


def is_dangling_symlink(path: Path) -> bool:
    return target_present(path) and path.is_symlink() and not path.exists()


def _pre_engram(path: Path) -> Path:
    return path.parent / (path.name + FIRST_BACKUP_SUFFIX)


def _stamped(path: Path, stamp: str, n: int) -> Path:
    suffix = "" if n == 0 else f"-{n}"
    return path.parent / f"{path.name}{STAMPED_BACKUP_INFIX}{stamp}{suffix}"


def _next_backup_name(path: Path, stamp: str) -> Path:
    """The first stamped backup name that is free right now (nothing, not even a dangling link, there)."""
    n = 0
    while target_present(_stamped(path, stamp, n)):
        n += 1
    return _stamped(path, stamp, n)


def _first_backup_name(path: Path, first_identity: bool) -> "Path | None":
    """`<name>.pre-engram` when this is an identity file and that name is free, else None."""
    if first_identity and not target_present(_pre_engram(path)):
        return _pre_engram(path)
    return None


def classify(path: Path, new_text: "str | None") -> str:
    if not target_present(path):
        return "absent"
    if is_dangling_symlink(path):
        return "dangling"
    if not path.is_file():
        return "blocked"
    if new_text is not None:
        try:
            if path.read_text() == new_text:
                return "identical"
        except (OSError, UnicodeDecodeError):
            pass
    return "differs"


def plan(items: Iterable[Item], stamp: "str | None" = None) -> Plan:
    """Check-only pass. Writes nothing and raises nothing."""
    stamp = stamp or utc_stamp()
    entries = []
    for it in items:
        state = classify(it.path, it.new_text)
        backup = _next_backup_name(it.path, stamp) if state == "differs" else None
        first = _first_backup_name(it.path, it.first_identity) if state == "differs" else None
        entries.append(Entry(it.path, it.new_text, state, backup, first, it.first_identity))
    return Plan(tuple(entries), stamp)


def refusal(p: Plan, consent: Consent = NO_CONSENT, context: str = "") -> "str | None":
    """ONE message naming every path that cannot proceed yet, or None when all can."""
    need = [e for e in p.entries if e.state == "differs" and not consent.allows(e.path)]
    stuck = [e for e in p.entries if e.state in ("dangling", "blocked")]
    if not need and not stuck:
        return None
    lines = ["[bootstrap] REFUSED: no existing file was changed."]
    if context:
        lines.append(context)
    if need:
        lines.append(
            "These existing files differ from what ENGRAM would write. Each is saved to the "
            "backup shown before it is replaced:"
        )
        for e in need:
            also = f" and {e.first_backup}" if e.first_backup is not None else ""
            lines.append(f"  - {e.path}   (backup: {e.backup}{also})")
        lines.append(
            "Show the user this list and get their yes first. Then re-run with "
            "--replace PATH (repeat it for each path they agreed to) or --force (every path "
            "listed). Or leave a file as it is: move it aside yourself, then re-run."
        )
    for e in stuck:
        if e.state == "dangling":
            lines.append(
                f"  - {e.path} is a symlink to a path that does not exist ({os.readlink(e.path)}). "
                f"Move the link aside (mv '{e.path}' '{e.path}.moved-aside') or repair it so it "
                f"points at a real file, then re-run."
            )
        else:
            lines.append(
                f"  - {e.path} is not a regular file, so it cannot be saved or replaced. "
                f"Move it aside, then re-run."
            )
    return "\n".join(lines)


# --- exclusive copy (moved here from bootstrap.py; one implementation) -------------------

# os.link errnos meaning "this filesystem has no hard links" (exFAT/FAT, some CIFS/SMB).
_NO_HARDLINK_ERRNOS = frozenset(
    e for e in (
        errno.EPERM, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", None),
        errno.EXDEV, errno.ENOSYS,
    ) if e is not None
)


def copy_exclusive(src: Path, dst: Path) -> None:
    """Copy bytes then metadata from `src` to `dst`, publishing it all at once; raise
    FileExistsError if anything is at `dst`.

    Property: after this returns or raises, `dst` either does not exist or holds a COMPLETE
    copy of `src` (bytes and metadata). It never removes, truncates or writes through an
    entry it did not create in this call. The copy is built in a UNIQUE sibling temp
    `<dst>.partial-<random>` made by tempfile.mkstemp (O_CREAT|O_EXCL, never reuses an
    existing name, so whatever already sits at any `.partial-` name is left alone); the
    temp's bytes are fsync'd, then it is published with os.link(temp, dst): atomic, raises
    FileExistsError for anything at `dst` (a symlink included), never overwrites or
    truncates. The temp this call created is always unlinked afterwards. No directory
    fsync is done, so no durability claim is made. shutil.copy2/copyfile onto `dst` would
    open it for writing and follow links.

    Fallback, honestly: on a filesystem without hard links (os.link fails with EPERM,
    EOPNOTSUPP, ENOTSUP, EXDEV or ENOSYS) the finished temp is published with
    os.replace(temp, dst). That is still atomic, so no partial file can appear, but it
    LOSES EXCLUSIVITY: os.replace overwrites whatever is at `dst`. The caller has already
    established that `dst` is absent, so the residual risk is the check-then-publish race,
    on those filesystems only. Any other OSError from os.link propagates with nothing at `dst`.
    """
    temp = None  # the exact path mkstemp returned, stored once; None until it has created it
    try:
        # dir=dst.parent is LOAD-BEARING: os.link is same-filesystem only, so a temp under
        # $TMPDIR would make every publish hit the EXDEV os.replace fallback below and
        # silently lose exclusivity. A killed run can leave a uniquely named
        # `<name>.partial-*` file; it is inert (never read or published) and safe to move
        # aside by hand.
        fd, temp = tempfile.mkstemp(dir=dst.parent, prefix=dst.name + ".partial-")
        # fdopen first, so the fd mkstemp returned is closed even if opening src fails.
        with os.fdopen(fd, "wb") as temp_f, open(src, "rb") as src_f:
            shutil.copyfileobj(src_f, temp_f)
            temp_f.flush()
            os.fsync(temp_f.fileno())
        shutil.copystat(src, temp)
        try:
            os.link(temp, dst)
        except FileExistsError:
            raise
        except OSError as e:
            if e.errno not in _NO_HARDLINK_ERRNOS:
                raise
            os.replace(temp, dst)
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass


def atomic_write_text(path: Path, text: str, mode: int) -> None:
    """Write `text` to `path` via a temp file in the same directory: fsync, then os.replace.

    The temp is created exclusively with `mode` and removed on any failure, so a crash
    leaves either the old file or the new one, never a partial file.
    """
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_path_problem(path: Path, backup: Path) -> "str | None":
    """The REFUSED message if `path` cannot be saved to the FIXED name `backup`, else None. Pure.

    Used for the one fixed-name backup that remains (settings.json.corrupt.<UTC>). A dangling
    symlink at `backup` is neither "a backup exists" nor "absent" (a plain copy would write
    through the link). A backup that is the same file as `path` would be lost with `path`.
    Anything else that is not a regular file cannot hold a copy.
    """
    if is_dangling_symlink(backup):
        return (
            f"[bootstrap] REFUSED: the backup path {backup} is a symlink to a path that "
            f"does not exist ({os.readlink(backup)}), so {path} cannot be saved there. "
            f"Move the link aside or repair it, then re-run."
        )
    if target_present(backup) and os.path.samefile(path, backup):
        return (
            f"[bootstrap] REFUSED: the backup path {backup} is the same file as {path} "
            f"(a hard link or link to it), so it would be overwritten too. Move "
            f"'{backup}' away, then re-run."
        )
    if target_present(backup) and not backup.is_file():
        return (
            f"[bootstrap] REFUSED: the backup path {backup} is not a regular file, so "
            f"{path} cannot be saved there. Move it away, then re-run."
        )
    return None


def backup_once(path: Path, backup: Path) -> None:
    """Copy `path` to the fixed name `backup` with metadata, once; an existing backup is never touched.

    Refuses (writing nothing) on any `backup_path_problem`. The copy is an exclusive create.
    """
    problem = backup_path_problem(path, backup)
    if problem is not None:
        raise SystemExit(problem)
    if target_present(backup):
        print(f"  backup: existing {backup} kept untouched", flush=True)
        return
    try:
        copy_exclusive(path, backup)
    except FileExistsError:
        raise SystemExit(
            f"[bootstrap] REFUSED: something appeared at the backup path {backup} while "
            f"{path} was being saved, so nothing was written there. Move it away, then re-run."
        )
    except OSError as e:
        raise SystemExit(
            f"[bootstrap] REFUSED: could not save {path} to the backup path {backup} "
            f"({e}), so nothing was written there. Fix the cause, then re-run."
        )
    print(f"  backup: {path} -> {backup}", flush=True)


def _copy_to_free_name(path: Path, pick) -> Path:
    """Copy `path` (exclusive create) to the first free name `pick()` offers. Returns that name.

    Raises SystemExit with a one-line REFUSED message if the copy cannot be written, so the
    caller never goes on to replace a file it could not save.
    """
    for _attempt in range(1000):
        backup = pick()
        try:
            copy_exclusive(path, backup)
        except FileExistsError:
            continue  # something appeared at that name meanwhile: take the next free one
        except OSError as e:
            raise SystemExit(
                f"[bootstrap] REFUSED: could not save {path} to the backup path {backup} "
                f"({e}), so nothing was written there. Fix the cause, then re-run."
            )
        print(f"  backup: {path} -> {backup}", flush=True)
        return backup
    raise SystemExit(f"[bootstrap] REFUSED: no free backup name for {path}; nothing was written there.")


def save_backup(path: Path, first_identity: bool, stamp: str) -> Path:
    """Save the current bytes of `path`: a stamped backup always, plus `<name>.pre-engram` for
    an identity file while that name is absent. Returns the stamped backup's name."""
    first = _first_backup_name(path, first_identity)
    if first is not None:
        try:
            copy_exclusive(path, first)
            print(f"  backup: {path} -> {first}", flush=True)
        except FileExistsError:
            pass  # taken meanwhile: the existing one stays untouched; the stamped copy still follows
        except OSError as e:
            raise SystemExit(
                f"[bootstrap] REFUSED: could not save {path} to the backup path {first} "
                f"({e}), so nothing was written there. Fix the cause, then re-run."
            )
    return _copy_to_free_name(path, lambda: _next_backup_name(path, stamp))


def _write(path: Path, text: str) -> None:
    """Write `text` over/into `path`, keeping a symlink in place and an existing file's mode."""
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target_present(target):
        mode = os.stat(target).st_mode & 0o7777
    else:
        old = os.umask(0)
        os.umask(old)
        mode = 0o666 & ~old
    atomic_write_text(target, text, mode)


def save_backups(p: Plan, consent: Consent = NO_CONSENT) -> "dict[str, Path]":
    """Back up every consented, differing path NOW (before anything else is written).

    Run this right after the check-only pass and before seeding: a backup that cannot be
    written then refuses the whole run while nothing else has changed. Returns
    {path key: backup path} for apply(), which will not copy the same bytes twice.
    """
    saved: dict[str, Path] = {}
    for e in p.entries:
        if e.state == "differs" and consent.allows(e.path):
            saved[_key(e.path)] = save_backup(e.path, e.first_identity, p.stamp)
    return saved


def _same_bytes(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def apply(p: Plan, consent: Consent = NO_CONSENT, context: str = "",
          saved: "dict[str, Path] | None" = None) -> "list[Path]":
    """Re-check every path, refuse if any needs consent it lacks, then write. Returns paths written.

    The state is read again here (not trusted from `p`), so a file that changed since the
    check-only pass is judged as it is now. Nothing is written before every path is judged.
    A differing path is backed up first unless `saved` (from save_backups) already holds a
    backup with exactly its current bytes. A path whose new_text is None is removed only after
    a backup, and only on consent.
    """
    saved = saved or {}
    fresh = plan([Item(e.path, e.new_text, e.first_identity) for e in p.entries], p.stamp)
    msg = refusal(fresh, consent, context)
    if msg is not None:
        raise Refused(msg)
    written: list[Path] = []
    for e in fresh.entries:
        if e.state == "identical" or (e.state == "absent" and e.new_text is None):
            continue
        if e.state == "differs":
            have = saved.get(_key(e.path))
            if have is None or not target_present(have) or not _same_bytes(e.path, have):
                save_backup(e.path, e.first_identity, fresh.stamp)
        if e.new_text is None:
            os.unlink(e.path)
        else:
            _write(e.path, e.new_text)
        written.append(e.path)
    return written
