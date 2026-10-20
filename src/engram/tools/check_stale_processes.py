#!/usr/bin/env python3
"""check_stale_processes.py -- is a long-lived ENGRAM process OLDER than the last deploy? (engram-alpha#3938)

An upgrade (install-local-marketplace.sh plus the reload trio) restarts only the MCP server.  The
engramd OWNER and the SURFACE DAEMON keep running the code they started with, so a seat can be on
mixed old/new code while every "is it alive?" check passes.  This gate compares each process's START
TIME with `deployed_at` in `$ENGRAM_HOME/.deployed-version`.

Three processes, one output line each, always in this order, then one summary line (so a reader who
counts fewer than four lines knows the tool broke):

    owner           pid in $ENGRAM_HOME/engramd.pid          launched script engramd_owner.py
    surface-daemon  pid in $ENGRAM_HOME/recall-daemon.pid    launched script engram-surface-daemon.py
    mcp-server      pid in $ENGRAM_HOME/mcp-tools-ready.json launched script server.py

Process identity is one rule for all three, element-level, never a substring: the pid's argv list
must be [<interpreter>, <script>, ...] where basename(argv[0]) matches `python[0-9.]*` (case-
insensitive: a macOS framework or Homebrew Python execs `.../Python.app/Contents/MacOS/Python`) and
basename(argv[1]) is exactly the script above.  The rule matches the launched script BY BASENAME: a
same-named script under any python passes.  The bound that makes that enough: the pid must also come
from ENGRAM's own pidfile and be alive, so the rule only has to reject a recycled pid.  Real shapes (measured from /proc/<pid>/cmdline):
`<venv>/bin/python3 <plugin>/engramd_owner.py`, `<venv>/bin/python3 <plugin>/hooks/engram-surface-
daemon.py <ENGRAM_HOME>`, `<venv>/bin/python3 <plugin>/server.py`.  No python flags may sit between
the interpreter and the script: none of the three launchers passes any, so the rule stays exact.
`vim engramd_owner.py` or `tail -f engram-surface-daemon.py` on a recycled pid is UNVERIFIED.

Verdicts (exactly one per process):

    FRESH        pidfile regular, positive int pid, alive, argv is the expected process, started at
                 or after deployed_at
    STALE        as FRESH but started BEFORE deployed_at (the line names pid, start, deployed_at and
                 the restart command)
    NOT_RUNNING  pidfile absent, or names a pid that is not alive
    UNVERIFIED   anything else: a pidfile that is not a regular file, unparseable content, a live pid
                 whose argv is not the expected process (a recycled pid), an unreadable start time

Exit codes (gate mode): 0 every process FRESH; 1 at least one STALE (wins over the rest); 3 no STALE
but some NOT_RUNNING / UNVERIFIED (a missing pidfile is never reported as clean); 2 `.deployed-version`
missing / unreadable / not a regular file / no parseable `deployed_at` (nothing can be judged; the
per-process lines are still printed, as UNVERIFIED); 4 the tool itself broke.  Exit 4 is the code of
EVERY end that is not a verdict, so 1 (STALE) and 2 (no deploy marker) are never a crash in
disguise: a failed import of a helper, a command-line error (argparse would say 2), an exception in
the check, a failure writing the output, a SystemExit(n) raised by a helper (caught as
`(Exception, SystemExit)`, as tools/_owner_read.py does).  (`--help` is not a failure and exits 0.)
Two bounds: KeyboardInterrupt is not caught (the shell's 130), and a usage error together with a dead
stderr (`--bogus` with stderr on /dev/full) fails inside argparse's own error print and exits 120,
which is the interpreter-shutdown path and out of scope.
`--informational` prints the same lines and exits 0 for every VERDICT: install-local-marketplace.sh
runs it, because right after an install every running process is older than the deploy by
construction.  A broken tool is still exit 4 there; the installer turns that into one warning line.

Two upstreams, STALE if either is before deployed_at: (A) the process start time below and (B) the
pidfile's own mtime (os.stat only).  A host suspend plus guest time sync (WSL2) can move btime
forward, which would make A later than the truth and a stale process read FRESH; B does not share
that clock.  They differing by more than 60 s is noted on the line and changes no verdict.

Start time is never taken from a rendered wall-clock string (`ps -o lstart` prints local time with no
zone).  Linux: /proc/<pid>/stat field 22 (clock ticks since boot) over SC_CLK_TCK plus btime from
/proc/stat.  macOS: `ps -o etime=` subtracted from the current time.

Bound on the MCP line: it covers only the server named in mcp-tools-ready.json (the most recent
session's); other concurrent sessions' servers are not checked.

Never opens a sqlite store.  Pidfiles and markers are read through engram_regular_io, never a plain
open(), because a FIFO at that path would block a plain open forever (#3591, #3628).
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


def _find_helper_dir() -> Optional[Path]:
    """The directory holding engram_regular_io.py: CLAUDE_PLUGIN_ROOT first, then a walk of this
    file's parents (installed plugin: helper at the plugin root with tools/ beside it; source
    tree: src/engram/).  Never a fixed parents[N]: the build flattens the layout."""
    cands: list[Path] = []
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        cands.append(Path(root))
    here = Path(__file__).resolve()
    for p in (here.parent, *here.parents):
        cands.append(p)
        cands.append(p / "src" / "engram")
    for c in cands:
        try:
            if (c / "engram_regular_io.py").exists():
                return c
        except OSError:  # an unreadable directory on the walk is not a reason to crash
            continue
    return None


def _die(msg: str) -> None:
    """Exit 4 (the tool broke) with one stderr line.  Exit 1 is STALE and 2 is NO_DEPLOY: an
    import-time traceback must never be mistaken for either."""
    print("check_stale_processes: %s" % msg, file=sys.stderr)
    sys.exit(4)


try:
    import engram_regular_io  # noqa: F401
except ImportError:
    _helper_dir = _find_helper_dir()
    if _helper_dir is not None:
        sys.path.append(str(_helper_dir))
    try:
        import engram_regular_io  # noqa: F401
    except (Exception, SystemExit) as _e:
        _die("cannot import engram_regular_io (the shared pidfile helper, installed beside the "
             "engram plugin); set CLAUDE_PLUGIN_ROOT or reinstall (%s: %s)" % (type(_e).__name__, _e))
except (Exception, SystemExit) as _e:  # the helper is present but broken
    _die("engram_regular_io failed to import (%s: %s)" % (type(_e).__name__, _e))
try:
    from engram_regular_io import kind_of_path, read_regular_text  # noqa: E402
except (Exception, SystemExit) as _e:
    _die("engram_regular_io lacks kind_of_path / read_regular_text (%s: %s); reinstall"
         % (type(_e).__name__, _e))

# The ENGRAM home has ONE definition: tools/_owner_read.engram_home() (#3862, #3863).  `tools` is
# found one directory above this file in BOTH layouts (src/engram/ and the deployed plugin root).
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.append(_PKG_ROOT)
try:
    from tools import _owner_read as owner_read  # noqa: E402
except (Exception, SystemExit) as _e:  # a partial or broken install: ImportError, or the helper failing on import
    _die("cannot import tools/_owner_read (the ENGRAM-home definition; the install looks partial "
         "or broken, reinstall): %s: %s" % (type(_e).__name__, _e))

DISAGREE_S = 60

EXIT_FRESH, EXIT_STALE, EXIT_NO_DEPLOY, EXIT_UNJUDGED, EXIT_BROKEN = 0, 1, 2, 3, 4

FRESH, STALE, NOT_RUNNING, UNVERIFIED = "FRESH", "STALE", "NOT_RUNNING", "UNVERIFIED"

MCP_BOUND = ("mcp-server covers only the server named in mcp-tools-ready.json (the most recent "
             "session's); other concurrent sessions' servers are not checked")

_ETIME_RE = re.compile(r"^(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)$")
_PYTHON_RE = re.compile(r"python[0-9.]*", re.IGNORECASE)


# ── parsers (pure) ────────────────────────────────────────────────────────────

def parse_proc_stat_starttime_ticks(text: str) -> Optional[int]:
    """Field 22 (starttime, clock ticks since boot) of /proc/<pid>/stat.  Field 2 (comm) may hold
    spaces and parens, so the fields are counted after the LAST ")"; they then start at field 3."""
    close = text.rfind(")")
    if close < 0:
        return None
    rest = text[close + 1:].split()
    idx = 22 - 3
    if len(rest) <= idx:
        return None
    try:
        return int(rest[idx])
    except ValueError:
        return None


def parse_etime(text: str) -> Optional[int]:
    """`ps -o etime=` elapsed time, `[[dd-]hh:]mm:ss`, in seconds; None when it is not that shape."""
    m = _ETIME_RE.match(text.strip())
    if not m:
        return None
    days, hours, mins, secs = (int(g) if g else 0 for g in m.groups())
    return days * 86400 + hours * 3600 + mins * 60 + secs


def parse_deployed_at(text: str) -> Optional[int]:
    """Epoch seconds of the `deployed_at=%Y-%m-%dT%H:%M:%SZ` (UTC) line, or None."""
    for line in text.splitlines():
        key, sep, val = line.partition("=")
        if sep and key.strip() == "deployed_at":
            try:
                return calendar.timegm(time.strptime(val.strip(), "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                return None
    return None


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch // 1)))


# ── process readers (injectable) ──────────────────────────────────────────────

@dataclass
class Readers:
    """The three things read from the process table.  alive(pid) -> bool; argv(pid) -> the argv as a
    LIST of strings, or None when unreadable; start_epoch(pid) -> epoch seconds (float) or None."""
    alive: Callable[[int], bool]
    argv: Callable[[int], Optional[list]]
    start_epoch: Callable[[int], Optional[float]]
    # Instrument B: the pidfile's own mtime (stat only, never an open: a FIFO would block).
    mtime: Callable[[Path], Optional[float]] = None  # type: ignore[assignment]


def _have_proc() -> bool:
    return os.path.isfile("/proc/self/stat")


def _ps(args: list) -> Optional[str]:
    try:
        r = subprocess.run(["ps", *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def _real_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):  # a pid >= 2**31 raises OverflowError: not a live process
        return False
    return True


def _real_argv(pid: int) -> Optional[list]:
    """The argv as a list.  Linux: /proc/<pid>/cmdline split on NUL, exact.  macOS has no /proc and
    `ps` prints one flat string, so it is split with shlex (falling back to whitespace); a path that
    contains a space cannot be told from two arguments there.  The bound is harmless for identity,
    which looks only at argv[0] and argv[1] of three launchers that use space-free paths."""
    if _have_proc():
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                raw = f.read()
        except OSError:
            return None
        return [p for p in raw.decode("utf-8", "replace").split("\0") if p] or None
    out = _ps(["-o", "args=", "-p", str(pid)])
    if not out:
        return None
    try:
        return shlex.split(out) or None
    except ValueError:
        return out.split() or None


def identity_ok(argv, script: str) -> bool:
    """The one identity rule for all three processes: argv is [python*, <script>, ...], compared
    per element on basenames.  Never a substring of the joined argv; no flags between the two."""
    if not argv or len(argv) < 2:
        return False
    return (_PYTHON_RE.fullmatch(os.path.basename(argv[0])) is not None
            and os.path.basename(argv[1]) == script)


def _real_start_epoch(pid: int) -> Optional[float]:
    if _have_proc():
        try:
            with open("/proc/%d/stat" % pid, "r", encoding="utf-8", errors="replace") as f:
                ticks = parse_proc_stat_starttime_ticks(f.read())
            with open("/proc/stat", "r", encoding="utf-8", errors="replace") as f:
                btime = next((int(ln.split()[1]) for ln in f if ln.startswith("btime ")), None)
            hz = os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, StopIteration, IndexError):
            return None
        if ticks is None or btime is None or not hz:
            return None
        return btime + ticks / float(hz)
    out = _ps(["-o", "etime=", "-p", str(pid)])
    elapsed = parse_etime(out) if out else None
    return None if elapsed is None else time.time() - elapsed


def _real_mtime(path: Path) -> Optional[float]:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def real_readers() -> Readers:
    return Readers(alive=_real_alive, argv=_real_argv, start_epoch=_real_start_epoch,
                   mtime=_real_mtime)


# ── restart commands ──────────────────────────────────────────────────────────

def resolve_plugin_root(home, env=None) -> Path:
    """The DEPLOYED plugin root, which is what every printed command must name (never this file's
    own location: a run from a source checkout would print src/engram/..., and the source layout
    differs from the deployed one).  $CLAUDE_PLUGIN_ROOT when it holds tools/check_stale_processes.py
    or hooks/start-engram-daemon.sh; else <ENGRAM_HOME>/marketplace/plugins/engram."""
    env = os.environ if env is None else env
    given = env.get("CLAUDE_PLUGIN_ROOT")
    if given:
        c = Path(given)
        try:
            if (c / "tools" / "check_stale_processes.py").exists() or \
                    (c / "hooks" / "start-engram-daemon.sh").exists():
                return c
        except OSError:
            pass
    return Path(home) / "marketplace" / "plugins" / "engram"


def _restart_command(name: str, pid: int, home: Path, root: Path, system: str) -> str:
    q = shlex.quote
    darwin = system == "Darwin"
    if name == "owner":
        setup = q(str(root / "tools" / "setup-surface-daemon-service.sh"))
        if darwin:  # its bootout + bootstrap IS the restart
            return "bash %s --owner" % setup
        key = q(str(root / "tools" / "engramd-owner-home-key.sh"))
        return "bash %s --owner && systemctl --user restart \"engramd-owner-$(bash %s %s).service\"" % (
            setup, key, q(str(home)))
    if name == "surface-daemon":
        start = q(str(root / "hooks" / "start-engram-daemon.sh"))
        manual = "kill -TERM %d, wait for exit, then CLAUDE_PLUGIN_ROOT=%s bash %s </dev/null" % (
            pid, q(str(root)), start)
        if darwin:  # label is a fixed literal in setup-surface-daemon-service.sh (PLIST_LABEL)
            return ("launchctl kickstart -k gui/$(id -u)/com.engram.surface-daemon (if it runs under "
                    "launchd); else " + manual)
        return "systemctl --user restart engram-surface-daemon.service (if it runs under a unit); else " + manual
    return "end the Claude Code session that owns it and start a new one, or /mcp reconnect"


# ── the check ─────────────────────────────────────────────────────────────────

_PROCS = (
    ("owner", "engramd.pid", "engramd_owner.py"),
    ("surface-daemon", "recall-daemon.pid", "engram-surface-daemon.py"),
    ("mcp-server", "mcp-tools-ready.json", "server.py"),
)


def _read_pid(path: Path, is_json: bool):
    """-> (pid, None) or (None, (verdict, reason))."""
    kind = kind_of_path(path)
    if kind is not None:
        return None, (UNVERIFIED, "pidfile %s is a %s, not a regular file" % (path.name, kind))
    try:
        text = read_regular_text(path)
    except FileNotFoundError:
        return None, (NOT_RUNNING, "no %s" % path.name)
    except OSError as e:
        return None, (UNVERIFIED, "cannot read %s (%s)" % (path.name, e.strerror or type(e).__name__))
    if is_json:
        try:
            obj = json.loads(text)
        except ValueError:
            return None, (UNVERIFIED, "%s is not valid JSON" % path.name)
        pid = obj.get("pid") if isinstance(obj, dict) else None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None, (UNVERIFIED, "%s has no positive integer pid" % path.name)
        return pid, None
    s = text.strip()
    if not re.fullmatch(r"[0-9]+", s) or int(s) <= 0:
        return None, (UNVERIFIED, "%s does not hold a positive integer pid" % path.name)
    return int(s), None


def _judge(name, pidfile, script, home, deployed_epoch, readers, root, system):
    """-> (verdict, detail)."""
    pid, problem = _read_pid(home / pidfile, pidfile.endswith(".json"))
    if problem is not None:
        return problem
    if not readers.alive(pid):
        return NOT_RUNNING, "pid %d is not alive" % pid
    argv = readers.argv(pid)
    if argv is None:
        return UNVERIFIED, "pid %d is alive but its argv is unreadable" % pid
    if not identity_ok(argv, script):
        return UNVERIFIED, "pid %d is alive but its argv is not python %s (recycled pid?)" % (
            pid, script)
    start = readers.start_epoch(pid)
    if start is None:
        return UNVERIFIED, "pid %d start time unreadable" % pid
    if deployed_epoch is None:
        return UNVERIFIED, "pid %d started %s; no deployed_at to compare" % (pid, _iso(start))
    # Two independent upstreams (amendment to #3938): A = the boot-clock start time, B = the
    # pidfile's own mtime.  A host suspend plus guest time sync (WSL2) can move btime forward, which
    # makes A LATER than the truth: a stale process would read FRESH, the silent direction.  STALE if
    # EITHER is before deployed_at; FRESH only if both are at or after it.
    mtime = (readers.mtime or _real_mtime)(home / pidfile)
    note = ""
    if mtime is not None and abs(mtime - start) > DISAGREE_S:
        note = " (instruments disagree: proc=%s pidfile_mtime=%s)" % (_iso(start), _iso(mtime))
    stale_a = int(start // 1) < deployed_epoch
    stale_b = mtime is not None and int(mtime // 1) < deployed_epoch
    if stale_a or stale_b:
        shown = start if stale_a else mtime
        return STALE, "pid %d started %s%s, before deployed_at %s; restart: %s" % (
            pid, _iso(shown), "" if stale_a else " (pidfile mtime)", _iso(deployed_epoch),
            _restart_command(name, pid, home, root, system)) + note
    return FRESH, "pid %d started %s, deployed_at %s%s" % (
        pid, _iso(start), _iso(deployed_epoch), note)


def run_check(home, informational: bool, readers: Optional[Readers] = None, root=None, system=None):
    """-> (the four output lines, the exit code)."""
    home = Path(home)
    readers = readers or real_readers()
    root = Path(root) if root is not None else resolve_plugin_root(home)
    system = system if system is not None else platform.system()

    deployed_epoch, why_blind = None, None
    dv = home / ".deployed-version"
    kind = kind_of_path(dv)
    if kind is not None:
        why_blind = ".deployed-version is a %s, not a regular file" % kind
    else:
        try:
            deployed_epoch = parse_deployed_at(read_regular_text(dv))
            if deployed_epoch is None:
                why_blind = ".deployed-version has no parseable deployed_at"
        except FileNotFoundError:
            why_blind = ".deployed-version is missing"
        except OSError as e:
            why_blind = ".deployed-version unreadable (%s)" % (e.strerror or type(e).__name__)

    lines, tally = [], {FRESH: 0, STALE: 0, NOT_RUNNING: 0, UNVERIFIED: 0}
    for name, pidfile, script in _PROCS:
        if why_blind is not None:
            verdict, detail = UNVERIFIED, "cannot judge: " + why_blind
        else:
            verdict, detail = _judge(name, pidfile, script, home, deployed_epoch, readers, root, system)
        tally[verdict] += 1
        lines.append("%s: %s %s" % (name, verdict, detail))

    if why_blind is not None:
        code = EXIT_NO_DEPLOY
    elif tally[STALE]:
        code = EXIT_STALE
    elif tally[NOT_RUNNING] or tally[UNVERIFIED]:
        code = EXIT_UNJUDGED
    else:
        code = EXIT_FRESH
    mode = "informational, exits 0, gate would be" if informational else ("PASS" if code == 0 else "FAIL")
    lines.append("summary: %s (exit %d) fresh=%d stale=%d not_running=%d unverified=%d. %s%s" % (
        mode, code, tally[FRESH], tally[STALE], tally[NOT_RUNNING], tally[UNVERIFIED], MCP_BOUND,
        "; " + why_blind if why_blind else ""))
    return lines, (0 if informational else code)


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a bad command line, which is EXIT_NO_DEPLOY here.  A usage error is the
    tool failing to run, so it is exit 4.  `--help` still exits 0."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_BROKEN, "%s: error: %s\n" % (self.prog, message))


def main(argv=None) -> int:
    ap = _Parser(
        description="Fail when the engramd owner, the surface daemon or the MCP server started "
                    "before the last deploy (engram-alpha#3938).")
    ap.add_argument("--informational", action="store_true",
                    help="print the same lines and the restart commands; exit 0 for every verdict")
    args = ap.parse_args(argv)
    try:
        home = owner_read.engram_home()
        lines, code = run_check(home, args.informational)
        print("\n".join(lines))
        sys.stdout.flush()
    except (Exception, SystemExit) as e:  # the tool itself broke: never let a traceback exit 1 (= STALE)
        print("check_stale_processes: tool error: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        try:  # a dead stdout would fail again at interpreter shutdown with a code of its own
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return EXIT_BROKEN
    return code


if __name__ == "__main__":
    sys.exit(main())
