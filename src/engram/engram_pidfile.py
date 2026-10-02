"""Pidfile liveness with process-identity -- single source for the surface
daemon and the engramd owner (engram-alpha#3240 item 1).

`os.kill(pid, 0)` alone cannot tell the instance that wrote a pidfile from
an unrelated process that later reused its pid: a crashed instance's pidfile
then reads as a live instance and every restart is refused until that
unrelated process exits. Contract: never refuse forever.

Identity rule (Linux): a pid counts as alive only if the process could have
WRITTEN the pidfile -- i.e. it did not start after the pidfile was last
written (mtime). A process that did not exist yet cannot be the writer.

Bounds, stated rather than hidden:
  * /proc unreadable for a pid `os.kill` says is alive (non-Linux, permission,
    race) -> conservative True. We never guess "dead" from missing data.
  * The pidfile format is unchanged (the bare pid), so an old-version
    instance that is genuinely alive (it started before writing its pidfile)
    still reads alive: upgrade/downgrade safe.
  * A reused pid whose new process started within _START_TIME_TOLERANCE_S of
    the pidfile's mtime is indistinguishable from the writer and reads alive.
  * Clock drift: the start time is `btime + starttime/CLK_TCK`, and on a VM
    whose boottime clock stops across a host suspend (WSL2 after sleep + a
    time sync) btime jumps FORWARD, pushing every long-lived process's
    computed start later -- possibly past its own pidfile's mtime. "Started
    after the pidfile" is therefore NOT sufficient to call a pid reused: the
    process's /proc/<pid>/cmdline must ALSO lack the caller's program marker.
    That cmdline guard is what keeps drift from producing a false "dead"
    (which would unlink a LIVE socket -- the #1874 failure). Residual: a
    reused pid whose new process happens to carry the marker in its argv
    reads alive (the conservative direction). Callers that pass no marker
    keep the start-time-only rule.
  * That residual is not only theoretical: a short-lived process that
    carries the marker in its argv (e.g. `pytest tests/test_engramd_owner.py`,
    `vim engramd_owner.py`, `tail engramd_owner.log`) at a reused pid reads
    alive until it exits -- a restart is refused for that long, then succeeds.
  * The marker must appear in the REAL launch argv. A future launcher whose
    argv lacks it (a console script named `engramd`, say) silently disables
    the drift guard, and clock drift can then read a live instance as dead.
"""

from __future__ import annotations

import os
from pathlib import Path

# Clock skew / tick-rounding slack between the pidfile's mtime and the
# process start time derived from /proc (btime has 1 s resolution, starttime
# has 1/CLK_TCK). A process started more than this after the pidfile's last
# write cannot be its writer.
_START_TIME_TOLERANCE_S = 2.0


def _parse_starttime_ticks(stat_text: str) -> int:
    """Field 22 (starttime, in clock ticks since boot) of /proc/<pid>/stat.

    Parses AFTER the LAST ')' -- the comm field (2) may itself contain
    spaces and parentheses. Fields after the ')' begin at field 3, so
    field 22 is index 19. Raises ValueError/IndexError on malformed input.
    """
    after_comm = stat_text[stat_text.rindex(")") + 1 :].split()
    return int(after_comm[19])


def _read_process_start_time(pid: int) -> float | None:
    """Wall-clock start time of `pid`, or None if it cannot be determined
    (non-Linux, /proc unreadable, malformed, process vanished)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            ticks = _parse_starttime_ticks(f.read())
        btime = None
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    btime = int(line.split()[1])
                    break
        if btime is None:
            return None
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def _started_after_pidfile(start_time: float, pidfile_mtime: float) -> bool:
    """True if a process with this start time began too long after the
    pidfile's last write to have written it."""
    return start_time > pidfile_mtime + _START_TIME_TOLERANCE_S


def _read_process_cmdline(pid: int) -> str | None:
    """/proc/<pid>/cmdline (NUL-separated argv) joined with spaces, or None
    if it cannot be read (non-Linux, permission, process vanished)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def recorded_pid_is_alive(pid_path, program_marker: str | None = None) -> bool:
    """True iff `pid_path` names a live process that could have written it.

    False for: missing/unreadable/garbage pidfile; a non-positive pid (0, -N:
    not a process, and `os.kill` would "succeed" on a group); a pid `os.kill(pid, 0)`
    says is not signalable (dead, or any other OSError); or (Linux) a process
    that started after the pidfile was last written. See the module
    docstring for the conservative-True bound when /proc is unreadable.

    `program_marker` (a substring of the owning program's argv): a process
    that started after the pidfile counts as a REUSED pid only if its
    cmdline does not contain the marker (so clock drift cannot make a live
    instance read dead); unreadable cmdline -> True. None -> start-time-only.
    """
    try:
        with open(pid_path) as f:
            pid = int(f.read().strip())
        mtime = os.stat(pid_path).st_mtime
    except (OSError, ValueError):
        return False
    if pid <= 0:
        # os.kill(0, 0) / os.kill(-1, 0) signal a process group / every
        # signalable process and succeed, and /proc/0|-1 is unreadable, so
        # they would read alive forever. Not a pid. (pid 1 is NOT special-
        # cased: in a container the owner can legitimately be pid 1.)
        return False
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        # ProcessLookupError (dead) or e.g. EPERM on a reused pid owned by
        # someone else -- not confirmably ours, same as dead (today's rule).
        # An out-of-range pid (OverflowError from os.kill) is a garbage
        # pidfile, not ours.
        return False
    start_time = _read_process_start_time(pid)
    if start_time is None:
        return True
    if not _started_after_pidfile(start_time, mtime):
        return True
    if program_marker is None:
        return False
    cmdline = _read_process_cmdline(pid)
    if cmdline is None:
        return True  # cannot tell reuse from drift -- never guess dead
    return program_marker in cmdline
