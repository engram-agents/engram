"""WAL/shm self-guard for ENGRAM — displacement detection and degraded-state marker.

Detects split-brain WAL-index conditions (knowledge.db-shm deleted or replaced
while the server holds it open) and provides marker-file primitives for
coordinating degraded-mode across all DB-touching processes.

Detection is Linux-only in v1: it relies on /proc/self/fd/ symlinks to compare
the inode the live server process holds open against the on-disk file.  On
non-Linux hosts or when /proc is absent, detect_shm_displacement() returns
{"reason": "detection_unavailable", ...} (#2651) -- distinct from the bare
None it returns for a GENUINELY confirmed-healthy inode match. Before #2651
this case collapsed into bare None too, indistinguishable from health; on
every seat without /proc (every Darwin seat) that made the guard silently
report clean on every call, for the life of the process.

Motivating incident (2026-06-03): knowledge.db-shm was deleted under the live
MCP server.  The server stayed coherent for ~6 hours while fresh external
connections saw a stale snapshot (split-brain WAL-index).  At session teardown
the dying server's final checkpoint truncated the main DB 31 pages short of its
header — total corruption, recovered only via the per-checkpoint SQL dump.

That framing is read-side only, and it is incomplete: the same window is also a
**write-durability** hazard.  On 2026-08-07 (alpha#1947) two observations filed
through the degraded server returned full success payloads — status `created`,
node id, evidence id, computed confidence, dedup neighbours — the same as any
other successful write.  Both were silently lost when the server was replaced:
the graph's max observation id rolled back on restart, and the emergency dump
predated the writes, so it did not rescue them either.  A caller filing during
split-brain has no way to tell a durable write from a doomed one — the "6 hours"
figure should be read as write exposure, not merely stale-read exposure.

Fail-closed gate (#2635 unit 2): detection alone does not stop a doomed write.
assert_not_displaced() is the single place that turns a CONFIRMED displacement
into a hard refusal (SubstrateDisplaced) — called unthrottled on every DB-
connection dispatch, additive alongside (never a replacement for) the
existing throttled marker/dump path below.

This module:
  - detect_shm_displacement()  — inode-compare check via /proc/self/fd/,
                                  covering both -shm and -wal
  - assert_not_displaced()     — raises SubstrateDisplaced on a CONFIRMED
                                  displacement; a quiet no-op on healthy or
                                  detection_unavailable (#2635 unit 2)
  - SubstrateDisplaced         — the exception assert_not_displaced() raises
  - write_degraded_marker()    — atomic marker write
  - read_degraded_marker()     — read the marker (returns None when absent)
  - clear_degraded_marker()    — remove the marker
  - recovery_block()           — structured recovery guidance (#1947), also
                                  embedded in write_degraded_marker()'s output
  - recovery_banner_text()     — compact prose form of the same guidance, used
                                  by engram_core's CRITICAL log line, the
                                  SUBSTRATE DEGRADED banner, and
                                  SubstrateDisplaced.remedy
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Recovery guidance (#1947)
# ---------------------------------------------------------------------------
# Single source of truth for the recovery instruction, shared by:
#   - engram_core._walguard_startup_clear's "[walguard] CRITICAL: ..." log line
#   - engram_core._walguard_degraded_banner's "SUBSTRATE DEGRADED" banner
#   - write_degraded_marker()'s structured "recovery" key
# so the three can never drift out of sync (alpha#1947 slice A/B — the prior
# "request a clean MCP restart" wording named only one of the two long-lived
# holders of knowledge.db; the surface daemon is the other, and recovery
# requires BOTH to release the DB).

_RECOVERY_ISSUE_URL = "https://github.com/engram-agents/engram/issues/786"

_RECOVERY_CONDITION = (
    "every process holding knowledge.db must release it; a displaced "
    "WAL-index cannot be rebuilt while any connection is open"
)

_RECOVERY_STEPS: list[str] = [
    "restart the MCP server (/mcp reconnect)",
    "stop the surface daemon: kill -TERM $(cat ~/.engram/recall-daemon.pid)",
    'restart it: bash "$CLAUDE_PLUGIN_ROOT/hooks/start-engram-daemon.sh"',
    'VERIFY before believing it worked: sqlite3 ~/.engram/knowledge.db '
    '"select count(*) from nodes" must succeed -- a reconnect that reports '
    'success is NOT evidence the index recovered',
]

_RECOVERY_VERIFY = 'sqlite3 <db_path> "select count(*) from nodes"'


def recovery_block() -> dict:
    """Structured recovery guidance (#1947), embedded verbatim in the degraded
    marker's "recovery" key.  Built from the same constants as
    recovery_banner_text() so the marker and the log/banner text can never
    drift out of sync.
    """
    return {
        "condition": _RECOVERY_CONDITION,
        "steps": list(_RECOVERY_STEPS),
        "verify": _RECOVERY_VERIFY,
        "issue": _RECOVERY_ISSUE_URL,
    }


def recovery_banner_text() -> str:
    """Compact prose form of the same recovery guidance as recovery_block(),
    for the CRITICAL stderr log line and the SUBSTRATE DEGRADED banner.

    Names every long-lived holder (MCP server AND the surface daemon) and
    ends with a verify step — the two gaps that cost ~20 extra minutes on
    2026-08-07 (alpha#1947): the operator restarted only the MCP server and
    had no stated way to check whether the remedy actually worked.
    """
    numbered = "; ".join(f"({i}) {step}" for i, step in enumerate(_RECOVERY_STEPS, 1))
    return (
        f"Recovery requires EVERY holder to release the DB, not just the MCP "
        f"server: {numbered}. (#786)"
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_shm_displacement(db_path: str) -> Optional[dict]:
    """Check whether the live process's shm AND wal fds have been displaced.

    Compares the inode of the -shm and -wal files the current process holds
    open (via /proc/self/fd/) against the inode of the corresponding on-disk
    <db_path>-shm / <db_path>-wal files.

    #2635 widening: v1 of this function checked -shm only. A real incident
    (2026-09-06) showed a server's -wal fd marked "(deleted)" while -shm
    looked perfectly healthy -- the shm-only check would have called that
    case healthy. Both files are now checked with the same deleted-vs-
    replaced logic; WAL is checked first (matching that incident's own
    narrative), though either check order reaches the same verdict, since
    ANY genuine displacement on either file is disqualifying.

    Returns:
        None  — GENUINELY healthy: at least one of shm/wal has an fd held by
                this process, and every HELD one's inode matches its on-disk
                file. The ONLY value a caller may read as "confirmed not
                displaced". (An fd not held for one of the two files is not
                itself a failure — it just means that file can't be judged
                yet; see "no shm or wal fd held" below for when NEITHER can
                be judged.)
        {"reason": "detection_unavailable", "why": <str>}
            — #2651: detection could not be performed, for any of several
              reasons (no /proc on this platform; neither shm nor wal fd held
              by this process yet, e.g. the connection hasn't opened WAL; a
              held fd or its on-disk file couldn't be stat()'d; an unexpected
              internal error). Mirrors capture_forensics()'s own
              "unavailable" vocabulary below. Callers MUST NOT treat this
              the same as None -- "could not check" is not "checked and
              clean". Before #2651 every one of these cases returned bare
              None, indistinguishable from genuine health; on a seat with
              no /proc (every Darwin seat, v1), that made this function
              return the None shape on EVERY call, permanently.
        {"reason": "shm_deleted", "fd": <int>} / {"reason": "wal_deleted", "fd": <int>}
            — the fd symlink ends with " (deleted)", meaning the on-disk shm
              (respectively wal) was removed while the process held it open.
        {"reason": "shm_replaced", "fd": <int>, "fd_inode": <int>, "disk_inode": <int>}
        {"reason": "wal_replaced", "fd": <int>, "fd_inode": <int>, "disk_inode": <int>}
            — the on-disk shm (respectively wal) exists but has a different
              inode than the one this process holds open (file was replaced,
              not deleted).  This is the atomic-replacement case — e.g.
              another process called os.replace() on the path — not the
              simple deletion case (which produces the "_deleted" reason via
              the "(deleted)" suffix).  On Linux, unlinking always produces
              the "(deleted)" suffix, so a "_replaced" reason requires a live
              replacement file to exist at the path with a mismatched inode.
    """
    try:
        shm_path = db_path + "-shm"
        wal_path = db_path + "-wal"
        proc_fd = "/proc/self/fd"
        if not os.path.isdir(proc_fd):
            return {
                "reason": "detection_unavailable",
                "why": "no /proc/self/fd (non-Linux or /proc not mounted; "
                "detection is Linux-only in v1)",
            }

        # (fd_num, raw readlink target) per file name, found by a single scan.
        held: dict[str, tuple[int, str]] = {}

        # os.listdir intentional: closes dir fd before return, preventing a
        # transient self-referencing fd that os.scandir (which keeps the dir fd
        # open across iterations) would create in /proc/self/fd/.
        for entry in os.listdir(proc_fd):
            try:
                fd_num = int(entry)
            except ValueError:
                continue
            try:
                target = os.readlink(os.path.join(proc_fd, entry))
            except OSError:
                continue
            # Match the exact path (ignoring any " (deleted)" suffix for
            # comparison purposes — the suffix itself is a displacement signal)
            # rstrip() is cosmetic — the ' (deleted)' suffix is handled by
            # endswith() below, not stripped here.  We strip any trailing
            # whitespace so the equality/startswith comparisons don't miss a
            # path that happens to have a trailing space on an exotic fs.
            base_target = target.rstrip()
            for which, path in (("wal", wal_path), ("shm", shm_path)):
                if which in held:
                    continue
                if base_target == path or base_target.startswith(path + " "):
                    held[which] = (fd_num, target)

        if not held:
            # No fd held for either file — cannot judge; connection may not
            # have opened WAL yet (e.g. the DB was opened in WAL mode but no
            # read/write has forced shm/wal creation yet). #2651: this is a
            # "can't tell" state, not a health verdict -- distinct from
            # None, even though (unlike the /proc-absent case) it is
            # typically transient and resolves on the next check once the
            # connection has opened WAL.
            return {
                "reason": "detection_unavailable",
                "why": "no shm or wal fd held by this process yet "
                "(connection may not have opened WAL yet)",
            }

        # Check WAL before SHM (matches the 2026-09-06 incident narrative —
        # -wal deleted while -shm looked fine); either order reaches the same
        # verdict since any genuine displacement, on either file, disqualifies.
        for which, path in (("wal", wal_path), ("shm", shm_path)):
            entry = held.get(which)
            if entry is None:
                # This file's fd isn't held (yet) — not itself a failure;
                # just nothing to judge for THIS file. Only "neither held"
                # (handled above) is a detection_unavailable verdict.
                continue
            fd_num, target = entry
            base_target = target.rstrip()

            # fd target ends with " (deleted)" → on-disk file is gone
            if base_target.endswith(" (deleted)"):
                return {"reason": f"{which}_deleted", "fd": fd_num}

            # Compare inodes
            fd_link = os.path.join(proc_fd, str(fd_num))
            try:
                fd_stat = os.stat(fd_link)
            except OSError:
                # can't stat the fd link -- #2651: unavailable, not healthy
                return {
                    "reason": "detection_unavailable",
                    "why": f"could not stat the held fd link {fd_link!r}",
                }

            try:
                disk_stat = os.stat(path)
            except FileNotFoundError:
                return {"reason": f"{which}_deleted", "fd": fd_num}
            except OSError:
                # unexpected; #2651: unavailable, not healthy -- don't raise
                return {
                    "reason": "detection_unavailable",
                    "why": f"could not stat the on-disk {which} path {path!r}",
                }

            if fd_stat.st_ino != disk_stat.st_ino:
                return {
                    "reason": f"{which}_replaced",
                    "fd": fd_num,
                    "fd_inode": fd_stat.st_ino,
                    "disk_inode": disk_stat.st_ino,
                }

        return None  # healthy — every held fd (wal and/or shm) matches disk

    except Exception as e:
        # Any unexpected error → the guard must never crash the caller, but
        # #2651: this is "could not check", not "checked and healthy" --
        # a bare None here was the same collapse this whole issue is about.
        return {
            "reason": "detection_unavailable",
            "why": f"unexpected error during detection: {e!r}",
        }


# ---------------------------------------------------------------------------
# Fail-closed gate (#2635 unit 2) — assert_not_displaced()
# ---------------------------------------------------------------------------
#
# Motivating incident: when the live server's -wal/-shm sidecar files get
# displaced out from under a held connection, a write still runs on the
# displaced connection and returns success — the tool call reports "created"
# while the write is invisible to every other process. detect_shm_
# displacement() above only DETECTS the condition; nothing before this unit
# REFUSED the call once detected. This is the single place that rule lives —
# nothing else re-implements it (call this function, don't hand-roll the
# detect-then-decide logic at a new call site).


class SubstrateDisplaced(Exception):
    """Raised by assert_not_displaced() when a genuine WAL/-shm displacement
    is confirmed for the checked db_path.

    Attributes:
        detection: the raw dict from detect_shm_displacement() (a genuine
            displacement reason — never None, never "detection_unavailable";
            assert_not_displaced() only raises past those two cases).
        remedy: the SAME remedy text every other degraded-state surface uses
            (recovery_banner_text(), below) — imported, never a second
            hand-copied string, so this and the marker/banner text can never
            drift out of sync.
    """

    def __init__(self, detection: dict, remedy: str):
        self.detection = detection
        self.remedy = remedy
        super().__init__(
            f"substrate displaced (reason={detection.get('reason')}); {remedy}"
        )


def assert_not_displaced(db_path: str) -> None:
    """Raise SubstrateDisplaced iff db_path's -wal/-shm are CONFIRMED displaced.

    Meant to be called UNTHROTTLED on every DB-connection dispatch (#2635
    unit 2) — separate from the throttled, storm-gated marker-write +
    forensic-capture + emergency-dump path in engram_core._run_walguard_check,
    which keeps its own cadence unchanged. detect_shm_displacement() itself is
    cheap (median ~29µs measured), so calling it unconditionally on every
    dispatch is intentional, not a perf concern.

    Verdict mapping (mirrors detect_shm_displacement()'s own three-way split):
      - None (genuinely confirmed healthy)      → return quietly.
      - {"reason": "detection_unavailable", ...} → NOT a confirmed
        displacement (couldn't check, e.g. non-Linux or no fd held yet) —
        treated identically to None, i.e. return quietly. Darwin seats get
        no protection from this gate; that is an accepted bound, not a bug
        here (detection is Linux-only in v1 — see detect_shm_displacement's
        own docstring).
      - any other dict (a genuine displacement reason)
        → raise SubstrateDisplaced(detection, recovery_banner_text()).

    Never attempts recovery/reopen itself — recovery stays operator-driven
    (the same recovery_block()/recovery_banner_text() guidance every other
    degraded-state surface points at). A fresh same-process connection has
    been measured to get "disk I/O error" while a stale holder lives, and a
    clean close of the stale holder checkpoints an orphan WAL over data other
    connections have since written — both bad, so this function does neither.
    """
    detection = detect_shm_displacement(db_path)
    if detection is None:
        return
    if detection.get("reason") == "detection_unavailable":
        return
    raise SubstrateDisplaced(detection, recovery_banner_text())


# ---------------------------------------------------------------------------
# Forensic capture (#2635) — extra reads at the exact moment of detection.
# ---------------------------------------------------------------------------
#
# Motivating incident: on 2026-09-06 four candidate mechanisms were proposed
# to explain a displacement and all four were falsified, because by the time
# anyone looked, the live state that would have discriminated between them
# was already gone.  These two extra reads — the lock table and the
# fd→inode mapping — are cheap, taken at the exact instant detect_shm_
# displacement() fires, and answer the two questions a bare /proc listing
# cannot: who else holds a read mark on these three inodes, and does this
# process's own fd still point at a live disk inode or a dead one.
#
# THE CONSTRAINT THAT IS THE WHOLE POINT (see #2651): "instrument
# unavailable" must never collapse into "instrument ran, found nothing".
# #2651 documents detect_shm_displacement() itself returning None for three
# different conditions (healthy / no shm fd / no /proc) with both callers
# reading None as healthy — silent false-healthy.  Every sub-capture below
# therefore returns an explicit "status" of "ok" or "unavailable", never a
# bare empty list/dict standing in for "couldn't check".

_FD_SCAN_LIMIT = 200_000   # cap on /proc/self/fd entries scanned per capture
_LOCK_ROWS_LIMIT = 500     # cap on matching /proc/locks rows kept per capture

# #2705: this cap used to be 4096, tuned without evidence of a realistic
# process's ambient fd count.  os.listdir("/proc/self/fd") returns entries
# in ascending fd-number order on Linux, and this scan's own cap is
# position-based (it stops after examining `max_scan` numeric entries,
# regardless of whether it has found any of the (small, fixed) set of
# target paths yet) -- so once a process's OWN total open-fd count exceeds
# the cap, any fd whose number sits past the cutoff is silently invisible
# to this scan, even though it is a genuine, currently-open fd. A freshly
# opened fd is assigned the lowest available number, but in a process that
# already holds many fds without gaps that is a HIGH number -- exactly the
# kind of fd this capture exists to find (the shm fd this process just
# touched at the moment of detection). Measured live on a real, unmodified
# `pytest tests/` run of this repo (no synthetic load, no concurrent
# process): ambient fd count in the SAME process reached 5295 by the time
# TestForensicCapture ran, comfortably above the old 4096 cap -- so this
# was not a pathological-load-only defect, and a long-lived MCP server
# accumulating a few thousand fds over real uptime is exactly the
# production case this capture is meant to serve. Raised to a large,
# still-bounded value so realistic ambient fd counts (test-suite or
# production) don't silently reintroduce the same truncation.


def _capture_locks(
    target_dev_inodes: set[tuple[int, int]], max_rows: Optional[int] = None
) -> dict:
    """Read /proc/locks, keep rows whose (device, inode) matches one of
    target_dev_inodes.

    Returns one of:
        {"status": "unavailable", "why": <str>}
        {"status": "ok", "rows": [<verbatim line>, ...], "pids": [<int>, ...],
         "truncated": bool}

    "unavailable" is returned distinctly from an "ok" result with an empty
    "rows" list — the former means the instrument could not be read at all,
    the latter means it was read and genuinely found no matching lock.  Per
    the spec: os.access() first (a bare `grep -q` inside an `if` cannot make
    this distinction — grep exits 2 on file-error and 1 on no-match, and -q
    destroys the difference).

    Matches on (device, inode), NOT inode alone (reviewer-fairy finding, PR
    #2653): inode numbers are only unique per-filesystem, so an inode-only
    match can mis-attribute a lock on a completely unrelated file sharing
    the same inode number on a different device to the target file, exactly
    the kind of false positive this capture exists to avoid producing in
    the "who else holds a read mark" field.

    max_rows=None (the default) resolves _LOCK_ROWS_LIMIT at CALL time, not
    def time (#2714) -- a plain `max_rows: int = _LOCK_ROWS_LIMIT` binds the
    module constant's value once, when this function is defined, so a
    caller that later monkeypatches `engram_walguard._LOCK_ROWS_LIMIT` (e.g.
    a test that needs a cap value independent of whatever the production
    default happens to be tuned to) has no effect on omitted-argument calls.
    An explicit max_rows= argument (e.g. a size-guard test's max_rows=1) is
    unaffected either way and still takes precedence. Same defect class as
    #2705/#2712's `_capture_fds`/`max_scan` fix.
    """
    if max_rows is None:
        max_rows = _LOCK_ROWS_LIMIT
    path = "/proc/locks"
    if not os.access(path, os.R_OK):
        return {"status": "unavailable", "why": f"{path} not readable (os.access failed)"}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return {"status": "unavailable", "why": f"open({path}) raised: {e!r}"}

    rows: list[str] = []
    pids: set[int] = set()
    truncated = False
    for line in lines:
        if len(rows) >= max_rows:
            truncated = True
            break
        # /proc/locks row shape: "<id>: <type> <mode> <kind> <pid> <maj>:<min>:<inode> <start> <end>"
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            maj_str, min_str, inode_str = parts[5].split(":")
            dev = os.makedev(int(maj_str, 16), int(min_str, 16))
            inode = int(inode_str)
        except (ValueError, IndexError):
            continue
        if (dev, inode) not in target_dev_inodes:
            continue
        rows.append(line.rstrip("\n"))
        try:
            pids.add(int(parts[4]))
        except (ValueError, IndexError):
            pass

    return {
        "status": "ok",
        "rows": rows,
        "pids": sorted(pids),
        "truncated": truncated,
    }


def _capture_fds(paths: dict[str, str], max_scan: Optional[int] = None) -> dict:
    """Resolve /proc/self/fd for every fd targeting one of `paths` (name→path).

    Returns one of:
        {"status": "unavailable", "why": <str>}
        {"status": "ok", "matches": [{"fd", "which", "target", "deleted",
                                       "fd_inode", "disk_inode"}, ...],
         "truncated": bool}

    This is the fd→inode mapping, not a bare fd list: a bare fd list is what
    left #2635's incident ambiguous, because the same fd number means
    opposite things depending on whether its inode still matches disk.
    "deleted" comes from the literal " (deleted)" suffix os/proc appends to
    the readlink target; fd_inode from fstat(fd); disk_inode from stat(path)
    on the live (non-deleted-suffixed) target path, or None when that path
    no longer resolves (covers both the deleted case and any other stat
    failure — e.g. the shm_replaced case where a live file exists at the
    path but under a different inode still resolves normally here).

    max_scan=None (the default) resolves _FD_SCAN_LIMIT at CALL time, not
    def time (#2705) -- a plain `max_scan: int = _FD_SCAN_LIMIT` binds the
    module constant's value once, when this function is defined, so a
    caller that later monkeypatches `engram_walguard._FD_SCAN_LIMIT` (e.g.
    a test that needs a cap value independent of whatever the production
    default happens to be tuned to) has no effect on omitted-argument calls.
    An explicit max_scan= argument (e.g. the size-guard test's max_scan=1)
    is unaffected either way and still takes precedence.
    """
    if max_scan is None:
        max_scan = _FD_SCAN_LIMIT
    proc_fd = "/proc/self/fd"
    if not os.path.isdir(proc_fd):
        return {
            "status": "unavailable",
            "why": f"{proc_fd} not available (non-Linux or /proc unmounted)",
        }
    try:
        # os.listdir intentional (see detect_shm_displacement): closes the
        # dir fd before return, avoiding a transient self-referencing entry.
        entries = os.listdir(proc_fd)
    except OSError as e:
        return {"status": "unavailable", "why": f"listdir({proc_fd}) raised: {e!r}"}

    target_by_path = {p: name for name, p in paths.items()}
    matches: list[dict] = []
    truncated = False
    scanned = 0

    for entry in entries:
        if scanned >= max_scan:
            truncated = True
            break
        try:
            fd_num = int(entry)
        except ValueError:
            continue
        scanned += 1
        try:
            target = os.readlink(os.path.join(proc_fd, entry))
        except OSError:
            continue

        base_target = target.rstrip()
        deleted = base_target.endswith(" (deleted)")
        clean_target = base_target[: -len(" (deleted)")] if deleted else base_target
        if clean_target not in target_by_path:
            continue

        try:
            fd_inode = os.fstat(fd_num).st_ino
        except OSError:
            fd_inode = None

        try:
            disk_inode = os.stat(clean_target).st_ino
        except OSError:
            disk_inode = None

        matches.append({
            "fd": fd_num,
            "which": target_by_path[clean_target],
            "target": target,
            "deleted": deleted,
            "fd_inode": fd_inode,
            "disk_inode": disk_inode,
        })

    return {"status": "ok", "matches": matches, "truncated": truncated}


def capture_forensics(db_path: str) -> dict:
    """Assemble the forensics dict for a live shm-displacement detection.

    Must be called AT DETECTION TIME (before the dump) so the reads land
    while the state that produced the detection is still live — the whole
    point of this capture (#2635) is that a `/proc` listing taken later, once
    the process that produced the displacement is gone, cannot answer
    whether a live connection stood behind the fd or whether it already
    pointed at the dead inode.

    Contract: MUST NEVER RAISE (same contract as the rest of this module —
    a guard that exists to diagnose an outage must not become one itself).
    Any unexpected failure degrades to a top-level {"status": "unavailable",
    "why": ...} rather than propagating.

    Shape:
        {
          "pid": int,
          "files": {"db": {...}, "wal": {...}, "shm": {...}},
          "locks": {...},   # see _capture_locks
          "fds": {...},     # see _capture_fds
        }

    Linux-only capture is expected: on a non-Linux seat, "fds" reads
    {"status": "unavailable", ...} (mirrors detect_shm_displacement's own
    /proc gate) rather than an empty match list that would read as "no fds
    held" — the same false-healthy shape #2651 documents for detection
    itself. "locks" degrades the same way if /proc/locks is absent.
    """
    try:
        wal_path = db_path + "-wal"
        shm_path = db_path + "-shm"
        paths = {"db": db_path, "wal": wal_path, "shm": shm_path}

        files: dict[str, dict] = {}
        target_dev_inodes: set[tuple[int, int]] = set()
        for name, p in paths.items():
            try:
                st = os.stat(p)
                entry = {"exists": True, "st_ino": st.st_ino}
                if name == "wal":
                    entry["size_bytes"] = st.st_size
                files[name] = entry
                # (device, inode), not inode alone: inode numbers are only
                # unique per-filesystem, so matching by inode alone can
                # mis-attribute a lock on an unrelated file that happens to
                # share the same inode number on a different device
                # (reviewer-fairy finding, PR #2653).
                target_dev_inodes.add((st.st_dev, st.st_ino))
            except OSError:
                files[name] = {"exists": False, "st_ino": None}

        return {
            "pid": os.getpid(),
            "files": files,
            "locks": _capture_locks(target_dev_inodes),
            "fds": _capture_fds(paths),
        }
    except Exception as e:
        # Must never raise into the caller — degrade to an explicit
        # unavailable status rather than propagate or silently return {}.
        return {"status": "unavailable", "why": f"capture_forensics raised: {e!r}"}


# ---------------------------------------------------------------------------
# Degraded-state marker
# ---------------------------------------------------------------------------

_MARKER_FILENAME = ".substrate-degraded.json"


def _marker_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / _MARKER_FILENAME


def write_degraded_marker(
    data_dir: str | Path,
    detection: dict,
    dump_info: Optional[dict] = None,
    *,
    detected_at: Optional[str] = None,
    forensics: Optional[dict] = None,
) -> str:
    """Write (or overwrite) the degraded marker atomically.

    Contents written:
        {
          "reason":              str,          # from detection["reason"]
          "detected_at":         str,          # UTC ISO-8601 (original detection time)
          "pid":                 int,          # os.getpid()
          "dump_committed":      bool,         # True when emergency dump succeeded
          "dump_sha":            str | null,   # git SHA of the emergency dump commit
          "last_emergency_dump": str | null,   # UTC ISO-8601 of most recent dump attempt
          "details":             dict,         # full detection dict + any dump_info +
                                                # forensics (#2635) under details["forensics"]
                                                # when `forensics` is provided
          "recovery":            dict,         # recovery_block() (#1947) — condition,
                                                # steps, verify, issue; same source as
                                                # the CRITICAL log line / degraded banner
        }

    Uses write-tmp + os.replace for atomicity (no partial-read window).

    Args:
        data_dir: Directory where the marker file lives.
        detection: Dict from detect_shm_displacement() with at least "reason".
        dump_info: Optional dict from _commit_snapshot() with "git_committed" /
            "commit_sha" / etc.
        detected_at: When provided, used verbatim as the "detected_at" field
            (preserves the original timestamp on hourly re-dumps).  When None,
            set to the current UTC time.
        forensics: Optional dict from capture_forensics() (#2635), taken at
            detection time before the dump.  Stored verbatim under
            details["forensics"] when provided; omitted entirely (not even
            an empty-dict key) when None — additive only, existing callers
            that don't pass it see no change to the marker's shape.

    Returns the marker path as a string.
    """
    target = _marker_path(data_dir)
    dump_committed = bool(dump_info and dump_info.get("git_committed"))
    dump_sha = (dump_info or {}).get("commit_sha", None)
    now_iso = datetime.now(timezone.utc).isoformat()

    details = {**detection, **(dump_info or {})}
    if forensics is not None:
        details["forensics"] = forensics

    payload = {
        "reason": detection.get("reason", "unknown"),
        "detected_at": detected_at if detected_at is not None else now_iso,
        "pid": os.getpid(),
        "dump_committed": dump_committed,
        "dump_sha": dump_sha,
        # Track when the last dump fired; updated on each dump attempt so the
        # hourly-refresh gate in _run_walguard_check can age-check without
        # touching detected_at (which must remain the original detection time).
        "last_emergency_dump": now_iso if dump_info is not None else None,
        "details": details,
        "recovery": recovery_block(),
    }

    # Atomic write: write to a sibling tmp file, then os.replace
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp",
        dir=str(Path(data_dir)),
        prefix=".substrate-degraded-",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, str(target))
    except Exception:
        # Clean up the tmp file on failure; do not leave a partial marker
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return str(target)


def read_degraded_marker(data_dir: str | Path) -> Optional[dict]:
    """Read the degraded marker if it exists.

    Returns the parsed JSON dict, or None when the marker is absent or
    unreadable (treats unreadable as absent — paranoia: the guard must not
    block recovery paths).
    """
    p = _marker_path(data_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def clear_degraded_marker(data_dir: str | Path) -> bool:
    """Remove the degraded marker.

    Returns True if removed, False if it was already absent or removal failed.
    """
    p = _marker_path(data_dir)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False
