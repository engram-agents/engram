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
to nothing), blocked (present but not a regular file, so it cannot be saved or replaced),
obstructed (an identity file whose `<name>.pre-engram` is the same file as the identity file
itself, so replacing it would overwrite that backup too). Only `differs` can be consented to.

What a backup promises: the file's BYTES, its permission MODE and its modification time (the
copy is shutil.copyfileobj + shutil.copystat). It does NOT promise extended attributes or ACLs;
shutil.copystat drops them on macOS. Backups accumulate: each replace adds one, and nothing
here deletes them. Stdlib only.

MERGE class (a JSON file where ENGRAM owns a few keys and the user owns everything else): see
OWNED_KEYS and apply_merge() below.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple

# ENGRAM's own bookkeeping files: a closed list, by file name. Setup rewrites them when they
# differ, with no consent and no backup (they hold nothing the user or their agent wrote; the
# install-time timestamp in first-run-pending differs on every run). A caller cannot add to it:
# Item(owned=True) is honoured only for these names, and only when the file sits directly in the
# ENGRAM home being set up (the `home` given to plan()). The same name in any other folder is a
# file like any other.
ENGRAM_OWNED_NAMES = ("first-run-pending", "seed-manifest.json")

STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
FIRST_BACKUP_SUFFIX = ".pre-engram"
STAMPED_BACKUP_INFIX = ".engram-backup."

# The clock behind every stamped backup name. A module seam so a test can pin it and force two
# backups into the same second (the second then takes the -1 suffix).
CLOCK: "Callable[[], datetime]" = lambda: datetime.now(timezone.utc)  # noqa: E731


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
    owned: bool = False
    # owned=True: ENGRAM's own bookkeeping file (see ENGRAM_OWNED_NAMES). It is rewritten when
    # it differs, with no consent and no backup; it is never something the user or their agent wrote.


class Entry(NamedTuple):
    path: Path
    new_text: "str | None"
    state: str           # absent | identical | differs | owned | dangling | blocked | obstructed
    backup: "Path | None"  # the stamped backup the current bytes would be saved to (differs only)
    first_backup: "Path | None" = None  # `<name>.pre-engram`, when it would also be written
    first_identity: bool = False
    owned: bool = False


class Plan(NamedTuple):
    entries: "tuple[Entry, ...]"
    stamp: str
    home: "Path | None" = None  # the ENGRAM home this plan is for; the only folder where owned=True counts


class Consent(NamedTuple):
    """Yes to every listed path (`all_paths`), or to exactly the paths in `paths`."""
    all_paths: bool = False
    paths: "frozenset[str]" = frozenset()

    def allows(self, path: Path) -> bool:
        """True for `--force`, for a path given in the same canonical spelling, or for a path
        that is the very same file under another spelling of its NAME (`claude.md` for
        `CLAUDE.md` on a case-insensitive filesystem: same folder, name equal ignoring case and
        Unicode form, and os.path.samefile says yes). A hard link or symlink under a different
        name is NOT accepted: consent names the file the user was shown."""
        if self.all_paths:
            return True
        key = _key(path)
        if key in self.paths:
            return True
        parent, name = os.path.split(key)
        norm = _norm_name(name)
        for given in self.paths:
            gparent, gname = os.path.split(given)
            if gparent == parent and gname != name and _norm_name(gname) == norm:
                try:
                    if os.path.samefile(given, key):
                        return True
                except OSError:
                    pass
        return False


def _norm_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


NO_CONSENT = Consent()


class Refused(Exception):
    """Raised by apply() when it finds a path that needs consent it does not have."""


def _key(path: Path) -> str:
    """One canonical spelling of `path`, used to compare consent with the planned path.

    The parent directory is resolved (symlinked folders, /var vs /private/var, relative paths
    against the current directory); the final name is kept as written, so a symlink AT the path
    is not followed into its target for the decision.
    """
    full = os.path.abspath(os.path.expanduser(str(path)))
    parent, name = os.path.split(full)
    return os.path.join(os.path.realpath(parent), name)


def consent_for(all_paths: bool, replace: Iterable[str]) -> Consent:
    return Consent(all_paths, frozenset(_key(Path(p)) for p in replace))


def utc_stamp() -> str:
    return CLOCK().astimezone(timezone.utc).strftime(STAMP_FORMAT)


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
            if path.read_text(encoding="utf-8") == new_text:  # utf-8 like the writes, never the locale's
                return "identical"
        except (OSError, UnicodeDecodeError):
            pass
    return "differs"


