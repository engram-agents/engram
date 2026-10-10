#!/usr/bin/env python3
"""I/O helper for start-engramd-owner.sh (engram-alpha#3663, round 8).

A bash hook cannot open a state-file path non-blockingly (a `[ -f ]` pre-check is a
TOCTOU, and `cat`/`>>` on a FIFO plant block until the hook's timeout). Every open of a
file under $ENGRAM_HOME that the hook needs is therefore done HERE, through the #3628
helpers in _hooklib (`open_regular_text`, `open_regular_text_append`: O_NONBLOCK, then an
fstat that rejects anything but a regular file). No second opener lives in this file.

Subcommands (the shell switches on what they print; they never raise, and exit 0):
  live <home>                  -> alive | none | notregular:<kind>
      `<home>/engramd.pid` names a positive pid that is alive AND whose
      /proc/<pid>/cmdline contains `engramd_owner` (cmdline unreadable: alive -- never
      guess dead). A missing / garbage / non-positive / dead / marker-less pidfile is
      `none`. A pidfile that is not a regular file is `notregular:<kind>` (fifo,
      directory, socket, device, or symlink-to-<kind>): a planted special file is an
      abnormal state.
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

import json
import os
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
_HEAD_CLIP = 600  # chars of message injected; the full message always goes to the log


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


def live(home: str) -> str:
    pid_path = os.path.join(home, "engramd.pid")
    try:
        with open_regular_text(pid_path, encoding="utf-8") as f:
            raw = f.read(64)
    except (FileNotFoundError, ValueError):  # missing / not UTF-8: garbage, as before
        return "none"
    except OSError:
        kind = _non_regular_kind(pid_path)
        return f"notregular:{kind}" if kind else "none"  # unreadable regular file: as before, none
    text = raw.strip()
    if not text.isascii() or not text.isdigit():
        return "none"
    try:
        pid = int(text)
    except ValueError:
        return "none"
    if pid <= 0:  # `kill -0 0` signals the process group and succeeds; not a process
        return "none"
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        return "none"
    try:
        # kernel-provided /proc file, not a path anyone can plant a FIFO at
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read()
    except OSError:
        return "alive"  # cannot tell reuse from a live owner -- never guess dead
    return "alive" if _MARKER.encode() in cmdline else "none"


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
        elif cmd == "report" and len(argv) == 4:
            report(argv[2], argv[3])
        elif cmd == "launch" and len(argv) == 5:
            print(launch(argv[2], argv[3], argv[4]))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
