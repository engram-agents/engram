#!/usr/bin/env python3
"""secrets_check — the check on <ENGRAM_HOME>/secrets/ [engram#4842 item 4].

An extended ACL on a credential directory is a defect by definition. The check reads
the ACL back through getfacl (never st_mode alone: an ACL mask is reported in the group
bits) and walks the directory AND every entry in it.

  secrets_check.py [--home DIR]      exit 0 pass, 1 refuse, 2 cannot tell

Library: check(home) -> Result(code, problems, remediation). Problems name an entry by
BASENAME only. No output of this module ever contains a secret value: no file is read.

Linux: directory passes only if mode 0700 and its access ACL is exactly the three base
entries (no named user/group entry, no mask line) and it has no default ACL line at all;
each regular file passes only if mode 0600 and base entries only. Anything that is not a
regular file (symlink, fifo, subdirectory, socket) refuses, judged with lstat and never
followed. getfacl missing or failing -> exit 2 (never a pass).
Other platforms: the ACL half is not implemented; a note goes to stderr every time and
the mode bits and the regular-file rule are still enforced.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

NON_LINUX_NOTE = "secrets ACL not checked on this platform"
NOTE_CHMOD = "note: 'chmod g-w' changes only the ACL mask, not a named entry."
NOTE_WIDEN = "note: removing one ACL entry can widen the mask; read the full ACL before and after."


class Result:
    """code 0 pass / 1 refuse / 2 cannot tell; problems and remediation are lists of lines.
    A plain class (not a dataclass) so any importlib loader can load this file."""

    def __init__(self, code=0, problems=None, remediation=None):
        self.code = code
        self.problems = [] if problems is None else problems
        self.remediation = [] if remediation is None else remediation


class _CannotTell(Exception):
    pass


def _acl_defects(path: str, is_dir: bool) -> list:
    """ACL defects of `path` as short phrases. Raises _CannotTell when getfacl cannot answer."""
    try:
        r = subprocess.run(
            ["getfacl", "-p", "--absolute-names", "--", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _CannotTell(type(exc).__name__)
    if r.returncode != 0:
        raise _CannotTell("getfacl exit %d" % r.returncode)
    defects = []
    base_seen = set()
    for raw in r.stdout.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("default:"):
            defects.append("default ACL")
            continue
        parts = line.split(":")
        kind = parts[0]
        qualifier = parts[1] if len(parts) > 1 else ""
        if kind == "mask":
            defects.append("ACL mask line")
        elif kind in ("user", "group") and qualifier:
            defects.append("named %s ACL entry" % kind)
        elif kind in ("user", "group", "other") and not qualifier:
            base_seen.add(kind)
        else:
            defects.append("unrecognised ACL line")
    if base_seen != {"user", "group", "other"}:
        raise _CannotTell("incomplete getfacl output")
    return sorted(set(defects))


def check(home) -> Result:
    home = Path(home)
    sec = home / "secrets"
    linux = sys.platform == "linux"
    if not linux:
        print(NON_LINUX_NOTE, file=sys.stderr)
    res = Result()
    try:
        st = os.lstat(sec)
    except FileNotFoundError:
        res.code = 1
        res.problems.append("no secrets folder at %s" % sec)
        return res
    except OSError as exc:
        res.code = 2
        res.problems.append("cannot tell: secrets: %s" % type(exc).__name__)
        return res
    if not stat.S_ISDIR(st.st_mode):
        res.code = 1
        res.problems.append("secrets is not a directory")
        return res

    cannot_tell = []
    fix_acl_dir = False
    fix_acl_files = []
    chmod_lines = []

    def judge(path: Path, name: str, st_mode: int, want: int, is_dir: bool):
        nonlocal fix_acl_dir
        if stat.S_IMODE(st_mode) != want:
            res.problems.append("%s: mode %o, want %o" % (name, stat.S_IMODE(st_mode), want))
            chmod_lines.append("chmod %o %s" % (want, shlex.quote(str(path))))
        if linux:
            try:
                defects = _acl_defects(str(path), is_dir)
            except _CannotTell as exc:
                cannot_tell.append("%s: cannot tell (%s)" % (name, exc))
                return
            if defects:
                res.problems.append("%s: %s" % (name, ", ".join(defects)))
                if is_dir:
                    fix_acl_dir = True
                else:
                    fix_acl_files.append(path)

    judge(sec, "secrets", st.st_mode, 0o700, True)

    try:
        names = sorted(os.listdir(sec))
    except OSError as exc:
        names = []
        cannot_tell.append("secrets: cannot tell (listdir %s)" % type(exc).__name__)
    for name in names:
        p = sec / name
        try:
            est = os.lstat(p)
        except OSError as exc:
            cannot_tell.append("%s: cannot tell (lstat %s)" % (name, type(exc).__name__))
            continue
        if not stat.S_ISREG(est.st_mode):
            kind = "symlink" if stat.S_ISLNK(est.st_mode) else "not a regular file"
            res.problems.append("%s: %s" % (name, kind))
            continue
        judge(p, name, est.st_mode, 0o600, False)

    if res.problems:
        res.code = 1
    elif cannot_tell:
        res.code = 2
    if res.code == 2 or (res.code == 1 and cannot_tell):
        res.problems.extend(cannot_tell)
    if res.code == 1:
        lines = []
        if fix_acl_dir:
            lines.append("setfacl -b -k %s" % shlex.quote(str(sec)))
        for p in fix_acl_files:
            lines.append("setfacl -b %s" % shlex.quote(str(p)))
        lines.extend(chmod_lines)
        lines.append("re-run the check")
        lines.extend([NOTE_CHMOD, NOTE_WIDEN])
        res.remediation = lines
    return res


def render(res: Result) -> str:
    out = []
    if res.code == 0:
        return "secrets check: pass"
    out.append("secrets check: %s" % ("refuse" if res.code == 1 else "cannot tell"))
    out.extend("  " + p for p in res.problems)
    out.extend(res.remediation)
    return "\n".join(out)


def _default_home() -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _owner_read import engram_home  # the ONE tools resolver

    return engram_home()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    home = None
    if argv[:1] == ["--home"] and len(argv) == 2:
        home = Path(argv[1])
    elif argv:
        print("usage: secrets_check.py [--home DIR]", file=sys.stderr)
        return 1
    if home is None:
        try:
            home = _default_home()
        except ValueError as exc:
            print("secrets_check: %s" % exc, file=sys.stderr)
            return 1
    res = check(home)
    print(render(res))
    return res.code


if __name__ == "__main__":
    sys.exit(main())