def _backup_name_is_the_file_itself(path: Path, first: Path) -> bool:
    """True if `first` (`<name>.pre-engram`) is the same file as `path`: a hard link, or a
    symlink that resolves to it. Replacing `path` would then overwrite that backup too. A
    dangling link or a directory at that name is a different case (never the same file)."""
    if not target_present(first):
        return False
    try:
        return os.path.samefile(path, first)
    except OSError:
        return False


def _is_directly_in(path: Path, home: "Path | None") -> bool:
    """True if `path` sits directly in the folder `home` (both compared in the canonical spelling)."""
    if home is None:
        return False
    return os.path.dirname(_key(path)) == os.path.realpath(os.path.abspath(os.path.expanduser(str(home))))


def plan(items: Iterable[Item], stamp: "str | None" = None, home: "Path | None" = None) -> Plan:
    """Check-only pass. Writes nothing and raises nothing.

    `home` is the ENGRAM home being set up. Item(owned=True) counts only for a path directly in
    it; with no `home`, nothing is treated as owned.
    """
    stamp = stamp or utc_stamp()
    entries = []
    for it in items:
        state = classify(it.path, it.new_text)
        if state == "differs" and it.first_identity and _backup_name_is_the_file_itself(it.path, _pre_engram(it.path)):
            state = "obstructed"
        if state == "differs" and it.owned and it.path.name in ENGRAM_OWNED_NAMES and _is_directly_in(it.path, home):
            state = "owned"
        backup = _next_backup_name(it.path, stamp) if state == "differs" else None
        first = _first_backup_name(it.path, it.first_identity) if state == "differs" else None
        entries.append(Entry(it.path, it.new_text, state, backup, first, it.first_identity, it.owned))
    return Plan(tuple(entries), stamp, home)


def _canon(path: Path) -> Path:
    return Path(_key(path))


