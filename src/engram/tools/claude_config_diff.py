#!/usr/bin/env python3
"""Say what the host CLI's marketplace add changed in the user's own Claude files (#4628).

`claude plugin marketplace add` rewrites files that belong to the user (the known-marketplaces
list and the settings file) and prints nothing about it. The installer takes a snapshot before
the call and runs a report after it, so the user sees every key that changed or vanished, and
keeps a copy of the old file when something would otherwise be lost.

    claude_config_diff.py snapshot --dest DIR FILE [FILE...]
    claude_config_diff.py report --snapshot DIR --backup-root DIR2 FILE [FILE...]

Every path comes in as an argument. This module reads no environment variable and knows no home
directory. A file is named in the output by its base name.

Contract (pinned by tests/test_claude_config_diff_4628.py):

  C1  For a file that is a JSON object both before and after, one line per dotted key path that
      was ADDED, REMOVED or CHANGED. Objects are walked; a list is compared as a whole value.
      Key order and whitespace alone are not a change: a file rewritten with the same content in
      another order prints nothing. Known limits: a key `a.b` and a key `a` holding `b` print the
      same dotted path; `1` and `1.0` count as a change. Key names are printed escaped (control
      characters and non-printables as \\uXXXX), so a name cannot forge a line.
  C2  A string value is never printed (added, removed, changed or unchanged). A changed boolean
      or number prints `old -> new`; any other changed value prints `changed (<type>)`, or
      `changed (<old type> -> <new type>)` when the type itself changed.
  C3  A file absent before and present after prints `created: <name>`; present before and absent
      after prints `removed: <name>`. A file that is not a JSON object before or after is compared
      by sha256 and prints `changed (not parsed as JSON): <name>` only if the bytes differ.
  C4  If any key was REMOVED or CHANGED (or a file was removed, or an unparsed file changed), the
      BEFORE bytes of that file are copied, right after that file's listing, to
      <backup-root>/pre-marketplace-add-<UTC stamp>/<name> (folder 0700, file 0600) and the path
      is printed. If the only differences are ADDED keys, or nothing
      differs, no backup is kept and none is announced. If the backup cannot be written, the line
      `no backup was kept (<exception class>)` follows the listing.
  C5  Neither subcommand ever fails the install: both always exit 0. Any exception prints one line
      `claude-config diff unavailable: <exception class>` (report handles each file on its
      own, so one file's error does not stop the others). A missing snapshot
      folder prints that no snapshot was taken. Output goes to stdout; nothing goes to stderr on
      the normal path. (A SIGKILL leaves the snapshot folder behind; nothing can clean that up.)
  C6  The Claude files are only READ. The helper writes inside the snapshot folder and the backup
      root, nowhere else.

Every stdout line starts with `[install-local-marketplace] `.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

TAG = "[install-local-marketplace] "
_FILES = "files"
_ABSENT = "absent"


def say(msg: str) -> None:
    print(TAG + msg, flush=True)


class _Args(argparse.ArgumentParser):
    def error(self, message):  # argparse would exit 2: the helper never fails the install
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    p = _Args(prog="claude_config_diff.py", add_help=False)
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Args)
    s = sub.add_parser("snapshot", add_help=False)
    s.add_argument("--dest", required=True)
    s.add_argument("files", nargs="+")
    r = sub.add_parser("report", add_help=False)
    r.add_argument("--snapshot", required=True)
    r.add_argument("--backup-root", required=True)
    r.add_argument("files", nargs="+")
    return p


def _jtype(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    return "object"


def _canon(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _parse_object(data: bytes):
    """The JSON object in `data`, or None when it is not valid JSON or not an object."""
    try:
        v = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return v if isinstance(v, dict) else None


def _walk(old: dict, new: dict, prefix: str, out: list) -> None:
    """Append (kind, dotted path, detail) for every added / removed / changed key."""
    for k in old:
        path = prefix + k
        if k not in new:
            out.append(("removed", path, ""))
        elif isinstance(old[k], dict) and isinstance(new[k], dict):
            _walk(old[k], new[k], path + ".", out)
        elif _canon(old[k]) != _canon(new[k]):
            out.append(("changed", path, _describe(old[k], new[k])))
    for k in new:
        if k not in old:
            out.append(("added", prefix + k, ""))


def _describe(old, new) -> str:
    to, tn = _jtype(old), _jtype(new)
    if to == tn and to in ("boolean", "number"):
        return f"{_canon(old)} -> {_canon(new)}"
    if to == tn:
        return f"changed ({tn})"
    return f"changed ({to} -> {tn})"


def _read(path: Path):
    """The file's bytes, or None when it does not exist. Any other failure raises."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def snapshot(dest: str, files: list) -> None:
    root = Path(dest)
    (root / _FILES).mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / _ABSENT).mkdir(exist_ok=True, mode=0o700)
    for f in files:
        p = Path(f)
        data = _read(p)
        if data is None:
            (root / _ABSENT / p.name).touch(mode=0o600)
            continue
        target = root / _FILES / p.name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(target, 0o600)


