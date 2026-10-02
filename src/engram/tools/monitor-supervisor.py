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

DEFAULT_PIDFILE = str(Path.home() / ".engram" / ".monitor-supervisor.pid")
DEFAULT_STATUS_FILE = str(Path.home() / ".engram" / ".monitor-supervisor-status.json")
DEFAULT_STATUS_INTERVAL_SECONDS = 10.0
DEFAULT_UMWELT_CONFIG = str(Path.home() / ".umwelt" / "config.json")
DEFAULT_UMWELT_CURSOR_FILE = str(Path.home() / ".umwelt" / "bell-cursors.json")
DEFAULT_UMWELT_BELL_SCRIPT = str(
    Path.home() / ".umwelt" / "marketplace" / "plugins" / "umwelt" / "tools" / "umwelt-bell.py"
)
DEFAULT_UMWELT_INTERVAL_SECONDS = 5.0


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
    if not os.path.basename(argv[0]).startswith("python"):
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
        old_pid = int(Path(pidfile_path).read_text().strip())
    except (OSError, ValueError):
        return  # no pidfile, or unreadable -- nothing recorded, nothing to reap
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
        data = {"generated_at": time.time(), "children": {}}
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
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.status_file)

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
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_argparser().parse_args(argv)

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
    Path(args.pidfile).write_text(str(os.getpid()))

    cmds = build_child_commands(args)
    supervisor = Supervisor(cmds, args.status_file, args.status_interval, args.pidfile)
    _install_signal_handler(supervisor)
    supervisor.start_all()

    supervisor.shutdown.wait()
    supervisor.teardown()


if __name__ == "__main__":
    main()
