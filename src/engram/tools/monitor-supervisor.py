#!/usr/bin/env python3
"""monitor-supervisor.py -- one supervisor, one Monitor, one expiry (#2982 step 1).

Every agent seat currently arms THREE separate long-running Monitor watchers
(forum DM/baton updates, forum @-mentions, umwelt world bell), each capped at
the Monitor tool's 30-minute hard lifetime, each requiring its own manual
re-arm, out of phase with the other two. This script is the cheap interim
fix: it runs the three EXISTING pollers as children of a single supervisor
process, so arming becomes ONE Monitor instead of three, and a lapsed re-arm
becomes one coverage gap instead of three independent ones. It does NOT
remove the 30-minute cap -- that is the later "relay session" design
(#2982's step 2). The three wrapped children
(forum-updates-monitor.sh, forum-mention-monitor.sh, umwelt-bell.py) are
UNCHANGED -- this script adds nothing on top of their own suppression /
seeding / coalescing logic (R9); it only proxies their stdout/stderr and
manages their process lifecycle.

Requirements implemented (see engram-alpha#2982 step 1 spec for the full
rationale behind each):

  R1  Every forwarded line is tagged with its source: "[bell]", "[updates]",
      "[mention]".
  R2  stdout/stderr are kept SEPARATE, never merged. A child's stdout goes to
      this supervisor's stdout (the Monitor event stream -- what wakes the
      agent); a child's stderr goes to this supervisor's stderr (logged to
      the output file only, never wakes anyone). umwelt-bell.py has real
      stderr-only lines (a "possible duplicate instance" warning, forum-
      recovery notices) that must not wake the agent -- merging streams
      would be a real regression, not a cosmetic one.
  R3  Lines are forwarded line-atomically: each child is read with
      `readline()` (never an arbitrary byte-chunk read), and every print is
      taken under one lock shared across all six reader threads (stdout +
      stderr, x3 children) so two children's lines can never interleave
      mid-line.
  R4  A child's death is LOUD: exactly one stdout line
      "[supervisor] CHILD <name> EXITED code=<n>", then a restart with
      capped exponential backoff (1s, 2s, 4s, 8s, ... capped at
      BACKOFF_CAP_SECONDS), with a second stdout line announcing each retry
      attempt.
  R5  No child outlives the supervisor: the supervisor makes itself its own
      process-group leader (`os.setpgrp()`) before spawning any child, so
      every child inherits that pgid; SIGTERM/SIGINT to the supervisor kills
      the whole group.
  R6  Survives being SIGKILLed: a signal handler cannot run on SIGKILL, so
      instead, on STARTUP (before spawning anything), the supervisor reads
      its pidfile and reaps (kills) whatever process group is recorded there
      if it is still alive -- covering both "the old leader is still
      running" and "the old leader is dead but its orphaned children (same
      pgid) are not" (the crash-recovery case this exists for).
  R7  Single instance per uid falls out of R6: a second supervisor start
      reaps the first one's group before proceeding, so after both have run
      exactly one set of three children remains.
  R8  No periodic heartbeat print -- ever. Liveness is checkable OUT OF BAND
      via a small status file (JSON: per-child pid / last-forwarded-line
      timestamp / restart count) that this supervisor rewrites periodically;
      reading it never wakes anyone.
  R9  Adds nothing of its own on top of the children's existing behaviour
      (engaged-window suppression, seed-on-first-successful-poll, the
      bell's own self-reap) -- pure proxy + lifecycle management.

Usage (production defaults -- reads ~/.umwelt/config.json for the bell's
agent/world_url):
    monitor-supervisor.py --purpose collab-loop

Health check (`--check`, #3311 Linux /proc; #3330 ps/lsof backend for macOS):
    monitor-supervisor.py --check [--pidfile P] [--status-file S] [--max-status-age SECONDS]

A read-only verification that the supervisor armed as ONE Monitor is actually
healthy -- it never starts, signals, reaps or restarts anything and never
writes the pidfile or status file. Under the supervisor the children's stdout
is a PIPE to the supervisor and only the supervisor's own stdout is the
Monitor's socket, so the old three-Monitor "fd 1 is a socket" check is wrong
here. Four items are verified, always printed (never stopping at the first
failure) as exactly five stdout lines:

    supervisor: <PASS|FAIL> pid=<n> <reason>
    updates:    <PASS|FAIL> pid=<n|?> <reason>
    mention:    <PASS|FAIL> pid=<n|?> <reason>
    bell:       <PASS|FAIL> pid=<n|?> <reason>
    CHECK <PASS|FAIL> <k>/4 failed[; extra child pid=<n>[,<n>...]]

supervisor PASSes iff the pidfile pid is alive, is a monitor-supervisor
(`_is_supervisor_process`), and its fd 1 is a `socket:`. A child PASSes iff
the status file (fresh) AND the process table agree: its status-file pid is
alive, is a direct child of the supervisor, carries the right script name in
argv (forum-updates-monitor / forum-mention-monitor / umwelt-bell -- the
default names; a `--*-cmd-json` override that renames the script will fail
this item), and its fd 1 is a `pipe:[N]` that the supervisor itself also holds
open. The supervisor's children are enumerated from the process table by
ppid (never `pgrep -f`); any child the status file does not name is an
"extra child" and fails the check (exit 1) even if all four items PASS.

Staleness: the supervisor records its own `status_interval` in the status
file; the default max age is 3x that value, `--max-status-age` overrides, and
a status file without the field (older supervisor) falls back to 3x
DEFAULT_STATUS_INTERVAL_SECONDS and says so on the supervisor line.

Two backends, identical output shape and exit codes. With /proc present
(Linux) the process table is read from /proc, as above. Without /proc
(macOS) ONE uid-scoped `ps -U <uid> -o pid=,ppid=,stat=,args=` snapshot gives
liveness (stat Z = dead), ppid and argv (the script NAME anywhere in args,
never argv[0]); a child's stdout is a PIPE iff `lsof -n -P -a -p <pid> -d 0-2
-F pftdn` shows fd 1 as type PIPE whose device/peer pair the supervisor's own
`lsof -F pftdn` shows reversed (the supervisor holds a PIPE whose device is
the child's peer AND whose peer is the child's device); the supervisor's own
stdout is the harness's `unix` socket. If lsof is absent or errors, the
pipe items FAIL naming lsof -- never PASS. All ps/lsof calls go through the
module-level CHECK_RUNNER seam, which tests replace with captured output.

Exit codes: 0 all four PASS; 1 any FAIL or extra child; 2 pidfile
missing/unreadable/not an integer (ONE line, `CHECK FAIL supervisor not
armed: ...`); 3 neither backend works -- no /proc AND ps unusable (ONE line,
`CHECK FAIL unsupported platform`). Exit 4 (start, #3773): the pidfile path is
not a single regular file (a FIFO, symlink, hard link, directory, socket ...):
ONE stderr line names it, nothing was started, no child was spawned; the same
meaning as the engramd owner's EXIT_PIDFILE_NOT_REGULAR. A pidfile or status
file that is not a regular file is read as unknown and never blocks `--check`.

See --help for every override (script paths, pidfile/status-file locations,
backoff/status-interval tuning, and the --*-cmd-json escape hatches the test
suite uses to substitute controllable fakes for one or more children without
touching the real scripts).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


# Inline copy by ordering choice, not constraint: importing tools._owner_read first would append the package root to
# sys.path, so the first `import engram_regular_io` could succeed from there and skip the CLAUDE_PLUGIN_ROOT-first lookup.
# Pinned identical to every other copy by tests/test_bounded_sibling_lookups_3983.py.
def _bounded_parents(here):
    """Directories to search for a sibling runtime file: `here`'s own directory
    up to and INCLUDING its own tree root, nearest first, never above it (#3983).
    The tree root is the nearest ancestor holding engram_core.py (the plugin root
    installed, src/engram/ in a source checkout; an essential-tier file, so every
    build ships it).  server.py is no marker: viz/server.py exists.  With no
    engram_core.py above, only the own directory and its parent are searched
    (the conservative case).  An unreadable directory is skipped
    (never taken for the root), so the walk still ends at the first readable root."""
    chain = []
    for d in here.parents:
        chain.append(d)
        try:
            if (d / "engram_core.py").exists():
                return chain
        except OSError:
            continue  # unreadable: not a root we can see; keep climbing
    return chain[:2]


def _find_helper_dir() -> Path | None:
    """The directory holding engram_regular_io.py: CLAUDE_PLUGIN_ROOT first,
    then a walk of this file's parents (the installed plugin has the helper at
    the plugin root with tools/ beside it; the source tree has it in
    src/engram/). Never a fixed parents[N]: the build flattens the layout."""
    cands: list[Path] = []
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        cands.append(Path(root))
    here = Path(__file__).resolve()
    for p in _bounded_parents(here):  # own tree only, never above its root (#3983)
        cands.append(p)
        cands.append(p / "src" / "engram")
    for c in cands:
        if (c / "engram_regular_io.py").exists():
            return c
    return None


try:
    import engram_regular_io  # noqa: F401
except ImportError:
    _helper_dir = _find_helper_dir()
    if _helper_dir is not None:
        sys.path.append(str(_helper_dir))
    try:
        import engram_regular_io  # noqa: F401
    except ImportError:
        print("monitor-supervisor: cannot import engram_regular_io (the shared "
              "pidfile helper, installed beside the engram plugin); set "
              "CLAUDE_PLUGIN_ROOT or reinstall", file=sys.stderr)
        sys.exit(1)
from engram_regular_io import (
    NotRegularFile,
    kind_of_path,
    read_regular_text,
    write_pidfile,
    write_regular_bytes_synced,
)

# The ENGRAM home has ONE definition: tools/_owner_read.engram_home() (U14, #3862; #3863). `tools` is
# found one directory above this file in BOTH layouts (src/engram/ and the deployed plugin root).
# APPENDED, not inserted first: this is a long-running supervisor launched by path, and the plugin
# root must not shadow anything already importable.
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.append(_PKG_ROOT)
from tools import _owner_read as owner_read  # noqa: E402

EXIT_PIDFILE_NOT_REGULAR = 4

TAGS = {
    "bell": "[bell]",
    "updates": "[updates]",
    "mention": "[mention]",
}
CHILD_NAMES = ("bell", "updates", "mention")

# R4: capped exponential backoff. 1, 2, 4, 8, 16, 32, 60, 60, ... -- an
# ambiguous-decision (the spec names the shape, not the exact cap): see the
# handoff's ambiguous-decisions log.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0

# If a child stays up longer than this before dying again, its backoff
# counter resets to 0 -- otherwise a child that runs fine for hours and dies
# once a day would face an ever-growing backoff from all its accumulated
# past restarts. Not named by the spec; a local judgment call (see handoff).
BACKOFF_RESET_AFTER_UPTIME_SECONDS = 60.0

# Both the start path and --check default to these two, so under one ENGRAM_HOME they cannot disagree.
DEFAULT_PIDFILE = str(owner_read.engram_home() / ".monitor-supervisor.pid")
DEFAULT_STATUS_FILE = str(owner_read.engram_home() / ".monitor-supervisor-status.json")
DEFAULT_STATUS_INTERVAL_SECONDS = 10.0
DEFAULT_UMWELT_CONFIG = str(Path.home() / ".umwelt" / "config.json")
DEFAULT_UMWELT_CURSOR_FILE = str(Path.home() / ".umwelt" / "bell-cursors.json")
DEFAULT_UMWELT_BELL_SCRIPT = str(
    Path.home() / ".umwelt" / "marketplace" / "plugins" / "umwelt" / "tools" / "umwelt-bell.py"
)
DEFAULT_UMWELT_INTERVAL_SECONDS = 5.0

# --check reads the process table here (module constant so tests can point it
# at a nonexistent root to exercise the "no /proc" exit).
PROC_ROOT = "/proc"
STATUS_STALE_MULTIPLIER = 3.0
_CHECK_ARGV_FRAGMENT = {
    "updates": "forum-updates-monitor",
    "mention": "forum-mention-monitor",
    "bell": "umwelt-bell",
}
_CHECK_ORDER = ("updates", "mention", "bell")


# ---------------------------------------------------------------------------
# R6/R7: start-time reap of a prior instance's process group.
# ---------------------------------------------------------------------------
def _pid_cmdline_status(pid: int) -> tuple[str, str]:
    """Portable cmdline lookup for `pid`. Returns (status, cmdline):
      "ok"      -- cmdline is the process's args (may be empty string)
      "gone"    -- the pid does not exist as a live process
      "unknown" -- could not determine (ps unavailable / errored / timed out)

    Ported from umwelt-bell.py's proc_cmdline() (umwelt#205): `ps -p <pid>
    -o args=` is portable across macOS (BSD ps, no /proc) and Linux
    (procps); the trailing `=` suppresses the header on both. This is the
    load-bearing identity check now -- see reap_stale_supervisor below --
    not merely informational, so "unknown" must be distinguishable from
    "ok" (a confirmed non-match) rather than collapsed into an empty string.

    `ww` (BSD-style "wide, wide" -- unlimited output width) is REQUIRED,
    not cosmetic: without it, `ps` truncates `args=` to whatever width it
    detects (falls back to a hardcoded 80 columns when it has no
    controlling terminal to query -- exactly the case for a `ps` subprocess
    spawned from a supervisor that itself has none, e.g. under a test
    runner or a detached/daemonized process). Measured live in this
    worktree: without `ww`, a genuine monitor-supervisor.py instance's own
    cmdline (script path alone is 92 characters here) was silently cut to
    80 chars before ever reaching "monitor-supervisor.py" in argv, so
    `_is_supervisor_process` below saw a truncated, non-matching string and
    refused to reap a perfectly genuine prior instance -- breaking R6/R7
    (SIGKILL recovery + single-instance) whenever the install path is long
    enough. Root-caused via `tests/test_monitor_supervisor.py`'s own
    `test_single_instance_two_starts`, which failed ONLY under `pytest`
    (no controlling terminal) and passed when driven as a standalone
    script attached to a real tty -- i.e. it is not a `pytest`-only quirk,
    it is "no controlling terminal", which `pytest` merely happens to be
    the common instance of on this host.
    """
    try:
        out = subprocess.run(
            ["ps", "ww", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return ("unknown", "")
    if out.returncode != 0:
        # ps exits non-zero when the pid isn't found -- the process is gone.
        return ("gone", "")
    return ("ok", out.stdout.strip())


def _is_supervisor_process(cmd: str) -> bool:
    """True iff cmd is a python interpreter running THIS script
    (monitor-supervisor.py).

    Same shape as umwelt-bell.py's _is_bell_process() (umwelt#205): requires
    BOTH argv[0] look like a python interpreter (so a shell/tmux-pane/other
    long-running process that merely happens to occupy this pid is spared),
    AND some later argv token IS the script name (so a python one-liner that
    merely mentions the filename as a string literal is spared too -- the
    string must be a standalone argv token, e.g. the path this script itself
    was invoked with).
    """
    if not cmd:
        return False
    argv = cmd.split()
    if not argv:
        return False
    # Case-insensitive: the python.org macOS framework interpreter is
    # `.../Python.app/Contents/MacOS/Python` (capital P). This guard is the one
    # place that is decided -- R6/R7 and --check (both backends) go through it.
    if not os.path.basename(argv[0]).lower().startswith("python"):
        return False
    return any(
        a == "monitor-supervisor.py" or a.endswith("/monitor-supervisor.py")
        for a in argv[1:]
    )


def reap_stale_supervisor(pidfile_path: str) -> None:
    """R6: reap a still-alive prior supervisor's process group before this
    instance proceeds -- but ONLY once its identity is confirmed.

    Every supervisor instance makes itself its own process-group leader via
    os.setpgrp() at startup (see main()), so pid == pgid for the value
    recorded in the pidfile, and reaping the group is one os.killpg() call:
    it kills the recorded leader AND every child it ever spawned into that
    group, in one shot.

    "Alive" is checked at the GROUP level (`os.killpg(pid, 0)`), not the bare
    pid: a dead/zombie leader can leave live orphaned children behind in the
    same pgid -- this is exactly the SIGKILL-recovery case R6 exists for
    (SIGKILL only ever hits the leader; its children, reparented but still
    pgid-tagged, keep running). Checking only `os.kill(pid, 0)` would miss
    that case, because the leader itself can be gone while the group is not.

    The cmdline identity check is now LOAD-BEARING, not informational (fixed
    per the round-1 review, mirroring umwelt-bell.py's reap_prior() three-way
    branch from umwelt#205): the recorded pid may have been reused by an
    UNRELATED process since the pidfile was last written -- routine on any
    long-running shared host -- and if that unrelated process happens to be
    its own process-group leader (true of shells, tmux panes, other
    supervisors, another agent's session wrapper), an unconditional
    os.killpg() would SIGKILL that entire unrelated process group. So:

      - the recorded pid is confirmed GONE (no live process there at all,
        e.g. a dead leader whose orphaned children -- caught by the group
        probe above -- keep the pgid alive): this is exactly the
        SIGKILL-recovery case R6 exists for; reap the group.
      - the recorded pid's identity canNOT be determined ("unknown" -- e.g.
        `ps` unavailable/errored): refuse and say so loudly, matching the
        bell's own /proc-on-macOS lesson -- claiming to have reaped without
        being able to verify is exactly what mints a silent duplicate.
      - the recorded pid is confirmed ALIVE and its cmdline matches THIS
        script: reap the group.
      - the recorded pid is confirmed ALIVE and its cmdline does NOT match
        (pid reuse): refuse to kill, print a loud warning, and proceed --
        the caller still gets a fresh, working supervisor; only the
        (evidently stale/wrong) pidfile entry is left alone.
    """
    try:
        old_pid = int(read_regular_text(pidfile_path).strip())
    except (OSError, ValueError):
        # no pidfile, or unreadable -- nothing recorded, nothing to reap. A
        # pidfile that resolves to a FIFO / directory / socket (#3773) is
        # named, once, and nothing is killed.
        kind = kind_of_path(pidfile_path)
        if kind is not None:
            print(f"[supervisor] pidfile {pidfile_path} is a {kind}, not a regular "
                  f"file; not reaping", file=sys.stderr, flush=True)
        return
    if old_pid == os.getpid():
        return
    try:
        os.killpg(old_pid, 0)  # probe only: does this process group still exist?
    except ProcessLookupError:
        return  # group is fully gone -- nothing to reap
    except PermissionError:
        print(f"[supervisor] pidfile names pgid {old_pid} but signalling it "
              f"was refused (owned by a different uid?) -- NOT reaping",
              file=sys.stderr, flush=True)
        return

    status, cmdline = _pid_cmdline_status(old_pid)
    if status == "unknown":
        print(f"[supervisor] could not verify prior pid {old_pid} is a "
              f"monitor-supervisor (ps unavailable/errored); NOT reaping -- "
              f"if a duplicate supervisor results, kill it by hand",
              file=sys.stderr, flush=True)
        return
    if status == "ok" and not _is_supervisor_process(cmdline):
        print(f"[supervisor] pidfile {pidfile_path!r} names pid {old_pid}, "
              f"but its cmdline does not look like a monitor-supervisor "
              f"({cmdline!r}) -- pid was likely reused by an unrelated "
              f"process; NOT reaping -- if a duplicate supervisor results, "
              f"kill it by hand", file=sys.stderr, flush=True)
        return
    # status == "gone" (leader dead, orphaned same-pgid children keep the
    # group probe above alive -- the crash-recovery case) or status == "ok"
    # with a confirmed matching cmdline: reap.
    print(f"[supervisor] reaping stale supervisor process group pgid={old_pid} "
          f"found in pidfile {pidfile_path}", file=sys.stderr, flush=True)
    try:
        os.killpg(old_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    time.sleep(0.3)  # let SIGKILL land before we spawn into a fresh group


# ---------------------------------------------------------------------------
# Child command construction
# ---------------------------------------------------------------------------
def _load_umwelt_config(config_path: str) -> dict:
    try:
        return json.loads(Path(config_path).read_text())
    except Exception:
        return {}


def build_child_commands(args: argparse.Namespace) -> dict[str, list[str]]:
    """Resolve the three child argv lists. Each child can be overridden
    wholesale via its own --*-cmd-json flag (JSON-encoded argv list) -- the
    test suite's escape hatch for substituting a controllable fake child
    without touching forum-updates-monitor.sh / forum-mention-monitor.sh /
    umwelt-bell.py, all three of which are out of this change's scope."""
    tools_dir = Path(__file__).resolve().parent
    cmds: dict[str, list[str]] = {}

    if args.updates_cmd_json:
        cmds["updates"] = json.loads(args.updates_cmd_json)
    else:
        script = args.updates_script or str(tools_dir / "forum-updates-monitor.sh")
        cmds["updates"] = [script, "--purpose", args.purpose]

    if args.mention_cmd_json:
        cmds["mention"] = json.loads(args.mention_cmd_json)
    else:
        script = args.mention_script or str(tools_dir / "forum-mention-monitor.sh")
        cmds["mention"] = [script, "--purpose", args.purpose]

    if args.bell_cmd_json:
        cmds["bell"] = json.loads(args.bell_cmd_json)
    else:
        cfg = _load_umwelt_config(args.umwelt_config)
        agent = args.umwelt_agent or cfg.get("agent")
        world_url = args.umwelt_world_url or cfg.get("world_url")
        if not agent or not world_url:
            raise SystemExit(
                "monitor-supervisor.py: --umwelt-agent/--umwelt-world-url "
                f"not given and not found in {args.umwelt_config} "
                "(expected 'agent' and 'world_url' keys) -- pass them "
                "explicitly, or use --bell-cmd-json to bypass the bell "
                "entirely"
            )
        bell_script = args.bell_script or DEFAULT_UMWELT_BELL_SCRIPT
        cmds["bell"] = [
            "python3", bell_script, agent, world_url,
            args.umwelt_cursor_file, str(args.umwelt_interval),
        ]
    return cmds


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------
class _Child:
    def __init__(self, name: str, cmd: list[str]):
        self.name = name
        self.cmd = cmd
        self.proc: subprocess.Popen | None = None
        self.restart_count = 0
        self.last_line_ts: float | None = None
        self.started_at: float | None = None


class Supervisor:
    def __init__(
        self,
        child_cmds: dict[str, list[str]],
        status_file: str,
        status_interval: float,
        pidfile: str,
    ):
        self.children = {name: _Child(name, cmd) for name, cmd in child_cmds.items()}
        self.status_file = status_file
        self.status_interval = status_interval
        self.pidfile = pidfile
        self.lock = threading.Lock()  # R3: shared across every reader thread
        self.shutdown = threading.Event()
        self._status_refusal_reported = False  # #3781: once per transition

    # -- lifecycle -----------------------------------------------------
    def start_child(self, name: str) -> None:
        child = self.children[name]
        proc = subprocess.Popen(
            child.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        child.proc = proc
        child.started_at = time.time()
        threading.Thread(
            target=self._reader, args=(name, proc.stdout, sys.stdout, True),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._reader, args=(name, proc.stderr, sys.stderr, False),
            daemon=True,
        ).start()
        threading.Thread(target=self._watcher, args=(name,), daemon=True).start()

    def start_all(self) -> None:
        for name in self.children:
            self.start_child(name)
        self.write_status()
        threading.Thread(target=self._status_loop, daemon=True).start()

    # -- R3: line-atomic, tagged forwarding -----------------------------
    def _reader(self, name: str, pipe, out_stream, is_stdout: bool) -> None:
        tag = TAGS[name]
        try:
            for line in iter(pipe.readline, ""):
                if line == "":
                    break
                line = line.rstrip("\n")
                with self.lock:
                    print(f"{tag} {line}", file=out_stream, flush=True)
                if is_stdout:
                    self.children[name].last_line_ts = time.time()
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    # -- R4: loud death + capped-backoff restart ------------------------
    def _watcher(self, name: str) -> None:
        child = self.children[name]
        proc = child.proc
        assert proc is not None
        proc.wait()
        if self.shutdown.is_set():
            return
        code = proc.returncode
        with self.lock:
            print(f"[supervisor] CHILD {name} EXITED code={code}", flush=True)

        uptime = time.time() - (child.started_at or time.time())
        if uptime >= BACKOFF_RESET_AFTER_UPTIME_SECONDS:
            child.restart_count = 0
        backoff = min(
            BACKOFF_BASE_SECONDS * (2 ** child.restart_count),
            BACKOFF_CAP_SECONDS,
        )
        with self.lock:
            print(f"[supervisor] restarting {name} in {backoff:g}s "
                  f"(attempt {child.restart_count + 1})", flush=True)
        if self.shutdown.wait(backoff):
            return  # shutdown fired while we were waiting out the backoff
        child.restart_count += 1
        self.start_child(name)  # spawns fresh reader/watcher threads for the new proc

    # -- R8: no heartbeat print; a periodic status FILE instead --------
    def write_status(self) -> None:
        data = {"generated_at": time.time(),
                "status_interval": self.status_interval, "children": {}}
        for name, child in self.children.items():
            proc = child.proc
            data["children"][name] = {
                "pid": proc.pid if proc else None,
                "alive": bool(proc and proc.poll() is None),
                "last_line_ts": child.last_line_ts,
                "restart_count": child.restart_count,
            }
        tmp = self.status_file + ".tmp"
        Path(tmp).parent.mkdir(parents=True, exist_ok=True)
        # #3781: the status file is a DIAGNOSTIC sink. A temp path that is not ONE
        # regular file (FIFO, directory, socket, symlink, hard link) skips this
        # write and is reported once per transition; the supervisor keeps
        # running and `--check` reads the status as stale.
        tmp_written = False
        try:
            write_regular_bytes_synced(tmp, json.dumps(data).encode("utf-8"))
            tmp_written = True
            os.replace(tmp, self.status_file)
        except OSError as exc:
            # A refused strict tmp write leaves the planted path untouched; only
            # a failed replace after a good write (e.g. a directory at the
            # destination) unlinks our own tmp.
            if tmp_written:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            if not self._status_refusal_reported:
                self._status_refusal_reported = True
                print(f"monitor-supervisor: status not written: {exc}",
                      file=sys.stderr, flush=True)
            return
        self._status_refusal_reported = False

    def _status_loop(self) -> None:
        while not self.shutdown.wait(self.status_interval):
            self.write_status()

    # -- R5: no child survives the supervisor ---------------------------
    def teardown(self) -> None:
        """SIGTERM the whole process group (this supervisor is a member of
        its own group, so this signal also reaches it -- harmless, since it
        already installed its own SIGTERM/SIGINT handler that just sets
        the shutdown event, not the default terminate-immediately
        disposition). Wait out the grace period for children to exit; only
        if the deadline is reached with children STILL alive does the final
        os.killpg(SIGKILL) fire.

        Round-2 fix (suggestion 1): that final SIGKILL used to fire
        UNCONDITIONALLY, every time -- which, because the supervisor shares
        its children's process group, also SIGKILLed the supervisor ITSELF
        on the ordinary graceful-shutdown path, making the os.remove(pidfile)
        right after it unreachable and turning every clean shutdown into a
        SIGKILL-shaped exit instead of a normal return."""
        pgid = os.getpgrp()
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.time() + 2.0
        graceful = False
        while time.time() < deadline:
            alive = [c for c in self.children.values() if c.proc and c.proc.poll() is None]
            if not alive:
                graceful = True
                break
            time.sleep(0.1)
        if not graceful:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.remove(self.pidfile)
        except OSError:
            pass


def _install_signal_handler(supervisor: Supervisor) -> None:
    def _handler(signum, frame):  # noqa: ARG001 - signal handler signature
        supervisor.shutdown.set()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


# ---------------------------------------------------------------------------
# --check (#3311): read-only health verification. /proc backend (Linux)
# below; the ps/lsof backend (#3330) for seats without /proc follows it.
# ---------------------------------------------------------------------------
def _proc_stat(pid: int) -> tuple[str, int] | None:
    """(state, ppid) from /proc/<pid>/stat, or None if unreadable. comm may
    contain spaces/parens, so split after the LAST ')'."""
    try:
        raw = Path(f"{PROC_ROOT}/{pid}/stat").read_text(errors="replace")
        rest = raw.rsplit(")", 1)[1].split()
        return rest[0], int(rest[1])
    except (OSError, IndexError, ValueError):
        return None


def _proc_alive(pid: int) -> bool:
    st = _proc_stat(pid)
    return st is not None and st[0] != "Z"


def _fd_target(pid: int, fd: int) -> str | None:
    try:
        return os.readlink(f"{PROC_ROOT}/{pid}/fd/{fd}")
    except OSError:
        return None


def _held_fd_targets(pid: int) -> set[str]:
    out: set[str] = set()
    try:
        names = os.listdir(f"{PROC_ROOT}/{pid}/fd")
    except OSError:
        return out
    for n in names:
        try:
            out.add(os.readlink(f"{PROC_ROOT}/{pid}/fd/{n}"))
        except OSError:
            pass
    return out


def _proc_argv(pid: int) -> str:
    try:
        raw = Path(f"{PROC_ROOT}/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode(errors="replace").strip()


def _process_table_children(sup_pid: int) -> set[int]:
    """Live (non-zombie) direct children of sup_pid, by scanning
    /proc/*/stat for ppid == sup_pid. Never pgrep -f: ppid enumeration cannot
    self-match the calling shell."""
    kids: set[int] = set()
    try:
        entries = os.listdir(PROC_ROOT)
    except OSError:
        return kids
    for e in entries:
        if not e.isdigit():
            continue
        st = _proc_stat(int(e))
        if st is not None and st[1] == sup_pid and st[0] != "Z":
            kids.add(int(e))
    return kids


def _check_supervisor_item(sup_pid: int, note: str) -> tuple[bool, str]:
    if not _proc_alive(sup_pid):
        return False, f"pid {sup_pid} not alive"
    status, cmdline = _pid_cmdline_status(sup_pid)
    if status == "unknown":
        return False, "could not verify identity (ps unavailable/errored)"
    if status == "gone":
        return False, f"pid {sup_pid} not alive"
    if not _is_supervisor_process(cmdline):
        return False, f"not a monitor-supervisor ({cmdline!r})"
    target = _fd_target(sup_pid, 1)
    if target is None:
        return False, "stdout (fd 1) UNREADABLE"
    if not target.startswith("socket:"):
        return False, f"stdout (fd 1) is {target}, not a socket"
    return True, f"stdout is {target}" + note


def _check_child_item(name: str, sup_pid: int, entry, stale: str | None,
                      sup_fds: set[str]) -> tuple[str, bool, str]:
    """Returns (pid-text, passed, reason). Steps run in the spec's order so
    the reason names the FIRST failing step."""
    pid = entry.get("pid") if isinstance(entry, dict) else None
    pid_txt = str(pid) if isinstance(pid, int) and not isinstance(pid, bool) else "?"
    if stale is not None:
        return pid_txt, False, stale
    if pid_txt == "?":
        return pid_txt, False, "no pid in status file"
    if not _proc_alive(pid):
        return pid_txt, False, f"pid {pid} not alive"
    st = _proc_stat(pid)
    ppid = st[1] if st else None
    if ppid != sup_pid:
        return pid_txt, False, f"not a child of supervisor (ppid={ppid}, supervisor={sup_pid})"
    frag = _CHECK_ARGV_FRAGMENT[name]
    argv = _proc_argv(pid)
    if frag not in argv:
        return pid_txt, False, f"argv does not identify {frag} ({argv!r})"
    target = _fd_target(pid, 1)
    if target is None or not target.startswith("pipe:["):
        return pid_txt, False, f"stdout (fd 1) is {target or 'UNREADABLE'}, not a pipe"
    if target not in sup_fds:
        return pid_txt, False, f"stdout {target} is not held open by the supervisor"
    return pid_txt, True, f"parent={sup_pid} stdout={target}"


# -- ps/lsof backend (#3330): seats with no /proc (macOS). ---------------------
def _default_runner(argv: list[str]) -> tuple[int, str]:
    """Run argv, return (returncode, stdout). Raises OSError / SubprocessError
    when the binary cannot run (absent, timed out) -- callers name that."""
    out = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=10)
    return out.returncode, out.stdout


# The ONE seam for every ps / lsof invocation the ps backend makes (module
# constant so tests substitute captured output, like PROC_ROOT).
CHECK_RUNNER = _default_runner


def _run_tool(argv: list[str], accept_nonzero=None) -> tuple[str | None, str]:
    """(error-or-None, stdout) from CHECK_RUNNER. Never raises.

    A non-zero exit is an error UNLESS `accept_nonzero(stdout)` is true: lsof
    can exit 1 on harmless warnings while its -F output is complete. The
    predicate must test for positive evidence in the output (never merely
    "non-empty"); callers still require the full match for any PASS."""
    try:
        rc, out = CHECK_RUNNER(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{argv[0]} unavailable ({exc.__class__.__name__}: {exc})", ""
    if rc != 0 and not (accept_nonzero is not None and accept_nonzero(out)):
        return f"{argv[0]} exited {rc}", out
    return None, out


def _ps_scope() -> str:
    """Value for `ps -U`: the login NAME (procps and BSD ps both accept one;
    BSD acceptance of a bare number is unverified), else the numeric uid."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        return str(os.getuid())


def _parse_ps(out: str) -> dict[int, tuple[int, str, str]]:
    table: dict[int, tuple[int, str, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        table[int(parts[0])] = (int(parts[1]), parts[2], parts[3] if len(parts) > 3 else "")
    return table


def _ps_snapshot() -> dict[int, tuple[int, str, str]] | None:
    """ONE uid-scoped process-table snapshot {pid: (ppid, stat, args)}, or None
    if ps is unusable. Scoped to the caller's uid: on a shared Mac an unscoped
    listing also shows another account's monitors (F2). `-ww`: unlimited args
    width (see _pid_cmdline_status)."""
    # Any non-zero exit = ps unusable: the extras line needs a COMPLETE table.
    err, out = _run_tool(["ps", "-ww", "-U", _ps_scope(), "-o", "pid=,ppid=,stat=,args="])
    if err is not None:
        return None
    return _parse_ps(out) or None


def _ps_alive(table: dict, pid: int) -> bool:
    row = table.get(pid)
    return row is not None and not row[1].startswith("Z")


def _lsof_records(pid: int, fds: str | None) -> tuple[str | None, list[dict]]:
    """(error-or-None, records) from `lsof -F pftdn`; each record is
    {"fd","t","d","n"} for one open file. `fds` narrows with -d (e.g. "0-2")."""
    argv = ["lsof", "-n", "-P", "-a", "-p", str(pid)]
    if fds:
        argv += ["-d", fds]
    # Non-zero exit accepted only with this pid's own `p<pid>` record present.
    err, out = _run_tool(argv + ["-F", "pftdn"],
                         accept_nonzero=lambda o: f"p{pid}" in o.splitlines())
    if err is not None:
        return err, []
    recs: list[dict] = []
    for line in out.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "f":
            recs.append({"fd": val})
        elif tag in "tdn" and recs:
            recs[-1][tag] = val
    return None, recs


def _pipe_pair(rec: dict) -> tuple[str, str] | None:
    """(device, peer) of a PIPE record, else None."""
    if rec.get("t") != "PIPE":
        return None
    n = rec.get("n", "")
    if not rec.get("d") or not n.startswith("->"):
        return None
    return rec["d"], n[2:]


def _ps_supervisor_item(table: dict, sup_pid: int, note: str,
                        sup_recs: tuple[str | None, list[dict]]) -> tuple[bool, str]:
    if not _ps_alive(table, sup_pid):
        return False, f"pid {sup_pid} not alive"
    args = table[sup_pid][2]
    if not _is_supervisor_process(args):
        return False, f"not a monitor-supervisor ({args!r})"
    err, recs = sup_recs
    if err is not None:
        return False, f"stdout (fd 1) UNVERIFIED: {err}"
    fd1 = next((r for r in recs if r.get("fd") == "1"), None)
    if fd1 is None:
        return False, "stdout (fd 1) UNREADABLE"
    if fd1.get("t") not in ("unix", "sock"):
        return False, f"stdout (fd 1) is {fd1.get('t')}, not a socket"
    return True, f"stdout is {fd1.get('t')} socket" + note


def _ps_child_item(name: str, sup_pid: int, entry, stale: str | None, table: dict,
                   sup_recs: tuple[str | None, list[dict]]) -> tuple[str, bool, str]:
    """Same step order, text and result shape as _check_child_item."""
    pid = entry.get("pid") if isinstance(entry, dict) else None
    pid_txt = str(pid) if isinstance(pid, int) and not isinstance(pid, bool) else "?"
    if stale is not None:
        return pid_txt, False, stale
    if pid_txt == "?":
        return pid_txt, False, "no pid in status file"
    if not _ps_alive(table, pid):
        return pid_txt, False, f"pid {pid} not alive"
    ppid, _stat, args = table[pid]
    if ppid != sup_pid:
        return pid_txt, False, f"not a child of supervisor (ppid={ppid}, supervisor={sup_pid})"
    frag = _CHECK_ARGV_FRAGMENT[name]
    if frag not in args:  # the script NAME anywhere in args, never argv[0] (F3)
        return pid_txt, False, f"argv does not identify {frag} ({args!r})"
    err, recs = _lsof_records(pid, "0-2")
    if err is not None:
        return pid_txt, False, f"stdout (fd 1) UNVERIFIED: {err}"
    fd1 = next((r for r in recs if r.get("fd") == "1"), None)
    pair = _pipe_pair(fd1) if fd1 is not None else None
    if pair is None:
        kind = fd1.get("t") if fd1 else None
        return pid_txt, False, f"stdout (fd 1) is {kind or 'UNREADABLE'}, not a pipe"
    sup_err, sup_list = sup_recs
    if sup_err is not None:
        return pid_txt, False, f"stdout pipe UNVERIFIED: supervisor {sup_err}"
    dev, peer = pair
    # F1: BOTH directions -- the supervisor holds the other end of this pipe.
    if not any(_pipe_pair(r) == (peer, dev) for r in sup_list):
        return pid_txt, False, f"stdout pipe {dev} is not held open by the supervisor"
    return pid_txt, True, f"parent={sup_pid} stdout=pipe {dev}->{peer}"


def _load_status(status_file: str, max_status_age: float | None):
    """Status-file upstream shared by both backends: (data, stale, note, children)."""
    data = None
    stale: str | None = None
    note = ""
    try:
        data = json.loads(read_regular_text(status_file))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        generated = float(data["generated_at"])
        age = time.time() - generated
        if max_status_age is not None:
            limit = max_status_age
        else:
            interval = data.get("status_interval")
            if isinstance(interval, (int, float)) and not isinstance(interval, bool) and interval > 0:
                limit = STATUS_STALE_MULTIPLIER * float(interval)
            else:
                limit = STATUS_STALE_MULTIPLIER * DEFAULT_STATUS_INTERVAL_SECONDS
                note = (" (status_interval absent from status file; assumed default "
                        f"{DEFAULT_STATUS_INTERVAL_SECONDS:g}s)")
        if age > limit:
            stale = f"status file missing/stale (age {age:.0f}s, max {limit:g}s)"
    except Exception as exc:  # missing, unparseable, no generated_at
        kind = kind_of_path(status_file) if isinstance(exc, OSError) else None
        if kind is not None:
            # #3773: a FIFO / directory / socket is UNKNOWN, named, never read.
            stale = f"status file is a {kind}, not a regular file"
        else:
            stale = f"status file missing/stale ({exc.__class__.__name__})"
        if not isinstance(data, dict):
            data = None
    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(children, dict):
        children = {}
    return data, stale, note, children


def _emit_verdict(results: list[tuple[str, str, bool, str]], extras: list[int]) -> int:
    """The five stdout lines + exit code, shared by both backends."""
    for label, pid_txt, ok, reason in results:
        print(f"{label + ':':<12}{'PASS' if ok else 'FAIL'} pid={pid_txt} {reason}")
    failed = sum(1 for r in results if not r[2])
    verdict = "PASS" if failed == 0 and not extras else "FAIL"
    tail = ""
    if extras:
        tail = "; extra child pid=" + ",".join(str(p) for p in extras)
    print(f"CHECK {verdict} {failed}/4 failed{tail}")
    return 0 if verdict == "PASS" else 1


def _evaluate_proc(sup_pid: int, data, stale, note, children):
    results: list[tuple[str, str, bool, str]] = []  # label, pid, passed, reason
    try:
        ok, reason = _check_supervisor_item(sup_pid, note)
    except Exception as exc:
        ok, reason = False, f"check error: {exc!r}"
    results.append(("supervisor", str(sup_pid), ok, reason))

    sup_fds = _held_fd_targets(sup_pid)
    for name in _CHECK_ORDER:
        try:
            pid_txt, ok, reason = _check_child_item(
                name, sup_pid, children.get(name), stale, sup_fds)
        except Exception as exc:
            pid_txt, ok, reason = "?", False, f"check error: {exc!r}"
        results.append((name, pid_txt, ok, reason))

    extras: list[int] = []
    if data is not None and _proc_alive(sup_pid):
        named = {e.get("pid") for e in children.values() if isinstance(e, dict)}
        extras = sorted(_process_table_children(sup_pid) - named)
    return results, extras


def _evaluate_ps(sup_pid: int, table: dict, data, stale, note, children):
    results: list[tuple[str, str, bool, str]] = []
    # One lsof of the supervisor, shared by its own item and every child's
    # pipe match. Only worth asking once the supervisor is alive.
    sup_recs: tuple[str | None, list[dict]] = (
        _lsof_records(sup_pid, None) if _ps_alive(table, sup_pid)
        else ("not alive", []))
    try:
        ok, reason = _ps_supervisor_item(table, sup_pid, note, sup_recs)
    except Exception as exc:
        ok, reason = False, f"check error: {exc!r}"
    results.append(("supervisor", str(sup_pid), ok, reason))
    for name in _CHECK_ORDER:
        try:
            pid_txt, ok, reason = _ps_child_item(
                name, sup_pid, children.get(name), stale, table, sup_recs)
        except Exception as exc:
            pid_txt, ok, reason = "?", False, f"check error: {exc!r}"
        results.append((name, pid_txt, ok, reason))

    extras: list[int] = []
    if data is not None and _ps_alive(table, sup_pid):
        named = {e.get("pid") for e in children.values() if isinstance(e, dict)}
        extras = sorted(p for p, (pp, st, _a) in table.items()
                        if pp == sup_pid and not st.startswith("Z") and p not in named)
    return results, extras


def run_check(pidfile: str, status_file: str, max_status_age: float | None) -> int:
    # Backend: /proc if the seat has one (unchanged path), else ONE ps snapshot.
    table = None
    use_proc = os.path.isdir(PROC_ROOT)
    if not use_proc:
        table = _ps_snapshot()
        if table is None:
            print("CHECK FAIL unsupported platform: no /proc and ps is unusable")
            return 3
    try:
        sup_pid = int(read_regular_text(pidfile).strip())
        if sup_pid <= 0:
            raise ValueError(f"non-positive pid {sup_pid}")
    except (OSError, ValueError) as exc:
        kind = kind_of_path(pidfile)
        if kind is not None:
            print(f"CHECK FAIL supervisor not armed: pidfile {pidfile} is a {kind}, "
                  f"not a regular file")
            return 2
        print(f"CHECK FAIL supervisor not armed: pidfile {pidfile} {exc.__class__.__name__}: {exc}")
        return 2

    data, stale, note, children = _load_status(status_file, max_status_age)
    if use_proc:
        results, extras = _evaluate_proc(sup_pid, data, stale, note, children)
    else:
        results, extras = _evaluate_ps(sup_pid, table, data, stale, note, children)
    return _emit_verdict(results, extras)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="monitor-supervisor.py",
        description="Run the three collab-loop pollers (forum updates, forum "
                     "mentions, umwelt bell) as one supervised child set, "
                     "armed as a single Monitor.",
    )
    p.add_argument("--purpose", default="collab-loop",
                    help="Forwarded as --purpose to both forum monitor "
                         "scripts (default: collab-loop).")
    # umwelt bell defaults / overrides
    p.add_argument("--umwelt-agent", default=None)
    p.add_argument("--umwelt-world-url", default=None)
    p.add_argument("--umwelt-cursor-file", default=DEFAULT_UMWELT_CURSOR_FILE)
    p.add_argument("--umwelt-interval", type=float, default=DEFAULT_UMWELT_INTERVAL_SECONDS)
    p.add_argument("--umwelt-config", default=DEFAULT_UMWELT_CONFIG,
                    help="Path to umwelt's config.json, read for 'agent' / "
                         "'world_url' when --umwelt-agent/--umwelt-world-url "
                         "are not given explicitly.")
    p.add_argument("--bell-script", default=None,
                    help=f"Path to umwelt-bell.py (default: {DEFAULT_UMWELT_BELL_SCRIPT}).")
    # forum monitor script overrides (path only -- args are always
    # ["--purpose", PURPOSE] unless a full --*-cmd-json override is given)
    p.add_argument("--updates-script", default=None)
    p.add_argument("--mention-script", default=None)
    # whole-command overrides -- the test suite's fake-child escape hatch
    p.add_argument("--bell-cmd-json", default=None,
                    help="JSON-encoded argv list; overrides the constructed "
                         "umwelt-bell.py invocation entirely.")
    p.add_argument("--updates-cmd-json", default=None,
                    help="JSON-encoded argv list; overrides the constructed "
                         "forum-updates-monitor.sh invocation entirely.")
    p.add_argument("--mention-cmd-json", default=None,
                    help="JSON-encoded argv list; overrides the constructed "
                         "forum-mention-monitor.sh invocation entirely.")
    # supervisor bookkeeping
    p.add_argument("--pidfile", default=DEFAULT_PIDFILE)
    p.add_argument("--status-file", default=DEFAULT_STATUS_FILE)
    p.add_argument("--status-interval", type=float, default=DEFAULT_STATUS_INTERVAL_SECONDS)
    p.add_argument("--check", action="store_true",
                    help="Read-only health check of a running supervisor "
                         "(Linux /proc or macOS ps/lsof; see module docstring). Prints five "
                         "lines; exit 0/1/2/3.")
    p.add_argument("--max-status-age", type=float, default=None,
                    help="--check only: max age of the status file in "
                         "seconds (default: 3x the status_interval the "
                         "supervisor recorded in the file).")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_argparser().parse_args(argv)

    if args.check:
        sys.exit(run_check(args.pidfile, args.status_file, args.max_status_age))

    reap_stale_supervisor(args.pidfile)
    # R5: become our own process-group leader before spawning children -- but
    # only if we aren't one already. A session leader (pid == pgid, e.g. when
    # armed as a single top-level command under `setsid`/Monitor) is BY
    # CONSTRUCTION already its own process-group leader, and POSIX documents
    # setpgid()/setpgrp() as unconditionally raising EPERM when called by a
    # session leader on itself -- regardless of whether the call would
    # actually change anything. Calling it anyway crashes the supervisor at
    # startup in exactly the natural single-command arm shape. Skipping the
    # call in that case is a no-op with respect to R5/R6 (both already hold
    # trivially: pgid == pid), not a weakening of the guarantee.
    if os.getpgrp() != os.getpid():
        os.setpgrp()
    Path(args.pidfile).parent.mkdir(parents=True, exist_ok=True)
    try:
        write_pidfile(args.pidfile, str(os.getpid()).encode("ascii"))
    except NotRegularFile as exc:
        # #3773: refuse by name, exit 4, BEFORE any child is spawned. This
        # process has no restarting unit, so a distinct exit code is safe.
        print(f"monitor-supervisor: refusing to start: the pidfile path {exc}; "
              f"nothing was started. Remove it and start again",
              file=sys.stderr, flush=True)
        sys.exit(EXIT_PIDFILE_NOT_REGULAR)

    cmds = build_child_commands(args)
    supervisor = Supervisor(cmds, args.status_file, args.status_interval, args.pidfile)
    _install_signal_handler(supervisor)
    supervisor.start_all()

    supervisor.shutdown.wait()
    supervisor.teardown()


if __name__ == "__main__":
    main()