def _before(snap: Path, name: str):
    """(known, bytes-or-None): whether the snapshot knows this file, and its bytes if it existed."""
    kept = snap / _FILES / name
    if kept.is_file():
        return True, kept.read_bytes()
    if (snap / _ABSENT / name).exists():
        return True, None
    return False, None


def _backup_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    n = 0
    while True:
        d = root / (f"pre-marketplace-add-{stamp}" + (f"-{n}" if n else ""))
        try:
            d.mkdir(mode=0o700)
            os.chmod(d, 0o700)
            return d
        except FileExistsError:
            n += 1


def _esc(name: str) -> str:
    """A key or file name for printing: control characters and non-printables are escaped
    (JSON style), so a name can never start a line of its own. Ordinary names are unchanged."""
    out = []
    for ch in json.dumps(name, ensure_ascii=False)[1:-1]:
        if ch.isprintable():
            out.append(ch)
        elif ord(ch) <= 0xFFFF:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append("\\U%08x" % ord(ch))
    return "".join(out)


def _keep(name: str, before: bytes, backup_root: Path, folder: list) -> None:
    """Write one file's backup and announce it now. A failure is said, never silent."""
    try:
        if not folder:
            folder.append(_backup_dir(backup_root))
        target = folder[0] / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(before)
        os.chmod(target, 0o600)
        say(f"your previous {_esc(name)} is kept at: {target}")
    except Exception as exc:  # noqa: BLE001 - say it, keep going (C5)
        say(f"no backup was kept ({type(exc).__name__})")


def _report_file(snap: Path, p: Path, backup_root: Path, folder: list) -> None:
    name = p.name
    shown = _esc(name)
    known, before = _before(snap, name)
    if not known:
        say(f"no snapshot of {shown} was taken, so changes to it cannot be listed")
        return
    after = _read(p)
    if before is None and after is None:
        return
    if before is None:
        say(f"created: {shown}")
        return
    if after is None:
        say(f"removed: {shown}")
        _keep(name, before, backup_root, folder)
        return
    old, new = _parse_object(before), _parse_object(after)
    if old is None or new is None:
        if hashlib.sha256(before).digest() != hashlib.sha256(after).digest():
            say(f"changed (not parsed as JSON): {shown}")
            _keep(name, before, backup_root, folder)
        return
    diffs: list = []
    _walk(old, new, "", diffs)
    if not diffs:
        return
    say(f"{shown} was changed by the marketplace add:")
    for kind, path, detail in diffs:
        say(f"  {_esc(path)}: {detail or kind}")
    if any(kind in ("removed", "changed") for kind, _, _ in diffs):
        _keep(name, before, backup_root, folder)


def report(snap_dir: str, backup_root: str, files: list) -> None:
    snap = Path(snap_dir)
    if not (snap / _FILES).is_dir():
        say("no snapshot was taken, so changes to your Claude files cannot be listed")
        return
    folder: list = []  # the one backup folder of this report, made when the first backup is needed
    for f in files:
        p = Path(f)
        try:  # each file stands alone: one file's error never hides another's changes or backup
            _report_file(snap, p, Path(backup_root), folder)
        except Exception as exc:  # noqa: BLE001 - never fail the install (C5)
            say(f"claude-config diff unavailable: {type(exc).__name__}")


def main(argv=None) -> int:
    try:
        args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
        if args.cmd == "snapshot":
            snapshot(args.dest, args.files)
        else:
            report(args.snapshot, args.backup_root, args.files)
    except BaseException as exc:  # noqa: BLE001 - never fail the install (C5)
        if isinstance(exc, KeyboardInterrupt):
            raise
        say(f"claude-config diff unavailable: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
