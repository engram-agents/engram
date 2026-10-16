"""Pidfile liveness with process-identity -- single source for the surface
daemon and the engramd owner (engram-alpha#3240 item 1).

`os.kill(pid, 0)` alone cannot tell the instance that wrote a pidfile from
an unrelated process that later reused its pid: a crashed instance's pidfile
then reads as a live instance and every restart is refused until that
unrelated process exits. Contract: never refuse forever.

Identity rule (Linux): a pid counts as alive only if the process could have
WRITTEN the pidfile -- i.e. it did not start after the pidfile was last
written (mtime). A process that did not exist yet cannot be the writer.

Identity rule (Darwin, #3714): same rule, different primitives. The start
time comes from libproc `proc_pidinfo(PROC_PIDTBSDINFO)` (pbi_start_tvsec /
pbi_start_tvusec) and argv from `sysctl(CTL_KERN, KERN_PROCARGS2)`. libproc
and libc are loaded lazily via ctypes, only on Darwin and only when a start
time is needed, so importing this module stays cheap and side-effect free.
The `struct proc_bsdinfo` size (136 bytes) and its pbi_pid are checked on
every read; a mismatch is "cannot read" (conservative True), never parsed.
An empty argv[0] is also "cannot read": it is indistinguishable from a
future, wider padding layout, so it is never parsed.
ESRCH after a successful `os.kill(pid, 0)` means the process died in
between: that is dead (False), unlike "cannot read".

Bounds, stated rather than hidden:
  * Start time or argv unreadable for a pid `os.kill` says is alive
    (permission, a cross-uid KERN_PROCARGS2 EINVAL, a libproc size mismatch,
    libproc missing, a platform with neither /proc nor the Darwin arm, race)
    -> conservative True. We never guess "dead" from missing data. The one
    exception is Darwin ESRCH (above): the process is gone.
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

import errno
import logging
import os
import platform
import struct
from pathlib import Path

from engram_regular_io import open_regular_text

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


def platform_arm() -> str | None:
    """Which platform arm this seat is on (#3712, moved here by #3707): "linux",
    "darwin", or None for any other platform. Keyed on `platform.system()`
    ALONE -- the single source of the platform decision. engramd_client
    delegates to it (the client imports this module, never the reverse), and
    the owner's `_owner_arm()` delegates to the client."""
    system = platform.system()
    if system == "Linux":
        return "linux"
    if system == "Darwin":
        return "darwin"
    return None


def _is_darwin() -> bool:
    """`platform_arm() == "darwin"`: this module is the source of the platform
    discriminator, so there is no import cycle to avoid any more."""
    return platform_arm() == "darwin"


def _read_process_start_time(pid: int) -> float | None:
    """Wall-clock start time of `pid`, or None if it cannot be determined
    (non-Linux, /proc unreadable, malformed, process vanished)."""
    if _is_darwin():  # #3714: Darwin arm, defined below
        return _read_process_start_time_darwin(pid)
    # The broad except below is also why the ProcessLookupError clause in
    # recorded_live_pid() is inert on Linux: a vanished process lands here
    # as None, never as an exception (see the comment at that clause).
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
    if _is_darwin():  # #3714: Darwin arm, defined below
        return _read_process_cmdline_darwin(pid)
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


# --- Darwin arm (#3714) ---------------------------------------------------
# Everything here is additive: the Linux readers above are untouched and stay
# the only readers on Linux; _read_process_start_time / _read_process_cmdline
# dispatch to the Darwin readers below with an early platform check.

_log = logging.getLogger(__name__)

_PROC_PIDTBSDINFO = 3
_PROC_BSDINFO_SIZE = 136  # sizeof(struct proc_bsdinfo), measured macOS 26.6.2
_PBI_PID_OFFSET = 12  # uint32 pbi_pid
_PBI_START_OFFSET = 120  # uint64 pbi_start_tvsec, then uint64 pbi_start_tvusec
_CTL_KERN, _KERN_ARGMAX, _KERN_PROCARGS2 = 1, 8, 49

# Unlocked module globals on purpose: the races are benign (worst case is a
# duplicate library load or a second warning).
_darwin_libs_cache = None
_size_warned = False


def _darwin_libs():
    """(libproc, libc) via ctypes, loaded on first use. Darwin only."""
    global _darwin_libs_cache
    if _darwin_libs_cache is None:
        import ctypes

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libc = ctypes.CDLL(None, use_errno=True)
        # int proc_pidinfo(int pid, int flavor, uint64_t arg, void *buffer, int buffersize)
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
        ]
        libproc.proc_pidinfo.restype = ctypes.c_int
        # int sysctl(int *name, u_int namelen, void *oldp, size_t *oldlenp,
        #            void *newp, size_t newlen)
        libc.sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t,
        ]
        libc.sysctl.restype = ctypes.c_int
        _darwin_libs_cache = (libproc, libc)
    return _darwin_libs_cache


