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
regular file (symlink, fifo, subdirectory, socket) refuses, and so does any entry whose name
begins with "." (".put-*" is a suspected leftover of an interrupted secret-put; any other dotfile,
such as .DS_Store, is an unexpected hidden file), judged with lstat and never
followed. getfacl missing or failing -> exit 2 (never a pass).
macOS (darwin): the ACL is read from the entry lines of `ls -ldeq <dir>` / `ls -leq <file>` (run with
"--" before the path), because the mode suffix shows "@" instead of "+" when the file also has
extended attributes. Any entry line, or a "+" suffix, refuses, named by basename, with the
remediation `chmod -N <path>`; "@" alone passes (macOS adds com.apple.provenance to ordinary files).
If ls fails, the suffix is anything else, or a later line is not an entry -> exit 2.
ls is called by absolute path (/bin/ls, part of macOS), never through PATH, so a GNU coreutils `ls`
earlier on PATH (it rejects -e) is never used; a missing /bin/ls is "cannot tell" (exit 2).
Other platforms: the ACL half is not implemented; a note goes to stderr every time, the stdout
verdict says "modes only; ACL not checked on this platform", and the mode bits and the
regular-file rule are still enforced.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

NON_LINUX_NOTE = "secrets ACL not checked on this platform"
PASS_MODES_ONLY = "secrets check: pass (modes only; ACL not checked on this platform)"
DARWIN_LS = "/bin/ls"  # absolute path on purpose: no PATH lookup, no env override, no fallback
_LS_MODE_RE = re.compile(r"^[-bcdlpsDw?][-rwxsStTlL]{9}[+@]?$")  # darwin: only none, "@" or "+" is understood
_ACL_ENTRY_RE = re.compile(r"^\s*\d+: \S")
NOTE_CHMOD = "note: 'chmod g-w' changes only the ACL mask, not a named entry."
NOTE_WIDEN = "note: removing one ACL entry can widen the mask; read the full ACL before and after."


class Result:
    """code 0 pass / 1 refuse / 2 cannot tell; problems and remediation are lists of lines.
    A plain class (not a dataclass) so any importlib loader can load this file."""

    def __init__(self, code=0, problems=None, remediation=None, acl_checked=True):
        self.code = code
        self.acl_checked = acl_checked  # False: only modes and the regular-file rule were judged
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
    except FileNotFoundError:
        raise _CannotTell("getfacl not found (install the acl package)")
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


def _darwin_acl_defects(path: str, is_dir: bool) -> list:
    """macOS: the ACL is read from the entry lines of `ls -ldeq` (dir) / `ls -leq` (file), because the
    mode suffix shows "@" instead of "+" when the file also has extended attributes (ls prints ONE suffix).
    -q prints non-printable name characters as "?", so a name cannot fake an entry line.
    ls is DARWIN_LS (/bin/ls, part of macOS), called by absolute path so a GNU coreutils `ls` earlier on
    PATH, which rejects -e, is never used; if that file is missing this is _CannotTell, never a PATH fallback.
    Line 1 is the long line: its mode field must end in nothing, "@" or "+" (anything else, e.g. "%", is
    not understood). Every later non-empty line must be an ACL entry (" 0: group:everyone allow read").
    Returns ["extended ACL"] for at least one entry line or a "+" suffix, else [].
    Raises _CannotTell when ls fails or its output does not have that shape."""
    try:
        r = subprocess.run(
            [DARWIN_LS, "-ldeq" if is_dir else "-leq", "--", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise _CannotTell("ls not found")
    except (OSError, subprocess.SubprocessError) as exc:
        raise _CannotTell(type(exc).__name__)
    if r.returncode != 0:
        raise _CannotTell("ls exit %d" % r.returncode)
    lines = r.stdout.splitlines()
    fields = lines[0].split(None, 1) if lines else []
    if not fields or not _LS_MODE_RE.match(fields[0]):
        raise _CannotTell("unparseable ls output")
    entries = 0
    for line in lines[1:]:
        if not line.strip():
            continue
        if not _ACL_ENTRY_RE.match(line):
            raise _CannotTell("unparseable ls output")
        entries += 1
    return ["extended ACL"] if entries or fields[0].endswith("+") else []


def check(home) -> Result:
    home = Path(home)
    sec = home / "secrets"
    linux = sys.platform == "linux"
    darwin = sys.platform == "darwin"
    acl_checked = linux or darwin
    if not acl_checked:
        print(NON_LINUX_NOTE, file=sys.stderr)
    res = Result(acl_checked=acl_checked)
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
        if acl_checked:
            try:
                defects = (_acl_defects if linux else _darwin_acl_defects)(str(path), is_dir)
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
        if name.startswith("."):
            # secret-put's temp files are mkstemp(prefix=".put-"): such a dotfile is the residue of a
            # secret-put killed between the write and the replace, and it holds the whole value.
            # Any other dotfile (.DS_Store from Finder, ...) is refused too, with its own message.
            if name.startswith(".put-"):
                res.problems.append("%s: suspected leftover temporary file from an interrupted secret-put; remove it" % name)
            else:
                res.problems.append("%s: unexpected hidden file in secrets/ (for example .DS_Store from Finder); remove it" % name)
            chmod_lines.append("rm -- %s" % shlex.quote(str(p)))
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
            lines.append(("setfacl -b -k %s" if linux else "chmod -N %s") % shlex.quote(str(sec)))
        for p in fix_acl_files:
            lines.append(("setfacl -b %s" if linux else "chmod -N %s") % shlex.quote(str(p)))
        lines.extend(chmod_lines)
        lines.append("re-run the check")
        if not darwin:  # the mask notes are about Linux ACLs; chmod -N removes the whole ACL
            lines.extend([NOTE_CHMOD, NOTE_WIDEN])
        res.remediation = lines
    return res


def render(res: Result) -> str:
    out = []
    if res.code == 0:
        return "secrets check: pass" if res.acl_checked else PASS_MODES_ONLY
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
