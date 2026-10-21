#!/usr/bin/env python3
"""I/O helper for start-engramd-owner.sh (engram-alpha#3663, round 8).

A bash hook cannot open a state-file path non-blockingly (a `[ -f ]` pre-check is a
TOCTOU, and `cat`/`>>` on a FIFO plant block until the hook's timeout). Every open of a
file under $ENGRAM_HOME that the hook needs is therefore done HERE, through the #3628
helpers in _hooklib (`open_regular_text`, `open_regular_text_append`: O_NONBLOCK, then an
fstat that rejects anything but a regular file). No second opener lives in this file.

Subcommands (the shell switches on what they print; they never raise, and exit 0):
  live <home>                  -> alive | none | notregular:<kind> | ambiguous:<why>
      `<home>/engramd.pid` names a positive pid that is alive AND whose argv contains
      `engramd_owner`, decided by engram_pidfile's STRICT mode
      (`recorded_live_pid(..., require_marker=True)`, #3709): the marker is required of
      any live pid whatever its start time, and an unreadable argv counts as alive
      (never guess dead). Same rule on Linux and macOS: this file reads no /proc.
      A missing / garbage / non-positive / dead / marker-less pidfile is `none`. A
      pidfile that is not a regular file is `notregular:<kind>` (fifo, directory, socket,
      device, or symlink-to-<kind>) -- checked FIRST, before engram_pidfile is touched, so
      a planted special file can neither block the open nor reach it. If engram_pidfile
      cannot be imported the answer is `ambiguous:<why>` (never `none`: the shell then
      launches nothing).
  launchd_state <label>        -> running | registered:<state>:<last exit or none> | notfound
                                  | no_gui_domain | ambiguous:<why>
      Runs `launchctl print gui/<uid>/<label>` under a Python-side timeout
      (LAUNCHD_BOUND_SECONDS; stock macOS has no GNU `timeout`) and hands the result to
      `parse_launchctl_print`. launchctl missing or timing out is ambiguous. `no_gui_domain`
      (#3780) means this uid has no launchd GUI domain at all (no GUI login session), so a
      bootstrap into it cannot work: the shell says so and launches nothing.
  serving <home>               -> serving | notserving:<what was observed> | ambiguous:<why>
      #3736: the owner is asked to answer a `ping` on its own socket, because "a pid is
      alive" is not "the owner serves". The WHOLE probe is bounded by
      PROBE_TOTAL_BOUND_SECONDS (a named constant; the helper owns the bound because stock
      macOS has no GNU `timeout`), and `notserving:` is the budgeted claim "no healthy ping
      answer within T" -- never "the owner does not serve". A bundle without an importable
      engramd_client is `ambiguous:`, which claims nothing about the owner. Launches nothing.
  report <home> <message>      -> one JSON object on stdout
      Appends `[ts] [start-engramd-owner] <message>` (the FULL message) to
      `<home>/engramd-owner.log` when that is a regular file (silently skipped
      otherwise) and prints the SessionStart additionalContext, composed through
      `_hooklib.emit_context` under the harness budget (trailer first). The injected
      head is the message with its middle clipped when long, so a very long ENGRAM_HOME
      cannot blow the budget; the diagnosis words lead every message.
  launch <home> <python> <owner.py>  -> `<pid> log` | `<pid> nolog` | `fail`
      Starts the owner detached (own session, stdin /dev/null, ENGRAM_HOME in the env),
      its output appended to `<home>/engramd-owner.log` when that is a regular file,
      else discarded (/dev/null).
"""

from __future__ import annotations

import importlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time

_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

from _harness import detect_harness, get_harness_budgets  # noqa: E402
from _hooklib import emit_context, open_regular_text, open_regular_text_append  # noqa: E402

_MARKER = "engramd_owner"
LAUNCHD_BOUND_SECONDS = 3.0  # `launchctl print` is killed past this (Linux's systemctl bound is 3 s too)
_NO_GUI_DOMAIN_TEXT = "Domain does not support specified action"  # rc 125's stderr, measured (#3780)
_HEAD_CLIP = 600  # chars of message injected; the full message always goes to the log