def refusal(p: Plan, consent: Consent = NO_CONSENT, context: str = "") -> "str | None":
    """ONE message naming every path that cannot proceed yet, or None when all can.

    Every path is printed in one canonical form (parent directory resolved, name as written),
    the same form `--replace PATH` is compared in.
    """
    need = [e for e in p.entries if e.state == "differs" and not consent.allows(e.path)]
    stuck = [e for e in p.entries if e.state in ("dangling", "blocked", "obstructed")]
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
            where = _canon(e.path)
            also = f" and {where.parent / e.first_backup.name}" if e.first_backup is not None else ""
            lines.append(f"  - {where}   (backup: {where.parent / e.backup.name}{also})")
        lines.append(
            "Backups accumulate: every replace adds one, and nothing deletes them automatically. "
            "Show the user this list and get their yes first. Then re-run with "
            "--replace PATH (repeat it for each path they agreed to) or --force (every path "
            "listed). Or leave a file as it is: move it aside yourself, then re-run."
        )
    for e in stuck:
        where = _canon(e.path)
        if e.state == "dangling":
            lines.append(
                f"  - {where} is a symlink to a path that does not exist ({os.readlink(e.path)}). "
                f"Move the link aside (mv '{where}' '{where}.moved-aside') or repair it so it "
                f"points at a real file, then re-run."
            )
        elif e.state == "obstructed":
            first = where.parent / (where.name + FIRST_BACKUP_SUFFIX)
            lines.append(
                f"  - {where}: its backup name {first} is the same file as {where} (a hard "
                f"link or a link to it), so replacing {where.name} would overwrite that backup "
                f"too. Move that backup name aside (mv '{first}' '{first}.moved-aside'), then re-run."
            )
        else:
            lines.append(
                f"  - {where} is not a regular file, so it cannot be saved or replaced. "
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

    The temp has a UNIQUE name per call (tempfile.mkstemp: O_CREAT|O_EXCL, never reuses an
    existing name, so a leftover from a crashed earlier run is left alone and cannot make this
    call fail). It is set to `mode` and removed on any failure, so a crash leaves either the
    old file or the new one, never a partial file.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.tmp-")
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
    check-only pass is judged as it is now. Order, exactly:
      1. judge every path; any that needs consent it lacks (or is stuck) -> Refused, nothing
         written;
      2. back up EVERY differing path that `saved` (from save_backups) does not already cover
         with exactly its current bytes; if any one backup cannot be written, SystemExit
         (REFUSED) and NOT ONE path has been replaced (backups already made stay; they are
         copies, nothing is lost);
      3. only then write, path by path.
    A path whose new_text is None is removed only after a backup, and only on consent. A path
    in ENGRAM_OWNED_NAMES that differs is rewritten with no consent and no backup.
    """
    saved = saved or {}
    fresh = plan([Item(e.path, e.new_text, e.first_identity, e.owned) for e in p.entries], p.stamp, p.home)
    msg = refusal(fresh, consent, context)
    if msg is not None:
        raise Refused(msg)
    for e in fresh.entries:
        if e.state == "differs":
            have = saved.get(_key(e.path))
            if have is None or not target_present(have) or not _same_bytes(e.path, have):
                save_backup(e.path, e.first_identity, fresh.stamp)
    written: list[Path] = []
    for e in fresh.entries:
        if e.state == "identical" or (e.state == "absent" and e.new_text is None):
            continue
        if e.new_text is None:
            os.unlink(e.path)
        else:
            _write(e.path, e.new_text)
        written.append(e.path)
    return written


# --- MERGE class: ENGRAM owns a few keys of a JSON file, the user owns the rest ---------------
#
# Four conditions, each pinned by tests/test_never_clobber_contract.py:
#   1. The owned keys are ONE closed, named list per file (OWNED_KEYS). A key not on it is never
#      ENGRAM's to change. A dotted name is a nested key: "forum.url" is the key `url` inside the
#      object `forum`.
#   2. A merge keeps every other key, in value and in order.
#   3. A file that does not parse (or is not a JSON object) is NOT a merge candidate: nothing
#      repairs a broken user file; the merge refuses and the file is left untouched.
#   4. A merge that changes the file takes a stamped backup first; a merge that leaves the file
#      byte-identical writes nothing and backs up nothing.

OWNED_KEYS: "dict[str, tuple[str, ...]]" = {
    "settings.json": ("cleanupPeriodDays",),
    "config.json": ("install_tier", "forum.url"),
}


class MergeRefused(Exception):
    """A merge that would change something that is not ENGRAM's, or into a file that cannot be merged."""


def json_indent(text: str) -> "int | str":
    """The indent of the first indented line of `text` (spaces or a tab), else 2."""
    m = re.search(r"^([ \t]+)\S", text, re.MULTILINE)
    if not m:
        return 2
    lead = m.group(1)
    return "\t" if lead.startswith("\t") else len(lead)


def _without_owned(doc: dict, owned: "tuple[str, ...]") -> dict:
    """A copy of `doc` with every owned leaf removed, for comparing the part that is not ENGRAM's.

    The containers of a nested owned key are created if missing on BOTH sides of a comparison,
    so a container the merge had to create does not count as a change.
    """
    out = json.loads(json.dumps(doc))
    for key in owned:
        parts = key.split(".")
        node = out
        for part in parts[:-1]:
            nxt = node.setdefault(part, {})
            if not isinstance(nxt, dict):
                node = None
                break
            node = nxt
        if isinstance(node, dict):
            node.pop(parts[-1], None)
    return out


def _strict_loads(text: str, filename: str) -> Any:
    """json.loads that refuses what a re-serialization would silently change: a duplicate key
    (the parser keeps only the last one) and a non-finite number (`Infinity`, `NaN`, or a number
    like 1e400 that overflows to infinity, which strict JSON readers reject when written back).
    Raises MergeRefused, which is a refusal, never a rewrite."""
    def pairs(items: "list[tuple[str, Any]]") -> dict:
        seen: set = set()
        for k, _v in items:
            if k in seen:
                raise MergeRefused(
                    f"{filename} has the key {k!r} more than once; rewriting it would keep only one "
                    f"of them. Nothing was changed. Remove the duplicate yourself, then re-run."
                )
            seen.add(k)
        return dict(items)

    def number(s: str) -> float:
        value = float(s)
        if value in (float("inf"), float("-inf")):
            raise MergeRefused(
                f"{filename} holds the number {s} which is not a finite value; rewriting it would "
                f"change it. Nothing was changed."
            )
        return value

    def constant(name: str) -> float:
        raise MergeRefused(
            f"{filename} holds {name}, which is not valid JSON; rewriting it would change it. "
            f"Nothing was changed."
        )

    return json.loads(text, object_pairs_hook=pairs, parse_float=number, parse_constant=constant)


def _drop_path(doc: dict, parts: "list[str]") -> None:
    node: Any = doc
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def merge_json_text(filename: str, existing: "str | None", changes: "dict[str, Any]",
                    replace_non_object: bool = False) -> str:
    """Return `existing` (None = absent file) with the owned keys in `changes` set. Pure.

    `replace_non_object=True` is the caller saying the user agreed (their yes) to replace a
    parent value that is not an object (for "forum.url": a `forum` that holds a string or a
    number) with an object. Without it that case is refused. The replaced value survives only
    in the stamped backup that apply_merge takes before it writes.

    Raises MergeRefused if `filename` has no owned keys, if `changes` names a key that is not
    owned, if `existing` does not parse as a JSON object, if it holds a duplicate key or a
    non-finite number (NaN, Infinity, 1e400), or if setting a nested key would mean replacing
    something the user wrote that is not an object. Returns `existing` unchanged, byte for
    byte, when every owned key already holds the requested value. Otherwise the result is
    re-serialized with the file's own indent: every other key keeps its parsed value and its
    order. "Keeps its parsed value" is the bound: NUMBER SPELLINGS MAY BE NORMALISED on a merge
    (1E5 is written back as 100000.0, 1.0 stays 1.0, 10 stays 10); the number is the same, its
    spelling may not be. Comments do not exist in JSON; a file with any is not a JSON object
    and is refused above.
    """
    owned = OWNED_KEYS.get(filename)
    if owned is None:
        raise MergeRefused(f"{filename} has no keys ENGRAM owns, so ENGRAM never merges into it")
    stray = [k for k in changes if k not in owned]
    if stray:
        raise MergeRefused(
            f"{filename}: {', '.join(stray)} is not one of the keys ENGRAM owns "
            f"({', '.join(owned)}), so it will not be changed"
        )
    if existing is None:
        doc: dict = {}
    else:
        try:
            doc = _strict_loads(existing, filename)
        except ValueError as e:
            raise MergeRefused(f"{filename} is not valid JSON ({e}); nothing repairs a broken file") from e
        if not isinstance(doc, dict):
            raise MergeRefused(f"{filename} holds JSON {type(doc).__name__}, not an object; nothing repairs it")
    before = json.loads(json.dumps(doc))
    replaced: "list[list[str]]" = []  # parents the caller agreed to replace; compared apart below
    for key, value in changes.items():
        parts = key.split(".")
        node = doc
        for i, part in enumerate(parts[:-1]):
            if part not in node:
                node[part] = {}
            elif not isinstance(node[part], dict):
                if not replace_non_object:
                    raise MergeRefused(
                        f"{filename}: '{part}' is not an object, so {key} cannot be set without changing it"
                    )
                node[part] = {}
                replaced.append(parts[: i + 1])
            node = node[part]
        node[parts[-1]] = value
    if existing is not None and doc == before:
        return existing
    cmp_before, cmp_after = json.loads(json.dumps(before)), json.loads(json.dumps(doc))
    for parts in replaced:
        _drop_path(cmp_before, parts)
        _drop_path(cmp_after, parts)
    if json.dumps(_without_owned(cmp_before, owned)) != json.dumps(_without_owned(cmp_after, owned)):
        raise MergeRefused(f"{filename}: the merge would change a key that is not ENGRAM's; nothing was written")
    indent = json_indent(existing) if existing else 2
    return json.dumps(doc, indent=indent, ensure_ascii=False) + "\n"


def apply_merge(path: Path, changes: "dict[str, Any]", *, filename: "str | None" = None,
                new_mode: int = 0o600, stamp: "str | None" = None,
                replace_non_object: bool = False) -> str:
    """Merge the owned keys in `changes` into the JSON file at `path`. Returns what happened:
    "created" (file was absent), "merged" (stamped backup taken, then written atomically,
    keeping the file's mode and a symlink in place) or "unchanged" (nothing written, no backup).
    Raises MergeRefused, with the file untouched, for anything merge_json_text refuses and for a
    dangling link, a non-regular file or an unreadable file. `replace_non_object` is passed on
    to merge_json_text; the stamped backup is taken first either way.
    """
    name = filename or path.name
    if is_dangling_symlink(path):
        raise MergeRefused(f"{path} is a symlink to a path that does not exist ({os.readlink(path)})")
    real = Path(os.path.realpath(path)) if path.is_symlink() else path
    existing = None
    if target_present(real):
        if not real.is_file():
            raise MergeRefused(f"{path} is not a regular file, so it cannot be merged into")
        try:
            existing = real.read_bytes().decode("utf-8")
        except OSError as e:
            raise MergeRefused(f"cannot read {path} ({e})") from e
        except UnicodeDecodeError as e:
            raise MergeRefused(f"{path} is not valid UTF-8 text ({e}); nothing repairs a broken file") from e
    new_text = merge_json_text(name, existing, changes, replace_non_object)
    if existing is None:
        real.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(real, new_text, new_mode)
        return "created"
    if new_text == existing:
        return "unchanged"
    save_backup(path, False, stamp or utc_stamp())
    atomic_write_text(real, new_text, stat.S_IMODE(os.stat(real).st_mode))
    return "merged"
