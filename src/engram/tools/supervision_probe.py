#!/usr/bin/env python3
"""Fleet-wide per-seat supervision probe (#2749, part of #2652).

Turns #2652's phase-0 acceptance criteria into one runnable, per-seat check:
"is the MCP server / surface daemon actually decoupled from this agent's
interactive session" as a MEASUREMENT, not a belief. Design of record is
Borges's 2026-09-06 18:34Z decomposition comment on #2652, Ariadne's 18:35Z
correction, and Borges's 18:39Z amendment (issue #2749 body quotes all
three verbatim) -- every rule below is lifted from there, not re-derived.

## What this checks, per seat, for BOTH the surface daemon and the MCP server

    1  start time consistent with the supervisor's own record   necessary, NOT sufficient
    2  owner NOT in any agent-session@* cgroup (Linux) /
       registered under its own LaunchAgent, not the session (Darwin)
                                                                  the property phase 0 exists for

Criterion 1 alone cannot certify decoupling: a daemon reparented to init
shows `ppid 1`, looks independent to `ps`/`pgrep`, and still dies with its
session slice (`KillMode=control-group` on Linux; no equivalent guarantee on
Darwin either -- see the Darwin section below). Criterion 2 is the actual
property phase 0 exists to establish.

## Usage

    ~/.engram/venv/bin/python3 tools/supervision_probe.py [--engram-home PATH]

One line per (seat, process) on stdout:

    <seat> <process> pid=<n> cgroup=<PASS|FAIL|FAIL:<why>|ERROR:<why>> start=<UNCHANGED|MOVED|UNKNOWN> supervisor=<unit-or-label|none>

`:<why>` follows whenever a FAIL or ERROR verdict carries a reason (a
diagnostic tool that discards its own reason string defeats its own purpose
-- reviewer-fairy finding on this PR, folded in after an earlier draft
special-cased only the zero-matches `FAIL:not-found` line). PASS never
carries a suffix; a bare `FAIL` with no reason means the pgrep-resolved cgroup
marker check ran and matched (no further explanation needed beyond the
verdict itself).

Exit code: 0 unless a line's `cgroup=` field reads FAIL or ERROR; 1 if it
does (so a fleet sweep can `grep -c` and count without parsing every line).
Only criterion 2 (`cgroup=`) gates the exit code -- criterion 1 (`start=`) is
"necessary, NOT sufficient" per the design of record, and #2749's own exit
rule names only FAIL/ERROR, not MOVED; a `start=MOVED` line with
`cgroup=PASS` is intentionally still exit 0 (reviewer-fairy finding on this
PR, confirmed by `test_moved_start_does_not_gate_exit_code` below).

## Pinned rules this implementation obeys (each cost a wrong answer on 09-06)

- Resolve the pid AT TEST TIME via a scoped `pgrep -f`; never cite a literal
  pid (the spec's own worked example decayed within the thread's lifetime).
  Zero matches -> FAIL (the process is not independently discoverable by its
  own launch identity, not a probe error -- see `_reap_pattern` in
  start-engram-daemon.sh: the daemon's own ENGRAM_HOME trailing argv is what
  makes it findable; a seat whose launch line lacks it genuinely fails this
  bar). Many matches -> ERROR (ambiguous; the probe cannot know which one to
  read, and guessing would silently reintroduce the "cite a literal pid"
  failure this rule exists to prevent).
- Read the cgroup, never the ppid (Linux).
- `grep` exit code discipline for `grep -q 'agent-session@' /proc/<pid>/cgroup`:
  exit 1 = PASS, exit 0 = FAIL, exit 2 = ERROR (unreadable / dead pid). The
  naive `!`/`|| pass` idiom collapses 1 and 2 -- a dead pid must never read
  as a pass. Cross-checked against Ariadne's own control pair
  (`/proc/999999/cgroup` -> 2, `/proc/self/cgroup` -> 0) via this module's
  test suite, run under a synthetic proc root (the same test-injection
  SPIRIT `tools/_proc_identity.py` established via its `ENGRAM_PROC_DIR` env
  var -- this module's own mechanism differs in form, an explicit
  `proc_root` parameter surfaced via a hidden `--proc-root` flag, since a
  standalone script's `main()` already has a natural place to thread an
  override through call sites rather than reading the environment inside
  each function).
- ANSWERS the EMPTY half of Ariadne's open question ("can `/proc/<pid>/cgroup`
  read as empty or truncated -- exit 1 from a file that answered nothing?"):
  yes, a zero-byte cgroup file gives `grep` the same exit 1 as a
  genuinely-clean, non-empty file with no match -- those are NOT the same
  claim ("verified not in that slice" vs "nothing was there to check").
  Guarded explicitly: an empty (or unreadable-for-size) cgroup file
  downgrades the verdict to ERROR before `grep` ever runs, so a dead
  instrument cannot silently read as PASS. See `_read_cgroup_verdict` below.
  The TRUNCATED half (non-empty but partial/garbage content) is NOT
  separately guarded -- reviewer-fairy finding on this PR (2026-09-09): a
  truncated-but-non-empty read still reaches the real `grep` call and could
  in principle read PASS on partial content. Left unguarded because procfs
  pseudo-files are generated atomically per read (the kernel does not hand
  back a partial write mid-generation the way a real file can), so this is
  believed low-probability rather than closed by construction; named here as
  a real gap rather than silently narrowing the claim without saying so.
- Timing: `/proc/<pid>/stat` field 22 (process start time, in clock ticks
  since boot -- see `man 5 proc`) against `systemctl --user show -p
  ActiveEnterTimestampMonotonic` (microseconds since boot). Both are
  boot-relative on purpose. Never `/proc/stat` btime -- it drifted 30s
  between two reads on the host that found this bug.
- Any `systemctl --user ... | grep`: this module never pipes systemctl
  output through grep at all -- it captures stdout, checks the exit code
  first, and only then parses the captured text -- so there is no row-count
  ambiguity to report (a dead bus and an empty system cannot look alike when
  the exit code is checked before the text is trusted).
- Tests run under `~/.engram/venv/bin/python3` (this repo's `fastmcp`-bearing
  venv), never system python3.

## Timing semantics -- a design decision named explicitly (open question)

The spec pins WHICH TWO NUMBERS to compare; it does not spell out the exact
comparison this probe should perform, and no persisted cross-run baseline is
named anywhere in the design of record. Read literally, "one command per
seat" argues against inventing a new state file this issue never mentions.
So this implementation treats the two timestamps as a SINGLE-SHOT
CONSISTENCY CHECK, not a before/after diff: if a supervisor unit is
registered and active, its `ActiveEnterTimestampMonotonic` is compared
against the resolved pid's own kernel-reported start time (both converted to
seconds since boot); the two should closely agree if the observed pid really
is the instance the supervisor believes it launched. `UNCHANGED` = the two
agree within a tolerance (`_START_TOLERANCE_SECONDS`, currently 5s -- service
startup overhead, not a load-bearing constant); `MOVED` = they disagree
beyond that, meaning the pid answering today does not match what the
supervisor's own activation record would predict; `UNKNOWN` = no supervisor
record exists to compare against at all (no unit, unit not active, Darwin,
or the read itself failed) -- necessarily the common case on any seat this
epic has not yet reached. This is flagged in the PR description as a
judgment call for the colleague pass, not asserted as the design's own
intent. Kepler's read on #2758 (2026-09-09 12:49Z, forum #405): "your
reading stands... a single-shot check answers the question phase 0 actually
cares about (is the running daemon the one the supervisor started, or a
hook relaunch) without inventing a state file" -- with one required addition,
folded in below.

**What this check CANNOT see (name the reach, don't just name the result):**
`UNCHANGED` is NOT "this process has never restarted" -- a supervisor-managed
restart (crash + `Restart=always`, or a manual `systemctl restart`) updates
BOTH `ActiveEnterTimestampMonotonic` and the new process's own kernel start
time together, so a perfectly legitimate restart still reads `UNCHANGED`
here; this check certifies "the pid answering today is the one the
supervisor's own record says it launched," not "nothing has ever happened
to this process." And `UNKNOWN` is not evidence of a problem: a
hook-launched daemon running ALONGSIDE a registered-but-unrelated unit (the
UNSUPERVISED reap+relaunch path `start-engram-daemon.sh` still falls back to
on a seat with no supervisor installed) has no unit record to compare
against and correctly reads `UNKNOWN`, not `FAIL` -- `UNKNOWN` here means
"this check has no opinion," never "this check flagged something."

## Darwin equivalent (macOS has no cgroups)

Named here, as the issue requires, rather than faked with a ppid read.
`launchctl print gui/<uid>/<label>` is checked for a well-known label
(`com.engram.surface-daemon` for the daemon; this repo does not yet define
one for the MCP server, since no LaunchAgent template exists for it --
`_MCP_SERVER_LAUNCHD_LABEL` below is a placeholder pending that follow-up).
PASS requires the label to be registered AND the pid it reports to match the
resolved pid; anything else -- no registration, a registration for a
DIFFERENT pid, or a launchctl read failure -- is FAIL, mirroring the same
"reparented-to-init looks independent but isn't actually decoupled" trap
Linux's cgroup check exists to catch (a Darwin process reparented to
launchd's pid 1 on session exit looks ownerless to `ps`/`pgrep` regardless of
whether any LaunchAgent job was ever registered for it). `start=` stays
UNKNOWN on Darwin -- launchd's `state`/`runs` fields are not a boot-relative
monotonic timestamp comparable to `/proc/<pid>/stat` field 22, and inventing
a second timing heuristic for one platform is out of scope here.

Live-verified worked FAIL on this probe's own author's seat (Darwin,
2026-09-09): the surface daemon (`ppid=1`, reparented after its
SessionStart-hook-launched parent shell exited) has NO real LaunchAgent
registered on this host at all -- `launchctl print gui/<uid>/` finds only a
stale test-fixture registration pointing at a since-deleted worktree tmp
path, `active count = 0`. Both processes on this seat therefore report
`cgroup=FAIL` today, correctly: this seat is genuinely unsupervised, exactly
as `start-engram-daemon.sh`'s own header comment already documents ("today
several seats on a given host have NO supervisor unit registered at all").
No live Darwin PASS case exists yet on this fleet to test the positive arm
against -- named as a gap for whoever next runs `setup-surface-daemon-service.sh`
for real.

## Tier

`dev` (this repo's `src/build/packaging/tiers.json` convention) -- a
diagnostic/verification tool for confirming #2652's own deployment state,
same class as `tools/os-harness-check.py`; not something an end user's
install needs.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional

_START_TOLERANCE_SECONDS = 5.0
_SESSION_CGROUP_MARKER = "agent-session@"
_DAEMON_LAUNCHD_LABEL = "com.engram.surface-daemon"
# Placeholder: no LaunchAgent template exists for the MCP server yet (only
# the surface daemon and the viz server have one in this repo today). Left
# named, not silently omitted, so a future template lands with an obvious
# place to wire it in.
_MCP_SERVER_LAUNCHD_LABEL: Optional[str] = None
_DAEMON_SYSTEMD_UNIT = "engram-surface-daemon.service"
# Same placeholder reasoning as the launchd label above.
_MCP_SERVER_SYSTEMD_UNIT: Optional[str] = None


# ---------------------------------------------------------------------------
# seat identity
# ---------------------------------------------------------------------------


def _load_config(engram_home: Path) -> dict:
    try:
        return json.loads((engram_home / "config.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def seat_name(engram_home: Path) -> str:
    """Resolve this seat's agent name.

    Mirrors `tools/forum.py`'s `_get_agent_name` priority order exactly
    (config.json["agent_name"] -> $USER -> $LOGNAME -> pw_name, each with
    the "agent-" prefix stripped) -- reimplemented locally rather than
    imported, so this standalone diagnostic script does not pull in
    forum.py's network-client dependencies for a one-line lookup.
    """
    config = _load_config(engram_home)
    name = str(config.get("agent_name", "")).strip()
    if name:
        return name

    def _strip_agent_prefix(username: str) -> str:
        return username[len("agent-"):] if username.startswith("agent-") else username

    for envvar in ("USER", "LOGNAME"):
        username = os.environ.get(envvar, "").strip()
        if username:
            return _strip_agent_prefix(username)

    try:
        username = pwd.getpwuid(os.getuid()).pw_name
        if username:
            return _strip_agent_prefix(username)
    except KeyError:
        pass

    return "unknown-seat"


# ---------------------------------------------------------------------------
# process path resolution (mirrors hooks/claude/_hooklib.py's pattern)
# ---------------------------------------------------------------------------


def resolve_own_server_py_path(engram_home: Path) -> str:
    """Resolve THIS agent's own MCP server.py, for a scoped `pgrep -f` match.

    `tools/` sits at the same nesting depth in both the source tree
    (`src/engram/tools/`) and the deployed plugin tree (`<plugin>/tools/`) --
    unlike `hooks/claude/*` -> `hooks/*`, the build does not flatten
    `tools/` (verified against the live deployed tree, not assumed from
    `src/`, per this project's CLAUDE.md plugin-restructure warning).  So
    `server.py`'s sibling relationship to `tools/` (`dirname(dirname(this
    file))/server.py`) holds in both trees, mirroring
    `_hooklib.resolve_own_server_py_path`'s own reasoning for `hooks/`.
    """
    here = Path(__file__).resolve()
    runtime_dir = here.parent.parent
    colocated = runtime_dir / "server.py"
    if colocated.exists():
        return str(colocated)
    candidate = engram_home / "server.py"
    if candidate.exists():
        return str(candidate)
    return str(Path("~/engram-alpha/server.py").expanduser())


def resolve_daemon_script_path(engram_home: Path) -> str:
    """Resolve the surface daemon script, for a scoped `pgrep -f` match.

    Checks both the deployed (flattened `hooks/`) and source
    (`hooks/claude/`) layouts, in that order, mirroring the walk-parents
    discipline this project's CLAUDE.md prescribes rather than assuming
    either layout.
    """
    here = Path(__file__).resolve()
    runtime_dir = here.parent.parent
    for candidate in (
        runtime_dir / "hooks" / "engram-surface-daemon.py",
        runtime_dir / "hooks" / "claude" / "engram-surface-daemon.py",
    ):
        if candidate.exists():
            return str(candidate)
    candidate = engram_home / "engram-surface-daemon.py"
    if candidate.exists():
        return str(candidate)
    return str(runtime_dir / "hooks" / "engram-surface-daemon.py")


# ---------------------------------------------------------------------------
# pid resolution -- resolve at test time, fail loudly on zero-or-many
# ---------------------------------------------------------------------------


class PidResolution(NamedTuple):
    status: str  # "found" | "not-found" | "ambiguous"
    pid: Optional[str]  # set only when status == "found"
    count: int  # number of matches (0, 1, or N)


def _pgrep_matches(pattern: str) -> list[str]:
    """`pgrep -f <pattern>`, returning matched pids. Never raises.

    pgrep's own exit codes: 0 = matches found, 1 = no matches, >1 = pgrep
    itself errored (bad option, unreadable /proc, etc.) -- treated the same
    as "no matches" here (a >1 exit means pgrep could not even attempt the
    search, which is a "not found by this probe" outcome, not a distinct
    third case worth its own branch for this narrow use).
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def resolve_pid(pattern: str) -> PidResolution:
    pids = _pgrep_matches(pattern)
    if len(pids) == 1:
        return PidResolution("found", pids[0], 1)
    if len(pids) == 0:
        return PidResolution("not-found", None, 0)
    return PidResolution("ambiguous", None, len(pids))


# ---------------------------------------------------------------------------
# criterion 2: cgroup (Linux) / launchd registration (Darwin)
# ---------------------------------------------------------------------------


class Verdict(NamedTuple):
    status: str  # "PASS" | "FAIL" | "ERROR"
    why: str  # empty for PASS/FAIL; a short reason for ERROR


def _read_cgroup_verdict(pid: str, proc_root: str = "/proc") -> Verdict:
    """Linux cgroup check -- exact `grep` exit-code discipline from the spec.

    Test-injectable via the `proc_root` parameter (same test-injection
    SPIRIT `tools/_proc_identity.py`'s `ENGRAM_PROC_DIR` env var established,
    a different mechanism -- see the module docstring), so tests can point
    this at a synthetic fixture instead of a real systemd session slice.
    """
    cgroup_path = f"{proc_root}/{pid}/cgroup"
    # #2790: procfs pseudo-files always report st_size == 0 via stat(),
    # regardless of actual content -- os.path.getsize() (which is stat()-
    # backed) is therefore never a valid emptiness check here. Read the
    # actual content and gate on THAT instead; grep below already reads
    # the real bytes directly and was never affected by this. Binary mode
    # (matching `_proc_identity.py`'s convention for reading procfs
    # pseudo-files) so no decode step exists to raise on odd bytes -- an
    # emptiness check needs only the byte count, never the text.
    try:
        with open(cgroup_path, "rb") as f:
            content = f.read()
    except OSError as e:
        return Verdict("ERROR", f"unreadable ({e.__class__.__name__})")
    if not content:
        # Answers Ariadne's named open question: an empty file gives grep
        # the same exit 1 as a genuine non-match -- do not let that read as
        # PASS. A dead instrument must never read as a clean answer.
        return Verdict("ERROR", "empty cgroup file (nothing to verify against)")

    try:
        proc = subprocess.run(
            ["grep", "-q", _SESSION_CGROUP_MARKER, cgroup_path],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return Verdict("ERROR", f"grep failed ({e.__class__.__name__})")

    if proc.returncode == 1:
        return Verdict("PASS", "")
    if proc.returncode == 0:
        return Verdict("FAIL", "")
    return Verdict("ERROR", f"grep exit {proc.returncode} (unreadable/dead pid)")


def _launchctl_print(label: str) -> Optional[str]:
    """`launchctl print gui/<uid>/<label>`, or None on any failure."""
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


class LaunchdState(NamedTuple):
    registered: bool  # a job by this label exists at all (any state)
    pid: Optional[str]  # the "pid = <n>" line's value, if the job is live


def _launchd_state(label: str) -> LaunchdState:
    """Parse `launchctl print gui/<uid>/<label>` into (registered, live-pid).

    `launchctl print` does not have a stable machine-readable format, so
    this is a best-effort line scan -- guarded: any parse failure degrades
    to "not registered" (registered=False) rather than raising. A
    nonexistent label makes the whole `launchctl print` call itself exit
    nonzero (verified live: "Could not find service ... in domain", rc=113
    on this seat), which `_launchctl_print` already turns into `None` --
    so `output is None` IS the "not registered at all" case; a present-but-
    no-"pid ="-line output (a registered job with no live process, e.g.
    `state = spawn scheduled` / `active count = 0`, verified live on this
    seat's own stale test-fixture registration) is `registered=True,
    pid=None` -- a DIFFERENT claim than "not registered", and callers must
    not collapse the two.
    """
    output = _launchctl_print(label)
    if output is None:
        return LaunchdState(registered=False, pid=None)
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            candidate = line[len("pid = "):].strip()
            if candidate.isdigit():
                return LaunchdState(registered=True, pid=candidate)
    return LaunchdState(registered=True, pid=None)


def _read_launchd_verdict(pid: str, label: Optional[str], state: Optional[LaunchdState]) -> Verdict:
    """Darwin equivalent of `_read_cgroup_verdict` -- see module docstring.

    `state` is the caller's own single `_launchd_state(label)` read (or None
    when `label` is None) -- passed in rather than re-fetched here, so a
    caller that also needs the registration state for the `supervisor` field
    (see `probe_one`) makes exactly one `launchctl print` call, not two.
    """
    if label is None:
        return Verdict("FAIL", "no LaunchAgent label defined for this process yet")
    assert state is not None
    if not state.registered:
        return Verdict("FAIL", f"not registered under {label}")
    if state.pid is None:
        return Verdict("FAIL", f"registered under {label} but no live pid (not currently running under it)")
    if state.pid != pid:
        return Verdict("FAIL", f"{label} reports pid={state.pid}, resolved pid is {pid}")
    return Verdict("PASS", "")


# ---------------------------------------------------------------------------
# criterion 1: start-time consistency (Linux only -- see module docstring)
# ---------------------------------------------------------------------------


def _proc_stat_start_seconds(pid: str, proc_root: str = "/proc") -> Optional[float]:
    """/proc/<pid>/stat field 22 (starttime, clock ticks since boot) -> seconds."""
    try:
        raw = Path(f"{proc_root}/{pid}/stat").read_text()
    except OSError:
        return None
    # Field 2 (comm) can itself contain spaces/parens -- split on the last
    # ')' to skip past it safely, same trick `man 5 proc` itself recommends.
    try:
        after_comm = raw.rsplit(")", 1)[1]
        fields = after_comm.split()
        starttime_ticks = int(fields[19])  # field 22 overall; 20th after comm (0-indexed 19)
    except (IndexError, ValueError):
        return None
    try:
        clk_tck = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        clk_tck = 100  # documented POSIX default when sysconf is unavailable
    return starttime_ticks / clk_tck


class SystemdUnitState(NamedTuple):
    load_state: str  # "" on any read failure
    active_state: str  # "" on any read failure
    active_enter_seconds: Optional[float]  # None unless load_state/active parsed cleanly


def _systemd_show(unit: Optional[str]) -> SystemdUnitState:
    """`systemctl --user show <unit> -p LoadState,ActiveState,ActiveEnterTimestampMonotonic`.

    One call fetches all three properties `probe_one` needs (registration
    state, activity state, and the timing field) -- mirrors
    `start-engram-daemon.sh`'s own `_supervisor_registered_state` pattern of
    reading LoadState/ActiveState together, unpiped, with the exit status
    checked before any text is trusted (no `| grep` -- the spec's "print the
    row count" rule exists to catch exactly the ambiguity a piped read would
    introduce; capturing then checking rc sidesteps it entirely).

    Also applies the same `XDG_RUNTIME_DIR` guard `start-engram-daemon.sh`
    had to add (Kepler's PR #2700 finding): an interactive/hook environment
    without a logind session has no `XDG_RUNTIME_DIR`, which makes
    `systemctl --user` fail outright ("Failed to connect to bus") regardless
    of whether a unit is actually registered -- reproducing, for every seat
    that lacks it, the exact stranding this probe exists to measure instead
    of guess at.
    """
    empty = SystemdUnitState("", "", None)
    if unit is None:
        return empty
    env = dict(os.environ)
    if "XDG_RUNTIME_DIR" not in env:
        candidate = f"/run/user/{os.getuid()}"
        if os.path.isdir(candidate):
            env["XDG_RUNTIME_DIR"] = candidate
    try:
        result = subprocess.run(
            [
                "systemctl", "--user", "show", unit,
                "-p", "LoadState,ActiveState,ActiveEnterTimestampMonotonic",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return empty
    if result.returncode != 0:
        return empty

    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key] = value.strip()

    load_state = fields.get("LoadState", "")
    active_state = fields.get("ActiveState", "")
    active_enter_seconds: Optional[float] = None
    raw_ts = fields.get("ActiveEnterTimestampMonotonic", "")
    if raw_ts.isdigit():
        microseconds = int(raw_ts)
        if microseconds > 0:
            active_enter_seconds = microseconds / 1_000_000.0
    return SystemdUnitState(load_state, active_state, active_enter_seconds)


def _start_verdict(pid: str, unit_state: SystemdUnitState, proc_root: str = "/proc") -> str:
    """UNCHANGED | MOVED | UNKNOWN -- see module docstring's timing section."""
    if not Path(proc_root).exists():
        return "UNKNOWN"  # Darwin: no /proc, no boot-relative comparison available
    if unit_state.active_enter_seconds is None:
        return "UNKNOWN"
    proc_seconds = _proc_stat_start_seconds(pid, proc_root)
    if proc_seconds is None:
        return "UNKNOWN"
    delta = abs(proc_seconds - unit_state.active_enter_seconds)
    return "UNCHANGED" if delta <= _START_TOLERANCE_SECONDS else "MOVED"


# ---------------------------------------------------------------------------
# per-process probe + formatting
# ---------------------------------------------------------------------------


class ProcessCheck(NamedTuple):
    name: str
    pattern: str
    systemd_unit: Optional[str]
    launchd_label: Optional[str]


def probe_one(seat: str, check: ProcessCheck, *, proc_root: str = "/proc") -> tuple[str, bool]:
    """Return (formatted line, is_ok). is_ok is False if the line carries
    any FAIL or ERROR anywhere in it -- callers sum this for the exit code.
    """
    resolution = resolve_pid(check.pattern)

    if resolution.status == "not-found":
        # Pinned rule: zero matches is a FAIL, not an ERROR -- the process
        # is not independently discoverable by its own launch identity.
        line = (
            f"{seat} {check.name} pid=none cgroup=FAIL:not-found "
            f"start=UNKNOWN supervisor=none"
        )
        return line, False

    if resolution.status == "ambiguous":
        # Never cite a literal pid when the match is ambiguous.
        line = (
            f"{seat} {check.name} pid=ambiguous cgroup=ERROR:{resolution.count}-matches "
            f"start=UNKNOWN supervisor=unknown"
        )
        return line, False

    pid = resolution.pid
    assert pid is not None

    is_linux = Path(proc_root).exists()
    if is_linux:
        cgroup_verdict = _read_cgroup_verdict(pid, proc_root)
        unit_state = _systemd_show(check.systemd_unit)
        # supervisor reports the ACTUALLY-DETERMINED registration, never the
        # name of the unit this run merely checked FOR -- echoing the
        # expected name unconditionally would read as "yes, supervised" even
        # when LoadState says otherwise. "not-found" is systemd's own
        # well-formed answer for "no such unit"; anything else non-empty
        # (registered, whatever its current activity) reports the unit name.
        if check.systemd_unit is None or unit_state.load_state in ("", "not-found"):
            supervisor = "none"
        else:
            supervisor = check.systemd_unit
        start = _start_verdict(pid, unit_state, proc_root)
    else:
        launchd_state = _launchd_state(check.launchd_label) if check.launchd_label else None
        cgroup_verdict = _read_launchd_verdict(pid, check.launchd_label, launchd_state)
        registered = launchd_state is not None and launchd_state.registered
        supervisor = check.launchd_label if registered else "none"
        start = "UNKNOWN"  # no boot-relative timestamp on Darwin -- see module docstring

    # Append :<why> whenever there IS a why -- consistent across FAIL and
    # ERROR, not special-cased per status. A diagnostic tool's whole point is
    # naming what went wrong; a FAIL that discards its own reason (e.g. "not
    # registered under com.engram.surface-daemon", "registered but no live
    # pid") forces a reader back into the source to learn what the not-found
    # variant already prints inline. (The not-found pid=none branch above
    # constructs its own line directly rather than through this formatter --
    # see the module docstring's grammar note for why that one line's
    # cgroup=FAIL:not-found looks the same shape as this general case.)
    cgroup_field = cgroup_verdict.status
    if cgroup_verdict.why:
        cgroup_field = f"{cgroup_verdict.status}:{cgroup_verdict.why}"

    is_ok = cgroup_verdict.status == "PASS"
    line = f"{seat} {check.name} pid={pid} cgroup={cgroup_field} start={start} supervisor={supervisor}"
    return line, is_ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--engram-home",
        default=os.environ.get("ENGRAM_HOME", os.path.expanduser("~/.engram")),
        help="ENGRAM_HOME to resolve config/paths from (default: $ENGRAM_HOME or ~/.engram)",
    )
    parser.add_argument(
        "--proc-root",
        default="/proc",
        help=argparse.SUPPRESS,  # test-injection only -- same spirit as _proc_identity.py's ENGRAM_PROC_DIR, different mechanism (see module docstring)
    )
    args = parser.parse_args(argv)

    engram_home = Path(args.engram_home).expanduser()
    seat = seat_name(engram_home)

    checks = [
        ProcessCheck(
            name="daemon",
            pattern=resolve_daemon_script_path(engram_home),
            systemd_unit=_DAEMON_SYSTEMD_UNIT,
            launchd_label=_DAEMON_LAUNCHD_LABEL,
        ),
        ProcessCheck(
            name="mcp-server",
            pattern=resolve_own_server_py_path(engram_home),
            systemd_unit=_MCP_SERVER_SYSTEMD_UNIT,
            launchd_label=_MCP_SERVER_LAUNCHD_LABEL,
        ),
    ]

    any_bad = False
    for check in checks:
        line, is_ok = probe_one(seat, check, proc_root=args.proc_root)
        print(line)
        if not is_ok:
            any_bad = True

    return 1 if any_bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