# The serving probe (#3736). T is the WHOLE probe's budget and it is a named constant, because
# every claim the probe makes is relative to it: it establishes "the owner did not answer a ping
# within T", which is the distinction from the exited state (#3736 cell d) -- never "the owner
# does not serve", which a bounded probe cannot establish. The helper owns the bound because
# stock macOS has no GNU `timeout` and the shell must not do arithmetic on a deadline; the hook's
# own timeout is 15 s and the launch watch already spends ~1.2 s of it, so T leaves room.
PROBE_TOTAL_BOUND_SECONDS = 1.5
PROBE_CALL_BOUND_SECONDS = 0.5  # one attempt; a bound-but-silent socket cannot eat the window
PROBE_POLL_SECONDS = 0.25       # between attempts: an owner that is still binding gets the window


def _kind_of(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISSOCK(mode):
        return "socket"
    return "device"


def _non_regular_kind(path: str):
    """The kind of a non-regular file at `path` (symlink-to-<kind> when behind a link), or None."""
    try:
        lst = os.lstat(path)
        st = os.stat(path)
    except OSError:
        return None
    if stat.S_ISREG(st.st_mode):
        return None
    kind = _kind_of(st.st_mode)
    return f"symlink-to-{kind}" if stat.S_ISLNK(lst.st_mode) else kind


def _import_sibling(filename: str, module: str):
    """A sibling module of this file, by the ONE walk-parents rule both importers use.

    The plugin root is the parent of hooks/ in the flattened bundle and two up from hooks/claude/ in
    the source tree; the first ancestor holding `filename` is inserted into sys.path (so the sibling
    module's own imports resolve from there too). Never a fixed parents[N]: the build flattens
    hooks/claude/* -> hooks/*, and a source-calibrated index overshoots in the deployed tree. Falls
    back to wherever the interpreter finds it; raises ImportError when it cannot be found at all.
    ONE copy, because a layout fix applied to one importer and not the other is the drift the
    walk-parents rule exists to prevent (round-2 F5)."""
    for up in (1, 2):
        cand = _this_dir
        for _ in range(up):
            cand = os.path.dirname(cand)
        if os.path.isfile(os.path.join(cand, filename)):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            break
    return importlib.import_module(module)


def _import_pidfile():
    """engram_pidfile, resolved by `_import_sibling` (which owns the walk-parents rule)."""
    return _import_sibling("engram_pidfile.py", "engram_pidfile")


def _import_client():
    """engramd_client, resolved by `_import_sibling`. The client's own `import engram_pidfile`
    resolves from the same directory, which is why the walk is the one that inserts into sys.path."""
    return _import_sibling("engramd_client.py", "engramd_client")


def live(home: str) -> str:
    pid_path = os.path.join(home, "engramd.pid")
    # FIRST, before engram_pidfile is imported or called: a planted FIFO/special file at
    # engramd.pid must be reported without ever being opened by anything but the
    # non-blocking helper (engram_pidfile opens it plainly, which would block).
    kind = _non_regular_kind(pid_path)
    if kind:
        return f"notregular:{kind}"
    try:
        ep = _import_pidfile()
    except Exception as exc:  # cannot check -> ambiguous, never `none` (a duplicate is worse)
        return f"ambiguous:engram_pidfile not importable ({type(exc).__name__})"
    try:
        # strict mode: marker required for any live pid, unreadable argv counts as alive
        pid = ep.recorded_live_pid(pid_path, _MARKER, require_marker=True)
    except Exception as exc:
        return f"ambiguous:engram_pidfile failed ({type(exc).__name__})"
    return "alive" if pid is not None else "none"


def _probe_why(ec, result) -> str:
    """One short ASCII token for a ping that did not come back healthy: the client's own
    Unavailable reason code (`down`, `timeout`, `protocol`, `busy`, `busy_or_stuck`, `local`,
    `internal`), or `rejected-<reason>` when the owner answered and refused the ping. Sanitised:
    this token lands in a one-line report, and a hostile or broken owner must not be able to
    break the line."""
    reason = getattr(result, "reason", None)
    token = f"rejected-{reason or 'unknown'}" if isinstance(result, ec.Rejected) else str(reason or "unknown")
    return re.sub(r"[^A-Za-z0-9_-]", "-", token)[:40] or "unknown"


def serving(home: str) -> str:
    """Does the owner for `home` actually SERVE? (#3736)

    serving | notserving:<what was observed> | ambiguous:<why>

    Asking the owner's own socket for a `ping` -- the method engramd_owner registers for exactly
    this and the one method in its NO_STORE_METHODS (it touches no store, so it is exempt from the
    displacement guard): a pid that exists is not an owner that serves, and only the socket can
    tell the difference. NEVER RAISES and never launches anything (a second owner against a live
    pid is the hazard this hook exists to avoid); the caller decides what to report.

    The whole probe is bounded by PROBE_TOTAL_BOUND_SECONDS, structurally: the deadline is computed
    once here and every attempt is armed with the remaining budget, so a silent or trickling peer
    cannot extend the call. Attempts are repeated until the deadline, because an owner that has just
    been launched may still be binding its socket -- a single immediate connect would misreport that
    transient as "not serving". The loop runs its whole budget (it never gives up one poll early),
    so the window this report names is the window the probe actually used (#3736 round-2 F2).

    `notserving:` is a BUDGETED claim -- "no healthy ping answer within T" -- and can never be
    "the owner does not serve". `ambiguous:` means the probe itself could not run (no importable
    client): nothing is claimed about the owner, and the caller must not read it as not-serving.
    """
    try:
        ec = _import_client()
    except Exception as exc:  # a bundle without the client: say so, claim nothing about the owner
        return f"ambiguous:engramd_client not importable ({type(exc).__name__})"
    try:
        sock = ec.socket_path_for_home(home)
    except Exception as exc:
        return f"ambiguous:engramd_client failed ({type(exc).__name__})"
    deadline = time.monotonic() + PROBE_TOTAL_BOUND_SECONDS
    why = "unknown"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Armed with the remaining budget once less than one PROBE_CALL_BOUND_SECONDS is left, so
        # the probe keeps ASKING to its deadline rather than sleeping past its last question. That
        # is what makes "within PROBE_TOTAL_BOUND_SECONDS" true of the probe that ran: the earlier
        # form broke out one poll early, realizing ~1.25 s of the 1.5 s it printed (#3736 F2).
        try:
            result = ec.read(
                "ping", {}, timeout=min(remaining, PROBE_CALL_BOUND_SECONDS), socket_path=sock
            )
        except Exception as exc:  # `read` is raise-free by construction; this is the belt
            return f"ambiguous:engramd_client raised ({type(exc).__name__})"
        if isinstance(result, ec.Ok):
            return "serving"
        why = _probe_why(ec, result)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(PROBE_POLL_SECONDS, remaining))
    return (
        f"notserving:no healthy ping answer within {PROBE_TOTAL_BOUND_SECONDS:g}s (last: {why})"
    )


