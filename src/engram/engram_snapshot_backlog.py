"""Read-only view of the snapshot worker's pending queue (#4843).

A snapshot job that cannot commit stays pending (by design: the captured binary is the
durability backstop). Before #4843 nothing printed that fact except server stderr and a
lag field that warns only at two distinct pending turns. This module reads the worker's
queue file directly and reports a backlog once a pending job is old enough that a healthy
worker cannot explain it.

Stdlib only, and deliberately does NOT import the worker: SessionStart and the sleep
skill run it where the engram runtime (sqlite_vec, the venv) may not load. It never
creates a directory or a file (unlike the worker's ``_pending_dir``).

The pending-set rule mirrors ``engram_snapshot_worker._pending_jobs``: job entries with
no done entry of the same (turn, seq); blank lines ignored; a line that is not valid JSON
is skipped.

CLI: ``python3 engram_snapshot_backlog.py [ENGRAM_HOME]`` prints the line when there is
one; exit 0 ok, 1 backlog, 2 unreadable.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Optional

# Basis, measured 2026-11-17 on dev c18102c8, a 23.5 MB store, Linux, idle host: a healthy
# job is pending 0.26-1.38 s from enqueue to done (n=11, median 0.51 s); the worker retries
# a failed job at its next wake, at most 30 s later (`_work_event.wait(timeout=30.0)`);
# 300 s is ten retry wakes and over 200x the measured healthy maximum.
# Unmeasured: macOS, larger graphs.
BACKLOG_THRESHOLD_SECS = 300

_MAX_LINE = 300
_UNREADABLE_PREFIX = "Snapshot commits: queue could not be read ("


def _clip(line: str) -> str:
    line = line.replace("\r", " ").replace("\n", " ")
    return line if len(line) <= _MAX_LINE else line[: _MAX_LINE - 3] + "..."


def _result(status: str, pending: int = 0, oldest: Optional[float] = None,
            nbytes: int = 0, line: Optional[str] = None) -> dict:
    return {"status": status, "pending": pending, "oldest_age_secs": oldest,
            "bytes": nbytes, "line": line}


def _unreadable(exc: BaseException) -> dict:
    return _result("unreadable", line=_clip(f"{_UNREADABLE_PREFIX}{type(exc).__name__}: {exc})"))


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def read_backlog(engram_home, now: Optional[float] = None,
                 threshold: float = BACKLOG_THRESHOLD_SECS) -> dict:
    """Return {status, pending, oldest_age_secs, bytes, line} for the pending queue."""
    pending_dir = Path(engram_home) / "db-backup" / ".pending"
    queue = pending_dir / "queue.jsonl"
    try:
        # lstat first: opening a FIFO for read blocks forever, and a symlink, socket or
        # device is not the regular file the worker writes. Never open anything else.
        try:
            st = os.lstat(queue)
        except (FileNotFoundError, NotADirectoryError):
            return _result("ok")
        if not stat.S_ISREG(st.st_mode):
            kind = ("directory" if stat.S_ISDIR(st.st_mode) else
                    "symlink" if stat.S_ISLNK(st.st_mode) else
                    "FIFO" if stat.S_ISFIFO(st.st_mode) else
                    "socket" if stat.S_ISSOCK(st.st_mode) else
                    "device" if (stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode)) else
                    "non-regular file")
            return _unreadable(OSError(f"queue is a {kind}, not a regular file"))
        text = queue.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _unreadable(exc)

    done: set = set()
    jobs: list = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        key = (e.get("turn"), e.get("seq"))
        if e.get("type") == "done":
            done.add(key)
        elif e.get("type") == "job":
            jobs.append(e)
    pending = [j for j in jobs if (j.get("turn"), j.get("seq")) not in done]
    if not pending:
        return _result("ok")

    if now is None:
        now = time.time()
    oldest = None
    nbytes = 0
    for j in pending:
        ts = j.get("ts")
        if isinstance(ts, (int, float)):
            age = now - ts
            oldest = age if oldest is None else max(oldest, age)
        name = j.get("snapshot") or f"snapshot-{j.get('turn')}-{j.get('seq')}.db"
        # Confine to the pending dir: only the final path component of the name counts;
        # an empty, "." or ".." name counts 0 bytes.
        base = Path(str(name)).name
        if base in ("", ".", ".."):
            continue
        snap = pending_dir / base
        nbytes += (_size(snap) + _size(snap.with_name(snap.name + "-wal"))
                   + _size(snap.with_name(snap.name + "-shm")))

    if oldest is not None and oldest > threshold:
        line = (f"Snapshot commits: {len(pending)} job(s) pending, oldest {int(oldest)}s old, "
                f"{nbytes} bytes held; git history is not advancing")
        return _result("backlog", len(pending), oldest, nbytes, line)
    return _result("ok", len(pending), oldest, nbytes)


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    home = argv[0] if argv else os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")
    r = read_backlog(home)
    if r["line"] is not None:
        print(r["line"])
    return {"ok": 0, "backlog": 1}.get(r["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