def _darwin_proc_pidinfo(pid: int):
    """Raw proc_pidinfo(PROC_PIDTBSDINFO) -> (nbytes, errno, buf bytes).
    Raises OSError if libproc cannot be loaded."""
    import ctypes

    libproc, _ = _darwin_libs()
    buf = ctypes.create_string_buffer(_PROC_BSDINFO_SIZE)
    # Must run BEFORE proc_pidinfo: a stale errno left by an earlier call, read
    # afterwards as ESRCH, would turn a live owner into a dead one -- the one
    # fail-open direction this module guards.
    ctypes.set_errno(0)
    n = libproc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, buf, _PROC_BSDINFO_SIZE)
    return n, ctypes.get_errno(), buf.raw


def _darwin_procargs2(pid: int):
    """Raw sysctl(CTL_KERN, KERN_PROCARGS2, pid) -> (bytes | None, errno).
    Raises OSError if libc cannot be loaded."""
    import ctypes

    _, libc = _darwin_libs()
    ctypes.set_errno(0)
    argmax = ctypes.c_int(0)
    sz = ctypes.c_size_t(ctypes.sizeof(argmax))
    mib2 = (ctypes.c_int * 2)(_CTL_KERN, _KERN_ARGMAX)
    if libc.sysctl(mib2, 2, ctypes.byref(argmax), ctypes.byref(sz), None, 0) != 0:
        return None, ctypes.get_errno()
    buf = ctypes.create_string_buffer(argmax.value)
    size = ctypes.c_size_t(argmax.value)
    mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None, ctypes.get_errno()
    return buf.raw[: size.value], 0


def _read_process_start_time_darwin(pid: int) -> float | None:
    """Wall-clock start time of `pid` via libproc, or None if it cannot be
    determined (libproc missing, size/pid mismatch, EPERM, ...). Raises
    ProcessLookupError on ESRCH: the process is gone (dead, not unreadable)."""
    global _size_warned
    try:
        n, err, raw = _darwin_proc_pidinfo(pid)
    except (OSError, AttributeError):
        return None
    if n == 0 and err == errno.ESRCH:
        raise ProcessLookupError(errno.ESRCH, "process gone", pid)
    if n > 0 and n != _PROC_BSDINFO_SIZE:
        # A positive, wrong size is a real layout signal. n == 0 with an errno
        # other than ESRCH (plain EPERM, ...) is just "unreadable": no warning.
        if not _size_warned:
            _size_warned = True
            _log.warning(
                "proc_pidinfo(PROC_PIDTBSDINFO) returned %s bytes (errno %s), expected %s; "
                "not parsing, treating the start time as unreadable",
                n, err, _PROC_BSDINFO_SIZE,
            )
        return None
    if n != _PROC_BSDINFO_SIZE or len(raw) < _PROC_BSDINFO_SIZE:
        return None
    if struct.unpack_from("I", raw, _PBI_PID_OFFSET)[0] != pid:
        return None
    sec, usec = struct.unpack_from("QQ", raw, _PBI_START_OFFSET)
    return sec + usec / 1e6