def parse_launchctl_print(rc: int, stdout: str, stderr: str) -> str:
    """The verdict for one `launchctl print gui/<uid>/<label>` result.

    running | registered:<state>:<last exit code or none> | notfound | no_gui_domain
    | ambiguous:<why>

    Shapes measured on macOS 26.6.2 (tests/fixtures/launchctl_3709/README.md):
      * unknown label: rc 113, stdout empty, stderr `Bad request.` + `Could not find
        service "<label>" in domain for user gui: <uid>` -> notfound. BOTH the rc and the
        text are required: either alone is ambiguous.
      * no GUI domain for this uid (no GUI login session; #3780): rc 125, stdout empty,
        stderr `Could not print domain: 125: Domain does not support specified action`
        -> no_gui_domain. Again BOTH the rc and the text are required. This is not a
        not-found: the domain itself is absent, so a bootstrap would fail too.
      * known job: rc 0; the job's own state is the ONE-TAB-indented `state =` line.
        The nested dicts below it carry `state = active` at deeper indent, which is not
        the job's state, so only that exact indent counts. `last exit code =` is read
        the same way (a running job has none).
    Everything else (a different rc, empty output, no top-level state line) is
    ambiguous: the shell launches nothing."""
    if rc == 113:
        if "Could not find service" in (stderr or ""):
            return "notfound"
        return "ambiguous:launchctl exited 113 without the not-found text"
    if rc == 125:
        if _NO_GUI_DOMAIN_TEXT in (stderr or ""):
            return "no_gui_domain"
        return f"ambiguous:launchctl print exited {rc}"
    if rc != 0:
        return f"ambiguous:launchctl print exited {rc}"
    if not (stdout or "").strip():
        return "ambiguous:launchctl print answered with empty output"
    state = last = None
    for line in stdout.splitlines():
        if line.startswith("\tstate = ") and state is None:
            state = line[len("\tstate = "):].strip()
        elif line.startswith("\tlast exit code = ") and last is None:
            last = line[len("\tlast exit code = "):].strip()
    if not state:
        return "ambiguous:launchctl print answered with no top-level state line"
    state = state.replace(":", " ")  # ':' separates the fields of the verdict
    if state == "running":
        return "running"
    last = (last or "none").replace(":", " ")
    return f"registered:{state}:{last}"


def launchd_state(label: str) -> str:
    argv = ["launchctl", "print", f"gui/{os.getuid()}/{label}"]
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", start_new_session=True,
        )
    except FileNotFoundError:
        return "ambiguous:launchctl not found on PATH"
    except OSError as exc:
        return f"ambiguous:launchctl could not be run ({type(exc).__name__})"
    try:
        out, err = proc.communicate(timeout=LAUNCHD_BOUND_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=1.0)
        except Exception:
            pass
        return f"ambiguous:launchctl print timed out after {LAUNCHD_BOUND_SECONDS:g}s"
    return parse_launchctl_print(proc.returncode, out, err)


def _clip_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = (limit - 3) // 2
    return text[:keep] + " … " + text[-keep:]


def _append_log(home: str, line: str) -> None:
    try:
        with open_regular_text_append(os.path.join(home, "engramd-owner.log"), encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # not a regular file / unwritable: log writes are skipped


def report(home: str, message: str) -> None:
    message = " ".join(message.split())  # one line; control characters out
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _append_log(home, f"[{stamp}] [start-engramd-owner] {message}")
    budgets = get_harness_budgets(detect_harness())
    ctx = emit_context(
        _clip_middle(message, _HEAD_CLIP), [],
        budget=budgets["budget"], target=min(budgets["head_budget"], budgets["budget"]),
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))


def launch(home: str, python_bin: str, owner_py: str) -> str:
    log_state = "log"
    try:
        log = open_regular_text_append(os.path.join(home, "engramd-owner.log"), encoding="utf-8")
    except OSError:
        log, log_state = open(os.devnull, "a"), "nolog"
    try:
        proc = subprocess.Popen(
            [python_bin, owner_py], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            env={**os.environ, "ENGRAM_HOME": home}, start_new_session=True, close_fds=True,
        )
    except OSError:
        return "fail"
    finally:
        log.close()
    return f"{proc.pid} {log_state}"


def main(argv: list) -> int:
    try:
        cmd = argv[1]
        if cmd == "live" and len(argv) == 3:
            print(live(argv[2]))
        elif cmd == "launchd_state" and len(argv) == 3:
            print(launchd_state(argv[2]))
        elif cmd == "serving" and len(argv) == 3:
            print(serving(argv[2]))
        elif cmd == "report" and len(argv) == 4:
            report(argv[2], argv[3])
        elif cmd == "launch" and len(argv) == 5:
            print(launch(argv[2], argv[3], argv[4]))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