def _read_process_cmdline_darwin(pid: int) -> str | None:
    """argv of `pid` joined with spaces (the shape _read_process_cmdline
    returns on Linux), or None if unreadable (cross-uid EINVAL, vanished,
    libc missing, malformed).

    KERN_PROCARGS2 layout: int32 argc, the exec path, NUL padding, then argc
    argv strings, then the environment. The padding is not a blind run of
    NULs: measured on macOS 26.6.2 across exec-path lengths of every residue
    mod 8, argv[0] starts at the next multiple of 8 after the exec path's
    terminating NUL, counted from raw[4:] (granularity 8, not 16). We compute
    that offset instead of stripping NULs. If the padding bytes are not all
    NUL, fewer than argc terminated strings follow, or argv[0] itself is
    EMPTY, the layout is not what we measured: return None (unreadable ->
    conservative True), never guess. An empty argv[0] is therefore
    "unreadable", not parsed (see the comment at the check)."""
    try:
        raw, _err = _darwin_procargs2(pid)
    except (OSError, AttributeError):
        return None
    if raw is None or len(raw) < 4:
        return None
    try:
        argc = struct.unpack_from("i", raw, 0)[0]
        rest = raw[4:]
        path_end = rest.index(b"\0")
    except (struct.error, ValueError):
        return None
    if argc < 0:
        return None
    start = -(-(path_end + 1) // 8) * 8  # next multiple of 8 after path + its NUL
    if start > len(rest) or rest[path_end:start].strip(b"\0"):
        return None
    parts = rest[start:].split(b"\0")
    if len(parts) <= argc:  # the argc-th string must itself be NUL-terminated
        return None
    # Layout-drift guard. Padding WIDER than the next-multiple-of-8 rule puts a
    # NUL at the computed start, so it always surfaces as an empty argv[0]
    # (and would shift the parse, dropping the last argument from parts[:argc]);
    # NARROWER padding puts non-NUL bytes inside the computed padding region,
    # which the check above already rejects. So every drift lands on None. The
    # cost: a real process with an empty argv[0] reads as unreadable, the same
    # conservative outcome as a cross-uid EINVAL.
    if not parts[0]:
        return None
    return b" ".join(parts[:argc]).decode("utf-8", "replace")


def recorded_pid_is_alive(pid_path, program_marker: str | None = None) -> bool:
    """True iff `pid_path` names a live process that could have written it.
    The bool form of `recorded_live_pid` (same logic, run once there)."""
    return recorded_live_pid(pid_path, program_marker) is not None


def recorded_live_pid(
    pid_path, program_marker: str | None = None, *, require_marker: bool = False
) -> int | None:
    """The pid `pid_path` names when it is a live process that could have
    written it, else None (#3707). One read of the pidfile: a caller that needs
    the pid must not read it separately from the liveness check, or the two
    could disagree. `recorded_pid_is_alive` is `... is not None`.

    None (False in the bool form) for: missing/unreadable/garbage pidfile; a non-positive pid (0, -N:
    not a process, and `os.kill` would "succeed" on a group); a pid `os.kill(pid, 0)`
    says is not signalable (dead, or any other OSError); or (Linux) a process
    that started after the pidfile was last written (Linux via /proc,
    Darwin via libproc; a Darwin ESRCH after a successful kill is dead too).
    See the module docstring for the conservative-True bound when the start
    time is unreadable.

    A pidfile path that is not a regular file (FIFO, socket, device,
    directory) is "no live pid", answered without blocking (#3768).

    `program_marker` (a substring of the owning program's argv): a process
    that started after the pidfile counts as a REUSED pid only if its
    cmdline does not contain the marker (so clock drift cannot make a live
    instance read dead); unreadable cmdline -> True. None -> start-time-only.

    `require_marker=True` (#3709) is the STRICT mode, named so a caller opts in
    and the default rule stays exactly what every existing caller relies on.
    The marker is then required of ANY live pid, whatever its start time: the
    pid reads alive only if its cmdline contains `program_marker` (unreadable
    cmdline -> still alive, never guess dead), and the start-time comparison
    is not consulted. The default rule consults the marker only for a process
    that started after the pidfile, so a live non-owner OLDER than the pidfile
    reads alive there; that shape needs no malice (a plain `cp` of a home
    rewrites the pidfile's mtime), and a caller that must not mistake such a
    process for its owner (the SessionStart hook, which would otherwise
    never launch the owner) passes `require_marker=True`. The one start-time
    read still made is the Darwin ESRCH check (the process exited between
    `os.kill` and the read: gone, not unreadable). Requires `program_marker`
    (ValueError otherwise: a strict check against no marker is meaningless).
    """
    if require_marker and not program_marker:
        raise ValueError("require_marker=True needs a program_marker")
    try:
        with open_regular_text(pid_path) as f:
            pid = int(f.read().strip())
        mtime = os.stat(pid_path).st_mtime
    except OSError as exc:
        if (
            exc.errno in (errno.EINVAL, errno.EISDIR)
            and "not a regular file" in str(exc)
        ):
            _log.warning(
                "pidfile %s is not a regular file; treating as no live pid",
                pid_path,
            )
        return None
    except ValueError:
        return None
    if pid <= 0:
        # os.kill(0, 0) / os.kill(-1, 0) signal a process group / every
        # signalable process and succeed, and /proc/0|-1 is unreadable, so
        # they would read alive forever. Not a pid. (pid 1 is NOT special-
        # cased: in a container the owner can legitimately be pid 1.)
        return None
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        # ProcessLookupError (dead) or e.g. EPERM on a reused pid owned by
        # someone else -- not confirmably ours, same as dead (today's rule).
        # An out-of-range pid (OverflowError from os.kill) is a garbage
        # pidfile, not ours.
        return None
    try:
        start_time = _read_process_start_time(pid)
    except ProcessLookupError:
        # Not platform-gated, but only the Darwin reader raises it: kill(pid, 0)
        # succeeded, then libproc says ESRCH -- the process exited in between.
        # Gone, not unreadable. On Linux this clause is inert, because the
        # Linux reader's broader `except (OSError, ...)` already returns None
        # for a vanished process. If that clause is ever narrowed, a
        # ProcessLookupError escaping it is still ESRCH (no such process), so
        # reading it as dead here stays correct. The two clauses go together.
        return None
    if require_marker:
        # Strict (#3709): the marker decides, whatever the start time said.
        cmdline = _read_process_cmdline(pid)
        if cmdline is None:
            return pid  # cannot tell reuse from a live owner -- never guess dead
        return pid if program_marker in cmdline else None
    if start_time is None:
        return pid
    if not _started_after_pidfile(start_time, mtime):
        return pid
    if program_marker is None:
        return None
    cmdline = _read_process_cmdline(pid)
    if cmdline is None:
        return pid  # cannot tell reuse from drift -- never guess dead
    return pid if program_marker in cmdline else None
